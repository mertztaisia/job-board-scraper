<#
  Job Tracker local service manager.

  Usage (from the job_search folder):
    powershell -ExecutionPolicy Bypass -File service.ps1 install    # auto-start at login + start now
    powershell -ExecutionPolicy Bypass -File service.ps1 start
    powershell -ExecutionPolicy Bypass -File service.ps1 stop
    powershell -ExecutionPolicy Bypass -File service.ps1 restart    # after updating code
    powershell -ExecutionPolicy Bypass -File service.ps1 status
    powershell -ExecutionPolicy Bypass -File service.ps1 logs
    powershell -ExecutionPolicy Bypass -File service.ps1 versions   # list the releases
    powershell -ExecutionPolicy Bypass -File service.ps1 uninstall  # remove auto-start + stop

  Pick a version with -Version (install, start and restart; remembered by the auto-start task):
    ... service.ps1 install -Version latest   # the newest release (the default)
    ... service.ps1 restart -Version 1.6.0    # a specific release
    ... service.ps1 restart -Version local    # the code in this folder, unreleased changes included

  The service runs launch.py (which starts serve.py at that version) with pythonw.exe (no
  window) as your Windows user, starts when you log in, and restarts itself if it crashes.
#>
param(
    [Parameter(Position = 0)][ValidateSet("install", "uninstall", "start", "stop", "restart", "status", "logs", "versions")][string]$Action = "status",
    [ValidatePattern('^(latest|local|v?\d+\.\d+\.\d+)$')][string]$Version = "latest"
)

$ErrorActionPreference = "Stop"
$Here     = Split-Path -Parent $MyInvocation.MyCommand.Path
$TaskName = "Job Tracker"
$PyW      = Join-Path $Here ".venv\Scripts\pythonw.exe"
$Py       = Join-Path $Here ".venv\Scripts\python.exe"
$Launch   = Join-Path $Here "launch.py"
$LogDir   = Join-Path $Here "logs"
$VersionGiven = $PSBoundParameters.ContainsKey("Version")
$VersionsDir = if ($env:JOBS_VERSIONS_DIR) { $env:JOBS_VERSIONS_DIR } else { Join-Path $env:LOCALAPPDATA "JobTracker\versions" }

function Get-Port {
    $port = 5055
    $envFile = Join-Path $Here ".env"
    if (Test-Path $envFile) {
        $line = Get-Content $envFile | Where-Object { $_ -match '^\s*JOBS_PORT\s*=' } | Select-Object -First 1
        if ($line) { $port = [int](($line -split '=', 2)[1].Trim()) }
    }
    if ($env:JOBS_PORT) { $port = [int]$env:JOBS_PORT }
    return $port
}
$Port = Get-Port
$Url  = "http://127.0.0.1:$Port"

function Get-ServerPids {
    try {
        Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop |
            Select-Object -ExpandProperty OwningProcess -Unique
    } catch { @() }
}

function Test-Health {
    try { Invoke-RestMethod -Uri "$Url/api/health" -TimeoutSec 3 } catch { $null }
}

function New-TrackerAction([string]$v) {
    New-ScheduledTaskAction -Execute $PyW -Argument "`"$Launch`" --version $v" -WorkingDirectory $Here
}

# The version the auto-start task runs; $null for a task installed before versions existed
# (it runs serve.py directly). start/restart switch such a task to launch.py.
function Get-TaskVersion($task) {
    $arg = ($task.Actions | Select-Object -First 1).Arguments
    if ($arg -match '--version\s+(\S+)') { return $Matches[1] }
    return $null
}

function Start-Tracker {
    $existing = @(Get-ServerPids)
    if ($existing) {
        if (@($existing | Where-Object { Test-IsTracker $_ })) { Write-Host "Already running on $Url (stop or restart it to change the version)" }
        else { Write-Warning "Port $Port is used by another program, so the tracker can't start. Set JOBS_PORT in .env to a free port." }
        return
    }
    if (-not (Test-Path $PyW)) { throw "Missing $PyW. Create the virtual env first (see README)." }
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($task) {
        # -Version changes the version the auto-start task runs (from now on, at login too).
        if ($VersionGiven -or -not (Get-TaskVersion $task)) {
            Set-ScheduledTask -TaskName $TaskName -Action (New-TrackerAction $Version) | Out-Null
            Write-Host "Auto-start now runs version: $Version"
        }
        Start-ScheduledTask -TaskName $TaskName
    } else {
        Start-Process -FilePath $PyW -ArgumentList "`"$Launch`" --version $Version" -WorkingDirectory $Here -WindowStyle Hidden
    }
    # The first start of an older release unpacks it, so allow a little longer.
    for ($i = 0; $i -lt 40; $i++) {
        Start-Sleep -Milliseconds 500
        $h = Test-Health
        if ($h) {
            $v = if ($h.launched_as) { $h.launched_as } else { $h.version }
            Write-Host "Running: $Url  (version $v, $($h.roles) roles, db $($h.db))"; return
        }
    }
    Write-Warning "Started, but $Url/api/health isn't answering yet. Check: service.ps1 logs"
}

# True if the process is this tracker: its command line runs serve.py, app.py or launch.py
# from this folder, or a release unpacked by launch.py. (In a venv the listening process
# is the base Python, so the exe path alone can't tell.)
function Test-IsTracker([int]$ProcessId) {
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction SilentlyContinue
    if (-not $proc -or -not $proc.CommandLine) { return $false }
    $paths = @("serve.py", "app.py", "launch.py" | ForEach-Object { Join-Path $Here $_ }) + @($VersionsDir)
    foreach ($p in $paths) {
        if ($proc.CommandLine.IndexOf($p, [StringComparison]::OrdinalIgnoreCase) -ge 0) { return $true }
    }
    return $false
}

function Stop-Tracker {
    $pids = Get-ServerPids
    if (-not $pids) { Write-Host "Not running."; return }
    $stopped = $false
    foreach ($p in $pids) {
        if (Test-IsTracker $p) {
            Stop-Process -Id $p -Force -ErrorAction SilentlyContinue
            $stopped = $true
        } else {
            $name = (Get-Process -Id $p -ErrorAction SilentlyContinue).ProcessName
            Write-Warning "Port $Port is used by another program ($name, PID $p), not the tracker. Leaving it running."
        }
    }
    if ($stopped) { Write-Host "Stopped." }
}

switch ($Action) {
    "install" {
        # Not "$action": PowerShell names are case-insensitive, so it would clash with the
        # $Action parameter (and its ValidateSet) and the install would fail.
        $taskAction = New-TrackerAction $Version
        $trigger  = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
        $settingsObj = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) `
            -MultipleInstances IgnoreNew -StartWhenAvailable
        $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
        Register-ScheduledTask -TaskName $TaskName -Action $taskAction -Trigger $trigger -Settings $settingsObj `
            -Principal $principal -Description "Job Tracker web app on $Url (job_search/launch.py)" -Force | Out-Null
        Write-Host "Installed: '$TaskName' starts automatically when you log in (version: $Version)."
        $script:VersionGiven = $false   # the task was just set up with it
        Start-Tracker
    }
    "uninstall" {
        Stop-Tracker
        if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
            Write-Host "Removed auto-start task '$TaskName'."
        } else { Write-Host "No auto-start task to remove." }
    }
    "start"   { Start-Tracker }
    "stop"    { Stop-Tracker }
    "restart" { Stop-Tracker; Start-Sleep -Seconds 1; Start-Tracker }
    "status" {
        $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Write-Host ("Auto-start: " + $(if ($task) { "installed ($($task.State)), version: $(if (Get-TaskVersion $task) { Get-TaskVersion $task } else { 'local, set up before versions (start or restart switches it to latest)' })" } else { "not installed" }))
        $h = Test-Health
        if ($h) {
            $v = if ($h.launched_as) { $h.launched_as } else { $h.version }
            Write-Host "Server:     running at $Url  (version $v, up since $($h.started_at), $($h.roles) roles)"
        }
        elseif (Get-ServerPids) { Write-Host "Server:     port $Port is in use but /api/health didn't answer" }
        else { Write-Host "Server:     not running" }
    }
    "logs" {
        foreach ($f in @("launch.log", "server.log")) {
            $path = Join-Path $LogDir $f
            if (Test-Path $path) { Write-Host "== $path"; Get-Content $path -Tail 40 }
        }
        if (-not (Test-Path (Join-Path $LogDir "server.log"))) { Write-Host "No server log yet ($LogDir)." }
    }
    "versions" { & $Py $Launch --list }
}
