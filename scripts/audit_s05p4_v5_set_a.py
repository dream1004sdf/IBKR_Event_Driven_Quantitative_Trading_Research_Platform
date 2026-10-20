from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from recorder.s05_proxy_semantic_v5 import (  # noqa: E402
    S05P4_V5_VERSION,
    V5PolicyConfig,
    V5SemanticMapping,
    V5Thresholds,
    map_headline_v5,
)


SAFE_COLUMNS = (
    "review_row_id",
    "normalized_headline",
    "plan_as_of_utc",
    "provider",
    "proposed_content_type_v4",
    "proposed_region_v4",
    "proposed_direction_v4",
    "proposed_affected_channel_v4",
    "proposed_proxy_family_v4",
    "proposed_primary_proxy_v4",
)

HUMAN_COLUMNS = (
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
)

FORBIDDEN_INPUT_TOKENS = (
    "return",
    "mfe",
    "mae",
    "pnl",
    "model_decision",
    "strict_eligibility",
    "event_cluster_id",
    "set_b",
    "tp",
    "fp",
    "fn",
    "tn",
)

FIELD_SPECS = {
    "event_type": ("proposed_content_type_v4", "human_content_type_correct"),
    "region": ("proposed_region_v4", "human_region_correct"),
    "direction": ("proposed_direction_v4", "human_direction_correct"),
    "affected_channel": (
        "proposed_affected_channel_v4",
        "human_affected_channel_correct",
    ),
    "proxy_family": ("proposed_proxy_family_v4", "human_proxy_family_correct"),
    "primary_proxy": ("proposed_primary_proxy_v4", "human_primary_proxy_correct"),
}

RULE_CANDIDATES = {
    "news_content_type": "independent_news_content_extraction",
    "event_type": "semantic_event_extraction_independent_of_proxy_family",
    "region": "role_based_region_resolver",
    "direction": "split_event_channel_proxy_direction",
    "affected_channel": "causal_channel_graph",
    "proxy_family": "hard_evidence_family_validator",
    "primary_proxy": "ranked_proxy_with_abstention",
}

REGION_TO_V4 = {
    "US": "united_states",
    "CN": "china",
    "TW": "taiwan",
    "JP": "japan",
    "EU": "eurozone",
    "GB": "united_kingdom",
    "IN": "india",
    "IR": "iran",
    "IL": "middle_east",
    "ME": "middle_east",
    "UA": "ukraine",
    "RU": "russia",
    "KR": "korea",
    "KP": "korea",
    "GLOBAL": "global",
}

CHANNEL_TO_V4 = {
    "rates_discount_rate": "sovereign_risk",
    "fx_currency": "currency",
    "energy_supply": "energy",
    "energy_demand": "energy",
    "macro_activity": "macro",
    "commodity_supply": "trade",
    "trade_export_control": "trade",
    "semiconductor_supply_chain": "technology",
    "defense_demand": "defense",
    "cybersecurity_spending": "technology",
    "regulatory_cost": "technology",
    "credit_liquidity": "sovereign_risk",
    "consumer_demand": "consumer",
    "labor_cost": "labor",
    "inflation_pressure": "inflation",
    "sovereign_risk": "sovereign_risk",
    "risk_off": "defense",
}


_AUDIT_CB_INSTITUTION = (
    r"(?:fed(?:eral\s+reserve)?|fomc|ecb|boe|boj|bank\s+of\s+japan|"
    r"bank\s+of\s+england)"
)
_AUDIT_CB_ACTION = (
    r"(?:(?:cut|cuts|cutting|lowered|raised|hiked|held|left|kept)\s+"
    r"(?:interest\s+)?rates?|(?:rate\s+)?(?:cut|hike|hold|decision))"
)
_AUDIT_PRIOR_CB_DECISION = re.compile(
    rf"(?:\b(?:even\s+as|after|following|since|despite|recent|last|prior|previous|earlier)\b"
    rf".{{0,120}}\b{_AUDIT_CB_INSTITUTION}\b.{{0,100}}\b{_AUDIT_CB_ACTION}\b|"
    rf"\b{_AUDIT_CB_INSTITUTION}\b.{{0,100}}\b{_AUDIT_CB_ACTION}\b.{{0,100}}"
    rf"\b(?:previous|last|prior|earlier|this\s+week(?:'s)?)\b.{{0,30}}\bmeeting\b)",
    re.IGNORECASE,
)
_AUDIT_CB_SPEAKER = re.compile(
    r"\b(?:powell|waller|barkin|kashkari|collins|miran|cook|kugler|schmid|"
    r"bowman|goolsbee|williams|jefferson|barr|daly|logan|bostic|musalem)\b"
    r".{0,50}\b(?:says?|said|speaking|tells?|remarks?|backs?|advocat\w*|"
    r"expects?|project\w*|dissent\w*)\b",
    re.IGNORECASE,
)
_AUDIT_CB_CALENDAR_CUE = re.compile(
    r"\b(?:ahead\s+of|before|next|upcoming|this\s+week(?:'s)?)\b.{0,40}"
    r"\b(?:fed|fomc|central\s+bank|policy|rate)?\s*meeting\b|"
    r"\bmeeting\s+by\s+meeting\b",
    re.IGNORECASE,
)
_AUDIT_MACRO_NUMERIC_CUE = re.compile(
    r"(?:\b\d+(?:\.\d+)?\s*%|\b\d{2,3}(?:,\d{3})+\b).{0,80}"
    r"\b(?:cpi|pce|inflation|payrolls?|unemployment|employment|jobs?|gdp|"
    r"retail\s+sales|production)\b|"
    r"\b(?:cpi|pce|inflation|payrolls?|unemployment|employment|jobs?|gdp|"
    r"retail\s+sales|production)\b.{0,80}"
    r"(?:\b\d+(?:\.\d+)?\s*%|\b\d{2,3}(?:,\d{3})+\b)",
    re.IGNORECASE,
)
_AUDIT_MACRO_RELEASE_AUTHORITY_SUBJECT = re.compile(
    r"^(?:[!\s'\"])*(?:(?:reuters|bloomberg)\s+(?:reports?|says?)\s+)?"
    r"(?:bls|bureau\s+of\s+labor\s+statistics|bea|bureau\s+of\s+economic\s+analysis|"
    r"census(?:\s+bureau)?|labor\s+department|department\s+of\s+labor|"
    r"commerce\s+department|department\s+of\s+commerce|ism|s&p\s+global)\b.{0,40}"
    r"(?:\b(?:reports?|says?|announces?|releases?|publishes?|shows?)\b|[:,-])",
    re.IGNORECASE,
)


def _headline_audit_flags(headline: str) -> dict[str, bool]:
    """Independent headline-only cues used to audit, not to produce, v5 mappings."""
    speaker = bool(_AUDIT_CB_SPEAKER.search(headline))
    return {
        "prior_cb_decision_reference": bool(_AUDIT_PRIOR_CB_DECISION.search(headline)),
        "cb_speaker": speaker,
        "speaker_calendar_reference": speaker and bool(_AUDIT_CB_CALENDAR_CUE.search(headline)),
        "speaker_numeric_macro_reference": (
            speaker
            and bool(_AUDIT_MACRO_NUMERIC_CUE.search(headline))
            and not bool(_AUDIT_MACRO_RELEASE_AUTHORITY_SUBJECT.search(headline))
        ),
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _pytest_evidence(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "status": "NOT_PROVIDED",
            "path": str(path.resolve()),
            "collected": 0,
            "passed": 0,
            "failed": 0,
            "errors": 0,
            "skipped": 0,
        }
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
    collected = sum(int(suite.attrib.get("tests", 0)) for suite in suites)
    failed = sum(int(suite.attrib.get("failures", 0)) for suite in suites)
    errors = sum(int(suite.attrib.get("errors", 0)) for suite in suites)
    skipped = sum(int(suite.attrib.get("skipped", 0)) for suite in suites)
    passed = collected - failed - errors - skipped
    return {
        "status": "PASS" if collected > 0 and failed == 0 and errors == 0 else "FAIL",
        "path": str(path.resolve()),
        "sha256": _sha256(path),
        "collected": collected,
        "passed": passed,
        "failed": failed,
        "errors": errors,
        "skipped": skipped,
    }


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    if not path.exists():
        raise FileNotFoundError(path)
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"missing CSV header: {path}")
        return list(reader.fieldnames), list(reader)


def _validate_safe_packet(
    input_path: Path,
    reviewer_a_path: Path,
    reviewer_b_path: Path,
) -> tuple[list[dict[str, str]], dict[str, dict[str, str]], dict[str, dict[str, str]]]:
    for path in (input_path, reviewer_a_path, reviewer_b_path):
        lowered = str(path).lower()
        if "set_b" in lowered or "input_b" in lowered or "lockbox" in lowered:
            raise RuntimeError(f"Set B/lockbox path is forbidden: {path}")

    input_fields, input_rows = _read_csv(input_path)
    a_fields, a_rows = _read_csv(reviewer_a_path)
    b_fields, b_rows = _read_csv(reviewer_b_path)
    if tuple(input_fields) != SAFE_COLUMNS:
        raise ValueError(f"v5 input must contain exactly the frozen safe columns: {input_fields}")
    for name in input_fields:
        lowered = name.lower()
        if any(token in lowered for token in FORBIDDEN_INPUT_TOKENS):
            raise ValueError(f"forbidden field in v5 input: {name}")
    expected_review_fields = SAFE_COLUMNS + HUMAN_COLUMNS
    if tuple(a_fields) != expected_review_fields:
        raise ValueError("reviewer A columns differ from the frozen secondary template")
    if tuple(b_fields) != expected_review_fields:
        raise ValueError("reviewer B columns differ from the frozen secondary template")
    if len(input_rows) != 400 or len(a_rows) != 400 or len(b_rows) != 400:
        raise ValueError(
            f"expected 400 rows in input/A/B, got {len(input_rows)}/{len(a_rows)}/{len(b_rows)}"
        )

    def index(rows: Iterable[dict[str, str]], label: str) -> dict[str, dict[str, str]]:
        result: dict[str, dict[str, str]] = {}
        for row in rows:
            row_id = row["review_row_id"]
            if not row_id or row_id in result:
                raise ValueError(f"missing/duplicate review_row_id in {label}: {row_id}")
            result[row_id] = row
        return result

    input_index = index(input_rows, "input")
    a_index = index(a_rows, "reviewer A")
    b_index = index(b_rows, "reviewer B")
    if set(input_index) != set(a_index) or set(input_index) != set(b_index):
        raise ValueError("review_row_id sets differ across input/reviewer files")
    for row_id, source in input_index.items():
        for review_label, review in (("A", a_index[row_id]), ("B", b_index[row_id])):
            for column in SAFE_COLUMNS:
                if source[column] != review[column]:
                    raise ValueError(
                        f"frozen input value changed in reviewer {review_label}: {row_id}.{column}"
                    )
            for column in HUMAN_COLUMNS:
                if review[column] == "" and column not in {"human_exclusion_reason"}:
                    raise ValueError(f"missing reviewer value: {review_label}.{row_id}.{column}")
            for column in HUMAN_COLUMNS:
                if column.startswith("human_") and column != "human_exclusion_reason":
                    if review[column] not in {"0", "1"}:
                        raise ValueError(f"invalid boolean: {review_label}.{row_id}.{column}")
            if review["reviewer_confidence"] not in {"1", "2", "3", "4", "5"}:
                raise ValueError(f"invalid reviewer confidence: {review_label}.{row_id}")
    return input_rows, a_index, b_index


def _primary_region_v4(mapping: V5SemanticMapping) -> str:
    for role in ("market_expression", "directly_affected", "target", "actor"):
        candidates = [row for row in mapping.regions if row.region_role == role]
        if candidates:
            best = max(candidates, key=lambda item: item.confidence)
            return REGION_TO_V4.get(best.region_code, best.region_code.lower())
    return ""


def _primary_channel_v4(mapping: V5SemanticMapping) -> str:
    candidates = [row for row in mapping.channels if row.channel_role == "primary"]
    if not candidates:
        return ""
    best = max(candidates, key=lambda item: item.confidence)
    return CHANNEL_TO_V4.get(best.channel_code, best.channel_code)


def _v5_field_values(mapping: V5SemanticMapping) -> dict[str, str]:
    return {
        "news_content_type": mapping.news_content_type,
        "event_type": mapping.event_type,
        "region": _primary_region_v4(mapping),
        "direction": mapping.event_direction,
        "affected_channel": _primary_channel_v4(mapping),
        "proxy_family": mapping.proxy_family,
        "primary_proxy": mapping.primary_proxy_selected,
    }


def _confusion(predicted: Iterable[int], actual: Iterable[int]) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    for prediction, target in zip(predicted, actual):
        counts[{(1, 1): "TP", (1, 0): "FP", (0, 1): "FN", (0, 0): "TN"}[
            (int(prediction), int(target))
        ]] += 1
    tp, fp, fn = counts["TP"], counts["FP"], counts["FN"]
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    return {
        "TP": counts["TP"],
        "FP": counts["FP"],
        "FN": counts["FN"],
        "TN": counts["TN"],
        "precision": precision,
        "recall": recall,
    }


def _field_is_strict(field: str, mapping: V5SemanticMapping, thresholds: V5Thresholds) -> bool:
    if field == "region":
        return mapping.semantic_mapping_strict and mapping.region_confidence >= thresholds.region
    if field == "affected_channel":
        return mapping.semantic_mapping_strict and mapping.channel_confidence >= thresholds.channel
    if field == "proxy_family":
        return mapping.semantic_mapping_strict and mapping.proxy_family_confidence >= thresholds.proxy_family
    if field == "primary_proxy":
        return bool(
            mapping.semantic_mapping_strict
            and not mapping.proxy_abstained
            and mapping.primary_proxy_confidence >= thresholds.primary_proxy
        )
    if field == "direction":
        return bool(
            mapping.semantic_mapping_strict
            and mapping.event_direction_confidence >= thresholds.event_direction
            and mapping.event_direction not in {"mixed", "unclear"}
        )
    if field == "news_content_type":
        return bool(mapping.news_content_type)
    return mapping.semantic_mapping_strict


def _confidence_for_field(field: str, mapping: V5SemanticMapping) -> float:
    return {
        "news_content_type": 0.0,
        "event_type": 1.0 if mapping.event_type else 0.0,
        "region": mapping.region_confidence,
        "direction": mapping.event_direction_confidence,
        "affected_channel": mapping.channel_confidence,
        "proxy_family": mapping.proxy_family_confidence,
        "primary_proxy": mapping.primary_proxy_confidence,
    }[field]


def _decile(value: float) -> str:
    lower = min(9, int(max(0.0, min(0.999999, value)) * 10))
    return f"{lower / 10:.1f}-{(lower + 1) / 10:.1f}"


def build_report(
    input_rows: list[dict[str, str]],
    reviewer_a: Mapping[str, dict[str, str]],
    reviewer_b: Mapping[str, dict[str, str]],
    *,
    policy_config: V5PolicyConfig | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    policy_config = policy_config or V5PolicyConfig()
    thresholds = policy_config.thresholds
    mappings: list[tuple[dict[str, str], V5SemanticMapping]] = []
    tiers: Counter[str] = Counter()
    families: Counter[str] = Counter()
    exclusions: Counter[str] = Counter()
    conflicts: Counter[str] = Counter()
    content_statuses: Counter[str] = Counter()
    recovery_statuses: Counter[str] = Counter()
    status: Counter[str] = Counter()
    adjudications: list[dict[str, Any]] = []
    field_stats: dict[str, Counter[str]] = {field: Counter() for field in FIELD_SPECS}
    calibration: dict[str, dict[str, dict[str, Counter[str]]]] = {
        field: {
            "reviewer_a": defaultdict(Counter),
            "reviewer_b": defaultdict(Counter),
        }
        for field in FIELD_SPECS
    }

    for source in input_rows:
        row_id = source["review_row_id"]
        mapping = map_headline_v5(
            source["normalized_headline"],
            provider=source["provider"],
            policy_config=policy_config,
        )
        mappings.append((source, mapping))
        tiers[mapping.mapping_tier] += 1
        families[mapping.proxy_family or "<abstain>"] += 1
        conflicts.update(mapping.critical_conflicts)
        content_statuses[mapping.content_type_status] += 1
        recovery_statuses[mapping.recovery_action_status] += 1
        if mapping.causal_exclusion_reason:
            exclusions[mapping.causal_exclusion_reason] += 1
        status["causal_eligible"] += int(mapping.causal_eligible)
        status["recovered_event_candidate"] += int(mapping.recovered_event_candidate)
        status["core_event_observation_eligible"] += int(
            mapping.core_event_observation_eligible
        )
        status["semantic_mapping_strict"] += int(mapping.semantic_mapping_strict)
        status["directional_strict"] += int(mapping.directional_strict)
        status["reaction_observation_eligible"] += int(mapping.reaction_observation_eligible)
        status["trade_candidate_eligible"] += int(mapping.trade_candidate_eligible)
        status["directional_trade_candidate_eligible"] += int(
            mapping.directional_trade_candidate_eligible
        )
        status["proxy_family_abstained"] += int(mapping.proxy_family_abstained)
        status["proxy_abstained"] += int(mapping.proxy_abstained)
        status["family_watch_only"] += int(mapping.family_watch_only)
        status["watch_only_trade_violation"] += int(
            mapping.family_watch_only and mapping.trade_candidate_eligible
        )
        status["primary_proxy_selected"] += int(bool(mapping.primary_proxy_selected))
        status["content_type_unknown"] += int(mapping.content_type_status == "unknown")
        status["content_type_legacy_unvalidated"] += int(
            mapping.content_type_status == "legacy_rule_unvalidated"
        )
        status["content_type_strict_eligible"] += int(mapping.content_type_strict_eligible)
        status["unknown_content_strict_violation"] += int(
            mapping.content_type_status == "unknown" and mapping.semantic_mapping_strict
        )
        status["legacy_content_strict_violation"] += int(
            mapping.content_type_status == "legacy_rule_unvalidated"
            and mapping.semantic_mapping_strict
        )
        status["macro_speech_release_violation"] += int(
            mapping.decision_subject_type in {"official_speaker", "external_commentator"}
            and mapping.event_type in {"macro_data_release", "inflation_data_release"}
        )
        status["speaker_commentary_decision_violation"] += int(
            mapping.decision_subject_type in {"official_speaker", "external_commentator"}
            and mapping.event_type == "central_bank_decision"
        )
        status["conditional_recovered_event_observation_violation"] += int(
            mapping.recovery_action_status not in {
                "not_applicable", "executed", "officially_announced", "confirmed_release"
            }
            and (mapping.core_event_observation_eligible or mapping.reaction_observation_eligible)
        )
        status["taxonomy_expansion_requires_adjudication"] += int(
            mapping.taxonomy_expansion_requires_adjudication
        )
        status["taxonomy_expansion_strict_violation"] += int(
            mapping.taxonomy_expansion_requires_adjudication
            and not mapping.taxonomy_strict_eligible
            and mapping.semantic_mapping_strict
        )
        status["taxonomy_expansion_training_violation"] += int(
            mapping.taxonomy_expansion_requires_adjudication
            and not mapping.taxonomy_strict_eligible
            and mapping.strict_training_eligible
        )
        status["taxonomy_expansion_trade_violation"] += int(
            mapping.taxonomy_expansion_requires_adjudication
            and not mapping.taxonomy_strict_eligible
            and mapping.trade_candidate_eligible
        )
        status["taxonomy_rejected_observation_violation"] += int(
            mapping.taxonomy_expansion_status == "rejected"
            and mapping.reaction_observation_eligible
        )
        status["conditional_or_prospective_strict_violation"] += int(
            "conditional_or_prospective_action" in mapping.critical_conflicts
            and (mapping.semantic_mapping_strict or mapping.reaction_observation_eligible)
        )
        status["critical_conflict_reaction_observation_violation"] += int(
            bool(mapping.critical_conflicts) and mapping.reaction_observation_eligible
        )
        conflict_occurrences = len(mapping.critical_conflicts)
        status["critical_conflicts_detected"] += conflict_occurrences
        status["critical_conflicts_correctly_blocked"] += (
            conflict_occurrences
            if conflict_occurrences
            and not mapping.semantic_mapping_strict
            and not mapping.reaction_observation_eligible
            else 0
        )
        status["critical_conflicts_retained_in_strict"] += (
            conflict_occurrences if mapping.semantic_mapping_strict else 0
        )
        status["critical_conflicts_retained_in_observation"] += (
            conflict_occurrences if mapping.reaction_observation_eligible else 0
        )
        status["invalid_actor_relation"] += int(any(
            region.region_role == "actor"
            and region.relation_type not in {"subject_of_action", "institutional_actor"}
            for region in mapping.regions
        ))
        headline_lower = mapping.normalized_headline.lower()
        headline_flags = _headline_audit_flags(mapping.normalized_headline)
        status["prior_decision_current_violation"] += int(
            headline_flags["prior_cb_decision_reference"]
            and mapping.event_type == "central_bank_decision"
        )
        status["speaker_calendar_primary_violation"] += int(
            headline_flags["speaker_calendar_reference"]
            and mapping.event_type == "central_bank_calendar_preview"
        )
        status["speaker_numeric_macro_release_violation"] += int(
            headline_flags["speaker_numeric_macro_reference"]
            and mapping.event_type in {"macro_data_release", "inflation_data_release"}
        )
        status["japan_rate_decision_false_negative"] += int(
            "japan interest rate decision" in headline_lower
            and mapping.event_type != "central_bank_decision"
        )
        status["cyber_deploy_defense_channel_violation"] += int(
            "deploy" in headline_lower
            and any(token in headline_lower for token in ("malware", "software", "model"))
            and any(
                channel.channel_code in {"defense_demand", "risk_off"}
                for channel in mapping.channels
            )
        )

        new_values = _v5_field_values(mapping)
        adjudications.append({
            "review_row_id": row_id,
            "field_name": "news_content_type",
            "proposed_value": new_values["news_content_type"],
            "reviewer_a_value": "",
            "reviewer_b_value": "",
            "final_adjudicated_value": "",
            "disagreement_type": "new_v5_field_requires_independent_adjudication",
            "adjudication_reason": "news_content_type has no equivalent frozen v4 correctness field",
            "rule_candidate": RULE_CANDIDATES["news_content_type"],
            "adjudication_version": "s05p4-v5.4.1-set-a-adjudication-candidates-v5",
            "v4_value": "",
            "v5_confidence": 0.0,
            "v5_strict_subset": 0,
            "v5_abstain_reason": "",
        })
        for field, (old_column, correctness_column) in FIELD_SPECS.items():
            old_value = source[old_column]
            new_value = new_values[field]
            a_value = reviewer_a[row_id][correctness_column]
            b_value = reviewer_b[row_id][correctness_column]
            unchanged = bool(new_value) and old_value == new_value
            strict = _field_is_strict(field, mapping, thresholds)
            if unchanged:
                if a_value == b_value == "1":
                    disagreement_type = "consensus_correct"
                    final_value = new_value
                    field_stats[field]["consensus_correct"] += 1
                elif a_value == b_value == "0":
                    disagreement_type = "consensus_incorrect_target_unknown"
                    final_value = ""
                    field_stats[field]["consensus_incorrect"] += 1
                else:
                    disagreement_type = "reviewer_disagreement"
                    final_value = ""
                    field_stats[field]["reviewer_disagreement"] += 1
                field_stats[field]["unchanged_evaluable"] += 1
                if strict:
                    field_stats[field]["strict_unchanged_evaluable"] += 1
                    field_stats[field]["strict_reviewer_a_correct"] += int(a_value)
                    field_stats[field]["strict_reviewer_b_correct"] += int(b_value)
                confidence_bin = _decile(_confidence_for_field(field, mapping))
                for reviewer_name, value in (("reviewer_a", a_value), ("reviewer_b", b_value)):
                    calibration[field][reviewer_name][confidence_bin]["rows"] += 1
                    calibration[field][reviewer_name][confidence_bin]["correct"] += int(value)
            else:
                disagreement_type = "v5_target_changed_requires_human_adjudication"
                final_value = ""
                field_stats[field]["target_changed_unresolved"] += 1

            adjudications.append({
                "review_row_id": row_id,
                "field_name": field,
                "proposed_value": new_value,
                "reviewer_a_value": a_value,
                "reviewer_b_value": b_value,
                "final_adjudicated_value": final_value,
                "disagreement_type": disagreement_type,
                "adjudication_reason": (
                    "reviewers scored the same frozen v4 value; v5 value is unchanged"
                    if unchanged
                    else "v4 correctness booleans cannot validate a changed v5 target value"
                ),
                "rule_candidate": RULE_CANDIDATES[field],
                "adjudication_version": "s05p4-v5.4.1-set-a-adjudication-candidates-v5",
                "v4_value": old_value,
                "v5_confidence": _confidence_for_field(field, mapping),
                "v5_strict_subset": int(strict),
                "v5_abstain_reason": (
                    mapping.proxy_family_abstain_reason if field == "proxy_family"
                    else mapping.proxy_abstain_reason if field == "primary_proxy"
                    else "event_type_unknown" if field == "event_type" and new_value == "unknown"
                    else "event_direction_unclear" if field == "direction" and new_value == "unclear"
                    else "no_v5_target" if not new_value else ""
                ),
            })

    predicted = [int(mapping.causal_eligible) for _, mapping in mappings]
    actual_a = [int(reviewer_a[row["review_row_id"]]["human_causal_eligible"]) for row, _ in mappings]
    actual_b = [int(reviewer_b[row["review_row_id"]]["human_causal_eligible"]) for row, _ in mappings]
    consensus_pairs = [
        (prediction, a)
        for prediction, a, b in zip(predicted, actual_a, actual_b)
        if a == b
    ]
    causal = {
        "reviewer_a": _confusion(predicted, actual_a),
        "reviewer_b": _confusion(predicted, actual_b),
        "reviewer_consensus_only": _confusion(
            [pair[0] for pair in consensus_pairs],
            [pair[1] for pair in consensus_pairs],
        ),
        "reviewer_disagreement_rows": sum(a != b for a, b in zip(actual_a, actual_b)),
        "consensus_rows": len(consensus_pairs),
    }

    field_summary: dict[str, Any] = {}
    for field, counts in field_stats.items():
        strict_rows = counts["strict_unchanged_evaluable"]
        field_summary[field] = {
            **dict(sorted(counts.items())),
            "strict_unchanged_reviewer_a_precision": (
                counts["strict_reviewer_a_correct"] / strict_rows if strict_rows else None
            ),
            "strict_unchanged_reviewer_b_precision": (
                counts["strict_reviewer_b_correct"] / strict_rows if strict_rows else None
            ),
            "production_interpretation": (
                "NOT_EVALUABLE_CHANGED_TARGETS_REQUIRE_VALUE_ADJUDICATION"
                if counts["target_changed_unresolved"]
                else "LIMITED_TO_UNCHANGED_V4_TARGETS"
            ),
        }

    calibration_report: dict[str, Any] = {}
    for field, reviewers in calibration.items():
        calibration_report[field] = {}
        for reviewer_name, bins in reviewers.items():
            calibration_report[field][reviewer_name] = {
                key: {
                    "rows": values["rows"],
                    "correct": values["correct"],
                    "precision": values["correct"] / values["rows"] if values["rows"] else None,
                }
                for key, values in sorted(bins.items())
            }

    unresolved = sum(
        1 for row in adjudications if not row["final_adjudicated_value"]
    )
    critical_conflicts_retained = (
        status["critical_conflicts_retained_in_strict"]
        + status["critical_conflicts_retained_in_observation"]
    )
    target_policy_evaluation = {
        "tier_a_precision": {
            "target": policy_config.production_targets.tier_a_precision,
            "status": "NOT_EVALUABLE_NO_PIT_PROXY_OR_DIRECTION_CALIBRATION",
        },
        "critical_region_channel_errors": {
            "target": policy_config.production_targets.critical_region_channel_errors,
            "critical_conflicts_detected": status["critical_conflicts_detected"],
            "critical_conflicts_correctly_blocked": status["critical_conflicts_correctly_blocked"],
            "critical_conflicts_retained_in_strict": status["critical_conflicts_retained_in_strict"],
            "critical_conflicts_retained_in_observation": status["critical_conflicts_retained_in_observation"],
            "observed_retained_errors": critical_conflicts_retained,
            "status": "PASS_STRUCTURAL_SAMPLE" if critical_conflicts_retained == 0 else "FAIL",
        },
        "primary_proxy_precision": {
            "target": policy_config.production_targets.primary_proxy_precision,
            "status": "NOT_EVALUABLE_PRIMARY_PROXY_ABSTAINED_WITHOUT_PIT_METRICS",
        },
        "high_confidence_direction_precision": {
            "target": policy_config.production_targets.high_confidence_direction_precision,
            "status": "NOT_EVALUABLE_NO_CALIBRATION_SNAPSHOT",
        },
        "watch_only_trade_violations": {
            "observed": status["watch_only_trade_violation"],
            "status": "PASS" if status["watch_only_trade_violation"] == 0 else "FAIL",
        },
    }
    detailed_rows = [
        {
            "review_row_id": source["review_row_id"],
            "provider": source["provider"],
            "normalized_headline": source["normalized_headline"],
            "causal_eligible": mapping.causal_eligible,
            "causal_exclusion_reason": mapping.causal_exclusion_reason,
            "recovered_event_candidate": mapping.recovered_event_candidate,
            "recovered_event_clause": mapping.recovered_event_clause,
            "framing_clause": mapping.framing_clause,
            "recovery_action_status": mapping.recovery_action_status,
            "core_event_observation_eligible": mapping.core_event_observation_eligible,
            "news_content_type": mapping.news_content_type,
            "content_type_status": mapping.content_type_status,
            "content_type_strict_eligible": mapping.content_type_strict_eligible,
            "content_type_adjudication_id": mapping.content_type_adjudication_id,
            "event_type": mapping.event_type,
            "referenced_event_types": list(mapping.referenced_event_types),
            "decision_subject_type": mapping.decision_subject_type,
            "decision_temporality": mapping.decision_temporality,
            "taxonomy_expansion_requires_adjudication": mapping.taxonomy_expansion_requires_adjudication,
            "taxonomy_expansion_status": mapping.taxonomy_expansion_status,
            "taxonomy_adjudication_id": mapping.taxonomy_adjudication_id,
            "taxonomy_strict_eligible": mapping.taxonomy_strict_eligible,
            "sample_purpose": mapping.sample_purpose,
            "strict_training_eligible": mapping.strict_training_eligible,
            "region_scope": mapping.region_scope,
            "region_confidence": mapping.region_confidence,
            "regions": [row.__dict__ for row in mapping.regions],
            "channel_confidence": mapping.channel_confidence,
            "channels": [row.__dict__ for row in mapping.channels],
            "event_direction": mapping.event_direction,
            "event_direction_confidence": mapping.event_direction_confidence,
            "event_direction_rule_strength": mapping.event_direction_rule_strength,
            "event_direction_empirical_precision": mapping.event_direction_empirical_precision,
            "direction_calibration_snapshot_id": mapping.direction_calibration_snapshot_id,
            "proxy_family": mapping.proxy_family,
            "proxy_family_confidence": mapping.proxy_family_confidence,
            "primary_proxy_selected": mapping.primary_proxy_selected,
            "primary_proxy_confidence": mapping.primary_proxy_confidence,
            "top1_top2_score_gap": mapping.top1_top2_score_gap,
            "proxy_abstained": mapping.proxy_abstained,
            "proxy_abstain_reason": mapping.proxy_abstain_reason,
            "family_watch_only": mapping.family_watch_only,
            "critical_conflicts": list(mapping.critical_conflicts),
            "semantic_mapping_strict": mapping.semantic_mapping_strict,
            "directional_strict": mapping.directional_strict,
            "reaction_observation_eligible": mapping.reaction_observation_eligible,
            "trade_candidate_eligible": mapping.trade_candidate_eligible,
            "mapping_tier": mapping.mapping_tier,
            "pre_event_feature_snapshot_id": mapping.pre_event_feature_snapshot_id,
            "pre_event_feature_snapshot_hash": mapping.pre_event_feature_snapshot_hash,
            "learned_direction_snapshot_id": mapping.learned_direction_snapshot_id,
            "learned_direction_snapshot_hash": mapping.learned_direction_snapshot_hash,
            "external_gate_state_hash": mapping.external_gate_state_hash,
        }
        for source, mapping in mappings
    ]

    report = {
        "version": "s05p4-v5.4.1-set-a-research-audit-v5",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "semantic_version": S05P4_V5_VERSION,
        "scope": "set_a_only_research_v5_4_1_structural_audit",
        "rows": len(input_rows),
        "authoritative_independent_human_review": False,
        "reviewer_results_are_secondary_ai_reviews": True,
        "set_b_accessed": False,
        "market_outcomes_accessed": False,
        "pre_event_market_metrics_supplied": False,
        "learned_reaction_direction_supplied": False,
        "persistence_contract": {
            "semantic_run": "semantic_input_hash+threshold_config_hash",
            "proxy_ranking_run": "semantic_run_key+pre_event_feature_snapshot_id+snapshot_hash",
            "policy_evaluation_run": "semantic+ranking+learned_direction_hash+external_gate_hash+policy_hash",
            "immutable_created_at": True,
            "business_content_hash_required": True,
            "foreign_keys_required": True,
        },
        "point_in_time_proxy_contract": {
            "missing_metrics_action": "candidate_basket_only_primary_proxy_abstain",
            "snapshot_id_required": True,
            "post_event_metrics_forbidden": True,
        },
        "direction_confidence_contract": {
            "rule_strength_is_probability": False,
            "empirical_precision_required_for_calibration": True,
            "calibration_snapshot_required": True,
            "learned_direction_snapshot_required": True,
        },
        "thresholds": thresholds.__dict__,
        "production_targets": policy_config.production_targets.__dict__,
        "production_target_evaluation": target_policy_evaluation,
        "small_sample_families_watch_only": sorted(
            policy_config.small_sample_families_watch_only
        ),
        "safety_policy": policy_config.safety.__dict__,
        "status_counts": dict(sorted(status.items())),
        "mapping_tier_counts": dict(sorted(tiers.items())),
        "proxy_family_counts": dict(sorted(families.items())),
        "causal_exclusion_reason_counts": dict(sorted(exclusions.items())),
        "critical_conflict_counts": dict(sorted(conflicts.items())),
        "content_type_status_counts": dict(sorted(content_statuses.items())),
        "recovery_action_status_counts": dict(sorted(recovery_statuses.items())),
        "critical_conflict_handling": {
            "detected": status["critical_conflicts_detected"],
            "correctly_blocked": status["critical_conflicts_correctly_blocked"],
            "retained_in_strict": status["critical_conflicts_retained_in_strict"],
            "retained_in_observation": status["critical_conflicts_retained_in_observation"],
        },
        "causal_comparison": causal,
        "field_comparison": field_summary,
        "new_field_adjudication": {
            "news_content_type_rows": len(input_rows),
            "status": "REQUIRES_INDEPENDENT_TARGET_VALUE_REVIEW",
        },
        "confidence_calibration_on_unchanged_targets_only": calibration_report,
        "adjudication_rows": len(adjudications),
        "unresolved_adjudication_rows": unresolved,
        "production_gates": {
            "region_channel_precision": "NOT_EVALUABLE_CHANGED_TARGETS_REQUIRE_VALUE_ADJUDICATION",
            "proxy_family_precision": "NOT_EVALUABLE_CHANGED_TARGETS_REQUIRE_VALUE_ADJUDICATION",
            "primary_proxy_precision": "NOT_EVALUABLE_PRE_EVENT_METRICS_AND_VALUE_ADJUDICATION_REQUIRED",
            "direction_precision": "NOT_EVALUABLE_REACTION_LEARNER_AND_VALUE_ADJUDICATION_REQUIRED",
            "small_family_independent_sample": "NOT_EVALUATED",
            "full_clone_pit_idempotency": "NOT_RUN",
            "policy_config_enforced": True,
            "missing_pre_event_metrics_forces_proxy_abstention": True,
            "uncalibrated_direction_forces_directional_abstention": True,
            "unknown_unadjudicated_content_forces_strict_abstention": (
                status["unknown_content_strict_violation"] == 0
            ),
            "legacy_unvalidated_content_forces_strict_abstention": (
                status["legacy_content_strict_violation"] == 0
            ),
            "fed_speech_misclassified_as_macro_release_zero": (
                status["macro_speech_release_violation"] == 0
            ),
            "speaker_or_external_commentary_classified_as_cb_decision_zero": (
                status["speaker_commentary_decision_violation"] == 0
            ),
            "conditional_recovered_event_promoted_to_observation_zero": (
                status["conditional_recovered_event_observation_violation"] == 0
            ),
            "conditional_or_prospective_action_blocks_strict_and_observation": (
                status["conditional_or_prospective_strict_violation"] == 0
            ),
            "critical_conflict_blocks_reaction_observation": (
                status["critical_conflict_reaction_observation_violation"] == 0
            ),
            "actor_rows_have_explicit_valid_relation": (
                status["invalid_actor_relation"] == 0
            ),
            "japan_rate_decision_false_negative_zero": (
                status["japan_rate_decision_false_negative"] == 0
            ),
            "cyber_deploy_defense_channel_violation_zero": (
                status["cyber_deploy_defense_channel_violation"] == 0
            ),
        },
        "v5_4_1_freeze_gates": {
            "taxonomy_expansion_retained_in_strict_zero": (
                status["taxonomy_expansion_strict_violation"] == 0
            ),
            "taxonomy_expansion_retained_in_training_zero": (
                status["taxonomy_expansion_training_violation"] == 0
            ),
            "taxonomy_expansion_retained_in_trade_zero": (
                status["taxonomy_expansion_trade_violation"] == 0
            ),
            "taxonomy_rejected_reaction_observation_zero": (
                status["taxonomy_rejected_observation_violation"] == 0
            ),
            "prior_or_speaker_decision_current_zero": (
                status["prior_decision_current_violation"] == 0
                and status["speaker_commentary_decision_violation"] == 0
            ),
            "speaker_primary_calendar_preview_zero": (
                status["speaker_calendar_primary_violation"] == 0
            ),
            "speaker_numeric_reference_macro_release_zero": (
                status["speaker_numeric_macro_release_violation"] == 0
            ),
            "unknown_or_legacy_strict_zero": (
                status["unknown_content_strict_violation"] == 0
                and status["legacy_content_strict_violation"] == 0
            ),
            "conditional_recovery_observation_zero": (
                status["conditional_recovered_event_observation_violation"] == 0
            ),
        },
        "verdict": "HOLD_RESEARCH_V5_4_1_PENDING_FREEZE_GATE_EVIDENCE",
        "safety": {
            "set_b_released": False,
            "canonical_database_modified": False,
            "canonical_promotion": False,
            "m254_m255_m256": False,
            "orders": False,
            "transmit": False,
            "live_routing": False,
            "threshold_promotion": False,
        },
        "detailed_rows": detailed_rows,
    }
    return report, adjudications


def _markdown(report: Mapping[str, Any]) -> str:
    status = report["status_counts"]
    lines = [
        "# S05P4 v5.4.1 Set A structural research audit",
        "",
        f"- Verdict: **{report['verdict']}**",
        f"- Safe Set A rows: `{report['rows']}`",
        f"- Causal eligible: `{status.get('causal_eligible', 0)}`",
        f"- Recovered event candidates (not strict): `{status.get('recovered_event_candidate', 0)}`",
        f"- Semantic strict: `{status.get('semantic_mapping_strict', 0)}`",
        f"- Reaction observation eligible: `{status.get('reaction_observation_eligible', 0)}`",
        f"- Family abstained: `{status.get('proxy_family_abstained', 0)}`",
        f"- Proxy abstained: `{status.get('proxy_abstained', 0)}`",
        f"- Directional strict without learned confirmation: `{status.get('directional_strict', 0)}`",
        f"- Trade candidates without external gates: `{status.get('trade_candidate_eligible', 0)}`",
        f"- Unknown/unadjudicated content strict violations: `{status.get('unknown_content_strict_violation', 0)}`",
        f"- Legacy-unvalidated content strict violations: `{status.get('legacy_content_strict_violation', 0)}`",
        f"- Fed-speaker macro-release violations: `{status.get('macro_speech_release_violation', 0)}`",
        f"- Speaker/external-commentator CB-decision violations: `{status.get('speaker_commentary_decision_violation', 0)}`",
        f"- Conditional recovered-event observation violations: `{status.get('conditional_recovered_event_observation_violation', 0)}`",
        f"- Taxonomy expansions requiring adjudication: `{status.get('taxonomy_expansion_requires_adjudication', 0)}`",
        f"- Taxonomy expansion strict violations: `{status.get('taxonomy_expansion_strict_violation', 0)}`",
        f"- Taxonomy expansion training violations: `{status.get('taxonomy_expansion_training_violation', 0)}`",
        f"- Taxonomy expansion trade violations: `{status.get('taxonomy_expansion_trade_violation', 0)}`",
        f"- Rejected taxonomy reaction-observation violations: `{status.get('taxonomy_rejected_observation_violation', 0)}`",
        f"- Headline-audited prior decisions classified current: `{status.get('prior_decision_current_violation', 0)}`",
        f"- Headline-audited speaker/calendar rows classified as primary preview: `{status.get('speaker_calendar_primary_violation', 0)}`",
        f"- Headline-audited speaker numeric references classified as macro releases: `{status.get('speaker_numeric_macro_release_violation', 0)}`",
        f"- Conditional/prospective strict or observation violations: `{status.get('conditional_or_prospective_strict_violation', 0)}`",
        f"- Critical-conflict reaction-observation violations: `{status.get('critical_conflict_reaction_observation_violation', 0)}`",
        f"- Invalid actor-relation rows: `{status.get('invalid_actor_relation', 0)}`",
        f"- Japan rate-decision false negatives: `{status.get('japan_rate_decision_false_negative', 0)}`",
        f"- Cyber deploy defense-channel violations: `{status.get('cyber_deploy_defense_channel_violation', 0)}`",
        f"- Critical conflicts detected: `{status.get('critical_conflicts_detected', 0)}`",
        f"- Critical conflicts correctly blocked: `{status.get('critical_conflicts_correctly_blocked', 0)}`",
        f"- Critical conflicts retained in strict: `{status.get('critical_conflicts_retained_in_strict', 0)}`",
        f"- Critical conflicts retained in observation: `{status.get('critical_conflicts_retained_in_observation', 0)}`",
        "",
        "This is a research-only structural audit. The two completed reviews are secondary AI reviews, not an authoritative independent human adjudication.",
        "Direction rule strength is stored separately from empirical precision and calibrated confidence. No direction calibration snapshot was supplied, so directional eligibility remains zero.",
        "No PIT pre-event feature snapshot was supplied, so candidate baskets may be generated but every primary proxy selection must abstain.",
        f"Semantic rule freeze status: `{report.get('semantic_rule_freeze_status', 'NOT_EVALUATED')}`.",
        "",
        "## Causal comparison",
        "",
        "| Label source | TP | FP | FN | TN | Precision | Recall |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for label, key in (
        ("Reviewer A", "reviewer_a"),
        ("Reviewer B", "reviewer_b"),
        ("A/B consensus rows only", "reviewer_consensus_only"),
    ):
        row = report["causal_comparison"][key]
        precision = "n/a" if row["precision"] is None else f"{row['precision']:.2%}"
        recall = "n/a" if row["recall"] is None else f"{row['recall']:.2%}"
        lines.append(
            f"| {label} | {row['TP']} | {row['FP']} | {row['FN']} | {row['TN']} | {precision} | {recall} |"
        )

    lines.extend([
        "",
        "## Coverage and abstention",
        "",
        "| Tier | Count |",
        "|---|---:|",
    ])
    for tier, count in report["mapping_tier_counts"].items():
        lines.append(f"| {tier} | {count} |")

    lines.extend([
        "",
        "## Field comparison limits",
        "",
        "The frozen reviews contain correctness booleans for v4 values, not corrected target values. Therefore a changed v5 region/channel/family/proxy cannot be declared correct from those files. Such rows remain unresolved adjudication candidates.",
        f"The independent news_content_type field has no frozen v4 equivalent; all `{report['new_field_adjudication']['news_content_type_rows']}` rows require separate target-value review.",
        "",
        "| Field | Unchanged/evaluable | Changed/unresolved | Strict unchanged | Reviewer A precision | Reviewer B precision |",
        "|---|---:|---:|---:|---:|---:|",
    ])
    for field, row in report["field_comparison"].items():
        a = row["strict_unchanged_reviewer_a_precision"]
        b = row["strict_unchanged_reviewer_b_precision"]
        lines.append(
            f"| {field} | {row.get('unchanged_evaluable', 0)} | {row.get('target_changed_unresolved', 0)} | {row.get('strict_unchanged_evaluable', 0)} | "
            f"{'n/a' if a is None else f'{a:.2%}'} | {'n/a' if b is None else f'{b:.2%}'} |"
        )

    lines.extend([
        "",
        "## Focused review packets",
        "",
        f"- Non-empty target changes: `{report['review_packet_counts']['target_change']}`",
        f"- Abstentions requiring an abstention decision: `{report['review_packet_counts']['abstention']}`",
        f"- Multi-role region/channel rows: `{report['review_packet_counts']['multi_role_region_channel']}`",
        f"- Recovered event candidates requiring review: `{report['review_packet_counts']['recovered_event_candidate']}`",
        f"- News-content-type rows requiring review: `{report['review_packet_counts']['news_content_type']}`",
        "",
        "## Test execution evidence",
        "",
        f"- Status: `{report.get('pytest', {}).get('status', 'NOT_PROVIDED')}`",
        f"- Collected: `{report.get('pytest', {}).get('collected', 0)}`",
        f"- Passed: `{report.get('pytest', {}).get('passed', 0)}`",
        f"- Failed: `{report.get('pytest', {}).get('failed', 0)}`",
        f"- Errors: `{report.get('pytest', {}).get('errors', 0)}`",
        f"- Skipped: `{report.get('pytest', {}).get('skipped', 0)}`",
        "",
        "## Production decision",
        "",
        "Production PASS is not asserted. Human target-value adjudication, pre-event tradability metrics, learned reaction-direction confirmation, family sample sufficiency, and a clone PIT/idempotency run are still required.",
        "",
        "Set B was not accessed. Canonical DB/promotion, M254/M255/M256, broker orders, transmit, live routing, and threshold promotion remained disabled.",
        "",
    ])
    return "\n".join(lines)


def _write_adjudications(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0]) if rows else [
        "review_row_id",
        "field_name",
        "proposed_value",
        "reviewer_a_value",
        "reviewer_b_value",
        "final_adjudicated_value",
        "disagreement_type",
        "adjudication_reason",
        "rule_candidate",
        "adjudication_version",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _write_rows(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _write_freeze_manifest(
    json_path: Path,
    markdown_path: Path,
    *,
    report: Mapping[str, Any],
    files: Mapping[str, Path],
) -> None:
    manifest_files = {
        label: {
            "path": str(path.resolve()),
            "bytes": path.stat().st_size,
            "sha256": _sha256(path),
        }
        for label, path in files.items()
    }
    payload = {
        "version": "s05p4-v5.4.1-semantic-rule-freeze-v1",
        "frozen_at_utc": report["generated_at_utc"],
        "semantic_version": report["semantic_version"],
        "freeze_status": report["semantic_rule_freeze_status"],
        "verdict": report["verdict"],
        "set_a_rows": report["rows"],
        "freeze_gates": report["v5_4_1_freeze_gates"],
        "safety": report["safety"],
        "files": manifest_files,
    }
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    lines = [
        "# S05P4 v5.4.1 semantic-rule freeze manifest",
        "",
        f"- Freeze status: **{payload['freeze_status']}**",
        f"- Verdict: **{payload['verdict']}**",
        f"- Set A rows: `{payload['set_a_rows']}`",
        "- Set B/canonical/M254+/broker paths: `untouched and disabled`",
        "",
        "## Frozen files",
        "",
        "| Label | Bytes | SHA-256 | Path |",
        "|---|---:|---|---|",
    ]
    for label, row in manifest_files.items():
        lines.append(f"| {label} | {row['bytes']} | `{row['sha256']}` | `{row['path']}` |")
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _build_review_packets(
    report: Mapping[str, Any],
    adjudications: list[dict[str, Any]],
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    details = {row["review_row_id"]: row for row in report["detailed_rows"]}
    target_changes: list[dict[str, Any]] = []
    abstentions: list[dict[str, Any]] = []
    for row in adjudications:
        detail = details[row["review_row_id"]]
        base = {
            "review_row_id": row["review_row_id"],
            "normalized_headline": detail["normalized_headline"],
            "provider": detail["provider"],
            "field_name": row["field_name"],
            "v4_value": row["v4_value"],
        }
        proposed_is_abstention = not row["proposed_value"] or (
            row["field_name"] in {"event_type", "direction"}
            and row["proposed_value"] in {"unknown", "unclear"}
        )
        if row["v4_value"] and not proposed_is_abstention and row["v4_value"] != row["proposed_value"]:
            target_changes.append({
                **base,
                "proposed_v5_value": row["proposed_value"],
                "v5_confidence": row["v5_confidence"],
                "human_v5_target_correct": "",
                "human_corrected_target": "",
                "reviewer_id": "",
                "reviewed_at": "",
            })
        elif row["v4_value"] and proposed_is_abstention:
            abstentions.append({
                **base,
                "v5_abstain_reason": row["v5_abstain_reason"],
                "human_should_abstain": "",
                "human_required_mapping_level": "",
                "human_corrected_target": "",
                "reviewer_id": "",
                "reviewed_at": "",
            })

    multi_role: list[dict[str, Any]] = []
    recovered_event_groups: dict[tuple[str, str], dict[str, Any]] = {}
    news_content_types: list[dict[str, Any]] = []
    for detail in report["detailed_rows"]:
        news_content_types.append({
            "review_row_id": detail["review_row_id"],
            "normalized_headline": detail["normalized_headline"],
            "provider": detail["provider"],
            "proposed_news_content_type_v5": detail["news_content_type"],
            "human_news_content_type_correct": "",
            "human_corrected_news_content_type": "",
            "reviewer_confidence": "",
            "reviewer_id": "",
            "reviewed_at": "",
        })
        if detail["recovered_event_candidate"]:
            key = (detail["normalized_headline"], detail["recovered_event_clause"])
            group = recovered_event_groups.setdefault(key, {
                "review_row_id": detail["review_row_id"],
                "source_review_row_ids": [],
                "providers": [],
                "normalized_headline": detail["normalized_headline"],
                "framing_clause_v5": detail["framing_clause"],
                "recovered_event_clause_v5": detail["recovered_event_clause"],
                "recovery_action_status_v5": detail["recovery_action_status"],
            })
            group["source_review_row_ids"].append(detail["review_row_id"])
            group["providers"].append(detail["provider"])
        if not detail["regions"] and not detail["channels"]:
            continue
        multi_role.append({
            "review_row_id": detail["review_row_id"],
            "normalized_headline": detail["normalized_headline"],
            "provider": detail["provider"],
            "news_content_type_v5": detail["news_content_type"],
            "event_type_v5": detail["event_type"],
            "region_scope_v5": detail["region_scope"],
            "region_roles_json_v5": json.dumps(detail["regions"], ensure_ascii=False, sort_keys=True),
            "channels_json_v5": json.dumps(detail["channels"], ensure_ascii=False, sort_keys=True),
            "human_actor_regions": "",
            "human_target_regions": "",
            "human_directly_affected_regions": "",
            "human_market_expression_regions": "",
            "human_primary_channel": "",
            "human_secondary_channels": "",
            "human_region_channel_correct": "",
            "reviewer_id": "",
            "reviewed_at": "",
        })
    recovered_events: list[dict[str, Any]] = []
    for _, group in sorted(recovered_event_groups.items()):
        source_ids = sorted(set(group.pop("source_review_row_ids")))
        providers = sorted(set(group.pop("providers")))
        recovered_events.append({
            **group,
            "source_review_row_ids_json": json.dumps(source_ids, ensure_ascii=False),
            "source_row_count": len(source_ids),
            "provider": "|".join(providers),
            "human_recovery_correct": "",
            "human_causal_eligible_after_clause_split": "",
            "human_corrected_event_clause": "",
            "reviewer_id": "",
            "reviewed_at": "",
        })
    return target_changes, abstentions, multi_role, recovered_events, news_content_types


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit research-only S05P4 v5.4.1 on unsealed Set A")
    parser.add_argument(
        "--input",
        type=Path,
        default=Path(
            "outputs/s05p4_v4_secondary_review_packet/"
            "NON_INDEPENDENT_AI_HUMAN_STYLE_REVIEW/human_review_input_A_v4_secondary.csv"
        ),
    )
    parser.add_argument(
        "--reviewer-a",
        type=Path,
        default=Path(r"C:\Users\USER\Downloads\human_review_output_A_v4_secondary_completed.csv"),
    )
    parser.add_argument(
        "--reviewer-b",
        type=Path,
        default=Path(
            "outputs/s05p4_v4_secondary_review_packet/SECONDARY_AI_REVIEWER_B/"
            "human_review_output_A_v4_secondary_completed_independent.csv"
        ),
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config/s05p4_semantic_v5_4_1_research.json"),
    )
    parser.add_argument(
        "--json-output",
        type=Path,
        default=Path("artifacts/audit/s05p4_v5_4_1_set_a_research_audit.json"),
    )
    parser.add_argument(
        "--markdown-output",
        type=Path,
        default=Path("artifacts/audit/s05p4_v5_4_1_set_a_research_audit.md"),
    )
    parser.add_argument(
        "--adjudication-output",
        type=Path,
        default=Path("artifacts/audit/s05p4_v5_4_1_semantic_adjudications.csv"),
    )
    parser.add_argument(
        "--target-change-output",
        type=Path,
        default=Path("artifacts/audit/s05p4_v5_4_1_target_change_review_packet.csv"),
    )
    parser.add_argument(
        "--abstention-output",
        type=Path,
        default=Path("artifacts/audit/s05p4_v5_4_1_abstention_review_packet.csv"),
    )
    parser.add_argument(
        "--multi-role-output",
        type=Path,
        default=Path("artifacts/audit/s05p4_v5_4_1_multi_role_region_channel_review_packet.csv"),
    )
    parser.add_argument(
        "--recovered-event-output",
        type=Path,
        default=Path("artifacts/audit/s05p4_v5_4_1_recovered_event_review_packet.csv"),
    )
    parser.add_argument(
        "--news-content-type-output",
        type=Path,
        default=Path("artifacts/audit/s05p4_v5_4_1_news_content_type_review_packet.csv"),
    )
    parser.add_argument(
        "--pytest-junit-xml",
        type=Path,
        default=Path("artifacts/audit/s05p4_v5_4_1_pytest.xml"),
    )
    parser.add_argument(
        "--freeze-manifest-output",
        type=Path,
        default=Path("artifacts/audit/s05p4_v5_4_1_freeze_manifest.json"),
    )
    parser.add_argument(
        "--freeze-manifest-markdown-output",
        type=Path,
        default=Path("artifacts/audit/s05p4_v5_4_1_freeze_manifest.md"),
    )
    args = parser.parse_args()

    input_rows, reviewer_a, reviewer_b = _validate_safe_packet(
        args.input, args.reviewer_a, args.reviewer_b
    )
    config_payload = json.loads(args.config.read_text(encoding="utf-8"))
    policy_config = V5PolicyConfig.from_config(config_payload)
    report, adjudications = build_report(
        input_rows,
        reviewer_a,
        reviewer_b,
        policy_config=policy_config,
    )
    repeat_report, repeat_adjudications = build_report(
        input_rows,
        reviewer_a,
        reviewer_b,
        policy_config=policy_config,
    )
    first_deterministic = dict(report)
    repeat_deterministic = dict(repeat_report)
    first_deterministic.pop("generated_at_utc", None)
    repeat_deterministic.pop("generated_at_utc", None)
    same_input_rerun_diff = int(
        first_deterministic != repeat_deterministic
        or adjudications != repeat_adjudications
    )
    if same_input_rerun_diff:
        raise RuntimeError("same-input v5.4.1 semantic audit rerun is not deterministic")
    report["same_input_rerun"] = {
        "diff_count": same_input_rerun_diff,
        "status": "PASS" if same_input_rerun_diff == 0 else "FAIL",
    }
    report["pytest"] = _pytest_evidence(args.pytest_junit_xml)
    report["production_gates"]["same_input_rerun_diff_zero"] = same_input_rerun_diff == 0
    report["production_gates"]["pytest_failure_zero"] = (
        report["pytest"].get("status") == "PASS"
        and report["pytest"].get("failed", 0) == 0
        and report["pytest"].get("errors", 0) == 0
    )
    report["production_gates"]["critical_conflict_retained_zero"] = (
        report["critical_conflict_handling"]["retained_in_strict"] == 0
        and report["critical_conflict_handling"]["retained_in_observation"] == 0
    )
    report["v5_4_1_freeze_gates"]["same_input_diff_zero"] = same_input_rerun_diff == 0
    report["v5_4_1_freeze_gates"]["pytest_failures_errors_zero"] = (
        report["pytest"].get("status") == "PASS"
        and report["pytest"].get("failed", 0) == 0
        and report["pytest"].get("errors", 0) == 0
    )
    freeze_ready = all(report["v5_4_1_freeze_gates"].values())
    report["semantic_rule_freeze_status"] = (
        "FROZEN_FOR_SET_A_INDEPENDENT_ADJUDICATION"
        if freeze_ready
        else "HOLD_RESEARCH_V5_4_1_FREEZE_GATES_FAILED"
    )
    report["verdict"] = (
        "FREEZE_V5_4_1_READY_FOR_SET_A_INDEPENDENT_ADJUDICATION_PRODUCTION_HOLD"
        if freeze_ready
        else "HOLD_RESEARCH_V5_4_1_REQUIRES_REPAIR"
    )
    (
        target_changes,
        abstentions,
        multi_role,
        recovered_events,
        news_content_types,
    ) = _build_review_packets(report, adjudications)
    report["review_packet_counts"] = {
        "target_change": len(target_changes),
        "abstention": len(abstentions),
        "multi_role_region_channel": len(multi_role),
        "recovered_event_candidate": len(recovered_events),
        "news_content_type": len(news_content_types),
    }
    report["inputs"] = {
        "safe_set_a": {"path": str(args.input.resolve()), "sha256": _sha256(args.input)},
        "secondary_ai_reviewer_a": {
            "path": str(args.reviewer_a.resolve()),
            "sha256": _sha256(args.reviewer_a),
        },
        "secondary_ai_reviewer_b": {
            "path": str(args.reviewer_b.resolve()),
            "sha256": _sha256(args.reviewer_b),
        },
        "research_config": {
            "path": str(args.config.resolve()),
            "sha256": _sha256(args.config),
        },
    }
    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_output.write_text(_markdown(report), encoding="utf-8")
    _write_adjudications(args.adjudication_output, adjudications)
    _write_rows(
        args.target_change_output,
        target_changes,
        [
            "review_row_id", "normalized_headline", "provider", "field_name",
            "v4_value", "proposed_v5_value", "v5_confidence",
            "human_v5_target_correct", "human_corrected_target", "reviewer_id", "reviewed_at",
        ],
    )
    _write_rows(
        args.abstention_output,
        abstentions,
        [
            "review_row_id", "normalized_headline", "provider", "field_name",
            "v4_value", "v5_abstain_reason", "human_should_abstain",
            "human_required_mapping_level", "human_corrected_target", "reviewer_id", "reviewed_at",
        ],
    )
    _write_rows(
        args.multi_role_output,
        multi_role,
        [
            "review_row_id", "normalized_headline", "provider",
            "news_content_type_v5", "event_type_v5", "region_scope_v5",
            "region_roles_json_v5", "channels_json_v5", "human_actor_regions",
            "human_target_regions", "human_directly_affected_regions",
            "human_market_expression_regions", "human_primary_channel",
            "human_secondary_channels", "human_region_channel_correct",
            "reviewer_id", "reviewed_at",
        ],
    )
    _write_rows(
        args.recovered_event_output,
        recovered_events,
        [
            "review_row_id", "source_review_row_ids_json", "source_row_count",
            "normalized_headline", "provider", "framing_clause_v5",
            "recovered_event_clause_v5", "recovery_action_status_v5",
            "human_recovery_correct", "human_causal_eligible_after_clause_split",
            "human_corrected_event_clause", "reviewer_id", "reviewed_at",
        ],
    )
    _write_rows(
        args.news_content_type_output,
        news_content_types,
        [
            "review_row_id", "normalized_headline", "provider",
            "proposed_news_content_type_v5", "human_news_content_type_correct",
            "human_corrected_news_content_type", "reviewer_confidence",
            "reviewer_id", "reviewed_at",
        ],
    )
    _write_freeze_manifest(
        args.freeze_manifest_output,
        args.freeze_manifest_markdown_output,
        report=report,
        files={
            "semantic_mapper": ROOT / "recorder/s05_proxy_semantic_v5.py",
            "set_a_audit": Path(__file__),
            "semantic_tests": ROOT / "tests/test_s05_proxy_semantic_v5.py",
            "research_config": args.config,
            "safe_set_a_input": args.input,
            "secondary_reviewer_a": args.reviewer_a,
            "secondary_reviewer_b": args.reviewer_b,
            "pytest_junit": args.pytest_junit_xml,
            "set_a_audit_json": args.json_output,
            "set_a_audit_markdown": args.markdown_output,
            "semantic_adjudications": args.adjudication_output,
            "target_change_packet": args.target_change_output,
            "abstention_packet": args.abstention_output,
            "multi_role_packet": args.multi_role_output,
            "recovered_event_packet": args.recovered_event_output,
            "news_content_type_packet": args.news_content_type_output,
        },
    )
    print(json.dumps({
        "verdict": report["verdict"],
        "rows": report["rows"],
        "status_counts": report["status_counts"],
        "mapping_tier_counts": report["mapping_tier_counts"],
        "causal_comparison": report["causal_comparison"],
        "adjudication_rows": report["adjudication_rows"],
        "unresolved_adjudication_rows": report["unresolved_adjudication_rows"],
        "same_input_rerun": report["same_input_rerun"],
        "safety": report["safety"],
    }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
