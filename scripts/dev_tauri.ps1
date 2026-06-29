<#
.SYNOPSIS
  Запуск Aionet в Tauri dev-режиме (Windows).

.DESCRIPTION
  PowerShell-аналог dev_tauri.sh. Что делает:
    1. Проверяет что Rust toolchain установлен (подсказка если нет)
    2. Проверяет что Node.js установлен
    3. Устанавливает npm-зависимости frontend (если node_modules нет)
    4. Устанавливает tauri-cli через cargo (если нет)
    5. Запускает `cargo tauri dev` — поднимает Vite dev-server :5173
       + компилирует Rust backend + открывает окно приложения

  ВАЖНО: backend-сервисы (memory, llm_engine, tools, avatar, agent_core)
  должны быть уже запущены через start_bg.ps1. Tauri-приложение само
  подключается к agent_core (:5550) и avatar_bridge (:8765).

.EXAMPLE
  .\scripts\dev_tauri.ps1
#>
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir
Set-Location $ProjectRoot

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Aionet Tauri dev launcher (Windows)    " -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# 1. Проверка Rust
$cargo = Get-Command cargo -ErrorAction SilentlyContinue
if (-not $cargo) {
  # Проверим в стандартном месте rustup
  $cargoPath = "$env:USERPROFILE\.cargo\bin\cargo.exe"
  if (Test-Path $cargoPath) {
    $env:Path += ";$env:USERPROFILE\.cargo\bin"
    $cargo = Get-Command cargo -ErrorAction SilentlyContinue
  }
}
if (-not $cargo) {
  Write-Host "[ERROR] Rust toolchain not found." -ForegroundColor Red
  Write-Host ""
  Write-Host "  Install Rust via rustup:" -ForegroundColor Yellow
  Write-Host "    1. Download: https://win.rustup.rs/x86_64"
  Write-Host "    2. Run rustup-init.exe"
  Write-Host "    3. Restart PowerShell"
  Write-Host "    4. Re-run this script"
  Write-Host ""
  Write-Host "  Or via winget:" -ForegroundColor Yellow
  Write-Host "    winget install Rustlang.Rustup"
  exit 1
}
$rustVer = (rustc --version 2>&1)
Write-Host "[OK] Rust: $rustVer"

# 2. Проверка Node.js
$node = Get-Command node -ErrorAction SilentlyContinue
if (-not $node) {
  Write-Host "[ERROR] Node.js not found." -ForegroundColor Red
  Write-Host ""
  Write-Host "  Install Node.js 18+:" -ForegroundColor Yellow
  Write-Host "    winget install OpenJS.NodeJS.LTS"
  Write-Host "    # или: https://nodejs.org/"
  exit 1
}
$nodeVer = (node --version 2>&1)
Write-Host "[OK] Node.js: $nodeVer"

# 3. Доп. зависимости для Tauri на Windows
# Tauri требует WebView2 Runtime (Edge) — обычно уже есть на Windows 10/11
Write-Host ""
Write-Host "[i] Tauri requires Microsoft Visual Studio C++ Build Tools."
Write-Host "    If build fails, install:" -ForegroundColor Yellow
Write-Host "      winget install Microsoft.VisualStudio.2022.BuildTools"
Write-Host "    (select 'Desktop development with C++' workload)"
Write-Host ""

# 4. Установка npm-зависимостей
Set-Location "$ProjectRoot\rust\frontend"
if (-not (Test-Path "node_modules")) {
  Write-Host "[i] Installing frontend dependencies (first run, ~1 min)..."
  npm install
  if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] npm install failed" -ForegroundColor Red
    exit 1
  }
}
Write-Host "[OK] frontend deps installed"

# 5. Установка tauri-cli
Set-Location "$ProjectRoot\rust"
$tauriVer = $null
try {
  $tauriVer = (cargo tauri --version 2>&1 | Select-Object -First 1)
} catch { }
if (-not $tauriVer) {
  Write-Host "[i] Installing tauri-cli (first run, ~2 min)..."
  cargo install tauri-cli --version "^2.0"
  if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] cargo install tauri-cli failed" -ForegroundColor Red
    exit 1
  }
  $tauriVer = (cargo tauri --version 2>&1 | Select-Object -First 1)
}
Write-Host "[OK] tauri-cli: $tauriVer"

# 6. Проверка что backend запущен
Write-Host ""
Write-Host "=== Backend health check ===" -ForegroundColor Cyan
try {
  $null = Invoke-RestMethod -Uri "http://127.0.0.1:11434/api/tags" -TimeoutSec 2
  Write-Host "  Ollama: OK" -ForegroundColor Green
} catch {
  Write-Host "  [WARN] Ollama/mock_ollama not responding on :11434" -ForegroundColor Yellow
  Write-Host "         Start backend first: .\scripts\start_bg.ps1"
}
try {
  $conn = Get-NetTCPConnection -LocalPort 5550 -State Listen -ErrorAction SilentlyContinue
  if ($conn) {
    Write-Host "  agent_core :5550 OK" -ForegroundColor Green
  } else {
    Write-Host "  [WARN] agent_core not listening on :5550" -ForegroundColor Yellow
    Write-Host "         Start backend first: .\scripts\start_bg.ps1"
  }
} catch {
  Write-Host "  [WARN] Cannot check :5550" -ForegroundColor Yellow
}
try {
  $conn = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue
  if ($conn) {
    Write-Host "  avatar_bridge :8765 OK" -ForegroundColor Green
  } else {
    Write-Host "  [WARN] avatar_bridge not listening on :8765" -ForegroundColor Yellow
    Write-Host "         Start backend first: .\scripts\start_bg.ps1"
  }
} catch {
  Write-Host "  [WARN] Cannot check :8765" -ForegroundColor Yellow
}

# 7. Запуск
Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Starting Tauri dev mode..."
Write-Host "  Vite dev-server: http://localhost:5173"
Write-Host "  Tauri window opens automatically."
Write-Host ""
Write-Host "  Press Ctrl-C to stop."
Write-Host "========================================" -ForegroundColor Cyan
Set-Location "$ProjectRoot\rust"
cargo tauri dev
