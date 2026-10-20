@echo off
setlocal
cd /d "%~dp0"
echo [DISABLED] Automated execution-safety broker refresh is stopped.
echo This launcher no longer starts what-if, short-availability, or option-quote refreshes.
exit /b 0
