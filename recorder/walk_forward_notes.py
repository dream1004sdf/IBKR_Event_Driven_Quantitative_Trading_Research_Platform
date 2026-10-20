from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .ai_provider import PermanentAnalysisError, TransientAnalysisError
from .evidence_packet import build_overlay_evidence_packet
from .walk_forward_training import (
    _direction_hit,
    _gap,
    _parse_dt,
    _pattern_keys,
    _packet_tracks,
    _relation_to_prediction,
)


WALK_FORWARD_NOTE_VERSION = "walk-forward-adjustment-notes-v1"


class OpenAIWalkForwardCauseAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason_category: str = Field(min_length=1, max_length=96)
    cause_summary: str = Field(min_length=1, max_length=1500)
    recommended_memory_update: str = Field(min_length=1, max_length=1500)
    evidence_weights: list["OpenAIWalkForwardEvidenceWeight"] = Field(max_length=10)
    pattern_tags: list[str] = Field(max_length=12)
    reflex_candidate: bool
    confidence: float = Field(ge=0.0, le=1.0)


class OpenAIWalkForwardEvidenceWeight(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str = Field(min_length=1, max_length=80)
    weight: float = Field(ge=0.0, le=1.0)


@dataclass(frozen=True)
class WalkForwardAdjustmentNote:
    index: int
    score_id: int | None
    outcome_id: int | None
    replay_corpus_id: int | None
    symbol: str
    horizon: str
    replay_timestamp: str
    scorer_name: str
    model_name: str
    scorer_version: str
    predicted_direction: str
    actual_direction: str
    expected_return: float | None
    actual_excess_return: float | None
    absolute_excess_error: float | None
    direction_hit: int | None
    gap_type: str
    tracks: tuple[str, ...]
    pattern_keys: tuple[str, ...]
    weak_patterns_before: tuple[dict[str, Any], ...]
    reason_category: str
    cause_summary: str
    recommended_memory_update: str
    reflex_candidate: bool
    evidence_snapshot: dict[str, Any]
    analysis_provider: str = "deterministic"
    analysis_model: str = "rulebook"
    note_version: str = WALK_FORWARD_NOTE_VERSION
    raw: dict[str, Any] | None = None


def _safe_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed != parsed:
        return None
    return parsed


def _hit_rate(stats: dict[str, Any]) -> float | None:
    seen = int(stats.get("seen") or 0)
    if seen <= 0:
        return None
    return int(stats.get("hits") or 0) / seen


def _compact_signal(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": item.get("source"),
        "published_at": item.get("published_at"),
        "title": str(item.get("title") or "")[:220],
        "region": item.get("region"),
        "theme": item.get("theme"),
        "sentiment_label": item.get("sentiment_label"),
        "assets": item.get("assets") or [],
        "emergency": bool(item.get("emergency")),
    }


def _evidence_snapshot(packet: dict[str, Any]) -> dict[str, Any]:
    overlay = packet.get("overlay") or {}
    standing = packet.get("standing_regime_overlay") or {}
    economic = packet.get("economic_indicator_desk") or {}
    alignment = packet.get("tri_desk_alignment") or {}
    top_signals = [
        _compact_signal(item)
        for item in (overlay.get("top_signals") or [])
        if isinstance(item, dict)
    ][:5]
    active_regimes = []
    for item in (standing.get("active_regimes") or [])[:6]:
        if not isinstance(item, dict):
            continue
        active_regimes.append(
            {
                "family": item.get("family"),
                "status": item.get("status"),
                "directional_bias": item.get("directional_bias"),
                "relation_to_prediction": item.get("relation_to_prediction"),
                "latest_evidence_at": item.get("latest_evidence_at"),
                "summary": str(item.get("summary") or item.get("reason") or "")[:220],
            }
        )
    return {
        "overlay": {
            "overlay_state": overlay.get("overlay_state"),
            "overlay_action": overlay.get("overlay_action"),
            "overlay_score": overlay.get("overlay_score"),
            "urgency": overlay.get("urgency"),
            "emergency": bool(overlay.get("emergency")),
            "matched_signal_count": overlay.get("matched_signal_count"),
            "top_signals": top_signals,
        },
        "standing_regime_overlay": {
            "matched_regime_count": standing.get("matched_regime_count"),
            "active_regimes": active_regimes,
        },
        "economic_indicator_desk": {
            "risk_bias": economic.get("risk_bias"),
            "confidence": economic.get("confidence"),
            "as_of": economic.get("as_of"),
            "summary": str(economic.get("summary") or "")[:300],
        },
        "tri_desk_alignment": {
            "confirmation_score": alignment.get("confirmation_score"),
            "conflict_score": alignment.get("conflict_score"),
            "market_vote": alignment.get("market_vote"),
            "geopolitical_vote": alignment.get("geopolitical_vote"),
            "economic_vote": alignment.get("economic_vote"),
        },
    }


def _has_emergency(snapshot: dict[str, Any]) -> bool:
    overlay = snapshot.get("overlay") or {}
    return bool(overlay.get("emergency")) or any(
        bool(item.get("emergency")) for item in (overlay.get("top_signals") or []) if isinstance(item, dict)
    )


def _standing_conflicts(row: dict[str, Any], snapshot: dict[str, Any]) -> list[str]:
    predicted = str(row.get("predicted_direction") or "").lower()
    conflicts: list[str] = []
    for regime in ((snapshot.get("standing_regime_overlay") or {}).get("active_regimes") or []):
        if not isinstance(regime, dict):
            continue
        relation = str(regime.get("relation_to_prediction") or "")
        if not relation:
            relation = _relation_to_prediction(predicted, str(regime.get("directional_bias") or ""))
        if relation == "conflict":
            conflicts.append(str(regime.get("family") or "unknown_regime"))
    return conflicts


def _reason_category(row: dict[str, Any], snapshot: dict[str, Any], weak_patterns: list[dict[str, Any]]) -> str:
    overlay = snapshot.get("overlay") or {}
    economic = snapshot.get("economic_indicator_desk") or {}
    conflicts = _standing_conflicts(row, snapshot)
    if _has_emergency(snapshot):
        return "emergency_reflex_gap"
    if conflicts:
        return "standing_regime_threshold_gap"
    if str(overlay.get("overlay_state") or "normal") != "normal" or int(overlay.get("matched_signal_count") or 0) > 0:
        return "short_geopolitical_impact_gap"
    if str(economic.get("risk_bias") or "") in {"macro_risk_off", "macro_mixed"}:
        return "economic_macro_offset_gap"
    if weak_patterns:
        return "repeated_market_news_pattern_gap"
    return "market_news_primary_gap"


def _deterministic_summary(
    row: dict[str, Any],
    snapshot: dict[str, Any],
    gap: dict[str, Any],
    weak_patterns: list[dict[str, Any]],
    *,
    min_pattern_observations: int,
    weak_hit_rate: float,
) -> tuple[str, str, bool]:
    category = _reason_category(row, snapshot, weak_patterns)
    symbol = str(row.get("symbol") or "").upper()
    pred = str(row.get("predicted_direction") or "unknown")
    actual = str(row.get("actual_direction") or "unknown")
    abs_error = gap.get("absolute_excess_error")
    overlay = snapshot.get("overlay") or {}
    economic = snapshot.get("economic_indicator_desk") or {}
    alignment = snapshot.get("tri_desk_alignment") or {}
    conflicts = _standing_conflicts(row, snapshot)
    top_signal_titles = [
        str(item.get("title") or "")
        for item in (overlay.get("top_signals") or [])
        if isinstance(item, dict) and str(item.get("title") or "").strip()
    ][:2]

    parts = [
        f"{symbol} {row.get('horizon') or ''} prediction was {pred}, realized {actual}.",
        f"gap_type={gap.get('gap_type')}",
    ]
    if abs_error is not None:
        parts.append(f"absolute_excess_error={float(abs_error):.2%}")
    if overlay.get("overlay_state"):
        parts.append(
            f"overlay={overlay.get('overlay_state')}/{overlay.get('overlay_action')} "
            f"signals={overlay.get('matched_signal_count')}"
        )
    if conflicts:
        parts.append(f"standing regime conflict={','.join(conflicts[:4])}")
    if economic.get("risk_bias"):
        parts.append(f"economic_bias={economic.get('risk_bias')}")
    if alignment:
        parts.append(
            f"tri_desk confirm={alignment.get('confirmation_score')} conflict={alignment.get('conflict_score')}"
        )
    if top_signal_titles:
        parts.append("top_external_signal=" + " / ".join(title[:120] for title in top_signal_titles))
    if weak_patterns:
        parts.append(
            "prior weak memory="
            + "; ".join(
                f"{item.get('key')} seen={item.get('seen')} hit_rate={float(item.get('hit_rate') or 0.0):.1%}"
                for item in weak_patterns[:3]
            )
        )
    cause_summary = " ".join(parts)[:1500]

    if category == "emergency_reflex_gap":
        action = (
            "Store this as an emergency-news reflex candidate. If the same urgency/state/prediction pattern "
            f"reaches at least {min_pattern_observations} prior observations below {weak_hit_rate:.0%} hit rate, "
            "route future matching headlines to observe-only/second-pass before spending an LLM call."
        )
    elif category == "standing_regime_threshold_gap":
        action = (
            "Treat the active standing-regime conflict as a threshold modifier. Future related headlines should "
            "distinguish intensification, easing, and resolution instead of using the normal one-shot news threshold."
        )
    elif category == "short_geopolitical_impact_gap":
        action = (
            "Train the short-geopolitical impact map with this miss: require stronger mapped-asset or market-proxy "
            "confirmation before converting region/theme news into a directional market-news forecast."
        )
    elif category == "economic_macro_offset_gap":
        action = (
            "Store as a macro/economic offset case. Future market-news forecasts should compare the headline signal "
            "against current macro risk bias before assigning direction or confidence."
        )
    elif category == "repeated_market_news_pattern_gap":
        action = (
            "The prior pattern memory was already weak before this row. Keep the pattern gated until later rows show "
            "a sustained hit-rate recovery."
        )
    else:
        action = (
            "Keep as a primary market-news gap note. Do not promote a new rule until similar symbol/event patterns "
            "repeat with enough prior observations."
        )
    reflex_candidate = category == "emergency_reflex_gap" and bool(weak_patterns)
    return category, cause_summary, action[:1500], reflex_candidate


def _extract_openai_parsed(response: Any) -> OpenAIWalkForwardCauseAnalysis:
    direct = getattr(response, "output_parsed", None)
    if isinstance(direct, OpenAIWalkForwardCauseAnalysis):
        return direct
    refusal_messages: list[str] = []
    for output in getattr(response, "output", []) or []:
        for content in getattr(output, "content", []) or []:
            parsed = getattr(content, "parsed", None)
            if isinstance(parsed, OpenAIWalkForwardCauseAnalysis):
                return parsed
            if getattr(content, "type", None) == "refusal":
                refusal_messages.append(str(getattr(content, "refusal", "refused")))
    if refusal_messages:
        raise PermanentAnalysisError("Model refused walk-forward note request: " + "; ".join(refusal_messages))
    raise TransientAnalysisError("Structured response did not contain an OpenAIWalkForwardCauseAnalysis")


def _map_openai_exception(exc: Exception) -> Exception:
    name = exc.__class__.__name__
    message = str(exc)[:1500]
    status = getattr(exc, "status_code", None)
    if name in {"APIConnectionError", "APITimeoutError", "RateLimitError", "InternalServerError"} or (
        isinstance(status, int) and status >= 500
    ):
        return TransientAnalysisError(f"OpenAI transient walk-forward note error ({name}): {message}")
    if name == "AuthenticationError":
        return PermanentAnalysisError("OpenAI walk-forward note authentication failed. Check OPENAI_API_KEY.")
    if name in {"PermissionDeniedError", "NotFoundError", "UnprocessableEntityError", "BadRequestError"} or (
        isinstance(status, int) and 400 <= status < 500
    ):
        return PermanentAnalysisError(f"OpenAI walk-forward note request/configuration error ({name}): {message}")
    return TransientAnalysisError(f"OpenAI unexpected walk-forward note error ({name}): {message}")


class OpenAIWalkForwardNoteAnalyzer:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        timeout_seconds: float = 90.0,
        reasoning_effort: str = "low",
        max_input_chars: int = 16000,
        client: Any | None = None,
    ) -> None:
        if not api_key and client is None:
            raise PermanentAnalysisError("OPENAI_API_KEY is missing. Add it to .env before running OpenAI walk-forward notes.")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.reasoning_effort = reasoning_effort
        self.max_input_chars = max_input_chars
        if client is not None:
            self.client = client
        else:
            try:
                from openai import OpenAI
            except ImportError as exc:
                raise PermanentAnalysisError("The openai package is not installed. Run pip install -r requirements.txt.") from exc
            self.client = OpenAI(api_key=api_key, timeout=timeout_seconds)

    def analyze(self, note: WalkForwardAdjustmentNote) -> WalkForwardAdjustmentNote:
        payload = {
            "task": (
                "Post-outcome walk-forward education note. Explain why a stored forecast differed from the realized "
                "market outcome using only point-in-time evidence attached to this row and prior-memory flags. "
                "Return diagnostic guidance for future scorer design; do not give trading instructions."
            ),
            "note_seed": {
                "symbol": note.symbol,
                "horizon": note.horizon,
                "replay_timestamp": note.replay_timestamp,
                "predicted_direction": note.predicted_direction,
                "actual_direction": note.actual_direction,
                "expected_return": note.expected_return,
                "actual_excess_return": note.actual_excess_return,
                "absolute_excess_error": note.absolute_excess_error,
                "gap_type": note.gap_type,
                "tracks": list(note.tracks),
                "pattern_keys": list(note.pattern_keys),
                "weak_patterns_before": list(note.weak_patterns_before),
                "deterministic_reason_category": note.reason_category,
                "deterministic_cause_summary": note.cause_summary,
                "deterministic_recommended_memory_update": note.recommended_memory_update,
            },
            "evidence_snapshot": note.evidence_snapshot,
            "allowed_categories": [
                "market_news_primary_gap",
                "emergency_reflex_gap",
                "short_geopolitical_impact_gap",
                "standing_regime_threshold_gap",
                "economic_macro_offset_gap",
                "repeated_market_news_pattern_gap",
                "multi_source_conflict_gap",
                "insufficient_evidence_gap",
            ],
        }
        text = json.dumps(payload, ensure_ascii=False, default=str)[: self.max_input_chars]
        try:
            response = self.client.responses.parse(
                model=self.model,
                input=[
                    {
                        "role": "system",
                        "content": (
                            "You are a financial replay education analyst. Diagnose forecast gaps from stored "
                            "walk-forward simulations using only supplied evidence and prior-memory state."
                        ),
                    },
                    {"role": "user", "content": text},
                ],
                text_format=OpenAIWalkForwardCauseAnalysis,
                reasoning={"effort": self.reasoning_effort},
                store=False,
            )
            parsed = _extract_openai_parsed(response)
        except (PermanentAnalysisError, TransientAnalysisError):
            raise
        except (ValidationError, Exception) as exc:
            mapped = _map_openai_exception(exc)
            raise mapped from exc
        return WalkForwardAdjustmentNote(
            **{
                **note.__dict__,
                "reason_category": parsed.reason_category,
                "cause_summary": parsed.cause_summary,
                "recommended_memory_update": parsed.recommended_memory_update,
                "reflex_candidate": bool(parsed.reflex_candidate),
                "analysis_provider": "openai",
                "analysis_model": self.model,
                "raw": {
                    "source": "openai_walk_forward_cause_analysis",
                    "evidence_weights": [item.model_dump() for item in parsed.evidence_weights],
                    "pattern_tags": parsed.pattern_tags,
                    "confidence": parsed.confidence,
                    "deterministic_seed": note.raw,
                },
            }
        )


def build_walk_forward_adjustment_notes(
    rows: list[dict[str, Any]],
    external_signals: list[dict[str, Any]],
    db: Any,
    *,
    limit: int = 50,
    min_pattern_observations: int = 8,
    weak_hit_rate: float = 0.45,
    min_abs_error: float | None = None,
    include_hits: bool = False,
    analyzer: OpenAIWalkForwardNoteAnalyzer | None = None,
    packet_builder: Callable[[dict[str, Any], list[dict[str, Any]], Any], dict[str, Any]] = build_overlay_evidence_packet,
) -> dict[str, Any]:
    ordered = sorted(rows, key=lambda row: (_parse_dt(row.get("replay_timestamp")), int(row.get("outcome_id") or 0)))
    memory: dict[str, dict[str, Any]] = {}
    candidates: list[tuple[float, int, dict[str, Any], dict[str, Any], list[str], list[dict[str, Any]], dict[str, Any]]] = []
    errors: list[dict[str, Any]] = []

    for index, row in enumerate(ordered, start=1):
        packet = packet_builder(row, external_signals, db)
        keys = _pattern_keys(row, packet)
        tracks = _packet_tracks(row, packet)
        weak_patterns: list[dict[str, Any]] = []
        for key in keys:
            stats = memory.get(key)
            if not stats:
                continue
            seen = int(stats.get("seen") or 0)
            hit_rate = _hit_rate(stats)
            if seen >= int(min_pattern_observations) and hit_rate is not None and hit_rate < float(weak_hit_rate):
                weak_patterns.append({"key": key, "seen": seen, "hit_rate": hit_rate})
        gap = _gap(row)
        direction_hit = gap["direction_hit"]
        abs_error = gap.get("absolute_excess_error")
        should_note = gap["gap_type"] != "small_gap_or_hit" or bool(weak_patterns)
        if min_abs_error is not None:
            should_note = should_note and abs_error is not None and float(abs_error) >= float(min_abs_error)
        if not include_hits and direction_hit == 1 and gap["gap_type"] == "small_gap_or_hit":
            should_note = False
        if should_note:
            severity = float(abs_error or 0.0) + (0.05 if direction_hit == 0 else 0.0) + (0.025 if weak_patterns else 0.0)
            candidates.append((severity, index, row, packet, tracks, weak_patterns, gap))
        if direction_hit is not None:
            for key in keys:
                stats = memory.setdefault(key, {"seen": 0, "hits": 0, "gap_types": Counter()})
                stats["seen"] = int(stats.get("seen") or 0) + 1
                stats["hits"] = int(stats.get("hits") or 0) + int(direction_hit)
                if isinstance(stats.get("gap_types"), Counter):
                    stats["gap_types"][str(gap["gap_type"])] += 1

    selected = sorted(candidates, key=lambda item: (-item[0], item[1]))[: max(0, int(limit))]
    notes: list[WalkForwardAdjustmentNote] = []
    for _severity, index, row, packet, tracks, weak_patterns, gap in selected:
        snapshot = _evidence_snapshot(packet)
        category, summary, action, reflex_candidate = _deterministic_summary(
            row,
            snapshot,
            gap,
            weak_patterns,
            min_pattern_observations=min_pattern_observations,
            weak_hit_rate=weak_hit_rate,
        )
        note = WalkForwardAdjustmentNote(
            index=index,
            score_id=int(row["id"]) if row.get("id") is not None else None,
            outcome_id=int(row["outcome_id"]) if row.get("outcome_id") is not None else None,
            replay_corpus_id=int(row["replay_corpus_id"]) if row.get("replay_corpus_id") is not None else None,
            symbol=str(row.get("symbol") or "").upper(),
            horizon=str(row.get("horizon") or "").upper(),
            replay_timestamp=str(row.get("replay_timestamp") or ""),
            scorer_name=str(row.get("scorer_name") or ""),
            model_name=str(row.get("model_name") or ""),
            scorer_version=str(row.get("scorer_version") or ""),
            predicted_direction=str(row.get("predicted_direction") or ""),
            actual_direction=str(row.get("actual_direction") or ""),
            expected_return=_safe_float(row.get("expected_return")),
            actual_excess_return=_safe_float(row.get("actual_excess_return")),
            absolute_excess_error=_safe_float(gap.get("absolute_excess_error")),
            direction_hit=_direction_hit(row),
            gap_type=str(gap.get("gap_type") or ""),
            tracks=tuple(tracks),
            pattern_keys=tuple(_pattern_keys(row, packet)),
            weak_patterns_before=tuple(weak_patterns[:5]),
            reason_category=category,
            cause_summary=summary,
            recommended_memory_update=action,
            reflex_candidate=reflex_candidate,
            evidence_snapshot=snapshot,
            raw={"source": "deterministic_walk_forward_adjustment_note"},
        )
        if analyzer is not None:
            try:
                note = analyzer.analyze(note)
            except (PermanentAnalysisError, TransientAnalysisError) as exc:
                errors.append({"score_id": note.score_id, "error": f"{exc.__class__.__name__}: {str(exc)[:500]}"})
                continue
        notes.append(note)

    categories = Counter(note.reason_category for note in notes)
    tracks = Counter(track for note in notes for track in note.tracks)
    return {
        "version": WALK_FORWARD_NOTE_VERSION,
        "rows_scanned": len(ordered),
        "candidate_rows": len(candidates),
        "notes": [note_to_dict(note) for note in notes],
        "summary": {
            "notes": len(notes),
            "categories": dict(categories),
            "tracks": dict(tracks),
            "reflex_candidates": sum(1 for note in notes if note.reflex_candidate),
            "analysis_provider": "openai" if analyzer is not None else "deterministic",
            "errors": errors,
        },
        "safety": {
            "leakage_policy": "Rows are processed chronologically; prior-memory weak patterns are computed before the current row outcome updates memory.",
            "mutation_policy": "Report-only; no scorer rows, portfolio state, account state, or orders are mutated.",
        },
    }


def note_to_dict(note: WalkForwardAdjustmentNote) -> dict[str, Any]:
    return {
        "index": note.index,
        "score_id": note.score_id,
        "outcome_id": note.outcome_id,
        "replay_corpus_id": note.replay_corpus_id,
        "symbol": note.symbol,
        "horizon": note.horizon,
        "replay_timestamp": note.replay_timestamp,
        "scorer_name": note.scorer_name,
        "model_name": note.model_name,
        "scorer_version": note.scorer_version,
        "predicted_direction": note.predicted_direction,
        "actual_direction": note.actual_direction,
        "expected_return": note.expected_return,
        "actual_excess_return": note.actual_excess_return,
        "absolute_excess_error": note.absolute_excess_error,
        "direction_hit": note.direction_hit,
        "gap_type": note.gap_type,
        "tracks": list(note.tracks),
        "pattern_keys": list(note.pattern_keys),
        "weak_patterns_before": list(note.weak_patterns_before),
        "reason_category": note.reason_category,
        "cause_summary": note.cause_summary,
        "recommended_memory_update": note.recommended_memory_update,
        "reflex_candidate": note.reflex_candidate,
        "evidence_snapshot": note.evidence_snapshot,
        "analysis_provider": note.analysis_provider,
        "analysis_model": note.analysis_model,
        "note_version": note.note_version,
        "raw": note.raw or {},
    }


def _fmt_pct(value: Any) -> str:
    if value is None:
        return "-"
    return f"{float(value):.2%}"


def _fmt_signed_pct(value: Any) -> str:
    if value is None:
        return "-"
    return f"{float(value):+.2%}"


def walk_forward_adjustment_notes_markdown(
    result: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
    title: str = "Walk-Forward Adjustment Notes",
) -> str:
    summary = result.get("summary") or {}
    lines = [
        f"# {title}",
        "",
        f"- Generated at: `{datetime.now(UTC).isoformat()}`",
        f"- Version: `{result.get('version')}`",
        f"- Rows scanned: `{result.get('rows_scanned')}`",
        f"- Candidate gap rows: `{result.get('candidate_rows')}`",
        f"- Notes emitted: `{summary.get('notes')}`",
        f"- Analysis provider: `{summary.get('analysis_provider')}`",
        f"- Reflex candidates: `{summary.get('reflex_candidates')}`",
        "- Leakage policy: rows are processed chronologically; prior-memory gates use only outcomes observed before each row.",
        "- Safety: report-only; no scorer mutation, IBKR account query, portfolio access, or order action.",
        "",
        "## Filters",
        "",
    ]
    if filters:
        for key, value in filters.items():
            lines.append(f"- `{key}`: `{value if value is not None else '-'}`")
    else:
        lines.append("- none")

    lines.extend(["", "## Category counts", "", "| Category | Notes |", "| --- | ---: |"])
    categories = summary.get("categories") or {}
    if not categories:
        lines.append("| - | 0 |")
    for category, count in sorted(categories.items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"| {category} | {count} |")

    lines.extend(["", "## Track counts", "", "| Track | Notes |", "| --- | ---: |"])
    tracks = summary.get("tracks") or {}
    if not tracks:
        lines.append("| - | 0 |")
    for track, count in sorted(tracks.items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"| {track} | {count} |")

    errors = summary.get("errors") or []
    if errors:
        lines.extend(["", "## Analysis errors", "", "| Score ID | Error |", "| ---: | --- |"])
        for item in errors[:20]:
            lines.append(f"| {item.get('score_id')} | {str(item.get('error') or '').replace('|', '/')} |")

    lines.extend(
        [
            "",
            "## Notes",
            "",
            "| Time | Symbol | Horizon | Pred | Actual | Gap | Abs error | Category | Reflex | Cause | Memory update |",
            "| --- | --- | --- | --- | --- | --- | ---: | --- | --- | --- | --- |",
        ]
    )
    notes = result.get("notes") or []
    if not notes:
        lines.append("| - | - | - | - | - | - | - | - | - | - | - |")
    for note in notes:
        lines.append(
            f"| {note.get('replay_timestamp')} | {note.get('symbol')} | {note.get('horizon')} "
            f"| {note.get('predicted_direction')} | {note.get('actual_direction')} | {note.get('gap_type')} "
            f"| {_fmt_signed_pct(note.get('absolute_excess_error'))} | {note.get('reason_category')} "
            f"| {note.get('reflex_candidate')} | {str(note.get('cause_summary') or '')[:260].replace('|', '/')} "
            f"| {str(note.get('recommended_memory_update') or '')[:240].replace('|', '/')} |"
        )

    lines.extend(
        [
            "",
            "## Next-use note",
            "",
            "These notes are designed to become training memory for later threshold tuning. Emergency/reflex candidates can be promoted into deterministic gates only after repeated prior observations, while standing-regime and macro/economic notes should alter thresholds rather than overwrite the original market-news prediction.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_walk_forward_adjustment_notes_markdown(
    output_path: Path,
    result: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(walk_forward_adjustment_notes_markdown(result, filters=filters), encoding="utf-8")
    return output_path


def write_walk_forward_adjustment_notes_json(output_path: Path, result: dict[str, Any]) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return output_path
