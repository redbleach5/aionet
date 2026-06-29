#!/usr/bin/env bash
# Простой надёжный запуск всех сервисов в фоне, с проверкой здоровья.
set -u
cd "$(dirname "$0")/.."

export PYTHONPATH="$(pwd)/python:$(pwd)/proto/_gen"
export AIONET_CONFIG="$(pwd)/config.toml"
export PYTHONUNBUFFERED="1"

mkdir -p logs data workspace
# Останавливаем старые
pkill -f "python -m memory" 2>/dev/null
pkill -f "python -m llm_engine" 2>/dev/null
pkill -f "python -m tools" 2>/dev/null
pkill -f "python -m avatar" 2>/dev/null
pkill -f "python -m agent_core" 2>/dev/null
pkill -f "mock_ollama" 2>/dev/null
sleep 1

start() {
  local name="$1"; shift
  nohup "$@" > "logs/$name.log" 2>&1 &
  echo "  $name: PID=$!"
}

echo "[1/6] mock_ollama"
start mock_ollama python scripts/mock_ollama.py
sleep 1
curl -s http://127.0.0.1:11434/api/tags > /dev/null && echo "  ✓ mock_ollama healthy" || echo "  ✗ mock_ollama unhealthy"

echo "[2/6] memory"
start memory python -m memory
sleep 1.5

echo "[3/6] llm_engine"
start llm_engine python -m llm_engine
sleep 1

echo "[4/6] tools broker"
start tools python -m tools
sleep 1.5

echo "[5/6] avatar bridge"
start avatar python -m avatar
sleep 1

echo "[6/6] agent_core"
start agent_core python -m agent_core
sleep 1.5

echo ""
echo "=== PORTS ==="
for port in 11434 5550 5551 5552 5553 5555 8765; do
    if ss -tln 2>/dev/null | grep -q ":$port "; then
      echo "  :$port ✓"
    else
      echo "  :$port ✗"
    fi
done

echo ""
echo "=== ALIVE PROCESSES ==="
ps -ef | grep -E "python -m|mock_ollama" | grep -v grep | wc -l
echo "expected: 6"
