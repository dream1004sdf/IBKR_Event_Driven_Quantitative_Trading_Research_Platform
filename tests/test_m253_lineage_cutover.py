from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

from recorder.analysis_database import AnalysisDatabase
from recorder.external_market_sources import ExternalMarketSignalCollector
from recorder.market_outcomes import DailyBar
from recorder.models import NewsInput
from recorder.news_lineage_v2 import rebuild_news_lineage_v2
from recorder.news_market_outcome_backfill import build_news_market_outcomes
from recorder.ticker_candidate_ranking import build_ticker_candidate_ranking_report


class FakeDailySource:
    name = "fake_daily"

    def get_daily_bars(self, symbol: str, start: date, end: date) -> list[DailyBar]:
        return [
            DailyBar(symbol=symbol, date=date(2026, 7, 3), close=100.0),
            DailyBar(symbol=symbol, date=date(2026, 7, 6), close=104.0),
            DailyBar(symbol=symbol, date=date(2026, 7, 7), close=108.0),
        ]


def _insert_news(
    db: AnalysisDatabase,
    *,
    provider: str,
    article_id: str,
    headline: str,
    primary_symbol: str,
    published_at: datetime,
) -> int:
    normalized = " ".join(headline.lower().split())
    return db.insert_news(
        NewsInput(
            provider=provider,
            article_id=article_id,
            headline=headline,
            published_at=published_at,
            received_at=published_at,
            primary_symbol=primary_symbol,
            extra_data="{}",
            provider_published_at=published_at,
            first_seen_at=published_at,
        ),
        normalized,
        hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
    )


def test_m253_ticker_ranking_uses_cluster_candidates_without_signal_double_count(tmp_path: Path) -> None:
    db = AnalysisDatabase(tmp_path / "m253-ranking.db")
    event_time = datetime(2026, 7, 6, 12, 0, tzinfo=UTC)
    _insert_news(
        db,
        provider="BZ",
        article_id="bz-nvda-m253",
        headline="Nvidia shares jump on AI chip demand",
        primary_symbol="BROADTAPE:BZ",
        published_at=event_time,
    )
    _insert_news(
        db,
        provider="DJNL",
        article_id="dj-nvda-m253",
        headline="Nvidia shares rise as AI chip demand lifts sector",
        primary_symbol="NVDA",
        published_at=event_time,
    )
    collector = ExternalMarketSignalCollector(db)
    collector.convert_benzinga_news(start=date(2026, 7, 1), end=date(2026, 7, 7), keywords_only=False)
    collector.convert_dow_jones_news(start=date(2026, 7, 1), end=date(2026, 7, 7), keywords_only=False)

    report = build_ticker_candidate_ranking_report(
        database_path=db.path,
        market_data_payload={"rows": [{"key": "NVDA", "status": "live", "price": 180.0}]},
        options_market_structure_payload={"symbols": [{"symbol": "NVDA", "confidence": "medium", "quote_count": 40}]},
        capital_state_payload={"status": "green", "budgets": {"options_premium_budget": 250.0}},
        now="2026-07-06T12:15:00+00:00",
        lookback_hours=6,
        max_ranked_candidates=10,
    )

    nvda = {row["symbol"]: row for row in report["ranking"]}["NVDA"]
    assert report["ticker_candidate_ranking_version"].startswith("m253")
    assert report["input_window"]["input_mode"] == "lineage_v2_cluster"
    assert nvda["event_count"] == 1
    assert nvda["direct_symbol_hit_count"] == 1
    assert nvda["score_components"]["provider_confirmation_score"] > 0
    assert nvda["recent_events"][0]["source_table"] == "news_event_clusters"


def test_m253_outcome_backfill_uses_one_cluster_symbol_candidate(tmp_path: Path) -> None:
    db = AnalysisDatabase(tmp_path / "m253-outcome.db")
    event_time = datetime(2026, 7, 6, 12, 0, tzinfo=UTC)
    _insert_news(
        db,
        provider="BZ",
        article_id="bz-outcome-m253",
        headline="Nvidia shares jump on AI chip demand",
        primary_symbol="BROADTAPE:BZ",
        published_at=event_time,
    )
    _insert_news(
        db,
        provider="DJNL",
        article_id="dj-outcome-m253",
        headline="Nvidia shares rise as AI chip demand lifts sector",
        primary_symbol="NVDA",
        published_at=event_time,
    )
    collector = ExternalMarketSignalCollector(db)
    collector.convert_benzinga_news(start=date(2026, 7, 1), end=date(2026, 7, 7), keywords_only=False)
    collector.convert_dow_jones_news(start=date(2026, 7, 1), end=date(2026, 7, 7), keywords_only=False)

    report = build_news_market_outcomes(
        db.path,
        start="2026-07-01",
        end="2026-07-07",
        symbols="NVDA",
        horizons=("NEXT_DAY",),
        price_source="yahoo",
        daily_price_source=FakeDailySource(),
    )

    assert report["candidate_count"] == 1
    assert report["candidate_source_counts"] == {"lineage_v2_cluster_candidate": 1}
    assert report["completed_outcomes"] == 1
    with sqlite3.connect(db.path) as conn:
        rows = conn.execute("SELECT news_id,symbol,horizon FROM market_reactions").fetchall()
    assert rows == [(1, "NVDA", "NEXT_DAY")]


def test_m253_broadtape_context_fallback_is_not_direct_outcome_candidate(tmp_path: Path) -> None:
    db = AnalysisDatabase(tmp_path / "m253-fallback.db")
    event_time = datetime(2026, 7, 6, 12, 0, tzinfo=UTC)
    _insert_news(
        db,
        provider="BZ",
        article_id="bz-context-m253",
        headline="Company announces strategic review",
        primary_symbol="BROADTAPE:BZ",
        published_at=event_time,
    )
    rebuild_news_lineage_v2(str(db.path), start="2026-07-01", end="2026-07-07")

    report = build_news_market_outcomes(
        db.path,
        start="2026-07-01",
        end="2026-07-07",
        symbols="SPY",
        horizons=("NEXT_DAY",),
        price_source="yahoo",
        daily_price_source=FakeDailySource(),
    )

    assert report["candidate_count"] == 0
    assert report["candidate_source_counts"] == {}
