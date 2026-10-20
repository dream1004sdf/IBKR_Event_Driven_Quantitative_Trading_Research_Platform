from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from recorder.s05_reaction_label_pilot import (
    PILOT_VERSION,
    audit_pilot,
    build_pilot_labels,
    canonical_counts,
    markdown,
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_value(*args: str) -> str:
    result = subprocess.run(["git", *args], check=False, capture_output=True, text=True)
    return result.stdout.strip()


def clone_sqlite(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    with sqlite3.connect(source) as source_conn, sqlite3.connect(target) as target_conn:
        source_conn.backup(target_conn)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run clone-only S05/M253 macro reaction-label pilot")
    parser.add_argument(
        "--source-db",
        type=Path,
        default=Path("work/canonical_research_backfill/m254_canonical_20250101_20260706_v1/canonical_market_events.db"),
    )
    parser.add_argument(
        "--cache-db",
        type=Path,
        default=Path("work/s04_market_data_closeout/ibkr_live_20250101_20260706/ibkr_historical_cache.db"),
    )
    parser.add_argument(
        "--supplemental-audit",
        type=Path,
        default=Path("artifacts/audit/s04_market_data_pre_s05_audit.json"),
    )
    parser.add_argument(
        "--corporate-actions",
        type=Path,
        default=Path("artifacts/audit/s04_market_data_corporate_actions_snapshot.json"),
    )
    parser.add_argument(
        "--clone-db",
        type=Path,
        default=Path("work/s05_reaction_label_pilot/macro_s04_pass_v1/canonical_market_events_pilot.db"),
    )
    parser.add_argument(
        "--json-output",
        type=Path,
        default=Path("artifacts/audit/s05_m253_macro_reaction_label_pilot.json"),
    )
    parser.add_argument(
        "--markdown-output",
        type=Path,
        default=Path("artifacts/audit/s05_m253_macro_reaction_label_pilot.md"),
    )
    parser.add_argument(
        "--manifest-output",
        type=Path,
        default=Path("artifacts/audit/s04_to_s05_pilot_reproducibility_manifest.json"),
    )
    parser.add_argument("--max-trade-delay-seconds", type=int, default=300)
    args = parser.parse_args()

    source_hash_before = sha256(args.source_db)
    clone_sqlite(args.source_db, args.clone_db)
    source_hash_after_clone = sha256(args.source_db)
    if source_hash_before != source_hash_after_clone:
        raise RuntimeError("canonical source DB changed while creating pilot clone")

    first_build = build_pilot_labels(
        args.clone_db,
        args.cache_db,
        args.supplemental_audit,
        args.corporate_actions,
        max_trade_delay_seconds=args.max_trade_delay_seconds,
    )
    first_counts = canonical_counts(args.clone_db)
    second_build = build_pilot_labels(
        args.clone_db,
        args.cache_db,
        args.supplemental_audit,
        args.corporate_actions,
        max_trade_delay_seconds=args.max_trade_delay_seconds,
    )
    second_counts = canonical_counts(args.clone_db)
    pilot_audit = audit_pilot(
        args.clone_db,
        first_counts,
        second_counts,
        supplemental_audit_path=args.supplemental_audit,
        corporate_actions_path=args.corporate_actions,
    )
    source_hash_final = sha256(args.source_db)
    source_unchanged = source_hash_before == source_hash_final
    if not source_unchanged:
        pilot_audit["checks"]["canonical_source_db_unchanged"] = False
        pilot_audit["verdict"] = "FAIL"
    else:
        pilot_audit["checks"]["canonical_source_db_unchanged"] = True

    status = git_value("status", "--porcelain=v1")
    report = {
        "pilot_version": PILOT_VERSION,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "verdict": pilot_audit["verdict"],
        "scope": "112 canonical macro events only; audited S04 event symbols; decision lags [0,60,180] for intraday and lag 0 for daily; no full news-cluster S05",
        "source_database": str(args.source_db),
        "clone_database": str(args.clone_db),
        "market_data_cache": str(args.cache_db),
        "source_db_unchanged": source_unchanged,
        "first_build": first_build,
        "second_build": second_build,
        "pilot_audit": pilot_audit,
        "safety": {
            "full_s05_executed": False,
            "m255_m256_executed": False,
            "broker_orders": False,
            "broker_what_if": False,
            "transmit": False,
            "live_routing": False,
            "threshold_promotion": False,
        },
    }
    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    args.markdown_output.write_text(markdown(report), encoding="utf-8")

    manifest = {
        "checkpoint_version": "s04-to-s05-pilot-manifest-v1",
        "generated_at_utc": report["generated_at_utc"],
        "git_head": git_value("rev-parse", "HEAD"),
        "git_worktree_clean": not bool(status),
        "git_status_sha256": hashlib.sha256(status.encode("utf-8")).hexdigest(),
        "note": (
            "Pre-existing dirty worktree: no misleading Git tag was created. "
            "This content-addressed manifest pins the exact pilot inputs and outputs."
        ),
        "files": {
            "canonical_source_db": {"path": str(args.source_db), "sha256": source_hash_final},
            "market_data_cache": {"path": str(args.cache_db), "sha256": sha256(args.cache_db)},
            "pilot_clone_db": {"path": str(args.clone_db), "sha256": sha256(args.clone_db)},
            "s04_supplemental_audit": {"path": str(args.supplemental_audit), "sha256": sha256(args.supplemental_audit)},
            "corporate_actions_snapshot": {"path": str(args.corporate_actions), "sha256": sha256(args.corporate_actions)},
            "pilot_report": {"path": str(args.json_output), "sha256": sha256(args.json_output)},
            "reaction_label_schema_source": {
                "path": "recorder/news_lineage_v2.py",
                "sha256": sha256(Path("recorder/news_lineage_v2.py")),
            },
            "pilot_builder_source": {
                "path": "recorder/s05_reaction_label_pilot.py",
                "sha256": sha256(Path("recorder/s05_reaction_label_pilot.py")),
            },
            "pilot_runner_source": {
                "path": "scripts/run_s05_reaction_label_pilot.py",
                "sha256": sha256(Path("scripts/run_s05_reaction_label_pilot.py")),
            },
        },
        "canonical_counts": second_counts,
        "s04_market_data_counts": {
            "bar_count": 873602,
            "rth_count": 353400,
            "eth_count": 513290,
            "daily_count": 6912,
        },
        "pilot_config_hash": first_build["config_hash"],
        "price_quality_version": first_build["price_quality"]["version"],
        "corporate_action_version": first_build["corporate_action_version"],
    }
    args.manifest_output.parent.mkdir(parents=True, exist_ok=True)
    args.manifest_output.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    print(
        json.dumps(
            {
                "verdict": report["verdict"],
                "clone_database": str(args.clone_db),
                "counts": pilot_audit["counts"],
                "excluded_reasons": pilot_audit["excluded_label_reasons"],
                "rerun_diff": pilot_audit["clone_rerun_count_diff"],
                "source_db_unchanged": source_unchanged,
                "report": str(args.json_output),
                "manifest": str(args.manifest_output),
            },
            indent=2,
        )
    )
    return 0 if report["verdict"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
