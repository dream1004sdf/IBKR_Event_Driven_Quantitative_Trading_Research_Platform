@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "TMPDIR="

if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] .venv\Scripts\python.exe not found.
  echo Run your project setup first, then retry.
  pause
  exit /b 1
)

if not defined RUN_NAME set "RUN_NAME=%~1"
if not defined RESTART_DELAY set "RESTART_DELAY=%~2"
if not defined RUN_COMMAND set "RUN_COMMAND=%~3"

if "%RUN_NAME%"=="" set "RUN_NAME=IBKR worker"
if "%RESTART_DELAY%"=="" set "RESTART_DELAY=10"
if "%RUN_COMMAND%"=="" (
  echo [ERROR] RUN_COMMAND is empty.
  echo Usage:
  echo   run_with_restart_windows.bat "Name" 10 "command"
  echo or set RUN_NAME / RESTART_DELAY / RUN_COMMAND before calling.
  pause
  exit /b 2
)

:restart
echo.
echo ========================================================
echo [%RUN_NAME%] starting
echo Command: %RUN_COMMAND%
echo ========================================================
call %RUN_COMMAND%
set "EXITCODE=%ERRORLEVEL%"
if "%EXITCODE%"=="0" (
  echo.
  echo [%RUN_NAME%] exited normally with code 0.
  exit /b 0
)
echo.
echo [%RUN_NAME%] exited unexpectedly with code %EXITCODE%.
echo Restarting in %RESTART_DELAY% seconds. Press Ctrl+C to stop this window.
timeout /t %RESTART_DELAY% /nobreak >nul
goto restart
