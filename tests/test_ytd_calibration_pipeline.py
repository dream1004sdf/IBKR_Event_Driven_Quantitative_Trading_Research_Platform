from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from recorder.ytd_calibration_pipeline import (
    DEFAULT_YTD_SYMBOLS,
    YtdCalibrationConfig,
    YtdCalibrationPipeline,
    parse_csv,
    parse_replay_event_type,
    parse_stages,
)


def test_ytd_parsers_default_to_unique_symbols_all_stages_and_all_replay_events() -> None:
    assert parse_csv(None, DEFAULT_YTD_SYMBOLS) == DEFAULT_YTD_SYMBOLS
    assert parse_csv("mu,nvda, MU, spy", DEFAULT_YTD_SYMBOLS) == ("MU", "NVDA", "SPY")
    assert "scoring" in parse_stages("all")
    assert "adjustments" in parse_stages(None)
    assert parse_replay_event_type(None) is None
    assert parse_replay_event_type("all") is None
    assert parse_replay_event_type("earnings_guidance") == "earnings_guidance"


def test_ytd_dry_run_documents_score_before_adjustment_flow(tmp_path) -> None:
    settings = SimpleNamespace(database_path=tmp_path / "analysis.db")
    config = YtdCalibrationConfig(
        start="2026-01-01",
        end="2026-06-28",
        symbols=("MU", "NVDA"),
        stages=parse_stages("all"),
        output_dir=tmp_path,
        dry_run=True,
    )

    result = YtdCalibrationPipeline(settings, config).run()

    planned = result["planned_actions"]
    scoring_index = planned.index("Heuristic and optional OpenAI replay scoring")
    adjustment_index = planned.index("Deterministic and optional OpenAI surprise-adjustment note build after scoring")
    assert result["replay_event_type"] == "all"
    assert scoring_index < adjustment_index
    assert any("IBKR/Benzinga" in item for item in planned)
    assert Path(result["manifest_path"]).exists()
