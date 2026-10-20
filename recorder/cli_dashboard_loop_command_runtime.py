from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .cli_profile_utils import (
    coalesce_profile_path as _coalesce_profile_path,
    coalesce_profile_value as _coalesce_profile_value,
    load_profile_json as _load_profile_json,
)
from .cli_dashboard_loop_helpers import (
    _console_safe_text,
    _run_singleton,
    _run_watch_evidence_refresh_watchdog,
    _run_dashboard_source_ingest_health,
    _run_dashboard_market_data_session_overlay,
    _run_dashboard_market_data_risk_gate,
    _run_dashboard_session_transition_closed_loop,
    _run_dashboard_session_transition_release_governor,
    _run_dashboard_release_governed_recovery_closed_loop,
)


def _build_dashboard_pre_snapshot_hook(profile: dict[str, Any], profile_base: Path | None):
    watchdog_profile = profile.get("evidence_refresh_watchdog") if isinstance(profile.get("evidence_refresh_watchdog"), dict) else {}
    market_data_session_overlay_profile = profile.get("market_data_session_overlay") if isinstance(profile.get("market_data_session_overlay"), dict) else {}
    market_data_gate_profile = profile.get("market_data_risk_gate") if isinstance(profile.get("market_data_risk_gate"), dict) else {}
    session_transition_closed_loop_profile = profile.get("market_data_session_transition_closed_loop") if isinstance(profile.get("market_data_session_transition_closed_loop"), dict) else {}
    session_transition_release_governor_profile = profile.get("session_transition_release_governor") if isinstance(profile.get("session_transition_release_governor"), dict) else {}
    release_governed_recovery_closed_loop_profile = profile.get("release_governed_recovery_closed_loop") if isinstance(profile.get("release_governed_recovery_closed_loop"), dict) else {}
    if not (
        (bool(watchdog_profile.get("enabled", False)) and not bool(watchdog_profile.get("ignore", False)))
        or (bool(market_data_gate_profile.get("enabled", False)) and not bool(market_data_gate_profile.get("ignore", False)))
        or (bool(session_transition_closed_loop_profile.get("enabled", False)) and not bool(session_transition_closed_loop_profile.get("ignore", False)))
        or (bool(session_transition_release_governor_profile.get("enabled", False)) and not bool(session_transition_release_governor_profile.get("ignore", False)))
        or (bool(release_governed_recovery_closed_loop_profile.get("enabled", False)) and not bool(release_governed_recovery_closed_loop_profile.get("ignore", False)))
    ):
        return None

    def pre_snapshot_hook(tick_index: int, loop_run_id: str) -> dict[str, Any]:
        hook_run_id = f"{loop_run_id}_dashboard_tick_{tick_index:02d}"
        summary: dict[str, Any] = {"enabled": True, "applied": True, "hook": "dashboard_pre_snapshot"}
        if bool(watchdog_profile.get("enabled", False)) and not bool(watchdog_profile.get("ignore", False)):
            _, evidence_summary = _run_watch_evidence_refresh_watchdog(profile=profile, profile_base=profile_base, run_id=hook_run_id)
            evidence_summary["hook"] = "evidence_refresh_watchdog"
            evidence_summary["source_ingest_health"] = _run_dashboard_source_ingest_health(profile=profile, profile_base=profile_base, run_id=hook_run_id)
            summary["evidence_refresh_watchdog"] = evidence_summary
        if bool(market_data_session_overlay_profile.get("enabled", False)) and not bool(market_data_session_overlay_profile.get("ignore", False)):
            summary["market_data_session_overlay"] = _run_dashboard_market_data_session_overlay(profile=profile, profile_base=profile_base, run_id=hook_run_id)
        summary["market_data_risk_gate"] = _run_dashboard_market_data_risk_gate(profile=profile, profile_base=profile_base, run_id=hook_run_id)
        if bool(session_transition_closed_loop_profile.get("enabled", False)) and not bool(session_transition_closed_loop_profile.get("ignore", False)):
            summary["market_data_session_transition_closed_loop"] = _run_dashboard_session_transition_closed_loop(profile=profile, profile_base=profile_base, run_id=hook_run_id)
        if bool(session_transition_release_governor_profile.get("enabled", False)) and not bool(session_transition_release_governor_profile.get("ignore", False)):
            summary["session_transition_release_governor"] = _run_dashboard_session_transition_release_governor(
                profile=profile,
                profile_base=profile_base,
                run_id=hook_run_id,
                closed_loop_summary=summary.get("market_data_session_transition_closed_loop"),
            )
        if bool(release_governed_recovery_closed_loop_profile.get("enabled", False)) and not bool(release_governed_recovery_closed_loop_profile.get("ignore", False)):
            summary["release_governed_recovery_closed_loop"] = _run_dashboard_release_governed_recovery_closed_loop(
                profile=profile,
                profile_base=profile_base,
                run_id=hook_run_id,
                closed_loop_summary=summary.get("market_data_session_transition_closed_loop"),
                release_governor_summary=summary.get("session_transition_release_governor"),
            )
        return summary

    return pre_snapshot_hook


def _emit_trading_dashboard_loop_result(
    *,
    result: dict[str, Any],
    summary_markdown_output_path: Path | None,
    summary_json_output_path: Path | None,
    json_output: bool,
) -> dict[str, Any]:
    from recorder.trading_dashboard import trading_dashboard_loop_markdown, write_trading_dashboard_loop_markdown

    if result.get("status") == "skipped_already_running":
        if json_output:
            print(json.dumps(result, indent=2, ensure_ascii=True))
        else:
            print(f"{result.get('command')} skipped: {result.get('reason')} ({(result.get('singleton') or {}).get('message')})")
        return result
    written_summary = write_trading_dashboard_loop_markdown(summary_markdown_output_path, result) if summary_markdown_output_path else None
    if json_output:
        print(json.dumps(result, indent=2, ensure_ascii=True))
        return result
    print(_console_safe_text(trading_dashboard_loop_markdown(result)))
    if written_summary:
        print(f"\nLoop summary Markdown written: {written_summary}")
    if summary_json_output_path:
        print(f"Loop summary JSON written: {summary_json_output_path.expanduser().resolve()}")
    return result


def command_trading_dashboard_loop(
    settings,
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
    snapshot_json_output_path: Path | None,
    markdown_output_path: Path | None,
    html_output_path: Path | None,
    tick_log_jsonl_path: Path | None,
    alert_state_json_path: Path | None,
    alert_events_jsonl_path: Path | None,
    telegram_delivery_jsonl_path: Path | None,
    summary_json_output_path: Path | None,
    summary_markdown_output_path: Path | None,
    action_queue_json_output_path: Path | None,
    action_queue_markdown_output_path: Path | None,
    recovery_plan_json_output_path: Path | None,
    recovery_plan_markdown_output_path: Path | None,
    recovery_executor_json_output_path: Path | None,
    recovery_executor_markdown_output_path: Path | None,
    recovery_post_validation_json_output_path: Path | None,
    recovery_post_validation_markdown_output_path: Path | None,
    refresh_seconds: int | None,
    title: str | None,
    max_ticks: int | None,
    interval_seconds: float | None,
    sleep_between_ticks: bool | None,
    disable_alerts: bool,
    emit_resolution_alerts: bool | None,
    alert_buying_power_threshold: float | None,
    alert_available_funds_threshold: float | None,
    alert_worsening_min_delta: float | None,
    alert_worsening_relative: float | None,
    disable_telegram: bool,
    telegram_bot_token: str | None,
    telegram_chat_id: str | None,
    telegram_bot_token_env: str | None,
    telegram_chat_id_env: str | None,
    telegram_dry_run: bool | None,
    telegram_timeout_seconds: float | None,
    tick_log_max_bytes: int | None,
    tick_log_backups: int | None,
    run_id: str | None,
    json_output: bool,
) -> dict[str, Any]:
    from recorder.trading_dashboard import run_trading_dashboard_loop

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
    market_data_operating_auto_gate_json = _coalesce_profile_path(market_data_operating_auto_gate_json, profile, "paths", "market_data_operating_auto_gate_json", profile_base) or _coalesce_profile_path(None, profile, "outputs", "market_data_operating_auto_gate_json_output_path", profile_base)
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
    market_data_session_transition_gate_json = _coalesce_profile_path(market_data_session_transition_gate_json, profile, "paths", "market_data_session_transition_gate_json", profile_base)
    session_transition_release_governor_json = _coalesce_profile_path(session_transition_release_governor_json, profile, "paths", "session_transition_release_governor_json", profile_base)
    market_data_operating_execution_intent_ledger_json = _coalesce_profile_path(market_data_operating_execution_intent_ledger_json, profile, "paths", "market_data_operating_execution_intent_ledger_json", profile_base) or _coalesce_profile_path(None, profile, "outputs", "market_data_operating_execution_intent_ledger_json_output_path", profile_base)
    operating_defensive_action_plan_json = _coalesce_profile_path(operating_defensive_action_plan_json, profile, "paths", "operating_defensive_action_plan_json", profile_base)
    operating_defensive_intent_ledger_json = _coalesce_profile_path(operating_defensive_intent_ledger_json, profile, "paths", "operating_defensive_intent_ledger_json", profile_base)
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
    snapshot_json_output_path = _coalesce_profile_path(snapshot_json_output_path, profile, "outputs", "json_output_path", profile_base) or Path("exports/m143_trading_dashboard_snapshot.json")
    markdown_output_path = _coalesce_profile_path(markdown_output_path, profile, "outputs", "output", profile_base)
    html_output_path = _coalesce_profile_path(html_output_path, profile, "outputs", "html_output_path", profile_base) or Path("exports/trading_command_center.html")
    tick_log_jsonl_path = _coalesce_profile_path(tick_log_jsonl_path, profile, "outputs", "loop_tick_log_jsonl_path", profile_base) or Path("exports/m145_trading_dashboard_loop_ticks.jsonl")
    alert_state_json_path = _coalesce_profile_path(alert_state_json_path, profile, "outputs", "alert_state_json_path", profile_base) or Path("exports/m146_trading_dashboard_alert_state.json")
    alert_events_jsonl_path = _coalesce_profile_path(alert_events_jsonl_path, profile, "outputs", "alert_events_jsonl_path", profile_base) or Path("exports/m146_trading_dashboard_alert_events.jsonl")
    telegram_delivery_jsonl_path = _coalesce_profile_path(telegram_delivery_jsonl_path, profile, "outputs", "telegram_delivery_jsonl_path", profile_base) or Path("exports/m147_trading_dashboard_telegram_deliveries.jsonl")
    summary_json_output_path = _coalesce_profile_path(summary_json_output_path, profile, "outputs", "loop_summary_json_output_path", profile_base) or Path("exports/m145_trading_dashboard_loop_summary.json")
    summary_markdown_output_path = _coalesce_profile_path(summary_markdown_output_path, profile, "outputs", "loop_summary_markdown_output_path", profile_base) or Path("exports/m145_trading_dashboard_loop_summary.md")
    action_queue_json_output_path = _coalesce_profile_path(action_queue_json_output_path, profile, "outputs", "action_queue_json_output_path", profile_base) or Path("exports/m178_dashboard_action_queue_latest.json")
    action_queue_markdown_output_path = _coalesce_profile_path(action_queue_markdown_output_path, profile, "outputs", "action_queue_markdown_output_path", profile_base) or Path("exports/m178_dashboard_action_queue_latest.md")
    recovery_plan_json_output_path = _coalesce_profile_path(recovery_plan_json_output_path, profile, "outputs", "recovery_plan_json_output_path", profile_base) or Path("exports/m178_dashboard_recovery_plan_latest.json")
    recovery_plan_markdown_output_path = _coalesce_profile_path(recovery_plan_markdown_output_path, profile, "outputs", "recovery_plan_markdown_output_path", profile_base) or Path("exports/m178_dashboard_recovery_plan_latest.md")
    recovery_executor_json_output_path = _coalesce_profile_path(recovery_executor_json_output_path, profile, "outputs", "recovery_executor_json_output_path", profile_base) or Path("exports/m178_dashboard_recovery_executor_latest.json")
    recovery_executor_markdown_output_path = _coalesce_profile_path(recovery_executor_markdown_output_path, profile, "outputs", "recovery_executor_markdown_output_path", profile_base) or Path("exports/m178_dashboard_recovery_executor_latest.md")
    recovery_post_validation_json_output_path = _coalesce_profile_path(recovery_post_validation_json_output_path, profile, "outputs", "recovery_post_validation_json_output_path", profile_base) or Path("exports/m179_dashboard_recovery_post_validation_latest.json")
    recovery_post_validation_markdown_output_path = _coalesce_profile_path(recovery_post_validation_markdown_output_path, profile, "outputs", "recovery_post_validation_markdown_output_path", profile_base) or Path("exports/m179_dashboard_recovery_post_validation_latest.md")
    news_limit = int(_coalesce_profile_value(news_limit, profile, "dashboard", "news_limit", news_limit))
    regime_limit = int(_coalesce_profile_value(regime_limit, profile, "dashboard", "regime_limit", regime_limit))
    mode = str(_coalesce_profile_value(mode, profile, "dashboard", "mode", mode))
    refresh_seconds = int(_coalesce_profile_value(refresh_seconds, profile, "dashboard", "refresh_seconds", 1))
    title = str(_coalesce_profile_value(title, profile, "dashboard", "title", "Trading Command Center"))
    max_ticks = int(_coalesce_profile_value(max_ticks, profile, "dashboard_loop", "max_ticks", 1))
    interval_seconds = float(_coalesce_profile_value(interval_seconds, profile, "dashboard_loop", "interval_seconds", 1.0))
    sleep_between_ticks = bool(_coalesce_profile_value(sleep_between_ticks, profile, "dashboard_loop", "sleep_between_ticks", False))
    alerts_profile_enabled = bool(_coalesce_profile_value(None, profile, "alerts", "enabled", True))
    alerts_enabled = alerts_profile_enabled and not bool(disable_alerts)
    emit_resolution_alerts = bool(_coalesce_profile_value(emit_resolution_alerts, profile, "alerts", "emit_resolution_alerts", False))
    alert_buying_power_threshold = float(_coalesce_profile_value(alert_buying_power_threshold, profile, "alerts", "buying_power_threshold", 100.0))
    alert_available_funds_threshold = float(_coalesce_profile_value(alert_available_funds_threshold, profile, "alerts", "available_funds_threshold", 100.0))
    alert_worsening_min_delta = float(_coalesce_profile_value(alert_worsening_min_delta, profile, "alerts", "worsening_min_delta", 1.0))
    alert_worsening_relative = float(_coalesce_profile_value(alert_worsening_relative, profile, "alerts", "worsening_relative", 0.10))
    telegram_profile_enabled = bool(_coalesce_profile_value(None, profile, "telegram", "enabled", False))
    telegram_enabled = telegram_profile_enabled and not bool(disable_telegram)
    telegram_bot_token_env = str(_coalesce_profile_value(telegram_bot_token_env, profile, "telegram", "bot_token_env", "TELEGRAM_BOT_TOKEN") or "")
    telegram_chat_id_env = str(_coalesce_profile_value(telegram_chat_id_env, profile, "telegram", "chat_id_env", "TELEGRAM_CHAT_ID") or "")
    telegram_bot_token = telegram_bot_token or (os.environ.get(telegram_bot_token_env) if telegram_bot_token_env else None)
    telegram_chat_id = telegram_chat_id or (os.environ.get(telegram_chat_id_env) if telegram_chat_id_env else None)
    telegram_dry_run = bool(_coalesce_profile_value(telegram_dry_run, profile, "telegram", "dry_run", False))
    telegram_timeout_seconds = float(_coalesce_profile_value(telegram_timeout_seconds, profile, "telegram", "timeout_seconds", 10.0))
    tick_log_max_bytes = int(_coalesce_profile_value(tick_log_max_bytes, profile, "outputs", "loop_tick_log_max_bytes", 128 * 1024 * 1024))
    tick_log_backups = int(_coalesce_profile_value(tick_log_backups, profile, "outputs", "loop_tick_log_backups", 5))
    run_id = str(_coalesce_profile_value(run_id, profile, "dashboard_loop", "run_id", "") or "") or None
    pre_snapshot_hook = _build_dashboard_pre_snapshot_hook(profile, profile_base)

    result = _run_singleton(
        "trading-dashboard-loop",
        lambda: run_trading_dashboard_loop(
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
            snapshot_json_output_path=snapshot_json_output_path,
            markdown_output_path=markdown_output_path,
            html_output_path=html_output_path,
            tick_log_jsonl_path=tick_log_jsonl_path,
            alert_state_json_path=alert_state_json_path,
            alert_events_jsonl_path=alert_events_jsonl_path,
            telegram_delivery_jsonl_path=telegram_delivery_jsonl_path,
            summary_json_output_path=summary_json_output_path,
            action_queue_json_output_path=action_queue_json_output_path,
            action_queue_markdown_output_path=action_queue_markdown_output_path,
            recovery_plan_json_output_path=recovery_plan_json_output_path,
            recovery_plan_markdown_output_path=recovery_plan_markdown_output_path,
            recovery_executor_json_output_path=recovery_executor_json_output_path,
            recovery_executor_markdown_output_path=recovery_executor_markdown_output_path,
            recovery_post_validation_json_output_path=recovery_post_validation_json_output_path,
            recovery_post_validation_markdown_output_path=recovery_post_validation_markdown_output_path,
            news_limit=news_limit,
            regime_limit=regime_limit,
            mode=mode,
            refresh_seconds=refresh_seconds,
            title=title,
            max_ticks=max_ticks,
            interval_seconds=interval_seconds,
            sleep_between_ticks=sleep_between_ticks,
            alerts_enabled=alerts_enabled,
            emit_resolution_alerts=emit_resolution_alerts,
            alert_buying_power_threshold=alert_buying_power_threshold,
            alert_available_funds_threshold=alert_available_funds_threshold,
            alert_worsening_min_delta=alert_worsening_min_delta,
            alert_worsening_relative=alert_worsening_relative,
            telegram_enabled=telegram_enabled,
            telegram_bot_token=telegram_bot_token,
            telegram_chat_id=telegram_chat_id,
            telegram_dry_run=telegram_dry_run,
            telegram_timeout_seconds=telegram_timeout_seconds,
            tick_log_max_bytes=tick_log_max_bytes,
            tick_log_backups=tick_log_backups,
            pre_snapshot_hook=pre_snapshot_hook,
            run_id=run_id,
        ),
    )
    return _emit_trading_dashboard_loop_result(
        result=result,
        summary_markdown_output_path=summary_markdown_output_path,
        summary_json_output_path=summary_json_output_path,
        json_output=json_output,
    )
