"""Общий базовый класс для всех MCP-серверов инструментов.

Использует FastMCP из официального MCP Python SDK — даёт декоратор @mcp.tool()
для регистрации функций. stdio-транспорт под капотом.

Каждый инструмент наследуется от BaseToolServer и регистрирует свои методы
через декоратор @self.mcp.tool() в методе _register_tools().
"""
from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP

from common.logging import get_logger

log = get_logger(__name__)


class BaseToolServer:
    """Базовый класс stdio-MCP-сервера инструмента.

    Подкласс должен:
      1) Переопределить `name` (имя сервиса).
      2) Зарегистрировать tool-функции через декоратор @self.mcp.tool()
         в методе `_register_tools()`.
    """

    name: str = "base"

    def __init__(self):
        self.mcp = FastMCP(self.name)
        self._register_tools()

    def _register_tools(self) -> None:
        """Переопределяется в подклассе для регистрации tool-функций."""
        raise NotImplementedError

    async def run(self) -> None:
        # FastMCP.run_stdio_async() может выбрасывать BrokenPipeError
        # когда родительский процесс закрывает stdin/stdout. Это нормально
        # при shutdown — логируем как info, не как error.
        try:
            await self.mcp.run_stdio_async()
        except (BrokenPipeError, ConnectionResetError):
            log.info("MCP server '%s' stdio pipe closed", self.name)
        except Exception as e:
            # anyio оборачивает в ExceptionGroup — раскручиваем
            eg = getattr(e, "exceptions", None)
            if eg and all(isinstance(sub, (BrokenPipeError, ConnectionResetError))
                         for sub in eg):
                log.info("MCP server '%s' stdio pipe closed (exception group)", self.name)
            else:
                raise


def _ok(**kwargs) -> str:
    """Сериализует успешный результат в JSON-строку."""
    return json.dumps({"ok": True, **kwargs}, ensure_ascii=False)


def _err(message: str, **extra) -> str:
    return json.dumps({"ok": False, "error": message, **extra}, ensure_ascii=False)


def _parse_args(raw: str | dict | None) -> dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return {}


def run_stdio_server(server: BaseToolServer) -> None:
    """Синхронная точка входа для запуска stdio-MCP-сервера."""
    log.info("starting MCP server '%s' on stdio", server.name)
    try:
        asyncio.run(server.run())
    except KeyboardInterrupt:
        log.info("MCP server '%s' interrupted", server.name)
    except Exception:
        log.exception("MCP server '%s' crashed", server.name)
        sys.exit(1)
