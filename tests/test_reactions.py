from datetime import datetime, timedelta, timezone

from recorder.database import Database
from recorder.models import NewsInput, QuoteSnapshot
from recorder.dedup import DedupConfig, NewsDeduplicator
from recorder.price_cache import PriceCache
from recorder.reactions import ReactionTracker

UTC = timezone.utc


def test_reaction_capture_and_restart_persistence(tmp_path):
    db_path = tmp_path / "reaction.db"
    db = Database(db_path)
    t0 = datetime(2026, 6, 25, 14, 0, tzinfo=UTC)
    dedup = NewsDeduplicator(db, DedupConfig())
    item = NewsInput("DJ-RT", "A", "A sufficiently long unique market headline", t0, t0, "MU")
    news_id = dedup.ingest(item).canonical_news_id

    cache = PriceCache()
    cache.put_snapshot(QuoteSnapshot("MU", t0, bid=99.9, ask=100.1, last=100.0))
    tracker = ReactionTracker(db, cache, ("1m",), max_price_age_seconds=600)
    tracker.register_event(news_id, t0, ("MU",))

    cache.put_snapshot(
        QuoteSnapshot("MU", t0 + timedelta(minutes=1, seconds=2), bid=101.9, ask=102.1, last=102)
    )
    assert tracker.process_due(t0 + timedelta(minutes=1, seconds=2)) == 1

    reopened = Database(db_path)
    assert reopened.summary()["completed_reactions"] == 1


def test_overdue_target_is_not_filled_with_wrong_current_price(tmp_path):
    db = Database(tmp_path / "overdue.db")
    t0 = datetime(2026, 6, 25, 14, 0, tzinfo=UTC)
    dedup = NewsDeduplicator(db, DedupConfig())
    item = NewsInput("DJ-RT", "B", "Another sufficiently long unique market headline", t0, t0, "MU")
    news_id = dedup.ingest(item).canonical_news_id

    cache = PriceCache()
    cache.put_snapshot(QuoteSnapshot("MU", t0, last=100.0))
    tracker = ReactionTracker(
        db,
        cache,
        ("1m",),
        max_price_age_seconds=600,
        max_lateness_seconds=30,
    )
    tracker.register_event(news_id, t0, ("MU",))

    much_later = t0 + timedelta(minutes=10)
    cache.put_snapshot(QuoteSnapshot("MU", much_later, last=110.0))
    assert tracker.process_due(much_later) == 0
    assert db.summary()["completed_reactions"] == 0
    assert db.summary()["pending_targets"] == 0
    with db.connect() as conn:
        row = conn.execute("SELECT status,last_error FROM reaction_targets").fetchone()
    assert row["status"] == "missed"
    assert "historical backfill required" in row["last_error"]


def test_missing_reaction_quote_uses_backoff_and_terminal_attempt_limit(tmp_path):
    db = Database(tmp_path / "retry.db")
    t0 = datetime(2026, 6, 25, 14, 0, tzinfo=UTC)
    dedup = NewsDeduplicator(db, DedupConfig())
    item = NewsInput("DJ-RT", "C", "A third sufficiently long unique market headline", t0, t0, "MU")
    news_id = dedup.ingest(item).canonical_news_id

    initial = PriceCache()
    initial.put_snapshot(QuoteSnapshot("MU", t0, bid=99.9, ask=100.1, last=100.0))
    registrar = ReactionTracker(db, initial, ("1m",), max_price_age_seconds=600)
    registrar.register_event(news_id, t0, ("MU",))

    empty = PriceCache()
    tracker = ReactionTracker(
        db,
        empty,
        ("1m",),
        max_price_age_seconds=600,
        max_lateness_seconds=600,
        max_attempts=2,
        retry_base_seconds=5,
        retry_max_seconds=5,
    )
    due = t0 + timedelta(minutes=1)
    assert tracker.process_due(due) == 0
    with db.connect() as conn:
        first = conn.execute("SELECT status,attempts,next_attempt_at FROM reaction_targets").fetchone()
    assert first["status"] == "pending"
    assert first["attempts"] == 1

    # The same poll time is before next_attempt_at, so it must not spin/retry again.
    assert tracker.process_due(due) == 0
    with db.connect() as conn:
        unchanged = conn.execute("SELECT status,attempts FROM reaction_targets").fetchone()
    assert unchanged["attempts"] == 1

    assert tracker.process_due(due + timedelta(seconds=6)) == 0
    with db.connect() as conn:
        final = conn.execute("SELECT status,attempts FROM reaction_targets").fetchone()
    assert final["status"] == "failed"
    assert final["attempts"] == 2
