from __future__ import annotations

import json
import re
import sqlite3
import hashlib
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .news_asset_mapper import infer_news_assets


CROSS_PROVIDER_EVENT_ROLE_VERSION = "m236-cross-provider-event-role-v5-no-alpha-active-training"
CONFIDENCE_ADJUSTMENT_POSITIVE_CAP = 0.12
CONFIDENCE_ADJUSTMENT_NEGATIVE_CAP = -0.18
DEFAULT_OUTCOME_HORIZONS = ("5M", "15M", "30M", "EOD")
CORE_US_ASSETS = {"SPY", "QQQ", "IWM", "DIA", "SPX", "NDX", "RUT", "VIX", "NVDA", "AAPL", "MSFT", "TSLA", "AMZN", "META", "GOOGL"}
PSEUDO_ROUTE_PREFIXES = ("BROADTAPE:",)
INDEX_PROXY_ALIASES = {
    "SPX": "SPY",
    "SPXW": "SPY",
    "XSP": "SPY",
    "NDX": "QQQ",
    "XND": "QQQ",
    "RUT": "IWM",
    "VIX": "VXX",
}
ROLE_FAMILY_PROXY_ASSETS = {
    "macro": ("SPY", "QQQ", "TLT", "UUP", "GLD"),
    "geopolitical": ("USO", "XLE", "GLD", "VXX", "SPY"),
    "sector": ("QQQ", "SPY", "SOXX", "SMH"),
    "options": ("SPY", "QQQ", "IWM"),
    "market_color": ("SPY", "QQQ", "IWM", "DIA"),
    "context": ("SPY",),
}
PROVIDER_WEIGHT_BY_FAMILY = {
    "dow_jones": {"reliability": 0.92, "speed": 0.78, "independence": 0.95},
    "benzinga": {"reliability": 0.78, "speed": 0.88, "independence": 0.85},
    "emergency_breaking_news": {"reliability": 0.62, "speed": 0.96, "independence": 0.55},
    "marketaux": {"reliability": 0.46, "speed": 0.42, "independence": 0.35},
}

_ROLE_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("analyst_rating", re.compile(r"\b(upgrade|downgrade|maintains|initiates|assumes|price target|pt raised|pt lowered|rating)\b", re.I)),
    ("analyst_price_target", re.compile(r"\b(price target|pt raised|pt lowered|target price|raises target|lowers target)\b", re.I)),
    ("earnings_guidance", re.compile(r"\b(guidance|outlook|forecast|raises fy|lowers fy|sees q[1-4]|sees fiscal)\b", re.I)),
    ("earnings_result", re.compile(r"\b(earnings|eps|revenue|preliminary results|quarterly results|fiscal|beats estimates|misses estimates)\b", re.I)),
    ("earnings", re.compile(r"\b(earnings|eps|revenue|guidance|preliminary results|quarterly results|fiscal)\b", re.I)),
    ("macro_data_release", re.compile(r"\b(cpi|pce|inflation|payrolls|jobs report|unemployment|gdp|retail sales|ism|pmi|consumer confidence|michigan sentiment|jobless claims)\b", re.I)),
    ("fed_commentary", re.compile(r"\b(fed|fomc|powell|bostic|waller|williams|treasury yield|rate cut|rate hike|rates)\b", re.I)),
    ("macro", re.compile(r"\b(cpi|pce|inflation|payrolls|jobs report|unemployment|gdp|retail sales|ism|pmi|fed|fomc|treasury yield|rates)\b", re.I)),
    ("policy", re.compile(r"\b(tariff|sanction|export control|tax bill|regulation|regulatory|white house|congress|sec probe|ftc|doj)\b", re.I)),
    ("geopolitical_deescalation", re.compile(r"\b(ceasefire|truce|peace talks|de-escalation|deescalation|ceases fire|hostage deal|diplomatic talks)\b", re.I)),
    ("geopolitical_escalation", re.compile(r"\b(attack|attacks|missile|invasion|war|strike|strikes|sanction|escalation|hormuz|red sea|houthi|nato)\b", re.I)),
    ("geopolitical", re.compile(r"\b(iran|israel|gaza|houthi|red sea|hormuz|taiwan|china|russia|ukraine|north korea|nato|war|missile|ceasefire)\b", re.I)),
    ("sector_ai_theme", re.compile(r"\b(ai|artificial intelligence|semiconductor|chip|gpu|hbm|data center|datacenter|accelerator)\b", re.I)),
    ("option_market_color", re.compile(r"\b(options|gamma|max pain|put-call|put call|0dte|call wall|put wall|dealer positioning)\b", re.I)),
    ("sector_rotation", re.compile(r"\b(sector|semiconductor|chip|ai|energy|oil|banks|financials|defense|utilities|healthcare|biotech)\b", re.I)),
    ("market_color", re.compile(r"\b(stocks to watch|market talk|market roundup|etf falls|etf rises|futures|nasdaq|dow jones|s&p 500|wall street)\b", re.I)),
    ("single_stock_catalyst", re.compile(r"\b(shares|stock|company|announces|launches|contract|deal|partnership|buyback|offering|fda|trial)\b", re.I)),
)

_POSITIVE = re.compile(r"\b(raises|raised|beats|beat|surges|jumps|rises|gains|buy|outperform|positive|approval|partnership|buyback|record|optimistic|strong|demand|upgrade|accelerates|resilient)\b", re.I)
_NEGATIVE = re.compile(r"\b(cuts|cut|misses|miss|falls|drops|sinks|sell|underperform|negative|probe|lawsuit|warning|downgrade|risk|war|sanction|cautious|concern|pressure|valuation|margin|weak|slows|uncertain|overvalued|lower|lowers)\b", re.I)
_MARKETAUX_NOISE = re.compile(
    r"\b(sensex|nifty|bse|tadawul|saudi|arabian|lse|ftse|hk bourse|form 8\.5|plc|tsx|asx|jse|bovespa|warrant exercise)\b",
    re.I,
)
_US_MARKET_RELEVANT = re.compile(
    r"\b(nasdaq|s&p 500|spx|spy|qqq|iwm|dow|russell|vix|nvda|aapl|msft|amzn|meta|googl|tsla|ai|chip|semiconductor|fed|fomc|cpi|pce|payrolls|treasury|oil|tariff|white house|earnings|guidance)\b",
    re.I,
)


@dataclass(frozen=True)
class CrossProviderEventRoleConfig:
    database_path: Path = Path("market_events.db")
    mesh_json_path: Path = Path("exports/m235_news_subscription_mesh.json")
    outcome_log_jsonl_path: Path | None = None
    now: str | None = None
    lookback_minutes: int = 240
    max_news_rows: int = 2000
    max_external_rows: int = 1000
    cluster_window_minutes: int = 45
    write_outcome_log: bool = False


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _config_now(config: CrossProviderEventRoleConfig) -> datetime:
    parsed = _parse_dt(config.now)
    return parsed or datetime.now(UTC)


def _parse_dt(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.expanduser().read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _tokenize(text: str) -> set[str]:
    stop = {
        "the",
        "and",
        "for",
        "with",
        "from",
        "that",
        "this",
        "after",
        "into",
        "over",
        "under",
        "market",
        "stock",
        "stocks",
        "shares",
        "news",
        "says",
    }
    return {t for t in re.findall(r"[a-z0-9]{3,}", text.lower()) if t not in stop}


def _stable_hash(parts: list[Any], *, length: int = 16) -> str:
    raw = "|".join(str(part or "") for part in parts)
    return hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()[:length]


def _provider_family(provider: str, source: str = "") -> str:
    text = f"{provider} {source}".strip().lower()
    if not text:
        return ""
    if text.startswith("dj") or "dow" in text:
        return "dow_jones"
    if "benzinga" in text or text.startswith("bz"):
        return "benzinga"
    if "marketaux" in text:
        return "marketaux"
    if "alpha" in text:
        return "alpha_vantage"
    if "emergency" in text or "breaking" in text:
        return "emergency_breaking_news"
    return re.sub(r"[^a-z0-9_]+", "_", text).strip("_")


def _provider_weight(provider: str, source: str = "") -> dict[str, float]:
    family = _provider_family(provider, source)
    base = PROVIDER_WEIGHT_BY_FAMILY.get(family, {"reliability": 0.30, "speed": 0.25, "independence": 0.20})
    text = str(provider or "").upper()
    speed_boost = 0.08 if text.startswith("DJ-RT") or text == "BZ" else 0.0
    return {
        "reliability": round(min(1.0, float(base["reliability"])), 4),
        "speed": round(min(1.0, float(base["speed"]) + speed_boost), 4),
        "independence": round(min(1.0, float(base["independence"])), 4),
        "overall": round((float(base["reliability"]) * 0.55 + min(1.0, float(base["speed"]) + speed_boost) * 0.25 + float(base["independence"]) * 0.20), 4),
    }


def _role_family(role: str) -> str:
    text = str(role or "unknown")
    if text.startswith("breaking_"):
        text = text.replace("breaking_", "")
    if text.startswith("analyst_") or text == "analyst_rating":
        return "analyst"
    if text.startswith("earnings"):
        return "earnings"
    if text.startswith("macro") or text.startswith("fed_"):
        return "macro"
    if text.startswith("geopolitical"):
        return "geopolitical"
    if text.startswith("sector_"):
        return "sector"
    if text.startswith("option_"):
        return "options"
    return text


def _max_pairwise_token_overlap(items: list[dict[str, Any]]) -> int:
    token_sets = [_tokenize(str(item.get("title") or "")) for item in items]
    best = 0
    for idx, left in enumerate(token_sets):
        for right in token_sets[idx + 1 :]:
            best = max(best, len(left.intersection(right)))
    return best


def _time_span_minutes(items: list[dict[str, Any]]) -> float:
    dates = [_parse_dt(item.get("published_at")) for item in items]
    parsed = [date for date in dates if date is not None]
    if len(parsed) < 2:
        return 0.0
    return round((max(parsed) - min(parsed)).total_seconds() / 60.0, 2)


def _marketaux_quality(title: str, assets: list[str]) -> tuple[float, list[str]]:
    score = 0.0
    flags: list[str] = []
    text = str(title or "")
    normalized_assets = {str(asset or "").upper() for asset in assets if str(asset or "").strip()}
    if _US_MARKET_RELEVANT.search(text):
        score += 0.45
        flags.append("us_market_topic")
    if normalized_assets.intersection(CORE_US_ASSETS):
        score += 0.35
        flags.append("core_us_asset")
    if _MARKETAUX_NOISE.search(text):
        score -= 0.55
        flags.append("likely_non_us_or_feed_noise")
    if not normalized_assets:
        score -= 0.15
        flags.append("no_asset_mapping")
    return max(-1.0, min(1.0, round(score, 4))), flags


def _item_quality(item: dict[str, Any]) -> tuple[float, list[str]]:
    provider = str(item.get("provider") or "").lower()
    source = str(item.get("source") or "").lower()
    title = str(item.get("title") or "")
    if "marketaux" in {provider, source}:
        return _marketaux_quality(title, list(item.get("assets") or []))
    if _provider_family(str(item.get("provider") or ""), str(item.get("source") or "")) in {"benzinga", "dow_jones", "emergency_breaking_news"}:
        if _MARKETAUX_NOISE.search(title):
            return 0.15, ["primary_or_fast_provider", "likely_non_us_or_feed_noise"]
        return 0.75, ["primary_or_fast_provider"]
    if "alpha" in {provider, source}:
        return 0.45, ["sentiment_crosscheck"]
    return 0.25, ["context_provider"]


def _sentiment_role(item: dict[str, Any]) -> str:
    family = str(item.get("provider_family") or _provider_family(str(item.get("provider") or ""), str(item.get("source") or "")))
    source_table = str(item.get("source_table") or "")
    if source_table == "news_articles" and family in {"benzinga", "dow_jones"}:
        return "primary_event_source"
    if family == "emergency_breaking_news":
        return "fast_reflex_source"
    if family in {"marketaux", "alpha_vantage"}:
        return "sentiment_crosscheck"
    if source_table == "external_market_signals":
        return "aggregator_crosscheck"
    return "context"


def _ticker_inference(item: dict[str, Any], temp_symbols: set[str]) -> dict[str, Any]:
    primary = str(item.get("primary_symbol") or "").upper()
    assets = sorted({str(asset or "").upper() for asset in item.get("assets") or [] if str(asset or "").strip()})
    title_tokens = _tokenize(str(item.get("title") or ""))
    inferred = bool(not primary or primary.startswith("BROADTAPE:"))
    if not inferred:
        score = 0.95
        method = "direct_primary_symbol"
    else:
        score = 0.10
        method = "broadtape_or_external_inferred"
        if assets:
            score += 0.35
        if set(assets).intersection(CORE_US_ASSETS):
            score += 0.20
        if any(asset.lower() in title_tokens for asset in assets):
            score += 0.15
        if temp_symbols.intersection(set(assets)):
            score += 0.10
    if not assets:
        score -= 0.10
    return {
        "inferred": inferred,
        "method": method,
        "confidence": round(max(0.0, min(1.0, score)), 4),
        "asset_count": len(assets),
        "temp_symbol_related": bool(temp_symbols.intersection(set(assets))),
    }


def _adaptive_confidence_cap(
    *,
    agreement: str,
    confirmation_quality: str,
    role_family: str,
    provider_families: list[str],
    avg_ticker_confidence: float,
    conflict_score: float,
    average_quality: float,
) -> dict[str, Any]:
    primary_families = {"benzinga", "dow_jones", "emergency_breaking_news"}
    primary_confirmed = len(primary_families.intersection(set(provider_families))) >= 2
    positive_cap = 0.0
    negative_cap = 0.0
    reason = "no_boost"
    if agreement == "confirmed":
        positive_cap = 0.12 if primary_confirmed else 0.08
        reason = "confirmed_primary_provider_match" if primary_confirmed else "confirmed_with_external_crosscheck"
        if role_family in {"context", "breaking"}:
            positive_cap = min(positive_cap, 0.03)
            reason = "confirmed_but_context_or_unspecified"
        if avg_ticker_confidence < 0.45:
            positive_cap = min(positive_cap, 0.04)
            reason = "confirmed_but_low_ticker_confidence"
    elif agreement == "weak_confirmation":
        positive_cap = 0.03
        reason = "weak_confirmation_small_boost_only"
    elif agreement == "conflict":
        negative_cap = -0.08 - min(0.10, max(0.0, conflict_score) * 0.10)
        reason = "provider_direction_conflict"
    if average_quality < 0.25 and positive_cap > 0:
        positive_cap = min(positive_cap, 0.02)
        reason = "low_quality_cap"
    return {
        "positive_max": round(min(CONFIDENCE_ADJUSTMENT_POSITIVE_CAP, positive_cap), 4),
        "negative_min": round(max(CONFIDENCE_ADJUSTMENT_NEGATIVE_CAP, negative_cap), 4),
        "reason": reason,
        "primary_provider_confirmed": primary_confirmed,
    }


def _confidence_adjustment(agreement: str, confirmation_quality: str, conflict_score: float, average_quality: float, adaptive_cap: dict[str, Any] | None = None) -> float:
    cap = adaptive_cap or {
        "positive_max": CONFIDENCE_ADJUSTMENT_POSITIVE_CAP,
        "negative_min": CONFIDENCE_ADJUSTMENT_NEGATIVE_CAP,
    }
    if agreement == "conflict":
        penalty = -0.08 - min(0.10, max(0.0, conflict_score) * 0.10)
        return max(float(cap.get("negative_min", CONFIDENCE_ADJUSTMENT_NEGATIVE_CAP)), round(penalty, 4))
    if agreement == "confirmed":
        base = 0.12 if confirmation_quality == "strong" else 0.08
        if average_quality < 0.35:
            base = min(base, 0.04)
        return min(float(cap.get("positive_max", CONFIDENCE_ADJUSTMENT_POSITIVE_CAP)), round(base, 4))
    if agreement == "weak_confirmation":
        return min(float(cap.get("positive_max", 0.03)), 0.02)
    return 0.0


def _canonical_event(items: list[dict[str, Any]], role_family: str, shared_assets: list[str]) -> dict[str, Any]:
    asset_key = (shared_assets or sorted({asset for item in items for asset in (item.get("assets") or [])}) or ["MARKET"])[0]
    token_counter: Counter[str] = Counter()
    for item in items:
        token_counter.update(_tokenize(str(item.get("title") or "")))
    signature_tokens = [token for token, _count in token_counter.most_common(8)]
    first_time = min((_parse_dt(item.get("published_at")) for item in items if _parse_dt(item.get("published_at"))), default=None)
    canonical_key = f"{asset_key}|{role_family}|{' '.join(signature_tokens[:6])}"
    canonical_id = _stable_hash([canonical_key, first_time.isoformat() if first_time else ""])
    return {
        "canonical_event_id": canonical_id,
        "canonical_event_key": canonical_key,
        "canonical_asset": asset_key,
        "signature_tokens": signature_tokens,
        "first_seen_at": first_time.isoformat() if first_time else None,
        "duplicate_count": max(0, len(items) - 1),
        "is_repeat_coverage": len(items) > 1,
    }


def _append_outcome_learning_records(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    existing: set[str] = set()
    if resolved.exists():
        try:
            for line in resolved.read_text(encoding="utf-8").splitlines()[-5000:]:
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                key = str(row.get("learning_record_key") or "")
                if key:
                    existing.add(key)
        except Exception:
            existing = set()
    appended = 0
    records: list[str] = []
    for cluster in payload.get("clusters") or []:
        first = (cluster.get("items") or [{}])[0]
        event_time = first.get("published_at") or cluster.get("canonical_event", {}).get("first_seen_at") or payload.get("generated_at")
        key = _stable_hash([cluster.get("canonical_event_id"), event_time, cluster.get("provider_agreement")], length=20)
        if key in existing:
            continue
        record = {
            "learning_record_version": "m236-outcome-learning-pending-v1",
            "learning_record_key": key,
            "status": "pending_market_outcome",
            "created_at": payload.get("generated_at"),
            "created_at_local": payload.get("generated_at_local"),
            "canonical_event_id": cluster.get("canonical_event_id"),
            "canonical_event_key": cluster.get("canonical_event", {}).get("canonical_event_key"),
            "event_time": event_time,
            "target_assets": cluster.get("shared_assets") or [cluster.get("canonical_event", {}).get("canonical_asset")],
            "target_asset_mapping": cluster.get("target_asset_mapping") or {},
            "source_titles": [str(item.get("title") or "") for item in (cluster.get("items") or [])[:5]],
            "provider_agreement": cluster.get("provider_agreement"),
            "confirmation_quality": cluster.get("confirmation_quality"),
            "event_role": cluster.get("event_role"),
            "role_family": cluster.get("role_family"),
            "direction_counts": cluster.get("direction_counts") or {},
            "average_direction_score": cluster.get("average_direction_score"),
            "confidence_adjustment": cluster.get("confidence_adjustment"),
            "adaptive_confidence_cap": cluster.get("adaptive_confidence_cap") or {},
            "provider_families": cluster.get("provider_families") or [],
            "provider_weighted_confirmation_score": cluster.get("provider_weighted_confirmation_score"),
            "ticker_inference_confidence": cluster.get("average_ticker_inference_confidence"),
            "horizons": list(DEFAULT_OUTCOME_HORIZONS),
            "outcome_attachment_policy": "Attach future market reactions later; this record itself contains no future outcome data.",
        }
        records.append(json.dumps(record, sort_keys=True, ensure_ascii=False))
        existing.add(key)
        appended += 1
    if records:
        with resolved.open("a", encoding="utf-8") as handle:
            for record in records:
                handle.write(record + "\n")
    return {
        "path": str(resolved),
        "records_appended": appended,
        "status": "ok",
        "horizons": list(DEFAULT_OUTCOME_HORIZONS),
    }


def classify_event_role(title: str, *, provider: str = "", source: str = "") -> dict[str, Any]:
    text = str(title or "")
    roles = [role for role, pattern in _ROLE_RULES if pattern.search(text)]
    if not roles:
        roles = ["breaking" if provider.upper() == "BZ" else "context"]
    pos = len(_POSITIVE.findall(text))
    neg = len(_NEGATIVE.findall(text))
    if pos > neg:
        direction = "positive"
        direction_score = min(1.0, (pos - neg) / 3.0)
    elif neg > pos:
        direction = "negative"
        direction_score = -min(1.0, (neg - pos) / 3.0)
    else:
        direction = "neutral"
        direction_score = 0.0
    is_fast = provider.upper() == "BZ" or provider.upper().startswith("DJ-RT")
    primary = roles[0]
    if any(role.startswith("geopolitical") for role in roles) and is_fast:
        primary = "breaking_geopolitical"
    elif primary == "market_color" and is_fast:
        primary = "breaking_market_color"
    return {
        "event_role": primary,
        "all_roles": roles,
        "direction": direction,
        "direction_score": round(direction_score, 4),
        "provider": provider,
        "source": source or ("news_articles" if provider else "external_market_signals"),
        "is_fast_provider": is_fast,
    }


def _coerce_assets(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = [raw]
    else:
        parsed = raw
    return [str(item).strip().upper() for item in parsed or [] if str(item).strip()]


def _normalizable_asset(raw: Any) -> str:
    asset = str(raw or "").strip().upper()
    if not asset:
        return ""
    if any(asset.startswith(prefix) for prefix in PSEUDO_ROUTE_PREFIXES):
        return ""
    asset = INDEX_PROXY_ALIASES.get(asset, asset)
    if asset in {"", "UNKNOWN"}:
        return ""
    if any(ch in asset for ch in ("/", " ", ":", "=")):
        return ""
    return asset


def _normalized_assets(values: Any) -> list[str]:
    assets: list[str] = []
    for raw in _coerce_assets(values):
        asset = _normalizable_asset(raw)
        if asset and asset not in assets:
            assets.append(asset)
    return assets


def _asset_mapping_for_news_item(*, title: str, primary_symbol: str, provider: str, extra_data: Any = None) -> dict[str, Any]:
    mapping = infer_news_assets(
        title,
        primary_symbol=primary_symbol,
        provider=provider,
        extra_data=extra_data,
    )
    candidates = _normalized_assets(mapping.target_candidates)
    return {
        **mapping.as_dict(),
        "normalized_target_candidates": candidates,
    }


def _cluster_target_assets(items: list[dict[str, Any]], role_family: str, shared_assets: list[str]) -> tuple[list[str], dict[str, Any]]:
    combined_title = " ".join(str(item.get("title") or "") for item in items)[:20000]
    direct_assets = _normalized_assets(shared_assets)
    if not direct_assets:
        direct_assets = _normalized_assets(asset for item in items for asset in (item.get("assets") or []))
    mapping = infer_news_assets(combined_title, provider="cluster")
    mapped_assets = _normalized_assets(mapping.target_candidates)
    proxies = list(ROLE_FAMILY_PROXY_ASSETS.get(str(role_family or ""), ()))
    assets = _normalized_assets([*direct_assets, *mapped_assets, *proxies])
    if not assets:
        assets = ["SPY"]
    return assets[:20], {
        "mapping_version": "m236-cluster-asset-proxy-v1",
        "direct_assets": direct_assets[:20],
        "mapped_assets": mapped_assets[:20],
        "role_family_proxy_assets": proxies,
        "confidence": mapping.confidence,
        "reason": mapping.reason,
        "themes": list(mapping.themes),
    }


def _cluster_key(item: dict[str, Any], cluster_window_minutes: int, *, now: datetime) -> str:
    published = _parse_dt(item.get("published_at")) or now
    bucket = int(published.timestamp() // max(60, int(cluster_window_minutes) * 60))
    assets = _normalized_assets(item.get("assets")) or _normalized_assets([str(item.get("primary_symbol") or "").upper()])
    asset_key = assets[0] if assets else "MARKET"
    role = _role_family(str(item.get("event_role") or "unknown"))
    return f"{bucket}|{asset_key}|{role}"


def _load_rows(config: CrossProviderEventRoleConfig, *, now: datetime) -> list[dict[str, Any]]:
    since = (now - timedelta(minutes=int(config.lookback_minutes))).isoformat()
    rows: list[dict[str, Any]] = []
    con = sqlite3.connect(str(config.database_path), timeout=30)
    con.row_factory = sqlite3.Row
    try:
        for row in con.execute(
            """
            SELECT id,provider,primary_symbol,headline AS title,
                   COALESCE(provider_published_at,published_at,received_at) AS published_at,
                   extra_data
              FROM news_articles
             WHERE COALESCE(provider_published_at,published_at,received_at)>=?
               AND (provider='BZ' OR upper(provider) LIKE 'DJ%')
             ORDER BY COALESCE(provider_published_at,published_at,received_at) DESC,id DESC
             LIMIT ?
            """,
            (since, int(config.max_news_rows)),
        ):
            primary = str(row["primary_symbol"] or "").upper()
            assets = [primary] if primary and not primary.startswith("BROADTAPE:") else []
            mapping_payload: dict[str, Any] = {}
            try:
                extra = json.loads(row["extra_data"] or "{}")
                mapping = (extra.get("asset_mapping") or {}) if isinstance(extra, dict) else {}
                assets.extend(_coerce_assets(mapping.get("target_candidates")))
            except Exception:
                extra = {}
            try:
                mapping_payload = _asset_mapping_for_news_item(
                    title=str(row["title"] or ""),
                    primary_symbol=primary,
                    provider=str(row["provider"] or ""),
                    extra_data=extra,
                )
                assets.extend(mapping_payload.get("normalized_target_candidates") or [])
            except Exception:
                mapping_payload = {}
            rows.append(
                {
                    "source_table": "news_articles",
                    "source_id": int(row["id"]),
                    "provider": str(row["provider"] or ""),
                    "source": str(row["provider"] or ""),
                    "primary_symbol": primary,
                    "title": str(row["title"] or ""),
                    "published_at": str(row["published_at"] or ""),
                    "assets": _normalized_assets(assets),
                    "asset_mapping": mapping_payload,
                }
            )
        for row in con.execute(
            """
            SELECT id,source,title,published_at,sentiment_score,sentiment_label,assets_json
              FROM external_market_signals
             WHERE published_at>=?
               AND source IN ('marketaux','benzinga','emergency_breaking_news')
             ORDER BY published_at DESC,id DESC
             LIMIT ?
            """,
            (since, int(config.max_external_rows)),
        ):
            score = row["sentiment_score"]
            direction = "neutral"
            try:
                value = float(score)
                if value >= 0.15:
                    direction = "positive"
                elif value <= -0.15:
                    direction = "negative"
            except (TypeError, ValueError):
                value = 0.0
            rows.append(
                {
                    "source_table": "external_market_signals",
                    "source_id": int(row["id"]),
                    "provider": str(row["source"] or ""),
                    "source": str(row["source"] or ""),
                    "primary_symbol": "",
                    "title": str(row["title"] or ""),
                    "published_at": str(row["published_at"] or ""),
                    "assets": _normalized_assets(row["assets_json"]),
                    "external_sentiment_label": str(row["sentiment_label"] or direction),
                    "external_sentiment_score": value,
                }
            )
    finally:
        con.close()
    return rows


def build_cross_provider_event_roles(config: CrossProviderEventRoleConfig) -> dict[str, Any]:
    now_dt = _config_now(config)
    rows = _load_rows(config, now=now_dt)
    mesh = _read_json(config.mesh_json_path)
    temp_symbols = set((mesh.get("broadtape_temp_subscription_queue") or {}).get("symbols") or [])
    classified: list[dict[str, Any]] = []
    for row in rows:
        classification = classify_event_role(str(row.get("title") or ""), provider=str(row.get("provider") or ""), source=str(row.get("source_table") or ""))
        if row.get("source_table") == "external_market_signals" and row.get("external_sentiment_score") is not None:
            score = float(row.get("external_sentiment_score") or 0.0)
            classification["direction_score"] = round(score, 4)
            classification["direction"] = "positive" if score >= 0.15 else "negative" if score <= -0.15 else "neutral"
        item = {**row, **classification}
        quality_score, quality_flags = _item_quality(item)
        item["quality_score"] = quality_score
        item["quality_flags"] = quality_flags
        item["provider_family"] = _provider_family(str(item.get("provider") or ""), str(item.get("source") or ""))
        item["provider_weight"] = _provider_weight(str(item.get("provider") or ""), str(item.get("source") or ""))
        item["role_family"] = _role_family(str(item.get("event_role") or "unknown"))
        item["sentiment_role"] = _sentiment_role(item)
        item["ticker_inference"] = _ticker_inference(item, temp_symbols)
        item["cluster_key"] = _cluster_key(item, int(config.cluster_window_minutes), now=now_dt)
        item["is_temp_symbol_related"] = bool(temp_symbols.intersection(set(item.get("assets") or [])))
        classified.append(item)

    clusters: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in classified:
        clusters[item["cluster_key"]].append(item)

    cluster_summaries: list[dict[str, Any]] = []
    for key, items in clusters.items():
        providers = sorted({str(item.get("provider") or "") for item in items if item.get("provider")})
        provider_families = sorted({str(item.get("provider_family") or "") for item in items if item.get("provider_family")})
        provider_weights = [float((item.get("provider_weight") or {}).get("overall") or 0.0) for item in items]
        weighted_confirmation_score = sum(provider_weights) / len(provider_weights) if provider_weights else 0.0
        roles = Counter(str(item.get("event_role") or "unknown") for item in items)
        role_families = Counter(str(item.get("role_family") or "unknown") for item in items)
        sentiment_roles = Counter(str(item.get("sentiment_role") or "unknown") for item in items)
        directions = Counter(str(item.get("direction") or "neutral") for item in items)
        scores = [float(item.get("direction_score") or 0.0) for item in items]
        avg_score = sum(scores) / len(scores) if scores else 0.0
        score_spread = round((max(scores) - min(scores)) if scores else 0.0, 4)
        independent_provider_count = len(provider_families)
        has_opposing_direction = len({d for d in directions if d in {"positive", "negative"}}) > 1
        conflict = bool(has_opposing_direction and (independent_provider_count >= 2 or score_spread >= 0.4))
        conflict_score = 0.0
        if conflict:
            conflict_score = min(1.0, max(score_spread, (directions.get("positive", 0) + directions.get("negative", 0)) / max(1, len(items))))
        token_overlap = _max_pairwise_token_overlap(items)
        asset_counts = Counter(asset for item in items for asset in (item.get("assets") or []))
        shared_assets = sorted(asset for asset, count in asset_counts.items() if count >= 2)
        span_minutes = _time_span_minutes(items)
        quality_scores = [float(item.get("quality_score") or 0.0) for item in items]
        avg_quality = sum(quality_scores) / len(quality_scores) if quality_scores else 0.0
        ticker_confidences = [float((item.get("ticker_inference") or {}).get("confidence") or 0.0) for item in items]
        avg_ticker_confidence = sum(ticker_confidences) / len(ticker_confidences) if ticker_confidences else 0.0
        marketaux_items = [item for item in items if str(item.get("provider_family") or "") == "marketaux"]
        noisy_marketaux_count = sum(1 for item in marketaux_items if float(item.get("quality_score") or 0.0) <= 0.0)
        cluster_noise_count = sum(1 for item in items if "likely_non_us_or_feed_noise" in set(item.get("quality_flags") or []))
        confirmation_quality = "none"
        agreement = "single_provider"
        if conflict:
            agreement = "conflict"
            confirmation_quality = "conflict"
        elif (
            independent_provider_count >= 2
            and span_minutes <= 90
            and avg_quality >= 0.25
            and noisy_marketaux_count == 0
            and cluster_noise_count == 0
            and role_families.most_common(1)[0][0] != "context"
            and (token_overlap >= 2 or bool(shared_assets))
        ):
            agreement = "confirmed"
            confirmation_quality = "strong" if independent_provider_count >= 3 or token_overlap >= 4 else "medium"
        elif independent_provider_count >= 2:
            agreement = "weak_confirmation"
            confirmation_quality = "weak"
        elif len(marketaux_items) == len(items) and noisy_marketaux_count:
            agreement = "marketaux_noise_filtered"
            confirmation_quality = "filtered"
        elif any(item.get("source_table") == "external_market_signals" for item in items):
            agreement = "external_only_or_unconfirmed"
        role_family = role_families.most_common(1)[0][0] if role_families else "unknown"
        adaptive_cap = _adaptive_confidence_cap(
            agreement=agreement,
            confirmation_quality=confirmation_quality,
            role_family=role_family,
            provider_families=provider_families,
            avg_ticker_confidence=avg_ticker_confidence,
            conflict_score=conflict_score,
            average_quality=avg_quality,
        )
        confidence_adjustment = _confidence_adjustment(agreement, confirmation_quality, conflict_score, avg_quality, adaptive_cap)
        target_assets, target_asset_mapping = _cluster_target_assets(items, role_family, shared_assets[:20])
        canonical_event = _canonical_event(items, role_family, target_assets[:20])
        cluster_summaries.append(
            {
                "cluster_key": key,
                "canonical_event_id": canonical_event["canonical_event_id"],
                "canonical_event": canonical_event,
                "provider_count": len(providers),
                "providers": providers,
                "provider_family_count": independent_provider_count,
                "provider_families": provider_families,
                "provider_weighted_confirmation_score": round(weighted_confirmation_score, 4),
                "event_role": roles.most_common(1)[0][0] if roles else "unknown",
                "role_counts": dict(roles),
                "role_family": role_family,
                "role_family_counts": dict(role_families),
                "sentiment_role_counts": dict(sentiment_roles),
                "direction_counts": dict(directions),
                "average_direction_score": round(avg_score, 4),
                "score_spread": score_spread,
                "headline_token_overlap_max": token_overlap,
                "shared_assets": target_assets[:20],
                "raw_shared_assets": shared_assets[:20],
                "target_asset_mapping": target_asset_mapping,
                "cluster_time_span_minutes": span_minutes,
                "average_quality_score": round(avg_quality, 4),
                "average_ticker_inference_confidence": round(avg_ticker_confidence, 4),
                "marketaux_noise_filtered_count": noisy_marketaux_count,
                "cluster_noise_count": cluster_noise_count,
                "provider_agreement": agreement,
                "provider_disagreement": conflict,
                "confirmation_quality": confirmation_quality,
                "conflict_score": round(conflict_score, 4),
                "confidence_adjustment": confidence_adjustment,
                "adaptive_confidence_cap": adaptive_cap,
                "confidence_adjustment_cap": {
                    "positive_max": CONFIDENCE_ADJUSTMENT_POSITIVE_CAP,
                    "negative_min": CONFIDENCE_ADJUSTMENT_NEGATIVE_CAP,
                },
                "items": sorted(items, key=lambda item: str(item.get("published_at") or ""), reverse=True)[:8],
            }
        )
    cluster_summaries.sort(
        key=lambda item: (
            item["provider_family_count"],
            str(item["provider_agreement"]) == "confirmed",
            abs(float(item["average_direction_score"])),
            float(item.get("average_quality_score") or 0.0),
        ),
        reverse=True,
    )
    canonical_event_summaries = [
        {
            "canonical_event_id": cluster.get("canonical_event_id"),
            "canonical_event_key": (cluster.get("canonical_event") or {}).get("canonical_event_key"),
            "canonical_asset": (cluster.get("canonical_event") or {}).get("canonical_asset"),
            "provider_agreement": cluster.get("provider_agreement"),
            "confirmation_quality": cluster.get("confirmation_quality"),
            "role_family": cluster.get("role_family"),
            "confidence_adjustment": cluster.get("confidence_adjustment"),
            "provider_weighted_confirmation_score": cluster.get("provider_weighted_confirmation_score"),
            "average_ticker_inference_confidence": cluster.get("average_ticker_inference_confidence"),
        }
        for cluster in cluster_summaries[:50]
    ]
    payload = {
        "version": CROSS_PROVIDER_EVENT_ROLE_VERSION,
        "generated_at": now_dt.isoformat(),
        "generated_at_local": now_dt.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z (%z)"),
        "status": "ok",
        "config": {
            "lookback_minutes": int(config.lookback_minutes),
            "max_news_rows": int(config.max_news_rows),
            "max_external_rows": int(config.max_external_rows),
            "cluster_window_minutes": int(config.cluster_window_minutes),
        },
        "summary": {
            "classified_event_count": len(classified),
            "cluster_count": len(cluster_summaries),
            "provider_counts": dict(Counter(str(item.get("provider") or "") for item in classified)),
            "provider_family_counts": dict(Counter(str(item.get("provider_family") or "") for item in classified)),
            "sentiment_role_counts": dict(Counter(str(item.get("sentiment_role") or "") for item in classified)),
            "event_role_counts": dict(Counter(str(item.get("event_role") or "") for item in classified)),
            "role_family_counts": dict(Counter(str(item.get("role_family") or "") for item in classified)),
            "agreement_counts": dict(Counter(str(item.get("provider_agreement") or "") for item in cluster_summaries)),
            "confirmation_quality_counts": dict(Counter(str(item.get("confirmation_quality") or "") for item in cluster_summaries)),
        },
        "clusters": cluster_summaries[:100],
        "canonical_events": canonical_event_summaries,
        "scorer_evidence_packet_v3": {
            "packet_version": "scorer-evidence-cross-provider-v3",
            "top_clusters": cluster_summaries[:20],
            "canonical_events": canonical_event_summaries[:20],
            "use": (
                "Adjust scorer confidence by event-role, cross-provider confirmation/conflict, "
                "provider weights, inferred ticker confidence, external sentiment role, canonical event repetition, "
                "adaptive confidence caps, and whether a BroadTape temp symbol is involved."
            ),
            "confidence_adjustment_cap": {
                "positive_max": CONFIDENCE_ADJUSTMENT_POSITIVE_CAP,
                "negative_min": CONFIDENCE_ADJUSTMENT_NEGATIVE_CAP,
            },
            "external_sentiment_policy": {
                "marketaux": "sentiment_crosscheck_only_unless_confirmed_by_primary_provider",
                "alpha_vantage": "removed_from_active_training_inputs",
                "single_external_source": "no_directional_boost_without_primary_provider_or_shared_asset_confirmation",
            },
            "outcome_learning_policy": {
                "enabled": bool(config.write_outcome_log and config.outcome_log_jsonl_path),
                "horizons": list(DEFAULT_OUTCOME_HORIZONS),
                "leakage_policy": "M236 writes pending records only; future price outcomes must be attached by a later evaluator.",
            },
        },
        "safety": {
            "read_only_report": True,
            "broker_orders_submitted": False,
            "broker_subscriptions_mutated": False,
        },
    }
    if config.write_outcome_log and config.outcome_log_jsonl_path is not None:
        payload["outcome_learning_log"] = _append_outcome_learning_records(config.outcome_log_jsonl_path, payload)
    else:
        payload["outcome_learning_log"] = {
            "status": "disabled",
            "records_appended": 0,
            "horizons": list(DEFAULT_OUTCOME_HORIZONS),
        }
    return payload


def render_cross_provider_event_roles_markdown(payload: dict[str, Any]) -> str:
    summary = payload.get("summary") or {}
    lines = [
        "# M236 Cross-Provider Event Role Classifier",
        "",
        f"- Version: `{payload.get('version')}`",
        f"- Generated: `{payload.get('generated_at_local') or payload.get('generated_at')}`",
        f"- Status: `{payload.get('status')}`",
        f"- Classified events: `{summary.get('classified_event_count')}`",
        f"- Clusters: `{summary.get('cluster_count')}`",
        f"- Provider counts: `{summary.get('provider_counts') or {}}`",
        f"- Provider families: `{summary.get('provider_family_counts') or {}}`",
        f"- Sentiment roles: `{summary.get('sentiment_role_counts') or {}}`",
        f"- Event roles: `{summary.get('event_role_counts') or {}}`",
        f"- Role families: `{summary.get('role_family_counts') or {}}`",
        f"- Agreement: `{summary.get('agreement_counts') or {}}`",
        f"- Confirmation quality: `{summary.get('confirmation_quality_counts') or {}}`",
        f"- Outcome learning log: `{payload.get('outcome_learning_log') or {}}`",
        "",
        "## Top clusters",
        "",
    ]
    for cluster in (payload.get("clusters") or [])[:20]:
        first = (cluster.get("items") or [{}])[0]
        lines.append(
            f"- `{cluster.get('provider_agreement')}` `{cluster.get('event_role')}` "
            f"families `{cluster.get('provider_families')}` q `{cluster.get('confirmation_quality')}` "
            f"w `{cluster.get('provider_weighted_confirmation_score')}` ticker `{cluster.get('average_ticker_inference_confidence')}` "
            f"cap `{(cluster.get('adaptive_confidence_cap') or {}).get('reason')}` "
            f"conflict `{cluster.get('conflict_score')}` adj `{cluster.get('confidence_adjustment')}`: "
            f"{first.get('title')}"
        )
    lines.extend(
        [
            "",
            "## Safety",
            "",
            "- Read-only classifier artifact.",
            "- No broker orders or subscription mutation.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_cross_provider_event_roles_json(path: Path, payload: dict[str, Any]) -> Path:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return resolved


def write_cross_provider_event_roles_markdown(path: Path, payload: dict[str, Any]) -> Path:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(render_cross_provider_event_roles_markdown(payload), encoding="utf-8")
    return resolved
