[CmdletBinding()]
param(
  [string]$RepoRoot = (Split-Path -Parent $PSScriptRoot),
  [string]$JevEnvFile = "",
  [string]$StateDir = "",
  [string]$RouterDir = ""
)

$ErrorActionPreference = "Stop"

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
if (-not [string]::IsNullOrWhiteSpace($RouterDir)) {
  $env:CODEX_ROUTER_DIR = [IO.Path]::GetFullPath($RouterDir)
}
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

$python = Resolve-Python
$server = Join-Path $RepoRoot "server\jev_server.py"
if (-not (Test-Path -LiteralPath $server -PathType Leaf)) {
  throw "jev_server.py not found at $server"
}

$outLog = Join-Path $StateDir "jev-router.out.log"
$errLog = Join-Path $StateDir "jev-router.err.log"
$argsList = @($python.Prefix) + @($server)

& $python.Path @argsList 1>> $outLog 2>> $errLog
exit $LASTEXITCODE
