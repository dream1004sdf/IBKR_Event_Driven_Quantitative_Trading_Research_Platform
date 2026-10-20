$ErrorActionPreference = "Continue"

$RepoRoot = "C:\IBKR_Lumibot\ibkr_market_event_recorder"
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$Stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$LogDir = Join-Path $RepoRoot "exports\scheduled_option_validation"
$LogPath = Join-Path $LogDir "option_validation_2240_kst_$Stamp.log"

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Write-Log {
    param([string]$Message)
    $Line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss K"), $Message
    $Line | Tee-Object -FilePath $LogPath -Append
}

Set-Location $RepoRoot
Write-Log "Starting scheduled option validation at Korea/local computer time."
Write-Log "RepoRoot=$RepoRoot"

if ($env:IBKR_ALLOW_SCHEDULED_BROKER_REFRESH -ne "I_UNDERSTAND") {
    Write-Log "DISABLED: scheduled IBKR option validation is fail-closed; no broker/API action was started."
    exit 0
}

if (-not (Test-Path $Python)) {
    Write-Log "ERROR: Python venv not found: $Python"
    exit 1
}

$ValidationJson = Join-Path $RepoRoot "exports\m220_option_live_validation_check_2240_kst.json"
$ValidationMd = Join-Path $RepoRoot "exports\m220_option_live_validation_check_2240_kst.md"
$DashboardJson = Join-Path $RepoRoot "exports\m220_trading_dashboard_snapshot.json"
$DashboardMd = Join-Path $RepoRoot "exports\m220_trading_dashboard_snapshot.md"
$DashboardHtml = Join-Path $RepoRoot "exports\m220_trading_command_center.html"
$DashboardSummaryJson = Join-Path $RepoRoot "exports\m220_trading_dashboard_loop_summary.json"
$DashboardSummaryMd = Join-Path $RepoRoot "exports\m220_trading_dashboard_loop_summary.md"
$DashboardTicks = Join-Path $RepoRoot "exports\m220_trading_dashboard_loop_ticks.jsonl"

Write-Log "Running M220 options market-structure live validation."
& $Python "main.py" "options-market-structure-supervisor" `
    "--profile-json" "config\options_market_structure_supervisor_realtime_profile_v1.json" `
    "--max-ticks" "1" `
    "--execute-actions" `
    "--json-output-path" $ValidationJson `
    "--output" $ValidationMd `
    "--json" *>&1 | Tee-Object -FilePath $LogPath -Append
$ValidationExit = $LASTEXITCODE
Write-Log "M220 validation exit code: $ValidationExit"

Write-Log "Refreshing trading dashboard once after validation."
& $Python "main.py" "trading-dashboard-loop" `
    "--profile-json" "config\trading_dashboard_snapshot_profile_v1.json" `
    "--max-ticks" "1" `
    "--interval-seconds" "0" `
    "--disable-telegram" `
    "--snapshot-json-output-path" $DashboardJson `
    "--markdown-output-path" $DashboardMd `
    "--html-output-path" $DashboardHtml `
    "--summary-json-output-path" $DashboardSummaryJson `
    "--summary-markdown-output-path" $DashboardSummaryMd `
    "--tick-log-jsonl-path" $DashboardTicks *>&1 | Tee-Object -FilePath $LogPath -Append
$DashboardExit = $LASTEXITCODE
Write-Log "Dashboard refresh exit code: $DashboardExit"

Write-Log "Scheduled option validation completed. ValidationJson=$ValidationJson DashboardHtml=$DashboardHtml"

if ($ValidationExit -ne 0 -or $DashboardExit -ne 0) {
    exit 1
}
exit 0
