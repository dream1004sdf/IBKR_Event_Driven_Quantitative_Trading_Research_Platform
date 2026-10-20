param(
    [int]$OriginalRootPid = 256856,
    [string]$StartDate = "2025-01-01",
    [string]$EndDate = "2026-07-05",
    [string]$Symbols = "SPY,QQQ,IWM,DIA,SMH,SOXX,NVDA,MSFT,AAPL,AMZN,META,GOOGL,TSLA,AVGO,AMD,MU,TSM,USO,XLE,XOM,CVX,GLD,UUP,TLT,ITA,XAR,EWY,CIBR,HACK,DBA,WEAT",
    [string]$NewsSymbolTierConfig = "config\news_market_outcome_symbol_tiers_v1.json",
    [int]$NewsOutcomeIntradayLimit = 50000,
    [int]$NewsOutcomeEventIntradayLimit = 20000,
    [int]$NewsOutcomeIbkrClientId = 0,
    [int]$NewsOutcomeRepairPasses = 2,
    [int]$NewsOutcomeRepairIbkrTimeout = 60,
    [int]$NewsOutcomeCloseRetryBypassAfterFailures = 3,
    [int]$ReplayLimit = 25000,
    [int]$ReplayIntradayLimit = 250,
    [int]$OpenAiScoreLimit = 500,
    [int]$OpenAiAdjustmentNoteLimit = 100,
    [int]$CleanBackfillCycleIndex = 1,
    [ValidateSet("auto", "codex-assisted", "blind")]
    [string]$CleanBackfillCycleMode = "codex-assisted",
    [ValidateSet("balanced", "aggressive", "max")]
    [string]$TrainingPerformanceProfile = "aggressive",
    [int]$WatchIntervalSeconds = 10,
    [int]$WatchTimeoutHours = 18,
    [string]$ExistingRunDir = ""
)

$ErrorActionPreference = "Continue"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Resolve-Path (Join-Path $ScriptDir "..")
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not $ExistingRunDir) {
    $latest = Get-ChildItem -LiteralPath (Join-Path $RepoRoot "logs") -Directory -Filter "historical_backfill_training_2025_*" |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    if ($latest) {
        $ExistingRunDir = $latest.FullName
    } else {
        $ExistingRunDir = Join-Path $RepoRoot "logs"
    }
}
New-Item -ItemType Directory -Force -Path $ExistingRunDir | Out-Null
$InterceptLog = Join-Path $ExistingRunDir "intercept_repair_resume.log"

if ($NewsOutcomeIbkrClientId -le 0) {
    $NewsOutcomeIbkrClientId = Get-Random -Minimum 36000 -Maximum 62000
}
if ($WatchIntervalSeconds -lt 2) {
    $WatchIntervalSeconds = 2
}
if ($WatchTimeoutHours -lt 1) {
    $WatchTimeoutHours = 1
}
if ($NewsOutcomeCloseRetryBypassAfterFailures -lt 0) {
    $NewsOutcomeCloseRetryBypassAfterFailures = 0
}

function Write-InterceptLog {
    param([string]$Message)
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Write-Host $line
    Add-Content -LiteralPath $InterceptLog -Value $line
}

function Get-MatchingPythonProcesses {
    param([string]$Pattern)
    $matches = @()
    try {
        $matches += Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
            Where-Object { $_.CommandLine -like "*$Pattern*" -and $_.CommandLine -like "*ibkr_market_event_recorder*" }
        $matches += Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe'" |
            Where-Object { $_.CommandLine -like "*$Pattern*" -and $_.CommandLine -like "*ibkr_market_event_recorder*" }
    } catch {
        Write-InterceptLog "WARN process query failed for pattern=$Pattern error=$($_.Exception.Message)"
    }
    return $matches
}

function Stop-MatchingPythonProcesses {
    param([string]$Pattern)
    foreach ($proc in (Get-MatchingPythonProcesses -Pattern $Pattern)) {
        try {
            Write-InterceptLog "Stopping matching python process pid=$($proc.ProcessId) pattern=$Pattern"
            Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
        } catch {
            Write-InterceptLog "WARN stop matching process failed pid=$($proc.ProcessId) error=$($_.Exception.Message)"
        }
    }
}

function Invoke-MainStage {
    param(
        [string]$Name,
        [string[]]$Arguments,
        [switch]$AllowFailure
    )
    $safeName = ($Name -replace "[^A-Za-z0-9_.-]", "_")
    $stdout = Join-Path $ExistingRunDir ("{0}.stdout.log" -f $safeName)
    $stderr = Join-Path $ExistingRunDir ("{0}.stderr.log" -f $safeName)
    Write-InterceptLog "START $Name :: main.py $($Arguments -join ' ')"
    Push-Location $RepoRoot
    try {
        & $Python "main.py" @Arguments 1> $stdout 2> $stderr
        $exitCode = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    if ($exitCode -eq 0) {
        Write-InterceptLog "DONE  $Name"
    } elseif ($AllowFailure) {
        Write-InterceptLog "WARN  $Name exit=$exitCode; continuing"
    } else {
        throw "Stage failed: $Name exit=$exitCode"
    }
}

function Invoke-ResumeRunner {
    $resumeArgs = @(
        "-ExecutionPolicy", "Bypass",
        "-File", (Join-Path $ScriptDir "run_historical_backfill_training_2025.ps1"),
        "-StartDate", $StartDate,
        "-EndDate", $EndDate,
        "-Symbols", $Symbols,
        "-NewsSymbolTierConfig", $NewsSymbolTierConfig,
        "-CleanBackfillCycleIndex", "$CleanBackfillCycleIndex",
        "-CleanBackfillCycleMode", $CleanBackfillCycleMode,
        "-ResumeAfterNewsMarketOutcomeBackfill",
        "-ForceReplayScores",
        "-TrainingPerformanceMode",
        "-TrainingPerformanceProfile", $TrainingPerformanceProfile,
        "-ReplayLimit", "$ReplayLimit",
        "-ReplayIntradayLimit", "$ReplayIntradayLimit",
        "-OpenAiScoreLimit", "$OpenAiScoreLimit",
        "-OpenAiAdjustmentNoteLimit", "$OpenAiAdjustmentNoteLimit",
        "-NewsOutcomeIntradayLimit", "$NewsOutcomeIntradayLimit",
        "-NewsOutcomeEventIntradayLimit", "$NewsOutcomeEventIntradayLimit",
        "-NewsOutcomeIbkrClientId", "$($NewsOutcomeIbkrClientId + 80)",
        "-NewsOutcomeRepairPasses", "$NewsOutcomeRepairPasses",
        "-NewsOutcomeRepairIbkrTimeout", "$NewsOutcomeRepairIbkrTimeout",
        "-NewsOutcomeCloseRetryBypassAfterFailures", "$NewsOutcomeCloseRetryBypassAfterFailures"
    )
    $stdout = Join-Path $ExistingRunDir "resume_after_news_outcome_runner.stdout.log"
    $stderr = Join-Path $ExistingRunDir "resume_after_news_outcome_runner.stderr.log"
    Write-InterceptLog "START resume_after_news_market_outcome runner"
    $proc = Start-Process -FilePath "powershell.exe" -ArgumentList $resumeArgs -WorkingDirectory $RepoRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput $stdout -RedirectStandardError $stderr
    Write-InterceptLog "Resume runner started pid=$($proc.Id) stdout=$stdout stderr=$stderr"
}

$CoreJson = Join-Path $RepoRoot "exports\historical_backfill_news_outcome_intraday_core.json"
$deadline = (Get-Date).AddHours($WatchTimeoutHours)
Write-InterceptLog "Watcher started. original_root_pid=$OriginalRootPid core_json=$CoreJson"

while ((Get-Date) -lt $deadline) {
    if (Test-Path -LiteralPath $CoreJson) {
        Write-InterceptLog "Core intraday artifact detected; intercepting before event-window continues."
        break
    }
    $rootAlive = Get-Process -Id $OriginalRootPid -ErrorAction SilentlyContinue
    if (-not $rootAlive) {
        if (Test-Path -LiteralPath $CoreJson) {
            Write-InterceptLog "Original root ended and core artifact exists; continuing."
            break
        }
        throw "Original root process ended before the core intraday artifact appeared."
    }
    Start-Sleep -Seconds $WatchIntervalSeconds
}

if (-not (Test-Path -LiteralPath $CoreJson)) {
    throw "Timed out waiting for core intraday artifact: $CoreJson"
}

try {
    $rootAlive = Get-Process -Id $OriginalRootPid -ErrorAction SilentlyContinue
    if ($rootAlive) {
        Write-InterceptLog "Stopping original root process pid=$OriginalRootPid"
        Stop-Process -Id $OriginalRootPid -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 2
    Stop-MatchingPythonProcesses -Pattern "historical_backfill_news_outcome_intraday_event_progress"
    Stop-MatchingPythonProcesses -Pattern "historical_backfill_news_outcome_intraday_event_repair_pass"
    Stop-MatchingPythonProcesses -Pattern "run_historical_backfill_training_2025.ps1"

    for ($repairPass = 1; $repairPass -le $NewsOutcomeRepairPasses; $repairPass++) {
        Invoke-MainStage -Name "intercept_core_repair_pass_$repairPass" -AllowFailure -Arguments @(
            "news-market-outcome-backfill",
            "--start", $StartDate, "--end", $EndDate,
            "--horizons", "5M,15M,1H,CLOSE",
            "--price-source", "ibkr",
            "--only-missing",
            "--ibkr-client-id", "$($NewsOutcomeIbkrClientId + 10 + $repairPass)",
            "--ibkr-timeout", "$NewsOutcomeRepairIbkrTimeout",
            "--ibkr-bar-size", "1 min",
            "--ibkr-all-sessions",
            "--limit", "$NewsOutcomeIntradayLimit",
            "--symbol-tier-config", $NewsSymbolTierConfig,
            "--symbol-tier-filter", "core_always,macro_proxy",
            "--event-proxy-policy", "full-symbol",
            "--unknown-symbol-policy", "exclude",
            "--close-retry-bypass-after-failures", "$NewsOutcomeCloseRetryBypassAfterFailures",
            "--progress-jsonl-path", "logs\historical_backfill_news_outcome_intraday_core_repair_pass_${repairPass}_progress.jsonl",
            "--output", "exports\historical_backfill_news_outcome_intraday_core_repair_pass_${repairPass}.md",
            "--json-output-path", "exports\historical_backfill_news_outcome_intraday_core_repair_pass_${repairPass}.json"
        )
    }

    Invoke-MainStage -Name "intercept_event_window" -AllowFailure -Arguments @(
        "news-market-outcome-backfill",
        "--start", $StartDate, "--end", $EndDate,
        "--horizons", "5M,15M,1H,CLOSE",
        "--price-source", "ibkr",
        "--only-missing",
        "--ibkr-client-id", "$($NewsOutcomeIbkrClientId + 1)",
        "--ibkr-timeout", "30",
        "--ibkr-bar-size", "1 min",
        "--ibkr-all-sessions",
        "--limit", "$NewsOutcomeEventIntradayLimit",
        "--symbol-tier-config", $NewsSymbolTierConfig,
        "--symbol-tier-filter", "geopolitical_event_proxy,theme_event_proxy",
        "--event-proxy-policy", "matched-news-only",
        "--unknown-symbol-policy", "exclude",
        "--close-retry-bypass-after-failures", "$NewsOutcomeCloseRetryBypassAfterFailures",
        "--progress-jsonl-path", "logs\historical_backfill_news_outcome_intraday_event_progress.jsonl",
        "--output", "exports\historical_backfill_news_outcome_intraday_event.md",
        "--json-output-path", "exports\historical_backfill_news_outcome_intraday_event.json"
    )

    for ($repairPass = 1; $repairPass -le $NewsOutcomeRepairPasses; $repairPass++) {
        Invoke-MainStage -Name "intercept_event_window_repair_pass_$repairPass" -AllowFailure -Arguments @(
            "news-market-outcome-backfill",
            "--start", $StartDate, "--end", $EndDate,
            "--horizons", "5M,15M,1H,CLOSE",
            "--price-source", "ibkr",
            "--only-missing",
            "--ibkr-client-id", "$($NewsOutcomeIbkrClientId + 30 + $repairPass)",
            "--ibkr-timeout", "$NewsOutcomeRepairIbkrTimeout",
            "--ibkr-bar-size", "1 min",
            "--ibkr-all-sessions",
            "--limit", "$NewsOutcomeEventIntradayLimit",
            "--symbol-tier-config", $NewsSymbolTierConfig,
            "--symbol-tier-filter", "geopolitical_event_proxy,theme_event_proxy",
            "--event-proxy-policy", "matched-news-only",
            "--unknown-symbol-policy", "exclude",
            "--close-retry-bypass-after-failures", "$NewsOutcomeCloseRetryBypassAfterFailures",
            "--progress-jsonl-path", "logs\historical_backfill_news_outcome_intraday_event_repair_pass_${repairPass}_progress.jsonl",
            "--output", "exports\historical_backfill_news_outcome_intraday_event_repair_pass_${repairPass}.md",
            "--json-output-path", "exports\historical_backfill_news_outcome_intraday_event_repair_pass_${repairPass}.json"
        )
    }

    Invoke-ResumeRunner
    Write-InterceptLog "Intercept repair/resume handoff complete."
} catch {
    Write-InterceptLog "ERROR $($_.Exception.Message)"
    throw
}
