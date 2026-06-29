"""Брокер MCP↔ZeroMQ.

Запускает каждый MCP-сервер из config.tools.servers как отдельный stdio-подпроцесс,
держит с ним постоянную сессию через MCP SDK и проксирует входящие ToolCallMessage
от агента в нужный сервер.

Это устраняет необходимость в Ollama-MCP Bridge / MCPHost — мы реализуем
собственный лёгкий брокер.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
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
    """Один поднятый stdio-MCP-сервер с ленивой инициализацией сессии."""

    def __init__(self, name: str, command: str, args: list[str],
                 env: dict[str, str] | None = None):
        self.name = name
        self.command = command
        self.args = args
        self.env = env or {}
        self._session: ClientSession | None = None
        self._ctx_stack: Any = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        if self._session is not None:
            return
        params = StdioServerParameters(
            command=self.command, args=self.args,
            env={**os.environ, **self.env},
        )
        # stdio_client — async context manager; открываем его вручную.
        self._ctx_stack = stdio_client(params)
        read, write = await self._ctx_stack.__aenter__()
        self._session = ClientSession(read, write)
        await self._session.__aenter__()
        await self._session.initialize()
        log.info("MCP server '%s' started: %s %s",
                 self.name, self.command, " ".join(self.args))

    async def list_tools(self) -> list[ToolSchema]:
        await self.start()
        res = await self._session.list_tools()
        out: list[ToolSchema] = []
        for t in res.tools:
            # parameters — JSON-Schema в виде dict; сериализуем в строку.
            schema_dict = t.inputSchema if isinstance(t.inputSchema, dict) \
                else {"type": "object", "properties": {}}
            out.append(ToolSchema(
                name=f"{self.name}/{t.name}",
                description=t.description or "",
                parameters_json=json.dumps(schema_dict, ensure_ascii=False),
            ))
        return out

    async def call_tool(self, tool_short: str, arguments: dict[str, Any]) -> ToolExecutionResult:
        await self.start()
        try:
            res = await self._session.call_tool(tool_short, arguments)
        except Exception as e:
            return ToolExecutionResult(ok=False, output=None, error=str(e))
        # res.content — список TextContent / ImageContent / ...
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
            ok=not res.isError, output=parsed if parsed is not None else out_str,
            error="" if not res.isError else out_str,
        )

    async def stop(self) -> None:
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


class MCPBroker(ToolRunner):
    """Агрегирует все MCP-серверы. Вызовы от агента приходят через ZeroMQ REP."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._clients: dict[str, StdioMCPClient] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._tools_cache: list[ToolSchema] | None = None

    # ------- lifecycle -------
    def start(self) -> None:
        # Запускаем event-loop в фоне; ZeroMQ-handler будет планировать в него.
        self._loop = asyncio.new_event_loop()
        # Поднимаем все серверы синхронно (через run_until_complete).
        for srv in self.cfg.tool_servers:
            env = {f"AIONET_{k.upper()}": str(v) for k, v in srv.items()
                   if isinstance(v, (str, int, bool, list))}
            client = StdioMCPClient(
                name=srv["name"],
                command=srv["command"],
                args=list(srv.get("args", [])),
                env=env,
            )
            try:
                self._loop.run_until_complete(client.start())
                self._clients[srv["name"]] = client
            except Exception:
                log.exception("failed to start MCP server '%s'", srv["name"])

    def shutdown(self) -> None:
        if self._loop is None:
            return
        for c in self._clients.values():
            try:
                self._loop.run_until_complete(c.stop())
            except Exception:
                pass
        self._loop.close()
        self._loop = None

    # ------- ToolRunner impl -------
    def list_tools(self) -> list[ToolSchema]:
        if self._tools_cache is not None:
            return self._tools_cache
        out: list[ToolSchema] = []
        for c in self._clients.values():
            try:
                tools = self._loop.run_until_complete(c.list_tools())
                out.extend(tools)
            except Exception:
                log.exception("list_tools failed for '%s'", c.name)
        self._tools_cache = out
        return out

    def call(self, *, tool_name: str, arguments, timeout_ms: int = 30000) -> ToolExecutionResult:
        # tool_name имеет вид "<server>/<tool>" или "<server>/run"
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
        # Здесь же поддерживаем «ad-hoc» режим: если short=="run", берём
        # args["command"] как имя MCP-tool'а, а args["args"] как список.
        if short == "run":
            real_tool = args_dict.get("command")
            if not real_tool:
                return ToolExecutionResult(ok=False, output=None,
                                            error="missing 'command' in arguments")
            real_args = args_dict.get("args", [])
            # Преобразуем list[str] → dict, если MCP-tool ожидает kwargs.
            # Это упрощение: для более точной маршрутизации агент должен
            # сам вызывать "<server>/<tool>" напрямую.
            args_dict = {"command": real_tool, "args": real_args}
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
            import json as _json
            try:
                args = _json.loads(payload.arguments_json) if payload.arguments_json else {}
            except _json.JSONDecodeError as e:
                res = ToolExecutionResult(ok=False, output=None,
                                            error=f"invalid arguments: {e}")
                return build_payload(PayloadType.TOOL_RESULT,
                                     ok=False, error=str(e),
                                     duration_ms=0)
            t0 = __import__("time").time()
            result = broker.call(
                tool_name=payload.tool_name,
                arguments=args,
                timeout_ms=payload.timeout_ms or cfg.tools.get("default_timeout_ms", 30000),
            )
            dt = int((__import__("time").time() - t0) * 1000)
            try:
                out_str = _json.dumps(result.output, ensure_ascii=False) \
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
    log.info("MCP Broker listening at %s, %d servers registered",
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
