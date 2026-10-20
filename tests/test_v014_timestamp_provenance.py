from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from recorder.database import Database
from recorder.models import NewsInput

UTC = timezone.utc


def test_legacy_news_schema_is_upgraded_to_receipt_only_provenance(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    at = datetime(2025, 1, 2, 14, 30, tzinfo=UTC).isoformat()
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE news_articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,provider TEXT NOT NULL,
            article_id TEXT NOT NULL,headline TEXT NOT NULL,
            normalized_headline TEXT NOT NULL,headline_hash TEXT NOT NULL,
            body TEXT,published_at TEXT NOT NULL,received_at TEXT NOT NULL,
            primary_symbol TEXT NOT NULL,con_id INTEGER,extra_data TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,UNIQUE(provider,article_id)
        )"""
    )
    conn.execute(
        """INSERT INTO news_articles(
            provider,article_id,headline,normalized_headline,headline_hash,
            published_at,received_at,primary_symbol,created_at
        ) VALUES (?,?,?,?,?,?,?,?,?)""",
        ("TEST", "legacy", "Legacy event", "legacy event", "legacy", at, at, "MU", at),
    )
    conn.commit()
    conn.close()

    db = Database(path)
    with db.connect() as conn:
        row = conn.execute(
            """SELECT first_seen_at,timestamp_quality,timestamp_source
               FROM news_articles WHERE article_id='legacy'"""
        ).fetchone()
    assert row["first_seen_at"] == at
    assert row["timestamp_quality"] == "receipt_only"
    assert row["timestamp_source"] == "received_at"


def test_provider_timestamp_provenance_is_stored_as_utc(tmp_path: Path) -> None:
    db = Database(tmp_path / "provenance.db")
    received = datetime(2025, 1, 2, 14, 35, tzinfo=UTC)
    published = received - timedelta(minutes=5)
    accepted = received - timedelta(minutes=4)
    news_id = db.insert_news(
        NewsInput(
            provider="TEST",
            article_id="provenance",
            headline="Timestamp provenance event",
            published_at=published,
            received_at=received,
            primary_symbol="MU",
            provider_published_at=published,
            source_accepted_at=accepted,
            first_seen_at=received,
        ),
        "timestamp provenance event",
        "provenance-hash",
    )
    with db.connect() as conn:
        row = conn.execute(
            """SELECT provider_published_at,source_accepted_at,first_seen_at,
                      received_at,timestamp_quality,timestamp_source
               FROM news_articles WHERE id=?""",
            (news_id,),
        ).fetchone()
    assert row["provider_published_at"] == published.isoformat()
    assert row["source_accepted_at"] == accepted.isoformat()
    assert row["first_seen_at"] == received.isoformat()
    assert row["received_at"] == received.isoformat()
    assert row["timestamp_quality"] == "source_accepted"
    assert row["timestamp_source"] == "source_accepted_at"
