"""HomunculusMCPBackend — интеграция с Desktop Homunculus через MCP.

Desktop Homunculus — это Rust/Bevy-приложение с MCP-сервером для управления
3D-аватаром. На момент написания публичного релиза нет, поэтому этот класс
реализован как заглушка с готовым контрактом.

Когда Homunculus станет доступен:
  1) Запустить его как отдельный процесс (Rust binary) или MCP-сервер
  2) В config.toml: [avatar].backend = "homunculus"
  3) Указать путь к MCP-серверу: [avatar].homunculus_mcp_command = ["./homunculus_mcp"]
  4) Этот класс поднимет stdio-MCP-сессию и будет проксировать вызовы

Архитектурно HomunculusMCPBackend — это sibling WebSocketBridge: оба реализуют
AvatarBackend, но первый говорит по MCP (stdio), второй — по WebSocket.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
from typing import Any

from common.config import Config
from common.interfaces import AvatarBackend
from common.logging import get_logger

log = get_logger(__name__)


class HomunculusMCPBackend(AvatarBackend):
    """AvatarBackend поверх MCP-сервера Desktop Homunculus.

    Контракт MCP-инструментов Homunculus (ожидаемый):
      * homunculus.speak(text: str, emotion: str = "") → ack
      * homunculus.set_emotion(emotion: str, intensity: float = 1.0) → ack
      * homunculus.gesture(name: str) → ack
      * homunculus.look_at(x: float, y: float, z: float) → ack
      * homunculus.idle() → ack
      * homunculus.get_state() → {emotion, is_speaking, ...}

    Реализация использует mcp.ClientSession (как tools/broker.py), но в одном
    экземпляре — один Homunculus, не нужен broker.

    Все методы speak/emote/gesture/etc. — синхронные обёртки над async MCP-
    вызовами. Внутри крутится отдельный event-loop в daemon-потоке.
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        acfg = cfg.avatar
        # Путь к MCP-серверу Homunculus (команда + аргументы)
        # Например: ["/opt/homunculus/bin/homunculus_mcp", "--port", "0"]
        self._command: list[str] = list(acfg.get(
            "homunculus_mcp_command", ["./homunculus_mcp"]))
        self._env: dict[str, str] = dict(acfg.get("homunculus_env", {}))
        self._session = None
        self._ctx_stack = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None
        self._started = False
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        """Поднимает stdio-MCP-сессию с Homunculus в отдельном event-loop."""
        if self._started:
            return
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError as e:
            log.error("mcp SDK not available for HomunculusMCPBackend: %s", e)
            raise

        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._run_loop, name="homunculus-mcp-loop", daemon=True,
        )
        self._loop_thread.start()

        # Инициализируем сессию в event-loop
        fut = asyncio.run_coroutine_threadsafe(self._init_session(), self._loop)
        try:
            fut.result(timeout=10)
            self._started = True
            log.info("HomunculusMCPBackend started: %s", " ".join(self._command))
        except Exception as e:
            log.error("HomunculusMCPBackend init failed: %s (is Homunculus installed?)", e)
            # Fallback: backend остаётся в неактивном состоянии; вызовы будут логироваться
            self._started = False

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    async def _init_session(self) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        params = StdioServerParameters(
            command=self._command[0],
            args=self._command[1:] if len(self._command) > 1 else [],
            env={**os.environ, **self._env},
        )
        self._ctx_stack = stdio_client(params)
        read, write = await self._ctx_stack.__aenter__()
        self._session = ClientSession(read, write)
        await self._session.__aenter__()
        await self._session.initialize()
        log.info("Homunculus MCP session initialized")

    def stop(self) -> None:
        if not self._started:
            return
        if self._session is not None:
            try:
                fut = asyncio.run_coroutine_threadsafe(
                    self._session.__aexit__(None, None, None), self._loop)
                fut.result(timeout=3)
            except Exception:
                pass
            self._session = None
        if self._ctx_stack is not None:
            try:
                fut = asyncio.run_coroutine_threadsafe(
                    self._ctx_stack.__aexit__(None, None, None), self._loop)
                fut.result(timeout=3)
            except Exception:
                pass
            self._ctx_stack = None
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
            if self._loop_thread:
                self._loop_thread.join(timeout=3)
        self._started = False
        log.info("HomunculusMCPBackend stopped")

    # ------------------------------------------------------------------
    # AvatarBackend impl
    # ------------------------------------------------------------------
    def _call_tool(self, tool_name: str, arguments: dict[str, Any]) -> None:
        """Синхронная обёртка над async MCP call_tool. Логирует, не падает."""
        if not self._started or self._session is None:
            log.warning("Homunculus not started; skipping %s(%s)",
                        tool_name, arguments)
            return
        try:
            fut = asyncio.run_coroutine_threadsafe(
                self._session.call_tool(tool_name, arguments),
                self._loop,
            )
            res = fut.result(timeout=5.0)
            # res.content — список TextContent
            for c in res.content:
                text = getattr(c, "text", None)
                if text:
                    log.debug("Homunculus %s → %s", tool_name, text[:80])
        except Exception as e:
            log.warning("Homunculus %s failed: %s", tool_name, e)

    def speak(self, text: str) -> None:
        self._call_tool("speak", {"text": text})

    def emote(self, emotion: str) -> None:
        self._call_tool("set_emotion", {"emotion": emotion, "intensity": 1.0})

    def gesture(self, name: str) -> None:
        self._call_tool("gesture", {"name": name})

    def look_at(self, x: float, y: float, z: float) -> None:
        self._call_tool("look_at", {"x": x, "y": y, "z": z})

    def idle(self) -> None:
        self._call_tool("idle", {})


# =============================================================================
# Фабрика
# =============================================================================
def make_avatar_backend(cfg: Config) -> AvatarBackend:
    """Создаёт AvatarBackend по config.avatar.backend.

    Поддерживаемые значения:
      * "html5_threejs" (по умолчанию) — WebSocketBridge → Three.js в Tauri
      * "homunculus" — HomunculusMCPBackend (когда станет доступен)
    """
    backend = cfg.avatar.get("backend", "html5_threejs")
    if backend == "homunculus":
        try:
            b = HomunculusMCPBackend(cfg)
            b.start()
            return b
        except Exception as e:
            log.error("HomunculusMCPBackend init failed: %s; falling back to WS bridge", e)
            # Fallback на WS-bridge
            from avatar.ws_bridge import AvatarBridge
            return _WSBridgeAdapter(AvatarBridge(cfg))
    elif backend == "html5_threejs":
        from avatar.ws_bridge import AvatarBridge
        return _WSBridgeAdapter(AvatarBridge(cfg))
    raise ValueError(f"unknown avatar backend: {backend}")


class _WSBridgeAdapter(AvatarBackend):
    """Адаптер: AvatarBridge (async PUB/SUB) → синхронный AvatarBackend.

    WebSocket-мост уже получает команды через ZMQ PUB от agent_core.
    Этот адаптер — для случаев, когда код хочет вызвать аватар напрямую
    через AvatarBackend (например, в тестах).
    """

    def __init__(self, bridge):
        self._bridge = bridge

    def speak(self, text: str) -> None:
        from common.proto import build_payload, PayloadType
        self._bridge._evt_pub.publish(
            target="avatar",
            payload_type=PayloadType.AVATAR_CMD,
            payload=build_payload(PayloadType.AVATAR_CMD, action=0, text=text),
        )

    def emote(self, emotion: str) -> None:
        from common.proto import build_payload, PayloadType
        self._bridge._evt_pub.publish(
            target="avatar",
            payload_type=PayloadType.AVATAR_CMD,
            payload=build_payload(PayloadType.AVATAR_CMD, action=1, emotion=emotion),
        )

    def gesture(self, name: str) -> None:
        from common.proto import build_payload, PayloadType
        self._bridge._evt_pub.publish(
            target="avatar",
            payload_type=PayloadType.AVATAR_CMD,
            payload=build_payload(PayloadType.AVATAR_CMD, action=2, gesture=name),
        )

    def look_at(self, x: float, y: float, z: float) -> None:
        from common.proto import build_payload, PayloadType
        self._bridge._evt_pub.publish(
            target="avatar",
            payload_type=PayloadType.AVATAR_CMD,
            payload=build_payload(PayloadType.AVATAR_CMD, action=4, x=x, y=y, z=z),
        )

    def idle(self) -> None:
        from common.proto import build_payload, PayloadType
        self._bridge._evt_pub.publish(
            target="avatar",
            payload_type=PayloadType.AVATAR_CMD,
            payload=build_payload(PayloadType.AVATAR_CMD, action=3),
        )
