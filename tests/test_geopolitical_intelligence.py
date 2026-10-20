from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from recorder.geopolitical_intelligence import (
    adaptive_assets_for_region_theme,
    build_geopolitical_heatmap,
    build_geopolitical_intelligence_report,
    signal_scope,
)


class _SQLiteDb:
    def __init__(self, path: Path) -> None:
        self.path = path

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn


def test_geopolitical_intelligence_records_outcomes_and_adaptive_assets(tmp_path: Path) -> None:
    db = _SQLiteDb(tmp_path / "geo.db")
    with db.connect() as conn:
        conn.executescript(
            """
            CREATE TABLE external_market_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                external_id TEXT NOT NULL,
                title TEXT NOT NULL,
                published_at TEXT NOT NULL,
                region TEXT NOT NULL,
                theme TEXT NOT NULL,
                sentiment_score REAL,
                sentiment_label TEXT NOT NULL,
                confidence REAL NOT NULL,
                assets_json TEXT NOT NULL DEFAULT '[]',
                market_reaction_status TEXT NOT NULL DEFAULT '',
                prediction_gate_status TEXT NOT NULL DEFAULT '',
                raw_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE market_baselines (
                symbol TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                selected_price REAL,
                selected_price_source TEXT
            );
            CREATE TABLE market_reactions (
                symbol TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                reaction_price REAL,
                price_source TEXT
            );
            """
        )
        conn.execute(
            """
            INSERT INTO external_market_signals(
                source,external_id,title,published_at,region,theme,sentiment_score,
                sentiment_label,confidence,assets_json,market_reaction_status,
                prediction_gate_status,raw_json,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "emergency_breaking_news",
                "geo-1",
                "Breaking: missile strike near Hormuz lifts oil supply risk",
                "2026-07-01T12:00:00+00:00",
                "middle_east",
                "energy_supply",
                -0.8,
                "negative",
                0.92,
                json.dumps([{"symbol": "USO"}, {"symbol": "GLD"}, {"symbol": "SPY"}]),
                "available",
                "eligible",
                json.dumps({"urgency": {"urgency": "critical"}}),
                "2026-07-01T12:00:01+00:00",
                "2026-07-01T12:00:01+00:00",
            ),
        )
        conn.execute(
            """
            INSERT INTO external_market_signals(
                source,external_id,title,published_at,region,theme,sentiment_score,
                sentiment_label,confidence,assets_json,market_reaction_status,
                prediction_gate_status,raw_json,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "emergency_breaking_news",
                "noise-1",
                "Breaking: S&P 500 futures rise as traders await jobs report",
                "2026-07-01T12:01:00+00:00",
                "global",
                "market_news",
                0.1,
                "neutral",
                0.8,
                json.dumps([{"symbol": "SPY"}]),
                "available",
                "eligible",
                json.dumps({"urgency": {"urgency": "high"}}),
                "2026-07-01T12:01:01+00:00",
                "2026-07-01T12:01:01+00:00",
            ),
        )
        for symbol, baseline, outcome in (("USO", 75.0, 76.0), ("GLD", 240.0, 241.2), ("SPY", 620.0, 618.0)):
            conn.execute(
                "INSERT INTO market_baselines(symbol,captured_at,selected_price,selected_price_source) VALUES (?,?,?,?)",
                (symbol, "2026-07-01T11:59:00+00:00", baseline, "fixture"),
            )
            conn.execute(
                "INSERT INTO market_reactions(symbol,captured_at,reaction_price,price_source) VALUES (?,?,?,?)",
                (symbol, "2026-07-01T12:05:00+00:00", outcome, "fixture"),
            )
        conn.commit()

    result = build_geopolitical_intelligence_report(
        db,
        now="2026-07-01T12:30:00+00:00",
        lookback_hours=24,
        horizons_minutes="5",
        signal_limit=20,
        max_assets_per_signal=3,
    )
    adaptive = adaptive_assets_for_region_theme(
        db,
        region="middle_east",
        theme="energy_supply",
        fallback_assets=("USO", "XLE", "GLD"),
        min_weight=0.1,
        limit=5,
    )
    heatmap = build_geopolitical_heatmap(db, lookback_hours=100000, limit=5)

    assert result["candidate_signal_count"] == 2
    assert result["source_signal_count"] == 1
    assert result["signal_scope_counts"]["geopolitical"] == 1
    assert result["signal_scope_counts"]["macro_policy"] == 1
    assert result["recorded_count"] == 3
    assert result["duplicate_skipped_count"] == 0
    assert result["evaluated_count"] == 3
    assert result["status_counts"]["evaluated"] == 3
    assert result["coverage_audit"]["candidate_asset_counts"]["USO"] == 1
    assert "USO" in adaptive
    assert heatmap["top_buckets"][0]["region"] == "middle_east"
    assert heatmap["top_buckets"][0]["theme"] == "energy_supply"

    duplicate_run = build_geopolitical_intelligence_report(
        db,
        now="2026-07-01T12:31:00+00:00",
        lookback_hours=24,
        horizons_minutes="5",
        signal_limit=20,
        max_assets_per_signal=3,
        evaluate_pending=False,
    )
    assert duplicate_run["recorded_count"] == 0
    assert duplicate_run["duplicate_skipped_count"] == 3


def test_signal_scope_separates_geo_macro_and_market_noise() -> None:
    assert (
        signal_scope(
            {
                "source": "emergency_breaking_news",
                "title": "Breaking: missile strike near Hormuz sends oil supply risk higher",
                "region": "middle_east",
                "theme": "energy_supply",
                "raw_json": "{}",
            }
        )
        == "geopolitical"
    )
    assert (
        signal_scope(
            {
                "source": "emergency_breaking_news",
                "title": "Breaking: S&P 500 futures rise as traders await jobs report",
                "region": "global",
                "theme": "market_news",
                "raw_json": "{}",
            }
        )
        == "macro_policy"
    )
    assert (
        signal_scope(
            {
                "source": "marketaux",
                "title": "Peconic Partners Stock Portfolio: 9 Stock Picks",
                "region": "global",
                "theme": "market_news",
                "raw_json": json.dumps({"description": "A hedge fund article mentions geopolitical uncertainty and jobs report timing."}),
            }
        )
        == "macro_policy"
    )
    assert (
        signal_scope(
            {
                "source": "marketaux",
                "title": "Best Stocks To Buy According To Analysts",
                "region": "global",
                "theme": "market_news",
                "raw_json": json.dumps({"description": "A generic portfolio and analyst stock-pick article."}),
            }
        )
        == "market_noise"
    )
