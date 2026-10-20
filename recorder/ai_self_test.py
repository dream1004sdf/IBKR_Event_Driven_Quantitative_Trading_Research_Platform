from __future__ import annotations

import tempfile
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .ai_provider import HeuristicNewsAnalyzer
from .analysis_database import AnalysisDatabase
from .analysis_service import AIAnalysisService
from .config import Settings
from .models import NewsInput, QuoteSnapshot

UTC = timezone.utc


def _insert_event(db: AnalysisDatabase, article_id: str, headline: str, symbol: str) -> int:
    at = datetime.now(UTC) - timedelta(seconds=30)
    news_id = db.insert_news(
        NewsInput(
            provider="SELFTEST",
            article_id=article_id,
            headline=headline,
            published_at=at,
            received_at=at,
            primary_symbol=symbol,
            con_id=1,
            extra_data="",
        ),
        normalized_headline=headline.lower(),
        headline_hash=f"selftest-{article_id}",
    )
    db.add_news_symbol(news_id, symbol, 1, at)
    db.insert_baseline(
        news_id,
        QuoteSnapshot(
            symbol=symbol,
            timestamp=at,
            bid=99.9,
            ask=100.1,
            last=100.0,
            close=99.0,
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


def run_ai_self_test() -> dict[str, object]:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "ai_self_test.db"
        db = AnalysisDatabase(path)
        _insert_event(
            db,
            "A1",
            "Micron raises quarterly revenue outlook on stronger AI demand",
            "MU",
        )
        _insert_event(db, "A2", "QQQ ETF Falls 3.3%", "QQQ")
        _insert_event(db, "A3", "Broadcom and OpenAI develop custom chip", "AVGO")

        settings = replace(
            Settings(),
            database_path=path,
            ai_provider="heuristic",
            ai_model="heuristic-v1",
            ai_body_wait_seconds=0,
            ai_batch_size=10,
            ai_backfill_limit=100,
        )
        service = AIAnalysisService(
            settings,
            analyzer=HeuristicNewsAnalyzer(),
            db=db,
            worker_id="ai-self-test",
        )
        processed = service.run_once(limit=10)
        summary = db.analysis_summary(settings.analysis_key)

        assert processed == 3
        assert summary["jobs_complete"] == 3
        assert summary["analyses"] == 3
        assert summary["predictions"] == 10  # Two causal events × five horizons.
        assert service.run_once(limit=10) == 0  # Immutable/idempotent.

        return {
            "status": "PASS",
            "processed_jobs": processed,
            "analyses": summary["analyses"],
            "predictions": summary["predictions"],
            "market_price_update_filtered": True,
            "idempotent_second_run": True,
            "sqlite_migration_and_persistence": True,
        }
