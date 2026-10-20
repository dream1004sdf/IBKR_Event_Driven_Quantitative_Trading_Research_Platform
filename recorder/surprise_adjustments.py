from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .ai_provider import PermanentAnalysisError, TransientAnalysisError

SURPRISE_ADJUSTMENT_VERSION = "surprise-adjustment-notes-v3"

GEOPOLITICAL_REASON_CATEGORIES = (
    "geopolitical_risk_absorbed_no_edge",
    "geopolitical_channel_underweighted",
    "geopolitical_asset_map_wrong",
    "geopolitical_source_confirmation_weak",
    "geopolitical_market_confirmation_missing",
    "geopolitical_offset_by_macro_or_earnings",
)

GEOPOLITICAL_TEXT_PATTERN = re.compile(
    r"\b(?:geopolitical|war|missile|airstrike|strike|sanctions?|export controls?|"
    r"tariffs?|trade war|military|invasion|ceasefire|red sea|hormuz|taiwan|china|"
    r"russia|ukraine|iran|israel|north korea|korea peninsula|cyberattack|shipping|"
    r"chokepoint|oil supply|defense)\b",
    re.I,
)

GEOPOLITICAL_THEMES = {
    "military_escalation",
    "energy_supply",
    "shipping_chokepoint",
    "trade_conflict",
    "export_controls",
    "semiconductor_supply_chain",
    "food_supply",
    "defense",
    "cybersecurity",
    "broad_geopolitical",
}

GEOPOLITICAL_REGIONS = {
    "middle_east",
    "taiwan_china",
    "korea_peninsula",
    "russia_ukraine",
}


class EvidenceSourceWeight(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str = Field(min_length=1, max_length=80)
    weight: float = Field(ge=0.0, le=1.0)


class OpenAISurpriseCauseAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason_category: str = Field(min_length=1, max_length=96)
    reason_summary: str = Field(min_length=1, max_length=1500)
    recommended_adjustment: str = Field(min_length=1, max_length=1500)
    evidence_source_weights: list[EvidenceSourceWeight] = Field(max_length=10)
    missing_evidence: list[str] = Field(max_length=12)
    pattern_tags: list[str] = Field(max_length=12)
    scorer_rule_candidate: str = Field(min_length=1, max_length=900)
    confidence: float = Field(ge=0.0, le=1.0)


@dataclass(frozen=True)
class SurpriseAdjustmentNote:
    score_id: int
    outcome_id: int
    replay_corpus_id: int
    symbol: str
    event_type: str
    event_subtype: str
    replay_timestamp: str
    horizon: str
    price_source: str
    scorer_name: str
    model_name: str
    scorer_version: str
    predicted_direction: str
    actual_direction: str
    expected_return: float
    actual_raw_return: float | None
    actual_excess_return: float | None
    mismatch_type: str
    reason_category: str
    reason_summary: str
    recommended_adjustment: str
    feature_hints: dict[str, Any]
    evidence: dict[str, Any]
    analysis_provider: str = "deterministic"
    analysis_model: str = "rulebook"
    adjustment_version: str = SURPRISE_ADJUSTMENT_VERSION
    raw: dict[str, Any] | None = None


def _load_snapshot(row: dict[str, Any]) -> dict[str, Any]:
    try:
        parsed = json.loads(row.get("input_snapshot_json") or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _safe_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed != parsed:
        return None
    return parsed


def _external_signal_features(snapshot: dict[str, Any]) -> dict[str, Any]:
    desk = ((snapshot.get("source_desk_context") or {}).get("external_geopolitical_news_desk") or {})
    signals = desk.get("signals") or []
    sentiments = [str(item.get("sentiment_label") or "unknown") for item in signals if isinstance(item, dict)]
    themes = [str(item.get("theme") or "unknown") for item in signals if isinstance(item, dict)]
    regions = [str(item.get("region") or "unknown") for item in signals if isinstance(item, dict)]
    assets: list[str] = []
    market_statuses: list[str] = []
    gate_statuses: list[str] = []
    sources: list[str] = []
    for item in signals:
        if not isinstance(item, dict):
            continue
        sources.append(str(item.get("source") or "unknown"))
        market_statuses.append(str(item.get("market_reaction_status") or "unknown"))
        gate_statuses.append(str(item.get("prediction_gate_status") or "unknown"))
        for asset in item.get("assets") or []:
            if str(asset).strip():
                assets.append(str(asset).strip().upper())
    return {
        "signal_count": len(signals),
        "sentiments": dict(Counter(sentiments)),
        "themes": dict(Counter(themes)),
        "regions": dict(Counter(regions)),
        "sources": dict(Counter(sources)),
        "assets": sorted(set(assets)),
        "market_reaction_statuses": dict(Counter(market_statuses)),
        "prediction_gate_statuses": dict(Counter(gate_statuses)),
        "top_titles": [str(item.get("title") or "")[:160] for item in signals[:3] if isinstance(item, dict)],
    }


def _compact_items(items: Any, keys: tuple[str, ...], *, limit: int = 3) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    compacted: list[dict[str, Any]] = []
    for item in items[:limit]:
        if not isinstance(item, dict):
            continue
        compacted.append({key: item.get(key) for key in keys if key in item})
    return compacted


def _all_source_evidence_features(snapshot: dict[str, Any]) -> dict[str, Any]:
    context = snapshot.get("source_desk_context") or {}
    market_data = context.get("market_data_desk") or {}
    news = context.get("news_desk") or {}
    sec = context.get("sec_filings_desk") or {}
    financial = context.get("financial_statements_desk") or {}
    fred = context.get("fred_macro_desk") or {}
    technical = context.get("technical_indicators_desk") or {}
    external = context.get("external_geopolitical_news_desk") or {}
    external_features = _external_signal_features(snapshot)
    return {
        "event": {
            "headline": str(snapshot.get("headline") or "")[:260],
            "analysis_profile": str(snapshot.get("analysis_profile") or ""),
            "deterministic_event_hint": str(snapshot.get("deterministic_event_hint") or ""),
            "event_scope_hint": str(snapshot.get("event_scope_hint") or ""),
            "subject_entity_hint": str(snapshot.get("subject_entity_hint") or ""),
            "target_assets": snapshot.get("target_assets") or [],
            "required_prediction_horizons": snapshot.get("required_prediction_horizons") or [],
        },
        "market_data_desk": {
            "target_quote_available": bool(market_data.get("target_quote_available")),
            "delivery_quote_available": bool(market_data.get("delivery_quote_available")),
            "target_quotes": _compact_items(
                market_data.get("target_quotes"),
                ("symbol", "captured_at", "selected_price", "selected_price_source", "spread_ratio", "quote_integrity_status"),
            ),
            "delivery_quotes": _compact_items(
                market_data.get("delivery_quotes"),
                ("symbol", "captured_at", "selected_price", "selected_price_source", "spread_ratio", "quote_integrity_status"),
            ),
        },
        "news_desk": {
            "provider": str(news.get("provider") or snapshot.get("provider") or ""),
            "source_tier_hint": str(news.get("source_tier_hint") or snapshot.get("source_tier_hint") or ""),
            "article_body_available": bool(news.get("article_body_available") or snapshot.get("article_body_available_to_model")),
            "article_body_truncated": bool(news.get("article_body_truncated") or snapshot.get("article_body_truncated")),
            "timestamp_quality": str(news.get("timestamp_quality") or (snapshot.get("timestamp_provenance") or {}).get("timestamp_quality") or ""),
        },
        "sec_filings_desk": {
            "attached_count": int(sec.get("attached_count") or 0),
            "source_types": sec.get("source_types") or [],
            "forms": sec.get("forms") or [],
            "materiality": _compact_items(sec.get("materiality"), ("title", "classification", "reason", "available_at")),
        },
        "financial_statements_desk": {
            "symbol": str(financial.get("symbol") or ""),
            "fact_count": int(financial.get("fact_count") or 0),
            "facts": _compact_items(
                financial.get("facts"),
                ("tag", "fiscal_year", "fiscal_period", "form", "filed_at", "period_end", "value"),
                limit=5,
            ),
        },
        "fred_macro_desk": {
            "series_count": int(fred.get("series_count") or 0),
            "observations": _compact_items(
                fred.get("observations"),
                ("series_id", "observation_date", "value", "realtime_start"),
                limit=8,
            ),
        },
        "technical_indicators_desk": {
            "symbol": str(technical.get("symbol") or ""),
            "indicator_count": int(technical.get("indicator_count") or 0),
            "indicators": _compact_items(
                technical.get("indicators"),
                ("symbol", "as_of_date", "price_source", "close", "return_1d", "return_5d", "rsi_14", "volatility_20"),
            ),
        },
        "external_geopolitical_news_desk": {
            "signal_count": int(external.get("signal_count") or external_features["signal_count"]),
            **external_features,
        },
    }


def _mismatch_type(predicted: str, actual: str) -> str:
    if predicted == actual:
        return "large_return_error"
    return f"predicted_{predicted}_actual_{actual}"


def _geopolitical_context(row: dict[str, Any], snapshot: dict[str, Any], evidence_features: dict[str, Any]) -> dict[str, Any]:
    event = evidence_features.get("event") or {}
    external = evidence_features.get("external_geopolitical_news_desk") or {}
    news = evidence_features.get("news_desk") or {}
    market = evidence_features.get("market_data_desk") or {}
    technical = evidence_features.get("technical_indicators_desk") or {}
    event_text = " ".join(
        str(value or "")
        for value in (
            row.get("event_type"),
            row.get("event_subtype"),
            snapshot.get("headline"),
            snapshot.get("analysis_profile"),
            snapshot.get("deterministic_event_hint"),
            snapshot.get("event_scope_hint"),
            event.get("headline"),
            event.get("analysis_profile"),
            event.get("deterministic_event_hint"),
            event.get("event_scope_hint"),
        )
    )
    themes = external.get("themes") or {}
    regions = external.get("regions") or {}
    assets = set(str(asset).upper() for asset in (external.get("assets") or []) if str(asset).strip())
    target_assets = set(str(asset).upper() for asset in (event.get("target_assets") or snapshot.get("target_assets") or []) if str(asset).strip())
    if row.get("symbol"):
        target_assets.add(str(row.get("symbol")).upper())
    source_count = sum(int(count or 0) for count in (external.get("sources") or {}).values())
    market_statuses = external.get("market_reaction_statuses") or {}
    market_confirmed = bool(int(market_statuses.get("available") or 0))
    if not market_confirmed:
        market_confirmed = bool(market.get("target_quote_available")) or int(technical.get("indicator_count") or 0) > 0
    region_names = sorted(name for name, count in regions.items() if count and name in GEOPOLITICAL_REGIONS)
    theme_names = sorted(name for name, count in themes.items() if count and name in GEOPOLITICAL_THEMES)
    is_geopolitical = (
        str(row.get("event_type") or "").lower() == "geopolitical"
        or str(snapshot.get("analysis_profile") or "").lower() == "geopolitical"
        or str(snapshot.get("deterministic_event_hint") or "").lower() == "geopolitical"
        or bool(region_names)
        or bool(theme_names)
        or bool(GEOPOLITICAL_TEXT_PATTERN.search(event_text))
    )
    return {
        "is_geopolitical": is_geopolitical,
        "regions": region_names or sorted(name for name, count in regions.items() if count and name != "unknown"),
        "themes": theme_names or sorted(name for name, count in themes.items() if count and name != "unknown"),
        "mapped_assets": sorted(assets),
        "target_assets": sorted(target_assets),
        "asset_overlap": sorted(assets.intersection(target_assets)),
        "source_count": source_count,
        "market_confirmed": market_confirmed,
        "market_reaction_statuses": market_statuses,
        "news_provider": news.get("provider"),
        "source_tier_hint": news.get("source_tier_hint"),
    }


def _geopolitical_reason_for(
    row: dict[str, Any],
    snapshot: dict[str, Any],
    evidence_features: dict[str, Any],
    geo: dict[str, Any],
) -> tuple[str, str, str]:
    predicted = str(row.get("predicted_direction") or "unknown")
    actual = str(row.get("actual_direction") or "unknown")
    horizon = str(row.get("horizon") or "").upper()
    headline = str(snapshot.get("headline") or (evidence_features.get("event") or {}).get("headline") or "")
    regions = geo.get("regions") or ["unknown_region"]
    themes = geo.get("themes") or ["unknown_theme"]
    mapped_assets = geo.get("mapped_assets") or []
    overlap = geo.get("asset_overlap") or []
    market_confirmed = bool(geo.get("market_confirmed"))
    source_count = int(geo.get("source_count") or 0)

    if source_count <= 0:
        category = "geopolitical_source_confirmation_weak"
        summary = (
            "The event looked geopolitical, but the stored external-source desk did not provide corroborating "
            "pre-event Marketaux/Alpha/external signals. The miss should be treated as weak-source-confirmation "
            "rather than a clean geopolitical channel failure."
        )
        action = (
            "Do not convert geopolitical-looking headlines into strong forecasts unless at least one reliable "
            "source desk confirms the same region/theme before the event cutoff."
        )
    elif not market_confirmed:
        category = "geopolitical_market_confirmation_missing"
        summary = (
            "Geopolitical source context existed, but market/proxy confirmation was missing or unavailable. "
            "The mapped channel may not have been actively repriced at the forecast horizon."
        )
        action = (
            "Require confirmation from relevant proxies such as VIX/GLD/USO/XLE/SMH/QQQ/FX/rates before "
            "promoting the geopolitical signal from observe-only to directional."
        )
    elif mapped_assets and not overlap:
        category = "geopolitical_asset_map_wrong"
        summary = (
            "The geopolitical source desk mapped the event to assets that did not overlap the scored target. "
            "The forecast target may have been the wrong transmission channel for this region/theme."
        )
        action = (
            "Review the region/theme asset map and prefer the mapped proxy basket over an unrelated delivery "
            "symbol or broad target when scoring similar events."
        )
    elif actual == "flat" and predicted in {"up", "down"}:
        category = "geopolitical_risk_absorbed_no_edge"
        summary = (
            "The scorer expected a directional geopolitical reaction, but the realized outcome was flat. "
            "This often means the risk was already priced, the event was limited/non-escalatory, supply-chain "
            "impact was unconfirmed, or offsetting macro/earnings forces dominated."
        )
        action = (
            "For similar geopolitical headlines, raise the flat/observe-only threshold unless both source "
            "confirmation and mapped market-proxy confirmation are present."
        )
    elif predicted == "flat" and actual in {"up", "down"}:
        category = "geopolitical_channel_underweighted"
        summary = (
            "The scorer treated the geopolitical event as no-edge, but the realized market reaction was "
            "directional. A region/theme channel such as energy, volatility, semiconductors, defense, FX, "
            "or rates may have been underweighted."
        )
        action = (
            "When multiple sources point to the same geopolitical region/theme and mapped proxies are active, "
            "increase the probability of the relevant risk-on/risk-off channel instead of defaulting to flat."
        )
    else:
        category = "geopolitical_offset_by_macro_or_earnings"
        summary = (
            "The stored geopolitical context and realized reaction diverged in direction. The likely cause is "
            "an offset from non-geopolitical drivers such as rates, earnings, AI/sector momentum, liquidity, "
            "or positioning."
        )
        action = (
            "Store this as an interaction case and require the next scorer to compare geopolitical source "
            "strength against macro, earnings, market-data, and technical context before assigning direction."
        )

    summary += (
        f" Region(s)={regions}; theme(s)={themes}; mapped_assets={mapped_assets}; "
        f"target_overlap={overlap}; market_confirmed={market_confirmed}; source_count={source_count}."
    )
    if headline:
        summary += f" Headline: {headline[:180]}"
    if horizon in {"30M", "2H"}:
        action += " Short intraday horizons should stay low-confidence unless the channel is already visible in mapped proxies."
    return category, summary[:2000], action[:2000]


def _reason_for(row: dict[str, Any], snapshot: dict[str, Any], evidence_features: dict[str, Any]) -> tuple[str, str, str]:
    predicted = str(row.get("predicted_direction") or "unknown")
    actual = str(row.get("actual_direction") or "unknown")
    event_type = str(row.get("event_type") or "").lower()
    event_subtype = str(row.get("event_subtype") or "").lower()
    horizon = str(row.get("horizon") or "").upper()
    headline = str(snapshot.get("headline") or "")
    rationale = str(row.get("rationale") or "")
    external_features = evidence_features.get("external_geopolitical_news_desk") or {}
    external_sentiments = external_features.get("sentiments") or {}
    source_counts = {
        "sec": (evidence_features.get("sec_filings_desk") or {}).get("attached_count"),
        "financial": (evidence_features.get("financial_statements_desk") or {}).get("fact_count"),
        "fred": (evidence_features.get("fred_macro_desk") or {}).get("series_count"),
        "technical": (evidence_features.get("technical_indicators_desk") or {}).get("indicator_count"),
        "external": external_features.get("signal_count"),
    }
    geo = _geopolitical_context(row, snapshot, evidence_features)

    if geo["is_geopolitical"]:
        return _geopolitical_reason_for(row, snapshot, evidence_features, geo)

    if actual == "flat":
        category = "surprise_absorbed_no_edge"
        summary = (
            "The stored scorer expected a directional reaction, but the realized outcome was flat. "
            "This is a candidate for neutral/no-edge calibration: the event may have been priced in, "
            "offset by positioning, or lacked enough fresh surprise for the selected horizon."
        )
        action = (
            "For similar low-confirmation setups, increase the flat probability and require stronger "
            "evidence before assigning a directional forecast."
        )
    elif predicted == "up" and actual == "down":
        category = "sell_the_news_or_negative_offset"
        summary = (
            "The scorer read the event as positive, but the market reaction was negative. "
            "Possible explanations include sell-the-news positioning, weak forward-looking details, "
            "valuation/implied-move pressure, or a negative macro/sector offset not captured strongly enough."
        )
        action = (
            "Lower positive-score conversion when the text is mixed, when guidance/quality is uncertain, "
            "or when external/technical desks do not confirm a positive channel."
        )
    elif predicted == "down" and actual == "up":
        category = "bad_news_absorbed_or_offset_by_relief"
        summary = (
            "The scorer expected a negative reaction, but the market moved up. "
            "Possible explanations include the bad news being less severe than feared, relief versus consensus, "
            "pre-positioned shorts, dovish/rate offsets, or stronger simultaneous risk appetite."
        )
        action = (
            "For apparently negative macro/company events, distinguish absolute bad news from surprise versus "
            "expectations and add an offset/relief pathway before forcing a down forecast."
        )
    elif event_type in {"macro", "cpi", "fomc", "employment"} or "macro" in event_subtype:
        category = "macro_surprise_vs_market_interpretation"
        summary = (
            "Macro releases can diverge from textbook direction because the market reacts to surprise versus "
            "consensus, revisions, rates path, real yields, risk positioning, and policy interpretation rather "
            "than the headline level alone."
        )
        action = (
            "Require explicit actual/consensus/prior/revision evidence and rate-market context before using "
            "a simple hot-equals-down or cool-equals-up mapping."
        )
    else:
        category = "unexplained_directional_mismatch"
        summary = (
            "The scorer direction did not match the realized reaction. The stored snapshot should be reviewed "
            "for missing expectations, positioning, event-stage, sector context, or simultaneous market drivers."
        )
        action = "Keep as review data and avoid promoting a rule until similar misses repeat across enough rows."

    if external_sentiments:
        summary += f" External pre-event signal mix was {external_sentiments}."
    summary += f" Evidence-source coverage was {source_counts}."
    if horizon in {"30M", "2H"}:
        action += " Treat this especially carefully because short intraday horizons are sensitive to liquidity and timing."
    if headline:
        summary += f" Headline: {headline[:180]}"
    if rationale:
        summary += f" Stored scorer rationale: {rationale[:180]}"
    return category, summary[:2000], action[:2000]


def make_surprise_adjustment_note(row: dict[str, Any]) -> SurpriseAdjustmentNote:
    snapshot = _load_snapshot(row)
    evidence_features = _all_source_evidence_features(snapshot)
    geopolitical_context = _geopolitical_context(row, snapshot, evidence_features)
    predicted = str(row.get("predicted_direction") or "unknown")
    actual = str(row.get("actual_direction") or "unknown")
    reason_category, reason_summary, recommended_adjustment = _reason_for(row, snapshot, evidence_features)
    feature_hints = {
        "event_type": str(row.get("event_type") or ""),
        "event_subtype": str(row.get("event_subtype") or ""),
        "horizon": str(row.get("horizon") or ""),
        "session": str(row.get("session") or ""),
        "price_source": str(row.get("price_source") or ""),
        "confidence": _safe_float(row.get("confidence")),
        "absolute_raw_error": _safe_float(row.get("absolute_raw_error")),
        "absolute_excess_error": _safe_float(row.get("absolute_excess_error")),
        "evidence_sources": evidence_features,
        "geopolitical_context": geopolitical_context,
    }
    evidence = evidence_features
    return SurpriseAdjustmentNote(
        score_id=int(row["score_id"]),
        outcome_id=int(row["outcome_id"]),
        replay_corpus_id=int(row["replay_corpus_id"]),
        symbol=str(row.get("symbol") or "").upper(),
        event_type=str(row.get("event_type") or ""),
        event_subtype=str(row.get("event_subtype") or ""),
        replay_timestamp=str(row.get("replay_timestamp") or ""),
        horizon=str(row.get("horizon") or "").upper(),
        price_source=str(row.get("price_source") or ""),
        scorer_name=str(row.get("scorer_name") or ""),
        model_name=str(row.get("model_name") or ""),
        scorer_version=str(row.get("scorer_version") or ""),
        predicted_direction=predicted,
        actual_direction=actual,
        expected_return=float(row.get("expected_return") or 0.0),
        actual_raw_return=_safe_float(row.get("actual_raw_return")),
        actual_excess_return=_safe_float(row.get("actual_excess_return")),
        mismatch_type=_mismatch_type(predicted, actual),
        reason_category=reason_category,
        reason_summary=reason_summary,
        recommended_adjustment=recommended_adjustment,
        feature_hints=feature_hints,
        evidence=evidence,
        raw={"source": "deterministic_surprise_adjustment_rulebook"},
    )


def _extract_openai_parsed(response: Any) -> OpenAISurpriseCauseAnalysis:
    direct = getattr(response, "output_parsed", None)
    if isinstance(direct, OpenAISurpriseCauseAnalysis):
        return direct
    refusal_messages: list[str] = []
    for output in getattr(response, "output", []) or []:
        for content in getattr(output, "content", []) or []:
            parsed = getattr(content, "parsed", None)
            if isinstance(parsed, OpenAISurpriseCauseAnalysis):
                return parsed
            if getattr(content, "type", None) == "refusal":
                refusal_messages.append(str(getattr(content, "refusal", "refused")))
    if refusal_messages:
        raise PermanentAnalysisError("Model refused surprise adjustment request: " + "; ".join(refusal_messages))
    raise TransientAnalysisError("Structured response did not contain an OpenAISurpriseCauseAnalysis")


def _map_openai_exception(exc: Exception) -> Exception:
    name = exc.__class__.__name__
    message = str(exc)[:1500]
    status = getattr(exc, "status_code", None)
    if name in {"APIConnectionError", "APITimeoutError", "RateLimitError", "InternalServerError"} or (
        isinstance(status, int) and status >= 500
    ):
        return TransientAnalysisError(f"OpenAI transient surprise-adjustment error ({name}): {message}")
    if name == "AuthenticationError":
        return PermanentAnalysisError("OpenAI surprise adjustment authentication failed. Check OPENAI_API_KEY.")
    if name in {"PermissionDeniedError", "NotFoundError", "UnprocessableEntityError", "BadRequestError"} or (
        isinstance(status, int) and 400 <= status < 500
    ):
        return PermanentAnalysisError(f"OpenAI surprise-adjustment request/configuration error ({name}): {message}")
    return TransientAnalysisError(f"OpenAI unexpected surprise-adjustment error ({name}): {message}")


class OpenAISurpriseAdjustmentAnalyzer:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        timeout_seconds: float = 90.0,
        reasoning_effort: str = "low",
        max_input_chars: int = 18000,
        client: Any | None = None,
    ) -> None:
        if not api_key and client is None:
            raise PermanentAnalysisError("OPENAI_API_KEY is missing. Add it to .env before running OpenAI surprise adjustments.")
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
            self.client = OpenAI(api_key=api_key, timeout=timeout_seconds)

    def analyze(self, row: dict[str, Any], deterministic: SurpriseAdjustmentNote) -> SurpriseAdjustmentNote:
        payload = {
            "task": (
                "Post-outcome diagnostic only. Explain why a replay scorer forecast differed from the "
                "realized reaction using the full evidence-source bundle. Do not overwrite labels, do not "
                "give trading instructions, and do not claim live causality beyond the evidence."
            ),
            "score": {
                "symbol": row.get("symbol"),
                "event_type": row.get("event_type"),
                "event_subtype": row.get("event_subtype"),
                "horizon": row.get("horizon"),
                "session": row.get("session"),
                "predicted_direction": row.get("predicted_direction"),
                "actual_direction": row.get("actual_direction"),
                "expected_return": row.get("expected_return"),
                "actual_raw_return": row.get("actual_raw_return"),
                "actual_excess_return": row.get("actual_excess_return"),
                "scorer_version": row.get("scorer_version"),
                "scorer_rationale": row.get("rationale"),
            },
            "deterministic_seed": {
                "reason_category": deterministic.reason_category,
                "reason_summary": deterministic.reason_summary,
                "recommended_adjustment": deterministic.recommended_adjustment,
            },
            "evidence_sources": deterministic.evidence,
            "allowed_reason_categories": [
                "surprise_absorbed_no_edge",
                "sell_the_news_or_negative_offset",
                "bad_news_absorbed_or_offset_by_relief",
                "macro_surprise_vs_market_interpretation",
                "source_quality_or_timestamp_issue",
                "missing_expectations_or_consensus",
                "multi_source_conflict",
                "technical_positioning_or_gap_effect",
                "unexplained_directional_mismatch",
                *GEOPOLITICAL_REASON_CATEGORIES,
            ],
        }
        text = json.dumps(payload, ensure_ascii=False, default=str)[: self.max_input_chars]
        try:
            response = self.client.responses.parse(
                model=self.model,
                input=[
                    {
                        "role": "system",
                        "content": (
                            "You are a financial replay error diagnostician. Analyze stored replay misses "
                            "after outcomes are known, using all provided evidence sources. Produce structured "
                            "diagnostic notes for future scorer design only."
                        ),
                    },
                    {"role": "user", "content": text},
                ],
                text_format=OpenAISurpriseCauseAnalysis,
                reasoning={"effort": self.reasoning_effort},
                store=False,
            )
            parsed = _extract_openai_parsed(response)
        except (PermanentAnalysisError, TransientAnalysisError):
            raise
        except (ValidationError, Exception) as exc:
            mapped = _map_openai_exception(exc)
            raise mapped from exc
        raw = {
            "source": "openai_surprise_adjustment_analysis",
            "deterministic_seed": deterministic.raw,
            "evidence_source_weights": [item.model_dump() for item in parsed.evidence_source_weights],
            "missing_evidence": parsed.missing_evidence,
            "pattern_tags": parsed.pattern_tags,
            "scorer_rule_candidate": parsed.scorer_rule_candidate,
            "confidence": parsed.confidence,
        }
        return SurpriseAdjustmentNote(
            **{
                **deterministic.__dict__,
                "reason_category": parsed.reason_category,
                "reason_summary": parsed.reason_summary,
                "recommended_adjustment": parsed.recommended_adjustment,
                "feature_hints": {
                    **deterministic.feature_hints,
                    "openai_evidence_source_weights": [item.model_dump() for item in parsed.evidence_source_weights],
                    "openai_missing_evidence": parsed.missing_evidence,
                    "openai_pattern_tags": parsed.pattern_tags,
                    "openai_confidence": parsed.confidence,
                },
                "analysis_provider": "openai",
                "analysis_model": self.model,
                "raw": raw,
            }
        )


class ReplaySurpriseAdjustmentEngine:
    def __init__(self, db: Any, analyzer: OpenAISurpriseAdjustmentAnalyzer | None = None) -> None:
        self.db = db
        self.analyzer = analyzer

    def build(
        self,
        *,
        limit: int = 100,
        event_type: str | None = None,
        symbols: tuple[str, ...] | None = None,
        price_source: str | None = None,
        horizon: str | None = None,
        scorer_name: str | None = None,
        model_name: str | None = None,
        scorer_version: str | None = None,
        start: str | None = None,
        end: str | None = None,
        misses_only: bool = True,
        min_abs_error: float | None = None,
    ) -> dict[str, Any]:
        rows = self.db.replay_surprise_candidate_rows(
            limit=limit,
            event_type=event_type,
            symbols=symbols,
            price_source=price_source,
            horizon=horizon,
            scorer_name=scorer_name,
            model_name=model_name,
            scorer_version=scorer_version,
            start=start,
            end=end,
            misses_only=misses_only,
            min_abs_error=min_abs_error,
        )
        created = 0
        updated = 0
        categories: Counter[str] = Counter()
        errors: list[dict[str, Any]] = []
        for row in rows:
            deterministic = make_surprise_adjustment_note(row)
            note = deterministic
            if self.analyzer is not None:
                try:
                    note = self.analyzer.analyze(row, deterministic)
                except (PermanentAnalysisError, TransientAnalysisError) as exc:
                    errors.append(
                        {
                            "score_id": int(row.get("score_id") or 0),
                            "error": f"{exc.__class__.__name__}: {str(exc)[:500]}",
                        }
                    )
                    continue
            _, was_created = self.db.upsert_replay_surprise_adjustment_note(note)
            created += int(was_created)
            updated += int(not was_created)
            categories[note.reason_category] += 1
        return {
            "candidate_rows": len(rows),
            "created": created,
            "updated": updated,
            "categories": dict(categories),
            "adjustment_version": SURPRISE_ADJUSTMENT_VERSION,
            "analysis_provider": "openai" if self.analyzer is not None else "deterministic",
            "errors": errors,
        }


def summarize_adjustment_notes(rows: list[dict[str, Any]]) -> dict[str, Any]:
    categories = Counter(str(row.get("reason_category") or "unknown") for row in rows)
    pairs = Counter(str(row.get("mismatch_type") or "unknown") for row in rows)
    return {
        "rows": len(rows),
        "categories": dict(categories),
        "mismatch_types": dict(pairs),
    }


def format_adjustment_report(rows: list[dict[str, Any]]) -> str:
    summary = summarize_adjustment_notes(rows)
    lines = [
        "Replay surprise adjustment notes:",
        f"rows={summary['rows']} categories={summary['categories']}",
        "",
    ]
    if not rows:
        lines.append("No replay surprise adjustment notes match the selected filters.")
        return "\n".join(lines)
    for row in rows:
        lines.append(
            f"#{row['id']} {row['symbol']} {row['horizon']} {row['scorer_version']} "
            f"{row['mismatch_type']} category={row['reason_category']} "
            f"expected={float(row['expected_return']):+.2%} "
            f"actual={float(row['actual_raw_return']):+.2%} "
            f"summary={str(row['reason_summary'])[:220]}"
        )
    return "\n".join(lines)


def adjustment_markdown(
    rows: list[dict[str, Any]],
    *,
    filters: dict[str, Any] | None = None,
    title: str = "Replay Surprise Adjustment Notes",
) -> str:
    generated_at = datetime.now(UTC).isoformat()
    summary = summarize_adjustment_notes(rows)
    lines = [
        f"# {title}",
        "",
        f"- Generated at: `{generated_at}`",
        f"- Rows: `{summary['rows']}`",
        "- Safety: diagnostic notes only. Existing replay scores are not overwritten and no live orders are generated.",
        "",
        "## Filters",
        "",
    ]
    for key, value in (filters or {}).items():
        lines.append(f"- `{key}`: `{value if value is not None else '-'}`")
    if not filters:
        lines.append("- none")
    lines.extend(
        [
            "",
            "## Category counts",
            "",
            "| Category | Rows |",
            "| --- | ---: |",
        ]
    )
    if not summary["categories"]:
        lines.append("| - | 0 |")
    for category, count in sorted(summary["categories"].items()):
        lines.append(f"| {category} | {count} |")
    lines.extend(
        [
            "",
            "## Notes",
            "",
            "| Symbol | Horizon | Version | Mismatch | Category | Expected | Actual raw | Reason | Recommended adjustment |",
            "| --- | --- | --- | --- | --- | ---: | ---: | --- | --- |",
        ]
    )
    if not rows:
        lines.append("| - | - | - | - | - | - | - | - | - |")
    for row in rows:
        actual_raw = row.get("actual_raw_return")
        actual_text = "-" if actual_raw is None else f"{float(actual_raw):+.2%}"
        lines.append(
            f"| {row['symbol']} | {row['horizon']} | {row['scorer_version']} "
            f"| {row['mismatch_type']} | {row['reason_category']} "
            f"| {float(row['expected_return']):+.2%} | {actual_text} "
            f"| {str(row['reason_summary'])[:240]} | {str(row['recommended_adjustment'])[:220]} |"
        )
    return "\n".join(lines)


def write_adjustment_markdown(output_path: Path, rows: list[dict[str, Any]], *, filters: dict[str, Any] | None = None) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(adjustment_markdown(rows, filters=filters), encoding="utf-8")
    return output_path
