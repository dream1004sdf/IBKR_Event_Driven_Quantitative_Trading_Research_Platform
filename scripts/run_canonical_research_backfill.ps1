param(
    [string]$Config = "config\backfill\canonical_20250101_20260706.yaml",
    [string]$RunId = "",
    [string]$Stage = "",
    [switch]$Resume,
    [switch]$ForceStage,
    [switch]$AllowBlind,
    [switch]$DryRun,
    [switch]$ListStages
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $ScriptDir
Set-Location $RepoRoot

$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    $Python = "python"
}

$argsList = @(
    "-m",
    "recorder.research_backfill_runner",
    "--config",
    $Config
)

if ($RunId.Trim()) {
    $argsList += @("--run-id", $RunId)
}
if ($Stage.Trim()) {
    $argsList += @("--stage", $Stage)
}
if ($Resume) {
    $argsList += "--resume"
}
if ($ForceStage) {
    $argsList += "--force-stage"
}
if ($AllowBlind) {
    $argsList += "--allow-blind"
}
if ($DryRun) {
    $argsList += "--dry-run"
}
if ($ListStages) {
    $argsList += "--list-stages"
}

& $Python @argsList
exit $LASTEXITCODE
