<#
.SYNOPSIS
  Запуск всех сервисов Aionet в фоне (Windows PowerShell).

.DESCRIPTION
  PowerShell-аналог start_bg.sh для Windows. Поднимает:
    1. mock_ollama (если Ollama не запущена)
    2. memory
    3. llm_engine
    4. tools broker
    5. avatar bridge
    6. agent_core

  PID'ы сохраняются в logs\pids.txt для последующей остановки через
  stop_bg.ps1.

.PARAMETER SkipMockOllama
  Не запускать mock_ollama (если уже установлена реальная Ollama).

.PARAMETER PythonExe
  Python-интерпретатор (по умолчанию пытается 'python', потом 'py').
  Используйте, если Python установлен в нестандартном месте:
    -PythonExe "C:\Python312\python.exe"

.EXAMPLE
  .\scripts\start_bg.ps1
  .\scripts\start_bg.ps1 -SkipMockOllama
  .\scripts\start_bg.ps1 -PythonExe "C:\Python312\python.exe"
#>
[CmdletBinding()]
param(
  [switch]$SkipMockOllama,
  [string]$PythonExe
)

$ErrorActionPreference = "Stop"

# ── Переход в корень проекта (два уровня вверх от scripts/) ──
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir
Set-Location $ProjectRoot

# ── Окружение ──
$env:PYTHONPATH = "$ProjectRoot\python;$ProjectRoot\proto\_gen"
$env:AIONET_CONFIG = "$ProjectRoot\config.toml"
$env:PYTHONUNBUFFERED = "1"

# Создаём директории
foreach ($d in @("logs", "data", "workspace")) {
  if (-not (Test-Path $d)) { New-Item -ItemType Directory -Path $d | Out-Null }
}

# ── Поиск Python ──
if (-not $PythonExe) {
  $PythonExe = (Get-Command python -ErrorAction SilentlyContinue).Source
  if (-not $PythonExe) {
    $PythonExe = (Get-Command py -ErrorAction SilentlyContinue).Source
    if ($PythonExe) { $PythonExe = "$PythonExe -3" }  # py launcher
  }
  if (-not $PythonExe) {
    Write-Host "[ERROR] Python not found. Install Python 3.11+ or pass -PythonExe." -ForegroundColor Red
    Write-Host "  Download: https://www.python.org/downloads/"
    Write-Host "  Or use: winget install Python.Python.3.12"
    exit 1
  }
}
Write-Host "[i] Python: $PythonExe"

# ── Функция остановки старых процессов Aionet ──
function Stop-AionetProcesses {
  Write-Host "[i] Stopping previous Aionet processes..."
  # Ищем python-процессы, в командной строке которых есть наши модули
  $patterns = @(
    "python -m memory",
    "python -m llm_engine",
    "python -m tools",
    "python -m avatar",
    "python -m agent_core",
    "mock_ollama"
  )
  foreach ($pat in $patterns) {
    # WMI-CIM даёт доступ к CommandLine (Get-Process не даёт)
    $procs = Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'py.exe'" -ErrorAction SilentlyContinue |
             Where-Object { $_.CommandLine -like "*$pat*" }
    foreach ($p in $procs) {
      try {
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
        Write-Host "  stopped PID $($p.ProcessId) ($pat)"
      } catch {
        # процесс мог уже умереть
      }
    }
  }
  Start-Sleep -Seconds 1
}

# ── Проверка занят ли порт ──
function Test-Port {
  param([int]$Port)
  try {
    $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    return $null -ne $conn
  } catch {
    # Get-NetTCPConnection может отсутствовать на старых Windows
    try {
      $tcp = New-Object System.Net.Sockets.TcpListener([IPAddress]::Loopback, $Port)
      $tcp.Stop()
      return $false  # порт свободен (смогли открыть listener)
    } catch {
      return $true  # порт занят
    }
  }
}

# ── Запуск процесса в фоне с лог-файлом ──
# Используем $script: scope чтобы PIDs накапливались между вызовами функции
$script:StartedPids = @()
function Start-BgService {
  param(
    [string]$Name,
    [string[]]$ArgumentList,
    [string]$LogFile
  )
  # ArgumentList передаётся как массив: ["-m", "memory"]
  # $PythonExe может содержать "py -3" — разбиваем
  $pyParts = $PythonExe -split " "
  $exe = $pyParts[0]
  $pyArgs = @()
  if ($pyParts.Count -gt 1) { $pyArgs += $pyParts[1..($pyParts.Count-1)] }
  $pyArgs += $ArgumentList

  $psi = New-Object System.Diagnostics.ProcessStartInfo
  $psi.FileName = $exe
  $psi.Arguments = ($pyArgs | ForEach-Object {
    if ($_ -match "\s") { "`"$_`"" } else { $_ }
  }) -join " "
  $psi.WorkingDirectory = $ProjectRoot
  $psi.UseShellExecute = $false
  $psi.RedirectStandardOutput = $true
  $psi.RedirectStandardError = $true
  $psi.CreateNoWindow = $true
  # Окружение
  foreach ($key in @("PYTHONPATH", "AIONET_CONFIG", "PYTHONUNBUFFERED", "PATH")) {
    $psi.EnvironmentVariables[$key] = (Get-Item "env:$key").Value
  }

  $proc = New-Object System.Diagnostics.Process
  $proc.StartInfo = $psi

  # Асинхронный вывод в лог-файл
  $logPath = Join-Path $ProjectRoot "logs\$LogFile"
  $logWriter = [System.IO.StreamWriter]::new($logPath, $false, [System.Text.Encoding]::UTF8)
  $logWriter.AutoFlush = $true

  $outAction = {
    if (-not $EventArgs.Data) { return }
    $logWriter.WriteLine($EventArgs.Data)
  }
  $errAction = {
    if (-not $EventArgs.Data) { return }
    $logWriter.WriteLine("STDERR: " + $EventArgs.Data)
  }

  Register-ObjectEvent -InputObject $proc -EventName "OutputDataReceived" -Action $outAction | Out-Null
  Register-ObjectEvent -InputObject $proc -EventName "ErrorDataReceived" -Action $errAction | Out-Null

  [void]$proc.Start()
  $proc.BeginOutputReadLine()
  $proc.BeginErrorReadLine()

  $script:StartedPids += $proc.Id
  Write-Host "  $Name : PID=$($proc.Id)"
  return $proc.Id
}

# ── Проверка здоровья mock_ollama ──
function Test-MockOllama {
  try {
    $resp = Invoke-RestMethod -Uri "http://127.0.0.1:11434/api/tags" -TimeoutSec 2 -ErrorAction Stop
    return $true
  } catch {
    return $false
  }
}

# =============================================================================
# Главный сценарий
# =============================================================================

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Aionet services launcher (Windows)    " -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# 0. Останавливаем старые
Stop-AionetProcesses

# 1. mock_ollama (если не SkipMockOllama и Ollama не отвечает)
$mockPid = $null
if (-not $SkipMockOllama) {
  if (Test-MockOllama) {
    Write-Host "[1/6] mock_ollama: skipped (something already listens on :11434)" -ForegroundColor Yellow
  } else {
    Write-Host "[1/6] mock_ollama"
    $mockPid = Start-BgService -Name "mock_ollama" -ArgumentList @("scripts\mock_ollama.py") -LogFile "mock_ollama.log"
    Start-Sleep -Seconds 2
    if (Test-MockOllama) {
      Write-Host "  mock_ollama healthy" -ForegroundColor Green
    } else {
      Write-Host "  mock_ollama unhealthy!" -ForegroundColor Red
    }
  }
} else {
  Write-Host "[1/6] mock_ollama: skipped (-SkipMockOllama)" -ForegroundColor Yellow
}

# 2. memory
Write-Host "[2/6] memory"
Start-BgService -Name "memory" -ArgumentList @("-m", "memory") -LogFile "memory.log" | Out-Null
Start-Sleep -Seconds 2

# 3. llm_engine
Write-Host "[3/6] llm_engine"
Start-BgService -Name "llm_engine" -ArgumentList @("-m", "llm_engine") -LogFile "llm_engine.log" | Out-Null
Start-Sleep -Seconds 1

# 4. tools broker
Write-Host "[4/6] tools broker"
Start-BgService -Name "tools" -ArgumentList @("-m", "tools") -LogFile "tools.log" | Out-Null
Start-Sleep -Seconds 2

# 5. avatar bridge
Write-Host "[5/6] avatar bridge"
Start-BgService -Name "avatar" -ArgumentList @("-m", "avatar") -LogFile "avatar.log" | Out-Null
Start-Sleep -Seconds 1

# 6. agent_core
Write-Host "[6/6] agent_core"
Start-BgService -Name "agent_core" -ArgumentList @("-m", "agent_core") -LogFile "agent_core.log" | Out-Null
Start-Sleep -Seconds 2

# Сохраняем PID'ы
$script:StartedPids | Out-File -FilePath "logs\pids.txt" -Encoding utf8
Write-Host ""
Write-Host "[i] $($script:StartedPids.Count) processes started. PIDs saved to logs\pids.txt"

# Проверка портов
Write-Host ""
Write-Host "=== PORTS ===" -ForegroundColor Cyan
$ports = @(11434, 5550, 5551, 5552, 5553, 5555, 8765)
$aliveCount = 0
foreach ($port in $ports) {
  if (Test-Port -Port $port) {
    Write-Host ("  :{0} OK" -f $port) -ForegroundColor Green
    $aliveCount++
  } else {
    Write-Host ("  :{0} FAIL" -f $port) -ForegroundColor Red
  }
}

Write-Host ""
Write-Host "=== STATUS ===" -ForegroundColor Cyan
Write-Host "  Ports alive: $aliveCount / $($ports.Count)"
if ($aliveCount -ge 6) {
  Write-Host "  Services started successfully!" -ForegroundColor Green
  Write-Host ""
  Write-Host "  Next steps:"
  Write-Host "    - Run tests:  PYTHONPATH=python;proto\_gen python tests\test_integration.py"
  Write-Host "    - Stop all:   .\scripts\stop_bg.ps1"
} else {
  Write-Host "  Some services failed to start!" -ForegroundColor Red
  Write-Host "  Check logs in .\logs\ for details."
}

Write-Host ""
Write-Host "[i] To stop all services: .\scripts\stop_bg.ps1"
