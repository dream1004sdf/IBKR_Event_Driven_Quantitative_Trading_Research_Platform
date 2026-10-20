from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from .market_outcomes import IBKRHistoricalDailyPriceSource, IBKRHistoricalIntradayPriceSource


PILOT_VERSION = "ibkr-market-data-pilot-v1"


def _hash(*parts: Any) -> str:
    payload = "|".join(str(part) for part in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


@dataclass(frozen=True)
class MacroEvent:
    macro_event_id: int
    event_cluster_id: int
    source_system: str
    event_type: str
    release_name: str
    country: str
    model_visible_at_utc: datetime
    release_time_quality: str = "official_exact"
    intraday_label_eligible: bool = True


@dataclass(frozen=True)
class EventWindow:
    window_key: str
    macro_event_id: int
    event_cluster_id: int
    source_system: str
    event_type: str
    release_name: str
    symbol: str
    anchor_at_utc: datetime
    start_at_utc: datetime
    end_at_utc: datetime


@dataclass(frozen=True)
class BarShard:
    shard_key: str
    data_kind: str
    symbol: str
    start_at_utc: datetime
    end_at_utc: datetime
    bar_size: str
    what_to_show: str
    use_rth: bool


def connect_cache(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=60000")
    return conn


def ensure_cache_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS ibkr_pilot_runs (
            run_key TEXT PRIMARY KEY,
            pilot_version TEXT NOT NULL,
            config_hash TEXT NOT NULL,
            source_database TEXT NOT NULL,
            window_start TEXT NOT NULL,
            window_end TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at_utc TEXT NOT NULL,
            completed_at_utc TEXT,
            error_text TEXT
        );
        CREATE TABLE IF NOT EXISTS ibkr_contracts (
            symbol TEXT PRIMARY KEY,
            con_id INTEGER NOT NULL,
            sec_type TEXT NOT NULL,
            exchange TEXT NOT NULL,
            primary_exchange TEXT,
            currency TEXT NOT NULL,
            local_symbol TEXT,
            trading_class TEXT,
            resolved_at_utc TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ibkr_bar_shards (
            shard_key TEXT PRIMARY KEY,
            data_kind TEXT NOT NULL,
            symbol TEXT NOT NULL,
            start_at_utc TEXT NOT NULL,
            end_at_utc TEXT NOT NULL,
            bar_size TEXT NOT NULL,
            what_to_show TEXT NOT NULL,
            use_rth INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempt_count INTEGER NOT NULL DEFAULT 0,
            provider_row_count INTEGER NOT NULL DEFAULT 0,
            inserted_row_count INTEGER NOT NULL DEFAULT 0,
            con_id INTEGER,
            last_error TEXT,
            started_at_utc TEXT,
            completed_at_utc TEXT
        );
        CREATE TABLE IF NOT EXISTS ibkr_event_windows (
            window_key TEXT PRIMARY KEY,
            macro_event_id INTEGER NOT NULL,
            event_cluster_id INTEGER NOT NULL,
            source_system TEXT NOT NULL,
            event_type TEXT NOT NULL,
            release_name TEXT NOT NULL,
            symbol TEXT NOT NULL,
            anchor_at_utc TEXT NOT NULL,
            start_at_utc TEXT NOT NULL,
            end_at_utc TEXT NOT NULL,
            shard_key TEXT NOT NULL,
            FOREIGN KEY(shard_key) REFERENCES ibkr_bar_shards(shard_key)
        );
        CREATE TABLE IF NOT EXISTS ibkr_daily_event_anchors (
            anchor_key TEXT PRIMARY KEY,
            macro_event_id INTEGER NOT NULL,
            event_cluster_id INTEGER NOT NULL,
            source_system TEXT NOT NULL,
            event_type TEXT NOT NULL,
            release_name TEXT NOT NULL,
            release_time_quality TEXT NOT NULL,
            intraday_label_eligible INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            anchor_at_utc TEXT NOT NULL,
            anchor_date TEXT NOT NULL,
            horizon_1d_eligible INTEGER NOT NULL DEFAULT 1,
            horizon_5d_eligible INTEGER NOT NULL DEFAULT 1,
            UNIQUE(macro_event_id,symbol)
        );
        CREATE TABLE IF NOT EXISTS ibkr_historical_bars (
            bar_key TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            con_id INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            data_kind TEXT NOT NULL,
            bar_size TEXT NOT NULL,
            what_to_show TEXT NOT NULL,
            use_rth INTEGER NOT NULL,
            bar_time_utc TEXT NOT NULL,
            session_date TEXT NOT NULL,
            open REAL,
            high REAL,
            low REAL,
            close REAL NOT NULL,
            volume REAL,
            wap REAL,
            trade_count INTEGER,
            first_seen_at_utc TEXT NOT NULL,
            last_seen_at_utc TEXT NOT NULL,
            UNIQUE(con_id,bar_size,what_to_show,use_rth,bar_time_utc)
        );
        CREATE TABLE IF NOT EXISTS ibkr_market_data_provenance (
            provenance_key TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            market_data_type TEXT NOT NULL,
            symbol TEXT NOT NULL,
            con_id INTEGER NOT NULL,
            data_kind TEXT NOT NULL,
            bar_size TEXT NOT NULL,
            what_to_show TEXT NOT NULL,
            use_rth INTEGER NOT NULL,
            source_exchange TEXT NOT NULL,
            source_primary_exchange TEXT NOT NULL,
            currency TEXT NOT NULL,
            request_timezone TEXT NOT NULL,
            bar_timezone TEXT NOT NULL,
            adjustment_mode TEXT NOT NULL,
            data_source_version TEXT NOT NULL,
            first_seen_at_utc TEXT NOT NULL,
            last_seen_at_utc TEXT NOT NULL,
            UNIQUE(symbol,data_kind,bar_size,what_to_show,use_rth)
        );
        CREATE INDEX IF NOT EXISTS idx_ibkr_bars_symbol_time
            ON ibkr_historical_bars(symbol,bar_time_utc);
        CREATE INDEX IF NOT EXISTS idx_ibkr_windows_event
            ON ibkr_event_windows(macro_event_id,symbol);
        CREATE INDEX IF NOT EXISTS idx_ibkr_daily_anchors_event
            ON ibkr_daily_event_anchors(macro_event_id,symbol);
        """
    )
    conn.commit()


def load_macro_events(source_database: Path, start: datetime, end: datetime) -> list[MacroEvent]:
    return _load_macro_events_from_view(
        source_database,
        start,
        end,
        "v_canonical_intraday_reaction_trigger_macro_events",
    )


def load_all_reaction_macro_events(source_database: Path, start: datetime, end: datetime) -> list[MacroEvent]:
    return _load_macro_events_from_view(
        source_database,
        start,
        end,
        "v_canonical_reaction_trigger_macro_events",
    )


def _load_macro_events_from_view(source_database: Path, start: datetime, end: datetime, view_name: str) -> list[MacroEvent]:
    if view_name not in {
        "v_canonical_intraday_reaction_trigger_macro_events",
        "v_canonical_reaction_trigger_macro_events",
    }:
        raise ValueError(f"Unsupported canonical macro view: {view_name}")
    uri = f"file:{source_database.resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            f"""
            SELECT macro_event_id,event_cluster_id,source_system,event_type,
                   release_name,country,model_visible_at_utc,
                   release_time_quality,intraday_label_eligible
              FROM {view_name}
             WHERE model_visible_at_utc >= ? AND model_visible_at_utc < ?
             ORDER BY model_visible_at_utc,macro_event_id
            """,
            (_iso(start), _iso(end)),
        ).fetchall()
    finally:
        conn.close()
    return [
        MacroEvent(
            macro_event_id=int(row["macro_event_id"]),
            event_cluster_id=int(row["event_cluster_id"]),
            source_system=str(row["source_system"]),
            event_type=str(row["event_type"]),
            release_name=str(row["release_name"]),
            country=str(row["country"] or ""),
            model_visible_at_utc=_parse_utc(str(row["model_visible_at_utc"])),
            release_time_quality=str(row["release_time_quality"] or ""),
            intraday_label_eligible=bool(row["intraday_label_eligible"]),
        )
        for row in rows
    ]


def _symbols_for_event(event: MacroEvent, config: dict[str, Any]) -> list[str]:
    universe = config["universe"]
    base = [str(symbol).upper() for symbol in universe.get("base_symbols") or []]
    source_symbols = {
        str(source): [str(symbol).upper() for symbol in symbols]
        for source, symbols in (universe.get("source_symbols") or {}).items()
    }
    return list(dict.fromkeys(base + source_symbols.get(event.source_system, [])))


def build_event_windows(events: list[MacroEvent], config: dict[str, Any]) -> list[EventWindow]:
    before = timedelta(minutes=int(config["intraday"]["minutes_before"]))
    after = timedelta(hours=float(config["intraday"]["hours_after"]))
    windows: list[EventWindow] = []
    for event in events:
        symbols = _symbols_for_event(event, config)
        for symbol in symbols:
            start = event.model_visible_at_utc - before
            end = event.model_visible_at_utc + after
            key = _hash("event-window", event.macro_event_id, symbol, _iso(start), _iso(end))
            windows.append(
                EventWindow(
                    window_key=key,
                    macro_event_id=event.macro_event_id,
                    event_cluster_id=event.event_cluster_id,
                    source_system=event.source_system,
                    event_type=event.event_type,
                    release_name=event.release_name,
                    symbol=symbol,
                    anchor_at_utc=event.model_visible_at_utc,
                    start_at_utc=start,
                    end_at_utc=end,
                )
            )
    return windows


def build_shards(windows: list[EventWindow], config: dict[str, Any], start: datetime, end: datetime) -> tuple[list[BarShard], dict[str, str]]:
    intraday = config["intraday"]
    grouped: dict[str, list[EventWindow]] = {}
    for window in windows:
        grouped.setdefault(window.symbol, []).append(window)
    shards: list[BarShard] = []
    assignments: dict[str, str] = {}
    for symbol, symbol_windows in grouped.items():
        ordered = sorted(symbol_windows, key=lambda row: (row.start_at_utc, row.end_at_utc))
        buckets: list[tuple[datetime, datetime, list[EventWindow]]] = []
        for window in ordered:
            if buckets and window.start_at_utc <= buckets[-1][1]:
                bucket_start, bucket_end, bucket_windows = buckets[-1]
                buckets[-1] = (bucket_start, max(bucket_end, window.end_at_utc), bucket_windows + [window])
            else:
                buckets.append((window.start_at_utc, window.end_at_utc, [window]))
        for shard_start, shard_end, shard_windows in buckets:
            key = _hash("intraday", symbol, _iso(shard_start), _iso(shard_end), intraday["bar_size"], intraday["what_to_show"], intraday["use_rth"])
            shards.append(
                BarShard(key, "intraday", symbol, shard_start, shard_end, str(intraday["bar_size"]), str(intraday["what_to_show"]), bool(intraday["use_rth"]))
            )
            for window in shard_windows:
                assignments[window.window_key] = key
    daily = config["daily"]
    symbols = sorted({window.symbol for window in windows})
    daily_start = start - timedelta(days=int(daily["buffer_days_before"]))
    daily_end = end + timedelta(days=int(daily["buffer_days_after"]))
    for symbol in symbols:
        key = _hash("daily", symbol, daily_start.date(), daily_end.date(), daily["what_to_show"], daily["use_rth"])
        shards.append(
            BarShard(key, "daily", symbol, daily_start, daily_end, "1 day", str(daily["what_to_show"]), bool(daily["use_rth"]))
        )
    return shards, assignments


def round_robin_shards(shards: list[BarShard]) -> list[BarShard]:
    by_symbol: dict[str, list[BarShard]] = {}
    for shard in sorted(shards, key=lambda item: (item.symbol, item.start_at_utc, item.end_at_utc)):
        by_symbol.setdefault(shard.symbol, []).append(shard)
    ordered: list[BarShard] = []
    symbols = sorted(by_symbol)
    while any(by_symbol[symbol] for symbol in symbols):
        for symbol in symbols:
            if by_symbol[symbol]:
                ordered.append(by_symbol[symbol].pop(0))
    return ordered


def split_intraday_range(start: datetime, end: datetime, chunk_hours: float) -> list[tuple[datetime, datetime]]:
    if end <= start:
        raise ValueError("end must be after start")
    if chunk_hours <= 0:
        raise ValueError("chunk_hours must be positive")
    step = timedelta(hours=float(chunk_hours))
    chunks: list[tuple[datetime, datetime]] = []
    cursor = start
    while cursor < end:
        chunk_end = min(cursor + step, end)
        chunks.append((cursor, chunk_end))
        cursor = chunk_end
    return chunks


def persist_plan(conn: sqlite3.Connection, windows: list[EventWindow], shards: list[BarShard], assignments: dict[str, str]) -> None:
    for shard in shards:
        conn.execute(
            """
            INSERT OR IGNORE INTO ibkr_bar_shards(
                shard_key,data_kind,symbol,start_at_utc,end_at_utc,bar_size,what_to_show,use_rth,status
            ) VALUES(?,?,?,?,?,?,?,?, 'pending')
            """,
            (shard.shard_key, shard.data_kind, shard.symbol, _iso(shard.start_at_utc), _iso(shard.end_at_utc), shard.bar_size, shard.what_to_show, int(shard.use_rth)),
        )
    for window in windows:
        conn.execute(
            """
            INSERT OR REPLACE INTO ibkr_event_windows(
                window_key,macro_event_id,event_cluster_id,source_system,event_type,release_name,
                symbol,anchor_at_utc,start_at_utc,end_at_utc,shard_key
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                window.window_key,
                window.macro_event_id,
                window.event_cluster_id,
                window.source_system,
                window.event_type,
                window.release_name,
                window.symbol,
                _iso(window.anchor_at_utc),
                _iso(window.start_at_utc),
                _iso(window.end_at_utc),
                assignments[window.window_key],
            ),
        )
    conn.commit()


def persist_daily_event_anchors(conn: sqlite3.Connection, events: list[MacroEvent], config: dict[str, Any]) -> int:
    touched = 0
    for event in events:
        for symbol in _symbols_for_event(event, config):
            key = _hash("daily-event-anchor", event.macro_event_id, symbol)
            conn.execute(
                """
                INSERT INTO ibkr_daily_event_anchors(
                    anchor_key,macro_event_id,event_cluster_id,source_system,event_type,release_name,
                    release_time_quality,intraday_label_eligible,symbol,anchor_at_utc,anchor_date,
                    horizon_1d_eligible,horizon_5d_eligible
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(anchor_key) DO UPDATE SET
                    event_cluster_id=excluded.event_cluster_id,source_system=excluded.source_system,
                    event_type=excluded.event_type,release_name=excluded.release_name,
                    release_time_quality=excluded.release_time_quality,
                    intraday_label_eligible=excluded.intraday_label_eligible,
                    anchor_at_utc=excluded.anchor_at_utc,anchor_date=excluded.anchor_date
                """,
                (
                    key,
                    event.macro_event_id,
                    event.event_cluster_id,
                    event.source_system,
                    event.event_type,
                    event.release_name,
                    event.release_time_quality,
                    int(event.intraday_label_eligible),
                    symbol,
                    _iso(event.model_visible_at_utc),
                    event.model_visible_at_utc.date().isoformat(),
                    1,
                    1,
                ),
            )
            touched += 1
    conn.commit()
    return touched


def _persist_contract(conn: sqlite3.Connection, symbol: str, contract: Any) -> int:
    now = datetime.now(UTC).isoformat()
    con_id = int(getattr(contract, "conId", 0) or 0)
    if con_id <= 0:
        raise RuntimeError(f"IBKR resolved {symbol} without a positive conId")
    conn.execute(
        """
        INSERT INTO ibkr_contracts(symbol,con_id,sec_type,exchange,primary_exchange,currency,local_symbol,trading_class,resolved_at_utc)
        VALUES(?,?,?,?,?,?,?,?,?)
        ON CONFLICT(symbol) DO UPDATE SET
            con_id=excluded.con_id,sec_type=excluded.sec_type,exchange=excluded.exchange,
            primary_exchange=excluded.primary_exchange,currency=excluded.currency,
            local_symbol=excluded.local_symbol,trading_class=excluded.trading_class,
            resolved_at_utc=excluded.resolved_at_utc
        """,
        (
            symbol,
            con_id,
            str(getattr(contract, "secType", "") or ""),
            str(getattr(contract, "exchange", "") or ""),
            str(getattr(contract, "primaryExchange", "") or ""),
            str(getattr(contract, "currency", "") or ""),
            str(getattr(contract, "localSymbol", "") or ""),
            str(getattr(contract, "tradingClass", "") or ""),
            now,
        ),
    )
    conn.commit()
    return con_id


def _persist_bars(conn: sqlite3.Connection, shard: BarShard, con_id: int, rows: list[Any]) -> int:
    now = datetime.now(UTC).isoformat()
    inserted = 0
    for row in rows:
        if shard.data_kind == "daily":
            bar_time = f"{row.date.isoformat()}T00:00:00+00:00"
            session_date = row.date.isoformat()
        else:
            bar_time = _iso(row.timestamp)
            session_date = row.timestamp.astimezone(UTC).date().isoformat()
        bar_key = _hash("ibkr", con_id, shard.bar_size, shard.what_to_show, int(shard.use_rth), bar_time)
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO ibkr_historical_bars(
                bar_key,source,con_id,symbol,data_kind,bar_size,what_to_show,use_rth,
                bar_time_utc,session_date,open,high,low,close,volume,wap,trade_count,
                first_seen_at_utc,last_seen_at_utc
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                bar_key,
                "ibkr",
                con_id,
                shard.symbol,
                shard.data_kind,
                shard.bar_size,
                shard.what_to_show,
                int(shard.use_rth),
                bar_time,
                session_date,
                getattr(row, "open", None),
                getattr(row, "high", None),
                getattr(row, "low", None),
                float(row.close),
                getattr(row, "volume", None),
                getattr(row, "wap", None),
                getattr(row, "trade_count", None),
                now,
                now,
            ),
        )
        inserted += int(cursor.rowcount > 0)
        conn.execute("UPDATE ibkr_historical_bars SET last_seen_at_utc=? WHERE bar_key=?", (now, bar_key))
    conn.commit()
    return inserted


def persist_market_data_provenance(conn: sqlite3.Connection) -> int:
    now = datetime.now(UTC).isoformat()
    touched = 0
    rows = conn.execute(
        """
        SELECT DISTINCT s.symbol,s.con_id,s.data_kind,s.bar_size,s.what_to_show,s.use_rth,
               c.exchange,c.primary_exchange,c.currency
          FROM ibkr_bar_shards s
          JOIN ibkr_contracts c ON c.symbol=s.symbol AND c.con_id=s.con_id
         WHERE s.con_id IS NOT NULL
        """
    ).fetchall()
    for row in rows:
        key = _hash(
            "ibkr-market-data-provenance",
            row["symbol"],
            row["data_kind"],
            row["bar_size"],
            row["what_to_show"],
            int(row["use_rth"]),
        )
        conn.execute(
            """
            INSERT INTO ibkr_market_data_provenance(
                provenance_key,source,market_data_type,symbol,con_id,data_kind,bar_size,
                what_to_show,use_rth,source_exchange,source_primary_exchange,currency,
                request_timezone,bar_timezone,adjustment_mode,data_source_version,
                first_seen_at_utc,last_seen_at_utc
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(provenance_key) DO UPDATE SET
                con_id=excluded.con_id,source_exchange=excluded.source_exchange,
                source_primary_exchange=excluded.source_primary_exchange,currency=excluded.currency,
                adjustment_mode=excluded.adjustment_mode,data_source_version=excluded.data_source_version,
                last_seen_at_utc=excluded.last_seen_at_utc
            """,
            (
                key,
                "ibkr",
                "historical",
                row["symbol"],
                int(row["con_id"]),
                row["data_kind"],
                row["bar_size"],
                row["what_to_show"],
                int(row["use_rth"]),
                str(row["exchange"] or ""),
                str(row["primary_exchange"] or ""),
                str(row["currency"] or ""),
                "UTC",
                "UTC",
                "split_adjusted_dividend_unadjusted",
                PILOT_VERSION,
                now,
                now,
            ),
        )
        touched += 1
    conn.commit()
    return touched


def cache_snapshot(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in (
            "ibkr_contracts",
            "ibkr_bar_shards",
            "ibkr_event_windows",
            "ibkr_daily_event_anchors",
            "ibkr_historical_bars",
            "ibkr_market_data_provenance",
        )
    }


def run_collection(config: dict[str, Any], *, resume: bool = True) -> dict[str, Any]:
    source_db = Path(config["paths"]["source_database"]).expanduser().resolve()
    cache_db = Path(config["paths"]["cache_database"]).expanduser().resolve()
    start = _parse_utc(config["window"]["start_utc"])
    end = _parse_utc(config["window"]["end_utc"])
    config_hash = _hash(json.dumps(config, sort_keys=True, separators=(",", ":")))
    run_key = _hash(PILOT_VERSION, config_hash, _iso(start), _iso(end))
    events = load_macro_events(source_db, start, end)
    daily_events = load_all_reaction_macro_events(source_db, start, end)
    if not events:
        raise RuntimeError("No canonical exact-time macro events were found for the pilot window")
    windows = build_event_windows(events, config)
    shards, assignments = build_shards(windows, config, start, end)
    gateway = config["gateway"]
    runtime = config["runtime"]
    result: dict[str, Any] = {
        "pilot_version": PILOT_VERSION,
        "run_key": run_key,
        "config_hash": config_hash,
        "event_count": len(events),
        "daily_event_count": len(daily_events),
        "event_window_count": len(windows),
        "planned_shard_count": len(shards),
        "requested_shards": 0,
        "skipped_completed_shards": 0,
        "completed_shards": 0,
        "failed_shards": 0,
        "errors": [],
    }
    with connect_cache(cache_db) as conn:
        ensure_cache_schema(conn)
        persist_plan(conn, windows, shards, assignments)
        result["daily_event_anchor_count"] = persist_daily_event_anchors(conn, daily_events, config)
        conn.execute(
            """
            INSERT INTO ibkr_pilot_runs(run_key,pilot_version,config_hash,source_database,window_start,window_end,status,started_at_utc)
            VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(run_key) DO UPDATE SET status=excluded.status,started_at_utc=excluded.started_at_utc,error_text=NULL
            """,
            (run_key, PILOT_VERSION, config_hash, str(source_db), _iso(start), _iso(end), "running", datetime.now(UTC).isoformat()),
        )
        conn.commit()
        for data_kind in ("daily", "intraday"):
            kind_shards = [shard for shard in shards if shard.data_kind == data_kind]
            if data_kind == "intraday":
                kind_shards = round_robin_shards(kind_shards)
            if data_kind == "daily":
                source: Any = IBKRHistoricalDailyPriceSource(
                    host=str(gateway["host"]),
                    port=int(gateway["port"]),
                    client_id=int(gateway["daily_client_id"]),
                    timeout_seconds=float(runtime["timeout_seconds"]),
                    what_to_show=str(config["daily"]["what_to_show"]),
                    use_rth=bool(config["daily"]["use_rth"]),
                )
            else:
                source = IBKRHistoricalIntradayPriceSource(
                    host=str(gateway["host"]),
                    port=int(gateway["port"]),
                    client_id=int(gateway["intraday_client_id"]),
                    timeout_seconds=float(runtime["timeout_seconds"]),
                    what_to_show=str(config["intraday"]["what_to_show"]),
                    bar_size=str(config["intraday"]["bar_size"]),
                    use_rth=bool(config["intraday"]["use_rth"]),
                )
            try:
                for shard in kind_shards:
                    stored = conn.execute("SELECT status FROM ibkr_bar_shards WHERE shard_key=?", (shard.shard_key,)).fetchone()
                    if resume and stored and stored["status"] == "completed":
                        result["skipped_completed_shards"] += 1
                        continue
                    result["requested_shards"] += 1
                    max_attempts = max(1, int(runtime["max_attempts"]))
                    last_error = ""
                    for attempt in range(1, max_attempts + 1):
                        started_at = datetime.now(UTC).isoformat()
                        conn.execute(
                            "UPDATE ibkr_bar_shards SET status='running',attempt_count=attempt_count+1,started_at_utc=?,last_error=NULL WHERE shard_key=?",
                            (started_at, shard.shard_key),
                        )
                        conn.commit()
                        try:
                            contract = source.resolve_stock_contract(shard.symbol)
                            con_id = _persist_contract(conn, shard.symbol, contract)
                            if data_kind == "daily":
                                rows = source.get_daily_bars(shard.symbol, shard.start_at_utc.date(), shard.end_at_utc.date())
                            else:
                                rows = source.get_intraday_bars(shard.symbol, shard.start_at_utc, shard.end_at_utc)
                            inserted = _persist_bars(conn, shard, con_id, rows)
                            status = "completed" if rows else "no_data"
                            conn.execute(
                                """
                                UPDATE ibkr_bar_shards
                                   SET status=?,provider_row_count=?,inserted_row_count=inserted_row_count+?,
                                       con_id=?,last_error=NULL,completed_at_utc=?
                                 WHERE shard_key=?
                                """,
                                (status, len(rows), inserted, con_id, datetime.now(UTC).isoformat(), shard.shard_key),
                            )
                            conn.commit()
                            if status == "completed":
                                result["completed_shards"] += 1
                            else:
                                result["failed_shards"] += 1
                                result["errors"].append({"shard_key": shard.shard_key, "symbol": shard.symbol, "error": "no_data"})
                            break
                        except Exception as exc:
                            last_error = f"{type(exc).__name__}: {exc}"
                            if attempt < max_attempts:
                                time.sleep(float(runtime["retry_delay_seconds"]) * attempt)
                                continue
                            conn.execute(
                                "UPDATE ibkr_bar_shards SET status='error',last_error=?,completed_at_utc=? WHERE shard_key=?",
                                (last_error[:2000], datetime.now(UTC).isoformat(), shard.shard_key),
                            )
                            conn.commit()
                            result["failed_shards"] += 1
                            result["errors"].append({"shard_key": shard.shard_key, "symbol": shard.symbol, "error": last_error})
                    time.sleep(float(runtime["request_pause_seconds"]))
            finally:
                source.close()
        final_status = "completed" if result["failed_shards"] == 0 else "failed"
        result["provenance_count"] = persist_market_data_provenance(conn)
        conn.execute(
            "UPDATE ibkr_pilot_runs SET status=?,completed_at_utc=?,error_text=? WHERE run_key=?",
            (final_status, datetime.now(UTC).isoformat(), json.dumps(result["errors"], ensure_ascii=False) if result["errors"] else None, run_key),
        )
        conn.commit()
        result["cache_snapshot"] = cache_snapshot(conn)
    return result


def audit_cache(config: dict[str, Any], *, idempotency_diff: dict[str, int] | None = None) -> dict[str, Any]:
    cache_db = Path(config["paths"]["cache_database"]).expanduser().resolve()
    with connect_cache(cache_db) as conn:
        ensure_cache_schema(conn)
        snapshot = cache_snapshot(conn)
        status_counts = {
            str(row["status"]): int(row["count"])
            for row in conn.execute("SELECT status,COUNT(*) AS count FROM ibkr_bar_shards GROUP BY status")
        }
        duplicate_groups = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM (
                    SELECT con_id,bar_size,what_to_show,use_rth,bar_time_utc,COUNT(*) c
                      FROM ibkr_historical_bars
                     GROUP BY con_id,bar_size,what_to_show,use_rth,bar_time_utc
                    HAVING c > 1
                )
                """
            ).fetchone()[0]
        )
        invalid_ohlc = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM ibkr_historical_bars
                 WHERE close <= 0 OR open IS NULL OR high IS NULL OR low IS NULL
                    OR high < MAX(open,close) OR low > MIN(open,close)
                    OR (volume IS NOT NULL AND volume < 0)
                """
            ).fetchone()[0]
        )
        timestamp_issues = int(
            conn.execute(
                "SELECT COUNT(*) FROM ibkr_historical_bars WHERE data_kind='intraday' AND bar_time_utc NOT LIKE '%+00:00'"
            ).fetchone()[0]
        )
        missing_window_coverage = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM ibkr_event_windows w
                 WHERE NOT EXISTS (
                    SELECT 1 FROM ibkr_historical_bars b
                     WHERE b.symbol=w.symbol AND b.data_kind='intraday'
                       AND b.bar_time_utc >= w.anchor_at_utc AND b.bar_time_utc <= w.end_at_utc
                 )
                """
            ).fetchone()[0]
        )
        missing_daily_symbols = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM (SELECT DISTINCT symbol FROM ibkr_event_windows) s
                 WHERE NOT EXISTS (
                    SELECT 1 FROM ibkr_historical_bars b WHERE b.symbol=s.symbol AND b.data_kind='daily'
                 )
                """
            ).fetchone()[0]
        )
        symbol_counts = [
            dict(row)
            for row in conn.execute(
                "SELECT symbol,data_kind,COUNT(*) AS bar_count,MIN(bar_time_utc) AS first_bar,MAX(bar_time_utc) AS last_bar FROM ibkr_historical_bars GROUP BY symbol,data_kind ORDER BY symbol,data_kind"
            )
        ]
        contract_count = snapshot["ibkr_contracts"]
        expected_symbols = int(conn.execute("SELECT COUNT(DISTINCT symbol) FROM ibkr_event_windows").fetchone()[0])
    diff = idempotency_diff or {}
    pass_conditions = {
        "all_shards_completed": status_counts.get("completed", 0) == snapshot["ibkr_bar_shards"],
        "contracts_cover_symbols": contract_count == expected_symbols and expected_symbols > 0,
        "bars_present": snapshot["ibkr_historical_bars"] > 0,
        "duplicate_bar_identity_zero": duplicate_groups == 0,
        "invalid_ohlc_zero": invalid_ohlc == 0,
        "timestamp_issues_zero": timestamp_issues == 0,
        "event_window_coverage_complete": missing_window_coverage == 0,
        "daily_symbol_coverage_complete": missing_daily_symbols == 0,
        "resume_count_diff_zero": bool(diff) and all(value == 0 for value in diff.values()),
    }
    return {
        "pilot_version": PILOT_VERSION,
        "verdict": "PASS" if all(pass_conditions.values()) else "FAIL",
        "pass_conditions": pass_conditions,
        "snapshot": snapshot,
        "shard_status_counts": status_counts,
        "duplicate_bar_identity_groups": duplicate_groups,
        "invalid_ohlc_rows": invalid_ohlc,
        "timestamp_issue_rows": timestamp_issues,
        "missing_event_window_coverage": missing_window_coverage,
        "missing_daily_symbols": missing_daily_symbols,
        "idempotency_diff": diff,
        "symbol_counts": symbol_counts,
    }


def audit_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# IBKR Historical Market-Data Collection Audit",
        "",
        f"- Verdict: **{report['verdict']}**",
        f"- Version: `{report['pilot_version']}`",
        f"- Snapshot: `{report['snapshot']}`",
        f"- Shard status: `{report['shard_status_counts']}`",
        f"- Idempotency diff: `{report['idempotency_diff']}`",
        "",
        "## Pass conditions",
        "",
    ]
    for key, value in report["pass_conditions"].items():
        lines.append(f"- `{key}`: `{value}`")
    lines.extend(["", "## Symbol coverage", "", "| Symbol | Kind | Bars | First | Last |", "| --- | --- | ---: | --- | --- |"])
    for row in report["symbol_counts"]:
        lines.append(f"| `{row['symbol']}` | `{row['data_kind']}` | {row['bar_count']} | `{row['first_bar']}` | `{row['last_bar']}` |")
    return "\n".join(lines) + "\n"
