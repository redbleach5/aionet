"""MCP-сервер: запуск shell-команд в песочнице.

Инструмент `shell_run`:
  command: list[str]   — команда с аргументами
  cwd:     str?        — рабочая директория (внутри /workspace)
  timeout: int?        — таймаут в секундах (по умолчанию 30)

Все вызовы идут через Sandbox (Docker+seccomp+AppArmor, непривилегированный
пользователь, read-only rootfs, no-network), если config.security.sandbox=true.
"""
from __future__ import annotations

import os
from typing import Any

from . import _base


class ShellServer(_base.BaseToolServer):
    name = "shell"

    def _register_tools(self) -> None:
        from common.config import load_config
        from common.logging import get_logger
        log = get_logger("tools.shell")

        cfg = load_config()

        @self.mcp.tool()
        async def shell_run(command: list[str], cwd: str = "",
                            timeout: int = 30) -> str:
            """Выполнить shell-команду в песочнице.

            Args:
                command: Команда и её аргументы, например ["ls", "-la"].
                cwd:     Рабочая директория (внутри /workspace).
                timeout: Таймаут в секундах.
            """
            if not isinstance(command, list) or not command:
                return _base._err("command must be non-empty list[str]")
            try:
                # Импорт Sandbox делаем здесь, чтобы MCP-сервер не падал
                # при загрузке, если common.agent_core недоступен.
                from agent_core.security import make_sandbox
                sandbox = make_sandbox(cfg)
                ws = os.path.join("/workspace", cwd) if cwd else "/workspace"
                res = sandbox.run(
                    command=command,
                    workspace=ws,
                    timeout_s=int(timeout),
                )
                return _base._ok(
                    exit_code=res.exit_code,
                    stdout=res.stdout,
                    stderr=res.stderr,
                    duration_ms=res.duration_ms,
                )
            except Exception as e:
                log.exception("shell_run failed")
                return _base._err(str(e))


def main():
    _base.run_stdio_server(ShellServer())


if __name__ == "__main__":
    main()
