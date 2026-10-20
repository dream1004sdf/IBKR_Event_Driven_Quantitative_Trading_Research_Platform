from __future__ import annotations

from recorder.live_read_only_capital_validation import (
    build_live_read_only_capital_validation_report,
    live_read_only_capital_validation_markdown,
)


def _capital_state(**budget_overrides) -> dict:
    budgets = {
        "deployable_cash_after_reserve": 500.0,
        "max_new_initial_margin": 1_000.0,
        "core_risk_budget": 100.0,
        "max_single_position_notional": 250.0,
        "options_premium_budget": 50.0,
    }
    budgets.update(budget_overrides)
    return {
        "capital_margin_state_version": "m238-capital-withdrawal-guard-v1",
        "generated_at": "2026-07-03T10:00:00+00:00",
        "status": "green",
        "status_reasons": ["capital_margin_buffer_normal"],
        "leverage_mode": "normal_margin",
        "selected_account": "U1",
        "snapshot_age_seconds": 60,
        "metrics": {"net_liquidation": 10_000.0, "available_funds": 2_000.0},
        "budgets": budgets,
        "withdrawal_guard": {"max_safe_withdrawal": 300.0, "requires_fresh_account_snapshot": False},
        "safety": {"ibkr_order_submitted": False, "transmit_enabled": False},
    }


def _item(symbol: str, *, ticket_type: str, quantity: float, cash: float, margin: float, risk: float, notional: float) -> dict:
    return {
        "symbol": symbol,
        "ticket_type": ticket_type,
        "action": "BUY",
        "quantity": quantity,
        "auto_execution_status": "auto_execution_ready",
        "capital_gate_status": "capital_ready",
        "capital_requirements": {
            "cash_required": cash,
            "initial_margin_required": margin,
            "risk_required": risk,
            "stock_notional_required": notional if ticket_type == "stock" else 0.0,
            "option_premium_required": max(cash, margin, risk) if "option" in ticket_type else 0.0,
        },
    }


def test_m243_reports_max_feasible_quantity_from_live_account_budgets() -> None:
    report = build_live_read_only_capital_validation_report(
        capital_state_payload=_capital_state(options_premium_budget=200.0),
        capital_aware_gate_payload={
            "capital_aware_action_gate_version": "m239-capital-aware-action-sizing-gate-v1",
            "auto_execution_items": [
                _item("SPY", ticket_type="stock", quantity=10.0, cash=1_000.0, margin=0.0, risk=100.0, notional=1_000.0),
                _item("QQQ", ticket_type="option_combo_debit_spread", quantity=2.0, cash=200.0, margin=40.0, risk=40.0, notional=200.0),
            ],
        },
        now="2026-07-03T10:05:00+00:00",
    )

    assert report["live_read_only_capital_validation_version"] == "m243-live-read-only-capital-validation-v2-gate-attribution"
    assert report["current_size_feasible_count"] == 1
    assert report["resize_required_count"] == 1
    spy = report["validation_items"][0]
    assert spy["symbol"] == "SPY"
    assert spy["status"] == "live_read_only_resize_required"
    assert spy["max_feasible_quantity"] == 2.5
    assert spy["binding_constraint"] == "single_position_notional_budget"
    qqq = report["validation_items"][1]
    assert qqq["status"] == "live_read_only_current_size_feasible"
    assert qqq["max_feasible_quantity"] == 2
    assert qqq["execution_gate"]["status"] == "not_trade_executable"
    assert qqq["execution_gate"]["blocked_by"]["micro_policy"] is True
    assert "micro_policy_option_ticket_risk_cap_exceeded" in qqq["execution_gate"]["execution_block_reasons"]
    assert spy["execution_gate"]["status"] == "trade_executable_after_resize"
    assert report["safety"]["ibkr_order_submitted"] is False
    assert "M243 Live Read-Only Capital Validation" in live_read_only_capital_validation_markdown(report)


def test_m243_uses_safe_broker_whatif_cache_as_extra_margin_constraint() -> None:
    report = build_live_read_only_capital_validation_report(
        capital_state_payload=_capital_state(
            deployable_cash_after_reserve=10_000.0,
            max_new_initial_margin=100.0,
            core_risk_budget=10_000.0,
            max_single_position_notional=10_000.0,
        ),
        capital_aware_gate_payload={
            "auto_execution_items": [
                _item("SPY", ticket_type="stock", quantity=10.0, cash=100.0, margin=0.0, risk=10.0, notional=1_000.0),
            ]
        },
        broker_whatif_payload={
            "whatif_preview_version": "ibkr-order-whatif-preview-v2",
            "response_count": 1,
            "responses": [
                {
                    "symbol": "SPY",
                    "order": {"source_total_quantity": 10.0, "total_quantity": 10.0},
                    "order_state": {"status": "PreSubmitted", "initMarginChange": "200.0", "maintMarginChange": "150.0"},
                }
            ],
            "safety": {"what_if_only": True, "live_order_transmitted": False, "ibkr_order_submitted": False},
        },
    )

    item = report["validation_items"][0]
    assert item["status"] == "live_read_only_resize_required"
    assert item["binding_constraint"] == "broker_whatif_margin_budget"
    assert item["max_feasible_quantity"] == 5
    assert item["broker_whatif_available"] is True


def test_m243_ignores_unsafe_broker_whatif_cache_and_stays_closed() -> None:
    report = build_live_read_only_capital_validation_report(
        capital_state_payload=_capital_state(),
        capital_aware_gate_payload={
            "auto_execution_items": [
                _item("SPY", ticket_type="stock", quantity=1.0, cash=10.0, margin=0.0, risk=1.0, notional=10.0),
            ]
        },
        broker_whatif_payload={
            "response_count": 1,
            "responses": [{"symbol": "SPY", "order_state": {"initMarginChange": "9999.0"}}],
            "safety": {"what_if_only": False, "live_order_transmitted": True, "ibkr_order_submitted": False},
        },
    )

    item = report["validation_items"][0]
    assert item["status"] == "live_read_only_current_size_feasible"
    assert item["broker_whatif_available"] is False
    assert "broker_whatif_not_marked_what_if_only" in item["reasons"]
    assert "broker_whatif_live_order_transmitted_true" in item["reasons"]
    assert item["execution_gate"]["status"] == "not_trade_executable"
    assert item["execution_gate"]["blocked_by"]["broker"] is True
    assert report["safety"]["whatif_request_submitted_by_m243"] is False


def test_m243_blocks_short_stock_when_live_account_is_cash_only() -> None:
    capital_state = _capital_state()
    capital_state["leverage_mode"] = "cash_only"
    report = build_live_read_only_capital_validation_report(
        capital_state_payload=capital_state,
        capital_aware_gate_payload={
            "auto_execution_items": [
                {
                    **_item("ORCL", ticket_type="stock", quantity=0.02, cash=2.0, margin=0.0, risk=0.2, notional=2.0),
                    "action": "SELL_SHORT",
                },
            ]
        },
    )

    item = report["validation_items"][0]
    assert report["status"] == "yellow"
    assert item["status"] == "live_read_only_blocked"
    assert item["max_feasible_quantity"] > 0
    assert "cash_only_blocks_short_stock" in item["reasons"]
    assert item["execution_gate"]["status"] == "not_trade_executable"
    assert item["execution_gate"]["blocked_by"]["cash_only"] is True


def test_m245_separates_capital_feasible_from_trade_executable_when_source_market_gate_blocks() -> None:
    report = build_live_read_only_capital_validation_report(
        capital_state_payload=_capital_state(),
        capital_aware_gate_payload={
            "capital_aware_action_gate_version": "m239-capital-aware-action-sizing-gate-v1",
            "auto_execution_items": [
                {
                    **_item("NFLX", ticket_type="stock", quantity=1.0, cash=100.0, margin=0.0, risk=2.0, notional=100.0),
                    "auto_execution_status": "auto_execution_blocked",
                    "capital_gate_status": "capital_blocked_source_gate",
                    "capital_block_reasons": [
                        "source_auto_gate_not_ready",
                        "market_data_defensive_mode_blocks_intent_role",
                    ],
                    "source_gate_snapshot": {
                        "source_auto_execution_status": "auto_execution_blocked",
                        "source_block_reasons": ["market_data_defensive_mode_blocks_ordinary_alpha_entries"],
                        "source_market_data_action": "block_new_entries",
                        "source_market_data_defensive_mode": "risk_reduction_only",
                    },
                }
            ],
        },
    )

    item = report["validation_items"][0]
    assert report["trade_executable_split_version"] == "m245-capital-feasible-vs-trade-executable-v2-gate-attribution"
    assert report["current_size_feasible_count"] == 1
    assert report["trade_executable_current_size_count"] == 0
    assert report["not_trade_executable_count"] == 1
    assert report["blocked_by_gate_counts"]["source"] == 1
    assert report["blocked_by_gate_counts"]["market"] == 1
    assert report["blocked_by_gate_counts"]["capital_action"] == 1
    assert "capital_capacity" not in report["blocked_by_gate_counts"]
    assert item["current_size_feasible"] is True
    assert item["execution_gate"]["trade_executable"] is False
    assert item["execution_gate"]["blocked_by"]["capital_capacity"] is False
    assert item["execution_gate"]["blocked_by"]["capital_action"] is True
    assert item["execution_gate"]["blocked_by"]["source"] is True
    assert item["execution_gate"]["blocked_by"]["market"] is True
    assert "source_gate_not_ready" in item["execution_gate"]["execution_block_reasons"]
    assert "market_gate_not_ready" in item["execution_gate"]["execution_block_reasons"]


def test_m246_micro_cash_policy_allows_tiny_stock_long_but_blocks_short_and_options() -> None:
    capital_state = _capital_state(
        deployable_cash_after_reserve=100.0,
        max_new_initial_margin=0.0,
        core_risk_budget=20.0,
        max_single_position_notional=50.0,
        options_premium_budget=0.0,
    )
    capital_state["leverage_mode"] = "cash_only"
    capital_state["metrics"] = {"net_liquidation": 1_200.0, "total_cash_value": 150.0, "available_funds": 100.0}

    report = build_live_read_only_capital_validation_report(
        capital_state_payload=capital_state,
        capital_aware_gate_payload={
            "auto_execution_items": [
                _item("SPY", ticket_type="stock", quantity=0.05, cash=5.0, margin=5.0, risk=0.5, notional=5.0),
                {
                    **_item("QQQ", ticket_type="stock", quantity=0.05, cash=0.0, margin=0.0, risk=0.5, notional=5.0),
                    "action": "SELL_SHORT",
                },
                _item("IWM", ticket_type="option", quantity=1.0, cash=10.0, margin=10.0, risk=10.0, notional=10.0),
            ],
        },
    )

    assert report["micro_policy_operating_mode"] == "micro_cash_only"
    assert report["micro_policy_current_ready_count"] == 1
    assert report["micro_policy_blocked_count"] == 2
    assert report["blocked_by_gate_counts"]["micro_policy"] == 2
    stock_long, short_stock, option_row = report["validation_items"]
    assert stock_long["execution_gate"]["status"] == "trade_executable_current_size"
    assert stock_long["execution_gate"]["micro_policy_ready"] is True
    assert stock_long["execution_gate"]["micro_policy_current"]["selected_expression_hint"] == "fractional_stock_long_micro"
    assert short_stock["execution_gate"]["blocked_by"]["micro_policy"] is True
    assert "micro_policy_blocks_short_stock" in short_stock["execution_gate"]["execution_block_reasons"]
    assert option_row["execution_gate"]["blocked_by"]["micro_policy"] is True
    assert "micro_policy_option_budget_below_minimum" in option_row["execution_gate"]["execution_block_reasons"]


def test_m246_micro_cash_policy_requires_three_exit_confirmations_before_cash_only_mode() -> None:
    micro_state = _capital_state(
        deployable_cash_after_reserve=100.0,
        max_new_initial_margin=0.0,
        core_risk_budget=20.0,
        max_single_position_notional=50.0,
        options_premium_budget=0.0,
    )
    micro_state["leverage_mode"] = "cash_only"
    micro_state["metrics"] = {"net_liquidation": 1_200.0, "total_cash_value": 150.0, "available_funds": 100.0}
    first = build_live_read_only_capital_validation_report(
        capital_state_payload=micro_state,
        capital_aware_gate_payload={"auto_execution_items": []},
        now="2026-07-03T10:00:00+00:00",
    )
    assert first["micro_policy_operating_mode"] == "micro_cash_only"
    assert first["micro_policy_state"]["transition"] == "micro_cash_entered"

    recovered = _capital_state(
        deployable_cash_after_reserve=700.0,
        max_new_initial_margin=0.0,
        core_risk_budget=100.0,
        max_single_position_notional=400.0,
        options_premium_budget=160.0,
    )
    recovered["leverage_mode"] = "cash_only"
    recovered["metrics"] = {"net_liquidation": 2_600.0, "total_cash_value": 900.0, "available_funds": 700.0}
    second = build_live_read_only_capital_validation_report(
        capital_state_payload=recovered,
        capital_aware_gate_payload={"auto_execution_items": []},
        previous_micro_policy_state_payload=first["micro_policy_state"],
        now="2026-07-03T10:01:00+00:00",
    )
    assert second["micro_policy_operating_mode"] == "micro_cash_only"
    assert second["micro_policy_state"]["transition"] == "micro_cash_exit_pending"
    assert second["micro_policy_state"]["consecutive_exit_confirmations"] == 1

    third = build_live_read_only_capital_validation_report(
        capital_state_payload=recovered,
        capital_aware_gate_payload={"auto_execution_items": []},
        previous_micro_policy_state_payload=second["micro_policy_state"],
        now="2026-07-03T10:02:00+00:00",
    )
    assert third["micro_policy_operating_mode"] == "micro_cash_only"
    assert third["micro_policy_state"]["consecutive_exit_confirmations"] == 2

    fourth = build_live_read_only_capital_validation_report(
        capital_state_payload=recovered,
        capital_aware_gate_payload={"auto_execution_items": []},
        previous_micro_policy_state_payload=third["micro_policy_state"],
        now="2026-07-03T10:03:00+00:00",
    )
    assert fourth["micro_policy_operating_mode"] == "cash_only"
    assert fourth["micro_policy_state"]["transition"] == "micro_cash_exit_confirmed"
    assert fourth["micro_cash_policy"]["thresholds"]["min_option_budget"] == 100.0
    assert fourth["micro_cash_policy"]["thresholds"]["min_spread_budget"] == 150.0
    assert fourth["micro_cash_policy"]["thresholds"]["min_fractional_stock_cash"] == 10.0
