# Temporary fixed-universe option sampler.
#
# Reads the current DB symbols from option_structure_outcome_logs and launches
# the existing read-only options-market-structure supervisor with that universe
# pinned into a generated profile.

[CmdletBinding()]
param(
    [string]$DatabasePath = "market_events.db",
    [string]$ProfileTemplate = "config\options_market_structure_supervisor_realtime_profile_v1.json",
    [string]$GeneratedProfilePath = "work\db_symbol_option_sampler_profile.json",
    [string]$Output = "exports\db_symbol_option_sampler_supervisor.md",
    [string]$JsonOutputPath = "exports\db_symbol_option_sampler_supervisor.json",
    [int]$MaxTicks = 0,
    [double]$IntervalSeconds = 60.0,
    [string]$RunIdPrefix = "db-symbol-option-sampler",
    [switch]$PlanOnly,
    [switch]$DryRun,
    [switch]$Json
)

$ErrorActionPreference = "Stop"

$RepoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
Set-Location $RepoRoot

function Get-RepoPath {
    param([Parameter(Mandatory = $true)][string]$PathText)
    if ([System.IO.Path]::IsPathRooted($PathText)) {
        return [System.IO.Path]::GetFullPath($PathText)
    }
    return [System.IO.Path]::GetFullPath((Join-Path $RepoRoot $PathText))
}

$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) {
    $Python = "python"
}

$DatabaseFullPath = Get-RepoPath $DatabasePath
$TemplateFullPath = Get-RepoPath $ProfileTemplate
$GeneratedProfileFullPath = Get-RepoPath $GeneratedProfilePath
$OutputFullPath = Get-RepoPath $Output
$JsonOutputFullPath = Get-RepoPath $JsonOutputPath

if (-not (Test-Path -LiteralPath $DatabaseFullPath)) {
    throw "Database not found: $DatabaseFullPath"
}
if (-not (Test-Path -LiteralPath $TemplateFullPath)) {
    throw "Profile template not found: $TemplateFullPath"
}

New-Item -ItemType Directory -Force -Path (Split-Path -Parent $GeneratedProfileFullPath) | Out-Null
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $OutputFullPath) | Out-Null
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $JsonOutputFullPath) | Out-Null

$ProfileBuilder = @'
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


db_path = Path(sys.argv[1])
template_path = Path(sys.argv[2])
generated_path = Path(sys.argv[3])

optional_index_like = {
    "SPX",
    "XSP",
    "NDX",
    "XND",
    "RUT",
    "VIX",
    "VXX",
    "VIXY",
    "UVXY",
}

with sqlite3.connect(db_path) as conn:
    exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='option_structure_outcome_logs'"
    ).fetchone()
    if not exists:
        raise SystemExit("option_structure_outcome_logs table not found in database")

    columns = {row[1] for row in conn.execute("PRAGMA table_info(option_structure_outcome_logs)").fetchall()}
    if "symbol" not in columns:
        raise SystemExit("option_structure_outcome_logs has no symbol column")

    rows = conn.execute(
        """
        SELECT UPPER(TRIM(symbol)) AS symbol, COUNT(*) AS row_count
        FROM option_structure_outcome_logs
        WHERE symbol IS NOT NULL AND TRIM(symbol) != ''
        GROUP BY UPPER(TRIM(symbol))
        ORDER BY symbol
        """
    ).fetchall()

symbols = [str(row[0]).upper() for row in rows if row[0]]
row_counts = {str(row[0]).upper(): int(row[1]) for row in rows if row[0]}
if not symbols:
    raise SystemExit("No symbols found in option_structure_outcome_logs")

optional_symbols = [symbol for symbol in symbols if symbol in optional_index_like]
core_symbols = [symbol for symbol in symbols if symbol not in set(optional_symbols)]

profile = json.loads(template_path.read_text(encoding="utf-8"))
profile["profile_version"] = "db-symbol-option-sampler-temporary-v1"
profile["description"] = (
    "TEMPORARY read-only option sampler profile generated from the current "
    "option_structure_outcome_logs DB symbols. This pins the option collection "
    "universe while the larger data-lineage/source rebuild is in progress."
)
profile["symbols"] = core_symbols
profile["optional_index_symbols"] = optional_symbols

symbol_groups = profile.setdefault("symbol_groups", {})
symbol_groups["core_etf_proxies"] = core_symbols
symbol_groups["optional_index_options"] = optional_symbols

symbol_set = set(symbols)
families = []
for family in profile.get("index_option_families") or []:
    family = dict(family)
    for key in ("direct_symbols", "mini_symbols", "proxy_symbols"):
        family[key] = [str(item).upper() for item in family.get(key, []) if str(item).upper() in symbol_set]
    if any(family.get(key) for key in ("direct_symbols", "mini_symbols", "proxy_symbols")):
        families.append(family)
profile["index_option_families"] = families

for key in ("underlying_sec_types", "underlying_exchanges", "reference_prices"):
    if isinstance(profile.get(key), dict):
        profile[key] = {str(k).upper(): v for k, v in profile[key].items() if str(k).upper() in symbol_set}

paths = profile.setdefault("paths", {})
paths.update(
    {
        "quote_json_path": "../exports/db_symbol_option_sampler_fresh_option_quote_snapshot.json",
        "quote_markdown_path": "../exports/db_symbol_option_sampler_fresh_option_quote_snapshot.md",
        "structure_json_path": "../exports/db_symbol_option_sampler_options_market_structure.json",
        "structure_markdown_path": "../exports/db_symbol_option_sampler_options_market_structure.md",
        "optional_index_quote_json_path": "../exports/db_symbol_option_sampler_optional_index_option_quote_snapshot.json",
        "optional_index_quote_markdown_path": "../exports/db_symbol_option_sampler_optional_index_option_quote_snapshot.md",
        "optional_index_structure_json_path": "../exports/db_symbol_option_sampler_optional_index_options_market_structure.json",
        "optional_index_structure_markdown_path": "../exports/db_symbol_option_sampler_optional_index_options_market_structure.md",
        "option_structure_outcome_json_path": "../exports/db_symbol_option_sampler_option_structure_outcome_log.json",
        "option_structure_outcome_markdown_path": "../exports/db_symbol_option_sampler_option_structure_outcome_log.md",
        "optional_index_option_structure_outcome_json_path": "../exports/db_symbol_option_sampler_optional_index_option_structure_outcome_log.json",
        "optional_index_option_structure_outcome_markdown_path": "../exports/db_symbol_option_sampler_optional_index_option_structure_outcome_log.md",
        "index_option_capability_probe_json_path": "../exports/db_symbol_option_sampler_index_option_capability_probe.json",
        "index_option_capability_probe_markdown_path": "../exports/db_symbol_option_sampler_index_option_capability_probe.md",
        "state_json_path": "../exports/db_symbol_option_sampler_state.json",
        "tick_log_jsonl_path": "../exports/db_symbol_option_sampler_ticks.jsonl",
    }
)

outputs = profile.setdefault("outputs", {})
outputs["output"] = "../exports/db_symbol_option_sampler_supervisor.md"
outputs["json_output_path"] = "../exports/db_symbol_option_sampler_supervisor.json"

schedule = profile.setdefault("schedule", {})
schedule["execute_actions"] = True
schedule["max_ticks"] = 0
schedule["sleep_between_ticks"] = True

outcomes = profile.setdefault("option_structure_outcomes", {})
outcomes["enabled"] = True
outcomes.setdefault("price_source", "ibkr")
outcomes.setdefault("horizons", "5M,15M,30M,EOD")
outcomes.setdefault("flat_threshold", 0.001)

profile["temporary_fixed_universe"] = {
    "source_table": "option_structure_outcome_logs",
    "generated_at_utc": datetime.now(timezone.utc).isoformat(),
    "symbol_count": len(symbols),
    "core_symbol_count": len(core_symbols),
    "optional_index_symbol_count": len(optional_symbols),
    "row_counts": row_counts,
}

generated_path.write_text(json.dumps(profile, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
print(
    json.dumps(
        {
            "profile_path": str(generated_path),
            "symbol_count": len(symbols),
            "core_symbols": core_symbols,
            "optional_index_symbols": optional_symbols,
            "row_counts": row_counts,
        },
        ensure_ascii=False,
    )
)
'@

$BuilderOutput = $ProfileBuilder | & $Python - $DatabaseFullPath $TemplateFullPath $GeneratedProfileFullPath
if ($LASTEXITCODE -ne 0) {
    throw "Failed to build generated fixed-universe profile."
}

$Summary = ($BuilderOutput | Out-String | ConvertFrom-Json)
$Timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$RunId = "$RunIdPrefix-$Timestamp"

Write-Host "DB fixed option universe profile: $GeneratedProfileFullPath"
Write-Host ("Symbols: {0} total / {1} core / {2} optional-index" -f $Summary.symbol_count, $Summary.core_symbols.Count, $Summary.optional_index_symbols.Count)
Write-Host ("Core: {0}" -f (($Summary.core_symbols | ForEach-Object { [string]$_ }) -join ","))
Write-Host ("Optional/index: {0}" -f (($Summary.optional_index_symbols | ForEach-Object { [string]$_ }) -join ","))

$SupervisorArgs = @(
    "main.py",
    "options-market-structure-supervisor",
    "--profile-json",
    $GeneratedProfileFullPath,
    "--max-ticks",
    [string]$MaxTicks,
    "--interval-seconds",
    [string]$IntervalSeconds,
    "--run-id",
    $RunId,
    "--output",
    $OutputFullPath,
    "--json-output-path",
    $JsonOutputFullPath
)

if (-not $PlanOnly) {
    $SupervisorArgs += "--execute-actions"
}
if ($Json) {
    $SupervisorArgs += "--json"
}

Write-Host ("PlanOnly: {0} / DryRun: {1} / MaxTicks: {2} / IntervalSeconds: {3}" -f [bool]$PlanOnly, [bool]$DryRun, $MaxTicks, $IntervalSeconds)
Write-Host ("Command: {0} {1}" -f $Python, ($SupervisorArgs -join " "))

if ($DryRun) {
    Write-Host "DryRun requested; supervisor was not started."
    exit 0
}

& $Python @SupervisorArgs
exit $LASTEXITCODE
