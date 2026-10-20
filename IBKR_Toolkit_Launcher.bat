@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

set "PYTHON_CMD=python"
if exist ".venv\Scripts\pythonw.exe" set "PYTHON_CMD=.venv\Scripts\pythonw.exe"
if exist ".venv\Scripts\python.exe" if not exist ".venv\Scripts\pythonw.exe" set "PYTHON_CMD=.venv\Scripts\python.exe"

where %PYTHON_CMD% >nul 2>&1
if errorlevel 1 (
    echo Python을 찾을 수 없습니다.
    echo Python 3.11 이상을 설치하거나 .venv를 생성한 뒤 다시 실행하십시오.
    pause
    exit /b 1
)

start "IBKR Toolkit" %PYTHON_CMD% "%~dp0IBKR_Toolkit.py"
endlocal
