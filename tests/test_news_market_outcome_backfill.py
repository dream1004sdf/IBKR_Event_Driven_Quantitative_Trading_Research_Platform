from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from recorder.market_outcomes import DailyBar, IntradayBar
from recorder.news_market_outcome_backfill import build_news_market_outcomes


UTC = timezone.utc


class FakeDailySource:
    name = "fake_daily"

    def get_daily_bars(self, symbol: str, start: date, end: date) -> list[DailyBar]:
        return [
            DailyBar(symbol=symbol, date=date(2026, 1, 1), close=100.0),
            DailyBar(symbol=symbol, date=date(2026, 1, 2), close=105.0),
            DailyBar(symbol=symbol, date=date(2026, 1, 5), close=110.0),
        ]


class FakeIntradaySource:
    name = "fake_intraday"

    def get_intraday_bars(self, symbol: str, start: datetime, end: datetime) -> list[IntradayBar]:
        return [
            IntradayBar(symbol=symbol, timestamp=datetime(2026, 1, 2, 14, 30, tzinfo=UTC), close=100.0),
            IntradayBar(symbol=symbol, timestamp=datetime(2026, 1, 2, 14, 35, tzinfo=UTC), close=101.0),
            IntradayBar(symbol=symbol, timestamp=datetime(2026, 1, 2, 14, 45, tzinfo=UTC), close=102.0),
            IntradayBar(symbol=symbol, timestamp=datetime(2026, 1, 2, 15, 30, tzinfo=UTC), close=103.0),
            IntradayBar(symbol=symbol, timestamp=datetime(2026, 1, 2, 21, 0, tzinfo=UTC), close=104.0),
        ]


class FakeIntradayWeekendCloseSource:
    name = "fake_intraday_weekend"

    def get_intraday_bars(self, symbol: str, start: datetime, end: datetime) -> list[IntradayBar]:
        return [
            IntradayBar(symbol=symbol, timestamp=datetime(2026, 1, 2, 20, 59, tzinfo=UTC), close=99.0),
            IntradayBar(symbol=symbol, timestamp=datetime(2026, 1, 2, 21, 0, tzinfo=UTC), close=100.0),
            IntradayBar(symbol=symbol, timestamp=datetime(2026, 1, 5, 14, 30, tzinfo=UTC), close=101.0),
            IntradayBar(symbol=symbol, timestamp=datetime(2026, 1, 5, 18, 0, tzinfo=UTC), close=102.0),
            IntradayBar(symbol=symbol, timestamp=datetime(2026, 1, 5, 21, 0, tzinfo=UTC), close=103.0),
        ]


class FakeEmptyIntradaySource:
    name = "fake_empty_intraday"

    def get_intraday_bars(self, symbol: str, start: datetime, end: datetime) -> list[IntradayBar]:
        return []


class FakeCloseRetryFailingIntradaySource:
    name = "fake_retry_failing_intraday"

    def __init__(self) -> None:
        self.normal_calls = 0
        self.wide_retry_calls = 0

    def get_intraday_bars(self, symbol: str, start: datetime, end: datetime) -> list[IntradayBar]:
        if end - start > timedelta(days=2):
            self.wide_retry_calls += 1
            raise TimeoutError(f"simulated wide close retry timeout for {symbol}")
        self.normal_calls += 1
        return [
            IntradayBar(symbol=symbol, timestamp=start + (end - start) / 4, close=100.0),
        ]


class FakeWideDailySource:
    name = "fake_daily"

    def get_daily_bars(self, symbol: str, start: date, end: date) -> list[DailyBar]:
        return [
            DailyBar(symbol=symbol, date=date(2026, 1, 1), close=100.0),
            DailyBar(symbol=symbol, date=date(2026, 1, 2), close=101.0),
            DailyBar(symbol=symbol, date=date(2026, 1, 5), close=102.0),
            DailyBar(symbol=symbol, date=date(2026, 1, 6), close=103.0),
        ]


def _make_db(path: Path) -> None:
    con = sqlite3.connect(path)
    try:
        con.executescript(
            """
            CREATE TABLE news_articles (
                id INTEGER PRIMARY KEY,
                provider TEXT,
                article_id TEXT,
                headline TEXT,
                published_at TEXT
            );
            CREATE TABLE news_symbols (
                news_id INTEGER,
                symbol TEXT,
                con_id INTEGER,
                first_seen_at TEXT
            );
            CREATE TABLE market_baselines (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                news_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                bid REAL,
                ask REAL,
                last REAL,
                midpoint REAL,
                selected_price REAL,
                selected_price_source TEXT,
                volume REAL,
                currency TEXT,
                con_id INTEGER,
                contract_symbol TEXT,
                sec_type TEXT,
                exchange TEXT,
                primary_exchange TEXT,
                multiplier TEXT,
                market_data_type INTEGER,
                quote_integrity_status TEXT,
                quote_integrity_reasons_json TEXT,
                reaction_baseline_eligible INTEGER,
                spread_ratio REAL,
                last_midpoint_deviation REAL,
                close_reference_deviation REAL,
                UNIQUE(news_id, symbol)
            );
            CREATE TABLE reaction_targets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                news_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                horizon TEXT NOT NULL,
                due_at TEXT NOT NULL,
                next_attempt_at TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                updated_at TEXT NOT NULL,
                usage_status TEXT NOT NULL DEFAULT 'provisional',
                usage_reason TEXT NOT NULL DEFAULT '',
                governance_analysis_id INTEGER,
                calibration_eligible INTEGER NOT NULL DEFAULT 0,
                observation_eligible INTEGER NOT NULL DEFAULT 0,
                UNIQUE(news_id, symbol, horizon)
            );
            CREATE TABLE market_reactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                target_id INTEGER NOT NULL UNIQUE,
                news_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                horizon TEXT NOT NULL,
                due_at TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                baseline_price REAL NOT NULL,
                reaction_price REAL NOT NULL,
                raw_return REAL NOT NULL,
                price_source TEXT NOT NULL,
                created_at TEXT NOT NULL,
                usage_status TEXT NOT NULL DEFAULT 'provisional',
                usage_reason TEXT NOT NULL DEFAULT '',
                governance_analysis_id INTEGER,
                calibration_eligible INTEGER NOT NULL DEFAULT 0,
                observation_eligible INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        con.execute(
            "INSERT INTO news_articles(id,provider,article_id,headline,published_at) VALUES (1,'BZ','a','headline','2026-01-02T14:30:00+00:00')"
        )
        con.execute("INSERT INTO news_symbols(news_id,symbol,first_seen_at) VALUES (1,'SPY','2026-01-02T14:30:00+00:00')")
        con.commit()
    finally:
        con.close()


def test_news_market_outcome_backfill_daily_next_day_observation_only(tmp_path: Path) -> None:
    db = tmp_path / "market_events.db"
    _make_db(db)

    report = build_news_market_outcomes(
        db,
        start="2026-01-01",
        end="2026-01-03",
        horizons=("NEXT_DAY",),
        price_source="yahoo",
        daily_price_source=FakeDailySource(),
    )

    assert report["completed_outcomes"] == 1
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT * FROM market_reactions").fetchone()
    target = con.execute("SELECT * FROM reaction_targets").fetchone()
    con.close()
    assert row["horizon"] == "NEXT_DAY"
    assert row["usage_status"] == "observation_only"
    assert row["observation_eligible"] == 1
    assert row["calibration_eligible"] == 0
    assert row["baseline_price"] == 100.0
    assert row["reaction_price"] == 110.0
    assert target["status"] == "complete"


def test_news_market_outcome_backfill_intraday_horizons(tmp_path: Path) -> None:
    db = tmp_path / "market_events.db"
    _make_db(db)

    report = build_news_market_outcomes(
        db,
        start="2026-01-01",
        end="2026-01-03",
        horizons=("5M", "15M", "1H", "CLOSE"),
        price_source="ibkr",
        intraday_price_source=FakeIntradaySource(),
    )

    assert report["completed_outcomes"] == 4
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    rows = {row["horizon"]: row for row in con.execute("SELECT * FROM market_reactions")}
    con.close()
    assert rows["5M"]["reaction_price"] == 101.0
    assert rows["15M"]["reaction_price"] == 102.0
    assert rows["1H"]["reaction_price"] == 103.0
    assert rows["CLOSE"]["reaction_price"] == 104.0


def test_news_market_outcome_backfill_only_missing_skips_existing_horizons(tmp_path: Path) -> None:
    db = tmp_path / "market_events.db"
    _make_db(db)

    first = build_news_market_outcomes(
        db,
        start="2026-01-01",
        end="2026-01-03",
        horizons=("5M",),
        price_source="ibkr",
        intraday_price_source=FakeIntradaySource(),
    )
    assert first["completed_outcomes"] == 1

    second = build_news_market_outcomes(
        db,
        start="2026-01-01",
        end="2026-01-03",
        horizons=("5M", "15M"),
        price_source="ibkr",
        intraday_price_source=FakeIntradaySource(),
        only_missing=True,
    )

    assert second["candidate_count"] == 1
    assert second["requested_outcome_count"] == 1
    assert second["completed_outcomes"] == 1
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    rows = {row["horizon"]: row for row in con.execute("SELECT * FROM market_reactions")}
    con.close()
    assert sorted(rows) == ["15M", "5M"]
    assert rows["5M"]["reaction_price"] == 101.0
    assert rows["15M"]["reaction_price"] == 102.0


def test_news_market_outcome_backfill_close_uses_next_real_session_close(tmp_path: Path) -> None:
    db = tmp_path / "market_events.db"
    _make_db(db)
    con = sqlite3.connect(db)
    try:
        con.execute("UPDATE news_articles SET published_at='2026-01-03T15:00:00+00:00' WHERE id=1")
        con.execute("UPDATE news_symbols SET first_seen_at='2026-01-03T15:00:00+00:00' WHERE news_id=1")
        con.commit()
    finally:
        con.close()

    report = build_news_market_outcomes(
        db,
        start="2026-01-01",
        end="2026-01-04",
        horizons=("CLOSE",),
        price_source="ibkr",
        intraday_price_source=FakeIntradayWeekendCloseSource(),
    )

    assert report["completed_outcomes"] == 1
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT * FROM market_reactions").fetchone()
    con.close()
    assert row["horizon"] == "CLOSE"
    assert row["baseline_price"] == 100.0
    assert row["reaction_price"] == 103.0
    assert row["captured_at"] == "2026-01-05T21:00:00+00:00"


def test_news_market_outcome_backfill_close_daily_fallback_for_closed_session(tmp_path: Path) -> None:
    db = tmp_path / "market_events.db"
    _make_db(db)
    con = sqlite3.connect(db)
    try:
        con.execute("UPDATE news_articles SET published_at='2026-01-03T15:00:00+00:00' WHERE id=1")
        con.execute("UPDATE news_symbols SET first_seen_at='2026-01-03T15:00:00+00:00' WHERE news_id=1")
        con.commit()
    finally:
        con.close()

    report = build_news_market_outcomes(
        db,
        start="2026-01-01",
        end="2026-01-04",
        horizons=("CLOSE",),
        price_source="ibkr",
        intraday_price_source=FakeEmptyIntradaySource(),
        daily_price_source=FakeDailySource(),
    )

    assert report["completed_outcomes"] == 1
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT * FROM market_reactions").fetchone()
    con.close()
    assert row["horizon"] == "CLOSE"
    assert row["baseline_price"] == 105.0
    assert row["reaction_price"] == 110.0
    assert row["price_source"] == "fake_daily_close_daily_fallback"


def test_news_market_outcome_backfill_skips_scheduled_closed_session_zero_bars(tmp_path: Path) -> None:
    db = tmp_path / "market_events.db"
    _make_db(db)
    con = sqlite3.connect(db)
    try:
        con.execute("UPDATE news_articles SET published_at='2026-06-19T14:30:00+00:00' WHERE id=1")
        con.execute("UPDATE news_symbols SET first_seen_at='2026-06-19T14:30:00+00:00' WHERE news_id=1")
        con.commit()
    finally:
        con.close()

    progress_path = tmp_path / "progress.jsonl"
    report = build_news_market_outcomes(
        db,
        start="2026-06-18",
        end="2026-06-20",
        horizons=("5M", "15M"),
        price_source="ibkr",
        intraday_price_source=FakeEmptyIntradaySource(),
        progress_jsonl_path=progress_path,
    )

    assert report["completed_outcomes"] == 0
    assert report["blocked_outcomes"] == 0
    assert report["skipped_closed_session_outcomes"] == 2
    assert report["status_counts"] == {"skipped_closed_session": 2}
    progress = progress_path.read_text(encoding="utf-8")
    assert '"zero_bar_classification": "scheduled_market_closure"' in progress
    assert '"closed_session_reason": "juneteenth"' in progress
    assert '"zero_bar_is_error": false' in progress

    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    targets = con.execute("SELECT horizon,status,next_attempt_at,last_error FROM reaction_targets ORDER BY horizon").fetchall()
    reactions = con.execute("SELECT * FROM market_reactions").fetchall()
    con.close()
    assert [row["horizon"] for row in targets] == ["15M", "5M"]
    assert {row["status"] for row in targets} == {"skipped_closed_session"}
    assert all(row["next_attempt_at"] is None for row in targets)
    assert all("scheduled_market_closure:juneteenth" in row["last_error"] for row in targets)
    assert reactions == []

    second = build_news_market_outcomes(
        db,
        start="2026-06-18",
        end="2026-06-20",
        horizons=("5M", "15M"),
        price_source="ibkr",
        intraday_price_source=FakeEmptyIntradaySource(),
        only_missing=True,
    )

    assert second["candidate_count"] == 0


def test_news_market_outcome_backfill_skips_off_hours_short_intraday_but_keeps_close(tmp_path: Path) -> None:
    db = tmp_path / "market_events.db"
    _make_db(db)
    con = sqlite3.connect(db)
    try:
        con.execute("UPDATE news_articles SET published_at='2026-01-02T05:00:00+00:00' WHERE id=1")
        con.execute("UPDATE news_symbols SET first_seen_at='2026-01-02T05:00:00+00:00' WHERE news_id=1")
        con.commit()
    finally:
        con.close()

    progress_path = tmp_path / "progress.jsonl"
    report = build_news_market_outcomes(
        db,
        start="2026-01-02",
        end="2026-01-03",
        horizons=("5M", "15M", "1H", "CLOSE"),
        price_source="ibkr",
        intraday_price_source=FakeEmptyIntradaySource(),
        daily_price_source=FakeDailySource(),
        progress_jsonl_path=progress_path,
    )

    assert report["completed_outcomes"] == 1
    assert report["blocked_outcomes"] == 0
    assert report["skipped_no_tradable_intraday_outcomes"] == 3
    assert report["status_counts"] == {"skipped_no_tradable_intraday_session": 3, "complete": 1}
    progress = progress_path.read_text(encoding="utf-8")
    assert '"zero_bar_classification": "no_tradable_intraday_session"' in progress
    assert '"no_tradable_intraday_reason": "event_before_extended_session_open"' in progress
    assert '"zero_bar_is_error": false' in progress

    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    targets = con.execute("SELECT horizon,status,next_attempt_at,last_error FROM reaction_targets ORDER BY horizon").fetchall()
    reactions = con.execute("SELECT horizon,price_source FROM market_reactions ORDER BY horizon").fetchall()
    con.close()
    assert [row["horizon"] for row in targets] == ["15M", "1H", "5M", "CLOSE"]
    short_targets = [row for row in targets if row["horizon"] != "CLOSE"]
    assert {row["status"] for row in short_targets} == {"skipped_no_tradable_intraday_session"}
    assert all(row["next_attempt_at"] is None for row in short_targets)
    assert all("no_tradable_intraday_session:event_before_extended_session_open" in row["last_error"] for row in short_targets)
    assert [(row["horizon"], row["price_source"]) for row in reactions] == [("CLOSE", "fake_daily_close_daily_fallback")]

    second = build_news_market_outcomes(
        db,
        start="2026-01-02",
        end="2026-01-03",
        horizons=("5M", "15M", "1H"),
        price_source="ibkr",
        intraday_price_source=FakeEmptyIntradaySource(),
        only_missing=True,
    )

    assert second["candidate_count"] == 0


def test_news_market_outcome_backfill_bypasses_repeated_close_retry_failures(tmp_path: Path) -> None:
    db = tmp_path / "market_events.db"
    _make_db(db)
    con = sqlite3.connect(db)
    try:
        con.execute("UPDATE news_articles SET published_at='2026-01-02T14:30:00+00:00' WHERE id=1")
        con.execute(
            "INSERT INTO news_articles(id,provider,article_id,headline,published_at) VALUES (2,'BZ','b','headline 2','2026-01-05T14:30:00+00:00')"
        )
        con.execute(
            "INSERT INTO news_articles(id,provider,article_id,headline,published_at) VALUES (3,'BZ','c','headline 3','2026-01-06T14:30:00+00:00')"
        )
        con.execute("INSERT INTO news_symbols(news_id,symbol,first_seen_at) VALUES (2,'SPY','2026-01-05T14:30:00+00:00')")
        con.execute("INSERT INTO news_symbols(news_id,symbol,first_seen_at) VALUES (3,'SPY','2026-01-06T14:30:00+00:00')")
        con.commit()
    finally:
        con.close()

    intraday_source = FakeCloseRetryFailingIntradaySource()
    progress_path = tmp_path / "progress.jsonl"
    report = build_news_market_outcomes(
        db,
        start="2026-01-01",
        end="2026-01-07",
        horizons=("CLOSE",),
        price_source="ibkr",
        intraday_price_source=intraday_source,
        daily_price_source=FakeWideDailySource(),
        close_retry_bypass_after_failures=2,
        progress_jsonl_path=progress_path,
    )

    assert report["completed_outcomes"] == 3
    assert report["close_retry_bypassed_count"] == 1
    assert intraday_source.normal_calls == 3
    assert intraday_source.wide_retry_calls == 2
    progress = progress_path.read_text(encoding="utf-8")
    assert "intraday_group_close_retry_bypassed" in progress
