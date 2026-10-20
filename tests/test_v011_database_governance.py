from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from recorder.ai_provider import HeuristicNewsAnalyzer
from recorder.analysis_database import AnalysisDatabase
from recorder.analysis_models import NewsAnalysis
from recorder.analysis_service import AIAnalysisService
from recorder.config import Settings
from recorder.database import Database
from recorder.db_audit import audit_database
from recorder.event_clusters import build_cluster_identity
from recorder.models import NewsInput, QuoteSnapshot

UTC = timezone.utc


def _add_news(db: Database, headline: str, symbol: str, article_id: str) -> int:
    at = datetime.now(UTC) - timedelta(seconds=20)
    news_id = db.insert_news(
        NewsInput("TEST", article_id, headline, at, at, symbol, 123, ""),
        headline.lower(),
        f"hash-{article_id}",
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
            close=99.0,
            currency="USD",
            con_id=123,
            contract_symbol=symbol,
            sec_type="STK",
            exchange="SMART",
            primary_exchange="NASDAQ",
            multiplier="1",
            market_data_type=1,
        ),
    )
    for horizon, minutes in {"15m": 15, "1h": 60, "close": 240, "1d": 1440, "3d": 4320}.items():
        db.create_reaction_target(news_id, symbol, horizon, at + timedelta(minutes=minutes))
    return news_id


def _settings(path: Path) -> Settings:
    return Settings(
        database_path=path,
        ai_provider="heuristic",
        ai_model="heuristic-v1",
        ai_prompt_version="v011-test",
        ai_body_wait_seconds=0,
        ai_batch_size=10,
        ai_backfill_limit=100,
        ai_backfill_max_age_days=30,
    )


def _analysis(**changes) -> NewsAnalysis:
    base = NewsAnalysis(
        primary_symbol="SPY",
        related_symbols=["SPY"],
        event_scope="market",
        subject_entity="us_equities",
        subject_asset_type="macro_event",
        target_asset_type="etf",
        target_role="market_proxy",
        target_assets=["SPY"],
        attribution_status="delivery_proxy",
        attribution_confidence=0.94,
        original_subscription_symbol="MU",
        subject_target_available=True,
        article_scope="market",
        article_primary_subject="us_equities",
        mentioned_symbols=["MU"],
        subscription_entity_role="mentioned_entity",
        event_type="market_price_update",
        event_subtype="broad market recap",
        event_stage="retrospective",
        sentiment="mixed",
        causal_signal=False,
        prediction_eligible=False,
        reaction_observation_eligible=False,
        analysis_priority="low",
        impact_score=0.2,
        novelty_score=0.1,
        source_quality=0.8,
        confidence=0.9,
        time_horizon="intraday",
        summary="Retrospective market recap.",
    )
    return base.model_copy(update=changes)


def test_market_price_update_excludes_reaction_targets_after_analysis(tmp_path: Path):
    path = tmp_path / "events.db"
    db = AnalysisDatabase(path)
    news_id = _add_news(db, "QQQ ETF Falls 3.3%", "QQQ", "m1")
    settings = _settings(path)
    service = AIAnalysisService(settings, analyzer=HeuristicNewsAnalyzer(), db=db, worker_id="w")
    assert service.run_once(limit=10) == 1
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT usage_status,calibration_eligible,observation_eligible FROM reaction_targets WHERE news_id=?",
            (news_id,),
        ).fetchall()
    assert [tuple(row) for row in rows] == [("excluded", 0, 0)]


def test_fresh_causal_event_marks_reactions_eligible(tmp_path: Path):
    path = tmp_path / "events.db"
    db = AnalysisDatabase(path)
    news_id = _add_news(db, "Micron raises quarterly revenue outlook on AI demand", "MU", "m2")
    settings = _settings(path)
    service = AIAnalysisService(settings, analyzer=HeuristicNewsAnalyzer(), db=db, worker_id="w")
    assert service.run_once(limit=10) == 1
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT usage_status,calibration_eligible,observation_eligible FROM reaction_targets WHERE news_id=?",
            (news_id,),
        ).fetchall()
    assert [tuple(row) for row in rows] == [("eligible", 1, 1)]


def test_cluster_family_separates_broad_recap_from_instrument_recap():
    published = datetime.now(UTC).isoformat()
    broad = _analysis()
    instrument = _analysis(
        original_subscription_symbol="SPY",
        subscription_entity_role="market_proxy",
        attribution_status="matched",
        mentioned_symbols=[],
        event_subtype="automated price move",
    )
    broad_id = build_cluster_identity(
        broad,
        headline="Dow Jones Futures Fall, Techs Skid",
        body="Micron rose while megacaps fell.",
        published_at=published,
    )
    instrument_id = build_cluster_identity(
        instrument,
        headline="SPY ETF Declines 0.3%",
        body="The ETF declined during the session.",
        published_at=published,
    )
    assert broad_id.cluster_family == "broad_market_recap"
    assert instrument_id.cluster_family == "instrument_market_recap"
    assert broad_id.cluster_key != instrument_id.cluster_key


def test_database_audit_detects_no_governance_leak_after_analysis(tmp_path: Path):
    path = tmp_path / "events.db"
    db = AnalysisDatabase(path)
    _add_news(db, "QQQ ETF Falls 3.3%", "QQQ", "m3")
    settings = _settings(path)
    service = AIAnalysisService(settings, analyzer=HeuristicNewsAnalyzer(), db=db, worker_id="w")
    service.run_once(limit=10)
    result = audit_database(path)
    error_codes = {f["code"] for f in result["findings"] if f["severity"] in {"critical", "error"}}
    assert "governance_calibration_leak" not in error_codes
    assert "analyzed_news_still_provisional" not in error_codes


def test_market_event_asset_type_round_trips_from_stored_json():
    analysis = _analysis(subject_asset_type="market_event")
    restored = NewsAnalysis.model_validate_json(analysis.model_dump_json())
    assert restored.subject_asset_type == "market_event"
