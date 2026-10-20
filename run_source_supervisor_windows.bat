@echo off
setlocal
cd /d "%~dp0"
set "RUN_NAME=Source Online Supervisor"
set "RESTART_DELAY=10"
set "RUN_COMMAND=.venv\Scripts\python.exe main.py source-online-supervisor --profile-json config\source_online_supervisor_profile_v1.json --max-ticks 0 --interval-seconds 1 --sleep-between-ticks"
call run_with_restart_windows.bat
