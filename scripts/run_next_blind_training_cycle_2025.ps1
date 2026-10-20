param(
    [string]$StartDate = "2025-01-01",
    [string]$EndDate = "2026-07-05",
    [int]$CycleIndex = 0,
    [int]$ReplayLimit = 25000,
    [int]$ReplayIntradayLimit = 250,
    [ValidateSet("balanced", "aggressive", "max")]
    [string]$TrainingPerformanceProfile = "aggressive",
    [ValidateSet("blind", "source-locked-blind-reclass")]
    [string]$CycleMode = "blind",
    [int]$OpenAiScoreLimit = 0,
    [int]$OpenAiAdjustmentNoteLimit = 0,
    [int]$ReplayAdjustmentNoteLimit = 5000,
    [string]$NewsSymbolTierConfig = "config\news_market_outcome_symbol_tiers_v1.json",
    [string]$CleanBackfillCycleLedgerPath = "exports\clean_backfill_cycle_correction_ledger.jsonl",
    [switch]$WhatIf
)

$ErrorActionPreference = "Stop"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

$CanonicalCycleStartDate = "2025-01-01"
try {
    $ParsedStartDate = [DateTime]::Parse($StartDate, [System.Globalization.CultureInfo]::InvariantCulture)
} catch {
    throw "Next blind training cycle must use a parseable StartDate equal to $CanonicalCycleStartDate. received=$StartDate"
}
if ($ParsedStartDate.ToString("yyyy-MM-dd") -ne $CanonicalCycleStartDate) {
    throw "Next blind training cycle must start at $CanonicalCycleStartDate for comparable blind walk-forward learning. received=$StartDate"
}
$StartDate = $CanonicalCycleStartDate

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Resolve-Path (Join-Path $ScriptDir "..")
$Runner = Join-Path $ScriptDir "run_historical_backfill_training_2025.ps1"
$ExportDir = Join-Path $RepoRoot "exports"
$LogDir = Join-Path $RepoRoot "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$LauncherLog = Join-Path $LogDir "next_blind_training_cycle_2025.log"

function Write-LauncherLog {
    param([string]$Message)
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Write-Host $line
    Add-Content -LiteralPath $LauncherLog -Value $line
}

function Read-JsonFile {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        return $null
    }
    try {
        return (Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json)
    } catch {
        Write-LauncherLog "WARN failed to parse JSON path=$Path error=$($_.Exception.Message)"
        return $null
    }
}

function Resolve-NextCycleIndex {
    param([int]$RequestedCycleIndex)
    if ($RequestedCycleIndex -ge 2) {
        return $RequestedCycleIndex
    }

    $summary = Read-JsonFile -Path (Join-Path $ExportDir "historical_backfill_training_2025_summary.json")
    $report = Read-JsonFile -Path (Join-Path $ExportDir "historical_backfill_cycle_report.json")
    $indexes = @()
    if ($summary -and $summary.clean_backfill_cycle_index) {
        $indexes += [int]$summary.clean_backfill_cycle_index
    }
    if ($report -and $report.cycle_index) {
        $indexes += [int]$report.cycle_index
    }
    if ($indexes.Count -eq 0) {
        return 2
    }
    return [Math]::Max(2, (($indexes | Measure-Object -Maximum).Maximum + 1))
}

$ResolvedCycleIndex = Resolve-NextCycleIndex -RequestedCycleIndex $CycleIndex
if ($ResolvedCycleIndex -lt 2) {
    throw "Next blind training cycle must use CycleIndex >= 2. resolved=$ResolvedCycleIndex"
}

$Args = @(
    "-NoProfile",
    "-ExecutionPolicy", "Bypass",
    "-File", $Runner,
    "-StartDate", $StartDate,
    "-EndDate", $EndDate,
    "-CleanBackfillCycleIndex", "$ResolvedCycleIndex",
    "-CleanBackfillCycleMode", $CycleMode,
    "-CleanBackfillCycleLedgerPath", $CleanBackfillCycleLedgerPath,
    "-NewsSymbolTierConfig", $NewsSymbolTierConfig,
    "-SkipNetworkBackfill",
    "-TrainingPerformanceMode",
    "-TrainingPerformanceProfile", $TrainingPerformanceProfile,
    "-ReplayLimit", "$ReplayLimit",
    "-ReplayIntradayLimit", "$ReplayIntradayLimit",
    "-OpenAiScoreLimit", "$OpenAiScoreLimit",
    "-OpenAiAdjustmentNoteLimit", "$OpenAiAdjustmentNoteLimit",
    "-ReplayAdjustmentNoteLimit", "$ReplayAdjustmentNoteLimit"
)
if ($CycleMode -eq "blind") {
    $Args += @(
        "-ReuseExistingDb",
        "-SkipNewsMarketOutcomeBackfill",
        "-SkipNewsOutcomeRepairPass"
    )
} else {
    $Args += "-ForceReplayScores"
}

Write-LauncherLog "Preparing next blind training cycle. cycle=$ResolvedCycleIndex mode=$CycleMode start=$StartDate end=$EndDate profile=$TrainingPerformanceProfile replay_limit=$ReplayLimit"
if ($CycleMode -eq "blind") {
    Write-LauncherLog "Guard active: no clean-derived rebuild, no network/source backfill, no news outcome rebuild, reuse existing DB/artifacts."
} else {
    Write-LauncherLog "Source-locked reclass active: no raw/source network backfill, but classification, ticker tiering, missing-only outcomes, replay, and tuning are regenerated."
}
Write-LauncherLog "Command: powershell.exe $($Args -join ' ')"

if ($WhatIf) {
    Write-LauncherLog "WhatIf requested; not launching."
    return
}

Push-Location $RepoRoot
try {
    & powershell.exe @Args
    $exitCode = $LASTEXITCODE
} finally {
    Pop-Location
}

Write-LauncherLog "Next blind training cycle finished. cycle=$ResolvedCycleIndex exit=$exitCode"
exit $exitCode
