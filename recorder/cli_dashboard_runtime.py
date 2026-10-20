from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .cli_profile_utils import (
    coalesce_profile_path as _coalesce_profile_path,
    coalesce_profile_value as _coalesce_profile_value,
    load_profile_json as _load_profile_json,
)


def _console_safe_text(text: str) -> str:
    return text.encode("ascii", errors="replace").decode("ascii")


def command_trading_dashboard_snapshot(
    settings: Any,
    profile_json_path: Path | None,
    account_snapshot_json: Path | None,
    paper_portfolio_state_json: Path | None,
    portfolio_risk_state_json: Path | None,
    execution_intent_ledger_json: Path | None,
    mark_quality_daemon_json: Path | None,
    economic_desk_json: Path | None,
    market_data_json: Path | None,
    market_data_session_overlay_json: Path | None,
    market_data_risk_gate_json: Path | None,
    market_data_operating_auto_gate_json: Path | None,
    capital_margin_state_json: Path | None,
    capital_aware_action_gate_json: Path | None,
    live_read_only_capital_validation_json: Path | None,
    capital_pipeline_orchestrator_json: Path | None,
    market_data_session_transition_gate_json: Path | None,
    session_transition_release_governor_json: Path | None,
    market_data_operating_execution_intent_ledger_json: Path | None,
    operating_defensive_action_plan_json: Path | None,
    operating_defensive_intent_ledger_json: Path | None,
    evidence_freshness_json: Path | None,
    evidence_refresh_watchdog_json: Path | None,
    source_ingest_health_json: Path | None,
    source_online_supervisor_json: Path | None,
    live_scorer_learning_json: Path | None,
    live_learning_llm_notes_json: Path | None,
    execution_safety_cache_supervisor_json: Path | None,
    options_market_structure_json: Path | None,
    options_market_structure_supervisor_json: Path | None,
    micro_option_candidates_json: Path | None,
    paper_option_ticket_preview_json: Path | None,
    option_whatif_safety_validation_json: Path | None,
    database_path: Path | None,
    news_limit: int,
    regime_limit: int,
    mode: str,
    output: Path | None,
    json_output_path: Path | None,
    json_output: bool,
) -> dict[str, Any]:
    from recorder.trading_dashboard import (
        build_trading_dashboard_snapshot,
        trading_dashboard_markdown,
        write_trading_dashboard_markdown,
        write_trading_dashboard_snapshot_json,
    )

    profile: dict[str, Any] = {}
    profile_base: Path | None = None
    if profile_json_path is not None:
        profile, profile_base = _load_profile_json(profile_json_path)
    account_snapshot_json = _coalesce_profile_path(account_snapshot_json, profile, "paths", "account_snapshot_json", profile_base)
    paper_portfolio_state_json = _coalesce_profile_path(paper_portfolio_state_json, profile, "paths", "paper_portfolio_state_json", profile_base)
    portfolio_risk_state_json = _coalesce_profile_path(portfolio_risk_state_json, profile, "paths", "portfolio_risk_state_json", profile_base)
    execution_intent_ledger_json = _coalesce_profile_path(execution_intent_ledger_json, profile, "paths", "execution_intent_ledger_json", profile_base)
    mark_quality_daemon_json = _coalesce_profile_path(mark_quality_daemon_json, profile, "paths", "mark_quality_daemon_json", profile_base)
    economic_desk_json = _coalesce_profile_path(economic_desk_json, profile, "paths", "economic_desk_json", profile_base)
    market_data_json = _coalesce_profile_path(market_data_json, profile, "paths", "market_data_json", profile_base)
    market_data_session_overlay_json = _coalesce_profile_path(market_data_session_overlay_json, profile, "paths", "market_data_session_overlay_json", profile_base)
    market_data_risk_gate_json = _coalesce_profile_path(market_data_risk_gate_json, profile, "paths", "market_data_risk_gate_json", profile_base)
    market_data_operating_auto_gate_json = (
        _coalesce_profile_path(market_data_operating_auto_gate_json, profile, "paths", "market_data_operating_auto_gate_json", profile_base)
        or _coalesce_profile_path(None, profile, "outputs", "market_data_operating_auto_gate_json_output_path", profile_base)
    )
    capital_margin_state_json = _coalesce_profile_path(capital_margin_state_json, profile, "paths", "capital_margin_state_json", profile_base)
    capital_aware_action_gate_json = _coalesce_profile_path(capital_aware_action_gate_json, profile, "paths", "capital_aware_action_gate_json", profile_base)
    live_read_only_capital_validation_json = _coalesce_profile_path(
        live_read_only_capital_validation_json,
        profile,
        "paths",
        "live_read_only_capital_validation_json",
        profile_base,
    )
    capital_pipeline_orchestrator_json = _coalesce_profile_path(
        capital_pipeline_orchestrator_json,
        profile,
        "paths",
        "capital_pipeline_orchestrator_json",
        profile_base,
    )
    market_data_session_transition_gate_json = _coalesce_profile_path(
        market_data_session_transition_gate_json,
        profile,
        "paths",
        "market_data_session_transition_gate_json",
        profile_base,
    )
    session_transition_release_governor_json = _coalesce_profile_path(
        session_transition_release_governor_json,
        profile,
        "paths",
        "session_transition_release_governor_json",
        profile_base,
    )
    market_data_operating_execution_intent_ledger_json = (
        _coalesce_profile_path(market_data_operating_execution_intent_ledger_json, profile, "paths", "market_data_operating_execution_intent_ledger_json", profile_base)
        or _coalesce_profile_path(None, profile, "outputs", "market_data_operating_execution_intent_ledger_json_output_path", profile_base)
    )
    operating_defensive_action_plan_json = _coalesce_profile_path(
        operating_defensive_action_plan_json,
        profile,
        "paths",
        "operating_defensive_action_plan_json",
        profile_base,
    )
    operating_defensive_intent_ledger_json = _coalesce_profile_path(
        operating_defensive_intent_ledger_json,
        profile,
        "paths",
        "operating_defensive_intent_ledger_json",
        profile_base,
    )
    evidence_freshness_json = _coalesce_profile_path(evidence_freshness_json, profile, "paths", "evidence_freshness_json", profile_base)
    evidence_refresh_watchdog_json = _coalesce_profile_path(evidence_refresh_watchdog_json, profile, "paths", "evidence_refresh_watchdog_json", profile_base)
    source_ingest_health_json = _coalesce_profile_path(source_ingest_health_json, profile, "paths", "source_ingest_health_json", profile_base)
    source_online_supervisor_json = _coalesce_profile_path(source_online_supervisor_json, profile, "paths", "source_online_supervisor_json", profile_base)
    live_scorer_learning_json = _coalesce_profile_path(live_scorer_learning_json, profile, "paths", "live_scorer_learning_json", profile_base)
    live_learning_llm_notes_json = _coalesce_profile_path(live_learning_llm_notes_json, profile, "paths", "live_learning_llm_notes_json", profile_base)
    threshold_candidate_evaluator_json = _coalesce_profile_path(None, profile, "paths", "threshold_candidate_evaluator_json", profile_base)
    m232_hot_cache_json = _coalesce_profile_path(None, profile, "paths", "m232_hot_cache_json", profile_base)
    execution_safety_cache_supervisor_json = _coalesce_profile_path(execution_safety_cache_supervisor_json, profile, "paths", "execution_safety_cache_supervisor_json", profile_base)
    options_market_structure_json = _coalesce_profile_path(options_market_structure_json, profile, "paths", "options_market_structure_json", profile_base)
    options_market_structure_supervisor_json = _coalesce_profile_path(options_market_structure_supervisor_json, profile, "paths", "options_market_structure_supervisor_json", profile_base)
    strategy_allocation_engine_json = _coalesce_profile_path(None, profile, "paths", "strategy_allocation_engine_json", profile_base)
    micro_option_candidates_json = _coalesce_profile_path(micro_option_candidates_json, profile, "paths", "micro_option_candidates_json", profile_base)
    paper_option_ticket_preview_json = _coalesce_profile_path(paper_option_ticket_preview_json, profile, "paths", "paper_option_ticket_preview_json", profile_base)
    option_whatif_safety_validation_json = _coalesce_profile_path(option_whatif_safety_validation_json, profile, "paths", "option_whatif_safety_validation_json", profile_base)
    database_path = _coalesce_profile_path(database_path, profile, "paths", "database_path", profile_base) or settings.database_path
    output = _coalesce_profile_path(output, profile, "outputs", "output", profile_base)
    json_output_path = _coalesce_profile_path(json_output_path, profile, "outputs", "json_output_path", profile_base)
    news_limit = int(_coalesce_profile_value(news_limit, profile, "dashboard", "news_limit", news_limit))
    regime_limit = int(_coalesce_profile_value(regime_limit, profile, "dashboard", "regime_limit", regime_limit))
    mode = str(_coalesce_profile_value(mode, profile, "dashboard", "mode", mode))
    snapshot = build_trading_dashboard_snapshot(
        account_snapshot_path=account_snapshot_json,
        paper_portfolio_state_path=paper_portfolio_state_json,
        portfolio_risk_state_path=portfolio_risk_state_json,
        execution_intent_ledger_path=execution_intent_ledger_json,
        mark_quality_daemon_path=mark_quality_daemon_json,
        economic_desk_path=economic_desk_json,
        market_data_path=market_data_json,
        market_data_session_overlay_path=market_data_session_overlay_json,
        market_data_risk_gate_path=market_data_risk_gate_json,
        market_data_operating_auto_gate_path=market_data_operating_auto_gate_json,
        capital_margin_state_path=capital_margin_state_json,
        capital_aware_action_gate_path=capital_aware_action_gate_json,
        live_read_only_capital_validation_path=live_read_only_capital_validation_json,
        capital_pipeline_orchestrator_path=capital_pipeline_orchestrator_json,
        market_data_session_transition_gate_path=market_data_session_transition_gate_json,
        session_transition_release_governor_path=session_transition_release_governor_json,
        market_data_operating_execution_intent_ledger_path=market_data_operating_execution_intent_ledger_json,
        operating_defensive_action_plan_path=operating_defensive_action_plan_json,
        operating_defensive_intent_ledger_path=operating_defensive_intent_ledger_json,
        database_path=database_path,
        evidence_freshness_path=evidence_freshness_json,
        evidence_refresh_watchdog_path=evidence_refresh_watchdog_json,
        source_ingest_health_path=source_ingest_health_json,
        source_online_supervisor_path=source_online_supervisor_json,
        live_scorer_learning_path=live_scorer_learning_json,
        live_learning_llm_notes_path=live_learning_llm_notes_json,
        threshold_candidate_evaluator_path=threshold_candidate_evaluator_json,
        m232_hot_cache_path=m232_hot_cache_json,
        execution_safety_cache_supervisor_path=execution_safety_cache_supervisor_json,
        options_market_structure_path=options_market_structure_json,
        options_market_structure_supervisor_path=options_market_structure_supervisor_json,
        strategy_allocation_engine_path=strategy_allocation_engine_json,
        micro_option_candidates_path=micro_option_candidates_json,
        paper_option_ticket_preview_path=paper_option_ticket_preview_json,
        option_whatif_safety_validation_path=option_whatif_safety_validation_json,
        news_limit=news_limit,
        regime_limit=regime_limit,
        mode=mode,
    )
    written = write_trading_dashboard_markdown(output, snapshot) if output else None
    json_written = write_trading_dashboard_snapshot_json(json_output_path, snapshot) if json_output_path else None
    if json_output:
        print(json.dumps(snapshot, indent=2, ensure_ascii=True))
        return snapshot
    print(_console_safe_text(trading_dashboard_markdown(snapshot)))
    if written:
        print(f"\nMarkdown report written: {written}")
    if json_written:
        print(f"JSON snapshot written: {json_written}")
    return snapshot


def command_trading_dashboard_html(
    settings: Any,
    profile_json_path: Path | None,
    snapshot_json: Path | None,
    account_snapshot_json: Path | None,
    paper_portfolio_state_json: Path | None,
    portfolio_risk_state_json: Path | None,
    execution_intent_ledger_json: Path | None,
    mark_quality_daemon_json: Path | None,
    economic_desk_json: Path | None,
    market_data_json: Path | None,
    market_data_session_overlay_json: Path | None,
    market_data_risk_gate_json: Path | None,
    market_data_operating_auto_gate_json: Path | None,
    capital_margin_state_json: Path | None,
    capital_aware_action_gate_json: Path | None,
    live_read_only_capital_validation_json: Path | None,
    capital_pipeline_orchestrator_json: Path | None,
    market_data_session_transition_gate_json: Path | None,
    session_transition_release_governor_json: Path | None,
    market_data_operating_execution_intent_ledger_json: Path | None,
    operating_defensive_action_plan_json: Path | None,
    operating_defensive_intent_ledger_json: Path | None,
    evidence_freshness_json: Path | None,
    evidence_refresh_watchdog_json: Path | None,
    source_ingest_health_json: Path | None,
    source_online_supervisor_json: Path | None,
    live_scorer_learning_json: Path | None,
    live_learning_llm_notes_json: Path | None,
    execution_safety_cache_supervisor_json: Path | None,
    options_market_structure_json: Path | None,
    options_market_structure_supervisor_json: Path | None,
    micro_option_candidates_json: Path | None,
    paper_option_ticket_preview_json: Path | None,
    option_whatif_safety_validation_json: Path | None,
    database_path: Path | None,
    news_limit: int,
    regime_limit: int,
    mode: str,
    output: Path | None,
    json_output_path: Path | None,
    refresh_seconds: int | None,
    title: str | None,
    json_output: bool,
) -> dict[str, Any]:
    from recorder.trading_dashboard import (
        build_trading_dashboard_snapshot,
        write_trading_dashboard_html,
        write_trading_dashboard_snapshot_json,
    )

    profile: dict[str, Any] = {}
    profile_base: Path | None = None
    if profile_json_path is not None:
        profile, profile_base = _load_profile_json(profile_json_path)

    snapshot_json = _coalesce_profile_path(snapshot_json, profile, "outputs", "snapshot_json_path", profile_base)
    account_snapshot_json = _coalesce_profile_path(account_snapshot_json, profile, "paths", "account_snapshot_json", profile_base)
    paper_portfolio_state_json = _coalesce_profile_path(paper_portfolio_state_json, profile, "paths", "paper_portfolio_state_json", profile_base)
    portfolio_risk_state_json = _coalesce_profile_path(portfolio_risk_state_json, profile, "paths", "portfolio_risk_state_json", profile_base)
    execution_intent_ledger_json = _coalesce_profile_path(execution_intent_ledger_json, profile, "paths", "execution_intent_ledger_json", profile_base)
    mark_quality_daemon_json = _coalesce_profile_path(mark_quality_daemon_json, profile, "paths", "mark_quality_daemon_json", profile_base)
    economic_desk_json = _coalesce_profile_path(economic_desk_json, profile, "paths", "economic_desk_json", profile_base)
    market_data_json = _coalesce_profile_path(market_data_json, profile, "paths", "market_data_json", profile_base)
    market_data_session_overlay_json = _coalesce_profile_path(market_data_session_overlay_json, profile, "paths", "market_data_session_overlay_json", profile_base)
    market_data_risk_gate_json = _coalesce_profile_path(market_data_risk_gate_json, profile, "paths", "market_data_risk_gate_json", profile_base)
    market_data_operating_auto_gate_json = (
        _coalesce_profile_path(market_data_operating_auto_gate_json, profile, "paths", "market_data_operating_auto_gate_json", profile_base)
        or _coalesce_profile_path(None, profile, "outputs", "market_data_operating_auto_gate_json_output_path", profile_base)
    )
    capital_margin_state_json = _coalesce_profile_path(capital_margin_state_json, profile, "paths", "capital_margin_state_json", profile_base)
    capital_aware_action_gate_json = _coalesce_profile_path(capital_aware_action_gate_json, profile, "paths", "capital_aware_action_gate_json", profile_base)
    live_read_only_capital_validation_json = _coalesce_profile_path(
        live_read_only_capital_validation_json,
        profile,
        "paths",
        "live_read_only_capital_validation_json",
        profile_base,
    )
    capital_pipeline_orchestrator_json = _coalesce_profile_path(
        capital_pipeline_orchestrator_json,
        profile,
        "paths",
        "capital_pipeline_orchestrator_json",
        profile_base,
    )
    market_data_session_transition_gate_json = _coalesce_profile_path(
        market_data_session_transition_gate_json,
        profile,
        "paths",
        "market_data_session_transition_gate_json",
        profile_base,
    )
    session_transition_release_governor_json = _coalesce_profile_path(
        session_transition_release_governor_json,
        profile,
        "paths",
        "session_transition_release_governor_json",
        profile_base,
    )
    market_data_operating_execution_intent_ledger_json = (
        _coalesce_profile_path(market_data_operating_execution_intent_ledger_json, profile, "paths", "market_data_operating_execution_intent_ledger_json", profile_base)
        or _coalesce_profile_path(None, profile, "outputs", "market_data_operating_execution_intent_ledger_json_output_path", profile_base)
    )
    operating_defensive_action_plan_json = _coalesce_profile_path(
        operating_defensive_action_plan_json,
        profile,
        "paths",
        "operating_defensive_action_plan_json",
        profile_base,
    )
    operating_defensive_intent_ledger_json = _coalesce_profile_path(
        operating_defensive_intent_ledger_json,
        profile,
        "paths",
        "operating_defensive_intent_ledger_json",
        profile_base,
    )
    evidence_freshness_json = _coalesce_profile_path(evidence_freshness_json, profile, "paths", "evidence_freshness_json", profile_base)
    evidence_refresh_watchdog_json = _coalesce_profile_path(evidence_refresh_watchdog_json, profile, "paths", "evidence_refresh_watchdog_json", profile_base)
    source_ingest_health_json = _coalesce_profile_path(source_ingest_health_json, profile, "paths", "source_ingest_health_json", profile_base)
    source_online_supervisor_json = _coalesce_profile_path(source_online_supervisor_json, profile, "paths", "source_online_supervisor_json", profile_base)
    live_scorer_learning_json = _coalesce_profile_path(live_scorer_learning_json, profile, "paths", "live_scorer_learning_json", profile_base)
    live_learning_llm_notes_json = _coalesce_profile_path(live_learning_llm_notes_json, profile, "paths", "live_learning_llm_notes_json", profile_base)
    threshold_candidate_evaluator_json = _coalesce_profile_path(None, profile, "paths", "threshold_candidate_evaluator_json", profile_base)
    m232_hot_cache_json = _coalesce_profile_path(None, profile, "paths", "m232_hot_cache_json", profile_base)
    execution_safety_cache_supervisor_json = _coalesce_profile_path(execution_safety_cache_supervisor_json, profile, "paths", "execution_safety_cache_supervisor_json", profile_base)
    options_market_structure_json = _coalesce_profile_path(options_market_structure_json, profile, "paths", "options_market_structure_json", profile_base)
    options_market_structure_supervisor_json = _coalesce_profile_path(options_market_structure_supervisor_json, profile, "paths", "options_market_structure_supervisor_json", profile_base)
    strategy_allocation_engine_json = _coalesce_profile_path(None, profile, "paths", "strategy_allocation_engine_json", profile_base)
    micro_option_candidates_json = _coalesce_profile_path(micro_option_candidates_json, profile, "paths", "micro_option_candidates_json", profile_base)
    paper_option_ticket_preview_json = _coalesce_profile_path(paper_option_ticket_preview_json, profile, "paths", "paper_option_ticket_preview_json", profile_base)
    option_whatif_safety_validation_json = _coalesce_profile_path(option_whatif_safety_validation_json, profile, "paths", "option_whatif_safety_validation_json", profile_base)
    database_path = _coalesce_profile_path(database_path, profile, "paths", "database_path", profile_base) or settings.database_path
    output = _coalesce_profile_path(output, profile, "outputs", "html_output_path", profile_base) or Path("exports/trading_command_center.html")
    json_output_path = _coalesce_profile_path(json_output_path, profile, "outputs", "json_output_path", profile_base)
    news_limit = int(_coalesce_profile_value(news_limit, profile, "dashboard", "news_limit", news_limit))
    regime_limit = int(_coalesce_profile_value(regime_limit, profile, "dashboard", "regime_limit", regime_limit))
    mode = str(_coalesce_profile_value(mode, profile, "dashboard", "mode", mode))
    refresh_seconds = int(_coalesce_profile_value(refresh_seconds, profile, "dashboard", "refresh_seconds", 15))
    title = str(_coalesce_profile_value(title, profile, "dashboard", "title", "Trading Command Center"))

    if snapshot_json is not None:
        resolved_snapshot = snapshot_json.expanduser().resolve()
        snapshot = json.loads(resolved_snapshot.read_text(encoding="utf-8"))
        snapshot_json_reference: Path | str | None = resolved_snapshot
    else:
        snapshot = build_trading_dashboard_snapshot(
            account_snapshot_path=account_snapshot_json,
            paper_portfolio_state_path=paper_portfolio_state_json,
            portfolio_risk_state_path=portfolio_risk_state_json,
            execution_intent_ledger_path=execution_intent_ledger_json,
            mark_quality_daemon_path=mark_quality_daemon_json,
            economic_desk_path=economic_desk_json,
            market_data_path=market_data_json,
            market_data_session_overlay_path=market_data_session_overlay_json,
            market_data_risk_gate_path=market_data_risk_gate_json,
            market_data_operating_auto_gate_path=market_data_operating_auto_gate_json,
            capital_margin_state_path=capital_margin_state_json,
            capital_aware_action_gate_path=capital_aware_action_gate_json,
            live_read_only_capital_validation_path=live_read_only_capital_validation_json,
            capital_pipeline_orchestrator_path=capital_pipeline_orchestrator_json,
            market_data_session_transition_gate_path=market_data_session_transition_gate_json,
            session_transition_release_governor_path=session_transition_release_governor_json,
            market_data_operating_execution_intent_ledger_path=market_data_operating_execution_intent_ledger_json,
            operating_defensive_action_plan_path=operating_defensive_action_plan_json,
            operating_defensive_intent_ledger_path=operating_defensive_intent_ledger_json,
            database_path=database_path,
            evidence_freshness_path=evidence_freshness_json,
            evidence_refresh_watchdog_path=evidence_refresh_watchdog_json,
            source_ingest_health_path=source_ingest_health_json,
            source_online_supervisor_path=source_online_supervisor_json,
            live_scorer_learning_path=live_scorer_learning_json,
            live_learning_llm_notes_path=live_learning_llm_notes_json,
            threshold_candidate_evaluator_path=threshold_candidate_evaluator_json,
            m232_hot_cache_path=m232_hot_cache_json,
            execution_safety_cache_supervisor_path=execution_safety_cache_supervisor_json,
            options_market_structure_path=options_market_structure_json,
            options_market_structure_supervisor_path=options_market_structure_supervisor_json,
            strategy_allocation_engine_path=strategy_allocation_engine_json,
            micro_option_candidates_path=micro_option_candidates_json,
            paper_option_ticket_preview_path=paper_option_ticket_preview_json,
            option_whatif_safety_validation_path=option_whatif_safety_validation_json,
            news_limit=news_limit,
            regime_limit=regime_limit,
            mode=mode,
        )
        snapshot_json_reference = write_trading_dashboard_snapshot_json(json_output_path, snapshot) if json_output_path else None

    html_path = write_trading_dashboard_html(
        output,
        snapshot,
        refresh_seconds=refresh_seconds,
        snapshot_json_path=snapshot_json_reference,
        title=title,
    )
    if json_output:
        print(
            json.dumps(
                {
                    "dashboard_html": str(html_path),
                    "snapshot_json": str(snapshot_json_reference) if snapshot_json_reference else None,
                    "status": snapshot.get("status"),
                    "mode": snapshot.get("mode"),
                    "refresh_seconds": refresh_seconds,
                },
                indent=2,
                ensure_ascii=True,
            )
        )
        return snapshot
    print(f"Trading Command Center HTML written: {html_path}")
    if snapshot_json_reference:
        print(f"Dashboard snapshot JSON: {snapshot_json_reference}")
    print(f"Status: {(snapshot.get('status') or {}).get('color')} / reasons {(snapshot.get('status') or {}).get('reasons')}")
    return snapshot


def command_dashboard_action_queue(
    snapshot_json: Path,
    output: Path | None,
    json_output_path: Path | None,
    max_actions: int,
    json_output: bool,
) -> dict[str, Any]:
    from recorder.dashboard_action_queue import (
        build_dashboard_action_queue,
        dashboard_action_queue_markdown,
        write_dashboard_action_queue_json,
        write_dashboard_action_queue_markdown,
    )

    snapshot = json.loads(snapshot_json.expanduser().resolve().read_text(encoding="utf-8"))
    queue = build_dashboard_action_queue(snapshot, max_actions=max_actions)
    written = write_dashboard_action_queue_markdown(output, queue) if output else None
    json_written = write_dashboard_action_queue_json(json_output_path, queue) if json_output_path else None
    if json_output:
        print(json.dumps(queue, indent=2, ensure_ascii=True))
        return queue
    print(_console_safe_text(dashboard_action_queue_markdown(queue)))
    if written:
        print(f"\nAction queue Markdown written: {written}")
    if json_written:
        print(f"Action queue JSON written: {json_written}")
    return queue


def command_dashboard_recovery_plan(
    action_queue_json: Path,
    output: Path | None,
    json_output_path: Path | None,
    max_steps: int,
    json_output: bool,
) -> dict[str, Any]:
    from recorder.dashboard_operations import (
        build_dashboard_recovery_plan,
        dashboard_recovery_plan_markdown,
        write_dashboard_recovery_plan_json,
        write_dashboard_recovery_plan_markdown,
    )

    action_queue = json.loads(action_queue_json.expanduser().resolve().read_text(encoding="utf-8"))
    plan = build_dashboard_recovery_plan(action_queue, max_steps=max_steps, dry_run=True)
    written = write_dashboard_recovery_plan_markdown(output, plan) if output else None
    json_written = write_dashboard_recovery_plan_json(json_output_path, plan) if json_output_path else None
    if json_output:
        print(json.dumps(plan, indent=2, ensure_ascii=True))
        return plan
    print(_console_safe_text(dashboard_recovery_plan_markdown(plan)))
    if written:
        print(f"\nRecovery plan Markdown written: {written}")
    if json_written:
        print(f"Recovery plan JSON written: {json_written}")
    return plan


def command_dashboard_recovery_executor(
    recovery_plan_json: Path,
    output: Path | None,
    json_output_path: Path | None,
    max_steps: int,
    timeout_seconds: float,
    execute: bool,
    allowed_phases: list[str] | None,
    json_output: bool,
) -> dict[str, Any]:
    from recorder.dashboard_operations import (
        DEFAULT_ALLOWED_PHASES,
        dashboard_recovery_executor_markdown,
        run_dashboard_recovery_executor,
        write_dashboard_recovery_executor_json,
        write_dashboard_recovery_executor_markdown,
    )

    recovery_plan = json.loads(recovery_plan_json.expanduser().resolve().read_text(encoding="utf-8"))
    phases = set(allowed_phases or DEFAULT_ALLOWED_PHASES)
    report = run_dashboard_recovery_executor(
        recovery_plan,
        execute=execute,
        max_steps=max_steps,
        allowed_phases=phases,
        timeout_seconds=timeout_seconds,
        cwd=Path.cwd(),
    )
    written = write_dashboard_recovery_executor_markdown(output, report) if output else None
    json_written = write_dashboard_recovery_executor_json(json_output_path, report) if json_output_path else None
    if json_output:
        print(json.dumps(report, indent=2, ensure_ascii=True))
        return report
    print(_console_safe_text(dashboard_recovery_executor_markdown(report)))
    if written:
        print(f"\nRecovery executor Markdown written: {written}")
    if json_written:
        print(f"Recovery executor JSON written: {json_written}")
    return report


def command_dashboard_recovery_post_validation(
    before_snapshot_json: Path | None,
    after_snapshot_json: Path | None,
    before_action_queue_json: Path | None,
    after_action_queue_json: Path | None,
    before_recovery_plan_json: Path | None,
    after_recovery_plan_json: Path | None,
    recovery_executor_json: Path | None,
    output: Path | None,
    json_output_path: Path | None,
    json_output: bool,
) -> dict[str, Any]:
    from recorder.dashboard_operations import (
        build_dashboard_recovery_post_validation_from_paths,
        dashboard_recovery_post_validation_markdown,
        write_dashboard_recovery_post_validation_json,
        write_dashboard_recovery_post_validation_markdown,
    )

    report = build_dashboard_recovery_post_validation_from_paths(
        before_snapshot_path=before_snapshot_json,
        after_snapshot_path=after_snapshot_json,
        before_action_queue_path=before_action_queue_json,
        after_action_queue_path=after_action_queue_json,
        before_recovery_plan_path=before_recovery_plan_json,
        after_recovery_plan_path=after_recovery_plan_json,
        recovery_executor_path=recovery_executor_json,
    )
    written = write_dashboard_recovery_post_validation_markdown(output, report) if output else None
    json_written = write_dashboard_recovery_post_validation_json(json_output_path, report) if json_output_path else None
    if json_output:
        print(json.dumps(report, indent=2, ensure_ascii=True))
        return report
    print(_console_safe_text(dashboard_recovery_post_validation_markdown(report)))
    if written:
        print(f"\nRecovery post-validation Markdown written: {written}")
    if json_written:
        print(f"Recovery post-validation JSON written: {json_written}")
    return report


def command_dashboard_recovery_closed_loop(
    snapshot_json: Path,
    action_queue_json: Path,
    recovery_plan_json: Path,
    profile_json: Path,
    state_json_path: Path | None,
    executor_json_output_path: Path | None,
    output: Path | None,
    json_output_path: Path | None,
    max_steps: int,
    cooldown_seconds: float,
    timeout_seconds: float,
    execute: bool,
    refresh_after: bool,
    allowed_phases: list[str] | None,
    json_output: bool,
) -> dict[str, Any]:
    from recorder.dashboard_operations import (
        dashboard_recovery_closed_loop_markdown,
        run_dashboard_recovery_closed_loop,
        write_dashboard_recovery_closed_loop_json,
        write_dashboard_recovery_closed_loop_markdown,
    )
    from recorder.dashboard_operations import DEFAULT_ALLOWED_PHASES

    phases = set(allowed_phases or DEFAULT_ALLOWED_PHASES)
    report = run_dashboard_recovery_closed_loop(
        snapshot_path=snapshot_json,
        action_queue_path=action_queue_json,
        recovery_plan_path=recovery_plan_json,
        profile_json_path=profile_json,
        state_path=state_json_path,
        output_executor_path=executor_json_output_path,
        execute=execute,
        refresh_after=refresh_after,
        max_steps=max_steps,
        cooldown_seconds=cooldown_seconds,
        timeout_seconds=timeout_seconds,
        allowed_phases=phases,
        cwd=Path.cwd(),
    )
    written = write_dashboard_recovery_closed_loop_markdown(output, report) if output else None
    json_written = write_dashboard_recovery_closed_loop_json(json_output_path, report) if json_output_path else None
    if json_output:
        print(json.dumps(report, indent=2, ensure_ascii=True))
        return report
    print(_console_safe_text(dashboard_recovery_closed_loop_markdown(report)))
    if written:
        print(f"\nRecovery closed-loop Markdown written: {written}")
    if json_written:
        print(f"Recovery closed-loop JSON written: {json_written}")
    return report


def command_dashboard_latency_safety_audit(
    profile_json: Path,
    loop_summary_json: Path | None,
    output: Path | None,
    json_output_path: Path | None,
    json_output: bool,
) -> dict[str, Any]:
    from recorder.dashboard_operations import (
        build_dashboard_latency_safety_audit_from_paths,
        dashboard_latency_safety_audit_markdown,
        write_dashboard_latency_safety_audit_json,
        write_dashboard_latency_safety_audit_markdown,
    )

    report = build_dashboard_latency_safety_audit_from_paths(
        profile_json_path=profile_json,
        loop_summary_json_path=loop_summary_json,
    )
    written = write_dashboard_latency_safety_audit_markdown(output, report) if output else None
    json_written = write_dashboard_latency_safety_audit_json(json_output_path, report) if json_output_path else None
    if json_output:
        print(json.dumps(report, indent=2, ensure_ascii=True))
        return report
    print(_console_safe_text(dashboard_latency_safety_audit_markdown(report)))
    if written:
        print(f"\nLatency safety audit Markdown written: {written}")
    if json_written:
        print(f"Latency safety audit JSON written: {json_written}")
    return report


def command_dashboard_supervisor_handoff_audit(
    profile_json: Path,
    loop_summary_json: Path | None,
    output: Path | None,
    json_output_path: Path | None,
    json_output: bool,
) -> dict[str, Any]:
    from recorder.dashboard_operations import (
        build_dashboard_supervisor_handoff_audit_from_paths,
        dashboard_supervisor_handoff_audit_markdown,
        write_dashboard_supervisor_handoff_audit_json,
        write_dashboard_supervisor_handoff_audit_markdown,
    )

    report = build_dashboard_supervisor_handoff_audit_from_paths(
        profile_json_path=profile_json,
        loop_summary_json_path=loop_summary_json,
    )
    written = write_dashboard_supervisor_handoff_audit_markdown(output, report) if output else None
    json_written = write_dashboard_supervisor_handoff_audit_json(json_output_path, report) if json_output_path else None
    if json_output:
        print(json.dumps(report, indent=2, ensure_ascii=True))
        return report
    print(_console_safe_text(dashboard_supervisor_handoff_audit_markdown(report)))
    if written:
        print(f"\nSupervisor handoff audit Markdown written: {written}")
    if json_written:
        print(f"Supervisor handoff audit JSON written: {json_written}")
    return report


def command_execution_hot_path_latency_audit(
    profile_json: Path,
    loop_summary_json: Path | None,
    output: Path | None,
    json_output_path: Path | None,
    json_output: bool,
) -> dict[str, Any]:
    from recorder.execution_hot_path_latency_audit import (
        build_execution_hot_path_latency_audit_from_paths,
        execution_hot_path_latency_audit_markdown,
        write_execution_hot_path_latency_audit_json,
        write_execution_hot_path_latency_audit_markdown,
    )

    report = build_execution_hot_path_latency_audit_from_paths(
        profile_json_path=profile_json,
        loop_summary_json_path=loop_summary_json,
    )
    written = write_execution_hot_path_latency_audit_markdown(output, report) if output else None
    json_written = write_execution_hot_path_latency_audit_json(json_output_path, report) if json_output_path else None
    if json_output:
        print(json.dumps(report, indent=2, ensure_ascii=True))
        return report
    print(_console_safe_text(execution_hot_path_latency_audit_markdown(report)))
    if written:
        print(f"\nExecution hot-path latency audit Markdown written: {written}")
    if json_written:
        print(f"Execution hot-path latency audit JSON written: {json_written}")
    return report
