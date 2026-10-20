@echo off
setlocal
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Missing .venv. Create it and install requirements first.
  pause
  exit /b 1
)

.venv\Scripts\python.exe main.py ai-diagnose
if errorlevel 1 (
  echo.
  echo AI configuration validation failed. Fix .env before retrying.
  pause
  exit /b 1
)

:restart
set "RUN_NAME=AI News Analysis Service"
set "RESTART_DELAY=10"
set "RUN_COMMAND=.venv\Scripts\python.exe main.py ai-run"
call run_with_restart_windows.bat
