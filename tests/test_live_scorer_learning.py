import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from recorder.live_scorer_learning import (
    LiveScorerLearningConfig,
    live_scorer_learning_markdown,
    run_live_scorer_learning,
)


def _write_json(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_live_scorer_learning_logs_shadow_predictions_and_outcomes(tmp_path: Path) -> None:
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
    con.execute(
        "INSERT INTO external_market_signals VALUES (1,'marketaux','Middle East ceasefire headline','2026-07-01T15:00:00+00:00','Middle East','geopolitics','[\"USO\",\"GLD\"]','2026-07-01T15:00:00+00:00')"
    )
    con.commit()
    con.close()

    market_data = _write_json(
        tmp_path / "market.json",
        {
            "generated_at": "2026-07-01T15:40:00+00:00",
            "status": "green",
            "rows": [
                {"key": "SPY", "price": 101.0},
                {"key": "USO", "price": 49.0},
                {"key": "GLD", "price": 300.0},
            ],
        },
    )
    m232_hot_cache = _write_json(
        tmp_path / "m232_hot_cache.json",
        {
            "m232_hot_cache_version": "m232-hot-cache-v1",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "max_consumer_age_seconds": 300.0,
            "adaptive_schedule": {"tier": "hot_emergency", "next_interval_seconds": 5.0},
            "scorer_overlay_cache": {
                "paper_scorer_ready": True,
                "conditional_overlay_available": True,
                "withhold_reasons": {"watchlist": {}, "blocked": {}},
            },
            "reflex_hot_cache": {
                "reflex_candidate_count": 1,
                "top_assets": {"SPY": 1},
                "memories": [{"asset": "SPY", "recommended_reflex_action": "reflex_candidate"}],
            },
            "option_structure_cache": {
                "memory_count": 1,
                "memories": [{"symbol": "SPY", "recommended_use": "scorer_evidence_packet_context"}],
            },
            "action_gate_cache": {
                "memory_count": 1,
                "paper_entry_candidate_count": 1,
                "block_or_second_pass_count": 0,
                "memories": [{"symbol": "SPY", "recommended_action_gate_policy": "paper_entry_candidate"}],
            },
        },
    )
    result = run_live_scorer_learning(
        LiveScorerLearningConfig(
            database_path=db_path,
            market_data_json_path=market_data,
            m232_hot_cache_json_path=m232_hot_cache,
            max_ticks=1,
            lookback_minutes=300,
            horizons_minutes=(5,),
            max_new_events_per_tick=10,
            current_time=datetime(2026, 7, 1, 15, 45, tzinfo=timezone.utc),
        )
    )
    markdown = live_scorer_learning_markdown(result)

    assert result["shadow_only"] is True
    assert result["evidence_packet_version"] == "m224-evidence-packet-v2"
    assert result["latest_counts"]["prediction_count"] == 2
    assert result["latest_counts"]["evidence_packet_v2_prediction_count"] == 2
    assert result["latest_counts"]["packet_quality_counts"]
    assert result["latest_counts"]["asset_mapping_confidence_counts"]["high"] >= 2
    assert result["latest_counts"]["timestamp_alignment_counts"]["late_baseline"] == 2
    assert result["latest_counts"]["late_baseline_count"] == 2
    assert result["prediction_count"] == 2
    assert result["late_baseline_count"] == 2
    assert result["latest_counts"]["evaluated_outcome_count"] == 2
    assert result["safety"]["live_scorer_mutated"] is False
    assert "M223 Live Scorer Learning Supervisor" in markdown

    con = sqlite3.connect(db_path)
    prediction_count = con.execute("SELECT COUNT(*) FROM m223_live_learning_predictions").fetchone()[0]
    outcome_count = con.execute("SELECT COUNT(*) FROM m223_live_learning_outcomes WHERE status='evaluated'").fetchone()[0]
    candidate_count = con.execute("SELECT COUNT(*) FROM m223_live_threshold_candidates").fetchone()[0]
    packet_row = con.execute(
        "SELECT evidence_packet_version, market_context_json, option_structure_context_json, evidence_quality_json, scorer_input_packet_json, packet_quality_score, packet_quality_level, asset_mapping_confidence, event_market_data_lag_seconds, timestamp_alignment_status, baseline_is_late FROM m223_live_learning_predictions LIMIT 1"
    ).fetchone()
    con.close()

    assert prediction_count == 2
    assert outcome_count == 2
    assert candidate_count == 1
    assert packet_row[0] == "m224-evidence-packet-v2"
    assert json.loads(packet_row[1])["symbol"] in {"SPY", "GLD"}
    assert "available" in json.loads(packet_row[2])
    assert "freshness_overall_status" in json.loads(packet_row[3])
    assert json.loads(packet_row[4])["packet_version"] == "m224-evidence-packet-v2"
    assert json.loads(packet_row[4])["m232_hot_cache_context"]["usable"] is True
    assert json.loads(packet_row[4])["m232_hot_cache_context"]["adaptive_tier"] == "hot_emergency"
    assert "scorer_calibration" in json.loads(packet_row[4])
    assert packet_row[5] is not None
    assert packet_row[6] in {"complete", "usable", "weak", "poor"}
    assert packet_row[7] == "high"
    assert packet_row[8] is not None
    assert packet_row[9] == "late_baseline"
    assert packet_row[10] == 1


def test_live_scorer_learning_writes_summary_sidecar_and_marks_expected_deferred_option_context(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "recorder.live_scorer_learning._current_us_option_session_context",
        lambda: {"session": "closed_or_extended", "is_regular_option_session": False},
    )
    db_path = tmp_path / "events.db"
    con = sqlite3.connect(db_path)
    con.execute(
        "CREATE TABLE news_articles (id INTEGER PRIMARY KEY, provider TEXT, headline TEXT, published_at TEXT, received_at TEXT, primary_symbol TEXT, created_at TEXT, extra_data TEXT)"
    )
    con.execute(
        "CREATE TABLE external_market_signals (id INTEGER PRIMARY KEY, source TEXT, title TEXT, published_at TEXT, region TEXT, theme TEXT, assets_json TEXT, created_at TEXT)"
    )
    con.execute(
        "INSERT INTO news_articles VALUES (1,'BZ','SPY downgrade after tariff shock','2026-07-01T15:00:00+00:00','2026-07-01T15:00:00+00:00','SPY','2026-07-01T15:00:00+00:00','')"
    )
    con.commit()
    con.close()

    market_data = _write_json(
        tmp_path / "market.json",
        {
            "generated_at": "2026-07-01T15:40:00+00:00",
            "status": "green",
            "rows": [{"key": "SPY", "price": 100.0}],
        },
    )
    options = _write_json(
        tmp_path / "options.json",
        {
            "generated_at": "2026-07-01T14:00:00+00:00",
            "source_quote_generated_at": "2026-07-01T14:00:00+00:00",
            "status": "yellow",
            "session_guard": {"freshness_status": "fresh", "max_snapshot_age_minutes": 10},
            "symbols": [{"symbol": "SPY", "gamma_regime": "negative_gamma_trend_amplification_bias", "confidence": "medium"}],
        },
    )
    supervisor = _write_json(
        tmp_path / "supervisor.json",
        {
            "completed_at": "2026-07-01T14:00:00+00:00",
            "final_status": "green",
            "final_quote_fresh": True,
            "final_effective_max_snapshot_age_minutes": 10,
            "final_quote_quality_status": "yellow",
        },
    )
    out = tmp_path / "m223_live_scorer_learning.json"
    result = run_live_scorer_learning(
        LiveScorerLearningConfig(
            database_path=db_path,
            market_data_json_path=market_data,
            options_market_structure_json_path=options,
            options_market_structure_supervisor_json_path=supervisor,
            max_ticks=1,
            lookback_minutes=300,
            horizons_minutes=(5,),
            max_new_events_per_tick=10,
            current_time=datetime(2026, 7, 1, 15, 45, tzinfo=timezone.utc),
            json_output_path=out,
        )
    )

    sidecar = tmp_path / "m223_live_scorer_learning_summary.json"
    assert out.exists()
    assert sidecar.exists()
    summary = json.loads(sidecar.read_text(encoding="utf-8"))
    assert summary["prediction_count"] == result["prediction_count"] == 1
    con = sqlite3.connect(db_path)
    packet_json, option_json, predicted_direction = con.execute(
        "SELECT scorer_input_packet_json, option_structure_context_json, predicted_direction FROM m223_live_learning_predictions LIMIT 1"
    ).fetchone()
    con.close()
    packet = json.loads(packet_json)
    option_context = json.loads(option_json)
    assert option_context["freshness_status"] == "expected_deferred_stale"
    assert option_context["freshness"]["expected_deferred_stale"] is True
    assert "option_structure_artifact_age_exceeded" in option_context["freshness"]["stale_reasons"]
    assert packet["scorer_calibration"]["applied"] is True
    assert predicted_direction == "neutral"
