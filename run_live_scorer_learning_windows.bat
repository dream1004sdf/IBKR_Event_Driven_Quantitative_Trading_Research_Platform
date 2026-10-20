@echo off
setlocal
cd /d "%~dp0"
set "TMPDIR="

set "RUN_NAME=M223 Live Scorer Learning"
set "RESTART_DELAY=10"
set "RUN_COMMAND=.venv\Scripts\python.exe main.py live-scorer-learning --profile-json config\live_scorer_learning_profile_v1.json"
call run_with_restart_windows.bat
