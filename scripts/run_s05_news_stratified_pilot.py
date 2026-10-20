from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import time as sleep_time
from collections import Counter, defaultdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from recorder.ibkr_market_data_pilot import (
    BarShard,
    EventWindow,
    _persist_bars,
    _persist_contract,
    build_shards,
    cache_snapshot,
    connect_cache,
    ensure_cache_schema,
    persist_market_data_provenance,
    persist_plan,
)
from recorder.market_outcomes import IBKRHistoricalDailyPriceSource, IBKRHistoricalIntradayPriceSource
from recorder.s04_market_data_pre_s05_audit import calendar_schedule, fetch_yahoo_corporate_actions, parse_utc
from recorder.s05_reaction_label_pilot import (
    CORPORATE_ACTION_SOURCE,
    DAILY_WINDOWS,
    INTRADAY_WINDOWS,
    LATENCY_MODEL_VERSION,
    PRICE_QUALITY_VERSION,
    RESIDUAL_MODEL_VERSION,
    RETURN_MATH_VERSION,
    STRICT_DECISION_LAGS_SECONDS,
    _action_map,
    _actions_in_window,
    _bars_between,
    _daily_by_session,
    _first_at_or_after,
    _flag_values,
    _price_quality_severity,
    _session_bucket,
    _stable_hash,
    _upsert_label,
    apply_selection_bias_policy,
    audit_price_quality,
    ensure_pilot_schema,
    load_market_data,
    populate_label_math,
)


NEWS_PLAN_VERSION = "m253-s05-news-stratified-pilot-v1"
NEWS_PILOT_VERSION = "s05-news-stratified-reaction-label-pilot-v1"
NON_STOCK_PROXY_SYMBOLS = {"VIX"}


def ceil_minute(value: datetime) -> datetime:
    rounded = value.replace(second=0, microsecond=0)
    return rounded if value == rounded else rounded + timedelta(minutes=1)


def sqlite_clone(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    with sqlite3.connect(source) as source_conn, sqlite3.connect(target) as target_conn:
        source_conn.backup(target_conn)


def select_assets(database: Path, clusters: list[dict[str, Any]]) -> dict[int, list[dict[str, Any]]]:
    ids = [int(row["event_cluster_id"]) for row in clusters]
    placeholders = ",".join("?" for _ in ids)
    with sqlite3.connect(database) as conn:
        conn.row_factory = sqlite3.Row
        rows = [
            dict(row)
            for row in conn.execute(
                f"""SELECT * FROM v_cluster_asset_candidate_aggregates
                      WHERE event_cluster_id IN ({placeholders})
                      ORDER BY event_cluster_id,
                               CASE role WHEN 'primary' THEN 0 WHEN 'secondary' THEN 1 WHEN 'proxy' THEN 2 ELSE 3 END,
                               confidence DESC,symbol""",
                ids,
            )
        ]
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[int(row["event_cluster_id"])].append(row)
    result: dict[int, list[dict[str, Any]]] = {}
    for cluster in clusters:
        cluster_id = int(cluster["event_cluster_id"])
        candidates = grouped.get(cluster_id, [])
        chosen: list[dict[str, Any]] = [
            {
                "symbol": "SPY",
                "asset_role": "core_index",
                "proxy_family": "broad_us_equity",
                "label_scope": "core_context",
                "inclusion_source": "required_market_benchmark",
                "resolver_status": "resolved",
                "tradable_candidate": 1,
            }
        ]
        direct = next(
            (
                row
                for row in candidates
                if str(row["resolver_status"]) == "resolved"
                and int(row["tradable_candidate"] or 0) == 1
                and str(row["role"]) in {"primary", "secondary"}
                and str(row["source"]) not in {"ibkr_route", "fallback_context"}
                and str(row["symbol"]).upper() not in NON_STOCK_PROXY_SYMBOLS
            ),
            None,
        )
        if direct and str(direct["symbol"]).upper() != "SPY":
            chosen.append(
                {
                    "symbol": str(direct["symbol"]).upper(),
                    "asset_role": "direct_entity",
                    "proxy_family": "direct",
                    "label_scope": "direct",
                    "inclusion_source": str(direct["source"]),
                    "resolver_status": str(direct["resolver_status"]),
                    "tradable_candidate": int(direct["tradable_candidate"]),
                }
            )
        proxy = next(
            (
                row
                for row in candidates
                if str(row["resolver_status"]) == "proxy_only"
                and int(row["tradable_candidate"] or 0) == 1
                and str(row["role"]) == "proxy"
                and str(row["source"]) == "theme_proxy"
                and str(row["symbol"]).upper() not in NON_STOCK_PROXY_SYMBOLS
                and str(row["symbol"]).upper() not in {asset["symbol"] for asset in chosen}
            ),
            None,
        )
        if proxy:
            chosen.append(
                {
                    "symbol": str(proxy["symbol"]).upper(),
                    "asset_role": "event_proxy",
                    "proxy_family": str(cluster.get("event_role") or "event_proxy"),
                    "label_scope": "event_proxy",
                    "inclusion_source": "theme_proxy",
                    "resolver_status": "proxy_only",
                    "tradable_candidate": 1,
                }
            )
        result[cluster_id] = chosen
    return result


def create_cache_plan(
    cache_path: Path,
    clusters: list[dict[str, Any]],
    assets: dict[int, list[dict[str, Any]]],
) -> tuple[list[EventWindow], list[BarShard], dict[str, str], dict[str, Any]]:
    windows: list[EventWindow] = []
    events: list[dict[str, Any]] = []
    for cluster in clusters:
        cluster_id = int(cluster["event_cluster_id"])
        anchor = parse_utc(str(cluster["anchor_at_utc"]))
        events.append(cluster)
        for asset in assets[cluster_id]:
            start = anchor - timedelta(minutes=60)
            end = anchor + timedelta(minutes=75)
            key = _stable_hash(["news-window", cluster_id, asset["symbol"], start.isoformat(), end.isoformat()])
            windows.append(
                EventWindow(
                    window_key=key,
                    macro_event_id=cluster_id,
                    event_cluster_id=cluster_id,
                    source_system=str(cluster["provider_bucket"]),
                    event_type=str(cluster["event_class"]),
                    release_name=str(cluster["primary_headline"]),
                    symbol=str(asset["symbol"]),
                    anchor_at_utc=anchor,
                    start_at_utc=start,
                    end_at_utc=end,
                )
            )
    start = min(window.anchor_at_utc for window in windows)
    end = max(window.anchor_at_utc for window in windows) + timedelta(days=1)
    config = {
        "intraday": {"bar_size": "1 min", "what_to_show": "TRADES", "use_rth": False},
        "daily": {"buffer_days_before": 10, "buffer_days_after": 15, "what_to_show": "TRADES", "use_rth": True},
    }
    shards, assignments = build_shards(windows, config, start, end)
    with connect_cache(cache_path) as conn:
        ensure_cache_schema(conn)
        persist_plan(conn, windows, shards, assignments)
        for cluster in clusters:
            cluster_id = int(cluster["event_cluster_id"])
            anchor = parse_utc(str(cluster["anchor_at_utc"]))
            for asset in assets[cluster_id]:
                key = _stable_hash(["news-daily-anchor", cluster_id, asset["symbol"]])
                conn.execute(
                    """INSERT INTO ibkr_daily_event_anchors(
                           anchor_key,macro_event_id,event_cluster_id,source_system,event_type,release_name,
                           release_time_quality,intraday_label_eligible,symbol,anchor_at_utc,anchor_date,
                           horizon_1d_eligible,horizon_5d_eligible
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(anchor_key) DO UPDATE SET anchor_at_utc=excluded.anchor_at_utc""",
                    (
                        key,
                        cluster_id,
                        cluster_id,
                        str(cluster["provider_bucket"]),
                        str(cluster["event_class"]),
                        str(cluster["primary_headline"]),
                        "historical_news_visibility_policy",
                        1,
                        str(asset["symbol"]),
                        anchor.isoformat(),
                        anchor.date().isoformat(),
                        1,
                        1,
                    ),
                )
        conn.commit()
    return windows, shards, assignments, config


def collect_market_data(cache_path: Path, shards: list[BarShard]) -> dict[str, Any]:
    intraday_source = IBKRHistoricalIntradayPriceSource(
        host="127.0.0.1", port=4001, client_id=8611, timeout_seconds=40, what_to_show="TRADES", bar_size="1 min", use_rth=False
    )
    daily_source = IBKRHistoricalDailyPriceSource(
        host="127.0.0.1", port=4001, client_id=8612, timeout_seconds=40, what_to_show="TRADES", use_rth=True
    )
    errors: list[dict[str, Any]] = []
    requested = 0
    completed = 0
    symbols = sorted({shard.symbol for shard in shards})
    contracts: dict[str, Any] = {}
    try:
        with connect_cache(cache_path) as conn:
            for symbol in symbols:
                try:
                    contract = intraday_source.resolve_stock_contract(symbol)
                    contracts[symbol] = contract
                    _persist_contract(conn, symbol, contract)
                except Exception as exc:
                    errors.append({"symbol": symbol, "stage": "contract_resolution", "error": str(exc)})
            for shard in sorted(shards, key=lambda item: (item.data_kind, item.symbol, item.start_at_utc)):
                status = conn.execute("SELECT status FROM ibkr_bar_shards WHERE shard_key=?", (shard.shard_key,)).fetchone()
                if status and str(status[0]) == "completed":
                    completed += 1
                    continue
                if shard.symbol not in contracts:
                    conn.execute("UPDATE ibkr_bar_shards SET status='failed',last_error=? WHERE shard_key=?", ("contract_resolution_failed", shard.shard_key))
                    continue
                requested += 1
                last_error = ""
                for attempt in range(1, 3):
                    try:
                        conn.execute(
                            "UPDATE ibkr_bar_shards SET status='running',attempt_count=attempt_count+1,con_id=?,started_at_utc=? WHERE shard_key=?",
                            (int(contracts[shard.symbol].conId), datetime.now(UTC).isoformat(), shard.shard_key),
                        )
                        conn.commit()
                        if shard.data_kind == "daily":
                            rows = daily_source.get_daily_bars(shard.symbol, shard.start_at_utc.date(), shard.end_at_utc.date())
                        else:
                            rows = intraday_source.get_intraday_bars(shard.symbol, shard.start_at_utc, shard.end_at_utc)
                        inserted = _persist_bars(conn, shard, int(contracts[shard.symbol].conId), rows)
                        conn.execute(
                            """UPDATE ibkr_bar_shards SET status='completed',provider_row_count=?,inserted_row_count=inserted_row_count+?,
                                      completed_at_utc=?,last_error=NULL WHERE shard_key=?""",
                            (len(rows), inserted, datetime.now(UTC).isoformat(), shard.shard_key),
                        )
                        conn.commit()
                        completed += 1
                        last_error = ""
                        break
                    except Exception as exc:
                        last_error = str(exc)
                        sleep_time.sleep(1.0 * attempt)
                if last_error:
                    conn.execute("UPDATE ibkr_bar_shards SET status='failed',last_error=? WHERE shard_key=?", (last_error, shard.shard_key))
                    conn.commit()
                    errors.append({"symbol": shard.symbol, "stage": shard.data_kind, "shard_key": shard.shard_key, "error": last_error})
                sleep_time.sleep(0.15)
            persist_market_data_provenance(conn)
            snapshot = cache_snapshot(conn)
            failed = int(conn.execute("SELECT COUNT(*) FROM ibkr_bar_shards WHERE status='failed'").fetchone()[0])
    finally:
        intraday_source.close()
        daily_source.close()
    return {"symbols": len(symbols), "planned_shards": len(shards), "requested_shards": requested,
            "completed_shards": completed, "failed_shards": failed, "errors": errors, "snapshot": snapshot}


def create_plans(
    clone_path: Path,
    clusters: list[dict[str, Any]],
    assets: dict[int, list[dict[str, Any]]],
    cache_path: Path,
    *,
    plan_version: str = NEWS_PLAN_VERSION,
    core_scope_version: str = "news-stratified-core-spy-v1",
    event_proxy_map_version: str = "one-event-proxy-v1",
    reason: str = "clone-only stratified news pilot",
) -> tuple[dict[int, int], dict[tuple[int, str], dict[str, Any]]]:
    with sqlite3.connect(cache_path) as cache:
        cache.row_factory = sqlite3.Row
        con_ids = {str(row["symbol"]): int(row["con_id"]) for row in cache.execute("SELECT * FROM ibkr_contracts")}
    plan_ids: dict[int, int] = {}
    asset_lookup: dict[tuple[int, str], dict[str, Any]] = {}
    with sqlite3.connect(clone_path) as conn:
        conn.row_factory = sqlite3.Row
        ensure_pilot_schema(conn)
        now = datetime.now(UTC).isoformat()
        for cluster in clusters:
            cluster_id = int(cluster["event_cluster_id"])
            conn.execute(
                """INSERT OR IGNORE INTO reaction_universe_plans(
                       event_cluster_id,plan_version,created_at,available_at_cutoff,core_scope_version,
                       event_proxy_map_version,security_master_version,mapper_version,classifier_version,reason
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    cluster_id, plan_version, now, str(cluster["anchor_at_utc"]),
                    core_scope_version, event_proxy_map_version, "ibkr-dynamic-resolution-v1",
                    "m251-canonical-candidates", "s03-current-classification-v1", reason,
                ),
            )
            plan_id = int(conn.execute("SELECT reaction_plan_id FROM reaction_universe_plans WHERE event_cluster_id=? AND plan_version=?", (cluster_id, plan_version)).fetchone()[0])
            plan_ids[cluster_id] = plan_id
            for asset in assets[cluster_id]:
                if asset["symbol"] not in con_ids:
                    continue
                asset_lookup[(cluster_id, asset["symbol"])] = asset
                conn.execute(
                    """INSERT OR IGNORE INTO reaction_universe_assets(
                           reaction_plan_id,event_cluster_id,asset_symbol,con_id,asset_role,proxy_family,
                           inclusion_reason,inclusion_source,tradable_candidate,label_scope,created_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        plan_id,cluster_id,asset["symbol"],con_ids[asset["symbol"]],asset["asset_role"],asset["proxy_family"],
                        "stratified news pilot audited candidate",asset["inclusion_source"],1,asset["label_scope"],now,
                    ),
                )
        conn.commit()
    return plan_ids, asset_lookup


def build_labels(
    clone_path: Path,
    cache_path: Path,
    clusters: list[dict[str, Any]],
    plan_ids: dict[int, int],
    asset_lookup: dict[tuple[int, str], dict[str, Any]],
    corporate_snapshot: dict[str, Any],
) -> dict[str, Any]:
    market_data = load_market_data(cache_path)
    actions, action_version = _action_map(corporate_snapshot)
    events = {
        int(cluster["event_cluster_id"]): {
            "macro_event_id": int(cluster["event_cluster_id"]),
            "event_cluster_id": int(cluster["event_cluster_id"]),
            "source_system": str(cluster["provider_bucket"]),
            "subevent_type": str(cluster["event_class"]),
            "anchor_at_utc": str(cluster["anchor_at_utc"]),
            "release_time_quality": "historical_news_visibility_policy",
            "parent_meeting_id": f"news_cluster:{cluster['event_cluster_id']}",
            "release_bundle_id": f"news_cluster:{cluster['event_cluster_id']}",
            "overlap_group_id": f"news_cluster:{cluster['event_cluster_id']}",
            "overlap_weight": 1.0,
        }
        for cluster in clusters
    }
    start_date = min(parse_utc(str(cluster["anchor_at_utc"])).date() for cluster in clusters) - timedelta(days=15)
    end_date = max(parse_utc(str(cluster["anchor_at_utc"])).date() for cluster in clusters) + timedelta(days=20)
    schedule = calendar_schedule(start_date, end_date)
    with sqlite3.connect(clone_path) as conn:
        conn.row_factory = sqlite3.Row
        ensure_pilot_schema(conn)
        price_flags, price_quality = audit_price_quality(conn, market_data)
        label_ids: list[int] = []
        for window_row in market_data["windows"]:
            cluster_id = int(window_row["event_cluster_id"])
            symbol = str(window_row["symbol"])
            asset = asset_lookup.get((cluster_id, symbol))
            if not asset:
                continue
            event = events[cluster_id]
            anchor = parse_utc(str(window_row["anchor_at_utc"]))
            bars = market_data["intraday"].get(symbol, [])
            profile = market_data["profiles"][(symbol, "intraday")]["provenance_key"]
            for lag in STRICT_DECISION_LAGS_SECONDS:
                decision_time = anchor + timedelta(seconds=lag)
                target_entry = ceil_minute(decision_time)
                entry = _first_at_or_after(bars, target_entry)
                entry_delay = int((entry.at - decision_time).total_seconds()) if entry else None
                for window, minutes in INTRADAY_WINDOWS.items():
                    target_exit = target_entry + timedelta(minutes=minutes)
                    exit_bar = _first_at_or_after(bars, target_exit)
                    exit_delay = int((exit_bar.at - target_exit).total_seconds()) if exit_bar else None
                    observed = _bars_between(bars, entry.at, exit_bar.at) if entry and exit_bar else []
                    expected = minutes + 1
                    coverage = min(1.0, len(observed) / expected) if observed else 0.0
                    flags = _flag_values(price_flags, "intraday", symbol, observed)
                    exclusion = ""
                    if not entry or not exit_bar:
                        exclusion = "missing_entry_or_exit_trade"
                    elif entry_delay is None or exit_delay is None or entry_delay > 300 or exit_delay > 300:
                        exclusion = "trade_delay_exceeds_300s"
                    elif any(flags.values()):
                        exclusion = "price_quality_flag"
                    elif coverage < 0.20:
                        exclusion = "insufficient_observed_trade_coverage"
                    raw_return = ((exit_bar.close / entry.close) - 1.0) if entry and exit_bar and entry.close > 0 else None
                    closes = [bar.close for bar in observed]
                    values = {
                        "event_cluster_id": cluster_id,"reaction_plan_id": plan_ids[cluster_id],"asset_symbol": symbol,
                        "asset_role": asset["asset_role"],"proxy_family": asset["proxy_family"],"window": window,
                        "decision_lag_seconds": lag,"start_at_utc": entry.at.isoformat() if entry else target_entry.isoformat(),
                        "end_at_utc": exit_bar.at.isoformat() if exit_bar else target_exit.isoformat(),"session_bucket": _session_bucket(decision_time),
                        "raw_return": raw_return,"total_return_adjusted": raw_return,
                        "mfe": max((v / entry.close)-1 for v in closes) if closes and entry else None,
                        "mae": min((v / entry.close)-1 for v in closes) if closes and entry else None,
                        "market_data_profile_id": profile,"entry_trade_observed": int(entry is not None and entry.at==target_entry),
                        "exit_trade_observed": int(exit_bar is not None and exit_bar.at==target_exit),"entry_delay_seconds": entry_delay,
                        "exit_delay_seconds": exit_delay,"observed_trade_count": sum(bar.trade_count for bar in observed),
                        "observed_bar_count": len(observed),"window_trade_coverage_ratio": coverage,
                        "no_trade_minute_count": max(0,expected-len(observed)),"eth_sparse_trade_flag": int(_session_bucket(decision_time)!="regular" and coverage<0.8),
                        **flags,"price_quality_version": PRICE_QUALITY_VERSION,"price_quality_severity": _price_quality_severity(flags),
                        "label_quality_flag": "excluded" if exclusion else ("sparse_observed_trades" if coverage<0.8 else "pass"),
                        "label_quality_reason": exclusion or "observed_trades_only","label_exclusion_reason": exclusion,
                        "return_basis": "raw_price_and_total_return","dividend_contaminated": 0,"dividend_amount_in_window": 0.0,
                        "split_contaminated": 0,"corporate_action_source": CORPORATE_ACTION_SOURCE,"corporate_action_version": action_version,
                        "strict_training_eligible": int(not exclusion and coverage>=0.8),
                        "parent_meeting_id": event["parent_meeting_id"],"release_bundle_id": event["release_bundle_id"],
                        "overlap_group_id": event["overlap_group_id"],"subevent_type": event["subevent_type"],"sample_weight": 1.0,
                        "independent_sample_flag": 1,"validation_split_group": event["overlap_group_id"],"market_benchmark_symbol": "SPY",
                        "sector_benchmark_symbol": "SPY",
                        "residual_model_version": RESIDUAL_MODEL_VERSION,"return_math_version": RETURN_MATH_VERSION,
                        "decision_lag_policy": "ceil_to_next_full_minute_no_containing_bar",
                        "release_time_quality": "historical_news_visibility_policy","intraday_label_eligible": 1,
                        "horizon_anchor_policy": "ceil_to_next_full_minute_then_wall_clock","first_regular_session_date": "",
                        "entry_session_date": entry.session_date if entry else "","exit_session_date": exit_bar.session_date if exit_bar else "",
                        "execution_price_model": "observed_ibkr_trades_reaction_only_not_executable","spread_model_version": "not_estimated_v0",
                        "slippage_model_version": "not_estimated_v0","latency_model_version": LATENCY_MODEL_VERSION,
                        "cost_estimate_quality": "not_available_reaction_only","outcome_builder_version": NEWS_PILOT_VERSION,
                        "market_data_source": "ibkr_historical_trades","calendar_version": "XNYS-exchange-calendars","created_at": datetime.now(UTC).isoformat(),
                    }
                    label_ids.append(_upsert_label(conn, values))
        # Daily labels at lag 0.
        for anchor_row in market_data["anchors"]:
            cluster_id = int(anchor_row["event_cluster_id"]); symbol = str(anchor_row["symbol"])
            asset = asset_lookup.get((cluster_id,symbol))
            if not asset: continue
            anchor = parse_utc(str(anchor_row["anchor_at_utc"])); daily_dates,daily_map=_daily_by_session(market_data["daily"].get(symbol,[]))
            first_session = next((d.isoformat() for d in sorted(schedule) if schedule[d][1]>anchor and d.isoformat() in daily_map),"")
            if not first_session: continue
            first_index=daily_dates.index(first_session); entry=daily_map[first_session]; profile=market_data["profiles"][(symbol,"daily")]["provenance_key"]
            for window,offset in DAILY_WINDOWS.items():
                exit_session=daily_dates[first_index+offset] if first_index+offset<len(daily_dates) else ""; exit_bar=daily_map.get(exit_session)
                selected=[bar for bar in (entry,exit_bar) if bar]; flags=_flag_values(price_flags,"daily",symbol,selected)
                dividend,div_flag,split_flag=_actions_in_window(actions,symbol,first_session,exit_session) if exit_session else (0.0,False,False)
                exclusion="" if exit_bar and not any(flags.values()) and not split_flag else ("missing_daily_entry_or_exit" if not exit_bar else "price_or_split_quality_flag")
                raw_return=((exit_bar.close/entry.close)-1) if exit_bar and entry.close>0 else None
                values={
                    "event_cluster_id":cluster_id,"reaction_plan_id":plan_ids[cluster_id],"asset_symbol":symbol,"asset_role":asset["asset_role"],
                    "proxy_family":asset["proxy_family"],"window":window,"decision_lag_seconds":0,
                    "start_at_utc":schedule[date.fromisoformat(first_session)][1].isoformat(),
                    "end_at_utc":schedule[date.fromisoformat(exit_session)][1].isoformat() if exit_session else "",
                    "session_bucket":_session_bucket(anchor),"raw_return":raw_return,
                    "total_return_adjusted":((exit_bar.close+dividend)/entry.close-1) if exit_bar and entry.close>0 else None,
                    "market_data_profile_id":profile,"entry_trade_observed":1,"exit_trade_observed":int(exit_bar is not None),
                    "entry_delay_seconds":0,"exit_delay_seconds":0 if exit_bar else None,"observed_trade_count":sum(bar.trade_count for bar in selected),
                    "observed_bar_count":len(selected),"window_trade_coverage_ratio":1.0 if exit_bar else 0.0,"no_trade_minute_count":0,"eth_sparse_trade_flag":0,
                    **flags,"price_quality_version":PRICE_QUALITY_VERSION,"price_quality_severity":_price_quality_severity(flags),
                    "label_quality_flag":"excluded" if exclusion else ("corporate_action_adjusted" if div_flag else "pass"),
                    "label_quality_reason":exclusion or ("total_return_includes_dividend" if div_flag else "strict_session_close"),
                    "label_exclusion_reason":exclusion,"return_basis":"raw_price_and_total_return","dividend_contaminated":int(div_flag),
                    "dividend_amount_in_window":dividend,"split_contaminated":int(split_flag),"corporate_action_source":CORPORATE_ACTION_SOURCE,
                    "corporate_action_version":action_version,"strict_training_eligible":int(not exclusion),
                    "parent_meeting_id":f"news_cluster:{cluster_id}","release_bundle_id":f"news_cluster:{cluster_id}","overlap_group_id":f"news_cluster:{cluster_id}",
                    "subevent_type":str(next(c["event_class"] for c in clusters if int(c["event_cluster_id"])==cluster_id)),"sample_weight":1.0,
                    "independent_sample_flag":1,"validation_split_group":f"news_cluster:{cluster_id}","market_benchmark_symbol":"SPY","sector_benchmark_symbol":"SPY",
                    "residual_model_version":RESIDUAL_MODEL_VERSION,"return_math_version":RETURN_MATH_VERSION,"decision_lag_policy":"daily_lag0_only",
                    "release_time_quality":"historical_news_visibility_policy","intraday_label_eligible":1,"horizon_anchor_policy":f"first_regular_close_plus_{offset}_XNYS_sessions",
                    "first_regular_session_date":first_session,"entry_session_date":first_session,"exit_session_date":exit_session,
                    "execution_price_model":"observed_ibkr_trades_reaction_only_not_executable","spread_model_version":"not_estimated_v0","slippage_model_version":"not_estimated_v0",
                    "latency_model_version":LATENCY_MODEL_VERSION,"cost_estimate_quality":"not_available_reaction_only","outcome_builder_version":NEWS_PILOT_VERSION,
                    "market_data_source":"ibkr_historical_trades","calendar_version":"XNYS-exchange-calendars","created_at":datetime.now(UTC).isoformat(),
                }
                label_ids.append(_upsert_label(conn,values))
        label_math=populate_label_math(conn,market_data=market_data,events=events,plan_version=NEWS_PLAN_VERSION)
        bias_policy=apply_selection_bias_policy(conn,plan_version=NEWS_PLAN_VERSION)
        conn.commit()
        return {"labels_touched":len(set(label_ids)),"price_quality":price_quality,"label_math":label_math,"selection_bias_policy":bias_policy,"corporate_action_version":action_version}


def audit_news_pilot(clone_path: Path, cache_path: Path, selection: dict[str, Any], first_counts: dict[str,int], second_counts: dict[str,int], collection: dict[str,Any]) -> dict[str,Any]:
    with sqlite3.connect(clone_path) as conn:
        conn.row_factory=sqlite3.Row
        plan_clause="p.plan_version=?"; params=(NEWS_PLAN_VERSION,)
        counts={"plans":conn.execute("SELECT COUNT(*) FROM reaction_universe_plans p WHERE "+plan_clause,params).fetchone()[0],
                "assets":conn.execute("SELECT COUNT(*) FROM reaction_universe_assets a JOIN reaction_universe_plans p ON p.reaction_plan_id=a.reaction_plan_id WHERE "+plan_clause,params).fetchone()[0],
                "labels":conn.execute("SELECT COUNT(*) FROM event_reaction_labels l JOIN reaction_universe_plans p ON p.reaction_plan_id=l.reaction_plan_id WHERE "+plan_clause,params).fetchone()[0],
                "strict":conn.execute("SELECT COUNT(*) FROM event_reaction_labels l JOIN reaction_universe_plans p ON p.reaction_plan_id=l.reaction_plan_id WHERE "+plan_clause+" AND l.strict_training_eligible=1",params).fetchone()[0]}
        duplicate_labels=conn.execute("""SELECT COUNT(*) FROM (SELECT l.reaction_plan_id,l.asset_symbol,l.asset_role,l.proxy_family,l.window,l.decision_lag_seconds,COUNT(*) n FROM event_reaction_labels l JOIN reaction_universe_plans p ON p.reaction_plan_id=l.reaction_plan_id WHERE p.plan_version=? GROUP BY 1,2,3,4,5,6 HAVING n>1)""",params).fetchone()[0]
        route_direct=conn.execute("""SELECT COUNT(*) FROM reaction_universe_assets a JOIN reaction_universe_plans p ON p.reaction_plan_id=a.reaction_plan_id WHERE p.plan_version=? AND a.asset_role='direct_entity' AND a.inclusion_source IN ('ibkr_route','fallback_context')""",params).fetchone()[0]
        unresolved_labels=conn.execute("""SELECT COUNT(*) FROM event_reaction_labels l JOIN reaction_universe_plans p ON p.reaction_plan_id=l.reaction_plan_id LEFT JOIN reaction_universe_assets a ON a.reaction_plan_id=l.reaction_plan_id AND a.asset_symbol=l.asset_symbol AND a.asset_role=l.asset_role WHERE p.plan_version=? AND (a.tradable_candidate<>1 OR a.con_id IS NULL)""",params).fetchone()[0]
        strict_math_missing=conn.execute("""SELECT COUNT(*) FROM event_reaction_labels l JOIN reaction_universe_plans p ON p.reaction_plan_id=l.reaction_plan_id WHERE p.plan_version=? AND l.strict_training_eligible=1 AND (l.raw_return IS NULL OR l.market_adjusted_return IS NULL OR l.residual_return IS NULL OR l.return_zscore IS NULL OR l.volume_zscore IS NULL OR l.mfe IS NULL OR l.mae IS NULL)""",params).fetchone()[0]
        critical_strict=conn.execute("""SELECT COUNT(*) FROM event_reaction_labels l JOIN reaction_universe_plans p ON p.reaction_plan_id=l.reaction_plan_id WHERE p.plan_version=? AND l.strict_training_eligible=1 AND l.price_quality_severity='critical'""",params).fetchone()[0]
        rows=[dict(r) for r in conn.execute("""SELECT l.*,p.available_at_cutoff FROM event_reaction_labels l JOIN reaction_universe_plans p ON p.reaction_plan_id=l.reaction_plan_id WHERE p.plan_version=?""",params)]
        alignment=0
        for row in rows:
            if row["window"] not in INTRADAY_WINDOWS: continue
            target=ceil_minute(parse_utc(row["available_at_cutoff"])+timedelta(seconds=int(row["decision_lag_seconds"])))
            if parse_utc(row["start_at_utc"])<target: alignment+=1
        label_counts=[dict(r) for r in conn.execute("""SELECT l.window,l.session_bucket,l.label_quality_flag,COUNT(*) count FROM event_reaction_labels l JOIN reaction_universe_plans p ON p.reaction_plan_id=l.reaction_plan_id WHERE p.plan_version=? GROUP BY 1,2,3 ORDER BY 1,2,3""",params)]
        integrity=conn.execute("PRAGMA integrity_check").fetchone()[0]; foreign=len(conn.execute("PRAGMA foreign_key_check").fetchall())
    rerun={k:second_counts.get(k,0)-first_counts.get(k,0) for k in first_counts}
    checks={"selected_200_to_500":200<=selection["selected_count"]<=500,
            "market_data_shards_complete":collection["failed_shards"]==0 and collection["completed_shards"]==collection["planned_shards"],
            "route_or_fallback_direct_zero":route_direct==0,"unresolved_tradable_labels_zero":unresolved_labels==0,
            "arbitrary_second_alignment_zero":alignment==0,"duplicate_label_keys_zero":duplicate_labels==0,
            "strict_math_complete":strict_math_missing==0,"critical_bad_tick_strict_zero":critical_strict==0,
            "clone_rerun_diff_zero":all(v==0 for v in rerun.values()),"integrity_ok":integrity=="ok" and foreign==0}
    return {"verdict":"PASS" if all(checks.values()) else "FAIL","checks":checks,"counts":counts,"label_counts":label_counts,
            "route_or_fallback_direct":route_direct,"unresolved_tradable_labels":unresolved_labels,"alignment_violations":alignment,
            "duplicate_labels":duplicate_labels,"strict_math_missing":strict_math_missing,"critical_strict":critical_strict,"rerun_diff":rerun,
            "collection":collection,"selection_counts":selection["counts"],"integrity":integrity,"foreign_keys":foreign}


def markdown_report(report: dict[str, Any]) -> str:
    audit = report["audit"]
    selection = report["selection_summary"]
    lines = [
        "# S05 Stratified News Reaction-Label Pilot",
        "",
        f"- Verdict: **{report['verdict']}**",
        f"- Scope: {report['scope']}",
        f"- Selected clusters: **{selection['selected_count']}**",
        f"- Plans / assets / labels: **{audit['counts']['plans']} / {audit['counts']['assets']} / {audit['counts']['labels']}**",
        f"- Strict eligible labels: **{audit['counts']['strict']}**",
        f"- IBKR shards completed / planned / newly requested / failed: **{audit['collection']['completed_shards']} / {audit['collection']['planned_shards']} / {audit['collection']['requested_shards']} / {audit['collection']['failed_shards']}**",
        "",
        "## Hard gates",
        "",
    ]
    for key, value in audit["checks"].items():
        lines.append(f"- {key}: **{'PASS' if value else 'FAIL'}**")
    lines += [
        "",
        "## Selection coverage",
        "",
        f"- Provider buckets: `{json.dumps(selection['counts']['provider'], ensure_ascii=False, sort_keys=True)}`",
        f"- Session buckets: `{json.dumps(selection['counts']['session'], ensure_ascii=False, sort_keys=True)}`",
        f"- Exact-second clusters: **{selection['counts']['exact_second']}**",
        f"- Direct resolved clusters: **{selection['counts']['with_direct_resolved']}**",
        f"- Unresolved/ambiguous control clusters: **{selection['counts']['with_unresolved_or_ambiguous']}**",
        f"- Unavailable requested strata: `{json.dumps(selection.get('unavailable_requested_strata', []), ensure_ascii=False)}`",
        "",
        "`broadtape_no_direct_entity` has no eligible rows in the audited source window when listed above. It was not fabricated; the target size was completed with balanced provider/session rows.",
        "",
        "## Safety and scope boundary",
        "",
        "This was a clone-only research pilot. Full canonical news S05 was not promoted. M254/M255/M256, broker orders, transmit, live routing, and threshold promotion were not executed.",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser=argparse.ArgumentParser(description="Run clone-only stratified news S05 pilot")
    parser.add_argument("--canonical-db",type=Path,default=Path("work/canonical_research_backfill/m254_canonical_20250101_20260706_v1/canonical_market_events.db"))
    parser.add_argument("--selection",type=Path,default=Path("artifacts/audit/s05_news_stratified_pilot_selection.json"))
    parser.add_argument("--clone-db",type=Path,default=Path("work/s05_news_stratified_pilot/news_240_v1/canonical_market_events_news_pilot.db"))
    parser.add_argument("--cache-db",type=Path,default=Path("work/s05_news_stratified_pilot/news_240_v1/ibkr_news_market_data_cache.db"))
    parser.add_argument("--json-output",type=Path,default=Path("artifacts/audit/s05_news_stratified_pilot.json"))
    parser.add_argument("--markdown-output",type=Path,default=Path("artifacts/audit/s05_news_stratified_pilot.md"))
    args=parser.parse_args()
    selection=json.loads(args.selection.read_text(encoding="utf-8")); clusters=selection["clusters"]
    sqlite_clone(args.canonical_db,args.clone_db)
    assets=select_assets(args.canonical_db,clusters)
    windows,shards,_,_=create_cache_plan(args.cache_db,clusters,assets)
    collection=collect_market_data(args.cache_db,shards)
    resolved_symbols={row["symbol"] for values in assets.values() for row in values}
    actions=fetch_yahoo_corporate_actions(sorted(resolved_symbols),min(parse_utc(c["anchor_at_utc"]).date() for c in clusters)-timedelta(days=15),max(parse_utc(c["anchor_at_utc"]).date() for c in clusters)+timedelta(days=20))
    plan_ids,asset_lookup=create_plans(args.clone_db,clusters,assets,args.cache_db)
    first_build=build_labels(args.clone_db,args.cache_db,clusters,plan_ids,asset_lookup,actions)
    with sqlite3.connect(args.clone_db) as conn: first_counts={t:conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("reaction_universe_plans","reaction_universe_assets","event_reaction_labels","s05_market_data_price_quality_flags")}
    second_build=build_labels(args.clone_db,args.cache_db,clusters,plan_ids,asset_lookup,actions)
    with sqlite3.connect(args.clone_db) as conn: second_counts={t:conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in first_counts}
    audit=audit_news_pilot(args.clone_db,args.cache_db,selection,first_counts,second_counts,collection)
    report={"version":NEWS_PILOT_VERSION,"verdict":audit["verdict"],"scope":"240 stratified news clusters, clone only","selection":str(args.selection),
            "selection_summary":{"selected_count":selection["selected_count"],"counts":selection["counts"],
                                 "stratum_availability":selection.get("stratum_availability",{}),
                                 "unavailable_requested_strata":selection.get("unavailable_requested_strata",[])},
            "clone_db":str(args.clone_db),"cache_db":str(args.cache_db),"first_build":first_build,"second_build":second_build,"audit":audit,
            "safety":{"canonical_news_s05":False,"m254_m255_m256":False,"orders":False,"transmit":False,"routing":False,"promotion":False}}
    args.json_output.parent.mkdir(parents=True,exist_ok=True); args.json_output.write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding="utf-8")
    args.markdown_output.write_text(markdown_report(report),encoding="utf-8")
    print(json.dumps({"verdict":report["verdict"],"counts":audit["counts"],"checks":audit["checks"],"collection":collection,"report":str(args.json_output)},indent=2))
    return 0 if report["verdict"]=="PASS" else 2


if __name__=="__main__": raise SystemExit(main())
