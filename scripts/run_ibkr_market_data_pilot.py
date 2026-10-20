from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from recorder.ibkr_market_data_pilot import audit_cache, audit_markdown, cache_snapshot, connect_cache, ensure_cache_schema, run_collection


def main() -> int:
    parser = argparse.ArgumentParser(description="Run an isolated read-only IBKR historical market-data pilot")
    parser.add_argument("--config", type=Path, default=Path("config/backfill/ibkr_market_data_pilot_202603_paper.json"))
    parser.add_argument("--no-resume-check", action="store_true")
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    cache_path = Path(config["paths"]["cache_database"])
    source_path = Path(config["paths"]["source_database"])
    source_before = {"size": source_path.stat().st_size, "mtime_ns": source_path.stat().st_mtime_ns}
    first = run_collection(config, resume=True)
    repair_runs = []
    repair_diffs = []
    repair_passes = max(0, int(config.get("runtime", {}).get("repair_passes", 2)))
    for pass_index in range(1, repair_passes + 1):
        with connect_cache(cache_path) as conn:
            ensure_cache_schema(conn)
            before_repair = cache_snapshot(conn)
            incomplete = int(
                conn.execute("SELECT COUNT(1) FROM ibkr_bar_shards WHERE status<>'completed'").fetchone()[0]
            )
        if incomplete == 0:
            break
        repair = run_collection(config, resume=True)
        with connect_cache(cache_path) as conn:
            ensure_cache_schema(conn)
            after_repair = cache_snapshot(conn)
        repair_runs.append({"pass_index": pass_index, "incomplete_before": incomplete, "result": repair})
        repair_diffs.append(
            {
                "pass_index": pass_index,
                "diff": {key: after_repair[key] - before_repair[key] for key in before_repair},
            }
        )
    with connect_cache(cache_path) as conn:
        ensure_cache_schema(conn)
        before_resume = cache_snapshot(conn)
    second = None
    if not args.no_resume_check:
        second = run_collection(config, resume=True)
    with connect_cache(cache_path) as conn:
        ensure_cache_schema(conn)
        after_resume = cache_snapshot(conn)
    diff = {key: after_resume[key] - before_resume[key] for key in before_resume}
    report = audit_cache(config, idempotency_diff=diff)
    source_after = {"size": source_path.stat().st_size, "mtime_ns": source_path.stat().st_mtime_ns}
    report["source_database_unchanged"] = source_before == source_after
    report["first_run"] = first
    report["repair_runs"] = repair_runs
    report["repair_diffs"] = repair_diffs
    report["resume_run"] = second
    if not report["source_database_unchanged"]:
        report["verdict"] = "FAIL"
        report["pass_conditions"]["source_database_unchanged"] = False
    else:
        report["pass_conditions"]["source_database_unchanged"] = True
    json_path = Path(config["paths"]["audit_json"])
    md_path = Path(config["paths"]["audit_markdown"])
    json_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    md_path.write_text(audit_markdown(report), encoding="utf-8")
    print(json.dumps({"verdict": report["verdict"], "audit_json": str(json_path), "audit_markdown": str(md_path), "snapshot": report["snapshot"], "shard_status": report["shard_status_counts"], "errors": first["errors"]}, indent=2, ensure_ascii=False))
    return 0 if report["verdict"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
