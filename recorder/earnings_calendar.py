from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Any


EARNINGS_CALENDAR_VERSION = "future-earnings-calendar-v1-sec-cycle-projection"
DEFAULT_CLUSTER_DAYS = 45
MIN_REASONABLE_CYCLE_DAYS = 70


def _parse_dt(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _event_score(*, symbol: str, event_subtype: str, primary_document: str, materiality_reason: str) -> int:
    score = 0
    document = primary_document.lower()
    if document.startswith(f"{symbol.lower()}-"):
        score += 3
    if "guidance" in event_subtype.lower():
        score += 1
    if "forward guidance" in materiality_reason.lower():
        score += 1
    if document.startswith("tm"):
        score -= 2
    return score


def _collapse_quarterly_events(events: list[tuple[datetime, int]], *, cluster_days: int = DEFAULT_CLUSTER_DAYS) -> list[datetime]:
    if not events:
        return []
    sorted_events = sorted(events, key=lambda item: item[0])
    clusters: list[list[tuple[datetime, int]]] = []
    for event in sorted_events:
        if not clusters:
            clusters.append([event])
            continue
        previous_ts = clusters[-1][-1][0]
        if (event[0] - previous_ts).total_seconds() / 86400.0 <= cluster_days:
            clusters[-1].append(event)
        else:
            clusters.append([event])
    representatives: list[datetime] = []
    for cluster in clusters:
        best_ts, _best_score = max(cluster, key=lambda item: (item[1], item[0]))
        representatives.append(best_ts)
    return representatives


def _symbol_rows(conn: sqlite3.Connection, symbols: tuple[str, ...]) -> dict[str, list[datetime]]:
    params: list[Any] = []
    where = "h.event_type='earnings'"
    if symbols:
        placeholders = ",".join("?" for _ in symbols)
        where += f" AND upper(actual_subject_symbol) IN ({placeholders})"
        params.extend(symbol.upper() for symbol in symbols)
    raw_rows: dict[str, list[tuple[datetime, int]]] = {}
    for symbol, event_timestamp, event_subtype, primary_document, materiality_reason in conn.execute(
        f"""
        SELECT
            upper(h.actual_subject_symbol),
            h.event_timestamp,
            h.event_subtype,
            coalesce(d.primary_document, ''),
            coalesce(d.materiality_reason, '')
        FROM historical_events h
        LEFT JOIN official_documents d ON h.official_document_id = d.id
        WHERE {where}
        ORDER BY upper(h.actual_subject_symbol), h.event_timestamp
        """,
        params,
    ):
        parsed = _parse_dt(event_timestamp)
        if parsed is None:
            continue
        symbol_text = str(symbol)
        score = _event_score(
            symbol=symbol_text,
            event_subtype=str(event_subtype or ""),
            primary_document=str(primary_document or ""),
            materiality_reason=str(materiality_reason or ""),
        )
        raw_rows.setdefault(symbol_text, []).append((parsed, score))
    return {symbol: _collapse_quarterly_events(events) for symbol, events in raw_rows.items()}


def _projection(symbol: str, events: list[datetime], *, now: datetime, default_cycle_days: int) -> dict[str, Any]:
    events = sorted(events)
    latest = events[-1] if events else None
    intervals = [
        max(1.0, (right - left).total_seconds() / 86400.0)
        for left, right in zip(events, events[1:])
        if right > left and (right - left).total_seconds() / 86400.0 >= MIN_REASONABLE_CYCLE_DAYS
    ]
    cycle_days = float(median(intervals)) if intervals else float(default_cycle_days)
    if latest is None:
        expected = now + timedelta(days=default_cycle_days)
        confidence = "low"
        method = "default_cycle_no_history"
    else:
        expected = latest + timedelta(days=cycle_days)
        while expected < now:
            expected += timedelta(days=cycle_days)
        confidence = "medium" if len(events) >= 3 else "low"
        method = "historical_sec_8k_cycle_projection"
    warmup_start = expected - timedelta(days=7)
    return {
        "symbol": symbol,
        "method": method,
        "confidence": confidence,
        "history_count": len(events),
        "latest_historical_earnings_at": latest.isoformat() if latest else None,
        "median_cycle_days": round(cycle_days, 2),
        "event_cluster_days": DEFAULT_CLUSTER_DAYS,
        "min_reasonable_cycle_days": MIN_REASONABLE_CYCLE_DAYS,
        "projected_earnings_at": expected.isoformat(),
        "projected_date": expected.date().isoformat(),
        "suggested_warmup_start": warmup_start.isoformat(),
        "suggested_actions": [
            "refresh_evidence_packet",
            "refresh_option_structure",
            "tighten_risk_gate_review",
            "watch_for_official_8k_confirmation",
        ],
        "warning": "Projection only; confirm with official company/exchange/provider calendar before treating as a scheduled event.",
    }


def build_future_earnings_calendar(
    *,
    database_path: str | Path,
    symbols: tuple[str, ...] = (),
    horizon_days: int = 120,
    default_cycle_days: int = 91,
    now: str | None = None,
) -> dict[str, Any]:
    now_dt = _parse_dt(now) if now else datetime.now(UTC)
    if now_dt is None:
        now_dt = datetime.now(UTC)
    conn = sqlite3.connect(database_path, timeout=30)
    try:
        by_symbol = _symbol_rows(conn, tuple(symbol.upper() for symbol in symbols if symbol.strip()))
    finally:
        conn.close()
    projections = [
        _projection(symbol, events, now=now_dt, default_cycle_days=default_cycle_days)
        for symbol, events in sorted(by_symbol.items())
    ]
    horizon_end = now_dt + timedelta(days=max(1, int(horizon_days)))
    in_horizon = [
        item for item in projections if (_parse_dt(item.get("projected_earnings_at")) or horizon_end) <= horizon_end
    ]
    return {
        "future_earnings_calendar_version": EARNINGS_CALENDAR_VERSION,
        "generated_at": now_dt.isoformat(),
        "database_path": str(Path(database_path).expanduser().resolve()),
        "horizon_days": int(horizon_days),
        "horizon_end": horizon_end.isoformat(),
        "default_cycle_days": int(default_cycle_days),
        "requested_symbols": list(symbols),
        "projection_count": len(in_horizon),
        "projections": in_horizon,
        "out_of_horizon_count": len(projections) - len(in_horizon),
        "safety": {
            "read_only_report": True,
            "projection_not_confirmed_calendar": True,
            "does_not_call_external_api": True,
            "does_not_change_risk_gates": True,
        },
    }


def future_earnings_calendar_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Future Earnings Calendar",
        "",
        f"- Version: `{report.get('future_earnings_calendar_version')}`",
        f"- Generated at: `{report.get('generated_at')}`",
        f"- Horizon days: `{report.get('horizon_days')}`",
        f"- Projection count: `{report.get('projection_count')}`",
        "",
        "| Symbol | Projected date | Confidence | History | Median cycle | Warmup start |",
        "| --- | --- | --- | ---: | ---: | --- |",
    ]
    for item in report.get("projections") or []:
        lines.append(
            f"| {item.get('symbol')} | {item.get('projected_date')} | {item.get('confidence')} | "
            f"{item.get('history_count')} | {item.get('median_cycle_days')} | {item.get('suggested_warmup_start')} |"
        )
    lines.append("")
    lines.append("- Warning: projection only; confirm with an official/company/provider earnings calendar before treating as scheduled.")
    return "\n".join(lines)


def write_future_earnings_calendar_json(output_path: Path, report: dict[str, Any]) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return output_path


def write_future_earnings_calendar_markdown(output_path: Path, report: dict[str, Any]) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(future_earnings_calendar_markdown(report), encoding="utf-8")
    return output_path
