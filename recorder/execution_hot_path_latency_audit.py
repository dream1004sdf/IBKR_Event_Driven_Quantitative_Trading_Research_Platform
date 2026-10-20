from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


EXECUTION_HOT_PATH_LATENCY_AUDIT_VERSION = "execution-hot-path-latency-audit-v1"


SAFETY_FALSE_KEYS = {
    "ibkr_order_submitted",
    "transmit_enabled",
    "live_order_routing_enabled",
    "portfolio_mutated",
    "scorer_rows_mutated",
    "account_or_portfolio_mutated",
}


def _read_json_timed(path: Path | None) -> tuple[dict[str, Any], float]:
    if path is None:
        return {}, 0.0
    started = time.perf_counter()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}, (time.perf_counter() - started) * 1000.0
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    return (payload if isinstance(payload, dict) else {}), elapsed_ms


def _read_json(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _resolve_path(value: Any, base: Path | None) -> Path | None:
    if value in (None, ""):
        return None
    path = Path(str(value))
    if not path.is_absolute() and base is not None:
        path = base / path
    return path.expanduser().resolve()


def _artifact_age_seconds(path: Path | None) -> float | None:
    if path is None or not path.exists():
        return None
    return max(0.0, time.time() - path.stat().st_mtime)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_get(payload: dict[str, Any], *keys: str) -> Any:
    current: Any = payload
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _safety_payloads(payload: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    safety = payload.get("safety")
    if isinstance(safety, dict):
        items.append(safety)
    for key in (
        "operating_final_preflight",
        "session_transition_release_governor",
        "market_data_risk_gate",
        "evidence_preflight",
        "source_portfolio_risk_gate",
    ):
        nested = payload.get(key)
        if isinstance(nested, dict) and isinstance(nested.get("safety"), dict):
            items.append(nested["safety"])
    return items


def _safety_findings(payload: dict[str, Any], stage: dict[str, Any]) -> dict[str, Any]:
    safety_payloads = _safety_payloads(payload)
    violations: list[str] = []
    observed: dict[str, Any] = {}
    for safety in safety_payloads:
        for key in SAFETY_FALSE_KEYS:
            if key in safety:
                observed[key] = safety.get(key)
                if bool(safety.get(key)):
                    violations.append(f"{key}_true")
        if "transmit" in safety:
            observed["transmit"] = safety.get("transmit")
            if bool(safety.get("transmit")):
                violations.append("transmit_true")
        if "live_order_routing_enabled" in safety:
            observed["live_order_routing_enabled"] = safety.get("live_order_routing_enabled")
    broker_api_called = any(bool(safety.get("ibkr_order_api_called")) for safety in safety_payloads)
    if broker_api_called:
        what_if_only = any(safety.get("what_if_only") is True for safety in safety_payloads)
        transmit_false = any(safety.get("transmit") is False for safety in safety_payloads)
        live_order_not_transmitted = any(safety.get("live_order_transmitted") is False for safety in safety_payloads)
        whatif_api_transmit_flag = any(safety.get("ibkr_whatif_api_transmit_flag") is True for safety in safety_payloads)
        observed["ibkr_order_api_called"] = True
        observed["what_if_only"] = what_if_only
        observed["transmit_false"] = transmit_false
        observed["live_order_not_transmitted"] = live_order_not_transmitted
        observed["ibkr_whatif_api_transmit_flag"] = whatif_api_transmit_flag
        if bool(stage.get("require_what_if_only_when_broker_api_called", True)) and not what_if_only:
            violations.append("broker_api_called_without_what_if_only")
        if not (transmit_false or live_order_not_transmitted):
            violations.append("broker_api_called_without_no_live_transmission_evidence")
    return {
        "observed": observed,
        "violations": sorted(set(violations)),
        "safety_payload_count": len(safety_payloads),
    }


def _extract_stage_status(stage_key: str, payload: dict[str, Any]) -> dict[str, Any]:
    if not payload:
        return {}
    if stage_key == "source_evidence_packet":
        return {
            "status": payload.get("final_status"),
            "source_ingest_health": payload.get("final_source_ingest_health_status"),
            "failed_action_count": payload.get("final_failed_action_count"),
            "online_problem_source_keys": payload.get("final_online_problem_source_keys") or [],
        }
    if stage_key == "market_data_context":
        summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
        temperature = payload.get("temperature") if isinstance(payload.get("temperature"), dict) else {}
        return {
            "status": payload.get("status"),
            "temperature_label": temperature.get("label"),
            "temperature_score": temperature.get("score"),
            "delayed_count": summary.get("delayed_count"),
            "missing_price_count": summary.get("missing_price_count"),
            "alert_count": summary.get("alert_count"),
        }
    if stage_key == "market_data_risk_gate":
        return {
            "gate_status": payload.get("gate_status"),
            "action": payload.get("action"),
            "allowed": payload.get("allowed"),
            "new_entries_allowed": payload.get("new_entries_allowed"),
            "entry_size_multiplier": payload.get("entry_size_multiplier"),
        }
    if stage_key == "operating_auto_gate":
        return {
            "auto_ready_count": payload.get("auto_ready_count"),
            "market_data_operating_mode": payload.get("market_data_operating_mode"),
            "protective_candidate_count": _safe_get(payload, "protective_analysis_packet", "candidate_count"),
            "status_counts": payload.get("auto_gate_status_counts") or {},
        }
    if stage_key == "session_transition_release_governor":
        return {
            "release_status": payload.get("release_status"),
            "ordinary_alpha_release_allowed": payload.get("ordinary_alpha_release_allowed"),
            "max_new_entry_size_multiplier": payload.get("max_new_entry_size_multiplier"),
            "reason": payload.get("reason"),
        }
    if stage_key == "release_governed_execution_intent_ledger":
        return {
            "intent_count": payload.get("intent_count"),
            "pending_paper_execution_count": payload.get("pending_paper_execution_count"),
            "blocked_by_session_transition_release_governor_count": payload.get("blocked_by_session_transition_release_governor_count"),
            "intent_state_counts": payload.get("intent_state_counts") or {},
        }
    if stage_key == "paper_router_preview":
        return {
            "paper_order_count": payload.get("paper_order_count"),
            "paper_fill_count": payload.get("paper_fill_count"),
            "skipped_counts": payload.get("skipped_counts") or {},
            "operating_final_preflight_allowed": payload.get("operating_final_preflight_allowed"),
            "session_transition_release_governor_allowed": payload.get("session_transition_release_governor_allowed"),
        }
    if stage_key == "broker_margin_whatif_cache":
        return {
            "requested_count": payload.get("requested_count"),
            "response_count": payload.get("response_count"),
            "skipped_count": payload.get("skipped_count"),
            "error_count": len(payload.get("errors") or []),
        }
    return {
        "status": payload.get("status") or payload.get("final_status") or payload.get("gate_status"),
    }


def _latest_tick(loop_summary: dict[str, Any]) -> dict[str, Any]:
    ticks = loop_summary.get("tick_summaries")
    if isinstance(ticks, list) and ticks:
        last = ticks[-1]
        return last if isinstance(last, dict) else {}
    return {}


def _observed_loop_latency(loop_summary: dict[str, Any]) -> dict[str, Any]:
    ticks = [item for item in (loop_summary.get("tick_summaries") or []) if isinstance(item, dict)]
    elapsed: list[float] = []
    release_durations: list[float] = []
    release_pass_seen = False
    release_governor_reused = False
    for tick in ticks:
        try:
            elapsed.append(float(tick.get("elapsed_seconds")))
        except (TypeError, ValueError):
            pass
        pre = tick.get("pre_snapshot_refresh") if isinstance(tick.get("pre_snapshot_refresh"), dict) else {}
        release = pre.get("release_governed_recovery_closed_loop") if isinstance(pre.get("release_governed_recovery_closed_loop"), dict) else {}
        if release:
            if release.get("status") == "pass":
                release_pass_seen = True
            if bool(release.get("release_governor_reused")):
                release_governor_reused = True
            try:
                release_durations.append(float(release.get("duration_seconds")))
            except (TypeError, ValueError):
                pass
    latest = _latest_tick(loop_summary)
    return {
        "observed_tick_count": len(ticks),
        "latest_tick_elapsed_seconds": elapsed[-1] if elapsed else None,
        "max_tick_elapsed_seconds": max(elapsed) if elapsed else None,
        "min_tick_elapsed_seconds": min(elapsed) if elapsed else None,
        "latest_pending_paper_execution_count": latest.get("pending_paper_execution_count"),
        "latest_release_governed_plan_blocked_count": latest.get("release_governed_plan_blocked_count"),
        "latest_market_data_operating_mode": latest.get("market_data_operating_mode"),
        "latest_session_transition_release_status": latest.get("session_transition_release_status"),
        "latest_session_transition_release_allowed": latest.get("session_transition_release_allowed"),
        "release_governed_pass_seen": release_pass_seen,
        "release_governor_reused_seen": release_governor_reused,
        "release_governed_inline_duration_seconds_latest": release_durations[-1] if release_durations else None,
        "release_governed_inline_duration_seconds_max": max(release_durations) if release_durations else None,
    }


def _stage_report(
    *,
    stage_key: str,
    stage_policy: dict[str, Any],
    profile_base: Path | None,
) -> dict[str, Any]:
    primary_path = _resolve_path(stage_policy.get("json_path"), profile_base)
    fallback_path = _resolve_path(stage_policy.get("fallback_json_path"), profile_base)
    selected_path = primary_path if primary_path and primary_path.exists() else fallback_path
    exists = bool(selected_path and selected_path.exists())
    payload, read_ms = _read_json_timed(selected_path) if exists else ({}, 0.0)
    age = _artifact_age_seconds(selected_path)
    max_age = float(stage_policy.get("max_artifact_age_seconds", 0.0) or 0.0)
    warn_age = float(stage_policy.get("warn_artifact_age_seconds", max_age) or max_age)
    max_read_ms = float(stage_policy.get("max_read_ms", 999999.0) or 999999.0)
    required = bool(stage_policy.get("required", False))
    fresh = bool(exists and age is not None and (max_age <= 0.0 or age <= max_age))
    warn_stale = bool(exists and age is not None and warn_age > 0.0 and age > warn_age)
    read_within_budget = bool(read_ms <= max_read_ms)
    safety = _safety_findings(payload, stage_policy)
    violations: list[str] = []
    warnings: list[str] = []
    if required and not exists:
        violations.append("required_artifact_missing")
    if required and exists and not fresh:
        violations.append("required_artifact_stale")
    if exists and not read_within_budget:
        violations.append("artifact_read_over_budget")
    if warn_stale and fresh:
        warnings.append("artifact_refresh_recommended")
    recommended_age = stage_policy.get("recommended_operational_max_age_seconds")
    if exists and age is not None and recommended_age is not None:
        try:
            if age > float(recommended_age):
                warnings.append("operational_cache_refresh_recommended")
        except (TypeError, ValueError):
            pass
    violations.extend(safety.get("violations") or [])
    return {
        "stage_key": stage_key,
        "role": stage_policy.get("role"),
        "execution_mode": stage_policy.get("execution_mode"),
        "source_owner": stage_policy.get("source_owner"),
        "required": required,
        "hard_gate": bool(stage_policy.get("hard_gate", False)),
        "primary_path": str(primary_path) if primary_path else None,
        "fallback_path": str(fallback_path) if fallback_path else None,
        "selected_path": str(selected_path) if selected_path else None,
        "exists": exists,
        "fresh": fresh,
        "age_seconds": round(age, 3) if age is not None else None,
        "max_artifact_age_seconds": max_age,
        "warn_artifact_age_seconds": warn_age,
        "read_ms": round(read_ms, 3),
        "max_read_ms": max_read_ms,
        "read_within_budget": read_within_budget,
        "status_summary": _extract_stage_status(stage_key, payload),
        "generated_at": payload.get("generated_at"),
        "safety": safety,
        "warnings": sorted(set(warnings)),
        "violations": sorted(set(violations)),
    }


def build_execution_hot_path_latency_audit(
    *,
    profile_payload: dict[str, Any],
    profile_base: Path | None = None,
    loop_summary_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    budget = profile_payload.get("hot_path_latency_budget") if isinstance(profile_payload.get("hot_path_latency_budget"), dict) else {}
    stages_policy = budget.get("stages") if isinstance(budget.get("stages"), dict) else {}
    loop_summary_payload = loop_summary_payload or {}
    stage_reports = [
        _stage_report(stage_key=stage_key, stage_policy=stage, profile_base=profile_base)
        for stage_key, stage in stages_policy.items()
        if isinstance(stage, dict)
    ]
    loop_latency = _observed_loop_latency(loop_summary_payload)
    total_read_ms = round(sum(float(stage.get("read_ms") or 0.0) for stage in stage_reports), 3)
    max_total_read_ms = float(budget.get("max_total_artifact_read_ms", 999999.0) or 999999.0)
    max_dashboard_tick_seconds = float(budget.get("max_dashboard_tick_seconds", 999999.0) or 999999.0)
    max_release_inline_seconds = float(budget.get("max_release_governed_inline_seconds", 999999.0) or 999999.0)

    violations: list[str] = []
    warnings: list[str] = []
    for stage in stage_reports:
        for violation in stage.get("violations") or []:
            violations.append(f"{stage.get('stage_key')}:{violation}")
        for warning in stage.get("warnings") or []:
            warnings.append(f"{stage.get('stage_key')}:{warning}")
    if total_read_ms > max_total_read_ms:
        violations.append("total_artifact_read_over_budget")
    max_tick = loop_latency.get("max_tick_elapsed_seconds")
    if max_tick is not None and float(max_tick) > max_dashboard_tick_seconds:
        violations.append("dashboard_tick_over_hot_path_budget")
    release_max = loop_latency.get("release_governed_inline_duration_seconds_max")
    if release_max is not None and float(release_max) > max_release_inline_seconds:
        violations.append("release_governed_inline_over_budget")
    if loop_summary_payload and not loop_latency.get("release_governed_pass_seen"):
        violations.append("release_governed_pass_not_seen_in_loop_summary")

    safety_invariants = {
        "ibkr_order_submitted": False,
        "transmit_enabled": False,
        "live_order_routing_enabled": False,
        "portfolio_mutated": False,
        "scorer_rows_mutated": False,
        "hot_path_audit_submits_orders": False,
        "what_if_api_allowed_only_with_what_if_true_and_transmit_false": True,
        "stale_required_artifacts_fail_closed": True,
    }
    recommendations = []
    if any("operational_cache_refresh_recommended" in (stage.get("warnings") or []) for stage in stage_reports):
        recommendations.append("Refresh broker what-if and fast gate caches before market-open/order windows; keep actual order-time path to fresh-cache checks.")
    if any("artifact_refresh_recommended" in (stage.get("warnings") or []) for stage in stage_reports):
        recommendations.append("Keep Source Online Supervisor and final-gate builders running continuously so hot-path artifacts stay within warning freshness.")
    if not violations:
        recommendations.append("Proceed to M211 safety-cache/pre-check supervisor using these budgets as cache freshness contracts.")

    return {
        "execution_hot_path_latency_audit_version": EXECUTION_HOT_PATH_LATENCY_AUDIT_VERSION,
        "generated_at": _iso_now(),
        "status": "pass" if not violations else "review",
        "violations": sorted(set(violations)),
        "warnings": sorted(set(warnings)),
        "budget": {
            "enabled": bool(budget.get("enabled", False)),
            "max_dashboard_tick_seconds": max_dashboard_tick_seconds,
            "max_release_governed_inline_seconds": max_release_inline_seconds,
            "max_total_artifact_read_ms": max_total_read_ms,
        },
        "latency_summary": {
            "total_artifact_read_ms": total_read_ms,
            "total_artifact_read_within_budget": total_read_ms <= max_total_read_ms,
            **loop_latency,
        },
        "stage_reports": stage_reports,
        "safety_invariants": safety_invariants,
        "recommendations": recommendations,
    }


def build_execution_hot_path_latency_audit_from_paths(
    *,
    profile_json_path: Path,
    loop_summary_json_path: Path | None = None,
) -> dict[str, Any]:
    profile_json_path = profile_json_path.expanduser().resolve()
    profile_payload = _read_json(profile_json_path)
    if loop_summary_json_path is None:
        default_inputs = profile_payload.get("default_inputs") if isinstance(profile_payload.get("default_inputs"), dict) else {}
        loop_summary_json_path = _resolve_path(default_inputs.get("loop_summary_json"), profile_json_path.parent)
    return build_execution_hot_path_latency_audit(
        profile_payload=profile_payload,
        profile_base=profile_json_path.parent,
        loop_summary_payload=_read_json(loop_summary_json_path.expanduser().resolve() if loop_summary_json_path else None),
    )


def execution_hot_path_latency_audit_markdown(report: dict[str, Any]) -> str:
    latency = report.get("latency_summary") or {}
    lines = [
        "# Execution Hot Path Latency Audit",
        "",
        f"- Version: `{report.get('execution_hot_path_latency_audit_version')}`",
        f"- Status: `{report.get('status')}`",
        f"- Violations: `{report.get('violations') or []}`",
        f"- Warnings: `{report.get('warnings') or []}`",
        f"- Total artifact read ms: `{latency.get('total_artifact_read_ms')}` / budget `{(report.get('budget') or {}).get('max_total_artifact_read_ms')}`",
        f"- Dashboard tick latest/max seconds: `{latency.get('latest_tick_elapsed_seconds')}` / `{latency.get('max_tick_elapsed_seconds')}`",
        f"- Release-governed inline latest/max seconds: `{latency.get('release_governed_inline_duration_seconds_latest')}` / `{latency.get('release_governed_inline_duration_seconds_max')}`",
        "",
        "## Stage reports",
        "",
        "| Stage | Mode | Required | Fresh | Age sec | Read ms | Status | Warnings | Violations |",
        "| --- | --- | --- | --- | ---: | ---: | --- | --- | --- |",
    ]
    for stage in report.get("stage_reports") or []:
        status = stage.get("status_summary") or {}
        lines.append(
            f"| `{stage.get('stage_key')}` | `{stage.get('execution_mode')}` | `{stage.get('required')}` | `{stage.get('fresh')}` | `{stage.get('age_seconds')}` | `{stage.get('read_ms')}` | `{status}` | `{stage.get('warnings') or []}` | `{stage.get('violations') or []}` |"
        )
    lines.extend(["", "## Safety invariants", ""])
    for key, value in (report.get("safety_invariants") or {}).items():
        lines.append(f"- `{key}`: `{value}`")
    lines.extend(["", "## Recommendations", ""])
    for item in report.get("recommendations") or []:
        lines.append(f"- {item}")
    return "\n".join(lines) + "\n"


def write_execution_hot_path_latency_audit_json(output_path: Path, report: dict[str, Any]) -> Path:
    resolved = output_path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return resolved


def write_execution_hot_path_latency_audit_markdown(output_path: Path, report: dict[str, Any]) -> Path:
    resolved = output_path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(execution_hot_path_latency_audit_markdown(report), encoding="utf-8")
    return resolved
