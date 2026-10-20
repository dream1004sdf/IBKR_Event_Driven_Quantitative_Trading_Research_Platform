from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from prediction_evaluation.calibration import CalibrationEngine
from prediction_evaluation.config import EvaluationConfig
from prediction_evaluation.db import EvaluationDatabase
from prediction_evaluation.evaluation import PredictionEvaluationEngine
from prediction_evaluation.metrics import calculate_metrics, realized_direction
from prediction_evaluation.replay import HistoricalReplayEngine
from recorder.analysis_database import AnalysisDatabase

UTC = timezone.utc


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _build_db(path: Path, count: int = 1, *, include_prediction_inputs: bool = False) -> None:
    AnalysisDatabase(path)
    conn = sqlite3.connect(path)
    now = datetime(2025, 1, 1, 14, 30, tzinfo=UTC)
    for index in range(count):
        received = now + timedelta(days=index)
        due = received + timedelta(hours=1)
        created = received + timedelta(minutes=1)
        symbol = "MU"
        cursor = conn.execute(
            """
            INSERT INTO news_articles(
                provider,article_id,headline,normalized_headline,headline_hash,body,
                published_at,received_at,provider_published_at,first_seen_at,
                timestamp_quality,timestamp_source,primary_symbol,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "TEST",f"article-{index}",f"MU earnings event {index}",
                f"mu earnings event {index}",f"hash-{index}","MU reported earnings and guidance.",
                _iso(received),_iso(received),_iso(received),_iso(received),
                "provider_published","provider_published_at",symbol,_iso(received),
            ),
        )
        news_id = int(cursor.lastrowid)
        conn.execute(
            "INSERT INTO news_symbols(news_id,symbol,first_seen_at) VALUES (?,?,?)",
            (news_id,symbol,_iso(received)),
        )
        cursor = conn.execute(
            """
            INSERT INTO analysis_jobs(
                news_id,analysis_key,provider,model_name,prompt_version,status,priority,
                attempts,next_attempt_at,created_at,updated_at
            ) VALUES (?,?,?,?,?,'complete',0,1,?,?,?)
            """,
            (news_id,f"key-{index}","openai","gpt-5.5","test-prompt",_iso(received),_iso(received),_iso(received)),
        )
        job_id = int(cursor.lastrowid)
        raw = {
            "event_stage": "initial_release",
            "event_type": "earnings",
            "prediction_eligible": True,
        }
        cursor = conn.execute(
            """
            INSERT INTO news_analyses(
                job_id,news_id,analysis_key,provider,model_name,prompt_version,prompt_hash,
                input_hash,started_at,completed_at,duration_ms,event_type,event_subtype,
                sentiment,causal_signal,prediction_eligible,analysis_priority,primary_symbol,
                related_symbols_json,impact_score,novelty_score,source_quality,confidence,
                time_horizon,summary,bull_case_json,bear_case_json,data_limitations_json,
                raw_json,created_at,event_scope,subject_entity,reaction_observation_eligible,
                source_tier,official_source_status,event_subject_symbol,subject_asset_type,
                target_asset_type,target_role,target_assets_json,attribution_status,
                attribution_confidence,original_subscription_symbol,subject_target_available,
                attribution_reason,target_quote_integrity_status
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                job_id,news_id,f"key-{index}","openai","gpt-5.5","test-prompt","ph",
                f"input-{index}",_iso(received),_iso(created),100,"earnings","quarterly",
                "positive",1,1,"high",symbol,"[]",0.8,0.8,0.9,0.8,
                "1_to_3_days","test analysis","[]","[]","[]",json.dumps(raw),_iso(created),
                "company","Micron",1,"reputable_secondary","not_available",symbol,"stock",
                "stock","subject_asset",json.dumps([symbol]),"matched",0.99,symbol,1,"matched",
                "valid",
            ),
        )
        analysis_id = int(cursor.lastrowid)
        conn.execute(
            """
            INSERT INTO market_baselines(
                news_id,symbol,captured_at,last,midpoint,selected_price,selected_price_source,
                currency,con_id,contract_symbol,sec_type,exchange,primary_exchange,
                market_data_type,quote_integrity_status,quote_integrity_reasons_json,
                reaction_baseline_eligible
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (news_id,symbol,_iso(received),100.0,100.0,100.0,"last","USD",1,symbol,"STK","SMART","NASDAQ",1,"valid","[]",1),
        )
        cursor = conn.execute(
            """
            INSERT INTO reaction_targets(
                news_id,symbol,horizon,due_at,next_attempt_at,status,attempts,updated_at,
                usage_status,usage_reason,governance_analysis_id,calibration_eligible,
                observation_eligible
            ) VALUES (?,?,?,?,?,'complete',0,?,'eligible','eligible test',?,1,1)
            """,
            (news_id,symbol,"1H",_iso(due),_iso(due),_iso(due),analysis_id),
        )
        target_id = int(cursor.lastrowid)
        actual_up = index % 2 == 0
        reaction_price = 101.0 if actual_up else 99.0
        raw_return = reaction_price / 100.0 - 1.0
        conn.execute(
            """
            INSERT INTO market_reactions(
                target_id,news_id,symbol,horizon,due_at,captured_at,baseline_price,
                reaction_price,raw_return,price_source,created_at,usage_status,usage_reason,
                governance_analysis_id,calibration_eligible,observation_eligible
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,'eligible','eligible test',?,1,1)
            """,
            (target_id,news_id,symbol,"1H",_iso(due),_iso(due),100.0,reaction_price,raw_return,"last",_iso(due),analysis_id),
        )
        conn.execute(
            """
            INSERT INTO predictions(
                analysis_id,news_id,symbol,horizon,expected_return,bear_return,bull_return,
                up_probability,flat_probability,down_probability,confidence,created_at,
                evaluation_due_at,is_valid,evaluation_status,evaluation_label,
                valid_for_directional_trading,valid_for_probability_calibration
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1,'valid','test',1,1)
            """,
            (analysis_id,news_id,symbol,"1H",0.005,-0.02,0.02,0.65,0.15,0.20,0.75,_iso(created),_iso(due)),
        )
        if include_prediction_inputs:
            snapshot = {
                "anti_leakage_note": "No post-news reaction data is included.",
                "headline": f"MU earnings event {index}",
                "article_body": "MU reported earnings and guidance.",
                "attributed_primary_symbol": symbol,
                "baseline_market_snapshot": [
                    {"symbol": symbol, "captured_at": _iso(received), "selected_price": 100.0}
                ],
                "official_evidence": [],
                "required_prediction_horizons": ["15m", "1h", "close", "1d", "3d"],
            }
            conn.execute(
                """
                INSERT INTO prediction_inputs(
                    analysis_id,news_id,input_hash,headline_hash,body_hash,input_snapshot_json,
                    baseline_snapshot_json,created_at
                ) VALUES (?,?,?,?,?,?,?,?)
                """,
                (analysis_id,news_id,f"input-{index}","hh","bh",json.dumps(snapshot),json.dumps(snapshot["baseline_market_snapshot"]),_iso(created)),
            )
    conn.commit()
    conn.close()


def test_metrics_direction_and_scores() -> None:
    rows = [
        {
            "predicted_direction": "up",
            "realized_direction": "up",
            "p_up": 0.7,
            "p_flat": 0.2,
            "p_down": 0.1,
            "expected_return": 0.02,
            "realized_return": 0.01,
            "scenario_range_hit": 1,
        },
        {
            "predicted_direction": "down",
            "realized_direction": "flat",
            "p_up": 0.1,
            "p_flat": 0.3,
            "p_down": 0.6,
            "expected_return": -0.01,
            "realized_return": 0.0,
            "scenario_range_hit": 1,
        },
    ]
    result = calculate_metrics(rows)
    assert result.sample_count == 2
    assert result.direction_accuracy == pytest.approx(0.5)
    assert result.mae == pytest.approx(0.01)
    assert result.bias == pytest.approx(0.0)
    assert realized_direction(0.002, 0.0025) == "flat"


def test_evaluation_migration_and_run(tmp_path: Path) -> None:
    db_path = tmp_path / "events.db"
    _build_db(db_path)
    config = EvaluationConfig(database_path=db_path)
    db = EvaluationDatabase(db_path)
    assert db.migrate() is True
    assert db.migrate() is False
    result = PredictionEvaluationEngine(config).run(full=True)
    assert result.evaluated_count == 1
    report = PredictionEvaluationEngine(config).report(show_exclusions=True)
    assert report["evaluated_count"] == 1
    assert report["metrics"]["sample_count"] == 1
    assert report["metrics"]["direction_accuracy"] == 1.0


def test_calibration_uses_shrinkage_after_minimum_sample(tmp_path: Path) -> None:
    db_path = tmp_path / "events.db"
    _build_db(db_path, count=30)
    config = EvaluationConfig(database_path=db_path)
    PredictionEvaluationEngine(config).run(full=True)
    fit = CalibrationEngine(config).fit(method="auto", training_fraction=0.8)
    assert fit.profile_count >= 3
    report = CalibrationEngine(config).report(version=fit.version)
    global_methods = {
        profile["method"]
        for profile in report["profiles"]
        if profile["fallback_level"] == "global"
    }
    assert "bin_shrinkage" in global_methods


def test_replay_audit_rejects_future_outcome_fields(tmp_path: Path) -> None:
    db_path = tmp_path / "events.db"
    _build_db(db_path, include_prediction_inputs=True)
    config = EvaluationConfig(database_path=db_path)
    settings = SimpleNamespace(ai_prompt_version="replay-test-v1")
    replay = HistoricalReplayEngine(settings, config)
    planned = replay.plan(
        start=datetime(2025, 1, 1, tzinfo=UTC),
        end=datetime(2025, 1, 3, tzinfo=UTC),
        event_types=("earnings",),
    )
    assert planned == 1
    with replay.db.connect() as conn:
        job_id = int(conn.execute("SELECT id FROM replay_jobs").fetchone()["id"])
    status, _ = replay.audit_job(job_id, strict=True)
    assert status == "PASS"
    with replay.db.connect() as conn:
        row = conn.execute("SELECT snapshot_json FROM replay_jobs WHERE id=?", (job_id,)).fetchone()
        snapshot = json.loads(row["snapshot_json"])
        snapshot["realized_return"] = 0.10
        conn.execute(
            "UPDATE replay_jobs SET snapshot_json=? WHERE id=?",
            (json.dumps(snapshot), job_id),
        )
    status, reasons = replay.audit_job(job_id, strict=True)
    assert status == "FAIL"
    assert any("forbidden snapshot key" in reason for reason in reasons)


def test_replay_strict_mode_flags_receipt_only_timestamp_provenance(tmp_path: Path) -> None:
    db_path = tmp_path / "receipt-only.db"
    _build_db(db_path, include_prediction_inputs=True)
    conn = sqlite3.connect(db_path)
    conn.execute(
        """UPDATE news_articles
           SET provider_published_at=NULL,source_accepted_at=NULL,
               first_seen_at=received_at,timestamp_quality='receipt_only',
               timestamp_source='received_at'"""
    )
    conn.commit()
    conn.close()

    replay = HistoricalReplayEngine(
        SimpleNamespace(ai_prompt_version="replay-timestamp-test"),
        EvaluationConfig(database_path=db_path),
    )
    assert replay.plan(
        start=datetime(2025, 1, 1, tzinfo=UTC),
        end=datetime(2025, 1, 3, tzinfo=UTC),
        event_types=("earnings",),
    ) == 1
    with replay.db.connect() as conn:
        job_id = int(conn.execute("SELECT id FROM replay_jobs").fetchone()["id"])
    status, reasons = replay.audit_job(job_id, strict=True)
    assert status == "REVIEW_REQUIRED"
    assert any("receipt_only" in reason for reason in reasons)


def test_replay_rejects_content_updated_after_cutoff(tmp_path: Path) -> None:
    db_path = tmp_path / "updated-content.db"
    _build_db(db_path, include_prediction_inputs=True)
    updated_at = datetime(2025, 1, 2, 14, 30, tzinfo=UTC)
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE news_articles SET content_updated_at=?", (_iso(updated_at),))
    conn.commit()
    conn.close()

    replay = HistoricalReplayEngine(
        SimpleNamespace(ai_prompt_version="replay-timestamp-test"),
        EvaluationConfig(database_path=db_path),
    )
    assert replay.plan(
        start=datetime(2025, 1, 1, tzinfo=UTC),
        end=datetime(2025, 1, 3, tzinfo=UTC),
        event_types=("earnings",),
    ) == 1
    with replay.db.connect() as conn:
        job_id = int(conn.execute("SELECT id FROM replay_jobs").fetchone()["id"])
    status, reasons = replay.audit_job(job_id, strict=True)
    assert status == "FAIL"
    assert "article content was updated after replay cutoff" in reasons
