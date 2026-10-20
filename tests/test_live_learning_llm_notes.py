import json
import sqlite3
from pathlib import Path
from freezegun import freeze_time

from recorder.live_learning_llm_notes import (
    LiveLearningEvidenceWeight,
    LiveLearningLLMAdjustment,
    LiveLearningLLMNoteConfig,
    run_live_learning_llm_adjustment_notes,
)
from recorder.live_scorer_learning import LiveScorerLearningConfig, run_live_scorer_learning


def _write_json(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class FakeAnalyzer:
    def analyze_with_metadata(self, packet: dict) -> tuple[LiveLearningLLMAdjustment, dict]:
        return self.analyze(packet), {
            "response_id": "resp_fake_m225",
            "billing_payer": "developer",
            "input_tokens": 123,
            "output_tokens": 45,
            "total_tokens": 168,
            "duration_ms": 67,
        }

    def analyze(self, packet: dict) -> LiveLearningLLMAdjustment:
        return LiveLearningLLMAdjustment(
            reason_category="option_structure_or_timestamp_context",
            reason_summary=(
                f"{packet['event']['symbol']} diverged because market context, option structure, "
                "and timestamp alignment jointly changed the expected reaction window enough to explain the gap."
            ),
            recommended_adjustment="Keep shadow-only and require repeated confirmation before threshold promotion.",
            evidence_source_weights=[
                LiveLearningEvidenceWeight(source="market_context", weight=0.4),
                LiveLearningEvidenceWeight(source="option_structure", weight=0.3),
                LiveLearningEvidenceWeight(source="timestamp_alignment", weight=0.3),
            ],
            missing_evidence=[],
            pattern_tags=["unit_test"],
            threshold_candidate="candidate only",
            confidence=0.77,
        )


@freeze_time("2026-07-01 16:00:00")
def test_live_learning_llm_notes_store_openai_style_note(tmp_path: Path) -> None:
    db_path = tmp_path / "events.db"
    con = sqlite3.connect(db_path)
    con.execute(
        "CREATE TABLE news_articles (id INTEGER PRIMARY KEY, provider TEXT, headline TEXT, published_at TEXT, received_at TEXT, primary_symbol TEXT, created_at TEXT, extra_data TEXT)"
    )
    con.execute(
        "CREATE TABLE external_market_signals (id INTEGER PRIMARY KEY, source TEXT, title TEXT, published_at TEXT, region TEXT, theme TEXT, assets_json TEXT, created_at TEXT)"
    )
    con.execute(
        "INSERT INTO news_articles VALUES (1,'BZ','SPY upgrade after strong earnings beat','2026-07-01T15:00:00+00:00','2026-07-01T15:00:00+00:00','SPY','2026-07-01T15:00:00+00:00','')"
    )
    con.commit()
    con.close()
    market_data = _write_json(
        tmp_path / "market.json",
        {"generated_at": "2026-07-01T14:55:00+00:00", "status": "green", "rows": [{"key": "SPY", "price": 99.0}]},
    )
    run_live_scorer_learning(
        LiveScorerLearningConfig(
            database_path=db_path,
            market_data_json_path=market_data,
            max_ticks=1,
            lookback_minutes=10000,
            horizons_minutes=(5,),
            max_new_events_per_tick=10,
        )
    )

    result = run_live_learning_llm_adjustment_notes(
        LiveLearningLLMNoteConfig(
            database_path=db_path,
            api_key="test",
            model="fake-model",
            limit=5,
            min_packet_quality_score=0,
            adaptive_backlog_enabled=True,
            adaptive_min_limit=2,
            adaptive_max_limit=10,
            adaptive_low_backlog=2,
            adaptive_high_backlog=5,
            adaptive_critical_backlog=20,
            adaptive_min_interval_seconds=60,
            adaptive_max_interval_seconds=600,
            json_output_path=tmp_path / "notes.json",
            output_path=tmp_path / "notes.md",
        ),
        analyzer=FakeAnalyzer(),
    )

    assert result["created_count"] == 1
    assert result["error_count"] == 0
    assert result["rejected_count"] == 0
    assert result["backlog_count"] == 1
    assert result["adaptive_schedule"]["tier"] == "low"
    assert result["adaptive_schedule"]["effective_limit"] == 5
    assert result["threshold_candidate_summary"]["candidate_group_count"] == 1
    con = sqlite3.connect(db_path)
    row = con.execute(
        "SELECT analysis_provider, analysis_model, confidence, reason_summary, pattern_tags_json, note_quality_level, note_rejected, response_id, input_tokens, output_tokens, duration_ms, billing_payer FROM m223_live_learning_adjustment_notes WHERE note_version='m225-llm-adjustment-notes-v1'"
    ).fetchone()
    candidate_count = con.execute("SELECT COUNT(*) FROM m225_llm_note_threshold_candidates").fetchone()[0]
    con.close()
    assert row[0] == "openai"
    assert row[1] == "fake-model"
    assert row[2] == 0.77
    assert "diverged" in row[3]
    assert json.loads(row[4]) == ["unit_test"]
    assert row[5] in {"strong", "usable"}
    assert row[6] == 0
    assert row[7] == "resp_fake_m225"
    assert row[8] == 123
    assert row[9] == 45
    assert row[10] == 67
    assert row[11] == "developer"
    assert candidate_count == 1
