@echo off
setlocal
cd /d "%~dp0"
set "TMPDIR="

if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] .venv\Scripts\python.exe not found.
  echo Run your project setup first, then retry.
  pause
  exit /b 1
)

set "PY=.venv\Scripts\python.exe"

:MENU
cls
echo ========================================================
echo   IBKR Market Event Recorder - Launcher
echo ========================================================
echo   1) Start realtime ops   (Recorder + Supervisors + Dashboard)
echo   2) Data collection       (News / Macro / Options / Market data)
echo   3) AI analysis ^& eval    (AI Analyzer / Evaluation / Calibration)
echo   4) Run tests             (pytest)
echo   5) Build EXE
echo   6) Open GUI toolkit      (single-command console)
echo   7) Backup database        (VACUUM INTO snapshot, keeps last 14)
echo   8) Artifact cleanup       (dry-run first; optional execute)
echo   9) AI cost report         (token usage and configured estimates)
echo  10) Prediction trend report (recent vs previous hit-rate)
echo  11) Future earnings calendar (SEC-cycle projection)
echo  12) Lock status            (process / lock / data truth)
echo  13) Stale lock cleanup     (dry-run first; optional execute)
echo  14) Code health report     (large files / long functions)
echo  15) Capital/margin state   (M237 account budget and flow check)
echo  16) Capital-aware gate     (M239 budget-aware action gate)
echo   0) Exit
echo ========================================================
set "CHOICE="
set /p CHOICE=Select ^>
if "%CHOICE%"=="1" goto OPT_REALTIME
if "%CHOICE%"=="2" goto DATA_MENU
if "%CHOICE%"=="3" goto AI_MENU
if "%CHOICE%"=="4" goto OPT_TEST
if "%CHOICE%"=="5" goto OPT_BUILD
if "%CHOICE%"=="6" goto OPT_GUI
if "%CHOICE%"=="7" goto OPT_DB_BACKUP
if "%CHOICE%"=="8" goto OPT_ARTIFACT_CLEANUP
if "%CHOICE%"=="9" goto OPT_AI_COST_REPORT
if "%CHOICE%"=="10" goto OPT_PREDICTION_TREND
if "%CHOICE%"=="11" goto OPT_FUTURE_EARNINGS
if "%CHOICE%"=="12" goto OPT_LOCK_STATUS
if "%CHOICE%"=="13" goto OPT_LOCK_CLEANUP
if "%CHOICE%"=="14" goto OPT_CODE_HEALTH
if "%CHOICE%"=="15" goto OPT_CAPITAL_MARGIN
if "%CHOICE%"=="16" goto OPT_CAPITAL_AWARE_GATE
if "%CHOICE%"=="0" goto :EOF
echo.
echo Invalid choice.
pause
goto MENU

:OPT_REALTIME
call run_realtime_ops_windows.bat
goto MENU

:OPT_TEST
call test_windows.bat
goto MENU

:OPT_BUILD
call build_windows_exe.bat
goto MENU

:OPT_GUI
start "IBKR Toolkit" "IBKR_Toolkit_Launcher.bat"
goto MENU

:OPT_DB_BACKUP
echo.
echo [Database backup]
"%PY%" main.py db-backup --backup-dir backups --keep 14
pause
goto MENU

:OPT_ARTIFACT_CLEANUP
echo.
echo [Artifact cleanup dry-run]
"%PY%" main.py artifact-cleanup --keep-latest-per-logical-key 5 --work-max-age-days 14
echo.
set "CLEANUP_CONFIRM="
set /p CLEANUP_CONFIRM=Execute deletion of the listed candidates? Type YES to continue ^>
if /I "%CLEANUP_CONFIRM%"=="YES" (
  "%PY%" main.py artifact-cleanup --keep-latest-per-logical-key 5 --work-max-age-days 14 --execute
) else (
  echo Cleanup execute skipped.
)
pause
goto MENU

:OPT_AI_COST_REPORT
echo.
echo [AI cost report]
"%PY%" main.py ai-cost-report
pause
goto MENU

:OPT_PREDICTION_TREND
echo.
echo [Prediction trend report]
"%PY%" main.py prediction-trend-report --window-days 14 --date-field updated_at --min-rows 1
pause
goto MENU

:OPT_FUTURE_EARNINGS
echo.
echo [Future earnings calendar]
"%PY%" main.py future-earnings-calendar --horizon-days 120
pause
goto MENU

:OPT_LOCK_STATUS
echo.
echo [Operational lock status]
"%PY%" main.py singleton-lock-status
pause
goto MENU

:OPT_LOCK_CLEANUP
echo.
echo [Stale lock cleanup dry-run]
"%PY%" main.py singleton-lock-cleanup --min-age-seconds 30
echo.
set "LOCK_CLEANUP_CONFIRM="
set /p LOCK_CLEANUP_CONFIRM=Remove stale locks whose PID is not running? Type YES to continue ^>
if /I "%LOCK_CLEANUP_CONFIRM%"=="YES" (
  "%PY%" main.py singleton-lock-cleanup --min-age-seconds 30 --execute
) else (
  echo Stale lock cleanup execute skipped.
)
pause
goto MENU

:OPT_CODE_HEALTH
echo.
echo [Code health report]
"%PY%" main.py code-health-report
pause
goto MENU

:OPT_CAPITAL_MARGIN
echo.
echo [M237 Capital/margin state]
set "CAPITAL_REFRESH="
set /p CAPITAL_REFRESH=Refresh read-only IBKR account snapshot first? Type YES to refresh ^>
if /I "%CAPITAL_REFRESH%"=="YES" (
  "%PY%" main.py capital-margin-state --refresh-account-snapshot
) else (
  "%PY%" main.py capital-margin-state
)
pause
goto MENU

:OPT_CAPITAL_AWARE_GATE
echo.
echo [M239 Capital-aware action gate]
"%PY%" main.py replay-score-capital-aware-action-gate
pause
goto MENU

:: =========================================================
:: Data collection (news / macro / options / market data)
:: =========================================================
:DATA_MENU
cls
echo ========================================================
echo   Data collection
echo ========================================================
echo   1) News backfill          (last 7 days, needs IBKR)
echo   2) Macro/economic sync    (Fed/BLS/BEA + FRED, no IBKR needed)
echo   3) Options structure refresh (1 tick, needs IBKR)
echo   4) Market data snapshot   (30s, needs IBKR)
echo   5) Run all (1 -^> 4)
echo   0) Back
echo ========================================================
set "CHOICE="
set /p CHOICE=Select ^>
if "%CHOICE%"=="1" (call :DATA_NEWS & goto DATA_MENU)
if "%CHOICE%"=="2" (call :DATA_MACRO & goto DATA_MENU)
if "%CHOICE%"=="3" (call :DATA_OPTIONS & goto DATA_MENU)
if "%CHOICE%"=="4" (call :DATA_MARKET & goto DATA_MENU)
if "%CHOICE%"=="5" (call :DATA_NEWS & call :DATA_MACRO & call :DATA_OPTIONS & call :DATA_MARKET & goto DATA_MENU)
if "%CHOICE%"=="0" goto MENU
echo.
echo Invalid choice.
pause
goto DATA_MENU

:DATA_NEWS
for /f %%d in ('powershell -NoProfile -Command "(Get-Date).ToString(''yyyy-MM-dd'')"') do set "TODAY=%%d"
for /f %%d in ('powershell -NoProfile -Command "(Get-Date).AddDays(-7).ToString(''yyyy-MM-dd'')"') do set "WEEKAGO=%%d"
echo.
echo [News backfill] %WEEKAGO% to %TODAY%
"%PY%" main.py ibkr-news-backfill --start %WEEKAGO% --end %TODAY%
pause
exit /b 0

:DATA_MACRO
echo.
echo [Macro sync] Fed/BLS/BEA + FRED global_core universe
"%PY%" main.py macro-sync --source all
"%PY%" main.py fred-universe-sync --universe global_core
pause
exit /b 0

:DATA_OPTIONS
echo.
echo [Options structure refresh] 1 tick
"%PY%" main.py options-market-structure-supervisor --profile-json config\options_market_structure_supervisor_profile_v1.json --max-ticks 1 --execute-actions
pause
exit /b 0

:DATA_MARKET
echo.
echo [Market data snapshot] 30s
"%PY%" main.py market-data-desk --seconds 30
pause
exit /b 0

:: =========================================================
:: AI analysis and evaluation
:: =========================================================
:AI_MENU
cls
echo ========================================================
echo   AI analysis and evaluation
echo ========================================================
echo   1) Start AI Analyzer (new window, keeps running)
echo   2) Run evaluation pipeline (migrate + run + report)
echo   3) Calibration fit + report
echo   0) Back
echo ========================================================
set "CHOICE="
set /p CHOICE=Select ^>
if "%CHOICE%"=="1" goto AI_ANALYZER
if "%CHOICE%"=="2" goto AI_EVAL
if "%CHOICE%"=="3" goto AI_CALIBRATION
if "%CHOICE%"=="0" goto MENU
echo.
echo Invalid choice.
pause
goto AI_MENU

:AI_ANALYZER
start "AI News Analysis Service" cmd /k "run_ai_windows.bat"
echo AI Analyzer started in a new window.
pause
goto AI_MENU

:AI_EVAL
call run_evaluation_windows.bat
goto AI_MENU

:AI_CALIBRATION
echo.
echo [Calibration fit]
"%PY%" main.py calibration-fit
echo.
echo [Calibration report]
"%PY%" main.py calibration-report
pause
goto AI_MENU
