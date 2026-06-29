"""SystemPromptBuilder — разбивает system-prompt на static prefix + dynamic suffix.

Принцип (вдохновлён Lia-v2, адаптирован под Aionet):
  * **Static prefix** (~600-800 токенов): личность, правила ответа, список
    инструментов. НЕ меняется между запросами. Ollama кэширует KV-prefix,
    повторные вызовы в 3-5× быстрее.
  * **Dynamic suffix**: текущая сложность задачи, контекст из памяти, факты,
    эмоции (Sprint 2), disagreement-уровень (Sprint 2), RL-action (Sprint 5).

Контракт:
  * Если в config.toml задан `[llm].system_prompt_static` — он используется
    как static prefix.
  * Dynamic suffix собирается из переданных параметров: complexity_assessment,
    memory_context, episode_facts, и т.д. Любое поле опционально.
  * Если static_prefix не задан — fallback на единый `system_prompt_plan`/
    `system_prompt_instruct` из config.toml (legacy-режим, обратная совместимость).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from common.config import Config
from common.logging import get_logger

log = get_logger(__name__)


@dataclass
class DynamicContext:
    """Динамический контекст для system-prompt — меняется между запросами.

    Любое поле может быть None/пустым — оно просто не попадёт в suffix.
    """
    complexity_description: str | None = None     # из TaskComplexityClassifier
    complexity_level: str | None = None           # "trivial"|"simple"|...
    memory_context: str | None = None             # релевантные фрагменты из памяти
    episode_facts: str | None = None              # факты текущего эпизода
    user_profile: str | None = None               # глобальные факты о пользователе
    open_tasks: str | None = None                 # незавершённые задачи
    recent_assistant_messages: str | None = None  # последние N ответов агента
    # Зарезервировано под Sprint 2:
    emotion_text: str | None = None               # из EmotionEngine
    disagreement_level: str | None = None         # из DisagreementAssessor
    disagreement_reason: str | None = None
    rl_action_instruction: str | None = None      # из RL-sidecar (Sprint 5)
    extra: dict[str, str] = field(default_factory=dict)


class SystemPromptBuilder:
    """Собирает static_prefix и dynamic_suffix для LLMCall.

    Используется AgentRuntime'ом перед каждым вызовом LLM-engine.
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        llm = cfg.llm
        # Явный static-prefix из конфига — приоритет.
        self._static_prefix: str = llm.get("system_prompt_static", "") or ""
        # Fallback: если static не задан — используем единый plan-prompt.
        self._fallback_prompt: str = llm.get("system_prompt_plan", "") or ""
        self._instruct_prompt: str = llm.get("system_prompt_instruct", "") or ""

    @property
    def has_explicit_static(self) -> bool:
        """True, если в конфиге задан явный static prefix (KV-cache режим активен)."""
        return bool(self._static_prefix.strip())

    def build(self, ctx: DynamicContext, *, instruct_mode: bool = False
              ) -> tuple[str | None, str | None]:
        """Возвращает (static_prefix, dynamic_suffix) для LLMCall.

        Если static_prefix не задан — возвращает (None, None), и AgentRuntime
        должен использовать legacy-режим (единый system_prompt). Это сохраняет
        обратную совместимость: существующие конфиги без system_prompt_static
        продолжают работать как раньше.
        """
        if not self.has_explicit_static:
            log.debug("no static_prefix in config; using legacy system_prompt")
            return (None, None)

        dynamic = self._build_dynamic_suffix(ctx)
        return (self._static_prefix, dynamic if dynamic else None)

    def build_legacy(self, *, instruct_mode: bool = False) -> str:
        """Legacy-режим: единый system_prompt без split. Для обратной совместимости."""
        return self._instruct_prompt if instruct_mode else self._fallback_prompt

    def _build_dynamic_suffix(self, ctx: DynamicContext) -> str:
        """Собирает dynamic suffix из всех непустых полей контекста."""
        parts: list[str] = []

        # Сложность задачи — влияет на тон/длину ответа
        if ctx.complexity_level:
            parts.append(
                f"СЛОЖНОСТЬ ЗАДАЧИ: {ctx.complexity_level}"
                + (f" ({ctx.complexity_description})" if ctx.complexity_description else "")
                + ". Адаптируй глубину ответа под сложность."
            )

        # Глобальный профиль пользователя (выживает между чатами)
        if ctx.user_profile:
            parts.append(f"ПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ:\n{ctx.user_profile}")

        # Факты текущего эпизода
        if ctx.episode_facts:
            parts.append(f"КОНТЕКСТ ЧАТА:\n{ctx.episode_facts}")

        # Релевантные воспоминания из памяти
        if ctx.memory_context:
            parts.append(f"РЕЛЕВАНТНЫЕ ВОСПОМИНАНИЯ:\n{ctx.memory_context}")

        # Незавершённые задачи
        if ctx.open_tasks:
            parts.append(f"ОТКРЫТЫЕ ЗАДАЧИ:\n{ctx.open_tasks}")

        # Последние ответы агента — для непрерывности диалога
        if ctx.recent_assistant_messages:
            parts.append(f"МОИ ПОСЛЕДНИЕ ОТВЕТЫ:\n{ctx.recent_assistant_messages}")

        # Sprint 2 зарезервировано: эмоции, disagreement, RL-action
        if ctx.emotion_text:
            parts.append(f"ТЕКУЩЕЕ НАСТРОЕНИЕ: {ctx.emotion_text}")
        if ctx.disagreement_level and ctx.disagreement_level != "execute":
            parts.append(
                f"УРОВЕНЬ НЕСОГЛАСИЯ: {ctx.disagreement_level}"
                + (f" — {ctx.disagreement_reason}" if ctx.disagreement_reason else "")
            )
        if ctx.rl_action_instruction:
            parts.append(f"СТИЛЬ ОТВЕТА (RL): {ctx.rl_action_instruction}")

        # Любые дополнительные поля из extra
        for key, value in ctx.extra.items():
            if value:
                parts.append(f"{key.upper()}:\n{value}")

        return "\n\n".join(parts)
