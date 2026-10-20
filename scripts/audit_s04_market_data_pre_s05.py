from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from recorder.s04_market_data_pre_s05_audit import (
    AUDIT_VERSION,
    build_corporate_action_audit,
    build_eth_audit,
    build_overlap_audit,
    build_provenance_audit,
    fetch_yahoo_corporate_actions,
    markdown,
)


def _s05_rows(source_path: Path) -> int:
    conn = sqlite3.connect(f"file:{source_path.resolve().as_posix()}?mode=ro", uri=True, timeout=60)
    try:
        return int(
            conn.execute(
                "SELECT COUNT(1) FROM research_backfill_stage_runs WHERE stage_name LIKE 'S05%' OR stage_name LIKE '%M253%'"
            ).fetchone()[0]
        )
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit S04 market-data semantics before any S05 pilot")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config/backfill/s04_ibkr_market_data_closeout_20250101_20260706_live.json"),
    )
    parser.add_argument(
        "--json-output", type=Path, default=Path("artifacts/audit/s04_market_data_pre_s05_audit.json")
    )
    parser.add_argument(
        "--markdown-output", type=Path, default=Path("artifacts/audit/s04_market_data_pre_s05_audit.md")
    )
    parser.add_argument(
        "--corporate-actions-output",
        type=Path,
        default=Path("artifacts/audit/s04_market_data_corporate_actions_snapshot.json"),
    )
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    cache_path = Path(config["paths"]["cache_database"])
    source_path = Path(config["paths"]["source_database"])
    eth = build_eth_audit(cache_path)
    eth_checks = {
        "expected_eth_present": eth["counts"]["expected_eth_minutes"] > 0,
        "provider_missing_candidates_zero": eth["counts"]["provider_missing_candidate_timestamps"] == 0,
        "classification_complete": eth["counts"]["missing_trade_minutes"]
        == eth["counts"]["provider_missing_candidate_symbol_minutes"]
        + eth["counts"]["no_trade_or_symbol_specific_candidate_minutes"],
        "per_symbol_present": len(eth["per_symbol"]) == 18,
        "event_window_metrics_present": len(eth["event_windows"]) > 0,
    }
    eth["checks"] = eth_checks
    eth["verdict"] = "PASS" if all(eth_checks.values()) else "FAIL"
    provenance = build_provenance_audit(cache_path)
    symbols = sorted(provenance_row["symbol"] for provenance_row in provenance["profiles"] if provenance_row["data_kind"] == "daily")
    snapshot = fetch_yahoo_corporate_actions(symbols, date(2024, 12, 20), date(2026, 7, 20))
    args.corporate_actions_output.parent.mkdir(parents=True, exist_ok=True)
    args.corporate_actions_output.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False), encoding="utf-8")
    corporate_actions = build_corporate_action_audit(cache_path, snapshot)
    overlap = build_overlap_audit(source_path)
    s05_rows = _s05_rows(source_path)
    components_pass = all(
        component["verdict"] == "PASS" for component in (eth, provenance, corporate_actions, overlap)
    )
    report = {
        "audit_version": AUDIT_VERSION,
        "verdict": "PASS" if components_pass and s05_rows == 0 else "FAIL",
        "s05_pilot_gate": "GO_WITH_REQUIRED_LABEL_FLAGS" if components_pass and s05_rows == 0 else "HOLD",
        "s05_m253_stage_rows": s05_rows,
        "eth": eth,
        "provenance": provenance,
        "corporate_actions": corporate_actions,
        "event_overlap": overlap,
        "required_s05_contract": {
            "daily_horizons": "next 1 and next 5 XNYS sessions, not calendar days",
            "corporate_actions": "persist dividend/split contamination and label_quality_flag or use total-return adjusted prices",
            "overlap": "persist parent_meeting_id/release_bundle_id/subevent_type/overlap_group_id/overlap_weight",
            "intraday_prices": "TRADES only; do not substitute MIDPOINT into realized returns",
        },
    }
    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    args.markdown_output.write_text(markdown(report), encoding="utf-8")
    print(
        json.dumps(
            {
                "verdict": report["verdict"],
                "s05_pilot_gate": report["s05_pilot_gate"],
                "eth_counts": eth["counts"],
                "corporate_action_counts": corporate_actions["counts"],
                "overlap_counts": overlap["counts"],
                "s05_m253_stage_rows": s05_rows,
            },
            indent=2,
        )
    )
    return 0 if report["verdict"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
