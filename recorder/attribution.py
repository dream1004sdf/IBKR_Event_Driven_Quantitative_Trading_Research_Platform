from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

# Deterministic company registry.  It intentionally extends beyond the subscribed
# universe so that an article delivered through NVDA can still be identified as BB,
# AMAT, WEN, or another actual subject and blocked from contaminating NVDA data.
_SYMBOL_ALIASES: dict[str, tuple[str, ...]] = {
    "MU": ("micron", "micron technology"),
    "NVDA": ("nvidia",),
    "AVGO": ("broadcom",),
    "AMD": ("advanced micro devices",),
    "INTC": ("intel", "intel corp", "intel corporation"),
    "TSM": ("tsmc", "taiwan semiconductor", "taiwan semiconductor manufacturing"),
    "AAPL": ("apple", "apple inc"),
    "MSFT": ("microsoft",),
    "META": ("meta platforms", "facebook"),
    "GOOGL": ("alphabet", "google"),
    "AMZN": ("amazon",),
    "QCOM": ("qualcomm",),
    "AMAT": ("applied materials", "applied materials inc"),
    "BB": ("blackberry", "blackberry ltd"),
    "WEN": ("wendy's", "wendys", "the wendy's company"),
    "SNDK": ("sandisk", "san disk"),
    "WDC": ("western digital",),
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
}

_COMPANY_NAMES: dict[str, str] = {
    "MU": "Micron Technology",
    "NVDA": "Nvidia",
    "AVGO": "Broadcom",
    "AMD": "Advanced Micro Devices",
    "INTC": "Intel",
    "TSM": "Taiwan Semiconductor Manufacturing",
    "AAPL": "Apple",
    "MSFT": "Microsoft",
    "META": "Meta Platforms",
    "GOOGL": "Alphabet",
    "AMZN": "Amazon",
    "QCOM": "Qualcomm",
    "AMAT": "Applied Materials",
    "BB": "BlackBerry",
    "WEN": "Wendy's",
    "SNDK": "SanDisk",
    "WDC": "Western Digital",
    "TSLA": "Tesla",
    "NFLX": "Netflix",
    "CRM": "Salesforce",
    "ORCL": "Oracle",
    "EOG": "EOG Resources",
    "XOM": "Exxon Mobil",
    "CVX": "Chevron",
    "TCOM": "Trip.com Group",
    "CRWV": "CoreWeave",
    "NBIS": "Nebius",
    "SMCI": "Super Micro Computer",
}

ATTRIBUTION_ENGINE_VERSION = "v0.14.0"
ATTRIBUTION_METHOD = "deterministic_headline_body_rules"

_ASSET_METADATA: dict[str, tuple[str, str, str]] = {
    # symbol: (asset_type, natural_scope, subject)
    "SPY": ("etf", "market", "us_equities"),
    "QQQ": ("etf", "market", "nasdaq_100"),
    "IWM": ("etf", "market", "us_small_caps"),
    "SOXX": ("etf", "sector", "semiconductors"),
    "SMH": ("etf", "sector", "semiconductors"),
    "TLT": ("etf", "macro", "long_duration_treasuries"),
    "VIX": ("index", "market", "equity_volatility"),
}

# Specific macro releases take precedence.  FOMC is deliberately narrow: a generic
# reference to the Federal Reserve or an inflation gauge is not an FOMC event.
_SPECIFIC_MACRO_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("pce", re.compile(r"\b(?:core\s+)?pce\b|personal consumption expenditures|personal income and outlays|fed(?:eral reserve)?['’]s preferred inflation (?:gauge|measure)", re.I)),
    ("cpi", re.compile(r"\b(?:core\s+)?cpi\b|consumer price index", re.I)),
    ("employment", re.compile(r"\b(?:nonfarm payrolls?|payrolls?|jobs report|employment report|unemployment rate|initial jobless claims)\b", re.I)),
    ("gdp", re.compile(r"\b(?:gross domestic product|gdp)\b", re.I)),
    ("retail_sales", re.compile(r"\bretail sales\b", re.I)),
    ("ism", re.compile(r"\b(?:ism|pmi|purchasing managers? index)\b", re.I)),
    (
        "fomc",
        re.compile(
            r"\b(?:fomc|federal open market committee|fed(?:eral reserve)?\s+(?:rate\s+)?decision|"
            r"fed(?:eral reserve)?\s+policy statement|fed(?:eral reserve)?\s+meeting|"
            r"dot plot|summary of economic projections|\bsep\b|fed funds target|"
            r"powell (?:press conference|speaks after the decision)|fomc minutes|fed meeting minutes)\b",
            re.I,
        ),
    ),
)

_GENERIC_MACRO_CONTEXT = re.compile(
    r"\b(?:federal reserve|\bfed\b|inflation gauge|inflation measure|rate expectations?|"
    r"treasury yields?|interest-rate outlook)\b",
    re.I,
)

_GEOPOLITICAL_CONTEXT = re.compile(
    r"\b(?:geopolitical|war|missile|airstrike|attack|invasion|ceasefire|"
    r"military escalation|middle east|red sea|taiwan|strait of hormuz|"
    r"ukraine|russia|israel|iran|china tensions?|north korea|"
    r"sanctions?|export controls?|tariffs?|trade war|shipping chokepoint|"
    r"cyberattack|energy supply disruption|oil supply)\b",
    re.I,
)

_GEOPOLITICAL_SECTOR_PREFERENCES: tuple[tuple[re.Pattern[str], tuple[str, ...], str], ...] = (
    (
        re.compile(r"\b(?:taiwan|semiconductor|chip makers?|chips?|export controls?|advanced chips?|hbm|ai chips?)\b", re.I),
        ("SOXX", "QQQ", "SPY", "SMH"),
        "semiconductors",
    ),
    (
        re.compile(r"\b(?:oil|crude|brent|wti|hormuz|red sea|tanker|energy|opec|shipping chokepoint)\b", re.I),
        ("SPY", "IWM", "TLT", "QQQ"),
        "energy_supply",
    ),
    (
        re.compile(r"\b(?:cyberattack|cyber attack|hack|ransomware)\b", re.I),
        ("QQQ", "SPY"),
        "technology_cybersecurity",
    ),
)

_SECTOR_PATTERNS: tuple[tuple[str, re.Pattern[str], tuple[str, ...]], ...] = (
    ("semiconductors", re.compile(r"\b(?:semiconductor|chip makers?|chip stocks?|memory chips?|dram|nand|hbm)\b", re.I), ("SOXX", "QQQ", "SPY")),
    ("technology", re.compile(r"\b(?:technology stocks?|tech stocks?|nasdaq)\b", re.I), ("QQQ", "SPY")),
)

_MARKET_HEADLINE_RE = re.compile(
    r"^(?:dow(?: jones)?|s&p(?: 500)?|nasdaq|u\.?s\.? stocks?|stock market|markets?|"
    r"futures|wall street)\b|\b(?:market recap|stocks that explain today['’]s market|"
    r"what to do now|premarket movers|market today)\b",
    re.I,
)

_COMPANY_EVENT_PATTERNS = re.compile(
    r"\b(?:earnings|eps|revenue|guidance|outlook|forecast|merger|acquisition|"
    r"contract|product|launch|recall|lawsuit|settlement|offering|form\s+144|"
    r"form\s+[sS]-?[13]|8-k|10-q|10-k|investigation|approval|price target|"
    r"rating|upgrade|downgrade|maintained|reiterated)\b",
    re.I,
)


@dataclass(frozen=True)
class AttributionDecision:
    event_scope: str
    subject_entity: str
    subject_symbol: str
    subject_asset_type: str
    primary_symbol: str
    primary_target_role: str
    primary_target_asset_type: str
    primary_targets: tuple[str, ...]
    secondary_targets: tuple[str, ...]
    attribution_status: str
    attribution_confidence: float
    subject_target_available: bool
    original_subscription_symbol: str
    reason: str
    article_scope: str = "unknown"
    article_primary_subject: str = ""
    mentioned_symbols: tuple[str, ...] = ()
    subscription_entity_role: str = "delivery_route"
    subject_name: str = ""
    affected_symbols: tuple[str, ...] = ()
    benchmark_symbols: tuple[str, ...] = ()


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        cleaned = str(value).strip().upper()
        if cleaned and cleaned not in result:
            result.append(cleaned)
    return tuple(result)


def asset_metadata(symbol: str) -> tuple[str, str, str]:
    cleaned = str(symbol).strip().upper()
    return _ASSET_METADATA.get(cleaned, ("stock", "company", cleaned))


def subject_display_name(subject: str) -> str:
    """Return a stable human-readable name without treating it as a ticker."""
    cleaned = str(subject).strip().upper()
    if cleaned in _COMPANY_NAMES:
        return _COMPANY_NAMES[cleaned]
    return str(subject).strip().replace("_", " ").title()


def _first_available(preferred: Iterable[str], available: tuple[str, ...], fallback: str) -> str:
    available_set = set(available)
    for symbol in preferred:
        if symbol in available_set:
            return symbol
    return fallback if fallback in available_set else (available[0] if available else fallback)


def _alias_matches(text: str, symbol: str) -> tuple[int, int]:
    """Return (count, first_position) for a known company in text."""
    candidates: list[re.Pattern[str]] = [
        re.compile(rf"(?<![A-Z0-9])\$?{re.escape(symbol)}(?![A-Z0-9])", re.I)
    ]
    for alias in _SYMBOL_ALIASES.get(symbol, ()):
        candidates.append(re.compile(rf"(?<![A-Z0-9]){re.escape(alias)}(?![A-Z0-9])", re.I))
    positions: list[int] = []
    for pattern in candidates:
        positions.extend(match.start() for match in pattern.finditer(text))
    return len(positions), min(positions) if positions else 10**9


def _rank_company_candidates(text: str) -> list[tuple[int, int, str]]:
    ranked: list[tuple[int, int, str]] = []
    for symbol in _SYMBOL_ALIASES:
        count, first = _alias_matches(text, symbol)
        if count:
            ranked.append((-count, first, symbol))
    ranked.sort()
    return ranked


def _first_company(text: str) -> str:
    ranked = _rank_company_candidates(text)
    return ranked[0][2] if ranked else ""


def _detect_company_subject(headline: str, body: str) -> tuple[str, str, float, str]:
    """Return subject symbol, status hint, confidence, and reason.

    Headline identity normally wins.  A materially different, repeated body identity
    wins only when the headline identity is absent from the body, which catches an
    incorrectly attached Wendy's article under a Micron/Nvidia headline.
    """
    headline_subject = _first_company(headline)
    body_ranked = _rank_company_candidates(body[:12000])
    body_subject = body_ranked[0][2] if body_ranked else ""
    body_top_count = -body_ranked[0][0] if body_ranked else 0

    if headline_subject and body_subject and headline_subject != body_subject:
        headline_in_body, _ = _alias_matches(body[:12000], headline_subject)
        if headline_in_body == 0 and body_top_count >= 1:
            return (
                body_subject,
                "headline_body_mismatch",
                0.98,
                f"headline subject={headline_subject} but article body subject={body_subject}",
            )
        return headline_subject, "headline_primary", 0.90, f"headline subject={headline_subject}; body contains secondary companies"
    if headline_subject:
        return headline_subject, "headline_primary", 0.96, f"headline subject={headline_subject}"
    if body_subject:
        return body_subject, "body_primary", 0.78, f"article body subject={body_subject}"
    return "", "unresolved", 0.25, "no known company subject detected"


def _detect_macro_subject(headline: str, body: str, *, headline_company: str) -> tuple[str, float, str]:
    lead = f"{headline}\n{body[:1800]}"
    for subject, pattern in _SPECIFIC_MACRO_PATTERNS:
        if subject == "fomc":
            positive = False
            for sentence in re.split(r"[.!?;\n]+", lead):
                if not pattern.search(sentence):
                    continue
                if re.search(r"\b(?:no|without|did not|does not|lacks?|absent)\b", sentence, re.I):
                    continue
                positive = True
                break
            if positive:
                return subject, 0.95, f"specific macro event detected: {subject}"
        elif pattern.search(lead):
            return subject, 0.95, f"specific macro event detected: {subject}"

    # Generic Fed/macro language is context, not FOMC.  Do not override a clear company
    # headline unless the headline itself is a macro/market headline.
    generic_in_headline = bool(_GENERIC_MACRO_CONTEXT.search(headline))
    generic_in_lead = bool(_GENERIC_MACRO_CONTEXT.search(lead))
    market_headline = bool(re.search(r"\b(?:market|stocks?|futures|dow|s&p|nasdaq|treasury|inflation)\b", headline, re.I))
    if generic_in_lead and (generic_in_headline or market_headline or not headline_company):
        return "macro_market_context", 0.68, "generic Fed/rates context detected without FOMC release evidence"
    return "", 0.0, ""


def _detect_geopolitical_subject(headline: str, body: str) -> tuple[str, tuple[str, ...], float, str]:
    lead = f"{headline}\n{body[:1800]}"
    if not _GEOPOLITICAL_CONTEXT.search(lead):
        return "", (), 0.0, ""
    for pattern, preferred, subject in _GEOPOLITICAL_SECTOR_PREFERENCES:
        if pattern.search(lead):
            return subject, preferred, 0.82, f"geopolitical risk channel detected: {subject}"
    return "geopolitical_risk", ("SPY", "QQQ", "TLT", "IWM"), 0.76, "broad geopolitical risk context detected"


def _mentioned_company_symbols(headline: str, body: str) -> tuple[str, ...]:
    ranked = _rank_company_candidates(f"{headline}\n{body[:12000]}")
    return _unique(item[2] for item in ranked)


def _is_broad_market_article(headline: str) -> bool:
    return bool(_MARKET_HEADLINE_RE.search(str(headline or "").strip()))


def _headline_multi_subjects(headline: str) -> tuple[str, ...]:
    """Identify deliberately joint company headlines conservatively.

    Merely mentioning a peer is not enough.  A conjunction in the headline is a
    strong enough deterministic signal to classify the article as multi-subject
    and keep it out of single-company prediction paths.
    """
    if not re.search(r"\b(?:and|&|versus|vs\.?)\b", headline, re.I):
        return ()
    symbols = _unique(item[2] for item in _rank_company_candidates(headline))
    return symbols if len(symbols) >= 2 else ()


def _benchmark_symbols(available: Iterable[str], *, exclude: Iterable[str] = ()) -> tuple[str, ...]:
    excluded = set(_unique(exclude))
    return _unique(
        symbol
        for symbol in available
        if symbol not in excluded and asset_metadata(symbol)[0] in {"etf", "index"}
    )


def attribute_event_targets(
    *,
    headline: str,
    body: str,
    original_symbol: str,
    available_symbols: Iterable[str],
) -> AttributionDecision:
    """Separate actual event subject, subscription route, and reaction target."""

    original = str(original_symbol).strip().upper()
    available = _unique((*available_symbols, original))
    available_set = set(available)
    headline_text = str(headline or "")
    body_text = str(body or "")
    is_broadtape_route = original.startswith("BROADTAPE:")

    company_subject, company_hint, company_confidence, company_reason = _detect_company_subject(
        headline_text, body_text
    )
    macro_subject, macro_confidence, macro_reason = _detect_macro_subject(
        headline_text, body_text, headline_company=company_subject
    )
    geopolitical_subject, geopolitical_preferred, geopolitical_confidence, geopolitical_reason = _detect_geopolitical_subject(
        headline_text, body_text
    )

    if macro_subject and (not geopolitical_subject or macro_confidence >= geopolitical_confidence):
        preferred = ("SPY", "QQQ", "TLT", "IWM")
        primary = _first_available(preferred, available, original)
        secondary = _unique(symbol for symbol in preferred if symbol in available_set and symbol != primary)
        target_asset_type, _, _ = asset_metadata(primary)
        return AttributionDecision(
            event_scope="macro",
            subject_entity=macro_subject,
            subject_symbol="",
            subject_asset_type="macro_event",
            primary_symbol=primary,
            primary_target_role="market_proxy",
            primary_target_asset_type=target_asset_type,
            primary_targets=(primary,),
            secondary_targets=secondary,
            attribution_status="matched" if original == primary else "delivery_proxy",
            attribution_confidence=macro_confidence,
            subject_target_available=True,
            original_subscription_symbol=original,
            reason=f"{macro_reason}; original subscription symbol={original}",
            article_scope="macro",
            article_primary_subject=macro_subject,
            mentioned_symbols=_mentioned_company_symbols(headline_text, body_text),
            subscription_entity_role="delivery_route" if original != primary else "market_proxy",
            subject_name=subject_display_name(macro_subject),
            benchmark_symbols=_unique((primary, *secondary)),
        )

    if geopolitical_subject and not company_subject:
        primary = _first_available(geopolitical_preferred, available, original)
        secondary = _unique(symbol for symbol in geopolitical_preferred if symbol in available_set and symbol != primary)
        target_asset_type, natural_scope, _ = asset_metadata(primary)
        return AttributionDecision(
            event_scope="global" if geopolitical_subject == "geopolitical_risk" else "sector",
            subject_entity=geopolitical_subject,
            subject_symbol="",
            subject_asset_type="market_event" if geopolitical_subject == "geopolitical_risk" else "sector_event",
            primary_symbol=primary,
            primary_target_role="market_proxy" if natural_scope in {"market", "macro"} else "sector_proxy",
            primary_target_asset_type=target_asset_type,
            primary_targets=(primary,),
            secondary_targets=secondary,
            attribution_status="matched" if original == primary else "delivery_proxy",
            attribution_confidence=geopolitical_confidence,
            subject_target_available=True,
            original_subscription_symbol=original,
            reason=f"{geopolitical_reason}; original subscription symbol={original}",
            article_scope="global" if geopolitical_subject == "geopolitical_risk" else "sector",
            article_primary_subject=geopolitical_subject,
            mentioned_symbols=_mentioned_company_symbols(headline_text, body_text),
            subscription_entity_role="market_proxy" if original == primary else "delivery_route",
            subject_name=subject_display_name(geopolitical_subject),
            benchmark_symbols=_unique((primary, *secondary)),
        )

    original_asset_type, original_scope, original_subject = asset_metadata(original)
    if (
        original_asset_type in {"etf", "index"}
        and re.search(rf"(?<![A-Z0-9]){re.escape(original)}(?![A-Z0-9])", headline_text, re.I)
        and not _COMPANY_EVENT_PATTERNS.search(headline_text)
    ):
        return AttributionDecision(
            event_scope=original_scope,
            subject_entity=original_subject,
            subject_symbol="",
            subject_asset_type=original_asset_type,
            primary_symbol=original,
            primary_target_role=("sector_proxy" if original_scope == "sector" else "market_proxy"),
            primary_target_asset_type=original_asset_type,
            primary_targets=(original,),
            secondary_targets=_unique(symbol for symbol in available if symbol != original),
            attribution_status="matched",
            attribution_confidence=0.98,
            subject_target_available=True,
            original_subscription_symbol=original,
            reason=f"headline is about the subscribed {original_asset_type} itself",
            article_scope=original_scope,
            article_primary_subject=original_subject,
            mentioned_symbols=(),
            subscription_entity_role=("sector_proxy" if original_scope == "sector" else "market_proxy"),
            subject_name=subject_display_name(original_subject),
            benchmark_symbols=(original,),
        )

    if _is_broad_market_article(headline_text):
        mentioned = _mentioned_company_symbols(headline_text, body_text)
        primary = _first_available(("SPY", "QQQ", "IWM"), available, original)
        secondary = _unique(
            symbol
            for symbol in ("QQQ", "IWM", "TLT")
            if symbol in available_set and symbol != primary
        )
        target_asset_type, _, _ = asset_metadata(primary)
        status = (
            "matched"
            if original == primary
            else ("delivery_proxy" if original in mentioned else "market_level")
        )
        return AttributionDecision(
            event_scope="market",
            subject_entity="us_equities",
            subject_symbol="",
            subject_asset_type="market_event",
            primary_symbol=primary,
            primary_target_role="market_proxy",
            primary_target_asset_type=target_asset_type,
            primary_targets=(primary,),
            secondary_targets=secondary,
            attribution_status=status,
            attribution_confidence=0.94,
            subject_target_available=True,
            original_subscription_symbol=original,
            reason=(
                f"broad market headline; original subscription symbol={original}; "
                f"mentioned companies={','.join(mentioned) if mentioned else 'none'}"
            ),
            article_scope="market",
            article_primary_subject="us_equities",
            mentioned_symbols=mentioned,
            subscription_entity_role=("mentioned_entity" if original in mentioned else "delivery_route"),
            subject_name="U.S. equities",
            benchmark_symbols=_unique((primary, *secondary)),
        )

    multi_subjects = _headline_multi_subjects(headline_text)
    if multi_subjects:
        return AttributionDecision(
            event_scope="market",
            subject_entity="multi_subject",
            subject_symbol="",
            subject_asset_type="unknown",
            primary_symbol=original,
            primary_target_role="unavailable_subject",
            primary_target_asset_type="unknown",
            primary_targets=(),
            secondary_targets=(),
            attribution_status="multi_subject",
            attribution_confidence=0.82,
            subject_target_available=False,
            original_subscription_symbol=original,
            reason=(
                "multiple company subjects explicitly joined in headline; "
                f"subjects={','.join(multi_subjects)}; original subscription symbol={original}"
            ),
            article_scope="market",
            article_primary_subject="multi_subject",
            mentioned_symbols=multi_subjects,
            subscription_entity_role="delivery_route",
            subject_name="Multiple companies",
            affected_symbols=multi_subjects,
            benchmark_symbols=_benchmark_symbols(available, exclude=multi_subjects),
        )

    if company_subject:
        target_available = company_subject in available_set
        mismatch = company_hint == "headline_body_mismatch" or (
            company_subject != original and not target_available
        )
        if target_available:
            primary = company_subject
            role = "subject_asset"
            targets = (company_subject,)
            status = "matched" if company_subject == original else "delivery_proxy"
        else:
            # Keep a technical primary symbol for immutable storage compatibility, but
            # expose an empty target list and unavailable_subject role so forecasts and
            # reaction calibration are deterministically suppressed downstream.
            primary = original
            role = "unavailable_subject"
            targets = ()
            status = "mismatch" if mismatch else "unresolved"
        secondary = _unique(symbol for symbol in available if symbol != primary)
        reason = (
            f"{company_reason}; actual subject={company_subject}; original subscription symbol={original}; "
            f"subject target {'available' if target_available else 'not available'}"
        )
        return AttributionDecision(
            event_scope="company",
            subject_entity=company_subject,
            subject_symbol=company_subject,
            subject_asset_type="stock",
            primary_symbol=primary,
            primary_target_role=role,
            primary_target_asset_type=asset_metadata(primary)[0] if targets else "unknown",
            primary_targets=targets,
            secondary_targets=secondary,
            attribution_status=status,
            attribution_confidence=company_confidence,
            subject_target_available=target_available,
            original_subscription_symbol=original,
            reason=reason,
            article_scope="company",
            article_primary_subject=company_subject,
            mentioned_symbols=_mentioned_company_symbols(headline_text, body_text),
            subscription_entity_role=(
                "subject" if company_subject == original else ("delivery_route" if not target_available else "mentioned_entity")
            ),
            subject_name=subject_display_name(company_subject),
            affected_symbols=(company_subject,),
            benchmark_symbols=_benchmark_symbols(available, exclude=(company_subject,)),
        )

    text = f"{headline_text}\n{body_text[:12000]}"
    for subject, pattern, preferred in _SECTOR_PATTERNS:
        if pattern.search(text):
            primary = _first_available(preferred, available, original)
            secondary = _unique(symbol for symbol in preferred if symbol in available_set and symbol != primary)
            return AttributionDecision(
                event_scope="sector",
                subject_entity=subject,
                subject_symbol="",
                subject_asset_type="sector_event",
                primary_symbol=primary,
                primary_target_role="sector_proxy",
                primary_target_asset_type=asset_metadata(primary)[0],
                primary_targets=(primary,),
                secondary_targets=secondary,
                attribution_status="delivery_proxy" if primary != original else "matched",
                attribution_confidence=0.72,
                subject_target_available=True,
                original_subscription_symbol=original,
                reason=f"sector-wide context detected: {subject}",
                article_scope="sector",
                article_primary_subject=subject,
            mentioned_symbols=_mentioned_company_symbols(headline_text, body_text),
            subscription_entity_role="sector_proxy" if original == primary else "delivery_route",
            subject_name=subject_display_name(subject),
            benchmark_symbols=_unique((primary, *secondary)),
        )

    asset_type, natural_scope, natural_subject = asset_metadata(original)
    if is_broadtape_route:
        primary = _first_available(("SPY", "QQQ", "IWM", "TLT"), available, "SPY")
        secondary = _unique(symbol for symbol in ("QQQ", "IWM", "TLT") if symbol in available_set and symbol != primary)
        return AttributionDecision(
            event_scope="market",
            subject_entity="unmapped_broadtape_news",
            subject_symbol="",
            subject_asset_type="news_route",
            primary_symbol=primary,
            primary_target_role="market_proxy",
            primary_target_asset_type=asset_metadata(primary)[0],
            primary_targets=(primary,),
            secondary_targets=secondary,
            attribution_status="broadtape_proxy",
            attribution_confidence=0.45,
            subject_target_available=True,
            original_subscription_symbol=original,
            reason=f"BroadTape delivery route had no stronger deterministic company/sector/macro match; using low-confidence market proxy {primary}",
            article_scope="market",
            article_primary_subject="unmapped_broadtape_news",
            mentioned_symbols=_mentioned_company_symbols(headline_text, body_text),
            subscription_entity_role="delivery_route",
            subject_name="Unmapped BroadTape news",
            benchmark_symbols=_unique((primary, *secondary)),
        )
    return AttributionDecision(
        event_scope=natural_scope,
        subject_entity=natural_subject,
        subject_symbol=original if asset_type == "stock" else "",
        subject_asset_type=asset_type,
        primary_symbol=original,
        primary_target_role=("subject_asset" if asset_type == "stock" else "market_proxy"),
        primary_target_asset_type=asset_type,
        primary_targets=(original,),
        secondary_targets=_unique(symbol for symbol in available if symbol != original),
        attribution_status="unresolved",
        attribution_confidence=0.35,
        subject_target_available=True,
        original_subscription_symbol=original,
        reason=f"default {natural_scope} attribution for asset_type={asset_type}; no independent subject match",
        article_scope=natural_scope,
        article_primary_subject=natural_subject,
        mentioned_symbols=_mentioned_company_symbols(headline_text, body_text),
        subscription_entity_role="subject" if asset_type == "stock" else "delivery_route",
        subject_name=subject_display_name(natural_subject),
        affected_symbols=(original,) if asset_type == "stock" else (),
        benchmark_symbols=_benchmark_symbols(available, exclude=(original,)),
    )
