from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .cli_profile_utils import (
    coalesce_profile_path as _coalesce_profile_path,
    coalesce_profile_value as _coalesce_profile_value,
    profile_path_value as _profile_path_value,
)


def _write_markdown_text_atomic(path: Path | None, text: str) -> Path | None:
    if path is None:
        return None
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{int(time.time() * 1000)}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    return path


def _run_watch_evidence_refresh_watchdog(
    *,
    profile: dict[str, Any],
    profile_base: Path | None,
    run_id: str,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    watchdog_profile = profile.get("evidence_refresh_watchdog") if isinstance(profile.get("evidence_refresh_watchdog"), dict) else {}
    if not bool(watchdog_profile.get("enabled", False)) or bool(watchdog_profile.get("ignore", False)):
        return None, {"enabled": bool(watchdog_profile.get("enabled", False)), "applied": False, "reason": "disabled_or_ignored"}

    from recorder.evidence_operations import (
        EvidenceRefreshWatchdogConfig,
        evidence_refresh_watchdog_markdown,
        run_evidence_refresh_watchdog,
        write_evidence_refresh_watchdog_json,
    )

    freshness_profile_path = _profile_path_value(
        watchdog_profile.get("freshness_profile_json") or "../config/evidence_freshness_desk_profile_v1.json",
        profile_base,
    )
    refresh_queue_profile_path = _profile_path_value(
        watchdog_profile.get("refresh_queue_profile_json") or "../config/evidence_refresh_queue_profile_v1.json",
        profile_base,
    )
    freshness_report_path = _profile_path_value(
        watchdog_profile.get("freshness_report_json") or "../exports/m152_evidence_freshness_desk.json",
        profile_base,
    )
    refresh_queue_json_path = _profile_path_value(
        watchdog_profile.get("refresh_queue_json") or "../exports/m153_evidence_refresh_queue_execute.json",
        profile_base,
    )
    refresh_queue_markdown_path = _profile_path_value(
        watchdog_profile.get("refresh_queue_markdown") or "../exports/m153_evidence_refresh_queue_execute.md",
        profile_base,
    )
    state_json_path = _profile_path_value(
        watchdog_profile.get("state_json_path") or "../exports/m154_evidence_refresh_watchdog_state.json",
        profile_base,
    )
    tick_log_jsonl_path = _profile_path_value(
        watchdog_profile.get("tick_log_jsonl_path") or "../exports/m157_evidence_refresh_watchdog_ticks.jsonl",
        profile_base,
    )
    output_path = _profile_path_value(
        watchdog_profile.get("output") or "../exports/m157_evidence_refresh_watchdog_latest.md",
        profile_base,
    )
    json_output_path = _profile_path_value(
        watchdog_profile.get("json_output_path") or "../exports/m157_evidence_refresh_watchdog_latest.json",
        profile_base,
    )
    dry_run = bool(watchdog_profile.get("dry_run", False))
    config = EvidenceRefreshWatchdogConfig(
        freshness_profile_path=freshness_profile_path,
        refresh_queue_profile_path=refresh_queue_profile_path,
        freshness_report_path=freshness_report_path,
        refresh_queue_json_path=refresh_queue_json_path,
        refresh_queue_markdown_path=refresh_queue_markdown_path,
        state_json_path=state_json_path,
        tick_log_jsonl_path=tick_log_jsonl_path,
        max_ticks=int(watchdog_profile.get("max_ticks", 1)),
        interval_seconds=float(watchdog_profile.get("interval_seconds", 1.0)),
        sleep_between_ticks=bool(watchdog_profile.get("sleep_between_ticks", False)),
        cooldown_seconds=float(watchdog_profile.get("cooldown_seconds", 300.0)),
        initial_backoff_seconds=float(watchdog_profile.get("initial_backoff_seconds", 120.0)),
        max_backoff_seconds=float(watchdog_profile.get("max_backoff_seconds", 1800.0)),
        queue_timeout_seconds=float(watchdog_profile.get("queue_timeout_seconds", 240.0)),
        execute_refresh=not dry_run,
        run_freshness_recheck=not bool(watchdog_profile.get("skip_freshness_recheck", False)),
    )
    should_skip, latency_budget = _latency_budget_artifact_skip(
        profile=profile,
        stage_key="evidence_refresh_watchdog",
        artifact_path=json_output_path,
    )
    if should_skip:
        result = _latency_budget_read_json(json_output_path)
        return (
            result,
            {
                "enabled": True,
                "applied": False,
                "dry_run": dry_run,
                "status": "skipped_latency_budget_recent_artifact",
                "previous_status": result.get("final_status"),
                "freshness_status": result.get("final_freshness_status"),
                "stale_source_keys": result.get("final_stale_source_keys") or [],
                "lagging_source_keys": result.get("final_lagging_source_keys") or [],
                "consecutive_failures": result.get("consecutive_failures"),
                "state_json_path": str(state_json_path.expanduser().resolve()) if state_json_path else None,
                "json_output_path": str(json_output_path.expanduser().resolve()) if json_output_path else None,
                "latency_budget": latency_budget,
                "safety": {
                    "existing_artifact_reused": True,
                    "refresh_queue_not_executed_inline": True,
                    "evidence_gates_still_read_latest_artifact": True,
                    "fail_closed_if_artifact_stale_or_missing": True,
                },
            },
        )
    result = run_evidence_refresh_watchdog(
        config=config,
        cwd=Path.cwd(),
        run_id=str(watchdog_profile.get("run_id") or f"{run_id}_evidence_refresh_watchdog"),
    )
    if json_output_path is not None:
        write_evidence_refresh_watchdog_json(json_output_path, result)
    if output_path is not None:
        _write_markdown_text_atomic(output_path, evidence_refresh_watchdog_markdown(result))
    return (
        result,
        {
            "enabled": True,
            "applied": True,
            "dry_run": dry_run,
            "status": result.get("final_status"),
            "freshness_status": result.get("final_freshness_status"),
            "stale_source_keys": result.get("final_stale_source_keys") or [],
            "lagging_source_keys": result.get("final_lagging_source_keys") or [],
            "consecutive_failures": result.get("consecutive_failures"),
            "state_json_path": str(state_json_path.expanduser().resolve()) if state_json_path else None,
            "json_output_path": str(json_output_path.expanduser().resolve()) if json_output_path else None,
            "latency_budget": latency_budget,
        },
    )


def _latency_budget_stage_policy(profile: dict[str, Any], stage_key: str) -> dict[str, Any]:
    budget = profile.get("dashboard_latency_budget") if isinstance(profile.get("dashboard_latency_budget"), dict) else {}
    if not bool(budget.get("enabled", False)):
        return {"enabled": False}
    stages = budget.get("stages") if isinstance(budget.get("stages"), dict) else {}
    stage = stages.get(stage_key) if isinstance(stages.get(stage_key), dict) else {}
    if not stage:
        return {"enabled": False}
    return {
        "enabled": bool(stage.get("enabled", True)),
        "mode": str(stage.get("mode") or "skip_when_recent_artifact"),
        "max_artifact_age_seconds": float(stage.get("max_artifact_age_seconds", budget.get("default_max_artifact_age_seconds", 30.0))),
        "safety_fallback": str(stage.get("safety_fallback") or "use_existing_artifact_and_fail_closed_gates"),
    }


def _latency_budget_artifact_skip(
    *,
    profile: dict[str, Any],
    stage_key: str,
    artifact_path: Path | None,
) -> tuple[bool, dict[str, Any]]:
    policy = _latency_budget_stage_policy(profile, stage_key)
    if not policy.get("enabled") or policy.get("mode") != "skip_when_recent_artifact":
        return False, {"enabled": False, "stage_key": stage_key}
    if artifact_path is None:
        return False, {"enabled": True, "stage_key": stage_key, "reason": "artifact_path_missing", "policy": policy}
    resolved = artifact_path.expanduser().resolve()
    if not resolved.exists():
        return False, {"enabled": True, "stage_key": stage_key, "reason": "artifact_missing", "policy": policy, "artifact_path": str(resolved)}
    age_seconds = max(0.0, time.time() - resolved.stat().st_mtime)
    max_age = float(policy.get("max_artifact_age_seconds") or 0.0)
    decision = {
        "enabled": True,
        "stage_key": stage_key,
        "policy": policy,
        "artifact_path": str(resolved),
        "artifact_age_seconds": round(age_seconds, 3),
        "max_artifact_age_seconds": max_age,
        "safety_fallback": policy.get("safety_fallback"),
    }
    if age_seconds <= max_age:
        return True, {**decision, "reason": "recent_artifact_within_latency_budget"}
    return False, {**decision, "reason": "artifact_older_than_latency_budget"}


def _latency_budget_read_json(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        return {}
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _run_dashboard_source_ingest_health(
    *,
    profile: dict[str, Any],
    profile_base: Path | None,
    run_id: str,
) -> dict[str, Any]:
    health_profile = profile.get("source_ingest_health") if isinstance(profile.get("source_ingest_health"), dict) else {}
    if not bool(health_profile.get("enabled", False)) or bool(health_profile.get("ignore", False)):
        return {"enabled": bool(health_profile.get("enabled", False)), "applied": False, "reason": "disabled_or_ignored"}

    from recorder.source_operations import (
        DEFAULT_BATCH_SOURCES,
        DEFAULT_ONLINE_REQUIRED_SOURCES,
        SourceIngestHealthConfig,
        run_source_ingest_health_loop,
    )

    def csv_or_default(value: Any, default: tuple[str, ...]) -> tuple[str, ...]:
        if isinstance(value, list):
            return tuple(str(item).strip() for item in value if str(item).strip())
        if value:
            return tuple(item.strip() for item in str(value).split(",") if item.strip())
        return default

    freshness_report_path = _profile_path_value(
        health_profile.get("freshness_report_json") or "../exports/m152_evidence_freshness_desk.json",
        profile_base,
    )
    refresh_queue_path = _profile_path_value(
        health_profile.get("refresh_queue_json") or "../exports/m153_evidence_refresh_queue_execute.json",
        profile_base,
    )
    watchdog_path = _profile_path_value(
        health_profile.get("watchdog_json")
        or ((profile.get("paths") or {}).get("evidence_refresh_watchdog_json") if isinstance(profile.get("paths"), dict) else None)
        or "../exports/m159_dashboard_evidence_refresh_watchdog_latest.json",
        profile_base,
    )
    live_validation_path = _profile_path_value(
        health_profile.get("live_validation_json") or "../exports/m161_source_live_validation.json",
        profile_base,
    )
    output_path = _profile_path_value(health_profile.get("output") or "../exports/m160_source_ingest_health.md", profile_base)
    json_output_path = _profile_path_value(health_profile.get("json_output_path") or "../exports/m160_source_ingest_health.json", profile_base)
    tick_log_jsonl_path = _profile_path_value(health_profile.get("tick_log_jsonl_path") or "../exports/m160_source_ingest_health_ticks.jsonl", profile_base)
    config = SourceIngestHealthConfig(
        freshness_report_path=freshness_report_path,
        refresh_queue_path=refresh_queue_path,
        watchdog_path=watchdog_path,
        live_validation_path=live_validation_path,
        online_required_sources=csv_or_default(health_profile.get("online_required_sources"), DEFAULT_ONLINE_REQUIRED_SOURCES),
        batch_sources=csv_or_default(health_profile.get("batch_sources"), DEFAULT_BATCH_SOURCES),
        max_ticks=1,
        interval_seconds=float(health_profile.get("interval_seconds", 1.0)),
        sleep_between_ticks=False,
    )
    should_skip, latency_budget = _latency_budget_artifact_skip(
        profile=profile,
        stage_key="source_ingest_health",
        artifact_path=json_output_path,
    )
    if should_skip:
        latest = _latency_budget_read_json(json_output_path)
        return {
            "enabled": True,
            "applied": False,
            "status": "skipped_latency_budget_recent_artifact",
            "previous_status": latest.get("overall_status"),
            "online_problem_source_keys": latest.get("online_problem_source_keys") or [],
            "degraded_source_keys": latest.get("degraded_source_keys") or [],
            "root_cause_counts": latest.get("root_cause_counts") or {},
            "json_output_path": str(json_output_path.expanduser().resolve()) if json_output_path else None,
            "latency_budget": latency_budget,
            "safety": {
                "existing_artifact_reused": True,
                "source_health_recheck_not_executed_inline": True,
                "dashboard_still_reads_latest_source_health_artifact": True,
                "fail_closed_if_artifact_stale_or_missing": True,
            },
        }
    result = run_source_ingest_health_loop(
        config=config,
        output_path=output_path,
        json_output_path=json_output_path,
        tick_log_jsonl_path=tick_log_jsonl_path,
        run_id=str(health_profile.get("run_id") or f"{run_id}_source_ingest_health"),
    )
    latest = result.get("latest_report") or {}
    return {
        "enabled": True,
        "applied": True,
        "status": latest.get("overall_status"),
        "online_problem_source_keys": latest.get("online_problem_source_keys") or [],
        "degraded_source_keys": latest.get("degraded_source_keys") or [],
        "root_cause_counts": latest.get("root_cause_counts") or {},
        "json_output_path": str(json_output_path.expanduser().resolve()) if json_output_path else None,
        "latency_budget": latency_budget,
    }


def _run_dashboard_market_data_session_overlay(
    *,
    profile: dict[str, Any],
    profile_base: Path | None,
    run_id: str,
) -> dict[str, Any]:
    overlay_profile = profile.get("market_data_session_overlay") if isinstance(profile.get("market_data_session_overlay"), dict) else {}
    if not bool(overlay_profile.get("enabled", False)) or bool(overlay_profile.get("ignore", False)):
        return {"enabled": bool(overlay_profile.get("enabled", False)), "applied": False, "reason": "disabled_or_ignored"}

    from recorder.market_data_session_overlay import (
        build_market_data_session_overlay,
        market_data_session_overlay_markdown,
        write_market_data_session_overlay_json,
        write_market_data_session_overlay_markdown,
    )

    paths = profile.get("paths") if isinstance(profile.get("paths"), dict) else {}
    market_data_path = _profile_path_value(
        overlay_profile.get("market_data_json") or paths.get("market_data_json") or "../exports/m153_market_data_desk_refresh.json",
        profile_base,
    )
    output_path = _profile_path_value(overlay_profile.get("output") or "../exports/m194_market_data_session_overlay.md", profile_base)
    json_output_path = _profile_path_value(
        overlay_profile.get("json_output_path") or paths.get("market_data_session_overlay_json") or "../exports/m194_market_data_session_overlay.json",
        profile_base,
    )
    if market_data_path is None or not market_data_path.expanduser().resolve().exists():
        return {"enabled": True, "applied": False, "status": "failed", "reason": "market_data_json_missing", "market_data_json": str(market_data_path) if market_data_path else None}
    payload = json.loads(market_data_path.expanduser().resolve().read_text(encoding="utf-8"))
    result = build_market_data_session_overlay(payload, now=overlay_profile.get("now"))
    json_written = write_market_data_session_overlay_json(json_output_path, result) if json_output_path else None
    markdown_written = write_market_data_session_overlay_markdown(output_path, result) if output_path else None
    return {
        "enabled": True,
        "applied": True,
        "hook": "market_data_session_overlay",
        "run_id": run_id,
        "session": (result.get("session") or {}).get("session"),
        "risk_interpretation": result.get("risk_interpretation"),
        "monitorability_counts": (result.get("summary") or {}).get("monitorability_counts") or {},
        "market_data_json": str(market_data_path.expanduser().resolve()) if market_data_path else None,
        "json_output_path": str(json_written.expanduser().resolve()) if json_written else None,
        "output": str(markdown_written.expanduser().resolve()) if markdown_written else None,
    }


def _run_dashboard_market_data_risk_gate(
    *,
    profile: dict[str, Any],
    profile_base: Path | None,
    run_id: str,
) -> dict[str, Any]:
    gate_profile = profile.get("market_data_risk_gate") if isinstance(profile.get("market_data_risk_gate"), dict) else {}
    if not bool(gate_profile.get("enabled", False)) or bool(gate_profile.get("ignore", False)):
        return {"enabled": bool(gate_profile.get("enabled", False)), "applied": False, "reason": "disabled_or_ignored"}

    from recorder.market_data_risk_gate import (
        build_market_data_risk_gate_report,
        market_data_risk_gate_markdown,
        write_market_data_risk_gate_json,
    )

    paths = profile.get("paths") if isinstance(profile.get("paths"), dict) else {}
    market_data_path = _profile_path_value(gate_profile.get("market_data_json") or paths.get("market_data_json") or "../exports/m153_market_data_desk_refresh.json", profile_base)
    market_data_session_overlay_path = _profile_path_value(
        gate_profile.get("market_data_session_overlay_json") or paths.get("market_data_session_overlay_json") or "../exports/m194_market_data_session_overlay.json",
        profile_base,
    )
    macro_release_interpreter_path = _profile_path_value(
        gate_profile.get("macro_release_interpreter_json") or paths.get("macro_release_interpreter_json") or "../exports/m237_macro_release_interpreter.json",
        profile_base,
    )
    options_market_structure_path = _profile_path_value(
        gate_profile.get("options_market_structure_json") or paths.get("options_market_structure_json") or "../exports/m217_options_market_structure_desk.json",
        profile_base,
    )
    options_market_structure_supervisor_path = _profile_path_value(
        gate_profile.get("options_market_structure_supervisor_json") or paths.get("options_market_structure_supervisor_json") or "../exports/m219_options_market_structure_supervisor.json",
        profile_base,
    )
    output_path = _profile_path_value(gate_profile.get("gate_output") or "../exports/m175_dashboard_market_data_risk_gate_latest.md", profile_base)
    json_output_path = _profile_path_value(gate_profile.get("gate_json_output_path") or paths.get("market_data_risk_gate_json") or "../exports/m175_dashboard_market_data_risk_gate_latest.json", profile_base)
    if market_data_path is None or not market_data_path.expanduser().resolve().exists():
        return {"enabled": True, "applied": False, "status": "failed", "reason": "market_data_json_missing", "market_data_json": str(market_data_path) if market_data_path else None}
    payload = json.loads(market_data_path.expanduser().resolve().read_text(encoding="utf-8"))
    session_payload = None
    if market_data_session_overlay_path and market_data_session_overlay_path.expanduser().resolve().exists():
        session_payload = json.loads(market_data_session_overlay_path.expanduser().resolve().read_text(encoding="utf-8"))
    macro_release_payload = None
    if macro_release_interpreter_path and macro_release_interpreter_path.expanduser().resolve().exists():
        macro_release_payload = json.loads(macro_release_interpreter_path.expanduser().resolve().read_text(encoding="utf-8"))
    options_market_structure_payload = None
    if options_market_structure_path and options_market_structure_path.expanduser().resolve().exists():
        options_market_structure_payload = json.loads(options_market_structure_path.expanduser().resolve().read_text(encoding="utf-8"))
    options_market_structure_supervisor_payload = None
    if options_market_structure_supervisor_path and options_market_structure_supervisor_path.expanduser().resolve().exists():
        options_market_structure_supervisor_payload = json.loads(options_market_structure_supervisor_path.expanduser().resolve().read_text(encoding="utf-8"))
    result = build_market_data_risk_gate_report(
        payload,
        market_data_session_overlay_payload=session_payload,
        macro_release_interpreter_payload=macro_release_payload,
        options_market_structure_payload=options_market_structure_payload,
        options_market_structure_supervisor_payload=options_market_structure_supervisor_payload,
        block_on_delayed=bool(gate_profile.get("block_on_delayed", True)),
        block_on_missing=bool(gate_profile.get("block_on_missing", True)),
        block_on_warming_up=bool(gate_profile.get("block_on_warming_up", True)),
        block_on_errors=bool(gate_profile.get("block_on_errors", True)),
        block_on_stale=bool(gate_profile.get("block_on_stale", True)),
        block_on_close_only=bool(gate_profile.get("block_on_close_only", False)),
        block_on_severe_wide_spread=bool(gate_profile.get("block_on_severe_wide_spread", False)),
        watch_size_multiplier=float(gate_profile.get("watch_size_multiplier", 0.70)),
        hot_size_multiplier=float(gate_profile.get("hot_size_multiplier", 0.50)),
        defensive_size_multiplier=float(gate_profile.get("defensive_size_multiplier", 0.0)),
        stress_score_threshold=float(gate_profile.get("stress_score_threshold", 30.0)),
        stale_hard_block_instruments=gate_profile.get("stale_hard_block_instruments"),
        monitoring_only_instruments=gate_profile.get("monitoring_only_instruments"),
        regular_session_only_stale_hard_block_instruments=gate_profile.get("regular_session_only_stale_hard_block_instruments"),
        stale_hard_block_proxy_groups=gate_profile.get("stale_hard_block_proxy_groups"),
        macro_event_window_minutes=float(gate_profile.get("macro_event_window_minutes", 180.0)),
        closed_session_expected_stale_min_wait_hours=float(gate_profile.get("closed_session_expected_stale_min_wait_hours", 24.0)),
    )
    result["artifact_role"] = "canonical_latest"
    result["canonical_latest_artifact"] = str(json_output_path.expanduser().resolve()) if json_output_path else "exports/m175_dashboard_market_data_risk_gate_latest.json"
    result["canonical_latest_policy"] = "dashboard_m175_is_canonical_latest; m174 is a manual/debug report"
    write_market_data_risk_gate_json(json_output_path, result)
    _write_markdown_text_atomic(output_path, market_data_risk_gate_markdown(result))
    return {
        "enabled": True,
        "applied": True,
        "hook": "market_data_risk_gate",
        "run_id": run_id,
        "gate_status": result.get("gate_status"),
        "gate_action": result.get("action"),
        "entry_size_multiplier": result.get("entry_size_multiplier"),
        "market_data_session_priority_class": (result.get("market_data_session_context") or {}).get("session_priority_class"),
        "session_adjusted_priority": result.get("session_adjusted_priority"),
        "market_data_json": str(market_data_path.expanduser().resolve()) if market_data_path else None,
        "market_data_session_overlay_json": str(market_data_session_overlay_path.expanduser().resolve()) if market_data_session_overlay_path else None,
        "macro_release_interpreter_json": str(macro_release_interpreter_path.expanduser().resolve()) if macro_release_interpreter_path else None,
        "options_market_structure_json": str(options_market_structure_path.expanduser().resolve()) if options_market_structure_path else None,
        "options_market_structure_supervisor_json": str(options_market_structure_supervisor_path.expanduser().resolve()) if options_market_structure_supervisor_path else None,
        "json_output_path": str(json_output_path.expanduser().resolve()) if json_output_path else None,
    }


def _run_dashboard_session_transition_closed_loop(
    *,
    profile: dict[str, Any],
    profile_base: Path | None,
    run_id: str,
) -> dict[str, Any]:
    loop_profile = (
        profile.get("market_data_session_transition_closed_loop")
        if isinstance(profile.get("market_data_session_transition_closed_loop"), dict)
        else {}
    )
    if not bool(loop_profile.get("enabled", False)) or bool(loop_profile.get("ignore", False)):
        return {"enabled": bool(loop_profile.get("enabled", False)), "applied": False, "reason": "disabled_or_ignored"}

    from recorder.market_transition_operations import (
        market_data_session_transition_closed_loop_markdown,
        run_market_data_session_transition_closed_loop,
        write_market_data_session_transition_closed_loop_json,
        write_market_data_session_transition_closed_loop_markdown,
    )

    paths = profile.get("paths") if isinstance(profile.get("paths"), dict) else {}
    outputs = profile.get("outputs") if isinstance(profile.get("outputs"), dict) else {}

    market_data_json = _profile_path_value(
        loop_profile.get("market_data_json") or paths.get("market_data_json") or "../exports/m153_market_data_desk_refresh.json",
        profile_base,
    )
    market_data_session_overlay_json = _profile_path_value(
        loop_profile.get("market_data_session_overlay_json") or paths.get("market_data_session_overlay_json") or "../exports/m194_market_data_session_overlay.json",
        profile_base,
    )
    state_json_path = _profile_path_value(
        loop_profile.get("state_json_path") or "../exports/m199_market_data_session_transition_state.json",
        profile_base,
    )
    market_data_risk_gate_json = _profile_path_value(
        loop_profile.get("market_data_risk_gate_json")
        or paths.get("market_data_risk_gate_json")
        or "../exports/m175_dashboard_market_data_risk_gate_latest.json",
        profile_base,
    )
    market_data_operating_auto_gate_json = _profile_path_value(
        loop_profile.get("market_data_operating_auto_gate_json")
        or paths.get("market_data_operating_auto_gate_json")
        or outputs.get("market_data_operating_auto_gate_json_output_path")
        or "../exports/m182_market_data_operating_auto_gate_latest.json",
        profile_base,
    )
    preview_json = _profile_path_value(
        loop_profile.get("preview_json") or "../exports/m112_m111_order_preview.json",
        profile_base,
    )
    short_availability_json = _profile_path_value(
        loop_profile.get("short_availability_json") or "../exports/m113_ibkr_short_availability.json",
        profile_base,
    )
    evidence_preflight_json = _profile_path_value(
        loop_profile.get("evidence_preflight_json") or "../exports/m156_evidence_preflight_latest.json",
        profile_base,
    )
    output_dir = _profile_path_value(loop_profile.get("output_dir") or "../exports", profile_base) or Path("exports")
    output_path = _profile_path_value(
        loop_profile.get("output") or "../exports/m199_market_data_session_transition_closed_loop.md",
        profile_base,
    )
    json_output_path = _profile_path_value(
        loop_profile.get("json_output_path") or "../exports/m199_market_data_session_transition_closed_loop.json",
        profile_base,
    )
    missing_required: list[str] = []
    for label, path in {
        "market_data_json": market_data_json,
        "market_data_session_overlay_json": market_data_session_overlay_json,
        "preview_json": preview_json,
    }.items():
        if path is None or not path.expanduser().resolve().exists():
            missing_required.append(label)
    if missing_required:
        return {
            "enabled": True,
            "applied": False,
            "status": "failed",
            "reason": "required_input_missing",
            "missing_required": missing_required,
        }

    def csv_tuple(value: Any, default: tuple[str, ...]) -> tuple[str, ...]:
        if isinstance(value, list):
            parsed = tuple(str(item).strip() for item in value if str(item).strip())
            return parsed or default
        if value:
            parsed = tuple(item.strip() for item in str(value).split(",") if item.strip())
            return parsed or default
        return default

    result = run_market_data_session_transition_closed_loop(
        market_data_json_path=market_data_json,
        current_session_overlay_path=market_data_session_overlay_json,
        previous_state_path=state_json_path,
        market_data_risk_gate_path=market_data_risk_gate_json,
        market_data_operating_auto_gate_path=market_data_operating_auto_gate_json,
        preview_json_path=preview_json,
        short_availability_json_path=short_availability_json,
        evidence_preflight_json_path=evidence_preflight_json,
        output_dir=output_dir,
        execute=not bool(loop_profile.get("dry_run", False)),
        max_session_overlay_age_seconds=float(loop_profile.get("max_session_overlay_age_seconds", 180.0)),
        max_risk_gate_age_seconds=float(loop_profile.get("max_risk_gate_age_seconds", 180.0)),
        max_operating_gate_age_seconds=float(loop_profile.get("max_operating_gate_age_seconds", 180.0)),
        allow_fractional_shares=bool(loop_profile.get("allow_fractional_shares", True)),
        allow_short_stock=bool(loop_profile.get("allow_short_stock", True)),
        require_short_availability_for_shorts=bool(loop_profile.get("require_short_availability_for_shorts", True)),
        require_broker_whatif=bool(loop_profile.get("require_broker_whatif", False)),
        max_auto_orders=int(loop_profile.get("max_auto_orders", 10)),
        min_cash_buffer=float(loop_profile.get("min_cash_buffer", 50.0)),
        allowed_ticket_types=csv_tuple(loop_profile.get("allowed_ticket_types"), ("stock",)),
        allowed_actions=csv_tuple(loop_profile.get("allowed_actions"), ("BUY", "SELL")),
        sample_limit=int(loop_profile.get("sample_limit", 20)),
    )
    if output_path:
        write_market_data_session_transition_closed_loop_markdown(output_path, result)
    if json_output_path:
        write_market_data_session_transition_closed_loop_json(json_output_path, result)

    final_gate = result.get("final_transition_gate") or {}
    controls = result.get("controls") or {}
    return {
        "enabled": True,
        "applied": True,
        "hook": "market_data_session_transition_closed_loop",
        "run_id": run_id,
        "status": result.get("status"),
        "reason": result.get("reason"),
        "initial_decision": controls.get("initial_decision"),
        "final_decision": controls.get("final_decision"),
        "final_severity": final_gate.get("severity"),
        "final_rebuild_required_before_auto_release": controls.get("final_rebuild_required_before_auto_release"),
        "final_alpha_allowed_after_transition": controls.get("final_ordinary_alpha_entries_allowed_after_transition"),
        "final_max_new_entry_size_multiplier": controls.get("final_max_new_entry_size_multiplier"),
        "step_count": result.get("step_count"),
        "output": str(output_path.expanduser().resolve()) if output_path else None,
        "json_output_path": str(json_output_path.expanduser().resolve()) if json_output_path else None,
        "final_transition_gate_json": (result.get("artifacts") or {}).get("market_data_session_transition_gate_final_json"),
        "artifacts": result.get("artifacts") or {},
        "safety": result.get("safety") or {},
    }


def _run_dashboard_session_transition_release_governor(
    *,
    profile: dict[str, Any],
    profile_base: Path | None,
    run_id: str,
    closed_loop_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    governor_profile = (
        profile.get("session_transition_release_governor")
        if isinstance(profile.get("session_transition_release_governor"), dict)
        else {}
    )
    if not bool(governor_profile.get("enabled", False)) or bool(governor_profile.get("ignore", False)):
        return {"enabled": bool(governor_profile.get("enabled", False)), "applied": False, "reason": "disabled_or_ignored"}

    from recorder.market_transition_operations import (
        build_session_transition_release_governor_from_paths,
        write_session_transition_release_governor_json,
        write_session_transition_release_governor_markdown,
    )

    paths = profile.get("paths") if isinstance(profile.get("paths"), dict) else {}
    outputs = profile.get("outputs") if isinstance(profile.get("outputs"), dict) else {}
    closed_loop_artifacts = (closed_loop_summary or {}).get("artifacts") or {}
    prefer_closed_loop_artifacts = bool(governor_profile.get("prefer_closed_loop_artifacts", True))

    def artifact_path(key: str) -> Path | None:
        value = closed_loop_artifacts.get(key)
        if value:
            return Path(str(value)).expanduser().resolve()
        return None

    transition_gate_json = _profile_path_value(
        governor_profile.get("transition_gate_json")
        or paths.get("session_transition_release_transition_gate_json")
        or paths.get("market_data_session_transition_gate_json")
        or "../exports/m199_market_data_session_transition_gate_final.json",
        profile_base,
    )
    market_data_risk_gate_json = _profile_path_value(
        governor_profile.get("market_data_risk_gate_json")
        or paths.get("market_data_risk_gate_json")
        or "../exports/m175_dashboard_market_data_risk_gate_latest.json",
        profile_base,
    )
    market_data_operating_auto_gate_json = _profile_path_value(
        governor_profile.get("market_data_operating_auto_gate_json")
        or paths.get("market_data_operating_auto_gate_json")
        or outputs.get("market_data_operating_auto_gate_json_output_path")
        or "../exports/m182_market_data_operating_auto_gate_latest.json",
        profile_base,
    )
    if prefer_closed_loop_artifacts:
        transition_gate_json = artifact_path("market_data_session_transition_gate_final_json") or transition_gate_json
        market_data_risk_gate_json = artifact_path("market_data_risk_gate_json") or market_data_risk_gate_json
        market_data_operating_auto_gate_json = artifact_path("market_data_operating_auto_gate_json") or market_data_operating_auto_gate_json

    portfolio_risk_state_json = _profile_path_value(
        governor_profile.get("portfolio_risk_state_json")
        or paths.get("portfolio_risk_state_json")
        or "../exports/m123_paper_portfolio_risk_state.json",
        profile_base,
    )
    evidence_preflight_json = _profile_path_value(
        governor_profile.get("evidence_preflight_json") or "../exports/m156_evidence_preflight_latest.json",
        profile_base,
    )
    operating_final_preflight_json = _profile_path_value(
        governor_profile.get("operating_final_preflight_json"),
        profile_base,
    )
    output_path = _profile_path_value(
        governor_profile.get("output") or "../exports/m201_session_transition_release_governor.md",
        profile_base,
    )
    json_output_path = _profile_path_value(
        governor_profile.get("json_output_path")
        or paths.get("session_transition_release_governor_json")
        or "../exports/m201_session_transition_release_governor.json",
        profile_base,
    )

    missing_required: list[str] = []
    for label, path in {
        "transition_gate_json": transition_gate_json,
        "market_data_risk_gate_json": market_data_risk_gate_json,
        "market_data_operating_auto_gate_json": market_data_operating_auto_gate_json,
        "portfolio_risk_state_json": portfolio_risk_state_json,
        "evidence_preflight_json": evidence_preflight_json,
    }.items():
        if path is None or not path.expanduser().resolve().exists():
            missing_required.append(label)
    if missing_required:
        return {
            "enabled": True,
            "applied": False,
            "hook": "session_transition_release_governor",
            "run_id": run_id,
            "status": "failed",
            "reason": "required_input_missing",
            "missing_required": missing_required,
            "prefer_closed_loop_artifacts": prefer_closed_loop_artifacts,
        }

    result = build_session_transition_release_governor_from_paths(
        transition_gate_path=transition_gate_json,
        market_data_risk_gate_path=market_data_risk_gate_json,
        market_data_operating_auto_gate_path=market_data_operating_auto_gate_json,
        portfolio_risk_state_path=portfolio_risk_state_json,
        evidence_preflight_path=evidence_preflight_json,
        operating_final_preflight_path=operating_final_preflight_json,
        now=governor_profile.get("now"),
    )
    if output_path:
        write_session_transition_release_governor_markdown(output_path, result)
    if json_output_path:
        write_session_transition_release_governor_json(json_output_path, result)

    return {
        "enabled": True,
        "applied": True,
        "hook": "session_transition_release_governor",
        "run_id": run_id,
        "release_status": result.get("release_status"),
        "severity": result.get("severity"),
        "ordinary_alpha_release_allowed": result.get("ordinary_alpha_release_allowed"),
        "max_new_entry_size_multiplier": result.get("max_new_entry_size_multiplier"),
        "reason": result.get("reason"),
        "failed_check_count": len([item for item in result.get("checks") or [] if not item.get("passed")]),
        "check_count": len(result.get("checks") or []),
        "prefer_closed_loop_artifacts": prefer_closed_loop_artifacts,
        "transition_gate_json": str(transition_gate_json.expanduser().resolve()) if transition_gate_json else None,
        "market_data_risk_gate_json": str(market_data_risk_gate_json.expanduser().resolve()) if market_data_risk_gate_json else None,
        "market_data_operating_auto_gate_json": str(market_data_operating_auto_gate_json.expanduser().resolve()) if market_data_operating_auto_gate_json else None,
        "portfolio_risk_state_json": str(portfolio_risk_state_json.expanduser().resolve()) if portfolio_risk_state_json else None,
        "evidence_preflight_json": str(evidence_preflight_json.expanduser().resolve()) if evidence_preflight_json else None,
        "output": str(output_path.expanduser().resolve()) if output_path else None,
        "json_output_path": str(json_output_path.expanduser().resolve()) if json_output_path else None,
        "safety": result.get("safety") or {},
    }


def _run_dashboard_release_governed_recovery_closed_loop(
    *,
    profile: dict[str, Any],
    profile_base: Path | None,
    run_id: str,
    closed_loop_summary: dict[str, Any] | None = None,
    release_governor_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    recovery_profile = (
        profile.get("release_governed_recovery_closed_loop")
        if isinstance(profile.get("release_governed_recovery_closed_loop"), dict)
        else {}
    )
    if not bool(recovery_profile.get("enabled", False)) or bool(recovery_profile.get("ignore", False)):
        return {"enabled": bool(recovery_profile.get("enabled", False)), "applied": False, "reason": "disabled_or_ignored"}

    from recorder.market_transition_operations import run_release_governed_recovery_closed_loop

    paths = profile.get("paths") if isinstance(profile.get("paths"), dict) else {}
    outputs = profile.get("outputs") if isinstance(profile.get("outputs"), dict) else {}
    closed_loop_artifacts = (closed_loop_summary or {}).get("artifacts") or {}
    prefer_closed_loop_artifacts = bool(recovery_profile.get("prefer_closed_loop_artifacts", True))

    def artifact_path(key: str) -> Path | None:
        value = closed_loop_artifacts.get(key)
        if value:
            return Path(str(value)).expanduser().resolve()
        return None

    transition_gate_json = _profile_path_value(
        recovery_profile.get("transition_gate_json")
        or paths.get("market_data_session_transition_gate_json")
        or "../exports/m199_market_data_session_transition_gate_final.json",
        profile_base,
    )
    market_data_risk_gate_json = _profile_path_value(
        recovery_profile.get("market_data_risk_gate_json")
        or "../exports/m199_market_data_risk_gate.json",
        profile_base,
    )
    market_data_operating_auto_gate_json = _profile_path_value(
        recovery_profile.get("market_data_operating_auto_gate_json")
        or "../exports/m199_market_data_operating_auto_gate.json",
        profile_base,
    )
    if prefer_closed_loop_artifacts:
        transition_gate_json = artifact_path("market_data_session_transition_gate_final_json") or transition_gate_json
        market_data_risk_gate_json = artifact_path("market_data_risk_gate_json") or market_data_risk_gate_json
        market_data_operating_auto_gate_json = artifact_path("market_data_operating_auto_gate_json") or market_data_operating_auto_gate_json

    portfolio_risk_state_json = _profile_path_value(
        recovery_profile.get("portfolio_risk_state_json")
        or paths.get("portfolio_risk_state_json")
        or "../exports/m123_paper_portfolio_risk_state.json",
        profile_base,
    )
    evidence_preflight_json = _profile_path_value(
        recovery_profile.get("evidence_preflight_json")
        or "../exports/m156_evidence_preflight_latest.json",
        profile_base,
    )
    operating_final_preflight_json = _profile_path_value(recovery_profile.get("operating_final_preflight_json"), profile_base)
    snapshot_json = _profile_path_value(
        recovery_profile.get("snapshot_json") or outputs.get("json_output_path"),
        profile_base,
    )
    existing_ledger_json = _profile_path_value(
        recovery_profile.get("existing_ledger_json") or paths.get("execution_intent_ledger_json"),
        profile_base,
    )
    existing_router_json = _profile_path_value(recovery_profile.get("existing_router_json"), profile_base)
    marks_json = _profile_path_value(recovery_profile.get("marks_json"), profile_base)
    release_governor_output = _profile_path_value(
        recovery_profile.get("release_governor_output")
        or (release_governor_summary or {}).get("output")
        or "../exports/m206_session_transition_release_governor.md",
        profile_base,
    )
    release_governor_json_output_path = _profile_path_value(
        recovery_profile.get("release_governor_json_output_path")
        or (release_governor_summary or {}).get("json_output_path")
        or paths.get("session_transition_release_governor_json")
        or "../exports/m206_session_transition_release_governor.json",
        profile_base,
    )
    ledger_output = _profile_path_value(
        recovery_profile.get("ledger_output") or "../exports/m206_release_governed_execution_intent_ledger.md",
        profile_base,
    )
    ledger_json_output_path = _profile_path_value(
        recovery_profile.get("ledger_json_output_path")
        or paths.get("execution_intent_ledger_json")
        or "../exports/m206_release_governed_execution_intent_ledger.json",
        profile_base,
    )
    router_output = _profile_path_value(
        recovery_profile.get("router_output") or "../exports/m206_release_governed_paper_order_router.md",
        profile_base,
    )
    router_json_output_path = _profile_path_value(
        recovery_profile.get("router_json_output_path") or "../exports/m206_release_governed_paper_order_router.json",
        profile_base,
    )
    output_path = _profile_path_value(
        recovery_profile.get("output") or "../exports/m206_release_governed_recovery_closed_loop.md",
        profile_base,
    )
    json_output_path = _profile_path_value(
        recovery_profile.get("json_output_path") or "../exports/m206_release_governed_recovery_closed_loop.json",
        profile_base,
    )

    missing_required: list[str] = []
    for label, path in {
        "transition_gate_json": transition_gate_json,
        "market_data_risk_gate_json": market_data_risk_gate_json,
        "market_data_operating_auto_gate_json": market_data_operating_auto_gate_json,
        "portfolio_risk_state_json": portfolio_risk_state_json,
        "evidence_preflight_json": evidence_preflight_json,
    }.items():
        if path is None or not path.expanduser().resolve().exists():
            missing_required.append(label)
    if missing_required:
        return {
            "enabled": True,
            "applied": False,
            "hook": "release_governed_recovery_closed_loop",
            "run_id": run_id,
            "status": "failed",
            "reason": "required_input_missing",
            "missing_required": missing_required,
            "prefer_closed_loop_artifacts": prefer_closed_loop_artifacts,
        }

    release_governor_payload: dict[str, Any] | None = None
    release_json_from_summary = (release_governor_summary or {}).get("json_output_path")
    if release_json_from_summary:
        release_path = Path(str(release_json_from_summary)).expanduser().resolve()
        if release_path.exists():
            loaded = json.loads(release_path.read_text(encoding="utf-8"))
            release_governor_payload = loaded if isinstance(loaded, dict) else None

    started = time.monotonic()
    result = run_release_governed_recovery_closed_loop(
        transition_gate_path=transition_gate_json,
        market_data_risk_gate_path=market_data_risk_gate_json,
        market_data_operating_auto_gate_path=market_data_operating_auto_gate_json,
        portfolio_risk_state_path=portfolio_risk_state_json,
        evidence_preflight_path=evidence_preflight_json,
        operating_final_preflight_path=operating_final_preflight_json,
        snapshot_path=snapshot_json,
        existing_ledger_path=existing_ledger_json,
        existing_router_path=existing_router_json,
        marks_path=marks_json,
        release_governor_payload=release_governor_payload,
        release_governor_markdown_output_path=release_governor_output,
        release_governor_json_output_path=release_governor_json_output_path,
        ledger_markdown_output_path=ledger_output,
        ledger_json_output_path=ledger_json_output_path,
        router_markdown_output_path=router_output,
        router_json_output_path=router_json_output_path,
        monitor_size_multiplier=float(recovery_profile.get("monitor_size_multiplier", 0.50)),
        max_intents=int(recovery_profile.get("max_intents", 10)),
        max_orders=int(recovery_profile.get("max_orders", 10)),
        sample_limit=int(recovery_profile.get("sample_limit", 20)),
        require_operating_preflight=not bool(recovery_profile.get("ignore_operating_preflight", False)),
        allow_non_operating_source_ledger=bool(recovery_profile.get("allow_non_operating_source_ledger", False)),
        require_portfolio_risk_gate=not bool(recovery_profile.get("ignore_portfolio_risk_gate", False)),
        require_session_transition_release_governor=not bool(recovery_profile.get("ignore_session_transition_release_governor", False)),
        fill_price_source=str(recovery_profile.get("fill_price_source") or "local_estimate"),
        now=recovery_profile.get("now"),
    )
    if output_path:
        from recorder.market_transition_operations import write_release_governed_recovery_closed_loop_markdown

        write_release_governed_recovery_closed_loop_markdown(output_path, result)
    if json_output_path:
        from recorder.market_transition_operations import write_release_governed_recovery_closed_loop_json

        write_release_governed_recovery_closed_loop_json(json_output_path, result)

    post_validation = result.get("post_validation") or {}
    return {
        "enabled": True,
        "applied": True,
        "hook": "release_governed_recovery_closed_loop",
        "run_id": run_id,
        "status": result.get("status"),
        "reason": result.get("reason"),
        "duration_seconds": round(time.monotonic() - started, 3),
        "release_governor_reused": bool(release_governor_payload),
        "release_governor_status": (result.get("release_governor") or {}).get("status"),
        "release_governor_allowed": (result.get("release_governor") or {}).get("allowed"),
        "ledger_pending_paper_execution_count": (result.get("ledger_summary") or {}).get("pending_paper_execution_count"),
        "ledger_release_block_count": (result.get("ledger_summary") or {}).get("blocked_by_session_transition_release_governor_count"),
        "router_fill_count": (result.get("router_summary") or {}).get("fill_count"),
        "post_validation_outcome": post_validation.get("outcome"),
        "stale_pending_cleared": post_validation.get("stale_pending_cleared"),
        "release_governed_path_active": post_validation.get("release_governed_path_active"),
        "prefer_closed_loop_artifacts": prefer_closed_loop_artifacts,
        "output": str(output_path.expanduser().resolve()) if output_path else None,
        "json_output_path": str(json_output_path.expanduser().resolve()) if json_output_path else None,
        "artifacts": result.get("artifacts") or {},
        "safety": result.get("safety") or {},
    }
