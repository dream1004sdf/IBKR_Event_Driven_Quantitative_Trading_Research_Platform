from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from recorder.s05_proxy_semantic_v4 import (  # noqa: E402
    PROXY_MAP_VERSION_V4,
    S05P4_V4_VERSION,
    classify_v4_causal_proxy_headline,
    infer_channel_v4,
    infer_primary_proxy_v4,
    infer_region_v4,
    match_proxy_rule_v4,
)
from recorder.s05_proxy_semantic_closeout import infer_content_type, infer_direction  # noqa: E402


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _mapping_status(old_correct: int, old_value: str, new_value: str) -> str:
    changed = old_value != new_value
    if old_correct:
        return "regression_candidate" if changed else "known_correct_preserved"
    return "correction_candidate" if changed else "known_error_unchanged"


def build_report(comparison: dict[str, Any]) -> dict[str, Any]:
    rows = list(comparison["adjudicated_rows"])
    adjudicated: list[dict[str, Any]] = []
    confusion: Counter[str] = Counter()
    exclusion_reasons: Counter[str] = Counter()
    provider_retained: Counter[str] = Counter()
    family_retained: Counter[str] = Counter()
    mapping: dict[str, Counter[str]] = {
        "proxy_family": Counter(),
        "primary_proxy": Counter(),
        "region": Counter(),
        "affected_channel": Counter(),
    }

    for row in rows:
        headline = str(row["normalized_headline"])
        provider = str(row["provider"])
        v3 = int(row["model_v3_decision"])
        human = int(row["human_causal_eligible"])
        rule = match_proxy_rule_v4(headline, provider=provider) if v3 else None
        v4 = int(v3 == 1 and rule is not None)
        confusion[{(1, 1): "TP", (1, 0): "FP", (0, 1): "FN", (0, 0): "TN"}[(v4, human)]] += 1

        old_family = str(row["proposed_proxy_family"])
        old_proxy = str(row["proposed_primary_proxy"])
        old_region = str(row["proposed_region"])
        old_channel = str(row["proposed_affected_channel"])
        old_content_type = str(row["proposed_content_type"])
        old_direction = str(row["proposed_direction"])
        causal_decision = classify_v4_causal_proxy_headline(
            headline,
            provider=provider,
            family=old_family,
        )
        if rule is not None:
            new_family = rule.family
            new_region = infer_region_v4(new_family, headline)
            new_channel = infer_channel_v4(new_family, headline)
            new_proxy = infer_primary_proxy_v4(new_family, new_region, new_channel)
            new_content_type = infer_content_type(new_family, headline)
            new_direction = infer_direction(new_family, headline)
            provider_retained[provider] += 1
            family_retained[new_family] += 1
        else:
            new_family = new_region = new_channel = new_proxy = ""
            new_content_type = new_direction = ""

        if v3 and not v4:
            exclusion_reasons[causal_decision.exclusion_reason or "no_v4_family_match"] += 1

        if v4:
            mapping["proxy_family"][_mapping_status(
                int(row["human_proxy_family_correct"]), old_family, new_family
            )] += 1
            mapping["primary_proxy"][_mapping_status(
                int(row["human_primary_proxy_correct"]), old_proxy, new_proxy
            )] += 1
            mapping["region"][_mapping_status(
                int(row["human_region_correct"]), old_region, new_region
            )] += 1
            mapping["affected_channel"][_mapping_status(
                int(row["human_affected_channel_correct"]), old_channel, new_channel
            )] += 1

        adjudicated.append({
            "review_row_id": row["review_row_id"],
            "provider": provider,
            "headline": headline,
            "human_causal_eligible": human,
            "v3_decision": v3,
            "v4_decision": v4,
            "v4_matched_rule": (
                causal_decision.matched_rule if v3 else "v3_rejected_monotonic_hold"
            ),
            "v4_exclusion_reason": "" if v4 else (
                causal_decision.exclusion_reason
                if v3 else "v3_rejected_monotonic_hold"
            ),
            "old_content_type": old_content_type,
            "v4_content_type": new_content_type,
            "old_direction": old_direction,
            "v4_direction": new_direction,
            "old_proxy_family": old_family,
            "v4_proxy_family": new_family,
            "old_primary_proxy": old_proxy,
            "v4_primary_proxy": new_proxy,
            "old_region": old_region,
            "v4_region": new_region,
            "old_affected_channel": old_channel,
            "v4_affected_channel": new_channel,
        })

    tp, fp, fn, tn = (int(confusion[name]) for name in ("TP", "FP", "FN", "TN"))
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    causal_pass = precision is not None and precision >= 0.95
    unresolved_mapping = sum(
        counts.get("correction_candidate", 0) + counts.get("regression_candidate", 0)
        + counts.get("known_error_unchanged", 0)
        for counts in mapping.values()
    )
    return {
        "version": "s05p4-v4-set-a-design-audit-v1",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "scope": "set_a_only_generalized_v4_design",
        "set_b_accessed": False,
        "authoritative_independent_review": False,
        "v4_semantic_version": S05P4_V4_VERSION,
        "v4_proxy_map_version": PROXY_MAP_VERSION_V4,
        "rows": len(rows),
        "confusion": {name: int(confusion[name]) for name in ("TP", "FP", "FN", "TN")},
        "causal_precision": precision,
        "causal_recall": recall,
        "causal_precision_gate_0_95": causal_pass,
        "v3_retained": sum(int(row["model_v3_decision"]) for row in rows),
        "v4_retained": tp + fp,
        "v3_to_v4_excluded": sum(
            int(row["model_v3_decision"] == 1 and item["v4_decision"] == 0)
            for row, item in zip(rows, adjudicated)
        ),
        "exclusion_reason_counts": dict(sorted(exclusion_reasons.items())),
        "retained_provider_counts": dict(sorted(provider_retained.items())),
        "retained_family_counts": dict(sorted(family_retained.items())),
        "mapping_status": {key: dict(sorted(value.items())) for key, value in mapping.items()},
        "mapping_target_adjudication_required": unresolved_mapping > 0,
        "verdict": (
            "PASS_CAUSAL_FILTER_HOLD_MAPPING_ADJUDICATION"
            if causal_pass else "FAIL_CAUSAL_FILTER"
        ),
        "safety": {
            "set_b_released": False,
            "canonical_promotion": False,
            "canonical_database_modified": False,
            "m254_m255_m256": False,
            "orders": False,
            "transmit": False,
            "live_routing": False,
            "threshold_promotion": False,
        },
        "adjudicated_rows": adjudicated,
    }


def markdown(report: dict[str, Any]) -> str:
    confusion = report["confusion"]
    lines = [
        "# S05P4 generalized v4 Set A design audit",
        "",
        f"- Verdict: **{report['verdict']}**",
        f"- Set A rows: `{report['rows']}`",
        f"- V3 retained: `{report['v3_retained']}`",
        f"- V4 retained: `{report['v4_retained']}`",
        f"- Confusion: `TP={confusion['TP']} FP={confusion['FP']} FN={confusion['FN']} TN={confusion['TN']}`",
        f"- Causal precision: `{report['causal_precision']:.4%}`",
        f"- Causal recall: `{report['causal_recall']:.4%}`",
        f"- Causal precision >=95%: **{'PASS' if report['causal_precision_gate_0_95'] else 'FAIL'}**",
        "",
        "## Generalized exclusions",
        "",
    ]
    lines.extend(f"- {key}: `{value}`" for key, value in report["exclusion_reason_counts"].items())
    lines.extend(["", "## Mapping status", ""])
    for field, counts in report["mapping_status"].items():
        lines.append(f"- {field}: `{json.dumps(counts, sort_keys=True)}`")
    lines.extend([
        "",
        "Changed family/region/channel/proxy values require targeted Set A adjudication because the frozen review recorded correctness booleans, not corrected target values.",
        "",
        "Set B was not accessed. Canonical promotion, M254/M255/M256, and all broker-order paths remained disabled.",
        "",
    ])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit generalized S05P4 v4 against unsealed Set A only")
    parser.add_argument(
        "--comparison",
        type=Path,
        default=Path("artifacts/audit/s05p4_set_a_ai_comparison.json"),
    )
    parser.add_argument(
        "--json-output",
        type=Path,
        default=Path("artifacts/audit/s05p4_v4_set_a_design_audit.json"),
    )
    parser.add_argument(
        "--markdown-output",
        type=Path,
        default=Path("artifacts/audit/s05p4_v4_set_a_design_audit.md"),
    )
    args = parser.parse_args()
    if "set_b" in str(args.comparison).lower() or "input_b" in str(args.comparison).lower():
        raise RuntimeError("Set B inputs are forbidden during v4 design")
    payload = json.loads(args.comparison.read_text(encoding="utf-8"))
    report = build_report(payload)
    report["input_comparison"] = str(args.comparison.resolve())
    report["input_comparison_sha256"] = sha256(args.comparison)
    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_output.write_text(markdown(report), encoding="utf-8")
    print(json.dumps({key: report[key] for key in (
        "verdict", "confusion", "causal_precision", "causal_recall",
        "v3_retained", "v4_retained", "mapping_status", "safety",
    )}, indent=2, ensure_ascii=False))
    return 0 if report["causal_precision_gate_0_95"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
