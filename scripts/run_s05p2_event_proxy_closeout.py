from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.run_s05_news_stratified_pilot as base
from recorder.market_outcomes import IBKRHistoricalIntradayPriceSource
from recorder.s04_market_data_pre_s05_audit import fetch_yahoo_corporate_actions, parse_utc
from recorder.s05_event_proxy_closeout import (
    EVENT_PROXY_MAP_VERSION,
    PLAN_VERSION,
    SOURCE_COVERAGE_PROFILE_ID,
    assets_for_selection,
    ensure_s05p2_schema,
    has_outcome_leakage_headline,
    persist_broadtape_probe_shadow,
    persist_s05p2_lineage,
    select_proxy_clusters,
)
from scripts.run_s05p1_news_label_pilot import collect_market_data_parallel


PILOT_VERSION = "s05p2-conditional-event-proxy-closeout-v2"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_proxy_contracts(assets: dict[int, list[dict[str, Any]]], host: str, port: int) -> dict[str, Any]:
    source = IBKRHistoricalIntradayPriceSource(
        host=host, port=port, client_id=8721, timeout_seconds=45,
        what_to_show="TRADES", bar_size="1 min", use_rth=False,
    )
    resolved: dict[str, int] = {}
    metadata: dict[str, dict[str, Any]] = {}
    errors: dict[str, str] = {}
    try:
        for symbol in sorted({row["symbol"] for values in assets.values() for row in values}):
            try:
                contract = source.resolve_stock_contract(symbol)
                resolved[symbol] = int(contract.conId)
                metadata[symbol] = {
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
    for values in assets.values():
        for row in values:
            if row["symbol"] in resolved:
                row["resolver_status"] = "resolved"
                row["con_id"] = resolved[row["symbol"]]
    return {"resolved_symbols": resolved, "resolved_contracts": metadata, "errors": errors}


def scoped_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        "plans": int(conn.execute("SELECT COUNT(*) FROM reaction_universe_plans WHERE plan_version=?", (PLAN_VERSION,)).fetchone()[0]),
        "assets": int(conn.execute("""SELECT COUNT(*) FROM reaction_universe_assets a JOIN reaction_universe_plans p
                                      ON p.reaction_plan_id=a.reaction_plan_id WHERE p.plan_version=?""", (PLAN_VERSION,)).fetchone()[0]),
        "labels": int(conn.execute("""SELECT COUNT(*) FROM event_reaction_labels l JOIN reaction_universe_plans p
                                      ON p.reaction_plan_id=l.reaction_plan_id WHERE p.plan_version=?""", (PLAN_VERSION,)).fetchone()[0]),
        "strict": int(conn.execute("""SELECT COUNT(*) FROM event_reaction_labels l JOIN reaction_universe_plans p
                                      ON p.reaction_plan_id=l.reaction_plan_id WHERE p.plan_version=?
                                      AND l.strict_training_eligible=1""", (PLAN_VERSION,)).fetchone()[0]),
        "assignments": int(conn.execute("SELECT COUNT(*) FROM s05p2_event_proxy_assignments").fetchone()[0]),
        "coverage": int(conn.execute("SELECT COUNT(*) FROM s05p2_event_source_coverage").fetchone()[0]),
    }


def audit(
    clone_db: Path,
    canonical_db: Path,
    selection: dict[str, Any],
    collection: dict[str, Any],
    first_counts: dict[str, int],
    second_counts: dict[str, int],
    shadow: dict[str, Any],
    canonical_hash: str,
    s05p1_clone: Path,
) -> dict[str, Any]:
    with sqlite3.connect(clone_db) as conn:
        conn.row_factory = sqlite3.Row
        counts = scoped_counts(conn)
        expected_missing = int(conn.execute(
            """SELECT COUNT(*) FROM reaction_universe_plans p WHERE p.plan_version=? AND NOT EXISTS(
                   SELECT 1 FROM reaction_universe_assets a WHERE a.reaction_plan_id=p.reaction_plan_id AND a.asset_role='event_proxy')""",
            (PLAN_VERSION,),
        ).fetchone()[0])
        future_map = int(conn.execute(
            "SELECT COUNT(*) FROM s05p2_event_proxy_assignments WHERE plan_as_of_utc<map_valid_from OR plan_as_of_utc>=map_valid_to"
        ).fetchone()[0])
        proxy_as_core = int(conn.execute(
            """SELECT COUNT(*) FROM reaction_universe_assets a JOIN reaction_universe_plans p ON p.reaction_plan_id=a.reaction_plan_id
                 WHERE p.plan_version=? AND a.inclusion_source='event_proxy_map' AND a.asset_role<>'event_proxy'""",
            (PLAN_VERSION,),
        ).fetchone()[0])
        causal_ineligible_strict = int(conn.execute(
            """SELECT COUNT(*) FROM event_reaction_labels l JOIN s05p2_event_proxy_assignments x
                   ON x.reaction_plan_id=l.reaction_plan_id AND x.asset_symbol=l.asset_symbol
                 WHERE x.proxy_causal_eligible=0 AND l.strict_training_eligible=1"""
        ).fetchone()[0])
        duplicate_proxy = int(conn.execute(
            """SELECT COUNT(*) FROM (SELECT reaction_plan_id,asset_symbol,proxy_rule_id,COUNT(*) n
                 FROM s05p2_event_proxy_assignments GROUP BY 1,2,3 HAVING n>1)"""
        ).fetchone()[0])
        route_contamination = int(conn.execute(
            "SELECT COUNT(*) FROM s05p2_event_proxy_assignments WHERE inclusion_source<>'event_proxy_map'"
        ).fetchone()[0])
        diagnostic_plan_contamination = int(conn.execute(
            """SELECT COUNT(*) FROM reaction_universe_assets a JOIN reaction_universe_plans p
                 ON p.reaction_plan_id=a.reaction_plan_id WHERE p.plan_version=? AND a.asset_role<>'event_proxy'""",
            (PLAN_VERSION,),
        ).fetchone()[0])
        lineage_missing = int(conn.execute(
            """SELECT COUNT(*) FROM s05p2_event_proxy_assignments WHERE asset_role<>'event_proxy' OR proxy_family=''
                 OR event_proxy_map_version='' OR proxy_rule_id='' OR inclusion_source='' OR inclusion_reason=''
                 OR map_valid_from='' OR map_valid_to='' OR plan_as_of_utc=''"""
        ).fetchone()[0])
        coverage_bad = int(conn.execute(
            """SELECT COUNT(*) FROM s05p2_event_source_coverage WHERE source_coverage_profile_id<>?
                 OR broadtape_available_at_event<>0 OR broadtape_observed<>0 OR available_provider_count<>3
                 OR confirmation_ratio<0 OR confirmation_ratio>1""",
            (SOURCE_COVERAGE_PROFILE_ID,),
        ).fetchone()[0])
        duplicate_labels = int(conn.execute(
            """SELECT COUNT(*) FROM (SELECT l.reaction_plan_id,l.asset_symbol,l.asset_role,l.proxy_family,
                 l.window,l.decision_lag_seconds,COUNT(*) n FROM event_reaction_labels l
                 JOIN reaction_universe_plans p ON p.reaction_plan_id=l.reaction_plan_id WHERE p.plan_version=?
                 GROUP BY 1,2,3,4,5,6 HAVING n>1)""", (PLAN_VERSION,)
        ).fetchone()[0])
        family_matrix = [dict(row) for row in conn.execute(
            """SELECT x.proxy_family,COUNT(DISTINCT x.reaction_plan_id) plans,COUNT(l.reaction_label_id) labels,
                 COALESCE(SUM(l.strict_training_eligible),0) strict FROM s05p2_event_proxy_assignments x
                 LEFT JOIN event_reaction_labels l ON l.reaction_plan_id=x.reaction_plan_id AND l.asset_symbol=x.asset_symbol
                 GROUP BY x.proxy_family ORDER BY x.proxy_family"""
        )]
        event_class_matrix = [dict(row) for row in conn.execute(
            """SELECT c.event_class,COUNT(DISTINCT x.reaction_plan_id) plans FROM s05p2_event_proxy_assignments x
                 JOIN news_event_clusters c ON c.event_cluster_id=x.event_cluster_id GROUP BY c.event_class ORDER BY plans DESC"""
        )]
        label_rows = [dict(row) for row in conn.execute(
            """SELECT l.*,p.event_cluster_id FROM event_reaction_labels l JOIN reaction_universe_plans p
                 ON p.reaction_plan_id=l.reaction_plan_id WHERE p.plan_version=?""", (PLAN_VERSION,)
        )]
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = len(conn.execute("PRAGMA foreign_key_check").fetchall())
    meta = {int(row["event_cluster_id"]): row for row in selection["clusters"]}
    matrix_specs = {
        "provider": lambda row: (meta[int(row["event_cluster_id"])]["provider_bucket"],),
        "session": lambda row: (str(row["session_bucket"]),),
        "content_type": lambda row: (meta[int(row["event_cluster_id"])]["semantic_news_content_type"],),
        "event_class": lambda row: (meta[int(row["event_cluster_id"])]["event_class"],),
        "asset_role": lambda row: (str(row["asset_role"]),),
        "proxy_family": lambda row: (meta[int(row["event_cluster_id"])]["proxy_family"],),
        "window_lag": lambda row: (str(row["window"]), int(row["decision_lag_seconds"])),
    }
    strict_matrices: dict[str, list[dict[str, Any]]] = {}
    for name, key_fn in matrix_specs.items():
        totals: Counter[tuple[Any, ...]] = Counter()
        strict: Counter[tuple[Any, ...]] = Counter()
        for row in label_rows:
            key = key_fn(row)
            totals[key] += 1
            strict[key] += int(row["strict_training_eligible"])
        strict_matrices[name] = [
            {"bucket": list(key), "count": totals[key], "strict": strict[key], "excluded": totals[key] - strict[key]}
            for key in sorted(totals, key=lambda value: tuple(str(item) for item in value))
        ]
    with sqlite3.connect(f"file:{s05p1_clone.as_posix()}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        existing_roles = [dict(row) for row in conn.execute(
            """SELECT a.asset_role,COUNT(*) count,COUNT(DISTINCT a.reaction_plan_id) plans
                 FROM reaction_universe_assets a JOIN reaction_universe_plans p ON p.reaction_plan_id=a.reaction_plan_id
                WHERE p.plan_version='m253-s05p1-news-semantic-initial-plan-v1'
                GROUP BY a.asset_role ORDER BY count DESC"""
        )]
        existing_special = {
            role: int(conn.execute(
                """SELECT COUNT(*) FROM reaction_universe_assets a JOIN reaction_universe_plans p
                     ON p.reaction_plan_id=a.reaction_plan_id WHERE p.plan_version='m253-s05p1-news-semantic-initial-plan-v1'
                     AND a.asset_role=?""", (role,)
            ).fetchone()[0])
            for role in ("event_proxy", "sector_context", "residual_pair")
        }
    with sqlite3.connect(f"file:{canonical_db.as_posix()}?mode=ro", uri=True) as conn:
        canonical_news_plans = int(conn.execute("SELECT COUNT(*) FROM reaction_universe_plans WHERE plan_version=?", (PLAN_VERSION,)).fetchone()[0])
        m254plus = int(conn.execute(
            "SELECT COUNT(*) FROM research_backfill_stage_runs WHERE stage_name LIKE 'M254%' OR stage_name LIKE 'M255%' OR stage_name LIKE 'M256%'"
        ).fetchone()[0])
    rerun = {key: second_counts[key] - first_counts[key] for key in first_counts}
    outcome_leakage_rows = [
        {"event_cluster_id": row["event_cluster_id"], "headline": row["primary_headline"]}
        for row in selection["clusters"] if has_outcome_leakage_headline(row["primary_headline"])
    ]
    checks = {
        "selected_100_to_200": 100 <= selection["selected_count"] <= 200,
        "all_proxy_families_covered": len(selection["family_counts"]) == 8,
        "outcome_leakage_selection_zero": len(outcome_leakage_rows) == 0,
        "expected_event_proxy_missing_zero": expected_missing == 0,
        "event_proxy_asset_count_positive": counts["assets"] > 0,
        "event_proxy_recorded_as_core_macro_zero": proxy_as_core == 0,
        "future_proxy_map_usage_zero": future_map == 0,
        "causal_ineligible_strict_event_proxy_zero": causal_ineligible_strict == 0,
        "duplicate_proxy_asset_per_plan_zero": duplicate_proxy == 0,
        "route_fallback_proxy_contamination_zero": route_contamination == 0,
        "diagnostic_benchmark_plan_contamination_zero": diagnostic_plan_contamination == 0,
        "proxy_lineage_missing_zero": lineage_missing == 0,
        "source_coverage_complete": coverage_bad == 0 and counts["coverage"] == counts["plans"],
        "market_data_shards_complete": (
            collection["completed_shards"] >= collection["planned_shards"]
            and collection["failed_shards"] == 0
            and collection.get("pending_shards", 0) == 0
            and collection.get("requested_no_data_unclassified", 0) == 0
        ),
        "duplicate_label_groups_zero": duplicate_labels == 0,
        "same_config_rerun_diff_zero": all(value == 0 for value in rerun.values()),
        "shadow_rerun_diff_zero": all(value == 0 for value in shadow["rerun_diff"].values()),
        "shadow_historical_contamination_zero": shadow["contamination_rows"] == 0 and not shadow["historical_training_mixed"],
        "clone_integrity_ok": integrity == "ok" and foreign_keys == 0,
        "canonical_source_unchanged": sha256(canonical_db) == canonical_hash,
        "canonical_news_not_promoted": canonical_news_plans == 0,
        "m254_m255_m256_not_started": m254plus == 0,
    }
    return {
        "verdict": "PASS" if all(checks.values()) else "FAIL",
        "checks": checks,
        "counts": counts,
        "selection": {"selected": selection["selected_count"], "family_counts": selection["family_counts"], "period_counts": selection["period_counts"]},
        "outcome_leakage_rows": outcome_leakage_rows,
        "s05p1_existing_role_audit": {"roles": existing_roles, "special_roles": existing_special},
        "collection": collection,
        "family_matrix": family_matrix,
        "event_class_matrix": event_class_matrix,
        "strict_matrices": strict_matrices,
        "rerun_diff": rerun,
        "shadow": shadow,
        "integrity": integrity,
        "foreign_key_violations": foreign_keys,
        "canonical_news_plans": canonical_news_plans,
        "m254plus_stage_rows": m254plus,
    }


def markdown(report: dict[str, Any]) -> str:
    audit = report["audit"]
    lines = [
        "# S05P2 Conditional Event-Proxy Closeout",
        "",
        "## Executive summary",
        "",
        f"The clone-only event-proxy closeout verdict is **{report['verdict']}**.",
        f"It selected **{audit['selection']['selected']}** point-in-time clusters and produced "
        f"**{audit['counts']['plans']} plans / {audit['counts']['assets']} event proxies / "
        f"{audit['counts']['labels']} labels / {audit['counts']['strict']} strict labels**.",
        "Selection does not use post-event market moves. Full canonical news promotion remains HOLD pending user review.",
        "",
        "## Existing S05P1 role audit",
        "",
        "| Role | Assets | Plans |",
        "| --- | ---: | ---: |",
    ]
    lines.extend(
        f"| {row['asset_role']} | {row['count']} | {row['plans']} |"
        for row in audit["s05p1_existing_role_audit"]["roles"]
    )
    special = audit["s05p1_existing_role_audit"]["special_roles"]
    lines += [
        "",
        f"S05P1 event_proxy / sector_context / residual_pair: **{special['event_proxy']} / "
        f"{special['sector_context']} / {special['residual_pair']}**.",
        "S05P2 closes only the conditional event_proxy scope; it does not relabel proxies as core assets.",
        "",
        "## Selection and family results",
        "",
        f"- Periods: `{json.dumps(audit['selection']['period_counts'], sort_keys=True)}`",
        f"- Families: `{json.dumps(audit['selection']['family_counts'], sort_keys=True)}`",
        f"- Map version: `{EVENT_PROXY_MAP_VERSION}`",
        f"- Source coverage profile: `{SOURCE_COVERAGE_PROFILE_ID}`",
        "",
        "| Proxy family | Plans | Labels | Strict |",
        "| --- | ---: | ---: | ---: |",
    ]
    lines.extend(
        f"| {row['proxy_family']} | {row['plans']} | {row['labels']} | {row['strict']} |"
        for row in audit["family_matrix"]
    )
    lines += [
        "",
        "## Hard gates",
        "",
    ]
    lines.extend(f"- {key}: **{'PASS' if value else 'FAIL'}**" for key, value in audit["checks"].items())
    lines += [
        "",
        "## Market data and idempotency",
        "",
        f"- Current-plan required shards: **{audit['collection']['planned_shards']}**",
        f"- Cache cumulative completed/pending/failed: **{audit['collection']['completed_shards']} / "
        f"{audit['collection']['pending_shards']} / {audit['collection']['failed_shards']}**",
        f"- Historical bars in isolated cache: **{audit['collection']['snapshot']['ibkr_historical_bars']}**",
        f"- Same-config canonical rerun diff: `{json.dumps(audit['rerun_diff'], sort_keys=True)}`",
        "",
        "## Strict/excluded matrices",
        "",
    ]
    for matrix_name, rows in audit["strict_matrices"].items():
        lines += [
            f"### {matrix_name}",
            "",
            "| Bucket | Count | Strict | Excluded |",
            "| --- | ---: | ---: | ---: |",
        ]
        lines.extend(
            f"| {' / '.join(str(value) for value in row['bucket'])} | {row['count']} | "
            f"{row['strict']} | {row['excluded']} |"
            for row in rows
        )
        lines.append("")
    lines += [
        "## Coverage and safety decision",
        "",
        "- Historical BroadTape: unavailable, excluded from training, not negative evidence.",
        f"- Live BroadTape shadow DB: `{audit['shadow']['database']}`; raw={audit['shadow']['second']['raw']}, proxies={audit['shadow']['second']['proxies']}.",
        f"- Shadow rerun diff: `{json.dumps(audit['shadow']['rerun_diff'], sort_keys=True)}`; "
        f"historical contamination rows={audit['shadow']['contamination_rows']}.",
        "- Full canonical news S05 was not promoted. M254/M255/M256 were not started.",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run clone-only S05P2 conditional event-proxy closeout")
    parser.add_argument("--canonical-db", type=Path, default=Path("work/canonical_research_backfill/m254_canonical_20250101_20260706_v1/canonical_market_events.db"))
    parser.add_argument("--clone-db", type=Path, default=Path("work/s05p2_event_proxy_closeout/pilot_100_v2/canonical_market_events_s05p2_clone.db"))
    parser.add_argument("--cache-db", type=Path, default=Path("work/s05p2_event_proxy_closeout/pilot_100_v1/ibkr_s05p2_proxy_cache.db"))
    parser.add_argument("--shadow-db", type=Path, default=Path("work/s05p2_event_proxy_closeout/live_broadtape_shadow_v2.db"))
    parser.add_argument("--broadtape-probe", type=Path, default=Path("artifacts/audit/s05p1_broadtape_live_forward_smoke.json"))
    parser.add_argument("--s05p1-clone", type=Path, default=Path("work/s05p1_news_label_pilot/time_stratified_300_v1/canonical_market_events_s05p1_label_clone.db"))
    parser.add_argument("--selection-output", type=Path, default=Path("artifacts/audit/s05p2_event_proxy_selection.json"))
    parser.add_argument("--json-output", type=Path, default=Path("artifacts/audit/s05p2_event_proxy_closeout.json"))
    parser.add_argument("--markdown-output", type=Path, default=Path("artifacts/audit/s05p2_event_proxy_closeout.md"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4001)
    parser.add_argument("--target", type=int, default=100)
    args = parser.parse_args()

    canonical_hash = sha256(args.canonical_db)
    selection = select_proxy_clusters(args.canonical_db, args.target)
    args.selection_output.parent.mkdir(parents=True, exist_ok=True)
    args.selection_output.write_text(json.dumps(selection, indent=2, ensure_ascii=False), encoding="utf-8")
    base.sqlite_clone(args.canonical_db, args.clone_db)
    assets = assets_for_selection(selection)
    cache_assets = {cluster_id: [dict(row) for row in values] for cluster_id, values in assets.items()}
    for values in cache_assets.values():
        for symbol in ("SPY", "QQQ", "IWM"):
            values.append({"symbol": symbol, "asset_role": "diagnostic_benchmark", "proxy_family": "no_trade_quorum"})
    resolution = resolve_proxy_contracts(cache_assets, args.host, args.port)
    if resolution["errors"]:
        raise RuntimeError(f"event proxy contract resolution failed: {resolution['errors']}")
    windows, shards, _assignments, _config = base.create_cache_plan(args.cache_db, selection["clusters"], cache_assets)
    collection = collect_market_data_parallel(args.cache_db, shards, resolution, host=args.host, port=args.port, workers=8)
    with sqlite3.connect(args.cache_db) as cache_conn:
        status_counts = {
            str(status): int(count)
            for status, count in cache_conn.execute("SELECT status,COUNT(*) FROM ibkr_bar_shards GROUP BY status")
        }
    collection["cache_status_counts"] = status_counts
    collection["pending_shards"] = int(status_counts.get("pending", 0) + status_counts.get("running", 0))
    base.NEWS_PLAN_VERSION = PLAN_VERSION
    base.NEWS_PILOT_VERSION = PILOT_VERSION
    plan_ids, asset_lookup = base.create_plans(args.clone_db, selection["clusters"], assets, args.cache_db)
    with sqlite3.connect(args.clone_db) as conn:
        conn.row_factory = sqlite3.Row
        persist_s05p2_lineage(conn, selection, plan_ids)
    symbols = sorted(resolution["resolved_symbols"])
    start = min(parse_utc(row["anchor_at_utc"]).date() for row in selection["clusters"]) - timedelta(days=15)
    end = max(parse_utc(row["anchor_at_utc"]).date() for row in selection["clusters"]) + timedelta(days=20)
    corporate = fetch_yahoo_corporate_actions(symbols, start, end)
    first_build = base.build_labels(args.clone_db, args.cache_db, selection["clusters"], plan_ids, asset_lookup, corporate)
    with sqlite3.connect(args.clone_db) as conn:
        conn.row_factory = sqlite3.Row
        persist_s05p2_lineage(conn, selection, plan_ids)
        first_counts = scoped_counts(conn)
    second_build = base.build_labels(args.clone_db, args.cache_db, selection["clusters"], plan_ids, asset_lookup, corporate)
    with sqlite3.connect(args.clone_db) as conn:
        conn.row_factory = sqlite3.Row
        persist_s05p2_lineage(conn, selection, plan_ids)
        second_counts = scoped_counts(conn)
    shadow = persist_broadtape_probe_shadow(args.broadtape_probe, args.shadow_db)
    result = audit(args.clone_db, args.canonical_db, selection, collection, first_counts, second_counts, shadow, canonical_hash, args.s05p1_clone)
    report = {
        "version": PILOT_VERSION,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "verdict": result["verdict"],
        "scope": SOURCE_COVERAGE_PROFILE_ID,
        "clone_database": str(args.clone_db),
        "cache_database": str(args.cache_db),
        "first_build": first_build,
        "second_build": second_build,
        "resolution": resolution,
        "audit": result,
        "safety": {"canonical_news_promoted": False,"m254_m255_m256": False,"orders": False,"transmit": False,"live_routing": False,"threshold_promotion": False},
    }
    args.json_output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    args.markdown_output.write_text(markdown(report), encoding="utf-8")
    print(json.dumps({"verdict": report["verdict"], "counts": result["counts"], "selection": result["selection"], "checks": result["checks"], "collection": collection}, indent=2))
    return 0 if report["verdict"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
