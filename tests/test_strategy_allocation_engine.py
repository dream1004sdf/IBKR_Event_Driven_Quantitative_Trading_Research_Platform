from __future__ import annotations

from recorder.strategy_allocation_engine import (
    build_strategy_allocation_engine_report,
    strategy_allocation_engine_markdown,
)


def _capital_gate_item(symbol: str, action: str, *, status: str = "capital_ready") -> dict:
    return {
        "symbol": symbol,
        "action": action,
        "ticket_type": "stock",
        "capital_gate_status": status,
        "auto_execution_status": "auto_execution_ready" if status in {"capital_ready", "capital_resized_ready"} else "auto_execution_blocked",
        "quantity": 1,
        "source_preview_item": {
            "symbol": symbol,
            "ticket_type": "stock",
            "ticket_draft": {
                "symbol": symbol,
                "strategy": "stock_long" if action == "BUY" else "stock_short",
                "order_action": action,
                "quantity": 1,
            },
        },
        "capital_requirements": {
            "cash_required": 100.0 if action == "BUY" else 0.0,
            "initial_margin_required": 0.0,
            "risk_required": 2.0,
            "stock_notional_required": 100.0,
            "option_premium_required": 0.0,
        },
        "capital_block_reasons": [] if status == "capital_ready" else ["source_auto_gate_not_ready"],
        "safety": {"ibkr_order_submitted": False, "transmit": False},
    }


def _base_payloads(item: dict, *, options_budget: float = 100.0, risk_action: str = "allow_normal") -> dict:
    return {
        "capital_aware_gate_payload": {
            "capital_aware_action_gate_version": "m239-capital-aware-action-sizing-gate-v1",
            "generated_at": "2026-07-03T10:00:00+00:00",
            "capital_ready_count": 1 if item["capital_gate_status"] == "capital_ready" else 0,
            "capital_blocked_count": 0 if item["capital_gate_status"] == "capital_ready" else 1,
            "auto_execution_items": [item],
            "capital_state": {
                "status": "green",
                "leverage_mode": "cash_only",
                "budgets": {
                    "options_premium_budget": options_budget,
                    "hedge_budget": 50.0,
                    "deployable_cash_after_reserve": 500.0,
                },
                "withdrawal_guard": {"max_safe_withdrawal": 0.0},
            },
        },
        "capital_state_payload": {
            "capital_margin_state_version": "m238-capital-withdrawal-guard-v1",
            "status": "green",
            "leverage_mode": "cash_only",
            "metrics": {"net_liquidation": 10000.0, "available_funds": 8000.0, "total_cash_value": 9000.0},
            "budgets": {
                "options_premium_budget": options_budget,
                "hedge_budget": 50.0,
                "deployable_cash_after_reserve": 500.0,
            },
            "withdrawal_guard": {"max_safe_withdrawal": 0.0},
        },
        "market_data_payload": {
            "status": "green",
            "temperature": {"label": "calm", "score": 0.0, "risk_flags": []},
            "summary": {"status": "green", "alert_count": 0, "delayed_count": 0, "missing_price_count": 0},
            "rows": [],
        },
        "market_data_risk_gate_payload": {
            "gate_status": "allow",
            "action": risk_action,
            "new_entries_allowed": risk_action == "allow_normal",
            "alpha_entries_allowed": risk_action == "allow_normal",
            "hedge_entries_allowed": True,
            "option_strategy_bias": "defined_risk_preferred",
        },
        "options_market_structure_payload": {
            "status": "green",
            "summary": {"gamma_regime_counts": {"negative_gamma_trend_amplification": 1}},
            "symbols": [
                {
                    "symbol": item["symbol"],
                    "confidence": "medium",
                    "gamma_regime": "negative_gamma_trend_amplification",
                    "pin_risk": False,
                    "warnings": [],
                }
            ],
        },
        "options_market_structure_supervisor_payload": {
            "final_status": "green",
            "final_quote_fresh": True,
            "final_quote_usable": True,
            "final_quote_quality_status": "green",
        },
        "live_scorer_learning_payload": {
            "status": "collecting",
            "shadow_only": True,
            "candidate_hit_rate": 0.91,
            "avg_packet_quality_score": 80.0,
        },
        "news_learning_payload": {"status": "ok"},
    }


def test_strategy_allocation_engine_prefers_defined_risk_spread_when_options_are_usable() -> None:
    payloads = _base_payloads(_capital_gate_item("SPY", "BUY"), options_budget=100.0)

    report = build_strategy_allocation_engine_report(**payloads, min_option_budget=25.0, min_spread_budget=50.0)

    row = report["recommendations"][0]
    assert row["recommendation"] == "defined_risk_spread"
    assert row["selected_expression"] == "bull_call_debit_spread"
    assert report["summary"]["paper_trade_ready_count"] == 1
    assert report["safety"]["ibkr_order_submitted"] is False


def test_strategy_allocation_engine_attaches_m241_5_ticker_rank_context() -> None:
    payloads = _base_payloads(_capital_gate_item("NVDA", "BUY"), options_budget=100.0)
    payloads["ticker_candidate_ranking_payload"] = {
        "ticker_candidate_ranking_version": "m241_5-ticker-candidate-ranking-v1",
        "generated_at": "2026-07-04T12:00:00+00:00",
        "summary": {"ranked_candidate_count": 2, "top_symbols": ["NVDA", "QQQ"], "review_queue_count": 0},
        "ranking": [
            {
                "rank": 1,
                "symbol": "NVDA",
                "total_score": 92.5,
                "confidence": "high",
                "ranking_reasons": ["live_market_data", "option_structure_medium"],
                "cautions": [],
            }
        ],
    }

    report = build_strategy_allocation_engine_report(**payloads, min_option_budget=25.0, min_spread_budget=50.0)
    row = report["recommendations"][0]

    assert report["contexts"]["ticker_candidate_ranking"]["ranked_candidate_count"] == 2
    assert row["ticker_candidate_rank"]["available"] is True
    assert row["ticker_candidate_rank"]["rank"] == 1
    assert "m241_5_top_ranked_ticker" in row["reasons"]


def test_strategy_allocation_engine_appends_m241_6_synthetic_candidates() -> None:
    payloads = _base_payloads(_capital_gate_item("SPY", "BUY"), options_budget=100.0)
    payloads["capital_aware_gate_payload"]["auto_execution_items"] = []
    payloads["capital_aware_gate_payload"]["capital_ready_count"] = 0
    payloads["ticker_candidate_ranking_payload"] = {
        "ticker_candidate_ranking_version": "m241_5-ticker-candidate-ranking-v1",
        "generated_at": "2026-07-04T12:00:00+00:00",
        "summary": {"ranked_candidate_count": 1, "top_symbols": ["NVDA"], "review_queue_count": 0},
        "ranking": [
            {
                "rank": 1,
                "symbol": "NVDA",
                "total_score": 92.5,
                "confidence": "high",
                "ranking_reasons": ["live_market_data", "option_structure_medium"],
                "cautions": [],
            }
        ],
    }
    payloads["ticker_action_candidate_synthesis_payload"] = {
        "ticker_action_candidate_synthesis_version": "m241_6-ticker-action-candidate-synthesis-v1",
        "generated_at": "2026-07-04T12:00:01+00:00",
        "summary": {"synthetic_candidate_count": 1, "injectable_candidate_count": 1, "top_injected_symbols": ["NVDA"]},
        "m241_injection_candidates": [
            {
                "symbol": "NVDA",
                "action": "BUY",
                "ticket_type": "stock",
                "quantity": 0.25,
                "candidate_origin": "m241_6_ticker_action_candidate_synthesis",
                "synthetic_candidate": True,
                "inject_to_m241": True,
                "auto_execution_status": "auto_execution_ready",
                "capital_gate_status": "capital_ready",
                "capital_requirements": {
                    "cash_required": 25.0,
                    "initial_margin_required": 0.0,
                    "risk_required": 2.0,
                    "stock_notional_required": 25.0,
                    "option_premium_required": 0.0,
                },
                "capital_block_reasons": [],
                "source_preview_item": {
                    "symbol": "NVDA",
                    "ticket_type": "stock",
                    "ticket_draft": {"symbol": "NVDA", "strategy": "bullish_stock_or_call_review", "order_action": "BUY", "quantity": 0.25},
                },
                "safety": {"ibkr_order_submitted": False, "transmit": False},
            }
        ],
    }

    report = build_strategy_allocation_engine_report(**payloads, min_option_budget=25.0, min_spread_budget=50.0)
    row = report["recommendations"][0]

    assert report["summary"]["candidate_count"] == 1
    assert report["summary"]["m241_6_synthetic_appended_count"] == 1
    assert report["contexts"]["ticker_action_candidate_synthesis"]["injectable_candidate_count"] == 1
    assert row["symbol"] == "NVDA"
    assert row["candidate_source"] == "m241_6_ticker_action_candidate_synthesis"
    assert row["synthetic_candidate"] is True
    assert row["recommendation"] in {"stock", "long_option", "defined_risk_spread"}


def test_strategy_allocation_engine_preserves_m241_7_cross_asset_proxy_expression() -> None:
    payloads = _base_payloads(_capital_gate_item("SPY", "BUY"), options_budget=100.0)
    payloads["capital_aware_gate_payload"]["auto_execution_items"] = []
    payloads["capital_aware_gate_payload"]["capital_ready_count"] = 0
    payloads["ticker_action_candidate_synthesis_payload"] = {
        "ticker_action_candidate_synthesis_version": "m241_6-ticker-action-candidate-synthesis-v1",
        "summary": {
            "synthetic_candidate_count": 1,
            "injectable_candidate_count": 1,
            "cross_asset_seed_count": 1,
            "cross_asset_injected_count": 1,
            "top_cross_asset_expressions": ["GLD:stock_long"],
            "top_injected_symbols": ["GLD"],
        },
        "m241_injection_candidates": [
            {
                "symbol": "GLD",
                "action": "BUY",
                "ticket_type": "stock",
                "quantity": 0.25,
                "candidate_origin": "m241_6_cross_asset_expression_seed",
                "synthetic_candidate": True,
                "inject_to_m241": True,
                "auto_execution_status": "auto_execution_ready",
                "capital_gate_status": "capital_ready",
                "capital_requirements": {
                    "cash_required": 25.0,
                    "initial_margin_required": 0.0,
                    "risk_required": 2.0,
                    "stock_notional_required": 25.0,
                    "option_premium_required": 0.0,
                },
                "capital_block_reasons": [],
                "source_preview_item": {
                    "symbol": "GLD",
                    "ticket_type": "stock",
                    "ticket_draft": {"symbol": "GLD", "strategy": "stock_long", "order_action": "BUY", "quantity": 0.25},
                },
                "m241_7_cross_asset_expression": {
                    "symbol": "GLD",
                    "scenario": "broad_market_down",
                    "expression_hint": "stock_long",
                    "cross_asset_rank": 2,
                    "cross_asset_score": 72.0,
                    "requires_option": False,
                },
            }
        ],
    }

    report = build_strategy_allocation_engine_report(**payloads, min_option_budget=25.0, min_spread_budget=50.0)
    row = report["recommendations"][0]

    assert report["contexts"]["ticker_action_candidate_synthesis"]["cross_asset_injected_count"] == 1
    assert row["symbol"] == "GLD"
    assert row["candidate_source"] == "m241_6_cross_asset_expression_seed"
    assert row["recommendation"] == "stock"
    assert row["selected_expression"] == "stock_long"
    assert row["cross_asset_expression"]["scenario"] == "broad_market_down"
    assert "m241_7_prefers_stock_or_etf_proxy" in row["reasons"]


def test_strategy_allocation_engine_prioritizes_m241_6_before_blocked_source_candidates() -> None:
    payloads = _base_payloads(_capital_gate_item("ORCL", "BUY", status="capital_blocked_source_gate"), options_budget=100.0)
    payloads["ticker_candidate_ranking_payload"] = {
        "ticker_candidate_ranking_version": "m241_5-ticker-candidate-ranking-v1",
        "summary": {"ranked_candidate_count": 1, "top_symbols": ["NVDA"]},
        "ranking": [{"rank": 1, "symbol": "NVDA", "total_score": 92.5, "confidence": "high"}],
    }
    payloads["ticker_action_candidate_synthesis_payload"] = {
        "ticker_action_candidate_synthesis_version": "m241_6-ticker-action-candidate-synthesis-v1",
        "summary": {"synthetic_candidate_count": 1, "injectable_candidate_count": 1, "top_injected_symbols": ["NVDA"]},
        "m241_injection_candidates": [
            {
                "symbol": "NVDA",
                "action": "BUY",
                "ticket_type": "stock",
                "quantity": 0.25,
                "candidate_origin": "m241_6_ticker_action_candidate_synthesis",
                "synthetic_candidate": True,
                "inject_to_m241": True,
                "auto_execution_status": "auto_execution_ready",
                "capital_gate_status": "capital_ready",
                "capital_requirements": {
                    "cash_required": 25.0,
                    "initial_margin_required": 0.0,
                    "risk_required": 2.0,
                    "stock_notional_required": 25.0,
                    "option_premium_required": 0.0,
                },
                "capital_block_reasons": [],
                "source_preview_item": {
                    "symbol": "NVDA",
                    "ticket_type": "stock",
                    "ticket_draft": {"symbol": "NVDA", "strategy": "bullish_stock_or_call_review", "order_action": "BUY", "quantity": 0.25},
                },
            }
        ],
    }

    report = build_strategy_allocation_engine_report(**payloads, min_option_budget=25.0, min_spread_budget=50.0, max_recommendations=1)

    assert report["recommendations"][0]["symbol"] == "NVDA"
    assert report["recommendations"][0]["candidate_source"] == "m241_6_ticker_action_candidate_synthesis"


def test_strategy_allocation_engine_uses_put_spread_for_bearish_cash_only_when_options_are_usable() -> None:
    payloads = _base_payloads(_capital_gate_item("QQQ", "SELL_SHORT"), options_budget=100.0)

    report = build_strategy_allocation_engine_report(**payloads, min_option_budget=25.0, min_spread_budget=50.0)

    row = report["recommendations"][0]
    assert row["recommendation"] == "defined_risk_spread"
    assert row["selected_expression"] == "bear_put_debit_spread"
    assert "cash_only_blocks_short_stock_without_put_expression" not in row["reasons"]


def test_strategy_allocation_engine_blocks_when_source_or_market_gate_blocks_alpha() -> None:
    payloads = _base_payloads(
        _capital_gate_item("SPY", "BUY", status="capital_blocked_source_gate"),
        options_budget=100.0,
        risk_action="block_new_entries",
    )

    report = build_strategy_allocation_engine_report(**payloads, min_option_budget=25.0, min_spread_budget=50.0)
    markdown = strategy_allocation_engine_markdown(report)

    row = report["recommendations"][0]
    assert row["recommendation"] == "block"
    assert "capital_or_source_gate_not_ready" in row["reasons"]
    assert "market_gate_blocks_ordinary_alpha" in row["reasons"]
    assert "Treasury" not in markdown


def test_strategy_allocation_engine_applies_gate_policy_haircut_without_blocking() -> None:
    payloads = _base_payloads(_capital_gate_item("SPY", "BUY"), options_budget=100.0)
    payloads["market_data_payload"]["status"] = "yellow"
    payloads["market_data_payload"]["temperature"] = {"label": "hot", "score": 0.7, "risk_flags": ["market_temperature_elevated"]}
    payloads["market_data_risk_gate_payload"].update(
        {
            "gate_status": "watch",
            "action": "allow_with_market_data_haircut",
            "new_entries_allowed": True,
            "alpha_entries_allowed": True,
            "hedge_entries_allowed": True,
            "entry_size_multiplier": 0.35,
        }
    )

    report = build_strategy_allocation_engine_report(**payloads, min_option_budget=25.0, min_spread_budget=50.0)
    row = report["recommendations"][0]

    assert row["recommendation"] == "defined_risk_spread"
    assert row["recommended_size_multiplier"] == 0.35
    assert "gate_policy_size_haircut_active" in row["reasons"]
    assert report["summary"]["gate_execution_posture"] == "size_reduced"
    assert report["contexts"]["gate_severity_policy"]["new_entries_allowed_now"] is True


def test_m246_strategy_allocation_uses_fractional_stock_when_micro_cash_only_options_unusable() -> None:
    payloads = _base_payloads(_capital_gate_item("SPY", "BUY"), options_budget=0.0)
    payloads["capital_state_payload"]["metrics"] = {"net_liquidation": 1_200.0, "available_funds": 100.0, "total_cash_value": 150.0}
    payloads["capital_state_payload"]["budgets"]["max_single_position_notional"] = 250.0

    report = build_strategy_allocation_engine_report(**payloads, min_option_budget=25.0, min_spread_budget=50.0)

    row = report["recommendations"][0]
    assert report["summary"]["micro_policy_operating_mode"] == "micro_cash_only"
    assert report["summary"]["micro_policy_ready_count"] == 1
    assert row["recommendation"] == "stock"
    assert row["selected_expression"] == "fractional_stock_long_micro"
    assert row["micro_policy"]["status"] == "allowed"


def test_m246_strategy_allocation_blocks_bearish_stock_when_micro_cash_only_has_no_option_budget() -> None:
    payloads = _base_payloads(_capital_gate_item("QQQ", "SELL_SHORT"), options_budget=0.0)
    payloads["capital_state_payload"]["metrics"] = {"net_liquidation": 1_200.0, "available_funds": 100.0, "total_cash_value": 150.0}
    payloads["capital_state_payload"]["budgets"]["max_single_position_notional"] = 250.0

    report = build_strategy_allocation_engine_report(**payloads, min_option_budget=25.0, min_spread_budget=50.0)

    row = report["recommendations"][0]
    assert row["recommendation"] == "block"
    assert row["micro_policy"]["status"] == "blocked"
    assert "micro_policy_blocks_short_stock" in row["reasons"]
    assert report["summary"]["micro_policy_blocked_count"] == 1
