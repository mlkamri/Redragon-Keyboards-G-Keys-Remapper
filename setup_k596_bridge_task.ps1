<#
.SYNOPSIS
    Registers k596_gkey_bridge.py to run automatically, hidden, at logon,
    via Windows Task Scheduler.

.DESCRIPTION
    Creates a Scheduled Task named "K596GKeyBridge" that launches the
    bridge script silently (via pythonw.exe, no console window) every
    time you log in. Always registers the task to "Run with highest
    privileges" so it can still send input to elevated target
    applications (this only actually applies elevation if your Windows
    account is a member of the Administrators group).

.PARAMETER ScriptPath
    Path to k596_gkey_bridge.py. Defaults to the copy sitting next to
    this .ps1 file, which is the normal case if you keep both files
    together (e.g. after cloning/downloading this repo).

.PARAMETER ExtraArgs
    Extra command-line arguments to pass to the bridge script, e.g.
    "--start-key 0x91" to remap to Scroll Lock instead of F13. See
    "python k596_gkey_bridge.py --help" for all options.

.PARAMETER Uninstall
    Removes the scheduled task instead of creating it.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File setup_k596_bridge_task.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File setup_k596_bridge_task.ps1 -Uninstall

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File setup_k596_bridge_task.ps1 -ExtraArgs "--count 6 --start-key 0x91"
#>

param(
    [string]$ScriptPath = (Join-Path $PSScriptRoot "k596_gkey_bridge.py"),
    [string]$ExtraArgs = "",
    [switch]$Uninstall
)

$TaskName = "K596GKeyBridge"

if ($Uninstall) {
    try {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop
        Write-Host "Removed scheduled task '$TaskName'."
    } catch {
        Write-Host "Could not remove task (it may not exist): $($_.Exception.Message)"
    }
    exit
}

if (-not (Test-Path $ScriptPath)) {
    Write-Host "ERROR: Script not found at: $ScriptPath"
    Write-Host "Pass -ScriptPath explicitly if you keep k596_gkey_bridge.py somewhere else, e.g.:"
    Write-Host "    powershell -ExecutionPolicy Bypass -File setup_k596_bridge_task.ps1 -ScriptPath 'C:\path\to\k596_gkey_bridge.py'"
    exit 1
}
$ScriptPath = (Resolve-Path $ScriptPath).Path
$WorkDir = Split-Path -Path $ScriptPath -Parent

# Find pythonw.exe next to whatever "py" launcher resolves to (avoids a
# visible console window, unlike python.exe/py.exe).
$pyLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
$pythonwPath = $null

if ($pyLauncher) {
    $pyHome = & py -c "import sys; print(sys.base_prefix)" 2>$null
    if ($pyHome) {
        $candidate = Join-Path $pyHome "pythonw.exe"
        if (Test-Path $candidate) { $pythonwPath = $candidate }
    }
}
if (-not $pythonwPath) {
    $found = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if ($found) { $pythonwPath = $found.Source }
}
if (-not $pythonwPath) {
    Write-Host "ERROR: Could not locate pythonw.exe automatically."
    Write-Host "Make sure Python is installed and on PATH, or find pythonw.exe manually"
    Write-Host "(usually next to python.exe) and re-run passing it via a custom Action."
    exit 1
}

Write-Host "Interpreter : $pythonwPath"
Write-Host "Script      : $ScriptPath"
if ($ExtraArgs) { Write-Host "Extra args  : $ExtraArgs" }

$fullArgs = "`"$ScriptPath`""
if ($ExtraArgs) { $fullArgs = "$fullArgs $ExtraArgs" }

$action  = New-ScheduledTaskAction -Execute $pythonwPath -Argument $fullArgs -WorkingDirectory $WorkDir
$trigger = New-ScheduledTaskTrigger -AtLogOn

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltinRole]::Administrator)

# Always request highest privileges. Task Scheduler grants the elevated
# token at trigger time (at logon), not at registration time, and does
# NOT show a UAC prompt when doing so for a task configured this way
# (unlike double-clicking an .exe that requests elevation). Your Windows
# account needs to be an Administrator for this to actually mean
# anything; it's silently a no-op otherwise.
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Highest
Write-Host "Task will be registered to 'Run with highest privileges'."
if (-not $isAdmin) {
    Write-Host "NOTE: this setup script is not currently elevated. If the task doesn't actually"
    Write-Host "run elevated afterward (check Task Scheduler > Properties > General > 'Run with"
    Write-Host "highest privileges'), re-run this script from an elevated PowerShell instead"
    Write-Host "(right-click PowerShell -> Run as administrator)."
}

$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew

try {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings | Out-Null
    Write-Host "`nScheduled task '$TaskName' created. It will start the bridge silently at your next logon."
    Write-Host "To start it immediately without logging out, run:"
    Write-Host "    Start-ScheduledTask -TaskName `"$TaskName`""
    Write-Host "To remove it later, run this script again with -Uninstall."
} catch {
    Write-Host "ERROR creating scheduled task: $($_.Exception.Message)"
    exit 1
}
