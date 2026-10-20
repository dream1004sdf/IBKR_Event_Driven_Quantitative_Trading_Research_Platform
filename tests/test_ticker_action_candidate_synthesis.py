from __future__ import annotations

from recorder.ticker_action_candidate_synthesis import build_ticker_action_candidate_synthesis_report


def _ranking_payload() -> dict:
    return {
        "ticker_candidate_ranking_version": "m241_5-ticker-candidate-ranking-v1",
        "generated_at": "2026-07-04T12:00:00+00:00",
        "summary": {"ranked_candidate_count": 4, "top_symbols": ["SPY", "QQQ", "GLD", "NVDA"]},
        "ranking": [
            {
                "rank": 1,
                "symbol": "SPY",
                "total_score": 96.0,
                "confidence": "high",
                "event_count": 3,
                "direct_symbol_hit_count": 1,
                "providers": {"benzinga": 2},
                "recent_events": [{"headline": "SPY rallies as inflation cooling optimism lifts stocks"}],
            },
            {
                "rank": 2,
                "symbol": "QQQ",
                "total_score": 88.0,
                "confidence": "medium",
                "event_count": 2,
                "direct_symbol_hit_count": 0,
                "providers": {"dow_jones": 1},
                "recent_events": [{"headline": "Tariff war worries pressure technology shares and Nasdaq futures"}],
            },
            {
                "rank": 3,
                "symbol": "GLD",
                "total_score": 80.0,
                "confidence": "medium",
                "event_count": 2,
                "direct_symbol_hit_count": 0,
                "providers": {"benzinga": 1},
                "recent_events": [{"headline": "Missile attack risk pushes investors toward havens"}],
            },
            {
                "rank": 4,
                "symbol": "NVDA",
                "total_score": 76.0,
                "confidence": "medium",
                "event_count": 2,
                "direct_symbol_hit_count": 1,
                "providers": {"benzinga": 1},
                "recent_events": [{"headline": "Nvidia raises outlook as AI demand stays strong"}],
            },
        ],
    }


def test_m241_6_synthesizes_ranked_tickers_into_strategy_review_candidates() -> None:
    report = build_ticker_action_candidate_synthesis_report(
        ticker_candidate_ranking_payload=_ranking_payload(),
        capital_aware_gate_payload={"auto_execution_items": [{"symbol": "SPY"}]},
        capital_state_payload={
            "status": "green",
            "leverage_mode": "cash_only",
            "budgets": {
                "deployable_cash_after_reserve": 500.0,
                "max_single_position_notional": 250.0,
                "options_premium_budget": 175.0,
            },
        },
        max_synthetic_candidates=10,
    )

    symbols = [row["symbol"] for row in report["m241_injection_candidates"]]
    by_symbol = {row["symbol"]: row for row in report["m241_injection_candidates"]}

    assert report["ticker_action_candidate_synthesis_version"].startswith("m241_6")
    assert "SPY" not in symbols
    assert by_symbol["QQQ"]["action"] == "SELL_SHORT"
    assert by_symbol["GLD"]["action"] == "BUY"
    assert by_symbol["NVDA"]["action"] == "BUY"
    assert report["summary"]["injectable_candidate_count"] == 3
    assert report["safety"]["ibkr_order_submitted"] is False
    assert report["safety"]["live_routing_enabled"] is False


def test_m241_6_keeps_neutral_candidates_review_only_by_default() -> None:
    payload = _ranking_payload()
    payload["ranking"] = [
        {
            "rank": 1,
            "symbol": "IWM",
            "total_score": 60.0,
            "confidence": "medium",
            "event_count": 1,
            "providers": {"benzinga": 1},
            "recent_events": [{"headline": "Small caps mixed in quiet trading"}],
        }
    ]
    report = build_ticker_action_candidate_synthesis_report(
        ticker_candidate_ranking_payload=payload,
        capital_state_payload={
            "status": "green",
            "leverage_mode": "cash_only",
            "budgets": {"deployable_cash_after_reserve": 500.0, "options_premium_budget": 175.0},
        },
    )

    assert report["synthetic_candidates"][0]["action"] == "WATCH"
    assert report["synthetic_candidates"][0]["inject_to_m241"] is False
    assert report["summary"]["injectable_candidate_count"] == 0


def test_m241_6_consumes_m241_7_cross_asset_expression_seeds_before_raw_ranking() -> None:
    report = build_ticker_action_candidate_synthesis_report(
        ticker_candidate_ranking_payload=_ranking_payload(),
        cross_asset_expression_ranking_payload={
            "cross_asset_expression_ranking_version": "m241_7-cross-asset-expression-ranking-v1",
            "m241_6_candidate_seeds": [
                {
                    "symbol": "QQQ",
                    "action": "SELL_SHORT",
                    "direction": "bearish",
                    "expression_hint": "long_put",
                    "scenario": "tariff_policy_risk",
                    "cross_asset_rank": 1,
                    "cross_asset_score": 118.0,
                    "requires_option": True,
                    "reasons": ["tariff_policy_risk"],
                    "cautions": [],
                },
                {
                    "symbol": "GLD",
                    "action": "BUY",
                    "direction": "bullish",
                    "expression_hint": "stock_long",
                    "scenario": "broad_market_down",
                    "cross_asset_rank": 2,
                    "cross_asset_score": 72.0,
                    "requires_option": False,
                    "reasons": ["safe_haven_proxy"],
                    "cautions": [],
                },
            ],
        },
        capital_state_payload={
            "status": "green",
            "leverage_mode": "cash_only",
            "budgets": {
                "deployable_cash_after_reserve": 500.0,
                "max_single_position_notional": 250.0,
                "options_premium_budget": 175.0,
            },
        },
        max_synthetic_candidates=3,
    )

    by_symbol = {row["symbol"]: row for row in report["m241_injection_candidates"]}

    assert report["summary"]["used_cross_asset_expression_ranking"] is True
    assert report["summary"]["cross_asset_injected_count"] == 2
    assert report["summary"]["top_cross_asset_expressions"][:2] == ["QQQ:long_put", "GLD:stock_long"]
    assert by_symbol["QQQ"]["candidate_origin"] == "m241_6_cross_asset_expression_seed"
    assert by_symbol["QQQ"]["ticket_type"] == "option_review"
    assert by_symbol["QQQ"]["m241_7_cross_asset_expression"]["scenario"] == "tariff_policy_risk"
    assert by_symbol["GLD"]["ticket_type"] == "stock"
