from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from recorder.ai_provider import ProviderResult
from recorder.analysis_database import AnalysisDatabase
from recorder.analysis_models import EventFact, HorizonPrediction, NewsAnalysis
from recorder.analysis_service import AIAnalysisService
from recorder.config import Settings
from recorder.economic_plausibility import (
    EconomicPlausibilityConfig,
    assess_economic_plausibility,
)
from recorder.models import NewsInput, QuoteSnapshot

UTC = timezone.utc
HORIZONS = ("15m", "1h", "close", "1d", "3d")


def _predictions(symbol: str = "MU") -> list[HorizonPrediction]:
    return [
        HorizonPrediction(
            symbol=symbol,
            horizon=h,
            expected_return=0.01,
            bear_return=-0.01,
            bull_return=0.03,
            up_probability=0.60,
            flat_probability=0.24,
            down_probability=0.16,
            confidence=0.70,
        )
        for h in HORIZONS
    ]


def _target_analysis(new: str, prior: str) -> NewsAnalysis:
    return NewsAnalysis(
        primary_symbol="MU",
        related_symbols=["MU"],
        event_scope="company",
        subject_entity="MU",
        event_subject_symbol="MU",
        subject_asset_type="stock",
        target_asset_type="stock",
        target_role="subject_asset",
        target_assets=["MU"],
        event_type="analyst_action",
        event_subtype="price target raise",
        event_stage="initial_release",
        analysis_depth="article_body",
        surprise_direction="positive",
        sentiment="positive",
        causal_signal=True,
        prediction_eligible=True,
        reaction_observation_eligible=False,
        analysis_priority="normal",
        impact_score=0.42,
        novelty_score=0.55,
        source_quality=0.80,
        source_tier="reputable_secondary",
        official_source_status="not_available",
        confidence=0.72,
        time_horizon="1_to_3_days",
        summary="RBC raised its Micron price target.",
        key_facts=[
            EventFact(
                name="Price target raised",
                actual=new,
                prior=prior,
                direction="positive",
                importance="high",
            ),
            EventFact(name="Analyst firm", actual="RBC Capital", importance="medium"),
        ],
        predictions=_predictions(),
    )


def _snapshot(*, reference: float | None = 100.0, official_split: bool = False) -> dict:
    baselines = []
    if reference is not None:
        baselines.append(
            {
                "symbol": "MU",
                "selected_price": reference,
                "selected_price_source": "last",
                "last": reference,
            }
        )
    official = []
    body = "RBC raised Micron's price target."
    if official_split:
        body += " The values are split-adjusted after a stock split."
        official = [
            {
                "source_type": "company_ir",
                "title": "Official stock split notice",
                "content": "The company completed a stock split and all prices are split-adjusted.",
                "status": "manual",
            }
        ]
    return {
        "headline": "Micron price target raised",
        "article_body": body,
        "baseline_market_snapshot": baselines,
        "official_evidence": official,
    }


def test_implausible_target_is_failed_against_event_time_price():
    analysis = _target_analysis("$1500.00/share", "$1200.00/share")
    assess_economic_plausibility(analysis, _snapshot(reference=100.0), EconomicPlausibilityConfig())
    assert analysis.economic_plausibility_status == "failed"
    assert analysis.economic_reference_price == 100.0
    assert analysis.economic_currency == "USD"
    assert analysis.split_adjustment_status == "unknown"
    assert analysis.source_confirmation_count == 1
    assert "economic_plausibility_failed" in analysis.interpretation_flags
    assert any("15.00" in note for note in analysis.economic_validation_notes)


def test_normal_target_is_valid():
    analysis = _target_analysis("$150.00/share", "$120.00/share")
    assess_economic_plausibility(analysis, _snapshot(reference=100.0), EconomicPlausibilityConfig())
    assert analysis.economic_plausibility_status == "valid"
    assert analysis.economic_anomaly_score == 0.0
    assert analysis.split_adjustment_status == "not_required"


def test_missing_reference_price_is_calibration_warning():
    analysis = _target_analysis("$150.00/share", "$120.00/share")
    assess_economic_plausibility(analysis, _snapshot(reference=None), EconomicPlausibilityConfig())
    assert analysis.economic_plausibility_status == "warning"
    assert analysis.economic_reference_price is None
    assert any("reference price was unavailable" in note for note in analysis.economic_validation_notes)


def test_confirmed_split_downgrades_anomaly_to_warning_not_directional_valid():
    analysis = _target_analysis("$1500.00/share", "$1200.00/share")
    assess_economic_plausibility(
        analysis,
        _snapshot(reference=100.0, official_split=True),
        EconomicPlausibilityConfig(),
    )
    assert analysis.economic_plausibility_status == "warning"
    assert analysis.split_adjustment_status == "confirmed"
    assert analysis.source_confirmation_count == 2


def test_earnings_actual_expected_magnitude_anomaly_is_failed():
    analysis = _target_analysis("$150.00/share", "$120.00/share")
    analysis.event_type = "earnings"
    analysis.event_subtype = "quarterly earnings"
    analysis.key_facts = [
        EventFact(
            name="Adjusted EPS",
            actual="$2500.00",
            expected="$20.00",
            direction="positive",
            importance="high",
        )
    ]
    assess_economic_plausibility(analysis, _snapshot(reference=100.0), EconomicPlausibilityConfig())
    assert analysis.economic_plausibility_status == "failed"
    assert any("actual/expected magnitude ratio" in note for note in analysis.economic_validation_notes)


class StaticAnalyzer:
    provider_name = "test"
    model_name = "test-model"

    def __init__(self, analysis: NewsAnalysis):
        self.analysis = analysis

    def analyze(self, snapshot):
        clone = self.analysis.model_copy(deep=True)
        return ProviderResult(
            analysis=clone,
            raw_json=clone.model_dump_json(),
            response_id="economic-test",
            input_tokens=10,
            output_tokens=20,
            duration_ms=5,
            provider_name="test",
            model_name="test-model",
            route_name="test",
            reasoning_effort="low",
        )


def _settings(path: Path) -> Settings:
    base = Settings(
        database_path=path,
        ai_provider="test",
        ai_model="test-model",
        ai_enable_critical_model=False,
        ai_prompt_version="news-agent-v7-economic-plausibility-test",
        ai_body_wait_seconds=0,
        ai_batch_size=5,
        ai_backfill_limit=100,
        ai_backfill_max_age_days=30,
        ai_use_article_body=True,
    )
    return replace(base, economic_target_max_price_multiple=3.0)


def _add_target_news(db: AnalysisDatabase) -> None:
    at = datetime.now(UTC) - timedelta(seconds=10)
    headline = "Micron Technology Price Target Raised to $1500.00/Share From $1200.00 by RBC Capital"
    news_id = db.insert_news(
        NewsInput(
            provider="DJ-N",
            article_id="economic-target",
            headline=headline,
            published_at=at,
            received_at=at,
            primary_symbol="MU",
            con_id=1,
            extra_data="",
        ),
        normalized_headline=headline.lower(),
        headline_hash="economic-target-hash",
    )
    db.update_article_body(news_id, headline)
    db.add_news_symbol(news_id, "MU", 1, at)
    db.insert_baseline(
        news_id,
        QuoteSnapshot(
            symbol="MU",
            timestamp=at,
            bid=99.9,
            ask=100.1,
            last=100.0,
            close=98.0,
            volume=1_000_000,
        ),
    )
    for horizon, minutes in {"15m": 15, "1h": 60, "close": 240, "1d": 1440, "3d": 4320}.items():
        db.create_reaction_target(news_id, "MU", horizon, at + timedelta(minutes=minutes))


def test_integration_anomalous_target_predictions_are_observe_only(tmp_path: Path):
    path = tmp_path / "economic.db"
    db = AnalysisDatabase(path)
    _add_target_news(db)
    settings = _settings(path)
    service = AIAnalysisService(
        settings,
        analyzer=StaticAnalyzer(_target_analysis("$1500.00/share", "$1200.00/share")),
        db=db,
        worker_id="economic-worker",
    )

    assert service.run_once(1) == 1
    row = db.recent_analyses(1, settings.analysis_key)[0]
    predictions = db.prediction_rows(int(row["id"]))
    assert len(predictions) == 5
    assert {item["evaluation_status"] for item in predictions} == {"observe_only"}
    assert all(item["valid_for_directional_trading"] == 0 for item in predictions)
    assert all("economic plausibility" in (item["quality_reason"] or "") for item in predictions)
    summary = db.analysis_summary(settings.analysis_key)
    assert summary["economic_failed"] == 1
    assert summary["directional_predictions"] == 0
