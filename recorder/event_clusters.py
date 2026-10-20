from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from rapidfuzz.fuzz import token_set_ratio

from .analysis_models import NewsAnalysis
from .database import parse_utc

UTC = timezone.utc

_QUARTER_RE = re.compile(
    r"\b(?:fiscal\s*)?(?:q([1-4])|([1-4])(?:st|nd|rd|th)[-\s]+quarter|"
    r"(first|second|third|fourth)[-\s]+quarter)(?:\s+(?:fy)?(20\d{2}))?\b",
    re.I,
)
_MONTH_RE = re.compile(
    r"\b(january|february|march|april|may|june|july|august|september|october|november|december)\b",
    re.I,
)
_MACRO_SUBJECTS = ("pce", "cpi", "employment", "fomc", "gdp", "retail_sales", "ism")


@dataclass(frozen=True)
class ClusterIdentity:
    cluster_key: str
    event_scope: str
    event_type: str
    cluster_family: str
    subject_key: str
    primary_symbol: str
    subject_symbol: str
    target_symbol: str
    event_anchor: str
    comparison_text: str
    event_time: datetime


def _quarter_label(text: str) -> str | None:
    match = _QUARTER_RE.search(text)
    if not match:
        return None
    raw = match.group(1) or match.group(2) or match.group(3) or ""
    words = {"first": "1", "second": "2", "third": "3", "fourth": "4"}
    quarter = words.get(raw.lower(), raw)
    year = match.group(4) or ""
    return f"q{quarter}{('-' + year) if year else ''}"


def _macro_period(text: str, event_time: datetime) -> str:
    month = _MONTH_RE.search(text)
    month_label = month.group(1).lower() if month else event_time.strftime("%Y-%m")
    year_match = re.search(r"\b(20\d{2})\b", text)
    year = year_match.group(1) if year_match else str(event_time.year)
    return f"{month_label}-{year}"


def _subject_key(analysis: NewsAnalysis) -> str:
    subject = (analysis.subject_entity or "").strip().lower().replace(" ", "_")
    if subject:
        return subject[:80]
    if analysis.event_subject_symbol:
        return analysis.event_subject_symbol.lower()
    if analysis.event_scope == "macro":
        subtype = analysis.event_subtype.lower()
        for token in _MACRO_SUBJECTS:
            if token in subtype:
                return token
        return analysis.event_type
    return analysis.primary_symbol.lower()


def _cluster_family(analysis: NewsAnalysis, headline: str) -> str:
    if analysis.event_type != "market_price_update":
        return analysis.event_type
    if analysis.attribution_status == "mismatch" or not analysis.subject_target_available:
        return "mismatched_subject_recap"
    if (
        analysis.target_role == "market_proxy"
        and analysis.subscription_entity_role == "market_proxy"
        and analysis.primary_symbol in {"SPY", "QQQ", "IWM", "DIA"}
    ):
        return "instrument_market_recap"
    if (
        analysis.article_scope == "market"
        and analysis.subscription_entity_role in {"mentioned_entity", "delivery_route"}
        and analysis.attribution_status == "delivery_proxy"
    ):
        return "broad_market_recap"
    if analysis.article_scope == "company":
        return "company_price_recap"
    return "generic_market_recap"


def build_cluster_identity(
    analysis: NewsAnalysis,
    *,
    headline: str,
    body: str,
    published_at: str,
) -> ClusterIdentity:
    event_time = parse_utc(published_at)
    text = " ".join(f"{headline} {body[:5000]}".split())
    subject = _subject_key(analysis)
    family = _cluster_family(analysis, headline)

    if analysis.event_scope == "macro":
        anchor = _macro_period(text, event_time)
        # FOMC meetings are clustered by event date because multiple stages occur on the same day.
        if analysis.event_type in {"fomc", "fed_speech"} or subject == "fomc":
            anchor = event_time.date().isoformat()
    elif analysis.event_type in {"earnings", "guidance"}:
        anchor = _quarter_label(text) or event_time.date().isoformat()
    else:
        # General events use a shorter time anchor to avoid merging unrelated items over long periods.
        anchor = event_time.strftime("%Y-%m-%d")

    raw_key = "|".join(
        (
            analysis.event_scope,
            analysis.event_type,
            family,
            subject,
            analysis.event_subject_symbol or analysis.subject_entity or analysis.primary_symbol,
            anchor,
        )
    )
    digest = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()[:20]
    return ClusterIdentity(
        cluster_key=f"evt:{digest}",
        event_scope=analysis.event_scope,
        event_type=analysis.event_type,
        cluster_family=family,
        subject_key=subject,
        primary_symbol=analysis.event_subject_symbol or analysis.primary_symbol,
        subject_symbol=analysis.event_subject_symbol,
        target_symbol=(
            analysis.primary_symbol
            if analysis.subject_target_available and analysis.target_role != "unavailable_subject"
            else ""
        ),
        event_anchor=anchor,
        comparison_text=text[:4000],
        event_time=event_time,
    )



def cluster_member_role(analysis: NewsAnalysis) -> str:
    """Classify the document's role inside an event cluster.

    This keeps an official/initial release distinct from a market recap or
    reaction commentary even when all items belong to the same PCE, earnings,
    or FOMC event cluster.
    """
    if analysis.official_source_status == "attached" and analysis.event_stage in {
        "initial_release", "regulatory_filing", "press_conference"
    }:
        return "primary_release"
    if analysis.event_type == "market_price_update":
        return "market_recap"
    if analysis.event_stage == "retrospective":
        return "reaction_commentary"
    if analysis.event_stage == "pre_event":
        return "pre_event"
    if analysis.event_stage in {"initial_release", "regulatory_filing", "press_conference"}:
        return "secondary_report"
    if analysis.event_stage == "follow_up":
        return "follow_up"
    return "unknown"

def source_rank(source_tier: str, event_stage: str, has_official_document: bool, key_fact_count: int) -> int:
    rank = {
        "official_primary": 400,
        "reputable_secondary": 250,
        "secondary": 150,
        "unknown": 100,
    }.get(source_tier, 100)
    if has_official_document:
        rank += 100
    rank += {
        "initial_release": 60,
        "press_conference": 50,
        "regulatory_filing": 55,
        "follow_up": 30,
        "retrospective": 5,
        "pre_event": 0,
        "unknown": 0,
    }.get(event_stage, 0)
    rank += min(40, key_fact_count * 4)
    return rank


def cluster_similarity(identity: ClusterIdentity, existing: dict[str, Any]) -> float:
    if str(existing.get("event_scope")) != identity.event_scope:
        return 0.0
    if str(existing.get("event_type")) != identity.event_type:
        return 0.0
    existing_family = str(existing.get("cluster_family") or "unknown")
    if existing_family not in {"", "unknown", identity.cluster_family}:
        return 0.0
    if str(existing.get("subject_key")) != identity.subject_key:
        return 0.0
    if str(existing.get("event_anchor")) == identity.event_anchor:
        base = 94.0
    else:
        base = 65.0
    comparison = str(existing.get("comparison_text") or "")
    if comparison and identity.comparison_text:
        return max(base, float(token_set_ratio(identity.comparison_text, comparison)))
    return base
