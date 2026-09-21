[CmdletBinding()]
param(
  [string]$TaskName = "Jev Codex Router",
  [string]$EvalTaskName = "Jev Codex Router Shadow Eval"
)

$ErrorActionPreference = "Stop"

foreach ($name in @($TaskName, $EvalTaskName)) {
  $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
  if ($task) {
    try { Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue } catch {}
    Unregister-ScheduledTask -TaskName $name -Confirm:$false
    Write-Host "Removed scheduled task: $name"
  }
}
