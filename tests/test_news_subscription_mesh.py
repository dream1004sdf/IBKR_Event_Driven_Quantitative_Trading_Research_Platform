from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from recorder.news_subscription_mesh import (
    NewsSubscriptionMeshConfig,
    build_news_subscription_mesh,
    classify_geopolitical_headline,
)


def _make_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.execute(
        """
        CREATE TABLE news_articles (
            id INTEGER PRIMARY KEY,
            provider TEXT,
            primary_symbol TEXT,
            headline TEXT,
            body TEXT,
            published_at TEXT,
            received_at TEXT,
            provider_published_at TEXT,
            extra_data TEXT
        )
        """
    )
    now = "2026-07-02T10:00:00+00:00"
    mapping = {
        "source_mode": "ibkr_broadtape",
        "asset_mapping": {
            "inferred_symbols": ["XYZ"],
            "inferred_assets": [],
            "target_candidates": ["XYZ"],
            "confidence": "high",
            "reason": "direct ticker token detected in BroadTape headline",
            "source_mode": "ibkr_broadtape",
            "themes": [],
        },
    }
    con.execute(
        """
        INSERT INTO news_articles
        (id, provider, primary_symbol, headline, body, published_at, received_at, provider_published_at, extra_data)
        VALUES (1, 'BZ', 'BROADTAPE:BZ', 'XYZ shares jump after new AI infrastructure contract', '', ?, ?, ?, ?)
        """,
        (now, now, now, json.dumps(mapping)),
    )
    con.execute(
        """
        INSERT INTO news_articles
        (id, provider, primary_symbol, headline, body, published_at, received_at, provider_published_at, extra_data)
        VALUES (2, 'DJ-RTG', 'SPY', 'Oil rises as Iran tensions raise Hormuz shipping concerns', '', ?, ?, ?, '')
        """,
        (now, now, now),
    )
    con.commit()
    con.close()


def _make_universe(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "core_anchors": ["SPY", "QQQ", "NVDA"],
                "dow30": ["AAPL", "MSFT"],
                "nasdaq100_seed": ["AVGO"],
                "sp500_seed": ["XOM", "CVX"],
                "fx_macro_market_indicators": ["DXY", "US10Y", "VIX"],
            }
        ),
        encoding="utf-8",
    )


def test_news_subscription_mesh_builds_core_temp_and_geo(tmp_path: Path) -> None:
    db_path = tmp_path / "events.db"
    universe_path = tmp_path / "universe.json"
    _make_db(db_path)
    _make_universe(universe_path)

    payload = build_news_subscription_mesh(
        NewsSubscriptionMeshConfig(
            database_path=db_path,
            universe_json_path=universe_path,
            lookback_minutes=240,
            max_temp_symbols=10,
        ),
        now=datetime(2026, 7, 2, 10, 5, tzinfo=UTC),
    )

    assert payload["status"] == "ok"
    assert "SPY" in payload["core_universe"]["core_live_symbols"]
    assert "XYZ" in payload["broadtape_temp_subscription_queue"]["symbols"]
    candidate = payload["broadtape_temp_subscription_queue"]["candidates"][0]
    assert candidate["subscription_source"] == "broadtape_temp"
    assert candidate["confirmation_providers"] == ["DJ-N", "DJ-RT"]
    assert payload["dow_jones_geopolitical_candidates"]["candidate_count"] == 1
    geo = payload["dow_jones_geopolitical_candidates"]["candidates"][0]["geopolitical"]
    assert "Middle East" in geo["regions"]
    assert "USO" in geo["assets"]


def test_classify_geopolitical_headline_rejects_plain_market_trade_phrase() -> None:
    assert classify_geopolitical_headline("The AI trade keeps lifting megacap tech") is None
    assert classify_geopolitical_headline("China Billion Resources files HK results announcement") is None
    result = classify_geopolitical_headline("New tariffs escalate global trade war concerns")
    assert result is not None
    assert "Global Trade" in result["regions"]
