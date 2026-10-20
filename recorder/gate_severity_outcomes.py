from __future__ import annotations

import json
import math
import sqlite3
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


GATE_SEVERITY_OUTCOMES_VERSION = "m250_5-gate-severity-outcomes-v1"


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _result(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    result = payload.get("result")
    return result if isinstance(result, dict) else payload


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed):
        return None
    return parsed


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _connect(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path.expanduser().resolve(), timeout=30)
    con.row_factory = sqlite3.Row
    return con


def ensure_gate_severity_outcome_schema(con: sqlite3.Connection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS m250_5_gate_severity_outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_key TEXT NOT NULL,
            symbol TEXT NOT NULL,
            action TEXT NOT NULL,
            horizon_minutes INTEGER NOT NULL,
            event_at TEXT NOT NULL,
            due_at TEXT NOT NULL,
            baseline_price REAL,
            baseline_source TEXT,
            outcome_at TEXT,
            outcome_price REAL,
            realized_return REAL,
            signed_return REAL,
            gate_posture TEXT,
            size_multiplier REAL,
            confidence_multiplier REAL,
            paper_state TEXT,
            paper_quantity REAL,
            expected_direction TEXT,
            status TEXT NOT NULL,
            blocker TEXT,
            payload_json TEXT NOT NULL,
            outcome_version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(snapshot_key, symbol, action, horizon_minutes)
        )
        """
    )
    con.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_m250_5_gate_outcomes_status
        ON m250_5_gate_severity_outcomes(status,due_at)
        """
    )
    con.commit()


def _price_observations(con: sqlite3.Connection, symbols: set[str]) -> dict[str, list[dict[str, Any]]]:
    if not symbols:
        return {}
    placeholders = ",".join("?" for _ in symbols)
    params = tuple(sorted(symbols))
    observations: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for table, time_col, price_col, source_col in (
        ("market_baselines", "captured_at", "selected_price", "selected_price_source"),
        ("market_reactions", "captured_at", "reaction_price", "price_source"),
    ):
        try:
            rows = con.execute(
                f"""
                SELECT symbol, {time_col} AS at, {price_col} AS price, {source_col} AS source
                  FROM {table}
                 WHERE symbol IN ({placeholders}) AND {price_col} IS NOT NULL
                """,
                params,
            ).fetchall()
        except sqlite3.Error:
            continue
        for row in rows:
            at = _parse_dt(row["at"])
            price = _as_float(row["price"])
            if at is not None and price is not None:
                observations[str(row["symbol"]).upper()].append(
                    {"at": at, "price": price, "source": row["source"] or table}
                )
    for symbol, rows in observations.items():
        observations[symbol] = sorted(rows, key=lambda item: item["at"])
    return observations


def _select_baseline(rows: list[dict[str, Any]], event_at: datetime) -> dict[str, Any] | None:
    selected = None
    for row in rows:
        if row["at"] <= event_at:
            selected = row
        else:
            break
    return selected


def _select_outcome(rows: list[dict[str, Any]], due_at: datetime, *, max_lag_minutes: float) -> dict[str, Any] | None:
    max_dt = due_at + timedelta(minutes=max(0.0, float(max_lag_minutes)))
    for row in rows:
        if due_at <= row["at"] <= max_dt:
            return row
    return None


def _paper_intents(m242_payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    payload = _result(m242_payload)
    ledger = payload.get("ledger") if isinstance(payload.get("ledger"), dict) else {}
    if not ledger and isinstance(payload.get("result"), dict):
        ledger = payload["result"].get("ledger") if isinstance(payload["result"].get("ledger"), dict) else {}
    return [row for row in ledger.get("intents") or [] if isinstance(row, dict)]


def _record_current_snapshot(
    con: sqlite3.Connection,
    *,
    m250_payload: dict[str, Any],
    m242_payload: dict[str, Any] | None,
    horizons_minutes: list[int],
    now_iso: str,
) -> list[dict[str, Any]]:
    m250 = _result(m250_payload)
    event_at = _parse_dt(m250.get("generated_at")) or _parse_dt(now_iso) or datetime.now(UTC)
    summary = m250.get("summary") if isinstance(m250.get("summary"), dict) else {}
    contexts = m250.get("contexts") if isinstance(m250.get("contexts"), dict) else {}
    policy = contexts.get("gate_severity_policy") if isinstance(contexts.get("gate_severity_policy"), dict) else {}
    posture = summary.get("gate_execution_posture") or policy.get("recommended_execution_posture") or "unknown"
    size_multiplier = _as_float(summary.get("gate_size_multiplier") or policy.get("size_multiplier")) or 1.0
    confidence_multiplier = _as_float(summary.get("gate_confidence_multiplier") or policy.get("confidence_multiplier")) or 1.0
    intents = _paper_intents(m242_payload)
    symbols = {str(row.get("symbol") or "").upper() for row in intents if row.get("symbol")}
    observations = _price_observations(con, symbols)
    inserted: list[dict[str, Any]] = []
    for intent in intents:
        symbol = str(intent.get("symbol") or "").upper()
        action = str(intent.get("action") or "").upper()
        if not symbol or not action:
            continue
        baseline = _select_baseline(observations.get(symbol) or [], event_at)
        direction = "up" if action in {"BUY", "BUY_TO_OPEN"} else "down" if action in {"SELL", "SELL_SHORT", "BUY_TO_COVER"} else "unknown"
        for horizon in horizons_minutes:
            due_at = event_at + timedelta(minutes=int(horizon))
            snapshot_key = f"{event_at.isoformat()}|{symbol}|{action}|{posture}"
            status = "pending" if baseline else "baseline_unavailable"
            blocker = None if baseline else "baseline_unavailable"
            payload = {
                "m250_generated_at": m250.get("generated_at"),
                "intent_id": intent.get("intent_id"),
                "intent_state": intent.get("state"),
                "final_entry_size_resolution": intent.get("final_entry_size_resolution"),
                "gate_severity_policy": policy,
            }
            con.execute(
                """
                INSERT OR IGNORE INTO m250_5_gate_severity_outcomes (
                    snapshot_key,symbol,action,horizon_minutes,event_at,due_at,
                    baseline_price,baseline_source,gate_posture,size_multiplier,
                    confidence_multiplier,paper_state,paper_quantity,expected_direction,
                    status,blocker,payload_json,outcome_version,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    snapshot_key,
                    symbol,
                    action,
                    int(horizon),
                    event_at.isoformat(),
                    due_at.isoformat(),
                    baseline.get("price") if baseline else None,
                    baseline.get("source") if baseline else None,
                    posture,
                    size_multiplier,
                    confidence_multiplier,
                    intent.get("state"),
                    _as_float(intent.get("quantity")),
                    direction,
                    status,
                    blocker,
                    json.dumps(payload, ensure_ascii=False),
                    GATE_SEVERITY_OUTCOMES_VERSION,
                    now_iso,
                    now_iso,
                ),
            )
            inserted.append({"symbol": symbol, "action": action, "horizon_minutes": int(horizon), "status": status})
    con.commit()
    return inserted


def _evaluate_pending(con: sqlite3.Connection, *, max_lag_minutes: float, now_iso: str, limit: int) -> list[dict[str, Any]]:
    rows = con.execute(
        """
        SELECT * FROM m250_5_gate_severity_outcomes
         WHERE status IN ('pending','outcome_unavailable')
         ORDER BY due_at ASC
         LIMIT ?
        """,
        (max(1, int(limit)),),
    ).fetchall()
    symbols = {str(row["symbol"]).upper() for row in rows}
    observations = _price_observations(con, symbols)
    updates: list[dict[str, Any]] = []
    for row in rows:
        due_at = _parse_dt(row["due_at"])
        baseline_price = _as_float(row["baseline_price"])
        if due_at is None or baseline_price is None:
            continue
        outcome = _select_outcome(observations.get(str(row["symbol"]).upper()) or [], due_at, max_lag_minutes=max_lag_minutes)
        if outcome is None:
            status = "outcome_unavailable"
            con.execute(
                "UPDATE m250_5_gate_severity_outcomes SET status=?, blocker=?, updated_at=? WHERE id=?",
                (status, "outcome_unavailable", now_iso, row["id"]),
            )
            updates.append({"id": row["id"], "symbol": row["symbol"], "status": status})
            continue
        outcome_price = float(outcome["price"])
        realized_return = (outcome_price / baseline_price - 1.0) if baseline_price else None
        expected_direction = str(row["expected_direction"] or "")
        signed_return = realized_return
        if expected_direction == "down" and realized_return is not None:
            signed_return = -realized_return
        con.execute(
            """
            UPDATE m250_5_gate_severity_outcomes
               SET status='evaluated',
                   blocker=NULL,
                   outcome_at=?,
                   outcome_price=?,
                   realized_return=?,
                   signed_return=?,
                   updated_at=?
             WHERE id=?
            """,
            (outcome["at"].isoformat(), outcome_price, realized_return, signed_return, now_iso, row["id"]),
        )
        updates.append({"id": row["id"], "symbol": row["symbol"], "status": "evaluated", "signed_return": signed_return})
    con.commit()
    return updates


def build_gate_severity_outcome_report(
    *,
    database_path: Path,
    limited_auto_readiness_payload: dict[str, Any],
    paper_capital_execution_payload: dict[str, Any] | None = None,
    horizons_minutes: str = "5,15,30",
    record_current_snapshot: bool = True,
    evaluate_pending: bool = True,
    max_outcome_lag_minutes: float = 1440.0,
    evaluation_limit: int = 2000,
    now: str | None = None,
) -> dict[str, Any]:
    now_iso = now or _iso_now()
    horizons = [int(part.strip()) for part in str(horizons_minutes or "").split(",") if part.strip()]
    with _connect(database_path) as con:
        ensure_gate_severity_outcome_schema(con)
        inserted = (
            _record_current_snapshot(
                con,
                m250_payload=limited_auto_readiness_payload,
                m242_payload=paper_capital_execution_payload,
                horizons_minutes=horizons,
                now_iso=now_iso,
            )
            if record_current_snapshot
            else []
        )
        evaluated = (
            _evaluate_pending(con, max_lag_minutes=max_outcome_lag_minutes, now_iso=now_iso, limit=evaluation_limit)
            if evaluate_pending
            else []
        )
        status_counts = dict(Counter(row[0] for row in con.execute("SELECT status FROM m250_5_gate_severity_outcomes")))
        posture_counts = dict(Counter(row[0] for row in con.execute("SELECT gate_posture FROM m250_5_gate_severity_outcomes")))
        latest = [
            dict(row)
            for row in con.execute(
                """
                SELECT symbol,action,horizon_minutes,gate_posture,size_multiplier,confidence_multiplier,
                       paper_state,realized_return,signed_return,status,blocker,updated_at
                  FROM m250_5_gate_severity_outcomes
                 ORDER BY updated_at DESC, id DESC
                 LIMIT 20
                """
            )
        ]
    return {
        "gate_severity_outcomes_version": GATE_SEVERITY_OUTCOMES_VERSION,
        "generated_at": now_iso,
        "status": "ok",
        "database_path": str(database_path.expanduser().resolve()),
        "summary": {
            "recorded_rows_this_run": len(inserted),
            "evaluated_rows_this_run": sum(1 for row in evaluated if row.get("status") == "evaluated"),
            "status_counts": status_counts,
            "gate_posture_counts": posture_counts,
            "horizons_minutes": horizons,
        },
        "latest_rows": latest,
        "safety": {
            "local_outcome_log_only": True,
            "broker_orders_submitted": False,
            "transmit_enabled": False,
            "live_order_routing_enabled": False,
            "threshold_table_mutated": False,
        },
    }


def gate_severity_outcome_markdown(report: dict[str, Any]) -> str:
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    lines = [
        "# M250.5 Gate Severity Outcome Log",
        "",
        f"- Generated: `{report.get('generated_at')}`",
        f"- Status: `{report.get('status')}`",
        f"- Recorded / evaluated this run: `{summary.get('recorded_rows_this_run')}` / `{summary.get('evaluated_rows_this_run')}`",
        f"- Status counts: `{summary.get('status_counts') or {}}`",
        f"- Gate posture counts: `{summary.get('gate_posture_counts') or {}}`",
        "- Safety: local outcome log only; no broker order submission, no transmit, no live routing, no threshold mutation.",
        "",
        "## Latest rows",
        "",
        "| Symbol | Action | Horizon | Posture | Size | Confidence | Paper state | Signed return | Status | Blocker |",
        "| --- | --- | ---: | --- | ---: | ---: | --- | ---: | --- | --- |",
    ]
    for row in report.get("latest_rows") or []:
        lines.append(
            f"| `{row.get('symbol')}` | `{row.get('action')}` | {row.get('horizon_minutes')} | `{row.get('gate_posture')}` | "
            f"{row.get('size_multiplier')} | {row.get('confidence_multiplier')} | `{row.get('paper_state')}` | "
            f"{row.get('signed_return')} | `{row.get('status')}` | `{row.get('blocker') or '-'}` |"
        )
    if not report.get("latest_rows"):
        lines.append("| - | - | - | - | - | - | - | - | - | - |")
    return "\n".join(lines) + "\n"


def write_gate_severity_outcome_json(path: Path, report: dict[str, Any]) -> Path:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return resolved


def write_gate_severity_outcome_markdown(path: Path, report: dict[str, Any]) -> Path:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(gate_severity_outcome_markdown(report), encoding="utf-8")
    return resolved
