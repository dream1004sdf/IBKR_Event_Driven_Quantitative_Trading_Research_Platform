from datetime import datetime, timedelta, timezone

from recorder.database import Database
from recorder.dedup import DedupConfig, NewsDeduplicator
from recorder.models import NewsInput

UTC = timezone.utc


def news(provider, article_id, headline, at, symbol="MU"):
    return NewsInput(provider, article_id, headline, at, at, symbol)


def test_all_three_dedup_layers(tmp_path):
    db = Database(tmp_path / "test.db")
    dedup = NewsDeduplicator(
        db,
        DedupConfig(fuzzy_threshold=89, fuzzy_window_minutes=30),
    )
    t0 = datetime(2026, 6, 25, 14, 0, tzinfo=UTC)
    first = dedup.ingest(
        news(
            "DJ-RT",
            "1",
            "Micron raises quarterly revenue outlook on stronger AI memory demand",
            t0,
        )
    )
    assert not first.is_duplicate

    duplicate_id = dedup.ingest(
        news(
            "DJ-RT",
            "1",
            "Micron raises quarterly revenue outlook on stronger AI memory demand",
            t0 + timedelta(seconds=1),
        )
    )
    assert duplicate_id.reason == "provider_article_id"

    duplicate_hash = dedup.ingest(
        news(
            "BRFG",
            "2",
            "BREAKING: Micron raises quarterly revenue outlook on stronger AI memory demand!",
            t0 + timedelta(minutes=1),
        )
    )
    assert duplicate_hash.reason == "normalized_headline_hash"

    duplicate_fuzzy = dedup.ingest(
        news(
            "DJ-GLOBAL",
            "3",
            "Micron lifts quarterly revenue outlook on stronger AI memory demand",
            t0 + timedelta(minutes=4),
        )
    )
    assert duplicate_fuzzy.reason == "fuzzy_time_window"


def test_fuzzy_is_time_and_symbol_bounded(tmp_path):
    db = Database(tmp_path / "test.db")
    dedup = NewsDeduplicator(
        db,
        DedupConfig(
            fuzzy_threshold=89,
            fuzzy_window_minutes=30,
            fuzzy_require_same_symbol=True,
        ),
    )
    t0 = datetime(2026, 6, 25, 14, 0, tzinfo=UTC)
    dedup.ingest(
        news(
            "DJ-RT",
            "1",
            "Micron lifts revenue forecast as demand for AI memory strengthens",
            t0,
        )
    )
    other_symbol = dedup.ingest(
        news(
            "DJ-RT",
            "2",
            "Micron lifts quarterly revenue outlook on stronger AI memory demand",
            t0 + timedelta(minutes=2),
            symbol="NVDA",
        )
    )
    outside_window = dedup.ingest(
        news(
            "DJ-RT",
            "3",
            "Micron lifts revenue outlook as demand for AI memory strengthens",
            t0 + timedelta(hours=1),
        )
    )
    assert not other_symbol.is_duplicate
    assert not outside_window.is_duplicate


def test_same_article_preserves_multiple_symbol_links(tmp_path):
    db = Database(tmp_path / "symbols.db")
    dedup = NewsDeduplicator(db, DedupConfig())
    t0 = datetime(2026, 6, 25, 14, 0, tzinfo=UTC)
    first = dedup.ingest(
        news("DJ-RT", "SHARED", "A broad semiconductor headline with enough detail", t0, "MU")
    )
    second = dedup.ingest(
        news(
            "DJ-RT",
            "SHARED",
            "A broad semiconductor headline with enough detail",
            t0 + timedelta(seconds=5),
            "NVDA",
        )
    )
    assert not first.is_duplicate
    assert second.is_duplicate
    assert second.symbol_added
    assert db.summary()["news_symbol_links"] == 2
