[CmdletBinding()]
param(
  [string]$TaskName = "Jev Codex Router",
  [string]$EvalTaskName = "Jev Codex Router Shadow Eval",
  [string]$ToggleTaskName = "Jev Codex Auto Toggle",
  [string]$StateDir = ""
)

$ErrorActionPreference = "Stop"

try {
  $body = @{ enabled = $false } | ConvertTo-Json -Compress
  Invoke-RestMethod -Uri "http://127.0.0.1:4319/control/auto" -Method Post -ContentType "application/json" -Body $body -TimeoutSec 4 | Out-Null
} catch {
  Write-Warning "Could not switch Auto off before uninstall. If native redirect stays active, clear it from Codex Router manually."
}

foreach ($name in @($ToggleTaskName, $EvalTaskName, $TaskName)) {
  $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
  if ($task) {
    try { Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue } catch {}
    Unregister-ScheduledTask -TaskName $name -Confirm:$false
    Write-Host "Removed scheduled task: $name"
  }
}

if ([string]::IsNullOrWhiteSpace($StateDir)) {
  $StateDir = if ($env:CODEX_ROUTER_STATE_DIR) {
    $env:CODEX_ROUTER_STATE_DIR
  } else {
    Join-Path $HOME ".codex\codex-router"
  }
}
$toggleDir = Join-Path ([IO.Path]::GetFullPath($StateDir)) "jev-auto-toggle"
if (Test-Path -LiteralPath $toggleDir) {
  Remove-Item -LiteralPath $toggleDir -Recurse -Force -ErrorAction SilentlyContinue
}
