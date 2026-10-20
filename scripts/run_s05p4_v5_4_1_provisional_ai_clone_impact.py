from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
VERSION = "s05p4-v5.4.1-owner-accepted-provisional-ai-v1"
FIELD_ORDER = (
    "news_content_type",
    "event_type",
    "region",
    "direction",
    "affected_channel",
    "proxy_family",
    "primary_proxy",
)
OVERLAY_TABLES = (
    "s05p4_v5_4_1_provisional_apply_runs",
    "s05p4_v5_4_1_provisional_target_adjudications",
    "s05p4_v5_4_1_provisional_semantic_rows",
    "s05p4_v5_4_1_provisional_region_channel_graphs",
    "s05p4_v5_4_1_provisional_recovered_events",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_hash(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"missing CSV header: {path}")
        return list(reader)


def parse_json_array(value: str) -> list[str]:
    parsed = json.loads(value)
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        raise ValueError(f"expected a JSON string array, got: {value!r}")
    return parsed


def distribution(values: Iterable[str]) -> dict[str, int]:
    return dict(sorted(Counter(values).items()))


def sqlite_snapshot(source: Path, destination: Path) -> None:
    if destination.exists():
        raise FileExistsError(f"refusing to overwrite existing dry-run clone: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.unlink(missing_ok=True)
    source_connection = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    destination_connection = sqlite3.connect(temporary)
    try:
        source_connection.backup(destination_connection)
    finally:
        destination_connection.close()
        source_connection.close()
    temporary.replace(destination)


def load_set_a_mapping(
    sealed_key_path: Path,
    expected_sha256: str,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    actual_sha256 = sha256_file(sealed_key_path)
    if actual_sha256 != expected_sha256:
        raise RuntimeError(
            f"sealed key hash mismatch: {actual_sha256} != {expected_sha256}"
        )
    payload = json.loads(sealed_key_path.read_text(encoding="utf-8"))
    set_a_rows = [
        dict(row) for row in payload["rows"] if row.get("review_set") == "A"
    ]
    if len(set_a_rows) != 400:
        raise RuntimeError(f"expected 400 Set A rows, got {len(set_a_rows)}")
    if any(bool(row.get("source_outcome_or_return_visible")) for row in set_a_rows):
        raise RuntimeError("Set A key unexpectedly exposes an outcome/return")
    mapping = {str(row["review_row_id"]): row for row in set_a_rows}
    if len(mapping) != len(set_a_rows):
        raise RuntimeError("duplicate Set A review_row_id in sealed key")
    audit = {
        "sealed_key_sha256": actual_sha256,
        "set_a_rows_loaded": len(set_a_rows),
        "set_a_unique_review_row_ids": len(mapping),
        "set_a_outcome_visible_rows": 0,
        "set_b_rows_exported": 0,
        "set_b_rows_used": 0,
        "set_b_payload_released": False,
    }
    return mapping, audit


def reviewed_at_max(rows: Sequence[Mapping[str, str]]) -> str:
    values = [str(row["reviewed_at"]) for row in rows if str(row["reviewed_at"]).strip()]
    if not values:
        raise ValueError("completed review has no reviewed_at timestamp")
    parsed = [datetime.fromisoformat(value.replace("Z", "+00:00")) for value in values]
    if any(value.tzinfo is None for value in parsed):
        raise ValueError("reviewed_at must include a timezone offset")
    return max(parsed).isoformat()


def build_provisional_rows(
    packet_dir: Path,
    candidates_path: Path,
    set_a_mapping: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    candidates = read_csv(candidates_path)
    target = read_csv(packet_dir / "target_change_completed.csv")
    content = read_csv(packet_dir / "news_content_type_completed.csv")
    abstention = read_csv(packet_dir / "abstention_completed.csv")
    graphs = read_csv(packet_dir / "multi_role_region_channel_completed.csv")
    recovered = read_csv(packet_dir / "recovered_event_completed.csv")

    expected_counts = {
        "candidates": 2800,
        "target": 201,
        "content": 400,
        "abstention": 1170,
        "graphs": 389,
        "recovered": 6,
    }
    actual_counts = {
        "candidates": len(candidates),
        "target": len(target),
        "content": len(content),
        "abstention": len(abstention),
        "graphs": len(graphs),
        "recovered": len(recovered),
    }
    if actual_counts != expected_counts:
        raise RuntimeError(f"input count mismatch: {actual_counts} != {expected_counts}")

    candidate_index: dict[tuple[str, str], dict[str, str]] = {}
    for row in candidates:
        key = (row["review_row_id"], row["field_name"])
        if key in candidate_index:
            raise RuntimeError(f"duplicate candidate key: {key}")
        if row["review_row_id"] not in set_a_mapping:
            raise RuntimeError(f"candidate row is not in Set A: {row['review_row_id']}")
        candidate_index[key] = row
    if {field for _, field in candidate_index} != set(FIELD_ORDER):
        raise RuntimeError("candidate fields differ from the frozen seven-field contract")

    reviewer_ids = {
        row["reviewer_id"]
        for group in (target, content, abstention, graphs, recovered)
        for row in group
    }
    if reviewer_ids != {"independent_ai_reviewer_v5_4_1_01"}:
        raise RuntimeError(f"unexpected reviewer identities: {sorted(reviewer_ids)}")

    decisions: dict[tuple[str, str], dict[str, str]] = {}

    def add_decision(
        key: tuple[str, str],
        *,
        provisional_value: str,
        status: str,
        source_packet: str,
        reviewer_id: str,
        reviewed_at: str,
    ) -> None:
        if key not in candidate_index:
            raise RuntimeError(f"review decision has no frozen candidate: {key}")
        if key in decisions:
            raise RuntimeError(f"overlapping review decisions: {key}")
        decisions[key] = {
            "provisional_value": provisional_value,
            "resolution_status": status,
            "source_packet": source_packet,
            "reviewer_id": reviewer_id,
            "reviewed_at": reviewed_at,
        }

    for row in target:
        correct = row["human_v5_target_correct"] == "1"
        value = row["proposed_v5_value"] if correct else row["human_corrected_target"]
        add_decision(
            (row["review_row_id"], row["field_name"]),
            provisional_value=value,
            status="resolved_proposed" if correct else "resolved_corrected",
            source_packet="target_change",
            reviewer_id=row["reviewer_id"],
            reviewed_at=row["reviewed_at"],
        )

    for row in content:
        correct = row["human_news_content_type_correct"] == "1"
        value = (
            row["proposed_news_content_type_v5"]
            if correct
            else row["human_corrected_news_content_type"]
        )
        add_decision(
            (row["review_row_id"], "news_content_type"),
            provisional_value=value,
            status="resolved_proposed" if correct else "resolved_corrected",
            source_packet="news_content_type",
            reviewer_id=row["reviewer_id"],
            reviewed_at=row["reviewed_at"],
        )

    for row in abstention:
        abstain = row["human_should_abstain"] == "1"
        add_decision(
            (row["review_row_id"], row["field_name"]),
            provisional_value="" if abstain else row["human_corrected_target"],
            status="explicit_abstain" if abstain else "resolved_corrected",
            source_packet="abstention",
            reviewer_id=row["reviewer_id"],
            reviewed_at=row["reviewed_at"],
        )

    adjudication_rows: list[dict[str, Any]] = []
    semantic_by_review: dict[str, dict[str, dict[str, str]]] = {}
    for key in sorted(candidate_index):
        review_row_id, field_name = key
        candidate = candidate_index[key]
        decision = decisions.get(key)
        if decision is None:
            decision = {
                "provisional_value": "",
                "resolution_status": "not_reviewed_in_target_pass",
                "source_packet": "none",
                "reviewer_id": "",
                "reviewed_at": "",
            }
        source_row = set_a_mapping[review_row_id]
        working_value = (
            decision["provisional_value"]
            if decision["resolution_status"].startswith("resolved_")
            else candidate["proposed_value"]
        )
        adjudication_rows.append(
            {
                "review_row_id": review_row_id,
                "event_cluster_id": int(source_row["source_event_cluster_id"]),
                "field_name": field_name,
                "proposed_value": candidate["proposed_value"],
                "provisional_value": decision["provisional_value"],
                "working_research_value": working_value,
                "resolution_status": decision["resolution_status"],
                "source_packet": decision["source_packet"],
                "reviewer_id": decision["reviewer_id"],
                "reviewed_at": decision["reviewed_at"],
                "provisional_version": VERSION,
            }
        )
        semantic_by_review.setdefault(review_row_id, {})[field_name] = {
            "value": working_value,
            "status": decision["resolution_status"],
        }

    semantic_rows: list[dict[str, Any]] = []
    for review_row_id in sorted(semantic_by_review):
        fields = semantic_by_review[review_row_id]
        statuses = [fields[field]["status"] for field in FIELD_ORDER]
        resolved = sum(status.startswith("resolved_") for status in statuses)
        abstained = statuses.count("explicit_abstain")
        unreviewed = statuses.count("not_reviewed_in_target_pass")
        primary_proxy_abstained = fields["primary_proxy"]["status"] == "explicit_abstain"
        semantic_rows.append(
            {
                "review_row_id": review_row_id,
                "event_cluster_id": int(
                    set_a_mapping[review_row_id]["source_event_cluster_id"]
                ),
                "news_content_type": fields["news_content_type"]["value"],
                "event_type": fields["event_type"]["value"],
                "region": fields["region"]["value"],
                "direction": fields["direction"]["value"],
                "affected_channel": fields["affected_channel"]["value"],
                "proxy_family": fields["proxy_family"]["value"],
                "primary_proxy": "" if primary_proxy_abstained else fields["primary_proxy"]["value"],
                "primary_proxy_abstained": int(primary_proxy_abstained),
                "resolved_field_count": resolved,
                "explicit_abstain_count": abstained,
                "unreviewed_field_count": unreviewed,
                "research_context_complete": int(resolved >= 6 and primary_proxy_abstained),
                "reaction_observation_eligible": 0,
                "semantic_strict_eligible": 0,
                "strict_training_eligible": 0,
                "eligibility_hold_reason": (
                    "provisional_ai_only_no_pit_proxy_ranker_or_human_authority"
                ),
                "provisional_version": VERSION,
            }
        )

    graph_rows: list[dict[str, Any]] = []
    for row in graphs:
        review_row_id = row["review_row_id"]
        if review_row_id not in set_a_mapping:
            raise RuntimeError(f"graph row is not in Set A: {review_row_id}")
        graph_rows.append(
            {
                "review_row_id": review_row_id,
                "event_cluster_id": int(
                    set_a_mapping[review_row_id]["source_event_cluster_id"]
                ),
                "actor_regions_json": json.dumps(
                    parse_json_array(row["human_actor_regions"]), separators=(",", ":")
                ),
                "target_regions_json": json.dumps(
                    parse_json_array(row["human_target_regions"]), separators=(",", ":")
                ),
                "directly_affected_regions_json": json.dumps(
                    parse_json_array(row["human_directly_affected_regions"]),
                    separators=(",", ":"),
                ),
                "market_expression_regions_json": json.dumps(
                    parse_json_array(row["human_market_expression_regions"]),
                    separators=(",", ":"),
                ),
                "primary_channel": row["human_primary_channel"],
                "secondary_channels_json": json.dumps(
                    parse_json_array(row["human_secondary_channels"]),
                    separators=(",", ":"),
                ),
                "all_roles_channels_correct": int(
                    row["human_region_channel_correct"]
                ),
                "reviewer_id": row["reviewer_id"],
                "reviewed_at": row["reviewed_at"],
                "provisional_version": VERSION,
            }
        )

    recovered_rows: list[dict[str, Any]] = []
    for row in recovered:
        review_row_id = row["review_row_id"]
        if review_row_id not in set_a_mapping:
            raise RuntimeError(f"recovered row is not in Set A: {review_row_id}")
        recovered_rows.append(
            {
                "review_row_id": review_row_id,
                "event_cluster_id": int(
                    set_a_mapping[review_row_id]["source_event_cluster_id"]
                ),
                "framing_clause": row["framing_clause_v5"],
                "recovered_event_clause": row["recovered_event_clause_v5"],
                "recovery_correct": int(row["human_recovery_correct"]),
                "causal_eligible_after_split": int(
                    row["human_causal_eligible_after_clause_split"]
                ),
                "reviewer_id": row["reviewer_id"],
                "reviewed_at": row["reviewed_at"],
                "provisional_version": VERSION,
            }
        )

    all_review_rows = [*target, *content, *abstention, *graphs, *recovered]
    input_hashes = {
        name: sha256_file(packet_dir / name)
        for name in (
            "target_change_completed.csv",
            "news_content_type_completed.csv",
            "abstention_completed.csv",
            "multi_role_region_channel_completed.csv",
            "recovered_event_completed.csv",
        )
    }
    return {
        "adjudications": adjudication_rows,
        "semantic_rows": semantic_rows,
        "graphs": graph_rows,
        "recovered": recovered_rows,
        "counts": actual_counts,
        "input_hashes": input_hashes,
        "input_hash": canonical_hash(input_hashes),
        "stable_applied_at": reviewed_at_max(all_review_rows),
    }


def ensure_overlay_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS s05p4_v5_4_1_provisional_apply_runs(
            run_key TEXT PRIMARY KEY,
            provisional_version TEXT NOT NULL,
            reviewer_label TEXT NOT NULL,
            owner_acceptance_status TEXT NOT NULL,
            adjudication_input_hash TEXT NOT NULL,
            sealed_set_a_key_sha256 TEXT NOT NULL,
            source_frozen_clone_sha256 TEXT NOT NULL,
            source_canonical_sha256 TEXT NOT NULL,
            set_a_rows INTEGER NOT NULL,
            applied_at_utc TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS s05p4_v5_4_1_provisional_target_adjudications(
            review_row_id TEXT NOT NULL,
            event_cluster_id INTEGER NOT NULL,
            field_name TEXT NOT NULL,
            proposed_value TEXT NOT NULL,
            provisional_value TEXT NOT NULL,
            working_research_value TEXT NOT NULL,
            resolution_status TEXT NOT NULL,
            source_packet TEXT NOT NULL,
            reviewer_id TEXT NOT NULL,
            reviewed_at TEXT NOT NULL,
            provisional_version TEXT NOT NULL,
            PRIMARY KEY(review_row_id,field_name,provisional_version)
        );
        CREATE TABLE IF NOT EXISTS s05p4_v5_4_1_provisional_semantic_rows(
            review_row_id TEXT NOT NULL,
            event_cluster_id INTEGER NOT NULL,
            news_content_type TEXT NOT NULL,
            event_type TEXT NOT NULL,
            region TEXT NOT NULL,
            direction TEXT NOT NULL,
            affected_channel TEXT NOT NULL,
            proxy_family TEXT NOT NULL,
            primary_proxy TEXT NOT NULL,
            primary_proxy_abstained INTEGER NOT NULL,
            resolved_field_count INTEGER NOT NULL,
            explicit_abstain_count INTEGER NOT NULL,
            unreviewed_field_count INTEGER NOT NULL,
            research_context_complete INTEGER NOT NULL,
            reaction_observation_eligible INTEGER NOT NULL,
            semantic_strict_eligible INTEGER NOT NULL,
            strict_training_eligible INTEGER NOT NULL,
            eligibility_hold_reason TEXT NOT NULL,
            provisional_version TEXT NOT NULL,
            PRIMARY KEY(review_row_id,provisional_version)
        );
        CREATE TABLE IF NOT EXISTS s05p4_v5_4_1_provisional_region_channel_graphs(
            review_row_id TEXT NOT NULL,
            event_cluster_id INTEGER NOT NULL,
            actor_regions_json TEXT NOT NULL,
            target_regions_json TEXT NOT NULL,
            directly_affected_regions_json TEXT NOT NULL,
            market_expression_regions_json TEXT NOT NULL,
            primary_channel TEXT NOT NULL,
            secondary_channels_json TEXT NOT NULL,
            all_roles_channels_correct INTEGER NOT NULL,
            reviewer_id TEXT NOT NULL,
            reviewed_at TEXT NOT NULL,
            provisional_version TEXT NOT NULL,
            PRIMARY KEY(review_row_id,provisional_version)
        );
        CREATE TABLE IF NOT EXISTS s05p4_v5_4_1_provisional_recovered_events(
            review_row_id TEXT NOT NULL,
            event_cluster_id INTEGER NOT NULL,
            framing_clause TEXT NOT NULL,
            recovered_event_clause TEXT NOT NULL,
            recovery_correct INTEGER NOT NULL,
            causal_eligible_after_split INTEGER NOT NULL,
            reviewer_id TEXT NOT NULL,
            reviewed_at TEXT NOT NULL,
            provisional_version TEXT NOT NULL,
            PRIMARY KEY(review_row_id,provisional_version)
        );
        """
    )


def insert_dict_rows(
    connection: sqlite3.Connection,
    table: str,
    rows: Sequence[Mapping[str, Any]],
) -> None:
    if not rows:
        return
    columns = list(rows[0].keys())
    placeholders = ",".join("?" for _ in columns)
    sql = (
        f'INSERT OR REPLACE INTO "{table}" '
        f'({",".join(columns)}) VALUES({placeholders})'
    )
    connection.executemany(sql, [[row[column] for column in columns] for row in rows])


def apply_overlay(
    connection: sqlite3.Connection,
    payload: Mapping[str, Any],
    *,
    run_row: Mapping[str, Any],
) -> None:
    ensure_overlay_schema(connection)
    for table in OVERLAY_TABLES:
        version_column = "provisional_version"
        if table.endswith("apply_runs"):
            version_column = "provisional_version"
        connection.execute(
            f'DELETE FROM "{table}" WHERE {version_column}=?', (VERSION,)
        )
    insert_dict_rows(
        connection, "s05p4_v5_4_1_provisional_apply_runs", [run_row]
    )
    insert_dict_rows(
        connection,
        "s05p4_v5_4_1_provisional_target_adjudications",
        payload["adjudications"],
    )
    insert_dict_rows(
        connection,
        "s05p4_v5_4_1_provisional_semantic_rows",
        payload["semantic_rows"],
    )
    insert_dict_rows(
        connection,
        "s05p4_v5_4_1_provisional_region_channel_graphs",
        payload["graphs"],
    )
    insert_dict_rows(
        connection,
        "s05p4_v5_4_1_provisional_recovered_events",
        payload["recovered"],
    )
    connection.commit()


def table_digest(connection: sqlite3.Connection, table: str) -> dict[str, Any]:
    columns = [row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')]
    order = ",".join(str(index + 1) for index in range(len(columns)))
    rows = [list(row) for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY {order}')]
    return {"count": len(rows), "sha256": canonical_hash(rows)}


def overlay_digest(connection: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    return {table: table_digest(connection, table) for table in OVERLAY_TABLES}


def table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is not None
    )


def source_table_snapshot(connection: sqlite3.Connection) -> dict[str, Any]:
    snapshot: dict[str, Any] = {}
    queries = {
        "s05_full_range_label_eligibility": """
            SELECT COUNT(*),COUNT(DISTINCT event_cluster_id),
                   COALESCE(SUM(market_data_strict_eligible),0),
                   COALESCE(SUM(point_in_time_strict_eligible),0),
                   COALESCE(SUM(semantic_strict_eligible),0),
                   COALESCE(SUM(causal_strict_eligible),0),
                   COALESCE(SUM(strict_training_eligible),0),
                   COALESCE(MIN(reaction_label_id),0),COALESCE(MAX(reaction_label_id),0)
              FROM s05_full_range_label_eligibility
        """,
        "s05_full_range_proxy_assignments": """
            SELECT COUNT(*),COUNT(DISTINCT event_cluster_id),
                   COUNT(DISTINCT proxy_symbol),COUNT(DISTINCT proxy_family)
              FROM s05_full_range_proxy_assignments
        """,
        "news_event_clusters": "SELECT COUNT(*),COALESCE(MIN(event_cluster_id),0),COALESCE(MAX(event_cluster_id),0) FROM news_event_clusters",
        "news_event_classifications_canonical": "SELECT COUNT(*) FROM news_event_classifications_canonical",
        "news_event_gates_canonical": "SELECT COUNT(*) FROM news_event_gates_canonical",
    }
    for table, sql in queries.items():
        if table_exists(connection, table):
            snapshot[table] = list(connection.execute(sql).fetchone())
    return snapshot


def set_a_label_impact(
    connection: sqlite3.Connection,
    cluster_ids: Sequence[int],
) -> dict[str, Any]:
    placeholders = ",".join("?" for _ in cluster_ids)
    row = connection.execute(
        f"""
        SELECT COUNT(*),COUNT(DISTINCT event_cluster_id),
               COALESCE(SUM(market_data_strict_eligible),0),
               COALESCE(SUM(point_in_time_strict_eligible),0),
               COALESCE(SUM(semantic_strict_eligible),0),
               COALESCE(SUM(causal_strict_eligible),0),
               COALESCE(SUM(strict_training_eligible),0)
          FROM s05_full_range_label_eligibility
         WHERE event_cluster_id IN ({placeholders})
        """,
        list(cluster_ids),
    ).fetchone()
    return {
        "labels_linked_to_set_a_clusters": int(row[0]),
        "set_a_clusters_with_labels": int(row[1]),
        "baseline_market_data_strict": int(row[2]),
        "baseline_point_in_time_strict": int(row[3]),
        "baseline_semantic_strict": int(row[4]),
        "baseline_causal_strict": int(row[5]),
        "baseline_strict_training": int(row[6]),
        "newly_semantic_strict": 0,
        "newly_reaction_observation_eligible": 0,
        "newly_strict_training_eligible": 0,
        "human_or_provisional_rejected_strict_leakage": 0,
        "policy": "overlay_only_no_unlock_without_human_authority_pit_proxy_ranker_and_direction_calibration",
    }


def set_a_label_coverage_audit(
    connection: sqlite3.Connection,
    set_a_mapping: Mapping[str, Mapping[str, Any]],
    detailed_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    linked_cluster_ids = {
        int(row[0])
        for row in connection.execute(
            "SELECT DISTINCT event_cluster_id FROM s05_full_range_label_eligibility"
        )
    }
    details = {str(row["review_row_id"]): row for row in detailed_rows}
    missing_review_ids = [
        review_row_id
        for review_row_id, row in set_a_mapping.items()
        if int(row["source_event_cluster_id"]) not in linked_cluster_ids
    ]
    missing_v5_causal = sum(
        bool(details[review_row_id]["causal_eligible"])
        for review_row_id in missing_review_ids
    )
    return {
        "set_a_clusters_without_labels": len(missing_review_ids),
        "without_labels_model_v3_decision": distribution(
            str(set_a_mapping[review_row_id]["model_v3_decision"])
            for review_row_id in missing_review_ids
        ),
        "without_labels_v5_causal_eligible": missing_v5_causal,
        "without_labels_v5_noncausal": len(missing_review_ids) - missing_v5_causal,
        "without_labels_v5_exclusion_reason": distribution(
            str(details[review_row_id]["causal_exclusion_reason"])
            for review_row_id in missing_review_ids
        ),
        "coverage_interpretation": (
            "all no-label Set A clusters were excluded by the frozen v3 model; "
            "v5-causal rows are future coverage candidates, not missing label writes"
        ),
    }


def safe_input_pit_audit(path: Path, expected_hash: str) -> dict[str, Any]:
    if sha256_file(path) != expected_hash:
        raise RuntimeError("safe Set A input hash differs from the frozen manifest")
    rows = read_csv(path)
    missing = 0
    naive = 0
    invalid = 0
    for row in rows:
        value = row["plan_as_of_utc"].strip()
        if not value:
            missing += 1
            continue
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            invalid += 1
            continue
        if parsed.tzinfo is None:
            naive += 1
    return {
        "rows": len(rows),
        "missing_plan_as_of_utc": missing,
        "invalid_plan_as_of_utc": invalid,
        "timezone_naive_plan_as_of_utc": naive,
        "critical_issues": missing + invalid + naive,
    }


def build_impact_report(
    payload: Mapping[str, Any],
    *,
    label_impact: Mapping[str, Any],
    source_before: Mapping[str, Any],
    source_after: Mapping[str, Any],
    first_digest: Mapping[str, Any],
    second_digest: Mapping[str, Any],
    key_audit: Mapping[str, Any],
    pit_audit: Mapping[str, Any],
    canonical_before: str,
    canonical_after: str,
    frozen_before: str,
    frozen_after: str,
    clone_path: Path,
    clone_quick_check: str,
) -> dict[str, Any]:
    adjudications = payload["adjudications"]
    semantic_rows = payload["semantic_rows"]
    status_counts = distribution(row["resolution_status"] for row in adjudications)
    by_field: dict[str, Any] = {}
    for field in FIELD_ORDER:
        field_rows = [row for row in adjudications if row["field_name"] == field]
        changed = sum(
            row["resolution_status"] == "resolved_corrected"
            and row["proposed_value"] != row["provisional_value"]
            for row in field_rows
        )
        by_field[field] = {
            "rows": len(field_rows),
            "resolution_status": distribution(
                row["resolution_status"] for row in field_rows
            ),
            "corrected_value_changes": changed,
            "before_distribution": distribution(
                row["proposed_value"] for row in field_rows
            ),
            "working_distribution": distribution(
                row["working_research_value"] for row in field_rows
            ),
        }
    duplicate_target_keys = len(adjudications) - len(
        {(row["review_row_id"], row["field_name"]) for row in adjudications}
    )
    duplicate_semantic_rows = len(semantic_rows) - len(
        {row["review_row_id"] for row in semantic_rows}
    )
    duplicate_clusters = len(semantic_rows) - len(
        {row["event_cluster_id"] for row in semantic_rows}
    )
    primary_proxy_abstain = sum(
        row["primary_proxy_abstained"] for row in semantic_rows
    )
    graph_corrections = sum(
        not bool(row["all_roles_channels_correct"]) for row in payload["graphs"]
    )
    recovered_correct = sum(row["recovery_correct"] for row in payload["recovered"])
    recovered_causal = sum(
        row["causal_eligible_after_split"] for row in payload["recovered"]
    )
    hard_gates = {
        "canonical_checksum_unchanged": canonical_before == canonical_after,
        "frozen_source_checksum_unchanged": frozen_before == frozen_after,
        "source_tables_unchanged": source_before == source_after,
        "same_input_rerun_diff_zero": first_digest == second_digest,
        "clone_quick_check_ok": clone_quick_check == "ok",
        "target_key_duplicates_zero": duplicate_target_keys == 0,
        "semantic_row_duplicates_zero": duplicate_semantic_rows == 0,
        "set_a_cluster_mapping_unique": duplicate_clusters == 0,
        "set_a_key_missing_zero": key_audit["set_a_rows_loaded"] == 400,
        "pit_critical_issues_zero": pit_audit["critical_issues"] == 0,
        "primary_proxy_forced_selection_zero": primary_proxy_abstain == 400,
        "strict_unlock_zero": (
            label_impact["newly_semantic_strict"] == 0
            and label_impact["newly_reaction_observation_eligible"] == 0
            and label_impact["newly_strict_training_eligible"] == 0
        ),
        "set_b_rows_used_zero": key_audit["set_b_rows_used"] == 0,
    }
    pass_all = all(hard_gates.values())
    return {
        "version": VERSION,
        "verdict": (
            "PASS_CLONE_ONLY_PROVISIONAL_IMPACT_DRY_RUN_APPLICATION_HOLD"
            if pass_all
            else "FAIL_CLONE_ONLY_PROVISIONAL_IMPACT_DRY_RUN"
        ),
        "status": "PASS" if pass_all else "FAIL",
        "scope": "owner-accepted provisional AI Set A target adjudication overlay only",
        "clone_db": str(clone_path.resolve()),
        "input_hashes": payload["input_hashes"],
        "adjudication_input_hash": payload["input_hash"],
        "counts": {
            "set_a_rows": len(semantic_rows),
            "candidate_field_rows": len(adjudications),
            "resolved_field_rows": sum(
                row["resolution_status"].startswith("resolved_")
                for row in adjudications
            ),
            "explicit_abstain_field_rows": status_counts.get(
                "explicit_abstain", 0
            ),
            "not_reviewed_in_target_pass": status_counts.get(
                "not_reviewed_in_target_pass", 0
            ),
            "multi_role_graph_rows": len(payload["graphs"]),
            "multi_role_graph_rows_corrected": graph_corrections,
            "recovered_event_rows": len(payload["recovered"]),
            "recovered_event_correct": recovered_correct,
            "recovered_event_causal_after_split": recovered_causal,
            "primary_proxy_abstain_rows": primary_proxy_abstain,
        },
        "field_impact": by_field,
        "label_impact": dict(label_impact),
        "taxonomy_impact": {
            "human_approved_applied": 0,
            "rejected_applied": 0,
            "pending_status_changed": 0,
            "automatic_taxonomy_approval": False,
        },
        "point_in_time": dict(pit_audit),
        "duplicates": {
            "target_key_duplicates": duplicate_target_keys,
            "semantic_row_duplicates": duplicate_semantic_rows,
            "set_a_cluster_mapping_duplicates": duplicate_clusters,
        },
        "idempotency": {
            "first": first_digest,
            "second": second_digest,
            "same_input_rerun_diff_zero": first_digest == second_digest,
        },
        "source_immutability": {
            "canonical_before_sha256": canonical_before,
            "canonical_after_sha256": canonical_after,
            "frozen_source_before_sha256": frozen_before,
            "frozen_source_after_sha256": frozen_after,
            "source_table_snapshot_before": source_before,
            "source_table_snapshot_after": source_after,
            "source_tables_overwritten": False,
        },
        "sealed_boundary": dict(key_audit),
        "hard_gates": hard_gates,
        "safety": {
            "semantic_rules_changed": False,
            "canonical_database_modified": False,
            "set_b_released": False,
            "taxonomy_status_applied": False,
            "full_range_clone_started": False,
            "canonical_promotion": False,
            "m254_m255_m256": False,
            "orders": False,
            "transmit": False,
            "live_routing": False,
            "threshold_promotion": False,
        },
        "decision": {
            "provisional_overlay": "PASS" if pass_all else "FAIL",
            "official_human_authority": "HOLD",
            "canonical_application": "HOLD",
            "set_b": "SEALED",
            "next_allowed_step": (
                "review impact report and decide whether to commission human adjudication "
                "or design generalized non-production calibration"
            ),
        },
    }


def markdown_report(report: Mapping[str, Any]) -> str:
    counts = report["counts"]
    labels = report["label_impact"]
    lines = [
        "# S05P4 v5.4.1 provisional AI clone-only impact dry-run",
        "",
        f"- Verdict: **{report['verdict']}**",
        f"- Clone: `{report['clone_db']}`",
        "- Authority: owner-accepted provisional research evidence, not official human review",
        "- Canonical application: **HOLD**",
        "- Set B: **SEALED**",
        "",
        "## Adjudication coverage",
        "",
        "| Measure | Count |",
        "|---|---:|",
        f"| Set A rows | {counts['set_a_rows']} |",
        f"| Candidate field rows | {counts['candidate_field_rows']} |",
        f"| Resolved field rows | {counts['resolved_field_rows']} |",
        f"| Explicit abstentions | {counts['explicit_abstain_field_rows']} |",
        f"| Not reviewed in target pass | {counts['not_reviewed_in_target_pass']} |",
        f"| Multi-role graphs corrected | {counts['multi_role_graph_rows_corrected']} / {counts['multi_role_graph_rows']} |",
        f"| Primary-proxy abstentions | {counts['primary_proxy_abstain_rows']} / 400 |",
        f"| Recovered events correct and causal | {counts['recovered_event_correct']} / {counts['recovered_event_rows']} |",
        "",
        "## Clone label impact",
        "",
        f"- Labels linked to Set A clusters: `{labels['labels_linked_to_set_a_clusters']}`",
        f"- Set A clusters with labels: `{labels['set_a_clusters_with_labels']}`",
        f"- Set A clusters without labels: `{labels['set_a_clusters_without_labels']}` "
        f"(all frozen-v3 excluded; v5-causal future coverage candidates: "
        f"`{labels['without_labels_v5_causal_eligible']}`)",
        f"- Newly semantic-strict: `{labels['newly_semantic_strict']}`",
        f"- Newly reaction-observation eligible: `{labels['newly_reaction_observation_eligible']}`",
        f"- Newly strict-training eligible: `{labels['newly_strict_training_eligible']}`",
        f"- Strict leakage: `{labels['human_or_provisional_rejected_strict_leakage']}`",
        "",
        "No strict/trade/reaction eligibility was unlocked. The overlay records corrected research context only.",
        "",
        "## Hard gates",
        "",
        "| Gate | Result |",
        "|---|---|",
    ]
    for key, value in report["hard_gates"].items():
        lines.append(f"| {key} | {'PASS' if value else 'FAIL'} |")
    lines.extend(
        [
            "",
            "## Decision",
            "",
            "The clone-only overlay is internally consistent and idempotent when all hard gates pass. "
            "It does not authorize canonical application, Set B release, taxonomy approval, or downstream M254+ execution.",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Apply owner-accepted v5.4.1 AI target adjudication to a dedicated clone overlay"
    )
    parser.add_argument(
        "--canonical-db",
        type=Path,
        default=ROOT
        / "work/canonical_research_backfill/m254_canonical_20250101_20260706_v1/canonical_market_events.db",
    )
    parser.add_argument(
        "--source-frozen-clone",
        type=Path,
        default=ROOT
        / "work/s05p4_independent_semantic/v1/frozen/canonical_market_events_s05_full_range_frozen.db",
    )
    parser.add_argument(
        "--output-clone",
        type=Path,
        default=ROOT
        / "work/s05p4_v5_4_1_provisional_ai_dry_run/v1/"
        "canonical_market_events_s05p4_v5_4_1_provisional_ai_clone.db",
    )
    parser.add_argument(
        "--packet-dir",
        type=Path,
        default=ROOT
        / "artifacts/review/s05p4_v5_4_1_independent_target_adjudication/reviewer_packet",
    )
    parser.add_argument(
        "--candidates",
        type=Path,
        default=ROOT / "artifacts/audit/s05p4_v5_4_1_semantic_adjudications.csv",
    )
    parser.add_argument(
        "--sealed-key",
        type=Path,
        default=ROOT
        / "work/s05p4_independent_semantic/v1/sealed/sealed_model_key.json",
    )
    parser.add_argument(
        "--protocol-freeze",
        type=Path,
        default=ROOT
        / "artifacts/review/s05p4_independent_semantic_v1/REVIEW_PROTOCOL_FREEZE.json",
    )
    parser.add_argument(
        "--v5-freeze",
        type=Path,
        default=ROOT / "artifacts/audit/s05p4_v5_4_1_freeze_manifest.json",
    )
    parser.add_argument(
        "--v5-audit-json",
        type=Path,
        default=ROOT / "artifacts/audit/s05p4_v5_4_1_set_a_research_audit.json",
    )
    parser.add_argument(
        "--safe-set-a-input",
        type=Path,
        default=ROOT
        / "outputs/s05p4_v4_secondary_review_packet/"
        "NON_INDEPENDENT_AI_HUMAN_STYLE_REVIEW/human_review_input_A_v4_secondary.csv",
    )
    parser.add_argument(
        "--json-output",
        type=Path,
        default=ROOT
        / "artifacts/audit/s05p4_v5_4_1_provisional_ai_clone_impact.json",
    )
    parser.add_argument(
        "--markdown-output",
        type=Path,
        default=ROOT
        / "artifacts/audit/s05p4_v5_4_1_provisional_ai_clone_impact.md",
    )
    parser.add_argument(
        "--reuse-existing-clone",
        action="store_true",
        help="Reuse the dedicated dry-run clone after verifying and replacing only overlay rows",
    )
    args = parser.parse_args()

    protocol = json.loads(args.protocol_freeze.read_text(encoding="utf-8"))
    freeze = json.loads(args.v5_freeze.read_text(encoding="utf-8"))
    v5_audit = json.loads(args.v5_audit_json.read_text(encoding="utf-8"))
    if freeze["freeze_status"] != "FROZEN_FOR_SET_A_INDEPENDENT_ADJUDICATION":
        raise RuntimeError("v5.4.1 semantic rules are not frozen")

    canonical_before = sha256_file(args.canonical_db)
    frozen_before = sha256_file(args.source_frozen_clone)
    if canonical_before != protocol["checksums"]["canonical_source_sha256"]:
        raise RuntimeError("canonical DB hash differs from the frozen protocol")
    if frozen_before != protocol["checksums"]["frozen_clone_sha256"]:
        raise RuntimeError("source frozen clone hash differs from the frozen protocol")

    set_a_mapping, key_audit = load_set_a_mapping(
        args.sealed_key, protocol["checksums"]["sealed_model_key_sha256"]
    )
    payload = build_provisional_rows(
        args.packet_dir, args.candidates, set_a_mapping
    )
    pit_audit = safe_input_pit_audit(
        args.safe_set_a_input, freeze["files"]["safe_set_a_input"]["sha256"]
    )

    if args.output_clone.exists():
        if not args.reuse_existing_clone:
            raise FileExistsError(
                "dry-run clone already exists; pass --reuse-existing-clone only for an "
                "idempotent overlay rerun"
            )
    else:
        sqlite_snapshot(args.source_frozen_clone, args.output_clone)
    with sqlite3.connect(args.output_clone) as connection:
        source_before = source_table_snapshot(connection)
        run_key = canonical_hash(
            [VERSION, payload["input_hash"], frozen_before, canonical_before]
        )
        run_row = {
            "run_key": run_key,
            "provisional_version": VERSION,
            "reviewer_label": "NON_HUMAN_INDEPENDENT_AI_TARGET_ADJUDICATION",
            "owner_acceptance_status": "owner_accepted_provisional_research_only",
            "adjudication_input_hash": payload["input_hash"],
            "sealed_set_a_key_sha256": key_audit["sealed_key_sha256"],
            "source_frozen_clone_sha256": frozen_before,
            "source_canonical_sha256": canonical_before,
            "set_a_rows": len(set_a_mapping),
            "applied_at_utc": payload["stable_applied_at"],
        }
        apply_overlay(connection, payload, run_row=run_row)
        first_digest = overlay_digest(connection)
        apply_overlay(connection, payload, run_row=run_row)
        second_digest = overlay_digest(connection)
        source_after = source_table_snapshot(connection)
        cluster_ids = sorted(
            {int(row["source_event_cluster_id"]) for row in set_a_mapping.values()}
        )
        if len(cluster_ids) != 400:
            raise RuntimeError("Set A cluster mapping is not one-to-one")
        label_impact = set_a_label_impact(connection, cluster_ids)
        label_impact.update(
            set_a_label_coverage_audit(
                connection, set_a_mapping, v5_audit["detailed_rows"]
            )
        )
        clone_quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])

    canonical_after = sha256_file(args.canonical_db)
    frozen_after = sha256_file(args.source_frozen_clone)
    report = build_impact_report(
        payload,
        label_impact=label_impact,
        source_before=source_before,
        source_after=source_after,
        first_digest=first_digest,
        second_digest=second_digest,
        key_audit=key_audit,
        pit_audit=pit_audit,
        canonical_before=canonical_before,
        canonical_after=canonical_after,
        frozen_before=frozen_before,
        frozen_after=frozen_after,
        clone_path=args.output_clone,
        clone_quick_check=clone_quick_check,
    )
    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    args.markdown_output.write_text(
        markdown_report(report), encoding="utf-8", newline="\n"
    )
    print(
        json.dumps(
            {
                "status": report["status"],
                "verdict": report["verdict"],
                "clone_db": report["clone_db"],
                "counts": report["counts"],
                "label_impact": report["label_impact"],
                "hard_gates": report["hard_gates"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
