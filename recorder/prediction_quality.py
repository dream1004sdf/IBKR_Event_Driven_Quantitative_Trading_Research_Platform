from __future__ import annotations

from dataclasses import dataclass

from .analysis_models import HorizonPrediction, NewsAnalysis


@dataclass(frozen=True)
class PredictionQualityDecision:
    status: str  # valid | calibration_only | observe_only | invalid
    reason: str | None
    valid_for_directional_trading: bool
    valid_for_probability_calibration: bool


_DEFAULT_MAX_RANGE_WIDTH = {
    "15m": 0.05,
    "1h": 0.08,
    "close": 0.12,
    "1d": 0.16,
    "3d": 0.24,
}


def _decision(
    status: str,
    reason: str | None,
    *,
    directional: bool = False,
    calibration: bool = False,
) -> PredictionQualityDecision:
    return PredictionQualityDecision(status, reason, directional, calibration)


def evaluate_prediction_quality(
    analysis: NewsAnalysis,
    prediction: HorizonPrediction,
    *,
    min_analysis_confidence: float,
    min_prediction_confidence: float,
    min_top_probability: float,
    min_probability_edge: float,
    max_range_width_by_horizon: dict[str, float] | None = None,
    official_required_event_types: tuple[str, ...] = (),
    min_validated_facts_without_official: int = 2,
    integrity_partial_calibration_only: bool = True,
) -> PredictionQualityDecision:
    if analysis.event_stage == "retrospective":
        return _decision(
            "observe_only",
            "retrospective event; retain for reaction observation only",
        )
    if analysis.text_integrity_status == "failed":
        return _decision("observe_only", "text integrity validation failed")
    if "integrity_recovery_unavailable_observe_only" in analysis.interpretation_flags:
        return _decision("observe_only", "integrity recovery unavailable")
    if analysis.economic_plausibility_status == "failed":
        return _decision(
            "observe_only",
            "economic plausibility validation failed; external confirmation required",
        )

    if analysis.confidence < min_analysis_confidence:
        return _decision(
            "observe_only",
            f"analysis confidence {analysis.confidence:.2f} below {min_analysis_confidence:.2f}",
        )
    if prediction.confidence < min_prediction_confidence:
        return _decision(
            "observe_only",
            f"prediction confidence {prediction.confidence:.2f} below {min_prediction_confidence:.2f}",
        )

    limits = dict(_DEFAULT_MAX_RANGE_WIDTH)
    if max_range_width_by_horizon:
        limits.update(max_range_width_by_horizon)
    width = prediction.bull_return - prediction.bear_return
    limit = limits.get(prediction.horizon)
    if limit is not None and width > limit:
        return _decision(
            "observe_only",
            f"forecast range width {width:.2%} exceeds {limit:.2%} for {prediction.horizon}",
        )

    required = {value.lower() for value in official_required_event_types}
    if analysis.event_type.lower() in required and analysis.official_source_status != "attached":
        if analysis.validated_fact_count >= min_validated_facts_without_official:
            return _decision(
                "calibration_only",
                (
                    f"official source required for {analysis.event_type}; "
                    f"secondary report retained for probability calibration only"
                ),
                calibration=True,
            )
        return _decision(
            "observe_only",
            f"official source required for {analysis.event_type} and grounded facts are insufficient",
        )

    if (
        integrity_partial_calibration_only
        and "integrity_recovery_partial_calibration_only" in analysis.interpretation_flags
    ):
        return _decision(
            "calibration_only",
            "integrity recovery was partial; directional use disabled",
            calibration=True,
        )
    if analysis.economic_plausibility_status == "warning":
        return _decision(
            "calibration_only",
            "economic plausibility is unresolved; directional use disabled",
            calibration=True,
        )

    # A flat-dominant distribution is useful for probability calibration, but it is not
    # a directional trading edge.  This prevents Form 144-style 53% flat forecasts from
    # being counted as valid directional signals.
    probabilities = {
        "up": prediction.up_probability,
        "flat": prediction.flat_probability,
        "down": prediction.down_probability,
    }
    ranked = sorted(probabilities.items(), key=lambda item: item[1], reverse=True)
    top_name, top_value = ranked[0]
    second_value = ranked[1][1]

    if top_value < min_top_probability:
        return _decision(
            "observe_only",
            f"largest probability {top_value:.2f} below {min_top_probability:.2f}",
        )
    if top_value - second_value < min_probability_edge:
        return _decision(
            "observe_only",
            f"probability edge {top_value - second_value:.2f} below {min_probability_edge:.2f}",
        )
    if top_name == "flat":
        return _decision(
            "calibration_only",
            "flat outcome has the highest probability; no directional edge",
            calibration=True,
        )

    return _decision(
        "valid",
        None,
        directional=True,
        calibration=True,
    )
