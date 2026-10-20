param(
    [int]$IntervalSeconds = 3600,
    [int]$MaxTicks = 0,
    [switch]$Execute
)

$ErrorActionPreference = "Continue"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Resolve-Path (Join-Path $ScriptDir "..")
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$LogDir = Join-Path $RepoRoot "logs"
$LogPath = Join-Path $LogDir "artifact_cleanup_loop.log"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Python runtime not found: $Python"
}

if (-not (Test-Path -LiteralPath $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir | Out-Null
}

function Write-LoopLog {
    param([string]$Message)
    $stamp = (Get-Date).ToString("o")
    Add-Content -LiteralPath $LogPath -Value "[$stamp] $Message" -Encoding UTF8
}

$tick = 0
Write-LoopLog "artifact cleanup loop started interval=$IntervalSeconds max_ticks=$MaxTicks execute=$Execute"
while ($MaxTicks -le 0 -or $tick -lt $MaxTicks) {
    $tick += 1
    try {
        $args = @(
            "main.py", "artifact-cleanup",
            "--json-output-path", "exports\artifact_cleanup_latest.json",
            "--output", "exports\artifact_cleanup_latest.md",
            "--keep-latest-per-logical-key", "5",
            "--work-max-age-days", "14"
        )
        if ($Execute) {
            $args += "--execute"
        }
        & $Python @args *> $null
        Write-LoopLog "tick ${tick}: artifact-cleanup exit=$LASTEXITCODE"
    } catch {
        Write-LoopLog "tick ${tick}: error $($_.Exception.Message)"
    }

    if ($MaxTicks -gt 0 -and $tick -ge $MaxTicks) {
        break
    }
    Start-Sleep -Seconds ([Math]::Max(300, $IntervalSeconds))
}
Write-LoopLog "artifact cleanup loop stopped ticks=$tick"
