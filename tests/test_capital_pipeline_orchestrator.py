from __future__ import annotations

import json
from pathlib import Path

from recorder.capital_pipeline_orchestrator import (
    build_capital_pipeline_freshness_report,
    capital_pipeline_orchestrator_markdown,
)


def _write(path: Path, generated_at: str, **extra) -> Path:
    payload = {"generated_at": generated_at, **extra}
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_m244_flags_downstream_artifacts_older_than_fresh_capital_state(tmp_path: Path) -> None:
    capital = _write(tmp_path / "m237.json", "2026-07-03T13:10:00+00:00")
    m239 = _write(tmp_path / "m239.json", "2026-07-03T12:00:00+00:00")
    m241_5 = _write(tmp_path / "m241_5.json", "2026-07-03T12:20:00+00:00")
    m241_7 = _write(tmp_path / "m241_7.json", "2026-07-03T12:22:00+00:00")
    m241_8 = _write(tmp_path / "m241_8.json", "2026-07-03T12:23:00+00:00")
    m241_6 = _write(tmp_path / "m241_6.json", "2026-07-03T12:25:00+00:00")
    m241 = _write(tmp_path / "m241.json", "2026-07-03T12:30:00+00:00")
    m242 = _write(tmp_path / "m242.json", "2026-07-03T12:35:00+00:00")
    m243 = _write(tmp_path / "m243.json", "2026-07-03T13:10:10+00:00")

    report = build_capital_pipeline_freshness_report(
        capital_state_path=capital,
        capital_aware_gate_path=m239,
        ticker_candidate_ranking_path=m241_5,
        cross_asset_expression_ranking_path=m241_7,
        cross_asset_expression_outcomes_path=m241_8,
        ticker_action_candidates_path=m241_6,
        strategy_allocation_path=m241,
        paper_capital_execution_path=m242,
        live_read_only_validation_path=m243,
        max_artifact_age_minutes=120,
        max_downstream_lag_seconds=30,
        now="2026-07-03T13:15:00+00:00",
    )

    assert report["capital_pipeline_orchestrator_version"] == "m244-capital-pipeline-freshness-orchestrator-v1"
    assert report["status"] == "yellow"
    assert report["rebuild_steps"] == [
        "m239_capital_aware_action_gate",
        "m241_5_ticker_candidate_ranking",
        "m241_7_cross_asset_expression_ranking",
        "m241_8_cross_asset_expression_outcomes",
        "m241_6_ticker_action_candidates",
        "m241_strategy_allocation_engine",
        "m242_paper_capital_aware_execution",
        "m243_live_read_only_capital_validation",
    ]
    m239_row = next(row for row in report["artifacts"] if row["artifact_id"] == "m239_capital_aware_action_gate")
    assert "older_than_dependency" in m239_row["reasons"]
    m243_row = next(row for row in report["artifacts"] if row["artifact_id"] == "m243_live_read_only_capital_validation")
    assert "upstream_dependency_stale" in m243_row["reasons"]


def test_m244_reports_green_when_dependency_chain_is_current(tmp_path: Path) -> None:
    capital = _write(tmp_path / "m237.json", "2026-07-03T13:10:00+00:00")
    m239 = _write(tmp_path / "m239.json", "2026-07-03T13:10:20+00:00")
    m241_5 = _write(tmp_path / "m241_5.json", "2026-07-03T13:10:23+00:00")
    m241_7 = _write(tmp_path / "m241_7.json", "2026-07-03T13:10:24+00:00")
    m241_8 = _write(tmp_path / "m241_8.json", "2026-07-03T13:10:25+00:00")
    m241_6 = _write(tmp_path / "m241_6.json", "2026-07-03T13:10:26+00:00")
    m241 = _write(tmp_path / "m241.json", "2026-07-03T13:10:27+00:00")
    m242 = _write(tmp_path / "m242.json", "2026-07-03T13:10:30+00:00")
    m243 = _write(tmp_path / "m243.json", "2026-07-03T13:10:35+00:00")

    report = build_capital_pipeline_freshness_report(
        capital_state_path=capital,
        capital_aware_gate_path=m239,
        ticker_candidate_ranking_path=m241_5,
        cross_asset_expression_ranking_path=m241_7,
        cross_asset_expression_outcomes_path=m241_8,
        ticker_action_candidates_path=m241_6,
        strategy_allocation_path=m241,
        paper_capital_execution_path=m242,
        live_read_only_validation_path=m243,
        max_artifact_age_minutes=30,
        max_downstream_lag_seconds=30,
        now="2026-07-03T13:11:00+00:00",
    )

    assert report["status"] == "green"
    assert report["rebuild_required_count"] == 0
    assert report["status_counts"] == {"green": 9}
    assert "M244 Capital Pipeline Freshness Orchestrator" in capital_pipeline_orchestrator_markdown(report)
