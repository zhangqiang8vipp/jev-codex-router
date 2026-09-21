[CmdletBinding()]
param(
  [string]$RouterDir = "",
  [string]$InstallDir = "",
  [string]$JevEnvFile = "",
  [string]$StateDir = "",
  [string]$Branch = "main",
  [switch]$NoPrompt
)

Set-StrictMode -Version 3.0
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$RepoOwner = "zhangqiang8vipp"
$RepoName = "jev-codex-router"
$ArchiveUrl = "https://github.com/$RepoOwner/$RepoName/archive/refs/heads/$Branch.zip"

if ($env:OS -ne "Windows_NT") {
  throw "This bootstrap installer is for Windows PowerShell. On macOS/Linux use setup-local.sh."
}

try {
  [Net.ServicePointManager]::SecurityProtocol =
    [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
} catch {}

function Write-Step([string]$Message) {
  Write-Host ""
  Write-Host "==> $Message" -ForegroundColor Cyan
}

function Refresh-ProcessPath {
  $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
  $user = [Environment]::GetEnvironmentVariable("Path", "User")
  $env:Path = (($machine, $user) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }) -join ";"
}

function Confirm-Yes([string]$Prompt, [bool]$DefaultYes = $true) {
  if ($NoPrompt) { return $false }
  $suffix = if ($DefaultYes) { "[Y/n]" } else { "[y/N]" }
  $answer = Read-Host "$Prompt $suffix"
  if ([string]::IsNullOrWhiteSpace($answer)) { return $DefaultYes }
  return $answer.Trim().ToLowerInvariant() -in @("y", "yes")
}

function Test-DotNet8 {
  $dotnet = Get-Command dotnet.exe -ErrorAction SilentlyContinue
  if (-not $dotnet) { return $false }
  try {
    $sdks = & $dotnet.Source --list-sdks 2>$null
    return [bool]($sdks | Where-Object { $_ -match '^8\.' })
  } catch {
    return $false
  }
}

function Ensure-Dependency(
  [string]$Label,
  [scriptblock]$Probe,
  [string]$WingetId,
  [string]$ManualHint
) {
  if (& $Probe) { return }

  $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
  if (-not $winget) {
    throw "$Label is required. $ManualHint"
  }

  if (-not (Confirm-Yes "$Label is missing. Install it with winget now?")) {
    throw "$Label is required. $ManualHint"
  }

  Write-Step "Installing $Label"
  & $winget.Source install --id $WingetId -e --accept-package-agreements --accept-source-agreements
  if ($LASTEXITCODE -ne 0) {
    throw "winget could not install $Label (package: $WingetId). $ManualHint"
  }

  Refresh-ProcessPath
  if (-not (& $Probe)) {
    throw "$Label was installed but is not visible in this PowerShell session. Open a new PowerShell window and rerun the installer."
  }
}

function Resolve-RouterCheckout([string]$Explicit) {
  $candidates = New-Object System.Collections.Generic.List[string]

  if (-not [string]::IsNullOrWhiteSpace($Explicit)) { [void]$candidates.Add($Explicit) }
  if (-not [string]::IsNullOrWhiteSpace($env:CODEX_ROUTER_DIR)) { [void]$candidates.Add($env:CODEX_ROUTER_DIR) }

  foreach ($candidate in @(
    (Join-Path $HOME "Documents\GitHub\codex-router"),
    (Join-Path $HOME "GitHub\codex-router"),
    (Join-Path $HOME "source\repos\codex-router"),
    (Join-Path $HOME "codex-router")
  )) { [void]$candidates.Add($candidate) }

  foreach ($candidate in $candidates) {
    if ([string]::IsNullOrWhiteSpace($candidate)) { continue }
    try { $root = [IO.Path]::GetFullPath($candidate) } catch { continue }
    if (
      (Test-Path -LiteralPath (Join-Path $root "model-router.ps1") -PathType Leaf) -and
      (Test-Path -LiteralPath (Join-Path $root "src\control.mjs") -PathType Leaf)
    ) { return $root }
  }

  if (-not $NoPrompt) {
    $typed = Read-Host "Codex Router checkout path (folder containing model-router.ps1)"
    if (-not [string]::IsNullOrWhiteSpace($typed)) {
      $root = [IO.Path]::GetFullPath($typed)
      if (
        (Test-Path -LiteralPath (Join-Path $root "model-router.ps1") -PathType Leaf) -and
        (Test-Path -LiteralPath (Join-Path $root "src\control.mjs") -PathType Leaf)
      ) { return $root }
    }
  }

  throw "Codex Router checkout was not found. Set CODEX_ROUTER_DIR to the folder containing model-router.ps1, then rerun."
}

function Protect-KeyFile([string]$Path) {
  try {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    & icacls.exe $Path /inheritance:r /grant:r "${identity}:(R,W)" | Out-Null
  } catch {
    Write-Warning "Could not tighten the key-file ACL automatically: $($_.Exception.Message)"
  }
}

function Ensure-JevKeyFile([string]$Explicit) {
  $path = $Explicit
  if ([string]::IsNullOrWhiteSpace($path)) {
    $path = if (-not [string]::IsNullOrWhiteSpace($env:JEV_ENV_FILE)) {
      $env:JEV_ENV_FILE
    } else {
      Join-Path $HOME ".hermes\.env"
    }
  }
  $path = [IO.Path]::GetFullPath($path)

  if (Test-Path -LiteralPath $path -PathType Leaf) {
    $hasKey = Select-String -LiteralPath $path -Pattern '^\s*TYPESAFE_API_KEY\s*=\s*\S+' -Quiet
    if ($hasKey) {
      Protect-KeyFile $path
      return $path
    }
  }

  $plain = $null
  if (-not [string]::IsNullOrWhiteSpace($env:TYPESAFE_API_KEY)) {
    $plain = $env:TYPESAFE_API_KEY.Trim()
  } elseif ($NoPrompt) {
    throw "TypeSafe/Jev key is missing. Set TYPESAFE_API_KEY or create $path."
  } else {
    Write-Host ""
    Write-Host "TypeSafe/Jev API key is required." -ForegroundColor Yellow
    Write-Host "The key is read locally and is not printed or sent through command arguments."
    $secure = Read-Host "TypeSafe API key" -AsSecureString
    if ($secure.Length -eq 0) { throw "TypeSafe/Jev key cannot be empty." }
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
      $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
    } finally {
      [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)
    }
  }

  if ([string]::IsNullOrWhiteSpace($plain)) { throw "TypeSafe/Jev key cannot be empty." }

  $parent = Split-Path -Parent $path
  [void][IO.Directory]::CreateDirectory($parent)
  [IO.File]::WriteAllText(
    $path,
    "TYPESAFE_API_KEY=$($plain.Trim())`r`n",
    [Text.UTF8Encoding]::new($false)
  )
  Protect-KeyFile $path
  return $path
}

function Stop-ExistingJevTasks {
  foreach ($name in @("Jev Codex Auto Toggle", "Jev Codex Router")) {
    try {
      if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
      }
    } catch {}
  }
  Start-Sleep -Milliseconds 400
}

function Install-SourceTree([string]$Destination) {
  $parent = Split-Path -Parent $Destination
  [void][IO.Directory]::CreateDirectory($parent)

  $tempRoot = Join-Path ([IO.Path]::GetTempPath()) ("jev-codex-router-" + [Guid]::NewGuid().ToString("N"))
  $zipPath = Join-Path $tempRoot "source.zip"
  $extractPath = Join-Path $tempRoot "extract"
  $backupPath = "$Destination.previous"
  [void][IO.Directory]::CreateDirectory($tempRoot)

  try {
    Write-Step "Downloading Jev Codex Router ($Branch)"
    Invoke-WebRequest -Uri $ArchiveUrl -OutFile $zipPath
    Expand-Archive -LiteralPath $zipPath -DestinationPath $extractPath -Force

    $source = Get-ChildItem -LiteralPath $extractPath -Directory | Select-Object -First 1
    if (-not $source) { throw "Downloaded archive did not contain a source directory." }
    if (-not (Test-Path -LiteralPath (Join-Path $source.FullName "setup-local.ps1") -PathType Leaf)) {
      throw "Downloaded archive is missing setup-local.ps1."
    }

    Stop-ExistingJevTasks

    if (Test-Path -LiteralPath $backupPath) { Remove-Item -LiteralPath $backupPath -Recurse -Force }
    if (Test-Path -LiteralPath $Destination) { Move-Item -LiteralPath $Destination -Destination $backupPath }

    try {
      Move-Item -LiteralPath $source.FullName -Destination $Destination
    } catch {
      if ((-not (Test-Path -LiteralPath $Destination)) -and (Test-Path -LiteralPath $backupPath)) {
        Move-Item -LiteralPath $backupPath -Destination $Destination
      }
      throw
    }

    return [pscustomobject]@{
      Path = $Destination
      Backup = if (Test-Path -LiteralPath $backupPath) { $backupPath } else { $null }
    }
  } finally {
    if (Test-Path -LiteralPath $tempRoot) {
      Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
  }
}

function Restore-SourceTree($InstallResult) {
  if (-not $InstallResult -or [string]::IsNullOrWhiteSpace($InstallResult.Backup)) { return }
  try {
    if (Test-Path -LiteralPath $InstallResult.Path) { Remove-Item -LiteralPath $InstallResult.Path -Recurse -Force }
    Move-Item -LiteralPath $InstallResult.Backup -Destination $InstallResult.Path
    Write-Warning "Restored the previous Jev Codex Router source tree after setup failed."
  } catch {
    Write-Warning "Setup failed and the previous source tree could not be restored automatically: $($_.Exception.Message)"
  }
}

Write-Host ""
Write-Host "Jev Codex Router bootstrap installer" -ForegroundColor Green
Write-Host "Repository: https://github.com/$RepoOwner/$RepoName"

Ensure-Dependency "Node.js" { [bool](Get-Command node.exe -ErrorAction SilentlyContinue) } "OpenJS.NodeJS.LTS" "Install Node.js LTS and rerun."
Ensure-Dependency "Python 3" { [bool](Get-Command py.exe -ErrorAction SilentlyContinue) -or [bool](Get-Command python.exe -ErrorAction SilentlyContinue) } "Python.Python.3.12" "Install Python 3.11+ and rerun."
Ensure-Dependency ".NET 8 SDK" { Test-DotNet8 } "Microsoft.DotNet.SDK.8" "Install the .NET 8 SDK and rerun."

$RouterDir = Resolve-RouterCheckout $RouterDir
$JevEnvFile = Ensure-JevKeyFile $JevEnvFile

if ([string]::IsNullOrWhiteSpace($InstallDir)) {
  $localAppData = if (-not [string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) { $env:LOCALAPPDATA } else { Join-Path $HOME "AppData\Local" }
  $InstallDir = Join-Path $localAppData "JevCodexRouter\source"
}
$InstallDir = [IO.Path]::GetFullPath($InstallDir)
if (-not [string]::IsNullOrWhiteSpace($StateDir)) { $StateDir = [IO.Path]::GetFullPath($StateDir) }

$install = Install-SourceTree $InstallDir
$setup = Join-Path $InstallDir "setup-local.ps1"

Write-Step "Running local setup"
$setupArgs = @(
  "-NoProfile",
  "-ExecutionPolicy", "Bypass",
  "-File", $setup,
  "-RouterDir", $RouterDir,
  "-JevEnvFile", $JevEnvFile
)
if (-not [string]::IsNullOrWhiteSpace($StateDir)) { $setupArgs += @("-StateDir", $StateDir) }

try {
  & powershell.exe @setupArgs
  if ($LASTEXITCODE -ne 0) { throw "setup-local.ps1 exited with status $LASTEXITCODE." }
} catch {
  Restore-SourceTree $install
  throw
}

if ($install.Backup -and (Test-Path -LiteralPath $install.Backup)) {
  Remove-Item -LiteralPath $install.Backup -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Step "Installed"
Write-Host "Source:       $InstallDir"
Write-Host "Codex Router: $RouterDir"
Write-Host "Key file:     $JevEnvFile"

try {
  $status = Invoke-RestMethod -Uri "http://127.0.0.1:4319/control/status" -TimeoutSec 4
  Write-Host "Auto control: available=$($status.available) auto=$($status.auto)"
} catch {
  Write-Warning "Install completed, but Auto status could not be queried yet: $($_.Exception.Message)"
}

Write-Host ""
Write-Host "Usage:" -ForegroundColor Green
Write-Host "  Open Codex Desktop and keep using its native model + reasoning controls."
Write-Host "  Click the small Auto pill beside the reasoning control:"
Write-Host "    Auto OFF -> Codex native selection"
Write-Host "    Auto ON  -> Jev dynamically selects model + effort"
Write-Host ""
Write-Host "You may fully quit and reopen Codex Desktop once after first install."
