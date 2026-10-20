from __future__ import annotations

import sqlite3
from pathlib import Path

from recorder.cross_asset_expression_outcomes import build_cross_asset_expression_outcomes_report
from recorder.cross_asset_expression_ranking import build_cross_asset_expression_ranking_report


def _price_tables(path: Path) -> None:
    con = sqlite3.connect(path)
    con.execute(
        """
        CREATE TABLE market_baselines (
            id INTEGER PRIMARY KEY,
            news_id INTEGER,
            symbol TEXT,
            captured_at TEXT,
            selected_price REAL,
            selected_price_source TEXT
        )
        """
    )
    con.execute(
        """
        CREATE TABLE market_reactions (
            id INTEGER PRIMARY KEY,
            symbol TEXT,
            captured_at TEXT,
            reaction_price REAL,
            price_source TEXT
        )
        """
    )
    con.execute(
        "INSERT INTO market_baselines(news_id, symbol, captured_at, selected_price, selected_price_source) VALUES (1, 'SPY', '2026-07-04T12:00:00+00:00', 100.0, 'fixture')"
    )
    con.execute(
        "INSERT INTO market_reactions(symbol, captured_at, reaction_price, price_source) VALUES ('SPY', '2026-07-04T12:05:00+00:00', 99.0, 'fixture')"
    )
    con.commit()
    con.close()


def test_m241_8_records_and_evaluates_cross_asset_expression_outcome(tmp_path: Path) -> None:
    db_path = tmp_path / "market_events.db"
    _price_tables(db_path)
    ranking = {
        "cross_asset_expression_ranking_version": "m241_7-cross-asset-expression-ranking-v1",
        "generated_at": "2026-07-04T12:00:00+00:00",
        "summary": {"top_expressions": ["SPY:long_put"]},
        "expression_rankings": [
            {
                "rank": 1,
                "scenario": "broad_market_down",
                "scenario_strength": 50.0,
                "symbol": "SPY",
                "expression": "long_put",
                "action": "SELL_SHORT",
                "direction": "bearish",
                "expression_score": 120.0,
                "market_context": {"price": 100.0},
            }
        ],
    }

    report = build_cross_asset_expression_outcomes_report(
        database_path=db_path,
        cross_asset_expression_ranking_payload=ranking,
        horizons_minutes="5",
        min_evaluated_samples=1,
        now="2026-07-04T12:10:00+00:00",
    )

    assert report["milestone"] == "M241.8"
    assert report["summary"]["recorded_current_rows"] == 1
    assert report["summary"]["evaluated_pending_rows"] == 1
    assert report["summary"]["status_counts"]["evaluated"] == 1
    assert report["latest_rows"][0]["direction_hit"] == 1
    exact = report["adjustment_map"]["broad_market_down|SPY|long_put"]
    assert exact["status"] == "active"
    assert exact["score_adjustment"] > 0
    assert report["safety"]["ibkr_order_submitted"] is False
    assert report["safety"]["live_routing_enabled"] is False


def test_m241_7_applies_m241_8_tuning_adjustment() -> None:
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
        cross_asset_tuning_payload={
            "adjustment_map": {
                "broad_market_down|SPY|long_put": {
                    "status": "active",
                    "score_adjustment": 6.0,
                    "evaluated_samples": 4,
                    "hit_rate": 0.75,
                }
            }
        },
        min_expression_score=20.0,
        now="2026-07-04T12:00:00+00:00",
    )

    spy_put = next(row for row in report["expression_rankings"] if row["symbol"] == "SPY" and row["expression"] == "long_put")

    assert spy_put["score_components"]["m241_8_tuning_adjustment"] == 6.0
    assert any(str(reason).startswith("m241_8_exact_tuning") for reason in spy_put["reasons"])
