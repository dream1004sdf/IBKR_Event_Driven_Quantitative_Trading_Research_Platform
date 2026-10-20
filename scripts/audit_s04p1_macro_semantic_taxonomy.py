from __future__ import annotations

import argparse
import inspect
import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from recorder import reaction_research_engine  # noqa: E402
from recorder.macro_event_lineage import S04P1_REGISTRY_VERSION  # noqa: E402
from scripts.audit_s04_macro_closeout import run_clone_idempotency  # noqa: E402


DEFAULT_DB = ROOT / "work" / "canonical_research_backfill" / "m254_canonical_20250101_20260706_v1" / "canonical_market_events.db"
DEFAULT_CONFIG = ROOT / "config" / "backfill" / "canonical_20250101_20260706.yaml"
DEFAULT_OUT = ROOT / "artifacts" / "audit"


def count(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> int:
    row = conn.execute(sql, params).fetchone()
    return int(row[0] or 0) if row else 0


def rows(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def duplicate_groups(conn: sqlite3.Connection, table: str, key: str) -> int:
    return count(
        conn,
        f"""SELECT COUNT(*) FROM (
                SELECT {key},COUNT(*) AS n FROM {table}
                 WHERE COALESCE({key},'')<>''
                 GROUP BY {key} HAVING COUNT(*)>1
            )""",
    )


def central_bank_coverage(conn: sqlite3.Connection) -> tuple[dict[str, dict[str, int]], list[str]]:
    raw = rows(
        conn,
        """SELECT central_bank,decision_type,COUNT(*) AS count
             FROM central_bank_events
            GROUP BY central_bank,decision_type
            ORDER BY central_bank,decision_type""",
    )
    normalized: dict[str, dict[str, int]] = {"Fed": {}, "ECB": {}, "BOJ": {}}
    for row in raw:
        bank = str(row["central_bank"] or "")
        decision_type = str(row["decision_type"] or "")
        target = decision_type.removeprefix("central_bank_")
        if bank == "BOJ" and target in {"minutes", "summary_of_opinions"}:
            target = "minutes_or_summary"
        normalized.setdefault(bank, {})[target] = normalized.setdefault(bank, {}).get(target, 0) + int(row["count"])
    required = {
        "Fed": {"decision", "minutes", "projection"},
        "ECB": {"decision", "statement", "press_conference"},
        "BOJ": {"decision", "statement", "minutes_or_summary"},
    }
    missing = [
        f"{bank}:{event_type}"
        for bank, event_types in required.items()
        for event_type in sorted(event_types)
        if int(normalized.get(bank, {}).get(event_type, 0)) <= 0
    ]
    return normalized, missing


def audit(db_path: Path, config_path: Path) -> dict[str, Any]:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    macro_config = config.get("macro") or {}
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    try:
        view_names = {
            str(row[0])
            for row in conn.execute(
                """SELECT name FROM sqlite_master
                    WHERE type='view' AND name IN (
                        'v_canonical_reaction_trigger_macro_events',
                        'v_canonical_intraday_reaction_trigger_macro_events',
                        'v_macro_regime_features'
                    )"""
            ).fetchall()
        }
        checks = {
            "wti_brent_inventory_or_trigger_rows": count(
                conn,
                """SELECT COUNT(*) FROM macro_event_releases r
                     JOIN macro_observations o ON o.macro_event_id=r.macro_event_id
                    WHERE o.series_id IN ('DCOILWTICO','DCOILBRENTEU')
                      AND (r.event_type<>'energy_price_observation' OR r.reaction_trigger_eligible<>0)""",
            ),
            "blocked_daily_regime_trigger_rows": count(
                conn,
                """SELECT COUNT(*) FROM macro_event_releases r
                     JOIN macro_observations o ON o.macro_event_id=r.macro_event_id
                    WHERE r.reaction_trigger_eligible=1
                      AND (o.series_id GLOB 'DGS*'
                           OR o.series_id IN ('EFFR','SOFR','RRPONTSYD','VIXCLS','T10Y2Y','T10Y3M')
                           OR o.series_id GLOB 'BAML*'
                           OR o.series_id IN ('DCOILWTICO','DCOILBRENTEU'))""",
            ),
            "reaction_trigger_without_registry_allowlist": count(
                conn,
                """SELECT COUNT(*) FROM macro_event_releases r
                     LEFT JOIN macro_series_registry g ON g.registry_key=r.registry_key
                    WHERE r.reaction_trigger_eligible=1
                      AND (g.registry_id IS NULL OR g.reaction_trigger_eligible<>1)""",
            ),
            "observation_date_only_reaction_trigger_rows": count(
                conn,
                """SELECT COUNT(*) FROM macro_event_releases
                    WHERE reaction_trigger_eligible=1
                      AND release_time_quality IN ('observation_date_only','estimated','unknown','backfilled_observation_date')""",
            ),
            "intraday_non_exact_rows": count(
                conn,
                """SELECT COUNT(*) FROM macro_event_releases
                    WHERE intraday_label_eligible=1
                      AND release_time_quality NOT IN ('official_exact','provider_exact')""",
            ),
            "active_ineligible_macro_links": count(
                conn,
                """SELECT COUNT(*) FROM macro_event_cluster_links l
                     JOIN macro_event_releases r ON r.macro_event_id=l.macro_event_id
                    WHERE COALESCE(l.link_status,'active')='active'
                      AND (r.event_cluster_eligible<>1 OR r.reaction_trigger_eligible<>1)""",
            ),
            "canonical_view_ineligible_rows": count(
                conn,
                """SELECT COUNT(*) FROM v_canonical_reaction_trigger_macro_events
                    WHERE link_status<>'active'""",
            ) if "v_canonical_reaction_trigger_macro_events" in view_names else -1,
            "legacy_duplicate_regime_view_rows": count(
                conn,
                """SELECT COUNT(*) FROM v_macro_regime_features v
                     JOIN macro_event_releases r ON r.macro_event_id=v.macro_event_id
                    WHERE r.event_eligibility_reason LIKE 'legacy_duplicate_of:%'""",
            ) if "v_macro_regime_features" in view_names else -1,
            "macro_event_key_duplicate_groups": duplicate_groups(conn, "macro_event_releases", "macro_event_key"),
            "macro_observation_key_duplicate_groups": duplicate_groups(
                conn, "macro_observations", "macro_observation_key"
            ),
            "macro_cluster_link_key_duplicate_groups": duplicate_groups(
                conn, "macro_event_cluster_links", "macro_cluster_link_key"
            ),
            "registry_key_duplicate_groups": duplicate_groups(conn, "macro_series_registry", "registry_key"),
            "fake_macro_news_rows": count(
                conn,
                """SELECT COUNT(*) FROM raw_news_articles
                    WHERE lower(source_system) IN ('fred','alfred','bls','bea','eia','fed','federal_reserve','ecb','boj')""",
            ),
            "regime_only_reaction_plan_rows": count(
                conn,
                """SELECT COUNT(*) FROM reaction_universe_plans p
                     JOIN news_event_clusters c ON c.event_cluster_id=p.event_cluster_id
                    WHERE c.cluster_type IN ('macro_release','central_bank_event')
                      AND NOT EXISTS (
                          SELECT 1 FROM v_canonical_reaction_trigger_macro_events v
                           WHERE v.event_cluster_id=p.event_cluster_id
                      )""",
            ),
            "non_exact_intraday_label_rows": count(
                conn,
                """SELECT COUNT(*) FROM event_reaction_labels e
                     JOIN reaction_universe_plans p ON p.reaction_plan_id=e.reaction_plan_id
                     JOIN v_canonical_reaction_trigger_macro_events v ON v.event_cluster_id=p.event_cluster_id
                    WHERE e.window IN ('5m','15m','1h','close')
                      AND (v.intraday_label_eligible<>1 OR v.release_time_quality NOT IN ('official_exact','provider_exact'))""",
            ),
            "s05_m253plus_stage_rows": count(
                conn,
                """SELECT COUNT(*) FROM research_backfill_stage_runs
                    WHERE stage_name LIKE 'S05%' OR stage_name LIKE 'M253%' OR stage_name LIKE 'M254%'
                       OR stage_name LIKE 'M255%' OR stage_name LIKE 'M256%'""",
            ),
        }
        coverage, coverage_missing = central_bank_coverage(conn)
        registry_distribution = rows(
            conn,
            """SELECT semantic_type,event_type,reaction_trigger_eligible,intraday_label_eligible,COUNT(*) AS count
                 FROM macro_series_registry
                GROUP BY semantic_type,event_type,reaction_trigger_eligible,intraday_label_eligible
                ORDER BY count DESC""",
        )
        release_distribution = rows(
            conn,
            """SELECT semantic_type,event_type,reaction_trigger_eligible,intraday_label_eligible,
                      release_time_quality,COUNT(*) AS count
                 FROM macro_event_releases
                GROUP BY semantic_type,event_type,reaction_trigger_eligible,intraday_label_eligible,release_time_quality
                ORDER BY count DESC""",
        )
        series_samples = rows(
            conn,
            """SELECT o.series_id,r.semantic_type,r.event_type,r.reaction_trigger_eligible,
                      r.intraday_label_eligible,r.release_time_quality,COUNT(*) AS count
                 FROM macro_observations o
                 JOIN macro_event_releases r ON r.macro_event_id=o.macro_event_id
                GROUP BY o.series_id,r.semantic_type,r.event_type,r.reaction_trigger_eligible,
                         r.intraday_label_eligible,r.release_time_quality
                ORDER BY count DESC,o.series_id
                LIMIT 100""",
        )
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
    finally:
        conn.close()

    source = inspect.getsource(reaction_research_engine._eligible_event_cluster_ids)
    read_path = {
        "uses_canonical_macro_view": "v_canonical_reaction_trigger_macro_events" in source,
        "direct_raw_macro_link_read": "macro_event_cluster_links" in source,
        "store_label_has_exact_time_guard": "intraday macro label requires exact official/provider release time"
        in inspect.getsource(reaction_research_engine.store_event_reaction_label),
    }
    config_checks = {
        "build_scope": macro_config.get("build_scope"),
        "central_bank_scope": macro_config.get("central_bank_scope"),
        "registry_version": macro_config.get("taxonomy_registry_version"),
        "reaction_trigger_default": macro_config.get("reaction_trigger_default"),
        "market_data_policy": macro_config.get("market_data_policy"),
    }
    hard_failures = [key for key, value in checks.items() if int(value) != 0]
    if view_names != {
        "v_canonical_reaction_trigger_macro_events",
        "v_canonical_intraday_reaction_trigger_macro_events",
        "v_macro_regime_features",
    }:
        hard_failures.append("canonical_macro_views_missing")
    if coverage_missing:
        hard_failures.append("central_bank_coverage_missing")
    if not read_path["uses_canonical_macro_view"] or read_path["direct_raw_macro_link_read"]:
        hard_failures.append("s05_macro_read_path_not_canonical")
    if not read_path["store_label_has_exact_time_guard"]:
        hard_failures.append("intraday_label_guard_missing")
    if config_checks["build_scope"] != "global_macro_v1":
        hard_failures.append("global_build_scope_missing")
    if set(config_checks["central_bank_scope"] or []) != {"Fed", "ECB", "BOJ"}:
        hard_failures.append("global_central_bank_scope_missing")
    if config_checks["registry_version"] != S04P1_REGISTRY_VERSION:
        hard_failures.append("registry_version_config_mismatch")
    if config_checks["reaction_trigger_default"] is not False:
        hard_failures.append("reaction_trigger_default_not_false")
    if not str(config_checks["market_data_policy"] or "").startswith("ibkr_read_only"):
        hard_failures.append("ibkr_read_only_market_data_policy_missing")
    if integrity != "ok":
        hard_failures.append("sqlite_integrity_check_failed")

    return {
        "database": str(db_path),
        "config": str(config_path),
        "checks": checks,
        "view_names": sorted(view_names),
        "central_bank_coverage": coverage,
        "central_bank_coverage_missing": coverage_missing,
        "read_path": read_path,
        "config_checks": config_checks,
        "registry_distribution": registry_distribution,
        "release_distribution": release_distribution,
        "top_series": series_samples,
        "integrity_check": integrity,
        "hard_failures": hard_failures,
        "pass_without_clone": not hard_failures,
    }


def render_markdown(report: dict[str, Any]) -> str:
    audit_payload = report["audit"]
    lines = [
        "# S04P1 Macro Semantic Taxonomy & Release-Time Audit",
        "",
        f"- Verdict: **{report['verdict']}**",
        f"- Database: `{audit_payload['database']}`",
        "- Build scope: `global_macro_v1`",
        "- Central bank scope: `Fed, ECB, BOJ`",
        "- Full S05/M253+ started: **no**",
        "",
        "## Hard checks",
        "",
        "| check | count |",
        "|---|---:|",
        *[f"| `{key}` | {value} |" for key, value in audit_payload["checks"].items()],
        "",
        "## Central-bank coverage",
        "",
        "```json",
        json.dumps(audit_payload["central_bank_coverage"], ensure_ascii=False, indent=2),
        "```",
        "",
        "## Clone idempotency",
        "",
        f"- Pass: `{bool((report.get('clone_idempotency') or {}).get('pass'))}`",
        "",
        "## Hard failures",
        "",
    ]
    failures = report.get("hard_failures") or []
    lines.extend([f"- {item}" for item in failures] if failures else ["- None"])
    lines.extend(
        [
            "",
            "## Safety",
            "",
            "- No broker order, transmit, live routing, live threshold promotion, or live IBKR market-data request was performed.",
            "- IBKR remains the configured read-only market-data priority after security-master/contract resolution.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit S04P1 semantic taxonomy and release-time gates")
    parser.add_argument("--database", default=str(DEFAULT_DB))
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUT))
    parser.add_argument("--run-clone-idempotency", action="store_true")
    parser.add_argument("--clone-timeout-seconds", type=int, default=3600)
    args = parser.parse_args()
    db_path = Path(args.database).resolve()
    config_path = Path(args.config).resolve()
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    audit_payload = audit(db_path, config_path)
    clone = None
    if args.run_clone_idempotency:
        clone = run_clone_idempotency(db_path, config_path, output_dir, args.clone_timeout_seconds)
    hard_failures = list(audit_payload["hard_failures"])
    if clone is None:
        hard_failures.append("clone_idempotency_not_run")
    elif not clone.get("pass"):
        hard_failures.append("clone_idempotency_failed")
    verdict = "PASS" if not hard_failures else "FAIL"
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    report = {
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "verdict": verdict,
        "audit": audit_payload,
        "clone_idempotency": clone,
        "hard_failures": hard_failures,
        "safety": {
            "broker_orders_submitted": False,
            "transmit_enabled": False,
            "live_order_routing_enabled": False,
            "live_threshold_promotion": False,
            "ibkr_market_data_fetch_performed": False,
            "s05_started": False,
        },
    }
    json_path = output_dir / f"s04p1_macro_semantic_taxonomy_audit_{stamp}.json"
    md_path = output_dir / f"s04p1_macro_semantic_taxonomy_audit_{stamp}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps({"verdict": verdict, "json": str(json_path), "md": str(md_path)}, ensure_ascii=False, indent=2))
    return 0 if verdict == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
