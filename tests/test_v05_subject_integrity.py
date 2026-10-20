from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from recorder.ai_provider import ProviderResult
from recorder.analysis_database import AnalysisDatabase
from recorder.analysis_models import EventFact, HorizonPrediction, NewsAnalysis
from recorder.analysis_service import AIAnalysisService
from recorder.attribution import attribute_event_targets
from recorder.config import Settings
from recorder.database import Database
from recorder.models import NewsInput, QuoteSnapshot

UTC = timezone.utc
HORIZONS = ("15m", "1h", "close", "1d", "3d")


def _settings(path: Path, **changes) -> Settings:
    values = dict(
        database_path=path,
        ai_provider="heuristic",
        ai_model="test",
        ai_prompt_version="v05-test",
        ai_body_wait_seconds=0,
        ai_backfill_limit=100,
        ai_backfill_max_age_days=30,
        ai_horizons=HORIZONS,
    )
    values.update(changes)
    return Settings(**values)


def _add_news(
    db: Database,
    *,
    headline: str,
    body: str,
    symbol: str,
    article_id: str,
    symbols: tuple[str, ...],
) -> int:
    at = datetime.now(UTC) - timedelta(seconds=10)
    news_id = db.insert_news(
        NewsInput("DJ-N", article_id, headline, at, at, symbol, 1, ""),
        headline.lower(),
        f"hash-{article_id}",
    )
    db.update_article_body(news_id, body)
    for tracked in symbols:
        db.add_news_symbol(news_id, tracked, 1, at)
        db.insert_baseline(news_id, QuoteSnapshot(tracked, at, last=100.0))
        for horizon, minutes in {"15m": 15, "1h": 60, "close": 240, "1d": 1440, "3d": 4320}.items():
            db.create_reaction_target(news_id, tracked, horizon, at + timedelta(minutes=minutes))
    return news_id


class FixedAnalyzer:
    provider_name = "test"
    model_name = "test-model"

    def __init__(self, analysis: NewsAnalysis):
        self.analysis = analysis

    def analyze(self, snapshot):
        clone = self.analysis.model_copy(deep=True)
        return ProviderResult(
            analysis=clone,
            raw_json=clone.model_dump_json(),
            response_id="test",
            input_tokens=1,
            output_tokens=1,
            provider_name="test",
            model_name="test-model",
            duration_ms=1,
        )


def _predictions(symbol: str, *, up: float = 0.62, flat: float = 0.20, down: float = 0.18):
    return [
        HorizonPrediction(
            symbol=symbol,
            horizon=h,
            expected_return=0.01,
            bear_return=-0.01,
            bull_return=0.03,
            up_probability=up,
            flat_probability=flat,
            down_probability=down,
            confidence=0.75,
        )
        for h in HORIZONS
    ]


def test_mu_event_delivered_through_soxx_keeps_mu_as_subject_and_target():
    decision = attribute_event_targets(
        headline="Micron fiscal Q3 earnings beat and guidance rises",
        body="Micron Technology reported revenue and EPS above expectations.",
        original_symbol="SOXX",
        available_symbols=("SOXX", "MU", "QQQ", "SPY"),
    )
    assert decision.event_scope == "company"
    assert decision.subject_symbol == "MU"
    assert decision.primary_symbol == "MU"
    assert decision.primary_target_role == "subject_asset"
    assert "SOXX" in decision.secondary_targets


def test_spy_and_qqq_are_market_etfs_not_companies_or_sectors():
    spy = attribute_event_targets(
        headline="SPY ETF Declines 1.2%",
        body="Broad market ETF price update.",
        original_symbol="SPY",
        available_symbols=("SPY", "QQQ"),
    )
    qqq = attribute_event_targets(
        headline="QQQ ETF Declines 0.4%",
        body="Technology shares moved in premarket trading.",
        original_symbol="QQQ",
        available_symbols=("QQQ", "SPY"),
    )
    assert (spy.event_scope, spy.subject_entity, spy.subject_asset_type) == ("market", "us_equities", "etf")
    assert (qqq.event_scope, qqq.subject_entity, qqq.subject_asset_type) == ("market", "nasdaq_100", "etf")


def test_routine_form144_without_materiality_produces_no_forecast(tmp_path: Path):
    path = tmp_path / "form144.db"
    db = AnalysisDatabase(path)
    _add_news(
        db,
        headline="Holder D95GT LLC Registers 340,208 Of Broadcom Inc",
        body="Form 144 permits the holder to sell up to 340,208 shares within 90 days. No completed sale is reported.",
        symbol="AVGO",
        article_id="f144",
        symbols=("AVGO", "SPY", "QQQ"),
    )
    analysis = NewsAnalysis(
        primary_symbol="AVGO",
        related_symbols=["AVGO"],
        event_type="securities_filing",
        event_subtype="Form 144 holder notice to sell restricted shares",
        event_stage="regulatory_filing",
        sentiment="neutral",
        causal_signal=True,
        prediction_eligible=True,
        analysis_priority="normal",
        impact_score=0.18,
        novelty_score=0.5,
        source_quality=0.8,
        confidence=0.7,
        time_horizon="1_to_3_days",
        summary="A Form 144 permits a potential sale, but execution is not confirmed.",
        key_facts=[EventFact(name="Shares registered", actual="340,208")],
        predictions=_predictions("AVGO", up=0.25, flat=0.55, down=0.20),
    )
    service = AIAnalysisService(_settings(path), analyzer=FixedAnalyzer(analysis), db=db, worker_id="w")
    assert service.run_once(1) == 1
    row = db.recent_analyses(1, _settings(path).analysis_key)[0]
    assert row["prediction_eligible"] == 0
    assert row["reaction_observation_eligible"] == 1
    assert row["prediction_count"] == 0


def test_flat_dominant_prediction_is_calibration_only(tmp_path: Path):
    path = tmp_path / "flat.db"
    db = AnalysisDatabase(path)
    _add_news(
        db,
        headline="Broadcom routine shareholder update",
        body="Broadcom issued a routine shareholder update with no change to guidance.",
        symbol="AVGO",
        article_id="flat",
        symbols=("AVGO", "SPY"),
    )
    analysis = NewsAnalysis(
        primary_symbol="AVGO",
        related_symbols=["AVGO"],
        event_type="other",
        event_subtype="routine shareholder update",
        event_stage="initial_release",
        sentiment="neutral",
        causal_signal=True,
        prediction_eligible=True,
        analysis_priority="normal",
        impact_score=0.3,
        novelty_score=0.5,
        source_quality=0.8,
        confidence=0.75,
        time_horizon="intraday",
        summary="A routine update is expected to have little directional effect.",
        predictions=_predictions("AVGO", up=0.20, flat=0.60, down=0.20),
    )
    settings = _settings(path)
    service = AIAnalysisService(settings, analyzer=FixedAnalyzer(analysis), db=db, worker_id="w")
    service.run_once(1)
    row = db.recent_analyses(1, settings.analysis_key)[0]
    predictions = db.prediction_rows(int(row["id"]))
    assert {p["evaluation_status"] for p in predictions} == {"calibration_only"}
    assert all(p["valid_for_directional_trading"] == 0 for p in predictions)
    assert all(p["valid_for_probability_calibration"] == 1 for p in predictions)


def test_text_integrity_removes_encoding_artifact_and_rejects_ungrounded_fact(tmp_path: Path):
    path = tmp_path / "integrity.db"
    db = AnalysisDatabase(path)
    _add_news(
        db,
        headline="Helium supply disruption did not stop chip production",
        body="Qatar shipments stalled after a shipping route closed. Chip production continued without significant disruption.",
        symbol="NVDA",
        article_id="integrity",
        symbols=("NVDA", "SPY"),
    )
    analysis = NewsAnalysis(
        primary_symbol="NVDA",
        related_symbols=["NVDA"],
        event_type="supply_chain",
        event_subtype="helium disruption resilience",
        event_stage="follow_up",
        sentiment="neutral",
        causal_signal=False,
        prediction_eligible=False,
        analysis_priority="normal",
        impact_score=0.2,
        novelty_score=0.4,
        source_quality=0.8,
        confidence=0.7,
        time_horizon="1_to_3_days",
        summary="Shipments stalled after a航 shipping route closed,, while production continued.",
        key_facts=[
            EventFact(name="Production", actual="continued without significant disruption"),
            EventFact(name="Invented price", actual="$999.99"),
        ],
    )
    settings = _settings(path, ai_use_article_body=True)
    service = AIAnalysisService(settings, analyzer=FixedAnalyzer(analysis), db=db, worker_id="w")
    service.run_once(1)
    row = db.recent_analyses(1, settings.analysis_key)[0]
    raw = __import__("json").loads(row["raw_json"])
    assert "航" not in row["summary"]
    assert ",," not in row["summary"]
    assert row["text_integrity_status"] == "warning"
    assert row["validated_fact_count"] == 1
    assert row["rejected_fact_count"] == 1
    assert len(raw["key_facts"]) == 1


def test_macro_without_official_source_is_calibration_only(tmp_path: Path):
    path = tmp_path / "macro-official.db"
    db = AnalysisDatabase(path)
    _add_news(
        db,
        headline="May CPI rises 0.3%, matching expectations",
        body="The consumer price index rose 0.3% in May, matching the 0.3% expectation. Core CPI rose 0.2%.",
        symbol="MU",
        article_id="cpi",
        symbols=("MU", "SPY", "QQQ", "TLT"),
    )
    analysis = NewsAnalysis(
        primary_symbol="SPY",
        related_symbols=["SPY", "QQQ", "TLT"],
        event_scope="macro",
        subject_entity="cpi",
        event_type="cpi",
        event_subtype="May CPI release",
        event_stage="initial_release",
        sentiment="neutral",
        causal_signal=True,
        prediction_eligible=True,
        analysis_priority="high",
        impact_score=0.8,
        novelty_score=0.9,
        source_quality=0.85,
        confidence=0.8,
        time_horizon="intraday",
        summary="CPI matched expectations.",
        key_facts=[
            EventFact(name="Headline CPI", actual="0.3%", expected="0.3%"),
            EventFact(name="Core CPI", actual="0.2%"),
        ],
        predictions=_predictions("SPY"),
    )
    settings = _settings(path, ai_use_article_body=True)
    service = AIAnalysisService(settings, analyzer=FixedAnalyzer(analysis), db=db, worker_id="w")
    service.run_once(1)
    row = db.recent_analyses(1, settings.analysis_key)[0]
    predictions = db.prediction_rows(int(row["id"]))
    assert {p["evaluation_status"] for p in predictions} == {"calibration_only"}
    assert all(p["valid_for_directional_trading"] == 0 for p in predictions)
    assert all(p["valid_for_probability_calibration"] == 1 for p in predictions)


def test_manual_official_document_can_trigger_immutable_reanalysis(tmp_path: Path):
    from dataclasses import replace

    path = tmp_path / "official-reanalysis.db"
    db = AnalysisDatabase(path)
    news_id = _add_news(
        db,
        headline="Company reports quarterly results",
        body="A secondary report says quarterly results were released.",
        symbol="MU",
        article_id="official-reanalysis",
        symbols=("MU", "SPY", "QQQ"),
    )
    official_text = "Official earnings release: revenue was $10.0 billion versus $9.0 billion expected."
    db.add_manual_official_document(
        news_id,
        "company_ir",
        "https://investors.example.com/results",
        "Official results",
        official_text,
    )
    analysis = NewsAnalysis(
        primary_symbol="MU",
        related_symbols=["MU"],
        event_type="earnings",
        event_subtype="quarterly earnings release",
        event_stage="initial_release",
        sentiment="positive",
        causal_signal=True,
        prediction_eligible=True,
        analysis_priority="high",
        impact_score=0.8,
        novelty_score=0.9,
        source_quality=0.9,
        confidence=0.8,
        time_horizon="1_to_3_days",
        summary="Official results show revenue above expectations.",
        key_facts=[EventFact(name="Revenue", actual="$10.0 billion", expected="$9.0 billion")],
        predictions=_predictions("MU"),
    )
    base = _settings(path)
    revised = replace(base, ai_prompt_version="v05-test-official-abc")
    db.enqueue_news_job(
        news_id,
        revised.analysis_key,
        revised.ai_provider,
        revised.ai_model,
        revised.ai_prompt_version,
        priority=1000,
    )
    service = AIAnalysisService(revised, analyzer=FixedAnalyzer(analysis), db=db, worker_id="official-worker")
    assert service.process_batch(1) == 1
    row = db.recent_analyses(1, revised.analysis_key)[0]
    assert row["source_tier"] == "official_primary"
    assert row["official_source_status"] == "attached"
    assert row["text_integrity_status"] == "valid"
