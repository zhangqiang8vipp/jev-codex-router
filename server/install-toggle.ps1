[CmdletBinding()]
param(
  [string]$RepoRoot = (Split-Path -Parent $PSScriptRoot),
  [string]$StateDir = "",
  [string]$TaskName = "Jev Codex Auto Toggle"
)

$ErrorActionPreference = "Stop"

if ($env:OS -ne "Windows_NT") {
  throw "server/install-toggle.ps1 is for Windows only."
}

function Get-AutoToggleProcesses([string]$Directory) {
  $root = ([IO.Path]::GetFullPath($Directory)).TrimEnd("\") + "\"
  try {
    return @(
      Get-CimInstance Win32_Process -Filter "Name = 'JevCodexAutoToggle.exe'" -ErrorAction SilentlyContinue |
        Where-Object {
          $candidate = [string]$_.ExecutablePath
          if ([string]::IsNullOrWhiteSpace($candidate)) {
            $candidate = [string]$_.CommandLine
          }
          if ([string]::IsNullOrWhiteSpace($candidate)) { return $false }

          try {
            $full = [IO.Path]::GetFullPath($candidate.Trim('"'))
            return $full.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)
          } catch {
            return ([string]$_.CommandLine).IndexOf($root, [StringComparison]::OrdinalIgnoreCase) -ge 0
          }
        }
    )
  } catch {
    return @()
  }
}

function Stop-AutoToggleProcesses([string]$Directory, [int]$TimeoutSeconds = 10) {
  foreach ($process in @(Get-AutoToggleProcesses $Directory)) {
    try {
      Stop-Process -Id ([int]$process.ProcessId) -Force -ErrorAction SilentlyContinue
    } catch {}
  }

  $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
  do {
    $remaining = @(Get-AutoToggleProcesses $Directory)
    if ($remaining.Count -eq 0) { return }
    Start-Sleep -Milliseconds 250
  } while ((Get-Date) -lt $deadline)

  $ids = (@(Get-AutoToggleProcesses $Directory) | ForEach-Object { $_.ProcessId }) -join ","
  throw "The previous Jev Auto Toggle process did not exit (PID(s): $ids)."
}

function Remove-DirectoryWithRetry([string]$Path, [int]$Attempts = 8) {
  for ($i = 0; $i -lt $Attempts; $i++) {
    if (-not (Test-Path -LiteralPath $Path)) { return }
    try {
      Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction Stop
      return
    } catch {
      if ($i -eq $Attempts - 1) { throw }
      Start-Sleep -Milliseconds (250 * ($i + 1))
    }
  }
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

$dotnet = Get-Command dotnet.exe -ErrorAction SilentlyContinue
if (-not $dotnet) {
  throw "The .NET 8 SDK is required to build the Auto toggle. Install Microsoft .NET SDK 8 and rerun setup-local.ps1."
}

$project = Join-Path $RepoRoot "desktop\JevAutoToggle\JevAutoToggle.csproj"
if (-not (Test-Path -LiteralPath $project -PathType Leaf)) {
  throw "Auto toggle project not found: $project"
}

$rid = if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64") { "win-arm64" } else { "win-x64" }
$publishDir = Join-Path $StateDir "jev-auto-toggle"
$tempDir = Join-Path $StateDir ("jev-auto-toggle.publish-" + [Guid]::NewGuid().ToString("N"))

try {
  & $dotnet.Source publish $project -c Release -r $rid --self-contained true -p:PublishSingleFile=false -o $tempDir
  if ($LASTEXITCODE -ne 0) {
    throw "dotnet publish failed with status $LASTEXITCODE."
  }

  $exe = Join-Path $tempDir "JevCodexAutoToggle.exe"
  if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) {
    throw "Published Auto toggle executable was not found: $exe"
  }

  $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
  if ($existing) {
    try { Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue } catch {}
    # Unregister before replacing files so the minute heartbeat cannot relaunch
    # the old executable while its WPF resource assemblies are being removed.
    try { Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue } catch {}
  }

  # WPF loads satellite resource DLLs (for example zh-Hans PresentationCore)
  # into the overlay process. Windows keeps those files locked until the process
  # exits; stopping the scheduled task alone is not always enough.
  Stop-AutoToggleProcesses $publishDir 10

  if (Test-Path -LiteralPath $publishDir) {
    Remove-DirectoryWithRetry $publishDir 8
  }
  Move-Item -LiteralPath $tempDir -Destination $publishDir

  $exe = Join-Path $publishDir "JevCodexAutoToggle.exe"
  $action = New-ScheduledTaskAction -Execute $exe
  $user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
  $logon = New-ScheduledTaskTrigger -AtLogOn -User $user
  $heartbeatParams = @{
    Once = $true
    At = (Get-Date).AddMinutes(1)
    RepetitionInterval = (New-TimeSpan -Minutes 1)
    RepetitionDuration = (New-TimeSpan -Days 3650)
  }
  $heartbeat = New-ScheduledTaskTrigger @heartbeatParams
  $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -StartWhenAvailable
  $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited

  Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger @($logon, $heartbeat) -Settings $settings -Principal $principal -Force | Out-Null
  Start-ScheduledTask -TaskName $TaskName

  Write-Host "Auto toggle installed: $exe"
  Write-Host "Scheduled task: $TaskName"
}
finally {
  if (Test-Path -LiteralPath $tempDir) {
    Remove-Item -LiteralPath $tempDir -Recurse -Force -ErrorAction SilentlyContinue
  }
}
