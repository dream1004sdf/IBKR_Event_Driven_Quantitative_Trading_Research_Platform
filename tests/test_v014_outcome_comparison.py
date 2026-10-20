from __future__ import annotations

import csv
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from recorder.analysis_database import AnalysisDatabase
from recorder.evidence_packet import (
    EVIDENCE_PACKET_VERSION,
    build_overlay_evidence_packet,
    evidence_packet_markdown,
    evidence_packet_summary,
)
from recorder.outcome_comparison import ReplayOutcomeComparisonReporter, format_comparison_text
from recorder.market_overlay import (
    emergency_overlay_markdown,
    emergency_overlay_summary,
    market_overlay_markdown,
    market_overlay_summary,
    overlay_for_score,
)
from recorder.replay_dataset import REPLAY_DATASET_VERSION, ReplayDatasetExporter
from recorder.replay_scoring import (
    HeuristicReplayScorer,
    OpenAIReplayPrediction,
    OpenAIReplayScorer,
    OPENAI_REPLAY_SCORER_VERSION_V14_OVERLAY_V1,
    OPENAI_REPLAY_SCORER_VERSION_V14_OVERLAY_V2,
    OPENAI_REPLAY_SCORER_VERSION_V14_OVERLAY_V3,
    OPENAI_REPLAY_SCORER_VERSION_V14_NUANCED_V1,
    ReplayScoringEngine,
    postscore_threshold_tuning_markdown,
    postscore_threshold_tuning_summary,
    transform_v14_overlay_postscore,
    transform_v14_overlay_postscore_v2,
    transform_v14_overlay_postscore_v3,
    transform_v14_nuanced_action_postscore,
    action_gate_markdown,
    action_gate_summary,
    calibration_summary,
    error_analysis_markdown,
    error_analysis_summary,
    leaderboard_markdown,
    oos_markdown,
    oos_summary,
    pair_comparison_markdown,
    pair_comparison_summary,
    paper_action_simulation_markdown,
    paper_action_simulation_summary,
    paper_order_objects_markdown,
    paper_order_objects_summary,
    paper_order_parameter_tuning_markdown,
    paper_order_risk_layer_markdown,
    paper_order_risk_layer_summary,
    paper_portfolio_risk_layer_markdown,
    paper_portfolio_risk_layer_summary,
    summarize_scores,
    tune_paper_order_parameters,
    variant_comparison_markdown,
    variant_comparison_summary,
    walk_forward_paper_order_parameter_tuning,
    walk_forward_paper_order_parameter_tuning_markdown,
    _compact_scorer_evidence_packet,
)
from recorder.replay_corpus import ReplayCorpusBuilder
from recorder.surprise_adjustments import (
    EvidenceSourceWeight,
    OpenAISurpriseAdjustmentAnalyzer,
    OpenAISurpriseCauseAnalysis,
    ReplaySurpriseAdjustmentEngine,
    adjustment_markdown,
    format_adjustment_report,
    make_surprise_adjustment_note,
)


def _official_doc(symbol: str, suffix: str, published_at: str, content: str = "Official financial results release."):
    return SimpleNamespace(
        source_type="sec",
        source_url=f"https://www.sec.gov/Archives/example/{suffix}.htm",
        title=f"{symbol} 8-K Example",
        content=content,
        content_hash=f"hash-{suffix}",
        document_type="8-K",
        event_type="earnings",
        actual_subject_symbol=symbol,
        published_at=published_at,
        accepted_at=published_at,
        first_seen_at=published_at,
        available_at=published_at,
        external_document_id=f"sec:{suffix}",
        raw_snapshot="<html>Official financial results release.</html>",
        materiality_classification="material_earnings",
        materiality_reason="test",
        ingestion_version="test",
        status="fetched",
    )


def _historical_event(document_id: int, symbol: str, suffix: str, timestamp: str):
    return SimpleNamespace(
        event_type="earnings",
        event_subtype="quarterly_results",
        actual_subject_symbol=symbol,
        official_document_id=document_id,
        event_timestamp=timestamp,
        available_at=timestamp,
        source_hash=f"hash-{suffix}",
        prediction_cutoff=timestamp,
        market_session="after_hours",
        replay_eligibility="eligible",
        replay_eligibility_reasons=(),
        ingestion_version="test",
    )


def _add_replay_event(
    db: AnalysisDatabase,
    symbol: str,
    suffix: str,
    timestamp: str,
    content: str = "Official financial results release.",
) -> dict:
    document_id, _ = db.upsert_official_document(_official_doc(symbol, suffix, timestamp, content))
    event_id, _ = db.upsert_historical_event(_historical_event(document_id, symbol, suffix, timestamp))
    result = ReplayCorpusBuilder(db).build(event_type="earnings")
    assert result["eligible"] >= 1
    row = [
        item
        for item in db.recent_replay_corpus(limit=10, event_type="earnings")
        if item["historical_event_id"] == event_id
    ][0]
    row["historical_event_id"] = event_id
    return row


def _outcome(row: dict, *, source: str, horizon: str, status: str = "complete", raw=0.0, excess=0.0):
    return SimpleNamespace(
        replay_corpus_id=row["id"],
        historical_event_id=row["historical_event_id"],
        symbol=row["actual_subject_symbol"],
        benchmark_symbol="SPY",
        horizon=horizon,
        baseline_at="2026-06-24",
        reaction_at="2026-06-25" if status == "complete" else None,
        baseline_price=100.0 if status == "complete" else None,
        reaction_price=100.0 * (1.0 + raw) if status == "complete" else None,
        raw_return=raw if status == "complete" else None,
        benchmark_baseline_price=500.0 if status == "complete" else None,
        benchmark_reaction_price=500.0 * (1.0 + raw - excess) if status == "complete" else None,
        benchmark_return=(raw - excess) if status == "complete" else None,
        excess_return=excess if status == "complete" else None,
        direction_label="unknown" if status != "complete" else "up" if raw > 0 else "down" if raw < 0 else "flat",
        price_source=source,
        price_status=status,
        blocker_reason="" if status == "complete" else "price data unavailable",
        ingestion_version="test",
    )


def _score_variant(row: dict, *, predicted_direction: str, scorer_version: str):
    seed = HeuristicReplayScorer().score(row)
    expected_return = {"up": 0.01, "down": -0.01, "flat": 0.0}[predicted_direction]
    actual_raw = seed.actual_raw_return
    actual_excess = seed.actual_excess_return
    direction_hit = int(predicted_direction == seed.actual_direction)
    return replace(
        seed,
        scorer_name="openai",
        model_name="gpt-test-replay",
        scorer_version=scorer_version,
        predicted_direction=predicted_direction,
        expected_return=expected_return,
        confidence=0.34,
        direction_hit=direction_hit,
        absolute_raw_error=(
            abs(expected_return - float(actual_raw))
            if actual_raw is not None
            else None
        ),
        absolute_excess_error=(
            abs(expected_return - float(actual_excess))
            if actual_excess is not None
            else None
        ),
    )


def test_outcome_comparison_summary_aggregates_complete_and_blocked_rows(tmp_path: Path) -> None:
    db = AnalysisDatabase(tmp_path / "comparison-summary.db")
    first = _add_replay_event(db, "MU", "mu-1", "2026-06-24T20:02:01+00:00")
    second = _add_replay_event(db, "MU", "mu-2", "2026-06-25T20:02:01+00:00")
    db.upsert_replay_market_outcome(
        _outcome(first, source="yahoo_chart_daily_adjclose", horizon="1D", raw=0.10, excess=0.08)
    )
    db.upsert_replay_market_outcome(
        _outcome(second, source="yahoo_chart_daily_adjclose", horizon="1D", raw=-0.02, excess=-0.03)
    )
    db.upsert_replay_market_outcome(
        _outcome(second, source="ibkr_historical_trades_daily", horizon="1D", status="blocked")
    )

    rows = db.replay_outcome_comparison_rows(limit=10)
    comparison = ReplayOutcomeComparisonReporter(rows).build()

    yahoo = [
        item
        for item in comparison.summary
        if item["price_source"] == "yahoo_chart_daily_adjclose" and item["horizon"] == "1D"
    ][0]
    assert yahoo["rows"] == 2
    assert yahoo["complete"] == 2
    assert yahoo["blocked"] == 0
    assert yahoo["up"] == 1
    assert yahoo["down"] == 1
    assert round(float(yahoo["avg_raw_return"]), 6) == 0.04
    assert round(float(yahoo["avg_excess_return"]), 6) == 0.025

    ibkr = [
        item
        for item in comparison.summary
        if item["price_source"] == "ibkr_historical_trades_daily" and item["horizon"] == "1D"
    ][0]
    assert ibkr["complete"] == 0
    assert ibkr["blocked"] == 1
    assert ibkr["avg_raw_return"] is None


def test_outcome_comparison_filters_by_symbol_and_price_source(tmp_path: Path) -> None:
    db = AnalysisDatabase(tmp_path / "comparison-filter.db")
    mu = _add_replay_event(db, "MU", "mu", "2026-06-24T20:02:01+00:00")
    nvda = _add_replay_event(db, "NVDA", "nvda", "2026-06-25T20:02:01+00:00")
    db.upsert_replay_market_outcome(
        _outcome(mu, source="yahoo_chart_daily_adjclose", horizon="1D", raw=0.01, excess=0.00)
    )
    db.upsert_replay_market_outcome(
        _outcome(mu, source="ibkr_historical_trades_daily", horizon="1D", raw=0.02, excess=0.01)
    )
    db.upsert_replay_market_outcome(
        _outcome(nvda, source="yahoo_chart_daily_adjclose", horizon="1D", raw=0.03, excess=0.02)
    )

    rows = db.replay_outcome_comparison_rows(
        limit=10,
        symbol="mu",
        price_source="ibkr_historical_trades_daily",
    )
    comparison = ReplayOutcomeComparisonReporter(rows).build()

    assert len(comparison.events) == 1
    assert comparison.events[0]["symbol"] == "MU"
    assert [item["price_source"] for item in comparison.events[0]["outcomes"]] == [
        "ibkr_historical_trades_daily"
    ]


def test_outcome_comparison_text_and_json_shape(tmp_path: Path) -> None:
    db = AnalysisDatabase(tmp_path / "comparison-text.db")
    event = _add_replay_event(db, "MU", "mu", "2026-06-24T20:02:01+00:00")
    db.upsert_replay_market_outcome(
        _outcome(
            event,
            source="ibkr_historical_trades_5_mins_all_sessions",
            horizon="30M",
            raw=0.0125,
            excess=0.01,
        )
    )

    comparison = ReplayOutcomeComparisonReporter(db.replay_outcome_comparison_rows(limit=10)).build()
    payload = comparison.as_dict()
    text = format_comparison_text(comparison)

    assert payload["summary"][0]["horizon"] == "30M"
    assert payload["events"][0]["outcomes"][0]["raw_return"] == 0.0125
    assert "Replay outcome comparison summary:" in text
    assert "ibkr_historical_trades_5_mins_all_sessions 30M" in text
    json.dumps(payload)


def test_replay_dataset_export_rows_default_to_complete_labels(tmp_path: Path) -> None:
    db = AnalysisDatabase(tmp_path / "dataset-rows.db")
    event = _add_replay_event(db, "MU", "mu", "2026-06-24T20:02:01+00:00")
    db.upsert_replay_market_outcome(
        _outcome(event, source="yahoo_chart_daily_adjclose", horizon="1D", raw=0.04, excess=0.03)
    )
    db.upsert_replay_market_outcome(
        _outcome(event, source="yahoo_chart_daily_adjclose", horizon="5D", status="blocked")
    )

    complete_rows = db.replay_dataset_export_rows(limit=10)
    all_rows = db.replay_dataset_export_rows(limit=10, include_blocked=True)

    assert len(complete_rows) == 1
    assert complete_rows[0]["price_status"] == "complete"
    assert complete_rows[0]["horizon"] == "1D"
    assert len(all_rows) == 2


def test_replay_dataset_export_writes_csv_and_manifest(tmp_path: Path) -> None:
    db = AnalysisDatabase(tmp_path / "dataset-csv.db")
    event = _add_replay_event(db, "MU", "mu", "2026-06-24T20:02:01+00:00")
    db.upsert_replay_market_outcome(
        _outcome(event, source="yahoo_chart_daily_adjclose", horizon="1D", raw=0.04, excess=0.03)
    )
    output = tmp_path / "exports" / "dataset.csv"

    result = ReplayDatasetExporter(db.replay_dataset_export_rows(limit=10)).export(
        output,
        file_format="csv",
        include_input_snapshot=False,
        include_blocked=False,
        filters={"symbol": "MU"},
    )

    assert result.row_count == 1
    with output.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows[0]["dataset_version"] == REPLAY_DATASET_VERSION
    assert rows[0]["symbol"] == "MU"
    assert rows[0]["target_direction"] == "up"
    assert "input_snapshot" not in rows[0]
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["rows"] == 1
    assert manifest["complete_rows"] == 1
    assert manifest["include_input_snapshot"] is False


def test_replay_dataset_export_writes_jsonl_with_optional_snapshot(tmp_path: Path) -> None:
    db = AnalysisDatabase(tmp_path / "dataset-jsonl.db")
    event = _add_replay_event(db, "NVDA", "nvda", "2026-06-25T20:02:01+00:00")
    db.upsert_replay_market_outcome(
        _outcome(
            event,
            source="ibkr_historical_trades_5_mins_all_sessions",
            horizon="30M",
            raw=-0.012,
            excess=-0.01,
        )
    )
    output = tmp_path / "dataset.jsonl"

    result = ReplayDatasetExporter(db.replay_dataset_export_rows(limit=10)).export(
        output,
        file_format="jsonl",
        include_input_snapshot=True,
        include_blocked=False,
    )

    assert result.row_count == 1
    record = json.loads(output.read_text(encoding="utf-8").strip())
    assert record["symbol"] == "NVDA"
    assert record["horizon"] == "30M"
    assert record["target_direction"] == "down"
    assert record["input_snapshot"]["snapshot_type"] == "official_historical_event"
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["include_input_snapshot"] is True


def test_replay_score_build_persists_versioned_scores_idempotently(tmp_path: Path) -> None:
    db = AnalysisDatabase(tmp_path / "score-build.db")
    event = _add_replay_event(
        db,
        "MU",
        "mu-score",
        "2026-06-24T20:02:01+00:00",
        content="Micron beats expectations and raises outlook on strong growth.",
    )
    db.upsert_replay_market_outcome(
        _outcome(event, source="yahoo_chart_daily_adjclose", horizon="1D", raw=0.04, excess=0.03)
    )

    engine = ReplayScoringEngine(db, HeuristicReplayScorer())
    first = engine.build(symbol="MU", price_source="yahoo_chart_daily_adjclose", horizon="1D", limit=10)
    second = engine.build(symbol="MU", price_source="yahoo_chart_daily_adjclose", horizon="1D", limit=10)
    third = engine.build(
        symbol="MU",
        price_source="yahoo_chart_daily_adjclose",
        horizon="1D",
        limit=10,
        only_missing=True,
    )

    assert first["scored"] == 1
    assert first["created"] == 1
    assert first["skipped_existing"] == 0
    assert first["direction_hits"] == 1
    assert second["created"] == 0
    assert second["updated"] == 1
    assert third["candidate_rows"] == 1
    assert third["skipped_existing"] == 1
    assert third["scored"] == 0
    assert third["created"] == 0
    assert third["updated"] == 0
    scores = db.recent_replay_model_scores(limit=10)
    assert len(scores) == 1
    assert scores[0]["predicted_direction"] == "up"
    assert scores[0]["actual_direction"] == "up"
    assert scores[0]["direction_hit"] == 1


def test_replay_score_summary_handles_misses_and_errors(tmp_path: Path) -> None:
    db = AnalysisDatabase(tmp_path / "score-summary.db")
    positive = _add_replay_event(
        db,
        "MU",
        "mu-positive",
        "2026-06-24T20:02:01+00:00",
        content="Micron beats estimates and reports strong growth.",
    )
    negative = _add_replay_event(
        db,
        "NVDA",
        "nvda-negative",
        "2026-06-25T20:02:01+00:00",
        content="Nvidia cuts outlook amid weak demand and uncertainty.",
    )
    db.upsert_replay_market_outcome(
        _outcome(positive, source="yahoo_chart_daily_adjclose", horizon="1D", raw=0.03, excess=0.02)
    )
    db.upsert_replay_market_outcome(
        _outcome(negative, source="yahoo_chart_daily_adjclose", horizon="1D", raw=0.02, excess=0.01)
    )

    result = ReplayScoringEngine(db).build(price_source="yahoo_chart_daily_adjclose", horizon="1D", limit=10)
    summary = summarize_scores(db.recent_replay_model_scores(limit=10))

    assert result["scored"] == 2
    assert summary["rows"] == 2
    assert summary["evaluated"] == 2
    assert summary["direction_hits"] == 1
    assert summary["direction_accuracy"] == 0.5
    assert summary["avg_absolute_raw_error"] is not None


def test_replay_score_filters_by_symbol(tmp_path: Path) -> None:
    db = AnalysisDatabase(tmp_path / "score-filter.db")
    mu = _add_replay_event(
        db,
        "MU",
        "mu-filter",
        "2026-06-24T20:02:01+00:00",
        content="Micron raises guidance.",
    )
    nvda = _add_replay_event(
        db,
        "NVDA",
        "nvda-filter",
        "2026-06-25T20:02:01+00:00",
        content="Nvidia raises guidance.",
    )
    db.upsert_replay_market_outcome(
        _outcome(mu, source="ibkr_historical_trades_daily", horizon="1D", raw=0.01, excess=0.0)
    )
    db.upsert_replay_market_outcome(
        _outcome(nvda, source="ibkr_historical_trades_daily", horizon="1D", raw=0.01, excess=0.0)
    )

    ReplayScoringEngine(db).build(symbol="MU", price_source="ibkr_historical_trades_daily", limit=10)

    scores = db.recent_replay_model_scores(limit=10)
    assert len(scores) == 1
    assert scores[0]["symbol"] == "MU"


def test_replay_score_leaderboard_ranks_model_groups(tmp_path: Path) -> None:
    db = AnalysisDatabase(tmp_path / "leaderboard.db")
    event = _add_replay_event(
        db,
        "MU",
        "leaderboard",
        "2026-06-24T20:02:01+00:00",
        content="Micron raises guidance on strong growth.",
    )
    db.upsert_replay_market_outcome(
        _outcome(event, source="yahoo_chart_daily_adjclose", horizon="1D", raw=0.02, excess=0.01)
    )
    row = db.replay_dataset_export_rows(limit=1)[0]
    good = HeuristicReplayScorer().score(row)
    bad = replace(
        good,
        scorer_name="contrarian",
        model_name="contrarian-v1",
        scorer_version="contrarian-score-v1",
        predicted_direction="down",
        expected_return=-0.01,
        direction_hit=0,
        absolute_raw_error=0.03,
        absolute_excess_error=0.02,
    )
    db.upsert_replay_model_score(bad)
    db.upsert_replay_model_score(good)

    rows = db.replay_score_leaderboard_rows(limit=10)

    assert len(rows) == 2
    assert rows[0]["model_name"] == "heuristic-replay-v1"
    assert rows[0]["direction_accuracy"] == 1.0
    assert rows[1]["model_name"] == "contrarian-v1"
    assert rows[1]["direction_accuracy"] == 0.0


def test_replay_score_leaderboard_filters_and_min_rows(tmp_path: Path) -> None:
    db = AnalysisDatabase(tmp_path / "leaderboard-filter.db")
    mu = _add_replay_event(
        db,
        "MU",
        "leaderboard-mu",
        "2026-06-24T20:02:01+00:00",
        content="Micron raises guidance.",
    )
    nvda = _add_replay_event(
        db,
        "NVDA",
        "leaderboard-nvda",
        "2026-06-25T20:02:01+00:00",
        content="Nvidia raises guidance.",
    )
    db.upsert_replay_market_outcome(
        _outcome(mu, source="ibkr_historical_trades_daily", horizon="1D", raw=0.01, excess=0.0)
    )
    db.upsert_replay_market_outcome(
        _outcome(nvda, source="yahoo_chart_daily_adjclose", horizon="1D", raw=0.01, excess=0.0)
    )
    ReplayScoringEngine(db).build(limit=10)

    ibkr_rows = db.replay_score_leaderboard_rows(price_source="ibkr_historical_trades_daily", min_rows=1)
    sparse_rows = db.replay_score_leaderboard_rows(price_source="ibkr_historical_trades_daily", min_rows=2)

    assert len(ibkr_rows) == 1
    assert ibkr_rows[0]["price_source"] == "ibkr_historical_trades_daily"
    assert sparse_rows == []


def test_replay_score_leaderboard_markdown_report_shape(tmp_path: Path) -> None:
    db = AnalysisDatabase(tmp_path / "leaderboard-markdown.db")
    event = _add_replay_event(
        db,
        "MU",
        "leaderboard-markdown",
        "2026-06-24T20:02:01+00:00",
        content="Micron beats expectations and raises outlook.",
    )
    db.upsert_replay_market_outcome(
        _outcome(event, source="yahoo_chart_daily_adjclose", horizon="1D", raw=0.04, excess=0.03)
    )
    ReplayScoringEngine(db).build(limit=10)

    rows = db.replay_score_leaderboard_rows(limit=10)
    report = leaderboard_markdown(rows, filters={"symbol": "MU"})

    assert "# Replay Model Leaderboard" in report
    assert "| Rank | Scorer | Model |" in report
    assert "heuristic-replay-v1" in report
    assert "`symbol`: `MU`" in report


def test_openai_replay_scorer_uses_structured_output_without_outcome_label(tmp_path: Path) -> None:
    db = AnalysisDatabase(tmp_path / "openai-score.db")
    event = _add_replay_event(
        db,
        "MU",
        "openai-score",
        "2026-06-24T20:02:01+00:00",
        content="Micron reports stronger revenue and raises outlook.",
    )
    db.upsert_replay_market_outcome(
        _outcome(event, source="yahoo_chart_daily_adjclose", horizon="1D", raw=0.03, excess=0.02)
    )
    row = db.replay_dataset_export_rows(limit=1)[0]
    row["session"] = "regular"
    parsed = OpenAIReplayPrediction(
        predicted_direction="up",
        expected_return=0.02,
        bear_return=-0.01,
        bull_return=0.05,
        up_probability=0.62,
        flat_probability=0.18,
        down_probability=0.20,
        confidence=0.71,
        rationale="Revenue strength and raised outlook support a positive replay reaction.",
    )

    class FakeResponses:
        def parse(self, **kwargs):
            assert kwargs["model"] == "gpt-test-replay"
            assert kwargs["reasoning"] == {"effort": "low"}
            assert kwargs["text_format"] is OpenAIReplayPrediction
            assert kwargs["store"] is False
            user_payload = json.loads(kwargs["input"][1]["content"])
            assert "raw_return" not in json.dumps(user_payload)
            assert user_payload["symbol"] == "MU"
            assert user_payload["horizon"] == "1D"
            assert "calibration_policy" in user_payload
            assert "flat_threshold" in user_payload["calibration_policy"]
            assert "flat_debias_policy" in user_payload["calibration_policy"]
            return SimpleNamespace(
                id="resp_replay_test",
                output=[SimpleNamespace(type="message", content=[SimpleNamespace(type="output_text", parsed=parsed)])],
            )

    scorer = OpenAIReplayScorer(
        api_key="test",
        model="gpt-test-replay",
        timeout_seconds=30,
        reasoning_effort="low",
        client=SimpleNamespace(responses=FakeResponses()),
    )
    score = scorer.score(row)

    assert score.scorer_name == "openai"
    assert score.model_name == "gpt-test-replay"
    assert score.scorer_version == "openai-replay-score-v6"
    assert score.predicted_direction == "up"
    assert score.actual_direction == "up"
    assert score.direction_hit == 1
    assert "resp_replay_test" in score.raw_json


def test_openai_replay_scorer_persists_and_competes_in_leaderboard(tmp_path: Path) -> None:
    db = AnalysisDatabase(tmp_path / "openai-leaderboard.db")
    event = _add_replay_event(
        db,
        "MU",
        "openai-leaderboard",
        "2026-06-24T20:02:01+00:00",
        content="Micron raises outlook on stronger demand.",
    )
    db.upsert_replay_market_outcome(
        _outcome(event, source="yahoo_chart_daily_adjclose", horizon="1D", raw=0.02, excess=0.01)
    )
    parsed = OpenAIReplayPrediction(
        predicted_direction="up",
        expected_return=0.015,
        bear_return=-0.01,
        bull_return=0.04,
        up_probability=0.60,
        flat_probability=0.20,
        down_probability=0.20,
        confidence=0.66,
        rationale="Raised outlook supports upside.",
    )

    class FakeResponses:
        def parse(self, **kwargs):
            return SimpleNamespace(
                id="resp_replay_leaderboard",
                output=[SimpleNamespace(type="message", content=[SimpleNamespace(type="output_text", parsed=parsed)])],
            )

    scorer = OpenAIReplayScorer(
        api_key="test",
        model="gpt-test-replay",
        timeout_seconds=30,
        scorer_version="openai-replay-score-v5",
        client=SimpleNamespace(responses=FakeResponses()),
    )

    result = ReplayScoringEngine(db, scorer).build(price_source="yahoo_chart_daily_adjclose", horizon="1D", limit=10)
    rows = db.replay_score_leaderboard_rows(limit=10)

    assert result["created"] == 1
    assert rows[0]["scorer_name"] == "openai"
    assert rows[0]["model_name"] == "gpt-test-replay"
    assert rows[0]["direction_accuracy"] == 1.0


def test_openai_replay_scorer_v3_debiases_low_conviction_flat_outputs(tmp_path: Path) -> None:
    db = AnalysisDatabase(tmp_path / "openai-debias.db")
    event = _add_replay_event(
        db,
        "MU",
        "openai-debias",
        "2026-06-24T20:02:01+00:00",
        content="Micron reports stronger revenue and raises outlook.",
    )
    db.upsert_replay_market_outcome(
        _outcome(event, source="yahoo_chart_daily_adjclose", horizon="1D", raw=0.02, excess=0.01)
    )
    row = db.replay_dataset_export_rows(limit=1)[0]
    row["session"] = "regular"
    parsed = OpenAIReplayPrediction(
        predicted_direction="flat",
        expected_return=0.0008,
        bear_return=-0.01,
        bull_return=0.012,
        up_probability=0.33,
        flat_probability=0.39,
        down_probability=0.28,
        confidence=0.34,
        rationale="The release is positive but short-term reaction is uncertain.",
    )

    class FakeResponses:
        def parse(self, **kwargs):
            return SimpleNamespace(
                id="resp_replay_debias",
                output=[SimpleNamespace(type="message", content=[SimpleNamespace(type="output_text", parsed=parsed)])],
            )

    scorer = OpenAIReplayScorer(
        api_key="test",
        model="gpt-test-replay",
        timeout_seconds=30,
        scorer_version="openai-replay-score-v3",
        client=SimpleNamespace(responses=FakeResponses()),
    )
    score = scorer.score(row)
    raw = json.loads(score.raw_json)

    assert score.scorer_version == "openai-replay-score-v3"
    assert score.predicted_direction == "up"
    assert score.direction_hit == 1
    assert score.expected_return >= 0.004
    assert raw["original_prediction"]["predicted_direction"] == "flat"
    assert raw["debias"]["applied"] is True


def test_openai_replay_scorer_v4_uses_intraday_expected_return_tiebreak(tmp_path: Path) -> None:
    db = AnalysisDatabase(tmp_path / "openai-intraday-v4.db")
    event = _add_replay_event(
        db,
        "MU",
        "openai-intraday-v4",
        "2026-06-24T20:02:01+00:00",
        content="Micron reports stronger revenue and raises outlook.",
    )
    db.upsert_replay_market_outcome(
        _outcome(
            event,
            source="ibkr_historical_trades_5_mins_all_sessions",
            horizon="30M",
            raw=0.004,
            excess=0.003,
        )
    )
    row = db.replay_dataset_export_rows(limit=1)[0]
    parsed = OpenAIReplayPrediction(
        predicted_direction="flat",
        expected_return=0.0005,
        bear_return=-0.006,
        bull_return=0.007,
        up_probability=0.28,
        flat_probability=0.42,
        down_probability=0.30,
        confidence=0.34,
        rationale="The release is positive, but immediate intraday reaction is uncertain.",
    )

    class FakeResponses:
        def parse(self, **kwargs):
            user_payload = json.loads(kwargs["input"][1]["content"])
            assert user_payload["horizon"] == "30M"
            assert "intraday_policy" in user_payload["calibration_policy"]
            return SimpleNamespace(
                id="resp_replay_intraday_v4",
                output=[SimpleNamespace(type="message", content=[SimpleNamespace(type="output_text", parsed=parsed)])],
            )

    scorer = OpenAIReplayScorer(
        api_key="test",
        model="gpt-test-replay",
        timeout_seconds=30,
        scorer_version="openai-replay-score-v4",
        client=SimpleNamespace(responses=FakeResponses()),
    )
    score = scorer.score(row)
    raw = json.loads(score.raw_json)

    assert score.scorer_version == "openai-replay-score-v4"
    assert score.predicted_direction == "up"
    assert score.direction_hit == 1
    assert score.expected_return >= 0.0012
    assert score.confidence <= 0.42
    assert raw["debias"]["method"] == "intraday_expected_return_tiebreak_v1"
    assert raw["prompt_profile"] == "market-reaction-intraday-tuned-v4"


def test_openai_replay_scorer_v5_preserves_weak_intraday_flat_outputs(tmp_path: Path) -> None:
    db = AnalysisDatabase(tmp_path / "openai-intraday-v5.db")
    event = _add_replay_event(
        db,
        "MU",
        "openai-intraday-v5",
        "2026-06-24T20:02:01+00:00",
        content="Micron reports stronger revenue and raises outlook.",
    )
    db.upsert_replay_market_outcome(
        _outcome(
            event,
            source="ibkr_historical_trades_5_mins_all_sessions",
            horizon="30M",
            raw=0.0,
            excess=0.0,
        )
    )
    row = db.replay_dataset_export_rows(limit=1)[0]
    row["session"] = "regular"
    parsed = OpenAIReplayPrediction(
        predicted_direction="flat",
        expected_return=0.0004,
        bear_return=-0.006,
        bull_return=0.007,
        up_probability=0.28,
        flat_probability=0.46,
        down_probability=0.26,
        confidence=0.34,
        rationale="The release is positive, but immediate intraday reaction is uncertain.",
    )

    class FakeResponses:
        def parse(self, **kwargs):
            user_payload = json.loads(kwargs["input"][1]["content"])
            assert "intraday_policy" in user_payload["calibration_policy"]
            return SimpleNamespace(
                id="resp_replay_intraday_v5",
                output=[SimpleNamespace(type="message", content=[SimpleNamespace(type="output_text", parsed=parsed)])],
            )

    scorer = OpenAIReplayScorer(
        api_key="test",
        model="gpt-test-replay",
        timeout_seconds=30,
        scorer_version="openai-replay-score-v5",
        client=SimpleNamespace(responses=FakeResponses()),
    )
    score = scorer.score(row)
    raw = json.loads(score.raw_json)

    assert score.scorer_version == "openai-replay-score-v5"
    assert score.predicted_direction == "flat"
    assert score.direction_hit == 1
    assert score.confidence <= 0.36
    assert raw["debias"]["method"] == "intraday_balanced_flat_retention_v1"
    assert raw["prompt_profile"] == "market-reaction-intraday-balanced-v5"


def test_openai_replay_scorer_v6_restores_weak_intraday_up_to_flat(tmp_path: Path) -> None:
    db = AnalysisDatabase(tmp_path / "openai-intraday-v6.db")
    event = _add_replay_event(
        db,
        "QCOM",
        "openai-intraday-v6",
        "2026-04-29T20:02:20+00:00",
        content="Qualcomm reports mixed results with cautious near-term commentary.",
    )
    db.upsert_replay_market_outcome(
        _outcome(
            event,
            source="ibkr_historical_trades_5_mins_all_sessions",
            horizon="30M",
            raw=0.0,
            excess=0.0,
        )
    )
    row = db.replay_dataset_export_rows(limit=1)[0]
    row["session"] = "after_hours"
    parsed = OpenAIReplayPrediction(
        predicted_direction="flat",
        expected_return=0.0004,
        bear_return=-0.006,
        bull_return=0.007,
        up_probability=0.28,
        flat_probability=0.46,
        down_probability=0.26,
        confidence=0.34,
        rationale="The release is mixed and the immediate intraday reaction is uncertain.",
    )

    class FakeResponses:
        def parse(self, **kwargs):
            user_payload = json.loads(kwargs["input"][1]["content"])
            assert user_payload["horizon"] == "30M"
            assert "intraday_policy" in user_payload["calibration_policy"]
            return SimpleNamespace(
                id="resp_replay_intraday_v6",
                output=[SimpleNamespace(type="message", content=[SimpleNamespace(type="output_text", parsed=parsed)])],
            )

    scorer = OpenAIReplayScorer(
        api_key="test",
        model="gpt-test-replay",
        timeout_seconds=30,
        scorer_version="openai-replay-score-v6",
        client=SimpleNamespace(responses=FakeResponses()),
    )
    score = scorer.score(row)
    raw = json.loads(score.raw_json)

    assert score.scorer_version == "openai-replay-score-v6"
    assert score.predicted_direction == "flat"
    assert score.direction_hit == 1
    assert score.confidence <= 0.36
    assert raw["debias"]["method"] == "intraday_v6_weak_up_to_flat_v1"
    assert raw["debias"]["v5"]["method"] == "intraday_session_horizon_prior_v1"
    assert raw["prompt_profile"] == "market-reaction-intraday-upbias-reduced-v6"


def test_openai_replay_scorer_v7_recovers_raw_flat_positive_after_hours_2h_up(tmp_path: Path) -> None:
    db = AnalysisDatabase(tmp_path / "openai-intraday-v7-after-hours.db")
    event = _add_replay_event(
        db,
        "SNOW",
        "openai-intraday-v7-after-hours",
        "2026-05-27T20:07:20+00:00",
        content="Snowflake reports mixed results but highlights improving product demand.",
    )
    db.upsert_replay_market_outcome(
        _outcome(
            event,
            source="ibkr_historical_trades_5_mins_all_sessions",
            horizon="2H",
            raw=0.02,
            excess=0.015,
        )
    )
    row = db.replay_dataset_export_rows(limit=1)[0]
    row["session"] = "after_hours"
    parsed = OpenAIReplayPrediction(
        predicted_direction="flat",
        expected_return=0.0004,
        bear_return=-0.006,
        bull_return=0.007,
        up_probability=0.31,
        flat_probability=0.40,
        down_probability=0.29,
        confidence=0.34,
        rationale="The release is mixed, but the raw short-horizon tilt is slightly positive.",
    )

    class FakeResponses:
        def parse(self, **kwargs):
            user_payload = json.loads(kwargs["input"][1]["content"])
            assert user_payload["horizon"] == "2H"
            assert "intraday_policy" in user_payload["calibration_policy"]
            return SimpleNamespace(
                id="resp_replay_intraday_v7_after_hours",
                output=[SimpleNamespace(type="message", content=[SimpleNamespace(type="output_text", parsed=parsed)])],
            )

    scorer = OpenAIReplayScorer(
        api_key="test",
        model="gpt-test-replay",
        timeout_seconds=30,
        scorer_version="openai-replay-score-v7",
        client=SimpleNamespace(responses=FakeResponses()),
    )
    score = scorer.score(row)
    raw = json.loads(score.raw_json)

    assert score.scorer_version == "openai-replay-score-v7"
    assert score.predicted_direction == "up"
    assert score.direction_hit == 1
    assert raw["debias"]["method"] == "intraday_v7_after_hours_2h_raw_flat_up_recovery_v1"
    assert raw["debias"]["v6"]["method"] == "intraday_v6_kept_non_up_v5_output"
    assert raw["prompt_profile"] == "market-reaction-intraday-up-recovery-v7"


def test_openai_replay_scorer_v7_recovers_regular_30m_flat_retention_up(tmp_path: Path) -> None:
    db = AnalysisDatabase(tmp_path / "openai-intraday-v7-regular.db")
    event = _add_replay_event(
        db,
        "INTC",
        "openai-intraday-v7-regular",
        "2026-04-23T16:07:02+00:00",
        content="Intel reports mixed results with a slight positive immediate read.",
    )
    db.upsert_replay_market_outcome(
        _outcome(
            event,
            source="ibkr_historical_trades_5_mins_all_sessions",
            horizon="30M",
            raw=0.01,
            excess=0.008,
        )
    )
    row = db.replay_dataset_export_rows(limit=1)[0]
    row["session"] = "regular"
    parsed = OpenAIReplayPrediction(
        predicted_direction="flat",
        expected_return=0.0004,
        bear_return=-0.006,
        bull_return=0.007,
        up_probability=0.31,
        flat_probability=0.42,
        down_probability=0.27,
        confidence=0.34,
        rationale="The release is mixed, but the raw short-horizon tilt is slightly positive.",
    )

    class FakeResponses:
        def parse(self, **kwargs):
            user_payload = json.loads(kwargs["input"][1]["content"])
            assert user_payload["horizon"] == "30M"
            assert "intraday_policy" in user_payload["calibration_policy"]
            return SimpleNamespace(
                id="resp_replay_intraday_v7_regular",
                output=[SimpleNamespace(type="message", content=[SimpleNamespace(type="output_text", parsed=parsed)])],
            )

    scorer = OpenAIReplayScorer(
        api_key="test",
        model="gpt-test-replay",
        timeout_seconds=30,
        scorer_version="openai-replay-score-v7",
        client=SimpleNamespace(responses=FakeResponses()),
    )
    score = scorer.score(row)
    raw = json.loads(score.raw_json)

    assert score.scorer_version == "openai-replay-score-v7"
    assert score.predicted_direction == "up"
    assert score.direction_hit == 1
    assert raw["debias"]["method"] == "intraday_v7_regular_30m_raw_flat_up_recovery_v1"
    assert raw["debias"]["v6"]["v5"]["method"] == "intraday_balanced_flat_retention_v1"
    assert raw["prompt_profile"] == "market-reaction-intraday-up-recovery-v7"


def test_openai_replay_scorer_v8_neutralizes_mixed_low_edge_intraday_up(tmp_path: Path) -> None:
    db = AnalysisDatabase(tmp_path / "openai-intraday-v8-neutral.db")
    event = _add_replay_event(
        db,
        "SNOW",
        "openai-intraday-v8-neutral",
        "2026-05-27T20:07:20+00:00",
        content=(
            "Snowflake reports strong growth and beats expectations, but management "
            "also highlights cautious demand, margin pressure, and capex risk."
        ),
    )
    db.upsert_replay_market_outcome(
        _outcome(
            event,
            source="ibkr_historical_trades_5_mins_all_sessions",
            horizon="2H",
            raw=0.0,
            excess=0.0,
        )
    )
    row = db.replay_dataset_export_rows(limit=1)[0]
    row["session"] = "after_hours"
    parsed = OpenAIReplayPrediction(
        predicted_direction="flat",
        expected_return=0.0004,
        bear_return=-0.006,
        bull_return=0.007,
        up_probability=0.31,
        flat_probability=0.40,
        down_probability=0.29,
        confidence=0.34,
        rationale="The release is mixed, but the raw short-horizon tilt is slightly positive.",
    )

    class FakeResponses:
        def parse(self, **kwargs):
            user_payload = json.loads(kwargs["input"][1]["content"])
            assert "flat_threshold" in user_payload["calibration_policy"]
            return SimpleNamespace(
                id="resp_replay_intraday_v8_neutral",
                output=[SimpleNamespace(type="message", content=[SimpleNamespace(type="output_text", parsed=parsed)])],
            )

    scorer = OpenAIReplayScorer(
        api_key="test",
        model="gpt-test-replay",
        timeout_seconds=30,
        scorer_version="openai-replay-score-v8",
        client=SimpleNamespace(responses=FakeResponses()),
    )
    score = scorer.score(row)
    raw = json.loads(score.raw_json)

    assert score.scorer_version == "openai-replay-score-v8"
    assert score.predicted_direction == "flat"
    assert score.direction_hit == 1
    assert raw["debias"]["method"] == "intraday_v8_directional_to_neutral_v1"
    assert raw["debias"]["v7"]["method"] == "intraday_v7_after_hours_2h_raw_flat_up_recovery_v1"
    assert raw["prompt_profile"] == "market-reaction-intraday-neutral-edge-v8"


def test_openai_replay_scorer_v8_keeps_clear_directional_edge(tmp_path: Path) -> None:
    db = AnalysisDatabase(tmp_path / "openai-intraday-v8-direction.db")
    event = _add_replay_event(
        db,
        "GOOGL",
        "openai-intraday-v8-direction",
        "2026-04-29T20:02:20+00:00",
        content="Alphabet reports a strong beat, accelerating growth, and raised outlook.",
    )
    db.upsert_replay_market_outcome(
        _outcome(
            event,
            source="ibkr_historical_trades_5_mins_all_sessions",
            horizon="2H",
            raw=0.025,
            excess=0.020,
        )
    )
    row = db.replay_dataset_export_rows(limit=1)[0]
    row["session"] = "after_hours"
    parsed = OpenAIReplayPrediction(
        predicted_direction="up",
        expected_return=0.012,
        bear_return=-0.008,
        bull_return=0.035,
        up_probability=0.55,
        flat_probability=0.25,
        down_probability=0.20,
        confidence=0.55,
        rationale="The release has a clear positive surprise and raised outlook.",
    )

    class FakeResponses:
        def parse(self, **kwargs):
            return SimpleNamespace(
                id="resp_replay_intraday_v8_direction",
                output=[SimpleNamespace(type="message", content=[SimpleNamespace(type="output_text", parsed=parsed)])],
            )

    scorer = OpenAIReplayScorer(
        api_key="test",
        model="gpt-test-replay",
        timeout_seconds=30,
        scorer_version="openai-replay-score-v8",
        client=SimpleNamespace(responses=FakeResponses()),
    )
    score = scorer.score(row)
    raw = json.loads(score.raw_json)

    assert score.scorer_version == "openai-replay-score-v8"
    assert score.predicted_direction == "up"
    assert score.direction_hit == 1
    assert raw["debias"]["method"] == "intraday_v8_kept_v7_directional_output"
    assert raw["prompt_profile"] == "market-reaction-intraday-neutral-edge-v8"


def test_openai_replay_scorer_v9_restores_low_edge_2h_up_to_flat(tmp_path: Path) -> None:
    db = AnalysisDatabase(tmp_path / "openai-intraday-v9-downrisk.db")
    event = _add_replay_event(
        db,
        "ADBE",
        "openai-intraday-v9-downrisk",
        "2026-06-17T20:04:30+00:00",
        content="Adobe reports in-line results with modest growth and mixed demand commentary.",
    )
    db.upsert_replay_market_outcome(
        _outcome(
            event,
            source="ibkr_historical_trades_5_mins_all_sessions",
            horizon="2H",
            raw=0.0,
            excess=0.0,
        )
    )
    row = db.replay_dataset_export_rows(limit=1)[0]
    row["session"] = "regular"
    parsed = OpenAIReplayPrediction(
        predicted_direction="flat",
        expected_return=0.0005,
        bear_return=-0.006,
        bull_return=0.008,
        up_probability=0.33,
        flat_probability=0.38,
        down_probability=0.29,
        confidence=0.35,
        rationale="The release is mixed, with a small positive short-horizon tilt.",
    )

    class FakeResponses:
        def parse(self, **kwargs):
            return SimpleNamespace(
                id="resp_replay_intraday_v9_downrisk",
                output=[SimpleNamespace(type="message", content=[SimpleNamespace(type="output_text", parsed=parsed)])],
            )

    scorer = OpenAIReplayScorer(
        api_key="test",
        model="gpt-test-replay",
        timeout_seconds=30,
        scorer_version="openai-replay-score-v9",
        client=SimpleNamespace(responses=FakeResponses()),
    )
    score = scorer.score(row)
    raw = json.loads(score.raw_json)

    assert score.scorer_version == "openai-replay-score-v9"
    assert score.predicted_direction == "flat"
    assert score.direction_hit == 1
    assert raw["debias"]["method"] == "intraday_v9_2h_low_edge_up_to_flat_v1"
    assert raw["debias"]["v6"]["method"] == "intraday_v6_kept_supported_up_v5_output"
    assert raw["prompt_profile"] == "market-reaction-intraday-downrisk-v9"


def test_openai_replay_scorer_v9_keeps_30m_up_recovery(tmp_path: Path) -> None:
    db = AnalysisDatabase(tmp_path / "openai-intraday-v9-30m.db")
    event = _add_replay_event(
        db,
        "CRM",
        "openai-intraday-v9-30m",
        "2026-05-28T14:34:20+00:00",
        content="Salesforce reports steady growth and a slightly better outlook.",
    )
    db.upsert_replay_market_outcome(
        _outcome(
            event,
            source="ibkr_historical_trades_5_mins_all_sessions",
            horizon="30M",
            raw=0.012,
            excess=0.010,
        )
    )
    row = db.replay_dataset_export_rows(limit=1)[0]
    row["session"] = "regular"
    parsed = OpenAIReplayPrediction(
        predicted_direction="flat",
        expected_return=0.0005,
        bear_return=-0.006,
        bull_return=0.008,
        up_probability=0.33,
        flat_probability=0.38,
        down_probability=0.29,
        confidence=0.35,
        rationale="The release is mixed, with a small positive short-horizon tilt.",
    )

    class FakeResponses:
        def parse(self, **kwargs):
            return SimpleNamespace(
                id="resp_replay_intraday_v9_30m",
                output=[SimpleNamespace(type="message", content=[SimpleNamespace(type="output_text", parsed=parsed)])],
            )

    scorer = OpenAIReplayScorer(
        api_key="test",
        model="gpt-test-replay",
        timeout_seconds=30,
        scorer_version="openai-replay-score-v9",
        client=SimpleNamespace(responses=FakeResponses()),
    )
    score = scorer.score(row)
    raw = json.loads(score.raw_json)

    assert score.scorer_version == "openai-replay-score-v9"
    assert score.predicted_direction == "up"
    assert score.direction_hit == 1
    assert raw["debias"]["method"] == "intraday_v9_kept_v6_directional_output"
    assert raw["debias"]["v6"]["method"] == "intraday_v6_kept_supported_up_v5_output"
    assert raw["prompt_profile"] == "market-reaction-intraday-downrisk-v9"


def test_openai_replay_scorer_v10_daily_up_to_downrisk(tmp_path: Path) -> None:
    db = AnalysisDatabase(tmp_path / "openai-daily-v10-downrisk.db")
    event = _add_replay_event(
        db,
        "XOM",
        "openai-daily-v10-downrisk",
        "2026-04-25T12:30:00+00:00",
        content="Exxon reports superficially positive results, but margin pressure and mixed outlook commentary.",
    )
    db.upsert_replay_market_outcome(
        _outcome(
            event,
            source="yahoo_chart_daily_adjclose",
            horizon="1D",
            raw=-0.025,
            excess=-0.020,
        )
    )
    row = db.replay_dataset_export_rows(limit=1)[0]
    row["session"] = "pre_market"
    parsed = OpenAIReplayPrediction(
        predicted_direction="up",
        expected_return=0.012,
        bear_return=-0.012,
        bull_return=0.030,
        up_probability=0.48,
        flat_probability=0.28,
        down_probability=0.24,
        confidence=0.46,
        rationale="Headline results appear positive, but risks remain meaningful.",
    )

    class FakeResponses:
        def parse(self, **kwargs):
            system_prompt = kwargs["input"][0]["content"]
            assert "For 1D/5D horizons, explicitly consider down-risk" in system_prompt
            return SimpleNamespace(
                id="resp_replay_daily_v10_downrisk",
                output=[SimpleNamespace(type="message", content=[SimpleNamespace(type="output_text", parsed=parsed)])],
            )

    scorer = OpenAIReplayScorer(
        api_key="test",
        model="gpt-test-replay",
        timeout_seconds=30,
        scorer_version="openai-replay-score-v10",
        client=SimpleNamespace(responses=FakeResponses()),
    )
    score = scorer.score(row)
    raw = json.loads(score.raw_json)

    assert score.scorer_version == "openai-replay-score-v10"
    assert score.predicted_direction == "down"
    assert score.direction_hit == 1
    assert score.expected_return < 0
    assert raw["debias"]["method"] == "daily_v10_up_to_downrisk_v1"
    assert raw["prompt_profile"] == "market-reaction-daily-downrisk-neutral-v10"


def test_openai_replay_scorer_v10_keeps_clear_daily_up(tmp_path: Path) -> None:
    db = AnalysisDatabase(tmp_path / "openai-daily-v10-clear-up.db")
    event = _add_replay_event(
        db,
        "MU",
        "openai-daily-v10-clear-up",
        "2026-06-24T20:02:01+00:00",
        content="Micron reports strong revenue, stronger margins, and raises outlook.",
    )
    db.upsert_replay_market_outcome(
        _outcome(
            event,
            source="yahoo_chart_daily_adjclose",
            horizon="5D",
            raw=0.08,
            excess=0.07,
        )
    )
    row = db.replay_dataset_export_rows(limit=1)[0]
    row["session"] = "after_hours"
    parsed = OpenAIReplayPrediction(
        predicted_direction="up",
        expected_return=0.045,
        bear_return=-0.010,
        bull_return=0.090,
        up_probability=0.68,
        flat_probability=0.20,
        down_probability=0.12,
        confidence=0.64,
        rationale="Strong results and raised outlook create a clear positive daily edge.",
    )

    class FakeResponses:
        def parse(self, **kwargs):
            return SimpleNamespace(
                id="resp_replay_daily_v10_clear_up",
                output=[SimpleNamespace(type="message", content=[SimpleNamespace(type="output_text", parsed=parsed)])],
            )

    scorer = OpenAIReplayScorer(
        api_key="test",
        model="gpt-test-replay",
        timeout_seconds=30,
        scorer_version="openai-replay-score-v10",
        client=SimpleNamespace(responses=FakeResponses()),
    )
    score = scorer.score(row)
    raw = json.loads(score.raw_json)

    assert score.scorer_version == "openai-replay-score-v10"
    assert score.predicted_direction == "up"
    assert score.direction_hit == 1
    assert raw["debias"]["method"] == "daily_v10_kept_v6_directional_output"
    assert raw["prompt_profile"] == "market-reaction-daily-downrisk-neutral-v10"


def test_openai_replay_scorer_v11_recovers_strong_daily_up_from_v10_downrisk(tmp_path: Path) -> None:
    db = AnalysisDatabase(tmp_path / "openai-daily-v11-strong-up.db")
    event = _add_replay_event(
        db,
        "SNOW",
        "openai-daily-v11-strong-up",
        "2025-08-27T20:06:52+00:00",
        content="Snowflake reports strong demand, raises guidance, and expands margins.",
    )
    db.upsert_replay_market_outcome(
        _outcome(
            event,
            source="yahoo_chart_daily_adjclose",
            horizon="1D",
            raw=0.11,
            excess=0.10,
        )
    )
    row = db.replay_dataset_export_rows(limit=1)[0]
    row["session"] = "after_hours"
    parsed = OpenAIReplayPrediction(
        predicted_direction="up",
        expected_return=0.015,
        bear_return=-0.012,
        bull_return=0.045,
        up_probability=0.54,
        flat_probability=0.21,
        down_probability=0.25,
        confidence=0.60,
        rationale="Strong demand and raised guide support upside despite a nontrivial risk bucket.",
    )

    class FakeResponses:
        def parse(self, **kwargs):
            system_prompt = kwargs["input"][0]["content"]
            assert "strong-up recovery" in system_prompt
            return SimpleNamespace(
                id="resp_replay_daily_v11_strong_up",
                output=[SimpleNamespace(type="message", content=[SimpleNamespace(type="output_text", parsed=parsed)])],
            )

    scorer = OpenAIReplayScorer(
        api_key="test",
        model="gpt-test-replay",
        timeout_seconds=30,
        scorer_version="openai-replay-score-v11",
        client=SimpleNamespace(responses=FakeResponses()),
    )
    score = scorer.score(row)
    raw = json.loads(score.raw_json)

    assert score.scorer_version == "openai-replay-score-v11"
    assert score.predicted_direction == "up"
    assert score.direction_hit == 1
    assert raw["debias"]["method"] == "daily_v11_strong_up_recovery_v1"
    assert raw["debias"]["v10"]["method"] == "daily_v10_up_to_downrisk_v1"
    assert raw["prompt_profile"] == "market-reaction-daily-strongup-flat-v11"


def test_openai_replay_scorer_v11_restores_low_edge_daily_down_to_flat(tmp_path: Path) -> None:
    db = AnalysisDatabase(tmp_path / "openai-daily-v11-flat.db")
    event = _add_replay_event(
        db,
        "IBM",
        "openai-daily-v11-flat",
        "2026-04-22T20:10:35+00:00",
        content="IBM reports mixed results with offsetting positives and negatives.",
    )
    db.upsert_replay_market_outcome(
        _outcome(
            event,
            source="yahoo_chart_daily_adjclose",
            horizon="1D",
            raw=0.0,
            excess=0.0,
        )
    )
    row = db.replay_dataset_export_rows(limit=1)[0]
    row["session"] = "after_hours"
    parsed = OpenAIReplayPrediction(
        predicted_direction="down",
        expected_return=-0.004,
        bear_return=-0.014,
        bull_return=0.006,
        up_probability=0.29,
        flat_probability=0.41,
        down_probability=0.30,
        confidence=0.37,
        rationale="Mixed results leave only a tiny negative edge.",
    )

    class FakeResponses:
        def parse(self, **kwargs):
            return SimpleNamespace(
                id="resp_replay_daily_v11_flat",
                output=[SimpleNamespace(type="message", content=[SimpleNamespace(type="output_text", parsed=parsed)])],
            )

    scorer = OpenAIReplayScorer(
        api_key="test",
        model="gpt-test-replay",
        timeout_seconds=30,
        scorer_version="openai-replay-score-v11",
        client=SimpleNamespace(responses=FakeResponses()),
    )
    score = scorer.score(row)
    raw = json.loads(score.raw_json)

    assert score.scorer_version == "openai-replay-score-v11"
    assert score.predicted_direction == "flat"
    assert score.direction_hit == 1
    assert raw["debias"]["method"] == "daily_v11_true_flat_low_edge_v1"
    assert raw["prompt_profile"] == "market-reaction-daily-strongup-flat-v11"


def test_openai_replay_scorer_v12_recovers_only_strict_strong_daily_up(tmp_path: Path) -> None:
    db = AnalysisDatabase(tmp_path / "openai-daily-v12-strong-up.db")
    event = _add_replay_event(
        db,
        "SNOW",
        "openai-daily-v12-strong-up",
        "2025-08-27T20:06:52+00:00",
        content="Snowflake reports clear upside surprise, raises guidance, and expands margins.",
    )
    db.upsert_replay_market_outcome(
        _outcome(
            event,
            source="yahoo_chart_daily_adjclose",
            horizon="1D",
            raw=0.11,
            excess=0.10,
        )
    )
    row = db.replay_dataset_export_rows(limit=1)[0]
    row["session"] = "after_hours"
    parsed = OpenAIReplayPrediction(
        predicted_direction="up",
        expected_return=0.018,
        bear_return=-0.012,
        bull_return=0.050,
        up_probability=0.55,
        flat_probability=0.19,
        down_probability=0.26,
        confidence=0.58,
        rationale="Clear upside surprise and raised guidance support upside despite some risk.",
    )

    class FakeResponses:
        def parse(self, **kwargs):
            system_prompt = kwargs["input"][0]["content"]
            assert "explicitly consider down-risk" in system_prompt
            assert "strong-up recovery" not in system_prompt
            return SimpleNamespace(
                id="resp_replay_daily_v12_strong_up",
                output=[SimpleNamespace(type="message", content=[SimpleNamespace(type="output_text", parsed=parsed)])],
            )

    scorer = OpenAIReplayScorer(
        api_key="test",
        model="gpt-test-replay",
        timeout_seconds=30,
        scorer_version="openai-replay-score-v12",
        client=SimpleNamespace(responses=FakeResponses()),
    )
    score = scorer.score(row)
    raw = json.loads(score.raw_json)

    assert score.scorer_version == "openai-replay-score-v12"
    assert score.predicted_direction == "up"
    assert score.direction_hit == 1
    assert raw["debias"]["method"] == "daily_v12_strong_up_recovery_v2"
    assert raw["debias"]["v10"]["method"] == "daily_v10_up_to_downrisk_v1"
    assert raw["debias"]["trade_action"] == "directional_signal"
    assert raw["prompt_profile"] == "market-reaction-daily-v10-postcalibrated-v12"


def test_openai_replay_scorer_v12_marks_weak_downrisk_as_observe_only(tmp_path: Path) -> None:
    db = AnalysisDatabase(tmp_path / "openai-daily-v12-observe.db")
    event = _add_replay_event(
        db,
        "IBM",
        "openai-daily-v12-observe",
        "2026-04-22T20:10:35+00:00",
        content="IBM reports mixed results with a small positive tilt and several offsetting risks.",
    )
    db.upsert_replay_market_outcome(
        _outcome(
            event,
            source="yahoo_chart_daily_adjclose",
            horizon="5D",
            raw=-0.015,
            excess=-0.012,
        )
    )
    row = db.replay_dataset_export_rows(limit=1)[0]
    row["session"] = "after_hours"
    parsed = OpenAIReplayPrediction(
        predicted_direction="up",
        expected_return=0.006,
        bear_return=-0.012,
        bull_return=0.018,
        up_probability=0.43,
        flat_probability=0.32,
        down_probability=0.25,
        confidence=0.40,
        rationale="Mixed evidence leaves only a weak positive edge with meaningful downside risk.",
    )

    class FakeResponses:
        def parse(self, **kwargs):
            return SimpleNamespace(
                id="resp_replay_daily_v12_observe",
                output=[SimpleNamespace(type="message", content=[SimpleNamespace(type="output_text", parsed=parsed)])],
            )

    scorer = OpenAIReplayScorer(
        api_key="test",
        model="gpt-test-replay",
        timeout_seconds=30,
        scorer_version="openai-replay-score-v12",
        client=SimpleNamespace(responses=FakeResponses()),
    )
    score = scorer.score(row)
    raw = json.loads(score.raw_json)

    assert score.scorer_version == "openai-replay-score-v12"
    assert score.predicted_direction == "down"
    assert score.direction_hit == 1
    assert raw["debias"]["method"] == "daily_v12_kept_v10_output"
    assert raw["debias"]["v10"]["method"] == "daily_v10_up_to_downrisk_v1"
    assert raw["debias"]["observe_only"] is True
    assert raw["debias"]["trade_action"] == "observe_only"
    assert raw["debias"]["observe_only_reason"] == "weak_raw_up_shifted_to_downrisk"
    assert raw["prompt_profile"] == "market-reaction-daily-v10-postcalibrated-v12"


def test_openai_replay_scorer_v13_keeps_weak_downrisk_tradeable(tmp_path: Path) -> None:
    db = AnalysisDatabase(tmp_path / "openai-daily-v13-tradeable.db")
    event = _add_replay_event(
        db,
        "IBM",
        "openai-daily-v13-tradeable",
        "2026-04-22T20:10:35+00:00",
        content="IBM reports mixed results with a small positive tilt and several offsetting risks.",
    )
    db.upsert_replay_market_outcome(
        _outcome(
            event,
            source="yahoo_chart_daily_adjclose",
            horizon="5D",
            raw=-0.015,
            excess=-0.012,
        )
    )
    row = db.replay_dataset_export_rows(limit=1)[0]
    row["session"] = "after_hours"
    parsed = OpenAIReplayPrediction(
        predicted_direction="up",
        expected_return=0.006,
        bear_return=-0.012,
        bull_return=0.018,
        up_probability=0.43,
        flat_probability=0.32,
        down_probability=0.25,
        confidence=0.40,
        rationale="Mixed evidence leaves only a weak positive edge with meaningful downside risk.",
    )

    class FakeResponses:
        def parse(self, **kwargs):
            system_prompt = kwargs["input"][0]["content"]
            assert "explicitly consider down-risk" in system_prompt
            assert "strong-up recovery" not in system_prompt
            return SimpleNamespace(
                id="resp_replay_daily_v13_tradeable",
                output=[SimpleNamespace(type="message", content=[SimpleNamespace(type="output_text", parsed=parsed)])],
            )

    scorer = OpenAIReplayScorer(
        api_key="test",
        model="gpt-test-replay",
        timeout_seconds=30,
        scorer_version="openai-replay-score-v13",
        client=SimpleNamespace(responses=FakeResponses()),
    )
    score = scorer.score(row)
    raw = json.loads(score.raw_json)

    assert score.scorer_version == "openai-replay-score-v13"
    assert score.predicted_direction == "down"
    assert score.direction_hit == 1
    assert raw["debias"]["method"] == "daily_v13_narrowed_action_gate_v1"
    assert raw["debias"]["trade_action"] == "directional_signal"
    assert raw["debias"]["observe_only"] is False
    assert raw["debias"]["narrowed_from_observe_only_reason"] == "weak_raw_up_shifted_to_downrisk"
    assert raw["prompt_profile"] == "market-reaction-daily-v10-narrow-action-gate-v13"


def test_openai_replay_scorer_v13_keeps_low_edge_observe_only(tmp_path: Path) -> None:
    db = AnalysisDatabase(tmp_path / "openai-daily-v13-low-edge.db")
    event = _add_replay_event(
        db,
        "IBM",
        "openai-daily-v13-low-edge",
        "2026-04-22T20:10:35+00:00",
        content="IBM reports mixed results with offsetting positives and negatives.",
    )
    db.upsert_replay_market_outcome(
        _outcome(
            event,
            source="yahoo_chart_daily_adjclose",
            horizon="1D",
            raw=-0.002,
            excess=-0.001,
        )
    )
    row = db.replay_dataset_export_rows(limit=1)[0]
    row["session"] = "after_hours"
    parsed = OpenAIReplayPrediction(
        predicted_direction="down",
        expected_return=-0.004,
        bear_return=-0.014,
        bull_return=0.006,
        up_probability=0.29,
        flat_probability=0.41,
        down_probability=0.30,
        confidence=0.37,
        rationale="Mixed results leave only a tiny negative edge.",
    )

    class FakeResponses:
        def parse(self, **kwargs):
            return SimpleNamespace(
                id="resp_replay_daily_v13_low_edge",
                output=[SimpleNamespace(type="message", content=[SimpleNamespace(type="output_text", parsed=parsed)])],
            )

    scorer = OpenAIReplayScorer(
        api_key="test",
        model="gpt-test-replay",
        timeout_seconds=30,
        scorer_version="openai-replay-score-v13",
        client=SimpleNamespace(responses=FakeResponses()),
    )
    score = scorer.score(row)
    raw = json.loads(score.raw_json)

    assert score.scorer_version == "openai-replay-score-v13"
    assert score.predicted_direction == "down"
    assert raw["debias"]["method"] == "daily_v13_kept_v12_gate"
    assert raw["debias"]["trade_action"] == "observe_only"
    assert raw["debias"]["observe_only"] is True
    assert raw["debias"]["observe_only_reason"] == "low_edge_directional_signal"
    assert raw["prompt_profile"] == "market-reaction-daily-v10-narrow-action-gate-v13"


def test_openai_replay_scorer_v14_marks_tri_desk_conflict_second_pass(tmp_path: Path) -> None:
    db = AnalysisDatabase(tmp_path / "openai-daily-v14-conflict.db")
    event = _add_replay_event(
        db,
        "NVDA",
        "openai-daily-v14-conflict",
        "2026-06-20T20:02:01+00:00",
        content="Nvidia raises outlook on stronger AI demand.",
    )
    db.upsert_replay_market_outcome(
        _outcome(event, source="yahoo_chart_daily_adjclose", horizon="1D", raw=-0.025, excess=-0.020)
    )
    row = db.replay_dataset_export_rows(limit=1)[0]
    row["scorer_evidence_packet"] = {
        "packet_version": EVIDENCE_PACKET_VERSION,
        "packet_strength": "rich",
        "overlay": {
            "overlay_state": "asset_specific_shock",
            "overlay_action": "risk_reduce",
            "overlay_score": 6.0,
            "matched_signal_count": 2,
            "top_signals": [{"title": "Taiwan escalation pressures semiconductor supply chain"}],
        },
        "economic_indicator_desk": {
            "risk_bias": "macro_risk_off",
            "covered_series_count": 75,
            "series_count": 82,
            "macro_flags": [{"flag": "rates restrictive"}],
            "scorer_takeaways": ["Rates/curve desk is restrictive."],
        },
        "market_regime": {
            "regime": "risk_off",
            "risk_off_flags": ["QQQ 1D weak -2.0%", "VIX 1D spike +12.0%"],
            "risk_on_flags": [],
            "inflation_pressure_flags": [],
            "dollar_pressure_flags": [],
            "technical_indicator_count": 8,
            "macro_observation_count": 8,
        },
    }
    parsed = OpenAIReplayPrediction(
        predicted_direction="up",
        expected_return=0.018,
        bear_return=-0.012,
        bull_return=0.050,
        up_probability=0.56,
        flat_probability=0.25,
        down_probability=0.19,
        confidence=0.57,
        rationale="The company-specific guide is strong.",
    )

    class FakeResponses:
        def parse(self, **kwargs):
            system_prompt = kwargs["input"][0]["content"]
            payload = json.loads(kwargs["input"][1]["content"])
            assert "scorer_evidence_packet_v2" in payload
            assert "scorer_evidence_packet_v2 as point-in-time specialist context" in system_prompt
            return SimpleNamespace(
                id="resp_replay_daily_v14_conflict",
                output=[SimpleNamespace(type="message", content=[SimpleNamespace(type="output_text", parsed=parsed)])],
            )

    scorer = OpenAIReplayScorer(
        api_key="test",
        model="gpt-test-replay",
        timeout_seconds=30,
        scorer_version="openai-replay-score-v14",
        client=SimpleNamespace(responses=FakeResponses()),
    )
    score = scorer.score(row)
    raw = json.loads(score.raw_json)

    assert score.scorer_version == "openai-replay-score-v14"
    assert score.predicted_direction == "up"
    assert score.confidence <= 0.42
    assert raw["debias"]["method"] == "daily_v14_tri_desk_second_pass_required_v1"
    assert raw["debias"]["trade_action"] == "observe_only"
    assert raw["debias"]["observe_only_reason"] == "tri_desk_second_pass_required"
    assert raw["debias"]["tri_desk_alignment"]["alignment"] == "contradicted"
    assert raw["debias"]["tri_desk_conflict_score"] >= 0.67
    assert raw["prompt_profile"] == "market-reaction-daily-tri-desk-evidence-v14"


def test_openai_replay_scorer_v14_releases_confirmed_low_edge_gate(tmp_path: Path) -> None:
    db = AnalysisDatabase(tmp_path / "openai-daily-v14-confirm.db")
    event = _add_replay_event(
        db,
        "IBM",
        "openai-daily-v14-confirm",
        "2026-04-22T20:10:35+00:00",
        content="IBM reports mixed results with modest downside risk.",
    )
    db.upsert_replay_market_outcome(
        _outcome(event, source="yahoo_chart_daily_adjclose", horizon="1D", raw=-0.010, excess=-0.008)
    )
    row = db.replay_dataset_export_rows(limit=1)[0]
    row["scorer_evidence_packet"] = {
        "packet_version": EVIDENCE_PACKET_VERSION,
        "packet_strength": "rich",
        "overlay": {
            "overlay_state": "risk_off",
            "overlay_action": "risk_reduce",
            "overlay_score": 5.2,
            "matched_signal_count": 2,
            "top_signals": [{"title": "Macro risk-off tone pressures cyclicals"}],
        },
        "economic_indicator_desk": {
            "risk_bias": "macro_risk_off",
            "covered_series_count": 75,
            "series_count": 82,
            "macro_flags": [{"flag": "rates restrictive"}],
            "scorer_takeaways": ["Up calls need stronger company-specific evidence."],
        },
        "market_regime": {
            "regime": "risk_off",
            "risk_off_flags": ["SPY 1D weak -1.5%", "VIX 1D spike +8.0%"],
            "risk_on_flags": [],
            "inflation_pressure_flags": [],
            "dollar_pressure_flags": [],
            "technical_indicator_count": 8,
            "macro_observation_count": 8,
        },
    }
    parsed = OpenAIReplayPrediction(
        predicted_direction="down",
        expected_return=-0.004,
        bear_return=-0.014,
        bull_return=0.006,
        up_probability=0.29,
        flat_probability=0.41,
        down_probability=0.30,
        confidence=0.37,
        rationale="Mixed results leave only a tiny negative edge.",
    )

    class FakeResponses:
        def parse(self, **kwargs):
            return SimpleNamespace(
                id="resp_replay_daily_v14_confirm",
                output=[SimpleNamespace(type="message", content=[SimpleNamespace(type="output_text", parsed=parsed)])],
            )

    scorer = OpenAIReplayScorer(
        api_key="test",
        model="gpt-test-replay",
        timeout_seconds=30,
        scorer_version="openai-replay-score-v14",
        client=SimpleNamespace(responses=FakeResponses()),
    )
    score = scorer.score(row)
    raw = json.loads(score.raw_json)

    assert score.scorer_version == "openai-replay-score-v14"
    assert score.predicted_direction == "down"
    assert raw["debias"]["method"] == "daily_v14_tri_desk_confirmation_release_v1"
    assert raw["debias"]["trade_action"] == "directional_signal"
    assert raw["debias"]["observe_only"] is False
    assert raw["debias"]["tri_desk_alignment"]["alignment"] == "confirmed"
    assert raw["debias"]["tri_desk_confirmation_score"] >= 0.67


def test_openai_replay_scorer_v15_combines_tri_desk_and_emergency_conflict(tmp_path: Path) -> None:
    db = AnalysisDatabase(tmp_path / "openai-daily-v15-combined-conflict.db")
    event = _add_replay_event(
        db,
        "NVDA",
        "openai-daily-v15-combined-conflict",
        "2026-06-20T20:02:01+00:00",
        content="Nvidia raises outlook on stronger AI demand.",
    )
    db.upsert_replay_market_outcome(
        _outcome(event, source="yahoo_chart_daily_adjclose", horizon="1D", raw=-0.025, excess=-0.020)
    )
    row = db.replay_dataset_export_rows(limit=1)[0]
    row["scorer_evidence_packet"] = {
        "packet_version": EVIDENCE_PACKET_VERSION,
        "packet_strength": "rich",
        "overlay": {
            "overlay_state": "asset_specific_shock",
            "overlay_action": "risk_reduce",
            "overlay_score": 8.4,
            "matched_signal_count": 3,
            "emergency": True,
            "urgency": "critical",
            "age_hours": 0.75,
            "region": "taiwan_china",
            "theme": "semiconductor_supply_chain",
            "top_signals": [
                {
                    "title": "Breaking: Taiwan blockade threat hits advanced semiconductor supply chain",
                    "emergency": True,
                    "urgency": "critical",
                    "age_hours": 0.75,
                    "source": "emergency_breaking_news",
                }
            ],
        },
        "economic_indicator_desk": {
            "risk_bias": "macro_risk_off",
            "covered_series_count": 75,
            "series_count": 82,
            "macro_flags": [{"flag": "rates restrictive"}],
            "scorer_takeaways": ["Macro desk is risk-off."],
        },
        "market_regime": {
            "regime": "risk_off",
            "risk_off_flags": ["QQQ 1D weak -2.0%", "VIX 1D spike +12.0%"],
            "risk_on_flags": [],
            "inflation_pressure_flags": [],
            "dollar_pressure_flags": [],
            "technical_indicator_count": 8,
            "macro_observation_count": 8,
        },
    }
    parsed = OpenAIReplayPrediction(
        predicted_direction="up",
        expected_return=0.018,
        bear_return=-0.012,
        bull_return=0.050,
        up_probability=0.56,
        flat_probability=0.25,
        down_probability=0.19,
        confidence=0.57,
        rationale="The company-specific guide is strong.",
    )

    class FakeResponses:
        def parse(self, **kwargs):
            system_prompt = kwargs["input"][0]["content"]
            payload = json.loads(kwargs["input"][1]["content"])
            assert "fresh emergency_breaking_news overlay context" in system_prompt
            assert payload["scorer_evidence_packet_v2"]["overlay"]["emergency"] is True
            return SimpleNamespace(
                id="resp_replay_daily_v15_combined_conflict",
                output=[SimpleNamespace(type="message", content=[SimpleNamespace(type="output_text", parsed=parsed)])],
            )

    scorer = OpenAIReplayScorer(
        api_key="test",
        model="gpt-test-replay",
        timeout_seconds=30,
        scorer_version="openai-replay-score-v15",
        client=SimpleNamespace(responses=FakeResponses()),
    )
    score = scorer.score(row)
    raw = json.loads(score.raw_json)

    assert score.scorer_version == "openai-replay-score-v15"
    assert score.predicted_direction == "up"
    assert score.confidence <= 0.40
    assert raw["debias"]["method"] == "daily_v15_tri_emergency_second_pass_v1"
    assert raw["debias"]["trade_action"] == "observe_only"
    assert raw["debias"]["observe_only_reason"] == "tri_desk_emergency_second_pass_required"
    assert raw["debias"]["combined_gate"]["combined_second_pass"] is True
    assert raw["debias"]["emergency_overlay"]["relation"] == "emergency_conflicts_up"
    assert raw["prompt_profile"] == "market-reaction-daily-tri-desk-emergency-v15"


def test_openai_replay_scorer_v15_preserves_emergency_confirmed_down(tmp_path: Path) -> None:
    db = AnalysisDatabase(tmp_path / "openai-daily-v15-confirm-down.db")
    event = _add_replay_event(
        db,
        "IBM",
        "openai-daily-v15-confirm-down",
        "2026-04-22T20:10:35+00:00",
        content="IBM reports mixed results with modest downside risk.",
    )
    db.upsert_replay_market_outcome(
        _outcome(event, source="yahoo_chart_daily_adjclose", horizon="1D", raw=-0.010, excess=-0.008)
    )
    row = db.replay_dataset_export_rows(limit=1)[0]
    row["scorer_evidence_packet"] = {
        "packet_version": EVIDENCE_PACKET_VERSION,
        "packet_strength": "rich",
        "overlay": {
            "overlay_state": "risk_off",
            "overlay_action": "risk_reduce",
            "overlay_score": 7.8,
            "matched_signal_count": 2,
            "emergency": True,
            "urgency": "high",
            "age_hours": 3.0,
            "region": "global",
            "theme": "trade_conflict",
            "top_signals": [
                {
                    "title": "Breaking: Tariff shock weighs on global cyclicals",
                    "emergency": True,
                    "urgency": "high",
                    "age_hours": 3.0,
                    "source": "emergency_breaking_news",
                }
            ],
        },
        "economic_indicator_desk": {
            "risk_bias": "macro_risk_off",
            "covered_series_count": 75,
            "series_count": 82,
            "macro_flags": [{"flag": "financial conditions tight"}],
            "scorer_takeaways": ["Risk-off macro confirms downside sensitivity."],
        },
        "market_regime": {
            "regime": "risk_off",
            "risk_off_flags": ["SPY 1D weak -1.5%", "VIX 1D spike +8.0%"],
            "risk_on_flags": [],
            "inflation_pressure_flags": [],
            "dollar_pressure_flags": [],
            "technical_indicator_count": 8,
            "macro_observation_count": 8,
        },
    }
    parsed = OpenAIReplayPrediction(
        predicted_direction="down",
        expected_return=-0.004,
        bear_return=-0.014,
        bull_return=0.006,
        up_probability=0.29,
        flat_probability=0.41,
        down_probability=0.30,
        confidence=0.37,
        rationale="Mixed results leave only a tiny negative edge.",
    )

    class FakeResponses:
        def parse(self, **kwargs):
            return SimpleNamespace(
                id="resp_replay_daily_v15_confirm_down",
                output=[SimpleNamespace(type="message", content=[SimpleNamespace(type="output_text", parsed=parsed)])],
            )

    scorer = OpenAIReplayScorer(
        api_key="test",
        model="gpt-test-replay",
        timeout_seconds=30,
        scorer_version="openai-replay-score-v15",
        client=SimpleNamespace(responses=FakeResponses()),
    )
    score = scorer.score(row)
    raw = json.loads(score.raw_json)

    assert score.scorer_version == "openai-replay-score-v15"
    assert score.predicted_direction == "down"
    assert raw["debias"]["method"] == "daily_v15_emergency_down_confirmation_preserve_v1"
    assert raw["debias"]["trade_action"] == "directional_signal"
    assert raw["debias"]["observe_only"] is False
    assert raw["debias"]["combined_gate"]["confirmed_down"] is True
    assert raw["debias"]["emergency_overlay"]["relation"] == "emergency_confirms_down"
    assert raw["debias"]["tri_desk_confirmation_score"] >= 0.67


def test_openai_replay_scorer_v16_keeps_only_combined_emergency_conflict_gate(tmp_path: Path) -> None:
    db = AnalysisDatabase(tmp_path / "openai-daily-v16-combined-conflict.db")
    event = _add_replay_event(
        db,
        "NVDA",
        "openai-daily-v16-combined-conflict",
        "2026-06-20T20:02:01+00:00",
        content="Nvidia raises outlook on stronger AI demand.",
    )
    db.upsert_replay_market_outcome(
        _outcome(event, source="yahoo_chart_daily_adjclose", horizon="1D", raw=-0.025, excess=-0.020)
    )
    row = db.replay_dataset_export_rows(limit=1)[0]
    row["scorer_evidence_packet"] = {
        "packet_version": EVIDENCE_PACKET_VERSION,
        "packet_strength": "rich",
        "overlay": {
            "overlay_state": "asset_specific_shock",
            "overlay_action": "risk_reduce",
            "overlay_score": 8.4,
            "matched_signal_count": 3,
            "emergency": True,
            "urgency": "critical",
            "age_hours": 0.75,
            "region": "taiwan_china",
            "theme": "semiconductor_supply_chain",
            "top_signals": [
                {
                    "title": "Breaking: Taiwan blockade threat hits advanced semiconductor supply chain",
                    "emergency": True,
                    "urgency": "critical",
                    "age_hours": 0.75,
                    "source": "emergency_breaking_news",
                }
            ],
        },
        "economic_indicator_desk": {
            "risk_bias": "macro_risk_off",
            "covered_series_count": 75,
            "series_count": 82,
            "macro_flags": [{"flag": "rates restrictive"}],
            "scorer_takeaways": ["Macro desk is risk-off."],
        },
        "market_regime": {
            "regime": "risk_off",
            "risk_off_flags": ["QQQ 1D weak -2.0%", "VIX 1D spike +12.0%"],
            "risk_on_flags": [],
            "inflation_pressure_flags": [],
            "dollar_pressure_flags": [],
            "technical_indicator_count": 8,
            "macro_observation_count": 8,
        },
    }
    parsed = OpenAIReplayPrediction(
        predicted_direction="up",
        expected_return=0.018,
        bear_return=-0.012,
        bull_return=0.050,
        up_probability=0.56,
        flat_probability=0.25,
        down_probability=0.19,
        confidence=0.57,
        rationale="The company-specific guide is strong.",
    )

    class FakeResponses:
        def parse(self, **kwargs):
            payload = json.loads(kwargs["input"][1]["content"])
            assert payload["scorer_evidence_packet_v2"]["overlay"]["emergency"] is True
            return SimpleNamespace(
                id="resp_replay_daily_v16_combined_conflict",
                output=[SimpleNamespace(type="message", content=[SimpleNamespace(type="output_text", parsed=parsed)])],
            )

    scorer = OpenAIReplayScorer(
        api_key="test",
        model="gpt-test-replay",
        timeout_seconds=30,
        scorer_version="openai-replay-score-v16",
        client=SimpleNamespace(responses=FakeResponses()),
    )
    score = scorer.score(row)
    raw = json.loads(score.raw_json)

    assert score.scorer_version == "openai-replay-score-v16"
    assert raw["debias"]["method"] == "daily_v16_tri_emergency_second_pass_v2"
    assert raw["debias"]["trade_action"] == "observe_only"
    assert raw["debias"]["observe_only_reason"] == "tri_desk_emergency_second_pass_required"
    assert raw["debias"]["combined_gate"]["combined_second_pass"] is True
    assert raw["debias"]["combined_gate"]["broad_haircut_disabled"] is True
    assert raw["prompt_profile"] == "market-reaction-daily-tri-desk-emergency-narrow-v16"


def test_openai_replay_scorer_v16_preserves_confirmed_down_without_broad_haircut(tmp_path: Path) -> None:
    db = AnalysisDatabase(tmp_path / "openai-daily-v16-confirm-down.db")
    event = _add_replay_event(
        db,
        "IBM",
        "openai-daily-v16-confirm-down",
        "2026-04-22T20:10:35+00:00",
        content="IBM reports mixed results with modest downside risk.",
    )
    db.upsert_replay_market_outcome(
        _outcome(event, source="yahoo_chart_daily_adjclose", horizon="1D", raw=-0.010, excess=-0.008)
    )
    row = db.replay_dataset_export_rows(limit=1)[0]
    row["scorer_evidence_packet"] = {
        "packet_version": EVIDENCE_PACKET_VERSION,
        "packet_strength": "rich",
        "overlay": {
            "overlay_state": "risk_off",
            "overlay_action": "risk_reduce",
            "overlay_score": 7.8,
            "matched_signal_count": 2,
            "emergency": True,
            "urgency": "high",
            "age_hours": 3.0,
            "region": "global",
            "theme": "trade_conflict",
            "top_signals": [
                {
                    "title": "Breaking: Tariff shock weighs on global cyclicals",
                    "emergency": True,
                    "urgency": "high",
                    "age_hours": 3.0,
                    "source": "emergency_breaking_news",
                }
            ],
        },
        "economic_indicator_desk": {
            "risk_bias": "macro_risk_off",
            "covered_series_count": 75,
            "series_count": 82,
            "macro_flags": [{"flag": "financial conditions tight"}],
            "scorer_takeaways": ["Risk-off macro confirms downside sensitivity."],
        },
        "market_regime": {
            "regime": "risk_off",
            "risk_off_flags": ["SPY 1D weak -1.5%", "VIX 1D spike +8.0%"],
            "risk_on_flags": [],
            "inflation_pressure_flags": [],
            "dollar_pressure_flags": [],
            "technical_indicator_count": 8,
            "macro_observation_count": 8,
        },
    }
    parsed = OpenAIReplayPrediction(
        predicted_direction="down",
        expected_return=-0.004,
        bear_return=-0.014,
        bull_return=0.006,
        up_probability=0.29,
        flat_probability=0.41,
        down_probability=0.30,
        confidence=0.37,
        rationale="Mixed results leave only a tiny negative edge.",
    )

    class FakeResponses:
        def parse(self, **kwargs):
            return SimpleNamespace(
                id="resp_replay_daily_v16_confirm_down",
                output=[SimpleNamespace(type="message", content=[SimpleNamespace(type="output_text", parsed=parsed)])],
            )

    scorer = OpenAIReplayScorer(
        api_key="test",
        model="gpt-test-replay",
        timeout_seconds=30,
        scorer_version="openai-replay-score-v16",
        client=SimpleNamespace(responses=FakeResponses()),
    )
    score = scorer.score(row)
    raw = json.loads(score.raw_json)

    assert score.scorer_version == "openai-replay-score-v16"
    assert raw["debias"]["method"] == "daily_v16_emergency_down_confirmation_preserve_v2"
    assert raw["debias"]["trade_action"] == "directional_signal"
    assert raw["debias"]["observe_only"] is False
    assert raw["debias"]["combined_gate"]["confirmed_down"] is True
    assert raw["debias"]["combined_gate"]["broad_haircut_disabled"] is True
    assert raw["debias"]["tri_desk_confirmation_score"] >= 0.67


def test_replay_scoring_engine_attaches_v14_evidence_packet(tmp_path: Path) -> None:
    db = AnalysisDatabase(tmp_path / "openai-daily-v14-engine-packet.db")
    event = _add_replay_event(
        db,
        "MSFT",
        "openai-daily-v14-engine-packet",
        "2026-05-01T20:10:35+00:00",
        content="Microsoft raises outlook on cloud demand.",
    )
    db.upsert_replay_market_outcome(
        _outcome(event, source="yahoo_chart_daily_adjclose", horizon="1D", raw=0.010, excess=0.008)
    )

    class PacketAssertingScorer:
        scorer_name = "openai"
        model_name = "gpt-test-replay"
        scorer_version = "openai-replay-score-v14"

        def score(self, row):
            assert row["scorer_evidence_packet"]["packet_version"] == EVIDENCE_PACKET_VERSION
            assert "tri_desk_alignment" in row["scorer_evidence_packet"]
            base = HeuristicReplayScorer().score(row)
            return replace(
                base,
                scorer_name=self.scorer_name,
                model_name=self.model_name,
                scorer_version=self.scorer_version,
                raw_json=json.dumps({"packet_attached": True}),
            )

    result = ReplayScoringEngine(db, PacketAssertingScorer()).build(
        price_source="yahoo_chart_daily_adjclose",
        limit=1,
    )

    assert result["scored"] == 1
    rows = db.replay_model_score_rows(scorer_name="openai", scorer_version="openai-replay-score-v14", limit=1)
    assert rows[0]["model_name"] == "gpt-test-replay"


def test_compact_scorer_evidence_packet_includes_m221_options_structure() -> None:
    compact = _compact_scorer_evidence_packet(
        {
            "packet_version": EVIDENCE_PACKET_VERSION,
            "event": {"cutoff_date": "2026-07-01"},
            "overlay": {},
            "economic_indicator_desk": {},
            "market_regime": {},
            "tri_desk_alignment": {},
            "option_structure_context": {
                "available": True,
                "source": "m220-market-move-option-structure-overlay-v1",
                "freshness_status": "green",
                "overall_status": "option_structure_confirms_trend_risk",
                "mapped_move_count": 2,
                "matched_row_count": 1,
                "selected_source_type": "proxy",
                "coverage_status": "proxy_only",
                "matched_option_symbol": "QQQ",
                "gamma_regime": "negative_gamma_trend_amplification_bias",
                "call_wall": 530.0,
                "put_wall": 510.0,
                "max_pain": 520.0,
                "proximity_alerts": ["near_call_wall"],
                "explanation_status": "confirmed_trend_amplification",
                "scorer_bias": "trend_amplification",
                "scorer_action": "allow_larger_reaction_if_news_and_market_move_confirm",
                "confidence": 0.4,
                "guidance": ["Negative gamma can amplify a clean move."],
            },
        }
    )

    assert compact["options_market_structure"]["available"] is True
    assert compact["options_market_structure"]["gamma_regime"] == "negative_gamma_trend_amplification_bias"
    assert compact["options_market_structure"]["scorer_action"] == "allow_larger_reaction_if_news_and_market_move_confirm"


def test_replay_scoring_engine_attaches_v15_evidence_packet(tmp_path: Path) -> None:
    db = AnalysisDatabase(tmp_path / "openai-daily-v15-engine-packet.db")
    event = _add_replay_event(
        db,
        "MSFT",
        "openai-daily-v15-engine-packet",
        "2026-05-01T20:10:35+00:00",
        content="Microsoft raises outlook on cloud demand.",
    )
    db.upsert_replay_market_outcome(
        _outcome(event, source="yahoo_chart_daily_adjclose", horizon="1D", raw=0.010, excess=0.008)
    )

    class PacketAssertingScorer:
        scorer_name = "openai"
        model_name = "gpt-test-replay"
        scorer_version = "openai-replay-score-v15"

        def score(self, row):
            assert row["scorer_evidence_packet"]["packet_version"] == EVIDENCE_PACKET_VERSION
            assert "tri_desk_alignment" in row["scorer_evidence_packet"]
            base = HeuristicReplayScorer().score(row)
            return replace(
                base,
                scorer_name=self.scorer_name,
                model_name=self.model_name,
                scorer_version=self.scorer_version,
                raw_json=json.dumps({"packet_attached": True}),
            )

    result = ReplayScoringEngine(db, PacketAssertingScorer()).build(
        price_source="yahoo_chart_daily_adjclose",
        limit=1,
    )

    assert result["scored"] == 1
    rows = db.replay_model_score_rows(scorer_name="openai", scorer_version="openai-replay-score-v15", limit=1)
    assert rows[0]["model_name"] == "gpt-test-replay"


def test_replay_scoring_engine_attaches_v16_evidence_packet(tmp_path: Path) -> None:
    db = AnalysisDatabase(tmp_path / "openai-daily-v16-engine-packet.db")
    event = _add_replay_event(
        db,
        "MSFT",
        "openai-daily-v16-engine-packet",
        "2026-05-01T20:10:35+00:00",
        content="Microsoft raises outlook on cloud demand.",
    )
    db.upsert_replay_market_outcome(
        _outcome(event, source="yahoo_chart_daily_adjclose", horizon="1D", raw=0.010, excess=0.008)
    )

    class PacketAssertingScorer:
        scorer_name = "openai"
        model_name = "gpt-test-replay"
        scorer_version = "openai-replay-score-v16"

        def score(self, row):
            assert row["scorer_evidence_packet"]["packet_version"] == EVIDENCE_PACKET_VERSION
            assert "tri_desk_alignment" in row["scorer_evidence_packet"]
            base = HeuristicReplayScorer().score(row)
            return replace(
                base,
                scorer_name=self.scorer_name,
                model_name=self.model_name,
                scorer_version=self.scorer_version,
                raw_json=json.dumps({"packet_attached": True}),
            )

    result = ReplayScoringEngine(db, PacketAssertingScorer()).build(
        price_source="yahoo_chart_daily_adjclose",
        limit=1,
    )

    assert result["scored"] == 1
    rows = db.replay_model_score_rows(scorer_name="openai", scorer_version="openai-replay-score-v16", limit=1)
    assert rows[0]["model_name"] == "gpt-test-replay"


def test_v14_overlay_postscore_preserves_direction_and_marks_combined_conflict() -> None:
    row = {
        "replay_corpus_id": 1,
        "outcome_id": 2,
        "historical_event_id": 3,
        "official_document_id": 4,
        "symbol": "NVDA",
        "event_type": "earnings",
        "event_subtype": "",
        "replay_timestamp": "2026-06-20T20:02:01+00:00",
        "prediction_cutoff_at": "2026-06-20T20:02:01+00:00",
        "horizon": "1D",
        "price_source": "yahoo_chart_daily_adjclose",
        "scorer_name": "openai",
        "model_name": "gpt-5.4-mini",
        "scorer_version": "openai-replay-score-v14",
        "input_snapshot_hash": "hash-v14",
        "prediction_status": "complete",
        "predicted_direction": "up",
        "expected_return": 0.018,
        "bear_return": -0.012,
        "bull_return": 0.050,
        "up_probability": 0.56,
        "flat_probability": 0.25,
        "down_probability": 0.19,
        "confidence": 0.57,
        "rationale": "Stored v14 up call.",
        "raw_json": json.dumps(
            {
                "debias": {
                    "method": "daily_v14_kept_v13_gate",
                    "trade_action": "directional_signal",
                    "observe_only": False,
                    "observe_only_reason": "",
                }
            }
        ),
        "actual_direction": "down",
        "actual_raw_return": -0.025,
        "actual_excess_return": -0.020,
        "direction_hit": 0,
        "absolute_raw_error": 0.043,
        "absolute_excess_error": 0.038,
    }
    evidence_packet = {
        "overlay": {
            "overlay_state": "asset_specific_shock",
            "overlay_action": "risk_reduce",
            "overlay_score": 8.4,
            "matched_signal_count": 3,
            "emergency": True,
            "urgency": "critical",
            "age_hours": 0.75,
            "region": "taiwan_china",
            "theme": "semiconductor_supply_chain",
            "top_signals": [
                {
                    "title": "Breaking: Taiwan blockade threat hits advanced semiconductor supply chain",
                    "emergency": True,
                    "urgency": "critical",
                    "age_hours": 0.75,
                    "source": "emergency_breaking_news",
                }
            ],
        },
        "economic_indicator_desk": {
            "risk_bias": "macro_risk_off",
            "covered_series_count": 75,
            "series_count": 82,
            "macro_flags": [{"flag": "rates restrictive"}],
            "scorer_takeaways": ["Macro desk is risk-off."],
        },
        "market_regime": {
            "regime": "risk_off",
            "risk_off_flags": ["QQQ 1D weak -2.0%", "VIX 1D spike +12.0%"],
            "risk_on_flags": [],
            "inflation_pressure_flags": [],
            "dollar_pressure_flags": [],
            "technical_indicator_count": 8,
            "macro_observation_count": 8,
        },
    }

    score = transform_v14_overlay_postscore(row, evidence_packet)
    raw = json.loads(score.raw_json)

    assert score.scorer_version == OPENAI_REPLAY_SCORER_VERSION_V14_OVERLAY_V1
    assert score.predicted_direction == "up"
    assert score.direction_hit == 0
    assert score.confidence <= 0.42
    assert raw["debias"]["method"] == "daily_v14_overlay_v1_tri_emergency_second_pass"
    assert raw["debias"]["trade_action"] == "observe_only"
    assert raw["debias"]["combined_gate"]["direction_preserved"] is True
    assert raw["debias"]["combined_gate"]["openai_called"] is False
    assert raw["prompt_profile"] == "deterministic-v14-postscore-overlay-v1"


def test_v14_overlay_postscore_preserves_confirmed_down_directional_metadata() -> None:
    row = {
        "replay_corpus_id": 1,
        "outcome_id": 2,
        "historical_event_id": 3,
        "official_document_id": 4,
        "symbol": "IBM",
        "event_type": "earnings",
        "event_subtype": "",
        "replay_timestamp": "2026-04-22T20:10:35+00:00",
        "prediction_cutoff_at": "2026-04-22T20:10:35+00:00",
        "horizon": "1D",
        "price_source": "yahoo_chart_daily_adjclose",
        "scorer_name": "openai",
        "model_name": "gpt-5.4-mini",
        "scorer_version": "openai-replay-score-v14",
        "input_snapshot_hash": "hash-v14",
        "prediction_status": "complete",
        "predicted_direction": "down",
        "expected_return": -0.004,
        "bear_return": -0.014,
        "bull_return": 0.006,
        "up_probability": 0.29,
        "flat_probability": 0.41,
        "down_probability": 0.30,
        "confidence": 0.37,
        "rationale": "Stored v14 down-risk call.",
        "raw_json": json.dumps(
            {
                "debias": {
                    "method": "daily_v14_tri_desk_confidence_haircut_v1",
                    "trade_action": "observe_only",
                    "observe_only": True,
                    "observe_only_reason": "low_edge_directional_signal",
                }
            }
        ),
        "actual_direction": "down",
        "actual_raw_return": -0.010,
        "actual_excess_return": -0.008,
        "direction_hit": 1,
        "absolute_raw_error": 0.006,
        "absolute_excess_error": 0.004,
    }
    evidence_packet = {
        "overlay": {
            "overlay_state": "risk_off",
            "overlay_action": "risk_reduce",
            "overlay_score": 7.8,
            "matched_signal_count": 2,
            "emergency": True,
            "urgency": "high",
            "age_hours": 3.0,
            "region": "global",
            "theme": "trade_conflict",
            "top_signals": [
                {
                    "title": "Breaking: Tariff shock weighs on global cyclicals",
                    "emergency": True,
                    "urgency": "high",
                    "age_hours": 3.0,
                    "source": "emergency_breaking_news",
                }
            ],
        },
        "economic_indicator_desk": {
            "risk_bias": "macro_risk_off",
            "covered_series_count": 75,
            "series_count": 82,
            "macro_flags": [{"flag": "financial conditions tight"}],
            "scorer_takeaways": ["Risk-off macro confirms downside sensitivity."],
        },
        "market_regime": {
            "regime": "risk_off",
            "risk_off_flags": ["SPY 1D weak -1.5%", "VIX 1D spike +8.0%"],
            "risk_on_flags": [],
            "inflation_pressure_flags": [],
            "dollar_pressure_flags": [],
            "technical_indicator_count": 8,
            "macro_observation_count": 8,
        },
    }

    score = transform_v14_overlay_postscore(row, evidence_packet)
    raw = json.loads(score.raw_json)

    assert score.predicted_direction == "down"
    assert score.direction_hit == 1
    assert raw["debias"]["method"] == "daily_v14_overlay_v1_emergency_down_confirmation_preserve"
    assert raw["debias"]["trade_action"] == "directional_signal"
    assert raw["debias"]["observe_only"] is False
    assert raw["debias"]["combined_gate"]["confirmed_down"] is True
    assert raw["debias"]["combined_gate"]["openai_called"] is False


def test_v14_overlay_v2_applies_m72_best_threshold_gate() -> None:
    row = {
        "replay_corpus_id": 1,
        "outcome_id": 2,
        "historical_event_id": 3,
        "official_document_id": 4,
        "symbol": "NVDA",
        "event_type": "earnings",
        "event_subtype": "",
        "replay_timestamp": "2026-06-20T20:02:01+00:00",
        "prediction_cutoff_at": "2026-06-20T20:02:01+00:00",
        "horizon": "1D",
        "price_source": "yahoo_chart_daily_adjclose",
        "scorer_name": "openai",
        "model_name": "gpt-5.4-mini",
        "scorer_version": "openai-replay-score-v14",
        "input_snapshot_hash": "hash-v14",
        "prediction_status": "complete",
        "predicted_direction": "up",
        "expected_return": 0.018,
        "bear_return": -0.012,
        "bull_return": 0.050,
        "up_probability": 0.56,
        "flat_probability": 0.25,
        "down_probability": 0.19,
        "confidence": 0.57,
        "rationale": "Stored v14 up call.",
        "raw_json": json.dumps({"debias": {"trade_action": "directional_signal", "observe_only": False}}),
        "actual_direction": "down",
        "actual_raw_return": -0.025,
        "actual_excess_return": -0.020,
        "direction_hit": 0,
        "absolute_raw_error": 0.043,
        "absolute_excess_error": 0.038,
    }
    evidence_packet = {
        "overlay": {
            "overlay_state": "asset_specific_shock",
            "overlay_action": "risk_reduce",
            "overlay_score": 5.0,
            "matched_signal_count": 3,
            "emergency": True,
            "urgency": "high",
            "age_hours": 20.0,
            "region": "taiwan_china",
            "theme": "semiconductor_supply_chain",
            "top_signals": [
                {
                    "title": "Breaking: Taiwan blockade threat hits advanced semiconductor supply chain",
                    "emergency": True,
                    "urgency": "high",
                    "age_hours": 20.0,
                    "source": "emergency_breaking_news",
                }
            ],
        },
        "economic_indicator_desk": {
            "risk_bias": "macro_risk_off",
            "covered_series_count": 75,
            "series_count": 82,
            "macro_flags": [{"flag": "rates restrictive"}],
            "scorer_takeaways": ["Macro desk is risk-off."],
        },
        "market_regime": {
            "regime": "risk_off",
            "risk_off_flags": ["QQQ 1D weak -2.0%", "VIX 1D spike +12.0%"],
            "risk_on_flags": [],
            "inflation_pressure_flags": [],
            "dollar_pressure_flags": [],
            "technical_indicator_count": 8,
            "macro_observation_count": 8,
        },
    }

    score = transform_v14_overlay_postscore_v2(row, evidence_packet)
    raw = json.loads(score.raw_json)

    assert score.scorer_version == OPENAI_REPLAY_SCORER_VERSION_V14_OVERLAY_V2
    assert score.predicted_direction == "up"
    assert raw["debias"]["method"] == "daily_v14_overlay_v2_tri_emergency_second_pass"
    assert raw["debias"]["trade_action"] == "observe_only"
    assert raw["debias"]["thresholds"]["conflict_score_min"] == 0.5
    assert raw["debias"]["thresholds"]["confirmation_score_min"] == 0.5
    assert raw["debias"]["thresholds"]["age_hours_max"] == 24.0
    assert raw["debias"]["combined_gate"]["openai_called"] is False
    assert raw["prompt_profile"] == "deterministic-v14-postscore-overlay-v2"


def test_v14_overlay_v3_applies_critical_minerals_family_gate_without_emergency() -> None:
    row = {
        "replay_corpus_id": 1,
        "outcome_id": 10,
        "historical_event_id": 100,
        "official_document_id": 1000,
        "symbol": "NVDA",
        "event_type": "earnings",
        "event_subtype": "",
        "replay_timestamp": "2026-06-20T20:02:01+00:00",
        "prediction_cutoff_at": "2026-06-20T20:02:01+00:00",
        "session": "after_hours",
        "horizon": "1D",
        "price_source": "yahoo_chart_daily_adjclose",
        "scorer_name": "openai",
        "model_name": "gpt-5.4-mini",
        "scorer_version": "openai-replay-score-v14",
        "input_snapshot_hash": "hash-up",
        "prediction_status": "complete",
        "predicted_direction": "up",
        "expected_return": 0.018,
        "bear_return": -0.012,
        "bull_return": 0.050,
        "up_probability": 0.56,
        "flat_probability": 0.25,
        "down_probability": 0.19,
        "confidence": 0.57,
        "rationale": "Stored v14 up call.",
        "raw_json": json.dumps({"debias": {"trade_action": "directional_signal", "observe_only": False}}),
        "actual_direction": "down",
        "actual_raw_return": -0.025,
        "actual_excess_return": -0.020,
        "direction_hit": 0,
        "absolute_raw_error": 0.043,
        "absolute_excess_error": 0.038,
    }
    evidence_packet = {
        "overlay": {
            "overlay_state": "normal",
            "overlay_action": "none",
            "overlay_score": 0.0,
            "matched_signal_count": 0,
            "top_signals": [],
        },
        "economic_indicator_desk": {
            "risk_bias": "macro_neutral",
            "covered_series_count": 75,
            "series_count": 82,
            "macro_flags": [],
            "scorer_takeaways": [],
        },
        "market_regime": {
            "regime": "insufficient",
            "risk_off_flags": [],
            "risk_on_flags": [],
            "inflation_pressure_flags": [],
            "dollar_pressure_flags": [],
            "technical_indicator_count": 8,
            "macro_observation_count": 8,
        },
        "standing_regime_overlay": {
            "bias": "risk_off",
            "matched_regime_count": 1,
            "active_regimes": [
                {
                    "family": "critical_minerals_geopolitics",
                    "directional_bias": "risk_off",
                    "status": "active",
                    "severity_score": 6.2,
                    "confidence": 0.93,
                }
            ],
        },
        "tri_desk_alignment": {
            "conflict_score": 0.50,
            "confirmation_score": 0.0,
            "net_alignment_score": -0.50,
        },
    }

    score = transform_v14_overlay_postscore_v3(row, evidence_packet)
    raw = json.loads(score.raw_json)

    assert score.scorer_version == OPENAI_REPLAY_SCORER_VERSION_V14_OVERLAY_V3
    assert score.predicted_direction == "up"
    assert raw["debias"]["method"] == "daily_v14_overlay_v3_critical_minerals_family_conflict_second_pass"
    assert raw["debias"]["trade_action"] == "observe_only"
    assert raw["debias"]["observe_only_reason"] == "critical_minerals_family_conflict_second_pass"
    assert raw["debias"]["standing_regime_family_gate"]["applies"] is True
    assert raw["debias"]["combined_gate"]["openai_called"] is False
    assert raw["prompt_profile"] == "deterministic-v14-postscore-overlay-v3-standing-family-gate"


def test_postscore_threshold_tuning_ranks_gate_that_filters_bad_up_conflict(tmp_path: Path) -> None:
    db = AnalysisDatabase(tmp_path / "postscore-threshold-tuning.db")
    rows = [
        {
            "replay_corpus_id": 1,
            "outcome_id": 10,
            "historical_event_id": 100,
            "official_document_id": 1000,
            "symbol": "NVDA",
            "event_type": "earnings",
            "event_subtype": "",
            "replay_timestamp": "2026-06-20T20:02:01+00:00",
            "prediction_cutoff_at": "2026-06-20T20:02:01+00:00",
            "session": "after_hours",
            "horizon": "1D",
            "price_source": "yahoo_chart_daily_adjclose",
            "scorer_name": "openai",
            "model_name": "gpt-5.4-mini",
            "scorer_version": "openai-replay-score-v14",
            "input_snapshot_hash": "hash-up",
            "prediction_status": "complete",
            "predicted_direction": "up",
            "expected_return": 0.018,
            "bear_return": -0.012,
            "bull_return": 0.050,
            "up_probability": 0.56,
            "flat_probability": 0.25,
            "down_probability": 0.19,
            "confidence": 0.57,
            "rationale": "Stored v14 up call.",
            "raw_json": json.dumps({"debias": {"trade_action": "directional_signal", "observe_only": False}}),
            "actual_direction": "down",
            "actual_raw_return": -0.025,
            "actual_excess_return": -0.020,
            "direction_hit": 0,
            "absolute_raw_error": 0.043,
            "absolute_excess_error": 0.038,
        },
        {
            "replay_corpus_id": 2,
            "outcome_id": 20,
            "historical_event_id": 200,
            "official_document_id": 2000,
            "symbol": "IBM",
            "event_type": "earnings",
            "event_subtype": "",
            "replay_timestamp": "2026-06-20T20:02:01+00:00",
            "prediction_cutoff_at": "2026-06-20T20:02:01+00:00",
            "session": "after_hours",
            "horizon": "1D",
            "price_source": "yahoo_chart_daily_adjclose",
            "scorer_name": "openai",
            "model_name": "gpt-5.4-mini",
            "scorer_version": "openai-replay-score-v14",
            "input_snapshot_hash": "hash-down",
            "prediction_status": "complete",
            "predicted_direction": "down",
            "expected_return": -0.004,
            "bear_return": -0.014,
            "bull_return": 0.006,
            "up_probability": 0.29,
            "flat_probability": 0.41,
            "down_probability": 0.30,
            "confidence": 0.37,
            "rationale": "Stored v14 down-risk call.",
            "raw_json": json.dumps({"debias": {"trade_action": "observe_only", "observe_only": True}}),
            "actual_direction": "down",
            "actual_raw_return": -0.010,
            "actual_excess_return": -0.008,
            "direction_hit": 1,
            "absolute_raw_error": 0.006,
            "absolute_excess_error": 0.004,
        },
    ]
    signals = [
        {
            "source": "emergency_breaking_news",
            "title": "Breaking: Taiwan blockade threat hits advanced semiconductor supply chain",
            "published_at": "2026-06-20T19:10:01+00:00",
            "assets_json": json.dumps(["NVDA", "SMH", "QQQ"]),
            "region": "taiwan_china",
            "theme": "semiconductor_supply_chain",
            "sentiment_label": "negative",
            "sentiment_score": -0.80,
            "confidence": 0.90,
            "prediction_gate_status": "eligible",
            "raw_json": json.dumps({"emergency": True, "urgency": {"urgency": "critical"}}),
            "market_reaction_json": "{}",
        },
        {
            "source": "emergency_breaking_news",
            "title": "Breaking: Tariff shock weighs on global cyclicals",
            "published_at": "2026-06-20T19:30:01+00:00",
            "assets_json": json.dumps(["IBM", "SPY", "QQQ"]),
            "region": "global",
            "theme": "trade_conflict",
            "sentiment_label": "negative",
            "sentiment_score": -0.70,
            "confidence": 0.88,
            "prediction_gate_status": "eligible",
            "raw_json": json.dumps({"emergency": True, "urgency": {"urgency": "high"}}),
            "market_reaction_json": "{}",
        },
    ]

    summary = postscore_threshold_tuning_summary(
        rows,
        signals,
        db,
        overlay_score_grid=(4.5,),
        conflict_score_grid=(0.40,),
        confirmation_score_grid=(0.40,),
        age_hours_grid=(4.0,),
        confirmation_conflict_grid=(0.40,),
        critical_only_grid=(False,),
        min_directional_coverage=0.1,
        top_n=1,
    )
    report = postscore_threshold_tuning_markdown(summary)

    assert summary["top_candidates"]
    top = summary["top_candidates"][0]
    assert top["tradeable_directional"]["directional_accuracy"] == 1.0
    assert top["observe_only_directional"]["directional_accuracy"] == 0.0
    assert top["method_counts"]["threshold_tri_emergency_second_pass"] == 1
    assert top["method_counts"]["threshold_emergency_down_confirmation_preserve"] == 1
    assert "Top threshold candidates" in report
    assert "overlay>=4.5" in report


def test_replay_score_calibration_summary_bins_confidence(tmp_path: Path) -> None:
    db = AnalysisDatabase(tmp_path / "score-calibration.db")
    up = _add_replay_event(
        db,
        "MU",
        "calibration-up",
        "2026-06-24T20:02:01+00:00",
        content="Micron raises outlook on stronger demand.",
    )
    down = _add_replay_event(
        db,
        "NVDA",
        "calibration-down",
        "2026-06-25T20:02:01+00:00",
        content="Nvidia raises outlook but investors expected more.",
    )
    db.upsert_replay_market_outcome(
        _outcome(up, source="yahoo_chart_daily_adjclose", horizon="1D", raw=0.02, excess=0.01)
    )
    db.upsert_replay_market_outcome(
        _outcome(down, source="yahoo_chart_daily_adjclose", horizon="1D", raw=-0.02, excess=-0.01)
    )
    ReplayScoringEngine(db, HeuristicReplayScorer()).build(
        price_source="yahoo_chart_daily_adjclose",
        horizon="1D",
        limit=10,
    )

    rows = db.replay_model_score_rows(scorer_name="heuristic", limit=10)
    summary = calibration_summary(rows, bin_count=5)

    assert summary["rows"] == 2
    assert summary["evaluated"] == 2
    assert summary["direction_accuracy"] == 0.5
    assert len(summary["bins"]) == 5
    assert "up" in summary["direction_distribution"]


def test_replay_score_action_gate_summary_excludes_observe_only_directional_rows() -> None:
    rows = [
        {
            "predicted_direction": "up",
            "direction_hit": 1,
            "actual_raw_return": 0.03,
            "actual_excess_return": 0.02,
            "confidence": 0.55,
            "horizon": "1D",
            "raw_json": json.dumps({"debias": {"trade_action": "directional_signal"}}),
        },
        {
            "predicted_direction": "down",
            "direction_hit": 1,
            "actual_raw_return": -0.02,
            "actual_excess_return": -0.01,
            "confidence": 0.50,
            "horizon": "1D",
            "raw_json": json.dumps({"debias": {"trade_action": "directional_signal"}}),
        },
        {
            "predicted_direction": "up",
            "direction_hit": 0,
            "actual_raw_return": -0.04,
            "actual_excess_return": -0.03,
            "confidence": 0.40,
            "horizon": "5D",
            "raw_json": json.dumps(
                {"debias": {"trade_action": "observe_only", "observe_only_reason": "low_edge_directional_signal"}}
            ),
        },
        {
            "predicted_direction": "flat",
            "direction_hit": 1,
            "actual_raw_return": 0.0,
            "actual_excess_return": 0.0,
            "confidence": 0.35,
            "horizon": "5D",
            "raw_json": json.dumps({"debias": {"observe_only": True, "observe_only_reason": "v10_flat_no_edge"}}),
        },
    ]

    summary = action_gate_summary(rows)

    assert summary["all_directional"]["directional_rows"] == 3
    assert summary["all_directional"]["directional_hits"] == 2
    assert summary["tradeable_directional"]["directional_rows"] == 2
    assert summary["tradeable_directional"]["directional_hits"] == 2
    assert summary["observe_only_directional"]["directional_rows"] == 1
    assert summary["observe_only_directional"]["avg_signed_excess_return"] == -0.03
    assert summary["directional_accuracy_delta"] == 1.0 - (2 / 3)
    report = action_gate_markdown(rows)
    assert "Replay Score Action-Gate Report" in report
    assert "tradeable_directional" in report
    assert "low_edge_directional_signal" in report


def test_replay_market_overlay_uses_only_pre_event_geopolitical_signals() -> None:
    row = {
        "symbol": "NVDA",
        "replay_timestamp": "2026-06-20T14:00:00+00:00",
        "horizon": "1D",
        "predicted_direction": "up",
        "actual_direction": "down",
        "direction_hit": 0,
        "actual_raw_return": -0.04,
        "actual_excess_return": -0.035,
    }
    pre_event_signal = {
        "source": "gdelt",
        "title": "Taiwan military escalation raises semiconductor supply chain risk",
        "published_at": "2026-06-20T10:00:00+00:00",
        "region": "taiwan_china",
        "theme": "semiconductor_supply_chain",
        "sentiment_score": -0.65,
        "sentiment_label": "negative",
        "confidence": 0.86,
        "assets_json": json.dumps(["TSM", "SMH", "SOXX", "NVDA", "QQQ"]),
        "market_reaction_json": json.dumps(
            {"technical_indicators": [{"symbol": "SMH", "as_of_date": "2026-06-20", "return_1d": -0.025}]}
        ),
        "prediction_gate_status": "eligible",
    }
    future_signal = {
        **pre_event_signal,
        "title": "Future Taiwan escalation article",
        "published_at": "2026-06-21T10:00:00+00:00",
    }

    overlay = overlay_for_score(row, [future_signal, pre_event_signal])
    summary = market_overlay_summary([row], [future_signal, pre_event_signal])
    report = market_overlay_markdown([row], [future_signal, pre_event_signal])

    assert overlay["overlay_state"] == "asset_specific_shock"
    assert overlay["overlay_action"] == "risk_reduce"
    assert overlay["source"] == "gdelt"
    assert overlay["matched_signal_count"] == 1
    assert "NVDA" in overlay["asset_overlap"]
    assert "NVDA" in overlay["specific_asset_overlap"]
    assert summary["overlay_coverage"] == 1.0
    assert summary["risk_overlay_coverage"] == 1.0
    assert "Future Taiwan escalation article" not in report
    assert "Taiwan military escalation" in report


def test_replay_emergency_overlay_tuning_groups_conflict_and_confirmation() -> None:
    signals = [
        {
            "source": "emergency_breaking_news",
            "title": "Breaking: Taiwan blockade threat hits advanced semiconductor supply chain",
            "published_at": "2026-06-20T10:00:00+00:00",
            "region": "taiwan_china",
            "theme": "semiconductor_supply_chain",
            "sentiment_score": -0.72,
            "sentiment_label": "negative",
            "confidence": 0.90,
            "assets_json": json.dumps(["NVDA", "SMH", "SOXX", "QQQ", "VIX"]),
            "market_reaction_json": json.dumps({"technical_indicators": []}),
            "prediction_gate_status": "eligible",
            "raw_json": json.dumps(
                {
                    "emergency": True,
                    "urgency": {"urgency": "critical"},
                    "latency_policy": "immediate_overlay_then_full_analysis",
                    "system_action": "immediate_scorer_alert",
                }
            ),
        }
    ]
    rows = [
        {
            "symbol": "NVDA",
            "replay_timestamp": "2026-06-20T10:30:00+00:00",
            "horizon": "1D",
            "predicted_direction": "up",
            "actual_direction": "down",
            "direction_hit": 0,
            "actual_raw_return": -0.03,
            "actual_excess_return": -0.025,
            "raw_json": json.dumps({"debias": {"method": "daily_v14_kept_v13_gate"}}),
        },
        {
            "symbol": "NVDA",
            "replay_timestamp": "2026-06-20T10:30:00+00:00",
            "horizon": "1D",
            "predicted_direction": "down",
            "actual_direction": "down",
            "direction_hit": 1,
            "actual_raw_return": -0.03,
            "actual_excess_return": -0.025,
            "raw_json": json.dumps({"debias": {"method": "daily_v14_tri_desk_confidence_haircut_v1"}}),
        },
    ]

    summary = emergency_overlay_summary(rows, signals)
    report = emergency_overlay_markdown(rows, signals)

    assert summary["rows"] == 2
    assert summary["emergency_rows"]["rows"] == 2
    assert summary["conflict_rows"]["rows"] == 1
    assert summary["confirmation_rows"]["rows"] == 1
    assert {item["bucket"] for item in summary["by_relation"]} >= {
        "emergency_conflicts_up",
        "emergency_confirms_down",
    }
    assert "critical_fresh_conflict_second_pass" in report
    assert "Replay Emergency Breaking-News Overlay Tuning Report" in report


def test_replay_evidence_packet_combines_overlay_macro_and_market_regime(tmp_path: Path) -> None:
    db = AnalysisDatabase(tmp_path / "evidence-packet.db")
    row = {
        "symbol": "NVDA",
        "event_type": "earnings",
        "event_subtype": "quarterly_results",
        "horizon": "1D",
        "session": "regular",
        "replay_timestamp": "2026-06-20T14:00:00+00:00",
        "prediction_cutoff_at": "2026-06-20T14:00:00+00:00",
        "predicted_direction": "up",
        "actual_direction": "down",
        "direction_hit": 0,
        "actual_excess_return": -0.04,
    }
    signal = {
        "source": "gdelt",
        "title": "Taiwan chip export controls raise semiconductor supply-chain risk",
        "published_at": "2026-06-20T10:00:00+00:00",
        "region": "taiwan_china",
        "theme": "semiconductor_supply_chain",
        "sentiment_label": "negative",
        "sentiment_score": -0.72,
        "confidence": 0.86,
        "prediction_gate_status": "eligible",
        "assets_json": json.dumps(["NVDA", "SMH", "SOXX", "QQQ"]),
        "market_reaction_json": json.dumps({"technical_indicators": [{"symbol": "SOXX", "return_1d": -0.03}]}),
    }
    future_signal = {
        **signal,
        "title": "Future Taiwan headline should not enter packet",
        "published_at": "2026-06-21T10:00:00+00:00",
    }
    for series_id, value in [("DGS10", 4.7), ("DGS2", 4.9), ("VIXCLS", 24.0), ("DTWEXBGS", 126.0)]:
        db.upsert_fred_observation(
            SimpleNamespace(
                series_id=series_id,
                observation_date="2026-06-19",
                value=value,
                realtime_start="2026-06-19",
                realtime_end="2026-06-19",
                source_url=f"https://fred.test/{series_id}",
                source_hash=f"fred-{series_id}",
                ingestion_version="test",
            )
        )
    for symbol, return_1d, return_5d in [
        ("SPY", -0.014, -0.031),
        ("QQQ", -0.018, -0.040),
        ("VIX", 0.12, 0.20),
        ("NVDA", -0.025, -0.060),
        ("SMH", -0.020, -0.055),
    ]:
        db.upsert_technical_indicator(
            SimpleNamespace(
                symbol=symbol,
                as_of_date="2026-06-19",
                price_source="yahoo",
                close=100.0,
                return_1d=return_1d,
                return_5d=return_5d,
                return_20d=return_5d,
                sma_20=101.0,
                sma_50=102.0,
                ema_12=100.0,
                ema_26=101.0,
                macd=-0.4,
                macd_signal=-0.2,
                rsi_14=35.0,
                volatility_20=0.3,
                source_start_date="2026-05-01",
                source_end_date="2026-06-19",
                ingestion_version="test",
            )
        )

    packet = build_overlay_evidence_packet(row, [future_signal, signal], db)
    summary = evidence_packet_summary([row], [future_signal, signal], db)
    report = evidence_packet_markdown([row], [future_signal, signal], db)

    assert packet["overlay"]["overlay_state"] == "asset_specific_shock"
    assert packet["market_regime"]["regime"] == "risk_off"
    assert packet["economic_indicator_desk"]["risk_bias"] in {"macro_mixed", "macro_risk_off"}
    assert packet["packet_version"] == EVIDENCE_PACKET_VERSION
    assert packet["tri_desk_alignment"]["alignment"] in {"contradicted", "conflicted"}
    assert packet["tri_desk_alignment"]["scorer_action"] in {"second_pass_required", "confidence_haircut"}
    assert packet["tri_desk_alignment"]["conflict_score"] >= 0.40
    assert "market_data_desk" in packet["tri_desk_alignment"]["vote_summary"]
    assert "Economic Indicator Desk:" in packet["scorer_injection_text"]
    assert "Tri-desk alignment:" in packet["scorer_injection_text"]
    assert packet["packet_strength"] == "rich"
    assert packet["anti_leakage"]["contains_actual_outcome"] is False
    assert "Stored prediction is up while overlay/regime is defensive" in packet["scorer_injection_text"]
    assert "Future Taiwan headline" not in packet["scorer_injection_text"]
    assert summary["macro_coverage"] == 1.0
    assert summary["technical_coverage"] == 1.0
    assert summary["tri_desk_conflict_rows"]["rows"] == 1
    assert summary["by_tri_desk_action"]
    assert "Example scorer injection text" in report
    assert "By tri-desk scorer action" in report
    assert "Actual direction" not in packet["scorer_injection_text"]


def test_replay_score_oos_summary_splits_by_temporal_cutoff(tmp_path: Path) -> None:
    db = AnalysisDatabase(tmp_path / "score-oos.db")
    timestamps = [
        "2026-02-01T20:00:00+00:00",
        "2026-03-01T20:00:00+00:00",
        "2026-05-01T20:00:00+00:00",
        "2026-06-01T20:00:00+00:00",
    ]
    events = [
        _add_replay_event(
            db,
            "MU",
            f"oos-{index}",
            timestamp,
            content="Micron raises outlook on stronger growth.",
        )
        for index, timestamp in enumerate(timestamps, start=1)
    ]
    for event in events:
        db.upsert_replay_market_outcome(
            _outcome(event, source="ibkr_historical_trades_5_mins_all_sessions", horizon="2H", raw=0.02, excess=0.01)
        )

    for index, row in enumerate(db.replay_dataset_export_rows(limit=10)):
        base = HeuristicReplayScorer().score(row)
        alpha_hit = 1 if index in {0, 1, 2} else 0
        beta_hit = 1 if index in {0, 1} else 0
        db.upsert_replay_model_score(
            replace(
                base,
                scorer_name="test",
                model_name="alpha",
                scorer_version="alpha-v1",
                direction_hit=alpha_hit,
            )
        )
        db.upsert_replay_model_score(
            replace(
                base,
                scorer_name="test",
                model_name="beta",
                scorer_version="beta-v1",
                direction_hit=beta_hit,
            )
        )

    rows = db.replay_model_score_rows(
        price_source="ibkr_historical_trades_5_mins_all_sessions",
        horizon="2H",
        scorer_name="test",
        limit=20,
    )
    summary = oos_summary(rows, cutoff="2026-04-01T00:00:00+00:00", min_holdout_rows=2)

    assert summary["train"]["evaluated"] == 4
    assert summary["holdout"]["evaluated"] == 4
    assert len(summary["groups"]) == 2
    assert summary["groups"][0]["model_name"] == "alpha"
    assert summary["groups"][0]["train"]["direction_accuracy"] == 1.0
    assert summary["groups"][0]["holdout"]["direction_accuracy"] == 0.5
    assert summary["groups"][0]["holdout_accuracy_minus_train"] == -0.5


def test_replay_score_oos_markdown_report_shape(tmp_path: Path) -> None:
    db = AnalysisDatabase(tmp_path / "score-oos-markdown.db")
    before = _add_replay_event(
        db,
        "MU",
        "oos-md-before",
        "2026-02-01T20:00:00+00:00",
        content="Micron raises guidance.",
    )
    after = _add_replay_event(
        db,
        "MU",
        "oos-md-after",
        "2026-05-01T20:00:00+00:00",
        content="Micron raises guidance.",
    )
    for event in (before, after):
        db.upsert_replay_market_outcome(
            _outcome(event, source="yahoo_chart_daily_adjclose", horizon="1D", raw=0.02, excess=0.01)
        )
    for row in db.replay_dataset_export_rows(limit=10):
        db.upsert_replay_model_score(HeuristicReplayScorer().score(row))

    rows = db.replay_model_score_rows(price_source="yahoo_chart_daily_adjclose", horizon="1D", limit=10)
    report = oos_markdown(
        rows,
        cutoff="2026-04-01T00:00:00+00:00",
        filters={"price_source": "yahoo_chart_daily_adjclose"},
    )

    assert "# Replay Score Temporal Holdout Report" in report
    assert "`price_source`: `yahoo_chart_daily_adjclose`" in report
    assert "| Rank | Scorer | Model |" in report
    assert "Safety: read-only temporal split" in report


def test_replay_score_pair_comparison_summarizes_candidate_delta(tmp_path: Path) -> None:
    db = AnalysisDatabase(tmp_path / "score-pair-comparison.db")
    up = _add_replay_event(
        db,
        "QCOM",
        "pair-up",
        "2026-04-29T20:02:20+00:00",
        content="Qualcomm raises outlook on stronger demand.",
    )
    down_win = _add_replay_event(
        db,
        "CRM",
        "pair-down-win",
        "2026-05-28T20:03:20+00:00",
        content="Salesforce warns about softer demand.",
    )
    down_loss = _add_replay_event(
        db,
        "IBM",
        "pair-down-loss",
        "2026-04-22T20:10:35+00:00",
        content="IBM reports mixed results.",
    )
    db.upsert_replay_market_outcome(
        _outcome(up, source="ibkr_historical_trades_5_mins_all_sessions", horizon="30M", raw=0.03, excess=0.02)
    )
    db.upsert_replay_market_outcome(
        _outcome(down_win, source="ibkr_historical_trades_5_mins_all_sessions", horizon="30M", raw=-0.03, excess=-0.02)
    )
    db.upsert_replay_market_outcome(
        _outcome(down_loss, source="ibkr_historical_trades_5_mins_all_sessions", horizon="2H", raw=-0.02, excess=-0.01)
    )
    rows_by_symbol = {
        row["actual_subject_symbol"]: row
        for row in db.replay_dataset_export_rows(
            price_source="ibkr_historical_trades_5_mins_all_sessions",
            limit=10,
        )
    }
    variants = {
        "QCOM": ("down", "up"),
        "CRM": ("up", "down"),
        "IBM": ("down", "up"),
    }
    for symbol, (base_prediction, candidate_prediction) in variants.items():
        row = rows_by_symbol[symbol]
        db.upsert_replay_model_score(
            _score_variant(
                row,
                predicted_direction=base_prediction,
                scorer_version="openai-replay-score-v5",
            )
        )
        db.upsert_replay_model_score(
            _score_variant(
                row,
                predicted_direction=candidate_prediction,
                scorer_version="openai-replay-score-v6",
            )
        )

    rows = db.replay_score_pair_comparison_rows(
        price_source="ibkr_historical_trades_5_mins_all_sessions",
        base_scorer_name="openai",
        base_scorer_version="openai-replay-score-v5",
        candidate_scorer_name="openai",
        candidate_scorer_version="openai-replay-score-v6",
        limit=10,
    )
    summary = pair_comparison_summary(rows)

    assert summary["evaluated"] == 3
    assert summary["base_hits"] == 1
    assert summary["candidate_hits"] == 2
    assert summary["net_hits"] == 1
    assert summary["candidate_wins"] == 2
    assert summary["candidate_losses"] == 1
    assert summary["changed_predictions"] == 3
    assert {item["bucket"] for item in summary["by_horizon"]} == {"30M", "2H"}
    assert summary["prediction_changes"]


def test_replay_score_pair_comparison_markdown_shape(tmp_path: Path) -> None:
    db = AnalysisDatabase(tmp_path / "score-pair-markdown.db")
    event = _add_replay_event(
        db,
        "QCOM",
        "pair-md",
        "2026-04-29T20:02:20+00:00",
        content="Qualcomm raises outlook on stronger demand.",
    )
    db.upsert_replay_market_outcome(
        _outcome(event, source="ibkr_historical_trades_5_mins_all_sessions", horizon="30M", raw=0.03, excess=0.02)
    )
    row = db.replay_dataset_export_rows(price_source="ibkr_historical_trades_5_mins_all_sessions", limit=1)[0]
    db.upsert_replay_model_score(
        _score_variant(row, predicted_direction="down", scorer_version="openai-replay-score-v5")
    )
    db.upsert_replay_model_score(
        _score_variant(row, predicted_direction="up", scorer_version="openai-replay-score-v6")
    )

    rows = db.replay_score_pair_comparison_rows(
        price_source="ibkr_historical_trades_5_mins_all_sessions",
        base_scorer_name="openai",
        base_scorer_version="openai-replay-score-v5",
        candidate_scorer_name="openai",
        candidate_scorer_version="openai-replay-score-v6",
        limit=10,
    )
    report = pair_comparison_markdown(
        rows,
        filters={"base_scorer_version": "openai-replay-score-v5"},
    )

    assert "# Replay Score Paired Comparison Report" in report
    assert "Candidate wins/losses" in report
    assert "## Prediction changes" in report
    assert "Safety: read-only paired comparison" in report


def test_replay_score_error_analysis_breaks_down_misses(tmp_path: Path) -> None:
    db = AnalysisDatabase(tmp_path / "score-error-analysis.db")
    up = _add_replay_event(
        db,
        "QCOM",
        "error-up",
        "2026-04-29T20:02:20+00:00",
        content="Qualcomm raises outlook on stronger demand.",
    )
    down = _add_replay_event(
        db,
        "INTU",
        "error-down",
        "2026-05-20T20:05:49+00:00",
        content="Intuit reports growth but warns about slowing demand.",
    )
    flat = _add_replay_event(
        db,
        "IBM",
        "error-flat",
        "2026-04-22T20:10:35+00:00",
        content="IBM reports mixed results.",
    )
    db.upsert_replay_market_outcome(
        _outcome(up, source="ibkr_historical_trades_5_mins_all_sessions", horizon="30M", raw=0.04, excess=0.03)
    )
    db.upsert_replay_market_outcome(
        _outcome(down, source="ibkr_historical_trades_5_mins_all_sessions", horizon="30M", raw=-0.03, excess=-0.02)
    )
    db.upsert_replay_market_outcome(
        _outcome(flat, source="ibkr_historical_trades_5_mins_all_sessions", horizon="2H", raw=0.0, excess=0.0)
    )
    for index, row in enumerate(db.replay_dataset_export_rows(limit=10)):
        base = HeuristicReplayScorer().score(row)
        hit = 1 if row["actual_subject_symbol"] == "QCOM" else 0
        predicted = row["direction_label"] if hit else "up"
        db.upsert_replay_model_score(
            replace(
                base,
                scorer_name="openai",
                model_name="gpt-test",
                scorer_version="openai-replay-score-v5",
                predicted_direction=predicted,
                direction_hit=hit,
                confidence=0.35,
            )
        )

    rows = db.replay_model_score_analysis_rows(
        symbols=("QCOM", "INTU", "IBM"),
        price_source="ibkr_historical_trades_5_mins_all_sessions",
        scorer_name="openai",
        scorer_version="openai-replay-score-v5",
        limit=10,
    )
    summary = error_analysis_summary(rows)

    assert summary["evaluated"] == 3
    assert summary["hits"] == 1
    assert summary["misses"] == 2
    assert summary["confusion"]["down"]["up"] == 1
    assert summary["confusion"]["flat"]["up"] == 1
    assert {item["bucket"] for item in summary["by_horizon"]} == {"30M", "2H"}
    assert summary["top_misses"]


def test_replay_score_error_analysis_markdown_shape(tmp_path: Path) -> None:
    db = AnalysisDatabase(tmp_path / "score-error-analysis-markdown.db")
    event = _add_replay_event(
        db,
        "QCOM",
        "error-md",
        "2026-04-29T20:02:20+00:00",
        content="Qualcomm raises outlook on stronger demand.",
    )
    db.upsert_replay_market_outcome(
        _outcome(event, source="ibkr_historical_trades_5_mins_all_sessions", horizon="30M", raw=-0.02, excess=-0.01)
    )
    row = db.replay_dataset_export_rows(limit=1)[0]
    base = HeuristicReplayScorer().score(row)
    db.upsert_replay_model_score(
        replace(
            base,
            scorer_name="openai",
            model_name="gpt-test",
            scorer_version="openai-replay-score-v5",
            predicted_direction="up",
            direction_hit=0,
            confidence=0.35,
        )
    )

    rows = db.replay_model_score_analysis_rows(scorer_name="openai", scorer_version="openai-replay-score-v5", limit=10)
    report = error_analysis_markdown(
        rows,
        filters={"symbols": "QCOM", "scorer_version": "openai-replay-score-v5"},
    )

    assert "# Replay Score Error Analysis Report" in report
    assert "## Confusion matrix" in report
    assert "## By symbol" in report
    assert "## Largest misses" in report
    assert "Safety: read-only error analysis" in report


def test_replay_surprise_adjustment_notes_capture_mismatch_reasons(tmp_path: Path) -> None:
    db = AnalysisDatabase(tmp_path / "surprise-adjustments.db")
    event = _add_replay_event(
        db,
        "QCOM",
        "surprise-flat",
        "2026-04-29T20:02:20+00:00",
        content="Qualcomm raises outlook but valuation and demand commentary are mixed.",
    )
    db.upsert_replay_market_outcome(
        _outcome(event, source="ibkr_historical_trades_5_mins_all_sessions", horizon="30M", raw=0.0, excess=0.0)
    )
    row = db.replay_dataset_export_rows(limit=1)[0]
    base = HeuristicReplayScorer().score(row)
    db.upsert_replay_model_score(
        replace(
            base,
            scorer_name="openai",
            model_name="gpt-test",
            scorer_version="openai-replay-score-v6",
            predicted_direction="up",
            expected_return=0.012,
            direction_hit=0,
            confidence=0.41,
            absolute_raw_error=0.012,
            absolute_excess_error=0.012,
        )
    )

    result = ReplaySurpriseAdjustmentEngine(db).build(
        scorer_name="openai",
        scorer_version="openai-replay-score-v6",
        limit=10,
    )
    notes = db.recent_replay_surprise_adjustment_notes(limit=10)

    assert result["created"] == 1
    assert notes[0]["mismatch_type"] == "predicted_up_actual_flat"
    assert notes[0]["reason_category"] == "surprise_absorbed_no_edge"
    assert "flat probability" in notes[0]["recommended_adjustment"]
    evidence = json.loads(notes[0]["evidence_json"])
    assert "market_data_desk" in evidence
    assert "news_desk" in evidence
    assert "sec_filings_desk" in evidence
    assert "financial_statements_desk" in evidence
    assert "fred_macro_desk" in evidence
    assert "technical_indicators_desk" in evidence
    assert "external_geopolitical_news_desk" in evidence
    assert "Replay surprise adjustment notes" in format_adjustment_report(notes)
    assert "Replay Surprise Adjustment Notes" in adjustment_markdown(notes)


def test_geopolitical_surprise_adjustment_uses_region_theme_channel_context() -> None:
    snapshot = {
        "headline": "Middle East missile strike raises Hormuz oil supply risk",
        "analysis_profile": "geopolitical",
        "deterministic_event_hint": "geopolitical",
        "event_scope_hint": "global",
        "target_assets": ["USO", "XLE", "GLD", "VIX"],
        "source_desk_context": {
            "market_data_desk": {"target_quote_available": True, "target_quotes": [{"symbol": "USO"}]},
            "news_desk": {"provider": "BZ", "source_tier_hint": "licensed_news"},
            "sec_filings_desk": {"attached_count": 0},
            "financial_statements_desk": {"fact_count": 0},
            "fred_macro_desk": {"series_count": 2},
            "technical_indicators_desk": {"indicator_count": 1},
            "external_geopolitical_news_desk": {
                "signals": [
                    {
                        "source": "marketaux",
                        "region": "middle_east",
                        "theme": "energy_supply",
                        "sentiment_label": "negative",
                        "assets": ["USO", "XLE", "XOM", "CVX", "GLD", "VIX"],
                        "market_reaction_status": "available",
                        "prediction_gate_status": "eligible",
                        "title": "Oil rises after Middle East escalation near Hormuz",
                    }
                ],
                "signal_count": 1,
            },
        },
    }
    row = {
        "score_id": 1,
        "outcome_id": 2,
        "replay_corpus_id": 3,
        "symbol": "USO",
        "event_type": "geopolitical",
        "event_subtype": "energy_supply",
        "replay_timestamp": "2026-06-24T12:00:00+00:00",
        "horizon": "2H",
        "price_source": "ibkr_historical_trades_5_mins_all_sessions",
        "scorer_name": "openai",
        "model_name": "gpt-test",
        "scorer_version": "openai-replay-score-v6",
        "predicted_direction": "up",
        "actual_direction": "flat",
        "expected_return": 0.012,
        "actual_raw_return": 0.0,
        "actual_excess_return": 0.0,
        "confidence": 0.42,
        "absolute_raw_error": 0.012,
        "absolute_excess_error": 0.012,
        "rationale": "Oil and volatility should rise on escalation risk.",
        "input_snapshot_json": json.dumps(snapshot),
    }

    note = make_surprise_adjustment_note(row)

    assert note.reason_category == "geopolitical_risk_absorbed_no_edge"
    assert "middle_east" in note.reason_summary
    assert "energy_supply" in note.reason_summary
    assert "USO" in note.reason_summary
    assert note.feature_hints["geopolitical_context"]["market_confirmed"] is True
    assert {"USO", "XLE", "GLD", "VIX"}.issubset(set(note.feature_hints["geopolitical_context"]["asset_overlap"]))


def test_geopolitical_surprise_adjustment_flags_weak_source_confirmation() -> None:
    snapshot = {
        "headline": "Unconfirmed cyberattack rumor hits Asian semiconductor supply chain",
        "analysis_profile": "geopolitical",
        "target_assets": ["SOXX", "QQQ"],
        "source_desk_context": {
            "market_data_desk": {"target_quote_available": False},
            "news_desk": {"provider": "unknown", "source_tier_hint": "rumor"},
            "sec_filings_desk": {"attached_count": 0},
            "financial_statements_desk": {"fact_count": 0},
            "fred_macro_desk": {"series_count": 0},
            "technical_indicators_desk": {"indicator_count": 0},
            "external_geopolitical_news_desk": {"signals": [], "signal_count": 0},
        },
    }
    row = {
        "score_id": 10,
        "outcome_id": 20,
        "replay_corpus_id": 30,
        "symbol": "SOXX",
        "event_type": "geopolitical",
        "event_subtype": "cybersecurity",
        "replay_timestamp": "2026-06-24T12:00:00+00:00",
        "horizon": "30M",
        "price_source": "ibkr_historical_trades_5_mins_all_sessions",
        "scorer_name": "openai",
        "model_name": "gpt-test",
        "scorer_version": "openai-replay-score-v6",
        "predicted_direction": "down",
        "actual_direction": "up",
        "expected_return": -0.006,
        "actual_raw_return": 0.008,
        "actual_excess_return": 0.007,
        "confidence": 0.35,
        "absolute_raw_error": 0.014,
        "absolute_excess_error": 0.013,
        "rationale": "Cyberattack rumor should pressure chips.",
        "input_snapshot_json": json.dumps(snapshot),
    }

    note = make_surprise_adjustment_note(row)

    assert note.reason_category == "geopolitical_source_confirmation_weak"
    assert "weak-source-confirmation" in note.reason_summary
    assert "source desk confirms" in note.recommended_adjustment
    assert note.feature_hints["geopolitical_context"]["source_count"] == 0


def test_openai_surprise_adjustment_analyzer_persists_structured_cause(tmp_path: Path) -> None:
    db = AnalysisDatabase(tmp_path / "surprise-openai.db")
    event = _add_replay_event(
        db,
        "MU",
        "surprise-openai",
        "2026-06-24T20:02:01+00:00",
        content="Micron reports stronger revenue but guidance details are mixed.",
    )
    db.upsert_replay_market_outcome(
        _outcome(event, source="ibkr_historical_trades_5_mins_all_sessions", horizon="2H", raw=-0.03, excess=-0.02)
    )
    row = db.replay_dataset_export_rows(limit=1)[0]
    base = HeuristicReplayScorer().score(row)
    db.upsert_replay_model_score(
        replace(
            base,
            scorer_name="openai",
            model_name="gpt-test",
            scorer_version="openai-replay-score-v6",
            predicted_direction="up",
            expected_return=0.01,
            direction_hit=0,
            confidence=0.40,
            absolute_raw_error=0.04,
            absolute_excess_error=0.03,
        )
    )
    parsed = OpenAISurpriseCauseAnalysis(
        reason_category="multi_source_conflict",
        reason_summary="OpenAI diagnostic says the positive headline conflicted with mixed guidance and weak reaction context.",
        recommended_adjustment="Downweight headline-positive events when source desks show mixed quality or missing guidance confirmation.",
        evidence_source_weights=[
            EvidenceSourceWeight(source="news_desk", weight=0.35),
            EvidenceSourceWeight(source="sec_filings_desk", weight=0.30),
            EvidenceSourceWeight(source="technical_indicators_desk", weight=0.20),
        ],
        missing_evidence=["consensus guidance"],
        pattern_tags=["headline_positive_but_quality_mixed"],
        scorer_rule_candidate="Require quality confirmation before converting positive headline tone into an up forecast.",
        confidence=0.66,
    )

    class FakeResponses:
        def parse(self, **kwargs):
            assert kwargs["model"] == "gpt-test-cause"
            assert kwargs["reasoning"] == {"effort": "low"}
            assert kwargs["text_format"] is OpenAISurpriseCauseAnalysis
            assert kwargs["store"] is False
            user_payload = json.loads(kwargs["input"][1]["content"])
            assert "evidence_sources" in user_payload
            assert "technical_indicators_desk" in user_payload["evidence_sources"]
            assert user_payload["score"]["actual_direction"] == "down"
            return SimpleNamespace(
                id="resp_surprise_test",
                output=[SimpleNamespace(type="message", content=[SimpleNamespace(type="output_text", parsed=parsed)])],
            )

    analyzer = OpenAISurpriseAdjustmentAnalyzer(
        api_key="test",
        model="gpt-test-cause",
        timeout_seconds=30,
        reasoning_effort="low",
        client=SimpleNamespace(responses=FakeResponses()),
    )
    result = ReplaySurpriseAdjustmentEngine(db, analyzer=analyzer).build(
        scorer_name="openai",
        scorer_version="openai-replay-score-v6",
        limit=10,
    )
    notes = db.recent_replay_surprise_adjustment_notes(limit=10)

    assert result["analysis_provider"] == "openai"
    assert result["created"] == 1
    assert notes[0]["analysis_provider"] == "openai"
    assert notes[0]["analysis_model"] == "gpt-test-cause"
    assert notes[0]["reason_category"] == "multi_source_conflict"
    hints = json.loads(notes[0]["feature_hints_json"])
    assert {"source": "news_desk", "weight": 0.35} in hints["openai_evidence_source_weights"]


def test_v14_nuanced_action_postscore_keeps_confidence_haircut_tradeable() -> None:
    row = {
        "replay_corpus_id": 1,
        "outcome_id": 88,
        "historical_event_id": 3,
        "official_document_id": 4,
        "symbol": "NVDA",
        "event_type": "earnings",
        "event_subtype": "",
        "replay_timestamp": "2026-04-22T20:10:35+00:00",
        "prediction_cutoff_at": "2026-04-22T20:10:35+00:00",
        "horizon": "1D",
        "price_source": "yahoo_chart_daily_adjclose",
        "scorer_name": "openai",
        "model_name": "gpt-5.4-mini",
        "scorer_version": "openai-replay-score-v14",
        "input_snapshot_hash": "hash-v14",
        "prediction_status": "complete",
        "predicted_direction": "up",
        "expected_return": 0.012,
        "bear_return": -0.012,
        "bull_return": 0.030,
        "up_probability": 0.48,
        "flat_probability": 0.30,
        "down_probability": 0.22,
        "confidence": 0.50,
        "rationale": "Stored v14 up call.",
        "raw_json": json.dumps({"debias": {"trade_action": "directional_signal", "observe_only": False}}),
        "actual_direction": "down",
        "actual_raw_return": -0.02,
        "actual_excess_return": -0.025,
        "direction_hit": 0,
        "absolute_raw_error": 0.032,
        "absolute_excess_error": 0.037,
    }
    evidence_packet = {
        "overlay": {"overlay_state": "normal", "overlay_action": "none", "matched_signal_count": 0},
        "standing_regime_overlay": {"matched_regime_count": 0, "active_regimes": []},
        "economic_indicator_desk": {"risk_bias": "macro_neutral"},
    }
    policy = {
        "market_news_primary|symbol=NVDA|pred=up": {
            "pattern_key": "market_news_primary|symbol=NVDA|pred=up",
            "track": "market_news_primary",
            "action": "confidence_haircut",
            "seen": 40,
            "hits": 18,
            "hit_rate": 0.45,
            "flat_rate": 0.0,
            "source_cycle": 4,
            "reason": "fixture learned weak-but-tradeable pattern",
        }
    }

    score = transform_v14_nuanced_action_postscore(row, evidence_packet, policy, policy_source="fixture")
    raw = json.loads(score.raw_json)

    assert score.scorer_version == OPENAI_REPLAY_SCORER_VERSION_V14_NUANCED_V1
    assert score.predicted_direction == "up"
    assert score.confidence < row["confidence"]
    assert raw["debias"]["method"] == "daily_v14_nuanced_v1_confidence_haircut"
    assert raw["debias"]["trade_action"] == "confidence_haircut"
    assert raw["debias"]["observe_only"] is False
    assert raw["debias"]["combined_gate"]["confidence_haircut_tradeable"] is True
    assert raw["debias"]["nuanced_policy_matches"][0]["key"] == "market_news_primary|symbol=NVDA|pred=up"


def test_variant_comparison_summary_ranks_gate_candidate_on_common_rows() -> None:
    def row(
        outcome_id: int,
        *,
        version: str,
        hit: int,
        action: str = "directional_signal",
    ) -> dict:
        observe_only = action == "observe_only"
        return {
            "outcome_id": outcome_id,
            "symbol": "NVDA",
            "horizon": "1D",
            "price_source": "yahoo_chart_daily_adjclose",
            "replay_timestamp": f"2026-01-0{outcome_id}T14:00:00+00:00",
            "scorer_name": "openai",
            "model_name": "gpt-test",
            "scorer_version": version,
            "predicted_direction": "up",
            "actual_direction": "up" if hit else "down",
            "direction_hit": hit,
            "confidence": 0.50,
            "actual_raw_return": 0.02 if hit else -0.02,
            "actual_excess_return": 0.02 if hit else -0.02,
            "absolute_raw_error": 0.01,
            "absolute_excess_error": 0.01,
            "raw_json": json.dumps(
                {
                    "debias": {
                        "method": f"{version}_{action}",
                        "trade_action": action,
                        "observe_only": observe_only,
                        "observe_only_reason": "fixture_gate" if observe_only else "",
                    }
                }
            ),
        }

    base_rows = [
        row(1, version="openai-replay-score-v14", hit=1),
        row(2, version="openai-replay-score-v14", hit=0),
        row(3, version="openai-replay-score-v14", hit=0),
    ]
    gated_rows = [
        row(1, version="openai-replay-score-v14-nuanced-v1", hit=1),
        row(2, version="openai-replay-score-v14-nuanced-v1", hit=0, action="observe_only"),
        row(3, version="openai-replay-score-v14-nuanced-v1", hit=1),
    ]

    summary = variant_comparison_summary(
        {
            "openai-replay-score-v14": base_rows,
            "openai-replay-score-v14-nuanced-v1": gated_rows,
        },
        base_variant="openai-replay-score-v14",
    )
    report = variant_comparison_markdown(summary, filters={"price_source": "yahoo_chart_daily_adjclose"})

    assert summary["common_rows"] == 3
    assert summary["best_variant"] == "openai-replay-score-v14-nuanced-v1"
    assert summary["variant_summaries"][0]["tradeable_directional_accuracy"] == 1.0
    assert summary["deltas_vs_base"][0]["tradeable_accuracy_delta"] > 0
    assert "Replay Score Variant Comparison Report" in report
    assert "Variant scoreboard" in report


def test_paper_action_simulation_converts_actions_to_paper_decisions() -> None:
    def score_row(outcome_id: int, *, action: str, hit: int, predicted: str = "up") -> dict:
        observe_only = action == "observe_only"
        return {
            "id": outcome_id,
            "outcome_id": outcome_id,
            "symbol": "NVDA",
            "horizon": "1D",
            "price_source": "yahoo_chart_daily_adjclose",
            "replay_timestamp": f"2026-01-0{outcome_id}T14:00:00+00:00",
            "predicted_direction": predicted,
            "actual_direction": "up" if hit else "down",
            "direction_hit": hit,
            "confidence": 0.50,
            "expected_return": 0.01,
            "actual_raw_return": 0.02 if hit else -0.02,
            "actual_excess_return": 0.02 if hit else -0.02,
            "raw_json": json.dumps(
                {
                    "debias": {
                        "method": action,
                        "trade_action": action,
                        "observe_only": observe_only,
                        "observe_only_reason": "fixture_review" if observe_only else "",
                    }
                }
            ),
        }

    rows = [
        score_row(1, action="directional_signal", hit=1),
        score_row(2, action="confidence_haircut", hit=1),
        score_row(3, action="observe_only", hit=0),
        score_row(4, action="directional_signal", hit=0, predicted="flat"),
    ]

    summary = paper_action_simulation_summary(
        rows,
        normal_size=1.0,
        haircut_size_multiplier=0.5,
    )
    report = paper_action_simulation_markdown(summary)

    assert summary["all_decisions"]["paper_trade_rows"] == 2
    assert summary["paper_trades"]["paper_trade_hits"] == 2
    assert summary["paper_trades"]["paper_trade_accuracy"] == 1.0
    assert summary["paper_trades"]["gross_size_units"] == 1.5
    buckets = {item["bucket"]: item for item in summary["by_paper_decision"]}
    assert buckets["paper_trade"]["paper_trade_rows"] == 1
    assert buckets["paper_trade_reduced_size"]["paper_trade_rows"] == 1
    assert buckets["review_only_no_trade"]["paper_trade_rows"] == 0
    assert "Replay Score Paper Action Simulation Report" in report


def test_paper_order_objects_are_candidate_or_blocked() -> None:
    def score_row(outcome_id: int, *, action: str, predicted: str, hit: int) -> dict:
        observe_only = action == "observe_only"
        return {
            "id": outcome_id,
            "outcome_id": outcome_id,
            "symbol": "NVDA",
            "horizon": "1D",
            "price_source": "yahoo_chart_daily_adjclose",
            "replay_timestamp": f"2026-01-0{outcome_id}T14:00:00+00:00",
            "scorer_name": "openai",
            "model_name": "gpt-test",
            "scorer_version": "openai-replay-score-v14-nuanced-v1",
            "predicted_direction": predicted,
            "actual_direction": predicted if hit else "down" if predicted == "up" else "up",
            "direction_hit": hit,
            "confidence": 0.50,
            "expected_return": 0.01 if predicted == "up" else -0.01 if predicted == "down" else 0.0,
            "actual_raw_return": 0.02 if hit else -0.02,
            "actual_excess_return": 0.02 if hit else -0.02,
            "raw_json": json.dumps(
                {
                    "debias": {
                        "method": action,
                        "trade_action": action,
                        "observe_only": observe_only,
                        "observe_only_reason": "fixture_review" if observe_only else "",
                    }
                }
            ),
        }

    rows = [
        score_row(1, action="directional_signal", predicted="up", hit=1),
        score_row(2, action="confidence_haircut", predicted="down", hit=1),
        score_row(3, action="observe_only", predicted="up", hit=0),
        score_row(4, action="directional_signal", predicted="flat", hit=0),
    ]

    summary = paper_order_objects_summary(
        rows,
        base_quantity=10.0,
        haircut_size_multiplier=0.5,
        stop_loss_pct=0.02,
        take_profit_pct=0.04,
        sample_limit=10,
    )
    report = paper_order_objects_markdown(summary)
    orders = summary["paper_orders"]
    candidates = [order for order in orders if order["order_status"] == "candidate"]
    blocked = [order for order in orders if order["order_status"] == "blocked"]

    assert len(candidates) == 2
    assert len(blocked) == 2
    assert candidates[0]["order_action"] == "BUY"
    assert candidates[0]["quantity_units"] == 10.0
    assert candidates[1]["order_action"] == "SELL"
    assert candidates[1]["quantity_units"] == 5.0
    assert all(order["paper_only"] is True for order in orders)
    assert all(order["safety"]["ibkr_order_submitted"] is False for order in orders)
    assert summary["candidate_orders"]["gross_quantity_units"] == 15.0
    assert "Replay Score Paper Order Object Report" in report


def test_paper_order_parameter_tuner_selects_candidate() -> None:
    def score_row(outcome_id: int, *, action: str, hit: int, confidence: float) -> dict:
        observe_only = action == "observe_only"
        return {
            "id": outcome_id,
            "outcome_id": outcome_id,
            "symbol": "NVDA",
            "horizon": "1D",
            "price_source": "yahoo_chart_daily_adjclose",
            "replay_timestamp": f"2026-01-0{outcome_id}T14:00:00+00:00",
            "scorer_name": "openai",
            "model_name": "gpt-test",
            "scorer_version": "openai-replay-score-v14-nuanced-v1",
            "predicted_direction": "up",
            "actual_direction": "up" if hit else "down",
            "direction_hit": hit,
            "confidence": confidence,
            "expected_return": 0.01,
            "actual_raw_return": 0.03 if hit else -0.03,
            "actual_excess_return": 0.03 if hit else -0.03,
            "raw_json": json.dumps(
                {
                    "debias": {
                        "method": action,
                        "trade_action": action,
                        "observe_only": observe_only,
                    }
                }
            ),
        }

    rows = [
        score_row(1, action="directional_signal", hit=1, confidence=0.50),
        score_row(2, action="directional_signal", hit=1, confidence=0.48),
        score_row(3, action="confidence_haircut", hit=0, confidence=0.32),
        score_row(4, action="confidence_haircut", hit=0, confidence=0.31),
        score_row(5, action="observe_only", hit=0, confidence=0.50),
    ]

    result = tune_paper_order_parameters(
        rows,
        min_confidence_grid=(0.0, 0.40),
        haircut_size_grid=(0.25, 0.75),
        normal_size_grid=(1.0,),
        min_candidate_rate=0.40,
        top_n=4,
    )
    report = paper_order_parameter_tuning_markdown(result)

    assert result["candidates_evaluated"] == 4
    assert result["selected"] is not None
    assert result["selected"]["params"]["min_confidence"] == 0.40
    assert result["selected"]["candidate_accuracy"] == 1.0
    assert result["selected"]["passes_min_candidate_rate"] is True
    assert "Paper Order Parameter Tuning Report" in report


def test_walk_forward_paper_order_parameter_tuning_uses_prior_rows() -> None:
    def score_row(outcome_id: int, *, hit: int, confidence: float) -> dict:
        return {
            "id": outcome_id,
            "outcome_id": outcome_id,
            "symbol": "NVDA",
            "horizon": "1D",
            "price_source": "yahoo_chart_daily_adjclose",
            "replay_timestamp": f"2026-01-{outcome_id:02d}T14:00:00+00:00",
            "scorer_name": "openai",
            "model_name": "gpt-test",
            "scorer_version": "openai-replay-score-v14-nuanced-v1",
            "predicted_direction": "up",
            "actual_direction": "up" if hit else "down",
            "direction_hit": hit,
            "confidence": confidence,
            "expected_return": 0.01,
            "actual_raw_return": 0.03 if hit else -0.03,
            "actual_excess_return": 0.03 if hit else -0.03,
            "raw_json": json.dumps(
                {
                    "debias": {
                        "method": "directional_signal",
                        "trade_action": "directional_signal",
                        "observe_only": False,
                    }
                }
            ),
        }

    rows = [
        score_row(1, hit=1, confidence=0.50),
        score_row(2, hit=1, confidence=0.48),
        score_row(3, hit=0, confidence=0.32),
        score_row(4, hit=0, confidence=0.31),
        score_row(5, hit=1, confidence=0.52),
        score_row(6, hit=1, confidence=0.47),
        score_row(7, hit=0, confidence=0.33),
        score_row(8, hit=0, confidence=0.30),
    ]

    result = walk_forward_paper_order_parameter_tuning(
        rows,
        min_train_rows=4,
        test_window_rows=4,
        min_confidence_grid=(0.0, 0.40),
        haircut_size_grid=(0.25,),
        normal_size_grid=(1.0,),
        min_candidate_rate=0.40,
    )
    report = walk_forward_paper_order_parameter_tuning_markdown(result)

    assert result["fold_count"] == 1
    assert result["folds"][0]["selected"]["params"]["min_confidence"] == 0.40
    assert result["aggregate_test_summary"]["candidate_orders"]["candidate_orders"] == 2
    assert result["aggregate_test_summary"]["candidate_orders"]["candidate_accuracy"] == 1.0
    assert "Walk-Forward Paper Order Parameter Tuning Report" in report


def test_paper_order_risk_layer_blocks_and_reduces_candidates() -> None:
    def score_row(outcome_id: int, *, symbol: str, hit: int, expected_return: float) -> dict:
        return {
            "id": outcome_id,
            "outcome_id": outcome_id,
            "symbol": symbol,
            "horizon": "1D",
            "price_source": "yahoo_chart_daily_adjclose",
            "replay_timestamp": f"2026-01-{outcome_id:02d}T14:00:00+00:00",
            "scorer_name": "openai",
            "model_name": "gpt-test",
            "scorer_version": "openai-replay-score-v14-nuanced-v1",
            "predicted_direction": "up",
            "actual_direction": "up" if hit else "down",
            "direction_hit": hit,
            "confidence": 0.60,
            "expected_return": expected_return,
            "actual_raw_return": 0.03 if hit else -0.03,
            "actual_excess_return": 0.03 if hit else -0.03,
            "raw_json": json.dumps(
                {
                    "debias": {
                        "method": "directional_signal",
                        "trade_action": "directional_signal",
                        "observe_only": False,
                    }
                }
            ),
        }

    rows = [
        score_row(1, symbol="NVDA", hit=1, expected_return=0.03),
        score_row(2, symbol="NVDA", hit=1, expected_return=0.02),
        score_row(3, symbol="MSFT", hit=0, expected_return=0.02),
        score_row(4, symbol="AAPL", hit=1, expected_return=0.001),
    ]

    summary = paper_order_risk_layer_summary(
        rows,
        min_confidence=0.0,
        normal_size=1.0,
        haircut_size_multiplier=1.0,
        base_quantity=1.0,
        max_total_gross_quantity=2.5,
        max_symbol_orders=1,
        min_abs_expected_return=0.01,
    )
    report = paper_order_risk_layer_markdown(summary)

    assert summary["candidate_delta"]["pre_candidates"] == 4
    assert summary["candidate_delta"]["post_candidates"] == 2
    assert summary["candidate_delta"]["post_gross_quantity_units"] == 2.0
    reasons = {row["bucket"]: row for row in summary["by_risk_reason"]}
    assert reasons["symbol_order_limit"]["orders"] == 1
    assert reasons["weak_expected_return"]["orders"] == 1
    assert "Paper Order Risk Layer Report" in report


def test_paper_portfolio_risk_layer_tracks_virtual_daily_loss() -> None:
    def score_row(outcome_id: int, *, hit: int, excess_return: float) -> dict:
        return {
            "id": outcome_id,
            "outcome_id": outcome_id,
            "symbol": "NVDA",
            "horizon": "1D",
            "price_source": "yahoo_chart_daily_adjclose",
            "replay_timestamp": f"2026-01-01T14:0{outcome_id}:00+00:00",
            "scorer_name": "openai",
            "model_name": "gpt-test",
            "scorer_version": "openai-replay-score-v14-nuanced-v1",
            "predicted_direction": "up",
            "actual_direction": "up" if hit else "down",
            "direction_hit": hit,
            "confidence": 0.60,
            "expected_return": 0.03,
            "actual_raw_return": excess_return,
            "actual_excess_return": excess_return,
            "raw_json": json.dumps(
                {
                    "debias": {
                        "method": "directional_signal",
                        "trade_action": "directional_signal",
                        "observe_only": False,
                    }
                }
            ),
        }

    rows = [
        score_row(1, hit=0, excess_return=-0.03),
        score_row(2, hit=1, excess_return=0.02),
        score_row(3, hit=1, excess_return=0.02),
    ]

    summary = paper_portfolio_risk_layer_summary(
        rows,
        min_confidence=0.0,
        normal_size=1.0,
        haircut_size_multiplier=1.0,
        base_quantity=1.0,
        max_total_gross_quantity=100.0,
        max_symbol_gross_quantity=100.0,
        max_side_gross_quantity=100.0,
        max_symbol_orders=10,
        max_symbol_day_orders=10,
        min_abs_expected_return=0.0,
        starting_equity=1000.0,
        notional_per_quantity=1000.0,
        max_daily_gross_exposure_pct=10.0,
        max_symbol_daily_exposure_pct=10.0,
        max_sector_daily_exposure_pct=10.0,
        max_daily_loss_pct=0.02,
        max_drawdown_pct=0.50,
        max_consecutive_losses=10,
    )
    report = paper_portfolio_risk_layer_markdown(summary)
    reasons = {row["bucket"]: row for row in summary["by_portfolio_reason"]}

    assert summary["candidate_delta"]["static_risk_candidates"] == 3
    assert summary["candidate_delta"]["portfolio_candidates"] == 1
    assert reasons["portfolio_daily_loss_limit"]["orders"] == 2
    assert summary["portfolio_metrics"]["ending_equity"] == 970.0
    assert "Paper Portfolio Risk Layer Report" in report
