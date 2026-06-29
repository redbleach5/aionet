"""TaskComplexityClassifier — классификатор сложности запроса.

5 уровней (вдохновлено Lia-v2, расширено под нашу палитру задач):

    trivial    — привет/спасибо/ок (1 LLM-call, max_tokens=512)
    simple     — простой вопрос (1 call, 1024 tokens)
    moderate   — умеренная задача (1-2 calls, 2048 tokens)
    complex    — анализ/рассуждение/рефакторинг (2-3 calls, 4096 tokens)
    research   — нужен поиск/документация (1-2 calls + web_search)

Классификация — чисто regex-эвристики, БЕЗ LLM-вызова. Дёшево, детерминированно,
используется:
  * CognitivePlanner'ом для выбора числа LLM-вызовов
  * AgentRuntime для динамической настройки max_iter / max_tokens
  * UI для индикатора сложности
  * логами для аналитики

Анти-паттерн (учтён из Lia-v2): не делаем классификацию по длине одной только —
длинное сообщение может быть тривиальным ("привет, как дела, я тут подумал
о том о сём..."). Сначала regex-паттерны, потом длина как fallback.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Iterable

from common.logging import get_logger

log = get_logger(__name__)


# =============================================================================
# Уровни сложности
# =============================================================================
class TaskComplexity(str, Enum):
    TRIVIAL = "trivial"
    SIMPLE = "simple"
    MODERATE = "moderate"
    COMPLEX = "complex"
    RESEARCH = "research"


# Числовые веса — для сравнения "что сложнее" при множественных сигналах.
COMPLEXITY_WEIGHT: dict[TaskComplexity, int] = {
    TaskComplexity.TRIVIAL:  0,
    TaskComplexity.SIMPLE:   1,
    TaskComplexity.MODERATE: 2,
    TaskComplexity.COMPLEX:  3,
    TaskComplexity.RESEARCH: 4,
}


COMPLEXITY_DESCRIPTIONS: dict[TaskComplexity, str] = {
    TaskComplexity.TRIVIAL:  "тривиальная (привет/спасибо)",
    TaskComplexity.SIMPLE:   "простой вопрос",
    TaskComplexity.MODERATE: "умеренная задача",
    TaskComplexity.COMPLEX:  "сложная задача (анализ/рассуждение)",
    TaskComplexity.RESEARCH: "исследовательская (нужен поиск)",
}


# =============================================================================
# Паттерны (Cyrillic-safe, case-insensitive)
# =============================================================================
# Trivial — приветствия, благодарности, подтверждения
TRIVIAL_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"^(привет|здравствуй|здравствуйте|хай|hi|hello|приветик|йо|здорово)\b", re.I),
    re.compile(r"^(пока|до свидания|bye|goodbye|увидимся|до встречи|спокойной ночи)\b", re.I),
    re.compile(r"^(спасибо|благодарю|thanks|thank you|спс|пасиб|благодарствую)\b", re.I),
    re.compile(r"^(ок|окей|хорошо|ладно|да|нет|угу|ага|конечно|верно|точно)\b", re.I),
    re.compile(r"^(как дела|как ты|что делаешь|как настроение|как жизнь|ты тут)\b", re.I),
)

# Complex — многошаговое рассуждение, анализ, доказательство, сравнение
COMPLEX_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"\b(докажи|выведи|обоснуй|проанализируй|сравни|оцени|рассмотри|разбери)\b", re.I),
    re.compile(r"\b(архитектур|проектир|стратеги|план реализации|пошаговый план|roadmap)\b", re.I),
    re.compile(r"\b(почему|зачем|как устроен|как работает|в чём разница|отчего)\b", re.I),
    re.compile(r"\b(рефакторинг|оптимизируй|найди ошибку|debug|дебаг|почини)\b", re.I),
    re.compile(r"\b(переведи|реши|вычисли|рассчитай)\b.*\b(уравнени|задач|формул|интеграл|производн|матриц)", re.I),
    re.compile(r"\b(плюсы и минусы|преимущества и недостатки|pros and cons)\b", re.I),
    re.compile(r"\b(Trade-?off|tradeoffs|компромисс)\b", re.I),
)

# Research — нужен внешний поиск (web_search, docs, версии)
RESEARCH_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"\b(найди информацию|поищи|загугли|что нового|актуальн|последн)\b", re.I),
    re.compile(r"\b(версия|release|changelog|обновлен|что вышло)\b", re.I),
    re.compile(r"\b(документаци|docs|documentation|spec|спецификаци|api reference)\b", re.I),
    re.compile(r"\b(статистик|исследовани|study|paper|статья|arxiv|benchmark)\b", re.I),
    re.compile(r"\b(сравни.*между собой|какой лучше|что выбрать|рейтинг)\b", re.I),
)

# Moderate — заметные, но не сложные: письмо, код, конфиг, отчёт
MODERATE_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"\b(напиши|создай|сделай|сгенерируй)\b.*\b(письмо|сообщение|текст|статья|пост|комментарий)\b", re.I),
    re.compile(r"\b(напиши|реши|сделай)\b.*\b(функци|класс|скрипт|компонент|тест)\b", re.I),
    re.compile(r"\b(настрой|сконфигурируй|разверни)\b.*\b(docker|nginx|kubernetes|ci|cd)\b", re.I),
    re.compile(r"\b(составь|подготовь)\b.*\b(отчёт|план|регламент|чек-лист)\b", re.I),
)


# =============================================================================
# Классификатор
# =============================================================================
@dataclass
class ComplexityAssessment:
    level: TaskComplexity
    description: str
    matched_patterns: list[str]
    length: int
    has_question: bool

    def to_dict(self) -> dict:
        return {
            "level": self.level.value,
            "description": self.description,
            "matched_patterns": self.matched_patterns,
            "length": self.length,
            "has_question": self.has_question,
        }


def classify_complexity(message: str) -> ComplexityAssessment:
    """Классифицирует сложность пользовательского сообщения.

    Алгоритм:
      1. Проверяем TRIVIAL_PATTERNS — если совпало, выходим сразу.
      2. Проверяем RESEARCH_PATTERNS — research приоритетнее complex
         (если нужен поиск, это определяет выбор инструментов).
      3. Проверяем COMPLEX_PATTERNS.
      4. Проверяем MODERATE_PATTERNS.
      5. Fallback по длине/вопросительности.
    """
    text = (message or "").strip()
    lower = text.lower()
    has_question = "?" in text
    matched: list[str] = []

    # 1. Trivial — короткие приветствия/благодарности
    if text:
        for p in TRIVIAL_PATTERNS:
            if p.search(text):
                matched.append(p.pattern)
                return ComplexityAssessment(
                    level=TaskComplexity.TRIVIAL,
                    description=COMPLEXITY_DESCRIPTIONS[TaskComplexity.TRIVIAL],
                    matched_patterns=matched,
                    length=len(text),
                    has_question=has_question,
                )

    # 2. Research — нужен внешний поиск
    for p in RESEARCH_PATTERNS:
        if p.search(lower):
            matched.append(p.pattern)
            return ComplexityAssessment(
                level=TaskComplexity.RESEARCH,
                description=COMPLEXITY_DESCRIPTIONS[TaskComplexity.RESEARCH],
                matched_patterns=matched,
                length=len(text),
                has_question=has_question,
            )

    # 3. Complex — анализ/рассуждение/рефакторинг
    for p in COMPLEX_PATTERNS:
        if p.search(lower):
            matched.append(p.pattern)
            return ComplexityAssessment(
                level=TaskComplexity.COMPLEX,
                description=COMPLEXITY_DESCRIPTIONS[TaskComplexity.COMPLEX],
                matched_patterns=matched,
                length=len(text),
                has_question=has_question,
            )

    # 4. Moderate — код/письмо/конфиг
    for p in MODERATE_PATTERNS:
        if p.search(lower):
            matched.append(p.pattern)
            return ComplexityAssessment(
                level=TaskComplexity.MODERATE,
                description=COMPLEXITY_DESCRIPTIONS[TaskComplexity.MODERATE],
                matched_patterns=matched,
                length=len(text),
                has_question=has_question,
            )

    # 5. Fallback по длине
    if len(text) < 20:
        if has_question:
            return ComplexityAssessment(
                level=TaskComplexity.SIMPLE,
                description=COMPLEXITY_DESCRIPTIONS[TaskComplexity.SIMPLE],
                matched_patterns=[],
                length=len(text),
                has_question=True,
            )
        return ComplexityAssessment(
            level=TaskComplexity.TRIVIAL,
            description=COMPLEXITY_DESCRIPTIONS[TaskComplexity.TRIVIAL],
            matched_patterns=[],
            length=len(text),
            has_question=False,
        )

    if len(text) > 500:
        return ComplexityAssessment(
            level=TaskComplexity.MODERATE,
            description=COMPLEXITY_DESCRIPTIONS[TaskComplexity.MODERATE],
            matched_patterns=[],
            length=len(text),
            has_question=has_question,
        )

    if has_question:
        return ComplexityAssessment(
            level=TaskComplexity.SIMPLE,
            description=COMPLEXITY_DESCRIPTIONS[TaskComplexity.SIMPLE],
            matched_patterns=[],
            length=len(text),
            has_question=True,
        )

    return ComplexityAssessment(
        level=TaskComplexity.MODERATE,
        description=COMPLEXITY_DESCRIPTIONS[TaskComplexity.MODERATE],
        matched_patterns=[],
        length=len(text),
        has_question=has_question,
    )


# =============================================================================
# Cognitive parameters per complexity — preview для Sprint 2 (CognitivePlanner)
# =============================================================================
# Эти параметры — предварительные. Окончательный выбор будет делаться в
# CognitivePlanner как функция (tier × complexity × mode). Здесь — дефолты
# для случая "tier=standard, mode=auto", чтобы AgentRuntime уже сейчас
# мог динамически настраивать max_iter / max_tokens.
COMPLEXITY_DEFAULTS: dict[TaskComplexity, dict] = {
    TaskComplexity.TRIVIAL:  {"max_iter": 1, "max_tokens": 512,  "tools": False, "web_search": False},
    TaskComplexity.SIMPLE:   {"max_iter": 1, "max_tokens": 1024, "tools": False, "web_search": False},
    TaskComplexity.MODERATE: {"max_iter": 3, "max_tokens": 2048, "tools": True,  "web_search": False},
    TaskComplexity.COMPLEX:  {"max_iter": 5, "max_tokens": 4096, "tools": True,  "web_search": True},
    TaskComplexity.RESEARCH: {"max_iter": 4, "max_tokens": 4096, "tools": True,  "web_search": True},
}


def get_defaults(level: TaskComplexity) -> dict:
    return dict(COMPLEXITY_DEFAULTS.get(level, COMPLEXITY_DEFAULTS[TaskComplexity.MODERATE]))
