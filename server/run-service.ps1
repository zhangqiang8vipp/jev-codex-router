[CmdletBinding()]
param(
  [string]$RepoRoot = (Split-Path -Parent $PSScriptRoot),
  [string]$JevEnvFile = "",
  [string]$StateDir = "",
  [string]$RouterDir = ""
)

$ErrorActionPreference = "Stop"

function Test-RouterRoot([string]$Path) {
  if ([string]::IsNullOrWhiteSpace($Path)) { return $false }
  try { $root = [IO.Path]::GetFullPath($Path.Trim()) } catch { return $false }
  return Test-Path -LiteralPath (Join-Path $root "src\control.mjs") -PathType Leaf
}

function Resolve-RouterRoot([string]$Explicit, [string]$StateDirectory) {
  $candidates = New-Object System.Collections.Generic.List[string]
  if (-not [string]::IsNullOrWhiteSpace($Explicit)) { [void]$candidates.Add($Explicit) }
  if (-not [string]::IsNullOrWhiteSpace($env:CODEX_ROUTER_DIR)) {
    [void]$candidates.Add($env:CODEX_ROUTER_DIR)
  }

  $saved = Join-Path $StateDirectory "jev-router-dir.txt"
  if (Test-Path -LiteralPath $saved -PathType Leaf) {
    try {
      $value = (Get-Content -LiteralPath $saved -Raw -ErrorAction Stop).Trim()
      if ($value) { [void]$candidates.Add($value) }
    } catch {}
  }

  if ($env:LOCALAPPDATA) { [void]$candidates.Add((Join-Path $env:LOCALAPPDATA "codex-router")) }
  foreach ($candidate in @(
    (Join-Path $HOME "Documents\GitHub\codex-router"),
    (Join-Path $HOME "GitHub\codex-router"),
    (Join-Path $HOME "source\repos\codex-router"),
    (Join-Path $HOME "codex-router")
  )) {
    [void]$candidates.Add($candidate)
  }

  foreach ($candidate in $candidates) {
    if (Test-RouterRoot $candidate) {
      return [IO.Path]::GetFullPath($candidate.Trim())
    }
  }
  throw "Codex Router checkout could not be resolved. Re-run setup-local.ps1 once to persist RouterDir."
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
if ([string]::IsNullOrWhiteSpace($StateDir)) {
  $StateDir = if ($env:CODEX_ROUTER_STATE_DIR) {
    $env:CODEX_ROUTER_STATE_DIR
  } else {
    Join-Path $HOME ".codex\codex-router"
  }
}
$StateDir = [IO.Path]::GetFullPath($StateDir)
[void][IO.Directory]::CreateDirectory($StateDir)

if (-not [string]::IsNullOrWhiteSpace($JevEnvFile)) {
  $env:JEV_ENV_FILE = [IO.Path]::GetFullPath($JevEnvFile)
}
$env:CODEX_ROUTER_STATE_DIR = $StateDir
$RouterDir = Resolve-RouterRoot $RouterDir $StateDir
$env:CODEX_ROUTER_DIR = $RouterDir
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

$python = Resolve-Python
$server = Join-Path $RepoRoot "server\jev_server.py"
$patcher = Join-Path $RepoRoot "server\patch_codex_router.py"
if (-not (Test-Path -LiteralPath $server -PathType Leaf)) {
  throw "jev_server.py not found at $server"
}

$outLog = Join-Path $StateDir "jev-router.out.log"
$errLog = Join-Path $StateDir "jev-router.err.log"

# Prefer Codex Router's authenticated exact-route probe over temporarily moving
# native-redirect.json. The patch is a guarded one-line source change and only
# restarts Codex Router when an upstream update removed it. If the upstream
# source shape changes unexpectedly, keep Jev available on the legacy
# suppression fallback rather than turning a compatibility issue into downtime.
$exactRouteReady = $false
if (Test-Path -LiteralPath $patcher -PathType Leaf) {
  $patchArgs = @($python.Prefix) + @(
    $patcher,
    "--router-dir", $RouterDir,
    "--state-dir", $StateDir,
    "--restart"
  )
  & $python.Path @patchArgs 1>> $outLog 2>> $errLog
  $exactRouteReady = ($LASTEXITCODE -eq 0)
}
if ($exactRouteReady) {
  $env:JEV_EXACT_NATIVE_ROUTE = "1"
} else {
  Remove-Item Env:\JEV_EXACT_NATIVE_ROUTE -ErrorAction SilentlyContinue
  "[jev-router] scoped exact native route unavailable; using legacy redirect suppression fallback" |
    Out-File -LiteralPath $errLog -Append -Encoding utf8
}

$argsList = @($python.Prefix) + @($server)
& $python.Path @argsList 1>> $outLog 2>> $errLog
exit $LASTEXITCODE