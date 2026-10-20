from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from .ai_provider import PermanentAnalysisError, TransientAnalysisError
from .market_overlay import BROAD_RISK_ASSETS, CHIP_ASSETS, ENERGY_ASSETS, overlay_asset_family


STANDING_REGIME_VERSION = "standing-market-regimes-v1"
BLIND_REGIME_DISCOVERY_VERSION = "blind-standing-regime-discovery-v1"
POINT_IN_TIME_REGIME_OVERLAY_VERSION = "standing-market-regimes-point-in-time-overlay-v2"
STANDING_REGIME_OUTCOMES_VERSION = "standing-regime-outcomes-v1"
BLIND_REGIME_PROMOTION_WAITLIST_VERSION = "blind-standing-regime-promotion-waitlist-v1"

DEFAULT_REGIME_OUTCOME_SYMBOLS = ("SPY", "QQQ", "IWM", "DIA", "SMH", "SOXX", "USO", "XLE", "GLD", "VIX")

AI_BENEFICIARY_ASSETS = {
    "NVDA",
    "AMD",
    "AVGO",
    "TSM",
    "SMH",
    "SOXX",
    "QQQ",
    "MSFT",
    "GOOGL",
    "META",
    "AMZN",
    "MU",
    "LRCX",
    "ASML",
}

DEFENSE_ASSETS = {"ITA", "XAR", "LMT", "RTX", "NOC"}

CRITICAL_MINERALS_ASSETS = {
    "REMX",
    "PICK",
    "XME",
    "LIT",
    "COPX",
    "URA",
    "MP",
    "ALB",
    "FCX",
    "RIO",
    "BHP",
    "VALE",
    "TSLA",
    "GM",
    "F",
    "NVDA",
    "TSM",
    "SMH",
    "SOXX",
    "QQQ",
    "SPY",
    "VIX",
    "GLD",
    "UUP",
}

REGIME_RESOLUTION_PROFILES: dict[str, dict[str, Any]] = {
    "critical_minerals_geopolitics": {
        "resolution_prompt": "Only treat the regime as easing/resolved when export restrictions, strategic stockpiling pressure, or scarcity narratives are reversed by implemented policy or confirmed alternative supply, and market data shows supply-chain risk premia fading.",
        "resolution_confirmation_requirements": [
            "implemented policy change or signed supply agreement, not just negotiation headlines",
            "no fresh escalation headlines for multiple sessions",
            "affected assets show normalization instead of one-off relief rally",
        ],
        "post_resolution_threshold_action": "gradually remove supply-chain risk haircut; keep monitor-only status until at least two confirmation windows pass",
    },
    "global_tariff_trade_war": {
        "positive_regime_news": "new tariffs, retaliation, sanctions, export controls, or widened trade probes intensify the regime and should raise confirmation requirements for risk-on calls in exposed sectors",
        "negative_regime_news": "implemented tariff relief, delayed enforcement, formal carve-outs, or durable trade settlement can ease the regime and reduce broad risk-off bias",
        "neutral_regime_news": "campaign rhetoric, negotiation comments, or repeated unsourced speculation should refresh context but not change thresholds without policy action or market confirmation",
        "resolution_prompt": "Classify as regime_easing only when tariff/sanction pressure is formally delayed, narrowed, or settled; classify as regime_resolved only after implementation and market normalization are both visible.",
        "resolution_confirmation_requirements": [
            "official policy action, signed agreement, or published enforcement rollback",
            "two or more sessions without renewed retaliation/escalation",
            "USD/VIX/index/sector reactions confirm relief instead of short-lived headline squeeze",
        ],
        "post_resolution_threshold_action": "step down risk-off haircut gradually; do not immediately loosen all exposed-sector up calls after a single relief headline",
    },
    "taiwan_china_semiconductor_risk": {
        "positive_regime_news": "military exercises, blockade language, export controls, sanctions, or semiconductor chokepoint escalation intensify downside risk for semis/QQQ unless AI-demand evidence dominates",
        "negative_regime_news": "verified de-escalation, resumed official channels, license relief, or supply-chain diversification can ease semiconductor geopolitical risk",
        "neutral_regime_news": "routine diplomatic statements or repeated strategic commentary should refresh regime age but not alter thresholds without action or market confirmation",
        "resolution_prompt": "Treat peace-talks or softer rhetoric as easing, not resolved, unless military/policy pressure actually retreats and semiconductor option/market structure confirms lower stress.",
        "resolution_confirmation_requirements": [
            "military or export-control pressure demonstrably reduced",
            "semiconductor ETFs and volatility proxies stop pricing incremental stress",
            "no new Taiwan/China escalation headlines across multiple sessions",
        ],
        "post_resolution_threshold_action": "reduce semiconductor geopolitical haircut in stages; keep cross-check against AI boom and option structure before loosening QQQ/SMH/SOXX calls",
    },
    "middle_east_energy_security_risk": {
        "positive_regime_news": "missiles, airstrikes, shipping disruption, Hormuz/Red Sea threats, sanctions, or oil supply interruption intensify energy inflation and broad risk-off pressure",
        "negative_regime_news": "verified ceasefire implementation, reopened shipping routes, lower military tempo, or supply restoration can ease energy/security risk",
        "neutral_regime_news": "talks, proposals, or repeated local incidents with no market reaction should mainly refresh context unless oil/VIX/GLD confirms stress",
        "resolution_prompt": "Classify ceasefire or negotiation headlines as false_resolution unless implementation, shipping/oil normalization, and market confirmation are visible.",
        "resolution_confirmation_requirements": [
            "verified implementation rather than announced talks",
            "oil/shipping/GLD/VIX stress indicators normalize or stop reacting",
            "no renewed high-severity attack or chokepoint threat for multiple sessions",
        ],
        "post_resolution_threshold_action": "lower energy/security haircut gradually; keep oil and VIX confirmation gates active for the first post-resolution sessions",
    },
    "latin_america_oil_supply_risk": {
        "resolution_prompt": "Treat sanctions waivers or oil-license headlines as easing only when export/production access is actually restored and oil-market reaction confirms supply normalization.",
        "resolution_confirmation_requirements": [
            "official license, waiver, production/export data, or signed implementation evidence",
            "energy market reaction is consistent with supply normalization",
            "no immediate reversal, legal challenge, or retaliatory sanction headline",
        ],
        "post_resolution_threshold_action": "reduce oil-supply risk premium in stages while keeping energy confirmation checks active",
    },
    "russia_ukraine_war_risk": {
        "positive_regime_news": "new attacks, NATO spillover, sanctions, energy disruption, Black Sea/wheat disruption, or weapons escalation intensify war-risk thresholds",
        "negative_regime_news": "verified ceasefire implementation, durable negotiations, sanctions relief, grain/energy route normalization, or troop withdrawal can ease the regime",
        "neutral_regime_news": "routine battlefield updates or negotiation rumors should refresh context but not count as resolution without implementation and market confirmation",
        "resolution_prompt": "Do not classify a single ceasefire-talk headline as resolved. Require implementation evidence plus normalization in energy, wheat, defense, USD, VIX, and broad risk proxies.",
        "resolution_confirmation_requirements": [
            "implemented ceasefire/withdrawal/sanctions relief or reopened routes",
            "commodity/defense/volatility reaction shows durable normalization",
            "no fresh escalation across multiple sessions",
        ],
        "post_resolution_threshold_action": "step down war-risk haircut slowly; keep commodity and defense cross-checks for residual headline risk",
    },
    "korea_peninsula_security_risk": {
        "positive_regime_news": "missile launches, nuclear rhetoric, DMZ/military escalation, sanctions, or semiconductor supply-chain stress intensify Korea risk",
        "negative_regime_news": "verified diplomatic channel restoration, reduced military tempo, or official de-escalation can ease Korea peninsula risk",
        "neutral_regime_news": "routine rhetoric or low-impact military commentary should refresh context only unless EWY/KRW/SOX/VIX confirms market stress",
        "resolution_prompt": "Treat softer rhetoric as easing only; require reduced military tempo and market normalization before resolving the regime.",
        "resolution_confirmation_requirements": [
            "no new launch/escalation headlines across multiple sessions",
            "EWY/KRW/semiconductor/volatility reactions normalize",
            "official de-escalation is confirmed by both sides or by observable military tempo",
        ],
        "post_resolution_threshold_action": "reduce Korea-specific haircut gradually; keep semiconductor and FX cross-checks active",
    },
}

REGIME_RULES: tuple[dict[str, Any], ...] = (
    {
        "family": "ai_infrastructure_boom",
        "name": "AI infrastructure / semiconductor capex boom",
        "bias": "risk_on",
        "regions": {"global", "unknown", "taiwan_china"},
        "themes": {"market_news", "semiconductor_supply_chain", "trade_conflict", "unknown"},
        "keywords": (
            "ai",
            "artificial intelligence",
            "generative ai",
            "gpu",
            "accelerator",
            "data center",
            "datacenter",
            "cloud capex",
            "hyperscaler",
            "inference",
            "hbm",
            "nvidia",
            "semiconductor",
            "chip demand",
        ),
        "assets": sorted(AI_BENEFICIARY_ASSETS),
        "base_severity": 2.2,
        "threshold_profile": {
            "up_calls": "preserve or slightly loosen when company/sector evidence also confirms AI demand",
            "down_calls": "require stronger idiosyncratic negative evidence for AI beneficiaries during active regime",
            "affected_assets": sorted(AI_BENEFICIARY_ASSETS),
        },
    },
    {
        "family": "critical_minerals_geopolitics",
        "name": "Critical minerals geopolitics / strategic supply-chain risk",
        "bias": "risk_off",
        "regions": {"global", "taiwan_china", "europe", "unknown"},
        "themes": {
            "trade_conflict",
            "semiconductor_supply_chain",
            "energy_supply",
            "broad_geopolitical",
            "market_news",
            "unknown",
        },
        "keywords": (
            "critical minerals",
            "critical mineral",
            "rare earth",
            "rare earths",
            "lithium",
            "cobalt",
            "nickel",
            "graphite",
            "copper",
            "gallium",
            "germanium",
            "antimony",
            "tungsten",
            "strategic minerals",
            "mineral supply",
            "minerals agenda",
            "mineral security",
            "price floor",
            "price floors",
            "strategic stockpile",
            "strategic stockpiling",
            "export restriction",
            "export restrictions",
            "export control",
            "export controls",
            "supply chain",
        ),
        "required_keywords": (
            "critical minerals",
            "critical mineral",
            "rare earth",
            "rare earths",
            "lithium",
            "cobalt",
            "nickel",
            "graphite",
            "gallium",
            "germanium",
            "antimony",
            "tungsten",
            "strategic minerals",
            "mineral supply",
            "minerals agenda",
            "mineral security",
        ),
        "assets": sorted(CRITICAL_MINERALS_ASSETS),
        "base_severity": 2.5,
        "threshold_profile": {
            "up_calls": "tighten broad-index, semiconductor, EV, and import-dependent industrial up calls when export restrictions, price floors, or strategic stockpiling intensify; preserve miner/materials beneficiaries only when asset-specific market data confirms",
            "down_calls": "preserve supply-chain risk-off calls when policy action, export controls, or allied stockpiling evidence confirms",
            "positive_regime_news": "new export controls, price floors, resource nationalism, or strategic stockpiling usually intensify supply-chain risk and may benefit selected miners/materials while pressuring import-dependent semis, EVs, and broad risk",
            "negative_regime_news": "de-escalation, diversified supply agreements, or relaxed export restrictions can ease supply-chain risk and reduce hedge demand",
            "neutral_regime_news": "treat broad strategic-minerals commentary as freshness context unless it contains policy action, supply disruption, or market-reaction confirmation",
            **REGIME_RESOLUTION_PROFILES["critical_minerals_geopolitics"],
            "affected_assets": sorted(CRITICAL_MINERALS_ASSETS),
        },
    },
    {
        "family": "global_tariff_trade_war",
        "name": "Global tariff / trade-war pressure",
        "bias": "risk_off",
        "regions": {"global", "taiwan_china", "unknown"},
        "themes": {"trade_conflict", "semiconductor_supply_chain", "market_news", "unknown"},
        "keywords": (
            "tariff",
            "tariffs",
            "trade war",
            "trade conflict",
            "export control",
            "export controls",
            "sanction",
            "sanctions",
            "import duty",
            "duties",
            "retaliatory",
        ),
        "assets": sorted({"SPY", "QQQ", "IWM", "SMH", "SOXX", "TSM", "NVDA", "UUP", "VIX"}),
        "base_severity": 2.4,
        "threshold_profile": {
            "up_calls": "tighten confirmation threshold for tariff-exposed sectors",
            "down_calls": "preserve if market/economic desks confirm risk-off",
            **REGIME_RESOLUTION_PROFILES["global_tariff_trade_war"],
            "affected_assets": sorted({"SPY", "QQQ", "IWM", "SMH", "SOXX", "TSM", "NVDA", "UUP", "VIX"}),
        },
    },
    {
        "family": "taiwan_china_semiconductor_risk",
        "name": "Taiwan/China semiconductor supply-chain risk",
        "bias": "risk_off",
        "regions": {"taiwan_china", "global", "unknown"},
        "themes": {"semiconductor_supply_chain", "military_escalation", "trade_conflict", "market_news", "unknown"},
        "keywords": (
            "taiwan",
            "china",
            "tsmc",
            "blockade",
            "strait",
            "export control",
            "chip restriction",
            "semiconductor restriction",
        ),
        "assets": sorted(CHIP_ASSETS | {"SPY", "VIX"}),
        "base_severity": 2.6,
        "threshold_profile": {
            "up_calls": "tighten for semis/QQQ when risk is active unless AI-demand evidence is stronger",
            "down_calls": "preserve risk-off direction for semis/QQQ when fresh evidence confirms",
            **REGIME_RESOLUTION_PROFILES["taiwan_china_semiconductor_risk"],
            "affected_assets": sorted(CHIP_ASSETS | {"SPY", "VIX"}),
        },
    },
    {
        "family": "middle_east_energy_security_risk",
        "name": "Middle East energy / shipping-security risk",
        "bias": "risk_off",
        "regions": {"middle_east", "global", "unknown"},
        "themes": {"military_escalation", "energy_supply", "shipping_chokepoint", "market_news", "unknown"},
        "keywords": (
            "iran",
            "israel",
            "gaza",
            "hormuz",
            "red sea",
            "houthi",
            "missile",
            "airstrike",
            "drone",
            "ceasefire",
            "oil supply",
            "shipping",
        ),
        "assets": sorted(ENERGY_ASSETS | BROAD_RISK_ASSETS | {"GLD"}),
        "base_severity": 2.8,
        "threshold_profile": {
            "up_calls": "tighten broad-index and non-energy up calls during active escalation",
            "down_calls": "preserve broad risk-off/energy inflation calls when market data confirms",
            **REGIME_RESOLUTION_PROFILES["middle_east_energy_security_risk"],
            "affected_assets": sorted(ENERGY_ASSETS | BROAD_RISK_ASSETS | {"GLD"}),
        },
    },
    {
        "family": "latin_america_oil_supply_risk",
        "name": "Latin America oil supply / sanctions-access risk",
        "bias": "risk_off",
        "regions": {"latin_america", "global", "unknown"},
        "themes": {"energy_supply", "trade_conflict", "market_news", "broad_geopolitical", "unknown"},
        "keywords": (
            "venezuela",
            "pdvsa",
            "caracas",
            "maduro",
            "guyana",
            "latin america",
            "latin american",
            "oil license",
            "general license",
            "sanctions relief",
            "sanctions waiver",
            "oil sanctions",
            "crude exports",
            "oil exports",
            "asset swap",
            "chevron license",
            "orinoco",
        ),
        "required_keywords": (
            "venezuela",
            "pdvsa",
            "caracas",
            "maduro",
            "guyana",
            "trinidad",
            "latin america",
            "latin american",
            "orinoco",
        ),
        "assets": sorted(ENERGY_ASSETS | {"SPY", "VIX", "UUP", "GLD"}),
        "base_severity": 2.4,
        "threshold_profile": {
            "up_calls": "tighten broad-index up calls when sanctions/supply disruption risk is active; preserve energy up calls only if oil/energy market data confirms scarcity",
            "down_calls": "preserve broad risk-off or energy-supply-risk calls when sanctions, license, export, or production disruption evidence confirms",
            "positive_regime_news": "supply normalization or sanctions relief can be bearish for crude/energy equities but mildly risk-on for broad inflation-sensitive assets",
            "negative_regime_news": "new sanctions, license cancellations, export disruption, or regional escalation can be bullish for crude/energy and risk-off for broad assets",
            **REGIME_RESOLUTION_PROFILES["latin_america_oil_supply_risk"],
            "affected_assets": sorted(ENERGY_ASSETS | {"SPY", "VIX", "UUP", "GLD"}),
        },
    },
    {
        "family": "russia_ukraine_war_risk",
        "name": "Russia/Ukraine war and sanctions risk",
        "bias": "risk_off",
        "regions": {"russia_ukraine", "europe", "global", "unknown"},
        "themes": {"military_escalation", "energy_supply", "food_supply", "trade_conflict", "market_news", "unknown"},
        "keywords": (
            "russia",
            "ukraine",
            "kyiv",
            "moscow",
            "nato",
            "wheat",
            "sanction",
            "sanctions",
            "pipeline",
        ),
        "assets": sorted({"WEAT", "USO", "XLE", "ITA", "XAR", "SPY", "VIX", "UUP", "GLD"}),
        "base_severity": 2.5,
        "threshold_profile": {
            "up_calls": "tighten for Europe/energy/defense-sensitive assets when escalation is active",
            "down_calls": "preserve if energy, dollar, or volatility desks confirm",
            **REGIME_RESOLUTION_PROFILES["russia_ukraine_war_risk"],
            "affected_assets": sorted({"WEAT", "USO", "XLE", "ITA", "XAR", "SPY", "VIX", "UUP", "GLD"}),
        },
    },
    {
        "family": "korea_peninsula_security_risk",
        "name": "Korea peninsula security risk",
        "bias": "risk_off",
        "regions": {"korea_peninsula", "global", "unknown"},
        "themes": {"military_escalation", "semiconductor_supply_chain", "market_news", "unknown"},
        "keywords": (
            "north korea",
            "south korea",
            "korea peninsula",
            "missile launch",
            "pyongyang",
            "seoul",
            "dmz",
        ),
        "assets": sorted({"EWY", "KRW", "SOXX", "SMH", "QQQ", "SPY", "VIX"}),
        "base_severity": 2.3,
        "threshold_profile": {
            "up_calls": "tighten for Korea/semiconductor exposed assets while active",
            "down_calls": "preserve risk-off direction if market data confirms",
            **REGIME_RESOLUTION_PROFILES["korea_peninsula_security_risk"],
            "affected_assets": sorted({"EWY", "KRW", "SOXX", "SMH", "QQQ", "SPY", "VIX"}),
        },
    },
)


@dataclass(frozen=True)
class StandingRegimeEpisode:
    regime_key: str
    family: str
    regime_name: str
    directional_bias: str
    status: str
    region: str
    theme: str
    started_at: str
    last_seen_at: str
    resolved_at: str
    severity_score: float
    confidence: float
    affected_assets: tuple[str, ...]
    evidence_count: int
    source_counts: dict[str, int]
    latest_titles: tuple[str, ...]
    threshold_profile: dict[str, Any]
    raw: dict[str, Any]
    ingestion_version: str = STANDING_REGIME_VERSION


class RegimePolicyDraft(BaseModel):
    archetype: Literal[
        "commodity_supply_shock",
        "technology_boom",
        "trade_policy_shock",
        "military_escalation",
        "financial_stress",
        "rate_shock",
        "macro_inflation",
        "policy_easing",
        "policy_tightening",
        "energy_transition",
        "other",
        "none",
    ] = "none"
    promotion_readiness: Literal["ready", "needs_review", "insufficient"] = "insufficient"
    beneficiary_assets: list[str] = Field(default_factory=list, max_length=20)
    victim_assets: list[str] = Field(default_factory=list, max_length=20)
    hedge_assets: list[str] = Field(default_factory=list, max_length=20)
    positive_news_meaning: Literal[
        "regime_intensifying", "regime_easing", "mixed", "neutral", "unknown"
    ] = "unknown"
    negative_news_meaning: Literal[
        "regime_intensifying", "regime_easing", "mixed", "neutral", "unknown"
    ] = "unknown"
    neutral_news_meaning: Literal["monitor_only", "regime_freshness_only", "neutral", "unknown"] = "unknown"
    intensifying_threshold_action: str = Field(default="", max_length=500)
    easing_threshold_action: str = Field(default="", max_length=500)
    neutral_threshold_action: str = Field(default="", max_length=500)
    resolution_prompt: str = Field(default="", max_length=1000)
    resolution_confirmation_requirements: list[str] = Field(default_factory=list, max_length=8)
    post_resolution_threshold_action: str = Field(default="", max_length=500)
    regime_status_hint: str = Field(default="", max_length=500)


class BlindRegimeCandidateReview(BaseModel):
    candidate_key: str = Field(min_length=1)
    verdict: Literal["promote_candidate", "monitor_only", "routine_data", "already_covered", "noise"]
    proposed_family: str = Field(default="", max_length=96)
    regime_name: str = Field(default="", max_length=160)
    directional_bias: Literal["risk_on", "risk_off", "mixed", "neutral", "unknown"]
    confidence: float = Field(ge=0.0, le=1.0)
    affected_assets: list[str] = Field(default_factory=list, max_length=20)
    rationale: str = Field(min_length=1, max_length=800)
    threshold_profile_hint: str = Field(default="", max_length=800)
    missing_evidence: list[str] = Field(default_factory=list, max_length=8)
    profile_draft: RegimePolicyDraft = Field(default_factory=RegimePolicyDraft)


class BlindRegimeAIReviewBatch(BaseModel):
    review_version: str = Field(default="blind-standing-regime-ai-review-v1")
    summary: str = Field(min_length=1, max_length=1200)
    reviews: list[BlindRegimeCandidateReview] = Field(default_factory=list, max_length=80)


def _parse_dt(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _date_to_dt(value: str | date | datetime, *, end_of_day: bool = False) -> datetime:
    if isinstance(value, datetime):
        parsed = value
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    if isinstance(value, date):
        if end_of_day:
            return datetime(value.year, value.month, value.day, 23, 59, 59, tzinfo=UTC)
        return datetime(value.year, value.month, value.day, 0, 0, 0, tzinfo=UTC)
    text = str(value).strip()
    if "T" in text:
        parsed = _parse_dt(text)
        if parsed is not None:
            return parsed
    suffix = "T23:59:59+00:00" if end_of_day else "T00:00:00+00:00"
    parsed = _parse_dt(text[:10] + suffix)
    if parsed is None:
        raise ValueError(f"Unparseable date/datetime: {value!r}")
    return parsed


def _load_assets(value: Any) -> set[str]:
    if isinstance(value, (list, tuple, set)):
        return {str(item).strip().upper() for item in value if str(item).strip()}
    try:
        parsed = json.loads(str(value or "[]"))
    except json.JSONDecodeError:
        parsed = []
    if not isinstance(parsed, list):
        return set()
    return {str(item).strip().upper() for item in parsed if str(item).strip()}


def _keyword_regex(keywords: tuple[str, ...]) -> re.Pattern[str]:
    escaped = [re.escape(item).replace("\\ ", r"\s+") for item in keywords]
    return re.compile(r"\b(?:" + "|".join(escaped) + r")\b", re.I)


_RULE_REGEX = {rule["family"]: _keyword_regex(tuple(rule["keywords"])) for rule in REGIME_RULES}

_BLIND_STOPWORDS = {
    "about",
    "after",
    "against",
    "ahead",
    "amid",
    "among",
    "analyst",
    "analysts",
    "announces",
    "around",
    "baker",
    "barrel",
    "barrels",
    "before",
    "being",
    "benzinga",
    "billion",
    "brief",
    "build",
    "chevron",
    "count",
    "crude",
    "business",
    "calls",
    "could",
    "daily",
    "draw",
    "during",
    "earnings",
    "energy",
    "estimate",
    "expects",
    "from",
    "global",
    "higher",
    "hughes",
    "into",
    "inventories",
    "inventory",
    "latest",
    "lower",
    "market",
    "markets",
    "million",
    "more",
    "natgas",
    "news",
    "opec",
    "over",
    "president",
    "price",
    "prices",
    "prior",
    "production",
    "raises",
    "report",
    "reports",
    "says",
    "said",
    "shares",
    "stock",
    "stocks",
    "target",
    "than",
    "that",
    "their",
    "this",
    "today",
    "total",
    "trump",
    "trading",
    "update",
    "weekly",
    "what",
    "while",
    "will",
    "with",
    "would",
    "x2014",
}


def classify_signal_for_regimes(signal: dict[str, Any]) -> list[dict[str, Any]]:
    title = str(signal.get("title") or "")
    haystack = _signal_search_text(signal)
    region = str(signal.get("region") or "unknown")
    theme = str(signal.get("theme") or "unknown")
    assets = _load_assets(signal.get("assets_json"))
    matches: list[dict[str, Any]] = []
    for rule in REGIME_RULES:
        region_match = region in rule["regions"]
        theme_match = theme in rule["themes"]
        keyword_match = bool(_RULE_REGEX[str(rule["family"])].search(haystack))
        required_keywords = tuple(rule.get("required_keywords") or ())
        if required_keywords and not _keyword_regex(required_keywords).search(haystack):
            continue
        asset_match = bool(assets.intersection(set(rule["assets"])))
        if not (keyword_match and (region_match or theme_match or asset_match)):
            continue
        confidence = float(signal.get("confidence") or 0.0)
        severity = float(rule["base_severity"])
        if keyword_match:
            severity += 0.7
        if asset_match:
            severity += 0.5
        if confidence >= 0.7:
            severity += 0.4
        if str(signal.get("source") or "") == "emergency_breaking_news":
            severity += 0.7
        matches.append(
            {
                "family": str(rule["family"]),
                "regime_name": str(rule["name"]),
                "directional_bias": str(rule["bias"]),
                "region": region if region != "unknown" else "global",
                "theme": theme if theme != "unknown" else str(rule["family"]),
                "affected_assets": tuple(sorted(set(rule["assets"]).union(assets.intersection(set(rule["assets"]))))),
                "severity": severity,
                "confidence": confidence,
                "threshold_profile": dict(rule["threshold_profile"]),
            }
        )
    return matches


def _signal_search_text(signal: dict[str, Any]) -> str:
    title = str(signal.get("title") or "")
    raw_payload: dict[str, Any] = {}
    raw_value = signal.get("raw_json") or signal.get("raw") or {}
    if isinstance(raw_value, dict):
        raw_payload = raw_value
    else:
        try:
            parsed = json.loads(str(raw_value or "{}"))
        except json.JSONDecodeError:
            parsed = {}
        if isinstance(parsed, dict):
            raw_payload = parsed
    text_fields = [
        title,
        str(raw_payload.get("summary") or ""),
        str(raw_payload.get("description") or ""),
        str(raw_payload.get("snippet") or ""),
        str(raw_payload.get("body") or ""),
        str(raw_payload.get("article_body") or ""),
    ]
    return "\n".join(item for item in text_fields if item)


def _blind_tokens(text: str) -> list[str]:
    words = [
        item.lower()
        for item in re.findall(r"[A-Za-z][A-Za-z0-9&'\-]{2,}", text)
        if len(item) >= 4 and item.lower() not in _BLIND_STOPWORDS
    ]
    words = [item.strip("'").replace("&", "and") for item in words if not item.isdigit()]
    tokens = [item for item in words if len(item) >= 4 and item not in _BLIND_STOPWORDS]
    phrases: list[str] = []
    for left, right in zip(tokens, tokens[1:]):
        if left == right:
            continue
        phrases.append(f"{left} {right}")
    counts = Counter(tokens + phrases)
    return [token for token, _ in counts.most_common(18)]


def _episode_status(*, last_seen: datetime, build_end: datetime, active_gap_days: int, easing_gap_days: int) -> tuple[str, str]:
    age_days = max(0.0, (build_end - last_seen).total_seconds() / 86400.0)
    if age_days <= active_gap_days:
        return "active", ""
    if age_days <= easing_gap_days:
        return "easing", ""
    return "resolved", (last_seen + timedelta(days=easing_gap_days)).isoformat()


def _regime_decay(*, age_days: float, status: str, active_gap_days: int = 14, easing_gap_days: int = 45) -> float:
    status = str(status or "").lower()
    age = max(0.0, float(age_days))
    if status == "active":
        if age <= active_gap_days:
            return 1.0
        status = "easing"
    if status == "easing":
        span = max(1.0, float(easing_gap_days - active_gap_days))
        progress = max(0.0, min(1.0, (age - active_gap_days) / span))
        return round(max(0.25, 1.0 - progress * 0.65), 4)
    if status == "resolved":
        return 0.10
    return 0.50


def _regime_freshness_status(age_days: float, *, active_gap_days: int = 14, easing_gap_days: int = 45) -> str:
    age = max(0.0, float(age_days))
    if age <= 3:
        return "fresh_confirmation"
    if age <= active_gap_days:
        return "confirmed"
    if age <= easing_gap_days:
        return "aging"
    return "stale_or_resolved"


def _prediction_regime_relation(predicted_direction: str | None, directional_bias: str) -> str:
    predicted = str(predicted_direction or "").lower()
    bias = str(directional_bias or "").lower()
    if predicted not in {"up", "down"}:
        return "context_only"
    if bias == "risk_on":
        return "confirm" if predicted == "up" else "conflict"
    if bias == "risk_off":
        return "confirm" if predicted == "down" else "conflict"
    if bias == "mixed":
        return "mixed"
    return "context_only"


def standing_regime_runtime_metrics(row: dict[str, Any], *, cutoff_at: str | datetime | None = None) -> dict[str, Any]:
    cutoff = _date_to_dt(cutoff_at) if cutoff_at is not None else datetime.now(UTC)
    last_seen = _parse_dt(row.get("last_seen_at")) or cutoff
    age_days = max(0.0, (cutoff - last_seen).total_seconds() / 86400.0)
    status = str(row.get("status") or "")
    base_weight = float(row.get("severity_score") or 0.0) * float(row.get("confidence") or 0.0)
    status_decay = _regime_decay(age_days=age_days, status=status)
    effective_weight = base_weight * status_decay
    return {
        "age_days": round(age_days, 2),
        "freshness_status": _regime_freshness_status(age_days),
        "status_decay": status_decay,
        "base_weight": round(base_weight, 3),
        "effective_weight": round(effective_weight, 3),
    }


def build_standing_regime_episodes(
    signals: list[dict[str, Any]],
    *,
    start: str | date | datetime,
    end: str | date | datetime,
    max_gap_days: int = 30,
    active_gap_days: int = 14,
    easing_gap_days: int = 45,
) -> list[StandingRegimeEpisode]:
    start_dt = _date_to_dt(start)
    end_dt = _date_to_dt(end, end_of_day=True)
    grouped: dict[str, list[tuple[datetime, dict[str, Any], dict[str, Any]]]] = {}
    for signal in signals:
        published_at = _parse_dt(signal.get("published_at"))
        if published_at is None or published_at < start_dt or published_at > end_dt:
            continue
        for match in classify_signal_for_regimes(signal):
            grouped.setdefault(str(match["family"]), []).append((published_at, signal, match))

    episodes: list[StandingRegimeEpisode] = []
    for family, items in grouped.items():
        items.sort(key=lambda item: item[0])
        current: list[tuple[datetime, dict[str, Any], dict[str, Any]]] = []
        for item in items:
            if current and (item[0] - current[-1][0]).days > max_gap_days:
                episodes.append(
                    _build_episode(
                        family=family,
                        items=current,
                        build_end=end_dt,
                        active_gap_days=active_gap_days,
                        easing_gap_days=easing_gap_days,
                    )
                )
                current = []
            current.append(item)
        if current:
            episodes.append(
                _build_episode(
                    family=family,
                    items=current,
                    build_end=end_dt,
                    active_gap_days=active_gap_days,
                    easing_gap_days=easing_gap_days,
                )
            )
    episodes.sort(key=lambda item: (item.status != "active", -item.severity_score, item.family, item.started_at))
    return episodes


def _build_episode(
    *,
    family: str,
    items: list[tuple[datetime, dict[str, Any], dict[str, Any]]],
    build_end: datetime,
    active_gap_days: int,
    easing_gap_days: int,
) -> StandingRegimeEpisode:
    started = items[0][0]
    last_seen = items[-1][0]
    latest_match = items[-1][2]
    status, resolved_at = _episode_status(
        last_seen=last_seen,
        build_end=build_end,
        active_gap_days=active_gap_days,
        easing_gap_days=easing_gap_days,
    )
    source_counts = Counter(str(signal.get("source") or "unknown") for _, signal, _ in items)
    assets: set[str] = set()
    regions = Counter()
    themes = Counter()
    severities: list[float] = []
    confidences: list[float] = []
    titles: list[str] = []
    for _, signal, match in items:
        assets.update(match["affected_assets"])
        regions[str(match.get("region") or "global")] += 1
        themes[str(match.get("theme") or family)] += 1
        severities.append(float(match.get("severity") or 0.0))
        confidences.append(float(match.get("confidence") or 0.0))
        title = str(signal.get("title") or "").strip()
        if title and title not in titles:
            titles.append(title[:220])
    evidence_count = len(items)
    severity = min(10.0, (sum(severities) / len(severities)) + min(2.2, evidence_count * 0.18))
    confidence = min(1.0, 0.35 + min(0.35, evidence_count * 0.035) + min(0.30, sum(confidences) / max(1, len(confidences)) * 0.30))
    regime_key = f"{family}:{started.date().isoformat()}:{last_seen.date().isoformat()}"
    return StandingRegimeEpisode(
        regime_key=regime_key,
        family=family,
        regime_name=str(latest_match["regime_name"]),
        directional_bias=str(latest_match["directional_bias"]),
        status=status,
        region=regions.most_common(1)[0][0] if regions else "global",
        theme=themes.most_common(1)[0][0] if themes else family,
        started_at=started.isoformat(),
        last_seen_at=last_seen.isoformat(),
        resolved_at=resolved_at,
        severity_score=round(severity, 3),
        confidence=round(confidence, 3),
        affected_assets=tuple(sorted(assets)),
        evidence_count=evidence_count,
        source_counts=dict(source_counts),
        latest_titles=tuple(titles[-5:]),
        threshold_profile=dict(latest_match["threshold_profile"]),
        raw={
            "builder": STANDING_REGIME_VERSION,
            "episode_gap_days": max(0, (last_seen - started).days),
            "active_gap_days": active_gap_days,
            "easing_gap_days": easing_gap_days,
        },
    )


def upsert_standing_regime(db: Any, episode: StandingRegimeEpisode) -> tuple[int, bool]:
    now = datetime.now(UTC).isoformat()
    params = {
        "regime_key": episode.regime_key,
        "family": episode.family,
        "regime_name": episode.regime_name,
        "directional_bias": episode.directional_bias,
        "status": episode.status,
        "region": episode.region,
        "theme": episode.theme,
        "started_at": episode.started_at,
        "last_seen_at": episode.last_seen_at,
        "resolved_at": episode.resolved_at,
        "severity_score": episode.severity_score,
        "confidence": episode.confidence,
        "affected_assets_json": json.dumps(list(episode.affected_assets), ensure_ascii=False),
        "evidence_count": episode.evidence_count,
        "source_counts_json": json.dumps(episode.source_counts, ensure_ascii=False, sort_keys=True),
        "latest_titles_json": json.dumps(list(episode.latest_titles), ensure_ascii=False),
        "threshold_profile_json": json.dumps(episode.threshold_profile, ensure_ascii=False, sort_keys=True),
        "raw_json": json.dumps(episode.raw, ensure_ascii=False, sort_keys=True),
        "ingestion_version": episode.ingestion_version,
        "created_at": now,
        "updated_at": now,
    }
    with db.connect() as conn:
        before = conn.total_changes
        conn.execute(
            """INSERT OR IGNORE INTO standing_market_regimes(
                regime_key,family,regime_name,directional_bias,status,region,theme,
                started_at,last_seen_at,resolved_at,severity_score,confidence,
                affected_assets_json,evidence_count,source_counts_json,latest_titles_json,
                threshold_profile_json,raw_json,ingestion_version,created_at,updated_at
            ) VALUES (
                :regime_key,:family,:regime_name,:directional_bias,:status,:region,:theme,
                :started_at,:last_seen_at,:resolved_at,:severity_score,:confidence,
                :affected_assets_json,:evidence_count,:source_counts_json,:latest_titles_json,
                :threshold_profile_json,:raw_json,:ingestion_version,:created_at,:updated_at
            )""",
            params,
        )
        row = conn.execute(
            """SELECT id FROM standing_market_regimes
               WHERE regime_key=? AND ingestion_version=?""",
            (episode.regime_key, episode.ingestion_version),
        ).fetchone()
        if row is None:
            raise RuntimeError("standing regime insert did not return a row")
        created = conn.total_changes > before
        if not created:
            conn.execute(
                """UPDATE standing_market_regimes
                   SET regime_name=?,directional_bias=?,status=?,region=?,theme=?,
                       started_at=?,last_seen_at=?,resolved_at=?,severity_score=?,confidence=?,
                       affected_assets_json=?,evidence_count=?,source_counts_json=?,latest_titles_json=?,
                       threshold_profile_json=?,raw_json=?,updated_at=?
                   WHERE id=?""",
                (
                    episode.regime_name,
                    episode.directional_bias,
                    episode.status,
                    episode.region,
                    episode.theme,
                    episode.started_at,
                    episode.last_seen_at,
                    episode.resolved_at,
                    episode.severity_score,
                    episode.confidence,
                    params["affected_assets_json"],
                    episode.evidence_count,
                    params["source_counts_json"],
                    params["latest_titles_json"],
                    params["threshold_profile_json"],
                    params["raw_json"],
                    now,
                    int(row["id"]),
                ),
            )
        return int(row["id"]), created


def build_and_store_standing_regimes(
    db: Any,
    *,
    start: str | date | datetime,
    end: str | date | datetime,
    max_gap_days: int = 30,
    active_gap_days: int = 14,
    easing_gap_days: int = 45,
    limit: int = 100000,
) -> dict[str, Any]:
    start_dt = _date_to_dt(start)
    end_dt = _date_to_dt(end, end_of_day=True)
    with db.connect() as conn:
        signals = [
            dict(row)
            for row in conn.execute(
                """SELECT * FROM external_market_signals
                   WHERE published_at!='' AND published_at>=? AND published_at<=?
                   ORDER BY published_at ASC,id ASC
                   LIMIT ?""",
                (start_dt.isoformat(), end_dt.isoformat(), int(limit)),
            ).fetchall()
        ]
        deleted_existing = conn.execute(
            "DELETE FROM standing_market_regimes WHERE ingestion_version=?",
            (STANDING_REGIME_VERSION,),
        ).rowcount
    episodes = build_standing_regime_episodes(
        signals,
        start=start_dt,
        end=end_dt,
        max_gap_days=max_gap_days,
        active_gap_days=active_gap_days,
        easing_gap_days=easing_gap_days,
    )
    created = updated = 0
    for episode in episodes:
        _, was_created = upsert_standing_regime(db, episode)
        if was_created:
            created += 1
        else:
            updated += 1
    return {
        "version": STANDING_REGIME_VERSION,
        "start": start_dt.isoformat(),
        "end": end_dt.isoformat(),
        "signals_scanned": len(signals),
        "deleted_existing": int(deleted_existing or 0),
        "episodes": len(episodes),
        "created": created,
        "updated": updated,
        "by_status": dict(Counter(episode.status for episode in episodes)),
        "by_family": dict(Counter(episode.family for episode in episodes)),
    }


def _episode_to_row(episode: StandingRegimeEpisode) -> dict[str, Any]:
    return {
        "id": None,
        "regime_key": episode.regime_key,
        "family": episode.family,
        "regime_name": episode.regime_name,
        "directional_bias": episode.directional_bias,
        "status": episode.status,
        "region": episode.region,
        "theme": episode.theme,
        "started_at": episode.started_at,
        "last_seen_at": episode.last_seen_at,
        "resolved_at": episode.resolved_at,
        "severity_score": episode.severity_score,
        "confidence": episode.confidence,
        "affected_assets_json": json.dumps(list(episode.affected_assets), ensure_ascii=False),
        "evidence_count": episode.evidence_count,
        "source_counts_json": json.dumps(episode.source_counts, ensure_ascii=False, sort_keys=True),
        "latest_titles_json": json.dumps(list(episode.latest_titles), ensure_ascii=False),
        "threshold_profile_json": json.dumps(episode.threshold_profile, ensure_ascii=False, sort_keys=True),
        "raw_json": json.dumps(episode.raw, ensure_ascii=False, sort_keys=True),
        "ingestion_version": episode.ingestion_version,
    }


def point_in_time_standing_regime_rows(
    db: Any,
    *,
    cutoff_at: str | datetime,
    start: str | date | datetime | None = None,
    max_gap_days: int = 30,
    active_gap_days: int = 14,
    easing_gap_days: int = 45,
    limit: int = 100000,
) -> list[dict[str, Any]]:
    cutoff = _date_to_dt(cutoff_at) if not isinstance(cutoff_at, datetime) else _date_to_dt(cutoff_at)
    start_dt = _date_to_dt(start) if start is not None else datetime(2025, 1, 1, tzinfo=UTC)
    try:
        with db.connect() as conn:
            signals = [
                dict(row)
                for row in conn.execute(
                    """SELECT * FROM external_market_signals
                       WHERE published_at!='' AND published_at>=? AND published_at<=?
                       ORDER BY published_at ASC,id ASC
                       LIMIT ?""",
                    (start_dt.isoformat(), cutoff.isoformat(), int(limit)),
                ).fetchall()
            ]
    except Exception:
        return standing_regime_rows(db, cutoff_at=cutoff, limit=200)
    episodes = build_standing_regime_episodes(
        signals,
        start=start_dt,
        end=cutoff,
        max_gap_days=max_gap_days,
        active_gap_days=active_gap_days,
        easing_gap_days=easing_gap_days,
    )
    rows = [_episode_to_row(episode) for episode in episodes]
    rows.sort(
        key=lambda item: (
            {"active": 0, "easing": 1, "resolved": 2}.get(str(item.get("status") or ""), 3),
            -float(item.get("severity_score") or 0.0),
            str(item.get("family") or ""),
            str(item.get("started_at") or ""),
        )
    )
    return rows[:200]


def discover_blind_regime_candidates(
    db: Any,
    *,
    start: str | date | datetime,
    end: str | date | datetime,
    exclude_known: bool = True,
    min_evidence: int = 12,
    min_duration_days: int = 45,
    min_active_months: int = 3,
    max_gap_days: int = 45,
    exclude_themes: tuple[str, ...] = (),
    limit: int = 100000,
    top_n: int = 40,
) -> dict[str, Any]:
    """Find long-running candidate regimes without using predefined family rules.

    This is deliberately report-only. The candidates are meant for human review
    before they are promoted into `REGIME_RULES` or scorer thresholds.
    """

    start_dt = _date_to_dt(start)
    end_dt = _date_to_dt(end, end_of_day=True)
    with db.connect() as conn:
        signals = [
            dict(row)
            for row in conn.execute(
                """SELECT * FROM external_market_signals
                   WHERE published_at!='' AND published_at>=? AND published_at<=?
                   ORDER BY published_at ASC,id ASC
                   LIMIT ?""",
                (start_dt.isoformat(), end_dt.isoformat(), int(limit)),
            ).fetchall()
        ]

    clusters: dict[str, list[tuple[datetime, dict[str, Any], str]]] = {}
    known_skipped = 0
    excluded_theme_count = 0
    excluded_theme_set = {str(item).strip() for item in exclude_themes if str(item).strip()}
    for signal in signals:
        published_at = _parse_dt(signal.get("published_at"))
        if published_at is None:
            continue
        if excluded_theme_set and str(signal.get("theme") or "unknown") in excluded_theme_set:
            excluded_theme_count += 1
            continue
        known_matches = classify_signal_for_regimes(signal)
        if known_matches and exclude_known:
            known_skipped += 1
            continue
        text = _signal_search_text(signal)
        tokens = _blind_tokens(text)
        if not tokens:
            continue
        region = str(signal.get("region") or "unknown")
        theme = str(signal.get("theme") or "unknown")
        assets = sorted(_load_assets(signal.get("assets_json")) - BROAD_RISK_ASSETS)
        asset_group = ",".join(assets[:4]) if assets else "broad"
        keys: list[tuple[str, str]] = []
        seen_keys: set[tuple[str, str]] = set()
        for token in tokens[:12]:
            region_key = (f"region_theme_token:{region}:{theme}:{token}", token)
            if region_key not in seen_keys:
                keys.append(region_key)
                seen_keys.add(region_key)
            if assets:
                asset_key = (f"theme_asset_token:{theme}:{asset_group}:{token}", token)
                if asset_key not in seen_keys:
                    keys.append(asset_key)
                    seen_keys.add(asset_key)
        for key, token in keys:
            clusters.setdefault(key, []).append((published_at, signal, token))

    candidates = []
    for key, items in clusters.items():
        items.sort(key=lambda item: item[0])
        episode: list[tuple[datetime, dict[str, Any], str]] = []
        for item in items:
            if episode and (item[0] - episode[-1][0]).days > max_gap_days:
                maybe = _blind_candidate_from_episode(key, episode, end_dt)
                if maybe is not None:
                    candidates.append(maybe)
                episode = []
            episode.append(item)
        maybe = _blind_candidate_from_episode(key, episode, end_dt)
        if maybe is not None:
            candidates.append(maybe)

    filtered = [
        item
        for item in candidates
        if int(item["evidence_count"]) >= min_evidence
        and int(item["duration_days"]) >= min_duration_days
        and int(item["active_month_count"]) >= min_active_months
    ]
    deduped = _dedupe_blind_candidates(filtered)
    deduped.sort(key=lambda item: (float(item["candidate_score"]), int(item["duration_days"]), int(item["evidence_count"])), reverse=True)
    top = deduped[: max(0, int(top_n))]
    return attach_blind_regime_promotion_waitlist({
        "version": BLIND_REGIME_DISCOVERY_VERSION,
        "start": start_dt.isoformat(),
        "end": end_dt.isoformat(),
        "signals_scanned": len(signals),
        "known_signals_skipped": known_skipped,
        "theme_excluded_signals": excluded_theme_count,
        "raw_candidate_count": len(candidates),
        "filtered_candidate_count": len(filtered),
        "returned_candidate_count": len(top),
        "filters": {
            "exclude_known": exclude_known,
            "min_evidence": min_evidence,
            "min_duration_days": min_duration_days,
            "min_active_months": min_active_months,
            "max_gap_days": max_gap_days,
            "exclude_themes": sorted(excluded_theme_set),
            "top_n": top_n,
        },
        "candidates": top,
    })


def _blind_candidate_from_episode(
    key: str,
    items: list[tuple[datetime, dict[str, Any], str]],
    build_end: datetime,
) -> dict[str, Any] | None:
    if not items:
        return None
    started = items[0][0]
    last_seen = items[-1][0]
    duration_days = max(0, (last_seen - started).days)
    months = sorted({item[0].strftime("%Y-%m") for item in items})
    sources = Counter(str(signal.get("source") or "unknown") for _, signal, _ in items)
    regions = Counter(str(signal.get("region") or "unknown") for _, signal, _ in items)
    themes = Counter(str(signal.get("theme") or "unknown") for _, signal, _ in items)
    tokens = Counter(token for _, _, token in items)
    assets_counter: Counter[str] = Counter()
    titles: list[str] = []
    sentiment_values: list[float] = []
    confidence_values: list[float] = []
    for _, signal, _ in items:
        assets_counter.update(_load_assets(signal.get("assets_json")))
        title = str(signal.get("title") or "").strip()
        if title and title not in titles:
            titles.append(title[:220])
        try:
            if signal.get("sentiment_score") is not None:
                sentiment_values.append(float(signal.get("sentiment_score")))
        except (TypeError, ValueError):
            pass
        try:
            confidence_values.append(float(signal.get("confidence") or 0.0))
        except (TypeError, ValueError):
            pass
    avg_sentiment = sum(sentiment_values) / len(sentiment_values) if sentiment_values else 0.0
    avg_confidence = sum(confidence_values) / len(confidence_values) if confidence_values else 0.0
    source_diversity = len([source for source, count in sources.items() if count > 0])
    recency_days = max(0.0, (build_end - last_seen).total_seconds() / 86400.0)
    if recency_days <= 14:
        status = "active"
    elif recency_days <= 45:
        status = "easing"
    else:
        status = "resolved"
    candidate_score = (
        min(35.0, len(items) * 0.55)
        + min(25.0, duration_days / 8.0)
        + min(20.0, len(months) * 3.0)
        + min(10.0, source_diversity * 2.0)
        + min(10.0, avg_confidence * 10.0)
        - min(10.0, recency_days / 12.0)
    )
    if status == "active":
        candidate_score += 6.0
    elif status == "easing":
        candidate_score += 2.0
    key_token = key.rsplit(":", 1)[-1] if ":" in key else key
    top_tokens = [token for token, _ in tokens.most_common(8)]
    if key_token and key_token not in top_tokens:
        top_tokens = [key_token, *top_tokens[:7]]
    return {
        "candidate_key": key,
        "status": status,
        "started_at": started.isoformat(),
        "last_seen_at": last_seen.isoformat(),
        "duration_days": duration_days,
        "active_month_count": len(months),
        "months": months[:24],
        "evidence_count": len(items),
        "candidate_score": round(candidate_score, 3),
        "avg_sentiment": round(avg_sentiment, 3),
        "avg_confidence": round(avg_confidence, 3),
        "source_counts": dict(sources),
        "top_region": regions.most_common(1)[0][0] if regions else "unknown",
        "top_theme": themes.most_common(1)[0][0] if themes else "unknown",
        "top_tokens": top_tokens,
        "top_assets": [asset for asset, _ in assets_counter.most_common(12)],
        "latest_titles": titles[-6:],
    }


def _dedupe_blind_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in sorted(candidates, key=lambda row: float(row["candidate_score"]), reverse=True):
        tokens = tuple(str(token) for token in item.get("top_tokens", [])[:3])
        token_key = tokens[0] if tokens else str(item.get("candidate_key") or "")
        key = (str(item.get("top_region") or ""), str(item.get("top_theme") or ""), token_key)
        if key in seen:
            continue
        seen.add(key)
        output.append(item)
    return output


def _blind_candidate_promotion_status(candidate: dict[str, Any]) -> tuple[str, list[str], dict[str, Any]]:
    review = candidate.get("ai_review") if isinstance(candidate.get("ai_review"), dict) else {}
    draft = review.get("profile_draft") if isinstance(review.get("profile_draft"), dict) else {}
    verdict = str(review.get("verdict") or "").strip()
    readiness = str(draft.get("promotion_readiness") or "").strip()
    confidence = float(review.get("confidence") or 0.0) if review.get("confidence") is not None else 0.0
    evidence_count = int(candidate.get("evidence_count") or 0)
    duration_days = int(candidate.get("duration_days") or 0)
    active_months = int(candidate.get("active_month_count") or 0)
    candidate_status = str(candidate.get("status") or "")
    reasons: list[str] = []
    if not review:
        reasons.append("ai_review_missing")
    if verdict != "promote_candidate":
        reasons.append(f"verdict_{verdict or 'missing'}")
    if readiness != "ready":
        reasons.append(f"readiness_{readiness or 'missing'}")
    if confidence < 0.70:
        reasons.append("ai_confidence_below_0_70")
    if evidence_count < 12:
        reasons.append("evidence_count_below_12")
    if duration_days < 45:
        reasons.append("duration_below_45_days")
    if active_months < 3:
        reasons.append("active_months_below_3")
    if candidate_status == "resolved":
        reasons.append("candidate_already_resolved")
    resolution_prompt = str(draft.get("resolution_prompt") or "")
    if not resolution_prompt:
        reasons.append("resolution_prompt_missing")
    if not (review.get("affected_assets") or draft.get("affected_assets")) and not (
        draft.get("beneficiary_assets") or draft.get("victim_assets") or draft.get("hedge_assets")
    ):
        reasons.append("affected_assets_missing")
    if not reasons:
        return "promotion_waitlist", [], {
            "confidence": confidence,
            "promotion_readiness": readiness,
            "verdict": verdict,
        }
    if review and verdict in {"promote_candidate", "monitor_only"} and confidence >= 0.55 and candidate_status != "resolved":
        return "watchlist", reasons, {
            "confidence": confidence,
            "promotion_readiness": readiness,
            "verdict": verdict,
        }
    return "blocked", reasons, {
        "confidence": confidence,
        "promotion_readiness": readiness,
        "verdict": verdict,
    }


def attach_blind_regime_promotion_waitlist(result: dict[str, Any]) -> dict[str, Any]:
    candidates = []
    status_counts: Counter[str] = Counter()
    reason_counts: Counter[str] = Counter()
    for item in result.get("candidates") or []:
        if not isinstance(item, dict):
            continue
        status, reasons, details = _blind_candidate_promotion_status(item)
        status_counts[status] += 1
        for reason in reasons:
            reason_counts[reason] += 1
        review = item.get("ai_review") if isinstance(item.get("ai_review"), dict) else {}
        draft = review.get("profile_draft") if isinstance(review.get("profile_draft"), dict) else {}
        candidates.append(
            {
                "candidate_key": item.get("candidate_key"),
                "status": status,
                "reasons": reasons,
                "proposed_family": review.get("proposed_family") or item.get("candidate_key"),
                "regime_name": review.get("regime_name") or "",
                "directional_bias": review.get("directional_bias") or "unknown",
                "confidence": details.get("confidence"),
                "promotion_readiness": details.get("promotion_readiness"),
                "verdict": details.get("verdict"),
                "top_assets": item.get("top_assets") or [],
                "beneficiary_assets": draft.get("beneficiary_assets") or [],
                "victim_assets": draft.get("victim_assets") or [],
                "hedge_assets": draft.get("hedge_assets") or [],
                "threshold_profile_hint": review.get("threshold_profile_hint") or "",
                "resolution_prompt": draft.get("resolution_prompt") or "",
                "resolution_confirmation_requirements": draft.get("resolution_confirmation_requirements") or [],
                "post_resolution_threshold_action": draft.get("post_resolution_threshold_action") or "",
                "profile_draft": draft,
                "safety": {
                    "report_only": True,
                    "scorer_threshold_mutated": False,
                    "live_threshold_promoted": False,
                    "broker_orders_submitted": False,
                },
            }
        )
    output = dict(result)
    output["promotion_waitlist"] = {
        "version": BLIND_REGIME_PROMOTION_WAITLIST_VERSION,
        "mode": "report_only_shadow_waitlist",
        "candidate_count": len(candidates),
        "waitlist_count": int(status_counts.get("promotion_waitlist") or 0),
        "watchlist_count": int(status_counts.get("watchlist") or 0),
        "blocked_count": int(status_counts.get("blocked") or 0),
        "status_counts": dict(status_counts),
        "reason_counts": dict(reason_counts.most_common(20)),
        "candidates": candidates,
        "promotion_requirements": [
            "AI verdict promote_candidate",
            "profile_draft.promotion_readiness ready",
            "AI confidence >= 0.70",
            "minimum evidence/duration/month coverage",
            "resolution_prompt present",
            "affected asset map present",
            "future backtest/outcome validation before live threshold promotion",
        ],
        "safety": {
            "report_only": True,
            "scorer_threshold_mutated": False,
            "live_threshold_promoted": False,
            "broker_orders_submitted": False,
        },
    }
    return output


def _extract_blind_ai_review(response: Any) -> BlindRegimeAIReviewBatch:
    direct = getattr(response, "output_parsed", None)
    if isinstance(direct, BlindRegimeAIReviewBatch):
        return direct
    refusal_messages: list[str] = []
    for output in getattr(response, "output", []) or []:
        for content in getattr(output, "content", []) or []:
            parsed = getattr(content, "parsed", None)
            if isinstance(parsed, BlindRegimeAIReviewBatch):
                return parsed
            if getattr(content, "type", None) == "refusal":
                refusal_messages.append(str(getattr(content, "refusal", "refused")))
    if refusal_messages:
        raise PermanentAnalysisError("Model refused blind regime review: " + "; ".join(refusal_messages))
    raise TransientAnalysisError("Structured response did not contain BlindRegimeAIReviewBatch")


def _map_blind_openai_exception(exc: Exception) -> Exception:
    name = exc.__class__.__name__
    message = str(exc)[:1500]
    status = getattr(exc, "status_code", None)
    if name in {"APIConnectionError", "APITimeoutError", "RateLimitError", "InternalServerError"} or (
        isinstance(status, int) and status >= 500
    ):
        return TransientAnalysisError(f"OpenAI transient blind-regime review error ({name}): {message}")
    if name == "AuthenticationError":
        return PermanentAnalysisError("OpenAI blind-regime review authentication failed. Check OPENAI_API_KEY.")
    if name in {"PermissionDeniedError", "NotFoundError", "UnprocessableEntityError", "BadRequestError"} or (
        isinstance(status, int) and 400 <= status < 500
    ):
        return PermanentAnalysisError(f"OpenAI blind-regime review request/configuration error ({name}): {message}")
    return TransientAnalysisError(f"OpenAI unexpected blind-regime review error ({name}): {message}")


class OpenAIBlindRegimeReviewer:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        timeout_seconds: float = 90.0,
        reasoning_effort: str = "low",
        max_input_chars: int = 60000,
        client: Any | None = None,
    ) -> None:
        if not api_key and client is None:
            raise PermanentAnalysisError("OPENAI_API_KEY is missing. Add it to .env before running blind regime AI review.")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.reasoning_effort = reasoning_effort
        self.max_input_chars = max_input_chars
        if client is not None:
            self.client = client
        else:
            try:
                from openai import OpenAI
            except ImportError as exc:
                raise PermanentAnalysisError("The openai package is not installed. Run pip install -r requirements.txt.") from exc
            self.client = OpenAI(api_key=api_key, timeout=timeout_seconds, max_retries=0)

    def review(self, result: dict[str, Any], *, max_candidates: int = 20) -> dict[str, Any]:
        candidates = list(result.get("candidates") or [])[: max(0, int(max_candidates))]
        compact_candidates = [
            {
                "candidate_key": item.get("candidate_key"),
                "status": item.get("status"),
                "region": item.get("top_region"),
                "theme": item.get("top_theme"),
                "evidence_count": item.get("evidence_count"),
                "duration_days": item.get("duration_days"),
                "active_month_count": item.get("active_month_count"),
                "avg_sentiment": item.get("avg_sentiment"),
                "avg_confidence": item.get("avg_confidence"),
                "top_tokens": item.get("top_tokens"),
                "top_assets": item.get("top_assets"),
                "latest_titles": item.get("latest_titles"),
                "source_counts": item.get("source_counts"),
            }
            for item in candidates
        ]
        payload = {
            "task": (
                "Review blind-discovered candidate market regimes. Decide whether each is a real long-running "
                "background regime worth promoting, a routine recurring data/news item, already covered by known "
                "standing regimes, mere noise, or a monitor-only theme. Do not provide trading advice. This is "
                "for scorer feature design only. Return exactly one review for every candidate_key provided."
            ),
            "known_regime_families": [str(rule["family"]) for rule in REGIME_RULES],
            "verdicts": ["promote_candidate", "monitor_only", "routine_data", "already_covered", "noise"],
            "policy_draft_archetypes": [
                "commodity_supply_shock",
                "technology_boom",
                "trade_policy_shock",
                "military_escalation",
                "financial_stress",
                "rate_shock",
                "macro_inflation",
                "policy_easing",
                "policy_tightening",
                "energy_transition",
                "other",
                "none",
            ],
            "candidate_guidance": {
                "promote_candidate": "Semantically coherent, persistent, market-relevant, not already covered, and likely to change thresholds for a defined asset set.",
                "monitor_only": "Possibly important but not yet specific enough for scorer threshold impact.",
                "routine_data": "Recurring scheduled/statistical items such as weekly inventories or routine macro prints, unless they form a persistent surprise regime.",
                "already_covered": "Mostly a variant of an existing standing regime family.",
                "noise": "Formatting artifacts, generic tokens, or incoherent grouping.",
            },
            "profile_draft_requirements": (
                "For promote_candidate and monitor_only reviews, fill profile_draft as a future threshold-engine draft: "
                "choose an archetype, promotion_readiness, beneficiary/victim/hedge assets, positive/negative/neutral "
                "news meanings, threshold-action hints for intensifying/easing/neutral updates, a resolution_prompt, "
                "resolution_confirmation_requirements, post_resolution_threshold_action, and a regime status hint. "
                "For routine_data, already_covered, or noise, keep promotion_readiness='insufficient' unless the draft "
                "would be useful for merging into an existing family. The draft is not trading advice and must remain "
                "conditional on later backtest validation."
            ),
            "resolution_prompt_requirements": (
                "Write resolution_prompt as a reusable instruction for classifying future headlines into: "
                "regime_intensifying, regime_easing, regime_resolved, false_resolution, or monitor_only. "
                "A true regime_resolved signal should require more than a single optimistic headline: specify the "
                "confirming policy/market/evidence conditions and how scorer thresholds should normalize afterward."
            ),
            "candidates": compact_candidates,
        }
        text = json.dumps(payload, ensure_ascii=False, default=str)[: self.max_input_chars]
        try:
            response = self.client.responses.parse(
                model=self.model,
                input=[
                    {
                        "role": "system",
                        "content": (
                            "You are a macro/geopolitical/thematic regime analyst for a market-event replay system. "
                            "You separate true long-running regimes from routine data, duplicated known regimes, and token noise. "
                            "Be skeptical and concise. Promote only candidates that would plausibly change scorer thresholds. "
                            "Also draft a generic regime policy profile usable by a future threshold engine. "
                            "For each promotable or monitor-only regime, include an explicit resolution prompt: how to "
                            "recognize easing/resolution headlines, what confirmation is needed, and how thresholds "
                            "should return toward normal after resolution. "
                            "You must return one structured review per input candidate_key; do not silently skip candidates."
                        ),
                    },
                    {"role": "user", "content": text},
                ],
                text_format=BlindRegimeAIReviewBatch,
                reasoning={"effort": self.reasoning_effort},
                store=False,
            )
            parsed = _extract_blind_ai_review(response)
        except (PermanentAnalysisError, TransientAnalysisError):
            raise
        except (ValidationError, Exception) as exc:
            mapped = _map_blind_openai_exception(exc)
            raise mapped from exc

        reviews_by_key = {review.candidate_key: review.model_dump(mode="json") for review in parsed.reviews}
        reviewed_candidates = []
        for item in result.get("candidates") or []:
            clone = dict(item)
            review = reviews_by_key.get(str(item.get("candidate_key") or ""))
            if review is not None:
                clone["ai_review"] = review
            reviewed_candidates.append(clone)
        output = dict(result)
        output["candidates"] = reviewed_candidates
        output["ai_review"] = {
            "review_version": parsed.review_version,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "reviewed_candidate_count": len(reviews_by_key),
            "summary": parsed.summary,
            "verdict_counts": dict(Counter(str(review.get("verdict") or "unknown") for review in reviews_by_key.values())),
        }
        return attach_blind_regime_promotion_waitlist(output)


def blind_regime_discovery_markdown(result: dict[str, Any]) -> str:
    lines = [
        "# Blind Standing Regime Discovery Report",
        "",
        f"- Generated at: `{datetime.now(UTC).isoformat()}`",
        f"- Version: `{result.get('version')}`",
        f"- Start: `{result.get('start')}`",
        f"- End: `{result.get('end')}`",
        f"- Signals scanned: `{result.get('signals_scanned')}`",
        f"- Known-rule signals skipped: `{result.get('known_signals_skipped')}`",
        f"- Raw candidates: `{result.get('raw_candidate_count')}`",
        f"- Filtered candidates: `{result.get('filtered_candidate_count')}`",
        f"- Returned candidates: `{result.get('returned_candidate_count')}`",
        "- Safety: report-only discovery; candidates are not injected into scorer/action gates until promoted deliberately.",
        "",
        "## Filters",
        "",
    ]
    for key, value in (result.get("filters") or {}).items():
        lines.append(f"- `{key}`: `{value}`")
    ai_review = result.get("ai_review") or {}
    if ai_review:
        lines.extend(
            [
                "",
                "## AI review summary",
                "",
                f"- Model: `{ai_review.get('model')}`",
                f"- Reasoning effort: `{ai_review.get('reasoning_effort')}`",
                f"- Reviewed candidates: `{ai_review.get('reviewed_candidate_count')}`",
                f"- Verdict counts: `{json.dumps(ai_review.get('verdict_counts') or {}, ensure_ascii=False, sort_keys=True)}`",
                f"- Summary: {str(ai_review.get('summary') or '').replace('|', '/')}",
            ]
        )
    waitlist = result.get("promotion_waitlist") if isinstance(result.get("promotion_waitlist"), dict) else {}
    if waitlist:
        lines.extend(
            [
                "",
                "## Promotion waitlist / resolution prompts",
                "",
                f"- Mode: `{waitlist.get('mode')}`",
                f"- Waitlist / watchlist / blocked: `{waitlist.get('waitlist_count')}` / `{waitlist.get('watchlist_count')}` / `{waitlist.get('blocked_count')}`",
                f"- Reason counts: `{json.dumps(waitlist.get('reason_counts') or {}, ensure_ascii=False, sort_keys=True)}`",
                "- Safety: report-only shadow queue; no scorer threshold, live routing, or broker order is changed here.",
                "",
                "| Status | Family | Bias | Conf | Readiness | Reasons | Resolution prompt | Confirmation requirements |",
                "| --- | --- | --- | ---: | --- | --- | --- | --- |",
            ]
        )
        waitlist_candidates = waitlist.get("candidates") or []
        if not waitlist_candidates:
            lines.append("| - | - | - | - | - | - | - | - |")
        for item in waitlist_candidates[:25]:
            family = str(item.get("proposed_family") or item.get("candidate_key") or "-").replace("|", "/")[:80]
            reasons = ", ".join(str(reason) for reason in (item.get("reasons") or []))[:140] or "-"
            prompt = str(item.get("resolution_prompt") or "").replace("|", "/")[:220] or "-"
            requirements = "; ".join(str(req) for req in (item.get("resolution_confirmation_requirements") or []))[:180] or "-"
            lines.append(
                f"| `{item.get('status')}` | {family} | `{item.get('directional_bias')}` | "
                f"{float(item.get('confidence') or 0.0):.2f} | `{item.get('promotion_readiness') or '-'}` | "
                f"{reasons} | {prompt} | {requirements} |"
            )
    lines.extend(
        [
            "",
            "## Candidate regimes",
            "",
            "| Score | AI verdict | Draft readiness | Archetype | AI family | Status | Region | Theme | Evidence | Months | Duration | Sent | Conf | Tokens | Assets | Threshold draft | Resolution action | AI rationale | Latest evidence |",
            "| ---: | --- | --- | --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- | --- | --- | --- | --- | --- |",
        ]
    )
    candidates = result.get("candidates") or []
    if not candidates:
        lines.append("| - | - | - | - | - | - | - | - | 0 | 0 | 0 | - | - | - | - | - | - | - | - |")
    for item in candidates:
        latest = str((item.get("latest_titles") or [""])[-1]).replace("|", "/")[:180]
        review = item.get("ai_review") or {}
        ai_verdict = str(review.get("verdict") or "-")
        ai_family = str(review.get("proposed_family") or review.get("regime_name") or "-").replace("|", "/")[:80]
        ai_rationale = str(review.get("rationale") or "").replace("|", "/")[:180]
        draft = review.get("profile_draft") or {}
        draft_readiness = str(draft.get("promotion_readiness") or "-").replace("|", "/")[:40]
        draft_archetype = str(draft.get("archetype") or "-").replace("|", "/")[:60]
        threshold_draft = (
            str(draft.get("intensifying_threshold_action") or draft.get("easing_threshold_action") or draft.get("neutral_threshold_action") or "")
            .replace("|", "/")[:180]
        )
        resolution_action = (
            str(draft.get("post_resolution_threshold_action") or draft.get("resolution_prompt") or "")
            .replace("|", "/")[:180]
        )
        lines.append(
            f"| {float(item.get('candidate_score') or 0.0):.2f} | {ai_verdict} | {draft_readiness} | {draft_archetype} | {ai_family} | {item.get('status')} "
            f"| {item.get('top_region')} | {item.get('top_theme')} "
            f"| {int(item.get('evidence_count') or 0)} | {int(item.get('active_month_count') or 0)} "
            f"| {int(item.get('duration_days') or 0)} | {float(item.get('avg_sentiment') or 0.0):+.2f} "
            f"| {float(item.get('avg_confidence') or 0.0):.2f} "
            f"| {', '.join(item.get('top_tokens') or [])[:120]} "
            f"| {', '.join(item.get('top_assets') or [])[:120]} | {threshold_draft} | {resolution_action} | {ai_rationale} | {latest} |"
        )
    lines.extend(
        [
            "",
            "## Promotion rule",
            "",
            "Promote only candidates whose latest evidence is semantically coherent, economically plausible, and not already covered by an existing standing regime. Promotion should add a named family, affected assets, direction bias, and threshold profile before any scorer impact.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_blind_regime_discovery_markdown(output_path: Path, result: dict[str, Any]) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(blind_regime_discovery_markdown(result), encoding="utf-8")
    return output_path


def active_standing_regimes_for_event(
    db: Any,
    *,
    symbol: str,
    cutoff_at: str | datetime,
    predicted_direction: str | None = None,
    limit: int = 8,
    point_in_time: bool = True,
) -> dict[str, Any]:
    cutoff = _date_to_dt(cutoff_at) if not isinstance(cutoff_at, datetime) else _date_to_dt(cutoff_at)
    symbol_upper = str(symbol or "").upper()
    target_assets = overlay_asset_family(symbol_upper)
    rows = (
        point_in_time_standing_regime_rows(db, cutoff_at=cutoff, limit=100000)
        if point_in_time
        else standing_regime_rows(db, cutoff_at=cutoff, limit=200)
    )
    matches = []
    for row in rows:
        assets = _load_assets(row.get("affected_assets_json"))
        direct = sorted(target_assets.intersection(assets))
        specific = sorted(set(direct) - BROAD_RISK_ASSETS)
        broad = bool(assets.intersection(BROAD_RISK_ASSETS))
        broad_index_target = symbol_upper in BROAD_RISK_ASSETS
        if not specific and not (broad_index_target and broad):
            continue
        status = str(row.get("status") or "")
        metrics = standing_regime_runtime_metrics(row, cutoff_at=cutoff)
        age_days = float(metrics["age_days"])
        base_weight = float(metrics["base_weight"])
        status_decay = float(metrics["status_decay"])
        effective_weight = float(metrics["effective_weight"])
        if specific:
            effective_weight *= 1.15
        elif broad_index_target and broad:
            effective_weight *= 0.85
        score = effective_weight
        if status == "active":
            score += 0.6
        elif status == "easing":
            score -= 0.2
        clone = dict(row)
        clone["asset_overlap"] = direct
        clone["specific_asset_overlap"] = specific
        clone["age_days"] = round(age_days, 2)
        clone["freshness_status"] = metrics["freshness_status"]
        clone["status_decay"] = status_decay
        clone["base_weight"] = round(base_weight, 3)
        clone["effective_weight"] = round(effective_weight, 3)
        clone["relation_to_prediction"] = _prediction_regime_relation(predicted_direction, str(row.get("directional_bias") or ""))
        clone["_match_score"] = round(score, 3)
        matches.append(clone)
    matches.sort(key=lambda item: float(item.get("_match_score") or 0.0), reverse=True)
    top = matches[: max(0, int(limit))]
    risk_weight = sum(
        float(row.get("effective_weight") or 0.0)
        for row in top
        if row.get("directional_bias") == "risk_off"
    )
    favorable_weight = sum(
        float(row.get("effective_weight") or 0.0)
        for row in top
        if row.get("directional_bias") == "risk_on"
    )
    if risk_weight > favorable_weight * 1.2:
        bias = "risk_off"
    elif favorable_weight > risk_weight * 1.2:
        bias = "risk_on"
    elif risk_weight or favorable_weight:
        bias = "mixed"
    else:
        bias = "neutral"
    return {
        "standing_regime_version": POINT_IN_TIME_REGIME_OVERLAY_VERSION,
        "source_regime_version": STANDING_REGIME_VERSION,
        "point_in_time_rebuild": bool(point_in_time),
        "symbol": str(symbol or "").upper(),
        "cutoff_at": cutoff.isoformat(),
        "bias": bias,
        "risk_weight": round(risk_weight, 3),
        "favorable_weight": round(favorable_weight, 3),
        "net_effective_weight": round(favorable_weight - risk_weight, 3),
        "conflict_count": sum(1 for row in top if row.get("relation_to_prediction") == "conflict"),
        "confirmation_count": sum(1 for row in top if row.get("relation_to_prediction") == "confirm"),
        "fresh_regime_count": sum(1 for row in top if row.get("freshness_status") in {"fresh_confirmation", "confirmed"}),
        "aging_regime_count": sum(1 for row in top if row.get("freshness_status") == "aging"),
        "matched_regime_count": len(top),
        "active_regimes": [_compact_regime_row(row) for row in top],
    }


def _compact_regime_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "regime_key": str(row.get("regime_key") or ""),
        "family": str(row.get("family") or ""),
        "regime_name": str(row.get("regime_name") or ""),
        "directional_bias": str(row.get("directional_bias") or ""),
        "status": str(row.get("status") or ""),
        "region": str(row.get("region") or ""),
        "theme": str(row.get("theme") or ""),
        "started_at": str(row.get("started_at") or ""),
        "last_seen_at": str(row.get("last_seen_at") or ""),
        "severity_score": float(row.get("severity_score") or 0.0),
        "confidence": float(row.get("confidence") or 0.0),
        "evidence_count": int(row.get("evidence_count") or 0),
        "asset_overlap": row.get("asset_overlap") or [],
        "specific_asset_overlap": row.get("specific_asset_overlap") or [],
        "age_days": row.get("age_days"),
        "freshness_status": row.get("freshness_status"),
        "status_decay": row.get("status_decay"),
        "base_weight": row.get("base_weight"),
        "effective_weight": row.get("effective_weight"),
        "relation_to_prediction": row.get("relation_to_prediction"),
        "latest_titles": json.loads(str(row.get("latest_titles_json") or "[]")),
        "threshold_profile": json.loads(str(row.get("threshold_profile_json") or "{}")),
    }


def standing_regime_rows(
    db: Any,
    *,
    cutoff_at: str | datetime | None = None,
    symbol: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if cutoff_at is not None:
        cutoff = _date_to_dt(cutoff_at) if not isinstance(cutoff_at, datetime) else _date_to_dt(cutoff_at)
        clauses.append("started_at<=?")
        params.append(cutoff.isoformat())
        clauses.append("(resolved_at='' OR resolved_at>=?)")
        params.append(cutoff.isoformat())
    if status:
        clauses.append("status=?")
        params.append(status.strip().lower())
    where = "" if not clauses else "WHERE " + " AND ".join(clauses)
    params.append(max(0, int(limit)))
    with db.connect() as conn:
        rows = [
            dict(row)
            for row in conn.execute(
                f"""SELECT * FROM standing_market_regimes
                    {where}
                    ORDER BY
                      CASE status WHEN 'active' THEN 1 WHEN 'easing' THEN 2 ELSE 3 END,
                      severity_score DESC,last_seen_at DESC,id DESC
                    LIMIT ?""",
                tuple(params),
            ).fetchall()
        ]
    if symbol:
        family = overlay_asset_family(symbol)
        filtered = []
        for row in rows:
            assets = _load_assets(row.get("affected_assets_json"))
            if family.intersection(assets) or assets.intersection(BROAD_RISK_ASSETS):
                filtered.append(row)
        return filtered
    return rows


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _parse_horizons(value: str | list[int] | tuple[int, ...]) -> list[int]:
    if isinstance(value, (list, tuple)):
        parts = value
    else:
        parts = str(value or "5,15,30,390").replace(";", ",").split(",")
    horizons: list[int] = []
    for part in parts:
        try:
            parsed = int(str(part).strip())
        except ValueError:
            continue
        if parsed > 0 and parsed not in horizons:
            horizons.append(parsed)
    return horizons or [5, 15, 30, 390]


def _expected_direction_for_regime_bias(bias: str) -> str:
    normalized = str(bias or "").strip().lower()
    if normalized == "risk_on":
        return "up"
    if normalized == "risk_off":
        return "down"
    return "neutral"


def _realized_direction_from_return(raw_return: float | None, *, flat_threshold_pct: float = 0.05) -> str:
    if raw_return is None:
        return "unknown"
    threshold = max(0.0, float(flat_threshold_pct)) / 100.0
    if raw_return > threshold:
        return "up"
    if raw_return < -threshold:
        return "down"
    return "flat"


def _direction_hit(expected: str, realized: str) -> int | None:
    if expected not in {"up", "down"} or realized == "unknown":
        return None
    if realized == "flat":
        return 0
    return 1 if expected == realized else 0


def ensure_standing_regime_outcome_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS standing_regime_outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_key TEXT NOT NULL,
            regime_key TEXT NOT NULL,
            family TEXT NOT NULL,
            symbol TEXT NOT NULL,
            horizon_minutes INTEGER NOT NULL,
            event_at TEXT NOT NULL,
            due_at TEXT NOT NULL,
            baseline_price REAL,
            baseline_source TEXT,
            outcome_at TEXT,
            outcome_price REAL,
            raw_return REAL,
            signed_return REAL,
            actual_direction TEXT NOT NULL DEFAULT 'unknown',
            direction_hit INTEGER,
            regime_bias TEXT NOT NULL DEFAULT 'unknown',
            expected_direction TEXT NOT NULL DEFAULT 'neutral',
            relation_to_prediction TEXT NOT NULL DEFAULT 'context_only',
            effective_weight REAL,
            freshness_status TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            blocker TEXT,
            payload_json TEXT NOT NULL DEFAULT '{}',
            outcome_version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(snapshot_key, regime_key, symbol, horizon_minutes)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_standing_regime_outcomes_status
        ON standing_regime_outcomes(status,due_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_standing_regime_outcomes_family
        ON standing_regime_outcomes(family,symbol,horizon_minutes,status)
        """
    )
    conn.commit()


def _regime_price_observations(conn: sqlite3.Connection, symbols: set[str]) -> dict[str, list[dict[str, Any]]]:
    if not symbols:
        return {}
    placeholders = ",".join("?" for _ in symbols)
    params = tuple(sorted(symbols))
    observations: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for table, time_col, price_col, source_col in (
        ("market_baselines", "captured_at", "selected_price", "selected_price_source"),
        ("market_reactions", "captured_at", "reaction_price", "price_source"),
    ):
        try:
            rows = conn.execute(
                f"""
                SELECT symbol, {time_col} AS at, {price_col} AS price, {source_col} AS source
                  FROM {table}
                 WHERE symbol IN ({placeholders}) AND {price_col} IS NOT NULL
                """,
                params,
            ).fetchall()
        except sqlite3.Error:
            continue
        for row in rows:
            at = _parse_dt(row["at"])
            price = _as_float(row["price"])
            if at is None or price is None:
                continue
            observations[str(row["symbol"]).upper()].append(
                {"at": at, "price": price, "source": row["source"] or table}
            )
    for symbol, rows in observations.items():
        observations[symbol] = sorted(rows, key=lambda item: item["at"])
    return observations


def _regime_select_baseline(rows: list[dict[str, Any]], event_at: datetime) -> dict[str, Any] | None:
    selected = None
    for row in rows:
        if row["at"] <= event_at:
            selected = row
        else:
            break
    return selected


def _regime_select_outcome(rows: list[dict[str, Any]], due_at: datetime, *, max_lag_minutes: float) -> dict[str, Any] | None:
    max_dt = due_at + timedelta(minutes=max(0.0, float(max_lag_minutes)))
    for row in rows:
        if due_at <= row["at"] <= max_dt:
            return row
    return None


def _standing_regime_snapshot_key(event_at: datetime, regime: dict[str, Any], symbol: str) -> str:
    raw = json.dumps(
        {
            "event_at": event_at.isoformat(),
            "regime_key": regime.get("regime_key"),
            "family": regime.get("family"),
            "symbol": symbol,
            "version": STANDING_REGIME_OUTCOMES_VERSION,
        },
        sort_keys=True,
        ensure_ascii=True,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _record_standing_regime_outcome_snapshot(
    conn: sqlite3.Connection,
    *,
    event_at: datetime,
    now_iso: str,
    symbols: tuple[str, ...],
    regime_rows_by_symbol: dict[str, list[dict[str, Any]]],
    observations: dict[str, list[dict[str, Any]]],
    horizons_minutes: list[int],
) -> list[dict[str, Any]]:
    recorded: list[dict[str, Any]] = []
    for symbol in symbols:
        baseline = _regime_select_baseline(observations.get(symbol) or [], event_at)
        for regime in regime_rows_by_symbol.get(symbol, []):
            regime_key = str(regime.get("regime_key") or regime.get("family") or "")
            family = str(regime.get("family") or "")
            if not regime_key or not family:
                continue
            expected = _expected_direction_for_regime_bias(str(regime.get("directional_bias") or ""))
            snapshot_key = _standing_regime_snapshot_key(event_at, regime, symbol)
            for horizon in horizons_minutes:
                due_at = event_at + timedelta(minutes=int(horizon))
                status = "pending" if baseline else "baseline_unavailable"
                blocker = None if baseline else "baseline_unavailable"
                payload = {
                    "regime_name": regime.get("regime_name"),
                    "status": regime.get("status"),
                    "region": regime.get("region"),
                    "theme": regime.get("theme"),
                    "asset_overlap": regime.get("asset_overlap") or [],
                    "specific_asset_overlap": regime.get("specific_asset_overlap") or [],
                    "latest_titles": regime.get("latest_titles") or [],
                    "threshold_profile": regime.get("threshold_profile") or {},
                    "recording_note": "report_only_learning_log_no_threshold_mutation",
                }
                conn.execute(
                    """
                    INSERT OR IGNORE INTO standing_regime_outcomes (
                        snapshot_key,regime_key,family,symbol,horizon_minutes,event_at,due_at,
                        baseline_price,baseline_source,regime_bias,expected_direction,relation_to_prediction,
                        effective_weight,freshness_status,status,blocker,payload_json,outcome_version,created_at,updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        snapshot_key,
                        regime_key,
                        family,
                        symbol,
                        int(horizon),
                        event_at.isoformat(),
                        due_at.isoformat(),
                        baseline.get("price") if baseline else None,
                        baseline.get("source") if baseline else None,
                        regime.get("directional_bias") or "unknown",
                        expected,
                        regime.get("relation_to_prediction") or "context_only",
                        _as_float(regime.get("effective_weight")),
                        regime.get("freshness_status") or "",
                        status,
                        blocker,
                        json.dumps(payload, ensure_ascii=False, sort_keys=True),
                        STANDING_REGIME_OUTCOMES_VERSION,
                        now_iso,
                        now_iso,
                    ),
                )
                recorded.append(
                    {
                        "symbol": symbol,
                        "family": family,
                        "horizon_minutes": int(horizon),
                        "status": status,
                    }
                )
    conn.commit()
    return recorded


def _evaluate_standing_regime_outcomes(
    conn: sqlite3.Connection,
    *,
    now_dt: datetime,
    now_iso: str,
    max_outcome_lag_minutes: float,
    evaluation_limit: int,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT * FROM standing_regime_outcomes
         WHERE status IN ('pending','outcome_unavailable')
         ORDER BY due_at ASC
         LIMIT ?
        """,
        (max(1, int(evaluation_limit)),),
    ).fetchall()
    symbols = {str(row["symbol"]).upper() for row in rows}
    observations = _regime_price_observations(conn, symbols)
    updates: list[dict[str, Any]] = []
    for row in rows:
        due_at = _parse_dt(row["due_at"])
        baseline_price = _as_float(row["baseline_price"])
        symbol = str(row["symbol"]).upper()
        if due_at is None or baseline_price is None:
            continue
        if due_at > now_dt:
            continue
        outcome = _regime_select_outcome(observations.get(symbol) or [], due_at, max_lag_minutes=max_outcome_lag_minutes)
        if outcome is None:
            max_dt = due_at + timedelta(minutes=max(0.0, float(max_outcome_lag_minutes)))
            if now_dt > max_dt:
                conn.execute(
                    "UPDATE standing_regime_outcomes SET status=?, blocker=?, updated_at=? WHERE id=?",
                    ("outcome_unavailable", "outcome_unavailable", now_iso, row["id"]),
                )
            continue
        outcome_price = _as_float(outcome.get("price"))
        if outcome_price is None:
            continue
        raw_return = outcome_price / baseline_price - 1.0
        expected = str(row["expected_direction"] or "neutral")
        if expected == "down":
            signed_return = -raw_return
        elif expected == "up":
            signed_return = raw_return
        else:
            signed_return = raw_return
        actual_direction = _realized_direction_from_return(raw_return)
        hit = _direction_hit(expected, actual_direction)
        conn.execute(
            """
            UPDATE standing_regime_outcomes
               SET outcome_at=?,outcome_price=?,raw_return=?,signed_return=?,
                   actual_direction=?,direction_hit=?,status='evaluated',blocker=NULL,updated_at=?
             WHERE id=?
            """,
            (
                outcome["at"].isoformat(),
                outcome_price,
                raw_return,
                signed_return,
                actual_direction,
                hit,
                now_iso,
                row["id"],
            ),
        )
        updates.append(
            {
                "id": row["id"],
                "symbol": symbol,
                "family": row["family"],
                "horizon_minutes": int(row["horizon_minutes"]),
                "raw_return": round(raw_return, 6),
                "signed_return": round(signed_return, 6),
                "actual_direction": actual_direction,
                "direction_hit": hit,
            }
        )
    conn.commit()
    return updates


def _standing_regime_outcome_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    status_counts = dict(
        conn.execute(
            "SELECT status, COUNT(*) FROM standing_regime_outcomes GROUP BY status"
        ).fetchall()
    )
    family_rows = [
        dict(row)
        for row in conn.execute(
            """
            SELECT family,symbol,horizon_minutes,COUNT(*) AS rows,
                   SUM(CASE WHEN status='evaluated' THEN 1 ELSE 0 END) AS evaluated,
                   AVG(CASE WHEN status='evaluated' THEN signed_return END) AS avg_signed_return,
                   AVG(CASE WHEN status='evaluated' AND direction_hit IS NOT NULL THEN direction_hit END) AS direction_hit_rate
              FROM standing_regime_outcomes
             GROUP BY family,symbol,horizon_minutes
             ORDER BY evaluated DESC, rows DESC, family ASC, symbol ASC
             LIMIT 50
            """
        ).fetchall()
    ]
    latest_rows = [
        dict(row)
        for row in conn.execute(
            """
            SELECT family,symbol,horizon_minutes,event_at,due_at,status,expected_direction,
                   actual_direction,raw_return,signed_return,direction_hit,freshness_status,effective_weight
              FROM standing_regime_outcomes
             ORDER BY updated_at DESC,id DESC
             LIMIT 25
            """
        ).fetchall()
    ]
    return {
        "status_counts": status_counts,
        "family_symbol_horizon_summary": family_rows,
        "latest_rows": latest_rows,
    }


def build_standing_regime_outcome_report(
    db: Any,
    *,
    symbols: tuple[str, ...] | list[str] | str = DEFAULT_REGIME_OUTCOME_SYMBOLS,
    horizons_minutes: str | list[int] | tuple[int, ...] = "5,15,30,390",
    record_current_snapshot: bool = True,
    evaluate_pending: bool = True,
    max_outcome_lag_minutes: float = 1440.0,
    evaluation_limit: int = 2000,
    now: str | datetime | None = None,
) -> dict[str, Any]:
    now_dt = _date_to_dt(now) if now is not None else datetime.now(UTC)
    now_iso = now_dt.isoformat()
    if isinstance(symbols, str):
        parsed_symbols = tuple(item.strip().upper() for item in symbols.split(",") if item.strip())
    else:
        parsed_symbols = tuple(str(item).strip().upper() for item in symbols if str(item).strip())
    parsed_symbols = parsed_symbols or DEFAULT_REGIME_OUTCOME_SYMBOLS
    horizons = _parse_horizons(horizons_minutes)
    regime_rows_by_symbol: dict[str, list[dict[str, Any]]] = {}
    if record_current_snapshot:
        for symbol in parsed_symbols:
            overlay = active_standing_regimes_for_event(
                db,
                symbol=symbol,
                cutoff_at=now_dt,
                limit=10,
                point_in_time=True,
            )
            regime_rows_by_symbol[symbol] = [
                row
                for row in (overlay.get("active_regimes") or [])
                if str(row.get("status") or "") in {"active", "easing"}
            ]
    with db.connect() as conn:
        ensure_standing_regime_outcome_schema(conn)
        observations = _regime_price_observations(conn, set(parsed_symbols))
        recorded = (
            _record_standing_regime_outcome_snapshot(
                conn,
                event_at=now_dt,
                now_iso=now_iso,
                symbols=parsed_symbols,
                regime_rows_by_symbol=regime_rows_by_symbol,
                observations=observations,
                horizons_minutes=horizons,
            )
            if record_current_snapshot
            else []
        )
        evaluated = (
            _evaluate_standing_regime_outcomes(
                conn,
                now_dt=now_dt,
                now_iso=now_iso,
                max_outcome_lag_minutes=max_outcome_lag_minutes,
                evaluation_limit=evaluation_limit,
            )
            if evaluate_pending
            else []
        )
        summary = _standing_regime_outcome_summary(conn)
    return {
        "standing_regime_outcomes_version": STANDING_REGIME_OUTCOMES_VERSION,
        "generated_at": now_iso,
        "mode": "report_only_shadow_learning_log",
        "symbols": list(parsed_symbols),
        "horizons_minutes": horizons,
        "record_current_snapshot": bool(record_current_snapshot),
        "evaluate_pending": bool(evaluate_pending),
        "recorded_count": len(recorded),
        "evaluated_count": len(evaluated),
        "recorded": recorded[:50],
        "evaluated": evaluated[:50],
        **summary,
        "safety": {
            "report_only": True,
            "scorer_threshold_mutated": False,
            "live_threshold_promoted": False,
            "paper_orders_created": False,
            "broker_orders_submitted": False,
            "transmit_enabled": False,
        },
    }


def standing_regime_outcome_markdown(result: dict[str, Any]) -> str:
    lines = [
        "# Standing Regime Outcome Learning Report",
        "",
        f"- Generated at: `{result.get('generated_at')}`",
        f"- Version: `{result.get('standing_regime_outcomes_version')}`",
        f"- Mode: `{result.get('mode')}`",
        f"- Symbols: `{', '.join(result.get('symbols') or [])}`",
        f"- Horizons minutes: `{result.get('horizons_minutes')}`",
        f"- Recorded / evaluated this run: `{result.get('recorded_count')}` / `{result.get('evaluated_count')}`",
        f"- Status counts: `{result.get('status_counts') or {}}`",
        "- Safety: report-only learning log; no scorer threshold, paper order, or broker order is changed.",
        "",
        "## Family / symbol / horizon summary",
        "",
        "| Family | Symbol | Horizon | Rows | Evaluated | Avg signed return | Direction hit rate |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    rows = result.get("family_symbol_horizon_summary") or []
    if not rows:
        lines.append("| - | - | - | 0 | 0 | - | - |")
    for row in rows[:50]:
        avg_signed = row.get("avg_signed_return")
        hit_rate = row.get("direction_hit_rate")
        lines.append(
            f"| `{row.get('family')}` | `{row.get('symbol')}` | {row.get('horizon_minutes')} | "
            f"{row.get('rows')} | {row.get('evaluated')} | "
            f"{float(avg_signed):+.5f} | {float(hit_rate):.2f} |"
            if avg_signed is not None and hit_rate is not None
            else f"| `{row.get('family')}` | `{row.get('symbol')}` | {row.get('horizon_minutes')} | {row.get('rows')} | {row.get('evaluated')} | - | - |"
        )
    lines.extend(
        [
            "",
            "## Latest outcome rows",
            "",
            "| Status | Family | Symbol | Horizon | Expected | Actual | Raw return | Signed return | Hit | Freshness | Weight |",
            "| --- | --- | --- | ---: | --- | --- | ---: | ---: | ---: | --- | ---: |",
        ]
    )
    latest = result.get("latest_rows") or []
    if not latest:
        lines.append("| - | - | - | - | - | - | - | - | - | - | - |")
    for row in latest[:25]:
        raw_return = row.get("raw_return")
        signed_return = row.get("signed_return")
        weight = row.get("effective_weight")
        raw_text = f"{float(raw_return):+.5f}" if raw_return is not None else "-"
        signed_text = f"{float(signed_return):+.5f}" if signed_return is not None else "-"
        hit_text = str(row.get("direction_hit")) if row.get("direction_hit") is not None else "-"
        weight_text = f"{float(weight):.2f}" if weight is not None else "-"
        lines.append(
            f"| `{row.get('status')}` | `{row.get('family')}` | `{row.get('symbol')}` | {row.get('horizon_minutes')} | "
            f"`{row.get('expected_direction')}` | `{row.get('actual_direction')}` | {raw_text} | {signed_text} | "
            f"{hit_text} | `{row.get('freshness_status')}` | {weight_text} |"
        )
    return "\n".join(lines) + "\n"


def write_standing_regime_outcome_json(path: Path, result: dict[str, Any]) -> Path:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return resolved


def write_standing_regime_outcome_markdown(path: Path, result: dict[str, Any]) -> Path:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(standing_regime_outcome_markdown(result), encoding="utf-8")
    return resolved


def standing_regime_markdown(rows: list[dict[str, Any]], *, filters: dict[str, Any] | None = None) -> str:
    filters = filters or {}
    lines = [
        "# Standing Market Regime Report",
        "",
        f"- Generated at: `{datetime.now(UTC).isoformat()}`",
        f"- Version: `{STANDING_REGIME_VERSION}`",
        f"- Rows: `{len(rows)}`",
        "- Purpose: persistent background context for non-one-off favorable/risk regimes such as AI boom, tariff war, and geopolitical conflicts.",
        "- Safety: read-only report; no OpenAI call, IBKR account query, portfolio access, or order action.",
        "",
        "## Filters",
        "",
    ]
    if filters:
        for key, value in filters.items():
            lines.append(f"- `{key}`: `{value if value is not None else '-'}`")
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "## Regimes",
            "",
            "| Status | Bias | Family | Region | Theme | Started | Last seen | Severity | Conf | Evidence | Assets | Latest evidence |",
            "| --- | --- | --- | --- | --- | --- | --- | ---: | ---: | ---: | --- | --- |",
        ]
    )
    if not rows:
        lines.append("| - | - | - | - | - | - | - | - | - | 0 | - | - |")
    for row in rows:
        assets = ", ".join(sorted(_load_assets(row.get("affected_assets_json")))[:10])
        titles = json.loads(str(row.get("latest_titles_json") or "[]"))
        latest = str(titles[-1] if titles else "").replace("|", "/")[:160]
        lines.append(
            f"| {row.get('status') or ''} | {row.get('directional_bias') or ''} | {row.get('family') or ''} "
            f"| {row.get('region') or ''} | {row.get('theme') or ''} | {str(row.get('started_at') or '')[:10]} "
            f"| {str(row.get('last_seen_at') or '')[:10]} | {float(row.get('severity_score') or 0.0):.2f} "
            f"| {float(row.get('confidence') or 0.0):.2f} | {int(row.get('evidence_count') or 0)} "
            f"| {assets} | {latest} |"
        )
    lines.extend(
        [
            "",
            "## How to use",
            "",
            "- `risk_off` active regimes should temporarily tighten up-call thresholds for affected assets unless company/event evidence is strong.",
            "- `risk_on` active regimes, such as AI infrastructure boom, can preserve or slightly loosen thresholds for affected beneficiaries when market data confirms.",
            "- `mixed` exposure means the scorer should explicitly compare favorable and defensive background forces rather than treating the event in isolation.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_standing_regime_markdown(output_path: Path, rows: list[dict[str, Any]], *, filters: dict[str, Any] | None = None) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(standing_regime_markdown(rows, filters=filters), encoding="utf-8")
    return output_path
