from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable


_COMPANY_ALIASES: dict[str, tuple[str, ...]] = {
    "MU": ("micron", "micron technology"),
    "NVDA": ("nvidia",),
    "AVGO": ("broadcom",),
    "AMD": ("advanced micro devices",),
    "INTC": ("intel",),
    "TSM": ("tsmc", "taiwan semiconductor", "taiwan semiconductor manufacturing"),
    "AAPL": ("apple",),
    "MSFT": ("microsoft",),
    "META": ("meta platforms", "facebook"),
    "GOOGL": ("alphabet", "google"),
    "AMZN": ("amazon",),
    "QCOM": ("qualcomm",),
    "AMAT": ("applied materials",),
    "TSLA": ("tesla",),
    "NFLX": ("netflix",),
    "CRM": ("salesforce",),
    "ORCL": ("oracle",),
    "EOG": ("eog resources",),
    "XOM": ("exxon", "exxon mobil", "exxonmobil"),
    "CVX": ("chevron",),
    "TCOM": ("trip.com", "trip com", "trip.com group", "ctrip"),
    "CRWV": ("coreweave",),
    "NBIS": ("nebius",),
    "SMCI": ("super micro", "supermicro", "super micro computer"),
    "PLTR": ("palantir",),
    "ARM": ("arm holdings",),
    "JPM": ("jpmorgan", "jp morgan"),
    "BAC": ("bank of america",),
    "GS": ("goldman sachs",),
    "MS": ("morgan stanley",),
}

_KNOWN_DIRECT_TICKERS = frozenset(
    {
        *_COMPANY_ALIASES.keys(),
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
        "ITA",
        "XAR",
        "EWY",
        "VIX",
        "VXX",
        "UUP",
        "WEAT",
        "DBA",
        "CIBR",
        "HACK",
    }
)

_FALSE_TICKERS = frozenset(
    {
        "A",
        "AI",
        "API",
        "BUY",
        "CEO",
        "CFO",
        "CPI",
        "DJ",
        "EPS",
        "ETF",
        "FED",
        "FOMC",
        "GDP",
        "IPO",
        "NASDAQ",
        "NYSE",
        "OPEC",
        "PMI",
        "SEC",
        "USA",
        "US",
        "WSJ",
    }
)

_THEME_RULES: tuple[tuple[str, re.Pattern[str], tuple[str, ...], str], ...] = (
    (
        "semiconductor_ai",
        re.compile(r"\b(?:semiconductor|chip|chips|ai chip|hbm|gpu|nvidia|micron|broadcom|tsmc|super micro|coreweave|nebius)\b", re.I),
        ("QQQ", "SOXX", "SMH", "NVDA", "MU", "AVGO", "TSM"),
        "AI/semiconductor wording maps to QQQ/SOXX/SMH and key chip names",
    ),
    (
        "energy_oil",
        re.compile(r"\b(?:oil|crude|wti|brent|eog resources|exxon|chevron|opec|gasoline|distillates|inventory|hormuz|red sea)\b", re.I),
        ("XLE", "USO", "XOM", "CVX", "EOG", "SPY"),
        "Energy/oil wording maps to XLE/USO plus energy equities",
    ),
    (
        "rates_macro",
        re.compile(r"\b(?:fed|federal reserve|rate|rates|treasury|bond|inflation|cpi|pce|payroll|jobs|unemployment|yield)\b", re.I),
        ("SPY", "QQQ", "TLT", "UUP", "GLD"),
        "Macro/rates wording maps to broad equity, duration, dollar, and gold proxies",
    ),
    (
        "broad_market",
        re.compile(r"\b(?:stock market|dow jones|nasdaq|s&p 500|futures|wall street|market today|market recap)\b", re.I),
        ("SPY", "QQQ", "IWM", "DIA", "TLT"),
        "Broad market wording maps to index ETF proxies",
    ),
    (
        "taiwan_china",
        re.compile(r"\b(?:taiwan|china|beijing|export control|export controls|south china sea|trip\.com|ctrip)\b", re.I),
        ("QQQ", "TSM", "SOXX", "SMH", "NVDA", "TCOM"),
        "Taiwan/China wording maps to QQQ, chip supply chain, and named China ADRs",
    ),
    (
        "middle_east",
        re.compile(r"\b(?:iran|israel|middle east|red sea|hormuz|houthi|gaza|lebanon|ceasefire|missile|airstrike)\b", re.I),
        ("USO", "XLE", "GLD", "VIX", "SPY"),
        "Middle East/geopolitical wording maps to oil, gold, volatility, and broad risk",
    ),
    (
        "defense",
        re.compile(r"\b(?:defense|aerospace|weapons|drone|military contractor|military spending)\b", re.I),
        ("ITA", "XAR", "SPY"),
        "Defense wording maps to defense ETFs and broad risk",
    ),
)

_PREFERRED_PROXY_ORDER = (
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
    "EWY",
    "ITA",
    "XAR",
)


@dataclass(frozen=True)
class NewsAssetMapping:
    inferred_symbols: tuple[str, ...]
    inferred_assets: tuple[str, ...]
    target_candidates: tuple[str, ...]
    confidence: str
    reason: str
    source_mode: str
    themes: tuple[str, ...]
    context_assets: tuple[str, ...] = ()
    tradable_candidate: bool = True
    fallback_allowed_for_context: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "inferred_symbols": list(self.inferred_symbols),
            "inferred_assets": list(self.inferred_assets),
            "target_candidates": list(self.target_candidates),
            "confidence": self.confidence,
            "reason": self.reason,
            "source_mode": self.source_mode,
            "themes": list(self.themes),
            "context_assets": list(self.context_assets),
            "tradable_candidate": self.tradable_candidate,
            "fallback_allowed_for_context": self.fallback_allowed_for_context,
        }


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    seen: list[str] = []
    for value in values:
        item = str(value or "").strip().upper()
        if item and item not in seen and item not in _FALSE_TICKERS:
            seen.append(item)
    return tuple(seen)


def _direct_symbols(text: str) -> tuple[str, ...]:
    candidates: list[str] = []
    for match in re.finditer(r"(?<![A-Z0-9])\$?([A-Z]{1,5})(?![A-Z0-9])", text):
        symbol = match.group(1).upper()
        if symbol in _KNOWN_DIRECT_TICKERS:
            candidates.append(symbol)
    return _unique(candidates)


def _alias_symbols(text: str) -> tuple[str, ...]:
    found: list[str] = []
    for symbol, aliases in _COMPANY_ALIASES.items():
        for alias in aliases:
            if re.search(rf"(?<![A-Z0-9]){re.escape(alias)}(?![A-Z0-9])", text, re.I):
                found.append(symbol)
                break
    return _unique(found)


def _theme_assets(text: str) -> tuple[tuple[str, tuple[str, ...], str], ...]:
    matched: list[tuple[str, tuple[str, ...], str]] = []
    for theme, pattern, assets, reason in _THEME_RULES:
        if pattern.search(text):
            matched.append((theme, assets, reason))
    return tuple(matched)


def _parse_existing_mapping(extra_data: str | dict[str, Any] | None) -> NewsAssetMapping | None:
    if not extra_data:
        return None
    payload: Any = extra_data
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            return None
    if not isinstance(payload, dict):
        return None
    mapping = payload.get("asset_mapping") or payload.get("inferred_asset_mapping")
    if not isinstance(mapping, dict):
        return None
    return NewsAssetMapping(
        inferred_symbols=_unique(mapping.get("inferred_symbols") or ()),
        inferred_assets=_unique(mapping.get("inferred_assets") or ()),
        target_candidates=_unique(mapping.get("target_candidates") or ()),
        confidence=str(mapping.get("confidence") or "unknown"),
        reason=str(mapping.get("reason") or "stored inferred asset mapping"),
        source_mode=str(mapping.get("source_mode") or payload.get("source_mode") or "stored_mapping"),
        themes=_unique(mapping.get("themes") or ()),
        context_assets=_unique(mapping.get("context_assets") or ()),
        tradable_candidate=bool(mapping.get("tradable_candidate", True)),
        fallback_allowed_for_context=bool(mapping.get("fallback_allowed_for_context", False)),
    )


def infer_news_assets(
    headline: str,
    body: str = "",
    *,
    primary_symbol: str = "",
    provider: str = "",
    extra_data: str | dict[str, Any] | None = None,
) -> NewsAssetMapping:
    stored = _parse_existing_mapping(extra_data)
    if stored is not None and (stored.target_candidates or stored.inferred_symbols or stored.inferred_assets):
        return stored

    source_mode = "broadtape" if str(primary_symbol).upper().startswith("BROADTAPE:") else "ticker_specific"
    text = f"{headline or ''}\n{body or ''}"[:20000]
    direct = _direct_symbols(text)
    aliases = _alias_symbols(text)
    symbols = _unique((*direct, *aliases))
    theme_matches = _theme_assets(text)
    themes = _unique(theme for theme, _, _ in theme_matches)
    theme_asset_values: list[str] = []
    theme_reasons: list[str] = []
    for _, assets, reason in theme_matches:
        theme_asset_values.extend(assets)
        theme_reasons.append(reason)

    primary = str(primary_symbol or "").strip().upper()
    assets = _unique((*theme_asset_values, *(s for s in symbols if s in _KNOWN_DIRECT_TICKERS)))
    if primary and not primary.startswith("BROADTAPE:"):
        symbols = _unique((primary, *symbols))
        assets = _unique((primary, *assets))

    preferred_proxy_set = set(_PREFERRED_PROXY_ORDER)
    proxy_candidates = [symbol for symbol in assets if symbol in preferred_proxy_set]
    company_candidates = [symbol for symbol in symbols if symbol not in proxy_candidates]
    target_candidates = _unique((*proxy_candidates, *company_candidates, *assets))

    if direct:
        confidence = "high"
        reason = f"direct ticker token(s) detected: {','.join(direct)}"
    elif aliases and theme_matches:
        confidence = "high"
        reason = f"company alias plus theme mapping: {','.join(aliases)}; {'; '.join(theme_reasons[:2])}"
    elif aliases:
        confidence = "medium"
        reason = f"company alias detected: {','.join(aliases)}"
    elif theme_matches:
        confidence = "medium"
        reason = "; ".join(theme_reasons[:2])
    elif source_mode == "broadtape":
        confidence = "low"
        reason = (
            f"{provider or 'provider'} BroadTape headline with no direct ticker/theme match; "
            "SPY retained as context-only fallback"
        )
        target_candidates = ()
        assets = ()
        context_assets = _unique(("SPY",))
        tradable_candidate = False
        fallback_allowed_for_context = True
    else:
        confidence = "high" if primary else "low"
        reason = f"ticker-specific route maps to {primary}" if primary else "no symbol or theme match"
        context_assets = ()
        tradable_candidate = bool(primary)
        fallback_allowed_for_context = False

    if source_mode != "broadtape" or theme_matches or direct or aliases:
        context_assets = ()
        tradable_candidate = True
        fallback_allowed_for_context = False

    return NewsAssetMapping(
        inferred_symbols=symbols,
        inferred_assets=assets,
        target_candidates=target_candidates,
        confidence=confidence,
        reason=reason,
        source_mode=source_mode,
        themes=themes,
        context_assets=context_assets,
        tradable_candidate=tradable_candidate,
        fallback_allowed_for_context=fallback_allowed_for_context,
    )


def preferred_trading_symbol(mapping: NewsAssetMapping, *, fallback: str = "SPY") -> tuple[str, str, str]:
    for symbol in mapping.target_candidates:
        if symbol in _PREFERRED_PROXY_ORDER:
            return symbol, mapping.confidence, "broadtape_asset_mapper"
    for symbol in mapping.target_candidates:
        return symbol, mapping.confidence, "broadtape_asset_mapper"
    return fallback, "low", "default_market_proxy"
