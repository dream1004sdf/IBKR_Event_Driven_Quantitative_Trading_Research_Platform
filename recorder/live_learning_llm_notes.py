from __future__ import annotations

import json
import sqlite3
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .live_scorer_learning import ensure_live_learning_schema


M225_LLM_ADJUSTMENT_NOTE_VERSION = "m225-llm-adjustment-notes-v1"
M225_LLM_USAGE_TRACKING_VERSION = "m225-llm-usage-tracking-v1"
UTC = timezone.utc


class LiveLearningEvidenceWeight(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str = Field(min_length=1, max_length=80)
    weight: float = Field(ge=0.0, le=1.0)


class LiveLearningLLMAdjustment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason_category: str = Field(min_length=1, max_length=96)
    reason_summary: str = Field(min_length=1, max_length=1600)
    recommended_adjustment: str = Field(min_length=1, max_length=1600)
    evidence_source_weights: list[LiveLearningEvidenceWeight] = Field(max_length=10)
    missing_evidence: list[str] = Field(max_length=12)
    pattern_tags: list[str] = Field(max_length=12)
    threshold_candidate: str = Field(min_length=1, max_length=900)
    confidence: float = Field(ge=0.0, le=1.0)


@dataclass(frozen=True)
class LiveLearningLLMNoteConfig:
    database_path: Path
    api_key: str | None
    model: str = "gpt-5.4-mini"
    limit: int = 10
    min_packet_quality_score: float = 40.0
    min_note_confidence: float = 0.55
    min_reason_summary_chars: int = 80
    min_evidence_weight_total: float = 0.75
    max_ticks: int = 1
    interval_seconds: float = 300.0
    sleep_between_ticks: bool = False
    cooldown_seconds: float = 300.0
    max_consecutive_errors: int = 3
    adaptive_backlog_enabled: bool = False
    adaptive_min_limit: int = 3
    adaptive_max_limit: int = 25
    adaptive_low_backlog: int = 25
    adaptive_high_backlog: int = 100
    adaptive_critical_backlog: int = 300
    adaptive_min_interval_seconds: float = 120.0
    adaptive_max_interval_seconds: float = 900.0
    note_version: str = M225_LLM_ADJUSTMENT_NOTE_VERSION
    output_path: Path | None = None
    json_output_path: Path | None = None
    state_json_path: Path | None = None
    focus_feedback_json_path: Path | None = None
    dry_run: bool = False


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path.expanduser().resolve(), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_m225_schema(conn: sqlite3.Connection) -> None:
    def ensure_column(table: str, column: str, definition: str) -> None:
        existing = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    ensure_column("m223_live_learning_adjustment_notes", "note_quality_score", "REAL")
    ensure_column("m223_live_learning_adjustment_notes", "note_quality_level", "TEXT NOT NULL DEFAULT ''")
    ensure_column("m223_live_learning_adjustment_notes", "note_rejected", "INTEGER NOT NULL DEFAULT 0")
    ensure_column("m223_live_learning_adjustment_notes", "rejection_reason", "TEXT NOT NULL DEFAULT ''")
    ensure_column("m223_live_learning_adjustment_notes", "response_id", "TEXT NOT NULL DEFAULT ''")
    ensure_column("m223_live_learning_adjustment_notes", "input_tokens", "INTEGER")
    ensure_column("m223_live_learning_adjustment_notes", "output_tokens", "INTEGER")
    ensure_column("m223_live_learning_adjustment_notes", "duration_ms", "INTEGER")
    ensure_column("m223_live_learning_adjustment_notes", "billing_payer", "TEXT NOT NULL DEFAULT ''")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS m225_llm_note_threshold_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_key TEXT NOT NULL UNIQUE,
            generated_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'shadow_candidate',
            reason_category TEXT NOT NULL DEFAULT '',
            pattern_tags_json TEXT NOT NULL DEFAULT '[]',
            sample_count INTEGER NOT NULL DEFAULT 0,
            avg_confidence REAL,
            avg_note_quality_score REAL,
            threshold_candidate TEXT NOT NULL DEFAULT '',
            source_note_ids_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )


def _json_loads(value: Any, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except Exception:
        return fallback


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


def _read_state(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        payload = json.loads(path.expanduser().resolve().read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _read_focus_feedback(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        payload = json.loads(path.expanduser().resolve().read_text(encoding="utf-8"))
    except Exception:
        return {}
    feedback = payload.get("m225_focus_feedback") if isinstance(payload, dict) else {}
    return feedback if isinstance(feedback, dict) else {}


def _write_state(path: Path | None, payload: dict[str, Any]) -> None:
    if path is None:
        return
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _compact_packet(row: sqlite3.Row) -> dict[str, Any]:
    packet = _json_loads(row["scorer_input_packet_json"], {})
    if not isinstance(packet, dict):
        packet = {}
    event = packet.get("event") or {}
    return {
        "event": {
            "event_at": event.get("event_at") or row["event_at"],
            "provider": event.get("provider") or row["provider"],
            "symbol": row["symbol"],
            "headline": row["headline"],
            "event_family": row["event_family"],
        },
        "prediction": {
            "predicted_direction": row["predicted_direction"],
            "expected_return": row["expected_return"],
            "confidence": row["confidence"],
        },
        "outcome": {
            "horizon_minutes": row["horizon_minutes"],
            "actual_direction": row["actual_direction"],
            "raw_return": row["raw_return"],
            "absolute_error": row["absolute_error"],
            "direction_hit": row["direction_hit"],
            "status": row["outcome_status"],
        },
        "packet_quality": packet.get("packet_quality") or {
            "packet_quality_score": row["packet_quality_score"],
            "packet_quality_level": row["packet_quality_level"],
        },
        "asset_mapping": packet.get("asset_mapping") or {
            "confidence": row["asset_mapping_confidence"],
            "reason": row["asset_mapping_reason"],
        },
        "timestamp_alignment": packet.get("timestamp_alignment") or {
            "event_market_data_lag_seconds": row["event_market_data_lag_seconds"],
            "timestamp_alignment_status": row["timestamp_alignment_status"],
            "baseline_is_late": bool(row["baseline_is_late"]),
        },
        "market_context": packet.get("market_context") or {},
        "option_structure_context": packet.get("option_structure_context") or {},
        "evidence_quality": packet.get("evidence_quality") or {},
    }


def _pending_candidate_count(conn: sqlite3.Connection, config: LiveLearningLLMNoteConfig) -> int:
    row = conn.execute(
        """SELECT COUNT(*)
           FROM m223_live_learning_outcomes o
           JOIN m223_live_learning_predictions p ON p.id=o.prediction_id
           WHERE o.status='evaluated'
             AND (o.direction_hit=0 OR COALESCE(o.absolute_error,0) >= 0.003)
             AND COALESCE(p.packet_quality_score,0) >= ?
             AND NOT EXISTS (
                SELECT 1 FROM m223_live_learning_adjustment_notes n
                WHERE n.outcome_id=o.id AND n.note_version=?
             )""",
        (float(config.min_packet_quality_score), config.note_version),
    ).fetchone()
    return int(row[0] or 0) if row else 0


def _adaptive_schedule(config: LiveLearningLLMNoteConfig, backlog_count: int) -> dict[str, Any]:
    base_limit = max(1, int(config.limit))
    base_interval = max(0.0, float(config.interval_seconds))
    if not config.adaptive_backlog_enabled:
        return {
            "adaptive_backlog_enabled": False,
            "backlog_count": int(backlog_count),
            "effective_limit": base_limit,
            "next_interval_seconds": base_interval,
            "tier": "fixed",
        }

    min_limit = max(1, int(config.adaptive_min_limit))
    max_limit = max(min_limit, int(config.adaptive_max_limit))
    min_interval = max(0.0, float(config.adaptive_min_interval_seconds))
    max_interval = max(min_interval, float(config.adaptive_max_interval_seconds))
    low = max(0, int(config.adaptive_low_backlog))
    high = max(low + 1, int(config.adaptive_high_backlog))
    critical = max(high + 1, int(config.adaptive_critical_backlog))

    if backlog_count >= critical:
        tier = "critical"
        effective_limit = max_limit
        next_interval = min_interval
    elif backlog_count >= high:
        tier = "high"
        effective_limit = min(max_limit, max(base_limit * 2, base_limit + 5))
        next_interval = max(min_interval, min(base_interval, 180.0))
    elif backlog_count >= low:
        tier = "medium"
        effective_limit = min(max_limit, max(base_limit, min_limit))
        next_interval = max(min_interval, min(base_interval, 300.0))
    elif backlog_count > 0:
        tier = "low"
        effective_limit = min(max_limit, max(min_limit, min(base_limit, 5)))
        next_interval = max(min_interval, min(base_interval, 300.0))
    else:
        tier = "empty"
        effective_limit = min_limit
        next_interval = max_interval

    return {
        "adaptive_backlog_enabled": True,
        "backlog_count": int(backlog_count),
        "effective_limit": int(effective_limit),
        "next_interval_seconds": float(next_interval),
        "tier": tier,
        "thresholds": {
            "low": low,
            "high": high,
            "critical": critical,
            "min_limit": min_limit,
            "max_limit": max_limit,
            "min_interval_seconds": min_interval,
            "max_interval_seconds": max_interval,
        },
    }


def _focus_score(row: sqlite3.Row, focus_feedback: dict[str, Any]) -> float:
    patterns = focus_feedback.get("focus_patterns") or []
    if not patterns:
        return 0.0
    text = " ".join(
        [
            str(row["headline"] or ""),
            str(row["event_family"] or ""),
            str(row["asset_mapping_reason"] or ""),
            str(row["timestamp_alignment_status"] or ""),
            str(row["event_features_json"] or ""),
        ]
    ).lower()
    try:
        lag_seconds = float(row["event_market_data_lag_seconds"] or 0.0)
    except (TypeError, ValueError):
        lag_seconds = 0.0
    score = 0.0
    for pattern in patterns:
        signals = [str(item).lower() for item in pattern.get("focus_signals") or []]
        tags = [str(item).lower() for item in pattern.get("focus_tags") or []]
        if any("timestamp_alignment" in signal or "late_baseline" in signal for signal in signals):
            if int(row["baseline_is_late"] or 0) or lag_seconds > 60:
                score += 5.0
        if any("option_structure" in signal for signal in signals):
            if "option" in text or "gamma" in text or "spread" in text:
                score += 2.0
        if any("asset_mapping" in signal for signal in signals):
            if "asset" in text or "mapping" in text or "symbol" in text:
                score += 2.0
        for tag in tags:
            normalized = tag.replace("_", " ")
            if tag in text or normalized in text:
                score += 0.5
    return score


def _candidate_rows(conn: sqlite3.Connection, config: LiveLearningLLMNoteConfig, *, effective_limit: int | None = None) -> list[sqlite3.Row]:
    limit = max(1, int(effective_limit if effective_limit is not None else config.limit))
    focus_feedback = _read_focus_feedback(config.focus_feedback_json_path)
    raw_rows = conn.execute(
        """SELECT
              o.id AS outcome_id,
              o.prediction_id,
              o.horizon_minutes,
              o.actual_direction,
              o.raw_return,
              o.absolute_error,
              o.direction_hit,
              o.status AS outcome_status,
              p.event_at,
              p.provider,
              p.symbol,
              p.headline,
              p.event_family,
              p.event_features_json,
              p.predicted_direction,
              p.expected_return,
              p.confidence,
              p.packet_quality_score,
              p.packet_quality_level,
              p.asset_mapping_confidence,
              p.asset_mapping_reason,
              p.event_market_data_lag_seconds,
              p.timestamp_alignment_status,
              p.baseline_is_late,
              p.scorer_input_packet_json
           FROM m223_live_learning_outcomes o
           JOIN m223_live_learning_predictions p ON p.id=o.prediction_id
           WHERE o.status='evaluated'
             AND (o.direction_hit=0 OR COALESCE(o.absolute_error,0) >= 0.003)
             AND COALESCE(p.packet_quality_score,0) >= ?
             AND NOT EXISTS (
                SELECT 1 FROM m223_live_learning_adjustment_notes n
                WHERE n.outcome_id=o.id AND n.note_version=?
             )
           ORDER BY
              CASE WHEN o.direction_hit=0 THEN 0 ELSE 1 END,
              COALESCE(p.packet_quality_score,0) DESC,
              COALESCE(p.confidence,0) DESC,
              CASE WHEN p.event_family IN ('geopolitical','market_news') THEN 0 ELSE 1 END,
              o.evaluated_at DESC
           LIMIT ?""",
        (float(config.min_packet_quality_score), config.note_version, max(limit * 8 if focus_feedback else limit * 4, limit)),
    ).fetchall()
    if focus_feedback:
        raw_rows = sorted(raw_rows, key=lambda row: (_focus_score(row, focus_feedback), 0 if int(row["direction_hit"] or 0) == 0 else -1, float(row["packet_quality_score"] or 0)), reverse=True)
    selected: list[sqlite3.Row] = []
    seen: set[tuple[str, str]] = set()
    for row in raw_rows:
        headline_key = str(row["headline"] or "")[:96].lower().strip()
        key = (str(row["symbol"] or "").upper(), headline_key)
        if key in seen:
            continue
        seen.add(key)
        selected.append(row)
        if len(selected) >= limit:
            break
    return selected


def _deterministic_fallback(packet: dict[str, Any]) -> LiveLearningLLMAdjustment:
    quality = packet.get("packet_quality") or {}
    asset = packet.get("asset_mapping") or {}
    ts = packet.get("timestamp_alignment") or {}
    reason_parts = []
    if ts.get("baseline_is_late"):
        reason_parts.append("baseline may be late relative to event time")
    if asset.get("confidence") in {"low", "medium"}:
        reason_parts.append(f"asset mapping confidence is {asset.get('confidence')}")
    if (packet.get("option_structure_context") or {}).get("available") is False:
        reason_parts.append("option-structure context is unavailable")
    if not reason_parts:
        reason_parts.append("market reaction diverged from the shadow scorer expectation")
    return LiveLearningLLMAdjustment(
        reason_category="deterministic_fallback_live_learning_gap",
        reason_summary="; ".join(reason_parts),
        recommended_adjustment="Keep this as shadow learning memory; require additional similar samples before changing thresholds.",
        evidence_source_weights=[
            LiveLearningEvidenceWeight(source="market_context", weight=0.35),
            LiveLearningEvidenceWeight(source="timestamp_alignment", weight=0.25),
            LiveLearningEvidenceWeight(source="asset_mapping", weight=0.20),
            LiveLearningEvidenceWeight(source="option_structure", weight=0.20),
        ],
        missing_evidence=list(quality.get("missing_sections") or []),
        pattern_tags=list(quality.get("quality_warnings") or [])[:12],
        threshold_candidate="No live threshold promotion; use for candidate-memory clustering only.",
        confidence=0.45,
    )


def _note_quality(note: LiveLearningLLMAdjustment, packet: dict[str, Any], config: LiveLearningLLMNoteConfig) -> dict[str, Any]:
    score = 100.0
    reasons: list[str] = []
    if note.confidence < float(config.min_note_confidence):
        score -= 25
        reasons.append("low_confidence")
    if len(note.reason_summary.strip()) < int(config.min_reason_summary_chars):
        score -= 20
        reasons.append("reason_summary_too_short")
    weight_total = sum(float(item.weight) for item in note.evidence_source_weights)
    if weight_total < float(config.min_evidence_weight_total):
        score -= 15
        reasons.append("evidence_weight_total_low")
    if len(note.threshold_candidate.strip()) < 20:
        score -= 10
        reasons.append("threshold_candidate_too_vague")
    packet_quality = ((packet.get("packet_quality") or {}).get("packet_quality_score"))
    try:
        packet_quality_float = float(packet_quality)
    except (TypeError, ValueError):
        packet_quality_float = None
    if packet_quality_float is not None and packet_quality_float < 60:
        score -= 10
        reasons.append("weak_packet_quality")
    score = max(0.0, min(100.0, score))
    if score >= 80:
        level = "strong"
    elif score >= 60:
        level = "usable"
    elif score >= 40:
        level = "weak"
    else:
        level = "reject"
    rejected = score < 40 or note.confidence < max(0.25, float(config.min_note_confidence) - 0.20)
    return {
        "note_quality_score": round(score, 2),
        "note_quality_level": level,
        "note_rejected": bool(rejected),
        "rejection_reason": ",".join(reasons),
        "evidence_weight_total": round(weight_total, 4),
    }


class OpenAILiveLearningNoteAnalyzer:
    def __init__(self, *, api_key: str, model: str, client: Any | None = None) -> None:
        if not api_key and client is None:
            raise ValueError("OPENAI_API_KEY is required for OpenAI live-learning note analysis")
        self.model = model
        if client is not None:
            self.client = client
        else:
            from openai import OpenAI

            self.client = OpenAI(api_key=api_key)

    @staticmethod
    def _usage_metadata(response: Any, *, started_at: float, completed_at: float) -> dict[str, Any]:
        usage = getattr(response, "usage", None)
        billing = getattr(response, "billing", None)
        if isinstance(billing, dict):
            billing_payer = billing.get("payer")
        else:
            billing_payer = getattr(billing, "payer", None)
        input_tokens = getattr(usage, "input_tokens", None) if usage is not None else None
        output_tokens = getattr(usage, "output_tokens", None) if usage is not None else None
        total_tokens = getattr(usage, "total_tokens", None) if usage is not None else None
        try:
            input_tokens = int(input_tokens) if input_tokens is not None else None
        except (TypeError, ValueError):
            input_tokens = None
        try:
            output_tokens = int(output_tokens) if output_tokens is not None else None
        except (TypeError, ValueError):
            output_tokens = None
        try:
            total_tokens = int(total_tokens) if total_tokens is not None else None
        except (TypeError, ValueError):
            total_tokens = None
        return {
            "usage_tracking_version": M225_LLM_USAGE_TRACKING_VERSION,
            "response_id": str(getattr(response, "id", "") or ""),
            "billing_payer": str(billing_payer or ""),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "duration_ms": int(max(0.0, completed_at - started_at) * 1000),
        }

    def analyze_with_metadata(self, packet: dict[str, Any]) -> tuple[LiveLearningLLMAdjustment, dict[str, Any]]:
        prompt = (
            "You are analyzing a shadow-only trading scorer prediction error. "
            "Do not recommend live trading. Explain why the prediction and actual market reaction diverged, "
            "using only the provided packet. Focus on market context, option structure, timestamp alignment, "
            "asset mapping confidence, and evidence freshness. Return a concise structured adjustment note."
        )
        started = time.monotonic()
        response = self.client.responses.parse(
            model=self.model,
            input=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": json.dumps(packet, ensure_ascii=False, sort_keys=True)[:24000]},
            ],
            text_format=LiveLearningLLMAdjustment,
        )
        completed = time.monotonic()
        parsed = getattr(response, "output_parsed", None)
        if isinstance(parsed, LiveLearningLLMAdjustment):
            return parsed, self._usage_metadata(response, started_at=started, completed_at=completed)
        raise RuntimeError("OpenAI response did not include parsed LiveLearningLLMAdjustment")

    def analyze(self, packet: dict[str, Any]) -> LiveLearningLLMAdjustment:
        note, _metadata = self.analyze_with_metadata(packet)
        return note


def _store_note(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    note: LiveLearningLLMAdjustment,
    config: LiveLearningLLMNoteConfig,
    *,
    provider: str,
    raw: dict[str, Any],
    quality: dict[str, Any],
    usage_metadata: dict[str, Any] | None = None,
) -> bool:
    now = _iso_now()
    before = conn.total_changes
    note_version = config.note_version if provider == "openai" else f"{config.note_version}-fallback"
    usage_metadata = usage_metadata or {}
    conn.execute(
        """INSERT OR IGNORE INTO m223_live_learning_adjustment_notes(
            prediction_id,outcome_id,note_version,mismatch_type,reason_summary,
            recommended_adjustment,evidence_json,created_at,updated_at,
            analysis_provider,analysis_model,confidence,raw_json,pattern_tags_json
            ,note_quality_score,note_quality_level,note_rejected,rejection_reason,
            response_id,input_tokens,output_tokens,duration_ms,billing_payer
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            int(row["prediction_id"]),
            int(row["outcome_id"]),
            note_version,
            "llm_direction_or_magnitude_gap",
            note.reason_summary,
            note.recommended_adjustment,
            json.dumps(
                {
                    "reason_category": note.reason_category,
                    "evidence_source_weights": [item.model_dump() for item in note.evidence_source_weights],
                    "missing_evidence": note.missing_evidence,
                    "threshold_candidate": note.threshold_candidate,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            now,
            now,
            provider,
            config.model if provider == "openai" else "rulebook",
            note.confidence,
            json.dumps(raw, ensure_ascii=False, sort_keys=True),
            json.dumps(note.pattern_tags, ensure_ascii=False, sort_keys=True),
            quality.get("note_quality_score"),
            quality.get("note_quality_level"),
            1 if quality.get("note_rejected") else 0,
            quality.get("rejection_reason") or "",
            str(usage_metadata.get("response_id") or ""),
            usage_metadata.get("input_tokens"),
            usage_metadata.get("output_tokens"),
            usage_metadata.get("duration_ms"),
            str(usage_metadata.get("billing_payer") or ""),
        ),
    )
    return conn.total_changes > before


def _refresh_threshold_candidates(conn: sqlite3.Connection, note_version: str) -> dict[str, Any]:
    rows = conn.execute(
        """SELECT id, evidence_json, confidence, note_quality_score, pattern_tags_json
           FROM m223_live_learning_adjustment_notes
           WHERE note_version=? AND analysis_provider='openai' AND COALESCE(note_rejected,0)=0
           ORDER BY updated_at DESC
           LIMIT 500""",
        (note_version,),
    ).fetchall()
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        evidence = _json_loads(row["evidence_json"], {})
        tags = _json_loads(row["pattern_tags_json"], [])
        category = str(evidence.get("reason_category") or "unknown")
        key = f"{category}:{','.join(sorted(str(item) for item in tags[:5]))}"
        grouped.setdefault(key, []).append(
            {
                "id": int(row["id"]),
                "category": category,
                "tags": tags,
                "confidence": row["confidence"],
                "quality": row["note_quality_score"],
                "threshold_candidate": str(evidence.get("threshold_candidate") or ""),
            }
        )
    now = _iso_now()
    written = 0
    for key, items in grouped.items():
        if not items:
            continue
        confidences = [float(item["confidence"]) for item in items if item["confidence"] is not None]
        qualities = [float(item["quality"]) for item in items if item["quality"] is not None]
        category = str(items[0]["category"])
        tags = items[0]["tags"] if isinstance(items[0]["tags"], list) else []
        candidate = next((item["threshold_candidate"] for item in items if item["threshold_candidate"]), "")
        conn.execute(
            """INSERT OR REPLACE INTO m225_llm_note_threshold_candidates(
                candidate_key,generated_at,status,reason_category,pattern_tags_json,
                sample_count,avg_confidence,avg_note_quality_score,threshold_candidate,
                source_note_ids_json,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                key,
                now,
                "shadow_candidate",
                category,
                json.dumps(tags, ensure_ascii=False, sort_keys=True),
                len(items),
                (sum(confidences) / len(confidences)) if confidences else None,
                (sum(qualities) / len(qualities)) if qualities else None,
                candidate,
                json.dumps([item["id"] for item in items], ensure_ascii=False),
                now,
                now,
            ),
        )
        written += 1
    return {"candidate_group_count": len(grouped), "candidate_rows_written": written}


def _classify_error(message: str) -> str:
    lowered = message.lower()
    if "rate" in lowered or "429" in lowered:
        return "rate_limit"
    if "timeout" in lowered or "temporar" in lowered or "connection" in lowered:
        return "transient"
    if "invalid schema" in lowered or "badrequest" in lowered or "400" in lowered:
        return "schema_or_request"
    if "api_key" in lowered or "authentication" in lowered or "401" in lowered:
        return "auth"
    return "unknown"


def _stable_category_key(value: Any) -> str:
    text = str(value or "unknown").strip()
    if not text:
        return "unknown"
    return " ".join(text.split()).lower()


def _note_counts(conn: sqlite3.Connection, note_version: str) -> dict[str, Any]:
    rows = conn.execute(
        """SELECT analysis_provider, COUNT(*), AVG(confidence), AVG(note_quality_score)
           FROM m223_live_learning_adjustment_notes
           WHERE note_version IN (?, ?)
           GROUP BY analysis_provider""",
        (note_version, f"{note_version}-fallback"),
    ).fetchall()
    provider_counts = {str(row[0] or "unknown"): int(row[1]) for row in rows}
    avg_confidence = {
        str(row[0] or "unknown"): row[2]
        for row in rows
        if row[2] is not None
    }
    avg_quality = {
        str(row[0] or "unknown"): row[3]
        for row in rows
        if row[3] is not None
    }
    quality_counts = {
        str(row[0] or "unknown"): int(row[1])
        for row in conn.execute(
            """SELECT COALESCE(NULLIF(note_quality_level,''),'unknown'), COUNT(*)
               FROM m223_live_learning_adjustment_notes
               WHERE note_version IN (?, ?)
               GROUP BY COALESCE(NULLIF(note_quality_level,''),'unknown')""",
            (note_version, f"{note_version}-fallback"),
        ).fetchall()
    }
    category_counts: Counter[str] = Counter()
    for row in conn.execute(
        """SELECT evidence_json FROM m223_live_learning_adjustment_notes
           WHERE note_version=? AND analysis_provider='openai'
           ORDER BY updated_at DESC LIMIT 200""",
        (note_version,),
    ).fetchall():
        evidence = _json_loads(row[0], {})
        category_counts[_stable_category_key(evidence.get("reason_category"))] += 1
    pending = conn.execute(
        """SELECT COUNT(*)
           FROM m223_live_learning_outcomes o
           JOIN m223_live_learning_predictions p ON p.id=o.prediction_id
           WHERE o.status='evaluated'
             AND (o.direction_hit=0 OR COALESCE(o.absolute_error,0) >= 0.003)
             AND COALESCE(p.packet_quality_score,0) >= ?
             AND NOT EXISTS (
                SELECT 1 FROM m223_live_learning_adjustment_notes n
                WHERE n.outcome_id=o.id AND n.note_version=?
             )""",
        (0.0, note_version),
    ).fetchone()[0]
    return {
        "provider_counts": provider_counts,
        "avg_confidence_by_provider": avg_confidence,
        "avg_quality_by_provider": avg_quality,
        "note_quality_counts": quality_counts,
        "recent_reason_category_counts": dict(category_counts),
        "pending_candidate_count_unfiltered": int(pending or 0),
    }


def _run_one_tick(config: LiveLearningLLMNoteConfig, *, analyzer: OpenAILiveLearningNoteAnalyzer | None, state: dict[str, Any]) -> dict[str, Any]:
    now_dt = datetime.now(UTC)
    now = now_dt.isoformat()
    next_eligible = _parse_dt(state.get("next_eligible_at"))
    if next_eligible is not None and now_dt < next_eligible:
        return {
            "checked_at": now,
            "status": "skipped_cooldown",
            "next_eligible_at": next_eligible.isoformat(),
            "candidate_count": 0,
            "backlog_count": int((state.get("last_adaptive_schedule") or {}).get("backlog_count") or 0),
            "adaptive_schedule": state.get("last_adaptive_schedule") or {},
            "created_count": 0,
            "error_count": 0,
            "errors": [],
            "note_counts": state.get("last_note_counts") or {},
            "threshold_candidate_summary": state.get("last_threshold_candidate_summary") or {},
        }

    created = 0
    errors: list[str] = []
    skipped = 0
    rejected_count = 0
    quality_counts: Counter[str] = Counter()
    threshold_summary: dict[str, Any] = {}
    note_counts: dict[str, Any] = {}
    adaptive: dict[str, Any] = {}
    with _connect(config.database_path) as conn:
        _ensure_m225_schema(conn)
        backlog_count = _pending_candidate_count(conn, config)
        adaptive = _adaptive_schedule(config, backlog_count)
        rows = _candidate_rows(conn, config, effective_limit=int(adaptive.get("effective_limit") or config.limit))
        if config.dry_run:
            skipped = len(rows)
        else:
            openai_analyzer = analyzer
            if openai_analyzer is None and config.api_key:
                try:
                    openai_analyzer = OpenAILiveLearningNoteAnalyzer(api_key=config.api_key, model=config.model)
                except Exception as exc:
                    errors.append(f"openai_init:{exc.__class__.__name__}:{str(exc)[:300]}")
            for row in rows:
                packet = _compact_packet(row)
                provider = "openai"
                raw: dict[str, Any] = {"packet": packet}
                usage_metadata: dict[str, Any] = {}
                try:
                    if openai_analyzer is None:
                        raise RuntimeError("OpenAI analyzer unavailable")
                    if hasattr(openai_analyzer, "analyze_with_metadata"):
                        note, usage_metadata = openai_analyzer.analyze_with_metadata(packet)
                    else:
                        note = openai_analyzer.analyze(packet)
                    raw["llm_note"] = note.model_dump()
                    if usage_metadata:
                        raw["openai_usage"] = usage_metadata
                except Exception as exc:
                    provider = "deterministic_fallback"
                    error_msg = f"outcome_{row['outcome_id']}:{exc.__class__.__name__}:{str(exc)[:300]}"
                    errors.append(error_msg)
                    note = _deterministic_fallback(packet)
                    raw["fallback_note"] = note.model_dump()
                    usage_metadata = {}
                quality = _note_quality(note, packet, config)
                quality_counts[str(quality.get("note_quality_level") or "unknown")] += 1
                if quality.get("note_rejected"):
                    rejected_count += 1
                raw["note_quality"] = quality
                if _store_note(conn, row, note, config, provider=provider, raw=raw, quality=quality, usage_metadata=usage_metadata):
                    created += 1
            threshold_summary = _refresh_threshold_candidates(conn, config.note_version)
        note_counts = _note_counts(conn, config.note_version)

    error_types = Counter(_classify_error(item) for item in errors)
    previous_errors = int(state.get("consecutive_error_ticks") or 0)
    consecutive_errors = previous_errors + 1 if errors else 0
    next_eligible_at = None
    cooldown_reasons: list[str] = []
    if error_types.get("rate_limit"):
        next_eligible_at = now_dt + timedelta(seconds=float(config.cooldown_seconds))
        cooldown_reasons.append("rate_limit")
    elif consecutive_errors >= int(config.max_consecutive_errors) and errors:
        next_eligible_at = now_dt + timedelta(seconds=float(config.cooldown_seconds))
        cooldown_reasons.append("max_consecutive_errors")
    if next_eligible_at is not None:
        state["next_eligible_at"] = next_eligible_at.isoformat()
    else:
        state.pop("next_eligible_at", None)
    state["consecutive_error_ticks"] = consecutive_errors
    state["last_note_counts"] = note_counts
    state["last_threshold_candidate_summary"] = threshold_summary
    state["last_adaptive_schedule"] = adaptive
    state["updated_at"] = now
    status = "dry_run" if config.dry_run else "ok"
    if errors and created == 0:
        status = "error"
    elif errors:
        status = "partial_with_fallbacks"
    if next_eligible_at is not None:
        status = "cooldown"
    return {
        "checked_at": now,
        "status": status,
        "candidate_count": len(rows) if "rows" in locals() else 0,
        "backlog_count": int(adaptive.get("backlog_count") or 0),
        "adaptive_schedule": adaptive,
        "created_count": created,
        "skipped_count": skipped,
        "rejected_count": rejected_count,
        "note_quality_counts_this_tick": dict(quality_counts),
        "error_count": len(errors),
        "error_type_counts": dict(error_types),
        "errors": errors[:20],
        "cooldown_reasons": cooldown_reasons,
        "next_eligible_at": next_eligible_at.isoformat() if next_eligible_at else None,
        "consecutive_error_ticks": consecutive_errors,
        "note_counts": note_counts,
        "threshold_candidate_summary": threshold_summary,
    }


def run_live_learning_llm_adjustment_notes(config: LiveLearningLLMNoteConfig, *, analyzer: OpenAILiveLearningNoteAnalyzer | None = None) -> dict[str, Any]:
    ensure_live_learning_schema(config.database_path)
    with _connect(config.database_path) as conn:
        _ensure_m225_schema(conn)
    started_at = _iso_now()
    tick_summaries: list[dict[str, Any]] = []
    state = _read_state(config.state_json_path)
    max_ticks = int(config.max_ticks)
    continuous = max_ticks <= 0
    tick_index = 0
    while continuous or tick_index < max_ticks:
        tick_index += 1
        tick = _run_one_tick(config, analyzer=analyzer, state=state)
        tick["tick_index"] = tick_index
        tick_summaries.append(tick)
        _write_state(config.state_json_path, state)
        partial = _m225_result(config, started_at, tick_summaries)
        if config.json_output_path:
            write_live_learning_llm_notes_json(config.json_output_path, partial)
        if config.output_path:
            write_live_learning_llm_notes_markdown(config.output_path, partial)
        if continuous or tick_index < max_ticks:
            adaptive = tick.get("adaptive_schedule") or {}
            sleep_seconds = float(adaptive.get("next_interval_seconds") if adaptive.get("next_interval_seconds") is not None else config.interval_seconds)
            if config.sleep_between_ticks and sleep_seconds > 0:
                time.sleep(sleep_seconds)
            else:
                break
    return _m225_result(config, started_at, tick_summaries)


def _m225_result(config: LiveLearningLLMNoteConfig, started_at: str, tick_summaries: list[dict[str, Any]]) -> dict[str, Any]:
    completed_at = _iso_now()
    latest = tick_summaries[-1] if tick_summaries else {}
    result = {
        "m225_live_learning_llm_notes_version": M225_LLM_ADJUSTMENT_NOTE_VERSION,
        "started_at": started_at,
        "completed_at": completed_at,
        "model": config.model,
        "note_version": config.note_version,
        "status": latest.get("status"),
        "candidate_count": latest.get("candidate_count", 0),
        "backlog_count": latest.get("backlog_count", 0),
        "adaptive_schedule": latest.get("adaptive_schedule") or {},
        "created_count": latest.get("created_count", 0),
        "skipped_count": latest.get("skipped_count", 0),
        "rejected_count": latest.get("rejected_count", 0),
        "error_count": latest.get("error_count", 0),
        "error_type_counts": latest.get("error_type_counts") or {},
        "errors": latest.get("errors") or [],
        "cooldown_reasons": latest.get("cooldown_reasons") or [],
        "next_eligible_at": latest.get("next_eligible_at"),
        "consecutive_error_ticks": latest.get("consecutive_error_ticks", 0),
        "note_quality_counts_this_tick": latest.get("note_quality_counts_this_tick") or {},
        "note_counts": latest.get("note_counts") or {},
        "threshold_candidate_summary": latest.get("threshold_candidate_summary") or {},
        "tick_summaries": tick_summaries[-20:],
        "shadow_only": True,
        "safety": {
            "broker_orders_submitted": False,
            "transmit_enabled": False,
            "live_scorer_mutated": False,
            "threshold_promoted": False,
        },
    }
    return result


def live_learning_llm_notes_markdown(result: dict[str, Any]) -> str:
    note_counts = result.get("note_counts") or {}
    threshold_summary = result.get("threshold_candidate_summary") or {}
    adaptive = result.get("adaptive_schedule") or {}
    return "\n".join(
        [
            "# M225 Live Learning LLM Adjustment Notes",
            "",
            f"- Version: `{result.get('m225_live_learning_llm_notes_version')}`",
            f"- Model: `{result.get('model')}`",
            f"- Status: `{result.get('status')}`",
            f"- Backlog: `{result.get('backlog_count')}`",
            f"- Adaptive tier / limit / next interval: `{adaptive.get('tier') or '-'}` / `{adaptive.get('effective_limit')}` / `{adaptive.get('next_interval_seconds')}`",
            f"- Candidates: `{result.get('candidate_count')}`",
            f"- Created: `{result.get('created_count')}`",
            f"- Rejected: `{result.get('rejected_count')}`",
            f"- Errors: `{result.get('error_count')}`",
            f"- Error types: `{result.get('error_type_counts') or {}}`",
            f"- Cooldown reasons: `{result.get('cooldown_reasons') or []}`",
            f"- Next eligible: `{result.get('next_eligible_at') or '-'}`",
            f"- Quality this tick: `{result.get('note_quality_counts_this_tick') or {}}`",
            f"- Provider counts: `{note_counts.get('provider_counts') or {}}`",
            f"- Note quality counts: `{note_counts.get('note_quality_counts') or {}}`",
            f"- Recent reason categories: `{note_counts.get('recent_reason_category_counts') or {}}`",
            f"- Pending candidate count: `{note_counts.get('pending_candidate_count_unfiltered')}`",
            f"- Threshold candidate groups: `{threshold_summary.get('candidate_group_count')}`",
            f"- Threshold rows written: `{threshold_summary.get('candidate_rows_written')}`",
            f"- Shadow only: `{result.get('shadow_only')}`",
            "",
            "## Safety",
            "",
            "- This command writes explanatory learning notes only. It does not mutate the live scorer, promote thresholds, submit broker orders, or enable transmit.",
        ]
    ) + "\n"


def write_live_learning_llm_notes_json(path: Path, result: dict[str, Any]) -> Path:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return resolved


def write_live_learning_llm_notes_markdown(path: Path, result: dict[str, Any]) -> Path:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(live_learning_llm_notes_markdown(result), encoding="utf-8")
    return resolved
