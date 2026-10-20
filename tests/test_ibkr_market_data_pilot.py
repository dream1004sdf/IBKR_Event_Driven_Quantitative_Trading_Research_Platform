from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from recorder.ibkr_market_data_pilot import (
    MacroEvent,
    audit_cache,
    build_event_windows,
    build_shards,
    cache_snapshot,
    connect_cache,
    ensure_cache_schema,
    persist_daily_event_anchors,
    persist_market_data_provenance,
    round_robin_shards,
    split_intraday_range,
)


def _config(cache_path: Path) -> dict:
    return {
        "paths": {"cache_database": str(cache_path)},
        "daily": {"buffer_days_before": 7, "buffer_days_after": 10, "what_to_show": "TRADES", "use_rth": True},
        "intraday": {"bar_size": "5 mins", "minutes_before": 60, "hours_after": 36, "what_to_show": "TRADES", "use_rth": False},
        "universe": {
            "base_symbols": ["SPY"],
            "source_symbols": {"federal_reserve": ["DIA"], "ecb": ["FXE"], "boj": ["EWJ"]},
        },
    }


def test_event_windows_coalesce_overlapping_cross_bank_shards(tmp_path: Path) -> None:
    config = _config(tmp_path / "cache.db")
    events = [
        MacroEvent(1, 101, "federal_reserve", "central_bank_decision", "FOMC", "US", datetime(2026, 3, 18, 18, tzinfo=UTC)),
        MacroEvent(2, 102, "ecb", "central_bank_decision", "ECB", "EU", datetime(2026, 3, 19, 13, 15, tzinfo=UTC)),
        MacroEvent(3, 103, "boj", "central_bank_minutes", "BOJ", "JP", datetime(2026, 3, 24, 23, 50, tzinfo=UTC)),
    ]
    start = datetime(2026, 3, 1, tzinfo=UTC)
    end = datetime(2026, 4, 1, tzinfo=UTC)

    windows = build_event_windows(events, config)
    shards, assignments = build_shards(windows, config, start, end)

    assert len(windows) == 6
    assert len(assignments) == 6
    assert sum(1 for shard in shards if shard.data_kind == "intraday") == 5
    assert sum(1 for shard in shards if shard.data_kind == "daily") == 4


def test_cache_audit_passes_complete_unique_ohlcv_fixture(tmp_path: Path) -> None:
    cache_path = tmp_path / "cache.db"
    config = _config(cache_path)
    with connect_cache(cache_path) as conn:
        ensure_cache_schema(conn)
        conn.execute(
            "INSERT INTO ibkr_contracts VALUES(?,?,?,?,?,?,?,?,?)",
            ("SPY", 756733, "STK", "SMART", "ARCA", "USD", "SPY", "SPY", "2026-07-10T00:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO ibkr_bar_shards(shard_key,data_kind,symbol,start_at_utc,end_at_utc,bar_size,what_to_show,use_rth,status,con_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("intraday", "intraday", "SPY", "2026-03-18T17:00:00+00:00", "2026-03-20T06:00:00+00:00", "5 mins", "TRADES", 0, "completed", 756733),
        )
        conn.execute(
            "INSERT INTO ibkr_bar_shards(shard_key,data_kind,symbol,start_at_utc,end_at_utc,bar_size,what_to_show,use_rth,status,con_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("daily", "daily", "SPY", "2026-02-22T00:00:00+00:00", "2026-04-11T00:00:00+00:00", "1 day", "TRADES", 1, "completed", 756733),
        )
        conn.execute(
            "INSERT INTO ibkr_event_windows VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("window", 1, 101, "federal_reserve", "central_bank_decision", "FOMC", "SPY", "2026-03-18T18:00:00+00:00", "2026-03-18T17:00:00+00:00", "2026-03-20T06:00:00+00:00", "intraday"),
        )
        bars = [
            ("intraday-bar", "intraday", "5 mins", 0, "2026-03-18T18:05:00+00:00", "2026-03-18"),
            ("daily-bar", "daily", "1 day", 1, "2026-03-18T00:00:00+00:00", "2026-03-18"),
        ]
        for key, kind, size, rth, bar_time, session_date in bars:
            conn.execute(
                """
                INSERT INTO ibkr_historical_bars(
                    bar_key,source,con_id,symbol,data_kind,bar_size,what_to_show,use_rth,
                    bar_time_utc,session_date,open,high,low,close,volume,wap,trade_count,
                    first_seen_at_utc,last_seen_at_utc
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (key, "ibkr", 756733, "SPY", kind, size, "TRADES", rth, bar_time, session_date, 100.0, 102.0, 99.0, 101.0, 1000.0, 100.5, 20, "2026-07-10T00:00:00+00:00", "2026-07-10T00:00:00+00:00"),
            )
        conn.commit()

    report = audit_cache(
        config,
        idempotency_diff={
            "ibkr_contracts": 0,
            "ibkr_bar_shards": 0,
            "ibkr_event_windows": 0,
            "ibkr_daily_event_anchors": 0,
            "ibkr_historical_bars": 0,
            "ibkr_market_data_provenance": 0,
        },
    )

    assert report["verdict"] == "PASS"
    assert report["duplicate_bar_identity_groups"] == 0
    assert report["missing_event_window_coverage"] == 0
    assert report["invalid_ohlc_rows"] == 0


def test_round_robin_shards_interleaves_symbols() -> None:
    from recorder.ibkr_market_data_pilot import BarShard

    start = datetime(2026, 3, 1, tzinfo=UTC)
    shards = [
        BarShard(f"{symbol}-{index}", "intraday", symbol, start, start, "5 mins", "TRADES", False)
        for symbol in ("SPY", "QQQ", "TLT")
        for index in range(3)
    ]

    ordered = round_robin_shards(shards)

    assert [shard.symbol for shard in ordered[:6]] == ["QQQ", "SPY", "TLT", "QQQ", "SPY", "TLT"]


def test_daily_event_anchors_include_date_only_events_and_are_idempotent(tmp_path: Path) -> None:
    cache_path = tmp_path / "cache.db"
    config = _config(cache_path)
    events = [
        MacroEvent(
            1,
            101,
            "federal_reserve",
            "central_bank_decision",
            "FOMC",
            "US",
            datetime(2026, 3, 18, 18, tzinfo=UTC),
        ),
        MacroEvent(
            2,
            102,
            "boj",
            "central_bank_decision",
            "BOJ",
            "JP",
            datetime(2026, 3, 19, tzinfo=UTC),
            "official_date_only",
            False,
        ),
    ]

    with connect_cache(cache_path) as conn:
        ensure_cache_schema(conn)
        assert persist_daily_event_anchors(conn, events, config) == 4
        first = cache_snapshot(conn)
        assert persist_daily_event_anchors(conn, events, config) == 4
        second = cache_snapshot(conn)
        date_only = conn.execute(
            "SELECT COUNT(1) FROM ibkr_daily_event_anchors WHERE intraday_label_eligible=0"
        ).fetchone()[0]

    assert first == second
    assert first["ibkr_daily_event_anchors"] == 4
    assert date_only == 2


def test_split_intraday_range_preserves_full_window_without_gaps() -> None:
    start = datetime(2025, 7, 9, 17, tzinfo=UTC)
    end = datetime(2025, 7, 11, 6, tzinfo=UTC)

    chunks = split_intraday_range(start, end, 12)

    assert chunks[0][0] == start
    assert chunks[-1][1] == end
    assert all(left[1] == right[0] for left, right in zip(chunks, chunks[1:]))
    assert all((chunk_end - chunk_start).total_seconds() <= 12 * 3600 for chunk_start, chunk_end in chunks)


def test_market_data_provenance_is_persisted_idempotently(tmp_path: Path) -> None:
    cache_path = tmp_path / "cache.db"
    with connect_cache(cache_path) as conn:
        ensure_cache_schema(conn)
        conn.execute(
            "INSERT INTO ibkr_contracts VALUES(?,?,?,?,?,?,?,?,?)",
            ("SPY", 756733, "STK", "SMART", "ARCA", "USD", "SPY", "SPY", "2026-07-11T00:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO ibkr_bar_shards(shard_key,data_kind,symbol,start_at_utc,end_at_utc,bar_size,what_to_show,use_rth,status,con_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("daily", "daily", "SPY", "2025-01-01T00:00:00+00:00", "2026-07-07T00:00:00+00:00", "1 day", "TRADES", 1, "completed", 756733),
        )
        conn.execute(
            "INSERT INTO ibkr_bar_shards(shard_key,data_kind,symbol,start_at_utc,end_at_utc,bar_size,what_to_show,use_rth,status,con_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("intraday", "intraday", "SPY", "2025-01-01T00:00:00+00:00", "2025-01-02T00:00:00+00:00", "1 min", "TRADES", 0, "completed", 756733),
        )
        assert persist_market_data_provenance(conn) == 2
        first = cache_snapshot(conn)
        assert persist_market_data_provenance(conn) == 2
        second = cache_snapshot(conn)
        rows = [dict(row) for row in conn.execute("SELECT * FROM ibkr_market_data_provenance ORDER BY data_kind")]

    assert first == second
    assert len(rows) == 2
    assert {row["adjustment_mode"] for row in rows} == {"split_adjusted_dividend_unadjusted"}
    assert {row["bar_timezone"] for row in rows} == {"UTC"}
