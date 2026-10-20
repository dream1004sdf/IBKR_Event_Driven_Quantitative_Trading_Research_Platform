from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from freezegun import freeze_time

from recorder.single_stock_option_watchlist import (
    SingleStockOptionWatchlistConfig,
    build_single_stock_option_watchlist,
    load_single_stock_option_watchlist_config,
    run_single_stock_option_watchlist,
    single_stock_option_watchlist_markdown,
)


def _news_db(path: Path) -> Path:
    con = sqlite3.connect(str(path))
    con.execute(
        """
        CREATE TABLE news_articles (
            id INTEGER PRIMARY KEY,
            provider TEXT,
            primary_symbol TEXT,
            headline TEXT,
            published_at TEXT,
            received_at TEXT,
            provider_published_at TEXT,
            created_at TEXT
        )
        """
    )
    con.execute(
        """INSERT INTO news_articles(provider,primary_symbol,headline,published_at,received_at,provider_published_at,created_at)
           VALUES('BZ','NVDA','NVDA announces new AI chip orders','2026-07-02T18:00:00+00:00','2026-07-02T18:00:01+00:00',NULL,'2026-07-02T18:00:01+00:00')"""
    )
    con.execute(
        """INSERT INTO news_articles(provider,primary_symbol,headline,published_at,received_at,provider_published_at,created_at)
           VALUES('BZ','SPY','SPY broad market headline should be excluded','2026-07-02T18:01:00+00:00','2026-07-02T18:01:01+00:00',NULL,'2026-07-02T18:01:01+00:00')"""
    )
    con.execute(
        """INSERT INTO news_articles(provider,primary_symbol,headline,published_at,received_at,provider_published_at,created_at)
           VALUES('BZ','BROADTAPE:BZ','Breaking: TSLA shares move after robotaxi report','2026-07-02T18:02:00+00:00','2026-07-02T18:02:01+00:00',NULL,'2026-07-02T18:02:01+00:00')"""
    )
    con.commit()
    con.close()
    return path


@freeze_time("2026-07-02 19:00:00")
def test_single_stock_option_watchlist_selects_news_triggered_stocks(tmp_path: Path) -> None:
    db_path = _news_db(tmp_path / "events.db")
    market_data = tmp_path / "market.json"
    market_data.write_text(
        json.dumps({"rows": [{"key": "NVDA", "price": 160.0}, {"key": "TSLA", "price": 320.0}, {"key": "SPY", "price": 740.0}]}),
        encoding="utf-8",
    )
    config = SingleStockOptionWatchlistConfig(
        database_path=db_path,
        market_data_json_path=market_data,
        quote_json_path=tmp_path / "quote.json",
        quote_markdown_path=tmp_path / "quote.md",
        structure_json_path=tmp_path / "structure.json",
        structure_markdown_path=tmp_path / "structure.md",
        outcome_json_path=tmp_path / "outcome.json",
        outcome_markdown_path=tmp_path / "outcome.md",
        base_watchlist_symbols=["NVDA", "TSLA"],
        theme_baskets={},
        theme_keywords={},
        max_symbols=5,
        news_lookback_minutes=10_000,
    )

    report = build_single_stock_option_watchlist(config)

    assert report["status"] == "ready"
    assert set(report["selected_symbols"]) == {"NVDA", "TSLA"}
    assert report["watchlist_version"].endswith("institutional-adaptive")
    assert report["candidate_rows"][0]["tier"] == "tier_2_event"
    assert "score_components" in report["candidate_rows"][0]
    assert "trade_permission" in report["candidate_rows"][0]
    assert "SPY" not in report["selected_symbols"]
    assert report["reference_prices"] == {"NVDA": 160.0, "TSLA": 320.0}


def test_single_stock_option_watchlist_plans_read_only_commands(tmp_path: Path) -> None:
    db_path = _news_db(tmp_path / "events.db")
    config = SingleStockOptionWatchlistConfig(
        database_path=db_path,
        quote_json_path=tmp_path / "quote.json",
        quote_markdown_path=tmp_path / "quote.md",
        structure_json_path=tmp_path / "structure.json",
        structure_markdown_path=tmp_path / "structure.md",
        outcome_json_path=tmp_path / "outcome.json",
        outcome_markdown_path=tmp_path / "outcome.md",
        adaptive_state_json_path=tmp_path / "state.json",
        inclusion_log_jsonl_path=tmp_path / "inclusions.jsonl",
        base_watchlist_symbols=["NVDA", "TSLA"],
        max_symbols=2,
        news_lookback_minutes=10_000,
    )

    report = run_single_stock_option_watchlist(config, cwd=tmp_path, execute_actions=False)
    markdown = single_stock_option_watchlist_markdown(report)

    assert report["execute_actions"] is False
    assert report["action_status_counts"] == {"planned": 3}
    assert (tmp_path / "state.json").exists()
    assert (tmp_path / "inclusions.jsonl").exists()
    assert [action["action_id"] for action in report["actions"]] == [
        "single_stock_option_quote_snapshot",
        "single_stock_options_market_structure_rebuild",
        "single_stock_option_structure_outcome_log",
    ]
    assert report["actions"][0]["command"][2] == "ibkr-options-quote-snapshot"
    assert "--symbols" in report["actions"][0]["command"]
    assert "Single-Stock Option Watchlist" in markdown


def test_single_stock_option_watchlist_profile_loads_paths(tmp_path: Path) -> None:
    profile = tmp_path / "profile.json"
    profile.write_text(
        json.dumps(
            {
                "paths": {
                    "database_path": "events.db",
                    "market_data_json_path": "market.json",
                    "quote_json_path": "quote.json",
                    "quote_markdown_path": "quote.md",
                },
                "watchlist": {"base_watchlist_symbols": ["NVDA"], "max_symbols": 1},
                "quote_snapshot": {"max_quote_contracts": 20},
                "outcome_learning": {"enabled": False},
            }
        ),
        encoding="utf-8",
    )

    config = load_single_stock_option_watchlist_config(profile)

    assert config.database_path == tmp_path / "events.db"
    assert config.market_data_json_path == tmp_path / "market.json"
    assert config.quote_json_path == tmp_path / "quote.json"
    assert config.max_quote_contracts == 20
    assert config.outcome_enabled is False
    assert config.adaptive_enabled is True


@freeze_time("2026-07-02 19:00:00")
def test_single_stock_option_watchlist_adaptive_theme_basket_and_tier_budget(tmp_path: Path) -> None:
    db_path = tmp_path / "events.db"
    con = sqlite3.connect(str(db_path))
    con.execute(
        """
        CREATE TABLE news_articles (
            id INTEGER PRIMARY KEY,
            provider TEXT,
            primary_symbol TEXT,
            headline TEXT,
            published_at TEXT,
            received_at TEXT,
            provider_published_at TEXT,
            created_at TEXT
        )
        """
    )
    con.execute(
        """INSERT INTO news_articles(provider,primary_symbol,headline,published_at,received_at,provider_published_at,created_at)
           VALUES('BZ','BROADTAPE:BZ','AI data center chip demand lifts semiconductor complex','2026-07-02T18:00:00+00:00','2026-07-02T18:00:01+00:00',NULL,'2026-07-02T18:00:01+00:00')"""
    )
    con.commit()
    con.close()
    config = SingleStockOptionWatchlistConfig(
        database_path=db_path,
        quote_json_path=tmp_path / "quote.json",
        quote_markdown_path=tmp_path / "quote.md",
        structure_json_path=tmp_path / "structure.json",
        structure_markdown_path=tmp_path / "structure.md",
        outcome_json_path=tmp_path / "outcome.json",
        outcome_markdown_path=tmp_path / "outcome.md",
        adaptive_state_json_path=tmp_path / "state.json",
        inclusion_log_jsonl_path=tmp_path / "inclusions.jsonl",
        base_watchlist_symbols=["NVDA", "AMD", "AVGO", "MU"],
        theme_baskets={"ai_compute": ["NVDA", "AMD", "AVGO", "MU"]},
        theme_keywords={"ai_compute": ["AI", "CHIP"]},
        tier_budgets={"tier_4_theme_regime": 2, "tier_1_core": 0, "tier_2_event": 2},
        max_symbols=3,
        news_lookback_minutes=10_000,
    )

    report = build_single_stock_option_watchlist(config)

    assert "AMD" in report["selected_symbols"] or "AVGO" in report["selected_symbols"] or "MU" in report["selected_symbols"]
    assert any(row["tier"] == "tier_4_theme_regime" for row in report["candidate_rows"])
    assert any("ai_compute" in row["themes"] for row in report["candidate_rows"])
