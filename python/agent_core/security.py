"""Реализация абстракции Sandbox.

Поддерживаются 3 реализации:
  * DockerSandbox — основная. Контейнер с seccomp+AppArmor, read-only rootfs,
    непривилегированный user, no-network по умолчанию.
  * MXCSandbox    — заглушка под Microsoft Execution Containers (когда станет
    доступен — заменить реализацию run()).
  * NoneSandbox   — для dev-режима на хосте без Docker. БЕЗОПАСНО ТОЛЬКО В DEV.

Выбор реализации — по config.security.backend.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import time
from pathlib import Path
from typing import Mapping

from common.config import Config
from common.interfaces import Sandbox, SandboxResult
from common.logging import get_logger

log = get_logger(__name__)


class NoneSandbox(Sandbox):
    """DEV-режим: запуск напрямую subprocess на хосте. Никакой изоляции."""

    def __init__(self, cfg: Config):
        self.cfg = cfg

    def run(self, *, command, workspace=None, network=False, timeout_s=30, env=None):
        t0 = time.time()
        try:
            r = subprocess.run(
                command,
                cwd=workspace or None,
                env={**os.environ, **(env or {})},
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
            return SandboxResult(
                exit_code=r.returncode,
                stdout=r.stdout,
                stderr=r.stderr,
                duration_ms=int((time.time() - t0) * 1000),
                ok=r.returncode == 0,
            )
        except subprocess.TimeoutExpired as e:
            return SandboxResult(
                exit_code=124, stdout=e.stdout or "", stderr=e.stderr or "",
                duration_ms=int((time.time() - t0) * 1000), ok=False,
            )


class DockerSandbox(Sandbox):
    """Docker-песочница с seccomp + AppArmor + read-only rootfs."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        sec = cfg.security
        self.image = sec.get("sandbox_image", "aionet-toolbox:latest")
        self.seccomp = sec.get("seccomp_profile")
        self.apparmor = sec.get("apparmor_profile")
        self.user = sec.get("run_as_user", "1000:1000")
        self.network = bool(sec.get("network_enabled", False))
        self.read_only = bool(sec.get("read_only_rootfs", True))
        self.mem_limit = sec.get("memory_limit_mb", 512)
        self.cpu_quota = sec.get("cpu_quota_percent", 50)
        self.mount_workspace = sec.get("mount_workspace", "/workspace")
        self._docker_check()

    @staticmethod
    def _docker_check():
        try:
            subprocess.run(["docker", "version"], check=True,
                           capture_output=True, timeout=10)
        except Exception as e:
            raise RuntimeError(f"Docker not available: {e}") from e

    def run(self, *, command, workspace=None, network=False, timeout_s=30, env=None):
        ws = str(Path(workspace or ".").resolve())
        cmd = [
            "docker", "run", "--rm",
            "-i",
            "--user", self.user,
            "--memory", f"{self.mem_limit}m",
            "--cpus", str(self.cpu_quota / 100.0),
            "--read-only" if self.read_only else "--read-only=false",
            "--tmpfs", "/tmp:rw,size=64m,mode=1777",
            "--security-opt", f"seccomp={self.seccomp}" if self.seccomp else "seccomp=unconfined",
        ]
        if self.apparmor:
            cmd += ["--security-opt", f"apparmor={self.apparmor}"]
        if not (self.network or network):
            cmd += ["--network", "none"]
        # Монтируем workspace
        cmd += ["-v", f"{ws}:{self.mount_workspace}:rw"]
        cmd += ["-w", self.mount_workspace]
        # ENV-переменные
        for k, v in (env or {}).items():
            cmd += ["-e", f"{k}={v}"]
        # Образ + команда
        cmd += [self.image] + list(command)

        log.debug("docker run: %s", " ".join(shlex.quote(c) for c in cmd))
        t0 = time.time()
        try:
            r = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=timeout_s + 5,  # +5с на накладные расходы Docker
            )
            return SandboxResult(
                exit_code=r.returncode,
                stdout=r.stdout,
                stderr=r.stderr,
                duration_ms=int((time.time() - t0) * 1000),
                ok=r.returncode == 0,
            )
        except subprocess.TimeoutExpired as e:
            return SandboxResult(
                exit_code=124, stdout=e.stdout or "", stderr=e.stderr or "timeout",
                duration_ms=int((time.time() - t0) * 1000), ok=False,
            )


class MXCSandbox(Sandbox):
    """Заглушка под Microsoft Execution Containers.

    На момент реализации MXC не имеет публичных релизов. Класс оставлен как
    точка расширения: при появлении MXC достаточно реализовать метод run()
    через их SDK, не трогая остальной код.
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        log.warning("MXCSandbox requested but MXC unavailable; falling back to DockerSandbox")
        self._fallback = DockerSandbox(cfg)

    def run(self, **kwargs):
        # TODO: заменить на реальный MXC API, когда будет опубликован.
        return self._fallback.run(**kwargs)


def make_sandbox(cfg: Config) -> Sandbox:
    backend = cfg.security.get("backend", "docker")
    if backend == "mxc":
        try:
            return MXCSandbox(cfg)
        except Exception:
            log.exception("MXC init failed; falling back to docker")
            return DockerSandbox(cfg)
    if backend == "docker":
        try:
            return DockerSandbox(cfg)
        except Exception:
            log.exception("Docker init failed; falling back to NoneSandbox (DEV ONLY)")
            return NoneSandbox(cfg)
    if backend == "none":
        return NoneSandbox(cfg)
    raise ValueError(f"unknown sandbox backend: {backend}")
