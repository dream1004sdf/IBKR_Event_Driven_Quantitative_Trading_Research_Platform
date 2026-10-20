from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.run_s05_news_stratified_pilot as base
from recorder.ibkr_market_data_pilot import BarShard, _parse_utc, _persist_bars, _persist_contract
from recorder.market_outcomes import IBKRHistoricalIntradayPriceSource
from scripts.run_s05p1_news_label_pilot import (
    classify_no_trade_shard,
    ensure_no_trade_classification_schema,
    persist_no_trade_classification,
)


DEFAULT_SHARD_KEY = "9cfb877bbc982a7b74e39c8533eb09eac5a6e93ef6ed0975d2c00d20188e2cd8"


def _backup(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
        src.backup(dst)


def _source(host: str, port: int, client_id: int, what_to_show: str) -> IBKRHistoricalIntradayPriceSource:
    return IBKRHistoricalIntradayPriceSource(
        host=host,
        port=port,
        client_id=client_id,
        timeout_seconds=60,
        what_to_show=what_to_show,
        bar_size="1 min",
        use_rth=False,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Repair/classify one S05P1 HMDS no-data shard without substituting MIDPOINT into returns"
    )
    parser.add_argument(
        "--cache-db",
        type=Path,
        default=Path("work/s05p1_news_label_pilot/time_stratified_300_v1/ibkr_s05p1_news_cache.db"),
    )
    parser.add_argument(
        "--clone-db",
        type=Path,
        default=Path(
            "work/s05p1_news_label_pilot/time_stratified_300_v1/canonical_market_events_s05p1_label_clone.db"
        ),
    )
    parser.add_argument("--shard-key", default=DEFAULT_SHARD_KEY)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4001)
    parser.add_argument("--client-id", type=int, default=9360)
    parser.add_argument("--expanded-start-utc", default="2025-01-15T09:00:00+00:00")
    parser.add_argument("--expanded-end-utc", default="2025-01-15T16:30:00+00:00")
    parser.add_argument(
        "--json-output", type=Path, default=Path("artifacts/audit/s05p1_uup_no_trade_repair.json")
    )
    parser.add_argument(
        "--markdown-output", type=Path, default=Path("artifacts/audit/s05p1_uup_no_trade_repair.md")
    )
    args = parser.parse_args()

    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    backup = args.cache_db.parent / "backups" / f"before_no_trade_repair_{timestamp}_{args.cache_db.name}"
    _backup(args.cache_db, backup)

    with base.connect_cache(args.cache_db) as conn:
        ensure_no_trade_classification_schema(conn)
        row = conn.execute("SELECT * FROM ibkr_bar_shards WHERE shard_key=?", (args.shard_key,)).fetchone()
        if row is None:
            raise RuntimeError(f"shard not found: {args.shard_key}")
        shard = BarShard(
            str(row["shard_key"]),
            str(row["data_kind"]),
            str(row["symbol"]),
            _parse_utc(str(row["start_at_utc"])),
            _parse_utc(str(row["end_at_utc"])),
            str(row["bar_size"]),
            str(row["what_to_show"]),
            bool(row["use_rth"]),
        )
        midpoint_before = int(
            conn.execute("SELECT COUNT(*) FROM ibkr_historical_bars WHERE what_to_show='MIDPOINT'").fetchone()[0]
        )

        trades = _source(args.host, args.port, args.client_id, "TRADES")
        exact_trade_rows = []
        exact_trade_error = ""
        expanded_trade_rows = []
        try:
            contract = trades.resolve_stock_contract(shard.symbol)
            _persist_contract(conn, shard.symbol, contract)
            try:
                exact_trade_rows = trades.get_intraday_bars(shard.symbol, shard.start_at_utc, shard.end_at_utc)
            except Exception as exc:
                exact_trade_error = f"{type(exc).__name__}: {exc}"
            expanded_trade_rows = trades.get_intraday_bars(
                shard.symbol,
                _parse_utc(args.expanded_start_utc),
                _parse_utc(args.expanded_end_utc),
            )
        finally:
            trades.close()

        midpoint = _source(args.host, args.port, args.client_id + 1, "MIDPOINT")
        try:
            midpoint_rows = midpoint.get_intraday_bars(shard.symbol, shard.start_at_utc, shard.end_at_utc)
        finally:
            midpoint.close()

        if exact_trade_rows:
            inserted = _persist_bars(conn, shard, int(contract.conId), exact_trade_rows)
            conn.execute(
                """UPDATE ibkr_bar_shards SET status='completed',con_id=?,provider_row_count=?,
                          inserted_row_count=inserted_row_count+?,completed_at_utc=?,last_error=NULL
                    WHERE shard_key=?""",
                (int(contract.conId), len(exact_trade_rows), inserted, datetime.now(UTC).isoformat(), shard.shard_key),
            )
            repair_mode = "trade_rows_recovered"
            classification = None
        else:
            classification = classify_no_trade_shard(conn, shard, exact_trade_error)
            if classification["classification"] != "no_trade_or_symbol_specific_candidate":
                raise RuntimeError(f"HMDS no-data could not be classified safely: {classification}")
            persist_no_trade_classification(conn, classification)
            conn.execute(
                """UPDATE ibkr_bar_shards SET status='completed',con_id=?,provider_row_count=0,
                          completed_at_utc=?,last_error=NULL WHERE shard_key=?""",
                (int(contract.conId), datetime.now(UTC).isoformat(), shard.shard_key),
            )
            repair_mode = "explicit_no_trade_classification"
        conn.commit()

        classification_count_before = int(
            conn.execute(
                "SELECT COUNT(*) FROM ibkr_bar_shard_classifications WHERE shard_key=?", (shard.shard_key,)
            ).fetchone()[0]
        )
        if classification is not None:
            persist_no_trade_classification(conn, classification)
        classification_count_after = int(
            conn.execute(
                "SELECT COUNT(*) FROM ibkr_bar_shard_classifications WHERE shard_key=?", (shard.shard_key,)
            ).fetchone()[0]
        )
        midpoint_after = int(
            conn.execute("SELECT COUNT(*) FROM ibkr_historical_bars WHERE what_to_show='MIDPOINT'").fetchone()[0]
        )
        contract_row = dict(conn.execute("SELECT * FROM ibkr_contracts WHERE symbol=?", (shard.symbol,)).fetchone())
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])

    affected_intraday_labels = 0
    affected_intraday_strict = 0
    if args.clone_db.exists():
        with sqlite3.connect(args.clone_db) as conn:
            affected_intraday_labels, affected_intraday_strict = conn.execute(
                """SELECT COUNT(*),COALESCE(SUM(l.strict_training_eligible),0)
                     FROM event_reaction_labels l
                     JOIN reaction_universe_plans p ON p.reaction_plan_id=l.reaction_plan_id
                    WHERE p.plan_version='m253-s05p1-news-semantic-initial-plan-v1'
                      AND l.asset_symbol=?
                      AND l.window IN ('5m','15m','1h','first_regular_close')
                      AND p.available_at_cutoff>=? AND p.available_at_cutoff<=?""",
                (shard.symbol, shard.start_at_utc.isoformat(), shard.end_at_utc.isoformat()),
            ).fetchone()

    checks = {
        "contract_con_id_positive": int(contract_row["con_id"]) > 0,
        "uup_primary_exchange_arca": shard.symbol != "UUP" or contract_row["primary_exchange"] == "ARCA",
        "symbol_history_available_in_expanded_trades": len(expanded_trade_rows) > 0,
        "exact_no_trade_has_quote_diagnostic": bool(exact_trade_rows) or len(midpoint_rows) > 0,
        "exact_no_trade_classified_or_recovered": bool(exact_trade_rows)
        or (classification or {}).get("classification") == "no_trade_or_symbol_specific_candidate",
        "midpoint_not_persisted": midpoint_after == midpoint_before,
        "classification_upsert_idempotent": classification_count_after == classification_count_before,
        "affected_intraday_labels_not_strict": affected_intraday_labels > 0 and affected_intraday_strict == 0,
        "integrity_ok": integrity == "ok",
    }
    report = {
        "version": "s05p1-no-trade-shard-repair-v1",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "verdict": "PASS" if all(checks.values()) else "FAIL",
        "repair_mode": repair_mode,
        "cache_database": str(args.cache_db),
        "backup_database": str(backup),
        "shard": {
            "shard_key": shard.shard_key,
            "symbol": shard.symbol,
            "start_at_utc": shard.start_at_utc.isoformat(),
            "end_at_utc": shard.end_at_utc.isoformat(),
        },
        "contract": contract_row,
        "exact_trades": {"rows": len(exact_trade_rows), "error": exact_trade_error},
        "expanded_trades": {
            "rows": len(expanded_trade_rows),
            "start_at_utc": args.expanded_start_utc,
            "end_at_utc": args.expanded_end_utc,
        },
        "midpoint_diagnostic": {
            "rows": len(midpoint_rows),
            "persisted_rows_before": midpoint_before,
            "persisted_rows_after": midpoint_after,
            "used_for_returns": False,
        },
        "classification": classification,
        "label_audit": {
            "clone_database": str(args.clone_db),
            "affected_intraday_labels": affected_intraday_labels,
            "affected_intraday_strict": affected_intraday_strict,
        },
        "checks": checks,
        "safety": {
            "isolated_cache_only": True,
            "canonical_database_modified": False,
            "midpoint_substituted": False,
            "orders": False,
            "transmit": False,
            "live_routing": False,
            "threshold_promotion": False,
        },
    }
    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    lines = [
        "# S05P1 HMDS No-Trade Shard Repair",
        "",
        f"- Verdict: **{report['verdict']}**",
        f"- Mode: **{repair_mode}**",
        f"- Symbol/shard: **{shard.symbol} / {shard.shard_key}**",
        f"- Exact TRADES rows: **{len(exact_trade_rows)}**",
        f"- Expanded TRADES rows: **{len(expanded_trade_rows)}**",
        f"- MIDPOINT diagnostic rows: **{len(midpoint_rows)}** (not persisted, not used for returns)",
        f"- Classification: **{(classification or {}).get('classification', 'trade_rows_recovered')}**",
        f"- Contract: **conId={contract_row['con_id']} SMART/{contract_row['primary_exchange']}**",
        f"- Affected intraday labels/strict: **{affected_intraday_labels} / {affected_intraday_strict}**",
        "",
        "MIDPOINT is diagnostic evidence only. Realized reaction labels remain TRADES-only.",
        "",
    ]
    args.markdown_output.write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"verdict": report["verdict"], "checks": checks, "report": str(args.json_output)}, indent=2))
    return 0 if report["verdict"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
