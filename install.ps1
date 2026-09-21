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
$CodexRouterRepositoryUrl = "https://github.com/duolahypercho/codex-router.git"

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

function Test-ExcludedPythonPath([string]$Path) {
  if ([string]::IsNullOrWhiteSpace($Path)) { return $true }
  $normalized = $Path.Replace("/", "\").ToLowerInvariant()

  # The Windows Store aliases are placeholders rather than a Python runtime.
  if ($normalized -like "*\microsoft\windowsapps\python*.exe") { return $true }

  # For this repair path we deliberately need a non-uv CPython runtime because
  # the existing failure is inside the uv-managed Windows Python/OpenSSL stack.
  if ($normalized -like "*\uv\python\*") { return $true }

  return $false
}

function Add-PythonCandidate(
  [System.Collections.Generic.List[string]]$Candidates,
  [string]$Candidate
) {
  if ([string]::IsNullOrWhiteSpace($Candidate)) { return }
  try { $full = [IO.Path]::GetFullPath($Candidate.Trim()) } catch { return }
  if (Test-ExcludedPythonPath $full) { return }
  if (-not (Test-Path -LiteralPath $full -PathType Leaf)) { return }
  if (-not $Candidates.Contains($full)) { [void]$Candidates.Add($full) }
}

function Resolve-SystemPython {
  $candidates = New-Object System.Collections.Generic.List[string]

  $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
  if ($launcher) {
    foreach ($selector in @("-3.12", "-3")) {
      try {
        $candidate = (& $launcher.Source $selector -c "import sys; print(sys.executable)" 2>$null | Select-Object -Last 1)
        if ($LASTEXITCODE -eq 0) {
          Add-PythonCandidate $candidates $candidate
        }
      } catch {}
    }
  }

  foreach ($commandName in @("python.exe", "python3.exe")) {
    $command = Get-Command $commandName -ErrorAction SilentlyContinue
    if ($command) { Add-PythonCandidate $candidates $command.Source }
  }

  # winget's python.org package may not be on this PowerShell process PATH
  # immediately after install, so also inspect its standard per-user/system
  # installation roots.
  $roots = @()
  if (-not [string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
    $roots += (Join-Path $env:LOCALAPPDATA "Programs\Python")
  }
  if (-not [string]::IsNullOrWhiteSpace($env:ProgramFiles)) {
    $roots += $env:ProgramFiles
  }
  $programFilesX86 = [Environment]::GetEnvironmentVariable("ProgramFiles(x86)")
  if (-not [string]::IsNullOrWhiteSpace($programFilesX86)) {
    $roots += $programFilesX86
  }

  foreach ($root in $roots | Select-Object -Unique) {
    if (-not (Test-Path -LiteralPath $root -PathType Container)) { continue }
    try {
      Get-ChildItem -LiteralPath $root -Directory -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -like "Python3*" } |
        ForEach-Object {
          Add-PythonCandidate $candidates (Join-Path $_.FullName "python.exe")
        }
    } catch {}
  }

  foreach ($candidate in $candidates) {
    try {
      & $candidate -I -c "import sys; raise SystemExit(0 if sys.implementation.name == 'cpython' and sys.version_info >= (3, 10) else 1)" 2>$null
      if ($LASTEXITCODE -eq 0) {
        return $candidate
      }
    } catch {}
  }

  throw "A non-uv system CPython 3.10+ runtime was not found."
}

function Test-SystemPython {
  try {
    [void](Resolve-SystemPython)
    return $true
  } catch {
    return $false
  }
}

function Test-PythonOpenSsl([string]$Python) {
  if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) { return $false }
  try {
    & $Python -I -c "import ssl; ssl.create_default_context(); print(ssl.OPENSSL_VERSION)" 2>$null | Out-Null
    return $LASTEXITCODE -eq 0
  } catch {
    return $false
  }
}

function Get-CodexRouterLogPath {
  return Join-Path $HOME ".codex\codex-router\router.log"
}

function Get-OpenSslRepairMarkerPath {
  return Join-Path $HOME ".codex\codex-router\jev-openssl-repair-v2.json"
}

function Get-OpenSslRepairCheckpoint {
  $marker = Get-OpenSslRepairMarkerPath
  if (-not (Test-Path -LiteralPath $marker -PathType Leaf)) { return [int64]0 }
  try {
    $parsed = Get-Content -LiteralPath $marker -Raw | ConvertFrom-Json
    $value = [int64]$parsed.log_length
    if ($value -lt 0) { return [int64]0 }
    return $value
  } catch {
    return [int64]0
  }
}

function Record-OpenSslRepairCheckpoint {
  $log = Get-CodexRouterLogPath
  $marker = Get-OpenSslRepairMarkerPath
  $parent = Split-Path -Parent $marker
  [void][IO.Directory]::CreateDirectory($parent)
  $payload = @{
    version = 2
    log_length = (Get-FileLength $log)
    at = [DateTimeOffset]::Now.ToString("O")
  } | ConvertTo-Json -Compress
  [IO.File]::WriteAllText($marker, $payload + "`r`n", [Text.UTF8Encoding]::new($false))
}

function Test-HistoricalOpenSslCrash {
  $log = Get-CodexRouterLogPath
  if (-not (Test-Path -LiteralPath $log -PathType Leaf)) { return $false }
  $checkpoint = Get-OpenSslRepairCheckpoint
  $text = Get-AppendedUtf8Text $log $checkpoint
  return $text -match "no OPENSSL_Applink"
}

function Get-FileLength([string]$Path) {
  if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return [int64]0 }
  try { return [int64](Get-Item -LiteralPath $Path).Length } catch { return [int64]0 }
}

function Get-AppendedUtf8Text([string]$Path, [int64]$Offset) {
  if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return "" }
  try {
    $bytes = [IO.File]::ReadAllBytes($Path)
    if ($Offset -lt 0 -or $Offset -gt $bytes.LongLength) {
      $Offset = 0
    }
    if ($Offset -eq $bytes.LongLength) { return "" }
    $count = [int]($bytes.LongLength - $Offset)
    return [Text.Encoding]::UTF8.GetString($bytes, [int]$Offset, $count)
  } catch {
    return ""
  }
}

function Prepare-CodexRouterVenv([string]$Directory, [switch]$ForceRebuild) {
  $venv = Join-Path $Directory ".venv"
  $venvPython = Join-Path $venv "Scripts\python.exe"

  if (
    -not $ForceRebuild -and
    (Test-Path -LiteralPath $venvPython -PathType Leaf) -and
    (Test-PythonOpenSsl $venvPython)
  ) {
    return $false
  }

  $systemPython = Resolve-SystemPython
  if (-not (Test-PythonOpenSsl $systemPython)) {
    throw @"
The system Python runtime failed its OpenSSL self-test.
Check for SSLKEYLOGFILE or conflicting OpenSSL DLLs on PATH before retrying.
"@
  }

  if ($ForceRebuild) {
    Write-Step "Repairing Codex Router venv after a recorded OPENSSL_Applink crash"
  } else {
    Write-Step "Creating Codex Router venv with system Python"
  }

  if (Test-Path -LiteralPath $venv) {
    Remove-Item -LiteralPath $venv -Recurse -Force
  }

  & $systemPython -m venv $venv
  if ($LASTEXITCODE -ne 0 -or -not (Test-PythonOpenSsl $venvPython)) {
    throw "System Python could not create a working Codex Router virtual environment."
  }

  return $true
}

function Prepare-And-VerifyCodexRouterPython([string]$Directory, [bool]$ForceRepair) {
  $rebuilt = Prepare-CodexRouterVenv $Directory -ForceRebuild:$ForceRepair
  if (-not $rebuilt) { return }

  Write-Step "Installing locked Codex Router Python dependencies"
  $routerInstall = Join-Path $Directory "install.ps1"
  $prepareArgs = @(
    "-NoProfile",
    "-ExecutionPolicy", "Bypass",
    "-File", $routerInstall,
    "-CheckoutInstall",
    "-PrepareOnly",
    "-ForceDeps",
    "-Target", "codex"
  )
  & powershell.exe @prepareArgs
  if ($LASTEXITCODE -ne 0) {
    throw "Codex Router dependency preparation failed."
  }

  $venv = Join-Path $Directory ".venv"
  $venvPython = Join-Path $venv "Scripts\python.exe"
  $verify = Join-Path $Directory "scripts\verify-python-lock.py"
  if (-not (Test-Path -LiteralPath $verify -PathType Leaf)) {
    throw "Codex Router Python verification script is missing: $verify"
  }

  Write-Step "Smoke-testing LiteLLM before registering the Windows task"
  Push-Location $Directory
  try {
    # Emit one requirement per line instead of round-tripping a top-level
    # JSON array through Windows PowerShell 5.1. ConvertFrom-Json can preserve
    # that array as a single nested object; casting it to [string] then joins
    # its elements with spaces and turns two requirements into one argv value.
    $requirements = @(
      @(
        & node.exe -e "import('./src/install-plan.mjs').then(m=>m.PYTHON_REQUIREMENTS.forEach(x=>console.log(x)))" 2>$null
      ) | ForEach-Object { "$_".Trim() } | Where-Object { $_ }
    )
    if ($LASTEXITCODE -ne 0 -or $requirements.Count -eq 0) {
      throw "Could not read Codex Router's pinned Python requirements."
    }

    $verifyArgs = @($verify, "--venv", $venv, "--proxy-timeout", "90")
    foreach ($requirement in $requirements) {
      $verifyArgs += @("--requirement", $requirement)
    }

    & $venvPython @verifyArgs
    if ($LASTEXITCODE -ne 0) {
      throw "LiteLLM smoke test failed before service installation."
    }
  } finally {
    Pop-Location
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

function Test-RouterCheckout([string]$Directory) {
  if ([string]::IsNullOrWhiteSpace($Directory)) { return $false }
  try { $root = [IO.Path]::GetFullPath($Directory) } catch { return $false }
  return (
    (Test-Path -LiteralPath (Join-Path $root "model-router.ps1") -PathType Leaf) -and
    (Test-Path -LiteralPath (Join-Path $root "src\control.mjs") -PathType Leaf)
  )
}

function Install-CodexRouterCheckout {
  $localAppData = if (-not [string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
    $env:LOCALAPPDATA
  } else {
    Join-Path $HOME "AppData\Local"
  }
  $target = Join-Path $localAppData "codex-router"

  if (-not (Test-Path -LiteralPath $target)) {
    Write-Step "Codex Router not found; cloning the base router automatically"
    & git clone --depth 1 $CodexRouterRepositoryUrl $target
    if ($LASTEXITCODE -ne 0) {
      throw "Unable to clone Codex Router."
    }
  }

  if (-not (Test-RouterCheckout $target)) {
    throw "$target exists but is not a valid Codex Router checkout."
  }

  return [IO.Path]::GetFullPath($target)
}

function Ensure-CodexRouterBaseInstalled([string]$Directory) {
  $forceOpenSslRepair = Test-HistoricalOpenSslCrash
  Prepare-And-VerifyCodexRouterPython $Directory $forceOpenSslRepair

  # Install the upstream router in credential-free idle mode. When an old
  # OPENSSL_Applink crash was present, the venv has already been rebuilt and
  # the LiteLLM proxy has been boot-tested above.
  Write-Step "Installing/repairing the Codex Router base service"
  $routerInstall = Join-Path $Directory "install.ps1"
  $log = Get-CodexRouterLogPath
  $logOffset = Get-FileLength $log

  $routerInstallArgs = @(
    "-NoProfile",
    "-ExecutionPolicy", "Bypass",
    "-File", $routerInstall,
    "-Target", "codex",
    "-NoProvider",
    "-NoDiscovery",
    "-NoTray"
  )
  & powershell.exe @routerInstallArgs

  if ($LASTEXITCODE -ne 0) {
    $newLog = Get-AppendedUtf8Text $log $logOffset
    if ($newLog -match "no OPENSSL_Applink") {
      throw "This install attempt still hit OPENSSL_Applink after rebuilding and smoke-testing the system-Python venv."
    }
    if ($forceOpenSslRepair) {
      Record-OpenSslRepairCheckpoint
    }
    throw "Codex Router installer exited with status $LASTEXITCODE. The failure was not a new OPENSSL_Applink crash."
  }

  if ($forceOpenSslRepair) {
    Record-OpenSslRepairCheckpoint
  }
}

function Resolve-RouterCheckout([string]$Explicit) {
  if (-not [string]::IsNullOrWhiteSpace($Explicit)) {
    if (-not (Test-RouterCheckout $Explicit)) {
      throw "The supplied RouterDir is not a Codex Router checkout: $Explicit"
    }
    return [IO.Path]::GetFullPath($Explicit)
  }

  if (-not [string]::IsNullOrWhiteSpace($env:CODEX_ROUTER_DIR)) {
    if (-not (Test-RouterCheckout $env:CODEX_ROUTER_DIR)) {
      throw "CODEX_ROUTER_DIR is set but is not a Codex Router checkout: $env:CODEX_ROUTER_DIR"
    }
    return [IO.Path]::GetFullPath($env:CODEX_ROUTER_DIR)
  }

  $localAppData = if (-not [string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
    $env:LOCALAPPDATA
  } else {
    Join-Path $HOME "AppData\Local"
  }

  foreach ($candidate in @(
    (Join-Path $localAppData "codex-router"),
    (Join-Path $HOME "Documents\GitHub\codex-router"),
    (Join-Path $HOME "GitHub\codex-router"),
    (Join-Path $HOME "source\repos\codex-router"),
    (Join-Path $HOME "codex-router")
  )) {
    if (Test-RouterCheckout $candidate) {
      return [IO.Path]::GetFullPath($candidate)
    }
  }

  return Install-CodexRouterCheckout
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

  $lines = if (Test-Path -LiteralPath $path -PathType Leaf) {
    @(Get-Content -LiteralPath $path)
  } else {
    @()
  }
  $replacement = "TYPESAFE_API_KEY=$($plain.Trim())"
  $replaced = $false
  $updated = foreach ($line in $lines) {
    if (-not $replaced -and $line -match '^\s*TYPESAFE_API_KEY\s*=') {
      $replaced = $true
      $replacement
    } else {
      $line
    }
  }
  if (-not $replaced) {
    $updated = @($updated) + $replacement
  }

  $temp = "$path.tmp-$([Guid]::NewGuid().ToString("N"))"
  try {
    [IO.File]::WriteAllLines($temp, [string[]]@($updated), [Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $temp -Destination $path -Force
  } finally {
    if (Test-Path -LiteralPath $temp) {
      Remove-Item -LiteralPath $temp -Force -ErrorAction SilentlyContinue
    }
  }

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

function Start-ExistingJevTasks {
  foreach ($name in @("Jev Codex Router", "Jev Codex Auto Toggle")) {
    try {
      if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
        Start-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
      }
    } catch {}
  }
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

    try {
      if (Test-Path -LiteralPath $backupPath) {
        Remove-Item -LiteralPath $backupPath -Recurse -Force
      }
      if (Test-Path -LiteralPath $Destination) {
        Move-Item -LiteralPath $Destination -Destination $backupPath
      }
      Move-Item -LiteralPath $source.FullName -Destination $Destination
    } catch {
      try {
        if ((-not (Test-Path -LiteralPath $Destination)) -and (Test-Path -LiteralPath $backupPath)) {
          Move-Item -LiteralPath $backupPath -Destination $Destination
        }
      } finally {
        Start-ExistingJevTasks
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
    Start-ExistingJevTasks
    Write-Warning "Restored the previous Jev Codex Router source tree after setup failed."
  } catch {
    Write-Warning "Setup failed and the previous source tree could not be restored automatically: $($_.Exception.Message)"
  }
}

Write-Host ""
Write-Host "Jev Codex Router bootstrap installer" -ForegroundColor Green
Write-Host "Repository: https://github.com/$RepoOwner/$RepoName"

Ensure-Dependency "Git" { [bool](Get-Command git.exe -ErrorAction SilentlyContinue) } "Git.Git" "Install Git for Windows and rerun."
Ensure-Dependency "Node.js" { [bool](Get-Command node.exe -ErrorAction SilentlyContinue) } "OpenJS.NodeJS.LTS" "Install Node.js LTS and rerun."
Ensure-Dependency "CPython 3.10+" { Test-SystemPython } "Python.Python.3.12" "Install the python.org CPython 3.12 package and rerun."
Ensure-Dependency ".NET 8 SDK" { Test-DotNet8 } "Microsoft.DotNet.SDK.8" "Install the .NET 8 SDK and rerun."

$RouterDir = Resolve-RouterCheckout $RouterDir
Ensure-CodexRouterBaseInstalled $RouterDir
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
