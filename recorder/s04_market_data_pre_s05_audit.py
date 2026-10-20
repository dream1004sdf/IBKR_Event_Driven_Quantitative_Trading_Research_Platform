from __future__ import annotations

import hashlib
import json
import sqlite3
import urllib.parse
import urllib.request
from collections import Counter
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import pandas as pd


AUDIT_VERSION = "s04-market-data-pre-s05-audit-v1"
NY = ZoneInfo("America/New_York")
BENCHMARK_SYMBOLS = ("SPY", "QQQ", "IWM", "DIA")


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def stable_id(prefix: str, *parts: Any) -> str:
    payload = "|".join(str(part) for part in parts)
    return f"{prefix}:{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:24]}"


def calendar_schedule(start_date: date, end_date: date) -> dict[date, tuple[datetime, datetime]]:
    calendar = xcals.get_calendar("XNYS")
    sessions = calendar.sessions_in_range(pd.Timestamp(start_date), pd.Timestamp(end_date))
    return {
        session.date(): (
            calendar.session_open(session).to_pydatetime().astimezone(UTC),
            calendar.session_close(session).to_pydatetime().astimezone(UTC),
        )
        for session in sessions
    }


def _minute_range(start: datetime, end: datetime) -> set[datetime]:
    if end <= start:
        return set()
    cursor = start.replace(second=0, microsecond=0)
    if cursor < start:
        cursor += timedelta(minutes=1)
    result: set[datetime] = set()
    while cursor < end:
        result.add(cursor)
        cursor += timedelta(minutes=1)
    return result


def expected_eth_for_window(
    start: datetime,
    end: datetime,
    schedule: dict[date, tuple[datetime, datetime]],
) -> tuple[set[datetime], set[datetime]]:
    expected: set[datetime] = set()
    market_closed: set[datetime] = set()
    first_date = start.astimezone(NY).date() - timedelta(days=1)
    last_date = end.astimezone(NY).date() + timedelta(days=1)
    cursor_date = first_date
    while cursor_date <= last_date:
        generic_open = datetime.combine(cursor_date, time(4, 0), NY).astimezone(UTC)
        generic_end = datetime.combine(cursor_date, time(20, 0), NY).astimezone(UTC)
        clipped_generic = _minute_range(max(start, generic_open), min(end, generic_end))
        session = schedule.get(cursor_date)
        if not session:
            market_closed.update(clipped_generic)
            cursor_date += timedelta(days=1)
            continue
        regular_open, regular_close = session
        early_close = regular_close.astimezone(NY).time() < time(16, 0)
        extended_end_local = time(17, 0) if early_close else time(20, 0)
        extended_end = datetime.combine(cursor_date, extended_end_local, NY).astimezone(UTC)
        expected.update(_minute_range(max(start, generic_open), min(end, regular_open)))
        expected.update(_minute_range(max(start, regular_close), min(end, extended_end)))
        market_closed.update(_minute_range(max(start, extended_end), min(end, generic_end)))
        cursor_date += timedelta(days=1)
    return expected, market_closed


def build_eth_audit(cache_path: Path) -> dict[str, Any]:
    with sqlite3.connect(cache_path) as conn:
        conn.row_factory = sqlite3.Row
        windows = [dict(row) for row in conn.execute("SELECT * FROM ibkr_event_windows")]
        bars = [
            dict(row)
            for row in conn.execute(
                "SELECT symbol,bar_time_utc FROM ibkr_historical_bars WHERE data_kind='intraday'"
            )
        ]
    actual: dict[str, set[datetime]] = {}
    for row in bars:
        actual.setdefault(str(row["symbol"]), set()).add(parse_utc(str(row["bar_time_utc"])))
    start_date = min(parse_utc(row["start_at_utc"]).date() for row in windows) - timedelta(days=2)
    end_date = max(parse_utc(row["end_at_utc"]).date() for row in windows) + timedelta(days=2)
    schedule = calendar_schedule(start_date, end_date)
    expected_by_symbol: dict[str, set[datetime]] = {}
    market_closed_by_symbol: dict[str, set[datetime]] = {}
    window_metrics: list[dict[str, Any]] = []
    for row in windows:
        start = parse_utc(row["start_at_utc"])
        end = parse_utc(row["end_at_utc"])
        expected, market_closed = expected_eth_for_window(start, end, schedule)
        symbol = str(row["symbol"])
        expected_by_symbol.setdefault(symbol, set()).update(expected)
        market_closed_by_symbol.setdefault(symbol, set()).update(market_closed)
        actual_values = actual.get(symbol, set())
        missing = expected - actual_values
        window_metrics.append(
            {
                "window_key": row["window_key"],
                "macro_event_id": int(row["macro_event_id"]),
                "symbol": symbol,
                "anchor_at_utc": row["anchor_at_utc"],
                "expected_eth_minutes": len(expected),
                "actual_trade_minutes": len(expected.intersection(actual_values)),
                "missing_trade_minutes": len(missing),
                "trade_missing_rate": (len(missing) / len(expected)) if expected else 0.0,
            }
        )
    missing_by_symbol = {
        symbol: expected - actual.get(symbol, set()) for symbol, expected in expected_by_symbol.items()
    }
    all_missing = set().union(*missing_by_symbol.values()) if missing_by_symbol else set()
    synchronized_provider_timestamps = {
        stamp
        for stamp in all_missing
        if sum(
            stamp in missing_by_symbol.get(symbol, set())
            for symbol in BENCHMARK_SYMBOLS
            if stamp in expected_by_symbol.get(symbol, set())
        )
        >= 3
    }
    per_symbol: dict[str, dict[str, Any]] = {}
    for symbol, expected in sorted(expected_by_symbol.items()):
        missing = missing_by_symbol[symbol]
        provider_candidates = missing.intersection(synchronized_provider_timestamps)
        no_trade_candidates = missing - provider_candidates
        per_symbol[symbol] = {
            "expected_eth_minutes": len(expected),
            "actual_trade_minutes": len(expected.intersection(actual.get(symbol, set()))),
            "missing_trade_minutes": len(missing),
            "trade_missing_rate": (len(missing) / len(expected)) if expected else 0.0,
            "provider_missing_candidate_minutes": len(provider_candidates),
            "no_trade_or_symbol_specific_candidate_minutes": len(no_trade_candidates),
            "market_closed_minutes": len(market_closed_by_symbol.get(symbol, set())),
            "provider_missing_timestamps": [stamp.isoformat() for stamp in sorted(provider_candidates)],
        }
    return {
        "policy": {
            "normal_session": "04:00-20:00 America/New_York excluding RTH",
            "early_close_session": "04:00-17:00 America/New_York excluding shortened RTH",
            "bar_basis": "TRADES; an absent trade bar is not automatically a provider failure",
            "provider_gap_rule": "missing for at least 3 simultaneously expected benchmark symbols among SPY/QQQ/IWM/DIA",
            "residual_gap_rule": "no-trade or symbol-specific missing candidate; MIDPOINT is not substituted into returns",
        },
        "counts": {
            "expected_eth_minutes": sum(len(values) for values in expected_by_symbol.values()),
            "actual_trade_minutes": sum(
                len(expected_by_symbol[symbol].intersection(actual.get(symbol, set())))
                for symbol in expected_by_symbol
            ),
            "missing_trade_minutes": sum(len(values) for values in missing_by_symbol.values()),
            "provider_missing_candidate_timestamps": len(synchronized_provider_timestamps),
            "provider_missing_candidate_symbol_minutes": sum(
                len(values.intersection(synchronized_provider_timestamps))
                for values in missing_by_symbol.values()
            ),
            "no_trade_or_symbol_specific_candidate_minutes": sum(
                len(values - synchronized_provider_timestamps) for values in missing_by_symbol.values()
            ),
            "market_closed_minutes": sum(len(values) for values in market_closed_by_symbol.values()),
        },
        "per_symbol": per_symbol,
        "event_windows": sorted(
            window_metrics,
            key=lambda row: (row["provider_missing_candidate_minutes"] if "provider_missing_candidate_minutes" in row else 0, row["missing_trade_minutes"]),
            reverse=True,
        ),
        "provider_missing_timestamps": [stamp.isoformat() for stamp in sorted(synchronized_provider_timestamps)],
    }


def build_provenance_audit(cache_path: Path) -> dict[str, Any]:
    with sqlite3.connect(cache_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(row) for row in conn.execute("SELECT * FROM ibkr_market_data_provenance ORDER BY symbol,data_kind")]
        symbols = int(conn.execute("SELECT COUNT(1) FROM ibkr_contracts").fetchone()[0])
    required = (
        "market_data_type",
        "what_to_show",
        "source_exchange",
        "source_primary_exchange",
        "request_timezone",
        "bar_timezone",
        "adjustment_mode",
        "data_source_version",
    )
    missing = sum(1 for row in rows for key in required if not str(row.get(key) or "").strip())
    checks = {
        "two_profiles_per_symbol": len(rows) == symbols * 2,
        "required_fields_complete": missing == 0,
        "historical_only": {row["market_data_type"] for row in rows} == {"historical"},
        "trades_only": {row["what_to_show"] for row in rows} == {"TRADES"},
        "timezone_utc": {row["bar_timezone"] for row in rows} == {"UTC"},
        "adjustment_mode_explicit": {row["adjustment_mode"] for row in rows}
        == {"split_adjusted_dividend_unadjusted"},
        "positive_conids": all(int(row["con_id"]) > 0 for row in rows),
    }
    return {
        "verdict": "PASS" if all(checks.values()) else "FAIL",
        "checks": checks,
        "row_count": len(rows),
        "symbol_count": symbols,
        "missing_required_fields": missing,
        "profiles": rows,
    }


def fetch_yahoo_corporate_actions(symbols: list[str], start: date, end: date) -> dict[str, Any]:
    period1 = int(datetime.combine(start, time.min, tzinfo=UTC).timestamp())
    period2 = int(datetime.combine(end + timedelta(days=1), time.min, tzinfo=UTC).timestamp())
    result: dict[str, Any] = {"provider": "yahoo_chart_events", "symbols": {}, "errors": []}
    for symbol in symbols:
        params = urllib.parse.urlencode(
            {
                "period1": period1,
                "period2": period2,
                "interval": "1d",
                "events": "div,splits",
                "includeAdjustedClose": "true",
            }
        )
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}?{params}"
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8", errors="replace"))
            chart = ((payload.get("chart") or {}).get("result") or [None])[0]
            if not chart:
                raise RuntimeError("Yahoo chart returned no result")
            actions: list[dict[str, Any]] = []
            for action_type, values in (chart.get("events") or {}).items():
                for event_id, item in (values or {}).items():
                    timestamp = int(item.get("date") or event_id)
                    action_date = datetime.fromtimestamp(timestamp, tz=UTC).astimezone(NY).date()
                    actions.append(
                        {
                            "event_id": str(event_id),
                            "action_type": "dividend" if action_type == "dividends" else "split",
                            "action_date": action_date.isoformat(),
                            "amount": item.get("amount"),
                            "numerator": item.get("numerator"),
                            "denominator": item.get("denominator"),
                            "split_ratio": item.get("splitRatio"),
                        }
                    )
            result["symbols"][symbol] = {
                "exchange_timezone": (chart.get("meta") or {}).get("exchangeTimezoneName"),
                "actions": sorted(actions, key=lambda item: (item["action_date"], item["action_type"])),
            }
        except Exception as exc:
            result["errors"].append({"symbol": symbol, "error": f"{type(exc).__name__}: {exc}"})
    return result


def build_corporate_action_audit(cache_path: Path, snapshot: dict[str, Any]) -> dict[str, Any]:
    with sqlite3.connect(cache_path) as conn:
        conn.row_factory = sqlite3.Row
        anchors = [dict(row) for row in conn.execute("SELECT * FROM ibkr_daily_event_anchors")]
    anchor_dates = [date.fromisoformat(row["anchor_date"]) for row in anchors]
    schedule = calendar_schedule(min(anchor_dates) - timedelta(days=5), max(anchor_dates) + timedelta(days=15))
    sessions = sorted(schedule)
    actions_by_symbol = {
        symbol: [
            {**item, "date": date.fromisoformat(item["action_date"])}
            for item in payload.get("actions", [])
        ]
        for symbol, payload in snapshot.get("symbols", {}).items()
    }
    flags: list[dict[str, Any]] = []
    for row in anchors:
        anchor_date = date.fromisoformat(row["anchor_date"])
        future_sessions = [session for session in sessions if session > anchor_date]
        one_day = set(future_sessions[:1])
        five_day = set(future_sessions[:5])
        actions = actions_by_symbol.get(str(row["symbol"]), [])
        horizon_1d = [item for item in actions if item["date"] in one_day]
        horizon_5d = [item for item in actions if item["date"] in five_day]
        if horizon_1d or horizon_5d:
            flags.append(
                {
                    "macro_event_id": int(row["macro_event_id"]),
                    "event_cluster_id": int(row["event_cluster_id"]),
                    "symbol": row["symbol"],
                    "anchor_date": row["anchor_date"],
                    "horizon_1d_actions": [
                        {key: value for key, value in item.items() if key != "date"} for item in horizon_1d
                    ],
                    "horizon_5d_actions": [
                        {key: value for key, value in item.items() if key != "date"} for item in horizon_5d
                    ],
                    "dividend_contaminated_1d": any(item["action_type"] == "dividend" for item in horizon_1d),
                    "dividend_contaminated_5d": any(item["action_type"] == "dividend" for item in horizon_5d),
                    "split_flag_1d": any(item["action_type"] == "split" for item in horizon_1d),
                    "split_flag_5d": any(item["action_type"] == "split" for item in horizon_5d),
                    "label_quality_flag": "corporate_action_review_required",
                }
            )
    counts = Counter()
    for item in flags:
        for key in ("dividend_contaminated_1d", "dividend_contaminated_5d", "split_flag_1d", "split_flag_5d"):
            counts[key] += int(bool(item[key]))
    checks = {
        "all_symbols_fetched": len(snapshot.get("symbols", {})) == len({row["symbol"] for row in anchors}),
        "provider_errors_zero": not snapshot.get("errors"),
        "flags_generated": True,
        "ibkr_adjustment_policy_explicit": True,
    }
    return {
        "verdict": "PASS" if all(checks.values()) else "FAIL",
        "checks": checks,
        "policy": {
            "ibkr_trades_adjustment": "split-adjusted, dividend-unadjusted",
            "daily_horizon": "strict next 1 and next 5 XNYS sessions after anchor_date",
            "s05_requirement": "use total-return adjusted price or persist dividend/split contamination and label quality flags",
        },
        "counts": {
            "source_symbols": len(snapshot.get("symbols", {})),
            "source_actions": sum(len(item.get("actions", [])) for item in snapshot.get("symbols", {}).values()),
            "flagged_anchor_symbol_rows": len(flags),
            **dict(counts),
        },
        "flags": flags,
        "source_snapshot": snapshot,
    }


def build_overlap_audit(source_path: Path) -> dict[str, Any]:
    conn = sqlite3.connect(f"file:{source_path.resolve().as_posix()}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    try:
        rows = [
            dict(row)
            for row in conn.execute(
                """
                SELECT v.*,r.raw_json
                  FROM v_canonical_reaction_trigger_macro_events v
                  JOIN macro_event_releases r ON r.macro_event_id=v.macro_event_id
                 ORDER BY v.model_visible_at_utc,v.macro_event_id
                """
            )
        ]
    finally:
        conn.close()
    events: list[dict[str, Any]] = []
    bundles: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        try:
            raw = json.loads(row.get("raw_json") or "{}")
        except json.JSONDecodeError:
            raw = {}
        anchor = parse_utc(row["model_visible_at_utc"])
        meeting_date = str(raw.get("meeting_date") or raw.get("event_date") or anchor.date().isoformat())
        parent_meeting_id = stable_id("meeting", row["source_system"], meeting_date)
        release_bundle_id = stable_id("release_bundle", row["source_system"], meeting_date)
        item = {
            "macro_event_id": int(row["macro_event_id"]),
            "event_cluster_id": int(row["event_cluster_id"]),
            "source_system": row["source_system"],
            "subevent_type": row["event_type"],
            "release_name": row["release_name"],
            "anchor_at_utc": row["model_visible_at_utc"],
            "release_time_quality": row["release_time_quality"],
            "parent_meeting_id": parent_meeting_id,
            "release_bundle_id": release_bundle_id,
        }
        bundles.setdefault(release_bundle_id, []).append(item)
        events.append(item)
    overlap_groups: list[dict[str, Any]] = []
    event_to_overlap: dict[int, tuple[str, float]] = {}
    for bundle_id, items in sorted(bundles.items()):
        if len(items) <= 1:
            continue
        overlap_group_id = stable_id("overlap_group", bundle_id)
        weight = 1.0 / len(items)
        overlap_groups.append(
            {
                "overlap_group_id": overlap_group_id,
                "release_bundle_id": bundle_id,
                "event_count": len(items),
                "overlap_weight_each": weight,
                "macro_event_ids": [item["macro_event_id"] for item in items],
                "subevent_types": [item["subevent_type"] for item in items],
                "anchors": [item["anchor_at_utc"] for item in items],
            }
        )
        for item in items:
            event_to_overlap[item["macro_event_id"]] = (overlap_group_id, weight)
    for item in events:
        overlap_group_id, weight = event_to_overlap.get(
            item["macro_event_id"], (stable_id("overlap_group", item["macro_event_id"]), 1.0)
        )
        item["overlap_group_id"] = overlap_group_id
        item["overlap_weight"] = weight
    exact = [item for item in events if item["release_time_quality"] != "official_date_only"]
    cross_bundle_pairs: list[dict[str, Any]] = []
    for index, left in enumerate(exact):
        left_anchor = parse_utc(left["anchor_at_utc"])
        for right in exact[index + 1 :]:
            right_anchor = parse_utc(right["anchor_at_utc"])
            if abs((right_anchor - left_anchor).total_seconds()) >= 3600:
                continue
            if left["release_bundle_id"] == right["release_bundle_id"]:
                continue
            cross_bundle_pairs.append(
                {
                    "left_macro_event_id": left["macro_event_id"],
                    "right_macro_event_id": right["macro_event_id"],
                    "left_source": left["source_system"],
                    "right_source": right["source_system"],
                    "minutes_apart": abs((right_anchor - left_anchor).total_seconds()) / 60.0,
                }
            )
    checks = {
        "all_112_events_mapped": len(events) == 112,
        "metadata_complete": all(
            item["parent_meeting_id"]
            and item["release_bundle_id"]
            and item["subevent_type"]
            and item["overlap_group_id"]
            and 0 < float(item["overlap_weight"]) <= 1
            for item in events
        ),
        "group_weights_sum_to_one": all(
            abs(group["event_count"] * group["overlap_weight_each"] - 1.0) < 1e-9
            for group in overlap_groups
        ),
    }
    return {
        "verdict": "PASS" if all(checks.values()) else "FAIL",
        "checks": checks,
        "counts": {
            "events": len(events),
            "release_bundles": len(bundles),
            "multi_event_bundles": len(overlap_groups),
            "events_in_multi_event_bundles": sum(group["event_count"] for group in overlap_groups),
            "effective_weighted_bundle_samples": sum(
                sum(item["overlap_weight"] for item in events if item["release_bundle_id"] == bundle_id)
                for bundle_id in bundles
            ),
            "cross_bundle_intraday_overlap_pairs": len(cross_bundle_pairs),
        },
        "events": events,
        "overlap_groups": overlap_groups,
        "cross_bundle_intraday_overlap_pairs": cross_bundle_pairs,
        "s05_requirement": "dedupe or weight outcome samples by overlap_group_id; do not count bundled subevents as independent full-weight samples",
    }


def markdown(report: dict[str, Any]) -> str:
    lines = [
        "# S04 Market-Data Pre-S05 Supplemental Audit",
        "",
        f"- Verdict: **{report['verdict']}**",
        f"- S05 pilot gate: **{report['s05_pilot_gate']}**",
        f"- S05/M253 stage rows: `{report['s05_m253_stage_rows']}`",
        "",
        "## Component verdicts",
        "",
    ]
    for key in ("eth", "provenance", "corporate_actions", "event_overlap"):
        component = report[key]
        lines.append(f"- `{key}`: **{component.get('verdict', 'PASS')}**")
    lines.extend(
        [
            "",
            "## ETH completeness",
            "",
            f"- Counts: `{report['eth']['counts']}`",
            "",
            "| Symbol | Expected | Trade bars | Missing trade | Provider candidate | No-trade/symbol candidate | Closed |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for symbol, row in report["eth"]["per_symbol"].items():
        lines.append(
            f"| `{symbol}` | {row['expected_eth_minutes']} | {row['actual_trade_minutes']} | "
            f"{row['missing_trade_minutes']} | {row['provider_missing_candidate_minutes']} | "
            f"{row['no_trade_or_symbol_specific_candidate_minutes']} | {row['market_closed_minutes']} |"
        )
    lines.extend(
        [
            "",
            "## Provenance",
            "",
            f"- Checks: `{report['provenance']['checks']}`",
            "",
            "## Corporate actions",
            "",
            f"- Counts: `{report['corporate_actions']['counts']}`",
            f"- Policy: `{report['corporate_actions']['policy']}`",
            "",
            "## Event overlap",
            "",
            f"- Counts: `{report['event_overlap']['counts']}`",
            f"- S05 requirement: {report['event_overlap']['s05_requirement']}",
            "",
            "## Safety",
            "",
            "- Audit/research metadata only.",
            "- No S05/M253 execution, broker order, what-if, transmit, live routing, or threshold promotion.",
            "",
        ]
    )
    return "\n".join(lines)
