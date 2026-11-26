@echo off
cd /d "%~dp0"
rem Kept for older scheduled tasks. New setup (no console window): see README section 3.
".venv\Scripts\python.exe" daily_jobs.py --trigger schedule >> run_log.txt 2>&1
