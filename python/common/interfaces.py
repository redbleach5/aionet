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


# =============================================================================
# Sandbox Policy — точка расширения для MXC/OpenShell
# =============================================================================
@dataclass
class SandboxPolicy:
    """Декларативное описание политик песочницы.

    Унифицирует конфигурацию для DockerSandbox / MXCSandbox / OpenShell.
    Реальные MXC/OpenShell принимают YAML/JSON-политики на уровне ядра;
    этот класс — их Python-представление. Метод Sandbox.apply_policy()
    конвертирует его в формат, понятный конкретной реализации.

    Поля:
      seccomp_profile: путь к seccomp JSON (Docker) или имя встроенной политики MXC
      apparmor_profile: имя AppArmor-профиля (Docker/Linux)
      yaml_policy: путь к YAML-файлу OpenShell-стиля (для MXCSandbox)
      network: список разрешённых хостов/портов; пустой = no-network
      fs_read: список разрешённых для чтения путей
      fs_write: список разрешённых для записи путей
      syscalls_allow: whitelist syscall'ов (для seccomp)
      syscalls_deny: blacklist syscall'ов
      capabilities: список Linux capabilities (CAP_NET_BIND_SERVICE и т.п.)
      env: переменные окружения, передаваемые в песочницу
      memory_limit_mb: лимит памяти
      cpu_quota_percent: лимит CPU (0..100)
      pid_limit: max процессов внутри песочницы
    """
    seccomp_profile: str | None = None
    apparmor_profile: str | None = None
    yaml_policy: str | None = None
    network: list[str] = field(default_factory=list)  # [] = no-network
    fs_read: list[str] = field(default_factory=list)
    fs_write: list[str] = field(default_factory=list)
    syscalls_allow: list[str] = field(default_factory=list)
    syscalls_deny: list[str] = field(default_factory=list)
    capabilities: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    memory_limit_mb: int = 512
    cpu_quota_percent: int = 50
    pid_limit: int = 64

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "SandboxPolicy":
        """Создаёт политику из dict (например, из YAML/JSON-конфига)."""
        return cls(
            seccomp_profile=d.get("seccomp_profile"),
            apparmor_profile=d.get("apparmor_profile"),
            yaml_policy=d.get("yaml_policy"),
            network=list(d.get("network", [])),
            fs_read=list(d.get("fs_read", [])),
            fs_write=list(d.get("fs_write", [])),
            syscalls_allow=list(d.get("syscalls_allow", [])),
            syscalls_deny=list(d.get("syscalls_deny", [])),
            capabilities=list(d.get("capabilities", [])),
            env=dict(d.get("env", {})),
            memory_limit_mb=int(d.get("memory_limit_mb", 512)),
            cpu_quota_percent=int(d.get("cpu_quota_percent", 50)),
            pid_limit=int(d.get("pid_limit", 64)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "seccomp_profile": self.seccomp_profile,
            "apparmor_profile": self.apparmor_profile,
            "yaml_policy": self.yaml_policy,
            "network": list(self.network),
            "fs_read": list(self.fs_read),
            "fs_write": list(self.fs_write),
            "syscalls_allow": list(self.syscalls_allow),
            "syscalls_deny": list(self.syscalls_deny),
            "capabilities": list(self.capabilities),
            "env": dict(self.env),
            "memory_limit_mb": self.memory_limit_mb,
            "cpu_quota_percent": self.cpu_quota_percent,
            "pid_limit": self.pid_limit,
        }


class Sandbox(abc.ABC):
    """Изоляция вызова инструментов.

    Реализации: DockerSandbox (текущая), MXCSandbox (заглушка под MS Execution
    Containers), NoneSandbox (dev без изоляции).

    Расширенный контракт (rev 2): поддержка декларативных политик через
    apply_policy(). Это позволяет передавать YAML-политики OpenShell-стиля
    в MXCSandbox, не меняя остальной код.
    """

    @abc.abstractmethod
    def run(self, *,
            command: list[str],
            workspace: str | None = None,
            network: bool = False,
            timeout_s: int = 30,
            env: Mapping[str, str] | None = None) -> SandboxResult:
        """Запускает команду в изолированном окружении.

        Если ранее был применён policy через apply_policy() — он имеет приоритет
        над параметрами по умолчанию, но явные аргументы (network, env) могут
        его переопределять для конкретного вызова.
        """

    def apply_policy(self, policy: SandboxPolicy | Mapping[str, Any]) -> None:
        """Применяет декларативную политику к песочнице.

        Реализация по умолчанию: сохраняет политику в self._policy.
        Конкретные реализации (DockerSandbox, MXCSandbox) переопределяют
        этот метод, чтобы сконвертировать политику в свой формат
        (Docker security-opt, MXC YAML, OpenShell policy file).

        Для NoneSandbox: политика игнорируется (dev-режим без изоляции).
        """
        if isinstance(policy, Mapping):
            policy = SandboxPolicy.from_dict(policy)
        self._policy = policy  # type: ignore[attr-defined]

    @property
    def policy(self) -> SandboxPolicy | None:
        """Текущая применённая политика (или None, если не применялась)."""
        return getattr(self, "_policy", None)


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
