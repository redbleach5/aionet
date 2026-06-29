"""Unit-тесты для Sprint 1: LoopDetector + TaskComplexityClassifier + SystemPromptBuilder.

Запуск:
    cd /home/z/my-project/local-ai-agent
    PYTHONPATH=python:proto/_gen python -m pytest tests/test_sprint1.py -v

Или без pytest:
    PYTHONPATH=python:proto/_gen python tests/test_sprint1.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
os.environ.setdefault("AIONET_CONFIG", str(ROOT / "config.toml"))

from agent_core.loop_detector import (
    LoopDetector, AgentStep, LoopSignalKind,
    detect_pattern_loop, detect_empty_loop, detect_semantic_loop,
)
from agent_core.task_complexity import (
    TaskComplexity, classify_complexity, get_defaults, COMPLEXITY_DEFAULTS,
)
from agent_core.prompt_builder import SystemPromptBuilder, DynamicContext
from common.config import load_config


# =============================================================================
# TaskComplexityClassifier
# =============================================================================
def test_complexity_trivial():
    cases = ["привет", "спасибо!", "ок", "как дела?", "да"]
    for text in cases:
        a = classify_complexity(text)
        assert a.level == TaskComplexity.TRIVIAL, f"expected TRIVIAL for {text!r}, got {a.level}"


def test_complexity_research():
    cases = [
        "найди информацию о новом релизе Python",
        "какая последняя версия React?",
        "поищи benchmark LLM моделей",
        "загугли документацию по API",
    ]
    for text in cases:
        a = classify_complexity(text)
        assert a.level == TaskComplexity.RESEARCH, f"expected RESEARCH for {text!r}, got {a.level}"


def test_complexity_complex():
    cases = [
        "проанализируй архитектуру этого проекта",
        "сравни Kafka и RabbitMQ, плюсы и минусы",
        "рефакторинг этого модуля с обоснованием",
        "обоснуй выбор базы данных для high-load",
    ]
    for text in cases:
        a = classify_complexity(text)
        assert a.level == TaskComplexity.COMPLEX, f"expected COMPLEX for {text!r}, got {a.level}"


def test_complexity_moderate():
    cases = [
        "напиши функцию сортировки пузырьком на Python",
        "создай docker-compose для PostgreSQL",
        "напиши письмо клиенту с извинениями",
    ]
    for text in cases:
        a = classify_complexity(text)
        assert a.level == TaskComplexity.MODERATE, f"expected MODERATE for {text!r}, got {a.level}"


def test_complexity_simple_question():
    a = classify_complexity("Что такое HTTP?")
    assert a.level == TaskComplexity.SIMPLE, f"got {a.level}"
    assert a.has_question is True


def test_complexity_defaults():
    assert get_defaults(TaskComplexity.TRIVIAL)["max_iter"] == 1
    assert get_defaults(TaskComplexity.COMPLEX)["max_iter"] == 5
    assert get_defaults(TaskComplexity.RESEARCH)["web_search"] is True
    assert get_defaults(TaskComplexity.TRIVIAL)["tools"] is False


# =============================================================================
# LoopDetector — Pattern
# =============================================================================
def test_pattern_loop_detected():
    detector = LoopDetector(pattern_limit=2)
    detector.add(AgentStep(thought="t1", tool_name="fs_read",
                           arguments={"path": "/etc/passwd"}, observation="content1"))
    assert detector.check() is None
    detector.add(AgentStep(thought="t2", tool_name="fs_read",
                           arguments={"path": "/etc/passwd"}, observation="content2"))
    assert detector.check() is None
    detector.add(AgentStep(thought="t3", tool_name="fs_read",
                           arguments={"path": "/etc/passwd"}, observation="content3"))
    sig = detector.check()
    assert sig is not None
    assert sig.kind == LoopSignalKind.PATTERN
    assert "fs_read" in sig.detail


def test_pattern_loop_not_triggered_different_args():
    detector = LoopDetector(pattern_limit=2)
    detector.add(AgentStep(thought="t1", tool_name="fs_read",
                           arguments={"path": "/a"}, observation="x"))
    detector.add(AgentStep(thought="t2", tool_name="fs_read",
                           arguments={"path": "/b"}, observation="y"))
    detector.add(AgentStep(thought="t3", tool_name="fs_read",
                           arguments={"path": "/c"}, observation="z"))
    assert detector.check() is None  # разные args — не цикл


def test_pattern_loop_not_triggered_different_tools():
    detector = LoopDetector(pattern_limit=2)
    for i in range(5):
        detector.add(AgentStep(
            thought=f"t{i}", tool_name=f"tool_{i}",
            arguments={"x": i}, observation=f"r{i}",
        ))
    assert detector.check() is None


# =============================================================================
# LoopDetector — Empty
# =============================================================================
def test_empty_loop_detected():
    """3 подряд true-empty observation (null/none/[]) → EMPTY-сигнал.

    ВАЖНО: используем разные tool-имена, чтобы pattern-loop не сработал
    первым (он приоритетнее). Иначе получили бы PATTERN вместо EMPTY.
    """
    detector = LoopDetector(empty_limit=3, empty_min_length=20)
    detector.add(AgentStep(thought="t1", tool_name="search_a",
                           arguments={}, observation=""))
    detector.add(AgentStep(thought="t2", tool_name="search_b",
                           arguments={}, observation="null"))
    detector.add(AgentStep(thought="t3", tool_name="search_c",
                           arguments={}, observation="[]"))
    sig = detector.check()
    assert sig is not None
    assert sig.kind == LoopSignalKind.EMPTY, f"got {sig.kind}"


def test_empty_loop_NOT_triggered_by_short_valid_results():
    """Короткие валидные ответы (OK/done/42/true) НЕ считаются пустым циклом."""
    detector = LoopDetector(empty_limit=3, empty_min_length=20)
    detector.add(AgentStep(thought="t1", observation="OK"))
    detector.add(AgentStep(thought="t2", observation="done"))
    detector.add(AgentStep(thought="t3", observation="42"))
    sig = detector.check()
    assert sig is None, "Короткие валидные ответы не должны считаться циклом"


def test_empty_loop_NOT_triggered_by_llm_errors():
    """Критичный тест: LLM-инфраструктурные ошибки не считаются пустым циклом."""
    detector = LoopDetector(empty_limit=3, empty_min_length=20)
    detector.add(AgentStep(thought="t1", observation="ECONNREFUSED 127.0.0.1:11434"))
    detector.add(AgentStep(thought="t2", observation="ollama_error: timeout exceeded"))
    detector.add(AgentStep(thought="t3", observation="503 service unavailable"))
    sig = detector.check()
    assert sig is None, "LLM-ошибки не должны считаться циклом"


def test_empty_loop_partial_llm_error_no_signal():
    """Даже если 2 пустых + 1 LLM-ошибка — не считаем циклом (LLM-ошибка прерывает подсчёт)."""
    detector = LoopDetector(empty_limit=3, empty_min_length=20)
    detector.add(AgentStep(thought="t1", observation=""))  # пусто
    detector.add(AgentStep(thought="t2", observation="x"))  # короткое
    detector.add(AgentStep(thought="t3", observation="connect econnrefused"))  # LLM-ошибка
    sig = detector.check()
    assert sig is None


# =============================================================================
# LoopDetector — Semantic
# =============================================================================
def test_semantic_loop_detected():
    # Тривиальный embed_fn: одинаковые строки → cosine=1.0
    def embed(text: str) -> list[float]:
        if "одинаковая" in text:
            return [1.0, 0.0, 0.0]
        return [0.0, 1.0, 0.0]

    detector = LoopDetector(
        semantic_threshold=0.85, semantic_window=3, embed_fn=embed,
    )
    detector.add(AgentStep(thought="одинаковая мысль 1", observation="r1"))
    detector.add(AgentStep(thought="одинаковая мысль 2", observation="r2"))
    detector.add(AgentStep(thought="одинаковая мысль 3", observation="r3"))
    sig = detector.check()
    assert sig is not None
    assert sig.kind == LoopSignalKind.SEMANTIC
    assert "cosine" in sig.detail


def test_semantic_loop_no_embed_fn_skipped():
    detector = LoopDetector(semantic_threshold=0.85, embed_fn=None)
    for i in range(5):
        detector.add(AgentStep(thought="identical thought", observation="r"))
    assert detector.check() is None  # без embed_fn semantic не работает


def test_semantic_loop_different_thoughts_no_signal():
    def embed(text: str) -> list[float]:
        # Ортогональные векторы для разных thought'ов
        vectors = {
            "alpha": [1.0, 0.0, 0.0],
            "beta":  [0.0, 1.0, 0.0],
            "gamma": [0.0, 0.0, 1.0],
        }
        for key, v in vectors.items():
            if key in text:
                return v
        return [0.5, 0.5, 0.5]

    detector = LoopDetector(semantic_threshold=0.85, semantic_window=3, embed_fn=embed)
    detector.add(AgentStep(thought="alpha thought", observation="r1"))
    detector.add(AgentStep(thought="beta thought", observation="r2"))
    detector.add(AgentStep(thought="gamma thought", observation="r3"))
    assert detector.check() is None  # разные → cosine=0


# =============================================================================
# LoopDetector — комплексный
# =============================================================================
def test_detector_summary():
    detector = LoopDetector()
    detector.add(AgentStep(thought="t1", tool_name="t", arguments={}, observation="r1"))
    s = detector.summary()
    assert s["steps_count"] == 1
    assert s["last_signal"] is None
    assert "pattern_limit" in s["config"]


def test_detector_reset():
    detector = LoopDetector()
    detector.add(AgentStep(thought="t", observation="r"))
    assert len(detector.steps) == 1
    detector.reset()
    assert len(detector.steps) == 0


# =============================================================================
# SystemPromptBuilder
# =============================================================================
def test_prompt_builder_legacy_fallback():
    """Если system_prompt_static не задан — fallback на legacy."""
    cfg = load_config()
    # Сохраняем оригинал, убираем static
    original = cfg.llm.get("system_prompt_static", "")
    try:
        cfg.raw["llm"]["system_prompt_static"] = ""
        builder = SystemPromptBuilder(cfg)
        assert not builder.has_explicit_static
        ctx = DynamicContext(complexity_level="simple")
        static, dynamic = builder.build(ctx)
        assert static is None
        assert dynamic is None
    finally:
        cfg.raw["llm"]["system_prompt_static"] = original


def test_prompt_builder_split_mode():
    """Если system_prompt_static задан — split-режим."""
    cfg = load_config()
    builder = SystemPromptBuilder(cfg)
    if not builder.has_explicit_static:
        # В конфиге нет static — пропускаем
        return
    ctx = DynamicContext(
        complexity_level="complex",
        complexity_description="сложная задача",
        memory_context="факт1\nфакт2",
    )
    static, dynamic = builder.build(ctx)
    assert static is not None
    assert "Aionet" in static  # personality в static
    assert dynamic is not None
    assert "СЛОЖНОСТЬ ЗАДАЧИ" in dynamic
    assert "РЕЛЕВАНТНЫЕ ВОСПОМИНАНИЯ" in dynamic


def test_prompt_builder_empty_context():
    """Пустой dynamic_context → dynamic_suffix может быть None."""
    cfg = load_config()
    builder = SystemPromptBuilder(cfg)
    if not builder.has_explicit_static:
        return
    static, dynamic = builder.build(DynamicContext())
    assert static is not None
    # Все поля пустые → dynamic может быть None или ""
    assert dynamic is None or dynamic == ""


# =============================================================================
# Запуск без pytest
# =============================================================================
def _run_all():
    tests = [
        ("test_complexity_trivial", test_complexity_trivial),
        ("test_complexity_research", test_complexity_research),
        ("test_complexity_complex", test_complexity_complex),
        ("test_complexity_moderate", test_complexity_moderate),
        ("test_complexity_simple_question", test_complexity_simple_question),
        ("test_complexity_defaults", test_complexity_defaults),
        ("test_pattern_loop_detected", test_pattern_loop_detected),
        ("test_pattern_loop_not_triggered_different_args", test_pattern_loop_not_triggered_different_args),
        ("test_pattern_loop_not_triggered_different_tools", test_pattern_loop_not_triggered_different_tools),
        ("test_empty_loop_detected", test_empty_loop_detected),
        ("test_empty_loop_NOT_triggered_by_short_valid_results", test_empty_loop_NOT_triggered_by_short_valid_results),
        ("test_empty_loop_NOT_triggered_by_llm_errors", test_empty_loop_NOT_triggered_by_llm_errors),
        ("test_empty_loop_partial_llm_error_no_signal", test_empty_loop_partial_llm_error_no_signal),
        ("test_semantic_loop_detected", test_semantic_loop_detected),
        ("test_semantic_loop_no_embed_fn_skipped", test_semantic_loop_no_embed_fn_skipped),
        ("test_semantic_loop_different_thoughts_no_signal", test_semantic_loop_different_thoughts_no_signal),
        ("test_detector_summary", test_detector_summary),
        ("test_detector_reset", test_detector_reset),
        ("test_prompt_builder_legacy_fallback", test_prompt_builder_legacy_fallback),
        ("test_prompt_builder_split_mode", test_prompt_builder_split_mode),
        ("test_prompt_builder_empty_context", test_prompt_builder_empty_context),
    ]
    passed = 0
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ✓ {name}")
            passed += 1
        except AssertionError as e:
            print(f"  ✗ {name}: {e}")
            failed += 1
        except Exception as e:
            print(f"  ✗ {name}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{'='*60}")
    print(f"  PASSED: {passed}/{passed+failed}")
    if failed:
        print(f"  FAILED: {failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(_run_all())
