from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .s05_event_proxy_closeout import match_proxy_rule
from .s05_news_semantic_closeout import (
    CLASSIFIER_VERSION,
    NORMALIZATION_VERSION,
    classify_headline,
    normalize_headline,
    normalized_series_text,
    provider_family,
    stable_hash,
)
from .s05_proxy_semantic_closeout import (
    CUTOFF_END_UTC,
    CUTOFF_START_UTC,
    PROXY_MAP_VERSION,
    PROXY_SCOPE,
    infer_channel,
    infer_content_type,
    infer_direction,
    infer_primary_proxy,
    infer_region,
    is_v3_causal_proxy_headline,
)
from .s05_proxy_semantic_v4 import (
    PROXY_MAP_VERSION_V4,
    S05P4_V4_VERSION,
    classify_v4_causal_proxy_headline,
    infer_channel_v4,
    infer_primary_proxy_v4,
    infer_region_v4,
    match_proxy_rule_v4,
)


FULL_RANGE_VERSION = "s05-full-range-news-clone-v1"
FULL_RANGE_VERSION_V4 = "s05-full-range-news-clone-v4"
SOURCE_SCOPE = "historical_news_bz_djnl_marketaux_no_broadtape_v1"
PLAN_VERSION = "m253-s05-full-range-news-v1"
PLAN_VERSION_V4 = "m253-s05-full-range-news-v4"


def build_full_range_selection(
    database: Path,
    *,
    limit: int = 0,
    rules_version: str = "v3",
) -> dict[str, Any]:
    if rules_version not in {"v3", "v4"}:
        raise ValueError(f"unsupported rules_version={rules_version!r}")
    full_range_version = FULL_RANGE_VERSION_V4 if rules_version == "v4" else FULL_RANGE_VERSION
    plan_version = PLAN_VERSION_V4 if rules_version == "v4" else PLAN_VERSION
    proxy_map_version = PROXY_MAP_VERSION_V4 if rules_version == "v4" else PROXY_MAP_VERSION
    with sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        raw_rows = [dict(row) for row in conn.execute(
            """WITH visible AS (
                   SELECT l.event_cluster_id,r.id news_id,r.headline,r.source_system,r.provider,r.route_type,
                          COALESCE(v.model_visible_at_utc,
                            CASE WHEN r.source_system='ibkr' THEN strftime('%Y-%m-%dT%H:%M:%S+00:00',r.published_at_utc,'+60 seconds')
                                 ELSE r.available_at_utc END,r.published_at_utc) visible_at,
                          ROW_NUMBER() OVER(PARTITION BY l.event_cluster_id ORDER BY COALESCE(v.model_visible_at_utc,
                            CASE WHEN r.source_system='ibkr' THEN strftime('%Y-%m-%dT%H:%M:%S+00:00',r.published_at_utc,'+60 seconds')
                                 ELSE r.available_at_utc END,r.published_at_utc),r.id) rn
                     FROM news_article_cluster_links l JOIN raw_news_articles r ON r.id=l.news_id
                     LEFT JOIN news_visibility_policy v ON v.news_id=r.id)
                SELECT c.event_cluster_id,v.news_id,v.headline raw_headline,v.source_system,v.provider,v.route_type,
                       v.visible_at plan_as_of_utc
                  FROM news_event_clusters c JOIN visible v ON v.event_cluster_id=c.event_cluster_id AND v.rn=1
                 WHERE c.cluster_type='news_event' AND c.reaction_trigger_eligible=1
                   AND v.visible_at>=? AND v.visible_at<?
                 ORDER BY v.visible_at,c.event_cluster_id""",
            (CUTOFF_START_UTC,CUTOFF_END_UTC),
        )]
    cohort_rows: list[dict[str, Any]] = []
    plan_rows: list[dict[str, Any]] = []
    for row in raw_rows:
        headline = normalize_headline(str(row["raw_headline"] or ""))
        semantic = classify_headline(headline)
        v3_rule = match_proxy_rule(headline)
        proxy_candidate = v3_rule is not None
        v3_proxy_ok = bool(proxy_candidate and is_v3_causal_proxy_headline(headline))
        v4_rule = match_proxy_rule_v4(headline, provider=str(row["provider"])) if rules_version == "v4" else None
        proxy_ok = bool(v3_proxy_ok and (v4_rule is not None if rules_version == "v4" else True))
        rule = v4_rule if proxy_ok and v4_rule is not None else v3_rule
        v4_decision = classify_v4_causal_proxy_headline(
            headline,
            provider=str(row["provider"]),
            family=str(rule.family if rule is not None else ""),
        ) if rules_version == "v4" else None
        semantic_causal = bool(semantic.causal_trigger_eligible)
        sample_purpose = "causal_event" if semantic_causal else ("event_proxy_causal" if proxy_ok else "excluded_noncausal")
        record = {
            **row,
            "normalized_headline":headline,"primary_headline":headline,"anchor_at_utc":str(row["plan_as_of_utc"]),
            "provider_bucket":provider_family(str(row["provider"]),str(row["source_system"])),
            "news_content_type":semantic.news_content_type,"event_class":semantic.event_class,
            "event_class_confidence":semantic.confidence,"semantic_causal_original":int(semantic_causal),
            "causal_trigger_eligible":int(semantic_causal or proxy_ok),"sample_purpose":sample_purpose,
            "proxy_candidate":int(proxy_candidate),"proxy_eligible":int(proxy_ok),"proxy_family":"","proxy_symbol":"",
            "proxy_rule_id":"","proxy_scope":PROXY_SCOPE,"event_proxy_map_version":proxy_map_version,
            "cluster_specific_override_used":0,
            "rules_version":rules_version,"v3_proxy_eligible":int(v3_proxy_ok),
            "v4_proxy_eligible":int(proxy_ok) if rules_version == "v4" else int(v3_proxy_ok),
            "v4_exclusion_reason":str(v4_decision.exclusion_reason if v4_decision and not proxy_ok else ""),
            "v4_matched_rule":str(v4_decision.matched_rule if v4_decision else ""),
        }
        if proxy_candidate and rule is not None:
            if rules_version == "v4" and proxy_ok:
                region=infer_region_v4(rule.family,headline);channel=infer_channel_v4(rule.family,headline)
                proxy_symbol=infer_primary_proxy_v4(rule.family,region,channel)
            else:
                region=infer_region(rule.family,headline);channel=infer_channel(rule.family,headline)
                proxy_symbol=infer_primary_proxy(rule.family,region,channel,headline)
            record.update({
                "proxy_family":rule.family,"proxy_rule_id":rule.rule_id,"affected_region":region,
                "affected_asset_channel":channel,"event_direction":infer_direction(rule.family,headline),
                "adjudicated_content_type":infer_content_type(rule.family,headline),
                "proxy_symbol":proxy_symbol,
            })
        cohort_rows.append(record)
        if record["causal_trigger_eligible"]:
            plan_rows.append(dict(record))
    if limit:
        plan_rows=plan_rows[:limit]
    groups: dict[str,list[dict[str,Any]]]=defaultdict(list)
    for row in plan_rows:
        key=normalized_series_text(row["normalized_headline"])
        groups[key].append(row)
    for key,rows in groups.items():
        series_id=f"news-series:{stable_hash(full_range_version,key)[:24]}"
        weight=1.0/len(rows)
        ordered = sorted(rows,key=lambda item:(item["plan_as_of_utc"],item["event_cluster_id"]))
        for index,row in enumerate(ordered):
            row["event_series_id"]=series_id;row["series_sample_weight"]=weight
            row["novelty_type"]="new" if index==0 else "update"
            row["update_of_cluster_id"]=None if index==0 else int(ordered[0]["event_cluster_id"])
    return {
        "version":full_range_version,"rules_version":rules_version,"plan_version":plan_version,
        "cutoff":{"start_utc":CUTOFF_START_UTC,"end_utc":CUTOFF_END_UTC,"timezone":"America/New_York"},
        "source_scope":SOURCE_SCOPE,"proxy_scope":PROXY_SCOPE,"proxy_map_version":proxy_map_version,
        "eligible_count":len(cohort_rows),"plan_count":len(plan_rows),
        "counts":{
            "sample_purpose":dict(Counter(row["sample_purpose"] for row in cohort_rows)),
            "provider":dict(Counter(row["provider_bucket"] for row in cohort_rows)),
            "content_type":dict(Counter(row["news_content_type"] for row in cohort_rows)),
            "proxy_candidate_count":sum(row["proxy_candidate"] for row in cohort_rows),
            "proxy_causal_count":sum(row["proxy_eligible"] for row in cohort_rows),
            "proxy_family":dict(Counter(row["proxy_family"] for row in cohort_rows if row["proxy_candidate"])),
            "cluster_specific_override_count":sum(row["cluster_specific_override_used"] for row in cohort_rows),
            "v3_proxy_causal_count":sum(row["v3_proxy_eligible"] for row in cohort_rows),
            "v4_proxy_causal_count":sum(row["v4_proxy_eligible"] for row in cohort_rows),
            "v4_exclusion_reason":dict(Counter(row["v4_exclusion_reason"] for row in cohort_rows if row["v4_exclusion_reason"])),
        },
        "cohorts":cohort_rows,"clusters":plan_rows,
    }


def ensure_full_range_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS s05_full_range_cohorts(
            event_cluster_id INTEGER PRIMARY KEY,news_id INTEGER NOT NULL,plan_as_of_utc TEXT NOT NULL,
            provider_bucket TEXT NOT NULL,news_content_type TEXT NOT NULL,event_class TEXT NOT NULL,
            sample_purpose TEXT NOT NULL,semantic_causal_original INTEGER NOT NULL,causal_strict_eligible INTEGER NOT NULL,
            proxy_candidate INTEGER NOT NULL,proxy_eligible INTEGER NOT NULL,proxy_family TEXT NOT NULL,proxy_symbol TEXT NOT NULL,
            proxy_scope TEXT NOT NULL,proxy_map_version TEXT NOT NULL,source_scope TEXT NOT NULL,
            cluster_specific_override_used INTEGER NOT NULL,selection_version TEXT NOT NULL,created_at_utc TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS s05_full_range_stage_runs(
            stage_name TEXT PRIMARY KEY,status TEXT NOT NULL,row_count INTEGER NOT NULL,details_json TEXT NOT NULL,
            started_at_utc TEXT NOT NULL,completed_at_utc TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS s05_full_range_v4_semantic_decisions(
            event_cluster_id INTEGER PRIMARY KEY,
            rules_version TEXT NOT NULL,
            v3_proxy_eligible INTEGER NOT NULL,
            v4_proxy_eligible INTEGER NOT NULL,
            exclusion_reason TEXT NOT NULL,
            matched_rule TEXT NOT NULL,
            proxy_family TEXT NOT NULL,
            proxy_symbol TEXT NOT NULL,
            affected_region TEXT NOT NULL,
            affected_asset_channel TEXT NOT NULL,
            semantic_version TEXT NOT NULL,
            created_at_utc TEXT NOT NULL
        );
        """
    )


def persist_cohorts(conn: sqlite3.Connection, selection: dict[str, Any]) -> None:
    ensure_full_range_schema(conn)
    now=datetime.now(UTC).isoformat()
    for row in selection["cohorts"]:
        conn.execute(
            """INSERT OR REPLACE INTO s05_full_range_cohorts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (row["event_cluster_id"],row["news_id"],row["plan_as_of_utc"],row["provider_bucket"],
             row["news_content_type"],row["event_class"],row["sample_purpose"],row["semantic_causal_original"],
             row["causal_trigger_eligible"],row["proxy_candidate"],row["proxy_eligible"],row["proxy_family"],row["proxy_symbol"],
             str(selection.get("proxy_scope") or PROXY_SCOPE),str(selection.get("proxy_map_version") or PROXY_MAP_VERSION),
             SOURCE_SCOPE,row["cluster_specific_override_used"],str(selection.get("version") or FULL_RANGE_VERSION),now),
        )
        if str(selection.get("rules_version") or "v3") == "v4":
            conn.execute(
                """INSERT OR REPLACE INTO s05_full_range_v4_semantic_decisions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    row["event_cluster_id"],"v4",row["v3_proxy_eligible"],row["v4_proxy_eligible"],
                    row["v4_exclusion_reason"],row["v4_matched_rule"],row["proxy_family"],row["proxy_symbol"],
                    str(row.get("affected_region") or ""),str(row.get("affected_asset_channel") or ""),
                    S05P4_V4_VERSION,now,
                ),
            )
    conn.commit()


def record_stage(conn: sqlite3.Connection, name: str, status: str, count: int, details: dict[str, Any]) -> None:
    ensure_full_range_schema(conn)
    now=datetime.now(UTC).isoformat()
    conn.execute(
        "INSERT OR REPLACE INTO s05_full_range_stage_runs VALUES(?,?,?,?,?,?)",
        (name,status,count,json.dumps(details,sort_keys=True),now,now if status in {"completed","failed"} else ""),
    )
    conn.commit()
