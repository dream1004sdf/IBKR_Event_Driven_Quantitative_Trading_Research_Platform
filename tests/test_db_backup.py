from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from recorder.db_backup import backup_database

UTC = timezone.utc


def _make_source_db(path) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.execute("INSERT INTO t (v) VALUES ('hello')")
    conn.commit()
    conn.close()


def test_backup_creates_valid_readable_snapshot(tmp_path) -> None:
    source = tmp_path / "market_events.db"
    _make_source_db(source)

    result = backup_database(source, tmp_path / "backups", keep=14)

    backup_path = Path(result["backup_path"])
    assert backup_path.exists()
    conn = sqlite3.connect(backup_path)
    rows = conn.execute("SELECT v FROM t").fetchall()
    conn.close()
    assert rows == [("hello",)]


def test_backup_missing_source_raises(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        backup_database(tmp_path / "does_not_exist.db", tmp_path / "backups", keep=14)


def test_backup_prunes_beyond_keep_limit(tmp_path) -> None:
    source = tmp_path / "market_events.db"
    _make_source_db(source)
    backup_dir = tmp_path / "backups"

    base = datetime(2026, 1, 1, tzinfo=UTC)
    timestamps = [base + timedelta(minutes=i) for i in range(5)]
    for ts in timestamps:
        backup_database(source, backup_dir, keep=3, now=ts)

    remaining = {p.name for p in backup_dir.glob("market_events_*.db")}
    expected_survivors = {
        f"market_events_{ts.strftime('%Y%m%dT%H%M%SZ')}.db" for ts in timestamps[-3:]
    }
    assert remaining == expected_survivors
