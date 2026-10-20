from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, time as dt_time, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .live_learning_control_plane import compact_m232_hot_cache_for_scorer
from .news_asset_mapper import infer_news_assets, preferred_trading_symbol


M223_LIVE_SCORER_LEARNING_VERSION = "m223-live-scorer-learning-v2-evidence-packet"
M224_EVIDENCE_PACKET_VERSION = "m224-evidence-packet-v2"
UTC = timezone.utc


@dataclass(frozen=True)
class LiveScorerLearningConfig:
    database_path: Path
    market_data_json_path: Path | None = None
    evidence_freshness_json_path: Path | None = None
    source_online_supervisor_json_path: Path | None = None
    options_market_structure_json_path: Path | None = None
    options_market_structure_supervisor_json_path: Path | None = None
    m232_hot_cache_json_path: Path | None = None
    max_ticks: int = 1
    interval_seconds: float = 1.0
    sleep_between_ticks: bool = False
    lookback_minutes: int = 180
    max_new_events_per_tick: int = 25
    horizons_minutes: tuple[int, ...] = (5, 15, 30)
    scorer_name: str = "m223_shadow_heuristic"
    scorer_version: str = "shadow-threshold-candidate-v1"
    magnitude_threshold_pct: float = 0.15
    current_time: datetime | None = None
    output_path: Path | None = None
    json_output_path: Path | None = None


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
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


def _local_time_text(value: Any, default: str = "-") -> str:
    parsed = _parse_dt(value)
    if parsed is None:
        return default
    local = parsed.astimezone()
    zone = local.tzname() or "local"
    return f"{local.strftime('%Y-%m-%d %H:%M:%S')} {zone} ({local.strftime('%z')})"


def _read_json(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        return json.loads(path.expanduser().resolve().read_text(encoding="utf-8"))
    except Exception:
        return {}


def _json_hash(payload: Any) -> str:
    return sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _connect(path: Path) -> sqlite3.Connection:
    path.expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path.expanduser().resolve(), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_live_learning_schema(database_path: Path) -> None:
    with _connect(database_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS m223_live_learning_predictions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_table TEXT NOT NULL,
                source_id INTEGER NOT NULL,
                event_hash TEXT NOT NULL UNIQUE,
                event_at TEXT NOT NULL,
                event_at_local TEXT NOT NULL DEFAULT '',
                provider TEXT NOT NULL DEFAULT '',
                symbol TEXT NOT NULL DEFAULT '',
                symbol_source TEXT NOT NULL DEFAULT '',
                headline TEXT NOT NULL DEFAULT '',
                event_family TEXT NOT NULL DEFAULT '',
                evidence_snapshot_json TEXT NOT NULL DEFAULT '{}',
                evidence_snapshot_hash TEXT NOT NULL DEFAULT '',
                baseline_price REAL,
                baseline_price_status TEXT NOT NULL DEFAULT 'missing',
                baseline_market_data_generated_at TEXT NOT NULL DEFAULT '',
                predicted_direction TEXT NOT NULL DEFAULT 'neutral',
                expected_return REAL NOT NULL DEFAULT 0,
                confidence REAL NOT NULL DEFAULT 0,
                scorer_name TEXT NOT NULL,
                scorer_version TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'collecting',
                shadow_only INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_m223_predictions_status
                ON m223_live_learning_predictions(status,event_at,symbol);

            CREATE TABLE IF NOT EXISTS m223_live_learning_outcomes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                prediction_id INTEGER NOT NULL,
                horizon_minutes INTEGER NOT NULL,
                due_at TEXT NOT NULL,
                due_at_local TEXT NOT NULL DEFAULT '',
                evaluated_at TEXT,
                evaluated_at_local TEXT NOT NULL DEFAULT '',
                baseline_price REAL,
                reaction_price REAL,
                raw_return REAL,
                actual_direction TEXT NOT NULL DEFAULT 'unknown',
                direction_hit INTEGER,
                absolute_error REAL,
                status TEXT NOT NULL DEFAULT 'pending',
                blocker_reason TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(prediction_id,horizon_minutes),
                FOREIGN KEY(prediction_id) REFERENCES m223_live_learning_predictions(id)
            );
            CREATE INDEX IF NOT EXISTS idx_m223_outcomes_due
                ON m223_live_learning_outcomes(status,due_at);

            CREATE TABLE IF NOT EXISTS m223_live_learning_adjustment_notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                prediction_id INTEGER NOT NULL,
                outcome_id INTEGER NOT NULL,
                note_version TEXT NOT NULL,
                mismatch_type TEXT NOT NULL,
                reason_summary TEXT NOT NULL,
                recommended_adjustment TEXT NOT NULL,
                evidence_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(outcome_id,note_version),
                FOREIGN KEY(prediction_id) REFERENCES m223_live_learning_predictions(id),
                FOREIGN KEY(outcome_id) REFERENCES m223_live_learning_outcomes(id)
            );

            CREATE TABLE IF NOT EXISTS m223_live_threshold_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                candidate_key TEXT NOT NULL UNIQUE,
                generated_at TEXT NOT NULL,
                generated_at_local TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'shadow_candidate',
                source_window TEXT NOT NULL DEFAULT '',
                sample_count INTEGER NOT NULL DEFAULT 0,
                hit_rate REAL,
                avg_absolute_error REAL,
                threshold_json TEXT NOT NULL DEFAULT '{}',
                promoted_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        _ensure_column(conn, "m223_live_learning_predictions", "evidence_packet_version", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "m223_live_learning_predictions", "event_features_json", "TEXT NOT NULL DEFAULT '{}'")
        _ensure_column(conn, "m223_live_learning_predictions", "market_context_json", "TEXT NOT NULL DEFAULT '{}'")
        _ensure_column(conn, "m223_live_learning_predictions", "option_structure_context_json", "TEXT NOT NULL DEFAULT '{}'")
        _ensure_column(conn, "m223_live_learning_predictions", "evidence_quality_json", "TEXT NOT NULL DEFAULT '{}'")
        _ensure_column(conn, "m223_live_learning_predictions", "scorer_input_packet_json", "TEXT NOT NULL DEFAULT '{}'")
        _ensure_column(conn, "m223_live_learning_predictions", "scorer_input_packet_hash", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "m223_live_learning_predictions", "packet_quality_score", "REAL")
        _ensure_column(conn, "m223_live_learning_predictions", "packet_quality_level", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "m223_live_learning_predictions", "asset_mapping_confidence", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "m223_live_learning_predictions", "asset_mapping_reason", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "m223_live_learning_predictions", "event_market_data_lag_seconds", "REAL")
        _ensure_column(conn, "m223_live_learning_predictions", "timestamp_alignment_status", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "m223_live_learning_predictions", "baseline_is_late", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "m223_live_learning_adjustment_notes", "analysis_provider", "TEXT NOT NULL DEFAULT 'deterministic'")
        _ensure_column(conn, "m223_live_learning_adjustment_notes", "analysis_model", "TEXT NOT NULL DEFAULT 'rulebook'")
        _ensure_column(conn, "m223_live_learning_adjustment_notes", "confidence", "REAL")
        _ensure_column(conn, "m223_live_learning_adjustment_notes", "raw_json", "TEXT NOT NULL DEFAULT '{}'")
        _ensure_column(conn, "m223_live_learning_adjustment_notes", "pattern_tags_json", "TEXT NOT NULL DEFAULT '[]'")


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    existing = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _market_price_map(market_data: dict[str, Any]) -> tuple[dict[str, float], str]:
    rows = market_data.get("rows") or []
    prices: dict[str, float] = {}
    for row in rows:
        key = str(row.get("key") or "").upper()
        if not key:
            continue
        price = row.get("price")
        try:
            prices[key] = float(price)
        except (TypeError, ValueError):
            continue
    return prices, str(market_data.get("generated_at") or "")


def _market_row_map(market_data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = market_data.get("rows") or []
    mapped: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = str(row.get("key") or "").upper()
        if key:
            mapped[key] = row
    return mapped


def _compact_market_context(symbol: str, market_data: dict[str, Any], prices: dict[str, float]) -> dict[str, Any]:
    rows = _market_row_map(market_data)
    row = rows.get(symbol.upper()) or {}
    return {
        "symbol": symbol.upper(),
        "desk_generated_at": market_data.get("generated_at"),
        "desk_status": market_data.get("status"),
        "temperature_label": ((market_data.get("temperature") or {}).get("label")),
        "price": prices.get(symbol.upper()),
        "price_source": row.get("price_source"),
        "market_data_type_label": row.get("market_data_type_label"),
        "delayed": bool(row.get("delayed", False)),
        "gap_pct": row.get("gap_pct"),
        "tick_move_pct": row.get("tick_move_pct"),
        "quote_spread_pct": row.get("quote_spread_pct"),
        "alerts": row.get("alerts") or [],
        "quote_age_seconds": row.get("quote_age_seconds"),
        "status": row.get("status"),
    }


def _option_symbol_map(options_market_structure: dict[str, Any]) -> dict[str, dict[str, Any]]:
    mapped: dict[str, dict[str, Any]] = {}
    for row in options_market_structure.get("symbols") or []:
        symbol = str(row.get("symbol") or "").upper()
        if symbol:
            mapped[symbol] = row
    return mapped


def _age_seconds(value: Any) -> float | None:
    parsed = _parse_dt(value)
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


def _option_structure_freshness(options_market_structure: dict[str, Any], supervisor: dict[str, Any]) -> dict[str, Any]:
    session_guard = options_market_structure.get("session_guard") or {}
    guard_status = str(session_guard.get("freshness_status") or "").lower()
    max_age_minutes = session_guard.get("max_snapshot_age_minutes")
    if max_age_minutes is None:
        max_age_minutes = supervisor.get("final_effective_max_snapshot_age_minutes")
    try:
        max_age_seconds = float(max_age_minutes if max_age_minutes is not None else 30.0) * 60.0
    except (TypeError, ValueError):
        max_age_seconds = 1800.0
    structure_age = _age_seconds(options_market_structure.get("generated_at"))
    quote_age = _age_seconds(options_market_structure.get("source_quote_generated_at"))
    supervisor_age = _age_seconds(supervisor.get("completed_at"))
    ages = [value for value in (structure_age, quote_age, supervisor_age) if value is not None]
    observed_age = max(ages) if ages else None
    stale_reasons: list[str] = []
    if not options_market_structure:
        stale_reasons.append("option_structure_missing")
    if observed_age is None:
        stale_reasons.append("option_structure_timestamp_missing")
    elif observed_age > max_age_seconds:
        stale_reasons.append("option_structure_artifact_age_exceeded")
    if guard_status and guard_status not in {"green", "fresh", "ok"}:
        stale_reasons.append(f"session_guard_{guard_status}")
    supervisor_fresh = bool(supervisor.get("final_quote_fresh"))
    supervisor_fresh_trusted = supervisor_fresh and supervisor_age is not None and supervisor_age <= max_age_seconds
    status = "fresh" if not stale_reasons and (guard_status in {"green", "fresh", "ok"} or supervisor_fresh_trusted) else "stale"
    if not options_market_structure:
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


def _compact_option_context(symbol: str, options_market_structure: dict[str, Any], supervisor: dict[str, Any]) -> dict[str, Any]:
    row = _option_symbol_map(options_market_structure).get(symbol.upper()) or {}
    freshness = _option_structure_freshness(options_market_structure, supervisor)
    return {
        "symbol": symbol.upper(),
        "structure_generated_at": options_market_structure.get("generated_at"),
        "source_quote_generated_at": options_market_structure.get("source_quote_generated_at"),
        "status": options_market_structure.get("status"),
        "session_guard": options_market_structure.get("session_guard") or {},
        "freshness": freshness,
        "freshness_status": freshness.get("status"),
        "fresh": freshness.get("fresh"),
        "supervisor_status": supervisor.get("final_status"),
        "supervisor_session": supervisor.get("final_session"),
        "supervisor_quote_quality_status": supervisor.get("final_quote_quality_status"),
        "gamma_regime": row.get("gamma_regime"),
        "total_gamma_exposure": row.get("total_gamma_exposure"),
        "gamma_flip": row.get("gamma_flip"),
        "call_wall": row.get("call_wall"),
        "put_wall": row.get("put_wall"),
        "max_pain": row.get("max_pain"),
        "distance_to_pin_pct": row.get("distance_to_pin_pct"),
        "pin_risk": row.get("pin_risk"),
        "proximity_alerts": row.get("proximity_alerts") or [],
        "expiry_bucket_counts": row.get("expiry_bucket_counts") or {},
        "confidence": row.get("confidence"),
        "confidence_score": row.get("confidence_score"),
        "warnings": row.get("warnings") or [],
        "available": bool(row),
    }


def _evidence_quality_context(evidence_freshness: dict[str, Any], source_online_supervisor: dict[str, Any]) -> dict[str, Any]:
    latest_tick = (source_online_supervisor.get("tick_summaries") or [{}])[-1] if source_online_supervisor.get("tick_summaries") else {}
    return {
        "freshness_overall_status": evidence_freshness.get("overall_status"),
        "scorer_guard_action": (evidence_freshness.get("scorer_guard") or {}).get("action"),
        "source_online_status": source_online_supervisor.get("final_status") or source_online_supervisor.get("last_status"),
        "source_live_validation_status": source_online_supervisor.get("final_live_validation_status") or latest_tick.get("live_validation_status"),
        "source_ingest_health_status": source_online_supervisor.get("final_source_ingest_health_status") or latest_tick.get("source_ingest_health_status"),
        "online_problem_source_keys": source_online_supervisor.get("final_online_problem_source_keys") or latest_tick.get("online_problem_source_keys") or [],
        "action_status_counts": source_online_supervisor.get("final_action_status_counts") or latest_tick.get("action_status_counts") or {},
    }


def _event_features(event: dict[str, Any], headline: str) -> dict[str, Any]:
    text = headline.lower()
    emergency_terms = ("breaking", "urgent", "flash", "halts", "explosion", "missile", "attack", "ceasefire", "tariff", "fed", "cpi")
    return {
        "event_family": event.get("event_family") or "",
        "source_table": event.get("source_table") or "",
        "provider": event.get("provider") or "",
        "symbol_source": event.get("symbol_source") or "",
        "headline_length": len(headline),
        "is_short_headline": len(headline) <= 140,
        "emergency_term_hits": [term for term in emergency_terms if term in text],
        "has_emergency_terms": any(term in text for term in emergency_terms),
    }


def _timestamp_alignment(event_at: str, market_generated_at: str) -> dict[str, Any]:
    event_dt = _parse_dt(event_at)
    market_dt = _parse_dt(market_generated_at)
    if event_dt is None or market_dt is None:
        return {
            "event_market_data_lag_seconds": None,
            "timestamp_alignment_status": "unknown",
            "baseline_is_late": False,
            "timestamp_warning": "missing_event_or_market_timestamp",
        }
    lag = (market_dt - event_dt).total_seconds()
    abs_lag = abs(lag)
    if lag > 300:
        status = "late_baseline"
    elif lag < -60:
        status = "market_snapshot_before_event"
    elif abs_lag <= 60:
        status = "aligned"
    else:
        status = "watch"
    return {
        "event_market_data_lag_seconds": round(lag, 3),
        "timestamp_alignment_status": status,
        "baseline_is_late": bool(lag > 300),
        "timestamp_warning": "" if status == "aligned" else status,
    }


def _packet_quality(
    *,
    baseline_price: float | None,
    baseline_status: str,
    market_context: dict[str, Any],
    option_context: dict[str, Any],
    evidence_quality: dict[str, Any],
    mapping_confidence: str,
    timestamp_context: dict[str, Any],
) -> dict[str, Any]:
    score = 100.0
    missing: list[str] = []
    warnings: list[str] = []
    if baseline_price is None or baseline_status != "available":
        score -= 30
        missing.append("baseline_market_price")
    if not option_context.get("available"):
        score -= 10
        missing.append("option_structure")
    if option_context.get("available") and option_context.get("fresh") is False:
        score -= 15
        if option_context.get("freshness_status") == "expected_deferred_stale":
            warnings.append("expected_deferred_stale_option_structure")
        else:
            warnings.append("stale_option_structure")
    if market_context.get("delayed"):
        score -= 10
        warnings.append("delayed_market_data")
    if evidence_quality.get("freshness_overall_status") not in {"green", "fresh", None}:
        score -= 15
        warnings.append("evidence_not_green")
    if evidence_quality.get("source_online_status") in {"red", "failed"}:
        score -= 15
        warnings.append("source_online_not_green")
    if mapping_confidence == "medium":
        score -= 5
    elif mapping_confidence == "low":
        score -= 15
        warnings.append("low_asset_mapping_confidence")
    if timestamp_context.get("baseline_is_late"):
        score -= 20
        warnings.append("late_baseline")
    elif timestamp_context.get("timestamp_alignment_status") in {"watch", "market_snapshot_before_event", "unknown"}:
        score -= 7
        warnings.append(str(timestamp_context.get("timestamp_alignment_status")))
    score = max(0.0, min(100.0, score))
    if score >= 80:
        level = "complete"
    elif score >= 60:
        level = "usable"
    elif score >= 40:
        level = "weak"
    else:
        level = "poor"
    return {
        "packet_quality_score": round(score, 2),
        "packet_quality_level": level,
        "missing_sections": missing,
        "quality_warnings": [item for item in warnings if item],
    }


def _calibrate_shadow_prediction(
    *,
    direction: str,
    expected_return: float,
    confidence: float,
    timestamp_context: dict[str, Any],
    option_context: dict[str, Any],
    mapping_confidence: str,
    event_family: str,
) -> tuple[str, float, float, dict[str, Any]]:
    adjusted_direction = direction
    adjusted_expected_return = float(expected_return)
    adjusted_confidence = float(confidence)
    reasons: list[str] = []
    if timestamp_context.get("baseline_is_late"):
        adjusted_confidence = max(0.0, adjusted_confidence - 0.15)
        reasons.append("late_baseline_confidence_haircut")
        weak_context = mapping_confidence in {"low", "medium"} or option_context.get("fresh") is False
        if weak_context and adjusted_confidence <= 0.60 and adjusted_direction in {"up", "down"}:
            adjusted_direction = "neutral"
            adjusted_expected_return = 0.0
            reasons.append("late_baseline_flat_misread_neutralization")
    if event_family == "geopolitical" and option_context.get("fresh") is False and adjusted_direction in {"up", "down"}:
        adjusted_confidence = min(adjusted_confidence, 0.55)
        reasons.append("geopolitical_stale_option_context_cap")
        if timestamp_context.get("baseline_is_late"):
            adjusted_direction = "neutral"
            adjusted_expected_return = 0.0
            reasons.append("geopolitical_late_baseline_direction_suppressed")
    calibration = {
        "policy_version": "m223-late-baseline-flat-misread-calibration-v1",
        "applied": bool(reasons),
        "reasons": reasons,
        "original": {
            "predicted_direction": direction,
            "expected_return": expected_return,
            "confidence": confidence,
        },
        "adjusted": {
            "predicted_direction": adjusted_direction,
            "expected_return": adjusted_expected_return,
            "confidence": round(adjusted_confidence, 4),
        },
        "safety": {
            "shadow_only": True,
            "live_threshold_mutated": False,
            "broker_orders_submitted": False,
        },
    }
    return adjusted_direction, adjusted_expected_return, round(adjusted_confidence, 4), calibration


def _build_scorer_input_packet(
    event: dict[str, Any],
    *,
    headline: str,
    symbol: str,
    direction: str,
    expected_return: float,
    confidence: float,
    market_context: dict[str, Any],
    option_context: dict[str, Any],
    evidence_quality: dict[str, Any],
    event_features: dict[str, Any],
    asset_mapping: dict[str, Any],
    timestamp_alignment: dict[str, Any],
    packet_quality: dict[str, Any],
    scorer_calibration: dict[str, Any] | None = None,
    m232_hot_cache_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "packet_version": M224_EVIDENCE_PACKET_VERSION,
        "event": {
            "source_table": event.get("source_table"),
            "source_id": event.get("source_id"),
            "event_at": event.get("event_at"),
            "provider": event.get("provider"),
            "symbol": symbol,
            "headline": headline,
            "event_family": event.get("event_family"),
        },
        "shadow_prediction": {
            "predicted_direction": direction,
            "expected_return": expected_return,
            "confidence": confidence,
        },
        "event_features": event_features,
        "asset_mapping": asset_mapping,
        "scorer_calibration": scorer_calibration or {},
        "timestamp_alignment": timestamp_alignment,
        "packet_quality": packet_quality,
        "market_context": market_context,
        "option_structure_context": option_context,
        "evidence_quality": evidence_quality,
        "m232_hot_cache_context": m232_hot_cache_context or {
            "available": False,
            "usable": False,
            "status": "not_configured",
        },
        "promotion_policy": {
            "mode": "shadow_only",
            "live_scorer_mutation_allowed": False,
            "threshold_auto_promotion_allowed": False,
        },
    }


def _extract_assets(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            return []
    assets: list[str] = []
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str):
                assets.append(item.upper())
            elif isinstance(item, dict):
                for key in ("symbol", "ticker", "asset", "key"):
                    if item.get(key):
                        assets.append(str(item[key]).upper())
                        break
    return [item for item in assets if item]


def _symbol_for_external_signal(row: sqlite3.Row) -> tuple[str, str, str, str]:
    assets = _extract_assets(row["assets_json"])
    for preferred in ("SPY", "QQQ", "IWM", "DIA", "VIX", "GLD", "USO", "XLE", "EWY", "SOXX", "SMH"):
        if preferred in assets:
            return preferred, "high", "asset_map", f"provider asset map includes preferred market proxy {preferred}"
    if assets:
        return assets[0], "medium", "asset_map", f"provider asset map first available asset {assets[0]}"
    region = str(row["region"] or "").lower()
    if "china" in region or "taiwan" in region:
        return "QQQ", "medium", "region_proxy", "Taiwan/China regional risk mapped to QQQ proxy"
    if "korea" in region:
        return "EWY", "medium", "region_proxy", "Korea regional risk mapped to EWY proxy"
    if "middle" in region or "iran" in region or "oil" in region:
        return "USO", "medium", "region_proxy", "Middle East/oil regional risk mapped to USO proxy"
    return "SPY", "low", "default_market_proxy", "No direct asset/region map; default broad-market proxy"


def _news_mapping_confidence(symbol: str, symbol_source: str) -> tuple[str, str]:
    if symbol and symbol_source == "primary_symbol":
        return "high", f"news primary_symbol maps directly to {symbol}"
    if symbol and symbol_source == "default_market_proxy":
        return "low", "missing primary symbol; default market proxy used"
    return "medium", f"symbol mapped by {symbol_source or 'unknown'}"


def _symbol_for_news_article(row: sqlite3.Row) -> tuple[str, str, str, str, dict[str, Any]]:
    primary_symbol = str(row["primary_symbol"] or "").upper()
    if primary_symbol.startswith("BROADTAPE:"):
        mapping = infer_news_assets(
            str(row["headline"] or ""),
            primary_symbol=primary_symbol,
            provider=str(row["provider"] or ""),
            extra_data=str(row["extra_data"] or ""),
        )
        symbol, confidence, source = preferred_trading_symbol(mapping, fallback="SPY")
        return symbol, confidence, source, mapping.reason, mapping.as_dict()
    symbol = primary_symbol or "SPY"
    confidence, reason = _news_mapping_confidence(
        symbol,
        "primary_symbol" if primary_symbol else "default_market_proxy",
    )
    return symbol, confidence, "primary_symbol" if primary_symbol else "default_market_proxy", reason, {}


def _predict_direction(text: str, event_family: str, magnitude_threshold_pct: float) -> tuple[str, float, float]:
    lowered = text.lower()
    bearish_terms = ("war", "attack", "missile", "tariff", "sanction", "inflation", "hot cpi", "lawsuit", "cut guidance", "downgrade", "probe")
    bullish_terms = ("beat", "raises guidance", "approval", "deal", "buyback", "upgrade", "cooling inflation", "ceasefire", "stimulus")
    bear = sum(1 for item in bearish_terms if item in lowered)
    bull = sum(1 for item in bullish_terms if item in lowered)
    if bear > bull:
        return "down", -abs(magnitude_threshold_pct) / 100.0, min(0.85, 0.50 + 0.08 * bear)
    if bull > bear:
        return "up", abs(magnitude_threshold_pct) / 100.0, min(0.85, 0.50 + 0.08 * bull)
    if event_family == "geopolitical":
        return "neutral", 0.0, 0.45
    return "neutral", 0.0, 0.40


def _direction_from_return(value: float | None, threshold: float) -> str:
    if value is None:
        return "unknown"
    if value > threshold:
        return "up"
    if value < -threshold:
        return "down"
    return "neutral"


def _event_hash(source_table: str, source_id: int, event_at: str, headline: str, symbol: str) -> str:
    return _json_hash({"source_table": source_table, "source_id": source_id, "event_at": event_at, "headline": headline, "symbol": symbol})


def _recent_candidate_events(conn: sqlite3.Connection, since: str, limit: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in conn.execute(
        """SELECT id, provider, headline, published_at, received_at, primary_symbol, extra_data
           FROM news_articles
           WHERE COALESCE(published_at, received_at, created_at) >= ?
           ORDER BY COALESCE(published_at, received_at, created_at) DESC
           LIMIT ?""",
        (since, limit),
    ):
        symbol, mapping_confidence, symbol_source, mapping_reason, inferred_mapping = _symbol_for_news_article(row)
        rows.append(
            {
                "source_table": "news_articles",
                "source_id": int(row["id"]),
                "provider": str(row["provider"] or ""),
                "headline": str(row["headline"] or ""),
                "event_at": str(row["published_at"] or row["received_at"] or ""),
                "symbol": symbol,
                "symbol_source": symbol_source,
                "asset_mapping_confidence": mapping_confidence,
                "asset_mapping_reason": mapping_reason,
                "inferred_asset_mapping": inferred_mapping,
                "event_family": "market_news",
            }
        )
    for row in conn.execute(
        """SELECT id, source, title, published_at, region, theme, assets_json
           FROM external_market_signals
           WHERE COALESCE(published_at, created_at) >= ?
           ORDER BY COALESCE(published_at, created_at) DESC
           LIMIT ?""",
        (since, limit),
    ):
        symbol, mapping_confidence, symbol_source, mapping_reason = _symbol_for_external_signal(row)
        rows.append(
            {
                "source_table": "external_market_signals",
                "source_id": int(row["id"]),
                "provider": str(row["source"] or ""),
                "headline": str(row["title"] or ""),
                "event_at": str(row["published_at"] or ""),
                "symbol": symbol,
                "symbol_source": symbol_source,
                "asset_mapping_confidence": mapping_confidence,
                "asset_mapping_reason": mapping_reason,
                "event_family": "geopolitical",
            }
        )
    rows.sort(key=lambda item: item.get("event_at") or "", reverse=True)
    return rows[:limit]


def _insert_predictions(conn: sqlite3.Connection, config: LiveScorerLearningConfig, evidence: dict[str, Any], prices: dict[str, float], market_generated_at: str, now: datetime) -> int:
    since = (now - timedelta(minutes=int(config.lookback_minutes))).isoformat()
    created = 0
    compact_evidence = {key: value for key, value in evidence.items() if not key.endswith("_raw")}
    snapshot_hash = _json_hash(compact_evidence)
    for event in _recent_candidate_events(conn, since, int(config.max_new_events_per_tick)):
        event_at = event.get("event_at") or now.isoformat()
        symbol = str(event.get("symbol") or "SPY").upper()
        headline = str(event.get("headline") or "")
        event_hash = _event_hash(str(event["source_table"]), int(event["source_id"]), event_at, headline, symbol)
        direction, expected_return, confidence = _predict_direction(headline, str(event.get("event_family") or ""), float(config.magnitude_threshold_pct))
        baseline_price = prices.get(symbol)
        baseline_status = "available" if baseline_price is not None else "missing_market_price"
        market_context = _compact_market_context(symbol, evidence.get("market_data_raw") or {}, prices)
        option_context = _compact_option_context(
            symbol,
            evidence.get("options_market_structure_raw") or {},
            evidence.get("options_market_structure_supervisor_raw") or {},
        )
        evidence_quality = _evidence_quality_context(
            evidence.get("evidence_freshness_raw") or {},
            evidence.get("source_online_supervisor_raw") or {},
        )
        event_features = _event_features(event, headline)
        mapping_confidence = str(event.get("asset_mapping_confidence") or "medium")
        mapping_reason = str(event.get("asset_mapping_reason") or "mapping confidence not provided")
        asset_mapping = {
            "symbol": symbol,
            "symbol_source": event.get("symbol_source") or "",
            "confidence": mapping_confidence,
            "reason": mapping_reason,
            "inferred_asset_mapping": event.get("inferred_asset_mapping") or {},
        }
        timestamp_context = _timestamp_alignment(event_at, market_generated_at)
        direction, expected_return, confidence, scorer_calibration = _calibrate_shadow_prediction(
            direction=direction,
            expected_return=expected_return,
            confidence=confidence,
            timestamp_context=timestamp_context,
            option_context=option_context,
            mapping_confidence=mapping_confidence,
            event_family=str(event.get("event_family") or ""),
        )
        quality = _packet_quality(
            baseline_price=baseline_price,
            baseline_status=baseline_status,
            market_context=market_context,
            option_context=option_context,
            evidence_quality=evidence_quality,
            mapping_confidence=mapping_confidence,
            timestamp_context=timestamp_context,
        )
        scorer_packet = _build_scorer_input_packet(
            event,
            headline=headline,
            symbol=symbol,
            direction=direction,
            expected_return=expected_return,
            confidence=confidence,
            market_context=market_context,
            option_context=option_context,
            evidence_quality=evidence_quality,
            event_features=event_features,
            asset_mapping=asset_mapping,
            timestamp_alignment=timestamp_context,
            packet_quality=quality,
            scorer_calibration=scorer_calibration,
            m232_hot_cache_context=evidence.get("m232_hot_cache_context") or {},
        )
        packet_hash = _json_hash(scorer_packet)
        before = conn.total_changes
        conn.execute(
            """INSERT OR IGNORE INTO m223_live_learning_predictions(
                source_table,source_id,event_hash,event_at,event_at_local,provider,symbol,symbol_source,
                headline,event_family,evidence_snapshot_json,evidence_snapshot_hash,
                evidence_packet_version,event_features_json,market_context_json,option_structure_context_json,
                evidence_quality_json,scorer_input_packet_json,scorer_input_packet_hash,
                packet_quality_score,packet_quality_level,asset_mapping_confidence,asset_mapping_reason,
                event_market_data_lag_seconds,timestamp_alignment_status,baseline_is_late,
                baseline_price,baseline_price_status,baseline_market_data_generated_at,
                predicted_direction,expected_return,confidence,scorer_name,scorer_version,
                status,shadow_only,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                event["source_table"],
                event["source_id"],
                event_hash,
                event_at,
                _local_time_text(event_at),
                event.get("provider") or "",
                symbol,
                event.get("symbol_source") or "",
                headline,
                event.get("event_family") or "",
                json.dumps(compact_evidence, ensure_ascii=False, sort_keys=True),
                snapshot_hash,
                M224_EVIDENCE_PACKET_VERSION,
                json.dumps(event_features, ensure_ascii=False, sort_keys=True),
                json.dumps(market_context, ensure_ascii=False, sort_keys=True),
                json.dumps(option_context, ensure_ascii=False, sort_keys=True),
                json.dumps(evidence_quality, ensure_ascii=False, sort_keys=True),
                json.dumps(scorer_packet, ensure_ascii=False, sort_keys=True),
                packet_hash,
                quality["packet_quality_score"],
                quality["packet_quality_level"],
                mapping_confidence,
                mapping_reason,
                timestamp_context.get("event_market_data_lag_seconds"),
                timestamp_context.get("timestamp_alignment_status"),
                1 if timestamp_context.get("baseline_is_late") else 0,
                baseline_price,
                baseline_status,
                market_generated_at,
                direction,
                expected_return,
                confidence,
                config.scorer_name,
                config.scorer_version,
                "collecting",
                1,
                now.isoformat(),
                now.isoformat(),
            ),
        )
        row = conn.execute("SELECT id FROM m223_live_learning_predictions WHERE event_hash=?", (event_hash,)).fetchone()
        if row is not None and conn.total_changes > before:
            created += 1
            for horizon in config.horizons_minutes:
                due_at = (_parse_dt(event_at) or now) + timedelta(minutes=int(horizon))
                conn.execute(
                    """INSERT OR IGNORE INTO m223_live_learning_outcomes(
                        prediction_id,horizon_minutes,due_at,due_at_local,baseline_price,status,created_at,updated_at
                    ) VALUES (?,?,?,?,?,?,?,?)""",
                    (int(row["id"]), int(horizon), due_at.isoformat(), _local_time_text(due_at.isoformat()), baseline_price, "pending", now.isoformat(), now.isoformat()),
                )
    return created


def _regular_us_equity_session_approx(now: datetime) -> bool:
    now_utc = now.astimezone(UTC)
    if now_utc.weekday() >= 5:
        return False
    minutes = now_utc.hour * 60 + now_utc.minute
    # Summer regular session approximation: 09:30-16:15 ET == 13:30-20:15 UTC.
    # This is intentionally a deferral classifier only; the actual price feed is
    # still the source of truth before any outcome is marked evaluated.
    return (13 * 60 + 30) <= minutes <= (20 * 60 + 15)


def _evaluate_outcomes(conn: sqlite3.Connection, config: LiveScorerLearningConfig, prices: dict[str, float], now: datetime) -> tuple[int, int]:
    evaluated = 0
    notes = 0
    threshold = float(config.magnitude_threshold_pct) / 100.0
    regular_session = _regular_us_equity_session_approx(now)
    rows = conn.execute(
        """SELECT o.*, p.symbol, p.predicted_direction, p.expected_return, p.headline, p.event_family
           FROM m223_live_learning_outcomes o
           JOIN m223_live_learning_predictions p ON p.id=o.prediction_id
           WHERE o.status IN ('pending','blocked','market_closed_deferred','awaiting_regular_session') AND o.due_at <= ?
           ORDER BY o.due_at ASC
           LIMIT 200""",
        (now.isoformat(),),
    ).fetchall()
    for row in rows:
        symbol = str(row["symbol"] or "").upper()
        reaction = prices.get(symbol)
        baseline = row["baseline_price"]
        if reaction is None or baseline in (None, 0):
            deferred_status = "market_closed_deferred" if not regular_session else "blocked"
            blocker_reason = "awaiting_regular_session_reaction_price" if not regular_session else "missing_reaction_or_baseline_price"
            conn.execute(
                """UPDATE m223_live_learning_outcomes
                   SET status='blocked', blocker_reason=?, updated_at=?
                   WHERE id=?""",
                (blocker_reason, now.isoformat(), int(row["id"])),
            )
            if deferred_status != "blocked":
                conn.execute(
                    """UPDATE m223_live_learning_outcomes
                       SET status=?, blocker_reason=?, updated_at=?
                       WHERE id=?""",
                    (deferred_status, blocker_reason, now.isoformat(), int(row["id"])),
                )
            continue
        raw_return = (float(reaction) / float(baseline)) - 1.0
        actual = _direction_from_return(raw_return, threshold)
        predicted = str(row["predicted_direction"] or "neutral")
        hit = 1 if predicted == actual else 0
        abs_error = abs(float(row["expected_return"] or 0.0) - raw_return)
        conn.execute(
            """UPDATE m223_live_learning_outcomes
               SET evaluated_at=?, evaluated_at_local=?, reaction_price=?, raw_return=?,
                   actual_direction=?, direction_hit=?, absolute_error=?, status='evaluated',
                   blocker_reason='', updated_at=?
               WHERE id=?""",
            (now.isoformat(), _local_time_text(now.isoformat()), reaction, raw_return, actual, hit, abs_error, now.isoformat(), int(row["id"])),
        )
        evaluated += 1
        if not hit or abs_error >= threshold * 2:
            mismatch = "direction_miss" if not hit else "magnitude_error"
            reason = "Shadow scorer prediction diverged from observed market reaction; keep as training memory and require later threshold validation before promotion."
            if row["event_family"] == "geopolitical":
                reason = "Geopolitical headline reaction differed from the simple shadow expectation; compare against regime, option structure, macro and market-data context before promotion."
            before = conn.total_changes
            conn.execute(
                """INSERT OR IGNORE INTO m223_live_learning_adjustment_notes(
                    prediction_id,outcome_id,note_version,mismatch_type,reason_summary,
                    recommended_adjustment,evidence_json,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    int(row["prediction_id"]),
                    int(row["id"]),
                    "m223-adjustment-note-v1",
                    mismatch,
                    reason,
                    "shadow_only: update candidate threshold memory; do not mutate live scorer until walk-forward validation improves.",
                    json.dumps({"symbol": symbol, "horizon_minutes": int(row["horizon_minutes"]), "headline": row["headline"]}, ensure_ascii=False, sort_keys=True),
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            if conn.total_changes > before:
                notes += 1
    return evaluated, notes


def _backfill_missing_evidence_packets(conn: sqlite3.Connection, evidence: dict[str, Any], prices: dict[str, float], now: datetime, *, limit: int = 200) -> int:
    compact_evidence = {key: value for key, value in evidence.items() if not key.endswith("_raw")}
    snapshot_hash = _json_hash(compact_evidence)
    rows = conn.execute(
        """SELECT id,source_table,source_id,event_at,provider,symbol,symbol_source,headline,event_family,
                  predicted_direction,expected_return,confidence,asset_mapping_confidence,asset_mapping_reason
           FROM m223_live_learning_predictions
           WHERE COALESCE(evidence_packet_version,'')='' OR COALESCE(packet_quality_level,'')=''
           ORDER BY created_at DESC
           LIMIT ?""",
        (int(limit),),
    ).fetchall()
    updated = 0
    for row in rows:
        symbol = str(row["symbol"] or "SPY").upper()
        headline = str(row["headline"] or "")
        event = {
            "source_table": row["source_table"],
            "source_id": row["source_id"],
            "event_at": row["event_at"],
            "provider": row["provider"],
            "symbol_source": row["symbol_source"],
            "event_family": row["event_family"],
        }
        market_context = _compact_market_context(symbol, evidence.get("market_data_raw") or {}, prices)
        option_context = _compact_option_context(
            symbol,
            evidence.get("options_market_structure_raw") or {},
            evidence.get("options_market_structure_supervisor_raw") or {},
        )
        evidence_quality = _evidence_quality_context(
            evidence.get("evidence_freshness_raw") or {},
            evidence.get("source_online_supervisor_raw") or {},
        )
        event_features = _event_features(event, headline)
        mapping_confidence = str(row["asset_mapping_confidence"] or "")
        mapping_reason = str(row["asset_mapping_reason"] or "")
        if not mapping_confidence:
            if str(row["source_table"] or "") == "news_articles":
                mapping_confidence, mapping_reason = _news_mapping_confidence(symbol, str(row["symbol_source"] or ""))
            else:
                mapping_confidence = "medium"
                mapping_reason = "historical prediction backfilled before explicit mapping confidence"
        baseline_price = market_context.get("price")
        baseline_status = "available" if baseline_price is not None else "missing_market_price"
        timestamp_context = _timestamp_alignment(str(row["event_at"] or ""), str(market_context.get("desk_generated_at") or ""))
        quality = _packet_quality(
            baseline_price=baseline_price,
            baseline_status=baseline_status,
            market_context=market_context,
            option_context=option_context,
            evidence_quality=evidence_quality,
            mapping_confidence=mapping_confidence,
            timestamp_context=timestamp_context,
        )
        asset_mapping = {
            "symbol": symbol,
            "symbol_source": row["symbol_source"] or "",
            "confidence": mapping_confidence,
            "reason": mapping_reason,
        }
        _calibrated_direction, _calibrated_expected_return, _calibrated_confidence, scorer_calibration = _calibrate_shadow_prediction(
            direction=str(row["predicted_direction"] or "neutral"),
            expected_return=float(row["expected_return"] or 0.0),
            confidence=float(row["confidence"] or 0.0),
            timestamp_context=timestamp_context,
            option_context=option_context,
            mapping_confidence=mapping_confidence,
            event_family=str(row["event_family"] or ""),
        )
        scorer_packet = _build_scorer_input_packet(
            event,
            headline=headline,
            symbol=symbol,
            direction=str(row["predicted_direction"] or "neutral"),
            expected_return=float(row["expected_return"] or 0.0),
            confidence=float(row["confidence"] or 0.0),
            market_context=market_context,
            option_context=option_context,
            evidence_quality=evidence_quality,
            event_features=event_features,
            asset_mapping=asset_mapping,
            timestamp_alignment=timestamp_context,
            packet_quality=quality,
            scorer_calibration=scorer_calibration,
            m232_hot_cache_context=evidence.get("m232_hot_cache_context") or {},
        )
        conn.execute(
            """UPDATE m223_live_learning_predictions
               SET evidence_snapshot_json=?, evidence_snapshot_hash=?,
                   evidence_packet_version=?, event_features_json=?, market_context_json=?,
                   option_structure_context_json=?, evidence_quality_json=?,
                   scorer_input_packet_json=?, scorer_input_packet_hash=?,
                   packet_quality_score=?, packet_quality_level=?,
                   asset_mapping_confidence=?, asset_mapping_reason=?,
                   event_market_data_lag_seconds=?, timestamp_alignment_status=?,
                   baseline_is_late=?, updated_at=?
               WHERE id=?""",
            (
                json.dumps(compact_evidence, ensure_ascii=False, sort_keys=True),
                snapshot_hash,
                M224_EVIDENCE_PACKET_VERSION,
                json.dumps(event_features, ensure_ascii=False, sort_keys=True),
                json.dumps(market_context, ensure_ascii=False, sort_keys=True),
                json.dumps(option_context, ensure_ascii=False, sort_keys=True),
                json.dumps(evidence_quality, ensure_ascii=False, sort_keys=True),
                json.dumps(scorer_packet, ensure_ascii=False, sort_keys=True),
                _json_hash(scorer_packet),
                quality["packet_quality_score"],
                quality["packet_quality_level"],
                mapping_confidence,
                mapping_reason,
                timestamp_context.get("event_market_data_lag_seconds"),
                timestamp_context.get("timestamp_alignment_status"),
                1 if timestamp_context.get("baseline_is_late") else 0,
                now.isoformat(),
                int(row["id"]),
            ),
        )
        updated += 1
    return updated


def _refresh_candidate(conn: sqlite3.Connection, config: LiveScorerLearningConfig, now: datetime) -> dict[str, Any]:
    rows = conn.execute(
        """SELECT direction_hit, absolute_error
           FROM m223_live_learning_outcomes
           WHERE status='evaluated'
           ORDER BY evaluated_at DESC
           LIMIT 500"""
    ).fetchall()
    sample_count = len(rows)
    hits = [int(row["direction_hit"]) for row in rows if row["direction_hit"] is not None]
    errors = [float(row["absolute_error"]) for row in rows if row["absolute_error"] is not None]
    hit_rate = (sum(hits) / len(hits)) if hits else None
    avg_error = (sum(errors) / len(errors)) if errors else None
    threshold_payload = {
        "promotion_mode": "shadow_only",
        "magnitude_threshold_pct": config.magnitude_threshold_pct,
        "min_samples_before_promotion_review": 100,
        "observed_hit_rate": hit_rate,
        "observed_avg_absolute_error": avg_error,
        "note": "Candidate only. Live scorer thresholds are not mutated by M223.",
    }
    candidate_key = f"{config.scorer_version}:last500:{sample_count}:{round(hit_rate or 0, 4)}:{round(avg_error or 0, 6)}"
    conn.execute(
        """INSERT OR REPLACE INTO m223_live_threshold_candidates(
            candidate_key,generated_at,generated_at_local,status,source_window,sample_count,
            hit_rate,avg_absolute_error,threshold_json,promoted_at,created_at,updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            candidate_key,
            now.isoformat(),
            _local_time_text(now.isoformat()),
            "shadow_candidate",
            "last_500_evaluated_live_learning_outcomes",
            sample_count,
            hit_rate,
            avg_error,
            json.dumps(threshold_payload, ensure_ascii=False, sort_keys=True),
            None,
            now.isoformat(),
            now.isoformat(),
        ),
    )
    return {"candidate_key": candidate_key, "sample_count": sample_count, "hit_rate": hit_rate, "avg_absolute_error": avg_error, "threshold": threshold_payload}


def _counts(conn: sqlite3.Connection) -> dict[str, Any]:
    def one(sql: str, params: tuple[Any, ...] = ()) -> Any:
        row = conn.execute(sql, params).fetchone()
        return row[0] if row else None

    quality_counts = {
        str(row[0] or "unknown"): int(row[1])
        for row in conn.execute(
            "SELECT COALESCE(NULLIF(packet_quality_level,''),'unknown'), COUNT(*) FROM m223_live_learning_predictions GROUP BY COALESCE(NULLIF(packet_quality_level,''),'unknown')"
        ).fetchall()
    }
    mapping_counts = {
        str(row[0] or "unknown"): int(row[1])
        for row in conn.execute(
            "SELECT COALESCE(NULLIF(asset_mapping_confidence,''),'unknown'), COUNT(*) FROM m223_live_learning_predictions GROUP BY COALESCE(NULLIF(asset_mapping_confidence,''),'unknown')"
        ).fetchall()
    }
    timestamp_counts = {
        str(row[0] or "unknown"): int(row[1])
        for row in conn.execute(
            "SELECT COALESCE(NULLIF(timestamp_alignment_status,''),'unknown'), COUNT(*) FROM m223_live_learning_predictions GROUP BY COALESCE(NULLIF(timestamp_alignment_status,''),'unknown')"
        ).fetchall()
    }
    return {
        "prediction_count": one("SELECT COUNT(*) FROM m223_live_learning_predictions") or 0,
        "evidence_packet_v2_prediction_count": one("SELECT COUNT(*) FROM m223_live_learning_predictions WHERE evidence_packet_version=?", (M224_EVIDENCE_PACKET_VERSION,)) or 0,
        "packet_quality_counts": quality_counts,
        "asset_mapping_confidence_counts": mapping_counts,
        "timestamp_alignment_counts": timestamp_counts,
        "late_baseline_count": one("SELECT COUNT(*) FROM m223_live_learning_predictions WHERE baseline_is_late=1") or 0,
        "avg_packet_quality_score": one("SELECT AVG(packet_quality_score) FROM m223_live_learning_predictions WHERE packet_quality_score IS NOT NULL"),
        "pending_outcome_count": one("SELECT COUNT(*) FROM m223_live_learning_outcomes WHERE status='pending'") or 0,
        "due_pending_outcome_count": one("SELECT COUNT(*) FROM m223_live_learning_outcomes WHERE status='pending' AND due_at <= ?", (datetime.now(UTC).isoformat(),)) or 0,
        "market_closed_deferred_outcome_count": one("SELECT COUNT(*) FROM m223_live_learning_outcomes WHERE status IN ('market_closed_deferred','awaiting_regular_session')") or 0,
        "blocked_outcome_count": one("SELECT COUNT(*) FROM m223_live_learning_outcomes WHERE status='blocked'") or 0,
        "evaluated_outcome_count": one("SELECT COUNT(*) FROM m223_live_learning_outcomes WHERE status='evaluated'") or 0,
        "adjustment_note_count": one("SELECT COUNT(*) FROM m223_live_learning_adjustment_notes") or 0,
        "latest_prediction_at": one("SELECT MAX(created_at) FROM m223_live_learning_predictions"),
        "latest_evaluated_at": one("SELECT MAX(evaluated_at) FROM m223_live_learning_outcomes"),
    }


def run_live_scorer_learning(config: LiveScorerLearningConfig) -> dict[str, Any]:
    ensure_live_learning_schema(config.database_path)
    run_id = f"m223_live_learning_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    started_at = _iso_now()
    tick_summaries: list[dict[str, Any]] = []
    max_ticks = int(config.max_ticks)
    continuous = max_ticks <= 0
    tick_index = 0
    while continuous or tick_index < max_ticks:
        tick_index += 1
        now = (config.current_time or datetime.now(UTC)).astimezone(UTC)
        market_data = _read_json(config.market_data_json_path)
        prices, market_generated_at = _market_price_map(market_data)
        evidence_freshness = _read_json(config.evidence_freshness_json_path)
        source_online_supervisor = _read_json(config.source_online_supervisor_json_path)
        options_market_structure = _read_json(config.options_market_structure_json_path)
        options_market_structure_supervisor = _read_json(config.options_market_structure_supervisor_json_path)
        m232_hot_cache_context = compact_m232_hot_cache_for_scorer(config.m232_hot_cache_json_path)
        evidence = {
            "packet_version": M224_EVIDENCE_PACKET_VERSION,
            "market_data": {
                "generated_at": market_generated_at,
                "status": market_data.get("status"),
                "temperature": market_data.get("temperature") or {},
                "summary": market_data.get("summary") or {},
            },
            "evidence_freshness": {
                "overall_status": evidence_freshness.get("overall_status"),
                "scorer_guard": evidence_freshness.get("scorer_guard") or {},
            },
            "source_online_supervisor": {
                "final_status": source_online_supervisor.get("final_status") or source_online_supervisor.get("last_status"),
                "final_live_validation_status": source_online_supervisor.get("final_live_validation_status"),
                "final_source_ingest_health_status": source_online_supervisor.get("final_source_ingest_health_status"),
                "final_action_status_counts": source_online_supervisor.get("final_action_status_counts") or {},
            },
            "options_market_structure": {
                "generated_at": options_market_structure.get("generated_at"),
                "source_quote_generated_at": options_market_structure.get("source_quote_generated_at"),
                "status": options_market_structure.get("status"),
                "summary": options_market_structure.get("summary") or {},
            },
            "options_market_structure_supervisor": {
                "final_status": options_market_structure_supervisor.get("final_status"),
                "final_session": options_market_structure_supervisor.get("final_session"),
                "final_quote_quality_status": options_market_structure_supervisor.get("final_quote_quality_status"),
            },
            "m232_hot_cache_context": m232_hot_cache_context,
            "market_data_raw": market_data,
            "evidence_freshness_raw": evidence_freshness,
            "source_online_supervisor_raw": source_online_supervisor,
            "options_market_structure_raw": options_market_structure,
            "options_market_structure_supervisor_raw": options_market_structure_supervisor,
        }
        with _connect(config.database_path) as conn:
            created = _insert_predictions(conn, config, evidence, prices, market_generated_at, now)
            upgraded_packets = _backfill_missing_evidence_packets(conn, evidence, prices, now)
            evaluated, notes = _evaluate_outcomes(conn, config, prices, now)
            candidate = _refresh_candidate(conn, config, now)
            counts = _counts(conn)
        tick = {
            "tick_index": tick_index,
            "checked_at": now.isoformat(),
            "checked_at_local": _local_time_text(now.isoformat()),
            "new_predictions": created,
            "upgraded_evidence_packets": upgraded_packets,
            "evaluated_outcomes": evaluated,
            "new_adjustment_notes": notes,
            "candidate": candidate,
            "counts": counts,
            "market_price_count": len(prices),
            "market_data_generated_at": market_generated_at,
            "m232_hot_cache_status": m232_hot_cache_context.get("status"),
            "m232_hot_cache_usable": m232_hot_cache_context.get("usable"),
            "m232_hot_cache_age_seconds": m232_hot_cache_context.get("age_seconds"),
            "m232_hot_cache_adaptive_tier": m232_hot_cache_context.get("adaptive_tier"),
            "status": "collecting" if created or counts["pending_outcome_count"] else "idle_waiting_for_new_events",
        }
        tick_summaries.append(tick)
        if config.json_output_path:
            write_live_scorer_learning_json(config.json_output_path, _result(config, run_id, started_at, tick_summaries))
        if continuous or tick_index < max_ticks:
            if config.sleep_between_ticks and config.interval_seconds > 0:
                time.sleep(float(config.interval_seconds))
            else:
                break
    return _result(config, run_id, started_at, tick_summaries)


def _result(config: LiveScorerLearningConfig, run_id: str, started_at: str, ticks: list[dict[str, Any]]) -> dict[str, Any]:
    completed_at = _iso_now()
    latest = ticks[-1] if ticks else {}
    counts = latest.get("counts") or {}
    candidate = latest.get("candidate") or {}
    return {
        "live_scorer_learning_version": M223_LIVE_SCORER_LEARNING_VERSION,
        "run_id": run_id,
        "started_at": started_at,
        "started_at_local": _local_time_text(started_at),
        "completed_at": completed_at,
        "completed_at_local": _local_time_text(completed_at),
        "shadow_only": True,
        "promotion_mode": "shadow_only_until_walk_forward_validation",
        "evidence_packet_version": M224_EVIDENCE_PACKET_VERSION,
        "scorer_name": config.scorer_name,
        "scorer_version": config.scorer_version,
        "horizons_minutes": list(config.horizons_minutes),
        "latest_status": latest.get("status"),
        "status": latest.get("status"),
        "latest_counts": counts,
        "latest_candidate": candidate,
        "prediction_count": counts.get("prediction_count", 0),
        "evidence_packet_v2_prediction_count": counts.get("evidence_packet_v2_prediction_count", 0),
        "packet_quality_counts": counts.get("packet_quality_counts") or {},
        "asset_mapping_confidence_counts": counts.get("asset_mapping_confidence_counts") or {},
        "timestamp_alignment_counts": counts.get("timestamp_alignment_counts") or {},
        "late_baseline_count": counts.get("late_baseline_count", 0),
        "avg_packet_quality_score": counts.get("avg_packet_quality_score"),
        "pending_outcome_count": counts.get("pending_outcome_count", 0),
        "due_pending_outcome_count": counts.get("due_pending_outcome_count", 0),
        "market_closed_deferred_outcome_count": counts.get("market_closed_deferred_outcome_count", 0),
        "blocked_outcome_count": counts.get("blocked_outcome_count", 0),
        "evaluated_outcome_count": counts.get("evaluated_outcome_count", 0),
        "adjustment_note_count": counts.get("adjustment_note_count", 0),
        "latest_prediction_at": counts.get("latest_prediction_at"),
        "latest_evaluated_at": counts.get("latest_evaluated_at"),
        "candidate_sample_count": candidate.get("sample_count", 0),
        "candidate_hit_rate": candidate.get("hit_rate"),
        "candidate_avg_absolute_error": candidate.get("avg_absolute_error"),
        "summary_sidecar_version": "m223-live-scorer-learning-summary-v1",
        "tick_summaries": ticks[-20:],
        "safety": {
            "broker_orders_submitted": False,
            "transmit_enabled": False,
            "live_scorer_mutated": False,
            "threshold_promoted": False,
            "shadow_learning_only": True,
        },
    }


def live_scorer_learning_markdown(result: dict[str, Any]) -> str:
    counts = result.get("latest_counts") or {}
    candidate = result.get("latest_candidate") or {}
    lines = [
        "# M223 Live Scorer Learning Supervisor",
        "",
        f"- Version: `{result.get('live_scorer_learning_version')}`",
        f"- Status: `{result.get('latest_status')}`",
        f"- Run local: `{result.get('started_at_local')}` → `{result.get('completed_at_local')}`",
        f"- Shadow only: `{result.get('shadow_only')}`",
        f"- Evidence packet: `{result.get('evidence_packet_version')}`",
        f"- Scorer: `{result.get('scorer_name')}` / `{result.get('scorer_version')}`",
        f"- Horizons minutes: `{result.get('horizons_minutes')}`",
        f"- Predictions: `{counts.get('prediction_count', 0)}`",
        f"- Evidence packet v2 predictions: `{counts.get('evidence_packet_v2_prediction_count', 0)}`",
        f"- Packet quality counts: `{counts.get('packet_quality_counts') or {}}`",
        f"- Asset mapping confidence counts: `{counts.get('asset_mapping_confidence_counts') or {}}`",
        f"- Timestamp alignment counts: `{counts.get('timestamp_alignment_counts') or {}}`",
        f"- Late baseline count: `{counts.get('late_baseline_count', 0)}`",
        f"- Average packet quality score: `{counts.get('avg_packet_quality_score')}`",
        f"- Pending / blocked / evaluated outcomes: `{counts.get('pending_outcome_count', 0)}` / `{counts.get('blocked_outcome_count', 0)}` / `{counts.get('evaluated_outcome_count', 0)}`",
        f"- Due pending / market-closed deferred outcomes: `{counts.get('due_pending_outcome_count', 0)}` / `{counts.get('market_closed_deferred_outcome_count', 0)}`",
        f"- Adjustment notes: `{counts.get('adjustment_note_count', 0)}`",
        f"- Candidate sample count: `{candidate.get('sample_count', 0)}`",
        f"- Candidate hit rate: `{candidate.get('hit_rate')}`",
        f"- Candidate avg abs error: `{candidate.get('avg_absolute_error')}`",
        "",
        "## Safety",
        "",
        "- M223 records live learning memory and threshold candidates only. It does not mutate the live scorer, promote thresholds, submit broker orders, or enable transmit.",
    ]
    return "\n".join(lines) + "\n"


def write_live_scorer_learning_json(path: Path, result: dict[str, Any]) -> Path:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    summary_keys = [
        "summary_sidecar_version",
        "live_scorer_learning_version",
        "run_id",
        "started_at",
        "started_at_local",
        "completed_at",
        "completed_at_local",
        "status",
        "shadow_only",
        "promotion_mode",
        "evidence_packet_version",
        "scorer_name",
        "scorer_version",
        "prediction_count",
        "evidence_packet_v2_prediction_count",
        "packet_quality_counts",
        "asset_mapping_confidence_counts",
        "timestamp_alignment_counts",
        "late_baseline_count",
        "avg_packet_quality_score",
        "pending_outcome_count",
        "due_pending_outcome_count",
        "market_closed_deferred_outcome_count",
        "blocked_outcome_count",
        "evaluated_outcome_count",
        "adjustment_note_count",
        "latest_prediction_at",
        "latest_evaluated_at",
        "candidate_sample_count",
        "candidate_hit_rate",
        "candidate_avg_absolute_error",
        "safety",
    ]
    sidecar = {key: result.get(key) for key in summary_keys if key in result}
    sidecar_path = resolved.with_name(f"{resolved.stem}_summary{resolved.suffix}")
    sidecar_path.write_text(json.dumps(sidecar, indent=2, ensure_ascii=False), encoding="utf-8")
    return resolved


def write_live_scorer_learning_markdown(path: Path, result: dict[str, Any]) -> Path:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(live_scorer_learning_markdown(result), encoding="utf-8")
    return resolved
