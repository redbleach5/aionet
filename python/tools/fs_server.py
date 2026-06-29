"""MCP-сервер: файловые операции в разрешённых корнях.

Инструменты:
  * fs_read(path)        → содержимое файла
  * fs_write(path, content) → запись
  * fs_list(dir)         → список файлов
  * fs_stat(path)        → метаданные

Все пути проверяются на принадлежность к allowed_roots из config.toml.
"""
from __future__ import annotations

import os
from pathlib import Path

from . import _base


class FsServer(_base.BaseToolServer):
    name = "filesystem"

    def __init__(self):
        from common.config import load_config
        self.cfg = load_config()
        # allowed_roots — для сервера внутри контейнера это /workspace,
        # но на хосте — реальные пути.
        servers = {s["name"]: s for s in self.cfg.tool_servers}
        fs_cfg = servers.get("filesystem", {})
        self.allowed_roots = [
            os.path.abspath(r) for r in fs_cfg.get("allowed_roots", ["./workspace"])
        ]
        # Создаём корни, если их нет.
        for r in self.allowed_roots:
            Path(r).mkdir(parents=True, exist_ok=True)
        super().__init__()

    def _safe(self, path: str) -> Path | None:
        """Возвращает Path, если path внутри allowed_roots, иначе None."""
        try:
            p = Path(path).resolve()
        except Exception:
            return None
        for root in self.allowed_roots:
            try:
                p.relative_to(root)
                return p
            except ValueError:
                continue
        return None

    def _register_tools(self) -> None:
        from common.logging import get_logger
        log = get_logger("tools.fs")

        @self.mcp.tool()
        async def fs_read(path: str) -> str:
            """Прочитать файл как UTF-8 текст."""
            p = self._safe(path)
            if p is None or not p.is_file():
                return _base._err("path not allowed or not a file")
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
                return _base._ok(content=text, size=p.stat().st_size)
            except Exception as e:
                return _base._err(str(e))

        @self.mcp.tool()
        async def fs_write(path: str, content: str) -> str:
            """Записать файл (UTF-8). Создаёт промежуточные директории."""
            p = self._safe(path)
            if p is None:
                return _base._err("path not allowed")
            try:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(content, encoding="utf-8")
                return _base._ok(size=p.stat().st_size)
            except Exception as e:
                return _base._err(str(e))

        @self.mcp.tool()
        async def fs_list(dir: str = ".") -> str:
            """Список файлов в директории."""
            p = self._safe(dir)
            if p is None or not p.is_dir():
                return _base._err("dir not allowed or not a directory")
            try:
                items = [
                    {"name": x.name, "is_dir": x.is_dir(), "size":
                     x.stat().st_size if x.is_file() else 0}
                    for x in sorted(p.iterdir())
                ]
                return _base._ok(items=items)
            except Exception as e:
                return _base._err(str(e))

        @self.mcp.tool()
        async def fs_stat(path: str) -> str:
            """Метаданные файла/директории."""
            p = self._safe(path)
            if p is None or not p.exists():
                return _base._err("path not allowed or does not exist")
            try:
                st = p.stat()
                return _base._ok(
                    size=st.st_size, modified=st.st_mtime,
                    is_dir=p.is_dir(), is_file=p.is_file(),
                    abs_path=str(p),
                )
            except Exception as e:
                return _base._err(str(e))


def main():
    _base.run_stdio_server(FsServer())


if __name__ == "__main__":
    main()
