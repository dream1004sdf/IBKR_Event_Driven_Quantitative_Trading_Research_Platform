from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]

PACKETS = {
    "target_change": {
        "source_key": "target_change_packet",
        "source": ROOT / "artifacts/audit/s05p4_v5_4_1_target_change_review_packet.csv",
        "rows": 201,
    },
    "news_content_type": {
        "source_key": "news_content_type_packet",
        "source": ROOT / "artifacts/audit/s05p4_v5_4_1_news_content_type_review_packet.csv",
        "rows": 400,
    },
    "multi_role_region_channel": {
        "source_key": "multi_role_packet",
        "source": ROOT
        / "artifacts/audit/s05p4_v5_4_1_multi_role_region_channel_review_packet.csv",
        "rows": 389,
    },
    "abstention": {
        "source_key": "abstention_packet",
        "source": ROOT / "artifacts/audit/s05p4_v5_4_1_abstention_review_packet.csv",
        "rows": 1170,
    },
    "recovered_event": {
        "source_key": "recovered_event_packet",
        "source": ROOT / "artifacts/audit/s05p4_v5_4_1_recovered_event_review_packet.csv",
        "rows": 6,
    },
}

FORBIDDEN_HEADER_TOKENS = (
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


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def csv_info(path: Path, expected_rows: int) -> dict[str, Any]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"missing CSV header: {path}")
        fields = list(reader.fieldnames)
        rows = list(reader)
    if len(rows) != expected_rows:
        raise ValueError(f"unexpected row count for {path}: {len(rows)} != {expected_rows}")
    lowered = [field.lower() for field in fields]
    forbidden = sorted(
        {
            token
            for token in FORBIDDEN_HEADER_TOKENS
            if any(token in field for field in lowered)
        }
    )
    if forbidden:
        raise ValueError(f"forbidden columns in {path.name}: {forbidden}")
    human_fields = [
        field
        for field in fields
        if field.startswith("human_")
        or field in {"reviewer_confidence", "reviewer_id", "reviewed_at"}
    ]
    nonblank_human_cells = sum(
        1
        for row in rows
        for field in human_fields
        if row[field].strip()
    )
    if nonblank_human_cells:
        raise ValueError(
            f"source packet contains prefilled reviewer cells: {path} "
            f"({nonblank_human_cells})"
        )
    return {
        "rows": len(rows),
        "columns": fields,
        "unique_review_row_ids": len({row["review_row_id"] for row in rows}),
        "human_fields": human_fields,
        "nonblank_human_cells": nonblank_human_cells,
        "forbidden_header_tokens": forbidden,
    }


def exact_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    if sha256_file(source) != sha256_file(target):
        raise RuntimeError(f"copy hash mismatch: {source} -> {target}")


def add_manifest_file(
    files: dict[str, dict[str, Any]],
    path: Path,
    *,
    source: str,
    role: str,
    csv_details: dict[str, Any] | None = None,
) -> None:
    item: dict[str, Any] = {
        "source": source,
        "packet_path": str(path.resolve()),
        "role": role,
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }
    if csv_details is not None:
        item.update(csv_details)
    files[path.name] = item


def build_zip(packet_dir: Path, zip_path: Path, names: list[str]) -> None:
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(names):
            archive.write(packet_dir / name, arcname=name)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Build a clean official-human Set A handoff packet from frozen v5.4.1 inputs"
        )
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT
        / "artifacts/review/s05p4_v5_4_1_official_human_set_a/reviewer_packet",
    )
    parser.add_argument(
        "--zip-output",
        type=Path,
        default=ROOT
        / "artifacts/review/s05p4_v5_4_1_official_human_set_a/"
        "s05p4_v5_4_1_official_human_set_a_packet.zip",
    )
    parser.add_argument(
        "--receipt-json",
        type=Path,
        default=ROOT
        / "artifacts/audit/s05p4_v5_4_1_official_human_set_a_packet_release.json",
    )
    parser.add_argument(
        "--receipt-markdown",
        type=Path,
        default=ROOT
        / "artifacts/audit/s05p4_v5_4_1_official_human_set_a_packet_release.md",
    )
    args = parser.parse_args()

    freeze_path = ROOT / "artifacts/audit/s05p4_v5_4_1_freeze_manifest.json"
    freeze = json.loads(freeze_path.read_text(encoding="utf-8"))
    if freeze["freeze_status"] != "FROZEN_FOR_SET_A_INDEPENDENT_ADJUDICATION":
        raise RuntimeError("v5.4.1 is not in the expected frozen state")

    if args.output_dir.exists():
        existing = [path for path in args.output_dir.iterdir()]
        if existing:
            raise FileExistsError(
                f"refusing to overwrite an existing human reviewer packet: {args.output_dir}"
            )
    args.output_dir.mkdir(parents=True, exist_ok=True)

    files: dict[str, dict[str, Any]] = {}
    codebook_source = (
        ROOT / "artifacts/review/s05p4_independent_semantic_v1/REVIEW_CODEBOOK.md"
    )
    codebook_target = args.output_dir / "REVIEW_CODEBOOK.md"
    exact_copy(codebook_source, codebook_target)
    add_manifest_file(
        files,
        codebook_target,
        source=str(codebook_source.resolve()),
        role="frozen_codebook",
    )

    for name, spec in PACKETS.items():
        source = spec["source"]
        expected_hash = freeze["files"][spec["source_key"]]["sha256"]
        actual_hash = sha256_file(source)
        if actual_hash != expected_hash:
            raise RuntimeError(
                f"frozen input hash mismatch for {name}: {actual_hash} != {expected_hash}"
            )
        details = csv_info(source, spec["rows"])
        input_target = args.output_dir / f"{name}_input.csv"
        template_target = args.output_dir / f"{name}_output_template.csv"
        exact_copy(source, input_target)
        exact_copy(source, template_target)
        add_manifest_file(
            files,
            input_target,
            source=str(source.resolve()),
            role="frozen_input",
            csv_details=details,
        )
        add_manifest_file(
            files,
            template_target,
            source=str(source.resolve()),
            role="blank_output_template",
            csv_details=details,
        )

    instructions = """# S05P4 v5.4.1 official independent human Set A adjudication

## Independence boundary

Use only the files in this packet. Do not inspect prior AI reviews, completed AI CSVs,
comparison/audit reports, the sealed key, Set B, repository code, market prices, returns,
MFE/MAE, PnL, later news, or internet search. If any of those have already been seen,
stop and assign the packet to a different reviewer.

This packet contains no AI-completed values. The `*_output_template.csv` files are exact
blank copies of the frozen Set A inputs. Copy each template to `*_completed.csv`; preserve
all input columns, row order, and `review_row_id` values exactly.

## Mechanical completion contract

- `target_change`: mark `human_v5_target_correct` as `1` or `0`; when `0`, supply
  `human_corrected_target`, otherwise leave it blank.
- `news_content_type`: mark correctness as `1` or `0`; when `0`, supply the corrected
  content type. Confidence is an integer from `1` through `5`.
- `multi_role_region_channel`: fill every region list and secondary-channel list as a JSON
  array of codes. Fill the primary channel. Set `human_region_channel_correct=1` only when
  all proposed roles and channels are correct; otherwise use `0` and provide the complete
  corrected human graph.
- `abstention`: use `human_should_abstain=1` only when the headline cannot support the
  field or when primary-proxy selection requires pre-event market metrics. When it is `0`,
  supply `human_corrected_target`; when it is `1`, leave that field blank and record the
  required mapping level.
- `recovered_event`: judge whether the recovered event clause is correct and whether it is
  causal-eligible after the clause split. When recovery is incorrect, supply a corrected
  event clause.
- Use one stable reviewer ID and timezone-aware ISO-8601 `reviewed_at` values.
- Do not add, remove, or rename columns.

The frozen `REVIEW_CODEBOOK.md` remains authoritative. These instructions only describe
how to fill the five frozen forms; they do not change semantic rules or thresholds.

After all five files are completed, return only the five `*_completed.csv` files. The
operator will run no-key validation before any comparison. Do not open Set B.
"""
    instructions_path = args.output_dir / "HUMAN_REVIEW_INSTRUCTIONS.md"
    instructions_path.write_text(instructions, encoding="utf-8", newline="\n")
    add_manifest_file(
        files,
        instructions_path,
        source="generated_mechanical_handoff_instructions",
        role="reviewer_instructions",
    )

    manifest = {
        "version": "s05p4-v5.4.1-official-human-set-a-handoff-v1",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "status": "READY_FOR_EXTERNAL_INDEPENDENT_HUMAN_REVIEW",
        "review_completion": "PENDING",
        "source_freeze_manifest": str(freeze_path.resolve()),
        "source_freeze_manifest_sha256": sha256_file(freeze_path),
        "semantic_version": freeze["semantic_version"],
        "set_a_rows": freeze["set_a_rows"],
        "set_b_included": False,
        "sealed_key_included": False,
        "ai_completed_values_included": False,
        "ai_summary_or_metrics_included": False,
        "market_outcomes_included": False,
        "comparison_output_included": False,
        "database_access_required": False,
        "files": files,
    }
    manifest_path = args.output_dir / "PACKET_MANIFEST.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    zip_names = [*files.keys(), manifest_path.name]
    build_zip(args.output_dir, args.zip_output, zip_names)
    with zipfile.ZipFile(args.zip_output, "r") as archive:
        zipped_names = sorted(archive.namelist())
        if zipped_names != sorted(zip_names):
            raise RuntimeError("ZIP contents differ from the declared safe packet")

    receipt = {
        "version": "s05p4-v5.4.1-official-human-set-a-release-receipt-v1",
        "status": "PASS_READY_FOR_EXTERNAL_INDEPENDENT_HUMAN_REVIEW",
        "review_completion": "PENDING",
        "packet_dir": str(args.output_dir.resolve()),
        "packet_manifest": str(manifest_path.resolve()),
        "packet_manifest_sha256": sha256_file(manifest_path),
        "zip_path": str(args.zip_output.resolve()),
        "zip_bytes": args.zip_output.stat().st_size,
        "zip_sha256": sha256_file(args.zip_output),
        "zip_files": zipped_names,
        "zip_file_count": len(zipped_names),
        "frozen_source_hashes_match": True,
        "blank_reviewer_cells": True,
        "set_b_included": False,
        "sealed_key_included": False,
        "ai_results_included": False,
        "comparison_run": False,
        "canonical_or_db_modified": False,
        "downstream_started": False,
    }
    args.receipt_json.parent.mkdir(parents=True, exist_ok=True)
    args.receipt_json.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    markdown = [
        "# S05P4 v5.4.1 official human Set A packet release",
        "",
        f"- Status: **{receipt['status']}**",
        "- Human review completion: **PENDING**",
        f"- ZIP files: `{receipt['zip_file_count']}`",
        f"- ZIP bytes: `{receipt['zip_bytes']}`",
        f"- ZIP SHA-256: `{receipt['zip_sha256']}`",
        "- Frozen source hashes match: `true`",
        "- Blank reviewer cells: `true`",
        "- Set B included: `false`",
        "- Sealed key included: `false`",
        "- Prior AI results included: `false`",
        "- Comparison run: `false`",
        "- Canonical/DB/downstream changes: `false`",
        "",
        "The package is ready to hand to an external independent human reviewer. "
        "No official result exists until all five completed CSVs are returned and pass no-key validation.",
    ]
    args.receipt_markdown.write_text(
        "\n".join(markdown) + "\n", encoding="utf-8", newline="\n"
    )
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
