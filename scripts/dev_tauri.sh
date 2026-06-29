#!/usr/bin/env bash
# Запуск Aionet в Tauri dev-режиме (Linux/macOS).
#
# Что делает:
#   1. Проверяет что Rust toolchain установлен (подсказка если нет)
#   2. Проверяет что Node.js установлен
#   3. Устанавливает npm-зависимости frontend (если node_modules нет)
#   4. Устанавливает tauri-cli через cargo (если нет)
#   5. Запускает `cargo tauri dev` — поднимает Vite dev-server :5173
#      + компилирует Rust backend + открывает окно приложения
#
# ВАЖНО: backend-сервисы (memory, llm_engine, tools, avatar, agent_core)
# должны быть уже запущены через start_bg.sh. Tauri-приложение само
# подключается к agent_core (:5550) и avatar_bridge (:8765).
set -u

cd "$(dirname "$0")/.."
ROOT="$(pwd)"

echo "========================================"
echo "  Aionet Tauri dev launcher (Linux/macOS)"
echo "========================================"
echo ""

# 1. Проверка Rust
if ! command -v cargo &> /dev/null; then
  echo "[ERROR] Rust toolchain not found."
  echo ""
  echo "  Install Rust via rustup:"
  echo "    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh"
  echo "    source \$HOME/.cargo/env"
  echo ""
  echo "  Then re-run this script."
  exit 1
fi
echo "[OK] Rust: $(rustc --version)"

# 2. Проверка Node.js
if ! command -v node &> /dev/null; then
  echo "[ERROR] Node.js not found."
  echo ""
  echo "  Install Node.js 18+:"
  echo "    # macOS:  brew install node"
  echo "    # Ubuntu: curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash - && sudo apt install -y nodejs"
  echo "    # или:    https://nodejs.org/"
  exit 1
fi
echo "[OK] Node.js: $(node --version)"

# 3. Установка npm-зависимостей
cd "$ROOT/rust/frontend"
if [ ! -d "node_modules" ]; then
  echo ""
  echo "[i] Installing frontend dependencies (first run, ~1 min)..."
  npm install
  if [ $? -ne 0 ]; then
    echo "[ERROR] npm install failed"
    exit 1
  fi
fi
echo "[OK] frontend deps installed"

# 4. Установка tauri-cli
cd "$ROOT/rust"
if ! cargo tauri --version &> /dev/null; then
  echo ""
  echo "[i] Installing tauri-cli (first run, ~2 min)..."
  cargo install tauri-cli --version "^2.0"
  if [ $? -ne 0 ]; then
    echo "[ERROR] cargo install tauri-cli failed"
    exit 1
  fi
fi
echo "[OK] tauri-cli: $(cargo tauri --version 2>&1 | head -1)"

# 5. Проверка что backend запущен
echo ""
echo "=== Backend health check ==="
if ! curl -s http://127.0.0.1:11434/api/tags > /dev/null 2>&1; then
  echo "[WARN] Ollama/mock_ollama not responding on :11434"
  echo "       Start backend first: bash scripts/start_bg.sh"
fi
if ! ss -tln 2>/dev/null | grep -q ":5550 "; then
  echo "[WARN] agent_core not listening on :5550"
  echo "       Start backend first: bash scripts/start_bg.sh"
fi
if ! ss -tln 2>/dev/null | grep -q ":8765 "; then
  echo "[WARN] avatar_bridge not listening on :8765"
  echo "       Start backend first: bash scripts/start_bg.sh"
fi
echo ""

# 6. Запуск
echo "========================================"
echo "  Starting Tauri dev mode..."
echo "  Vite dev-server: http://localhost:5173"
echo "  Tauri window opens automatically."
echo ""
echo "  Press Ctrl-C to stop."
echo "========================================"
cd "$ROOT/rust"
cargo tauri dev
