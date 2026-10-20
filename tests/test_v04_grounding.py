from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from recorder.analysis_database import AnalysisDatabase
from recorder.analysis_models import HorizonPrediction, NewsAnalysis
from recorder.analysis_service import AIAnalysisService
from recorder.ai_provider import ProviderResult
from recorder.calendar_utils import due_time, evaluation_label
from recorder.config import Settings
from recorder.database import Database
from recorder.models import NewsInput, QuoteSnapshot

UTC = timezone.utc
HORIZONS = ("15m", "1h", "close", "1d", "3d")


class SnapshotAnalyzer:
    provider_name = "test"
    model_name = "test-model"

    def __init__(self, *, event_type: str = "other", stage: str = "initial_release", confidence: float = 0.75):
        self.event_type = event_type
        self.stage = stage
        self.confidence = confidence
        self.seen = []

    def analyze(self, snapshot):
        self.seen.append(snapshot)
        symbol = snapshot["attributed_primary_symbol"]
        eligible = self.stage != "retrospective"
        analysis = NewsAnalysis(
            primary_symbol=symbol,
            related_symbols=[symbol],
            event_scope=snapshot["event_scope_hint"],
            subject_entity=snapshot["subject_entity_hint"],
            event_type=self.event_type,
            event_subtype="test_event",
            event_stage=self.stage,
            sentiment="positive",
            causal_signal=True,
            prediction_eligible=eligible,
            analysis_priority="high",
            impact_score=0.8,
            novelty_score=0.8,
            source_quality=0.8,
            confidence=self.confidence,
            time_horizon="1_to_3_days",
            summary="Structured test event.",
            predictions=[
                HorizonPrediction(
                    symbol=symbol,
                    horizon=h,
                    expected_return=0.01,
                    bear_return=-0.01,
                    bull_return=0.03,
                    up_probability=0.60,
                    flat_probability=0.20,
                    down_probability=0.20,
                    confidence=self.confidence,
                )
                for h in HORIZONS
            ] if eligible else [],
        )
        return ProviderResult(analysis=analysis, raw_json=analysis.model_dump_json(), response_id=None, input_tokens=None, output_tokens=None, provider_name="test", model_name="test-model", duration_ms=1)


def settings(path: Path) -> Settings:
    return Settings(
        database_path=path,
        ai_provider="heuristic",
        ai_model="test-model",
        ai_prompt_version="v04-test",
        ai_body_wait_seconds=0,
        ai_backfill_max_age_days=30,
        ai_backfill_limit=100,
        ai_horizons=HORIZONS,
        prediction_min_analysis_confidence=0.45,
        prediction_min_confidence=0.45,
    )


def add_news(db: Database, headline: str, symbol: str, article_id: str, at: datetime, body: str = "") -> int:
    news_id = db.insert_news(
        NewsInput("DJ-N", article_id, headline, at, at, symbol, 1, ""),
        headline.lower(), f"hash-{article_id}"
    )
    if body:
        db.update_article_body(news_id, body)
    for tracked in (symbol, "SPY", "QQQ", "SOXX"):
        db.add_news_symbol(news_id, tracked, 1, at)
        db.insert_baseline(news_id, QuoteSnapshot(tracked, at, last=100.0))
        for horizon, minutes in {"15m": 15, "1h": 60, "close": 240, "1d": 1440, "3d": 4320}.items():
            db.create_reaction_target(news_id, tracked, horizon, at + timedelta(minutes=minutes))
    return news_id


def test_macro_article_received_under_mu_is_attributed_to_spy(tmp_path: Path):
    path = tmp_path / "macro.db"
    db = AnalysisDatabase(path)
    at = datetime.now(UTC) - timedelta(seconds=20)
    add_news(db, "May PCE inflation matches forecasts; Fed outlook in focus", "MU", "pce1", at)
    analyzer = SnapshotAnalyzer(event_type="macro")
    svc = AIAnalysisService(settings(path), analyzer=analyzer, db=db, worker_id="w")
    assert svc.run_once(1) == 1
    row = db.recent_analyses(1, settings(path).analysis_key)[0]
    assert row["primary_symbol"] == "SPY"
    assert row["event_scope"] == "macro"
    assert row["subject_entity"] == "pce"
    predictions = db.prediction_rows(int(row["id"]))
    assert {p["symbol"] for p in predictions} == {"SPY"}
    assert analyzer.seen[0]["original_subscription_symbol"] == "MU"


def test_same_earnings_event_is_clustered_across_followup_articles(tmp_path: Path):
    path = tmp_path / "cluster.db"
    db = AnalysisDatabase(path)
    at = datetime.now(UTC) - timedelta(minutes=30)
    add_news(db, "Micron fiscal Q3 earnings beat expectations", "MU", "e1", at)
    add_news(db, "Micron third-quarter results and guidance recap", "MU", "e2", at + timedelta(minutes=20))
    analyzer = SnapshotAnalyzer(event_type="earnings")
    svc = AIAnalysisService(settings(path), analyzer=analyzer, db=db, worker_id="w")
    assert svc.run_once(10) == 2
    summary = db.analysis_summary(settings(path).analysis_key)
    assert summary["event_clusters"] == 1
    cluster = db.recent_clusters(1)[0]
    assert cluster["member_count"] == 2
    assert cluster["event_type"] == "earnings"


def test_official_document_is_prioritized_in_snapshot_and_analysis(tmp_path: Path):
    path = tmp_path / "official.db"
    db = AnalysisDatabase(path)
    at = datetime.now(UTC) - timedelta(seconds=20)
    news_id = add_news(db, "Company reports quarterly results", "MU", "o1", at)
    db.add_manual_official_document(
        news_id, "company_ir", "https://investors.example.com/results", "Official release",
        "Official earnings release: revenue and guidance details."
    )
    analyzer = SnapshotAnalyzer(event_type="earnings")
    svc = AIAnalysisService(settings(path), analyzer=analyzer, db=db, worker_id="w")
    svc.run_once(1)
    row = db.recent_analyses(1, settings(path).analysis_key)[0]
    assert row["source_tier"] == "official_primary"
    assert row["official_source_status"] == "attached"
    raw = analyzer.seen[0]
    assert raw["official_evidence"][0]["content"].startswith("Official earnings")


def test_low_edge_prediction_is_observe_only_not_valid(tmp_path: Path):
    path = tmp_path / "quality.db"
    db = AnalysisDatabase(path)
    at = datetime.now(UTC) - timedelta(seconds=20)
    add_news(db, "Micron launches a product", "MU", "q1", at)
    analyzer = SnapshotAnalyzer(event_type="product", confidence=0.40)
    svc = AIAnalysisService(settings(path), analyzer=analyzer, db=db, worker_id="w")
    svc.run_once(1)
    row = db.recent_analyses(1, settings(path).analysis_key)[0]
    predictions = db.prediction_rows(int(row["id"]))
    assert predictions
    assert {p["evaluation_status"] for p in predictions} == {"observe_only"}
    assert all(p["is_valid"] == 0 for p in predictions)


def test_retrospective_event_is_observation_eligible_and_not_predicted(tmp_path: Path):
    path = tmp_path / "retro.db"
    db = AnalysisDatabase(path)
    at = datetime.now(UTC) - timedelta(seconds=20)
    add_news(db, "Micron earnings recap after shares rose 17%", "MU", "r1", at)
    analyzer = SnapshotAnalyzer(event_type="earnings", stage="retrospective")
    svc = AIAnalysisService(settings(path), analyzer=analyzer, db=db, worker_id="w")
    svc.run_once(1)
    row = db.recent_analyses(1, settings(path).analysis_key)[0]
    assert row["prediction_eligible"] == 0
    assert row["reaction_observation_eligible"] == 1
    assert row["prediction_count"] == 0


def test_afterhours_trading_day_one_is_next_session_close_not_second_session():
    event = datetime(2026, 6, 25, 22, 0, tzinfo=UTC)  # 6pm New York in June
    assert due_time(event, "close") == due_time(event, "1d")
    assert evaluation_label(event, "close") == "next_regular_close"
