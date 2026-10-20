param(
    [string]$StartDate = "2025-01-01",
    [string]$EndDate = "2026-07-05",
    [int]$FirstCycleIndex = 2,
    [int]$MaxBlindCycles = 0,
    [int]$PollSeconds = 60,
    [int]$ReplayLimit = 25000,
    [int]$ReplayIntradayLimit = 250,
    [ValidateSet("balanced", "aggressive", "max")]
    [string]$TrainingPerformanceProfile = "aggressive",
    [string]$NewsSymbolTierConfig = "config\news_market_outcome_symbol_tiers_v1.json",
    [string]$CleanBackfillCycleLedgerPath = "exports\clean_backfill_cycle_correction_ledger.jsonl"
)

$ErrorActionPreference = "Stop"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Resolve-Path (Join-Path $ScriptDir "..")
$ExportDir = Join-Path $RepoRoot "exports"
$LogDir = Join-Path $RepoRoot "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$WatcherLog = Join-Path $LogDir "next_blind_training_cycle_watcher.log"
$MutexName = "Global\IBKR_Lumibot_NextBlindTrainingCycleWatcher_v1"
$createdNew = $false
$mutex = New-Object System.Threading.Mutex($false, $MutexName, [ref]$createdNew)
if (-not $createdNew) {
    $line = "[{0}] Another next-blind-cycle watcher is already running; exiting." -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss")
    Write-Host $line
    Add-Content -LiteralPath $WatcherLog -Value $line
    return
}

function Write-WatcherLog {
    param([string]$Message)
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Write-Host $line
    Add-Content -LiteralPath $WatcherLog -Value $line
}

function Read-CycleReport {
    $path = Join-Path $ExportDir "historical_backfill_cycle_report.json"
    if (-not (Test-Path -LiteralPath $path)) {
        return $null
    }
    try {
        $item = Get-Item -LiteralPath $path
        $json = Get-Content -LiteralPath $path -Raw | ConvertFrom-Json
        return [pscustomobject]@{
            Path = $path
            LastWriteTime = $item.LastWriteTime
            Json = $json
        }
    } catch {
        Write-WatcherLog "WARN failed to read cycle report: $($_.Exception.Message)"
        return $null
    }
}

try {
    if ($PollSeconds -lt 15) {
        $PollSeconds = 15
    }
    if ($FirstCycleIndex -lt 2) {
        $FirstCycleIndex = 2
    }

    $anchor = Get-Date
    $cyclesLaunched = 0
    $nextCycleIndex = $FirstCycleIndex
    Write-WatcherLog "Watcher started. Waiting for a new cycle report after $($anchor.ToUniversalTime().ToString('o')). first_cycle=$nextCycleIndex max_cycles=$MaxBlindCycles"

    while ($true) {
        $reportRecord = $null
        while ($null -eq $reportRecord) {
            Start-Sleep -Seconds $PollSeconds
            $candidate = Read-CycleReport
            if ($candidate -and $candidate.LastWriteTime -gt $anchor) {
                $reportRecord = $candidate
            }
        }

        $report = $reportRecord.Json
        $decision = $report.cycle_stop_decision
        $status = if ($decision) { [string]$decision.status } else { "unknown" }
        $shouldStop = if ($decision) { [bool]$decision.should_stop_training_cycles } else { $false }
        $reportedCycle = if ($report.cycle_index) { [int]$report.cycle_index } else { 0 }
        Write-WatcherLog "New cycle report detected. reported_cycle=$reportedCycle status=$status should_stop=$shouldStop path=$($reportRecord.Path)"

        if ($shouldStop -or $status -eq "full_auto_candidate_ready") {
            Write-WatcherLog "Readiness says stop repeated blind cycles. No live routing or broker order permission is implied. Watcher exiting."
            break
        }

        if ($MaxBlindCycles -gt 0 -and $cyclesLaunched -ge $MaxBlindCycles) {
            Write-WatcherLog "MaxBlindCycles reached ($MaxBlindCycles). Watcher exiting."
            break
        }

        $nextCycleIndex = [Math]::Max(2, [Math]::Max($nextCycleIndex, $reportedCycle + 1))
        Write-WatcherLog "Launching blind cycle $nextCycleIndex on the same fixed dataset."
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $ScriptDir "run_next_blind_training_cycle_2025.ps1") `
            -StartDate $StartDate `
            -EndDate $EndDate `
            -CycleIndex $nextCycleIndex `
            -ReplayLimit $ReplayLimit `
            -ReplayIntradayLimit $ReplayIntradayLimit `
            -TrainingPerformanceProfile $TrainingPerformanceProfile `
            -OpenAiScoreLimit 0 `
            -OpenAiAdjustmentNoteLimit 0 `
            -NewsSymbolTierConfig $NewsSymbolTierConfig `
            -CleanBackfillCycleLedgerPath $CleanBackfillCycleLedgerPath
        $exitCode = $LASTEXITCODE
        $cyclesLaunched += 1
        Write-WatcherLog "Blind cycle $nextCycleIndex completed with exit=$exitCode."
        if ($exitCode -ne 0) {
            Write-WatcherLog "Stopping watcher after non-zero blind cycle exit."
            break
        }
        # Keep the anchor at the report that triggered this launch so the next
        # freshly written cycle report can be detected and chained immediately.
        $anchor = $reportRecord.LastWriteTime
        $nextCycleIndex += 1
    }
} finally {
    try { $mutex.ReleaseMutex() | Out-Null } catch {}
    $mutex.Dispose()
}
