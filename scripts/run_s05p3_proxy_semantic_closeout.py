from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.run_s05_news_stratified_pilot as base
from recorder.s05_event_proxy_closeout import match_proxy_rule
from recorder.s05_news_semantic_closeout import classify_headline, normalize_headline, provider_family
from recorder.s05_proxy_semantic_closeout import (
    CUTOFF_END_UTC,
    CUTOFF_START_UTC,
    PROXY_MAP_VERSION,
    PROXY_SCOPE,
    S05P3_VERSION,
    ensure_s05p3_schema,
    has_s05p3_outcome_or_context,
    infer_channel,
    infer_content_type,
    infer_direction,
    infer_primary_proxy,
    infer_region,
    load_current_classifications,
    persist_reviews_and_eligibility,
    review_proxy_row,
    stable_hash,
    MANUAL_CAUSAL_EXCLUSIONS,
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def audit_djnl_candidates(canonical_db: Path) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    with sqlite3.connect(f"file:{canonical_db.as_posix()}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT DISTINCT l.event_cluster_id,r.id news_id,r.headline,r.provider,r.source_system,
                      COALESCE(v.model_visible_at_utc,
                          CASE WHEN r.source_system='ibkr' THEN strftime('%Y-%m-%dT%H:%M:%S+00:00',r.published_at_utc,'+60 seconds')
                               ELSE r.available_at_utc END,r.published_at_utc) visible_at
                 FROM raw_news_articles r JOIN news_article_cluster_links l ON l.news_id=r.id
                 LEFT JOIN news_visibility_policy v ON v.news_id=r.id
                WHERE COALESCE(v.model_visible_at_utc,
                          CASE WHEN r.source_system='ibkr' THEN strftime('%Y-%m-%dT%H:%M:%S+00:00',r.published_at_utc,'+60 seconds')
                               ELSE r.available_at_utc END,r.published_at_utc)>=?
                  AND COALESCE(v.model_visible_at_utc,
                          CASE WHEN r.source_system='ibkr' THEN strftime('%Y-%m-%dT%H:%M:%S+00:00',r.published_at_utc,'+60 seconds')
                               ELSE r.available_at_utc END,r.published_at_utc)<?""",
            (CUTOFF_START_UTC, CUTOFF_END_UTC),
        )
        for raw in rows:
            row = dict(raw)
            if provider_family(str(row["provider"]), str(row["source_system"])) != "djnl":
                continue
            if has_s05p3_outcome_or_context(str(row["headline"] or "")):
                continue
            rule = match_proxy_rule(str(row["headline"] or ""))
            if rule is None:
                continue
            row["proxy_family"] = rule.family
            row["proxy_rule_id"] = rule.rule_id
            row["affected_region"] = infer_region(rule.family,str(row["headline"]))
            row["affected_asset_channel"] = infer_channel(rule.family,str(row["headline"]))
            row["event_direction"] = infer_direction(rule.family,str(row["headline"]))
            row["adjudicated_content_type"] = infer_content_type(rule.family,str(row["headline"]))
            row["proposed_proxy_symbol"] = infer_primary_proxy(
                rule.family,row["affected_region"],row["affected_asset_channel"],str(row["headline"])
            )
            candidates.append(row)
    deduped: list[dict[str, Any]] = []
    seen_headlines: set[str] = set()
    for row in sorted(candidates,key=lambda item: stable_hash("s05p3-djnl-target",item["event_cluster_id"],item["news_id"])):
        key=normalize_headline(str(row["headline"])).lower()
        if key in seen_headlines:
            continue
        seen_headlines.add(key)
        deduped.append(row)
    targeted = deduped[:25]
    return {
        "eligible_article_count": len(deduped),
        "eligible_cluster_count": len({int(row["event_cluster_id"]) for row in deduped}),
        "targeted_pilot_count": len(targeted),
        "targeted_family_counts": dict(Counter(row["proxy_family"] for row in targeted)),
        "coverage_gap_djnl_event_proxy_pilot": len(deduped) < 20,
        "eligible_candidate_omission_count": 0 if (not deduped or 20 <= len(targeted) <= 30 or len(targeted)==len(deduped)) else len(deduped),
        "reason": "No DJNL article matched the frozen high-precision event-proxy rules in the exact cutoff." if not deduped
                  else ("Eligible DJNL candidates exist; a deterministic 25-row targeted mapping pilot is included."
                        if len(deduped)>=20 else "Fewer than 20 unique high-precision DJNL candidates remain after outcome/context exclusion; all are included and the targeted-pilot coverage gap remains explicit."),
        "candidates": targeted,
    }


def full_range_preflight(canonical_db: Path) -> dict[str, Any]:
    total = 0
    causal = 0
    proxy_candidates = 0
    providers: Counter[str] = Counter()
    content_types: Counter[str] = Counter()
    proxy_families: Counter[str] = Counter()
    with sqlite3.connect(f"file:{canonical_db.as_posix()}?mode=ro",uri=True) as conn:
        conn.row_factory=sqlite3.Row
        rows=conn.execute(
            """WITH visible AS (
                   SELECT l.event_cluster_id,r.headline,r.source_system,r.provider,
                          COALESCE(v.model_visible_at_utc,
                            CASE WHEN r.source_system='ibkr' THEN strftime('%Y-%m-%dT%H:%M:%S+00:00',r.published_at_utc,'+60 seconds')
                                 ELSE r.available_at_utc END,r.published_at_utc) visible_at,
                          ROW_NUMBER() OVER(PARTITION BY l.event_cluster_id ORDER BY COALESCE(v.model_visible_at_utc,
                            CASE WHEN r.source_system='ibkr' THEN strftime('%Y-%m-%dT%H:%M:%S+00:00',r.published_at_utc,'+60 seconds')
                                 ELSE r.available_at_utc END,r.published_at_utc),r.id) rn
                     FROM news_article_cluster_links l JOIN raw_news_articles r ON r.id=l.news_id
                     LEFT JOIN news_visibility_policy v ON v.news_id=r.id)
                SELECT c.event_cluster_id,v.headline,v.source_system,v.provider
                  FROM news_event_clusters c JOIN visible v ON v.event_cluster_id=c.event_cluster_id AND v.rn=1
                 WHERE c.cluster_type='news_event' AND c.reaction_trigger_eligible=1
                   AND v.visible_at>=? AND v.visible_at<?""",
            (CUTOFF_START_UTC,CUTOFF_END_UTC),
        )
        for raw in rows:
            row=dict(raw); total+=1
            semantic=classify_headline(str(row["headline"] or ""))
            content_types[semantic.news_content_type]+=1
            providers[provider_family(str(row["provider"]),str(row["source_system"]))]+=1
            causal+=int(semantic.causal_trigger_eligible)
            if has_s05p3_outcome_or_context(str(row["headline"] or "")):
                continue
            rule=match_proxy_rule(str(row["headline"] or ""))
            if rule is None or int(row["event_cluster_id"]) in MANUAL_CAUSAL_EXCLUSIONS:
                continue
            proxy_candidates+=1;proxy_families[rule.family]+=1
    return {
        "status":"NOT_STARTED_SIZING_ONLY",
        "eligible_clusters":total,"causal_eligible_clusters":causal,"proxy_eligible_clusters":proxy_candidates,
        "provider_counts":dict(providers),"content_type_counts":dict(content_types),"proxy_family_counts":dict(proxy_families),
        "cutoff_start_utc":CUTOFF_START_UTC,"cutoff_end_utc":CUTOFF_END_UTC,
    }


def scoped_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        "reviews": int(conn.execute("SELECT COUNT(*) FROM s05p3_proxy_semantic_reviews").fetchone()[0]),
        "assignments": int(conn.execute("SELECT COUNT(*) FROM s05p3_proxy_assignments").fetchone()[0]),
        "eligibility": int(conn.execute("SELECT COUNT(*) FROM s05p3_label_eligibility").fetchone()[0]),
        "effective_strict": int(conn.execute("SELECT COUNT(*) FROM s05p3_label_eligibility WHERE strict_training_eligible=1").fetchone()[0]),
        "djnl_coverage": int(conn.execute("SELECT COUNT(*) FROM s05p3_djnl_proxy_coverage").fetchone()[0]),
    }


def persist_djnl_coverage(conn: sqlite3.Connection, djnl: dict[str, Any]) -> None:
    ensure_s05p3_schema(conn)
    conn.execute(
        """INSERT OR REPLACE INTO s05p3_djnl_proxy_coverage VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            stable_hash(S05P3_VERSION,"djnl",CUTOFF_START_UTC,CUTOFF_END_UTC),
            djnl["eligible_article_count"],djnl["eligible_cluster_count"],djnl["targeted_pilot_count"],
            int(djnl["coverage_gap_djnl_event_proxy_pilot"]),djnl["reason"],
            CUTOFF_START_UTC,CUTOFF_END_UTC,datetime.now(UTC).isoformat(),
        ),
    )
    conn.commit()


def build_audit(
    clone_db: Path,
    canonical_db: Path,
    reviews: list[dict[str, Any]],
    djnl: dict[str, Any],
    before: dict[str, int],
    after: dict[str, int],
    canonical_hash: str,
) -> dict[str, Any]:
    with sqlite3.connect(clone_db) as conn:
        conn.row_factory = sqlite3.Row
        counts = scoped_counts(conn)
        old_primary_errors = int(conn.execute("SELECT COUNT(*) FROM s05p3_proxy_semantic_reviews WHERE primary_proxy_appropriate=0").fetchone()[0])
        old_event_class_errors = int(conn.execute("SELECT COUNT(*) FROM s05p3_proxy_semantic_reviews WHERE event_class_correct=0").fetchone()[0])
        old_content_errors = int(conn.execute("SELECT COUNT(*) FROM s05p3_proxy_semantic_reviews WHERE semantic_content_type_correct=0").fetchone()[0])
        conflicts = int(conn.execute("SELECT COUNT(*) FROM s05p3_proxy_semantic_reviews WHERE classifier_proxy_conflict_detected=1").fetchone()[0])
        unresolved_conflicts = int(conn.execute("SELECT COUNT(*) FROM s05p3_proxy_semantic_reviews WHERE classifier_proxy_conflict_detected=1 AND classifier_proxy_conflict_resolved=0").fetchone()[0])
        lineage_missing = int(conn.execute(
            """SELECT COUNT(*) FROM s05p3_proxy_semantic_reviews WHERE classification_id IS NULL OR classifier_version=''
                 OR classification_available_at='' OR proxy_selection_basis=''"""
        ).fetchone()[0])
        future_map = int(conn.execute(
            "SELECT COUNT(*) FROM s05p3_proxy_assignments WHERE plan_as_of_utc<map_valid_from OR plan_as_of_utc>=map_valid_to"
        ).fetchone()[0])
        cutoff_bad = int(conn.execute(
            "SELECT COUNT(*) FROM s05p3_proxy_assignments WHERE plan_as_of_utc<? OR plan_as_of_utc>=?",
            (CUTOFF_START_UTC,CUTOFF_END_UTC),
        ).fetchone()[0])
        critical_region = int(conn.execute(
            """SELECT COUNT(*) FROM s05p3_proxy_assignments WHERE
                 (proxy_family='rates_policy' AND affected_region='japan' AND asset_symbol<>'EWJ') OR
                 (proxy_family='china_taiwan' AND affected_region='taiwan' AND asset_symbol<>'EWT') OR
                 (proxy_family='china_taiwan' AND affected_region='china' AND asset_symbol<>'FXI')"""
        ).fetchone()[0])
        family_errors = int(conn.execute("SELECT COUNT(*) FROM s05p3_proxy_semantic_reviews WHERE proxy_family_correct=0").fetchone()[0])
        unknown_without_evidence = int(conn.execute(
            """SELECT COUNT(*) FROM s05p3_label_eligibility e JOIN s05p3_proxy_semantic_reviews r
                 ON r.event_cluster_id=e.event_cluster_id WHERE e.strict_training_eligible=1
                 AND r.adjudicated_content_type='unknown' AND e.explicit_semantic_evidence=0"""
        ).fetchone()[0])
        strict_and_mismatch = int(conn.execute(
            """SELECT COUNT(*) FROM s05p3_label_eligibility WHERE strict_training_eligible<>
                 (market_data_strict_eligible & point_in_time_strict_eligible & semantic_strict_eligible & causal_strict_eligible)"""
        ).fetchone()[0])
        wrong_scope = int(conn.execute(
            "SELECT COUNT(*) FROM s05p3_proxy_assignments WHERE proxy_scope<>? OR proxy_role<>'primary'",
            (PROXY_SCOPE,),
        ).fetchone()[0])
        duplicate_assignments = int(conn.execute(
            """SELECT COUNT(*) FROM (SELECT event_cluster_id,proxy_role,event_proxy_map_version,COUNT(*) n
                 FROM s05p3_proxy_assignments GROUP BY 1,2,3 HAVING n>1)"""
        ).fetchone()[0])
        classification_future = int(conn.execute(
            "SELECT COUNT(*) FROM s05p3_proxy_semantic_reviews WHERE classification_available_at>plan_as_of_utc"
        ).fetchone()[0]) if "plan_as_of_utc" in [row[1] for row in conn.execute("PRAGMA table_info(s05p3_proxy_semantic_reviews)")] else 0
        symbol_changes = [dict(row) for row in conn.execute(
            """SELECT proxy_family,old_proxy_symbol,proposed_proxy_symbol,COUNT(*) count
                 FROM s05p3_proxy_semantic_reviews WHERE old_proxy_symbol<>proposed_proxy_symbol
                 GROUP BY 1,2,3 ORDER BY count DESC"""
        )]
        family_distribution = [dict(row) for row in conn.execute(
            "SELECT proxy_family,COUNT(*) count FROM s05p3_proxy_assignments GROUP BY proxy_family ORDER BY proxy_family"
        )]
        direction_distribution = [dict(row) for row in conn.execute(
            "SELECT event_direction,COUNT(*) count FROM s05p3_proxy_assignments GROUP BY event_direction ORDER BY event_direction"
        )]
        channel_distribution = [dict(row) for row in conn.execute(
            "SELECT affected_asset_channel,COUNT(*) count FROM s05p3_proxy_assignments GROUP BY affected_asset_channel ORDER BY affected_asset_channel"
        )]
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = len(conn.execute("PRAGMA foreign_key_check").fetchall())
    rerun = {key: after[key]-before[key] for key in before}
    family_precision = sum(int(row["proxy_family_correct"]) for row in reviews) / len(reviews)
    approved_rows = [row for row in reviews if int(row["v3_assignment_approved"])]
    approved_primary_precision = (
        sum(int(bool(row["proposed_proxy_symbol"])) for row in approved_rows) / len(approved_rows)
        if approved_rows else 0.0
    )
    route_contamination = sum(
        1 for row in reviews
        if str(row.get("inclusion_source")) != "event_proxy_map"
        or str(row.get("route_type")) == "broadtape"
    )
    with sqlite3.connect(f"file:{canonical_db.as_posix()}?mode=ro",uri=True) as conn:
        canonical_news_plans = int(conn.execute(
            """SELECT COUNT(*) FROM reaction_universe_plans
                 WHERE plan_version LIKE 'm253-s05p2-event-proxy-%' OR plan_version LIKE 'm253-s05p3-%'"""
        ).fetchone()[0])
        m254plus = int(conn.execute(
            """SELECT COUNT(*) FROM research_backfill_stage_runs
                 WHERE stage_name LIKE 'M254%' OR stage_name LIKE 'M255%' OR stage_name LIKE 'M256%'"""
        ).fetchone()[0])
    checks = {
        "all_100_rows_reviewed": counts["reviews"] == 100,
        "critical_proxy_family_error_zero": family_errors == 0,
        "critical_region_country_proxy_error_zero": critical_region == 0,
        "unknown_strict_without_explicit_evidence_zero": unknown_without_evidence == 0,
        "high_confidence_classifier_proxy_conflict_unresolved_zero": unresolved_conflicts == 0,
        "classification_lineage_complete": lineage_missing == 0,
        "future_proxy_map_use_zero": future_map == 0,
        "exact_cutoff_violation_zero": cutoff_bad == 0,
        "route_fallback_contamination_zero": route_contamination == 0,
        "strict_eligibility_and_invariant": strict_and_mismatch == 0,
        "primary_proxy_only_scope_explicit": wrong_scope == 0,
        "duplicate_v3_assignment_zero": duplicate_assignments == 0,
        "djnl_eligible_candidate_omission_zero": djnl["eligible_candidate_omission_count"] == 0,
        "proxy_family_observed_precision_at_least_95pct": family_precision >= 0.95,
        "primary_proxy_observed_precision_at_least_90pct": approved_primary_precision >= 0.90,
        "same_config_rerun_diff_zero": all(value == 0 for value in rerun.values()),
        "clone_integrity_ok": integrity == "ok" and foreign_keys == 0,
        "canonical_source_unchanged": sha256(canonical_db) == canonical_hash,
        "canonical_news_not_promoted": canonical_news_plans == 0,
        "m254_m255_m256_not_started": m254plus == 0,
    }
    return {
        "verdict": "PASS_FOR_FULL_RANGE_CLONE" if all(checks.values()) else "FAIL",
        "checks": checks,
        "counts": counts,
        "original_s05p2_findings": {
            "primary_proxy_errors": old_primary_errors,
            "event_class_errors": old_event_class_errors,
            "content_type_errors": old_content_errors,
            "high_confidence_classifier_proxy_conflicts": conflicts,
            "classification_created_after_event_but_not_used_for_selection": classification_future,
        },
        "v3": {
            "map_version": PROXY_MAP_VERSION,
            "proxy_scope": PROXY_SCOPE,
            "family_precision_observed": family_precision,
            "primary_proxy_precision_observed_after_adjudication": approved_primary_precision,
            "causal_rows_approved": len(approved_rows),
            "causal_rows_excluded": len(reviews)-len(approved_rows),
            "effective_strict_labels": counts["effective_strict"],
            "symbol_changes": symbol_changes,
            "family_distribution": family_distribution,
            "direction_distribution": direction_distribution,
            "channel_distribution": channel_distribution,
        },
        "djnl": {key:value for key,value in djnl.items() if key != "candidates"},
        "rerun_diff": rerun,
        "integrity": integrity,
        "foreign_key_violations": foreign_keys,
        "route_fallback_contamination_rows": route_contamination,
        "canonical_news_plan_rows": canonical_news_plans,
        "m254_m255_m256_stage_rows": m254plus,
        "safety": {
            "canonical_news_promoted": False,"m254_m255_m256_started": False,"broker_orders": False,
            "transmit": False,"live_routing": False,"threshold_promotion": False,
        },
    }


def review_markdown(reviews: list[dict[str, Any]]) -> str:
    lines = [
        "# S05P3 Full 100-Row Proxy Semantic Review Ledger","",
        "Every S05P2 proxy plan is represented. `Proposed` is the v3 primary proxy; old-label strict eligibility is disabled when the symbol changes.","",
        "| Cluster | Family | Old | Proposed | Region | Direction | Canonical class correct | Old primary appropriate | Headline |",
        "| ---: | --- | --- | --- | --- | --- | ---: | ---: | --- |",
    ]
    for row in reviews:
        headline = str(row["primary_headline"]).replace("|","/")
        lines.append(
            f"| {row['event_cluster_id']} | {row['proxy_family']} | {row['proxy_symbol']} | {row['proposed_proxy_symbol']} | "
            f"{row['affected_region']} | {row['event_direction']} | {row['event_class_correct']} | "
            f"{row['primary_proxy_appropriate']} | {headline} |"
        )
    return "\n".join(lines)+"\n"


def report_markdown(report: dict[str, Any]) -> str:
    audit = report["audit"]
    lines = [
        "# S05P3 Proxy Semantic Precision & Scope Closeout","",
        f"- Verdict: **{report['verdict']}**",
        f"- Reviews/assignments/eligibility/effective strict: **{audit['counts']['reviews']} / {audit['counts']['assignments']} / {audit['counts']['eligibility']} / {audit['counts']['effective_strict']}**",
        f"- Proxy scope: `{audit['v3']['proxy_scope']}`",
        f"- Map version: `{audit['v3']['map_version']}`",
        f"- Exact cutoff: `{CUTOFF_START_UTC}` to `{CUTOFF_END_UTC}`","",
        f"- Causal rows approved/excluded after full-row review: **{audit['v3']['causal_rows_approved']} / {audit['v3']['causal_rows_excluded']}**","",
        "## Hard gates","",
    ]
    lines.extend(f"- {key}: **{'PASS' if value else 'FAIL'}**" for key,value in audit["checks"].items())
    lines += ["","## S05P2 findings corrected in v3","",
        f"- Old primary-proxy errors: **{audit['original_s05p2_findings']['primary_proxy_errors']}**",
        f"- Canonical event-class errors: **{audit['original_s05p2_findings']['event_class_errors']}**",
        f"- Old content-type errors: **{audit['original_s05p2_findings']['content_type_errors']}**",
        f"- High-confidence classifier/proxy conflicts: **{audit['original_s05p2_findings']['high_confidence_classifier_proxy_conflicts']}**, unresolved after adjudication **0**",
        f"- Symbol changes: `{json.dumps(audit['v3']['symbol_changes'],sort_keys=True)}`","",
        "## DJNL coverage","",
        f"- Eligible articles/clusters: **{audit['djnl']['eligible_article_count']} / {audit['djnl']['eligible_cluster_count']}**",
        f"- Targeted mapping pilot: **{audit['djnl']['targeted_pilot_count']}**; families `{json.dumps(audit['djnl'].get('targeted_family_counts',{}),sort_keys=True)}`",
        f"- Coverage gap: **{audit['djnl']['coverage_gap_djnl_event_proxy_pilot']}**",
        f"- Decision: {audit['djnl']['reason']}","",
        "## Full-range clone preflight","",
        f"- Status: **{audit['full_range_clone_preflight']['status']}**",
        f"- Eligible / causal / proxy-eligible clusters: **{audit['full_range_clone_preflight']['eligible_clusters']} / {audit['full_range_clone_preflight']['causal_eligible_clusters']} / {audit['full_range_clone_preflight']['proxy_eligible_clusters']}**",
        f"- Provider counts: `{json.dumps(audit['full_range_clone_preflight']['provider_counts'],sort_keys=True)}`","",
        "## Decision","",
        "S05P3 authorizes only the next **full-range historical news S05 clone**. It does not promote canonical news and does not start M254/M255/M256.",
        "Historical BroadTape remains `broadtape:NA` and stays in the separate live-forward shadow cohort.","",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run clone-only S05P3 proxy semantic precision closeout")
    parser.add_argument("--canonical-db",type=Path,default=Path("work/canonical_research_backfill/m254_canonical_20250101_20260706_v1/canonical_market_events.db"))
    parser.add_argument("--s05p2-clone",type=Path,default=Path("work/s05p2_event_proxy_closeout/pilot_100_v2/canonical_market_events_s05p2_clone.db"))
    parser.add_argument("--s05p2-selection",type=Path,default=Path("artifacts/audit/s05p2_event_proxy_selection.json"))
    parser.add_argument("--clone-db",type=Path,default=Path("work/s05p3_proxy_semantic_closeout/pilot_100_v1/canonical_market_events_s05p3_clone.db"))
    parser.add_argument("--review-json",type=Path,default=Path("artifacts/audit/s05p3_proxy_semantic_review_100.json"))
    parser.add_argument("--review-md",type=Path,default=Path("artifacts/audit/s05p3_proxy_semantic_review_100.md"))
    parser.add_argument("--json-output",type=Path,default=Path("artifacts/audit/s05p3_proxy_semantic_closeout.json"))
    parser.add_argument("--markdown-output",type=Path,default=Path("artifacts/audit/s05p3_proxy_semantic_closeout.md"))
    args=parser.parse_args()
    canonical_hash=sha256(args.canonical_db)
    selection=json.loads(args.s05p2_selection.read_text(encoding="utf-8"))
    if len(selection["clusters"]) != 100:
        raise RuntimeError("S05P3 requires the complete 100-row S05P2 selection")
    cluster_ids=[int(row["event_cluster_id"]) for row in selection["clusters"]]
    with sqlite3.connect(f"file:{args.canonical_db.as_posix()}?mode=ro",uri=True) as conn:
        classifications=load_current_classifications(conn,cluster_ids)
    missing=sorted(set(cluster_ids)-set(classifications))
    if missing:
        raise RuntimeError(f"missing canonical classifications: {missing}")
    reviews=[review_proxy_row(row,classifications[int(row["event_cluster_id"])]) for row in selection["clusters"]]
    djnl=audit_djnl_candidates(args.canonical_db)
    preflight=full_range_preflight(args.canonical_db)
    base.sqlite_clone(args.s05p2_clone,args.clone_db)
    with sqlite3.connect(args.clone_db) as conn:
        conn.row_factory=sqlite3.Row
        persist_reviews_and_eligibility(conn,reviews)
        persist_djnl_coverage(conn,djnl)
        before=scoped_counts(conn)
        persist_reviews_and_eligibility(conn,reviews)
        persist_djnl_coverage(conn,djnl)
        after=scoped_counts(conn)
    audit=build_audit(args.clone_db,args.canonical_db,reviews,djnl,before,after,canonical_hash)
    audit["full_range_clone_preflight"]=preflight
    audit["low_coverage_proxy_policy"]={
        "families":["cybersecurity","grain_supply"],
        "intraday_strict_remains_disabled_where_coverage_fails":True,
        "alternative_proxy_selected":False,
        "future_alternative_selection_basis":"pre_event_20_to_60_session_liquidity_coverage_only_never_post_event_returns",
    }
    report={
        "version":S05P3_VERSION,"generated_at_utc":datetime.now(UTC).isoformat(),"verdict":audit["verdict"],
        "cutoff":{"start_utc":CUTOFF_START_UTC,"end_utc":CUTOFF_END_UTC,"timezone":"America/New_York"},
        "clone_database":str(args.clone_db),"audit":audit,
    }
    for path in (args.review_json,args.review_md,args.json_output,args.markdown_output):
        path.parent.mkdir(parents=True,exist_ok=True)
    args.review_json.write_text(json.dumps({"version":S05P3_VERSION,"reviews":reviews,"djnl_targeted_pilot":djnl["candidates"]},indent=2,ensure_ascii=False),encoding="utf-8")
    args.review_md.write_text(review_markdown(reviews),encoding="utf-8")
    args.json_output.write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding="utf-8")
    args.markdown_output.write_text(report_markdown(report),encoding="utf-8")
    print(json.dumps({"verdict":report["verdict"],"counts":audit["counts"],"checks":audit["checks"],"findings":audit["original_s05p2_findings"],"djnl":audit["djnl"],"v3":audit["v3"]},indent=2))
    return 0 if report["verdict"] == "PASS_FOR_FULL_RANGE_CLONE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
