from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.run_s05_news_stratified_pilot as base
from recorder.market_outcomes import IBKRHistoricalDailyPriceSource, IBKRHistoricalIntradayPriceSource
from recorder.s04_market_data_pre_s05_audit import fetch_yahoo_corporate_actions, parse_utc
from recorder.s05_news_semantic_closeout import (
    CORE_SCOPE_VERSION,
    REQUIRED_CORE_ASSETS,
    ensure_closeout_schema,
    materialize_closeout,
)


PLAN_VERSION = "m253-s05p1-news-semantic-initial-plan-v1"
UPDATE_PLAN_VERSION = "m253-s05p1-news-semantic-confirmed-update-v1"
PILOT_VERSION = "s05p1-news-semantic-label-pilot-v1"
NO_TRADE_CLASSIFIER_VERSION = "s05p1-no-trade-benchmark-quorum-v1"
NO_TRADE_BENCHMARKS = ("SPY", "QQQ", "IWM", "DIA")
NO_TRADE_BENCHMARK_QUORUM = 3


def ensure_no_trade_classification_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ibkr_bar_shard_classifications(
            shard_key TEXT PRIMARY KEY,
            symbol TEXT NOT NULL,
            data_kind TEXT NOT NULL,
            classification TEXT NOT NULL,
            source_error_category TEXT NOT NULL,
            source_error_text TEXT NOT NULL,
            benchmark_symbols_json TEXT NOT NULL,
            benchmark_observed_json TEXT NOT NULL,
            benchmark_observed_count INTEGER NOT NULL,
            benchmark_quorum INTEGER NOT NULL,
            midpoint_diagnostic_only INTEGER NOT NULL DEFAULT 0,
            midpoint_substituted INTEGER NOT NULL DEFAULT 0,
            classifier_version TEXT NOT NULL,
            classified_at_utc TEXT NOT NULL,
            FOREIGN KEY(shard_key) REFERENCES ibkr_bar_shards(shard_key)
        )
        """
    )
    conn.commit()


def classify_no_trade_shard(conn: sqlite3.Connection, shard: Any, source_error_text: str) -> dict[str, Any]:
    observed: dict[str, int] = {}
    for symbol in NO_TRADE_BENCHMARKS:
        count = int(
            conn.execute(
                """SELECT COUNT(*) FROM ibkr_historical_bars
                    WHERE symbol=? AND data_kind='intraday' AND what_to_show='TRADES'
                      AND bar_time_utc>=? AND bar_time_utc<=?""",
                (symbol, shard.start_at_utc.isoformat(), shard.end_at_utc.isoformat()),
            ).fetchone()[0]
        )
        if count > 0:
            observed[symbol] = count
    classification = (
        "no_trade_or_symbol_specific_candidate"
        if shard.data_kind == "intraday" and len(observed) >= NO_TRADE_BENCHMARK_QUORUM
        else "unclassified_hmds_no_data"
    )
    return {
        "shard_key": shard.shard_key,
        "symbol": shard.symbol,
        "data_kind": shard.data_kind,
        "classification": classification,
        "source_error_category": "historical_unavailable",
        "source_error_text": source_error_text,
        "benchmark_symbols": list(NO_TRADE_BENCHMARKS),
        "benchmark_observed": observed,
        "benchmark_observed_count": len(observed),
        "benchmark_quorum": NO_TRADE_BENCHMARK_QUORUM,
        "midpoint_diagnostic_only": True,
        "midpoint_substituted": False,
        "classifier_version": NO_TRADE_CLASSIFIER_VERSION,
    }


def persist_no_trade_classification(conn: sqlite3.Connection, result: dict[str, Any]) -> None:
    ensure_no_trade_classification_schema(conn)
    conn.execute(
        """
        INSERT INTO ibkr_bar_shard_classifications(
            shard_key,symbol,data_kind,classification,source_error_category,source_error_text,
            benchmark_symbols_json,benchmark_observed_json,benchmark_observed_count,benchmark_quorum,
            midpoint_diagnostic_only,midpoint_substituted,classifier_version,classified_at_utc
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(shard_key) DO UPDATE SET
            symbol=excluded.symbol,data_kind=excluded.data_kind,classification=excluded.classification,
            source_error_category=excluded.source_error_category,source_error_text=excluded.source_error_text,
            benchmark_symbols_json=excluded.benchmark_symbols_json,
            benchmark_observed_json=excluded.benchmark_observed_json,
            benchmark_observed_count=excluded.benchmark_observed_count,
            benchmark_quorum=excluded.benchmark_quorum,
            midpoint_diagnostic_only=excluded.midpoint_diagnostic_only,
            midpoint_substituted=excluded.midpoint_substituted,
            classifier_version=excluded.classifier_version,
            classified_at_utc=excluded.classified_at_utc
        """,
        (
            result["shard_key"], result["symbol"], result["data_kind"], result["classification"],
            result["source_error_category"], result["source_error_text"],
            json.dumps(result["benchmark_symbols"], sort_keys=True),
            json.dumps(result["benchmark_observed"], sort_keys=True),
            result["benchmark_observed_count"], result["benchmark_quorum"],
            int(result["midpoint_diagnostic_only"]), int(result["midpoint_substituted"]),
            result["classifier_version"], datetime.now(UTC).isoformat(),
        ),
    )
    conn.commit()


def preserve_shard_classifications(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    ensure_no_trade_classification_schema(conn)
    rows = [dict(row) for row in conn.execute("SELECT * FROM ibkr_bar_shard_classifications")]
    conn.execute("DELETE FROM ibkr_bar_shard_classifications")
    conn.commit()
    return rows


def restore_shard_classifications(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> None:
    existing = {str(row[0]) for row in conn.execute("SELECT shard_key FROM ibkr_bar_shards")}
    for row in rows:
        if str(row["shard_key"]) not in existing:
            continue
        persist_no_trade_classification(
            conn,
            {
                "shard_key": row["shard_key"],
                "symbol": row["symbol"],
                "data_kind": row["data_kind"],
                "classification": row["classification"],
                "source_error_category": row["source_error_category"],
                "source_error_text": row["source_error_text"],
                "benchmark_symbols": json.loads(row["benchmark_symbols_json"]),
                "benchmark_observed": json.loads(row["benchmark_observed_json"]),
                "benchmark_observed_count": row["benchmark_observed_count"],
                "benchmark_quorum": row["benchmark_quorum"],
                "midpoint_diagnostic_only": bool(row["midpoint_diagnostic_only"]),
                "midpoint_substituted": bool(row["midpoint_substituted"]),
                "classifier_version": row["classifier_version"],
            },
        )


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def select_assets(selection: dict[str, Any]) -> dict[int, list[dict[str, Any]]]:
    result: dict[int, list[dict[str, Any]]] = {}
    for cluster in selection["clusters"]:
        cluster_id = int(cluster["event_cluster_id"])
        assets = [
            {"symbol": symbol, "asset_role": role, "proxy_family": family, "label_scope": "core_context",
             "inclusion_source": "required_core_contract", "resolver_status": "pending_dynamic_resolution", "tradable_candidate": 1}
            for symbol, role, family in REQUIRED_CORE_ASSETS
        ]
        if int(cluster["causal_trigger_eligible"]):
            for role in cluster.get("direct_roles", [])[:2]:
                symbol = str(role["symbol"]).upper()
                if symbol in {asset["symbol"] for asset in assets}:
                    continue
                assets.append({"symbol": symbol, "asset_role": "direct_entity", "proxy_family": "direct_subject",
                               "label_scope": "direct", "inclusion_source": "s05p1_explicit_subject_role",
                               "resolver_status": "pending_dynamic_resolution", "tradable_candidate": 1})
        result[cluster_id] = assets
    return result


def resolve_and_prune_assets(assets: dict[int, list[dict[str, Any]]], *, host: str, port: int, client_id: int) -> dict[str, Any]:
    source = IBKRHistoricalIntradayPriceSource(host=host, port=port, client_id=client_id, timeout_seconds=30,
                                               what_to_show="TRADES", bar_size="1 min", use_rth=False)
    symbols = sorted({asset["symbol"] for values in assets.values() for asset in values})
    resolved: dict[str, int] = {}
    resolved_contracts: dict[str, dict[str, Any]] = {}
    errors: dict[str, str] = {}
    try:
        for symbol in symbols:
            try:
                contract = source.resolve_stock_contract(symbol)
                resolved[symbol] = int(contract.conId)
                resolved_contracts[symbol] = {
                    "con_id": int(contract.conId),
                    "sec_type": str(getattr(contract, "secType", "") or "STK"),
                    "exchange": str(getattr(contract, "exchange", "") or "SMART"),
                    "primary_exchange": str(getattr(contract, "primaryExchange", "") or ""),
                    "currency": str(getattr(contract, "currency", "") or "USD"),
                    "local_symbol": str(getattr(contract, "localSymbol", "") or symbol),
                    "trading_class": str(getattr(contract, "tradingClass", "") or symbol),
                }
            except Exception as exc:
                errors[symbol] = str(exc)
    finally:
        source.close()
    required = {symbol for symbol, _, _ in REQUIRED_CORE_ASSETS}
    missing_core = sorted(required - set(resolved))
    if missing_core:
        raise RuntimeError(f"required core resolution failed: {missing_core}")
    dropped_direct = Counter()
    for cluster_id, values in list(assets.items()):
        kept = []
        for asset in values:
            if asset["symbol"] in resolved:
                copy = dict(asset)
                copy["resolver_status"] = "resolved"
                copy["con_id"] = resolved[asset["symbol"]]
                kept.append(copy)
            elif asset["asset_role"] == "direct_entity":
                dropped_direct[asset["symbol"]] += 1
        assets[cluster_id] = kept
    return {"resolved_symbols": resolved, "resolved_contracts": resolved_contracts,
            "errors": errors, "missing_core": missing_core,
            "dropped_direct": dict(dropped_direct)}


def collect_market_data_parallel(cache_path: Path, shards: list[Any], resolution: dict[str, Any], *, host: str, port: int,
                                 workers: int = 4) -> dict[str, Any]:
    with base.connect_cache(cache_path) as conn:
        ensure_no_trade_classification_schema(conn)
        completed_keys = {str(row[0]) for row in conn.execute("SELECT shard_key FROM ibkr_bar_shards WHERE status='completed'")}
        pending = [shard for shard in shards if shard.shard_key not in completed_keys]
        for symbol, con_id in resolution["resolved_symbols"].items():
            metadata = resolution.get("resolved_contracts", {}).get(symbol, {})
            class ContractStub:
                pass
            contract = ContractStub()
            contract.conId = con_id; contract.secType = metadata.get("sec_type", "STK")
            contract.exchange = metadata.get("exchange", "SMART")
            contract.primaryExchange = metadata.get("primary_exchange", "")
            contract.currency = metadata.get("currency", "USD")
            contract.localSymbol = metadata.get("local_symbol", symbol)
            contract.tradingClass = metadata.get("trading_class", symbol)
            base._persist_contract(conn, symbol, contract)
            conn.execute("UPDATE ibkr_bar_shards SET con_id=? WHERE symbol=?", (con_id, symbol))
        for shard in pending:
            conn.execute("UPDATE ibkr_bar_shards SET status='pending',last_error=NULL WHERE shard_key=?", (shard.shard_key,))
        conn.commit()

    local = threading.local()
    sources: list[Any] = []
    sources_lock = threading.Lock()
    client_counter = iter(range(8650, 8650 + workers))

    def get_sources() -> tuple[Any, Any]:
        if not hasattr(local, "intraday"):
            client_id = next(client_counter)
            local.intraday = IBKRHistoricalIntradayPriceSource(host=host, port=port, client_id=client_id,
                timeout_seconds=45, what_to_show="TRADES", bar_size="1 min", use_rth=False)
            local.daily = IBKRHistoricalDailyPriceSource(host=host, port=port, client_id=client_id + 100,
                timeout_seconds=45, what_to_show="TRADES", use_rth=True)
            with sources_lock:
                sources.extend((local.intraday, local.daily))
        return local.intraday, local.daily

    def fetch(shard: Any) -> tuple[Any, list[Any] | None, str, str]:
        intraday, daily = get_sources()
        last_error = ""
        for attempt in range(2):
            try:
                if shard.data_kind == "daily":
                    rows = daily.get_daily_bars(shard.symbol, shard.start_at_utc.date(), shard.end_at_utc.date())
                else:
                    rows = intraday.get_intraday_bars(shard.symbol, shard.start_at_utc, shard.end_at_utc)
                time.sleep(0.10)
                return shard, rows, "", ""
            except Exception as exc:
                last_error = str(exc)
                if "HMDS query returned no data" in last_error:
                    return shard, [], "", last_error
                time.sleep(1.0 + attempt)
        return shard, None, last_error, ""

    errors: list[dict[str, Any]] = []
    no_data_shards = 0
    requested_no_data_classified = 0
    requested_no_data_unclassified = 0
    no_data_results: list[tuple[Any, str]] = []
    try:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="s05p1-ibkr") as executor, base.connect_cache(cache_path) as conn:
            futures = {executor.submit(fetch, shard): shard for shard in pending}
            for future in as_completed(futures):
                shard, rows, error, no_data_error = future.result()
                if error or rows is None:
                    conn.execute("UPDATE ibkr_bar_shards SET status='failed',last_error=? WHERE shard_key=?", (error, shard.shard_key))
                    errors.append({"symbol": shard.symbol, "shard_key": shard.shard_key, "stage": shard.data_kind, "error": error})
                elif no_data_error:
                    no_data_shards += 1
                    no_data_results.append((shard, no_data_error))
                else:
                    inserted = base._persist_bars(conn, shard, int(resolution["resolved_symbols"][shard.symbol]), rows)
                    conn.execute("""UPDATE ibkr_bar_shards SET status='completed',con_id=?,provider_row_count=?,
                                   inserted_row_count=inserted_row_count+?,completed_at_utc=?,last_error=NULL WHERE shard_key=?""",
                                 (int(resolution["resolved_symbols"][shard.symbol]),len(rows),inserted,datetime.now(UTC).isoformat(),shard.shard_key))
                conn.commit()
            for shard, no_data_error in no_data_results:
                classification = classify_no_trade_shard(conn, shard, no_data_error)
                if classification["classification"] == "no_trade_or_symbol_specific_candidate":
                    persist_no_trade_classification(conn, classification)
                    conn.execute(
                        """UPDATE ibkr_bar_shards SET status='completed',con_id=?,provider_row_count=0,
                                  completed_at_utc=?,last_error=NULL WHERE shard_key=?""",
                        (int(resolution["resolved_symbols"][shard.symbol]), datetime.now(UTC).isoformat(), shard.shard_key),
                    )
                    requested_no_data_classified += 1
                else:
                    conn.execute(
                        "UPDATE ibkr_bar_shards SET status='failed',last_error=? WHERE shard_key=?",
                        (no_data_error, shard.shard_key),
                    )
                    errors.append({
                        "symbol": shard.symbol,
                        "shard_key": shard.shard_key,
                        "stage": shard.data_kind,
                        "error": no_data_error,
                        "classification": classification,
                    })
                    requested_no_data_unclassified += 1
                conn.commit()
            base.persist_market_data_provenance(conn)
            snapshot = base.cache_snapshot(conn)
            completed = int(conn.execute("SELECT COUNT(*) FROM ibkr_bar_shards WHERE status='completed'").fetchone()[0])
            failed = int(conn.execute("SELECT COUNT(*) FROM ibkr_bar_shards WHERE status='failed'").fetchone()[0])
            classified_no_trade_shards = int(
                conn.execute(
                    "SELECT COUNT(*) FROM ibkr_bar_shard_classifications WHERE classification='no_trade_or_symbol_specific_candidate'"
                ).fetchone()[0]
            )
    finally:
        for source in sources:
            source.close()
    return {"symbols": len(resolution["resolved_symbols"]),"workers":workers,"planned_shards":len(shards),
            "requested_shards":len(pending),"completed_shards":completed,"failed_shards":failed,
            "no_data_shards":no_data_shards,
            "requested_no_data_classified":requested_no_data_classified,
            "requested_no_data_unclassified":requested_no_data_unclassified,
            "classified_no_trade_shards":classified_no_trade_shards,
            "errors":errors,"snapshot":snapshot}


def coalesce_intraday_plan(cache_path: Path, windows: list[Any], shards: list[Any], assignments: dict[str, str],
                           max_span_hours: int = 24) -> tuple[list[Any], dict[str, str]]:
    intraday = [shard for shard in shards if shard.data_kind == "intraday"]
    daily = [shard for shard in shards if shard.data_kind == "daily"]
    by_symbol: dict[str, list[Any]] = {}
    for shard in intraday:
        by_symbol.setdefault(shard.symbol, []).append(shard)
    merged: list[Any] = []
    old_to_new: dict[str, str] = {}
    max_span = timedelta(hours=max_span_hours)
    for symbol, values in by_symbol.items():
        bucket: list[Any] = []
        bucket_start = None
        bucket_end = None
        for shard in sorted(values, key=lambda item: (item.start_at_utc,item.end_at_utc)):
            proposed_end = max(bucket_end,shard.end_at_utc) if bucket_end else shard.end_at_utc
            if bucket and shard.start_at_utc <= bucket_end + max_span and proposed_end - bucket_start <= max_span:
                bucket.append(shard); bucket_end = proposed_end
            else:
                if bucket:
                    key=base._stable_hash(["s05p1-coalesced",symbol,bucket_start.isoformat(),bucket_end.isoformat(),"1 min","TRADES",False])
                    item=base.BarShard(key,"intraday",symbol,bucket_start,bucket_end,"1 min","TRADES",False)
                    merged.append(item)
                    old_to_new.update({old.shard_key:key for old in bucket})
                bucket=[shard]; bucket_start=shard.start_at_utc; bucket_end=shard.end_at_utc
        if bucket:
            key=base._stable_hash(["s05p1-coalesced",symbol,bucket_start.isoformat(),bucket_end.isoformat(),"1 min","TRADES",False])
            item=base.BarShard(key,"intraday",symbol,bucket_start,bucket_end,"1 min","TRADES",False)
            merged.append(item)
            old_to_new.update({old.shard_key:key for old in bucket})
    merged.extend(daily)
    new_assignments={window_key:old_to_new.get(old_key,old_key) for window_key,old_key in assignments.items()}
    new_keys={shard.shard_key for shard in merged}
    with base.connect_cache(cache_path) as conn:
        classifications = preserve_shard_classifications(conn)
        preserved={str(row["shard_key"]):dict(row) for row in conn.execute("SELECT * FROM ibkr_bar_shards WHERE shard_key IN (%s)" % ",".join("?" for _ in new_keys),tuple(new_keys))}
        conn.execute("DELETE FROM ibkr_event_windows")
        conn.execute("DELETE FROM ibkr_bar_shards")
        conn.commit()
        base.persist_plan(conn,windows,merged,new_assignments)
        for key,row in preserved.items():
            if row["status"] == "completed":
                conn.execute("""UPDATE ibkr_bar_shards SET status='completed',con_id=?,provider_row_count=?,inserted_row_count=?,
                              completed_at_utc=?,last_error=NULL WHERE shard_key=?""",
                             (row["con_id"],row["provider_row_count"],row["inserted_row_count"],row["completed_at_utc"],key))
        restore_shard_classifications(conn, classifications)
        conn.commit()
    return merged,new_assignments


def restore_original_plan_from_completed_coverage(cache_path: Path, windows: list[Any], shards: list[Any],
                                                  assignments: dict[str, str]) -> int:
    with base.connect_cache(cache_path) as conn:
        classifications = preserve_shard_classifications(conn)
        completed_ranges = [dict(row) for row in conn.execute(
            "SELECT symbol,data_kind,start_at_utc,end_at_utc FROM ibkr_bar_shards WHERE status='completed'"
        )]
        conn.execute("DELETE FROM ibkr_event_windows")
        conn.execute("DELETE FROM ibkr_bar_shards")
        conn.commit()
        base.persist_plan(conn,windows,shards,assignments)
        restored = 0
        for shard in shards:
            covered = any(
                row["symbol"] == shard.symbol and row["data_kind"] == shard.data_kind
                and parse_utc(row["start_at_utc"]) <= shard.start_at_utc
                and parse_utc(row["end_at_utc"]) >= shard.end_at_utc
                for row in completed_ranges
            )
            if covered:
                conn.execute("UPDATE ibkr_bar_shards SET status='completed',completed_at_utc=? WHERE shard_key=?",
                             (datetime.now(UTC).isoformat(),shard.shard_key))
                restored += 1
        restore_shard_classifications(conn, classifications)
        conn.commit()
    return restored


def attach_metadata_and_policy(clone_db: Path, selection: dict[str, Any]) -> dict[str, Any]:
    by_cluster = {int(row["event_cluster_id"]): row for row in selection["clusters"]}
    now = datetime.now(UTC).isoformat()
    with sqlite3.connect(clone_db) as conn:
        conn.row_factory = sqlite3.Row
        ensure_closeout_schema(conn)
        plans = [dict(row) for row in conn.execute("SELECT reaction_plan_id,event_cluster_id FROM reaction_universe_plans WHERE plan_version=?", (PLAN_VERSION,))]
        for plan in plans:
            cluster = by_cluster[int(plan["event_cluster_id"])]
            conn.execute(
                """INSERT OR REPLACE INTO s05p1_reaction_plan_metadata(
                       reaction_plan_id,cluster_snapshot_id,plan_as_of_utc,news_content_type,causal_trigger_eligible,
                       event_series_id,novelty_type,series_sample_weight,core_scope_version,created_at_utc
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (plan["reaction_plan_id"],cluster["cluster_snapshot_id"],cluster["plan_as_of_utc"],cluster["news_content_type"],
                 cluster["causal_trigger_eligible"],cluster["event_series_id"],cluster["novelty_type"],
                 cluster["series_sample_weight"],CORE_SCOPE_VERSION,now),
            )
            conn.execute(
                """UPDATE event_reaction_labels
                      SET session_bucket=?,sample_weight=?,validation_split_group=?,release_bundle_id=?,overlap_group_id=?
                    WHERE reaction_plan_id=?""",
                (cluster["session_bucket"],cluster["series_sample_weight"],cluster["event_series_id"],cluster["event_series_id"],
                 cluster["event_series_id"],plan["reaction_plan_id"]),
            )
            if not int(cluster["causal_trigger_eligible"]):
                conn.execute(
                    """UPDATE event_reaction_labels
                          SET strict_training_eligible=0,label_quality_flag='excluded',
                              label_quality_reason='causal_ineligible_content',label_exclusion_reason='causal_ineligible_content'
                        WHERE reaction_plan_id=?""", (plan["reaction_plan_id"],)
                )
        conn.commit()
        return {"plans": len(plans), "causal_eligible_plans": sum(int(row["causal_trigger_eligible"]) for row in by_cluster.values()),
                "causal_ineligible_plans": sum(not int(row["causal_trigger_eligible"]) for row in by_cluster.values())}


def create_confirmed_update_plans(clone_db: Path, resolution: dict[str, Any]) -> int:
    now = datetime.now(UTC).isoformat()
    created = 0
    with sqlite3.connect(clone_db) as conn:
        conn.row_factory = sqlite3.Row
        ensure_closeout_schema(conn)
        snapshots = [dict(row) for row in conn.execute(
            "SELECT * FROM s05p1_news_cluster_snapshots WHERE snapshot_type='confirmed_update' ORDER BY event_cluster_id,plan_as_of_utc"
        )]
        for snapshot in snapshots:
            conn.execute(
                """INSERT OR IGNORE INTO reaction_universe_plans(
                       event_cluster_id,plan_version,created_at,available_at_cutoff,core_scope_version,event_proxy_map_version,
                       security_master_version,mapper_version,classifier_version,reason
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (snapshot["event_cluster_id"],UPDATE_PLAN_VERSION,now,snapshot["plan_as_of_utc"],CORE_SCOPE_VERSION,
                 "fail_closed_update_core_only_v1","ibkr_dynamic_resolution_v1","s05p1_snapshot_entities_v1",
                 snapshot["classifier_version"],"late cross-provider confirmation creates separate update plan"),
            )
            plan_id = int(conn.execute(
                "SELECT reaction_plan_id FROM reaction_universe_plans WHERE event_cluster_id=? AND plan_version=?",
                (snapshot["event_cluster_id"],UPDATE_PLAN_VERSION),
            ).fetchone()[0])
            for symbol, role, family in REQUIRED_CORE_ASSETS:
                conn.execute(
                    """INSERT OR IGNORE INTO reaction_universe_assets(
                           reaction_plan_id,event_cluster_id,asset_symbol,con_id,asset_role,proxy_family,inclusion_reason,
                           inclusion_source,tradable_candidate,label_scope,created_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (plan_id,snapshot["event_cluster_id"],symbol,resolution["resolved_symbols"][symbol],role,family,
                     "confirmed update core context","required_core_contract",1,"core_context",now),
                )
            conn.execute(
                """INSERT OR REPLACE INTO s05p1_reaction_plan_metadata(
                       reaction_plan_id,cluster_snapshot_id,plan_as_of_utc,news_content_type,causal_trigger_eligible,
                       event_series_id,novelty_type,series_sample_weight,core_scope_version,created_at_utc
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (plan_id,snapshot["cluster_snapshot_id"],snapshot["plan_as_of_utc"],snapshot["news_content_type"],
                 snapshot["causal_trigger_eligible"],snapshot["event_series_id"],"update",snapshot["series_sample_weight"],
                 CORE_SCOPE_VERSION,now),
            )
            created += 1
        conn.commit()
    return created


def audit_s05p1(clone_db: Path, cache_db: Path, selection: dict[str, Any], collection: dict[str, Any],
                 first_counts: dict[str, int], second_counts: dict[str, int], resolution: dict[str, Any]) -> dict[str, Any]:
    required = {symbol for symbol, _, _ in REQUIRED_CORE_ASSETS}
    with sqlite3.connect(clone_db) as conn:
        conn.row_factory = sqlite3.Row
        plans = int(conn.execute("SELECT COUNT(*) FROM reaction_universe_plans WHERE plan_version=?", (PLAN_VERSION,)).fetchone()[0])
        assets = [dict(row) for row in conn.execute(
            """SELECT a.*,m.news_content_type,m.causal_trigger_eligible,m.event_series_id
                 FROM reaction_universe_assets a JOIN reaction_universe_plans p ON p.reaction_plan_id=a.reaction_plan_id
                 JOIN s05p1_reaction_plan_metadata m ON m.reaction_plan_id=p.reaction_plan_id WHERE p.plan_version=?""", (PLAN_VERSION,))]
        labels = [dict(row) for row in conn.execute(
            """SELECT l.*,m.news_content_type,m.causal_trigger_eligible,m.event_series_id,s.is_trading_day,s.plan_as_of_utc
                 FROM event_reaction_labels l JOIN reaction_universe_plans p ON p.reaction_plan_id=l.reaction_plan_id
                 JOIN s05p1_reaction_plan_metadata m ON m.reaction_plan_id=p.reaction_plan_id
                 JOIN s05p1_news_cluster_snapshots s ON s.cluster_snapshot_id=m.cluster_snapshot_id
                WHERE p.plan_version=?""", (PLAN_VERSION,))]
        core_by_plan: dict[int, set[str]] = {}
        for row in assets:
            if row["label_scope"] == "core_context":
                core_by_plan.setdefault(int(row["reaction_plan_id"]), set()).add(str(row["asset_symbol"]))
        core_missing = sum(symbols != required for symbols in core_by_plan.values()) + max(0, plans - len(core_by_plan))
        core_as_direct = sum(row["asset_symbol"] in required and row["asset_role"] == "direct_entity" for row in assets)
        route_direct = sum(row["asset_role"] == "direct_entity" and row["inclusion_source"] in {"ibkr_route","fallback_context"} for row in assets)
        unresolved_labels = sum(row["con_id"] is None for row in assets)
        causal_ineligible_strict = sum(not int(row["causal_trigger_eligible"]) and int(row["strict_training_eligible"]) for row in labels)
        session_mismatch = sum((not int(row["is_trading_day"])) and row["session_bucket"] in {"premarket","regular","afterhours"} for row in labels)
        exact_lookahead = 0
        cluster_map = {int(row["event_cluster_id"]): row for row in selection["clusters"]}
        for row in labels:
            if row["window"] not in base.INTRADAY_WINDOWS:
                continue
            cluster = cluster_map[int(row["event_cluster_id"])]
            target = base.ceil_minute(parse_utc(cluster["plan_as_of_utc"]) + timedelta(seconds=int(row["decision_lag_seconds"])))
            if parse_utc(row["start_at_utc"]) < target:
                exact_lookahead += 1
        duplicates = int(conn.execute(
            """SELECT COUNT(*) FROM (SELECT l.reaction_plan_id,l.asset_symbol,l.asset_role,l.proxy_family,l.window,l.decision_lag_seconds,COUNT(*) n
                 FROM event_reaction_labels l JOIN reaction_universe_plans p ON p.reaction_plan_id=l.reaction_plan_id
                WHERE p.plan_version=? GROUP BY 1,2,3,4,5,6 HAVING n>1)""", (PLAN_VERSION,)).fetchone()[0])
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        confirmed_snapshots = int(conn.execute("SELECT COUNT(*) FROM s05p1_news_cluster_snapshots WHERE snapshot_type='confirmed_update'").fetchone()[0])
        confirmed_update_plans = int(conn.execute("SELECT COUNT(*) FROM reaction_universe_plans WHERE plan_version=?", (UPDATE_PLAN_VERSION,)).fetchone()[0])
        asset_roles = dict(Counter(str(row["asset_role"]) for row in assets))
        assets_per_plan = dict(Counter(sum(1 for row in assets if int(row["reaction_plan_id"]) == plan_id)
                                       for plan_id in {int(row["reaction_plan_id"]) for row in assets}))
        window_lag = [dict(row) for row in conn.execute(
            """SELECT l.window,l.decision_lag_seconds,COUNT(*) count,SUM(l.strict_training_eligible) strict_count
                 FROM event_reaction_labels l JOIN reaction_universe_plans p ON p.reaction_plan_id=l.reaction_plan_id
                WHERE p.plan_version=? GROUP BY 1,2 ORDER BY 1,2""", (PLAN_VERSION,))]
        matrices = {}
        for name, columns in {
            "exclusion_by_session": "l.session_bucket,l.label_exclusion_reason",
            "exclusion_by_event_class": "s.event_class_as_of,l.label_exclusion_reason",
            "exclusion_by_asset_role": "l.asset_role,l.label_exclusion_reason",
            "exclusion_by_window_lag": "l.window,l.decision_lag_seconds,l.label_exclusion_reason",
        }.items():
            matrices[name] = [dict(row) for row in conn.execute(
                f"""SELECT {columns},COUNT(*) count,SUM(l.strict_training_eligible) strict_count
                      FROM event_reaction_labels l JOIN reaction_universe_plans p ON p.reaction_plan_id=l.reaction_plan_id
                      JOIN s05p1_reaction_plan_metadata m ON m.reaction_plan_id=p.reaction_plan_id
                      JOIN s05p1_news_cluster_snapshots s ON s.cluster_snapshot_id=m.cluster_snapshot_id
                     WHERE p.plan_version=? GROUP BY {columns}""", (PLAN_VERSION,))]
        provider_matrix = Counter()
        strict_by_scope = Counter()
        asset_scope = {(int(row["reaction_plan_id"]),str(row["asset_symbol"]),str(row["asset_role"])): str(row["label_scope"]) for row in assets}
        for row in labels:
            provider = str(cluster_map[int(row["event_cluster_id"])]["provider_bucket"])
            provider_matrix[(provider,str(row["label_exclusion_reason"] or "none"))] += 1
            scope = asset_scope.get((int(row["reaction_plan_id"]),str(row["asset_symbol"]),str(row["asset_role"])),"unknown")
            strict_by_scope[(scope,int(row["strict_training_eligible"]))] += 1
        matrices["exclusion_by_provider"] = [
            {"provider": provider, "label_exclusion_reason": reason, "count": count}
            for (provider,reason),count in sorted(provider_matrix.items())
        ]
        matrices["strict_by_label_scope"] = [
            {"label_scope": scope, "strict_training_eligible": strict, "count": count}
            for (scope,strict),count in sorted(strict_by_scope.items())
        ]
    rerun_diff = {key: second_counts[key] - first_counts[key] for key in first_counts}
    checks = {
        "selected_300_to_500": 300 <= selection["selected_count"] <= 500,
        "core_missing_plan_count_zero": core_missing == 0,
        "core_context_not_direct": core_as_direct == 0,
        "route_fallback_direct_contamination_zero": route_direct == 0,
        "unresolved_tradable_labels_zero": unresolved_labels == 0,
        "causal_ineligible_strict_labels_zero": causal_ineligible_strict == 0,
        "holiday_weekend_session_misclassification_zero": session_mismatch == 0,
        "exact_second_lookahead_zero": exact_lookahead == 0,
        "duplicate_label_groups_zero": duplicates == 0,
        "clone_rerun_count_diff_zero": all(value == 0 for value in rerun_diff.values()),
        "market_data_shards_complete": collection["completed_shards"] == collection["planned_shards"] and collection["failed_shards"] == 0,
        "requested_hmds_no_data_classified": collection.get("requested_no_data_unclassified", 0) == 0,
        "late_confirmation_update_plans_complete": confirmed_update_plans == confirmed_snapshots,
        "integrity_ok": integrity == "ok",
    }
    return {"verdict": "PASS" if all(checks.values()) else "FAIL", "checks": checks,
            "counts": {"plans": plans,"assets": len(assets),"labels": len(labels),
                       "strict": sum(int(row["strict_training_eligible"]) for row in labels)},
            "asset_roles": asset_roles,"assets_per_plan_distribution": assets_per_plan,"core_missing_plan_count": core_missing,
            "core_as_direct": core_as_direct,"route_direct": route_direct,"unresolved_assets": unresolved_labels,
            "causal_ineligible_strict": causal_ineligible_strict,"session_mismatch": session_mismatch,
            "confirmed_update_snapshots": confirmed_snapshots,"confirmed_update_plans": confirmed_update_plans,
            "exact_second_lookahead": exact_lookahead,"duplicate_labels": duplicates,"window_lag": window_lag,
            "exclusion_matrices": matrices,"rerun_diff": rerun_diff,"collection": collection,"resolution": resolution,
            "integrity": integrity}


def markdown(report: dict[str, Any]) -> str:
    audit = report["audit"]
    lines = ["# S05P1 Time-Stratified News Label Pilot", "", f"- Verdict: **{report['verdict']}**",
             f"- Plans/assets/labels/strict: **{audit['counts']['plans']} / {audit['counts']['assets']} / {audit['counts']['labels']} / {audit['counts']['strict']}**",
             f"- Required core missing plans: **{audit['core_missing_plan_count']}**",
             f"- Classified no-trade shards: **{audit['collection'].get('classified_no_trade_shards', 0)}**",
             f"- Asset roles: `{json.dumps(audit['asset_roles'], sort_keys=True)}`",
             f"- Assets per plan: `{json.dumps(audit['assets_per_plan_distribution'], sort_keys=True)}`", "", "## Hard gates", ""]
    lines.extend(f"- {key}: **{'PASS' if value else 'FAIL'}**" for key, value in audit["checks"].items())
    lines += ["", "## Window by lag", ""]
    lines.extend(f"- {row['window']} lag={row['decision_lag_seconds']}: count={row['count']}, strict={row['strict_count']}" for row in audit["window_lag"])
    lines += ["", "This remains clone-only. Full canonical news S05 and M254/M255/M256 were not run.", ""]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run S05P1 required-core time-stratified news label pilot")
    parser.add_argument("--canonical-db", type=Path, default=Path("work/canonical_research_backfill/m254_canonical_20250101_20260706_v1/canonical_market_events.db"))
    parser.add_argument("--selection", type=Path, default=Path("artifacts/audit/s05p1_news_time_stratified_selection.json"))
    parser.add_argument("--clone-db", type=Path, default=Path("work/s05p1_news_label_pilot/time_stratified_300_v1/canonical_market_events_s05p1_label_clone.db"))
    parser.add_argument("--cache-db", type=Path, default=Path("work/s05p1_news_label_pilot/time_stratified_300_v1/ibkr_s05p1_news_cache.db"))
    parser.add_argument("--json-output", type=Path, default=Path("artifacts/audit/s05p1_news_label_pilot.json"))
    parser.add_argument("--markdown-output", type=Path, default=Path("artifacts/audit/s05p1_news_label_pilot.md"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4001)
    args = parser.parse_args()
    selection = json.loads(args.selection.read_text(encoding="utf-8"))
    canonical_sha = sha256(args.canonical_db)
    base.sqlite_clone(args.canonical_db,args.clone_db)
    selection = materialize_closeout(args.clone_db,selection)["selection"]
    assets = select_assets(selection)
    resolution = resolve_and_prune_assets(assets,host=args.host,port=args.port,client_id=8631)
    windows,shards,assignments,_=base.create_cache_plan(args.cache_db,selection["clusters"],assets)
    restored_from_coverage=restore_original_plan_from_completed_coverage(args.cache_db,windows,shards,assignments)
    collection=collect_market_data_parallel(args.cache_db,shards,resolution,host=args.host,port=args.port,workers=8)
    collection["restored_from_completed_coalesced_coverage"] = restored_from_coverage
    symbols=sorted({asset["symbol"] for values in assets.values() for asset in values})
    actions=fetch_yahoo_corporate_actions(symbols,min(parse_utc(c["anchor_at_utc"]).date() for c in selection["clusters"])-timedelta(days=15),
                                           max(parse_utc(c["anchor_at_utc"]).date() for c in selection["clusters"])+timedelta(days=20))
    base.NEWS_PLAN_VERSION=PLAN_VERSION; base.NEWS_PILOT_VERSION=PILOT_VERSION
    plan_ids,asset_lookup=base.create_plans(args.clone_db,selection["clusters"],assets,args.cache_db)
    confirmed_update_plans=create_confirmed_update_plans(args.clone_db,resolution)
    first_build=base.build_labels(args.clone_db,args.cache_db,selection["clusters"],plan_ids,asset_lookup,actions)
    first_policy=attach_metadata_and_policy(args.clone_db,selection)
    with sqlite3.connect(args.clone_db) as conn:
        first_counts={table:int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) for table in ("reaction_universe_plans","reaction_universe_assets","event_reaction_labels","s05_market_data_price_quality_flags","s05p1_reaction_plan_metadata")}
    second_build=base.build_labels(args.clone_db,args.cache_db,selection["clusters"],plan_ids,asset_lookup,actions)
    second_policy=attach_metadata_and_policy(args.clone_db,selection)
    with sqlite3.connect(args.clone_db) as conn:
        second_counts={table:int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) for table in first_counts}
    audit=audit_s05p1(args.clone_db,args.cache_db,selection,collection,first_counts,second_counts,resolution)
    audit["checks"]["canonical_source_unchanged"]=sha256(args.canonical_db)==canonical_sha
    verdict="PASS" if all(audit["checks"].values()) else "FAIL"
    report={"version":PILOT_VERSION,"generated_at_utc":datetime.now(UTC).isoformat(),"verdict":verdict,
            "selection":str(args.selection),"clone_db":str(args.clone_db),"cache_db":str(args.cache_db),
            "first_build":first_build,"first_policy":first_policy,"second_build":second_build,"second_policy":second_policy,
            "confirmed_update_plans":confirmed_update_plans,
            "audit":audit,"safety":{"macro_canonical_modified":False,"full_news_canonical_promoted":False,
            "m254_m255_m256":False,"orders":False,"transmit":False,"live_routing":False,"threshold_promotion":False}}
    args.json_output.parent.mkdir(parents=True,exist_ok=True)
    args.json_output.write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding="utf-8")
    args.markdown_output.write_text(markdown(report),encoding="utf-8")
    print(json.dumps({"verdict":verdict,"counts":audit["counts"],"checks":audit["checks"],"asset_roles":audit["asset_roles"],
                      "core_missing":audit["core_missing_plan_count"],"collection":collection,"report":str(args.json_output)},indent=2,ensure_ascii=False))
    return 0 if verdict=="PASS" else 2


if __name__=="__main__":
    raise SystemExit(main())
