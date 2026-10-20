from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


CAPITAL_PIPELINE_ORCHESTRATOR_VERSION = "m244-capital-pipeline-freshness-orchestrator-v1"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _read_json(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        return {}
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _payload_result(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not payload:
        return {}
    result = payload.get("result")
    return result if isinstance(result, dict) else payload


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _artifact_generated_at(payload: dict[str, Any]) -> str | None:
    result = _payload_result(payload)
    return result.get("generated_at") or payload.get("generated_at")


def _artifact_status(
    *,
    artifact_id: str,
    path: Path,
    payload: dict[str, Any],
    now_dt: datetime,
    max_age_seconds: float,
    dependency_generated_at: datetime | None,
    dependency_stale: bool,
    max_downstream_lag_seconds: float,
    required: bool = True,
) -> dict[str, Any]:
    exists = bool(payload)
    generated_at = _artifact_generated_at(payload) if exists else None
    generated_dt = _parse_dt(generated_at)
    age_seconds = (now_dt - generated_dt).total_seconds() if generated_dt else None
    reasons: list[str] = []
    if not exists:
        reasons.append("artifact_missing")
    if exists and generated_dt is None:
        reasons.append("artifact_generated_at_missing_or_invalid")
    if age_seconds is not None and age_seconds > max_age_seconds:
        reasons.append("artifact_older_than_policy")
    dependency_lag_seconds = None
    if dependency_generated_at is not None and generated_dt is not None:
        dependency_lag_seconds = (dependency_generated_at - generated_dt).total_seconds()
        if dependency_lag_seconds > max_downstream_lag_seconds:
            reasons.append("older_than_dependency")
    if dependency_stale:
        reasons.append("upstream_dependency_stale")
    if required and not exists:
        severity = "red"
    elif any(reason in reasons for reason in ("artifact_missing", "artifact_generated_at_missing_or_invalid")):
        severity = "red"
    elif reasons:
        severity = "yellow"
    else:
        severity = "green"
    rebuild_required = severity != "green" and artifact_id != "m237_capital_margin_state"
    return {
        "artifact_id": artifact_id,
        "path": str(path.expanduser().resolve()),
        "exists": exists,
        "generated_at": generated_at,
        "age_seconds": round(age_seconds, 3) if age_seconds is not None else None,
        "dependency_generated_at": dependency_generated_at.isoformat() if dependency_generated_at else None,
        "dependency_lag_seconds": round(dependency_lag_seconds, 3) if dependency_lag_seconds is not None else None,
        "status": severity,
        "rebuild_required": rebuild_required,
        "reasons": reasons or ["fresh"],
    }


def build_capital_pipeline_freshness_report(
    *,
    capital_state_path: Path = Path("exports/m237_capital_margin_state.json"),
    capital_aware_gate_path: Path = Path("exports/m239_capital_aware_action_gate.json"),
    ticker_candidate_ranking_path: Path = Path("exports/m241_5_ticker_candidate_ranking.json"),
    cross_asset_expression_ranking_path: Path = Path("exports/m241_7_cross_asset_expression_ranking.json"),
    cross_asset_expression_outcomes_path: Path = Path("exports/m241_8_cross_asset_expression_outcomes.json"),
    ticker_action_candidates_path: Path = Path("exports/m241_6_ticker_action_candidates.json"),
    strategy_allocation_path: Path = Path("exports/m241_strategy_allocation_engine.json"),
    paper_capital_execution_path: Path = Path("exports/m242_paper_capital_aware_execution.json"),
    live_read_only_validation_path: Path = Path("exports/m243_live_read_only_capital_validation.json"),
    max_artifact_age_minutes: float = 30.0,
    max_downstream_lag_seconds: float = 30.0,
    now: str | None = None,
    executed_rebuild: bool = False,
    executed_steps: list[str] | None = None,
) -> dict[str, Any]:
    now_dt = _parse_dt(now) if now else datetime.now(UTC)
    if now_dt is None:
        now_dt = datetime.now(UTC)
    max_age_seconds = max(0.0, float(max_artifact_age_minutes) * 60.0)
    max_lag_seconds = max(0.0, float(max_downstream_lag_seconds))
    payloads = {
        "m237_capital_margin_state": _read_json(capital_state_path),
        "m239_capital_aware_action_gate": _read_json(capital_aware_gate_path),
        "m241_5_ticker_candidate_ranking": _read_json(ticker_candidate_ranking_path),
        "m241_7_cross_asset_expression_ranking": _read_json(cross_asset_expression_ranking_path),
        "m241_8_cross_asset_expression_outcomes": _read_json(cross_asset_expression_outcomes_path),
        "m241_6_ticker_action_candidates": _read_json(ticker_action_candidates_path),
        "m241_strategy_allocation_engine": _read_json(strategy_allocation_path),
        "m242_paper_capital_aware_execution": _read_json(paper_capital_execution_path),
        "m243_live_read_only_capital_validation": _read_json(live_read_only_validation_path),
    }
    paths = {
        "m237_capital_margin_state": capital_state_path,
        "m239_capital_aware_action_gate": capital_aware_gate_path,
        "m241_5_ticker_candidate_ranking": ticker_candidate_ranking_path,
        "m241_7_cross_asset_expression_ranking": cross_asset_expression_ranking_path,
        "m241_8_cross_asset_expression_outcomes": cross_asset_expression_outcomes_path,
        "m241_6_ticker_action_candidates": ticker_action_candidates_path,
        "m241_strategy_allocation_engine": strategy_allocation_path,
        "m242_paper_capital_aware_execution": paper_capital_execution_path,
        "m243_live_read_only_capital_validation": live_read_only_validation_path,
    }
    m237_dt = _parse_dt(_artifact_generated_at(payloads["m237_capital_margin_state"]))
    m239_dt = _parse_dt(_artifact_generated_at(payloads["m239_capital_aware_action_gate"]))
    m237_payload = _payload_result(payloads["m237_capital_margin_state"])
    m237_snapshot_dt = _parse_dt(m237_payload.get("snapshot_generated_at"))
    m237_snapshot_age_seconds = round((now_dt - m237_snapshot_dt).total_seconds(), 3) if m237_snapshot_dt else None
    m237_stored_snapshot_age_seconds = m237_payload.get("snapshot_age_seconds")
    m237_snapshot_age_minutes = round(float(m237_snapshot_age_seconds) / 60.0, 3) if m237_snapshot_age_seconds is not None else None
    artifacts: list[dict[str, Any]] = []
    m237 = _artifact_status(
        artifact_id="m237_capital_margin_state",
        path=capital_state_path,
        payload=payloads["m237_capital_margin_state"],
        now_dt=now_dt,
        max_age_seconds=max_age_seconds,
        dependency_generated_at=None,
        dependency_stale=False,
        max_downstream_lag_seconds=max_lag_seconds,
    )
    artifacts.append(m237)
    m237_stale = m237["status"] != "green"
    m239 = _artifact_status(
        artifact_id="m239_capital_aware_action_gate",
        path=capital_aware_gate_path,
        payload=payloads["m239_capital_aware_action_gate"],
        now_dt=now_dt,
        max_age_seconds=max_age_seconds,
        dependency_generated_at=m237_dt,
        dependency_stale=m237_stale,
        max_downstream_lag_seconds=max_lag_seconds,
    )
    artifacts.append(m239)
    m239_stale = m239["status"] != "green"
    m241_5 = _artifact_status(
        artifact_id="m241_5_ticker_candidate_ranking",
        path=ticker_candidate_ranking_path,
        payload=payloads["m241_5_ticker_candidate_ranking"],
        now_dt=now_dt,
        max_age_seconds=max_age_seconds,
        dependency_generated_at=m237_dt,
        dependency_stale=m237_stale,
        max_downstream_lag_seconds=max_lag_seconds,
    )
    artifacts.append(m241_5)
    m241_5_dt = _parse_dt(_artifact_generated_at(payloads["m241_5_ticker_candidate_ranking"]))
    m241_5_stale = m241_5["status"] != "green"
    m241_7 = _artifact_status(
        artifact_id="m241_7_cross_asset_expression_ranking",
        path=cross_asset_expression_ranking_path,
        payload=payloads["m241_7_cross_asset_expression_ranking"],
        now_dt=now_dt,
        max_age_seconds=max_age_seconds,
        dependency_generated_at=max((dt for dt in (m237_dt, m241_5_dt) if dt), default=None),
        dependency_stale=m237_stale or m241_5_stale,
        max_downstream_lag_seconds=max_lag_seconds,
    )
    artifacts.append(m241_7)
    m241_7_dt = _parse_dt(_artifact_generated_at(payloads["m241_7_cross_asset_expression_ranking"]))
    m241_7_stale = m241_7["status"] != "green"
    m241_8 = _artifact_status(
        artifact_id="m241_8_cross_asset_expression_outcomes",
        path=cross_asset_expression_outcomes_path,
        payload=payloads["m241_8_cross_asset_expression_outcomes"],
        now_dt=now_dt,
        max_age_seconds=max_age_seconds,
        dependency_generated_at=m241_7_dt,
        dependency_stale=m241_7_stale,
        max_downstream_lag_seconds=max_lag_seconds,
    )
    artifacts.append(m241_8)
    m241_8_dt = _parse_dt(_artifact_generated_at(payloads["m241_8_cross_asset_expression_outcomes"]))
    m241_8_stale = m241_8["status"] != "green"
    m241_6 = _artifact_status(
        artifact_id="m241_6_ticker_action_candidates",
        path=ticker_action_candidates_path,
        payload=payloads["m241_6_ticker_action_candidates"],
        now_dt=now_dt,
        max_age_seconds=max_age_seconds,
        dependency_generated_at=max((dt for dt in (m237_dt, m239_dt, m241_5_dt, m241_7_dt) if dt), default=None),
        dependency_stale=m237_stale or m239_stale or m241_5_stale or m241_7_stale,
        max_downstream_lag_seconds=max_lag_seconds,
    )
    artifacts.append(m241_6)
    m241_6_dt = _parse_dt(_artifact_generated_at(payloads["m241_6_ticker_action_candidates"]))
    m241_6_stale = m241_6["status"] != "green"
    downstream_specs = [
        ("m241_strategy_allocation_engine", strategy_allocation_path, max((dt for dt in (m237_dt, m239_dt, m241_5_dt, m241_7_dt, m241_6_dt) if dt), default=None), m237_stale or m239_stale or m241_5_stale or m241_7_stale or m241_6_stale),
        ("m242_paper_capital_aware_execution", paper_capital_execution_path, m239_dt, m239_stale),
        ("m243_live_read_only_capital_validation", live_read_only_validation_path, max((dt for dt in (m237_dt, m239_dt) if dt), default=None), m237_stale or m239_stale),
    ]
    for artifact_id, path, dependency_dt, dependency_stale in downstream_specs:
        artifacts.append(
            _artifact_status(
                artifact_id=artifact_id,
                path=path,
                payload=payloads[artifact_id],
                now_dt=now_dt,
                max_age_seconds=max_age_seconds,
                dependency_generated_at=dependency_dt,
                dependency_stale=dependency_stale,
                max_downstream_lag_seconds=max_lag_seconds,
            )
        )
    status_counts = Counter(row["status"] for row in artifacts)
    reason_counts = Counter(reason for row in artifacts for reason in row.get("reasons", []))
    rebuild_steps = [
        row["artifact_id"]
        for row in artifacts
        if row.get("rebuild_required")
    ]
    if status_counts.get("red"):
        status = "red"
    elif rebuild_steps or status_counts.get("yellow"):
        status = "yellow"
    else:
        status = "green"
    snapshot_fresh = (
        m237_snapshot_age_minutes is not None
        and m237_snapshot_age_minutes <= float(max_artifact_age_minutes)
        and "stale_account_snapshot" not in set(m237_payload.get("status_reasons") or [])
        and "missing_account_snapshot" not in set(m237_payload.get("status_reasons") or [])
        and "unknown_account_snapshot_age" not in set(m237_payload.get("status_reasons") or [])
    )
    return {
        "capital_pipeline_orchestrator_version": CAPITAL_PIPELINE_ORCHESTRATOR_VERSION,
        "generated_at": now_dt.isoformat(),
        "mode": "capital_pipeline_freshness_orchestration",
        "status": status,
        "account_snapshot_freshness": {
            "snapshot_generated_at": m237_payload.get("snapshot_generated_at"),
            "snapshot_age_seconds": m237_snapshot_age_seconds,
            "snapshot_age_minutes": m237_snapshot_age_minutes,
            "stored_m237_snapshot_age_seconds": m237_stored_snapshot_age_seconds,
            "fresh": bool(snapshot_fresh),
            "status_reasons": m237_payload.get("status_reasons") or [],
            "refresh_command_hint": (
                "main.py capital-margin-state --refresh-account-snapshot "
                "--account-snapshot-json exports/m110_ibkr_account_snapshot.json "
                "--json-output-path exports/m237_capital_margin_state.json "
                "--output exports/m237_capital_margin_state.md"
            ),
        },
        "artifact_count": len(artifacts),
        "stale_or_missing_count": sum(1 for row in artifacts if row["status"] != "green"),
        "rebuild_required_count": len(rebuild_steps),
        "rebuild_steps": rebuild_steps,
        "status_counts": dict(status_counts),
        "reason_counts": dict(reason_counts),
        "artifacts": artifacts,
        "paths": {key: str(value.expanduser().resolve()) for key, value in paths.items()},
        "policy": {
            "max_artifact_age_minutes": float(max_artifact_age_minutes),
            "max_downstream_lag_seconds": float(max_downstream_lag_seconds),
            "dependency_order": [
                "m237_capital_margin_state",
                "m239_capital_aware_action_gate",
                "m241_5_ticker_candidate_ranking",
                "m241_7_cross_asset_expression_ranking",
                "m241_8_cross_asset_expression_outcomes",
                "m241_6_ticker_action_candidates",
                "m241_strategy_allocation_engine",
                "m242_paper_capital_aware_execution",
                "m243_live_read_only_capital_validation",
            ],
        },
        "execution": {
            "executed_rebuild": bool(executed_rebuild),
            "executed_steps": executed_steps or [],
        },
        "safety": {
            "read_only_or_paper_only": True,
            "ibkr_order_submitted": False,
            "transmit_enabled": False,
            "live_routing_enabled": False,
            "real_portfolio_mutated": False,
        },
    }


def _write_json(path: Path, payload: dict[str, Any]) -> Path:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return resolved


def _write_text(path: Path, text: str) -> Path:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(text, encoding="utf-8")
    return resolved


def run_capital_pipeline_rebuild(
    *,
    auto_gate_path: Path = Path("exports/m182_market_data_operating_auto_gate_latest.json"),
    capital_state_path: Path = Path("exports/m237_capital_margin_state.json"),
    capital_aware_gate_path: Path = Path("exports/m239_capital_aware_action_gate.json"),
    capital_aware_gate_markdown_path: Path = Path("exports/m239_capital_aware_action_gate.md"),
    ticker_candidate_ranking_path: Path = Path("exports/m241_5_ticker_candidate_ranking.json"),
    ticker_candidate_ranking_markdown_path: Path = Path("exports/m241_5_ticker_candidate_ranking.md"),
    cross_asset_expression_ranking_path: Path = Path("exports/m241_7_cross_asset_expression_ranking.json"),
    cross_asset_expression_ranking_markdown_path: Path = Path("exports/m241_7_cross_asset_expression_ranking.md"),
    cross_asset_expression_outcomes_path: Path = Path("exports/m241_8_cross_asset_expression_outcomes.json"),
    cross_asset_expression_outcomes_markdown_path: Path = Path("exports/m241_8_cross_asset_expression_outcomes.md"),
    ticker_action_candidates_path: Path = Path("exports/m241_6_ticker_action_candidates.json"),
    ticker_action_candidates_markdown_path: Path = Path("exports/m241_6_ticker_action_candidates.md"),
    strategy_allocation_path: Path = Path("exports/m241_strategy_allocation_engine.json"),
    strategy_allocation_markdown_path: Path = Path("exports/m241_strategy_allocation_engine.md"),
    market_data_path: Path = Path("exports/m153_market_data_desk_refresh.json"),
    market_data_risk_gate_path: Path = Path("exports/m175_dashboard_market_data_risk_gate_latest.json"),
    options_market_structure_path: Path = Path("exports/m217_options_market_structure_desk.json"),
    options_market_structure_supervisor_path: Path = Path("exports/m219_options_market_structure_supervisor.json"),
    live_scorer_learning_path: Path = Path("exports/m223_live_scorer_learning.json"),
    news_learning_path: Path = Path("exports/m236_news_learning_report.json"),
    database_path: Path | None = None,
    paper_capital_execution_path: Path = Path("exports/m242_paper_capital_aware_execution.json"),
    paper_capital_execution_markdown_path: Path = Path("exports/m242_paper_capital_aware_execution.md"),
    ledger_path: Path = Path("exports/m242_capital_aware_execution_intent_ledger.json"),
    ledger_markdown_path: Path = Path("exports/m242_capital_aware_execution_intent_ledger.md"),
    router_path: Path = Path("exports/m242_capital_aware_paper_order_router.json"),
    router_markdown_path: Path = Path("exports/m242_capital_aware_paper_order_router.md"),
    portfolio_risk_state_path: Path = Path("exports/m123_paper_portfolio_risk_state.json"),
    session_transition_release_governor_path: Path = Path("exports/m206_session_transition_release_governor.json"),
    operating_auto_gate_path: Path = Path("exports/m182_market_data_operating_auto_gate_latest.json"),
    marks_path: Path | None = None,
    broker_whatif_path: Path = Path("exports/m211_ibkr_defensive_order_whatif_cache.json"),
    live_read_only_validation_path: Path = Path("exports/m243_live_read_only_capital_validation.json"),
    live_read_only_validation_markdown_path: Path = Path("exports/m243_live_read_only_capital_validation.md"),
    reduce_to_fit_capacity: bool = True,
    allow_fractional_shares: bool = True,
    min_stock_quantity: float = 0.01,
    stock_quantity_increment: float = 0.01,
    max_ready_actions: int = 10,
    max_recommendations: int = 25,
    latest_news_limit: int = 12,
    min_option_budget: float = 100.0,
    min_spread_budget: float = 150.0,
    monitor_size_multiplier: float = 0.50,
    max_intents: int = 10,
    max_orders: int = 10,
    fill_price_source: str = "local_estimate",
    fill_model: str = "immediate_local_estimate_fill_v1",
    slippage_bps: float = 0.0,
    commission_per_order: float = 0.0,
    sample_limit: int = 20,
) -> dict[str, Any]:
    from recorder.capital_aware_action_gate import (
        build_capital_aware_action_gate_report,
        capital_aware_action_gate_markdown,
    )
    from recorder.live_read_only_capital_validation import (
        build_live_read_only_capital_validation_report,
        live_read_only_capital_validation_markdown,
    )
    from recorder.options_analysis import (
        build_execution_intent_ledger_report,
        build_paper_order_router_report,
        execution_intent_ledger_markdown,
        paper_order_router_markdown,
    )
    from recorder.strategy_allocation_engine import (
        build_strategy_allocation_engine_report,
        strategy_allocation_engine_markdown,
    )
    from recorder.ticker_candidate_ranking import (
        build_ticker_candidate_ranking_report,
        ticker_candidate_ranking_markdown,
    )
    from recorder.cross_asset_expression_ranking import (
        build_cross_asset_expression_ranking_report,
        cross_asset_expression_ranking_markdown,
    )
    from recorder.cross_asset_expression_outcomes import (
        build_cross_asset_expression_outcomes_report,
        cross_asset_expression_outcomes_markdown,
    )
    from recorder.final_entry_size_resolver import apply_strategy_gate_modifiers_to_capital_gate
    from recorder.ticker_action_candidate_synthesis import (
        build_ticker_action_candidate_synthesis_report,
        ticker_action_candidate_synthesis_markdown,
    )
    from recorder.cli_replay_score_execution_runtime import paper_capital_aware_execution_markdown

    executed_steps: list[str] = []
    capital_state = _read_json(capital_state_path)
    previous_live_validation = _read_json(live_read_only_validation_path)
    previous_micro_policy_state = previous_live_validation.get("micro_policy_state") if isinstance(previous_live_validation.get("micro_policy_state"), dict) else previous_live_validation
    auto_gate = _read_json(auto_gate_path)
    capital_gate = build_capital_aware_action_gate_report(
        auto_gate,
        capital_state,
        reduce_to_fit_capacity=reduce_to_fit_capacity,
        allow_fractional_shares=allow_fractional_shares,
        min_stock_quantity=min_stock_quantity,
        stock_quantity_increment=stock_quantity_increment,
        max_ready_actions=max_ready_actions,
        sample_limit=sample_limit,
    )
    _write_json(capital_aware_gate_path, capital_gate)
    _write_text(capital_aware_gate_markdown_path, capital_aware_action_gate_markdown(capital_gate))
    executed_steps.append("m239_capital_aware_action_gate")

    ticker_ranking = build_ticker_candidate_ranking_report(
        database_path=database_path or Path("market_events.db"),
        market_data_payload=_read_json(market_data_path),
        options_market_structure_payload=_read_json(options_market_structure_path),
        options_market_structure_supervisor_payload=_read_json(options_market_structure_supervisor_path),
        capital_state_payload=capital_state,
        news_learning_payload=_read_json(news_learning_path),
    )
    _write_json(ticker_candidate_ranking_path, ticker_ranking)
    _write_text(ticker_candidate_ranking_markdown_path, ticker_candidate_ranking_markdown(ticker_ranking))
    executed_steps.append("m241_5_ticker_candidate_ranking")

    cross_asset_expression_ranking = build_cross_asset_expression_ranking_report(
        ticker_candidate_ranking_payload=ticker_ranking,
        database_path=database_path or Path("market_events.db"),
        market_data_payload=_read_json(market_data_path),
        options_market_structure_payload=_read_json(options_market_structure_path),
        options_market_structure_supervisor_payload=_read_json(options_market_structure_supervisor_path),
        capital_state_payload=capital_state,
        cross_asset_tuning_payload=_read_json(cross_asset_expression_outcomes_path),
    )
    _write_json(cross_asset_expression_ranking_path, cross_asset_expression_ranking)
    _write_text(cross_asset_expression_ranking_markdown_path, cross_asset_expression_ranking_markdown(cross_asset_expression_ranking))
    executed_steps.append("m241_7_cross_asset_expression_ranking")

    cross_asset_expression_outcomes = build_cross_asset_expression_outcomes_report(
        database_path=database_path or Path("market_events.db"),
        cross_asset_expression_ranking_payload=cross_asset_expression_ranking,
    )
    _write_json(cross_asset_expression_outcomes_path, cross_asset_expression_outcomes)
    _write_text(cross_asset_expression_outcomes_markdown_path, cross_asset_expression_outcomes_markdown(cross_asset_expression_outcomes))
    executed_steps.append("m241_8_cross_asset_expression_outcomes")

    ticker_action_candidates = build_ticker_action_candidate_synthesis_report(
        ticker_candidate_ranking_payload=ticker_ranking,
        cross_asset_expression_ranking_payload=cross_asset_expression_ranking,
        capital_aware_gate_payload=capital_gate,
        capital_state_payload=capital_state,
    )
    _write_json(ticker_action_candidates_path, ticker_action_candidates)
    _write_text(ticker_action_candidates_markdown_path, ticker_action_candidate_synthesis_markdown(ticker_action_candidates))
    executed_steps.append("m241_6_ticker_action_candidates")

    strategy = build_strategy_allocation_engine_report(
        capital_aware_gate_payload=capital_gate,
        capital_state_payload=capital_state,
        market_data_payload=_read_json(market_data_path),
        market_data_risk_gate_payload=_read_json(market_data_risk_gate_path),
        options_market_structure_payload=_read_json(options_market_structure_path),
        options_market_structure_supervisor_payload=_read_json(options_market_structure_supervisor_path),
        live_scorer_learning_payload=_read_json(live_scorer_learning_path),
        news_learning_payload=_read_json(news_learning_path),
        ticker_candidate_ranking_payload=ticker_ranking,
        ticker_action_candidate_synthesis_payload=ticker_action_candidates,
        previous_micro_policy_state_payload=previous_micro_policy_state,
        database_path=database_path,
        min_option_budget=min_option_budget,
        min_spread_budget=min_spread_budget,
        max_recommendations=max_recommendations,
        latest_news_limit=latest_news_limit,
    )
    _write_json(strategy_allocation_path, strategy)
    _write_text(strategy_allocation_markdown_path, strategy_allocation_engine_markdown(strategy))
    executed_steps.append("m241_strategy_allocation_engine")
    execution_capital_gate = apply_strategy_gate_modifiers_to_capital_gate(capital_gate, strategy)

    filters = {
        "capital_aware_gate_json": str(capital_aware_gate_path.expanduser().resolve()),
        "portfolio_risk_state_json": str(portfolio_risk_state_path.expanduser().resolve()) if portfolio_risk_state_path else None,
        "session_transition_release_governor_json": str(session_transition_release_governor_path.expanduser().resolve()) if session_transition_release_governor_path else None,
        "operating_auto_gate_json": str(operating_auto_gate_path.expanduser().resolve()) if operating_auto_gate_path else None,
        "marks_json": str(marks_path.expanduser().resolve()) if marks_path else None,
        "require_capital_aware_gate": True,
        "require_portfolio_risk_gate": True,
        "require_operating_preflight": True,
        "require_session_transition_release_governor": True,
        "max_intents": max_intents,
        "max_orders": max_orders,
        "fill_price_source": fill_price_source,
    }
    ledger = build_execution_intent_ledger_report(
        execution_capital_gate,
        existing_ledger_payload=_read_json(ledger_path),
        portfolio_risk_state_payload=_read_json(portfolio_risk_state_path),
        session_transition_release_governor_payload=_read_json(session_transition_release_governor_path),
        require_capital_aware_gate=True,
        monitor_size_multiplier=monitor_size_multiplier,
        max_intents=max_intents,
        ledger_run_id=None,
        sample_limit=sample_limit,
    )
    _write_json(ledger_path, {"result": ledger, "filters": filters, "output": str(ledger_markdown_path.expanduser().resolve())})
    _write_text(ledger_markdown_path, execution_intent_ledger_markdown(ledger, filters=filters))
    router = build_paper_order_router_report(
        {"result": ledger},
        existing_router_payload=_read_json(router_path),
        marks_payload=_read_json(marks_path),
        operating_auto_gate_payload=_read_json(operating_auto_gate_path),
        session_transition_release_governor_payload=_read_json(session_transition_release_governor_path),
        require_operating_preflight=True,
        allow_non_operating_source_ledger=False,
        require_session_transition_release_governor=True,
        fill_price_source=fill_price_source,
        fill_model=fill_model,
        require_capital_aware_gate=True,
        require_portfolio_risk_gate=True,
        max_orders=max_orders,
        slippage_bps=slippage_bps,
        commission_per_order=commission_per_order,
        router_run_id=None,
        sample_limit=sample_limit,
    )
    _write_json(router_path, {"result": router, "filters": filters, "output": str(router_markdown_path.expanduser().resolve())})
    _write_text(router_markdown_path, paper_order_router_markdown(router, filters=filters))
    paper_execution = {
        "paper_capital_aware_execution_version": "m242-paper-capital-aware-execution-v1",
        "generated_at": _now_iso(),
        "mode": "paper_capital_aware_execution",
        "require_capital_aware_gate": True,
        "filters": filters,
        "ledger": ledger,
        "router": router,
        "artifacts": {
            "ledger_markdown": str(ledger_markdown_path.expanduser().resolve()),
            "ledger_json": str(ledger_path.expanduser().resolve()),
            "router_markdown": str(router_markdown_path.expanduser().resolve()),
            "router_json": str(router_path.expanduser().resolve()),
        },
        "safety": {
            "paper_only": True,
            "ibkr_order_submitted": False,
            "transmit_enabled": False,
            "live_order_routing_enabled": False,
        },
    }
    _write_json(paper_capital_execution_path, {"result": paper_execution, "output": str(paper_capital_execution_markdown_path.expanduser().resolve())})
    _write_text(paper_capital_execution_markdown_path, paper_capital_aware_execution_markdown(paper_execution))
    executed_steps.append("m242_paper_capital_aware_execution")

    live_validation = build_live_read_only_capital_validation_report(
        capital_state_payload=capital_state,
        capital_aware_gate_payload=execution_capital_gate,
        broker_whatif_payload=_read_json(broker_whatif_path),
        allow_fractional_shares=allow_fractional_shares,
        stock_quantity_increment=stock_quantity_increment,
        max_items=max_recommendations,
        sample_limit=sample_limit,
        previous_micro_policy_state_payload=previous_micro_policy_state,
    )
    _write_json(live_read_only_validation_path, live_validation)
    _write_text(live_read_only_validation_markdown_path, live_read_only_capital_validation_markdown(live_validation))
    executed_steps.append("m243_live_read_only_capital_validation")
    return {"executed_steps": executed_steps}


def capital_pipeline_orchestrator_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# M244 Capital Pipeline Freshness Orchestrator",
        "",
        f"- Generated: `{report.get('generated_at')}`",
        f"- Status: `{report.get('status')}`",
        f"- Stale/missing artifacts: `{report.get('stale_or_missing_count')}`",
        f"- Rebuild required: `{report.get('rebuild_required_count')}`",
        f"- Rebuild steps: `{report.get('rebuild_steps') or []}`",
        f"- Executed rebuild: `{(report.get('execution') or {}).get('executed_rebuild')}` steps `{(report.get('execution') or {}).get('executed_steps') or []}`",
        "- Safety: read-only or paper-only artifact rebuilds; no broker order submission, no transmit, no live routing.",
        "",
        "## Artifact freshness",
        "",
        "| Artifact | Status | Age sec | Dependency lag sec | Rebuild | Reasons |",
        "| --- | --- | ---: | ---: | --- | --- |",
    ]
    for row in report.get("artifacts") or []:
        lines.append(
            f"| `{row.get('artifact_id')}` | `{row.get('status')}` | {row.get('age_seconds')} | "
            f"{row.get('dependency_lag_seconds')} | `{row.get('rebuild_required')}` | `{', '.join(row.get('reasons') or [])}` |"
        )
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "M244 prevents a fresh account/capital snapshot from being mixed with older downstream ranking, cross-asset expression, outcome tuning, action synthesis, allocation, paper execution, or live read-only capacity artifacts. If a downstream artifact is older than its dependency, it is flagged for rebuild in dependency order.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_capital_pipeline_orchestrator_json(path: Path, report: dict[str, Any]) -> Path:
    return _write_json(path, report)


def write_capital_pipeline_orchestrator_markdown(path: Path, report: dict[str, Any]) -> Path:
    return _write_text(path, capital_pipeline_orchestrator_markdown(report))
