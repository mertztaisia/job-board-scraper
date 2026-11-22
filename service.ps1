<#
  Job Tracker local service manager.

  Usage (from the job_search folder):
    powershell -ExecutionPolicy Bypass -File service.ps1 install    # auto-start at login + start now
    powershell -ExecutionPolicy Bypass -File service.ps1 start
    powershell -ExecutionPolicy Bypass -File service.ps1 stop
    powershell -ExecutionPolicy Bypass -File service.ps1 restart    # after updating code
    powershell -ExecutionPolicy Bypass -File service.ps1 status
    powershell -ExecutionPolicy Bypass -File service.ps1 logs
    powershell -ExecutionPolicy Bypass -File service.ps1 uninstall  # remove auto-start + stop

  The service runs serve.py with pythonw.exe (no window) as your Windows user,
  starts when you log in, and restarts itself if it crashes.
#>
param([Parameter(Position = 0)][ValidateSet("install", "uninstall", "start", "stop", "restart", "status", "logs")][string]$Action = "status")

$ErrorActionPreference = "Stop"
$Here     = Split-Path -Parent $MyInvocation.MyCommand.Path
$TaskName = "Job Tracker"
$PyW      = Join-Path $Here ".venv\Scripts\pythonw.exe"
$Serve    = Join-Path $Here "serve.py"
$LogFile  = Join-Path $Here "logs\server.log"

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

function Start-Tracker {
    $existing = @(Get-ServerPids)
    if ($existing) {
        if (@($existing | Where-Object { Test-IsTracker $_ })) { Write-Host "Already running on $Url" }
        else { Write-Warning "Port $Port is used by another program, so the tracker can't start. Set JOBS_PORT in .env to a free port." }
        return
    }
    if (-not (Test-Path $PyW)) { throw "Missing $PyW. Create the virtual env first (see README)." }
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Start-ScheduledTask -TaskName $TaskName
    } else {
        Start-Process -FilePath $PyW -ArgumentList "`"$Serve`"" -WorkingDirectory $Here -WindowStyle Hidden
    }
    for ($i = 0; $i -lt 20; $i++) {
        Start-Sleep -Milliseconds 500
        $h = Test-Health
        if ($h) { Write-Host "Running: $Url  ($($h.roles) roles, db $($h.db))"; return }
    }
    Write-Warning "Started, but $Url/api/health isn't answering yet. Check: service.ps1 logs"
}

# True if the process is this tracker: its command line runs serve.py or app.py from
# this folder. (In a venv the listening process is the base Python, so the exe path
# alone can't tell.)
function Test-IsTracker([int]$ProcessId) {
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction SilentlyContinue
    if (-not $proc -or -not $proc.CommandLine) { return $false }
    foreach ($script in @("serve.py", "app.py")) {
        if ($proc.CommandLine.IndexOf((Join-Path $Here $script), [StringComparison]::OrdinalIgnoreCase) -ge 0) { return $true }
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
        $taskAction = New-ScheduledTaskAction -Execute $PyW -Argument "`"$Serve`"" -WorkingDirectory $Here
        $trigger  = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
        $settingsObj = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) `
            -MultipleInstances IgnoreNew -StartWhenAvailable
        $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
        Register-ScheduledTask -TaskName $TaskName -Action $taskAction -Trigger $trigger -Settings $settingsObj `
            -Principal $principal -Description "Job Tracker web app on $Url (job_search/serve.py)" -Force | Out-Null
        Write-Host "Installed: '$TaskName' starts automatically when you log in."
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
        Write-Host ("Auto-start: " + $(if ($task) { "installed ($($task.State))" } else { "not installed" }))
        $h = Test-Health
        if ($h) { Write-Host "Server:     running at $Url  (v$($h.version), up since $($h.started_at), $($h.roles) roles)" }
        elseif (Get-ServerPids) { Write-Host "Server:     port $Port is in use but /api/health didn't answer" }
        else { Write-Host "Server:     not running" }
    }
    "logs" {
        if (Test-Path $LogFile) { Get-Content $LogFile -Tail 60 } else { Write-Host "No log yet ($LogFile)." }
    }
}
