from __future__ import annotations

import json
import math
import re
import time
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .ai_provider import PermanentAnalysisError, TransientAnalysisError
from .evidence_packet import build_overlay_evidence_packet, build_tri_desk_alignment


REPLAY_SCORER_VERSION = "replay-heuristic-score-v1"
OPENAI_REPLAY_SCORER_VERSION_V1 = "openai-replay-score-v1"
OPENAI_REPLAY_SCORER_VERSION_V2 = "openai-replay-score-v2"
OPENAI_REPLAY_SCORER_VERSION_V3 = "openai-replay-score-v3"
OPENAI_REPLAY_SCORER_VERSION_V4 = "openai-replay-score-v4"
OPENAI_REPLAY_SCORER_VERSION_V5 = "openai-replay-score-v5"
OPENAI_REPLAY_SCORER_VERSION_V6 = "openai-replay-score-v6"
OPENAI_REPLAY_SCORER_VERSION_V7 = "openai-replay-score-v7"
OPENAI_REPLAY_SCORER_VERSION_V8 = "openai-replay-score-v8"
OPENAI_REPLAY_SCORER_VERSION_V9 = "openai-replay-score-v9"
OPENAI_REPLAY_SCORER_VERSION_V10 = "openai-replay-score-v10"
OPENAI_REPLAY_SCORER_VERSION_V11 = "openai-replay-score-v11"
OPENAI_REPLAY_SCORER_VERSION_V12 = "openai-replay-score-v12"
OPENAI_REPLAY_SCORER_VERSION_V13 = "openai-replay-score-v13"
OPENAI_REPLAY_SCORER_VERSION_V14 = "openai-replay-score-v14"
OPENAI_REPLAY_SCORER_VERSION_V15 = "openai-replay-score-v15"
OPENAI_REPLAY_SCORER_VERSION_V16 = "openai-replay-score-v16"
OPENAI_REPLAY_SCORER_VERSION_V14_OVERLAY_V1 = "openai-replay-score-v14-overlay-v1"
OPENAI_REPLAY_SCORER_VERSION_V14_OVERLAY_V2 = "openai-replay-score-v14-overlay-v2"
OPENAI_REPLAY_SCORER_VERSION_V14_OVERLAY_V3 = "openai-replay-score-v14-overlay-v3"
OPENAI_REPLAY_SCORER_VERSION_V14_NUANCED_V1 = "openai-replay-score-v14-nuanced-v1"
OPENAI_REPLAY_SCORER_VERSION = "openai-replay-score-v6"

V14_OVERLAY_V2_THRESHOLDS = {
    "overlay_score_min": 4.5,
    "conflict_score_min": 0.50,
    "confirmation_score_min": 0.50,
    "age_hours_max": 24.0,
    "confirmation_conflict_max": 0.25,
    "critical_only": False,
}

V14_OVERLAY_V3_STANDING_FAMILY_THRESHOLDS = {
    "families": ("critical_minerals_geopolitics",),
    "relation": "conflict",
    "tri_desk_conflict_score_min": 0.50,
}

DEFAULT_NUANCED_ACTION_POLICY_PATH = Path("exports/m87_walk_forward_nuanced_actions_v14.json")

_POSITIVE_TERMS = {
    "beat": 1.2,
    "beats": 1.2,
    "above": 0.5,
    "raise": 1.0,
    "raises": 1.0,
    "raised": 1.0,
    "growth": 0.6,
    "record": 0.5,
    "strong": 0.7,
    "strength": 0.7,
    "accelerate": 0.7,
    "accelerated": 0.7,
    "improve": 0.6,
    "improved": 0.6,
    "higher": 0.5,
    "up": 0.3,
}

_NEGATIVE_TERMS = {
    "miss": -1.2,
    "misses": -1.2,
    "below": -0.5,
    "cut": -1.0,
    "cuts": -1.0,
    "lowered": -1.0,
    "weak": -0.7,
    "weakness": -0.7,
    "decline": -0.6,
    "declined": -0.6,
    "decrease": -0.6,
    "decreased": -0.6,
    "loss": -0.7,
    "down": -0.3,
    "risk": -0.4,
    "uncertain": -0.4,
    "uncertainty": -0.4,
}

_HORIZON_SCALE = {
    "30M": 0.35,
    "2H": 0.55,
    "1D": 1.0,
    "5D": 1.25,
}


@dataclass(frozen=True)
class ReplayScore:
    replay_corpus_id: int
    outcome_id: int
    historical_event_id: int
    official_document_id: int
    symbol: str
    event_type: str
    event_subtype: str
    replay_timestamp: str
    prediction_cutoff_at: str
    horizon: str
    price_source: str
    scorer_name: str
    model_name: str
    scorer_version: str
    input_snapshot_hash: str
    prediction_status: str
    predicted_direction: str
    expected_return: float
    bear_return: float
    bull_return: float
    up_probability: float
    flat_probability: float
    down_probability: float
    confidence: float
    rationale: str
    raw_json: str
    actual_direction: str
    actual_raw_return: float | None
    actual_excess_return: float | None
    direction_hit: int | None
    absolute_raw_error: float | None
    absolute_excess_error: float | None


class OpenAIReplayPrediction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    predicted_direction: Literal["up", "down", "flat"]
    expected_return: float = Field(ge=-1.0, le=5.0)
    bear_return: float = Field(ge=-1.0, le=5.0)
    bull_return: float = Field(ge=-1.0, le=5.0)
    up_probability: float = Field(ge=0.0, le=1.0)
    flat_probability: float = Field(ge=0.0, le=1.0)
    down_probability: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(min_length=1, max_length=800)

    @model_validator(mode="after")
    def validate_probability_mass(self) -> "OpenAIReplayPrediction":
        total = self.up_probability + self.flat_probability + self.down_probability
        if not 0.98 <= total <= 1.02:
            raise ValueError("up/flat/down probabilities must sum to approximately 1.0")
        if self.bear_return > self.expected_return or self.expected_return > self.bull_return:
            raise ValueError("return range must satisfy bear <= expected <= bull")
        return self


def _snapshot(row: dict[str, Any]) -> dict[str, Any]:
    try:
        payload = json.loads(row.get("input_snapshot_json") or "{}")
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _document_text(snapshot: dict[str, Any]) -> str:
    document = snapshot.get("official_document")
    if not isinstance(document, dict):
        return ""
    title = str(document.get("title") or "")
    content = str(document.get("content") or "")
    return f"{title}\n{content}"


def _softmax3(up_signal: float) -> tuple[float, float, float]:
    up_logit = up_signal
    down_logit = -up_signal
    flat_logit = 0.5 - abs(up_signal) * 0.35
    values = [math.exp(up_logit), math.exp(flat_logit), math.exp(down_logit)]
    total = sum(values)
    return values[0] / total, values[1] / total, values[2] / total


def _direction(value: float) -> str:
    if value > 0.002:
        return "up"
    if value < -0.002:
        return "down"
    return "flat"


def _extract_openai_parsed(response: Any) -> OpenAIReplayPrediction:
    direct = getattr(response, "output_parsed", None)
    if isinstance(direct, OpenAIReplayPrediction):
        return direct
    refusal_messages: list[str] = []
    for output in getattr(response, "output", []) or []:
        for content in getattr(output, "content", []) or []:
            parsed = getattr(content, "parsed", None)
            if isinstance(parsed, OpenAIReplayPrediction):
                return parsed
            if getattr(content, "type", None) == "refusal":
                refusal_messages.append(str(getattr(content, "refusal", "refused")))
    if refusal_messages:
        raise PermanentAnalysisError("Model refused replay scoring request: " + "; ".join(refusal_messages))
    raise TransientAnalysisError("Structured response did not contain a parsed OpenAIReplayPrediction")


def _map_openai_exception(exc: Exception) -> Exception:
    name = exc.__class__.__name__
    message = str(exc)[:1500]
    status = getattr(exc, "status_code", None)
    transient_names = {
        "APIConnectionError",
        "APITimeoutError",
        "RateLimitError",
        "InternalServerError",
    }
    permanent_names = {
        "AuthenticationError",
        "PermissionDeniedError",
        "NotFoundError",
        "UnprocessableEntityError",
        "BadRequestError",
    }
    if name in transient_names or (isinstance(status, int) and status >= 500):
        return TransientAnalysisError(f"OpenAI transient replay scoring error ({name}): {message}")
    if name == "AuthenticationError":
        return PermanentAnalysisError("OpenAI replay scoring authentication failed. Check OPENAI_API_KEY.")
    if name in permanent_names or (isinstance(status, int) and 400 <= status < 500):
        return PermanentAnalysisError(f"OpenAI replay scoring request/configuration error ({name}): {message}")
    return TransientAnalysisError(f"OpenAI unexpected replay scoring error ({name}): {message}")


def _make_score_from_prediction(
    row: dict[str, Any],
    *,
    scorer_name: str,
    model_name: str,
    scorer_version: str,
    prediction: OpenAIReplayPrediction,
    raw_json: str,
    prediction_status: str = "complete",
) -> ReplayScore:
    actual_direction = str(row.get("direction_label") or "unknown")
    actual_raw = row.get("raw_return")
    actual_excess = row.get("excess_return")
    actual_raw_float = float(actual_raw) if actual_raw is not None else None
    actual_excess_float = float(actual_excess) if actual_excess is not None else None
    direction_hit = (
        int(prediction.predicted_direction == actual_direction)
        if actual_direction in {"up", "down", "flat"}
        else None
    )
    absolute_raw_error = (
        abs(prediction.expected_return - actual_raw_float)
        if actual_raw_float is not None
        else None
    )
    absolute_excess_error = (
        abs(prediction.expected_return - actual_excess_float)
        if actual_excess_float is not None
        else None
    )
    return ReplayScore(
        replay_corpus_id=int(row["replay_corpus_id"]),
        outcome_id=int(row["outcome_id"]),
        historical_event_id=int(row["historical_event_id"]),
        official_document_id=int(row["official_document_id"]),
        symbol=str(row["actual_subject_symbol"]).upper(),
        event_type=str(row["event_type"]),
        event_subtype=str(row["event_subtype"] or ""),
        replay_timestamp=str(row["replay_timestamp"]),
        prediction_cutoff_at=str(row["prediction_cutoff_at"]),
        horizon=str(row["horizon"]).upper(),
        price_source=str(row["price_source"]),
        scorer_name=scorer_name,
        model_name=model_name,
        scorer_version=scorer_version,
        input_snapshot_hash=str(row["input_snapshot_hash"]),
        prediction_status=prediction_status,
        predicted_direction=prediction.predicted_direction,
        expected_return=prediction.expected_return,
        bear_return=prediction.bear_return,
        bull_return=prediction.bull_return,
        up_probability=prediction.up_probability,
        flat_probability=prediction.flat_probability,
        down_probability=prediction.down_probability,
        confidence=prediction.confidence,
        rationale=prediction.rationale,
        raw_json=raw_json,
        actual_direction=actual_direction,
        actual_raw_return=actual_raw_float,
        actual_excess_return=actual_excess_float,
        direction_hit=direction_hit,
        absolute_raw_error=absolute_raw_error,
        absolute_excess_error=absolute_excess_error,
    )


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _compact_scorer_evidence_packet(packet: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(packet, dict):
        return None
    overlay = packet.get("overlay") if isinstance(packet.get("overlay"), dict) else {}
    market_regime = packet.get("market_regime") if isinstance(packet.get("market_regime"), dict) else {}
    economic = packet.get("economic_indicator_desk") if isinstance(packet.get("economic_indicator_desk"), dict) else {}
    tri_desk = packet.get("tri_desk_alignment") if isinstance(packet.get("tri_desk_alignment"), dict) else {}
    option_structure = packet.get("option_structure_context") if isinstance(packet.get("option_structure_context"), dict) else {}
    return {
        "packet_version": packet.get("packet_version"),
        "event_cutoff_date": (packet.get("event") or {}).get("cutoff_date") if isinstance(packet.get("event"), dict) else "",
        "packet_strength": packet.get("packet_strength"),
        "overlay": {
            "overlay_state": overlay.get("overlay_state"),
            "overlay_action": overlay.get("overlay_action"),
            "overlay_score": overlay.get("overlay_score"),
            "matched_signal_count": overlay.get("matched_signal_count"),
            "region": overlay.get("region"),
            "theme": overlay.get("theme"),
            "emergency": overlay.get("emergency"),
            "urgency": overlay.get("urgency"),
            "age_hours": overlay.get("age_hours"),
            "latency_policy": overlay.get("latency_policy"),
            "system_action": overlay.get("system_action"),
            "asset_overlap": overlay.get("asset_overlap"),
            "top_signals": [
                {
                    "source": item.get("source"),
                    "published_at": item.get("published_at"),
                    "region": item.get("region"),
                    "theme": item.get("theme"),
                    "sentiment_label": item.get("sentiment_label"),
                    "confidence": item.get("confidence"),
                    "prediction_gate_status": item.get("prediction_gate_status"),
                    "emergency": item.get("emergency"),
                    "urgency": item.get("urgency"),
                    "age_hours": item.get("age_hours"),
                    "title": item.get("title"),
                }
                for item in (overlay.get("top_signals") or [])[:3]
                if isinstance(item, dict)
            ],
        },
        "economic_indicator_desk": {
            "risk_bias": economic.get("risk_bias"),
            "covered_series_count": economic.get("covered_series_count"),
            "series_count": economic.get("series_count"),
            "macro_flags": (economic.get("macro_flags") or [])[:6],
            "scorer_takeaways": (economic.get("scorer_takeaways") or [])[:5],
        },
        "market_data_desk": {
            "regime": market_regime.get("regime"),
            "risk_off_flags": (market_regime.get("risk_off_flags") or [])[:6],
            "risk_on_flags": (market_regime.get("risk_on_flags") or [])[:6],
            "inflation_pressure_flags": (market_regime.get("inflation_pressure_flags") or [])[:4],
            "dollar_pressure_flags": (market_regime.get("dollar_pressure_flags") or [])[:4],
            "technical_indicator_count": market_regime.get("technical_indicator_count"),
            "macro_observation_count": market_regime.get("macro_observation_count"),
        },
        "options_market_structure": {
            "available": option_structure.get("available"),
            "source": option_structure.get("source"),
            "freshness_status": option_structure.get("freshness_status"),
            "overall_status": option_structure.get("overall_status"),
            "mapped_move_count": option_structure.get("mapped_move_count"),
            "matched_row_count": option_structure.get("matched_row_count"),
            "selected_source_type": option_structure.get("selected_source_type"),
            "coverage_status": option_structure.get("coverage_status"),
            "matched_option_symbol": option_structure.get("matched_option_symbol"),
            "gamma_regime": option_structure.get("gamma_regime"),
            "call_wall": option_structure.get("call_wall"),
            "put_wall": option_structure.get("put_wall"),
            "max_pain": option_structure.get("max_pain"),
            "distance_to_pin_pct": option_structure.get("distance_to_pin_pct"),
            "proximity_alerts": option_structure.get("proximity_alerts") or [],
            "explanation_status": option_structure.get("explanation_status"),
            "scorer_bias": option_structure.get("scorer_bias"),
            "scorer_action": option_structure.get("scorer_action"),
            "confidence": option_structure.get("confidence"),
            "guidance": (option_structure.get("guidance") or [])[:5],
        },
        "tri_desk_alignment": {
            "target_prediction": tri_desk.get("target_prediction"),
            "alignment": tri_desk.get("alignment"),
            "scorer_action": tri_desk.get("scorer_action"),
            "confirmation_score": tri_desk.get("confirmation_score"),
            "conflict_score": tri_desk.get("conflict_score"),
            "net_alignment_score": tri_desk.get("net_alignment_score"),
            "vote_summary": tri_desk.get("vote_summary"),
        },
    }


def _tri_desk_alignment_for_prediction(
    *,
    row: dict[str, Any],
    prediction: OpenAIReplayPrediction,
) -> dict[str, Any] | None:
    packet = row.get("scorer_evidence_packet")
    if not isinstance(packet, dict):
        return None
    overlay = packet.get("overlay")
    economic = packet.get("economic_indicator_desk")
    market_regime = packet.get("market_regime")
    option_structure = packet.get("option_structure_context")
    if not isinstance(overlay, dict) or not isinstance(economic, dict) or not isinstance(market_regime, dict):
        return None
    scoring_row = dict(row)
    scoring_row["predicted_direction"] = prediction.predicted_direction
    return build_tri_desk_alignment(
        row=scoring_row,
        overlay=overlay,
        economic_indicator_desk=economic,
        market_regime=market_regime,
        standing_regime_overlay=packet.get("standing_regime_overlay") if isinstance(packet.get("standing_regime_overlay"), dict) else None,
        option_structure_context=option_structure if isinstance(option_structure, dict) else None,
    )


def _daily_tri_desk_evidence_packet_openai_prediction(
    prediction: OpenAIReplayPrediction,
    row: dict[str, Any],
) -> tuple[OpenAIReplayPrediction, dict[str, Any]]:
    """Apply v2 scorer evidence packet as a non-leaking v14 action/confidence gate.

    v14 deliberately keeps v13's direction logic first.  It then scores whether
    the tri-desk evidence packet confirms or contradicts the chosen direction.
    High conflict becomes observe-only/second-pass metadata, medium conflict
    applies a confidence haircut, and strong confirmation can release narrow
    low-edge observe-only rows back to directional-signal metadata.  No realized
    outcome, future price, or future news is used.
    """

    v13_prediction, v13_metadata = _daily_v10_narrow_action_gate_openai_prediction(prediction, row)
    alignment = _tri_desk_alignment_for_prediction(row=row, prediction=v13_prediction)
    if alignment is None:
        return v13_prediction, {
            "applied": bool(v13_metadata.get("applied")),
            "method": "daily_v14_no_evidence_packet",
            "v13": v13_metadata,
            "chosen_direction": v13_prediction.predicted_direction,
            "trade_action": v13_metadata.get("trade_action", "directional_signal"),
            "observe_only": bool(v13_metadata.get("observe_only")),
            "observe_only_reason": str(v13_metadata.get("observe_only_reason") or ""),
            "tri_desk_alignment": None,
        }

    scorer_action = str(alignment.get("scorer_action") or "")
    conflict_score = float(alignment.get("conflict_score") or 0.0)
    confirmation_score = float(alignment.get("confirmation_score") or 0.0)
    net_alignment = float(alignment.get("net_alignment_score") or 0.0)
    trade_action = str(v13_metadata.get("trade_action") or "directional_signal")
    observe_only = bool(v13_metadata.get("observe_only"))
    observe_only_reason = str(v13_metadata.get("observe_only_reason") or "")
    method = "daily_v14_kept_v13_gate"
    tuned = v13_prediction
    applied = bool(v13_metadata.get("applied"))

    if scorer_action == "second_pass_required" and v13_prediction.predicted_direction in {"up", "down"}:
        shrink = 0.55
        tuned_expected = float(v13_prediction.expected_return) * shrink
        tuned_bear = min(float(v13_prediction.bear_return), tuned_expected - max(0.006, abs(tuned_expected) * 1.6))
        tuned_bull = max(float(v13_prediction.bull_return), tuned_expected + max(0.006, abs(tuned_expected) * 1.6))
        tuned = v13_prediction.model_copy(
            update={
                "expected_return": tuned_expected,
                "bear_return": tuned_bear,
                "bull_return": tuned_bull,
                "confidence": _clamp(float(v13_prediction.confidence) * 0.72, 0.20, 0.42),
                "rationale": (
                    v13_prediction.rationale
                    + " Daily v14 note: tri-desk evidence packet strongly contradicted this direction; "
                    + "confidence and expected return were cut and the row was marked second-pass/observe-only."
                )[:800],
            }
        )
        tuned.validate_probability_mass()
        method = "daily_v14_tri_desk_second_pass_required_v1"
        trade_action = "observe_only"
        observe_only = True
        observe_only_reason = "tri_desk_second_pass_required"
        applied = True
    elif scorer_action == "confidence_haircut" and v13_prediction.predicted_direction in {"up", "down"}:
        tuned_expected = float(v13_prediction.expected_return) * 0.75
        tuned = v13_prediction.model_copy(
            update={
                "expected_return": tuned_expected,
                "bear_return": min(float(v13_prediction.bear_return), tuned_expected - max(0.005, abs(tuned_expected))),
                "bull_return": max(float(v13_prediction.bull_return), tuned_expected + max(0.005, abs(tuned_expected))),
                "confidence": _clamp(float(v13_prediction.confidence) * 0.85, 0.22, 0.50),
                "rationale": (
                    v13_prediction.rationale
                    + " Daily v14 note: tri-desk evidence was conflicted, so confidence was reduced "
                    + "without changing the stored direction."
                )[:800],
            }
        )
        tuned.validate_probability_mass()
        method = "daily_v14_tri_desk_confidence_haircut_v1"
        applied = True
    elif (
        scorer_action == "confirm_direction"
        and observe_only
        and observe_only_reason in {"low_edge_directional_signal", "non_daily_low_edge_or_flat"}
        and v13_prediction.predicted_direction in {"up", "down"}
        and conflict_score <= 0.15
        and confirmation_score >= 0.67
    ):
        tuned = v13_prediction.model_copy(
            update={
                "confidence": _clamp(float(v13_prediction.confidence) * 1.05, 0.28, 0.62),
                "rationale": (
                    v13_prediction.rationale
                    + " Daily v14 note: tri-desk evidence confirmed the direction, so the narrow "
                    + "low-edge observe-only gate was released to directional metadata."
                )[:800],
            }
        )
        tuned.validate_probability_mass()
        method = "daily_v14_tri_desk_confirmation_release_v1"
        trade_action = "directional_signal"
        observe_only = False
        observe_only_reason = ""
        applied = True

    return tuned, {
        "applied": applied,
        "method": method,
        "v13": v13_metadata,
        "chosen_direction": tuned.predicted_direction,
        "trade_action": trade_action,
        "observe_only": observe_only,
        "observe_only_reason": observe_only_reason,
        "tri_desk_alignment": alignment,
        "tri_desk_scorer_action": scorer_action,
        "tri_desk_conflict_score": conflict_score,
        "tri_desk_confirmation_score": confirmation_score,
        "tri_desk_net_alignment_score": net_alignment,
    }


def _fresh_emergency_overlay_context(
    *,
    row: dict[str, Any],
    prediction: OpenAIReplayPrediction,
) -> dict[str, Any]:
    packet = row.get("scorer_evidence_packet")
    overlay = packet.get("overlay") if isinstance(packet, dict) and isinstance(packet.get("overlay"), dict) else {}
    top_signals = overlay.get("top_signals") if isinstance(overlay.get("top_signals"), list) else []
    emergency_signals = [item for item in top_signals if isinstance(item, dict) and item.get("emergency") is True]
    top_emergency = emergency_signals[0] if emergency_signals else overlay if overlay.get("emergency") is True else {}
    urgency = str(top_emergency.get("urgency") or overlay.get("urgency") or "").lower()
    try:
        age_hours = float(top_emergency.get("age_hours") if top_emergency.get("age_hours") is not None else overlay.get("age_hours"))
    except (TypeError, ValueError):
        age_hours = 9999.0
    try:
        overlay_score = float(overlay.get("overlay_score") or 0.0)
    except (TypeError, ValueError):
        overlay_score = 0.0
    emergency = bool(top_emergency) or overlay.get("emergency") is True
    overlay_action = str(overlay.get("overlay_action") or "")
    predicted = str(prediction.predicted_direction or "").lower()
    if not emergency:
        relation = "no_emergency"
    elif overlay_action == "risk_reduce" and predicted == "up":
        relation = "emergency_conflicts_up"
    elif overlay_action == "risk_reduce" and predicted == "down":
        relation = "emergency_confirms_down"
    elif overlay_action == "risk_reduce" and predicted == "flat":
        relation = "emergency_conflicts_flat"
    elif overlay_action == "caution":
        relation = "emergency_caution"
    else:
        relation = "emergency_neutral"
    return {
        "emergency": emergency,
        "relation": relation,
        "urgency": urgency,
        "age_hours": round(age_hours, 3) if age_hours < 9999.0 else None,
        "fresh_24h": age_hours <= 24.0,
        "fresh_4h": age_hours <= 4.0,
        "critical": urgency == "critical",
        "high_or_critical": urgency in {"high", "critical"},
        "overlay_action": overlay_action,
        "overlay_state": str(overlay.get("overlay_state") or ""),
        "overlay_score": round(overlay_score, 3),
        "region": str(overlay.get("region") or ""),
        "theme": str(overlay.get("theme") or ""),
        "title": str(top_emergency.get("title") or overlay.get("title") or "")[:220],
    }


def _daily_tri_desk_emergency_v15_openai_prediction(
    prediction: OpenAIReplayPrediction,
    row: dict[str, Any],
) -> tuple[OpenAIReplayPrediction, dict[str, Any]]:
    """Apply a narrow v15 gate that combines tri-desk and emergency context.

    v15 keeps v13 as the base direction stack, then recomputes tri-desk
    alignment for that direction and cross-checks it against fresh
    emergency-breaking overlay context.  The goal is to avoid v14's overly
    broad observe-only behavior: a tri-desk conflict alone gets a confidence
    haircut, while a fresh emergency conflict and tri-desk conflict together
    can require a second pass.  Confirmed down-risk calls are preserved.
    """

    v13_prediction, v13_metadata = _daily_v10_narrow_action_gate_openai_prediction(prediction, row)
    alignment = _tri_desk_alignment_for_prediction(row=row, prediction=v13_prediction)
    emergency = _fresh_emergency_overlay_context(row=row, prediction=v13_prediction)
    if alignment is None:
        return v13_prediction, {
            "applied": bool(v13_metadata.get("applied")),
            "method": "daily_v15_no_evidence_packet",
            "v13": v13_metadata,
            "chosen_direction": v13_prediction.predicted_direction,
            "trade_action": v13_metadata.get("trade_action", "directional_signal"),
            "observe_only": bool(v13_metadata.get("observe_only")),
            "observe_only_reason": str(v13_metadata.get("observe_only_reason") or ""),
            "tri_desk_alignment": None,
            "emergency_overlay": emergency,
        }

    scorer_action = str(alignment.get("scorer_action") or "")
    conflict_score = float(alignment.get("conflict_score") or 0.0)
    confirmation_score = float(alignment.get("confirmation_score") or 0.0)
    net_alignment = float(alignment.get("net_alignment_score") or 0.0)
    trade_action = str(v13_metadata.get("trade_action") or "directional_signal")
    observe_only = bool(v13_metadata.get("observe_only"))
    observe_only_reason = str(v13_metadata.get("observe_only_reason") or "")
    method = "daily_v15_kept_v13_gate"
    tuned = v13_prediction
    applied = bool(v13_metadata.get("applied"))

    predicted_direction = str(v13_prediction.predicted_direction or "").lower()
    emergency_conflicts_up = (
        emergency["relation"] == "emergency_conflicts_up"
        and emergency["fresh_24h"]
        and emergency["high_or_critical"]
        and float(emergency["overlay_score"] or 0.0) >= 4.5
    )
    emergency_confirms_down = (
        emergency["relation"] == "emergency_confirms_down"
        and emergency["fresh_24h"]
        and float(emergency["overlay_score"] or 0.0) >= 4.5
    )
    combined_second_pass = (
        predicted_direction == "up"
        and emergency_conflicts_up
        and conflict_score >= 0.40
        and (conflict_score >= 0.67 or emergency["critical"] or emergency["fresh_4h"])
    )
    combined_haircut = (
        predicted_direction == "up"
        and (emergency_conflicts_up or scorer_action in {"second_pass_required", "confidence_haircut"})
        and not combined_second_pass
    )
    confirmed_down = (
        predicted_direction == "down"
        and emergency_confirms_down
        and confirmation_score >= 0.40
        and conflict_score <= 0.40
    )

    if combined_second_pass:
        tuned_expected = float(v13_prediction.expected_return) * 0.50
        tuned = v13_prediction.model_copy(
            update={
                "expected_return": tuned_expected,
                "bear_return": min(float(v13_prediction.bear_return), tuned_expected - max(0.007, abs(tuned_expected) * 1.8)),
                "bull_return": max(float(v13_prediction.bull_return), tuned_expected + max(0.007, abs(tuned_expected) * 1.8)),
                "confidence": _clamp(float(v13_prediction.confidence) * 0.66, 0.20, 0.40),
                "rationale": (
                    v13_prediction.rationale
                    + " Daily v15 note: fresh emergency risk and tri-desk conflict jointly contradicted the up call; "
                    + "confidence was cut and the row was marked second-pass/observe-only."
                )[:800],
            }
        )
        tuned.validate_probability_mass()
        method = "daily_v15_tri_emergency_second_pass_v1"
        trade_action = "observe_only"
        observe_only = True
        observe_only_reason = "tri_desk_emergency_second_pass_required"
        applied = True
    elif confirmed_down:
        tuned = v13_prediction.model_copy(
            update={
                "confidence": _clamp(float(v13_prediction.confidence) * 1.06, 0.30, 0.64),
                "rationale": (
                    v13_prediction.rationale
                    + " Daily v15 note: fresh emergency overlay and tri-desk context confirmed the down-risk call; "
                    + "the signal was preserved instead of weakened."
                )[:800],
            }
        )
        tuned.validate_probability_mass()
        method = "daily_v15_emergency_down_confirmation_preserve_v1"
        trade_action = "directional_signal"
        observe_only = False
        observe_only_reason = ""
        applied = True
    elif combined_haircut and predicted_direction in {"up", "down"}:
        tuned_expected = float(v13_prediction.expected_return) * 0.72
        tuned = v13_prediction.model_copy(
            update={
                "expected_return": tuned_expected,
                "bear_return": min(float(v13_prediction.bear_return), tuned_expected - max(0.005, abs(tuned_expected) * 1.2)),
                "bull_return": max(float(v13_prediction.bull_return), tuned_expected + max(0.005, abs(tuned_expected) * 1.2)),
                "confidence": _clamp(float(v13_prediction.confidence) * 0.82, 0.22, 0.49),
                "rationale": (
                    v13_prediction.rationale
                    + " Daily v15 note: tri-desk or emergency context conflicted with the up call, so confidence "
                    + "was reduced without changing the stored direction."
                )[:800],
            }
        )
        tuned.validate_probability_mass()
        method = "daily_v15_tri_or_emergency_confidence_haircut_v1"
        applied = True
    elif scorer_action == "second_pass_required" and predicted_direction in {"up", "down"}:
        tuned_expected = float(v13_prediction.expected_return) * 0.82
        tuned = v13_prediction.model_copy(
            update={
                "expected_return": tuned_expected,
                "confidence": _clamp(float(v13_prediction.confidence) * 0.90, 0.24, 0.52),
                "rationale": (
                    v13_prediction.rationale
                    + " Daily v15 note: tri-desk conflict alone was treated as a confidence haircut rather than "
                    + "a blanket observe-only block."
                )[:800],
            }
        )
        tuned.validate_probability_mass()
        method = "daily_v15_tri_desk_conflict_haircut_v2"
        applied = True
    elif (
        scorer_action == "confirm_direction"
        and observe_only
        and observe_only_reason in {"low_edge_directional_signal", "non_daily_low_edge_or_flat"}
        and predicted_direction in {"up", "down"}
        and conflict_score <= 0.15
        and confirmation_score >= 0.67
    ):
        tuned = v13_prediction.model_copy(
            update={
                "confidence": _clamp(float(v13_prediction.confidence) * 1.05, 0.28, 0.62),
                "rationale": (
                    v13_prediction.rationale
                    + " Daily v15 note: tri-desk evidence confirmed the direction, so the narrow low-edge "
                    + "observe-only gate was released."
                )[:800],
            }
        )
        tuned.validate_probability_mass()
        method = "daily_v15_tri_desk_confirmation_release_v2"
        trade_action = "directional_signal"
        observe_only = False
        observe_only_reason = ""
        applied = True

    return tuned, {
        "applied": applied,
        "method": method,
        "v13": v13_metadata,
        "chosen_direction": tuned.predicted_direction,
        "trade_action": trade_action,
        "observe_only": observe_only,
        "observe_only_reason": observe_only_reason,
        "tri_desk_alignment": alignment,
        "tri_desk_scorer_action": scorer_action,
        "tri_desk_conflict_score": conflict_score,
        "tri_desk_confirmation_score": confirmation_score,
        "tri_desk_net_alignment_score": net_alignment,
        "emergency_overlay": emergency,
        "combined_gate": {
            "emergency_conflicts_up": emergency_conflicts_up,
            "emergency_confirms_down": emergency_confirms_down,
            "combined_second_pass": combined_second_pass,
            "combined_haircut": combined_haircut,
            "confirmed_down": confirmed_down,
        },
    }


def _daily_tri_desk_emergency_v16_openai_prediction(
    prediction: OpenAIReplayPrediction,
    row: dict[str, Any],
) -> tuple[OpenAIReplayPrediction, dict[str, Any]]:
    """Apply the useful v15 buckets only, without the broad haircut bucket.

    v16 intentionally returns to the v13 daily direction/action base, then adds
    only two outcome-safe gates that separated well in M69 diagnostics:
    combined tri-desk + emergency conflict for up calls, and emergency/tri-desk
    confirmation for down-risk calls.  The weak broad-confidence-haircut bucket
    from v15 is deliberately omitted.
    """

    v13_prediction, v13_metadata = _daily_v10_narrow_action_gate_openai_prediction(prediction, row)
    alignment = _tri_desk_alignment_for_prediction(row=row, prediction=v13_prediction)
    emergency = _fresh_emergency_overlay_context(row=row, prediction=v13_prediction)
    if alignment is None:
        return v13_prediction, {
            "applied": bool(v13_metadata.get("applied")),
            "method": "daily_v16_no_evidence_packet",
            "v13": v13_metadata,
            "chosen_direction": v13_prediction.predicted_direction,
            "trade_action": v13_metadata.get("trade_action", "directional_signal"),
            "observe_only": bool(v13_metadata.get("observe_only")),
            "observe_only_reason": str(v13_metadata.get("observe_only_reason") or ""),
            "tri_desk_alignment": None,
            "emergency_overlay": emergency,
        }

    scorer_action = str(alignment.get("scorer_action") or "")
    conflict_score = float(alignment.get("conflict_score") or 0.0)
    confirmation_score = float(alignment.get("confirmation_score") or 0.0)
    net_alignment = float(alignment.get("net_alignment_score") or 0.0)
    trade_action = str(v13_metadata.get("trade_action") or "directional_signal")
    observe_only = bool(v13_metadata.get("observe_only"))
    observe_only_reason = str(v13_metadata.get("observe_only_reason") or "")
    method = "daily_v16_kept_v13_gate"
    tuned = v13_prediction
    applied = bool(v13_metadata.get("applied"))

    predicted_direction = str(v13_prediction.predicted_direction or "").lower()
    emergency_conflicts_up = (
        emergency["relation"] == "emergency_conflicts_up"
        and emergency["fresh_24h"]
        and emergency["high_or_critical"]
        and float(emergency["overlay_score"] or 0.0) >= 4.5
    )
    emergency_confirms_down = (
        emergency["relation"] == "emergency_confirms_down"
        and emergency["fresh_24h"]
        and float(emergency["overlay_score"] or 0.0) >= 4.5
    )
    combined_second_pass = (
        predicted_direction == "up"
        and emergency_conflicts_up
        and conflict_score >= 0.40
        and (conflict_score >= 0.67 or emergency["critical"] or emergency["fresh_4h"])
    )
    confirmed_down = (
        predicted_direction == "down"
        and emergency_confirms_down
        and confirmation_score >= 0.40
        and conflict_score <= 0.40
    )

    if combined_second_pass:
        tuned_expected = float(v13_prediction.expected_return) * 0.50
        tuned = v13_prediction.model_copy(
            update={
                "expected_return": tuned_expected,
                "bear_return": min(float(v13_prediction.bear_return), tuned_expected - max(0.007, abs(tuned_expected) * 1.8)),
                "bull_return": max(float(v13_prediction.bull_return), tuned_expected + max(0.007, abs(tuned_expected) * 1.8)),
                "confidence": _clamp(float(v13_prediction.confidence) * 0.66, 0.20, 0.40),
                "rationale": (
                    v13_prediction.rationale
                    + " Daily v16 note: fresh emergency risk and tri-desk conflict jointly contradicted the up call; "
                    + "confidence was cut and the row was marked second-pass/observe-only."
                )[:800],
            }
        )
        tuned.validate_probability_mass()
        method = "daily_v16_tri_emergency_second_pass_v2"
        trade_action = "observe_only"
        observe_only = True
        observe_only_reason = "tri_desk_emergency_second_pass_required"
        applied = True
    elif confirmed_down:
        tuned = v13_prediction.model_copy(
            update={
                "confidence": _clamp(float(v13_prediction.confidence) * 1.06, 0.30, 0.64),
                "rationale": (
                    v13_prediction.rationale
                    + " Daily v16 note: fresh emergency overlay and tri-desk context confirmed the down-risk call; "
                    + "the signal was preserved without applying the broad v15 haircut."
                )[:800],
            }
        )
        tuned.validate_probability_mass()
        method = "daily_v16_emergency_down_confirmation_preserve_v2"
        trade_action = "directional_signal"
        observe_only = False
        observe_only_reason = ""
        applied = True

    return tuned, {
        "applied": applied,
        "method": method,
        "v13": v13_metadata,
        "chosen_direction": tuned.predicted_direction,
        "trade_action": trade_action,
        "observe_only": observe_only,
        "observe_only_reason": observe_only_reason,
        "tri_desk_alignment": alignment,
        "tri_desk_scorer_action": scorer_action,
        "tri_desk_conflict_score": conflict_score,
        "tri_desk_confirmation_score": confirmation_score,
        "tri_desk_net_alignment_score": net_alignment,
        "emergency_overlay": emergency,
        "combined_gate": {
            "emergency_conflicts_up": emergency_conflicts_up,
            "emergency_confirms_down": emergency_confirms_down,
            "combined_second_pass": combined_second_pass,
            "confirmed_down": confirmed_down,
            "broad_haircut_disabled": True,
        },
    }


def _prediction_from_score_row(row: dict[str, Any]) -> OpenAIReplayPrediction:
    prediction = OpenAIReplayPrediction(
        predicted_direction=str(row.get("predicted_direction") or "flat"),
        expected_return=float(row.get("expected_return") or 0.0),
        bear_return=float(row.get("bear_return") or 0.0),
        bull_return=float(row.get("bull_return") or 0.0),
        up_probability=float(row.get("up_probability") or 0.0),
        flat_probability=float(row.get("flat_probability") or 0.0),
        down_probability=float(row.get("down_probability") or 0.0),
        confidence=float(row.get("confidence") or 0.0),
        rationale=str(row.get("rationale") or "Stored replay score."),
    )
    prediction.validate_probability_mass()
    return prediction


def _score_from_base_row(
    row: dict[str, Any],
    *,
    prediction: OpenAIReplayPrediction,
    scorer_version: str,
    raw_json: str,
) -> ReplayScore:
    actual_direction = str(row.get("actual_direction") or "unknown")
    actual_raw = row.get("actual_raw_return")
    actual_excess = row.get("actual_excess_return")
    actual_raw_float = float(actual_raw) if actual_raw is not None else None
    actual_excess_float = float(actual_excess) if actual_excess is not None else None
    direction_hit = (
        int(prediction.predicted_direction == actual_direction)
        if actual_direction in {"up", "down", "flat"}
        else None
    )
    absolute_raw_error = (
        abs(prediction.expected_return - actual_raw_float)
        if actual_raw_float is not None
        else None
    )
    absolute_excess_error = (
        abs(prediction.expected_return - actual_excess_float)
        if actual_excess_float is not None
        else None
    )
    return ReplayScore(
        replay_corpus_id=int(row["replay_corpus_id"]),
        outcome_id=int(row["outcome_id"]),
        historical_event_id=int(row["historical_event_id"]),
        official_document_id=int(row["official_document_id"]),
        symbol=str(row.get("symbol") or "").upper(),
        event_type=str(row.get("event_type") or ""),
        event_subtype=str(row.get("event_subtype") or ""),
        replay_timestamp=str(row.get("replay_timestamp") or ""),
        prediction_cutoff_at=str(row.get("prediction_cutoff_at") or ""),
        horizon=str(row.get("horizon") or "").upper(),
        price_source=str(row.get("price_source") or ""),
        scorer_name=str(row.get("scorer_name") or "openai"),
        model_name=str(row.get("model_name") or ""),
        scorer_version=scorer_version,
        input_snapshot_hash=str(row.get("input_snapshot_hash") or ""),
        prediction_status=str(row.get("prediction_status") or "complete"),
        predicted_direction=prediction.predicted_direction,
        expected_return=prediction.expected_return,
        bear_return=prediction.bear_return,
        bull_return=prediction.bull_return,
        up_probability=prediction.up_probability,
        flat_probability=prediction.flat_probability,
        down_probability=prediction.down_probability,
        confidence=prediction.confidence,
        rationale=prediction.rationale,
        raw_json=raw_json,
        actual_direction=actual_direction,
        actual_raw_return=actual_raw_float,
        actual_excess_return=actual_excess_float,
        direction_hit=direction_hit,
        absolute_raw_error=absolute_raw_error,
        absolute_excess_error=absolute_excess_error,
    )


def transform_v14_overlay_postscore(
    row: dict[str, Any],
    evidence_packet: dict[str, Any],
    *,
    target_scorer_version: str = OPENAI_REPLAY_SCORER_VERSION_V14_OVERLAY_V1,
) -> ReplayScore:
    """Transform a stored v14 score using point-in-time overlay gates only.

    This intentionally avoids another OpenAI call. Direction, class
    probabilities, and the base explanation are inherited from the stored v14
    row. The transformer only modifies confidence/action metadata for the two
    emergency/tri-desk buckets that separated well in the M69/M70 diagnostics.
    """

    base_prediction = _prediction_from_score_row(row)
    scoring_row = dict(row)
    scoring_row["scorer_evidence_packet"] = evidence_packet
    alignment = _tri_desk_alignment_for_prediction(row=scoring_row, prediction=base_prediction)
    emergency = _fresh_emergency_overlay_context(row=scoring_row, prediction=base_prediction)
    try:
        base_raw = json.loads(str(row.get("raw_json") or "{}"))
    except json.JSONDecodeError:
        base_raw = {}
    if not isinstance(base_raw, dict):
        base_raw = {}
    base_debias = base_raw.get("debias") if isinstance(base_raw.get("debias"), dict) else {}
    trade_action = str(base_debias.get("trade_action") or "").strip() or (
        "observe_only" if base_debias.get("observe_only") is True else "directional_signal"
    )
    observe_only = bool(base_debias.get("observe_only")) or trade_action == "observe_only"
    observe_only_reason = str(base_debias.get("observe_only_reason") or "")
    method = "daily_v14_overlay_v1_kept_base_v14_gate"
    tuned = base_prediction
    applied = False

    if alignment is None:
        postscore = {
            "applied": False,
            "method": "daily_v14_overlay_v1_no_evidence_packet",
            "base_scorer_version": str(row.get("scorer_version") or ""),
            "chosen_direction": tuned.predicted_direction,
            "trade_action": trade_action,
            "observe_only": observe_only,
            "observe_only_reason": observe_only_reason,
            "tri_desk_alignment": None,
            "emergency_overlay": emergency,
        }
    else:
        conflict_score = float(alignment.get("conflict_score") or 0.0)
        confirmation_score = float(alignment.get("confirmation_score") or 0.0)
        net_alignment = float(alignment.get("net_alignment_score") or 0.0)
        predicted_direction = str(base_prediction.predicted_direction or "").lower()
        emergency_conflicts_up = (
            emergency["relation"] == "emergency_conflicts_up"
            and emergency["fresh_24h"]
            and emergency["high_or_critical"]
            and float(emergency["overlay_score"] or 0.0) >= 4.5
        )
        emergency_confirms_down = (
            emergency["relation"] == "emergency_confirms_down"
            and emergency["fresh_24h"]
            and float(emergency["overlay_score"] or 0.0) >= 4.5
        )
        combined_second_pass = (
            predicted_direction == "up"
            and emergency_conflicts_up
            and conflict_score >= 0.40
            and (conflict_score >= 0.67 or emergency["critical"] or emergency["fresh_4h"])
        )
        confirmed_down = (
            predicted_direction == "down"
            and emergency_confirms_down
            and confirmation_score >= 0.40
            and conflict_score <= 0.40
        )

        if combined_second_pass:
            tuned_expected = float(base_prediction.expected_return) * 0.55
            tuned = base_prediction.model_copy(
                update={
                    "expected_return": tuned_expected,
                    "bear_return": min(float(base_prediction.bear_return), tuned_expected - max(0.006, abs(tuned_expected) * 1.5)),
                    "bull_return": max(float(base_prediction.bull_return), tuned_expected + max(0.006, abs(tuned_expected) * 1.5)),
                    "confidence": _clamp(float(base_prediction.confidence) * 0.72, 0.20, 0.42),
                    "rationale": (
                        base_prediction.rationale
                        + " Postscore overlay v1: stored v14 up call was contradicted by fresh emergency risk "
                        + "and tri-desk conflict; marked second-pass/observe-only without changing direction."
                    )[:800],
                }
            )
            tuned.validate_probability_mass()
            method = "daily_v14_overlay_v1_tri_emergency_second_pass"
            trade_action = "observe_only"
            observe_only = True
            observe_only_reason = "tri_desk_emergency_second_pass_required"
            applied = True
        elif confirmed_down:
            tuned = base_prediction.model_copy(
                update={
                    "confidence": _clamp(float(base_prediction.confidence) * 1.04, 0.30, 0.64),
                    "rationale": (
                        base_prediction.rationale
                        + " Postscore overlay v1: fresh emergency overlay and tri-desk context confirmed the "
                        + "stored v14 down-risk call; preserved directional metadata."
                    )[:800],
                }
            )
            tuned.validate_probability_mass()
            method = "daily_v14_overlay_v1_emergency_down_confirmation_preserve"
            trade_action = "directional_signal"
            observe_only = False
            observe_only_reason = ""
            applied = True

        postscore = {
            "applied": applied,
            "method": method,
            "base_scorer_version": str(row.get("scorer_version") or ""),
            "base_debias": base_debias,
            "chosen_direction": tuned.predicted_direction,
            "trade_action": trade_action,
            "observe_only": observe_only,
            "observe_only_reason": observe_only_reason,
            "tri_desk_alignment": alignment,
            "tri_desk_scorer_action": str(alignment.get("scorer_action") or ""),
            "tri_desk_conflict_score": conflict_score,
            "tri_desk_confirmation_score": confirmation_score,
            "tri_desk_net_alignment_score": net_alignment,
            "emergency_overlay": emergency,
            "combined_gate": {
                "emergency_conflicts_up": emergency_conflicts_up,
                "emergency_confirms_down": emergency_confirms_down,
                "combined_second_pass": combined_second_pass,
                "confirmed_down": confirmed_down,
                "direction_preserved": True,
                "openai_called": False,
            },
        }

    raw_payload = {
        "prediction": tuned.model_dump(mode="json"),
        "original_prediction": base_prediction.model_dump(mode="json"),
        "debias": postscore,
        "base_score": {
            "scorer_name": row.get("scorer_name"),
            "model_name": row.get("model_name"),
            "scorer_version": row.get("scorer_version"),
            "direction_hit": row.get("direction_hit"),
            "confidence": row.get("confidence"),
            "expected_return": row.get("expected_return"),
        },
        "base_raw_debias": base_debias,
        "duration_ms": 0,
        "model": row.get("model_name"),
        "reasoning_effort": "none",
        "scorer_version": target_scorer_version,
        "prompt_profile": "deterministic-v14-postscore-overlay-v1",
    }
    return _score_from_base_row(
        row,
        prediction=tuned,
        scorer_version=target_scorer_version,
        raw_json=json.dumps(raw_payload, ensure_ascii=False, sort_keys=True),
    )


def transform_v14_overlay_postscore_v2(
    row: dict[str, Any],
    evidence_packet: dict[str, Any],
    *,
    target_scorer_version: str = OPENAI_REPLAY_SCORER_VERSION_V14_OVERLAY_V2,
) -> ReplayScore:
    """Apply the M72 best threshold as a deterministic v14 postscore variant."""

    base_prediction = _prediction_from_score_row(row)
    scoring_row = dict(row)
    scoring_row["scorer_evidence_packet"] = evidence_packet
    alignment = _tri_desk_alignment_for_prediction(row=scoring_row, prediction=base_prediction)
    emergency = _fresh_emergency_overlay_context(row=scoring_row, prediction=base_prediction)
    trade_action, observe_only, observe_only_reason, base_debias = _base_trade_action(row)
    method = "daily_v14_overlay_v2_kept_base_v14_gate"
    tuned = base_prediction
    applied = False

    if alignment is None:
        postscore = {
            "applied": False,
            "method": "daily_v14_overlay_v2_no_evidence_packet",
            "base_scorer_version": str(row.get("scorer_version") or ""),
            "chosen_direction": tuned.predicted_direction,
            "trade_action": trade_action,
            "observe_only": observe_only,
            "observe_only_reason": observe_only_reason,
            "tri_desk_alignment": None,
            "emergency_overlay": emergency,
            "thresholds": dict(V14_OVERLAY_V2_THRESHOLDS),
        }
    else:
        conflict_score = float(alignment.get("conflict_score") or 0.0)
        confirmation_score = float(alignment.get("confirmation_score") or 0.0)
        net_alignment = float(alignment.get("net_alignment_score") or 0.0)
        predicted_direction = str(base_prediction.predicted_direction or "").lower()
        emergency_age = emergency.get("age_hours")
        age_ok = emergency_age is not None and float(emergency_age) <= V14_OVERLAY_V2_THRESHOLDS["age_hours_max"]
        urgency_ok = emergency["critical"] if V14_OVERLAY_V2_THRESHOLDS["critical_only"] else emergency["high_or_critical"]
        overlay_ok = float(emergency["overlay_score"] or 0.0) >= V14_OVERLAY_V2_THRESHOLDS["overlay_score_min"]
        emergency_conflicts_up = (
            predicted_direction == "up"
            and emergency["relation"] == "emergency_conflicts_up"
            and age_ok
            and urgency_ok
            and overlay_ok
            and conflict_score >= V14_OVERLAY_V2_THRESHOLDS["conflict_score_min"]
        )
        emergency_confirms_down = (
            predicted_direction == "down"
            and emergency["relation"] == "emergency_confirms_down"
            and age_ok
            and urgency_ok
            and overlay_ok
            and confirmation_score >= V14_OVERLAY_V2_THRESHOLDS["confirmation_score_min"]
            and conflict_score <= V14_OVERLAY_V2_THRESHOLDS["confirmation_conflict_max"]
        )

        if emergency_conflicts_up:
            tuned_expected = float(base_prediction.expected_return) * 0.55
            tuned = base_prediction.model_copy(
                update={
                    "expected_return": tuned_expected,
                    "bear_return": min(float(base_prediction.bear_return), tuned_expected - max(0.006, abs(tuned_expected) * 1.5)),
                    "bull_return": max(float(base_prediction.bull_return), tuned_expected + max(0.006, abs(tuned_expected) * 1.5)),
                    "confidence": _clamp(float(base_prediction.confidence) * 0.72, 0.20, 0.42),
                    "rationale": (
                        base_prediction.rationale
                        + " Postscore overlay v2: M72 threshold gate moved this stored v14 up call to "
                        + "second-pass/observe-only because fresh emergency risk and tri-desk conflict overlapped."
                    )[:800],
                }
            )
            tuned.validate_probability_mass()
            method = "daily_v14_overlay_v2_tri_emergency_second_pass"
            trade_action = "observe_only"
            observe_only = True
            observe_only_reason = "threshold_tri_emergency_second_pass"
            applied = True
        elif emergency_confirms_down:
            tuned = base_prediction.model_copy(
                update={
                    "confidence": _clamp(float(base_prediction.confidence) * 1.04, 0.30, 0.64),
                    "rationale": (
                        base_prediction.rationale
                        + " Postscore overlay v2: M72 threshold gate confirmed the stored v14 down-risk call "
                        + "with fresh emergency overlay and tri-desk confirmation."
                    )[:800],
                }
            )
            tuned.validate_probability_mass()
            method = "daily_v14_overlay_v2_emergency_down_confirmation_preserve"
            trade_action = "directional_signal"
            observe_only = False
            observe_only_reason = ""
            applied = True

        postscore = {
            "applied": applied,
            "method": method,
            "base_scorer_version": str(row.get("scorer_version") or ""),
            "base_debias": base_debias,
            "chosen_direction": tuned.predicted_direction,
            "trade_action": trade_action,
            "observe_only": observe_only,
            "observe_only_reason": observe_only_reason,
            "tri_desk_alignment": alignment,
            "tri_desk_scorer_action": str(alignment.get("scorer_action") or ""),
            "tri_desk_conflict_score": conflict_score,
            "tri_desk_confirmation_score": confirmation_score,
            "tri_desk_net_alignment_score": net_alignment,
            "emergency_overlay": emergency,
            "thresholds": dict(V14_OVERLAY_V2_THRESHOLDS),
            "combined_gate": {
                "emergency_conflicts_up": emergency_conflicts_up,
                "emergency_confirms_down": emergency_confirms_down,
                "combined_second_pass": emergency_conflicts_up,
                "confirmed_down": emergency_confirms_down,
                "direction_preserved": True,
                "openai_called": False,
            },
        }

    raw_payload = {
        "prediction": tuned.model_dump(mode="json"),
        "original_prediction": base_prediction.model_dump(mode="json"),
        "debias": postscore,
        "base_score": {
            "scorer_name": row.get("scorer_name"),
            "model_name": row.get("model_name"),
            "scorer_version": row.get("scorer_version"),
            "direction_hit": row.get("direction_hit"),
            "confidence": row.get("confidence"),
            "expected_return": row.get("expected_return"),
        },
        "base_raw_debias": base_debias,
        "duration_ms": 0,
        "model": row.get("model_name"),
        "reasoning_effort": "none",
        "scorer_version": target_scorer_version,
        "prompt_profile": "deterministic-v14-postscore-overlay-v2",
    }
    return _score_from_base_row(
        row,
        prediction=tuned,
        scorer_version=target_scorer_version,
        raw_json=json.dumps(raw_payload, ensure_ascii=False, sort_keys=True),
    )


def _prediction_from_replay_score(score: ReplayScore) -> OpenAIReplayPrediction:
    prediction = OpenAIReplayPrediction(
        predicted_direction=score.predicted_direction,
        expected_return=score.expected_return,
        bear_return=score.bear_return,
        bull_return=score.bull_return,
        up_probability=score.up_probability,
        flat_probability=score.flat_probability,
        down_probability=score.down_probability,
        confidence=score.confidence,
        rationale=score.rationale,
    )
    prediction.validate_probability_mass()
    return prediction


def _standing_family_gate_for_prediction(
    *,
    row: dict[str, Any],
    evidence_packet: dict[str, Any],
    predicted_direction: str,
    families: tuple[str, ...] = V14_OVERLAY_V3_STANDING_FAMILY_THRESHOLDS["families"],
    conflict_score_min: float = V14_OVERLAY_V3_STANDING_FAMILY_THRESHOLDS["tri_desk_conflict_score_min"],
) -> dict[str, Any]:
    alignment = evidence_packet.get("tri_desk_alignment") or {}
    conflict_score = float(alignment.get("conflict_score") or 0.0)
    matched = []
    for regime in (evidence_packet.get("standing_regime_overlay") or {}).get("active_regimes") or []:
        family = str(regime.get("family") or "")
        if family not in set(families):
            continue
        bias = str(regime.get("directional_bias") or "")
        relation = _standing_family_relation(predicted_direction, bias)
        if relation == "conflict":
            matched.append(
                {
                    "family": family,
                    "bias": bias,
                    "status": str(regime.get("status") or ""),
                    "severity_score": float(regime.get("severity_score") or 0.0),
                    "confidence": float(regime.get("confidence") or 0.0),
                    "relation": relation,
                }
            )
    applies = bool(matched) and conflict_score >= float(conflict_score_min)
    return {
        "applies": applies,
        "families": list(families),
        "matched_regimes": matched,
        "tri_desk_conflict_score": conflict_score,
        "tri_desk_conflict_score_min": float(conflict_score_min),
        "relation": "conflict",
    }


def transform_v14_overlay_postscore_v3(
    row: dict[str, Any],
    evidence_packet: dict[str, Any],
    *,
    target_scorer_version: str = OPENAI_REPLAY_SCORER_VERSION_V14_OVERLAY_V3,
) -> ReplayScore:
    """Apply the M82 standing-regime family conflict gate as a v14 postscore variant.

    v3 keeps the v2 emergency/tri-desk threshold behavior, then adds a
    report-derived family gate: critical-minerals standing-regime conflict plus
    tri-desk conflict >= 0.50 becomes observe-only/second-pass. Direction is
    preserved and no OpenAI call is made.
    """

    v2_score = transform_v14_overlay_postscore_v2(
        row,
        evidence_packet,
        target_scorer_version=target_scorer_version,
    )
    tuned = _prediction_from_replay_score(v2_score)
    try:
        raw_payload = json.loads(v2_score.raw_json)
    except json.JSONDecodeError:
        raw_payload = {}
    if not isinstance(raw_payload, dict):
        raw_payload = {}
    postscore = raw_payload.get("debias") if isinstance(raw_payload.get("debias"), dict) else {}
    trade_action = str(postscore.get("trade_action") or "").strip() or "directional_signal"
    observe_only = bool(postscore.get("observe_only")) or trade_action == "observe_only"
    observe_only_reason = str(postscore.get("observe_only_reason") or "")
    method = str(postscore.get("method") or "daily_v14_overlay_v3_kept_base_v14_gate")
    applied = bool(postscore.get("applied"))

    family_gate = _standing_family_gate_for_prediction(
        row=row,
        evidence_packet=evidence_packet,
        predicted_direction=tuned.predicted_direction,
    )
    if family_gate["applies"]:
        tuned_expected = float(tuned.expected_return) * 0.55
        tuned = tuned.model_copy(
            update={
                "expected_return": tuned_expected,
                "bear_return": min(float(tuned.bear_return), tuned_expected - max(0.006, abs(tuned_expected) * 1.5)),
                "bull_return": max(float(tuned.bull_return), tuned_expected + max(0.006, abs(tuned_expected) * 1.5)),
                "confidence": _clamp(float(tuned.confidence) * 0.72, 0.20, 0.42),
                "rationale": (
                    tuned.rationale
                    + " Postscore overlay v3: critical-minerals standing-regime conflict and tri-desk "
                    + "conflict met the M82 family gate; marked second-pass/observe-only without changing direction."
                )[:800],
            }
        )
        tuned.validate_probability_mass()
        method = "daily_v14_overlay_v3_critical_minerals_family_conflict_second_pass"
        trade_action = "observe_only"
        observe_only = True
        observe_only_reason = "critical_minerals_family_conflict_second_pass"
        applied = True

    postscore.update(
        {
            "applied": applied,
            "method": method,
            "base_scorer_version": str(row.get("scorer_version") or ""),
            "chosen_direction": tuned.predicted_direction,
            "trade_action": trade_action,
            "observe_only": observe_only,
            "observe_only_reason": observe_only_reason,
            "standing_regime_family_gate": family_gate,
            "v2_thresholds": dict(V14_OVERLAY_V2_THRESHOLDS),
            "v3_thresholds": {
                **V14_OVERLAY_V3_STANDING_FAMILY_THRESHOLDS,
                "families": list(V14_OVERLAY_V3_STANDING_FAMILY_THRESHOLDS["families"]),
            },
            "combined_gate": {
                **(postscore.get("combined_gate") if isinstance(postscore.get("combined_gate"), dict) else {}),
                "standing_family_second_pass": bool(family_gate["applies"]),
                "direction_preserved": True,
                "openai_called": False,
            },
        }
    )
    raw_payload.update(
        {
            "prediction": tuned.model_dump(mode="json"),
            "debias": postscore,
            "duration_ms": 0,
            "model": row.get("model_name"),
            "reasoning_effort": "none",
            "scorer_version": target_scorer_version,
            "prompt_profile": "deterministic-v14-postscore-overlay-v3-standing-family-gate",
        }
    )
    return _score_from_base_row(
        row,
        prediction=tuned,
        scorer_version=target_scorer_version,
        raw_json=json.dumps(raw_payload, ensure_ascii=False, sort_keys=True),
    )


def load_nuanced_action_policy(policy_path: Path | str) -> dict[str, dict[str, Any]]:
    path = Path(policy_path).expanduser()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        entries = payload.get("final_threshold_table") or payload.get("threshold_table") or []
    elif isinstance(payload, list):
        entries = payload
    else:
        entries = []
    policy: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("pattern_key") or "").strip()
        if not key:
            continue
        policy[key] = dict(entry)
    return policy


def _nuanced_postscore_action_priority(action: str) -> int:
    return {
        "reflex_observe_only": 5,
        "second_pass_review": 4,
        "neutral_candidate": 3,
        "confidence_haircut": 2,
        "preserve": 1,
    }.get(str(action or "preserve"), 1)


def _normalized_nuanced_postscore_action(entry: dict[str, Any] | None) -> str:
    action = str((entry or {}).get("action") or "preserve").strip()
    if action in {"reflex_observe_only", "second_pass_review", "neutral_candidate", "confidence_haircut"}:
        return action
    return "preserve"


def _nuanced_postscore_matches(
    row: dict[str, Any],
    evidence_packet: dict[str, Any],
    policy: dict[str, dict[str, Any]],
) -> tuple[list[str], list[dict[str, Any]]]:
    from .walk_forward_training import _pattern_keys

    keys = _pattern_keys(row, evidence_packet)
    matches: list[dict[str, Any]] = []
    for key in keys:
        entry = policy.get(key)
        if not isinstance(entry, dict):
            continue
        action = _normalized_nuanced_postscore_action(entry)
        if action == "preserve":
            continue
        matches.append(
            {
                "key": key,
                "action": action,
                "track": entry.get("track"),
                "seen": int(entry.get("seen") or 0),
                "hits": int(entry.get("hits") or 0),
                "hit_rate": entry.get("hit_rate"),
                "flat_rate": entry.get("flat_rate"),
                "source_cycle": entry.get("source_cycle"),
                "reason": str(entry.get("reason") or ""),
            }
        )
    matches.sort(key=lambda item: (-_nuanced_postscore_action_priority(str(item.get("action") or "")), -int(item.get("seen") or 0), str(item.get("key") or "")))
    return keys, matches


def _neutralized_prediction(prediction: OpenAIReplayPrediction) -> OpenAIReplayPrediction:
    flat_probability = max(float(prediction.flat_probability), 0.50)
    remaining = max(0.0, 1.0 - flat_probability)
    directional_total = max(0.000001, float(prediction.up_probability) + float(prediction.down_probability))
    up_probability = remaining * float(prediction.up_probability) / directional_total
    down_probability = remaining * float(prediction.down_probability) / directional_total
    neutral = prediction.model_copy(
        update={
            "predicted_direction": "flat",
            "expected_return": 0.0,
            "bear_return": min(float(prediction.bear_return), -0.006),
            "bull_return": max(float(prediction.bull_return), 0.006),
            "up_probability": up_probability,
            "flat_probability": flat_probability,
            "down_probability": down_probability,
            "confidence": _clamp(float(prediction.confidence) * 0.66, 0.20, 0.40),
            "rationale": (
                prediction.rationale
                + " Nuanced action note: learned walk-forward policy classified this pattern as a neutral/no-edge candidate; "
                + "the postscore action moved the row to flat/observe-only without using realized outcome labels."
            )[:800],
        }
    )
    neutral.validate_probability_mass()
    return neutral


def transform_v14_nuanced_action_postscore(
    row: dict[str, Any],
    evidence_packet: dict[str, Any],
    nuanced_policy: dict[str, dict[str, Any]],
    *,
    target_scorer_version: str = OPENAI_REPLAY_SCORER_VERSION_V14_NUANCED_V1,
    policy_source: str | None = None,
) -> ReplayScore:
    """Apply an M87 walk-forward nuanced action table to a stored score row.

    This is deterministic postscore metadata/action gating. It reads only the
    point-in-time evidence packet plus the learned policy table. It does not
    call OpenAI and does not use future realized labels while transforming a
    row. Direction is preserved for confidence-haircut/second-pass/reflex
    actions; only `neutral_candidate` is converted to a flat observe-only row.
    """

    base_prediction = _prediction_from_score_row(row)
    base_trade_action, base_observe_only, base_observe_only_reason, base_debias = _base_trade_action(row)
    trade_action = "directional_signal"
    observe_only = False
    observe_only_reason = ""
    keys, matches = _nuanced_postscore_matches(row, evidence_packet, nuanced_policy)
    selected = matches[0] if matches else None
    selected_action = str((selected or {}).get("action") or "directional_signal")
    tuned = base_prediction
    method = "daily_v14_nuanced_v1_no_policy_match"
    applied = False

    if selected_action == "confidence_haircut":
        tuned_expected = float(base_prediction.expected_return) * 0.85
        tuned = base_prediction.model_copy(
            update={
                "expected_return": tuned_expected,
                "bear_return": min(float(base_prediction.bear_return), tuned_expected - max(0.004, abs(tuned_expected) * 1.2)),
                "bull_return": max(float(base_prediction.bull_return), tuned_expected + max(0.004, abs(tuned_expected) * 1.2)),
                "confidence": _clamp(float(base_prediction.confidence) * 0.82, 0.20, 0.52),
                "rationale": (
                    base_prediction.rationale
                    + " Nuanced action v1: M87 walk-forward memory kept this pattern tradeable but applied a confidence haircut."
                )[:800],
            }
        )
        tuned.validate_probability_mass()
        trade_action = "confidence_haircut"
        observe_only = False
        observe_only_reason = ""
        method = "daily_v14_nuanced_v1_confidence_haircut"
        applied = True
    elif selected_action == "second_pass_review":
        tuned_expected = float(base_prediction.expected_return) * 0.55
        tuned = base_prediction.model_copy(
            update={
                "expected_return": tuned_expected,
                "bear_return": min(float(base_prediction.bear_return), tuned_expected - max(0.006, abs(tuned_expected) * 1.5)),
                "bull_return": max(float(base_prediction.bull_return), tuned_expected + max(0.006, abs(tuned_expected) * 1.5)),
                "confidence": _clamp(float(base_prediction.confidence) * 0.70, 0.20, 0.42),
                "rationale": (
                    base_prediction.rationale
                    + " Nuanced action v1: M87 walk-forward memory marked this pattern for second-pass review/observe-only."
                )[:800],
            }
        )
        tuned.validate_probability_mass()
        trade_action = "observe_only"
        observe_only = True
        observe_only_reason = "nuanced_second_pass_review"
        method = "daily_v14_nuanced_v1_second_pass_review"
        applied = True
    elif selected_action == "reflex_observe_only":
        tuned_expected = float(base_prediction.expected_return) * 0.40
        tuned = base_prediction.model_copy(
            update={
                "expected_return": tuned_expected,
                "bear_return": min(float(base_prediction.bear_return), tuned_expected - max(0.007, abs(tuned_expected) * 1.8)),
                "bull_return": max(float(base_prediction.bull_return), tuned_expected + max(0.007, abs(tuned_expected) * 1.8)),
                "confidence": _clamp(float(base_prediction.confidence) * 0.60, 0.20, 0.38),
                "rationale": (
                    base_prediction.rationale
                    + " Nuanced action v1: M87 emergency-reflex memory marked this pattern observe-only until stronger confirmation."
                )[:800],
            }
        )
        tuned.validate_probability_mass()
        trade_action = "observe_only"
        observe_only = True
        observe_only_reason = "nuanced_reflex_observe_only"
        method = "daily_v14_nuanced_v1_reflex_observe_only"
        applied = True
    elif selected_action == "neutral_candidate":
        tuned = _neutralized_prediction(base_prediction)
        trade_action = "observe_only"
        observe_only = True
        observe_only_reason = "nuanced_neutral_candidate"
        method = "daily_v14_nuanced_v1_neutral_candidate"
        applied = True

    postscore = {
        "applied": applied,
        "method": method,
        "base_scorer_version": str(row.get("scorer_version") or ""),
        "base_debias": base_debias,
        "base_trade_action": base_trade_action,
        "base_observe_only": base_observe_only,
        "base_observe_only_reason": base_observe_only_reason,
        "chosen_direction": tuned.predicted_direction,
        "trade_action": trade_action,
        "observe_only": observe_only,
        "observe_only_reason": observe_only_reason,
        "nuanced_action": selected_action,
        "nuanced_policy_source": policy_source or "",
        "nuanced_policy_entries": len(nuanced_policy),
        "nuanced_policy_matches": matches[:5],
        "pattern_keys": keys,
        "combined_gate": {
            "direction_preserved": tuned.predicted_direction == base_prediction.predicted_direction,
            "openai_called": False,
            "confidence_haircut_tradeable": selected_action == "confidence_haircut",
        },
    }
    raw_payload = {
        "prediction": tuned.model_dump(mode="json"),
        "original_prediction": base_prediction.model_dump(mode="json"),
        "debias": postscore,
        "base_score": {
            "scorer_name": row.get("scorer_name"),
            "model_name": row.get("model_name"),
            "scorer_version": row.get("scorer_version"),
            "direction_hit": row.get("direction_hit"),
            "confidence": row.get("confidence"),
            "expected_return": row.get("expected_return"),
        },
        "base_raw_debias": base_debias,
        "duration_ms": 0,
        "model": row.get("model_name"),
        "reasoning_effort": "none",
        "scorer_version": target_scorer_version,
        "prompt_profile": "deterministic-v14-postscore-nuanced-action-v1",
    }
    return _score_from_base_row(
        row,
        prediction=tuned,
        scorer_version=target_scorer_version,
        raw_json=json.dumps(raw_payload, ensure_ascii=False, sort_keys=True),
    )


class ReplayPostscoreTransformer:
    def __init__(
        self,
        db: Any,
        *,
        base_scorer_name: str = "openai",
        base_model_name: str | None = None,
        base_scorer_version: str = OPENAI_REPLAY_SCORER_VERSION_V14,
        target_scorer_version: str = OPENAI_REPLAY_SCORER_VERSION_V14_OVERLAY_V1,
        signal_limit: int = 12000,
        nuanced_policy_path: Path | str | None = None,
    ) -> None:
        self.db = db
        self.base_scorer_name = base_scorer_name
        self.base_model_name = base_model_name
        self.base_scorer_version = base_scorer_version
        self.target_scorer_version = target_scorer_version
        self.signal_limit = max(0, int(signal_limit))
        self.nuanced_policy_path = nuanced_policy_path
        self._nuanced_policy: dict[str, dict[str, Any]] | None = None

    def _load_nuanced_policy(self) -> dict[str, dict[str, Any]]:
        if self._nuanced_policy is None:
            path = self.nuanced_policy_path or DEFAULT_NUANCED_ACTION_POLICY_PATH
            self._nuanced_policy = load_nuanced_action_policy(path)
            self.nuanced_policy_path = path
        return self._nuanced_policy

    def build(
        self,
        *,
        event_type: str | None = None,
        symbols: tuple[str, ...] | None = None,
        price_source: str | None = None,
        horizon: str | None = None,
        start: str | None = None,
        end: str | None = None,
        limit: int = 1000,
        only_missing: bool = False,
    ) -> dict[str, Any]:
        rows = self.db.replay_model_score_analysis_rows(
            event_type=event_type,
            symbols=symbols,
            price_source=price_source,
            horizon=horizon,
            scorer_name=self.base_scorer_name,
            model_name=self.base_model_name,
            scorer_version=self.base_scorer_version,
            start=start,
            end=end,
            limit=limit,
        )
        candidate_rows = len(rows)
        skipped_existing = 0
        if only_missing and hasattr(self.db, "existing_replay_model_score_outcome_ids"):
            existing = self.db.existing_replay_model_score_outcome_ids(
                outcome_ids=[int(row["outcome_id"]) for row in rows],
                scorer_name=self.base_scorer_name,
                model_name=str(self.base_model_name or (rows[0].get("model_name") if rows else "")),
                scorer_version=self.target_scorer_version,
            )
            if existing:
                rows = [row for row in rows if int(row["outcome_id"]) not in existing]
                skipped_existing = candidate_rows - len(rows)
        external_signals = []
        if hasattr(self.db, "recent_external_market_signals"):
            external_signals = self.db.recent_external_market_signals(limit=self.signal_limit)
        created = 0
        updated = 0
        hits = 0
        scored = 0
        method_counts: Counter[str] = Counter()
        observe_only = 0
        nuanced_action_counts: Counter[str] = Counter()
        nuanced_policy_entries = 0
        nuanced_policy_source = None
        nuanced_policy = None
        if self.target_scorer_version == OPENAI_REPLAY_SCORER_VERSION_V14_NUANCED_V1:
            nuanced_policy = self._load_nuanced_policy()
            nuanced_policy_entries = len(nuanced_policy)
            nuanced_policy_source = str(self.nuanced_policy_path or DEFAULT_NUANCED_ACTION_POLICY_PATH)
        for row in rows:
            evidence_packet = build_overlay_evidence_packet(row, external_signals, self.db)
            if self.target_scorer_version == OPENAI_REPLAY_SCORER_VERSION_V14_NUANCED_V1:
                score = transform_v14_nuanced_action_postscore(
                    row,
                    evidence_packet,
                    nuanced_policy or {},
                    target_scorer_version=self.target_scorer_version,
                    policy_source=nuanced_policy_source,
                )
            elif self.target_scorer_version == OPENAI_REPLAY_SCORER_VERSION_V14_OVERLAY_V3:
                score = transform_v14_overlay_postscore_v3(
                    row,
                    evidence_packet,
                    target_scorer_version=self.target_scorer_version,
                )
            elif self.target_scorer_version == OPENAI_REPLAY_SCORER_VERSION_V14_OVERLAY_V2:
                score = transform_v14_overlay_postscore_v2(
                    row,
                    evidence_packet,
                    target_scorer_version=self.target_scorer_version,
                )
            else:
                score = transform_v14_overlay_postscore(
                    row,
                    evidence_packet,
                    target_scorer_version=self.target_scorer_version,
                )
            _, was_created = self.db.upsert_replay_model_score(score)
            created += int(was_created)
            updated += int(not was_created)
            hits += int(score.direction_hit or 0)
            scored += 1
            try:
                raw = json.loads(score.raw_json)
            except json.JSONDecodeError:
                raw = {}
            debias = raw.get("debias") if isinstance(raw, dict) else {}
            if isinstance(debias, dict):
                method_counts[str(debias.get("method") or "unknown")] += 1
                observe_only += int(debias.get("observe_only") is True)
                nuanced_action_counts[str(debias.get("nuanced_action") or "none")] += 1
        return {
            "candidate_rows": candidate_rows,
            "skipped_existing": skipped_existing,
            "scored": scored,
            "created": created,
            "updated": updated,
            "direction_hits": hits,
            "direction_accuracy": (hits / scored) if scored else None,
            "base_scorer_name": self.base_scorer_name,
            "base_model_name": self.base_model_name,
            "base_scorer_version": self.base_scorer_version,
            "target_scorer_version": self.target_scorer_version,
            "method_counts": dict(method_counts),
            "observe_only_rows": observe_only,
            "nuanced_policy_source": nuanced_policy_source,
            "nuanced_policy_entries": nuanced_policy_entries,
            "nuanced_action_counts": dict(nuanced_action_counts),
            "openai_called": False,
        }


def _float_values(values: tuple[float, ...] | list[float] | None, default: tuple[float, ...]) -> tuple[float, ...]:
    cleaned = tuple(float(value) for value in (values or ()) if value is not None)
    return cleaned or default


def _base_trade_action(row: dict[str, Any]) -> tuple[str, bool, str, dict[str, Any]]:
    try:
        raw = json.loads(str(row.get("raw_json") or "{}"))
    except json.JSONDecodeError:
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    debias = raw.get("debias") if isinstance(raw.get("debias"), dict) else {}
    action = str(debias.get("trade_action") or "").strip() or (
        "observe_only" if debias.get("observe_only") is True else "directional_signal"
    )
    observe_only = bool(debias.get("observe_only")) or action == "observe_only"
    reason = str(debias.get("observe_only_reason") or debias.get("method") or "").strip()
    return action, observe_only, reason, debias


def _simulate_v14_overlay_threshold_gate(
    row: dict[str, Any],
    evidence_packet: dict[str, Any],
    *,
    overlay_score_min: float,
    conflict_score_min: float,
    confirmation_score_min: float,
    age_hours_max: float,
    confirmation_conflict_max: float,
    critical_only: bool,
) -> dict[str, Any]:
    base_prediction = _prediction_from_score_row(row)
    scoring_row = dict(row)
    scoring_row["scorer_evidence_packet"] = evidence_packet
    alignment = _tri_desk_alignment_for_prediction(row=scoring_row, prediction=base_prediction)
    emergency = _fresh_emergency_overlay_context(row=scoring_row, prediction=base_prediction)
    trade_action, observe_only, observe_only_reason, base_debias = _base_trade_action(row)
    method = "threshold_kept_base_gate"
    predicted_direction = str(base_prediction.predicted_direction or "").lower()
    conflict_score = float((alignment or {}).get("conflict_score") or 0.0)
    confirmation_score = float((alignment or {}).get("confirmation_score") or 0.0)
    emergency_age = emergency.get("age_hours")
    age_ok = emergency_age is not None and float(emergency_age) <= float(age_hours_max)
    urgency_ok = emergency["critical"] if critical_only else emergency["high_or_critical"]
    overlay_ok = float(emergency["overlay_score"] or 0.0) >= float(overlay_score_min)
    emergency_conflicts_up = (
        predicted_direction == "up"
        and emergency["relation"] == "emergency_conflicts_up"
        and age_ok
        and urgency_ok
        and overlay_ok
        and conflict_score >= float(conflict_score_min)
    )
    emergency_confirms_down = (
        predicted_direction == "down"
        and emergency["relation"] == "emergency_confirms_down"
        and age_ok
        and urgency_ok
        and overlay_ok
        and confirmation_score >= float(confirmation_score_min)
        and conflict_score <= float(confirmation_conflict_max)
    )
    if emergency_conflicts_up:
        method = "threshold_tri_emergency_second_pass"
        trade_action = "observe_only"
        observe_only = True
        observe_only_reason = "threshold_tri_emergency_second_pass"
    elif emergency_confirms_down:
        method = "threshold_emergency_down_confirmation_preserve"
        trade_action = "directional_signal"
        observe_only = False
        observe_only_reason = ""

    clone = dict(row)
    clone["raw_json"] = json.dumps(
        {
            "debias": {
                "applied": method != "threshold_kept_base_gate",
                "method": method,
                "base_debias": base_debias,
                "trade_action": trade_action,
                "observe_only": observe_only,
                "observe_only_reason": observe_only_reason,
                "tri_desk_alignment": alignment,
                "tri_desk_conflict_score": conflict_score,
                "tri_desk_confirmation_score": confirmation_score,
                "emergency_overlay": emergency,
                "thresholds": {
                    "overlay_score_min": overlay_score_min,
                    "conflict_score_min": conflict_score_min,
                    "confirmation_score_min": confirmation_score_min,
                    "age_hours_max": age_hours_max,
                    "confirmation_conflict_max": confirmation_conflict_max,
                    "critical_only": critical_only,
                },
            },
            "prompt_profile": "deterministic-v14-postscore-threshold-tuning",
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return clone


def postscore_threshold_tuning_summary(
    rows: list[dict[str, Any]],
    external_signals: list[dict[str, Any]],
    db: Any,
    *,
    overlay_score_grid: tuple[float, ...] | list[float] | None = None,
    conflict_score_grid: tuple[float, ...] | list[float] | None = None,
    confirmation_score_grid: tuple[float, ...] | list[float] | None = None,
    age_hours_grid: tuple[float, ...] | list[float] | None = None,
    confirmation_conflict_grid: tuple[float, ...] | list[float] | None = None,
    critical_only_grid: tuple[bool, ...] = (False, True),
    min_directional_coverage: float = 0.35,
    top_n: int = 20,
) -> dict[str, Any]:
    overlay_values = _float_values(overlay_score_grid, (4.5, 5.5, 6.5, 7.5))
    conflict_values = _float_values(conflict_score_grid, (0.40, 0.50, 0.67))
    confirmation_values = _float_values(confirmation_score_grid, (0.40, 0.50, 0.67))
    age_values = _float_values(age_hours_grid, (1.0, 4.0, 24.0))
    confirmation_conflict_values = _float_values(confirmation_conflict_grid, (0.25, 0.40))
    critical_values = tuple(bool(value) for value in critical_only_grid) or (False, True)
    evidence_rows = [
        {
            "row": row,
            "evidence_packet": build_overlay_evidence_packet(row, external_signals, db),
        }
        for row in rows
    ]
    base_summary = action_gate_summary(rows)
    base_all = base_summary["all_directional"]
    base_tradeable = base_summary["tradeable_directional"]
    candidate_summaries: list[dict[str, Any]] = []
    for overlay_min in overlay_values:
        for conflict_min in conflict_values:
            for confirmation_min in confirmation_values:
                for age_max in age_values:
                    for confirmation_conflict_max in confirmation_conflict_values:
                        for critical_only in critical_values:
                            simulated = [
                                _simulate_v14_overlay_threshold_gate(
                                    item["row"],
                                    item["evidence_packet"],
                                    overlay_score_min=overlay_min,
                                    conflict_score_min=conflict_min,
                                    confirmation_score_min=confirmation_min,
                                    age_hours_max=age_max,
                                    confirmation_conflict_max=confirmation_conflict_max,
                                    critical_only=critical_only,
                                )
                                for item in evidence_rows
                            ]
                            summary = action_gate_summary(simulated)
                            all_dir = summary["all_directional"]
                            tradeable = summary["tradeable_directional"]
                            observe_only = summary["observe_only_directional"]
                            coverage = summary["directional_coverage_after_gate"]
                            if coverage is None or coverage < float(min_directional_coverage):
                                continue
                            trade_acc = tradeable["directional_accuracy"]
                            all_acc = all_dir["directional_accuracy"]
                            obs_acc = observe_only["directional_accuracy"]
                            score = (
                                (trade_acc or 0.0)
                                - (all_acc or 0.0)
                                + 0.20 * ((all_acc or 0.0) - (obs_acc or 0.0))
                                + 0.03 * min(float(coverage or 0.0), 1.0)
                            )
                            method_counts = Counter()
                            for row in simulated:
                                try:
                                    raw = json.loads(str(row.get("raw_json") or "{}"))
                                except json.JSONDecodeError:
                                    raw = {}
                                debias = raw.get("debias") if isinstance(raw, dict) else {}
                                method_counts[str((debias or {}).get("method") or "unknown")] += 1
                            candidate_summaries.append(
                                {
                                    "params": {
                                        "overlay_score_min": overlay_min,
                                        "conflict_score_min": conflict_min,
                                        "confirmation_score_min": confirmation_min,
                                        "age_hours_max": age_max,
                                        "confirmation_conflict_max": confirmation_conflict_max,
                                        "critical_only": critical_only,
                                    },
                                    "score": score,
                                    "rows": summary["rows"],
                                    "all_directional": all_dir,
                                    "tradeable_directional": tradeable,
                                    "observe_only_directional": observe_only,
                                    "directional_coverage_after_gate": coverage,
                                    "directional_accuracy_delta": summary["directional_accuracy_delta"],
                                    "signed_excess_return_delta": summary["signed_excess_return_delta"],
                                    "method_counts": dict(method_counts),
                                }
                            )
    candidate_summaries.sort(
        key=lambda item: (
            -float(item["score"]),
            -float(item["tradeable_directional"]["directional_accuracy"] or 0.0),
            float(item["observe_only_directional"]["directional_accuracy"] or 1.0),
            -float(item["directional_coverage_after_gate"] or 0.0),
        )
    )
    return {
        "rows": len(rows),
        "grid_size": len(candidate_summaries),
        "base": {
            "all_directional": base_all,
            "tradeable_directional": base_tradeable,
            "directional_coverage_after_gate": base_summary["directional_coverage_after_gate"],
            "directional_accuracy_delta": base_summary["directional_accuracy_delta"],
        },
        "top_candidates": candidate_summaries[: max(1, int(top_n))],
        "all_candidates": candidate_summaries,
    }


def _threshold_param_text(params: dict[str, Any]) -> str:
    return (
        f"overlay>={params['overlay_score_min']}, conflict>={params['conflict_score_min']}, "
        f"confirm>={params['confirmation_score_min']}, age<={params['age_hours_max']}h, "
        f"confirm_conflict<={params['confirmation_conflict_max']}, "
        f"critical_only={params['critical_only']}"
    )


def postscore_threshold_tuning_markdown(
    summary: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
    title: str = "Replay Postscore Threshold Tuning Report",
) -> str:
    generated_at = datetime.now(UTC).isoformat()
    filters = filters or {}
    base = summary["base"]
    base_all = base["all_directional"]
    base_tradeable = base["tradeable_directional"]
    lines = [
        f"# {title}",
        "",
        f"- Generated at: `{generated_at}`",
        f"- Rows: `{summary['rows']}`",
        f"- Candidate grid rows kept: `{summary['grid_size']}`",
        f"- Base all directional accuracy: `{base_all['directional_hits']}/{base_all['directional_rows']} = {_fmt_optional_pct(base_all['directional_accuracy'])}`",
        f"- Base tradeable directional accuracy: `{base_tradeable['directional_hits']}/{base_tradeable['directional_rows']} = {_fmt_optional_pct(base_tradeable['directional_accuracy'])}`",
        "- Safety: read-only threshold tuning over stored replay scores/signals; no OpenAI, IBKR account, portfolio, or order action.",
        "",
        "## Filters",
        "",
    ]
    for key, value in filters.items():
        lines.append(f"- `{key}`: `{value if value is not None else '-'}`")
    lines.extend(
        [
            "",
            "## Top threshold candidates",
            "",
            "| Rank | Score | Params | Tradeable acc | Observe-only acc | All acc | Coverage | Acc delta | Signed excess delta | Method counts |",
            "| ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    if not summary["top_candidates"]:
        lines.append("| 1 | - | no candidate met minimum coverage | - | - | - | - | - | - | - |")
    for index, item in enumerate(summary["top_candidates"], start=1):
        tradeable = item["tradeable_directional"]
        observe_only = item["observe_only_directional"]
        all_dir = item["all_directional"]
        method_counts = ", ".join(f"{key}:{value}" for key, value in sorted(item["method_counts"].items()))
        lines.append(
            f"| {index} | {item['score']:.4f} | {_threshold_param_text(item['params'])} "
            f"| {tradeable['directional_hits']}/{tradeable['directional_rows']} = {_fmt_optional_pct(tradeable['directional_accuracy'])} "
            f"| {observe_only['directional_hits']}/{observe_only['directional_rows']} = {_fmt_optional_pct(observe_only['directional_accuracy'])} "
            f"| {all_dir['directional_hits']}/{all_dir['directional_rows']} = {_fmt_optional_pct(all_dir['directional_accuracy'])} "
            f"| {_fmt_optional_pct(item['directional_coverage_after_gate'])} "
            f"| {_fmt_optional_pct(item['directional_accuracy_delta'])} "
            f"| {_fmt_optional_signed_pct(item['signed_excess_return_delta'])} "
            f"| {method_counts} |"
        )
    lines.extend(
        [
            "",
            "## Interpretation note",
            "",
            "A deployable action gate should raise tradeable directional accuracy and/or signed excess return while pushing weaker rows into observe-only. If the best candidate still has tradeable accuracy below the all-directional baseline, the gate should remain research-only.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_postscore_threshold_tuning_markdown(
    output_path: Path,
    summary: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(postscore_threshold_tuning_markdown(summary, filters=filters), encoding="utf-8")
    return output_path


def _standing_family_relation(predicted: str, bias: str) -> str:
    predicted = predicted.strip().lower()
    bias = bias.strip().lower()
    if predicted == "up":
        if bias == "risk_on":
            return "confirm"
        if bias == "risk_off":
            return "conflict"
    if predicted == "down":
        if bias == "risk_off":
            return "confirm"
        if bias == "risk_on":
            return "conflict"
    if predicted in {"flat", "neutral"}:
        if bias in {"neutral", "mixed"}:
            return "confirm"
        if bias in {"risk_on", "risk_off"}:
            return "conflict"
    return "neutral"


def _standing_family_entries(row: dict[str, Any], packet: dict[str, Any], row_index: int) -> list[dict[str, Any]]:
    overlay = packet.get("standing_regime_overlay") or {}
    regimes = overlay.get("active_regimes") or []
    predicted = str(row.get("predicted_direction") or "").lower()
    conflict_score = float((packet.get("tri_desk_alignment") or {}).get("conflict_score") or 0.0)
    confirmation_score = float((packet.get("tri_desk_alignment") or {}).get("confirmation_score") or 0.0)
    entries = []
    for regime in regimes:
        family = str(regime.get("family") or "").strip()
        if not family:
            continue
        bias = str(regime.get("directional_bias") or "").strip().lower()
        entries.append(
            {
                "row_index": row_index,
                "family": family,
                "bias": bias,
                "status": str(regime.get("status") or ""),
                "severity_score": float(regime.get("severity_score") or 0.0),
                "confidence": float(regime.get("confidence") or 0.0),
                "relation": _standing_family_relation(predicted, bias),
                "tri_desk_conflict_score": conflict_score,
                "tri_desk_confirmation_score": confirmation_score,
                "row": row,
            }
        )
    return entries


def _standing_family_profile(entries: list[dict[str, Any]]) -> dict[str, Any]:
    rows = [entry["row"] for entry in entries]
    conflict_rows = [entry["row"] for entry in entries if entry["relation"] == "conflict"]
    confirm_rows = [entry["row"] for entry in entries if entry["relation"] == "confirm"]
    neutral_rows = [entry["row"] for entry in entries if entry["relation"] == "neutral"]
    relation_counts = Counter(str(entry["relation"]) for entry in entries)
    avg_severity = sum(float(entry["severity_score"]) for entry in entries) / len(entries) if entries else None
    avg_regime_confidence = sum(float(entry["confidence"]) for entry in entries) / len(entries) if entries else None
    avg_tri_conflict = (
        sum(float(entry["tri_desk_conflict_score"]) for entry in entries) / len(entries)
        if entries
        else None
    )
    return {
        "rows": len(entries),
        "all_directional": _action_gate_group_metrics(rows),
        "conflict_directional": _action_gate_group_metrics(conflict_rows),
        "confirm_directional": _action_gate_group_metrics(confirm_rows),
        "neutral_directional": _action_gate_group_metrics(neutral_rows),
        "relation_counts": dict(relation_counts),
        "avg_severity_score": avg_severity,
        "avg_regime_confidence": avg_regime_confidence,
        "avg_tri_desk_conflict_score": avg_tri_conflict,
    }


def standing_regime_family_gate_tuning_summary(
    rows: list[dict[str, Any]],
    external_signals: list[dict[str, Any]],
    db: Any,
    *,
    conflict_score_grid: tuple[float, ...] | list[float] | None = None,
    min_family_rows: int = 5,
    min_directional_coverage: float = 0.35,
    top_n: int = 20,
) -> dict[str, Any]:
    conflict_values = _float_values(conflict_score_grid, (0.40, 0.50, 0.67))
    family_entries: dict[str, list[dict[str, Any]]] = {}
    packet_count = 0
    for index, row in enumerate(rows):
        packet = build_overlay_evidence_packet(row, external_signals, db)
        packet_count += 1
        for entry in _standing_family_entries(row, packet, index):
            family_entries.setdefault(entry["family"], []).append(entry)

    family_profiles = []
    candidate_gates = []
    for family, entries in family_entries.items():
        if len(entries) < int(min_family_rows):
            continue
        profile = {"family": family, **_standing_family_profile(entries)}
        family_profiles.append(profile)
        all_rows = [entry["row"] for entry in entries]
        all_metrics = profile["all_directional"]
        all_accuracy = all_metrics["directional_accuracy"]
        for conflict_min in conflict_values:
            observe_entries = [
                entry
                for entry in entries
                if entry["relation"] == "conflict"
                and float(entry["tri_desk_conflict_score"]) >= float(conflict_min)
            ]
            if not observe_entries:
                continue
            observe_indexes = {int(entry["row_index"]) for entry in observe_entries}
            tradeable_rows = [
                entry["row"]
                for entry in entries
                if int(entry["row_index"]) not in observe_indexes
            ]
            observe_rows = [entry["row"] for entry in observe_entries]
            tradeable_metrics = _action_gate_group_metrics(tradeable_rows)
            observe_metrics = _action_gate_group_metrics(observe_rows)
            if observe_metrics["directional_rows"] == 0:
                continue
            coverage = (
                tradeable_metrics["directional_rows"] / all_metrics["directional_rows"]
                if all_metrics["directional_rows"]
                else None
            )
            if coverage is None or coverage < float(min_directional_coverage):
                continue
            trade_acc = tradeable_metrics["directional_accuracy"]
            observe_acc = observe_metrics["directional_accuracy"]
            score = (
                (trade_acc or 0.0)
                - (all_accuracy or 0.0)
                + 0.25 * ((all_accuracy or 0.0) - (observe_acc or 0.0))
                + 0.03 * min(float(coverage or 0.0), 1.0)
            )
            candidate_gates.append(
                {
                    "family": family,
                    "params": {
                        "relation": "conflict",
                        "tri_desk_conflict_score_min": float(conflict_min),
                    },
                    "score": score,
                    "rows": len(entries),
                    "all_directional": all_metrics,
                    "tradeable_directional": tradeable_metrics,
                    "observe_only_directional": observe_metrics,
                    "directional_coverage_after_gate": coverage,
                    "directional_accuracy_delta": (
                        trade_acc - all_accuracy
                        if trade_acc is not None and all_accuracy is not None
                        else None
                    ),
                    "signed_excess_return_delta": (
                        tradeable_metrics["avg_signed_excess_return"] - all_metrics["avg_signed_excess_return"]
                        if tradeable_metrics["avg_signed_excess_return"] is not None
                        and all_metrics["avg_signed_excess_return"] is not None
                        else None
                    ),
                }
            )

    family_profiles.sort(
        key=lambda item: (
            -int(item["all_directional"]["directional_rows"]),
            -float(item["avg_tri_desk_conflict_score"] or 0.0),
            item["family"],
        )
    )
    candidate_gates.sort(
        key=lambda item: (
            -float(item["score"]),
            -float(item["tradeable_directional"]["directional_accuracy"] or 0.0),
            float(item["observe_only_directional"]["directional_accuracy"] or 1.0),
            -float(item["directional_coverage_after_gate"] or 0.0),
            item["family"],
        )
    )
    return {
        "rows": len(rows),
        "packets_built": packet_count,
        "signals": len(external_signals),
        "families_considered": len(family_profiles),
        "candidate_gate_count": len(candidate_gates),
        "min_family_rows": int(min_family_rows),
        "min_directional_coverage": float(min_directional_coverage),
        "conflict_score_grid": list(conflict_values),
        "family_profiles": family_profiles,
        "top_candidates": candidate_gates[: max(1, int(top_n))],
        "all_candidates": candidate_gates,
    }


def _standing_family_gate_param_text(params: dict[str, Any]) -> str:
    return f"{params.get('relation')} and tri_conflict>={float(params.get('tri_desk_conflict_score_min') or 0.0):.2f}"


def standing_regime_family_gate_tuning_markdown(
    summary: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
    title: str = "Standing Regime Family Action-Gate Tuning Report",
) -> str:
    filters = filters or {}
    lines = [
        f"# {title}",
        "",
        f"- Generated at: `{datetime.now(UTC).isoformat()}`",
        f"- Rows: `{summary['rows']}`",
        f"- Evidence packets built: `{summary['packets_built']}`",
        f"- External signals available: `{summary['signals']}`",
        f"- Families considered: `{summary['families_considered']}`",
        f"- Candidate gates kept: `{summary['candidate_gate_count']}`",
        f"- Conflict-score grid: `{', '.join(str(item) for item in summary.get('conflict_score_grid') or [])}`",
        "- Gate simulated: mark rows observe-only when a standing-regime family conflicts with the stored direction and tri-desk conflict exceeds the threshold.",
        "- Safety: read-only tuning over stored replay scores/signals; no OpenAI, IBKR account, portfolio, order action, or scorer mutation.",
        "",
        "## Filters",
        "",
    ]
    if filters:
        for key, value in filters.items():
            lines.append(f"- `{key}`: `{value if value is not None else '-'}`")
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "## Top family gate candidates",
            "",
            "| Rank | Family | Score | Params | Tradeable acc | Observe-only acc | All acc | Coverage | Acc delta | Signed excess delta |",
            "| ---: | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    if not summary["top_candidates"]:
        lines.append("| 1 | - | - | no candidate met filters | - | - | - | - | - | - |")
    for index, item in enumerate(summary["top_candidates"], start=1):
        tradeable = item["tradeable_directional"]
        observe_only = item["observe_only_directional"]
        all_dir = item["all_directional"]
        lines.append(
            f"| {index} | {item['family']} | {float(item['score']):.4f} | {_standing_family_gate_param_text(item['params'])} "
            f"| {tradeable['directional_hits']}/{tradeable['directional_rows']} = {_fmt_optional_pct(tradeable['directional_accuracy'])} "
            f"| {observe_only['directional_hits']}/{observe_only['directional_rows']} = {_fmt_optional_pct(observe_only['directional_accuracy'])} "
            f"| {all_dir['directional_hits']}/{all_dir['directional_rows']} = {_fmt_optional_pct(all_dir['directional_accuracy'])} "
            f"| {_fmt_optional_pct(item['directional_coverage_after_gate'])} "
            f"| {_fmt_optional_pct(item['directional_accuracy_delta'])} "
            f"| {_fmt_optional_signed_pct(item['signed_excess_return_delta'])} |"
        )
    lines.extend(
        [
            "",
            "## Family profiles",
            "",
            "| Family | Rows | Relations | All acc | Conflict acc | Confirm acc | Avg severity | Avg regime conf | Avg tri conflict |",
            "| --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    if not summary["family_profiles"]:
        lines.append("| - | 0 | - | - | - | - | - | - | - |")
    for item in summary["family_profiles"]:
        relations = ", ".join(f"{key}:{value}" for key, value in sorted((item.get("relation_counts") or {}).items()))
        all_dir = item["all_directional"]
        conflict = item["conflict_directional"]
        confirm = item["confirm_directional"]
        lines.append(
            f"| {item['family']} | {item['rows']} | {relations or '-'} "
            f"| {all_dir['directional_hits']}/{all_dir['directional_rows']} = {_fmt_optional_pct(all_dir['directional_accuracy'])} "
            f"| {conflict['directional_hits']}/{conflict['directional_rows']} = {_fmt_optional_pct(conflict['directional_accuracy'])} "
            f"| {confirm['directional_hits']}/{confirm['directional_rows']} = {_fmt_optional_pct(confirm['directional_accuracy'])} "
            f"| {float(item['avg_severity_score'] or 0.0):.2f} "
            f"| {float(item['avg_regime_confidence'] or 0.0):.2f} "
            f"| {float(item['avg_tri_desk_conflict_score'] or 0.0):.2f} |"
        )
    lines.extend(
        [
            "",
            "## Interpretation note",
            "",
            "Promote a family gate only when it improves tradeable accuracy or signed excess return and the observe-only bucket is materially weaker. Small samples should remain research-only until more replay rows accumulate.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_standing_regime_family_gate_tuning_markdown(
    output_path: Path,
    summary: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        standing_regime_family_gate_tuning_markdown(summary, filters=filters),
        encoding="utf-8",
    )
    return output_path


def _debiased_openai_prediction(
    prediction: OpenAIReplayPrediction,
    row: dict[str, Any],
) -> tuple[OpenAIReplayPrediction, dict[str, Any]]:
    """Reduce chronic flat bias without using realized outcomes.

    The adjustment uses only the model's own up/down probabilities, expected
    return, and the requested horizon. It is intentionally conservative and
    applies only when the model selected flat while still assigning material
    probability mass to a directional class.
    """

    if prediction.predicted_direction != "flat":
        return prediction, {"applied": False, "reason": "model_selected_directional_class"}

    horizon = str(row.get("horizon") or "").upper()
    if horizon not in {"30M", "2H", "1D", "5D"}:
        return prediction, {"applied": False, "reason": "unsupported_horizon"}

    directional = {
        "up": float(prediction.up_probability),
        "down": float(prediction.down_probability),
    }
    chosen_direction = "up" if directional["up"] >= directional["down"] else "down"
    chosen_probability = directional[chosen_direction]
    other_direction = "down" if chosen_direction == "up" else "up"
    other_probability = directional[other_direction]
    directional_edge = abs(chosen_probability - other_probability)

    if chosen_probability < 0.28 and abs(prediction.expected_return) < 0.0015:
        return prediction, {
            "applied": False,
            "reason": "directional_probability_too_low",
            "chosen_direction": chosen_direction,
            "chosen_probability": chosen_probability,
            "directional_edge": directional_edge,
        }

    horizon_floor = {
        "30M": 0.0020,
        "2H": 0.0030,
        "1D": 0.0040,
        "5D": 0.0060,
    }.get(horizon, 0.0040)
    sign = 1.0 if chosen_direction == "up" else -1.0
    expected = float(prediction.expected_return)
    if abs(expected) < horizon_floor or (expected > 0) != (sign > 0):
        expected = sign * horizon_floor

    width = max(0.018, abs(expected) * 3.0)
    if chosen_direction == "up":
        up_probability = _clamp(max(prediction.up_probability, 0.42 + directional_edge * 0.5), 0.34, 0.55)
        down_probability = _clamp(min(prediction.down_probability, 0.27 - directional_edge * 0.2), 0.15, 0.33)
    else:
        down_probability = _clamp(max(prediction.down_probability, 0.42 + directional_edge * 0.5), 0.34, 0.55)
        up_probability = _clamp(min(prediction.up_probability, 0.27 - directional_edge * 0.2), 0.15, 0.33)
    flat_probability = max(0.10, 1.0 - up_probability - down_probability)
    total = up_probability + flat_probability + down_probability
    up_probability /= total
    flat_probability /= total
    down_probability /= total

    confidence = _clamp(
        0.26 + directional_edge * 1.5 + min(0.12, abs(expected) * 8.0),
        0.25,
        0.48,
    )
    debiased = prediction.model_copy(
        update={
            "predicted_direction": chosen_direction,
            "expected_return": expected,
            "bear_return": min(expected - width, expected),
            "bull_return": max(expected + width, expected),
            "up_probability": up_probability,
            "flat_probability": flat_probability,
            "down_probability": down_probability,
            "confidence": confidence,
            "rationale": (
                prediction.rationale
                + " Debias note: original model selected flat, but its directional "
                + f"probability mass favored {chosen_direction}; v3 reassigned a modest "
                + "directional forecast without using outcome labels."
            )[:800],
        }
    )
    debiased.validate_probability_mass()
    return debiased, {
        "applied": True,
        "method": "flat_probability_reassignment_v1",
        "original_predicted_direction": prediction.predicted_direction,
        "chosen_direction": chosen_direction,
        "chosen_probability": chosen_probability,
        "other_probability": other_probability,
        "original_flat_probability": prediction.flat_probability,
        "directional_edge": directional_edge,
        "horizon_floor": horizon_floor,
    }


def _intraday_tuned_openai_prediction(
    prediction: OpenAIReplayPrediction,
    row: dict[str, Any],
) -> tuple[OpenAIReplayPrediction, dict[str, Any]]:
    """Tune very short-horizon OpenAI scores without using outcome labels.

    Intraday rows are noisier than daily rows. The v3 daily debiaser often
    converted weak flat outputs into directional calls from tiny up/down
    probability differences. For 30M/2H rows, v4 instead uses the model's own
    expected-return sign as the tiebreaker and keeps confidence deliberately
    low unless the model supplied a stronger directional edge.
    """

    horizon = str(row.get("horizon") or "").upper()
    if horizon not in {"30M", "2H"}:
        return _debiased_openai_prediction(prediction, row)

    if prediction.predicted_direction != "flat":
        probabilities = {
            "up": float(prediction.up_probability),
            "down": float(prediction.down_probability),
        }
        selected_probability = probabilities[prediction.predicted_direction]
        opposite_probability = probabilities["down" if prediction.predicted_direction == "up" else "up"]
        edge = selected_probability - opposite_probability
        if abs(float(prediction.expected_return)) < 0.00015 and edge < 0.04:
            flat_probability = max(0.42, float(prediction.flat_probability))
            directional_total = max(0.0, 1.0 - flat_probability)
            up_probability = directional_total / 2.0
            down_probability = directional_total / 2.0
            tuned = prediction.model_copy(
                update={
                    "predicted_direction": "flat",
                    "expected_return": 0.0,
                    "bear_return": min(float(prediction.bear_return), -0.0040),
                    "bull_return": max(float(prediction.bull_return), 0.0040),
                    "up_probability": up_probability,
                    "flat_probability": flat_probability,
                    "down_probability": down_probability,
                    "confidence": _clamp(float(prediction.confidence), 0.22, 0.34),
                    "rationale": (
                        prediction.rationale
                        + " Intraday v4 note: directional signal was too small for a short-horizon call, "
                        + "so the score was kept flat without using outcome labels."
                    )[:800],
                }
            )
            tuned.validate_probability_mass()
            return tuned, {
                "applied": True,
                "method": "intraday_low_edge_direction_to_flat_v1",
                "original_predicted_direction": prediction.predicted_direction,
                "selected_probability": selected_probability,
                "opposite_probability": opposite_probability,
                "directional_edge": edge,
            }
        return prediction, {
            "applied": False,
            "reason": "intraday_model_selected_directional_class",
            "selected_probability": selected_probability,
            "opposite_probability": opposite_probability,
            "directional_edge": edge,
        }

    expected = float(prediction.expected_return)
    probabilities = {
        "up": float(prediction.up_probability),
        "down": float(prediction.down_probability),
    }
    probability_direction = "up" if probabilities["up"] >= probabilities["down"] else "down"
    probability_edge = abs(probabilities["up"] - probabilities["down"])

    if abs(expected) >= 0.00015:
        chosen_direction = "up" if expected > 0 else "down"
        method = "intraday_expected_return_tiebreak_v1"
    elif probability_edge >= 0.08 and max(probabilities.values()) >= 0.36:
        chosen_direction = probability_direction
        method = "intraday_probability_edge_reassignment_v1"
    else:
        return prediction, {
            "applied": False,
            "reason": "intraday_flat_signal_too_small",
            "probability_direction": probability_direction,
            "directional_edge": probability_edge,
        }

    sign = 1.0 if chosen_direction == "up" else -1.0
    horizon_floor = {"30M": 0.0012, "2H": 0.0018}[horizon]
    tuned_expected = expected if (expected > 0) == (sign > 0) and abs(expected) >= horizon_floor else sign * horizon_floor
    other_direction = "down" if chosen_direction == "up" else "up"
    selected_probability = probabilities[chosen_direction]
    opposite_probability = probabilities[other_direction]
    directional_edge = abs(selected_probability - opposite_probability)

    chosen_probability = _clamp(0.37 + min(0.08, directional_edge), 0.34, 0.48)
    flat_probability = _clamp(0.35 - min(0.05, directional_edge * 0.5), 0.25, 0.42)
    other_probability = max(0.10, 1.0 - chosen_probability - flat_probability)
    total = chosen_probability + flat_probability + other_probability
    chosen_probability /= total
    flat_probability /= total
    other_probability /= total
    if chosen_direction == "up":
        up_probability = chosen_probability
        down_probability = other_probability
    else:
        up_probability = other_probability
        down_probability = chosen_probability

    width = max(0.008, abs(tuned_expected) * 4.0)
    confidence = _clamp(0.27 + min(0.10, directional_edge * 1.5) + min(0.04, abs(expected) * 40.0), 0.24, 0.42)
    tuned = prediction.model_copy(
        update={
            "predicted_direction": chosen_direction,
            "expected_return": tuned_expected,
            "bear_return": min(tuned_expected - width, tuned_expected),
            "bull_return": max(tuned_expected + width, tuned_expected),
            "up_probability": up_probability,
            "flat_probability": flat_probability,
            "down_probability": down_probability,
            "confidence": confidence,
            "rationale": (
                prediction.rationale
                + " Intraday v4 note: original flat output had a non-zero expected-return sign, "
                + f"so a low-confidence {chosen_direction} forecast was assigned without using outcome labels."
            )[:800],
        }
    )
    tuned.validate_probability_mass()
    return tuned, {
        "applied": True,
        "method": method,
        "original_predicted_direction": prediction.predicted_direction,
        "chosen_direction": chosen_direction,
        "expected_return_before": expected,
        "probability_direction": probability_direction,
        "chosen_probability_before": selected_probability,
        "other_probability_before": opposite_probability,
        "directional_edge": directional_edge,
        "horizon_floor": horizon_floor,
    }


def _intraday_balanced_openai_prediction(
    prediction: OpenAIReplayPrediction,
    row: dict[str, Any],
) -> tuple[OpenAIReplayPrediction, dict[str, Any]]:
    """Balance short-horizon OpenAI outputs against intraday sample bias.

    v5 keeps the non-leaking boundary from v4, but low-conviction 30M/2H
    outputs may be calibrated by the replay row's market session and horizon.
    It does not inspect realized returns, labels, future prices, or later news.
    """

    horizon = str(row.get("horizon") or "").upper()
    if horizon not in {"30M", "2H"}:
        return _debiased_openai_prediction(prediction, row)

    probabilities = {
        "up": float(prediction.up_probability),
        "down": float(prediction.down_probability),
    }
    probability_direction = "up" if probabilities["up"] >= probabilities["down"] else "down"
    probability_edge = abs(probabilities["up"] - probabilities["down"])
    flat_probability = float(prediction.flat_probability)
    expected = float(prediction.expected_return)
    session = str(row.get("session") or "").lower()
    flat_floor = {"30M": 0.42, "2H": 0.44}[horizon]
    edge_floor = 0.03
    expected_floor = 0.00015

    def _prior_direction() -> str:
        if session == "regular":
            return "flat"
        if horizon == "2H":
            return "down"
        return "up"

    def _prior_prediction(reason: str) -> tuple[OpenAIReplayPrediction, dict[str, Any]]:
        prior_direction = _prior_direction()
        if prior_direction == "flat":
            return _flat_prediction(reason)
        sign = 1.0 if prior_direction == "up" else -1.0
        horizon_floor = {"30M": 0.0012, "2H": 0.0018}[horizon]
        tuned_expected = sign * max(horizon_floor, min(0.0030, abs(expected) if abs(expected) >= horizon_floor else horizon_floor))
        chosen_probability = 0.39
        retained_flat_probability = 0.34
        other_probability = 0.27
        if prior_direction == "up":
            up_probability = chosen_probability
            down_probability = other_probability
        else:
            up_probability = other_probability
            down_probability = chosen_probability
        width = max(0.008, abs(tuned_expected) * 4.0)
        tuned = prediction.model_copy(
            update={
                "predicted_direction": prior_direction,
                "expected_return": tuned_expected,
                "bear_return": min(tuned_expected - width, tuned_expected),
                "bull_return": max(tuned_expected + width, tuned_expected),
                "up_probability": up_probability,
                "flat_probability": retained_flat_probability,
                "down_probability": down_probability,
                "confidence": _clamp(float(prediction.confidence), 0.24, 0.35),
                "rationale": (
                    prediction.rationale
                    + " Intraday v5 note: low-conviction model output was calibrated with a "
                    + f"session/horizon prior ({session or 'unknown'} {horizon}) without using outcome labels."
                )[:800],
            }
        )
        tuned.validate_probability_mass()
        return tuned, {
            "applied": True,
            "method": "intraday_session_horizon_prior_v1",
            "reason": reason,
            "original_predicted_direction": prediction.predicted_direction,
            "chosen_direction": prior_direction,
            "session": session,
            "horizon": horizon,
            "flat_probability": flat_probability,
            "flat_floor": flat_floor,
            "probability_direction": probability_direction,
            "directional_edge": probability_edge,
            "expected_return_before": expected,
            "horizon_floor": horizon_floor,
        }

    def _flat_prediction(reason: str) -> tuple[OpenAIReplayPrediction, dict[str, Any]]:
        tuned_flat_probability = max(flat_floor, flat_probability)
        directional_total = max(0.0, 1.0 - tuned_flat_probability)
        total_directional = probabilities["up"] + probabilities["down"]
        if total_directional > 0:
            up_probability = directional_total * probabilities["up"] / total_directional
            down_probability = directional_total * probabilities["down"] / total_directional
        else:
            up_probability = directional_total / 2.0
            down_probability = directional_total / 2.0
        total = up_probability + tuned_flat_probability + down_probability
        up_probability /= total
        tuned_flat_probability /= total
        down_probability /= total
        width = max(0.006, abs(expected) * 3.0)
        tuned = prediction.model_copy(
            update={
                "predicted_direction": "flat",
                "expected_return": 0.0 if abs(expected) < 0.002 else expected * 0.35,
                "bear_return": min(-width, float(prediction.bear_return)),
                "bull_return": max(width, float(prediction.bull_return)),
                "up_probability": up_probability,
                "flat_probability": tuned_flat_probability,
                "down_probability": down_probability,
                "confidence": _clamp(float(prediction.confidence), 0.22, 0.36),
                "rationale": (
                    prediction.rationale
                    + " Intraday v5 note: short-horizon evidence was not strong enough to "
                    + "override the raw flat signal, so the forecast was kept flat without outcome labels."
                )[:800],
            }
        )
        tuned.validate_probability_mass()
        return tuned, {
            "applied": True,
            "method": "intraday_balanced_flat_retention_v1",
            "reason": reason,
            "original_predicted_direction": prediction.predicted_direction,
            "flat_probability": flat_probability,
            "flat_floor": flat_floor,
            "probability_direction": probability_direction,
            "directional_edge": probability_edge,
            "expected_return_before": expected,
        }

    if prediction.predicted_direction == "flat":
        if (
            flat_probability >= flat_floor
            or probability_edge < edge_floor
            or abs(expected) < expected_floor
        ):
            return _prior_prediction("raw_flat_signal_with_insufficient_intraday_edge")
        chosen_direction = "up" if expected > 0 else "down" if expected < 0 else probability_direction
        method = "intraday_balanced_expected_return_tiebreak_v1"
    else:
        chosen_direction = prediction.predicted_direction
        selected_probability = probabilities[chosen_direction]
        opposite_probability = probabilities["down" if chosen_direction == "up" else "up"]
        if flat_probability >= flat_floor and (selected_probability - opposite_probability) < edge_floor:
            return _prior_prediction("directional_output_with_high_flat_probability")
        method = "intraday_balanced_model_direction_v1"

    sign = 1.0 if chosen_direction == "up" else -1.0
    horizon_floor = {"30M": 0.0012, "2H": 0.0018}[horizon]
    tuned_expected = expected if (expected > 0) == (sign > 0) and abs(expected) >= horizon_floor else sign * horizon_floor
    other_direction = "down" if chosen_direction == "up" else "up"
    selected_probability = probabilities[chosen_direction]
    opposite_probability = probabilities[other_direction]
    directional_edge = abs(selected_probability - opposite_probability)

    chosen_probability = _clamp(0.37 + min(0.07, directional_edge), 0.34, 0.47)
    retained_flat_probability = _clamp(flat_probability, 0.30, 0.43)
    other_probability = max(0.10, 1.0 - chosen_probability - retained_flat_probability)
    total = chosen_probability + retained_flat_probability + other_probability
    chosen_probability /= total
    retained_flat_probability /= total
    other_probability /= total
    if chosen_direction == "up":
        up_probability = chosen_probability
        down_probability = other_probability
    else:
        up_probability = other_probability
        down_probability = chosen_probability

    width = max(0.008, abs(tuned_expected) * 4.0)
    confidence = _clamp(0.26 + min(0.08, directional_edge * 1.2) + min(0.03, abs(expected) * 35.0), 0.23, 0.40)
    tuned = prediction.model_copy(
        update={
            "predicted_direction": chosen_direction,
            "expected_return": tuned_expected,
            "bear_return": min(tuned_expected - width, tuned_expected),
            "bull_return": max(tuned_expected + width, tuned_expected),
            "up_probability": up_probability,
            "flat_probability": retained_flat_probability,
            "down_probability": down_probability,
            "confidence": confidence,
            "rationale": (
                prediction.rationale
                + " Intraday v5 note: raw probabilities cleared the horizon-specific "
                + f"edge threshold, so a low-confidence {chosen_direction} forecast was assigned without outcome labels."
            )[:800],
        }
    )
    tuned.validate_probability_mass()
    return tuned, {
        "applied": True,
        "method": method,
        "original_predicted_direction": prediction.predicted_direction,
        "chosen_direction": chosen_direction,
        "flat_probability": flat_probability,
        "flat_floor": flat_floor,
        "expected_return_before": expected,
        "probability_direction": probability_direction,
        "chosen_probability_before": selected_probability,
        "other_probability_before": opposite_probability,
        "directional_edge": directional_edge,
        "horizon_floor": horizon_floor,
    }


def _intraday_upbias_reduced_openai_prediction(
    prediction: OpenAIReplayPrediction,
    row: dict[str, Any],
) -> tuple[OpenAIReplayPrediction, dict[str, Any]]:
    """Reduce low-conviction intraday up-bias without using outcome labels.

    M26 showed that the fresh-symbol v5 misses were dominated by weak up calls
    against actual down/flat reactions, while the largest misses still included
    some actual-up cases. v6 therefore does not simply become bearish. It keeps
    v5's non-leaking calibration, then restores a flat bucket for weak 30M/2H
    up calls whose raw probabilities/expected return do not show a stronger
    directional edge.
    """

    horizon = str(row.get("horizon") or "").upper()
    if horizon not in {"30M", "2H"}:
        return _debiased_openai_prediction(prediction, row)

    v5_prediction, v5_metadata = _intraday_balanced_openai_prediction(prediction, row)
    if v5_prediction.predicted_direction != "up":
        return v5_prediction, {
            "applied": bool(v5_metadata.get("applied")),
            "method": "intraday_v6_kept_non_up_v5_output",
            "v5": v5_metadata,
            "chosen_direction": v5_prediction.predicted_direction,
        }

    up_probability = float(prediction.up_probability)
    down_probability = float(prediction.down_probability)
    flat_probability = float(prediction.flat_probability)
    expected = float(prediction.expected_return)
    session = str(row.get("session") or "").lower()
    event_type = str(row.get("event_type") or "").lower()
    event_subtype = str(row.get("event_subtype") or "").lower()
    up_edge = up_probability - down_probability
    abs_expected = abs(expected)
    v5_method = str(v5_metadata.get("method") or "")
    v5_was_weak_up_prior = (
        v5_method == "intraday_session_horizon_prior_v1"
        and str(v5_metadata.get("chosen_direction") or "") == "up"
    )
    guidance_like = event_type == "guidance" or "guidance" in event_subtype

    # Restore flat only when the raw model signal has a clearly dominant flat
    # bucket and nearly zero expected return. The first v6 shadow run showed
    # that a broad weak-up rule over-flattened 30M reactions, so v6 keeps v5's
    # directional call unless the raw no-move evidence is unusually explicit.
    weak_up_to_flat = (
        flat_probability >= 0.44
        and flat_probability >= max(up_probability, down_probability) + 0.08
        and abs_expected < 0.0010
        and up_edge < 0.040
    )
    if not weak_up_to_flat:
        return v5_prediction, {
            "applied": bool(v5_metadata.get("applied")),
            "method": "intraday_v6_kept_supported_up_v5_output",
            "v5": v5_metadata,
            "up_probability": up_probability,
            "down_probability": down_probability,
            "flat_probability": flat_probability,
            "up_edge": up_edge,
            "expected_return_before": expected,
        }

    tuned_flat_probability = max(0.44 if horizon == "2H" else 0.42, flat_probability, 0.46)
    directional_total = max(0.0, 1.0 - tuned_flat_probability)
    total_directional = max(0.000001, up_probability + down_probability)
    tuned_up_probability = directional_total * up_probability / total_directional
    tuned_down_probability = directional_total * down_probability / total_directional
    total = tuned_up_probability + tuned_flat_probability + tuned_down_probability
    tuned_up_probability /= total
    tuned_flat_probability /= total
    tuned_down_probability /= total
    width = max(0.006, abs_expected * 3.0, abs(float(v5_prediction.expected_return)) * 2.0)
    tuned = v5_prediction.model_copy(
        update={
            "predicted_direction": "flat",
            "expected_return": 0.0 if abs_expected < 0.004 else expected * 0.25,
            "bear_return": min(-width, float(v5_prediction.bear_return)),
            "bull_return": max(width, float(v5_prediction.bull_return)),
            "up_probability": tuned_up_probability,
            "flat_probability": tuned_flat_probability,
            "down_probability": tuned_down_probability,
            "confidence": _clamp(float(v5_prediction.confidence), 0.22, 0.36),
            "rationale": (
                v5_prediction.rationale
                + " Intraday v6 note: weak up evidence was not strong enough after the M26 "
                + "fresh-symbol error review, so the short-horizon forecast was restored to flat "
                + "without using outcome labels."
            )[:800],
        }
    )
    tuned.validate_probability_mass()
    return tuned, {
        "applied": True,
        "method": "intraday_v6_weak_up_to_flat_v1",
        "v5": v5_metadata,
        "original_predicted_direction": prediction.predicted_direction,
        "v5_predicted_direction": v5_prediction.predicted_direction,
        "chosen_direction": "flat",
        "session": session,
        "horizon": horizon,
        "event_type": event_type,
        "event_subtype": event_subtype,
        "up_probability": up_probability,
        "down_probability": down_probability,
        "flat_probability": flat_probability,
        "up_edge": up_edge,
        "expected_return_before": expected,
        "v5_was_weak_up_prior": v5_was_weak_up_prior,
    }


def _intraday_up_recovery_openai_prediction(
    prediction: OpenAIReplayPrediction,
    row: dict[str, Any],
) -> tuple[OpenAIReplayPrediction, dict[str, Any]]:
    """Recover selected raw-flat positive intraday up signals after v6.

    M28 paired comparison showed v6's useful improvement came from reducing
    false up calls on actual-down rows, but it still lost several actual-up
    reactions when a session prior turned raw flat/positive evidence into down
    or flat. v7 therefore starts from v6 and only restores up when the raw
    model output is flat, expected return is positive, and the raw up-vs-down
    probability edge clears a narrow horizon/session-specific threshold.
    """

    horizon = str(row.get("horizon") or "").upper()
    if horizon not in {"30M", "2H"}:
        return _debiased_openai_prediction(prediction, row)

    v6_prediction, v6_metadata = _intraday_upbias_reduced_openai_prediction(prediction, row)
    if v6_prediction.predicted_direction == "up":
        return v6_prediction, {
            "applied": bool(v6_metadata.get("applied")),
            "method": "intraday_v7_kept_v6_up_output",
            "v6": v6_metadata,
            "chosen_direction": "up",
        }

    raw_predicted_direction = str(prediction.predicted_direction)
    expected = float(prediction.expected_return)
    up_probability = float(prediction.up_probability)
    down_probability = float(prediction.down_probability)
    flat_probability = float(prediction.flat_probability)
    up_edge = up_probability - down_probability
    session = str(row.get("session") or "").lower()
    v5_metadata = v6_metadata.get("v5") if isinstance(v6_metadata.get("v5"), dict) else {}
    v5_method = str(v5_metadata.get("method") or "")
    v6_method = str(v6_metadata.get("method") or "")

    after_hours_2h_recovery = (
        session == "after_hours"
        and horizon == "2H"
        and v6_method == "intraday_v6_kept_non_up_v5_output"
        and v5_method == "intraday_session_horizon_prior_v1"
        and raw_predicted_direction == "flat"
        and expected > 0.00015
        and up_edge > 0.014
    )
    regular_30m_recovery = (
        session == "regular"
        and horizon == "30M"
        and v6_method == "intraday_v6_kept_non_up_v5_output"
        and v5_method == "intraday_balanced_flat_retention_v1"
        and raw_predicted_direction == "flat"
        and expected > 0.00015
        and up_edge >= 0.019
    )

    if not (after_hours_2h_recovery or regular_30m_recovery):
        return v6_prediction, {
            "applied": bool(v6_metadata.get("applied")),
            "method": "intraday_v7_kept_v6_non_up_output",
            "v6": v6_metadata,
            "chosen_direction": v6_prediction.predicted_direction,
            "up_probability": up_probability,
            "down_probability": down_probability,
            "flat_probability": flat_probability,
            "up_edge": up_edge,
            "expected_return_before": expected,
        }

    horizon_floor = {"30M": 0.0012, "2H": 0.0018}[horizon]
    tuned_expected = max(horizon_floor, min(0.0030, abs(expected) if abs(expected) >= horizon_floor else horizon_floor))
    chosen_probability = _clamp(0.38 + min(0.05, up_edge), 0.38, 0.44)
    retained_flat_probability = _clamp(flat_probability, 0.32, 0.43)
    down_probability_tuned = max(0.10, 1.0 - chosen_probability - retained_flat_probability)
    total = chosen_probability + retained_flat_probability + down_probability_tuned
    tuned_up_probability = chosen_probability / total
    tuned_flat_probability = retained_flat_probability / total
    tuned_down_probability = down_probability_tuned / total
    width = max(0.008, tuned_expected * 4.0)
    tuned = v6_prediction.model_copy(
        update={
            "predicted_direction": "up",
            "expected_return": tuned_expected,
            "bear_return": min(tuned_expected - width, float(v6_prediction.bear_return)),
            "bull_return": max(tuned_expected + width, float(v6_prediction.bull_return)),
            "up_probability": tuned_up_probability,
            "flat_probability": tuned_flat_probability,
            "down_probability": tuned_down_probability,
            "confidence": _clamp(float(v6_prediction.confidence), 0.26, 0.38),
            "rationale": (
                v6_prediction.rationale
                + " Intraday v7 note: raw flat output carried a positive up-vs-down edge and "
                + "positive expected return, so the v6 session-prior down/flat result was restored "
                + "to a low-confidence up call without using outcome labels."
            )[:800],
        }
    )
    tuned.validate_probability_mass()
    return tuned, {
        "applied": True,
        "method": (
            "intraday_v7_after_hours_2h_raw_flat_up_recovery_v1"
            if after_hours_2h_recovery
            else "intraday_v7_regular_30m_raw_flat_up_recovery_v1"
        ),
        "v6": v6_metadata,
        "original_predicted_direction": prediction.predicted_direction,
        "v6_predicted_direction": v6_prediction.predicted_direction,
        "chosen_direction": "up",
        "session": session,
        "horizon": horizon,
        "up_probability": up_probability,
        "down_probability": down_probability,
        "flat_probability": flat_probability,
        "up_edge": up_edge,
        "expected_return_before": expected,
    }


def _intraday_neutral_edge_openai_prediction(
    prediction: OpenAIReplayPrediction,
    row: dict[str, Any],
) -> tuple[OpenAIReplayPrediction, dict[str, Any]]:
    """Use flat as a deliberate neutral/no-edge bucket after v7.

    v8 keeps the v6/v7 non-leaking intraday scaffolding, but makes neutral
    stronger.  It does not use realized returns, labels, future prices, later
    news, account state, or order state.  The adjustment only uses the raw
    model probabilities/expected return plus replay metadata and the immutable
    point-in-time document snapshot.
    """

    horizon = str(row.get("horizon") or "").upper()
    if horizon not in {"30M", "2H"}:
        return _debiased_openai_prediction(prediction, row)

    v7_prediction, v7_metadata = _intraday_up_recovery_openai_prediction(prediction, row)
    if v7_prediction.predicted_direction == "flat":
        return v7_prediction, {
            "applied": bool(v7_metadata.get("applied")),
            "method": "intraday_v8_kept_existing_neutral_output",
            "v7": v7_metadata,
            "chosen_direction": "flat",
        }

    raw_direction = str(prediction.predicted_direction)
    chosen_direction = str(v7_prediction.predicted_direction)
    up_probability = float(prediction.up_probability)
    down_probability = float(prediction.down_probability)
    flat_probability = float(prediction.flat_probability)
    expected = float(prediction.expected_return)
    abs_expected = abs(expected)
    raw_directional_edge = abs(up_probability - down_probability)
    raw_selected_probability = up_probability if chosen_direction == "up" else down_probability
    raw_opposite_probability = down_probability if chosen_direction == "up" else up_probability
    selected_over_flat = raw_selected_probability - flat_probability
    session = str(row.get("session") or "").lower()

    snapshot = _snapshot(row)
    document_text = _document_text(snapshot).lower()
    mixed_terms = (
        "mixed",
        "cautious",
        "uncertain",
        "headwind",
        "pressure",
        "margin",
        "capex",
        "capital expenditure",
        "weaker",
        "weakness",
        "decline",
        "below",
        "miss",
        "risk",
        "slower",
        "soft",
    )
    positive_terms = (
        "beat",
        "beats",
        "above",
        "raise",
        "raised",
        "raises",
        "record",
        "strong",
        "growth",
    )
    has_mixed_risk = any(term in document_text for term in mixed_terms)
    has_positive = any(term in document_text for term in positive_terms)
    mixed_positive_release = has_mixed_risk and has_positive

    nearly_zero_expected = abs_expected < (0.0010 if horizon == "2H" else 0.0008)
    flat_competitive = flat_probability >= (0.40 if horizon == "2H" else 0.42) and selected_over_flat < 0.025
    weak_directional_edge = raw_directional_edge < 0.035
    raw_flat_or_conflicted = raw_direction == "flat" and selected_over_flat <= 0.0
    v6_metadata = v7_metadata.get("v6") if isinstance(v7_metadata.get("v6"), dict) else {}
    v5_metadata = v6_metadata.get("v5") if isinstance(v6_metadata.get("v5"), dict) else {}
    session_prior_direction = str(v5_metadata.get("method") or "") == "intraday_session_horizon_prior_v1"
    up_neutral_guard = (
        chosen_direction == "up"
        and mixed_positive_release
        and raw_direction == "flat"
        and flat_probability >= (0.40 if horizon == "2H" else 0.42)
        and raw_directional_edge < 0.045
        and abs_expected < 0.0015
    )

    neutralize = (
        (flat_competitive and weak_directional_edge and nearly_zero_expected)
        or (raw_flat_or_conflicted and flat_probability >= (0.40 if horizon == "2H" else 0.42) and nearly_zero_expected)
        or (
            session_prior_direction
            and raw_direction == "flat"
            and flat_probability >= (0.40 if horizon == "2H" else 0.42)
            and raw_directional_edge < 0.035
            and nearly_zero_expected
        )
        or up_neutral_guard
    )

    if not neutralize:
        return v7_prediction, {
            "applied": bool(v7_metadata.get("applied")),
            "method": "intraday_v8_kept_v7_directional_output",
            "v7": v7_metadata,
            "chosen_direction": chosen_direction,
            "raw_predicted_direction": raw_direction,
            "session": session,
            "horizon": horizon,
            "up_probability": up_probability,
            "down_probability": down_probability,
            "flat_probability": flat_probability,
            "raw_directional_edge": raw_directional_edge,
            "selected_over_flat": selected_over_flat,
            "expected_return_before": expected,
            "mixed_positive_release": mixed_positive_release,
        }

    tuned_flat_probability = max(0.44 if horizon == "2H" else 0.42, flat_probability, 0.46)
    directional_total = max(0.0, 1.0 - tuned_flat_probability)
    total_directional = max(0.000001, up_probability + down_probability)
    tuned_up_probability = directional_total * up_probability / total_directional
    tuned_down_probability = directional_total * down_probability / total_directional
    total = tuned_up_probability + tuned_flat_probability + tuned_down_probability
    tuned_up_probability /= total
    tuned_flat_probability /= total
    tuned_down_probability /= total
    width = max(0.006, abs_expected * 3.0, abs(float(v7_prediction.expected_return)) * 2.0)
    tuned = v7_prediction.model_copy(
        update={
            "predicted_direction": "flat",
            "expected_return": 0.0 if abs_expected < 0.004 else expected * 0.25,
            "bear_return": min(-width, float(v7_prediction.bear_return)),
            "bull_return": max(width, float(v7_prediction.bull_return)),
            "up_probability": tuned_up_probability,
            "flat_probability": tuned_flat_probability,
            "down_probability": tuned_down_probability,
            "confidence": _clamp(float(v7_prediction.confidence), 0.22, 0.36),
            "rationale": (
                v7_prediction.rationale
                + " Intraday v8 note: directional evidence did not clear the neutral/no-edge "
                + "bar for a short-horizon call, so the forecast was assigned to flat without "
                + "using outcome labels."
            )[:800],
        }
    )
    tuned.validate_probability_mass()
    return tuned, {
        "applied": True,
        "method": "intraday_v8_directional_to_neutral_v1",
        "v7": v7_metadata,
        "original_predicted_direction": prediction.predicted_direction,
        "v7_predicted_direction": v7_prediction.predicted_direction,
        "chosen_direction": "flat",
        "session": session,
        "horizon": horizon,
        "up_probability": up_probability,
        "down_probability": down_probability,
        "flat_probability": flat_probability,
        "raw_selected_probability": raw_selected_probability,
        "raw_opposite_probability": raw_opposite_probability,
        "raw_directional_edge": raw_directional_edge,
        "selected_over_flat": selected_over_flat,
        "expected_return_before": expected,
        "mixed_positive_release": mixed_positive_release,
        "neutral_reasons": {
            "flat_competitive": flat_competitive,
            "weak_directional_edge": weak_directional_edge,
            "nearly_zero_expected": nearly_zero_expected,
            "raw_flat_or_conflicted": raw_flat_or_conflicted,
            "session_prior_direction": session_prior_direction,
            "up_neutral_guard": up_neutral_guard,
        },
    }


def _intraday_downrisk_critic_openai_prediction(
    prediction: OpenAIReplayPrediction,
    row: dict[str, Any],
) -> tuple[OpenAIReplayPrediction, dict[str, Any]]:
    """Apply a narrow down-risk critic on top of v6.

    v9 intentionally starts from v6 rather than v7/v8.  Offline shadow
    analysis of stored v6 rows showed the cleanest non-leaking improvement was
    not a broad neutral policy, but a small 2H guard: when the raw model said
    flat with almost no up-vs-down edge and v6 still promoted it to up, restore
    the forecast to flat.  The rule uses only replay-time inputs and raw model
    probabilities, not realized outcomes.
    """

    horizon = str(row.get("horizon") or "").upper()
    if horizon not in {"30M", "2H"}:
        return _debiased_openai_prediction(prediction, row)

    v6_prediction, v6_metadata = _intraday_upbias_reduced_openai_prediction(prediction, row)
    if v6_prediction.predicted_direction != "up":
        return v6_prediction, {
            "applied": bool(v6_metadata.get("applied")),
            "method": "intraday_v9_kept_v6_non_up_output",
            "v6": v6_metadata,
            "chosen_direction": v6_prediction.predicted_direction,
        }

    raw_direction = str(prediction.predicted_direction)
    expected = float(prediction.expected_return)
    abs_expected = abs(expected)
    up_probability = float(prediction.up_probability)
    down_probability = float(prediction.down_probability)
    flat_probability = float(prediction.flat_probability)
    up_edge = up_probability - down_probability
    session = str(row.get("session") or "").lower()

    low_edge_2h_up_to_flat = (
        horizon == "2H"
        and raw_direction == "flat"
        and up_edge < 0.050
        and flat_probability >= 0.36
        and abs_expected < 0.0020
    )

    if not low_edge_2h_up_to_flat:
        return v6_prediction, {
            "applied": bool(v6_metadata.get("applied")),
            "method": "intraday_v9_kept_v6_directional_output",
            "v6": v6_metadata,
            "chosen_direction": "up",
            "raw_predicted_direction": raw_direction,
            "session": session,
            "horizon": horizon,
            "up_probability": up_probability,
            "down_probability": down_probability,
            "flat_probability": flat_probability,
            "up_edge": up_edge,
            "expected_return_before": expected,
        }

    tuned_flat_probability = max(0.46, flat_probability)
    directional_total = max(0.0, 1.0 - tuned_flat_probability)
    total_directional = max(0.000001, up_probability + down_probability)
    tuned_up_probability = directional_total * up_probability / total_directional
    tuned_down_probability = directional_total * down_probability / total_directional
    total = tuned_up_probability + tuned_flat_probability + tuned_down_probability
    tuned_up_probability /= total
    tuned_flat_probability /= total
    tuned_down_probability /= total
    width = max(0.006, abs_expected * 3.0, abs(float(v6_prediction.expected_return)) * 2.0)
    tuned = v6_prediction.model_copy(
        update={
            "predicted_direction": "flat",
            "expected_return": 0.0 if abs_expected < 0.004 else expected * 0.25,
            "bear_return": min(-width, float(v6_prediction.bear_return)),
            "bull_return": max(width, float(v6_prediction.bull_return)),
            "up_probability": tuned_up_probability,
            "flat_probability": tuned_flat_probability,
            "down_probability": tuned_down_probability,
            "confidence": _clamp(float(v6_prediction.confidence), 0.22, 0.36),
            "rationale": (
                v6_prediction.rationale
                + " Intraday v9 note: the raw 2H output was flat with only a very small "
                + "up-vs-down edge, so the v6 up promotion was restored to flat without "
                + "using outcome labels."
            )[:800],
        }
    )
    tuned.validate_probability_mass()
    return tuned, {
        "applied": True,
        "method": "intraday_v9_2h_low_edge_up_to_flat_v1",
        "v6": v6_metadata,
        "original_predicted_direction": prediction.predicted_direction,
        "v6_predicted_direction": v6_prediction.predicted_direction,
        "chosen_direction": "flat",
        "session": session,
        "horizon": horizon,
        "up_probability": up_probability,
        "down_probability": down_probability,
        "flat_probability": flat_probability,
        "up_edge": up_edge,
        "expected_return_before": expected,
        "low_edge_2h_up_to_flat": low_edge_2h_up_to_flat,
    }


def _daily_downrisk_neutral_openai_prediction(
    prediction: OpenAIReplayPrediction,
    row: dict[str, Any],
) -> tuple[OpenAIReplayPrediction, dict[str, Any]]:
    """Add a daily down-risk and neutral/no-edge gate on top of v6.

    M47 full-sample scoring showed that v6 improved over the heuristic baseline,
    but still had a large daily up-bias: many actual-down 1D/5D outcomes were
    scored as up, and actual-flat rows were never forecast as flat.  v10 keeps
    v6 as the base, then applies two narrow non-leaking guards using only the
    raw replay-time probabilities, expected return, and horizon:

    - daily_downrisk: if an up call has a meaningful down bucket and the up-vs-
      down edge is not decisive, flip to a low-confidence down call.
    - daily_neutral: if flat is competitive and expected return is tiny, restore
      a flat/no-edge call.

    The thresholds were selected from stored v6 score diagnostics, but the rule
    does not read realized outcomes at scoring time.
    """

    horizon = str(row.get("horizon") or "").upper()
    if horizon in {"30M", "2H"}:
        intraday_prediction, intraday_metadata = _intraday_downrisk_critic_openai_prediction(prediction, row)
        return intraday_prediction, {
            "applied": bool(intraday_metadata.get("applied")),
            "method": "daily_v10_delegated_intraday_to_v9",
            "v9": intraday_metadata,
            "chosen_direction": intraday_prediction.predicted_direction,
        }
    if horizon not in {"1D", "5D"}:
        return _intraday_upbias_reduced_openai_prediction(prediction, row)

    v6_prediction, v6_metadata = _intraday_upbias_reduced_openai_prediction(prediction, row)
    if v6_prediction.predicted_direction != "up":
        return v6_prediction, {
            "applied": bool(v6_metadata.get("applied")),
            "method": "daily_v10_kept_v6_non_up_output",
            "v6": v6_metadata,
            "chosen_direction": v6_prediction.predicted_direction,
            "horizon": horizon,
        }

    up_probability = float(v6_prediction.up_probability)
    flat_probability = float(v6_prediction.flat_probability)
    down_probability = float(v6_prediction.down_probability)
    expected = float(v6_prediction.expected_return)
    confidence = float(v6_prediction.confidence)
    up_down_edge = up_probability - down_probability
    up_flat_edge = up_probability - flat_probability

    daily_downrisk = (
        down_probability >= 0.24
        and up_down_edge <= 0.30
        and expected <= 0.030
    )
    daily_neutral = (
        not daily_downrisk
        and flat_probability >= 0.36
        and up_flat_edge <= 0.04
        and abs(expected) <= 0.010
        and confidence <= 0.55
    )

    if not (daily_downrisk or daily_neutral):
        return v6_prediction, {
            "applied": bool(v6_metadata.get("applied")),
            "method": "daily_v10_kept_v6_directional_output",
            "v6": v6_metadata,
            "chosen_direction": "up",
            "horizon": horizon,
            "up_probability": up_probability,
            "flat_probability": flat_probability,
            "down_probability": down_probability,
            "up_down_edge": up_down_edge,
            "up_flat_edge": up_flat_edge,
            "expected_return_before": expected,
            "confidence_before": confidence,
            "daily_downrisk": daily_downrisk,
            "daily_neutral": daily_neutral,
        }

    if daily_neutral:
        tuned_flat_probability = max(0.46, flat_probability)
        directional_total = max(0.0, 1.0 - tuned_flat_probability)
        total_directional = max(0.000001, up_probability + down_probability)
        tuned_up_probability = directional_total * up_probability / total_directional
        tuned_down_probability = directional_total * down_probability / total_directional
        total = tuned_up_probability + tuned_flat_probability + tuned_down_probability
        tuned_up_probability /= total
        tuned_flat_probability /= total
        tuned_down_probability /= total
        width = max(0.006, abs(expected) * 2.5)
        tuned = v6_prediction.model_copy(
            update={
                "predicted_direction": "flat",
                "expected_return": 0.0,
                "bear_return": min(-width, float(v6_prediction.bear_return)),
                "bull_return": max(width, float(v6_prediction.bull_return)),
                "up_probability": tuned_up_probability,
                "flat_probability": tuned_flat_probability,
                "down_probability": tuned_down_probability,
                "confidence": _clamp(confidence, 0.24, 0.40),
                "rationale": (
                    v6_prediction.rationale
                    + " Daily v10 note: flat/no-edge evidence was competitive and expected return "
                    + "was too small for a directional daily call, so the forecast was restored to flat "
                    + "without using outcome labels."
                )[:800],
            }
        )
        tuned.validate_probability_mass()
        return tuned, {
            "applied": True,
            "method": "daily_v10_competitive_flat_no_edge_v1",
            "v6": v6_metadata,
            "original_predicted_direction": prediction.predicted_direction,
            "v6_predicted_direction": v6_prediction.predicted_direction,
            "chosen_direction": "flat",
            "horizon": horizon,
            "up_probability": up_probability,
            "flat_probability": flat_probability,
            "down_probability": down_probability,
            "up_down_edge": up_down_edge,
            "up_flat_edge": up_flat_edge,
            "expected_return_before": expected,
            "confidence_before": confidence,
            "daily_neutral": daily_neutral,
        }

    tuned_down_probability = max(0.46, down_probability + 0.12)
    residual = max(0.0, 1.0 - tuned_down_probability)
    up_flat_total = max(0.000001, up_probability + flat_probability)
    tuned_up_probability = residual * up_probability / up_flat_total
    tuned_flat_probability = residual * flat_probability / up_flat_total
    total = tuned_up_probability + tuned_flat_probability + tuned_down_probability
    tuned_up_probability /= total
    tuned_flat_probability /= total
    tuned_down_probability /= total
    tuned_expected = -max(0.006, min(0.018, max(abs(expected) * 0.45, 0.008)))
    width = max(0.012, abs(expected) * 1.5, 0.014 if horizon == "5D" else 0.010)
    tuned = v6_prediction.model_copy(
        update={
            "predicted_direction": "down",
            "expected_return": tuned_expected,
            "bear_return": min(tuned_expected - width, float(v6_prediction.bear_return)),
            "bull_return": max(tuned_expected + width, 0.004, float(v6_prediction.bull_return) * 0.35),
            "up_probability": tuned_up_probability,
            "flat_probability": tuned_flat_probability,
            "down_probability": tuned_down_probability,
            "confidence": _clamp(confidence, 0.34, 0.58),
            "rationale": (
                v6_prediction.rationale
                + " Daily v10 note: the raw up call carried a meaningful down-risk bucket and "
                + "the up-vs-down edge did not clear the daily down-risk critic, so the forecast "
                + "was shifted to low-confidence down without using outcome labels."
            )[:800],
        }
    )
    tuned.validate_probability_mass()
    return tuned, {
        "applied": True,
        "method": "daily_v10_up_to_downrisk_v1",
        "v6": v6_metadata,
        "original_predicted_direction": prediction.predicted_direction,
        "v6_predicted_direction": v6_prediction.predicted_direction,
        "chosen_direction": "down",
        "horizon": horizon,
        "up_probability": up_probability,
        "flat_probability": flat_probability,
        "down_probability": down_probability,
        "up_down_edge": up_down_edge,
        "up_flat_edge": up_flat_edge,
        "expected_return_before": expected,
        "confidence_before": confidence,
        "daily_downrisk": daily_downrisk,
    }


def _daily_strongup_flat_recovery_openai_prediction(
    prediction: OpenAIReplayPrediction,
    row: dict[str, Any],
) -> tuple[OpenAIReplayPrediction, dict[str, Any]]:
    """Recover selected strong-up cases and add a very narrow flat gate on v10.

    v11 is intentionally conservative.  M47 v10 improved actual-down handling
    substantially, so v11 keeps v10's down-risk critic and only restores up when
    the pre-v10 daily up evidence was unusually strong.  It also adds a narrow
    true-flat gate for extremely low-confidence/no-edge rows.  Both rules use
    only replay-time probabilities, confidence, expected return, and horizon.
    """

    v10_prediction, v10_metadata = _daily_downrisk_neutral_openai_prediction(prediction, row)
    horizon = str(row.get("horizon") or "").upper()
    if horizon not in {"1D", "5D"}:
        return v10_prediction, {
            "applied": bool(v10_metadata.get("applied")),
            "method": "daily_v11_delegated_non_daily_to_v10",
            "v10": v10_metadata,
            "chosen_direction": v10_prediction.predicted_direction,
        }

    v10_method = str(v10_metadata.get("method") or "")
    raw_up_probability = float(v10_metadata.get("up_probability", prediction.up_probability))
    raw_flat_probability = float(v10_metadata.get("flat_probability", prediction.flat_probability))
    raw_down_probability = float(v10_metadata.get("down_probability", prediction.down_probability))
    raw_expected = float(v10_metadata.get("expected_return_before", prediction.expected_return))
    raw_confidence = float(v10_metadata.get("confidence_before", prediction.confidence))
    raw_up_down_edge = float(v10_metadata.get("up_down_edge", raw_up_probability - raw_down_probability))
    raw_up_flat_edge = float(v10_metadata.get("up_flat_edge", raw_up_probability - raw_flat_probability))

    strong_up_recovery = (
        v10_method == "daily_v10_up_to_downrisk_v1"
        and v10_prediction.predicted_direction == "down"
        and raw_up_probability >= 0.54
        and raw_expected >= 0.012
        and raw_up_down_edge >= 0.18
        and raw_confidence >= 0.58
    )
    true_flat_gate = (
        not strong_up_recovery
        and v10_prediction.predicted_direction in {"up", "down"}
        and abs(float(v10_prediction.expected_return)) <= 0.006
        and float(v10_prediction.flat_probability) >= 0.40
        and (max(float(v10_prediction.up_probability), float(v10_prediction.down_probability)) - float(v10_prediction.flat_probability)) <= 0.06
        and float(v10_prediction.confidence) <= 0.40
    )

    if strong_up_recovery:
        tuned_up_probability = max(0.48, raw_up_probability)
        residual = max(0.0, 1.0 - tuned_up_probability)
        flat_down_total = max(0.000001, raw_flat_probability + raw_down_probability)
        tuned_flat_probability = residual * raw_flat_probability / flat_down_total
        tuned_down_probability = residual * raw_down_probability / flat_down_total
        total = tuned_up_probability + tuned_flat_probability + tuned_down_probability
        tuned_up_probability /= total
        tuned_flat_probability /= total
        tuned_down_probability /= total
        tuned_expected = max(0.010, min(raw_expected, 0.045))
        width = max(0.012, abs(raw_expected) * 1.4, 0.016 if horizon == "5D" else 0.012)
        tuned = v10_prediction.model_copy(
            update={
                "predicted_direction": "up",
                "expected_return": tuned_expected,
                "bear_return": min(tuned_expected - width, float(v10_prediction.bear_return)),
                "bull_return": max(tuned_expected + width, float(v10_prediction.bull_return)),
                "up_probability": tuned_up_probability,
                "flat_probability": tuned_flat_probability,
                "down_probability": tuned_down_probability,
                "confidence": _clamp(raw_confidence, 0.46, 0.64),
                "rationale": (
                    v10_prediction.rationale
                    + " Daily v11 note: v10 down-risk was reversed because pre-v10 up evidence "
                    + "had high confidence, positive expected return, and a clear up-vs-down edge; "
                    + "the forecast was restored to up without using outcome labels."
                )[:800],
            }
        )
        tuned.validate_probability_mass()
        return tuned, {
            "applied": True,
            "method": "daily_v11_strong_up_recovery_v1",
            "v10": v10_metadata,
            "chosen_direction": "up",
            "horizon": horizon,
            "raw_up_probability": raw_up_probability,
            "raw_flat_probability": raw_flat_probability,
            "raw_down_probability": raw_down_probability,
            "raw_expected_return": raw_expected,
            "raw_confidence": raw_confidence,
            "raw_up_down_edge": raw_up_down_edge,
            "raw_up_flat_edge": raw_up_flat_edge,
        }

    if true_flat_gate:
        tuned_flat_probability = max(0.46, float(v10_prediction.flat_probability))
        directional_total = max(0.0, 1.0 - tuned_flat_probability)
        up_down_total = max(0.000001, float(v10_prediction.up_probability) + float(v10_prediction.down_probability))
        tuned_up_probability = directional_total * float(v10_prediction.up_probability) / up_down_total
        tuned_down_probability = directional_total * float(v10_prediction.down_probability) / up_down_total
        total = tuned_up_probability + tuned_flat_probability + tuned_down_probability
        tuned_up_probability /= total
        tuned_flat_probability /= total
        tuned_down_probability /= total
        width = max(0.006, abs(float(v10_prediction.expected_return)) * 2.0)
        tuned = v10_prediction.model_copy(
            update={
                "predicted_direction": "flat",
                "expected_return": 0.0,
                "bear_return": min(-width, float(v10_prediction.bear_return)),
                "bull_return": max(width, float(v10_prediction.bull_return)),
                "up_probability": tuned_up_probability,
                "flat_probability": tuned_flat_probability,
                "down_probability": tuned_down_probability,
                "confidence": _clamp(float(v10_prediction.confidence), 0.24, 0.38),
                "rationale": (
                    v10_prediction.rationale
                    + " Daily v11 note: top directional probability was barely ahead of flat and "
                    + "expected return was near zero, so the forecast was assigned to flat without "
                    + "using outcome labels."
                )[:800],
            }
        )
        tuned.validate_probability_mass()
        return tuned, {
            "applied": True,
            "method": "daily_v11_true_flat_low_edge_v1",
            "v10": v10_metadata,
            "chosen_direction": "flat",
            "horizon": horizon,
            "v10_predicted_direction": v10_prediction.predicted_direction,
            "v10_up_probability": float(v10_prediction.up_probability),
            "v10_flat_probability": float(v10_prediction.flat_probability),
            "v10_down_probability": float(v10_prediction.down_probability),
            "v10_expected_return": float(v10_prediction.expected_return),
            "v10_confidence": float(v10_prediction.confidence),
        }

    return v10_prediction, {
        "applied": bool(v10_metadata.get("applied")),
        "method": "daily_v11_kept_v10_output",
        "v10": v10_metadata,
        "chosen_direction": v10_prediction.predicted_direction,
        "horizon": horizon,
        "strong_up_recovery": strong_up_recovery,
        "true_flat_gate": true_flat_gate,
        "raw_up_probability": raw_up_probability,
        "raw_flat_probability": raw_flat_probability,
        "raw_down_probability": raw_down_probability,
        "raw_expected_return": raw_expected,
        "raw_confidence": raw_confidence,
        "raw_up_down_edge": raw_up_down_edge,
    }


def _daily_v10_postcalibrated_openai_prediction(
    prediction: OpenAIReplayPrediction,
    row: dict[str, Any],
) -> tuple[OpenAIReplayPrediction, dict[str, Any]]:
    """Keep the v10 prompt/output shape, then add narrow deterministic v12 guards.

    M48 v11 showed that changing the daily prompt can move the raw model output
    too far.  v12 therefore deliberately starts from v10 and does not ask the
    model to become more bullish or more bearish.  It only adds:

    - strong_up_recovery_v2: a very narrow restoration of v10 down-risk calls
      when the pre-v10 up evidence is unusually strong.
    - observe_only_gate: a non-direction-changing metadata flag for low-edge or
      flat/no-edge cases.  The stored direction remains comparable with prior
      score rows, while downstream execution layers can avoid weak trades.

    The rules use only replay-time probabilities, confidence, expected return,
    horizon, and market session.  They do not read realized outcomes.
    """

    v10_prediction, v10_metadata = _daily_downrisk_neutral_openai_prediction(prediction, row)
    horizon = str(row.get("horizon") or "").upper()
    if horizon not in {"1D", "5D"}:
        observe_only = v10_prediction.predicted_direction == "flat" or (
            abs(float(v10_prediction.expected_return)) <= 0.004
            and float(v10_prediction.confidence) <= 0.38
        )
        return v10_prediction, {
            "applied": bool(v10_metadata.get("applied")),
            "method": "daily_v12_delegated_non_daily_to_v10",
            "v10": v10_metadata,
            "chosen_direction": v10_prediction.predicted_direction,
            "trade_action": "observe_only" if observe_only else "directional_signal",
            "observe_only": observe_only,
            "observe_only_reason": "non_daily_low_edge_or_flat" if observe_only else "",
        }

    v10_method = str(v10_metadata.get("method") or "")
    raw_up_probability = float(v10_metadata.get("up_probability", prediction.up_probability))
    raw_flat_probability = float(v10_metadata.get("flat_probability", prediction.flat_probability))
    raw_down_probability = float(v10_metadata.get("down_probability", prediction.down_probability))
    raw_expected = float(v10_metadata.get("expected_return_before", prediction.expected_return))
    raw_confidence = float(v10_metadata.get("confidence_before", prediction.confidence))
    raw_up_down_edge = float(v10_metadata.get("up_down_edge", raw_up_probability - raw_down_probability))
    raw_up_flat_edge = float(v10_metadata.get("up_flat_edge", raw_up_probability - raw_flat_probability))

    strong_up_recovery = (
        v10_method == "daily_v10_up_to_downrisk_v1"
        and v10_prediction.predicted_direction == "down"
        and raw_up_probability >= 0.54
        and raw_up_down_edge >= 0.24
        and raw_expected >= 0.010
        and raw_confidence >= 0.58
    )

    observe_only = False
    observe_only_reason = ""
    v10_expected = float(v10_prediction.expected_return)
    v10_confidence = float(v10_prediction.confidence)
    v10_flat_probability = float(v10_prediction.flat_probability)
    v10_top_directional_probability = max(float(v10_prediction.up_probability), float(v10_prediction.down_probability))
    if v10_prediction.predicted_direction == "flat":
        observe_only = True
        observe_only_reason = "v10_flat_no_edge"
    elif (
        abs(v10_expected) <= 0.008
        and v10_confidence <= 0.44
        and v10_flat_probability >= 0.30
        and (v10_top_directional_probability - v10_flat_probability) <= 0.18
    ):
        observe_only = True
        observe_only_reason = "low_edge_directional_signal"
    elif (
        v10_method == "daily_v10_up_to_downrisk_v1"
        and raw_up_probability < 0.46
        and raw_up_down_edge <= 0.18
        and abs(raw_expected) <= 0.008
    ):
        observe_only = True
        observe_only_reason = "weak_raw_up_shifted_to_downrisk"

    if strong_up_recovery:
        tuned_up_probability = max(0.50, raw_up_probability)
        residual = max(0.0, 1.0 - tuned_up_probability)
        flat_down_total = max(0.000001, raw_flat_probability + raw_down_probability)
        tuned_flat_probability = residual * raw_flat_probability / flat_down_total
        tuned_down_probability = residual * raw_down_probability / flat_down_total
        total = tuned_up_probability + tuned_flat_probability + tuned_down_probability
        tuned_up_probability /= total
        tuned_flat_probability /= total
        tuned_down_probability /= total
        tuned_expected = max(0.010, min(raw_expected, 0.040))
        width = max(0.012, abs(raw_expected) * 1.35, 0.016 if horizon == "5D" else 0.012)
        tuned = v10_prediction.model_copy(
            update={
                "predicted_direction": "up",
                "expected_return": tuned_expected,
                "bear_return": min(tuned_expected - width, float(v10_prediction.bear_return)),
                "bull_return": max(tuned_expected + width, float(v10_prediction.bull_return)),
                "up_probability": tuned_up_probability,
                "flat_probability": tuned_flat_probability,
                "down_probability": tuned_down_probability,
                "confidence": _clamp(raw_confidence, 0.48, 0.62),
                "rationale": (
                    v10_prediction.rationale
                    + " Daily v12 note: v10 down-risk was reversed only because the pre-v10 "
                    + "up signal cleared the strict strong-up recovery threshold; the v10 prompt "
                    + "was preserved and no outcome label was used."
                )[:800],
            }
        )
        tuned.validate_probability_mass()
        return tuned, {
            "applied": True,
            "method": "daily_v12_strong_up_recovery_v2",
            "v10": v10_metadata,
            "chosen_direction": "up",
            "horizon": horizon,
            "trade_action": "directional_signal",
            "observe_only": False,
            "observe_only_reason": "",
            "raw_up_probability": raw_up_probability,
            "raw_flat_probability": raw_flat_probability,
            "raw_down_probability": raw_down_probability,
            "raw_expected_return": raw_expected,
            "raw_confidence": raw_confidence,
            "raw_up_down_edge": raw_up_down_edge,
            "raw_up_flat_edge": raw_up_flat_edge,
        }

    return v10_prediction, {
        "applied": bool(v10_metadata.get("applied")),
        "method": "daily_v12_kept_v10_output",
        "v10": v10_metadata,
        "chosen_direction": v10_prediction.predicted_direction,
        "horizon": horizon,
        "strong_up_recovery": strong_up_recovery,
        "trade_action": "observe_only" if observe_only else "directional_signal",
        "observe_only": observe_only,
        "observe_only_reason": observe_only_reason,
        "v10_expected_return": v10_expected,
        "v10_confidence": v10_confidence,
        "v10_flat_probability": v10_flat_probability,
        "raw_up_probability": raw_up_probability,
        "raw_flat_probability": raw_flat_probability,
        "raw_down_probability": raw_down_probability,
        "raw_expected_return": raw_expected,
        "raw_confidence": raw_confidence,
        "raw_up_down_edge": raw_up_down_edge,
        "raw_up_flat_edge": raw_up_flat_edge,
    }


def _daily_v10_narrow_action_gate_openai_prediction(
    prediction: OpenAIReplayPrediction,
    row: dict[str, Any],
) -> tuple[OpenAIReplayPrediction, dict[str, Any]]:
    """Narrow v12 observe-only behavior while preserving its direction logic.

    M51 action-gate evaluation showed that v12's broad observe-only gate helped
    slightly in aggregate, but one reason bucket was too conservative:
    ``weak_raw_up_shifted_to_downrisk`` rows performed well as tradeable down
    signals.  v13 therefore keeps v12's v10-preserving direction calibration
    and strong-up recovery, but reclassifies that reason back to
    ``directional_signal``.  The rule changes execution metadata only; it does
    not use realized outcomes while scoring.
    """

    v12_prediction, v12_metadata = _daily_v10_postcalibrated_openai_prediction(prediction, row)
    reason = str(v12_metadata.get("observe_only_reason") or "")
    if (
        v12_metadata.get("observe_only") is True
        and reason == "weak_raw_up_shifted_to_downrisk"
        and v12_prediction.predicted_direction in {"up", "down"}
    ):
        narrowed = dict(v12_metadata)
        narrowed.update(
            {
                "method": "daily_v13_narrowed_action_gate_v1",
                "v12": v12_metadata,
                "trade_action": "directional_signal",
                "observe_only": False,
                "observe_only_reason": "",
                "narrowed_from_observe_only_reason": reason,
                "chosen_direction": v12_prediction.predicted_direction,
            }
        )
        return v12_prediction, narrowed

    return v12_prediction, {
        "applied": bool(v12_metadata.get("applied")),
        "method": "daily_v13_kept_v12_gate",
        "v12": v12_metadata,
        "chosen_direction": v12_prediction.predicted_direction,
        "trade_action": v12_metadata.get("trade_action", "directional_signal"),
        "observe_only": bool(v12_metadata.get("observe_only")),
        "observe_only_reason": reason,
    }


class HeuristicReplayScorer:
    scorer_name = "heuristic"
    model_name = "heuristic-replay-v1"
    scorer_version = REPLAY_SCORER_VERSION

    def score(self, row: dict[str, Any]) -> ReplayScore:
        snapshot = _snapshot(row)
        text = _document_text(snapshot)
        tokens = re.findall(r"[a-z]+", text.lower())
        signal = sum(_POSITIVE_TERMS.get(token, 0.0) + _NEGATIVE_TERMS.get(token, 0.0) for token in tokens)
        signal = max(-4.0, min(4.0, signal))
        normalized = signal / 4.0
        horizon = str(row["horizon"]).upper()
        scale = _HORIZON_SCALE.get(horizon, 1.0)
        expected = normalized * 0.035 * scale
        width = 0.018 + abs(expected) * 1.8
        up_probability, flat_probability, down_probability = _softmax3(normalized * 1.7)
        predicted_direction = _direction(expected)
        confidence = max(0.20, min(0.82, 0.28 + abs(normalized) * 0.45))
        actual_direction = str(row.get("direction_label") or "unknown")
        actual_raw = row.get("raw_return")
        actual_excess = row.get("excess_return")
        actual_raw_float = float(actual_raw) if actual_raw is not None else None
        actual_excess_float = float(actual_excess) if actual_excess is not None else None
        direction_hit = (
            int(predicted_direction == actual_direction)
            if actual_direction in {"up", "down", "flat"}
            else None
        )
        absolute_raw_error = (
            abs(expected - actual_raw_float)
            if actual_raw_float is not None
            else None
        )
        absolute_excess_error = (
            abs(expected - actual_excess_float)
            if actual_excess_float is not None
            else None
        )
        raw = {
            "scorer": self.model_name,
            "signal": signal,
            "normalized_signal": normalized,
            "horizon_scale": scale,
            "matched_positive_terms": sorted({token for token in tokens if token in _POSITIVE_TERMS}),
            "matched_negative_terms": sorted({token for token in tokens if token in _NEGATIVE_TERMS}),
        }
        rationale = (
            f"keyword_signal={signal:.2f}; horizon_scale={scale:.2f}; "
            f"direction={predicted_direction}"
        )
        return ReplayScore(
            replay_corpus_id=int(row["replay_corpus_id"]),
            outcome_id=int(row["outcome_id"]),
            historical_event_id=int(row["historical_event_id"]),
            official_document_id=int(row["official_document_id"]),
            symbol=str(row["actual_subject_symbol"]).upper(),
            event_type=str(row["event_type"]),
            event_subtype=str(row["event_subtype"] or ""),
            replay_timestamp=str(row["replay_timestamp"]),
            prediction_cutoff_at=str(row["prediction_cutoff_at"]),
            horizon=horizon,
            price_source=str(row["price_source"]),
            scorer_name=self.scorer_name,
            model_name=self.model_name,
            scorer_version=self.scorer_version,
            input_snapshot_hash=str(row["input_snapshot_hash"]),
            prediction_status="complete",
            predicted_direction=predicted_direction,
            expected_return=expected,
            bear_return=max(-0.99, expected - width),
            bull_return=expected + width,
            up_probability=up_probability,
            flat_probability=flat_probability,
            down_probability=down_probability,
            confidence=confidence,
            rationale=rationale,
            raw_json=json.dumps(raw, ensure_ascii=False, sort_keys=True),
            actual_direction=actual_direction,
            actual_raw_return=actual_raw_float,
            actual_excess_return=actual_excess_float,
            direction_hit=direction_hit,
            absolute_raw_error=absolute_raw_error,
            absolute_excess_error=absolute_excess_error,
        )


class OpenAIReplayScorer:
    scorer_name = "openai"
    scorer_version = OPENAI_REPLAY_SCORER_VERSION

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        timeout_seconds: float,
        reasoning_effort: str = "low",
        max_input_chars: int = 16000,
        scorer_version: str = OPENAI_REPLAY_SCORER_VERSION,
        client: Any | None = None,
    ) -> None:
        if not api_key and client is None:
            raise PermanentAnalysisError("OPENAI_API_KEY is missing. Add it to .env before running --scorer openai.")
        if reasoning_effort not in {"none", "low", "medium", "high", "xhigh"}:
            raise PermanentAnalysisError(
                f"Unsupported reasoning effort {reasoning_effort!r}; use none, low, medium, high, or xhigh."
            )
        self.model_name = model
        self.reasoning_effort = reasoning_effort
        self.max_input_chars = max(1000, int(max_input_chars))
        self.scorer_version = scorer_version or OPENAI_REPLAY_SCORER_VERSION
        if client is not None:
            self.client = client
            return
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise PermanentAnalysisError("The openai package is not installed. Run pip install -r requirements.txt.") from exc
        self.client = OpenAI(api_key=api_key, timeout=timeout_seconds, max_retries=0)

    def score(self, row: dict[str, Any]) -> ReplayScore:
        snapshot = _snapshot(row)
        document = snapshot.get("official_document") if isinstance(snapshot, dict) else {}
        if not isinstance(document, dict):
            document = {}
        content = str(document.get("content") or "")
        trimmed_content = content[: self.max_input_chars]
        system_prompt = (
            "You are scoring a historical market-event replay. Use only the provided "
            "point-in-time official source material. Do not infer from realized prices, "
            "later news, or any outcome label.\n\n"
            "Your task is not to judge whether the company is good; it is to predict the "
            "short-horizon market reaction versus expectations for the requested horizon. "
            "Earnings/guidance reactions are often dominated by surprise, forward guide, "
            "margin tone, capex/demand commentary, and whether good news was already priced in.\n\n"
            "Calibration rules:\n"
            "- Reserve 'flat' for genuinely neutral or mixed releases where expected absolute return is below about 0.20%.\n"
            "- If the release has a clear positive or negative tilt, choose up/down even when confidence is modest.\n"
            "- Keep confidence calibrated: 0.33 means little edge, 0.45 weak edge, 0.55 moderate edge, >0.70 rare.\n"
            "- For 30M/2H intraday horizons, be extra cautious: short-horizon reaction can be noisy, so use lower confidence and avoid overreading tiny probability differences.\n"
            "- For 1D horizons, typical non-extreme earnings reactions are often in the +/-1% to +/-8% range; 5D can be wider.\n"
            "- Probabilities must reflect uncertainty, but predicted_direction should be the highest-probability class.\n"
            "- A deterministic post-processor may calibrate low-conviction 30M/2H outputs using only your probabilities, expected return, horizon, and market session; make flat a deliberate choice."
        )
        if self.scorer_version in {
            OPENAI_REPLAY_SCORER_VERSION_V10,
            OPENAI_REPLAY_SCORER_VERSION_V12,
            OPENAI_REPLAY_SCORER_VERSION_V13,
            OPENAI_REPLAY_SCORER_VERSION_V14,
            OPENAI_REPLAY_SCORER_VERSION_V15,
            OPENAI_REPLAY_SCORER_VERSION_V16,
        }:
            system_prompt += (
                "\n- For 1D/5D horizons, explicitly consider down-risk even when the headline is positive: "
                "sell-the-news, priced-in expectations, weak guide quality, margin/capex pressure, and sector drag "
                "can make a superficially good release trade down.\n"
                "- For 1D/5D horizons, use flat when positive and negative evidence are genuinely offset and the "
                "expected absolute return is small; do not force up just because company text sounds constructive."
            )
        if self.scorer_version in {OPENAI_REPLAY_SCORER_VERSION_V14, OPENAI_REPLAY_SCORER_VERSION_V15, OPENAI_REPLAY_SCORER_VERSION_V16}:
            system_prompt += (
                "\n- For v14/v15/v16, use scorer_evidence_packet_v2 as point-in-time specialist context only. "
                "It summarizes Economic Indicator Desk, Geopolitical Overlay, and Market Data Desk signals that "
                "were available at or before the replay cutoff. Treat tri-desk confirmation as support for the "
                "headline direction and tri-desk conflict as a reason to lower confidence or prefer flat/down-risk. "
                "If options_market_structure is available, use negative gamma/trend-amplification as evidence that "
                "a clean news reaction can overshoot, and use positive gamma/wall/max-pain proximity as evidence for "
                "mean-reversion, pinning, or overextension haircuts. Downweight stale, proxy-only, or probe-backed "
                "option context. Never use realized outcomes or future reactions."
            )
        if self.scorer_version == OPENAI_REPLAY_SCORER_VERSION_V15:
            system_prompt += (
                "\n- For v15, pay special attention to fresh emergency_breaking_news overlay context. "
                "A fresh high/critical emergency risk signal that conflicts with an up call should reduce confidence, "
                "especially when the tri-desk alignment is also conflicted. A fresh emergency signal confirming a "
                "down-risk call can support preserving that down-risk signal."
            )
        if self.scorer_version == OPENAI_REPLAY_SCORER_VERSION_V11:
            system_prompt += (
                "\n- For 1D/5D horizons, balance the v10 down-risk discipline with strong-up recovery: "
                "when the release has clear upside surprise, raised guide quality, strong demand, and the up "
                "probability edge is decisive, do not over-penalize it as sell-the-news.\n"
                "- For 1D/5D horizons, reserve flat for true no-edge situations with near-zero expected return, "
                "low confidence, and flat probability competitive with up/down."
            )
        user_payload = {
            "symbol": row["actual_subject_symbol"],
            "event_type": row["event_type"],
            "event_subtype": row["event_subtype"],
            "replay_timestamp": row["replay_timestamp"],
            "prediction_cutoff_at": row["prediction_cutoff_at"],
            "session": row["session"],
            "horizon": row["horizon"],
            "official_document": {
                "title": document.get("title") or "",
                "source_type": document.get("source_type") or "",
                "document_type": document.get("document_type") or "",
                "available_at": document.get("available_at") or "",
                "content": trimmed_content,
                "content_truncated": len(content) > len(trimmed_content),
            },
            "required_output": {
                "predicted_direction": "up|down|flat",
                "expected_return": "decimal return, e.g. 0.015 for +1.5%",
                "bear_return": "pessimistic decimal return",
                "bull_return": "optimistic decimal return",
                "probabilities": "up_probability + flat_probability + down_probability ~= 1",
                "confidence": "0 to 1",
                "rationale": "short rationale grounded in the official source",
            },
            "calibration_policy": {
                "flat_threshold": "Use flat only for truly neutral/mixed cases with expected absolute return near zero.",
                "confidence_scale": "0.33 no edge, 0.45 weak, 0.55 moderate, above 0.70 rare.",
                "intraday_policy": "For 30M/2H horizons, keep confidence lower and do not let very small up/down probability differences dominate the expected-return sign.",
                "flat_debias_policy": "If flat is selected but internal probabilities, expected return, horizon, or market session imply a calibration edge, the active scorer version may reassign a modest forecast without outcome labels.",
                "do_not_use": [
                    "realized returns",
                    "outcome labels",
                    "future prices",
                    "later news",
                ],
            },
        }
        compact_evidence_packet = _compact_scorer_evidence_packet(row.get("scorer_evidence_packet"))
        if self.scorer_version in {OPENAI_REPLAY_SCORER_VERSION_V14, OPENAI_REPLAY_SCORER_VERSION_V15, OPENAI_REPLAY_SCORER_VERSION_V16} and compact_evidence_packet is not None:
            user_payload["scorer_evidence_packet_v2"] = compact_evidence_packet
        started = time.perf_counter()
        try:
            response = self.client.responses.parse(
                model=self.model_name,
                reasoning={"effort": self.reasoning_effort},
                input=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
                ],
                text_format=OpenAIReplayPrediction,
                store=False,
            )
            prediction = _extract_openai_parsed(response)
        except (PermanentAnalysisError, TransientAnalysisError):
            raise
        except ValidationError as exc:
            raise TransientAnalysisError(f"OpenAI replay scoring output validation failed: {exc}") from exc
        except Exception as exc:
            mapped = _map_openai_exception(exc)
            raise mapped from exc

        original_prediction = prediction
        debias_metadata = {"applied": False, "reason": "scorer_version_without_debias"}
        if self.scorer_version == "openai-replay-score-v3":
            prediction, debias_metadata = _debiased_openai_prediction(prediction, row)
        elif self.scorer_version == "openai-replay-score-v4":
            prediction, debias_metadata = _intraday_tuned_openai_prediction(prediction, row)
        elif self.scorer_version == "openai-replay-score-v5":
            prediction, debias_metadata = _intraday_balanced_openai_prediction(prediction, row)
        elif self.scorer_version == "openai-replay-score-v6":
            prediction, debias_metadata = _intraday_upbias_reduced_openai_prediction(prediction, row)
        elif self.scorer_version == "openai-replay-score-v7":
            prediction, debias_metadata = _intraday_up_recovery_openai_prediction(prediction, row)
        elif self.scorer_version == "openai-replay-score-v8":
            prediction, debias_metadata = _intraday_neutral_edge_openai_prediction(prediction, row)
        elif self.scorer_version == "openai-replay-score-v9":
            prediction, debias_metadata = _intraday_downrisk_critic_openai_prediction(prediction, row)
        elif self.scorer_version == "openai-replay-score-v10":
            prediction, debias_metadata = _daily_downrisk_neutral_openai_prediction(prediction, row)
        elif self.scorer_version == "openai-replay-score-v11":
            prediction, debias_metadata = _daily_strongup_flat_recovery_openai_prediction(prediction, row)
        elif self.scorer_version == "openai-replay-score-v12":
            prediction, debias_metadata = _daily_v10_postcalibrated_openai_prediction(prediction, row)
        elif self.scorer_version == "openai-replay-score-v13":
            prediction, debias_metadata = _daily_v10_narrow_action_gate_openai_prediction(prediction, row)
        elif self.scorer_version == "openai-replay-score-v14":
            prediction, debias_metadata = _daily_tri_desk_evidence_packet_openai_prediction(prediction, row)
        elif self.scorer_version == "openai-replay-score-v15":
            prediction, debias_metadata = _daily_tri_desk_emergency_v15_openai_prediction(prediction, row)
        elif self.scorer_version == "openai-replay-score-v16":
            prediction, debias_metadata = _daily_tri_desk_emergency_v16_openai_prediction(prediction, row)

        raw_payload = {
            "prediction": prediction.model_dump(mode="json"),
            "original_prediction": original_prediction.model_dump(mode="json"),
            "debias": debias_metadata,
            "response_id": getattr(response, "id", None),
            "duration_ms": int((time.perf_counter() - started) * 1000),
            "model": self.model_name,
            "reasoning_effort": self.reasoning_effort,
            "scorer_version": self.scorer_version,
            "prompt_profile": (
                "market-reaction-intraday-downrisk-v9"
                if self.scorer_version == "openai-replay-score-v9"
                else "market-reaction-daily-downrisk-neutral-v10"
                if self.scorer_version == "openai-replay-score-v10"
                else "market-reaction-daily-v10-postcalibrated-v12"
                if self.scorer_version == "openai-replay-score-v12"
                else "market-reaction-daily-v10-narrow-action-gate-v13"
                if self.scorer_version == "openai-replay-score-v13"
                else "market-reaction-daily-tri-desk-evidence-v14"
                if self.scorer_version == "openai-replay-score-v14"
                else "market-reaction-daily-tri-desk-emergency-v15"
                if self.scorer_version == "openai-replay-score-v15"
                else "market-reaction-daily-tri-desk-emergency-narrow-v16"
                if self.scorer_version == "openai-replay-score-v16"
                else "market-reaction-daily-strongup-flat-v11"
                if self.scorer_version == "openai-replay-score-v11"
                else "market-reaction-intraday-neutral-edge-v8"
                if self.scorer_version == "openai-replay-score-v8"
                else "market-reaction-intraday-up-recovery-v7"
                if self.scorer_version == "openai-replay-score-v7"
                else "market-reaction-intraday-upbias-reduced-v6"
                if self.scorer_version == "openai-replay-score-v6"
                else "market-reaction-intraday-balanced-v5"
                if self.scorer_version == "openai-replay-score-v5"
                else "market-reaction-intraday-tuned-v4"
                if self.scorer_version == "openai-replay-score-v4"
                else "market-reaction-flat-debiased-v3"
            ),
        }
        return _make_score_from_prediction(
            row,
            scorer_name=self.scorer_name,
            model_name=self.model_name,
            scorer_version=self.scorer_version,
            prediction=prediction,
            raw_json=json.dumps(raw_payload, ensure_ascii=False, sort_keys=True),
        )


class ReplayScoringEngine:
    def __init__(self, db: Any, scorer: Any | None = None) -> None:
        self.db = db
        self.scorer = scorer or HeuristicReplayScorer()

    def build(
        self,
        *,
        event_type: str | None = None,
        symbol: str | None = None,
        price_source: str | None = None,
        horizon: str | None = None,
        start: str | None = None,
        end: str | None = None,
        limit: int = 1000,
        only_missing: bool = False,
    ) -> dict[str, Any]:
        rows = self.db.replay_dataset_export_rows(
            limit=limit,
            event_type=event_type,
            symbol=symbol,
            price_source=price_source,
            horizon=horizon,
            start=start,
            end=end,
            include_blocked=False,
        )
        candidate_rows = len(rows)
        skipped_existing = 0
        if only_missing and hasattr(self.db, "existing_replay_model_score_outcome_ids"):
            existing = self.db.existing_replay_model_score_outcome_ids(
                outcome_ids=[int(row["outcome_id"]) for row in rows],
                scorer_name=self.scorer.scorer_name,
                model_name=self.scorer.model_name,
                scorer_version=self.scorer.scorer_version,
            )
            if existing:
                rows = [row for row in rows if int(row["outcome_id"]) not in existing]
                skipped_existing = candidate_rows - len(rows)
        created = 0
        updated = 0
        hits = 0
        scored = 0
        external_signals: list[dict[str, Any]] = []
        use_scorer_evidence_packet = (
            getattr(self.scorer, "scorer_name", "") == "openai"
            and getattr(self.scorer, "scorer_version", "") in {
                OPENAI_REPLAY_SCORER_VERSION_V14,
                OPENAI_REPLAY_SCORER_VERSION_V15,
                OPENAI_REPLAY_SCORER_VERSION_V16,
            }
        )
        if use_scorer_evidence_packet and hasattr(self.db, "recent_external_market_signals"):
            external_signals = self.db.recent_external_market_signals(limit=12000)
        for row in rows:
            score_row = dict(row)
            if use_scorer_evidence_packet:
                score_row["scorer_evidence_packet"] = build_overlay_evidence_packet(score_row, external_signals, self.db)
            score = self.scorer.score(score_row)
            _, was_created = self.db.upsert_replay_model_score(score)
            created += int(was_created)
            updated += int(not was_created)
            if score.direction_hit is not None:
                hits += score.direction_hit
            scored += 1
        return {
            "candidate_rows": candidate_rows,
            "skipped_existing": skipped_existing,
            "scored": scored,
            "created": created,
            "updated": updated,
            "direction_hits": hits,
            "direction_accuracy": (hits / scored) if scored else None,
            "scorer_name": self.scorer.scorer_name,
            "model_name": self.scorer.model_name,
            "scorer_version": self.scorer.scorer_version,
        }


def summarize_scores(rows: list[dict[str, Any]]) -> dict[str, Any]:
    evaluated = [row for row in rows if row.get("direction_hit") is not None]
    hits = sum(int(row["direction_hit"]) for row in evaluated)
    raw_errors = [float(row["absolute_raw_error"]) for row in rows if row.get("absolute_raw_error") is not None]
    excess_errors = [
        float(row["absolute_excess_error"])
        for row in rows
        if row.get("absolute_excess_error") is not None
    ]
    return {
        "rows": len(rows),
        "evaluated": len(evaluated),
        "direction_hits": hits,
        "direction_accuracy": (hits / len(evaluated)) if evaluated else None,
        "avg_absolute_raw_error": (sum(raw_errors) / len(raw_errors)) if raw_errors else None,
        "avg_absolute_excess_error": (sum(excess_errors) / len(excess_errors)) if excess_errors else None,
    }


def calibration_summary(rows: list[dict[str, Any]], *, bin_count: int = 5) -> dict[str, Any]:
    evaluated = [row for row in rows if row.get("direction_hit") is not None]
    if not evaluated:
        return {
            "rows": len(rows),
            "evaluated": 0,
            "direction_hits": 0,
            "direction_accuracy": None,
            "avg_confidence": None,
            "calibration_gap": None,
            "expected_calibration_error": None,
            "bins": [],
            "direction_distribution": {},
        }

    bin_count = max(1, int(bin_count))
    bins: list[dict[str, Any]] = []
    total_evaluated = len(evaluated)
    weighted_abs_gap = 0.0
    for index in range(bin_count):
        lower = index / bin_count
        upper = (index + 1) / bin_count
        if index == bin_count - 1:
            members = [
                row for row in evaluated
                if lower <= float(row.get("confidence") or 0.0) <= upper
            ]
        else:
            members = [
                row for row in evaluated
                if lower <= float(row.get("confidence") or 0.0) < upper
            ]
        if not members:
            bins.append(
                {
                    "bin": f"{lower:.0%}-{upper:.0%}",
                    "lower": lower,
                    "upper": upper,
                    "rows": 0,
                    "accuracy": None,
                    "avg_confidence": None,
                    "calibration_gap": None,
                }
            )
            continue
        hits = sum(int(row["direction_hit"]) for row in members)
        accuracy = hits / len(members)
        avg_confidence = sum(float(row.get("confidence") or 0.0) for row in members) / len(members)
        gap = avg_confidence - accuracy
        weighted_abs_gap += (len(members) / total_evaluated) * abs(gap)
        bins.append(
            {
                "bin": f"{lower:.0%}-{upper:.0%}",
                "lower": lower,
                "upper": upper,
                "rows": len(members),
                "accuracy": accuracy,
                "avg_confidence": avg_confidence,
                "calibration_gap": gap,
            }
        )

    hits = sum(int(row["direction_hit"]) for row in evaluated)
    accuracy = hits / total_evaluated
    avg_confidence = sum(float(row.get("confidence") or 0.0) for row in evaluated) / total_evaluated
    direction_distribution: dict[str, dict[str, int]] = {}
    for row in evaluated:
        predicted = str(row.get("predicted_direction") or "unknown")
        actual = str(row.get("actual_direction") or "unknown")
        bucket = direction_distribution.setdefault(predicted, {"rows": 0, "hits": 0})
        bucket["rows"] += 1
        bucket["hits"] += int(row["direction_hit"])
        actual_bucket = direction_distribution.setdefault(f"actual:{actual}", {"rows": 0, "hits": 0})
        actual_bucket["rows"] += 1

    return {
        "rows": len(rows),
        "evaluated": total_evaluated,
        "direction_hits": hits,
        "direction_accuracy": accuracy,
        "avg_confidence": avg_confidence,
        "calibration_gap": avg_confidence - accuracy,
        "expected_calibration_error": weighted_abs_gap,
        "bins": bins,
        "direction_distribution": direction_distribution,
    }


def _fmt_optional_pct(value: Any) -> str:
    if value is None:
        return "-"
    return f"{float(value):.2%}"


def _fmt_optional_signed_pct(value: Any) -> str:
    if value is None:
        return "-"
    return f"{float(value):+.2%}"


def calibration_markdown(
    rows: list[dict[str, Any]],
    *,
    filters: dict[str, Any] | None = None,
    title: str = "Replay Score Calibration Report",
) -> str:
    generated_at = datetime.now(UTC).isoformat()
    summary = calibration_summary(rows)
    filters = filters or {}
    lines = [
        f"# {title}",
        "",
        f"- Generated at: `{generated_at}`",
        f"- Rows: `{summary['rows']}`",
        f"- Evaluated: `{summary['evaluated']}`",
        f"- Direction accuracy: `{_fmt_optional_pct(summary['direction_accuracy'])}`",
        f"- Average confidence: `{_fmt_optional_pct(summary['avg_confidence'])}`",
        f"- Confidence minus accuracy: `{_fmt_optional_pct(summary['calibration_gap'])}`",
        f"- Expected calibration error: `{_fmt_optional_pct(summary['expected_calibration_error'])}`",
        "- Safety: read-only report over stored replay scores; no IBKR, OpenAI, order, or Lumibot action.",
        "",
        "## Filters",
        "",
    ]
    if filters:
        for key, value in filters.items():
            lines.append(f"- `{key}`: `{value if value is not None else '-'}`")
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "## Confidence bins",
            "",
            "| Confidence bin | Rows | Accuracy | Avg confidence | Confidence - accuracy |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for item in summary["bins"]:
        lines.append(
            f"| {item['bin']} | {item['rows']} | {_fmt_optional_pct(item['accuracy'])} "
            f"| {_fmt_optional_pct(item['avg_confidence'])} | {_fmt_optional_pct(item['calibration_gap'])} |"
        )
    lines.extend(
        [
            "",
            "## Direction distribution",
            "",
            "| Direction bucket | Rows | Hits |",
            "| --- | ---: | ---: |",
        ]
    )
    for key, value in sorted(summary["direction_distribution"].items()):
        lines.append(f"| {key} | {value['rows']} | {value['hits']} |")
    lines.extend(
        [
            "",
            "## Interpretation note",
            "",
            "Positive confidence-minus-accuracy means the scorer is overconfident on this sample. "
            "Negative values mean it is underconfident. Small samples should be treated as smoke evidence.",
            "",
        ]
    )
    return "\n".join(lines)


def write_calibration_markdown(
    output_path: Path,
    rows: list[dict[str, Any]],
    *,
    filters: dict[str, Any] | None = None,
) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(calibration_markdown(rows, filters=filters), encoding="utf-8")
    return output_path


def _trade_action_from_score(row: dict[str, Any]) -> tuple[str, str]:
    raw_json = row.get("raw_json") or "{}"
    try:
        raw = json.loads(str(raw_json))
    except json.JSONDecodeError:
        raw = {}
    debias = raw.get("debias") if isinstance(raw, dict) else {}
    if not isinstance(debias, dict):
        debias = {}
    action = str(debias.get("trade_action") or "").strip().lower()
    if not action:
        action = "observe_only" if debias.get("observe_only") is True else "directional_signal"
    reason = str(debias.get("observe_only_reason") or debias.get("method") or "").strip()
    return action, reason


def _signed_realized_return(row: dict[str, Any], key: str) -> float | None:
    direction = str(row.get("predicted_direction") or "").lower()
    if direction not in {"up", "down"}:
        return None
    value = row.get(key)
    if value is None:
        return None
    realized = float(value)
    return realized if direction == "up" else -realized


def _action_gate_group_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    evaluated = [row for row in rows if row.get("direction_hit") is not None]
    directional = [row for row in evaluated if str(row.get("predicted_direction") or "").lower() in {"up", "down"}]
    hits = sum(int(row["direction_hit"]) for row in evaluated)
    directional_hits = sum(int(row["direction_hit"]) for row in directional)
    raw_returns = [
        value
        for value in (_signed_realized_return(row, "actual_raw_return") for row in directional)
        if value is not None
    ]
    excess_returns = [
        value
        for value in (_signed_realized_return(row, "actual_excess_return") for row in directional)
        if value is not None
    ]
    confidence_values = [float(row.get("confidence") or 0.0) for row in evaluated]
    predicted_counts = Counter(str(row.get("predicted_direction") or "unknown") for row in rows)
    return {
        "rows": len(rows),
        "evaluated": len(evaluated),
        "hits": hits,
        "accuracy": (hits / len(evaluated)) if evaluated else None,
        "directional_rows": len(directional),
        "directional_hits": directional_hits,
        "directional_accuracy": (directional_hits / len(directional)) if directional else None,
        "avg_confidence": (sum(confidence_values) / len(confidence_values)) if confidence_values else None,
        "avg_signed_raw_return": (sum(raw_returns) / len(raw_returns)) if raw_returns else None,
        "avg_signed_excess_return": (sum(excess_returns) / len(excess_returns)) if excess_returns else None,
        "positive_signed_raw_rate": (
            sum(1 for value in raw_returns if value > 0.0) / len(raw_returns)
            if raw_returns
            else None
        ),
        "positive_signed_excess_rate": (
            sum(1 for value in excess_returns if value > 0.0) / len(excess_returns)
            if excess_returns
            else None
        ),
        "predicted_counts": dict(predicted_counts),
    }


def _group_action_gate_rows(
    rows: list[dict[str, Any]],
    key_fn: Any,
    *,
    min_rows: int = 1,
) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(str(key_fn(row) or "unknown"), []).append(row)
    result = []
    for bucket, members in groups.items():
        metrics = _action_gate_group_metrics(members)
        if metrics["rows"] < min_rows:
            continue
        result.append({"bucket": bucket, **metrics})
    result.sort(
        key=lambda item: (
            -int(item["directional_rows"]),
            -float(item["directional_accuracy"] if item["directional_accuracy"] is not None else -1.0),
            item["bucket"],
        )
    )
    return result


def action_gate_summary(
    rows: list[dict[str, Any]],
    *,
    min_group_rows: int = 1,
) -> dict[str, Any]:
    enriched = []
    for row in rows:
        action, reason = _trade_action_from_score(row)
        clone = dict(row)
        clone["_trade_action"] = action
        clone["_trade_action_reason"] = reason
        enriched.append(clone)

    all_directional = [
        row
        for row in enriched
        if str(row.get("predicted_direction") or "").lower() in {"up", "down"}
    ]
    tradeable_directional = [
        row for row in all_directional if row.get("_trade_action") != "observe_only"
    ]
    observe_only_directional = [
        row for row in all_directional if row.get("_trade_action") == "observe_only"
    ]
    all_metrics = _action_gate_group_metrics(enriched)
    all_directional_metrics = _action_gate_group_metrics(all_directional)
    tradeable_metrics = _action_gate_group_metrics(tradeable_directional)
    observe_only_metrics = _action_gate_group_metrics(observe_only_directional)
    coverage = (
        tradeable_metrics["directional_rows"] / all_directional_metrics["directional_rows"]
        if all_directional_metrics["directional_rows"]
        else None
    )
    return {
        "rows": len(enriched),
        "all_rows": all_metrics,
        "all_directional": all_directional_metrics,
        "tradeable_directional": tradeable_metrics,
        "observe_only_directional": observe_only_metrics,
        "directional_coverage_after_gate": coverage,
        "directional_accuracy_delta": (
            tradeable_metrics["directional_accuracy"] - all_directional_metrics["directional_accuracy"]
            if tradeable_metrics["directional_accuracy"] is not None
            and all_directional_metrics["directional_accuracy"] is not None
            else None
        ),
        "signed_excess_return_delta": (
            tradeable_metrics["avg_signed_excess_return"] - all_directional_metrics["avg_signed_excess_return"]
            if tradeable_metrics["avg_signed_excess_return"] is not None
            and all_directional_metrics["avg_signed_excess_return"] is not None
            else None
        ),
        "by_trade_action": _group_action_gate_rows(
            enriched,
            lambda row: row.get("_trade_action"),
            min_rows=min_group_rows,
        ),
        "by_horizon_and_action": _group_action_gate_rows(
            enriched,
            lambda row: f"{row.get('horizon') or 'unknown'}:{row.get('_trade_action')}",
            min_rows=min_group_rows,
        ),
        "by_reason": _group_action_gate_rows(
            [row for row in enriched if row.get("_trade_action") == "observe_only"],
            lambda row: row.get("_trade_action_reason") or "observe_only",
            min_rows=min_group_rows,
        ),
    }


def format_action_gate(summary: dict[str, Any]) -> str:
    all_directional = summary["all_directional"]
    tradeable = summary["tradeable_directional"]
    observe_only = summary["observe_only_directional"]
    return (
        "Replay score action-gate analysis:\n"
        f"rows={summary['rows']} "
        f"all_directional={all_directional['directional_hits']}/{all_directional['directional_rows']} "
        f"acc={_fmt_optional_pct(all_directional['directional_accuracy'])} "
        f"tradeable={tradeable['directional_hits']}/{tradeable['directional_rows']} "
        f"acc={_fmt_optional_pct(tradeable['directional_accuracy'])} "
        f"observe_only_directional={observe_only['directional_hits']}/{observe_only['directional_rows']} "
        f"acc={_fmt_optional_pct(observe_only['directional_accuracy'])} "
        f"coverage={_fmt_optional_pct(summary['directional_coverage_after_gate'])} "
        f"accuracy_delta={_fmt_optional_pct(summary['directional_accuracy_delta'])}"
    )


def _append_action_gate_table(lines: list[str], title: str, rows: list[dict[str, Any]]) -> None:
    lines.extend(
        [
            "",
            f"## {title}",
            "",
            "| Bucket | Rows | Directional rows | Directional hits | Directional acc | Avg signed raw | Avg signed excess | Positive excess rate | Avg confidence | Pred up/down/flat |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    if not rows:
        lines.append("| - | 0 | 0 | 0 | - | - | - | - | - | - |")
        return
    for row in rows:
        predicted = row.get("predicted_counts") or {}
        lines.append(
            f"| {row['bucket']} | {row['rows']} | {row['directional_rows']} | {row['directional_hits']} "
            f"| {_fmt_optional_pct(row['directional_accuracy'])} "
            f"| {_fmt_optional_signed_pct(row['avg_signed_raw_return'])} "
            f"| {_fmt_optional_signed_pct(row['avg_signed_excess_return'])} "
            f"| {_fmt_optional_pct(row['positive_signed_excess_rate'])} "
            f"| {_fmt_optional_pct(row['avg_confidence'])} "
            f"| {int(predicted.get('up', 0))}/{int(predicted.get('down', 0))}/{int(predicted.get('flat', 0))} |"
        )


def action_gate_markdown(
    rows: list[dict[str, Any]],
    *,
    filters: dict[str, Any] | None = None,
    min_group_rows: int = 1,
    title: str = "Replay Score Action-Gate Report",
) -> str:
    generated_at = datetime.now(UTC).isoformat()
    filters = filters or {}
    summary = action_gate_summary(rows, min_group_rows=min_group_rows)
    all_directional = summary["all_directional"]
    tradeable = summary["tradeable_directional"]
    observe_only = summary["observe_only_directional"]
    lines = [
        f"# {title}",
        "",
        f"- Generated at: `{generated_at}`",
        f"- Rows: `{summary['rows']}`",
        f"- All directional accuracy: `{all_directional['directional_hits']}/{all_directional['directional_rows']} = {_fmt_optional_pct(all_directional['directional_accuracy'])}`",
        f"- Tradeable directional accuracy after gate: `{tradeable['directional_hits']}/{tradeable['directional_rows']} = {_fmt_optional_pct(tradeable['directional_accuracy'])}`",
        f"- Observe-only directional accuracy: `{observe_only['directional_hits']}/{observe_only['directional_rows']} = {_fmt_optional_pct(observe_only['directional_accuracy'])}`",
        f"- Directional coverage after gate: `{_fmt_optional_pct(summary['directional_coverage_after_gate'])}`",
        f"- Directional accuracy delta after gate: `{_fmt_optional_pct(summary['directional_accuracy_delta'])}`",
        f"- Avg signed excess-return delta after gate: `{_fmt_optional_signed_pct(summary['signed_excess_return_delta'])}`",
        "- Return convention: predicted up is treated as long; predicted down is treated as short; flat rows are excluded from signed-return averages.",
        "- Safety: read-only action-gate analysis over stored `replay_model_scores`; no IBKR, OpenAI, order, or Lumibot action.",
        "",
        "## Filters",
        "",
    ]
    if filters:
        for key, value in filters.items():
            lines.append(f"- `{key}`: `{value if value is not None else '-'}`")
    else:
        lines.append("- none")
    _append_action_gate_table(
        lines,
        "Core groups",
        [
            {"bucket": "all_rows", **summary["all_rows"]},
            {"bucket": "all_directional", **all_directional},
            {"bucket": "tradeable_directional", **tradeable},
            {"bucket": "observe_only_directional", **observe_only},
        ],
    )
    _append_action_gate_table(lines, "By trade action", summary["by_trade_action"])
    _append_action_gate_table(lines, "By horizon and action", summary["by_horizon_and_action"])
    _append_action_gate_table(lines, "Observe-only reasons", summary["by_reason"])
    lines.extend(
        [
            "",
            "## Interpretation note",
            "",
            "A useful gate should generally raise tradeable directional accuracy and/or signed realized return while excluding enough weak rows to matter. This report evaluates stored historical labels only; it is not live-trading authorization.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_action_gate_markdown(
    output_path: Path,
    rows: list[dict[str, Any]],
    *,
    filters: dict[str, Any] | None = None,
    min_group_rows: int = 1,
) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        action_gate_markdown(rows, filters=filters, min_group_rows=min_group_rows),
        encoding="utf-8",
    )
    return output_path


def _paper_action_from_score(
    row: dict[str, Any],
    *,
    min_confidence: float = 0.0,
    normal_size: float = 1.0,
    haircut_size_multiplier: float = 0.50,
) -> dict[str, Any]:
    trade_action, reason = _trade_action_from_score(row)
    predicted = str(row.get("predicted_direction") or "").lower()
    confidence = float(row.get("confidence") or 0.0)
    expected_return = float(row.get("expected_return") or 0.0)
    raw_signed = _signed_realized_return(row, "actual_raw_return")
    excess_signed = _signed_realized_return(row, "actual_excess_return")
    side = "long" if predicted == "up" else "short" if predicted == "down" else "none"
    decision = "paper_trade"
    block_reason = ""
    size_multiplier = float(normal_size)
    if predicted == "flat":
        decision = "no_trade"
        block_reason = "flat_prediction"
        size_multiplier = 0.0
    elif trade_action == "observe_only":
        decision = "review_only_no_trade"
        block_reason = reason or "observe_only"
        size_multiplier = 0.0
    elif confidence < float(min_confidence):
        decision = "no_trade_low_confidence"
        block_reason = f"confidence_below_{float(min_confidence):.2f}"
        size_multiplier = 0.0
    elif trade_action == "confidence_haircut":
        decision = "paper_trade_reduced_size"
        block_reason = ""
        size_multiplier = float(normal_size) * float(haircut_size_multiplier)
    elif trade_action not in {"directional_signal", ""}:
        decision = f"paper_trade_{trade_action}"
    return {
        "score_id": row.get("id") or row.get("score_id"),
        "outcome_id": row.get("outcome_id"),
        "symbol": row.get("symbol"),
        "horizon": row.get("horizon"),
        "price_source": row.get("price_source"),
        "replay_timestamp": row.get("replay_timestamp"),
        "predicted_direction": predicted,
        "actual_direction": row.get("actual_direction"),
        "trade_action": trade_action,
        "trade_action_reason": reason,
        "paper_decision": decision,
        "block_reason": block_reason,
        "side": side,
        "size_multiplier": size_multiplier,
        "confidence": confidence,
        "expected_return": expected_return,
        "direction_hit": row.get("direction_hit"),
        "signed_raw_return": raw_signed,
        "signed_excess_return": excess_signed,
        "weighted_signed_raw_return": raw_signed * size_multiplier if raw_signed is not None else None,
        "weighted_signed_excess_return": excess_signed * size_multiplier if excess_signed is not None else None,
    }


def _paper_decision_group_metrics(decisions: list[dict[str, Any]]) -> dict[str, Any]:
    traded = [item for item in decisions if float(item.get("size_multiplier") or 0.0) > 0.0]
    evaluated = [item for item in decisions if item.get("direction_hit") is not None]
    traded_evaluated = [item for item in traded if item.get("direction_hit") is not None]
    hits = sum(int(item.get("direction_hit") or 0) for item in evaluated)
    traded_hits = sum(int(item.get("direction_hit") or 0) for item in traded_evaluated)
    signed_excess = [
        float(item["signed_excess_return"])
        for item in traded
        if item.get("signed_excess_return") is not None
    ]
    weighted_excess = [
        float(item["weighted_signed_excess_return"])
        for item in traded
        if item.get("weighted_signed_excess_return") is not None
    ]
    signed_raw = [
        float(item["signed_raw_return"])
        for item in traded
        if item.get("signed_raw_return") is not None
    ]
    weighted_raw = [
        float(item["weighted_signed_raw_return"])
        for item in traded
        if item.get("weighted_signed_raw_return") is not None
    ]
    sizes = [float(item.get("size_multiplier") or 0.0) for item in traded]
    return {
        "rows": len(decisions),
        "evaluated": len(evaluated),
        "directional_hits": hits,
        "directional_accuracy": hits / len(evaluated) if evaluated else None,
        "paper_trade_rows": len(traded),
        "paper_trade_hits": traded_hits,
        "paper_trade_accuracy": traded_hits / len(traded_evaluated) if traded_evaluated else None,
        "paper_trade_rate": len(traded) / len(decisions) if decisions else None,
        "avg_size_multiplier": sum(sizes) / len(sizes) if sizes else None,
        "gross_size_units": sum(sizes),
        "avg_trade_signed_raw_return": sum(signed_raw) / len(signed_raw) if signed_raw else None,
        "avg_trade_signed_excess_return": sum(signed_excess) / len(signed_excess) if signed_excess else None,
        "avg_weighted_signed_raw_return": sum(weighted_raw) / len(weighted_raw) if weighted_raw else None,
        "avg_weighted_signed_excess_return": sum(weighted_excess) / len(weighted_excess) if weighted_excess else None,
        "total_weighted_signed_raw_return": sum(weighted_raw) if weighted_raw else None,
        "total_weighted_signed_excess_return": sum(weighted_excess) if weighted_excess else None,
        "positive_trade_excess_rate": (
            sum(1 for value in signed_excess if value > 0.0) / len(signed_excess)
            if signed_excess
            else None
        ),
    }


def _group_paper_decisions(
    decisions: list[dict[str, Any]],
    key_fn: Any,
    *,
    min_rows: int = 1,
) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in decisions:
        groups.setdefault(str(key_fn(item) or "unknown"), []).append(item)
    rows = []
    for bucket, members in groups.items():
        metrics = _paper_decision_group_metrics(members)
        if metrics["rows"] < min_rows:
            continue
        rows.append({"bucket": bucket, **metrics})
    rows.sort(
        key=lambda item: (
            -int(item["paper_trade_rows"]),
            -float(item["paper_trade_accuracy"] if item["paper_trade_accuracy"] is not None else -1.0),
            item["bucket"],
        )
    )
    return rows


def paper_action_simulation_summary(
    rows: list[dict[str, Any]],
    *,
    min_confidence: float = 0.0,
    normal_size: float = 1.0,
    haircut_size_multiplier: float = 0.50,
    min_group_rows: int = 1,
) -> dict[str, Any]:
    decisions = [
        _paper_action_from_score(
            row,
            min_confidence=min_confidence,
            normal_size=normal_size,
            haircut_size_multiplier=haircut_size_multiplier,
        )
        for row in rows
    ]
    traded = [item for item in decisions if float(item.get("size_multiplier") or 0.0) > 0.0]
    skipped = [item for item in decisions if float(item.get("size_multiplier") or 0.0) <= 0.0]
    return {
        "rows": len(rows),
        "parameters": {
            "min_confidence": float(min_confidence),
            "normal_size": float(normal_size),
            "haircut_size_multiplier": float(haircut_size_multiplier),
        },
        "all_decisions": _paper_decision_group_metrics(decisions),
        "paper_trades": _paper_decision_group_metrics(traded),
        "no_trade_or_review": _paper_decision_group_metrics(skipped),
        "by_paper_decision": _group_paper_decisions(decisions, lambda item: item.get("paper_decision"), min_rows=min_group_rows),
        "by_trade_action": _group_paper_decisions(decisions, lambda item: item.get("trade_action"), min_rows=min_group_rows),
        "by_horizon_and_decision": _group_paper_decisions(
            decisions,
            lambda item: f"{item.get('horizon') or 'unknown'}:{item.get('paper_decision')}",
            min_rows=min_group_rows,
        ),
        "sample_decisions": decisions[:20],
        "safety": {
            "mode": "read_only_paper_action_simulation",
            "policy": "No OpenAI, IBKR account, portfolio, order, or scorer-row mutation.",
        },
    }


def _append_paper_decision_table(lines: list[str], title: str, rows: list[dict[str, Any]]) -> None:
    lines.extend(
        [
            "",
            f"## {title}",
            "",
            "| Bucket | Rows | Paper trades | Trade rate | Trade hits | Trade acc | Avg size | Avg signed excess | Avg weighted excess | Total weighted excess | Positive excess rate |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    if not rows:
        lines.append("| - | 0 | 0 | - | 0 | - | - | - | - | - | - |")
        return
    for row in rows:
        lines.append(
            f"| {row['bucket']} | {row['rows']} | {row['paper_trade_rows']} "
            f"| {_fmt_optional_pct(row['paper_trade_rate'])} "
            f"| {row['paper_trade_hits']} "
            f"| {_fmt_optional_pct(row['paper_trade_accuracy'])} "
            f"| {_fmt_optional_pct(row['avg_size_multiplier'])} "
            f"| {_fmt_optional_signed_pct(row['avg_trade_signed_excess_return'])} "
            f"| {_fmt_optional_signed_pct(row['avg_weighted_signed_excess_return'])} "
            f"| {_fmt_optional_signed_pct(row['total_weighted_signed_excess_return'])} "
            f"| {_fmt_optional_pct(row['positive_trade_excess_rate'])} |"
        )


def paper_action_simulation_markdown(
    summary: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
    title: str = "Replay Score Paper Action Simulation Report",
) -> str:
    filters = filters or {}
    all_decisions = summary.get("all_decisions") or {}
    paper_trades = summary.get("paper_trades") or {}
    no_trade = summary.get("no_trade_or_review") or {}
    params = summary.get("parameters") or {}
    lines = [
        f"# {title}",
        "",
        f"- Generated at: `{datetime.now(UTC).isoformat()}`",
        f"- Rows: `{summary.get('rows')}`",
        f"- Paper trade rows: `{paper_trades.get('paper_trade_rows')}`",
        f"- Paper trade accuracy: `{paper_trades.get('paper_trade_hits')}/{paper_trades.get('evaluated')} = {_fmt_optional_pct(paper_trades.get('paper_trade_accuracy'))}`",
        f"- Paper trade rate: `{_fmt_optional_pct(all_decisions.get('paper_trade_rate'))}`",
        f"- Avg paper-trade signed excess return: `{_fmt_optional_signed_pct(paper_trades.get('avg_trade_signed_excess_return'))}`",
        f"- Avg weighted signed excess return: `{_fmt_optional_signed_pct(paper_trades.get('avg_weighted_signed_excess_return'))}`",
        f"- Total weighted signed excess return units: `{_fmt_optional_signed_pct(paper_trades.get('total_weighted_signed_excess_return'))}`",
        f"- No-trade/review rows: `{no_trade.get('rows')}`",
        f"- Parameters: min_confidence=`{params.get('min_confidence')}`, normal_size=`{params.get('normal_size')}`, haircut_size_multiplier=`{params.get('haircut_size_multiplier')}`",
        "- Safety: read-only paper simulation over stored `replay_model_scores`; no OpenAI, IBKR account, portfolio, order, or scorer mutation.",
        "",
        "## Filters",
        "",
    ]
    if filters:
        for key, value in filters.items():
            lines.append(f"- `{key}`: `{value if value is not None else '-'}`")
    else:
        lines.append("- none")
    _append_paper_decision_table(
        lines,
        "Core groups",
        [
            {"bucket": "all_decisions", **all_decisions},
            {"bucket": "paper_trades", **paper_trades},
            {"bucket": "no_trade_or_review", **no_trade},
        ],
    )
    _append_paper_decision_table(lines, "By paper decision", summary.get("by_paper_decision") or [])
    _append_paper_decision_table(lines, "By trade action", summary.get("by_trade_action") or [])
    _append_paper_decision_table(lines, "By horizon and decision", summary.get("by_horizon_and_decision") or [])
    lines.extend(
        [
            "",
            "## Interpretation note",
            "",
            "This is the first operating-shape simulation: it converts stored scorer/action-gate outputs into paper decisions and evaluates the historical result. It is intentionally not an order router. A future stage can add latency windows, asset liquidity rules, portfolio exposure, and paper order objects.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_paper_action_simulation_markdown(
    output_path: Path,
    summary: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        paper_action_simulation_markdown(summary, filters=filters),
        encoding="utf-8",
    )
    return output_path


def _paper_order_id(row: dict[str, Any], decision: dict[str, Any]) -> str:
    parts = [
        "paper",
        str(row.get("scorer_version") or "unknown-version"),
        str(decision.get("outcome_id") or row.get("outcome_id") or "0"),
        str(decision.get("horizon") or row.get("horizon") or "unknown-horizon"),
        str(row.get("id") or row.get("score_id") or decision.get("score_id") or "0"),
    ]
    cleaned = [re.sub(r"[^A-Za-z0-9_.-]+", "-", part).strip("-") for part in parts]
    return "-".join(item for item in cleaned if item)


def _paper_order_object_from_score(
    row: dict[str, Any],
    *,
    min_confidence: float = 0.0,
    normal_size: float = 1.0,
    haircut_size_multiplier: float = 0.50,
    base_quantity: float = 1.0,
    stop_loss_pct: float | None = None,
    take_profit_pct: float | None = None,
) -> dict[str, Any]:
    decision = _paper_action_from_score(
        row,
        min_confidence=min_confidence,
        normal_size=normal_size,
        haircut_size_multiplier=haircut_size_multiplier,
    )
    size_multiplier = float(decision.get("size_multiplier") or 0.0)
    side = str(decision.get("side") or "none")
    order_action = "BUY" if side == "long" else "SELL" if side == "short" else "NONE"
    quantity_units = float(base_quantity) * size_multiplier
    is_candidate = size_multiplier > 0.0 and order_action in {"BUY", "SELL"}
    trade_action = str(decision.get("trade_action") or "")
    sizing_policy = (
        "confidence_haircut_reduced_size"
        if trade_action == "confidence_haircut"
        else "normal_size"
        if is_candidate
        else "blocked_no_size"
    )
    return {
        "paper_order_id": _paper_order_id(row, decision),
        "paper_only": True,
        "order_status": "candidate" if is_candidate else "blocked",
        "order_action": order_action,
        "order_type": "paper_market_on_signal",
        "time_in_force": "paper_only",
        "symbol": decision.get("symbol"),
        "side": side,
        "quantity_units": quantity_units,
        "base_quantity": float(base_quantity),
        "size_multiplier": size_multiplier,
        "sizing_policy": sizing_policy,
        "stop_loss_pct": stop_loss_pct,
        "take_profit_pct": take_profit_pct,
        "exit_horizon": decision.get("horizon"),
        "signal": {
            "score_id": decision.get("score_id"),
            "outcome_id": decision.get("outcome_id"),
            "scorer_name": row.get("scorer_name"),
            "model_name": row.get("model_name"),
            "scorer_version": row.get("scorer_version"),
            "price_source": decision.get("price_source"),
            "replay_timestamp": decision.get("replay_timestamp"),
            "horizon": decision.get("horizon"),
            "predicted_direction": decision.get("predicted_direction"),
            "expected_return": decision.get("expected_return"),
            "confidence": decision.get("confidence"),
            "trade_action": trade_action,
            "trade_action_reason": decision.get("trade_action_reason"),
            "paper_decision": decision.get("paper_decision"),
            "block_reason": decision.get("block_reason"),
        },
        "paper_evaluation": {
            "actual_direction": decision.get("actual_direction"),
            "direction_hit": decision.get("direction_hit"),
            "signed_raw_return": decision.get("signed_raw_return"),
            "signed_excess_return": decision.get("signed_excess_return"),
            "weighted_signed_raw_return": decision.get("weighted_signed_raw_return"),
            "weighted_signed_excess_return": decision.get("weighted_signed_excess_return"),
        },
        "safety": {
            "ibkr_order_submitted": False,
            "account_or_portfolio_checked": False,
            "mutation": "none",
        },
    }


def _paper_order_group_metrics(orders: list[dict[str, Any]]) -> dict[str, Any]:
    candidates = [order for order in orders if order.get("order_status") == "candidate"]
    evaluated = [
        order
        for order in candidates
        if (order.get("paper_evaluation") or {}).get("direction_hit") is not None
    ]
    hits = sum(int((order.get("paper_evaluation") or {}).get("direction_hit") or 0) for order in evaluated)
    quantities = [float(order.get("quantity_units") or 0.0) for order in candidates]
    signed_excess = [
        float((order.get("paper_evaluation") or {}).get("signed_excess_return"))
        for order in candidates
        if (order.get("paper_evaluation") or {}).get("signed_excess_return") is not None
    ]
    weighted_excess = [
        float((order.get("paper_evaluation") or {}).get("weighted_signed_excess_return"))
        for order in candidates
        if (order.get("paper_evaluation") or {}).get("weighted_signed_excess_return") is not None
    ]
    return {
        "orders": len(orders),
        "candidate_orders": len(candidates),
        "blocked_orders": len(orders) - len(candidates),
        "candidate_rate": len(candidates) / len(orders) if orders else None,
        "evaluated_candidates": len(evaluated),
        "candidate_hits": hits,
        "candidate_accuracy": hits / len(evaluated) if evaluated else None,
        "gross_quantity_units": sum(quantities),
        "avg_quantity_units": sum(quantities) / len(quantities) if quantities else None,
        "avg_signed_excess_return": sum(signed_excess) / len(signed_excess) if signed_excess else None,
        "avg_weighted_signed_excess_return": sum(weighted_excess) / len(weighted_excess) if weighted_excess else None,
        "total_weighted_signed_excess_return": sum(weighted_excess) if weighted_excess else None,
        "positive_excess_rate": (
            sum(1 for value in signed_excess if value > 0.0) / len(signed_excess)
            if signed_excess
            else None
        ),
    }


def _group_paper_orders(
    orders: list[dict[str, Any]],
    key_fn: Any,
    *,
    min_rows: int = 1,
) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for order in orders:
        groups.setdefault(str(key_fn(order) or "unknown"), []).append(order)
    rows = []
    for bucket, members in groups.items():
        metrics = _paper_order_group_metrics(members)
        if metrics["orders"] < min_rows:
            continue
        rows.append({"bucket": bucket, **metrics})
    rows.sort(
        key=lambda item: (
            -int(item["candidate_orders"]),
            -float(item["candidate_accuracy"] if item["candidate_accuracy"] is not None else -1.0),
            item["bucket"],
        )
    )
    return rows


def paper_order_objects_summary(
    rows: list[dict[str, Any]],
    *,
    min_confidence: float = 0.0,
    normal_size: float = 1.0,
    haircut_size_multiplier: float = 0.50,
    base_quantity: float = 1.0,
    stop_loss_pct: float | None = None,
    take_profit_pct: float | None = None,
    min_group_rows: int = 1,
    sample_limit: int = 20,
) -> dict[str, Any]:
    orders = [
        _paper_order_object_from_score(
            row,
            min_confidence=min_confidence,
            normal_size=normal_size,
            haircut_size_multiplier=haircut_size_multiplier,
            base_quantity=base_quantity,
            stop_loss_pct=stop_loss_pct,
            take_profit_pct=take_profit_pct,
        )
        for row in rows
    ]
    candidates = [order for order in orders if order.get("order_status") == "candidate"]
    blocked = [order for order in orders if order.get("order_status") != "candidate"]
    return {
        "rows": len(rows),
        "orders": len(orders),
        "parameters": {
            "min_confidence": float(min_confidence),
            "normal_size": float(normal_size),
            "haircut_size_multiplier": float(haircut_size_multiplier),
            "base_quantity": float(base_quantity),
            "stop_loss_pct": stop_loss_pct,
            "take_profit_pct": take_profit_pct,
        },
        "all_orders": _paper_order_group_metrics(orders),
        "candidate_orders": _paper_order_group_metrics(candidates),
        "blocked_orders": _paper_order_group_metrics(blocked),
        "by_order_status": _group_paper_orders(orders, lambda order: order.get("order_status"), min_rows=min_group_rows),
        "by_order_action": _group_paper_orders(orders, lambda order: order.get("order_action"), min_rows=min_group_rows),
        "by_sizing_policy": _group_paper_orders(orders, lambda order: order.get("sizing_policy"), min_rows=min_group_rows),
        "by_horizon_and_status": _group_paper_orders(
            orders,
            lambda order: f"{(order.get('signal') or {}).get('horizon') or 'unknown'}:{order.get('order_status')}",
            min_rows=min_group_rows,
        ),
        "sample_orders": orders[: max(0, int(sample_limit))],
        "paper_orders": orders,
        "safety": {
            "mode": "read_only_paper_order_object_generation",
            "policy": "No OpenAI, IBKR account, portfolio, order submission, or scorer-row mutation.",
        },
    }


def _append_paper_order_table(lines: list[str], title: str, rows: list[dict[str, Any]]) -> None:
    lines.extend(
        [
            "",
            f"## {title}",
            "",
            "| Bucket | Orders | Candidates | Blocked | Candidate rate | Hits | Accuracy | Gross qty | Avg qty | Avg signed excess | Avg weighted excess | Total weighted excess |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    if not rows:
        lines.append("| - | 0 | 0 | 0 | - | 0 | - | - | - | - | - | - |")
        return
    for row in rows:
        lines.append(
            f"| {row['bucket']} | {row['orders']} | {row['candidate_orders']} | {row['blocked_orders']} "
            f"| {_fmt_optional_pct(row['candidate_rate'])} "
            f"| {row['candidate_hits']} "
            f"| {_fmt_optional_pct(row['candidate_accuracy'])} "
            f"| {float(row['gross_quantity_units'] or 0.0):.2f} "
            f"| {float(row['avg_quantity_units'] or 0.0):.2f} "
            f"| {_fmt_optional_signed_pct(row['avg_signed_excess_return'])} "
            f"| {_fmt_optional_signed_pct(row['avg_weighted_signed_excess_return'])} "
            f"| {_fmt_optional_signed_pct(row['total_weighted_signed_excess_return'])} |"
        )


def paper_order_objects_markdown(
    summary: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
    title: str = "Replay Score Paper Order Object Report",
) -> str:
    filters = filters or {}
    candidates = summary.get("candidate_orders") or {}
    blocked = summary.get("blocked_orders") or {}
    params = summary.get("parameters") or {}
    lines = [
        f"# {title}",
        "",
        f"- Generated at: `{datetime.now(UTC).isoformat()}`",
        f"- Rows: `{summary.get('rows')}`",
        f"- Paper order objects: `{summary.get('orders')}`",
        f"- Candidate paper orders: `{candidates.get('candidate_orders')}`",
        f"- Blocked paper orders: `{blocked.get('orders')}`",
        f"- Candidate accuracy: `{candidates.get('candidate_hits')}/{candidates.get('evaluated_candidates')} = {_fmt_optional_pct(candidates.get('candidate_accuracy'))}`",
        f"- Gross quantity units: `{float(candidates.get('gross_quantity_units') or 0.0):.2f}`",
        f"- Avg candidate signed excess return: `{_fmt_optional_signed_pct(candidates.get('avg_signed_excess_return'))}`",
        f"- Avg weighted signed excess return: `{_fmt_optional_signed_pct(candidates.get('avg_weighted_signed_excess_return'))}`",
        f"- Parameters: base_quantity=`{params.get('base_quantity')}`, normal_size=`{params.get('normal_size')}`, haircut_size_multiplier=`{params.get('haircut_size_multiplier')}`, stop_loss_pct=`{params.get('stop_loss_pct')}`, take_profit_pct=`{params.get('take_profit_pct')}`",
        "- Safety: read-only paper order objects only; no OpenAI, IBKR account, portfolio, order submission, or scorer mutation.",
        "",
        "## Filters",
        "",
    ]
    if filters:
        for key, value in filters.items():
            lines.append(f"- `{key}`: `{value if value is not None else '-'}`")
    else:
        lines.append("- none")
    _append_paper_order_table(
        lines,
        "Core groups",
        [
            {"bucket": "all_orders", **(summary.get("all_orders") or {})},
            {"bucket": "candidate_orders", **candidates},
            {"bucket": "blocked_orders", **blocked},
        ],
    )
    _append_paper_order_table(lines, "By order status", summary.get("by_order_status") or [])
    _append_paper_order_table(lines, "By order action", summary.get("by_order_action") or [])
    _append_paper_order_table(lines, "By sizing policy", summary.get("by_sizing_policy") or [])
    _append_paper_order_table(lines, "By horizon and status", summary.get("by_horizon_and_status") or [])
    lines.extend(
        [
            "",
            "## Sample paper order objects",
            "",
            "| Paper order id | Status | Action | Symbol | Side | Qty units | Sizing | Confidence | Reason |",
            "| --- | --- | --- | --- | --- | ---: | --- | ---: | --- |",
        ]
    )
    for order in summary.get("sample_orders") or []:
        signal = order.get("signal") or {}
        reason = signal.get("block_reason") or signal.get("trade_action_reason") or signal.get("trade_action") or "-"
        lines.append(
            f"| {order.get('paper_order_id')} | {order.get('order_status')} | {order.get('order_action')} "
            f"| {order.get('symbol')} | {order.get('side')} | {float(order.get('quantity_units') or 0.0):.2f} "
            f"| {order.get('sizing_policy')} | {_fmt_optional_pct(signal.get('confidence'))} | {reason} |"
        )
    lines.extend(
        [
            "",
            "## Interpretation note",
            "",
            "These objects are intentionally paper-only order candidates. They make the later order-router integration easier to audit, but they are not executable IBKR orders and do not check account, position, margin, liquidity, or risk limits yet.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_paper_order_objects_markdown(
    output_path: Path,
    summary: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        paper_order_objects_markdown(summary, filters=filters),
        encoding="utf-8",
    )
    return output_path


def _paper_order_optimizer_score(
    summary: dict[str, Any],
    *,
    min_candidate_rate: float,
    coverage_weight: float,
    weighted_return_weight: float,
    quantity_penalty_weight: float,
) -> float:
    candidates = summary.get("candidate_orders") or {}
    all_orders = summary.get("all_orders") or {}
    accuracy = float(candidates.get("candidate_accuracy") or 0.0)
    candidate_rate = float(all_orders.get("candidate_rate") or 0.0)
    avg_weighted = float(candidates.get("avg_weighted_signed_excess_return") or 0.0)
    avg_qty = float(candidates.get("avg_quantity_units") or 0.0)
    shortfall = max(0.0, float(min_candidate_rate) - candidate_rate)
    return (
        accuracy
        + float(coverage_weight) * candidate_rate
        + float(weighted_return_weight) * avg_weighted
        - float(quantity_penalty_weight) * avg_qty
        - 0.75 * shortfall
    )


def tune_paper_order_parameters(
    rows: list[dict[str, Any]],
    *,
    min_confidence_grid: tuple[float, ...] = (0.0, 0.30, 0.35, 0.40, 0.45),
    haircut_size_grid: tuple[float, ...] = (0.25, 0.50, 0.75),
    normal_size_grid: tuple[float, ...] = (1.0,),
    base_quantity: float = 1.0,
    min_candidate_rate: float = 0.50,
    coverage_weight: float = 0.08,
    weighted_return_weight: float = 2.0,
    quantity_penalty_weight: float = 0.02,
    top_n: int = 20,
) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    for min_confidence in min_confidence_grid:
        for haircut_size in haircut_size_grid:
            for normal_size in normal_size_grid:
                summary = paper_order_objects_summary(
                    rows,
                    min_confidence=float(min_confidence),
                    normal_size=float(normal_size),
                    haircut_size_multiplier=float(haircut_size),
                    base_quantity=float(base_quantity),
                    min_group_rows=1,
                    sample_limit=0,
                )
                candidate_orders = summary.get("candidate_orders") or {}
                all_orders = summary.get("all_orders") or {}
                score = _paper_order_optimizer_score(
                    summary,
                    min_candidate_rate=min_candidate_rate,
                    coverage_weight=coverage_weight,
                    weighted_return_weight=weighted_return_weight,
                    quantity_penalty_weight=quantity_penalty_weight,
                )
                candidates.append(
                    {
                        "params": {
                            "min_confidence": float(min_confidence),
                            "haircut_size_multiplier": float(haircut_size),
                            "normal_size": float(normal_size),
                            "base_quantity": float(base_quantity),
                        },
                        "score": score,
                        "passes_min_candidate_rate": float(all_orders.get("candidate_rate") or 0.0) >= float(min_candidate_rate),
                        "candidate_orders": candidate_orders.get("candidate_orders"),
                        "blocked_orders": summary.get("blocked_orders", {}).get("orders"),
                        "candidate_rate": all_orders.get("candidate_rate"),
                        "candidate_accuracy": candidate_orders.get("candidate_accuracy"),
                        "candidate_hits": candidate_orders.get("candidate_hits"),
                        "evaluated_candidates": candidate_orders.get("evaluated_candidates"),
                        "gross_quantity_units": candidate_orders.get("gross_quantity_units"),
                        "avg_quantity_units": candidate_orders.get("avg_quantity_units"),
                        "avg_signed_excess_return": candidate_orders.get("avg_signed_excess_return"),
                        "avg_weighted_signed_excess_return": candidate_orders.get("avg_weighted_signed_excess_return"),
                        "total_weighted_signed_excess_return": candidate_orders.get("total_weighted_signed_excess_return"),
                        "by_sizing_policy": summary.get("by_sizing_policy"),
                    }
                )
    candidates.sort(
        key=lambda item: (
            not bool(item["passes_min_candidate_rate"]),
            -float(item["score"]),
            -float(item["candidate_accuracy"] or 0.0),
            -float(item["candidate_rate"] or 0.0),
            -float(item["avg_weighted_signed_excess_return"] or 0.0),
        )
    )
    selected = candidates[0] if candidates else None
    selected_summary = None
    if selected is not None:
        params = selected["params"]
        selected_summary = paper_order_objects_summary(
            rows,
            min_confidence=float(params["min_confidence"]),
            normal_size=float(params["normal_size"]),
            haircut_size_multiplier=float(params["haircut_size_multiplier"]),
            base_quantity=float(params["base_quantity"]),
            min_group_rows=1,
            sample_limit=20,
        )
    return {
        "rows": len(rows),
        "grid": {
            "min_confidence_grid": list(min_confidence_grid),
            "haircut_size_grid": list(haircut_size_grid),
            "normal_size_grid": list(normal_size_grid),
            "base_quantity": float(base_quantity),
            "min_candidate_rate": float(min_candidate_rate),
            "coverage_weight": float(coverage_weight),
            "weighted_return_weight": float(weighted_return_weight),
            "quantity_penalty_weight": float(quantity_penalty_weight),
        },
        "candidates_evaluated": len(candidates),
        "selected": selected,
        "selected_summary": selected_summary,
        "top_candidates": candidates[: max(0, int(top_n))],
        "safety": {
            "mode": "read_only_paper_order_parameter_tuning",
            "policy": "No OpenAI, IBKR account, portfolio, order submission, or scorer-row mutation.",
        },
    }


def paper_order_parameter_tuning_markdown(
    result: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
    title: str = "Paper Order Parameter Tuning Report",
) -> str:
    filters = filters or {}
    selected = result.get("selected") or {}
    selected_params = selected.get("params") or {}
    lines = [
        f"# {title}",
        "",
        f"- Generated at: `{datetime.now(UTC).isoformat()}`",
        f"- Rows: `{result.get('rows')}`",
        f"- Candidates evaluated: `{result.get('candidates_evaluated')}`",
        f"- Selected min confidence: `{selected_params.get('min_confidence', '-')}`",
        f"- Selected haircut size multiplier: `{selected_params.get('haircut_size_multiplier', '-')}`",
        f"- Selected normal size: `{selected_params.get('normal_size', '-')}`",
        f"- Selected score: `{float(selected.get('score') or 0.0):.4f}`",
        f"- Selected candidate accuracy: `{selected.get('candidate_hits')}/{selected.get('evaluated_candidates')} = {_fmt_optional_pct(selected.get('candidate_accuracy'))}`",
        f"- Selected candidate rate: `{_fmt_optional_pct(selected.get('candidate_rate'))}`",
        f"- Selected avg weighted signed excess return: `{_fmt_optional_signed_pct(selected.get('avg_weighted_signed_excess_return'))}`",
        "- Safety: read-only parameter tuning over stored `replay_model_scores`; no OpenAI, IBKR account, portfolio, order submission, or scorer mutation.",
        "",
        "## Filters",
        "",
    ]
    if filters:
        for key, value in filters.items():
            lines.append(f"- `{key}`: `{value if value is not None else '-'}`")
    else:
        lines.append("- none")
    grid = result.get("grid") or {}
    lines.extend(
        [
            "",
            "## Grid",
            "",
            f"- `min_confidence_grid`: `{grid.get('min_confidence_grid')}`",
            f"- `haircut_size_grid`: `{grid.get('haircut_size_grid')}`",
            f"- `normal_size_grid`: `{grid.get('normal_size_grid')}`",
            f"- `min_candidate_rate`: `{grid.get('min_candidate_rate')}`",
            f"- `coverage_weight`: `{grid.get('coverage_weight')}`",
            f"- `weighted_return_weight`: `{grid.get('weighted_return_weight')}`",
            f"- `quantity_penalty_weight`: `{grid.get('quantity_penalty_weight')}`",
            "",
            "## Top candidates",
            "",
            "| Rank | Score | Params | Pass rate floor | Candidates | Accuracy | Candidate rate | Gross qty | Avg qty | Avg signed excess | Avg weighted excess | Total weighted excess |",
            "| ---: | ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    if not result.get("top_candidates"):
        lines.append("| - | - | - | - | 0 | - | - | - | - | - | - | - |")
    for index, item in enumerate(result.get("top_candidates") or [], start=1):
        params = item.get("params") or {}
        param_text = (
            f"min_conf={params.get('min_confidence')}, "
            f"haircut={params.get('haircut_size_multiplier')}, "
            f"normal={params.get('normal_size')}"
        )
        lines.append(
            f"| {index} | {float(item.get('score') or 0.0):.4f} | {param_text} "
            f"| {'yes' if item.get('passes_min_candidate_rate') else 'no'} "
            f"| {item.get('candidate_orders')} "
            f"| {item.get('candidate_hits')}/{item.get('evaluated_candidates')} = {_fmt_optional_pct(item.get('candidate_accuracy'))} "
            f"| {_fmt_optional_pct(item.get('candidate_rate'))} "
            f"| {float(item.get('gross_quantity_units') or 0.0):.2f} "
            f"| {float(item.get('avg_quantity_units') or 0.0):.2f} "
            f"| {_fmt_optional_signed_pct(item.get('avg_signed_excess_return'))} "
            f"| {_fmt_optional_signed_pct(item.get('avg_weighted_signed_excess_return'))} "
            f"| {_fmt_optional_signed_pct(item.get('total_weighted_signed_excess_return'))} |"
        )
    selected_summary = result.get("selected_summary") or {}
    if selected_summary:
        lines.extend(
            [
                "",
                "## Selected sizing policy detail",
                "",
                "| Sizing policy | Orders | Candidates | Accuracy | Gross qty | Avg weighted excess |",
                "| --- | ---: | ---: | ---: | ---: | ---: |",
            ]
        )
        for row in selected_summary.get("by_sizing_policy") or []:
            lines.append(
                f"| {row['bucket']} | {row['orders']} | {row['candidate_orders']} "
                f"| {row['candidate_hits']}/{row['evaluated_candidates']} = {_fmt_optional_pct(row['candidate_accuracy'])} "
                f"| {float(row.get('gross_quantity_units') or 0.0):.2f} "
                f"| {_fmt_optional_signed_pct(row.get('avg_weighted_signed_excess_return'))} |"
            )
    lines.extend(
        [
            "",
            "## Interpretation note",
            "",
            "This tuner selects paper-order operating parameters, not a live trading configuration. A future walk-forward optimizer should split the timeline into training/holdout windows so these parameter choices are not overfit to the full replay sample.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_paper_order_parameter_tuning_markdown(
    output_path: Path,
    result: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        paper_order_parameter_tuning_markdown(result, filters=filters),
        encoding="utf-8",
    )
    return output_path


def _sort_replay_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        rows,
        key=lambda row: (
            str(row.get("replay_timestamp") or ""),
            int(row.get("id") or row.get("outcome_id") or 0),
        ),
    )


def _paper_order_summary_from_orders(
    orders: list[dict[str, Any]],
    *,
    min_group_rows: int = 1,
    sample_limit: int = 20,
) -> dict[str, Any]:
    candidates = [order for order in orders if order.get("order_status") == "candidate"]
    blocked = [order for order in orders if order.get("order_status") != "candidate"]
    return {
        "rows": len(orders),
        "orders": len(orders),
        "all_orders": _paper_order_group_metrics(orders),
        "candidate_orders": _paper_order_group_metrics(candidates),
        "blocked_orders": _paper_order_group_metrics(blocked),
        "by_order_status": _group_paper_orders(orders, lambda order: order.get("order_status"), min_rows=min_group_rows),
        "by_order_action": _group_paper_orders(orders, lambda order: order.get("order_action"), min_rows=min_group_rows),
        "by_sizing_policy": _group_paper_orders(orders, lambda order: order.get("sizing_policy"), min_rows=min_group_rows),
        "by_horizon_and_status": _group_paper_orders(
            orders,
            lambda order: f"{(order.get('signal') or {}).get('horizon') or 'unknown'}:{order.get('order_status')}",
            min_rows=min_group_rows,
        ),
        "sample_orders": orders[: max(0, int(sample_limit))],
        "paper_orders": orders,
    }


def _paper_orders_for_rows(
    rows: list[dict[str, Any]],
    *,
    min_confidence: float,
    normal_size: float,
    haircut_size_multiplier: float,
    base_quantity: float,
    fold_index: int | None = None,
) -> list[dict[str, Any]]:
    orders: list[dict[str, Any]] = []
    for row in rows:
        order = _paper_order_object_from_score(
            row,
            min_confidence=min_confidence,
            normal_size=normal_size,
            haircut_size_multiplier=haircut_size_multiplier,
            base_quantity=base_quantity,
        )
        if fold_index is not None:
            order = {**order, "walk_forward_fold": fold_index}
        orders.append(order)
    return orders


def walk_forward_paper_order_parameter_tuning(
    rows: list[dict[str, Any]],
    *,
    min_train_rows: int = 240,
    test_window_rows: int = 80,
    step_rows: int | None = None,
    rolling_train_rows: int | None = None,
    min_confidence_grid: tuple[float, ...] = (0.0, 0.30, 0.35, 0.40, 0.45),
    haircut_size_grid: tuple[float, ...] = (0.25, 0.50, 0.75),
    normal_size_grid: tuple[float, ...] = (1.0,),
    base_quantity: float = 1.0,
    min_candidate_rate: float = 0.50,
    coverage_weight: float = 0.08,
    weighted_return_weight: float = 2.0,
    quantity_penalty_weight: float = 0.02,
    top_n: int = 5,
    min_group_rows: int = 1,
    sample_limit: int = 20,
) -> dict[str, Any]:
    """Tune paper-order parameters on past rows and apply them to future rows.

    This is intentionally read-only. Each fold selects parameters using only
    rows before the test window, then applies those selected parameters to the
    next chronological test window.
    """

    ordered = _sort_replay_rows(rows)
    min_train_rows = max(0, int(min_train_rows))
    test_window_rows = max(1, int(test_window_rows))
    step_rows = max(1, int(step_rows if step_rows is not None else test_window_rows))
    rolling_train_rows = int(rolling_train_rows) if rolling_train_rows is not None else None
    if rolling_train_rows is not None:
        rolling_train_rows = max(1, rolling_train_rows)

    folds: list[dict[str, Any]] = []
    all_test_orders: list[dict[str, Any]] = []
    fold_index = 1
    test_start = min_train_rows
    while test_start < len(ordered):
        test_end = min(len(ordered), test_start + test_window_rows)
        if rolling_train_rows is None:
            train_start = 0
        else:
            train_start = max(0, test_start - rolling_train_rows)
        train_rows = ordered[train_start:test_start]
        test_rows = ordered[test_start:test_end]
        if not train_rows or not test_rows:
            test_start += step_rows
            continue
        train_tuning = tune_paper_order_parameters(
            train_rows,
            min_confidence_grid=min_confidence_grid,
            haircut_size_grid=haircut_size_grid,
            normal_size_grid=normal_size_grid,
            base_quantity=base_quantity,
            min_candidate_rate=min_candidate_rate,
            coverage_weight=coverage_weight,
            weighted_return_weight=weighted_return_weight,
            quantity_penalty_weight=quantity_penalty_weight,
            top_n=top_n,
        )
        selected = train_tuning.get("selected")
        if selected is None:
            test_start += step_rows
            continue
        params = selected.get("params") or {}
        test_orders = _paper_orders_for_rows(
            test_rows,
            min_confidence=float(params.get("min_confidence") or 0.0),
            normal_size=float(params.get("normal_size") or 1.0),
            haircut_size_multiplier=float(params.get("haircut_size_multiplier") or 0.0),
            base_quantity=float(params.get("base_quantity") or base_quantity),
            fold_index=fold_index,
        )
        test_summary = _paper_order_summary_from_orders(
            test_orders,
            min_group_rows=min_group_rows,
            sample_limit=0,
        )
        all_test_orders.extend(test_orders)
        folds.append(
            {
                "fold": fold_index,
                "train_start": train_rows[0].get("replay_timestamp"),
                "train_end": train_rows[-1].get("replay_timestamp"),
                "test_start": test_rows[0].get("replay_timestamp"),
                "test_end": test_rows[-1].get("replay_timestamp"),
                "train_rows": len(train_rows),
                "test_rows": len(test_rows),
                "selected": selected,
                "test_summary": test_summary,
            }
        )
        fold_index += 1
        test_start += step_rows

    aggregate_test_summary = _paper_order_summary_from_orders(
        all_test_orders,
        min_group_rows=min_group_rows,
        sample_limit=sample_limit,
    )
    return {
        "rows": len(ordered),
        "folds": folds,
        "fold_count": len(folds),
        "aggregate_test_summary": aggregate_test_summary,
        "parameters": {
            "min_train_rows": min_train_rows,
            "test_window_rows": test_window_rows,
            "step_rows": step_rows,
            "rolling_train_rows": rolling_train_rows,
            "min_confidence_grid": list(min_confidence_grid),
            "haircut_size_grid": list(haircut_size_grid),
            "normal_size_grid": list(normal_size_grid),
            "base_quantity": float(base_quantity),
            "min_candidate_rate": float(min_candidate_rate),
            "coverage_weight": float(coverage_weight),
            "weighted_return_weight": float(weighted_return_weight),
            "quantity_penalty_weight": float(quantity_penalty_weight),
            "top_n": int(top_n),
        },
        "safety": {
            "mode": "read_only_walk_forward_paper_order_parameter_tuning",
            "policy": "Each fold tunes only on prior stored rows; no OpenAI, IBKR account, portfolio, order submission, or scorer-row mutation.",
        },
    }


def walk_forward_paper_order_parameter_tuning_markdown(
    result: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
    title: str = "Walk-Forward Paper Order Parameter Tuning Report",
) -> str:
    filters = filters or {}
    aggregate = result.get("aggregate_test_summary") or {}
    aggregate_candidates = aggregate.get("candidate_orders") or {}
    aggregate_all = aggregate.get("all_orders") or {}
    params = result.get("parameters") or {}
    lines = [
        f"# {title}",
        "",
        f"- Generated at: `{datetime.now(UTC).isoformat()}`",
        f"- Rows: `{result.get('rows')}`",
        f"- Folds: `{result.get('fold_count')}`",
        f"- Aggregate test candidates: `{aggregate_candidates.get('candidate_orders')}`",
        f"- Aggregate test candidate rate: `{_fmt_optional_pct(aggregate_all.get('candidate_rate'))}`",
        f"- Aggregate test accuracy: `{aggregate_candidates.get('candidate_hits')}/{aggregate_candidates.get('evaluated_candidates')} = {_fmt_optional_pct(aggregate_candidates.get('candidate_accuracy'))}`",
        f"- Aggregate avg weighted signed excess return: `{_fmt_optional_signed_pct(aggregate_candidates.get('avg_weighted_signed_excess_return'))}`",
        "- Safety: read-only walk-forward tuning over stored `replay_model_scores`; no OpenAI, IBKR account, portfolio, order submission, or scorer mutation.",
        "",
        "## Filters",
        "",
    ]
    if filters:
        for key, value in filters.items():
            lines.append(f"- `{key}`: `{value if value is not None else '-'}`")
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "## Walk-forward setup",
            "",
            f"- `min_train_rows`: `{params.get('min_train_rows')}`",
            f"- `test_window_rows`: `{params.get('test_window_rows')}`",
            f"- `step_rows`: `{params.get('step_rows')}`",
            f"- `rolling_train_rows`: `{params.get('rolling_train_rows') if params.get('rolling_train_rows') is not None else 'expanding'}`",
            f"- `min_confidence_grid`: `{params.get('min_confidence_grid')}`",
            f"- `haircut_size_grid`: `{params.get('haircut_size_grid')}`",
            f"- `normal_size_grid`: `{params.get('normal_size_grid')}`",
            f"- `min_candidate_rate`: `{params.get('min_candidate_rate')}`",
            f"- `coverage_weight`: `{params.get('coverage_weight')}`",
            f"- `weighted_return_weight`: `{params.get('weighted_return_weight')}`",
            f"- `quantity_penalty_weight`: `{params.get('quantity_penalty_weight')}`",
            "",
            "## Fold results",
            "",
            "| Fold | Train window | Test window | Train rows | Test rows | Selected params | Train score | Test candidates | Test acc | Test candidate rate | Test avg weighted excess |",
            "| ---: | --- | --- | ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    if not result.get("folds"):
        lines.append("| - | - | - | 0 | 0 | - | - | 0 | - | - | - |")
    for fold in result.get("folds") or []:
        selected = fold.get("selected") or {}
        selected_params = selected.get("params") or {}
        test_summary = fold.get("test_summary") or {}
        test_candidates = test_summary.get("candidate_orders") or {}
        test_all = test_summary.get("all_orders") or {}
        selected_text = (
            f"min_conf={selected_params.get('min_confidence')}, "
            f"haircut={selected_params.get('haircut_size_multiplier')}, "
            f"normal={selected_params.get('normal_size')}"
        )
        lines.append(
            f"| {fold.get('fold')} "
            f"| {fold.get('train_start')} to {fold.get('train_end')} "
            f"| {fold.get('test_start')} to {fold.get('test_end')} "
            f"| {fold.get('train_rows')} "
            f"| {fold.get('test_rows')} "
            f"| {selected_text} "
            f"| {float(selected.get('score') or 0.0):.4f} "
            f"| {test_candidates.get('candidate_orders')} "
            f"| {test_candidates.get('candidate_hits')}/{test_candidates.get('evaluated_candidates')} = {_fmt_optional_pct(test_candidates.get('candidate_accuracy'))} "
            f"| {_fmt_optional_pct(test_all.get('candidate_rate'))} "
            f"| {_fmt_optional_signed_pct(test_candidates.get('avg_weighted_signed_excess_return'))} |"
        )
    _append_paper_order_table(
        lines,
        "Aggregate test core groups",
        [
            {"bucket": "all_test_orders", **(aggregate.get("all_orders") or {})},
            {"bucket": "candidate_test_orders", **aggregate_candidates},
            {"bucket": "blocked_test_orders", **(aggregate.get("blocked_orders") or {})},
        ],
    )
    _append_paper_order_table(lines, "Aggregate test by sizing policy", aggregate.get("by_sizing_policy") or [])
    lines.extend(
        [
            "",
            "## Interpretation note",
            "",
            "This report is stricter than full-sample tuning: each fold chooses parameters using only earlier rows, then evaluates those parameters on later rows. Treat the aggregate test section as the safer operating-candidate estimate, while still paper-only and non-executable.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_walk_forward_paper_order_parameter_tuning_markdown(
    output_path: Path,
    result: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        walk_forward_paper_order_parameter_tuning_markdown(result, filters=filters),
        encoding="utf-8",
    )
    return output_path


def _paper_order_sort_key(order: dict[str, Any]) -> tuple[str, str]:
    signal = order.get("signal") or {}
    return (str(signal.get("replay_timestamp") or ""), str(order.get("paper_order_id") or ""))


def _paper_order_symbol_day_key(order: dict[str, Any]) -> str:
    signal = order.get("signal") or {}
    timestamp = str(signal.get("replay_timestamp") or "")
    day = timestamp.split("T", 1)[0] if timestamp else "unknown-date"
    return f"{order.get('symbol') or 'UNKNOWN'}:{day}"


def _risk_adjust_order_quantity(order: dict[str, Any], quantity_units: float, *, risk_reason: str, risk_status: str) -> dict[str, Any]:
    adjusted = dict(order)
    original_quantity = float(order.get("quantity_units") or 0.0)
    base_quantity = float(order.get("base_quantity") or 1.0)
    quantity_units = max(0.0, float(quantity_units))
    size_multiplier = quantity_units / base_quantity if base_quantity else 0.0
    evaluation = dict(order.get("paper_evaluation") or {})
    signed_raw = evaluation.get("signed_raw_return")
    signed_excess = evaluation.get("signed_excess_return")
    if signed_raw is not None:
        evaluation["weighted_signed_raw_return"] = float(signed_raw) * size_multiplier
    if signed_excess is not None:
        evaluation["weighted_signed_excess_return"] = float(signed_excess) * size_multiplier
    adjusted.update(
        {
            "order_status": "candidate" if quantity_units > 0.0 else "blocked",
            "quantity_units": quantity_units,
            "size_multiplier": size_multiplier,
            "pre_risk_quantity_units": original_quantity,
            "risk_status": risk_status,
            "risk_reason": risk_reason,
            "risk_quantity_units": quantity_units,
            "risk_size_multiplier": size_multiplier,
            "paper_evaluation": evaluation,
            "risk_safety": {
                "paper_only": True,
                "ibkr_order_submitted": False,
                "account_or_portfolio_checked": False,
                "mutation": "none",
            },
        }
    )
    return adjusted


def apply_paper_order_risk_layer(
    orders: list[dict[str, Any]],
    *,
    max_total_gross_quantity: float | None = None,
    max_symbol_gross_quantity: float | None = None,
    max_side_gross_quantity: float | None = None,
    max_symbol_orders: int | None = None,
    max_symbol_day_orders: int | None = None,
    min_abs_expected_return: float | None = None,
    reduce_to_fit_capacity: bool = True,
    min_reduced_quantity: float = 0.01,
) -> list[dict[str, Any]]:
    total_gross = 0.0
    symbol_gross: dict[str, float] = {}
    side_gross: dict[str, float] = {}
    symbol_orders: dict[str, int] = {}
    symbol_day_orders: dict[str, int] = {}
    risk_orders: list[dict[str, Any]] = []

    for order in sorted(orders, key=_paper_order_sort_key):
        if order.get("order_status") != "candidate":
            risk_orders.append(_risk_adjust_order_quantity(order, 0.0, risk_reason="not_candidate", risk_status="not_candidate"))
            continue
        quantity = float(order.get("quantity_units") or 0.0)
        symbol = str(order.get("symbol") or "UNKNOWN")
        side = str(order.get("order_action") or "UNKNOWN")
        symbol_day = _paper_order_symbol_day_key(order)
        signal = order.get("signal") or {}
        expected_return = signal.get("expected_return")
        if min_abs_expected_return is not None and (
            expected_return is None or abs(float(expected_return)) < float(min_abs_expected_return)
        ):
            risk_orders.append(_risk_adjust_order_quantity(order, 0.0, risk_reason="weak_expected_return", risk_status="risk_blocked"))
            continue
        if max_symbol_orders is not None and symbol_orders.get(symbol, 0) >= int(max_symbol_orders):
            risk_orders.append(_risk_adjust_order_quantity(order, 0.0, risk_reason="symbol_order_limit", risk_status="risk_blocked"))
            continue
        if max_symbol_day_orders is not None and symbol_day_orders.get(symbol_day, 0) >= int(max_symbol_day_orders):
            risk_orders.append(_risk_adjust_order_quantity(order, 0.0, risk_reason="symbol_day_order_limit", risk_status="risk_blocked"))
            continue

        remaining_caps: list[float] = []
        if max_total_gross_quantity is not None:
            remaining_caps.append(float(max_total_gross_quantity) - total_gross)
        if max_symbol_gross_quantity is not None:
            remaining_caps.append(float(max_symbol_gross_quantity) - symbol_gross.get(symbol, 0.0))
        if max_side_gross_quantity is not None:
            remaining_caps.append(float(max_side_gross_quantity) - side_gross.get(side, 0.0))
        remaining_capacity = min(remaining_caps) if remaining_caps else quantity
        if remaining_capacity <= 0.0:
            risk_orders.append(_risk_adjust_order_quantity(order, 0.0, risk_reason="capacity_exhausted", risk_status="risk_blocked"))
            continue
        risk_quantity = quantity
        risk_status = "risk_accepted"
        risk_reason = "accepted"
        if quantity > remaining_capacity:
            if reduce_to_fit_capacity and remaining_capacity >= float(min_reduced_quantity):
                risk_quantity = remaining_capacity
                risk_status = "risk_reduced"
                risk_reason = "reduced_to_capacity"
            else:
                risk_orders.append(_risk_adjust_order_quantity(order, 0.0, risk_reason="capacity_exceeded", risk_status="risk_blocked"))
                continue

        adjusted = _risk_adjust_order_quantity(order, risk_quantity, risk_reason=risk_reason, risk_status=risk_status)
        total_gross += risk_quantity
        symbol_gross[symbol] = symbol_gross.get(symbol, 0.0) + risk_quantity
        side_gross[side] = side_gross.get(side, 0.0) + risk_quantity
        symbol_orders[symbol] = symbol_orders.get(symbol, 0) + 1
        symbol_day_orders[symbol_day] = symbol_day_orders.get(symbol_day, 0) + 1
        risk_orders.append(adjusted)
    return risk_orders


def paper_order_risk_layer_summary(
    rows: list[dict[str, Any]],
    *,
    min_confidence: float = 0.35,
    normal_size: float = 1.25,
    haircut_size_multiplier: float = 0.75,
    base_quantity: float = 1.0,
    max_total_gross_quantity: float | None = None,
    max_symbol_gross_quantity: float | None = None,
    max_side_gross_quantity: float | None = None,
    max_symbol_orders: int | None = None,
    max_symbol_day_orders: int | None = None,
    min_abs_expected_return: float | None = None,
    reduce_to_fit_capacity: bool = True,
    min_reduced_quantity: float = 0.01,
    min_group_rows: int = 1,
    sample_limit: int = 20,
) -> dict[str, Any]:
    pre_risk = paper_order_objects_summary(
        rows,
        min_confidence=min_confidence,
        normal_size=normal_size,
        haircut_size_multiplier=haircut_size_multiplier,
        base_quantity=base_quantity,
        min_group_rows=min_group_rows,
        sample_limit=0,
    )
    risk_orders = apply_paper_order_risk_layer(
        pre_risk.get("paper_orders") or [],
        max_total_gross_quantity=max_total_gross_quantity,
        max_symbol_gross_quantity=max_symbol_gross_quantity,
        max_side_gross_quantity=max_side_gross_quantity,
        max_symbol_orders=max_symbol_orders,
        max_symbol_day_orders=max_symbol_day_orders,
        min_abs_expected_return=min_abs_expected_return,
        reduce_to_fit_capacity=reduce_to_fit_capacity,
        min_reduced_quantity=min_reduced_quantity,
    )
    post_risk = _paper_order_summary_from_orders(
        risk_orders,
        min_group_rows=min_group_rows,
        sample_limit=sample_limit,
    )
    pre_candidates = pre_risk.get("candidate_orders") or {}
    post_candidates = post_risk.get("candidate_orders") or {}
    return {
        "rows": len(rows),
        "parameters": {
            "min_confidence": float(min_confidence),
            "normal_size": float(normal_size),
            "haircut_size_multiplier": float(haircut_size_multiplier),
            "base_quantity": float(base_quantity),
        },
        "risk_limits": {
            "max_total_gross_quantity": max_total_gross_quantity,
            "max_symbol_gross_quantity": max_symbol_gross_quantity,
            "max_side_gross_quantity": max_side_gross_quantity,
            "max_symbol_orders": max_symbol_orders,
            "max_symbol_day_orders": max_symbol_day_orders,
            "min_abs_expected_return": min_abs_expected_return,
            "reduce_to_fit_capacity": bool(reduce_to_fit_capacity),
            "min_reduced_quantity": float(min_reduced_quantity),
        },
        "pre_risk_summary": pre_risk,
        "post_risk_summary": post_risk,
        "candidate_delta": {
            "pre_candidates": pre_candidates.get("candidate_orders"),
            "post_candidates": post_candidates.get("candidate_orders"),
            "blocked_or_removed_candidates": int(pre_candidates.get("candidate_orders") or 0) - int(post_candidates.get("candidate_orders") or 0),
            "pre_accuracy": pre_candidates.get("candidate_accuracy"),
            "post_accuracy": post_candidates.get("candidate_accuracy"),
            "pre_gross_quantity_units": pre_candidates.get("gross_quantity_units"),
            "post_gross_quantity_units": post_candidates.get("gross_quantity_units"),
            "pre_avg_weighted_signed_excess_return": pre_candidates.get("avg_weighted_signed_excess_return"),
            "post_avg_weighted_signed_excess_return": post_candidates.get("avg_weighted_signed_excess_return"),
        },
        "by_risk_status": _group_paper_orders(risk_orders, lambda order: order.get("risk_status"), min_rows=min_group_rows),
        "by_risk_reason": _group_paper_orders(risk_orders, lambda order: order.get("risk_reason"), min_rows=min_group_rows),
        "by_symbol": _group_paper_orders(risk_orders, lambda order: order.get("symbol"), min_rows=min_group_rows),
        "paper_orders": risk_orders,
        "sample_orders": risk_orders[: max(0, int(sample_limit))],
        "safety": {
            "mode": "read_only_paper_order_risk_layer",
            "policy": "Uses only stored score-time fields and configured paper risk limits; no OpenAI, IBKR account, portfolio, order submission, or scorer-row mutation.",
        },
    }


def paper_order_risk_layer_markdown(
    summary: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
    title: str = "Paper Order Risk Layer Report",
) -> str:
    filters = filters or {}
    delta = summary.get("candidate_delta") or {}
    post = summary.get("post_risk_summary") or {}
    post_candidates = post.get("candidate_orders") or {}
    lines = [
        f"# {title}",
        "",
        f"- Generated at: `{datetime.now(UTC).isoformat()}`",
        f"- Rows: `{summary.get('rows')}`",
        f"- Post-risk candidate orders: `{post_candidates.get('candidate_orders')}`",
        f"- Post-risk accuracy: `{post_candidates.get('candidate_hits')}/{post_candidates.get('evaluated_candidates')} = {_fmt_optional_pct(post_candidates.get('candidate_accuracy'))}`",
        f"- Post-risk gross quantity units: `{float(post_candidates.get('gross_quantity_units') or 0.0):.2f}`",
        f"- Post-risk avg weighted signed excess return: `{_fmt_optional_signed_pct(post_candidates.get('avg_weighted_signed_excess_return'))}`",
        f"- Candidate delta: `{delta.get('pre_candidates')} -> {delta.get('post_candidates')}`",
        "- Safety: read-only paper risk layer only; no OpenAI, IBKR account, portfolio, order submission, or scorer mutation.",
        "",
        "## Filters",
        "",
    ]
    if filters:
        for key, value in filters.items():
            lines.append(f"- `{key}`: `{value if value is not None else '-'}`")
    else:
        lines.append("- none")
    params = summary.get("parameters") or {}
    limits = summary.get("risk_limits") or {}
    lines.extend(
        [
            "",
            "## Parameters and risk limits",
            "",
            f"- `min_confidence`: `{params.get('min_confidence')}`",
            f"- `normal_size`: `{params.get('normal_size')}`",
            f"- `haircut_size_multiplier`: `{params.get('haircut_size_multiplier')}`",
            f"- `base_quantity`: `{params.get('base_quantity')}`",
            f"- `max_total_gross_quantity`: `{limits.get('max_total_gross_quantity')}`",
            f"- `max_symbol_gross_quantity`: `{limits.get('max_symbol_gross_quantity')}`",
            f"- `max_side_gross_quantity`: `{limits.get('max_side_gross_quantity')}`",
            f"- `max_symbol_orders`: `{limits.get('max_symbol_orders')}`",
            f"- `max_symbol_day_orders`: `{limits.get('max_symbol_day_orders')}`",
            f"- `min_abs_expected_return`: `{limits.get('min_abs_expected_return')}`",
            f"- `reduce_to_fit_capacity`: `{limits.get('reduce_to_fit_capacity')}`",
            f"- `min_reduced_quantity`: `{limits.get('min_reduced_quantity')}`",
        ]
    )
    _append_paper_order_table(
        lines,
        "Pre-risk vs post-risk",
        [
            {"bucket": "pre_risk_candidates", **((summary.get("pre_risk_summary") or {}).get("candidate_orders") or {})},
            {"bucket": "post_risk_candidates", **post_candidates},
            {"bucket": "post_risk_blocked", **(post.get("blocked_orders") or {})},
        ],
    )
    _append_paper_order_table(lines, "By risk status", summary.get("by_risk_status") or [])
    _append_paper_order_table(lines, "By risk reason", summary.get("by_risk_reason") or [])
    _append_paper_order_table(lines, "By symbol", summary.get("by_symbol") or [])
    lines.extend(
        [
            "",
            "## Sample post-risk paper orders",
            "",
            "| Paper order id | Risk status | Risk reason | Status | Action | Symbol | Qty units | Confidence | Expected return |",
            "| --- | --- | --- | --- | --- | --- | ---: | ---: | ---: |",
        ]
    )
    for order in summary.get("sample_orders") or []:
        signal = order.get("signal") or {}
        lines.append(
            f"| {order.get('paper_order_id')} | {order.get('risk_status')} | {order.get('risk_reason')} "
            f"| {order.get('order_status')} | {order.get('order_action')} | {order.get('symbol')} "
            f"| {float(order.get('quantity_units') or 0.0):.2f} "
            f"| {_fmt_optional_pct(signal.get('confidence'))} "
            f"| {_fmt_optional_signed_pct(signal.get('expected_return'))} |"
        )
    lines.extend(
        [
            "",
            "## Interpretation note",
            "",
            "This is a paper-only risk wrapper. It intentionally does not inspect account state, positions, margin, liquidity, or live volatility yet. It exists to make the future order router safer by proving risk acceptance, reduction, and blocking behavior before any executable IBKR order path is introduced.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_paper_order_risk_layer_markdown(
    output_path: Path,
    summary: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        paper_order_risk_layer_markdown(summary, filters=filters),
        encoding="utf-8",
    )
    return output_path


DEFAULT_PAPER_ORDER_SECTOR_MAP = {
    "AAPL": "technology",
    "ADBE": "technology",
    "AMD": "semiconductors",
    "AMZN": "consumer_discretionary",
    "AVGO": "semiconductors",
    "CRM": "software",
    "CSCO": "technology",
    "CVX": "energy",
    "GOOGL": "communication_services",
    "IBM": "technology",
    "INTC": "semiconductors",
    "INTU": "software",
    "LRCX": "semiconductors",
    "META": "communication_services",
    "MSFT": "technology",
    "MU": "semiconductors",
    "NFLX": "communication_services",
    "NOW": "software",
    "NVDA": "semiconductors",
    "ORCL": "software",
    "QCOM": "semiconductors",
    "SNOW": "software",
    "TSLA": "consumer_discretionary",
    "TXN": "semiconductors",
    "XOM": "energy",
}


def _paper_order_sector(order: dict[str, Any], sector_map: dict[str, str] | None = None) -> str:
    mapping = {**DEFAULT_PAPER_ORDER_SECTOR_MAP, **(sector_map or {})}
    return mapping.get(str(order.get("symbol") or "").upper(), "unknown")


def _paper_order_day(order: dict[str, Any]) -> str:
    signal = order.get("signal") or {}
    timestamp = str(signal.get("replay_timestamp") or "")
    return timestamp.split("T", 1)[0] if timestamp else "unknown-date"


def _paper_order_signed_excess(order: dict[str, Any]) -> float:
    evaluation = order.get("paper_evaluation") or {}
    value = evaluation.get("signed_excess_return")
    return float(value) if value is not None else 0.0


def apply_paper_portfolio_risk_layer(
    orders: list[dict[str, Any]],
    *,
    sector_map: dict[str, str] | None = None,
    starting_equity: float = 100_000.0,
    notional_per_quantity: float = 1_000.0,
    max_daily_gross_exposure_pct: float | None = 0.30,
    max_symbol_daily_exposure_pct: float | None = 0.06,
    max_sector_daily_exposure_pct: float | None = 0.15,
    max_daily_loss_pct: float | None = 0.02,
    max_drawdown_pct: float | None = 0.05,
    max_consecutive_losses: int | None = 5,
    loss_throttle_multiplier: float = 0.50,
    min_reduced_quantity: float = 0.01,
) -> list[dict[str, Any]]:
    """Apply a virtual portfolio state to already paper-risked orders.

    Gating uses only prior accepted/reduced paper outcomes to update virtual
    equity, daily PnL, drawdown, and consecutive-loss state. Current-order
    realized returns are written only after the paper decision is made.
    """

    equity = float(starting_equity)
    day_start_equity = equity
    peak_equity = equity
    current_day: str | None = None
    daily_pnl = 0.0
    day_gross_notional = 0.0
    day_symbol_notional: dict[str, float] = {}
    day_sector_notional: dict[str, float] = {}
    consecutive_losses = 0
    max_drawdown_observed = 0.0
    max_daily_loss_observed = 0.0
    result: list[dict[str, Any]] = []

    for order in sorted(orders, key=_paper_order_sort_key):
        day = _paper_order_day(order)
        if current_day != day:
            current_day = day
            day_start_equity = equity
            daily_pnl = 0.0
            day_gross_notional = 0.0
            day_symbol_notional = {}
            day_sector_notional = {}

        state_before = {
            "day": current_day,
            "equity": equity,
            "peak_equity": peak_equity,
            "drawdown_pct": (peak_equity - equity) / peak_equity if peak_equity else 0.0,
            "daily_pnl": daily_pnl,
            "daily_pnl_pct": daily_pnl / day_start_equity if day_start_equity else 0.0,
            "day_gross_notional": day_gross_notional,
            "consecutive_losses": consecutive_losses,
        }
        if order.get("order_status") != "candidate":
            adjusted = _risk_adjust_order_quantity(
                order,
                0.0,
                risk_reason="portfolio_upstream_blocked",
                risk_status="portfolio_upstream_blocked",
            )
            adjusted["portfolio_state_before"] = state_before
            adjusted["portfolio_state_after"] = dict(state_before)
            adjusted["portfolio_realized_pnl"] = 0.0
            adjusted["sector"] = _paper_order_sector(order, sector_map)
            result.append(adjusted)
            continue

        quantity = float(order.get("quantity_units") or 0.0)
        symbol = str(order.get("symbol") or "UNKNOWN")
        sector = _paper_order_sector(order, sector_map)
        order_notional = quantity * float(notional_per_quantity)
        daily_loss_hit = (
            max_daily_loss_pct is not None
            and day_start_equity > 0
            and daily_pnl / day_start_equity <= -float(max_daily_loss_pct)
        )
        drawdown = (peak_equity - equity) / peak_equity if peak_equity else 0.0
        drawdown_hit = max_drawdown_pct is not None and drawdown >= float(max_drawdown_pct)
        if daily_loss_hit:
            adjusted = _risk_adjust_order_quantity(order, 0.0, risk_reason="portfolio_daily_loss_limit", risk_status="portfolio_blocked")
            adjusted["portfolio_state_before"] = state_before
            adjusted["portfolio_state_after"] = dict(state_before)
            adjusted["portfolio_realized_pnl"] = 0.0
            adjusted["sector"] = sector
            result.append(adjusted)
            continue
        if drawdown_hit:
            adjusted = _risk_adjust_order_quantity(order, 0.0, risk_reason="portfolio_drawdown_limit", risk_status="portfolio_blocked")
            adjusted["portfolio_state_before"] = state_before
            adjusted["portfolio_state_after"] = dict(state_before)
            adjusted["portfolio_realized_pnl"] = 0.0
            adjusted["sector"] = sector
            result.append(adjusted)
            continue

        remaining_caps: list[float] = []
        if max_daily_gross_exposure_pct is not None:
            remaining_caps.append(float(max_daily_gross_exposure_pct) * equity - day_gross_notional)
        if max_symbol_daily_exposure_pct is not None:
            remaining_caps.append(float(max_symbol_daily_exposure_pct) * equity - day_symbol_notional.get(symbol, 0.0))
        if max_sector_daily_exposure_pct is not None:
            remaining_caps.append(float(max_sector_daily_exposure_pct) * equity - day_sector_notional.get(sector, 0.0))
        remaining_notional = min(remaining_caps) if remaining_caps else order_notional
        if remaining_notional <= 0.0:
            adjusted = _risk_adjust_order_quantity(order, 0.0, risk_reason="portfolio_exposure_capacity", risk_status="portfolio_blocked")
            adjusted["portfolio_state_before"] = state_before
            adjusted["portfolio_state_after"] = dict(state_before)
            adjusted["portfolio_realized_pnl"] = 0.0
            adjusted["sector"] = sector
            result.append(adjusted)
            continue

        portfolio_quantity = quantity
        status = "portfolio_accepted"
        reason = "portfolio_accepted"
        if order_notional > remaining_notional:
            portfolio_quantity = remaining_notional / float(notional_per_quantity)
            status = "portfolio_reduced"
            reason = "portfolio_reduced_to_exposure_capacity"
        if max_consecutive_losses is not None and consecutive_losses >= int(max_consecutive_losses):
            portfolio_quantity *= max(0.0, float(loss_throttle_multiplier))
            status = "portfolio_reduced" if portfolio_quantity > 0.0 else "portfolio_blocked"
            reason = "portfolio_consecutive_loss_throttle"
        if portfolio_quantity < float(min_reduced_quantity):
            adjusted = _risk_adjust_order_quantity(order, 0.0, risk_reason=reason, risk_status="portfolio_blocked")
            adjusted["portfolio_state_before"] = state_before
            adjusted["portfolio_state_after"] = dict(state_before)
            adjusted["portfolio_realized_pnl"] = 0.0
            adjusted["sector"] = sector
            result.append(adjusted)
            continue

        adjusted = _risk_adjust_order_quantity(order, portfolio_quantity, risk_reason=reason, risk_status=status)
        accepted_notional = portfolio_quantity * float(notional_per_quantity)
        realized_pnl = _paper_order_signed_excess(adjusted) * accepted_notional
        equity += realized_pnl
        peak_equity = max(peak_equity, equity)
        daily_pnl += realized_pnl
        day_gross_notional += accepted_notional
        day_symbol_notional[symbol] = day_symbol_notional.get(symbol, 0.0) + accepted_notional
        day_sector_notional[sector] = day_sector_notional.get(sector, 0.0) + accepted_notional
        consecutive_losses = consecutive_losses + 1 if realized_pnl < 0.0 else 0
        drawdown_after = (peak_equity - equity) / peak_equity if peak_equity else 0.0
        max_drawdown_observed = max(max_drawdown_observed, drawdown_after)
        max_daily_loss_observed = min(max_daily_loss_observed, daily_pnl / day_start_equity if day_start_equity else 0.0)
        adjusted["portfolio_state_before"] = state_before
        adjusted["portfolio_state_after"] = {
            "day": current_day,
            "equity": equity,
            "peak_equity": peak_equity,
            "drawdown_pct": drawdown_after,
            "daily_pnl": daily_pnl,
            "daily_pnl_pct": daily_pnl / day_start_equity if day_start_equity else 0.0,
            "day_gross_notional": day_gross_notional,
            "consecutive_losses": consecutive_losses,
            "max_drawdown_observed": max_drawdown_observed,
            "max_daily_loss_observed": max_daily_loss_observed,
        }
        adjusted["portfolio_realized_pnl"] = realized_pnl
        adjusted["portfolio_notional"] = accepted_notional
        adjusted["sector"] = sector
        result.append(adjusted)
    return result


def _paper_portfolio_metrics(orders: list[dict[str, Any]], *, starting_equity: float) -> dict[str, Any]:
    accepted = [order for order in orders if order.get("order_status") == "candidate"]
    realized = [float(order.get("portfolio_realized_pnl") or 0.0) for order in accepted]
    ending_equity = float(starting_equity) + sum(realized)
    state_afters = [order.get("portfolio_state_after") or {} for order in orders]
    max_drawdown = max((float(state.get("drawdown_pct") or 0.0) for state in state_afters), default=0.0)
    max_daily_loss = min((float(state.get("daily_pnl_pct") or 0.0) for state in state_afters), default=0.0)
    return {
        "starting_equity": float(starting_equity),
        "ending_equity": ending_equity,
        "net_pnl": ending_equity - float(starting_equity),
        "net_return": (ending_equity - float(starting_equity)) / float(starting_equity) if starting_equity else None,
        "accepted_orders": len(accepted),
        "winning_orders": sum(1 for value in realized if value > 0.0),
        "losing_orders": sum(1 for value in realized if value < 0.0),
        "win_rate": sum(1 for value in realized if value > 0.0) / len(realized) if realized else None,
        "total_notional": sum(float(order.get("portfolio_notional") or 0.0) for order in accepted),
        "avg_notional": (
            sum(float(order.get("portfolio_notional") or 0.0) for order in accepted) / len(accepted)
            if accepted
            else None
        ),
        "max_drawdown": max_drawdown,
        "max_daily_loss": max_daily_loss,
    }


def paper_portfolio_risk_layer_summary(
    rows: list[dict[str, Any]],
    *,
    min_confidence: float = 0.35,
    normal_size: float = 1.25,
    haircut_size_multiplier: float = 0.75,
    base_quantity: float = 1.0,
    max_total_gross_quantity: float | None = 300.0,
    max_symbol_gross_quantity: float | None = 12.0,
    max_side_gross_quantity: float | None = 175.0,
    max_symbol_orders: int | None = 10,
    max_symbol_day_orders: int | None = 1,
    min_abs_expected_return: float | None = 0.0025,
    starting_equity: float = 100_000.0,
    notional_per_quantity: float = 1_000.0,
    max_daily_gross_exposure_pct: float | None = 0.30,
    max_symbol_daily_exposure_pct: float | None = 0.06,
    max_sector_daily_exposure_pct: float | None = 0.15,
    max_daily_loss_pct: float | None = 0.02,
    max_drawdown_pct: float | None = 0.05,
    max_consecutive_losses: int | None = 5,
    loss_throttle_multiplier: float = 0.50,
    min_reduced_quantity: float = 0.01,
    sector_map: dict[str, str] | None = None,
    min_group_rows: int = 1,
    sample_limit: int = 20,
) -> dict[str, Any]:
    static_summary = paper_order_risk_layer_summary(
        rows,
        min_confidence=min_confidence,
        normal_size=normal_size,
        haircut_size_multiplier=haircut_size_multiplier,
        base_quantity=base_quantity,
        max_total_gross_quantity=max_total_gross_quantity,
        max_symbol_gross_quantity=max_symbol_gross_quantity,
        max_side_gross_quantity=max_side_gross_quantity,
        max_symbol_orders=max_symbol_orders,
        max_symbol_day_orders=max_symbol_day_orders,
        min_abs_expected_return=min_abs_expected_return,
        reduce_to_fit_capacity=True,
        min_reduced_quantity=min_reduced_quantity,
        min_group_rows=min_group_rows,
        sample_limit=0,
    )
    portfolio_orders = apply_paper_portfolio_risk_layer(
        static_summary.get("paper_orders") or [],
        sector_map=sector_map,
        starting_equity=starting_equity,
        notional_per_quantity=notional_per_quantity,
        max_daily_gross_exposure_pct=max_daily_gross_exposure_pct,
        max_symbol_daily_exposure_pct=max_symbol_daily_exposure_pct,
        max_sector_daily_exposure_pct=max_sector_daily_exposure_pct,
        max_daily_loss_pct=max_daily_loss_pct,
        max_drawdown_pct=max_drawdown_pct,
        max_consecutive_losses=max_consecutive_losses,
        loss_throttle_multiplier=loss_throttle_multiplier,
        min_reduced_quantity=min_reduced_quantity,
    )
    portfolio_summary = _paper_order_summary_from_orders(
        portfolio_orders,
        min_group_rows=min_group_rows,
        sample_limit=sample_limit,
    )
    static_candidates = (static_summary.get("post_risk_summary") or {}).get("candidate_orders") or {}
    portfolio_candidates = portfolio_summary.get("candidate_orders") or {}
    return {
        "rows": len(rows),
        "static_risk_summary": static_summary,
        "portfolio_summary": portfolio_summary,
        "portfolio_metrics": _paper_portfolio_metrics(portfolio_orders, starting_equity=starting_equity),
        "portfolio_limits": {
            "starting_equity": float(starting_equity),
            "notional_per_quantity": float(notional_per_quantity),
            "max_daily_gross_exposure_pct": max_daily_gross_exposure_pct,
            "max_symbol_daily_exposure_pct": max_symbol_daily_exposure_pct,
            "max_sector_daily_exposure_pct": max_sector_daily_exposure_pct,
            "max_daily_loss_pct": max_daily_loss_pct,
            "max_drawdown_pct": max_drawdown_pct,
            "max_consecutive_losses": max_consecutive_losses,
            "loss_throttle_multiplier": float(loss_throttle_multiplier),
            "min_reduced_quantity": float(min_reduced_quantity),
        },
        "candidate_delta": {
            "static_risk_candidates": static_candidates.get("candidate_orders"),
            "portfolio_candidates": portfolio_candidates.get("candidate_orders"),
            "blocked_or_removed_candidates": int(static_candidates.get("candidate_orders") or 0) - int(portfolio_candidates.get("candidate_orders") or 0),
            "static_accuracy": static_candidates.get("candidate_accuracy"),
            "portfolio_accuracy": portfolio_candidates.get("candidate_accuracy"),
            "static_avg_weighted_signed_excess_return": static_candidates.get("avg_weighted_signed_excess_return"),
            "portfolio_avg_weighted_signed_excess_return": portfolio_candidates.get("avg_weighted_signed_excess_return"),
        },
        "by_portfolio_status": _group_paper_orders(portfolio_orders, lambda order: order.get("risk_status"), min_rows=min_group_rows),
        "by_portfolio_reason": _group_paper_orders(portfolio_orders, lambda order: order.get("risk_reason"), min_rows=min_group_rows),
        "by_sector": _group_paper_orders(portfolio_orders, lambda order: order.get("sector"), min_rows=min_group_rows),
        "paper_orders": portfolio_orders,
        "sample_orders": portfolio_orders[: max(0, int(sample_limit))],
        "safety": {
            "mode": "read_only_paper_portfolio_risk_layer",
            "policy": "Virtual paper portfolio only; no OpenAI, IBKR account, portfolio query, margin query, order submission, or scorer-row mutation.",
        },
    }


def paper_portfolio_risk_layer_markdown(
    summary: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
    title: str = "Paper Portfolio Risk Layer Report",
) -> str:
    filters = filters or {}
    portfolio_summary = summary.get("portfolio_summary") or {}
    candidates = portfolio_summary.get("candidate_orders") or {}
    metrics = summary.get("portfolio_metrics") or {}
    delta = summary.get("candidate_delta") or {}
    lines = [
        f"# {title}",
        "",
        f"- Generated at: `{datetime.now(UTC).isoformat()}`",
        f"- Rows: `{summary.get('rows')}`",
        f"- Portfolio candidates: `{candidates.get('candidate_orders')}`",
        f"- Portfolio candidate accuracy: `{candidates.get('candidate_hits')}/{candidates.get('evaluated_candidates')} = {_fmt_optional_pct(candidates.get('candidate_accuracy'))}`",
        f"- Portfolio avg weighted signed excess return: `{_fmt_optional_signed_pct(candidates.get('avg_weighted_signed_excess_return'))}`",
        f"- Virtual ending equity: `{float(metrics.get('ending_equity') or 0.0):.2f}`",
        f"- Virtual net PnL: `{float(metrics.get('net_pnl') or 0.0):+.2f}`",
        f"- Virtual net return: `{_fmt_optional_signed_pct(metrics.get('net_return'))}`",
        f"- Max drawdown: `{_fmt_optional_pct(metrics.get('max_drawdown'))}`",
        f"- Max daily loss: `{_fmt_optional_signed_pct(metrics.get('max_daily_loss'))}`",
        f"- Candidate delta: `{delta.get('static_risk_candidates')} -> {delta.get('portfolio_candidates')}`",
        "- Safety: read-only virtual paper portfolio only; no OpenAI, IBKR account, portfolio, margin, or order submission.",
        "",
        "## Filters",
        "",
    ]
    if filters:
        for key, value in filters.items():
            lines.append(f"- `{key}`: `{value if value is not None else '-'}`")
    else:
        lines.append("- none")
    limits = summary.get("portfolio_limits") or {}
    lines.extend(
        [
            "",
            "## Portfolio limits",
            "",
            f"- `starting_equity`: `{limits.get('starting_equity')}`",
            f"- `notional_per_quantity`: `{limits.get('notional_per_quantity')}`",
            f"- `max_daily_gross_exposure_pct`: `{limits.get('max_daily_gross_exposure_pct')}`",
            f"- `max_symbol_daily_exposure_pct`: `{limits.get('max_symbol_daily_exposure_pct')}`",
            f"- `max_sector_daily_exposure_pct`: `{limits.get('max_sector_daily_exposure_pct')}`",
            f"- `max_daily_loss_pct`: `{limits.get('max_daily_loss_pct')}`",
            f"- `max_drawdown_pct`: `{limits.get('max_drawdown_pct')}`",
            f"- `max_consecutive_losses`: `{limits.get('max_consecutive_losses')}`",
            f"- `loss_throttle_multiplier`: `{limits.get('loss_throttle_multiplier')}`",
            f"- `min_reduced_quantity`: `{limits.get('min_reduced_quantity')}`",
        ]
    )
    _append_paper_order_table(
        lines,
        "Static risk vs portfolio risk",
        [
            {"bucket": "static_risk_candidates", **(((summary.get("static_risk_summary") or {}).get("post_risk_summary") or {}).get("candidate_orders") or {})},
            {"bucket": "portfolio_candidates", **candidates},
            {"bucket": "portfolio_blocked", **(portfolio_summary.get("blocked_orders") or {})},
        ],
    )
    _append_paper_order_table(lines, "By portfolio status", summary.get("by_portfolio_status") or [])
    _append_paper_order_table(lines, "By portfolio reason", summary.get("by_portfolio_reason") or [])
    _append_paper_order_table(lines, "By sector", summary.get("by_sector") or [])
    lines.extend(
        [
            "",
            "## Sample portfolio-risk paper orders",
            "",
            "| Paper order id | Portfolio status | Reason | Symbol | Sector | Qty | PnL | Equity after |",
            "| --- | --- | --- | --- | --- | ---: | ---: | ---: |",
        ]
    )
    for order in summary.get("sample_orders") or []:
        after = order.get("portfolio_state_after") or {}
        lines.append(
            f"| {order.get('paper_order_id')} | {order.get('risk_status')} | {order.get('risk_reason')} "
            f"| {order.get('symbol')} | {order.get('sector')} "
            f"| {float(order.get('quantity_units') or 0.0):.2f} "
            f"| {float(order.get('portfolio_realized_pnl') or 0.0):+.2f} "
            f"| {float(after.get('equity') or 0.0):.2f} |"
        )
    lines.extend(
        [
            "",
            "## Interpretation note",
            "",
            "This is still paper-only. The virtual portfolio state makes the risk wrapper more realistic than static caps, but it does not replace live account, margin, liquidity, volatility, borrowing, short-sale, or execution checks.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_paper_portfolio_risk_layer_markdown(
    output_path: Path,
    summary: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        paper_portfolio_risk_layer_markdown(summary, filters=filters),
        encoding="utf-8",
    )
    return output_path


def _variant_row_key(row: dict[str, Any]) -> tuple[int, str, str]:
    return (
        int(row.get("outcome_id") or 0),
        str(row.get("horizon") or "").upper(),
        str(row.get("price_source") or ""),
    )


def _variant_action_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    method_counts: Counter[str] = Counter()
    for row in rows:
        action, _ = _trade_action_from_score(row)
        counts[action] += 1
        try:
            raw = json.loads(str(row.get("raw_json") or "{}"))
        except json.JSONDecodeError:
            raw = {}
        debias = raw.get("debias") if isinstance(raw, dict) else {}
        if isinstance(debias, dict):
            method_counts[str(debias.get("method") or "unknown")] += 1
    return {
        "actions": dict(counts),
        "methods": dict(method_counts),
    }


def _variant_compare_score(summary: dict[str, Any]) -> float:
    tradeable = summary.get("tradeable_directional") or {}
    observe_only = summary.get("observe_only_directional") or {}
    accuracy = float(tradeable.get("directional_accuracy") or 0.0)
    coverage = float(summary.get("directional_coverage_after_gate") or 0.0)
    obs_accuracy = observe_only.get("directional_accuracy")
    obs_penalty = float(obs_accuracy) if obs_accuracy is not None else 0.0
    signed_delta = float(summary.get("signed_excess_return_delta") or 0.0)
    return accuracy + 0.08 * coverage + 0.50 * signed_delta - 0.04 * obs_penalty


def variant_comparison_summary(
    variant_rows: dict[str, list[dict[str, Any]]],
    *,
    base_variant: str | None = None,
    min_group_rows: int = 1,
) -> dict[str, Any]:
    indexed: dict[str, dict[tuple[int, str, str], dict[str, Any]]] = {
        name: {_variant_row_key(row): row for row in rows}
        for name, rows in variant_rows.items()
    }
    key_sets = [set(rows_by_key.keys()) for rows_by_key in indexed.values() if rows_by_key]
    common_keys = set.intersection(*key_sets) if key_sets else set()
    ordered_keys = sorted(common_keys, key=lambda item: (item[1], item[2], item[0]))
    variant_summaries: list[dict[str, Any]] = []
    for name, rows_by_key in indexed.items():
        common_rows = [rows_by_key[key] for key in ordered_keys if key in rows_by_key]
        summary = action_gate_summary(common_rows, min_group_rows=min_group_rows)
        all_directional = summary["all_directional"]
        tradeable = summary["tradeable_directional"]
        observe_only = summary["observe_only_directional"]
        variant_summaries.append(
            {
                "variant": name,
                "input_rows": len(variant_rows.get(name) or []),
                "common_rows": len(common_rows),
                "all_directional_hits": all_directional["directional_hits"],
                "all_directional_rows": all_directional["directional_rows"],
                "all_directional_accuracy": all_directional["directional_accuracy"],
                "tradeable_directional_hits": tradeable["directional_hits"],
                "tradeable_directional_rows": tradeable["directional_rows"],
                "tradeable_directional_accuracy": tradeable["directional_accuracy"],
                "observe_only_directional_hits": observe_only["directional_hits"],
                "observe_only_directional_rows": observe_only["directional_rows"],
                "observe_only_directional_accuracy": observe_only["directional_accuracy"],
                "coverage": summary["directional_coverage_after_gate"],
                "accuracy_delta_after_gate": summary["directional_accuracy_delta"],
                "signed_excess_return_delta": summary["signed_excess_return_delta"],
                "avg_tradeable_signed_excess_return": tradeable["avg_signed_excess_return"],
                "avg_observe_only_signed_excess_return": observe_only["avg_signed_excess_return"],
                "score": _variant_compare_score(summary),
                "action_counts": _variant_action_counts(common_rows),
                "summary": summary,
            }
        )
    variant_summaries.sort(
        key=lambda item: (
            -float(item["score"]),
            -float(item["tradeable_directional_accuracy"] or 0.0),
            -float(item["coverage"] or 0.0),
            item["variant"],
        )
    )
    by_name = {item["variant"]: item for item in variant_summaries}
    base = by_name.get(base_variant or "") if base_variant else None
    deltas = []
    if base is not None:
        for item in variant_summaries:
            if item["variant"] == base["variant"]:
                continue
            deltas.append(
                {
                    "variant": item["variant"],
                    "base_variant": base["variant"],
                    "tradeable_accuracy_delta": (
                        float(item["tradeable_directional_accuracy"]) - float(base["tradeable_directional_accuracy"])
                        if item["tradeable_directional_accuracy"] is not None and base["tradeable_directional_accuracy"] is not None
                        else None
                    ),
                    "coverage_delta": (
                        float(item["coverage"]) - float(base["coverage"])
                        if item["coverage"] is not None and base["coverage"] is not None
                        else None
                    ),
                    "observe_only_accuracy_delta": (
                        float(item["observe_only_directional_accuracy"]) - float(base["observe_only_directional_accuracy"])
                        if item["observe_only_directional_accuracy"] is not None and base["observe_only_directional_accuracy"] is not None
                        else None
                    ),
                    "signed_excess_return_delta_delta": (
                        float(item["signed_excess_return_delta"]) - float(base["signed_excess_return_delta"])
                        if item["signed_excess_return_delta"] is not None and base["signed_excess_return_delta"] is not None
                        else None
                    ),
                    "score_delta": float(item["score"]) - float(base["score"]),
                }
            )
    return {
        "rows_by_variant": {name: len(rows) for name, rows in variant_rows.items()},
        "variants_requested": list(variant_rows.keys()),
        "base_variant": base_variant,
        "common_rows": len(ordered_keys),
        "common_directional_rows": max((int(item["all_directional_rows"]) for item in variant_summaries), default=0),
        "variant_summaries": variant_summaries,
        "best_variant": variant_summaries[0]["variant"] if variant_summaries else None,
        "deltas_vs_base": deltas,
        "safety": {
            "mode": "read_only_stored_score_variant_comparison",
            "policy": "No OpenAI, IBKR account, portfolio, order, or scorer-row mutation.",
        },
    }


def variant_comparison_markdown(
    summary: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
    title: str = "Replay Score Variant Comparison Report",
) -> str:
    filters = filters or {}
    lines = [
        f"# {title}",
        "",
        f"- Generated at: `{datetime.now(UTC).isoformat()}`",
        f"- Variants requested: `{', '.join(summary.get('variants_requested') or [])}`",
        f"- Base variant: `{summary.get('base_variant') or '-'}`",
        f"- Common rows: `{summary.get('common_rows')}`",
        f"- Best score variant: `{summary.get('best_variant') or '-'}`",
        "- Safety: read-only comparison over stored `replay_model_scores`; no OpenAI, IBKR account, portfolio, order, or scorer mutation.",
        "",
        "## Filters",
        "",
    ]
    if filters:
        for key, value in filters.items():
            lines.append(f"- `{key}`: `{value if value is not None else '-'}`")
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "## Variant scoreboard",
            "",
            "| Rank | Variant | Input rows | Common rows | All directional | Tradeable directional | Observe-only directional | Coverage | Acc delta | Signed excess delta | Score | Actions |",
            "| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    for index, item in enumerate(summary.get("variant_summaries") or [], start=1):
        actions = item.get("action_counts", {}).get("actions", {})
        action_text = ", ".join(f"{key}:{value}" for key, value in sorted(actions.items())) or "-"
        lines.append(
            f"| {index} | {item['variant']} | {item['input_rows']} | {item['common_rows']} "
            f"| {item['all_directional_hits']}/{item['all_directional_rows']} = {_fmt_optional_pct(item['all_directional_accuracy'])} "
            f"| {item['tradeable_directional_hits']}/{item['tradeable_directional_rows']} = {_fmt_optional_pct(item['tradeable_directional_accuracy'])} "
            f"| {item['observe_only_directional_hits']}/{item['observe_only_directional_rows']} = {_fmt_optional_pct(item['observe_only_directional_accuracy'])} "
            f"| {_fmt_optional_pct(item['coverage'])} "
            f"| {_fmt_optional_signed_pct(item['accuracy_delta_after_gate'])} "
            f"| {_fmt_optional_signed_pct(item['signed_excess_return_delta'])} "
            f"| {float(item['score']):.4f} | {action_text} |"
        )
    lines.extend(
        [
            "",
            "## Deltas vs base",
            "",
            "| Variant | Base | Tradeable acc delta | Coverage delta | Observe-only acc delta | Signed excess delta-delta | Score delta |",
            "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    if not summary.get("deltas_vs_base"):
        lines.append("| - | - | - | - | - | - | - |")
    for item in summary.get("deltas_vs_base") or []:
        lines.append(
            f"| {item['variant']} | {item['base_variant']} "
            f"| {_fmt_optional_signed_pct(item['tradeable_accuracy_delta'])} "
            f"| {_fmt_optional_signed_pct(item['coverage_delta'])} "
            f"| {_fmt_optional_signed_pct(item['observe_only_accuracy_delta'])} "
            f"| {_fmt_optional_signed_pct(item['signed_excess_return_delta_delta'])} "
            f"| {float(item['score_delta']):+.4f} |"
        )
    lines.extend(
        [
            "",
            "## Interpretation note",
            "",
            "The score is a compact operating-candidate heuristic: tradeable accuracy is primary, coverage is rewarded, signed excess-return improvement helps, and a high observe-only accuracy is penalized because it means the gate may be blocking useful rows. Treat this as a shortlist tool, not trading authorization.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_variant_comparison_markdown(
    output_path: Path,
    summary: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        variant_comparison_markdown(summary, filters=filters),
        encoding="utf-8",
    )
    return output_path


def _score_group_key(row: dict[str, Any]) -> tuple[str, str, str, str, str]:
    return (
        str(row.get("scorer_name") or ""),
        str(row.get("model_name") or ""),
        str(row.get("scorer_version") or ""),
        str(row.get("price_source") or ""),
        str(row.get("horizon") or ""),
    )


def _split_oos_rows(
    rows: list[dict[str, Any]],
    *,
    cutoff: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    cutoff_value = str(cutoff)
    train = [row for row in rows if str(row.get("replay_timestamp") or "") < cutoff_value]
    holdout = [row for row in rows if str(row.get("replay_timestamp") or "") >= cutoff_value]
    return train, holdout


def _oos_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    score_summary = summarize_scores(rows)
    calibration = calibration_summary(rows)
    return {
        "rows": score_summary["rows"],
        "evaluated": score_summary["evaluated"],
        "direction_hits": score_summary["direction_hits"],
        "direction_accuracy": score_summary["direction_accuracy"],
        "avg_absolute_raw_error": score_summary["avg_absolute_raw_error"],
        "avg_absolute_excess_error": score_summary["avg_absolute_excess_error"],
        "avg_confidence": calibration["avg_confidence"],
        "expected_calibration_error": calibration["expected_calibration_error"],
    }


def oos_summary(
    rows: list[dict[str, Any]],
    *,
    cutoff: str,
    min_holdout_rows: int = 1,
) -> dict[str, Any]:
    """Summarize stored replay scores before/after a temporal cutoff.

    The function is intentionally read-only and uses only persisted score rows
    that already contain separated outcome labels. It does not build new
    predictions or contact any external service.
    """

    groups: dict[tuple[str, str, str, str, str], list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(_score_group_key(row), []).append(row)

    min_holdout_rows = max(0, int(min_holdout_rows))
    group_summaries: list[dict[str, Any]] = []
    for key, members in groups.items():
        train_rows, holdout_rows = _split_oos_rows(members, cutoff=cutoff)
        train = _oos_metrics(train_rows)
        holdout = _oos_metrics(holdout_rows)
        if holdout["evaluated"] < min_holdout_rows:
            continue
        train_accuracy = train["direction_accuracy"]
        holdout_accuracy = holdout["direction_accuracy"]
        delta = (
            holdout_accuracy - train_accuracy
            if train_accuracy is not None and holdout_accuracy is not None
            else None
        )
        group_summaries.append(
            {
                "scorer_name": key[0],
                "model_name": key[1],
                "scorer_version": key[2],
                "price_source": key[3],
                "horizon": key[4],
                "cutoff": cutoff,
                "train": train,
                "holdout": holdout,
                "holdout_accuracy_minus_train": delta,
            }
        )

    group_summaries.sort(
        key=lambda row: (
            -1.0 if row["holdout"]["direction_accuracy"] is None else -float(row["holdout"]["direction_accuracy"]),
            1.0 if row["holdout"]["avg_absolute_raw_error"] is None else float(row["holdout"]["avg_absolute_raw_error"]),
            -int(row["holdout"]["evaluated"]),
            row["scorer_name"],
            row["model_name"],
            row["price_source"],
            row["horizon"],
        )
    )
    train_all, holdout_all = _split_oos_rows(rows, cutoff=cutoff)
    return {
        "cutoff": cutoff,
        "rows": len(rows),
        "train": _oos_metrics(train_all),
        "holdout": _oos_metrics(holdout_all),
        "groups": group_summaries,
    }


def format_oos_summary(summary: dict[str, Any]) -> str:
    lines = [
        "Replay score temporal holdout summary:",
        (
            f"cutoff={summary['cutoff']} rows={summary['rows']} "
            f"train={summary['train']['evaluated']} holdout={summary['holdout']['evaluated']}"
        ),
        "",
        "Groups ranked by holdout accuracy:",
    ]
    if not summary["groups"]:
        lines.append("No replay model score groups match the selected filters/minimum holdout rows.")
        return "\n".join(lines)
    for index, row in enumerate(summary["groups"], start=1):
        train = row["train"]
        holdout = row["holdout"]
        lines.append(
            f"{index}. {row['scorer_name']}/{row['model_name']} "
            f"version={row['scorer_version']} {row['horizon']} source={row['price_source']} "
            f"train={train['direction_hits']}/{train['evaluated']} acc={_fmt_optional_pct(train['direction_accuracy'])} "
            f"holdout={holdout['direction_hits']}/{holdout['evaluated']} acc={_fmt_optional_pct(holdout['direction_accuracy'])} "
            f"delta={_fmt_optional_signed_pct(row['holdout_accuracy_minus_train'])} "
            f"holdout_conf={_fmt_optional_pct(holdout['avg_confidence'])} "
            f"holdout_ece={_fmt_optional_pct(holdout['expected_calibration_error'])}"
        )
    return "\n".join(lines)


def oos_markdown(
    rows: list[dict[str, Any]],
    *,
    cutoff: str,
    min_holdout_rows: int = 1,
    filters: dict[str, Any] | None = None,
    title: str = "Replay Score Temporal Holdout Report",
) -> str:
    generated_at = datetime.now(UTC).isoformat()
    filters = filters or {}
    summary = oos_summary(rows, cutoff=cutoff, min_holdout_rows=min_holdout_rows)
    lines = [
        f"# {title}",
        "",
        f"- Generated at: `{generated_at}`",
        f"- Cutoff: `{cutoff}`",
        f"- Rows: `{summary['rows']}`",
        f"- Train evaluated: `{summary['train']['evaluated']}`",
        f"- Holdout evaluated: `{summary['holdout']['evaluated']}`",
        "- Safety: read-only temporal split over stored `replay_model_scores`; no IBKR, OpenAI, order, or Lumibot action.",
        "",
        "## Filters",
        "",
    ]
    if filters:
        for key, value in filters.items():
            lines.append(f"- `{key}`: `{value if value is not None else '-'}`")
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "## Holdout leaderboard",
            "",
            "| Rank | Scorer | Model | Version | Source | Horizon | Train hits/eval | Train acc | Holdout hits/eval | Holdout acc | Holdout - train | Holdout confidence | Holdout ECE |",
            "| ---: | --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    if not summary["groups"]:
        lines.append("| - | - | - | - | - | - | 0/0 | - | 0/0 | - | - | - | - |")
    for index, row in enumerate(summary["groups"], start=1):
        train = row["train"]
        holdout = row["holdout"]
        lines.append(
            f"| {index} | {row['scorer_name']} | {row['model_name']} | {row['scorer_version']} "
            f"| {row['price_source']} | {row['horizon']} "
            f"| {train['direction_hits']}/{train['evaluated']} | {_fmt_optional_pct(train['direction_accuracy'])} "
            f"| {holdout['direction_hits']}/{holdout['evaluated']} | {_fmt_optional_pct(holdout['direction_accuracy'])} "
            f"| {_fmt_optional_signed_pct(row['holdout_accuracy_minus_train'])} "
            f"| {_fmt_optional_pct(holdout['avg_confidence'])} "
            f"| {_fmt_optional_pct(holdout['expected_calibration_error'])} |"
        )
    lines.extend(
        [
            "",
            "## Interpretation note",
            "",
            "This is a temporal holdout over already stored replay scores. It is stronger than an in-sample leaderboard, "
            "but small row counts and a manually selected cutoff should still be treated as smoke evidence rather than production proof.",
            "",
        ]
    )
    return "\n".join(lines)


def write_oos_markdown(
    output_path: Path,
    rows: list[dict[str, Any]],
    *,
    cutoff: str,
    min_holdout_rows: int = 1,
    filters: dict[str, Any] | None = None,
) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        oos_markdown(rows, cutoff=cutoff, min_holdout_rows=min_holdout_rows, filters=filters),
        encoding="utf-8",
    )
    return output_path


def _pair_comparison_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    evaluated = [
        row
        for row in rows
        if row.get("base_direction_hit") is not None and row.get("candidate_direction_hit") is not None
    ]
    base_hits = sum(int(row["base_direction_hit"]) for row in evaluated)
    candidate_hits = sum(int(row["candidate_direction_hit"]) for row in evaluated)
    candidate_wins = [
        row
        for row in evaluated
        if int(row["base_direction_hit"]) == 0 and int(row["candidate_direction_hit"]) == 1
    ]
    candidate_losses = [
        row
        for row in evaluated
        if int(row["base_direction_hit"]) == 1 and int(row["candidate_direction_hit"]) == 0
    ]
    both_hit = [
        row
        for row in evaluated
        if int(row["base_direction_hit"]) == 1 and int(row["candidate_direction_hit"]) == 1
    ]
    both_miss = [
        row
        for row in evaluated
        if int(row["base_direction_hit"]) == 0 and int(row["candidate_direction_hit"]) == 0
    ]
    changed_predictions = [
        row
        for row in evaluated
        if str(row.get("base_predicted_direction") or "") != str(row.get("candidate_predicted_direction") or "")
    ]
    base_errors = [
        float(row["base_absolute_excess_error"])
        for row in evaluated
        if row.get("base_absolute_excess_error") is not None
    ]
    candidate_errors = [
        float(row["candidate_absolute_excess_error"])
        for row in evaluated
        if row.get("candidate_absolute_excess_error") is not None
    ]
    base_accuracy = (base_hits / len(evaluated)) if evaluated else None
    candidate_accuracy = (candidate_hits / len(evaluated)) if evaluated else None
    return {
        "rows": len(rows),
        "evaluated": len(evaluated),
        "base_hits": base_hits,
        "candidate_hits": candidate_hits,
        "net_hits": candidate_hits - base_hits,
        "base_accuracy": base_accuracy,
        "candidate_accuracy": candidate_accuracy,
        "delta_accuracy": (
            candidate_accuracy - base_accuracy
            if base_accuracy is not None and candidate_accuracy is not None
            else None
        ),
        "candidate_wins": len(candidate_wins),
        "candidate_losses": len(candidate_losses),
        "both_hit": len(both_hit),
        "both_miss": len(both_miss),
        "changed_predictions": len(changed_predictions),
        "same_predictions": len(evaluated) - len(changed_predictions),
        "avg_base_absolute_excess_error": (sum(base_errors) / len(base_errors)) if base_errors else None,
        "avg_candidate_absolute_excess_error": (
            sum(candidate_errors) / len(candidate_errors)
            if candidate_errors
            else None
        ),
    }


def _pair_group_rows(rows: list[dict[str, Any]], key: str, *, min_rows: int = 1) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if key == "prediction_change":
            value = (
                f"{row.get('actual_direction') or 'unknown'}:"
                f"{row.get('base_predicted_direction') or 'unknown'}"
                f"->{row.get('candidate_predicted_direction') or 'unknown'}"
            )
        else:
            value = str(row.get(key) or "unknown")
        groups.setdefault(value, []).append(row)
    result: list[dict[str, Any]] = []
    for value, members in groups.items():
        metrics = _pair_comparison_metrics(members)
        if metrics["evaluated"] < min_rows:
            continue
        result.append({"bucket": value, **metrics})
    result.sort(
        key=lambda item: (
            -abs(float(item["delta_accuracy"] if item["delta_accuracy"] is not None else 0.0)),
            -int(item["evaluated"]),
            item["bucket"],
        )
    )
    return result


def pair_comparison_summary(
    rows: list[dict[str, Any]],
    *,
    min_group_rows: int = 1,
    top_deltas: int = 10,
) -> dict[str, Any]:
    evaluated = [
        row
        for row in rows
        if row.get("base_direction_hit") is not None and row.get("candidate_direction_hit") is not None
    ]
    wins = [
        row
        for row in evaluated
        if int(row["base_direction_hit"]) == 0 and int(row["candidate_direction_hit"]) == 1
    ]
    losses = [
        row
        for row in evaluated
        if int(row["base_direction_hit"]) == 1 and int(row["candidate_direction_hit"]) == 0
    ]

    def _sort_delta_rows(delta_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(
            delta_rows,
            key=lambda row: (
                -float(
                    row.get("candidate_absolute_excess_error")
                    if row.get("candidate_absolute_excess_error") is not None
                    else row.get("base_absolute_excess_error")
                    if row.get("base_absolute_excess_error") is not None
                    else -1.0
                ),
                str(row.get("replay_timestamp") or ""),
                int(row.get("outcome_id") or 0),
            ),
        )[: max(0, int(top_deltas))]

    return {
        **_pair_comparison_metrics(rows),
        "by_horizon": _pair_group_rows(evaluated, "horizon", min_rows=min_group_rows),
        "by_session": _pair_group_rows(evaluated, "session", min_rows=min_group_rows),
        "by_symbol": _pair_group_rows(evaluated, "symbol", min_rows=min_group_rows),
        "by_actual_direction": _pair_group_rows(evaluated, "actual_direction", min_rows=min_group_rows),
        "prediction_changes": _pair_group_rows(evaluated, "prediction_change", min_rows=min_group_rows),
        "top_candidate_wins": _sort_delta_rows(wins),
        "top_candidate_losses": _sort_delta_rows(losses),
    }


def format_pair_comparison(summary: dict[str, Any]) -> str:
    return (
        "Replay score paired comparison:\n"
        f"rows={summary['rows']} evaluated={summary['evaluated']} "
        f"base={summary['base_hits']}/{summary['evaluated']} "
        f"candidate={summary['candidate_hits']}/{summary['evaluated']} "
        f"net_hits={summary['net_hits']:+d} "
        f"delta_accuracy={_fmt_optional_signed_pct(summary['delta_accuracy'])} "
        f"wins={summary['candidate_wins']} losses={summary['candidate_losses']} "
        f"changed_predictions={summary['changed_predictions']}"
    )


def _append_pair_group_table(lines: list[str], title: str, rows: list[dict[str, Any]]) -> None:
    lines.extend(
        [
            "",
            f"## {title}",
            "",
            "| Bucket | Evaluated | Base hits | Candidate hits | Net hits | Base acc | Candidate acc | Delta acc | Wins | Losses | Changed predictions |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    if not rows:
        lines.append("| - | 0 | 0 | 0 | 0 | - | - | - | 0 | 0 | 0 |")
        return
    for row in rows:
        lines.append(
            f"| {row['bucket']} | {row['evaluated']} | {row['base_hits']} "
            f"| {row['candidate_hits']} | {row['net_hits']:+d} "
            f"| {_fmt_optional_pct(row['base_accuracy'])} "
            f"| {_fmt_optional_pct(row['candidate_accuracy'])} "
            f"| {_fmt_optional_signed_pct(row['delta_accuracy'])} "
            f"| {row['candidate_wins']} | {row['candidate_losses']} "
            f"| {row['changed_predictions']} |"
        )


def pair_comparison_markdown(
    rows: list[dict[str, Any]],
    *,
    filters: dict[str, Any] | None = None,
    min_group_rows: int = 1,
    top_deltas: int = 10,
    title: str = "Replay Score Paired Comparison Report",
) -> str:
    generated_at = datetime.now(UTC).isoformat()
    filters = filters or {}
    summary = pair_comparison_summary(rows, min_group_rows=min_group_rows, top_deltas=top_deltas)
    lines = [
        f"# {title}",
        "",
        f"- Generated at: `{generated_at}`",
        f"- Rows: `{summary['rows']}`",
        f"- Evaluated paired rows: `{summary['evaluated']}`",
        f"- Base hits: `{summary['base_hits']}`",
        f"- Candidate hits: `{summary['candidate_hits']}`",
        f"- Net hits: `{summary['net_hits']:+d}`",
        f"- Base accuracy: `{_fmt_optional_pct(summary['base_accuracy'])}`",
        f"- Candidate accuracy: `{_fmt_optional_pct(summary['candidate_accuracy'])}`",
        f"- Delta accuracy: `{_fmt_optional_signed_pct(summary['delta_accuracy'])}`",
        f"- Candidate wins/losses: `{summary['candidate_wins']}/{summary['candidate_losses']}`",
        f"- Changed predictions: `{summary['changed_predictions']}`",
        "- Safety: read-only paired comparison over stored `replay_model_scores`; no IBKR, OpenAI, order, or Lumibot action.",
        "",
        "## Filters",
        "",
    ]
    if filters:
        for key, value in filters.items():
            lines.append(f"- `{key}`: `{value if value is not None else '-'}`")
    else:
        lines.append("- none")

    _append_pair_group_table(lines, "By horizon", summary["by_horizon"])
    _append_pair_group_table(lines, "By session", summary["by_session"])
    _append_pair_group_table(lines, "By symbol", summary["by_symbol"])
    _append_pair_group_table(lines, "By actual direction", summary["by_actual_direction"])
    _append_pair_group_table(lines, "Prediction changes", summary["prediction_changes"])

    def _append_delta_table(title_text: str, delta_rows: list[dict[str, Any]]) -> None:
        lines.extend(
            [
                "",
                f"## {title_text}",
                "",
                "| Symbol | Horizon | Session | Replay timestamp | Actual | Base pred | Candidate pred | Base expected | Candidate expected | Actual excess | Base hit | Candidate hit |",
                "| --- | --- | --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: |",
            ]
        )
        if not delta_rows:
            lines.append("| - | - | - | - | - | - | - | - | - | - | - | - |")
            return
        for row in delta_rows:
            lines.append(
                f"| {row.get('symbol')} | {row.get('horizon')} | {row.get('session') or '-'} "
                f"| {row.get('replay_timestamp')} | {row.get('actual_direction')} "
                f"| {row.get('base_predicted_direction')} | {row.get('candidate_predicted_direction')} "
                f"| {_fmt_optional_signed_pct(row.get('base_expected_return'))} "
                f"| {_fmt_optional_signed_pct(row.get('candidate_expected_return'))} "
                f"| {_fmt_optional_signed_pct(row.get('actual_excess_return'))} "
                f"| {row.get('base_direction_hit')} | {row.get('candidate_direction_hit')} |"
            )

    _append_delta_table("Largest candidate wins", summary["top_candidate_wins"])
    _append_delta_table("Largest candidate losses", summary["top_candidate_losses"])
    lines.extend(
        [
            "",
            "## Interpretation note",
            "",
            "This report pairs two stored scorer versions on the same replay outcomes. It is useful for scorer tuning, "
            "but it must not be treated as live-trading authorization or used to rewrite historical outcomes.",
            "",
        ]
    )
    return "\n".join(lines)


def write_pair_comparison_markdown(
    output_path: Path,
    rows: list[dict[str, Any]],
    *,
    filters: dict[str, Any] | None = None,
    min_group_rows: int = 1,
    top_deltas: int = 10,
) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        pair_comparison_markdown(
            rows,
            filters=filters,
            min_group_rows=min_group_rows,
            top_deltas=top_deltas,
        ),
        encoding="utf-8",
    )
    return output_path


def _analysis_group_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    evaluated = [row for row in rows if row.get("direction_hit") is not None]
    hits = sum(int(row["direction_hit"]) for row in evaluated)
    misses = len(evaluated) - hits
    raw_errors = [float(row["absolute_raw_error"]) for row in evaluated if row.get("absolute_raw_error") is not None]
    excess_errors = [
        float(row["absolute_excess_error"])
        for row in evaluated
        if row.get("absolute_excess_error") is not None
    ]
    confidence_values = [float(row.get("confidence") or 0.0) for row in evaluated]
    return {
        "rows": len(rows),
        "evaluated": len(evaluated),
        "hits": hits,
        "misses": misses,
        "accuracy": (hits / len(evaluated)) if evaluated else None,
        "miss_rate": (misses / len(evaluated)) if evaluated else None,
        "avg_confidence": (sum(confidence_values) / len(confidence_values)) if confidence_values else None,
        "avg_absolute_raw_error": (sum(raw_errors) / len(raw_errors)) if raw_errors else None,
        "avg_absolute_excess_error": (sum(excess_errors) / len(excess_errors)) if excess_errors else None,
    }


def _group_error_rows(rows: list[dict[str, Any]], key: str, *, min_rows: int = 1) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        value = str(row.get(key) or "unknown")
        groups.setdefault(value, []).append(row)
    result: list[dict[str, Any]] = []
    for value, members in groups.items():
        metrics = _analysis_group_metrics(members)
        if metrics["evaluated"] < min_rows:
            continue
        result.append({"bucket": value, **metrics})
    result.sort(
        key=lambda item: (
            -float(item["miss_rate"] if item["miss_rate"] is not None else -1.0),
            -int(item["evaluated"]),
            item["bucket"],
        )
    )
    return result


def error_analysis_summary(
    rows: list[dict[str, Any]],
    *,
    min_group_rows: int = 1,
    top_misses: int = 10,
) -> dict[str, Any]:
    evaluated = [row for row in rows if row.get("direction_hit") is not None]
    misses = [row for row in evaluated if int(row["direction_hit"]) == 0]
    hits = [row for row in evaluated if int(row["direction_hit"]) == 1]
    confusion: dict[str, dict[str, int]] = {}
    for row in evaluated:
        actual = str(row.get("actual_direction") or "unknown")
        predicted = str(row.get("predicted_direction") or "unknown")
        confusion.setdefault(actual, {}).setdefault(predicted, 0)
        confusion[actual][predicted] += 1

    error_pairs: dict[str, list[dict[str, Any]]] = {}
    for row in misses:
        pair = f"{row.get('actual_direction') or 'unknown'}->{row.get('predicted_direction') or 'unknown'}"
        error_pairs.setdefault(pair, []).append(row)
    pair_rows = []
    for pair, members in error_pairs.items():
        metrics = _analysis_group_metrics(members)
        pair_rows.append({"bucket": pair, **metrics})
    pair_rows.sort(key=lambda item: (-int(item["evaluated"]), item["bucket"]))

    sorted_misses = sorted(
        misses,
        key=lambda row: (
            -float(row.get("absolute_excess_error") if row.get("absolute_excess_error") is not None else -1.0),
            -float(row.get("absolute_raw_error") if row.get("absolute_raw_error") is not None else -1.0),
            str(row.get("replay_timestamp") or ""),
            int(row.get("id") or 0),
        ),
    )[: max(0, int(top_misses))]
    return {
        "rows": len(rows),
        "evaluated": len(evaluated),
        "hits": len(hits),
        "misses": len(misses),
        "accuracy": (len(hits) / len(evaluated)) if evaluated else None,
        "miss_rate": (len(misses) / len(evaluated)) if evaluated else None,
        "avg_confidence": (
            sum(float(row.get("confidence") or 0.0) for row in evaluated) / len(evaluated)
            if evaluated
            else None
        ),
        "confusion": confusion,
        "by_horizon": _group_error_rows(evaluated, "horizon", min_rows=min_group_rows),
        "by_session": _group_error_rows(evaluated, "session", min_rows=min_group_rows),
        "by_symbol": _group_error_rows(evaluated, "symbol", min_rows=min_group_rows),
        "by_event_type": _group_error_rows(evaluated, "event_type", min_rows=min_group_rows),
        "by_actual_direction": _group_error_rows(evaluated, "actual_direction", min_rows=min_group_rows),
        "by_predicted_direction": _group_error_rows(evaluated, "predicted_direction", min_rows=min_group_rows),
        "error_pairs": pair_rows,
        "top_misses": sorted_misses,
    }


def format_error_analysis(summary: dict[str, Any]) -> str:
    return (
        "Replay score error analysis:\n"
        f"rows={summary['rows']} evaluated={summary['evaluated']} "
        f"hits={summary['hits']} misses={summary['misses']} "
        f"accuracy={_fmt_optional_pct(summary['accuracy'])} "
        f"miss_rate={_fmt_optional_pct(summary['miss_rate'])} "
        f"avg_confidence={_fmt_optional_pct(summary['avg_confidence'])}"
    )


def _append_group_table(lines: list[str], title: str, rows: list[dict[str, Any]]) -> None:
    lines.extend(
        [
            "",
            f"## {title}",
            "",
            "| Bucket | Evaluated | Hits | Misses | Accuracy | Miss rate | Avg confidence | Avg abs excess error |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    if not rows:
        lines.append("| - | 0 | 0 | 0 | - | - | - | - |")
        return
    for row in rows:
        lines.append(
            f"| {row['bucket']} | {row['evaluated']} | {row['hits']} | {row['misses']} "
            f"| {_fmt_optional_pct(row['accuracy'])} | {_fmt_optional_pct(row['miss_rate'])} "
            f"| {_fmt_optional_pct(row['avg_confidence'])} | {_fmt_optional_pct(row['avg_absolute_excess_error'])} |"
        )


def error_analysis_markdown(
    rows: list[dict[str, Any]],
    *,
    filters: dict[str, Any] | None = None,
    min_group_rows: int = 1,
    top_misses: int = 10,
    title: str = "Replay Score Error Analysis Report",
) -> str:
    generated_at = datetime.now(UTC).isoformat()
    filters = filters or {}
    summary = error_analysis_summary(rows, min_group_rows=min_group_rows, top_misses=top_misses)
    lines = [
        f"# {title}",
        "",
        f"- Generated at: `{generated_at}`",
        f"- Rows: `{summary['rows']}`",
        f"- Evaluated: `{summary['evaluated']}`",
        f"- Hits: `{summary['hits']}`",
        f"- Misses: `{summary['misses']}`",
        f"- Accuracy: `{_fmt_optional_pct(summary['accuracy'])}`",
        f"- Miss rate: `{_fmt_optional_pct(summary['miss_rate'])}`",
        f"- Average confidence: `{_fmt_optional_pct(summary['avg_confidence'])}`",
        "- Safety: read-only error analysis over stored `replay_model_scores`; no IBKR, OpenAI, order, or Lumibot action.",
        "",
        "## Filters",
        "",
    ]
    if filters:
        for key, value in filters.items():
            lines.append(f"- `{key}`: `{value if value is not None else '-'}`")
    else:
        lines.append("- none")

    lines.extend(
        [
            "",
            "## Confusion matrix",
            "",
            "| Actual \\ Predicted | up | down | flat |",
            "| --- | ---: | ---: | ---: |",
        ]
    )
    for actual in ("up", "down", "flat", "unknown"):
        values = summary["confusion"].get(actual, {})
        if actual == "unknown" and not values:
            continue
        lines.append(f"| {actual} | {values.get('up', 0)} | {values.get('down', 0)} | {values.get('flat', 0)} |")

    _append_group_table(lines, "By horizon", summary["by_horizon"])
    _append_group_table(lines, "By session", summary["by_session"])
    _append_group_table(lines, "By symbol", summary["by_symbol"])
    _append_group_table(lines, "By event type", summary["by_event_type"])
    _append_group_table(lines, "By actual direction", summary["by_actual_direction"])
    _append_group_table(lines, "By predicted direction", summary["by_predicted_direction"])
    _append_group_table(lines, "Error pairs", summary["error_pairs"])

    lines.extend(
        [
            "",
            "## Largest misses",
            "",
            "| Symbol | Horizon | Session | Event | Replay timestamp | Predicted | Actual | Expected | Actual raw | Actual excess | Confidence |",
            "| --- | --- | --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: |",
        ]
    )
    if not summary["top_misses"]:
        lines.append("| - | - | - | - | - | - | - | - | - | - | - |")
    for row in summary["top_misses"]:
        lines.append(
            f"| {row.get('symbol')} | {row.get('horizon')} | {row.get('session') or '-'} "
            f"| {row.get('event_type')}/{row.get('event_subtype')} | {row.get('replay_timestamp')} "
            f"| {row.get('predicted_direction')} | {row.get('actual_direction')} "
            f"| {_fmt_optional_signed_pct(row.get('expected_return'))} "
            f"| {_fmt_optional_signed_pct(row.get('actual_raw_return'))} "
            f"| {_fmt_optional_signed_pct(row.get('actual_excess_return'))} "
            f"| {_fmt_optional_pct(row.get('confidence'))} |"
        )
    lines.extend(
        [
            "",
            "## Interpretation note",
            "",
            "This report describes where a stored scorer failed on already separated replay outcomes. "
            "It should guide the next scorer design, but it must not be used to overwrite historical labels or justify live orders.",
            "",
        ]
    )
    return "\n".join(lines)


def write_error_analysis_markdown(
    output_path: Path,
    rows: list[dict[str, Any]],
    *,
    filters: dict[str, Any] | None = None,
    min_group_rows: int = 1,
    top_misses: int = 10,
) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        error_analysis_markdown(rows, filters=filters, min_group_rows=min_group_rows, top_misses=top_misses),
        encoding="utf-8",
    )
    return output_path


def format_score_report(rows: list[dict[str, Any]]) -> str:
    summary = summarize_scores(rows)
    accuracy_text = "-" if summary["direction_accuracy"] is None else f"{summary['direction_accuracy']:.2%}"
    raw_error_text = (
        "-"
        if summary["avg_absolute_raw_error"] is None
        else f"{summary['avg_absolute_raw_error']:.2%}"
    )
    lines = [
        "Replay model score summary:",
        (
            f"rows={summary['rows']} evaluated={summary['evaluated']} "
            f"hits={summary['direction_hits']} accuracy={accuracy_text} "
            f"avg_abs_raw_error={raw_error_text}"
        ),
        "",
        "Recent replay model scores:",
    ]
    for row in rows:
        hit = "-" if row["direction_hit"] is None else "yes" if int(row["direction_hit"]) else "no"
        actual_raw_text = (
            "-"
            if row["actual_raw_return"] is None
            else f"{float(row['actual_raw_return']):+.2%}"
        )
        lines.append(
            f"#{row['id']} {row['symbol']} {row['event_type']}/{row['event_subtype']} "
            f"{row['horizon']} source={row['price_source']} "
            f"pred={row['predicted_direction']} actual={row['actual_direction']} hit={hit} "
            f"expected={float(row['expected_return']):+.2%} "
            f"actual_raw={actual_raw_text} "
            f"confidence={float(row['confidence']):.0%} model={row['model_name']}"
        )
    return "\n".join(lines)


def _pct(value: Any) -> str:
    if value is None:
        return "-"
    return f"{float(value):.2%}"


def format_leaderboard(rows: list[dict[str, Any]]) -> str:
    lines = ["Replay model leaderboard:"]
    if not rows:
        lines.append("No replay model score groups match the selected filters.")
        return "\n".join(lines)
    for index, row in enumerate(rows, start=1):
        lines.append(
            f"{index}. {row['scorer_name']}/{row['model_name']} "
            f"version={row['scorer_version']} "
            f"{row['horizon']} source={row['price_source']} "
            f"rows={row['rows']} evaluated={row['evaluated']} "
            f"accuracy={_pct(row['direction_accuracy'])} "
            f"avg_abs_raw_error={_pct(row['avg_absolute_raw_error'])} "
            f"avg_abs_excess_error={_pct(row['avg_absolute_excess_error'])} "
            f"confidence={_pct(row['avg_confidence'])}"
        )
    return "\n".join(lines)


def leaderboard_markdown(
    rows: list[dict[str, Any]],
    *,
    filters: dict[str, Any] | None = None,
    title: str = "Replay Model Leaderboard",
) -> str:
    generated_at = datetime.now(UTC).isoformat()
    filters = filters or {}
    lines = [
        f"# {title}",
        "",
        f"- Generated at: `{generated_at}`",
        f"- Groups: `{len(rows)}`",
        "- Safety: read-only leaderboard over stored `replay_model_scores`; no IBKR, OpenAI, order, or Lumibot action.",
        "",
        "## Filters",
        "",
    ]
    if filters:
        for key, value in filters.items():
            lines.append(f"- `{key}`: `{value if value is not None else '-'}`")
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "## Leaderboard",
            "",
            "| Rank | Scorer | Model | Version | Source | Horizon | Rows | Evaluated | Hits | Accuracy | Avg abs raw error | Avg abs excess error | Avg confidence |",
            "| ---: | --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    if not rows:
        lines.append("| - | - | - | - | - | - | 0 | 0 | 0 | - | - | - | - |")
    for index, row in enumerate(rows, start=1):
        lines.append(
            f"| {index} | {row['scorer_name']} | {row['model_name']} | {row['scorer_version']} "
            f"| {row['price_source']} | {row['horizon']} | {row['rows']} | {row['evaluated']} "
            f"| {row['direction_hits']} | {_pct(row['direction_accuracy'])} "
            f"| {_pct(row['avg_absolute_raw_error'])} | {_pct(row['avg_absolute_excess_error'])} "
            f"| {_pct(row['avg_confidence'])} |"
        )
    lines.extend(
        [
            "",
            "## Interpretation note",
            "",
            "This leaderboard compares stored replay predictions against already separated outcome labels. "
            "Small row counts should be treated as smoke-test evidence, not production model performance.",
            "",
        ]
    )
    return "\n".join(lines)


def write_leaderboard_markdown(
    output_path: Path,
    rows: list[dict[str, Any]],
    *,
    filters: dict[str, Any] | None = None,
) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(leaderboard_markdown(rows, filters=filters), encoding="utf-8")
    return output_path
