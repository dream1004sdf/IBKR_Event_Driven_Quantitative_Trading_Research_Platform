from __future__ import annotations

import json
import sqlite3
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, time as dt_time, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .live_learning_llm_notes import M225_LLM_ADJUSTMENT_NOTE_VERSION
from .live_scorer_learning import ensure_live_learning_schema


M226_THRESHOLD_CANDIDATE_EVALUATOR_VERSION = "m226-threshold-candidate-evaluator-v10"
M226_THRESHOLD_CANDIDATE_EVALUATOR_SUMMARY_VERSION = "m226-threshold-candidate-evaluator-summary-v1"
UTC = timezone.utc


@dataclass(frozen=True)
class ThresholdCandidateEvaluatorConfig:
    database_path: Path
    note_version: str = M225_LLM_ADJUSTMENT_NOTE_VERSION
    min_watch_sample_count: int = 2
    min_promotion_sample_count: int = 8
    min_avg_confidence: float = 0.65
    min_avg_note_quality_score: float = 70.0
    min_promotion_score: float = 80.0
    min_estimated_error_reduction_bps: float = 1.0
    max_candidates: int = 100
    semantic_family_enabled: bool = True
    outcome_direction_split_enabled: bool = True
    max_focus_patterns: int = 8
    shadow_promotion_trial_enabled: bool = True
    min_shadow_trial_cycles: int = 3
    min_shadow_acceptance_score: float = 75.0
    promotion_gate_enabled: bool = True
    promotion_gate_min_sample_count: int = 8
    promotion_gate_recent_trial_count: int = 3
    promotion_gate_min_recent_acceptance_score: float = 75.0
    promotion_gate_max_abs_error_worsening_pct: float = 0.25
    promotion_gate_max_regime_family_share: float = 0.85
    promotion_gate_watchlist_enabled: bool = True
    promotion_gate_watchlist_stale_ticks: int = 10
    promotion_gate_watchlist_recycle_ticks: int = 20
    promotion_gate_recycle_min_new_samples: int = 5
    promotion_gate_recycle_return_confidence_ticks: int = 3
    promotion_gate_recycle_quarantine_ticks: int = 3
    promotion_gate_recycle_dormant_ticks: int = 30
    promotion_gate_recycle_retire_ticks: int = 60
    promotion_gate_recycle_near_return_remaining_checks: int = 1
    promotion_gate_option_root_cause_policy_enabled: bool = True
    promotion_gate_transition_history_limit: int = 50
    evidence_freshness_json_path: Path | None = None
    options_market_structure_json_path: Path | None = None
    options_market_structure_supervisor_json_path: Path | None = None
    portfolio_risk_state_json_path: Path | None = None
    max_ticks: int = 1
    interval_seconds: float = 300.0
    sleep_between_ticks: bool = False
    adaptive_refresh_enabled: bool = False
    adaptive_min_interval_seconds: float = 60.0
    adaptive_max_interval_seconds: float = 900.0
    adaptive_high_candidate_count: int = 100
    adaptive_changed_interval_seconds: float = 120.0
    adaptive_unchanged_interval_seconds: float = 600.0
    state_json_path: Path | None = None
    output_path: Path | None = None
    json_output_path: Path | None = None
    dry_run: bool = False


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _parse_iso_datetime(value: Any) -> datetime | None:
    try:
        text = str(value or "")
        if not text:
            return None
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except Exception:
        return None


def _age_seconds(value: Any) -> float | None:
    parsed = _parse_iso_datetime(value)
    if parsed is None:
        return None
    return round((datetime.now(UTC) - parsed).total_seconds(), 3)


def _current_us_option_session_context(now: datetime | None = None) -> dict[str, Any]:
    now_utc = (now or datetime.now(UTC)).astimezone(UTC)
    try:
        now_et = now_utc.astimezone(ZoneInfo("America/New_York"))
        timezone_name = "America/New_York"
    except Exception:
        now_et = now_utc
        timezone_name = "UTC_fallback"
    local_time = now_et.timetz().replace(tzinfo=None)
    is_weekday = now_et.weekday() < 5
    is_regular = bool(is_weekday and dt_time(9, 30) <= local_time < dt_time(16, 0))
    return {
        "checked_at": now_utc.isoformat(),
        "checked_at_et": now_et.isoformat(),
        "timezone": timezone_name,
        "session": "regular" if is_regular else "closed_or_extended",
        "is_weekday": is_weekday,
        "is_regular_option_session": is_regular,
        "regular_session_window_et": "09:30-16:00",
        "holiday_calendar_applied": False,
    }


def _option_structure_freshness(options_payload: dict[str, Any], supervisor_payload: dict[str, Any]) -> dict[str, Any]:
    session_guard = options_payload.get("session_guard") or {}
    guard_status = str(session_guard.get("freshness_status") or "").lower()
    max_age_minutes = session_guard.get("max_snapshot_age_minutes")
    if max_age_minutes is None:
        max_age_minutes = supervisor_payload.get("final_effective_max_snapshot_age_minutes")
    try:
        max_age_seconds = float(max_age_minutes if max_age_minutes is not None else 30.0) * 60.0
    except (TypeError, ValueError):
        max_age_seconds = 1800.0
    structure_age = _age_seconds(options_payload.get("generated_at"))
    quote_age = _age_seconds(options_payload.get("source_quote_generated_at"))
    supervisor_age = _age_seconds(supervisor_payload.get("completed_at"))
    ages = [value for value in (structure_age, quote_age, supervisor_age) if value is not None]
    observed_age = max(ages) if ages else None
    stale_reasons: list[str] = []
    if not options_payload:
        stale_reasons.append("option_structure_missing")
    if observed_age is None:
        stale_reasons.append("option_structure_timestamp_missing")
    elif observed_age > max_age_seconds:
        stale_reasons.append("option_structure_artifact_age_exceeded")
    if guard_status and guard_status not in {"green", "fresh", "ok"}:
        stale_reasons.append(f"session_guard_{guard_status}")
    supervisor_fresh = bool(supervisor_payload.get("final_quote_fresh"))
    supervisor_fresh_trusted = supervisor_fresh and supervisor_age is not None and supervisor_age <= max_age_seconds
    status = "fresh" if not stale_reasons and (guard_status in {"green", "fresh", "ok"} or supervisor_fresh_trusted) else "stale"
    if not options_payload:
        status = "missing"
    session_context = _current_us_option_session_context()
    expected_deferred_stale = bool(
        status == "stale"
        and "option_structure_artifact_age_exceeded" in stale_reasons
        and not session_context.get("is_regular_option_session")
    )
    if expected_deferred_stale:
        status = "expected_deferred_stale"
    return {
        "status": status,
        "fresh": status == "fresh",
        "expected_deferred_stale": expected_deferred_stale,
        "current_option_session": session_context,
        "guard_status": guard_status or None,
        "structure_age_seconds": structure_age,
        "source_quote_age_seconds": quote_age,
        "supervisor_age_seconds": supervisor_age,
        "observed_age_seconds": round(observed_age, 3) if observed_age is not None else None,
        "max_age_seconds": round(max_age_seconds, 3),
        "stale_reasons": list(dict.fromkeys(stale_reasons)),
        "supervisor_final_quote_fresh": supervisor_fresh,
        "supervisor_final_quote_fresh_trusted": supervisor_fresh_trusted,
    }


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path.expanduser().resolve(), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def _json_loads(value: Any, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except Exception:
        return fallback


def _row_has(row: sqlite3.Row | dict[str, Any], key: str) -> bool:
    if isinstance(row, dict):
        return key in row
    try:
        return key in row.keys()
    except Exception:
        return False


def _read_state(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        payload = json.loads(path.expanduser().resolve().read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_state(path: Path | None, payload: dict[str, Any]) -> None:
    if path is None:
        return
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _read_json_file(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        payload = json.loads(path.expanduser().resolve().read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _ensure_m226_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS m226_llm_threshold_candidate_evaluations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_key TEXT NOT NULL UNIQUE,
            evaluated_at TEXT NOT NULL,
            evaluator_version TEXT NOT NULL,
            status TEXT NOT NULL,
            promotion_score REAL,
            sample_count INTEGER NOT NULL DEFAULT 0,
            source_note_count INTEGER NOT NULL DEFAULT 0,
            avg_confidence REAL,
            avg_note_quality_score REAL,
            direction_miss_rate REAL,
            avg_absolute_error REAL,
            estimated_error_reduction_bps REAL,
            reason_category TEXT NOT NULL DEFAULT '',
            pattern_tags_json TEXT NOT NULL DEFAULT '[]',
            threshold_candidate TEXT NOT NULL DEFAULT '',
            blockers_json TEXT NOT NULL DEFAULT '[]',
            supporting_metrics_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS m227_shadow_promotion_trials (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_key TEXT NOT NULL,
            created_at TEXT NOT NULL,
            evaluator_version TEXT NOT NULL,
            trial_status TEXT NOT NULL,
            cycles_seen INTEGER NOT NULL DEFAULT 1,
            promotion_score REAL,
            sample_count INTEGER NOT NULL DEFAULT 0,
            direction_miss_rate REAL,
            avg_absolute_error REAL,
            estimated_error_reduction_bps REAL,
            acceptance_score REAL,
            ready_for_real_promotion INTEGER NOT NULL DEFAULT 0,
            trial_payload_json TEXT NOT NULL DEFAULT '{}'
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS m228_threshold_promotion_gate_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_key TEXT NOT NULL,
            reviewed_at TEXT NOT NULL,
            evaluator_version TEXT NOT NULL,
            gate_status TEXT NOT NULL,
            ready_for_promotion_waitlist INTEGER NOT NULL DEFAULT 0,
            failed_check_count INTEGER NOT NULL DEFAULT 0,
            blocked_reasons_json TEXT NOT NULL DEFAULT '[]',
            check_results_json TEXT NOT NULL DEFAULT '{}',
            review_payload_json TEXT NOT NULL DEFAULT '{}'
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS m229_threshold_promotion_gate_transitions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_key TEXT NOT NULL,
            transitioned_at TEXT NOT NULL,
            evaluator_version TEXT NOT NULL,
            previous_gate_status TEXT,
            new_gate_status TEXT NOT NULL,
            transition_type TEXT NOT NULL,
            semantic_family_key TEXT,
            outcome_direction_key TEXT,
            transition_payload_json TEXT NOT NULL DEFAULT '{}'
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS m230_threshold_promotion_recycle_state (
            candidate_key TEXT PRIMARY KEY,
            recycle_started_at TEXT NOT NULL,
            recycle_started_sample_count INTEGER NOT NULL DEFAULT 0,
            current_sample_count INTEGER NOT NULL DEFAULT 0,
            new_sample_count INTEGER NOT NULL DEFAULT 0,
            recycle_review_count INTEGER NOT NULL DEFAULT 0,
            consecutive_option_confidence_ticks INTEGER NOT NULL DEFAULT 0,
            last_option_medium_high_count INTEGER NOT NULL DEFAULT 0,
            quarantine_remaining_ticks INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'active',
            updated_at TEXT NOT NULL,
            state_payload_json TEXT NOT NULL DEFAULT '{}'
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS m231_threshold_promotion_recycle_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_key TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            evaluator_version TEXT NOT NULL,
            operational_gate_status TEXT NOT NULL,
            lifecycle_status TEXT NOT NULL,
            new_sample_count INTEGER NOT NULL DEFAULT 0,
            consecutive_option_confidence_ticks INTEGER NOT NULL DEFAULT 0,
            quarantine_remaining_ticks INTEGER NOT NULL DEFAULT 0,
            blocked_by_json TEXT NOT NULL DEFAULT '[]',
            root_causes_json TEXT NOT NULL DEFAULT '[]',
            scorer_action TEXT NOT NULL DEFAULT '',
            history_payload_json TEXT NOT NULL DEFAULT '{}'
        )"""
    )


def _semantic_family(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    tags = _json_loads(row["pattern_tags_json"], [])
    text = " ".join(
        [
            str(row["candidate_key"] or ""),
            str(row["reason_category"] or ""),
            str(row["threshold_candidate"] or ""),
            " ".join(str(item) for item in tags if item is not None),
        ]
    ).lower()
    signals: list[str] = []
    if any(token in text for token in ("late_baseline", "baseline_is_late", "timestamp", "event_market_data_lag", "stale_context", "stale baseline")):
        signals.append("timestamp_alignment_late_baseline")
    if any(token in text for token in ("asset_mapping", "asset map", "weak_asset", "symbol_relevance", "indirect_symbol", "underlying mismatch", "event-to-underlying")):
        signals.append("asset_mapping_relevance")
    if any(token in text for token in ("option", "gamma", "pin", "wide_spread", "spread", "chain", "greek", "oi", "max pain")):
        signals.append("option_structure_quality")
    if any(token in text for token in ("neutral", "flat", "low_directional", "weak market reaction", "fully_priced")):
        signals.append("neutral_flat_reaction")
    if any(token in text for token in ("macro", "policy", "cpi", "fed", "rates", "inflation")):
        signals.append("macro_headline_context")
    if any(token in text for token in ("geopolitical", "tariff", "war", "china", "iran", "russia", "ukraine")):
        signals.append("geopolitical_context")
    if not signals:
        normalized = "".join(ch if ch.isalnum() else "_" for ch in str(row["reason_category"] or "general").lower()).strip("_")
        signals.append(normalized[:64] or "general_threshold_candidate")
    primary = signals[0]
    secondary = next((item for item in signals[1:] if item != primary), None)
    key = primary if secondary is None else f"{primary}+{secondary}"
    return {
        "semantic_family_key": key,
        "semantic_family_label": key.replace("_", " / ").replace("+", " + "),
        "semantic_family_signals": signals,
    }


def _candidate_rows(conn: sqlite3.Connection, config: ThresholdCandidateEvaluatorConfig) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT candidate_key, generated_at, status, reason_category, pattern_tags_json,
                  sample_count, avg_confidence, avg_note_quality_score, threshold_candidate,
                  source_note_ids_json
           FROM m225_llm_note_threshold_candidates
           ORDER BY sample_count DESC, COALESCE(avg_note_quality_score,0) DESC, COALESCE(avg_confidence,0) DESC
           LIMIT ?""",
        (max(1, int(config.max_candidates)),),
    ).fetchall()


def _outcome_direction_bucket(metrics: dict[str, Any]) -> dict[str, Any]:
    predicted_counts = metrics.get("predicted_direction_counts") or {}
    actual_counts = metrics.get("actual_direction_counts") or {}
    pair_counts = metrics.get("direction_pair_counts") or {}
    avg_raw_return = metrics.get("avg_raw_return")
    avg_raw_abs = metrics.get("avg_raw_return_abs")
    avg_raw_return_float = float(avg_raw_return) if avg_raw_return is not None else 0.0
    avg_raw_abs_float = float(avg_raw_abs) if avg_raw_abs is not None else 0.0
    direction_miss_rate = metrics.get("direction_miss_rate")
    miss_float = float(direction_miss_rate) if direction_miss_rate is not None else 0.0

    top_predicted = max(predicted_counts.items(), key=lambda item: item[1])[0] if predicted_counts else "unknown"
    top_actual = max(actual_counts.items(), key=lambda item: item[1])[0] if actual_counts else "unknown"
    if avg_raw_abs_float < 0.002 and miss_float >= 0.35:
        key = "flat_misread"
    elif top_predicted == "up" and top_actual in {"down", "flat"} and miss_float >= 0.35:
        key = "false_positive_up"
    elif top_predicted == "down" and top_actual in {"up", "flat"} and miss_float >= 0.35:
        key = "false_positive_down"
    elif top_predicted != "up" and top_actual == "up" and avg_raw_return_float >= 0.005:
        key = "missed_strong_up"
    elif top_predicted != "down" and top_actual == "down" and avg_raw_return_float <= -0.005:
        key = "missed_strong_down"
    elif miss_float <= 0.25:
        key = "confirmed_direction"
    else:
        key = f"{top_predicted}_to_{top_actual}"
    return {
        "outcome_direction_key": key,
        "outcome_direction_label": key.replace("_", " / "),
        "top_predicted_direction": top_predicted,
        "top_actual_direction": top_actual,
        "direction_pair_counts": pair_counts,
    }


def _candidate_family_rows(
    conn: sqlite3.Connection,
    rows: list[sqlite3.Row],
    config: ThresholdCandidateEvaluatorConfig,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not config.semantic_family_enabled:
        return [{key: row[key] for key in row.keys()} for row in rows], {"semantic_family_enabled": False, "raw_candidate_count": len(rows), "family_count": len(rows)}
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        family = _semantic_family(row)
        tags = _json_loads(row["pattern_tags_json"], [])
        note_ids = [int(item) for item in _json_loads(row["source_note_ids_json"], []) if str(item).isdigit()]
        metrics = _note_metrics(conn, note_ids)
        outcome_direction = _outcome_direction_bucket(metrics) if config.outcome_direction_split_enabled else {
            "outcome_direction_key": "not_split",
            "outcome_direction_label": "not split",
        }
        base_family_key = family["semantic_family_key"]
        family_key = f"{base_family_key}|{outcome_direction['outcome_direction_key']}" if config.outcome_direction_split_enabled else base_family_key
        sample_count = int(row["sample_count"] or 0)
        confidence = float(row["avg_confidence"] or 0)
        quality = float(row["avg_note_quality_score"] or 0)
        if family_key not in grouped:
            grouped[family_key] = {
                "candidate_key": family_key,
                "semantic_family_key": family_key,
                "semantic_family_label": family["semantic_family_label"],
                "semantic_family_signals": family["semantic_family_signals"],
                "base_semantic_family_key": base_family_key,
                "outcome_direction_key": outcome_direction["outcome_direction_key"],
                "outcome_direction_label": outcome_direction["outcome_direction_label"],
                "generated_at": row["generated_at"],
                "status": "semantic_family",
                "reason_category": family["semantic_family_label"],
                "pattern_tags_counter": Counter(),
                "sample_count": 0,
                "confidence_weighted_sum": 0.0,
                "quality_weighted_sum": 0.0,
                "threshold_candidates": [],
                "source_note_ids": set(),
                "child_candidate_keys": [],
                "child_candidate_count": 0,
                "outcome_direction_counter": Counter(),
            }
        item = grouped[family_key]
        weight = max(1, sample_count)
        item["sample_count"] += sample_count
        item["confidence_weighted_sum"] += confidence * weight
        item["quality_weighted_sum"] += quality * weight
        item["threshold_candidates"].append(str(row["threshold_candidate"] or ""))
        item["source_note_ids"].update(note_ids)
        item["child_candidate_keys"].append(str(row["candidate_key"] or ""))
        item["child_candidate_count"] += 1
        item["pattern_tags_counter"].update(str(tag) for tag in tags if tag is not None)
        item["outcome_direction_counter"].update([str(outcome_direction["outcome_direction_key"])])
    family_rows: list[dict[str, Any]] = []
    for item in grouped.values():
        sample_count = max(0, int(item["sample_count"]))
        weight = max(1, sample_count)
        tags = [tag for tag, _ in item["pattern_tags_counter"].most_common(12)]
        threshold_candidate = next((candidate for candidate in item["threshold_candidates"] if candidate), "")
        source_note_ids = sorted(int(note_id) for note_id in item["source_note_ids"])
        family_rows.append(
            {
                "candidate_key": item["candidate_key"],
                "generated_at": item["generated_at"],
                "status": item["status"],
                "reason_category": item["reason_category"],
                "pattern_tags_json": json.dumps(tags, ensure_ascii=False, sort_keys=True),
                "sample_count": sample_count if sample_count > 0 else len(source_note_ids),
                "avg_confidence": item["confidence_weighted_sum"] / weight,
                "avg_note_quality_score": item["quality_weighted_sum"] / weight,
                "threshold_candidate": threshold_candidate,
                "source_note_ids_json": json.dumps(source_note_ids, ensure_ascii=False),
                "semantic_family_key": item["semantic_family_key"],
                "semantic_family_label": item["semantic_family_label"],
                "semantic_family_signals": item["semantic_family_signals"],
                "base_semantic_family_key": item["base_semantic_family_key"],
                "outcome_direction_key": item["outcome_direction_key"],
                "outcome_direction_label": item["outcome_direction_label"],
                "outcome_direction_counts": dict(item["outcome_direction_counter"]),
                "child_candidate_keys": item["child_candidate_keys"],
                "child_candidate_count": item["child_candidate_count"],
            }
        )
    family_rows.sort(key=lambda row: (int(row.get("sample_count") or 0), float(row.get("avg_note_quality_score") or 0), float(row.get("avg_confidence") or 0)), reverse=True)
    return family_rows, {
        "semantic_family_enabled": True,
        "outcome_direction_split_enabled": bool(config.outcome_direction_split_enabled),
        "raw_candidate_count": len(rows),
        "family_count": len(family_rows),
        "top_family_sample_counts": {str(row["candidate_key"]): int(row.get("sample_count") or 0) for row in family_rows[:10]},
    }


def _candidate_refresh_state(conn: sqlite3.Connection) -> dict[str, Any]:
    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    if "m225_llm_note_threshold_candidates" not in tables:
        return {
            "candidate_count_total": 0,
            "latest_candidate_updated_at": None,
            "latest_candidate_generated_at": None,
            "m225_table_present": False,
        }
    row = conn.execute(
        """SELECT COUNT(*), MAX(updated_at), MAX(generated_at)
           FROM m225_llm_note_threshold_candidates"""
    ).fetchone()
    return {
        "candidate_count_total": int(row[0] or 0) if row else 0,
        "latest_candidate_updated_at": row[1] if row else None,
        "latest_candidate_generated_at": row[2] if row else None,
        "m225_table_present": True,
    }


def _adaptive_refresh(config: ThresholdCandidateEvaluatorConfig, refresh_state: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    base_interval = max(0.0, float(config.interval_seconds))
    candidate_count = int(refresh_state.get("candidate_count_total") or 0)
    latest_updated_at = refresh_state.get("latest_candidate_updated_at")
    previous_updated_at = state.get("last_candidate_updated_at")
    previous_count = int(state.get("last_candidate_count_total") or 0)
    changed = latest_updated_at != previous_updated_at or candidate_count != previous_count
    if not config.adaptive_refresh_enabled:
        return {
            "adaptive_refresh_enabled": False,
            "candidate_count_total": candidate_count,
            "changed_since_last_tick": changed,
            "next_interval_seconds": base_interval,
            "tier": "fixed",
            "latest_candidate_updated_at": latest_updated_at,
        }

    min_interval = max(0.0, float(config.adaptive_min_interval_seconds))
    max_interval = max(min_interval, float(config.adaptive_max_interval_seconds))
    if candidate_count >= int(config.adaptive_high_candidate_count):
        tier = "high_candidate_count"
        next_interval = min_interval
    elif changed:
        tier = "changed"
        next_interval = max(min_interval, min(max_interval, float(config.adaptive_changed_interval_seconds)))
    elif candidate_count > 0:
        tier = "unchanged"
        next_interval = max(min_interval, min(max_interval, float(config.adaptive_unchanged_interval_seconds)))
    else:
        tier = "empty"
        next_interval = max_interval
    return {
        "adaptive_refresh_enabled": True,
        "candidate_count_total": candidate_count,
        "changed_since_last_tick": changed,
        "next_interval_seconds": float(next_interval),
        "tier": tier,
        "latest_candidate_updated_at": latest_updated_at,
        "thresholds": {
            "high_candidate_count": int(config.adaptive_high_candidate_count),
            "min_interval_seconds": min_interval,
            "max_interval_seconds": max_interval,
            "changed_interval_seconds": float(config.adaptive_changed_interval_seconds),
            "unchanged_interval_seconds": float(config.adaptive_unchanged_interval_seconds),
        },
    }


def _note_metrics(conn: sqlite3.Connection, note_ids: list[int]) -> dict[str, Any]:
    if not note_ids:
        return {
            "source_note_count": 0,
            "direction_miss_rate": None,
            "avg_absolute_error": None,
            "avg_raw_return": None,
            "avg_raw_return_abs": None,
            "predicted_direction_counts": {},
            "actual_direction_counts": {},
            "direction_pair_counts": {},
            "horizon_counts": {},
            "symbol_counts": {},
            "event_family_counts": {},
        }
    placeholders = ",".join("?" for _ in note_ids)
    rows = conn.execute(
        f"""SELECT n.id AS note_id, n.confidence, n.note_quality_score,
                   o.direction_hit, o.absolute_error, o.raw_return, o.actual_direction, o.horizon_minutes,
                   p.symbol, p.event_family, p.predicted_direction
            FROM m223_live_learning_adjustment_notes n
            JOIN m223_live_learning_outcomes o ON o.id=n.outcome_id
            JOIN m223_live_learning_predictions p ON p.id=n.prediction_id
            WHERE n.id IN ({placeholders})""",
        tuple(note_ids),
    ).fetchall()
    direction_values = [int(row["direction_hit"]) for row in rows if row["direction_hit"] is not None]
    abs_errors = [float(row["absolute_error"]) for row in rows if row["absolute_error"] is not None]
    raw_returns = [float(row["raw_return"]) for row in rows if row["raw_return"] is not None]
    raw_abs = [abs(float(row["raw_return"])) for row in rows if row["raw_return"] is not None]
    predicted_counts = Counter(str(row["predicted_direction"] or "unknown") for row in rows)
    actual_counts = Counter(str(row["actual_direction"] or "unknown") for row in rows)
    pair_counts = Counter(f"{row['predicted_direction'] or 'unknown'}->{row['actual_direction'] or 'unknown'}" for row in rows)
    horizon_counts = Counter(str(row["horizon_minutes"]) for row in rows if row["horizon_minutes"] is not None)
    symbol_counts = Counter(str(row["symbol"] or "unknown") for row in rows)
    family_counts = Counter(str(row["event_family"] or "unknown") for row in rows)
    direction_miss_rate = None
    if direction_values:
        direction_miss_rate = sum(1 for item in direction_values if item == 0) / len(direction_values)
    return {
        "source_note_count": len(rows),
        "direction_miss_rate": direction_miss_rate,
        "avg_absolute_error": (sum(abs_errors) / len(abs_errors)) if abs_errors else None,
        "avg_raw_return": (sum(raw_returns) / len(raw_returns)) if raw_returns else None,
        "avg_raw_return_abs": (sum(raw_abs) / len(raw_abs)) if raw_abs else None,
        "predicted_direction_counts": dict(predicted_counts),
        "actual_direction_counts": dict(actual_counts),
        "direction_pair_counts": dict(pair_counts),
        "horizon_counts": dict(horizon_counts),
        "symbol_counts": dict(symbol_counts.most_common(10)),
        "event_family_counts": dict(family_counts),
    }


def _blocker_severity(blockers: list[str]) -> dict[str, Any]:
    hard = {
        "avg_note_quality_below_minimum",
        "avg_confidence_below_minimum",
        "source_note_coverage_incomplete",
        "estimated_error_reduction_too_small",
    }
    soft = {
        "sample_count_below_watch_minimum",
        "sample_count_below_promotion_minimum",
        "promotion_score_below_minimum",
    }
    diagnostic = {
        "regime_specific_instability",
        "direction_split_mixed",
    }
    severity = "none"
    if any(blocker in hard for blocker in blockers):
        severity = "hard"
    elif any(blocker in diagnostic for blocker in blockers):
        severity = "diagnostic"
    elif any(blocker in soft for blocker in blockers):
        severity = "soft"
    return {
        "severity": severity,
        "hard_blockers": [blocker for blocker in blockers if blocker in hard],
        "soft_blockers": [blocker for blocker in blockers if blocker in soft],
        "diagnostic_blockers": [blocker for blocker in blockers if blocker in diagnostic],
    }


def _score_candidate(row: sqlite3.Row | dict[str, Any], metrics: dict[str, Any], config: ThresholdCandidateEvaluatorConfig) -> dict[str, Any]:
    sample_count = int(row["sample_count"] or 0)
    source_note_count = int(metrics.get("source_note_count") or 0)
    avg_confidence = row["avg_confidence"]
    avg_quality = row["avg_note_quality_score"]
    avg_confidence_float = float(avg_confidence) if avg_confidence is not None else 0.0
    avg_quality_float = float(avg_quality) if avg_quality is not None else 0.0
    direction_miss_rate = metrics.get("direction_miss_rate")
    direction_miss_float = float(direction_miss_rate) if direction_miss_rate is not None else 0.0
    avg_abs_error = metrics.get("avg_absolute_error")
    avg_abs_error_float = float(avg_abs_error) if avg_abs_error is not None else 0.0

    sample_score = min(25.0, (sample_count / max(1, int(config.min_promotion_sample_count))) * 25.0)
    quality_score = min(25.0, (avg_quality_float / 100.0) * 25.0)
    confidence_score = min(20.0, avg_confidence_float * 20.0)
    repeat_score = min(15.0, direction_miss_float * 15.0)
    magnitude_score = min(15.0, avg_abs_error_float * 10000.0 / 8.0)
    promotion_score = round(sample_score + quality_score + confidence_score + repeat_score + magnitude_score, 2)

    estimated_error_reduction_bps = round(
        avg_abs_error_float * 10000.0 * min(0.35, max(0.05, (avg_quality_float / 100.0) * avg_confidence_float * 0.35)),
        4,
    )

    blockers: list[str] = []
    if sample_count < int(config.min_watch_sample_count):
        blockers.append("sample_count_below_watch_minimum")
    if sample_count < int(config.min_promotion_sample_count):
        blockers.append("sample_count_below_promotion_minimum")
    if source_note_count < sample_count:
        blockers.append("source_note_coverage_incomplete")
    if avg_confidence_float < float(config.min_avg_confidence):
        blockers.append("avg_confidence_below_minimum")
    if avg_quality_float < float(config.min_avg_note_quality_score):
        blockers.append("avg_note_quality_below_minimum")
    if estimated_error_reduction_bps < float(config.min_estimated_error_reduction_bps):
        blockers.append("estimated_error_reduction_too_small")
    if promotion_score < float(config.min_promotion_score):
        blockers.append("promotion_score_below_minimum")

    critical_blockers = {
        "avg_note_quality_below_minimum",
        "avg_confidence_below_minimum",
        "source_note_coverage_incomplete",
        "estimated_error_reduction_too_small",
    }
    sample_only_blockers = {
        "sample_count_below_watch_minimum",
        "sample_count_below_promotion_minimum",
        "promotion_score_below_minimum",
    }
    if any(blocker in blockers for blocker in critical_blockers):
        status = "reject"
    elif sample_count < int(config.min_promotion_sample_count):
        status = "needs_more_samples"
    elif blockers and set(blockers).issubset(sample_only_blockers):
        status = "watch"
    elif blockers:
        status = "shadow_candidate"
    else:
        status = "promotion_ready"
    severity = _blocker_severity(blockers)

    return {
        "candidate_key": row["candidate_key"],
        "semantic_family_key": row["semantic_family_key"] if _row_has(row, "semantic_family_key") else row["candidate_key"],
        "semantic_family_label": row["semantic_family_label"] if _row_has(row, "semantic_family_label") else row["reason_category"],
        "semantic_family_signals": row["semantic_family_signals"] if _row_has(row, "semantic_family_signals") else [],
        "base_semantic_family_key": row["base_semantic_family_key"] if _row_has(row, "base_semantic_family_key") else row["candidate_key"],
        "outcome_direction_key": row["outcome_direction_key"] if _row_has(row, "outcome_direction_key") else _outcome_direction_bucket(metrics)["outcome_direction_key"],
        "outcome_direction_label": row["outcome_direction_label"] if _row_has(row, "outcome_direction_label") else _outcome_direction_bucket(metrics)["outcome_direction_label"],
        "outcome_direction_counts": row["outcome_direction_counts"] if _row_has(row, "outcome_direction_counts") else {},
        "child_candidate_count": row["child_candidate_count"] if _row_has(row, "child_candidate_count") else 1,
        "child_candidate_keys": row["child_candidate_keys"] if _row_has(row, "child_candidate_keys") else [row["candidate_key"]],
        "status": status,
        "promotion_score": promotion_score,
        "sample_count": sample_count,
        "source_note_count": source_note_count,
        "avg_confidence": avg_confidence_float if avg_confidence is not None else None,
        "avg_note_quality_score": avg_quality_float if avg_quality is not None else None,
        "direction_miss_rate": direction_miss_rate,
        "avg_absolute_error": avg_abs_error,
        "estimated_error_reduction_bps": estimated_error_reduction_bps,
        "reason_category": row["reason_category"] or "",
        "pattern_tags": _json_loads(row["pattern_tags_json"], []),
        "threshold_candidate": row["threshold_candidate"] or "",
        "blockers": blockers,
        "blocker_severity": severity["severity"],
        "blocker_detail": severity,
        "supporting_metrics": metrics,
    }


def _blocker_counts(evaluations: list[dict[str, Any]]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for item in evaluations:
        counts.update(str(blocker) for blocker in item.get("blockers") or [])
    return dict(counts)


def _blocker_severity_counts(evaluations: list[dict[str, Any]]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for item in evaluations:
        counts.update([str(item.get("blocker_severity") or "none")])
    return dict(counts)


def _m225_focus_feedback(evaluations: list[dict[str, Any]], config: ThresholdCandidateEvaluatorConfig) -> dict[str, Any]:
    focusable = [
        item
        for item in evaluations
        if item.get("status") in {"needs_more_samples", "watch", "shadow_candidate"}
    ]
    focusable.sort(
        key=lambda item: (
            item.get("status") == "needs_more_samples",
            float(item.get("promotion_score") or 0),
            int(item.get("sample_count") or 0),
        ),
        reverse=True,
    )
    patterns = []
    for item in focusable[: max(1, int(config.max_focus_patterns))]:
        sample_count = int(item.get("sample_count") or 0)
        needed = max(0, int(config.min_promotion_sample_count) - sample_count)
        tags = [str(tag) for tag in item.get("pattern_tags") or []]
        signals = [str(signal) for signal in item.get("semantic_family_signals") or []]
        metrics = item.get("supporting_metrics") or {}
        horizon_counts = metrics.get("horizon_counts") or {}
        preferred_horizons = [
            horizon
            for horizon, _ in sorted(horizon_counts.items(), key=lambda pair: int(pair[1] or 0))
        ][:3]
        source_note_gap = max(0, int(item.get("sample_count") or 0) - int(item.get("source_note_count") or 0))
        patterns.append(
            {
                "semantic_family_key": item.get("semantic_family_key"),
                "base_semantic_family_key": item.get("base_semantic_family_key"),
                "outcome_direction_key": item.get("outcome_direction_key"),
                "semantic_family_label": item.get("semantic_family_label"),
                "status": item.get("status"),
                "blocker_severity": item.get("blocker_severity"),
                "blocker_detail": item.get("blocker_detail") or {},
                "promotion_score": item.get("promotion_score"),
                "sample_count": sample_count,
                "target_total_samples": int(config.min_promotion_sample_count),
                "target_additional_samples": needed,
                "source_note_gap": source_note_gap,
                "preferred_horizons": preferred_horizons,
                "focus_tags": tags[:8],
                "focus_signals": signals[:8],
                "suggested_m225_priority_reason": (
                    "candidate has useful note quality/effect but needs repeated samples before threshold promotion; "
                    "prioritize matching family, outcome direction, and any under-sampled horizons"
                ),
            }
        )
    return {
        "feedback_version": "m226-to-m225-focus-feedback-v1",
        "generated_at": _iso_now(),
        "focus_pattern_count": len(patterns),
        "focus_patterns": patterns,
        "selection_hint": "M225 should prioritize evaluated outcome gaps matching these semantic families/tags while keeping normal quality and safety filters.",
    }


def _store_evaluation(conn: sqlite3.Connection, item: dict[str, Any]) -> None:
    now = _iso_now()
    conn.execute(
        """INSERT OR REPLACE INTO m226_llm_threshold_candidate_evaluations(
            candidate_key,evaluated_at,evaluator_version,status,promotion_score,
            sample_count,source_note_count,avg_confidence,avg_note_quality_score,
            direction_miss_rate,avg_absolute_error,estimated_error_reduction_bps,
            reason_category,pattern_tags_json,threshold_candidate,blockers_json,
            supporting_metrics_json,created_at,updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            item["candidate_key"],
            now,
            M226_THRESHOLD_CANDIDATE_EVALUATOR_VERSION,
            item["status"],
            item["promotion_score"],
            item["sample_count"],
            item["source_note_count"],
            item["avg_confidence"],
            item["avg_note_quality_score"],
            item["direction_miss_rate"],
            item["avg_absolute_error"],
            item["estimated_error_reduction_bps"],
            item["reason_category"],
            json.dumps(item["pattern_tags"], ensure_ascii=False, sort_keys=True),
            item["threshold_candidate"],
            json.dumps(item["blockers"], ensure_ascii=False, sort_keys=True),
            json.dumps(item["supporting_metrics"], ensure_ascii=False, sort_keys=True),
            now,
            now,
        ),
    )


def _shadow_trial_for_item(conn: sqlite3.Connection, item: dict[str, Any], config: ThresholdCandidateEvaluatorConfig) -> dict[str, Any]:
    prior_row = conn.execute(
        "SELECT COUNT(*) FROM m227_shadow_promotion_trials WHERE candidate_key=?",
        (str(item.get("candidate_key") or ""),),
    ).fetchone()
    cycles_seen = int(prior_row[0] or 0) + 1 if prior_row else 1
    promotion_score = float(item.get("promotion_score") or 0.0)
    estimated = float(item.get("estimated_error_reduction_bps") or 0.0)
    direction_miss = item.get("direction_miss_rate")
    miss_float = float(direction_miss) if direction_miss is not None else 0.0
    sample_count = int(item.get("sample_count") or 0)
    cycle_score = min(15.0, (cycles_seen / max(1, int(config.min_shadow_trial_cycles))) * 15.0)
    repeat_score = min(10.0, (sample_count / max(1, int(config.min_promotion_sample_count))) * 10.0)
    miss_score = min(10.0, miss_float * 10.0)
    acceptance_score = round(min(100.0, promotion_score * 0.65 + cycle_score + repeat_score + miss_score), 2)
    hard_blocked = (item.get("blocker_severity") == "hard")
    ready_for_real_promotion = (
        item.get("status") == "promotion_ready"
        and not hard_blocked
        and cycles_seen >= int(config.min_shadow_trial_cycles)
        and acceptance_score >= float(config.min_shadow_acceptance_score)
    )
    if item.get("status") != "promotion_ready":
        trial_status = "not_trial_eligible"
    elif ready_for_real_promotion:
        trial_status = "shadow_trial_passed_not_promoted"
    else:
        trial_status = "shadow_trial_active"
    return {
        "candidate_key": item.get("candidate_key"),
        "semantic_family_key": item.get("semantic_family_key"),
        "base_semantic_family_key": item.get("base_semantic_family_key"),
        "outcome_direction_key": item.get("outcome_direction_key"),
        "trial_status": trial_status,
        "cycles_seen": cycles_seen,
        "promotion_score": promotion_score,
        "sample_count": sample_count,
        "direction_miss_rate": direction_miss,
        "avg_absolute_error": item.get("avg_absolute_error"),
        "estimated_error_reduction_bps": estimated,
        "acceptance_score": acceptance_score,
        "ready_for_real_promotion": bool(ready_for_real_promotion),
        "safety_note": "shadow-only; no live scorer threshold table was mutated",
    }


def _store_shadow_trial(conn: sqlite3.Connection, trial: dict[str, Any]) -> None:
    now = _iso_now()
    conn.execute(
        """INSERT INTO m227_shadow_promotion_trials(
            candidate_key,created_at,evaluator_version,trial_status,cycles_seen,
            promotion_score,sample_count,direction_miss_rate,avg_absolute_error,
            estimated_error_reduction_bps,acceptance_score,ready_for_real_promotion,
            trial_payload_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            str(trial.get("candidate_key") or ""),
            now,
            M226_THRESHOLD_CANDIDATE_EVALUATOR_VERSION,
            str(trial.get("trial_status") or ""),
            int(trial.get("cycles_seen") or 0),
            trial.get("promotion_score"),
            int(trial.get("sample_count") or 0),
            trial.get("direction_miss_rate"),
            trial.get("avg_absolute_error"),
            trial.get("estimated_error_reduction_bps"),
            trial.get("acceptance_score"),
            1 if trial.get("ready_for_real_promotion") else 0,
            json.dumps(trial, ensure_ascii=False, sort_keys=True),
        ),
    )


def _shadow_promotion_trial_summary(trials: list[dict[str, Any]], config: ThresholdCandidateEvaluatorConfig) -> dict[str, Any]:
    status_counts = Counter(str(item.get("trial_status") or "unknown") for item in trials)
    ready = [item for item in trials if item.get("ready_for_real_promotion")]
    active = [item for item in trials if item.get("trial_status") == "shadow_trial_active"]
    top = sorted(
        trials,
        key=lambda item: (float(item.get("acceptance_score") or 0.0), int(item.get("cycles_seen") or 0)),
        reverse=True,
    )[:10]
    return {
        "shadow_trial_version": "m227-shadow-promotion-trial-v1",
        "enabled": bool(config.shadow_promotion_trial_enabled),
        "trial_count": len(trials),
        "active_count": len(active),
        "ready_for_real_promotion_count": len(ready),
        "status_counts": dict(status_counts),
        "min_shadow_trial_cycles": int(config.min_shadow_trial_cycles),
        "min_shadow_acceptance_score": float(config.min_shadow_acceptance_score),
        "top_trials": top,
        "safety": {
            "live_threshold_table_mutated": False,
            "real_promotion_performed": False,
        },
    }


def _latest_trial_history(conn: sqlite3.Connection, candidate_key: str, limit: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT created_at, trial_status, cycles_seen, promotion_score, sample_count,
                  direction_miss_rate, avg_absolute_error, estimated_error_reduction_bps,
                  acceptance_score, ready_for_real_promotion
           FROM m227_shadow_promotion_trials
           WHERE candidate_key=?
           ORDER BY id DESC
           LIMIT ?""",
        (candidate_key, max(1, int(limit))),
    ).fetchall()
    return [{key: row[key] for key in row.keys()} for row in rows]


def _gate_check(passed: bool, reason: str, details: dict[str, Any] | None = None, *, severity: str = "hard") -> dict[str, Any]:
    return {
        "passed": bool(passed),
        "reason": "ok" if passed else str(reason),
        "severity": "none" if passed else str(severity or "hard"),
        "details": details or {},
    }


def _candidate_gate_dependencies(item: dict[str, Any]) -> dict[str, Any]:
    text = " ".join(
        [
            str(item.get("semantic_family_key") or ""),
            str(item.get("base_semantic_family_key") or ""),
            str(item.get("outcome_direction_key") or ""),
            str(item.get("threshold_candidate") or ""),
            " ".join(str(tag) for tag in item.get("pattern_tags") or []),
            " ".join(str(signal) for signal in item.get("semantic_family_signals") or []),
        ]
    ).lower()
    required_sources = {"market_news", "market_data"}
    profile = "general_market_news"
    option_required = False
    option_relevant = any(token in text for token in ("option", "gamma", "pin", "max pain", "wall", "oi", "spread"))
    if "timestamp_alignment" in text or "late_baseline" in text or "baseline is late" in text:
        profile = "timestamp_alignment_late_baseline"
    if any(token in text for token in ("macro", "economic", "cpi", "payroll", "fomc", "fred", "inflation", "rates")):
        profile = "macro_headline_context"
        required_sources.update({"economic_indicators"})
    if any(token in text for token in ("geopolitical", "war", "tariff", "iran", "china", "taiwan", "russia", "ukraine", "middle east")):
        profile = "geopolitical_context"
        required_sources.update({"geopolitical_signals", "standing_regimes"})
    if any(token in text for token in ("asset_map", "asset mapping", "sector rotation", "cross asset")):
        profile = "asset_mapping_relevance"
    if option_relevant or any(token in text for token in ("flat_misread", "neutral_flat", "neutral", "pinning")):
        profile = "option_structure_quality" if option_relevant else profile
        option_required = True
    return {
        "dependency_profile": profile,
        "required_sources": sorted(required_sources),
        "option_structure_relevant": bool(option_relevant or option_required),
        "option_structure_required": bool(option_required),
        "option_structure_weight": "required" if option_required else ("optional" if option_relevant else "none"),
    }


def _source_status_map(payload: dict[str, Any]) -> dict[str, str]:
    source_map: dict[str, str] = {}
    for item in payload.get("sources") or []:
        if not isinstance(item, dict):
            continue
        key = str(item.get("source_key") or item.get("key") or "")
        if key:
            source_map[key] = str(item.get("status") or item.get("freshness_status") or "").lower()
    for key in payload.get("stale_source_keys") or []:
        source_map[str(key)] = "stale"
    for key in payload.get("lagging_source_keys") or []:
        source_map.setdefault(str(key), "lagging")
    return source_map


def _check_evidence_freshness(payload: dict[str, Any], dependencies: dict[str, Any] | None = None) -> dict[str, Any]:
    dependencies = dependencies or {}
    required_sources = [str(item) for item in dependencies.get("required_sources") or []]
    if not payload:
        return _gate_check(
            False,
            "evidence_freshness_missing",
            {"required_sources": required_sources},
            severity="hard",
        )
    overall = str(payload.get("overall_status") or "").lower()
    guard = payload.get("scorer_guard") or {}
    action = str(guard.get("action") or "").lower()
    stale = payload.get("stale_source_keys") or []
    lagging = payload.get("lagging_source_keys") or []
    source_status = _source_status_map(payload)
    missing_required = [key for key in required_sources if key not in source_status and (stale or lagging or payload.get("sources"))]
    bad_required = [
        key
        for key in required_sources
        if source_status.get(key) in {"stale", "lagging", "missing", "red", "error", "offline"}
    ]
    details = {
        "dependency_profile": dependencies.get("dependency_profile"),
        "required_sources": required_sources,
        "overall_status": payload.get("overall_status"),
        "scorer_guard_action": guard.get("action"),
        "stale_source_keys": stale,
        "lagging_source_keys": lagging,
        "missing_required_sources": missing_required,
        "bad_required_sources": bad_required,
    }
    if action not in {"normal", ""}:
        return _gate_check(False, "evidence_guard_action_blocks_promotion", details, severity="hard")
    if missing_required or bad_required:
        return _gate_check(False, "required_evidence_sources_not_fresh", details, severity="hard")
    if overall not in {"green", ""} or stale or lagging:
        return _gate_check(False, "non_required_evidence_sources_lagging", details, severity="soft")
    return _gate_check(True, "ok", details)


def _check_portfolio_drawdown(payload: dict[str, Any]) -> dict[str, Any]:
    if not payload:
        return _gate_check(False, "portfolio_risk_state_missing", {}, severity="hard")
    data = payload.get("result") if isinstance(payload.get("result"), dict) else payload
    risk_state = str(data.get("risk_state") or "").lower()
    recommended = str(data.get("recommended_action") or "").lower()
    breaches = data.get("breaches") or []
    warnings = data.get("warnings") or []
    passed = risk_state in {"within_limits", "ok", "green"} and recommended in {"allow_new_entries", "allow", "normal", ""} and not breaches
    return _gate_check(
        passed,
        "portfolio_drawdown_or_risk_state_blocks_promotion",
        {
            "risk_state": data.get("risk_state"),
            "recommended_action": data.get("recommended_action"),
            "breach_count": len(breaches),
            "warning_count": len(warnings),
            "metrics": data.get("metrics") or {},
        },
        severity="hard",
    )


def _option_confidence_diagnostics(options_payload: dict[str, Any], supervisor_payload: dict[str, Any]) -> dict[str, Any]:
    summary = options_payload.get("summary") or {}
    confidence_counts = summary.get("confidence_counts") or {}
    total_confidence = sum(int(value or 0) for value in confidence_counts.values()) if isinstance(confidence_counts, dict) else 0
    low_count = int((confidence_counts or {}).get("low") or 0)
    medium_count = int((confidence_counts or {}).get("medium") or 0)
    high_count = int((confidence_counts or {}).get("high") or 0)
    symbols = options_payload.get("symbols") or []
    missing_oi = summary.get("missing_open_interest_symbols") or []
    no_bid_ask = summary.get("no_bid_ask_symbols") or summary.get("missing_bid_ask_symbols") or []
    wide_spread = summary.get("wide_spread_symbols") or []
    thin_quote = summary.get("thin_quote_symbols") or []
    warnings: list[str] = []
    for symbol in symbols:
        if isinstance(symbol, dict):
            warnings.extend(str(item) for item in symbol.get("warnings") or [])
            if symbol.get("missing_open_interest"):
                missing_oi.append(symbol.get("symbol"))
            if symbol.get("bid_ask_missing") or symbol.get("missing_bid_ask"):
                no_bid_ask.append(symbol.get("symbol"))
            if symbol.get("wide_spread"):
                wide_spread.append(symbol.get("symbol"))
    supervisor_status = str(supervisor_payload.get("final_status") or supervisor_payload.get("status") or "").lower() if supervisor_payload else ""
    quote_quality = str(supervisor_payload.get("final_quote_quality_status") or "").lower() if supervisor_payload else ""
    optional_quality = str(supervisor_payload.get("final_optional_index_quote_quality_status") or "").lower() if supervisor_payload else ""
    action_counts = supervisor_payload.get("final_action_status_counts") or {}
    failure_counts = supervisor_payload.get("final_failure_reason_counts") or {}
    family_issue_summary = supervisor_payload.get("m220_family_issue_summary") or {}
    coverage_matrix = supervisor_payload.get("index_option_coverage_matrix") or {}
    family_repair_plan = supervisor_payload.get("m220_family_repair_plan") or coverage_matrix.get("family_repair_plan") or []
    root_causes: list[str] = []
    if missing_oi or any("open_interest" in item for item in warnings):
        root_causes.append("open_interest_missing_or_volume_proxy")
    if no_bid_ask or quote_quality in {"missing", "red"}:
        root_causes.append("bid_ask_missing_or_unusable")
    if wide_spread or any("spread" in item for item in warnings):
        root_causes.append("wide_spread_or_liquidity_thin")
    if thin_quote or any("thin" in item for item in warnings):
        root_causes.append("thin_option_quote_sample")
    if action_counts.get("deferred_until_regular_session") or str(supervisor_payload.get("final_session") or "").lower() in {"pre_market", "after_hours", "closed"}:
        root_causes.append("regular_session_dependency")
    if optional_quality in {"missing", "red"} or family_issue_summary.get("issue_category_counts") or family_repair_plan:
        root_causes.append("index_or_family_coverage_gap")
    if failure_counts:
        root_causes.append("supervisor_failure_reason_present")
    if supervisor_status in {"red", "failed", "error"}:
        root_causes.append("supervisor_status_not_green")
    if low_count > 0 and medium_count + high_count == 0 and not root_causes:
        root_causes.append("low_confidence_without_explicit_source_breakdown")
    return {
        "diagnostic_version": "m229-option-confidence-diagnostics-v1",
        "confidence_counts": confidence_counts,
        "confidence_total": total_confidence,
        "low_confidence_count": low_count,
        "medium_high_confidence_count": medium_count + high_count,
        "root_cause_tags": sorted(set(root_causes)),
        "missing_open_interest_symbols": sorted(set(str(item) for item in missing_oi if item)),
        "missing_bid_ask_symbols": sorted(set(str(item) for item in no_bid_ask if item)),
        "wide_spread_symbols": sorted(set(str(item) for item in wide_spread if item)),
        "thin_quote_symbols": sorted(set(str(item) for item in thin_quote if item)),
        "symbol_warnings": sorted(set(warnings))[:20],
        "supervisor_status": supervisor_status,
        "quote_quality_status": quote_quality,
        "optional_index_quote_quality_status": optional_quality,
        "action_status_counts": action_counts,
        "failure_reason_counts": failure_counts,
        "family_issue_summary": family_issue_summary,
        "family_repair_plan_sample": family_repair_plan[:5] if isinstance(family_repair_plan, list) else [],
    }


def _check_option_structure(
    item: dict[str, Any],
    options_payload: dict[str, Any],
    supervisor_payload: dict[str, Any],
    dependencies: dict[str, Any] | None = None,
) -> dict[str, Any]:
    dependencies = dependencies or {}
    text = " ".join(
        [
            str(item.get("semantic_family_key") or ""),
            str(item.get("threshold_candidate") or ""),
            " ".join(str(tag) for tag in item.get("pattern_tags") or []),
            " ".join(str(signal) for signal in item.get("semantic_family_signals") or []),
        ]
    ).lower()
    option_relevant = bool(dependencies.get("option_structure_relevant")) or any(token in text for token in ("option", "gamma", "pin", "max pain", "wall", "oi", "spread"))
    option_required = bool(dependencies.get("option_structure_required"))
    base_details = {
        "option_relevant": option_relevant,
        "option_required": option_required,
        "dependency_profile": dependencies.get("dependency_profile"),
        "option_structure_weight": dependencies.get("option_structure_weight"),
    }
    if not option_relevant:
        return _gate_check(True, "not_option_structure_candidate", base_details)
    if not options_payload:
        return _gate_check(
            False,
            "option_structure_missing" if option_required else "option_structure_optional_unavailable",
            base_details,
            severity="hard" if option_required else "soft",
        )
    status = str(options_payload.get("status") or "").lower()
    session_guard = options_payload.get("session_guard") or {}
    freshness_report = _option_structure_freshness(options_payload, supervisor_payload or {})
    freshness = str(freshness_report.get("status") or "").lower()
    summary = options_payload.get("summary") or {}
    confidence_counts = summary.get("confidence_counts") or {}
    gamma_counts = summary.get("gamma_regime_counts") or {}
    pin_risk_count = int(summary.get("pin_risk_symbol_count") or summary.get("pin_risk_count") or 0)
    proximity_alert_count = int(summary.get("proximity_alert_symbol_count") or summary.get("proximity_alert_count") or 0)
    low_count = int(confidence_counts.get("low") or 0)
    medium_count = int(confidence_counts.get("medium") or 0)
    high_count = int(confidence_counts.get("high") or 0)
    supervisor_status = str(supervisor_payload.get("final_status") or supervisor_payload.get("status") or "").lower() if supervisor_payload else ""
    quality_status = str(supervisor_payload.get("final_quote_quality_status") or "").lower() if supervisor_payload else ""
    failed_actions = int(supervisor_payload.get("final_failed_action_count") or supervisor_payload.get("failed_action_count") or 0) if supervisor_payload else 0
    confidence_diagnostics = _option_confidence_diagnostics(options_payload, supervisor_payload)
    details = {
        **base_details,
        "options_status": options_payload.get("status"),
        "freshness_status": freshness,
        "session_guard_freshness_status": session_guard.get("freshness_status"),
        "freshness": freshness_report,
        "confidence_counts": confidence_counts,
        "option_confidence_diagnostics": confidence_diagnostics,
        "gamma_regime_counts": gamma_counts,
        "pin_risk_symbol_count": pin_risk_count,
        "proximity_alert_symbol_count": proximity_alert_count,
        "supervisor_status": supervisor_status,
        "quality_status": quality_status,
        "failed_actions": failed_actions,
    }
    if status in {"red", "failed", "error"}:
        return _gate_check(False, "option_structure_status_red", details, severity="hard" if option_required else "soft")
    if freshness == "expected_deferred_stale":
        return _gate_check(False, "option_structure_expected_deferred_stale", details, severity="soft")
    if freshness and freshness != "fresh":
        return _gate_check(False, "option_structure_stale", details, severity="hard" if option_required else "soft")
    if failed_actions > 0 or supervisor_status in {"red", "failed", "error"} or quality_status in {"red", "failed", "error"}:
        return _gate_check(False, "option_structure_supervisor_failed", details, severity="hard")
    if high_count + medium_count <= 0:
        return _gate_check(False, "option_structure_low_confidence", details, severity="soft")
    negative_gamma = int(gamma_counts.get("negative") or gamma_counts.get("negative_gamma") or 0)
    positive_gamma = int(gamma_counts.get("positive") or gamma_counts.get("positive_gamma") or 0)
    flat_candidate = any(token in text for token in ("flat", "neutral", "mean-reversion", "mean reversion", "pin"))
    if flat_candidate and negative_gamma > positive_gamma:
        return _gate_check(False, "option_structure_negative_gamma_conflict", details, severity="soft")
    if flat_candidate and (pin_risk_count > 0 or proximity_alert_count > 0):
        return _gate_check(False, "option_structure_pin_risk_conflict", details, severity="soft")
    return _gate_check(True, "option_structure_ok", details)


def _promotion_gate_detail_reason_counts(reviews: list[dict[str, Any]], reason_field: str) -> Counter[str]:
    counts: Counter[str] = Counter()
    for item in reviews:
        checks = item.get("checks") or {}
        for check_name in item.get(reason_field) or []:
            check = checks.get(str(check_name)) or {}
            reason = str(check.get("reason") or check_name)
            counts.update([reason])
    return counts


def _promotion_gate_requirement_for_check(check_name: str, check: dict[str, Any]) -> dict[str, Any]:
    reason = str(check.get("reason") or "")
    details = check.get("details") or {}
    severity = str(check.get("severity") or "hard")
    requirement = "check must pass before this candidate can move to promotion_waitlist"
    target: Any = "pass"
    current: Any = reason
    if check_name == "option_structure_conflict":
        confidence = details.get("confidence_counts") or {}
        if reason == "option_structure_low_confidence":
            requirement = "option structure snapshot needs at least one medium/high confidence family before waitlist"
            current = confidence
            target = {"medium_or_high_confidence_family_count": ">= 1"}
        elif reason == "option_structure_stale":
            requirement = "fresh option market-structure snapshot is required before waitlist"
            current = details.get("freshness_status")
            target = "fresh"
        elif reason == "option_structure_expected_deferred_stale":
            requirement = "option market is outside regular session; wait for regular-session option refresh before waitlist"
            current = details.get("freshness_status")
            target = "fresh_after_regular_session_refresh"
        elif reason in {"option_structure_missing", "option_structure_optional_unavailable"}:
            requirement = "option market-structure artifact must be produced before waitlist"
            current = "missing"
            target = "available"
        elif reason == "option_structure_supervisor_failed":
            requirement = "option structure supervisor must be green with zero failed actions"
            current = {
                "supervisor_status": details.get("supervisor_status"),
                "quality_status": details.get("quality_status"),
                "failed_actions": details.get("failed_actions"),
            }
            target = {"supervisor_status": "green/yellow", "failed_actions": 0}
        elif reason == "option_structure_negative_gamma_conflict":
            requirement = "negative gamma conflict must clear, or candidate should become regime-conditional instead of globally promoted"
            current = details.get("gamma_regime_counts") or {}
            target = "no negative-gamma contradiction for this candidate family"
        elif reason == "option_structure_pin_risk_conflict":
            requirement = "pin/proximity risk must clear, or candidate should become wall-aware/conditional"
            current = {
                "pin_risk_symbol_count": details.get("pin_risk_symbol_count"),
                "proximity_alert_symbol_count": details.get("proximity_alert_symbol_count"),
            }
            target = {"pin_risk_symbol_count": 0, "proximity_alert_symbol_count": 0}
        elif reason == "option_structure_status_red":
            requirement = "option market-structure desk status must recover from red/error"
            current = details.get("options_status")
            target = "green/yellow"
    elif check_name == "evidence_freshness":
        requirement = "all required evidence sources for this candidate family must be fresh and scorer guard must be normal"
        current = {
            "overall_status": details.get("overall_status"),
            "scorer_guard_action": details.get("scorer_guard_action"),
            "missing_required_sources": details.get("missing_required_sources") or [],
            "bad_required_sources": details.get("bad_required_sources") or [],
            "stale_source_keys": details.get("stale_source_keys") or [],
            "lagging_source_keys": details.get("lagging_source_keys") or [],
        }
        target = {"required_sources": details.get("required_sources") or [], "scorer_guard_action": "normal"}
    elif check_name == "recent_shadow_performance":
        requirement = "shadow trial needs enough cycles and recent acceptance score above policy threshold"
        current = {
            "cycles_seen": details.get("cycles_seen"),
            "avg_recent_acceptance_score": details.get("avg_recent_acceptance_score"),
            "recent_score_count": details.get("recent_score_count"),
        }
        target = {
            "required_cycles": details.get("required_cycles"),
            "required_acceptance_score": details.get("required_acceptance_score"),
        }
    elif check_name == "candidate_drawdown_worsening":
        requirement = "candidate needs enough recent error history and no drawdown/absolute-error worsening"
        current = {
            "latest_avg_absolute_error": details.get("latest_avg_absolute_error"),
            "prior_avg_absolute_error": details.get("prior_avg_absolute_error"),
            "recent_error_count": details.get("recent_error_count"),
        }
        target = {"max_worsening_pct": details.get("max_worsening_pct")}
    elif check_name == "regime_bias":
        requirement = "candidate sample base must be less concentrated in a single event/regime family"
        current = {
            "dominant_event_family": details.get("dominant_event_family"),
            "dominant_event_family_share": details.get("dominant_event_family_share"),
        }
        target = {"max_allowed_share": details.get("max_allowed_share")}
    elif check_name == "minimum_sample_count":
        requirement = "candidate needs the minimum Promotion Gate sample count"
        current = {"sample_count": details.get("sample_count")}
        target = {"minimum": details.get("minimum")}
    elif check_name == "portfolio_drawdown_risk":
        requirement = "portfolio risk state must allow new entries before any threshold promotion"
        current = {
            "risk_state": details.get("risk_state"),
            "recommended_action": details.get("recommended_action"),
            "breach_count": details.get("breach_count"),
        }
        target = {"risk_state": "within_limits", "recommended_action": "allow_new_entries", "breach_count": 0}
    return {
        "check": check_name,
        "reason": reason,
        "severity": severity,
        "requirement": requirement,
        "current": current,
        "target": target,
    }


def _promotion_gate_requirements(checks: dict[str, Any], failed: list[str]) -> list[dict[str, Any]]:
    return [
        _promotion_gate_requirement_for_check(str(check_name), checks.get(str(check_name)) or {})
        for check_name in failed
    ]


def _promotion_gate_history_state(
    conn: sqlite3.Connection,
    candidate_key: str,
    current_status: str,
    reviewed_at: str,
    config: ThresholdCandidateEvaluatorConfig,
) -> dict[str, Any]:
    rows = conn.execute(
        """SELECT reviewed_at, gate_status
           FROM m228_threshold_promotion_gate_reviews
           WHERE candidate_key=?
           ORDER BY id DESC
           LIMIT ?""",
        (candidate_key, max(1, int(config.promotion_gate_transition_history_limit))),
    ).fetchall()
    history = [{"reviewed_at": row["reviewed_at"], "gate_status": row["gate_status"]} for row in rows]
    previous_status = str(history[0]["gate_status"]) if history else None
    consecutive_current = 1
    first_current_at = reviewed_at
    for item in history:
        if str(item.get("gate_status") or "") != current_status:
            break
        consecutive_current += 1
        first_current_at = str(item.get("reviewed_at") or first_current_at)
    first_dt = _parse_iso_datetime(first_current_at)
    now_dt = _parse_iso_datetime(reviewed_at)
    age_seconds = None
    if first_dt is not None and now_dt is not None:
        age_seconds = max(0.0, (now_dt - first_dt).total_seconds())
    stale_ticks = consecutive_current >= int(config.promotion_gate_watchlist_stale_ticks)
    return {
        "previous_gate_status": previous_status,
        "transition_type": (
            "new_candidate" if previous_status is None else ("unchanged" if previous_status == current_status else f"{previous_status}_to_{current_status}")
        ),
        "review_history_count": len(history) + 1,
        "status_age_review_count": consecutive_current,
        "status_first_seen_at": first_current_at,
        "status_age_seconds": age_seconds,
        "watchlist_stale": bool(current_status == "promotion_watchlist" and stale_ticks),
        "watchlist_stale_threshold_ticks": int(config.promotion_gate_watchlist_stale_ticks),
        "recent_status_path": [current_status] + [str(item.get("gate_status") or "") for item in history[:9]],
    }


def _conditional_promotion_plan(
    gate_status: str,
    dependencies: dict[str, Any],
    checks: dict[str, Any],
    requirements: list[dict[str, Any]],
) -> dict[str, Any]:
    option_check = checks.get("option_structure_conflict") or {}
    evidence_check = checks.get("evidence_freshness") or {}
    option_reason = str(option_check.get("reason") or "")
    evidence_reason = str(evidence_check.get("reason") or "")
    conditions: list[dict[str, Any]] = []
    if dependencies.get("option_structure_relevant"):
        conditions.append(
            {
                "source": "option_structure",
                "condition": "fresh option structure with medium/high confidence and no supervisor failure",
                "current_reason": option_reason,
                "required": bool(dependencies.get("option_structure_required")),
            }
        )
    required_sources = [str(item) for item in dependencies.get("required_sources") or []]
    if required_sources:
        conditions.append(
            {
                "source": "evidence_freshness",
                "condition": "required evidence sources fresh and scorer guard normal",
                "current_reason": evidence_reason,
                "required_sources": required_sources,
            }
        )
    for requirement in requirements:
        reason = str(requirement.get("reason") or "")
        if reason in {"option_structure_negative_gamma_conflict", "option_structure_pin_risk_conflict"}:
            conditions.append(
                {
                    "source": "market_structure_regime",
                    "condition": "apply only when gamma/pin regime is compatible with the candidate",
                    "current_reason": reason,
                }
            )
    has_conditions = bool(conditions)
    mode = "not_eligible"
    if gate_status == "promotion_waitlist":
        mode = "conditional_waitlist" if has_conditions else "unconditional_waitlist"
    elif gate_status == "promotion_watchlist":
        mode = "conditional_watchlist"
    elif gate_status == "promotion_gate_blocked":
        mode = "blocked"
    return {
        "eligible_for_conditional_promotion_queue": gate_status in {"promotion_waitlist", "promotion_watchlist"} and has_conditions,
        "mode": mode,
        "conditions": conditions,
        "requirement_count": len(requirements),
        "blocked_from_unconditional_promotion": bool(has_conditions or gate_status != "promotion_waitlist"),
        "safety_note": "conditional plan is advisory only; no live threshold mutation is performed",
    }


def _promotion_gate_stale_policy(
    gate_status: str,
    history_state: dict[str, Any],
    detail_reasons: dict[str, Any],
    config: ThresholdCandidateEvaluatorConfig,
) -> dict[str, Any]:
    age = int(history_state.get("status_age_review_count") or 0)
    stale_ticks = int(config.promotion_gate_watchlist_stale_ticks)
    recycle_ticks = int(config.promotion_gate_watchlist_recycle_ticks)
    detail_values = {str(value) for value in (detail_reasons or {}).values()}
    operational_status = gate_status
    severity = "green"
    action = "none"
    recommendation = "No stale-watchlist action is required."
    recycle_recommended = False
    refresh_required = False
    if gate_status == "promotion_watchlist" and age >= recycle_ticks:
        operational_status = "promotion_recycle_candidate"
        severity = "red"
        action = "recycle_to_m225_m226"
        recycle_recommended = True
        recommendation = (
            "Temporarily exclude this candidate from promotion waitlist consideration and recycle it into M225/M226 learning "
            "until fresh evidence or option confidence changes the blocker."
        )
    elif gate_status == "promotion_watchlist" and age >= stale_ticks:
        operational_status = "promotion_stale_watchlist"
        severity = "yellow"
        action = "refresh_blocking_evidence"
        recommendation = "Refresh the blocking evidence source before keeping this candidate in active watchlist."
    if (
        "option_structure_low_confidence" in detail_values
        or "option_structure_stale" in detail_values
        or "option_structure_expected_deferred_stale" in detail_values
    ):
        refresh_required = gate_status == "promotion_watchlist" and age >= stale_ticks
        if refresh_required and not recycle_recommended:
            action = "refresh_option_market_structure"
            recommendation = (
                "Run M218/M219 option market-structure refresh; require at least one medium/high confidence option family "
                "before this candidate can move to waitlist."
            )
    return {
        "policy_version": "m229-stale-watchlist-policy-v1",
        "operational_gate_status": operational_status,
        "severity": severity,
        "action": action,
        "recommendation": recommendation,
        "status_age_review_count": age,
        "stale_threshold_ticks": stale_ticks,
        "recycle_threshold_ticks": recycle_ticks,
        "watchlist_stale": bool(gate_status == "promotion_watchlist" and age >= stale_ticks),
        "recycle_recommended": recycle_recommended,
        "option_refresh_required": bool(refresh_required),
        "temporary_promotion_exclusion": bool(recycle_recommended),
    }


def _option_root_cause_response_policy(
    diagnostics: dict[str, Any],
    config: ThresholdCandidateEvaluatorConfig,
) -> dict[str, Any]:
    roots = {str(item) for item in diagnostics.get("root_cause_tags") or []}
    actions: list[str] = []
    block_reasons: list[str] = []
    confidence_multiplier = 1.0
    confidence_cap = 1.0
    if "bid_ask_missing_or_unusable" in roots:
        actions.append("block_option_structure_dependent_promotion")
        block_reasons.append("bid_ask_missing_or_unusable")
        confidence_multiplier = min(confidence_multiplier, 0.0)
        confidence_cap = min(confidence_cap, 0.0)
    if "wide_spread_or_liquidity_thin" in roots:
        actions.append("apply_liquidity_spread_haircut")
        confidence_multiplier = min(confidence_multiplier, 0.50)
        confidence_cap = min(confidence_cap, 0.65)
    if "open_interest_missing_or_volume_proxy" in roots:
        actions.append("require_volume_proxy_and_lower_oi_weight")
        confidence_multiplier = min(confidence_multiplier, 0.75)
        confidence_cap = min(confidence_cap, 0.75)
    if "index_or_family_coverage_gap" in roots:
        actions.append("require_index_family_coverage_repair_or_proxy")
        confidence_multiplier = min(confidence_multiplier, 0.70)
        confidence_cap = min(confidence_cap, 0.70)
    if "regular_session_dependency" in roots:
        actions.append("defer_return_until_regular_session_snapshot")
        confidence_multiplier = min(confidence_multiplier, 0.60)
        confidence_cap = min(confidence_cap, 0.60)
    if "supervisor_failure_reason_present" in roots or "supervisor_status_not_green" in roots:
        actions.append("block_until_supervisor_green")
        block_reasons.append("option_supervisor_not_green")
        confidence_multiplier = min(confidence_multiplier, 0.0)
        confidence_cap = min(confidence_cap, 0.0)
    if "thin_option_quote_sample" in roots:
        actions.append("require_additional_quote_samples")
        confidence_multiplier = min(confidence_multiplier, 0.80)
        confidence_cap = min(confidence_cap, 0.80)
    if not actions:
        actions.append("no_root_cause_haircut")
    enabled = bool(config.promotion_gate_option_root_cause_policy_enabled)
    return {
        "policy_version": "m230-option-root-cause-response-policy-v1",
        "enabled": enabled,
        "root_causes": sorted(roots),
        "actions": actions,
        "block_reasons": block_reasons,
        "blocks_return": bool(enabled and block_reasons),
        "confidence_multiplier": confidence_multiplier if enabled else 1.0,
        "confidence_cap": confidence_cap if enabled else 1.0,
        "interpretation": (
            "Root-cause policy is advisory and only affects Promotion Gate/scorer evidence handling; "
            "it does not mutate live thresholds or submit broker orders."
        ),
    }


def _active_recycle_state(conn: sqlite3.Connection, candidate_key: str) -> dict[str, Any]:
    row = conn.execute(
        """SELECT * FROM m230_threshold_promotion_recycle_state
           WHERE candidate_key=?""",
        (candidate_key,),
    ).fetchone()
    if not row:
        return {}
    payload = {key: row[key] for key in row.keys()}
    payload["state_payload"] = _json_loads(payload.get("state_payload_json"), {})
    return payload


def _update_recycle_state(
    conn: sqlite3.Connection,
    *,
    review_stub: dict[str, Any],
    item: dict[str, Any],
    checks: dict[str, Any],
    stale_policy: dict[str, Any],
    option_root_cause_policy: dict[str, Any],
    config: ThresholdCandidateEvaluatorConfig,
    reviewed_at: str,
) -> dict[str, Any]:
    candidate_key = str(review_stub.get("candidate_key") or "")
    operational = str(stale_policy.get("operational_gate_status") or review_stub.get("gate_status") or "")
    sample_count = int(item.get("sample_count") or 0)
    option_details = ((checks.get("option_structure_conflict") or {}).get("details") or {})
    diagnostics = option_details.get("option_confidence_diagnostics") or {}
    medium_high = int(diagnostics.get("medium_high_confidence_count") or 0)
    previous = _active_recycle_state(conn, candidate_key)
    if operational == "promotion_recycle_candidate":
        active_previous = previous if str(previous.get("status") or "") == "active" else {}
        started_at = str(active_previous.get("recycle_started_at") or reviewed_at)
        started_sample_count = int(active_previous.get("recycle_started_sample_count") or sample_count)
        review_count = int(active_previous.get("recycle_review_count") or 0) + 1
        consecutive = int(active_previous.get("consecutive_option_confidence_ticks") or 0)
        consecutive = consecutive + 1 if medium_high > 0 else 0
        new_sample_count = max(0, sample_count - started_sample_count)
        quarantine_remaining = max(0, int(config.promotion_gate_recycle_quarantine_ticks) - review_count)
        status = "active"
    else:
        started_at = str(previous.get("recycle_started_at") or reviewed_at)
        started_sample_count = int(previous.get("recycle_started_sample_count") or sample_count)
        review_count = int(previous.get("recycle_review_count") or 0)
        consecutive = int(previous.get("consecutive_option_confidence_ticks") or 0)
        new_sample_count = max(0, sample_count - started_sample_count)
        quarantine_remaining = 0
        status = "cleared" if previous else "inactive"
    state_payload = {
        "state_version": "m230-recycle-state-v1",
        "candidate_key": candidate_key,
        "semantic_family_key": review_stub.get("semantic_family_key"),
        "outcome_direction_key": review_stub.get("outcome_direction_key"),
        "operational_gate_status": operational,
        "detail_reasons": review_stub.get("detail_reasons") or {},
        "option_confidence_root_causes": diagnostics.get("root_cause_tags") or [],
        "option_root_cause_policy": option_root_cause_policy,
        "targets": {
            "new_sample_count": int(config.promotion_gate_recycle_min_new_samples),
            "consecutive_option_confidence_ticks": int(config.promotion_gate_recycle_return_confidence_ticks),
            "quarantine_ticks": int(config.promotion_gate_recycle_quarantine_ticks),
        },
    }
    conn.execute(
        """INSERT INTO m230_threshold_promotion_recycle_state(
            candidate_key,recycle_started_at,recycle_started_sample_count,current_sample_count,
            new_sample_count,recycle_review_count,consecutive_option_confidence_ticks,
            last_option_medium_high_count,quarantine_remaining_ticks,status,updated_at,state_payload_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(candidate_key) DO UPDATE SET
            recycle_started_at=excluded.recycle_started_at,
            recycle_started_sample_count=excluded.recycle_started_sample_count,
            current_sample_count=excluded.current_sample_count,
            new_sample_count=excluded.new_sample_count,
            recycle_review_count=excluded.recycle_review_count,
            consecutive_option_confidence_ticks=excluded.consecutive_option_confidence_ticks,
            last_option_medium_high_count=excluded.last_option_medium_high_count,
            quarantine_remaining_ticks=excluded.quarantine_remaining_ticks,
            status=excluded.status,
            updated_at=excluded.updated_at,
            state_payload_json=excluded.state_payload_json""",
        (
            candidate_key,
            started_at,
            started_sample_count,
            sample_count,
            new_sample_count,
            review_count,
            consecutive,
            medium_high,
            quarantine_remaining,
            status,
            reviewed_at,
            json.dumps(state_payload, ensure_ascii=False, sort_keys=True),
        ),
    )
    return {
        **state_payload,
        "recycle_started_at": started_at,
        "recycle_started_sample_count": started_sample_count,
        "current_sample_count": sample_count,
        "new_sample_count": new_sample_count,
        "recycle_review_count": review_count,
        "consecutive_option_confidence_ticks": consecutive,
        "last_option_medium_high_count": medium_high,
        "quarantine_remaining_ticks": quarantine_remaining,
        "status": status,
        "updated_at": reviewed_at,
    }


def _promotion_gate_action_requests(
    review: dict[str, Any],
    stale_policy: dict[str, Any],
    option_root_cause_policy: dict[str, Any] | None = None,
    recycle_return_criteria: dict[str, Any] | None = None,
    lifecycle_policy: dict[str, Any] | None = None,
    config: ThresholdCandidateEvaluatorConfig | None = None,
) -> list[dict[str, Any]]:
    requests: list[dict[str, Any]] = []
    detail_reasons = review.get("detail_reasons") or {}
    detail_values = {str(value) for value in detail_reasons.values()}
    option_root_cause_policy = option_root_cause_policy or {}
    recycle_return_criteria = recycle_return_criteria or {}
    lifecycle_policy = lifecycle_policy or {}
    if stale_policy.get("option_refresh_required") or detail_values & {
        "option_structure_low_confidence",
        "option_structure_stale",
        "option_structure_expected_deferred_stale",
    }:
        requests.append(
            {
                "action_id": "promotion_gate_refresh_option_market_structure",
                "domain": "threshold_promotion_gate",
                "target": "options_market_structure_supervisor",
                "severity": stale_policy.get("severity") if stale_policy.get("watchlist_stale") else "watch",
                "title": "Refresh option structure for stale Promotion Gate watchlist",
                "recommendation": (
                    "Run the Options Market Structure Supervisor/M218-M219 refresh path and recheck option confidence before promotion."
                ),
                "command_hint": "options-market-structure-supervisor --execute-actions",
                "candidate_key": review.get("candidate_key"),
                "semantic_family_key": review.get("semantic_family_key"),
                "detail_reasons": detail_reasons,
                "can_auto_execute": False,
                "read_only": True,
            }
        )
    if stale_policy.get("recycle_recommended"):
        requests.append(
            {
                "action_id": "promotion_gate_recycle_stale_candidate",
                "domain": "threshold_promotion_gate",
                "target": "live_learning_llm_notes",
                "severity": "red",
                "title": "Recycle stale Promotion Gate candidate",
                "recommendation": "Keep the candidate out of promotion waitlist and prioritize fresh M225/M226 samples for the same family.",
                "command_hint": "live-learning-llm-notes and threshold-candidate-evaluator normal cycle",
                "candidate_key": review.get("candidate_key"),
                "semantic_family_key": review.get("semantic_family_key"),
                "detail_reasons": detail_reasons,
                "can_auto_execute": False,
                "read_only": True,
            }
        )
    for root in option_root_cause_policy.get("root_causes") or []:
        root_text = str(root)
        repair_target = {
            "index_or_family_coverage_gap": "options_market_structure_supervisor",
            "open_interest_missing_or_volume_proxy": "options_market_structure_desk",
            "wide_spread_or_liquidity_thin": "options_market_structure_desk",
            "bid_ask_missing_or_unusable": "options_market_structure_desk",
            "regular_session_dependency": "options_market_structure_scheduler",
            "supervisor_failure_reason_present": "options_market_structure_supervisor",
            "supervisor_status_not_green": "options_market_structure_supervisor",
            "thin_option_quote_sample": "options_market_structure_desk",
        }.get(root_text, "options_market_structure_desk")
        requests.append(
            {
                "action_id": f"promotion_gate_repair_option_root:{root_text}",
                "domain": "threshold_promotion_gate",
                "target": repair_target,
                "severity": "yellow" if not option_root_cause_policy.get("blocks_return") else "red",
                "title": f"Repair option evidence root cause: {root_text}",
                "recommendation": {
                    "index_or_family_coverage_gap": "Refresh SPY/QQQ/IWM/DIA first, then direct index families where permission allows.",
                    "open_interest_missing_or_volume_proxy": "Keep OI-dependent evidence capped and collect volume-proxy samples until OI is available.",
                    "wide_spread_or_liquidity_thin": "Downweight option structure while spreads are wide; retry during liquid regular-session windows.",
                    "bid_ask_missing_or_unusable": "Block option-structure-dependent promotion until bid/ask quotes are available.",
                    "regular_session_dependency": "Wait for a regular-session option snapshot before promotion return.",
                    "supervisor_failure_reason_present": "Resolve supervisor failure reasons before return.",
                    "supervisor_status_not_green": "Require supervisor green/yellow and zero failed actions.",
                    "thin_option_quote_sample": "Collect additional quote samples before trusting this option family.",
                }.get(root_text, "Review option market-structure evidence and refresh affected family."),
                "candidate_key": review.get("candidate_key"),
                "semantic_family_key": review.get("semantic_family_key"),
                "root_cause": root_text,
                "can_auto_execute": False,
                "read_only": True,
            }
        )
    remaining = int(recycle_return_criteria.get("remaining_blocker_count") or len(recycle_return_criteria.get("blocked_by") or []))
    near_limit = int(config.promotion_gate_recycle_near_return_remaining_checks) if config else 1
    if recycle_return_criteria.get("eligible_to_return_now") or (
        recycle_return_criteria and remaining <= near_limit and lifecycle_policy.get("lifecycle_status") == "active"
    ):
        requests.append(
            {
                "action_id": "promotion_gate_recycle_return_review",
                "domain": "threshold_promotion_gate",
                "target": "threshold_candidate_evaluator",
                "severity": "yellow" if not recycle_return_criteria.get("eligible_to_return_now") else "green",
                "title": "Recycle candidate is near return criteria",
                "recommendation": "Review remaining blockers; if next evaluator ticks confirm, candidate can return to watchlist.",
                "candidate_key": review.get("candidate_key"),
                "semantic_family_key": review.get("semantic_family_key"),
                "remaining_blockers": recycle_return_criteria.get("blocked_by") or [],
                "can_auto_execute": False,
                "read_only": True,
            }
        )
    return requests


def _scorer_evidence_packet_hook(
    review: dict[str, Any],
    conditional_promotion: dict[str, Any],
    stale_policy: dict[str, Any],
) -> dict[str, Any]:
    return {
        "hook_version": "m229-scorer-evidence-packet-hook-v1",
        "candidate_key": review.get("candidate_key"),
        "semantic_family_key": review.get("semantic_family_key"),
        "outcome_direction_key": review.get("outcome_direction_key"),
        "gate_status": review.get("gate_status"),
        "operational_gate_status": stale_policy.get("operational_gate_status"),
        "application_policy": (
            "exclude_from_promotion"
            if stale_policy.get("temporary_promotion_exclusion")
            else "conditional_only"
            if conditional_promotion.get("eligible_for_conditional_promotion_queue")
            else "observe_only"
        ),
        "conditions": conditional_promotion.get("conditions") or [],
        "withhold_when": [
            "option_structure_low_confidence",
            "option_structure_stale",
            "option_structure_expected_deferred_stale",
            "evidence_required_source_not_fresh",
            "portfolio_risk_blocks_new_entries",
        ],
        "detail_reasons": review.get("detail_reasons") or {},
        "safety_note": "hook is advisory evidence only; scorer thresholds are not mutated here",
    }


def _promotion_gate_alert_signal(
    review: dict[str, Any],
    stale_policy: dict[str, Any],
    recycle_return_criteria: dict[str, Any] | None = None,
    lifecycle_policy: dict[str, Any] | None = None,
    config: ThresholdCandidateEvaluatorConfig | None = None,
) -> dict[str, Any]:
    recycle_return_criteria = recycle_return_criteria or {}
    lifecycle_policy = lifecycle_policy or {}
    remaining = int(recycle_return_criteria.get("remaining_blocker_count") or len(recycle_return_criteria.get("blocked_by") or []))
    near_limit = int(config.promotion_gate_recycle_near_return_remaining_checks) if config else 1
    near_return = bool(recycle_return_criteria.get("eligible_to_return_now") or (recycle_return_criteria and remaining <= near_limit))
    lifecycle_status = str(lifecycle_policy.get("lifecycle_status") or "")
    should_alert = stale_policy.get("watchlist_stale") or stale_policy.get("recycle_recommended")
    should_alert = bool(should_alert or near_return or lifecycle_status in {"dormant", "retired"})
    title = "Promotion Gate stale watchlist requires action"
    message = stale_policy.get("recommendation")
    severity = stale_policy.get("severity") or "green"
    if near_return:
        title = "Promotion recycle candidate is near return"
        severity = "yellow" if not recycle_return_criteria.get("eligible_to_return_now") else "green"
        message = f"Candidate has {remaining} remaining return blocker(s): {recycle_return_criteria.get('blocked_by') or []}"
    if lifecycle_status in {"dormant", "retired"}:
        title = f"Promotion recycle candidate moved to {lifecycle_status}"
        severity = "yellow" if lifecycle_status == "dormant" else "red"
        message = lifecycle_policy.get("recommendation") or message
    return {
        "alert_version": "m231-promotion-gate-alert-signal-v2",
        "enabled": should_alert,
        "severity": severity,
        "dedupe_key": f"promotion_gate:{review.get('candidate_key')}:{stale_policy.get('operational_gate_status')}:{lifecycle_status or 'normal'}:{'near_return' if near_return else 'watch'}",
        "alert_once_per_operational_status": True,
        "realert_on_worsening": True,
        "title": title,
        "message": message,
        "candidate_key": review.get("candidate_key"),
        "operational_gate_status": stale_policy.get("operational_gate_status"),
        "near_return": near_return,
        "remaining_blocker_count": remaining if recycle_return_criteria else None,
        "lifecycle_status": lifecycle_status or None,
    }


def _recycle_learning_plan(
    review: dict[str, Any],
    checks: dict[str, Any],
    stale_policy: dict[str, Any],
    config: ThresholdCandidateEvaluatorConfig,
) -> dict[str, Any]:
    option_details = (checks.get("option_structure_conflict") or {}).get("details") or {}
    diagnostics = option_details.get("option_confidence_diagnostics") or {}
    is_recycle = stale_policy.get("operational_gate_status") == "promotion_recycle_candidate"
    root_causes = diagnostics.get("root_cause_tags") or []
    priority = "normal"
    if is_recycle:
        priority = "high" if "bid_ask_missing_or_unusable" in root_causes or "index_or_family_coverage_gap" in root_causes else "medium"
    return {
        "plan_version": "m229-recycle-learning-plan-v1",
        "enabled": bool(is_recycle),
        "priority": priority,
        "candidate_key": review.get("candidate_key"),
        "semantic_family_key": review.get("semantic_family_key"),
        "outcome_direction_key": review.get("outcome_direction_key"),
        "target_new_sample_count": int(config.promotion_gate_recycle_min_new_samples),
        "focus_filters": {
            "semantic_family_key": review.get("semantic_family_key"),
            "outcome_direction_key": review.get("outcome_direction_key"),
            "detail_reasons": review.get("detail_reasons") or {},
            "option_confidence_root_causes": root_causes,
        },
        "collection_priorities": [
            "same semantic family news/outcomes",
            "samples after option confidence returns to medium/high",
            "fresh evidence packet with current market/option context",
            "mismatch notes explaining whether stale blocker persisted or cleared",
        ],
        "m225_feedback_hint": (
            "Prioritize new adjustment notes for this recycled family, especially after M218/M219 option confidence improves."
        ),
        "recycle_reason": stale_policy.get("recommendation"),
        "safety_note": "learning plan is advisory; no threshold mutation or broker action",
    }


def _recycle_return_criteria(
    review: dict[str, Any],
    checks: dict[str, Any],
    stale_policy: dict[str, Any],
    recycle_state: dict[str, Any],
    option_root_cause_policy: dict[str, Any],
    config: ThresholdCandidateEvaluatorConfig,
) -> dict[str, Any]:
    option_check = checks.get("option_structure_conflict") or {}
    option_details = option_check.get("details") or {}
    diagnostics = option_details.get("option_confidence_diagnostics") or {}
    medium_high = int(diagnostics.get("medium_high_confidence_count") or 0)
    new_sample_count = int(recycle_state.get("new_sample_count") or 0)
    consecutive_confidence = int(recycle_state.get("consecutive_option_confidence_ticks") or 0)
    quarantine_remaining = int(recycle_state.get("quarantine_remaining_ticks") or 0)
    evidence_check = checks.get("evidence_freshness") or {}
    portfolio_check = checks.get("portfolio_drawdown_risk") or {}
    shadow_check = checks.get("recent_shadow_performance") or {}
    drawdown_check = checks.get("candidate_drawdown_worsening") or {}
    criteria = [
        {
            "key": "option_confidence_medium_high",
            "passed": medium_high > 0,
            "current": medium_high,
            "target": ">=1 medium/high option confidence family",
        },
        {
            "key": "option_confidence_consecutive_ticks",
            "passed": consecutive_confidence >= int(config.promotion_gate_recycle_return_confidence_ticks),
            "current": consecutive_confidence,
            "target": int(config.promotion_gate_recycle_return_confidence_ticks),
            "note": "tracked in m230_threshold_promotion_recycle_state",
        },
        {
            "key": "option_root_cause_policy_clear",
            "passed": not bool(option_root_cause_policy.get("blocks_return")),
            "current": option_root_cause_policy.get("block_reasons") or [],
            "target": "no root-cause policy block",
            "haircut": {
                "confidence_multiplier": option_root_cause_policy.get("confidence_multiplier"),
                "confidence_cap": option_root_cause_policy.get("confidence_cap"),
            },
        },
        {
            "key": "recycle_quarantine_complete",
            "passed": quarantine_remaining <= 0,
            "current": quarantine_remaining,
            "target": 0,
            "note": "prevents immediate re-promotion after recycle",
        },
        {
            "key": "fresh_evidence",
            "passed": bool(evidence_check.get("passed")),
            "current": evidence_check.get("reason"),
            "target": "evidence_freshness ok",
        },
        {
            "key": "shadow_acceptance_maintained",
            "passed": bool(shadow_check.get("passed")),
            "current": (shadow_check.get("details") or {}).get("avg_recent_acceptance_score"),
            "target": (shadow_check.get("details") or {}).get("required_acceptance_score"),
        },
        {
            "key": "drawdown_not_worsened",
            "passed": bool(drawdown_check.get("passed")),
            "current": (drawdown_check.get("details") or {}).get("latest_avg_absolute_error"),
            "target": "not worse than allowed threshold",
        },
        {
            "key": "portfolio_risk_allows_entries",
            "passed": bool(portfolio_check.get("passed")),
            "current": (portfolio_check.get("details") or {}).get("risk_state"),
            "target": "within_limits",
        },
        {
            "key": "new_samples_after_recycle",
            "passed": new_sample_count >= int(config.promotion_gate_recycle_min_new_samples),
            "current": new_sample_count,
            "target": int(config.promotion_gate_recycle_min_new_samples),
            "note": "tracked in m230_threshold_promotion_recycle_state",
        },
    ]
    passed_count = sum(1 for item in criteria if item.get("passed"))
    eligible = bool(
        stale_policy.get("operational_gate_status") == "promotion_recycle_candidate"
        and passed_count == len(criteria)
    )
    blocked_by = [item["key"] for item in criteria if not item.get("passed")]
    return {
        "criteria_version": "m230-recycle-return-criteria-v2",
        "candidate_key": review.get("candidate_key"),
        "eligible_to_return_now": eligible,
        "next_status_if_all_pass": "promotion_watchlist",
        "eventual_path": ["promotion_recycle_candidate", "promotion_watchlist", "promotion_waitlist"],
        "passed_count": passed_count,
        "total_count": len(criteria),
        "criteria": criteria,
        "blocked_by": blocked_by,
        "remaining_blocker_count": len(blocked_by),
        "recycle_state": recycle_state,
        "option_root_cause_policy": option_root_cause_policy,
        "safety_note": "return criteria are state-tracked but still advisory; no live threshold mutation is performed",
    }


def _recycle_lifecycle_policy(
    recycle_state: dict[str, Any],
    return_criteria: dict[str, Any],
    stale_policy: dict[str, Any],
    config: ThresholdCandidateEvaluatorConfig,
) -> dict[str, Any]:
    operational = str(stale_policy.get("operational_gate_status") or "")
    review_count = int(recycle_state.get("recycle_review_count") or 0)
    new_samples = int(recycle_state.get("new_sample_count") or 0)
    confidence_ticks = int(recycle_state.get("consecutive_option_confidence_ticks") or 0)
    eligible = bool(return_criteria.get("eligible_to_return_now"))
    lifecycle = "inactive"
    recommendation = "No recycle lifecycle action required."
    operational_override = operational
    if operational == "promotion_recycle_candidate":
        lifecycle = "ready_to_return" if eligible else "active"
        recommendation = "Continue recycle learning until return criteria pass."
        no_progress = new_samples <= 0 and confidence_ticks <= 0
        if not eligible and review_count >= int(config.promotion_gate_recycle_retire_ticks) and no_progress:
            lifecycle = "retired"
            operational_override = "promotion_retired_candidate"
            recommendation = "Retire candidate from active recycle queue; it has not recovered after the retire threshold."
        elif not eligible and review_count >= int(config.promotion_gate_recycle_dormant_ticks) and no_progress:
            lifecycle = "dormant"
            operational_override = "promotion_dormant_candidate"
            recommendation = "Move candidate to dormant recycle state until fresh samples or option confidence return."
    return {
        "policy_version": "m231-recycle-lifecycle-policy-v1",
        "lifecycle_status": lifecycle,
        "operational_gate_status": operational_override,
        "review_count": review_count,
        "new_sample_count": new_samples,
        "consecutive_option_confidence_ticks": confidence_ticks,
        "eligible_to_return_now": eligible,
        "dormant_threshold_ticks": int(config.promotion_gate_recycle_dormant_ticks),
        "retire_threshold_ticks": int(config.promotion_gate_recycle_retire_ticks),
        "recommendation": recommendation,
    }


def _store_recycle_history(
    conn: sqlite3.Connection,
    *,
    review: dict[str, Any],
    return_criteria: dict[str, Any],
    lifecycle_policy: dict[str, Any],
    scorer_threshold_policy: dict[str, Any],
    reviewed_at: str,
) -> None:
    recycle_state = return_criteria.get("recycle_state") or {}
    if not recycle_state or recycle_state.get("status") == "inactive":
        return
    conn.execute(
        """INSERT INTO m231_threshold_promotion_recycle_history(
            candidate_key,recorded_at,evaluator_version,operational_gate_status,lifecycle_status,
            new_sample_count,consecutive_option_confidence_ticks,quarantine_remaining_ticks,
            blocked_by_json,root_causes_json,scorer_action,history_payload_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            str(review.get("candidate_key") or ""),
            reviewed_at,
            M226_THRESHOLD_CANDIDATE_EVALUATOR_VERSION,
            str(lifecycle_policy.get("operational_gate_status") or review.get("operational_gate_status") or ""),
            str(lifecycle_policy.get("lifecycle_status") or ""),
            int(recycle_state.get("new_sample_count") or 0),
            int(recycle_state.get("consecutive_option_confidence_ticks") or 0),
            int(recycle_state.get("quarantine_remaining_ticks") or 0),
            json.dumps(return_criteria.get("blocked_by") or [], ensure_ascii=False, sort_keys=True),
            json.dumps(recycle_state.get("option_confidence_root_causes") or [], ensure_ascii=False, sort_keys=True),
            str(scorer_threshold_policy.get("scorer_action") or ""),
            json.dumps(
                {
                    "return_criteria": return_criteria,
                    "lifecycle_policy": lifecycle_policy,
                    "scorer_threshold_policy": scorer_threshold_policy,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        ),
    )


def _scorer_threshold_application_policy(
    review: dict[str, Any],
    stale_policy: dict[str, Any],
    return_criteria: dict[str, Any],
    conditional_promotion: dict[str, Any],
    option_root_cause_policy: dict[str, Any],
) -> dict[str, Any]:
    operational = str(stale_policy.get("operational_gate_status") or review.get("gate_status") or "")
    if operational == "promotion_retired_candidate":
        action = "retire_threshold_candidate"
        confidence_multiplier = 0.0
    elif operational == "promotion_dormant_candidate":
        action = "hold_dormant_threshold_candidate"
        confidence_multiplier = 0.0
    elif operational == "promotion_recycle_candidate":
        action = "exclude_threshold_candidate"
        confidence_multiplier = 0.0
    elif operational == "promotion_stale_watchlist":
        action = "withhold_or_apply_strong_haircut"
        confidence_multiplier = 0.25
    elif conditional_promotion.get("eligible_for_conditional_promotion_queue"):
        action = "conditional_threshold_only"
        confidence_multiplier = 0.60
    elif review.get("gate_status") == "promotion_waitlist":
        action = "eligible_shadow_waitlist"
        confidence_multiplier = 1.0
    else:
        action = "observe_only"
        confidence_multiplier = 0.0
    root_multiplier = float(option_root_cause_policy.get("confidence_multiplier") if option_root_cause_policy.get("confidence_multiplier") is not None else 1.0)
    root_cap = float(option_root_cause_policy.get("confidence_cap") if option_root_cause_policy.get("confidence_cap") is not None else 1.0)
    confidence_multiplier = min(confidence_multiplier, root_multiplier, root_cap)
    if option_root_cause_policy.get("blocks_return") and action not in {"exclude_threshold_candidate", "observe_only"}:
        action = "withhold_for_option_root_cause_block"
    return {
        "policy_version": "m230-scorer-threshold-application-policy-v2",
        "candidate_key": review.get("candidate_key"),
        "operational_gate_status": operational,
        "scorer_action": action,
        "confidence_multiplier": confidence_multiplier,
        "directional_score_multiplier": confidence_multiplier,
        "option_root_cause_policy": option_root_cause_policy,
        "do_not_mutate_threshold": True,
        "return_criteria_passed_count": return_criteria.get("passed_count"),
        "return_criteria_total_count": return_criteria.get("total_count"),
        "conditions": conditional_promotion.get("conditions") or [],
        "withhold_when": [
            "promotion_recycle_candidate",
            "promotion_dormant_candidate",
            "promotion_retired_candidate",
            "option_structure_low_confidence",
            "option_structure_stale",
            "option_structure_expected_deferred_stale",
            "required_evidence_sources_not_fresh",
        ],
    }


def _promotion_gate_review_for_item(
    conn: sqlite3.Connection,
    item: dict[str, Any],
    trial: dict[str, Any],
    config: ThresholdCandidateEvaluatorConfig,
    external_context: dict[str, Any],
) -> dict[str, Any]:
    candidate_key = str(item.get("candidate_key") or "")
    reviewed_at = _iso_now()
    trial_history = _latest_trial_history(conn, candidate_key, int(config.promotion_gate_recent_trial_count))
    recent_scores = [float(row.get("acceptance_score") or 0.0) for row in trial_history]
    avg_recent_score = sum(recent_scores) / len(recent_scores) if recent_scores else 0.0
    recent_errors = [
        float(row.get("avg_absolute_error"))
        for row in trial_history
        if row.get("avg_absolute_error") is not None
    ]
    latest_error = recent_errors[0] if recent_errors else None
    prior_errors = recent_errors[1:]
    prior_error_avg = sum(prior_errors) / len(prior_errors) if prior_errors else None
    drawdown_worsened = False
    if latest_error is not None and prior_error_avg is not None and prior_error_avg > 0:
        drawdown_worsened = latest_error > prior_error_avg * (1.0 + float(config.promotion_gate_max_abs_error_worsening_pct))

    metrics = item.get("supporting_metrics") or {}
    event_family_counts = metrics.get("event_family_counts") or {}
    sample_count = max(1, int(item.get("sample_count") or 0))
    dominant_family = None
    dominant_share = 0.0
    if event_family_counts:
        dominant_family, dominant_count = max(event_family_counts.items(), key=lambda pair: int(pair[1] or 0))
        dominant_share = int(dominant_count or 0) / sample_count
    regime_bias_passed = (
        dominant_share <= float(config.promotion_gate_max_regime_family_share)
        or sample_count >= int(config.promotion_gate_min_sample_count) * 2
    )
    dependencies = _candidate_gate_dependencies(item)

    checks = {
        "m226_candidate_passed": _gate_check(
            item.get("status") == "promotion_ready",
            "candidate_not_promotion_ready",
            {"status": item.get("status")},
            severity="hard",
        ),
        "minimum_sample_count": _gate_check(
            sample_count >= int(config.promotion_gate_min_sample_count),
            "minimum_sample_count_not_met",
            {"sample_count": sample_count, "minimum": int(config.promotion_gate_min_sample_count)},
            severity="hard",
        ),
        "recent_shadow_performance": _gate_check(
            (
                int(trial.get("cycles_seen") or 0) >= int(config.min_shadow_trial_cycles)
                and avg_recent_score >= float(config.promotion_gate_min_recent_acceptance_score)
            ),
            "recent_shadow_performance_not_sufficient",
            {
                "cycles_seen": int(trial.get("cycles_seen") or 0),
                "required_cycles": int(config.min_shadow_trial_cycles),
                "avg_recent_acceptance_score": round(avg_recent_score, 4),
                "required_acceptance_score": float(config.promotion_gate_min_recent_acceptance_score),
                "recent_score_count": len(recent_scores),
            },
            severity="hard",
        ),
        "candidate_drawdown_worsening": _gate_check(
            not drawdown_worsened and len(recent_errors) >= min(2, int(config.promotion_gate_recent_trial_count)),
            "candidate_drawdown_history_insufficient_or_worsened",
            {
                "latest_avg_absolute_error": latest_error,
                "prior_avg_absolute_error": prior_error_avg,
                "max_worsening_pct": float(config.promotion_gate_max_abs_error_worsening_pct),
                "recent_error_count": len(recent_errors),
            },
            severity="hard",
        ),
        "regime_bias": _gate_check(
            regime_bias_passed,
            "regime_family_bias_too_concentrated",
            {
                "dominant_event_family": dominant_family,
                "dominant_event_family_share": round(dominant_share, 4),
                "max_allowed_share": float(config.promotion_gate_max_regime_family_share),
                "event_family_counts": event_family_counts,
            },
            severity="hard",
        ),
        "option_structure_conflict": _check_option_structure(
            item,
            external_context.get("options_market_structure") or {},
            external_context.get("options_market_structure_supervisor") or {},
            dependencies,
        ),
        "evidence_freshness": _check_evidence_freshness(external_context.get("evidence_freshness") or {}, dependencies),
        "portfolio_drawdown_risk": _check_portfolio_drawdown(external_context.get("portfolio_risk_state") or {}),
    }
    failed = [name for name, check in checks.items() if not check.get("passed")]
    hard_failed = [
        name
        for name, check in checks.items()
        if not check.get("passed") and str(check.get("severity") or "hard") == "hard"
    ]
    soft_failed = [
        name
        for name, check in checks.items()
        if not check.get("passed") and str(check.get("severity") or "hard") == "soft"
    ]
    watchlist_ready = bool(soft_failed and not hard_failed and config.promotion_gate_watchlist_enabled)
    if hard_failed or (soft_failed and not config.promotion_gate_watchlist_enabled):
        gate_status = "promotion_gate_blocked"
    elif soft_failed:
        gate_status = "promotion_watchlist"
    else:
        gate_status = "promotion_waitlist"
    promotion_requirements = _promotion_gate_requirements(checks, failed)
    history_state = _promotion_gate_history_state(conn, candidate_key, gate_status, reviewed_at, config)
    detail_reasons = {
        str(name): str((checks.get(name) or {}).get("reason") or "")
        for name in failed
    }
    stale_policy = _promotion_gate_stale_policy(gate_status, history_state, detail_reasons, config)
    auto_recheck_policy = {
        "enabled": True,
        "current_status": gate_status,
        "operational_gate_status": stale_policy.get("operational_gate_status"),
        "if_all_requirements_pass": "promotion_waitlist",
        "if_any_hard_requirement_fails": "promotion_gate_blocked",
        "if_only_soft_requirements_fail": "promotion_watchlist",
        "recheck_trigger": "each_threshold_candidate_evaluator_tick",
        "watchlist_stale": bool(history_state.get("watchlist_stale")),
    }
    conditional_promotion = _conditional_promotion_plan(gate_status, dependencies, checks, promotion_requirements)
    review_stub = {
        "candidate_key": candidate_key,
        "semantic_family_key": item.get("semantic_family_key"),
        "outcome_direction_key": item.get("outcome_direction_key"),
        "gate_status": gate_status,
        "detail_reasons": detail_reasons,
    }
    recycle_learning_plan = _recycle_learning_plan(review_stub, checks, stale_policy, config)
    option_details = ((checks.get("option_structure_conflict") or {}).get("details") or {})
    option_diagnostics = option_details.get("option_confidence_diagnostics") or {}
    option_root_cause_policy = _option_root_cause_response_policy(option_diagnostics, config)
    recycle_state = _update_recycle_state(
        conn,
        review_stub=review_stub,
        item=item,
        checks=checks,
        stale_policy=stale_policy,
        option_root_cause_policy=option_root_cause_policy,
        config=config,
        reviewed_at=reviewed_at,
    )
    recycle_return_criteria = _recycle_return_criteria(
        review_stub,
        checks,
        stale_policy,
        recycle_state,
        option_root_cause_policy,
        config,
    )
    lifecycle_policy = _recycle_lifecycle_policy(recycle_state, recycle_return_criteria, stale_policy, config)
    if lifecycle_policy.get("operational_gate_status") and lifecycle_policy.get("operational_gate_status") != stale_policy.get("operational_gate_status"):
        stale_policy = {
            **stale_policy,
            "operational_gate_status": lifecycle_policy.get("operational_gate_status"),
            "lifecycle_status": lifecycle_policy.get("lifecycle_status"),
            "recommendation": lifecycle_policy.get("recommendation") or stale_policy.get("recommendation"),
            "temporary_promotion_exclusion": True,
        }
    auto_recheck_policy["operational_gate_status"] = stale_policy.get("operational_gate_status")
    scorer_threshold_policy = _scorer_threshold_application_policy(
        review_stub,
        stale_policy,
        recycle_return_criteria,
        conditional_promotion,
        option_root_cause_policy,
    )
    action_requests = _promotion_gate_action_requests(
        review_stub,
        stale_policy,
        option_root_cause_policy=option_root_cause_policy,
        recycle_return_criteria=recycle_return_criteria,
        lifecycle_policy=lifecycle_policy,
        config=config,
    )
    scorer_hook = _scorer_evidence_packet_hook(review_stub, conditional_promotion, stale_policy)
    alert_signal = _promotion_gate_alert_signal(
        review_stub,
        stale_policy,
        recycle_return_criteria=recycle_return_criteria,
        lifecycle_policy=lifecycle_policy,
        config=config,
    )
    _store_recycle_history(
        conn,
        review={
            **review_stub,
            "operational_gate_status": stale_policy.get("operational_gate_status"),
        },
        return_criteria=recycle_return_criteria,
        lifecycle_policy=lifecycle_policy,
        scorer_threshold_policy=scorer_threshold_policy,
        reviewed_at=reviewed_at,
    )
    return {
        "candidate_key": candidate_key,
        "reviewed_at": reviewed_at,
        "semantic_family_key": item.get("semantic_family_key"),
        "outcome_direction_key": item.get("outcome_direction_key"),
        "gate_status": gate_status,
        "operational_gate_status": stale_policy.get("operational_gate_status"),
        "ready_for_promotion_waitlist": gate_status == "promotion_waitlist",
        "ready_for_promotion_watchlist": watchlist_ready,
        "failed_check_count": len(failed),
        "blocked_reasons": hard_failed,
        "watchlist_reasons": soft_failed,
        "issue_reasons": failed,
        "detail_reasons": detail_reasons,
        "promotion_requirements": promotion_requirements,
        "auto_recheck_policy": auto_recheck_policy,
        "history_state": history_state,
        "conditional_promotion": conditional_promotion,
        "stale_watchlist_policy": stale_policy,
        "action_requests": action_requests,
        "scorer_evidence_packet_hook": scorer_hook,
        "scorer_threshold_application_policy": scorer_threshold_policy,
        "alert_signal": alert_signal,
        "recycle_learning_plan": recycle_learning_plan,
        "recycle_return_criteria": recycle_return_criteria,
        "recycle_state": recycle_state,
        "recycle_lifecycle_policy": lifecycle_policy,
        "option_root_cause_policy": option_root_cause_policy,
        "dependency_profile": dependencies,
        "checks": checks,
        "trial": trial,
        "safety_note": "promotion gate is read-only; real scorer threshold promotion remains disabled",
    }


def _store_promotion_gate_review(conn: sqlite3.Connection, review: dict[str, Any]) -> None:
    now = str(review.get("reviewed_at") or _iso_now())
    conn.execute(
        """INSERT INTO m228_threshold_promotion_gate_reviews(
            candidate_key,reviewed_at,evaluator_version,gate_status,
            ready_for_promotion_waitlist,failed_check_count,blocked_reasons_json,
            check_results_json,review_payload_json
        ) VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            str(review.get("candidate_key") or ""),
            now,
            M226_THRESHOLD_CANDIDATE_EVALUATOR_VERSION,
            str(review.get("gate_status") or ""),
            1 if review.get("ready_for_promotion_waitlist") else 0,
            int(review.get("failed_check_count") or 0),
            json.dumps(review.get("blocked_reasons") or [], ensure_ascii=False, sort_keys=True),
            json.dumps(review.get("checks") or {}, ensure_ascii=False, sort_keys=True),
            json.dumps(review, ensure_ascii=False, sort_keys=True),
        ),
    )
    history = review.get("history_state") or {}
    transition_type = str(history.get("transition_type") or "unknown")
    if transition_type != "unchanged":
        conn.execute(
            """INSERT INTO m229_threshold_promotion_gate_transitions(
                candidate_key,transitioned_at,evaluator_version,previous_gate_status,new_gate_status,
                transition_type,semantic_family_key,outcome_direction_key,transition_payload_json
            ) VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                str(review.get("candidate_key") or ""),
                now,
                M226_THRESHOLD_CANDIDATE_EVALUATOR_VERSION,
                history.get("previous_gate_status"),
                str(review.get("gate_status") or ""),
                transition_type,
                review.get("semantic_family_key"),
                review.get("outcome_direction_key"),
                json.dumps(
                    {
                        "history_state": history,
                        "detail_reasons": review.get("detail_reasons") or {},
                        "promotion_requirements": review.get("promotion_requirements") or [],
                        "conditional_promotion": review.get("conditional_promotion") or {},
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            ),
        )


def _promotion_gate_transition_summary(conn: sqlite3.Connection | None) -> dict[str, Any]:
    if conn is None:
        return {"transition_count": 0, "transition_type_counts": {}, "recent_transitions": []}
    try:
        rows = conn.execute(
            """SELECT candidate_key,transitioned_at,previous_gate_status,new_gate_status,transition_type,
                      semantic_family_key,outcome_direction_key
               FROM m229_threshold_promotion_gate_transitions
               ORDER BY id DESC
               LIMIT 20"""
        ).fetchall()
    except Exception:
        return {"transition_count": 0, "transition_type_counts": {}, "recent_transitions": []}
    counts = Counter(str(row["transition_type"] or "unknown") for row in rows)
    total_row = conn.execute("SELECT COUNT(*) FROM m229_threshold_promotion_gate_transitions").fetchone()
    return {
        "transition_count": int(total_row[0] or 0) if total_row else len(rows),
        "transition_type_counts": dict(counts),
        "recent_transitions": [{key: row[key] for key in row.keys()} for row in rows[:10]],
    }


def _promotion_gate_backtest_report(reviews: list[dict[str, Any]], conn: sqlite3.Connection | None) -> dict[str, Any]:
    status_counts = Counter(str(item.get("gate_status") or "unknown") for item in reviews)
    acceptance_by_status: dict[str, list[float]] = {}
    for item in reviews:
        status = str(item.get("gate_status") or "unknown")
        trial = item.get("trial") or {}
        score = trial.get("acceptance_score")
        if score is not None:
            acceptance_by_status.setdefault(status, []).append(float(score))
    avg_acceptance = {
        status: round(sum(values) / len(values), 4)
        for status, values in acceptance_by_status.items()
        if values
    }
    historical_status_counts: Counter[str] = Counter()
    historical_watchlist_stale_count = 0
    if conn is not None:
        try:
            rows = conn.execute(
                """SELECT gate_status, review_payload_json
                   FROM m228_threshold_promotion_gate_reviews
                   ORDER BY id DESC
                   LIMIT 500"""
            ).fetchall()
            historical_status_counts.update(str(row["gate_status"] or "unknown") for row in rows)
            for row in rows:
                payload = _json_loads(row["review_payload_json"], {})
                if (payload.get("history_state") or {}).get("watchlist_stale"):
                    historical_watchlist_stale_count += 1
        except Exception:
            pass
    return {
        "report_version": "m229-promotion-gate-backtest-report-v1",
        "method": "shadow gate replay using stored reviews, transition history, and shadow acceptance scores; no live threshold mutation",
        "current_review_status_counts": dict(status_counts),
        "current_avg_acceptance_score_by_status": avg_acceptance,
        "historical_recent_status_counts": dict(historical_status_counts),
        "historical_watchlist_stale_review_count": historical_watchlist_stale_count,
        "interpretation": {
            "waitlist": "candidate passed all current gate checks but remains shadow-only",
            "watchlist": "candidate has only soft unresolved requirements and should be rechecked",
            "blocked": "candidate has at least one hard safety or evidence failure",
        },
    }


def _watchlist_aging_summary(reviews: list[dict[str, Any]]) -> dict[str, Any]:
    watchlist = [item for item in reviews if item.get("gate_status") == "promotion_watchlist"]
    stale = [item for item in watchlist if (item.get("history_state") or {}).get("watchlist_stale")]
    oldest = sorted(
        watchlist,
        key=lambda item: int((item.get("history_state") or {}).get("status_age_review_count") or 0),
        reverse=True,
    )[:10]
    return {
        "watchlist_count": len(watchlist),
        "stale_watchlist_count": len(stale),
        "stale_threshold_ticks": max(
            [int((item.get("history_state") or {}).get("watchlist_stale_threshold_ticks") or 0) for item in watchlist] or [0]
        ),
        "oldest_watchlist_candidates": [
            {
                "candidate_key": item.get("candidate_key"),
                "semantic_family_key": item.get("semantic_family_key"),
                "status_age_review_count": (item.get("history_state") or {}).get("status_age_review_count"),
                "watchlist_stale": (item.get("history_state") or {}).get("watchlist_stale"),
                "detail_reasons": item.get("detail_reasons") or {},
            }
            for item in oldest
        ],
    }


def _option_confidence_root_cause_summary(reviews: list[dict[str, Any]]) -> dict[str, Any]:
    root_counts: Counter[str] = Counter()
    family_counts: Counter[str] = Counter()
    diagnostic_samples: list[dict[str, Any]] = []
    for item in reviews:
        option_details = ((item.get("checks") or {}).get("option_structure_conflict") or {}).get("details") or {}
        diagnostics = option_details.get("option_confidence_diagnostics") or {}
        roots = [str(root) for root in diagnostics.get("root_cause_tags") or []]
        root_counts.update(roots)
        if roots:
            family_counts.update([str(item.get("semantic_family_key") or item.get("candidate_key") or "unknown")])
            diagnostic_samples.append(
                {
                    "candidate_key": item.get("candidate_key"),
                    "semantic_family_key": item.get("semantic_family_key"),
                    "root_cause_tags": roots,
                    "confidence_counts": diagnostics.get("confidence_counts") or {},
                    "missing_open_interest_symbols": diagnostics.get("missing_open_interest_symbols") or [],
                    "missing_bid_ask_symbols": diagnostics.get("missing_bid_ask_symbols") or [],
                    "wide_spread_symbols": diagnostics.get("wide_spread_symbols") or [],
                }
            )
    return {
        "summary_version": "m229-option-confidence-root-cause-summary-v1",
        "root_cause_counts": dict(root_counts),
        "family_counts": dict(family_counts),
        "diagnostic_samples": diagnostic_samples[:10],
    }


def _recycle_learning_summary(reviews: list[dict[str, Any]]) -> dict[str, Any]:
    plans = [item.get("recycle_learning_plan") or {} for item in reviews if (item.get("recycle_learning_plan") or {}).get("enabled")]
    priority_counts = Counter(str(plan.get("priority") or "unknown") for plan in plans)
    return_criteria = [
        item.get("recycle_return_criteria") or {}
        for item in reviews
        if (item.get("stale_watchlist_policy") or {}).get("operational_gate_status") == "promotion_recycle_candidate"
    ]
    blocked_counts: Counter[str] = Counter()
    eligible_count = 0
    active_states: list[dict[str, Any]] = []
    lifecycle_counts: Counter[str] = Counter()
    near_return: list[dict[str, Any]] = []
    lifecycle_by_key = {
        str(item.get("candidate_key") or ""): item.get("recycle_lifecycle_policy") or {}
        for item in reviews
        if item.get("recycle_lifecycle_policy")
    }
    for criteria in return_criteria:
        blocked_counts.update(str(item) for item in criteria.get("blocked_by") or [])
        if criteria.get("eligible_to_return_now"):
            eligible_count += 1
        remaining = int(criteria.get("remaining_blocker_count") or len(criteria.get("blocked_by") or []))
        if remaining <= 1:
            near_return.append(
                {
                    "candidate_key": criteria.get("candidate_key"),
                    "remaining_blocker_count": remaining,
                    "blocked_by": criteria.get("blocked_by") or [],
                    "eligible_to_return_now": criteria.get("eligible_to_return_now"),
                }
            )
        state = criteria.get("recycle_state") or {}
        if state:
            lifecycle = lifecycle_by_key.get(str(state.get("candidate_key") or "")) or {}
            active_states.append(
                {
                    "candidate_key": state.get("candidate_key"),
                    "status": state.get("status"),
                    "lifecycle_status": lifecycle.get("lifecycle_status"),
                    "operational_gate_status": lifecycle.get("operational_gate_status"),
                    "new_sample_count": state.get("new_sample_count"),
                    "target_new_sample_count": ((state.get("targets") or {}).get("new_sample_count")),
                    "consecutive_option_confidence_ticks": state.get("consecutive_option_confidence_ticks"),
                    "target_confidence_ticks": ((state.get("targets") or {}).get("consecutive_option_confidence_ticks")),
                    "quarantine_remaining_ticks": state.get("quarantine_remaining_ticks"),
                    "root_causes": state.get("option_confidence_root_causes") or [],
                }
            )
    for item in reviews:
        lifecycle = item.get("recycle_lifecycle_policy") or {}
        if lifecycle:
            lifecycle_counts.update([str(lifecycle.get("lifecycle_status") or "unknown")])
    return {
        "summary_version": "m231-recycle-learning-summary-v3",
        "recycle_candidate_count": len(plans),
        "eligible_to_return_count": eligible_count,
        "near_return_count": len(near_return),
        "lifecycle_status_counts": dict(lifecycle_counts),
        "priority_counts": dict(priority_counts),
        "return_blocker_counts": dict(blocked_counts),
        "active_states": active_states[:10],
        "near_return_candidates": near_return[:10],
        "plans": plans[:10],
        "return_criteria": return_criteria[:10],
    }


def _recycle_progress_history_summary(conn: sqlite3.Connection | None) -> dict[str, Any]:
    if conn is None:
        return {"summary_version": "m231-recycle-progress-history-summary-v1", "recent_rows": []}
    try:
        rows = conn.execute(
            """SELECT candidate_key,recorded_at,operational_gate_status,lifecycle_status,
                      new_sample_count,consecutive_option_confidence_ticks,quarantine_remaining_ticks,
                      blocked_by_json,root_causes_json,scorer_action
               FROM m231_threshold_promotion_recycle_history
               ORDER BY id DESC
               LIMIT 30"""
        ).fetchall()
    except Exception:
        rows = []
    lifecycle_counts = Counter(str(row["lifecycle_status"] or "unknown") for row in rows)
    return {
        "summary_version": "m231-recycle-progress-history-summary-v1",
        "recent_count": len(rows),
        "recent_lifecycle_counts": dict(lifecycle_counts),
        "recent_rows": [
            {
                "candidate_key": row["candidate_key"],
                "recorded_at": row["recorded_at"],
                "operational_gate_status": row["operational_gate_status"],
                "lifecycle_status": row["lifecycle_status"],
                "new_sample_count": row["new_sample_count"],
                "consecutive_option_confidence_ticks": row["consecutive_option_confidence_ticks"],
                "quarantine_remaining_ticks": row["quarantine_remaining_ticks"],
                "blocked_by": _json_loads(row["blocked_by_json"], []),
                "root_causes": _json_loads(row["root_causes_json"], []),
                "scorer_action": row["scorer_action"],
            }
            for row in rows[:10]
        ],
    }


def _recycle_graduation_report(reviews: list[dict[str, Any]], conn: sqlite3.Connection | None) -> dict[str, Any]:
    current_rows: list[dict[str, Any]] = []
    for item in reviews:
        state = item.get("recycle_state") or {}
        lifecycle = item.get("recycle_lifecycle_policy") or {}
        criteria = item.get("recycle_return_criteria") or {}
        if not state and not lifecycle and not criteria:
            continue
        current_rows.append(
            {
                "candidate_key": item.get("candidate_key"),
                "gate_status": item.get("gate_status"),
                "operational_gate_status": lifecycle.get("operational_gate_status")
                or (item.get("stale_watchlist_policy") or {}).get("operational_gate_status"),
                "lifecycle_status": lifecycle.get("lifecycle_status") or "unknown",
                "new_sample_count": state.get("new_sample_count"),
                "target_new_sample_count": ((state.get("targets") or {}).get("new_sample_count")),
                "consecutive_option_confidence_ticks": state.get("consecutive_option_confidence_ticks"),
                "target_confidence_ticks": ((state.get("targets") or {}).get("consecutive_option_confidence_ticks")),
                "quarantine_remaining_ticks": state.get("quarantine_remaining_ticks"),
                "remaining_blocker_count": criteria.get("remaining_blocker_count"),
                "blocked_by": criteria.get("blocked_by") or [],
                "recommendation": lifecycle.get("recommendation"),
            }
        )
    historical_latest: dict[str, dict[str, Any]] = {}
    state_rows: list[sqlite3.Row] = []
    history_rows: list[sqlite3.Row] = []
    if conn is not None:
        try:
            state_rows = conn.execute(
                """SELECT candidate_key,current_sample_count,new_sample_count,recycle_review_count,
                          consecutive_option_confidence_ticks,quarantine_remaining_ticks,status,
                          updated_at,state_payload_json
                   FROM m230_threshold_promotion_recycle_state
                   ORDER BY updated_at DESC
                   LIMIT 250"""
            ).fetchall()
        except Exception:
            state_rows = []
        try:
            history_rows = conn.execute(
                """SELECT candidate_key,recorded_at,operational_gate_status,lifecycle_status,
                          new_sample_count,consecutive_option_confidence_ticks,quarantine_remaining_ticks,
                          blocked_by_json,root_causes_json,scorer_action
                   FROM m231_threshold_promotion_recycle_history
                   ORDER BY id DESC
                   LIMIT 500"""
            ).fetchall()
        except Exception:
            history_rows = []
    for row in history_rows:
        key = str(row["candidate_key"] or "")
        if key in historical_latest:
            continue
        historical_latest[key] = {
            "candidate_key": key,
            "recorded_at": row["recorded_at"],
            "operational_gate_status": row["operational_gate_status"],
            "lifecycle_status": row["lifecycle_status"],
            "new_sample_count": row["new_sample_count"],
            "consecutive_option_confidence_ticks": row["consecutive_option_confidence_ticks"],
            "quarantine_remaining_ticks": row["quarantine_remaining_ticks"],
            "blocked_by": _json_loads(row["blocked_by_json"], []),
            "root_causes": _json_loads(row["root_causes_json"], []),
            "scorer_action": row["scorer_action"],
        }
    current_counts = Counter(str(item.get("lifecycle_status") or "unknown") for item in current_rows)
    historical_counts = Counter(str(item.get("lifecycle_status") or "unknown") for item in historical_latest.values())
    terminal_rows = [
        row
        for row in historical_latest.values()
        if str(row.get("lifecycle_status") or "").lower() in {"dormant", "retired"}
    ]
    near_graduation_rows = [
        row
        for row in current_rows
        if int(row.get("remaining_blocker_count") if row.get("remaining_blocker_count") is not None else 99) <= 1
    ]
    state_samples = []
    for row in state_rows[:20]:
        payload = _json_loads(row["state_payload_json"], {})
        state_samples.append(
            {
                "candidate_key": row["candidate_key"],
                "status": row["status"],
                "lifecycle_status": payload.get("lifecycle_status"),
                "current_sample_count": row["current_sample_count"],
                "new_sample_count": row["new_sample_count"],
                "recycle_review_count": row["recycle_review_count"],
                "consecutive_option_confidence_ticks": row["consecutive_option_confidence_ticks"],
                "quarantine_remaining_ticks": row["quarantine_remaining_ticks"],
                "updated_at": row["updated_at"],
            }
        )
    return {
        "report_version": "m231-recycle-graduation-report-v1",
        "generated_at": _iso_now(),
        "current_lifecycle_counts": dict(current_counts),
        "historical_latest_lifecycle_counts": dict(historical_counts),
        "terminal_candidate_count": len(terminal_rows),
        "near_graduation_count": len(near_graduation_rows),
        "dormant_or_retired_candidates": terminal_rows[:20],
        "near_graduation_candidates": near_graduation_rows[:20],
        "state_samples": state_samples,
        "operator_summary": {
            "dormant_meaning": "Candidate is paused from active promotion pressure until fresh samples or option confidence improve.",
            "retired_meaning": "Candidate has stayed unrecovered past the retirement threshold and should not auto-return without a new cycle.",
            "return_path": "recycle -> near_graduation -> promotion_watchlist -> promotion_waitlist, only if return criteria pass.",
        },
        "safety": {
            "read_only_report": True,
            "live_threshold_table_mutated": False,
            "broker_orders_submitted": False,
        },
    }


def _scorer_policy_summary(reviews: list[dict[str, Any]]) -> dict[str, Any]:
    policies = [
        item.get("scorer_threshold_application_policy") or {}
        for item in reviews
        if item.get("scorer_threshold_application_policy")
    ]
    action_counts = Counter(str(policy.get("scorer_action") or "unknown") for policy in policies)
    return {
        "summary_version": "m229-scorer-threshold-policy-summary-v1",
        "policy_count": len(policies),
        "scorer_action_counts": dict(action_counts),
        "policies": policies[:20],
    }


def _promotion_gate_audit_report(reviews: list[dict[str, Any]], conn: sqlite3.Connection | None) -> dict[str, Any]:
    operational_counts = Counter(str((item.get("stale_watchlist_policy") or {}).get("operational_gate_status") or item.get("gate_status") or "unknown") for item in reviews)
    detail_counts = Counter()
    for item in reviews:
        detail_counts.update(str(value) for value in (item.get("detail_reasons") or {}).values())
    action_counts = Counter()
    alert_count = 0
    for item in reviews:
        action_counts.update(str(req.get("action_id") or "unknown") for req in item.get("action_requests") or [])
        if (item.get("alert_signal") or {}).get("enabled"):
            alert_count += 1
    historical_rows = []
    if conn is not None:
        try:
            historical_rows = conn.execute(
                """SELECT reviewed_at, gate_status, review_payload_json
                   FROM m228_threshold_promotion_gate_reviews
                   ORDER BY id DESC
                   LIMIT 250"""
            ).fetchall()
        except Exception:
            historical_rows = []
    historical_operational = Counter()
    for row in historical_rows:
        payload = _json_loads(row["review_payload_json"], {})
        operational = (payload.get("stale_watchlist_policy") or {}).get("operational_gate_status") or payload.get("gate_status") or row["gate_status"]
        historical_operational.update([str(operational or "unknown")])
    return {
        "audit_report_version": "m229-promotion-gate-audit-report-v1",
        "generated_at": _iso_now(),
        "current_operational_status_counts": dict(operational_counts),
        "current_detail_reason_counts": dict(detail_counts),
        "current_action_request_counts": dict(action_counts),
        "current_alert_signal_count": alert_count,
        "historical_recent_operational_status_counts": dict(historical_operational),
        "option_confidence_bottlenecks": _option_confidence_root_cause_summary(reviews),
        "recycle_learning": _recycle_learning_summary(reviews),
        "scorer_threshold_policy": _scorer_policy_summary(reviews),
        "safety": {
            "read_only_report": True,
            "live_threshold_mutated": False,
            "broker_orders_submitted": False,
        },
    }


def _promotion_gate_summary(
    reviews: list[dict[str, Any]],
    config: ThresholdCandidateEvaluatorConfig,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    status_counts = Counter(str(item.get("gate_status") or "unknown") for item in reviews)
    blocked_reason_counts: Counter[str] = Counter()
    watchlist_reason_counts: Counter[str] = Counter()
    all_issue_reason_counts: Counter[str] = Counter()
    for item in reviews:
        blocked_reason_counts.update(str(reason) for reason in item.get("blocked_reasons") or [])
        watchlist_reason_counts.update(str(reason) for reason in item.get("watchlist_reasons") or [])
        all_issue_reason_counts.update(str(reason) for reason in item.get("issue_reasons") or item.get("blocked_reasons") or [])
    waitlist = [item for item in reviews if item.get("ready_for_promotion_waitlist")]
    watchlist = [item for item in reviews if item.get("gate_status") == "promotion_watchlist"]
    blocked = [item for item in reviews if item.get("gate_status") == "promotion_gate_blocked"]
    blocked_detail_reason_counts = _promotion_gate_detail_reason_counts(reviews, "blocked_reasons")
    watchlist_detail_reason_counts = _promotion_gate_detail_reason_counts(reviews, "watchlist_reasons")
    option_session_deferred_watchlist_count = int(watchlist_detail_reason_counts.get("option_structure_expected_deferred_stale") or 0)
    option_session_deferred_only = bool(
        len(blocked) == 0
        and len(waitlist) == 0
        and len(watchlist) > 0
        and option_session_deferred_watchlist_count > 0
        and option_session_deferred_watchlist_count >= len(watchlist)
    )
    requirement_counts: Counter[str] = Counter()
    for item in reviews:
        for requirement in item.get("promotion_requirements") or []:
            requirement_counts.update([str(requirement.get("reason") or requirement.get("check") or "unknown")])
    conditional = [
        item
        for item in reviews
        if (item.get("conditional_promotion") or {}).get("eligible_for_conditional_promotion_queue")
    ]
    conditional_mode_counts = Counter(str((item.get("conditional_promotion") or {}).get("mode") or "unknown") for item in conditional)
    operational_status_counts = Counter(str((item.get("stale_watchlist_policy") or {}).get("operational_gate_status") or item.get("gate_status") or "unknown") for item in reviews)
    action_requests = [
        request
        for item in reviews
        for request in (item.get("action_requests") or [])
    ]
    alert_signals = [
        item.get("alert_signal") or {}
        for item in reviews
        if (item.get("alert_signal") or {}).get("enabled")
    ]
    scorer_hooks = [
        item.get("scorer_evidence_packet_hook") or {}
        for item in reviews
        if item.get("scorer_evidence_packet_hook")
    ]
    scorer_threshold_policies = [
        item.get("scorer_threshold_application_policy") or {}
        for item in reviews
        if item.get("scorer_threshold_application_policy")
    ]
    return {
        "promotion_gate_version": "m231-threshold-promotion-gate-v7",
        "enabled": bool(config.promotion_gate_enabled),
        "review_count": len(reviews),
        "waitlist_count": len(waitlist),
        "watchlist_count": len(watchlist),
        "blocked_count": len(blocked),
        "status_counts": dict(status_counts),
        "operational_status_counts": dict(operational_status_counts),
        "blocked_reason_counts": dict(blocked_reason_counts),
        "watchlist_reason_counts": dict(watchlist_reason_counts),
        "blocked_detail_reason_counts": dict(blocked_detail_reason_counts),
        "watchlist_detail_reason_counts": dict(watchlist_detail_reason_counts),
        "option_session_deferred_watchlist_count": option_session_deferred_watchlist_count,
        "option_session_deferred_only": option_session_deferred_only,
        "all_issue_reason_counts": dict(all_issue_reason_counts),
        "promotion_requirement_counts": dict(requirement_counts),
        "transition_history": _promotion_gate_transition_summary(conn),
        "watchlist_aging": _watchlist_aging_summary(reviews),
        "conditional_promotion_count": len(conditional),
        "conditional_promotion_mode_counts": dict(conditional_mode_counts),
        "conditional_promotion_candidates": conditional[:10],
        "stale_watchlist_policy": {
            "policy_version": "m229-stale-watchlist-policy-v1",
            "stale_threshold_ticks": int(config.promotion_gate_watchlist_stale_ticks),
            "recycle_threshold_ticks": int(config.promotion_gate_watchlist_recycle_ticks),
            "recycle_quarantine_ticks": int(config.promotion_gate_recycle_quarantine_ticks),
            "operational_status_counts": dict(operational_status_counts),
            "option_refresh_required_count": sum(1 for item in reviews if (item.get("stale_watchlist_policy") or {}).get("option_refresh_required")),
            "recycle_recommended_count": sum(1 for item in reviews if (item.get("stale_watchlist_policy") or {}).get("recycle_recommended")),
        },
        "action_request_count": len(action_requests),
        "action_requests": action_requests[:20],
        "scorer_evidence_packet_hooks": scorer_hooks[:20],
        "scorer_threshold_application_policies": scorer_threshold_policies[:20],
        "option_confidence_root_causes": _option_confidence_root_cause_summary(reviews),
        "recycle_learning": _recycle_learning_summary(reviews),
        "recycle_progress_history": _recycle_progress_history_summary(conn),
        "recycle_graduation_report": _recycle_graduation_report(reviews, conn),
        "scorer_threshold_policy_summary": _scorer_policy_summary(reviews),
        "audit_report": _promotion_gate_audit_report(reviews, conn),
        "alert_signal_count": len(alert_signals),
        "alert_signals": alert_signals[:20],
        "backtest_report": _promotion_gate_backtest_report(reviews, conn),
        "waitlist_candidates": waitlist[:10],
        "watchlist_candidates": sorted(watchlist, key=lambda item: int(item.get("failed_check_count") or 0), reverse=True)[:10],
        "blocked_candidates": sorted(blocked, key=lambda item: int(item.get("failed_check_count") or 0), reverse=True)[:10],
        "auto_recheck": {
            "enabled": True,
            "watchlist_candidates_rechecked_each_tick": True,
            "promotion_path": "promotion_watchlist -> promotion_waitlist when all requirements pass",
            "demotion_path": "promotion_watchlist -> promotion_gate_blocked if a hard requirement fails",
        },
        "policy": {
            "only_m226_promotion_ready_candidates_reviewed": True,
            "three_stage_gate": ["promotion_gate_blocked", "promotion_watchlist", "promotion_waitlist"],
            "watchlist_enabled": bool(config.promotion_gate_watchlist_enabled),
            "minimum_sample_count": int(config.promotion_gate_min_sample_count),
            "recent_trial_count": int(config.promotion_gate_recent_trial_count),
            "minimum_recent_acceptance_score": float(config.promotion_gate_min_recent_acceptance_score),
            "max_abs_error_worsening_pct": float(config.promotion_gate_max_abs_error_worsening_pct),
            "max_regime_family_share": float(config.promotion_gate_max_regime_family_share),
        },
        "safety": {
            "live_threshold_table_mutated": False,
            "real_promotion_performed": False,
            "broker_orders_submitted": False,
        },
    }


def _cycle_report(evaluations: list[dict[str, Any]], state: dict[str, Any]) -> dict[str, Any]:
    current_families = [str(item.get("semantic_family_key") or item.get("candidate_key") or "") for item in evaluations]
    previous_families = [str(item) for item in state.get("previous_semantic_family_keys") or []]
    current_set = set(current_families)
    previous_set = set(previous_families)
    status_counts = Counter(str(item.get("status") or "unknown") for item in evaluations)
    severity_counts = Counter(str(item.get("blocker_severity") or "none") for item in evaluations)
    return {
        "cycle_report_version": "m226-cycle-report-v1",
        "generated_at": _iso_now(),
        "family_count": len(current_set),
        "new_family_count": len(current_set - previous_set),
        "continued_family_count": len(current_set & previous_set),
        "disappeared_family_count": len(previous_set - current_set),
        "new_families": sorted(current_set - previous_set)[:20],
        "continued_families": sorted(current_set & previous_set)[:20],
        "disappeared_families": sorted(previous_set - current_set)[:20],
        "status_counts": dict(status_counts),
        "blocker_severity_counts": dict(severity_counts),
        "ready_to_shadow_trial_count": int(status_counts.get("promotion_ready", 0)),
        "needs_more_samples_count": int(status_counts.get("needs_more_samples", 0)),
        "ineffective_or_hard_blocked_count": int(status_counts.get("reject", 0)) + int(severity_counts.get("hard", 0)),
    }


def _evaluate_threshold_candidates_once(config: ThresholdCandidateEvaluatorConfig, *, state: dict[str, Any] | None = None) -> dict[str, Any]:
    ensure_live_learning_schema(config.database_path)
    evaluated_at = _iso_now()
    evaluations: list[dict[str, Any]] = []
    missing_m225_table = False
    refresh_state: dict[str, Any] = {}
    adaptive_refresh: dict[str, Any] = {}
    semantic_family_summary: dict[str, Any] = {}
    shadow_trials: list[dict[str, Any]] = []
    promotion_gate_reviews: list[dict[str, Any]] = []
    promotion_gate: dict[str, Any] = {}
    external_context = {
        "evidence_freshness": _read_json_file(config.evidence_freshness_json_path),
        "options_market_structure": _read_json_file(config.options_market_structure_json_path),
        "options_market_structure_supervisor": _read_json_file(config.options_market_structure_supervisor_json_path),
        "portfolio_risk_state": _read_json_file(config.portfolio_risk_state_json_path),
    }
    state = state if state is not None else {}
    with _connect(config.database_path) as conn:
        _ensure_m226_schema(conn)
        refresh_state = _candidate_refresh_state(conn)
        adaptive_refresh = _adaptive_refresh(config, refresh_state, state)
        existing_tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        if "m225_llm_note_threshold_candidates" not in existing_tables:
            missing_m225_table = True
            rows: list[sqlite3.Row] = []
        else:
            rows = _candidate_rows(conn, config)
        candidate_rows, semantic_family_summary = _candidate_family_rows(conn, rows, config)
        for row in candidate_rows:
            note_ids = [int(item) for item in _json_loads(row["source_note_ids_json"], []) if str(item).isdigit()]
            metrics = _note_metrics(conn, note_ids)
            item = _score_candidate(row, metrics, config)
            evaluations.append(item)
            if not config.dry_run:
                _store_evaluation(conn, item)
            if config.shadow_promotion_trial_enabled and item.get("status") == "promotion_ready":
                trial = _shadow_trial_for_item(conn, item, config)
                shadow_trials.append(trial)
                if not config.dry_run:
                    _store_shadow_trial(conn, trial)
                if config.promotion_gate_enabled:
                    gate_review = _promotion_gate_review_for_item(conn, item, trial, config, external_context)
                    promotion_gate_reviews.append(gate_review)
                    if not config.dry_run:
                        _store_promotion_gate_review(conn, gate_review)
        cycle_report = _cycle_report(evaluations, state)
        state["last_candidate_updated_at"] = refresh_state.get("latest_candidate_updated_at")
        state["last_candidate_count_total"] = refresh_state.get("candidate_count_total")
        state["last_adaptive_refresh"] = adaptive_refresh
        state["previous_semantic_family_keys"] = [
            str(item.get("semantic_family_key") or item.get("candidate_key") or "")
            for item in evaluations
        ]
        state["last_cycle_report"] = cycle_report
        state["updated_at"] = evaluated_at
        promotion_gate = _promotion_gate_summary(promotion_gate_reviews, config, conn)

    status_counts = Counter(item["status"] for item in evaluations)
    blocker_counts = _blocker_counts(evaluations)
    blocker_severity_counts = _blocker_severity_counts(evaluations)
    focus_feedback = _m225_focus_feedback(evaluations, config)
    shadow_trial_summary = _shadow_promotion_trial_summary(shadow_trials, config)
    top = sorted(
        evaluations,
        key=lambda item: (float(item.get("promotion_score") or 0), int(item.get("sample_count") or 0)),
        reverse=True,
    )[:10]
    result = {
        "m226_threshold_candidate_evaluator_version": M226_THRESHOLD_CANDIDATE_EVALUATOR_VERSION,
        "evaluated_at": evaluated_at,
        "note_version": config.note_version,
        "candidate_count": len(evaluations),
        "candidate_count_total": refresh_state.get("candidate_count_total", len(evaluations)),
        "raw_candidate_count": semantic_family_summary.get("raw_candidate_count", len(evaluations)),
        "semantic_family_count": semantic_family_summary.get("family_count", len(evaluations)),
        "semantic_family_summary": semantic_family_summary,
        "adaptive_refresh": adaptive_refresh,
        "status_counts": dict(status_counts),
        "promotion_ready_count": int(status_counts.get("promotion_ready", 0)),
        "needs_more_samples_count": int(status_counts.get("needs_more_samples", 0)),
        "watch_count": int(status_counts.get("watch", 0)),
        "shadow_candidate_count": int(status_counts.get("shadow_candidate", 0)),
        "reject_count": int(status_counts.get("reject", 0)),
        "blocker_counts": blocker_counts,
        "blocker_severity_counts": blocker_severity_counts,
        "m225_focus_feedback": focus_feedback,
        "shadow_promotion_trial": shadow_trial_summary,
        "promotion_gate": promotion_gate,
        "cycle_report": state.get("last_cycle_report") or {},
        "top_candidates": top,
        "missing_m225_table": missing_m225_table,
        "dry_run": bool(config.dry_run),
        "shadow_only": True,
        "safety": {
            "live_scorer_mutated": False,
            "threshold_promoted": False,
            "broker_orders_submitted": False,
            "transmit_enabled": False,
        },
        "config": {
            "min_watch_sample_count": config.min_watch_sample_count,
            "min_promotion_sample_count": config.min_promotion_sample_count,
            "min_avg_confidence": config.min_avg_confidence,
            "min_avg_note_quality_score": config.min_avg_note_quality_score,
            "min_promotion_score": config.min_promotion_score,
            "min_estimated_error_reduction_bps": config.min_estimated_error_reduction_bps,
            "max_candidates": config.max_candidates,
            "semantic_family_enabled": config.semantic_family_enabled,
            "outcome_direction_split_enabled": config.outcome_direction_split_enabled,
            "max_focus_patterns": config.max_focus_patterns,
            "shadow_promotion_trial_enabled": config.shadow_promotion_trial_enabled,
            "min_shadow_trial_cycles": config.min_shadow_trial_cycles,
            "min_shadow_acceptance_score": config.min_shadow_acceptance_score,
            "promotion_gate_enabled": config.promotion_gate_enabled,
            "promotion_gate_min_sample_count": config.promotion_gate_min_sample_count,
            "promotion_gate_recent_trial_count": config.promotion_gate_recent_trial_count,
            "promotion_gate_min_recent_acceptance_score": config.promotion_gate_min_recent_acceptance_score,
            "promotion_gate_max_abs_error_worsening_pct": config.promotion_gate_max_abs_error_worsening_pct,
            "promotion_gate_max_regime_family_share": config.promotion_gate_max_regime_family_share,
            "promotion_gate_watchlist_enabled": config.promotion_gate_watchlist_enabled,
            "promotion_gate_watchlist_stale_ticks": config.promotion_gate_watchlist_stale_ticks,
            "promotion_gate_watchlist_recycle_ticks": config.promotion_gate_watchlist_recycle_ticks,
            "promotion_gate_recycle_min_new_samples": config.promotion_gate_recycle_min_new_samples,
            "promotion_gate_recycle_return_confidence_ticks": config.promotion_gate_recycle_return_confidence_ticks,
            "promotion_gate_recycle_quarantine_ticks": config.promotion_gate_recycle_quarantine_ticks,
            "promotion_gate_recycle_dormant_ticks": config.promotion_gate_recycle_dormant_ticks,
            "promotion_gate_recycle_retire_ticks": config.promotion_gate_recycle_retire_ticks,
            "promotion_gate_recycle_near_return_remaining_checks": config.promotion_gate_recycle_near_return_remaining_checks,
            "promotion_gate_option_root_cause_policy_enabled": config.promotion_gate_option_root_cause_policy_enabled,
            "promotion_gate_transition_history_limit": config.promotion_gate_transition_history_limit,
        },
    }
    return result


def _m226_loop_result(config: ThresholdCandidateEvaluatorConfig, started_at: str, tick_summaries: list[dict[str, Any]]) -> dict[str, Any]:
    latest = tick_summaries[-1] if tick_summaries else {}
    result = dict(latest)
    result["started_at"] = started_at
    result["completed_at"] = _iso_now()
    result["tick_count"] = len(tick_summaries)
    result["tick_summaries"] = tick_summaries[-20:]
    result["config"] = {
        **(latest.get("config") or {}),
        "max_ticks": config.max_ticks,
        "interval_seconds": config.interval_seconds,
        "sleep_between_ticks": config.sleep_between_ticks,
        "adaptive_refresh_enabled": config.adaptive_refresh_enabled,
    }
    return result


def evaluate_threshold_candidates(config: ThresholdCandidateEvaluatorConfig) -> dict[str, Any]:
    started_at = _iso_now()
    state = _read_state(config.state_json_path)
    tick_summaries: list[dict[str, Any]] = []
    max_ticks = int(config.max_ticks)
    continuous = max_ticks <= 0
    tick_index = 0
    while continuous or tick_index < max_ticks:
        tick_index += 1
        tick = _evaluate_threshold_candidates_once(config, state=state)
        tick["tick_index"] = tick_index
        tick_summaries.append(tick)
        _write_state(config.state_json_path, state)
        partial = _m226_loop_result(config, started_at, tick_summaries)
        if config.json_output_path:
            write_threshold_candidate_evaluator_json(config.json_output_path, partial)
        if config.output_path:
            write_threshold_candidate_evaluator_markdown(config.output_path, partial)
        if continuous or tick_index < max_ticks:
            adaptive = tick.get("adaptive_refresh") or {}
            sleep_seconds = float(adaptive.get("next_interval_seconds") if adaptive.get("next_interval_seconds") is not None else config.interval_seconds)
            if config.sleep_between_ticks and sleep_seconds > 0:
                time.sleep(sleep_seconds)
            else:
                break
    return _m226_loop_result(config, started_at, tick_summaries)


def threshold_candidate_evaluator_markdown(result: dict[str, Any]) -> str:
    adaptive = result.get("adaptive_refresh") or {}
    focus = result.get("m225_focus_feedback") or {}
    shadow_trial = result.get("shadow_promotion_trial") or {}
    promotion_gate = result.get("promotion_gate") or {}
    cycle_report = result.get("cycle_report") or {}
    lines = [
        "# M226 Threshold Candidate Evaluator",
        "",
        f"- Version: `{result.get('m226_threshold_candidate_evaluator_version')}`",
        f"- Evaluated at: `{result.get('evaluated_at')}`",
        f"- Tick count: `{result.get('tick_count', 1)}`",
        f"- Candidates: `{result.get('candidate_count')}`",
        f"- Total M225 candidate groups: `{result.get('candidate_count_total')}`",
        f"- Raw / semantic family count: `{result.get('raw_candidate_count')}` / `{result.get('semantic_family_count')}`",
        f"- Adaptive tier / changed / next interval: `{adaptive.get('tier') or '-'}` / `{adaptive.get('changed_since_last_tick')}` / `{adaptive.get('next_interval_seconds')}`",
        f"- Status counts: `{result.get('status_counts') or {}}`",
        f"- Blocker counts: `{result.get('blocker_counts') or {}}`",
        f"- Blocker severity counts: `{result.get('blocker_severity_counts') or {}}`",
        f"- Promotion ready: `{result.get('promotion_ready_count')}`",
        f"- Needs more samples: `{result.get('needs_more_samples_count')}`",
        f"- M225 focus patterns: `{focus.get('focus_pattern_count')}`",
        f"- Shadow trial active / ready: `{shadow_trial.get('active_count')}` / `{shadow_trial.get('ready_for_real_promotion_count')}`",
        f"- Promotion gate waitlist / watchlist / blocked: `{promotion_gate.get('waitlist_count')}` / `{promotion_gate.get('watchlist_count')}` / `{promotion_gate.get('blocked_count')}`",
        f"- Cycle new / continued / disappeared families: `{cycle_report.get('new_family_count')}` / `{cycle_report.get('continued_family_count')}` / `{cycle_report.get('disappeared_family_count')}`",
        f"- Shadow only: `{result.get('shadow_only')}`",
        "",
        "## Top candidates",
        "",
    ]
    top = result.get("top_candidates") or []
    if not top:
        lines.append("- none")
    for item in top:
        lines.extend(
            [
                f"- `{item.get('status')}` score `{item.get('promotion_score')}` samples `{item.get('sample_count')}` "
                f"quality `{item.get('avg_note_quality_score')}` confidence `{item.get('avg_confidence')}` "
                f"est_error_reduction_bps `{item.get('estimated_error_reduction_bps')}`",
                f"  - family: `{item.get('semantic_family_key') or item.get('reason_category')}`",
                f"  - outcome direction: `{item.get('outcome_direction_key') or '-'}`",
                f"  - blockers: `{item.get('blockers') or []}`",
                f"  - blocker severity: `{item.get('blocker_severity') or 'none'}`",
            ]
        )
    lines.extend(["", "## Shadow promotion trial", ""])
    trials = shadow_trial.get("top_trials") or []
    if not trials:
        lines.append("- none")
    for item in trials[:10]:
        lines.append(
            f"- `{item.get('trial_status')}` acceptance `{item.get('acceptance_score')}` cycles `{item.get('cycles_seen')}` "
            f"family `{item.get('semantic_family_key')}` ready `{item.get('ready_for_real_promotion')}`"
        )
    lines.extend(["", "## Promotion gate", ""])
    lines.extend(
        [
            f"- Waitlist candidates: `{promotion_gate.get('waitlist_count')}`",
            f"- Watchlist candidates: `{promotion_gate.get('watchlist_count')}`",
            f"- Blocked candidates: `{promotion_gate.get('blocked_count')}`",
            f"- Blocked reason counts: `{promotion_gate.get('blocked_reason_counts') or {}}`",
            f"- Watchlist reason counts: `{promotion_gate.get('watchlist_reason_counts') or {}}`",
            f"- Watchlist detail reasons: `{promotion_gate.get('watchlist_detail_reason_counts') or {}}`",
            f"- Promotion requirement counts: `{promotion_gate.get('promotion_requirement_counts') or {}}`",
            f"- Transition count: `{(promotion_gate.get('transition_history') or {}).get('transition_count')}`",
            f"- Watchlist stale / total: `{(promotion_gate.get('watchlist_aging') or {}).get('stale_watchlist_count')}` / `{(promotion_gate.get('watchlist_aging') or {}).get('watchlist_count')}`",
            f"- Conditional promotion candidates: `{promotion_gate.get('conditional_promotion_count')}` modes `{promotion_gate.get('conditional_promotion_mode_counts') or {}}`",
            f"- Backtest current status counts: `{((promotion_gate.get('backtest_report') or {}).get('current_review_status_counts')) or {}}`",
            f"- Operational status counts: `{promotion_gate.get('operational_status_counts') or {}}`",
            f"- Action requests / alert signals: `{promotion_gate.get('action_request_count')}` / `{promotion_gate.get('alert_signal_count')}`",
            f"- Scorer evidence packet hooks: `{len(promotion_gate.get('scorer_evidence_packet_hooks') or [])}`",
            f"- Option confidence root causes: `{(promotion_gate.get('option_confidence_root_causes') or {}).get('root_cause_counts') or {}}`",
            f"- Recycle learning candidates: `{(promotion_gate.get('recycle_learning') or {}).get('recycle_candidate_count')}` eligible-return `{(promotion_gate.get('recycle_learning') or {}).get('eligible_to_return_count')}` near-return `{(promotion_gate.get('recycle_learning') or {}).get('near_return_count')}` lifecycle `{(promotion_gate.get('recycle_learning') or {}).get('lifecycle_status_counts') or {}}` blockers `{(promotion_gate.get('recycle_learning') or {}).get('return_blocker_counts') or {}}`",
            f"- Recycle progress history rows: `{(promotion_gate.get('recycle_progress_history') or {}).get('recent_count')}` lifecycle `{(promotion_gate.get('recycle_progress_history') or {}).get('recent_lifecycle_counts') or {}}`",
            f"- Recycle graduation report: terminal `{(promotion_gate.get('recycle_graduation_report') or {}).get('terminal_candidate_count')}` near-graduation `{(promotion_gate.get('recycle_graduation_report') or {}).get('near_graduation_count')}` historical lifecycle `{(promotion_gate.get('recycle_graduation_report') or {}).get('historical_latest_lifecycle_counts') or {}}`",
            f"- Scorer threshold policy actions: `{(promotion_gate.get('scorer_threshold_policy_summary') or {}).get('scorer_action_counts') or {}}`",
        ]
    )
    blocked = promotion_gate.get("blocked_candidates") or []
    watchlist = promotion_gate.get("watchlist_candidates") or []
    if not blocked and not watchlist and not (promotion_gate.get("waitlist_candidates") or []):
        lines.append("- none")
    for item in watchlist[:10]:
        lines.append(
            f"- `{item.get('gate_status')}` issues `{item.get('failed_check_count')}` "
            f"operational `{item.get('operational_gate_status') or (item.get('stale_watchlist_policy') or {}).get('operational_gate_status')}` "
            f"family `{item.get('semantic_family_key')}` reasons `{item.get('watchlist_reasons') or []}` "
            f"details `{item.get('detail_reasons') or {}}`"
        )
        for requirement in (item.get("promotion_requirements") or [])[:3]:
            lines.append(
                f"  - need `{requirement.get('reason')}`: {requirement.get('requirement')}"
            )
        conditional = item.get("conditional_promotion") or {}
        if conditional:
            lines.append(
                f"  - conditional mode: `{conditional.get('mode')}` conditions `{len(conditional.get('conditions') or [])}`"
            )
        history = item.get("history_state") or {}
        if history:
            lines.append(
                f"  - age reviews: `{history.get('status_age_review_count')}` stale `{history.get('watchlist_stale')}` path `{history.get('recent_status_path') or []}`"
            )
        stale_policy = item.get("stale_watchlist_policy") or {}
        if stale_policy:
            lines.append(
                f"  - stale policy: `{stale_policy.get('operational_gate_status')}` action `{stale_policy.get('action')}` recycle `{stale_policy.get('recycle_recommended')}`"
            )
        hook = item.get("scorer_evidence_packet_hook") or {}
        if hook:
            lines.append(
                f"  - scorer hook: `{hook.get('application_policy')}` conditions `{len(hook.get('conditions') or [])}`"
            )
        threshold_policy = item.get("scorer_threshold_application_policy") or {}
        if threshold_policy:
            lines.append(
                f"  - threshold policy: `{threshold_policy.get('scorer_action')}` multiplier `{threshold_policy.get('confidence_multiplier')}`"
            )
        return_criteria = item.get("recycle_return_criteria") or {}
        if return_criteria:
            lines.append(
                f"  - return criteria: `{return_criteria.get('passed_count')}` / `{return_criteria.get('total_count')}` eligible `{return_criteria.get('eligible_to_return_now')}` blocked `{return_criteria.get('blocked_by') or []}`"
            )
        recycle_state = item.get("recycle_state") or {}
        if recycle_state and recycle_state.get("status") == "active":
            lines.append(
                f"  - recycle state: new samples `{recycle_state.get('new_sample_count')}` / `{(recycle_state.get('targets') or {}).get('new_sample_count')}` "
                f"confidence ticks `{recycle_state.get('consecutive_option_confidence_ticks')}` / `{(recycle_state.get('targets') or {}).get('consecutive_option_confidence_ticks')}` "
                f"quarantine remaining `{recycle_state.get('quarantine_remaining_ticks')}`"
            )
        lifecycle = item.get("recycle_lifecycle_policy") or {}
        if lifecycle:
            lines.append(
                f"  - lifecycle: `{lifecycle.get('lifecycle_status')}` operational `{lifecycle.get('operational_gate_status')}` review_count `{lifecycle.get('review_count')}`"
            )
        root_policy = item.get("option_root_cause_policy") or {}
        if root_policy:
            lines.append(
                f"  - root-cause policy: blocks `{root_policy.get('blocks_return')}` multiplier `{root_policy.get('confidence_multiplier')}` actions `{root_policy.get('actions') or []}`"
            )
        recycle_plan = item.get("recycle_learning_plan") or {}
        if recycle_plan.get("enabled"):
            lines.append(
                f"  - recycle learning: priority `{recycle_plan.get('priority')}` target samples `{recycle_plan.get('target_new_sample_count')}`"
            )
    for item in blocked[:10]:
        lines.append(
            f"- `{item.get('gate_status')}` failed `{item.get('failed_check_count')}` "
            f"family `{item.get('semantic_family_key')}` reasons `{item.get('blocked_reasons') or []}` "
            f"details `{item.get('detail_reasons') or {}}`"
        )
    lines.extend(["", "## Cycle report", ""])
    lines.extend(
        [
            f"- New families: `{cycle_report.get('new_families') or []}`",
            f"- Continued families: `{cycle_report.get('continued_families') or []}`",
            f"- Disappeared families: `{cycle_report.get('disappeared_families') or []}`",
        ]
    )
    lines.extend(["", "## M225 focus feedback", ""])
    patterns = focus.get("focus_patterns") or []
    if not patterns:
        lines.append("- none")
    for item in patterns[:10]:
        lines.append(
            f"- `{item.get('semantic_family_key')}` status `{item.get('status')}` "
            f"direction `{item.get('outcome_direction_key')}` samples `{item.get('sample_count')}` "
            f"target `{item.get('target_total_samples')}` additional `{item.get('target_additional_samples')}` "
            f"severity `{item.get('blocker_severity')}`"
        )
    lines.extend(
        [
            "",
            "## Safety",
            "",
            "- This evaluator is shadow-only. It does not mutate live scorer thresholds, promote candidates, submit orders, or enable transmit.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_threshold_candidate_evaluator_json(path: Path, result: dict[str, Any]) -> Path:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    summary_path = resolved.with_name(f"{resolved.stem}_summary{resolved.suffix}")
    write_threshold_candidate_evaluator_summary_json(summary_path, result)
    return resolved


def _compact_for_summary(value: Any, *, depth: int = 0, list_limit: int = 8) -> Any:
    if depth >= 5:
        if isinstance(value, dict):
            return {"_truncated": True, "key_count": len(value)}
        if isinstance(value, list):
            return {"_truncated": True, "item_count": len(value)}
        return value
    if isinstance(value, dict):
        return {str(key): _compact_for_summary(item, depth=depth + 1, list_limit=list_limit) for key, item in value.items()}
    if isinstance(value, list):
        return [_compact_for_summary(item, depth=depth + 1, list_limit=list_limit) for item in value[:list_limit]]
    return value


def threshold_candidate_evaluator_summary_json(result: dict[str, Any]) -> dict[str, Any]:
    keep_keys = (
        "m226_threshold_candidate_evaluator_version",
        "started_at",
        "completed_at",
        "evaluated_at",
        "note_version",
        "candidate_count",
        "candidate_count_total",
        "raw_candidate_count",
        "semantic_family_count",
        "semantic_family_summary",
        "adaptive_refresh",
        "tick_count",
        "status_counts",
        "promotion_ready_count",
        "needs_more_samples_count",
        "watch_count",
        "shadow_candidate_count",
        "reject_count",
        "blocker_counts",
        "blocker_severity_counts",
        "m225_focus_feedback",
        "shadow_promotion_trial",
        "promotion_gate",
        "cycle_report",
        "top_candidates",
        "missing_m225_table",
        "dry_run",
        "shadow_only",
        "safety",
        "config",
    )
    summary = {
        key: _compact_for_summary(result.get(key), list_limit=8)
        for key in keep_keys
        if key in result
    }
    summary["m226_threshold_candidate_evaluator_summary_version"] = M226_THRESHOLD_CANDIDATE_EVALUATOR_SUMMARY_VERSION
    summary["summary_generated_at"] = _iso_now()
    summary["source_payload_compacted"] = True
    summary["compaction_policy"] = {
        "list_limit": 8,
        "max_depth": 5,
        "purpose": "dashboard_hot_path_uses_summary_instead_of_large_m226_payload",
    }
    return summary


def write_threshold_candidate_evaluator_summary_json(path: Path, result: dict[str, Any]) -> Path:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(
        json.dumps(threshold_candidate_evaluator_summary_json(result), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return resolved


def write_threshold_candidate_evaluator_markdown(path: Path, result: dict[str, Any]) -> Path:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(threshold_candidate_evaluator_markdown(result), encoding="utf-8")
    return resolved
