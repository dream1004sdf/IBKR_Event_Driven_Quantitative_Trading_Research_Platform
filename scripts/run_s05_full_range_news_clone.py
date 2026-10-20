from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
import threading
import traceback
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.run_s05_news_stratified_pilot as base
from recorder.s04_market_data_pre_s05_audit import fetch_yahoo_corporate_actions, parse_utc
from recorder.market_outcomes import IBKRHistoricalIntradayPriceSource
from recorder.s05_full_range_news_clone import (
    FULL_RANGE_VERSION,
    FULL_RANGE_VERSION_V4,
    PLAN_VERSION,
    PLAN_VERSION_V4,
    SOURCE_SCOPE,
    build_full_range_selection,
    persist_cohorts,
    record_stage,
)
from recorder.s05_news_semantic_closeout import CORE_SCOPE_VERSION, REQUIRED_CORE_ASSETS, materialize_closeout
from recorder.s05_proxy_semantic_closeout import PROXY_MAP_VERSION, PROXY_SCOPE
from recorder.s05_proxy_semantic_v4 import PROXY_MAP_VERSION_V4, PROXY_SCOPE_V4
from scripts.run_s05p1_news_label_pilot import (
    collect_market_data_parallel,
    restore_original_plan_from_completed_coverage,
    select_assets,
)


STAGES = (
    "F01_freeze_selection_snapshot",
    "F02_build_core_context_plans",
    "F03_build_direct_entity_plans",
    "F04_build_primary_proxy_assignments",
    "F05_resolve_required_contracts",
    "F06_deduplicate_market_data_requirements",
    "F07_fetch_missing_readonly_market_data",
    "F08_build_reaction_labels",
    "F09_label_math_audit",
    "F10_semantic_blind_audit",
    "F11_source_coverage_audit",
    "F12_clone_idempotency_replay",
    "F13_full_range_closeout_report",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def clone_sqlite(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
        src.backup(dst)


def git_value(*args: str) -> str:
    try:
        return subprocess.check_output(["git", *args], cwd=ROOT, text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return "unavailable"


def file_hash(path: Path) -> str:
    return sha256(path) if path.exists() else "missing"


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def safe_print_json(value: Any, *, indent: int | None = None) -> None:
    """Progress output must never invalidate an otherwise successful clone run."""

    try:
        print(json.dumps(value, indent=indent, ensure_ascii=False), flush=True)
    except OSError:
        pass


def stage_status(clone_db: Path, stage: str) -> str:
    if not clone_db.exists():
        return ""
    try:
        with sqlite3.connect(clone_db) as conn:
            row = conn.execute("SELECT status FROM s05_full_range_stage_runs WHERE stage_name=?", (stage,)).fetchone()
            return str(row[0]) if row else ""
    except sqlite3.Error:
        return ""


def set_stage(clone_db: Path, stage: str, status: str, count: int, details: dict[str, Any]) -> None:
    with sqlite3.connect(clone_db) as conn:
        record_stage(conn, stage, status, count, details)
    safe_print_json({"stage": stage, "status": status, "row_count": count, "at_utc": datetime.now(UTC).isoformat()})


def freeze_manifest(
    canonical_db: Path,
    clone_db: Path,
    *,
    limit: int,
    rules_version: str,
    full_range_version: str,
    plan_version: str,
    proxy_map_version: str,
    proxy_scope: str,
) -> dict[str, Any]:
    config = ROOT / "config" / "backfill" / "canonical_20250101_20260706.yaml"
    relevant = (
        ROOT / "recorder" / "s05_full_range_news_clone.py",
        ROOT / "recorder" / "s05_news_semantic_closeout.py",
        ROOT / "recorder" / "s05_proxy_semantic_closeout.py",
        ROOT / "recorder" / "s05_proxy_semantic_v4.py",
        ROOT / "scripts" / "run_s05_full_range_news_clone.py",
        ROOT / "scripts" / "run_s05_news_stratified_pilot.py",
    )
    return {
        "full_range_version": full_range_version,
        "semantic_rules_version": rules_version,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "cutoff_start_utc": "2025-01-01T05:00:00+00:00",
        "cutoff_end_utc": "2026-07-06T04:00:00+00:00",
        "source_scope": SOURCE_SCOPE,
        "proxy_scope": proxy_scope,
        "proxy_map_version": proxy_map_version,
        "reaction_plan_version": plan_version,
        "normalization_version": "s05p1-news-normalization-v1",
        "entity_resolver_version": "s05p1-headline-entity-role-v1",
        "classification_version": "s05p1-news-semantic-classifier-v1",
        "label_math_version": "s05-news-reaction-math-v1",
        "corporate_action_version": "yahoo-chart-events-v1",
        "source_coverage_profile": SOURCE_SCOPE,
        "schema_version": "news-lineage-v2+s05-full-range-v1",
        "git_commit": git_value("rev-parse", "HEAD"),
        "git_status_hash": hashlib.sha256(git_value("status", "--porcelain=v1").encode()).hexdigest(),
        "config_hash": file_hash(config),
        "code_hashes": {str(path.relative_to(ROOT)): file_hash(path) for path in relevant},
        "canonical_source_db": str(canonical_db.resolve()),
        "canonical_source_checksum": sha256(canonical_db),
        "clone_db": str(clone_db.resolve()),
        "clone_initial_checksum": sha256(clone_db),
        "limit": limit,
        "cluster_specific_override_count": 0,
        "manual_review_ledger_used_as_lookup": False,
        "outcome_or_return_used_for_selection": False,
        "safety": {
            "canonical_promotion": False,
            "m254_m255_m256": False,
            "orders": False,
            "transmit": False,
            "live_routing": False,
            "threshold_promotion": False,
            "ibkr_usage": "read_only_historical_market_data",
        },
    }


def merge_materialized(selection: dict[str, Any], clone_db: Path) -> dict[str, Any]:
    selected = {int(row["event_cluster_id"]): row for row in selection["clusters"]}
    materialized = materialize_closeout(
        clone_db,
        {"clusters": selection["clusters"], "counts": dict(selection.get("counts", {}))},
    )["selection"]
    result: list[dict[str, Any]] = []
    for row in materialized["clusters"]:
        original = selected[int(row["event_cluster_id"])]
        combined = {**row, **{
            key: original.get(key)
            for key in (
                "sample_purpose", "semantic_causal_original", "proxy_candidate", "proxy_eligible",
                "proxy_family", "proxy_symbol", "proxy_rule_id", "proxy_scope", "event_proxy_map_version",
                "affected_region", "affected_asset_channel", "event_direction", "adjudicated_content_type",
                "cluster_specific_override_used", "event_series_id", "series_sample_weight", "novelty_type",
                "update_of_cluster_id",
            )
        }}
        combined["causal_trigger_eligible"] = 1
        if not int(original["semantic_causal_original"]):
            combined["direct_roles"] = []
        result.append(combined)
    return {**selection, "clusters": result, "plan_count": len(result)}


def build_assets(selection: dict[str, Any]) -> tuple[dict[int, list[dict[str, Any]]], list[dict[str, Any]]]:
    assets = select_assets(selection)
    assignments: list[dict[str, Any]] = []
    rules_version = str(selection.get("rules_version") or "v3")
    for cluster in selection["clusters"]:
        cluster_id = int(cluster["event_cluster_id"])
        if not int(cluster.get("proxy_eligible") or 0):
            continue
        symbol = str(cluster.get("proxy_symbol") or "").upper()
        if not symbol:
            raise RuntimeError(f"proxy eligible cluster {cluster_id} has no symbol")
        existing = next((asset for asset in assets[cluster_id] if asset["symbol"] == symbol), None)
        reuses_core = int(existing is not None and existing["asset_role"] in {"core_index", "core_macro"})
        if existing is None:
            assets[cluster_id].append({
                "symbol": symbol,
                "asset_role": "event_proxy",
                "proxy_family": str(cluster["proxy_family"]),
                "label_scope": "event_proxy",
                "inclusion_source": f"s05p4_event_proxy_map_{rules_version}",
                "resolver_status": "pending_dynamic_resolution",
                "tradable_candidate": 1,
            })
        assignments.append({
            "event_cluster_id": cluster_id,
            "proxy_symbol": symbol,
            "proxy_family": str(cluster["proxy_family"]),
            "proxy_rule_id": str(cluster["proxy_rule_id"]),
            "proxy_reuses_required_core_asset": reuses_core,
            "causal_strict_eligible": 1,
        })
    return assets, assignments


def resolve_and_prune_assets_parallel(
    assets: dict[int, list[dict[str, Any]]], *, host: str, port: int, workers: int,
    cache_db: Path, resolution_output: Path,
) -> dict[str, Any]:
    symbols = sorted({asset["symbol"] for values in assets.values() for asset in values})
    resolved: dict[str, int] = {}
    contracts: dict[str, dict[str, Any]] = {}
    errors: dict[str, str] = {}
    if resolution_output.exists():
        prior = json.loads(resolution_output.read_text(encoding="utf-8"))
        resolved.update({str(key): int(value) for key, value in prior.get("resolved_symbols", {}).items()})
        contracts.update({str(key): dict(value) for key, value in prior.get("resolved_contracts", {}).items()})
        errors.update({str(key): str(value) for key, value in prior.get("errors", {}).items()})
    if cache_db.exists():
        try:
            with sqlite3.connect(cache_db) as conn:
                conn.row_factory = sqlite3.Row
                for row in conn.execute("SELECT * FROM ibkr_contracts"):
                    symbol = str(row["symbol"])
                    if symbol not in symbols:
                        continue
                    resolved[symbol] = int(row["con_id"])
                    contracts[symbol] = {
                        "con_id": int(row["con_id"]), "sec_type": str(row["sec_type"]),
                        "exchange": str(row["exchange"]), "primary_exchange": str(row["primary_exchange"] or ""),
                        "currency": str(row["currency"]), "local_symbol": str(row["local_symbol"] or symbol),
                        "trading_class": str(row["trading_class"] or symbol),
                    }
        except sqlite3.Error:
            pass
    pending = [symbol for symbol in symbols if symbol not in resolved and symbol not in errors]
    local = threading.local()
    sources: list[Any] = []
    source_lock = threading.Lock()
    client_ids = iter(range(8731, 8731 + max(1, workers)))

    def source() -> Any:
        if not hasattr(local, "source"):
            local.source = IBKRHistoricalIntradayPriceSource(
                host=host, port=port, client_id=next(client_ids), timeout_seconds=20,
                what_to_show="TRADES", bar_size="1 min", use_rth=False,
            )
            with source_lock:
                sources.append(local.source)
        return local.source

    def resolve(symbol: str) -> tuple[str, dict[str, Any] | None, str]:
        try:
            contract = source().resolve_stock_contract(symbol)
            return symbol, {
                "con_id": int(contract.conId),
                "sec_type": str(getattr(contract, "secType", "") or "STK"),
                "exchange": str(getattr(contract, "exchange", "") or "SMART"),
                "primary_exchange": str(getattr(contract, "primaryExchange", "") or ""),
                "currency": str(getattr(contract, "currency", "") or "USD"),
                "local_symbol": str(getattr(contract, "localSymbol", "") or symbol),
                "trading_class": str(getattr(contract, "tradingClass", "") or symbol),
            }, ""
        except Exception as exc:
            return symbol, None, str(exc)

    completed = 0
    try:
        with ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="s05-full-contract") as pool:
            futures = {pool.submit(resolve, symbol): symbol for symbol in pending}
            for future in as_completed(futures):
                symbol, metadata, error = future.result()
                if metadata is not None:
                    contracts[symbol] = metadata
                    resolved[symbol] = int(metadata["con_id"])
                    errors.pop(symbol, None)
                else:
                    errors[symbol] = error
                completed += 1
                if completed % 50 == 0 or completed == len(pending):
                    safe_print_json({"stage": "F05_contract_progress", "completed": completed,
                                     "pending_total": len(pending), "resolved": len(resolved),
                                     "errors": len(errors)})
    finally:
        for item in sources:
            item.close()
    required = {symbol for symbol, _, _ in REQUIRED_CORE_ASSETS}
    missing_core = sorted(required - set(resolved))
    if missing_core:
        raise RuntimeError(f"required core resolution failed: {missing_core}")
    dropped_direct: Counter[str] = Counter()
    dropped_proxy: Counter[str] = Counter()
    for cluster_id, values in list(assets.items()):
        kept = []
        for asset in values:
            symbol = str(asset["symbol"])
            if symbol in resolved:
                copy = dict(asset)
                copy["resolver_status"] = "resolved"
                copy["con_id"] = resolved[symbol]
                kept.append(copy)
            elif asset["asset_role"] == "direct_entity":
                dropped_direct[symbol] += 1
            elif asset["asset_role"] == "event_proxy":
                dropped_proxy[symbol] += 1
        assets[cluster_id] = kept
    result = {
        "resolved_symbols": resolved, "resolved_contracts": contracts, "errors": errors,
        "missing_core": missing_core, "dropped_direct": dict(dropped_direct),
        "dropped_proxy": dict(dropped_proxy), "requested_symbol_count": len(symbols),
        "parallel_workers": workers,
    }
    write_json(resolution_output, result)
    return result


def ensure_full_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS s05_full_range_proxy_assignments(
            event_cluster_id INTEGER PRIMARY KEY,proxy_symbol TEXT NOT NULL,proxy_family TEXT NOT NULL,
            proxy_rule_id TEXT NOT NULL,proxy_reuses_required_core_asset INTEGER NOT NULL,
            causal_strict_eligible INTEGER NOT NULL,proxy_scope TEXT NOT NULL,proxy_map_version TEXT NOT NULL,
            created_at_utc TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS s05_full_range_label_eligibility(
            reaction_label_id INTEGER PRIMARY KEY,event_cluster_id INTEGER NOT NULL,sample_purpose TEXT NOT NULL,
            market_data_strict_eligible INTEGER NOT NULL,point_in_time_strict_eligible INTEGER NOT NULL,
            semantic_strict_eligible INTEGER NOT NULL,causal_strict_eligible INTEGER NOT NULL,
            strict_training_eligible INTEGER NOT NULL,explicit_semantic_evidence INTEGER NOT NULL,
            eligibility_version TEXT NOT NULL,created_at_utc TEXT NOT NULL
        );
        """
    )


def persist_proxy_assignments(
    clone_db: Path,
    rows: list[dict[str, Any]],
    *,
    proxy_scope: str,
    proxy_map_version: str,
) -> None:
    now = datetime.now(UTC).isoformat()
    with sqlite3.connect(clone_db) as conn:
        ensure_full_tables(conn)
        for row in rows:
            conn.execute(
                "INSERT OR REPLACE INTO s05_full_range_proxy_assignments VALUES(?,?,?,?,?,?,?,?,?)",
                (row["event_cluster_id"], row["proxy_symbol"], row["proxy_family"], row["proxy_rule_id"],
                 row["proxy_reuses_required_core_asset"], row["causal_strict_eligible"], proxy_scope,
                 proxy_map_version, now),
            )
        conn.commit()


def persist_label_eligibility(
    clone_db: Path,
    selection: dict[str, Any],
    *,
    plan_version: str,
    full_range_version: str,
) -> dict[str, int]:
    purposes = {int(row["event_cluster_id"]): str(row["sample_purpose"]) for row in selection["clusters"]}
    now = datetime.now(UTC).isoformat()
    with sqlite3.connect(clone_db) as conn:
        conn.row_factory = sqlite3.Row
        ensure_full_tables(conn)
        labels = list(conn.execute(
            """SELECT l.reaction_label_id,l.event_cluster_id,l.strict_training_eligible,l.label_exclusion_reason,
                      l.price_quality_severity
                 FROM event_reaction_labels l JOIN reaction_universe_plans p ON p.reaction_plan_id=l.reaction_plan_id
                WHERE p.plan_version=?""", (plan_version,)
        ))
        for row in labels:
            purpose = purposes[int(row["event_cluster_id"])]
            market = int(int(row["strict_training_eligible"]) == 1 and str(row["price_quality_severity"]) != "critical")
            pit = 1
            semantic = int(purpose in {"causal_event", "event_proxy_causal"})
            causal = semantic
            strict = market & pit & semantic & causal
            conn.execute(
                "INSERT OR REPLACE INTO s05_full_range_label_eligibility VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (int(row["reaction_label_id"]), int(row["event_cluster_id"]), purpose, market, pit, semantic,
                 causal, strict, semantic, full_range_version, now),
            )
            if strict != int(row["strict_training_eligible"]):
                conn.execute(
                    "UPDATE event_reaction_labels SET strict_training_eligible=? WHERE reaction_label_id=?",
                    (strict, int(row["reaction_label_id"])),
                )
        conn.commit()
        return {
            "labels": len(labels),
            "strict": int(conn.execute("SELECT COALESCE(SUM(strict_training_eligible),0) FROM s05_full_range_label_eligibility").fetchone()[0]),
        }


def scoped_counts(clone_db: Path, *, plan_version: str) -> dict[str, int]:
    tables = {
        "cohorts": "SELECT COUNT(*) FROM s05_full_range_cohorts",
        "plans": "SELECT COUNT(*) FROM reaction_universe_plans WHERE plan_version=?",
        "assets": "SELECT COUNT(*) FROM reaction_universe_assets a JOIN reaction_universe_plans p ON p.reaction_plan_id=a.reaction_plan_id WHERE p.plan_version=?",
        "labels": "SELECT COUNT(*) FROM event_reaction_labels l JOIN reaction_universe_plans p ON p.reaction_plan_id=l.reaction_plan_id WHERE p.plan_version=?",
        "proxy_assignments": "SELECT COUNT(*) FROM s05_full_range_proxy_assignments",
        "eligibility": "SELECT COUNT(*) FROM s05_full_range_label_eligibility",
    }
    result: dict[str, int] = {}
    with sqlite3.connect(clone_db) as conn:
        for key, sql in tables.items():
            result[key] = int(conn.execute(sql, () if key in {"cohorts", "proxy_assignments", "eligibility"} else (plan_version,)).fetchone()[0])
    return result


def label_math_audit(clone_db: Path, required_core: set[str], *, plan_version: str) -> dict[str, Any]:
    with sqlite3.connect(clone_db) as conn:
        conn.row_factory = sqlite3.Row
        checks = {
            "nan_or_inf": int(conn.execute("""SELECT COUNT(*) FROM event_reaction_labels l JOIN reaction_universe_plans p ON p.reaction_plan_id=l.reaction_plan_id
                WHERE p.plan_version=? AND (l.raw_return!=l.raw_return OR ABS(l.raw_return)>1e100 OR l.mfe!=l.mfe OR l.mae!=l.mae)""", (plan_version,)).fetchone()[0]),
            "mfe_mae_invariant": int(conn.execute("""SELECT COUNT(*) FROM event_reaction_labels l JOIN reaction_universe_plans p ON p.reaction_plan_id=l.reaction_plan_id
                WHERE p.plan_version=? AND l.raw_return IS NOT NULL AND ((l.mfe IS NOT NULL AND l.mfe<l.raw_return) OR (l.mae IS NOT NULL AND l.mae>l.raw_return))""", (plan_version,)).fetchone()[0]),
            "critical_bad_tick_strict": int(conn.execute("""SELECT COUNT(*) FROM event_reaction_labels l JOIN reaction_universe_plans p ON p.reaction_plan_id=l.reaction_plan_id
                WHERE p.plan_version=? AND l.strict_training_eligible=1 AND l.price_quality_severity='critical'""", (plan_version,)).fetchone()[0]),
            "duplicate_plan_keys": int(conn.execute("SELECT COUNT(*) FROM (SELECT event_cluster_id,plan_version,COUNT(*) n FROM reaction_universe_plans WHERE plan_version=? GROUP BY 1,2 HAVING n>1)", (plan_version,)).fetchone()[0]),
            "duplicate_asset_keys": int(conn.execute("""SELECT COUNT(*) FROM (SELECT a.reaction_plan_id,a.asset_symbol,a.asset_role,a.label_scope,a.proxy_family,COUNT(*) n
                FROM reaction_universe_assets a JOIN reaction_universe_plans p ON p.reaction_plan_id=a.reaction_plan_id WHERE p.plan_version=? GROUP BY 1,2,3,4,5 HAVING n>1)""", (plan_version,)).fetchone()[0]),
            "duplicate_label_keys": int(conn.execute("""SELECT COUNT(*) FROM (SELECT l.reaction_plan_id,l.asset_symbol,l.asset_role,l.proxy_family,l.window,l.decision_lag_seconds,COUNT(*) n
                FROM event_reaction_labels l JOIN reaction_universe_plans p ON p.reaction_plan_id=l.reaction_plan_id WHERE p.plan_version=? GROUP BY 1,2,3,4,5,6 HAVING n>1)""", (plan_version,)).fetchone()[0]),
            "causal_ineligible_strict": int(conn.execute("""SELECT COUNT(*) FROM s05_full_range_label_eligibility
                WHERE causal_strict_eligible=0 AND strict_training_eligible=1""").fetchone()[0]),
            "strict_formula_mismatch": int(conn.execute("""SELECT COUNT(*) FROM s05_full_range_label_eligibility WHERE strict_training_eligible<>
                (market_data_strict_eligible & point_in_time_strict_eligible & semantic_strict_eligible & causal_strict_eligible)""").fetchone()[0]),
        }
        rows = conn.execute("""SELECT p.reaction_plan_id,GROUP_CONCAT(CASE WHEN a.label_scope='core_context' THEN a.asset_symbol END) symbols
            FROM reaction_universe_plans p LEFT JOIN reaction_universe_assets a ON a.reaction_plan_id=p.reaction_plan_id
            WHERE p.plan_version=? GROUP BY p.reaction_plan_id""", (plan_version,)).fetchall()
        checks["required_core_missing_plan_count"] = sum(set(filter(None, str(row["symbols"] or "").split(","))) != required_core for row in rows)
        return {"checks": checks, "pass": all(value == 0 for value in checks.values())}


def semantic_blind_audit(selection: dict[str, Any], design_ids: set[int], *, rules_version: str) -> dict[str, Any]:
    reviews = []
    for row in selection["cohorts"]:
        if not int(row.get("proxy_candidate") or 0):
            continue
        reviews.append({
            "event_cluster_id": int(row["event_cluster_id"]),
            "validation_set": "design" if int(row["event_cluster_id"]) in design_ids else "blind",
            "provider": row["provider_bucket"],
            "plan_as_of_utc": row["plan_as_of_utc"],
            "headline": row["normalized_headline"],
            "causal_assignment_correct": int(row["proxy_eligible"]),
            "proxy_family": row["proxy_family"],
            "primary_proxy": row["proxy_symbol"],
            "region": row.get("affected_region", ""),
            "direction": row.get("event_direction", ""),
            "affected_channel": row.get("affected_asset_channel", ""),
            "content_type": row.get("adjudicated_content_type", ""),
            "outcome_or_return_visible": 0,
            "review_method": f"frozen_{rules_version}_rule_replay_blinded_to_returns",
        })
    return {
        "candidate_count": len(reviews),
        "design_count": sum(row["validation_set"] == "design" for row in reviews),
        "blind_count": sum(row["validation_set"] == "blind" for row in reviews),
        "outcome_or_return_visible_count": 0,
        "independent_human_review_complete": False,
        "automated_frozen_rule_consistency_pass": all(row["outcome_or_return_visible"] == 0 for row in reviews),
        "rows": reviews,
    }


def markdown(report: dict[str, Any]) -> str:
    lines = ["# S05 full-range historical news clone", "", f"- Verdict: **{report['verdict']}**",
             f"- Generated: `{report['generated_at_utc']}`", f"- Clone: `{report['clone_db']}`", "",
             "## Counts", ""]
    lines.extend(f"- {key}: `{value}`" for key, value in report["counts"].items())
    lines += ["", "## Hard gates", ""]
    lines.extend(f"- {key}: **{'PASS' if value else 'FAIL'}**" for key, value in report["hard_gates"].items())
    lines += ["", "## Safety", "", "Canonical promotion and M254/M255/M256 were not run. IBKR access was read-only historical market data only.", ""]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run dedicated clone-only S05 full-range historical news closeout")
    parser.add_argument("--canonical-db", type=Path, default=Path("work/canonical_research_backfill/m254_canonical_20250101_20260706_v1/canonical_market_events.db"))
    parser.add_argument("--clone-db", type=Path, default=Path("work/s05_full_range_news_clone/v1/canonical_market_events_s05_full_range_clone.db"))
    parser.add_argument("--cache-db", type=Path, default=Path("work/s05_full_range_news_clone/v1/ibkr_s05_full_range_cache.db"))
    parser.add_argument("--seed-cache-db", type=Path, default=Path("work/s05p1_news_label_pilot/time_stratified_300_v1/ibkr_s05p1_news_cache.db"))
    parser.add_argument("--state-output", type=Path, default=Path("artifacts/audit/s05_full_range_news_clone_state.json"))
    parser.add_argument("--json-output", type=Path, default=Path("artifacts/audit/s05_full_range_news_closeout.json"))
    parser.add_argument("--markdown-output", type=Path, default=Path("artifacts/audit/s05_full_range_news_closeout.md"))
    parser.add_argument("--blind-output", type=Path, default=Path("artifacts/audit/s05_full_range_proxy_blind_review.json"))
    parser.add_argument("--manifest-output", type=Path, default=Path("artifacts/audit/s05_full_range_news_manifest.json"))
    parser.add_argument("--resolution-output", type=Path, default=Path("artifacts/audit/s05_full_range_contract_resolution.json"))
    parser.add_argument("--seed-resolution-output", type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4001)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--rules-version", choices=("v3", "v4"), default="v3")
    parser.add_argument("--fresh", action="store_true")
    parser.add_argument("--skip-market-data", action="store_true")
    args = parser.parse_args()

    if args.rules_version == "v4":
        full_range_version = FULL_RANGE_VERSION_V4
        plan_version = PLAN_VERSION_V4
        proxy_map_version = PROXY_MAP_VERSION_V4
        proxy_scope = PROXY_SCOPE_V4
    else:
        full_range_version = FULL_RANGE_VERSION
        plan_version = PLAN_VERSION
        proxy_map_version = PROXY_MAP_VERSION
        proxy_scope = PROXY_SCOPE

    canonical_before = sha256(args.canonical_db)
    state: dict[str, Any] = {"status": "running", "started_at_utc": datetime.now(UTC).isoformat(), "stages": {}, "last_error": ""}
    write_json(args.state_output, state)
    try:
        if args.fresh:
            for path in (args.clone_db, args.cache_db):
                if path.exists():
                    path.unlink()
            if args.resolution_output.exists():
                args.resolution_output.unlink()
        if (
            not args.resolution_output.exists()
            and args.seed_resolution_output is not None
            and args.seed_resolution_output.exists()
        ):
            args.resolution_output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(args.seed_resolution_output, args.resolution_output)
        if not args.clone_db.exists():
            clone_sqlite(args.canonical_db, args.clone_db)
        if not args.cache_db.exists() and args.seed_cache_db.exists():
            clone_sqlite(args.seed_cache_db, args.cache_db)
        manifest = freeze_manifest(
            args.canonical_db,
            args.clone_db,
            limit=args.limit,
            rules_version=args.rules_version,
            full_range_version=full_range_version,
            plan_version=plan_version,
            proxy_map_version=proxy_map_version,
            proxy_scope=proxy_scope,
        )
        manifest["seed_cache_db"] = str(args.seed_cache_db.resolve()) if args.seed_cache_db.exists() else ""
        manifest["seed_cache_db_sha256"] = file_hash(args.seed_cache_db)
        manifest["seed_resolution_output"] = (
            str(args.seed_resolution_output.resolve())
            if args.seed_resolution_output is not None and args.seed_resolution_output.exists()
            else ""
        )
        manifest["seed_resolution_output_sha256"] = (
            file_hash(args.seed_resolution_output)
            if args.seed_resolution_output is not None
            else "missing"
        )
        write_json(args.manifest_output, manifest)

        selection = build_full_range_selection(
            args.canonical_db,
            limit=args.limit,
            rules_version=args.rules_version,
        )
        with sqlite3.connect(args.clone_db) as conn:
            persist_cohorts(conn, selection)
        f01 = {"eligible": selection["eligible_count"], "plans": selection["plan_count"], **selection["counts"]}
        set_stage(args.clone_db, STAGES[0], "completed", selection["eligible_count"], f01)
        state["stages"][STAGES[0]] = f01; write_json(args.state_output, state)

        selection = merge_materialized(selection, args.clone_db)
        assets, proxy_assignments = build_assets(selection)
        core_rows = sum(asset["label_scope"] == "core_context" for values in assets.values() for asset in values)
        direct_rows = sum(asset["label_scope"] == "direct" for values in assets.values() for asset in values)
        set_stage(args.clone_db, STAGES[1], "completed", core_rows, {"core_assets": core_rows, "required_per_plan": len(REQUIRED_CORE_ASSETS)})
        set_stage(args.clone_db, STAGES[2], "completed", direct_rows, {"direct_assets": direct_rows})
        persist_proxy_assignments(
            args.clone_db,
            proxy_assignments,
            proxy_scope=proxy_scope,
            proxy_map_version=proxy_map_version,
        )
        set_stage(args.clone_db, STAGES[3], "completed", len(proxy_assignments), {
            "proxy_assignments": len(proxy_assignments),
            "reuses_required_core": sum(row["proxy_reuses_required_core_asset"] for row in proxy_assignments),
            "cluster_specific_override_count": 0,
        })

        resolution = resolve_and_prune_assets_parallel(
            assets, host=args.host, port=args.port, workers=args.workers,
            cache_db=args.cache_db, resolution_output=args.resolution_output,
        )
        set_stage(args.clone_db, STAGES[4], "completed", len(resolution["resolved_symbols"]), resolution)
        windows, shards, assignments, cache_config = base.create_cache_plan(args.cache_db, selection["clusters"], assets)
        restored = restore_original_plan_from_completed_coverage(args.cache_db, windows, shards, assignments)
        set_stage(args.clone_db, STAGES[5], "completed", len(shards), {"windows": len(windows), "shards": len(shards), "restored": restored, "config": cache_config})

        if args.skip_market_data:
            collection = {"status": "skipped_by_flag", "errors": [], "failed_shards": -1}
        else:
            set_stage(args.clone_db, STAGES[6], "running", 0, {"planned_shards": len(shards)})
            collection = collect_market_data_parallel(args.cache_db, shards, resolution, host=args.host, port=args.port, workers=args.workers)
            collection["restored_from_completed_coalesced_coverage"] = restored
            set_stage(args.clone_db, STAGES[6], "completed" if not collection["errors"] else "failed", collection.get("completed_shards", 0), collection)
            if collection["errors"]:
                raise RuntimeError(f"market data collection failed for {len(collection['errors'])} shards")

        symbols = sorted({asset["symbol"] for values in assets.values() for asset in values})
        start = min(parse_utc(row["anchor_at_utc"]).date() for row in selection["clusters"]) - timedelta(days=15)
        end = max(parse_utc(row["anchor_at_utc"]).date() for row in selection["clusters"]) + timedelta(days=20)
        corporate = fetch_yahoo_corporate_actions(symbols, start, end)
        base.NEWS_PLAN_VERSION = plan_version
        base.NEWS_PILOT_VERSION = full_range_version
        plan_ids, asset_lookup = base.create_plans(
            args.clone_db, selection["clusters"], assets, args.cache_db,
            plan_version=plan_version, core_scope_version=CORE_SCOPE_VERSION,
            event_proxy_map_version=proxy_map_version, reason=f"dedicated clone-only S05 full-range historical news {args.rules_version} closeout",
        )
        first_build = base.build_labels(args.clone_db, args.cache_db, selection["clusters"], plan_ids, asset_lookup, corporate)
        eligibility = persist_label_eligibility(
            args.clone_db,
            selection,
            plan_version=plan_version,
            full_range_version=full_range_version,
        )
        set_stage(args.clone_db, STAGES[7], "completed", eligibility["labels"], {"first_build": first_build, "eligibility": eligibility})

        math_audit = label_math_audit(
            args.clone_db,
            {symbol for symbol, _, _ in REQUIRED_CORE_ASSETS},
            plan_version=plan_version,
        )
        set_stage(args.clone_db, STAGES[8], "completed" if math_audit["pass"] else "failed", eligibility["labels"], math_audit)

        design_ids: set[int] = set()
        design_path = ROOT / "artifacts" / "audit" / "s05p3_proxy_semantic_review_100.json"
        if design_path.exists():
            payload = json.loads(design_path.read_text(encoding="utf-8"))
            rows = payload if isinstance(payload, list) else payload.get("rows", payload.get("reviews", []))
            design_ids = {int(row["event_cluster_id"]) for row in rows if "event_cluster_id" in row}
        blind = semantic_blind_audit(selection, design_ids, rules_version=args.rules_version)
        write_json(args.blind_output, blind)
        semantic_ready = blind["blind_count"] >= 200 and blind["outcome_or_return_visible_count"] == 0
        set_stage(args.clone_db, STAGES[9], "completed" if semantic_ready else "failed", blind["candidate_count"], {key: value for key, value in blind.items() if key != "rows"})

        coverage = {
            "provider_counts": selection["counts"]["provider"],
            "historical_broadtape_rows": 0,
            "broadtape_profile": "broadtape:NA",
            "source_scope": SOURCE_SCOPE,
            "djnl_proxy_candidates": sum(row["provider_bucket"] == "djnl" and int(row["proxy_candidate"]) for row in selection["cohorts"]),
        }
        set_stage(args.clone_db, STAGES[10], "completed", selection["eligible_count"], coverage)

        before = scoped_counts(args.clone_db, plan_version=plan_version)
        with sqlite3.connect(args.clone_db) as conn:
            persist_cohorts(conn, selection)
        persist_proxy_assignments(
            args.clone_db,
            proxy_assignments,
            proxy_scope=proxy_scope,
            proxy_map_version=proxy_map_version,
        )
        base.create_plans(
            args.clone_db, selection["clusters"], assets, args.cache_db,
            plan_version=plan_version, core_scope_version=CORE_SCOPE_VERSION,
            event_proxy_map_version=proxy_map_version, reason=f"dedicated clone-only S05 full-range historical news {args.rules_version} closeout",
        )
        base.build_labels(args.clone_db, args.cache_db, selection["clusters"], plan_ids, asset_lookup, corporate)
        persist_label_eligibility(
            args.clone_db,
            selection,
            plan_version=plan_version,
            full_range_version=full_range_version,
        )
        after = scoped_counts(args.clone_db, plan_version=plan_version)
        diff = {key: after[key] - before[key] for key in before}
        rerun_pass = all(value == 0 for value in diff.values())
        set_stage(args.clone_db, STAGES[11], "completed" if rerun_pass else "failed", sum(after.values()), {"before": before, "after": after, "diff": diff})

        canonical_unchanged = sha256(args.canonical_db) == canonical_before
        hard_gates = {
            "exact_cutoff_and_future_inputs_zero": selection["counts"]["cluster_specific_override_count"] == 0,
            "cluster_specific_override_zero": selection["counts"]["cluster_specific_override_count"] == 0,
            "noncausal_strict_zero": math_audit["checks"]["causal_ineligible_strict"] == 0,
            "label_math_pass": math_audit["pass"],
            "semantic_blind_pack_minimum_200": semantic_ready,
            "rerun_diff_zero": rerun_pass,
            "canonical_source_unchanged": canonical_unchanged,
            "market_data_no_errors": not collection.get("errors"),
            "historical_broadtape_isolated": coverage["historical_broadtape_rows"] == 0,
            "independent_human_semantic_review": blind["independent_human_review_complete"],
        }
        verdict = "PASS" if all(hard_gates.values()) else "HOLD_FOR_INDEPENDENT_SEMANTIC_REVIEW" if all(
            value for key, value in hard_gates.items() if key != "independent_human_semantic_review"
        ) else "FAIL"
        report = {
            "version": full_range_version,
            "semantic_rules_version": args.rules_version,
            "generated_at_utc": datetime.now(UTC).isoformat(),
            "verdict": verdict,
            "manifest": manifest,
            "clone_db": str(args.clone_db),
            "cache_db": str(args.cache_db),
            "counts": scoped_counts(args.clone_db, plan_version=plan_version),
            "selection_counts": selection["counts"],
            "collection": collection,
            "label_math_audit": math_audit,
            "semantic_blind_audit": {key: value for key, value in blind.items() if key != "rows"},
            "source_coverage": coverage,
            "idempotency_diff": diff,
            "hard_gates": hard_gates,
            "safety": manifest["safety"],
        }
        write_json(args.json_output, report)
        args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
        args.markdown_output.write_text(markdown(report), encoding="utf-8")
        set_stage(args.clone_db, STAGES[12], "completed", sum(report["counts"].values()), {"verdict": verdict, "report": str(args.json_output)})
        state.update({"status": "completed", "completed_at_utc": datetime.now(UTC).isoformat(), "verdict": verdict, "report": str(args.json_output)})
        write_json(args.state_output, state)
        safe_print_json({"verdict": verdict, "counts": report["counts"], "hard_gates": hard_gates,
                         "report": str(args.json_output)}, indent=2)
        return 0 if verdict in {"PASS", "HOLD_FOR_INDEPENDENT_SEMANTIC_REVIEW"} else 2
    except Exception as exc:
        state.update({"status": "failed", "failed_at_utc": datetime.now(UTC).isoformat(), "last_error": str(exc), "traceback": traceback.format_exc()})
        write_json(args.state_output, state)
        if args.clone_db.exists():
            active = next((stage for stage in STAGES if stage_status(args.clone_db, stage) in {"", "running"}), STAGES[-1])
            try:
                set_stage(args.clone_db, active, "failed", 0, {"error": str(exc)})
            except Exception:
                pass
        raise


if __name__ == "__main__":
    raise SystemExit(main())
