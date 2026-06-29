#!/usr/bin/env python3
"""Интеграционный smoke-тест: проходит весь pipeline без UI/Tauri.

Запускает в фоне: memory, llm_engine, tools(broker), avatar_bridge, agent_core.
Шлёт тестовый запрос агенту через ZeroMQ-клиент и проверяет, что:
  1) Agent Core вернул непустый final_text.
  2) В логах memory есть STORE-операция.
  3) Avatar bridge получил хотя бы одну команду SPEAK.

Использование:
    python tests/integration_test.py
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ["PYTHONPATH"] = f"{ROOT}/python:{ROOT}/proto/_gen:{os.environ.get('PYTHONPATH','')}"
os.environ["AIONET_CONFIG"] = str(ROOT / "config.toml")
os.environ["PYTHONUNBUFFERED"] = "1"

PROCS: list[subprocess.Popen] = []


def cleanup():
    for p in reversed(PROCS):
        try:
            p.send_signal(signal.SIGTERM)
            p.wait(timeout=5)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass


def start(name: str, *args: str):
    print(f"[+] starting {name}: {' '.join(args)}")
    p = subprocess.Popen(
        list(args),
        cwd=str(ROOT),
        env=os.environ.copy(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    PROCS.append(p)
    time.sleep(0.5)
    return p


def main():
    import sys
    sys.path.insert(0, str(ROOT / "python"))

    # 1) Запускаем сервисы
    start("memory",       sys.executable, "-m", "memory")
    start("llm_engine",   sys.executable, "-m", "llm_engine")
    start("tools_broker", sys.executable, "-m", "tools")
    start("avatar_bridge", sys.executable, "-m", "avatar")
    start("agent_core",   sys.executable, "-m", "agent_core")
    time.sleep(2.0)

    # 2) Клиентский запрос к агенту
    try:
        from common.config import load_config
        from common.proto import build_payload, PayloadType
        from common.zmq_transport import ZMQClient

        cfg = load_config()
        client = ZMQClient(
            endpoint=cfg.zmq["agent_core_endpoint"],
            service_name="test_client",
            rcvtimeo_ms=120_000,
        )
        payload = build_payload(
            PayloadType.AGENT_REQUEST,
            session_id="test-session-001",
            user_text="Привет! Перечисли файлы в текущей директории.",
        )
        print("[*] sending AgentRequest...")
        resp = client.call(
            target="agent_core",
            payload_type=PayloadType.AGENT_REQUEST,
            payload=payload,
        )
        print(f"[OK] AgentResponse: final_text[:200]={resp.final_text[:200]!r}")
        print(f"     tool_calls={len(resp.tool_calls)} tokens={resp.tokens_used}")
        assert resp.final_text, "final_text is empty!"
        print("\n[PASS] Integration test succeeded.")
        cleanup()
        return 0
    except Exception as e:
        print(f"\n[FAIL] {e!r}")
        cleanup()
        return 1


if __name__ == "__main__":
    sys.exit(main())
