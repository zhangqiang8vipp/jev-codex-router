[CmdletBinding()]
param(
  [string]$RouterDir = "",
  [string]$JevEnvFile = "",
  [string]$StateDir = ""
)

$ErrorActionPreference = "Stop"

if ($env:OS -ne "Windows_NT") {
  throw "setup-local.ps1 is the Windows installer. Use setup-local.sh on macOS."
}

function Resolve-RouterDir([string]$Explicit) {
  $candidates = @()
  if (-not [string]::IsNullOrWhiteSpace($Explicit)) { $candidates += $Explicit }
  if ($env:CODEX_ROUTER_DIR) { $candidates += $env:CODEX_ROUTER_DIR }
  $candidates += @(
    (Join-Path (Split-Path -Parent $PSScriptRoot) "codex-router"),
    (Join-Path $HOME "Documents\GitHub\codex-router"),
    (Join-Path $HOME "GitHub\codex-router"),
    (Join-Path $HOME "source\repos\codex-router")
  )
  foreach ($candidate in $candidates) {
    if ([string]::IsNullOrWhiteSpace($candidate)) { continue }
    $root = [IO.Path]::GetFullPath($candidate)
    if (Test-Path -LiteralPath (Join-Path $root "model-router.ps1") -PathType Leaf) {
      return $root
    }
  }
  throw "Codex Router checkout not found. Pass -RouterDir C:\path\to\codex-router."
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

function Invoke-ModelRouter([string[]]$Arguments) {
  & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $script:ModelRouter @Arguments
  if ($LASTEXITCODE -ne 0) {
    throw "Codex Router command failed: $($Arguments -join ' ')"
  }
}

function Test-ModelRouter([string[]]$Arguments) {
  $savedPreference = $ErrorActionPreference
  try {
    # Windows PowerShell 5.1 can promote child-process stderr to a terminating
    # NativeCommandError while ErrorActionPreference=Stop. Probes intentionally
    # use non-zero exits, so judge only the child process exit code.
    $ErrorActionPreference = "Continue"
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $script:ModelRouter @Arguments *> $null
    return $LASTEXITCODE -eq 0
  } catch {
    return $false
  } finally {
    $ErrorActionPreference = $savedPreference
  }
}

function Get-GenericProviders {
  $savedPreference = $ErrorActionPreference
  try {
    $ErrorActionPreference = "Continue"
    $raw = (& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $script:ModelRouter codex providers generic list --json 2>$null | Out-String)
    $code = $LASTEXITCODE
  } finally {
    $ErrorActionPreference = $savedPreference
  }
  if ($code -ne 0 -or [string]::IsNullOrWhiteSpace($raw)) {
    throw "Could not list Codex Router generic providers."
  }
  try {
    return @((ConvertFrom-Json $raw).providers)
  } catch {
    throw "Codex Router returned invalid generic-provider JSON: $($_.Exception.Message)"
  }
}

function Wait-JevProviderDiscovery([int]$Attempts = 6) {
  for ($i = 0; $i -lt $Attempts; $i++) {
    if (Test-ModelRouter @("codex", "providers", "generic", "test", "jev")) {
      return $true
    }

    # A repeated install can briefly race the previous scheduled-task process.
    # Make sure the task is running and the local catalog is actually reachable
    # before asking Codex Router again.
    try {
      $catalog = Invoke-RestMethod -Uri "http://127.0.0.1:4319/v1/models" -TimeoutSec 2
      if (@($catalog.data | Where-Object { $_.id -eq "auto" }).Count -gt 0) {
        $task = Get-ScheduledTask -TaskName "Jev Codex Router" -ErrorAction SilentlyContinue
        if ($task -and $task.State -ne "Running") {
          Start-ScheduledTask -TaskName "Jev Codex Router" -ErrorAction SilentlyContinue
        }
      }
    } catch {}

    Start-Sleep -Seconds 1
  }
  return $false
}

$RepoRoot = [IO.Path]::GetFullPath($PSScriptRoot)
$RouterDir = Resolve-RouterDir $RouterDir
$script:ModelRouter = Join-Path $RouterDir "model-router.ps1"
$discoveryMode = Join-Path $RouterDir "src\discovery-mode.mjs"
$routerService = Join-Path $RouterDir "src\service.mjs"
$curate = Join-Path $RouterDir "src\curate-models.mjs"
$installService = Join-Path $RepoRoot "server\install-service.ps1"
$installToggle = Join-Path $RepoRoot "server\install-toggle.ps1"
$server = Join-Path $RepoRoot "server\jev_server.py"
$report = Join-Path $RepoRoot "server\report_shadow_eval.py"

if (-not (Get-Command node.exe -ErrorAction SilentlyContinue)) {
  throw "Node.js is required by Codex Router."
}
$python = Resolve-Python

if ([string]::IsNullOrWhiteSpace($JevEnvFile)) {
  $JevEnvFile = if ($env:JEV_ENV_FILE) { $env:JEV_ENV_FILE } else { Join-Path $HOME ".hermes\.env" }
}
$JevEnvFile = [IO.Path]::GetFullPath($JevEnvFile)
if (-not (Test-Path -LiteralPath $JevEnvFile -PathType Leaf)) {
  throw "TypeSafe/Jev key file not found: $JevEnvFile. Create $HOME\.hermes\.env with one line: TYPESAFE_API_KEY=YOUR_KEY. Never paste the key into chat."
}

if ([string]::IsNullOrWhiteSpace($StateDir)) {
  $StateDir = if ($env:CODEX_ROUTER_STATE_DIR) {
    $env:CODEX_ROUTER_STATE_DIR
  } else {
    Join-Path $HOME ".codex\codex-router"
  }
}
$StateDir = [IO.Path]::GetFullPath($StateDir)

Write-Host "== 1/10  Codex Router =="
if (-not (Test-ModelRouter @("codex", "status"))) {
  throw "Codex Router is not installed/running from $RouterDir. Install it with .\install.ps1 -Target codex -Guided -WithTray, then rerun this script."
}

# This integration intentionally reuses the user's local Codex ChatGPT login.
# Upstream's discovery-disabled mode promises not to read that session at all,
# so it cannot coexist with chatgpt-session sharing. Enable discovery only for
# this local router state, and restart the service if we changed that setting.
$discovery = $null
try {
  $discovery = (& node.exe $discoveryMode status | Select-Object -Last 1 | ConvertFrom-Json).discovery
} catch {
  throw "Could not read Codex Router credential-discovery mode: $($_.Exception.Message)"
}
if ($discovery -ne "enabled") {
  Write-Host "Enabling Codex credential discovery required for ChatGPT session sharing."
  & node.exe $discoveryMode set enabled
  if ($LASTEXITCODE -ne 0) {
    throw "Could not enable Codex Router credential discovery."
  }
  & node.exe $routerService restart
  if ($LASTEXITCODE -ne 0) {
    throw "Codex Router could not restart after enabling credential discovery."
  }
}

Write-Host "== 2/10  Codex ChatGPT session =="
$codex = Get-Command codex.exe -ErrorAction SilentlyContinue
if ($codex) {
  & $codex.Source login status
  if ($LASTEXITCODE -ne 0) {
    throw "Codex is not logged in. Run 'codex login' once, then rerun this script."
  }
}
Invoke-ModelRouter @("codex", "chatgpt-session", "enable")

Write-Host "== 3/10  Jev generic provider =="
$genericProviders = Get-GenericProviders
$jevProviderExists = [bool]($genericProviders | Where-Object { $_.id -eq "jev" } | Select-Object -First 1)
$jevProviderAction = if ($jevProviderExists) { "edit" } else { "add" }
Invoke-ModelRouter @(
  "codex", "providers", "generic", $jevProviderAction, "jev",
  "--name", "Jev Router",
  "--base-url", "http://127.0.0.1:4319/v1",
  "--adapter", "openai-responses",
  "--allow-private"
)

Write-Host "== 4/10  Windows background service + daily eval =="
$serviceArgs = @(
  "-NoProfile", "-ExecutionPolicy", "Bypass",
  "-File", $installService,
  "-RepoRoot", $RepoRoot,
  "-JevEnvFile", $JevEnvFile,
  "-StateDir", $StateDir,
  "-RouterDir", $RouterDir
)
& powershell.exe @serviceArgs
if ($LASTEXITCODE -ne 0) {
  throw "Windows Jev service installation failed."
}

Write-Host "== 5/10  Provider discovery =="
if (-not (Wait-JevProviderDiscovery 6)) {
  $catalogOk = $false
  try {
    $catalog = Invoke-RestMethod -Uri "http://127.0.0.1:4319/v1/models" -TimeoutSec 3
    $catalogOk = @($catalog.data | Where-Object { $_.id -eq "auto" }).Count -gt 0
  } catch {}
  if ($catalogOk) {
    throw "Jev /v1/models is healthy, but Codex Router could not reach the generic provider after retries."
  }
  throw "Jev provider discovery failed because the local /v1/models endpoint is not staying reachable."
}
Write-Host "Jev provider discovery is reachable."

Write-Host "== 6/10  Curate jev/auto =="
& node.exe $curate jev --models auto --efforts low,medium,high,xhigh,max --apply
if ($LASTEXITCODE -ne 0) {
  throw "Curating jev/auto failed."
}

Write-Host "== 7/10  Keep native Codex picker while routing through Codex Router =="
Invoke-ModelRouter @("codex", "signed-routing", "on")

Write-Host "== 8/10  Install native-looking Auto toggle =="
$toggleArgs = @(
  "-NoProfile", "-ExecutionPolicy", "Bypass",
  "-File", $installToggle,
  "-RepoRoot", $RepoRoot,
  "-StateDir", $StateDir
)
& powershell.exe @toggleArgs
if ($LASTEXITCODE -ne 0) {
  throw "Auto toggle installation failed."
}

try {
  $autoStatus = Invoke-RestMethod -Uri "http://127.0.0.1:4319/control/status" -TimeoutSec 4
  if ($autoStatus.available -ne $true) {
    throw "Jev server cannot reach Codex Router control.mjs. Check CODEX_ROUTER_DIR."
  }
} catch {
  throw "Auto control readiness failed: $($_.Exception.Message)"
}

Write-Host "== 9/10  Full readiness =="
$env:JEV_ENV_FILE = $JevEnvFile
$env:CODEX_ROUTER_STATE_DIR = $StateDir
$env:CODEX_ROUTER_DIR = $RouterDir
$checkArgs = @($python.Prefix) + @($server, "--check")
& $python.Path @checkArgs
if ($LASTEXITCODE -ne 0) {
  throw "Full Jev readiness check failed."
}

Write-Host "== 10/10  Seed rolling Shadow Eval report =="
$shadowLog = Join-Path $StateDir "jev-shadow-eval.jsonl"
if (Test-Path -LiteralPath $shadowLog -PathType Leaf) {
  $reportArgs = @($python.Prefix) + @(
    $report, "--days", "7", "--log", $shadowLog, "--write",
    "--json-out", (Join-Path $StateDir "jev-shadow-eval-7d.json"),
    "--text-out", (Join-Path $StateDir "jev-shadow-eval-7d.txt")
  )
  & $python.Path @reportArgs
} else {
  Write-Host "No production turns yet; the first request will create the Shadow Eval log."
}

Write-Host ""
Write-Host "READY: Jev Codex Router + Auto toggle are installed for Windows."
Write-Host "Fully quit and reopen Codex Desktop once."
Write-Host "Keep using Codex native model + reasoning controls; the small Auto button beside them switches Jev routing on/off."
Write-Host "Auto OFF = native Codex selection. Auto ON = native requests redirect to jev/auto dynamically."
Write-Host ""
Write-Host "Shadow Eval:"
Write-Host "  raw log:   $StateDir\jev-shadow-eval.jsonl"
Write-Host "  7d text:   $StateDir\jev-shadow-eval-7d.txt"
Write-Host "  7d json:   $StateDir\jev-shadow-eval-7d.json"
Write-Host "  scheduled: daily at 03:15, StartWhenAvailable"
Write-Host ""
Write-Host "Manual report:"
Write-Host "  py -3 server\report_shadow_eval.py --days 7 --write"
