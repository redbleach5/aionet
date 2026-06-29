"""MCP-сервер: обёртка над WinGet (Windows Package Manager).

На Windows вызывает реальный `winget.exe`. На Linux/macOS — diagnostic stub,
возвращающий список предустановленных пакетов (для dev-тестирования интерфейса).
"""
from __future__ import annotations

import asyncio
import json
import os
import platform
import shutil
import subprocess

from . import _base


class WingetServer(_base.BaseToolServer):
    name = "winget"

    def _register_tools(self) -> None:
        from common.logging import get_logger
        log = get_logger("tools.winget")

        is_windows = platform.system() == "Windows"
        winget_exe = shutil.which("winget") if is_windows else None

        @self.mcp.tool()
        async def winget_search(query: str) -> str:
            """Найти пакет в репозитории WinGet."""
            if not winget_exe:
                return _base._ok(
                    mock=True,
                    message="winget не установлен (не Windows). Возвращаем mock-результат.",
                    results=[{"name": query, "id": f"mock.{query}",
                              "version": "0.0.0", "source": "mock"}],
                )
            try:
                proc = await asyncio.create_subprocess_exec(
                    winget_exe, "search", query, "--accept-source-agreements",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                out, err = await proc.communicate()
                return _base._ok(
                    exit_code=proc.returncode,
                    stdout=out.decode("utf-8", "replace"),
                    stderr=err.decode("utf-8", "replace"),
                )
            except Exception as e:
                return _base._err(str(e))

        @self.mcp.tool()
        async def winget_install(package_id: str,
                                 scope: str = "user") -> str:
            """Установить пакет по его id (например, 'Git.Git')."""
            if not winget_exe:
                return _base._err(
                    "winget не установлен (не Windows). Установите WinGet "
                    "или используйте эквивалент для вашей платформы."
                )
            try:
                args = [winget_exe, "install", package_id,
                        "--accept-package-agreements",
                        "--accept-source-agreements",
                        "--silent",
                        f"--scope={scope}"]
                proc = await asyncio.create_subprocess_exec(
                    *args,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                out, err = await asyncio.wait_for(proc.communicate(), timeout=300)
                return _base._ok(
                    exit_code=proc.returncode,
                    stdout=out.decode("utf-8", "replace"),
                    stderr=err.decode("utf-8", "replace"),
                )
            except asyncio.TimeoutError:
                return _base._err("winget install timed out after 300s")
            except Exception as e:
                return _base._err(str(e))

        @self.mcp.tool()
        async def winget_list() -> str:
            """Список установленных пакетов."""
            if not winget_exe:
                # Mock-режим: возвращаем установленные Python-пакеты
                try:
                    proc = await asyncio.create_subprocess_exec(
                        "pip", "list", "--format=json",
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    out, _ = await proc.communicate()
                    pkgs = json.loads(out.decode("utf-8", "replace"))
                    return _base._ok(
                        mock=True,
                        message="winget недоступен — возвращаем pip list",
                        packages=[{"name": p["name"], "version": p["version"]}
                                  for p in pkgs],
                    )
                except Exception as e:
                    return _base._err(str(e))
            try:
                proc = await asyncio.create_subprocess_exec(
                    winget_exe, "list",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                out, err = await proc.communicate()
                return _base._ok(
                    exit_code=proc.returncode,
                    stdout=out.decode("utf-8", "replace"),
                    stderr=err.decode("utf-8", "replace"),
                )
            except Exception as e:
                return _base._err(str(e))


def main():
    _base.run_stdio_server(WingetServer())


if __name__ == "__main__":
    main()
