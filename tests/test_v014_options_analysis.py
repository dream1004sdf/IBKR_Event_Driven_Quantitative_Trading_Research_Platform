from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from recorder.options_analysis import (
    analyze_replay_row_options,
    analyze_replay_row_strategy_allocation,
    apply_option_leg_quote_gate_to_allocations,
    apply_option_liquidity_gate_to_allocations,
    account_backed_portfolio_readiness_markdown,
    build_execution_readiness_report,
    build_account_backed_portfolio_readiness_report,
    build_account_scaled_position_sizing_report,
    build_mock_portfolio_readiness_report,
    build_order_preview_report,
    build_autonomous_execution_gate_report,
    build_execution_intent_ledger_report,
    build_paper_order_router_report,
    build_paper_exit_intent_ledger_report,
    build_paper_exit_router_report,
    build_paper_position_mark_report,
    build_paper_position_mark_timeseries_report,
    build_paper_portfolio_state_report,
    build_paper_portfolio_risk_state_report,
    build_paper_portfolio_risk_loop_report,
    build_paper_cycle_runner_report,
    build_paper_multi_cycle_runner_report,
    build_paper_cycle_scheduler_report,
    build_paper_cycle_scheduler_checkpoint,
    build_paper_data_quality_preflight_report,
    build_paper_cycle_watch_tick_report,
    build_paper_cycle_watch_daemon_report,
    build_paper_cycle_watch_runner_report,
    extract_paper_cycle_scheduler_resume_state,
    execution_readiness_markdown,
    mock_portfolio_readiness_markdown,
    account_scaled_position_sizing_markdown,
    order_preview_markdown,
    autonomous_execution_gate_markdown,
    execution_intent_ledger_markdown,
    paper_order_router_markdown,
    paper_exit_intent_ledger_markdown,
    paper_exit_router_markdown,
    paper_position_mark_markdown,
    paper_position_mark_timeseries_markdown,
    paper_portfolio_state_markdown,
    paper_portfolio_risk_state_markdown,
    paper_portfolio_risk_loop_markdown,
    paper_cycle_runner_markdown,
    paper_multi_cycle_runner_markdown,
    paper_cycle_scheduler_markdown,
    paper_data_quality_preflight_markdown,
    paper_cycle_watch_tick_markdown,
    paper_cycle_watch_daemon_markdown,
    paper_cycle_watch_runner_markdown,
    option_leg_quote_gate_markdown,
    option_liquidity_gate_markdown,
    options_analysis_markdown,
    options_analysis_summary,
    strategy_allocation_markdown,
    strategy_allocation_summary,
    tune_strategy_allocation_parameters,
    walk_forward_strategy_leg_aware_tuning,
    walk_forward_strategy_leg_aware_tuning_markdown,
    walk_forward_strategy_allocation_tuning,
    walk_forward_strategy_allocation_tuning_markdown,
)
from recorder.operating_defensive import (
    build_operating_defensive_action_plan,
    operating_defensive_action_plan_markdown,
)
from recorder.operating_defensive import (
    build_operating_defensive_intent_ledger,
    operating_defensive_intent_ledger_markdown,
)
from recorder.operating_defensive import (
    build_operating_defensive_gate_evidence,
    build_operating_defensive_order_preview,
    operating_defensive_gate_evidence_markdown,
)
from recorder.operating_defensive import (
    build_operating_defensive_gate_retry_report,
    build_option_quote_resume_plan,
    defensive_option_symbols_from_plan,
    detect_ibkr_read_only_mode,
    evaluate_option_quote_session_gate,
    next_regular_option_session_open,
    option_quote_resume_plan_markdown,
    operating_defensive_gate_retry_markdown,
)
from recorder.ibkr_order_preview import _order_from_preview, _order_payload
from recorder.operating_final_preflight import build_operating_final_preflight_report
from main import (
    _apply_mark_refresh_quality_action_to_portfolio_risk,
    _evaluate_mark_refresh_policy,
    _evaluate_paper_session_gate,
    _mark_refresh_apply_partial_fallback,
    _mark_refresh_quality_execution_action,
)


def _score_row(
    outcome_id: int,
    *,
    direction: str,
    expected_return: float,
    actual_excess_return: float,
    trade_action: str = "directional_signal",
) -> dict:
    return {
        "id": outcome_id,
        "outcome_id": outcome_id,
        "symbol": "NVDA" if direction == "up" else "TSLA",
        "horizon": "1D",
        "price_source": "yahoo_chart_daily_adjclose",
        "replay_timestamp": f"2026-01-{outcome_id:02d}T14:00:00+00:00",
        "scorer_name": "openai",
        "model_name": "gpt-test",
        "scorer_version": "openai-replay-score-v14-nuanced-v1",
        "predicted_direction": direction,
        "actual_direction": direction,
        "direction_hit": 1,
        "confidence": 0.65,
        "expected_return": expected_return,
        "actual_raw_return": actual_excess_return,
        "actual_excess_return": actual_excess_return,
        "raw_json": json.dumps(
            {
                "debias": {
                    "method": trade_action,
                    "trade_action": trade_action,
                    "observe_only": trade_action == "observe_only",
                }
            }
        ),
    }


def _mark_snapshot(
    *,
    generated_at: str = "2026-06-30T00:00:00+00:00",
    symbol: str = "MSFT",
    delayed: bool = False,
    marked: bool = True,
    mark_timestamp: str = "2026-06-30T00:00:30+00:00",
) -> dict:
    market_data_type = 3 if delayed else 1
    return {
        "stock_mark_snapshot_version": "ibkr-stock-mark-snapshot-v1",
        "generated_at": generated_at,
        "symbols_requested": [symbol],
        "row_count": 1,
        "marked_count": 1 if marked else 0,
        "missing_mark_count": 0 if marked else 1,
        "marks": [
            {
                "symbol": symbol,
                "market_data_type": market_data_type,
                "market_data_type_label": "delayed" if delayed else "live",
                "delayed": delayed,
                "mark_price": 100.0 if marked else None,
                "mark_source": "ibkr_last_delayed" if delayed else "ibkr_last",
                "mark_status": "marked" if marked else "missing_mark",
                "mark_timestamp": mark_timestamp,
                "received_ticks": [
                    {
                        "tick_type": 4,
                        "price": 100.0,
                        "received_at": mark_timestamp,
                    }
                ],
            }
        ],
        "errors": [],
        "safety": {
            "ibkr_order_submitted": False,
            "transmit_enabled": False,
            "live_order_routing_enabled": False,
        },
    }


def test_options_analysis_selects_directional_defined_risk_candidate() -> None:
    row = _score_row(1, direction="up", expected_return=0.04, actual_excess_return=0.06)

    analysis = analyze_replay_row_options(
        row,
        underlying_reference_price=100.0,
        base_implied_vol=0.40,
        spread_width_pct=0.05,
        min_option_score=0.20,
    )

    assert analysis["option_candidate_status"] == "candidate"
    assert analysis["selected_strategy"]["strategy"] in {"long_call", "bull_call_debit_spread"}
    assert analysis["selected_strategy"]["max_loss"] > 0
    assert analysis["selected_strategy"]["realized_pnl"] is not None
    assert analysis["safety"]["ibkr_order_submitted"] is False


def test_options_analysis_blocks_observe_only_rows() -> None:
    row = _score_row(
        2,
        direction="down",
        expected_return=-0.03,
        actual_excess_return=-0.05,
        trade_action="observe_only",
    )

    analysis = analyze_replay_row_options(row)

    assert analysis["option_candidate_status"] == "blocked"
    assert analysis["selected_strategy"] is None
    assert analysis["strategy_candidates"] == []


def test_options_analysis_summary_reports_strategy_groups() -> None:
    rows = [
        _score_row(1, direction="up", expected_return=0.04, actual_excess_return=0.06),
        _score_row(2, direction="down", expected_return=-0.04, actual_excess_return=-0.06),
        _score_row(3, direction="up", expected_return=0.01, actual_excess_return=-0.02, trade_action="observe_only"),
    ]

    summary = options_analysis_summary(
        rows,
        underlying_reference_price=100.0,
        base_implied_vol=0.40,
        spread_width_pct=0.05,
        min_option_score=0.20,
    )
    report = options_analysis_markdown(summary)

    assert summary["rows"] == 3
    assert summary["option_candidates"] == 2
    assert summary["evaluated_candidates"] == 2
    assert summary["strategy_counts"]["blocked"] == 1
    assert "Replay Score Options Analysis Report" in report


def test_strategy_allocation_can_select_option_over_stock() -> None:
    row = _score_row(4, direction="up", expected_return=0.08, actual_excess_return=0.10)

    allocation = analyze_replay_row_strategy_allocation(
        row,
        underlying_reference_price=100.0,
        stock_quantity=10.0,
        stock_stop_loss_pct=0.02,
        base_implied_vol=0.30,
        spread_width_pct=0.05,
        min_option_score=0.20,
        option_preference_edge=-1.0,
    )

    assert allocation["allocation_status"] == "candidate"
    assert allocation["selected_expression"]["instrument"] == "option"
    assert allocation["selected_expression"]["max_loss"] > 0
    assert allocation["safety"]["ibkr_order_submitted"] is False


def test_strategy_allocation_summary_reports_stock_and_option_choices() -> None:
    rows = [
        _score_row(4, direction="up", expected_return=0.08, actual_excess_return=0.10),
        _score_row(5, direction="down", expected_return=-0.01, actual_excess_return=-0.02),
        _score_row(6, direction="up", expected_return=0.01, actual_excess_return=0.02, trade_action="observe_only"),
    ]

    summary = strategy_allocation_summary(
        rows,
        underlying_reference_price=100.0,
        stock_quantity=10.0,
        stock_stop_loss_pct=0.02,
        base_implied_vol=0.30,
        spread_width_pct=0.05,
        min_option_score=0.20,
        option_preference_edge=0.05,
    )
    report = strategy_allocation_markdown(summary)

    assert summary["rows"] == 3
    assert summary["selected_allocations"] >= 1
    assert "stock" in summary["selected_instrument_counts"] or "option" in summary["selected_instrument_counts"]
    assert "Replay Score Stock/Option Strategy Allocation Report" in report


def test_strategy_allocation_parameter_tuner_selects_params() -> None:
    rows = [
        _score_row(7, direction="up", expected_return=0.08, actual_excess_return=0.10),
        _score_row(8, direction="down", expected_return=-0.04, actual_excess_return=-0.05),
        _score_row(9, direction="up", expected_return=0.01, actual_excess_return=-0.02),
    ]

    result = tune_strategy_allocation_parameters(
        rows,
        min_confidence_grid=(0.30, 0.40),
        min_stock_expected_return_grid=(0.0025,),
        min_option_score_grid=(0.20,),
        min_option_return_on_risk_grid=(0.0,),
        option_preference_edge_grid=(0.05,),
        base_implied_vol=0.30,
        top_n=2,
    )

    assert result["candidates_evaluated"] == 2
    assert result["selected"]["params"]["min_confidence"] in {0.30, 0.40}
    assert result["selected_summary"]["selected_allocations"] >= 1


def test_walk_forward_strategy_allocation_tuning_uses_prior_rows() -> None:
    rows = [
        _score_row(10, direction="up", expected_return=0.08, actual_excess_return=0.10),
        _score_row(11, direction="down", expected_return=-0.04, actual_excess_return=-0.05),
        _score_row(12, direction="up", expected_return=0.02, actual_excess_return=0.03),
        _score_row(13, direction="down", expected_return=-0.03, actual_excess_return=-0.04),
        _score_row(14, direction="up", expected_return=0.04, actual_excess_return=-0.02),
    ]

    checkpoint_path = Path("work/test_strategy_wf_checkpoint.json")
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    if checkpoint_path.exists():
        checkpoint_path.unlink()
    result = walk_forward_strategy_allocation_tuning(
        rows,
        min_train_rows=3,
        test_window_rows=2,
        step_rows=2,
        min_confidence_grid=(0.30, 0.40),
        min_stock_expected_return_grid=(0.0025,),
        min_option_score_grid=(0.20,),
        min_option_return_on_risk_grid=(0.0,),
        option_preference_edge_grid=(0.05,),
        base_implied_vol=0.30,
        top_n=2,
        checkpoint_path=checkpoint_path,
    )
    resumed = walk_forward_strategy_allocation_tuning(
        rows,
        min_train_rows=3,
        test_window_rows=2,
        step_rows=2,
        min_confidence_grid=(0.30, 0.40),
        min_stock_expected_return_grid=(0.0025,),
        min_option_score_grid=(0.20,),
        min_option_return_on_risk_grid=(0.0,),
        option_preference_edge_grid=(0.05,),
        base_implied_vol=0.30,
        top_n=2,
        checkpoint_path=checkpoint_path,
    )
    report = walk_forward_strategy_allocation_tuning_markdown(result)

    assert result["fold_count"] == 1
    assert checkpoint_path.exists()
    assert resumed["fold_count"] == result["fold_count"]
    assert result["folds"][0]["train_rows"] == 3
    assert result["folds"][0]["test_rows"] == 2
    assert result["aggregate_test_summary"]["rows"] == 2
    assert "Walk-Forward Stock/Option Strategy Allocation Tuning Report" in report
    if checkpoint_path.exists():
        checkpoint_path.unlink()


def test_option_liquidity_gate_downgrades_failed_option_to_stock() -> None:
    row = _score_row(15, direction="up", expected_return=0.08, actual_excess_return=0.10)
    allocation = analyze_replay_row_strategy_allocation(
        row,
        underlying_reference_price=100.0,
        stock_quantity=10.0,
        stock_stop_loss_pct=0.02,
        base_implied_vol=0.30,
        spread_width_pct=0.05,
        min_option_score=0.20,
        option_preference_edge=-1.0,
    )
    assert allocation["selected_expression"]["instrument"] == "option"
    allocation_payload = {
        "aggregate_test_summary": {
            "rows": 1,
            "selected_allocations": 1,
            "allocations": [allocation],
            "parameters": {},
        }
    }
    quote_snapshot = {
        "snapshot_version": "test",
        "generated_at": "2026-06-29T00:00:00+00:00",
        "quotes": [
            {
                "symbol": "NVDA",
                "quote_status": "close_only",
                "close": 1.0,
                "spread_pct": None,
            }
        ],
    }

    result = apply_option_liquidity_gate_to_allocations(
        allocation_payload,
        quote_snapshot,
        fallback_to_stock=True,
    )
    report = option_liquidity_gate_markdown(result)
    gated = result["gated_summary"]["allocations"][0]

    assert result["option_gate_counts"]["option_allocations_seen"] == 1
    assert result["option_gate_counts"]["option_allocations_downgraded_to_stock"] == 1
    assert gated["selected_expression"]["instrument"] == "stock"
    assert gated["liquidity_gate"]["status"] == "downgraded_to_stock"
    assert "Paper Option Liquidity Gate Report" in report


def test_option_liquidity_gate_keeps_liquid_option() -> None:
    row = _score_row(16, direction="up", expected_return=0.08, actual_excess_return=0.10)
    allocation = analyze_replay_row_strategy_allocation(
        row,
        underlying_reference_price=100.0,
        stock_quantity=10.0,
        stock_stop_loss_pct=0.02,
        base_implied_vol=0.30,
        spread_width_pct=0.05,
        min_option_score=0.20,
        option_preference_edge=-1.0,
    )
    quote_snapshot = {
        "snapshot_version": "test",
        "generated_at": "2026-06-29T00:00:00+00:00",
        "quotes": [
            {
                "symbol": "NVDA",
                "quote_status": "quoted",
                "bid": 1.0,
                "ask": 1.1,
                "spread_pct": 0.095,
                "volume": 10,
                "open_interest": 100,
                "option_computation": {"MODEL_OPTION": {"implied_vol": 0.35}},
            }
        ],
    }

    result = apply_option_liquidity_gate_to_allocations(
        {"aggregate_test_summary": {"rows": 1, "selected_allocations": 1, "allocations": [allocation], "parameters": {}}},
        quote_snapshot,
        max_spread_pct=0.30,
        min_volume=1,
        min_open_interest=1,
        require_iv=True,
    )

    assert result["option_gate_counts"]["option_allocations_passed"] == 1
    assert result["gated_summary"]["allocations"][0]["selected_expression"]["instrument"] == "option"


def test_option_leg_quote_gate_keeps_call_debit_spread_when_legs_exist() -> None:
    row = _score_row(17, direction="up", expected_return=0.08, actual_excess_return=0.10)
    allocation = analyze_replay_row_strategy_allocation(
        row,
        underlying_reference_price=100.0,
        stock_quantity=10.0,
        stock_stop_loss_pct=0.02,
        base_implied_vol=0.30,
        spread_width_pct=0.05,
        min_option_score=0.20,
        option_preference_edge=-1.0,
    )
    assert allocation["selected_expression"]["strategy"] == "bull_call_debit_spread"
    quote_snapshot = {
        "snapshot_version": "test",
        "generated_at": "2026-06-29T00:00:00+00:00",
        "requested_quotes": 2,
        "quoted_contracts": 2,
        "quoted_rate": 1.0,
        "avg_spread_pct": 0.10,
        "quotes": [
            {
                "symbol": "NVDA",
                "expiration": "20260706",
                "dte": 7,
                "strike": 100.0,
                "right": "C",
                "reference_price": 100.0,
                "moneyness": 0.0,
                "quote_status": "quoted",
                "bid": 3.8,
                "ask": 4.0,
                "mid": 3.9,
                "spread_pct": 0.051,
                "multiplier": "100",
            },
            {
                "symbol": "NVDA",
                "expiration": "20260706",
                "dte": 7,
                "strike": 105.0,
                "right": "C",
                "reference_price": 100.0,
                "moneyness": 0.05,
                "quote_status": "quoted",
                "bid": 1.4,
                "ask": 1.6,
                "mid": 1.5,
                "spread_pct": 0.133,
                "multiplier": "100",
            },
        ],
    }

    result = apply_option_leg_quote_gate_to_allocations(
        {"aggregate_test_summary": {"rows": 1, "selected_allocations": 1, "allocations": [allocation], "parameters": {}}},
        quote_snapshot,
        max_leg_spread_pct=0.30,
    )
    report = option_leg_quote_gate_markdown(result)
    gated = result["gated_summary"]["allocations"][0]

    assert result["option_gate_counts"]["option_allocations_passed"] == 1
    assert gated["selected_expression"]["instrument"] == "option"
    assert gated["leg_quote_gate"]["status"] == "passed"
    assert len(gated["leg_quote_gate"]["legs"]) == 2
    assert gated["selected_expression"]["estimated_live_net_debit"] == 2.6
    assert "Paper Option Leg Quote Gate Report" in report


def test_option_leg_quote_gate_downgrades_when_short_leg_missing() -> None:
    row = _score_row(18, direction="up", expected_return=0.08, actual_excess_return=0.10)
    allocation = analyze_replay_row_strategy_allocation(
        row,
        underlying_reference_price=100.0,
        stock_quantity=10.0,
        stock_stop_loss_pct=0.02,
        base_implied_vol=0.30,
        spread_width_pct=0.05,
        min_option_score=0.20,
        option_preference_edge=-1.0,
    )
    quote_snapshot = {
        "snapshot_version": "test",
        "generated_at": "2026-06-29T00:00:00+00:00",
        "quotes": [
            {
                "symbol": "NVDA",
                "expiration": "20260706",
                "dte": 7,
                "strike": 100.0,
                "right": "C",
                "reference_price": 100.0,
                "moneyness": 0.0,
                "quote_status": "quoted",
                "bid": 3.8,
                "ask": 4.0,
                "mid": 3.9,
                "spread_pct": 0.051,
                "multiplier": "100",
            }
        ],
    }

    result = apply_option_leg_quote_gate_to_allocations(
        {"aggregate_test_summary": {"rows": 1, "selected_allocations": 1, "allocations": [allocation], "parameters": {}}},
        quote_snapshot,
        max_leg_spread_pct=0.30,
        fallback_to_stock=True,
    )
    gated = result["gated_summary"]["allocations"][0]

    assert result["option_gate_counts"]["option_allocations_downgraded_to_stock"] == 1
    assert gated["selected_expression"]["instrument"] == "stock"
    assert gated["leg_quote_gate"]["status"] == "downgraded_to_stock"
    assert gated["leg_quote_gate"]["failed_reasons"] == ["missing_higher_short_leg_same_expiration"]


def test_leg_aware_walk_forward_tunes_after_live_leg_gate() -> None:
    rows = [
        _score_row(19, direction="up", expected_return=0.08, actual_excess_return=0.10),
        _score_row(20, direction="up", expected_return=0.07, actual_excess_return=0.08),
        _score_row(21, direction="up", expected_return=0.06, actual_excess_return=0.07),
        _score_row(22, direction="up", expected_return=0.05, actual_excess_return=0.06),
    ]
    quote_snapshot = {
        "snapshot_version": "test",
        "generated_at": "2026-06-29T00:00:00+00:00",
        "requested_quotes": 1,
        "quoted_contracts": 1,
        "quoted_rate": 1.0,
        "avg_spread_pct": 0.05,
        "quotes": [
            {
                "symbol": "NVDA",
                "expiration": "20260706",
                "dte": 7,
                "strike": 100.0,
                "right": "C",
                "reference_price": 100.0,
                "moneyness": 0.0,
                "quote_status": "quoted",
                "bid": 3.8,
                "ask": 4.0,
                "mid": 3.9,
                "spread_pct": 0.051,
                "multiplier": "100",
            }
        ],
    }

    result = walk_forward_strategy_leg_aware_tuning(
        rows,
        quote_snapshot,
        min_train_rows=2,
        test_window_rows=2,
        step_rows=2,
        min_confidence_grid=(0.30,),
        min_stock_expected_return_grid=(0.0025,),
        min_option_score_grid=(0.20,),
        min_option_return_on_risk_grid=(0.0,),
        option_preference_edge_grid=(-1.0,),
        base_implied_vol=0.30,
        max_leg_spread_pct=0.30,
        fallback_to_stock=True,
    )
    report = walk_forward_strategy_leg_aware_tuning_markdown(result)

    assert result["fold_count"] == 1
    assert result["aggregate_test_summary"]["option_gate_counts"]["option_allocations_downgraded_to_stock"] >= 1
    assert result["aggregate_test_summary"]["selected_instrument_counts"]["stock"] >= 1
    assert "Walk-Forward Stock/Option Leg-Aware Strategy Tuning Report" in report


def test_execution_readiness_builds_option_combo_ticket_with_review() -> None:
    row = _score_row(23, direction="up", expected_return=0.08, actual_excess_return=0.10)
    allocation = analyze_replay_row_strategy_allocation(
        row,
        underlying_reference_price=100.0,
        stock_quantity=10.0,
        stock_stop_loss_pct=0.02,
        base_implied_vol=0.30,
        spread_width_pct=0.05,
        min_option_score=0.20,
        option_preference_edge=-1.0,
    )
    allocation["selected_expression"]["live_leg_quote_match"] = {
        "status": "passed",
        "symbol": "NVDA",
        "strategy": "bull_call_debit_spread",
        "right": "C",
        "legs": [
            {"role": "long", "symbol": "NVDA", "expiration": "20260706", "dte": 7, "strike": 100.0, "right": "C", "bid": 3.8, "ask": 4.0, "mid": 3.9, "spread_pct": 0.05, "multiplier": "100"},
            {"role": "short", "symbol": "NVDA", "expiration": "20260706", "dte": 7, "strike": 105.0, "right": "C", "bid": 1.4, "ask": 1.6, "mid": 1.5, "spread_pct": 0.13, "multiplier": "100"},
        ],
        "estimated_live_net_debit": 2.6,
        "estimated_live_width": 5.0,
        "estimated_live_max_loss": 260.0,
        "estimated_live_max_profit": 240.0,
        "multiplier": 100.0,
    }

    result = build_execution_readiness_report(
        {"aggregate_test_summary": {"rows": 1, "selected_allocations": 1, "allocations": [allocation], "parameters": {}}},
        max_option_max_loss=500.0,
        max_option_net_debit=500.0,
        min_option_reward_to_risk=0.20,
    )
    report = execution_readiness_markdown(result)
    readiness = result["readiness_items"][0]["execution_readiness"]

    assert result["readiness_counts"]["needs_human_review"] == 1
    assert result["ticket_type_counts"]["option_combo_debit_spread"] == 1
    assert readiness["ticket_draft"]["order_type"] == "NET_DEBIT_LIMIT"
    assert len(readiness["ticket_draft"]["legs"]) == 2
    assert readiness["human_review_required"] is True
    assert "Paper Execution Readiness Report" in report


def test_execution_readiness_marks_missing_option_live_match_not_ready() -> None:
    row = _score_row(24, direction="up", expected_return=0.08, actual_excess_return=0.10)
    allocation = analyze_replay_row_strategy_allocation(
        row,
        underlying_reference_price=100.0,
        stock_quantity=10.0,
        stock_stop_loss_pct=0.02,
        base_implied_vol=0.30,
        spread_width_pct=0.05,
        min_option_score=0.20,
        option_preference_edge=-1.0,
    )

    result = build_execution_readiness_report(
        {"aggregate_test_summary": {"rows": 1, "selected_allocations": 1, "allocations": [allocation], "parameters": {}}},
    )

    assert result["readiness_counts"]["not_ready"] == 1
    assert result["check_failure_counts"]["live_leg_quote_match_present"] == 1


def test_mock_portfolio_readiness_applies_batch_risk_budget() -> None:
    ready_item = {
        "symbol": "NVDA",
        "selected_expression": {"instrument": "stock", "strategy": "stock_long"},
        "execution_readiness": {
            "readiness_status": "ready",
            "human_review_required": False,
            "review_reasons": [],
            "ticket_draft": {
                "ticket_type": "stock",
                "order_action": "BUY",
                "estimated_notional": 1000.0,
                "stop_loss_proxy": 20.0,
            },
        },
    }
    review_item = {
        "symbol": "TSLA",
        "selected_expression": {"instrument": "stock", "strategy": "stock_short"},
        "execution_readiness": {
            "readiness_status": "needs_human_review",
            "human_review_required": True,
            "review_reasons": ["short_stock_requires_borrow_margin_and_account_review"],
            "ticket_draft": {
                "ticket_type": "stock",
                "order_action": "SELL_SHORT",
                "estimated_notional": 1000.0,
                "stop_loss_proxy": 20.0,
            },
        },
    }
    result = build_mock_portfolio_readiness_report(
        {
            "execution_readiness_version": "test",
            "source_rows": 2,
            "source_selected_allocations": 2,
            "ticket_drafts": 2,
            "actionable_ticket_drafts": 2,
            "readiness_items": [ready_item, review_item],
        },
        mock_account_equity=10000.0,
        mock_cash=10000.0,
        max_total_open_risk_pct=0.003,
        max_approved_tickets=5,
    )
    report = mock_portfolio_readiness_markdown(result)

    assert result["portfolio_status_counts"]["portfolio_ready"] == 1
    assert result["portfolio_status_counts"]["portfolio_not_ready"] == 1
    assert result["portfolio_check_failure_counts"]["total_open_risk_within_mock_policy"] == 1
    assert "Paper Mock Portfolio Readiness Report" in report


def test_mock_portfolio_readiness_preserves_human_review_status_when_budget_passes() -> None:
    review_item = {
        "symbol": "TSLA",
        "selected_expression": {"instrument": "stock", "strategy": "stock_short"},
        "execution_readiness": {
            "readiness_status": "needs_human_review",
            "human_review_required": True,
            "review_reasons": ["short_stock_requires_borrow_margin_and_account_review"],
            "ticket_draft": {
                "ticket_type": "stock",
                "order_action": "SELL_SHORT",
                "estimated_notional": 1000.0,
                "stop_loss_proxy": 20.0,
            },
        },
    }

    result = build_mock_portfolio_readiness_report(
        {
            "execution_readiness_version": "test",
            "source_rows": 1,
            "source_selected_allocations": 1,
            "ticket_drafts": 1,
            "actionable_ticket_drafts": 1,
            "readiness_items": [review_item],
        },
        mock_account_equity=10000.0,
        mock_cash=10000.0,
        max_total_open_risk_pct=0.10,
        max_short_notional_pct=0.50,
    )

    assert result["portfolio_status_counts"]["portfolio_needs_human_review"] == 1
    assert result["portfolio_review_reason_counts"]["short_stock_requires_borrow_margin_and_account_review"] == 1


def test_account_backed_portfolio_readiness_uses_snapshot_budget() -> None:
    ready_item = {
        "symbol": "NVDA",
        "selected_expression": {"instrument": "stock", "strategy": "stock_long"},
        "execution_readiness": {
            "readiness_status": "ready",
            "human_review_required": False,
            "review_reasons": [],
            "ticket_draft": {
                "ticket_type": "stock",
                "order_action": "BUY",
                "estimated_notional": 1000.0,
                "stop_loss_proxy": 20.0,
            },
        },
    }
    account_snapshot = {
        "snapshot_version": "ibkr-account-read-only-snapshot-v1",
        "generated_at": "2026-06-30T00:00:00+00:00",
        "selected_account": "DU123456",
        "equity": 10000.0,
        "cash": 10000.0,
        "buying_power": 20000.0,
        "position_count": 0,
        "open_order_count": 0,
        "errors": [],
    }

    result = build_account_backed_portfolio_readiness_report(
        {
            "execution_readiness_version": "test",
            "source_rows": 1,
            "source_selected_allocations": 1,
            "ticket_drafts": 1,
            "actionable_ticket_drafts": 1,
            "readiness_items": [ready_item],
        },
        account_snapshot,
        equity_haircut_pct=0.10,
        cash_haircut_pct=0.20,
        max_approved_tickets=5,
    )
    report = account_backed_portfolio_readiness_markdown(result)

    assert result["account_backed_readiness_version"] == "paper-account-backed-portfolio-readiness-v1"
    assert result["mock_account"]["equity"] == 9000.0
    assert result["mock_account"]["cash"] == 8000.0
    assert result["account_snapshot"]["raw_equity"] == 10000.0
    assert result["portfolio_status_counts"]["portfolio_ready"] == 1
    assert "Paper Account-Backed Portfolio Readiness Report" in report


def test_account_scaled_position_sizing_reduces_stock_ticket_to_fit_small_account() -> None:
    ready_item = {
        "symbol": "NVDA",
        "selected_expression": {"instrument": "stock", "strategy": "stock_long"},
        "execution_readiness": {
            "readiness_status": "ready",
            "human_review_required": False,
            "review_reasons": [],
            "checks": [],
            "ticket_draft": {
                "ticket_type": "stock",
                "order_action": "BUY",
                "quantity": 10.0,
                "estimated_notional": 1000.0,
                "stop_loss_proxy": 20.0,
            },
        },
    }
    account_snapshot = {
        "snapshot_version": "ibkr-account-read-only-snapshot-v1",
        "generated_at": "2026-06-30T00:00:00+00:00",
        "selected_account": "DU123456",
        "equity": 1000.0,
        "cash": 1000.0,
        "buying_power": 2000.0,
        "position_count": 0,
        "open_order_count": 0,
        "errors": [],
    }

    result = build_account_scaled_position_sizing_report(
        {
            "execution_readiness_version": "test",
            "source_rows": 1,
            "source_selected_allocations": 1,
            "ticket_drafts": 1,
            "actionable_ticket_drafts": 1,
            "readiness_items": [ready_item],
        },
        account_snapshot,
        max_approved_tickets=5,
    )
    report = account_scaled_position_sizing_markdown(result)
    sized_item = result["sized_execution_readiness"]["readiness_items"][0]
    sized_ticket = sized_item["execution_readiness"]["ticket_draft"]

    assert result["sizing_status_counts"]["sized"] == 1
    assert sized_ticket["quantity"] < 10.0
    assert sized_ticket["stop_loss_proxy"] <= result["limits"]["single_trade_risk"]
    assert result["account_backed_portfolio_readiness"]["approved_ticket_count"] == 1
    assert "Paper Account-Scaled Position Sizing Report" in report


def test_account_scaled_position_sizing_blocks_option_when_one_contract_exceeds_budget() -> None:
    option_item = {
        "symbol": "TSLA",
        "selected_expression": {"instrument": "option", "strategy": "bull_call_debit_spread"},
        "execution_readiness": {
            "readiness_status": "needs_human_review",
            "human_review_required": True,
            "review_reasons": ["short_option_leg_assignment_and_exercise_risk_requires_human_review"],
            "checks": [],
            "ticket_draft": {
                "ticket_type": "option_combo_debit_spread",
                "order_action": "BUY_COMBO",
                "quantity": 1,
                "estimated_max_loss": 200.0,
                "estimated_max_profit": 300.0,
                "estimated_net_debit": 2.0,
                "legs": [],
            },
        },
    }
    account_snapshot = {
        "snapshot_version": "ibkr-account-read-only-snapshot-v1",
        "generated_at": "2026-06-30T00:00:00+00:00",
        "selected_account": "DU123456",
        "equity": 1000.0,
        "cash": 1000.0,
        "position_count": 0,
        "open_order_count": 0,
        "errors": [],
    }

    result = build_account_scaled_position_sizing_report(
        {
            "execution_readiness_version": "test",
            "source_rows": 1,
            "source_selected_allocations": 1,
            "ticket_drafts": 1,
            "actionable_ticket_drafts": 1,
            "readiness_items": [option_item],
        },
        account_snapshot,
    )
    sized_item = result["sized_execution_readiness"]["readiness_items"][0]

    assert result["sizing_status_counts"]["not_sized"] == 1
    assert result["sizing_reason_counts"]["minimum_option_contract_exceeds_account_risk_budget"] == 1
    assert sized_item["execution_readiness"]["ticket_draft"] is None
    assert result["account_backed_portfolio_readiness"]["approved_ticket_count"] == 0


def test_order_preview_builds_what_if_safe_stock_preview_from_sized_ticket() -> None:
    ready_item = {
        "symbol": "NFLX",
        "selected_expression": {"instrument": "stock", "strategy": "stock_long"},
        "execution_readiness": {
            "readiness_status": "ready",
            "human_review_required": False,
            "review_reasons": [],
            "checks": [],
            "ticket_draft": {
                "ticket_type": "stock",
                "order_action": "BUY",
                "quantity": 10.0,
                "estimated_notional": 1000.0,
                "stop_loss_proxy": 20.0,
            },
        },
    }
    account_snapshot = {
        "snapshot_version": "ibkr-account-read-only-snapshot-v1",
        "generated_at": "2026-06-30T00:00:00+00:00",
        "selected_account": "DU123456",
        "equity": 1000.0,
        "cash": 1000.0,
        "position_count": 0,
        "open_order_count": 0,
        "errors": [],
    }
    sizing = build_account_scaled_position_sizing_report(
        {
            "execution_readiness_version": "test",
            "source_rows": 1,
            "source_selected_allocations": 1,
            "ticket_drafts": 1,
            "actionable_ticket_drafts": 1,
            "readiness_items": [ready_item],
        },
        account_snapshot,
        max_approved_tickets=5,
    )

    preview = build_order_preview_report(sizing, max_preview_items=5)
    report = order_preview_markdown(preview)
    item = preview["preview_items"][0]

    assert preview["order_preview_version"] == "paper-order-preview-margin-whatif-v1"
    assert preview["preview_item_count"] == 1
    assert item["ibkr_what_if_order_preview"]["what_if"] is True
    assert item["ibkr_what_if_order_preview"]["transmit"] is False
    assert item["contract_preview"]["sec_type"] == "STK"
    assert item["local_margin_estimate"]["estimated_initial_margin_change"] > 0
    assert preview["safety"]["ibkr_order_submitted"] is False
    assert "Paper Order Preview / Margin What-If Queue" in report


def test_autonomous_execution_gate_discharges_fractional_but_blocks_short_by_default() -> None:
    preview = {
        "order_preview_version": "paper-order-preview-margin-whatif-v1",
        "preview_item_count": 2,
        "account_snapshot": {"effective_cash": 100.0},
        "preview_items": [
            {
                "preview_status": "preview_needs_human_review",
                "symbol": "NFLX",
                "ticket_type": "stock",
                "ticket_draft": {"ticket_type": "stock", "order_action": "BUY", "quantity": 0.01, "estimated_notional": 1.0, "stop_loss_proxy": 0.02},
                "contract_preview": {"sec_type": "STK", "symbol": "NFLX", "exchange": "SMART", "currency": "USD"},
                "ibkr_what_if_order_preview": {"action": "BUY", "source_order_action": "BUY", "total_quantity": 0.01, "what_if": True, "transmit": False},
                "local_margin_estimate": {"estimated_initial_margin_change": 1.0, "estimated_cash_impact": -1.0},
            },
            {
                "preview_status": "preview_needs_human_review",
                "symbol": "ORCL",
                "ticket_type": "stock",
                "ticket_draft": {"ticket_type": "stock", "order_action": "SELL_SHORT", "quantity": 0.02, "estimated_notional": 2.0, "stop_loss_proxy": 0.04},
                "contract_preview": {"sec_type": "STK", "symbol": "ORCL", "exchange": "SMART", "currency": "USD"},
                "ibkr_what_if_order_preview": {"action": "SELL", "source_order_action": "SELL_SHORT", "total_quantity": 0.02, "what_if": True, "transmit": False},
                "local_margin_estimate": {"estimated_initial_margin_change": 3.0, "estimated_cash_impact": 0.0},
            },
        ],
    }

    result = build_autonomous_execution_gate_report(
        preview,
        allow_fractional_shares=True,
        allow_short_stock=False,
        require_short_availability_for_shorts=False,
        max_auto_orders=5,
    )
    report = autonomous_execution_gate_markdown(result)

    assert result["auto_ready_count"] == 1
    assert result["auto_gate_status_counts"]["auto_execution_ready"] == 1
    assert result["auto_gate_status_counts"]["auto_execution_blocked"] == 1
    assert result["auto_gate_block_reason_counts"]["short_stock_policy"] == 1
    assert result["safety"]["ibkr_order_submitted"] is False
    assert "Paper Autonomous Execution Gate Report" in report


def test_autonomous_execution_gate_allows_short_with_short_availability_evidence() -> None:
    preview = {
        "order_preview_version": "paper-order-preview-margin-whatif-v1",
        "preview_item_count": 1,
        "account_snapshot": {"effective_cash": 100.0},
        "preview_items": [
            {
                "preview_status": "preview_needs_human_review",
                "symbol": "ORCL",
                "ticket_type": "stock",
                "ticket_draft": {"ticket_type": "stock", "order_action": "SELL_SHORT", "quantity": 0.02, "estimated_notional": 2.0, "stop_loss_proxy": 0.04},
                "contract_preview": {"sec_type": "STK", "symbol": "ORCL", "exchange": "SMART", "currency": "USD"},
                "ibkr_what_if_order_preview": {"action": "SELL", "source_order_action": "SELL_SHORT", "total_quantity": 0.02, "what_if": True, "transmit": False},
                "local_margin_estimate": {"estimated_initial_margin_change": 3.0, "estimated_cash_impact": 0.0},
            }
        ],
    }
    short_availability = {
        "short_availability_version": "ibkr-short-availability-v1",
        "rows": [{"symbol": "ORCL", "shortable_shares": 1000, "shortable_score": 3.0}],
    }

    result = build_autonomous_execution_gate_report(
        preview,
        allow_fractional_shares=True,
        allow_short_stock=True,
        require_short_availability_for_shorts=True,
        short_availability_payload=short_availability,
        max_auto_orders=5,
    )

    assert result["auto_ready_count"] == 1
    assert result["auto_gate_status_counts"]["auto_execution_ready"] == 1
    assert "short_availability_confirmed" not in result["auto_gate_block_reason_counts"]


def test_execution_intent_ledger_creates_stable_deduplicated_intents() -> None:
    auto_gate = {
        "autonomous_execution_gate_version": "paper-autonomous-execution-gate-v1",
        "used_budget": {"cumulative_initial_margin": 1.0},
        "gate_parameters": {"allow_fractional_shares": True},
        "auto_execution_items": [
            {
                "auto_execution_status": "auto_execution_ready",
                "symbol": "NFLX",
                "ticket_type": "stock",
                "action": "BUY",
                "quantity": 0.01,
                "checks": [{"name": "test", "passed": True}],
                "local_margin_estimate": {"estimated_initial_margin_change": 1.0},
                "order_intent": {
                    "contract_preview": {"sec_type": "STK", "symbol": "NFLX", "exchange": "SMART", "currency": "USD"},
                    "order_preview": {"action": "BUY", "source_order_action": "BUY", "total_quantity": 0.01, "order_type": "MKT", "account": "DU123", "what_if": True, "transmit": False},
                },
                "source_preview_item": {"source_score_id": 1, "source_outcome_id": 2},
            }
        ],
    }

    first = build_execution_intent_ledger_report(auto_gate, ledger_run_id="test_run")
    second = build_execution_intent_ledger_report(auto_gate, existing_ledger_payload=first, ledger_run_id="test_run_2")
    report = execution_intent_ledger_markdown(first)

    assert first["intent_count"] == 1
    assert first["intents"][0]["state"] == "pending_paper_execution"
    assert second["intents"][0]["intent_id"] == first["intents"][0]["intent_id"]
    assert second["intents"][0]["state"] == "duplicate_existing_intent"
    assert first["safety"]["ibkr_order_submitted"] is False
    assert "Paper Execution Intent Ledger" in report


def test_execution_intent_ledger_gates_ordinary_alpha_with_session_transition_release_governor() -> None:
    blocked_release_governor = {
        "session_transition_release_governor_version": "session-transition-release-governor-v1",
        "release_status": "blocked",
        "ordinary_alpha_release_allowed": False,
        "max_new_entry_size_multiplier": 0.0,
        "reason": "transition_gate_not_release_candidate",
        "controls": {
            "ordinary_alpha_release_allowed": False,
            "max_new_entry_size_multiplier": 0.0,
        },
        "checks": [{"name": "transition_gate_is_release_candidate", "passed": False}],
    }
    auto_gate = {
        "autonomous_execution_gate_version": "market-data-operating-auto-gate-v1",
        "auto_execution_items": [
            {
                "auto_execution_status": "auto_execution_ready",
                "symbol": "NFLX",
                "ticket_type": "stock",
                "action": "BUY",
                "quantity": 1.0,
                "local_margin_estimate": {"estimated_initial_margin_change": 100.0},
                "order_intent": {
                    "contract_preview": {"sec_type": "STK", "symbol": "NFLX"},
                    "order_preview": {"action": "BUY", "total_quantity": 1.0, "order_type": "MKT"},
                },
                "source_preview_item": {"source_score_id": 1, "source_outcome_id": 2},
            },
            {
                "auto_execution_status": "auto_execution_ready",
                "symbol": "SPY",
                "ticket_type": "option",
                "action": "BUY",
                "quantity": 1.0,
                "intent_role": "hedge",
                "strategy": "protective_put",
                "local_margin_estimate": {"estimated_initial_margin_change": 100.0},
                "order_intent": {
                    "contract_preview": {"sec_type": "OPT", "symbol": "SPY"},
                    "order_preview": {"action": "BUY", "total_quantity": 1.0, "order_type": "MKT"},
                },
                "source_preview_item": {"source_score_id": 3, "source_outcome_id": 4},
            },
        ],
    }

    result = build_execution_intent_ledger_report(
        auto_gate,
        session_transition_release_governor_payload=blocked_release_governor,
        ledger_run_id="release_gate_test",
    )
    report = execution_intent_ledger_markdown(result)

    assert result["pending_paper_execution_count"] == 1
    assert result["blocked_by_session_transition_release_governor_count"] == 1
    assert result["intent_state_counts"]["blocked_by_session_transition_release_governor"] == 1
    assert result["intents"][0]["state"] == "blocked_by_session_transition_release_governor"
    assert result["intents"][1]["state"] == "pending_paper_execution"
    assert result["intents"][1]["session_transition_release_governor_exempt"] is True
    assert "Session-transition release governor" in report


def test_paper_order_router_fills_pending_intents_and_skips_existing_routes() -> None:
    ledger = {
        "execution_intent_ledger_version": "paper-execution-intent-ledger-v1",
        "intent_count": 1,
        "intents": [
            {
                "intent_id": "intent_test",
                "state": "pending_paper_execution",
                "symbol": "NFLX",
                "action": "BUY",
                "quantity": 0.01,
                "order_preview": {"order_type": "MKT", "total_quantity": 0.01},
                "local_margin_estimate": {
                    "estimate_method": "local_stock_cash_conservative_v1",
                    "estimated_cash_impact": -1.0,
                    "estimated_initial_margin_change": 1.0,
                },
            }
        ],
    }

    first = build_paper_order_router_report(ledger, router_run_id="router_test")
    second = build_paper_order_router_report(ledger, existing_router_payload=first, router_run_id="router_test_2")
    report = paper_order_router_markdown(first)

    assert first["paper_order_count"] == 1
    assert first["paper_fill_count"] == 1
    assert first["paper_positions"][0]["symbol"] == "NFLX"
    assert first["paper_positions"][0]["long_quantity"] == 0.01
    assert first["paper_orders"][0]["fill_price"] == 100.0
    assert second["paper_order_count"] == 0
    assert second["skipped_counts"]["intent_already_paper_routed"] == 1
    assert first["safety"]["ibkr_order_submitted"] is False
    assert "Paper Order Router / Simulated Fill Report" in report


def test_paper_order_router_requires_session_transition_release_governor_snapshot_for_alpha() -> None:
    blocked_release_governor = {
        "session_transition_release_governor_version": "session-transition-release-governor-v1",
        "release_status": "blocked",
        "ordinary_alpha_release_allowed": False,
        "max_new_entry_size_multiplier": 0.0,
        "reason": "transition_gate_not_release_candidate",
        "controls": {
            "ordinary_alpha_release_allowed": False,
            "max_new_entry_size_multiplier": 0.0,
        },
    }
    stale_ledger = {
        "execution_intent_ledger_version": "paper-execution-intent-ledger-v2-portfolio-risk-gated",
        "intent_count": 1,
        "pending_paper_execution_count": 1,
        "intents": [
            {
                "intent_id": "intent_stale_alpha",
                "state": "pending_paper_execution",
                "symbol": "NFLX",
                "action": "BUY",
                "quantity": 1.0,
                "order_preview": {"order_type": "MKT", "total_quantity": 1.0},
                "local_margin_estimate": {"estimated_cash_impact": -100.0, "estimated_initial_margin_change": 100.0},
            }
        ],
    }

    result = build_paper_order_router_report(
        stale_ledger,
        session_transition_release_governor_payload=blocked_release_governor,
        require_session_transition_release_governor=True,
        router_run_id="router_release_block",
    )
    report = paper_order_router_markdown(result)

    assert result["paper_order_count"] == 0
    assert result["paper_fill_count"] == 0
    assert result["skipped_counts"]["missing_session_transition_release_governor_on_intent"] == 1
    assert result["session_transition_release_governor_applied"] is True
    assert result["session_transition_release_governor_allowed"] is False
    assert "Session-transition release governor" in report


def test_paper_order_router_can_use_required_mark_prices_for_fill_reference() -> None:
    ledger = {
        "execution_intent_ledger_version": "paper-execution-intent-ledger-v1",
        "intent_count": 2,
        "intents": [
            {
                "intent_id": "intent_long",
                "state": "pending_paper_execution",
                "symbol": "NFLX",
                "action": "BUY",
                "quantity": 0.01,
                "order_preview": {"order_type": "MKT", "total_quantity": 0.01},
                "local_margin_estimate": {"estimated_cash_impact": -1.0, "estimated_initial_margin_change": 1.0},
            },
            {
                "intent_id": "intent_short",
                "state": "pending_paper_execution",
                "symbol": "TSLA",
                "action": "SELL_SHORT",
                "quantity": 0.02,
                "order_preview": {"order_type": "MKT", "total_quantity": 0.02},
                "local_margin_estimate": {
                    "estimate_method": "local_short_stock_conservative_v1",
                    "estimated_initial_margin_change": 3.0,
                },
            },
        ],
    }
    marks = {
        "marks": [
            {"symbol": "NFLX", "mark_price": 105.0, "mark_source": "ibkr_last"},
            {"symbol": "TSLA", "mark_price": 95.0, "mark_source": "ibkr_midpoint"},
        ]
    }

    result = build_paper_order_router_report(
        ledger,
        marks_payload=marks,
        fill_price_source="require_marks",
        router_run_id="router_mark_test",
    )
    missing = build_paper_order_router_report(
        ledger,
        marks_payload={"marks": [{"symbol": "NFLX", "mark_price": 105.0}]},
        fill_price_source="require_marks",
        router_run_id="router_missing_mark_test",
    )

    assert result["paper_order_count"] == 2
    assert result["input_mark_count"] == 2
    assert result["fill_reference_counts"] == {"ibkr_last": 1, "ibkr_midpoint": 1}
    assert result["paper_orders"][0]["fill_price"] == 105.0
    assert result["paper_orders"][0]["local_estimate_price"] == 100.0
    assert result["paper_positions"][0]["long_avg_price"] == 105.0
    assert result["paper_positions"][1]["short_avg_price"] == 95.0
    assert missing["paper_order_count"] == 1
    assert missing["skipped_counts"]["missing_required_fill_mark"] == 1


def test_paper_order_router_can_require_portfolio_risk_gated_intents() -> None:
    ungated_ledger = {
        "execution_intent_ledger_version": "paper-execution-intent-ledger-v1",
        "intent_count": 1,
        "intents": [
            {
                "intent_id": "intent_ungated",
                "state": "pending_paper_execution",
                "symbol": "NFLX",
                "action": "BUY",
                "quantity": 10.0,
                "order_preview": {"order_type": "MKT", "total_quantity": 10.0},
                "local_margin_estimate": {"estimated_cash_impact": -1000.0, "estimated_initial_margin_change": 1000.0},
            }
        ],
    }
    gated_ledger = {
        "execution_intent_ledger_version": "paper-execution-intent-ledger-v2-portfolio-risk-gated",
        "intent_count": 1,
        "portfolio_risk_gate": {
            "action": "allow_with_monitor_haircut",
            "portfolio_risk_state": "monitor",
            "entry_size_multiplier": 0.25,
        },
        "intents": [
            {
                "intent_id": "intent_gated",
                "state": "pending_paper_execution",
                "symbol": "NFLX",
                "action": "BUY",
                "quantity": 2.5,
                "original_quantity": 10.0,
                "portfolio_risk_gate_applied": True,
                "portfolio_risk_gate_snapshot": {
                    "action": "allow_with_monitor_haircut",
                    "portfolio_risk_state": "monitor",
                    "entry_size_multiplier": 0.25,
                },
                "order_preview": {"order_type": "MKT", "total_quantity": 2.5},
                "local_margin_estimate": {"estimated_cash_impact": -250.0, "estimated_initial_margin_change": 250.0},
            }
        ],
    }

    blocked = build_paper_order_router_report(
        ungated_ledger,
        require_portfolio_risk_gate=True,
        router_run_id="router_require_gate_block",
    )
    filled = build_paper_order_router_report(
        gated_ledger,
        require_portfolio_risk_gate=True,
        router_run_id="router_require_gate_fill",
    )
    report = paper_order_router_markdown(filled)

    assert blocked["paper_order_count"] == 0
    assert blocked["skipped_counts"]["missing_required_portfolio_risk_gate"] == 1
    assert filled["paper_order_count"] == 1
    assert filled["require_portfolio_risk_gate"] is True
    assert filled["source_has_portfolio_risk_gate"] is True
    assert filled["paper_orders"][0]["quantity"] == 2.5
    assert filled["paper_orders"][0]["estimated_reference_notional"] == 250.0
    assert filled["paper_positions"][0]["long_quantity"] == 2.5
    assert filled["paper_orders"][0]["portfolio_risk_gate_snapshot"]["portfolio_risk_state"] == "monitor"
    assert "Require portfolio risk gate" in report


def test_operating_final_preflight_blocks_stale_non_operating_router_ledger() -> None:
    legacy_ledger = {
        "execution_intent_ledger_version": "paper-execution-intent-ledger-v2-portfolio-risk-gated",
        "source_autonomous_execution_gate_version": "paper-autonomous-execution-gate-v1",
        "intent_count": 1,
        "pending_paper_execution_count": 1,
        "intents": [
            {
                "intent_id": "intent_legacy",
                "state": "pending_paper_execution",
                "symbol": "NFLX",
                "action": "BUY",
                "quantity": 0.01,
                "order_preview": {"order_type": "MKT", "total_quantity": 0.01},
                "local_margin_estimate": {"estimated_cash_impact": -1.0, "estimated_initial_margin_change": 1.0},
            }
        ],
    }
    operating_gate = {
        "autonomous_execution_gate_version": "market-data-operating-auto-gate-v1",
        "generated_at": "2026-06-30T16:00:00+00:00",
        "source_preview_item_count": 25,
        "auto_ready_count": 0,
        "auto_gate_status_counts": {"auto_execution_blocked": 10, "auto_execution_capacity_blocked": 15},
        "auto_gate_block_reason_counts": {"market_data_defensive_mode_blocks_ordinary_alpha_entries": 10},
        "market_data_operating_mode": "risk_reduction_only",
        "market_data_defensive_mode": {
            "mode": "risk_reduction_only",
            "severity": "red",
            "controls": {
                "ordinary_alpha_entries_allowed": False,
                "hedge_or_defensive_entries_allowed": False,
                "exit_or_risk_reduction_allowed": True,
                "protective_option_analysis_allowed": True,
                "protective_option_ticketing_allowed": False,
                "max_new_entry_size_multiplier": 0.0,
            },
        },
        "protective_analysis_packet": {"candidate_count": 25, "ticketing_allowed": False},
    }

    preflight = build_operating_final_preflight_report(legacy_ledger, operating_gate)
    result = build_paper_order_router_report(
        legacy_ledger,
        operating_auto_gate_payload=operating_gate,
        require_operating_preflight=True,
        router_run_id="router_operating_block",
    )
    report = paper_order_router_markdown(result)

    assert preflight["allowed"] is False
    assert "intent_ledger_not_derived_from_operating_auto_gate" in preflight["reasons"]
    assert "operating_auto_gate_has_no_auto_ready_intents" in preflight["reasons"]
    assert result["paper_order_count"] == 0
    assert result["paper_fill_count"] == 0
    assert result["skipped_counts"]["operating_final_preflight_blocked"] == 1
    assert result["operating_final_preflight_applied"] is True
    assert result["operating_final_preflight_allowed"] is False
    assert result["safety"]["operating_final_preflight_applied"] is True
    assert "Operating final preflight" in report


def test_operating_final_preflight_allows_matching_operating_router_ledger() -> None:
    operating_ledger = {
        "execution_intent_ledger_version": "paper-execution-intent-ledger-v2-portfolio-risk-gated",
        "source_autonomous_execution_gate_version": "market-data-operating-auto-gate-v1",
        "intent_count": 1,
        "pending_paper_execution_count": 1,
        "intents": [
            {
                "intent_id": "intent_operating",
                "state": "pending_paper_execution",
                "symbol": "NFLX",
                "action": "BUY",
                "quantity": 0.01,
                "order_preview": {"order_type": "MKT", "total_quantity": 0.01},
                "local_margin_estimate": {"estimated_cash_impact": -1.0, "estimated_initial_margin_change": 1.0},
            }
        ],
    }
    operating_gate = {
        "autonomous_execution_gate_version": "market-data-operating-auto-gate-v1",
        "generated_at": "2026-06-30T16:00:00+00:00",
        "source_preview_item_count": 1,
        "auto_ready_count": 1,
        "market_data_operating_mode": "normal",
        "market_data_defensive_mode": {
            "mode": "normal",
            "severity": "green",
            "controls": {
                "ordinary_alpha_entries_allowed": True,
                "hedge_or_defensive_entries_allowed": True,
                "exit_or_risk_reduction_allowed": True,
                "protective_option_analysis_allowed": True,
                "protective_option_ticketing_allowed": True,
                "max_new_entry_size_multiplier": 1.0,
            },
        },
        "protective_analysis_packet": {"candidate_count": 0, "ticketing_allowed": True},
    }

    result = build_paper_order_router_report(
        operating_ledger,
        operating_auto_gate_payload=operating_gate,
        require_operating_preflight=True,
        router_run_id="router_operating_allow",
    )

    assert result["operating_final_preflight_applied"] is True
    assert result["operating_final_preflight_allowed"] is True
    assert result["paper_order_count"] == 1
    assert result["paper_fill_count"] == 1
    assert result["paper_orders"][0]["intent_id"] == "intent_operating"


def test_paper_position_mark_report_calculates_long_short_pnl_and_exit_signals() -> None:
    router = {
        "paper_order_router_version": "paper-order-router-sim-fill-v1",
        "router_run_id": "router_test",
        "paper_positions": [
            {
                "symbol": "NFLX",
                "long_quantity": 0.01,
                "short_quantity": 0.0,
                "long_avg_price": 100.0,
                "short_avg_price": None,
                "net_quantity": 0.01,
            },
            {
                "symbol": "TSLA",
                "long_quantity": 0.0,
                "short_quantity": 0.02,
                "long_avg_price": None,
                "short_avg_price": 100.0,
                "net_quantity": -0.02,
            },
        ],
    }

    result = build_paper_position_mark_report(
        router,
        inline_marks={"NFLX": 105.0, "TSLA": 95.0},
        take_profit_pct=0.03,
        stop_loss_pct=0.02,
        mark_run_id="mark_test",
    )
    report = paper_position_mark_markdown(result)

    assert result["marked_position_count"] == 2
    assert result["missing_mark_count"] == 0
    assert result["exit_signal_counts"]["take_profit"] == 2
    assert round(result["totals"]["paper_unrealized_pnl"], 6) == 0.15
    nflx = next(row for row in result["marked_positions"] if row["symbol"] == "NFLX")
    tsla = next(row for row in result["marked_positions"] if row["symbol"] == "TSLA")
    assert nflx["suggested_close_action"] == "SELL"
    assert tsla["suggested_close_action"] == "BUY_TO_COVER"
    assert "Paper Position Mark-to-Market / Exit Signal Report" in report


def test_paper_position_mark_timeseries_report_tracks_pnl_delta_and_latest_positions() -> None:
    router = {
        "paper_order_router_version": "paper-order-router-sim-fill-v1",
        "router_run_id": "router_series_test",
        "paper_positions": [
            {
                "symbol": "NFLX",
                "long_quantity": 0.01,
                "short_quantity": 0.0,
                "long_avg_price": 100.0,
                "short_avg_price": None,
                "net_quantity": 0.01,
            },
            {
                "symbol": "TSLA",
                "long_quantity": 0.0,
                "short_quantity": 0.02,
                "long_avg_price": None,
                "short_avg_price": 100.0,
                "net_quantity": -0.02,
            },
        ],
    }
    first_marks = {
        "generated_at": "2026-06-30T00:00:00+00:00",
        "marks": [
            {"symbol": "NFLX", "mark_price": 100.0, "mark_source": "ibkr_last"},
            {"symbol": "TSLA", "mark_price": 100.0, "mark_source": "ibkr_last"},
        ],
    }
    second_marks = {
        "generated_at": "2026-06-30T00:01:00+00:00",
        "marks": [
            {"symbol": "NFLX", "mark_price": 104.0, "mark_source": "ibkr_last"},
            {"symbol": "TSLA", "mark_price": 98.0, "mark_source": "ibkr_last"},
        ],
    }

    result = build_paper_position_mark_timeseries_report(
        router,
        [first_marks, second_marks],
        take_profit_pct=0.03,
        stop_loss_pct=0.02,
        series_run_id="series_test",
    )
    report = paper_position_mark_timeseries_markdown(result)

    assert result["snapshot_count"] == 2
    assert result["ledger_row_count"] == 4
    assert result["pnl_summary"]["first_unrealized_pnl"] == 0.0
    assert round(result["pnl_summary"]["final_unrealized_pnl"], 6) == 0.08
    assert round(result["pnl_summary"]["delta_unrealized_pnl"], 6) == 0.08
    assert result["aggregate_exit_signal_counts"]["hold"] == 3
    assert result["aggregate_exit_signal_counts"]["take_profit"] == 1
    assert len(result["latest_positions"]) == 2
    assert "Paper Position Mark-to-Market Time Series Ledger" in report


def test_paper_exit_intent_ledger_creates_close_intents_and_deduplicates() -> None:
    timeseries = {
        "paper_position_mark_timeseries_version": "paper-position-mark-timeseries-v1",
        "series_run_id": "series_exit_test",
        "source_router_run_id": "router_exit_test",
        "latest_positions": [
            {
                "sequence": 2,
                "mark_generated_at": "2026-06-30T00:01:00+00:00",
                "symbol": "NFLX",
                "net_quantity": 0.01,
                "mark_price": 105.0,
                "mark_source": "ibkr_last",
                "paper_unrealized_pnl": 0.05,
                "paper_unrealized_pnl_pct": 0.05,
                "exit_signal": "take_profit",
                "suggested_close_action": "SELL",
                "suggested_close_quantity": 0.01,
            },
            {
                "sequence": 2,
                "mark_generated_at": "2026-06-30T00:01:00+00:00",
                "symbol": "TSLA",
                "net_quantity": -0.02,
                "mark_price": 99.0,
                "mark_source": "ibkr_last",
                "paper_unrealized_pnl": 0.02,
                "paper_unrealized_pnl_pct": 0.01,
                "exit_signal": "hold",
                "suggested_close_action": "BUY_TO_COVER",
                "suggested_close_quantity": 0.02,
            },
        ],
    }

    first = build_paper_exit_intent_ledger_report(timeseries, ledger_run_id="exit_test")
    second = build_paper_exit_intent_ledger_report(
        timeseries,
        existing_exit_ledger_payload=first,
        ledger_run_id="exit_test_2",
    )
    report = paper_exit_intent_ledger_markdown(first)

    assert first["exit_intent_count"] == 1
    assert first["pending_exit_intent_count"] == 1
    assert first["exit_intents"][0]["state"] == "pending_paper_exit"
    assert first["exit_intents"][0]["action"] == "SELL"
    assert first["skipped_counts"]["exit_signal:hold"] == 1
    assert second["exit_intents"][0]["state"] == "duplicate_existing_exit_intent"
    assert second["pending_exit_intent_count"] == 0
    assert second["skipped_counts"]["duplicate_existing_exit_intent"] == 1
    assert first["safety"]["ibkr_order_submitted"] is False
    assert "Paper Exit Intent Ledger" in report


def test_paper_exit_router_creates_close_fills_realized_pnl_and_skips_duplicates() -> None:
    exit_ledger = {
        "paper_exit_intent_ledger_version": "paper-exit-intent-ledger-v1",
        "ledger_run_id": "exit_ledger_test",
        "exit_intent_count": 2,
        "exit_intents": [
            {
                "exit_intent_id": "exit_long",
                "state": "pending_paper_exit",
                "symbol": "NFLX",
                "exit_signal": "take_profit",
                "action": "SELL",
                "quantity": 0.01,
                "mark_price": 105.0,
                "paper_unrealized_pnl": 0.05,
                "order_preview": {"order_type": "MKT", "what_if": True, "transmit": False},
            },
            {
                "exit_intent_id": "exit_short",
                "state": "pending_paper_exit",
                "symbol": "TSLA",
                "exit_signal": "stop_loss",
                "action": "BUY_TO_COVER",
                "quantity": 0.02,
                "mark_price": 102.0,
                "paper_unrealized_pnl": -0.04,
                "order_preview": {"order_type": "MKT", "what_if": True, "transmit": False},
            },
        ],
    }

    first = build_paper_exit_router_report(
        exit_ledger,
        router_run_id="exit_router_test",
        slippage_bps=10.0,
        commission_per_exit=0.01,
    )
    second = build_paper_exit_router_report(
        exit_ledger,
        existing_exit_router_payload=first,
        router_run_id="exit_router_test_2",
    )
    report = paper_exit_router_markdown(first)

    assert first["paper_exit_order_count"] == 2
    assert first["paper_exit_fill_count"] == 2
    assert first["order_state_counts"]["paper_exit_filled"] == 2
    assert first["paper_exit_orders"][0]["fill_price"] == 104.895
    assert first["paper_exit_orders"][1]["fill_price"] == 102.102
    assert round(first["totals"]["realized_pnl"], 6) == -0.01309
    assert second["paper_exit_order_count"] == 0
    assert second["skipped_counts"]["exit_intent_already_paper_routed"] == 2
    assert first["safety"]["ibkr_order_submitted"] is False
    assert "Paper Exit Router / Simulated Close Fill Report" in report


def test_paper_portfolio_state_reconciles_entries_exits_and_latest_marks() -> None:
    entry_router = {
        "paper_order_router_version": "paper-order-router-sim-fill-v1",
        "router_run_id": "entry_router_test",
        "paper_positions": [
            {
                "symbol": "NFLX",
                "long_quantity": 0.03,
                "short_quantity": 0.0,
                "long_cost_basis": 3.0,
                "short_proceeds": 0.0,
                "long_avg_price": 100.0,
                "short_avg_price": None,
            },
            {
                "symbol": "TSLA",
                "long_quantity": 0.0,
                "short_quantity": 0.04,
                "long_cost_basis": 0.0,
                "short_proceeds": 8.0,
                "long_avg_price": None,
                "short_avg_price": 200.0,
            },
        ],
    }
    exit_router = {
        "paper_exit_router_version": "paper-exit-router-sim-close-fill-v1",
        "router_run_id": "exit_router_test",
        "realized_pnl_ledger": [
            {
                "exit_intent_id": "exit_nflx",
                "symbol": "NFLX",
                "action": "SELL",
                "closed_quantity": 0.01,
                "realized_pnl": 0.20,
            },
            {
                "exit_intent_id": "exit_tsla",
                "symbol": "TSLA",
                "action": "BUY_TO_COVER",
                "closed_quantity": 0.02,
                "realized_pnl": -0.10,
            },
        ],
    }
    timeseries = {
        "paper_position_mark_timeseries_version": "paper-position-mark-timeseries-v1",
        "series_run_id": "series_test",
        "latest_positions": [
            {"symbol": "NFLX", "mark_price": 110.0, "mark_source": "ibkr_last", "mark_generated_at": "t2"},
            {"symbol": "TSLA", "mark_price": 190.0, "mark_source": "ibkr_last", "mark_generated_at": "t2"},
        ],
    }

    result = build_paper_portfolio_state_report(
        entry_router,
        exit_router_payload=exit_router,
        latest_timeseries_payload=timeseries,
        state_run_id="state_test",
    )
    report = paper_portfolio_state_markdown(result)

    assert result["status_counts"]["open"] == 2
    assert round(result["totals"]["realized_pnl"], 6) == 0.10
    assert round(result["totals"]["unrealized_pnl"], 6) == 0.40
    assert round(result["totals"]["total_pnl"], 6) == 0.50
    nflx = next(row for row in result["portfolio_rows"] if row["symbol"] == "NFLX")
    tsla = next(row for row in result["portfolio_rows"] if row["symbol"] == "TSLA")
    assert round(nflx["open_long_quantity"], 6) == 0.02
    assert round(tsla["open_short_quantity"], 6) == 0.02
    assert result["safety"]["ibkr_order_submitted"] is False
    assert "Paper Portfolio State Reconciler" in report


def test_paper_portfolio_risk_state_flags_breaches_and_allows_clean_state() -> None:
    portfolio_state = {
        "paper_portfolio_state_version": "paper-portfolio-state-reconciler-v1",
        "state_run_id": "state_risk_test",
        "totals": {
            "open_market_value": 150.0,
            "realized_pnl": 0.0,
            "unrealized_pnl": -20.0,
            "total_pnl": -20.0,
        },
        "portfolio_rows": [
            {
                "symbol": "NFLX",
                "position_status": "open",
                "open_long_quantity": 1.0,
                "open_short_quantity": 0.0,
                "open_market_value": 100.0,
                "open_basis": 90.0,
                "latest_mark_price": 100.0,
                "realized_pnl": 0.0,
                "unrealized_pnl": -10.0,
                "total_pnl": -10.0,
            },
            {
                "symbol": "TSLA",
                "position_status": "open",
                "open_long_quantity": 0.0,
                "open_short_quantity": 0.5,
                "open_market_value": 50.0,
                "open_basis": 55.0,
                "latest_mark_price": 100.0,
                "short_avg_price": 110.0,
                "realized_pnl": 0.0,
                "unrealized_pnl": -10.0,
                "total_pnl": -10.0,
            },
        ],
    }

    breached = build_paper_portfolio_risk_state_report(
        portfolio_state,
        max_gross_exposure=120.0,
        max_symbol_exposure=80.0,
        max_short_exposure=40.0,
        max_unrealized_loss=15.0,
        max_total_loss=15.0,
        max_open_symbols=1,
        risk_run_id="risk_breach_test",
    )
    clean = build_paper_portfolio_risk_state_report(
        portfolio_state,
        max_gross_exposure=1000.0,
        max_symbol_exposure=1000.0,
        max_short_exposure=1000.0,
        max_unrealized_loss=1000.0,
        max_total_loss=1000.0,
        max_open_symbols=10,
        risk_run_id="risk_clean_test",
    )
    report = paper_portfolio_risk_state_markdown(breached)

    assert breached["risk_state"] == "reduce_risk"
    assert breached["new_entries_allowed"] is False
    assert breached["breach_count"] >= 5
    assert any(item["name"] == "gross_exposure" for item in breached["breaches"])
    assert any(row["risk_status"] == "breach" for row in breached["row_risk"])
    assert clean["risk_state"] == "within_limits"
    assert clean["new_entries_allowed"] is True
    assert clean["safety"]["ibkr_order_submitted"] is False
    assert "Paper Portfolio Risk State" in report


def test_execution_intent_ledger_applies_portfolio_risk_gate_block_and_monitor_haircut() -> None:
    auto_gate = {
        "autonomous_execution_gate_version": "paper-autonomous-execution-gate-v1",
        "auto_execution_items": [
            {
                "auto_execution_status": "auto_execution_ready",
                "symbol": "NFLX",
                "ticket_type": "stock",
                "action": "BUY",
                "quantity": 10.0,
                "checks": [{"name": "test", "passed": True}],
                "local_margin_estimate": {
                    "estimate_method": "local_cash_stock_v1",
                    "estimated_cash_impact": -1000.0,
                    "estimated_initial_margin_change": 1000.0,
                    "estimated_maintenance_margin_change": 1000.0,
                },
                "order_intent": {
                    "contract_preview": {"symbol": "NFLX", "sec_type": "STK"},
                    "order_preview": {
                        "action": "BUY",
                        "source_order_action": "BUY",
                        "order_type": "MKT",
                        "total_quantity": 10.0,
                        "what_if": True,
                        "transmit": False,
                    },
                },
                "source_preview_item": {
                    "source_score_id": 101,
                    "source_outcome_id": 201,
                },
            }
        ],
    }
    reduce_risk = {
        "paper_portfolio_risk_state_version": "paper-portfolio-risk-state-v1",
        "risk_run_id": "risk_reduce",
        "risk_state": "reduce_risk",
        "new_entries_allowed": False,
        "breach_count": 1,
        "warning_count": 0,
        "recommended_action": "pause_new_entries_and_reduce_flagged_exposures",
    }
    monitor = {
        "paper_portfolio_risk_state_version": "paper-portfolio-risk-state-v1",
        "risk_run_id": "risk_monitor",
        "risk_state": "monitor",
        "new_entries_allowed": True,
        "breach_count": 0,
        "warning_count": 1,
        "recommended_action": "allow_new_entries_with_caution_and_refresh_marks",
    }

    blocked = build_execution_intent_ledger_report(
        auto_gate,
        portfolio_risk_state_payload=reduce_risk,
        ledger_run_id="ledger_reduce",
    )
    haircut = build_execution_intent_ledger_report(
        auto_gate,
        portfolio_risk_state_payload=monitor,
        monitor_size_multiplier=0.25,
        ledger_run_id="ledger_monitor",
    )
    report = execution_intent_ledger_markdown(haircut)

    assert blocked["portfolio_risk_gate"]["action"] == "block_new_entries"
    assert blocked["pending_paper_execution_count"] == 0
    assert blocked["blocked_by_portfolio_risk_state_count"] == 1
    assert blocked["intents"][0]["state"] == "blocked_by_portfolio_risk_state"
    assert haircut["portfolio_risk_gate"]["action"] == "allow_with_monitor_haircut"
    assert haircut["pending_paper_execution_count"] == 1
    assert haircut["intents"][0]["state"] == "pending_paper_execution"
    assert haircut["intents"][0]["portfolio_risk_gate_applied"] is True
    assert haircut["intents"][0]["quantity"] == 2.5
    assert haircut["intents"][0]["order_preview"]["total_quantity"] == 2.5
    assert haircut["intents"][0]["local_margin_estimate"]["estimated_cash_impact"] == -250.0
    assert haircut["safety"]["ibkr_order_submitted"] is False
    assert "Portfolio risk gate" in report


def test_paper_portfolio_risk_loop_links_router_marks_state_and_next_gate() -> None:
    router = {
        "paper_order_router_version": "paper-order-router-sim-fill-v1",
        "router_run_id": "router_loop_test",
        "paper_order_count": 1,
        "paper_fill_count": 1,
        "paper_position_count": 1,
        "paper_positions": [
            {
                "symbol": "NFLX",
                "long_quantity": 1.0,
                "short_quantity": 0.0,
                "long_cost_basis": 100.0,
                "short_proceeds": 0.0,
                "long_avg_price": 100.0,
                "short_avg_price": None,
                "net_quantity": 1.0,
            }
        ],
    }
    marks = [
        {"marks": [{"symbol": "NFLX", "mark_price": 100.0, "mark_source": "fill_mark", "mark_timestamp": "t1"}]},
        {"marks": [{"symbol": "NFLX", "mark_price": 101.0, "mark_source": "refresh_mark", "mark_timestamp": "t2"}]},
    ]

    result = build_paper_portfolio_risk_loop_report(
        router,
        marks,
        max_gross_exposure=1000.0,
        max_symbol_exposure=1000.0,
        max_short_exposure=1000.0,
        max_unrealized_loss=1000.0,
        max_total_loss=1000.0,
        loop_run_id="loop_test",
    )
    report = paper_portfolio_risk_loop_markdown(result)

    assert result["paper_portfolio_risk_loop_version"] == "paper-portfolio-risk-loop-v1"
    assert result["mark_timeseries"]["snapshot_count"] == 2
    assert result["portfolio_state"]["status_counts"]["open"] == 1
    assert round(result["portfolio_state"]["totals"]["unrealized_pnl"], 6) == 1.0
    assert result["portfolio_risk_state"]["risk_state"] == "within_limits"
    assert result["next_entry_gate"]["action"] == "allow_new_entries"
    assert result["safety"]["ibkr_order_submitted"] is False
    assert "Paper Portfolio Risk Loop" in report


def test_paper_cycle_runner_runs_entry_router_and_next_risk_loop() -> None:
    auto_gate = {
        "autonomous_execution_gate_version": "paper-autonomous-execution-gate-v1",
        "auto_execution_items": [
            {
                "auto_execution_status": "auto_execution_ready",
                "symbol": "NFLX",
                "ticket_type": "stock",
                "action": "BUY",
                "quantity": 1.0,
                "checks": [{"name": "test", "passed": True}],
                "local_margin_estimate": {
                    "estimate_method": "local_stock_cash_conservative_v1",
                    "estimated_cash_impact": -100.0,
                    "estimated_initial_margin_change": 100.0,
                    "estimated_maintenance_margin_change": 100.0,
                },
                "order_intent": {
                    "contract_preview": {"symbol": "NFLX", "sec_type": "STK"},
                    "order_preview": {
                        "action": "BUY",
                        "source_order_action": "BUY",
                        "order_type": "MKT",
                        "total_quantity": 1.0,
                        "what_if": True,
                        "transmit": False,
                    },
                },
                "source_preview_item": {"source_score_id": 1, "source_outcome_id": 2},
            }
        ],
    }
    risk_state = {
        "paper_portfolio_risk_state_version": "paper-portfolio-risk-state-v1",
        "risk_run_id": "risk_cycle_test",
        "risk_state": "within_limits",
        "new_entries_allowed": True,
        "breach_count": 0,
        "warning_count": 0,
        "recommended_action": "allow_new_entries",
    }
    fill_marks = {"marks": [{"symbol": "NFLX", "mark_price": 100.0, "mark_source": "fill_mark"}]}
    loop_marks = [
        {"marks": [{"symbol": "NFLX", "mark_price": 100.0, "mark_source": "fill_mark"}]},
        {"marks": [{"symbol": "NFLX", "mark_price": 101.0, "mark_source": "refresh_mark"}]},
    ]

    result = build_paper_cycle_runner_report(
        auto_gate,
        risk_state,
        fill_marks,
        loop_marks,
        cycle_run_id="cycle_test",
        max_gross_exposure=1000.0,
        max_symbol_exposure=1000.0,
        max_short_exposure=1000.0,
        max_unrealized_loss=1000.0,
        max_total_loss=1000.0,
    )
    report = paper_cycle_runner_markdown(result)

    assert result["paper_cycle_runner_version"] == "paper-cycle-runner-v1"
    assert result["entry_ledger"]["pending_paper_execution_count"] == 1
    assert result["entry_router"]["paper_fill_count"] == 1
    assert result["risk_loop"]["portfolio_state"]["status_counts"]["open"] == 1
    assert result["next_portfolio_risk_state"]["risk_state"] == "within_limits"
    assert result["next_entry_gate"]["action"] == "allow_new_entries"
    assert result["cycle_summary"]["post_cycle_total_pnl"] == 1.0
    assert result["safety"]["ibkr_order_submitted"] is False
    assert "Paper Cycle Runner" in report


def test_paper_cycle_runner_uses_session_transition_release_governor() -> None:
    auto_gate = {
        "autonomous_execution_gate_version": "market-data-operating-auto-gate-v1",
        "auto_execution_items": [
            {
                "auto_execution_status": "auto_execution_ready",
                "symbol": "NFLX",
                "ticket_type": "stock",
                "action": "BUY",
                "quantity": 1.0,
                "local_margin_estimate": {
                    "estimate_method": "local_stock_cash_conservative_v1",
                    "estimated_cash_impact": -100.0,
                    "estimated_initial_margin_change": 100.0,
                },
                "order_intent": {
                    "contract_preview": {"symbol": "NFLX", "sec_type": "STK"},
                    "order_preview": {"action": "BUY", "order_type": "MKT", "total_quantity": 1.0},
                },
                "source_preview_item": {"source_score_id": 1, "source_outcome_id": 2},
            }
        ],
    }
    risk_state = {
        "paper_portfolio_risk_state_version": "paper-portfolio-risk-state-v1",
        "risk_state": "within_limits",
        "new_entries_allowed": True,
        "breach_count": 0,
        "warning_count": 0,
        "recommended_action": "allow_new_entries",
    }
    release_governor = {
        "session_transition_release_governor_version": "session-transition-release-governor-v1",
        "release_status": "blocked",
        "ordinary_alpha_release_allowed": False,
        "max_new_entry_size_multiplier": 0.0,
        "controls": {"ordinary_alpha_release_allowed": False, "max_new_entry_size_multiplier": 0.0},
        "reason": "transition_gate_not_release_candidate",
    }
    fill_marks = {"marks": [{"symbol": "NFLX", "mark_price": 100.0, "mark_source": "fill_mark"}]}
    loop_marks = [{"marks": [{"symbol": "NFLX", "mark_price": 100.0, "mark_source": "fill_mark"}]}]

    result = build_paper_cycle_runner_report(
        auto_gate,
        risk_state,
        fill_marks,
        loop_marks,
        session_transition_release_governor_payload=release_governor,
        require_session_transition_release_governor=True,
        cycle_run_id="cycle_release_gate_test",
        max_gross_exposure=1000.0,
        max_symbol_exposure=1000.0,
        max_short_exposure=1000.0,
        max_unrealized_loss=1000.0,
        max_total_loss=1000.0,
    )
    report = paper_cycle_runner_markdown(result)

    assert result["entry_ledger"]["blocked_by_session_transition_release_governor_count"] == 1
    assert result["entry_ledger"]["pending_paper_execution_count"] == 0
    assert result["entry_router"]["paper_fill_count"] == 0
    assert result["cycle_summary"]["entry_release_status"] == "blocked"
    assert result["cycle_summary"]["release_governor_blocked_entry_intent_count"] == 1
    assert result["cycle_summary"]["session_transition_release_governor_applied"] is True
    assert result["cycle_summary"]["session_transition_release_governor_allowed"] is False
    assert "Release-governor blocked entry intents" in report


def test_paper_cycle_runner_applies_operating_final_preflight() -> None:
    auto_gate = {
        "autonomous_execution_gate_version": "paper-autonomous-execution-gate-v1",
        "auto_execution_items": [
            {
                "auto_execution_status": "auto_execution_ready",
                "symbol": "NFLX",
                "ticket_type": "stock",
                "action": "BUY",
                "quantity": 1.0,
                "checks": [{"name": "test", "passed": True}],
                "local_margin_estimate": {
                    "estimate_method": "local_stock_cash_conservative_v1",
                    "estimated_cash_impact": -100.0,
                    "estimated_initial_margin_change": 100.0,
                    "estimated_maintenance_margin_change": 100.0,
                },
                "order_intent": {
                    "contract_preview": {"symbol": "NFLX", "sec_type": "STK"},
                    "order_preview": {
                        "action": "BUY",
                        "source_order_action": "BUY",
                        "order_type": "MKT",
                        "total_quantity": 1.0,
                        "what_if": True,
                        "transmit": False,
                    },
                },
                "source_preview_item": {"source_score_id": 1, "source_outcome_id": 2},
            }
        ],
    }
    operating_gate = {
        "autonomous_execution_gate_version": "market-data-operating-auto-gate-v1",
        "auto_ready_count": 0,
        "market_data_operating_mode": "risk_reduction_only",
        "market_data_defensive_mode": {
            "mode": "risk_reduction_only",
            "controls": {
                "ordinary_alpha_entries_allowed": False,
                "hedge_or_defensive_entries_allowed": False,
                "exit_or_risk_reduction_allowed": True,
                "protective_option_ticketing_allowed": False,
            },
        },
        "auto_gate_status_counts": {"blocked": 1},
        "auto_gate_block_reason_counts": {"market_data_operating_mode_risk_reduction_only": 1},
    }
    risk_state = {
        "paper_portfolio_risk_state_version": "paper-portfolio-risk-state-v1",
        "risk_run_id": "risk_cycle_preflight_test",
        "risk_state": "within_limits",
        "new_entries_allowed": True,
        "breach_count": 0,
        "warning_count": 0,
        "recommended_action": "allow_new_entries",
    }
    fill_marks = {"marks": [{"symbol": "NFLX", "mark_price": 100.0, "mark_source": "fill_mark"}]}
    loop_marks = [{"marks": [{"symbol": "NFLX", "mark_price": 100.0, "mark_source": "fill_mark"}]}]

    result = build_paper_cycle_runner_report(
        auto_gate,
        risk_state,
        fill_marks,
        loop_marks,
        operating_auto_gate_payload=operating_gate,
        require_operating_preflight=True,
        cycle_run_id="cycle_preflight_test",
        max_gross_exposure=1000.0,
        max_symbol_exposure=1000.0,
        max_short_exposure=1000.0,
        max_unrealized_loss=1000.0,
        max_total_loss=1000.0,
    )
    report = paper_cycle_runner_markdown(result)

    assert result["cycle_summary"]["operating_final_preflight_applied"] is True
    assert result["cycle_summary"]["operating_final_preflight_allowed"] is False
    assert result["entry_router"]["paper_order_count"] == 0
    assert result["entry_router"]["paper_fill_count"] == 0
    assert result["entry_router"]["skipped_counts"]["operating_final_preflight_blocked"] == 1
    assert "operating_gate_blocks_ordinary_alpha_entries" in result["cycle_summary"]["operating_final_preflight_reasons"]
    assert "Operating final preflight" in report


def test_operating_defensive_action_plan_builds_exit_and_protective_candidates() -> None:
    operating_gate = {
        "autonomous_execution_gate_version": "market-data-operating-auto-gate-v1",
        "market_data_defensive_mode": {
            "mode": "risk_reduction_only",
            "severity": "critical",
            "controls": {
                "ordinary_alpha_entries_allowed": False,
                "hedge_or_defensive_entries_allowed": True,
                "exit_or_risk_reduction_allowed": True,
                "protective_option_analysis_allowed": True,
                "protective_option_ticketing_allowed": False,
                "allowed_option_structures": [
                    "protective_put_analysis_only",
                    "collar_analysis_only",
                    "put_spread_hedge_analysis_only",
                    "covered_call_risk_reduction_analysis_only",
                ],
            },
        },
        "market_data_operating_mode": {
            "mode": "risk_reduction_only",
            "severity": "critical",
        },
        "protective_analysis_packet": {
            "candidate_count": 1,
            "ticketing_allowed": False,
            "allowed_option_structures": [
                "protective_put_analysis_only",
                "collar_analysis_only",
            ],
            "candidates": [
                {
                    "symbol": "MSFT",
                    "source_action": "BUY",
                    "analysis_status": "analysis_only",
                    "ticketing_allowed": False,
                    "allowed_option_structures": ["protective_put_analysis_only"],
                    "reason": "market_data_defensive_mode",
                }
            ],
        },
    }
    portfolio = {
        "paper_portfolio_state_version": "paper-portfolio-state-v1",
        "portfolio_rows": [
            {
                "symbol": "NFLX",
                "open_net_quantity": 2.0,
                "open_market_value": 200.0,
                "latest_mark_price": 100.0,
                "unrealized_pnl": -12.5,
                "total_pnl": -12.5,
                "position_status": "open",
            }
        ],
    }

    result = build_operating_defensive_action_plan(
        operating_gate,
        paper_portfolio_state_payload=portfolio,
        max_candidates=10,
        max_actions_per_symbol=4,
        now="2026-07-01T00:00:00+00:00",
    )
    report = operating_defensive_action_plan_markdown(result)
    strategies = {row["strategy"] for row in result["top_actions"]}

    assert result["operating_defensive_action_plan_version"] == "operating-defensive-action-plan-v1"
    assert result["operating_mode"] == "risk_reduction_only"
    assert result["action_count"] >= 3
    assert "sell_or_reduce_long" in strategies
    assert "protective_put" in strategies
    assert "collar" in strategies
    assert result["summary"]["requires_option_chain_count"] >= 2
    assert result["safety"]["ibkr_order_submitted"] is False
    assert all((row["safety"]["ticket_created"] is False and row["safety"]["transmit_enabled"] is False) for row in result["top_actions"])
    assert "Operating Defensive Action Plan" in report


def test_operating_defensive_intent_ledger_blocks_without_required_gates() -> None:
    plan = {
        "operating_defensive_action_plan_version": "operating-defensive-action-plan-v1",
        "generated_at": "2026-07-01T00:00:00+00:00",
        "top_actions": [
            {
                "symbol": "NFLX",
                "action_type": "exit_or_reduce_position",
                "strategy": "sell_or_reduce_long",
                "priority_score": 125,
                "reason": "risk reduction",
                "requires_option_chain": False,
                "requires_live_option_quotes": False,
                "requires_margin_whatif": True,
                "estimated_notional": 200.0,
                "source_position": {
                    "symbol": "NFLX",
                    "open_net_quantity": 2.0,
                    "open_long_quantity": 2.0,
                    "latest_mark_price": 100.0,
                    "latest_mark_source": "ibkr_last",
                },
                "safety": {"ticket_created": False, "ibkr_order_submitted": False, "transmit_enabled": False},
            },
            {
                "symbol": "MSFT",
                "action_type": "protective_option_analysis",
                "strategy": "protective_put",
                "priority_score": 95,
                "reason": "protective analysis",
                "requires_option_chain": True,
                "requires_live_option_quotes": True,
                "requires_margin_whatif": True,
                "source_protective_candidate": {"symbol": "MSFT"},
                "safety": {"ticket_created": False, "ibkr_order_submitted": False, "transmit_enabled": False},
            },
        ],
    }

    result = build_operating_defensive_intent_ledger(
        plan,
        max_intents=5,
        ledger_run_id="strict-test",
        now="2026-07-01T00:01:00+00:00",
    )
    report = operating_defensive_intent_ledger_markdown(result)

    assert result["operating_defensive_intent_ledger_version"] == "operating-defensive-intent-ledger-v1"
    assert result["candidate_count"] == 2
    assert result["pending_defensive_paper_execution_count"] == 0
    assert result["blocked_count"] == 2
    assert result["summary"]["missing_whatif_block_count"] == 2
    assert result["summary"]["missing_option_quote_block_count"] == 1
    assert "missing_broker_margin_whatif" in result["defensive_intents"][0]["block_reasons"]
    assert "analysis_only_not_a_risk_reduction_order" in result["defensive_intents"][1]["block_reasons"]
    assert result["safety"]["ibkr_order_submitted"] is False
    assert "Operating Defensive Intent Ledger" in report


def test_operating_defensive_intent_ledger_can_create_dry_paper_risk_reduction_intent() -> None:
    operating_gate = {
        "autonomous_execution_gate_version": "market-data-operating-auto-gate-v1",
        "market_data_defensive_mode": {
            "mode": "risk_reduction_only",
            "severity": "critical",
            "controls": {
                "ordinary_alpha_entries_allowed": False,
                "hedge_or_defensive_entries_allowed": True,
                "exit_or_risk_reduction_allowed": True,
                "protective_option_analysis_allowed": True,
                "protective_option_ticketing_allowed": False,
                "allowed_option_structures": ["protective_put_analysis_only"],
            },
        },
        "market_data_operating_mode": {"mode": "risk_reduction_only", "severity": "critical"},
        "protective_analysis_packet": {
            "candidate_count": 1,
            "ticketing_allowed": False,
            "allowed_option_structures": ["protective_put_analysis_only"],
            "candidates": [{"symbol": "MSFT", "analysis_status": "analysis_only"}],
        },
    }
    portfolio = {
        "paper_portfolio_state_version": "paper-portfolio-state-v1",
        "portfolio_rows": [
            {
                "symbol": "NFLX",
                "open_net_quantity": 2.0,
                "open_long_quantity": 2.0,
                "open_market_value": 200.0,
                "latest_mark_price": 100.0,
                "latest_mark_source": "ibkr_last",
                "position_status": "open",
            }
        ],
    }
    plan = build_operating_defensive_action_plan(
        operating_gate,
        paper_portfolio_state_payload=portfolio,
        max_candidates=10,
        now="2026-07-01T00:00:00+00:00",
    )

    result = build_operating_defensive_intent_ledger(
        plan,
        allow_missing_whatif=True,
        max_intents=5,
        ledger_run_id="dry-paper-test",
        now="2026-07-01T00:01:00+00:00",
    )
    pending = result["pending_intents"]

    assert result["pending_defensive_paper_execution_count"] == 1
    assert pending[0]["symbol"] == "NFLX"
    assert pending[0]["action"] == "SELL"
    assert pending[0]["quantity"] == 2.0
    assert pending[0]["order_preview"]["what_if"] is True
    assert pending[0]["order_preview"]["transmit"] is False
    assert result["summary"]["analysis_only_block_count"] >= 1
    assert result["safety"]["transmit_enabled"] is False


def test_operating_defensive_gate_evidence_builds_whatif_preview_and_unlocks_strict_ledger() -> None:
    plan = {
        "operating_defensive_action_plan_version": "operating-defensive-action-plan-v1",
        "generated_at": "2026-07-01T00:00:00+00:00",
        "top_actions": [
            {
                "symbol": "NFLX",
                "action_type": "exit_or_reduce_position",
                "strategy": "sell_or_reduce_long",
                "priority_score": 125,
                "reason": "risk reduction",
                "requires_option_chain": False,
                "requires_live_option_quotes": False,
                "requires_margin_whatif": True,
                "estimated_notional": 200.0,
                "source_position": {
                    "symbol": "NFLX",
                    "open_net_quantity": 2.0,
                    "open_long_quantity": 2.0,
                    "latest_mark_price": 100.0,
                    "latest_mark_source": "ibkr_last",
                },
                "safety": {"ticket_created": False, "ibkr_order_submitted": False, "transmit_enabled": False},
            }
        ],
    }
    whatif = {
        "whatif_preview_version": "ibkr-order-whatif-preview-v1",
        "responses": [
            {
                "order_id": 1001,
                "symbol": "NFLX",
                "contract": {"symbol": "NFLX", "sec_type": "STK"},
                "order": {"action": "SELL", "total_quantity": 2.0, "what_if": True, "transmit": False},
                "order_state": {"status": "PreSubmitted", "initMarginChange": "0.00"},
            }
        ],
        "safety": {"what_if_only": True, "transmit": False, "ibkr_order_submitted": False},
    }

    preview = build_operating_defensive_order_preview(plan, account="DU123", now="2026-07-01T00:01:00+00:00")
    evidence = build_operating_defensive_gate_evidence(
        plan,
        broker_whatif_payload=whatif,
        account="DU123",
        now="2026-07-01T00:02:00+00:00",
    )
    report = operating_defensive_gate_evidence_markdown(evidence)
    strict_ledger = build_operating_defensive_intent_ledger(
        plan,
        broker_whatif_payload=evidence,
        max_intents=5,
        ledger_run_id="m188-strict-test",
        now="2026-07-01T00:03:00+00:00",
    )

    assert preview["order_preview_version"] == "operating-defensive-order-preview-v1"
    assert preview["preview_item_count"] == 1
    assert preview["preview_items"][0]["contract_preview"]["sec_type"] == "STK"
    assert preview["preview_items"][0]["ibkr_what_if_order_preview"]["what_if"] is True
    assert preview["preview_items"][0]["ibkr_what_if_order_preview"]["transmit"] is False
    assert evidence["operating_defensive_gate_evidence_version"] == "operating-defensive-gate-evidence-v1"
    assert evidence["summary"]["whatif_ok_count"] == 1
    assert evidence["what_if_results"][0]["status"] == "ok"
    assert strict_ledger["pending_defensive_paper_execution_count"] == 1
    assert strict_ledger["pending_intents"][0]["symbol"] == "NFLX"
    assert strict_ledger["safety"]["ibkr_order_submitted"] is False
    assert "Operating Defensive Gate Evidence" in report


def test_operating_defensive_gate_evidence_records_option_quote_and_chain_status() -> None:
    plan = {
        "operating_defensive_action_plan_version": "operating-defensive-action-plan-v1",
        "generated_at": "2026-07-01T00:00:00+00:00",
        "top_actions": [
            {
                "symbol": "MSFT",
                "action_type": "watchlist_defensive_analysis",
                "strategy": "protective_put",
                "priority_score": 100,
                "reason": "protective analysis",
                "requires_option_chain": True,
                "requires_live_option_quotes": True,
                "requires_margin_whatif": False,
                "source_protective_candidate": {"symbol": "MSFT"},
                "safety": {"ticket_created": False, "ibkr_order_submitted": False, "transmit_enabled": False},
            }
        ],
    }
    quote_snapshot = {
        "ibkr_option_quote_snapshot_version": "ibkr-option-quote-snapshot-v1",
        "quotes": [{"symbol": "MSFT", "right": "P", "strike": 400.0, "quote_status": "quoted", "bid": 1.0, "ask": 1.2}],
    }
    chain_scan = {
        "ibkr_option_chain_scan_version": "ibkr-option-chain-scan-v1",
        "candidate_contracts": [{"symbol": "MSFT", "right": "P", "strike": 400.0}],
    }

    evidence = build_operating_defensive_gate_evidence(
        plan,
        option_quote_payload=quote_snapshot,
        option_chain_payload=chain_scan,
        now="2026-07-01T00:02:00+00:00",
    )

    assert evidence["summary"]["option_quote_ok_count"] == 1
    assert evidence["option_quote_results"][0]["status"] == "ok"
    assert evidence["option_quote_results"][0]["option_chain_available"] is True
    assert evidence["option_quote_results"][0]["option_quote_available"] is True
    assert evidence["safety"]["ibkr_order_submitted"] is False


def test_operating_defensive_gate_retry_detects_read_only_and_recommends_operator_action() -> None:
    plan = {
        "operating_defensive_action_plan_version": "operating-defensive-action-plan-v1",
        "top_actions": [
            {
                "symbol": "NFLX",
                "action_type": "exit_or_reduce_position",
                "strategy": "sell_or_reduce_long",
                "requires_margin_whatif": True,
                "source_position": {"open_long_quantity": 2.0},
            },
            {
                "symbol": "MSFT",
                "action_type": "watchlist_defensive_analysis",
                "strategy": "protective_put",
                "requires_option_chain": True,
                "requires_live_option_quotes": True,
            },
        ],
    }
    order_preview = build_operating_defensive_order_preview(plan, account="DU123")
    whatif_attempt = {
        "whatif_preview_version": "ibkr-order-whatif-preview-v1",
        "requested_count": 1,
        "response_count": 0,
        "errors": [
            {
                "request_id": 1,
                "code": 321,
                "message": "Error validating request.-'bC' : cause - The API interface is currently in Read-Only mode.",
            }
        ],
    }
    option_attempt = {
        "ibkr_option_quote_snapshot_version": "ibkr-option-quote-snapshot-v1",
        "requested_quotes": 6,
        "quoted_contracts": 0,
        "status_counts": {"last_only": 4, "no_quote": 2},
    }
    evidence = build_operating_defensive_gate_evidence(
        plan,
        broker_whatif_payload=whatif_attempt,
        option_quote_payload=option_attempt,
    )
    ledger = build_operating_defensive_intent_ledger(
        plan,
        broker_whatif_payload=evidence,
        option_quote_gate_payload=evidence,
    )

    report = build_operating_defensive_gate_retry_report(
        defensive_action_plan_payload=plan,
        order_preview_payload=order_preview,
        whatif_attempt_payloads=[whatif_attempt],
        option_quote_attempt_payloads=[option_attempt],
        gate_evidence_payload=evidence,
        intent_ledger_payload=ledger,
        now="2026-07-01T00:04:00+00:00",
    )
    markdown = operating_defensive_gate_retry_markdown(report)

    assert detect_ibkr_read_only_mode(whatif_attempt) is True
    assert defensive_option_symbols_from_plan(plan) == ["MSFT"]
    assert report["summary"]["read_only_mode_detected"] is True
    assert "ibkr_api_read_only_mode_blocks_broker_whatif" in report["summary"]["blockers"]
    assert "option_bid_ask_quotes_unavailable_after_retry" in report["summary"]["blockers"]
    assert report["next_actions"][0]["action_id"] == "disable_ibkr_api_read_only_mode_then_retry_whatif"
    assert report["safety"]["transmit_enabled"] is False
    assert "Operating Defensive Gate Retry Report" in markdown


def test_operating_defensive_gate_retry_reports_success_when_evidence_unblocks_strict_ledger() -> None:
    plan = {
        "operating_defensive_action_plan_version": "operating-defensive-action-plan-v1",
        "top_actions": [
            {
                "symbol": "NFLX",
                "action_type": "exit_or_reduce_position",
                "strategy": "sell_or_reduce_long",
                "priority_score": 125,
                "requires_margin_whatif": True,
                "source_position": {"open_long_quantity": 2.0, "open_net_quantity": 2.0},
            }
        ],
    }
    whatif_attempt = {
        "whatif_preview_version": "ibkr-order-whatif-preview-v1",
        "requested_count": 1,
        "response_count": 1,
        "responses": [{"symbol": "NFLX", "order_state": {"status": "PreSubmitted"}}],
        "errors": [],
    }
    evidence = build_operating_defensive_gate_evidence(plan, broker_whatif_payload=whatif_attempt)
    ledger = build_operating_defensive_intent_ledger(plan, broker_whatif_payload=evidence)

    report = build_operating_defensive_gate_retry_report(
        defensive_action_plan_payload=plan,
        order_preview_payload=build_operating_defensive_order_preview(plan),
        whatif_attempt_payloads=[whatif_attempt],
        gate_evidence_payload=evidence,
        intent_ledger_payload=ledger,
        now="2026-07-01T00:05:00+00:00",
    )

    assert report["summary"]["best_whatif_response_count"] == 1
    assert report["summary"]["strict_ledger_pending_count"] == 1
    assert report["summary"]["blockers"] == []
    assert report["next_actions"] == []


def test_operating_defensive_gate_retry_defers_option_quotes_outside_regular_session() -> None:
    plan = {
        "operating_defensive_action_plan_version": "operating-defensive-action-plan-v1",
        "top_actions": [
            {
                "symbol": "AMD",
                "action_type": "watchlist_defensive_analysis",
                "strategy": "protective_put",
                "requires_option_chain": True,
                "requires_live_option_quotes": True,
            }
        ],
    }
    option_attempt = {
        "ibkr_option_quote_snapshot_version": "ibkr-option-quote-snapshot-v1",
        "requested_quotes": 6,
        "quoted_contracts": 0,
        "status_counts": {"close_only": 6},
    }
    session_gate = evaluate_option_quote_session_gate(now="2026-07-01T17:30:00-04:00")

    report = build_operating_defensive_gate_retry_report(
        defensive_action_plan_payload=plan,
        option_quote_attempt_payloads=[option_attempt],
        retry_policy={"option_quote_session_gate": session_gate, "regular_session_only_option_quotes": True},
        now="2026-07-01T21:30:00+00:00",
    )
    markdown = operating_defensive_gate_retry_markdown(report)

    assert session_gate["session"] == "after_hours"
    assert session_gate["allowed"] is False
    assert report["summary"]["option_quote_deferred"] is True
    assert "option_quote_deferred_until_regular_session" in report["summary"]["blockers"]
    assert report["next_actions"][0]["action_id"] == "run_option_quote_retry_next_regular_session"
    assert "Option quote session gate" in markdown


def test_option_quote_resume_plan_preserves_whatif_and_points_to_next_regular_open() -> None:
    retry = {
        "operating_defensive_gate_retry_version": "operating-defensive-gate-retry-v2",
        "generated_at": "2026-07-01T01:57:47+00:00",
        "summary": {
            "best_whatif_response_count": 5,
            "best_option_quoted_count": 0,
            "strict_ledger_pending_count": 5,
            "strict_ledger_blocked_count": 20,
            "option_quote_deferred": True,
            "option_quote_session": "after_hours",
        },
    }
    plan = {
        "operating_defensive_action_plan_version": "operating-defensive-action-plan-v1",
        "top_actions": [{"symbol": "AMD", "requires_option_chain": True, "requires_live_option_quotes": True}],
    }

    next_open = next_regular_option_session_open(now="2026-07-01T17:30:00-04:00")
    report = build_option_quote_resume_plan(
        latest_retry_payload=retry,
        defensive_action_plan_payload=plan,
        defensive_action_plan_json="exports/m186_operating_defensive_action_plan.json",
        existing_whatif_json="exports/m190_whatif.json",
        existing_option_quote_json="exports/m188_quote.json",
        account="DU123",
        now="2026-07-01T17:30:00-04:00",
    )
    markdown = option_quote_resume_plan_markdown(report)

    assert next_open["status"] == "waiting_for_next_regular_open"
    assert report["status"] == "ready_for_next_regular_session"
    assert report["ready_for_regular_retry"] is True
    assert report["source_defensive_option_symbols"] == ["AMD"]
    assert report["summary"]["best_whatif_response_count"] == 5
    assert "operating-defensive-gate-retry" in report["resume_command"]
    assert "--regular-session-only-option-quotes" in report["resume_command"]
    assert "Option Quote Resume Plan" in markdown


def test_ibkr_order_whatif_adapter_uses_safe_ibkr_required_flags_and_whole_share_proxy() -> None:
    preview_item = {
        "symbol": "GOOGL",
        "contract_preview": {"symbol": "GOOGL", "sec_type": "STK", "exchange": "SMART", "currency": "USD"},
        "ibkr_what_if_order_preview": {
            "action": "SELL",
            "order_type": "MKT",
            "total_quantity": 0.02,
            "tif": "DAY",
            "account": "DU123",
            "what_if": True,
            "transmit": False,
        },
    }

    order = _order_from_preview(preview_item)
    payload = _order_payload(order)

    assert order is not None
    assert payload["what_if"] is True
    assert payload["transmit"] is True
    assert payload["e_trade_only"] is False
    assert payload["firm_quote_only"] is False
    assert payload["source_total_quantity"] == 0.02
    assert payload["total_quantity"] == 1.0
    assert payload["quantity_normalized_for_ibkr_api"] is True
    assert payload["quantity_normalization_policy"] == "fractional_stock_quantity_rounded_up_to_whole_share_for_ibkr_api_whatif"


def test_paper_multi_cycle_runner_hands_off_risk_and_accumulates_portfolio() -> None:
    auto_gate = {
        "autonomous_execution_gate_version": "paper-autonomous-execution-gate-v1",
        "auto_execution_items": [
            {
                "auto_execution_status": "auto_execution_ready",
                "symbol": "NFLX",
                "ticket_type": "stock",
                "action": "BUY",
                "quantity": 1.0,
                "checks": [{"name": "test", "passed": True}],
                "local_margin_estimate": {
                    "estimate_method": "local_stock_cash_conservative_v1",
                    "estimated_cash_impact": -100.0,
                    "estimated_initial_margin_change": 100.0,
                    "estimated_maintenance_margin_change": 100.0,
                },
                "order_intent": {
                    "contract_preview": {"symbol": "NFLX", "sec_type": "STK"},
                    "order_preview": {
                        "action": "BUY",
                        "source_order_action": "BUY",
                        "order_type": "MKT",
                        "total_quantity": 1.0,
                        "what_if": True,
                        "transmit": False,
                    },
                },
                "source_preview_item": {"source_score_id": 1, "source_outcome_id": 2},
            }
        ],
    }
    risk_state = {
        "paper_portfolio_risk_state_version": "paper-portfolio-risk-state-v1",
        "risk_run_id": "risk_multi_cycle_test",
        "risk_state": "within_limits",
        "new_entries_allowed": True,
        "breach_count": 0,
        "warning_count": 0,
        "recommended_action": "allow_new_entries",
    }
    fill_marks = {"marks": [{"symbol": "NFLX", "mark_price": 100.0, "mark_source": "fill_mark"}]}
    loop_marks = [
        {"marks": [{"symbol": "NFLX", "mark_price": 100.0, "mark_source": "fill_mark"}]},
        {"marks": [{"symbol": "NFLX", "mark_price": 101.0, "mark_source": "refresh_mark"}]},
    ]

    repeated = build_paper_multi_cycle_runner_report(
        auto_gate,
        risk_state,
        fill_marks,
        loop_marks,
        cycle_count=2,
        multi_cycle_run_id="multi_cycle_repeat_test",
        max_gross_exposure=1000.0,
        max_symbol_exposure=1000.0,
        max_short_exposure=1000.0,
        max_unrealized_loss=1000.0,
        max_total_loss=1000.0,
    )
    deduped = build_paper_multi_cycle_runner_report(
        auto_gate,
        risk_state,
        fill_marks,
        loop_marks,
        cycle_count=2,
        dedupe_across_cycles=True,
        multi_cycle_run_id="multi_cycle_dedupe_test",
        max_gross_exposure=1000.0,
        max_symbol_exposure=1000.0,
        max_short_exposure=1000.0,
        max_unrealized_loss=1000.0,
        max_total_loss=1000.0,
    )
    report = paper_multi_cycle_runner_markdown(repeated)

    assert repeated["paper_multi_cycle_runner_version"] == "paper-multi-cycle-runner-v1"
    assert repeated["cycle_count_completed"] == 2
    assert repeated["multi_cycle_summary"]["total_new_paper_fills"] == 2
    assert repeated["multi_cycle_summary"]["final_cumulative_paper_fills"] == 2
    assert repeated["multi_cycle_summary"]["final_total_pnl"] == 2.0
    assert repeated["next_portfolio_risk_state"]["risk_state"] == "within_limits"
    assert deduped["cycle_summaries"][0]["paper_fills"] == 1
    assert deduped["cycle_summaries"][1]["paper_fills"] == 0
    assert deduped["multi_cycle_summary"]["final_cumulative_paper_fills"] == 1
    assert deduped["safety"]["ibkr_order_submitted"] is False
    assert "Paper Multi-Cycle Runner" in report


def test_paper_cycle_scheduler_runs_finite_ticks_and_carries_dedupe_state() -> None:
    auto_gate = {
        "autonomous_execution_gate_version": "paper-autonomous-execution-gate-v1",
        "auto_execution_items": [
            {
                "auto_execution_status": "auto_execution_ready",
                "symbol": "NFLX",
                "ticket_type": "stock",
                "action": "BUY",
                "quantity": 1.0,
                "checks": [{"name": "test", "passed": True}],
                "local_margin_estimate": {
                    "estimate_method": "local_stock_cash_conservative_v1",
                    "estimated_cash_impact": -100.0,
                    "estimated_initial_margin_change": 100.0,
                    "estimated_maintenance_margin_change": 100.0,
                },
                "order_intent": {
                    "contract_preview": {"symbol": "NFLX", "sec_type": "STK"},
                    "order_preview": {
                        "action": "BUY",
                        "source_order_action": "BUY",
                        "order_type": "MKT",
                        "total_quantity": 1.0,
                        "what_if": True,
                        "transmit": False,
                    },
                },
                "source_preview_item": {"source_score_id": 1, "source_outcome_id": 2},
            }
        ],
    }
    risk_state = {
        "paper_portfolio_risk_state_version": "paper-portfolio-risk-state-v1",
        "risk_run_id": "risk_scheduler_test",
        "risk_state": "within_limits",
        "new_entries_allowed": True,
        "breach_count": 0,
        "warning_count": 0,
        "recommended_action": "allow_new_entries",
    }
    fill_marks = {"marks": [{"symbol": "NFLX", "mark_price": 100.0, "mark_source": "fill_mark"}]}
    loop_marks = [
        {"marks": [{"symbol": "NFLX", "mark_price": 100.0, "mark_source": "fill_mark"}]},
        {"marks": [{"symbol": "NFLX", "mark_price": 101.0, "mark_source": "refresh_mark"}]},
    ]

    result = build_paper_cycle_scheduler_report(
        auto_gate,
        risk_state,
        fill_marks,
        loop_marks,
        max_runs=2,
        cycles_per_run=1,
        dedupe_across_cycles=True,
        carry_dedupe_state_across_runs=True,
        scheduler_run_id="scheduler_test",
        max_gross_exposure=1000.0,
        max_symbol_exposure=1000.0,
        max_short_exposure=1000.0,
        max_unrealized_loss=1000.0,
        max_total_loss=1000.0,
    )
    report = paper_cycle_scheduler_markdown(result)

    assert result["paper_cycle_scheduler_version"] == "paper-cycle-scheduler-v1"
    assert result["runs_completed"] == 2
    assert result["tick_summaries"][0]["total_new_paper_fills"] == 1
    assert result["tick_summaries"][1]["total_new_paper_fills"] == 0
    assert result["final_scheduler_summary"]["final_cumulative_paper_fills"] == 1
    assert result["final_scheduler_summary"]["final_risk_state"] == "within_limits"
    assert result["next_entry_gate"]["action"] == "allow_new_entries"
    assert result["safety"]["ibkr_order_submitted"] is False
    assert "Paper Cycle Scheduler" in report


def test_paper_cycle_scheduler_checkpoint_resumes_without_duplicate_fills() -> None:
    auto_gate = {
        "autonomous_execution_gate_version": "paper-autonomous-execution-gate-v1",
        "auto_execution_items": [
            {
                "auto_execution_status": "auto_execution_ready",
                "symbol": "MSFT",
                "ticket_type": "stock",
                "action": "BUY",
                "quantity": 1.0,
                "checks": [{"name": "test", "passed": True}],
                "local_margin_estimate": {
                    "estimate_method": "local_stock_cash_conservative_v1",
                    "estimated_cash_impact": -200.0,
                    "estimated_initial_margin_change": 200.0,
                    "estimated_maintenance_margin_change": 200.0,
                },
                "order_intent": {
                    "contract_preview": {"symbol": "MSFT", "sec_type": "STK"},
                    "order_preview": {
                        "action": "BUY",
                        "source_order_action": "BUY",
                        "order_type": "MKT",
                        "total_quantity": 1.0,
                        "what_if": True,
                        "transmit": False,
                    },
                },
                "source_preview_item": {"source_score_id": 11, "source_outcome_id": 22},
            }
        ],
    }
    risk_state = {
        "paper_portfolio_risk_state_version": "paper-portfolio-risk-state-v1",
        "risk_run_id": "risk_checkpoint_test",
        "risk_state": "within_limits",
        "new_entries_allowed": True,
        "breach_count": 0,
        "warning_count": 0,
        "recommended_action": "allow_new_entries",
    }
    fill_marks = {"marks": [{"symbol": "MSFT", "mark_price": 200.0, "mark_source": "fill_mark"}]}
    loop_marks = [{"marks": [{"symbol": "MSFT", "mark_price": 201.0, "mark_source": "refresh_mark"}]}]

    first = build_paper_cycle_scheduler_report(
        auto_gate,
        risk_state,
        fill_marks,
        loop_marks,
        max_runs=1,
        cycles_per_run=1,
        dedupe_across_cycles=True,
        carry_dedupe_state_across_runs=True,
        scheduler_run_id="checkpoint_first",
        max_gross_exposure=1000.0,
        max_symbol_exposure=1000.0,
        max_short_exposure=1000.0,
        max_unrealized_loss=1000.0,
        max_total_loss=1000.0,
    )
    checkpoint = build_paper_cycle_scheduler_checkpoint(first)
    resume_state = extract_paper_cycle_scheduler_resume_state(checkpoint)
    resumed = build_paper_cycle_scheduler_report(
        auto_gate,
        resume_state["next_portfolio_risk_state"],
        fill_marks,
        loop_marks,
        existing_ledger_payload=resume_state["existing_ledger"],
        existing_router_payload=resume_state["existing_router"],
        max_runs=1,
        cycles_per_run=1,
        dedupe_across_cycles=True,
        carry_dedupe_state_across_runs=True,
        scheduler_run_id="checkpoint_second",
        resume_prior_runs_completed=resume_state["runs_completed_total"],
        max_gross_exposure=1000.0,
        max_symbol_exposure=1000.0,
        max_short_exposure=1000.0,
        max_unrealized_loss=1000.0,
        max_total_loss=1000.0,
    )

    assert checkpoint["paper_cycle_scheduler_checkpoint_version"] == "paper-cycle-scheduler-checkpoint-v1"
    assert first["final_scheduler_summary"]["total_new_paper_fills"] == 1
    assert checkpoint["runs_completed_total"] == 1
    assert resume_state["existing_ledger"]["intent_count"] == 1
    assert resumed["resume_prior_runs_completed"] == 1
    assert resumed["runs_completed_total"] == 2
    assert resumed["tick_summaries"][0]["scheduler_tick_index"] == 2
    assert resumed["final_scheduler_summary"]["total_new_paper_fills"] == 0
    assert resumed["final_scheduler_summary"]["final_cumulative_paper_fills"] == 1
    assert resumed["safety"]["ibkr_order_submitted"] is False


def test_paper_cycle_watch_tick_report_marks_execute_and_skip_paths() -> None:
    input_snapshot = {
        "input_snapshot_version": "paper-watch-input-snapshot-v1",
        "signature": "abc123",
        "files": [
            {
                "role": "auto_gate_json",
                "path": "auto.json",
                "exists": True,
                "size": 10,
                "modified_utc": "2026-06-30T00:00:00+00:00",
                "digest": "deadbeef",
            }
        ],
    }
    scheduler_result = {
        "scheduler_run_id": "watch_scheduler",
        "runs_completed": 1,
        "runs_completed_total": 4,
        "resume_state": {"next_scheduler_tick_index": 5},
        "final_scheduler_summary": {
            "total_new_paper_fills": 0,
            "final_cumulative_paper_fills": 3,
            "final_risk_state": "within_limits",
            "final_gate_action": "allow_new_entries",
        },
    }
    checkpoint = {
        "checkpoint_run_id": "watch_checkpoint",
        "source_scheduler_run_id": "watch_scheduler",
        "runs_completed_total": 4,
        "next_scheduler_tick_index": 5,
        "final_scheduler_summary": {
            "final_risk_state": "within_limits",
            "final_cumulative_paper_fills": 3,
        },
    }

    executed = build_paper_cycle_watch_tick_report(
        watch_run_id="watch_execute",
        input_snapshot=input_snapshot,
        action="executed_scheduler_tick",
        scheduler_result=scheduler_result,
        checkpoint=checkpoint,
        evidence_refresh_watchdog={"final_status": "healthy_no_refresh_needed", "final_freshness_status": "green", "final_stale_source_keys": [], "final_lagging_source_keys": []},
        evidence_preflight_gate={"gate_status": "watch", "allowed": True, "action": "allow_with_evidence_haircut", "entry_size_multiplier": 0.5},
    )
    skipped = build_paper_cycle_watch_tick_report(
        watch_run_id="watch_skip",
        input_snapshot=input_snapshot,
        action="skipped_unchanged_inputs",
        checkpoint=checkpoint,
        skipped_reason="unchanged",
    )
    report = paper_cycle_watch_tick_markdown(executed)

    assert executed["paper_cycle_watch_tick_version"] == "paper-cycle-watch-tick-v1"
    assert executed["action"] == "executed_scheduler_tick"
    assert executed["next_scheduler_tick_index"] == 5
    assert executed["final_scheduler_summary"]["final_cumulative_paper_fills"] == 3
    assert executed["evidence_refresh_watchdog"]["final_status"] == "healthy_no_refresh_needed"
    assert executed["evidence_preflight_gate"]["action"] == "allow_with_evidence_haircut"
    assert skipped["action"] == "skipped_unchanged_inputs"
    assert skipped["skipped_reason"] == "unchanged"
    assert skipped["checkpoint_summary"]["next_scheduler_tick_index"] == 5
    assert skipped["final_scheduler_summary"]["total_new_paper_fills"] == 0
    assert skipped["final_scheduler_summary"]["final_cumulative_paper_fills"] == 3
    assert executed["safety"]["ibkr_order_submitted"] is False
    assert "Paper Cycle Watch Tick" in report
    assert "Evidence refresh watchdog" in report
    assert "Evidence preflight" in report


def test_paper_cycle_watch_runner_summarizes_finite_tick_results() -> None:
    tick_results = [
        {
            "watch_run_id": "runner_tick_01",
            "action": "executed_scheduler_tick",
            "runs_completed": 1,
            "runs_completed_total": 1,
            "next_scheduler_tick_index": 2,
            "final_scheduler_summary": {
                "total_new_paper_fills": 2,
                "final_cumulative_paper_fills": 2,
                "final_risk_state": "within_limits",
                "final_gate_action": "allow_new_entries",
                "final_total_pnl": 1.25,
            },
        },
        {
            "watch_run_id": "runner_tick_02",
            "action": "skipped_unchanged_inputs",
            "runs_completed": 0,
            "runs_completed_total": 1,
            "next_scheduler_tick_index": 2,
            "final_scheduler_summary": {
                "total_new_paper_fills": 0,
                "final_cumulative_paper_fills": 2,
                "final_risk_state": "within_limits",
                "final_gate_action": "allow_new_entries",
                "final_total_pnl": 1.25,
            },
        },
    ]

    result = build_paper_cycle_watch_runner_report(
        runner_run_id="runner_test",
        tick_results=tick_results,
        max_ticks_requested=2,
        profile_json="profile.json",
    )
    report = paper_cycle_watch_runner_markdown(result)

    assert result["paper_cycle_watch_runner_version"] == "paper-cycle-watch-runner-v1"
    assert result["ticks_completed"] == 2
    assert result["executed_tick_count"] == 1
    assert result["skipped_tick_count"] == 1
    assert result["final_runner_summary"]["total_new_paper_fills"] == 2
    assert result["final_runner_summary"]["final_cumulative_paper_fills"] == 2
    assert result["safety"]["ibkr_order_submitted"] is False
    assert "Paper Cycle Watch Runner" in report


def test_paper_session_gate_classifies_regular_and_blocks_closed() -> None:
    profile = {
        "session_gate": {
            "enabled": True,
            "timezone": "America/New_York",
            "allowed_sessions": ["regular"],
            "weekdays": [0, 1, 2, 3, 4],
            "pre_start": "04:00",
            "regular_start": "09:30",
            "regular_end": "16:00",
            "after_end": "20:00",
        }
    }

    regular = _evaluate_paper_session_gate(profile, now_value="2026-06-30T10:00:00-04:00")
    closed = _evaluate_paper_session_gate(profile, now_value="2026-06-30T20:30:00-04:00")

    assert regular["enabled"] is True
    assert regular["session"] == "regular"
    assert regular["allowed"] is True
    assert closed["session"] == "closed"
    assert closed["allowed"] is False


def test_paper_session_gate_calendar_blocks_holiday_and_early_close_after_regular() -> None:
    profile = {
        "session_gate": {
            "enabled": True,
            "timezone": "America/New_York",
            "allowed_sessions": ["regular"],
            "calendar": {
                "calendar_version": "test-market-calendar-v1",
                "market": "US_EQUITIES",
                "regular_hours": {
                    "pre_start": "04:00",
                    "regular_start": "09:30",
                    "regular_end": "16:00",
                    "after_end": "20:00",
                },
                "holidays": [
                    {"date": "2026-07-03", "name": "Independence Day observed"},
                ],
                "early_closes": [
                    {
                        "date": "2026-11-27",
                        "name": "Day after Thanksgiving",
                        "regular_end": "13:00",
                        "after_end": "17:00",
                    }
                ],
            },
        }
    }

    holiday = _evaluate_paper_session_gate(profile, now_value="2026-07-03T10:00:00-04:00")
    before_early_close = _evaluate_paper_session_gate(profile, now_value="2026-11-27T12:30:00-05:00")
    after_early_close = _evaluate_paper_session_gate(profile, now_value="2026-11-27T14:00:00-05:00")

    assert holiday["session"] == "closed"
    assert holiday["allowed"] is False
    assert holiday["calendar"]["holiday_name"] == "Independence Day observed"
    assert before_early_close["session"] == "regular"
    assert before_early_close["allowed"] is True
    assert before_early_close["calendar"]["early_close"] is True
    assert before_early_close["schedule"]["regular_end"] == "13:00"
    assert after_early_close["session"] == "after_hours"
    assert after_early_close["allowed"] is False


def test_paper_session_gate_loads_calendar_json_relative_to_profile_base(tmp_path: Path) -> None:
    calendar_path = tmp_path / "paper_market_calendar_us_v1.json"
    calendar_path.write_text(
        json.dumps(
            {
                "calendar_version": "test-market-calendar-file-v1",
                "market": "US_EQUITIES",
                "timezone": "America/New_York",
                "regular_hours": {
                    "pre_start": "04:00",
                    "regular_start": "09:30",
                    "regular_end": "16:00",
                    "after_end": "20:00",
                },
                "early_closes": [
                    {
                        "date": "2026-12-24",
                        "name": "Christmas Eve",
                        "regular_end": "13:00",
                        "after_end": "17:00",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    profile = {
        "session_gate": {
            "enabled": True,
            "calendar_json": "paper_market_calendar_us_v1.json",
            "allowed_sessions": ["regular"],
        }
    }

    before_early_close = _evaluate_paper_session_gate(
        profile,
        now_value="2026-12-24T12:45:00-05:00",
        profile_base=tmp_path,
    )
    after_early_close = _evaluate_paper_session_gate(
        profile,
        now_value="2026-12-24T13:15:00-05:00",
        profile_base=tmp_path,
    )

    assert before_early_close["session"] == "regular"
    assert before_early_close["allowed"] is True
    assert before_early_close["calendar"]["calendar_version"] == "test-market-calendar-file-v1"
    assert before_early_close["calendar"]["calendar_json"] == str(calendar_path.resolve())
    assert after_early_close["session"] == "after_hours"
    assert after_early_close["allowed"] is False


def test_paper_data_quality_preflight_passes_fresh_live_complete_marks() -> None:
    snapshot = _mark_snapshot()

    result = build_paper_data_quality_preflight_report(
        fill_marks_payload=snapshot,
        loop_mark_payloads=[snapshot],
        now="2026-06-30T00:05:00+00:00",
        max_snapshot_age_seconds=600,
        max_tick_age_seconds=600,
        allow_delayed=False,
        require_all_marked=True,
        min_marked_ratio=1.0,
    )
    report = paper_data_quality_preflight_markdown(result)

    assert result["paper_data_quality_preflight_version"] == "paper-data-quality-preflight-v1"
    assert result["gate_status"] == "pass"
    assert result["allowed"] is True
    assert result["summary"]["missing_mark_count"] == 0
    assert result["summary"]["delayed_count"] == 0
    assert "Paper Data Quality Preflight" in report


def test_paper_data_quality_preflight_blocks_delayed_missing_and_stale_marks() -> None:
    delayed = _mark_snapshot(delayed=True)
    missing = _mark_snapshot(symbol="TSLA", marked=False)
    stale = _mark_snapshot(
        symbol="NVDA",
        generated_at="2026-06-29T00:00:00+00:00",
        mark_timestamp="2026-06-29T00:00:30+00:00",
    )

    result = build_paper_data_quality_preflight_report(
        fill_marks_payload=delayed,
        loop_mark_payloads=[missing, stale],
        now="2026-06-30T00:05:00+00:00",
        max_snapshot_age_seconds=600,
        max_tick_age_seconds=600,
        allow_delayed=False,
        require_all_marked=True,
        min_marked_ratio=1.0,
    )

    assert result["gate_status"] == "block"
    assert result["allowed"] is False
    assert "fill_marks:delayed_market_data_not_allowed" in result["block_reasons"]
    assert "loop_marks_1:missing_marks" in result["block_reasons"]
    assert "loop_marks_2:stale_snapshot" in result["block_reasons"]


def test_paper_cycle_watch_runner_counts_session_gate_skips() -> None:
    tick_results = [
        {
            "watch_run_id": "session_tick_01",
            "action": "skipped_session_gate",
            "runs_completed": 0,
            "runs_completed_total": None,
            "next_scheduler_tick_index": None,
            "session_gate": {"session": "closed", "allowed": False},
            "final_scheduler_summary": {"total_new_paper_fills": 0},
        }
    ]

    result = build_paper_cycle_watch_runner_report(
        runner_run_id="session_runner_test",
        tick_results=tick_results,
        max_ticks_requested=1,
        profile_json="session_profile.json",
    )

    assert result["session_skipped_tick_count"] == 1
    assert result["skipped_tick_count"] == 1
    assert result["executed_tick_count"] == 0
    assert result["final_runner_summary"]["total_new_paper_fills"] == 0


def test_paper_cycle_watch_runner_counts_preflight_gate_skips() -> None:
    tick_results = [
        {
            "watch_run_id": "preflight_tick_01",
            "action": "skipped_preflight_gate",
            "runs_completed": 0,
            "runs_completed_total": None,
            "next_scheduler_tick_index": None,
            "session_gate": {"session": "regular", "allowed": True},
            "preflight_gate": {"gate_status": "block", "allowed": False},
            "final_scheduler_summary": {"total_new_paper_fills": 0},
        }
    ]

    result = build_paper_cycle_watch_runner_report(
        runner_run_id="preflight_runner_test",
        tick_results=tick_results,
        max_ticks_requested=1,
        profile_json="preflight_profile.json",
    )
    report = paper_cycle_watch_runner_markdown(result)

    assert result["preflight_skipped_tick_count"] == 1
    assert result["skipped_tick_count"] == 1
    assert result["executed_tick_count"] == 0
    assert "Preflight-gated skipped ticks" in report


def test_paper_cycle_watch_runner_counts_mark_refresh_gate_skips() -> None:
    tick_results = [
        {
            "watch_run_id": "refresh_tick_01",
            "action": "skipped_mark_refresh_gate",
            "runs_completed": 0,
            "runs_completed_total": None,
            "next_scheduler_tick_index": None,
            "session_gate": {"session": "regular", "allowed": True},
            "mark_refresh": {"status": "block", "allowed": False, "mode": "ibkr"},
            "final_scheduler_summary": {"total_new_paper_fills": 0},
        }
    ]

    result = build_paper_cycle_watch_runner_report(
        runner_run_id="refresh_runner_test",
        tick_results=tick_results,
        max_ticks_requested=1,
        profile_json="refresh_profile.json",
    )
    report = paper_cycle_watch_runner_markdown(result)

    assert result["mark_refresh_skipped_tick_count"] == 1
    assert result["skipped_tick_count"] == 1
    assert result["executed_tick_count"] == 0
    assert result["tick_summaries"][0]["mark_refresh_status"] == "block"
    assert "Mark-refresh skipped ticks" in report


def test_mark_refresh_partial_fallback_can_recover_missing_symbol() -> None:
    current = {
        "stock_mark_snapshot_version": "ibkr-stock-mark-snapshot-v1",
        "generated_at": "2026-06-30T04:59:00+00:00",
        "symbols_requested": ["AAPL", "MSFT"],
        "marks": [
            {
                "symbol": "AAPL",
                "market_data_type": 1,
                "market_data_type_label": "live",
                "delayed": False,
                "mark_price": 200.0,
                "mark_status": "marked",
                "mark_timestamp": "2026-06-30T04:59:00+00:00",
            },
            {
                "symbol": "MSFT",
                "market_data_type": 1,
                "market_data_type_label": "live",
                "delayed": False,
                "mark_price": None,
                "mark_status": "missing_mark",
                "mark_timestamp": "2026-06-30T04:59:00+00:00",
            },
        ],
    }
    fallback = _mark_snapshot(
        generated_at="2026-06-30T04:50:00+00:00",
        symbol="MSFT",
        mark_timestamp="2026-06-30T04:50:30+00:00",
    )

    merged, applied = _mark_refresh_apply_partial_fallback(current, fallback, enabled=True)
    policy = _evaluate_mark_refresh_policy(
        merged,
        refresh_profile={
            "fallback": {
                "enabled": True,
                "max_source_age_seconds": 3600,
                "min_marked_ratio": 1.0,
                "max_missing_symbols": 0,
                "allow_delayed": False,
            }
        },
        fallback_payload=fallback,
        fallback_applied_symbols=applied,
        fallback_reason="partial_refresh_fallback",
        now=datetime.fromisoformat("2026-06-30T05:00:00+00:00"),
        required=True,
    )

    assert applied == ["MSFT"]
    assert policy["allowed"] is True
    assert policy["status"] == "warn"
    assert policy["summary"]["missing_mark_count"] == 0
    assert policy["fallback"]["source_allowed"] is True


def test_mark_refresh_partial_fallback_blocks_when_source_is_stale() -> None:
    current = {
        "stock_mark_snapshot_version": "ibkr-stock-mark-snapshot-v1",
        "generated_at": "2026-06-30T04:59:00+00:00",
        "symbols_requested": ["MSFT"],
        "marks": [
            {
                "symbol": "MSFT",
                "market_data_type": 1,
                "market_data_type_label": "live",
                "delayed": False,
                "mark_price": None,
                "mark_status": "missing_mark",
                "mark_timestamp": "2026-06-30T04:59:00+00:00",
            }
        ],
    }
    fallback = _mark_snapshot(
        generated_at="2026-06-30T01:00:00+00:00",
        symbol="MSFT",
        mark_timestamp="2026-06-30T01:00:30+00:00",
    )

    merged, applied = _mark_refresh_apply_partial_fallback(current, fallback, enabled=True)
    policy = _evaluate_mark_refresh_policy(
        merged,
        refresh_profile={
            "fallback": {
                "enabled": True,
                "max_source_age_seconds": 300,
                "min_marked_ratio": 1.0,
                "max_missing_symbols": 0,
                "allow_delayed": False,
            }
        },
        fallback_payload=fallback,
        fallback_applied_symbols=applied,
        fallback_reason="partial_refresh_fallback",
        now=datetime.fromisoformat("2026-06-30T05:00:00+00:00"),
        required=True,
    )

    assert applied == ["MSFT"]
    assert policy["allowed"] is False
    assert policy["status"] == "block"
    assert "fallback_source_not_allowed" in policy["block_reasons"]
    assert policy["fallback"]["source_block_reasons"] == ["fallback_source_stale"]


def test_mark_refresh_quality_warn_overlays_portfolio_risk_monitor() -> None:
    mark_refresh = {
        "status": "warn",
        "allowed": True,
        "policy_evaluation": {"status": "warn"},
    }
    profile = {
        "mark_refresh": {
            "quality_actions": {
                "enabled": True,
                "actions": {
                    "warn": {
                        "action": "size_reduce",
                        "portfolio_risk_state_override": "monitor",
                        "monitor_size_multiplier": 0.25,
                    }
                },
            }
        }
    }

    action = _mark_refresh_quality_execution_action(mark_refresh, profile, base_monitor_size_multiplier=0.5)
    adjusted = _apply_mark_refresh_quality_action_to_portfolio_risk(
        {
            "risk_state": "within_limits",
            "new_entries_allowed": True,
            "warning_count": 0,
            "risk_run_id": "risk_01",
        },
        action,
    )

    assert action["action"] == "size_reduce"
    assert action["applied"] is True
    assert action["effective_monitor_size_multiplier"] == 0.25
    assert adjusted["risk_state"] == "monitor"
    assert adjusted["new_entries_allowed"] is True
    assert adjusted["warning_count"] == 1
    assert adjusted["mark_refresh_quality_overlay_applied"] is True
    assert adjusted["mark_refresh_quality_overlay"]["action"] == "size_reduce"


def test_mark_refresh_quality_pass_does_not_overlay_portfolio_risk() -> None:
    mark_refresh = {
        "status": "pass",
        "allowed": True,
        "policy_evaluation": {"status": "pass"},
    }
    profile = {"mark_refresh": {"quality_actions": {"enabled": True}}}

    action = _mark_refresh_quality_execution_action(mark_refresh, profile, base_monitor_size_multiplier=0.5)
    original = {"risk_state": "within_limits", "new_entries_allowed": True, "warning_count": 0}
    adjusted = _apply_mark_refresh_quality_action_to_portfolio_risk(original, action)

    assert action["action"] == "allow"
    assert action["applied"] is False
    assert adjusted == original


def test_paper_cycle_watch_runner_summarizes_mark_refresh_quality() -> None:
    tick_results = [
        {
            "watch_run_id": "quality_tick_01",
            "action": "executed_scheduler_tick",
            "runs_completed": 1,
            "runs_completed_total": 1,
            "next_scheduler_tick_index": 2,
            "session_gate": {"session": "regular", "allowed": True},
            "mark_refresh": {
                "status": "pass",
                "allowed": True,
                "policy_evaluation": {
                    "status": "pass",
                    "allowed": True,
                    "summary": {"missing_mark_count": 0, "delayed_count": 0},
                    "fallback": {"applied_symbols": []},
                },
                "execution_quality_action": {"action": "allow", "applied": False, "effective_monitor_size_multiplier": 0.5},
            },
            "preflight_gate": {"gate_status": "pass", "allowed": True},
            "final_scheduler_summary": {"total_new_paper_fills": 10, "final_cumulative_paper_fills": 10},
        },
        {
            "watch_run_id": "quality_tick_02",
            "action": "executed_scheduler_tick",
            "runs_completed": 1,
            "runs_completed_total": 2,
            "next_scheduler_tick_index": 3,
            "session_gate": {"session": "regular", "allowed": True},
            "mark_refresh": {
                "status": "warn",
                "allowed": True,
                "policy_evaluation": {
                    "status": "warn",
                    "allowed": True,
                    "summary": {"missing_mark_count": 1, "delayed_count": 2},
                    "fallback": {"applied_symbols": ["MSFT"], "reason": "partial_refresh_fallback"},
                },
                "execution_quality_action": {"action": "size_reduce", "applied": True, "effective_monitor_size_multiplier": 0.25},
            },
            "preflight_gate": {"gate_status": "pass", "allowed": True},
            "final_scheduler_summary": {"total_new_paper_fills": 5, "final_cumulative_paper_fills": 15},
        },
    ]

    result = build_paper_cycle_watch_runner_report(
        runner_run_id="quality_runner_test",
        tick_results=tick_results,
        max_ticks_requested=2,
        profile_json="quality_profile.json",
    )
    report = paper_cycle_watch_runner_markdown(result)
    quality = result["mark_refresh_quality_summary"]

    assert quality["observed_tick_count"] == 2
    assert quality["refresh_status_counts"] == {"pass": 1, "warn": 1}
    assert quality["policy_status_counts"] == {"pass": 1, "warn": 1}
    assert quality["quality_action_counts"] == {"allow": 1, "size_reduce": 1}
    assert quality["quality_action_applied_count"] == 1
    assert quality["delayed_row_count"] == 2
    assert quality["missing_mark_count"] == 1
    assert quality["fallback_tick_count"] == 1
    assert quality["fallback_symbol_counts"] == {"MSFT": 1}
    assert "Mark-refresh quality observed ticks" in report
    assert "Quality action" in report


def test_paper_cycle_watch_daemon_summarizes_runner_batches() -> None:
    batch_results = [
        build_paper_cycle_watch_runner_report(
            runner_run_id="daemon_batch_01",
            tick_results=[
                {
                    "watch_run_id": "daemon_batch_01_tick_01",
                    "action": "skipped_session_gate",
                    "runs_completed": 0,
                    "runs_completed_total": None,
                    "next_scheduler_tick_index": None,
                    "session_gate": {"session": "closed", "allowed": False},
                    "final_scheduler_summary": {"total_new_paper_fills": 0},
                }
            ],
            max_ticks_requested=1,
            profile_json="calendar_profile.json",
        ),
        build_paper_cycle_watch_runner_report(
            runner_run_id="daemon_batch_02",
            tick_results=[
                {
                    "watch_run_id": "daemon_batch_02_tick_01",
                    "action": "executed_scheduler_tick",
                    "runs_completed": 1,
                    "runs_completed_total": 1,
                    "next_scheduler_tick_index": 2,
                    "session_gate": {"session": "regular", "allowed": True},
                    "final_scheduler_summary": {
                        "total_new_paper_fills": 10,
                        "final_cumulative_paper_fills": 10,
                        "final_risk_state": "within_limits",
                        "final_gate_action": "allow_new_entries",
                    },
                }
            ],
            max_ticks_requested=1,
            profile_json="calendar_profile.json",
        ),
        build_paper_cycle_watch_runner_report(
            runner_run_id="daemon_batch_03",
            tick_results=[
                {
                    "watch_run_id": "daemon_batch_03_tick_01",
                    "action": "skipped_session_gate",
                    "runs_completed": 0,
                    "runs_completed_total": 1,
                    "next_scheduler_tick_index": 2,
                    "session_gate": {"session": "after_hours", "allowed": False},
                    "final_scheduler_summary": {
                        "total_new_paper_fills": 0,
                        "final_cumulative_paper_fills": 10,
                        "final_risk_state": "within_limits",
                    },
                }
            ],
            max_ticks_requested=1,
            profile_json="calendar_profile.json",
        ),
    ]

    result = build_paper_cycle_watch_daemon_report(
        daemon_run_id="daemon_test",
        batch_results=batch_results,
        max_batches_requested=3,
        ticks_per_batch=1,
        profile_json="calendar_profile.json",
        daily_log_jsonl="daily.jsonl",
    )
    report = paper_cycle_watch_daemon_markdown(result)

    assert result["paper_cycle_watch_daemon_version"] == "paper-cycle-watch-daemon-v1"
    assert result["batches_completed"] == 3
    assert result["executed_tick_count"] == 1
    assert result["session_skipped_tick_count"] == 2
    assert result["preflight_skipped_tick_count"] == 0
    assert result["mark_refresh_skipped_tick_count"] == 0
    assert result["mark_refresh_quality_summary"]["observed_tick_count"] == 0
    assert result["final_daemon_summary"]["total_new_paper_fills"] == 10
    assert result["final_daemon_summary"]["final_cumulative_paper_fills"] == 10
    assert result["safety"]["ibkr_order_submitted"] is False
    assert "Paper Cycle Watch Daemon" in report
