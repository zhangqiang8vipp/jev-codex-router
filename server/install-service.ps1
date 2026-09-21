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

$RepoRoot = [IO.Path]::GetFullPath($RepoRoot)
if (-not [string]::IsNullOrWhiteSpace($RouterDir)) {
  $RouterDir = [IO.Path]::GetFullPath($RouterDir)
  if (-not (Test-Path -LiteralPath (Join-Path $RouterDir "src\control.mjs") -PathType Leaf)) {
    throw "Codex Router control.mjs not found under: $RouterDir"
  }
}
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
  Start-Sleep -Milliseconds 500
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

$healthy = $false
for ($i = 0; $i -lt 20; $i++) {
  Start-Sleep -Milliseconds 750
  try {
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:4319/health" -TimeoutSec 2
    if ($health.ok -eq $true) {
      $healthy = $true
      break
    }
  } catch {
  }
}
if (-not $healthy) {
  throw "Jev service did not become healthy. Inspect $StateDir\jev-router.err.log"
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
