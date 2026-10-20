from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from recorder.ai_provider import (
    FailingAnalyzer,
    HeuristicNewsAnalyzer,
    OpenAINewsAnalyzer,
)
from recorder.analysis_database import AnalysisDatabase
from recorder.analysis_models import HorizonPrediction, NewsAnalysis
from recorder.analysis_service import AIAnalysisService
from recorder.config import Settings
from recorder.database import Database
from recorder.models import NewsInput, QuoteSnapshot

UTC = timezone.utc


def add_news(
    db: Database,
    *,
    headline: str,
    symbol: str = "MU",
    received_at: datetime | None = None,
    article_id: str = "A1",
) -> int:
    at = received_at or (datetime.now(UTC) - timedelta(seconds=30))
    news_id = db.insert_news(
        NewsInput(
            provider="DJ-N",
            article_id=article_id,
            headline=headline,
            published_at=at,
            received_at=at,
            primary_symbol=symbol,
            con_id=123,
            extra_data="",
        ),
        normalized_headline=headline.lower(),
        headline_hash=f"hash-{article_id}",
    )
    db.add_news_symbol(news_id, symbol, 123, at)
    db.insert_baseline(
        news_id,
        QuoteSnapshot(
            symbol=symbol,
            timestamp=at,
            bid=99.9,
            ask=100.1,
            last=100.0,
            close=98.0,
            volume=1_000_000,
        ),
    )
    for horizon, minutes in {
        "15m": 15,
        "1h": 60,
        "close": 240,
        "1d": 1440,
        "3d": 4320,
    }.items():
        db.create_reaction_target(news_id, symbol, horizon, at + timedelta(minutes=minutes))
    return news_id


def heuristic_settings(path: Path, **changes) -> Settings:
    base = Settings(
        database_path=path,
        ai_provider="heuristic",
        ai_model="heuristic-v1",
        ai_prompt_version="news-agent-v1",
        ai_body_wait_seconds=0,
        ai_poll_seconds=1,
        ai_batch_size=10,
        ai_backfill_max_age_days=30,
        ai_backfill_limit=1000,
        ai_retry_base_seconds=1,
        ai_retry_max_seconds=2,
        ai_max_attempts=3,
    )
    return replace(base, **changes)


def test_schema_migrates_existing_v01_database_without_data_loss(tmp_path: Path):
    path = tmp_path / "market_events.db"
    old_db = Database(path)
    news_id = add_news(old_db, headline="Micron raises guidance")
    assert old_db.summary()["news"] == 1

    upgraded = AnalysisDatabase(path)
    assert upgraded.summary()["news"] == 1
    assert upgraded.analysis_summary()["analyses"] == 0
    with upgraded.connect() as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert {"analysis_jobs", "news_analyses", "predictions", "prediction_inputs"} <= tables
    assert upgraded.get_news_by_identity("DJ-N", "A1")["id"] == news_id


def test_heuristic_service_creates_immutable_analysis_and_predictions(tmp_path: Path):
    path = tmp_path / "market_events.db"
    db = AnalysisDatabase(path)
    add_news(db, headline="Micron raises quarterly revenue outlook on AI demand")
    settings = heuristic_settings(path)
    service = AIAnalysisService(settings, analyzer=HeuristicNewsAnalyzer(), db=db, worker_id="w1")

    assert service.run_once(limit=10) == 1
    summary = db.analysis_summary(settings.analysis_key)
    assert summary["jobs_complete"] == 1
    assert summary["analyses"] == 1
    assert summary["predictions"] == 5

    # Running again must not create a duplicate analysis or prediction.
    assert service.run_once(limit=10) == 0
    second = db.analysis_summary(settings.analysis_key)
    assert second["analyses"] == 1
    assert second["predictions"] == 5


def test_ai_snapshot_includes_event_time_safe_source_desk_context(tmp_path: Path):
    path = tmp_path / "market_events.db"
    db = AnalysisDatabase(path)
    news_time = datetime(2026, 6, 1, 13, 30, tzinfo=UTC)
    add_news(
        db,
        headline="Micron raises quarterly revenue outlook on AI demand",
        received_at=news_time,
        article_id="source-desk-context",
    )
    db.upsert_financial_statement_fact(
        SimpleNamespace(
            symbol="MU",
            cik="723125",
            taxonomy="us-gaap",
            tag="Revenues",
            label="Revenue",
            unit="USD",
            fiscal_year=2026,
            fiscal_period="Q2",
            form="10-Q",
            filed_at="2026-05-15",
            start_date="2026-03-01",
            end_date="2026-05-01",
            frame="",
            value=8_000_000_000,
            source_url="https://www.sec.gov/example",
            accession_number="past",
            source_hash="past",
            ingestion_version="fixture",
        )
    )
    db.upsert_financial_statement_fact(
        SimpleNamespace(
            symbol="MU",
            cik="723125",
            taxonomy="us-gaap",
            tag="NetIncomeLoss",
            label="Net Income",
            unit="USD",
            fiscal_year=2026,
            fiscal_period="Q3",
            form="10-Q",
            filed_at="2026-06-15",
            start_date="2026-05-02",
            end_date="2026-06-01",
            frame="",
            value=2_000_000_000,
            source_url="https://www.sec.gov/future",
            accession_number="future",
            source_hash="future",
            ingestion_version="fixture",
        )
    )
    db.upsert_fred_observation(
        SimpleNamespace(
            series_id="DGS10",
            observation_date="2026-05-29",
            value=4.25,
            realtime_start="2026-05-29",
            realtime_end="2026-05-29",
            source_url="https://fred.example/DGS10",
            source_hash="fred-past",
            ingestion_version="fixture",
        )
    )
    db.upsert_fred_observation(
        SimpleNamespace(
            series_id="DGS10",
            observation_date="2026-06-05",
            value=4.90,
            realtime_start="2026-06-05",
            realtime_end="2026-06-05",
            source_url="https://fred.example/DGS10",
            source_hash="fred-future",
            ingestion_version="fixture",
        )
    )
    db.upsert_technical_indicator(
        SimpleNamespace(
            symbol="MU",
            as_of_date="2026-05-29",
            price_source="ibkr",
            close=100.0,
            return_1d=0.01,
            return_5d=0.03,
            return_20d=0.08,
            sma_20=96.0,
            sma_50=92.0,
            ema_12=98.0,
            ema_26=95.0,
            macd=1.5,
            macd_signal=1.0,
            rsi_14=62.0,
            volatility_20=0.35,
            source_start_date="2026-04-01",
            source_end_date="2026-05-29",
            ingestion_version="fixture",
        )
    )
    db.upsert_external_market_signal(
        SimpleNamespace(
            source="marketaux",
            external_id="pre-event-ai-memory",
            title="Memory chip stocks rise as AI server demand improves",
            url="https://example.test/ai-memory",
            published_at="2026-06-01T12:30:00+00:00",
            region="global",
            theme="semiconductor_supply_chain",
            sentiment_score=0.44,
            sentiment_label="positive",
            confidence=0.72,
            assets=("MU", "SOXX", "QQQ"),
            asset_map_reason="fixture external signal",
            market_reaction_status="available",
            market_reaction={"technical_indicators": []},
            prediction_gate_status="eligible",
            prediction_gate_reason="fixture positive sentiment cross-check",
            raw={"fixture": True},
            ingestion_version="fixture",
        )
    )
    db.upsert_external_market_signal(
        SimpleNamespace(
            source="alpha_vantage",
            external_id="future-event-ai-memory",
            title="Future semiconductor headline should not leak",
            url="https://example.test/future",
            published_at="2026-06-02T12:30:00+00:00",
            region="global",
            theme="semiconductor_supply_chain",
            sentiment_score=-0.80,
            sentiment_label="negative",
            confidence=0.80,
            assets=("MU", "SOXX"),
            asset_map_reason="future fixture external signal",
            market_reaction_status="available",
            market_reaction={"technical_indicators": []},
            prediction_gate_status="eligible",
            prediction_gate_reason="future fixture",
            raw={"fixture": "future"},
            ingestion_version="fixture",
        )
    )
    db.upsert_technical_indicator(
        SimpleNamespace(
            symbol="MU",
            as_of_date="2026-06-10",
            price_source="ibkr",
            close=140.0,
            return_1d=0.25,
            return_5d=0.40,
            return_20d=0.80,
            sma_20=120.0,
            sma_50=100.0,
            ema_12=130.0,
            ema_26=115.0,
            macd=10.0,
            macd_signal=5.0,
            rsi_14=88.0,
            volatility_20=0.90,
            source_start_date="2026-04-01",
            source_end_date="2026-06-10",
            ingestion_version="fixture",
        )
    )

    settings = heuristic_settings(path)
    service = AIAnalysisService(settings, analyzer=HeuristicNewsAnalyzer(), db=db, worker_id="source-desk")
    db.enqueue_jobs(
        settings.analysis_key,
        settings.ai_provider,
        settings.ai_model,
        settings.ai_prompt_version,
        30,
        100,
        now=news_time + timedelta(minutes=1),
    )
    job = db.claim_next_job("source-desk", settings.analysis_key, 30, 0, now=news_time + timedelta(minutes=1))
    assert job is not None

    snapshot, *_ = service._build_snapshot(job)
    context = snapshot["source_desk_context"]

    assert context["context_version"] == "source-desk-context-v2"
    assert context["market_data_desk"]["target_quote_available"] is True
    assert context["financial_statements_desk"]["facts"][0]["accession_number"] == "past"
    assert {row["accession_number"] for row in context["financial_statements_desk"]["facts"]} == {"past"}
    assert context["fred_macro_desk"]["observations"][0]["observation_date"] == "2026-05-29"
    assert context["economic_indicator_desk"]["covered_series_count"] >= 1
    assert context["economic_indicator_desk"]["indicators"][0]["latest_date"] == "2026-05-29"
    assert "2026-06-05" not in json.dumps(context["economic_indicator_desk"])
    assert context["technical_indicators_desk"]["indicators"][0]["as_of_date"] == "2026-05-29"
    assert context["external_geopolitical_news_desk"]["signal_count"] == 1
    assert context["external_geopolitical_news_desk"]["signals"][0]["source"] == "marketaux"
    assert "MU" in context["external_geopolitical_news_desk"]["signals"][0]["asset_overlap"]
    assert "Future semiconductor headline" not in json.dumps(context["external_geopolitical_news_desk"])
    assert "event-time cutoff" in context["anti_leakage_note"]


def test_market_move_headline_is_noncausal_and_has_no_prediction(tmp_path: Path):
    path = tmp_path / "market_events.db"
    db = AnalysisDatabase(path)
    add_news(db, headline="QQQ ETF Falls 3.3%", symbol="QQQ")
    settings = heuristic_settings(path)
    service = AIAnalysisService(settings, analyzer=HeuristicNewsAnalyzer(), db=db, worker_id="w1")

    service.run_once(limit=10)
    rows = db.recent_analyses(10, settings.analysis_key)
    assert len(rows) == 1
    assert rows[0]["event_type"] == "market_price_update"
    assert rows[0]["prediction_eligible"] == 0
    assert rows[0]["prediction_count"] == 0


def test_transient_failure_is_retried_without_crashing(tmp_path: Path):
    path = tmp_path / "market_events.db"
    db = AnalysisDatabase(path)
    add_news(db, headline="Micron raises outlook")
    settings = heuristic_settings(path, ai_retry_base_seconds=1, ai_retry_max_seconds=1)
    analyzer = FailingAnalyzer(failures=1)
    service = AIAnalysisService(settings, analyzer=analyzer, db=db, worker_id="w1")

    assert service.run_once(limit=1) == 1
    first = db.analysis_summary(settings.analysis_key)
    assert first["jobs_retry"] == 1
    assert first["analyses"] == 0

    with db.connect() as conn:
        conn.execute(
            "UPDATE analysis_jobs SET next_attempt_at=? WHERE analysis_key=?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), settings.analysis_key),
        )
    assert service.run_once(limit=1) == 1
    second = db.analysis_summary(settings.analysis_key)
    assert second["jobs_complete"] == 1
    assert second["analyses"] == 1


def test_stale_processing_job_is_recovered(tmp_path: Path):
    path = tmp_path / "market_events.db"
    db = AnalysisDatabase(path)
    add_news(db, headline="Micron raises outlook")
    settings = heuristic_settings(path, ai_job_stale_seconds=30)
    db.enqueue_jobs(
        settings.analysis_key,
        settings.ai_provider,
        settings.ai_model,
        settings.ai_prompt_version,
        30,
        100,
    )
    first = db.claim_next_job("dead-worker", settings.analysis_key, 30, 0)
    assert first is not None
    with db.connect() as conn:
        conn.execute(
            "UPDATE analysis_jobs SET locked_at=? WHERE id=?",
            ((datetime.now(UTC) - timedelta(minutes=10)).isoformat(), first.job_id),
        )
    recovered = db.claim_next_job("new-worker", settings.analysis_key, 30, 0)
    assert recovered is not None
    assert recovered.job_id == first.job_id
    events = [row["event_type"] for row in db.job_events(first.job_id)]
    assert "lease_recovered" in events


def test_two_workers_cannot_claim_same_job(tmp_path: Path):
    path = tmp_path / "market_events.db"
    db = AnalysisDatabase(path)
    add_news(db, headline="Micron raises outlook")
    settings = heuristic_settings(path)
    db.enqueue_jobs(
        settings.analysis_key,
        settings.ai_provider,
        settings.ai_model,
        settings.ai_prompt_version,
        30,
        100,
    )
    results = []
    lock = threading.Lock()

    def claim(worker: str):
        result = db.claim_next_job(worker, settings.analysis_key, 300, 0)
        with lock:
            results.append(result)

    threads = [threading.Thread(target=claim, args=(f"w{i}",)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum(result is not None for result in results) == 1


def test_predictions_created_after_horizon_are_marked_invalid(tmp_path: Path):
    path = tmp_path / "market_events.db"
    db = AnalysisDatabase(path)
    old = datetime.now(UTC) - timedelta(hours=2)
    add_news(db, headline="Micron raises outlook", received_at=old)
    settings = heuristic_settings(path)
    service = AIAnalysisService(settings, analyzer=HeuristicNewsAnalyzer(), db=db, worker_id="w1")
    service.run_once(limit=1)

    analysis_id = int(db.recent_analyses(1, settings.analysis_key)[0]["id"])
    predictions = {row["horizon"]: row for row in db.prediction_rows(analysis_id)}
    assert predictions["15m"]["is_valid"] == 0
    assert predictions["1h"]["is_valid"] == 0
    assert predictions["1d"]["evaluation_status"] != "invalid"


def sample_analysis() -> NewsAnalysis:
    horizons = ("15m", "1h", "close", "1d", "3d")
    return NewsAnalysis(
        primary_symbol="MU",
        related_symbols=["MU", "SOXX"],
        event_type="guidance",
        event_subtype="guidance_raise",
        sentiment="positive",
        causal_signal=True,
        prediction_eligible=True,
        analysis_priority="high",
        impact_score=0.8,
        novelty_score=0.7,
        source_quality=0.9,
        confidence=0.7,
        time_horizon="1_to_3_days",
        summary="Guidance was raised.",
        bull_case=["Higher outlook"],
        bear_case=["Pre-positioning risk"],
        data_limitations=[],
        predictions=[
            HorizonPrediction(
                symbol="MU",
                horizon=h,
                expected_return=0.02,
                bear_return=-0.02,
                bull_return=0.06,
                up_probability=0.65,
                flat_probability=0.15,
                down_probability=0.20,
                confidence=0.7,
            )
            for h in horizons
        ],
    )


def test_openai_structured_response_extraction_with_mock_client():
    analysis = sample_analysis()
    content = SimpleNamespace(type="output_text", parsed=analysis)
    response = SimpleNamespace(
        id="resp_test",
        output=[SimpleNamespace(type="message", content=[content])],
        usage=SimpleNamespace(input_tokens=100, output_tokens=200),
    )

    class FakeResponses:
        def parse(self, **kwargs):
            assert kwargs["text_format"] is NewsAnalysis
            assert kwargs["store"] is False
            return response

    fake_client = SimpleNamespace(responses=FakeResponses())
    analyzer = OpenAINewsAnalyzer(
        api_key="test",
        model="gpt-test",
        timeout_seconds=30,
        client=fake_client,
    )
    result = analyzer.analyze({"headline": "test", "primary_symbol": "MU"})
    assert result.analysis.event_type == "guidance"
    assert result.response_id == "resp_test"
    assert result.input_tokens == 100
    assert result.output_tokens == 200


def test_probability_rounding_is_normalized():
    prediction = HorizonPrediction(
        symbol="MU",
        horizon="1d",
        expected_return=0.01,
        bear_return=-0.02,
        bull_return=0.04,
        up_probability=0.333,
        flat_probability=0.333,
        down_probability=0.333,
        confidence=0.5,
    )
    assert prediction.up_probability + prediction.flat_probability + prediction.down_probability == pytest.approx(1.0)


def test_price_move_with_explicit_catalyst_is_not_prefiltered():
    from recorder.prompting import is_obvious_market_price_update

    assert is_obvious_market_price_update("QQQ ETF Falls 3.3%") is True
    assert (
        is_obvious_market_price_update("Stock Falls 8% After Company Cuts Guidance")
        is False
    )


def test_unexpected_analyzer_exception_is_persisted_not_raised(tmp_path: Path):
    path = tmp_path / "market_events.db"
    db = AnalysisDatabase(path)
    add_news(db, headline="Micron raises outlook")
    settings = heuristic_settings(path)

    class CrashAnalyzer:
        provider_name = "test"
        model_name = "crash"

        def analyze(self, snapshot):
            raise RuntimeError("simulated unexpected crash")

    service = AIAnalysisService(settings, analyzer=CrashAnalyzer(), db=db, worker_id="w1")
    assert service.run_once(limit=1) == 1
    summary = db.analysis_summary(settings.analysis_key)
    assert summary["jobs_retry"] == 1
    assert summary["analyses"] == 0


def test_repeated_permanent_failure_moves_job_to_dead_queue(tmp_path: Path):
    path = tmp_path / "market_events.db"
    db = AnalysisDatabase(path)
    add_news(db, headline="Micron raises outlook")
    settings = heuristic_settings(path, ai_max_attempts=5)
    service = AIAnalysisService(
        settings,
        analyzer=FailingAnalyzer(failures=10, permanent=True),
        db=db,
        worker_id="w1",
    )
    service.run_once(limit=1)
    with db.connect() as conn:
        conn.execute(
            "UPDATE analysis_jobs SET next_attempt_at=? WHERE analysis_key=?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), settings.analysis_key),
        )
    service.run_once(limit=1)
    summary = db.analysis_summary(settings.analysis_key)
    assert summary["jobs_dead"] == 1
    assert summary["jobs_retry"] == 0
