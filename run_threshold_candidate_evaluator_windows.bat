@echo off
setlocal
cd /d "%~dp0"
set "TMPDIR="

set "RUN_NAME=M226 Threshold Candidate Evaluator"
set "RESTART_DELAY=10"
set "RUN_COMMAND=.venv\Scripts\python.exe main.py threshold-candidate-evaluator --profile-json config\threshold_candidate_evaluator_profile_v1.json"
call run_with_restart_windows.bat
