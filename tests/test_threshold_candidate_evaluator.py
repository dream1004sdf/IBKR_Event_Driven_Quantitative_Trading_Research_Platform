import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from recorder.live_learning_llm_notes import _ensure_m225_schema
from recorder.live_scorer_learning import ensure_live_learning_schema
from recorder.threshold_candidate_evaluator import (
    ThresholdCandidateEvaluatorConfig,
    _check_option_structure,
    evaluate_threshold_candidates,
)


def test_threshold_candidate_evaluator_scores_shadow_candidates(tmp_path: Path) -> None:
    db_path = tmp_path / "events.db"
    ensure_live_learning_schema(db_path)
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    _ensure_m225_schema(con)
    now = "2026-07-01T15:00:00+00:00"
    note_ids: list[int] = []
    for idx in range(8):
        con.execute(
            """INSERT INTO m223_live_learning_predictions(
                source_table,source_id,event_hash,event_at,provider,symbol,headline,event_family,
                baseline_price,predicted_direction,expected_return,confidence,scorer_name,scorer_version,
                status,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "news_articles",
                idx + 1,
                f"hash-{idx}",
                now,
                "BZ",
                "SPY",
                f"SPY test headline {idx}",
                "market_news",
                100.0,
                "up",
                0.01,
                0.8,
                "test",
                "v1",
                "collecting",
                now,
                now,
            ),
        )
        prediction_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
        con.execute(
            """INSERT INTO m223_live_learning_outcomes(
                prediction_id,horizon_minutes,due_at,evaluated_at,baseline_price,reaction_price,
                raw_return,actual_direction,direction_hit,absolute_error,status,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                prediction_id,
                5,
                now,
                now,
                100.0,
                99.0,
                -0.01,
                "down",
                0,
                0.02,
                "evaluated",
                now,
                now,
            ),
        )
        outcome_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
        con.execute(
            """INSERT INTO m223_live_learning_adjustment_notes(
                prediction_id,outcome_id,note_version,mismatch_type,reason_summary,
                recommended_adjustment,evidence_json,created_at,updated_at,
                analysis_provider,analysis_model,confidence,raw_json,pattern_tags_json,
                note_quality_score,note_quality_level,note_rejected,rejection_reason
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                prediction_id,
                outcome_id,
                "m225-llm-adjustment-notes-v1",
                "gap",
                "option structure and market context repeated miss",
                "candidate only",
                json.dumps({"threshold_candidate": "raise confirmation threshold for this pattern"}),
                now,
                now,
                "openai",
                "fake",
                0.9,
                "{}",
                json.dumps(["unit_test"]),
                90.0,
                "strong",
                0,
                "",
            ),
        )
        note_ids.append(int(con.execute("SELECT last_insert_rowid()").fetchone()[0]))
    con.execute(
        """INSERT INTO m225_llm_note_threshold_candidates(
            candidate_key,generated_at,status,reason_category,pattern_tags_json,
            sample_count,avg_confidence,avg_note_quality_score,threshold_candidate,
            source_note_ids_json,created_at,updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "market_context:unit_test",
            now,
            "shadow_candidate",
            "market_context",
            json.dumps(["unit_test"]),
            8,
            0.9,
            90.0,
            "raise confirmation threshold for this pattern",
            json.dumps(note_ids),
            now,
            now,
        ),
    )
    con.commit()
    con.close()

    result = evaluate_threshold_candidates(
        ThresholdCandidateEvaluatorConfig(
            database_path=db_path,
            max_ticks=1,
            adaptive_refresh_enabled=True,
            output_path=tmp_path / "m226.md",
            json_output_path=tmp_path / "m226.json",
            state_json_path=tmp_path / "m226_state.json",
        )
    )

    assert result["candidate_count"] == 1
    assert result["tick_count"] == 1
    assert result["adaptive_refresh"]["tier"] == "changed"
    assert result["promotion_ready_count"] == 1
    assert result["top_candidates"][0]["status"] == "promotion_ready"
    assert result["top_candidates"][0]["outcome_direction_key"] == "false_positive_up"
    assert result["top_candidates"][0]["blocker_severity"] == "none"
    assert result["top_candidates"][0]["source_note_count"] == 8
    assert result["shadow_promotion_trial"]["active_count"] == 1
    assert result["shadow_promotion_trial"]["ready_for_real_promotion_count"] == 0
    assert result["promotion_gate"]["review_count"] == 1
    assert result["promotion_gate"]["waitlist_count"] == 0
    assert result["promotion_gate"]["watchlist_count"] == 0
    assert result["promotion_gate"]["blocked_count"] == 1
    assert "recent_shadow_performance" in result["promotion_gate"]["blocked_reason_counts"]
    assert "evidence_freshness" in result["promotion_gate"]["blocked_reason_counts"]
    assert result["cycle_report"]["new_family_count"] == 1
    con = sqlite3.connect(db_path)
    row = con.execute("SELECT status, sample_count FROM m226_llm_threshold_candidate_evaluations").fetchone()
    trial_row = con.execute("SELECT trial_status, cycles_seen FROM m227_shadow_promotion_trials").fetchone()
    gate_row = con.execute("SELECT gate_status, ready_for_promotion_waitlist FROM m228_threshold_promotion_gate_reviews").fetchone()
    con.close()
    assert row == ("promotion_ready", 8)
    assert trial_row == ("shadow_trial_active", 1)
    assert gate_row == ("promotion_gate_blocked", 0)


def test_threshold_candidate_evaluator_marks_option_structure_expected_deferred_when_options_closed(monkeypatch) -> None:
    monkeypatch.setattr(
        "recorder.threshold_candidate_evaluator._current_us_option_session_context",
        lambda: {"session": "closed_or_extended", "is_regular_option_session": False},
    )
    check = _check_option_structure(
        {
            "semantic_family_key": "timestamp_alignment_late_baseline+option_structure_quality|flat_misread",
            "threshold_candidate": "use gamma wall context",
            "pattern_tags": ["late_baseline", "option_structure"],
            "semantic_family_signals": ["option_structure_quality"],
        },
        {
            "generated_at": "2026-07-01T14:00:00+00:00",
            "source_quote_generated_at": "2026-07-01T14:00:00+00:00",
            "status": "yellow",
            "session_guard": {"freshness_status": "fresh", "max_snapshot_age_minutes": 10},
            "summary": {"confidence_counts": {"medium": 1}},
        },
        {
            "completed_at": "2026-07-01T14:00:00+00:00",
            "final_status": "green",
            "final_quote_fresh": True,
            "final_effective_max_snapshot_age_minutes": 10,
            "final_quote_quality_status": "yellow",
        },
        {"option_structure_relevant": True, "option_structure_required": True},
    )

    assert check["passed"] is False
    assert check["reason"] == "option_structure_expected_deferred_stale"
    assert check["severity"] == "soft"
    assert check["details"]["freshness_status"] == "expected_deferred_stale"
    assert check["details"]["freshness"]["expected_deferred_stale"] is True
    assert "option_structure_artifact_age_exceeded" in check["details"]["freshness"]["stale_reasons"]


def test_threshold_candidate_evaluator_merges_semantic_family_and_marks_needs_more_samples(tmp_path: Path) -> None:
    db_path = tmp_path / "events.db"
    ensure_live_learning_schema(db_path)
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    _ensure_m225_schema(con)
    now = "2026-07-01T15:00:00+00:00"
    all_note_ids: list[int] = []
    for idx in range(3):
        con.execute(
            """INSERT INTO m223_live_learning_predictions(
                source_table,source_id,event_hash,event_at,provider,symbol,headline,event_family,
                baseline_price,predicted_direction,expected_return,confidence,scorer_name,scorer_version,
                status,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "news_articles",
                idx + 1,
                f"family-hash-{idx}",
                now,
                "BZ",
                "SPY",
                f"SPY late baseline headline {idx}",
                "market_news",
                100.0,
                "up",
                0.01,
                0.8,
                "test",
                "v1",
                "collecting",
                now,
                now,
            ),
        )
        prediction_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
        con.execute(
            """INSERT INTO m223_live_learning_outcomes(
                prediction_id,horizon_minutes,due_at,evaluated_at,baseline_price,reaction_price,
                raw_return,actual_direction,direction_hit,absolute_error,status,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (prediction_id, 5, now, now, 100.0, 99.0, -0.01, "down", 0, 0.015, "evaluated", now, now),
        )
        outcome_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
        con.execute(
            """INSERT INTO m223_live_learning_adjustment_notes(
                prediction_id,outcome_id,note_version,mismatch_type,reason_summary,
                recommended_adjustment,evidence_json,created_at,updated_at,
                analysis_provider,analysis_model,confidence,raw_json,pattern_tags_json,
                note_quality_score,note_quality_level,note_rejected,rejection_reason
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                prediction_id,
                outcome_id,
                "m225-llm-adjustment-notes-v1",
                "gap",
                "late baseline caused direction mismatch",
                "candidate only",
                json.dumps({"threshold_candidate": "cap confidence when baseline is late"}),
                now,
                now,
                "openai",
                "fake",
                0.92,
                "{}",
                json.dumps(["late_baseline", f"variant_{idx}"]),
                95.0,
                "strong",
                0,
                "",
            ),
        )
        all_note_ids.append(int(con.execute("SELECT last_insert_rowid()").fetchone()[0]))
    for idx, note_id in enumerate(all_note_ids):
        con.execute(
            """INSERT INTO m225_llm_note_threshold_candidates(
                candidate_key,generated_at,status,reason_category,pattern_tags_json,
                sample_count,avg_confidence,avg_note_quality_score,threshold_candidate,
                source_note_ids_json,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                f"timestamp_alignment_variant_{idx}",
                now,
                "shadow_candidate",
                f"timestamp_alignment_variant_{idx}",
                json.dumps(["late_baseline", f"variant_{idx}"]),
                1,
                0.92,
                95.0,
                "cap confidence when baseline is late",
                json.dumps([note_id]),
                now,
                now,
            ),
        )
    con.commit()
    con.close()

    result = evaluate_threshold_candidates(
        ThresholdCandidateEvaluatorConfig(
            database_path=db_path,
            max_ticks=1,
            semantic_family_enabled=True,
            min_promotion_sample_count=8,
            adaptive_refresh_enabled=True,
        )
    )

    assert result["raw_candidate_count"] == 3
    assert result["semantic_family_count"] == 1
    assert result["needs_more_samples_count"] == 1
    assert result["blocker_severity_counts"]["soft"] == 1
    top = result["top_candidates"][0]
    assert top["sample_count"] == 3
    assert top["status"] == "needs_more_samples"
    assert top["outcome_direction_key"] == "false_positive_up"
    assert result["m225_focus_feedback"]["focus_patterns"][0]["target_additional_samples"] == 5
    assert result["m225_focus_feedback"]["focus_patterns"][0]["blocker_severity"] == "soft"
    assert result["m225_focus_feedback"]["focus_pattern_count"] == 1


def test_threshold_candidate_evaluator_routes_soft_option_conflict_to_watchlist(tmp_path: Path) -> None:
    db_path = tmp_path / "events.db"
    ensure_live_learning_schema(db_path)
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    _ensure_m225_schema(con)
    now = "2026-07-01T15:00:00+00:00"
    note_ids: list[int] = []
    for idx in range(8):
        con.execute(
            """INSERT INTO m223_live_learning_predictions(
                source_table,source_id,event_hash,event_at,provider,symbol,headline,event_family,
                baseline_price,predicted_direction,expected_return,confidence,scorer_name,scorer_version,
                status,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "news_articles",
                idx + 100,
                f"option-watch-hash-{idx}",
                now,
                "BZ",
                "QQQ",
                f"QQQ option structure headline {idx}",
                "market_news",
                100.0,
                "up",
                0.01,
                0.85,
                "test",
                "v1",
                "collecting",
                now,
                now,
            ),
        )
        prediction_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
        con.execute(
            """INSERT INTO m223_live_learning_outcomes(
                prediction_id,horizon_minutes,due_at,evaluated_at,baseline_price,reaction_price,
                raw_return,actual_direction,direction_hit,absolute_error,status,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (prediction_id, 5, now, now, 100.0, 99.0, -0.01, "down", 0, 0.02, "evaluated", now, now),
        )
        outcome_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
        con.execute(
            """INSERT INTO m223_live_learning_adjustment_notes(
                prediction_id,outcome_id,note_version,mismatch_type,reason_summary,
                recommended_adjustment,evidence_json,created_at,updated_at,
                analysis_provider,analysis_model,confidence,raw_json,pattern_tags_json,
                note_quality_score,note_quality_level,note_rejected,rejection_reason
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                prediction_id,
                outcome_id,
                "m225-llm-adjustment-notes-v1",
                "gap",
                "option structure low confidence should not be promoted directly",
                "candidate only",
                json.dumps({"threshold_candidate": "require option structure confirmation before promotion"}),
                now,
                now,
                "openai",
                "fake",
                0.9,
                "{}",
                json.dumps(["option_structure_quality", "gamma"]),
                90.0,
                "strong",
                0,
                "",
            ),
        )
        note_ids.append(int(con.execute("SELECT last_insert_rowid()").fetchone()[0]))
    con.execute(
        """INSERT INTO m225_llm_note_threshold_candidates(
            candidate_key,generated_at,status,reason_category,pattern_tags_json,
            sample_count,avg_confidence,avg_note_quality_score,threshold_candidate,
            source_note_ids_json,created_at,updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "option_structure_quality:watchlist",
            now,
            "shadow_candidate",
            "option_structure_quality",
            json.dumps(["option_structure_quality", "gamma"]),
            8,
            0.9,
            90.0,
            "require option structure confirmation before promotion",
            json.dumps(note_ids),
            now,
            now,
        ),
    )
    con.commit()
    con.close()

    evidence_path = tmp_path / "freshness.json"
    evidence_path.write_text(
        json.dumps({"overall_status": "green", "scorer_guard": {"action": "normal"}, "stale_source_keys": [], "lagging_source_keys": []}),
        encoding="utf-8",
    )
    options_path = tmp_path / "options.json"
    options_path.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(UTC).isoformat(),
                "source_quote_generated_at": datetime.now(UTC).isoformat(),
                "status": "yellow",
                "session_guard": {"freshness_status": "fresh"},
                "summary": {"confidence_counts": {"low": 3, "medium": 0, "high": 0}},
            }
        ),
        encoding="utf-8",
    )
    supervisor_path = tmp_path / "options_supervisor.json"
    supervisor_path.write_text(
        json.dumps(
            {
                "completed_at": datetime.now(UTC).isoformat(),
                "final_status": "green",
                "final_quote_fresh": True,
                "final_quote_quality_status": "green",
                "final_failed_action_count": 0,
            }
        ),
        encoding="utf-8",
    )
    portfolio_path = tmp_path / "portfolio.json"
    portfolio_path.write_text(
        json.dumps({"risk_state": "within_limits", "recommended_action": "allow_new_entries", "breaches": []}),
        encoding="utf-8",
    )

    result = evaluate_threshold_candidates(
        ThresholdCandidateEvaluatorConfig(
            database_path=db_path,
            max_ticks=1,
            min_shadow_trial_cycles=1,
            promotion_gate_recent_trial_count=1,
            promotion_gate_max_regime_family_share=1.0,
            evidence_freshness_json_path=evidence_path,
            options_market_structure_json_path=options_path,
            options_market_structure_supervisor_json_path=supervisor_path,
            portfolio_risk_state_json_path=portfolio_path,
        )
    )

    gate = result["promotion_gate"]
    assert gate["review_count"] == 1
    assert gate["waitlist_count"] == 0
    assert gate["watchlist_count"] == 1
    assert gate["blocked_count"] == 0
    assert gate["watchlist_reason_counts"] == {"option_structure_conflict": 1}
    assert gate["watchlist_detail_reason_counts"] == {"option_structure_low_confidence": 1}
    assert gate["promotion_requirement_counts"] == {"option_structure_low_confidence": 1}
    assert gate["auto_recheck"]["watchlist_candidates_rechecked_each_tick"] is True
    assert gate["transition_history"]["transition_count"] == 1
    assert gate["watchlist_aging"]["watchlist_count"] == 1
    assert gate["watchlist_aging"]["stale_watchlist_count"] == 0
    assert gate["conditional_promotion_count"] == 1
    assert gate["conditional_promotion_mode_counts"] == {"conditional_watchlist": 1}
    assert gate["backtest_report"]["current_review_status_counts"] == {"promotion_watchlist": 1}
    assert gate["option_confidence_root_causes"]["root_cause_counts"] == {"low_confidence_without_explicit_source_breakdown": 1}
    assert gate["recycle_learning"]["recycle_candidate_count"] == 0
    assert gate["scorer_threshold_policy_summary"]["scorer_action_counts"] == {"conditional_threshold_only": 1}
    assert gate["audit_report"]["current_operational_status_counts"] == {"promotion_watchlist": 1}
    candidate = gate["watchlist_candidates"][0]
    assert candidate["gate_status"] == "promotion_watchlist"
    assert candidate["blocked_reasons"] == []
    assert candidate["watchlist_reasons"] == ["option_structure_conflict"]
    assert candidate["detail_reasons"] == {"option_structure_conflict": "option_structure_low_confidence"}
    assert candidate["checks"]["option_structure_conflict"]["reason"] == "option_structure_low_confidence"
    assert candidate["promotion_requirements"][0]["reason"] == "option_structure_low_confidence"
    assert "medium/high confidence" in candidate["promotion_requirements"][0]["requirement"]
    assert candidate["auto_recheck_policy"]["if_all_requirements_pass"] == "promotion_waitlist"
    assert candidate["history_state"]["transition_type"] == "new_candidate"
    assert candidate["history_state"]["status_age_review_count"] == 1
    assert candidate["conditional_promotion"]["mode"] == "conditional_watchlist"
    assert candidate["recycle_learning_plan"]["enabled"] is False
    assert candidate["recycle_return_criteria"]["next_status_if_all_pass"] == "promotion_watchlist"
    assert candidate["scorer_threshold_application_policy"]["scorer_action"] == "conditional_threshold_only"

    con = sqlite3.connect(db_path)
    gate_row = con.execute("SELECT gate_status, ready_for_promotion_waitlist FROM m228_threshold_promotion_gate_reviews").fetchone()
    transition_row = con.execute("SELECT previous_gate_status, new_gate_status, transition_type FROM m229_threshold_promotion_gate_transitions").fetchone()
    con.close()
    assert gate_row == ("promotion_watchlist", 0)
    assert transition_row == (None, "promotion_watchlist", "new_candidate")
