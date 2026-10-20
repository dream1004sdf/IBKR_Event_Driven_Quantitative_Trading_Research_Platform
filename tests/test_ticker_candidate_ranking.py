from __future__ import annotations

import sqlite3
from pathlib import Path

from recorder.ticker_candidate_ranking import build_ticker_candidate_ranking_report


def _build_db(path: Path) -> Path:
    con = sqlite3.connect(path)
    con.execute(
        """
        CREATE TABLE news_articles (
            id INTEGER PRIMARY KEY,
            provider TEXT,
            headline TEXT,
            primary_symbol TEXT,
            extra_data TEXT,
            provider_published_at TEXT,
            published_at TEXT,
            received_at TEXT,
            created_at TEXT
        )
        """
    )
    con.execute(
        """
        CREATE TABLE external_market_signals (
            id INTEGER PRIMARY KEY,
            source TEXT,
            title TEXT,
            region TEXT,
            theme TEXT,
            sentiment_label TEXT,
            assets_json TEXT,
            published_at TEXT,
            created_at TEXT
        )
        """
    )
    con.execute(
        """
        INSERT INTO news_articles
        VALUES (1, 'BZ', 'Nvidia and Broadcom rally as AI chip demand accelerates', 'BROADTAPE:BZ', NULL,
                NULL, '2026-07-04T12:00:00+00:00', '2026-07-04T12:00:00+00:00', '2026-07-04T12:00:00+00:00')
        """
    )
    con.execute(
        """
        INSERT INTO news_articles
        VALUES (2, 'BZ', 'Chevron gains as oil industry pressure intensifies', 'CVX', NULL,
                NULL, '2026-07-04T11:45:00+00:00', '2026-07-04T11:45:00+00:00', '2026-07-04T11:45:00+00:00')
        """
    )
    con.execute(
        """
        INSERT INTO external_market_signals
        VALUES (10, 'marketaux', 'Semiconductor ETFs extend gains on AI infrastructure demand', 'global', 'market_news',
                'positive', '["SMH", "SOXX", "NVDA"]', '2026-07-04T11:30:00+00:00', '2026-07-04T11:35:00+00:00')
        """
    )
    con.commit()
    con.close()
    return path


def test_m241_5_ticker_candidate_ranking_expands_and_scores_theme_candidates(tmp_path: Path) -> None:
    db = _build_db(tmp_path / "events.db")

    report = build_ticker_candidate_ranking_report(
        database_path=db,
        market_data_payload={
            "status": "green",
            "rows": [
                {"key": "NVDA", "status": "live", "price": 180.0, "alerts": []},
                {"key": "CVX", "status": "live", "price": 160.0, "alerts": []},
                {"key": "SMH", "status": "live", "price": 300.0, "alerts": ["gap_up"]},
            ],
        },
        options_market_structure_payload={
            "status": "green",
            "symbols": [
                {"symbol": "NVDA", "confidence": "medium", "quote_count": 40, "gamma_regime": "negative_gamma_trend_amplification", "pin_risk": False},
                {"symbol": "SMH", "confidence": "medium", "quote_count": 30, "gamma_regime": "positive_gamma_mean_reversion_bias", "pin_risk": False},
            ],
        },
        options_market_structure_supervisor_payload={"final_status": "green", "final_quote_usable": True},
        capital_state_payload={"status": "green", "budgets": {"options_premium_budget": 250.0}},
        news_learning_payload={
            "provider_profile": {
                "benzinga": {"learned_weight": {"overall": 0.90}},
                "marketaux": {"learned_weight": {"overall": 0.35}},
            }
        },
        now="2026-07-04T12:15:00+00:00",
        lookback_hours=6,
        max_ranked_candidates=20,
    )

    symbols = {row["symbol"]: row for row in report["ranking"]}
    assert report["ticker_candidate_ranking_version"].startswith("m253")
    assert report["input_window"]["input_mode"] == "legacy_article_signal"
    assert {"NVDA", "AVGO", "SMH", "CVX"}.issubset(symbols)
    assert symbols["NVDA"]["inferred_symbol_hit_count"] >= 1
    assert symbols["CVX"]["direct_symbol_hit_count"] == 1
    assert symbols["SMH"]["score_components"]["market_data_score"] > 0
    assert report["safety"]["ibkr_order_submitted"] is False
