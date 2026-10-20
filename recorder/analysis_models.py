from __future__ import annotations

import math
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Horizon = Literal["15m", "1h", "close", "1d", "3d"]
Sentiment = Literal[
    "strong_negative", "negative", "neutral", "mixed", "positive", "strong_positive"
]
EventType = Literal[
    "earnings", "guidance", "analyst_action", "fomc", "fed_speech", "macro",
    "cpi", "employment", "m_and_a", "regulatory", "legal", "financing",
    "securities_filing", "ownership", "product", "supply_chain", "geopolitical",
    "market_price_update", "corporate_action", "investor_event", "other",
]
Priority = Literal["low", "normal", "high", "urgent"]
TimeHorizon = Literal["intraday", "1_to_3_days", "1_to_4_weeks", "long_term", "uncertain"]
EventStage = Literal[
    "pre_event", "initial_release", "follow_up", "press_conference", "retrospective",
    "regulatory_filing", "unknown",
]
SurpriseDirection = Literal[
    "strong_negative", "negative", "neutral", "mixed", "positive", "strong_positive", "unknown"
]
AnalysisDepth = Literal["headline_only", "article_body", "event_document"]
FactDirection = Literal["negative", "neutral", "mixed", "positive", "unknown"]
FactImportance = Literal["low", "medium", "high", "critical"]
EventScope = Literal["company", "sector", "macro", "market", "global", "unknown"]
SourceTier = Literal["official_primary", "reputable_secondary", "secondary", "unknown"]
OfficialSourceStatus = Literal["attached", "candidate_found", "not_available"]
TextIntegrityStatus = Literal["valid", "warning", "failed", "not_checked"]
EconomicPlausibilityStatus = Literal["not_checked", "not_applicable", "valid", "warning", "failed"]
IntegrityRecoveryStatus = Literal[
    "not_attempted",
    "not_needed_integrity_valid",
    "not_attempted_disabled",
    "not_attempted_low_impact",
    "not_attempted_low_signal",
    "not_attempted_retrospective",
    "not_attempted_attribution_mismatch",
    "not_attempted_no_recovery_model",
    "recovered",
    "partial",
    "hard_failed",
    "unavailable",
]
AssetType = Literal["stock", "etf", "index", "market_event", "macro_event", "sector_event", "unknown"]
TargetRole = Literal[
    "subject_asset", "market_proxy", "sector_proxy", "delivery_proxy",
    "unavailable_subject", "unknown",
]
# ``unresolved`` is retained for v0.13 rows. ``pending`` is an in-memory
# provider-output state only: the service must replace it with the immutable
# deterministic attribution from the input snapshot before persistence.
# v0.14 also adds explicit safe states for market-wide and genuinely
# multi-company articles.
AttributionStatus = Literal[
    "pending", "matched", "delivery_proxy", "mismatch", "unresolved", "unknown",
    "multi_subject", "market_level",
]

_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,14}$")

_SEMANTIC_EVENT_SUBJECTS = {
    "US_EQUITIES",
    "NASDAQ_100",
    "US_SMALL_CAPS",
    "SEMICONDUCTORS",
    "TECHNOLOGY",
    "LONG_DURATION_TREASURIES",
    "EQUITY_VOLATILITY",
    "PCE",
    "CPI",
    "EMPLOYMENT",
    "GDP",
    "RETAIL_SALES",
    "ISM",
    "FOMC",
    "MACRO_MARKET_CONTEXT",
}


def _clean_symbol(value: str) -> str:
    value = value.strip().upper()
    if not _SYMBOL_RE.fullmatch(value):
        raise ValueError(f"Invalid ticker symbol: {value!r}")
    return value


class EventFact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    actual: str = Field(default="", max_length=160)
    expected: str = Field(default="", max_length=160)
    prior: str = Field(default="", max_length=160)
    unit: str = Field(default="", max_length=60)
    direction: FactDirection = "unknown"
    importance: FactImportance = "medium"
    evidence: str = Field(default="", max_length=500)

    @field_validator("name", "actual", "expected", "prior", "unit", "evidence")
    @classmethod
    def clean_text(cls, value: str) -> str:
        return " ".join(str(value).split())


class HorizonPrediction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str
    horizon: Horizon
    expected_return: float = Field(ge=-1.0, le=5.0)
    bear_return: float = Field(ge=-1.0, le=5.0)
    bull_return: float = Field(ge=-1.0, le=5.0)
    up_probability: float = Field(ge=0.0, le=1.0)
    flat_probability: float = Field(ge=0.0, le=1.0)
    down_probability: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("symbol")
    @classmethod
    def validate_symbol(cls, value: str) -> str:
        return _clean_symbol(value)

    @model_validator(mode="after")
    def validate_distribution_and_range(self) -> "HorizonPrediction":
        values = (self.up_probability, self.flat_probability, self.down_probability)
        if not all(math.isfinite(v) for v in values):
            raise ValueError("Probabilities must be finite")
        total = sum(values)
        if total <= 0:
            raise ValueError("Probability total must be positive")
        if abs(total - 1.0) > 0.03:
            raise ValueError(f"Probabilities must sum to 1, got {total:.6f}")
        self.up_probability /= total
        self.flat_probability /= total
        self.down_probability /= total
        if self.bear_return > self.expected_return:
            raise ValueError("bear_return cannot exceed expected_return")
        if self.expected_return > self.bull_return:
            raise ValueError("expected_return cannot exceed bull_return")
        return self


class NewsAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    primary_symbol: str
    related_symbols: list[str] = Field(default_factory=list, max_length=16)
    event_scope: EventScope = "company"
    subject_entity: str = Field(default="", max_length=120)
    event_subject_symbol: str = Field(
        default="",
        max_length=15,
        description=(
            "Tradable ticker symbol for a company subject only. Use an empty string "
            "for semantic market, macro, index, or sector subjects such as nasdaq_100, "
            "us_equities, pce, fomc, or semiconductors."
        ),
    )
    subject_asset_type: AssetType = "unknown"
    target_asset_type: AssetType = "unknown"
    target_role: TargetRole = "unknown"
    target_assets: list[str] = Field(default_factory=list, max_length=16)
    attribution_status: AttributionStatus = "pending"
    attribution_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    original_subscription_symbol: str = Field(default="", max_length=15)
    subject_target_available: bool = True
    attribution_reason: str = Field(default="", max_length=800)
    article_scope: str = Field(default="unknown", max_length=40)
    article_primary_subject: str = Field(default="", max_length=120)
    mentioned_symbols: list[str] = Field(default_factory=list, max_length=32)
    subscription_entity_role: str = Field(default="delivery_route", max_length=40)
    target_quote_integrity_status: str = Field(default="not_available", max_length=40)
    target_quote_integrity_reasons: list[str] = Field(default_factory=list, max_length=16)
    delivery_reference_symbol: str = Field(default="", max_length=15)
    delivery_reference_price: float | None = Field(default=None, gt=0.0)
    delivery_reference_price_source: str = Field(default="", max_length=80)
    event_type: EventType
    event_subtype: str = Field(min_length=1, max_length=120)
    event_stage: EventStage = "unknown"
    analysis_depth: AnalysisDepth = "headline_only"
    surprise_direction: SurpriseDirection = "unknown"
    market_expectation_context: str = Field(default="", max_length=1200)
    key_facts: list[EventFact] = Field(default_factory=list, max_length=20)
    interpretation_flags: list[str] = Field(default_factory=list, max_length=16)
    sentiment: Sentiment
    causal_signal: bool
    prediction_eligible: bool
    reaction_observation_eligible: bool = False
    analysis_priority: Priority
    impact_score: float = Field(ge=0.0, le=1.0)
    novelty_score: float = Field(ge=0.0, le=1.0)
    source_quality: float = Field(ge=0.0, le=1.0)
    source_tier: SourceTier = "unknown"
    official_source_status: OfficialSourceStatus = "not_available"
    confidence: float = Field(ge=0.0, le=1.0)
    time_horizon: TimeHorizon
    summary: str = Field(min_length=1, max_length=1800)
    bull_case: list[str] = Field(default_factory=list, max_length=8)
    bear_case: list[str] = Field(default_factory=list, max_length=8)
    data_limitations: list[str] = Field(default_factory=list, max_length=14)
    text_integrity_status: TextIntegrityStatus = "not_checked"
    validated_fact_count: int = Field(default=0, ge=0, le=100)
    rejected_fact_count: int = Field(default=0, ge=0, le=100)
    fact_validation_notes: list[str] = Field(default_factory=list, max_length=12)
    integrity_recovery_status: IntegrityRecoveryStatus = "not_attempted"
    integrity_recovery_model: str = Field(default="", max_length=120)
    integrity_recovery_attempts: int = Field(default=0, ge=0, le=3)
    integrity_initial_status: TextIntegrityStatus = "not_checked"
    economic_plausibility_status: EconomicPlausibilityStatus = "not_checked"
    economic_anomaly_score: float = Field(default=0.0, ge=0.0, le=1.0)
    economic_reference_price: float | None = Field(default=None, gt=0.0)
    economic_reference_price_source: str = Field(default="", max_length=80)
    economic_currency: str = Field(default="unknown", max_length=16)
    split_adjustment_status: str = Field(default="unknown", max_length=40)
    source_confirmation_count: int = Field(default=0, ge=0, le=100)
    economic_validation_notes: list[str] = Field(default_factory=list, max_length=12)
    predictions: list[HorizonPrediction] = Field(default_factory=list, max_length=20)

    @field_validator("primary_symbol")
    @classmethod
    def validate_primary_symbol(cls, value: str) -> str:
        return _clean_symbol(value)

    @field_validator("related_symbols", "target_assets", "mentioned_symbols")
    @classmethod
    def validate_related_symbols(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        for value in values:
            symbol = _clean_symbol(value)
            if symbol not in cleaned:
                cleaned.append(symbol)
        return cleaned

    @field_validator("event_subject_symbol", mode="before")
    @classmethod
    def normalize_event_subject_symbol(cls, value: str) -> str:
        """Keep semantic subjects out of the ticker-only field.

        Structured-output models occasionally place values such as ``nasdaq_100``
        or ``us_equities`` in this field.  Those values belong in
        ``subject_entity`` and are deterministically restored from the event
        attribution snapshot after parsing.  Returning an empty string here
        prevents a recoverable schema mismatch from sending the queue job into
        repeated retries, while real ticker-looking values remain strictly
        validated.
        """
        normalized = str(value or "").strip().upper()
        if normalized in {"", "-", "NONE", "NULL", "N/A", "NA"}:
            return ""
        if normalized in _SEMANTIC_EVENT_SUBJECTS:
            return ""
        if _SYMBOL_RE.fullmatch(normalized):
            return normalized
        # Non-ticker semantic identifiers are valid event subjects, but not ticker
        # symbols.  The authoritative deterministic attribution will populate
        # subject_entity and overwrite this optional field after model parsing.
        return ""

    @field_validator("original_subscription_symbol", "delivery_reference_symbol")
    @classmethod
    def validate_optional_symbols(cls, value: str) -> str:
        value = str(value or "").strip().upper()
        return _clean_symbol(value) if value else ""

    @field_validator("event_subtype", "subject_entity", "integrity_recovery_model", "economic_reference_price_source", "economic_currency", "split_adjustment_status", "attribution_reason", "article_scope", "article_primary_subject", "subscription_entity_role", "target_quote_integrity_status", "delivery_reference_price_source")
    @classmethod
    def clean_short_text(cls, value: str) -> str:
        return " ".join(str(value).split())

    @field_validator("market_expectation_context", "summary")
    @classmethod
    def clean_long_text(cls, value: str) -> str:
        return " ".join(str(value).split())

    @field_validator("bull_case", "bear_case", "data_limitations", "interpretation_flags", "fact_validation_notes", "economic_validation_notes", "target_quote_integrity_reasons")
    @classmethod
    def clean_text_lists(cls, values: list[str]) -> list[str]:
        result: list[str] = []
        for value in values:
            cleaned = " ".join(str(value).split())[:500]
            if cleaned and cleaned not in result:
                result.append(cleaned)
        return result

    @model_validator(mode="after")
    def validate_prediction_policy(self) -> "NewsAnalysis":
        if self.target_role != "unavailable_subject" and self.primary_symbol not in self.target_assets:
            self.target_assets.insert(0, self.primary_symbol)
        # The provider model is parsed before the service applies the authoritative
        # deterministic attribution snapshot.  A default ``pending`` state must not
        # discard an otherwise valid forecast before that enrichment occurs.
        if self.attribution_status in {"mismatch", "multi_subject", "unknown", "unresolved"} or not self.subject_target_available:
            self.predictions = []
            self.prediction_eligible = False
            self.reaction_observation_eligible = False
        if not self.causal_signal or not self.prediction_eligible:
            self.predictions = []
            self.prediction_eligible = False
        if self.event_stage == "retrospective" and self.event_type != "market_price_update":
            self.reaction_observation_eligible = True
        seen: set[tuple[str, str]] = set()
        for prediction in self.predictions:
            key = (prediction.symbol, prediction.horizon)
            if key in seen:
                raise ValueError(f"Duplicate prediction: {key}")
            seen.add(key)
        return self


def validate_primary_horizons(
    analysis: NewsAnalysis,
    expected_symbol: str,
    required_horizons: tuple[str, ...],
) -> NewsAnalysis:
    expected_symbol = _clean_symbol(expected_symbol)
    normalized_horizons = tuple(h.lower() for h in required_horizons)
    analysis.primary_symbol = expected_symbol
    if expected_symbol not in analysis.related_symbols:
        analysis.related_symbols.insert(0, expected_symbol)
    if not analysis.prediction_eligible:
        analysis.predictions = []
        return analysis
    # A model may echo the IBKR subscription symbol. For deterministic macro attribution,
    # rewrite the prediction target to the event target while preserving the numeric forecast.
    rewritten: list[HorizonPrediction] = []
    for prediction in analysis.predictions:
        clone = prediction.model_copy(deep=True)
        clone.symbol = expected_symbol
        rewritten.append(clone)
    by_horizon = {p.horizon: p for p in rewritten}
    missing = [h for h in normalized_horizons if h not in by_horizon]
    if missing:
        raise ValueError(f"Missing required primary-symbol predictions: {missing}")
    predictions = [by_horizon[h] for h in normalized_horizons]
    analysis.predictions = predictions
    return analysis
