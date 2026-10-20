@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] .venv\Scripts\python.exe not found.
  echo Run your project setup first, then retry.
  pause
  exit /b 1
)

set "RUN_NAME=Runtime Watchdog Observer"
set "RESTART_DELAY=10"
set "RUN_COMMAND=powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\runtime_watchdog_observer.ps1 -IntervalSeconds 60 -MaxTicks 0 -RequireLearningLoops"
call run_with_restart_windows.bat
