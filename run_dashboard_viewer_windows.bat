@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] .venv\Scripts\python.exe not found.
  echo Run your project setup first, then retry.
  pause
  exit /b 1
)

echo Starting local Trading Command Center viewer server...
echo Serving only the exports\ directory (not the project root, so .env/secrets are not reachable).
echo.
echo Open this URL in your browser:
echo   http://127.0.0.1:8765/trading_command_center.html
echo.
echo This localhost view lets the dashboard update sections without a full page reload.
echo Keep this window open while using the dashboard.
echo.

set "RUN_NAME=Dashboard Local Viewer"
set "RESTART_DELAY=10"
set "RUN_COMMAND=.venv\Scripts\python.exe -m http.server 8765 --bind 127.0.0.1 --directory exports"
call run_with_restart_windows.bat
