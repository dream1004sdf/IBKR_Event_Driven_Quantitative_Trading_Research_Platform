from __future__ import annotations

import sqlite3

from scripts.run_s05p4_v5_4_1_provisional_ai_clone_impact import (
    VERSION,
    apply_overlay,
    overlay_digest,
)


def test_provisional_overlay_is_idempotent_and_does_not_overwrite_source_tables() -> None:
    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE TABLE source_labels(label_id INTEGER PRIMARY KEY, strict_eligible INTEGER NOT NULL)"
    )
    connection.execute("INSERT INTO source_labels VALUES(1,1)")
    source_before = connection.execute("SELECT * FROM source_labels").fetchall()

    payload = {
        "adjudications": [
            {
                "review_row_id": "row-1",
                "event_cluster_id": 101,
                "field_name": "event_type",
                "proposed_value": "unknown",
                "provisional_value": "policy_action",
                "working_research_value": "policy_action",
                "resolution_status": "resolved_corrected",
                "source_packet": "abstention",
                "reviewer_id": "reviewer-1",
                "reviewed_at": "2026-07-22T18:55:37+09:00",
                "provisional_version": VERSION,
            }
        ],
        "semantic_rows": [
            {
                "review_row_id": "row-1",
                "event_cluster_id": 101,
                "news_content_type": "event_report",
                "event_type": "policy_action",
                "region": "united_states",
                "direction": "tightening",
                "affected_channel": "sovereign_risk",
                "proxy_family": "sanctions",
                "primary_proxy": "",
                "primary_proxy_abstained": 1,
                "resolved_field_count": 6,
                "explicit_abstain_count": 1,
                "unreviewed_field_count": 0,
                "research_context_complete": 1,
                "reaction_observation_eligible": 0,
                "semantic_strict_eligible": 0,
                "strict_training_eligible": 0,
                "eligibility_hold_reason": "provisional_only",
                "provisional_version": VERSION,
            }
        ],
        "graphs": [],
        "recovered": [],
    }
    run_row = {
        "run_key": "run-1",
        "provisional_version": VERSION,
        "reviewer_label": "NON_HUMAN_INDEPENDENT_AI_TARGET_ADJUDICATION",
        "owner_acceptance_status": "owner_accepted_provisional_research_only",
        "adjudication_input_hash": "input-hash",
        "sealed_set_a_key_sha256": "key-hash",
        "source_frozen_clone_sha256": "clone-hash",
        "source_canonical_sha256": "canonical-hash",
        "set_a_rows": 1,
        "applied_at_utc": "2026-07-22T18:55:37+09:00",
    }

    apply_overlay(connection, payload, run_row=run_row)
    first = overlay_digest(connection)
    apply_overlay(connection, payload, run_row=run_row)
    second = overlay_digest(connection)

    assert first == second
    assert connection.execute("SELECT * FROM source_labels").fetchall() == source_before
    assert connection.execute(
        "SELECT reaction_observation_eligible,semantic_strict_eligible,strict_training_eligible "
        "FROM s05p4_v5_4_1_provisional_semantic_rows"
    ).fetchone() == (0, 0, 0)
    connection.close()
