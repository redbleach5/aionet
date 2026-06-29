#!/usr/bin/env bash
# Запуск всех ZeroMQ-воркеров в правильном порядке.
# Порядок: memory → llm_engine → tools(broker) → avatar_bridge → agent_core
# Tauri-приложение запускается отдельно (см. README.md).
set -euo pipefail
cd "$(dirname "$0")/.."

export PYTHONPATH="${PYTHONPATH:-}:$(pwd)/python:$(pwd)/proto/_gen"
export AIONET_CONFIG="$(pwd)/config.toml"
export PYTHONUNBUFFERED=1

mkdir -p logs data workspace

PIDS=()

cleanup() {
  echo ""
  echo "Shutting down services..."
  for pid in "${PIDS[@]}"; do
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
    fi
  done
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

start_svc() {
  local name="$1"; shift
  echo "[$(date +%H:%M:%S)] starting $name: $*"
  "$@" &
  PIDS+=("$!")
  sleep 0.4
}

# 1) Memory (FAISS)
start_svc "memory"       python -m memory

# 2) LLM Engine
start_svc "llm_engine"   python -m llm_engine

# 3) MCP Broker (поднимает stdio-MCP серверы)
start_svc "tools_broker" python -m tools

# 4) Avatar WS-bridge
start_svc "avatar_bridge" python -m avatar

# 5) Agent Core (последний — он держит REQ-соединения к остальным)
start_svc "agent_core"   python -m agent_core

echo ""
echo "============================================================"
echo " All Aionet services started. Tauri app should connect to:"
echo "   - agent_core: $(grep agent_core_endpoint config.toml | head -1)"
echo "   - avatar ws:  ws://127.0.0.1:8765"
echo "============================================================"
echo ""

wait
