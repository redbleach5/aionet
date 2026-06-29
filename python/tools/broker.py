"""Брокер MCP↔ZeroMQ.

Запускает каждый MCP-сервер из config.tools.servers как отдельный stdio-подпроцесс,
держит с ним постоянную сессию через MCP SDK и проксирует входящие ToolCallMessage
от агента в нужный сервер.

Ключевые архитектурные решения:
  * stdio-MCP-сессии живут внутри постоянного asyncio event loop (отдельный поток)
  * Инициализация сессии — ленивая: первый call_tool/list_tools поднимает её
  * Падение одного MCP-сервера НЕ валит брокер — сессия помечается как failed,
    повторный вызов пытается пересоздать
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import uuid
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from common.config import Config, load_config
from common.interfaces import ToolExecutionResult, ToolRunner, ToolSchema
from common.logging import get_logger, trace_context
from common.proto import build_payload, PayloadType
from common.zmq_transport import ZMQServer

log = get_logger(__name__)


class StdioMCPClient:
    """Один поднятый stdio-MCP-сервер с ленивой инициализацией сессии.

    Сессия создаётся при первом list_tools()/call_tool() и держится до
    явного stop(). Если подпроцесс падает — сессия помечается broken,
    следующий вызов пытается её пересоздать.
    """

    def __init__(self, name: str, command: str, args: list[str],
                 env: dict[str, str] | None = None):
        self.name = name
        self.command = command
        self.args = args
        self.env = env or {}
        self._session: ClientSession | None = None
        self._ctx_stack: Any = None
        self._lock = asyncio.Lock()
        self._broken = False

    async def _ensure_session(self) -> bool:
        """Создаёт сессию, если её нет. Возвращает True если сессия готова."""
        if self._session is not None and not self._broken:
            return True
        async with self._lock:
            if self._session is not None and not self._broken:
                return True
            # Закрываем старую сломанную сессию
            if self._broken:
                await self._cleanup()
                self._broken = False
            try:
                params = StdioServerParameters(
                    command=self.command, args=self.args,
                    env={**os.environ, **self.env},
                )
                self._ctx_stack = stdio_client(params)
                read, write = await self._ctx_stack.__aenter__()
                self._session = ClientSession(read, write)
                await self._session.__aenter__()
                await self._session.initialize()
                log.info("MCP server '%s' session initialized", self.name)
                return True
            except Exception as e:
                log.error("MCP server '%s' init failed: %s", self.name, e)
                self._broken = True
                await self._cleanup()
                return False

    async def _cleanup(self) -> None:
        if self._session is not None:
            try:
                await self._session.__aexit__(None, None, None)
            except Exception:
                pass
            self._session = None
        if self._ctx_stack is not None:
            try:
                await self._ctx_stack.__aexit__(None, None, None)
            except Exception:
                pass
            self._ctx_stack = None

    async def list_tools(self) -> list[ToolSchema]:
        if not await self._ensure_session():
            return []
        try:
            res = await self._session.list_tools()
        except Exception as e:
            log.warning("MCP '%s' list_tools failed: %s — marking broken", self.name, e)
            self._broken = True
            return []
        out: list[ToolSchema] = []
        for t in res.tools:
            schema_dict = t.inputSchema if isinstance(t.inputSchema, dict) \
                else {"type": "object", "properties": {}}
            out.append(ToolSchema(
                name=f"{self.name}/{t.name}",
                description=t.description or "",
                parameters_json=json.dumps(schema_dict, ensure_ascii=False),
            ))
        return out

    async def call_tool(self, tool_short: str, arguments: dict[str, Any]) -> ToolExecutionResult:
        if not await self._ensure_session():
            return ToolExecutionResult(ok=False, output=None,
                                        error=f"MCP server '{self.name}' unavailable")
        try:
            res = await self._session.call_tool(tool_short, arguments)
        except Exception as e:
            log.warning("MCP '%s' call_tool '%s' failed: %s — marking broken",
                        self.name, tool_short, e)
            self._broken = True
            return ToolExecutionResult(ok=False, output=None, error=str(e))
        text_parts: list[str] = []
        for c in res.content:
            text = getattr(c, "text", None)
            if text is not None:
                text_parts.append(text)
        out_str = "\n".join(text_parts) if text_parts else ""
        try:
            parsed = json.loads(out_str) if out_str else None
        except json.JSONDecodeError:
            parsed = out_str
        return ToolExecutionResult(
            ok=not res.isError,
            output=parsed if parsed is not None else out_str,
            error="" if not res.isError else out_str,
        )

    async def stop(self) -> None:
        await self._cleanup()


class MCPBroker(ToolRunner):
    """Агрегирует все MCP-серверы. Вызовы от агента приходят через ZeroMQ REP."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._clients: dict[str, StdioMCPClient] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None
        self._tools_cache: list[ToolSchema] | None = None

    # ------- lifecycle -------
    def start(self) -> None:
        # Создаём event loop в отдельном потоке — он будет жить всё время брокера
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._run_loop, name="mcp-broker-loop", daemon=True,
        )
        self._loop_thread.start()

        # Регистрируем клиентов (БЕЗ инициализации — она ленивая)
        for srv in self.cfg.tool_servers:
            env = {f"AIONET_{k.upper()}": str(v) for k, v in srv.items()
                   if isinstance(v, (str, int, bool, list))}
            client = StdioMCPClient(
                name=srv["name"],
                command=srv["command"],
                args=list(srv.get("args", [])),
                env=env,
            )
            self._clients[srv["name"]] = client
            log.info("registered MCP client '%s' (lazy init)", srv["name"])

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def shutdown(self) -> None:
        if self._loop is None:
            return
        # Останавливаем всех клиентов в event-loop
        for c in list(self._clients.values()):
            try:
                fut = asyncio.run_coroutine_threadsafe(c.stop(), self._loop)
                fut.result(timeout=3)
            except Exception:
                pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        if self._loop_thread:
            self._loop_thread.join(timeout=3)
        self._loop = None
        self._loop_thread = None

    # ------- ToolRunner impl -------
    def list_tools(self) -> list[ToolSchema]:
        if self._tools_cache is not None:
            return self._tools_cache
        out: list[ToolSchema] = []
        for c in self._clients.values():
            try:
                fut = asyncio.run_coroutine_threadsafe(c.list_tools(), self._loop)
                tools = fut.result(timeout=10)
                out.extend(tools)
            except Exception as e:
                log.warning("list_tools failed for '%s': %s", c.name, e)
        self._tools_cache = out
        return out

    def call(self, *, tool_name: str, arguments, timeout_ms: int = 30000) -> ToolExecutionResult:
        if "/" not in tool_name:
            return ToolExecutionResult(ok=False, output=None,
                                        error=f"tool name must be '<server>/<tool>', got {tool_name!r}")
        server, short = tool_name.split("/", 1)
        client = self._clients.get(server)
        if client is None:
            return ToolExecutionResult(ok=False, output=None,
                                        error=f"unknown MCP server: {server}")
        args_dict: dict[str, Any]
        if isinstance(arguments, str):
            try:
                args_dict = json.loads(arguments) if arguments else {}
            except json.JSONDecodeError as e:
                return ToolExecutionResult(ok=False, output=None,
                                            error=f"invalid arguments JSON: {e}")
        else:
            args_dict = dict(arguments)
        # Если инструмент зарегистрирован как "<server>/run", агент передаёт
        # command/args — преобразуем в вызов соответствующего MCP-tool.
        if short == "run":
            real_tool = args_dict.get("command")
            if not real_tool:
                return ToolExecutionResult(ok=False, output=None,
                                            error="missing 'command' in arguments")
            args_dict = {"command": args_dict.get("command"),
                         "args": args_dict.get("args", [])}
            short = real_tool
        try:
            fut = asyncio.run_coroutine_threadsafe(
                client.call_tool(short, args_dict),
                self._loop,
            )
            return fut.result(timeout=timeout_ms / 1000.0)
        except Exception as e:
            return ToolExecutionResult(ok=False, output=None, error=str(e))


# =============================================================================
# ZeroMQ-обёртка
# =============================================================================
def main():
    cfg = load_config()
    broker = MCPBroker(cfg)
    broker.start()

    def handler(env, payload) -> bytes:
        with trace_context(env.trace_id, env.span_id):
            log.info("ToolCall tool=%s args=%s",
                     payload.tool_name, payload.arguments_json[:200])
            try:
                args = json.loads(payload.arguments_json) if payload.arguments_json else {}
            except json.JSONDecodeError as e:
                return build_payload(PayloadType.TOOL_RESULT,
                                     ok=False, error=str(e), duration_ms=0)
            t0 = time.time()
            result = broker.call(
                tool_name=payload.tool_name,
                arguments=args,
                timeout_ms=payload.timeout_ms or cfg.tools.get("default_timeout_ms", 30000),
            )
            dt = int((time.time() - t0) * 1000)
            try:
                out_str = json.dumps(result.output, ensure_ascii=False) \
                    if result.output is not None else ""
            except TypeError:
                out_str = str(result.output)
            return build_payload(
                PayloadType.TOOL_RESULT,
                ok=result.ok,
                output_json=out_str,
                error=result.error,
                duration_ms=dt,
            )

    server = ZMQServer(
        endpoint=cfg.zmq["tools_endpoint"],
        service_name="tools",
        handler=handler,
        rcvtimeo_ms=cfg.zmq.get("zmq_rcvtimeo_ms", 30000),
    )
    log.info("MCP Broker listening at %s, %d clients registered (lazy)",
             cfg.zmq["tools_endpoint"], len(broker._clients))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
        broker.shutdown()


if __name__ == "__main__":
    main()
