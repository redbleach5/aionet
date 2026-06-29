#!/usr/bin/env python3
"""Запуск всех сервисов Aionet в тестовом окружении.

Поднимает по порядку:
  1. mock_ollama (если ещё не запущен) — имитирует Ollama API
  2. memory (FAISS + SQLite)
  3. llm_engine (Ollama client)
  4. tools (MCP broker)
  5. avatar (WS bridge)
  6. agent_core (orchestrator)

PID'ы пишет в /tmp/aionet_pids.txt для последующей остановки.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
os.environ["PYTHONPATH"] = f"{ROOT}/python:{ROOT}/proto/_gen"
os.environ["AIONET_CONFIG"] = str(ROOT / "config.toml")
os.environ["PYTHONUNBUFFERED"] = "1"

LOGS = ROOT / "logs"
LOGS.mkdir(exist_ok=True)
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
(ROOT / "workspace").mkdir(exist_ok=True)

PIDS_FILE = ROOT / "logs" / "pids.txt"


def is_port_listening(port: int) -> bool:
    import socket
    s = socket.socket()
    s.settimeout(0.3)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except Exception:
        return False
    finally:
        s.close()


PROCS: list[subprocess.Popen] = []


def start(name: str, *cmd: str, wait_port: int | None = None) -> subprocess.Popen:
    log_file = open(LOGS / f"{name}.log", "w")
    p = subprocess.Popen(
        list(cmd),
        stdout=log_file, stderr=subprocess.STDOUT,
        env=os.environ.copy(),
    )
    PROCS.append(p)
    print(f"[+] {name:15s} PID={p.pid}  cmd={' '.join(cmd[:3])}...")
    if wait_port:
        # ждём до 5с пока порт не встанет
        for _ in range(50):
            if is_port_listening(wait_port):
                print(f"    port {wait_port} ✓")
                break
            if p.poll() is not None:
                print(f"    ✗ process died early — see logs/{name}.log")
                break
            time.sleep(0.1)
        else:
            print(f"    ⚠ port {wait_port} still not listening after 5s")
    return p


def main():
    # 0. mock_ollama (если не запущен)
    if not is_port_listening(11434):
        start("mock_ollama", sys.executable, "scripts/mock_ollama.py",
              wait_port=11434)
    else:
        print("[=] mock_ollama already running on :11434")

    # 1. memory
    start("memory", sys.executable, "-m", "memory")
    time.sleep(1.5)

    # 2. llm_engine
    start("llm_engine", sys.executable, "-m", "llm_engine")
    time.sleep(1.0)

    # 3. tools broker — поднимает stdio-MCP-серверы (shell/fs/winget/browser)
    start("tools", sys.executable, "-m", "tools")
    time.sleep(2.0)

    # 4. avatar bridge
    start("avatar", sys.executable, "-m", "avatar")
    time.sleep(1.0)

    # 5. agent_core
    start("agent_core", sys.executable, "-m", "agent_core")
    time.sleep(1.0)

    # Сохраняем PID'ы
    with open(PIDS_FILE, "w") as f:
        for p in PROCS:
            f.write(f"{p.pid}\n")
    print(f"\n[i] {len(PROCS)} processes started. PIDs saved to {PIDS_FILE}")
    print(f"[i] logs in {LOGS}/")
    print(f"\n[i] Services running. Press Ctrl-C to stop all.")
    try:
        # Ждём, но НЕ убиваем остальных если один умер — логируем и продолжаем
        while True:
            for p in PROCS:
                if p.poll() is not None and p.returncode != 0:
                    # Процесс упал — логируем, но НЕ убиваем остальных
                    log_name = "?"
                    # Находим имя по PID
                    for name, lp in [("mock_ollama", PROCS[0] if PROCS else None),
                                      ("memory", PROCS[1] if len(PROCS) > 1 else None),
                                      ("llm_engine", PROCS[2] if len(PROCS) > 2 else None),
                                      ("tools", PROCS[3] if len(PROCS) > 3 else None),
                                      ("avatar", PROCS[4] if len(PROCS) > 4 else None),
                                      ("agent_core", PROCS[5] if len(PROCS) > 5 else None)]:
                        if lp is p:
                            log_name = name
                            break
                    print(f"[!] {log_name} (PID {p.pid}) exited with code {p.returncode} — see logs/{log_name}.log")
                    # Помечаем как "умер", чтобы не логировать повторно
                    p._logged_dead = True  # type: ignore
            time.sleep(2)
    except KeyboardInterrupt:
        print("\n[i] Shutting down all services...")
        for p in reversed(PROCS):
            try:
                if p.poll() is None:  # ещё жив
                    p.send_signal(signal.SIGTERM)
                    p.wait(timeout=3)
                    print(f"    [-] PID {p.pid} terminated")
            except Exception:
                try:
                    p.kill()
                    print(f"    [-] PID {p.pid} killed")
                except Exception:
                    pass


if __name__ == "__main__":
    main()
