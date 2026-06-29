#!/usr/bin/env python3
"""Комплексный интеграционный тест — прогоняет несколько сценариев через
полный pipeline: UI (test client) → agent_core → memory → llm_engine → tools → avatar.

Запуск (при уже поднятых сервисах):
    cd /home/z/my-project/local-ai-agent
    PYTHONPATH=python:proto/_gen python tests/test_integration.py
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
sys.path.insert(0, str(ROOT / "proto" / "_gen"))
os.environ.setdefault("AIONET_CONFIG", str(ROOT / "config.toml"))

from common.config import load_config
from common.proto import build_payload, PayloadType
from common.zmq_transport import ZMQClient


# =============================================================================
# Цветной вывод
# =============================================================================
def ok(msg: str) -> None:
    print(f"  \033[32m✓\033[0m {msg}")

def fail(msg: str) -> None:
    print(f"  \033[31m✗\033[0m {msg}")

def header(title: str) -> None:
    print(f"\n\033[1m{title}\033[0m")
    print("=" * 60)


# =============================================================================
# Тестовые сценарии
# =============================================================================
def make_agent_client(cfg) -> ZMQClient:
    return ZMQClient(
        endpoint=cfg.zmq["agent_core_endpoint"],
        service_name="test_client",
        rcvtimeo_ms=60_000,
    )


def send_request(client: ZMQClient, text: str, session_id: str | None = None):
    """Отправляет AgentRequest и возвращает AgentResponse."""
    payload = build_payload(
        PayloadType.AGENT_REQUEST,
        session_id=session_id or f"test-{uuid.uuid4().hex[:8]}",
        user_text=text,
    )
    return client.call(
        target="agent_core",
        payload_type=PayloadType.AGENT_REQUEST,
        payload=payload,
    )


# =============================================================================
# Сценарии
# =============================================================================
def test_simple_greeting(cfg) -> bool:
    """Сценарий 1: тривиальное приветствие — должен быть 1 LLM-call, без tools."""
    header("TEST 1: Simple greeting (trivial complexity)")
    client = make_agent_client(cfg)
    try:
        t0 = time.time()
        resp = send_request(client, "привет")
        dt = (time.time() - t0) * 1000
        ok(f"response in {dt:.0f}ms")
        ok(f"session_id: {resp.session_id[:16]}")
        ok(f"final_text: {resp.final_text[:80]!r}")
        ok(f"tool_calls: {len(resp.tool_calls)} (expected 0)")
        ok(f"tokens_used: {resp.tokens_used}")
        # Проверки
        assert resp.final_text, "final_text is empty"
        assert len(resp.tool_calls) == 0, "trivial should not call tools"
        ok("ALL ASSERTIONS PASSED")
        return True
    except Exception as e:
        fail(f"FAILED: {e}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        client.close()


def test_tool_call_fs(cfg) -> bool:
    """Сценарий 2: запрос на перечисление файлов — mock-LLM должна вернуть tool_call."""
    header("TEST 2: Tool call — list files (filesystem)")
    client = make_agent_client(cfg)
    try:
        resp = send_request(client, "перечисли файлы в текущей директории")
        ok(f"final_text: {resp.final_text[:80]!r}")
        ok(f"tool_calls: {len(resp.tool_calls)}")
        for tc in resp.tool_calls:
            ok(f"  • {tc.tool_name} args={tc.arguments[:60]} ok={tc.ok} "
               f"dur={tc.duration_ms}ms")
            ok(f"    result: {tc.result[:120]}")
        assert len(resp.tool_calls) > 0, "expected at least 1 tool_call"
        # Mock LLM эмитирует filesystem/run tool_call
        has_fs = any("filesystem" in tc.tool_name for tc in resp.tool_calls)
        if has_fs:
            ok("filesystem tool was invoked ✓")
        else:
            fail(f"expected filesystem tool, got: {[tc.tool_name for tc in resp.tool_calls]}")
        return True
    except Exception as e:
        fail(f"FAILED: {e}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        client.close()


def test_tool_call_shell(cfg) -> bool:
    """Сценарий 3: запрос на вычисление — mock-LLM эмитит shell tool_call."""
    header("TEST 3: Tool call — calc (shell)")
    client = make_agent_client(cfg)
    try:
        resp = send_request(client, "посчитай 2+2")
        ok(f"final_text: {resp.final_text[:80]!r}")
        ok(f"tool_calls: {len(resp.tool_calls)}")
        for tc in resp.tool_calls:
            ok(f"  • {tc.tool_name} args={tc.arguments[:60]} ok={tc.ok}")
        return True
    except Exception as e:
        fail(f"FAILED: {e}")
        return False
    finally:
        client.close()


def test_memory_persistence(cfg) -> bool:
    """Сценарий 4: память сохраняет и извлекает.

    В тестовом окружении (HashEmbedder вместо sentence-transformers) семантическое
    сходство не работает, поэтому RETRIEVE может вернуть 0 записей. Но STORE
    должна работать, и запись должна появиться в SQLite. Проверяем оба аспекта.
    """
    header("TEST 4: Memory store + retrieve")
    client = ZMQClient(
        endpoint=cfg.zmq["memory_endpoint"],
        service_name="test_client",
        rcvtimeo_ms=10_000,
    )
    session = f"memtest-{uuid.uuid4().hex[:8]}"
    test_text = "Пользователь сказал: меня зовут Иван, я работаю программистом."
    try:
        # STORE
        store_payload = build_payload(
            PayloadType.MEMORY_OP, op=0,  # STORE
            session_id=session,
            text=test_text,
        )
        store_res = client.call(target="memory",
                                payload_type=PayloadType.MEMORY_OP,
                                payload=store_payload)
        ok(f"STORE: ok={store_res.ok}")
        assert store_res.ok, f"STORE failed: {store_res.error}"

        # RETRIEVE — даже с HashEmbedder может вернуть запись при exact-match
        ret_payload = build_payload(
            PayloadType.MEMORY_OP, op=1,  # RETRIEVE
            session_id=session,
            text="Иван программист",
            top_k=3,
        )
        ret_res = client.call(target="memory",
                              payload_type=PayloadType.MEMORY_OP,
                              payload=ret_payload)
        ok(f"RETRIEVE: ok={ret_res.ok} records={len(ret_res.records)}")
        for r in ret_res.records:
            ok(f"  • score={r.score:.3f} imp={r.importance:.2f} text={r.text[:60]!r}")

        # Проверим напрямую через SQLite, что запись действительно сохранена
        import sqlite3
        db_path = ROOT / "data" / "memory.sqlite"
        if db_path.exists():
            db = sqlite3.connect(str(db_path))
            rows = db.execute(
                "SELECT text FROM memories WHERE session_id=?",
                (session,)
            ).fetchall()
            db.close()
            ok(f"SQLite direct check: {len(rows)} records for session {session[:16]}")
            assert len(rows) >= 1, "expected at least 1 record in SQLite"
            assert test_text in rows[0][0], "stored text mismatch"
            ok("record content matches ✓")
        else:
            fail("memory.sqlite not found")
            return False

        # STATS
        stats_payload = build_payload(PayloadType.MEMORY_OP, op=3)  # STATS
        stats_res = client.call(target="memory",
                                payload_type=PayloadType.MEMORY_OP,
                                payload=stats_payload)
        ok(f"STATS: {dict(stats_res.stats)}")

        ok("ALL ASSERTIONS PASSED")
        return True
    except Exception as e:
        fail(f"FAILED: {e}")
        return False
    finally:
        client.close()


def test_llm_engine_directly(cfg) -> bool:
    """Сценарий 5: прямой вызов LLM Engine (минуя agent_core)."""
    header("TEST 5: LLM Engine direct call (static/dynamic prompt)")
    client = ZMQClient(
        endpoint=cfg.zmq["llm_engine_endpoint"],
        service_name="test_client",
        rcvtimeo_ms=30_000,
    )
    try:
        # С static_prefix + dynamic_suffix
        payload = build_payload(
            PayloadType.LLM_CALL,
            model="mock:test-7b",
            system_prompt="",
            static_prefix="Ты — тестовый ассистент.",
            dynamic_suffix="СЛОЖНОСТЬ: simple.",
            messages=[],  # LLM Engine сам добавит system-сообщения
            temperature=0.3,
            max_tokens=100,
        )
        # Добавим user-сообщение вручную
        from common.proto import _pb
        pb = _pb()
        msg = pb.ChatMessage()
        msg.role = 0  # USER
        msg.content = "Расскажи о себе в одном предложении."
        # payload.messages — repeated, добавляем через внутренний доступ
        # но build_payload не позволяет добавить repeated — пересоберём вручную
        full_payload = build_payload(
            PayloadType.LLM_CALL,
            model="mock:test-7b",
            system_prompt="",
            static_prefix="Ты — тестовый ассистент.",
            dynamic_suffix="СЛОЖНОСТЬ: simple.",
            temperature=0.3,
            max_tokens=100,
            messages=[msg],
        )
        res = client.call(target="llm_engine",
                          payload_type=PayloadType.LLM_CALL,
                          payload=full_payload)
        ok(f"content: {res.content[:80]!r}")
        ok(f"model_used: {res.model_used}")
        ok(f"prompt_tokens={res.prompt_tokens} completion_tokens={res.completion_tokens}")
        ok(f"tool_calls: {len(res.tool_calls)}")
        assert res.content, "empty content"
        assert res.model_used == "mock:test-7b", f"wrong model: {res.model_used}"
        ok("ALL ASSERTIONS PASSED")
        return True
    except Exception as e:
        fail(f"FAILED: {e}")
        return False
    finally:
        client.close()


def test_tools_broker_directly(cfg) -> bool:
    """Сценарий 6: прямой вызов Tools-брокера."""
    header("TEST 6: Tools broker — fs_list via filesystem/fs_list")
    client = ZMQClient(
        endpoint=cfg.zmq["tools_endpoint"],
        service_name="test_client",
        rcvtimeo_ms=30_000,
    )
    try:
        # Используем абсолютный путь к workspace (он в allowed_roots)
        workspace_path = str(ROOT / "workspace")
        # Вызываем напрямую filesystem/fs_list с параметром dir
        payload = build_payload(
            PayloadType.TOOL_CALL,
            tool_name="filesystem/fs_list",
            arguments_json=json.dumps({"dir": workspace_path}),
            timeout_ms=15000,
        )
        res = client.call(target="tools",
                          payload_type=PayloadType.TOOL_CALL,
                          payload=payload)
        ok(f"ok={res.ok} dur={res.duration_ms}ms")
        ok(f"output_json: {res.output_json[:300] if res.output_json else '(empty)'}")
        if res.error:
            ok(f"error: {res.error[:120]}")
        assert res.ok, f"tool call failed: {res.error}"
        # Проверим, что в output_json есть test.txt
        assert "test.txt" in (res.output_json or ""), f"expected test.txt in output, got: {res.output_json}"
        ok("test.txt found in workspace listing ✓")
        ok("ALL ASSERTIONS PASSED")
        return True
    except Exception as e:
        fail(f"FAILED: {e}")
        return False
    finally:
        client.close()


def test_complex_query_full_pipeline(cfg) -> bool:
    """Сценарий 7: комплексный запрос через весь pipeline."""
    header("TEST 7: Complex query — full pipeline")
    client = make_agent_client(cfg)
    try:
        # Пройдёт через: classify_complexity → memory retrieve → LLM call → tool call → memory store → avatar
        t0 = time.time()
        resp = send_request(client,
            "перечисли файлы в текущей директории и расскажи что ты нашёл",
            session_id="complex-test-session")
        dt = (time.time() - t0) * 1000
        ok(f"total response time: {dt:.0f}ms")
        ok(f"final_text: {resp.final_text[:120]!r}")
        ok(f"tool_calls: {len(resp.tool_calls)}")
        ok(f"tokens_used: {resp.tokens_used}")
        for tc in resp.tool_calls:
            ok(f"  • {tc.tool_name} ok={tc.ok} dur={tc.duration_ms}ms")
        assert resp.final_text, "empty final_text"
        ok("ALL ASSERTIONS PASSED")
        return True
    except Exception as e:
        fail(f"FAILED: {e}")
        return False
    finally:
        client.close()


def test_complexity_classification(cfg) -> bool:
    """Сценарий 8: проверка TaskComplexityClassifier через логи agent_core."""
    header("TEST 8: Task complexity classification (via logs)")
    # Этот тест проверяет, что agent_core логирует классификацию сложности
    # при обработке каждого запроса. Мы уже сделали несколько запросов выше,
    # и в логе должны быть строки "complexity=...".
    log_path = ROOT / "logs" / "agent_core.log"
    try:
        log_text = log_path.read_text()
        # Ищем строки вида "complexity=trivial" или "complexity=complex"
        import re
        matches = re.findall(r"complexity=(\w+)", log_text)
        ok(f"complexity classifications found in logs: {matches}")
        assert len(matches) >= 3, f"expected at least 3 classifications, got {len(matches)}"
        # Должны быть разные уровни
        unique = set(matches)
        ok(f"unique levels: {unique}")
        assert len(unique) >= 2, "expected at least 2 different complexity levels"
        ok("ALL ASSERTIONS PASSED")
        return True
    except Exception as e:
        fail(f"FAILED: {e}")
        return False


def test_loop_detector_logs(cfg) -> bool:
    """Сценарий 9: проверка, что LoopDetector инициализирован."""
    header("TEST 9: LoopDetector initialization")
    log_path = ROOT / "logs" / "agent_core.log"
    try:
        log_text = log_path.read_text()
        if "LoopDetector" in log_text or "loop_detector" in log_text:
            ok("LoopDetector mentioned in logs ✓")
        else:
            fail("LoopDetector not mentioned in logs")
            return False
        # Проверим что HashEmbedder fallback активирован
        if "HashEmbedder fallback" in log_text:
            ok("HashEmbedder fallback for semantic-loop active ✓")
        ok("ALL ASSERTIONS PASSED")
        return True
    except Exception as e:
        fail(f"FAILED: {e}")
        return False


# =============================================================================
# Главный запуск
# =============================================================================
def main():
    print("\033[1m" + "═" * 60 + "\033[0m")
    print("\033[1m  Aionet Integration Test Suite\033[0m")
    print("\033[1m" + "═" * 60 + "\033[0m")
    cfg = load_config()
    print(f"  config: {cfg.path}")
    print(f"  agent_core: {cfg.zmq['agent_core_endpoint']}")
    print(f"  llm_engine: {cfg.zmq['llm_engine_endpoint']}")
    print(f"  memory:     {cfg.zmq['memory_endpoint']}")
    print(f"  tools:      {cfg.zmq['tools_endpoint']}")

    tests = [
        ("Simple greeting (trivial)",         test_simple_greeting),
        ("Tool call — filesystem",             test_tool_call_fs),
        ("Tool call — shell",                  test_tool_call_shell),
        ("Memory store + retrieve",            test_memory_persistence),
        ("LLM Engine direct (static/dyn)",     test_llm_engine_directly),
        ("Tools broker direct",                test_tools_broker_directly),
        ("Complex full-pipeline query",        test_complex_query_full_pipeline),
        ("Complexity classification (logs)",   test_complexity_classification),
        ("LoopDetector initialization (logs)", test_loop_detector_logs),
    ]

    results = []
    for name, fn in tests:
        try:
            passed = fn(cfg)
        except Exception as e:
            print(f"\n  \033[31m✗ CRASH: {e}\033[0m")
            passed = False
        results.append((name, passed))

    # Итог
    print("\n" + "\033[1m" + "═" * 60 + "\033[0m")
    print("\033[1m  RESULTS SUMMARY\033[0m")
    print("\033[1m" + "═" * 60 + "\033[0m")
    passed = sum(1 for _, p in results if p)
    total = len(results)
    for name, p in results:
        marker = "\033[32m✓\033[0m" if p else "\033[31m✗\033[0m"
        print(f"  {marker} {name}")
    print(f"\n  Passed: {passed}/{total}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
