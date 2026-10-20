@echo off
setlocal
cd /d "%~dp0"
set "RUN_NAME=IBKR Realtime Recorder"
set "RESTART_DELAY=10"
set "RUN_COMMAND=.venv\Scripts\python.exe main.py run"
call run_with_restart_windows.bat
