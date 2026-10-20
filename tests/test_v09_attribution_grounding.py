from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from recorder.ai_provider import ProviderResult
from recorder.analysis_database import AnalysisDatabase
from recorder.analysis_models import HorizonPrediction, NewsAnalysis
from recorder.analysis_service import AIAnalysisService
from recorder.attribution import attribute_event_targets
from recorder.config import Settings
from recorder.models import NewsInput, QuoteSnapshot
from recorder.price_cache import PriceCache

UTC = timezone.utc
HORIZONS = ("15m", "1h", "close", "1d", "3d")


def _predictions(symbol: str) -> list[HorizonPrediction]:
    return [
        HorizonPrediction(
            symbol=symbol,
            horizon=h,
            expected_return=0.01,
            bear_return=-0.01,
            bull_return=0.03,
            up_probability=0.62,
            flat_probability=0.22,
            down_probability=0.16,
            confidence=0.72,
        )
        for h in HORIZONS
    ]


def test_blackberry_story_delivered_through_nvda_is_deterministic_mismatch():
    decision = attribute_event_targets(
        headline="Forget the Cellphones: BlackBerry Shares Jump 23% as an AI Play",
        body="BlackBerry shares surged after investors focused on its AI software business.",
        original_symbol="NVDA",
        available_symbols=("NVDA", "SPY", "QQQ"),
    )
    assert decision.subject_symbol == "BB"
    assert decision.attribution_status == "mismatch"
    assert decision.subject_target_available is False
    assert decision.primary_target_role == "unavailable_subject"
    assert decision.primary_targets == ()
    assert decision.primary_symbol == "NVDA"  # storage-compatible delivery symbol only


def test_applied_materials_story_delivered_through_avgo_is_mismatch():
    decision = attribute_event_targets(
        headline="Applied Materials Inc. Stock Rises 13.4%, Outperforms Peers",
        body="Applied Materials shares rose after the company update.",
        original_symbol="AVGO",
        available_symbols=("AVGO", "SPY", "QQQ"),
    )
    assert decision.subject_symbol == "AMAT"
    assert decision.attribution_status == "mismatch"
    assert decision.primary_targets == ()


def test_headline_body_mismatch_uses_body_subject_and_blocks_target():
    decision = attribute_event_targets(
        headline="Micron Is the New Nvidia, It Just Changed the AI -2-",
        body="Wendy's shares drew meme-stock interest. Wendy's also named a new CFO.",
        original_symbol="NVDA",
        available_symbols=("NVDA", "MU", "SPY"),
    )
    assert decision.subject_symbol == "WEN"
    assert decision.attribution_status == "mismatch"
    assert "headline subject" in decision.reason


def test_mu_delivered_through_soxx_still_targets_mu_when_baseline_available():
    decision = attribute_event_targets(
        headline="Micron reports earnings beat and raises guidance",
        body="Micron Technology reported revenue and EPS above expectations.",
        original_symbol="SOXX",
        available_symbols=("SOXX", "MU", "QQQ", "SPY"),
    )
    assert decision.subject_symbol == "MU"
    assert decision.primary_symbol == "MU"
    assert decision.attribution_status == "delivery_proxy"
    assert decision.subject_target_available is True


def test_generic_fed_inflation_context_is_not_fomc():
    decision = attribute_event_targets(
        headline="Stocks Finish Mixed After Key Federal Reserve Inflation Gauge",
        body="The inflation gauge matched expectations. No rate decision, policy statement, dot plot, or press conference occurred.",
        original_symbol="SPY",
        available_symbols=("SPY", "QQQ", "TLT"),
    )
    assert decision.event_scope == "macro"
    assert decision.subject_entity == "macro_market_context"
    assert decision.subject_entity != "fomc"


def test_specific_pce_and_fomc_evidence_are_distinguished():
    pce = attribute_event_targets(
        headline="Core PCE inflation matches estimates",
        body="The personal consumption expenditures price index was released at 8:30 a.m.",
        original_symbol="MU",
        available_symbols=("MU", "SPY", "QQQ", "TLT"),
    )
    fomc = attribute_event_targets(
        headline="FOMC rate decision and dot plot released",
        body="The Federal Reserve policy statement and Summary of Economic Projections were published.",
        original_symbol="SPY",
        available_symbols=("SPY", "QQQ", "TLT"),
    )
    assert pce.subject_entity == "pce"
    assert pce.primary_symbol == "SPY"
    assert fomc.subject_entity == "fomc"


class StaticAnalyzer:
    provider_name = "test"
    model_name = "test-model"

    def analyze(self, snapshot):
        analysis = NewsAnalysis(
            primary_symbol="NVDA",
            related_symbols=["NVDA"],
            event_type="product",
            event_subtype="AI software catalyst",
            event_stage="initial_release",
            sentiment="positive",
            causal_signal=True,
            prediction_eligible=True,
            reaction_observation_eligible=True,
            analysis_priority="normal",
            impact_score=0.55,
            novelty_score=0.60,
            source_quality=0.80,
            confidence=0.72,
            time_horizon="1_to_3_days",
            summary="A positive BlackBerry AI catalyst.",
            predictions=_predictions("NVDA"),
        )
        return ProviderResult(
            analysis=analysis,
            raw_json=analysis.model_dump_json(),
            response_id="test",
            input_tokens=1,
            output_tokens=1,
            provider_name="test",
            model_name="test-model",
            duration_ms=1,
        )


def test_service_suppresses_forecasts_and_reaction_calibration_for_mismatch(tmp_path: Path):
    db = AnalysisDatabase(tmp_path / "mismatch.db")
    at = datetime.now(UTC) - timedelta(seconds=5)
    headline = "Forget the Cellphones: BlackBerry Shares Jump 23% as an AI Play"
    news_id = db.insert_news(
        NewsInput("DJ-N", "bb-mismatch", headline, at, at, "NVDA", 1, ""),
        headline.lower(),
        "bb-mismatch-hash",
    )
    db.update_article_body(news_id, "BlackBerry shares jumped as investors reassessed its AI software business.")
    for symbol in ("NVDA", "SPY", "QQQ"):
        db.add_news_symbol(news_id, symbol, 1, at)
        db.insert_baseline(news_id, QuoteSnapshot(symbol, at, last=100.0, currency="USD"))
        for h, mins in {"15m": 15, "1h": 60, "close": 240, "1d": 1440, "3d": 4320}.items():
            db.create_reaction_target(news_id, symbol, h, at + timedelta(minutes=mins))

    settings = Settings(
        database_path=tmp_path / "mismatch.db",
        ai_provider="heuristic",
        ai_model="test",
        ai_enable_critical_model=False,
        ai_prompt_version="v09-mismatch-test",
        ai_use_article_body=True,
        ai_body_wait_seconds=0,
        ai_horizons=HORIZONS,
    )
    service = AIAnalysisService(settings, analyzer=StaticAnalyzer(), db=db, worker_id="w")
    assert service.run_once(1) == 1
    row = db.recent_analyses(1, settings.analysis_key)[0]
    assert row["attribution_status"] == "mismatch"
    assert row["event_subject_symbol"] == "BB"
    assert row["target_role"] == "unavailable_subject"
    assert row["prediction_eligible"] == 0
    assert row["reaction_observation_eligible"] == 0
    assert db.prediction_rows(int(row["id"])) == []


def test_ibkr_contract_currency_flows_through_price_cache():
    cache = PriceCache()
    cache.set_contract_metadata("NVDA", currency="USD")
    cache.update_price("NVDA", "last", 200.0)
    quote = cache.get("NVDA")
    assert quote is not None
    assert quote.currency == "USD"


def test_valid_integrity_is_reported_as_not_needed_not_generic_not_attempted(tmp_path: Path):
    from recorder.text_integrity import IntegrityResult

    settings = Settings(
        database_path=tmp_path / "state.db",
        ai_provider="heuristic",
        ai_model="test",
        ai_enable_critical_model=False,
    )
    service = AIAnalysisService(settings, analyzer=StaticAnalyzer(), worker_id="state-worker")
    analysis = NewsAnalysis(
        primary_symbol="NVDA",
        related_symbols=["NVDA"],
        event_type="product",
        event_subtype="validated event",
        event_stage="initial_release",
        sentiment="positive",
        causal_signal=True,
        prediction_eligible=True,
        analysis_priority="normal",
        impact_score=0.5,
        novelty_score=0.5,
        source_quality=0.8,
        confidence=0.7,
        time_horizon="1_to_3_days",
        summary="Validated event.",
        predictions=_predictions("NVDA"),
    )
    integrity = IntegrityResult("valid", (), 2, 0, 1, 0, 0.0)
    decision = service._integrity_recovery_decision(
        analysis,
        integrity,
        raw_fact_count=2,
        raw_high_importance_count=1,
    )
    assert decision.attempt is False
    assert decision.status == "not_needed_integrity_valid"


def test_cluster_member_roles_distinguish_primary_release_and_market_recap():
    from recorder.event_clusters import cluster_member_role

    primary = NewsAnalysis(
        primary_symbol="SPY",
        related_symbols=["SPY"],
        event_scope="macro",
        subject_entity="pce",
        subject_asset_type="macro_event",
        target_asset_type="etf",
        target_role="market_proxy",
        target_assets=["SPY"],
        event_type="macro",
        event_subtype="PCE release",
        event_stage="initial_release",
        sentiment="neutral",
        causal_signal=True,
        prediction_eligible=False,
        analysis_priority="high",
        impact_score=0.7,
        novelty_score=0.8,
        source_quality=0.98,
        source_tier="official_primary",
        official_source_status="attached",
        confidence=0.9,
        time_horizon="intraday",
        summary="Official PCE release.",
    )
    recap = primary.model_copy(deep=True)
    recap.event_type = "market_price_update"
    recap.event_stage = "retrospective"
    recap.official_source_status = "not_available"
    assert cluster_member_role(primary) == "primary_release"
    assert cluster_member_role(recap) == "market_recap"


def test_baseline_contract_currency_is_persisted_and_returned(tmp_path: Path):
    db = AnalysisDatabase(tmp_path / "currency.db")
    at = datetime.now(UTC)
    headline = "Nvidia product update"
    news_id = db.insert_news(
        NewsInput("DJ-N", "currency", headline, at, at, "NVDA", 1, ""),
        headline.lower(),
        "currency-hash",
    )
    db.add_news_symbol(news_id, "NVDA", 1, at)
    db.insert_baseline(news_id, QuoteSnapshot("NVDA", at, last=200.0, currency="USD"))
    _, baselines = db.get_news_context(news_id)
    assert baselines[0]["currency"] == "USD"
