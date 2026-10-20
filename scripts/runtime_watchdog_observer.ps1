param(
  [string]$RepoRoot = "C:\IBKR_Lumibot\ibkr_market_event_recorder",
  [int]$IntervalSeconds = 60,
  [int]$MaxTicks = 0,
  [string]$ObservationJsonl = "exports\runtime_watchdog_observations.jsonl",
  [string]$ObservationMarkdown = "exports\runtime_watchdog_observations.md",
  [switch]$RequireLearningLoops
)

$ErrorActionPreference = "Continue"
Set-Location $RepoRoot

function Get-IsoNowLocal {
  return (Get-Date).ToString("yyyy-MM-ddTHH:mm:sszzz")
}

function Get-IsoNowUtc {
  return (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
}

function Get-ProcessMatches {
  param([string]$Pattern)
  $escapedRoot = [Regex]::Escape($RepoRoot)
  $items = Get-CimInstance Win32_Process -Filter "name = 'python.exe' or name = 'cmd.exe' or name = 'powershell.exe'" -ErrorAction SilentlyContinue |
    Where-Object {
      (
        $_.CommandLine -match $escapedRoot -or
        $_.CommandLine -match "ibkr_market_event_recorder" -or
        $_.CommandLine -match "main.py" -or
        $_.CommandLine -match "recorder\.live_learning_control_plane" -or
        $_.CommandLine -match "run_live_learning_control_plane_windows" -or
        $_.CommandLine -match "run_capital_refresh_loop\.ps1" -or
        $_.CommandLine -match "run_artifact_cleanup_loop\.ps1" -or
        $_.CommandLine -match "http.server\s+8765" -or
        $_.CommandLine -match "runtime_watchdog_observer"
      ) -and
      $_.CommandLine -match $Pattern
    } |
    Select-Object ProcessId, ParentProcessId, Name, CommandLine
  return @($items)
}

function Get-ArtifactInfo {
  param(
    [string]$Path,
    [int]$WarnAgeSeconds,
    [int]$RedAgeSeconds
  )
  $full = Join-Path $RepoRoot $Path
  if (!(Test-Path $full)) {
    return [ordered]@{
      path = $Path
      exists = $false
      age_seconds = $null
      status = "missing"
      last_write_local = $null
    }
  }
  $item = Get-Item $full
  $age = [int]([DateTime]::Now - $item.LastWriteTime).TotalSeconds
  $status = "green"
  if ($age -ge $RedAgeSeconds) {
    $status = "red"
  } elseif ($age -ge $WarnAgeSeconds) {
    $status = "yellow"
  }
  return [ordered]@{
    path = $Path
    exists = $true
    age_seconds = $age
    status = $status
    last_write_local = $item.LastWriteTime.ToString("yyyy-MM-ddTHH:mm:sszzz")
  }
}

function Read-JsonArtifact {
  param([string]$Path)
  $full = Join-Path $RepoRoot $Path
  if (!(Test-Path $full)) {
    return $null
  }
  try {
    return Get-Content -LiteralPath $full -Raw -Encoding UTF8 | ConvertFrom-Json
  } catch {
    return $null
  }
}

function New-Finding {
  param(
    [string]$Severity,
    [string]$Code,
    [string]$Message,
    [string]$Suggestion
  )
  return [ordered]@{
    severity = $Severity
    code = $Code
    message = $Message
    suggestion = $Suggestion
  }
}

function Write-Observation {
  param([hashtable]$Payload)
  $jsonlPath = Join-Path $RepoRoot $ObservationJsonl
  $mdPath = Join-Path $RepoRoot $ObservationMarkdown
  New-Item -ItemType Directory -Force -Path (Split-Path $jsonlPath) | Out-Null
  $Payload | ConvertTo-Json -Depth 8 -Compress | Add-Content -Path $jsonlPath -Encoding utf8

  $lines = New-Object System.Collections.Generic.List[string]
  $lines.Add("# Runtime Watchdog Observations")
  $lines.Add("")
  $lines.Add("- Latest local time: ``$($Payload.generated_at_local)``")
  $lines.Add("- Tick: ``$($Payload.tick)``")
  $lines.Add("- Overall status: ``$($Payload.status)``")
  $lines.Add("")
  $lines.Add("## Latest findings")
  if ($Payload.findings.Count -eq 0) {
    $lines.Add("")
    $lines.Add("- No actionable findings on the latest tick.")
  } else {
    foreach ($f in $Payload.findings) {
      $lines.Add("")
      $lines.Add("- [$($f.severity)] ``$($f.code)``: $($f.message)")
      if ($f.suggestion) {
        $lines.Add("  - Suggested later action: $($f.suggestion)")
      }
    }
  }
  $lines.Add("")
  $lines.Add("## Role summary")
  foreach ($roleName in $Payload.roles.Keys) {
    $value = $Payload.roles[$roleName]
    $lines.Add("- ``$roleName``: ``$($value.status)`` / processes ``$($value.count)``")
  }
  $lines.Add("")
  $lines.Add("## Artifact summary")
  foreach ($artifact in $Payload.artifacts) {
    $lines.Add("- ``$($artifact.path)``: ``$($artifact.status)`` / age ``$($artifact.age_seconds)`` seconds")
  }
  $lines.Add("")
  $lines.Add("Raw history is appended to ``$ObservationJsonl``.")
  Set-Content -Path $mdPath -Value $lines -Encoding utf8
}

$tick = 0
while ($true) {
  $tick += 1

  $rolePatterns = [ordered]@{
    recorder = "main.py\s+run"
    source_online_supervisor = "source-online-supervisor"
    execution_safety_cache_supervisor = "execution-safety-cache-supervisor"
    trading_dashboard_loop = "trading-dashboard-loop"
    dashboard_viewer = "http.server\s+8765"
    live_scorer_learning = "main.py\s+live-scorer-learning|RUN_COMMAND=.*main.py\s+live-scorer-learning"
    live_learning_llm_notes = "main.py\s+live-learning-llm-notes|RUN_COMMAND=.*main.py\s+live-learning-llm-notes"
    threshold_candidate_evaluator = "main.py\s+threshold-candidate-evaluator|RUN_COMMAND=.*main.py\s+threshold-candidate-evaluator"
    live_learning_control_plane = "recorder\.live_learning_control_plane|live_learning_control_plane|live-learning-control-plane"
    capital_refresh_loop = "run_capital_refresh_loop\.ps1|capital-margin-state"
    artifact_cleanup_loop = "run_artifact_cleanup_loop\.ps1|artifact-cleanup"
  }

  $roles = [ordered]@{}
  foreach ($name in $rolePatterns.Keys) {
    $roleMatches = @(Get-ProcessMatches -Pattern $rolePatterns[$name])
    $roleMatchCount = @($roleMatches).Count
    $required = $name -in @("recorder", "source_online_supervisor", "trading_dashboard_loop", "dashboard_viewer")
    if ($RequireLearningLoops -and $name -in @("live_scorer_learning", "live_learning_llm_notes", "threshold_candidate_evaluator", "live_learning_control_plane")) {
      $required = $true
    }
    $status = "green"
    if ($roleMatchCount -eq 0 -and $required) {
      $status = "red"
    } elseif ($roleMatchCount -eq 0) {
      $status = "not_running_optional"
    }
    $roles[$name] = [ordered]@{
      required = $required
      status = $status
      count = $roleMatchCount
      pids = @($roleMatches | ForEach-Object { $_.ProcessId })
    }
  }

  $artifacts = @(
    Get-ArtifactInfo -Path "exports\m173_source_online_supervisor.json" -WarnAgeSeconds 180 -RedAgeSeconds 600
    Get-ArtifactInfo -Path "exports\ibkr_connection_status.json" -WarnAgeSeconds 30 -RedAgeSeconds 180
    Get-ArtifactInfo -Path "exports\m153_market_data_desk_refresh.json" -WarnAgeSeconds 180 -RedAgeSeconds 600
    Get-ArtifactInfo -Path "exports\m179_trading_dashboard_snapshot.json" -WarnAgeSeconds 20 -RedAgeSeconds 120
    Get-ArtifactInfo -Path "exports\trading_command_center.html" -WarnAgeSeconds 20 -RedAgeSeconds 120
    Get-ArtifactInfo -Path "exports\m152_evidence_freshness_desk.json" -WarnAgeSeconds 600 -RedAgeSeconds 1800
    Get-ArtifactInfo -Path "exports\m223_live_scorer_learning.json" -WarnAgeSeconds 600 -RedAgeSeconds 1800
    Get-ArtifactInfo -Path "exports\m225_live_learning_llm_notes.json" -WarnAgeSeconds 1800 -RedAgeSeconds 7200
    Get-ArtifactInfo -Path "exports\m226_threshold_candidate_evaluator.json" -WarnAgeSeconds 1800 -RedAgeSeconds 7200
    Get-ArtifactInfo -Path "exports\m237_capital_margin_state.json" -WarnAgeSeconds 900 -RedAgeSeconds 1800
    Get-ArtifactInfo -Path "exports\m244_capital_pipeline_orchestrator.json" -WarnAgeSeconds 900 -RedAgeSeconds 1800
    Get-ArtifactInfo -Path "exports\artifact_cleanup_latest.json" -WarnAgeSeconds 7200 -RedAgeSeconds 14400
    Get-ArtifactInfo -Path "exports\m232_hot_cache.json" -WarnAgeSeconds 120 -RedAgeSeconds 300
    Get-ArtifactInfo -Path "exports\m232_live_learning_control_plane.json" -WarnAgeSeconds 900 -RedAgeSeconds 1800
    Get-ArtifactInfo -Path "exports\m247_micro_option_candidates.json" -WarnAgeSeconds 1800 -RedAgeSeconds 7200
    Get-ArtifactInfo -Path "exports\m245_capital_trade_executable_separation.json" -WarnAgeSeconds 1800 -RedAgeSeconds 7200
    Get-ArtifactInfo -Path "exports\m248_paper_option_ticket_preview.json" -WarnAgeSeconds 1800 -RedAgeSeconds 7200
    Get-ArtifactInfo -Path "exports\m249_option_whatif_safety_validation.json" -WarnAgeSeconds 1800 -RedAgeSeconds 7200
    Get-ArtifactInfo -Path "exports\m250_limited_auto_trading_readiness.json" -WarnAgeSeconds 1800 -RedAgeSeconds 7200
  )

  $findings = New-Object System.Collections.Generic.List[hashtable]
  foreach ($roleName in $roles.Keys) {
    $role = $roles[$roleName]
    if ($role.required -and $role.count -eq 0) {
      $findings.Add((New-Finding -Severity "red" -Code "required_role_not_running" -Message "$roleName is not running." -Suggestion "Restart the corresponding run_*_windows.bat wrapper or inspect the hidden process logs."))
    }
  }

  foreach ($artifact in $artifacts) {
    $expectedDeferredArtifact = $false
    if (
      $artifact.path -like "*m247_micro_option_candidates.json" -or
      $artifact.path -like "*m248_paper_option_ticket_preview.json" -or
      $artifact.path -like "*m249_option_whatif_safety_validation.json" -or
      $artifact.path -like "*m250_limited_auto_trading_readiness.json"
    ) {
      $m247Payload = Read-JsonArtifact -Path $artifact.path
      if ($null -ne $m247Payload) {
        $summaryExpectedDeferred = $false
        if ($null -ne $m247Payload.summary) {
          $summaryExpectedDeferred = [bool]$m247Payload.summary.expected_deferred
        }
        $expectedDeferredArtifact = ([bool]$m247Payload.expected_deferred) -or $summaryExpectedDeferred -or ([string]$m247Payload.status -eq "expected_deferred") -or ([string]$m247Payload.readiness_level -eq "session_deferred_normal")
      }
    }
    if ($artifact.status -eq "missing") {
      $findings.Add((New-Finding -Severity "yellow" -Code "artifact_missing" -Message "$($artifact.path) is missing." -Suggestion "If this artifact is expected in the current session, run or inspect its producer role."))
    } elseif ($artifact.status -eq "red" -and -not $expectedDeferredArtifact) {
      $findings.Add((New-Finding -Severity "yellow" -Code "artifact_stale" -Message "$($artifact.path) is stale at $($artifact.age_seconds) seconds." -Suggestion "Check whether the relevant market/session is closed before treating this as a bug. If session is open, inspect producer logs."))
    }
  }

  $ibkrConnection = Read-JsonArtifact -Path "exports\ibkr_connection_status.json"
  if ($null -eq $ibkrConnection) {
    $findings.Add((New-Finding -Severity "yellow" -Code "ibkr_connection_status_missing" -Message "IBKR connection status artifact is missing." -Suggestion "Start or restart the IBKR Realtime Recorder so it can publish reconnect state."))
  } else {
    $ibkrStatus = [string]$ibkrConnection.status
    if ($ibkrStatus -in @("reconnecting", "reconnect_wait", "reconnect_scheduled", "connecting", "reconnect_failed")) {
      $findings.Add((New-Finding -Severity "yellow" -Code "ibkr_gateway_reconnect_in_progress" -Message "IBKR Gateway connection is $ibkrStatus; auto-reconnect is active." -Suggestion "If this persists, confirm IB Gateway is logged in and API socket settings are enabled."))
    } elseif ($ibkrStatus -notin @("connected", "starting")) {
      $findings.Add((New-Finding -Severity "red" -Code "ibkr_gateway_not_connected" -Message "IBKR Gateway connection status is $ibkrStatus." -Suggestion "Confirm IB Gateway/TWS is running and logged in. The recorder should retry automatically if reconnect is enabled."))
    }
  }

  if (!$RequireLearningLoops -and ($roles.live_scorer_learning.count -eq 0 -or $roles.live_learning_llm_notes.count -eq 0 -or $roles.threshold_candidate_evaluator.count -eq 0 -or $roles.live_learning_control_plane.count -eq 0)) {
    $findings.Add((New-Finding -Severity "info" -Code "learning_loop_not_all_running" -Message "One or more learning loops are not running. Collection can still be online, but full training is not guaranteed." -Suggestion "If active training is desired, explicitly start live-scorer-learning, live-learning-llm-notes, threshold-candidate-evaluator, and live-learning-control-plane loops."))
  }

  $status = "green"
  if (@($findings | Where-Object { $_.severity -eq "red" }).Count -gt 0) {
    $status = "red"
  } elseif (@($findings | Where-Object { $_.severity -eq "yellow" }).Count -gt 0) {
    $status = "yellow"
  }

  $payload = [ordered]@{
    observer_version = "runtime-watchdog-observer-v1"
    generated_at_local = Get-IsoNowLocal
    generated_at_utc = Get-IsoNowUtc
    tick = $tick
    status = $status
    note_policy = "Record bugs and improvement candidates for later batch processing; do not auto-fix."
    require_learning_loops = [bool]$RequireLearningLoops
    roles = $roles
    artifacts = $artifacts
    findings = @($findings)
  }

  Write-Observation -Payload $payload

  if ($MaxTicks -gt 0 -and $tick -ge $MaxTicks) {
    break
  }
  Start-Sleep -Seconds ([Math]::Max(1, $IntervalSeconds))
}
