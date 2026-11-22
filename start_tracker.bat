@echo off
rem Runs the tracker in this window (useful for debugging).
rem For the always-on background service use:  powershell -ExecutionPolicy Bypass -File service.ps1 install
cd /d "%~dp0"
echo Job Tracker at http://localhost:5055  (close this window to stop)
start "" http://localhost:5055
".venv\Scripts\python.exe" serve.py
if errorlevel 1 (
    echo.
    echo The tracker didn't start here; see the message above.
    echo If the background service is already running, the browser tab that just opened is using it,
    echo and you don't need this window. Check with:  powershell -ExecutionPolicy Bypass -File service.ps1 status
    pause
)
