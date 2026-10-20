from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def distribution(rows: list[dict[str, str]], field: str) -> dict[str, int]:
    return dict(sorted(Counter(row[field] for row in rows).items()))


def correctness_by_field(
    rows: list[dict[str, str]],
    field_name: str,
    correctness_name: str,
) -> dict[str, dict[str, int]]:
    grouped: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        grouped[row[field_name]][row[correctness_name]] += 1
    return {
        key: dict(sorted(counts.items()))
        for key, counts in sorted(grouped.items())
    }


def pct(numerator: int, denominator: int) -> float:
    return round(100.0 * numerator / denominator, 2) if denominator else 0.0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Summarize the no-key v5.4.1 independent AI target adjudication"
    )
    parser.add_argument(
        "--packet-dir",
        type=Path,
        default=ROOT
        / "artifacts/review/s05p4_v5_4_1_independent_target_adjudication/reviewer_packet",
    )
    parser.add_argument(
        "--validation-json",
        type=Path,
        default=ROOT
        / "artifacts/audit/s05p4_v5_4_1_independent_adjudication_no_key_validation.json",
    )
    parser.add_argument(
        "--json-output",
        type=Path,
        default=ROOT
        / "artifacts/audit/s05p4_v5_4_1_independent_adjudication_closeout.json",
    )
    parser.add_argument(
        "--markdown-output",
        type=Path,
        default=ROOT
        / "artifacts/audit/s05p4_v5_4_1_independent_adjudication_closeout.md",
    )
    args = parser.parse_args()

    validation = json.loads(args.validation_json.read_text(encoding="utf-8"))
    if validation["status"] != "PASS":
        raise RuntimeError("no-key validation must pass before closeout summarization")
    if any(
        validation[key]
        for key in ("sealed_model_key_opened", "set_b_accessed", "comparison_run")
    ):
        raise RuntimeError("closeout summarization requires an untouched sealed boundary")

    target = read_csv(args.packet_dir / "target_change_completed.csv")
    content = read_csv(args.packet_dir / "news_content_type_completed.csv")
    multirole = read_csv(args.packet_dir / "multi_role_region_channel_completed.csv")
    abstention = read_csv(args.packet_dir / "abstention_completed.csv")
    recovered = read_csv(args.packet_dir / "recovered_event_completed.csv")

    target_dist = distribution(target, "human_v5_target_correct")
    content_dist = distribution(content, "human_news_content_type_correct")
    multirole_dist = distribution(multirole, "human_region_channel_correct")
    abstention_dist = distribution(abstention, "human_should_abstain")
    recovery_dist = distribution(recovered, "human_recovery_correct")
    recovery_causal_dist = distribution(
        recovered, "human_causal_eligible_after_clause_split"
    )

    report: dict[str, Any] = {
        "version": "s05p4-v5.4.1-independent-ai-target-adjudication-closeout-v1",
        "label": "NON_HUMAN_INDEPENDENT_AI_TARGET_ADJUDICATION",
        "verdict": (
            "AI_TARGET_ADJUDICATION_COMPLETE_NO_KEY_PASS_"
            "OFFICIAL_HUMAN_AND_APPLICATION_HOLD"
        ),
        "scope": "frozen Set A reviewer-safe target packets only",
        "no_key_validation": validation,
        "row_counts": {
            "target_change": len(target),
            "news_content_type": len(content),
            "multi_role_region_channel": len(multirole),
            "abstention": len(abstention),
            "recovered_event": len(recovered),
            "total_adjudication_rows": sum(
                map(len, (target, content, multirole, abstention, recovered))
            ),
        },
        "results": {
            "target_change": {
                "distribution": target_dist,
                "correct_pct": pct(target_dist.get("1", 0), len(target)),
                "by_field": correctness_by_field(
                    target, "field_name", "human_v5_target_correct"
                ),
            },
            "news_content_type": {
                "distribution": content_dist,
                "correct_pct": pct(content_dist.get("1", 0), len(content)),
                "confidence": distribution(content, "reviewer_confidence"),
            },
            "multi_role_region_channel": {
                "distribution": multirole_dist,
                "all_roles_and_channels_correct_pct": pct(
                    multirole_dist.get("1", 0), len(multirole)
                ),
            },
            "abstention": {
                "distribution": abstention_dist,
                "abstain_pct": pct(abstention_dist.get("1", 0), len(abstention)),
                "by_field": correctness_by_field(
                    abstention, "field_name", "human_should_abstain"
                ),
                "required_mapping_level": distribution(
                    abstention, "human_required_mapping_level"
                ),
            },
            "recovered_event": {
                "recovery_correct": recovery_dist,
                "causal_eligible_after_split": recovery_causal_dist,
            },
        },
        "boundaries": {
            "sealed_key_opened": False,
            "set_b_accessed": False,
            "prior_review_used": False,
            "market_outcomes_used": False,
            "database_results_applied": False,
            "taxonomy_status_applied": False,
            "full_range_clone_started": False,
            "canonical_promotion_started": False,
            "m254_m255_m256_started": False,
            "broker_or_live_actions": False,
        },
        "decision": {
            "official_human_review": "HOLD",
            "set_a_sealed_comparison": "NOT_RUN",
            "set_b": "SEALED",
            "adjudication_application": "HOLD",
            "semantic_rule_changes": "FROZEN_NOT_CHANGED",
            "production_and_downstream": "HOLD",
        },
    }

    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    lines = [
        "# S05P4 v5.4.1 independent AI target adjudication closeout",
        "",
        f"- Label: `{report['label']}`",
        f"- Verdict: **{report['verdict']}**",
        "- No-key validation: **PASS**",
        "- This is not an official independent human review.",
        "",
        "## Completion",
        "",
        "| Packet | Rows | Main result |",
        "|---|---:|---|",
        (
            f"| Target change | {len(target)} | {target_dist.get('1', 0)} correct / "
            f"{target_dist.get('0', 0)} corrected ({report['results']['target_change']['correct_pct']}%) |"
        ),
        (
            f"| News content type | {len(content)} | {content_dist.get('1', 0)} correct / "
            f"{content_dist.get('0', 0)} corrected ({report['results']['news_content_type']['correct_pct']}%) |"
        ),
        (
            f"| Multi-role region/channel | {len(multirole)} | "
            f"{multirole_dist.get('1', 0)} fully correct / {multirole_dist.get('0', 0)} corrected "
            f"({report['results']['multi_role_region_channel']['all_roles_and_channels_correct_pct']}% fully correct) |"
        ),
        (
            f"| Abstention | {len(abstention)} | {abstention_dist.get('1', 0)} abstain / "
            f"{abstention_dist.get('0', 0)} headline mapping ({report['results']['abstention']['abstain_pct']}% abstain) |"
        ),
        (
            f"| Recovered event | {len(recovered)} | {recovery_dist.get('1', 0)} recovery-correct; "
            f"{recovery_causal_dist.get('1', 0)} causal-eligible after split |"
        ),
        "",
        "## Boundary checks",
        "",
        "- Sealed key opened: `false`",
        "- Set B accessed: `false`",
        "- Prior review or market outcomes used: `false`",
        "- DB/taxonomy result applied: `false`",
        "- Full-range/canonical/M254+: `false`",
        "- Broker or live action: `false`",
        "",
        "## Decision",
        "",
        "The independent AI target adjudication and no-key validation are complete. "
        "The result is evidence for the next protocol decision, but it is not an official human adjudication. "
        "Set A sealed comparison was not run, Set B remains sealed, and no corrections were applied.",
    ]
    args.markdown_output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({
        "verdict": report["verdict"],
        "row_counts": report["row_counts"],
        "results": report["results"],
        "decision": report["decision"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
