from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


DASHBOARD_ACTION_QUEUE_VERSION = "dashboard-action-queue-v3"

_SEVERITY_RANK = {"green": 0, "info": 1, "watch": 1, "yellow": 2, "warning": 2, "red": 3, "critical": 4}


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _num(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _severity(value: Any, default: str = "yellow") -> str:
    text = str(value or default).lower()
    return text if text in _SEVERITY_RANK else default


def _priority(severity: str, magnitude: Any = None, domain_boost: int = 0) -> int:
    rank = _SEVERITY_RANK.get(_severity(severity), 1)
    mag = _num(magnitude) or 0.0
    return int((rank * 1000) + domain_boost + min(250, round(mag * 10)))


def _action(
    *,
    action_id: str,
    domain: str,
    source_key: str | None,
    severity: str,
    title: str,
    recommendation: str,
    reason: str,
    condition_keys: list[str] | None = None,
    magnitude: Any = None,
    domain_boost: int = 0,
    command_hint: str | None = None,
    auto_recovery_target: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    severity = _severity(severity)
    return {
        "action_id": action_id,
        "priority_score": _priority(severity, magnitude, domain_boost),
        "severity": severity,
        "domain": domain,
        "source_key": source_key,
        "title": title,
        "recommendation": recommendation,
        "reason": reason,
        "condition_keys": condition_keys or [],
        "magnitude": _num(magnitude),
        "command_hint": command_hint,
        "auto_recovery_target": auto_recovery_target,
        "can_auto_execute": False,
        "read_only": True,
        "broker_order_submitted": False,
        "transmit_enabled": False,
        "details": details or {},
    }


def _market_data_session_context(snapshot: dict[str, Any]) -> dict[str, Any]:
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
    is_regular_session = bool(overlay.get("is_regular_session"))
    regular_dependent_only = (
        not is_regular_session
        and len(monitorable_alert_instruments) == 0
        and (len(regular_dependent_alert_instruments) > 0 or int(monitorability_counts.get("monitorable_now") or 0) == 0)
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
        "session_priority_class": session_priority_class,
    }


def _condition_actions(snapshot: dict[str, Any], *, buying_power_threshold: float, available_funds_threshold: float) -> list[dict[str, Any]]:
    from recorder.trading_dashboard import dashboard_alert_conditions

    actions: list[dict[str, Any]] = []
    for condition in dashboard_alert_conditions(
        snapshot,
        buying_power_threshold=buying_power_threshold,
        available_funds_threshold=available_funds_threshold,
    ):
        key = str(condition.get("key") or "")
        severity = _severity(condition.get("severity"))
        magnitude = condition.get("magnitude")
        details = condition.get("details") or {}
        message = str(condition.get("message") or key)
        if key.startswith("market_data_"):
            session_context = details.get("market_data_session") or _market_data_session_context(snapshot)
            session_priority_class = str(session_context.get("session_priority_class") or "")
            action_severity = severity
            domain_boost = 80
            title = "Review Market Data Desk state"
            recommendation = "Refresh the Market Data Desk, then inspect delayed/missing/error rows and sudden-move causes first."
            if session_priority_class == "regular_dependent_after_hours_context":
                action_severity = "watch" if severity in {"watch", "yellow"} else "yellow"
                domain_boost = 20
                title = "Review session-dependent Market Data context"
                recommendation = (
                    "Keep overnight stock/ETF, futures, FX, commodity, and crypto monitoring active, but treat the current "
                    "Market Data stress as regular/reference dependent until the next regular session confirms it."
                )
                details = dict(details)
                details["session_adjusted_priority"] = True
            actions.append(
                _action(
                    action_id=f"review_{key}",
                    domain="market_data",
                    source_key="market_data",
                    severity=action_severity,
                    title=title,
                    recommendation=recommendation,
                    reason=message,
                    condition_keys=[key],
                    magnitude=magnitude,
                    domain_boost=domain_boost,
                    command_hint="market-data-desk / market-data-risk-gate refresh",
                    auto_recovery_target="market_data_risk_gate",
                    details=details,
                )
            )
        elif key.startswith("mark_quality_"):
            actions.append(
                _action(
                    action_id=f"review_{key}",
                    domain="mark_quality",
                    source_key="paper_marks",
                    severity=severity,
                    title="Review paper mark quality",
                    recommendation="Run mark refresh again and confirm missing/delayed/fallback rows have improved.",
                    reason=message,
                    condition_keys=[key],
                    magnitude=magnitude,
                    domain_boost=70,
                    command_hint="paper-cycle mark-refresh quality path",
                    auto_recovery_target="mark_refresh",
                    details=details,
                )
            )
        elif key.startswith("evidence_freshness_"):
            actions.append(
                _action(
                    action_id=f"refresh_{key}",
                    domain="evidence_freshness",
                    source_key="evidence_packet",
                    severity=severity,
                    title="Recover evidence freshness gap",
                    recommendation="Run the Evidence Refresh Watchdog or refresh queue first, then confirm stale/lagging sources are resolved.",
                    reason=message,
                    condition_keys=[key],
                    magnitude=magnitude,
                    domain_boost=90,
                    command_hint="evidence-refresh-watchdog / evidence-refresh-queue",
                    auto_recovery_target="evidence_refresh_watchdog",
                    details=details,
                )
            )
        elif key.startswith("evidence_auto_recovery_"):
            actions.append(
                _action(
                    action_id=f"inspect_{key}",
                    domain="evidence_auto_recovery",
                    source_key="evidence_packet",
                    severity=severity,
                    title="Inspect evidence auto-recovery result",
                    recommendation="Inspect watchdog cooldown/backoff and failed sources; recover the source supervisor first when needed.",
                    reason=message,
                    condition_keys=[key],
                    magnitude=magnitude,
                    domain_boost=85,
                    command_hint="source-online-supervisor then evidence-refresh-watchdog",
                    auto_recovery_target="source_online_supervisor",
                    details=details,
                )
            )
        elif key.startswith("source_ingest_health_"):
            actions.append(
                _action(
                    action_id=f"repair_{key}",
                    domain="source_ingest_health",
                    source_key="online_required_sources",
                    severity=severity,
                    title="Recover Data Source Doctor gap",
                    recommendation="Use the online-required source root cause to run the Source Online Supervisor first.",
                    reason=message,
                    condition_keys=[key],
                    magnitude=magnitude,
                    domain_boost=100,
                    command_hint="source-online-supervisor --execute-actions",
                    auto_recovery_target="source_online_supervisor",
                    details=details,
                )
            )
        elif key.startswith("source_online_supervisor_"):
            actions.append(
                _action(
                    action_id=f"repair_{key}",
                    domain="source_online_supervisor",
                    source_key="online_required_sources",
                    severity=severity,
                    title="Inspect Source Online Supervisor action",
                    recommendation="Inspect failed/timeout/cooldown actions first and check provider-specific backoff state.",
                    reason=message,
                    condition_keys=[key],
                    magnitude=magnitude,
                    domain_boost=110,
                    command_hint="source-online-supervisor",
                    auto_recovery_target="source_online_supervisor",
                    details=details,
                )
            )
        elif key.startswith("portfolio_risk") or key.startswith("account_") or "buying_power" in key or "available_funds" in key:
            actions.append(
                _action(
                    action_id=f"risk_{key}",
                    domain="portfolio_risk",
                    source_key="account_or_portfolio",
                    severity=severity,
                    title="Review account and portfolio risk",
                    recommendation="Before adding new risk, inspect cash, margin, exposure, and open intents.",
                    reason=message,
                    condition_keys=[key],
                    magnitude=magnitude,
                    domain_boost=120,
                    command_hint="account snapshot / portfolio risk state refresh",
                    auto_recovery_target="risk_review",
                    details=details,
                )
            )
        else:
            actions.append(
                _action(
                    action_id=f"review_{key or 'dashboard_condition'}",
                    domain="dashboard",
                    source_key=None,
                    severity=severity,
                    title="Review dashboard condition",
                    recommendation="Inspect the dashboard condition details and recover the related source first.",
                    reason=message,
                    condition_keys=[key] if key else [],
                    magnitude=magnitude,
                    domain_boost=10,
                    details=details,
                )
            )
    return actions


def _market_data_gate_actions(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    gate = ((snapshot.get("market_data") or {}).get("market_data_risk_gate") or {})
    if not gate:
        return []
    action = str(gate.get("action") or "").lower()
    status = str(gate.get("gate_status") or "").lower()
    if action in {"allow_normal", "normal"} and status in {"green", "ok", ""}:
        return []
    severity = "red" if action in {"block_new_entries", "defensive_or_hedge_only"} or status == "red" else "yellow"
    domain_boost = 130
    title = "Review Market Data Risk Gate sizing"
    recommendation = "Keep the new-entry sizing haircut or defensive bias until the Market Data Desk reason is resolved."
    if action == "block_new_entries":
        recommendation = "Stop new entries and recover delayed/missing/error sources first."
    elif action == "defensive_or_hedge_only":
        recommendation = "Keep only hedge or defined-risk candidates instead of directional new entries."
    session_context = _market_data_session_context(snapshot)
    if session_context.get("session_priority_class") == "regular_dependent_after_hours_context":
        severity = "yellow" if severity == "red" else "watch"
        domain_boost = 60
        recommendation = (
            "Keep the conservative gate in place, but treat this as session-dependent context until regular-session "
            "market data confirms whether the stress is still actionable."
        )
    return [
        _action(
            action_id=f"apply_market_data_risk_gate_{action or status or 'watch'}",
            domain="market_data_risk_gate",
            source_key="market_data",
            severity=severity,
            title=title,
            recommendation=recommendation,
            reason=str(gate.get("reason") or action or status),
            condition_keys=["market_data_risk_gate"],
            magnitude=gate.get("entry_size_multiplier"),
            domain_boost=domain_boost,
            command_hint="market-data-risk-gate then autonomous execution gate",
            auto_recovery_target="market_data_risk_gate",
            details={
                "gate_status": gate.get("gate_status"),
                "action": gate.get("action"),
                "entry_size_multiplier": gate.get("entry_size_multiplier"),
                "new_entries_allowed": gate.get("new_entries_allowed"),
                "option_strategy_bias": gate.get("option_strategy_bias"),
                "blocking_reasons": gate.get("blocking_reasons") or [],
                "haircut_reasons": gate.get("haircut_reasons") or [],
                "defensive_reasons": gate.get("defensive_reasons") or [],
                "market_data_session": session_context,
                "session_adjusted_priority": session_context.get("session_priority_class") == "regular_dependent_after_hours_context",
            },
        )
    ]


def _release_governor_actions(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    governor = ((snapshot.get("market_data") or {}).get("session_transition_release_governor") or {})
    if not governor:
        return []
    release_status = str(governor.get("release_status") or "").lower()
    release_allowed = bool(governor.get("ordinary_alpha_release_allowed"))
    if release_allowed and release_status in {"release_allowed", "release_allowed_with_haircut"}:
        if release_status != "release_allowed_with_haircut":
            return []
    severity = "red" if release_status == "blocked" else "yellow"
    actions: list[dict[str, Any]] = []
    failed_checks = [item for item in governor.get("checks") or [] if isinstance(item, dict) and not item.get("passed")]
    if not failed_checks and release_status == "blocked":
        failed_checks = [
            {
                "name": "session_transition_release_governor_blocked",
                "reason": governor.get("reason") or "session_transition_release_governor_blocks_alpha",
                "value": governor.get("release_status"),
                "limit": "release_allowed",
            }
        ]
    reason_map = {
        "transition_gate_is_release_candidate": {
            "title": "Rebuild or wait for release-candidate transition state",
            "recommendation": "Keep ordinary alpha closed until the session-transition closed loop produces a release_candidate final gate.",
            "target": "session_transition_closed_loop",
            "command": "market-data-session-transition-closed-loop then session-transition-release-governor",
        },
        "transition_gate_rebuild_not_required": {
            "title": "Run session-transition closed-loop rebuild",
            "recommendation": "Run the safe M199 closed loop so session, Market Data Risk Gate, and Operating Auto Gate artifacts are fresh before release.",
            "target": "session_transition_closed_loop",
            "command": "market-data-session-transition-closed-loop",
        },
        "transition_gate_alpha_allowed": {
            "title": "Keep alpha closed until transition gate allows release",
            "recommendation": "Inspect the final transition gate controls and keep ordinary alpha disabled until the transition gate explicitly allows release.",
            "target": "session_transition_closed_loop",
            "command": "market-data-session-transition-gate / market-data-session-transition-closed-loop",
        },
        "market_data_risk_gate_allows_alpha": {
            "title": "Recover Market Data Risk Gate before alpha release",
            "recommendation": "Refresh Market Data Desk and Market Data Risk Gate, then rerun the release governor after data quality or market stress improves.",
            "target": "market_data_risk_gate",
            "command": "market-data-desk then market-data-risk-gate",
        },
        "operating_auto_gate_has_ready_alpha": {
            "title": "Rebuild Operating Auto Gate before alpha release",
            "recommendation": "Rebuild the paper auto gate and Market Data Operating Auto Gate; release remains closed while no ordinary-alpha candidates are ready.",
            "target": "operating_auto_gate",
            "command": "replay-score-auto-execution-gate then market-data-operating-auto-gate",
        },
        "portfolio_risk_state_allows_new_entries": {
            "title": "Review portfolio risk before alpha release",
            "recommendation": "Refresh account and paper portfolio risk state; release remains closed while new entries are blocked or risk state is outside limits.",
            "target": "risk_review",
            "command": "account snapshot / portfolio risk state refresh",
        },
        "evidence_preflight_allows_entries": {
            "title": "Recover evidence preflight before alpha release",
            "recommendation": "Run evidence freshness recovery and rebuild Evidence Preflight before allowing ordinary paper alpha.",
            "target": "evidence_refresh_watchdog",
            "command": "evidence-refresh-watchdog then evidence-preflight",
        },
        "operating_final_preflight_has_no_blocks": {
            "title": "Clear operating final preflight blocks",
            "recommendation": "Inspect stale or non-operating ledgers and rebuild the final preflight path before release.",
            "target": "operating_final_preflight",
            "command": "operating final preflight / paper order router preflight",
        },
    }
    for check in failed_checks:
        check_name = str(check.get("name") or "unknown_check")
        meta = reason_map.get(
            check_name,
            {
                "title": "Inspect session-transition release governor block",
                "recommendation": "Inspect the release governor failed check and rerun the relevant upstream gate before allowing ordinary paper alpha.",
                "target": "release_governor_review",
                "command": "session-transition-release-governor",
            },
        )
        reason = str(check.get("reason") or governor.get("reason") or check_name)
        domain_boost = 140
        if meta["target"] == "market_data_risk_gate":
            domain_boost = 135
        elif meta["target"] == "operating_auto_gate":
            domain_boost = 130
        elif meta["target"] == "session_transition_closed_loop":
            domain_boost = 145
        actions.append(
            _action(
                action_id=f"resolve_session_transition_release_{check_name}",
                domain="session_transition_release_governor",
                source_key=str(meta["target"]),
                severity=severity,
                title=str(meta["title"]),
                recommendation=str(meta["recommendation"]),
                reason=reason,
                condition_keys=["session_transition_release_governor", f"release_check:{check_name}"],
                magnitude=governor.get("failed_check_count") or len(failed_checks),
                domain_boost=domain_boost,
                command_hint=str(meta["command"]),
                auto_recovery_target=str(meta["target"]),
                details={
                    "release_status": governor.get("release_status"),
                    "ordinary_alpha_release_allowed": governor.get("ordinary_alpha_release_allowed"),
                    "max_new_entry_size_multiplier": governor.get("max_new_entry_size_multiplier"),
                    "failed_check": check,
                    "transition_decision": governor.get("transition_decision"),
                    "market_data_risk_gate_status": governor.get("market_data_risk_gate_status"),
                    "operating_mode": governor.get("operating_mode"),
                    "portfolio_risk_state": governor.get("portfolio_risk_state"),
                    "evidence_gate_status": governor.get("evidence_gate_status"),
                },
            )
        )
    if release_status == "release_allowed_with_haircut":
        actions.append(
            _action(
                action_id="monitor_session_transition_release_haircut",
                domain="session_transition_release_governor",
                source_key="release_haircut",
                severity="yellow",
                title="Monitor session-transition release haircut",
                recommendation="Allow only the governed reduced size until all contributing gates return full-size release.",
                reason=governor.get("reason") or "release_allowed_with_haircut",
                condition_keys=["session_transition_release_governor", "release_haircut"],
                magnitude=governor.get("max_new_entry_size_multiplier"),
                domain_boost=90,
                command_hint="session-transition-release-governor",
                auto_recovery_target="release_governor_review",
                details={"release_governor": governor},
            )
        )
    return actions


def _paper_execution_path_actions(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    market_data = snapshot.get("market_data") or {}
    release_governor = market_data.get("session_transition_release_governor") or {}
    release_status = str(release_governor.get("release_status") or "").lower()
    release_allowed = bool(release_governor.get("ordinary_alpha_release_allowed"))
    ledgers = [
        ("primary", snapshot.get("buy_sell_plan") or {}),
        ("operating", market_data.get("market_data_operating_execution_intent_ledger") or {}),
    ]
    actions: list[dict[str, Any]] = []
    for label, ledger in ledgers:
        if not ledger:
            continue
        blocked_count = int(ledger.get("blocked_by_session_transition_release_governor_count") or 0)
        pending_count = int(ledger.get("pending_paper_execution_count") or 0)
        ledger_release = ledger.get("session_transition_release_governor") or {}
        if blocked_count > 0:
            actions.append(
                _action(
                    action_id=f"review_m204_release_governed_{label}_ledger_blocks",
                    domain="paper_execution_path",
                    source_key=f"{label}_execution_intent_ledger",
                    severity="red",
                    title="Review M204 release-governed paper intent blocks",
                    recommendation=(
                        "Keep ordinary alpha paper execution closed; rerun the session-transition closed loop and release governor "
                        "before rebuilding this paper intent ledger."
                    ),
                    reason=f"{label} ledger has {blocked_count} ordinary-alpha intents blocked by the session-transition release governor.",
                    condition_keys=[f"{label}_ledger:session_transition_release_governor_blocks", "m204_release_governed_execution_path"],
                    magnitude=blocked_count,
                    domain_boost=150,
                    command_hint="session-transition-release-governor then replay-score-execution-intent-ledger",
                    auto_recovery_target="session_transition_release_governor",
                    details={
                        "ledger_label": label,
                        "blocked_by_session_transition_release_governor_count": blocked_count,
                        "pending_paper_execution_count": pending_count,
                        "ledger_release_governor": ledger_release,
                        "dashboard_release_governor": release_governor,
                    },
                )
            )
        if release_status == "blocked" and pending_count > 0 and not ledger_release:
            actions.append(
                _action(
                    action_id=f"rebuild_m204_release_governed_{label}_ledger",
                    domain="paper_execution_path",
                    source_key=f"{label}_execution_intent_ledger",
                    severity="red",
                    title="Rebuild stale paper ledger with M204 release governor",
                    recommendation=(
                        "The dashboard release governor is blocked but this ledger still has pending ordinary paper execution without "
                        "a release-governor snapshot. Rebuild the ledger through the M204 release-governed path before any router consumes it."
                    ),
                    reason="pending_paper_execution_exists_without_m204_release_snapshot_while_release_blocked",
                    condition_keys=[f"{label}_ledger:stale_without_release_snapshot", "m204_release_governed_execution_path"],
                    magnitude=pending_count,
                    domain_boost=155,
                    command_hint="replay-score-execution-intent-ledger --session-transition-release-governor-json exports/m201_session_transition_release_governor.json",
                    auto_recovery_target="operating_auto_gate",
                    details={
                        "ledger_label": label,
                        "pending_paper_execution_count": pending_count,
                        "dashboard_release_status": release_status,
                        "dashboard_release_allowed": release_allowed,
                    },
                )
            )
    return actions


def _options_market_structure_supervisor_actions(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    supervisor = snapshot.get("options_market_structure_supervisor") or {}
    if not supervisor:
        return []
    actions: list[dict[str, Any]] = []
    final_status = str(supervisor.get("final_status") or "").lower()
    session = str(supervisor.get("final_session") or "").lower()
    phase = str(supervisor.get("final_session_refresh_phase") or session or "unknown")
    quote_allowed = bool(supervisor.get("final_option_quote_allowed"))
    core_quality = str(supervisor.get("final_quote_quality_status") or "").lower()
    optional_quality = str(supervisor.get("final_optional_index_quote_quality_status") or "").lower()
    core_usable = bool(supervisor.get("final_quote_usable"))
    optional_usable = bool(supervisor.get("final_optional_index_quote_usable"))
    action_counts = supervisor.get("final_action_status_counts") or {}
    failure_reason_counts = supervisor.get("final_failure_reason_counts") or {}
    artifact_delta = supervisor.get("final_artifact_delta") or {}
    optional_artifact_delta = supervisor.get("final_optional_index_artifact_delta") or {}
    symbol_status = supervisor.get("symbol_status") or {}
    symbol_failure_notes = supervisor.get("symbol_failure_notes") or {}
    isolated_symbols = supervisor.get("isolated_symbols_by_action") or {}
    core_latency = str(supervisor.get("final_quote_data_latency_status") or "").lower()
    optional_latency = str(supervisor.get("final_optional_index_quote_data_latency_status") or "").lower()
    coverage_matrix = supervisor.get("index_option_coverage_matrix") or {}
    optional_effective_quality = str(supervisor.get("final_optional_index_effective_quote_quality_status") or "").lower()
    optional_proxy_active = optional_effective_quality == "proxy_active_warning"
    core_bad = core_quality in {"missing", "red"} or not core_usable
    optional_bad = (optional_quality in {"missing", "red"} or (optional_quality and not optional_usable)) and not optional_proxy_active
    close_window = phase == "regular_close_window"
    open_warmup = phase == "regular_open_warmup"

    if quote_allowed and core_bad:
        severity = "red" if not open_warmup else "yellow"
        title = "Refresh core ETF option market-structure quotes"
        recommendation = (
            "Run the Options Market Structure Supervisor with execute-actions during regular option hours, then confirm core ETF quote quality improves."
        )
        if close_window:
            recommendation = (
                "Refresh core ETF option quotes now; close-window gamma/max-pain context uses a stricter freshness window."
            )
        actions.append(
            _action(
                action_id="refresh_core_option_market_structure_quotes",
                domain="options_market_structure_supervisor",
                source_key="core_etf_option_quotes",
                severity=severity,
                title=title,
                recommendation=recommendation,
                reason=f"core_quote_quality={core_quality or 'unknown'}, usable={core_usable}, phase={phase}",
                condition_keys=["options_market_structure_supervisor", "core_quote_quality"],
                magnitude=len((supervisor.get("config") or {}).get("symbols") or []),
                domain_boost=125 if severity == "red" else 95,
                command_hint="options-market-structure-supervisor --execute-actions",
                auto_recovery_target="options_market_structure_supervisor",
                details={
                    "phase": phase,
                    "final_status": final_status,
                    "core_quality": core_quality,
                    "core_usable": core_usable,
                    "action_counts": action_counts,
                    "artifact_delta": artifact_delta,
                    "symbol_status": symbol_status,
                    "data_latency_status": core_latency,
                },
            )
        )
    elif not quote_allowed and core_bad and action_counts.get("deferred_until_regular_session"):
        deferred_state = supervisor.get("option_quote_deferred_operating_state") or {}
        actions.append(
            _action(
                action_id="monitor_regular_session_deferred_option_quotes",
                domain="options_market_structure_supervisor",
                source_key="regular_session_deferred_context",
                severity="watch",
                title="Monitor normal regular-session option quote deferral",
                recommendation="Treat this as normal off-session waiting, not a live data failure; run the M220 one-command validation after regular listed-option hours open.",
                reason=f"option quotes deferred in {session}; deferred_state={(deferred_state.get('status') or 'normal_session_deferred')}.",
                condition_keys=["options_market_structure_supervisor", "option_quote_normal_deferred_until_regular_session"],
                magnitude=len((supervisor.get("config") or {}).get("symbols") or []),
                domain_boost=10,
                command_hint=(supervisor.get("m220_operational_runbook") or {}).get("one_command") or "options-market-structure-supervisor after regular option open",
                auto_recovery_target="options_market_structure_supervisor",
                details={
                    "phase": phase,
                    "action_counts": action_counts,
                    "core_quality": core_quality,
                    "deferred_operating_state": deferred_state,
                    "normal_off_session_wait": True,
                },
            )
        )

    if quote_allowed and optional_proxy_active and (supervisor.get("config") or {}).get("optional_index_symbols"):
        actions.append(
            _action(
                action_id="monitor_optional_index_proxy_active_warning",
                domain="options_market_structure_supervisor",
                source_key="optional_index_proxy_active_warning",
                severity="watch",
                title="Optional direct index quotes weak but proxy coverage is active",
                recommendation=(
                    "Keep ETF/proxy option structure available for context while scheduling direct index contract/permission repair as non-blocking maintenance."
                ),
                reason=supervisor.get("final_optional_index_effective_quote_quality_reason") or f"optional_effective_quality={optional_effective_quality}",
                condition_keys=["options_market_structure_supervisor", "optional_index_proxy_active_warning"],
                magnitude=len((supervisor.get("config") or {}).get("optional_index_symbols") or []),
                domain_boost=28,
                command_hint="options-market-structure-supervisor --execute-actions when direct index repair is needed",
                auto_recovery_target="options_market_structure_supervisor",
                details={
                    "phase": phase,
                    "optional_raw_quality": optional_quality,
                    "optional_effective_quality": optional_effective_quality,
                    "optional_usable": optional_usable,
                    "coverage_matrix_status": coverage_matrix.get("overall_status"),
                    "effective_family_usage": (supervisor.get("m220_family_issue_summary") or {}).get("effective_operating_status_counts") or {},
                    "scorer_use_strength": (supervisor.get("m220_family_issue_summary") or {}).get("scorer_use_strength_counts") or {},
                    "non_blocking": True,
                },
            )
        )
    elif quote_allowed and optional_bad and (supervisor.get("config") or {}).get("optional_index_symbols"):
        severity = "yellow" if not core_bad else "red"
        actions.append(
            _action(
                action_id="repair_optional_index_option_market_structure_quotes",
                domain="options_market_structure_supervisor",
                source_key="optional_index_option_quotes",
                severity=severity,
                title="Repair optional direct index option coverage",
                recommendation=(
                    "Check optional SPX/NDX/RUT/VIX contract mapping or market-data permissions. Keep ETF proxy coverage isolated while direct index coverage is repaired."
                ),
                reason=f"optional_index_quality={optional_quality or 'unknown'}, usable={optional_usable}, phase={phase}",
                condition_keys=["options_market_structure_supervisor", "optional_index_quote_quality"],
                magnitude=len((supervisor.get("config") or {}).get("optional_index_symbols") or []),
                domain_boost=85,
                command_hint="options-market-structure-supervisor --execute-actions with index contract diagnostics",
                auto_recovery_target="options_market_structure_supervisor",
                details={
                    "phase": phase,
                    "optional_quality": optional_quality,
                    "optional_usable": optional_usable,
                    "optional_artifact_delta": optional_artifact_delta,
                    "core_quality": core_quality,
                    "core_usable": core_usable,
                    "symbol_status": symbol_status,
                    "data_latency_status": optional_latency,
                },
            )
        )

    weak_families = [
        family for family in coverage_matrix.get("families") or []
        if str(family.get("coverage_status") or "") in {"proxy_only", "proxy_probe_only", "direct_failed_proxy_unavailable", "unavailable", "direct_ok_proxy_missing"}
    ]
    if quote_allowed and weak_families:
        red_families = [
            family for family in weak_families
            if str(family.get("confidence") or "") == "red"
            or str(family.get("coverage_status") or "") in {"unavailable", "direct_failed_proxy_unavailable"}
        ]
        actions.append(
            _action(
                action_id="review_index_option_family_coverage_matrix",
                domain="options_market_structure_supervisor",
                source_key="index_option_family_coverage",
                severity="red" if red_families else "yellow",
                title="Review direct/mini/proxy index option coverage",
                recommendation="Use the coverage matrix to repair direct index contract mapping or permissions while preserving ETF/futures proxy coverage when direct index coverage is unavailable.",
                reason=f"weak_index_option_families={len(weak_families)}",
                condition_keys=["options_market_structure_supervisor", "index_option_family_coverage"],
                magnitude=len(weak_families),
                domain_boost=105 if red_families else 70,
                command_hint="options-market-structure-supervisor --execute-actions with index family resolver diagnostics",
                auto_recovery_target="options_market_structure_supervisor",
                details={
                    "overall_status": coverage_matrix.get("overall_status"),
                    "weak_families": weak_families,
                    "resolver_policy": coverage_matrix.get("resolver_policy") or {},
                },
            )
        )
    for plan in supervisor.get("m220_family_repair_plan") or (coverage_matrix.get("family_repair_plan") or []):
        if not isinstance(plan, dict):
            continue
        mode = str(plan.get("operating_mode") or "")
        severity = str(plan.get("severity") or "yellow")
        if severity == "green" or mode == "direct_or_mini_usable":
            continue
        family_id = str(plan.get("family_id") or plan.get("label") or "unknown").lower().replace(" ", "_").replace("/", "_")
        selected = plan.get("selected_symbols") or []
        source_key = (
            "index_option_family_proxy_fallback"
            if plan.get("proxy_fallback_active")
            else "index_option_family_proxy_probe_backed"
            if plan.get("proxy_probe_backed")
            else "index_option_family_unavailable"
        )
        if plan.get("proxy_fallback_active"):
            title = f"Maintain proxy fallback and repair direct index coverage for {plan.get('label') or plan.get('family_id')}"
            recommendation = (
                "Use the selected proxy for live context while separately checking direct/mini index contract mapping, permissions, and quote budget."
            )
            domain_boost = 82
        elif plan.get("proxy_probe_backed"):
            title = f"Proxy contracts found but quote quality is thin for {plan.get('label') or plan.get('family_id')}"
            recommendation = (
                "Keep the probe-backed proxy visible for market-structure context, but avoid treating it as high-confidence live option pressure until bid/ask quotes improve."
            )
            domain_boost = 76
        elif mode == "coverage_unavailable":
            title = f"Repair unavailable option family coverage for {plan.get('label') or plan.get('family_id')}"
            recommendation = (
                "Do not use this family for option market-structure context until contract mapping, permissions, chain availability, or quote budget is repaired."
            )
            domain_boost = 118
        else:
            title = f"Review option family coverage for {plan.get('label') or plan.get('family_id')}"
            recommendation = "Inspect the family repair plan and fix the highest-severity symbol issue first."
            domain_boost = 70
        actions.append(
            _action(
                action_id=f"m220_repair_index_option_family_{family_id}",
                domain="options_market_structure_supervisor",
                source_key=source_key,
                severity="red" if severity == "red" else "yellow",
                title=title,
                recommendation=recommendation,
                reason=plan.get("operator_summary") or f"operating_mode={mode}",
                condition_keys=[
                    "options_market_structure_supervisor",
                    "m220_family_repair_plan",
                    f"family:{family_id}",
                ],
                magnitude=plan.get("issue_count") or 1,
                domain_boost=domain_boost,
                command_hint=(plan.get("next_action") or "options-market-structure-supervisor --execute-actions"),
                auto_recovery_target="options_market_structure_supervisor",
                details={
                    "family_repair_plan": plan,
                    "selected_symbols": selected,
                    "issue_category_counts": plan.get("issue_category_counts") or {},
                    "issues": plan.get("issues") or [],
                },
            )
        )

    if quote_allowed and core_latency in {"delayed_only", "mixed_delayed"}:
        actions.append(
            _action(
                action_id="check_option_quote_live_data_status",
                domain="options_market_structure_supervisor",
                source_key="core_option_quote_latency",
                severity="red" if core_latency == "delayed_only" or close_window else "yellow",
                title="Check option quote live/delayed data status",
                recommendation="Verify IBKR option market-data type and subscriptions before using option gamma/pin context for live decisions.",
                reason=f"core option quote latency={core_latency}",
                condition_keys=["options_market_structure_supervisor", "option_quote_latency"],
                magnitude=float(supervisor.get("final_quote_delayed_contracts") or 1),
                domain_boost=120 if close_window else 95,
                command_hint="ibkr-options-quote-snapshot with market-data-type=1 during regular option session",
                auto_recovery_target="options_market_structure_supervisor",
                details={
                    "core_latency": core_latency,
                    "delayed_symbols": supervisor.get("final_quote_delayed_symbols") or [],
                    "delayed_contracts": supervisor.get("final_quote_delayed_contracts"),
                },
            )
        )

    if quote_allowed and optional_latency in {"delayed_only", "mixed_delayed"}:
        actions.append(
            _action(
                action_id="check_optional_index_option_quote_live_data_status",
                domain="options_market_structure_supervisor",
                source_key="optional_index_option_quote_latency",
                severity="yellow",
                title="Check optional index option live/delayed data status",
                recommendation="Keep ETF proxy coverage isolated while verifying direct index option market-data permissions and data type.",
                reason=f"optional index option quote latency={optional_latency}",
                condition_keys=["options_market_structure_supervisor", "optional_index_option_quote_latency"],
                magnitude=float(supervisor.get("final_optional_index_quote_delayed_contracts") or 1),
                domain_boost=80,
                command_hint="options-market-structure-supervisor optional index quote diagnostics",
                auto_recovery_target="options_market_structure_supervisor",
                details={
                    "optional_latency": optional_latency,
                    "delayed_symbols": supervisor.get("final_optional_index_quote_delayed_symbols") or [],
                    "delayed_contracts": supervisor.get("final_optional_index_quote_delayed_contracts"),
                },
            )
        )

    if isolated_symbols:
        core_isolated = [
            row for key, row in isolated_symbols.items()
            if str((row or {}).get("action_id") or "").startswith("fresh_option_quote_snapshot")
        ]
        actions.append(
            _action(
                action_id="review_isolated_option_structure_symbols",
                domain="options_market_structure_supervisor",
                source_key="option_symbol_isolation",
                severity="red" if core_isolated else "yellow",
                title="Review isolated option market-structure symbols",
                recommendation="Inspect repeated failures, fix the symbol-specific cause, then allow the supervisor to auto-retry after isolation expires.",
                reason=f"isolated_symbols={len(isolated_symbols)}",
                condition_keys=["options_market_structure_supervisor", "symbol_isolation"],
                magnitude=len(isolated_symbols),
                domain_boost=110 if core_isolated else 75,
                command_hint="options-market-structure-supervisor symbol isolation review",
                auto_recovery_target="options_market_structure_supervisor",
                details={"isolated_symbols_by_action": isolated_symbols, "symbol_status": symbol_status},
            )
        )

    actions.extend(
        _option_supervisor_failure_reason_actions(
            failure_reason_counts=failure_reason_counts,
            symbol_failure_notes=symbol_failure_notes,
            symbol_status=symbol_status,
        )
    )

    if quote_allowed and close_window and str(artifact_delta.get("outcome") or "").lower() in {"worsened", "mixed"}:
        actions.append(
            _action(
                action_id="review_close_window_option_structure_artifact_delta",
                domain="options_market_structure_supervisor",
                source_key="option_structure_artifact_delta",
                severity="red",
                title="Review close-window option structure artifact delta",
                recommendation="Inspect whether the latest refresh worsened quote quality or increased problem symbols before using gamma/pin context aggressively.",
                reason=f"artifact_delta={artifact_delta.get('outcome')}",
                condition_keys=["options_market_structure_supervisor", "artifact_delta"],
                magnitude=(artifact_delta.get("worsened_signal_count") or 1),
                domain_boost=130,
                command_hint="options-market-structure-supervisor report review",
                auto_recovery_target="options_market_structure_supervisor",
                details={"artifact_delta": artifact_delta, "optional_artifact_delta": optional_artifact_delta},
            )
        )
    return actions


def _option_supervisor_failure_reason_actions(
    *,
    failure_reason_counts: dict[str, Any],
    symbol_failure_notes: dict[str, Any],
    symbol_status: dict[str, Any],
) -> list[dict[str, Any]]:
    reason_actions = {
        "contract_unresolved": (
            "check_ibkr_option_contract_mapping",
            "Check IBKR option contract mapping",
            "Inspect underlying secType/exchange/tradingClass candidates for failed option symbols before retrying.",
            "contract_mapping",
        ),
        "market_data_permission": (
            "check_ibkr_option_market_data_subscription",
            "Check IBKR option market-data permissions",
            "Verify IBKR market-data subscriptions/permissions for the failed option symbols, then retry the supervisor.",
            "market_data_permission",
        ),
        "no_option_chain": (
            "check_ibkr_option_chain_availability",
            "Check IBKR option-chain availability",
            "Confirm the underlying resolves and IBKR returns option-chain definitions before requesting quotes.",
            "option_chain",
        ),
        "no_bid_ask": (
            "wait_or_retry_option_bid_ask_liquidity",
            "Wait or retry option bid/ask liquidity",
            "Retry during liquid regular-session minutes; keep option-dependent paths blocked until bid/ask quotes exist.",
            "option_liquidity",
        ),
        "timeout": (
            "retry_options_market_structure_supervisor_timeout",
            "Retry timed-out option supervisor action",
            "Retry with a narrower symbol set or longer timeout; keep existing evidence downweighted until the retry succeeds.",
            "timeout",
        ),
    }
    actions: list[dict[str, Any]] = []
    for reason, count in failure_reason_counts.items():
        meta = reason_actions.get(str(reason))
        if not meta or int(count or 0) <= 0:
            continue
        action_id, title, recommendation, source_key = meta
        actions.append(
            _action(
                action_id=action_id,
                domain="options_market_structure_supervisor",
                source_key=source_key,
                severity="red" if reason in {"contract_unresolved", "market_data_permission", "timeout"} else "yellow",
                title=title,
                recommendation=recommendation,
                reason=f"{reason} count {count}",
                condition_keys=["options_market_structure_supervisor", f"failure_reason:{reason}"],
                magnitude=count,
                domain_boost=115,
                command_hint="options-market-structure-supervisor diagnostics",
                auto_recovery_target="options_market_structure_supervisor",
                details={
                    "failure_reason": reason,
                    "failure_count": count,
                    "symbol_failure_notes": symbol_failure_notes,
                    "symbol_status": symbol_status,
                },
            )
        )
    return actions


def _promotion_gate_actions(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    evaluator = snapshot.get("threshold_candidate_evaluator") or {}
    gate = evaluator.get("promotion_gate") or {}
    actions: list[dict[str, Any]] = []
    stale_policy = gate.get("stale_watchlist_policy") or {}
    stale_count = int(((gate.get("watchlist_aging") or {}).get("stale_watchlist_count")) or 0)
    option_refresh_count = int(stale_policy.get("option_refresh_required_count") or 0)
    recycle_count = int(stale_policy.get("recycle_recommended_count") or 0)
    if option_refresh_count > 0:
        actions.append(
            _action(
                action_id="promotion_gate_refresh_option_market_structure",
                domain="threshold_promotion_gate",
                source_key="promotion_gate_stale_watchlist",
                severity="yellow",
                title="Refresh option structure for stale Promotion Gate watchlist",
                recommendation=(
                    "Run the Options Market Structure Supervisor/M218-M219 refresh path; stale threshold candidates need medium/high option confidence before waitlist."
                ),
                reason=f"stale_watchlist={stale_count}, option_refresh_required={option_refresh_count}",
                condition_keys=["threshold_candidate_evaluator", "promotion_gate", "option_structure_low_confidence"],
                magnitude=option_refresh_count,
                domain_boost=120,
                command_hint="options-market-structure-supervisor --execute-actions",
                auto_recovery_target="options_market_structure_supervisor",
                details={
                    "stale_watchlist_policy": stale_policy,
                    "action_requests": gate.get("action_requests") or [],
                    "alert_signals": gate.get("alert_signals") or [],
                },
            )
        )
    if recycle_count > 0:
        actions.append(
            _action(
                action_id="promotion_gate_recycle_stale_candidates",
                domain="threshold_promotion_gate",
                source_key="promotion_gate_recycle_candidates",
                severity="red",
                title="Recycle stale Promotion Gate candidates",
                recommendation=(
                    "Temporarily exclude stale candidates from promotion consideration and prioritize fresh M225/M226 samples for those families."
                ),
                reason=f"recycle_recommended={recycle_count}",
                condition_keys=["threshold_candidate_evaluator", "promotion_gate", "stale_recycle"],
                magnitude=recycle_count,
                domain_boost=145,
                command_hint="live-learning-llm-notes and threshold-candidate-evaluator normal cycle",
                auto_recovery_target="threshold_candidate_evaluator",
                details={
                    "stale_watchlist_policy": stale_policy,
                    "scorer_evidence_packet_hooks": gate.get("scorer_evidence_packet_hooks") or [],
                },
            )
        )
    for request in gate.get("action_requests") or []:
        action_id = str(request.get("action_id") or "")
        if action_id.startswith("promotion_gate_repair_option_root:") or action_id == "promotion_gate_recycle_return_review":
            actions.append(
                _action(
                    action_id=action_id,
                    domain="threshold_promotion_gate",
                    source_key="promotion_gate_action_request",
                    severity=str(request.get("severity") or "yellow"),
                    title=str(request.get("title") or action_id),
                    recommendation=str(request.get("recommendation") or "Review Promotion Gate action request."),
                    reason=f"candidate={request.get('candidate_key') or '-'}",
                    condition_keys=["threshold_candidate_evaluator", "promotion_gate", action_id],
                    magnitude=1,
                    domain_boost=110,
                    command_hint=str(request.get("command_hint") or "threshold-candidate-evaluator normal cycle"),
                    auto_recovery_target=str(request.get("target") or "threshold_candidate_evaluator"),
                    details={"action_request": request},
                )
            )
    return actions


def _dedupe_actions(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for action in actions:
        action_id = str(action.get("action_id") or "")
        existing = merged.get(action_id)
        if existing is None:
            merged[action_id] = action
            continue
        existing["priority_score"] = max(int(existing.get("priority_score") or 0), int(action.get("priority_score") or 0))
        existing["condition_keys"] = sorted(set((existing.get("condition_keys") or []) + (action.get("condition_keys") or [])))
        existing_details = dict(existing.get("details") or {})
        existing_details.setdefault("merged_reasons", []).append(action.get("reason"))
        existing["details"] = existing_details
    return sorted(merged.values(), key=lambda item: (-int(item.get("priority_score") or 0), str(item.get("action_id") or "")))


def _runtime_guard_actions(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    guard = snapshot.get("runtime_version_guard") or {}
    if not guard.get("restart_recommended"):
        return []
    component = guard.get("options_market_structure_supervisor") or {}
    missing = component.get("missing_fields") or []
    return [
        _action(
            action_id="restart_dashboard_or_supervisor_for_runtime_guard",
            domain="runtime",
            source_key="runtime_version_guard",
            severity="yellow",
            title="Restart dashboard/supervisor loops to load current code fields",
            recommendation=(
                "Restart the options market-structure supervisor and dashboard loop when convenient; current artifacts are missing fields expected by the running dashboard code."
            ),
            reason=component.get("interpretation") or f"missing_fields={missing}",
            condition_keys=["runtime_version_guard", "artifact_schema_mismatch"],
            magnitude=len(missing) or 1,
            domain_boost=60,
            command_hint="restart options-market-structure-supervisor and trading-dashboard-loop",
            auto_recovery_target="runtime_version_guard",
            details={"runtime_version_guard": guard},
        )
    ]


def _is_promotion_repair_action(action: dict[str, Any]) -> bool:
    action_id = str(action.get("action_id") or "")
    return action_id.startswith("promotion_gate_repair_option_root:") or action_id in {
        "promotion_gate_recycle_return_review",
        "promotion_gate_refresh_option_market_structure",
    }


def _force_visible_promotion_repair_actions(actions: list[dict[str, Any]], max_actions: int) -> list[dict[str, Any]]:
    limit = max(0, int(max_actions))
    if limit <= 0:
        return []
    visible = list(actions[:limit])
    visible_ids = {str(item.get("action_id") or "") for item in visible}
    repair_actions = [item for item in actions if _is_promotion_repair_action(item)]
    for repair in repair_actions:
        repair_id = str(repair.get("action_id") or "")
        if repair_id in visible_ids:
            continue
        replacement_index = None
        for idx in range(len(visible) - 1, -1, -1):
            candidate = visible[idx]
            if _is_promotion_repair_action(candidate):
                continue
            if str(candidate.get("severity") or "").lower() in {"critical"}:
                continue
            replacement_index = idx
            break
        if replacement_index is None:
            continue
        visible[replacement_index] = repair
        visible_ids = {str(item.get("action_id") or "") for item in visible}
    return sorted(visible, key=lambda item: (-int(item.get("priority_score") or 0), str(item.get("action_id") or "")))


def build_dashboard_action_queue(
    snapshot: dict[str, Any],
    *,
    max_actions: int = 12,
    buying_power_threshold: float = 100.0,
    available_funds_threshold: float = 100.0,
    now: str | None = None,
) -> dict[str, Any]:
    actions = []
    actions.extend(_market_data_gate_actions(snapshot))
    actions.extend(_release_governor_actions(snapshot))
    actions.extend(_paper_execution_path_actions(snapshot))
    actions.extend(_options_market_structure_supervisor_actions(snapshot))
    actions.extend(_runtime_guard_actions(snapshot))
    actions.extend(_promotion_gate_actions(snapshot))
    actions.extend(
        _condition_actions(
            snapshot,
            buying_power_threshold=buying_power_threshold,
            available_funds_threshold=available_funds_threshold,
        )
    )
    actions = _dedupe_actions(actions)
    limited = _force_visible_promotion_repair_actions(actions, int(max_actions))
    promotion_repair_actions = [
        item
        for item in actions
        if _is_promotion_repair_action(item)
    ]
    visible_ids = {str(item.get("action_id") or "") for item in limited}
    hidden = [item for item in actions if str(item.get("action_id") or "") not in visible_ids]
    hidden_promotion_repair = [
        item for item in promotion_repair_actions if str(item.get("action_id") or "") not in visible_ids
    ]
    status = snapshot.get("status") or {}
    raw_status_color = status.get("raw_color") or status.get("color")
    display_status_color = status.get("display_color") or status.get("alert_color") or raw_status_color
    market_data_session_context = _market_data_session_context(snapshot)
    domain_counts = Counter(str(item.get("domain") or "unknown") for item in actions)
    severity_counts = Counter(str(item.get("severity") or "unknown") for item in actions)
    return {
        "dashboard_action_queue_version": DASHBOARD_ACTION_QUEUE_VERSION,
        "generated_at": now or _iso_now(),
        "source_dashboard_snapshot_version": snapshot.get("dashboard_snapshot_version"),
        "source_dashboard_generated_at": snapshot.get("generated_at"),
        "status_color": display_status_color,
        "raw_status_color": raw_status_color,
        "status_display_color": display_status_color,
        "status_alert_color": status.get("alert_color"),
        "status_reasons": status.get("reasons") or [],
        "total_action_count": len(actions),
        "visible_action_count": len(limited),
        "max_actions": int(max_actions),
        "highest_priority_score": int(limited[0].get("priority_score") or 0) if limited else 0,
        "highest_severity": limited[0].get("severity") if limited else "green",
        "domain_counts": dict(domain_counts),
        "severity_counts": dict(severity_counts),
        "hidden_action_summary": {
            "hidden_action_count": len(hidden),
            "promotion_repair_total_count": len(promotion_repair_actions),
            "promotion_repair_hidden_count": len(hidden_promotion_repair),
            "promotion_repair_hidden_action_ids": [item.get("action_id") for item in hidden_promotion_repair[:20]],
        },
        "market_data_session_context": market_data_session_context,
        "actions": limited,
        "safety": {
            "read_only_queue": True,
            "can_auto_execute": False,
            "broker_order_submitted": False,
            "transmit_enabled": False,
            "live_order_routing_enabled": False,
        },
    }


def dashboard_action_queue_summary(queue: dict[str, Any] | None) -> dict[str, Any]:
    data = queue or {}
    return {
        "version": data.get("dashboard_action_queue_version"),
        "generated_at": data.get("generated_at"),
        "status_color": data.get("status_color"),
        "raw_status_color": data.get("raw_status_color"),
        "status_display_color": data.get("status_display_color"),
        "status_alert_color": data.get("status_alert_color"),
        "total_action_count": data.get("total_action_count") or 0,
        "visible_action_count": data.get("visible_action_count") or 0,
        "highest_priority_score": data.get("highest_priority_score") or 0,
        "highest_severity": data.get("highest_severity") or "green",
        "domain_counts": data.get("domain_counts") or {},
        "severity_counts": data.get("severity_counts") or {},
        "hidden_action_summary": data.get("hidden_action_summary") or {},
        "market_data_session_context": data.get("market_data_session_context") or {},
        "top_actions": (data.get("actions") or [])[:5],
    }


def dashboard_action_queue_markdown(queue: dict[str, Any]) -> str:
    lines = [
        "# Dashboard Action Queue",
        "",
        f"- Version: `{queue.get('dashboard_action_queue_version')}`",
        f"- Generated at: `{queue.get('generated_at')}`",
        f"- Dashboard status: `{queue.get('status_color')}` reasons `{queue.get('status_reasons') or []}`",
        f"- Actions: `{queue.get('visible_action_count')}` visible / `{queue.get('total_action_count')}` total",
        f"- Highest severity: `{queue.get('highest_severity')}` priority `{queue.get('highest_priority_score')}`",
        f"- Hidden actions: `{(queue.get('hidden_action_summary') or {}).get('hidden_action_count') or 0}`; hidden promotion repair/recycle actions `{(queue.get('hidden_action_summary') or {}).get('promotion_repair_hidden_count') or 0}`",
        "- Safety: read-only queue; no broker order submission or transmit.",
        "",
        "| Priority | Severity | Domain | Action | Recommendation |",
        "| ---: | --- | --- | --- | --- |",
    ]
    for item in queue.get("actions") or []:
        lines.append(
            "| {priority} | {severity} | {domain} | `{action_id}` | {recommendation} |".format(
                priority=item.get("priority_score"),
                severity=item.get("severity"),
                domain=item.get("domain"),
                action_id=item.get("action_id"),
                recommendation=str(item.get("recommendation") or "").replace("|", "/"),
            )
        )
    if not queue.get("actions"):
        lines.append("| - | green | - | `none` | No priority action is required. |")
    return "\n".join(lines) + "\n"


def write_dashboard_action_queue_json(output_path: Path, queue: dict[str, Any]) -> Path:
    resolved = output_path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(json.dumps(queue, indent=2, ensure_ascii=False), encoding="utf-8")
    return resolved


def write_dashboard_action_queue_markdown(output_path: Path, queue: dict[str, Any]) -> Path:
    resolved = output_path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(dashboard_action_queue_markdown(queue), encoding="utf-8")
    return resolved
