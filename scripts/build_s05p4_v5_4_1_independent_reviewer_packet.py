from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_HEADER_TOKENS = (
    "return", "mfe", "mae", "pnl", "model_decision", "strict_eligibility",
    "event_cluster_id", "set_b", "tp", "fp", "fn", "tn",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def csv_info(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"missing CSV header: {path}")
        rows = list(reader)
    lowered = [name.lower() for name in reader.fieldnames]
    forbidden = sorted({
        token for token in FORBIDDEN_HEADER_TOKENS
        if any(token in name for name in lowered)
    })
    if forbidden:
        raise ValueError(f"reviewer packet exposes forbidden columns in {path.name}: {forbidden}")
    return {
        "rows": len(rows),
        "columns": list(reader.fieldnames),
        "unique_review_row_ids": len({row["review_row_id"] for row in rows}),
        "forbidden_header_tokens": forbidden,
    }


def copy_exact(source: Path, target: Path) -> dict[str, Any]:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    source_hash = sha256_file(source)
    target_hash = sha256_file(target)
    if source_hash != target_hash:
        raise RuntimeError(f"copy hash mismatch: {source} -> {target}")
    row = {
        "source_path": str(source.resolve()),
        "packet_path": str(target.resolve()),
        "bytes": target.stat().st_size,
        "sha256": target_hash,
        "source_copy_hash_match": True,
    }
    if target.suffix.lower() == ".csv":
        row.update(csv_info(target))
    return row


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build a reviewer-safe v5.4.1 Set A packet without Set B or comparison data"
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(
            "artifacts/review/s05p4_v5_4_1_independent_target_adjudication/reviewer_packet"
        ),
    )
    args = parser.parse_args()
    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    sources = {
        "REVIEW_CODEBOOK.md": ROOT / "artifacts/review/s05p4_independent_semantic_v1/REVIEW_CODEBOOK.md",
        "target_change_input.csv": ROOT / "artifacts/audit/s05p4_v5_4_1_target_change_review_packet.csv",
        "news_content_type_input.csv": ROOT / "artifacts/audit/s05p4_v5_4_1_news_content_type_review_packet.csv",
        "multi_role_region_channel_input.csv": ROOT / "artifacts/audit/s05p4_v5_4_1_multi_role_region_channel_review_packet.csv",
        "abstention_input.csv": ROOT / "artifacts/audit/s05p4_v5_4_1_abstention_review_packet.csv",
        "recovered_event_input.csv": ROOT / "artifacts/audit/s05p4_v5_4_1_recovered_event_review_packet.csv",
    }
    manifest_files = {
        name: copy_exact(source, output_dir / name)
        for name, source in sources.items()
    }
    for name, source in sources.items():
        if source.suffix.lower() == ".csv":
            template_name = name.replace("_input.csv", "_output_template.csv")
            manifest_files[template_name] = copy_exact(source, output_dir / template_name)

    instructions = """# S05P4 v5.4.1 independent Set A target adjudication

Use only `REVIEW_CODEBOOK.md` and the five `*_input.csv` files in this directory.
Do not inspect repository files, prior reviews, audit/comparison reports, model keys, Set B,
market outcomes, returns, MFE/MAE, PnL, or later news. Do not browse the internet.

Write completed results to new `*_completed.csv` files. Preserve every non-human input
field and row order exactly. Fill all reviewer fields with literal `0`/`1` booleans where
applicable, one stable reviewer ID, and ISO-8601 timestamps with a timezone offset.
Correction fields stay blank when the proposal is correct and contain a corrected value
when it is incorrect. Region lists and channel lists must be JSON arrays of codes.

This is an independent AI target adjudication artifact, not an official human review.
Do not apply any result to a database. Do not open the sealed key and do not run comparison.
"""
    instructions_path = output_dir / "INDEPENDENT_REVIEW_INSTRUCTIONS.md"
    instructions_path.write_text(instructions, encoding="utf-8", newline="\n")
    manifest_files[instructions_path.name] = {
        "source_path": "generated_reviewer_safe_instructions",
        "packet_path": str(instructions_path.resolve()),
        "bytes": instructions_path.stat().st_size,
        "sha256": sha256_file(instructions_path),
        "source_copy_hash_match": True,
    }

    manifest = {
        "version": "s05p4-v5.4.1-independent-ai-target-adjudication-packet-v1",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "scope": "set_a_only_reviewer_safe",
        "set_b_included": False,
        "sealed_key_included": False,
        "comparison_output_included": False,
        "market_outcome_included": False,
        "prior_review_included": False,
        "files": manifest_files,
    }
    manifest_path = output_dir / "PACKET_MANIFEST.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({
        "packet_dir": str(output_dir.resolve()),
        "packet_manifest": str(manifest_path.resolve()),
        "files": len(manifest_files),
        "set_b_included": False,
        "sealed_key_included": False,
        "prior_review_included": False,
    }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
