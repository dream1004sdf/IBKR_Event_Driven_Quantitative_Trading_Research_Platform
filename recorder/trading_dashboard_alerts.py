from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

TRADING_DASHBOARD_ALERT_VERSION = "trading-dashboard-alert-v1"
TRADING_DASHBOARD_TELEGRAM_VERSION = "trading-dashboard-telegram-alert-v1"
MARKET_DATA_ALERT_REARM_SECONDS = 120.0


def _read_json_file(path: Path | None) -> tuple[dict[str, Any] | None, str | None]:
    if path is None:
        return None, "path_not_configured"
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        return None, f"missing:{resolved}"
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except Exception as exc:
        return None, f"read_error:{resolved}:{exc}"
    return payload, None


def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _parse_dt(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _market_data_alert_rearm_seconds(condition_key: str) -> float:
    if condition_key in {
        "market_data_temperature_non_green",
        "market_data_delayed_instruments",
        "market_data_missing_prices",
        "market_data_gap_or_sudden_move_alerts",
    }:
        return MARKET_DATA_ALERT_REARM_SECONDS
    if condition_key.startswith("status_reason:market_data_"):
        return MARKET_DATA_ALERT_REARM_SECONDS
    return 0.0


def _evidence_auto_recovery_gap_is_currently_resolved(
    *,
    evidence_freshness: dict[str, Any] | None = None,
    evidence_auto_recovery: dict[str, Any] | None = None,
    source_ingest_health: dict[str, Any] | None = None,
    source_online_supervisor: dict[str, Any] | None = None,
) -> bool:
    auto = evidence_auto_recovery or {}
    if not auto.get("gap_remains_after_refresh"):
        return False
    if int(auto.get("consecutive_failures") or 0) > 0:
        return False

    freshness = evidence_freshness or {}
    freshness_status = str(freshness.get("overall_status") or "").lower()
    if freshness_status != "green":
        return False
    if freshness.get("stale_source_keys") or freshness.get("lagging_source_keys") or freshness.get("evidence_gap_warning"):
        return False

    source_health = source_ingest_health or {}
    if source_health:
        source_health_status = str(source_health.get("overall_status") or "").lower()
        if source_health_status and source_health_status != "green":
            return False
        if source_health.get("online_problem_source_keys") or source_health.get("degraded_source_keys"):
            return False

    supervisor = source_online_supervisor or {}
    if supervisor:
        supervisor_status = str(supervisor.get("final_status") or "").lower()
        supervisor_source_health = str(supervisor.get("final_source_ingest_health_status") or "").lower()
        if supervisor_status and supervisor_status not in {"green", "yellow"}:
            return False
        if supervisor_source_health and supervisor_source_health != "green":
            return False
        if supervisor.get("failed_actions") or supervisor.get("final_online_problem_source_keys"):
            return False

    return True

def _atomic_write_jsonl(path: Path, row: dict[str, Any]) -> Path:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    return path


def _rotate_jsonl_if_needed(path: Path, *, max_bytes: int | None, backups: int) -> dict[str, Any]:
    path = path.expanduser().resolve()
    if max_bytes is None or int(max_bytes) <= 0:
        return {"enabled": False, "rotated": False, "reason": "disabled"}
    max_bytes = int(max_bytes)
    backups = max(0, int(backups))
    if not path.exists():
        return {"enabled": True, "rotated": False, "max_bytes": max_bytes, "backups": backups, "current_bytes": 0}
    try:
        current_bytes = int(path.stat().st_size)
    except OSError as exc:
        return {"enabled": True, "rotated": False, "max_bytes": max_bytes, "backups": backups, "error": str(exc)}
    if current_bytes < max_bytes:
        return {"enabled": True, "rotated": False, "max_bytes": max_bytes, "backups": backups, "current_bytes": current_bytes}
    if backups <= 0:
        path.write_text("", encoding="utf-8")
        return {
            "enabled": True,
            "rotated": True,
            "mode": "truncate_no_backups",
            "max_bytes": max_bytes,
            "previous_bytes": current_bytes,
        }
    oldest = path.with_name(f"{path.name}.{backups}")
    if oldest.exists():
        try:
            oldest.unlink()
        except OSError:
            pass
    for index in range(backups - 1, 0, -1):
        src = path.with_name(f"{path.name}.{index}")
        dst = path.with_name(f"{path.name}.{index + 1}")
        if src.exists():
            try:
                src.replace(dst)
            except OSError:
                pass
    rotated_path = path.with_name(f"{path.name}.1")
    try:
        path.replace(rotated_path)
    except OSError as exc:
        return {
            "enabled": True,
            "rotated": False,
            "max_bytes": max_bytes,
            "backups": backups,
            "current_bytes": current_bytes,
            "error": str(exc),
        }
    return {
        "enabled": True,
        "rotated": True,
        "mode": "size_rotation",
        "max_bytes": max_bytes,
        "backups": backups,
        "previous_bytes": current_bytes,
        "rotated_path": str(rotated_path),
    }


def _read_dashboard_alert_state(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {
            "dashboard_alert_version": TRADING_DASHBOARD_ALERT_VERSION,
            "active_conditions": {},
        }
    payload, error = _read_json_file(path)
    if error or not isinstance(payload, dict):
        return {
            "dashboard_alert_version": TRADING_DASHBOARD_ALERT_VERSION,
            "active_conditions": {},
            "state_load_error": error,
        }
    payload.setdefault("dashboard_alert_version", TRADING_DASHBOARD_ALERT_VERSION)
    payload.setdefault("active_conditions", {})
    return payload


def write_dashboard_alert_state(output_path: Path, state: dict[str, Any]) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    return output_path


def _severity_rank(value: Any) -> int:
    text = str(value or "").lower()
    return {
        "green": 0,
        "info": 0,
        "yellow": 1,
        "warn": 1,
        "warning": 1,
        "monitor": 1,
        "red": 2,
        "critical": 2,
        "block": 2,
        "reduce_risk": 2,
    }.get(text, 0)


def _alert_condition(
    *,
    key: str,
    severity: str,
    message: str,
    magnitude: float = 1.0,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "key": key,
        "severity": severity,
        "severity_rank": _severity_rank(severity),
        "magnitude": float(magnitude or 0.0),
        "message": message,
        "details": details or {},
    }


def _market_data_session_alert_context(snapshot: dict[str, Any]) -> dict[str, Any]:
    overlay = ((snapshot.get("market_data") or {}).get("market_data_session_overlay") or {})
    if not overlay:
        return {
            "available": False,
            "session_priority_class": "no_session_overlay",
        }
    summary = overlay.get("summary") or {}
    monitorability_counts = overlay.get("monitorability_counts") or summary.get("monitorability_counts") or {}
    monitorable_alert_instruments = overlay.get("monitorable_alert_instruments") or summary.get("monitorable_alert_instruments") or []
    regular_dependent_alert_instruments = overlay.get("regular_dependent_alert_instruments") or summary.get("regular_dependent_alert_instruments") or []
    monitorable_now_count = int(monitorability_counts.get("monitorable_now") or 0)
    regular_dependent_count = int(monitorability_counts.get("monitor_if_fresh_price_present") or 0)
    monitorable_alert_count = len(monitorable_alert_instruments)
    regular_dependent_alert_count = len(regular_dependent_alert_instruments)
    is_regular_session = bool(overlay.get("is_regular_session"))
    regular_dependent_only = (
        not is_regular_session
        and monitorable_alert_count == 0
        and (regular_dependent_alert_count > 0 or (monitorable_now_count == 0 and regular_dependent_count > 0))
    )
    session_priority_class = "monitorable_now_stress"
    if regular_dependent_only:
        session_priority_class = "regular_dependent_after_hours_context"
    elif not is_regular_session:
        session_priority_class = "overnight_monitoring_active"
    return {
        "available": True,
        "session_name": overlay.get("session_name"),
        "is_regular_session": is_regular_session,
        "risk_interpretation": overlay.get("risk_interpretation"),
        "stock_cash_overnight_monitoring_enabled": overlay.get("stock_cash_overnight_monitoring_enabled"),
        "option_quotes_regular_session_dependent": overlay.get("option_quotes_regular_session_dependent"),
        "monitorability_counts": monitorability_counts,
        "monitorable_alert_instruments": monitorable_alert_instruments,
        "regular_dependent_alert_instruments": regular_dependent_alert_instruments,
        "monitorable_alert_count": monitorable_alert_count,
        "regular_dependent_alert_count": regular_dependent_alert_count,
        "session_priority_class": session_priority_class,
    }




def _append_status_account_risk_mark_alert_conditions(conditions: list[dict[str, Any]], snapshot: dict[str, Any], buying_power_threshold: float, available_funds_threshold: float) -> None:
    status = snapshot.get("status") or {}
    raw_status_color = str(status.get("raw_color") or status.get("color") or "green").lower()
    status_color = str(status.get("alert_color") or status.get("display_color") or raw_status_color).lower()
    status_rank = _severity_rank(status_color)
    if status_rank > 0:
        conditions.append(
            _alert_condition(
                key="dashboard_status_non_green",
                severity=status_color,
                magnitude=float(status_rank),
                message=f"Dashboard display status is {status_color}.",
                details={
                    "raw_status_color": raw_status_color,
                    "display_status_color": status.get("display_color"),
                    "reasons": status.get("reasons") or [],
                    "operational_readiness_overlay": status.get("operational_readiness_overlay") or {},
                },
            )
        )
    for reason in status.get("reasons") or []:
        if reason == "all_primary_dashboard_checks_green":
            continue
        conditions.append(
            _alert_condition(
                key=f"status_reason:{reason}",
                severity=status_color if status_rank > 0 else "info",
                magnitude=float(status_rank or 1),
                message=f"Dashboard status reason active: {reason}.",
                details={"status_color": status_color, "raw_status_color": raw_status_color},
            )
        )

    account = snapshot.get("account") or {}
    buying_power = _num(account.get("buying_power"))
    if buying_power is not None and buying_power < buying_power_threshold:
        conditions.append(
            _alert_condition(
                key="account_buying_power_below_threshold",
                severity="yellow",
                magnitude=buying_power_threshold - buying_power,
                message=f"Buying power is below threshold: {buying_power:.2f} < {buying_power_threshold:.2f}.",
                details={"buying_power": buying_power, "threshold": buying_power_threshold},
            )
        )
    available_funds = _num(account.get("available_funds"))
    if available_funds is not None and available_funds < available_funds_threshold:
        conditions.append(
            _alert_condition(
                key="account_available_funds_below_threshold",
                severity="yellow",
                magnitude=available_funds_threshold - available_funds,
                message=f"Available funds are below threshold: {available_funds:.2f} < {available_funds_threshold:.2f}.",
                details={"available_funds": available_funds, "threshold": available_funds_threshold},
            )
        )

    risk = snapshot.get("risk") or {}
    risk_state = str(risk.get("risk_state") or "").lower()
    if risk_state in {"monitor", "reduce_risk"}:
        conditions.append(
            _alert_condition(
                key="portfolio_risk_state_not_within_limits",
                severity="red" if risk_state == "reduce_risk" else "yellow",
                magnitude=2.0 if risk_state == "reduce_risk" else 1.0,
                message=f"Portfolio risk state is {risk_state}.",
                details={
                    "recommended_action": risk.get("recommended_action"),
                    "breach_count": risk.get("breach_count"),
                    "warning_count": risk.get("warning_count"),
                },
            )
        )

    mark_quality = ((snapshot.get("market_data") or {}).get("mark_refresh_quality") or {}).get("quality") or {}
    delayed = int(mark_quality.get("delayed_row_count") or 0)
    if delayed > 0:
        conditions.append(
            _alert_condition(
                key="mark_quality_delayed_rows",
                severity="red",
                magnitude=float(delayed),
                message=f"Mark quality has delayed rows: {delayed}.",
                details={"delayed_row_count": delayed},
            )
        )
    missing = int(mark_quality.get("missing_mark_count") or 0)
    if missing > 0:
        conditions.append(
            _alert_condition(
                key="mark_quality_missing_marks",
                severity="red",
                magnitude=float(missing),
                message=f"Mark quality has missing marks: {missing}.",
                details={"missing_mark_count": missing},
            )
        )
    action_counts = mark_quality.get("quality_action_counts") or {}
    for action_key, severity in {"block_new_entries": "red", "size_reduce": "yellow"}.items():
        count = int(action_counts.get(action_key) or 0)
        if count > 0:
            conditions.append(
                _alert_condition(
                    key=f"mark_quality_action:{action_key}",
                    severity=severity,
                    magnitude=float(count),
                    message=f"Mark quality action active: {action_key} count {count}.",
                    details={"quality_action_counts": action_counts},
                )
            )



def _append_market_data_alert_conditions(conditions: list[dict[str, Any]], snapshot: dict[str, Any]) -> None:
    market_data = ((snapshot.get("market_data") or {}).get("market_data_desk") or {})
    market_data_risk_gate = ((snapshot.get("market_data") or {}).get("market_data_risk_gate") or {})
    market_data_operating_auto_gate = ((snapshot.get("market_data") or {}).get("market_data_operating_auto_gate") or {})
    market_data_operating_plan = ((snapshot.get("market_data") or {}).get("market_data_operating_execution_intent_ledger") or {})
    operating_defensive_plan = ((snapshot.get("market_data") or {}).get("operating_defensive_action_plan") or {})
    operating_defensive_ledger = ((snapshot.get("market_data") or {}).get("operating_defensive_intent_ledger") or {})
    operating_defensive_mode = market_data_operating_auto_gate.get("defensive_mode") or {}
    operating_protective_packet = market_data_operating_auto_gate.get("protective_analysis_packet") or {}
    market_summary = market_data.get("summary") or {}
    market_session_context = _market_data_session_alert_context(snapshot)
    session_regular_dependent_only = market_session_context.get("session_priority_class") == "regular_dependent_after_hours_context"
    market_status = str(market_data.get("status") or market_summary.get("status") or "").lower()
    market_temperature_score = _num((market_data.get("temperature") or {}).get("score"))
    if market_temperature_score is None:
        market_temperature_score = _num(market_summary.get("temperature_score"))
    if market_status in {"yellow", "red"}:
        temperature_severity = market_status
        temperature_magnitude = float(market_temperature_score or _severity_rank(market_status))
        temperature_message = f"Market Data Desk temperature is {market_status}."
        temperature_adjustment = market_data.get("temperature_session_adjustment") or {}
        if temperature_adjustment.get("false_positive_risk"):
            temperature_severity = "yellow"
            temperature_magnitude = min(temperature_magnitude, 2.0)
            temperature_message = (
                f"Market Data Desk temperature is {market_status}, but session-aware adjustment treats it as "
                f"{temperature_adjustment.get('adjusted_interpretation')}."
            )
        if session_regular_dependent_only:
            temperature_severity = "yellow" if market_status == "red" else "watch"
            temperature_magnitude = float(max(1, market_session_context.get("regular_dependent_alert_count") or 1))
            temperature_message = (
                f"Market Data Desk temperature is {market_status}, but the session overlay marks current stress as "
                "regular/reference dependent outside regular session."
            )
        conditions.append(
            _alert_condition(
                key="market_data_temperature_non_green",
                severity=temperature_severity,
                magnitude=temperature_magnitude,
                message=temperature_message,
                details={
                    "temperature": market_data.get("temperature") or {},
                    "temperature_diagnostics": market_data.get("temperature_diagnostics") or {},
                    "risk_flags": market_summary.get("risk_flags") or [],
                    "market_data_session": market_session_context,
                },
            )
        )
    market_delayed = int(market_summary.get("delayed_count") or 0)
    if market_delayed > 0:
        conditions.append(
            _alert_condition(
                key="market_data_delayed_instruments",
                severity="yellow",
                magnitude=float(market_delayed),
                message=f"Market Data Desk has delayed instruments: {market_delayed}.",
                details={"delayed_instruments": market_summary.get("delayed_instruments") or []},
            )
        )
    market_missing = int(market_summary.get("missing_price_count") or 0)
    if market_missing > 0:
        conditions.append(
            _alert_condition(
                key="market_data_missing_prices",
                severity="red" if market_missing >= 5 else "yellow",
                magnitude=float(market_missing),
                message=f"Market Data Desk has missing prices: {market_missing}.",
                details={"missing_instruments": market_summary.get("missing_instruments") or []},
            )
        )
    market_errors = int(market_summary.get("error_count") or 0)
    if market_errors > 0:
        conditions.append(
            _alert_condition(
                key="market_data_contract_or_subscription_errors",
                severity="red",
                magnitude=float(market_errors),
                message=f"Market Data Desk has contract/subscription errors: {market_errors}.",
                details={"error_instruments": market_summary.get("error_instruments") or []},
            )
        )
    market_alerts = int(market_summary.get("alert_count") or 0)
    if market_alerts > 0:
        alert_severity = "yellow"
        alert_magnitude = float(market_alerts)
        alert_message = f"Market Data Desk has gap/sudden-move alerts: {market_alerts}."
        if session_regular_dependent_only:
            alert_severity = "watch"
            alert_magnitude = float(max(1, market_session_context.get("regular_dependent_alert_count") or 1))
            alert_message = (
                f"Market Data Desk has {market_alerts} gap/sudden-move alerts, but session overlay marks them as "
                "regular/reference dependent outside regular session."
            )
        conditions.append(
            _alert_condition(
                key="market_data_gap_or_sudden_move_alerts",
                severity=alert_severity,
                magnitude=alert_magnitude,
                message=alert_message,
                details={
                    "alerted_instruments": market_summary.get("alerted_instruments") or [],
                    "top_moves": market_summary.get("top_moves") or [],
                    "market_data_session": market_session_context,
                },
            )
        )



def _append_evidence_source_alert_conditions(conditions: list[dict[str, Any]], snapshot: dict[str, Any]) -> None:
    evidence_freshness = snapshot.get("evidence_freshness") or {}
    evidence_status = str(evidence_freshness.get("effective_status") or evidence_freshness.get("overall_status") or "").lower()
    evidence_artifact_stale = bool(evidence_freshness.get("artifact_stale"))
    stale_source_keys = evidence_freshness.get("stale_source_keys") or []
    lagging_source_keys = evidence_freshness.get("lagging_source_keys") or []
    if evidence_artifact_stale or evidence_status in {"yellow", "red"} or stale_source_keys:
        severity = "red" if evidence_status == "red" or evidence_artifact_stale else "yellow"
        conditions.append(
            _alert_condition(
                key="evidence_freshness_artifact_stale" if evidence_artifact_stale else "evidence_freshness_stale_sources",
                severity=severity,
                magnitude=float(len(stale_source_keys) or _severity_rank(severity)),
                message=(
                    "Evidence freshness artifact is stale; current source freshness cannot be trusted."
                    if evidence_artifact_stale
                    else f"Evidence freshness guard has stale/offline sources: {len(stale_source_keys)}."
                ),
                details={
                    "overall_status": evidence_freshness.get("overall_status"),
                    "effective_status": evidence_freshness.get("effective_status"),
                    "age_seconds": evidence_freshness.get("age_seconds"),
                    "artifact_stale_threshold_seconds": evidence_freshness.get("artifact_stale_threshold_seconds"),
                    "stale_source_keys": stale_source_keys,
                    "scorer_guard": evidence_freshness.get("scorer_guard") or {},
                },
            )
        )
    elif evidence_status == "watch" or lagging_source_keys:
        conditions.append(
            _alert_condition(
                key="evidence_freshness_lagging_sources",
                severity="yellow",
                magnitude=float(len(lagging_source_keys) or 1),
                message=f"Evidence freshness guard has lagging sources: {len(lagging_source_keys)}.",
                details={
                    "overall_status": evidence_freshness.get("overall_status"),
                    "lagging_source_keys": lagging_source_keys,
                    "scorer_guard": evidence_freshness.get("scorer_guard") or {},
                },
            )
        )

    evidence_auto_recovery = snapshot.get("evidence_auto_recovery") or {}
    auto_status = str(evidence_auto_recovery.get("status") or "").lower()
    auto_failures = int(evidence_auto_recovery.get("consecutive_failures") or 0)
    auto_stale_source_keys = evidence_auto_recovery.get("last_stale_source_keys") or []
    auto_lagging_source_keys = evidence_auto_recovery.get("last_lagging_source_keys") or []
    auto_gap_currently_resolved = bool((snapshot.get("status") or {}).get("evidence_auto_recovery_gap_currently_resolved")) or _evidence_auto_recovery_gap_is_currently_resolved(
        evidence_freshness=evidence_freshness,
        evidence_auto_recovery=evidence_auto_recovery,
        source_ingest_health=snapshot.get("source_ingest_health") or {},
        source_online_supervisor=snapshot.get("source_online_supervisor") or {},
    )
    if auto_status in {"refresh_failed", "timeout"} or auto_failures > 0:
        severity = "red" if auto_failures >= 3 else "yellow"
        conditions.append(
            _alert_condition(
                key="evidence_auto_recovery_failed",
                severity=severity,
                magnitude=float(max(1, auto_failures)),
                message=f"Evidence auto-recovery watchdog needs attention: {auto_status or 'failure'} failures={auto_failures}.",
                details={
                    "status": evidence_auto_recovery.get("status"),
                    "consecutive_failures": auto_failures,
                    "next_refresh_eligible_at": evidence_auto_recovery.get("next_refresh_eligible_at"),
                    "last_refresh_queue_status_counts": evidence_auto_recovery.get("last_refresh_queue_status_counts") or {},
                },
            )
        )
    if auto_status == "refresh_completed_gap_remains" and (auto_stale_source_keys or auto_lagging_source_keys) and not auto_gap_currently_resolved:
        severity = "red" if auto_stale_source_keys else "yellow"
        conditions.append(
            _alert_condition(
                key="evidence_auto_recovery_gap_remains_after_refresh",
                severity=severity,
                magnitude=float((len(auto_stale_source_keys) * 2) + len(auto_lagging_source_keys) or 1),
                message="Evidence refresh completed but source gap remains.",
                details={
                    "status": evidence_auto_recovery.get("status"),
                    "freshness_status": evidence_auto_recovery.get("last_freshness_status"),
                    "stale_source_keys": auto_stale_source_keys,
                    "lagging_source_keys": auto_lagging_source_keys,
                    "next_refresh_eligible_at": evidence_auto_recovery.get("next_refresh_eligible_at"),
                    "last_refresh_queue_status_counts": evidence_auto_recovery.get("last_refresh_queue_status_counts") or {},
                },
            )
        )
    if auto_status == "skipped_cooldown" and (auto_stale_source_keys or auto_lagging_source_keys or stale_source_keys or lagging_source_keys) and not auto_gap_currently_resolved:
        cooldown_stale_keys = auto_stale_source_keys or stale_source_keys
        cooldown_lagging_keys = auto_lagging_source_keys or lagging_source_keys
        conditions.append(
            _alert_condition(
                key="evidence_auto_recovery_gap_remains_after_refresh",
                severity="red" if cooldown_stale_keys else "yellow",
                magnitude=float((len(cooldown_stale_keys) * 2) + len(cooldown_lagging_keys) or 1),
                message="Evidence gap remains but watchdog is in cooldown/backoff.",
                details={
                    "status": evidence_auto_recovery.get("status"),
                    "freshness_status": evidence_auto_recovery.get("last_freshness_status"),
                    "next_refresh_eligible_at": evidence_auto_recovery.get("next_refresh_eligible_at"),
                    "stale_source_keys": cooldown_stale_keys,
                    "lagging_source_keys": cooldown_lagging_keys,
                },
            )
        )

    source_ingest_health = snapshot.get("source_ingest_health") or {}
    source_health_status = str(source_ingest_health.get("effective_status") or source_ingest_health.get("overall_status") or "").lower()
    source_health_artifact_stale = bool(source_ingest_health.get("artifact_stale"))
    online_problem_source_keys = source_ingest_health.get("online_problem_source_keys") or []
    degraded_source_keys = source_ingest_health.get("degraded_source_keys") or []
    if source_health_artifact_stale or source_health_status in {"red", "yellow", "watch"} or online_problem_source_keys:
        severity = "red" if source_health_status == "red" or source_health_artifact_stale else "yellow"
        conditions.append(
            _alert_condition(
                key="source_ingest_health_artifact_stale" if source_health_artifact_stale else "source_ingest_health_online_required_gap",
                severity=severity,
                magnitude=float((len(degraded_source_keys) * 2) + len(online_problem_source_keys) or _severity_rank(severity)),
                message=(
                    "Source ingest health artifact is stale; online-required source state cannot be trusted."
                    if source_health_artifact_stale
                    else f"Source ingest health has online-required gaps: {len(online_problem_source_keys)}."
                ),
                details={
                    "overall_status": source_ingest_health.get("overall_status"),
                    "effective_status": source_ingest_health.get("effective_status"),
                    "age_seconds": source_ingest_health.get("age_seconds"),
                    "artifact_stale_threshold_seconds": source_ingest_health.get("artifact_stale_threshold_seconds"),
                    "online_problem_source_keys": online_problem_source_keys,
                    "degraded_source_keys": degraded_source_keys,
                    "root_cause_counts": source_ingest_health.get("root_cause_counts") or {},
                },
            )
        )

    source_online_supervisor = snapshot.get("source_online_supervisor") or {}
    supervisor_status = str(source_online_supervisor.get("effective_status") or source_online_supervisor.get("final_status") or "").lower()
    supervisor_artifact_stale = bool(source_online_supervisor.get("artifact_stale"))
    supervisor_source_health_status = str(source_online_supervisor.get("final_source_ingest_health_status") or "").lower()
    supervisor_online_problem_source_keys = source_online_supervisor.get("final_online_problem_source_keys") or []
    supervisor_failed_actions = source_online_supervisor.get("failed_actions") or []
    supervisor_latest_tick = source_online_supervisor.get("latest_tick") or {}
    supervisor_actions = supervisor_latest_tick.get("actions") or []
    if supervisor_artifact_stale or supervisor_status == "red" or supervisor_failed_actions:
        conditions.append(
            _alert_condition(
                key="source_online_supervisor_artifact_stale" if supervisor_artifact_stale else "source_online_supervisor_failure",
                severity="red",
                magnitude=float(max(1, len(supervisor_failed_actions))),
                message=(
                    "Source Online Supervisor artifact is stale; realtime evidence collection is not confirmed online."
                    if supervisor_artifact_stale
                    else f"Source Online Supervisor has failed/blocked actions: {len(supervisor_failed_actions)}."
                ),
                details={
                    "final_status": source_online_supervisor.get("final_status"),
                    "effective_status": source_online_supervisor.get("effective_status"),
                    "age_seconds": source_online_supervisor.get("age_seconds"),
                    "artifact_stale_threshold_seconds": source_online_supervisor.get("artifact_stale_threshold_seconds"),
                    "failed_actions": [
                        {
                            "action_id": item.get("action_id"),
                            "source_key": item.get("source_key"),
                            "status": item.get("status"),
                            "description": item.get("description"),
                        }
                        for item in supervisor_failed_actions
                    ],
                },
            )
        )
    if supervisor_source_health_status in {"red", "yellow", "watch"} or supervisor_online_problem_source_keys:
        severity = "red" if supervisor_source_health_status == "red" else "yellow"
        conditions.append(
            _alert_condition(
                key="source_online_supervisor_online_gap",
                severity=severity,
                magnitude=float((2 if severity == "red" else 1) * max(1, len(supervisor_online_problem_source_keys))),
                message=f"Source Online Supervisor reports online-required source gaps: {len(supervisor_online_problem_source_keys)}.",
                details={
                    "final_source_ingest_health_status": source_online_supervisor.get("final_source_ingest_health_status"),
                    "final_online_problem_source_keys": supervisor_online_problem_source_keys,
                    "latest_root_cause_counts": supervisor_latest_tick.get("root_cause_counts") or {},
                    "latest_action_status_counts": supervisor_latest_tick.get("action_status_counts") or {},
                },
            )
        )
    action_by_id = {str(item.get("action_id") or ""): item for item in supervisor_actions}
    for action_id, condition_key, severity in [
        ("source_live_validation_ibkr", "source_online_supervisor_ibkr_validation_issue", "red"),
        ("market_news_ibkr_catchup", "source_online_supervisor_ibkr_benzinga_news_issue", "red"),
        ("external_market_sync_marketaux", "source_online_supervisor_marketaux_sync_issue", "yellow"),
        ("external_market_sync_alpha_vantage", "source_online_supervisor_alpha_vantage_sync_issue", "yellow"),
    ]:
        action = action_by_id.get(action_id)
        action_status = str((action or {}).get("status") or "").lower()
        if action_status in {"failed", "timeout", "skipped_live_ibkr_disabled"}:
            conditions.append(
                _alert_condition(
                    key=condition_key,
                    severity=severity,
                    magnitude=2.0 if severity == "red" else 1.0,
                    message=f"Source supervisor action {action_id} is {action_status}.",
                    details={
                        "action_id": action_id,
                        "status": action_status,
                        "description": (action or {}).get("description"),
                        "details": (action or {}).get("details") or {},
                    },
                )
            )



def _append_options_market_structure_alert_conditions(conditions: list[dict[str, Any]], snapshot: dict[str, Any]) -> None:
    options_supervisor = snapshot.get("options_market_structure_supervisor") or {}
    if options_supervisor:
        options_phase = str(options_supervisor.get("final_session_refresh_phase") or options_supervisor.get("final_session") or "").lower()
        options_quote_allowed = bool(options_supervisor.get("final_option_quote_allowed"))
        options_close_window = options_phase == "regular_close_window"
        options_open_warmup = options_phase == "regular_open_warmup"
        core_quality = str(options_supervisor.get("final_quote_quality_status") or "").lower()
        optional_quality = str(options_supervisor.get("final_optional_index_quote_quality_status") or "").lower()
        core_usable = bool(options_supervisor.get("final_quote_usable"))
        optional_usable = bool(options_supervisor.get("final_optional_index_quote_usable"))
        if options_quote_allowed and (core_quality in {"missing", "red"} or not core_usable):
            conditions.append(
                _alert_condition(
                    key="options_market_structure_core_quote_quality_gap",
                    severity="yellow" if options_open_warmup else "red",
                    magnitude=float(len((options_supervisor.get("config") or {}).get("symbols") or []) or 1),
                    message=f"Options Market Structure Supervisor core quote quality is {core_quality or 'unknown'} in {options_phase}.",
                    details={
                        "phase": options_phase,
                        "core_quality": core_quality,
                        "core_usable": core_usable,
                        "artifact_delta": options_supervisor.get("final_artifact_delta") or {},
                    },
                )
            )
        if options_quote_allowed and optional_quality in {"missing", "red"} and (options_supervisor.get("config") or {}).get("optional_index_symbols"):
            conditions.append(
                _alert_condition(
                    key="options_market_structure_optional_index_quality_gap",
                    severity="yellow" if core_usable else "red",
                    magnitude=float(len((options_supervisor.get("config") or {}).get("optional_index_symbols") or []) or 1),
                    message=f"Optional direct index option quote quality is {optional_quality or 'unknown'}; ETF proxy coverage should remain isolated.",
                    details={
                        "phase": options_phase,
                        "optional_quality": optional_quality,
                        "optional_usable": optional_usable,
                        "optional_artifact_delta": options_supervisor.get("final_optional_index_artifact_delta") or {},
                    },
                )
            )
        core_latency = str(options_supervisor.get("final_quote_data_latency_status") or "").lower()
        optional_latency = str(options_supervisor.get("final_optional_index_quote_data_latency_status") or "").lower()
        if options_quote_allowed and core_latency in {"delayed_only", "mixed_delayed"}:
            conditions.append(
                _alert_condition(
                    key="options_market_structure_core_quote_delayed_data",
                    severity="red" if core_latency == "delayed_only" or options_close_window else "yellow",
                    magnitude=float(options_supervisor.get("final_quote_delayed_contracts") or 1),
                    message=f"Core option quote snapshot is {core_latency}; gamma/pin context should be downweighted.",
                    details={
                        "phase": options_phase,
                        "delayed_symbols": options_supervisor.get("final_quote_delayed_symbols") or [],
                        "delayed_contracts": options_supervisor.get("final_quote_delayed_contracts"),
                    },
                )
            )
        if options_quote_allowed and optional_latency in {"delayed_only", "mixed_delayed"}:
            conditions.append(
                _alert_condition(
                    key="options_market_structure_optional_index_quote_delayed_data",
                    severity="yellow",
                    magnitude=float(options_supervisor.get("final_optional_index_quote_delayed_contracts") or 1),
                    message=f"Optional index option quote snapshot is {optional_latency}.",
                    details={
                        "phase": options_phase,
                        "delayed_symbols": options_supervisor.get("final_optional_index_quote_delayed_symbols") or [],
                    },
                )
            )
        for reason, count in (options_supervisor.get("final_failure_reason_counts") or {}).items():
            if int(count or 0) <= 0:
                continue
            severity = "red" if str(reason) in {"contract_unresolved", "market_data_permission", "timeout", "delayed_only"} else "yellow"
            conditions.append(
                _alert_condition(
                    key=f"options_market_structure_failure_reason:{reason}",
                    severity=severity,
                    magnitude=float(count),
                    message=f"Options Market Structure Supervisor failure reason active: {reason} count {count}.",
                    details={
                        "failure_reason": reason,
                        "failure_count": count,
                        "symbol_failure_notes": options_supervisor.get("symbol_failure_notes") or {},
                    },
                )
            )
        isolated_symbols = options_supervisor.get("isolated_symbols_by_action") or {}
        if isolated_symbols:
            core_isolated = [
                row for row in isolated_symbols.values()
                if str((row or {}).get("action_id") or "").startswith("fresh_option_quote_snapshot")
            ]
            conditions.append(
                _alert_condition(
                    key="options_market_structure_symbol_isolation_active",
                    severity="red" if core_isolated else "yellow",
                    magnitude=float(len(isolated_symbols)),
                    message=f"Options Market Structure Supervisor has isolated symbols: {len(isolated_symbols)}.",
                    details={"isolated_symbols_by_action": isolated_symbols},
                )
            )
        artifact_delta = options_supervisor.get("final_artifact_delta") or {}
        if options_quote_allowed and options_close_window and str(artifact_delta.get("outcome") or "").lower() in {"worsened", "mixed"}:
            conditions.append(
                _alert_condition(
                    key="options_market_structure_close_window_artifact_delta_worsened",
                    severity="red",
                    magnitude=float(artifact_delta.get("worsened_signal_count") or 1),
                    message="Close-window option market-structure artifact delta worsened or mixed.",
                    details={
                        "artifact_delta": artifact_delta,
                        "optional_artifact_delta": options_supervisor.get("final_optional_index_artifact_delta") or {},
                    },
                )
            )
        coverage_matrix = options_supervisor.get("index_option_coverage_matrix") or {}
        weak_families = [
            family for family in coverage_matrix.get("families") or []
            if str(family.get("coverage_status") or "") in {"proxy_only", "direct_failed_proxy_unavailable", "unavailable", "direct_ok_proxy_missing"}
        ]
        if options_quote_allowed and weak_families:
            red_families = [
                family for family in weak_families
                if str(family.get("confidence") or "") in {"red", "low"}
            ]
            conditions.append(
                _alert_condition(
                    key="options_market_structure_index_family_coverage_weak",
                    severity="red" if red_families else "yellow",
                    magnitude=float(len(weak_families)),
                    message=f"Index option family coverage is weak for {len(weak_families)} families.",
                    details={
                        "overall_status": coverage_matrix.get("overall_status"),
                        "weak_families": weak_families,
                        "resolver_policy": coverage_matrix.get("resolver_policy") or {},
                    },
                )
            )



def _append_promotion_heartbeat_runtime_alert_conditions(conditions: list[dict[str, Any]], snapshot: dict[str, Any]) -> None:
    promotion_gate = (snapshot.get("threshold_candidate_evaluator") or {}).get("promotion_gate") or {}
    for signal in promotion_gate.get("alert_signals") or []:
        if not isinstance(signal, dict) or not signal.get("enabled"):
            continue
        severity = str(signal.get("severity") or "yellow").lower()
        conditions.append(
            _alert_condition(
                key=str(signal.get("dedupe_key") or f"promotion_gate:{signal.get('candidate_key')}:alert"),
                severity=severity,
                magnitude=float(3 if severity == "red" else 2 if severity == "yellow" else 1),
                message=str(signal.get("message") or signal.get("title") or "Promotion Gate alert signal active."),
                details={
                    "candidate_key": signal.get("candidate_key"),
                    "operational_gate_status": signal.get("operational_gate_status"),
                    "alert_once_per_operational_status": signal.get("alert_once_per_operational_status"),
                    "realert_on_worsening": signal.get("realert_on_worsening"),
                    "promotion_gate_alert_signal": signal,
                },
            )
        )

    singleton_locks = snapshot.get("singleton_locks") or {}
    zombie_count = int(singleton_locks.get("zombie_output_stale_count") or 0)
    stale_missing_count = int(singleton_locks.get("stale_or_missing_count") or 0)
    if zombie_count > 0:
        conditions.append(
            _alert_condition(
                key="loop_heartbeat_zombie_output_stale",
                severity="critical",
                magnitude=float(zombie_count),
                message=f"Loop heartbeat detected stale output while process is online: {zombie_count}.",
                details={"singleton_locks": singleton_locks},
            )
        )
    elif stale_missing_count > 0:
        conditions.append(
            _alert_condition(
                key="loop_heartbeat_stale_or_missing_lock",
                severity="red",
                magnitude=float(stale_missing_count),
                message=f"Loop heartbeat has stale or missing locks: {stale_missing_count}.",
                details={"singleton_locks": singleton_locks},
            )
        )
    duplicate_count = int(singleton_locks.get("duplicate_block_count_recent") or 0)
    if duplicate_count > 0:
        conditions.append(
            _alert_condition(
                key="loop_heartbeat_duplicate_start_blocked",
                severity="info",
                magnitude=float(duplicate_count),
                message=f"Loop singleton blocked duplicate starts: {duplicate_count}.",
                details={"singleton_locks": singleton_locks},
            )
        )

    missing_sources = snapshot.get("missing_sources") or []
    if missing_sources:
        conditions.append(
            _alert_condition(
                key="dashboard_missing_sources",
                severity="yellow",
                magnitude=float(len(missing_sources)),
                message=f"Dashboard has missing sources: {len(missing_sources)}.",
                details={"missing_sources": missing_sources},
            )
        )
    runtime_guard = snapshot.get("runtime_version_guard") or {}
    if runtime_guard.get("restart_recommended"):
        missing = ((runtime_guard.get("options_market_structure_supervisor") or {}).get("missing_fields") or [])
        conditions.append(
            _alert_condition(
                key="dashboard_runtime_version_guard_restart_recommended",
                severity="yellow",
                magnitude=float(len(missing) or 1),
                message=f"Dashboard runtime guard recommends restart; missing current artifact fields: {len(missing)}.",
                details={"runtime_version_guard": runtime_guard},
            )
        )
def dashboard_alert_conditions(
    snapshot: dict[str, Any],
    *,
    buying_power_threshold: float = 100.0,
    available_funds_threshold: float = 100.0,
) -> list[dict[str, Any]]:
    conditions: list[dict[str, Any]] = []
    _append_status_account_risk_mark_alert_conditions(
        conditions,
        snapshot,
        buying_power_threshold,
        available_funds_threshold,
    )
    _append_market_data_alert_conditions(conditions, snapshot)
    _append_evidence_source_alert_conditions(conditions, snapshot)
    _append_options_market_structure_alert_conditions(conditions, snapshot)
    _append_promotion_heartbeat_runtime_alert_conditions(conditions, snapshot)
    return conditions


def evaluate_dashboard_alerts(
    snapshot: dict[str, Any],
    state: dict[str, Any] | None = None,
    *,
    now: str | None = None,
    buying_power_threshold: float = 100.0,
    available_funds_threshold: float = 100.0,
    worsening_min_delta: float = 1.0,
    worsening_relative: float = 0.10,
    emit_resolution_alerts: bool = False,
    run_id: str | None = None,
    tick_index: int | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    now = now or _iso_now()
    now_dt = _parse_dt(now)
    state = dict(state or {})
    active_conditions = dict(state.get("active_conditions") or {})
    current_conditions = {
        condition["key"]: condition
        for condition in dashboard_alert_conditions(
            snapshot,
            buying_power_threshold=buying_power_threshold,
            available_funds_threshold=available_funds_threshold,
        )
    }
    alerts: list[dict[str, Any]] = []
    for key, condition in current_conditions.items():
        previous = active_conditions.get(key) or {}
        previous_status = previous.get("status")
        previous_rank = int(previous.get("last_alerted_severity_rank") if previous.get("last_alerted_severity_rank") is not None else -1)
        previous_magnitude = _num(previous.get("last_alerted_magnitude"))
        current_rank = int(condition.get("severity_rank") or 0)
        current_magnitude = float(condition.get("magnitude") or 0.0)
        reason = None
        if not previous:
            reason = "new_condition"
        elif previous_status == "resolved":
            quiet_reappearance = False
            rearm_seconds = _market_data_alert_rearm_seconds(key)
            if rearm_seconds > 0 and now_dt is not None:
                resolved_dt = _parse_dt(previous.get("resolved_at"))
                if resolved_dt is not None:
                    seconds_since_resolved = (now_dt - resolved_dt).total_seconds()
                    quiet_reappearance = 0 <= seconds_since_resolved < rearm_seconds
            if quiet_reappearance:
                if current_rank > previous_rank:
                    reason = "worsened_severity"
                elif previous_magnitude is not None:
                    threshold = max(float(worsening_min_delta), abs(previous_magnitude) * float(worsening_relative))
                    if current_magnitude > previous_magnitude + threshold:
                        reason = "worsened_magnitude"
            else:
                reason = "new_after_resolution"
        elif current_rank > previous_rank:
            reason = "worsened_severity"
        elif previous_magnitude is not None:
            threshold = max(float(worsening_min_delta), abs(previous_magnitude) * float(worsening_relative))
            if current_magnitude > previous_magnitude + threshold:
                reason = "worsened_magnitude"
        if reason:
            alert = {
                "dashboard_alert_version": TRADING_DASHBOARD_ALERT_VERSION,
                "alert_event_id": f"{now}:{tick_index or 0}:{key}:{reason}",
                "event_type": reason,
                "condition_key": key,
                "severity": condition.get("severity"),
                "severity_rank": current_rank,
                "magnitude": current_magnitude,
                "message": condition.get("message"),
                "details": condition.get("details") or {},
                "run_id": run_id,
                "tick_index": tick_index,
                "created_at": now,
            }
            alerts.append(alert)
            previous["last_alerted_at"] = now
            previous["last_alerted_reason"] = reason
            previous["last_alerted_severity_rank"] = current_rank
            previous["last_alerted_magnitude"] = current_magnitude
            previous["last_alerted_message"] = condition.get("message")
        previous.update(
            {
                "status": "active",
                "condition_key": key,
                "current_severity": condition.get("severity"),
                "current_severity_rank": current_rank,
                "current_magnitude": current_magnitude,
                "current_message": condition.get("message"),
                "details": condition.get("details") or {},
                "last_seen_at": now,
                "resolved_at": None,
            }
        )
        previous["seen_count"] = int(previous.get("seen_count") or 0) + 1
        active_conditions[key] = previous

    for key, previous in list(active_conditions.items()):
        if key in current_conditions or previous.get("status") != "active":
            continue
        previous["status"] = "resolved"
        previous["resolved_at"] = now
        previous["last_seen_at"] = now
        active_conditions[key] = previous
        if emit_resolution_alerts:
            alerts.append(
                {
                    "dashboard_alert_version": TRADING_DASHBOARD_ALERT_VERSION,
                    "alert_event_id": f"{now}:{tick_index or 0}:{key}:resolved",
                    "event_type": "resolved_condition",
                    "condition_key": key,
                    "severity": "info",
                    "severity_rank": 0,
                    "magnitude": 0.0,
                    "message": f"Dashboard condition resolved: {key}.",
                    "details": {},
                    "run_id": run_id,
                    "tick_index": tick_index,
                    "created_at": now,
                }
            )

    state.update(
        {
            "dashboard_alert_version": TRADING_DASHBOARD_ALERT_VERSION,
            "updated_at": now,
            "active_conditions": active_conditions,
            "last_condition_keys": sorted(current_conditions),
            "last_alert_count": len(alerts),
            "last_alert_event_ids": [alert.get("alert_event_id") for alert in alerts],
        }
    )
    return state, alerts


def _telegram_alert_class(alert: dict[str, Any]) -> str:
    key = str(alert.get("condition_key") or "").lower()
    severity = str(alert.get("severity") or "").lower()
    if severity in {"critical", "red"} and any(token in key for token in ("heartbeat_zombie", "stale_or_missing_lock", "broker", "safety", "transmit")):
        return "critical"
    if severity in {"critical", "red"}:
        return "critical"
    if "duplicate_start_blocked" in key or "near_return" in key:
        return "info"
    if severity in {"info", "green"}:
        return "info"
    return "warning"


def _telegram_alert_text(
    alerts: list[dict[str, Any]],
    *,
    title: str = "Trading Command Center",
    status_color: str | None = None,
    run_id: str | None = None,
    tick_index: int | None = None,
) -> str:
    severity_order = {"red": 3, "critical": 3, "yellow": 2, "warning": 2, "info": 1}
    top = max((severity_order.get(str(alert.get("severity") or "").lower(), 1) for alert in alerts), default=1)
    icon = "🚨" if top >= 3 else "⚠️" if top == 2 else "ℹ️"
    alert_class_counts = {
        name: sum(1 for alert in alerts if _telegram_alert_class(alert) == name)
        for name in ("critical", "warning", "info")
    }
    lines = [
        f"{icon} {title} alert",
        f"Status: {status_color or '-'}",
        f"Run: {run_id or '-'} / tick {tick_index if tick_index is not None else '-'}",
        f"Alert count: {len(alerts)} classes={alert_class_counts}",
        "",
    ]
    for alert in alerts[:8]:
        lines.append(f"- [{_telegram_alert_class(alert)}/{alert.get('severity')}] {alert.get('condition_key')}: {alert.get('message')}")
        if alert.get("event_type"):
            lines.append(f"  event: {alert.get('event_type')}")
    if len(alerts) > 8:
        lines.append(f"- ... {len(alerts) - 8} more alerts omitted")
    return "\n".join(lines).strip()


def deliver_dashboard_alerts_to_telegram(
    alerts: list[dict[str, Any]],
    *,
    bot_token: str | None,
    chat_id: str | None,
    title: str = "Trading Command Center",
    status_color: str | None = None,
    run_id: str | None = None,
    tick_index: int | None = None,
    timeout_seconds: float = 10.0,
    dry_run: bool = False,
) -> dict[str, Any]:
    created_at = _iso_now()
    message = _telegram_alert_text(
        alerts,
        title=title,
        status_color=status_color,
        run_id=run_id,
        tick_index=tick_index,
    )
    result: dict[str, Any] = {
        "dashboard_telegram_version": TRADING_DASHBOARD_TELEGRAM_VERSION,
        "created_at": created_at,
        "run_id": run_id,
        "tick_index": tick_index,
        "alert_count": len(alerts),
        "condition_keys": [alert.get("condition_key") for alert in alerts],
        "alert_class_counts": {
            name: sum(1 for alert in alerts if _telegram_alert_class(alert) == name)
            for name in ("critical", "warning", "info")
        },
        "message_preview": message[:500],
        "dry_run": bool(dry_run),
        "sent": False,
        "status": "not_attempted",
    }
    if not alerts:
        result["status"] = "skipped_no_alerts"
        return result
    if not bot_token or not chat_id:
        result["status"] = "skipped_missing_credentials"
        result["missing"] = {
            "bot_token": not bool(bot_token),
            "chat_id": not bool(chat_id),
        }
        return result
    if dry_run:
        result["status"] = "dry_run_not_sent"
        return result
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = urllib.parse.urlencode(
        {
            "chat_id": chat_id,
            "text": message,
            "disable_web_page_preview": "true",
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=float(timeout_seconds)) as response:
            body = response.read(4096).decode("utf-8", errors="replace")
            result["status_code"] = getattr(response, "status", None)
            result["telegram_response_ok"] = '"ok":true' in body.replace(" ", "").lower()
            result["sent"] = bool(result["telegram_response_ok"])
            result["status"] = "sent" if result["sent"] else "telegram_response_not_ok"
    except urllib.error.HTTPError as exc:
        body = exc.read(1024).decode("utf-8", errors="replace") if hasattr(exc, "read") else ""
        result["status"] = "telegram_http_error"
        result["status_code"] = exc.code
        result["error"] = body[:500] or str(exc)
    except Exception as exc:
        result["status"] = "telegram_send_error"
        result["error"] = str(exc)
    return result
