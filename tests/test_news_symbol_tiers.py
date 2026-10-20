from __future__ import annotations

import sqlite3
from pathlib import Path

from recorder.news_symbol_tiers import (
    build_news_symbol_tier_report,
    candidate_allowed_by_symbol_tier,
    load_symbol_tier_policy,
)


def test_symbol_tier_policy_classifies_core_event_and_opportunistic() -> None:
    policy = load_symbol_tier_policy("config/news_market_outcome_symbol_tiers_v1.json")

    assert policy.classify("SPY").tier == "core_always"
    assert policy.classify("GLD").tier == "macro_proxy"
    assert policy.classify("ITA").collection_mode == "event_window"
    assert policy.classify("ADBE").collection_mode == "on_demand"
    assert policy.classify("UNKNOWN").tier == "unclassified_review"


def test_event_proxy_policy_requires_matching_news_or_primary_symbol() -> None:
    policy = load_symbol_tier_policy("config/news_market_outcome_symbol_tiers_v1.json")
    tier_filter = {"geopolitical_event_proxy"}

    assert candidate_allowed_by_symbol_tier(
        symbol="ITA",
        headline="U.S. launches retaliatory strikes; defense stocks and ETFs on watch",
        primary_symbol="SPY",
        policy=policy,
        tier_filter=tier_filter,
    )
    assert candidate_allowed_by_symbol_tier(
        symbol="ITA",
        headline="Unrelated software earnings roundup",
        primary_symbol="ITA",
        policy=policy,
        tier_filter=tier_filter,
    )
    assert not candidate_allowed_by_symbol_tier(
        symbol="ITA",
        headline="Unrelated software earnings roundup",
        primary_symbol="SPY",
        policy=policy,
        tier_filter=tier_filter,
    )
    assert not candidate_allowed_by_symbol_tier(
        symbol="ADBE",
        headline="Adobe shares move after earnings",
        primary_symbol="ADBE",
        policy=policy,
        tier_filter=tier_filter,
    )


def test_news_symbol_tier_report_includes_primary_exchange_candidates(tmp_path: Path) -> None:
    db_path = tmp_path / "tiers.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE news_articles(
                id INTEGER PRIMARY KEY,
                provider TEXT,
                primary_symbol TEXT,
                published_at TEXT,
                headline TEXT
            );
            CREATE TABLE news_symbols(news_id INTEGER, symbol TEXT);
            """
        )
        conn.execute(
            "INSERT INTO news_articles(id,provider,primary_symbol,published_at,headline) VALUES (1,'BZ','QQQ','2026-01-02T15:00:00+00:00','Nasdaq ETF moves')"
        )
        conn.execute("INSERT INTO news_symbols(news_id,symbol) VALUES (1,'QQQ')")
        conn.execute(
            "INSERT INTO news_articles(id,provider,primary_symbol,published_at,headline) VALUES (2,'BZ','NEWETF','2026-01-02T15:01:00+00:00','New ETF moves')"
        )
        conn.execute("INSERT INTO news_symbols(news_id,symbol) VALUES (2,'NEWETF')")
        conn.execute(
            "INSERT INTO news_articles(id,provider,primary_symbol,published_at,headline) VALUES (3,'BZ','AVGO','2026-01-02T15:02:00+00:00','Broadcom moves')"
        )
        conn.execute("INSERT INTO news_symbols(news_id,symbol) VALUES (3,'AVGO')")
        conn.execute(
            "INSERT INTO news_articles(id,provider,primary_symbol,published_at,headline) VALUES (4,'BZ','XOM','2026-01-02T15:03:00+00:00','Exxon moves')"
        )
        conn.execute("INSERT INTO news_symbols(news_id,symbol) VALUES (4,'XOM')")

    report = build_news_symbol_tier_report(
        db_path,
        start="2026-01-01",
        end="2026-01-03",
        config_path="config/news_market_outcome_symbol_tiers_v1.json",
    )

    by_symbol = {row["symbol"]: row for row in report["symbols"]}
    assert by_symbol["QQQ"]["primary_exchange_candidates"][:2] == ["NASDAQ", "ISLAND"]
    assert by_symbol["QQQ"]["contract_resolution_profile"]["gateway_exchange_aliases"] == ["NASDAQ", "ISLAND"]
    assert "nasdaq_island_gateway_alias" in by_symbol["QQQ"]["contract_resolution_risk_flags"]
    assert by_symbol["NEWETF"]["primary_exchange_candidates"] == ["NASDAQ", "ISLAND", "ARCA", "NYSE", "BATS", "AMEX"]
    assert by_symbol["NEWETF"]["requires_contract_probe"] is True
    assert "unknown_symbol_requires_contract_probe" in by_symbol["NEWETF"]["contract_resolution_risk_flags"]
    assert "foreign_or_cdr_same_ticker" in by_symbol["AVGO"]["contract_resolution_risk_flags"]
    assert by_symbol["AVGO"]["requires_contract_probe"] is True
    assert "foreign_or_cdr_same_ticker" in by_symbol["XOM"]["contract_resolution_risk_flags"]
    assert "corporate_action_or_new_conid" in by_symbol["XOM"]["contract_resolution_risk_flags"]
