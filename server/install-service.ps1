[CmdletBinding()]
param(
  [string]$RepoRoot = (Split-Path -Parent $PSScriptRoot),
  [string]$JevEnvFile = "",
  [string]$StateDir = "",
  [string]$RouterDir = "",
  [string]$TaskName = "Jev Codex Router",
  [string]$EvalTaskName = "Jev Codex Router Shadow Eval"
)

$ErrorActionPreference = "Stop"

if ($env:OS -ne "Windows_NT") {
  throw "server/install-service.ps1 is for Windows only."
}

function Resolve-Python {
  $py = Get-Command py.exe -ErrorAction SilentlyContinue
  if ($py) {
    return [pscustomobject]@{ Path = $py.Source; Prefix = @("-3") }
  }
  $python = Get-Command python.exe -ErrorAction SilentlyContinue
  if ($python) {
    return [pscustomobject]@{ Path = $python.Source; Prefix = @() }
  }
  throw "Python 3 was not found. Install Python 3.11+ or the Python launcher."
}

function Get-JevListenerProcess {
  $netCmd = Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue
  if (-not $netCmd) { return $null }
  try {
    $connection = Get-NetTCPConnection -LocalAddress "127.0.0.1" -LocalPort 4319 -State Listen -ErrorAction SilentlyContinue |
      Select-Object -First 1
    if (-not $connection) { return $null }
    return Get-CimInstance Win32_Process -Filter "ProcessId = $($connection.OwningProcess)" -ErrorAction SilentlyContinue
  } catch {
    return $null
  }
}

function Wait-JevPortRelease([int]$TimeoutSeconds = 12) {
  $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
  do {
    $listener = Get-JevListenerProcess
    if (-not $listener) { return $true }

    $commandLine = [string]$listener.CommandLine
    if ($commandLine -match '(?i)jev_server\.py' -and
        ($commandLine -match '(?i)JevCodexRouter' -or
         $commandLine -like "*$RepoRoot*")) {
      try {
        Stop-Process -Id ([int]$listener.ProcessId) -Force -ErrorAction Stop
      } catch {}
    }

    Start-Sleep -Milliseconds 300
  } while ((Get-Date) -lt $deadline)

  return -not [bool](Get-JevListenerProcess)
}

function Test-JevServiceSurface {
  try {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    if ($task.State -ne "Running") { return $false }

    $health = Invoke-RestMethod -Uri "http://127.0.0.1:4319/health" -TimeoutSec 2
    if ($health.ok -ne $true -or $health.service -ne "jev-router") { return $false }

    $catalog = Invoke-RestMethod -Uri "http://127.0.0.1:4319/v1/models" -TimeoutSec 2
    $auto = @($catalog.data | Where-Object { $_.id -eq "auto" } | Select-Object -First 1)
    return $auto.Count -gt 0
  } catch {
    return $false
  }
}

function Wait-JevServiceStable([int]$TimeoutSeconds = 30) {
  $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
  $stable = 0
  do {
    Start-Sleep -Milliseconds 500
    if (Test-JevServiceSurface) {
      $stable += 1
      if ($stable -ge 3) { return $true }
    } else {
      $stable = 0
    }
  } while ((Get-Date) -lt $deadline)
  return $false
}

$RepoRoot = [IO.Path]::GetFullPath($RepoRoot)
$runService = Join-Path $RepoRoot "server\run-service.ps1"
$runReport = Join-Path $RepoRoot "server\run-shadow-report.ps1"
$server = Join-Path $RepoRoot "server\jev_server.py"
foreach ($required in @($runService, $runReport, $server)) {
  if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
    throw "Required file not found: $required"
  }
}

if ([string]::IsNullOrWhiteSpace($StateDir)) {
  $StateDir = if ($env:CODEX_ROUTER_STATE_DIR) {
    $env:CODEX_ROUTER_STATE_DIR
  } else {
    Join-Path $HOME ".codex\codex-router"
  }
}
$StateDir = [IO.Path]::GetFullPath($StateDir)
[void][IO.Directory]::CreateDirectory($StateDir)

$routerCandidates = @()
if (-not [string]::IsNullOrWhiteSpace($RouterDir)) { $routerCandidates += $RouterDir }
if (-not [string]::IsNullOrWhiteSpace($env:CODEX_ROUTER_DIR)) { $routerCandidates += $env:CODEX_ROUTER_DIR }
$routerStateFile = Join-Path $StateDir "jev-router-dir.txt"
if (Test-Path -LiteralPath $routerStateFile -PathType Leaf) {
  try {
    $savedRouter = (Get-Content -LiteralPath $routerStateFile -Raw -ErrorAction Stop).Trim()
    if ($savedRouter) { $routerCandidates += $savedRouter }
  } catch {}
}
if ($env:LOCALAPPDATA) { $routerCandidates += (Join-Path $env:LOCALAPPDATA "codex-router") }
$routerCandidates += @(
  (Join-Path $HOME "Documents\GitHub\codex-router"),
  (Join-Path $HOME "GitHub\codex-router"),
  (Join-Path $HOME "source\repos\codex-router"),
  (Join-Path $HOME "codex-router")
)

$resolvedRouter = $null
foreach ($candidate in $routerCandidates | Select-Object -Unique) {
  if ([string]::IsNullOrWhiteSpace($candidate)) { continue }
  try { $root = [IO.Path]::GetFullPath($candidate.Trim()) } catch { continue }
  if (Test-Path -LiteralPath (Join-Path $root "src\control.mjs") -PathType Leaf) {
    $resolvedRouter = $root
    break
  }
}
if (-not $resolvedRouter) {
  throw "Codex Router control.mjs could not be resolved. Pass -RouterDir C:\path\to\codex-router."
}
$RouterDir = $resolvedRouter

$routerStateTemp = "$routerStateFile.tmp-$([Guid]::NewGuid().ToString('N'))"
try {
  [IO.File]::WriteAllText($routerStateTemp, $RouterDir + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))
  Move-Item -LiteralPath $routerStateTemp -Destination $routerStateFile -Force
} finally {
  if (Test-Path -LiteralPath $routerStateTemp) {
    Remove-Item -LiteralPath $routerStateTemp -Force -ErrorAction SilentlyContinue
  }
}

if ([string]::IsNullOrWhiteSpace($JevEnvFile)) {
  $JevEnvFile = if ($env:JEV_ENV_FILE) {
    $env:JEV_ENV_FILE
  } else {
    Join-Path $HOME ".hermes\.env"
  }
}
$JevEnvFile = [IO.Path]::GetFullPath($JevEnvFile)
if (-not (Test-Path -LiteralPath $JevEnvFile -PathType Leaf)) {
  throw "TypeSafe/Jev key file not found: $JevEnvFile. Create it with one line: TYPESAFE_API_KEY=..."
}

try {
  $identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
  & icacls.exe $JevEnvFile /inheritance:r /grant:r "${identity}:(R,W)" | Out-Null
} catch {
  Write-Warning "Could not tighten the key-file ACL automatically: $($_.Exception.Message)"
}

$powerShell = (Get-Command powershell.exe -ErrorAction Stop).Source
$serviceArgs = @(
  "-NoProfile",
  "-WindowStyle", "Hidden",
  "-ExecutionPolicy", "Bypass",
  "-File", ('"{0}"' -f $runService),
  "-RepoRoot", ('"{0}"' -f $RepoRoot),
  "-JevEnvFile", ('"{0}"' -f $JevEnvFile),
  "-StateDir", ('"{0}"' -f $StateDir),
  "-RouterDir", ('"{0}"' -f $RouterDir)
) -join " "

$serviceAction = New-ScheduledTaskAction -Execute $powerShell -Argument $serviceArgs
$currentUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$logon = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
$heartbeatParams = @{
  Once = $true
  At = (Get-Date).AddMinutes(1)
  RepetitionInterval = (New-TimeSpan -Minutes 1)
  RepetitionDuration = (New-TimeSpan -Days 3650)
}
$heartbeat = New-ScheduledTaskTrigger @heartbeatParams
$settingsParams = @{
  ExecutionTimeLimit = [TimeSpan]::Zero
  RestartCount = 999
  RestartInterval = (New-TimeSpan -Minutes 1)
  AllowStartIfOnBatteries = $true
  DontStopIfGoingOnBatteries = $true
  MultipleInstances = "IgnoreNew"
  StartWhenAvailable = $true
}
$settings = New-ScheduledTaskSettingsSet @settingsParams
$principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Limited

$existingServiceTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existingServiceTask) {
  try { Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue } catch {}
}
if (-not (Wait-JevPortRelease 12)) {
  $listener = Get-JevListenerProcess
  $detail = if ($listener) {
    "PID=$($listener.ProcessId) command=$([string]$listener.CommandLine)"
  } else {
    "listener details unavailable"
  }
  throw "Port 4319 is still occupied after stopping the old Jev task ($detail)."
}

Register-ScheduledTask -TaskName $TaskName -Action $serviceAction -Trigger @($logon, $heartbeat) -Settings $settings -Principal $principal -Force | Out-Null

$evalArgs = @(
  "-NoProfile",
  "-WindowStyle", "Hidden",
  "-ExecutionPolicy", "Bypass",
  "-File", ('"{0}"' -f $runReport),
  "-RepoRoot", ('"{0}"' -f $RepoRoot),
  "-StateDir", ('"{0}"' -f $StateDir),
  "-Days", "7"
) -join " "
$evalAction = New-ScheduledTaskAction -Execute $powerShell -Argument $evalArgs
$evalTrigger = New-ScheduledTaskTrigger -Daily -At 3:15am
$evalSettings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 15) -StartWhenAvailable
Register-ScheduledTask -TaskName $EvalTaskName -Action $evalAction -Trigger $evalTrigger -Settings $evalSettings -Principal $principal -Force | Out-Null

Start-ScheduledTask -TaskName $TaskName

$healthy = Wait-JevServiceStable 30
if (-not $healthy) {
  # One explicit restart covers a task that lost the first launch during the
  # service handoff. Do not loop forever; the scheduled task already has its
  # own minute-level restart policy.
  try { Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue } catch {}
  [void](Wait-JevPortRelease 8)
  try { Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop } catch {}
  $healthy = Wait-JevServiceStable 20
}
if (-not $healthy) {
  $taskState = "missing"
  $lastResult = "unknown"
  try { $taskState = (Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop).State } catch {}
  try { $lastResult = (Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction Stop).LastTaskResult } catch {}
  $tail = ""
  $errLog = Join-Path $StateDir "jev-router.err.log"
  if (Test-Path -LiteralPath $errLog -PathType Leaf) {
    try { $tail = ((Get-Content -LiteralPath $errLog -Tail 12) -join " | ") } catch {}
  }
  throw "Jev service did not stay healthy with /v1/models available (task=$taskState LastTaskResult=$lastResult). $tail"
}

$python = Resolve-Python
$env:JEV_ENV_FILE = $JevEnvFile
$env:CODEX_ROUTER_STATE_DIR = $StateDir
if (-not [string]::IsNullOrWhiteSpace($RouterDir)) {
  $env:CODEX_ROUTER_DIR = $RouterDir
}
$checkArgs = @($python.Prefix) + @($server, "--check-core")
& $python.Path @checkArgs
if ($LASTEXITCODE -ne 0) {
  throw "Bootstrap readiness failed with status $LASTEXITCODE."
}

Write-Host ""
Write-Host "Jev Windows service installed."
Write-Host "  service task: $TaskName"
Write-Host "  eval task:    $EvalTaskName (daily 03:15, rolling 7 days)"
Write-Host "  state:        $StateDir"
Write-Host "Uninstall: powershell -NoProfile -ExecutionPolicy Bypass -File server\uninstall-service.ps1"
