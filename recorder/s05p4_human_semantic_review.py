from __future__ import annotations

import csv
import hashlib
import json
import math
import sqlite3
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


S05P4_VERSION = "s05p4-independent-human-semantic-review-v1"
CODEBOOK_VERSION = "s05p4-independent-semantic-codebook-v1"

EXCLUSION_REASONS = {
    "market_recap",
    "price_move_recap",
    "preview",
    "opinion_or_analysis",
    "listicle",
    "duplicate_or_repeat",
    "secondary_report_no_new_information",
    "non_material_commentary",
    "wrong_region_or_channel",
    "wrong_proxy_family",
    "wrong_primary_proxy",
    "insufficient_context",
    "other",
}

REVIEW_POLICY = {
    "boolean_values": [0, 1],
    "confidence_values": [1, 2, 3, 4, 5],
    "ambiguous_or_unclear": {
        "missing_values_allowed": False,
        "causal_treatment": "use human_causal_eligible=0 with insufficient_context",
        "correctness_treatment": "use 0 only for fields that cannot be confirmed from the packet",
        "duplicate_treatment": "use 1 only for headline-local update/repeat evidence; otherwise 0",
        "confidence_treatment": "use reviewer_confidence 1 or 2",
        "metric_treatment": "rows remain in denominators; no post-review exclusion",
    },
    "reviewer_conflict": {
        "majority_vote": False,
        "metric_treatment": "exclude unresolved row from metrics and fail the phase",
        "resolution": "document a separate adjudication and compare only the final consolidated output",
        "original_reviews_preserved": True,
    },
    "correctness_metric_population": "all independently reviewed blind rows, including human_causal_eligible=0",
    "critical_error_population": "model_v3_decision=1 and human_causal_eligible=1",
    "duplicate_update_metric": "among human_duplicate_or_update=1 rows, fraction with model_v3_decision=0",
}

REVIEW_INPUT_FIELDS = [
    "review_row_id",
    "normalized_headline",
    "plan_as_of_utc",
    "provider",
    "proposed_content_type",
    "proposed_region",
    "proposed_direction",
    "proposed_affected_channel",
    "proposed_proxy_family",
    "proposed_primary_proxy",
]

HUMAN_REVIEW_FIELDS = [
    "human_causal_eligible",
    "human_primary_event",
    "human_content_type_correct",
    "human_region_correct",
    "human_direction_correct",
    "human_affected_channel_correct",
    "human_proxy_family_correct",
    "human_primary_proxy_correct",
    "human_duplicate_or_update",
    "human_exclusion_reason",
    "reviewer_confidence",
    "reviewer_id",
    "reviewed_at",
]

BOOLEAN_REVIEW_FIELDS = [
    "human_causal_eligible",
    "human_primary_event",
    "human_content_type_correct",
    "human_region_correct",
    "human_direction_correct",
    "human_affected_channel_correct",
    "human_proxy_family_correct",
    "human_primary_proxy_correct",
    "human_duplicate_or_update",
]

BANNED_REVIEW_FIELDS = {
    "causal_assignment_correct",
    "validation_set",
    "review_method",
    "event_cluster_id",
    "automated_eligibility",
    "strict_training_eligible",
    "outcome_or_return_visible",
    "raw_return",
    "abnormal_return",
    "residual_return",
    "mfe",
    "mae",
    "pnl",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_hash(*parts: Any) -> str:
    payload = "\x1f".join(str(part) for part in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def canonical_json_hash(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


def write_csv(path: Path, rows: Sequence[Mapping[str, Any]], fields: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fields), extrasaction="ignore", quoting=csv.QUOTE_ALL)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def calendar_quarter(timestamp: str) -> str:
    value = str(timestamp or "")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return "unknown"
    return f"{parsed.year}Q{((parsed.month - 1) // 3) + 1}"


def _source_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [dict(row) for row in payload]
    if isinstance(payload, dict) and isinstance(payload.get("rows"), list):
        return [dict(row) for row in payload["rows"]]
    raise ValueError("blind review payload must be a list or contain a rows list")


def validate_source_blind_pack(payload: Any) -> dict[str, int]:
    rows = _source_rows(payload)
    blind = [row for row in rows if str(row.get("validation_set")) == "blind"]
    design = [row for row in rows if str(row.get("validation_set")) == "design"]
    unknown = [row for row in rows if str(row.get("validation_set")) not in {"blind", "design"}]
    visible = sum(int(row.get("outcome_or_return_visible") or 0) != 0 for row in rows)
    missing_ids = sum(row.get("event_cluster_id") in {None, ""} for row in rows)
    duplicate_ids = len(rows) - len({str(row.get("event_cluster_id")) for row in rows})
    if unknown:
        raise ValueError(f"source pack has {len(unknown)} rows with unknown validation_set")
    if visible:
        raise ValueError(f"source pack exposes outcome/return on {visible} rows")
    if missing_ids or duplicate_ids:
        raise ValueError(f"source pack id integrity failed: missing={missing_ids}, duplicate={duplicate_ids}")
    return {
        "total": len(rows),
        "blind": len(blind),
        "design": len(design),
        "outcome_or_return_visible": visible,
        "missing_event_cluster_id": missing_ids,
        "duplicate_event_cluster_id": duplicate_ids,
    }


def _review_row_id(seed: str, row: Mapping[str, Any]) -> str:
    digest = stable_hash(
        seed,
        row.get("event_cluster_id"),
        row.get("plan_as_of_utc"),
        row.get("provider"),
        row.get("headline"),
    )
    return f"s05p4-{digest[:24]}"


def _sanitized_row(review_row_id: str, row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "review_row_id": review_row_id,
        "normalized_headline": str(row.get("headline") or ""),
        "plan_as_of_utc": str(row.get("plan_as_of_utc") or ""),
        "provider": str(row.get("provider") or ""),
        "proposed_content_type": str(row.get("content_type") or ""),
        "proposed_region": str(row.get("region") or ""),
        "proposed_direction": str(row.get("direction") or ""),
        "proposed_affected_channel": str(row.get("affected_channel") or ""),
        "proposed_proxy_family": str(row.get("proxy_family") or ""),
        "proposed_primary_proxy": str(row.get("primary_proxy") or ""),
    }


def _stratified_set_a_ids(rows: Sequence[Mapping[str, Any]], seed: str, target_a: int) -> set[str]:
    if target_a < 0 or target_a > len(rows):
        raise ValueError("set A target must be between zero and the blind row count")
    strata: dict[tuple[str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        strata[(
            str(row.get("provider") or ""),
            str(row.get("proxy_family") or ""),
            calendar_quarter(str(row.get("plan_as_of_utc") or "")),
        )].append(row)
    quotas: dict[tuple[str, str, str], int] = {}
    fractions: list[tuple[float, str, tuple[str, str, str]]] = []
    allocated = 0
    total = len(rows)
    for key, values in strata.items():
        exact = len(values) * target_a / total if total else 0.0
        base = math.floor(exact)
        quotas[key] = base
        allocated += base
        fractions.append((exact - base, stable_hash(seed, "quota", *key), key))
    for _, _, key in sorted(fractions, key=lambda item: (-item[0], item[1]))[: target_a - allocated]:
        quotas[key] += 1
    selected: set[str] = set()
    for key, values in strata.items():
        ordered = sorted(
            values,
            key=lambda row: stable_hash(seed, "within", *key, row["review_row_id"]),
        )
        selected.update(str(row["review_row_id"]) for row in ordered[: quotas[key]])
    if len(selected) != target_a:
        raise RuntimeError(f"stratified allocation produced {len(selected)} rows, expected {target_a}")
    return selected


def distribution(rows: Iterable[Mapping[str, Any]], field: str) -> dict[str, int]:
    return dict(sorted(Counter(str(row.get(field) or "") for row in rows).items()))


def sanitize_audit(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    fields = set().union(*(set(row) for row in rows)) if rows else set(REVIEW_INPUT_FIELDS)
    banned_present = sorted(fields & BANNED_REVIEW_FIELDS)
    unexpected = sorted(fields - set(REVIEW_INPUT_FIELDS))
    missing = sorted(set(REVIEW_INPUT_FIELDS) - fields)
    duplicate_review_ids = len(rows) - len({str(row.get("review_row_id")) for row in rows})
    return {
        "row_count": len(rows),
        "fields": sorted(fields),
        "banned_fields_present": banned_present,
        "unexpected_fields": unexpected,
        "missing_required_fields": missing,
        "duplicate_review_row_ids": duplicate_review_ids,
        "pass": not banned_present and not unexpected and not missing and duplicate_review_ids == 0,
    }


def build_review_material(payload: Any, *, seed: str, target_a: int = 400) -> dict[str, Any]:
    source_counts = validate_source_blind_pack(payload)
    source = _source_rows(payload)
    blind_source = [dict(row) for row in source if str(row.get("validation_set")) == "blind"]
    prepared: list[dict[str, Any]] = []
    for source_row in blind_source:
        review_id = _review_row_id(seed, source_row)
        sanitized = _sanitized_row(review_id, source_row)
        prepared.append({**source_row, **sanitized})
    set_a_ids = _stratified_set_a_ids(prepared, seed, target_a)
    sanitized_by_set: dict[str, list[dict[str, Any]]] = {"A": [], "B": []}
    sealed_rows: list[dict[str, Any]] = []
    for row in prepared:
        review_set = "A" if row["review_row_id"] in set_a_ids else "B"
        sanitized = {field: row[field] for field in REVIEW_INPUT_FIELDS}
        sanitized_by_set[review_set].append(sanitized)
        sealed_rows.append({
            "review_row_id": row["review_row_id"],
            "review_set": review_set,
            "source_event_cluster_id": int(row["event_cluster_id"]),
            "source_validation_set": str(row["validation_set"]),
            "model_v3_decision": int(row.get("causal_assignment_correct") or 0),
            "source_review_method": str(row.get("review_method") or ""),
            "source_outcome_or_return_visible": int(row.get("outcome_or_return_visible") or 0),
            "calendar_quarter": calendar_quarter(str(row.get("plan_as_of_utc") or "")),
            "sanitized": sanitized,
            "source_row_hash": canonical_json_hash(row),
        })
    for review_set in ("A", "B"):
        sanitized_by_set[review_set].sort(
            key=lambda row: stable_hash(seed, "order", review_set, row["review_row_id"])
        )
    sealed_rows.sort(key=lambda row: str(row["review_row_id"]))
    all_sanitized = sanitized_by_set["A"] + sanitized_by_set["B"]
    set_distributions = {}
    for review_set, values in sanitized_by_set.items():
        set_distributions[review_set] = {
            "provider": distribution(values, "provider"),
            "proxy_family": distribution(values, "proposed_proxy_family"),
            "calendar_quarter": distribution(
                [
                    {**row, "calendar_quarter": calendar_quarter(str(row.get("plan_as_of_utc") or ""))}
                    for row in values
                ],
                "calendar_quarter",
            ),
        }
    return {
        "source_counts": source_counts,
        "sanitized_by_set": sanitized_by_set,
        "sealed_rows": sealed_rows,
        "audit": {
            "set_counts": {key: len(value) for key, value in sanitized_by_set.items()},
            "set_overlap": len(
                {row["review_row_id"] for row in sanitized_by_set["A"]}
                & {row["review_row_id"] for row in sanitized_by_set["B"]}
            ),
            "design_rows_in_packets": 0,
            "outcome_visible_rows_in_packets": 0,
            "all_rows": sanitize_audit(all_sanitized),
            "set_a": sanitize_audit(sanitized_by_set["A"]),
            "set_b": sanitize_audit(sanitized_by_set["B"]),
            "blind_family_counts": distribution(prepared, "proxy_family"),
            "blind_provider_counts": distribution(prepared, "provider"),
            "blind_period_counts": distribution(
                [{**row, "period": calendar_quarter(str(row.get("plan_as_of_utc") or ""))} for row in prepared],
                "period",
            ),
            "model_decision_counts": dict(sorted(Counter(int(row.get("causal_assignment_correct") or 0) for row in prepared).items())),
            "set_distributions": set_distributions,
        },
    }


def review_output_template(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [{**row, **{field: "" for field in HUMAN_REVIEW_FIELDS}} for row in rows]


def reconcile_proxy_assignments(clone_db: Path, canonical_db: Path) -> dict[str, Any]:
    from .s05_full_range_news_clone import PLAN_VERSION, build_full_range_selection

    with sqlite3.connect(f"file:{clone_db.as_posix()}?mode=ro", uri=True) as conn:
        assignments = int(conn.execute("SELECT COUNT(*) FROM s05_full_range_proxy_assignments").fetchone()[0])
        unique_clusters = int(conn.execute(
            "SELECT COUNT(DISTINCT event_cluster_id) FROM s05_full_range_proxy_assignments"
        ).fetchone()[0])
        unique_plans = int(conn.execute(
            """SELECT COUNT(DISTINCT p.reaction_plan_id)
                 FROM s05_full_range_proxy_assignments a
                 JOIN reaction_universe_plans p USING(event_cluster_id)
                WHERE p.plan_version=?""",
            (PLAN_VERSION,),
        ).fetchone()[0])
        by_purpose = dict(conn.execute(
            """SELECT c.sample_purpose,COUNT(*)
                 FROM s05_full_range_proxy_assignments a
                 JOIN s05_full_range_cohorts c USING(event_cluster_id)
                GROUP BY c.sample_purpose"""
        ).fetchall())
        assignment_ids = {
            int(row[0]) for row in conn.execute("SELECT event_cluster_id FROM s05_full_range_proxy_assignments")
        }
    selection = build_full_range_selection(canonical_db)
    novelty = Counter(
        str(row.get("novelty_type") or "unknown")
        for row in selection["clusters"]
        if int(row["event_cluster_id"]) in assignment_ids
    )
    proxy_only = int(by_purpose.get("event_proxy_causal", 0))
    existing_causal = int(by_purpose.get("causal_event", 0))
    return {
        "proxy_assignments": assignments,
        "proxy_assignment_unique_clusters": unique_clusters,
        "proxy_assignment_unique_plans": unique_plans,
        "assignments_attached_to_existing_causal_event_plans": existing_causal,
        "proxy_only_new_plans": proxy_only,
        "event_proxy_causal_cohorts": proxy_only,
        "initial_update_snapshot_counts": dict(sorted(novelty.items())),
        "equation": f"{assignments} = {proxy_only} proxy-only new plans + {existing_causal} assignments on existing causal_event plans",
        "pass": assignments == unique_clusters == unique_plans == proxy_only + existing_causal,
    }


def _parse_boolean(value: Any) -> int | None:
    text = str(value or "").strip().lower()
    if text == "1":
        return 1
    if text == "0":
        return 0
    if not text:
        return None
    raise ValueError(f"expected exact boolean 0 or 1, got {value!r}")


def _parse_confidence(value: Any) -> int:
    text = str(value or "").strip()
    try:
        confidence = int(text)
    except ValueError as exc:
        raise ValueError(f"reviewer_confidence must be an integer from 1 to 5, got {value!r}") from exc
    if confidence not in REVIEW_POLICY["confidence_values"]:
        raise ValueError(f"reviewer_confidence must be an integer from 1 to 5, got {value!r}")
    return confidence


def _parse_reviewed_at(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError("missing reviewed_at")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"reviewed_at must be ISO-8601, got {value!r}") from exc
    if parsed.tzinfo is None:
        raise ValueError("reviewed_at must include a timezone offset")
    return parsed.isoformat()


def _parse_human_review_row(raw: Mapping[str, Any]) -> dict[str, Any]:
    parsed = dict(raw)
    for field in BOOLEAN_REVIEW_FIELDS:
        value = _parse_boolean(raw.get(field))
        if value is None:
            raise ValueError(f"missing {field}")
        parsed[field] = value
    reviewer_id = str(raw.get("reviewer_id") or "").strip()
    if not reviewer_id:
        raise ValueError("missing reviewer_id")
    parsed["reviewer_id"] = reviewer_id
    parsed["reviewer_confidence"] = _parse_confidence(raw.get("reviewer_confidence"))
    parsed["reviewed_at"] = _parse_reviewed_at(raw.get("reviewed_at"))
    reason = str(raw.get("human_exclusion_reason") or "").strip()
    if parsed["human_causal_eligible"] == 0:
        if not reason:
            raise ValueError("human_exclusion_reason is required when human_causal_eligible=0")
        if reason not in EXCLUSION_REASONS:
            raise ValueError(f"human_exclusion_reason must use the controlled vocabulary, got {reason!r}")
    elif reason:
        raise ValueError("human_exclusion_reason must be blank when human_causal_eligible=1")
    if parsed["human_causal_eligible"] == 0 and parsed["human_primary_event"] == 1:
        raise ValueError("human_primary_event=1 is inconsistent with human_causal_eligible=0")
    parsed["human_exclusion_reason"] = reason
    return parsed


def validate_review_output_without_model_key(
    review_input: Path,
    review_outputs: Sequence[Path],
) -> dict[str, Any]:
    expected_rows = read_csv(review_input)
    expected_fields = set(REVIEW_INPUT_FIELDS)
    input_fields = set(expected_rows[0]) if expected_rows else set()
    expected_by_id = {str(row.get("review_row_id") or ""): row for row in expected_rows}
    errors: list[str] = []
    if input_fields != expected_fields:
        errors.append(f"review input schema mismatch: {sorted(input_fields)}")
    if "" in expected_by_id or len(expected_by_id) != len(expected_rows):
        errors.append("review input has missing or duplicate review_row_id")
    completed_ids: set[str] = set()
    reviewer_pairs: set[tuple[str, str]] = set()
    parsed_by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    output_hashes: dict[str, str] = {}
    completed_rows = 0
    allowed_output_fields = set(REVIEW_INPUT_FIELDS + HUMAN_REVIEW_FIELDS)
    for path in review_outputs:
        output_hashes[str(path.resolve())] = sha256_file(path)
        rows = read_csv(path)
        fields = set(rows[0]) if rows else set()
        if fields != allowed_output_fields:
            errors.append(f"{path}: output schema mismatch: {sorted(fields)}")
        seen_in_file: set[str] = set()
        for row_number, raw in enumerate(rows, start=2):
            review_id = str(raw.get("review_row_id") or "").strip()
            if review_id in seen_in_file:
                errors.append(f"{path}:{row_number}: duplicate review_row_id {review_id!r}")
                continue
            seen_in_file.add(review_id)
            expected = expected_by_id.get(review_id)
            if expected is None:
                errors.append(f"{path}:{row_number}: unknown review_row_id {review_id!r}")
                continue
            changed = [
                field for field in REVIEW_INPUT_FIELDS
                if str(raw.get(field) or "") != str(expected.get(field) or "")
            ]
            if changed:
                errors.append(f"{path}:{row_number}: frozen input fields changed: {changed}")
                continue
            human_values = [str(raw.get(field) or "").strip() for field in HUMAN_REVIEW_FIELDS]
            if not any(human_values):
                continue
            try:
                parsed = _parse_human_review_row(raw)
            except ValueError as exc:
                errors.append(f"{path}:{row_number}: {exc}")
                continue
            pair = (review_id, str(parsed["reviewer_id"]))
            if pair in reviewer_pairs:
                errors.append(f"{path}:{row_number}: duplicate reviewer submission {pair!r}")
                continue
            reviewer_pairs.add(pair)
            completed_ids.add(review_id)
            completed_rows += 1
            parsed_by_id[review_id].append(parsed)
    for review_id, submissions in sorted(parsed_by_id.items()):
        conflict_fields = [
            field for field in BOOLEAN_REVIEW_FIELDS
            if len({int(row[field]) for row in submissions}) > 1
        ]
        if conflict_fields:
            errors.append(
                f"unresolved reviewer conflict for {review_id}: {conflict_fields}; no majority vote allowed"
            )
    missing_ids = sorted(set(expected_by_id) - completed_ids)
    pass_gate = not errors and not missing_ids and len(completed_ids) == len(expected_by_id)
    return {
        "version": S05P4_VERSION,
        "codebook_version": CODEBOOK_VERSION,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "validation_scope": "format_completeness_and_logical_consistency_without_sealed_model_key",
        "verdict": "PASS" if pass_gate else "HOLD",
        "ready_for_sealed_comparison": pass_gate,
        "expected_rows": len(expected_rows),
        "completed_unique_rows": len(completed_ids),
        "completed_submissions": completed_rows,
        "missing_row_count": len(missing_ids),
        "error_count": len(errors),
        "review_input": str(review_input.resolve()),
        "review_input_sha256": sha256_file(review_input),
        "review_output_sha256": output_hashes,
        "errors": errors,
        "missing_ids": missing_ids,
        "sealed_model_key_opened": False,
        "model_decisions_visible": False,
        "policy": REVIEW_POLICY,
    }


def wilson(successes: int, total: int, z: float = 1.959963984540054) -> dict[str, Any]:
    if total <= 0:
        return {"successes": successes, "total": total, "value": None, "wilson_low": None, "wilson_high": None}
    value = successes / total
    denominator = 1 + z * z / total
    center = (value + z * z / (2 * total)) / denominator
    margin = z * math.sqrt((value * (1 - value) + z * z / (4 * total)) / total) / denominator
    return {
        "successes": successes,
        "total": total,
        "value": value,
        "wilson_low": max(0.0, center - margin),
        "wilson_high": min(1.0, center + margin),
    }


def _confusion(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    result = Counter()
    for row in rows:
        model = int(row["model_v3_decision"])
        human = int(row["human_causal_eligible"])
        result[{(1, 1): "TP", (1, 0): "FP", (0, 1): "FN", (0, 0): "TN"}[(model, human)]] += 1
    return {name: int(result.get(name, 0)) for name in ("TP", "FP", "FN", "TN")}


def _metrics(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    confusion = _confusion(rows)
    tp, fp, fn = confusion["TP"], confusion["FP"], confusion["FN"]
    retained = [row for row in rows if int(row["model_v3_decision"]) == 1 and int(row["human_causal_eligible"]) == 1]
    duplicate_or_update = [row for row in rows if int(row["human_duplicate_or_update"]) == 1]
    return {
        "confusion": confusion,
        "causal_assignment_precision": wilson(tp, tp + fp),
        "causal_assignment_recall": wilson(tp, tp + fn),
        "proxy_family_precision": wilson(
            sum(int(row["human_proxy_family_correct"]) for row in rows), len(rows)
        ),
        "primary_proxy_precision": wilson(
            sum(int(row["human_primary_proxy_correct"]) for row in rows), len(rows)
        ),
        "content_type_accuracy": wilson(
            sum(int(row["human_content_type_correct"]) for row in rows), len(rows)
        ),
        "region_accuracy": wilson(sum(int(row["human_region_correct"]) for row in rows), len(rows)),
        "channel_accuracy": wilson(
            sum(int(row["human_affected_channel_correct"]) for row in rows), len(rows)
        ),
        "direction_accuracy": wilson(
            sum(int(row["human_direction_correct"]) for row in rows), len(rows)
        ),
        "duplicate_update_rejection_precision": wilson(
            sum(int(row["model_v3_decision"]) == 0 for row in duplicate_or_update),
            len(duplicate_or_update),
        ),
        "critical_retained_region_channel_errors": sum(
            int(row["model_v3_decision"]) == 1
            and int(row["human_causal_eligible"]) == 1
            and (int(row["human_region_correct"]) == 0 or int(row["human_affected_channel_correct"]) == 0)
            for row in rows
        ),
    }


def _matrix(rows: Sequence[Mapping[str, Any]], field: str) -> dict[str, Any]:
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row.get(field) or "unknown")].append(row)
    return {key: {"rows": len(values), **_metrics(values)} for key, values in sorted(groups.items())}


def load_completed_reviews(paths: Sequence[Path], sealed_rows: Sequence[Mapping[str, Any]], phase: str) -> dict[str, Any]:
    sealed_by_id = {str(row["review_row_id"]): dict(row) for row in sealed_rows}
    required_ids = {
        review_id for review_id, row in sealed_by_id.items()
        if phase == "full" or str(row["review_set"]) == phase
    }
    submissions: dict[str, list[dict[str, Any]]] = defaultdict(list)
    unknown_ids: set[str] = set()
    parse_errors: list[str] = []
    for path in paths:
        for row_number, raw in enumerate(read_csv(path), start=2):
            review_id = str(raw.get("review_row_id") or "").strip()
            if not review_id or not str(raw.get("human_causal_eligible") or "").strip():
                continue
            if review_id not in sealed_by_id:
                unknown_ids.add(review_id)
                continue
            try:
                parsed = _parse_human_review_row(raw)
                parsed["source_file"] = str(path)
                parsed["source_row_number"] = row_number
                submissions[review_id].append(parsed)
            except ValueError as exc:
                parse_errors.append(f"{path}:{row_number}: {exc}")
    conflicts: list[dict[str, Any]] = []
    adjudicated: list[dict[str, Any]] = []
    incomplete_fields: list[str] = []
    for review_id in sorted(required_ids):
        values = submissions.get(review_id, [])
        if not values:
            continue
        conflict_fields = [
            field for field in BOOLEAN_REVIEW_FIELDS
            if len({int(row[field]) for row in values}) > 1
        ]
        if conflict_fields:
            conflicts.append({"review_row_id": review_id, "fields": conflict_fields, "reviewers": [row["reviewer_id"] for row in values]})
            continue
        chosen = dict(values[0])
        chosen.update({
            "reviewer_ids": sorted({str(row["reviewer_id"]) for row in values}),
            "review_count": len(values),
            "model_v3_decision": int(sealed_by_id[review_id]["model_v3_decision"]),
            "review_set": str(sealed_by_id[review_id]["review_set"]),
            "source_event_cluster_id": int(sealed_by_id[review_id]["source_event_cluster_id"]),
            **dict(sealed_by_id[review_id]["sanitized"]),
        })
        adjudicated.append(chosen)
    reviewed_ids = {str(row["review_row_id"]) for row in adjudicated}
    missing_ids = sorted(required_ids - reviewed_ids - {str(row["review_row_id"]) for row in conflicts})
    return {
        "required_ids": required_ids,
        "adjudicated": adjudicated,
        "conflicts": conflicts,
        "missing_ids": missing_ids,
        "unknown_ids": sorted(unknown_ids),
        "parse_errors": parse_errors,
        "incomplete_fields": incomplete_fields,
        "submission_count": sum(len(values) for values in submissions.values()),
    }


def compare_reviews(
    sealed_key: Mapping[str, Any],
    review_paths: Sequence[Path],
    *,
    phase: str,
    gates: Mapping[str, Any],
    canonical_source_db: Path,
) -> dict[str, Any]:
    if phase not in {"A", "B", "full"}:
        raise ValueError("phase must be A, B, or full")
    sealed_rows = [dict(row) for row in sealed_key["rows"]]
    loaded = load_completed_reviews(review_paths, sealed_rows, phase)
    adjudicated = loaded["adjudicated"]
    required_count = len(loaded["required_ids"])
    metrics = _metrics(adjudicated)
    source_checksum = sha256_file(canonical_source_db)
    canonical_unchanged = source_checksum == str(sealed_key["freeze"]["canonical_source_checksum"])
    complete = (
        len(adjudicated) == required_count
        and not loaded["conflicts"]
        and not loaded["missing_ids"]
        and not loaded["unknown_ids"]
        and not loaded["parse_errors"]
    )
    precision = metrics["causal_assignment_precision"]["value"]
    family_precision = metrics["proxy_family_precision"]["value"]
    primary_precision = metrics["primary_proxy_precision"]["value"]
    phase_gates = {
        "all_required_rows_reviewed": complete,
        "outcome_visibility_zero": int(sealed_key["audit"]["outcome_visible_rows_in_packets"]) == 0,
        "design_rows_in_metrics_zero": int(sealed_key["audit"]["design_rows_in_packets"]) == 0,
        "causal_assignment_precision": precision is not None and precision >= float(gates["causal_assignment_precision_min"]),
        "proxy_family_precision": family_precision is not None and family_precision >= float(gates["proxy_family_precision_min"]),
        "primary_proxy_precision": primary_precision is not None and primary_precision >= float(gates["primary_proxy_precision_min"]),
        "critical_retained_region_channel_errors_zero": metrics["critical_retained_region_channel_errors"] <= int(gates["critical_retained_region_channel_errors_max"]),
        "unresolved_reviewer_conflicts_zero": len(loaded["conflicts"]) <= int(gates["unresolved_reviewer_conflicts_max"]),
        "canonical_source_unchanged": canonical_unchanged,
    }
    phase_pass = all(phase_gates.values())
    result = {
        "version": S05P4_VERSION,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "phase": phase,
        "verdict": "PASS" if phase_pass else "HOLD",
        "phase_gate_pass": phase_pass,
        "required_rows": required_count,
        "reviewed_rows": len(adjudicated),
        "submission_count": loaded["submission_count"],
        "missing_row_count": len(loaded["missing_ids"]),
        "unknown_row_count": len(loaded["unknown_ids"]),
        "parse_error_count": len(loaded["parse_errors"]),
        "unresolved_conflict_count": len(loaded["conflicts"]),
        "metrics": metrics,
        "metric_definitions": {
            "causal_assignment_precision": "TP/(TP+FP)",
            "causal_assignment_recall": "TP/(TP+FN)",
            "correctness_fields": REVIEW_POLICY["correctness_metric_population"],
            "critical_retained_region_channel_errors": REVIEW_POLICY["critical_error_population"],
            "duplicate_update_rejection_precision": REVIEW_POLICY["duplicate_update_metric"],
            "confidence_interval": "two-sided Wilson score interval, z=1.959963984540054",
        },
        "provider_matrix": _matrix(adjudicated, "provider"),
        "family_matrix": _matrix(adjudicated, "proposed_proxy_family"),
        "period_matrix": _matrix(
            [{**row, "calendar_quarter": calendar_quarter(str(row["plan_as_of_utc"]))} for row in adjudicated],
            "calendar_quarter",
        ),
        "djnl": {
            "reviewed_candidates": sum(str(row.get("provider")) == "djnl" for row in adjudicated),
            "model_retained": sum(str(row.get("provider")) == "djnl" and int(row["model_v3_decision"]) == 1 for row in adjudicated),
        },
        "phase_gates": phase_gates,
        "canonical_source_checksum": source_checksum,
        "errors": {
            "parse": loaded["parse_errors"],
            "unknown_ids": loaded["unknown_ids"],
            "missing_ids": loaded["missing_ids"],
            "conflicts": loaded["conflicts"],
        },
        "adjudicated_rows": adjudicated,
    }
    return result


def ensure_s05p4_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS s05p4_human_semantic_adjudications(
            review_row_id TEXT NOT NULL,
            reviewer_id TEXT NOT NULL,
            event_cluster_id INTEGER NOT NULL,
            review_set TEXT NOT NULL,
            model_v3_decision INTEGER NOT NULL,
            human_causal_eligible INTEGER NOT NULL,
            human_primary_event INTEGER NOT NULL,
            human_content_type_correct INTEGER NOT NULL,
            human_region_correct INTEGER NOT NULL,
            human_direction_correct INTEGER NOT NULL,
            human_affected_channel_correct INTEGER NOT NULL,
            human_proxy_family_correct INTEGER NOT NULL,
            human_primary_proxy_correct INTEGER NOT NULL,
            human_duplicate_or_update INTEGER NOT NULL,
            human_exclusion_reason TEXT NOT NULL,
            reviewer_confidence TEXT NOT NULL,
            reviewed_at TEXT NOT NULL,
            semantic_review_version TEXT NOT NULL,
            sealed_key_sha256 TEXT NOT NULL,
            created_at_utc TEXT NOT NULL,
            PRIMARY KEY(review_row_id,reviewer_id,semantic_review_version)
        );
        CREATE TABLE IF NOT EXISTS s05p4_final_label_eligibility(
            reaction_label_id INTEGER NOT NULL,
            event_cluster_id INTEGER NOT NULL,
            model_v3_strict_training_eligible INTEGER NOT NULL,
            human_adjudicated_decision INTEGER,
            final_canonical_eligibility INTEGER NOT NULL,
            final_semantic_strict_eligible INTEGER NOT NULL,
            final_causal_strict_eligible INTEGER NOT NULL,
            final_strict_training_eligible INTEGER NOT NULL,
            semantic_review_version TEXT NOT NULL,
            sealed_key_sha256 TEXT NOT NULL,
            created_at_utc TEXT NOT NULL,
            PRIMARY KEY(reaction_label_id,semantic_review_version)
        );
        """
    )


def _table_digest(conn: sqlite3.Connection, table: str, version: str) -> tuple[int, str]:
    cursor = conn.execute(
        f"SELECT * FROM {table} WHERE semantic_review_version=? ORDER BY 1,2",
        (version,),
    )
    rows = [list(row) for row in cursor.fetchall()]
    return len(rows), canonical_json_hash(rows)


def apply_completed_review_to_clone(
    clone_db: Path,
    sealed_key: Mapping[str, Any],
    comparison: Mapping[str, Any],
    *,
    sealed_key_sha256: str,
) -> dict[str, Any]:
    if comparison.get("phase") != "full" or not comparison.get("phase_gate_pass"):
        raise ValueError("only a complete full-phase PASS comparison may be applied")
    adjudicated = [dict(row) for row in comparison["adjudicated_rows"]]
    by_cluster = {int(row["source_event_cluster_id"]): row for row in adjudicated}
    now = datetime.now(UTC).isoformat()

    def persist(conn: sqlite3.Connection) -> None:
        ensure_s05p4_schema(conn)
        for row in adjudicated:
            for reviewer_id in row["reviewer_ids"]:
                conn.execute(
                    """INSERT OR REPLACE INTO s05p4_human_semantic_adjudications VALUES(
                        ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        row["review_row_id"], reviewer_id, row["source_event_cluster_id"], row["review_set"],
                        row["model_v3_decision"], row["human_causal_eligible"], row["human_primary_event"],
                        row["human_content_type_correct"], row["human_region_correct"], row["human_direction_correct"],
                        row["human_affected_channel_correct"], row["human_proxy_family_correct"],
                        row["human_primary_proxy_correct"], row["human_duplicate_or_update"],
                        str(row.get("human_exclusion_reason") or ""), str(row.get("reviewer_confidence") or ""),
                        str(row.get("reviewed_at") or ""), S05P4_VERSION, sealed_key_sha256, now,
                    ),
                )
        eligibility_rows = conn.execute(
            """SELECT reaction_label_id,event_cluster_id,market_data_strict_eligible,
                      point_in_time_strict_eligible,semantic_strict_eligible,
                      causal_strict_eligible,strict_training_eligible
                 FROM s05_full_range_label_eligibility"""
        ).fetchall()
        for label_id, cluster_id, market, pit, semantic, causal, strict in eligibility_rows:
            human_row = by_cluster.get(int(cluster_id))
            human_decision: int | None = None
            retain = 1
            if human_row is not None:
                human_decision = int(human_row["human_causal_eligible"])
                retain = int(human_row["model_v3_decision"]) & human_decision
            final_semantic = int(semantic) & retain
            final_causal = int(causal) & retain
            final_strict = int(market) & int(pit) & final_semantic & final_causal
            conn.execute(
                """INSERT OR REPLACE INTO s05p4_final_label_eligibility VALUES(
                    ?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    label_id, cluster_id, strict, human_decision, retain, final_semantic,
                    final_causal, final_strict, S05P4_VERSION, sealed_key_sha256, now,
                ),
            )
        conn.commit()

    with sqlite3.connect(clone_db) as conn:
        persist(conn)
        first = {
            table: _table_digest(conn, table, S05P4_VERSION)
            for table in ("s05p4_human_semantic_adjudications", "s05p4_final_label_eligibility")
        }
        persist(conn)
        second = {
            table: _table_digest(conn, table, S05P4_VERSION)
            for table in ("s05p4_human_semantic_adjudications", "s05p4_final_label_eligibility")
        }
        rejected_clusters = [
            int(row["source_event_cluster_id"])
            for row in adjudicated
            if int(row["model_v3_decision"]) == 1 and int(row["human_causal_eligible"]) == 0
        ]
        if rejected_clusters:
            placeholders = ",".join("?" for _ in rejected_clusters)
            rejected_remaining = int(conn.execute(
                f"""SELECT COUNT(*) FROM s05p4_final_label_eligibility
                      WHERE semantic_review_version=? AND event_cluster_id IN ({placeholders})
                        AND final_strict_training_eligible=1""",
                (S05P4_VERSION, *rejected_clusters),
            ).fetchone()[0])
        else:
            rejected_remaining = 0
    return {
        "version": S05P4_VERSION,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "separate_layers": {
            "model_v3_decision": "s05_full_range_label_eligibility",
            "human_adjudicated_decision": "s05p4_human_semantic_adjudications",
            "final_canonical_eligibility": "s05p4_final_label_eligibility",
        },
        "first": {key: {"count": value[0], "digest": value[1]} for key, value in first.items()},
        "second": {key: {"count": value[0], "digest": value[1]} for key, value in second.items()},
        "same_config_rerun_diff_zero": first == second,
        "human_rejected_semantic_strict_labels": rejected_remaining,
        "original_v3_tables_overwritten": False,
    }


def markdown_summary(report: Mapping[str, Any]) -> str:
    lines = [f"# {report.get('version', S05P4_VERSION)}", ""]
    for key in ("generated_at_utc", "status", "verdict", "phase"):
        if key in report:
            lines.append(f"- {key}: `{report[key]}`")
    if "counts" in report:
        lines.extend(["", "## Counts", ""])
        for key, value in report["counts"].items():
            lines.append(f"- {key}: `{value}`")
    if "audit" in report:
        lines.extend(["", "## Audit", "", f"```json\n{json.dumps(report['audit'], indent=2, sort_keys=True)}\n```"])
    if "reconciliation" in report:
        lines.extend(["", "## Proxy assignment reconciliation", "", f"```json\n{json.dumps(report['reconciliation'], indent=2, sort_keys=True)}\n```"])
    if "phase_gates" in report:
        lines.extend(["", "## Phase gates", ""])
        for key, value in report["phase_gates"].items():
            lines.append(f"- {key}: `{'PASS' if value else 'FAIL'}`")
    return "\n".join(lines) + "\n"
