from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any, Iterable

from .analysis_database import AnalysisDatabase
from .news_lineage_v2 import (
    MARKETAUX_HISTORICAL_VISIBILITY_LATENCY_SECONDS,
    NEWS_ASSET_MAPPER_VERSION,
    assign_article_to_event_cluster,
    ensure_news_lineage_v2_schema,
    record_provider_entities,
    store_article_asset_mapping,
    sync_legacy_news_article_to_raw,
    upsert_news_visibility_policy,
    upsert_raw_news_article,
    upsert_raw_news_processing_status,
)
from .news_asset_mapper import infer_news_assets

EXTERNAL_MARKET_SIGNAL_VERSION = "external-market-signals-v1"
BREAKING_NEWS_SIGNAL_VERSION = "emergency-breaking-news-signal-v1"
BREAKING_NEWS_SOURCE = "emergency_breaking_news"

GDELT_DOC_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
MARKETAUX_URL = "https://api.marketaux.com/v1/news/all"
ALPHA_VANTAGE_URL = "https://www.alphavantage.co/query"

GEOPOLITICAL_QUERY = (
    '(geopolitical OR war OR missile OR airstrike OR sanctions OR "export controls" OR '
    'tariff OR "trade war" OR Taiwan OR China OR "Middle East" OR "Red Sea" OR Hormuz OR '
    'Ukraine OR Russia OR Korea OR cyberattack OR "oil supply")'
)

GEOPOLITICAL_BACKFILL_QUERIES: tuple[tuple[str, str], ...] = (
    (
        "middle_east_energy",
        '(Iran OR Israel OR "Middle East" OR Hormuz OR "Red Sea" OR Houthi OR Gaza OR Lebanon) '
        '(oil OR crude OR energy OR tanker OR missile OR airstrike OR war)',
    ),
    (
        "taiwan_china_chips",
        '(Taiwan OR China OR Beijing OR "South China Sea" OR "export controls" OR sanctions) '
        '(semiconductor OR chips OR TSMC OR Nvidia OR "AI chips")',
    ),
    (
        "korea_peninsula",
        '("North Korea" OR "South Korea" OR "Korea Peninsula" OR Pyongyang OR Seoul) '
        '(missile OR military OR sanctions OR semiconductor OR volatility)',
    ),
    (
        "russia_ukraine",
        '(Russia OR Ukraine OR Kyiv OR Moscow OR "Black Sea") '
        '(war OR sanctions OR oil OR gas OR wheat OR grain OR defense)',
    ),
    (
        "russia_ukraine_nato",
        '(Russia OR Ukraine OR Kyiv OR Moscow OR NATO OR Poland OR "Black Sea") '
        '(drone OR missile OR airspace OR escalation OR defense OR weapons)',
    ),
    (
        "middle_east_shipping",
        '(Iran OR Israel OR "Red Sea" OR Hormuz OR Houthi OR Yemen OR Gaza) '
        '(shipping OR tanker OR chokepoint OR missile OR drone OR oil)',
    ),
    (
        "taiwan_blockade_chips",
        '(Taiwan OR China OR Beijing OR "South China Sea") '
        '(blockade OR military drill OR invasion OR semiconductor OR chips OR "critical minerals")',
    ),
    (
        "korea_missile",
        '("North Korea" OR Pyongyang OR "Korea Peninsula" OR DMZ) '
        '(missile OR launch OR nuclear OR military OR sanctions)',
    ),
    (
        "global_cyber",
        '(cyberattack OR "cyber attack" OR ransomware OR hack OR malware) '
        '(market OR exchange OR bank OR chip OR energy OR infrastructure)',
    ),
    (
        "trade_sanctions",
        '("trade war" OR tariff OR tariffs OR sanctions OR "export controls" OR blacklist) '
        '(China OR US OR Taiwan OR Russia OR Iran OR chips OR energy)',
    ),
)

MARKETAUX_BACKFILL_QUERIES: tuple[tuple[str, str], ...] = (
    ("middle_east_energy", "Iran Israel Middle East oil war"),
    ("taiwan_china_chips", "Taiwan China semiconductor chips export controls"),
    ("korea_peninsula", "North Korea South Korea missile sanctions"),
    ("russia_ukraine", "Russia Ukraine war sanctions oil wheat"),
    ("russia_ukraine_nato", "Russia Ukraine NATO drone missile defense escalation"),
    ("middle_east_shipping", "Iran Israel Red Sea Hormuz Houthi shipping oil"),
    ("taiwan_blockade_chips", "Taiwan China blockade military drill semiconductor critical minerals"),
    ("korea_missile", "North Korea missile nuclear sanctions"),
    ("global_cyber", "cyberattack ransomware hack energy bank market infrastructure"),
    ("trade_sanctions", "tariff sanctions export controls China"),
)

MARKETAUX_DEFAULT_ENTITY_TYPES = "equity,index,etf"
MARKETAUX_DEFAULT_COUNTRIES = "us"
MARKETAUX_BACKFILL_MODES = {"query", "broad", "hybrid"}

REGION_THEME_ASSET_MAP: dict[str, dict[str, tuple[str, ...]]] = {
    "middle_east": {
        "energy_supply": ("USO", "XLE", "XOM", "CVX", "GLD", "VIX"),
        "military_escalation": ("USO", "XLE", "GLD", "VIX", "SPY"),
        "shipping_chokepoint": ("USO", "XLE", "GLD", "VIX", "SPY"),
        "broad_geopolitical": ("USO", "XLE", "GLD", "VIX", "SPY"),
    },
    "taiwan_china": {
        "semiconductor_supply_chain": ("TSM", "SMH", "SOXX", "NVDA", "QQQ"),
        "export_controls": ("TSM", "SMH", "SOXX", "NVDA", "QQQ"),
        "trade_conflict": ("TSM", "SMH", "SOXX", "NVDA", "QQQ", "SPY"),
        "broad_geopolitical": ("TSM", "SMH", "SOXX", "NVDA", "QQQ"),
    },
    "korea_peninsula": {
        "military_escalation": ("EWY", "KRW", "SOXX", "SPY"),
        "semiconductor_supply_chain": ("EWY", "SOXX", "SMH", "QQQ"),
        "broad_geopolitical": ("EWY", "KRW", "SOXX", "SPY"),
    },
    "russia_ukraine": {
        "energy_supply": ("WEAT", "USO", "XLE", "ITA", "SPY"),
        "food_supply": ("WEAT", "DBA", "SPY"),
        "defense": ("ITA", "XAR", "SPY"),
        "military_escalation": ("ITA", "XAR", "WEAT", "USO", "XLE", "SPY", "VIX"),
        "trade_conflict": ("WEAT", "USO", "XLE", "UUP", "GLD", "SPY"),
        "broad_geopolitical": ("WEAT", "USO", "XLE", "ITA", "XAR", "SPY", "VIX"),
    },
    "global": {
        "market_news": ("SPY", "QQQ", "TLT", "GLD", "VIX"),
        "semiconductor_supply_chain": ("SOXX", "SMH", "NVDA", "QQQ", "SPY"),
        "energy_supply": ("USO", "XLE", "XOM", "CVX", "VIX"),
        "shipping_chokepoint": ("USO", "XLE", "GLD", "VIX", "SPY"),
        "cybersecurity": ("CIBR", "HACK", "QQQ", "SPY", "VIX"),
        "trade_conflict": ("SPY", "QQQ", "UUP", "GLD"),
        "broad_geopolitical": ("SPY", "QQQ", "TLT", "GLD", "VIX"),
    },
}

REGION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("middle_east", re.compile(r"\b(?:middle east|israel|iran|red sea|hormuz|gaza|lebanon|houthi|saudi|iraq|syria|yemen)\b", re.I)),
    ("taiwan_china", re.compile(r"\b(?:taiwan|china|beijing|xi jinping|south china sea|export controls?|advanced chips?|semiconductor)\b", re.I)),
    ("korea_peninsula", re.compile(r"\b(?:north korea|south korea|korea peninsula|pyongyang|seoul|dmz)\b", re.I)),
    ("russia_ukraine", re.compile(r"\b(?:russia|ukraine|kyiv|moscow|crimea|black sea|wheat|nato|poland)\b", re.I)),
)

THEME_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("semiconductor_supply_chain", re.compile(r"\b(?:semiconductor|chips?|advanced chips?|hbm|ai chips?|tsmc|nvidia|export controls?)\b", re.I)),
    ("energy_supply", re.compile(r"\b(?:oil|crude|brent|wti|energy supply|opec|gas pipeline|hormuz|red sea|tanker)\b", re.I)),
    ("shipping_chokepoint", re.compile(r"\b(?:shipping|red sea|hormuz|tanker|container ship|chokepoint|suez)\b", re.I)),
    ("trade_conflict", re.compile(r"\b(?:tariffs?|trade war|sanctions?|export controls?|blacklist)\b", re.I)),
    ("military_escalation", re.compile(r"\b(?:war|missile|airstrike|attack|invasion|military escalation|troops?|ceasefire|drone|airspace|blockade|military drill)\b", re.I)),
    ("food_supply", re.compile(r"\b(?:wheat|grain|food supply|black sea)\b", re.I)),
    ("defense", re.compile(r"\b(?:defense|weapons?|arms|military spending|nato)\b", re.I)),
    ("cybersecurity", re.compile(r"\b(?:cyberattack|cyber attack|hack|ransomware|malware)\b", re.I)),
)

GEOPOLITICAL_CONTEXT_PATTERN = re.compile(
    r"\b(?:geopolitical|war|missile|airstrike|sanctions?|tariffs?|trade war|"
    r"export controls?|military|invasion|ceasefire|nato|red sea|hormuz|"
    r"drone|airspace|blockade|cyberattack|ransomware|critical minerals|"
    r"taiwan|china|russia|ukraine|iran|israel|north korea)\b",
    re.I,
)

BENZINGA_SIGNAL_PATTERN = re.compile(
    r"\b(?:geopolitical|war|missile|airstrike|sanctions?|tariffs?|trade war|"
    r"export controls?|military|invasion|ceasefire|nato|red sea|hormuz|"
    r"taiwan|china|russia|ukraine|iran|israel|north korea|south korea|"
    r"middle east|oil|crude|opec|shipping|critical minerals|supply chain)\b",
    re.I,
)
DOW_JONES_SIGNAL_PATTERN = re.compile(
    r"\b(?:geopolitical|war|missile|airstrike|sanctions?|tariffs?|trade war|"
    r"export controls?|military|invasion|ceasefire|nato|red sea|hormuz|"
    r"taiwan|china|russia|ukraine|iran|israel|north korea|south korea|"
    r"middle east|oil|crude|opec|shipping|critical minerals|supply chain|"
    r"cpi|pce|payrolls?|jobs?\s+report|unemployment|claims|fed|fomc|treasury|"
    r"yield|inflation|vix|nasdaq|s&p\s*500|russell|semiconductor|chips?|ai|"
    r"earnings|guidance|outlook|upgrade|downgrade|price target|merger|"
    r"acquisition|buyback|shares?\s+(?:rise|fall|drop|slip|gain|shed)|"
    r"etf\s+(?:rises?|declines?|falls?))\b",
    re.I,
)
EXTERNAL_SIGNAL_PRIORITY_PATTERN = re.compile(
    r"\b(?:cpi|pce|payrolls?|jobs?\s+report|unemployment|claims|fed|fomc|treasury|"
    r"yield|inflation|tariffs?|sanctions?|export\s+controls?|oil|crude|brent|wti|"
    r"vix|nasdaq|s&p\s*500|dow\s+jones|russell|semiconductor|chips?|ai\s+chips?|"
    r"taiwan|china|russia|ukraine|iran|israel|north\s+korea|south\s+korea|red\s+sea|hormuz|"
    r"war|missile|airstrike|ceasefire|cyberattack|ransomware|supply\s+chain|"
    r"earnings|guidance|outlook|upgrade|downgrade|price\s+target|merger|"
    r"acquisition|buyback|offering|halt|fda|antitrust|sec\s+probe|"
    r"shares?\s+(?:rise|fall|drop|slip|gain|jump|sink|tumble|rally)|"
    r"futures?\s+(?:rise|fall|drop|jump|sink|tumble|rally)|"
    r"etf\s+(?:rises?|declines?|falls?|jumps?))\b",
    re.I,
)

BENZINGA_PREFIX_PATTERN = re.compile(r"^\{A:[^}]*\}")
BENZINGA_SENTIMENT_PATTERN = re.compile(r":K:([-+]?\d+(?:\.\d+)?|n/a)", re.I)
BENZINGA_CONFIDENCE_PATTERN = re.compile(r":C:([-+]?\d+(?:\.\d+)?)", re.I)

MARKETAUX_LOW_SIGNAL_PATTERN = re.compile(
    r"\b(?:best\s+\w*\s*stocks?\s+to\s+buy|is\s+.+?\s+a\s+good\s+stock\s+to\s+buy|"
    r"stock\s+portfolio|according\s+to\s+analysts|hedge\s+fund|dividend\s+stocks?|"
    r"penny\s+stocks?|zacks|motley\s+fool|insider\s+monkey|price\s+target\s+roundup|"
    r"wall\s+street\s+analysts?|top\s+\d+\s+stocks?)\b",
    re.I,
)
MARKETAUX_US_MARKET_RELEVANT_PATTERN = re.compile(
    r"\b(?:cpi|pce|payrolls?|jobs?\s+report|unemployment|claims|fed|fomc|treasury|"
    r"yield|inflation|tariffs?|sanctions?|export\s+controls?|oil|crude|brent|wti|"
    r"vix|nasdaq|s&p\s*500|dow\s+jones|russell|semiconductor|chips?|ai\s+chips?|"
    r"taiwan|china|russia|ukraine|iran|israel|north\s+korea|red\s+sea|hormuz|"
    r"war|missile|airstrike|ceasefire|cyberattack|ransomware|supply\s+chain)\b",
    re.I,
)
MARKETAUX_HARD_EVENT_PATTERN = re.compile(
    r"\b(?:war|missile|airstrike|sanctions?|tariffs?|trade war|export\s+controls?|"
    r"invasion|ceasefire|nato|red\s+sea|hormuz|drone|airspace|blockade|cyberattack|"
    r"ransomware|critical\s+minerals|taiwan|china|russia|ukraine|iran|israel|"
    r"north\s+korea|middle\s+east|oil|crude|brent|wti|shipping|"
    r"semiconductor\s+restriction|chip\s+restriction)\b",
    re.I,
)


def _dynamic_external_signal_score(row: dict[str, Any], *, source: str, priority_symbols: set[str]) -> float:
    headline = str(row.get("headline") or "")
    body = str(row.get("body") or "")
    text = f"{headline} {body}"
    symbol = str(row.get("primary_symbol") or "").upper()
    score = 0.0
    if symbol in priority_symbols:
        score += 35.0
    if EXTERNAL_SIGNAL_PRIORITY_PATTERN.search(text):
        score += 80.0
    if source == "benzinga" and BENZINGA_SIGNAL_PATTERN.search(text):
        score += 35.0
    if source == "dow_jones" and DOW_JONES_SIGNAL_PATTERN.search(text):
        score += 35.0
    if re.search(r"\b(?:earnings|guidance|outlook|upgrade|downgrade|price target|merger|acquisition|buyback)\b", text, re.I):
        score += 25.0
    if re.search(r"\b(?:futures|s&p\s*500|nasdaq|dow|vix|treasury|yield|fed|inflation|oil|crude)\b", text, re.I):
        score += 15.0
    if len(headline) >= 20:
        score += 3.0
    if body:
        score += 2.0
    return score


def _dynamic_news_row_limit(
    rows: list[dict[str, Any]],
    *,
    source: str,
    total_limit: int,
    per_symbol_limit: int,
    priority_per_symbol_limit: int,
    priority_symbols: tuple[str, ...] | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Select a bounded, high-signal, symbol-balanced subset of stored news rows.

    Raw/news rows remain preserved.  This limiter only controls expensive derived
    signal conversion so a broad canonical backfill does not spend hours turning
    every SPY/NVDA route headline into cluster/ranking inputs.
    """

    total_limit = int(total_limit or 0)
    if total_limit <= 0 or len(rows) <= total_limit:
        return rows, {
            "enabled": False,
            "input_rows": len(rows),
            "selected_rows": len(rows),
        }

    normal_cap = max(1, int(per_symbol_limit or total_limit))
    priority_cap = max(normal_cap, int(priority_per_symbol_limit or normal_cap))
    priority_set = {str(symbol).strip().upper() for symbol in (priority_symbols or ()) if str(symbol).strip()}

    grouped: dict[str, list[tuple[float, str, int, dict[str, Any]]]] = {}
    for idx, row in enumerate(rows):
        symbol = str(row.get("primary_symbol") or "UNKNOWN").upper()
        published = str(row.get("provider_published_at") or row.get("published_at") or row.get("received_at") or "")
        score = _dynamic_external_signal_score(row, source=source, priority_symbols=priority_set)
        grouped.setdefault(symbol, []).append((score, published, idx, row))

    capped: list[tuple[float, str, int, dict[str, Any]]] = []
    per_symbol_selected: dict[str, int] = {}
    for symbol, items in grouped.items():
        cap = priority_cap if symbol in priority_set else normal_cap
        selected_items = sorted(items, key=lambda item: (-item[0], item[1], item[2]))[:cap]
        per_symbol_selected[symbol] = len(selected_items)
        capped.extend(selected_items)

    selected = sorted(capped, key=lambda item: (-item[0], item[1], item[2]))[:total_limit]
    selected_rows = [item[3] for item in selected]
    selected_by_symbol: dict[str, int] = {}
    for row in selected_rows:
        symbol = str(row.get("primary_symbol") or "UNKNOWN").upper()
        selected_by_symbol[symbol] = selected_by_symbol.get(symbol, 0) + 1

    return selected_rows, {
        "enabled": True,
        "source": source,
        "input_rows": len(rows),
        "selected_rows": len(selected_rows),
        "total_limit": total_limit,
        "per_symbol_limit": normal_cap,
        "priority_per_symbol_limit": priority_cap,
        "priority_symbols": sorted(priority_set),
        "selected_by_symbol": dict(sorted(selected_by_symbol.items(), key=lambda item: (-item[1], item[0]))),
        "per_symbol_pre_total_cap": dict(sorted(per_symbol_selected.items(), key=lambda item: (-item[1], item[0]))),
    }

_BREAKING_CRITICAL_RE = re.compile(
    r"\b(?:breaking|urgent|flash|missile|airstrike|invasion|nuclear|explosion|"
    r"attack|assassination|emergency rate|emergency fed|bank failure|trading halt|"
    r"strait of hormuz|red sea|taiwan blockade|martial law)\b",
    re.I,
)
_BREAKING_HIGH_RE = re.compile(
    r"\b(?:tariff|sanction|export control|guidance cut|cuts guidance|bankruptcy|"
    r"default|downgrade|sec probe|doj probe|antitrust|fda approval|fda rejects|"
    r"merger|acquisition|takeover|ceo resigns|cfo resigns|cyberattack|ransomware|"
    r"cpi|pce|payrolls|jobs report|fed hikes|fed cuts|rate decision)\b",
    re.I,
)
_BREAKING_POSITIVE_RE = re.compile(
    r"\b(?:approval|approves|beats|raises guidance|raises outlook|acquisition|takeover|"
    r"contract win|record revenue|cooler than expected|fed cuts|tariff relief|ceasefire)\b",
    re.I,
)
_BREAKING_NEGATIVE_RE = re.compile(
    r"\b(?:missile|airstrike|attack|invasion|war|nuclear|tariff|sanction|export control|"
    r"guidance cut|cuts guidance|bankruptcy|default|downgrade|probe|rejects|halts?|"
    r"hotter than expected|cyberattack|ransomware|oil supply disruption)\b",
    re.I,
)


@dataclass(frozen=True)
class ExternalMarketSignal:
    source: str
    external_id: str
    title: str
    url: str
    published_at: str
    region: str
    theme: str
    sentiment_score: float | None
    sentiment_label: str
    confidence: float
    assets: tuple[str, ...]
    asset_map_reason: str
    market_reaction_status: str
    market_reaction: dict[str, Any]
    prediction_gate_status: str
    prediction_gate_reason: str
    raw: dict[str, Any]
    ingestion_version: str = EXTERNAL_MARKET_SIGNAL_VERSION
    source_news_id: int | None = None
    event_cluster_id: int | None = None
    classifier_version: str = EXTERNAL_MARKET_SIGNAL_VERSION
    mapper_version: str = NEWS_ASSET_MAPPER_VERSION
    config_hash: str = ""


def _json_request(url: str, *, timeout: float, headers: dict[str, str] | None = None) -> dict[str, Any]:
    request = urllib.request.Request(url, headers=headers or {"User-Agent": "IBKRMarketEventRecorder/0.14"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


def _stable_external_id(source: str, title: str, url: str, published_at: str) -> str:
    payload = json.dumps(
        {"source": source, "title": title, "url": url, "published_at": published_at},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def classify_region_theme(text: str) -> tuple[str, str]:
    region = "global"
    for name, pattern in REGION_PATTERNS:
        if pattern.search(text):
            region = name
            break
    theme = "broad_geopolitical" if region != "global" or GEOPOLITICAL_CONTEXT_PATTERN.search(text) else "market_news"
    for name, pattern in THEME_PATTERNS:
        if pattern.search(text):
            theme = name
            break
    return region, theme


def assets_for_region_theme(region: str, theme: str) -> tuple[str, ...]:
    theme_map = REGION_THEME_ASSET_MAP.get(region) or REGION_THEME_ASSET_MAP["global"]
    return tuple(theme_map.get(theme) or theme_map.get("broad_geopolitical") or REGION_THEME_ASSET_MAP["global"]["broad_geopolitical"])


def _marketaux_article_text(title: str, raw: dict[str, Any]) -> str:
    """Return article-only text, excluding backfill/search metadata that can pollute classification."""

    chunks: list[str] = [str(title or "")]
    for key in ("description", "snippet", "body", "summary"):
        value = raw.get(key)
        if value:
            chunks.append(str(value))
    source = raw.get("source")
    if isinstance(source, str):
        chunks.append(source)
    elif isinstance(source, dict):
        chunks.append(str(source.get("name") or source.get("domain") or ""))
    for entity in raw.get("entities", []) or []:
        if isinstance(entity, dict):
            chunks.append(str(entity.get("name") or ""))
            chunks.append(str(entity.get("symbol") or ""))
            chunks.append(str(entity.get("type") or ""))
    return " ".join(chunk for chunk in chunks if chunk).strip()


def _classification_text(source: str, title: str, raw: dict[str, Any]) -> str:
    if str(source or "").lower() == "marketaux":
        return _marketaux_article_text(title, raw)
    return f"{title} {json.dumps(raw, ensure_ascii=False)[:1500]}"


def marketaux_signal_quality_decision(title: str, raw: dict[str, Any]) -> tuple[bool, list[str], float]:
    """Decide if a Marketaux article is useful enough for geopolitical/market-evidence ingestion.

    The decision is deliberately conservative about generic stock-pick articles, but it keeps
    macro, policy, sector, and geopolitical articles even when they came from a broad query.
    """

    article_text = _marketaux_article_text(title, raw)
    flags: list[str] = []
    low_signal = bool(MARKETAUX_LOW_SIGNAL_PATTERN.search(article_text))
    geopolitical = bool(GEOPOLITICAL_CONTEXT_PATTERN.search(article_text))
    market_relevant = bool(MARKETAUX_US_MARKET_RELEVANT_PATTERN.search(article_text))
    hard_event = bool(MARKETAUX_HARD_EVENT_PATTERN.search(article_text))
    if low_signal:
        flags.append("generic_stock_pick_or_analyst_article")
    if not geopolitical and not market_relevant:
        flags.append("weak_market_or_geopolitical_context")
    if low_signal and not hard_event:
        return True, flags + ["skip_low_signal_marketaux_noise"], 0.0
    multiplier = 1.0
    if low_signal:
        multiplier *= 0.72
    if not geopolitical and market_relevant:
        multiplier *= 0.88
    if not geopolitical and not market_relevant:
        multiplier *= 0.80
    return False, flags or ["article_context_ok"], multiplier


def marketaux_processing_status_from_quality_flags(
    raw: dict[str, Any],
    quality_flags: Iterable[str],
) -> tuple[str, str]:
    """Map Marketaux filter evidence into durable closeout status/skip_reason values."""

    flags = {str(flag or "") for flag in quality_flags}
    entities = raw.get("entities") if isinstance(raw, dict) else None
    if not entities:
        return "skipped_no_entities", "skipped_no_entities"
    if "skip_low_signal_marketaux_noise" in flags:
        return "skipped_noise", "skip_low_signal_marketaux_noise"
    if "generic_stock_pick_or_analyst_article" in flags:
        return "skipped_low_quality", "generic_stock_pick_or_analyst_article"
    if "weak_market_or_geopolitical_context" in flags:
        return "skipped_non_market_relevant", "weak_market_or_geopolitical_context"
    return "skipped_low_quality", "marketaux_quality_filter"


def sentiment_label(score: float | None) -> str:
    if score is None:
        return "unknown"
    if score <= -0.35:
        return "negative"
    if score >= 0.35:
        return "positive"
    return "neutral"


def _normalize_alpha_time(value: str) -> str:
    if re.fullmatch(r"\d{8}T\d{6}", value or ""):
        try:
            return datetime.strptime(value, "%Y%m%dT%H%M%S").replace(tzinfo=UTC).isoformat()
        except ValueError:
            return value
    return value


def _normalize_gdelt_time(value: str) -> str:
    text = str(value or "").strip()
    for pattern in ("%Y%m%dT%H%M%SZ", "%Y%m%d%H%M%S"):
        try:
            return datetime.strptime(text, pattern).replace(tzinfo=UTC).isoformat()
        except ValueError:
            pass
    return text


def parse_date(value: str) -> date:
    return date.fromisoformat(str(value).strip()[:10])


def _date_windows(start: date, end: date, *, window_days: int) -> list[tuple[date, date]]:
    if end < start:
        raise ValueError("end date must be on or after start date")
    windows: list[tuple[date, date]] = []
    current = start
    step = timedelta(days=max(1, int(window_days)))
    while current <= end:
        window_end = min(end, current + step - timedelta(days=1))
        windows.append((current, window_end))
        current = window_end + timedelta(days=1)
    return windows


def _gdelt_dt(value: date, *, end_of_day: bool = False) -> str:
    suffix = "235959" if end_of_day else "000000"
    return value.strftime("%Y%m%d") + suffix


def _alpha_dt(value: date, *, end_of_day: bool = False) -> str:
    suffix = "T2359" if end_of_day else "T0000"
    return value.strftime("%Y%m%d") + suffix


def _merge_counts(results: list[dict[str, Any]]) -> dict[str, Any]:
    requested = sum(int(item.get("requested") or 0) for item in results)
    created = sum(int(item.get("created") or 0) for item in results)
    updated = sum(int(item.get("updated") or 0) for item in results)
    errors: list[str] = []
    for item in results:
        errors.extend(str(error) for error in (item.get("errors") or []))
    return {
        "requested": requested,
        "created": created,
        "updated": updated,
        "errors": errors,
    }


def _redact_error(text: Any) -> str:
    value = str(text)[:500]
    value = re.sub(r"(API key as )([A-Za-z0-9_\-]+)", r"\1[REDACTED]", value)
    value = re.sub(r"(api_key=|apikey=|api_token=)([^&\\s]+)", r"\1[REDACTED]", value, flags=re.I)
    return value


def _error_result(exc: BaseException) -> dict[str, Any]:
    return {"requested": 0, "created": 0, "updated": 0, "errors": [f"{exc.__class__.__name__}: {_redact_error(exc)}"]}


def _clean_benzinga_headline(value: Any) -> str:
    text = str(value or "").strip()
    return BENZINGA_PREFIX_PATTERN.sub("", text).strip()


def _benzinga_sentiment(value: Any) -> tuple[float | None, float]:
    text = str(value or "")
    sentiment_match = BENZINGA_SENTIMENT_PATTERN.search(text)
    confidence_match = BENZINGA_CONFIDENCE_PATTERN.search(text)
    sentiment: float | None = None
    if sentiment_match and sentiment_match.group(1).lower() != "n/a":
        try:
            sentiment = float(sentiment_match.group(1))
        except ValueError:
            sentiment = None
    confidence = 0.74
    if confidence_match:
        try:
            confidence = max(0.0, min(1.0, float(confidence_match.group(1))))
        except ValueError:
            pass
    return sentiment, confidence


def _dow_jones_sentiment(value: Any, provider: str) -> tuple[float | None, float]:
    text = str(value or "")
    sentiment = infer_breaking_news_sentiment(text)
    if sentiment is None:
        positive = bool(
            re.search(
                r"\b(?:rises?|gains?|jumps?|outperforms?|higher|upgrade|raises?|beats?|"
                r"competition|demand|approval|buyback|acquisition)\b",
                text,
                re.I,
            )
        )
        negative = bool(
            re.search(
                r"\b(?:falls?|drops?|declines?|slips?|sheds?|underperforms?|lower|downgrade|"
                r"cuts?|misses?|probe|ban|weak|pressure)\b",
                text,
                re.I,
            )
        )
        if positive and not negative:
            sentiment = 0.42
        elif negative and not positive:
            sentiment = -0.48
        elif positive and negative:
            sentiment = -0.12
    confidence = 0.78 if str(provider or "").upper().startswith("DJ-RT") else 0.72
    if sentiment is None:
        confidence = min(confidence, 0.62)
    return sentiment, confidence


def _market_reaction_check(db: AnalysisDatabase, assets: Iterable[str]) -> tuple[str, dict[str, Any]]:
    checked: list[dict[str, Any]] = []
    for asset in assets:
        rows = db.recent_technical_indicators(symbol=str(asset).upper(), limit=1)
        if not rows:
            continue
        row = rows[0]
        checked.append(
            {
                "symbol": row["symbol"],
                "as_of_date": row["as_of_date"],
                "price_source": row["price_source"],
                "return_1d": row["return_1d"],
                "return_5d": row["return_5d"],
                "volatility_20": row["volatility_20"],
            }
        )
    if checked:
        return "available", {"technical_indicators": checked}
    return "pending_market_data_followup", {"technical_indicators": []}


def _prediction_gate(
    *,
    assets: tuple[str, ...],
    confidence: float,
    sentiment: float | None,
    market_reaction_status: str,
) -> tuple[str, str]:
    if not assets:
        return "blocked", "no mapped market assets"
    if confidence < 0.50:
        return "blocked", f"source classification confidence {confidence:.2f} below 0.50"
    if sentiment is not None and abs(sentiment) >= 0.35:
        return "eligible", "mapped assets and non-neutral sentiment cross-check available"
    if market_reaction_status == "available":
        return "observe_only", "mapped assets and market context available, but sentiment edge is weak"
    return "observe_only", "mapped assets available; market reaction follow-up pending"


def _make_signal(
    *,
    source: str,
    title: str,
    url: str,
    published_at: str,
    sentiment_score: float | None,
    confidence: float,
    raw: dict[str, Any],
    db: AnalysisDatabase,
    source_news_id: int | None = None,
    classifier_version: str = EXTERNAL_MARKET_SIGNAL_VERSION,
    mapper_version: str = NEWS_ASSET_MAPPER_VERSION,
    config_hash: str = "",
) -> ExternalMarketSignal:
    text = _classification_text(source, title, raw)
    region, theme = classify_region_theme(text)
    assets = assets_for_region_theme(region, theme)
    try:
        from .geopolitical_intelligence import adaptive_assets_for_region_theme

        assets = adaptive_assets_for_region_theme(db, region=region, theme=theme, fallback_assets=assets)
    except Exception:
        pass
    market_status, market_payload = _market_reaction_check(db, assets)
    gate_status, gate_reason = _prediction_gate(
        assets=assets,
        confidence=confidence,
        sentiment=sentiment_score,
        market_reaction_status=market_status,
    )
    return ExternalMarketSignal(
        source=source,
        external_id=str(raw.get("uuid") or raw.get("id") or raw.get("url") or _stable_external_id(source, title, url, published_at)),
        title=title[:1000],
        url=url,
        published_at=published_at,
        region=region,
        theme=theme,
        sentiment_score=sentiment_score,
        sentiment_label=sentiment_label(sentiment_score),
        confidence=confidence,
        assets=assets,
        asset_map_reason=f"{region}/{theme} mapped to {','.join(assets)}",
        market_reaction_status=market_status,
        market_reaction=market_payload,
        prediction_gate_status=gate_status,
        prediction_gate_reason=gate_reason,
        raw=raw,
        source_news_id=source_news_id,
        classifier_version=classifier_version,
        mapper_version=mapper_version,
        config_hash=config_hash,
    )


def _coerce_published_at(value: str | None) -> str:
    text = str(value or "").strip()
    if not text:
        return datetime.now(UTC).isoformat()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat()


def _clean_symbols(symbols: Iterable[str] | str | None) -> tuple[str, ...]:
    if symbols is None:
        return ()
    if isinstance(symbols, str):
        items = symbols.split(",")
    else:
        items = symbols
    return tuple(dict.fromkeys(str(item).strip().upper() for item in items if str(item).strip()))


def _urgency_rank(value: str) -> int:
    return {"low": 0, "medium": 1, "high": 2, "critical": 3}.get(str(value or "").lower(), -1)


def classify_breaking_news_urgency(headline: str, *, body: str = "") -> dict[str, Any]:
    text = f"{headline} {body}".strip()
    signals: list[str] = []
    score = 0
    critical_hit = bool(_BREAKING_CRITICAL_RE.search(text))
    high_hit = bool(_BREAKING_HIGH_RE.search(text))
    if critical_hit:
        score += 3
        signals.append("critical_keyword")
    if high_hit:
        score += 2
        signals.append("high_impact_keyword")
    region, theme = classify_region_theme(text)
    if region != "global":
        score += 1
        signals.append(f"region={region}")
    if theme not in {"market_news", "broad_geopolitical"}:
        score += 1
        signals.append(f"theme={theme}")
    word_count = len(re.findall(r"\w+", str(headline or "")))
    if word_count <= 18:
        score += 1
        signals.append("one_line_fast_path")
    if critical_hit and score >= 5:
        urgency = "critical"
    elif critical_hit or (high_hit and score >= 3):
        urgency = "high"
    elif high_hit or score >= 2:
        urgency = "medium"
    else:
        urgency = "low"
    return {
        "urgency": urgency,
        "urgency_score": score,
        "signals": signals,
        "region": region,
        "theme": theme,
        "word_count": word_count,
    }


def infer_breaking_news_sentiment(headline: str) -> float | None:
    positive = bool(_BREAKING_POSITIVE_RE.search(headline))
    negative = bool(_BREAKING_NEGATIVE_RE.search(headline))
    if positive and not negative:
        return 0.58
    if negative and not positive:
        return -0.72
    if positive and negative:
        return -0.20
    return None


def build_breaking_news_signal(
    db: AnalysisDatabase,
    *,
    headline: str,
    published_at: str | None = None,
    symbols: Iterable[str] | str | None = None,
    source_label: str = "manual",
    url: str = "",
    sentiment_score: float | None = None,
    confidence: float | None = None,
) -> ExternalMarketSignal:
    title = " ".join(str(headline or "").split())
    if not title:
        raise ValueError("headline is required")
    published = _coerce_published_at(published_at)
    urgency = classify_breaking_news_urgency(title)
    score = sentiment_score if sentiment_score is not None else infer_breaking_news_sentiment(title)
    inferred_confidence = {
        "critical": 0.88,
        "high": 0.80,
        "medium": 0.66,
        "low": 0.52,
    }[urgency["urgency"]]
    confidence_value = max(0.0, min(1.0, float(confidence if confidence is not None else inferred_confidence)))
    mapped_assets = assets_for_region_theme(str(urgency["region"]), str(urgency["theme"]))
    explicit_symbols = _clean_symbols(symbols)
    assets = tuple(dict.fromkeys((*explicit_symbols, *mapped_assets)))
    if not assets:
        assets = ("SPY", "QQQ", "VIX")
    market_status, market_payload = _market_reaction_check(db, assets)
    gate_status, gate_reason = _prediction_gate(
        assets=assets,
        confidence=confidence_value,
        sentiment=score,
        market_reaction_status=market_status,
    )
    if urgency["urgency"] in {"critical", "high"} and gate_status == "observe_only":
        gate_status = "eligible"
        gate_reason = f"emergency {urgency['urgency']} one-line news requires immediate scorer alert; {gate_reason}"
    raw = {
        "emergency": True,
        "one_line": True,
        "source_label": source_label,
        "urgency": urgency,
        "latency_policy": "immediate_overlay_then_full_analysis",
        "system_action": "immediate_scorer_alert",
        "body_available": False,
        "ingestion_version": BREAKING_NEWS_SIGNAL_VERSION,
    }
    return ExternalMarketSignal(
        source=BREAKING_NEWS_SOURCE,
        external_id=f"breaking:{_stable_external_id(BREAKING_NEWS_SOURCE, title, url, published)}",
        title=title[:1000],
        url=url,
        published_at=published,
        region=str(urgency["region"]),
        theme=str(urgency["theme"]),
        sentiment_score=score,
        sentiment_label=sentiment_label(score),
        confidence=confidence_value,
        assets=assets,
        asset_map_reason=(
            f"emergency {urgency['urgency']} one-line headline; "
            f"{urgency['region']}/{urgency['theme']} mapped to {','.join(assets)}"
        ),
        market_reaction_status=market_status,
        market_reaction=market_payload,
        prediction_gate_status=gate_status,
        prediction_gate_reason=gate_reason,
        raw=raw,
        ingestion_version=BREAKING_NEWS_SIGNAL_VERSION,
    )


def breaking_news_packet(signal: ExternalMarketSignal) -> dict[str, Any]:
    raw = signal.raw if isinstance(signal.raw, dict) else {}
    urgency = raw.get("urgency") if isinstance(raw.get("urgency"), dict) else {}
    return {
        "packet_version": "breaking-news-emergency-packet-v1",
        "source": signal.source,
        "headline": signal.title,
        "published_at": signal.published_at,
        "urgency": urgency.get("urgency", "unknown"),
        "urgency_score": urgency.get("urgency_score"),
        "region": signal.region,
        "theme": signal.theme,
        "assets": list(signal.assets),
        "sentiment_label": signal.sentiment_label,
        "sentiment_score": signal.sentiment_score,
        "confidence": signal.confidence,
        "prediction_gate_status": signal.prediction_gate_status,
        "prediction_gate_reason": signal.prediction_gate_reason,
        "market_reaction_status": signal.market_reaction_status,
        "latency_policy": raw.get("latency_policy", "immediate_overlay_then_full_analysis"),
        "system_action": raw.get("system_action", "immediate_scorer_alert"),
        "scorer_note": (
            "Treat this as an immediate pre-analysis overlay. Do not wait for long-form article analysis "
            "before lowering confidence, requiring second pass, or checking mapped asset reaction."
        ),
    }


class GdeltClient:
    def __init__(self, *, timeout_seconds: float = 20.0, opener: Any | None = None) -> None:
        self.timeout_seconds = timeout_seconds
        self.opener = opener

    def fetch(
        self,
        *,
        query: str = GEOPOLITICAL_QUERY,
        max_records: int = 25,
        timespan: str | None = "3d",
        startdatetime: str | None = None,
        enddatetime: str | None = None,
    ) -> dict[str, Any]:
        params = {
            "query": query,
            "mode": "ArtList",
            "format": "json",
            "maxrecords": max(1, min(250, int(max_records))),
            "sort": "DateDesc",
        }
        if startdatetime and enddatetime:
            params["startdatetime"] = startdatetime
            params["enddatetime"] = enddatetime
        elif timespan:
            params["timespan"] = timespan
        url = f"{GDELT_DOC_URL}?{urllib.parse.urlencode(params)}"
        return _json_request(url, timeout=self.timeout_seconds)


class MarketauxClient:
    def __init__(self, *, api_key: str, timeout_seconds: float = 20.0) -> None:
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    def fetch(
        self,
        *,
        search: str | None = "geopolitical OR sanctions OR Taiwan OR oil OR war",
        symbols: str | None = None,
        limit: int = 20,
        published_after: str | None = None,
        published_before: str | None = None,
        page: int | None = None,
        filter_entities: bool = True,
        must_have_entities: bool | None = None,
        entity_types: str | None = None,
        countries: str | None = None,
        language: str = "en",
        group_similar: bool | None = None,
    ) -> dict[str, Any]:
        params = {
            "api_token": self.api_key,
            "limit": max(1, min(100, int(limit))),
            "filter_entities": "true" if filter_entities else "false",
        }
        if language:
            params["language"] = language
        if search:
            params["search"] = search
        if symbols:
            params["symbols"] = symbols
        if must_have_entities is not None:
            params["must_have_entities"] = "true" if must_have_entities else "false"
        if entity_types:
            params["entity_types"] = entity_types
        if countries:
            params["countries"] = countries
        if group_similar is not None:
            params["group_similar"] = "true" if group_similar else "false"
        if published_after:
            params["published_after"] = published_after
        if published_before:
            params["published_before"] = published_before
        if page is not None:
            params["page"] = max(1, int(page))
        url = f"{MARKETAUX_URL}?{urllib.parse.urlencode(params)}"
        return _json_request(url, timeout=self.timeout_seconds)


class AlphaVantageNewsClient:
    def __init__(self, *, api_key: str, timeout_seconds: float = 20.0) -> None:
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    def fetch(
        self,
        *,
        tickers: str = "SPY,QQQ,TLT,SOXX,TSM,NVDA,XLE,GLD",
        topics: str | None = None,
        limit: int = 50,
        time_from: str | None = None,
        time_to: str | None = None,
    ) -> dict[str, Any]:
        params = {
            "function": "NEWS_SENTIMENT",
            "apikey": self.api_key,
            "limit": max(1, min(1000, int(limit))),
            "sort": "LATEST",
        }
        if tickers:
            params["tickers"] = tickers
        if topics:
            params["topics"] = topics
        if time_from:
            params["time_from"] = time_from
        if time_to:
            params["time_to"] = time_to
        url = f"{ALPHA_VANTAGE_URL}?{urllib.parse.urlencode(params)}"
        return _json_request(url, timeout=self.timeout_seconds)


class ExternalMarketSignalCollector:
    def __init__(self, db: AnalysisDatabase, *, timeout_seconds: float = 20.0) -> None:
        self.db = db
        self.timeout_seconds = timeout_seconds

    def _store_marketaux_raw_article(
        self,
        item: dict[str, Any],
        *,
        ingest_mode: str,
    ) -> int:
        title = str(item.get("title") or "")
        url = str(item.get("url") or "")
        published = _coerce_published_at(str(item.get("published_at") or ""))
        updated = str(item.get("updated_at") or item.get("updated") or "") or None
        source = item.get("source")
        publisher = ""
        if isinstance(source, dict):
            publisher = str(source.get("name") or source.get("domain") or "")
        elif isinstance(source, str):
            publisher = source
        body = ""
        for key in ("description", "snippet", "body", "summary"):
            if item.get(key):
                body = str(item.get(key) or "")
                break
        raw_id = str(item.get("uuid") or item.get("id") or item.get("url") or "")
        received_at = datetime.now(UTC).isoformat()
        with self.db.connect() as conn:
            ensure_news_lineage_v2_schema(conn)
            news_id = upsert_raw_news_article(
                conn,
                source_system="marketaux",
                provider="marketaux",
                publisher=publisher,
                provider_article_id=raw_id,
                headline=title,
                body=body or None,
                url=url,
                published_at_utc=published,
                updated_at_utc=updated,
                received_at_utc=received_at,
                available_at_utc=published,
                availability_quality="provider_published",
                ingest_mode=ingest_mode,
                route_type="external_api",
                primary_route_symbol="",
                provider_primary_symbol=str(item.get("primary_symbol") or ""),
                raw_json=item,
            )
            upsert_news_visibility_policy(
                conn,
                news_id=news_id,
                source_system="marketaux",
                provider="marketaux",
                ingest_mode=ingest_mode,
                published_at_utc=published,
                provider_updated_at_utc=updated,
                received_at_utc=received_at,
                available_at_utc=published,
                latency_seconds=MARKETAUX_HISTORICAL_VISIBILITY_LATENCY_SECONDS,
            )
            record_provider_entities(
                conn,
                news_id=news_id,
                provider="marketaux",
                entities=item.get("entities") if isinstance(item.get("entities"), list) else (),
            )
            store_article_asset_mapping(conn, news_id)
            assign_article_to_event_cluster(conn, news_id)
            return news_id

    def _persist_marketaux_skip_status(
        self,
        *,
        news_id: int,
        processing_status: str,
        skip_reason: str,
        quality_flags: Iterable[str],
    ) -> None:
        with self.db.connect() as conn:
            cluster = conn.execute(
                """SELECT event_cluster_id
                     FROM news_article_cluster_links
                    WHERE news_id=?
                    ORDER BY is_primary_source DESC,event_cluster_id
                    LIMIT 1""",
                (int(news_id),),
            ).fetchone()
            upsert_raw_news_processing_status(
                conn,
                news_id=int(news_id),
                source_system="marketaux",
                provider="marketaux",
                processing_status=processing_status,
                skip_reason=skip_reason,
                skip_flags=list(quality_flags),
                derived_signal_id=None,
                event_cluster_id=int(cluster["event_cluster_id"]) if cluster is not None else None,
                classifier_version=EXTERNAL_MARKET_SIGNAL_VERSION,
                mapper_version=NEWS_ASSET_MAPPER_VERSION,
            )

    def _sync_legacy_news_article(self, legacy_news_id: int) -> int:
        with self.db.connect() as conn:
            raw_news_id = sync_legacy_news_article_to_raw(conn, int(legacy_news_id))
            store_article_asset_mapping(conn, raw_news_id)
            return raw_news_id

    def collect_gdelt(self, *, limit: int = 25, timespan: str = "3d") -> dict[str, Any]:
        return {
            "requested": 0,
            "created": 0,
            "updated": 0,
            "errors": ["GDELT is disabled in M167 active source operations"],
            "disabled": True,
        }

    def collect_gdelt_window(
        self,
        *,
        query: str,
        start: date,
        end: date,
        limit: int = 25,
    ) -> dict[str, Any]:
        return {
            "requested": 0,
            "created": 0,
            "updated": 0,
            "errors": ["GDELT is disabled in M167 active source operations"],
            "disabled": True,
        }

    def collect_marketaux(
        self,
        *,
        api_key: str,
        limit: int = 20,
        symbols: str | None = None,
        search: str | None = "geopolitical OR sanctions OR Taiwan OR oil OR war",
        filter_entities: bool = True,
        must_have_entities: bool | None = None,
        entity_types: str | None = None,
        countries: str | None = None,
        group_similar: bool | None = None,
    ) -> dict[str, Any]:
        try:
            payload = MarketauxClient(api_key=api_key, timeout_seconds=self.timeout_seconds).fetch(
                limit=limit,
                symbols=symbols,
                search=search,
                filter_entities=filter_entities,
                must_have_entities=must_have_entities,
                entity_types=entity_types,
                countries=countries,
                group_similar=group_similar,
            )
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            return _error_result(exc)
        signals: list[ExternalMarketSignal] = []
        skipped_noise = 0
        for item in payload.get("data", []) or []:
            title = str(item.get("title") or "")
            url = str(item.get("url") or "")
            published = str(item.get("published_at") or "")
            raw_item = dict(item)
            source_news_id = self._store_marketaux_raw_article(raw_item, ingest_mode="polling")
            skip, quality_flags, confidence_multiplier = marketaux_signal_quality_decision(title, raw_item)
            if skip:
                skipped_noise += 1
                processing_status, skip_reason = marketaux_processing_status_from_quality_flags(
                    raw_item,
                    quality_flags,
                )
                self._persist_marketaux_skip_status(
                    news_id=source_news_id,
                    processing_status=processing_status,
                    skip_reason=skip_reason,
                    quality_flags=quality_flags,
                )
                continue
            raw_item["marketaux_quality_flags"] = quality_flags
            raw_item["marketaux_quality_action"] = "accepted"
            score = item.get("sentiment_score")
            if score is None:
                entity_scores = []
                for entity in item.get("entities", []) or []:
                    try:
                        entity_scores.append(float(entity.get("sentiment_score")))
                    except (AttributeError, TypeError, ValueError):
                        pass
                if entity_scores:
                    score = sum(entity_scores) / len(entity_scores)
            try:
                parsed_score = float(score) if score is not None else None
            except (TypeError, ValueError):
                parsed_score = None
            if not title:
                self._persist_marketaux_skip_status(
                    news_id=source_news_id,
                    processing_status="error",
                    skip_reason="missing_title",
                    quality_flags=["missing_title"],
                )
                continue
            base_confidence = 0.72 if parsed_score is not None else 0.58
            signals.append(
                _make_signal(
                    source="marketaux",
                    title=title,
                    url=url,
                    published_at=published,
                    sentiment_score=parsed_score,
                    confidence=max(0.0, min(1.0, base_confidence * confidence_multiplier)),
                    raw=raw_item,
                    db=self.db,
                    source_news_id=source_news_id,
                )
            )
        result = self._store(signals)
        result["skipped_noise"] = skipped_noise
        return result

    def collect_marketaux_window(
        self,
        *,
        api_key: str,
        search: str | None,
        start: date,
        end: date,
        limit: int = 20,
        symbols: str | None = None,
        filter_entities: bool = True,
        must_have_entities: bool | None = None,
        entity_types: str | None = None,
        countries: str | None = None,
        group_similar: bool | None = None,
        backfill_mode: str = "query",
        query_name: str | None = None,
    ) -> dict[str, Any]:
        try:
            payload = MarketauxClient(api_key=api_key, timeout_seconds=self.timeout_seconds).fetch(
                search=search,
                limit=limit,
                symbols=symbols,
                published_after=f"{start.isoformat()}T00:00:00",
                published_before=f"{(end + timedelta(days=1)).isoformat()}T00:00:00",
                filter_entities=filter_entities,
                must_have_entities=must_have_entities,
                entity_types=entity_types,
                countries=countries,
                group_similar=group_similar,
            )
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            return _error_result(exc)
        signals: list[ExternalMarketSignal] = []
        skipped_noise = 0
        for item in payload.get("data", []) or []:
            title = str(item.get("title") or "")
            url = str(item.get("url") or "")
            published = str(item.get("published_at") or "")
            raw_item = {
                **dict(item),
                "backfill_query": search or "",
                "backfill_query_name": query_name or "",
                "marketaux_backfill_mode": backfill_mode,
                "backfill_start": start.isoformat(),
                "backfill_end": end.isoformat(),
            }
            source_news_id = self._store_marketaux_raw_article(raw_item, ingest_mode="manual_backfill")
            skip, quality_flags, confidence_multiplier = marketaux_signal_quality_decision(title, raw_item)
            if skip:
                skipped_noise += 1
                processing_status, skip_reason = marketaux_processing_status_from_quality_flags(
                    raw_item,
                    quality_flags,
                )
                self._persist_marketaux_skip_status(
                    news_id=source_news_id,
                    processing_status=processing_status,
                    skip_reason=skip_reason,
                    quality_flags=quality_flags,
                )
                continue
            raw_item["marketaux_quality_flags"] = quality_flags
            raw_item["marketaux_quality_action"] = "accepted"
            score = item.get("sentiment_score")
            if score is None:
                entity_scores = []
                for entity in item.get("entities", []) or []:
                    try:
                        entity_scores.append(float(entity.get("sentiment_score")))
                    except (AttributeError, TypeError, ValueError):
                        pass
                if entity_scores:
                    score = sum(entity_scores) / len(entity_scores)
            try:
                parsed_score = float(score) if score is not None else None
            except (TypeError, ValueError):
                parsed_score = None
            if not title:
                self._persist_marketaux_skip_status(
                    news_id=source_news_id,
                    processing_status="error",
                    skip_reason="missing_title",
                    quality_flags=["missing_title"],
                )
                continue
            base_confidence = 0.72 if parsed_score is not None else 0.58
            signals.append(
                _make_signal(
                    source="marketaux",
                    title=title,
                    url=url,
                    published_at=published,
                    sentiment_score=parsed_score,
                    confidence=max(0.0, min(1.0, base_confidence * confidence_multiplier)),
                    raw=raw_item,
                    db=self.db,
                    source_news_id=source_news_id,
                )
            )
        result = self._store(signals)
        result["skipped_noise"] = skipped_noise
        return result

    def collect_alpha_vantage(self, *, api_key: str, tickers: str, limit: int = 50, topics: str | None = None) -> dict[str, Any]:
        try:
            payload = AlphaVantageNewsClient(api_key=api_key, timeout_seconds=self.timeout_seconds).fetch(
                tickers=tickers,
                topics=topics,
                limit=limit,
            )
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            return _error_result(exc)
        if "Information" in payload or "Note" in payload:
            return {"requested": 0, "created": 0, "updated": 0, "errors": [_redact_error(payload.get("Information") or payload.get("Note"))]}
        signals: list[ExternalMarketSignal] = []
        for item in (payload.get("feed", []) or [])[: max(1, int(limit))]:
            title = str(item.get("title") or "")
            url = str(item.get("url") or "")
            published_raw = str(item.get("time_published") or "")
            published = _normalize_alpha_time(published_raw)
            score = item.get("overall_sentiment_score")
            try:
                parsed_score = float(score) if score is not None else None
            except (TypeError, ValueError):
                parsed_score = None
            if not title:
                continue
            signals.append(
                _make_signal(
                    source="alpha_vantage",
                    title=title,
                    url=url,
                    published_at=published,
                    sentiment_score=parsed_score,
                    confidence=0.70 if parsed_score is not None else 0.55,
                    raw=dict(item),
                    db=self.db,
                )
            )
        return self._store(signals)

    def collect_alpha_vantage_window(
        self,
        *,
        api_key: str,
        tickers: str,
        start: date,
        end: date,
        limit: int = 50,
    ) -> dict[str, Any]:
        try:
            payload = AlphaVantageNewsClient(api_key=api_key, timeout_seconds=self.timeout_seconds).fetch(
                tickers=tickers,
                limit=limit,
                time_from=_alpha_dt(start),
                time_to=_alpha_dt(end, end_of_day=True),
            )
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            return _error_result(exc)
        if "Information" in payload or "Note" in payload:
            return {"requested": 0, "created": 0, "updated": 0, "errors": [_redact_error(payload.get("Information") or payload.get("Note"))]}
        signals: list[ExternalMarketSignal] = []
        for item in (payload.get("feed", []) or [])[: max(1, int(limit))]:
            title = str(item.get("title") or "")
            url = str(item.get("url") or "")
            published_raw = str(item.get("time_published") or "")
            published = _normalize_alpha_time(published_raw)
            score = item.get("overall_sentiment_score")
            try:
                parsed_score = float(score) if score is not None else None
            except (TypeError, ValueError):
                parsed_score = None
            if not title:
                continue
            signals.append(
                _make_signal(
                    source="alpha_vantage",
                    title=title,
                    url=url,
                    published_at=published,
                    sentiment_score=parsed_score,
                    confidence=0.70 if parsed_score is not None else 0.55,
                    raw={**dict(item), "backfill_tickers": tickers, "backfill_start": start.isoformat(), "backfill_end": end.isoformat()},
                    db=self.db,
                )
            )
        return self._store(signals)

    def backfill(
        self,
        *,
        source: str,
        start: date,
        end: date,
        window_days: int,
        limit_per_window: int,
        api_key: str | None = None,
        symbols: str | None = None,
        tickers: str | None = None,
        max_windows: int | None = None,
        pause_seconds: float = 0.0,
        marketaux_mode: str = "query",
        marketaux_query_name: str | None = None,
        marketaux_entity_types: str = MARKETAUX_DEFAULT_ENTITY_TYPES,
        marketaux_countries: str = MARKETAUX_DEFAULT_COUNTRIES,
        marketaux_group_similar: bool = True,
        marketaux_must_have_entities: bool = True,
    ) -> dict[str, Any]:
        windows = _date_windows(start, end, window_days=window_days)
        if max_windows is not None:
            windows = windows[: max(0, int(max_windows))]
        results: list[dict[str, Any]] = []
        executed: list[dict[str, Any]] = []
        queries = GEOPOLITICAL_BACKFILL_QUERIES
        normalized_marketaux_mode = str(marketaux_mode or "query").strip().lower()
        if normalized_marketaux_mode not in MARKETAUX_BACKFILL_MODES:
            raise ValueError(f"Unsupported Marketaux backfill mode: {marketaux_mode}")
        selected_marketaux_queries = MARKETAUX_BACKFILL_QUERIES
        normalized_marketaux_query_name = str(marketaux_query_name or "").strip()
        if normalized_marketaux_query_name:
            selected_marketaux_queries = tuple(
                (query_name, query)
                for query_name, query in MARKETAUX_BACKFILL_QUERIES
                if query_name == normalized_marketaux_query_name
            )
            if not selected_marketaux_queries:
                valid_names = ", ".join(query_name for query_name, _ in MARKETAUX_BACKFILL_QUERIES)
                raise ValueError(
                    f"Unsupported Marketaux backfill query: {marketaux_query_name}. "
                    f"Valid query names: {valid_names}"
                )
        for window_start, window_end in windows:
            if source == "gdelt":
                result = self.collect_gdelt_window(
                    query="disabled",
                    start=window_start,
                    end=window_end,
                    limit=limit_per_window,
                )
                results.append(result)
                executed.append({"source": source, "query": "disabled", "start": window_start.isoformat(), "end": window_end.isoformat(), **result})
                break
            elif source == "marketaux":
                if not api_key:
                    result = {"requested": 0, "created": 0, "updated": 0, "errors": ["MARKETAUX_API_KEY is not configured"]}
                    results.append(result)
                    executed.append({"source": source, "start": window_start.isoformat(), "end": window_end.isoformat(), **result})
                    break
                if normalized_marketaux_mode in {"broad", "hybrid"}:
                    result = self.collect_marketaux_window(
                        api_key=api_key,
                        search=None,
                        symbols=None,
                        start=window_start,
                        end=window_end,
                        limit=limit_per_window,
                        filter_entities=True,
                        must_have_entities=marketaux_must_have_entities,
                        entity_types=marketaux_entity_types,
                        countries=marketaux_countries,
                        group_similar=marketaux_group_similar,
                        backfill_mode="broad",
                        query_name="broad_entity_discovery",
                    )
                    results.append(result)
                    executed.append(
                        {
                            "source": source,
                            "query": "broad_entity_discovery",
                            "mode": "broad",
                            "start": window_start.isoformat(),
                            "end": window_end.isoformat(),
                            **result,
                        }
                    )
                    if pause_seconds > 0:
                        time.sleep(float(pause_seconds))
                if normalized_marketaux_mode in {"query", "hybrid"}:
                    for query_name, query in selected_marketaux_queries:
                        result = self.collect_marketaux_window(
                            api_key=api_key,
                            search=query,
                            symbols=symbols,
                            start=window_start,
                            end=window_end,
                            limit=limit_per_window,
                            filter_entities=True,
                            backfill_mode="query",
                            query_name=query_name,
                        )
                        results.append(result)
                        executed.append({"source": source, "query": query_name, "mode": "query", "start": window_start.isoformat(), "end": window_end.isoformat(), **result})
                        if pause_seconds > 0:
                            time.sleep(float(pause_seconds))
            elif source == "alpha_vantage":
                if not api_key:
                    result = {"requested": 0, "created": 0, "updated": 0, "errors": ["ALPHA_VANTAGE_API_KEY is not configured"]}
                    results.append(result)
                    executed.append({"source": source, "start": window_start.isoformat(), "end": window_end.isoformat(), **result})
                    break
                result = self.collect_alpha_vantage_window(
                    api_key=api_key,
                    tickers=tickers or "SPY,QQQ,TLT,SOXX,TSM,NVDA,XLE,GLD",
                    start=window_start,
                    end=window_end,
                    limit=limit_per_window,
                )
                results.append(result)
                executed.append({"source": source, "start": window_start.isoformat(), "end": window_end.isoformat(), **result})
                if pause_seconds > 0:
                    time.sleep(float(pause_seconds))
            else:
                raise ValueError(f"Unsupported external market backfill source: {source}")
        merged = _merge_counts(results)
        merged.update(
            {
                "source": source,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "window_days": max(1, int(window_days)),
                "limit_per_window": max(1, int(limit_per_window)),
                "windows": len(windows),
                "executed": executed,
                "marketaux_mode": normalized_marketaux_mode if source == "marketaux" else None,
                "marketaux_query_name": normalized_marketaux_query_name if source == "marketaux" else None,
            }
        )
        return merged

    def convert_benzinga_news(
        self,
        *,
        start: date,
        end: date,
        limit: int = 5000,
        symbols: tuple[str, ...] | None = None,
        keywords_only: bool = True,
        dynamic_total_limit: int = 0,
        dynamic_per_symbol_limit: int = 0,
        dynamic_priority_per_symbol_limit: int = 0,
        dynamic_priority_symbols: tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        time_expr = "COALESCE(NULLIF(provider_published_at,''), published_at)"
        where = [
            "provider='BZ'",
            f"substr({time_expr},1,10)>=?",
            f"substr({time_expr},1,10)<=?",
        ]
        params: list[Any] = [start.isoformat(), end.isoformat()]
        cleaned_symbols = tuple(dict.fromkeys(symbol.strip().upper() for symbol in (symbols or ()) if symbol.strip()))
        if cleaned_symbols:
            where.append(f"primary_symbol IN ({','.join('?' for _ in cleaned_symbols)})")
            params.extend(cleaned_symbols)
        params.append(max(0, int(limit)))
        with self.db.connect() as conn:
            rows = [
                dict(row)
                for row in conn.execute(
                    f"""
                    SELECT id,provider,article_id,headline,body,primary_symbol,
                           published_at,provider_published_at,received_at,extra_data
                    FROM news_articles
                    WHERE {' AND '.join(where)}
                    ORDER BY {time_expr},id
                    LIMIT ?
                    """,
                    tuple(params),
                ).fetchall()
            ]

        selected_rows, dynamic_limit = _dynamic_news_row_limit(
            rows,
            source="benzinga",
            total_limit=dynamic_total_limit,
            per_symbol_limit=dynamic_per_symbol_limit,
            priority_per_symbol_limit=dynamic_priority_per_symbol_limit,
            priority_symbols=dynamic_priority_symbols,
        )

        signals: list[ExternalMarketSignal] = []
        skipped = 0
        for row in selected_rows:
            source_news_id = self._sync_legacy_news_article(int(row.get("id") or 0))
            raw_headline = str(row.get("headline") or "")
            clean_title = _clean_benzinga_headline(raw_headline)
            body = str(row.get("body") or "")
            if keywords_only and not BENZINGA_SIGNAL_PATTERN.search(f"{clean_title} {body}"):
                skipped += 1
                continue
            published_at = str(row.get("provider_published_at") or row.get("published_at") or row.get("received_at") or "")
            sentiment, confidence = _benzinga_sentiment(raw_headline)
            signal = _make_signal(
                source="benzinga",
                title=clean_title or raw_headline,
                url="",
                published_at=published_at,
                sentiment_score=sentiment,
                confidence=confidence,
                raw={
                    "news_article_id": row.get("id"),
                    "source_news_id": source_news_id,
                    "provider": row.get("provider"),
                    "article_id": row.get("article_id"),
                    "primary_symbol": row.get("primary_symbol"),
                    "headline": raw_headline,
                    "body_available": bool(body),
                    "source_table": "news_articles",
                },
                db=self.db,
                source_news_id=source_news_id,
            )
            primary_symbol = str(row.get("primary_symbol") or "").upper()
            inferred_mapping = infer_news_assets(
                clean_title or raw_headline,
                body,
                primary_symbol=primary_symbol,
                provider=str(row.get("provider") or ""),
                extra_data=str(row.get("extra_data") or ""),
            )
            if primary_symbol.startswith("BROADTAPE:"):
                assets = tuple(
                    dict.fromkeys(
                        (
                            *inferred_mapping.target_candidates,
                            *inferred_mapping.inferred_assets,
                            *signal.assets,
                        )
                    )
                )
                map_prefix = (
                    f"Benzinga BroadTape inferred {','.join(assets) if assets else 'UNKNOWN'} "
                    f"({inferred_mapping.confidence}); {inferred_mapping.reason}"
                )
            else:
                assets = tuple(dict.fromkeys((primary_symbol, *signal.assets))) if primary_symbol else signal.assets
                map_prefix = f"Benzinga {primary_symbol or 'UNKNOWN'} headline"
            signal = ExternalMarketSignal(
                source=signal.source,
                external_id=f"bz:{row.get('id')}",
                title=signal.title,
                url=signal.url,
                published_at=signal.published_at,
                region=signal.region,
                theme=signal.theme,
                sentiment_score=signal.sentiment_score,
                sentiment_label=signal.sentiment_label,
                confidence=signal.confidence,
                assets=assets,
                asset_map_reason=f"{map_prefix}; {signal.asset_map_reason}",
                market_reaction_status=signal.market_reaction_status,
                market_reaction=signal.market_reaction,
                prediction_gate_status=signal.prediction_gate_status,
                prediction_gate_reason=signal.prediction_gate_reason,
                raw={
                    **signal.raw,
                    "asset_mapping": inferred_mapping.as_dict(),
                },
                source_news_id=signal.source_news_id,
                event_cluster_id=signal.event_cluster_id,
                classifier_version=signal.classifier_version,
                mapper_version=signal.mapper_version,
                config_hash=signal.config_hash,
            )
            signals.append(signal)
        stored = self._store(signals)
        stored.update(
            {
                "source": "benzinga",
                "start": start.isoformat(),
                "end": end.isoformat(),
                "scanned": len(rows),
                "selected": len(selected_rows),
                "skipped": skipped,
                "dynamic_limit": dynamic_limit,
            }
        )
        return stored

    def convert_dow_jones_news(
        self,
        *,
        start: date,
        end: date,
        limit: int = 5000,
        symbols: tuple[str, ...] | None = None,
        keywords_only: bool = True,
        dynamic_total_limit: int = 0,
        dynamic_per_symbol_limit: int = 0,
        dynamic_priority_per_symbol_limit: int = 0,
        dynamic_priority_symbols: tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        time_expr = "COALESCE(NULLIF(provider_published_at,''), published_at)"
        where = [
            "upper(provider) LIKE 'DJ%'",
            f"substr({time_expr},1,10)>=?",
            f"substr({time_expr},1,10)<=?",
        ]
        params: list[Any] = [start.isoformat(), end.isoformat()]
        cleaned_symbols = tuple(dict.fromkeys(symbol.strip().upper() for symbol in (symbols or ()) if symbol.strip()))
        if cleaned_symbols:
            where.append(f"primary_symbol IN ({','.join('?' for _ in cleaned_symbols)})")
            params.extend(cleaned_symbols)
        params.append(max(0, int(limit)))
        with self.db.connect() as conn:
            rows = [
                dict(row)
                for row in conn.execute(
                    f"""
                    SELECT id,provider,article_id,headline,body,primary_symbol,
                           published_at,provider_published_at,received_at,extra_data
                    FROM news_articles
                    WHERE {' AND '.join(where)}
                    ORDER BY {time_expr},id
                    LIMIT ?
                    """,
                    tuple(params),
                ).fetchall()
            ]

        selected_rows, dynamic_limit = _dynamic_news_row_limit(
            rows,
            source="dow_jones",
            total_limit=dynamic_total_limit,
            per_symbol_limit=dynamic_per_symbol_limit,
            priority_per_symbol_limit=dynamic_priority_per_symbol_limit,
            priority_symbols=dynamic_priority_symbols,
        )

        signals: list[ExternalMarketSignal] = []
        skipped = 0
        for row in selected_rows:
            source_news_id = self._sync_legacy_news_article(int(row.get("id") or 0))
            provider = str(row.get("provider") or "").upper()
            raw_headline = " ".join(str(row.get("headline") or "").split())
            body = str(row.get("body") or "")
            if keywords_only and not DOW_JONES_SIGNAL_PATTERN.search(f"{raw_headline} {body}"):
                skipped += 1
                continue
            published_at = str(row.get("provider_published_at") or row.get("published_at") or row.get("received_at") or "")
            sentiment, confidence = _dow_jones_sentiment(raw_headline, provider)
            signal = _make_signal(
                source="dow_jones",
                title=raw_headline,
                url="",
                published_at=published_at,
                sentiment_score=sentiment,
                confidence=confidence,
                raw={
                    "news_article_id": row.get("id"),
                    "source_news_id": source_news_id,
                    "provider": row.get("provider"),
                    "article_id": row.get("article_id"),
                    "primary_symbol": row.get("primary_symbol"),
                    "headline": raw_headline,
                    "body_available": bool(body),
                    "source_table": "news_articles",
                },
                db=self.db,
                source_news_id=source_news_id,
            )
            primary_symbol = str(row.get("primary_symbol") or "").upper()
            inferred_mapping = infer_news_assets(
                raw_headline,
                body,
                primary_symbol=primary_symbol,
                provider=provider,
                extra_data=str(row.get("extra_data") or ""),
            )
            assets = tuple(
                dict.fromkeys(
                    (
                        *((primary_symbol,) if primary_symbol and not primary_symbol.startswith("BROADTAPE:") else ()),
                        *inferred_mapping.target_candidates,
                        *inferred_mapping.inferred_assets,
                        *signal.assets,
                    )
                )
            )
            map_prefix = (
                f"Dow Jones {provider or 'DJ'} inferred {','.join(assets) if assets else 'UNKNOWN'} "
                f"({inferred_mapping.confidence}); {inferred_mapping.reason}"
            )
            signal = ExternalMarketSignal(
                source=signal.source,
                external_id=f"dj:{row.get('id')}",
                title=signal.title,
                url=signal.url,
                published_at=signal.published_at,
                region=signal.region,
                theme=signal.theme,
                sentiment_score=signal.sentiment_score,
                sentiment_label=signal.sentiment_label,
                confidence=signal.confidence,
                assets=assets,
                asset_map_reason=f"{map_prefix}; {signal.asset_map_reason}",
                market_reaction_status=signal.market_reaction_status,
                market_reaction=signal.market_reaction,
                prediction_gate_status=signal.prediction_gate_status,
                prediction_gate_reason=signal.prediction_gate_reason,
                raw={
                    **signal.raw,
                    "asset_mapping": inferred_mapping.as_dict(),
                },
                source_news_id=signal.source_news_id,
                event_cluster_id=signal.event_cluster_id,
                classifier_version=signal.classifier_version,
                mapper_version=signal.mapper_version,
                config_hash=signal.config_hash,
            )
            signals.append(signal)
        stored = self._store(signals)
        stored.update(
            {
                "source": "dow_jones",
                "start": start.isoformat(),
                "end": end.isoformat(),
                "scanned": len(rows),
                "selected": len(selected_rows),
                "skipped": skipped,
                "dynamic_limit": dynamic_limit,
            }
        )
        return stored

    def ingest_breaking_news(
        self,
        *,
        headline: str,
        published_at: str | None = None,
        symbols: Iterable[str] | str | None = None,
        source_label: str = "manual",
        url: str = "",
        sentiment_score: float | None = None,
        confidence: float | None = None,
    ) -> dict[str, Any]:
        signal = build_breaking_news_signal(
            self.db,
            headline=headline,
            published_at=published_at,
            symbols=symbols,
            source_label=source_label,
            url=url,
            sentiment_score=sentiment_score,
            confidence=confidence,
        )
        signal_id, created = self.db.upsert_external_market_signal(signal)
        return {
            "requested": 1,
            "created": int(created),
            "updated": int(not created),
            "errors": [],
            "signal_id": signal_id,
            "emergency_packet": breaking_news_packet(signal),
        }

    def backfill_emergency_breaking_news(
        self,
        *,
        start: date,
        end: date,
        source: str = "all",
        limit: int = 100000,
        min_urgency: str = "high",
    ) -> dict[str, Any]:
        if end < start:
            raise ValueError("end date must be on or after start date")
        if source not in {"all", "news_articles", "external_market_signals"}:
            raise ValueError("source must be all, news_articles, or external_market_signals")
        threshold = _urgency_rank(min_urgency)
        if threshold < 0:
            raise ValueError("min_urgency must be low, medium, high, or critical")
        max_rows = max(0, int(limit))
        signals: list[ExternalMarketSignal] = []
        scanned = 0
        skipped_low_urgency = 0
        skipped_existing_emergency = 0
        skipped_duplicate = 0
        seen_keys: set[str] = set()
        errors: list[str] = []

        def _append_signal(signal: ExternalMarketSignal, *, external_id: str, raw_updates: dict[str, Any]) -> None:
            nonlocal skipped_duplicate
            normalized_title = re.sub(r"\s+", " ", signal.title.lower()).strip()
            key = f"{signal.published_at[:19]}|{normalized_title}"
            if key in seen_keys:
                skipped_duplicate += 1
                return
            seen_keys.add(key)
            raw = dict(signal.raw)
            raw.update(raw_updates)
            signals.append(replace(signal, external_id=external_id, raw=raw))

        if source in {"all", "news_articles"} and len(signals) < max_rows:
            time_expr = "COALESCE(NULLIF(provider_published_at,''), published_at)"
            with self.db.connect() as conn:
                rows = [
                    dict(row)
                    for row in conn.execute(
                        f"""
                        SELECT id,provider,article_id,headline,body,primary_symbol,{time_expr} AS event_time
                        FROM news_articles
                        WHERE substr({time_expr},1,10)>=? AND substr({time_expr},1,10)<=?
                        ORDER BY {time_expr},id
                        LIMIT ?
                        """,
                        (start.isoformat(), end.isoformat(), max_rows),
                    ).fetchall()
                ]
            for row in rows:
                scanned += 1
                headline = str(row.get("headline") or "")
                body = str(row.get("body") or "")
                urgency = classify_breaking_news_urgency(headline)
                if _urgency_rank(str(urgency.get("urgency"))) < threshold:
                    skipped_low_urgency += 1
                    continue
                sentiment: float | None = None
                confidence: float | None = None
                if str(row.get("provider") or "").upper() == "BZ":
                    sentiment, confidence = _benzinga_sentiment(headline)
                try:
                    signal = build_breaking_news_signal(
                        self.db,
                        headline=_clean_benzinga_headline(headline) or headline,
                        published_at=str(row.get("event_time") or ""),
                        symbols=(str(row.get("primary_symbol") or ""),),
                        source_label=f"historical_news:{row.get('provider') or 'unknown'}",
                        sentiment_score=sentiment,
                        confidence=confidence,
                    )
                    _append_signal(
                        signal,
                        external_id=f"breaking:news_article:{row.get('id')}",
                        raw_updates={
                            "backfill_source": "news_articles",
                            "news_article_id": row.get("id"),
                            "provider": row.get("provider"),
                            "article_id": row.get("article_id"),
                            "body_available": bool(body),
                        },
                    )
                except Exception as exc:
                    errors.append(_redact_error(f"news_article {row.get('id')}: {exc}"))
                if len(signals) >= max_rows:
                    break

        if source in {"all", "external_market_signals"} and len(signals) < max_rows:
            remaining = max_rows - len(signals)
            with self.db.connect() as conn:
                rows = [
                    dict(row)
                    for row in conn.execute(
                        """
                        SELECT *
                        FROM external_market_signals
                        WHERE source!=? AND published_at!=''
                          AND substr(published_at,1,10)>=? AND substr(published_at,1,10)<=?
                        ORDER BY published_at,id
                        LIMIT ?
                        """,
                        (BREAKING_NEWS_SOURCE, start.isoformat(), end.isoformat(), remaining),
                    ).fetchall()
                ]
            for row in rows:
                scanned += 1
                headline = str(row.get("title") or "")
                urgency = classify_breaking_news_urgency(headline)
                if _urgency_rank(str(urgency.get("urgency"))) < threshold:
                    skipped_low_urgency += 1
                    continue
                try:
                    assets = _clean_symbols(json.loads(str(row.get("assets_json") or "[]")))
                except json.JSONDecodeError:
                    assets = ()
                try:
                    signal = build_breaking_news_signal(
                        self.db,
                        headline=headline,
                        published_at=str(row.get("published_at") or ""),
                        symbols=assets,
                        source_label=f"historical_external:{row.get('source') or 'unknown'}",
                        url=str(row.get("url") or ""),
                        sentiment_score=row.get("sentiment_score"),
                        confidence=row.get("confidence"),
                    )
                    _append_signal(
                        signal,
                        external_id=f"breaking:external_signal:{row.get('id')}",
                        raw_updates={
                            "backfill_source": "external_market_signals",
                            "external_market_signal_id": row.get("id"),
                            "original_source": row.get("source"),
                            "original_region": row.get("region"),
                            "original_theme": row.get("theme"),
                        },
                    )
                except Exception as exc:
                    errors.append(_redact_error(f"external_signal {row.get('id')}: {exc}"))
                if len(signals) >= max_rows:
                    break

        stored = self._store(signals)
        stored.update(
            {
                "source": "emergency_breaking_news_backfill",
                "start": start.isoformat(),
                "end": end.isoformat(),
                "input_source": source,
                "min_urgency": min_urgency,
                "scanned": scanned,
                "skipped_low_urgency": skipped_low_urgency,
                "skipped_existing_emergency": skipped_existing_emergency,
                "skipped_duplicate": skipped_duplicate,
            }
        )
        stored["errors"].extend(errors)
        return stored

    def _store(self, signals: list[ExternalMarketSignal]) -> dict[str, Any]:
        created = 0
        updated = 0
        for signal in signals:
            _, was_created = self.db.upsert_external_market_signal(signal)
            if was_created:
                created += 1
            else:
                updated += 1
        return {"requested": len(signals), "created": created, "updated": updated, "errors": []}


def format_external_market_signal_report(rows: list[dict[str, Any]]) -> str:
    lines = [
        "External Market Signal Report",
        "source | published | region | theme | sentiment | assets | gate | title",
        "--- | --- | --- | --- | --- | --- | --- | ---",
    ]
    for row in rows:
        assets = ",".join(json.loads(row["assets_json"] or "[]"))
        sentiment = row["sentiment_label"]
        if row["sentiment_score"] is not None:
            sentiment = f"{sentiment}({float(row['sentiment_score']):+.2f})"
        lines.append(
            f"{row['source']} | {row['published_at'] or '-'} | {row['region']} | {row['theme']} | "
            f"{sentiment} | {assets or '-'} | {row['prediction_gate_status']} | {str(row['title'])[:140]}"
        )
    return "\n".join(lines)
