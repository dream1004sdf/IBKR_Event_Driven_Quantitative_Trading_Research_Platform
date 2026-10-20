from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


NY = ZoneInfo("America/New_York")
STRATA = (
    "cross_provider_confirmed",
    "marketaux_only_discovery",
    "bz_single_stock_breaking",
    "djnl_macro_policy",
    "broadtape_no_direct_entity",
    "earnings_guidance",
    "ma_regulatory",
    "geopolitical",
    "energy",
    "defense",
    "cybersecurity",
    "china_taiwan",
    "exact_second_timestamp",
    "unresolved_ambiguous",
)


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=ZoneInfo("UTC"))


def session_bucket(value: datetime) -> str:
    local = value.astimezone(NY)
    minute = local.hour * 60 + local.minute
    if 4 * 60 <= minute < 9 * 60 + 30:
        return "premarket"
    if 9 * 60 + 30 <= minute < 16 * 60:
        return "regular"
    if 16 * 60 <= minute < 20 * 60:
        return "afterhours"
    return "outside_session"


def load_candidates(database: Path, start: str, end: str) -> list[dict]:
    with sqlite3.connect(database) as conn:
        conn.row_factory = sqlite3.Row
        rows = [
            dict(row)
            for row in conn.execute(
                """WITH article_meta AS (
                       SELECT l.event_cluster_id,
                              GROUP_CONCAT(DISTINCT lower(COALESCE(NULLIF(r.provider,''),r.source_system))) providers,
                              MAX(CASE WHEN r.route_type='broadtape' THEN 1 ELSE 0 END) has_broadtape,
                              MIN(COALESCE(
                                  v.model_visible_at_utc,
                                  CASE
                                    WHEN r.source_system='ibkr'
                                     AND r.received_at_utc>=r.published_at_utc
                                     AND r.received_at_utc<=strftime('%Y-%m-%dT%H:%M:%S+00:00',r.published_at_utc,'+1 hour')
                                    THEN r.received_at_utc
                                    WHEN r.source_system='ibkr'
                                    THEN strftime('%Y-%m-%dT%H:%M:%S+00:00',r.published_at_utc,'+60 seconds')
                                    ELSE r.available_at_utc
                                  END,
                                  r.published_at_utc
                              )) anchor_at_utc
                         FROM news_article_cluster_links l
                         JOIN raw_news_articles r ON r.id=l.news_id
                         LEFT JOIN news_visibility_policy v ON v.news_id=r.id
                        GROUP BY l.event_cluster_id
                   ), asset_meta AS (
                       SELECT event_cluster_id,
                              SUM(CASE WHEN resolver_status='resolved' AND tradable_candidate=1
                                           AND role IN ('primary','secondary')
                                           AND source NOT IN ('ibkr_route','fallback_context') THEN 1 ELSE 0 END) direct_resolved,
                              SUM(CASE WHEN resolver_status IN ('unresolved','ambiguous')
                                           AND role IN ('primary','secondary') THEN 1 ELSE 0 END) unresolved_or_ambiguous,
                              GROUP_CONCAT(CASE WHEN resolver_status='resolved' AND tradable_candidate=1
                                                    AND role IN ('primary','secondary')
                                                    AND source NOT IN ('ibkr_route','fallback_context') THEN symbol END) direct_symbols
                         FROM v_cluster_asset_candidate_aggregates
                        GROUP BY event_cluster_id
                   )
                   SELECT c.event_cluster_id,c.primary_headline,c.event_class,c.provider_count,c.article_count,
                          c.canonical_entities_json,c.first_seen_at_utc,c.cluster_status,
                          COALESCE(r.event_role,'unknown') event_role,COALESCE(r.role_family,'unknown') role_family,
                          a.providers,a.has_broadtape,a.anchor_at_utc,
                          COALESCE(m.direct_resolved,0) direct_resolved,
                          COALESCE(m.unresolved_or_ambiguous,0) unresolved_or_ambiguous,
                          COALESCE(m.direct_symbols,'') direct_symbols
                     FROM news_event_clusters c
                     JOIN article_meta a ON a.event_cluster_id=c.event_cluster_id
                     LEFT JOIN news_event_cluster_roles r ON r.event_cluster_id=c.event_cluster_id
                     LEFT JOIN asset_meta m ON m.event_cluster_id=c.event_cluster_id
                    WHERE c.cluster_type='news_event' AND c.reaction_trigger_eligible=1
                      AND a.anchor_at_utc>=? AND a.anchor_at_utc<?
                    ORDER BY a.anchor_at_utc,c.event_cluster_id""",
                (start, end),
            )
        ]
    for row in rows:
        anchor = parse_utc(row["anchor_at_utc"])
        row["session_bucket"] = session_bucket(anchor)
        row["timestamp_second"] = anchor.second
        providers = str(row.get("providers") or "").lower()
        provider_families: set[str] = set()
        for provider in providers.split(","):
            normalized = provider.strip()
            if normalized.startswith("dj"):
                provider_families.add("djnl")
            elif normalized in {"bz", "benzinga"}:
                provider_families.add("bz")
            elif "marketaux" in normalized:
                provider_families.add("marketaux")
            elif normalized:
                provider_families.add(normalized)
        row["provider_families"] = sorted(provider_families)
        if len(provider_families) > 1:
            row["provider_bucket"] = "cross_provider"
        elif "djnl" in provider_families:
            row["provider_bucket"] = "djnl"
        elif "bz" in provider_families:
            row["provider_bucket"] = "bz"
        elif "marketaux" in provider_families:
            row["provider_bucket"] = "marketaux"
        else:
            row["provider_bucket"] = "other"
    return rows


def matches(row: dict, stratum: str) -> bool:
    text = f"{row.get('primary_headline','')} {row.get('event_class','')} {row.get('event_role','')}".lower()
    provider = row["provider_bucket"]
    direct = int(row.get("direct_resolved") or 0)
    if stratum == "cross_provider_confirmed":
        return provider == "cross_provider"
    if stratum == "marketaux_only_discovery":
        return provider == "marketaux"
    if stratum == "bz_single_stock_breaking":
        return provider == "bz" and direct > 0
    if stratum == "djnl_macro_policy":
        return provider == "djnl" and bool(re.search(r"\b(fed|fomc|rate|inflation|policy|tariff|jobs|payroll)\b", text))
    if stratum == "broadtape_no_direct_entity":
        return bool(row.get("has_broadtape")) and direct == 0
    if stratum == "earnings_guidance":
        return "earnings_guidance" in text or bool(re.search(r"\b(earnings|guidance|revenue|eps)\b", text))
    if stratum == "ma_regulatory":
        return bool(re.search(r"\b(merger|acquisition|acquire|takeover|antitrust|regulator|regulatory|ftc|doj)\b", text))
    if stratum == "geopolitical":
        return "geopolitical" in text or bool(re.search(r"\b(sanction|war|ceasefire|missile|iran|russia|ukraine|israel)\b", text))
    if stratum == "energy":
        return "energy_oil" in text or bool(re.search(r"\b(oil|crude|opec|gasoline|energy)\b", text))
    if stratum == "defense":
        return bool(re.search(r"\b(defense|military|missile|weapon|pentagon|nato)\b", text))
    if stratum == "cybersecurity":
        return bool(re.search(r"\b(cyber|hack|breach|ransomware|security software)\b", text))
    if stratum == "china_taiwan":
        return bool(re.search(r"\b(china|chinese|taiwan|beijing|semiconductor export)\b", text))
    if stratum == "exact_second_timestamp":
        return int(row.get("timestamp_second") or 0) != 0
    if stratum == "unresolved_ambiguous":
        return int(row.get("unresolved_or_ambiguous") or 0) > 0
    return False


def select(rows: list[dict], target: int, per_stratum: int) -> list[dict]:
    selected: list[dict] = []
    used: set[int] = set()
    for stratum in STRATA:
        candidates = [row for row in rows if row["event_cluster_id"] not in used and matches(row, stratum)]
        # Round-robin sessions so the sample is not dominated by one clock bucket.
        by_session = {
            session: sorted(
                [row for row in candidates if row["session_bucket"] == session],
                key=lambda row: (row["anchor_at_utc"], row["event_cluster_id"]),
            )
            for session in ("premarket", "regular", "afterhours", "outside_session")
        }
        picked: list[dict] = []
        while len(picked) < per_stratum and any(by_session.values()):
            for session in ("premarket", "regular", "afterhours", "outside_session"):
                if by_session[session] and len(picked) < per_stratum:
                    picked.append(by_session[session].pop(0))
        for row in picked:
            copy = dict(row)
            copy["selection_stratum"] = stratum
            selected.append(copy)
            used.add(int(row["event_cluster_id"]))
    if len(selected) < target:
        remaining = [row for row in rows if int(row["event_cluster_id"]) not in used]
        remaining.sort(
            key=lambda row: (
                Counter(item["provider_bucket"] for item in selected)[row["provider_bucket"]],
                Counter(item["session_bucket"] for item in selected)[row["session_bucket"]],
                row["anchor_at_utc"],
            )
        )
        for row in remaining[: target - len(selected)]:
            copy = dict(row)
            copy["selection_stratum"] = "balanced_fill"
            selected.append(copy)
    return selected[:target]


def main() -> int:
    parser = argparse.ArgumentParser(description="Select a stratified news-cluster S05 pilot sample")
    parser.add_argument("--database", type=Path, default=Path("work/canonical_research_backfill/m254_canonical_20250101_20260706_v1/canonical_market_events.db"))
    parser.add_argument("--start", default="2026-01-01T00:00:00+00:00")
    parser.add_argument("--end", default="2026-07-07T00:00:00+00:00")
    parser.add_argument("--target", type=int, default=240)
    parser.add_argument("--per-stratum", type=int, default=15)
    parser.add_argument("--output", type=Path, default=Path("artifacts/audit/s05_news_stratified_pilot_selection.json"))
    args = parser.parse_args()
    rows = load_candidates(args.database, args.start, args.end)
    stratum_availability = {
        stratum: sum(1 for row in rows if matches(row, stratum))
        for stratum in STRATA
    }
    selected = select(rows, args.target, args.per_stratum)
    report = {
        "version": "s05-news-stratified-selection-v1",
        "visibility_policy": "explicit news_visibility_policy; else IBKR received_at when within published+1h; else IBKR published+60s historical estimate",
        "database": str(args.database),
        "window": {"start": args.start, "end": args.end},
        "target": args.target,
        "candidate_count": len(rows),
        "selected_count": len(selected),
        "stratum_availability": stratum_availability,
        "unavailable_requested_strata": [
            stratum for stratum, count in stratum_availability.items() if count == 0
        ],
        "counts": {
            "stratum": dict(Counter(row["selection_stratum"] for row in selected)),
            "provider": dict(Counter(row["provider_bucket"] for row in selected)),
            "session": dict(Counter(row["session_bucket"] for row in selected)),
            "event_class": dict(Counter(row["event_class"] for row in selected)),
            "with_direct_resolved": sum(int(row["direct_resolved"] or 0) > 0 for row in selected),
            "with_unresolved_or_ambiguous": sum(int(row["unresolved_or_ambiguous"] or 0) > 0 for row in selected),
            "exact_second": sum(int(row["timestamp_second"] or 0) != 0 for row in selected),
        },
        "clusters": selected,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"selected_count": len(selected), "counts": report["counts"], "output": str(args.output)}, indent=2))
    return 0 if 200 <= len(selected) <= 500 else 2


if __name__ == "__main__":
    raise SystemExit(main())
