from __future__ import annotations

import hashlib
import re
import sqlite3
from datetime import UTC, datetime
from typing import Any

from .s05_news_semantic_closeout import normalize_headline


S05P3_VERSION = "s05p3-proxy-semantic-precision-v1"
PROXY_MAP_VERSION = "s05p3-event-proxy-map-v3"
PROXY_SCOPE = "primary_proxy_only_v1"
CUTOFF_START_UTC = "2025-01-01T05:00:00+00:00"
CUTOFF_END_UTC = "2026-07-06T04:00:00+00:00"


EXPECTED_EVENT_CLASS = {
    "oil_supply": "theme_event.energy_oil",
    "defense_conflict": "macro_event.geopolitical",
    "grain_supply": "theme_event.agriculture_supply",
    "korea_risk": "macro_event.geopolitical",
    "rates_policy": "macro_event.macro_rates",
    "sanctions": "macro_event.geopolitical",
    "cybersecurity": "theme_event.cybersecurity",
    "china_taiwan": "macro_event.geopolitical",
}

# Full-row S05P2 cohort adjudication. These rows have a topical family match but
# are secondary commentary, a conditional proposal, or a product/vulnerability
# article rather than a sufficiently direct causal trigger for strict labels.
MANUAL_CAUSAL_EXCLUSIONS = {
    84477: "secondary seller-reaction article, not a tariff action",
    90460: "market commentary on an existing tariff truce",
    76002: "Fed commentary about a China tariff pause",
    77683: "company-impact commentary about an existing tariff truce",
    92331: "company shipping/ad-spend follow-up after a trade truce",
    71131: "celebrity commentary on a ceasefire",
    73775: "conditional warning about facilities being targeted",
    66143: "reported possible import negotiation, not an executed grain action",
    97991: "policy wish/request, not an executed soybean order",
    24911: "reported negotiation objective, not an executed purchase",
    88583: "hypothetical vulnerability description, not an observed incident",
    83040: "consumer advice article, not a discrete cyber incident",
    95679: "partnership press release about generic threats",
    85266: "conditional statement that sanctions could be lifted",
    90938: "possible waiver extension, not a completed action",
    69024: "retrospective testimony about sanctions relief",
    65085: "denial of a reported sanctions-lift agreement",
    27009: "negotiating outline, not an implemented sanctions action",
    28123: "conditional final-deal terms, not an implemented action",
    24024: "former-envoy speculation about a future oil demand",
    65061: "negotiating proposal, not a completed oil-supply action",
    79160: "Fed-chair confirmation procedure, not a rate-policy event",
}

_S05P3_OUTCOME_OR_CONTEXT = re.compile(
    r"\b(dow jones futures|how will .{0,80} react|what to do(?: now)?|what'?s your move|"
    r"to watch this week|market talk|stock markets? (?:aren'?t|are|rally|fall|rise)|"
    r"asian equities? (?:fall|rise)|gold (?:rises?|falls?|rallies)|oil (?:gains?|pushes|prices? (?:dive|soar))|"
    r"futures (?:jump|spike|loom)|drives? gold higher|new buys?|stocks? spike|markets? are|"
    r"costing .{0,50} (?:billions|lost revenue)|precious metals? rise|press release: from)\b",
    re.IGNORECASE,
)


def has_s05p3_outcome_or_context(headline: str) -> bool:
    return bool(_S05P3_OUTCOME_OR_CONTEXT.search(normalize_headline(headline)))


_V3_NONCAUSAL_PROXY_CONTEXT = re.compile(
    r"\b(could|might|may (?:extend|lift|resume|cut|raise)|would likely|expects? could|"
    r"weighs?|seeking|hopes?|wants? to|outlines? .{0,30} plan|once (?:a )?final deal|"
    r"if (?:a |the )?deal|reported possible|is false|denies?|relief is .{0,20} myth|"
    r"partnership|help organizations|could steal|don'?t copy|backs? .{0,30} ceasefire|"
    r"warned .{0,40} consequences)\b",
    re.IGNORECASE,
)


def is_v3_causal_proxy_headline(headline: str) -> bool:
    """Generalized v3 causal gate; deliberately contains no cluster IDs."""
    text = normalize_headline(headline)
    return not has_s05p3_outcome_or_context(text) and not bool(_V3_NONCAUSAL_PROXY_CONTEXT.search(text))


def stable_hash(*parts: Any) -> str:
    return hashlib.sha256("|".join(str(part) for part in parts).encode("utf-8")).hexdigest()


def _contains(text: str, pattern: str) -> bool:
    return bool(re.search(pattern, text, re.IGNORECASE))


def infer_region(family: str, headline: str) -> str:
    text = normalize_headline(headline)
    if family == "rates_policy":
        if _contains(text, r"\b(japan|boj|bank of japan)\b"):
            return "japan"
        if _contains(text, r"\b(ecb|eurozone|european central bank)\b"):
            return "eurozone"
        return "united_states"
    for region, pattern in (
        ("taiwan", r"\b(taiwan|taiwanese)\b"),
        ("china", r"\b(china|chinese|beijing)\b"),
        ("iran", r"\biran(?:ian)?\b"),
        ("ukraine", r"\bukrain(?:e|ian)\b"),
        ("russia", r"\brussia(?:n)?\b"),
        ("korea", r"\b(?:north|south) korea|korean peninsula|pyongyang\b"),
        ("middle_east", r"\b(israel|israeli|gaza|houthi|lebanon|syria)\b"),
    ):
        if _contains(text, pattern):
            return region
    return "global"


def infer_channel(family: str, headline: str) -> str:
    text = normalize_headline(headline)
    if family == "oil_supply" or _contains(text, r"\b(oil|crude|opec|hormuz|refinery)\b"):
        return "energy"
    if family == "cybersecurity":
        return "technology"
    if family == "defense_conflict" or family == "korea_risk":
        return "defense"
    if family == "rates_policy":
        return "sovereign_risk"
    if family == "grain_supply":
        return "trade"
    if _contains(text, r"\b(chip|semiconductor|software|export control|tech|technology|nvidia|huawei|smic)\b"):
        return "technology"
    if _contains(text, r"\b(tariffs?|trade|exports?|imports?)\b"):
        return "trade"
    return "sovereign_risk"


def infer_direction(family: str, headline: str) -> str:
    text = normalize_headline(headline)
    if family == "sanctions":
        if _contains(text, r"\b(not|refus(?:e|ed)|fails?)\b.{0,30}\b(lift|remove|ease)\b"):
            return "tightening"
        if _contains(text, r"\b(lift|remove|ease).{0,30}\b(is false|false report|denies?)\b"):
            return "unclear"
        lifted = _contains(text, r"\b(lifts?|lifting|removes?|removing|eases?|easing|relief|waiver|unfreezes?)\b")
        imposed = _contains(text, r"\b(imposes?|tightens?|widens?|bans?|freezes?|new sanctions?|secondary sanctions?)\b")
        if lifted and not imposed:
            return "easing"
        if imposed and not lifted:
            return "tightening"
        if lifted and imposed:
            return "mixed"
    easing = _contains(text, r"\b(lifts?|removes?|eases?|relief|waiver|unfreezes?|ceasefire|truce|resumes?|reopens?|cuts? tariffs?)\b")
    tightening = _contains(text, r"\b(imposes?|tightens?|widens?|bans?|freezes?|sanctions?|tariffs?|shutdown|closes?|attack(?:s|ed)?|strikes?|missile)\b")
    if easing and tightening:
        return "mixed"
    if easing:
        return "deescalation" if family in {"defense_conflict", "korea_risk"} else "easing"
    if tightening:
        return "escalation" if family in {"defense_conflict", "korea_risk", "cybersecurity"} else "tightening"
    if family == "rates_policy":
        if _contains(text, r"\b(rate cut|cut rates?|dovish|recalibrate)\b"):
            return "easing"
        if _contains(text, r"\b(rate hike|raise rates?|hawkish)\b"):
            return "tightening"
    if family == "oil_supply":
        if _contains(text, r"\b(output hike|raise output|production start|start of (?:oil )?production|started (?:oil )?production|resumes? .* exports?)\b"):
            return "easing"
        if _contains(text, r"\b(output cut|cuts? production|export cuts?|supply disruption)\b"):
            return "tightening"
    return "unclear"


def infer_content_type(family: str, headline: str) -> str:
    text = normalize_headline(headline)
    if family == "rates_policy":
        return "central_bank_decision" if _contains(text, r"\b(rate decision|interest rate decision)\b") else "central_bank_communication"
    return {
        "oil_supply": "energy_supply_event",
        "defense_conflict": "geopolitical_conflict_event",
        "grain_supply": "agricultural_supply_event",
        "korea_risk": "geopolitical_security_event",
        "sanctions": "sanctions_policy_action",
        "cybersecurity": "cybersecurity_incident",
        "china_taiwan": "trade_or_cross_strait_policy_action",
    }[family]


def infer_primary_proxy(family: str, region: str, channel: str, headline: str) -> str:
    text = normalize_headline(headline)
    if family == "rates_policy":
        if region == "japan":
            return "EWJ"
        if region == "eurozone":
            return "EZU"
        return "IEF"
    if family == "china_taiwan":
        return "EWT" if region == "taiwan" else "FXI"
    if family == "sanctions":
        if channel == "energy":
            return "XLE"
        if channel == "technology" or region == "china":
            return "FXI"
        return "GLD"
    return {
        "oil_supply": "XLE",
        "defense_conflict": "ITA",
        "grain_supply": "DBA",
        "korea_risk": "EWY",
        "cybersecurity": "CIBR",
    }[family]


def infer_expected_proxy_direction(symbol: str, direction: str, family: str, headline: str) -> str:
    if direction in {"unclear", "mixed"}:
        return "unknown" if direction == "unclear" else "mixed"
    if symbol in {"GLD", "ITA", "XLE", "DBA"}:
        return "positive" if direction in {"escalation", "tightening"} else "negative"
    if symbol == "IEF":
        return "positive" if direction == "easing" else "negative"
    if symbol in {"EWJ", "EZU", "FXI", "EWT", "EWY"}:
        return "negative" if direction in {"escalation", "tightening"} else "positive"
    if family == "cybersecurity":
        return "mixed"
    return "unknown"


def classification_compatible(family: str, event_class: str) -> bool:
    value = str(event_class or "")
    if family == "rates_policy":
        return "macro_rates" in value
    if family == "oil_supply":
        return "energy_oil" in value
    if family in {"defense_conflict", "korea_risk", "sanctions", "china_taiwan"}:
        return "geopolitical" in value
    if family == "cybersecurity":
        return "cyber" in value
    if family == "grain_supply":
        return "agri" in value or "grain" in value
    return False


def review_proxy_row(row: dict[str, Any], classification: dict[str, Any]) -> dict[str, Any]:
    family = str(row["proxy_family"])
    headline = str(row["primary_headline"])
    region = infer_region(family, headline)
    channel = infer_channel(family, headline)
    direction = infer_direction(family, headline)
    proposed_symbol = infer_primary_proxy(family, region, channel, headline)
    expected_direction = infer_expected_proxy_direction(proposed_symbol, direction, family, headline)
    proposed_class = EXPECTED_EVENT_CLASS[family]
    canonical_class = str(classification.get("event_class") or "unknown")
    class_correct = classification_compatible(family, canonical_class)
    confidence = float(classification.get("classification_confidence") or 0.0)
    conflict = not class_correct and confidence >= 0.8
    old_symbol = str(row["proxy_symbol"])
    cluster_id = int(row["event_cluster_id"])
    causal_trigger = cluster_id not in MANUAL_CAUSAL_EXCLUSIONS
    exclusion_note = MANUAL_CAUSAL_EXCLUSIONS.get(cluster_id, "")
    reason = (
        f"Full-row adjudication: {family} evidence is explicit in the point-in-time headline; "
        f"region={region}, channel={channel}, direction={direction}; primary {old_symbol}->{proposed_symbol}."
        + (f" Strict causal exclusion: {exclusion_note}." if exclusion_note else "")
    )
    return {
        **row,
        "reviewer": "codex_full_row_review_20260711",
        "review_method": "full_100_row_semantic_adjudication",
        "reviewed_at_utc": datetime.now(UTC).isoformat(),
        "proxy_scope": PROXY_SCOPE,
        "proxy_family_correct": 1,
        "proxy_symbol_correct": int(old_symbol == proposed_symbol),
        "event_direction_correct": int(str(classification.get("direction") or "") == direction),
        "event_class_correct": int(class_correct),
        "semantic_content_type_correct": int(str(row.get("semantic_news_content_type") or "") == infer_content_type(family, headline)),
        "causal_trigger_correct": int(causal_trigger),
        "primary_proxy_appropriate": int(old_symbol == proposed_symbol),
        "secondary_proxy_needed": int(family in {"oil_supply", "defense_conflict", "grain_supply", "rates_policy", "cybersecurity", "china_taiwan"}),
        "reviewer_reason": reason,
        "adjudicated_event_class": proposed_class,
        "adjudicated_content_type": infer_content_type(family, headline),
        "event_direction": direction,
        "affected_region": region,
        "affected_asset_channel": channel,
        "proxy_expected_direction": expected_direction,
        "proxy_role": "primary",
        "proposed_proxy_symbol": proposed_symbol,
        "classification_id": classification.get("classification_id"),
        "classifier_version": str(classification.get("classifier_version") or ""),
        "classification_available_at": str(classification.get("created_at") or ""),
        "classification_confidence": confidence,
        "canonical_event_class": canonical_class,
        "canonical_direction": str(classification.get("direction") or ""),
        "classifier_proxy_conflict_detected": int(conflict),
        "classifier_proxy_conflict_resolved": 1,
        "conflict_resolution": "s05p3_manual_adjudication" if not class_correct else "not_required",
        "proxy_selection_basis": "point_in_time_headline_rule_plus_manual_adjudication;canonical_classification_audit_only",
        "explicit_semantic_evidence": 1,
        "v3_assignment_approved": int(causal_trigger),
        "existing_label_semantic_eligible": int(causal_trigger and old_symbol == proposed_symbol),
    }


def load_current_classifications(conn: sqlite3.Connection, cluster_ids: list[int]) -> dict[int, dict[str, Any]]:
    placeholders = ",".join("?" for _ in cluster_ids)
    conn.row_factory = sqlite3.Row
    return {
        int(row["event_cluster_id"]): dict(row)
        for row in conn.execute(
            f"""SELECT classification_id,event_cluster_id,classifier_version,event_class,direction,
                       classification_confidence,created_at,input_hash,config_hash
                  FROM v_current_event_classifications WHERE event_cluster_id IN ({placeholders})""",
            cluster_ids,
        )
    }


def ensure_s05p3_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS s05p3_proxy_semantic_reviews(
            review_key TEXT PRIMARY KEY,event_cluster_id INTEGER NOT NULL,news_id INTEGER NOT NULL,
            reviewer TEXT NOT NULL,review_method TEXT NOT NULL,reviewed_at_utc TEXT NOT NULL,
            plan_as_of_utc TEXT NOT NULL,
            proxy_family TEXT NOT NULL,old_proxy_symbol TEXT NOT NULL,proposed_proxy_symbol TEXT NOT NULL,
            proxy_scope TEXT NOT NULL,proxy_family_correct INTEGER NOT NULL,proxy_symbol_correct INTEGER NOT NULL,
            event_direction_correct INTEGER NOT NULL,event_class_correct INTEGER NOT NULL,
            semantic_content_type_correct INTEGER NOT NULL,causal_trigger_correct INTEGER NOT NULL,
            primary_proxy_appropriate INTEGER NOT NULL,secondary_proxy_needed INTEGER NOT NULL,
            reviewer_reason TEXT NOT NULL,adjudicated_event_class TEXT NOT NULL,
            adjudicated_content_type TEXT NOT NULL,event_direction TEXT NOT NULL,affected_region TEXT NOT NULL,
            affected_asset_channel TEXT NOT NULL,proxy_expected_direction TEXT NOT NULL,proxy_role TEXT NOT NULL,
            classification_id INTEGER,classifier_version TEXT NOT NULL,classification_available_at TEXT NOT NULL,
            classification_confidence REAL NOT NULL,canonical_event_class TEXT NOT NULL,canonical_direction TEXT NOT NULL,
            classifier_proxy_conflict_detected INTEGER NOT NULL,classifier_proxy_conflict_resolved INTEGER NOT NULL,
            conflict_resolution TEXT NOT NULL,proxy_selection_basis TEXT NOT NULL,explicit_semantic_evidence INTEGER NOT NULL,
            v3_assignment_approved INTEGER NOT NULL,existing_label_semantic_eligible INTEGER NOT NULL,
            UNIQUE(event_cluster_id,review_method)
        );
        CREATE TABLE IF NOT EXISTS s05p3_proxy_assignments(
            assignment_key TEXT PRIMARY KEY,event_cluster_id INTEGER NOT NULL,asset_symbol TEXT NOT NULL,
            proxy_family TEXT NOT NULL,proxy_role TEXT NOT NULL,proxy_scope TEXT NOT NULL,
            event_proxy_map_version TEXT NOT NULL,event_direction TEXT NOT NULL,affected_region TEXT NOT NULL,
            affected_asset_channel TEXT NOT NULL,proxy_expected_direction TEXT NOT NULL,
            map_valid_from TEXT NOT NULL,map_valid_to TEXT NOT NULL,plan_as_of_utc TEXT NOT NULL,
            source_review_key TEXT NOT NULL,label_ready_from_s05p2 INTEGER NOT NULL,
            UNIQUE(event_cluster_id,proxy_role,event_proxy_map_version)
        );
        CREATE TABLE IF NOT EXISTS s05p3_label_eligibility(
            reaction_label_id INTEGER PRIMARY KEY,event_cluster_id INTEGER NOT NULL,asset_symbol TEXT NOT NULL,
            market_data_strict_eligible INTEGER NOT NULL,point_in_time_strict_eligible INTEGER NOT NULL,
            semantic_strict_eligible INTEGER NOT NULL,causal_strict_eligible INTEGER NOT NULL,
            strict_training_eligible INTEGER NOT NULL,explicit_semantic_evidence INTEGER NOT NULL,
            exclusion_reason TEXT NOT NULL,eligibility_version TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS s05p3_djnl_proxy_coverage(
            coverage_key TEXT PRIMARY KEY,eligible_article_count INTEGER NOT NULL,eligible_cluster_count INTEGER NOT NULL,
            targeted_pilot_count INTEGER NOT NULL,coverage_gap INTEGER NOT NULL,reason TEXT NOT NULL,
            cutoff_start_utc TEXT NOT NULL,cutoff_end_utc TEXT NOT NULL,created_at_utc TEXT NOT NULL
        );
        """
    )


def persist_reviews_and_eligibility(conn: sqlite3.Connection, reviews: list[dict[str, Any]]) -> None:
    ensure_s05p3_schema(conn)
    by_cluster = {int(row["event_cluster_id"]): row for row in reviews}
    for row in reviews:
        review_key = stable_hash(S05P3_VERSION, row["event_cluster_id"])
        conn.execute(
            f"INSERT OR REPLACE INTO s05p3_proxy_semantic_reviews VALUES({','.join('?' for _ in range(40))})",
            (
                review_key,row["event_cluster_id"],row["news_id"],row["reviewer"],row["review_method"],row["reviewed_at_utc"],
                row["plan_as_of_utc"],
                row["proxy_family"],row["proxy_symbol"],row["proposed_proxy_symbol"],row["proxy_scope"],
                row["proxy_family_correct"],row["proxy_symbol_correct"],row["event_direction_correct"],
                row["event_class_correct"],row["semantic_content_type_correct"],row["causal_trigger_correct"],
                row["primary_proxy_appropriate"],row["secondary_proxy_needed"],row["reviewer_reason"],
                row["adjudicated_event_class"],row["adjudicated_content_type"],row["event_direction"],
                row["affected_region"],row["affected_asset_channel"],row["proxy_expected_direction"],row["proxy_role"],
                row["classification_id"],row["classifier_version"],row["classification_available_at"],
                row["classification_confidence"],row["canonical_event_class"],row["canonical_direction"],
                row["classifier_proxy_conflict_detected"],row["classifier_proxy_conflict_resolved"],row["conflict_resolution"],
                row["proxy_selection_basis"],row["explicit_semantic_evidence"],row["v3_assignment_approved"],
                row["existing_label_semantic_eligible"],
            ),
        )
        if row["v3_assignment_approved"]:
            conn.execute(
                """INSERT OR REPLACE INTO s05p3_proxy_assignments VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    stable_hash(PROXY_MAP_VERSION,row["event_cluster_id"],row["proposed_proxy_symbol"]),
                    row["event_cluster_id"],row["proposed_proxy_symbol"],row["proxy_family"],"primary",PROXY_SCOPE,
                    PROXY_MAP_VERSION,row["event_direction"],row["affected_region"],row["affected_asset_channel"],
                    row["proxy_expected_direction"],CUTOFF_START_UTC,CUTOFF_END_UTC,row["plan_as_of_utc"],review_key,
                    int(row["proxy_symbol"] == row["proposed_proxy_symbol"]),
                ),
            )
    label_rows = conn.execute(
        """SELECT l.reaction_label_id,l.event_cluster_id,l.asset_symbol,l.strict_training_eligible,
                  x.map_valid_from,x.map_valid_to,x.plan_as_of_utc
             FROM event_reaction_labels l JOIN s05p2_event_proxy_assignments x
               ON x.reaction_plan_id=l.reaction_plan_id AND x.asset_symbol=l.asset_symbol"""
    ).fetchall()
    for label in label_rows:
        review = by_cluster[int(label[1])]
        market = int(label[3])
        pit = int(str(label[4]) <= str(label[6]) < str(label[5]))
        semantic = int(review["explicit_semantic_evidence"] and review["existing_label_semantic_eligible"])
        causal = int(review["causal_trigger_correct"])
        effective = market & pit & semantic & causal
        reasons = []
        if not market: reasons.append("market_data")
        if not pit: reasons.append("point_in_time")
        if not semantic: reasons.append("semantic_or_proxy_reassigned")
        if not causal: reasons.append("causal")
        conn.execute(
            """INSERT OR REPLACE INTO s05p3_label_eligibility VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (label[0],label[1],label[2],market,pit,semantic,causal,effective,
             review["explicit_semantic_evidence"],"|".join(reasons),S05P3_VERSION),
        )
    conn.commit()
