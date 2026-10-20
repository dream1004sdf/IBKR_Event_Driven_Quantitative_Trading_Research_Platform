from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .s05_news_semantic_closeout import classify_headline, normalize_headline, provider_family


EVENT_PROXY_MAP_VERSION = "s05p2-event-proxy-map-v2"
SOURCE_COVERAGE_PROFILE_ID = "historical_news_bz_djnl_marketaux_no_broadtape_v1"
PLAN_VERSION = "m253-s05p2-event-proxy-v2"
MAP_VALID_FROM = "2025-01-01T00:00:00+00:00"
MAP_VALID_TO = "2026-07-07T00:00:00+00:00"


@dataclass(frozen=True)
class ProxyRule:
    family: str
    rule_id: str
    symbol: str
    family_pattern: str
    trigger_pattern: str
    quota: int
    exclude_pattern: str = ""


PROXY_RULES: tuple[ProxyRule, ...] = (
    ProxyRule("korea_risk", "proxy-korea-risk-v2", "EWY", r"\b(north korea|kim jong|pyongyang|korean peninsula|south korea.*missile)\b", r"\b(launch(?:es|ed)?|fires?|tests?|missile|nuclear|sanctions?|talks?|orders?|announces?|reports?)\b", 3, r"\b(like selling|anthropic|nvidia)\b"),
    ProxyRule("cybersecurity", "proxy-cyber-v2", "CIBR", r"\b(cyber ?attack|ransomware|data breach|security breach|malware)\b|\bhack(?:ed|ers?)\b.*\b(company|government|agency|system|network|customers?|users?|accounts?|database|platform)\b", r"\b(attack(?:s|ed)?|breach(?:ed)?|hack(?:ed|ers?)|ransomware|malware|steals?|exploits?|compromis(?:e|ed)|announces?|reports?)\b", 16, r"\b(etfs? to|flaw signals|crosswalk buttons?|voice clips?)\b"),
    ProxyRule("grain_supply", "proxy-grains-v2", "DBA", r"\b(wheat|corn|soybeans?|wasde|grain (?:prices?|exports?|imports?|supply|shortage)|crop (?:forecast|output|yield|shortage|failure))\b", r"\b(tariffs?|shortage|drought|export(?:s| ban| restriction)?|import(?:s|ing)?|forecast|output|yield|supply|failure|cuts?|raises?|announces?|reports?)\b", 4, r"\b(grain of salt|earnings call|earnings presentation|top headlines)\b"),
    ProxyRule("sanctions", "proxy-sanctions-v2", "GLD", r"\b(sanction(?:s|ed)?|export ban|asset freeze|embargo)\b", r"\b(imposes?|bans?|freezes?|embargoes?|lifts?|removes?|extends?|widens?|tightens?|plans?|approves?|announces?|retaliatory)\b", 15, r"\b(nvidia|alibaba|baidu|chips?|software|elon musk|social platform|eu fine|etf meltdown)\b"),
    ProxyRule("oil_supply", "proxy-oil-supply-v2", "XLE", r"\b(opec\+?|crude oil|oil (?:output|production|supply|exports?|prices?|demand)|hormuz|oil pipeline|refinery (?:outage|shutdown)|production cut|venezuela.{0,40}(?:oil|crude)|(?:oil|crude).{0,40}venezuela)\b", r"\b(cuts?|raises?|attack(?:s|ed)?|shutdown|outage|sanctions?|suspends?|resumes?|quota|target|compensat(?:e|ion)|closes?|announces?|reports?)\b", 15, r"\b(stock is trading|indices are trading|ahead of .* earnings|upstream earnings)\b"),
    ProxyRule("china_taiwan", "proxy-china-taiwan-v2", "EWT", r"\b(taiwan strait|china tariffs?|chinese tariffs?|pla drills?|china sanctions?)\b|\bchina\b.*\bexport controls?\b|\bexport controls?\b.*\b(china|taiwan)\b|\bbeijing\b.*\b(tariffs?|sanctions?|taiwan|export controls?)\b", r"\b(imposes?|raises?|cuts?|reduces?|pauses?|exempts?|retaliatory|truce|deal|approves?|restricts?|bans?|drills?|launches?|missile|announces?)\b", 15, r"\b(slams?|presses?|urges?|cramer|market will|markets? wobble|even if|appearance of impropriety|stock timers?)\b"),
    ProxyRule("defense_conflict", "proxy-defense-conflict-v2", "ITA", r"\b(armed conflict|military (?:strike|attack|operation)|airstrike|invasion|defense contract)\b|\b(?:ukraine|israel|iran|gaza|houthi|nato).{0,80}\b(?:attack|strike|missile|invasion|ceasefire|military|deploy)\b|\b(?:attack|strike|missile|invasion|ceasefire|military|deploy).{0,80}\b(?:ukraine|israel|iran|gaza|houthi|nato)\b", r"\b(attacks?|strikes?|invades?|launches?|fires?|intercepts?|ceasefire|contract|approves?|orders?|deploys?|announces?)\b", 16, r"\b(market summary|stocks? (?:rise|fall|pulls? back)|openai wins|war of the|earnings movers?|wall street surges)\b"),
    ProxyRule("rates_policy", "proxy-rates-policy-v2", "IEF", r"\b(fed(?:'s| chair| vice chair)|fomc|interest rate decision|rate decision|(?:boj|ecb|rbi|bank of england|japan).{0,40}interest rate|cpi.{0,50}(?:vs\.?|comes in)|payrolls?.{0,50}(?:vs\.?|comes in)|jobless claims?.{0,50}(?:vs\.?|comes in))\b", r"\b(cuts?|raises?|holds?|votes?|rate decision|unchanged at|comes in|increases?|decreases?|unexpectedly|basis points?|bps)\b", 16, r"\b(urges?|calls? for|says .* too (?:high|low)|economists? divided|warns? cuts?|shares? of|stocks?|market'?s bet|rate cut bets?|traders?|hedge fund|capital'?s|could soar|hopium|expert|einhorn|live on cnbc|etfs?|dollar rally|rate cut expected)\b"),
)

_CONTEXT_EXCLUSION = re.compile(
    r"\b(stock(?:s)? to (?:buy|watch)|best stock|technical analysis|price target|earnings preview|"
    r"earnings call|earnings presentation|results earnings|market recap|market summary|stock market today|"
    r"futures (?:rise|fall|tumble)|what you should know|top \d+|top headlines|global markets today|"
    r"stocks? (?:to|stand to|that could) (?:benefit|gain|lose)|stock news|"
    r"bulls and bears|grain of salt)\b",
    re.IGNORECASE,
)

_OUTCOME_LEAKAGE_EXCLUSION = re.compile(
    r"\b(?:is|are) trading (?:higher|lower)|\bshares? (?:rise|fall|surge|sink|jump|tumble)|"
    r"\b(?:oil|gold|stock) prices? (?:rise|fall|surge|sink|jump|tumble)|"
    r"\bmarkets? (?:react|rally|surge|tumble|wobble)|\bstock market (?:crash|rally)|"
    r"\bdow (?:sinks?|jumps?)|\bs&p 500 (?:falls?|rises?|jumps?)|\bnasdaq (?:falls?|rises?|jumps?)|"
    r"\bwall street (?:extends gains|rallies|surges)|\bsmall caps? rall(?:y|ies)|"
    r"\btech sector wins|\bwhat'?s moving markets?|\bdrops? amid",
    re.IGNORECASE,
)


def has_outcome_leakage_headline(headline: str) -> bool:
    """Return true when selection depends on an already-observed market move."""
    return bool(_OUTCOME_LEAKAGE_EXCLUSION.search(normalize_headline(headline)))


def stable_hash(*parts: Any) -> str:
    return hashlib.sha256("|".join(str(part) for part in parts).encode("utf-8")).hexdigest()


def _period(value: str) -> str:
    if value < "2025-07-01":
        return "2025_h1"
    if value < "2026-01-01":
        return "2025_h2"
    return "2026_h1"


def match_proxy_rule(headline: str) -> ProxyRule | None:
    text = normalize_headline(headline)
    if _CONTEXT_EXCLUSION.search(text) or has_outcome_leakage_headline(text):
        return None
    semantic = classify_headline(text).news_content_type
    if semantic in {"analyst_action", "opinion"}:
        return None
    for rule in PROXY_RULES:
        if (re.search(rule.family_pattern, text, re.IGNORECASE)
                and re.search(rule.trigger_pattern, text, re.IGNORECASE)
                and not (rule.exclude_pattern and re.search(rule.exclude_pattern, text, re.IGNORECASE))):
            return rule
    return None


def select_proxy_clusters(database: Path, target: int = 100) -> dict[str, Any]:
    with sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(row) for row in conn.execute(
            """WITH visible AS (
                   SELECT l.event_cluster_id,r.id news_id,r.headline,r.source_system,r.provider,r.route_type,
                          COALESCE(v.model_visible_at_utc,
                              CASE WHEN r.source_system='ibkr' THEN strftime('%Y-%m-%dT%H:%M:%S+00:00',r.published_at_utc,'+60 seconds')
                                   ELSE r.available_at_utc END,r.published_at_utc) plan_as_of_utc,
                          ROW_NUMBER() OVER(PARTITION BY l.event_cluster_id ORDER BY COALESCE(v.model_visible_at_utc,
                              CASE WHEN r.source_system='ibkr' THEN strftime('%Y-%m-%dT%H:%M:%S+00:00',r.published_at_utc,'+60 seconds')
                                   ELSE r.available_at_utc END,r.published_at_utc),r.id) rn
                     FROM news_article_cluster_links l JOIN raw_news_articles r ON r.id=l.news_id
                     LEFT JOIN news_visibility_policy v ON v.news_id=r.id
                ), cls AS (SELECT event_cluster_id,event_class FROM v_current_event_classifications)
                SELECT c.event_cluster_id,v.news_id,v.headline,v.source_system,v.provider,v.route_type,
                       v.plan_as_of_utc,COALESCE(cls.event_class,c.event_class,'') event_class
                  FROM news_event_clusters c JOIN visible v ON v.event_cluster_id=c.event_cluster_id AND v.rn=1
                  LEFT JOIN cls ON cls.event_cluster_id=c.event_cluster_id
                 WHERE c.cluster_type='news_event' AND v.plan_as_of_utc>=? AND v.plan_as_of_utc<?""",
            (MAP_VALID_FROM, MAP_VALID_TO),
        )]
        matched: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            rule = match_proxy_rule(str(row["headline"] or ""))
            if rule is None:
                continue
            row.update({
                "primary_headline": normalize_headline(str(row["headline"])),
                "anchor_at_utc": str(row["plan_as_of_utc"]),
                "provider_bucket": provider_family(str(row["provider"]), str(row["source_system"])),
                "proxy_family": rule.family,
                "proxy_rule_id": rule.rule_id,
                "proxy_symbol": rule.symbol,
                "event_proxy_map_version": EVENT_PROXY_MAP_VERSION,
                "map_valid_from": MAP_VALID_FROM,
                "map_valid_to": MAP_VALID_TO,
                "inclusion_source": "event_proxy_map",
                "inclusion_reason": f"point-in-time headline matched {rule.rule_id}",
                "proxy_causal_eligible": 1,
                "semantic_news_content_type": classify_headline(str(row["headline"] or "")).news_content_type,
                "selection_period": _period(str(row["plan_as_of_utc"])),
            })
            matched[rule.family].append(row)
        selected: list[dict[str, Any]] = []
        selected_ids: set[int] = set()
        for rule in PROXY_RULES:
            candidates = sorted(
                matched[rule.family],
                key=lambda row: (row["selection_period"], stable_hash(rule.rule_id, row["event_cluster_id"])),
            )
            buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for row in candidates:
                buckets[row["selection_period"]].append(row)
            ordered: list[dict[str, Any]] = []
            periods = ("2025_h1", "2025_h2", "2026_h1")
            while any(buckets[p] for p in periods):
                for period in periods:
                    if buckets[period]:
                        ordered.append(buckets[period].pop(0))
            for row in ordered:
                cluster_id = int(row["event_cluster_id"])
                if cluster_id in selected_ids:
                    continue
                selected.append(row)
                selected_ids.add(cluster_id)
                if sum(item["proxy_family"] == rule.family for item in selected) >= rule.quota:
                    break
        if len(selected) < target:
            extras = sorted(
                (row for values in matched.values() for row in values if int(row["event_cluster_id"]) not in selected_ids),
                key=lambda row: stable_hash("s05p2-fill", row["event_cluster_id"]),
            )
            for row in extras:
                selected.append(row)
                selected_ids.add(int(row["event_cluster_id"]))
                if len(selected) >= target:
                    break
        selected = sorted(selected[:target], key=lambda row: (row["anchor_at_utc"], row["event_cluster_id"]))
        ids = [int(row["event_cluster_id"]) for row in selected]
        visibility: dict[int, list[dict[str, Any]]] = defaultdict(list)
        if ids:
            placeholders = ",".join("?" for _ in ids)
            for row in conn.execute(
                f"""SELECT l.event_cluster_id,r.source_system,r.provider,r.route_type,
                            COALESCE(v.model_visible_at_utc,
                                CASE WHEN r.source_system='ibkr' THEN strftime('%Y-%m-%dT%H:%M:%S+00:00',r.published_at_utc,'+60 seconds')
                                     ELSE r.available_at_utc END,r.published_at_utc) visible_at
                       FROM news_article_cluster_links l JOIN raw_news_articles r ON r.id=l.news_id
                       LEFT JOIN news_visibility_policy v ON v.news_id=r.id
                      WHERE l.event_cluster_id IN ({placeholders})""",
                ids,
            ):
                visibility[int(row["event_cluster_id"])].append(dict(row))
    for row in selected:
        cutoff = str(row["plan_as_of_utc"])
        flags = {"bz": 0, "djnl": 0, "marketaux": 0}
        for article in visibility[int(row["event_cluster_id"])]:
            if str(article["visible_at"] or "") > cutoff:
                continue
            family = provider_family(str(article["provider"]), str(article["source_system"]))
            if family in flags:
                flags[family] = 1
        observed = sum(flags.values())
        row["source_coverage"] = {
            "profile_id": SOURCE_COVERAGE_PROFILE_ID,
            "broadtape_available_at_event": 0,
            "broadtape_observed": 0,
            "ticker_specific_bz_available": flags["bz"],
            "djnl_available": flags["djnl"],
            "marketaux_available": flags["marketaux"],
            "observed_provider_count": observed,
            "available_provider_count": 3,
            "confirmation_ratio": observed / 3.0,
            "source_coverage_mask": f"bz:{flags['bz']}|djnl:{flags['djnl']}|marketaux:{flags['marketaux']}|broadtape:NA",
        }
    return {
        "version": "s05p2-event-proxy-selection-v1",
        "target": target,
        "selected_count": len(selected),
        "family_counts": dict(Counter(row["proxy_family"] for row in selected)),
        "period_counts": dict(Counter(row["selection_period"] for row in selected)),
        "clusters": selected,
    }


def assets_for_selection(selection: dict[str, Any]) -> dict[int, list[dict[str, Any]]]:
    return {
        int(row["event_cluster_id"]): [{
            "symbol": row["proxy_symbol"],
            "asset_role": "event_proxy",
            "proxy_family": row["proxy_family"],
            "label_scope": "event_proxy",
            "inclusion_source": "event_proxy_map",
            "inclusion_reason": row["inclusion_reason"],
            "proxy_rule_id": row["proxy_rule_id"],
            "event_proxy_map_version": EVENT_PROXY_MAP_VERSION,
            "map_valid_from": MAP_VALID_FROM,
            "map_valid_to": MAP_VALID_TO,
            "plan_as_of_utc": row["plan_as_of_utc"],
            "resolver_status": "pending_dynamic_resolution",
            "tradable_candidate": 1,
        }]
        for row in selection["clusters"]
    }


def ensure_s05p2_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS s05p2_event_proxy_maps(
            proxy_rule_id TEXT PRIMARY KEY,proxy_family TEXT NOT NULL,asset_symbol TEXT NOT NULL,
            event_proxy_map_version TEXT NOT NULL,family_pattern TEXT NOT NULL,trigger_pattern TEXT NOT NULL,
            map_valid_from TEXT NOT NULL,map_valid_to TEXT NOT NULL,created_at_utc TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS s05p2_event_proxy_assignments(
            proxy_assignment_key TEXT PRIMARY KEY,reaction_plan_id INTEGER NOT NULL,event_cluster_id INTEGER NOT NULL,
            asset_symbol TEXT NOT NULL,asset_role TEXT NOT NULL,proxy_family TEXT NOT NULL,
            event_proxy_map_version TEXT NOT NULL,proxy_rule_id TEXT NOT NULL,inclusion_source TEXT NOT NULL,
            inclusion_reason TEXT NOT NULL,map_valid_from TEXT NOT NULL,map_valid_to TEXT NOT NULL,
            plan_as_of_utc TEXT NOT NULL,proxy_causal_eligible INTEGER NOT NULL,created_at_utc TEXT NOT NULL,
            UNIQUE(reaction_plan_id,asset_symbol,proxy_rule_id,event_proxy_map_version)
        );
        CREATE TABLE IF NOT EXISTS s05p2_source_coverage_profiles(
            source_coverage_profile_id TEXT PRIMARY KEY,historical_scope_name TEXT NOT NULL,
            broadtape_available INTEGER NOT NULL,broadtape_excluded_from_training INTEGER NOT NULL,
            available_sources_json TEXT NOT NULL,valid_from TEXT NOT NULL,valid_to TEXT NOT NULL,created_at_utc TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS s05p2_event_source_coverage(
            event_cluster_id INTEGER PRIMARY KEY,plan_as_of_utc TEXT NOT NULL,source_coverage_profile_id TEXT NOT NULL,
            broadtape_available_at_event INTEGER NOT NULL,broadtape_observed INTEGER NOT NULL,
            ticker_specific_bz_available INTEGER NOT NULL,djnl_available INTEGER NOT NULL,marketaux_available INTEGER NOT NULL,
            observed_provider_count INTEGER NOT NULL,available_provider_count INTEGER NOT NULL,
            confirmation_ratio REAL NOT NULL,source_coverage_mask TEXT NOT NULL,created_at_utc TEXT NOT NULL
        );
        """
    )


def persist_s05p2_lineage(
    conn: sqlite3.Connection,
    selection: dict[str, Any],
    plan_ids: dict[int, int],
) -> None:
    ensure_s05p2_schema(conn)
    now = datetime.now(UTC).isoformat()
    for rule in PROXY_RULES:
        conn.execute(
            """INSERT OR REPLACE INTO s05p2_event_proxy_maps VALUES(?,?,?,?,?,?,?,?,?)""",
            (rule.rule_id,rule.family,rule.symbol,EVENT_PROXY_MAP_VERSION,rule.family_pattern,
             rule.trigger_pattern,MAP_VALID_FROM,MAP_VALID_TO,now),
        )
    conn.execute(
        """INSERT OR REPLACE INTO s05p2_source_coverage_profiles VALUES(?,?,?,?,?,?,?,?)""",
        (SOURCE_COVERAGE_PROFILE_ID,SOURCE_COVERAGE_PROFILE_ID,0,1,
         json.dumps(["ibkr_bz_ticker_specific","ibkr_dow_jones","marketaux"]),MAP_VALID_FROM,MAP_VALID_TO,now),
    )
    for row in selection["clusters"]:
        cluster_id = int(row["event_cluster_id"])
        plan_id = int(plan_ids[cluster_id])
        conn.execute(
            """UPDATE reaction_universe_plans SET core_scope_version=?,event_proxy_map_version=?,reason=?
                 WHERE reaction_plan_id=?""",
            ("s05p2-event-proxy-only-v1",EVENT_PROXY_MAP_VERSION,
             "clone-only point-in-time conditional event-proxy closeout",plan_id),
        )
        key = stable_hash(plan_id,row["proxy_symbol"],row["proxy_rule_id"],EVENT_PROXY_MAP_VERSION)
        conn.execute(
            """INSERT OR REPLACE INTO s05p2_event_proxy_assignments VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (key,plan_id,cluster_id,row["proxy_symbol"],"event_proxy",row["proxy_family"],
             EVENT_PROXY_MAP_VERSION,row["proxy_rule_id"],"event_proxy_map",row["inclusion_reason"],
             MAP_VALID_FROM,MAP_VALID_TO,row["plan_as_of_utc"],1,now),
        )
        coverage = row["source_coverage"]
        conn.execute(
            """INSERT OR REPLACE INTO s05p2_event_source_coverage VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (cluster_id,row["plan_as_of_utc"],SOURCE_COVERAGE_PROFILE_ID,
             coverage["broadtape_available_at_event"],coverage["broadtape_observed"],
             coverage["ticker_specific_bz_available"],coverage["djnl_available"],coverage["marketaux_available"],
             coverage["observed_provider_count"],coverage["available_provider_count"],
             coverage["confirmation_ratio"],coverage["source_coverage_mask"],now),
        )
    conn.commit()


def persist_broadtape_probe_shadow(probe_report: Path, shadow_database: Path) -> dict[str, Any]:
    payload = json.loads(probe_report.read_text(encoding="utf-8"))
    headlines = list(payload.get("probe", {}).get("headlines", []))
    shadow_database.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(shadow_database) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS shadow_broadtape_raw(
                source_news_id TEXT PRIMARY KEY,provider TEXT NOT NULL,article_id TEXT NOT NULL,
                route_type TEXT NOT NULL,primary_route_symbol TEXT NOT NULL,raw_headline TEXT NOT NULL,
                normalized_headline TEXT NOT NULL,published_at_utc TEXT NOT NULL,received_at_utc TEXT NOT NULL,
                shadow_cluster_key TEXT NOT NULL,direct_entity_count INTEGER NOT NULL,
                fallback_spy_direct INTEGER NOT NULL,historical_training_eligible INTEGER NOT NULL,
                source_artifact TEXT NOT NULL,created_at_utc TEXT NOT NULL,
                UNIQUE(provider,article_id)
            );
            CREATE TABLE IF NOT EXISTS shadow_broadtape_event_proxies(
                shadow_proxy_key TEXT PRIMARY KEY,source_news_id TEXT NOT NULL,shadow_cluster_key TEXT NOT NULL,
                asset_symbol TEXT NOT NULL,asset_role TEXT NOT NULL,proxy_family TEXT NOT NULL,
                event_proxy_map_version TEXT NOT NULL,proxy_rule_id TEXT NOT NULL,
                inclusion_source TEXT NOT NULL,historical_training_eligible INTEGER NOT NULL,
                created_at_utc TEXT NOT NULL,UNIQUE(source_news_id,asset_symbol,proxy_rule_id)
            );
            """
        )
        before = {
            "raw": int(conn.execute("SELECT COUNT(*) FROM shadow_broadtape_raw").fetchone()[0]),
            "proxies": int(conn.execute("SELECT COUNT(*) FROM shadow_broadtape_event_proxies").fetchone()[0]),
        }
        now = datetime.now(UTC).isoformat()
        for row in headlines:
            provider = str(row.get("provider") or "")
            article_id = str(row.get("article_id") or "")
            source_news_id = f"shadow:{provider.lower()}:{article_id}"
            normalized = normalize_headline(str(row.get("headline") or ""))
            cluster_key = f"shadow-cluster:{stable_hash(normalized)[:24]}"
            conn.execute(
                """INSERT OR IGNORE INTO shadow_broadtape_raw VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (source_news_id,provider,article_id,"broadtape",str(row.get("primary_symbol") or ""),
                 str(row.get("headline") or ""),normalized,str(row.get("published_at") or ""),
                 str(row.get("received_at") or ""),cluster_key,0,0,0,str(probe_report),now),
            )
            rule = match_proxy_rule(normalized)
            if rule is not None:
                key = stable_hash(source_news_id,rule.symbol,rule.rule_id,EVENT_PROXY_MAP_VERSION)
                conn.execute(
                    """INSERT OR IGNORE INTO shadow_broadtape_event_proxies VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (key,source_news_id,cluster_key,rule.symbol,"event_proxy",rule.family,
                     EVENT_PROXY_MAP_VERSION,rule.rule_id,"event_proxy_map",0,now),
                )
        conn.commit()
        first = {
            "raw": int(conn.execute("SELECT COUNT(*) FROM shadow_broadtape_raw").fetchone()[0]),
            "proxies": int(conn.execute("SELECT COUNT(*) FROM shadow_broadtape_event_proxies").fetchone()[0]),
        }
        # Repeat the identical ingest to prove the shadow cohort is idempotent.
        for row in headlines:
            provider = str(row.get("provider") or "")
            article_id = str(row.get("article_id") or "")
            source_news_id = f"shadow:{provider.lower()}:{article_id}"
            normalized = normalize_headline(str(row.get("headline") or ""))
            cluster_key = f"shadow-cluster:{stable_hash(normalized)[:24]}"
            conn.execute(
                """INSERT OR IGNORE INTO shadow_broadtape_raw VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (source_news_id,provider,article_id,"broadtape",str(row.get("primary_symbol") or ""),
                 str(row.get("headline") or ""),normalized,str(row.get("published_at") or ""),
                 str(row.get("received_at") or ""),cluster_key,0,0,0,str(probe_report),now),
            )
        conn.commit()
        second = {
            "raw": int(conn.execute("SELECT COUNT(*) FROM shadow_broadtape_raw").fetchone()[0]),
            "proxies": int(conn.execute("SELECT COUNT(*) FROM shadow_broadtape_event_proxies").fetchone()[0]),
        }
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        contamination = int(conn.execute(
            "SELECT COUNT(*) FROM shadow_broadtape_raw WHERE direct_entity_count<>0 OR fallback_spy_direct<>0 OR historical_training_eligible<>0"
        ).fetchone()[0])
    return {
        "database": str(shadow_database),
        "source_artifact": str(probe_report),
        "before": before,
        "first": first,
        "second": second,
        "rerun_diff": {key: second[key] - first[key] for key in first},
        "contamination_rows": contamination,
        "integrity": integrity,
        "historical_training_mixed": False,
    }
