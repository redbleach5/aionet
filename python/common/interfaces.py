"""Абстрактные интерфейсы — точки расширения системы.

Каждая «заменяемая» подсистема описывается здесь ABC-классом. Реальные имплементации
(LLMClient, MemoryStore, Sandbox, AvatarBackend, ToolRunner) наследуются от них
и регистрируются в фабриках по строковому ключу из config.toml.

Это позволяет заменять компоненты (Ollama→llama.cpp, FAISS→Chroma,
Docker→MXC, Three.js→Homunculus) без переписывания остального кода.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping


# =============================================================================
# LLM
# =============================================================================
@dataclass
class ChatMessage:
    role: str          # "user" | "assistant" | "system" | "tool"
    content: str
    tool_call_id: str | None = None
    name: str | None = None


@dataclass
class ToolSchema:
    name: str
    description: str
    parameters_json: str  # JSON-Schema


@dataclass
class ToolCall:
    id: str
    name: str
    arguments_json: str


@dataclass
class LLMResult:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model_used: str = ""


class LLMClient(abc.ABC):
    """Абстракция над локальным LLM (Ollama / llama-cpp / что-то другое)."""

    @abc.abstractmethod
    def list_available_models(self) -> list[str]:
        """Возвращает модели, реально установленные в рантайме."""

    @abc.abstractmethod
    def call(
        self,
        *,
        model: str,
        system_prompt: str,
        messages: list[ChatMessage],
        tools: list[ToolSchema] | None = None,
        temperature: float = 0.3,
        max_tokens: int = 2048,
        timeout_s: int = 120,
    ) -> LLMResult:
        """Синхронный вызов. При наличии tools — может вернуть tool_calls."""


# =============================================================================
# Memory
# =============================================================================
@dataclass
class MemoryRecord:
    id: str
    text: str
    score: float = 0.0
    importance: float = 1.0
    created_at: int = 0       # unix-millis
    last_accessed: int = 0
    access_count: int = 0
    metadata: dict[str, str] = field(default_factory=dict)


class MemoryStore(abc.ABC):
    """Долговременная память агента с поддержкой многоканального извлечения."""

    @abc.abstractmethod
    def store(self, *, session_id: str, text: str,
              metadata: Mapping[str, str] | None = None,
              importance: float = 1.0) -> str:
        """Сохраняет фрагмент. Возвращает id записи."""

    @abc.abstractmethod
    def retrieve(self, *, session_id: str | None, text: str,
                 top_k: int = 5,
                 channels: Iterable[str] | None = None) -> list[MemoryRecord]:
        """Многоканальное извлечение. channels=None → все каналы."""

    @abc.abstractmethod
    def forget(self, *, session_id: str | None = None) -> int:
        """Принудительная сборка мусора по кривой забывания. Возвращает кол-во удалённых."""

    @abc.abstractmethod
    def stats(self) -> dict[str, str]:
        """Статистика хранилища для /health."""


# =============================================================================
# Sandbox
# =============================================================================
@dataclass
class SandboxResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    ok: bool


class Sandbox(abc.ABC):
    """Изоляция вызова инструментов. Реализации: DockerSandbox, MXCSandbox, NoneSandbox."""

    @abc.abstractmethod
    def run(self, *,
            command: list[str],
            workspace: str | None = None,
            network: bool = False,
            timeout_s: int = 30,
            env: Mapping[str, str] | None = None) -> SandboxResult:
        """Запускает команду в изолированном окружении."""


# =============================================================================
# Tools / MCP
# =============================================================================
@dataclass
class ToolExecutionResult:
    ok: bool
    output: Any
    error: str = ""
    duration_ms: int = 0


class ToolRunner(abc.ABC):
    """Брокер инструментов. Под капотом — stdio-MCP-серверы."""

    @abc.abstractmethod
    def list_tools(self) -> list[ToolSchema]:
        """Агрегирует tool-listing со всех подключённых MCP-серверов."""

    @abc.abstractmethod
    def call(self, *, tool_name: str, arguments: Mapping[str, Any],
             timeout_ms: int = 30000) -> ToolExecutionResult:
        """Вызывает инструмент по имени. Имя вида '<server>/<tool>'."""

    @abc.abstractmethod
    def shutdown(self) -> None:
        """Корректно завершает все поднятые MCP-серверы."""


# =============================================================================
# Avatar
# =============================================================================
class AvatarBackend(abc.ABC):
    """Управление аватаром: речь, эмоции, жесты, взгляд."""

    @abc.abstractmethod
    def speak(self, text: str) -> None: ...

    @abc.abstractmethod
    def emote(self, emotion: str) -> None: ...

    @abc.abstractmethod
    def gesture(self, name: str) -> None: ...

    @abc.abstractmethod
    def look_at(self, x: float, y: float, z: float) -> None: ...

    @abc.abstractmethod
    def idle(self) -> None: ...
