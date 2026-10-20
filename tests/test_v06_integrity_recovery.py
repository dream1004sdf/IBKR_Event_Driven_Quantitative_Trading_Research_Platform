from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from recorder.ai_provider import ProviderResult, TransientAnalysisError
from recorder.analysis_database import AnalysisDatabase
from recorder.analysis_models import EventFact, HorizonPrediction, NewsAnalysis
from recorder.analysis_service import AIAnalysisService
from recorder.config import Settings
from recorder.models import NewsInput, QuoteSnapshot

UTC = timezone.utc
HORIZONS = ("15m", "1h", "close", "1d", "3d")


def _settings(path: Path, **changes) -> Settings:
    base = Settings(
        database_path=path,
        ai_provider="test",
        ai_model="test-primary",
        ai_critical_model="test-critical",
        ai_prompt_version="news-agent-v6-recovery-state-machine-test",
        ai_body_wait_seconds=0,
        ai_batch_size=5,
        ai_backfill_max_age_days=30,
        ai_backfill_limit=100,
        ai_integrity_recovery_enabled=True,
        ai_integrity_recovery_min_impact=0.40,
        ai_integrity_recovery_min_validated_facts=2,
        ai_integrity_recovery_min_high_facts=2,
        ai_integrity_recovery_warning_reject_ratio=0.45,
        ai_integrity_recovery_partial_calibration_only=True,
        ai_use_article_body=True,
    )
    return replace(base, **changes)


def _add_news(db: AnalysisDatabase, *, article_id: str = "recovery") -> int:
    at = datetime.now(UTC) - timedelta(seconds=15)
    headline = "OpenAI and Broadcom announce custom AI inference chip collaboration"
    body = (
        "OpenAI and Broadcom announced a custom AI inference chip called Jalapeno. "
        "Broadcom provides silicon implementation and networking technology. "
        "The project aims to improve inference efficiency and lower serving costs. "
        "The companies described it as a multi-generation collaboration."
    )
    news_id = db.insert_news(
        NewsInput(
            provider="DJ-N",
            article_id=article_id,
            headline=headline,
            published_at=at,
            received_at=at,
            primary_symbol="AVGO",
            con_id=1,
            extra_data="",
        ),
        normalized_headline=headline.lower(),
        headline_hash=f"hash-{article_id}",
    )
    db.update_article_body(news_id, body)
    db.add_news_symbol(news_id, "AVGO", 1, at)
    db.insert_baseline(
        news_id,
        QuoteSnapshot(
            symbol="AVGO",
            timestamp=at,
            bid=299.9,
            ask=300.1,
            last=300.0,
            close=295.0,
            volume=2_000_000,
        ),
    )
    for horizon, minutes in {"15m": 15, "1h": 60, "close": 240, "1d": 1440, "3d": 4320}.items():
        db.create_reaction_target(news_id, "AVGO", horizon, at + timedelta(minutes=minutes))
    return news_id


def _predictions() -> list[HorizonPrediction]:
    return [
        HorizonPrediction(
            symbol="AVGO",
            horizon=h,
            expected_return=0.012,
            bear_return=-0.008,
            bull_return=0.032,
            up_probability=0.58,
            flat_probability=0.24,
            down_probability=0.18,
            confidence=0.68,
        )
        for h in HORIZONS
    ]


def _base_analysis(*, facts: list[EventFact]) -> NewsAnalysis:
    return NewsAnalysis(
        primary_symbol="AVGO",
        related_symbols=["AVGO"],
        event_scope="company",
        subject_entity="AVGO",
        event_subject_symbol="AVGO",
        subject_asset_type="stock",
        target_asset_type="stock",
        target_role="subject_asset",
        target_assets=["AVGO"],
        event_type="product",
        event_subtype="custom AI chip collaboration",
        event_stage="initial_release",
        sentiment="positive",
        causal_signal=True,
        prediction_eligible=True,
        reaction_observation_eligible=False,
        analysis_priority="high",
        impact_score=0.60,
        novelty_score=0.70,
        source_quality=0.85,
        confidence=0.70,
        time_horizon="1_to_3_days",
        summary="OpenAI and Broadcom announced a custom inference chip collaboration.",
        key_facts=facts,
        bull_case=["Broadcom expands its role in custom AI infrastructure."],
        bear_case=["No contract economics were disclosed."],
        predictions=_predictions(),
    )


def _initial_failed_analysis() -> NewsAnalysis:
    return _base_analysis(
        facts=[
            EventFact(name="Chip project", actual="custom AI inference chip called Jalapeno", importance="high"),
            EventFact(name="Broadcom role", actual="silicon implementation and networking technology", importance="high"),
            EventFact(name="Efficiency goal", actual="improve inference efficiency and lower serving costs", importance="medium"),
            EventFact(name="Broken one", actual="over the next four of", importance="medium"),
            EventFact(name="Broken two", actual="deployment was", importance="medium"),
            EventFact(name="Invented number", actual="$99 billion", importance="high"),
            EventFact(name="Invented date", actual="2035", importance="medium"),
        ]
    )


def _clean_recovery_analysis() -> NewsAnalysis:
    return _base_analysis(
        facts=[
            EventFact(name="Chip project", actual="custom AI inference chip called Jalapeno", importance="high"),
            EventFact(name="Broadcom role", actual="silicon implementation and networking technology", importance="high"),
            EventFact(name="Efficiency goal", actual="improve inference efficiency and lower serving costs", importance="medium"),
            EventFact(name="Collaboration scope", actual="multi-generation collaboration", importance="medium"),
        ]
    )


class RecoveringAnalyzer:
    provider_name = "test"
    model_name = "test-primary"

    def __init__(self, initial: NewsAnalysis, recovered: NewsAnalysis | Exception):
        self.initial = initial
        self.recovered = recovered
        self.recovery_calls = 0

    @staticmethod
    def _result(analysis: NewsAnalysis, model: str) -> ProviderResult:
        clone = analysis.model_copy(deep=True)
        return ProviderResult(
            analysis=clone,
            raw_json=clone.model_dump_json(),
            response_id=f"resp-{model}",
            input_tokens=10,
            output_tokens=20,
            duration_ms=10,
            provider_name="test",
            model_name=model,
            route_name=model,
            reasoning_effort="high",
        )

    def analyze(self, snapshot):
        return self._result(self.initial, "test-primary")

    def recover_integrity(self, snapshot, *, prior_result, integrity_notes):
        self.recovery_calls += 1
        assert snapshot["headline"].startswith("OpenAI and Broadcom")
        assert integrity_notes
        if isinstance(self.recovered, Exception):
            raise self.recovered
        return self._result(self.recovered, "test-critical")


def test_failed_high_signal_output_is_recovered_by_critical_pass(tmp_path: Path):
    path = tmp_path / "recovered.db"
    db = AnalysisDatabase(path)
    _add_news(db)
    analyzer = RecoveringAnalyzer(_initial_failed_analysis(), _clean_recovery_analysis())
    settings = _settings(path)
    service = AIAnalysisService(settings, analyzer=analyzer, db=db, worker_id="w")

    assert service.run_once(1) == 1
    assert analyzer.recovery_calls == 1
    row = db.recent_analyses(1, settings.analysis_key)[0]
    assert row["integrity_initial_status"] == "failed"
    assert row["integrity_recovery_status"] == "recovered"
    assert row["integrity_recovery_model"] == "test-critical"
    assert row["text_integrity_status"] == "valid"
    assert row["prediction_count"] == 5
    assert {p["evaluation_status"] for p in db.prediction_rows(int(row["id"]))} == {"valid"}


def test_partial_recovery_is_probability_calibration_only(tmp_path: Path):
    path = tmp_path / "partial.db"
    db = AnalysisDatabase(path)
    _add_news(db, article_id="partial")
    partial = _clean_recovery_analysis()
    partial.key_facts.append(EventFact(name="Truncated context", actual="the project was", importance="low"))
    analyzer = RecoveringAnalyzer(_initial_failed_analysis(), partial)
    settings = _settings(path)
    service = AIAnalysisService(settings, analyzer=analyzer, db=db, worker_id="w")

    service.run_once(1)
    row = db.recent_analyses(1, settings.analysis_key)[0]
    assert row["integrity_recovery_status"] == "partial"
    predictions = db.prediction_rows(int(row["id"]))
    assert {p["evaluation_status"] for p in predictions} == {"calibration_only"}
    assert all(p["valid_for_directional_trading"] == 0 for p in predictions)
    assert all(p["valid_for_probability_calibration"] == 1 for p in predictions)


def test_failed_recovery_suppresses_predictions_without_crashing(tmp_path: Path):
    path = tmp_path / "hard-fail.db"
    db = AnalysisDatabase(path)
    _add_news(db, article_id="hard")
    still_bad = _base_analysis(
        facts=[
            EventFact(name="Only good fact", actual="multi-generation collaboration", importance="high"),
            EventFact(name="Invented", actual="$200 billion", importance="critical"),
            EventFact(name="Broken", actual="the deployment was", importance="medium"),
        ]
    )
    analyzer = RecoveringAnalyzer(_initial_failed_analysis(), still_bad)
    settings = _settings(path)
    service = AIAnalysisService(settings, analyzer=analyzer, db=db, worker_id="w")

    assert service.run_once(1) == 1
    summary = db.analysis_summary(settings.analysis_key)
    assert summary["jobs_complete"] == 1
    assert summary["jobs_dead"] == 0
    row = db.recent_analyses(1, settings.analysis_key)[0]
    assert row["integrity_recovery_status"] == "hard_failed"
    assert row["prediction_eligible"] == 0
    assert row["prediction_count"] == 0


def test_recovery_provider_failure_degrades_safely_instead_of_retry_loop(tmp_path: Path):
    path = tmp_path / "unavailable.db"
    db = AnalysisDatabase(path)
    _add_news(db, article_id="unavailable")
    analyzer = RecoveringAnalyzer(
        _initial_failed_analysis(),
        TransientAnalysisError("simulated critical recovery timeout"),
    )
    settings = _settings(path)
    service = AIAnalysisService(settings, analyzer=analyzer, db=db, worker_id="w")

    assert service.run_once(1) == 1
    summary = db.analysis_summary(settings.analysis_key)
    assert summary["jobs_complete"] == 1
    assert summary["jobs_retry"] == 0
    assert summary["jobs_dead"] == 0
    row = db.recent_analyses(1, settings.analysis_key)[0]
    assert row["integrity_recovery_status"] == "unavailable"
    assert row["prediction_count"] == 0


def test_v05_database_migrates_recovery_columns(tmp_path: Path):
    path = tmp_path / "migration.db"
    db = AnalysisDatabase(path)
    with db.connect() as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(news_analyses)")}
    assert {
        "integrity_initial_status",
        "integrity_recovery_status",
        "integrity_recovery_model",
        "integrity_recovery_attempts",
    } <= columns


def test_router_integrity_recovery_uses_critical_model_and_immutable_snapshot_only():
    from recorder.ai_provider import OpenAIModelRouter

    class CaptureAnalyzer:
        provider_name = "test"
        model_name = "critical-capture"

        def __init__(self):
            self.snapshot = None

        def analyze(self, snapshot):
            self.snapshot = snapshot
            return RecoveringAnalyzer._result(_clean_recovery_analysis(), "critical-capture")

    critical = CaptureAnalyzer()
    primary = RecoveringAnalyzer(_initial_failed_analysis(), _clean_recovery_analysis())
    router = OpenAIModelRouter(
        primary=primary,
        critical=critical,
        critical_event_types=("earnings", "guidance", "fomc"),
        escalate_impact_score=0.75,
        escalate_confidence_below=0.55,
        fallback_to_primary=True,
        fallback_on_transient=False,
    )
    snapshot = {
        "headline": "OpenAI and Broadcom announce custom AI inference chip collaboration",
        "article_body": "Broadcom provides silicon implementation and networking technology.",
        "analysis_profile": "general",
    }
    prior = RecoveringAnalyzer._result(_initial_failed_analysis(), "test-primary")
    result = router.recover_integrity(
        snapshot,
        prior_result=prior,
        integrity_notes=("rejected malformed fact",),
    )
    assert result.model_name == "critical-capture"
    assert critical.snapshot["integrity_recovery"]["attempt"] == 1
    assert "prior_answer" not in critical.snapshot
    assert "over the next four of" not in str(critical.snapshot)


def test_recovery_system_prompt_requires_fresh_source_grounding():
    from recorder.prompting import system_prompt_for_profile

    prompt = system_prompt_for_profile("general", integrity_recovery=True)
    assert "fresh re-analysis" in prompt
    assert "Do not copy or repair wording from the rejected answer" in prompt


def _all_rejected_initial_analysis() -> NewsAnalysis:
    return _base_analysis(
        facts=[
            EventFact(name="Invented contract", actual="$99 billion", importance="high"),
            EventFact(name="Invented deployment", actual="2035", importance="high"),
            EventFact(name="Broken sentence", actual="over the next four of", importance="medium"),
            EventFact(name="Unsupported customer", actual="Amazon is the exclusive buyer", importance="high"),
            EventFact(name="Unsupported volume", actual="five million units", importance="medium"),
            EventFact(name="Unsupported margin", actual="adds 12 points of margin", importance="medium"),
        ]
    )


def test_zero_validated_facts_still_forces_recovery_for_high_signal_initial_release(tmp_path: Path):
    """Regression for the live AVGO case: failed 0/6 must call GPT-5.5 once."""
    path = tmp_path / "zero-validated.db"
    db = AnalysisDatabase(path)
    _add_news(db, article_id="zero-validated")
    analyzer = RecoveringAnalyzer(_all_rejected_initial_analysis(), _clean_recovery_analysis())
    settings = _settings(path, ai_integrity_recovery_min_high_facts=1)
    service = AIAnalysisService(settings, analyzer=analyzer, db=db, worker_id="w")

    assert service.run_once(1) == 1
    assert analyzer.recovery_calls == 1
    row = db.recent_analyses(1, settings.analysis_key)[0]
    assert row["integrity_initial_status"] == "failed"
    assert row["integrity_recovery_status"] == "recovered"
    assert row["integrity_recovery_attempts"] == 1
    assert row["integrity_recovery_model"] == "test-critical"
    assert not (
        row["integrity_recovery_status"] in {"hard_failed", "unavailable"}
        and row["integrity_recovery_attempts"] == 0
    )


def test_failed_low_impact_event_is_not_attempted_not_hard_failed(tmp_path: Path):
    path = tmp_path / "low-impact.db"
    db = AnalysisDatabase(path)
    _add_news(db, article_id="low-impact")
    initial = _all_rejected_initial_analysis()
    initial.impact_score = 0.24
    analyzer = RecoveringAnalyzer(initial, _clean_recovery_analysis())
    settings = _settings(path)
    service = AIAnalysisService(settings, analyzer=analyzer, db=db, worker_id="w")

    service.run_once(1)
    assert analyzer.recovery_calls == 0
    row = db.recent_analyses(1, settings.analysis_key)[0]
    assert row["integrity_recovery_status"] == "not_attempted_low_impact"
    assert row["integrity_recovery_attempts"] == 0
    assert row["integrity_recovery_model"] == ""


def test_attribution_mismatch_is_not_marked_unavailable(tmp_path: Path):
    path = tmp_path / "mismatch.db"
    db = AnalysisDatabase(path)
    _add_news(db, article_id="mismatch")
    initial = _all_rejected_initial_analysis()
    initial.event_subtype = "unclassified headline/body mismatch"
    analyzer = RecoveringAnalyzer(initial, _clean_recovery_analysis())
    settings = _settings(path)
    service = AIAnalysisService(settings, analyzer=analyzer, db=db, worker_id="w")

    service.run_once(1)
    assert analyzer.recovery_calls == 0
    row = db.recent_analyses(1, settings.analysis_key)[0]
    assert row["integrity_recovery_status"] == "not_attempted_attribution_mismatch"
    assert row["integrity_recovery_attempts"] == 0
    assert row["integrity_recovery_model"] == ""


def test_retrospective_failed_output_is_not_attempted_retrospective(tmp_path: Path):
    path = tmp_path / "retrospective.db"
    db = AnalysisDatabase(path)
    _add_news(db, article_id="retrospective")
    initial = _all_rejected_initial_analysis()
    initial.event_stage = "retrospective"
    analyzer = RecoveringAnalyzer(initial, _clean_recovery_analysis())
    settings = _settings(path)
    service = AIAnalysisService(settings, analyzer=analyzer, db=db, worker_id="w")

    service.run_once(1)
    assert analyzer.recovery_calls == 0
    row = db.recent_analyses(1, settings.analysis_key)[0]
    assert row["integrity_recovery_status"] == "not_attempted_retrospective"
    assert row["integrity_recovery_attempts"] == 0


def test_attempted_terminal_states_never_have_zero_attempts(tmp_path: Path):
    path = tmp_path / "terminal-invariant.db"
    db = AnalysisDatabase(path)
    _add_news(db, article_id="terminal-hard")
    still_bad = _all_rejected_initial_analysis()
    analyzer = RecoveringAnalyzer(_all_rejected_initial_analysis(), still_bad)
    settings = _settings(path)
    service = AIAnalysisService(settings, analyzer=analyzer, db=db, worker_id="w")

    service.run_once(1)
    row = db.recent_analyses(1, settings.analysis_key)[0]
    assert row["integrity_recovery_status"] == "hard_failed"
    assert row["integrity_recovery_attempts"] >= 1
    assert row["integrity_recovery_model"]


class AnalyzeOnlyAnalyzer:
    provider_name = "test"
    model_name = "test-primary"

    def __init__(self, analysis: NewsAnalysis):
        self.analysis = analysis

    def analyze(self, snapshot):
        return RecoveringAnalyzer._result(self.analysis, "test-primary")


def test_required_recovery_without_critical_model_is_not_marked_unavailable(tmp_path: Path):
    path = tmp_path / "no-recovery-model.db"
    db = AnalysisDatabase(path)
    _add_news(db, article_id="no-recovery-model")
    analyzer = AnalyzeOnlyAnalyzer(_all_rejected_initial_analysis())
    settings = _settings(path)
    service = AIAnalysisService(settings, analyzer=analyzer, db=db, worker_id="w")

    service.run_once(1)
    row = db.recent_analyses(1, settings.analysis_key)[0]
    assert row["integrity_recovery_status"] == "not_attempted_no_recovery_model"
    assert row["integrity_recovery_attempts"] == 0
    assert row["integrity_recovery_model"] == ""


def test_disabled_recovery_has_explicit_not_attempted_status(tmp_path: Path):
    path = tmp_path / "disabled-recovery.db"
    db = AnalysisDatabase(path)
    _add_news(db, article_id="disabled-recovery")
    analyzer = RecoveringAnalyzer(_all_rejected_initial_analysis(), _clean_recovery_analysis())
    settings = _settings(path, ai_integrity_recovery_enabled=False)
    service = AIAnalysisService(settings, analyzer=analyzer, db=db, worker_id="w")

    service.run_once(1)
    assert analyzer.recovery_calls == 0
    row = db.recent_analyses(1, settings.analysis_key)[0]
    assert row["integrity_recovery_status"] == "not_attempted_disabled"
    assert row["integrity_recovery_attempts"] == 0
