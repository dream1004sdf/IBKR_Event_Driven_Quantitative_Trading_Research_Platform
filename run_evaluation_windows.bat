@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\activate.bat" call ".venv\Scripts\activate.bat"
python main.py evaluation-migrate
if errorlevel 1 goto :error
python main.py evaluation-run
if errorlevel 1 goto :error
python main.py evaluation-report --show-exclusions
if errorlevel 1 goto :error
pause
exit /b 0
:error
echo Evaluation engine command failed with exit code %errorlevel%.
pause
exit /b %errorlevel%
