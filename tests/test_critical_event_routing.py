from __future__ import annotations

from types import SimpleNamespace

import pytest

from recorder.ai_provider import (
    OpenAINewsAnalyzer,
    OpenAIModelRouter,
    PermanentAnalysisError,
    ProviderResult,
)
from recorder.analysis_models import HorizonPrediction, NewsAnalysis
from recorder.analysis_service import AIAnalysisService
from recorder.prompting import (
    detect_event_profile,
    is_obvious_market_price_update,
    system_prompt_for_profile,
)


def make_analysis(
    event_type: str = "product",
    *,
    impact: float = 0.4,
    confidence: float = 0.8,
    causal: bool = True,
    eligible: bool = True,
) -> NewsAnalysis:
    predictions = []
    if eligible:
        predictions = [
            HorizonPrediction(
                symbol="MU",
                horizon=h,
                expected_return=0.01,
                bear_return=-0.02,
                bull_return=0.04,
                up_probability=0.55,
                flat_probability=0.20,
                down_probability=0.25,
                confidence=confidence,
            )
            for h in ("15m", "1h", "close", "1d", "3d")
        ]
    return NewsAnalysis(
        primary_symbol="MU",
        related_symbols=["MU"],
        event_type=event_type,
        event_subtype=f"{event_type}_test",
        event_stage="initial_release",
        analysis_depth="headline_only",
        surprise_direction="positive",
        market_expectation_context="",
        key_facts=[],
        interpretation_flags=[],
        sentiment="positive",
        causal_signal=causal,
        prediction_eligible=eligible,
        analysis_priority="high" if impact >= 0.75 else "normal",
        impact_score=impact,
        novelty_score=0.7,
        source_quality=0.8,
        confidence=confidence,
        time_horizon="1_to_3_days",
        summary="test analysis",
        bull_case=["positive catalyst"],
        bear_case=["uncertainty"],
        data_limitations=[],
        predictions=predictions,
    )


class StaticAnalyzer:
    provider_name = "openai"

    def __init__(self, model_name: str, analysis: NewsAnalysis):
        self.model_name = model_name
        self.analysis = analysis
        self.calls = 0

    def analyze(self, snapshot):
        self.calls += 1
        return ProviderResult(
            analysis=self.analysis.model_copy(deep=True),
            raw_json=self.analysis.model_dump_json(),
            response_id=f"resp-{self.model_name}",
            input_tokens=10,
            output_tokens=20,
            duration_ms=30,
            provider_name="openai",
            model_name=self.model_name,
            route_name=str(snapshot.get("analysis_profile", "general")),
            reasoning_effort="high" if self.model_name == "gpt-5.5" else "low",
        )


class PermanentFailureAnalyzer:
    provider_name = "openai"
    model_name = "gpt-5.5"

    def analyze(self, snapshot):
        raise PermanentAnalysisError("model unavailable")


def make_router(primary, critical):
    return OpenAIModelRouter(
        primary=primary,
        critical=critical,
        critical_event_types=("earnings", "guidance", "fomc", "fed_speech", "geopolitical"),
        escalate_impact_score=0.75,
        escalate_confidence_below=0.55,
        fallback_to_primary=True,
        fallback_on_transient=False,
    )


def test_event_profile_detects_earnings_guidance_and_fomc():
    assert detect_event_profile("Micron reports quarterly earnings").name == "earnings"
    assert detect_event_profile("Micron raises full-year guidance").name == "guidance"
    assert detect_event_profile("FOMC holds rates; Powell press conference ahead").name == "fomc"
    assert detect_event_profile("Oil jumps after missile strike raises Middle East risk").name == "geopolitical"
    assert detect_event_profile("Holder registers shares on Form S-3").name == "filing"


def test_market_move_with_critical_event_is_not_locally_discarded():
    assert is_obvious_market_price_update("QQQ ETF Falls 3.3%") is True
    assert (
        is_obvious_market_price_update(
            "Dow Futures Rise As Micron Earnings Send Chip Stocks Higher"
        )
        is False
    )


def test_critical_profile_routes_directly_to_gpt55():
    primary = StaticAnalyzer("gpt-5.4-mini", make_analysis())
    critical = StaticAnalyzer("gpt-5.5", make_analysis("earnings", impact=0.9))
    result = make_router(primary, critical).analyze(
        {"analysis_profile": "earnings", "deterministic_event_hint": "earnings"}
    )
    assert primary.calls == 0
    assert critical.calls == 1
    assert result.model_name == "gpt-5.5"


def test_geopolitical_profile_routes_directly_to_gpt55():
    primary = StaticAnalyzer("gpt-5.4-mini", make_analysis())
    critical = StaticAnalyzer("gpt-5.5", make_analysis("geopolitical", impact=0.85))
    result = make_router(primary, critical).analyze(
        {"analysis_profile": "geopolitical", "deterministic_event_hint": "geopolitical"}
    )
    assert primary.calls == 0
    assert critical.calls == 1
    assert result.model_name == "gpt-5.5"


def test_high_impact_primary_result_is_reviewed_by_gpt55_and_usage_is_aggregated():
    primary = StaticAnalyzer("gpt-5.4-mini", make_analysis("product", impact=0.9))
    critical = StaticAnalyzer("gpt-5.5", make_analysis("product", impact=0.9))
    result = make_router(primary, critical).analyze(
        {"analysis_profile": "general", "deterministic_event_hint": "other"}
    )
    assert primary.calls == 1
    assert critical.calls == 1
    assert result.model_name == "gpt-5.5"
    assert result.input_tokens == 20
    assert result.output_tokens == 40
    assert result.duration_ms == 60


def test_unavailable_critical_model_falls_back_without_crashing():
    primary = StaticAnalyzer("gpt-5.4-mini", make_analysis("earnings", impact=0.9))
    router = make_router(primary, PermanentFailureAnalyzer())
    result = router.analyze(
        {"analysis_profile": "earnings", "deterministic_event_hint": "earnings"}
    )
    assert primary.calls == 1
    assert result.model_name == "gpt-5.4-mini"
    assert any("fallback" in item.lower() for item in result.analysis.data_limitations)


def test_critical_prompts_include_event_specific_protocols():
    assert "reported-quarter results" in system_prompt_for_profile("earnings")
    assert "dot plot" in system_prompt_for_profile("fomc")
    assert "registration statement" in system_prompt_for_profile("filing")
    assert "energy-supply disruption" in system_prompt_for_profile("geopolitical")


def test_headline_only_critical_event_has_confidence_cap():
    analysis = make_analysis("guidance", impact=0.9, confidence=0.9)
    AIAnalysisService._apply_quality_guards(
        analysis,
        {"article_body_available_to_model": False},
    )
    assert analysis.analysis_depth == "headline_only"
    assert analysis.confidence <= 0.62
    assert all(pred.confidence <= 0.58 for pred in analysis.predictions)
    assert any("headline-only" in item for item in analysis.data_limitations)


def test_openai_parse_call_receives_reasoning_effort():
    analysis = make_analysis("fomc", impact=0.9)
    content = SimpleNamespace(type="output_text", parsed=analysis)
    response = SimpleNamespace(
        id="resp_test",
        output=[SimpleNamespace(type="message", content=[content])],
        usage=SimpleNamespace(input_tokens=100, output_tokens=200),
    )

    class FakeResponses:
        def parse(self, **kwargs):
            assert kwargs["model"] == "gpt-5.5"
            assert kwargs["reasoning"] == {"effort": "high"}
            return response

    analyzer = OpenAINewsAnalyzer(
        api_key="test",
        model="gpt-5.5",
        timeout_seconds=180,
        reasoning_effort="high",
        profile_name="fomc",
        client=SimpleNamespace(responses=FakeResponses()),
    )
    result = analyzer.analyze({"analysis_profile": "fomc", "headline": "FOMC"})
    assert result.model_name == "gpt-5.5"
    assert result.reasoning_effort == "high"
