param(
    [switch]$NoStart
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Resolve-Path (Join-Path $ScriptDir "..")
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$LocksDir = Join-Path $RepoRoot "locks"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Python runtime not found: $Python"
}

$commandPatterns = @(
    "market-data-desk",
    "source-online-supervisor",
    "live-scorer-learning",
    "live-learning-llm-notes",
    "threshold-candidate-evaluator",
    "live_learning_control_plane",
    "run_capital_refresh_loop.ps1",
    "run_artifact_cleanup_loop.ps1",
    "trading-dashboard-loop"
)

$repoPattern = [WildcardPattern]::Escape($RepoRoot.ProviderPath)
$targets = Get-CimInstance Win32_Process |
    Where-Object {
        $cmd = [string]$_.CommandLine
        $exe = [string]$_.ExecutablePath
        if (-not $cmd) { return $false }
        $inRepo = (
            $cmd -like "*$repoPattern*" -or
            $cmd -like "*ibkr_market_event_recorder*" -or
            ($exe -and $exe -like "$($RepoRoot.ProviderPath)*")
        )
        if (-not $inRepo) { return $false }
        if ($cmd -notlike "*main.py*" -and $cmd -notlike "*recorder.live_learning_control_plane*" -and $cmd -notlike "*run_capital_refresh_loop.ps1*" -and $cmd -notlike "*run_artifact_cleanup_loop.ps1*") { return $false }
        foreach ($pattern in $commandPatterns) {
            if ($cmd -like "*$pattern*") { return $true }
        }
        return $false
    } |
    Sort-Object ProcessId -Descending

foreach ($proc in $targets) {
    try {
        Stop-Process -Id $proc.ProcessId -Force -ErrorAction Stop
        Write-Host "Stopped PID $($proc.ProcessId): $($proc.CommandLine)"
    } catch {
        Write-Warning "Failed to stop PID $($proc.ProcessId): $($_.Exception.Message)"
    }
}

Start-Sleep -Seconds 2

if (Test-Path -LiteralPath $LocksDir) {
    Get-ChildItem -LiteralPath $LocksDir -Filter "*.lock" -File |
        Where-Object { $commandPatterns -contains $_.BaseName } |
        Remove-Item -Force
}

if ($NoStart) {
    Write-Host "Realtime stack stopped and locks cleaned. NoStart was requested."
    exit 0
}

function Start-StackProcess {
    param(
        [string]$Name,
        [string[]]$Arguments
    )
    $process = Start-Process -FilePath $Python -ArgumentList $Arguments -WorkingDirectory $RepoRoot -WindowStyle Hidden -PassThru
    Write-Host "Started $Name PID $($process.Id)"
}

function Start-PowerShellStackProcess {
    param(
        [string]$Name,
        [string[]]$Arguments
    )
    $process = Start-Process -FilePath "powershell.exe" -ArgumentList $Arguments -WorkingDirectory $RepoRoot -WindowStyle Hidden -PassThru
    Write-Host "Started $Name PID $($process.Id)"
}

Start-StackProcess "market-data-desk" @(
    "main.py", "market-data-desk",
    "--seconds", "0",
    "--refresh-seconds", "1",
    "--market-data-type", "1",
    "--fallback-after-seconds", "4",
    "--account-snapshot-json", "exports\m135_account_snapshot.json",
    "--output", "exports\m153_market_data_desk_refresh.md",
    "--json-output-path", "exports\m153_market_data_desk_refresh.json"
)

Start-StackProcess "source-online-supervisor" @(
    "main.py", "source-online-supervisor",
    "--profile-json", "config\source_online_supervisor_realtime_profile_v1.json"
)

Start-StackProcess "live-scorer-learning" @(
    "main.py", "live-scorer-learning",
    "--profile-json", "config\live_scorer_learning_profile_v1.json"
)

Start-StackProcess "live-learning-llm-notes" @(
    "main.py", "live-learning-llm-notes",
    "--profile-json", "config\live_learning_llm_notes_profile_v1.json"
)

Start-StackProcess "threshold-candidate-evaluator" @(
    "main.py", "threshold-candidate-evaluator",
    "--profile-json", "config\threshold_candidate_evaluator_profile_v1.json"
)

Start-StackProcess "live-learning-control-plane" @(
    "-m", "recorder.live_learning_control_plane",
    "--database-path", "market_events.db",
    "--live-scorer-learning-json", "exports\m223_live_scorer_learning.json",
    "--threshold-candidate-evaluator-json", "exports\m226_threshold_candidate_evaluator_summary.json",
    "--output", "exports\m232_live_learning_control_plane.md",
    "--json-output-path", "exports\m232_live_learning_control_plane.json",
    "--hot-cache-json-path", "exports\m232_hot_cache.json",
    "--reflex-hot-cache-json-path", "exports\m232_reflex_hot_cache.json",
    "--scorer-overlay-cache-json-path", "exports\m232_scorer_overlay_cache.json",
    "--option-structure-cache-json-path", "exports\m232_option_structure_cache.json",
    "--action-gate-cache-json-path", "exports\m232_action_gate_cache.json",
    "--refresh-context-learning",
    "--max-ticks", "0",
    "--interval-seconds", "60",
    "--adaptive",
    "--hot-limit", "500",
    "--hot-min-interval-seconds", "5",
    "--hot-normal-interval-seconds", "15",
    "--hot-max-interval-seconds", "60",
    "--full-report-interval-seconds", "300",
    "--sleep-between-ticks"
)

Start-PowerShellStackProcess "capital-refresh-loop" @(
    "-NoProfile",
    "-ExecutionPolicy", "Bypass",
    "-File", "scripts\run_capital_refresh_loop.ps1",
    "-IntervalSeconds", "300",
    "-MaxTicks", "0"
)

Start-PowerShellStackProcess "artifact-cleanup-loop" @(
    "-NoProfile",
    "-ExecutionPolicy", "Bypass",
    "-File", "scripts\run_artifact_cleanup_loop.ps1",
    "-IntervalSeconds", "3600",
    "-MaxTicks", "0"
)

Start-StackProcess "trading-dashboard-loop" @(
    "main.py", "trading-dashboard-loop",
    "--profile-json", "config\trading_dashboard_snapshot_profile_v1.json",
    "--max-ticks", "0",
    "--interval-seconds", "1",
    "--sleep-between-ticks",
    "--snapshot-json-output-path", "exports\m220_trading_dashboard_snapshot.json",
    "--markdown-output-path", "exports\m220_trading_dashboard_snapshot.md",
    "--html-output-path", "exports\m220_trading_command_center.html",
    "--summary-json-output-path", "exports\m220_trading_dashboard_loop_summary.json",
    "--summary-markdown-output-path", "exports\m220_trading_dashboard_loop_summary.md",
    "--tick-log-jsonl-path", "exports\m220_trading_dashboard_loop_ticks.jsonl"
)

Write-Host "Realtime stack restart requested. Give the loops a few seconds to emit fresh artifacts."
