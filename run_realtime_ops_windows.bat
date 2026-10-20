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

echo Starting realtime split-role operations preset...
echo.
echo This opens ten read-only/paper-control windows:
echo   1. IBKR Realtime Recorder: subscribes to live IBKR/Benzinga tickNews and price ticks immediately.
echo   2. Source Online Supervisor: owns heavy evidence/source catch-up and keeps feeds warm.
echo   3. M223 Live Scorer Learning: records prediction/outcome samples for training.
echo   4. M225 Live Learning LLM Notes: explains prediction gaps in shadow mode.
echo   5. M226 Threshold Candidate Evaluator: evaluates threshold candidates without promotion.
echo   6. M232 Live Learning Control Plane: publishes fresh hot-cache overlays for scorer/dashboard use.
echo   7. Capital Refresh Loop: refreshes read-only account snapshot, M237, and M244.
echo   8. Artifact Cleanup Dry-Run Loop: refreshes cleanup/size reports without deleting.
echo   9. Trading Command Center: reads artifacts, keeps cheap/final safety gates inline, and refreshes the dashboard every second.
echo   10. Dashboard Local Viewer: serves the HTML over localhost so sections can update without full page reload.
echo.
echo Safety:
echo   - No live order submission.
echo   - Live transmit/routing remains disabled.
echo   - Realtime recorder stores IBKR news/price evidence only.
echo   - Evidence/source artifacts may be refreshed by the supervisor profile.
echo   - Automated broker what-if, short-availability, and option-quote refresh is disabled.
echo   - Learning loops are shadow/paper only and do not promote thresholds automatically.
echo   - Capital refresh is read-only account state; cleanup loop is dry-run unless explicitly executed.
echo.

echo Cleaning stale singleton locks before startup...
".venv\Scripts\python.exe" main.py singleton-lock-cleanup --min-age-seconds 30 --execute
echo.

start "IBKR Realtime Recorder" cmd /k "run_recorder_windows.bat"

timeout /t 2 /nobreak >nul

start "Source Online Supervisor" cmd /k "run_source_supervisor_windows.bat"

timeout /t 2 /nobreak >nul

start "M223 Live Scorer Learning" cmd /k "run_live_scorer_learning_windows.bat"

timeout /t 2 /nobreak >nul

start "M225 Live Learning LLM Notes" cmd /k "run_live_learning_llm_notes_windows.bat"

timeout /t 2 /nobreak >nul

start "M226 Threshold Candidate Evaluator" cmd /k "run_threshold_candidate_evaluator_windows.bat"

timeout /t 2 /nobreak >nul

start "M232 Live Learning Control Plane" cmd /k "run_live_learning_control_plane_windows.bat"

timeout /t 2 /nobreak >nul

start "Capital Refresh Loop" cmd /k "run_capital_refresh_loop_windows.bat"

timeout /t 2 /nobreak >nul

start "Artifact Cleanup Dry-Run Loop" cmd /k "run_artifact_cleanup_loop_windows.bat"

timeout /t 2 /nobreak >nul

start "Trading Command Center Loop" cmd /k "run_trading_dashboard_loop_windows.bat"

timeout /t 2 /nobreak >nul

start "Dashboard Local Viewer" cmd /k "run_dashboard_viewer_windows.bat"

echo.
echo Realtime ops windows started.
echo Dashboard HTML will be written to:
echo   exports\trading_command_center.html
echo.
echo Open the no-full-reload localhost view at:
echo   http://127.0.0.1:8765/trading_command_center.html
echo.
echo You can leave all ten windows running.
pause
