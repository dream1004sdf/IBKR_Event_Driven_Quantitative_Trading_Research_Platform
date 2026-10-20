from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Keep the imports explicit below; these helpers preserve the same deterministic
# cache keys and contract lineage as the normal collector.
from recorder.ibkr_market_data_pilot import (
    BarShard,
    _parse_utc,
    _persist_bars,
    _persist_contract,
    connect_cache,
    ensure_cache_schema,
    split_intraday_range,
)
from recorder.market_outcomes import IBKRHistoricalIntradayPriceSource, classify_ibkr_historical_error


def _source(config: dict, client_id: int) -> IBKRHistoricalIntradayPriceSource:
    gateway = config["gateway"]
    runtime = config["runtime"]
    intraday = config["intraday"]
    return IBKRHistoricalIntradayPriceSource(
        host=str(gateway["host"]),
        port=int(gateway["port"]),
        client_id=client_id,
        timeout_seconds=float(runtime["timeout_seconds"]),
        what_to_show=str(intraday["what_to_show"]),
        bar_size=str(intraday["bar_size"]),
        use_rth=bool(intraday["use_rth"]),
    )


def _backup_database(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
        src.backup(dst)


def main() -> int:
    parser = argparse.ArgumentParser(description="Repair incomplete IBKR intraday cache shards using smaller HMDS requests")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--chunk-hours", type=float, default=12.0)
    parser.add_argument("--client-id", type=int, default=1394)
    parser.add_argument("--force-covering-utc", default="")
    parser.add_argument("--force-symbols", default="")
    parser.add_argument("--json-output", type=Path, default=Path("artifacts/audit/s04_ibkr_market_data_split_repair.json"))
    parser.add_argument("--markdown-output", type=Path, default=Path("artifacts/audit/s04_ibkr_market_data_split_repair.md"))
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    cache_path = Path(config["paths"]["cache_database"])
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    backup_path = cache_path.parent / "backups" / f"before_split_repair_{timestamp}_{cache_path.name}"
    _backup_database(cache_path, backup_path)
    runtime = config["runtime"]
    max_attempts = max(1, int(runtime.get("max_attempts", 3)))
    retry_delay = float(runtime.get("retry_delay_seconds", 10))
    request_pause = float(runtime.get("request_pause_seconds", 1))
    report = {
        "repair_version": "ibkr-market-data-split-shard-repair-v1",
        "cache_database": str(cache_path),
        "backup_database": str(backup_path),
        "chunk_hours": args.chunk_hours,
        "repaired_shards": [],
        "failed_shards": [],
    }
    force_at = _parse_utc(args.force_covering_utc) if args.force_covering_utc else None
    force_symbols = {item.strip().upper() for item in args.force_symbols.split(",") if item.strip()}
    with connect_cache(cache_path) as conn:
        ensure_cache_schema(conn)
        candidates = list(conn.execute("SELECT * FROM ibkr_bar_shards WHERE data_kind='intraday' ORDER BY symbol,start_at_utc"))
        rows = []
        for row in candidates:
            forced = (
                force_at is not None
                and (not force_symbols or str(row["symbol"]).upper() in force_symbols)
                and _parse_utc(str(row["start_at_utc"])) <= force_at <= _parse_utc(str(row["end_at_utc"]))
            )
            if str(row["status"]) != "completed" or forced:
                rows.append(row)
        for row in rows:
            shard = BarShard(
                str(row["shard_key"]),
                "intraday",
                str(row["symbol"]),
                _parse_utc(str(row["start_at_utc"])),
                _parse_utc(str(row["end_at_utc"])),
                str(row["bar_size"]),
                str(row["what_to_show"]),
                bool(row["use_rth"]),
            )
            chunks = split_intraday_range(shard.start_at_utc, shard.end_at_utc, args.chunk_hours)
            source = None
            provider_rows = 0
            inserted_rows = 0
            chunk_results = []
            failure = None
            try:
                for chunk_index, (chunk_start, chunk_end) in enumerate(chunks, start=1):
                    chunk_error = None
                    for attempt in range(1, max_attempts + 1):
                        if source is None:
                            source = _source(config, args.client_id + chunk_index + attempt - 1)
                        try:
                            contract = source.resolve_stock_contract(shard.symbol)
                            con_id = _persist_contract(conn, shard.symbol, contract)
                            bars = source.get_intraday_bars(shard.symbol, chunk_start, chunk_end)
                            inserted = _persist_bars(conn, shard, con_id, bars)
                            provider_rows += len(bars)
                            inserted_rows += inserted
                            chunk_results.append(
                                {
                                    "chunk_index": chunk_index,
                                    "start_at_utc": chunk_start.isoformat(),
                                    "end_at_utc": chunk_end.isoformat(),
                                    "attempt": attempt,
                                    "provider_rows": len(bars),
                                    "inserted_rows": inserted,
                                }
                            )
                            chunk_error = None
                            break
                        except Exception as exc:
                            chunk_error = f"{type(exc).__name__}: {exc}"
                            classification = classify_ibkr_historical_error(
                                chunk_error, symbol=shard.symbol, start=chunk_start
                            )
                            if source is not None:
                                source.close()
                                source = None
                            if not classification.retryable or attempt >= max_attempts:
                                break
                            time.sleep(retry_delay)
                    if chunk_error is not None:
                        failure = {
                            "chunk_index": chunk_index,
                            "start_at_utc": chunk_start.isoformat(),
                            "end_at_utc": chunk_end.isoformat(),
                            "error": chunk_error,
                        }
                        break
                    time.sleep(request_pause)
            finally:
                if source is not None:
                    source.close()
            if failure is None and provider_rows > 0:
                conn.execute(
                    """
                    UPDATE ibkr_bar_shards
                       SET status='completed',provider_row_count=?,inserted_row_count=inserted_row_count+?,
                           last_error=NULL,completed_at_utc=?
                     WHERE shard_key=?
                    """,
                    (provider_rows, inserted_rows, datetime.now(UTC).isoformat(), shard.shard_key),
                )
                conn.commit()
                report["repaired_shards"].append(
                    {
                        "shard_key": shard.shard_key,
                        "symbol": shard.symbol,
                        "provider_rows": provider_rows,
                        "inserted_rows": inserted_rows,
                        "forced_completed_shard": str(row["status"]) == "completed",
                        "chunks": chunk_results,
                    }
                )
            else:
                error_text = (failure or {"error": "split repair returned no data"})["error"]
                conn.execute(
                    "UPDATE ibkr_bar_shards SET status='error',last_error=? WHERE shard_key=?",
                    (error_text, shard.shard_key),
                )
                conn.commit()
                report["failed_shards"].append(
                    {"shard_key": shard.shard_key, "symbol": shard.symbol, "failure": failure, "chunks": chunk_results}
                )
    report["verdict"] = "PASS" if not report["failed_shards"] else "FAIL"
    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    lines = [
        "# IBKR Market-Data Split-Shard Repair",
        "",
        f"- Verdict: **{report['verdict']}**",
        f"- Backup: `{report['backup_database']}`",
        f"- Chunk hours: `{report['chunk_hours']}`",
        f"- Repaired shards: `{len(report['repaired_shards'])}`",
        f"- Failed shards: `{len(report['failed_shards'])}`",
        "",
        "This is a read-only IBKR historical-data repair. It writes only the isolated local cache.",
        "",
    ]
    args.markdown_output.write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"verdict": report["verdict"], "repaired": len(report["repaired_shards"]), "failed": len(report["failed_shards"])}, indent=2))
    return 0 if report["verdict"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
