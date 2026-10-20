from __future__ import annotations

from recorder.cross_asset_expression_ranking import build_cross_asset_expression_ranking_report


def test_m241_7_ranks_tariff_news_into_cross_asset_put_and_haven_expressions() -> None:
    report = build_cross_asset_expression_ranking_report(
        ticker_candidate_ranking_payload={
            "ticker_candidate_ranking_version": "m241_5-ticker-candidate-ranking-v1",
            "generated_at": "2026-07-04T12:00:00+00:00",
            "summary": {"ranked_candidate_count": 2, "top_symbols": ["QQQ", "GLD"]},
            "ranking": [
                {
                    "rank": 1,
                    "symbol": "QQQ",
                    "total_score": 94.0,
                    "confidence": "high",
                    "themes": {"tariff_policy": 2, "china_taiwan": 1},
                    "recent_events": [
                        {"headline": "Tariff war worries pressure technology shares and Nasdaq futures"}
                    ],
                },
                {
                    "rank": 2,
                    "symbol": "GLD",
                    "total_score": 75.0,
                    "confidence": "medium",
                    "themes": {"safe_haven": 1},
                    "recent_events": [
                        {"headline": "Investors seek haven assets as policy risk rises"}
                    ],
                },
            ],
        },
        market_data_payload={
            "rows": [
                {"symbol": "QQQ", "status": "green", "market_data_type": "live", "price": 550.0},
                {"symbol": "GLD", "status": "green", "market_data_type": "live", "price": 310.0},
            ]
        },
        options_market_structure_payload={
            "symbols": [
                {"symbol": "QQQ", "confidence": "medium", "gamma_regime": "negative_gamma_trend_amplification"}
            ]
        },
        options_market_structure_supervisor_payload={
            "final_quote_usable": True,
            "final_quote_fresh": True,
        },
        capital_state_payload={
            "status": "green",
            "budgets": {
                "options_premium_budget": 250.0,
                "max_single_position_notional": 300.0,
                "deployable_cash_after_reserve": 500.0,
            },
        },
        now="2026-07-04T12:00:00+00:00",
    )

    top = report["expression_rankings"][0]
    seeds_by_symbol = {row["symbol"]: row for row in report["m241_6_candidate_seeds"]}

    assert report["milestone"] == "M241.7"
    assert "tariff_policy_risk" in report["summary"]["top_scenarios"]
    assert top["symbol"] in {"QQQ", "SPY", "SMH", "SOXX", "IWM"}
    assert seeds_by_symbol["QQQ"]["expression_hint"] == "long_put"
    assert seeds_by_symbol["QQQ"]["action"] == "SELL_SHORT"
    assert report["safety"]["ibkr_order_submitted"] is False
    assert report["safety"]["live_routing_enabled"] is False


def test_m241_7_keeps_watch_only_volatility_products_review_safe() -> None:
    report = build_cross_asset_expression_ranking_report(
        ticker_candidate_ranking_payload={
            "ranking": [
                {
                    "rank": 1,
                    "symbol": "SPY",
                    "total_score": 90.0,
                    "confidence": "high",
                    "recent_events": [{"headline": "War risk and sanctions drive broad market risk-off"}],
                }
            ]
        },
        capital_state_payload={"status": "green", "budgets": {"options_premium_budget": 250.0}},
        min_expression_score=20.0,
        now="2026-07-04T12:00:00+00:00",
    )

    by_symbol = {row["symbol"]: row for row in report["expression_rankings"]}

    assert by_symbol["VIX"]["action"] == "WATCH"
    assert "watch_only_not_injected_by_default" in by_symbol["VIX"]["cautions"]
    assert report["policy"]["does_not_create_m242_order_intents"] is True
