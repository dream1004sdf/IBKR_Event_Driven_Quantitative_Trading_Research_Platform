from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from recorder.s05p4_human_semantic_review import (
    CODEBOOK_VERSION,
    EXCLUSION_REASONS,
    HUMAN_REVIEW_FIELDS,
    REVIEW_POLICY,
    REVIEW_INPUT_FIELDS,
    S05P4_VERSION,
    apply_completed_review_to_clone,
    build_review_material,
    canonical_json_hash,
    compare_reviews,
    load_json,
    markdown_summary,
    read_csv,
    reconcile_proxy_assignments,
    review_output_template,
    sha256_file,
    stable_hash,
    validate_review_output_without_model_key,
    write_csv,
    write_json,
)


DEFAULT_CANONICAL = ROOT / "work" / "canonical_research_backfill" / "m254_canonical_20250101_20260706_v1" / "canonical_market_events.db"
DEFAULT_CLONE = ROOT / "work" / "s05_full_range_news_clone" / "v1" / "canonical_market_events_s05_full_range_clone.db"
DEFAULT_BLIND = ROOT / "artifacts" / "audit" / "s05_full_range_proxy_blind_review.json"
DEFAULT_CLOSEOUT = ROOT / "artifacts" / "audit" / "s05_full_range_news_closeout.json"
DEFAULT_CONFIG = ROOT / "config" / "s05p4_independent_semantic_review_v1.json"
DEFAULT_PUBLIC = ROOT / "artifacts" / "review" / "s05p4_independent_semantic_v1"
DEFAULT_WORK = ROOT / "work" / "s05p4_independent_semantic" / "v1"
DEFAULT_SEALED = DEFAULT_WORK / "sealed" / "sealed_model_key.json"
DEFAULT_FROZEN_CLONE = DEFAULT_WORK / "frozen" / "canonical_market_events_s05_full_range_frozen.db"
DEFAULT_MANIFEST_JSON = ROOT / "artifacts" / "audit" / "s05p4_human_semantic_review_manifest.json"
DEFAULT_MANIFEST_MD = ROOT / "artifacts" / "audit" / "s05p4_human_semantic_review_manifest.md"
DEFAULT_PROTOCOL_JSON = DEFAULT_PUBLIC / "REVIEW_PROTOCOL_FREEZE.json"
DEFAULT_PROTOCOL_MD = DEFAULT_PUBLIC / "REVIEW_PROTOCOL_FREEZE.md"


def sqlite_snapshot(source: Path, destination: Path) -> bool:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        with sqlite3.connect(f"file:{destination.as_posix()}?mode=ro", uri=True) as existing:
            integrity = existing.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"existing frozen clone is not valid: {destination} ({integrity})")
        return False
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.unlink(missing_ok=True)
    src = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    dst = sqlite3.connect(temporary)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    temporary.replace(destination)
    return True


def codebook() -> str:
    fields = "\n".join(f"- `{field}`" for field in HUMAN_REVIEW_FIELDS)
    reasons = "\n".join(f"- `{reason}`" for reason in sorted(EXCLUSION_REASONS))
    return f"""# S05P4 independent semantic review

- Codebook version: `{CODEBOOK_VERSION}`
- Applies unchanged to review sets A and B.

This packet contains only frozen-v3 proposals. It does not contain the model's accept/reject decision,
prior adjudication, strict eligibility, event-cluster IDs, or return/outcome/PnL fields. Review only the
headline and proposed semantic fields. Do not use internet search, price charts, later news, or any other
post-event information.

## Reviewer independence

The reviewer must not have designed the v3 rules, read the sealed model key, seen set B before release,
or use this task's prior model-decision discussion. If any condition is violated, stop and replace the reviewer.

## Workflow

1. Review every row in set A without opening or requesting lockbox B.
2. Preserve all ten frozen input columns and `review_row_id` exactly.
3. Fill every human field using this codebook. Do not leave `NA`, `unknown`, or blank boolean fields.
4. Validate format, completeness, and logical consistency without opening the sealed key.
5. Only after validation passes, run the set-A sealed comparison.
6. Set B may be released only if set A passes. Use this exact same codebook version for B.

## Human fields

{fields}

All nine boolean fields accept exact `1` or `0` only.

## `human_causal_eligible`

- `1`: the headline first reports a new market-relevant event or adds materially new information to an existing event.
- `0`: market or price recap, preview, opinion/analysis, listicle, technical/valuation article, simple forecast,
  or a repeat/update without material new information.

## `human_primary_event`

- `1`: the event itself or an authoritative first report, such as an official policy decision, announcement,
  actual attack/agreement/sanction, or supply change.
- `0`: strategist/analyst reaction, market-move summary, `ahead of`, `what to do`, `stocks to watch`,
  or other secondary commentary. A materially new secondary report may be causal eligible while not primary.
- `human_primary_event=1` with `human_causal_eligible=0` is invalid.

## Correctness fields

Judge content type, region, direction, affected channel, proxy family, and primary proxy separately even when
`human_causal_eligible=0`. A recap may be non-causal while its proposed region or family is still correct.
These field metrics use all reviewed blind rows, not only human-retained rows.

## `human_duplicate_or_update`

- `1`: the headline itself clearly says `reported earlier`, `updated`, repeats a market recap, duplicates the same
  title, or is plainly a follow-up explanation without material new information.
- `0`: the headline alone appears to contain new substantive information.
- The packet does not provide a full related-headline series. Use only headline-local evidence.

## Ambiguous or insufficient context

Missing/NA values are not allowed. If the packet is insufficient, use `human_causal_eligible=0`,
`human_exclusion_reason=insufficient_context`, confidence `1` or `2`, and set an unconfirmable correctness field
to `0`. Set `human_duplicate_or_update=1` only when the headline itself contains update/repeat evidence;
otherwise use `0`. Ambiguous rows remain in metric denominators and cannot be removed after results are seen.

## Exclusion reason controlled vocabulary

When `human_causal_eligible=0`, choose exactly one value below. When it is `1`, leave the reason blank.

{reasons}

## Reviewer confidence

- `1`: very low
- `2`: low
- `3`: medium
- `4`: high
- `5`: very high

`reviewer_id` is mandatory. `reviewed_at` must be ISO-8601 with a timezone offset.

## Reviewer conflicts

No majority vote is allowed. If multiple submissions disagree on any boolean field, that row is unresolved,
excluded from metrics, and the phase fails. Resolve it in a separately documented adjudication pass, preserve
the original reviews, and compare only a final consolidated output.

## Frozen metric formulas

- Causal precision: `TP / (TP + FP)`.
- Causal recall: `TP / (TP + FN)`; reported but not a hard promotion gate.
- Content/region/direction/channel/family/proxy correctness: all reviewed blind rows.
- Critical retained region/channel errors: frozen-v3 retained and human-causal rows only.
- Duplicate/update rejection metric: among human duplicate/update rows, the fraction frozen v3 excluded.
- Confidence intervals: two-sided Wilson score interval using `z=1.959963984540054`.

Do not add return, outcome, MFE, MAE, PnL, automated decision, or strict-eligibility columns.
"""


def protocol_markdown(protocol: dict[str, Any]) -> str:
    checksums = "\n".join(
        f"- {key}: `{value}`" for key, value in protocol["checksums"].items()
    )
    thresholds = "\n".join(
        f"- {key}: `{value}`" for key, value in protocol["phase_gate_thresholds"].items()
    )
    return f"""# S05P4 review protocol freeze

- Version: `{protocol['version']}`
- Codebook: `{protocol['codebook_version']}`
- Status: `{protocol['status']}`
- Frozen at: `{protocol['frozen_at_utc']}`
- Set A rows: `{protocol['counts']['set_a']}`
- Set B locked rows: `{protocol['counts']['set_b_locked']}`
- Design rows in metrics: `{protocol['counts']['design_rows_in_metrics']}`

## Checksums

{checksums}

## Phase-gate thresholds

{thresholds}

## Frozen policies

```json
{json.dumps(protocol['review_policy'], indent=2, sort_keys=True)}
```

Set B content and the sealed model key remain unavailable to reviewers. This document contains commitments only.
"""


def freeze_summary(closeout: dict[str, Any], canonical: Path, clone: Path, frozen_clone: Path) -> dict[str, Any]:
    closeout_manifest = dict(closeout.get("manifest") or {})
    expected_canonical = str(closeout_manifest.get("canonical_source_checksum") or "")
    actual_canonical = sha256_file(canonical)
    if not expected_canonical or actual_canonical != expected_canonical:
        raise RuntimeError(
            f"canonical source checksum mismatch: expected={expected_canonical!r}, actual={actual_canonical!r}"
        )
    frozen_clone_created = sqlite_snapshot(clone, frozen_clone)
    with sqlite3.connect(f"file:{frozen_clone.as_posix()}?mode=ro", uri=True) as frozen_conn:
        frozen_integrity = frozen_conn.execute("PRAGMA integrity_check").fetchone()[0]
    return {
        "canonical_source_db": str(canonical.resolve()),
        "canonical_source_checksum": actual_canonical,
        "canonical_matches_s05_closeout": True,
        "source_clone_db": str(clone.resolve()),
        "frozen_clone_db": str(frozen_clone.resolve()),
        "frozen_clone_checksum": sha256_file(frozen_clone),
        "frozen_clone_integrity": frozen_integrity,
        "frozen_clone_created_this_run": frozen_clone_created,
        "closeout_verdict": str(closeout.get("verdict") or ""),
        "v3_code_hashes": closeout_manifest.get("code_hashes", {}),
        "v3_config_hash": str(closeout_manifest.get("config_hash") or ""),
        "v3_proxy_map_version": str(closeout_manifest.get("proxy_map_version") or ""),
        "v3_reaction_plan_version": str(closeout_manifest.get("reaction_plan_version") or ""),
    }


def command_generate(args: argparse.Namespace) -> int:
    if args.protocol_json.exists():
        raise RuntimeError(
            f"review protocol is already frozen at {args.protocol_json}; do not regenerate after review preparation"
        )
    config = load_json(args.config)
    if str(config.get("codebook_version") or "") != CODEBOOK_VERSION:
        raise RuntimeError("config/codebook version mismatch")
    closeout = load_json(args.closeout)
    source_payload = load_json(args.blind_pack)
    args.public_dir.mkdir(parents=True, exist_ok=True)
    codebook_path = args.public_dir / "REVIEW_CODEBOOK.md"
    codebook_path.write_text(codebook(), encoding="utf-8")
    codebook_hash = sha256_file(codebook_path)
    freeze = freeze_summary(closeout, args.canonical_db, args.clone_db, args.frozen_clone_db)
    config_hash = sha256_file(args.config)
    blind_hash = sha256_file(args.blind_pack)
    seed = stable_hash(S05P4_VERSION, config_hash, blind_hash, freeze["canonical_source_checksum"], freeze["frozen_clone_checksum"])
    material = build_review_material(source_payload, seed=seed, target_a=int(config["split"]["set_a_count"]))
    expected_b = int(config["split"]["set_b_count"])
    if material["audit"]["set_counts"]["B"] != expected_b:
        raise RuntimeError(f"set B count mismatch: {material['audit']['set_counts']['B']} != {expected_b}")
    set_b_commitment = canonical_json_hash(material["sanitized_by_set"]["B"])
    reconciliation = reconcile_proxy_assignments(args.frozen_clone_db, args.canonical_db)
    if not reconciliation["pass"]:
        raise RuntimeError(f"proxy assignment reconciliation failed: {reconciliation}")
    sealed_key = {
        "version": S05P4_VERSION,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "warning": "SEALED MODEL KEY - DO NOT DISTRIBUTE TO REVIEWERS BEFORE ALL REQUIRED REVIEWS ARE COMPLETE",
        "sealing": "workflow-separated and checksum-locked; not encrypted",
        "codebook": {
            "version": CODEBOOK_VERSION,
            "sha256": codebook_hash,
            "review_policy": REVIEW_POLICY,
        },
        "set_b_locked_payload_sha256": set_b_commitment,
        "seed_commitment": canonical_json_hash(seed),
        "source_blind_pack": str(args.blind_pack.resolve()),
        "source_blind_pack_sha256": blind_hash,
        "config": config,
        "config_sha256": config_hash,
        "freeze": freeze,
        "audit": material["audit"],
        "reconciliation": reconciliation,
        "rows": material["sealed_rows"],
    }
    write_json(args.sealed_key, sealed_key)
    set_a = material["sanitized_by_set"]["A"]
    write_csv(args.public_dir / "human_review_input_A.csv", set_a, REVIEW_INPUT_FIELDS)
    write_csv(
        args.public_dir / "human_review_output_A.csv",
        review_output_template(set_a),
        REVIEW_INPUT_FIELDS + HUMAN_REVIEW_FIELDS,
    )
    sealed_hash = sha256_file(args.sealed_key)
    protocol = {
        "version": S05P4_VERSION,
        "codebook_version": CODEBOOK_VERSION,
        "status": "FROZEN_BEFORE_FIRST_HUMAN_REVIEW",
        "frozen_at_utc": datetime.now(UTC).isoformat(),
        "reviewer_safe": True,
        "counts": {
            "set_a": len(set_a),
            "set_b_locked": expected_b,
            "design_rows_in_metrics": 0,
        },
        "checksums": {
            "set_a_input_sha256": sha256_file(args.public_dir / "human_review_input_A.csv"),
            "set_a_output_template_sha256": sha256_file(args.public_dir / "human_review_output_A.csv"),
            "set_b_locked_payload_sha256": set_b_commitment,
            "codebook_sha256": codebook_hash,
            "sealed_model_key_sha256": sealed_hash,
            "comparison_script_sha256": sha256_file(Path(__file__)),
            "comparison_module_sha256": sha256_file(ROOT / "recorder" / "s05p4_human_semantic_review.py"),
            "config_sha256": config_hash,
            "canonical_source_sha256": freeze["canonical_source_checksum"],
            "frozen_clone_sha256": freeze["frozen_clone_checksum"],
        },
        "phase_gate_thresholds": config["promotion_gates"],
        "review_policy": REVIEW_POLICY,
        "exclusion_reason_vocabulary": sorted(EXCLUSION_REASONS),
        "set_b_release_policy": config["split"]["set_b_release_policy"],
        "safety": config["safety"],
    }
    write_json(args.protocol_json, protocol)
    args.protocol_md.parent.mkdir(parents=True, exist_ok=True)
    args.protocol_md.write_text(protocol_markdown(protocol), encoding="utf-8")
    protocol_hash = sha256_file(args.protocol_json)
    family_counts = dict(material["audit"]["blind_family_counts"])
    djnl_rows = [
        row for row in material["sealed_rows"]
        if str(row["sanitized"].get("provider") or "") == "djnl"
    ]
    coverage_limits = {
        "china_taiwan_blind_count": int(family_counts.get("china_taiwan", 0)),
        "small_sample_families": {
            family: int(family_counts.get(family, 0))
            for family in ("cybersecurity", "grain_supply", "korea_risk")
        },
        "family_level_claim_policy": {
            "defense_conflict_rates_policy_oil_supply": "family-level independent validation available after review",
            "sanctions": "limited validation",
            "cybersecurity_grain_supply_korea_risk": "small-sample coverage gap",
            "china_taiwan": "no independent historical blind coverage; live-forward validation required",
        },
    }
    manifest = {
        "version": S05P4_VERSION,
        "codebook_version": CODEBOOK_VERSION,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "status": "READY_FOR_SET_A_REVIEW",
        "canonical_promotion": "HOLD",
        "m254_m255_m256": "NOT_STARTED",
        "counts": {
            "source_total": material["source_counts"]["total"],
            "design_excluded": material["source_counts"]["design"],
            "independent_blind": material["source_counts"]["blind"],
            "set_a_released": len(set_a),
            "set_b_locked": material["audit"]["set_counts"]["B"],
            "model_v3_accept_blind": int(material["audit"]["model_decision_counts"].get(1, 0)),
            "model_v3_reject_blind": int(material["audit"]["model_decision_counts"].get(0, 0)),
        },
        "freeze": freeze,
        "audit": material["audit"],
        "coverage_limits": coverage_limits,
        "djnl": {
            "full_range_blind_proxy_candidates": len(djnl_rows),
            "frozen_v3_retained": sum(int(row["model_v3_decision"]) == 1 for row in djnl_rows),
            "frozen_v3_excluded": sum(int(row["model_v3_decision"]) == 0 for row in djnl_rows),
            "interpretation": "candidate coverage exists; high-precision retained assignment is reported separately",
        },
        "reconciliation": reconciliation,
        "files": {
            "human_review_input_A": str((args.public_dir / "human_review_input_A.csv").resolve()),
            "human_review_input_A_sha256": sha256_file(args.public_dir / "human_review_input_A.csv"),
            "human_review_output_A": str((args.public_dir / "human_review_output_A.csv").resolve()),
            "human_review_output_A_sha256": sha256_file(args.public_dir / "human_review_output_A.csv"),
            "set_b": "LOCKED_IN_SEALED_MODEL_KEY_PENDING_SET_A_GATE_PASS",
            "set_b_locked_payload_sha256": set_b_commitment,
            "sealed_model_key": str(args.sealed_key.resolve()),
            "sealed_model_key_sha256": sealed_hash,
            "codebook": str(codebook_path.resolve()),
            "codebook_sha256": codebook_hash,
            "review_protocol_freeze": str(args.protocol_json.resolve()),
            "review_protocol_freeze_sha256": protocol_hash,
        },
        "code_hashes": {
            "generator": sha256_file(Path(__file__)),
            "module": sha256_file(ROOT / "recorder" / "s05p4_human_semantic_review.py"),
            "config": config_hash,
        },
        "safety": config["safety"],
    }
    write_json(args.manifest_json, manifest)
    args.manifest_md.parent.mkdir(parents=True, exist_ok=True)
    args.manifest_md.write_text(markdown_summary(manifest), encoding="utf-8")
    print(json.dumps(manifest, indent=2, ensure_ascii=False))
    return 0


def command_freeze_protocol(args: argparse.Namespace) -> int:
    if args.protocol_json.exists():
        raise RuntimeError(f"review protocol is already frozen at {args.protocol_json}")
    config = load_json(args.config)
    if str(config.get("codebook_version") or "") != CODEBOOK_VERSION:
        raise RuntimeError("config/codebook version mismatch")
    sealed = load_json(args.sealed_key)
    sealed_rows = [dict(row) for row in sealed["rows"]]
    expected_a = [dict(row["sanitized"]) for row in sealed_rows if str(row["review_set"]) == "A"]
    expected_b = [dict(row["sanitized"]) for row in sealed_rows if str(row["review_set"]) == "B"]
    input_a_path = args.public_dir / "human_review_input_A.csv"
    output_a_path = args.public_dir / "human_review_output_A.csv"
    input_a = read_csv(input_a_path)
    output_a = read_csv(output_a_path)
    expected_by_id = {str(row["review_row_id"]): row for row in expected_a}
    input_by_id = {str(row["review_row_id"]): row for row in input_a}
    output_by_id = {
        str(row["review_row_id"]): {field: row.get(field, "") for field in REVIEW_INPUT_FIELDS}
        for row in output_a
    }
    if len(expected_a) != 400 or len(expected_b) != 145:
        raise RuntimeError(f"sealed split changed: A={len(expected_a)}, B={len(expected_b)}")
    if expected_by_id != input_by_id or expected_by_id != output_by_id:
        raise RuntimeError("existing public A packet does not match the sealed A rows")
    if any(str(row.get(field) or "").strip() for row in output_a for field in HUMAN_REVIEW_FIELDS):
        raise RuntimeError("set A already contains human review values; protocol must be frozen before first review")
    if (args.public_dir / "human_review_input_B.csv").exists() or (args.public_dir / "human_review_output_B.csv").exists():
        raise RuntimeError("set B is already public; cannot create a pre-review freeze")
    codebook_path = args.public_dir / "REVIEW_CODEBOOK.md"
    codebook_path.write_text(codebook(), encoding="utf-8")
    codebook_hash = sha256_file(codebook_path)
    set_b_commitment = canonical_json_hash(expected_b)
    sealed["config"] = config
    sealed["config_sha256"] = sha256_file(args.config)
    sealed["codebook"] = {
        "version": CODEBOOK_VERSION,
        "sha256": codebook_hash,
        "review_policy": REVIEW_POLICY,
    }
    sealed["set_b_locked_payload_sha256"] = set_b_commitment
    sealed["protocol_frozen_at_utc"] = datetime.now(UTC).isoformat()
    write_json(args.sealed_key, sealed)
    sealed_hash = sha256_file(args.sealed_key)
    protocol = {
        "version": S05P4_VERSION,
        "codebook_version": CODEBOOK_VERSION,
        "status": "FROZEN_BEFORE_FIRST_HUMAN_REVIEW",
        "frozen_at_utc": sealed["protocol_frozen_at_utc"],
        "reviewer_safe": True,
        "counts": {"set_a": 400, "set_b_locked": 145, "design_rows_in_metrics": 0},
        "checksums": {
            "set_a_input_sha256": sha256_file(input_a_path),
            "set_a_output_template_sha256": sha256_file(output_a_path),
            "set_b_locked_payload_sha256": set_b_commitment,
            "codebook_sha256": codebook_hash,
            "sealed_model_key_sha256": sealed_hash,
            "comparison_script_sha256": sha256_file(Path(__file__)),
            "comparison_module_sha256": sha256_file(ROOT / "recorder" / "s05p4_human_semantic_review.py"),
            "config_sha256": sha256_file(args.config),
            "canonical_source_sha256": str(sealed["freeze"]["canonical_source_checksum"]),
            "frozen_clone_sha256": str(sealed["freeze"]["frozen_clone_checksum"]),
        },
        "phase_gate_thresholds": config["promotion_gates"],
        "review_policy": REVIEW_POLICY,
        "exclusion_reason_vocabulary": sorted(EXCLUSION_REASONS),
        "set_b_release_policy": config["split"]["set_b_release_policy"],
        "safety": config["safety"],
        "packet_preservation": {
            "existing_review_ids_preserved": True,
            "existing_a_rows_preserved": True,
            "existing_b_rows_preserved": True,
            "set_overlap": 0,
        },
    }
    write_json(args.protocol_json, protocol)
    args.protocol_md.parent.mkdir(parents=True, exist_ok=True)
    args.protocol_md.write_text(protocol_markdown(protocol), encoding="utf-8")
    manifest = load_json(args.manifest_json)
    manifest["generated_at_utc"] = datetime.now(UTC).isoformat()
    manifest["status"] = "READY_FOR_SET_A_REVIEW_PROTOCOL_FROZEN"
    manifest["codebook_version"] = CODEBOOK_VERSION
    manifest["files"].update({
        "set_b_locked_payload_sha256": set_b_commitment,
        "sealed_model_key_sha256": sealed_hash,
        "codebook": str(codebook_path.resolve()),
        "codebook_sha256": codebook_hash,
        "review_protocol_freeze": str(args.protocol_json.resolve()),
        "review_protocol_freeze_sha256": sha256_file(args.protocol_json),
    })
    manifest["code_hashes"].update({
        "generator": sha256_file(Path(__file__)),
        "module": sha256_file(ROOT / "recorder" / "s05p4_human_semantic_review.py"),
        "config": sha256_file(args.config),
    })
    manifest["review_policy"] = REVIEW_POLICY
    write_json(args.manifest_json, manifest)
    args.manifest_md.write_text(markdown_summary(manifest), encoding="utf-8")
    print(json.dumps(protocol, indent=2, ensure_ascii=False))
    return 0


def command_compare(args: argparse.Namespace) -> int:
    sealed = load_json(args.sealed_key)
    validation = load_json(args.validation_report)
    if not validation.get("ready_for_sealed_comparison"):
        raise RuntimeError("sealed comparison is blocked until no-key validation passes")
    expected_hashes = {
        str(path.resolve()): sha256_file(path) for path in args.review_output
    }
    if validation.get("review_output_sha256") != expected_hashes:
        raise RuntimeError("review output changed after no-key validation")
    config = dict(sealed["config"])
    report = compare_reviews(
        sealed,
        args.review_output,
        phase=args.phase,
        gates=config["promotion_gates"],
        canonical_source_db=args.canonical_db,
    )
    report["sealed_model_key_sha256"] = sha256_file(args.sealed_key)
    report["no_key_validation_report"] = str(args.validation_report.resolve())
    report["no_key_validation_report_sha256"] = sha256_file(args.validation_report)
    write_json(args.json_output, report)
    args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_output.write_text(markdown_summary(report), encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("phase", "verdict", "phase_gate_pass", "required_rows", "reviewed_rows", "phase_gates")}, indent=2))
    return 0 if report["phase_gate_pass"] else 2


def command_validate(args: argparse.Namespace) -> int:
    report = validate_review_output_without_model_key(args.review_input, args.review_output)
    write_json(args.json_output, report)
    args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_output.write_text(markdown_summary(report), encoding="utf-8")
    print(json.dumps({
        key: report[key]
        for key in (
            "verdict", "ready_for_sealed_comparison", "expected_rows",
            "completed_unique_rows", "missing_row_count", "error_count",
            "sealed_model_key_opened",
        )
    }, indent=2))
    return 0 if report["ready_for_sealed_comparison"] else 2


def command_release_b(args: argparse.Namespace) -> int:
    sealed = load_json(args.sealed_key)
    set_a_report = load_json(args.set_a_report)
    protocol = load_json(args.protocol_json)
    if set_a_report.get("phase") != "A" or not set_a_report.get("phase_gate_pass"):
        raise RuntimeError("set B remains locked until a set-A comparison has phase_gate_pass=true")
    if sha256_file(args.codebook) != str(sealed["codebook"]["sha256"]):
        raise RuntimeError("codebook changed after protocol freeze")
    if protocol.get("codebook_version") != sealed["codebook"]["version"]:
        raise RuntimeError("protocol/codebook version mismatch")
    if protocol["checksums"]["sealed_model_key_sha256"] != sha256_file(args.sealed_key):
        raise RuntimeError("sealed model key changed after protocol freeze")
    set_b = [dict(row["sanitized"]) for row in sealed["rows"] if str(row["review_set"]) == "B"]
    write_csv(args.public_dir / "human_review_input_B.csv", set_b, REVIEW_INPUT_FIELDS)
    write_csv(
        args.public_dir / "human_review_output_B.csv",
        review_output_template(set_b),
        REVIEW_INPUT_FIELDS + HUMAN_REVIEW_FIELDS,
    )
    result = {
        "version": S05P4_VERSION,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "status": "SET_B_RELEASED_AFTER_SET_A_PASS",
        "set_b_rows": len(set_b),
        "set_a_report": str(args.set_a_report.resolve()),
        "set_a_report_sha256": sha256_file(args.set_a_report),
        "human_review_input_B": str((args.public_dir / "human_review_input_B.csv").resolve()),
        "human_review_input_B_sha256": sha256_file(args.public_dir / "human_review_input_B.csv"),
        "human_review_output_B": str((args.public_dir / "human_review_output_B.csv").resolve()),
    }
    write_json(args.json_output, result)
    print(json.dumps(result, indent=2))
    return 0


def command_apply(args: argparse.Namespace) -> int:
    sealed = load_json(args.sealed_key)
    comparison = load_json(args.comparison_report)
    report = apply_completed_review_to_clone(
        args.clone_db,
        sealed,
        comparison,
        sealed_key_sha256=sha256_file(args.sealed_key),
    )
    report["canonical_source_checksum"] = sha256_file(args.canonical_db)
    report["canonical_source_unchanged"] = (
        report["canonical_source_checksum"] == str(sealed["freeze"]["canonical_source_checksum"])
    )
    write_json(args.json_output, report)
    args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_output.write_text(markdown_summary(report), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report["same_config_rerun_diff_zero"] and report["human_rejected_semantic_strict_labels"] == 0 else 2


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="S05P4 independent human semantic review workflow")
    sub = result.add_subparsers(dest="command", required=True)

    generate = sub.add_parser("generate", help="freeze inputs and release sanitized set A only")
    generate.add_argument("--canonical-db", type=Path, default=DEFAULT_CANONICAL)
    generate.add_argument("--clone-db", type=Path, default=DEFAULT_CLONE)
    generate.add_argument("--frozen-clone-db", type=Path, default=DEFAULT_FROZEN_CLONE)
    generate.add_argument("--blind-pack", type=Path, default=DEFAULT_BLIND)
    generate.add_argument("--closeout", type=Path, default=DEFAULT_CLOSEOUT)
    generate.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    generate.add_argument("--public-dir", type=Path, default=DEFAULT_PUBLIC)
    generate.add_argument("--sealed-key", type=Path, default=DEFAULT_SEALED)
    generate.add_argument("--manifest-json", type=Path, default=DEFAULT_MANIFEST_JSON)
    generate.add_argument("--manifest-md", type=Path, default=DEFAULT_MANIFEST_MD)
    generate.add_argument("--protocol-json", type=Path, default=DEFAULT_PROTOCOL_JSON)
    generate.add_argument("--protocol-md", type=Path, default=DEFAULT_PROTOCOL_MD)
    generate.set_defaults(func=command_generate)

    freeze_protocol = sub.add_parser(
        "freeze-protocol",
        help="freeze codebook, metrics, and checksums without changing an existing unreviewed A/B split",
    )
    freeze_protocol.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    freeze_protocol.add_argument("--public-dir", type=Path, default=DEFAULT_PUBLIC)
    freeze_protocol.add_argument("--sealed-key", type=Path, default=DEFAULT_SEALED)
    freeze_protocol.add_argument("--manifest-json", type=Path, default=DEFAULT_MANIFEST_JSON)
    freeze_protocol.add_argument("--manifest-md", type=Path, default=DEFAULT_MANIFEST_MD)
    freeze_protocol.add_argument("--protocol-json", type=Path, default=DEFAULT_PROTOCOL_JSON)
    freeze_protocol.add_argument("--protocol-md", type=Path, default=DEFAULT_PROTOCOL_MD)
    freeze_protocol.set_defaults(func=command_freeze_protocol)

    validate = sub.add_parser("validate", help="validate review format and logic without opening the sealed key")
    validate.add_argument("--review-input", type=Path, required=True)
    validate.add_argument("--review-output", type=Path, action="append", required=True)
    validate.add_argument("--json-output", type=Path, required=True)
    validate.add_argument("--markdown-output", type=Path, required=True)
    validate.set_defaults(func=command_validate)

    compare = sub.add_parser("compare", help="compare completed human reviews to the sealed model key")
    compare.add_argument("--sealed-key", type=Path, default=DEFAULT_SEALED)
    compare.add_argument("--canonical-db", type=Path, default=DEFAULT_CANONICAL)
    compare.add_argument("--phase", choices=("A", "B", "full"), required=True)
    compare.add_argument("--review-output", type=Path, action="append", required=True)
    compare.add_argument("--validation-report", type=Path, required=True)
    compare.add_argument("--json-output", type=Path, required=True)
    compare.add_argument("--markdown-output", type=Path, required=True)
    compare.set_defaults(func=command_compare)

    release = sub.add_parser("release-b", help="release lockbox B after set A passes")
    release.add_argument("--sealed-key", type=Path, default=DEFAULT_SEALED)
    release.add_argument("--set-a-report", type=Path, required=True)
    release.add_argument("--public-dir", type=Path, default=DEFAULT_PUBLIC)
    release.add_argument("--protocol-json", type=Path, default=DEFAULT_PROTOCOL_JSON)
    release.add_argument("--codebook", type=Path, default=DEFAULT_PUBLIC / "REVIEW_CODEBOOK.md")
    release.add_argument("--json-output", type=Path, default=ROOT / "artifacts" / "audit" / "s05p4_lockbox_b_release.json")
    release.set_defaults(func=command_release_b)

    apply_cmd = sub.add_parser("apply", help="write separate human/final eligibility layers to the clone only")
    apply_cmd.add_argument("--sealed-key", type=Path, default=DEFAULT_SEALED)
    apply_cmd.add_argument("--comparison-report", type=Path, required=True)
    apply_cmd.add_argument("--clone-db", type=Path, default=DEFAULT_CLONE)
    apply_cmd.add_argument("--canonical-db", type=Path, default=DEFAULT_CANONICAL)
    apply_cmd.add_argument("--json-output", type=Path, required=True)
    apply_cmd.add_argument("--markdown-output", type=Path, required=True)
    apply_cmd.set_defaults(func=command_apply)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
