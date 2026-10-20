@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

set "PYTHON_CMD=python"
if exist ".venv\Scripts\python.exe" set "PYTHON_CMD=.venv\Scripts\python.exe"

%PYTHON_CMD% -m pip install --upgrade pyinstaller
if errorlevel 1 (
  echo.
  echo EXE 빌드에 실패했습니다. 위 오류를 확인하십시오.
  pause
  exit /b 1
)

%PYTHON_CMD% -m PyInstaller ^
  --noconfirm ^
  --clean ^
  --onefile ^
  --console ^
  --name IBKR_Toolkit ^
  --collect-submodules recorder ^
  --collect-submodules prediction_evaluation ^
  --collect-all exchange_calendars ^
  --collect-all ibapi ^
  --add-data ".env.example;." ^
  --add-data "VERSION;." ^
  IBKR_Toolkit.py
if errorlevel 1 (
  echo.
  echo EXE 빌드에 실패했습니다. 위 오류를 확인하십시오.
  pause
  exit /b 1
)

echo.
echo Windows 실행파일 생성 완료:
echo   %CD%\dist\IBKR_Toolkit.exe
echo.
echo EXE를 프로젝트 폴더에 두고 .env 및 market_events.db와 함께 사용하십시오.
pause
exit /b 0
