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
from common.interfaces import Sandbox, SandboxPolicy, SandboxResult
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
    """Docker-песочница с seccomp + AppArmor + read-only rootfs.

    Поддерживает два режима конфигурации:
      1) Из config.toml [security] — дефолтные параметры (init-time)
      2) Через apply_policy(SandboxPolicy) — переопределение для конкретного
         вызова (например, разные политики для shell vs fs)

    При наличии policy — его поля имеют приоритет над init-параметрами,
    но явные аргументы run(network=True) переопределяют policy для этого вызова.
    """

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
        self._policy: SandboxPolicy | None = None
        self._docker_check()

    def apply_policy(self, policy) -> None:
        """Применяет SandboxPolicy к Docker-песочнице.

        Конвертирует абстрактную политику в Docker security-opt:
          seccomp_profile  → --security-opt seccomp=<path>
          apparmor_profile → --security-opt apparmor=<name>
          network          → --network none (если пусто) или --add-host per entry
          fs_read/fs_write → -v <path>:<path>:ro|rw
          capabilities     → --cap-add <cap>
          memory_limit_mb  → --memory <n>m
          cpu_quota_percent→ --cpus <n>
          pid_limit        → --pids-limit <n>
          env              → -e KEY=VALUE
        """
        if isinstance(policy, Mapping):
            policy = SandboxPolicy.from_dict(policy)
        self._policy = policy
        log.info("DockerSandbox policy applied: net=%s fs_read=%d fs_write=%d caps=%d",
                 bool(policy.network), len(policy.fs_read), len(policy.fs_write),
                 len(policy.capabilities))

    @staticmethod
    def _docker_check():
        try:
            subprocess.run(["docker", "version"], check=True,
                           capture_output=True, timeout=10)
        except Exception as e:
            raise RuntimeError(f"Docker not available: {e}") from e

    def _resolve_params(self, *, network_override: bool | None,
                        env_override: Mapping[str, str] | None):
        """Сливает init-параметры, policy и call-override в финальный набор."""
        p = self._policy
        # seccomp
        seccomp = p.seccomp_profile if p and p.seccomp_profile else self.seccomp
        # apparmor
        apparmor = p.apparmor_profile if p and p.apparmor_profile else self.apparmor
        # network: policy.network (список разрешённых) или init-флаг
        if p and p.network:
            # В policy задан список разрешённых хостов — сеть включена
            network_enabled = True
            extra_hosts = p.network
        else:
            network_enabled = network_override if network_override is not None else self.network
            extra_hosts = []
        # memory / cpu / pid — из policy если заданы, иначе init
        mem = p.memory_limit_mb if p else self.mem_limit
        cpu = p.cpu_quota_percent if p else self.cpu_quota
        pid_limit = p.pid_limit if p else None
        # capabilities — из policy (init не имеет)
        caps = p.capabilities if p else []
        # env — merge policy.env + override
        env = {}
        if p:
            env.update(p.env)
        if env_override:
            env.update(env_override)
        return {
            "seccomp": seccomp, "apparmor": apparmor,
            "network_enabled": network_enabled, "extra_hosts": extra_hosts,
            "mem": mem, "cpu": cpu, "pid_limit": pid_limit,
            "caps": caps, "env": env,
        }

    def run(self, *, command, workspace=None, network=False, timeout_s=30, env=None):
        ws = str(Path(workspace or ".").resolve())
        params = self._resolve_params(network_override=network, env_override=env)
        cmd = [
            "docker", "run", "--rm",
            "-i",
            "--user", self.user,
            "--memory", f"{params['mem']}m",
            "--cpus", str(params["cpu"] / 100.0),
            "--read-only" if self.read_only else "--read-only=false",
            "--tmpfs", "/tmp:rw,size=64m,mode=1777",
            "--security-opt",
            f"seccomp={params['seccomp']}" if params["seccomp"] else "seccomp=unconfined",
        ]
        if params["apparmor"]:
            cmd += ["--security-opt", f"apparmor={params['apparmor']}"]
        if params["pid_limit"]:
            cmd += ["--pids-limit", str(params["pid_limit"])]
        for cap in params["caps"]:
            cmd += ["--cap-add", cap]
        # Network: если выключена — --network none, иначе (список extra_hosts)
        if not params["network_enabled"]:
            cmd += ["--network", "none"]
        else:
            for host in params["extra_hosts"]:
                # host может быть "host:ip" или просто "host"
                cmd += ["--add-host", host]
        # Монтируем workspace (rw)
        cmd += ["-v", f"{ws}:{self.mount_workspace}:rw"]
        cmd += ["-w", self.mount_workspace]
        # Дополнительные read-only mounts из policy.fs_read
        if self._policy:
            for path in self._policy.fs_read:
                cmd += ["-v", f"{Path(path).resolve()}:{path}:ro"]
            for path in self._policy.fs_write:
                cmd += ["-v", f"{Path(path).resolve()}:{path}:rw"]
        # ENV-переменные
        for k, v in params["env"].items():
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

    При появлении публичного MXC SDK — заменить _run_via_mxc() на реальную
    интеграцию. Контракт apply_policy() уже совместим: MXC принимает YAML/
    JSON-политики на уровне ядра, мы передаём SandboxPolicy.yaml_policy
    (путь к файлу) или сериализуем SandboxPolicy.to_dict() в YAML.
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._policy: SandboxPolicy | None = None
        log.warning("MXCSandbox requested but MXC unavailable; falling back to DockerSandbox")
        self._fallback = DockerSandbox(cfg)

    def apply_policy(self, policy) -> None:
        """Применяет политику к MXCSandbox.

        При появлении реального MXC SDK здесь будет:
          1) Если policy.yaml_policy задан (путь к YAML) — передать его как есть
          2) Иначе — сериализовать SandboxPolicy в YAML через to_dict() + yaml.dump
          3) Вызвать mxc_client.create_container(yaml_policy)
        Пока — делегируем в DockerSandbox fallback.
        """
        if isinstance(policy, Mapping):
            policy = SandboxPolicy.from_dict(policy)
        self._policy = policy
        if policy.yaml_policy:
            log.info("MXCSandbox: would apply YAML policy from %s (when SDK available)",
                     policy.yaml_policy)
        else:
            log.info("MXCSandbox: would synthesize YAML from SandboxPolicy (when SDK available)")
        # Fallback: передаём в Docker
        self._fallback.apply_policy(policy)

    def run(self, **kwargs):
        # TODO: заменить на реальный MXC API, когда будет опубликован.
        # Контракт: если self._policy задан и в нём yaml_policy —
        # mxc_client.run_with_policy(yaml_path, command, workspace)
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
