from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _read_json_if_exists(path: Path | None) -> dict[str, Any]:
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


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def command_capital_margin_state(
    settings: Any,
    account_snapshot_json: Path,
    previous_state_json: Path | None,
    refresh_account_snapshot: bool,
    account: str | None,
    account_tags: str,
    account_timeout_seconds: float,
    account_client_id: int | None,
    include_positions: bool,
    include_open_orders: bool,
    max_snapshot_age_minutes: int,
    reserve_fraction: float,
    core_risk_fraction: float,
    options_premium_fraction: float,
    hedge_fraction: float,
    single_position_fraction: float,
    new_margin_fraction: float,
    min_cash_flow: float,
    min_cash_flow_fraction: float,
    minimum_operating_net_liq: float,
    minimum_cash_floor: float,
    minimum_available_funds_ratio: float,
    minimum_excess_liquidity_ratio: float,
    maximum_maintenance_margin_ratio: float,
    maximum_initial_margin_ratio: float,
    minimum_options_premium_budget: float,
    minimum_core_risk_budget: float,
    micro_cash_budget_enabled: bool,
    micro_cash_net_liq_threshold: float,
    micro_cash_option_cash_fraction: float,
    micro_cash_option_net_liq_fraction: float,
    micro_cash_option_budget_cap: float,
    output: Path | None,
    json_output_path: Path | None,
    json_output: bool,
) -> None:
    from recorder.capital_margin_state import (
        build_capital_margin_state,
        capital_margin_state_markdown,
        write_capital_margin_state_json,
        write_capital_margin_state_markdown,
    )

    if refresh_account_snapshot:
        from recorder.ibkr_account import AccountSnapshotConfig, run_ibkr_account_snapshot, write_account_snapshot_json

        account_config = AccountSnapshotConfig(
            host=settings.host,
            port=settings.port,
            client_id=int(account_client_id if account_client_id is not None else settings.client_id + 8000),
            account=account,
            tags=account_tags,
            timeout_seconds=account_timeout_seconds,
            include_positions=include_positions,
            include_open_orders=include_open_orders,
        )
        write_account_snapshot_json(account_snapshot_json, run_ibkr_account_snapshot(account_config))

    report = build_capital_margin_state(
        account_snapshot_path=account_snapshot_json,
        previous_state_path=previous_state_json,
        max_snapshot_age_minutes=max_snapshot_age_minutes,
        reserve_fraction=reserve_fraction,
        core_risk_fraction=core_risk_fraction,
        options_premium_fraction=options_premium_fraction,
        hedge_fraction=hedge_fraction,
        single_position_fraction=single_position_fraction,
        new_margin_fraction=new_margin_fraction,
        min_cash_flow=min_cash_flow,
        min_cash_flow_fraction=min_cash_flow_fraction,
        minimum_operating_net_liq=minimum_operating_net_liq,
        minimum_cash_floor=minimum_cash_floor,
        minimum_available_funds_ratio=minimum_available_funds_ratio,
        minimum_excess_liquidity_ratio=minimum_excess_liquidity_ratio,
        maximum_maintenance_margin_ratio=maximum_maintenance_margin_ratio,
        maximum_initial_margin_ratio=maximum_initial_margin_ratio,
        minimum_options_premium_budget=minimum_options_premium_budget,
        minimum_core_risk_budget=minimum_core_risk_budget,
        micro_cash_budget_enabled=micro_cash_budget_enabled,
        micro_cash_net_liq_threshold=micro_cash_net_liq_threshold,
        micro_cash_option_cash_fraction=micro_cash_option_cash_fraction,
        micro_cash_option_net_liq_fraction=micro_cash_option_net_liq_fraction,
        micro_cash_option_budget_cap=micro_cash_option_budget_cap,
    )
    if refresh_account_snapshot:
        report.setdefault("safety", {})["broker_connection_opened_by_m237"] = True
        report.setdefault("safety", {})["source"] = "fresh_ibkr_account_snapshot_then_file"
        report.setdefault("safety", {})["account_snapshot_refreshed_by_m238"] = True
    if json_output_path:
        write_capital_margin_state_json(json_output_path, report)
    if output:
        write_capital_margin_state_markdown(output, report)
    if json_output:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(capital_margin_state_markdown(report))


def command_live_read_only_capital_validation(
    settings: Any,
    capital_state_json: Path,
    capital_aware_gate_json: Path,
    broker_whatif_json: Path | None,
    micro_policy_state_json: Path | None,
    account_snapshot_json: Path,
    previous_state_json: Path | None,
    refresh_account_snapshot: bool,
    account: str | None,
    account_tags: str,
    account_timeout_seconds: float,
    account_client_id: int | None,
    include_positions: bool,
    include_open_orders: bool,
    max_snapshot_age_minutes: int,
    reserve_fraction: float,
    core_risk_fraction: float,
    options_premium_fraction: float,
    hedge_fraction: float,
    single_position_fraction: float,
    new_margin_fraction: float,
    min_cash_flow: float,
    min_cash_flow_fraction: float,
    minimum_operating_net_liq: float,
    minimum_cash_floor: float,
    minimum_available_funds_ratio: float,
    minimum_excess_liquidity_ratio: float,
    maximum_maintenance_margin_ratio: float,
    maximum_initial_margin_ratio: float,
    minimum_options_premium_budget: float,
    minimum_core_risk_budget: float,
    micro_cash_budget_enabled: bool,
    micro_cash_net_liq_threshold: float,
    micro_cash_option_cash_fraction: float,
    micro_cash_option_net_liq_fraction: float,
    micro_cash_option_budget_cap: float,
    allow_fractional_shares: bool,
    stock_quantity_increment: float,
    max_items: int,
    sample_limit: int,
    output: Path | None,
    json_output_path: Path | None,
    json_output: bool,
) -> None:
    from recorder.capital_margin_state import build_capital_margin_state, write_capital_margin_state_json
    from recorder.live_read_only_capital_validation import (
        build_live_read_only_capital_validation_report,
        live_read_only_capital_validation_markdown,
        write_live_read_only_capital_validation_json,
        write_live_read_only_capital_validation_markdown,
    )

    if refresh_account_snapshot:
        from recorder.ibkr_account import AccountSnapshotConfig, run_ibkr_account_snapshot, write_account_snapshot_json

        account_config = AccountSnapshotConfig(
            host=settings.host,
            port=settings.port,
            client_id=int(account_client_id if account_client_id is not None else settings.client_id + 8200),
            account=account,
            tags=account_tags,
            timeout_seconds=account_timeout_seconds,
            include_positions=include_positions,
            include_open_orders=include_open_orders,
        )
        write_account_snapshot_json(account_snapshot_json, run_ibkr_account_snapshot(account_config))

    capital_state_payload = _read_json_if_exists(capital_state_json)
    if refresh_account_snapshot or not capital_state_payload:
        capital_state_payload = build_capital_margin_state(
            account_snapshot_path=account_snapshot_json,
            previous_state_path=previous_state_json,
            max_snapshot_age_minutes=max_snapshot_age_minutes,
            reserve_fraction=reserve_fraction,
            core_risk_fraction=core_risk_fraction,
            options_premium_fraction=options_premium_fraction,
            hedge_fraction=hedge_fraction,
            single_position_fraction=single_position_fraction,
            new_margin_fraction=new_margin_fraction,
            min_cash_flow=min_cash_flow,
            min_cash_flow_fraction=min_cash_flow_fraction,
            minimum_operating_net_liq=minimum_operating_net_liq,
            minimum_cash_floor=minimum_cash_floor,
            minimum_available_funds_ratio=minimum_available_funds_ratio,
            minimum_excess_liquidity_ratio=minimum_excess_liquidity_ratio,
            maximum_maintenance_margin_ratio=maximum_maintenance_margin_ratio,
            maximum_initial_margin_ratio=maximum_initial_margin_ratio,
            minimum_options_premium_budget=minimum_options_premium_budget,
            minimum_core_risk_budget=minimum_core_risk_budget,
            micro_cash_budget_enabled=micro_cash_budget_enabled,
            micro_cash_net_liq_threshold=micro_cash_net_liq_threshold,
            micro_cash_option_cash_fraction=micro_cash_option_cash_fraction,
            micro_cash_option_net_liq_fraction=micro_cash_option_net_liq_fraction,
            micro_cash_option_budget_cap=micro_cash_option_budget_cap,
        )
        if refresh_account_snapshot:
            capital_state_payload.setdefault("safety", {})["broker_connection_opened_by_m243"] = True
            capital_state_payload.setdefault("safety", {})["source"] = "fresh_ibkr_account_snapshot_then_m243_validation"
        write_capital_margin_state_json(capital_state_json, capital_state_payload)

    report = build_live_read_only_capital_validation_report(
        capital_state_payload=capital_state_payload,
        capital_aware_gate_payload=_read_json_if_exists(capital_aware_gate_json),
        broker_whatif_payload=_read_json_if_exists(broker_whatif_json),
        allow_fractional_shares=allow_fractional_shares,
        stock_quantity_increment=stock_quantity_increment,
        max_items=max_items,
        sample_limit=sample_limit,
        previous_micro_policy_state_payload=_read_json_if_exists(micro_policy_state_json),
    )
    if refresh_account_snapshot:
        report.setdefault("safety", {})["broker_connection_opened_by_m243"] = True
        report.setdefault("safety", {})["account_snapshot_refreshed_by_m243"] = True
    if output:
        write_live_read_only_capital_validation_markdown(output, report)
    if json_output_path:
        write_live_read_only_capital_validation_json(json_output_path, report)
    if micro_policy_state_json and isinstance(report.get("micro_policy_state"), dict):
        _write_json(micro_policy_state_json, report["micro_policy_state"])
    if json_output:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(live_read_only_capital_validation_markdown(report))


def command_capital_trade_executable_separation(
    settings: Any,
    live_read_only_capital_validation_json: Path,
    sample_limit: int,
    output: Path | None,
    json_output_path: Path | None,
    json_output: bool,
) -> None:
    from recorder.capital_trade_executable_separation import (
        build_capital_trade_executable_separation_report,
        capital_trade_executable_separation_markdown,
        write_capital_trade_executable_separation_json,
        write_capital_trade_executable_separation_markdown,
    )

    report = build_capital_trade_executable_separation_report(
        _read_json_if_exists(live_read_only_capital_validation_json),
        sample_limit=sample_limit,
    )
    if output:
        write_capital_trade_executable_separation_markdown(output, report)
    if json_output_path:
        write_capital_trade_executable_separation_json(json_output_path, report)
    if json_output:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(capital_trade_executable_separation_markdown(report))


def command_capital_pipeline_orchestrator(
    settings: Any,
    execute_rebuild: bool,
    max_artifact_age_minutes: float,
    max_downstream_lag_seconds: float,
    auto_gate_json: Path,
    capital_state_json: Path,
    capital_aware_gate_json: Path,
    ticker_candidate_ranking_json: Path,
    cross_asset_expression_ranking_json: Path,
    cross_asset_expression_outcomes_json: Path,
    ticker_action_candidates_json: Path,
    strategy_allocation_json: Path,
    paper_capital_execution_json: Path,
    ledger_json: Path,
    router_json: Path,
    live_read_only_capital_validation_json: Path,
    market_data_json: Path,
    market_data_risk_gate_json: Path,
    options_market_structure_json: Path,
    options_market_structure_supervisor_json: Path,
    live_scorer_learning_json: Path,
    news_learning_json: Path,
    broker_whatif_json: Path,
    portfolio_risk_state_json: Path,
    session_transition_release_governor_json: Path,
    operating_auto_gate_json: Path,
    marks_json: Path | None,
    allow_fractional_shares: bool,
    stock_quantity_increment: float,
    max_ready_actions: int,
    max_recommendations: int,
    max_intents: int,
    max_orders: int,
    output: Path | None,
    json_output_path: Path | None,
    json_output: bool,
) -> None:
    from recorder.capital_pipeline_orchestrator import (
        build_capital_pipeline_freshness_report,
        capital_pipeline_orchestrator_markdown,
        run_capital_pipeline_rebuild,
        write_capital_pipeline_orchestrator_json,
        write_capital_pipeline_orchestrator_markdown,
    )

    executed_steps: list[str] = []
    if execute_rebuild:
        result = run_capital_pipeline_rebuild(
            auto_gate_path=auto_gate_json,
            capital_state_path=capital_state_json,
            capital_aware_gate_path=capital_aware_gate_json,
            ticker_candidate_ranking_path=ticker_candidate_ranking_json,
            cross_asset_expression_ranking_path=cross_asset_expression_ranking_json,
            cross_asset_expression_outcomes_path=cross_asset_expression_outcomes_json,
            ticker_action_candidates_path=ticker_action_candidates_json,
            strategy_allocation_path=strategy_allocation_json,
            market_data_path=market_data_json,
            market_data_risk_gate_path=market_data_risk_gate_json,
            options_market_structure_path=options_market_structure_json,
            options_market_structure_supervisor_path=options_market_structure_supervisor_json,
            live_scorer_learning_path=live_scorer_learning_json,
            news_learning_path=news_learning_json,
            database_path=settings.database_path,
            paper_capital_execution_path=paper_capital_execution_json,
            ledger_path=ledger_json,
            router_path=router_json,
            portfolio_risk_state_path=portfolio_risk_state_json,
            session_transition_release_governor_path=session_transition_release_governor_json,
            operating_auto_gate_path=operating_auto_gate_json,
            marks_path=marks_json,
            broker_whatif_path=broker_whatif_json,
            live_read_only_validation_path=live_read_only_capital_validation_json,
            allow_fractional_shares=allow_fractional_shares,
            stock_quantity_increment=stock_quantity_increment,
            max_ready_actions=max_ready_actions,
            max_recommendations=max_recommendations,
            max_intents=max_intents,
            max_orders=max_orders,
        )
        executed_steps = list(result.get("executed_steps") or [])
    report = build_capital_pipeline_freshness_report(
        capital_state_path=capital_state_json,
        capital_aware_gate_path=capital_aware_gate_json,
        ticker_candidate_ranking_path=ticker_candidate_ranking_json,
        cross_asset_expression_ranking_path=cross_asset_expression_ranking_json,
        cross_asset_expression_outcomes_path=cross_asset_expression_outcomes_json,
        ticker_action_candidates_path=ticker_action_candidates_json,
        strategy_allocation_path=strategy_allocation_json,
        paper_capital_execution_path=paper_capital_execution_json,
        live_read_only_validation_path=live_read_only_capital_validation_json,
        max_artifact_age_minutes=max_artifact_age_minutes,
        max_downstream_lag_seconds=max_downstream_lag_seconds,
        executed_rebuild=execute_rebuild,
        executed_steps=executed_steps,
    )
    if output:
        write_capital_pipeline_orchestrator_markdown(output, report)
    if json_output_path:
        write_capital_pipeline_orchestrator_json(json_output_path, report)
    if json_output:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(capital_pipeline_orchestrator_markdown(report))


def add_capital_commands(sub) -> None:
    capital_margin = sub.add_parser("capital-margin-state", help="Build M237 read-only capital/margin state and sizing budget report")
    capital_margin.add_argument("--account-snapshot-json", type=Path, default=Path("exports/m110_ibkr_account_snapshot.json"))
    capital_margin.add_argument("--previous-state-json", type=Path, default=Path("exports/m237_capital_margin_state.json"))
    capital_margin.add_argument("--refresh-account-snapshot", action="store_true", help="Refresh the read-only IBKR account snapshot before building M237/M238")
    capital_margin.add_argument("--account", help="Optional IBKR account filter for snapshot refresh")
    capital_margin.add_argument("--account-tags", default="NetLiquidation,TotalCashValue,AvailableFunds,BuyingPower,ExcessLiquidity,MaintMarginReq,InitMarginReq")
    capital_margin.add_argument("--account-timeout-seconds", type=float, default=12.0)
    capital_margin.add_argument("--account-client-id", type=int)
    capital_margin.add_argument("--no-positions", action="store_true", help="Do not request positions when refreshing account snapshot")
    capital_margin.add_argument("--no-open-orders", action="store_true", help="Do not request open orders when refreshing account snapshot")
    capital_margin.add_argument("--max-snapshot-age-minutes", type=int, default=30)
    capital_margin.add_argument("--reserve-fraction", type=float, default=0.20)
    capital_margin.add_argument("--core-risk-fraction", type=float, default=0.08)
    capital_margin.add_argument("--options-premium-fraction", type=float, default=0.03)
    capital_margin.add_argument("--hedge-fraction", type=float, default=0.02)
    capital_margin.add_argument("--single-position-fraction", type=float, default=0.12)
    capital_margin.add_argument("--new-margin-fraction", type=float, default=0.25)
    capital_margin.add_argument("--min-cash-flow", type=float, default=100.0)
    capital_margin.add_argument("--min-cash-flow-fraction", type=float, default=0.01)
    capital_margin.add_argument("--minimum-operating-net-liq", type=float, default=500.0)
    capital_margin.add_argument("--minimum-cash-floor", type=float, default=100.0)
    capital_margin.add_argument("--minimum-available-funds-ratio", type=float, default=0.15)
    capital_margin.add_argument("--minimum-excess-liquidity-ratio", type=float, default=0.20)
    capital_margin.add_argument("--maximum-maintenance-margin-ratio", type=float, default=0.35)
    capital_margin.add_argument("--maximum-initial-margin-ratio", type=float, default=0.50)
    capital_margin.add_argument("--minimum-options-premium-budget", type=float, default=25.0)
    capital_margin.add_argument("--minimum-core-risk-budget", type=float, default=25.0)
    capital_margin.add_argument("--disable-micro-cash-budget-overlay", action="store_true", help="Disable the small cash-only account budget overlay")
    capital_margin.add_argument("--micro-cash-net-liq-threshold", type=float, default=2000.0)
    capital_margin.add_argument("--micro-cash-option-cash-fraction", type=float, default=0.35)
    capital_margin.add_argument("--micro-cash-option-net-liq-fraction", type=float, default=0.30)
    capital_margin.add_argument("--micro-cash-option-budget-cap", type=float, default=150.0)
    capital_margin.add_argument("--output", type=Path, default=Path("exports/m237_capital_margin_state.md"))
    capital_margin.add_argument("--json-output-path", type=Path, default=Path("exports/m237_capital_margin_state.json"))
    capital_margin.add_argument("--json", action="store_true", help="Print machine-readable capital/margin state JSON")

    live_validation = sub.add_parser(
        "live-read-only-capital-validation",
        help="Build M243 live-account read-only capacity validation; answers how much could fit without submitting orders",
    )
    live_validation.add_argument("--capital-state-json", type=Path, default=Path("exports/m237_capital_margin_state.json"))
    live_validation.add_argument("--capital-aware-gate-json", type=Path, default=Path("exports/m239_capital_aware_action_gate.json"))
    live_validation.add_argument("--broker-whatif-json", type=Path, default=Path("exports/m211_ibkr_defensive_order_whatif_cache.json"))
    live_validation.add_argument("--micro-policy-state-json", type=Path, default=Path("exports/m246_micro_cash_policy_state.json"))
    live_validation.add_argument("--account-snapshot-json", type=Path, default=Path("exports/m110_ibkr_account_snapshot.json"))
    live_validation.add_argument("--previous-state-json", type=Path, default=Path("exports/m237_capital_margin_state.json"))
    live_validation.add_argument("--refresh-account-snapshot", action="store_true", help="Refresh the read-only IBKR account snapshot before M243 validation")
    live_validation.add_argument("--account", help="Optional IBKR account filter for snapshot refresh")
    live_validation.add_argument("--account-tags", default="NetLiquidation,TotalCashValue,AvailableFunds,BuyingPower,ExcessLiquidity,MaintMarginReq,InitMarginReq")
    live_validation.add_argument("--account-timeout-seconds", type=float, default=12.0)
    live_validation.add_argument("--account-client-id", type=int)
    live_validation.add_argument("--no-positions", action="store_true", help="Do not request positions when refreshing account snapshot")
    live_validation.add_argument("--no-open-orders", action="store_true", help="Do not request open orders when refreshing account snapshot")
    live_validation.add_argument("--max-snapshot-age-minutes", type=int, default=30)
    live_validation.add_argument("--reserve-fraction", type=float, default=0.20)
    live_validation.add_argument("--core-risk-fraction", type=float, default=0.08)
    live_validation.add_argument("--options-premium-fraction", type=float, default=0.03)
    live_validation.add_argument("--hedge-fraction", type=float, default=0.02)
    live_validation.add_argument("--single-position-fraction", type=float, default=0.12)
    live_validation.add_argument("--new-margin-fraction", type=float, default=0.25)
    live_validation.add_argument("--min-cash-flow", type=float, default=100.0)
    live_validation.add_argument("--min-cash-flow-fraction", type=float, default=0.01)
    live_validation.add_argument("--minimum-operating-net-liq", type=float, default=500.0)
    live_validation.add_argument("--minimum-cash-floor", type=float, default=100.0)
    live_validation.add_argument("--minimum-available-funds-ratio", type=float, default=0.15)
    live_validation.add_argument("--minimum-excess-liquidity-ratio", type=float, default=0.20)
    live_validation.add_argument("--maximum-maintenance-margin-ratio", type=float, default=0.35)
    live_validation.add_argument("--maximum-initial-margin-ratio", type=float, default=0.50)
    live_validation.add_argument("--minimum-options-premium-budget", type=float, default=25.0)
    live_validation.add_argument("--minimum-core-risk-budget", type=float, default=25.0)
    live_validation.add_argument("--disable-micro-cash-budget-overlay", action="store_true", help="Disable the small cash-only account budget overlay")
    live_validation.add_argument("--micro-cash-net-liq-threshold", type=float, default=2000.0)
    live_validation.add_argument("--micro-cash-option-cash-fraction", type=float, default=0.35)
    live_validation.add_argument("--micro-cash-option-net-liq-fraction", type=float, default=0.30)
    live_validation.add_argument("--micro-cash-option-budget-cap", type=float, default=150.0)
    live_validation.add_argument("--whole-share-only", action="store_true", help="Round stock capacity to whole shares instead of fractional share increments")
    live_validation.add_argument("--stock-quantity-increment", type=float, default=0.01)
    live_validation.add_argument("--max-items", type=int, default=50)
    live_validation.add_argument("--sample-limit", type=int, default=20)
    live_validation.add_argument("--output", type=Path, default=Path("exports/m243_live_read_only_capital_validation.md"))
    live_validation.add_argument("--json-output-path", type=Path, default=Path("exports/m243_live_read_only_capital_validation.json"))
    live_validation.add_argument("--json", action="store_true", help="Print machine-readable M243 live read-only capital validation JSON")

    trade_executable = sub.add_parser(
        "capital-trade-executable-separation",
        help="Build M245 summary that separates account capital feasibility from actual trade executability",
    )
    trade_executable.add_argument(
        "--live-read-only-capital-validation-json",
        type=Path,
        default=Path("exports/m243_live_read_only_capital_validation.json"),
    )
    trade_executable.add_argument("--sample-limit", type=int, default=20)
    trade_executable.add_argument("--output", type=Path, default=Path("exports/m245_capital_trade_executable_separation.md"))
    trade_executable.add_argument("--json-output-path", type=Path, default=Path("exports/m245_capital_trade_executable_separation.json"))
    trade_executable.add_argument("--json", action="store_true", help="Print machine-readable M245 capital/trade executable separation JSON")

    capital_pipeline = sub.add_parser(
        "capital-pipeline-orchestrator",
        help="Build M244 capital pipeline freshness audit and optionally rebuild M239/M241.5/M241.7/M241.8/M241.6/M241/M242/M243 in safe order",
    )
    capital_pipeline.add_argument("--execute-rebuild", action="store_true", help="Rebuild downstream paper/read-only artifacts after the freshness audit")
    capital_pipeline.add_argument("--max-artifact-age-minutes", type=float, default=30.0)
    capital_pipeline.add_argument("--max-downstream-lag-seconds", type=float, default=30.0)
    capital_pipeline.add_argument("--auto-gate-json", type=Path, default=Path("exports/m182_market_data_operating_auto_gate_latest.json"))
    capital_pipeline.add_argument("--capital-state-json", type=Path, default=Path("exports/m237_capital_margin_state.json"))
    capital_pipeline.add_argument("--capital-aware-gate-json", type=Path, default=Path("exports/m239_capital_aware_action_gate.json"))
    capital_pipeline.add_argument("--ticker-candidate-ranking-json", type=Path, default=Path("exports/m241_5_ticker_candidate_ranking.json"))
    capital_pipeline.add_argument("--cross-asset-expression-ranking-json", type=Path, default=Path("exports/m241_7_cross_asset_expression_ranking.json"))
    capital_pipeline.add_argument("--cross-asset-expression-outcomes-json", type=Path, default=Path("exports/m241_8_cross_asset_expression_outcomes.json"))
    capital_pipeline.add_argument("--ticker-action-candidates-json", type=Path, default=Path("exports/m241_6_ticker_action_candidates.json"))
    capital_pipeline.add_argument("--strategy-allocation-json", type=Path, default=Path("exports/m241_strategy_allocation_engine.json"))
    capital_pipeline.add_argument("--paper-capital-execution-json", type=Path, default=Path("exports/m242_paper_capital_aware_execution.json"))
    capital_pipeline.add_argument("--ledger-json", type=Path, default=Path("exports/m242_capital_aware_execution_intent_ledger.json"))
    capital_pipeline.add_argument("--router-json", type=Path, default=Path("exports/m242_capital_aware_paper_order_router.json"))
    capital_pipeline.add_argument("--live-read-only-capital-validation-json", type=Path, default=Path("exports/m243_live_read_only_capital_validation.json"))
    capital_pipeline.add_argument("--market-data-json", type=Path, default=Path("exports/m153_market_data_desk_refresh.json"))
    capital_pipeline.add_argument("--market-data-risk-gate-json", type=Path, default=Path("exports/m175_dashboard_market_data_risk_gate_latest.json"))
    capital_pipeline.add_argument("--options-market-structure-json", type=Path, default=Path("exports/m217_options_market_structure_desk.json"))
    capital_pipeline.add_argument("--options-market-structure-supervisor-json", type=Path, default=Path("exports/m219_options_market_structure_supervisor.json"))
    capital_pipeline.add_argument("--live-scorer-learning-json", type=Path, default=Path("exports/m223_live_scorer_learning.json"))
    capital_pipeline.add_argument("--news-learning-json", type=Path, default=Path("exports/m236_news_learning_report.json"))
    capital_pipeline.add_argument("--broker-whatif-json", type=Path, default=Path("exports/m211_ibkr_defensive_order_whatif_cache.json"))
    capital_pipeline.add_argument("--portfolio-risk-state-json", type=Path, default=Path("exports/m123_paper_portfolio_risk_state.json"))
    capital_pipeline.add_argument("--session-transition-release-governor-json", type=Path, default=Path("exports/m206_session_transition_release_governor.json"))
    capital_pipeline.add_argument("--operating-auto-gate-json", type=Path, default=Path("exports/m182_market_data_operating_auto_gate_latest.json"))
    capital_pipeline.add_argument("--marks-json", type=Path)
    capital_pipeline.add_argument("--whole-share-only", action="store_true", help="Round stock capacity to whole shares instead of fractional share increments")
    capital_pipeline.add_argument("--stock-quantity-increment", type=float, default=0.01)
    capital_pipeline.add_argument("--max-ready-actions", type=int, default=10)
    capital_pipeline.add_argument("--max-recommendations", type=int, default=25)
    capital_pipeline.add_argument("--max-intents", type=int, default=10)
    capital_pipeline.add_argument("--max-orders", type=int, default=10)
    capital_pipeline.add_argument("--output", type=Path, default=Path("exports/m244_capital_pipeline_orchestrator.md"))
    capital_pipeline.add_argument("--json-output-path", type=Path, default=Path("exports/m244_capital_pipeline_orchestrator.json"))
    capital_pipeline.add_argument("--json", action="store_true", help="Print machine-readable M244 capital pipeline report JSON")
