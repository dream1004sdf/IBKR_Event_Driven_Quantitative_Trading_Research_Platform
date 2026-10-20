from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .database import Database
from .dedup import DedupConfig, NewsDeduplicator
from .models import NewsInput, QuoteSnapshot
from .price_cache import PriceCache
from .reactions import ReactionTracker

UTC = timezone.utc


def _news(
    provider: str,
    article_id: str,
    headline: str,
    at: datetime,
    symbol: str = "MU",
) -> NewsInput:
    return NewsInput(
        provider=provider,
        article_id=article_id,
        headline=headline,
        published_at=at,
        received_at=at,
        primary_symbol=symbol,
    )


def run_self_test() -> dict[str, object]:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "self_test.db"
        db = Database(db_path)
        dedup = NewsDeduplicator(
            db,
            DedupConfig(
                hash_window_hours=24,
                fuzzy_window_minutes=30,
                fuzzy_threshold=89,
                fuzzy_require_same_symbol=True,
            ),
        )
        t0 = datetime(2026, 6, 25, 14, 0, tzinfo=UTC)

        original = dedup.ingest(
            _news(
                "DJ-RT",
                "A1",
                "Micron raises quarterly revenue outlook on stronger AI memory demand",
                t0,
            )
        )
        assert not original.is_duplicate

        exact_id = dedup.ingest(
            _news(
                "DJ-RT",
                "A1",
                "Micron raises quarterly revenue outlook on stronger AI memory demand",
                t0 + timedelta(seconds=10),
            )
        )
        assert exact_id.is_duplicate and exact_id.reason == "provider_article_id"

        exact_hash = dedup.ingest(
            _news(
                "BRFG",
                "B2",
                "BREAKING: Micron raises quarterly revenue outlook on stronger AI memory demand!",
                t0 + timedelta(minutes=2),
            )
        )
        assert exact_hash.is_duplicate and exact_hash.reason == "normalized_headline_hash"

        fuzzy = dedup.ingest(
            _news(
                "DJ-GLOBAL",
                "C3",
                "Micron lifts quarterly revenue outlook on stronger AI memory demand",
                t0 + timedelta(minutes=5),
            )
        )
        assert fuzzy.is_duplicate and fuzzy.reason == "fuzzy_time_window"

        outside_window = dedup.ingest(
            _news(
                "DJ-GLOBAL",
                "D4",
                "Micron lifts quarterly revenue outlook on stronger AI memory demand",
                t0 + timedelta(hours=2),
            )
        )
        assert not outside_window.is_duplicate

        prices = PriceCache()
        prices.put_snapshot(
            QuoteSnapshot(
                symbol="MU",
                timestamp=t0,
                bid=99.9,
                ask=100.1,
                last=100.0,
                close=98.0,
                volume=1_000_000,
            )
        )
        tracker = ReactionTracker(db, prices, horizons=("1m",), max_price_age_seconds=600)
        tracker.register_event(original.canonical_news_id, t0, ("MU",))

        prices.put_snapshot(
            QuoteSnapshot(
                symbol="MU",
                timestamp=t0 + timedelta(minutes=1, seconds=5),
                bid=101.9,
                ask=102.1,
                last=102.0,
                close=98.0,
                volume=1_100_000,
            )
        )
        completed = tracker.process_due(t0 + timedelta(minutes=1, seconds=5))
        assert completed == 1

        # Re-open the same SQLite file to verify persistence across restart.
        reopened = Database(db_path)
        summary = reopened.summary()
        assert summary["news"] == 2
        assert summary["duplicates"] == 3
        assert summary["completed_reactions"] == 1

        return {
            "status": "PASS",
            "canonical_news": summary["news"],
            "suppressed_duplicates": summary["duplicates"],
            "completed_reactions": summary["completed_reactions"],
            "database_restart_persistence": True,
            "tested_dedup_layers": [
                "provider+article_id",
                "normalized headline hash",
                "fuzzy similar headline within time window",
            ],
        }
