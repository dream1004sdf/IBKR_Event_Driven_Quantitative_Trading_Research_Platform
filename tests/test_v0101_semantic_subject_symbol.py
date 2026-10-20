from __future__ import annotations

import pytest

from recorder.analysis_models import NewsAnalysis


def _payload() -> dict:
    return {
        "primary_symbol": "QQQ",
        "related_symbols": ["QQQ", "SPY"],
        "event_scope": "market",
        "subject_entity": "nasdaq_100",
        "event_subject_symbol": "",
        "subject_asset_type": "etf",
        "target_asset_type": "etf",
        "target_role": "market_proxy",
        "target_assets": ["QQQ"],
        "attribution_status": "matched",
        "attribution_confidence": 0.98,
        "original_subscription_symbol": "QQQ",
        "subject_target_available": True,
        "attribution_reason": "headline is about QQQ",
        "event_type": "market_price_update",
        "event_subtype": "price_move",
        "event_stage": "retrospective",
        "analysis_depth": "article_body",
        "surprise_direction": "unknown",
        "sentiment": "neutral",
        "causal_signal": False,
        "prediction_eligible": False,
        "reaction_observation_eligible": False,
        "analysis_priority": "low",
        "impact_score": 0.1,
        "novelty_score": 0.1,
        "source_quality": 0.8,
        "confidence": 0.9,
        "time_horizon": "intraday",
        "summary": "Retrospective QQQ market update.",
        "predictions": [],
    }


def test_semantic_nasdaq_subject_is_not_rejected_as_ticker() -> None:
    payload = _payload()
    payload["event_subject_symbol"] = "nasdaq_100"
    analysis = NewsAnalysis.model_validate(payload)
    assert analysis.subject_entity == "nasdaq_100"
    assert analysis.event_subject_symbol == ""


@pytest.mark.parametrize(
    "semantic_value",
    ["us_equities", "pce", "fomc", "semiconductors", "long_duration_treasuries"],
)
def test_other_semantic_subjects_normalize_to_empty_symbol(semantic_value: str) -> None:
    payload = _payload()
    payload["event_subject_symbol"] = semantic_value
    analysis = NewsAnalysis.model_validate(payload)
    assert analysis.event_subject_symbol == ""


def test_real_ticker_subject_remains_strict_and_uppercase() -> None:
    payload = _payload()
    payload.update(
        {
            "primary_symbol": "MU",
            "event_scope": "company",
            "subject_entity": "MU",
            "event_subject_symbol": "mu",
            "subject_asset_type": "stock",
            "target_asset_type": "stock",
            "target_role": "subject_asset",
            "target_assets": ["MU"],
            "original_subscription_symbol": "MU",
        }
    )
    analysis = NewsAnalysis.model_validate(payload)
    assert analysis.event_subject_symbol == "MU"
