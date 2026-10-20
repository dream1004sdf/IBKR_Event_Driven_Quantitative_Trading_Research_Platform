from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .news_asset_mapper import infer_news_assets


NEWS_SUBSCRIPTION_MESH_VERSION = "m235-news-subscription-mesh-v1"

_CONFIDENCE_RANK = {"low": 1, "medium": 2, "high": 3}
_TEMP_EXCLUDED_PROXIES = {
    "SPY",
    "QQQ",
    "IWM",
    "DIA",
    "TLT",
    "GLD",
    "USO",
    "XLE",
    "SOXX",
    "SMH",
    "VIX",
    "VXX",
    "UUP",
    "EWY",
    "ITA",
    "XAR",
    "DBA",
    "WEAT",
}
_BAD_TEMP_SYMBOLS = {
    "A",
    "AI",
    "API",
    "CEO",
    "CFO",
    "CPI",
    "ETF",
    "FED",
    "FOMC",
    "GDP",
    "IPO",
    "PMI",
    "SEC",
    "USA",
    "US",
    "WSJ",
}


@dataclass(frozen=True)
class NewsSubscriptionMeshConfig:
    database_path: Path = Path("market_events.db")
    universe_json_path: Path = Path("config/news_subscription_mesh_universe_v1.json")
    lookback_minutes: int = 240
    temp_ttl_minutes: int = 180
    max_temp_symbols: int = 40
    max_core_live_symbols: int = 175
    min_temp_confidence: str = "medium"
    include_dow_jones_geopolitical_candidates: bool = True
    dow_jones_geopolitical_lookback_minutes: int = 720


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


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


def _unique_symbols(values: list[str] | tuple[str, ...]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        symbol = _normalize_symbol(value)
        if symbol and symbol not in seen:
            seen.add(symbol)
            out.append(symbol)
    return out


def _normalize_symbol(value: Any) -> str:
    symbol = str(value or "").strip().upper()
    if not symbol:
        return ""
    # IBKR commonly uses spaces for share classes, while many public lists use dots.
    symbol = symbol.replace(".", " ")
    return symbol


def _is_temp_symbol_candidate(symbol: str) -> bool:
    symbol = _normalize_symbol(symbol)
    if not symbol or symbol in _TEMP_EXCLUDED_PROXIES or symbol in _BAD_TEMP_SYMBOLS:
        return False
    if len(symbol.replace(" ", "")) > 6:
        return False
    return bool(re.fullmatch(r"[A-Z][A-Z0-9 ]{0,5}", symbol))


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.expanduser().read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _core_universe(universe: dict[str, Any]) -> dict[str, list[str]]:
    keys = ("core_anchors", "dow30", "nasdaq100_seed", "sp500_seed")
    return {key: _unique_symbols(tuple(universe.get(key) or ())) for key in keys}


def _core_symbol_set(core: dict[str, list[str]]) -> set[str]:
    symbols: set[str] = set()
    for values in core.values():
        symbols.update(values)
    return symbols


def _fx_macro_indicators(universe: dict[str, Any]) -> list[str]:
    return _unique_symbols(tuple(universe.get("fx_macro_market_indicators") or ()))


def _table_columns(con: sqlite3.Connection, table: str) -> set[str]:
    try:
        rows = con.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error:
        return set()
    return {str(row[1]) for row in rows}


def _fetch_recent_news(
    con: sqlite3.Connection,
    *,
    where_sql: str,
    params: tuple[Any, ...],
    limit: int,
) -> list[dict[str, Any]]:
    cols = _table_columns(con, "news_articles")
    body_expr = "body" if "body" in cols else "'' AS body"
    extra_expr = "extra_data" if "extra_data" in cols else "'' AS extra_data"
    provider_published_expr = "provider_published_at" if "provider_published_at" in cols else "NULL AS provider_published_at"
    sql = f"""
        SELECT id, provider, primary_symbol, headline, {body_expr}, published_at,
               received_at, {provider_published_expr}, {extra_expr}
          FROM news_articles
         WHERE {where_sql}
         ORDER BY COALESCE(provider_published_at, published_at, received_at) DESC, id DESC
         LIMIT ?
    """
    rows = con.execute(sql, (*params, int(limit))).fetchall()
    names = [item[0] for item in con.execute(sql, (*params, 0)).description]
    return [dict(zip(names, row)) for row in rows]


_GEOPOLITICAL_RULES: tuple[dict[str, Any], ...] = (
    {
        "theme": "middle_east_energy_risk",
        "region": "Middle East",
        "pattern": re.compile(r"\b(iran|israel|gaza|houthi|houthis|red sea|hormuz|hamas|hezbollah|saudi|qatar|iraq|syria|lebanon)\b", re.I),
        "assets": ("USO", "XLE", "XOM", "CVX", "GLD", "VIX", "SPY"),
    },
    {
        "theme": "taiwan_china_supply_chain_risk",
        "region": "Taiwan/China",
        "pattern": re.compile(r"\b(taiwan|beijing|pla|south china sea|export control|rare earth|chip ban|semiconductor restriction)\b", re.I),
        "assets": ("TSM", "SMH", "SOXX", "NVDA", "QQQ", "GLD"),
    },
    {
        "theme": "korea_peninsula_risk",
        "region": "Korea Peninsula",
        "pattern": re.compile(r"\b(north korea|pyongyang|korea peninsula|kim jong|missile launch)\b", re.I),
        "assets": ("EWY", "KRW", "SOXX", "QQQ", "GLD"),
    },
    {
        "theme": "russia_ukraine_risk",
        "region": "Russia/Ukraine",
        "pattern": re.compile(r"\b(russia|ukraine|moscow|kyiv|nato|kremlin|zelensky|putin)\b", re.I),
        "assets": ("WEAT", "DBA", "USO", "XLE", "ITA", "XAR", "GLD", "VIX"),
    },
    {
        "theme": "tariff_trade_war_risk",
        "region": "Global Trade",
        "pattern": re.compile(r"\b(tariff|tariffs|sanction|sanctions|trade war|export ban|import duty|customs duty)\b", re.I),
        "assets": ("SPY", "QQQ", "DIA", "IWM", "UUP", "GLD"),
    },
)

_CHINA_TOKEN_PATTERN = re.compile(r"\b(china|chinese)\b", re.I)
_CHINA_CONTEXT_PATTERN = re.compile(
    r"\b(tariff|tariffs|sanction|sanctions|export|restriction|restrictions|ban|bans|"
    r"military|war|trade war|supply chain|semiconductor|chip|probe|security|rare earth)\b",
    re.I,
)


def classify_geopolitical_headline(headline: str, body: str = "") -> dict[str, Any] | None:
    text = f"{headline or ''}\n{body or ''}"[:12000]
    matches: list[dict[str, Any]] = []
    for rule in _GEOPOLITICAL_RULES:
        if rule["pattern"].search(text):
            matches.append(
                {
                    "theme": rule["theme"],
                    "region": rule["region"],
                    "assets": list(rule["assets"]),
                }
            )
    if _CHINA_TOKEN_PATTERN.search(text) and _CHINA_CONTEXT_PATTERN.search(text):
        matches.append(
            {
                "theme": "taiwan_china_supply_chain_risk",
                "region": "Taiwan/China",
                "assets": ["TSM", "SMH", "SOXX", "NVDA", "QQQ", "GLD"],
            }
        )
    if not matches:
        return None
    assets: list[str] = []
    regions: list[str] = []
    themes: list[str] = []
    for match in matches:
        themes.append(str(match["theme"]))
        regions.append(str(match["region"]))
        assets.extend(str(item) for item in match["assets"])
    return {
        "classification_version": "dow-jones-geopolitical-keywords-v1",
        "themes": _unique_symbols(tuple(themes)),
        "regions": list(dict.fromkeys(regions)),
        "assets": _unique_symbols(tuple(assets)),
        "confidence": "medium" if len(matches) == 1 else "high",
        "reason": "Dow Jones headline/body matched geopolitical region/theme keyword rules.",
    }


def _build_core_live_symbols(core: dict[str, list[str]], max_core_live_symbols: int) -> list[str]:
    anchors = core.get("core_anchors") or []
    dow = core.get("dow30") or []
    nasdaq = core.get("nasdaq100_seed") or []
    sp500 = core.get("sp500_seed") or []
    ordered = _unique_symbols(tuple([*anchors, *dow, *nasdaq, *sp500]))
    if max_core_live_symbols <= 0:
        return ordered
    return ordered[: int(max_core_live_symbols)]


def build_news_subscription_mesh(
    config: NewsSubscriptionMeshConfig,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    now_dt = (now or datetime.now(UTC)).astimezone(UTC)
    universe = _read_json(config.universe_json_path)
    core = _core_universe(universe)
    core_set = _core_symbol_set(core)
    fx_macro = _fx_macro_indicators(universe)
    min_conf_rank = _CONFIDENCE_RANK.get(str(config.min_temp_confidence).lower(), 2)
    broad_since = (now_dt - timedelta(minutes=int(config.lookback_minutes))).isoformat()
    dj_since = (now_dt - timedelta(minutes=int(config.dow_jones_geopolitical_lookback_minutes))).isoformat()

    temp_by_symbol: dict[str, dict[str, Any]] = {}
    broadtape_rows: list[dict[str, Any]] = []
    dj_geo_candidates: list[dict[str, Any]] = []
    provider_counts: Counter[str] = Counter()
    confidence_counts: Counter[str] = Counter()

    con = sqlite3.connect(str(config.database_path), timeout=30)
    con.row_factory = sqlite3.Row
    try:
        broadtape_rows = _fetch_recent_news(
            con,
            where_sql="primary_symbol LIKE 'BROADTAPE:%' AND COALESCE(provider_published_at, published_at, received_at) >= ?",
            params=(broad_since,),
            limit=2000,
        )
        for row in broadtape_rows:
            provider = str(row.get("provider") or "")
            provider_counts[provider] += 1
            mapping = infer_news_assets(
                str(row.get("headline") or ""),
                str(row.get("body") or ""),
                primary_symbol=str(row.get("primary_symbol") or ""),
                provider=provider,
                extra_data=row.get("extra_data"),
            )
            confidence = str(mapping.confidence or "low").lower()
            confidence_counts[confidence] += 1
            if _CONFIDENCE_RANK.get(confidence, 0) < min_conf_rank:
                continue
            for symbol in mapping.target_candidates:
                normalized = _normalize_symbol(symbol)
                if normalized in core_set:
                    continue
                if not _is_temp_symbol_candidate(normalized):
                    continue
                observed_at = str(row.get("provider_published_at") or row.get("published_at") or row.get("received_at") or "")
                observed_dt = _parse_dt(observed_at) or now_dt
                expires_at = observed_dt + timedelta(minutes=int(config.temp_ttl_minutes))
                existing = temp_by_symbol.get(normalized)
                candidate = {
                    "symbol": normalized,
                    "source": "broadtape_inferred_ticker",
                    "subscription_source": "broadtape_temp",
                    "provider": provider,
                    "news_id": row.get("id"),
                    "headline": row.get("headline"),
                    "observed_at": observed_dt.isoformat(),
                    "expires_at": expires_at.isoformat(),
                    "confidence": confidence,
                    "reason": mapping.reason,
                    "themes": list(mapping.themes),
                    "confirmation_providers": ["DJ-N", "DJ-RT"],
                    "confirmation_mode": "temporary_ticker_specific_news_probe",
                }
                if existing is None or str(existing.get("observed_at") or "") < candidate["observed_at"]:
                    temp_by_symbol[normalized] = candidate

        if config.include_dow_jones_geopolitical_candidates:
            dj_rows = _fetch_recent_news(
                con,
                where_sql="upper(provider) LIKE 'DJ%' AND COALESCE(provider_published_at, published_at, received_at) >= ?",
                params=(dj_since,),
                limit=2000,
            )
            for row in dj_rows:
                classification = classify_geopolitical_headline(str(row.get("headline") or ""), str(row.get("body") or ""))
                if not classification:
                    continue
                dj_geo_candidates.append(
                    {
                        "provider": row.get("provider"),
                        "primary_symbol": row.get("primary_symbol"),
                        "news_id": row.get("id"),
                        "headline": row.get("headline"),
                        "observed_at": row.get("provider_published_at") or row.get("published_at") or row.get("received_at"),
                        "geopolitical": classification,
                        "subscription_source": "dow_jones_geopolitical_confirmation",
                    }
                )
    finally:
        con.close()

    temp_candidates = sorted(
        temp_by_symbol.values(),
        key=lambda item: (
            _CONFIDENCE_RANK.get(str(item.get("confidence") or "low"), 0),
            str(item.get("observed_at") or ""),
        ),
        reverse=True,
    )[: int(config.max_temp_symbols)]
    temp_symbols = [str(item["symbol"]) for item in temp_candidates]
    core_live_symbols = _build_core_live_symbols(core, int(config.max_core_live_symbols))
    effective_symbols = _unique_symbols(tuple([*core_live_symbols, *temp_symbols]))

    return {
        "version": NEWS_SUBSCRIPTION_MESH_VERSION,
        "generated_at": _iso_now(),
        "generated_at_local": datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z (%z)"),
        "status": "ok",
        "config": {
            "lookback_minutes": int(config.lookback_minutes),
            "temp_ttl_minutes": int(config.temp_ttl_minutes),
            "max_temp_symbols": int(config.max_temp_symbols),
            "max_core_live_symbols": int(config.max_core_live_symbols),
            "min_temp_confidence": str(config.min_temp_confidence),
            "include_dow_jones_geopolitical_candidates": bool(config.include_dow_jones_geopolitical_candidates),
            "dow_jones_geopolitical_lookback_minutes": int(config.dow_jones_geopolitical_lookback_minutes),
        },
        "core_universe": {
            "counts": {key: len(value) for key, value in core.items()},
            "core_anchors": core.get("core_anchors") or [],
            "fx_macro_market_indicators": fx_macro,
            "core_live_symbols": core_live_symbols,
            "eligible_symbol_count": len(core_set),
            "live_budget_symbol_count": len(core_live_symbols),
            "budget_policy": "eligible universe is broad; live ticker-specific subscriptions are capped/adaptive to avoid IBKR line and pacing pressure",
        },
        "broadtape_temp_subscription_queue": {
            "candidate_count": len(temp_candidates),
            "symbols": temp_symbols,
            "candidates": temp_candidates,
            "provider_counts_in_lookback": dict(provider_counts),
            "mapping_confidence_counts": dict(confidence_counts),
        },
        "dow_jones_geopolitical_candidates": {
            "candidate_count": len(dj_geo_candidates),
            "candidates": dj_geo_candidates[:100],
        },
        "effective_news_subscription_symbols": effective_symbols,
        "effective_news_subscription_symbol_count": len(effective_symbols),
        "safety": {
            "read_only_planner": True,
            "broker_orders_submitted": False,
            "broker_subscriptions_mutated": False,
            "intended_next_step": "recorder/live supervisor can consume effective symbols after budget and pacing guards",
        },
    }


def render_news_subscription_mesh_markdown(payload: dict[str, Any]) -> str:
    core = payload.get("core_universe") or {}
    temp = payload.get("broadtape_temp_subscription_queue") or {}
    geo = payload.get("dow_jones_geopolitical_candidates") or {}
    lines = [
        "# M235 News Subscription Mesh",
        "",
        f"- Version: `{payload.get('version')}`",
        f"- Generated: `{payload.get('generated_at_local') or payload.get('generated_at')}`",
        f"- Status: `{payload.get('status')}`",
        f"- Effective news symbols: `{payload.get('effective_news_subscription_symbol_count')}`",
        "",
        "## Core universe",
        "",
        f"- Counts: `{core.get('counts') or {}}`",
        f"- Eligible symbols: `{core.get('eligible_symbol_count')}`",
        f"- Live budget symbols: `{core.get('live_budget_symbol_count')}`",
        f"- Budget policy: {core.get('budget_policy')}",
        f"- Core anchors: `{core.get('core_anchors') or []}`",
        f"- FX/macro indicators: `{core.get('fx_macro_market_indicators') or []}`",
        "",
        "## BroadTape temporary subscription queue",
        "",
        f"- Candidate count: `{temp.get('candidate_count')}`",
        f"- Symbols: `{temp.get('symbols') or []}`",
        f"- Provider counts: `{temp.get('provider_counts_in_lookback') or {}}`",
        f"- Mapping confidence counts: `{temp.get('mapping_confidence_counts') or {}}`",
        "",
    ]
    for item in (temp.get("candidates") or [])[:20]:
        lines.append(
            f"- `{item.get('symbol')}` `{item.get('confidence')}` expires `{item.get('expires_at')}` "
            f"via `{item.get('provider')}`: {item.get('headline')}"
        )
    lines.extend(
        [
            "",
            "## Dow Jones geopolitical candidates",
            "",
            f"- Candidate count: `{geo.get('candidate_count')}`",
            "",
        ]
    )
    for item in (geo.get("candidates") or [])[:20]:
        classification = item.get("geopolitical") or {}
        lines.append(
            f"- `{item.get('provider')}/{item.get('primary_symbol')}` "
            f"{classification.get('regions') or []} {classification.get('themes') or []} "
            f"assets `{classification.get('assets') or []}`: {item.get('headline')}"
        )
    lines.extend(
        [
            "",
            "## Safety",
            "",
            "- Read-only planner only.",
            "- No broker order submitted.",
            "- No live broker subscription mutated by this command.",
        ]
    )
    return "\n".join(lines) + "\n"


def marketaux_expanded_symbols_from_mesh(
    mesh_payload: dict[str, Any],
    *,
    max_symbols: int = 100,
) -> list[str]:
    """Return a Dow-Jones-like Marketaux query universe from the M235 artifact.

    Marketaux has request/plan limits, so this returns an ordered, capped list:
    core anchors first, then BroadTape temp symbols, then the live core budget.
    """

    core = mesh_payload.get("core_universe") or {}
    temp = mesh_payload.get("broadtape_temp_subscription_queue") or {}
    ordered = _unique_symbols(
        tuple(
            [
                *(core.get("core_anchors") or []),
                *(temp.get("symbols") or []),
                *(core.get("core_live_symbols") or []),
                *(mesh_payload.get("effective_news_subscription_symbols") or []),
            ]
        )
    )
    if int(max_symbols) > 0:
        return ordered[: int(max_symbols)]
    return ordered


def write_news_subscription_mesh_json(path: Path, payload: dict[str, Any]) -> Path:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return resolved


def write_news_subscription_mesh_markdown(path: Path, payload: dict[str, Any]) -> Path:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(render_news_subscription_mesh_markdown(payload), encoding="utf-8")
    return resolved
