from __future__ import annotations

import json
import math
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from recorder.execution_readiness_safety import collect_safety_invariant_violations, paper_read_only_safety_block
from recorder.gate_severity_policy import GATE_SEVERITY_POLICY_VERSION, build_gate_severity_policy_report


LIMITED_AUTO_TRADING_READINESS_VERSION = "m250-limited-auto-trading-readiness-v5-gate-severity-policy"


DEFAULT_ARTIFACT_FRESHNESS_SLA_SECONDS = {
    "m237_capital_margin_state": 600.0,
    "m244_capital_pipeline_orchestrator": 900.0,
    "m241_strategy_allocation_engine": 900.0,
    "m243_live_read_only_capital_validation": 900.0,
    "m247_micro_option_candidates": 900.0,
    "m248_paper_option_ticket_preview": 900.0,
    "m249_option_whatif_safety_validation": 900.0,
    "m153_market_data_desk": 120.0,
    "m175_market_data_risk_gate": 180.0,
    "m219_options_market_structure_supervisor": 900.0,
    "m223_live_scorer_learning": 3600.0,
    "m226_threshold_candidate_evaluator": 3600.0,
    "m152_evidence_freshness": 900.0,
    "m173_source_online_supervisor": 900.0,
    "m211_execution_safety_cache_supervisor": 900.0,
}


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _result(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    result = payload.get("result")
    return result if isinstance(result, dict) else payload


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, dict):
        for key in ("numeric_value", "value", "amount"):
            parsed = _as_float(value.get(key))
            if parsed is not None:
                return parsed
        return None
    try:
        parsed = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed):
        return None
    return parsed


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _age_seconds(value: Any, *, now: datetime) -> float | None:
    parsed = _parse_dt(value)
    if parsed is None:
        return None
    return max(0.0, (now - parsed).total_seconds())


def _generated_at(payload: dict[str, Any]) -> str | None:
    data = _result(payload)
    for key in ("generated_at", "completed_at", "evaluated_at", "summary_generated_at", "started_at"):
        if data.get(key):
            return str(data.get(key))
    return None


def _artifact_row(
    artifact_id: str,
    payload: dict[str, Any] | None,
    *,
    now: datetime,
    max_age_seconds: float,
    required: bool = True,
    freshness_required: bool = True,
) -> dict[str, Any]:
    data = _result(payload)
    exists = bool(data)
    generated_at = _generated_at(data)
    age = _age_seconds(generated_at, now=now) if generated_at else None
    reasons: list[str] = []
    if not exists:
        reasons.append("artifact_missing")
    if exists and generated_at is None:
        reasons.append("artifact_generated_at_missing")
    if freshness_required and age is not None and age > max_age_seconds:
        reasons.append("artifact_stale")
    if required and not exists:
        status = "red"
    elif "artifact_generated_at_missing" in reasons:
        status = "yellow"
    elif "artifact_stale" in reasons:
        status = "yellow"
    else:
        status = "green"
    return {
        "artifact_id": artifact_id,
        "exists": exists,
        "generated_at": generated_at,
        "age_seconds": round(age, 3) if age is not None else None,
        "max_age_seconds": round(max_age_seconds, 3),
        "freshness_sla_seconds": round(max_age_seconds, 3),
        "freshness_required": bool(freshness_required),
        "status": status,
        "reasons": reasons or ["fresh_or_not_required"],
    }


def _summary(payload: dict[str, Any] | None) -> dict[str, Any]:
    data = _result(payload)
    summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
    return summary


def _capital_context(capital_state_payload: dict[str, Any] | None) -> dict[str, Any]:
    data = _result(capital_state_payload)
    budgets = data.get("budgets") if isinstance(data.get("budgets"), dict) else {}
    metrics = data.get("metrics") if isinstance(data.get("metrics"), dict) else {}
    withdrawal = data.get("withdrawal_guard") if isinstance(data.get("withdrawal_guard"), dict) else {}
    return {
        "status": data.get("status"),
        "status_reasons": data.get("status_reasons") or [],
        "leverage_mode": data.get("leverage_mode"),
        "selected_account": data.get("selected_account"),
        "snapshot_generated_at": data.get("snapshot_generated_at"),
        "snapshot_age_seconds": data.get("snapshot_age_seconds"),
        "net_liquidation": metrics.get("net_liquidation"),
        "cash": metrics.get("total_cash_value"),
        "available_funds": metrics.get("available_funds"),
        "budgets": {
            "deployable_cash_after_reserve": _as_float(budgets.get("deployable_cash_after_reserve")),
            "max_new_initial_margin": _as_float(budgets.get("max_new_initial_margin")),
            "core_risk_budget": _as_float(budgets.get("core_risk_budget")),
            "max_single_position_notional": _as_float(budgets.get("max_single_position_notional")),
            "options_premium_budget": _as_float(budgets.get("options_premium_budget")),
        },
        "withdrawal_guard": {
            "status": withdrawal.get("status"),
            "max_safe_withdrawal": withdrawal.get("max_safe_withdrawal"),
            "requires_fresh_account_snapshot": withdrawal.get("requires_fresh_account_snapshot"),
        },
    }


def _micro_policy_context(
    micro_option_candidates_payload: dict[str, Any] | None,
    strategy_allocation_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    m247 = _result(micro_option_candidates_payload)
    strategy = _result(strategy_allocation_payload)
    summary = _summary(micro_option_candidates_payload)
    contexts = m247.get("contexts") if isinstance(m247.get("contexts"), dict) else {}
    micro = contexts.get("micro_policy") if isinstance(contexts.get("micro_policy"), dict) else {}
    if not micro:
        strategy_contexts = strategy.get("contexts") if isinstance(strategy.get("contexts"), dict) else {}
        micro = strategy_contexts.get("micro_policy") if isinstance(strategy_contexts.get("micro_policy"), dict) else {}
    permissions = micro.get("permissions") if isinstance(micro.get("permissions"), dict) else {}
    return {
        "operating_mode": summary.get("micro_policy_operating_mode") or micro.get("operating_mode"),
        "allow_fractional_stock_long": permissions.get("allow_fractional_stock_long"),
        "allow_long_option": permissions.get("allow_long_option"),
        "allow_defined_risk_spread": permissions.get("allow_defined_risk_spread"),
        "allow_short_stock": permissions.get("allow_short_stock"),
        "allow_margin_heavy": permissions.get("allow_margin_heavy"),
        "default_blocked_reasons": micro.get("default_blocked_reasons") or [],
    }


def _market_context(
    market_data_payload: dict[str, Any] | None,
    market_data_risk_gate_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    market = _result(market_data_payload)
    gate = _result(market_data_risk_gate_payload)
    market_summary = market.get("summary") if isinstance(market.get("summary"), dict) else {}
    temperature = market.get("temperature") if isinstance(market.get("temperature"), dict) else {}
    session_context = gate.get("market_data_session_context") if isinstance(gate.get("market_data_session_context"), dict) else {}
    gate_market = gate.get("market_data") if isinstance(gate.get("market_data"), dict) else {}
    closed_session_expected_stale = (
        gate_market.get("closed_session_expected_stale")
        if isinstance(gate_market.get("closed_session_expected_stale"), dict)
        else {}
    )
    return {
        "market_data_status": market.get("status") or market_summary.get("status"),
        "temperature_label": temperature.get("label") or market_summary.get("temperature_label"),
        "temperature_score": temperature.get("score") if temperature.get("score") is not None else market_summary.get("temperature_score"),
        "risk_flags": temperature.get("risk_flags") or market_summary.get("risk_flags") or [],
        "alerted_instruments": market_summary.get("alerted_instruments") or [],
        "market_move_alert_count": market_summary.get("market_move_alert_count"),
        "scored_market_move_alert_count": market_summary.get("scored_market_move_alert_count"),
        "quality_excluded_market_move_alert_count": market_summary.get("quality_excluded_market_move_alert_count"),
        "missing_price_count": market_summary.get("missing_price_count"),
        "delayed_count": market_summary.get("delayed_count"),
        "error_count": market_summary.get("error_count"),
        "stale_count": market_summary.get("stale_count"),
        "close_only_count": market_summary.get("close_only_count"),
        "risk_gate_status": gate.get("gate_status") or gate.get("status"),
        "risk_gate_action": gate.get("action"),
        "new_entries_allowed": gate.get("new_entries_allowed"),
        "alpha_entries_allowed": gate.get("alpha_entries_allowed"),
        "hedge_entries_allowed": gate.get("hedge_entries_allowed"),
        "entry_size_multiplier": gate.get("entry_size_multiplier"),
        "session_context": session_context,
        "session_priority_class": session_context.get("session_priority_class"),
        "is_regular_session": session_context.get("is_regular_session"),
        "regular_dependent_alert_instruments": session_context.get("regular_dependent_alert_instruments") or [],
        "closed_session_expected_stale": closed_session_expected_stale,
        "closed_session_expected_stale_active": bool(closed_session_expected_stale.get("active")),
    }


def _market_data_session_deferred_normal(market: dict[str, Any]) -> bool:
    session_context = market.get("session_context") if isinstance(market.get("session_context"), dict) else {}
    if not session_context or session_context.get("available") is False:
        return False
    if session_context.get("is_regular_session") is True:
        return False
    missing = int(_as_float(market.get("missing_price_count")) or 0)
    errors = int(_as_float(market.get("error_count")) or 0)
    if missing > 0 or errors > 0:
        return False
    if market.get("closed_session_expected_stale_active"):
        return True
    stale = int(_as_float(market.get("stale_count")) or 0)
    close_only = int(_as_float(market.get("close_only_count")) or 0)
    risk_flags = {str(item) for item in market.get("risk_flags") or []}
    allowed_flags = {
        "stale_market_data_present",
        "close_only_quotes_present",
        "market_temperature_elevated",
        "market_data_temperature_normal",
        "quality_excluded_market_move_alerts",
    }
    alerted = {str(item).upper() for item in market.get("alerted_instruments") or [] if str(item or "").strip()}
    regular_dependent_alerted = {
        str(item).upper()
        for item in market.get("regular_dependent_alert_instruments") or []
        if str(item or "").strip()
    }
    if (
        "gap_or_sudden_move_alerts" in risk_flags
        and alerted
        and regular_dependent_alerted
        and alerted.issubset(regular_dependent_alerted)
    ):
        allowed_flags.add("gap_or_sudden_move_alerts")
    session_class = str(market.get("session_priority_class") or "")
    return bool(
        (stale > 0 or close_only > 0 or market.get("market_data_status") == "red")
        and (not risk_flags or risk_flags.issubset(allowed_flags))
        and session_class in {"overnight_monitoring_active", "regular_dependent_after_hours_context"}
    )


def _option_context(
    micro_option_candidates_payload: dict[str, Any] | None,
    paper_option_ticket_preview_payload: dict[str, Any] | None,
    option_whatif_safety_validation_payload: dict[str, Any] | None,
    options_market_structure_supervisor_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    m247 = _result(micro_option_candidates_payload)
    m248 = _result(paper_option_ticket_preview_payload)
    m249 = _result(option_whatif_safety_validation_payload)
    supervisor = _result(options_market_structure_supervisor_payload)
    s247 = _summary(micro_option_candidates_payload)
    s248 = _summary(paper_option_ticket_preview_payload)
    s249 = _summary(option_whatif_safety_validation_payload)
    return {
        "m247_status": m247.get("status"),
        "m247_expected_deferred": bool(m247.get("expected_deferred")),
        "candidate_count": s247.get("candidate_count"),
        "ready_candidate_count": s247.get("ready_count"),
        "m248_status": m248.get("status"),
        "m248_expected_deferred": bool(m248.get("expected_deferred")),
        "ticket_count": s248.get("ticket_count"),
        "ready_ticket_count": s248.get("ready_ticket_count"),
        "multi_leg_ticket_count": s248.get("multi_leg_ticket_count"),
        "m249_status": m249.get("status"),
        "m249_expected_deferred": bool(m249.get("expected_deferred")),
        "validation_item_count": s249.get("validation_item_count"),
        "whatif_passed_count": s249.get("passed_count"),
        "whatif_pending_count": s249.get("whatif_pending_count"),
        "whatif_blocked_count": s249.get("blocked_count"),
        "broker_whatif_required": s249.get("require_broker_whatif"),
        "broker_whatif_refresh_executed": s249.get("execute_whatif_refresh"),
        "quote_fresh": s249.get("quote_fresh"),
        "option_session_expected_deferred": bool(s249.get("option_session_expected_deferred")),
        "supervisor_status": supervisor.get("final_status"),
        "supervisor_session": supervisor.get("final_session"),
        "supervisor_quote_allowed": supervisor.get("final_option_quote_allowed"),
        "supervisor_quote_fresh": supervisor.get("final_quote_fresh"),
        "supervisor_quote_usable": supervisor.get("final_quote_usable"),
    }


def _learning_context(
    live_scorer_learning_payload: dict[str, Any] | None,
    threshold_candidate_evaluator_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    scorer = _result(live_scorer_learning_payload)
    evaluator = _result(threshold_candidate_evaluator_payload)
    promotion_gate = evaluator.get("promotion_gate") if isinstance(evaluator.get("promotion_gate"), dict) else {}
    shadow_trial = evaluator.get("shadow_promotion_trial") if isinstance(evaluator.get("shadow_promotion_trial"), dict) else {}
    promotion_ready_count = evaluator.get("promotion_ready_count")
    if promotion_ready_count is None:
        status_counts = evaluator.get("status_counts") if isinstance(evaluator.get("status_counts"), dict) else {}
        promotion_ready_count = status_counts.get("promotion_ready")
    return {
        "live_scorer_status": scorer.get("status") or scorer.get("latest_status"),
        "scorer_shadow_only": scorer.get("shadow_only"),
        "threshold_evaluator_shadow_only": evaluator.get("shadow_only"),
        "promotion_ready_count": promotion_ready_count,
        "promotion_gate_enabled": promotion_gate.get("enabled"),
        "promotion_gate_blocked_count": promotion_gate.get("blocked_count"),
        "promotion_gate_waitlist_count": promotion_gate.get("waitlist_count"),
        "promotion_gate_watchlist_count": promotion_gate.get("watchlist_count"),
        "promotion_gate_blocked_reason_counts": promotion_gate.get("blocked_reason_counts") or {},
        "promotion_gate_blocked_detail_reason_counts": promotion_gate.get("blocked_detail_reason_counts") or {},
        "promotion_gate_watchlist_detail_reason_counts": promotion_gate.get("watchlist_detail_reason_counts") or {},
        "shadow_trial_ready_for_real_promotion_count": shadow_trial.get("ready_for_real_promotion_count"),
        "candidate_hit_rate": scorer.get("candidate_hit_rate"),
        "candidate_avg_absolute_error": scorer.get("candidate_avg_absolute_error"),
        "packet_quality_counts": scorer.get("packet_quality_counts") or {},
    }


def _operations_context(
    evidence_freshness_payload: dict[str, Any] | None,
    source_online_supervisor_payload: dict[str, Any] | None,
    execution_safety_cache_supervisor_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    evidence = _result(evidence_freshness_payload)
    source = _result(source_online_supervisor_payload)
    safety = _result(execution_safety_cache_supervisor_payload)
    return {
        "evidence_status": evidence.get("effective_status") or evidence.get("status"),
        "evidence_artifact_stale": evidence.get("artifact_stale"),
        "source_supervisor_status": source.get("effective_status") or source.get("final_status") or source.get("status"),
        "source_supervisor_artifact_stale": source.get("artifact_stale"),
        "execution_safety_cache_status": safety.get("final_status") or safety.get("status"),
        "execution_safety_cache_ready": safety.get("final_cache_ready_for_all_dependent_order_paths"),
        "execution_safety_blocked_order_paths": safety.get("final_blocked_order_paths") or [],
    }


def _safety_violations(named_payloads: dict[str, dict[str, Any] | None]) -> list[dict[str, Any]]:
    return collect_safety_invariant_violations(named_payloads)


def _add_hard(hard_blocks: list[dict[str, Any]], code: str, message: str, *, source: str, value: Any = None) -> None:
    hard_blocks.append({"code": code, "source": source, "message": message, "value": value})


def _add_soft(soft_warnings: list[dict[str, Any]], code: str, message: str, *, source: str, value: Any = None) -> None:
    soft_warnings.append({"code": code, "source": source, "message": message, "value": value})


def _add_trade_blocker(trade_now_blockers: list[dict[str, Any]], code: str, message: str, *, source: str, value: Any = None) -> None:
    trade_now_blockers.append({"code": code, "source": source, "message": message, "value": value})


def _add_live_blocker(live_eligibility_blockers: list[dict[str, Any]], code: str, message: str, *, source: str, value: Any = None) -> None:
    live_eligibility_blockers.append({"code": code, "source": source, "message": message, "value": value})


def _next_steps(
    hard_blocks: list[dict[str, Any]],
    soft_warnings: list[dict[str, Any]],
    *,
    expected_deferred: bool,
    trade_now_blockers: list[dict[str, Any]] | None = None,
    live_eligibility_blockers: list[dict[str, Any]] | None = None,
) -> list[str]:
    codes = {row["code"] for row in hard_blocks}
    trade_codes = {row["code"] for row in trade_now_blockers or []}
    live_codes = {row["code"] for row in live_eligibility_blockers or []}
    steps: list[str] = []
    if expected_deferred:
        steps.append("Wait for the next regular option session, then rerun M247 -> M248 -> M249 -> M250.")
    if "capital_pipeline_not_green" in trade_codes:
        steps.append("Refresh M244/M239/M241/M243 alignment, but do not treat this as a broker-safety failure when M237 capital is fresh.")
    if "market_risk_gate_blocks_new_entries" in trade_codes:
        steps.append("Treat this as a trade-now restriction. Keep ordinary alpha closed, but keep readiness observation/paper planning alive.")
    if "m249_not_passed" in live_codes or "m249_missing_or_not_ready" in live_codes:
        steps.append("Rerun M249 after fresh M248 tickets exist; require broker what-if evidence before live-readiness promotion.")
    if "promotion_gate_not_clear" in live_codes:
        steps.append("Keep live eligibility closed, but allow paper/shadow operation while the Promotion Gate clears blocked/watchlist reasons.")
    if "safety_invariant_violation" in codes:
        steps.append("Stop progression and inspect safety invariant violation before any execution milestone.")
    if not steps and soft_warnings:
        steps.append("Review soft warnings, refresh stale artifacts, then keep observing in paper mode.")
    if not steps:
        steps.append("Continue paper execution and live-read-only validation; live routing remains locked until an explicit later milestone.")
    return steps


def build_limited_auto_trading_readiness_report(
    *,
    capital_state_payload: dict[str, Any] | None = None,
    capital_pipeline_orchestrator_payload: dict[str, Any] | None = None,
    strategy_allocation_payload: dict[str, Any] | None = None,
    live_read_only_capital_validation_payload: dict[str, Any] | None = None,
    micro_option_candidates_payload: dict[str, Any] | None = None,
    paper_option_ticket_preview_payload: dict[str, Any] | None = None,
    option_whatif_safety_validation_payload: dict[str, Any] | None = None,
    market_data_payload: dict[str, Any] | None = None,
    market_data_risk_gate_payload: dict[str, Any] | None = None,
    options_market_structure_supervisor_payload: dict[str, Any] | None = None,
    live_scorer_learning_payload: dict[str, Any] | None = None,
    threshold_candidate_evaluator_payload: dict[str, Any] | None = None,
    evidence_freshness_payload: dict[str, Any] | None = None,
    source_online_supervisor_payload: dict[str, Any] | None = None,
    execution_safety_cache_supervisor_payload: dict[str, Any] | None = None,
    max_artifact_age_minutes: float = 30.0,
    now: str | datetime | None = None,
) -> dict[str, Any]:
    now_dt = _parse_dt(now) if not isinstance(now, datetime) else now.astimezone(UTC)
    if now_dt is None:
        now_dt = datetime.now(UTC)
    fallback_max_age_seconds = max(0.0, float(max_artifact_age_minutes) * 60.0)
    artifact_specs = [
        ("m237_capital_margin_state", capital_state_payload, True),
        ("m244_capital_pipeline_orchestrator", capital_pipeline_orchestrator_payload, False),
        ("m241_strategy_allocation_engine", strategy_allocation_payload, False),
        ("m243_live_read_only_capital_validation", live_read_only_capital_validation_payload, False),
        ("m247_micro_option_candidates", micro_option_candidates_payload, False),
        ("m248_paper_option_ticket_preview", paper_option_ticket_preview_payload, False),
        ("m249_option_whatif_safety_validation", option_whatif_safety_validation_payload, True),
        ("m153_market_data_desk", market_data_payload, True),
        ("m175_market_data_risk_gate", market_data_risk_gate_payload, False),
        ("m219_options_market_structure_supervisor", options_market_structure_supervisor_payload, False),
        ("m223_live_scorer_learning", live_scorer_learning_payload, False),
        ("m226_threshold_candidate_evaluator", threshold_candidate_evaluator_payload, False),
        ("m152_evidence_freshness", evidence_freshness_payload, False),
        ("m173_source_online_supervisor", source_online_supervisor_payload, False),
        ("m211_execution_safety_cache_supervisor", execution_safety_cache_supervisor_payload, False),
    ]
    artifacts = [
        _artifact_row(
            artifact_id,
            payload,
            now=now_dt,
            max_age_seconds=float(DEFAULT_ARTIFACT_FRESHNESS_SLA_SECONDS.get(artifact_id, fallback_max_age_seconds)),
            required=required,
        )
        for artifact_id, payload, required in artifact_specs
    ]
    artifact_counts = Counter(row["status"] for row in artifacts)
    stale_artifacts = [row for row in artifacts if "artifact_stale" in row.get("reasons", [])]
    missing_required = [row for row in artifacts if row.get("status") == "red"]

    capital = _capital_context(capital_state_payload)
    micro_policy = _micro_policy_context(micro_option_candidates_payload, strategy_allocation_payload)
    market = _market_context(market_data_payload, market_data_risk_gate_payload)
    market_session_deferred_normal = _market_data_session_deferred_normal(market)
    options = _option_context(
        micro_option_candidates_payload,
        paper_option_ticket_preview_payload,
        option_whatif_safety_validation_payload,
        options_market_structure_supervisor_payload,
    )
    learning = _learning_context(live_scorer_learning_payload, threshold_candidate_evaluator_payload)
    operations = _operations_context(evidence_freshness_payload, source_online_supervisor_payload, execution_safety_cache_supervisor_payload)
    capital_pipeline = _result(capital_pipeline_orchestrator_payload)
    m249 = _result(option_whatif_safety_validation_payload)
    m249_summary = _summary(option_whatif_safety_validation_payload)
    live_validation = _result(live_read_only_capital_validation_payload)
    option_chain_expected_deferred = bool(
        options.get("option_session_expected_deferred")
        or options.get("m247_expected_deferred")
        or options.get("m248_expected_deferred")
        or options.get("m249_expected_deferred")
        or options.get("supervisor_quote_allowed") is False
        or (m249 and m249.get("status") == "expected_deferred")
    )
    if market_session_deferred_normal:
        option_chain_expected_deferred = option_chain_expected_deferred or bool(options.get("supervisor_quote_allowed") is False)

    hard_blocks: list[dict[str, Any]] = []
    soft_warnings: list[dict[str, Any]] = []
    trade_now_blockers: list[dict[str, Any]] = []
    live_eligibility_blockers: list[dict[str, Any]] = []
    expected_deferred_reasons: list[str] = []
    normal_deferred_artifacts: list[str] = []

    for row in missing_required:
        if row["artifact_id"] == "m237_capital_margin_state":
            _add_hard(hard_blocks, f"{row['artifact_id']}_missing", "Required account/capital artifact is missing.", source=row["artifact_id"])
        elif row["artifact_id"] == "m153_market_data_desk":
            _add_trade_blocker(trade_now_blockers, f"{row['artifact_id']}_missing", "Market data artifact is missing; do not open new trades now.", source=row["artifact_id"])
            _add_soft(soft_warnings, f"{row['artifact_id']}_missing", "Market data artifact is missing; readiness can remain observable but trade-now is blocked.", source=row["artifact_id"])
        elif row["artifact_id"] == "m249_option_whatif_safety_validation":
            _add_live_blocker(live_eligibility_blockers, "m249_missing_or_not_ready", "M249 option what-if validation is missing; option live eligibility is closed.", source=row["artifact_id"])
            _add_soft(soft_warnings, f"{row['artifact_id']}_missing", "M249 is missing; keep option execution readiness closed until it is rebuilt.", source=row["artifact_id"])
        else:
            _add_soft(soft_warnings, f"{row['artifact_id']}_missing", "Supporting readiness artifact is missing.", source=row["artifact_id"])
    for row in stale_artifacts:
        if market_session_deferred_normal and row["artifact_id"] in {"m153_market_data_desk", "m175_market_data_risk_gate"}:
            normal_deferred_artifacts.append(row["artifact_id"])
            continue
        if option_chain_expected_deferred and row["artifact_id"] in {
            "m247_micro_option_candidates",
            "m248_paper_option_ticket_preview",
            "m249_option_whatif_safety_validation",
            "m219_options_market_structure_supervisor",
        }:
            normal_deferred_artifacts.append(row["artifact_id"])
            continue
        if row["artifact_id"] == "m237_capital_margin_state":
            _add_hard(hard_blocks, f"{row['artifact_id']}_stale", "Account/capital artifact is stale.", source=row["artifact_id"], value=row.get("age_seconds"))
        elif row["artifact_id"] in {"m249_option_whatif_safety_validation", "m153_market_data_desk"}:
            _add_trade_blocker(trade_now_blockers, f"{row['artifact_id']}_stale", "Required trade-now evidence is stale.", source=row["artifact_id"], value=row.get("age_seconds"))
            _add_soft(soft_warnings, f"{row['artifact_id']}_stale", "Required trade-now evidence is stale; keep readiness observation alive but block immediate new entries.", source=row["artifact_id"], value=row.get("age_seconds"))
        else:
            _add_soft(soft_warnings, f"{row['artifact_id']}_stale", "Supporting artifact is stale; keep size constrained or refresh before progression.", source=row["artifact_id"], value=row.get("age_seconds"))
    if normal_deferred_artifacts:
        _add_soft(
            soft_warnings,
            "option_session_deferred_normal",
            "Option-chain artifacts are stale because the option session is deferred/closed; this is normal until the next regular option session.",
            source="m247/m248/m249",
            value=sorted(set(normal_deferred_artifacts)),
        )
    elif option_chain_expected_deferred:
        _add_soft(
            soft_warnings,
            "option_session_deferred_normal",
            "Option-chain refresh is deferred because the option session is closed/deferred; this is normal until the next regular option session.",
            source="m247/m248/m249",
            value=[],
        )

    if capital.get("status") != "green":
        _add_hard(hard_blocks, "capital_state_not_green", "Capital state is not green.", source="m237", value=capital.get("status"))
    if capital.get("withdrawal_guard", {}).get("requires_fresh_account_snapshot"):
        _add_hard(hard_blocks, "capital_snapshot_requires_refresh", "Withdrawal/capital guard requires a fresh account snapshot.", source="m237")
    if any(reason in set(capital.get("status_reasons") or []) for reason in ("stale_account_snapshot", "missing_account_snapshot", "unknown_account_snapshot_age")):
        _add_hard(hard_blocks, "capital_snapshot_not_fresh", "Capital state reports an account snapshot freshness problem.", source="m237", value=capital.get("status_reasons"))

    if capital_pipeline and capital_pipeline.get("status") != "green":
        _add_trade_blocker(trade_now_blockers, "capital_pipeline_not_green", "Capital pipeline has stale/misaligned downstream artifacts; refresh before new automated entries.", source="m244", value=capital_pipeline.get("status"))
        _add_soft(soft_warnings, "capital_pipeline_not_green", "Capital pipeline is not green, but fresh M237 capital can still support monitoring/paper planning.", source="m244", value=capital_pipeline.get("status"))
    elif not capital_pipeline:
        _add_soft(soft_warnings, "capital_pipeline_missing", "M244 capital pipeline artifact is missing; M250 can run but will not promote readiness.", source="m244")

    if market_session_deferred_normal:
        expected_deferred_reasons.append("market_data_session_deferred_normal")
        _add_soft(
            soft_warnings,
            "market_data_session_deferred_normal",
            "Market Data Desk is stale/close-only in a non-regular session without missing/error prices; treat this as normal session deferral, not a system-fatal red.",
            source="m153/m175",
            value={
                "session_priority_class": market.get("session_priority_class"),
                "risk_flags": market.get("risk_flags"),
                "stale_count": market.get("stale_count"),
                "close_only_count": market.get("close_only_count"),
            },
        )
    elif market.get("market_data_status") == "red":
        _add_trade_blocker(trade_now_blockers, "market_data_red", "Market Data Desk is red; treat this as a trade-now restriction, not a system-fatal safety block.", source="m153", value=market.get("risk_flags"))
        _add_soft(soft_warnings, "market_data_red", "Market Data Desk status is red; readiness stays observable while new entries remain restricted.", source="m153", value=market.get("risk_flags"))
    elif market.get("market_data_status") == "yellow":
        _add_soft(soft_warnings, "market_data_yellow", "Market Data Desk status is yellow.", source="m153", value=market.get("risk_flags"))
    if market_session_deferred_normal and (market.get("risk_gate_action") == "block_new_entries" or market.get("new_entries_allowed") is False):
        _add_soft(
            soft_warnings,
            "market_risk_gate_session_deferred_normal",
            "Market Data Risk Gate blocks entries because the current session is not regular/liquid; this is expected deferred behavior.",
            source="m175",
            value=market.get("risk_gate_action"),
        )
    elif market.get("risk_gate_action") == "block_new_entries" or market.get("new_entries_allowed") is False:
        _add_trade_blocker(trade_now_blockers, "market_risk_gate_blocks_new_entries", "Market Data Risk Gate blocks new entries now.", source="m175", value=market.get("risk_gate_action"))
        _add_soft(soft_warnings, "market_risk_gate_blocks_new_entries", "This blocks immediate new alpha entries, but should not make the whole auto-readiness stack red.", source="m175", value=market.get("risk_gate_action"))
    if market.get("temperature_label") in {"hot", "stress"}:
        _add_soft(soft_warnings, "market_temperature_elevated", "Market temperature is elevated; keep any paper sizing constrained.", source="m153", value=market.get("temperature_label"))

    if options.get("option_session_expected_deferred") or options.get("m247_expected_deferred") or options.get("m248_expected_deferred") or options.get("m249_expected_deferred"):
        expected_deferred_reasons.append("option_session_or_ticket_chain_expected_deferred")
    if options.get("supervisor_quote_allowed") is False:
        expected_deferred_reasons.append("option_quote_supervisor_deferred")
    if m249 and m249.get("status") == "expected_deferred":
        expected_deferred_reasons.append("m249_expected_deferred")
    elif not m249:
        _add_live_blocker(live_eligibility_blockers, "m249_missing_or_not_ready", "M249 option what-if safety validation is missing.", source="m249")
        _add_soft(soft_warnings, "m249_missing_or_not_ready", "M249 is missing; option live eligibility is closed until rebuilt.", source="m249")
    elif m249.get("status") != "green" or int(m249_summary.get("passed_count") or 0) <= 0:
        _add_live_blocker(live_eligibility_blockers, "m249_not_passed", "No M249 ticket has passed broker/local what-if safety validation.", source="m249", value=m249.get("status"))
        _add_trade_blocker(trade_now_blockers, "m249_not_passed", "Option tickets are not trade-now eligible without a passing M249 validation.", source="m249", value=m249.get("status"))
    if int(options.get("whatif_pending_count") or 0) > 0:
        _add_soft(soft_warnings, "m249_whatif_pending", "Some M249 items are still waiting for broker what-if evidence.", source="m249")
    if options.get("quote_fresh") is False and not option_chain_expected_deferred:
        _add_soft(soft_warnings, "option_quotes_not_fresh", "Option quote evidence is not fresh.", source="m249")

    if live_validation and live_validation.get("status") == "red":
        _add_hard(hard_blocks, "m243_live_capital_validation_red", "Live read-only capital validation is red.", source="m243")
    elif live_validation and live_validation.get("status") == "yellow":
        _add_soft(soft_warnings, "m243_live_capital_validation_yellow", "Live read-only capital validation has warnings/blocks.", source="m243", value=live_validation.get("block_reason_counts"))

    promotion_gate_blocked = int(learning.get("promotion_gate_blocked_count") or 0)
    promotion_ready = int(learning.get("promotion_ready_count") or 0)
    promotion_waitlist = int(learning.get("promotion_gate_waitlist_count") or 0)
    promotion_watchlist = int(learning.get("promotion_gate_watchlist_count") or 0)
    shadow_ready = int(learning.get("shadow_trial_ready_for_real_promotion_count") or 0)
    promotion_watch_detail_reasons = learning.get("promotion_gate_watchlist_detail_reason_counts") or {}
    promotion_expected_option_deferred_count = int(promotion_watch_detail_reasons.get("option_structure_expected_deferred_stale") or 0)
    promotion_gate_option_session_deferred = bool(
        option_chain_expected_deferred
        and promotion_gate_blocked == 0
        and promotion_waitlist <= 0
        and promotion_watchlist > 0
        and promotion_expected_option_deferred_count > 0
    )
    if promotion_ready <= 0 and shadow_ready <= 0:
        if promotion_gate_option_session_deferred:
            pass
        else:
            _add_live_blocker(live_eligibility_blockers, "promotion_gate_no_ready_candidate", "No promotion-ready scorer/threshold candidate exists.", source="m226")
            _add_soft(soft_warnings, "promotion_gate_no_ready_candidate", "No promotion-ready scorer/threshold candidate exists; keep live eligibility closed but paper/shadow can continue.", source="m226")
    if promotion_gate_blocked > 0 or promotion_waitlist <= 0:
        if promotion_gate_option_session_deferred:
            _add_soft(
                soft_warnings,
                "promotion_gate_option_session_deferred",
                "Promotion Gate has no waitlist only because option-structure evidence is regular-session deferred.",
                source="m226",
                value={
                    "blocked_count": promotion_gate_blocked,
                    "waitlist_count": promotion_waitlist,
                    "watchlist_count": promotion_watchlist,
                    "watchlist_detail_reason_counts": promotion_watch_detail_reasons,
                },
            )
        else:
            _add_live_blocker(
                live_eligibility_blockers,
                "promotion_gate_not_clear",
                "Promotion gate is not clear for live eligibility.",
                source="m226",
                value={
                    "blocked_count": promotion_gate_blocked,
                    "waitlist_count": promotion_waitlist,
                    "blocked_reason_counts": learning.get("promotion_gate_blocked_reason_counts"),
                    "blocked_detail_reason_counts": learning.get("promotion_gate_blocked_detail_reason_counts"),
                },
            )
            _add_soft(
                soft_warnings,
                "promotion_gate_not_clear",
                "Promotion gate blocks live eligibility, but paper/shadow readiness should remain usable.",
                source="m226",
                value={
                    "blocked_count": promotion_gate_blocked,
                    "waitlist_count": promotion_waitlist,
                    "blocked_reason_counts": learning.get("promotion_gate_blocked_reason_counts"),
                    "blocked_detail_reason_counts": learning.get("promotion_gate_blocked_detail_reason_counts"),
                },
            )
    if learning.get("scorer_shadow_only") is True or learning.get("threshold_evaluator_shadow_only") is True:
        _add_soft(soft_warnings, "learning_shadow_only", "Scorer/threshold learning is still in shadow mode.", source="m223/m226")

    if operations.get("evidence_status") in {"red", "blocked"}:
        _add_trade_blocker(trade_now_blockers, "evidence_freshness_red", "Evidence freshness is red/blocked; do not open new entries now.", source="m152", value=operations.get("evidence_status"))
        _add_soft(soft_warnings, "evidence_freshness_red", "Evidence freshness is red/blocked; keep readiness observable while trade-now is closed.", source="m152", value=operations.get("evidence_status"))
    elif operations.get("evidence_status") in {"yellow", "watch"}:
        _add_soft(soft_warnings, "evidence_freshness_yellow", "Evidence freshness status is cautionary.", source="m152", value=operations.get("evidence_status"))
    if operations.get("source_supervisor_status") in {"red", "blocked"}:
        _add_trade_blocker(trade_now_blockers, "source_supervisor_red", "Source Online Supervisor is red/blocked; do not open new entries now.", source="m173", value=operations.get("source_supervisor_status"))
        _add_soft(soft_warnings, "source_supervisor_red", "Source Online Supervisor is red/blocked; keep readiness observable while trade-now is closed.", source="m173", value=operations.get("source_supervisor_status"))
    elif operations.get("source_supervisor_status") in {"yellow", "watch"}:
        _add_soft(soft_warnings, "source_supervisor_yellow", "Source Online Supervisor status is cautionary.", source="m173", value=operations.get("source_supervisor_status"))
    execution_safety_blocked_paths = set(str(path) for path in operations.get("execution_safety_blocked_order_paths") or [])
    option_only_safety_deferred_paths = {
        "new_option_entries",
        "protective_option_ticketing_without_fresh_quotes",
    }
    if operations.get("execution_safety_cache_status") == "red" or operations.get("execution_safety_cache_ready") is False:
        if option_chain_expected_deferred and execution_safety_blocked_paths and execution_safety_blocked_paths.issubset(option_only_safety_deferred_paths):
            _add_soft(
                soft_warnings,
                "execution_safety_cache_option_session_deferred",
                "Execution safety cache blocks only option-entry paths because option quotes are regular-session deferred.",
                source="m211",
                value=sorted(execution_safety_blocked_paths),
            )
        else:
            _add_hard(hard_blocks, "execution_safety_cache_not_ready", "Execution safety cache is not ready.", source="m211", value=operations.get("execution_safety_blocked_order_paths"))

    safety_violations = _safety_violations(
        {
            "m237": capital_state_payload,
            "m241": strategy_allocation_payload,
            "m243": live_read_only_capital_validation_payload,
            "m247": micro_option_candidates_payload,
            "m248": paper_option_ticket_preview_payload,
            "m249": option_whatif_safety_validation_payload,
            "m223": live_scorer_learning_payload,
            "m226": threshold_candidate_evaluator_payload,
            "m211": execution_safety_cache_supervisor_payload,
        }
    )
    if safety_violations:
        _add_hard(hard_blocks, "safety_invariant_violation", "A source artifact reports live order/transmit/portfolio mutation safety violation.", source="safety", value=safety_violations)

    gate_severity_policy = build_gate_severity_policy_report(
        hard_blocks=hard_blocks,
        trade_now_blockers=trade_now_blockers,
        live_eligibility_blockers=live_eligibility_blockers,
        soft_warnings=soft_warnings,
    )
    expected_deferred = bool(expected_deferred_reasons)
    trade_now_allowed = bool(not hard_blocks and not trade_now_blockers and not expected_deferred)
    live_eligibility_clear = bool(
        trade_now_allowed
        and not live_eligibility_blockers
        and int(m249_summary.get("passed_count") or 0) > 0
        and promotion_ready > 0
        and promotion_gate_blocked == 0
    )
    paper_ticket_count = int(_summary(paper_option_ticket_preview_payload).get("ticket_count") or 0)
    m249_passed_count = int(m249_summary.get("passed_count") or 0)
    m249_pending_count = int(m249_summary.get("whatif_pending_count") or 0)
    if safety_violations:
        status = "red"
        readiness_level = "blocked"
    elif hard_blocks:
        status = "red"
        readiness_level = "blocked"
    elif expected_deferred:
        status = "expected_deferred"
        readiness_level = "session_deferred_normal"
    elif live_eligibility_clear:
        status = "green"
        readiness_level = "limited_live_eligible"
    elif trade_now_blockers:
        status = "yellow"
        if m249_passed_count > 0:
            readiness_level = "whatif_ready"
        elif paper_ticket_count > 0 or m249_pending_count > 0:
            readiness_level = "option_paper_ready"
        elif micro_policy.get("allow_fractional_stock_long"):
            readiness_level = "paper_planning_ready"
        else:
            readiness_level = "read_only_ready"
    elif m249_passed_count > 0:
        status = "yellow"
        readiness_level = "whatif_ready"
    elif paper_ticket_count > 0 or m249_pending_count > 0:
        status = "yellow"
        readiness_level = "option_paper_ready"
    elif micro_policy.get("allow_fractional_stock_long"):
        status = "yellow" if (soft_warnings or live_eligibility_blockers) else "green"
        readiness_level = "paper_planning_ready"
    else:
        status = "yellow" if (soft_warnings or live_eligibility_blockers) else "green"
        readiness_level = "read_only_ready"

    eligible_strategy_types: list[str] = []
    if micro_policy.get("allow_fractional_stock_long"):
        eligible_strategy_types.append("fractional_stock_long_micro_paper")
    if micro_policy.get("allow_long_option") and int(options.get("whatif_passed_count") or 0) > 0:
        eligible_strategy_types.append("single_long_option_after_m249")
    if micro_policy.get("allow_defined_risk_spread") and int(options.get("multi_leg_ticket_count") or 0) > 0:
        eligible_strategy_types.append("defined_risk_option_spread_local_paper")
    if micro_policy.get("allow_defined_risk_spread") and int(options.get("whatif_passed_count") or 0) > 0 and int(options.get("multi_leg_ticket_count") or 0) > 0:
        eligible_strategy_types.append("defined_risk_option_spread_after_m249_local_validation")
    if market.get("hedge_entries_allowed"):
        eligible_strategy_types.append("hedge_only_paper")
    if not eligible_strategy_types:
        eligible_strategy_types.append("read_only_monitoring")

    execution_permissions = {
        "paper_scorer_enabled": bool(not safety_violations),
        "paper_execution_enabled": bool(readiness_level in {"paper_planning_ready", "option_paper_ready", "whatif_ready", "limited_live_eligible"} and trade_now_allowed and not safety_violations),
        "whatif_validation_enabled": bool(int(m249_summary.get("passed_count") or 0) > 0 or int(options.get("ticket_count") or 0) > 0),
        "limited_auto_trade_candidate": bool(readiness_level == "limited_live_eligible"),
        "trade_now_allowed": trade_now_allowed,
        "live_eligibility_clear": live_eligibility_clear,
        "live_execution_enabled": False,
        "live_routing_status": "locked_until_explicit_live_routing_milestone",
        "broker_order_submit_allowed": False,
        "transmit_enabled_for_live_orders": False,
    }

    summary = {
        "hard_block_count": len(hard_blocks),
        "soft_warning_count": len(soft_warnings),
        "trade_now_blocker_count": len(trade_now_blockers),
        "live_eligibility_blocker_count": len(live_eligibility_blockers),
        "trade_now_allowed": trade_now_allowed,
        "live_eligibility_clear": live_eligibility_clear,
        "expected_deferred": expected_deferred,
        "expected_deferred_reasons": expected_deferred_reasons,
        "gate_severity_policy_version": gate_severity_policy.get("version"),
        "gate_execution_posture": gate_severity_policy.get("recommended_execution_posture"),
        "gate_haircut_count": gate_severity_policy.get("haircut_count"),
        "gate_confidence_discount_count": gate_severity_policy.get("confidence_discount_count"),
        "gate_monitoring_only_count": gate_severity_policy.get("monitoring_only_count"),
        "gate_size_multiplier": gate_severity_policy.get("size_multiplier"),
        "gate_confidence_multiplier": gate_severity_policy.get("confidence_multiplier"),
        "artifact_status_counts": dict(artifact_counts),
        "stale_artifact_count": len(stale_artifacts),
        "normal_deferred_artifact_count": len(set(normal_deferred_artifacts)),
        "normal_deferred_artifacts": sorted(set(normal_deferred_artifacts)),
        "missing_required_artifact_count": len(missing_required),
        "eligible_strategy_types": eligible_strategy_types,
        "max_allowed_premium": (capital.get("budgets") or {}).get("options_premium_budget"),
        "max_allowed_single_position_notional": (capital.get("budgets") or {}).get("max_single_position_notional"),
        "max_allowed_new_initial_margin": (capital.get("budgets") or {}).get("max_new_initial_margin"),
    }
    return {
        "limited_auto_trading_readiness_version": LIMITED_AUTO_TRADING_READINESS_VERSION,
        "generated_at": now_dt.isoformat(),
        "mode": "limited_auto_trading_readiness_guard",
        "status": status,
        "readiness_level": readiness_level,
        "summary": summary,
        "contexts": {
            "capital": capital,
            "micro_policy": micro_policy,
            "market": market,
            "options": options,
            "learning": learning,
            "operations": operations,
            "gate_severity_policy": gate_severity_policy,
        },
        "artifacts": artifacts,
        "hard_blocks": hard_blocks,
        "trade_now_blockers": trade_now_blockers,
        "live_eligibility_blockers": live_eligibility_blockers,
        "soft_warnings": soft_warnings,
        "execution_permissions": execution_permissions,
        "next_required_steps": _next_steps(
            hard_blocks,
            soft_warnings,
            expected_deferred=expected_deferred,
            trade_now_blockers=trade_now_blockers,
            live_eligibility_blockers=live_eligibility_blockers,
        ),
        "safety": paper_read_only_safety_block(
            thresholds_mutated_by_m250=False,
            safety_violations_detected=safety_violations,
        ),
        "policy": {
            "live_routing_locked_until_explicit_future_milestone": True,
            "requires_m249_passed_broker_whatif": True,
            "requires_promotion_gate_clear": True,
            "fresh_capital_pipeline_is_trade_now_gate_not_system_fatal": True,
            "market_risk_gate_blocks_trade_now_not_operational_readiness": True,
            "promotion_gate_blocks_live_eligibility_not_paper_readiness": True,
            "option_session_deferred_is_normal_until_regular_session": True,
            "market_data_session_deferred_is_not_system_fatal": True,
            "gate_severity_policy_version": GATE_SEVERITY_POLICY_VERSION,
            "gate_severity_split": ["hard_block", "trade_now_blocker", "haircut", "confidence_discount", "monitoring_only"],
            "readiness_levels": [
                "blocked",
                "session_deferred_normal",
                "read_only_ready",
                "paper_planning_ready",
                "option_paper_ready",
                "whatif_ready",
                "limited_live_eligible",
            ],
            "max_artifact_age_minutes": float(max_artifact_age_minutes),
            "artifact_freshness_sla_seconds": DEFAULT_ARTIFACT_FRESHNESS_SLA_SECONDS,
        },
    }


def limited_auto_trading_readiness_markdown(report: dict[str, Any]) -> str:
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    permissions = report.get("execution_permissions") if isinstance(report.get("execution_permissions"), dict) else {}
    contexts = report.get("contexts") if isinstance(report.get("contexts"), dict) else {}
    gate_policy = contexts.get("gate_severity_policy") if isinstance(contexts.get("gate_severity_policy"), dict) else {}
    lines = [
        "# M250 Limited Auto-Trading Readiness",
        "",
        f"- Generated: `{report.get('generated_at')}`",
        f"- Status: `{report.get('status')}`",
        f"- Readiness level: `{report.get('readiness_level')}`",
        f"- Hard blocks / trade-now blockers / live-eligibility blockers / soft warnings: `{summary.get('hard_block_count')}` / `{summary.get('trade_now_blocker_count')}` / `{summary.get('live_eligibility_blocker_count')}` / `{summary.get('soft_warning_count')}`",
        f"- Trade now allowed / live eligibility clear: `{summary.get('trade_now_allowed')}` / `{summary.get('live_eligibility_clear')}`",
        f"- Expected deferred: `{summary.get('expected_deferred')}` reasons `{summary.get('expected_deferred_reasons') or []}`",
        f"- Gate posture: `{summary.get('gate_execution_posture')}` size x`{summary.get('gate_size_multiplier')}` confidence x`{summary.get('gate_confidence_multiplier')}`",
        f"- Gate split hard/trade-now/haircut/confidence/monitoring: `{summary.get('hard_block_count')}` / `{summary.get('trade_now_blocker_count')}` / `{summary.get('gate_haircut_count')}` / `{summary.get('gate_confidence_discount_count')}` / `{summary.get('gate_monitoring_only_count')}`",
        f"- Eligible strategy types: `{summary.get('eligible_strategy_types') or []}`",
        f"- Max premium / single notional / new margin: `{summary.get('max_allowed_premium')}` / `{summary.get('max_allowed_single_position_notional')}` / `{summary.get('max_allowed_new_initial_margin')}`",
        f"- Live routing: `{permissions.get('live_routing_status')}`; live execution enabled `{permissions.get('live_execution_enabled')}`",
        "- Safety: read-only/paper readiness only; no broker order submission, no transmit, no live routing, no portfolio mutation.",
        "",
        "## Hard blocks",
        "",
        "| Code | Source | Message | Value |",
        "| --- | --- | --- | --- |",
    ]
    hard_blocks = report.get("hard_blocks") or []
    if hard_blocks:
        for row in hard_blocks:
            lines.append(f"| {row.get('code')} | {row.get('source')} | {row.get('message')} | `{row.get('value')}` |")
    else:
        lines.append("| - | - | No hard blocks | - |")
    lines.extend(["", "## Trade-now blockers", "", "| Code | Source | Message | Value |", "| --- | --- | --- | --- |"])
    trade_now_blockers = report.get("trade_now_blockers") or []
    if trade_now_blockers:
        for row in trade_now_blockers:
            lines.append(f"| {row.get('code')} | {row.get('source')} | {row.get('message')} | `{row.get('value')}` |")
    else:
        lines.append("| - | - | No trade-now blockers | - |")
    lines.extend(["", "## Live-eligibility blockers", "", "| Code | Source | Message | Value |", "| --- | --- | --- | --- |"])
    live_eligibility_blockers = report.get("live_eligibility_blockers") or []
    if live_eligibility_blockers:
        for row in live_eligibility_blockers:
            lines.append(f"| {row.get('code')} | {row.get('source')} | {row.get('message')} | `{row.get('value')}` |")
    else:
        lines.append("| - | - | No live-eligibility blockers | - |")
    lines.extend(["", "## Soft warnings", "", "| Code | Source | Message | Value |", "| --- | --- | --- | --- |"])
    soft_warnings = report.get("soft_warnings") or []
    if soft_warnings:
        for row in soft_warnings:
            lines.append(f"| {row.get('code')} | {row.get('source')} | {row.get('message')} | `{row.get('value')}` |")
    else:
        lines.append("| - | - | No soft warnings | - |")
    lines.extend(["", "## Realistic gate severity split", "", "| Category | Count | Meaning |", "| --- | ---: | --- |"])
    taxonomy = gate_policy.get("taxonomy") if isinstance(gate_policy.get("taxonomy"), dict) else {}
    counts = gate_policy.get("category_counts") if isinstance(gate_policy.get("category_counts"), dict) else {}
    for category in ("hard_block", "trade_now_blocker", "haircut", "confidence_discount", "monitoring_only"):
        lines.append(f"| `{category}` | {counts.get(category, 0)} | {taxonomy.get(category, '-')} |")
    lines.extend(["", "## Gate policy signals", "", "| Category | Origin | Code | Source | Size x | Confidence x |", "| --- | --- | --- | --- | ---: | ---: |"])
    signals = gate_policy.get("signals") if isinstance(gate_policy.get("signals"), list) else []
    if signals:
        for row in signals[:30]:
            lines.append(
                f"| `{row.get('category')}` | `{row.get('origin')}` | `{row.get('code')}` | `{row.get('source')}` | "
                f"`{row.get('size_multiplier')}` | `{row.get('confidence_multiplier')}` |"
            )
    else:
        lines.append("| - | - | No gate policy signals | - | - | - |")
    lines.extend(["", "## Next required steps", ""])
    for step in report.get("next_required_steps") or []:
        lines.append(f"- {step}")
    lines.extend(["", "## Artifact freshness", "", "| Artifact | Status | Age sec | Reasons |", "| --- | --- | ---: | --- |"])
    for row in report.get("artifacts") or []:
        lines.append(f"| {row.get('artifact_id')} | {row.get('status')} | {row.get('age_seconds')} | `{row.get('reasons')}` |")
    return "\n".join(lines) + "\n"


def write_limited_auto_trading_readiness_json(path: Path, report: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    return path


def write_limited_auto_trading_readiness_markdown(path: Path, report: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(limited_auto_trading_readiness_markdown(report), encoding="utf-8")
    return path
