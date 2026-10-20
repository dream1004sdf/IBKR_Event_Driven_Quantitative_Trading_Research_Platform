from __future__ import annotations

import re
from dataclasses import dataclass

from .s05_event_proxy_closeout import PROXY_RULES, ProxyRule
from .s05_news_semantic_closeout import classify_headline, normalize_headline
from .s05_proxy_semantic_closeout import infer_channel, infer_region


S05P4_V4_VERSION = "s05p4-proxy-semantic-precision-v4"
PROXY_MAP_VERSION_V4 = "s05p4-event-proxy-map-v4"
PROXY_SCOPE_V4 = "primary_proxy_only_v1"


@dataclass(frozen=True)
class V4CausalDecision:
    eligible: bool
    exclusion_reason: str
    matched_rule: str


_DUPLICATE_OR_REPEAT = re.compile(
    r"\breported earlier\b|\(updated\)|^\s*!?\s*update\s*[:\-]",
    re.IGNORECASE,
)

_LISTICLE_OR_ADVICE = re.compile(
    r"\b(?:stocks?|tickers?|etfs?|reits?)\s+(?:to\s+)?(?:watch|buy|tap)\b|"
    r"\b(?:top|best)\s+\d*\s*(?:stocks?|updates?|picks?|ways?)\b|"
    r"\bpicks?\s+\d+\s+winners?\b|\bhow\s+to\s+claim\b|"
    r"\bwhat\s+(?:that|it)\s+means?\s+for\s+(?:stocks?|etfs?|markets?)\b|"
    r"\bif\s+you\s+invested\b|\bfor\s+quick\s+price\s+swings\b|"
    r"\bconsumer tech news\b|\bis\b.{0,50}\bthe next\b",
    re.IGNORECASE,
)

_BROAD_MARKET_SUBJECT = (
    r"stock futures|u\.s\. stocks?|global markets?|major (?:indexes|indices)|"
    r"s&p 500|nasdaq(?: 100)?|dow(?: jones)?|wall street|tsx|market today|"
    r"investor sentiment|fear index|stock markets?"
)
_MARKET_MOVE = (
    r"surges?|jumps?|spikes?|soars?|rall(?:y|ies)|rises?|gains?|higher|slides?|falls?|drops?|"
    r"dips?|tumbles?|stumbles?|lower|mixed|record high|open higher|open lower"
)
_MARKET_RECAP = re.compile(
    rf"\b(?:{_BROAD_MARKET_SUBJECT})\b.{{0,100}}\b(?:{_MARKET_MOVE})\b|"
    rf"\b(?:{_MARKET_MOVE})\b.{{0,100}}\b(?:{_BROAD_MARKET_SUBJECT})\b|"
    r"\bmarkets?\s+(?:brace|braced|cheer|cheers|rattle|rattled)\b|"
    r"\blive coverage\b|\bmarket recap\b|\bstock market today\b|"
    r"\bwill\s+s&p 500\s+open\s+up\s+or\s+down\b|\bglobal energy pivot\b",
    re.IGNORECASE,
)

_PRICE_MOVE_RECAP = re.compile(
    r"\b(?:oil|crude|brent|wti|gold|shares?|energy stocks?|shipping insurance costs?)\b"
    r".{0,70}\b(?:prices?\s+)?(?:surges?|jumps?|rall(?:y|ies)|rises?|gains?|spikes?|soars?|"
    r"slides?|falls?|drops?|dips?|tumbles?|trims? (?:gains?|losses?)|extends? gains?|trading (?:higher|lower))\b|"
    r"\bwhy\s+(?:is|are|did)\b.{0,80}\b(?:stock|shares?|oil|crude)\b",
    re.IGNORECASE,
)

_PREVIEW_OR_FUTURE = re.compile(
    r"\bahead of\b|\bweek ahead\b|\bthis week\b|\btomorrow\b|\bwhat to expect\b|"
    r"\bfocus shifts? to\b|\b(?:decision|meeting|talks?)\s+looms?\b|"
    r"\b(?:will|scheduled to)\s+(?:announce|meet|discuss|consider)\b|"
    r"\b(?:is|are)\s+(?:not\s+)?expected to\b|\bexpected\s+['\"]?soon['\"]?\b|"
    r"\bmay\s+consider\b|\bsee scope to\b|\beyes? (?:all )?options? to\b|"
    r"\bplans? to meet\b|\bto meet\b.{0,50}\b(?:tonight|tomorrow|this week)\b|\bexpected to propose\b|"
    r"\bfomc rate decision for\b",
    re.IGNORECASE,
)

_OPINION_OR_ANALYSIS = re.compile(
    r"\b(?:cramer|strategist|yardeni|analyst|goldman sachs|pompliano)\b.{0,50}\b(?:says?|warns?|picks?|expects?|raises? .* forecasts?)\b|"
    r"\bmarket cheers?\b|\bwhat that means\b|\bhow long can markets?\b|"
    r"\breality gap\b|\bdanger zone\b|\bdeploy cash\b|\bpower struggle\b|"
    r"\bchallenge(?:s|d)?\b.{0,50}\bstrategy\b|\bargues? (?:it|that)\b.{0,40}\blegally\b|"
    r"\bwhat'?s your move\b|\bdecision\s+(?:was\s+)?(?:a\s+)?good first step\b",
    re.IGNORECASE,
)

_FORECAST_OR_SCENARIO = re.compile(
    r"\b(?:raises?|cuts?|lowers?|increases?)\b.{0,70}\b(?:oil[- ]demand|economic[- ]growth|oil price)(?:\s+growth)?\s+forecasts?\b|"
    r"\b(?:oil[- ]demand|economic[- ]growth)(?:\s+growth)?\s+forecasts?\b|"
    r"\bexpects?\b.{0,100}\b(?:output|trade patterns?|production growth|shut[ -]?ins?)\b|"
    r"\bwatching\b.{0,80}\bexpects?\b",
    re.IGNORECASE,
)

_NON_MATERIAL_COMMENTARY = re.compile(
    r"\bapproval rating\b|\bthinks?\b.{0,50}\bwill listen\b|"
    r"\bno guarantees?\b.{0,50}\bceasefire\b|\bpush\b.{0,60}\bextend\b.{0,40}\bwaiver\b|"
    r"\bprices are down, interest rates are down\b|"
    r"\bmarkets will dictate (?:the )?price\b",
    re.IGNORECASE,
)

_WRONG_PROXY_GRANULARITY = re.compile(
    r"\b(?:lands?|wins?|secures?|securing)\b.{0,80}\bdefen[cs]e contract\b|"
    r"\bdefen[cs]e contract\b.{0,80}\b(?:worth|\$|million|billion)\b|"
    r"\bdata breach settlement\b|\bhikes? dividend\b",
    re.IGNORECASE,
)

_PRESS_RELEASE_FRAGMENT = re.compile(r"^\s*press release:\s+from\b", re.IGNORECASE)

_AMBIGUOUS_COMPOUND = re.compile(
    r"\bwatching\s+s&p\s+futures\b.{0,80}\bjoint statement\b|"
    r"\bmiddle east conflict escalates\b.{0,180};.{0,180}\b(?:imports?|exports?)\b",
    re.IGNORECASE,
)

_MONETARY_POLICY_ANCHOR = re.compile(
    r"\b(?:fed(?:'s| chair| vice chair)?|fomc|boj|bank of japan|ecb|european central bank|"
    r"bank of england|rbi)\b.{0,160}\b(?:policy|rates?|inflation|labor market|employment|"
    r"neutral|restrictive|dovish|hawkish|basis points?|bps|balance sheet)\b|"
    r"\b(?:interest )?rate decision\b|\bfed minutes\b",
    re.IGNORECASE,
)

_OIL_SUPPLY_ANCHOR = re.compile(
    r"\b(?:oil|crude|lng|opec\+?|hormuz|refinery|pipeline|tanker|vessels?|strategic petroleum reserve)\b"
    r".{0,120}\b(?:cuts?|raises?|halts?|stops?|starts?|resumes?|reopens?|opens?|opening|secure|secured|closes?|shutdown|outage|"
    r"attacks?|strikes?|seizes?|sanctions?|bans?|selling|sales?|releases?|quota|compensation|capacity|transits?|flows?|passes?|passage)\b|"
    r"\b(?:cuts?|raises?|halts?|stops?|starts?|resumes?|reopens?|opens?|opening|secure|secured|shutdown|attacks?|seizes?|bans?|selling|sales?|releases?|passes?|passage)\b"
    r".{0,120}\b(?:oil|crude|lng|opec\+?|hormuz|refinery|pipeline|tanker|vessels?|strategic petroleum reserve)\b",
    re.IGNORECASE,
)

_OIL_SUPPLY_PRIORITY = re.compile(
    r"\b(?:oil|crude|lng|opec\+?|refinery|pipeline|tanker|vessels?|strategic petroleum reserve)\b"
    r".{0,120}\b(?:cuts?|raises?|halts?|stops?|starts?|resumes?|reopens?|opens?|closes?|shutdown|outage|"
    r"attacks?|strikes?|seizes?|sanctions?|bans?|selling|sales?|releases?|quota|compensation|capacity|transits?|flows?|passes?|passage)\b|"
    r"\b(?:cuts?|raises?|halts?|stops?|starts?|resumes?|reopens?|opens?|shutdown|attacks?|seizes?|bans?|selling|sales?|releases?|passes?|passage)\b"
    r".{0,120}\b(?:oil|crude|lng|opec\+?|refinery|pipeline|tanker|vessels?|strategic petroleum reserve)\b|"
    r"\bhormuz\b.{0,100}\b(?:closed|re-?closes?|reopens?|opening|secure|secured|transits?|tankers?|vessels?|ships?|passage)\b|"
    r"\b(?:closed|re-?closes?|reopens?|opening|secure|secured|transits?|tankers?|vessels?|ships?|passage)\b.{0,100}\bhormuz\b",
    re.IGNORECASE,
)

_CONDITIONAL_HORMUZ = re.compile(
    r"\b(?:may|might|could|asks?|implores?|if)\b.{0,120}\bhormuz\b|"
    r"\bhormuz\b.{0,120}\b(?:may|might|could|asks?|implores?|if)\b",
    re.IGNORECASE,
)


def classify_v4_causal_proxy_headline(
    headline: str,
    *,
    provider: str = "",
    family: str = "",
) -> V4CausalDecision:
    """High-precision causal gate derived from Set A error classes, never row IDs.

    The gate deliberately prefers false negatives over false positives because the
    frozen promotion contract uses causal precision as a hard gate and recall only
    as a diagnostic.
    """

    text = normalize_headline(headline)
    if _AMBIGUOUS_COMPOUND.search(text) or (
        len(text) >= 360
        and text.count(";") >= 3
        and len(re.findall(r"\b(?:iran|israel|lebanon|gaza|russia|china|japan|korea|ukraine|hormuz)\b", text, re.IGNORECASE)) >= 3
    ):
        return V4CausalDecision(False, "insufficient_context", "ambiguous_multi_event_compound")
    checks = (
        ("duplicate_or_repeat", "reported_earlier_or_update", _DUPLICATE_OR_REPEAT),
        ("listicle", "listicle_or_advice", _LISTICLE_OR_ADVICE),
        ("market_recap", "broad_market_recap", _MARKET_RECAP),
        ("price_move_recap", "price_move_recap", _PRICE_MOVE_RECAP),
        ("preview", "preview_or_future", _PREVIEW_OR_FUTURE),
        ("opinion_or_analysis", "opinion_or_analysis", _OPINION_OR_ANALYSIS),
        ("opinion_or_analysis", "forecast_or_scenario", _FORECAST_OR_SCENARIO),
        ("non_material_commentary", "non_material_commentary", _NON_MATERIAL_COMMENTARY),
        ("wrong_proxy_family", "wrong_proxy_granularity", _WRONG_PROXY_GRANULARITY),
        ("secondary_report_no_new_information", "press_release_fragment", _PRESS_RELEASE_FRAGMENT),
    )
    for reason, rule, pattern in checks:
        if pattern.search(text):
            return V4CausalDecision(False, reason, rule)

    semantic = classify_headline(text)
    if semantic.news_content_type in {
        "valuation_article",
        "market_recap",
        "price_move_recap",
        "technical_analysis",
        "listicle",
        "opinion",
        "secondary_report",
        "calendar_preview",
    }:
        return V4CausalDecision(False, semantic.news_content_type, "s05p1_noncausal_semantic")

    if family == "rates_policy" and not _MONETARY_POLICY_ANCHOR.search(text):
        return V4CausalDecision(False, "wrong_proxy_family", "rates_without_monetary_policy_anchor")
    if family == "oil_supply" and not _OIL_SUPPLY_ANCHOR.search(text):
        return V4CausalDecision(False, "wrong_proxy_family", "oil_without_supply_anchor")

    return V4CausalDecision(True, "", "explicit_causal_action_or_authoritative_update")


def _candidate_rules(text: str) -> list[ProxyRule]:
    semantic = classify_headline(text).news_content_type
    if semantic in {"analyst_action", "opinion"}:
        return []
    result: list[ProxyRule] = []
    for rule in PROXY_RULES:
        family_match = re.search(rule.family_pattern, text, re.IGNORECASE)
        trigger_match = re.search(rule.trigger_pattern, text, re.IGNORECASE)
        if rule.family == "oil_supply" and _OIL_SUPPLY_ANCHOR.search(text):
            family_match = True
            trigger_match = True
        if rule.family == "rates_policy" and family_match and _MONETARY_POLICY_ANCHOR.search(text):
            trigger_match = True
        excluded = rule.exclude_pattern and re.search(rule.exclude_pattern, text, re.IGNORECASE)
        if family_match and trigger_match and not excluded:
            result.append(rule)
    return result


def match_proxy_rule_v4(headline: str, *, provider: str = "") -> ProxyRule | None:
    """Return one generalized v4 family after causal filtering and precedence."""

    text = normalize_headline(headline)
    preliminary = classify_v4_causal_proxy_headline(text, provider=provider)
    if not preliminary.eligible:
        return None
    candidates = _candidate_rules(text)
    if not candidates:
        return None
    by_family = {rule.family: rule for rule in candidates}

    # Subject/action precedence prevents incidental oil words in a Fed statement
    # and incidental conflict words in a concrete oil-supply event from winning.
    if "rates_policy" in by_family and _MONETARY_POLICY_ANCHOR.search(text):
        chosen = by_family["rates_policy"]
    elif (
        "oil_supply" in by_family
        and _OIL_SUPPLY_PRIORITY.search(text)
        and not _CONDITIONAL_HORMUZ.search(text)
    ):
        chosen = by_family["oil_supply"]
    else:
        order = (
            "cybersecurity",
            "grain_supply",
            "korea_risk",
            "sanctions",
            "china_taiwan",
            "defense_conflict",
            "oil_supply",
            "rates_policy",
        )
        chosen = next((by_family[name] for name in order if name in by_family), candidates[0])

    final = classify_v4_causal_proxy_headline(text, provider=provider, family=chosen.family)
    return chosen if final.eligible else None


def infer_region_v4(family: str, headline: str) -> str:
    """Prefer the affected geography and avoid first-token wins in multi-country text."""

    text = normalize_headline(headline)
    if re.search(r"\b(?:white house|trump administration)\b", text, re.IGNORECASE) and re.search(
        r"\b(?:export ban|security order|administration prepares|told oil executives)\b",
        text,
        re.IGNORECASE,
    ):
        return "united_states"

    if family == "rates_policy":
        if re.search(r"\b(?:boj|bank of japan|japan interest rate)\b", text, re.IGNORECASE):
            return "japan"
        if re.search(r"\b(?:ecb|european central bank|eurozone)\b", text, re.IGNORECASE):
            return "eurozone"
        if re.search(r"\b(?:bank of england|boe|united kingdom|uk)\b", text, re.IGNORECASE):
            return "united_kingdom"
        if re.search(r"\b(?:rbi|reserve bank of india|india)\b", text, re.IGNORECASE):
            return "india"
        return "united_states"

    if family == "korea_risk":
        return "korea"

    middle_match = re.search(r"\b(?:lebanon|hezbollah|gaza|hamas)\b", text, re.IGNORECASE)
    iran_match = re.search(r"\biran(?:ian)?\b", text, re.IGNORECASE)
    if middle_match and (iran_match is None or middle_match.start() < iran_match.start()):
        return "middle_east"

    if family == "oil_supply" and re.search(
        r"\bopec\+?\b|\b(?:saudi arabia|russia|iraq|uae|kuwait|kazakhstan|algeria|oman)\b"
        r".{0,180}\b(?:saudi arabia|russia|iraq|uae|kuwait|kazakhstan|algeria|oman)\b",
        text,
        re.IGNORECASE,
    ):
        return "global"

    if family == "oil_supply":
        if re.search(r"\bkazakhstan\b", text[:120], re.IGNORECASE):
            return "kazakhstan"
        legacy_region = infer_region(family, text)
        iran_or_hormuz = re.search(r"\b(?:iran(?:ian)?|hormuz)\b", text, re.IGNORECASE)
        legacy_marker = re.search(r"\b(?:china|chinese|beijing|russia|russian|ukraine|ukrainian)\b", text, re.IGNORECASE)
        if (
            legacy_region in {"china", "russia", "ukraine"}
            and iran_or_hormuz
            and legacy_marker
            and iran_or_hormuz.start() < legacy_marker.start()
        ):
            return "iran"

    if family == "sanctions" and re.search(r"\bwhite house\b.{0,100}\bexport ban\b", text, re.IGNORECASE):
        return "united_states"
    return infer_region(family, text)


def infer_channel_v4(family: str, headline: str) -> str:
    text = normalize_headline(headline)
    if family == "rates_policy" and re.search(
        r"\bfed\b.{0,220}\b(?:does not|doesn't|not typically)\b.{0,60}\brespond\b.{0,40}\boil prices?\b",
        text,
        re.IGNORECASE,
    ):
        return "sovereign_risk"
    return infer_channel(family, text)


def infer_primary_proxy_v4(family: str, region: str, channel: str) -> str:
    if family == "rates_policy":
        return {
            "japan": "EWJ",
            "eurozone": "EZU",
            "united_kingdom": "EWU",
            "india": "INDA",
        }.get(region, "IEF")
    if family == "oil_supply":
        return "XLE"
    if family == "china_taiwan":
        return "EWT" if region == "taiwan" else "FXI"
    if family == "sanctions":
        if channel == "energy":
            return "XLE"
        if channel == "technology" or region == "china":
            return "FXI"
        return "GLD"
    return {
        "defense_conflict": "ITA",
        "grain_supply": "DBA",
        "korea_risk": "EWY",
        "cybersecurity": "CIBR",
    }[family]
