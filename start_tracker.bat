@echo off
rem Runs the tracker in this window (useful for debugging), at the latest release by default.
rem   start_tracker.bat                    latest release
rem   start_tracker.bat --version 1.6.0    a specific release
rem   start_tracker.bat --version local    the code in this folder, unreleased changes included
rem For the always-on background service use:  powershell -ExecutionPolicy Bypass -File service.ps1 install
cd /d "%~dp0"
echo Job Tracker at http://localhost:5055  (close this window to stop)
start "" http://localhost:5055
".venv\Scripts\python.exe" launch.py %*
if errorlevel 1 (
    echo.
    echo The tracker didn't start here; see the message above.
    echo If the background service is already running, the browser tab that just opened is using it,
    echo and you don't need this window. Check with:  powershell -ExecutionPolicy Bypass -File service.ps1 status
    pause
)
