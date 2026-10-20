@echo off
setlocal
cd /d "%~dp0"
set "TMPDIR="

set "RUN_NAME=M225 Live Learning LLM Notes"
set "RESTART_DELAY=10"
set "RUN_COMMAND=.venv\Scripts\python.exe main.py live-learning-llm-notes --profile-json config\live_learning_llm_notes_profile_v1.json"
call run_with_restart_windows.bat
