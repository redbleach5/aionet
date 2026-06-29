<#
.SYNOPSIS
  Остановка всех сервисов Aionet (Windows PowerShell).

.DESCRIPTION
  Останавливает процессы mock_ollama, memory, llm_engine, tools, avatar,
  agent_core. Сначала пытается прочитать logs\pids.txt (быстрый путь),
  потом делает fallback — ищет python-процессы по командной строке.

.EXAMPLE
  .\scripts\stop_bg.ps1
  .\scripts\stop_bg.ps1 -Force    # принудительно через Stop-Process -Force
#>
[CmdletBinding()]
param(
  [switch]$Force
)

$ErrorActionPreference = "Continue"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir

Write-Host "[i] Stopping Aionet services..." -ForegroundColor Cyan

# ── 1. Быстрый путь: из logs\pids.txt ──
$pidsFile = Join-Path $ProjectRoot "logs\pids.txt"
$stoppedFromPids = 0
if (Test-Path $pidsFile) {
  $pids = Get-Content $pidsFile | Where-Object { $_ -match "^\d+$" }
  foreach ($pid in $pids) {
    try {
      $proc = Get-Process -Id $pid -ErrorAction Stop
      if ($Force) {
        Stop-Process -Id $pid -Force -ErrorAction SilentlyContinue
      } else {
        # Мягкая остановка: SIGTERM-эквивалент
        $proc.CloseMainWindow() | Out-Null
        if (-not $proc.WaitForExit(3000)) {
          Stop-Process -Id $pid -Force -ErrorAction SilentlyContinue
        }
      }
      Write-Host "  stopped PID $pid" -ForegroundColor Green
      $stoppedFromPids++
    } catch {
      # процесс уже мёртв
    }
  }
  Remove-Item $pidsFile -Force -ErrorAction SilentlyContinue
}

# ── 2. Fallback: поиск по командной строке ──
$patterns = @(
  "python -m memory",
  "python -m llm_engine",
  "python -m tools",
  "python -m avatar",
  "python -m agent_core",
  "mock_ollama"
)
$stoppedFromScan = 0
foreach ($pat in $patterns) {
  $procs = Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'py.exe'" -ErrorAction SilentlyContinue |
           Where-Object { $_.CommandLine -like "*$pat*" }
  foreach ($p in $procs) {
    try {
      Stop-Process -Id $p.ProcessId -Force:$Force -ErrorAction SilentlyContinue
      Write-Host "  stopped PID $($p.ProcessId) ($pat)" -ForegroundColor Green
      $stoppedFromScan++
    } catch { }
  }
}

# ── 3. Также ищем реальные процессы mock_ollama.py (через python.exe) ──
$mockProcs = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue |
             Where-Object { $_.CommandLine -like "*mock_ollama*" }
foreach ($p in $mockProcs) {
  try {
    Stop-Process -Id $p.ProcessId -Force:$Force -ErrorAction SilentlyContinue
    Write-Host "  stopped mock_ollama PID $($p.ProcessId)" -ForegroundColor Green
  } catch { }
}

# ── Итог ──
Write-Host ""
$total = $stoppedFromPids + $stoppedFromScan
if ($total -eq 0) {
  Write-Host "[i] No Aionet processes found (already stopped)" -ForegroundColor Yellow
} else {
  Write-Host "[i] Stopped $total processes" -ForegroundColor Green
}

# Проверка портов
Write-Host ""
Write-Host "=== PORTS (after stop) ===" -ForegroundColor Cyan
$ports = @(11434, 5550, 5551, 5552, 5553, 5555, 8765)
foreach ($port in $ports) {
  try {
    $conn = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if ($conn) {
      Write-Host ("  :{0} still listening (forced stop may be needed)" -f $port) -ForegroundColor Yellow
    } else {
      Write-Host ("  :{0} free" -f $port) -ForegroundColor Green
    }
  } catch {
    Write-Host ("  :{0} free" -f $port) -ForegroundColor Green
  }
}
