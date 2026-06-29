"""Типизированный доступ к config.toml."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _dotted(cfg: dict, key: str, default: Any = None) -> Any:
    cur: Any = cfg
    for part in key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


@dataclass
class Config:
    raw: dict
    path: Path

    # --- универсальный доступ ---
    def get(self, key: str, default: Any = None) -> Any:
        return _dotted(self.raw, key, default)

    # --- часто используемые секции (кэшируются в свойствах) ---
    @property
    def zmq(self) -> dict:
        return self.raw.get("zmq", {})

    @property
    def llm(self) -> dict:
        return self.raw.get("llm", {})

    @property
    def memory(self) -> dict:
        return self.raw.get("memory", {})

    @property
    def tools(self) -> dict:
        return self.raw.get("tools", {})

    @property
    def security(self) -> dict:
        return self.raw.get("security", {})

    @property
    def avatar(self) -> dict:
        return self.raw.get("avatar", {})

    @property
    def ui(self) -> dict:
        return self.raw.get("ui", {})

    @property
    def system(self) -> dict:
        return self.raw.get("system", {})

    @property
    def tool_servers(self) -> list[dict]:
        return self.tools.get("servers", [])


_DEFAULT_PATH = Path(os.environ.get(
    "AIONET_CONFIG",
    Path(__file__).resolve().parents[2] / "config.toml",
))

_cache: dict[Path, Config] = {}


def load_config(path: Path | str | None = None) -> Config:
    p = Path(path) if path else _DEFAULT_PATH
    p = p.resolve()
    if p in _cache:
        return _cache[p]
    if not p.exists():
        raise FileNotFoundError(f"config.toml not found: {p}")
    with p.open("rb") as f:
        raw = tomllib.load(f)
    cfg = Config(raw=raw, path=p)
    _cache[p] = cfg
    return cfg
