from __future__ import annotations

import argparse
import csv
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any


PACKETS = {
    "target_change": {
        "input": "target_change_input.csv",
        "output": "target_change_completed.csv",
        "booleans": ("human_v5_target_correct",),
        "required": ("reviewer_id", "reviewed_at"),
    },
    "news_content_type": {
        "input": "news_content_type_input.csv",
        "output": "news_content_type_completed.csv",
        "booleans": ("human_news_content_type_correct",),
        "required": ("reviewer_confidence", "reviewer_id", "reviewed_at"),
    },
    "multi_role_region_channel": {
        "input": "multi_role_region_channel_input.csv",
        "output": "multi_role_region_channel_completed.csv",
        "booleans": ("human_region_channel_correct",),
        "required": (
            "human_actor_regions", "human_target_regions",
            "human_directly_affected_regions", "human_market_expression_regions",
            "human_secondary_channels", "reviewer_id", "reviewed_at",
        ),
    },
    "abstention": {
        "input": "abstention_input.csv",
        "output": "abstention_completed.csv",
        "booleans": ("human_should_abstain",),
        "required": ("human_required_mapping_level", "reviewer_id", "reviewed_at"),
    },
    "recovered_event": {
        "input": "recovered_event_input.csv",
        "output": "recovered_event_completed.csv",
        "booleans": (
            "human_recovery_correct", "human_causal_eligible_after_clause_split",
        ),
        "required": ("reviewer_id", "reviewed_at"),
    },
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"missing CSV header: {path}")
        return list(reader.fieldnames), list(reader)


def timezone_aware_iso(value: str) -> bool:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def validate_json_array(value: str) -> bool:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return False
    return isinstance(parsed, list) and all(isinstance(item, str) for item in parsed)


def validate_packet(
    packet_dir: Path,
    name: str,
    spec: dict[str, Any],
) -> dict[str, Any]:
    input_path = packet_dir / spec["input"]
    output_path = packet_dir / spec["output"]
    in_fields, inputs = read_csv(input_path)
    out_fields, outputs = read_csv(output_path)
    issues: dict[str, int] = {
        "header_mismatch": int(in_fields != out_fields),
        "row_count_mismatch": int(len(inputs) != len(outputs)),
        "review_row_id_or_order_mismatch": 0,
        "immutable_input_changed": 0,
        "invalid_boolean": 0,
        "missing_required": 0,
        "invalid_reviewed_at": 0,
        "invalid_confidence": 0,
        "invalid_correction_contract": 0,
        "invalid_json_array": 0,
    }
    if in_fields != out_fields or len(inputs) != len(outputs):
        return {
            "status": "FAIL",
            "input": str(input_path.resolve()),
            "output": str(output_path.resolve()),
            "rows": len(outputs),
            "issues": issues,
        }
    human_fields = {
        field for field in in_fields
        if field.startswith("human_") or field in {"reviewer_confidence", "reviewer_id", "reviewed_at"}
    }
    immutable_fields = [field for field in in_fields if field not in human_fields]
    for input_row, output_row in zip(inputs, outputs):
        if input_row["review_row_id"] != output_row["review_row_id"]:
            issues["review_row_id_or_order_mismatch"] += 1
        if any(input_row[field] != output_row[field] for field in immutable_fields):
            issues["immutable_input_changed"] += 1
        for field in spec["booleans"]:
            if output_row[field] not in {"0", "1"}:
                issues["invalid_boolean"] += 1
        for field in spec["required"]:
            if not output_row[field].strip():
                issues["missing_required"] += 1
        if not timezone_aware_iso(output_row["reviewed_at"]):
            issues["invalid_reviewed_at"] += 1

        if name == "target_change":
            correct = output_row["human_v5_target_correct"]
            correction = output_row["human_corrected_target"].strip()
            if (correct == "1" and correction) or (correct == "0" and not correction):
                issues["invalid_correction_contract"] += 1
        elif name == "news_content_type":
            correct = output_row["human_news_content_type_correct"]
            correction = output_row["human_corrected_news_content_type"].strip()
            if output_row["reviewer_confidence"] not in {"1", "2", "3", "4", "5"}:
                issues["invalid_confidence"] += 1
            if (correct == "1" and correction) or (correct == "0" and not correction):
                issues["invalid_correction_contract"] += 1
        elif name == "multi_role_region_channel":
            for field in (
                "human_actor_regions", "human_target_regions",
                "human_directly_affected_regions", "human_market_expression_regions",
                "human_secondary_channels",
            ):
                if not validate_json_array(output_row[field]):
                    issues["invalid_json_array"] += 1
        elif name == "abstention":
            should_abstain = output_row["human_should_abstain"]
            correction = output_row["human_corrected_target"].strip()
            if (should_abstain == "1" and correction) or (
                should_abstain == "0" and not correction
            ):
                issues["invalid_correction_contract"] += 1
        elif name == "recovered_event":
            correct = output_row["human_recovery_correct"]
            correction = output_row["human_corrected_event_clause"].strip()
            if (correct == "1" and correction) or (correct == "0" and not correction):
                issues["invalid_correction_contract"] += 1

    total_issues = sum(issues.values())
    return {
        "status": "PASS" if total_issues == 0 else "FAIL",
        "input": str(input_path.resolve()),
        "input_sha256": sha256_file(input_path),
        "output": str(output_path.resolve()),
        "output_sha256": sha256_file(output_path),
        "rows": len(outputs),
        "unique_review_row_ids": len({row["review_row_id"] for row in outputs}),
        "issues": issues,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="No-key validation for v5.4.1 Set A target adjudication")
    parser.add_argument(
        "--packet-dir",
        type=Path,
        default=Path(
            "artifacts/review/s05p4_v5_4_1_independent_target_adjudication/reviewer_packet"
        ),
    )
    parser.add_argument(
        "--json-output",
        type=Path,
        default=Path(
            "artifacts/audit/s05p4_v5_4_1_independent_adjudication_no_key_validation.json"
        ),
    )
    parser.add_argument(
        "--markdown-output",
        type=Path,
        default=Path(
            "artifacts/audit/s05p4_v5_4_1_independent_adjudication_no_key_validation.md"
        ),
    )
    args = parser.parse_args()
    manifest = json.loads((args.packet_dir / "PACKET_MANIFEST.json").read_text(encoding="utf-8"))
    results = {
        name: validate_packet(args.packet_dir, name, spec)
        for name, spec in PACKETS.items()
    }
    pass_all = all(row["status"] == "PASS" for row in results.values())
    report = {
        "version": "s05p4-v5.4.1-independent-adjudication-no-key-validation-v1",
        "validation_scope": "format_integrity_and_logical_consistency_without_sealed_key",
        "status": "PASS" if pass_all else "FAIL",
        "sealed_model_key_opened": False,
        "set_b_accessed": False,
        "comparison_run": False,
        "packet_manifest_sha256": sha256_file(args.packet_dir / "PACKET_MANIFEST.json"),
        "packet_safety": {
            "set_b_included": manifest["set_b_included"],
            "sealed_key_included": manifest["sealed_key_included"],
            "prior_review_included": manifest["prior_review_included"],
        },
        "packets": results,
    }
    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    lines = [
        "# S05P4 v5.4.1 independent adjudication no-key validation",
        "",
        f"- Status: **{report['status']}**",
        "- Sealed model key opened: `false`",
        "- Set B accessed: `false`",
        "- Comparison run: `false`",
        "",
        "| Packet | Status | Rows | Issues |",
        "|---|---|---:|---:|",
    ]
    for name, row in results.items():
        lines.append(f"| {name} | {row['status']} | {row['rows']} | {sum(row['issues'].values())} |")
    args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_output.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({
        "status": report["status"],
        "sealed_model_key_opened": False,
        "set_b_accessed": False,
        "comparison_run": False,
        "packets": {name: {"status": row["status"], "rows": row["rows"], "issues": sum(row["issues"].values())} for name, row in results.items()},
    }, indent=2, ensure_ascii=False))
    return 0 if pass_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
