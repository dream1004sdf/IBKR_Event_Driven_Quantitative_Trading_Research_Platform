from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from rapidfuzz.fuzz import partial_ratio

from .analysis_models import EventFact, NewsAnalysis

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_REPEAT_PUNCT_RE = re.compile(r"([,.;:!?])\1+")
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_NUM_RE = re.compile(r"(?<![A-Za-z])[-+]?\$?\d[\d,]*(?:\.\d+)?%?")
_TRAILING_FRAGMENT_RE = re.compile(
    r"(?:\b(?:and|or|the|a|an|of|to|with|by|for|from|at|in|been|being|was|were|is|are|has|have|had)|[,;:/-])$",
    re.I,
)
_PLACEHOLDER_VALUES = {
    "", "n/a", "na", "not available", "not stated", "unknown", "none", "not specified",
}
_HIGH_IMPORTANCE = {"high", "critical"}


@dataclass(frozen=True)
class IntegrityResult:
    status: str  # valid | warning | failed
    notes: tuple[str, ...]
    validated_fact_count: int
    rejected_fact_count: int
    validated_high_importance_count: int
    unsupported_numeric_count: int
    rejected_ratio: float


def sanitize_text(value: str, *, source_text: str = "") -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = _CONTROL_RE.sub(" ", text)
    text = _REPEAT_PUNCT_RE.sub(r"\1", text)
    text = re.sub(r"\s+", " ", text).strip()

    # A single unexpected CJK glyph embedded in an otherwise Latin response is usually
    # encoding/model corruption (for example ``closed a航``). Preserve genuine CJK only
    # when the immutable input itself contains CJK.
    if not _CJK_RE.search(source_text):
        text = _CJK_RE.sub("", text)
        text = re.sub(r"\s+", " ", text).strip()
    return text


def _normalized(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value).lower()).strip()


def _numeric_tokens(value: str) -> tuple[str, ...]:
    result: list[str] = []
    for token in _NUM_RE.findall(value):
        cleaned = token.lower().replace(",", "").replace("$", "")
        if cleaned not in result:
            result.append(cleaned)
    return tuple(result)


def _fact_support(value: str, source_text: str) -> tuple[bool, bool]:
    """Return ``(supported, numeric_mismatch)`` for an extracted fact value."""
    normalized_value = _normalized(value)
    if normalized_value in _PLACEHOLDER_VALUES:
        return True, False
    normalized_source = _normalized(source_text)
    source_numeric = normalized_source.replace(",", "").replace("$", "")
    numbers = _numeric_tokens(value)
    if numbers and not all(number in source_numeric for number in numbers):
        return False, True
    if normalized_value in normalized_source:
        return True, False
    # Paraphrases are acceptable only when the value is substantial and highly similar.
    if len(normalized_value) >= 12 and partial_ratio(normalized_value, normalized_source) >= 88:
        return True, False
    # Numeric facts whose numeric tokens are present can use a paraphrased label.
    return bool(numbers), False


def _sanitize_fact(fact: EventFact, source_text: str) -> EventFact:
    clone = fact.model_copy(deep=True)
    clone.name = sanitize_text(clone.name, source_text=source_text)
    clone.actual = sanitize_text(clone.actual, source_text=source_text)
    clone.expected = sanitize_text(clone.expected, source_text=source_text)
    clone.prior = sanitize_text(clone.prior, source_text=source_text)
    clone.unit = sanitize_text(clone.unit, source_text=source_text)
    clone.evidence = sanitize_text(clone.evidence, source_text=source_text)
    return clone


def source_text_for_snapshot(snapshot: dict) -> str:
    source_parts: list[str] = [
        str(snapshot.get("headline") or ""),
        str(snapshot.get("article_body") or ""),
    ]
    for document in snapshot.get("official_evidence") or []:
        if isinstance(document, dict):
            source_parts.append(str(document.get("content") or ""))
    return "\n".join(source_parts)


def suppress_forecasts_for_integrity(
    analysis: NewsAnalysis,
    *,
    recovery_status: str = "hard_failed",
    reason: str = "Text integrity validation failed; forecasts were suppressed",
) -> None:
    analysis.text_integrity_status = "failed"
    analysis.integrity_recovery_status = recovery_status  # type: ignore[assignment]
    analysis.prediction_eligible = False
    analysis.predictions = []
    analysis.reaction_observation_eligible = True
    if reason not in analysis.data_limitations:
        analysis.data_limitations.append(reason)


def validate_analysis_text(
    analysis: NewsAnalysis,
    snapshot: dict,
    *,
    suppress_on_failure: bool = True,
) -> IntegrityResult:
    """Sanitize and ground model output against the immutable input snapshot.

    ``suppress_on_failure=False`` is used by the v0.9 recovery pipeline. It leaves
    otherwise valid predictions in memory long enough for a critical model to re-read
    the immutable source and produce a clean replacement. No unvalidated output is
    written to SQLite.
    """
    source_text = source_text_for_snapshot(snapshot)

    notes: list[str] = []
    suspicious_before = sum(
        1
        for text in (
            analysis.summary,
            *(fact.actual for fact in analysis.key_facts),
            *(fact.evidence for fact in analysis.key_facts),
        )
        if _CJK_RE.search(str(text)) and not _CJK_RE.search(source_text)
    )

    analysis.summary = sanitize_text(analysis.summary, source_text=source_text)
    analysis.market_expectation_context = sanitize_text(
        analysis.market_expectation_context, source_text=source_text
    )
    analysis.event_subtype = sanitize_text(analysis.event_subtype, source_text=source_text)
    analysis.subject_entity = sanitize_text(analysis.subject_entity, source_text=source_text)
    analysis.bull_case = [sanitize_text(x, source_text=source_text) for x in analysis.bull_case]
    analysis.bear_case = [sanitize_text(x, source_text=source_text) for x in analysis.bear_case]
    analysis.data_limitations = [
        sanitize_text(x, source_text=source_text) for x in analysis.data_limitations
    ]
    analysis.interpretation_flags = [
        sanitize_text(x, source_text=source_text) for x in analysis.interpretation_flags
    ]

    validated: list[EventFact] = []
    rejected = 0
    unsupported_numeric_count = 0
    for original in analysis.key_facts:
        fact = _sanitize_fact(original, source_text)
        values = [fact.actual, fact.expected, fact.prior]
        nonempty_values = [
            value for value in values if _normalized(value) not in _PLACEHOLDER_VALUES
        ]
        unsupported: list[str] = []
        numeric_mismatch = False
        for value in nonempty_values:
            supported, mismatch = _fact_support(value, source_text)
            if not supported:
                unsupported.append(value)
            numeric_mismatch = numeric_mismatch or mismatch
        fragment = any(_TRAILING_FRAGMENT_RE.search(value) for value in nonempty_values)
        if unsupported or fragment or not fact.name:
            rejected += 1
            if numeric_mismatch:
                unsupported_numeric_count += 1
            reason_parts: list[str] = []
            if numeric_mismatch:
                reason_parts.append("numeric value not present in immutable input")
            elif unsupported:
                reason_parts.append("value not grounded in immutable input")
            if fragment:
                reason_parts.append("truncated sentence fragment")
            if not fact.name:
                reason_parts.append("missing fact name")
            notes.append(
                f"rejected fact '{fact.name or 'unknown'}': {', '.join(reason_parts)}"
            )
            continue
        validated.append(fact)
    analysis.key_facts = validated

    if suspicious_before:
        notes.append(f"removed {suspicious_before} unexpected CJK/encoding artifact(s)")
    if rejected:
        notes.append(f"rejected {rejected} ungrounded or malformed fact(s)")

    analysis.validated_fact_count = len(validated)
    analysis.rejected_fact_count = rejected
    analysis.fact_validation_notes = notes[:12]

    total = len(validated) + rejected
    rejected_ratio = rejected / total if total else 0.0
    high_count = sum(1 for fact in validated if fact.importance in _HIGH_IMPORTANCE)

    # A missing summary or a prediction-bearing result whose rejected facts outnumber
    # the grounded facts is unsafe. v0.9 may recover it with a fresh critical-model pass.
    if not analysis.summary or (
        analysis.prediction_eligible and rejected >= max(2, len(validated) + 1)
    ):
        status = "failed"
    elif notes:
        status = "warning"
    else:
        status = "valid"
    analysis.text_integrity_status = status  # type: ignore[assignment]

    if status == "failed" and suppress_on_failure:
        suppress_forecasts_for_integrity(analysis)
    elif status == "warning":
        limitation = "Text integrity validator sanitized or rejected malformed output"
        if limitation not in analysis.data_limitations:
            analysis.data_limitations.append(limitation)

    return IntegrityResult(
        status=status,
        notes=tuple(notes),
        validated_fact_count=len(validated),
        rejected_fact_count=rejected,
        validated_high_importance_count=high_count,
        unsupported_numeric_count=unsupported_numeric_count,
        rejected_ratio=rejected_ratio,
    )
