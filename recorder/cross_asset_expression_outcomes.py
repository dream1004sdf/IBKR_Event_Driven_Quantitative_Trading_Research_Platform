from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


CROSS_ASSET_EXPRESSION_OUTCOMES_VERSION = "m241_8-cross-asset-expression-outcomes-v1"


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _result(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    result = payload.get("result")
    return result if isinstance(result, dict) else payload


def _parse_horizons(value: str | list[int] | tuple[int, ...]) -> list[int]:
    if isinstance(value, (list, tuple)):
        parts = value
    else:
        parts = str(value or "5,15,30").replace(";", ",").split(",")
    horizons: list[int] = []
    for part in parts:
        try:
            parsed = int(str(part).strip())
        except ValueError:
            continue
        if parsed > 0 and parsed not in horizons:
            horizons.append(parsed)
    return horizons or [5, 15, 30]


def _snapshot_id(report: dict[str, Any]) -> str:
    generated_at = str(report.get("generated_at") or "")
    top = report.get("summary", {}).get("top_expressions") if isinstance(report.get("summary"), dict) else []
    raw = json.dumps(
        {
            "version": report.get("cross_asset_expression_ranking_version"),
            "generated_at": generated_at,
            "top": top,
        },
        sort_keys=True,
        ensure_ascii=True,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _expected_direction(row: dict[str, Any]) -> str:
    symbol = str(row.get("symbol") or "").upper()
    direction = str(row.get("direction") or "").lower()
    action = str(row.get("action") or "").upper()
    expression = str(row.get("expression") or "").lower()
    if direction == "bullish" or action in {"BUY", "BUY_TO_OPEN"} or expression in {"stock_long", "long_call"}:
        return "up"
    if direction == "bearish" or action in {"SELL_SHORT", "SELL_TO_OPEN"} or expression in {"long_put"}:
        return "down"
    if direction == "risk_reduction" and symbol in {"VIX", "VXX", "UVXY"}:
        return "up"
    return "neutral"


def _realized_direction(raw_return: float | None, *, flat_threshold_pct: float) -> str:
    if raw_return is None:
        return "unknown"
    threshold = max(0.0, float(flat_threshold_pct)) / 100.0
    if raw_return > threshold:
        return "up"
    if raw_return < -threshold:
        return "down"
    return "flat"


def _direction_hit(expected: str, realized: str) -> int | None:
    if expected not in {"up", "down"} or realized == "unknown":
        return None
    if realized == "flat":
        return 0
    return 1 if expected == realized else 0


def ensure_cross_asset_outcome_schema(con: sqlite3.Connection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS m241_8_cross_asset_expression_outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_id TEXT NOT NULL,
            ranking_generated_at TEXT NOT NULL,
            symbol TEXT NOT NULL,
            scenario TEXT NOT NULL,
            expression TEXT NOT NULL,
            action TEXT NOT NULL,
            expected_direction TEXT NOT NULL,
            rank INTEGER NOT NULL,
            expression_score REAL NOT NULL,
            horizon_minutes INTEGER NOT NULL,
            due_at TEXT NOT NULL,
            baseline_at TEXT,
            outcome_at TEXT,
            baseline_price REAL,
            outcome_price REAL,
            raw_return REAL,
            realized_direction TEXT NOT NULL DEFAULT 'unknown',
            direction_hit INTEGER,
            status TEXT NOT NULL DEFAULT 'pending',
            blocker_reason TEXT NOT NULL DEFAULT '',
            row_json TEXT NOT NULL DEFAULT '{}',
            outcome_version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    con.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_m241_8_cross_asset_outcomes_unique
        ON m241_8_cross_asset_expression_outcomes
        (snapshot_id, symbol, scenario, expression, horizon_minutes)
        """
    )
    con.commit()


def _price_observations(con: sqlite3.Connection, symbols: set[str]) -> dict[str, list[dict[str, Any]]]:
    if not symbols:
        return {}
    placeholders = ",".join("?" for _ in symbols)
    params = tuple(sorted(symbols))
    observations: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in con.execute(
        f"""
        SELECT symbol, captured_at AS at, selected_price AS price, selected_price_source AS source
          FROM market_baselines
         WHERE symbol IN ({placeholders}) AND selected_price IS NOT NULL
        """,
        params,
    ):
        observations[str(row[0]).upper()].append({"at": _parse_dt(row[1]), "price": _as_float(row[2]), "source": row[3] or "market_baselines"})
    for row in con.execute(
        f"""
        SELECT symbol, captured_at AS at, reaction_price AS price, price_source AS source
          FROM market_reactions
         WHERE symbol IN ({placeholders}) AND reaction_price IS NOT NULL
        """,
        params,
    ):
        observations[str(row[0]).upper()].append({"at": _parse_dt(row[1]), "price": _as_float(row[2]), "source": row[3] or "market_reactions"})
    for symbol, rows in observations.items():
        observations[symbol] = sorted(
            [row for row in rows if row.get("at") is not None and row.get("price") is not None],
            key=lambda item: item["at"],
        )
    return observations


def _select_baseline(
    rows: list[dict[str, Any]],
    target_dt: datetime,
    *,
    fallback_price: float | None,
    max_baseline_age_minutes: float,
) -> dict[str, Any] | None:
    selected = None
    for row in rows:
        if row["at"] <= target_dt:
            selected = row
        else:
            break
    if selected is not None:
        age_minutes = (target_dt - selected["at"]).total_seconds() / 60.0
        if age_minutes <= max(0.0, float(max_baseline_age_minutes)):
            return selected
    if fallback_price is not None:
        return {"at": target_dt, "price": fallback_price, "source": "m241_7_market_context_fallback"}
    return None


def _select_outcome(rows: list[dict[str, Any]], due_dt: datetime, *, max_outcome_lag_minutes: float) -> dict[str, Any] | None:
    max_dt = due_dt + timedelta(minutes=max(0.0, float(max_outcome_lag_minutes)))
    for row in rows:
        if due_dt <= row["at"] <= max_dt:
            return row
    return None


def _market_context_price(row: dict[str, Any]) -> float | None:
    market = row.get("market_context") if isinstance(row.get("market_context"), dict) else {}
    return _as_float(market.get("price") or market.get("last") or market.get("close"))


def _record_current_snapshot(
    con: sqlite3.Connection,
    ranking_report: dict[str, Any],
    *,
    horizons_minutes: list[int],
    max_expressions_to_record: int,
    now_iso: str,
) -> list[dict[str, Any]]:
    report = _result(ranking_report)
    generated_dt = _parse_dt(report.get("generated_at"))
    if generated_dt is None:
        return []
    sid = _snapshot_id(report)
    recorded: list[dict[str, Any]] = []
    rows = [row for row in (report.get("expression_rankings") or []) if isinstance(row, dict)]
    for row in rows[: max(0, int(max_expressions_to_record))]:
        symbol = str(row.get("symbol") or "").upper()
        scenario = str(row.get("scenario") or "")
        expression = str(row.get("expression") or "")
        if not symbol or not scenario or not expression:
            continue
        expected = _expected_direction(row)
        baseline_price = _market_context_price(row)
        for horizon in horizons_minutes:
            due_dt = generated_dt + timedelta(minutes=int(horizon))
            con.execute(
                """
                INSERT INTO m241_8_cross_asset_expression_outcomes (
                    snapshot_id, ranking_generated_at, symbol, scenario, expression, action,
                    expected_direction, rank, expression_score, horizon_minutes, due_at,
                    baseline_at, baseline_price, status, blocker_reason, row_json,
                    outcome_version, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(snapshot_id, symbol, scenario, expression, horizon_minutes)
                DO UPDATE SET
                    rank=excluded.rank,
                    expression_score=excluded.expression_score,
                    row_json=excluded.row_json,
                    updated_at=excluded.updated_at
                """,
                (
                    sid,
                    generated_dt.isoformat(),
                    symbol,
                    scenario,
                    expression,
                    str(row.get("action") or ""),
                    expected,
                    int(_as_float(row.get("rank")) or 0),
                    float(_as_float(row.get("expression_score")) or 0.0),
                    int(horizon),
                    due_dt.isoformat(),
                    generated_dt.isoformat() if baseline_price is not None else None,
                    baseline_price,
                    "pending",
                    "",
                    json.dumps(row, sort_keys=True, ensure_ascii=False),
                    CROSS_ASSET_EXPRESSION_OUTCOMES_VERSION,
                    now_iso,
                    now_iso,
                ),
            )
            recorded.append({"symbol": symbol, "scenario": scenario, "expression": expression, "horizon_minutes": int(horizon)})
    con.commit()
    return recorded


def _evaluate_pending_rows(
    con: sqlite3.Connection,
    *,
    now_dt: datetime,
    flat_threshold_pct: float,
    max_baseline_age_minutes: float,
    max_outcome_lag_minutes: float,
    limit: int,
) -> list[dict[str, Any]]:
    pending = [
        dict(row)
        for row in con.execute(
            """
            SELECT *
              FROM m241_8_cross_asset_expression_outcomes
             WHERE status IN ('pending', 'outcome_price_unavailable', 'baseline_unavailable')
               AND due_at <= ?
             ORDER BY due_at ASC
             LIMIT ?
            """,
            (now_dt.isoformat(), max(1, int(limit))),
        )
    ]
    symbols = {str(row.get("symbol") or "").upper() for row in pending if row.get("symbol")}
    observations = _price_observations(con, symbols)
    updated: list[dict[str, Any]] = []
    now_iso = now_dt.isoformat()
    for row in pending:
        symbol = str(row.get("symbol") or "").upper()
        generated_dt = _parse_dt(row.get("ranking_generated_at"))
        due_dt = _parse_dt(row.get("due_at"))
        if generated_dt is None or due_dt is None:
            continue
        obs = observations.get(symbol) or []
        baseline = None
        existing_baseline = _as_float(row.get("baseline_price"))
        if existing_baseline is not None:
            baseline = {"at": _parse_dt(row.get("baseline_at")) or generated_dt, "price": existing_baseline, "source": "stored_baseline"}
        else:
            row_json = {}
            try:
                row_json = json.loads(row.get("row_json") or "{}")
            except json.JSONDecodeError:
                row_json = {}
            baseline = _select_baseline(
                obs,
                generated_dt,
                fallback_price=_market_context_price(row_json),
                max_baseline_age_minutes=max_baseline_age_minutes,
            )
        outcome = _select_outcome(obs, due_dt, max_outcome_lag_minutes=max_outcome_lag_minutes)
        if baseline is None:
            status = "baseline_unavailable"
            blocker = "baseline_price_unavailable"
            raw_return = None
            realized = "unknown"
            hit = None
            outcome_at = None
            outcome_price = None
        elif outcome is None:
            status = "outcome_price_unavailable"
            blocker = "outcome_price_unavailable"
            raw_return = None
            realized = "unknown"
            hit = None
            outcome_at = None
            outcome_price = None
        else:
            baseline_price = float(baseline["price"])
            outcome_price = float(outcome["price"])
            raw_return = (outcome_price / baseline_price - 1.0) if baseline_price else None
            realized = _realized_direction(raw_return, flat_threshold_pct=flat_threshold_pct)
            hit = _direction_hit(str(row.get("expected_direction") or ""), realized)
            status = "evaluated"
            blocker = ""
            outcome_at = outcome["at"].isoformat()
        con.execute(
            """
            UPDATE m241_8_cross_asset_expression_outcomes
               SET baseline_at=?,
                   outcome_at=?,
                   baseline_price=?,
                   outcome_price=?,
                   raw_return=?,
                   realized_direction=?,
                   direction_hit=?,
                   status=?,
                   blocker_reason=?,
                   updated_at=?
             WHERE id=?
            """,
            (
                (baseline or {}).get("at").isoformat() if baseline else None,
                outcome_at,
                (baseline or {}).get("price") if baseline else None,
                outcome_price,
                raw_return,
                realized,
                hit,
                status,
                blocker,
                now_iso,
                row["id"],
            ),
        )
        updated.append({"id": row["id"], "symbol": symbol, "horizon_minutes": row.get("horizon_minutes"), "status": status, "direction_hit": hit})
    con.commit()
    return updated


def _aggregate_adjustments(con: sqlite3.Connection, *, min_evaluated_samples: int, adjustment_cap: float) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    evaluated = [
        dict(row)
        for row in con.execute(
            """
            SELECT scenario, symbol, expression, horizon_minutes, expected_direction,
                   raw_return, direction_hit
              FROM m241_8_cross_asset_expression_outcomes
             WHERE status='evaluated'
               AND direction_hit IS NOT NULL
            """
        )
    ]

    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    scenario_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in evaluated:
        groups[(row["scenario"], row["symbol"], row["expression"])].append(row)
        scenario_groups[row["scenario"]].append(row)

    adjustments: list[dict[str, Any]] = []
    adjustment_map: dict[str, Any] = {}

    def build_adjustment(key: str, rows: list[dict[str, Any]], *, scope: str, scenario: str, symbol: str = "", expression: str = "") -> dict[str, Any]:
        samples = len(rows)
        hits = sum(int(row.get("direction_hit") or 0) for row in rows)
        hit_rate = hits / samples if samples else 0.0
        aligned_returns: list[float] = []
        for row in rows:
            raw = _as_float(row.get("raw_return"))
            if raw is None:
                continue
            expected = str(row.get("expected_direction") or "")
            aligned_returns.append(raw if expected == "up" else -raw if expected == "down" else abs(raw))
        avg_aligned_return = sum(aligned_returns) / len(aligned_returns) if aligned_returns else 0.0
        if samples < int(min_evaluated_samples):
            score_adjustment = 0.0
            status = "insufficient_samples"
            confidence = "low"
        else:
            raw_adjustment = (hit_rate - 0.5) * 24.0 + avg_aligned_return * 1000.0
            score_adjustment = max(-float(adjustment_cap), min(float(adjustment_cap), raw_adjustment))
            status = "active"
            confidence = "high" if samples >= int(min_evaluated_samples) * 3 else "medium"
        return {
            "key": key,
            "scope": scope,
            "scenario": scenario,
            "symbol": symbol,
            "expression": expression,
            "evaluated_samples": samples,
            "hit_rate": round(hit_rate, 4),
            "avg_aligned_return": round(avg_aligned_return, 6),
            "score_adjustment": round(score_adjustment, 4),
            "status": status,
            "confidence": confidence,
        }

    for (scenario, symbol, expression), rows in sorted(groups.items()):
        key = f"{scenario}|{symbol}|{expression}"
        adj = build_adjustment(key, rows, scope="exact_expression", scenario=scenario, symbol=symbol, expression=expression)
        adjustments.append(adj)
        adjustment_map[key] = adj
    for scenario, rows in sorted(scenario_groups.items()):
        key = f"scenario::{scenario}"
        adj = build_adjustment(key, rows, scope="scenario", scenario=scenario)
        adjustments.append(adj)
        adjustment_map[key] = adj
    adjustments.sort(key=lambda item: (item["status"] == "active", abs(float(item["score_adjustment"])), item["evaluated_samples"]), reverse=True)
    return adjustments, adjustment_map


def build_cross_asset_expression_outcomes_report(
    *,
    database_path: Path,
    cross_asset_expression_ranking_payload: dict[str, Any] | None = None,
    horizons_minutes: str | list[int] | tuple[int, ...] = "5,15,30",
    max_expressions_to_record: int = 30,
    flat_threshold_pct: float = 0.05,
    max_baseline_age_minutes: float = 390.0,
    max_outcome_lag_minutes: float = 1440.0,
    min_evaluated_samples: int = 3,
    adjustment_cap: float = 12.0,
    evaluation_limit: int = 2000,
    record_current_snapshot: bool = True,
    evaluate_pending: bool = True,
    now: str | None = None,
) -> dict[str, Any]:
    now_dt = _parse_dt(now) if now else datetime.now(UTC)
    if now_dt is None:
        now_dt = datetime.now(UTC)
    horizons = _parse_horizons(horizons_minutes)
    path = database_path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    ensure_cross_asset_outcome_schema(con)

    recorded: list[dict[str, Any]] = []
    if record_current_snapshot and cross_asset_expression_ranking_payload:
        recorded = _record_current_snapshot(
            con,
            cross_asset_expression_ranking_payload,
            horizons_minutes=horizons,
            max_expressions_to_record=max_expressions_to_record,
            now_iso=now_dt.isoformat(),
        )

    evaluated: list[dict[str, Any]] = []
    if evaluate_pending:
        evaluated = _evaluate_pending_rows(
            con,
            now_dt=now_dt,
            flat_threshold_pct=flat_threshold_pct,
            max_baseline_age_minutes=max_baseline_age_minutes,
            max_outcome_lag_minutes=max_outcome_lag_minutes,
            limit=evaluation_limit,
        )

    adjustments, adjustment_map = _aggregate_adjustments(
        con,
        min_evaluated_samples=min_evaluated_samples,
        adjustment_cap=adjustment_cap,
    )
    status_counts = dict(Counter(row[0] for row in con.execute("SELECT status FROM m241_8_cross_asset_expression_outcomes")))
    horizon_counts = dict(Counter(str(row[0]) for row in con.execute("SELECT horizon_minutes FROM m241_8_cross_asset_expression_outcomes")))
    latest_rows = [
        dict(row)
        for row in con.execute(
            """
            SELECT snapshot_id, ranking_generated_at, symbol, scenario, expression, horizon_minutes,
                   expected_direction, raw_return, realized_direction, direction_hit, status, blocker_reason, updated_at
              FROM m241_8_cross_asset_expression_outcomes
             ORDER BY updated_at DESC, id DESC
             LIMIT 25
            """
        )
    ]
    con.close()
    active_adjustments = [row for row in adjustments if row.get("status") == "active" and abs(float(row.get("score_adjustment") or 0.0)) > 0]
    return {
        "cross_asset_expression_outcomes_version": CROSS_ASSET_EXPRESSION_OUTCOMES_VERSION,
        "milestone": "M241.8",
        "generated_at": now_dt.isoformat(),
        "status": "ok",
        "summary": {
            "recorded_current_rows": len(recorded),
            "evaluated_pending_rows": len(evaluated),
            "status_counts": status_counts,
            "horizon_counts": horizon_counts,
            "adjustment_count": len(adjustments),
            "active_adjustment_count": len(active_adjustments),
            "top_active_adjustments": active_adjustments[:10],
        },
        "parameters": {
            "horizons_minutes": horizons,
            "max_expressions_to_record": int(max_expressions_to_record),
            "flat_threshold_pct": float(flat_threshold_pct),
            "max_baseline_age_minutes": float(max_baseline_age_minutes),
            "max_outcome_lag_minutes": float(max_outcome_lag_minutes),
            "min_evaluated_samples": int(min_evaluated_samples),
            "adjustment_cap": float(adjustment_cap),
            "record_current_snapshot": bool(record_current_snapshot),
            "evaluate_pending": bool(evaluate_pending),
        },
        "adjustments": adjustments[:100],
        "adjustment_map": adjustment_map,
        "latest_rows": latest_rows,
        "policy": {
            "role": "log M241.7 expression outcomes and produce small score adjustments for the next M241.7 run",
            "pending_when_price_missing": True,
            "does_not_submit_orders": True,
            "does_not_enable_transmit": True,
            "does_not_create_m242_order_intents": True,
        },
        "safety": {
            "read_only_or_local_learning_only": True,
            "paper_recommendation_only": True,
            "ibkr_order_submitted": False,
            "transmit_enabled": False,
            "live_routing_enabled": False,
            "real_portfolio_mutated": False,
            "live_thresholds_mutated": False,
        },
    }


def cross_asset_expression_outcomes_markdown(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    lines = [
        "# M241.8 Cross-Asset Expression Outcomes",
        "",
        f"- Version: `{report.get('cross_asset_expression_outcomes_version')}`",
        f"- Generated: `{report.get('generated_at')}`",
        f"- Recorded / evaluated: `{summary.get('recorded_current_rows')}` / `{summary.get('evaluated_pending_rows')}`",
        f"- Status counts: `{summary.get('status_counts') or {}}`",
        f"- Active adjustments: `{summary.get('active_adjustment_count')}`",
        "- Safety: local outcome learning only; no broker order submission, no transmit, no live routing.",
        "",
        "## Top adjustments",
        "",
        "| Scope | Key | Samples | Hit rate | Avg aligned return | Score adj | Status |",
        "| --- | --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in report.get("adjustments") or []:
        lines.append(
            f"| `{row.get('scope')}` | `{row.get('key')}` | {row.get('evaluated_samples')} | "
            f"{row.get('hit_rate')} | {row.get('avg_aligned_return')} | {row.get('score_adjustment')} | `{row.get('status')}` |"
        )
    if not report.get("adjustments"):
        lines.append("| - | - | - | - | - | - | No evaluated samples yet. |")
    lines.extend(["", "## Latest outcome rows", "", "| Symbol | Scenario | Expression | Horizon | Expected | Return | Realized | Hit | Status | Blocker |", "| --- | --- | --- | ---: | --- | ---: | --- | --- | --- | --- |"])
    for row in report.get("latest_rows") or []:
        lines.append(
            f"| `{row.get('symbol')}` | `{row.get('scenario')}` | `{row.get('expression')}` | {row.get('horizon_minutes')} | "
            f"`{row.get('expected_direction')}` | {row.get('raw_return')} | `{row.get('realized_direction')}` | "
            f"`{row.get('direction_hit')}` | `{row.get('status')}` | `{row.get('blocker_reason') or '-'}` |"
        )
    if not report.get("latest_rows"):
        lines.append("| - | - | - | - | - | - | - | - | - | No rows. |")
    return "\n".join(lines) + "\n"


def write_cross_asset_expression_outcomes_json(path: Path, report: dict[str, Any]) -> Path:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return resolved


def write_cross_asset_expression_outcomes_markdown(path: Path, report: dict[str, Any]) -> Path:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(cross_asset_expression_outcomes_markdown(report), encoding="utf-8")
    return resolved
