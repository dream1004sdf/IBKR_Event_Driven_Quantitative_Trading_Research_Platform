from __future__ import annotations

import bisect
import hashlib
import json
import math
import sqlite3
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from .reaction_research_engine import ensure_reaction_research_schema
from .s04_market_data_pre_s05_audit import calendar_schedule, parse_utc


PILOT_VERSION = "s05-m253-macro-reaction-label-pilot-v1"
PLAN_VERSION = "m253-s05-macro-pilot-plan-v1"
PRICE_QUALITY_VERSION = "s05-robust-price-quality-v1"
LATENCY_MODEL_VERSION = "observed-trade-delay-v1-max300s"
RETURN_MATH_VERSION = "s05-label-math-pre-event-v1"
RESIDUAL_MODEL_VERSION = "pre-event-ols-market-beta-v1"
CORPORATE_ACTION_SOURCE = "yahoo_chart_events_snapshot"
NY = ZoneInfo("America/New_York")
INTRADAY_WINDOWS = {"5m": 5, "15m": 15, "1h": 60}
STRICT_DECISION_LAGS_SECONDS = (0, 60, 180)
DAILY_WINDOWS = {"t_plus_1_session_close": 1, "t_plus_5_session_close": 5}
REQUIRED_CONTRACT_COLUMNS = (
    "market_data_profile_id",
    "entry_trade_observed",
    "exit_trade_observed",
    "entry_delay_seconds",
    "exit_delay_seconds",
    "observed_trade_count",
    "observed_bar_count",
    "window_trade_coverage_ratio",
    "no_trade_minute_count",
    "eth_sparse_trade_flag",
    "bad_tick_flag",
    "stale_trade_flag",
    "label_quality_flag",
    "label_exclusion_reason",
    "return_basis",
    "dividend_contaminated",
    "dividend_amount_in_window",
    "split_contaminated",
    "total_return_adjusted",
    "corporate_action_version",
    "parent_meeting_id",
    "release_bundle_id",
    "overlap_group_id",
    "sample_weight",
    "independent_sample_flag",
    "market_benchmark_symbol",
    "sector_benchmark_symbol",
    "estimation_window_start_at_utc",
    "estimation_window_end_at_utc",
    "residual_model_version",
    "return_math_version",
    "decision_lag_policy",
    "session_bucket",
    "release_time_quality",
    "intraday_label_eligible",
    "horizon_anchor_policy",
    "first_regular_session_date",
    "entry_session_date",
    "exit_session_date",
)


@dataclass(frozen=True)
class PriceBar:
    symbol: str
    data_kind: str
    at: datetime
    session_date: str
    close: float
    volume: float
    trade_count: int


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stable_hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def ensure_pilot_schema(conn: sqlite3.Connection) -> None:
    ensure_reaction_research_schema(conn)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS s05_reaction_label_pilot_runs (
            pilot_version TEXT PRIMARY KEY,
            scope TEXT NOT NULL,
            source_db_sha256 TEXT NOT NULL,
            cache_db_sha256 TEXT NOT NULL,
            config_hash TEXT NOT NULL,
            first_run_at_utc TEXT NOT NULL,
            last_run_at_utc TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS s05_market_data_price_quality_flags (
            flag_key TEXT PRIMARY KEY,
            symbol TEXT NOT NULL,
            data_kind TEXT NOT NULL,
            bar_time_utc TEXT NOT NULL,
            extreme_return_flag INTEGER NOT NULL DEFAULT 0,
            stale_trade_flag INTEGER NOT NULL DEFAULT 0,
            isolated_spike_flag INTEGER NOT NULL DEFAULT 0,
            bad_tick_flag INTEGER NOT NULL DEFAULT 0,
            price_quality_version TEXT NOT NULL,
            details_json TEXT NOT NULL DEFAULT '{}',
            UNIQUE(symbol,data_kind,bar_time_utc,price_quality_version)
        );
        """
    )


def _session_bucket(value: datetime) -> str:
    local = value.astimezone(NY)
    local_time = local.time()
    if time(4, 0) <= local_time < time(9, 30):
        return "premarket"
    if time(9, 30) <= local_time < time(16, 0):
        return "regular"
    if time(16, 0) <= local_time < time(20, 0):
        return "afterhours"
    return "outside_session"


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * percentile
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return float(ordered[lower])
    return float(ordered[lower] * (upper - index) + ordered[upper] * (index - lower))


def load_market_data(cache_path: Path) -> dict[str, Any]:
    intraday: dict[str, list[PriceBar]] = defaultdict(list)
    daily: dict[str, list[PriceBar]] = defaultdict(list)
    profiles: dict[tuple[str, str], dict[str, Any]] = {}
    with sqlite3.connect(cache_path) as conn:
        conn.row_factory = sqlite3.Row
        for row in conn.execute(
            """SELECT symbol,data_kind,bar_time_utc,session_date,close,
                      COALESCE(volume,0) volume,COALESCE(trade_count,0) trade_count
                 FROM ibkr_historical_bars
                ORDER BY symbol,data_kind,bar_time_utc"""
        ):
            bar = PriceBar(
                symbol=str(row["symbol"]),
                data_kind=str(row["data_kind"]),
                at=parse_utc(str(row["bar_time_utc"])),
                session_date=str(row["session_date"]),
                close=float(row["close"]),
                volume=float(row["volume"] or 0),
                trade_count=int(row["trade_count"] or 0),
            )
            (intraday if bar.data_kind == "intraday" else daily)[bar.symbol].append(bar)
        for row in conn.execute("SELECT * FROM ibkr_market_data_provenance"):
            profiles[(str(row["symbol"]), str(row["data_kind"]))] = dict(row)
        windows = [dict(row) for row in conn.execute("SELECT * FROM ibkr_event_windows")]
        anchors = [dict(row) for row in conn.execute("SELECT * FROM ibkr_daily_event_anchors")]
        contracts = {
            str(row["symbol"]): dict(row) for row in conn.execute("SELECT * FROM ibkr_contracts")
        }
    return {
        "intraday": dict(intraday),
        "daily": dict(daily),
        "profiles": profiles,
        "windows": windows,
        "anchors": anchors,
        "contracts": contracts,
    }


def audit_price_quality(
    conn: sqlite3.Connection,
    market_data: dict[str, Any],
) -> tuple[dict[tuple[str, str, str], set[str]], dict[str, Any]]:
    flags: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    per_symbol: list[dict[str, Any]] = []
    for data_kind in ("intraday", "daily"):
        for symbol, bars in sorted(market_data[data_kind].items()):
            returns: list[float] = []
            for previous, current in zip(bars, bars[1:]):
                if previous.close > 0 and current.close > 0:
                    returns.append(math.log(current.close / previous.close))
            abs_returns = [abs(value) for value in returns]
            median_abs = statistics.median(abs_returns) if abs_returns else 0.0
            mad = statistics.median([abs(value - median_abs) for value in abs_returns]) if abs_returns else 0.0
            floor = 0.02 if data_kind == "intraday" else 0.15
            threshold = max(floor, median_abs + 25.0 * mad)
            for index, bar in enumerate(bars):
                key = (data_kind, symbol, bar.at.isoformat())
                if bar.close <= 0:
                    flags[key].add("bad_tick")
                if index > 0 and bars[index - 1].close > 0 and bar.close > 0:
                    change = math.log(bar.close / bars[index - 1].close)
                    if abs(change) > threshold:
                        flags[key].add("extreme_return")
                    if index + 1 < len(bars) and bars[index + 1].close > 0:
                        reversal = math.log(bars[index + 1].close / bar.close)
                        through = math.log(bars[index + 1].close / bars[index - 1].close)
                        if (
                            change * reversal < 0
                            and abs(change) > threshold
                            and abs(reversal) > threshold
                            and abs(through) < threshold / 4.0
                        ):
                            flags[key].update({"isolated_spike", "bad_tick"})
            if data_kind == "intraday":
                run_start = 0
                for index in range(1, len(bars) + 1):
                    continuation = (
                        index < len(bars)
                        and bars[index].close == bars[index - 1].close
                        and bars[index].at - bars[index - 1].at == timedelta(minutes=1)
                        and _session_bucket(bars[index].at) == "regular"
                    )
                    if continuation:
                        continue
                    if index - run_start >= 30:
                        for stale_bar in bars[run_start:index]:
                            flags[(data_kind, symbol, stale_bar.at.isoformat())].add("stale_trade")
                    run_start = index
            counts = Counter(flag for key, values in flags.items() if key[0] == data_kind and key[1] == symbol for flag in values)
            per_symbol.append(
                {
                    "symbol": symbol,
                    "data_kind": data_kind,
                    "bars": len(bars),
                    "robust_abs_return_threshold": threshold,
                    "median_abs_return": median_abs,
                    "mad_abs_return": mad,
                    "flag_counts": dict(counts),
                }
            )
    now = _utc_now()
    for (data_kind, symbol, bar_time), values in flags.items():
        payload = {
            "extreme_return_flag": int("extreme_return" in values),
            "stale_trade_flag": int("stale_trade" in values),
            "isolated_spike_flag": int("isolated_spike" in values),
            "bad_tick_flag": int("bad_tick" in values),
        }
        flag_key = _stable_hash([symbol, data_kind, bar_time, PRICE_QUALITY_VERSION])
        conn.execute(
            """INSERT INTO s05_market_data_price_quality_flags(
                   flag_key,symbol,data_kind,bar_time_utc,extreme_return_flag,stale_trade_flag,
                   isolated_spike_flag,bad_tick_flag,price_quality_version,details_json
               ) VALUES(?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(flag_key) DO UPDATE SET
                   extreme_return_flag=excluded.extreme_return_flag,
                   stale_trade_flag=excluded.stale_trade_flag,
                   isolated_spike_flag=excluded.isolated_spike_flag,
                   bad_tick_flag=excluded.bad_tick_flag,
                   details_json=excluded.details_json""",
            (
                flag_key,
                symbol,
                data_kind,
                bar_time,
                payload["extreme_return_flag"],
                payload["stale_trade_flag"],
                payload["isolated_spike_flag"],
                payload["bad_tick_flag"],
                PRICE_QUALITY_VERSION,
                json.dumps({"flags": sorted(values), "audited_at": now}),
            ),
        )
    summary_counts = Counter(flag for values in flags.values() for flag in values)
    return flags, {
        "version": PRICE_QUALITY_VERSION,
        "flagged_bar_keys": len(flags),
        "counts": dict(summary_counts),
        "per_symbol": per_symbol,
    }


def _first_at_or_after(bars: list[PriceBar], target: datetime) -> PriceBar | None:
    index = bisect.bisect_left(bars, target, key=lambda bar: bar.at)
    return bars[index] if index < len(bars) else None


def _bars_between(bars: list[PriceBar], start: datetime, end: datetime) -> list[PriceBar]:
    left = bisect.bisect_left(bars, start, key=lambda bar: bar.at)
    right = bisect.bisect_right(bars, end, key=lambda bar: bar.at)
    return bars[left:right]


def _daily_by_session(bars: list[PriceBar]) -> tuple[list[str], dict[str, PriceBar]]:
    by_date = {bar.session_date: bar for bar in bars}
    return sorted(by_date), by_date


def _first_regular_session(anchor: datetime, schedule: dict[date, tuple[datetime, datetime]]) -> str:
    for session_date in sorted(schedule):
        if schedule[session_date][1] > anchor:
            return session_date.isoformat()
    return ""


def _action_map(snapshot: dict[str, Any]) -> tuple[dict[str, list[dict[str, Any]]], str]:
    actions = {
        symbol: list((payload or {}).get("actions") or [])
        for symbol, payload in (snapshot.get("symbols") or {}).items()
    }
    return actions, _stable_hash(snapshot)


def _actions_in_window(
    actions: dict[str, list[dict[str, Any]]], symbol: str, start_date: str, end_date: str
) -> tuple[float, bool, bool]:
    dividends = 0.0
    split = False
    contaminated = False
    for action in actions.get(symbol, []):
        action_date = str(action.get("action_date") or "")
        if not action_date or not (start_date < action_date <= end_date):
            continue
        contaminated = True
        if str(action.get("action_type")) == "dividend":
            dividends += float(action.get("amount") or 0.0)
        elif str(action.get("action_type")) == "split":
            split = True
    return dividends, contaminated and dividends != 0.0, split


def _flag_values(
    price_flags: dict[tuple[str, str, str], set[str]], data_kind: str, symbol: str, bars: Iterable[PriceBar]
) -> dict[str, int]:
    combined: set[str] = set()
    for bar in bars:
        combined.update(price_flags.get((data_kind, symbol, bar.at.isoformat()), set()))
    return {
        "bad_tick_flag": int("bad_tick" in combined),
        "extreme_return_flag": int("extreme_return" in combined),
        "stale_trade_flag": int("stale_trade" in combined),
        "isolated_spike_flag": int("isolated_spike" in combined),
    }


def _price_quality_severity(flags: dict[str, int]) -> str:
    if flags.get("bad_tick_flag") or flags.get("isolated_spike_flag"):
        return "critical"
    if flags.get("stale_trade_flag") or flags.get("extreme_return_flag"):
        return "warning"
    return "none"


def _upsert_label(conn: sqlite3.Connection, values: dict[str, Any]) -> int:
    columns = list(values)
    placeholders = ",".join("?" for _ in columns)
    conflict = "reaction_plan_id,asset_symbol,asset_role,proxy_family,window,decision_lag_seconds"
    updates = ",".join(
        f"{column}=excluded.{column}"
        for column in columns
        if column not in {"reaction_label_id", "created_at"}
    )
    conn.execute(
        f"INSERT INTO event_reaction_labels({','.join(columns)}) VALUES({placeholders}) "
        f"ON CONFLICT({conflict}) DO UPDATE SET {updates}",
        tuple(values[column] for column in columns),
    )
    row = conn.execute(
        """SELECT reaction_label_id FROM event_reaction_labels
            WHERE reaction_plan_id=? AND asset_symbol=? AND asset_role=? AND proxy_family=?
              AND window=? AND decision_lag_seconds=?""",
        (
            values["reaction_plan_id"],
            values["asset_symbol"],
            values["asset_role"],
            values["proxy_family"],
            values["window"],
            values["decision_lag_seconds"],
        ),
    ).fetchone()
    return int(row[0])


def _base_label(
    *,
    event: dict[str, Any],
    plan_id: int,
    symbol: str,
    window: str,
    profile_id: str,
    start_at: str,
    end_at: str,
    session_bucket: str,
    decision_lag_seconds: int = 0,
) -> dict[str, Any]:
    return {
        "event_cluster_id": int(event["event_cluster_id"]),
        "reaction_plan_id": plan_id,
        "asset_symbol": symbol,
        "asset_role": "macro_reaction_proxy",
        "proxy_family": str(event["source_system"]),
        "window": window,
        "decision_lag_seconds": int(decision_lag_seconds),
        "start_at_utc": start_at,
        "end_at_utc": end_at,
        "session_bucket": session_bucket,
        "market_data_profile_id": profile_id,
        "price_quality_version": PRICE_QUALITY_VERSION,
        "price_quality_severity": "none",
        "label_quality_flag": "excluded",
        "label_quality_reason": "pilot_quality_contract",
        "label_exclusion_reason": "uncomputed",
        "return_basis": "raw_price_and_total_return",
        "corporate_action_source": CORPORATE_ACTION_SOURCE,
        "strict_training_eligible": 0,
        "parent_meeting_id": event["parent_meeting_id"],
        "release_bundle_id": event["release_bundle_id"],
        "overlap_group_id": event["overlap_group_id"],
        "subevent_type": event["subevent_type"],
        "sample_weight": float(event["overlap_weight"]),
        "independent_sample_flag": int(float(event["overlap_weight"]) == 1.0),
        "validation_split_group": event["overlap_group_id"],
        "market_benchmark_symbol": "SPY",
        "sector_benchmark_symbol": {
            "federal_reserve": "DIA",
            "ecb": "VGK",
            "boj": "EWJ",
        }.get(str(event["source_system"]), "SPY"),
        "residual_model_version": RESIDUAL_MODEL_VERSION,
        "return_math_version": RETURN_MATH_VERSION,
        "decision_lag_policy": (
            "event_time_first_complete_minute" if int(decision_lag_seconds) == 0
            else f"event_time_plus_{int(decision_lag_seconds)}s_first_complete_minute"
        ),
        "release_time_quality": event["release_time_quality"],
        "intraday_label_eligible": int(event["release_time_quality"] in {"official_exact", "provider_exact"}),
        "execution_price_model": "observed_ibkr_trades_reaction_only_not_executable",
        "spread_model_version": "not_estimated_v0",
        "slippage_model_version": "not_estimated_v0",
        "latency_model_version": LATENCY_MODEL_VERSION,
        "cost_estimate_quality": "not_available_reaction_only",
        "outcome_builder_version": PILOT_VERSION,
        "market_data_source": "ibkr_historical_trades",
        "calendar_version": "XNYS-exchange-calendars",
        "created_at": _utc_now(),
    }


def _plan_and_assets(
    conn: sqlite3.Connection,
    events: dict[int, dict[str, Any]],
    event_assets: dict[int, set[str]],
    contracts: dict[str, dict[str, Any]],
) -> dict[int, int]:
    plan_ids: dict[int, int] = {}
    now = _utc_now()
    for event in events.values():
        cluster_id = int(event["event_cluster_id"])
        conn.execute(
            """INSERT OR IGNORE INTO reaction_universe_plans(
                   event_cluster_id,plan_version,created_at,available_at_cutoff,core_scope_version,
                   event_proxy_map_version,security_master_version,mapper_version,classifier_version,reason
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                cluster_id,
                PLAN_VERSION,
                now,
                event["anchor_at_utc"],
                "s04-18-symbol-macro-slice-v1",
                "s04-source-specific-proxy-map-v1",
                "ibkr-resolved-conid-v1",
                "not_applicable_macro_pilot",
                "s04p1-canonical-macro-v1",
                "clone-only S05/M253 macro reaction-label pilot",
            ),
        )
        plan_id = int(
            conn.execute(
                "SELECT reaction_plan_id FROM reaction_universe_plans WHERE event_cluster_id=? AND plan_version=?",
                (cluster_id, PLAN_VERSION),
            ).fetchone()[0]
        )
        plan_ids[int(event["macro_event_id"])] = plan_id
        for symbol in sorted(event_assets.get(int(event["macro_event_id"]), set())):
            contract = contracts[symbol]
            conn.execute(
                """INSERT OR IGNORE INTO reaction_universe_assets(
                       reaction_plan_id,event_cluster_id,asset_symbol,con_id,asset_role,proxy_family,
                       inclusion_reason,inclusion_source,tradable_candidate,label_scope,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    plan_id,
                    cluster_id,
                    symbol,
                    int(contract["con_id"]),
                    "macro_reaction_proxy",
                    str(event["source_system"]),
                    "present in audited S04 market-data event window or daily anchor",
                    "s04_ibkr_market_data_closeout",
                    1,
                    "pilot_macro",
                    now,
                ),
            )
    return plan_ids


def _build_nonzero_lag_labels(
    conn: sqlite3.Connection,
    *,
    market_data: dict[str, Any],
    events: dict[int, dict[str, Any]],
    plan_ids: dict[int, int],
    price_flags: dict[tuple[str, str, str], set[str]],
    actions: dict[str, list[dict[str, Any]]],
    action_version: str,
    schedule: dict[date, tuple[datetime, datetime]],
    max_trade_delay_seconds: int,
) -> list[int]:
    label_ids: list[int] = []
    for window_row in market_data["windows"]:
        macro_event_id = int(window_row["macro_event_id"])
        event = events[macro_event_id]
        symbol = str(window_row["symbol"])
        event_anchor = parse_utc(str(window_row["anchor_at_utc"]))
        bars = market_data["intraday"][symbol]
        intraday_profile = market_data["profiles"][(symbol, "intraday")]["provenance_key"]
        for lag in STRICT_DECISION_LAGS_SECONDS[1:]:
            anchor = event_anchor + timedelta(seconds=lag)
            entry = _first_at_or_after(bars, anchor)
            entry_delay = int((entry.at - anchor).total_seconds()) if entry else None
            for window, minutes in INTRADAY_WINDOWS.items():
                target_exit = anchor + timedelta(minutes=minutes)
                exit_bar = _first_at_or_after(bars, target_exit)
                exit_delay = int((exit_bar.at - target_exit).total_seconds()) if exit_bar else None
                observed = _bars_between(bars, entry.at, exit_bar.at) if entry and exit_bar else []
                expected_count = minutes + 1
                coverage = min(1.0, len(observed) / expected_count) if observed else 0.0
                flags = _flag_values(price_flags, "intraday", symbol, observed)
                exclusion = ""
                if entry is None or exit_bar is None:
                    exclusion = "missing_entry_or_exit_trade"
                elif entry_delay is None or exit_delay is None or entry_delay > max_trade_delay_seconds or exit_delay > max_trade_delay_seconds:
                    exclusion = "trade_delay_exceeds_300s"
                elif any(flags.values()):
                    exclusion = "price_quality_flag"
                elif coverage < 0.20:
                    exclusion = "insufficient_observed_trade_coverage"
                raw_return = ((exit_bar.close / entry.close) - 1.0) if entry and exit_bar and entry.close > 0 else None
                closes = [bar.close for bar in observed]
                base = _base_label(
                    event=event,
                    plan_id=plan_ids[macro_event_id],
                    symbol=symbol,
                    window=window,
                    profile_id=str(intraday_profile),
                    start_at=entry.at.isoformat() if entry else anchor.isoformat(),
                    end_at=exit_bar.at.isoformat() if exit_bar else target_exit.isoformat(),
                    session_bucket=_session_bucket(anchor),
                    decision_lag_seconds=lag,
                )
                base.update(
                    {
                        "raw_return": raw_return,
                        "total_return_adjusted": raw_return,
                        "mfe": max((value / entry.close) - 1.0 for value in closes) if closes and entry else None,
                        "mae": min((value / entry.close) - 1.0 for value in closes) if closes and entry else None,
                        "entry_trade_observed": int(entry is not None and entry.at == anchor),
                        "exit_trade_observed": int(exit_bar is not None and exit_bar.at == target_exit),
                        "entry_delay_seconds": entry_delay,
                        "exit_delay_seconds": exit_delay,
                        "observed_trade_count": sum(bar.trade_count for bar in observed),
                        "observed_bar_count": len(observed),
                        "window_trade_coverage_ratio": coverage,
                        "no_trade_minute_count": max(0, expected_count - len(observed)),
                        "eth_sparse_trade_flag": int(_session_bucket(anchor) != "regular" and coverage < 0.80),
                        **flags,
                        "price_quality_severity": _price_quality_severity(flags),
                        "label_quality_flag": "excluded" if exclusion else ("sparse_observed_trades" if coverage < 0.80 else "pass"),
                        "label_quality_reason": exclusion or "observed_trades_only",
                        "label_exclusion_reason": exclusion,
                        "dividend_contaminated": 0,
                        "dividend_amount_in_window": 0.0,
                        "split_contaminated": 0,
                        "corporate_action_version": action_version,
                        "strict_training_eligible": int(not exclusion and coverage >= 0.80),
                        "horizon_anchor_policy": "decision_lag_then_wall_clock_observed_trades_only",
                        "first_regular_session_date": _first_regular_session(anchor, schedule),
                        "entry_session_date": entry.session_date if entry else "",
                        "exit_session_date": exit_bar.session_date if exit_bar else "",
                    }
                )
                label_ids.append(_upsert_label(conn, base))

            first_session = _first_regular_session(anchor, schedule)
            _, daily_map = _daily_by_session(market_data["daily"][symbol])
            close_bar = daily_map.get(first_session)
            observed_to_close = (
                _bars_between(bars, entry.at, schedule[date.fromisoformat(first_session)][1])
                if entry and close_bar and first_session
                else []
            )
            flags = _flag_values(price_flags, "intraday", symbol, observed_to_close)
            if close_bar:
                daily_flags = _flag_values(price_flags, "daily", symbol, [close_bar])
                flags = {name: max(flags[name], daily_flags[name]) for name in flags}
            dividend, dividend_flag, split_flag = _actions_in_window(
                actions, symbol, entry.session_date if entry else anchor.date().isoformat(), first_session
            ) if first_session else (0.0, False, False)
            exclusion = ""
            if entry is None or close_bar is None:
                exclusion = "missing_entry_or_first_regular_close"
            elif entry_delay is None or entry_delay > max_trade_delay_seconds:
                exclusion = "trade_delay_exceeds_300s"
            elif any(flags.values()) or split_flag:
                exclusion = "price_or_split_quality_flag"
            raw_return = ((close_bar.close / entry.close) - 1.0) if entry and close_bar and entry.close > 0 else None
            base = _base_label(
                event=event,
                plan_id=plan_ids[macro_event_id],
                symbol=symbol,
                window="first_regular_close",
                profile_id=json.dumps(
                    [intraday_profile, market_data["profiles"][(symbol, "daily")]["provenance_key"]],
                    separators=(",", ":"),
                ),
                start_at=entry.at.isoformat() if entry else anchor.isoformat(),
                end_at=schedule[date.fromisoformat(first_session)][1].isoformat() if first_session else "",
                session_bucket=_session_bucket(anchor),
                decision_lag_seconds=lag,
            )
            base.update(
                {
                    "raw_return": raw_return,
                    "total_return_adjusted": ((close_bar.close + dividend) / entry.close - 1.0) if entry and close_bar and entry.close > 0 else None,
                    "entry_trade_observed": int(entry is not None and entry.at == anchor),
                    "exit_trade_observed": int(close_bar is not None),
                    "entry_delay_seconds": entry_delay,
                    "exit_delay_seconds": 0 if close_bar else None,
                    "observed_trade_count": sum(bar.trade_count for bar in observed_to_close),
                    "observed_bar_count": len(observed_to_close),
                    "window_trade_coverage_ratio": 1.0 if close_bar and entry else 0.0,
                    "no_trade_minute_count": 0,
                    "eth_sparse_trade_flag": int(_session_bucket(anchor) != "regular" and entry_delay not in (None, 0)),
                    **flags,
                    "price_quality_severity": _price_quality_severity(flags),
                    "label_quality_flag": "excluded" if exclusion else "pass",
                    "label_quality_reason": exclusion or "observed_entry_and_regular_close",
                    "label_exclusion_reason": exclusion,
                    "dividend_contaminated": int(dividend_flag),
                    "dividend_amount_in_window": dividend,
                    "split_contaminated": int(split_flag),
                    "corporate_action_version": action_version,
                    "strict_training_eligible": int(
                        not exclusion
                        and not (_session_bucket(anchor) != "regular" and entry_delay not in (None, 0))
                    ),
                    "horizon_anchor_policy": "decision_lag_to_first_regular_session_close",
                    "first_regular_session_date": first_session,
                    "entry_session_date": entry.session_date if entry else "",
                    "exit_session_date": first_session,
                }
            )
            label_ids.append(_upsert_label(conn, base))
    return label_ids


def _bar_return_map(bars: list[PriceBar], *, before: datetime, lookback: timedelta) -> dict[datetime, tuple[float, float]]:
    left = bisect.bisect_left(bars, before - lookback, key=lambda bar: bar.at)
    right = bisect.bisect_left(bars, before, key=lambda bar: bar.at)
    selected = [bar for bar in bars[left:right] if bar.close > 0]
    result: dict[datetime, tuple[float, float]] = {}
    for previous, current in zip(selected, selected[1:]):
        if current.at <= previous.at or previous.close <= 0:
            continue
        result[current.at] = (math.log(current.close / previous.close), float(current.trade_count))
    return result


def _daily_return_map(bars: list[PriceBar], *, before_session: str, lookback_sessions: int = 8) -> dict[str, tuple[float, float]]:
    selected = [bar for bar in bars if bar.session_date < before_session and bar.close > 0][-lookback_sessions - 1 :]
    result: dict[str, tuple[float, float]] = {}
    for previous, current in zip(selected, selected[1:]):
        if previous.close > 0:
            result[current.session_date] = (math.log(current.close / previous.close), float(current.trade_count))
    return result


def _ols_beta(asset_returns: list[float], benchmark_returns: list[float]) -> float | None:
    if len(asset_returns) != len(benchmark_returns) or len(asset_returns) < 2:
        return None
    benchmark_mean = statistics.fmean(benchmark_returns)
    asset_mean = statistics.fmean(asset_returns)
    variance = sum((value - benchmark_mean) ** 2 for value in benchmark_returns)
    if variance <= 0:
        return None
    covariance = sum(
        (asset - asset_mean) * (benchmark - benchmark_mean)
        for asset, benchmark in zip(asset_returns, benchmark_returns)
    )
    return covariance / variance


def _safe_zscore(value: float, sample: list[float]) -> float | None:
    if len(sample) < 2:
        return None
    deviation = statistics.stdev(sample)
    if deviation <= 0:
        return None
    return (value - statistics.fmean(sample)) / deviation


def _benchmark_return(
    market_data: dict[str, Any],
    *,
    symbol: str,
    window: str,
    target_entry: datetime,
    target_exit: datetime | None,
    first_session: str,
    exit_session: str,
) -> float | None:
    if window in INTRADAY_WINDOWS:
        bars = market_data["intraday"].get(symbol, [])
        entry = _first_at_or_after(bars, target_entry)
        exit_bar = _first_at_or_after(bars, target_exit) if target_exit else None
    elif window == "first_regular_close":
        entry = _first_at_or_after(market_data["intraday"].get(symbol, []), target_entry)
        _, daily_map = _daily_by_session(market_data["daily"].get(symbol, []))
        exit_bar = daily_map.get(first_session)
    else:
        _, daily_map = _daily_by_session(market_data["daily"].get(symbol, []))
        entry = daily_map.get(first_session)
        exit_bar = daily_map.get(exit_session)
    if entry is None or exit_bar is None or entry.close <= 0:
        return None
    return (exit_bar.close / entry.close) - 1.0


def populate_label_math(
    conn: sqlite3.Connection,
    *,
    market_data: dict[str, Any],
    events: dict[int, dict[str, Any]],
    plan_version: str | None = None,
) -> dict[str, Any]:
    event_by_cluster = {int(event["event_cluster_id"]): event for event in events.values()}
    if plan_version:
        rows = [
            dict(row)
            for row in conn.execute(
                """SELECT l.* FROM event_reaction_labels l
                     JOIN reaction_universe_plans p ON p.reaction_plan_id=l.reaction_plan_id
                    WHERE p.plan_version=? ORDER BY l.reaction_label_id""",
                (plan_version,),
            )
        ]
    else:
        rows = [dict(row) for row in conn.execute("SELECT * FROM event_reaction_labels ORDER BY reaction_label_id")]
    updated = 0
    insufficient = 0
    missing_benchmark = 0
    for row in rows:
        raw_return = row.get("raw_return")
        if raw_return is None:
            continue
        event = event_by_cluster[int(row["event_cluster_id"])]
        model_visible = parse_utc(str(event["anchor_at_utc"]))
        lag = int(row["decision_lag_seconds"] or 0)
        target_entry = model_visible + timedelta(seconds=lag)
        window = str(row["window"])
        target_exit = (
            target_entry + timedelta(minutes=INTRADAY_WINDOWS[window])
            if window in INTRADAY_WINDOWS
            else None
        )
        symbol = str(row["asset_symbol"])
        market_symbol = str(row.get("market_benchmark_symbol") or "SPY")
        sector_symbol = str(row.get("sector_benchmark_symbol") or "SPY")
        first_session = str(row.get("first_regular_session_date") or "")
        exit_session = str(row.get("exit_session_date") or "")
        market_return = _benchmark_return(
            market_data,
            symbol=market_symbol,
            window=window,
            target_entry=target_entry,
            target_exit=target_exit,
            first_session=first_session,
            exit_session=exit_session,
        )
        sector_return = _benchmark_return(
            market_data,
            symbol=sector_symbol,
            window=window,
            target_entry=target_entry,
            target_exit=target_exit,
            first_session=first_session,
            exit_session=exit_session,
        )
        data_kind = "daily" if window in DAILY_WINDOWS else "intraday"
        if data_kind == "intraday":
            asset_map = _bar_return_map(
                market_data["intraday"].get(symbol, []),
                before=model_visible,
                lookback=timedelta(minutes=60),
            )
            benchmark_map = _bar_return_map(
                market_data["intraday"].get(market_symbol, []),
                before=model_visible,
                lookback=timedelta(minutes=60),
            )
            common = sorted(set(asset_map).intersection(benchmark_map))
            minimum_pairs = 20
            estimation_start = common[0].isoformat() if common else ""
            estimation_end = common[-1].isoformat() if common else ""
            asset_returns = [asset_map[key][0] for key in common]
            benchmark_returns = [benchmark_map[key][0] for key in common]
            pre_volumes = [asset_map[key][1] for key in common]
            selected_bars = _bars_between(
                market_data["intraday"].get(symbol, []),
                parse_utc(str(row["start_at_utc"])),
                parse_utc(str(row["end_at_utc"])),
            ) if row.get("start_at_utc") and row.get("end_at_utc") else []
            if window == "first_regular_close" and first_session:
                _, daily_map_for_close = _daily_by_session(market_data["daily"].get(symbol, []))
                regular_close_bar = daily_map_for_close.get(first_session)
                if regular_close_bar and (not selected_bars or selected_bars[-1].close != regular_close_bar.close):
                    selected_bars = [*selected_bars, regular_close_bar]
        else:
            asset_map_daily = _daily_return_map(
                market_data["daily"].get(symbol, []), before_session=first_session
            )
            benchmark_map_daily = _daily_return_map(
                market_data["daily"].get(market_symbol, []), before_session=first_session
            )
            common_daily = sorted(set(asset_map_daily).intersection(benchmark_map_daily))
            minimum_pairs = 5
            estimation_start = common_daily[0] + "T00:00:00+00:00" if common_daily else ""
            estimation_end = common_daily[-1] + "T00:00:00+00:00" if common_daily else ""
            asset_returns = [asset_map_daily[key][0] for key in common_daily]
            benchmark_returns = [benchmark_map_daily[key][0] for key in common_daily]
            pre_volumes = [asset_map_daily[key][1] for key in common_daily]
            daily_dates, daily_map = _daily_by_session(market_data["daily"].get(symbol, []))
            if first_session in daily_dates and exit_session in daily_dates:
                start_index = daily_dates.index(first_session)
                end_index = daily_dates.index(exit_session)
                selected_bars = [daily_map[key] for key in daily_dates[start_index : end_index + 1]]
            else:
                selected_bars = []
        beta = _ols_beta(asset_returns, benchmark_returns) if len(common if data_kind == "intraday" else common_daily) >= minimum_pairs else None
        if symbol == market_symbol and len(asset_returns) >= minimum_pairs:
            beta = 1.0
        label_exclusion = str(row.get("label_exclusion_reason") or "")
        math_reason = ""
        if market_return is None or sector_return is None:
            missing_benchmark += 1
            math_reason = "missing_required_benchmark_pair"
        elif beta is None or len(asset_returns) < minimum_pairs:
            insufficient += 1
            math_reason = "insufficient_pre_event_estimation"
        market_adjusted = float(raw_return) - market_return if market_return is not None else None
        sector_adjusted = float(raw_return) - sector_return if sector_return is not None else None
        residual = float(raw_return) - beta * market_return if beta is not None and market_return is not None else None
        horizon_units = max(1, int(row.get("observed_bar_count") or 1))
        scaled_pre_returns = [value * math.sqrt(horizon_units) for value in asset_returns]
        return_zscore = _safe_zscore(float(raw_return), scaled_pre_returns)
        observed_bar_count = max(1, int(row.get("observed_bar_count") or 1))
        avg_observed_trades = float(row.get("observed_trade_count") or 0) / observed_bar_count
        volume_zscore = _safe_zscore(avg_observed_trades, pre_volumes)
        if not math_reason and (return_zscore is None or volume_zscore is None):
            math_reason = "incomplete_pre_event_standardization"
        observed_returns = [
            math.log(current.close / previous.close)
            for previous, current in zip(selected_bars, selected_bars[1:])
            if previous.close > 0 and current.close > 0
        ]
        realized_volatility = statistics.stdev(observed_returns) if len(observed_returns) >= 2 else 0.0
        mfe = row.get("mfe")
        mae = row.get("mae")
        if selected_bars and selected_bars[0].close > 0:
            path_returns = [(bar.close / selected_bars[0].close) - 1.0 for bar in selected_bars]
            mfe = max(path_returns)
            mae = min(path_returns)
        strict = int(row.get("strict_training_eligible") or 0)
        if math_reason:
            strict = 0
            if not label_exclusion:
                label_exclusion = math_reason
        conn.execute(
            """UPDATE event_reaction_labels
                  SET market_adjusted_return=?,sector_adjusted_return=?,proxy_spread_return=?,
                      residual_return=?,return_zscore=?,volume_zscore=?,realized_volatility=?,
                      mfe=?,mae=?,beta_estimate=?,estimation_window_start_at_utc=?,
                      estimation_window_end_at_utc=?,residual_model_version=?,return_math_version=?,
                      strict_training_eligible=?,label_exclusion_reason=?,label_quality_flag=?,
                      label_quality_reason=?
                WHERE reaction_label_id=?""",
            (
                market_adjusted,
                sector_adjusted,
                sector_adjusted,
                residual,
                return_zscore,
                volume_zscore,
                realized_volatility,
                mfe,
                mae,
                beta,
                estimation_start,
                estimation_end,
                RESIDUAL_MODEL_VERSION,
                RETURN_MATH_VERSION,
                strict,
                label_exclusion,
                "excluded" if label_exclusion else str(row.get("label_quality_flag") or "pass"),
                math_reason or str(row.get("label_quality_reason") or "label_math_complete"),
                int(row["reaction_label_id"]),
            ),
        )
        updated += 1
    return {
        "version": RETURN_MATH_VERSION,
        "updated_labels": updated,
        "insufficient_pre_event_estimation": insufficient,
        "missing_required_benchmark_pair": missing_benchmark,
    }


def apply_selection_bias_policy(
    conn: sqlite3.Connection,
    *,
    minimum_symbol_window_strict_coverage: float = 0.50,
    plan_version: str | None = None,
) -> dict[str, Any]:
    disabled: list[dict[str, Any]] = []
    plan_ids: list[int] = []
    plan_filter = ""
    plan_params: tuple[Any, ...] = ()
    if plan_version:
        plan_ids = [
            int(row[0])
            for row in conn.execute(
                "SELECT reaction_plan_id FROM reaction_universe_plans WHERE plan_version=?",
                (plan_version,),
            )
        ]
        if not plan_ids:
            return {
                "policy": "no matching plan rows",
                "minimum_coverage": minimum_symbol_window_strict_coverage,
                "disabled_symbol_windows": [],
                "disabled_group_count": 0,
            }
        plan_filter = f" AND reaction_plan_id IN ({','.join('?' for _ in plan_ids)})"
        plan_params = tuple(plan_ids)
    for row in conn.execute(
        f"""SELECT asset_symbol,window,COUNT(*) total,SUM(strict_training_eligible) strict_count
             FROM event_reaction_labels
            WHERE window IN ('5m','15m','1h','first_regular_close')
            {plan_filter}
            GROUP BY asset_symbol,window""",
        plan_params,
    ):
        total = int(row["total"] or 0)
        strict_count = int(row["strict_count"] or 0)
        coverage = strict_count / total if total else 0.0
        if coverage >= minimum_symbol_window_strict_coverage:
            continue
        disabled.append(
            {
                "symbol": str(row["asset_symbol"]),
                "window": str(row["window"]),
                "total": total,
                "strict_before_policy": strict_count,
                "coverage_before_policy": coverage,
            }
        )
        conn.execute(
            """UPDATE event_reaction_labels
                  SET strict_training_eligible=0,
                      label_quality_flag=CASE WHEN label_exclusion_reason='' THEN 'excluded' ELSE label_quality_flag END,
                      label_exclusion_reason=CASE WHEN label_exclusion_reason='' THEN 'low_symbol_window_strict_coverage_policy' ELSE label_exclusion_reason END,
                      label_quality_reason=CASE WHEN label_exclusion_reason='' THEN 'intraday_symbol_window_coverage_below_50pct' ELSE label_quality_reason END
                WHERE asset_symbol=? AND window=? AND strict_training_eligible=1
                """ + plan_filter,
            (str(row["asset_symbol"]), str(row["window"]), *plan_params),
        )
    return {
        "policy": "disable strict intraday symbol-window when pre-policy strict coverage is below 50%; retain daily labels",
        "minimum_coverage": minimum_symbol_window_strict_coverage,
        "disabled_symbol_windows": disabled,
        "disabled_group_count": len(disabled),
    }


def build_pilot_labels(
    clone_path: Path,
    cache_path: Path,
    supplemental_audit_path: Path,
    corporate_actions_path: Path,
    *,
    max_trade_delay_seconds: int = 300,
) -> dict[str, Any]:
    audit = json.loads(supplemental_audit_path.read_text(encoding="utf-8"))
    snapshot = json.loads(corporate_actions_path.read_text(encoding="utf-8"))
    market_data = load_market_data(cache_path)
    events = {int(row["macro_event_id"]): row for row in audit["event_overlap"]["events"]}
    actions, action_version = _action_map(snapshot)
    event_assets: dict[int, set[str]] = defaultdict(set)
    for row in market_data["windows"] + market_data["anchors"]:
        event_assets[int(row["macro_event_id"])].add(str(row["symbol"]))
    min_date = min(parse_utc(row["anchor_at_utc"]).date() for row in market_data["anchors"]) - timedelta(days=10)
    max_date = max(parse_utc(row["anchor_at_utc"]).date() for row in market_data["anchors"]) + timedelta(days=20)
    schedule = calendar_schedule(min_date, max_date)
    with sqlite3.connect(clone_path, timeout=60) as conn:
        conn.row_factory = sqlite3.Row
        ensure_pilot_schema(conn)
        price_flags, price_quality = audit_price_quality(conn, market_data)
        plan_ids = _plan_and_assets(conn, events, event_assets, market_data["contracts"])
        label_ids: list[int] = []
        # Exact-time intraday and event-to-first-regular-close labels.
        for window_row in market_data["windows"]:
            macro_event_id = int(window_row["macro_event_id"])
            event = events[macro_event_id]
            symbol = str(window_row["symbol"])
            anchor = parse_utc(str(window_row["anchor_at_utc"]))
            bars = market_data["intraday"][symbol]
            entry = _first_at_or_after(bars, anchor)
            entry_delay = int((entry.at - anchor).total_seconds()) if entry else None
            intraday_profile = market_data["profiles"][(symbol, "intraday")]["provenance_key"]
            for window, minutes in INTRADAY_WINDOWS.items():
                target_exit = anchor + timedelta(minutes=minutes)
                exit_bar = _first_at_or_after(bars, target_exit)
                exit_delay = int((exit_bar.at - target_exit).total_seconds()) if exit_bar else None
                observed = _bars_between(bars, entry.at, exit_bar.at) if entry and exit_bar else []
                expected_count = minutes + 1
                coverage = min(1.0, len(observed) / expected_count) if observed else 0.0
                flags = _flag_values(price_flags, "intraday", symbol, observed)
                base = _base_label(
                    event=event,
                    plan_id=plan_ids[macro_event_id],
                    symbol=symbol,
                    window=window,
                    profile_id=str(intraday_profile),
                    start_at=entry.at.isoformat() if entry else anchor.isoformat(),
                    end_at=exit_bar.at.isoformat() if exit_bar else target_exit.isoformat(),
                    session_bucket=_session_bucket(anchor),
                )
                exclusion = ""
                if entry is None or exit_bar is None:
                    exclusion = "missing_entry_or_exit_trade"
                elif entry_delay is None or exit_delay is None or entry_delay > max_trade_delay_seconds or exit_delay > max_trade_delay_seconds:
                    exclusion = "trade_delay_exceeds_300s"
                elif any(flags.values()):
                    exclusion = "price_quality_flag"
                elif coverage < 0.20:
                    exclusion = "insufficient_observed_trade_coverage"
                raw_return = ((exit_bar.close / entry.close) - 1.0) if entry and exit_bar and entry.close > 0 else None
                closes = [bar.close for bar in observed] if entry else []
                base.update(
                    {
                        "raw_return": raw_return,
                        "total_return_adjusted": raw_return,
                        "mfe": max((value / entry.close) - 1.0 for value in closes) if closes and entry else None,
                        "mae": min((value / entry.close) - 1.0 for value in closes) if closes and entry else None,
                        "entry_trade_observed": int(entry is not None and entry.at == anchor),
                        "exit_trade_observed": int(exit_bar is not None and exit_bar.at == target_exit),
                        "entry_delay_seconds": entry_delay,
                        "exit_delay_seconds": exit_delay,
                        "observed_trade_count": sum(bar.trade_count for bar in observed),
                        "observed_bar_count": len(observed),
                        "window_trade_coverage_ratio": coverage,
                        "no_trade_minute_count": max(0, expected_count - len(observed)),
                        "eth_sparse_trade_flag": int(_session_bucket(anchor) != "regular" and coverage < 0.80),
                        **flags,
                        "price_quality_severity": _price_quality_severity(flags),
                        "label_quality_flag": "excluded" if exclusion else ("sparse_observed_trades" if coverage < 0.80 else "pass"),
                        "label_quality_reason": exclusion or "observed_trades_only",
                        "label_exclusion_reason": exclusion,
                        "dividend_contaminated": 0,
                        "dividend_amount_in_window": 0.0,
                        "split_contaminated": 0,
                        "corporate_action_version": action_version,
                        "strict_training_eligible": int(not exclusion and coverage >= 0.80),
                        "horizon_anchor_policy": "wall_clock_from_model_visible_at_observed_trades_only",
                        "first_regular_session_date": _first_regular_session(anchor, schedule),
                        "entry_session_date": entry.session_date if entry else "",
                        "exit_session_date": exit_bar.session_date if exit_bar else "",
                    }
                )
                label_ids.append(_upsert_label(conn, base))

            first_session = _first_regular_session(anchor, schedule)
            daily_dates, daily_map = _daily_by_session(market_data["daily"][symbol])
            close_bar = daily_map.get(first_session)
            observed_to_close = (
                _bars_between(bars, entry.at, schedule[date.fromisoformat(first_session)][1])
                if entry and close_bar and first_session
                else []
            )
            flags = _flag_values(price_flags, "intraday", symbol, observed_to_close)
            if close_bar:
                close_flags = _flag_values(price_flags, "daily", symbol, [close_bar])
                flags = {name: max(flags[name], close_flags[name]) for name in flags}
            dividend, dividend_flag, split_flag = _actions_in_window(
                actions, symbol, entry.session_date if entry else anchor.date().isoformat(), first_session
            ) if first_session else (0.0, False, False)
            base = _base_label(
                event=event,
                plan_id=plan_ids[macro_event_id],
                symbol=symbol,
                window="first_regular_close",
                profile_id=json.dumps(
                    [intraday_profile, market_data["profiles"][(symbol, "daily")]["provenance_key"]],
                    separators=(",", ":"),
                ),
                start_at=entry.at.isoformat() if entry else anchor.isoformat(),
                end_at=schedule[date.fromisoformat(first_session)][1].isoformat() if first_session else "",
                session_bucket=_session_bucket(anchor),
            )
            exclusion = ""
            if entry is None or close_bar is None:
                exclusion = "missing_entry_or_first_regular_close"
            elif entry_delay is None or entry_delay > max_trade_delay_seconds:
                exclusion = "trade_delay_exceeds_300s"
            elif any(flags.values()) or split_flag:
                exclusion = "price_or_split_quality_flag"
            raw_return = ((close_bar.close / entry.close) - 1.0) if entry and close_bar and entry.close > 0 else None
            base.update(
                {
                    "raw_return": raw_return,
                    "total_return_adjusted": ((close_bar.close + dividend) / entry.close - 1.0) if entry and close_bar and entry.close > 0 else None,
                    "entry_trade_observed": int(entry is not None and entry.at == anchor),
                    "exit_trade_observed": int(close_bar is not None),
                    "entry_delay_seconds": entry_delay,
                    "exit_delay_seconds": 0 if close_bar else None,
                    "observed_trade_count": sum(bar.trade_count for bar in observed_to_close),
                    "observed_bar_count": len(observed_to_close),
                    "window_trade_coverage_ratio": 1.0 if close_bar and entry else 0.0,
                    "no_trade_minute_count": 0,
                    "eth_sparse_trade_flag": int(_session_bucket(anchor) != "regular" and entry_delay not in (None, 0)),
                    **flags,
                    "price_quality_severity": _price_quality_severity(flags),
                    "label_quality_flag": "excluded" if exclusion else "pass",
                    "label_quality_reason": exclusion or "observed_entry_and_regular_close",
                    "label_exclusion_reason": exclusion,
                    "dividend_contaminated": int(dividend_flag),
                    "dividend_amount_in_window": dividend,
                    "split_contaminated": int(split_flag),
                    "corporate_action_version": action_version,
                    "strict_training_eligible": int(
                        not exclusion
                        and not (_session_bucket(anchor) != "regular" and entry_delay not in (None, 0))
                    ),
                    "horizon_anchor_policy": "model_visible_at_to_first_regular_session_close",
                    "first_regular_session_date": first_session,
                    "entry_session_date": entry.session_date if entry else "",
                    "exit_session_date": first_session,
                }
            )
            label_ids.append(_upsert_label(conn, base))

        # Daily session labels for exact and date-only releases.
        for anchor_row in market_data["anchors"]:
            macro_event_id = int(anchor_row["macro_event_id"])
            event = events[macro_event_id]
            symbol = str(anchor_row["symbol"])
            daily_dates, daily_map = _daily_by_session(market_data["daily"][symbol])
            anchor = parse_utc(str(anchor_row["anchor_at_utc"]))
            first_session = _first_regular_session(anchor, schedule)
            if str(anchor_row["release_time_quality"]) == "official_date_only":
                candidates = [value for value in daily_dates if value >= str(anchor_row["anchor_date"])]
                first_session = candidates[0] if candidates else ""
            first_index = daily_dates.index(first_session) if first_session in daily_dates else -1
            entry = daily_map.get(first_session)
            daily_profile = market_data["profiles"][(symbol, "daily")]["provenance_key"]
            for window, offset in DAILY_WINDOWS.items():
                exit_session = daily_dates[first_index + offset] if first_index >= 0 and first_index + offset < len(daily_dates) else ""
                exit_bar = daily_map.get(exit_session)
                selected = [bar for bar in (entry, exit_bar) if bar]
                flags = _flag_values(price_flags, "daily", symbol, selected)
                dividend, dividend_flag, split_flag = _actions_in_window(
                    actions, symbol, first_session, exit_session
                ) if first_session and exit_session else (0.0, False, False)
                base = _base_label(
                    event=event,
                    plan_id=plan_ids[macro_event_id],
                    symbol=symbol,
                    window=window,
                    profile_id=str(daily_profile),
                    start_at=schedule[date.fromisoformat(first_session)][1].isoformat() if first_session else "",
                    end_at=schedule[date.fromisoformat(exit_session)][1].isoformat() if exit_session else "",
                    session_bucket="date_only" if not int(anchor_row["intraday_label_eligible"]) else _session_bucket(anchor),
                )
                exclusion = ""
                if entry is None or exit_bar is None:
                    exclusion = "missing_daily_entry_or_exit"
                elif any(flags.values()) or split_flag:
                    exclusion = "price_or_split_quality_flag"
                raw_return = ((exit_bar.close / entry.close) - 1.0) if entry and exit_bar and entry.close > 0 else None
                total_return = ((exit_bar.close + dividend) / entry.close - 1.0) if entry and exit_bar and entry.close > 0 else None
                base.update(
                    {
                        "raw_return": raw_return,
                        "total_return_adjusted": total_return,
                        "entry_trade_observed": int(entry is not None),
                        "exit_trade_observed": int(exit_bar is not None),
                        "entry_delay_seconds": 0 if entry else None,
                        "exit_delay_seconds": 0 if exit_bar else None,
                        "observed_trade_count": (entry.trade_count if entry else 0) + (exit_bar.trade_count if exit_bar else 0),
                        "observed_bar_count": len(selected),
                        "window_trade_coverage_ratio": 1.0 if entry and exit_bar else 0.0,
                        "no_trade_minute_count": 0,
                        "eth_sparse_trade_flag": 0,
                        **flags,
                        "price_quality_severity": _price_quality_severity(flags),
                        "label_quality_flag": "excluded" if exclusion else ("corporate_action_adjusted" if dividend_flag else "pass"),
                        "label_quality_reason": exclusion or ("total_return_includes_dividend" if dividend_flag else "strict_session_close"),
                        "label_exclusion_reason": exclusion,
                        "dividend_contaminated": int(dividend_flag),
                        "dividend_amount_in_window": dividend,
                        "split_contaminated": int(split_flag),
                        "corporate_action_version": action_version,
                        "strict_training_eligible": int(not exclusion and total_return is not None),
                        "horizon_anchor_policy": f"first_regular_close_plus_{offset}_XNYS_sessions",
                        "first_regular_session_date": first_session,
                        "entry_session_date": first_session,
                        "exit_session_date": exit_session,
                    }
                )
                label_ids.append(_upsert_label(conn, base))
        label_ids.extend(
            _build_nonzero_lag_labels(
                conn,
                market_data=market_data,
                events=events,
                plan_ids=plan_ids,
                price_flags=price_flags,
                actions=actions,
                action_version=action_version,
                schedule=schedule,
                max_trade_delay_seconds=max_trade_delay_seconds,
            )
        )
        label_math = populate_label_math(conn, market_data=market_data, events=events)
        selection_bias_policy = apply_selection_bias_policy(conn)
        config_hash = _stable_hash(
            {
                "pilot_version": PILOT_VERSION,
                "plan_version": PLAN_VERSION,
                "max_trade_delay_seconds": max_trade_delay_seconds,
                "windows": [*INTRADAY_WINDOWS, "first_regular_close", *DAILY_WINDOWS],
                "strict_decision_lags_seconds": list(STRICT_DECISION_LAGS_SECONDS),
                "lag_30_policy": "not_strict_not_generated_without_subminute_data",
                "return_math_version": RETURN_MATH_VERSION,
            }
        )
        now = _utc_now()
        conn.execute(
            """INSERT INTO s05_reaction_label_pilot_runs(
                   pilot_version,scope,source_db_sha256,cache_db_sha256,config_hash,first_run_at_utc,last_run_at_utc
               ) VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(pilot_version) DO UPDATE SET
                   source_db_sha256=excluded.source_db_sha256,
                   cache_db_sha256=excluded.cache_db_sha256,
                   config_hash=excluded.config_hash,
                   last_run_at_utc=excluded.last_run_at_utc""",
            (
                PILOT_VERSION,
                "112 canonical macro events / audited S04 symbols / lag 0 only",
                "clone_of_canonical_source",
                _sha256(cache_path),
                config_hash,
                now,
                now,
            ),
        )
        conn.commit()
        return {
            "pilot_version": PILOT_VERSION,
            "plans_touched": len(set(plan_ids.values())),
            "assets_touched": sum(len(values) for values in event_assets.values()),
            "labels_touched": len(set(label_ids)),
            "price_quality": price_quality,
            "label_math": label_math,
            "selection_bias_policy": selection_bias_policy,
            "config_hash": config_hash,
            "corporate_action_version": action_version,
        }


def _distribution(rows: list[int]) -> dict[str, Any]:
    values = [float(value) for value in rows]
    return {
        "count": len(values),
        "p50": _percentile(values, 0.50),
        "p90": _percentile(values, 0.90),
        "p99": _percentile(values, 0.99),
        "max": max(values) if values else None,
    }


def audit_pilot(
    clone_path: Path,
    first_counts: dict[str, int],
    second_counts: dict[str, int],
    *,
    supplemental_audit_path: Path | None = None,
    corporate_actions_path: Path | None = None,
) -> dict[str, Any]:
    with sqlite3.connect(clone_path) as conn:
        conn.row_factory = sqlite3.Row
        actual_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(event_reaction_labels)")}
        missing_contract_columns = sorted(set(REQUIRED_CONTRACT_COLUMNS) - actual_columns)
        required_metadata_missing = int(
            conn.execute(
                """SELECT COUNT(*) FROM event_reaction_labels
                    WHERE market_data_profile_id=''
                       OR price_quality_version=''
                       OR corporate_action_version=''
                       OR release_bundle_id=''
                       OR overlap_group_id=''
                       OR validation_split_group=''
                       OR horizon_anchor_policy=''
                       OR execution_price_model=''"""
            ).fetchone()[0]
        )
        label_counts = [dict(row) for row in conn.execute(
            "SELECT window,session_bucket,label_quality_flag,COUNT(*) count FROM event_reaction_labels GROUP BY 1,2,3 ORDER BY 1,2,3"
        )]
        exclusion_counts = [dict(row) for row in conn.execute(
            "SELECT label_exclusion_reason,COUNT(*) count FROM event_reaction_labels WHERE label_exclusion_reason<>'' GROUP BY 1 ORDER BY count DESC"
        )]
        math_fields = (
            "raw_return",
            "market_adjusted_return",
            "sector_adjusted_return",
            "proxy_spread_return",
            "residual_return",
            "return_zscore",
            "volume_zscore",
            "mfe",
            "mae",
        )
        math_coverage = {
            field: {
                "non_null": int(conn.execute(f"SELECT COUNT(*) FROM event_reaction_labels WHERE {field} IS NOT NULL").fetchone()[0]),
                "strict_missing": int(conn.execute(f"SELECT COUNT(*) FROM event_reaction_labels WHERE strict_training_eligible=1 AND {field} IS NULL").fetchone()[0]),
            }
            for field in math_fields
        }
        numeric_rows = [dict(row) for row in conn.execute(
            f"SELECT reaction_label_id,{','.join(math_fields)} FROM event_reaction_labels"
        )]
        nonfinite_math_values = sum(
            1
            for row in numeric_rows
            for field in math_fields
            if row[field] is not None and not math.isfinite(float(row[field]))
        )
        mfe_mae_invariant_violations = int(conn.execute(
            """SELECT COUNT(*) FROM event_reaction_labels
                WHERE strict_training_eligible=1 AND (
                       mfe IS NULL OR mae IS NULL OR raw_return IS NULL
                    OR mfe < -0.0000000001 OR mae > 0.0000000001
                    OR mfe + 0.0000000001 < raw_return
                    OR mae - 0.0000000001 > raw_return
                )"""
        ).fetchone()[0])
        estimation_leakage_rows = int(conn.execute(
            """SELECT COUNT(*)
                 FROM event_reaction_labels l
                 JOIN v_canonical_reaction_trigger_macro_events v
                   ON v.event_cluster_id=l.event_cluster_id
                WHERE l.strict_training_eligible=1
                  AND (l.estimation_window_end_at_utc=''
                       OR l.estimation_window_end_at_utc>=v.model_visible_at_utc)"""
        ).fetchone()[0])
        strict_math_missing = sum(item["strict_missing"] for item in math_coverage.values())
        decision_lag_counts = [dict(row) for row in conn.execute(
            "SELECT decision_lag_seconds,window,COUNT(*) count,SUM(strict_training_eligible) strict_count FROM event_reaction_labels GROUP BY 1,2 ORDER BY 1,2"
        )]
        lag_30_labels = int(conn.execute("SELECT COUNT(*) FROM event_reaction_labels WHERE decision_lag_seconds=30").fetchone()[0])
        cluster_anchor = {
            int(row["event_cluster_id"]): parse_utc(str(row["model_visible_at_utc"]))
            for row in conn.execute("SELECT event_cluster_id,model_visible_at_utc FROM v_canonical_reaction_trigger_macro_events")
        }
        lag_alignment_violations = 0
        for row in conn.execute(
            "SELECT event_cluster_id,decision_lag_seconds,start_at_utc,window FROM event_reaction_labels WHERE window IN ('5m','15m','1h','first_regular_close')"
        ):
            target = cluster_anchor[int(row["event_cluster_id"])] + timedelta(seconds=int(row["decision_lag_seconds"] or 0))
            if not row["start_at_utc"] or parse_utc(str(row["start_at_utc"])) < target:
                lag_alignment_violations += 1
        selection_bias = {
            "symbol_window_session_strict": [dict(row) for row in conn.execute(
                "SELECT asset_symbol,window,session_bucket,strict_training_eligible,COUNT(*) count FROM event_reaction_labels GROUP BY 1,2,3,4 ORDER BY 1,2,3,4"
            )],
            "central_bank_event_window_exclusion": [dict(row) for row in conn.execute(
                """SELECT v.source_system central_bank,v.event_type,l.window,l.label_exclusion_reason,COUNT(*) count
                     FROM event_reaction_labels l
                     JOIN v_canonical_reaction_trigger_macro_events v ON v.event_cluster_id=l.event_cluster_id
                    GROUP BY 1,2,3,4 ORDER BY 1,2,3,4"""
            )],
            "asset_role_window_exclusion": [dict(row) for row in conn.execute(
                "SELECT asset_role,window,label_exclusion_reason,COUNT(*) count FROM event_reaction_labels GROUP BY 1,2,3 ORDER BY 1,2,3"
            )],
            "proxy_family_window_strict": [dict(row) for row in conn.execute(
                "SELECT proxy_family,window,strict_training_eligible,COUNT(*) count FROM event_reaction_labels GROUP BY 1,2,3 ORDER BY 1,2,3"
            )],
            "exclusion_reason_by_window": [dict(row) for row in conn.execute(
                "SELECT label_exclusion_reason,window,COUNT(*) count FROM event_reaction_labels WHERE label_exclusion_reason<>'' GROUP BY 1,2 ORDER BY 1,2"
            )],
        }
        bad_tick_severity = [dict(row) for row in conn.execute(
            """SELECT price_quality_severity,strict_training_eligible,window,asset_symbol,
                      SUM(bad_tick_flag) bad_tick_count,SUM(extreme_return_flag) extreme_return_count,
                      SUM(stale_trade_flag) stale_trade_count,SUM(isolated_spike_flag) isolated_spike_count,
                      COUNT(*) label_count
                 FROM event_reaction_labels
                WHERE price_quality_severity<>'none'
                GROUP BY 1,2,3,4 ORDER BY 1,2,3,4"""
        )]
        critical_strict_labels = int(conn.execute(
            "SELECT COUNT(*) FROM event_reaction_labels WHERE price_quality_severity='critical' AND strict_training_eligible=1"
        ).fetchone()[0])
        isolated_spike_strict_labels = int(conn.execute(
            "SELECT COUNT(*) FROM event_reaction_labels WHERE isolated_spike_flag=1 AND strict_training_eligible=1"
        ).fetchone()[0])
        low_coverage_policy_strict_leaks = int(conn.execute(
            """SELECT COUNT(*) FROM event_reaction_labels
                WHERE strict_training_eligible=1
                  AND (asset_symbol,window) IN (
                      SELECT asset_symbol,window FROM event_reaction_labels
                       WHERE label_exclusion_reason='low_symbol_window_strict_coverage_policy'
                  )"""
        ).fetchone()[0])
        dividend_count = int(conn.execute("SELECT COUNT(*) FROM event_reaction_labels WHERE dividend_contaminated=1").fetchone()[0])
        dividend_lag0_count = int(conn.execute("SELECT COUNT(*) FROM event_reaction_labels WHERE dividend_contaminated=1 AND decision_lag_seconds=0").fetchone()[0])
        dividend_unique_outcomes = int(conn.execute(
            """SELECT COUNT(*) FROM (SELECT DISTINCT event_cluster_id,asset_symbol,window
                 FROM event_reaction_labels WHERE dividend_contaminated=1)"""
        ).fetchone()[0])
        sparse_count = int(conn.execute("SELECT COUNT(*) FROM event_reaction_labels WHERE eth_sparse_trade_flag=1").fetchone()[0])
        duplicate_plans = int(conn.execute("SELECT COUNT(*) FROM (SELECT event_cluster_id,plan_version,COUNT(*) n FROM reaction_universe_plans GROUP BY 1,2 HAVING n>1)").fetchone()[0])
        duplicate_assets = int(conn.execute("SELECT COUNT(*) FROM (SELECT reaction_plan_id,asset_symbol,asset_role,label_scope,proxy_family,COUNT(*) n FROM reaction_universe_assets GROUP BY 1,2,3,4,5 HAVING n>1)").fetchone()[0])
        duplicate_labels = int(conn.execute("SELECT COUNT(*) FROM (SELECT reaction_plan_id,asset_symbol,asset_role,proxy_family,window,decision_lag_seconds,COUNT(*) n FROM event_reaction_labels GROUP BY 1,2,3,4,5,6 HAVING n>1)").fetchone()[0])
        date_only_intraday = int(conn.execute("SELECT COUNT(*) FROM event_reaction_labels WHERE release_time_quality='official_date_only' AND window IN ('5m','15m','1h','first_regular_close')").fetchone()[0])
        bundle_weight_violations = int(conn.execute(
            """SELECT COUNT(*) FROM (
                   SELECT release_bundle_id,asset_symbol,window,decision_lag_seconds,SUM(sample_weight) total_weight
                     FROM event_reaction_labels
                    GROUP BY 1,2,3,4 HAVING total_weight>1.0000001
               )"""
        ).fetchone()[0])
        split_group_mismatches = int(conn.execute(
            "SELECT COUNT(*) FROM event_reaction_labels WHERE validation_split_group<>overlap_group_id"
        ).fetchone()[0])
        invalid_sample_weights = int(conn.execute(
            "SELECT COUNT(*) FROM event_reaction_labels WHERE sample_weight<=0 OR sample_weight>1"
        ).fetchone()[0])
        corporate_contract_failures = int(conn.execute(
            """SELECT COUNT(*) FROM event_reaction_labels
                WHERE dividend_contaminated=1
                  AND (total_return_adjusted IS NULL OR corporate_action_version='')"""
        ).fetchone()[0])
        after_cost_misuse = int(conn.execute(
            """SELECT COUNT(*) FROM event_reaction_labels
                WHERE execution_price_model NOT LIKE '%not_executable%'
                   OR cost_estimate_quality<>'not_available_reaction_only'"""
        ).fetchone()[0])
        excluded_but_eligible = int(conn.execute(
            "SELECT COUNT(*) FROM event_reaction_labels WHERE label_exclusion_reason<>'' AND strict_training_eligible=1"
        ).fetchone()[0])
        sparse_but_strict = int(conn.execute(
            "SELECT COUNT(*) FROM event_reaction_labels WHERE eth_sparse_trade_flag=1 AND strict_training_eligible=1"
        ).fetchone()[0])
        entry_delays = [int(row[0]) for row in conn.execute("SELECT entry_delay_seconds FROM event_reaction_labels WHERE entry_delay_seconds IS NOT NULL")]
        exit_delays = [int(row[0]) for row in conn.execute("SELECT exit_delay_seconds FROM event_reaction_labels WHERE exit_delay_seconds IS NOT NULL")]
        eligible_entry_delays = [int(row[0]) for row in conn.execute("SELECT entry_delay_seconds FROM event_reaction_labels WHERE strict_training_eligible=1 AND entry_delay_seconds IS NOT NULL")]
        eligible_exit_delays = [int(row[0]) for row in conn.execute("SELECT exit_delay_seconds FROM event_reaction_labels WHERE strict_training_eligible=1 AND exit_delay_seconds IS NOT NULL")]
        effective_sample_rows = [dict(row) for row in conn.execute(
            """SELECT window,asset_symbol,SUM(sample_weight) effective_sample_count
                 FROM event_reaction_labels GROUP BY 1,2 ORDER BY 1,2"""
        )]
        independent_sample_accounting = {
            "raw_event_count": int(conn.execute("SELECT COUNT(DISTINCT event_cluster_id) FROM event_reaction_labels").fetchone()[0]),
            "release_bundle_count": int(conn.execute("SELECT COUNT(DISTINCT release_bundle_id) FROM event_reaction_labels").fetchone()[0]),
            "overlap_group_count": int(conn.execute("SELECT COUNT(DISTINCT overlap_group_id) FROM event_reaction_labels").fetchone()[0]),
            "strict_label_rows": int(conn.execute("SELECT COUNT(*) FROM event_reaction_labels WHERE strict_training_eligible=1").fetchone()[0]),
            "effective_weight_by_window": [dict(row) for row in conn.execute(
                "SELECT window,SUM(sample_weight) effective_weight FROM event_reaction_labels WHERE strict_training_eligible=1 GROUP BY 1 ORDER BY 1"
            )],
        }
        counts = {
            "reaction_universe_plans": int(conn.execute("SELECT COUNT(*) FROM reaction_universe_plans").fetchone()[0]),
            "reaction_universe_assets": int(conn.execute("SELECT COUNT(*) FROM reaction_universe_assets").fetchone()[0]),
            "event_reaction_labels": int(conn.execute("SELECT COUNT(*) FROM event_reaction_labels").fetchone()[0]),
            "strict_training_eligible": int(conn.execute("SELECT COUNT(*) FROM event_reaction_labels WHERE strict_training_eligible=1").fetchone()[0]),
            "excluded": int(conn.execute("SELECT COUNT(*) FROM event_reaction_labels WHERE label_exclusion_reason<>''").fetchone()[0]),
            "dividend_contaminated": dividend_count,
            "dividend_contaminated_lag_expanded_label_rows": dividend_count,
            "dividend_contaminated_lag0_label_rows": dividend_lag0_count,
            "dividend_contaminated_unique_event_asset_windows": dividend_unique_outcomes,
            "sparse_eth": sparse_count,
            "bad_tick_flagged_labels": int(conn.execute("SELECT COUNT(*) FROM event_reaction_labels WHERE bad_tick_flag=1 OR extreme_return_flag=1 OR stale_trade_flag=1 OR isolated_spike_flag=1").fetchone()[0]),
        }
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = len(conn.execute("PRAGMA foreign_key_check").fetchall())
    rerun_diff = {key: second_counts.get(key, 0) - first_counts.get(key, 0) for key in sorted(set(first_counts) | set(second_counts))}
    corporate_reconciliation: dict[str, Any] = {}
    if supplemental_audit_path and supplemental_audit_path.exists():
        supplemental = json.loads(supplemental_audit_path.read_text(encoding="utf-8"))
        s04_keys: set[tuple[int, str, str]] = set()
        s04_details: dict[tuple[int, str, str], dict[str, Any]] = {}
        for item in supplemental["corporate_actions"]["flags"]:
            if item.get("dividend_contaminated_1d"):
                key = (int(item["macro_event_id"]), str(item["symbol"]), "t_plus_1_session_close")
                s04_keys.add(key)
                s04_details[key] = item
            if item.get("dividend_contaminated_5d"):
                key = (int(item["macro_event_id"]), str(item["symbol"]), "t_plus_5_session_close")
                s04_keys.add(key)
                s04_details[key] = item
        with sqlite3.connect(clone_path) as conn:
            conn.row_factory = sqlite3.Row
            s05_rows = [dict(row) for row in conn.execute(
                """SELECT v.macro_event_id,l.asset_symbol,l.window,l.first_regular_session_date,
                          l.exit_session_date,l.dividend_amount_in_window,l.raw_return,l.total_return_adjusted
                     FROM event_reaction_labels l
                     JOIN v_canonical_reaction_trigger_macro_events v ON v.event_cluster_id=l.event_cluster_id
                    WHERE l.dividend_contaminated=1 AND l.decision_lag_seconds=0"""
            )]
        s05_daily_keys = {
            (int(row["macro_event_id"]), str(row["asset_symbol"]), str(row["window"]))
            for row in s05_rows
            if row["window"] in DAILY_WINDOWS
        }
        missing = sorted(s04_keys - s05_daily_keys)
        explanations = []
        for key in missing:
            details = s04_details[key]
            action_field = "horizon_1d_actions" if key[2] == "t_plus_1_session_close" else "horizon_5d_actions"
            action_dates = sorted(str(action.get("action_date") or "") for action in details.get(action_field) or [])
            explanations.append({"key": list(key), "action_dates": action_dates, "reason": "outside_first_close_to_exit_label_window"})
        corporate_reconciliation = {
            "s04_candidate_count": len(s04_keys),
            "s05_lag0_dividend_label_count": len(s05_rows),
            "s05_lag0_daily_dividend_label_count": len(s05_daily_keys),
            "s05_lag0_first_regular_close_dividend_count": sum(
                1 for row in s05_rows if row["window"] == "first_regular_close"
            ),
            "aggregate_difference_count": len(s04_keys) - len(s05_rows),
            "daily_key_difference_count": len(missing),
            "missing_from_s05": explanations,
            "extra_in_s05_daily": [list(key) for key in sorted(s05_daily_keys - s04_keys)],
            "difference_explanation": (
                "S04 counted anchor-date next-session candidates; S05 begins daily returns at the first regular close. "
                "Lag-0 first_regular_close labels absorb four same-window dividends, yielding the documented 319->317 net difference."
            ),
            "raw_and_total_return_present": all(row["raw_return"] is not None and row["total_return_adjusted"] is not None for row in s05_rows),
        }
    duplicate_corporate_actions = 0
    if corporate_actions_path and corporate_actions_path.exists():
        snapshot = json.loads(corporate_actions_path.read_text(encoding="utf-8"))
        for payload in (snapshot.get("symbols") or {}).values():
            ids = [str(action.get("event_id")) for action in payload.get("actions") or []]
            duplicate_corporate_actions += len(ids) - len(set(ids))
        corporate_reconciliation["duplicate_source_action_ids"] = duplicate_corporate_actions
    checks = {
        "required_contract_columns_present": not missing_contract_columns,
        "required_metadata_missing_zero": required_metadata_missing == 0,
        "duplicate_plan_keys_zero": duplicate_plans == 0,
        "duplicate_asset_keys_zero": duplicate_assets == 0,
        "duplicate_label_keys_zero": duplicate_labels == 0,
        "date_only_intraday_zero": date_only_intraday == 0,
        "bundle_weight_violations_zero": bundle_weight_violations == 0,
        "validation_split_group_matches_overlap_group": split_group_mismatches == 0,
        "sample_weights_valid": invalid_sample_weights == 0,
        "strict_label_math_complete": strict_math_missing == 0,
        "label_math_nonfinite_zero": nonfinite_math_values == 0,
        "mfe_mae_invariants_hold": mfe_mae_invariant_violations == 0,
        "pre_event_estimation_no_leakage": estimation_leakage_rows == 0,
        "decision_lag_alignment_valid": lag_alignment_violations == 0,
        "lag_30_not_strictly_generated": lag_30_labels == 0,
        "critical_bad_tick_strict_zero": critical_strict_labels == 0 and isolated_spike_strict_labels == 0,
        "low_coverage_symbol_windows_disabled": low_coverage_policy_strict_leaks == 0,
        "corporate_action_contract_failures_zero": corporate_contract_failures == 0,
        "after_cost_misuse_zero": after_cost_misuse == 0,
        "excluded_labels_not_strict": excluded_but_eligible == 0,
        "sparse_eth_labels_not_strict": sparse_but_strict == 0,
        "clone_rerun_count_diff_zero": all(value == 0 for value in rerun_diff.values()),
        "integrity_ok": integrity == "ok" and foreign_keys == 0,
    }
    if corporate_reconciliation:
        checks["corporate_action_reconciliation_explained"] = (
            corporate_reconciliation.get("s04_candidate_count") == 319
            and corporate_reconciliation.get("s05_lag0_dividend_label_count") == 317
            and corporate_reconciliation.get("aggregate_difference_count") == 2
            and corporate_reconciliation.get("raw_and_total_return_present")
            and corporate_reconciliation.get("duplicate_source_action_ids", 0) == 0
        )
    return {
        "verdict": "PASS" if all(checks.values()) else "FAIL",
        "checks": checks,
        "counts": counts,
        "labels_by_window_session_quality": label_counts,
        "label_math_coverage": math_coverage,
        "nonfinite_math_values": nonfinite_math_values,
        "mfe_mae_invariant_violations": mfe_mae_invariant_violations,
        "estimation_leakage_rows": estimation_leakage_rows,
        "decision_lag_counts": decision_lag_counts,
        "lag_alignment_violations": lag_alignment_violations,
        "selection_bias": selection_bias,
        "bad_tick_severity": bad_tick_severity,
        "critical_strict_labels": critical_strict_labels,
        "isolated_spike_strict_labels": isolated_spike_strict_labels,
        "low_coverage_policy_strict_leaks": low_coverage_policy_strict_leaks,
        "corporate_action_reconciliation": corporate_reconciliation,
        "independent_sample_accounting": independent_sample_accounting,
        "excluded_label_reasons": exclusion_counts,
        "entry_delay_seconds": _distribution(entry_delays),
        "exit_delay_seconds": _distribution(exit_delays),
        "strict_eligible_entry_delay_seconds": _distribution(eligible_entry_delays),
        "strict_eligible_exit_delay_seconds": _distribution(eligible_exit_delays),
        "overlap_weighted_effective_samples": effective_sample_rows,
        "duplicate_keys": {"plans": duplicate_plans, "assets": duplicate_assets, "labels": duplicate_labels},
        "missing_contract_columns": missing_contract_columns,
        "required_metadata_missing_count": required_metadata_missing,
        "bundle_weight_violations": bundle_weight_violations,
        "validation_split_group_mismatches": split_group_mismatches,
        "invalid_sample_weights": invalid_sample_weights,
        "sparse_eth_strict_training_rows": sparse_but_strict,
        "date_only_intraday_labels": date_only_intraday,
        "clone_rerun_count_diff": rerun_diff,
        "integrity_check": integrity,
        "foreign_key_violations": foreign_keys,
    }


def canonical_counts(path: Path) -> dict[str, int]:
    with sqlite3.connect(path) as conn:
        return {
            table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in (
                "reaction_universe_plans",
                "reaction_universe_assets",
                "event_reaction_labels",
                "s05_market_data_price_quality_flags",
                "s05_reaction_label_pilot_runs",
            )
        }


def markdown(report: dict[str, Any]) -> str:
    audit = report["pilot_audit"]
    lines = [
        "# S05/M253 Macro Reaction-Label Pilot",
        "",
        f"- Verdict: **{audit['verdict']}**",
        f"- Scope: {report['scope']}",
        f"- Clone DB: `{report['clone_database']}`",
        f"- Full S05/M255/M256 executed: **no**",
        "",
        "## Counts",
        "",
    ]
    for key, value in audit["counts"].items():
        lines.append(f"- {key}: `{value}`")
    lines.extend(["", "## Hard gates", ""])
    for key, value in audit["checks"].items():
        lines.append(f"- {key}: `{'PASS' if value else 'FAIL'}`")
    lines.extend(
        [
            "",
            "## Delay distributions",
            "",
            f"- Entry: `{json.dumps(audit['entry_delay_seconds'], sort_keys=True)}`",
            f"- Exit: `{json.dumps(audit['exit_delay_seconds'], sort_keys=True)}`",
            "",
            "## Exclusions",
            "",
        ]
    )
    if audit["excluded_label_reasons"]:
        for row in audit["excluded_label_reasons"]:
            lines.append(f"- {row['label_exclusion_reason']}: `{row['count']}`")
    else:
        lines.append("- None")
    lines.extend(
        [
            "",
            "## Safety",
            "",
            "- Clone-only research labels; canonical source DB is unchanged.",
            "- Missing ETH TRADES bars were not synthesized or forward-filled.",
            "- MFE/MAE use observed TRADES bars only.",
            "- TRADES prices are explicitly not treated as after-cost executable prices.",
            "- No broker order, what-if, transmit, live routing, threshold promotion, M255, or M256 action.",
            "",
        ]
    )
    return "\n".join(lines)
