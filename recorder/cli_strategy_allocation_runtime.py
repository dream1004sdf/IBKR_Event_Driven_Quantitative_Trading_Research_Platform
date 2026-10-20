from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def command_ticker_candidate_ranking(
    settings: Any,
    database_path: Path | None,
    market_data_json_path: Path | None,
    options_market_structure_json_path: Path | None,
    options_market_structure_supervisor_json_path: Path | None,
    capital_state_json_path: Path | None,
    news_learning_json_path: Path | None,
    lookback_hours: float,
    recency_half_life_hours: float,
    max_news_events: int,
    max_ranked_candidates: int,
    min_total_score: float,
    output: Path | None = None,
    json_output_path: Path | None = None,
    json_output: bool = False,
) -> dict[str, Any]:
    from recorder.ticker_candidate_ranking import (
        build_ticker_candidate_ranking_report,
        ticker_candidate_ranking_markdown,
        write_ticker_candidate_ranking_json,
        write_ticker_candidate_ranking_markdown,
    )

    def read_json(path: Path | None) -> dict[str, Any]:
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

    result = build_ticker_candidate_ranking_report(
        database_path=database_path or settings.database_path,
        market_data_payload=read_json(market_data_json_path),
        options_market_structure_payload=read_json(options_market_structure_json_path),
        options_market_structure_supervisor_payload=read_json(options_market_structure_supervisor_json_path),
        capital_state_payload=read_json(capital_state_json_path),
        news_learning_payload=read_json(news_learning_json_path),
        lookback_hours=lookback_hours,
        recency_half_life_hours=recency_half_life_hours,
        max_news_events=max_news_events,
        max_ranked_candidates=max_ranked_candidates,
        min_total_score=min_total_score,
    )
    written = write_ticker_candidate_ranking_markdown(output, result) if output else None
    json_written = write_ticker_candidate_ranking_json(json_output_path, result) if json_output_path else None
    if json_output:
        print(json.dumps(result, indent=2, ensure_ascii=True))
        return result
    print(ticker_candidate_ranking_markdown(result))
    if written:
        print(f"\nMarkdown report written: {written}")
    if json_written:
        print(f"JSON report written: {json_written}")
    return result


def command_cross_asset_expression_ranking(
    settings: Any,
    ticker_candidate_ranking_json_path: Path,
    database_path: Path | None,
    market_data_json_path: Path | None,
    options_market_structure_json_path: Path | None,
    options_market_structure_supervisor_json_path: Path | None,
    capital_state_json_path: Path | None,
    cross_asset_tuning_json_path: Path | None,
    lookback_hours: float,
    max_scorer_predictions: int,
    max_expressions: int,
    min_expression_score: float,
    output: Path | None = None,
    json_output_path: Path | None = None,
    json_output: bool = False,
) -> dict[str, Any]:
    from recorder.cross_asset_expression_ranking import (
        build_cross_asset_expression_ranking_report,
        cross_asset_expression_ranking_markdown,
        write_cross_asset_expression_ranking_json,
        write_cross_asset_expression_ranking_markdown,
    )

    def read_json(path: Path | None) -> dict[str, Any]:
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

    result = build_cross_asset_expression_ranking_report(
        ticker_candidate_ranking_payload=read_json(ticker_candidate_ranking_json_path),
        database_path=database_path or settings.database_path,
        market_data_payload=read_json(market_data_json_path),
        options_market_structure_payload=read_json(options_market_structure_json_path),
        options_market_structure_supervisor_payload=read_json(options_market_structure_supervisor_json_path),
        capital_state_payload=read_json(capital_state_json_path),
        cross_asset_tuning_payload=read_json(cross_asset_tuning_json_path),
        lookback_hours=lookback_hours,
        max_scorer_predictions=max_scorer_predictions,
        max_expressions=max_expressions,
        min_expression_score=min_expression_score,
    )
    written = write_cross_asset_expression_ranking_markdown(output, result) if output else None
    json_written = write_cross_asset_expression_ranking_json(json_output_path, result) if json_output_path else None
    if json_output:
        print(json.dumps(result, indent=2, ensure_ascii=True))
        return result
    print(cross_asset_expression_ranking_markdown(result))
    if written:
        print(f"\nMarkdown report written: {written}")
    if json_written:
        print(f"JSON report written: {json_written}")
    return result


def command_cross_asset_expression_outcomes(
    settings: Any,
    cross_asset_expression_ranking_json_path: Path,
    database_path: Path | None,
    horizons_minutes: str,
    max_expressions_to_record: int,
    flat_threshold_pct: float,
    max_baseline_age_minutes: float,
    max_outcome_lag_minutes: float,
    min_evaluated_samples: int,
    adjustment_cap: float,
    evaluation_limit: int,
    record_current_snapshot: bool,
    evaluate_pending: bool,
    output: Path | None = None,
    json_output_path: Path | None = None,
    json_output: bool = False,
) -> dict[str, Any]:
    from recorder.cross_asset_expression_outcomes import (
        build_cross_asset_expression_outcomes_report,
        cross_asset_expression_outcomes_markdown,
        write_cross_asset_expression_outcomes_json,
        write_cross_asset_expression_outcomes_markdown,
    )

    def read_json(path: Path | None) -> dict[str, Any]:
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

    result = build_cross_asset_expression_outcomes_report(
        database_path=database_path or settings.database_path,
        cross_asset_expression_ranking_payload=read_json(cross_asset_expression_ranking_json_path),
        horizons_minutes=horizons_minutes,
        max_expressions_to_record=max_expressions_to_record,
        flat_threshold_pct=flat_threshold_pct,
        max_baseline_age_minutes=max_baseline_age_minutes,
        max_outcome_lag_minutes=max_outcome_lag_minutes,
        min_evaluated_samples=min_evaluated_samples,
        adjustment_cap=adjustment_cap,
        evaluation_limit=evaluation_limit,
        record_current_snapshot=record_current_snapshot,
        evaluate_pending=evaluate_pending,
    )
    written = write_cross_asset_expression_outcomes_markdown(output, result) if output else None
    json_written = write_cross_asset_expression_outcomes_json(json_output_path, result) if json_output_path else None
    if json_output:
        print(json.dumps(result, indent=2, ensure_ascii=True))
        return result
    print(cross_asset_expression_outcomes_markdown(result))
    if written:
        print(f"\nMarkdown report written: {written}")
    if json_written:
        print(f"JSON report written: {json_written}")
    return result


def command_ticker_action_candidates(
    settings: Any,
    ticker_candidate_ranking_json_path: Path,
    cross_asset_expression_ranking_json_path: Path | None,
    capital_aware_gate_json_path: Path | None,
    capital_state_json_path: Path | None,
    max_rank: int,
    max_synthetic_candidates: int,
    min_total_score: float,
    min_confidence: str,
    include_existing_symbols: bool,
    allow_neutral_watch_injection: bool,
    default_stock_notional: float,
    min_fractional_stock_cash: float,
    default_option_probe_premium: float,
    output: Path | None = None,
    json_output_path: Path | None = None,
    json_output: bool = False,
) -> dict[str, Any]:
    from recorder.ticker_action_candidate_synthesis import (
        build_ticker_action_candidate_synthesis_report,
        ticker_action_candidate_synthesis_markdown,
        write_ticker_action_candidate_synthesis_json,
        write_ticker_action_candidate_synthesis_markdown,
    )

    def read_json(path: Path | None) -> dict[str, Any]:
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

    result = build_ticker_action_candidate_synthesis_report(
        ticker_candidate_ranking_payload=read_json(ticker_candidate_ranking_json_path),
        cross_asset_expression_ranking_payload=read_json(cross_asset_expression_ranking_json_path),
        capital_aware_gate_payload=read_json(capital_aware_gate_json_path),
        capital_state_payload=read_json(capital_state_json_path),
        max_rank=max_rank,
        max_synthetic_candidates=max_synthetic_candidates,
        min_total_score=min_total_score,
        min_confidence=min_confidence,
        include_existing_symbols=include_existing_symbols,
        allow_neutral_watch_injection=allow_neutral_watch_injection,
        default_stock_notional=default_stock_notional,
        min_fractional_stock_cash=min_fractional_stock_cash,
        default_option_probe_premium=default_option_probe_premium,
    )
    written = write_ticker_action_candidate_synthesis_markdown(output, result) if output else None
    json_written = write_ticker_action_candidate_synthesis_json(json_output_path, result) if json_output_path else None
    if json_output:
        print(json.dumps(result, indent=2, ensure_ascii=True))
        return result
    print(ticker_action_candidate_synthesis_markdown(result))
    if written:
        print(f"\nMarkdown report written: {written}")
    if json_written:
        print(f"JSON report written: {json_written}")
    return result


def command_strategy_allocation_engine(
    settings: Any,
    capital_aware_gate_json_path: Path,
    capital_state_json_path: Path,
    market_data_json_path: Path,
    market_data_risk_gate_json_path: Path,
    options_market_structure_json_path: Path,
    options_market_structure_supervisor_json_path: Path,
    live_scorer_learning_json_path: Path,
    news_learning_json_path: Path,
    ticker_candidate_ranking_json_path: Path | None,
    ticker_action_candidates_json_path: Path | None,
    micro_policy_state_json_path: Path | None,
    database_path: Path | None,
    min_option_budget: float,
    min_spread_budget: float,
    max_recommendations: int,
    latest_news_limit: int,
    output: Path | None = None,
    json_output_path: Path | None = None,
    json_output: bool = False,
) -> dict[str, Any]:
    from recorder.strategy_allocation_engine import (
        build_strategy_allocation_engine_report,
        strategy_allocation_engine_markdown,
        write_strategy_allocation_engine_json,
        write_strategy_allocation_engine_markdown,
    )

    def read_json(path: Path) -> dict[str, Any]:
        resolved = path.expanduser().resolve()
        if not resolved.exists():
            return {}
        try:
            payload = json.loads(resolved.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    result = build_strategy_allocation_engine_report(
        capital_aware_gate_payload=read_json(capital_aware_gate_json_path),
        capital_state_payload=read_json(capital_state_json_path),
        market_data_payload=read_json(market_data_json_path),
        market_data_risk_gate_payload=read_json(market_data_risk_gate_json_path),
        options_market_structure_payload=read_json(options_market_structure_json_path),
        options_market_structure_supervisor_payload=read_json(options_market_structure_supervisor_json_path),
        live_scorer_learning_payload=read_json(live_scorer_learning_json_path),
        news_learning_payload=read_json(news_learning_json_path),
        ticker_candidate_ranking_payload=read_json(ticker_candidate_ranking_json_path) if ticker_candidate_ranking_json_path else {},
        ticker_action_candidate_synthesis_payload=read_json(ticker_action_candidates_json_path) if ticker_action_candidates_json_path else {},
        previous_micro_policy_state_payload=read_json(micro_policy_state_json_path) if micro_policy_state_json_path else {},
        database_path=database_path or settings.database_path,
        min_option_budget=min_option_budget,
        min_spread_budget=min_spread_budget,
        max_recommendations=max_recommendations,
        latest_news_limit=latest_news_limit,
    )
    written = write_strategy_allocation_engine_markdown(output, result) if output else None
    json_written = write_strategy_allocation_engine_json(json_output_path, result) if json_output_path else None
    if json_output:
        print(json.dumps(result, indent=2, ensure_ascii=True))
        return result
    print(strategy_allocation_engine_markdown(result))
    if written:
        print(f"\nMarkdown report written: {written}")
    if json_written:
        print(f"JSON report written: {json_written}")
    return result


def command_micro_option_candidates(
    settings: Any,
    capital_state_json_path: Path,
    strategy_allocation_json_path: Path,
    option_quote_json_path: Path,
    options_market_structure_json_path: Path,
    options_market_structure_supervisor_json_path: Path,
    micro_policy_state_json_path: Path | None,
    symbols: str,
    min_option_budget: float,
    min_spread_budget: float,
    max_quote_age_minutes: float,
    max_spread_pct: float,
    min_volume: float,
    min_open_interest: float,
    min_dte: int,
    max_dte: int,
    max_premium_budget_fraction: float,
    max_candidates: int,
    output: Path | None = None,
    json_output_path: Path | None = None,
    json_output: bool = False,
) -> dict[str, Any]:
    from recorder.micro_option_candidate_selector import (
        build_micro_option_candidate_selector_report,
        micro_option_candidate_selector_markdown,
        write_micro_option_candidate_selector_json,
        write_micro_option_candidate_selector_markdown,
    )

    def read_json(path: Path | None) -> dict[str, Any]:
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

    parsed_symbols = tuple(symbol.strip().upper() for symbol in str(symbols or "").split(",") if symbol.strip())
    result = build_micro_option_candidate_selector_report(
        capital_state_payload=read_json(capital_state_json_path),
        strategy_allocation_payload=read_json(strategy_allocation_json_path),
        option_quote_snapshot_payload=read_json(option_quote_json_path),
        options_market_structure_payload=read_json(options_market_structure_json_path),
        options_market_structure_supervisor_payload=read_json(options_market_structure_supervisor_json_path),
        previous_micro_policy_state_payload=read_json(micro_policy_state_json_path),
        symbols=parsed_symbols,
        min_option_budget=min_option_budget,
        min_spread_budget=min_spread_budget,
        max_quote_age_minutes=max_quote_age_minutes,
        max_spread_pct=max_spread_pct,
        min_volume=min_volume,
        min_open_interest=min_open_interest,
        min_dte=min_dte,
        max_dte=max_dte,
        max_premium_budget_fraction=max_premium_budget_fraction,
        max_candidates=max_candidates,
    )
    written = write_micro_option_candidate_selector_markdown(output, result) if output else None
    json_written = write_micro_option_candidate_selector_json(json_output_path, result) if json_output_path else None
    if json_output:
        print(json.dumps(result, indent=2, ensure_ascii=True))
        return result
    print(micro_option_candidate_selector_markdown(result))
    if written:
        print(f"\nMarkdown report written: {written}")
    if json_written:
        print(f"JSON report written: {json_written}")
    return result


def command_paper_option_ticket_preview(
    settings: Any,
    micro_option_candidates_json_path: Path,
    capital_state_json_path: Path | None,
    max_tickets: int,
    default_quantity: int,
    limit_price_policy: str,
    allow_stale_quotes: bool,
    max_ticket_debit: float,
    output: Path | None = None,
    json_output_path: Path | None = None,
    json_output: bool = False,
) -> dict[str, Any]:
    from recorder.paper_option_ticket_preview import (
        build_paper_option_ticket_preview_report,
        paper_option_ticket_preview_markdown,
        write_paper_option_ticket_preview_json,
        write_paper_option_ticket_preview_markdown,
    )

    def read_json(path: Path | None) -> dict[str, Any]:
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

    result = build_paper_option_ticket_preview_report(
        micro_option_candidates_payload=read_json(micro_option_candidates_json_path),
        capital_state_payload=read_json(capital_state_json_path),
        max_tickets=max_tickets,
        default_quantity=default_quantity,
        limit_price_policy=limit_price_policy,
        require_fresh_quote=not bool(allow_stale_quotes),
        max_ticket_debit=max_ticket_debit,
    )
    written = write_paper_option_ticket_preview_markdown(output, result) if output else None
    json_written = write_paper_option_ticket_preview_json(json_output_path, result) if json_output_path else None
    if json_output:
        print(json.dumps(result, indent=2, ensure_ascii=True))
        return result
    print(paper_option_ticket_preview_markdown(result))
    if written:
        print(f"\nMarkdown report written: {written}")
    if json_written:
        print(f"JSON report written: {json_written}")
    return result


def command_option_whatif_safety_validation(
    settings: Any,
    paper_option_ticket_preview_json_path: Path,
    capital_state_json_path: Path | None,
    option_quote_json_path: Path | None,
    options_market_structure_supervisor_json_path: Path | None,
    broker_whatif_json_path: Path | None,
    allow_missing_broker_whatif: bool,
    execute_whatif_refresh: bool,
    max_whatif_tickets: int,
    whatif_timeout_seconds: float,
    whatif_client_id: int | None,
    max_spread_pct: float,
    max_quote_age_minutes: float,
    allow_stale_quotes: bool,
    output: Path | None = None,
    json_output_path: Path | None = None,
    json_output: bool = False,
) -> dict[str, Any]:
    from recorder.option_whatif_safety_validation import (
        OptionTicketWhatIfConfig,
        build_option_whatif_safety_validation_report,
        option_whatif_safety_validation_markdown,
        write_option_whatif_safety_validation_json,
        write_option_whatif_safety_validation_markdown,
    )

    def read_json(path: Path | None) -> dict[str, Any]:
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

    whatif_config = OptionTicketWhatIfConfig(
        host=settings.host,
        port=settings.port,
        client_id=int(whatif_client_id if whatif_client_id is not None else settings.client_id + 9700),
        timeout_seconds=float(whatif_timeout_seconds),
        max_tickets=int(max_whatif_tickets),
    )
    result = build_option_whatif_safety_validation_report(
        paper_option_ticket_preview_payload=read_json(paper_option_ticket_preview_json_path),
        capital_state_payload=read_json(capital_state_json_path),
        option_quote_snapshot_payload=read_json(option_quote_json_path),
        options_market_structure_supervisor_payload=read_json(options_market_structure_supervisor_json_path),
        broker_whatif_payload=read_json(broker_whatif_json_path),
        require_broker_whatif=not bool(allow_missing_broker_whatif),
        max_spread_pct=max_spread_pct,
        max_quote_age_minutes=max_quote_age_minutes,
        require_fresh_quote=not bool(allow_stale_quotes),
        execute_whatif_refresh=bool(execute_whatif_refresh),
        whatif_config=whatif_config,
    )
    written = write_option_whatif_safety_validation_markdown(output, result) if output else None
    json_written = write_option_whatif_safety_validation_json(json_output_path, result) if json_output_path else None
    if json_output:
        print(json.dumps(result, indent=2, ensure_ascii=True))
        return result
    print(option_whatif_safety_validation_markdown(result))
    if written:
        print(f"\nMarkdown report written: {written}")
    if json_written:
        print(f"JSON report written: {json_written}")
    return result


def command_limited_auto_trading_readiness(
    settings: Any,
    capital_state_json_path: Path | None,
    capital_pipeline_orchestrator_json_path: Path | None,
    strategy_allocation_json_path: Path | None,
    live_read_only_capital_validation_json_path: Path | None,
    micro_option_candidates_json_path: Path | None,
    paper_option_ticket_preview_json_path: Path | None,
    option_whatif_safety_validation_json_path: Path | None,
    market_data_json_path: Path | None,
    market_data_risk_gate_json_path: Path | None,
    options_market_structure_supervisor_json_path: Path | None,
    live_scorer_learning_json_path: Path | None,
    threshold_candidate_evaluator_json_path: Path | None,
    evidence_freshness_json_path: Path | None,
    source_online_supervisor_json_path: Path | None,
    execution_safety_cache_supervisor_json_path: Path | None,
    max_artifact_age_minutes: float,
    output: Path | None = None,
    json_output_path: Path | None = None,
    json_output: bool = False,
) -> dict[str, Any]:
    from recorder.limited_auto_trading_readiness import (
        build_limited_auto_trading_readiness_report,
        limited_auto_trading_readiness_markdown,
        write_limited_auto_trading_readiness_json,
        write_limited_auto_trading_readiness_markdown,
    )

    def read_json(path: Path | None) -> dict[str, Any]:
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

    result = build_limited_auto_trading_readiness_report(
        capital_state_payload=read_json(capital_state_json_path),
        capital_pipeline_orchestrator_payload=read_json(capital_pipeline_orchestrator_json_path),
        strategy_allocation_payload=read_json(strategy_allocation_json_path),
        live_read_only_capital_validation_payload=read_json(live_read_only_capital_validation_json_path),
        micro_option_candidates_payload=read_json(micro_option_candidates_json_path),
        paper_option_ticket_preview_payload=read_json(paper_option_ticket_preview_json_path),
        option_whatif_safety_validation_payload=read_json(option_whatif_safety_validation_json_path),
        market_data_payload=read_json(market_data_json_path),
        market_data_risk_gate_payload=read_json(market_data_risk_gate_json_path),
        options_market_structure_supervisor_payload=read_json(options_market_structure_supervisor_json_path),
        live_scorer_learning_payload=read_json(live_scorer_learning_json_path),
        threshold_candidate_evaluator_payload=read_json(threshold_candidate_evaluator_json_path),
        evidence_freshness_payload=read_json(evidence_freshness_json_path),
        source_online_supervisor_payload=read_json(source_online_supervisor_json_path),
        execution_safety_cache_supervisor_payload=read_json(execution_safety_cache_supervisor_json_path),
        max_artifact_age_minutes=max_artifact_age_minutes,
    )
    written = write_limited_auto_trading_readiness_markdown(output, result) if output else None
    json_written = write_limited_auto_trading_readiness_json(json_output_path, result) if json_output_path else None
    if json_output:
        print(json.dumps(result, indent=2, ensure_ascii=True))
        return result
    print(limited_auto_trading_readiness_markdown(result))
    if written:
        print(f"\nMarkdown report written: {written}")
    if json_written:
        print(f"JSON report written: {json_written}")
    return result


def command_gate_severity_outcomes(
    settings: Any,
    database_path: Path | None,
    limited_auto_readiness_json_path: Path | None,
    paper_capital_execution_json_path: Path | None,
    horizons_minutes: str,
    max_outcome_lag_minutes: float,
    evaluation_limit: int,
    record_current_snapshot: bool,
    evaluate_pending: bool,
    output: Path | None = None,
    json_output_path: Path | None = None,
    json_output: bool = False,
) -> dict[str, Any]:
    from recorder.gate_severity_outcomes import (
        build_gate_severity_outcome_report,
        gate_severity_outcome_markdown,
        write_gate_severity_outcome_json,
        write_gate_severity_outcome_markdown,
    )

    def read_json(path: Path | None) -> dict[str, Any]:
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

    result = build_gate_severity_outcome_report(
        database_path=database_path or settings.database_path,
        limited_auto_readiness_payload=read_json(limited_auto_readiness_json_path),
        paper_capital_execution_payload=read_json(paper_capital_execution_json_path),
        horizons_minutes=horizons_minutes,
        record_current_snapshot=record_current_snapshot,
        evaluate_pending=evaluate_pending,
        max_outcome_lag_minutes=max_outcome_lag_minutes,
        evaluation_limit=evaluation_limit,
    )
    written = write_gate_severity_outcome_markdown(output, result) if output else None
    json_written = write_gate_severity_outcome_json(json_output_path, result) if json_output_path else None
    if json_output:
        print(json.dumps(result, indent=2, ensure_ascii=True))
        return result
    print(gate_severity_outcome_markdown(result))
    if written:
        print(f"\nMarkdown report written: {written}")
    if json_written:
        print(f"JSON report written: {json_written}")
    return result
