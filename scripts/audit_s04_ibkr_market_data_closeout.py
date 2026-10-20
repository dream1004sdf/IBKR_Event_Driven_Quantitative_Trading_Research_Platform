from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from datetime import UTC, date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from recorder.ibkr_market_data_pilot import audit_cache, connect_cache, ensure_cache_schema


NY = ZoneInfo("America/New_York")


def _dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _read_source_stage_rows(path: Path) -> int:
    conn = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True, timeout=60)
    try:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM research_backfill_stage_runs WHERE stage_name LIKE 'S05%' OR stage_name LIKE '%M253%'"
            ).fetchone()[0]
        )
    finally:
        conn.close()


def _calendar_schedule(start_date: date, end_date: date) -> dict[date, tuple[datetime, datetime]]:
    calendar = xcals.get_calendar("XNYS")
    sessions = calendar.sessions_in_range(pd.Timestamp(start_date), pd.Timestamp(end_date))
    return {
        session.date(): (
            calendar.session_open(session).to_pydatetime().astimezone(UTC),
            calendar.session_close(session).to_pydatetime().astimezone(UTC),
        )
        for session in sessions
    }


def _ceil_minute(value: datetime) -> datetime:
    if value.second or value.microsecond:
        return value.replace(second=0, microsecond=0) + pd.Timedelta(minutes=1).to_pytimedelta()
    return value.replace(second=0, microsecond=0)


def build_audit(config: dict, collection_report: dict) -> dict:
    cache_path = Path(config["paths"]["cache_database"])
    source_path = Path(config["paths"]["source_database"])
    thresholds = config["audit_thresholds"]
    basic = audit_cache(config, idempotency_diff=collection_report.get("idempotency_diff") or {})
    with connect_cache(cache_path) as conn:
        ensure_cache_schema(conn)
        shard_rows = [dict(row) for row in conn.execute("SELECT * FROM ibkr_bar_shards WHERE data_kind='intraday'")]
        window_rows = [dict(row) for row in conn.execute("SELECT * FROM ibkr_event_windows")]
        anchor_rows = [dict(row) for row in conn.execute("SELECT * FROM ibkr_daily_event_anchors")]
        bar_rows = [
            dict(row)
            for row in conn.execute(
                "SELECT symbol,bar_time_utc FROM ibkr_historical_bars WHERE data_kind='intraday'"
            )
        ]
        daily_rows = [
            dict(row)
            for row in conn.execute(
                "SELECT symbol,session_date FROM ibkr_historical_bars WHERE data_kind='daily' ORDER BY symbol,session_date"
            )
        ]
        wrong_intraday_shard_size = int(
            conn.execute("SELECT COUNT(*) FROM ibkr_bar_shards WHERE data_kind='intraday' AND bar_size<>'1 min'").fetchone()[0]
        )
        wrong_intraday_bar_size = int(
            conn.execute("SELECT COUNT(*) FROM ibkr_historical_bars WHERE data_kind='intraday' AND bar_size<>'1 min'").fetchone()[0]
        )
        date_only_intraday_windows = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM ibkr_event_windows w
                JOIN ibkr_daily_event_anchors a
                  ON a.macro_event_id=w.macro_event_id AND a.symbol=w.symbol
                WHERE a.intraday_label_eligible=0 OR a.release_time_quality='official_date_only'
                """
            ).fetchone()[0]
        )
        exact_event_missing_windows = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM ibkr_daily_event_anchors a
                WHERE a.intraday_label_eligible=1
                  AND NOT EXISTS (
                    SELECT 1 FROM ibkr_event_windows w
                    WHERE w.macro_event_id=a.macro_event_id AND w.symbol=a.symbol
                  )
                """
            ).fetchone()[0]
        )
        exact_window_wrong_eligibility = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM ibkr_event_windows w
                JOIN ibkr_daily_event_anchors a
                  ON a.macro_event_id=w.macro_event_id AND a.symbol=w.symbol
                WHERE a.intraday_label_eligible<>1
                   OR a.release_time_quality NOT IN ('official_exact','provider_exact')
                """
            ).fetchone()[0]
        )
        missing_required_symbols = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM (SELECT 'USO' symbol UNION ALL SELECT 'XLE') required
                WHERE NOT EXISTS (SELECT 1 FROM ibkr_contracts c WHERE c.symbol=required.symbol)
                   OR NOT EXISTS (SELECT 1 FROM ibkr_historical_bars b WHERE b.symbol=required.symbol AND b.data_kind='daily')
                   OR NOT EXISTS (SELECT 1 FROM ibkr_historical_bars b WHERE b.symbol=required.symbol AND b.data_kind='intraday')
                """
            ).fetchone()[0]
        )
        retried_shards = int(
            conn.execute("SELECT COUNT(1) FROM ibkr_bar_shards WHERE attempt_count>1").fetchone()[0]
        )
        max_shard_attempts = int(
            conn.execute("SELECT COALESCE(MAX(attempt_count),0) FROM ibkr_bar_shards").fetchone()[0]
        )
        unresolved_error_shards = int(
            conn.execute("SELECT COUNT(1) FROM ibkr_bar_shards WHERE status='error' OR last_error IS NOT NULL").fetchone()[0]
        )

    intraday_by_symbol: dict[str, set[datetime]] = {}
    for row in bar_rows:
        intraday_by_symbol.setdefault(row["symbol"], set()).add(_dt(row["bar_time_utc"]))
    daily_by_symbol: dict[str, list[date]] = {}
    for row in daily_rows:
        daily_by_symbol.setdefault(row["symbol"], []).append(date.fromisoformat(row["session_date"]))

    all_start = min(_dt(row["start_at_utc"]) for row in shard_rows)
    all_end = max(_dt(row["end_at_utc"]) for row in shard_rows)
    schedule = _calendar_schedule(all_start.date(), all_end.date())
    expected_rth: dict[str, set[datetime]] = {symbol: set() for symbol in intraday_by_symbol}
    expected_early_close: dict[str, set[datetime]] = {symbol: set() for symbol in intraday_by_symbol}
    for shard in shard_rows:
        symbol = shard["symbol"]
        start_at = _dt(shard["start_at_utc"])
        end_at = _dt(shard["end_at_utc"])
        cursor_date = start_at.date()
        while cursor_date <= end_at.date():
            session = schedule.get(cursor_date)
            if session:
                session_open, session_close = session
                minute = _ceil_minute(max(start_at, session_open))
                limit = min(end_at, session_close)
                target = expected_rth.setdefault(symbol, set())
                early_target = expected_early_close.setdefault(symbol, set())
                early_close = session_close.astimezone(NY).time() < time(16, 0)
                while minute < limit:
                    target.add(minute)
                    if early_close:
                        early_target.add(minute)
                    minute += pd.Timedelta(minutes=1).to_pytimedelta()
            cursor_date += pd.Timedelta(days=1).to_pytimedelta()

    expected_rth_count = sum(len(values) for values in expected_rth.values())
    missing_rth_count = sum(len(values - intraday_by_symbol.get(symbol, set())) for symbol, values in expected_rth.items())
    rth_missing_rate = (missing_rth_count / expected_rth_count) if expected_rth_count else 1.0
    symbol_rth_coverage = {}
    for symbol, expected in sorted(expected_rth.items()):
        actual = intraday_by_symbol.get(symbol, set())
        missing = len(expected - actual)
        symbol_rth_coverage[symbol] = {
            "expected_minutes": len(expected),
            "actual_minutes": len(expected.intersection(actual)),
            "missing_minutes": missing,
            "missing_rate": (missing / len(expected)) if expected else 0.0,
        }
    expected_early_close_count = sum(len(values) for values in expected_early_close.values())
    missing_early_close_count = sum(
        len(values - intraday_by_symbol.get(symbol, set())) for symbol, values in expected_early_close.items()
    )
    early_close_missing_rate = (
        missing_early_close_count / expected_early_close_count if expected_early_close_count else 0.0
    )
    actual_intraday_count = sum(len(values) for values in intraday_by_symbol.values())
    actual_rth_count = sum(
        len(values.intersection(intraday_by_symbol.get(symbol, set()))) for symbol, values in expected_rth.items()
    )
    eth_bar_count = actual_intraday_count - actual_rth_count
    holiday_rth_bars = 0
    for values in intraday_by_symbol.values():
        for stamp in values:
            local = stamp.astimezone(NY)
            if local.date() not in schedule and time(9, 30) <= local.time() < time(16, 0):
                holiday_rth_bars += 1

    anchor_session_counts: Counter[str] = Counter()
    unique_events: dict[int, dict] = {}
    for row in anchor_rows:
        unique_events.setdefault(int(row["macro_event_id"]), row)
    for row in unique_events.values():
        anchor = _dt(row["anchor_at_utc"])
        session = schedule.get(anchor.date())
        local = anchor.astimezone(NY)
        if session and session[0] <= anchor < session[1]:
            anchor_session_counts["rth"] += 1
        elif time(4, 0) <= local.time() < time(20, 0):
            anchor_session_counts["eth"] += 1
        else:
            anchor_session_counts["closed"] += 1

    missing_anchor_after_bar = 0
    for row in window_rows:
        anchor = _dt(row["anchor_at_utc"])
        end_at = _dt(row["end_at_utc"])
        values = intraday_by_symbol.get(row["symbol"], set())
        if not any(anchor <= stamp <= end_at for stamp in values):
            missing_anchor_after_bar += 1

    daily_1d_missing = 0
    daily_5d_missing = 0
    for row in anchor_rows:
        anchor_date = date.fromisoformat(row["anchor_date"])
        after = [session for session in daily_by_symbol.get(row["symbol"], []) if session > anchor_date]
        if len(after) < 1:
            daily_1d_missing += 1
        if len(after) < 5:
            daily_5d_missing += 1

    early_close_sessions = sorted(
        session_date.isoformat()
        for session_date, (_, session_close) in schedule.items()
        if session_close.astimezone(NY).time() < time(16, 0)
    )
    s05_rows = _read_source_stage_rows(source_path)
    collection_source_unchanged = bool(collection_report.get("source_database_unchanged"))
    checks = {
        "collection_audit_pass": basic["verdict"] == "PASS",
        "canonical_event_count_112": len(unique_events) == 112,
        "exact_event_count_88": sum(1 for row in unique_events.values() if int(row["intraday_label_eligible"]) == 1) == 88,
        "date_only_event_count_24": sum(1 for row in unique_events.values() if int(row["intraday_label_eligible"]) == 0) == 24,
        "required_uso_xle_present": missing_required_symbols == 0,
        "intraday_bar_size_1min": wrong_intraday_shard_size == 0 and wrong_intraday_bar_size == 0,
        "exact_event_windows_complete": exact_event_missing_windows <= int(thresholds["max_exact_event_missing_windows"]),
        "date_only_intraday_windows_zero": date_only_intraday_windows <= int(thresholds["max_date_only_intraday_windows"]),
        "exact_window_eligibility_clean": exact_window_wrong_eligibility == 0,
        "anchor_after_bar_complete": missing_anchor_after_bar == 0,
        "daily_1d_complete": daily_1d_missing <= int(thresholds["max_daily_1d_missing"]),
        "daily_5d_complete": daily_5d_missing <= int(thresholds["max_daily_5d_missing"]),
        "rth_missing_rate_within_threshold": rth_missing_rate <= float(thresholds["max_rth_missing_rate"]),
        "early_close_missing_rate_within_threshold": early_close_missing_rate
        <= float(thresholds["max_rth_missing_rate"]),
        "holiday_rth_bars_zero": holiday_rth_bars <= int(thresholds["max_holiday_rth_bars"]),
        "eth_bars_present": eth_bar_count > 0,
        "duplicate_bar_groups_zero": basic["duplicate_bar_identity_groups"] <= int(thresholds["max_duplicate_bar_groups"]),
        "invalid_ohlc_zero": basic["invalid_ohlc_rows"] <= int(thresholds["max_invalid_ohlc_rows"]),
        "timestamp_issues_zero": basic["timestamp_issue_rows"] <= int(thresholds["max_timestamp_issue_rows"]),
        "resume_count_diff_zero": basic["pass_conditions"]["resume_count_diff_zero"],
        "source_database_unchanged": collection_source_unchanged,
        "unresolved_error_shards_zero": unresolved_error_shards == 0,
        "s05_m253_not_started": s05_rows == 0,
    }
    return {
        "audit_version": "s04-ibkr-market-data-closeout-v1",
        "verdict": "PASS" if all(checks.values()) else "FAIL",
        "checks": checks,
        "counts": {
            "canonical_events": len(unique_events),
            "exact_events": sum(1 for row in unique_events.values() if int(row["intraday_label_eligible"]) == 1),
            "date_only_events": sum(1 for row in unique_events.values() if int(row["intraday_label_eligible"]) == 0),
            "daily_event_anchors": len(anchor_rows),
            "intraday_event_windows": len(window_rows),
            "intraday_shards": len(shard_rows),
            "actual_intraday_bars": actual_intraday_count,
            "expected_rth_minutes": expected_rth_count,
            "missing_rth_minutes": missing_rth_count,
            "actual_rth_bars": actual_rth_count,
            "eth_bars": eth_bar_count,
            "expected_early_close_minutes": expected_early_close_count,
            "missing_early_close_minutes": missing_early_close_count,
            "holiday_rth_bars": holiday_rth_bars,
            "date_only_intraday_windows": date_only_intraday_windows,
            "exact_event_missing_windows": exact_event_missing_windows,
            "exact_window_wrong_eligibility": exact_window_wrong_eligibility,
            "missing_anchor_after_bar": missing_anchor_after_bar,
            "daily_1d_missing": daily_1d_missing,
            "daily_5d_missing": daily_5d_missing,
            "missing_required_symbols": missing_required_symbols,
            "wrong_intraday_shard_size": wrong_intraday_shard_size,
            "wrong_intraday_bar_size": wrong_intraday_bar_size,
            "retried_shards": retried_shards,
            "max_shard_attempts": max_shard_attempts,
            "unresolved_error_shards": unresolved_error_shards,
            "s05_m253_stage_rows": s05_rows,
        },
        "rates": {
            "rth_missing_rate": rth_missing_rate,
            "early_close_missing_rate": early_close_missing_rate,
        },
        "symbol_rth_coverage": symbol_rth_coverage,
        "anchor_session_counts": dict(anchor_session_counts),
        "early_close_sessions": early_close_sessions,
        "collection_audit": basic,
    }


def markdown(report: dict) -> str:
    lines = [
        "# S04 IBKR Market-Data Closeout Audit",
        "",
        f"- Verdict: **{report['verdict']}**",
        f"- Counts: `{report['counts']}`",
        f"- Rates: `{report['rates']}`",
        f"- Anchor sessions: `{report['anchor_session_counts']}`",
        "",
        "## Checks",
        "",
    ]
    for key, value in report["checks"].items():
        lines.append(f"- `{key}`: `{value}`")
    lines.extend(
        [
            "",
            "## Per-symbol RTH coverage",
            "",
            "| Symbol | Expected | Actual | Missing | Missing rate |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for symbol, row in report["symbol_rth_coverage"].items():
        lines.append(
            f"| `{symbol}` | {row['expected_minutes']} | {row['actual_minutes']} | "
            f"{row['missing_minutes']} | {row['missing_rate']:.4%} |"
        )
    lines.extend(["", "## Early-close sessions", "", f"`{report['early_close_sessions']}`", ""])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path("config/backfill/s04_ibkr_market_data_closeout_20250101_20260706_live.json"))
    parser.add_argument("--collection-report", type=Path, default=Path("artifacts/audit/s04_ibkr_market_data_collection_20250101_20260706.json"))
    parser.add_argument("--json-output", type=Path, default=Path("artifacts/audit/s04_ibkr_market_data_closeout_audit.json"))
    parser.add_argument("--markdown-output", type=Path, default=Path("artifacts/audit/s04_ibkr_market_data_closeout_audit.md"))
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    collection_report = json.loads(args.collection_report.read_text(encoding="utf-8"))
    report = build_audit(config, collection_report)
    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    args.markdown_output.write_text(markdown(report), encoding="utf-8")
    print(json.dumps({"verdict": report["verdict"], "counts": report["counts"], "rates": report["rates"], "failed_checks": [key for key, value in report["checks"].items() if not value]}, indent=2))
    return 0 if report["verdict"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
