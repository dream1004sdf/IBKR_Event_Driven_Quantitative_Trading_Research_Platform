from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


PREDICTION_TREND_REPORT_VERSION = "prediction-trend-report-v1"


def _parse_now(value: str | None) -> datetime:
    if not value:
        return datetime.now(UTC)
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _accuracy(hits: int, evaluated: int) -> float | None:
    return round(hits / evaluated, 6) if evaluated else None


def _window_rows(
    conn: sqlite3.Connection,
    *,
    start: datetime,
    end: datetime,
    date_field: str,
    min_rows: int,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"""
        SELECT scorer_name, model_name, scorer_version, horizon, event_type,
               COUNT(*) AS rows,
               SUM(CASE WHEN direction_hit IS NOT NULL THEN 1 ELSE 0 END) AS evaluated_rows,
               SUM(CASE WHEN direction_hit = 1 THEN 1 ELSE 0 END) AS direction_hits,
               AVG(absolute_raw_error) AS avg_absolute_raw_error,
               AVG(absolute_excess_error) AS avg_absolute_excess_error,
               AVG(confidence) AS avg_confidence
        FROM replay_model_scores
        WHERE direction_hit IS NOT NULL
          AND datetime({date_field}) >= datetime(?)
          AND datetime({date_field}) < datetime(?)
        GROUP BY scorer_name, model_name, scorer_version, horizon, event_type
        HAVING evaluated_rows >= ?
        ORDER BY scorer_name, model_name, scorer_version, horizon, event_type
        """,
        (start.isoformat(), end.isoformat(), int(min_rows)),
    ).fetchall()
    output: list[dict[str, Any]] = []
    for row in rows:
        evaluated = int(row["evaluated_rows"] or 0)
        hits = int(row["direction_hits"] or 0)
        output.append(
            {
                "scorer_name": row["scorer_name"],
                "model_name": row["model_name"],
                "scorer_version": row["scorer_version"],
                "horizon": row["horizon"],
                "event_type": row["event_type"],
                "rows": int(row["rows"] or 0),
                "evaluated_rows": evaluated,
                "direction_hits": hits,
                "direction_accuracy": _accuracy(hits, evaluated),
                "avg_absolute_raw_error": row["avg_absolute_raw_error"],
                "avg_absolute_excess_error": row["avg_absolute_excess_error"],
                "avg_confidence": row["avg_confidence"],
            }
        )
    return output


def _key(row: dict[str, Any]) -> tuple[str, str, str, str, str]:
    return (
        str(row.get("scorer_name") or ""),
        str(row.get("model_name") or ""),
        str(row.get("scorer_version") or ""),
        str(row.get("horizon") or ""),
        str(row.get("event_type") or ""),
    )


def build_prediction_trend_report(
    *,
    database_path: str | Path,
    window_days: int = 14,
    date_field: str = "updated_at",
    min_rows: int = 1,
    now: str | None = None,
) -> dict[str, Any]:
    if date_field not in {"updated_at", "created_at", "replay_timestamp", "prediction_cutoff_at"}:
        raise ValueError(f"Unsupported date_field: {date_field}")
    now_dt = _parse_now(now)
    current_start = now_dt - timedelta(days=max(1, int(window_days)))
    previous_start = current_start - timedelta(days=max(1, int(window_days)))
    conn = sqlite3.connect(database_path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        current_rows = _window_rows(
            conn,
            start=current_start,
            end=now_dt,
            date_field=date_field,
            min_rows=min_rows,
        )
        previous_rows = _window_rows(
            conn,
            start=previous_start,
            end=current_start,
            date_field=date_field,
            min_rows=min_rows,
        )
    finally:
        conn.close()

    previous_by_key = {_key(row): row for row in previous_rows}
    comparisons: list[dict[str, Any]] = []
    for current in current_rows:
        previous = previous_by_key.get(_key(current))
        current_accuracy = current.get("direction_accuracy")
        previous_accuracy = previous.get("direction_accuracy") if previous else None
        delta = (
            round(float(current_accuracy) - float(previous_accuracy), 6)
            if current_accuracy is not None and previous_accuracy is not None
            else None
        )
        comparisons.append(
            {
                "scorer_name": current.get("scorer_name"),
                "model_name": current.get("model_name"),
                "scorer_version": current.get("scorer_version"),
                "horizon": current.get("horizon"),
                "event_type": current.get("event_type"),
                "current": current,
                "previous": previous,
                "direction_accuracy_delta": delta,
                "trend": "new" if previous is None else "improving" if (delta or 0) > 0 else "declining" if (delta or 0) < 0 else "flat",
            }
        )
    comparisons.sort(
        key=lambda row: (
            str(row.get("trend") == "new"),
            row.get("direction_accuracy_delta") if row.get("direction_accuracy_delta") is not None else -999,
        ),
        reverse=True,
    )
    return {
        "prediction_trend_report_version": PREDICTION_TREND_REPORT_VERSION,
        "generated_at": now_dt.isoformat(),
        "database_path": str(Path(database_path).expanduser().resolve()),
        "date_field": date_field,
        "window_days": int(window_days),
        "min_rows": int(min_rows),
        "current_window": {"start": current_start.isoformat(), "end": now_dt.isoformat()},
        "previous_window": {"start": previous_start.isoformat(), "end": current_start.isoformat()},
        "current_group_count": len(current_rows),
        "previous_group_count": len(previous_rows),
        "comparison_count": len(comparisons),
        "comparisons": comparisons,
        "safety": {
            "read_only_report": True,
            "uses_stored_replay_model_scores_only": True,
            "does_not_call_openai_or_ibkr": True,
            "does_not_mutate_scorer_or_thresholds": True,
        },
    }


def prediction_trend_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Prediction Hit-Rate Trend Report",
        "",
        f"- Version: `{report.get('prediction_trend_report_version')}`",
        f"- Generated at: `{report.get('generated_at')}`",
        f"- Date field: `{report.get('date_field')}`",
        f"- Window days: `{report.get('window_days')}`",
        f"- Current window: `{(report.get('current_window') or {}).get('start')}` to `{(report.get('current_window') or {}).get('end')}`",
        f"- Previous window: `{(report.get('previous_window') or {}).get('start')}` to `{(report.get('previous_window') or {}).get('end')}`",
        "",
        "| Trend | Scorer | Model | Version | Horizon | Event | Current | Previous | Delta | Rows |",
        "| --- | --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: |",
    ]
    for item in report.get("comparisons") or []:
        current = item.get("current") or {}
        previous = item.get("previous") or {}
        cur_acc = current.get("direction_accuracy")
        prev_acc = previous.get("direction_accuracy")
        delta = item.get("direction_accuracy_delta")
        lines.append(
            f"| {item.get('trend')} | {item.get('scorer_name')} | `{item.get('model_name')}` | "
            f"`{item.get('scorer_version')}` | {item.get('horizon')} | {item.get('event_type')} | "
            f"{cur_acc if cur_acc is not None else '-'} | {prev_acc if prev_acc is not None else '-'} | "
            f"{delta if delta is not None else '-'} | {current.get('evaluated_rows')} |"
        )
    lines.append("")
    lines.append("- Safety: read-only over stored replay score outcomes; no OpenAI, IBKR, order, or threshold mutation.")
    return "\n".join(lines)


def write_prediction_trend_json(output_path: Path, report: dict[str, Any]) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return output_path


def write_prediction_trend_markdown(output_path: Path, report: dict[str, Any]) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(prediction_trend_markdown(report), encoding="utf-8")
    return output_path
