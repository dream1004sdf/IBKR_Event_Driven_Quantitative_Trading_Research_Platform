param(
    [string]$StartDate = "2025-01-01",
    [string]$EndDate = "2026-07-04",
    [string]$Symbols = "SPY,QQQ,IWM,DIA,SMH,SOXX,NVDA,MSFT,AAPL,AMZN,META,GOOGL,TSLA,AVGO,AMD,MU,TSM,USO,XLE,XOM,CVX,GLD,UUP,TLT,ITA,XAR,EWY,CIBR,HACK,DBA,WEAT",
    [string]$IbkrNewsProviders = "BZ,DJ-N,DJ-RT,DJ-RTA,DJ-RTE,DJ-RTG,DJNL",
    [int]$MarketauxWindowDays = 7,
    [int]$MarketauxLimitPerWindow = 100,
    [int]$IbkrLimitPerSymbol = 500,
    [int]$IbkrMaxPagesPerSymbol = 5,
    [int]$IbkrNewsTimeout = 300,
    [int]$IbkrNewsClientId = 0,
    [int]$IbkrReplayOutcomeClientId = 0,
    [int]$ReplayLimit = 25000,
    [int]$ReplayIntradayLimit = 250,
    [string]$NewsSymbolTierConfig = "config\news_market_outcome_symbol_tiers_v1.json",
    [int]$CleanBackfillCycleIndex = 1,
    [ValidateSet("auto", "codex-assisted", "blind", "source-locked-blind-reclass")]
    [string]$CleanBackfillCycleMode = "auto",
    [string]$CleanBackfillCycleLedgerPath = "exports\clean_backfill_cycle_correction_ledger.jsonl",
    [switch]$SkipNewsMarketOutcomeBackfill,
    [int]$NewsOutcomeIntradayLimit = 50000,
    [int]$NewsOutcomeEventIntradayLimit = 20000,
    [int]$NewsOutcomeIbkrClientId = 0,
    [switch]$SkipNewsOutcomeRepairPass,
    [int]$NewsOutcomeRepairPasses = 2,
    [int]$NewsOutcomeRepairIbkrTimeout = 60,
    [int]$NewsOutcomeCloseRetryBypassAfterFailures = 3,
    [int]$OpenAiScoreLimit = 500,
    [int]$ReplayAdjustmentNoteLimit = 5000,
    [int]$OpenAiAdjustmentNoteLimit = -1,
    [ValidateSet("full", "dynamic")]
    [string]$ExternalSignalConversionMode = "full",
    [int]$ExternalSignalLimit = 100000,
    [switch]$ParallelExternalSignalBuild,
    [int]$ExternalSignalMaxParallel = 0,
    [int]$ExternalSignalShardMultiplier = 4,
    [int]$BenzingaExternalSignalDynamicLimit = 5000,
    [int]$BenzingaExternalSignalPerSymbolLimit = 180,
    [int]$BenzingaExternalSignalPriorityPerSymbolLimit = 600,
    [int]$DowJonesExternalSignalDynamicLimit = 1500,
    [int]$DowJonesExternalSignalPerSymbolLimit = 100,
    [int]$DowJonesExternalSignalPriorityPerSymbolLimit = 250,
    [string]$ExternalSignalPrioritySymbols = "SPY,QQQ,NVDA,TSM,USO,GLD,SMH,SOXX,MSFT,AAPL,TSLA,XOM,CVX,MU,AVGO",
    [switch]$SkipNetworkBackfill,
    [switch]$SkipOpenAiReplayScoring,
    [switch]$ForceReplayScores,
    [switch]$ReuseExistingDb,
    [switch]$ResumeAfterNewsMarketOutcomeBackfill,
    [switch]$CleanDerivedRebuild,
    [switch]$ResetMarketauxSignals,
    [switch]$VacuumAfterDerivedReset,
    [switch]$TrainingPerformanceMode,
    [ValidateSet("balanced", "aggressive", "max")]
    [string]$TrainingPerformanceProfile = "balanced",
    [int]$MaxParallelTrainingStages = 0,
    [int]$StrategyAdaptiveLimit = 0
)

$ErrorActionPreference = "Continue"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

$CanonicalCycleStartDate = "2025-01-01"
try {
    $ParsedStartDate = [DateTime]::Parse($StartDate, [System.Globalization.CultureInfo]::InvariantCulture)
} catch {
    throw "Historical backfill/training cycles must use a parseable StartDate equal to $CanonicalCycleStartDate. received=$StartDate"
}
if ($ParsedStartDate.ToString("yyyy-MM-dd") -ne $CanonicalCycleStartDate) {
    throw "Historical backfill/training cycles must start at $CanonicalCycleStartDate for comparable blind walk-forward learning. received=$StartDate"
}
$StartDate = $CanonicalCycleStartDate

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Resolve-Path (Join-Path $ScriptDir "..")
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$RunId = "historical_backfill_training_2025_{0}" -f (Get-Date -Format "yyyyMMdd_HHmmss")
$LogDir = Join-Path $RepoRoot ("logs\{0}" -f $RunId)
$ExportDir = Join-Path $RepoRoot "exports"
$SummaryPath = Join-Path $ExportDir "historical_backfill_training_2025_summary.json"
$MarkdownPath = Join-Path $ExportDir "historical_backfill_training_2025_summary.md"
$ClientIdSeed = Get-Random -Minimum 25000 -Maximum 60000
if ($IbkrNewsClientId -le 0) {
    $IbkrNewsClientId = $ClientIdSeed
}
if ($IbkrReplayOutcomeClientId -le 0) {
    $IbkrReplayOutcomeClientId = $ClientIdSeed + 1
}
if ($NewsOutcomeIbkrClientId -le 0) {
    $NewsOutcomeIbkrClientId = $ClientIdSeed + 20
}
if ($CleanBackfillCycleIndex -lt 1) {
    $CleanBackfillCycleIndex = 1
}
if ($NewsOutcomeRepairPasses -lt 0) {
    $NewsOutcomeRepairPasses = 0
}
if ($NewsOutcomeRepairIbkrTimeout -lt 1) {
    $NewsOutcomeRepairIbkrTimeout = 60
}
if ($NewsOutcomeCloseRetryBypassAfterFailures -lt 0) {
    $NewsOutcomeCloseRetryBypassAfterFailures = 0
}
$EffectiveCleanBackfillCycleMode = $CleanBackfillCycleMode
if ($CleanBackfillCycleMode -eq "auto") {
    if ($CleanBackfillCycleIndex -le 1) {
        $EffectiveCleanBackfillCycleMode = "codex-assisted"
    } else {
        $EffectiveCleanBackfillCycleMode = "blind"
    }
} elseif ($CleanBackfillCycleMode -eq "codex-assisted" -and $CleanBackfillCycleIndex -gt 1) {
    $EffectiveCleanBackfillCycleMode = "blind"
}
$SourceLockedBlindReclassMode = $EffectiveCleanBackfillCycleMode -eq "source-locked-blind-reclass"
$CleanBackfillDedupGuardApplied = $false
if ($CleanBackfillCycleIndex -gt 1) {
    $CleanBackfillDedupGuardApplied = $true
    $CleanDerivedRebuild = $false
    $ResetMarketauxSignals = $false
    $SkipNetworkBackfill = $true
    if ($SourceLockedBlindReclassMode) {
        $ReuseExistingDb = $false
        $ForceReplayScores = $true
    } else {
        $SkipNewsMarketOutcomeBackfill = $true
        $ReuseExistingDb = $true
    }
}
if ($ResumeAfterNewsMarketOutcomeBackfill) {
    $SkipNetworkBackfill = $true
    $SkipNewsMarketOutcomeBackfill = $true
    $CleanDerivedRebuild = $false
    $ResetMarketauxSignals = $false
}
$LogicalCpuCount = [Math]::Max(1, [Environment]::ProcessorCount)
if ($MaxParallelTrainingStages -le 0) {
    switch ($TrainingPerformanceProfile) {
        "balanced" { $MaxParallelTrainingStages = [Math]::Max(1, [Math]::Min(3, $LogicalCpuCount - 2)) }
        "aggressive" { $MaxParallelTrainingStages = [Math]::Max(2, [Math]::Min(5, $LogicalCpuCount - 1)) }
        "max" { $MaxParallelTrainingStages = [Math]::Max(2, [Math]::Min(8, $LogicalCpuCount)) }
    }
}
if ($ExternalSignalMaxParallel -le 0) {
    switch ($TrainingPerformanceProfile) {
        "balanced" { $ExternalSignalMaxParallel = [Math]::Max(1, [Math]::Min(3, $LogicalCpuCount - 2)) }
        "aggressive" { $ExternalSignalMaxParallel = [Math]::Max(2, [Math]::Min(5, $LogicalCpuCount - 1)) }
        "max" { $ExternalSignalMaxParallel = [Math]::Max(2, [Math]::Min(8, $LogicalCpuCount)) }
    }
}
if ($ExternalSignalLimit -lt 1) {
    $ExternalSignalLimit = 100000
}
if ($StrategyAdaptiveLimit -le 0) {
    if ($TrainingPerformanceMode) {
        switch ($TrainingPerformanceProfile) {
            "balanced" { $StrategyAdaptiveLimit = [Math]::Min($ReplayLimit, 5000) }
            "aggressive" { $StrategyAdaptiveLimit = [Math]::Min($ReplayLimit, 12000) }
            "max" { $StrategyAdaptiveLimit = $ReplayLimit }
        }
    } else {
        $StrategyAdaptiveLimit = $ReplayLimit
    }
}
if ($CleanDerivedRebuild) {
    $ReuseExistingDb = $false
    $ForceReplayScores = $true
    if (-not $ResetMarketauxSignals) {
        $SkipNetworkBackfill = $true
    }
}
if ($ReuseExistingDb) {
    $SkipNetworkBackfill = $true
    $SkipOpenAiReplayScoring = $true
}
if ($OpenAiAdjustmentNoteLimit -lt 0) {
    if ($CleanDerivedRebuild -and -not $SkipOpenAiReplayScoring -and $OpenAiScoreLimit -gt 0) {
        $OpenAiAdjustmentNoteLimit = [Math]::Min(100, $OpenAiScoreLimit)
    } else {
        $OpenAiAdjustmentNoteLimit = 0
    }
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
New-Item -ItemType Directory -Force -Path $ExportDir | Out-Null

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Python runtime not found: $Python"
}

function Write-Log {
    param([string]$Message)
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Write-Host $line
    Add-Content -LiteralPath (Join-Path $LogDir "run.log") -Value $line
}

function Invoke-Stage {
    param(
        [string]$Name,
        [string[]]$Arguments,
        [switch]$AllowFailure
    )
    $safeName = ($Name -replace "[^A-Za-z0-9_.-]", "_")
    $stdout = Join-Path $LogDir ("{0}.stdout.log" -f $safeName)
    $stderr = Join-Path $LogDir ("{0}.stderr.log" -f $safeName)
    $started = Get-Date
    Write-Log "START $Name :: main.py $($Arguments -join ' ')"
    & $Python "main.py" @Arguments 1> $stdout 2> $stderr
    $exitCode = $LASTEXITCODE
    $ended = Get-Date
    $record = [ordered]@{
        name = $Name
        exit_code = $exitCode
        allow_failure = [bool]$AllowFailure
        started_at = $started.ToUniversalTime().ToString("o")
        ended_at = $ended.ToUniversalTime().ToString("o")
        duration_seconds = [math]::Round(($ended - $started).TotalSeconds, 3)
        stdout_log = $stdout
        stderr_log = $stderr
        command = "main.py $($Arguments -join ' ')"
    }
    $script:StageRecords.Add([pscustomobject]$record) | Out-Null
    if ($exitCode -eq 0) {
        Write-Log "DONE  $Name"
    } elseif ($AllowFailure) {
        Write-Log "WARN  $Name exit=$exitCode; continuing"
    } else {
        Write-Log "FAIL  $Name exit=$exitCode; continuing so the summary captures downstream blockers"
    }
}

function Invoke-JsonStage {
    param(
        [string]$Name,
        [string[]]$Arguments
    )
    Invoke-Stage -Name $Name -Arguments $Arguments
}

function Invoke-StageBatch {
    param(
        [object[]]$Stages,
        [int]$MaxParallel = 2
    )
    $MaxParallel = [Math]::Max(1, [int]$MaxParallel)
    $pending = New-Object System.Collections.Queue
    foreach ($stage in $Stages) {
        $pending.Enqueue($stage)
    }
    $running = @()
    while ($pending.Count -gt 0 -or $running.Count -gt 0) {
        while ($pending.Count -gt 0 -and $running.Count -lt $MaxParallel) {
            $stage = $pending.Dequeue()
            $name = [string]$stage.Name
            $arguments = [string[]]$stage.Arguments
            $allowFailure = [bool]$stage.AllowFailure
            $safeName = ($name -replace "[^A-Za-z0-9_.-]", "_")
            $stdout = Join-Path $LogDir ("{0}.stdout.log" -f $safeName)
            $stderr = Join-Path $LogDir ("{0}.stderr.log" -f $safeName)
            $started = Get-Date
            Write-Log "START parallel $name :: main.py $($arguments -join ' ')"
            $job = Start-Job -ScriptBlock {
                param(
                    [string]$PythonPath,
                    [string]$WorkingDirectory,
                    [string[]]$JobArguments,
                    [string]$StdoutPath,
                    [string]$StderrPath
                )
                Set-Location -LiteralPath $WorkingDirectory
                & $PythonPath "main.py" @JobArguments 1> $StdoutPath 2> $StderrPath
                return $LASTEXITCODE
            } -ArgumentList $Python, $RepoRoot, $arguments, $stdout, $stderr
            $running += [pscustomobject]@{
                Job = $job
                Name = $name
                Arguments = $arguments
                AllowFailure = $allowFailure
                Started = $started
                Stdout = $stdout
                Stderr = $stderr
            }
        }
        Start-Sleep -Seconds 1
        $done = @($running | Where-Object { $_.Job.State -in @("Completed", "Failed", "Stopped") })
        foreach ($item in $done) {
            $ended = Get-Date
            $exitCode = 999
            if ($item.Job.State -eq "Completed") {
                $received = @(Receive-Job -Job $item.Job -ErrorAction SilentlyContinue)
                if ($received.Count -gt 0 -and $null -ne $received[-1]) {
                    $exitCode = [int]$received[-1]
                } else {
                    $exitCode = 0
                }
            } else {
                Receive-Job -Job $item.Job -ErrorAction SilentlyContinue | Out-Null
            }
            Remove-Job -Job $item.Job -Force -ErrorAction SilentlyContinue
            $record = [ordered]@{
                name = $item.Name
                exit_code = $exitCode
                allow_failure = [bool]$item.AllowFailure
                parallel_batch = $true
                started_at = $item.Started.ToUniversalTime().ToString("o")
                ended_at = $ended.ToUniversalTime().ToString("o")
                duration_seconds = [math]::Round(($ended - $item.Started).TotalSeconds, 3)
                stdout_log = $item.Stdout
                stderr_log = $item.Stderr
                command = "main.py $($item.Arguments -join ' ')"
            }
            $script:StageRecords.Add([pscustomobject]$record) | Out-Null
            if ($exitCode -eq 0) {
                Write-Log "DONE  parallel $($item.Name)"
            } elseif ($item.AllowFailure) {
                Write-Log "WARN  parallel $($item.Name) exit=$exitCode; continuing"
            } else {
                Write-Log "FAIL  parallel $($item.Name) exit=$exitCode; continuing so the summary captures downstream blockers"
            }
            $running = @($running | Where-Object { $_.Job.Id -ne $item.Job.Id })
        }
    }
}

function Split-SymbolsForParallelStages {
    param(
        [string]$SymbolsValue,
        [int]$MaxShards
    )
    $items = @($SymbolsValue -split "," | ForEach-Object { $_.Trim().ToUpperInvariant() } | Where-Object { $_ })
    if ($items.Count -eq 0) {
        return @()
    }
    $shardCount = [Math]::Max(1, [Math]::Min([int]$MaxShards, $items.Count))
    $buckets = @()
    for ($i = 0; $i -lt $shardCount; $i++) {
        $buckets += ,(New-Object System.Collections.Generic.List[string])
    }
    for ($i = 0; $i -lt $items.Count; $i++) {
        $bucketIndex = $i % $shardCount
        $buckets[$bucketIndex].Add([string]$items[$i])
    }
    $result = @()
    foreach ($bucket in $buckets) {
        if ($bucket.Count -gt 0) {
            $result += ,($bucket -join ",")
        }
    }
    return $result
}

function New-ExternalSignalStage {
    param(
        [string]$Name,
        [string]$Command,
        [string]$SymbolShard,
        [int]$Limit
    )
    $args = @(
        $Command,
        "--start", $StartDate, "--end", $EndDate,
        "--symbols", $SymbolShard,
        "--limit", "$Limit",
        "--include-non-geopolitical"
    )
    if ($ExternalSignalConversionMode -eq "dynamic") {
        if ($Command -eq "benzinga-external-signals-build") {
            $args += @(
                "--dynamic-total-limit", "$BenzingaExternalSignalDynamicLimit",
                "--dynamic-per-symbol-limit", "$BenzingaExternalSignalPerSymbolLimit",
                "--dynamic-priority-per-symbol-limit", "$BenzingaExternalSignalPriorityPerSymbolLimit",
                "--dynamic-priority-symbols", $ExternalSignalPrioritySymbols
            )
        } else {
            $args += @(
                "--dynamic-total-limit", "$DowJonesExternalSignalDynamicLimit",
                "--dynamic-per-symbol-limit", "$DowJonesExternalSignalPerSymbolLimit",
                "--dynamic-priority-per-symbol-limit", "$DowJonesExternalSignalPriorityPerSymbolLimit",
                "--dynamic-priority-symbols", $ExternalSignalPrioritySymbols
            )
        }
    }
    return [pscustomobject]@{
        Name = $Name
        Arguments = $args
        AllowFailure = $false
    }
}

function Invoke-CleanDerivedRebuildReset {
    $name = "clean_derived_rebuild_reset"
    $safeName = $name
    $stdout = Join-Path $LogDir ("{0}.stdout.log" -f $safeName)
    $stderr = Join-Path $LogDir ("{0}.stderr.log" -f $safeName)
    $reportPath = Join-Path $ExportDir "historical_backfill_clean_derived_reset.json"
    $backupDir = Join-Path $RepoRoot "backups"
    $started = Get-Date
    $script:CleanDerivedResetReportPath = $reportPath
    Write-Log "START $name :: backup DB and clear rebuildable derived artifacts"
    $code = @'
import json
import sys
from pathlib import Path

from recorder.db_maintenance import clean_derived_rebuild_reset

db_path = Path(sys.argv[1])
backup_dir = Path(sys.argv[2])
report_path = Path(sys.argv[3])
reset_marketaux = sys.argv[4].lower() == 'true'
vacuum_after_reset = sys.argv[5].lower() == 'true'

report = clean_derived_rebuild_reset(
    db_path,
    backup_dir,
    report_path=report_path,
    reset_marketaux_signals=reset_marketaux,
    vacuum_after_reset=vacuum_after_reset,
)
print(json.dumps({
    'version': report.get('version'),
    'backup_path': (report.get('backup') or {}).get('backup_path'),
    'report_path': str(report_path),
    'operations': report.get('operations'),
    'reset_marketaux_signals': report.get('reset_marketaux_signals'),
}, indent=2, ensure_ascii=False))
'@
    $code | & $Python - "market_events.db" $backupDir $reportPath "$([bool]$ResetMarketauxSignals)" "$([bool]$VacuumAfterDerivedReset)" 1> $stdout 2> $stderr
    $exitCode = $LASTEXITCODE
    $ended = Get-Date
    if (Test-Path -LiteralPath $reportPath) {
        try {
            $resetReport = Get-Content -LiteralPath $reportPath -Raw | ConvertFrom-Json
            $script:CleanDerivedBackupPath = $resetReport.backup.backup_path
        } catch {
            $script:CleanDerivedBackupPath = $null
        }
    }
    $record = [ordered]@{
        name = $name
        exit_code = $exitCode
        allow_failure = $false
        started_at = $started.ToUniversalTime().ToString("o")
        ended_at = $ended.ToUniversalTime().ToString("o")
        duration_seconds = [math]::Round(($ended - $started).TotalSeconds, 3)
        stdout_log = $stdout
        stderr_log = $stderr
        command = "python -c recorder.db_maintenance.clean_derived_rebuild_reset"
        report_path = $reportPath
        backup_path = $script:CleanDerivedBackupPath
    }
    $script:StageRecords.Add([pscustomobject]$record) | Out-Null
    if ($exitCode -eq 0) {
        Write-Log "DONE  $name backup=$script:CleanDerivedBackupPath report=$reportPath"
    } else {
        Write-Log "FAIL  $name exit=$exitCode; clean derived rebuild cannot continue safely"
        throw "Clean derived rebuild reset failed. See $stderr"
    }
}

function Get-DbSummary {
    $code = @'
import json, sqlite3
from pathlib import Path
db = Path("market_events.db")
conn = sqlite3.connect(db)
conn.row_factory = sqlite3.Row
tables = [
    "news_articles","external_market_signals","geopolitical_signal_outcomes",
    "geopolitical_adaptive_asset_map","standing_market_regimes","standing_regime_outcomes",
    "replay_corpus","replay_market_outcomes","replay_model_scores",
    "technical_indicators","fred_observations","international_macro_observations",
    "market_baselines","market_reactions","m241_8_cross_asset_expression_outcomes",
    "m250_5_gate_severity_outcomes"
]
out = {}
for table in tables:
    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    if not exists:
        out[table] = {"exists": False, "count": None}
        continue
    count = conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
    cols = [row["name"] for row in conn.execute(f"PRAGMA table_info({table})")]
    date_col = next((c for c in ["published_at","created_at","updated_at","event_at","replay_timestamp","captured_at","observation_date","as_of_date"] if c in cols), None)
    item = {"exists": True, "count": int(count)}
    if date_col:
        row = conn.execute(f"SELECT MIN({date_col}) AS min_at, MAX({date_col}) AS max_at FROM {table}").fetchone()
        item["date_column"] = date_col
        item["min_at"] = row["min_at"]
        item["max_at"] = row["max_at"]
    out[table] = item
conn.close()
print(json.dumps(out, indent=2, ensure_ascii=False))
'@
    Push-Location $RepoRoot
    try {
        return ($code | & $Python - | Out-String)
    } finally {
        Pop-Location
    }
}

$script:StageRecords = New-Object System.Collections.ArrayList
$script:CleanDerivedResetReportPath = $null
$script:CleanDerivedBackupPath = $null
$StartedAt = Get-Date
Write-Log "Historical backfill/training started. run_id=$RunId start=$StartDate end=$EndDate"
Write-Log "Clean backfill cycle index=$CleanBackfillCycleIndex requested_mode=$CleanBackfillCycleMode effective_mode=$EffectiveCleanBackfillCycleMode dedup_guard=$CleanBackfillDedupGuardApplied"
if ($ResumeAfterNewsMarketOutcomeBackfill) {
    Write-Log "Resume-after-news-market-outcome mode enabled: skipping source conversion and news outcome backfill, then continuing at audit/training stages."
}
if ($CleanBackfillDedupGuardApplied -and -not $SourceLockedBlindReclassMode) {
    Write-Log "Cycle >=2 detected: skipping duplicate clean/source backfill and reusing the existing DB/artifacts for blind training/report cycles."
}
if ($SourceLockedBlindReclassMode) {
    Write-Log "Cycle >=2 source-locked blind reclass mode enabled: raw/source network collection is locked, but classification, ticker tiers, evidence/replay, and tuning stages are regenerated."
}

Push-Location $RepoRoot
try {
    Invoke-JsonStage "db_audit_preflight" @("db-audit")

    if ($CleanDerivedRebuild) {
        Invoke-CleanDerivedRebuildReset
    }

    if (-not $SkipNetworkBackfill) {
        Invoke-Stage "fred_us_core" @("fred-universe-sync", "--universe", "us_core", "--start", $StartDate, "--end", $EndDate, "--limit-per-series", "5000") -AllowFailure
        Invoke-Stage "fred_global_core" @("fred-universe-sync", "--universe", "global_core", "--start", $StartDate, "--end", $EndDate, "--limit-per-series", "5000") -AllowFailure
        Invoke-Stage "fred_international_core" @("fred-universe-sync", "--universe", "international_core", "--start", $StartDate, "--end", $EndDate, "--limit-per-series", "5000") -AllowFailure
        Invoke-Stage "international_macro_all" @("international-macro-sync", "--source", "all", "--start", $StartDate, "--end", $EndDate) -AllowFailure
        Invoke-Stage "macro_rss_snapshot" @("macro-sync", "--source", "all", "--limit", "500") -AllowFailure
        Invoke-Stage "marketaux_backfill" @(
            "external-market-backfill", "--source", "marketaux",
            "--start", $StartDate, "--end", $EndDate,
            "--window-days", "$MarketauxWindowDays",
            "--limit-per-window", "$MarketauxLimitPerWindow",
            "--pause-seconds", "0.75",
            "--marketaux-mode", "hybrid",
            "--marketaux-entity-types", "equity,index,etf",
            "--marketaux-countries", "us",
            "--marketaux-symbols", $Symbols
        ) -AllowFailure
        Invoke-Stage "ibkr_news_backfill" @(
            "ibkr-news-backfill", "--symbols", $Symbols,
            "--providers", $IbkrNewsProviders,
            "--start", $StartDate, "--end", $EndDate,
            "--ibkr-client-id", "$IbkrNewsClientId",
            "--limit-per-symbol", "$IbkrLimitPerSymbol",
            "--max-pages-per-symbol", "$IbkrMaxPagesPerSymbol",
            "--timeout", "$IbkrNewsTimeout"
        ) -AllowFailure
    } else {
        Write-Log "Skipping network backfill by request."
    }

    if (-not $ReuseExistingDb) {
        if (-not $ResumeAfterNewsMarketOutcomeBackfill) {
            if ($ParallelExternalSignalBuild) {
                $targetExternalSignalShards = [Math]::Max(
                    $ExternalSignalMaxParallel,
                    $ExternalSignalMaxParallel * [Math]::Max(1, $ExternalSignalShardMultiplier)
                )
                $symbolShards = @(Split-SymbolsForParallelStages -SymbolsValue $Symbols -MaxShards $targetExternalSignalShards)
                $shardCount = [Math]::Max(1, $symbolShards.Count)
                $perShardExternalSignalLimit = $ExternalSignalLimit
                Write-Log "External signal conversion parallel mode: shards=$shardCount max_parallel=$ExternalSignalMaxParallel shard_multiplier=$ExternalSignalShardMultiplier per_shard_limit=$perShardExternalSignalLimit mode=$ExternalSignalConversionMode"
                $benzingaStages = @()
                $dowJonesStages = @()
                for ($i = 0; $i -lt $symbolShards.Count; $i++) {
                    $suffix = "{0:D2}" -f ($i + 1)
                    $benzingaStages += ,(New-ExternalSignalStage -Name "benzinga_external_signals_build_$suffix" -Command "benzinga-external-signals-build" -SymbolShard $symbolShards[$i] -Limit $perShardExternalSignalLimit)
                    $dowJonesStages += ,(New-ExternalSignalStage -Name "dow_jones_external_signals_build_$suffix" -Command "dow-jones-external-signals-build" -SymbolShard $symbolShards[$i] -Limit $perShardExternalSignalLimit)
                }
                Invoke-StageBatch -Stages $benzingaStages -MaxParallel $ExternalSignalMaxParallel
                Invoke-StageBatch -Stages $dowJonesStages -MaxParallel $ExternalSignalMaxParallel
            } else {
                $benzingaStage = New-ExternalSignalStage -Name "benzinga_external_signals_build" -Command "benzinga-external-signals-build" -SymbolShard $Symbols -Limit $ExternalSignalLimit
                $dowJonesStage = New-ExternalSignalStage -Name "dow_jones_external_signals_build" -Command "dow-jones-external-signals-build" -SymbolShard $Symbols -Limit $ExternalSignalLimit
                Invoke-JsonStage "benzinga_external_signals_build" ([string[]]$benzingaStage.Arguments)
                Invoke-JsonStage "dow_jones_external_signals_build" ([string[]]$dowJonesStage.Arguments)
            }
            Invoke-JsonStage "m253_news_lineage_v2_rebuild" @(
                "news-lineage-v2-rebuild",
                "--start", $StartDate, "--end", $EndDate,
                "--bucket-minutes", "60",
                "--output", "exports\m253_news_lineage_v2_rebuild.md",
                "--json-output-path", "exports\m253_news_lineage_v2_rebuild.json"
            )
            Invoke-JsonStage "emergency_breaking_backfill" @("emergency-breaking-backfill", "--start", $StartDate, "--end", $EndDate, "--source", "all", "--limit", "100000", "--min-urgency", "medium")
            Invoke-Stage "news_subscription_mesh" @("news-subscription-mesh", "--profile-json", "config\news_subscription_mesh_profile_v1.json", "--output", "exports\m235_news_subscription_mesh.md", "--json-output-path", "exports\m235_news_subscription_mesh.json") -AllowFailure
            Invoke-Stage "cross_provider_event_roles" @("cross-provider-event-roles", "--profile-json", "config\cross_provider_event_roles_profile_v1.json", "--lookback-minutes", "900000", "--output", "exports\m236_cross_provider_event_roles_backfill.md", "--json-output-path", "exports\m236_cross_provider_event_roles_backfill.json") -AllowFailure
            Invoke-Stage "news_symbol_tier_report_during_clean_backfill" @(
                "news-symbol-tier-report",
                "--start", $StartDate, "--end", $EndDate,
                "--symbol-tier-config", $NewsSymbolTierConfig,
                "--output", "exports\historical_backfill_news_symbol_tiers.md",
                "--json-output-path", "exports\historical_backfill_news_symbol_tiers.json"
            ) -AllowFailure
            Invoke-Stage "clean_signal_classification_audit" @(
                "clean-signal-classification-audit",
                "--start", $StartDate, "--end", $EndDate,
                "--limit", "100000",
                "--symbol-tier-report-path", "exports\historical_backfill_news_symbol_tiers.json",
                "--output", "exports\clean_backfill_signal_classification_audit.md",
                "--json-output-path", "exports\clean_backfill_signal_classification_audit.json",
                "--notes-jsonl-path", "exports\clean_backfill_signal_classification_notes.jsonl"
            ) -AllowFailure
            Invoke-Stage "clean_cycle_audit_evidence_conversion" @(
                "clean-backfill-cycle-audit",
                "--start", $StartDate, "--end", $EndDate,
                "--cycle-index", "$CleanBackfillCycleIndex",
                "--cycle-mode", $EffectiveCleanBackfillCycleMode,
                "--phase", "evidence_conversion",
                "--symbol-tier-config", $NewsSymbolTierConfig,
                "--correction-ledger-path", $CleanBackfillCycleLedgerPath,
                "--output", "exports\historical_backfill_cycle_audit_evidence.md",
                "--json-output-path", "exports\historical_backfill_cycle_audit_evidence.json"
            ) -AllowFailure

            if (-not $SkipNewsMarketOutcomeBackfill) {
                Invoke-Stage "news_market_outcome_next_day_tiered" @(
                    "news-market-outcome-backfill",
                    "--start", $StartDate, "--end", $EndDate,
                    "--horizons", "NEXT_DAY",
                    "--price-source", "yahoo",
                    "--only-missing",
                    "--limit", "100000",
                    "--symbol-tier-config", $NewsSymbolTierConfig,
                    "--symbol-tier-filter", "core_always,macro_proxy,geopolitical_event_proxy,theme_event_proxy,opportunistic_single_stock",
                    "--unknown-symbol-policy", "exclude",
                    "--progress-jsonl-path", "logs\historical_backfill_news_outcome_next_day_progress.jsonl",
                    "--output", "exports\historical_backfill_news_outcome_next_day.md",
                    "--json-output-path", "exports\historical_backfill_news_outcome_next_day.json"
                ) -AllowFailure
                if (-not $SkipNewsOutcomeRepairPass -and $NewsOutcomeRepairPasses -gt 0) {
                    for ($repairPass = 1; $repairPass -le $NewsOutcomeRepairPasses; $repairPass++) {
                        Invoke-Stage "news_market_outcome_next_day_tiered_repair_pass_$repairPass" @(
                            "news-market-outcome-backfill",
                            "--start", $StartDate, "--end", $EndDate,
                            "--horizons", "NEXT_DAY",
                            "--price-source", "yahoo",
                            "--only-missing",
                            "--limit", "100000",
                            "--symbol-tier-config", $NewsSymbolTierConfig,
                            "--symbol-tier-filter", "core_always,macro_proxy,geopolitical_event_proxy,theme_event_proxy,opportunistic_single_stock",
                            "--unknown-symbol-policy", "exclude",
                            "--progress-jsonl-path", "logs\historical_backfill_news_outcome_next_day_repair_pass_${repairPass}_progress.jsonl",
                            "--output", "exports\historical_backfill_news_outcome_next_day_repair_pass_${repairPass}.md",
                            "--json-output-path", "exports\historical_backfill_news_outcome_next_day_repair_pass_${repairPass}.json"
                        ) -AllowFailure
                    }
                }
                Invoke-Stage "news_market_outcome_intraday_core_macro" @(
                    "news-market-outcome-backfill",
                    "--start", $StartDate, "--end", $EndDate,
                    "--horizons", "5M,15M,1H,CLOSE",
                    "--price-source", "ibkr",
                    "--only-missing",
                    "--ibkr-client-id", "$NewsOutcomeIbkrClientId",
                    "--ibkr-timeout", "30",
                    "--ibkr-bar-size", "1 min",
                    "--ibkr-all-sessions",
                    "--limit", "$NewsOutcomeIntradayLimit",
                    "--symbol-tier-config", $NewsSymbolTierConfig,
                    "--symbol-tier-filter", "core_always,macro_proxy",
                    "--event-proxy-policy", "full-symbol",
                    "--unknown-symbol-policy", "exclude",
                    "--close-retry-bypass-after-failures", "$NewsOutcomeCloseRetryBypassAfterFailures",
                    "--progress-jsonl-path", "logs\historical_backfill_news_outcome_intraday_core_progress.jsonl",
                    "--output", "exports\historical_backfill_news_outcome_intraday_core.md",
                    "--json-output-path", "exports\historical_backfill_news_outcome_intraday_core.json"
                ) -AllowFailure
                if (-not $SkipNewsOutcomeRepairPass -and $NewsOutcomeRepairPasses -gt 0) {
                    for ($repairPass = 1; $repairPass -le $NewsOutcomeRepairPasses; $repairPass++) {
                        Invoke-Stage "news_market_outcome_intraday_core_macro_repair_pass_$repairPass" @(
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
                        ) -AllowFailure
                    }
                }
                Invoke-Stage "news_market_outcome_intraday_event_window" @(
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
                ) -AllowFailure
                if (-not $SkipNewsOutcomeRepairPass -and $NewsOutcomeRepairPasses -gt 0) {
                    for ($repairPass = 1; $repairPass -le $NewsOutcomeRepairPasses; $repairPass++) {
                        Invoke-Stage "news_market_outcome_intraday_event_window_repair_pass_$repairPass" @(
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
                        ) -AllowFailure
                    }
                }
            } else {
                Write-Log "Skipping tiered news market outcome backfill by request."
            }
        } else {
            Write-Log "Resume-after-news-market-outcome mode: source conversion and tiered news market outcome backfill are already complete."
        }
        Invoke-Stage "clean_cycle_audit_news_outcomes" @(
            "clean-backfill-cycle-audit",
            "--start", $StartDate, "--end", $EndDate,
            "--cycle-index", "$CleanBackfillCycleIndex",
            "--cycle-mode", $EffectiveCleanBackfillCycleMode,
            "--phase", "news_outcome_backfill",
            "--symbol-tier-config", $NewsSymbolTierConfig,
            "--correction-ledger-path", $CleanBackfillCycleLedgerPath,
            "--output", "exports\historical_backfill_cycle_audit_news_outcomes.md",
            "--json-output-path", "exports\historical_backfill_cycle_audit_news_outcomes.json"
        ) -AllowFailure

        Invoke-Stage "technical_indicators_yahoo" @("technical-indicators-build", "--symbols", $Symbols, "--start", $StartDate, "--end", $EndDate, "--price-source", "yahoo", "--lookback-days", "90", "--limit-per-symbol", "5000") -AllowFailure
        Invoke-JsonStage "standing_regime_build" @("standing-regime-build", "--start", $StartDate, "--end", $EndDate, "--limit", "100000")
        Invoke-JsonStage "standing_regime_outcomes" @("standing-regime-outcomes", "--symbols", $Symbols, "--horizons-minutes", "5,15,30,390", "--evaluation-limit", "100000", "--output", "exports\standing_regime_outcomes.md", "--json-output-path", "exports\standing_regime_outcomes.json")
        Invoke-JsonStage "geopolitical_intelligence" @("geopolitical-intelligence", "--lookback-hours", "15000", "--horizons-minutes", "5,15,30,390", "--signal-limit", "100000", "--max-assets-per-signal", "8", "--evaluation-limit", "100000", "--output", "exports\geopolitical_intelligence.md", "--json-output-path", "exports\geopolitical_intelligence.json")

        Invoke-Stage "replay_corpus_build" @("replay-corpus-build", "--start", $StartDate, "--end", $EndDate, "--limit", "$ReplayLimit") -AllowFailure
        Invoke-Stage "replay_outcome_build_yahoo_daily" @("replay-outcome-build", "--start", $StartDate, "--end", $EndDate, "--horizons", "1D,5D", "--price-source", "yahoo", "--limit", "$ReplayLimit") -AllowFailure
        Invoke-Stage "replay_outcome_build_ibkr_intraday" @("replay-outcome-build", "--start", $StartDate, "--end", $EndDate, "--horizons", "30M,2H", "--price-source", "ibkr", "--ibkr-client-id", "$IbkrReplayOutcomeClientId", "--ibkr-timeout", "20", "--ibkr-bar-size", "5 mins", "--limit", "$ReplayIntradayLimit") -AllowFailure
        $heuristicArgs = @("replay-score-build", "--start", $StartDate, "--end", $EndDate, "--scorer", "heuristic", "--limit", "$ReplayLimit")
        if (-not $ForceReplayScores) {
            $heuristicArgs += "--only-missing"
        }
        Invoke-Stage "replay_score_build_heuristic" $heuristicArgs -AllowFailure
        if (-not $SkipOpenAiReplayScoring -and $OpenAiScoreLimit -gt 0) {
            $openAiArgs = @("replay-score-build", "--start", $StartDate, "--end", $EndDate, "--scorer", "openai", "--limit", "$OpenAiScoreLimit", "--reasoning-effort", "low")
            if (-not $ForceReplayScores) {
                $openAiArgs += "--only-missing"
            }
            Invoke-Stage "replay_score_build_openai_subset" $openAiArgs -AllowFailure
        }
        if ($ReplayAdjustmentNoteLimit -gt 0) {
            Invoke-Stage "replay_surprise_adjustments_deterministic" @(
                "replay-surprise-adjustments-build",
                "--start", $StartDate, "--end", $EndDate,
                "--limit", "$ReplayAdjustmentNoteLimit",
                "--analysis-provider", "deterministic",
                "--min-abs-error", "0.002",
                "--include-hits"
            ) -AllowFailure
        }
        if (-not $SkipOpenAiReplayScoring -and $OpenAiAdjustmentNoteLimit -gt 0) {
            Invoke-Stage "replay_surprise_adjustments_openai_subset" @(
                "replay-surprise-adjustments-build",
                "--start", $StartDate, "--end", $EndDate,
                "--limit", "$OpenAiAdjustmentNoteLimit",
                "--analysis-provider", "openai",
                "--reasoning-effort", "low",
                "--min-abs-error", "0.005"
            ) -AllowFailure
        }
        Invoke-Stage "replay_surprise_adjustments_report" @(
            "replay-surprise-adjustments-report",
            "--limit", "200",
            "--output", "exports\historical_backfill_surprise_adjustments.md"
        ) -AllowFailure
    } else {
        Write-Log "Reusing existing DB artifacts. Skipping source conversion, technical/regime/outcome rebuild, replay corpus build, replay outcome build, and replay score build."
    }

    Invoke-Stage "clean_cycle_audit_pre_learning" @(
        "clean-backfill-cycle-audit",
        "--start", $StartDate, "--end", $EndDate,
        "--cycle-index", "$CleanBackfillCycleIndex",
        "--cycle-mode", $EffectiveCleanBackfillCycleMode,
        "--phase", "pre_learning",
        "--symbol-tier-config", $NewsSymbolTierConfig,
        "--correction-ledger-path", $CleanBackfillCycleLedgerPath,
        "--output", "exports\historical_backfill_cycle_audit_pre_learning.md",
        "--json-output-path", "exports\historical_backfill_cycle_audit_pre_learning.json"
    ) -AllowFailure

    $reportStages = @(
        @{ Name = "leaderboard_all"; Arguments = @("replay-score-leaderboard", "--min-rows", "5", "--limit", "$ReplayLimit", "--output", "exports\historical_backfill_replay_leaderboard.md"); AllowFailure = $true },
        @{ Name = "calibration_all"; Arguments = @("replay-score-calibration", "--limit", "$ReplayLimit", "--output", "exports\historical_backfill_replay_calibration.md"); AllowFailure = $true },
        @{ Name = "oos_holdout"; Arguments = @("replay-score-oos", "--cutoff", "2026-01-01", "--min-holdout-rows", "5", "--limit", "$ReplayLimit", "--output", "exports\historical_backfill_replay_oos.md"); AllowFailure = $true },
        @{ Name = "paper_action_sim"; Arguments = @("replay-score-paper-action-sim", "--start", $StartDate, "--end", $EndDate, "--limit", "$ReplayLimit", "--output", "exports\historical_backfill_paper_action_sim.md", "--json-output-path", "exports\historical_backfill_paper_action_sim.json"); AllowFailure = $true },
        @{ Name = "paper_order_tune"; Arguments = @("replay-score-paper-order-tune", "--start", $StartDate, "--end", $EndDate, "--limit", "$ReplayLimit", "--top-n", "20", "--output", "exports\historical_backfill_paper_order_tune.md", "--json-output-path", "exports\historical_backfill_paper_order_tune.json"); AllowFailure = $true },
        @{ Name = "paper_order_walk_forward_tune"; Arguments = @("replay-score-paper-order-walk-forward-tune", "--start", $StartDate, "--end", $EndDate, "--sample-limit", "$ReplayLimit", "--top-n", "20", "--output", "exports\historical_backfill_paper_order_walk_forward_tune.md", "--json-output-path", "exports\historical_backfill_paper_order_walk_forward_tune.json"); AllowFailure = $true }
    )
    if ($TrainingPerformanceMode) {
        Invoke-StageBatch -Stages $reportStages -MaxParallel $MaxParallelTrainingStages
    } else {
        foreach ($stage in $reportStages) {
            Invoke-Stage -Name $stage.Name -Arguments $stage.Arguments -AllowFailure:([bool]$stage.AllowFailure)
        }
    }

    $thresholdStage = @{
        Name = "threshold_walk_forward_tune"
        Arguments = @(
        "walk-forward-cycle-threshold-tune",
        "--start", $StartDate, "--end", $EndDate,
        "--score-limit", "$ReplayLimit",
        "--cycles", "3",
        "--top-n", "20",
        "--checkpoint-jsonl-path", (Join-Path $LogDir "threshold_walk_forward_tune.checkpoints.jsonl"),
        "--packet-checkpoint-interval", "500",
        "--output", "exports\historical_backfill_walk_forward_threshold_tune.md",
        "--json-output-path", "exports\historical_backfill_walk_forward_threshold_tune.json"
        )
        AllowFailure = $true
    }
    $strategyArgs = @(
        "replay-score-strategy-walk-forward-tune",
        "--start", $StartDate, "--end", $EndDate,
        "--limit", "$StrategyAdaptiveLimit",
        "--top-n", "20",
        "--checkpoint-path", (Join-Path $LogDir "strategy_walk_forward_tune.checkpoint.json"),
        "--output", "exports\historical_backfill_strategy_walk_forward_tune.md",
        "--json-output-path", "exports\historical_backfill_strategy_walk_forward_tune.json"
    )
    if ($TrainingPerformanceMode) {
        $strategyArgs += @(
            "--min-train-rows", "400",
            "--test-window-rows", "200",
            "--step-rows", "200",
            "--min-confidence-grid", "0.30,0.40",
            "--min-stock-expected-return-grid", "0.0025,0.005",
            "--min-option-score-grid", "0.35,0.45",
            "--min-option-return-on-risk-grid", "0.0,0.25",
            "--option-preference-edge-grid", "0.05,0.20"
        )
    }
    $learningStages = @(
        $thresholdStage,
        @{ Name = "nuanced_walk_forward_train"; Arguments = @("walk-forward-cycle-nuanced-train", "--start", $StartDate, "--end", $EndDate, "--score-limit", "$ReplayLimit", "--cycles", "3", "--output", "exports\historical_backfill_walk_forward_nuanced_train.md", "--json-output-path", "exports\historical_backfill_walk_forward_nuanced_train.json"); AllowFailure = $true },
        @{ Name = "strategy_walk_forward_tune"; Arguments = $strategyArgs; AllowFailure = $true },
        @{ Name = "prediction_trend_report"; Arguments = @("prediction-trend-report", "--window-days", "60", "--min-rows", "5", "--output", "exports\historical_backfill_prediction_trend.md", "--json-output-path", "exports\historical_backfill_prediction_trend.json"); AllowFailure = $true },
        @{ Name = "m236_news_learning"; Arguments = @("m236-news-learning", "--profile-json", "config\m236_news_learning_profile_v1.json", "--output", "exports\m236_news_learning_report.md", "--json-output-path", "exports\m236_news_learning_report.json", "--provider-profile-output-path", "exports\m236_provider_weight_profile.json"); AllowFailure = $false }
    )
    if ($TrainingPerformanceMode) {
        Invoke-StageBatch -Stages $learningStages -MaxParallel $MaxParallelTrainingStages
    } else {
        foreach ($stage in $learningStages) {
            Invoke-Stage -Name $stage.Name -Arguments $stage.Arguments -AllowFailure:([bool]$stage.AllowFailure)
        }
    }

    Invoke-Stage "clean_cycle_final_report" @(
        "clean-backfill-cycle-report",
        "--start", $StartDate, "--end", $EndDate,
        "--cycle-index", "$CleanBackfillCycleIndex",
        "--cycle-mode", $EffectiveCleanBackfillCycleMode,
        "--artifact-dir", "exports",
        "--correction-ledger-path", $CleanBackfillCycleLedgerPath,
        "--output", "exports\historical_backfill_cycle_report.md",
        "--json-output-path", "exports\historical_backfill_cycle_report.json"
    ) -AllowFailure
    $SafeCycleMode = $EffectiveCleanBackfillCycleMode -replace "[^A-Za-z0-9_-]", "_"
    $CycleReportJsonSnapshot = Join-Path $ExportDir ("historical_backfill_cycle_{0:D3}_{1}_report.json" -f $CleanBackfillCycleIndex, $SafeCycleMode)
    $CycleReportMdSnapshot = Join-Path $ExportDir ("historical_backfill_cycle_{0:D3}_{1}_report.md" -f $CleanBackfillCycleIndex, $SafeCycleMode)
    if (Test-Path -LiteralPath (Join-Path $ExportDir "historical_backfill_cycle_report.json")) {
        Copy-Item -LiteralPath (Join-Path $ExportDir "historical_backfill_cycle_report.json") -Destination $CycleReportJsonSnapshot -Force
    }
    if (Test-Path -LiteralPath (Join-Path $ExportDir "historical_backfill_cycle_report.md")) {
        Copy-Item -LiteralPath (Join-Path $ExportDir "historical_backfill_cycle_report.md") -Destination $CycleReportMdSnapshot -Force
    }
    Write-Log "Cycle report snapshot written. json=$CycleReportJsonSnapshot markdown=$CycleReportMdSnapshot"

    Invoke-JsonStage "db_audit_post" @("db-audit")
} finally {
    Pop-Location
}

$EndedAt = Get-Date
$Failed = @($StageRecords | Where-Object { $_.exit_code -ne 0 -and -not $_.allow_failure })
$Warned = @($StageRecords | Where-Object { $_.exit_code -ne 0 -and $_.allow_failure })
$DbSummary = Get-DbSummary

$Summary = [ordered]@{
    version = "historical-backfill-training-2025-v1"
    run_id = $RunId
    start_date = $StartDate
    end_date = $EndDate
    symbols = $Symbols
    ibkr_news_client_id = $IbkrNewsClientId
    ibkr_replay_outcome_client_id = $IbkrReplayOutcomeClientId
    news_outcome_ibkr_client_id = $NewsOutcomeIbkrClientId
    ibkr_news_timeout = $IbkrNewsTimeout
    replay_intraday_limit = $ReplayIntradayLimit
    news_symbol_tier_config = $NewsSymbolTierConfig
    clean_backfill_cycle_index = $CleanBackfillCycleIndex
    clean_backfill_cycle_requested_mode = $CleanBackfillCycleMode
    clean_backfill_cycle_effective_mode = $EffectiveCleanBackfillCycleMode
    source_locked_blind_reclass_mode = [bool]$SourceLockedBlindReclassMode
    clean_backfill_cycle_dedup_guard_applied = [bool]$CleanBackfillDedupGuardApplied
    cycle_report_json_snapshot = $CycleReportJsonSnapshot
    cycle_report_markdown_snapshot = $CycleReportMdSnapshot
    clean_backfill_cycle_ledger_path = $CleanBackfillCycleLedgerPath
    resume_after_news_market_outcome_backfill = [bool]$ResumeAfterNewsMarketOutcomeBackfill
    skip_news_market_outcome_backfill = [bool]$SkipNewsMarketOutcomeBackfill
    news_outcome_intraday_limit = $NewsOutcomeIntradayLimit
    news_outcome_event_intraday_limit = $NewsOutcomeEventIntradayLimit
    skip_news_outcome_repair_pass = [bool]$SkipNewsOutcomeRepairPass
    news_outcome_repair_passes = $NewsOutcomeRepairPasses
    news_outcome_repair_ibkr_timeout = $NewsOutcomeRepairIbkrTimeout
    news_outcome_close_retry_bypass_after_failures = $NewsOutcomeCloseRetryBypassAfterFailures
    replay_adjustment_note_limit = $ReplayAdjustmentNoteLimit
    openai_adjustment_note_limit = $OpenAiAdjustmentNoteLimit
    reuse_existing_db = [bool]$ReuseExistingDb
    clean_derived_rebuild = [bool]$CleanDerivedRebuild
    reset_marketaux_signals = [bool]$ResetMarketauxSignals
    vacuum_after_derived_reset = [bool]$VacuumAfterDerivedReset
    clean_derived_reset_report_path = $script:CleanDerivedResetReportPath
    clean_derived_backup_path = $script:CleanDerivedBackupPath
    training_performance_mode = [bool]$TrainingPerformanceMode
    training_performance_profile = $TrainingPerformanceProfile
    logical_cpu_count = $LogicalCpuCount
    max_parallel_training_stages = $MaxParallelTrainingStages
    strategy_adaptive_limit = $StrategyAdaptiveLimit
    started_at = $StartedAt.ToUniversalTime().ToString("o")
    ended_at = $EndedAt.ToUniversalTime().ToString("o")
    duration_seconds = [math]::Round(($EndedAt - $StartedAt).TotalSeconds, 3)
    stage_count = $StageRecords.Count
    failed_count = $Failed.Count
    warning_count = $Warned.Count
    log_dir = $LogDir
    stages = $StageRecords
    db_summary_json = $DbSummary
    safety = [ordered]@{
        read_only_or_paper_only = $true
        broker_orders_submitted = $false
        transmit_enabled = $false
        live_order_routing_enabled = $false
        live_threshold_promoted = $false
    }
}

$Summary | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $SummaryPath -Encoding UTF8
$SafeSummaryMode = $EffectiveCleanBackfillCycleMode -replace "[^A-Za-z0-9_-]", "_"
$CycleSummaryJsonSnapshot = Join-Path $ExportDir ("historical_backfill_cycle_{0:D3}_{1}_summary.json" -f $CleanBackfillCycleIndex, $SafeSummaryMode)
$CycleSummaryMdSnapshot = Join-Path $ExportDir ("historical_backfill_cycle_{0:D3}_{1}_summary.md" -f $CleanBackfillCycleIndex, $SafeSummaryMode)
Copy-Item -LiteralPath $SummaryPath -Destination $CycleSummaryJsonSnapshot -Force

$md = @()
$md += "# Historical Backfill + Training Summary"
$md += ""
$md += "- Run: ``$RunId``"
$md += "- Range: ``$StartDate`` to ``$EndDate``"
$md += "- Duration seconds: ``$($Summary.duration_seconds)``"
$md += "- Stages: ``$($Summary.stage_count)`` failed ``$($Summary.failed_count)`` warning ``$($Summary.warning_count)``"
$md += "- Logs: ``$LogDir``"
$md += "- Clean backfill cycle: index ``$($Summary.clean_backfill_cycle_index)`` mode ``$($Summary.clean_backfill_cycle_effective_mode)`` dedup guard ``$($Summary.clean_backfill_cycle_dedup_guard_applied)``"
$md += "- Clean derived rebuild: ``$($Summary.clean_derived_rebuild)``"
$md += "- Resume after news market outcome backfill: ``$($Summary.resume_after_news_market_outcome_backfill)``"
if ($Summary.clean_derived_rebuild) {
    $md += "- Clean reset report: ``$($Summary.clean_derived_reset_report_path)``"
    $md += "- Pre-reset DB backup: ``$($Summary.clean_derived_backup_path)``"
    $md += "- Marketaux reset: ``$($Summary.reset_marketaux_signals)``"
    $md += "- OpenAI adjustment note limit: ``$($Summary.openai_adjustment_note_limit)``"
}
$md += "- News symbol tier config: ``$($Summary.news_symbol_tier_config)``"
$md += "- Tiered news outcome backfill skipped: ``$($Summary.skip_news_market_outcome_backfill)``"
$md += "- Tiered news outcome intraday limits: core ``$($Summary.news_outcome_intraday_limit)``, event ``$($Summary.news_outcome_event_intraday_limit)``"
$md += "- News outcome repair pass: skipped ``$($Summary.skip_news_outcome_repair_pass)``, passes ``$($Summary.news_outcome_repair_passes)``, IBKR timeout ``$($Summary.news_outcome_repair_ibkr_timeout)`` seconds"
$md += "- News outcome close-retry bypass: after ``$($Summary.news_outcome_close_retry_bypass_after_failures)`` consecutive failures per symbol"
$md += "- Safety: read-only/paper-only; no broker orders, no transmit, no live routing."
$md += ""
$md += "## Stages"
$md += ""
$md += "| Stage | Exit | Allow failure | Duration |"
$md += "| --- | ---: | --- | ---: |"
foreach ($stage in $StageRecords) {
    $md += "| ``$($stage.name)`` | $($stage.exit_code) | $($stage.allow_failure) | $($stage.duration_seconds) |"
}
$md += ""
$md += "## Key output files"
$md += ""
$md += "- ``exports\historical_backfill_replay_leaderboard.md``"
$md += "- ``exports\historical_backfill_replay_calibration.md``"
$md += "- ``exports\historical_backfill_paper_action_sim.json``"
$md += "- ``exports\historical_backfill_paper_order_tune.json``"
$md += "- ``exports\historical_backfill_paper_order_walk_forward_tune.json``"
$md += "- ``exports\historical_backfill_walk_forward_threshold_tune.json``"
$md += "- ``exports\historical_backfill_walk_forward_nuanced_train.json``"
$md += "- ``exports\historical_backfill_strategy_walk_forward_tune.json``"
$md += "- ``exports\historical_backfill_surprise_adjustments.md``"
$md += "- ``exports\historical_backfill_news_symbol_tiers.md``"
$md += "- ``exports\historical_backfill_news_outcome_next_day.md``"
$md += "- ``exports\historical_backfill_news_outcome_intraday_core.md``"
$md += "- ``exports\historical_backfill_news_outcome_intraday_event.md``"
$md += "- ``exports\historical_backfill_cycle_audit_pre_learning.md``"
$md += "- ``exports\historical_backfill_cycle_report.md``"
$md += ""
$md -join "`n" | Set-Content -LiteralPath $MarkdownPath -Encoding UTF8
Copy-Item -LiteralPath $MarkdownPath -Destination $CycleSummaryMdSnapshot -Force

Write-Log "Historical backfill/training complete. summary=$SummaryPath markdown=$MarkdownPath"
if ($Failed.Count -gt 0) {
    exit 1
}
exit 0
