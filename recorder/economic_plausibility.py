from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

from .analysis_models import NewsAnalysis


@dataclass(frozen=True)
class EconomicPlausibilityConfig:
    enabled: bool = True
    target_max_price_multiple: float = 3.0
    target_min_price_multiple: float = 0.10
    target_max_change_ratio: float = 1.0
    fundamental_max_actual_expected_ratio: float = 5.0
    fundamental_max_actual_prior_ratio: float = 10.0


@dataclass(frozen=True)
class ParsedNumber:
    value: float
    currency: str
    is_percent: bool
    raw: str


_NUMBER_RE = re.compile(
    r"(?P<currency>[$€£¥])?\s*"
    r"(?P<number>[-+]?\d[\d,]*(?:\.\d+)?)\s*"
    r"(?P<scale>trillion|billion|million|thousand|[TBMK])?\s*"
    r"(?P<percent>%|percent|percentage\s+points?)?",
    re.IGNORECASE,
)

_SCALE = {
    "trillion": 1_000_000_000_000.0,
    "t": 1_000_000_000_000.0,
    "billion": 1_000_000_000.0,
    "b": 1_000_000_000.0,
    "million": 1_000_000.0,
    "m": 1_000_000.0,
    "thousand": 1_000.0,
    "k": 1_000.0,
}

_CURRENCY = {"$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY"}

_TARGET_TERMS = (
    "price target",
    "target price",
    "price objective",
    "pt raise",
    "pt cut",
)

_FUNDAMENTAL_TERMS = (
    "eps",
    "earnings per share",
    "revenue",
    "sales",
    "guidance",
    "outlook",
    "gross margin",
    "operating margin",
    "net margin",
    "free cash flow",
    "cash flow",
    "ebitda",
    "operating income",
    "net income",
)

_MARGIN_TERMS = ("margin",)
_SPLIT_TERMS = ("stock split", "reverse split", "split-adjusted", "split adjusted")


def _parse_first_number(value: str) -> ParsedNumber | None:
    text = str(value or "").strip()
    if not text or text.lower() in {"n/a", "na", "none", "not stated", "unknown", "-"}:
        return None
    match = _NUMBER_RE.search(text)
    if not match:
        return None
    try:
        number = float(match.group("number").replace(",", ""))
    except (TypeError, ValueError):
        return None
    scale = str(match.group("scale") or "").lower()
    number *= _SCALE.get(scale, 1.0)
    currency = _CURRENCY.get(str(match.group("currency") or ""), "unknown")
    percent = bool(match.group("percent"))
    return ParsedNumber(number, currency, percent, match.group(0).strip())


def _reference_price(snapshot: dict[str, Any], symbol: str) -> tuple[float | None, str, str]:
    symbol = symbol.upper()
    for row in snapshot.get("baseline_market_snapshot") or []:
        if str(row.get("symbol") or "").upper() != symbol:
            continue
        for key in ("selected_price", "midpoint", "last", "ask", "bid"):
            value = row.get(key)
            try:
                parsed = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(parsed) and parsed > 0:
                source = str(row.get("selected_price_source") or key)
                currency = str(row.get("currency") or "unknown").upper()
                return parsed, source, currency
    return None, "unavailable", "unknown"


def _source_confirmation_count(snapshot: dict[str, Any]) -> int:
    official = snapshot.get("official_evidence") or []
    official_count = sum(1 for item in official if item.get("content"))
    # The current article itself counts as one source. Attached official evidence is
    # counted separately because it provides an independent primary document.
    return 1 + official_count


def _immutable_text(snapshot: dict[str, Any]) -> str:
    chunks = [
        str(snapshot.get("headline") or ""),
        str(snapshot.get("article_body") or ""),
    ]
    for item in snapshot.get("official_evidence") or []:
        chunks.append(str(item.get("title") or ""))
        chunks.append(str(item.get("content") or ""))
    return " ".join(chunks).lower()


def _split_status(snapshot: dict[str, Any], anomaly_detected: bool) -> str:
    text = _immutable_text(snapshot)
    mentioned = any(term in text for term in _SPLIT_TERMS)
    official = bool(snapshot.get("official_evidence"))
    if mentioned and official:
        return "confirmed"
    if mentioned:
        return "mentioned_unverified"
    if anomaly_detected:
        return "unknown"
    return "not_required"


def _append_unique(values: list[str], value: str) -> None:
    if value and value not in values:
        values.append(value)


def _mark(
    analysis: NewsAnalysis,
    *,
    status: str,
    score: float,
    notes: list[str],
    reference_price: float | None,
    reference_source: str,
    currency: str,
    split_status: str,
    confirmations: int,
) -> NewsAnalysis:
    analysis.economic_plausibility_status = status  # type: ignore[assignment]
    analysis.economic_anomaly_score = max(0.0, min(1.0, score))
    analysis.economic_reference_price = reference_price
    analysis.economic_reference_price_source = reference_source
    analysis.economic_currency = currency
    analysis.split_adjustment_status = split_status
    analysis.source_confirmation_count = max(0, confirmations)
    analysis.economic_validation_notes = notes[:12]

    if status == "failed":
        _append_unique(analysis.interpretation_flags, "economic_plausibility_failed")
        _append_unique(
            analysis.data_limitations,
            "Economic plausibility anomaly detected; directional forecasts are disabled",
        )
    elif status == "warning":
        _append_unique(analysis.interpretation_flags, "economic_plausibility_warning")
        _append_unique(
            analysis.data_limitations,
            "Economic plausibility could not be fully confirmed; forecasts are calibration-only",
        )
    return analysis


def assess_economic_plausibility(
    analysis: NewsAnalysis,
    snapshot: dict[str, Any],
    config: EconomicPlausibilityConfig,
) -> NewsAnalysis:
    """Evaluate whether source-grounded numbers are economically plausible.

    Text integrity asks whether the model copied the source correctly. This layer asks
    whether the source number itself is credible relative to the event-time market
    snapshot, adjacent values, units, split context, and independent confirmation.
    It never deletes the model forecasts; prediction-quality gates decide whether they
    are directional, calibration-only, or observation-only.
    """
    if not config.enabled:
        return _mark(
            analysis,
            status="not_checked",
            score=0.0,
            notes=["Economic plausibility validation is disabled"],
            reference_price=None,
            reference_source="disabled",
            currency="unknown",
            split_status="unknown",
            confirmations=_source_confirmation_count(snapshot),
        )

    confirmations = _source_confirmation_count(snapshot)

    if analysis.attribution_status in {"mismatch", "multi_subject", "unknown", "unresolved"} or not analysis.subject_target_available:
        return _mark(
            analysis,
            status="not_applicable",
            score=0.0,
            notes=[
                "Economic validation skipped because the true article subject has no event-time target quote"
            ],
            reference_price=None,
            reference_source="not_available_for_actual_subject",
            currency="unknown",
            split_status="not_required",
            confirmations=confirmations,
        )

    target_rows = snapshot.get("target_quote_audit_snapshot") or snapshot.get("baseline_market_snapshot") or []
    if target_rows and any(
        str(row.get("quote_integrity_status") or "not_checked") == "failed"
        or (
            "reaction_baseline_eligible" in row
            and not bool(row.get("reaction_baseline_eligible"))
        )
        for row in target_rows
    ):
        return _mark(
            analysis,
            status="not_checked",
            score=0.0,
            notes=["Economic validation skipped because the attributed target quote failed integrity checks"],
            reference_price=None,
            reference_source="invalid_target_quote",
            currency="unknown",
            split_status="unknown",
            confirmations=confirmations,
        )

    reference_price, reference_source, contract_currency = _reference_price(snapshot, analysis.primary_symbol)
    notes: list[str] = []
    status = "not_applicable"
    score = 0.0
    currency = contract_currency
    anomaly_detected = False

    subtype_text = f"{analysis.event_subtype} {_immutable_text(snapshot)[:1200]}".lower()
    target_event = analysis.event_type == "analyst_action" and any(
        term in subtype_text for term in _TARGET_TERMS
    )

    if target_event:
        status = "valid"
        target_value: ParsedNumber | None = None
        prior_value: ParsedNumber | None = None
        for fact in analysis.key_facts:
            fact_name = fact.name.lower()
            if not any(term in fact_name for term in _TARGET_TERMS) and "target" not in fact_name:
                continue
            target_value = target_value or _parse_first_number(fact.actual)
            prior_value = prior_value or _parse_first_number(fact.prior)

        if target_value is None:
            status = "warning"
            score = max(score, 0.45)
            notes.append("Price-target event did not contain a parseable new target")
        else:
            currency = target_value.currency if target_value.currency != "unknown" else contract_currency
            if reference_price is None:
                status = "warning"
                score = max(score, 0.55)
                notes.append("Event-time reference price was unavailable")
            else:
                multiple = target_value.value / reference_price
                notes.append(
                    f"New target/reference multiple={multiple:.2f} "
                    f"({target_value.value:.4g}/{reference_price:.4g})"
                )
                if multiple >= config.target_max_price_multiple:
                    anomaly_detected = True
                    score = max(score, min(1.0, multiple / max(config.target_max_price_multiple, 0.01) / 2.0))
                    notes.append(
                        f"New target exceeds {config.target_max_price_multiple:.2f}x the event-time reference price"
                    )
                if multiple <= config.target_min_price_multiple:
                    anomaly_detected = True
                    score = max(score, 0.80)
                    notes.append(
                        f"New target is below {config.target_min_price_multiple:.2f}x the event-time reference price"
                    )

            if prior_value is not None:
                if currency == "unknown":
                    currency = prior_value.currency
                if (
                    target_value.currency != "unknown"
                    and prior_value.currency != "unknown"
                    and target_value.currency != prior_value.currency
                ):
                    anomaly_detected = True
                    score = max(score, 0.95)
                    notes.append("New and prior targets use different currencies")
                if prior_value.value != 0:
                    change_ratio = abs(target_value.value - prior_value.value) / abs(prior_value.value)
                    notes.append(f"Target change={change_ratio:.2%}")
                    if change_ratio >= config.target_max_change_ratio:
                        anomaly_detected = True
                        score = max(score, min(1.0, change_ratio / max(config.target_max_change_ratio, 0.01) / 2.0))
                        notes.append(
                            f"Target change exceeds {config.target_max_change_ratio:.0%} threshold"
                        )
                if reference_price is not None:
                    prior_multiple = prior_value.value / reference_price
                    notes.append(f"Prior target/reference multiple={prior_multiple:.2f}")
                    if (
                        prior_multiple >= config.target_max_price_multiple
                        or prior_multiple <= config.target_min_price_multiple
                    ):
                        anomaly_detected = True
                        score = max(score, 0.80)
                        notes.append("Prior target is also implausible relative to the event-time reference price")

        split_status = _split_status(snapshot, anomaly_detected)
        if anomaly_detected:
            # A confirmed split plus an attached independent source can explain a large
            # apparent price mismatch. Preserve such records for calibration, but do not
            # allow them to become directional trades without a normalized share basis.
            if split_status == "confirmed" and confirmations >= 2:
                status = "warning"
                notes.append("Large target mismatch may be split-related; normalized share basis is unverified")
            else:
                status = "failed"
                notes.append("Single-source or unadjusted target anomaly requires external confirmation")
        return _mark(
            analysis,
            status=status,
            score=score,
            notes=notes,
            reference_price=reference_price,
            reference_source=reference_source,
            currency=currency,
            split_status=split_status,
            confirmations=confirmations,
        )

    if analysis.event_type in {"earnings", "guidance"}:
        status = "valid"
        checks = 0
        hard_anomalies = 0
        warnings = 0
        for fact in analysis.key_facts:
            name = fact.name.lower()
            if not any(term in name for term in _FUNDAMENTAL_TERMS):
                continue
            actual = _parse_first_number(fact.actual)
            expected = _parse_first_number(fact.expected)
            prior = _parse_first_number(fact.prior)
            if actual is None:
                continue
            currency = actual.currency if actual.currency != "unknown" else currency

            if any(term in name for term in _MARGIN_TERMS) and actual.is_percent:
                checks += 1
                if actual.value > 100.0 or actual.value < -100.0:
                    hard_anomalies += 1
                    score = max(score, 0.95)
                    notes.append(f"{fact.name}: margin {actual.value:.2f}% is outside [-100%, 100%]")

            if expected is not None:
                checks += 1
                if (
                    actual.currency != "unknown"
                    and expected.currency != "unknown"
                    and actual.currency != expected.currency
                ):
                    hard_anomalies += 1
                    score = max(score, 0.95)
                    notes.append(f"{fact.name}: actual and expected currencies differ")
                denominator = abs(expected.value)
                if denominator >= 0.05:
                    ratio = max(abs(actual.value) / denominator, denominator / max(abs(actual.value), 0.05))
                    if ratio > config.fundamental_max_actual_expected_ratio:
                        hard_anomalies += 1
                        score = max(
                            score,
                            min(1.0, ratio / max(config.fundamental_max_actual_expected_ratio, 0.01) / 2.0),
                        )
                        notes.append(
                            f"{fact.name}: actual/expected magnitude ratio {ratio:.2f} exceeds "
                            f"{config.fundamental_max_actual_expected_ratio:.2f}"
                        )

            if prior is not None and abs(prior.value) >= 0.05:
                checks += 1
                ratio = max(
                    abs(actual.value) / abs(prior.value),
                    abs(prior.value) / max(abs(actual.value), 0.05),
                )
                if ratio > config.fundamental_max_actual_prior_ratio:
                    warnings += 1
                    score = max(
                        score,
                        min(0.85, ratio / max(config.fundamental_max_actual_prior_ratio, 0.01) / 3.0),
                    )
                    notes.append(
                        f"{fact.name}: actual/prior magnitude ratio {ratio:.2f} exceeds "
                        f"{config.fundamental_max_actual_prior_ratio:.2f}"
                    )

        if checks == 0:
            status = "not_applicable"
        elif hard_anomalies:
            status = "failed"
        elif warnings:
            status = "warning"
        split_status = _split_status(snapshot, status in {"warning", "failed"})
        return _mark(
            analysis,
            status=status,
            score=score,
            notes=notes,
            reference_price=reference_price,
            reference_source=reference_source,
            currency=currency,
            split_status=split_status,
            confirmations=confirmations,
        )

    return _mark(
        analysis,
        status="not_applicable",
        score=0.0,
        notes=[],
        reference_price=reference_price,
        reference_source=reference_source,
        currency=contract_currency,
        split_status="not_required",
        confirmations=confirmations,
    )
