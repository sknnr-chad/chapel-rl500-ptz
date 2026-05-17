#requires -RunAsAdministrator
<#
.SYNOPSIS
  One-shot installer for pew-ptz on Windows.

.DESCRIPTION
  - Ensures Python 3.14 (winget: Python.Python.3.14)
  - Creates <InstallDir>\.venv and installs the package
  - Adds a Windows Firewall rule for inbound TCP <Port> (Private profile)
  - Registers a Scheduled Task that runs at logon of the operator user, with
    restart-on-failure, launched via pythonw.exe so no console window appears
  - Drops a desktop shortcut to http://localhost:<Port>
  - Starts the task immediately so you can verify

  Re-run safely: each step is idempotent.

  Defaults match the chapel deployment (C:\tools\pew-ptz, user "Chapel-AV").
  Override with -InstallDir / -TaskUser / -Port for your own deployment.

.PARAMETER InstallDir
  Where the app lives. Default: C:\tools\pew-ptz

.PARAMETER TaskUser
  The local user account that auto-logs into the host PC. Default: Chapel-AV

.PARAMETER Port
  HTTP port the controller binds to. Default: 8080

.PARAMETER TaskName
  Name shown in Task Scheduler. Default: "Pew PTZ Controller"

.PARAMETER PinSetup
  How to handle PIN setup:
    Auto  (default) — prompt for both PINs on first install; on re-install,
                      offer a Keep/Replace menu.
    Skip            — never prompt. Errors out if no PIN store exists yet.
    Force           — always re-prompt for both PINs, even if a store exists.
                      Also re-enables auth if a previous install was open.

.PARAMETER NoAuth
  Run open — no PIN lock, every phone on the LAN gets full access. Use on
  trusted home LANs where you don't want a login screen. Subsequent runs
  remember this choice (detected by reading the existing launch.cmd) so
  you don't have to re-pass the flag. Switch back to PINs with -PinSetup Force.
#>
[CmdletBinding()]
param(
  [string]$InstallDir = "C:\tools\pew-ptz",
  [string]$TaskUser   = "Chapel-AV",
  [int]   $Port       = 8080,
  [string]$TaskName   = "Pew PTZ Controller",
  [ValidateSet("Auto","Skip","Force")]
  [string]$PinSetup   = "Auto",
  [switch]$NoAuth
)

$ErrorActionPreference = "Stop"
$ProgressPreference    = "SilentlyContinue"

function Write-Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-OK  ($msg) { Write-Host "    $msg" -ForegroundColor Green }
function Write-Note($msg) { Write-Host "    $msg" -ForegroundColor DarkGray }

# ---- Sanity checks -------------------------------------------------------
Write-Step "Verifying environment"
if (-not (Test-Path $InstallDir)) {
  throw "InstallDir '$InstallDir' does not exist. Copy/clone the repo there first."
}
if (-not (Test-Path (Join-Path $InstallDir "pyproject.toml"))) {
  throw "pyproject.toml not found in $InstallDir. Are you sure the repo is here?"
}
Write-OK "Repo found at $InstallDir"

try { Get-LocalUser -Name $TaskUser | Out-Null; Write-OK "User '$TaskUser' exists" }
catch { throw "Local user '$TaskUser' not found. Create it first or pass -TaskUser." }

# ---- Python 3.14 via winget ---------------------------------------------
Write-Step "Ensuring Python 3.14 is installed (via winget)"
$py314 = $null
try { $py314 = (& py -3.14 -c "import sys; print(sys.executable)" 2>$null) } catch { }
if (-not $py314) {
  Write-Note "Python 3.14 not found, installing via winget..."
  & winget install --id Python.Python.3.14 -e --source winget `
    --accept-source-agreements --accept-package-agreements --silent
  if ($LASTEXITCODE -ne 0) { throw "winget install of Python.Python.3.14 failed (exit $LASTEXITCODE)" }
  $py314 = (& py -3.14 -c "import sys; print(sys.executable)" 2>$null)
  if (-not $py314) { throw "Python 3.14 installed but 'py -3.14' still cannot find it. Open a new shell and re-run." }
}
Write-OK "Python 3.14: $py314"

# ---- Create venv + install package --------------------------------------
$venvDir    = Join-Path $InstallDir ".venv"
$venvPython = Join-Path $venvDir "Scripts\python.exe"
$venvPyw    = Join-Path $venvDir "Scripts\pythonw.exe"
$logDir     = Join-Path $InstallDir "logs"

Write-Step "Creating virtual environment at $venvDir"
if (-not (Test-Path $venvPython)) {
  & $py314 -m venv $venvDir
  if ($LASTEXITCODE -ne 0) { throw "venv creation failed" }
  Write-OK "venv created"
} else {
  Write-OK "venv already exists"
}

Write-Step "Stopping any running instance before pip install"
try { Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue } catch { }
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" -ErrorAction SilentlyContinue |
  Where-Object { $_.CommandLine -match "pew_ptz" } |
  ForEach-Object {
    Write-Note "Killing PID $($_.ProcessId)"
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
  }
Start-Sleep -Milliseconds 400

Write-Step "Installing pew-ptz into venv"
& $venvPython -m pip install --upgrade pip --disable-pip-version-check | Out-Null
& $venvPython -m pip install --upgrade --disable-pip-version-check $InstallDir
if ($LASTEXITCODE -ne 0) { throw "pip install of pew-ptz failed" }
Write-OK "Package installed"

# ---- Authentication setup -------------------------------------------------
# Two layers of choice:
#   1. Whether to enable PIN auth at all (-NoAuth or interactive prompt)
#   2. If enabled, what to do about the actual PINs (-PinSetup Auto/Skip/Force)
# The auth-on/off decision is preserved across re-runs by reading the
# existing launch.cmd. PINs themselves live in <InstallDir>\auth.json — the
# auth CLI owns the prompt so PowerShell never sees plaintext.
Write-Step "Configuring authentication"
$authJson  = Join-Path $InstallDir "auth.json"
$pinExists = Test-Path $authJson
$launcher  = Join-Path $InstallDir "scripts\launch.cmd"
$previouslyNoAuth = $false
if (Test-Path $launcher) {
  $previouslyNoAuth = ((Get-Content $launcher -Raw -ErrorAction SilentlyContinue) -match 'PEW_PTZ_AUTH_DISABLED=1')
}
$env:PEW_PTZ_STATE_DIR = $InstallDir

# Decide whether auth is on or off for this install.
$script:authEnabled = $true
if ($NoAuth) {
  $script:authEnabled = $false
  Write-Note "-NoAuth specified — running open (no PIN lock)"
}
elseif ($previouslyNoAuth -and $PinSetup -ne 'Force') {
  $script:authEnabled = $false
  Write-Note "Previous install was running open. Keeping it that way."
  Write-Note "  To switch to PINs: re-run with -PinSetup Force"
}
elseif (-not $pinExists -and $PinSetup -ne 'Skip') {
  # First-time install (no prior decision) — ask
  Write-Host ""
  Write-Host "    The PIN lock is recommended for shared LANs (chapels, classrooms,"
  Write-Host "    anywhere people you don't know might find the URL). On a trusted"
  Write-Host "    home LAN you can skip it and run open."
  Write-Host ""
  $resp = (Read-Host "    Enable PIN authentication? [Y/n]").Trim().ToLower()
  if ($resp -eq 'n' -or $resp -eq 'no') { $script:authEnabled = $false }
}

if (-not $script:authEnabled) {
  Write-OK "Auth disabled. Server will run open."
}
else {
  # ---- PIN setup (only when auth is enabled) -----------------------------
  function Invoke-AuthSet([string]$role) {
    & $venvPython -m pew_ptz.auth set --role $role
    if ($LASTEXITCODE -ne 0) { throw "auth set --role $role failed (exit $LASTEXITCODE)" }
  }

  $rolesToSet = @()
  if ($PinSetup -eq "Skip") {
    if (-not $pinExists) {
      throw "PinSetup=Skip but no PIN store at $authJson. Run again without -PinSetup Skip."
    }
    Write-OK "Skipping PIN setup (existing store kept)."
  }
  elseif ($PinSetup -eq "Force" -or -not $pinExists) {
    if ($pinExists -and $PinSetup -eq "Force") {
      Write-Note "PinSetup=Force — replacing both PINs"
    } elseif (-not $pinExists) {
      Write-Note "Setting initial PINs."
    }
    $rolesToSet = @("user","admin")
  }
  else {
    Write-Note "Found existing PIN store: $authJson"
    & $venvPython -m pew_ptz.auth status
    Write-Host ""
    Write-Host "    [K] Keep both (default)"
    Write-Host "    [B] Replace both"
    Write-Host "    [U] Replace user PIN only"
    Write-Host "    [A] Replace admin PIN only"
    $choice = (Read-Host "  Choice [K]").Trim().ToUpper()
    if (-not $choice) { $choice = "K" }
    switch ($choice) {
      "K" { Write-OK "Keeping existing PINs." }
      "B" { $rolesToSet = @("user","admin") }
      "U" { $rolesToSet = @("user") }
      "A" { $rolesToSet = @("admin") }
      default { Write-Note "Unrecognized choice '$choice' — keeping existing PINs." }
    }
  }

  foreach ($role in $rolesToSet) {
    Write-Note "Setting $role PIN (typing is hidden — no echo or asterisks)"
    Invoke-AuthSet $role
  }
  if (Test-Path $authJson) { Write-OK "PIN store: $authJson" }
}

Write-Step "Creating logs directory"
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
$acl = Get-Acl $logDir
$rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
  $TaskUser, "Modify", "ContainerInherit,ObjectInherit", "None", "Allow")
$acl.SetAccessRule($rule)
Set-Acl $logDir $acl
Write-OK "Logs at $logDir (writable by $TaskUser)"

# ---- Firewall rule (Private profile only) -------------------------------
Write-Step "Adding Windows Firewall rule for TCP $Port (Private profile)"
$ruleName = "Pew PTZ Controller (TCP $Port)"
Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Action Allow `
  -Protocol TCP -LocalPort $Port -Profile Private -Enabled True | Out-Null
Write-OK "Firewall rule added"

# ---- Scheduled Task ------------------------------------------------------
Write-Step "Registering Scheduled Task '$TaskName'"

# Remove any existing task so settings stay in sync with this script
Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue |
  Unregister-ScheduledTask -Confirm:$false

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $TaskUser

# Run only when the user is logged on (CRITICAL: pynput needs a desktop session)
$principal = New-ScheduledTaskPrincipal -UserId $TaskUser -LogonType Interactive -RunLevel Limited

$settings = New-ScheduledTaskSettingsSet `
  -AllowStartIfOnBatteries `
  -DontStopIfGoingOnBatteries `
  -StartWhenAvailable `
  -RestartCount 999 `
  -RestartInterval (New-TimeSpan -Minutes 1) `
  -ExecutionTimeLimit (New-TimeSpan -Days 0) `
  -MultipleInstances IgnoreNew

# Inject env vars via a tiny launcher .cmd. Task Scheduler doesn't have a
# direct env field, and pythonw.exe inherits cmd's environment.
$envLines = @(
  "set PEW_PTZ_LOG_DIR=$logDir",
  "set PEW_PTZ_SERVER_PORT=$Port",
  "set PEW_PTZ_STATE_DIR=$InstallDir"
)
if (-not $script:authEnabled) {
  $envLines += "set PEW_PTZ_AUTH_DISABLED=1"
}
$envBlock = $envLines -join "`r`n"
@"
@echo off
$envBlock
start "" "$venvPyw" -m pew_ptz
"@ | Set-Content -Encoding ASCII $launcher

$action = New-ScheduledTaskAction -Execute $launcher -WorkingDirectory $InstallDir
$task   = New-ScheduledTask -Action $action -Trigger $trigger -Principal $principal -Settings $settings
$task.Description = "pew-ptz: phone-based PTZ + Zoom Alt+V/Alt+A toggles"

Register-ScheduledTask -TaskName $TaskName -InputObject $task | Out-Null
Write-OK "Task registered (At logon of $TaskUser, restart-on-failure)"

# ---- Desktop shortcut ----------------------------------------------------
Write-Step "Creating desktop shortcut for $TaskUser"
$desktop = "C:\Users\$TaskUser\Desktop"
if (Test-Path $desktop) {
  $lnk = Join-Path $desktop "Pew PTZ.url"
  @"
[InternetShortcut]
URL=http://localhost:$Port
"@ | Set-Content -Encoding ASCII $lnk
  Write-OK "Shortcut: $lnk"
} else {
  Write-Note "Desktop folder for $TaskUser not found yet (user has never logged in?). Skipping."
}

# ---- Kick it off now -----------------------------------------------------
Write-Step "Starting the task now (so you can verify without rebooting)"
try {
  Start-ScheduledTask -TaskName $TaskName
  Start-Sleep -Seconds 2
  $info = Get-ScheduledTaskInfo -TaskName $TaskName
  Write-OK ("LastRunTime: {0}  LastTaskResult: 0x{1:X}" -f $info.LastRunTime, $info.LastTaskResult)
} catch {
  Write-Note "Could not start task as the current admin (this is normal — it'll start when $TaskUser logs in)."
}

# ---- Healthz check -------------------------------------------------------
Write-Step "Probing http://localhost:$Port/healthz"
$ok = $false
1..10 | ForEach-Object {
  try {
    $r = Invoke-RestMethod -Uri "http://localhost:$Port/healthz" -TimeoutSec 2
    Write-OK ("healthz OK  uptime={0}s  camera={1}  keyboard_ok={2}" -f $r.uptime_s, $r.camera_ip, $r.keyboard_ok)
    $ok = $true; break
  } catch { Start-Sleep -Milliseconds 500 }
}
if (-not $ok) {
  Write-Note "healthz did not respond. If you're running this installer as Administrator (not as $TaskUser),"
  Write-Note "the task hasn't been triggered yet — log in as $TaskUser (or reboot) and it will start automatically."
  Write-Note "Logs: $logDir\server.log"
}

Write-Host ""
Write-Host "Done." -ForegroundColor Green
Write-Host "  Logs:      $logDir\server.log"
Write-Host "  URL:       http://localhost:$Port  (or http://<this-pc-lan-ip>:$Port from the phone)"
Write-Host "  Task:      '$TaskName' (Task Scheduler -> Task Scheduler Library)"
Write-Host "  Uninstall: scripts\uninstall.ps1"
