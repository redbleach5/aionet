"""Нагрузочный тест ZeroMQ + FAISS — измеряет пределы системы.

Запускается при поднятых сервисах (bash scripts/start_bg.sh).

Сценарии:
  1) Memory throughput: N операций STORE за T секунд → ops/sec
  2) Memory retrieval latency: p50/p95/p99 для RETRIEVE
  3) LLM Engine throughput: N параллельных запросов → throughput
  4) Agent Core end-to-end: N запросов → latency

Запуск:
    cd /home/z/my-project/local-ai-agent
    bash scripts/start_bg.sh
    PYTHONPATH=python:proto/_gen python tests/test_load.py
"""
from __future__ import annotations

import os
import statistics
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
sys.path.insert(0, str(ROOT / "proto" / "_gen"))
os.environ.setdefault("AIONET_CONFIG", str(ROOT / "config.toml"))

from common.config import load_config
from common.proto import build_payload, PayloadType
from common.zmq_transport import ZMQClient


def header(title: str):
    print(f"\n\033[1m{title}\033[0m")
    print("=" * 60)


def percentile(data: list[float], p: float) -> float:
    """p-перцентиль (p в долях: 0.5, 0.95, 0.99)."""
    if not data:
        return 0.0
    s = sorted(data)
    idx = int(len(s) * p)
    if idx >= len(s):
        idx = len(s) - 1
    return s[idx]


# =============================================================================
# 1. Memory throughput — STORE
# =============================================================================
def test_memory_store_throughput(cfg, n: int = 200):
    header(f"MEMORY STORE throughput (n={n})")
    client = ZMQClient(endpoint=cfg.zmq["memory_endpoint"],
                       service_name="load", rcvtimeo_ms=10_000)
    latencies: list[float] = []
    t0 = time.time()
    for i in range(n):
        ts = time.time()
        payload = build_payload(
            PayloadType.MEMORY_OP, op=0,
            session_id=f"load-{i//50}",  # 50 записей на сессию
            text=f"load test record number {i} with some text to embed",
        )
        res = client.call(target="memory", payload_type=PayloadType.MEMORY_OP,
                          payload=payload)
        latencies.append((time.time() - ts) * 1000)
        if not res.ok:
            print(f"  ✗ STORE failed at i={i}: {res.error}")
            break
    dt = time.time() - t0
    ops_sec = n / dt if dt > 0 else 0
    print(f"  Total: {n} ops in {dt:.2f}s → {ops_sec:.1f} ops/sec")
    print(f"  Latency (ms): p50={percentile(latencies,0.5):.1f} "
          f"p95={percentile(latencies,0.95):.1f} "
          f"p99={percentile(latencies,0.99):.1f} "
          f"max={max(latencies):.1f}")
    client.close()
    return ops_sec


# =============================================================================
# 2. Memory retrieval latency
# =============================================================================
def test_memory_retrieve_latency(cfg, n: int = 100):
    header(f"MEMORY RETRIEVE latency (n={n})")
    client = ZMQClient(endpoint=cfg.zmq["memory_endpoint"],
                       service_name="load", rcvtimeo_ms=10_000)
    # Сначала заполним память
    print("  seeding memory with 50 records...")
    for i in range(50):
        payload = build_payload(PayloadType.MEMORY_OP, op=0,
                                session_id="load-retrieve",
                                text=f"seed record {i} about topic {i%5}")
        client.call(target="memory", payload_type=PayloadType.MEMORY_OP,
                    payload=payload)
    # Теперь меряем RETRIEVE
    latencies: list[float] = []
    queries = ["topic 0", "topic 1", "seed record", "load test", "memory"]
    for i in range(n):
        q = queries[i % len(queries)]
        ts = time.time()
        payload = build_payload(PayloadType.MEMORY_OP, op=1,
                                session_id="load-retrieve",
                                text=q, top_k=5)
        res = client.call(target="memory", payload_type=PayloadType.MEMORY_OP,
                          payload=payload)
        latencies.append((time.time() - ts) * 1000)
        if not res.ok:
            print(f"  ✗ RETRIEVE failed at i={i}: {res.error}")
    print(f"  Total: {n} retrieves")
    print(f"  Latency (ms): p50={percentile(latencies,0.5):.1f} "
          f"p95={percentile(latencies,0.95):.1f} "
          f"p99={percentile(latencies,0.99):.1f} "
          f"max={max(latencies):.1f}")
    print(f"  Avg records returned: querying '{queries[0]}' returned {len(res.records)} last")
    client.close()
    return percentile(latencies, 0.95)


# =============================================================================
# 3. LLM Engine parallel throughput
# =============================================================================
def test_llm_parallel_throughput(cfg, n: int = 20, workers: int = 4):
    header(f"LLM ENGINE parallel throughput (n={n}, workers={workers})")
    def single_call(i: int) -> float:
        client = ZMQClient(endpoint=cfg.zmq["llm_engine_endpoint"],
                           service_name="load", rcvtimeo_ms=30_000)
        from common.proto import _pb
        pb = _pb()
        msg = pb.ChatMessage()
        msg.role = 0
        msg.content = f"test query number {i}"
        ts = time.time()
        payload = build_payload(PayloadType.LLM_CALL, model="mock:test-7b",
                                system_prompt="", static_prefix="test",
                                dynamic_suffix="", messages=[msg],
                                max_tokens=50)
        res = client.call(target="llm_engine",
                          payload_type=PayloadType.LLM_CALL, payload=payload)
        dt = (time.time() - ts) * 1000
        client.close()
        return dt

    latencies: list[float] = []
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(single_call, i) for i in range(n)]
        for f in as_completed(futures):
            try:
                latencies.append(f.result())
            except Exception as e:
                print(f"  ✗ call failed: {e}")
    dt = time.time() - t0
    throughput = n / dt if dt > 0 else 0
    print(f"  Total: {n} calls in {dt:.2f}s → {throughput:.1f} calls/sec")
    print(f"  Latency (ms): p50={percentile(latencies,0.5):.1f} "
          f"p95={percentile(latencies,0.95):.1f} "
          f"p99={percentile(latencies,0.99):.1f} "
          f"max={max(latencies):.1f}")
    return throughput


# =============================================================================
# 4. Agent Core end-to-end
# =============================================================================
def test_agent_e2e_latency(cfg, n: int = 20):
    header(f"AGENT CORE end-to-end latency (n={n})")
    client = ZMQClient(endpoint=cfg.zmq["agent_core_endpoint"],
                       service_name="load", rcvtimeo_ms=30_000)
    latencies: list[float] = []
    queries = [
        "привет",
        "расскажи о себе",
        "что ты умеешь?",
        "перечисли файлы",
        "как дела?",
    ]
    for i in range(n):
        q = queries[i % len(queries)]
        ts = time.time()
        payload = build_payload(PayloadType.AGENT_REQUEST,
                                session_id=f"load-{i//5}",
                                user_text=q)
        res = client.call(target="agent_core",
                          payload_type=PayloadType.AGENT_REQUEST,
                          payload=payload)
        latencies.append((time.time() - ts) * 1000)
        if not res.final_text:
            print(f"  ✗ empty response at i={i}")
    print(f"  Total: {n} requests")
    print(f"  Latency (ms): p50={percentile(latencies,0.5):.1f} "
          f"p95={percentile(latencies,0.95):.1f} "
          f"p99={percentile(latencies,0.99):.1f} "
          f"max={max(latencies):.1f}")
    client.close()
    return percentile(latencies, 0.95)


# =============================================================================
# Main
# =============================================================================
def main():
    print("\033[1m" + "═" * 60 + "\033[0m")
    print("\033[1m  Aionet Load Test Suite\033[0m")
    print("\033[1m" + "═" * 60 + "\033[0m")
    cfg = load_config()
    print(f"  config: {cfg.path}")

    results = {}
    try:
        results["memory_store_throughput_ops_sec"] = test_memory_store_throughput(cfg, n=100)
    except Exception as e:
        print(f"  ✗ memory store test crashed: {e}")
    try:
        results["memory_retrieve_p95_ms"] = test_memory_retrieve_latency(cfg, n=50)
    except Exception as e:
        print(f"  ✗ memory retrieve test crashed: {e}")
    try:
        results["llm_throughput_calls_sec"] = test_llm_parallel_throughput(cfg, n=15, workers=3)
    except Exception as e:
        print(f"  ✗ LLM throughput test crashed: {e}")
    try:
        results["agent_e2e_p95_ms"] = test_agent_e2e_latency(cfg, n=10)
    except Exception as e:
        print(f"  ✗ agent e2e test crashed: {e}")

    print("\n" + "\033[1m" + "═" * 60 + "\033[0m")
    print("\033[1m  LOAD TEST RESULTS\033[0m")
    print("\033[1m" + "═" * 60 + "\033[0m")
    for k, v in results.items():
        print(f"  {k}: {v:.2f}")
    print("\n  [i] These are baseline numbers with mock-ollama (no real LLM latency).")
    print("      In prod with real Ollama + 7B model, expect 5-50× slower LLM calls.")


if __name__ == "__main__":
    main()
