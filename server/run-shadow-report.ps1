[CmdletBinding()]
param(
  [string]$RepoRoot = (Split-Path -Parent $PSScriptRoot),
  [string]$StateDir = "",
  [int]$Days = 7
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
  throw "Python 3 was not found."
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

$log = Join-Path $StateDir "jev-shadow-eval.jsonl"
if (-not (Test-Path -LiteralPath $log -PathType Leaf)) {
  exit 0
}

$python = Resolve-Python
$report = Join-Path $RepoRoot "server\report_shadow_eval.py"
$jsonOut = Join-Path $StateDir "jev-shadow-eval-7d.json"
$textOut = Join-Path $StateDir "jev-shadow-eval-7d.txt"
$runLog = Join-Path $StateDir "jev-shadow-eval-report.log"

$argsList = @($python.Prefix) + @(
  $report,
  "--days", [string]$Days,
  "--log", $log,
  "--write",
  "--json-out", $jsonOut,
  "--text-out", $textOut
)

& $python.Path @argsList *>> $runLog
exit $LASTEXITCODE
