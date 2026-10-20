from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from recorder.analysis_database import AnalysisDatabase
from recorder.attribution import ATTRIBUTION_ENGINE_VERSION, attribute_event_targets
from recorder.analysis_models import HorizonPrediction, NewsAnalysis
from recorder.models import NewsInput, QuoteSnapshot

UTC = timezone.utc


def _predictions(symbol: str) -> list[HorizonPrediction]:
    return [
        HorizonPrediction(
            symbol=symbol,
            horizon=horizon,
            expected_return=0.01,
            bear_return=-0.02,
            bull_return=0.03,
            up_probability=0.55,
            flat_probability=0.25,
            down_probability=0.20,
            confidence=0.70,
        )
        for horizon in ("15m", "1h", "close", "1d", "3d")
    ]


def _analysis(*, attribution_status: str | None = None) -> NewsAnalysis:
    payload = {
        "primary_symbol": "MU",
        "related_symbols": ["MU"],
        "event_type": "guidance",
        "event_subtype": "raised outlook",
        "event_stage": "initial_release",
        "sentiment": "positive",
        "causal_signal": True,
        "prediction_eligible": True,
        "analysis_priority": "high",
        "impact_score": 0.80,
        "novelty_score": 0.70,
        "source_quality": 0.80,
        "confidence": 0.75,
        "time_horizon": "1_to_3_days",
        "summary": "Guidance was raised.",
        "predictions": _predictions("MU"),
    }
    if attribution_status is not None:
        payload["attribution_status"] = attribution_status
    return NewsAnalysis.model_validate(payload)


def test_provider_attribution_is_pending_until_snapshot_enrichment() -> None:
    pending = _analysis()
    assert pending.attribution_status == "pending"
    assert len(pending.predictions) == 5

    unresolved = _analysis(attribution_status="unresolved")
    assert unresolved.prediction_eligible is False
    assert unresolved.predictions == []


def test_actual_subject_is_separate_from_delivery_symbol() -> None:
    alphabet = attribute_event_targets(
        headline="Alphabet earnings beat sends Google shares higher",
        body="Alphabet reported stronger advertising revenue and raised its outlook.",
        original_symbol="NVDA",
        available_symbols=("NVDA", "SPY", "QQQ"),
    )
    assert alphabet.subject_name == "Alphabet"
    assert alphabet.subject_symbol == "GOOGL"
    assert alphabet.attribution_status == "mismatch"
    assert alphabet.primary_targets == ()

    nvidia = attribute_event_targets(
        headline="Nvidia announces a new data-center product",
        body="Nvidia introduced a new platform for enterprise AI workloads.",
        original_symbol="AVGO",
        available_symbols=("AVGO", "NVDA", "SPY"),
    )
    assert nvidia.subject_name == "Nvidia"
    assert nvidia.subject_symbol == "NVDA"
    assert nvidia.attribution_status == "delivery_proxy"
    assert nvidia.affected_symbols == ("NVDA",)
    assert nvidia.benchmark_symbols == ("SPY",)


def test_multi_subject_and_market_level_articles_are_not_forced_to_a_company() -> None:
    multi_subject = attribute_event_targets(
        headline="Nvidia and AMD announce a joint AI accelerator standard",
        body="Nvidia and AMD will support the shared interface.",
        original_symbol="NVDA",
        available_symbols=("NVDA", "AMD", "SPY", "QQQ"),
    )
    assert multi_subject.attribution_status == "multi_subject"
    assert multi_subject.subject_symbol == ""
    assert multi_subject.subject_target_available is False
    assert set(multi_subject.affected_symbols) == {"NVDA", "AMD"}

    market = attribute_event_targets(
        headline="Markets rally as investors digest inflation data",
        body="Broad U.S. equity indexes gained in afternoon trading.",
        original_symbol="NVDA",
        available_symbols=("NVDA", "SPY", "QQQ"),
    )
    assert market.attribution_status == "market_level"
    assert market.subject_symbol == ""
    assert market.subject_name == "U.S. equities"


def test_geopolitical_news_uses_market_or_sector_proxy_when_no_company_subject() -> None:
    broad = attribute_event_targets(
        headline="Oil jumps after missile strike raises Middle East geopolitical risk",
        body="Investors watched crude supply risk, haven demand, Treasury yields, and volatility.",
        original_symbol="MU",
        available_symbols=("MU", "SPY", "QQQ", "TLT", "IWM"),
    )
    assert broad.event_scope == "sector"
    assert broad.subject_entity == "energy_supply"
    assert broad.subject_symbol == ""
    assert broad.primary_symbol == "SPY"
    assert broad.primary_target_role == "market_proxy"
    assert broad.attribution_status == "delivery_proxy"
    assert broad.subject_target_available is True

    chips = attribute_event_targets(
        headline="Taiwan tensions raise risk for global chip supply chains",
        body="The report focused on semiconductor production risk rather than one company.",
        original_symbol="NVDA",
        available_symbols=("NVDA", "SOXX", "QQQ", "SPY"),
    )
    assert chips.event_scope == "sector"
    assert chips.subject_entity == "semiconductors"
    assert chips.primary_symbol == "SOXX"
    assert chips.primary_target_role == "sector_proxy"
    assert chips.attribution_status == "delivery_proxy"


def test_attribution_backfill_is_idempotent_and_preserves_evidence(tmp_path: Path) -> None:
    db = AnalysisDatabase(tmp_path / "attribution.db")
    at = datetime.now(UTC)
    news_id = db.insert_news(
        NewsInput(
            "TEST",
            "alphabet-delivered-through-nvda",
            "Alphabet earnings beat sends Google shares higher",
            at,
            at,
            "NVDA",
            1,
            "",
        ),
        "alphabet earnings beat sends google shares higher",
        "alphabet-event",
    )
    db.update_article_body(news_id, "Alphabet reported stronger advertising revenue and raised its outlook.")
    for symbol in ("NVDA", "SPY", "QQQ"):
        db.add_news_symbol(news_id, symbol, 1, at)
        db.insert_baseline(news_id, QuoteSnapshot(symbol, at, last=100.0, currency="USD"))

    first = db.attribution_backfill()
    second = db.attribution_backfill()
    report = db.attribution_report()

    assert first["created"] == 1
    assert second["created"] == 0
    assert report["attribution_version"] == ATTRIBUTION_ENGINE_VERSION
    assert report["status_counts"] == {"mismatch": 1}
    row = report["rows"][0]
    assert row["delivery_symbol"] == "NVDA"
    assert row["actual_subject_name"] == "Alphabet"
    assert row["actual_subject_symbol"] == "GOOGL"
    assert json.loads(row["affected_symbols_json"]) == ["GOOGL"]
    evidence = json.loads(row["attribution_evidence_json"])
    assert evidence[0]["engine_version"] == ATTRIBUTION_ENGINE_VERSION
