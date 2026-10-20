from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from recorder.ibkr_market_data_pilot import (
    BarShard,
    _parse_utc,
    _persist_bars,
    _persist_contract,
    connect_cache,
    ensure_cache_schema,
)
from recorder.market_outcomes import IBKRHistoricalIntradayPriceSource, classify_ibkr_historical_error
from recorder.s04_market_data_pre_s05_audit import build_eth_audit, parse_utc


def _backup(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
        src.backup(dst)


def _runs(values: list[datetime]) -> list[tuple[datetime, datetime]]:
    groups: list[list[datetime]] = []
    for value in sorted(values):
        if not groups or value - groups[-1][-1] > timedelta(minutes=1):
            groups.append([value])
        else:
            groups[-1].append(value)
    return [(group[0], group[-1] + timedelta(minutes=1)) for group in groups]


def _source(config: dict, client_id: int) -> IBKRHistoricalIntradayPriceSource:
    return IBKRHistoricalIntradayPriceSource(
        host=str(config["gateway"]["host"]),
        port=int(config["gateway"]["port"]),
        client_id=client_id,
        timeout_seconds=float(config["runtime"]["timeout_seconds"]),
        what_to_show=str(config["intraday"]["what_to_show"]),
        bar_size=str(config["intraday"]["bar_size"]),
        use_rth=bool(config["intraday"]["use_rth"]),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Repair synchronized ETH provider gaps without filling no-trade minutes")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--client-id", type=int, default=1500)
    parser.add_argument("--json-output", type=Path, default=Path("artifacts/audit/s04_ibkr_market_data_eth_gap_repair.json"))
    parser.add_argument("--markdown-output", type=Path, default=Path("artifacts/audit/s04_ibkr_market_data_eth_gap_repair.md"))
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    cache_path = Path(config["paths"]["cache_database"])
    before = build_eth_audit(cache_path)
    timestamps = [parse_utc(value) for value in before["provider_missing_timestamps"]]
    runs = _runs(timestamps)
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    backup_path = cache_path.parent / "backups" / f"before_eth_gap_repair_{timestamp}_{cache_path.name}"
    _backup(cache_path, backup_path)
    report = {
        "repair_version": "ibkr-market-data-eth-gap-repair-v1",
        "backup_database": str(backup_path),
        "before_counts": before["counts"],
        "runs": [],
        "errors": [],
    }
    max_attempts = max(1, int(config["runtime"].get("max_attempts", 3)))
    retry_delay = float(config["runtime"].get("retry_delay_seconds", 10))
    pause = float(config["runtime"].get("request_pause_seconds", 1))
    source = None
    client_id = args.client_id
    try:
        with connect_cache(cache_path) as conn:
            ensure_cache_schema(conn)
            conn.row_factory = sqlite3.Row
            shards = [dict(row) for row in conn.execute("SELECT * FROM ibkr_bar_shards WHERE data_kind='intraday'")]
            for run_index, (run_start, run_end) in enumerate(runs, start=1):
                run_result = {
                    "run_index": run_index,
                    "start_at_utc": run_start.isoformat(),
                    "end_at_utc": run_end.isoformat(),
                    "symbols": [],
                }
                for symbol, payload in before["per_symbol"].items():
                    symbol_provider_stamps = {
                        parse_utc(value) for value in payload.get("provider_missing_timestamps", [])
                    }
                    if not any(run_start <= stamp < run_end for stamp in symbol_provider_stamps):
                        continue
                    covering = next(
                        (
                            row
                            for row in shards
                            if row["symbol"] == symbol
                            and _parse_utc(row["start_at_utc"]) <= run_start
                            and _parse_utc(row["end_at_utc"]) >= run_end
                        ),
                        None,
                    )
                    if covering is None or payload["provider_missing_candidate_minutes"] <= 0:
                        continue
                    shard = BarShard(
                        covering["shard_key"],
                        "intraday",
                        symbol,
                        _parse_utc(covering["start_at_utc"]),
                        _parse_utc(covering["end_at_utc"]),
                        covering["bar_size"],
                        covering["what_to_show"],
                        bool(covering["use_rth"]),
                    )
                    last_error = None
                    rows = []
                    inserted = 0
                    for attempt in range(1, max_attempts + 1):
                        if source is None:
                            source = _source(config, client_id)
                        try:
                            contract = source.resolve_stock_contract(symbol)
                            con_id = _persist_contract(conn, symbol, contract)
                            rows = source.get_intraday_bars(symbol, run_start, run_end)
                            inserted = _persist_bars(conn, shard, con_id, rows)
                            conn.execute(
                                "UPDATE ibkr_bar_shards SET provider_row_count=provider_row_count+?,inserted_row_count=inserted_row_count+?,attempt_count=attempt_count+1 WHERE shard_key=?",
                                (len(rows), inserted, shard.shard_key),
                            )
                            conn.commit()
                            last_error = None
                            break
                        except Exception as exc:
                            last_error = f"{type(exc).__name__}: {exc}"
                            classification = classify_ibkr_historical_error(last_error, symbol=symbol, start=run_start)
                            if source is not None:
                                source.close()
                                source = None
                            client_id += 1
                            if not classification.retryable or attempt >= max_attempts:
                                break
                            time.sleep(retry_delay)
                    run_result["symbols"].append(
                        {"symbol": symbol, "provider_rows": len(rows), "inserted_rows": inserted, "error": last_error}
                    )
                    if last_error:
                        report["errors"].append({"symbol": symbol, "run_index": run_index, "error": last_error})
                    time.sleep(pause)
                report["runs"].append(run_result)
    finally:
        if source is not None:
            source.close()
    after = build_eth_audit(cache_path)
    report["after_counts"] = after["counts"]
    report["verdict"] = (
        "PASS"
        if not report["errors"] and after["counts"]["provider_missing_candidate_timestamps"] == 0
        else "FAIL"
    )
    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    args.markdown_output.write_text(
        "\n".join(
            [
                "# S04 IBKR ETH Provider-Gap Repair",
                "",
                f"- Verdict: **{report['verdict']}**",
                f"- Backup: `{report['backup_database']}`",
                f"- Before: `{report['before_counts']}`",
                f"- After: `{report['after_counts']}`",
                f"- Errors: `{len(report['errors'])}`",
                "",
            ]
        ),
        encoding="utf-8",
    )
    print(json.dumps({"verdict": report["verdict"], "before": report["before_counts"], "after": report["after_counts"], "errors": report["errors"]}, indent=2))
    return 0 if report["verdict"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
