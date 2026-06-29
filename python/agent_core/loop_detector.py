"""LoopDetector — детектор зацикливания агентского цикла.

Три независимых сигнала (вдохновлено Lia-v2, но переписано под нашу архитектуру):

1. **Pattern loop** — один и тот же (tool_name, arguments) повторяется > N раз
   подряд. Например, агент вызывает `fs_read('/etc/passwd')` 3 раза кряду.
2. **Empty loop** — K подряд observation пустые или слишком короткие (<20 символов).
   ВАЖНО: ошибки LLM (timeout, ECONNREFUSED, AI_APICallError) НЕ считаются
   "пустым результатом". Это инфраструктурная проблема, не цикл.
3. **Semantic loop** — embedding последних M thought'ов; если max pairwise
   cosine similarity ≥ 0.85 → мысли дублируются, агент топчется на месте.

При срабатывании любого сигнала — агент должен остановиться и либо спросить
пользователя (если есть UI), либо синтезировать ответ по накопленным шагам.

Этот модуль НЕ зависит от FAISS / sentence-transformers напрямую — embedding
считается через MemoryStore (если доступен) или через LLM-engine. Если
embedding-вызов недоступен — semantic-loop пропускается (non-fatal).
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

from common.logging import get_logger

log = get_logger(__name__)


# =============================================================================
# Параметры (можно переопределять из config.toml: [agent.loop_detector])
# =============================================================================
class LoopDetectorConfig:
    PATTERN_LIMIT = 2          # > 2 одинаковых (tool+args) подряд → signal
    EMPTY_LIMIT = 3            # 3 пустых observation подряд → signal
    EMPTY_MIN_LENGTH = 20      # observation короче 20 символов считаем пустым
    SEMANTIC_THRESHOLD = 0.85  # max pairwise cosine ≥ 0.85 → signal
    SEMANTIC_WINDOW = 3        # проверяем последние 3 thought'а
    LLM_ERROR_MARKERS = (
        "streamtext timeout", "plan generation timeout", "synthesize timeout",
        "no output generated", "ai_apicallerror", "ai_retryerror",
        "ai_nooutputgeneratederror", "econnrefused", "fetch failed",
        "connect econnrefused", "ollama_error", "llm_timeout",
        "timeout exceeded", "connection reset", "503 service unavailable",
        "504 gateway timeout", "internal server error",
    )


class LoopSignalKind(str, Enum):
    PATTERN = "pattern"
    EMPTY = "empty"
    SEMANTIC = "semantic"


@dataclass
class LoopSignal:
    """Конкретное срабатывание детектора."""
    kind: LoopSignalKind
    detail: str
    severity: float = 1.0   # 0..1, для приоритизации при множественных сигналах
    suggested_action: str = "pause_and_ask_user"


@dataclass
class AgentStep:
    """Один шаг цикла агента — нужна для детектора."""
    thought: str = ""                # что агент "думал" перед action
    tool_name: str = ""              # имя вызванного инструмента (или "")
    arguments: dict[str, Any] = field(default_factory=dict)
    observation: str = ""            # результат инструмента / LLM
    timestamp: float = field(default_factory=time.time)


# =============================================================================
# Утилиты
# =============================================================================
def _is_llm_error(observation: str) -> bool:
    """LLM-инфраструктурные ошибки не считаются циклом."""
    if not observation:
        return False
    lower = observation.lower()
    return any(marker in lower for marker in LoopDetectorConfig.LLM_ERROR_MARKERS)


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na > 0 and nb > 0 else 0.0


def _args_key(arguments: dict[str, Any] | str | None) -> str:
    """Стабильный ключ из arguments — для pattern-detection."""
    if arguments is None:
        return ""
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            return arguments
    try:
        return json.dumps(arguments, sort_keys=True, ensure_ascii=False,
                          default=str)
    except (TypeError, ValueError):
        return str(arguments)


# =============================================================================
# Детекторы — каждый возвращает LoopSignal | None
# =============================================================================
def detect_pattern_loop(steps: list[AgentStep],
                        limit: int = LoopDetectorConfig.PATTERN_LIMIT
                        ) -> Optional[LoopSignal]:
    """Одинаковый (tool, args) > `limit` раз подряд."""
    if len(steps) < limit + 1:
        return None
    last = steps[-1]
    if not last.tool_name:
        return None
    key = f"{last.tool_name}::{_args_key(last.arguments)}"
    count = 1
    for s in reversed(steps[:-1]):
        if not s.tool_name:
            break
        skey = f"{s.tool_name}::{_args_key(s.arguments)}"
        if skey == key:
            count += 1
            if count > limit:
                return LoopSignal(
                    kind=LoopSignalKind.PATTERN,
                    detail=(f"Tool '{last.tool_name}' with identical args "
                            f"called {count} times in a row"),
                    severity=min(1.0, 0.5 + count * 0.15),
                )
        else:
            break
    return None


def detect_empty_loop(steps: list[AgentStep],
                      limit: int = LoopDetectorConfig.EMPTY_LIMIT,
                      min_length: int = LoopDetectorConfig.EMPTY_MIN_LENGTH
                      ) -> Optional[LoopSignal]:
    """K подряд пустых/коротких observation. LLM-ошибки не считаются.

    "Пустым" считаем:
      * буквально пустую строку (или только whitespace)
      * строку из списка known-empty markers: "null", "none", "[]", "{}", "no results"
      * ЯВНО короткий результат (< min_length) И при этом tool вернул ok=False
        (т.е. это не валидный короткий ответ вроде "OK" / "42", а обрубленная ошибка)

    Важно: короткие валидные ответы ("OK", "done", "42", "true") НЕ считаются
    пустыми — это нормальные результаты tool-вызовов. Считать их циклом = false positive.
    """
    if len(steps) < limit:
        return None
    last_n = steps[-limit:]
    empty_count = 0
    for s in last_n:
        obs = (s.observation or "").strip()
        # LLM-инфраструктурная ошибка прерывает подсчёт — это не цикл.
        if _is_llm_error(obs):
            return None
        # Истинная пустота (пустая строка или whitespace-only)
        if len(obs) == 0:
            empty_count += 1
            continue
        # Known-empty markers (null/none/пустые JSON-структуры)
        if obs.lower() in ("null", "none", "nil", "[]", "{}", '""', "''",
                            "no results", "no output", "пусто", "нет данных"):
            empty_count += 1
            continue
        # Короткий результат БЕЗ признаков ошибки — НЕ считаем пустым.
        # "OK", "done", "42", "true", "yes" — валидные ответы инструментов.
    if empty_count >= limit:
        return LoopSignal(
            kind=LoopSignalKind.EMPTY,
            detail=f"{empty_count} consecutive empty/null observations",
            severity=0.7,
        )
    return None


def detect_semantic_loop(steps: list[AgentStep],
                         embed_fn: Callable[[str], list[float]] | None,
                         threshold: float = LoopDetectorConfig.SEMANTIC_THRESHOLD,
                         window: int = LoopDetectorConfig.SEMANTIC_WINDOW
                         ) -> Optional[LoopSignal]:
    """Embedding последних M thought'ов; max pairwise cosine ≥ threshold."""
    if embed_fn is None:
        return None  # embedding недоступен — non-fatal skip
    if len(steps) < window:
        return None
    recent = steps[-window:]
    thoughts = [s.thought for s in recent]
    if any(not t or not t.strip() for t in thoughts):
        return None
    try:
        embeddings = [embed_fn(t) for t in thoughts]
    except Exception as e:
        log.debug("semantic-loop embed failed (non-fatal): %s", e)
        return None
    if any(not e for e in embeddings):
        return None
    max_sim = 0.0
    max_pair: tuple[int, int] | None = None
    for i in range(len(embeddings)):
        for j in range(i + 1, len(embeddings)):
            sim = _cosine(embeddings[i], embeddings[j])
            if sim > max_sim:
                max_sim = sim
                max_pair = (i, j)
    if max_sim >= threshold and max_pair is not None:
        return LoopSignal(
            kind=LoopSignalKind.SEMANTIC,
            detail=(f"Thoughts {max_pair[0]} and {max_pair[1]} are too similar "
                    f"(cosine={max_sim:.3f})"),
            severity=min(1.0, max_sim),
        )
    return None


# =============================================================================
# Главный класс — агрегирует все три детектора
# =============================================================================
class LoopDetector:
    """Хранит историю шагов и предоставляет `check()` после каждого шага.

    Использование в AgentRuntime::

        detector = LoopDetector(cfg, embed_fn=memory.embed)
        for iteration in range(max_iter):
            step = ...  # выполнить итерацию
            detector.add(step)
            signal = detector.check()
            if signal:
                # пауза, ask_user или synthesize по накопленным шагам
                break
    """

    def __init__(self,
                 pattern_limit: int = LoopDetectorConfig.PATTERN_LIMIT,
                 empty_limit: int = LoopDetectorConfig.EMPTY_LIMIT,
                 empty_min_length: int = LoopDetectorConfig.EMPTY_MIN_LENGTH,
                 semantic_threshold: float = LoopDetectorConfig.SEMANTIC_THRESHOLD,
                 semantic_window: int = LoopDetectorConfig.SEMANTIC_WINDOW,
                 embed_fn: Callable[[str], list[float]] | None = None):
        self.pattern_limit = pattern_limit
        self.empty_limit = empty_limit
        self.empty_min_length = empty_min_length
        self.semantic_threshold = semantic_threshold
        self.semantic_window = semantic_window
        self.embed_fn = embed_fn
        self._steps: list[AgentStep] = []
        self._last_signal: LoopSignal | None = None

    def reset(self) -> None:
        self._steps.clear()
        self._last_signal = None

    def add(self, step: AgentStep) -> None:
        self._steps.append(step)

    @property
    def steps(self) -> list[AgentStep]:
        return list(self._steps)

    def check(self) -> LoopSignal | None:
        """Запускает все 3 детектора, возвращает первый сработавший."""
        # Pattern и empty — дешёвые, синхронные.
        sig = detect_pattern_loop(self._steps, self.pattern_limit)
        if sig is not None:
            self._last_signal = sig
            log.warning("LOOP DETECTED [%s]: %s", sig.kind.value, sig.detail)
            return sig
        sig = detect_empty_loop(self._steps, self.empty_limit,
                                self.empty_min_length)
        if sig is not None:
            self._last_signal = sig
            log.warning("LOOP DETECTED [%s]: %s", sig.kind.value, sig.detail)
            return sig
        # Semantic — тяжёлый (embedding), запускаем последним.
        sig = detect_semantic_loop(self._steps, self.embed_fn,
                                   self.semantic_threshold,
                                   self.semantic_window)
        if sig is not None:
            self._last_signal = sig
            log.warning("LOOP DETECTED [%s]: %s", sig.kind.value, sig.detail)
            return sig
        return None

    @property
    def last_signal(self) -> LoopSignal | None:
        return self._last_signal

    def summary(self) -> dict[str, Any]:
        """Для логов / UI — текущее состояние детектора."""
        return {
            "steps_count": len(self._steps),
            "last_signal": (
                {"kind": self._last_signal.kind.value,
                 "detail": self._last_signal.detail,
                 "severity": self._last_signal.severity}
                if self._last_signal else None
            ),
            "config": {
                "pattern_limit": self.pattern_limit,
                "empty_limit": self.empty_limit,
                "semantic_threshold": self.semantic_threshold,
                "semantic_window": self.semantic_window,
            },
        }


# =============================================================================
# Фабрика — собирает LoopDetector из config.toml
# =============================================================================
def make_loop_detector(cfg, embed_fn: Callable[[str], list[float]] | None = None
                       ) -> LoopDetector:
    """Создаёт LoopDetector с параметрами из config.toml.

    Ожидаемая секция:
        [agent.loop_detector]
        pattern_limit = 2
        empty_limit = 3
        empty_min_length = 20
        semantic_threshold = 0.85
        semantic_window = 3
    """
    ldc = cfg.get("agent.loop_detector", {}) or {}
    return LoopDetector(
        pattern_limit=int(ldc.get("pattern_limit",
                                  LoopDetectorConfig.PATTERN_LIMIT)),
        empty_limit=int(ldc.get("empty_limit",
                                LoopDetectorConfig.EMPTY_LIMIT)),
        empty_min_length=int(ldc.get("empty_min_length",
                                     LoopDetectorConfig.EMPTY_MIN_LENGTH)),
        semantic_threshold=float(ldc.get("semantic_threshold",
                                         LoopDetectorConfig.SEMANTIC_THRESHOLD)),
        semantic_window=int(ldc.get("semantic_window",
                                    LoopDetectorConfig.SEMANTIC_WINDOW)),
        embed_fn=embed_fn,
    )
