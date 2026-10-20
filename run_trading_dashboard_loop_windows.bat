@echo off
setlocal
cd /d "%~dp0"
set "RUN_NAME=Trading Command Center Loop"
set "RESTART_DELAY=10"
set "RUN_COMMAND=.venv\Scripts\python.exe main.py trading-dashboard-loop --profile-json config\trading_dashboard_snapshot_profile_v1.json --max-ticks 0 --interval-seconds 1 --sleep-between-ticks"
call run_with_restart_windows.bat
