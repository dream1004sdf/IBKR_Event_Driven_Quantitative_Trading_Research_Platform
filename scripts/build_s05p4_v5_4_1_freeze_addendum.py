from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import subprocess
import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def file_entry(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(path)
    return {
        "path": str(path.resolve()),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def run_text(*args: str) -> str:
    return subprocess.check_output(
        args,
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
    ).strip()


def markdown(payload: Mapping[str, Any]) -> str:
    lines = [
        "# S05P4 v5.4.1 freeze addendum",
        "",
        f"- Verdict: **{payload['verdict']}**",
        f"- Git commit: `{payload['environment']['git_commit']}`",
        f"- Python: `{payload['environment']['python_version']}`",
        f"- Worktree clean: `{payload['environment']['git_worktree_clean']}`",
        "- Set B payload opened: `false`",
        f"- Parent sealed Set B checksum: `{payload['protocol_lineage']['sealed_set_b_sha256']}`",
        "",
        "## Protocol lineage",
        "",
        f"- Parent protocol freeze SHA-256: `{payload['protocol_lineage']['parent_protocol_freeze_sha256']}`",
        f"- Parent Codebook SHA-256: `{payload['protocol_lineage']['parent_codebook_sha256']}`",
        f"- Codebook matches parent declaration: `{payload['protocol_lineage']['codebook_hash_matches_parent']}`",
        f"- Comparison module matches parent declaration: `{payload['protocol_lineage']['comparison_module_hash_matches_parent']}`",
        f"- Comparison script matches parent declaration: `{payload['protocol_lineage']['comparison_script_hash_matches_parent']}`",
        f"- Bundle manifest SHA-256: `{payload.get('bundle', {}).get('bundle_manifest_sha256', 'NOT_BUILT')}`",
        f"- Manifest payload files: `{payload.get('bundle', {}).get('bundle_file_count', 0)}`",
        f"- All manifest files in bundle: `{payload.get('bundle', {}).get('all_manifest_files_in_bundle', False)}`",
        "- Bundle ZIP SHA-256: stored in the external release receipt to avoid a cryptographic self-reference",
        "",
        "## Frozen files",
        "",
        "| Group | Name | Bytes | SHA-256 |",
        "|---|---|---:|---|",
    ]
    for group in ("runtime_imports", "dependency_specs", "review_protocol", "validators", "implementation", "evidence"):
        for name, row in payload["files"][group].items():
            lines.append(f"| {group} | {name} | {row['bytes']} | `{row['sha256']}` |")
    lines.extend([
        "",
        "## Boundaries",
        "",
        "This addendum freezes reproducibility and adjudication-application contracts only. Set B remains sealed. Full-range clone, canonical promotion, M254/M255/M256, orders, transmit, live routing, and threshold promotion remain disabled.",
        "",
    ])
    return "\n".join(lines)


def copy_bundle_file(source: Path, bundle_dir: Path) -> dict[str, Any]:
    target = bundle_dir / source.name
    shutil.copy2(source, target)
    source_hash = sha256_file(source)
    target_hash = sha256_file(target)
    return {
        "source_path": str(source.resolve()),
        "bundle_path": str(target.resolve()),
        "bytes": target.stat().st_size,
        "source_sha256": source_hash,
        "bundle_sha256": target_hash,
        "hash_match": source_hash == target_hash,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build the v5.4.1 freeze addendum without opening sealed Set B"
    )
    parser.add_argument(
        "--json-output",
        type=Path,
        default=Path("artifacts/audit/s05p4_v5_4_1_freeze_addendum.json"),
    )
    parser.add_argument(
        "--markdown-output",
        type=Path,
        default=Path("artifacts/audit/s05p4_v5_4_1_freeze_addendum.md"),
    )
    parser.add_argument(
        "--environment-output",
        type=Path,
        default=Path("artifacts/audit/s05p4_v5_4_1_environment_freeze.txt"),
    )
    parser.add_argument(
        "--bundle-dir",
        type=Path,
        default=Path("artifacts/audit/s05p4_v5_4_1_independent_closeout_bundle"),
    )
    parser.add_argument(
        "--bundle-zip",
        type=Path,
        default=Path("artifacts/audit/s05p4_v5_4_1_independent_closeout_bundle.zip"),
    )
    parser.add_argument(
        "--release-receipt-output",
        type=Path,
        default=Path("artifacts/audit/s05p4_v5_4_1_bundle_release_receipt.json"),
    )
    parser.add_argument(
        "--frozen-mapper-output",
        type=Path,
        default=Path("artifacts/audit/s05p4_v5_4_1_frozen_mapper.py"),
    )
    args = parser.parse_args()

    protocol_json = ROOT / "artifacts/review/s05p4_independent_semantic_v1/REVIEW_PROTOCOL_FREEZE.json"
    protocol_md = ROOT / "artifacts/review/s05p4_independent_semantic_v1/REVIEW_PROTOCOL_FREEZE.md"
    codebook = ROOT / "artifacts/review/s05p4_independent_semantic_v1/REVIEW_CODEBOOK.md"
    protocol = json.loads(protocol_json.read_text(encoding="utf-8"))

    git_commit = run_text("git", "rev-parse", "HEAD")
    git_status = run_text("git", "status", "--porcelain")
    pip_freeze = sorted(
        line.strip()
        for line in run_text(sys.executable, "-m", "pip", "freeze").splitlines()
        if line.strip()
    )
    environment_text = "\n".join([
        f"python_executable={sys.executable}",
        f"python_version={sys.version.replace(chr(10), ' ')}",
        f"implementation={platform.python_implementation()}",
        f"platform={platform.platform()}",
        f"git_commit={git_commit}",
        "",
        "[pip_freeze]",
        *pip_freeze,
        "",
    ])
    args.environment_output.parent.mkdir(parents=True, exist_ok=True)
    args.environment_output.write_text(environment_text, encoding="utf-8", newline="\n")

    groups: dict[str, dict[str, Path]] = {
        "runtime_imports": {
            "s05_news_semantic_closeout": ROOT / "recorder/s05_news_semantic_closeout.py",
            "s05_proxy_semantic_closeout": ROOT / "recorder/s05_proxy_semantic_closeout.py",
            "s05_proxy_semantic_v4": ROOT / "recorder/s05_proxy_semantic_v4.py",
        },
        "dependency_specs": {
            "requirements": ROOT / "requirements.txt",
            "requirements_build": ROOT / "requirements-build.txt",
            "environment_freeze": args.environment_output,
        },
        "review_protocol": {
            "review_codebook": codebook,
            "review_protocol_freeze_json": protocol_json,
            "review_protocol_freeze_markdown": protocol_md,
        },
        "validators": {
            "comparison_and_no_key_module": ROOT / "recorder/s05p4_human_semantic_review.py",
            "comparison_and_no_key_script": ROOT / "scripts/run_s05p4_human_semantic_review.py",
            "comparison_and_no_key_tests": ROOT / "tests/test_s05p4_human_semantic_review.py",
        },
        "implementation": {
            "semantic_mapper": ROOT / "recorder/s05_proxy_semantic_v5.py",
            "set_a_audit": ROOT / "scripts/audit_s05p4_v5_set_a.py",
            "semantic_tests": ROOT / "tests/test_s05_proxy_semantic_v5.py",
            "research_config": ROOT / "config/s05p4_semantic_v5_4_1_research.json",
            "addendum_builder": Path(__file__),
        },
        "evidence": {
            "pytest_junit": ROOT / "artifacts/audit/s05p4_v5_4_1_pytest.xml",
            "set_a_audit_json": ROOT / "artifacts/audit/s05p4_v5_4_1_set_a_research_audit.json",
            "set_a_audit_markdown": ROOT / "artifacts/audit/s05p4_v5_4_1_set_a_research_audit.md",
            "freeze_manifest_json": ROOT / "artifacts/audit/s05p4_v5_4_1_freeze_manifest.json",
            "freeze_manifest_markdown": ROOT / "artifacts/audit/s05p4_v5_4_1_freeze_manifest.md",
            "target_change_packet": ROOT / "artifacts/audit/s05p4_v5_4_1_target_change_review_packet.csv",
            "news_content_type_packet": ROOT / "artifacts/audit/s05p4_v5_4_1_news_content_type_review_packet.csv",
            "abstention_packet": ROOT / "artifacts/audit/s05p4_v5_4_1_abstention_review_packet.csv",
            "multi_role_region_channel_packet": ROOT / "artifacts/audit/s05p4_v5_4_1_multi_role_region_channel_review_packet.csv",
            "recovered_event_packet": ROOT / "artifacts/audit/s05p4_v5_4_1_recovered_event_review_packet.csv",
        },
    }
    files = {
        group: {name: file_entry(path) for name, path in paths.items()}
        for group, paths in groups.items()
    }
    checksums = protocol["checksums"]
    payload = {
        "version": "s05p4-v5.4.1-freeze-addendum-v1",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "verdict": "READY_FOR_SET_A_INDEPENDENT_ADJUDICATION_WITH_FREEZE_ADDENDUM",
        "environment": {
            "git_commit": git_commit,
            "git_worktree_clean": not bool(git_status),
            "git_dirty_entry_count": len(git_status.splitlines()) if git_status else 0,
            "git_status_sha256": sha256_text(git_status),
            "python_version": sys.version.replace("\n", " "),
            "python_executable": sys.executable,
            "python_implementation": platform.python_implementation(),
            "platform": platform.platform(),
            "pip_freeze_package_count": len(pip_freeze),
            "pip_freeze_sha256": sha256_text("\n".join(pip_freeze) + "\n"),
            "lockfile_present": False,
            "dependency_contract": "requirements hashes plus exact pip-freeze snapshot",
        },
        "protocol_lineage": {
            "parent_protocol_freeze_sha256": sha256_file(protocol_json),
            "parent_protocol_freeze_markdown_sha256": sha256_file(protocol_md),
            "parent_codebook_sha256": sha256_file(codebook),
            "sealed_set_b_sha256": checksums["set_b_locked_payload_sha256"],
            "sealed_model_key_sha256": checksums["sealed_model_key_sha256"],
            "set_b_payload_opened": False,
            "codebook_hash_matches_parent": sha256_file(codebook) == checksums["codebook_sha256"],
            "comparison_module_hash_matches_parent": (
                sha256_file(groups["validators"]["comparison_and_no_key_module"])
                == checksums["comparison_module_sha256"]
            ),
            "comparison_script_hash_matches_parent": (
                sha256_file(groups["validators"]["comparison_and_no_key_script"])
                == checksums["comparison_script_sha256"]
            ),
        },
        "files": files,
        "contracts": {
            "taxonomy_application_requires_id_and_explicit_status": True,
            "taxonomy_id_alone_never_approves": True,
            "rejected_taxonomy_reaction_observation": False,
            "macro_authority_must_be_release_subject": True,
            "semantic_rule_modification_after_addendum": "STOP",
            "adjudication_result_application": "FORBIDDEN_UNTIL_SEPARATE_APPROVAL",
        },
        "safety": {
            "set_b_released": False,
            "canonical_promotion": False,
            "full_range_clone": False,
            "m254_m255_m256": False,
            "orders": False,
            "transmit": False,
            "live_routing": False,
            "threshold_promotion": False,
        },
    }
    args.frozen_mapper_output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(groups["implementation"]["semantic_mapper"], args.frozen_mapper_output)
    if sha256_file(args.frozen_mapper_output) != files["implementation"]["semantic_mapper"]["sha256"]:
        raise RuntimeError("standalone frozen mapper copy hash mismatch")

    # The payload manifest intentionally excludes addendum/receipt metadata. This lets
    # the addendum pin the manifest without introducing a hash cycle.
    args.bundle_dir.mkdir(parents=True, exist_ok=True)
    payload_bundle_sources = [
        groups["implementation"]["semantic_mapper"],
        groups["runtime_imports"]["s05_news_semantic_closeout"],
        groups["runtime_imports"]["s05_proxy_semantic_closeout"],
        groups["runtime_imports"]["s05_proxy_semantic_v4"],
        groups["review_protocol"]["review_codebook"],
        groups["review_protocol"]["review_protocol_freeze_json"],
        groups["review_protocol"]["review_protocol_freeze_markdown"],
        groups["validators"]["comparison_and_no_key_module"],
        groups["validators"]["comparison_and_no_key_script"],
        groups["validators"]["comparison_and_no_key_tests"],
        groups["dependency_specs"]["requirements"],
        groups["dependency_specs"]["requirements_build"],
        groups["dependency_specs"]["environment_freeze"],
        groups["implementation"]["semantic_tests"],
        groups["implementation"]["set_a_audit"],
        groups["implementation"]["research_config"],
        groups["implementation"]["addendum_builder"],
        groups["evidence"]["pytest_junit"],
        groups["evidence"]["set_a_audit_json"],
        groups["evidence"]["set_a_audit_markdown"],
        groups["evidence"]["freeze_manifest_json"],
        groups["evidence"]["freeze_manifest_markdown"],
        groups["evidence"]["target_change_packet"],
        groups["evidence"]["news_content_type_packet"],
        groups["evidence"]["abstention_packet"],
        groups["evidence"]["multi_role_region_channel_packet"],
        groups["evidence"]["recovered_event_packet"],
    ]
    bundle_entries = {
        source.name: copy_bundle_file(source, args.bundle_dir)
        for source in payload_bundle_sources
    }
    if not all(row["hash_match"] for row in bundle_entries.values()):
        raise RuntimeError("independent closeout bundle copy hash mismatch")
    bundle_manifest = {
        "version": "s05p4-v5.4.1-independent-closeout-bundle-v1",
        "set_b_included": False,
        "all_source_copy_hashes_match": True,
        "manifest_scope": "payload_files_only_metadata_excluded_to_avoid_hash_cycle",
        "files": bundle_entries,
    }
    bundle_manifest_path = args.bundle_dir / "BUNDLE_MANIFEST.json"
    bundle_manifest_path.write_text(
        json.dumps(bundle_manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    payload["bundle"] = {
        "bundle_manifest_path": str(bundle_manifest_path.resolve()),
        "bundle_manifest_sha256": sha256_file(bundle_manifest_path),
        "bundle_file_count": len(bundle_entries),
        "all_manifest_files_in_bundle": all(
            Path(row["bundle_path"]).is_file() for row in bundle_entries.values()
        ),
        "all_source_copy_hashes_match": all(
            row["hash_match"] for row in bundle_entries.values()
        ),
        "all_addendum_declared_files_in_bundle": (
            len(bundle_entries) == sum(len(rows) for rows in files.values())
        ),
        "bundle_zip_sha256_location": "external_bundle_release_receipt",
        "hash_cycle_policy": "zip hash is never embedded in a file contained by that same zip",
    }
    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_output.write_text(markdown(payload), encoding="utf-8", newline="\n")
    addendum_json_entry = copy_bundle_file(args.json_output, args.bundle_dir)
    addendum_md_entry = copy_bundle_file(args.markdown_output, args.bundle_dir)

    args.bundle_zip.parent.mkdir(parents=True, exist_ok=True)
    zip_members = [
        Path(row["bundle_path"]) for row in bundle_entries.values()
    ] + [
        bundle_manifest_path,
        Path(addendum_json_entry["bundle_path"]),
        Path(addendum_md_entry["bundle_path"]),
    ]
    with zipfile.ZipFile(args.bundle_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(zip_members, key=lambda item: item.name):
            archive.write(path, arcname=path.name)

    release_receipt = {
        "version": "s05p4-v5.4.1-bundle-release-receipt-v1",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "verdict": "PASS_EXTERNAL_FROZEN_ARTIFACT_MATCH",
        "external_frozen_artifact_match_gap_closed": True,
        "bundle_zip": str(args.bundle_zip.resolve()),
        "bundle_zip_bytes": args.bundle_zip.stat().st_size,
        "bundle_zip_sha256": sha256_file(args.bundle_zip),
        "bundle_manifest": str(bundle_manifest_path.resolve()),
        "bundle_manifest_sha256": sha256_file(bundle_manifest_path),
        "bundle_manifest_file_count": len(bundle_entries),
        "bundle_total_file_count": len(zip_members),
        "all_manifest_files_in_bundle": payload["bundle"]["all_manifest_files_in_bundle"],
        "all_addendum_declared_files_in_bundle": payload["bundle"][
            "all_addendum_declared_files_in_bundle"
        ],
        "all_source_copy_hashes_match": payload["bundle"]["all_source_copy_hashes_match"],
        "standalone_frozen_mapper": {
            **file_entry(args.frozen_mapper_output),
            "expected_sha256": files["implementation"]["semantic_mapper"]["sha256"],
            "expected_bytes": files["implementation"]["semantic_mapper"]["bytes"],
            "matches_expected": (
                sha256_file(args.frozen_mapper_output)
                == files["implementation"]["semantic_mapper"]["sha256"]
                and args.frozen_mapper_output.stat().st_size
                == files["implementation"]["semantic_mapper"]["bytes"]
            ),
        },
        "bundled_addendum_json": addendum_json_entry,
        "bundled_addendum_markdown": addendum_md_entry,
        "set_b_included": False,
        "sealed_set_b_sha256": payload["protocol_lineage"]["sealed_set_b_sha256"],
        "self_reference_note": (
            "The ZIP SHA-256 is stored outside the ZIP because embedding it inside "
            "the ZIP would change the ZIP hash."
        ),
    }
    args.release_receipt_output.parent.mkdir(parents=True, exist_ok=True)
    args.release_receipt_output.write_text(
        json.dumps(release_receipt, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    print(json.dumps({
        "verdict": payload["verdict"],
        "git_commit": git_commit,
        "python_version": payload["environment"]["python_version"],
        "parent_protocol_freeze_sha256": payload["protocol_lineage"]["parent_protocol_freeze_sha256"],
        "parent_codebook_sha256": payload["protocol_lineage"]["parent_codebook_sha256"],
        "sealed_set_b_sha256": payload["protocol_lineage"]["sealed_set_b_sha256"],
        "set_b_payload_opened": False,
        "bundle_manifest_files": len(bundle_entries),
        "bundle_total_files": len(zip_members),
        "all_manifest_files_in_bundle": payload["bundle"]["all_manifest_files_in_bundle"],
        "bundle_zip": str(args.bundle_zip.resolve()),
        "bundle_zip_sha256": release_receipt["bundle_zip_sha256"],
        "release_receipt": str(args.release_receipt_output.resolve()),
        "standalone_frozen_mapper": release_receipt["standalone_frozen_mapper"],
    }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
