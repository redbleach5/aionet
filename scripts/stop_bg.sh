#!/usr/bin/env bash
# Остановка всех сервисов Aionet (Linux/macOS).
# Аналог stop_bg.ps1 для Windows.
set -u
cd "$(dirname "$0")/.."

echo "[i] Stopping Aionet services..."

# 1. Быстрый путь: из logs/pids.txt
if [ -f logs/pids.txt ]; then
  while read -r pid; do
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null
      echo "  stopped PID $pid"
    fi
  done < logs/pids.txt
  rm -f logs/pids.txt
fi

# 2. Fallback: по шаблонам командной строки
patterns=(
  "python -m memory"
  "python -m llm_engine"
  "python -m tools"
  "python -m avatar"
  "python -m agent_core"
  "mock_ollama"
)
for pat in "${patterns[@]}"; do
  pkill -f "$pat" 2>/dev/null && echo "  stopped: $pat"
done

sleep 1

# 3. Проверка портов
echo ""
echo "=== PORTS (after stop) ==="
for port in 11434 5550 5551 5552 5553 5555 8765; do
  if ss -tln 2>/dev/null | grep -q ":$port "; then
    echo "  :$port still listening (try: $0 --force)"
  else
    echo "  :$port free"
  fi
done
