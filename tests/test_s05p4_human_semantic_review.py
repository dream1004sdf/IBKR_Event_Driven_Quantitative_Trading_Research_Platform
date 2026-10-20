from __future__ import annotations

import sqlite3
from pathlib import Path

from recorder.s05p4_human_semantic_review import (
    BANNED_REVIEW_FIELDS,
    HUMAN_REVIEW_FIELDS,
    REVIEW_INPUT_FIELDS,
    S05P4_VERSION,
    apply_completed_review_to_clone,
    build_review_material,
    compare_reviews,
    review_output_template,
    sha256_file,
    validate_review_output_without_model_key,
    write_csv,
)


def _source_pack() -> dict:
    families = [
        "defense_conflict",
        "rates_policy",
        "oil_supply",
        "sanctions",
        "cybersecurity",
        "korea_risk",
        "grain_supply",
        "china_taiwan",
    ]
    providers = ["bz", "marketaux", "djnl"]
    rows = []
    for index in range(645):
        rows.append({
            "event_cluster_id": 10000 + index,
            "headline": f"Frozen semantic candidate {index}",
            "plan_as_of_utc": f"2025-{(index % 12) + 1:02d}-15T12:00:00+00:00",
            "provider": providers[index % len(providers)],
            "content_type": "geopolitical_primary_event",
            "region": "global",
            "direction": "escalation",
            "affected_channel": "risk",
            "proxy_family": families[index % len(families)],
            "primary_proxy": "SPY",
            "causal_assignment_correct": int(index % 5 != 0),
            "validation_set": "design" if index < 100 else "blind",
            "review_method": "frozen_v3_rule_replay_blinded_to_returns",
            "outcome_or_return_visible": 0,
        })
    return {"rows": rows}


def test_build_review_material_is_sanitized_stratified_and_deterministic() -> None:
    first = build_review_material(_source_pack(), seed="fixed-seed", target_a=400)
    second = build_review_material(_source_pack(), seed="fixed-seed", target_a=400)
    assert first["sanitized_by_set"] == second["sanitized_by_set"]
    assert len(first["sanitized_by_set"]["A"]) == 400
    assert len(first["sanitized_by_set"]["B"]) == 145
    assert first["audit"]["set_overlap"] == 0
    assert first["audit"]["design_rows_in_packets"] == 0
    assert first["audit"]["all_rows"]["pass"]
    assert set(first["sanitized_by_set"]["A"][0]) == set(REVIEW_INPUT_FIELDS)
    assert not (set(first["sanitized_by_set"]["A"][0]) & BANNED_REVIEW_FIELDS)
    assert all(row["source_validation_set"] == "blind" for row in first["sealed_rows"])


def test_compare_set_a_uses_only_completed_human_rows(tmp_path: Path) -> None:
    material = build_review_material(_source_pack(), seed="fixed-seed", target_a=400)
    canonical = tmp_path / "canonical.db"
    canonical.write_bytes(b"immutable-canonical")
    sealed = {
        "version": S05P4_VERSION,
        "freeze": {"canonical_source_checksum": sha256_file(canonical)},
        "audit": material["audit"],
        "rows": material["sealed_rows"],
    }
    set_a = material["sanitized_by_set"]["A"]
    model_by_id = {row["review_row_id"]: row["model_v3_decision"] for row in material["sealed_rows"]}
    completed = review_output_template(set_a)
    for row in completed:
        model = int(model_by_id[row["review_row_id"]])
        row.update({field: 1 for field in HUMAN_REVIEW_FIELDS if field.startswith("human_")})
        row["human_causal_eligible"] = model
        row["human_primary_event"] = model
        row["human_duplicate_or_update"] = 0
        row["human_exclusion_reason"] = "market_recap" if model == 0 else ""
        row["reviewer_confidence"] = "5"
        row["reviewer_id"] = "reviewer-a"
        row["reviewed_at"] = "2026-07-13T00:00:00+00:00"
    output = tmp_path / "review_a.csv"
    write_csv(output, completed, REVIEW_INPUT_FIELDS + HUMAN_REVIEW_FIELDS)
    report = compare_reviews(
        sealed,
        [output],
        phase="A",
        gates={
            "causal_assignment_precision_min": 0.95,
            "proxy_family_precision_min": 0.95,
            "primary_proxy_precision_min": 0.90,
            "critical_retained_region_channel_errors_max": 0,
            "unresolved_reviewer_conflicts_max": 0,
        },
        canonical_source_db=canonical,
    )
    assert report["phase_gate_pass"]
    assert report["reviewed_rows"] == 400
    assert report["metrics"]["causal_assignment_precision"]["value"] == 1.0
    assert report["metrics"]["causal_assignment_recall"]["value"] == 1.0


def test_no_key_validation_enforces_frozen_input_and_operational_values(tmp_path: Path) -> None:
    material = build_review_material(_source_pack(), seed="fixed-seed", target_a=400)
    review_input = tmp_path / "input_a.csv"
    review_output = tmp_path / "output_a.csv"
    write_csv(review_input, material["sanitized_by_set"]["A"], REVIEW_INPUT_FIELDS)
    completed = review_output_template(material["sanitized_by_set"]["A"])
    for row in completed:
        row.update({field: 1 for field in HUMAN_REVIEW_FIELDS if field.startswith("human_")})
        row["human_duplicate_or_update"] = 0
        row["human_exclusion_reason"] = ""
        row["reviewer_confidence"] = "4"
        row["reviewer_id"] = "independent-reviewer-a"
        row["reviewed_at"] = "2026-07-13T00:00:00+00:00"
    write_csv(review_output, completed, REVIEW_INPUT_FIELDS + HUMAN_REVIEW_FIELDS)
    report = validate_review_output_without_model_key(review_input, [review_output])
    assert report["ready_for_sealed_comparison"]
    assert report["completed_unique_rows"] == 400
    assert report["sealed_model_key_opened"] is False
    assert report["model_decisions_visible"] is False

    completed[0]["reviewer_confidence"] = "0.95"
    write_csv(review_output, completed, REVIEW_INPUT_FIELDS + HUMAN_REVIEW_FIELDS)
    bad = validate_review_output_without_model_key(review_input, [review_output])
    assert not bad["ready_for_sealed_comparison"]
    assert any("integer from 1 to 5" in error for error in bad["errors"])

    completed[0]["reviewer_confidence"] = "4"
    second = [dict(row) for row in completed]
    second[0]["reviewer_id"] = "independent-reviewer-b"
    second[0]["human_causal_eligible"] = 0
    second[0]["human_primary_event"] = 0
    second[0]["human_exclusion_reason"] = "insufficient_context"
    second_output = tmp_path / "output_a_second_reviewer.csv"
    write_csv(review_output, completed, REVIEW_INPUT_FIELDS + HUMAN_REVIEW_FIELDS)
    write_csv(second_output, second, REVIEW_INPUT_FIELDS + HUMAN_REVIEW_FIELDS)
    conflict = validate_review_output_without_model_key(review_input, [review_output, second_output])
    assert not conflict["ready_for_sealed_comparison"]
    assert any("no majority vote allowed" in error for error in conflict["errors"])


def test_apply_writes_separate_final_layer_without_overwriting_v3(tmp_path: Path) -> None:
    clone = tmp_path / "clone.db"
    with sqlite3.connect(clone) as conn:
        conn.executescript(
            """
            CREATE TABLE s05_full_range_label_eligibility(
                reaction_label_id INTEGER PRIMARY KEY,event_cluster_id INTEGER,
                market_data_strict_eligible INTEGER,point_in_time_strict_eligible INTEGER,
                semantic_strict_eligible INTEGER,causal_strict_eligible INTEGER,
                strict_training_eligible INTEGER
            );
            INSERT INTO s05_full_range_label_eligibility VALUES(1,101,1,1,1,1,1);
            INSERT INTO s05_full_range_label_eligibility VALUES(2,102,1,1,1,1,1);
            """
        )
    adjudicated = [
        {
            "review_row_id": "row-1",
            "reviewer_ids": ["human-a"],
            "source_event_cluster_id": 101,
            "review_set": "A",
            "model_v3_decision": 1,
            "human_causal_eligible": 0,
            "human_primary_event": 0,
            "human_content_type_correct": 1,
            "human_region_correct": 1,
            "human_direction_correct": 1,
            "human_affected_channel_correct": 1,
            "human_proxy_family_correct": 1,
            "human_primary_proxy_correct": 1,
            "human_duplicate_or_update": 0,
            "human_exclusion_reason": "secondary_report_no_new_information",
            "reviewer_confidence": "5",
            "reviewed_at": "2026-07-13T00:00:00+00:00",
        }
    ]
    report = apply_completed_review_to_clone(
        clone,
        {"rows": []},
        {"phase": "full", "phase_gate_pass": True, "adjudicated_rows": adjudicated},
        sealed_key_sha256="abc",
    )
    assert report["same_config_rerun_diff_zero"]
    assert report["human_rejected_semantic_strict_labels"] == 0
    assert not report["original_v3_tables_overwritten"]
    with sqlite3.connect(clone) as conn:
        assert conn.execute(
            "SELECT strict_training_eligible FROM s05_full_range_label_eligibility WHERE reaction_label_id=1"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT final_strict_training_eligible FROM s05p4_final_label_eligibility WHERE reaction_label_id=1"
        ).fetchone()[0] == 0
