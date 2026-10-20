from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from recorder.s05_reaction_label_pilot import (
    PILOT_VERSION,
    audit_pilot,
    build_pilot_labels,
    canonical_counts,
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sqlite_backup(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source) as source_conn, sqlite3.connect(target) as target_conn:
        source_conn.backup(target_conn)


def content_hash(path: Path, table: str, excluded: set[str]) -> str:
    digest = hashlib.sha256()
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        columns = [str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})") if str(row[1]) not in excluded]
        order = ",".join(columns)
        for row in conn.execute(f"SELECT {order} FROM {table} ORDER BY {order}"):
            payload = [row[column] for column in columns]
            digest.update(json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8"))
            digest.update(b"\n")
    return digest.hexdigest()


def canonical_hashes(path: Path) -> dict[str, str]:
    return {
        "reaction_universe_plans": content_hash(path, "reaction_universe_plans", {"reaction_plan_id", "created_at"}),
        "reaction_universe_assets": content_hash(path, "reaction_universe_assets", {"id", "created_at"}),
        "event_reaction_labels": content_hash(path, "event_reaction_labels", {"reaction_label_id", "created_at"}),
        "s05_market_data_price_quality_flags": content_hash(
            path,
            "s05_market_data_price_quality_flags",
            {"details_json"},
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Promote the passing clone-only macro S05 labels to canonical DB")
    parser.add_argument(
        "--canonical-db",
        type=Path,
        default=Path("work/canonical_research_backfill/m254_canonical_20250101_20260706_v1/canonical_market_events.db"),
    )
    parser.add_argument(
        "--clone-db",
        type=Path,
        default=Path("work/s05_reaction_label_pilot/macro_s04_pass_v1/canonical_market_events_pilot.db"),
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
        "--pilot-report",
        type=Path,
        default=Path("artifacts/audit/s05_m253_macro_reaction_label_pilot.json"),
    )
    parser.add_argument(
        "--pilot-manifest",
        type=Path,
        default=Path("artifacts/audit/s04_to_s05_pilot_reproducibility_manifest.json"),
    )
    parser.add_argument(
        "--json-output",
        type=Path,
        default=Path("artifacts/audit/s05_m253_macro_canonical_promotion.json"),
    )
    parser.add_argument(
        "--markdown-output",
        type=Path,
        default=Path("artifacts/audit/s05_m253_macro_canonical_promotion.md"),
    )
    args = parser.parse_args()

    pilot_report = json.loads(args.pilot_report.read_text(encoding="utf-8"))
    manifest = json.loads(args.pilot_manifest.read_text(encoding="utf-8"))
    if pilot_report.get("verdict") != "PASS" or not all(pilot_report["pilot_audit"]["checks"].values()):
        raise RuntimeError("macro pilot is not fully PASS; canonical promotion refused")
    expected_original_hash = str(manifest["files"]["canonical_source_db"]["sha256"])
    original_hash = sha256(args.canonical_db)
    if original_hash != expected_original_hash:
        raise RuntimeError("canonical DB changed since clone pilot; promotion refused")
    with sqlite3.connect(args.canonical_db) as conn:
        before_counts = {
            table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("reaction_universe_plans", "reaction_universe_assets", "event_reaction_labels")
        }
        stage_rows_before = int(
            conn.execute(
                "SELECT COUNT(*) FROM research_backfill_stage_runs WHERE stage_name LIKE 'S05%' OR stage_name LIKE '%M253%'"
            ).fetchone()[0]
        )
    if any(before_counts.values()) or stage_rows_before:
        raise RuntimeError(f"canonical macro target is not empty/clean: counts={before_counts}, stage_rows={stage_rows_before}")

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = args.canonical_db.parent / "backups" / f"before_s05_macro_canonical_promotion_{timestamp}_{args.canonical_db.name}"
    sqlite_backup(args.canonical_db, backup)
    clone_hashes = canonical_hashes(args.clone_db)
    sqlite_backup(args.clone_db, args.canonical_db)

    first_build = build_pilot_labels(
        args.canonical_db,
        args.cache_db,
        args.supplemental_audit,
        args.corporate_actions,
    )
    first_counts = canonical_counts(args.canonical_db)
    second_build = build_pilot_labels(
        args.canonical_db,
        args.cache_db,
        args.supplemental_audit,
        args.corporate_actions,
    )
    second_counts = canonical_counts(args.canonical_db)
    audit = audit_pilot(
        args.canonical_db,
        first_counts,
        second_counts,
        supplemental_audit_path=args.supplemental_audit,
        corporate_actions_path=args.corporate_actions,
    )
    canonical_content_hashes = canonical_hashes(args.canonical_db)
    content_matches_clone = canonical_content_hashes == clone_hashes
    with sqlite3.connect(args.canonical_db) as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS s05_macro_label_promotions (
                   promotion_version TEXT PRIMARY KEY,
                   promoted_at_utc TEXT NOT NULL,
                   source_pilot_version TEXT NOT NULL,
                   backup_path TEXT NOT NULL,
                   pilot_report_sha256 TEXT NOT NULL,
                   canonical_content_hash_json TEXT NOT NULL
               )"""
        )
        conn.execute(
            """INSERT INTO s05_macro_label_promotions VALUES(?,?,?,?,?,?)
               ON CONFLICT(promotion_version) DO UPDATE SET
                   promoted_at_utc=excluded.promoted_at_utc,
                   backup_path=excluded.backup_path,
                   pilot_report_sha256=excluded.pilot_report_sha256,
                   canonical_content_hash_json=excluded.canonical_content_hash_json""",
            (
                "s05-m253-macro-canonical-promotion-v1",
                datetime.now(UTC).isoformat(),
                PILOT_VERSION,
                str(backup),
                sha256(args.pilot_report),
                json.dumps(canonical_content_hashes, sort_keys=True),
            ),
        )
        conn.commit()
        stage_rows_after = int(
            conn.execute(
                "SELECT COUNT(*) FROM research_backfill_stage_runs WHERE stage_name LIKE 'S05%' OR stage_name LIKE '%M253%'"
            ).fetchone()[0]
        )
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = len(conn.execute("PRAGMA foreign_key_check").fetchall())
    checks = {
        "pilot_audit_pass": audit["verdict"] == "PASS" and all(audit["checks"].values()),
        "canonical_content_matches_clone": content_matches_clone,
        "canonical_rerun_diff_zero": all(value == 0 for value in audit["clone_rerun_count_diff"].values()),
        "stage_runner_rows_remain_zero": stage_rows_after == 0,
        "integrity_ok": integrity == "ok" and foreign_keys == 0,
        "backup_exists": backup.exists(),
    }
    report: dict[str, Any] = {
        "promotion_version": "s05-m253-macro-canonical-promotion-v1",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "verdict": "PASS" if all(checks.values()) else "FAIL",
        "checks": checks,
        "canonical_database": str(args.canonical_db),
        "backup_database": str(backup),
        "original_sha256": original_hash,
        "promoted_sha256": sha256(args.canonical_db),
        "before_counts": before_counts,
        "after_counts": second_counts,
        "clone_content_hashes": clone_hashes,
        "canonical_content_hashes": canonical_content_hashes,
        "first_build": first_build,
        "second_build": second_build,
        "canonical_audit": audit,
        "stage_rows_before": stage_rows_before,
        "stage_rows_after": stage_rows_after,
        "safety": {
            "full_news_s05": False,
            "m254_m255_m256": False,
            "broker_orders": False,
            "transmit": False,
            "live_routing": False,
            "threshold_promotion": False,
        },
    }
    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    lines = [
        "# S05/M253 Macro Canonical Promotion",
        "",
        f"- Verdict: **{report['verdict']}**",
        f"- Canonical DB: `{args.canonical_db}`",
        f"- Backup: `{backup}`",
        f"- Plans/assets/labels: `{second_counts['reaction_universe_plans']}` / `{second_counts['reaction_universe_assets']}` / `{second_counts['event_reaction_labels']}`",
        f"- Strict labels: `{audit['counts']['strict_training_eligible']}`",
        f"- Idempotent rerun: `{'yes' if checks['canonical_rerun_diff_zero'] else 'no'}`",
        f"- Clone content match: `{'yes' if content_matches_clone else 'no'}`",
        f"- Full news S05 or M254+: `not run`",
        "",
        "## Checks",
        "",
    ]
    lines.extend(f"- {key}: `{'PASS' if value else 'FAIL'}`" for key, value in checks.items())
    args.markdown_output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"verdict": report["verdict"], "checks": checks, "after_counts": second_counts, "backup": str(backup), "report": str(args.json_output)}, indent=2))
    return 0 if report["verdict"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
