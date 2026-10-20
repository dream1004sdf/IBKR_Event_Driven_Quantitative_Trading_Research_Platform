param(
    [int]$IntervalSeconds = 300,
    [int]$MaxTicks = 0,
    [int]$ExecutionReadinessEveryTicks = 1,
    [int]$SafetyCacheEveryTicks = 1,
    [switch]$SkipExecutionReadinessRefresh,
    [switch]$SkipSafetyCacheRefresh,
    [switch]$ExecuteSafetyCacheRefresh
)

$ErrorActionPreference = "Continue"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Resolve-Path (Join-Path $ScriptDir "..")
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$LogDir = Join-Path $RepoRoot "logs"
$LogPath = Join-Path $LogDir "capital_refresh_loop.log"

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
Write-LoopLog "capital refresh loop started interval=$IntervalSeconds max_ticks=$MaxTicks execution_readiness_every_ticks=$ExecutionReadinessEveryTicks skip_execution_readiness=$SkipExecutionReadinessRefresh automated_broker_refresh=hard_disabled"
if ($ExecuteSafetyCacheRefresh -or -not $SkipSafetyCacheRefresh) {
    Write-LoopLog "execution safety cache refresh arguments are ignored because automated broker refresh is hard-disabled"
}
while ($MaxTicks -le 0 -or $tick -lt $MaxTicks) {
    $tick += 1
    try {
        Write-LoopLog "tick ${tick}: refreshing read-only IBKR account snapshot and M237"
        & $Python "main.py" "capital-margin-state" `
            "--refresh-account-snapshot" `
            "--account-snapshot-json" "exports\m110_ibkr_account_snapshot.json" `
            "--json-output-path" "exports\m237_capital_margin_state.json" `
            "--output" "exports\m237_capital_margin_state.md" *> $null
        $code = $LASTEXITCODE
        Write-LoopLog "tick ${tick}: capital-margin-state exit=$code"

        $runReadiness = (-not $SkipExecutionReadinessRefresh) -and ($ExecutionReadinessEveryTicks -le 1 -or (($tick % $ExecutionReadinessEveryTicks) -eq 0))
        if ($runReadiness) {
            Write-LoopLog "tick ${tick}: rebuilding M239/M241/M242/M243 through M244 safe paper/read-only coordinator"
            & $Python "main.py" "capital-pipeline-orchestrator" `
                "--execute-rebuild" `
                "--json-output-path" "exports\m244_capital_pipeline_orchestrator.json" `
                "--output" "exports\m244_capital_pipeline_orchestrator.md" *> $null
            $code = $LASTEXITCODE
            Write-LoopLog "tick ${tick}: capital-pipeline-orchestrator rebuild exit=$code"

            Write-LoopLog "tick ${tick}: refreshing M245 feasible-vs-executable summary"
            & $Python "main.py" "capital-trade-executable-separation" `
                "--json-output-path" "exports\m245_capital_trade_executable_separation.json" `
                "--output" "exports\m245_capital_trade_executable_separation.md" *> $null
            $code = $LASTEXITCODE
            Write-LoopLog "tick ${tick}: capital-trade-executable-separation exit=$code"

            Write-LoopLog "tick ${tick}: refreshing M247 micro option candidates from stored option evidence"
            & $Python "main.py" "micro-option-candidates" `
                "--json-output-path" "exports\m247_micro_option_candidates.json" `
                "--output" "exports\m247_micro_option_candidates.md" *> $null
            $code = $LASTEXITCODE
            Write-LoopLog "tick ${tick}: micro-option-candidates exit=$code"

            Write-LoopLog "tick ${tick}: refreshing M248 paper option ticket preview"
            & $Python "main.py" "paper-option-ticket-preview" `
                "--json-output-path" "exports\m248_paper_option_ticket_preview.json" `
                "--output" "exports\m248_paper_option_ticket_preview.md" *> $null
            $code = $LASTEXITCODE
            Write-LoopLog "tick ${tick}: paper-option-ticket-preview exit=$code"

            Write-LoopLog "tick ${tick}: refreshing M249 local option what-if safety validation without broker what-if submission"
            & $Python "main.py" "option-whatif-safety-validation" `
                "--allow-missing-broker-whatif" `
                "--json-output-path" "exports\m249_option_whatif_safety_validation.json" `
                "--output" "exports\m249_option_whatif_safety_validation.md" *> $null
            $code = $LASTEXITCODE
            Write-LoopLog "tick ${tick}: option-whatif-safety-validation exit=$code"

            Write-LoopLog "tick ${tick}: refreshing M250 limited auto-trading readiness"
            & $Python "main.py" "limited-auto-trading-readiness" `
                "--json-output-path" "exports\m250_limited_auto_trading_readiness.json" `
                "--output" "exports\m250_limited_auto_trading_readiness.md" *> $null
            $code = $LASTEXITCODE
            Write-LoopLog "tick ${tick}: limited-auto-trading-readiness exit=$code"

            Write-LoopLog "tick ${tick}: recording/evaluating M250.5 gate severity outcomes"
            & $Python "main.py" "gate-severity-outcomes" `
                "--json-output-path" "exports\m250_5_gate_severity_outcomes.json" `
                "--output" "exports\m250_5_gate_severity_outcomes.md" *> $null
            $code = $LASTEXITCODE
            Write-LoopLog "tick ${tick}: gate-severity-outcomes exit=$code"
        } else {
            Write-LoopLog "tick ${tick}: refreshing M244 capital pipeline freshness report only"
            & $Python "main.py" "capital-pipeline-orchestrator" `
                "--json-output-path" "exports\m244_capital_pipeline_orchestrator.json" `
                "--output" "exports\m244_capital_pipeline_orchestrator.md" *> $null
            $code = $LASTEXITCODE
            Write-LoopLog "tick ${tick}: capital-pipeline-orchestrator exit=$code"

        }
    } catch {
        Write-LoopLog "tick ${tick}: error $($_.Exception.Message)"
    }

    if ($MaxTicks -gt 0 -and $tick -ge $MaxTicks) {
        break
    }
    Start-Sleep -Seconds ([Math]::Max(30, $IntervalSeconds))
}
Write-LoopLog "capital refresh loop stopped ticks=$tick"
