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

from recorder.s05_news_semantic_closeout import (
    audit_semantic_closeout,
    build_time_stratified_selection,
    materialize_closeout,
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sqlite_clone(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    with sqlite3.connect(source) as source_conn, sqlite3.connect(target) as target_conn:
        source_conn.backup(target_conn)


def counts(path: Path) -> dict[str, int]:
    with sqlite3.connect(path) as conn:
        return {
            table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("s05p1_news_cluster_snapshots", "s05p1_news_entity_roles")
        }


def markdown(report: dict[str, Any]) -> str:
    audit = report["audit"]
    lines = [
        "# S05P1 News Semantic/Causality/Calendar/Core Closeout",
        "",
        f"- Verdict: **{report['verdict']}**",
        "- Macro canonical promotion: **preserved**",
        f"- Selected clusters: **{report['selection']['selected_count']}**",
        f"- Periods: `{json.dumps(report['selection']['periods'], sort_keys=True)}`",
        f"- Historical BroadTape coverage gap: **{str(audit['broadtape']['coverage_gap_broadtape_historical']).lower()}**",
        "",
        "## Hard gates",
        "",
    ]
    lines.extend(f"- {key}: **{'PASS' if value else 'FAIL'}**" for key, value in audit["checks"].items())
    lines += [
        "",
        "## Coverage",
        "",
        f"- Provider: `{json.dumps(audit['counts']['provider'], sort_keys=True)}`",
        f"- Session: `{json.dumps(audit['counts']['session'], sort_keys=True)}`",
        f"- Content type: `{json.dumps(audit['counts']['content_type'], sort_keys=True)}`",
        f"- Event class: `{json.dumps(audit['counts']['event_class'], sort_keys=True)}`",
        f"- Direct-role precision sample: **{audit['direct_precision']['correct']}/{audit['direct_precision']['sample_size']} = {audit['direct_precision']['measured']:.3f}**; preregistered threshold `{audit['direct_precision']['threshold_preregistered']:.2f}`",
        f"- Future article/entity/classification inputs: `{json.dumps(audit['future_inputs'], sort_keys=True)}`",
        f"- Historical BroadTape raw/routes/no-direct: **{audit['broadtape']['raw_route_count']} / {audit['broadtape']['cluster_link_count']} / {audit['broadtape']['no_direct_entity_count']}**",
        "",
        "Historical BroadTape is an explicit coverage gap, not a fabricated PASS. A separate read-only live-forward probe is required.",
        "",
        "Full canonical news S05 and M254/M255/M256 remain disabled.",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run clone-only S05P1 news semantic closeout")
    parser.add_argument("--canonical-db", type=Path, default=Path("work/canonical_research_backfill/m254_canonical_20250101_20260706_v1/canonical_market_events.db"))
    parser.add_argument("--clone-db", type=Path, default=Path("work/s05p1_news_semantic_closeout/time_stratified_300_v1/canonical_market_events_s05p1_clone.db"))
    parser.add_argument("--selection-output", type=Path, default=Path("artifacts/audit/s05p1_news_time_stratified_selection.json"))
    parser.add_argument("--json-output", type=Path, default=Path("artifacts/audit/s05p1_news_semantic_closeout.json"))
    parser.add_argument("--markdown-output", type=Path, default=Path("artifacts/audit/s05p1_news_semantic_closeout.md"))
    parser.add_argument("--per-period", type=int, default=100)
    args = parser.parse_args()

    source_sha_before = sha256(args.canonical_db)
    sqlite_clone(args.canonical_db, args.clone_db)
    selection = build_time_stratified_selection(args.clone_db, per_period=args.per_period)
    first = materialize_closeout(args.clone_db, selection)
    first_counts = counts(args.clone_db)
    second = materialize_closeout(args.clone_db, first["selection"])
    second_counts = counts(args.clone_db)
    rerun_diff = {table: second_counts[table] - first_counts[table] for table in first_counts}
    audit = audit_semantic_closeout(args.clone_db, second["selection"], second["direct_rows"])
    audit["checks"]["clone_rerun_count_diff_zero"] = all(value == 0 for value in rerun_diff.values())
    audit["checks"]["canonical_source_unchanged"] = sha256(args.canonical_db) == source_sha_before
    hard_pass = all(audit["checks"].values())
    verdict = "PASS_WITH_COVERAGE_GAP" if hard_pass and audit["broadtape"]["coverage_gap_broadtape_historical"] else ("PASS" if hard_pass else "FAIL")
    report = {
        "version": "s05p1-news-semantic-closeout-v1",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "verdict": verdict,
        "canonical_database": str(args.canonical_db),
        "clone_database": str(args.clone_db),
        "canonical_sha256": source_sha_before,
        "selection": {key: second["selection"][key] for key in ("version","canonical_timezone","canonical_cutoff","selected_count","periods","counts","semantic_contract")},
        "audit": audit,
        "rerun_diff": rerun_diff,
        "safety": {"macro_canonical_modified": False, "full_news_canonical_promoted": False,
                   "m254_m255_m256": False, "orders": False, "transmit": False,
                   "live_routing": False, "threshold_promotion": False},
    }
    args.selection_output.parent.mkdir(parents=True, exist_ok=True)
    args.selection_output.write_text(json.dumps(second["selection"], indent=2, ensure_ascii=False), encoding="utf-8")
    args.json_output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    args.markdown_output.write_text(markdown(report), encoding="utf-8")
    print(json.dumps({"verdict": verdict, "selected": second["selection"]["selected_count"],
                      "periods": second["selection"]["periods"], "counts": audit["counts"],
                      "checks": audit["checks"], "broadtape": audit["broadtape"],
                      "rerun_diff": rerun_diff, "report": str(args.json_output)}, indent=2, ensure_ascii=False))
    return 0 if hard_pass else 2


if __name__ == "__main__":
    raise SystemExit(main())
