from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import ValidationError

from .analysis_models import HorizonPrediction, NewsAnalysis
from .prompting import (
    build_user_prompt,
    is_obvious_market_price_update,
    system_prompt_for_profile,
)


class AnalysisProviderError(RuntimeError):
    """Base class for provider failures safe to persist in a job record."""


class TransientAnalysisError(AnalysisProviderError):
    """A retry may succeed: timeout, connection, rate limit, server error, malformed output."""


class PermanentAnalysisError(AnalysisProviderError):
    """Retrying without a configuration or request change is unlikely to succeed."""


@dataclass(frozen=True)
class ProviderResult:
    analysis: NewsAnalysis
    raw_json: str
    response_id: str | None
    input_tokens: int | None
    output_tokens: int | None
    duration_ms: int
    provider_name: str = "unknown"
    model_name: str = "unknown"
    route_name: str = "unknown"
    reasoning_effort: str = "unknown"


class NewsAnalyzer(Protocol):
    provider_name: str
    model_name: str

    def analyze(self, snapshot: dict[str, Any]) -> ProviderResult:
        ...


def _extract_usage(response: Any) -> tuple[int | None, int | None]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return None, None
    input_tokens = getattr(usage, "input_tokens", None)
    output_tokens = getattr(usage, "output_tokens", None)
    try:
        input_tokens = int(input_tokens) if input_tokens is not None else None
    except (TypeError, ValueError):
        input_tokens = None
    try:
        output_tokens = int(output_tokens) if output_tokens is not None else None
    except (TypeError, ValueError):
        output_tokens = None
    return input_tokens, output_tokens


def _extract_parsed(response: Any) -> NewsAnalysis:
    direct = getattr(response, "output_parsed", None)
    if isinstance(direct, NewsAnalysis):
        return direct

    refusal_messages: list[str] = []
    for output in getattr(response, "output", []) or []:
        for content in getattr(output, "content", []) or []:
            parsed = getattr(content, "parsed", None)
            if isinstance(parsed, NewsAnalysis):
                return parsed
            if getattr(content, "type", None) == "refusal":
                refusal_messages.append(str(getattr(content, "refusal", "refused")))

    if refusal_messages:
        raise PermanentAnalysisError("Model refused request: " + "; ".join(refusal_messages))
    raise TransientAnalysisError("Structured response did not contain a parsed NewsAnalysis")


class OpenAINewsAnalyzer:
    provider_name = "openai"

    def __init__(
        self,
        api_key: str,
        model: str,
        timeout_seconds: float,
        reasoning_effort: str = "low",
        profile_name: str = "general",
        client: Any | None = None,
    ) -> None:
        if not api_key and client is None:
            raise PermanentAnalysisError(
                "OPENAI_API_KEY is missing. Add it to .env before running ai-run."
            )
        if reasoning_effort not in {"none", "low", "medium", "high", "xhigh"}:
            raise PermanentAnalysisError(
                f"Unsupported reasoning effort {reasoning_effort!r}; use none, low, medium, high, or xhigh."
            )
        self.model_name = model
        self.reasoning_effort = reasoning_effort
        self.profile_name = profile_name
        self.log = logging.getLogger(f"{self.__class__.__name__}.{profile_name}")
        if client is not None:
            self.client = client
            return

        try:
            from openai import OpenAI
        except ImportError as exc:
            raise PermanentAnalysisError(
                "The openai package is not installed. Run pip install -r requirements.txt."
            ) from exc

        self.client = OpenAI(
            api_key=api_key,
            timeout=timeout_seconds,
            max_retries=0,  # Retry policy is centralized in the durable job queue.
        )

    def analyze(self, snapshot: dict[str, Any]) -> ProviderResult:
        started = time.perf_counter()
        profile = str(snapshot.get("analysis_profile") or self.profile_name or "general")
        try:
            response = self.client.responses.parse(
                model=self.model_name,
                reasoning={"effort": self.reasoning_effort},
                input=[
                    {
                        "role": "system",
                        "content": system_prompt_for_profile(
                            profile,
                            integrity_recovery=bool(snapshot.get("integrity_recovery")),
                        ),
                    },
                    {"role": "user", "content": build_user_prompt(snapshot)},
                ],
                text_format=NewsAnalysis,
                store=False,
            )
            analysis = _extract_parsed(response)
        except PermanentAnalysisError:
            raise
        except ValidationError as exc:
            raise TransientAnalysisError(
                f"Structured output validation failed: {exc}"
            ) from exc
        except Exception as exc:  # SDK exceptions vary by installed version.
            mapped = self._map_openai_exception(exc)
            raise mapped from exc

        duration_ms = int((time.perf_counter() - started) * 1000)
        input_tokens, output_tokens = _extract_usage(response)
        raw_json = analysis.model_dump_json()
        return ProviderResult(
            analysis=analysis,
            raw_json=raw_json,
            response_id=getattr(response, "id", None),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            duration_ms=duration_ms,
            provider_name=self.provider_name,
            model_name=self.model_name,
            route_name=profile,
            reasoning_effort=self.reasoning_effort,
        )

    @staticmethod
    def _map_openai_exception(exc: Exception) -> AnalysisProviderError:
        name = exc.__class__.__name__
        message = str(exc)[:1500]
        status = getattr(exc, "status_code", None)

        transient_names = {
            "APIConnectionError",
            "APITimeoutError",
            "RateLimitError",
            "InternalServerError",
        }
        permanent_names = {
            "AuthenticationError",
            "PermissionDeniedError",
            "NotFoundError",
            "UnprocessableEntityError",
            "BadRequestError",
        }
        if name in transient_names or (isinstance(status, int) and status >= 500):
            return TransientAnalysisError(f"OpenAI transient error ({name}): {message}")
        if name in permanent_names or (isinstance(status, int) and 400 <= status < 500):
            return PermanentAnalysisError(
                f"OpenAI request/configuration error ({name}): {message}"
            )
        return TransientAnalysisError(f"OpenAI unexpected error ({name}): {message}")


class OpenAIModelRouter:
    """Routes routine news to a low-cost model and critical events to GPT-5.5."""

    provider_name = "openai"

    def __init__(
        self,
        *,
        primary: NewsAnalyzer,
        critical: NewsAnalyzer | None,
        critical_event_types: tuple[str, ...],
        escalate_impact_score: float,
        escalate_confidence_below: float,
        fallback_to_primary: bool,
        fallback_on_transient: bool,
    ) -> None:
        self.primary = primary
        self.critical = critical
        self.critical_event_types = {item.lower() for item in critical_event_types}
        self.escalate_impact_score = escalate_impact_score
        self.escalate_confidence_below = escalate_confidence_below
        self.fallback_to_primary = fallback_to_primary
        self.fallback_on_transient = fallback_on_transient
        self.model_name = (
            f"{getattr(primary, 'model_name', 'primary')}->"
            f"{getattr(critical, 'model_name', 'disabled')}"
        )
        self.log = logging.getLogger(self.__class__.__name__)

    def _profile_is_critical(self, snapshot: dict[str, Any]) -> bool:
        profile = str(snapshot.get("analysis_profile", "general")).lower()
        hint = str(snapshot.get("deterministic_event_hint", "other")).lower()
        return profile in self.critical_event_types or hint in self.critical_event_types

    def _analysis_needs_review(self, analysis: NewsAnalysis) -> bool:
        if self.critical is None or not analysis.causal_signal:
            return False
        if analysis.event_type.lower() in self.critical_event_types:
            return True
        if not analysis.prediction_eligible:
            return False
        return (
            analysis.impact_score >= self.escalate_impact_score
            or analysis.analysis_priority in {"high", "urgent"}
            or analysis.confidence < self.escalate_confidence_below
        )

    @staticmethod
    def _add_limitation(result: ProviderResult, message: str) -> ProviderResult:
        if message not in result.analysis.data_limitations:
            result.analysis.data_limitations.append(message)
        return ProviderResult(
            analysis=result.analysis,
            raw_json=result.analysis.model_dump_json(),
            response_id=result.response_id,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            duration_ms=result.duration_ms,
            provider_name=result.provider_name,
            model_name=result.model_name,
            route_name=result.route_name,
            reasoning_effort=result.reasoning_effort,
        )

    def _call_critical_with_fallback(
        self,
        snapshot: dict[str, Any],
        primary_result: ProviderResult | None = None,
    ) -> ProviderResult:
        if self.critical is None:
            if primary_result is not None:
                return primary_result
            return self.primary.analyze(snapshot)
        try:
            critical_result = self.critical.analyze(snapshot)
            if primary_result is None:
                return critical_result
            return ProviderResult(
                analysis=critical_result.analysis,
                raw_json=critical_result.raw_json,
                response_id=critical_result.response_id,
                input_tokens=(
                    (primary_result.input_tokens or 0) + (critical_result.input_tokens or 0)
                    if primary_result.input_tokens is not None
                    or critical_result.input_tokens is not None
                    else None
                ),
                output_tokens=(
                    (primary_result.output_tokens or 0) + (critical_result.output_tokens or 0)
                    if primary_result.output_tokens is not None
                    or critical_result.output_tokens is not None
                    else None
                ),
                duration_ms=primary_result.duration_ms + critical_result.duration_ms,
                provider_name=critical_result.provider_name,
                model_name=critical_result.model_name,
                route_name=f"primary_review->{critical_result.route_name}",
                reasoning_effort=critical_result.reasoning_effort,
            )
        except PermanentAnalysisError:
            if not self.fallback_to_primary:
                raise
            self.log.exception("Critical model unavailable; using primary-model fallback")
            result = primary_result or self.primary.analyze(snapshot)
            return self._add_limitation(
                result,
                "Critical model unavailable; primary-model fallback used",
            )
        except TransientAnalysisError:
            if not self.fallback_on_transient:
                raise
            self.log.exception("Critical model transient failure; using primary-model fallback")
            result = primary_result or self.primary.analyze(snapshot)
            return self._add_limitation(
                result,
                "Critical model transient failure; primary-model fallback used",
            )

    def recover_integrity(
        self,
        snapshot: dict[str, Any],
        *,
        prior_result: ProviderResult,
        integrity_notes: tuple[str, ...],
    ) -> ProviderResult:
        """Re-read immutable evidence with the critical model after integrity failure.

        The rejected model output is deliberately not included. Only short validator
        notes are supplied so the recovery pass cannot copy malformed wording.
        """
        if self.critical is None:
            raise PermanentAnalysisError(
                "Integrity recovery requires AI_ENABLE_CRITICAL_MODEL=true"
            )
        recovery_snapshot = json.loads(json.dumps(snapshot, ensure_ascii=False, default=str))
        recovery_snapshot["integrity_recovery"] = {
            "attempt": 1,
            "validator_notes": list(integrity_notes)[:12],
            "instruction": (
                "Reconstruct the analysis from immutable source only. Do not copy the prior answer."
            ),
        }
        critical_result = self.critical.analyze(recovery_snapshot)
        return ProviderResult(
            analysis=critical_result.analysis,
            raw_json=critical_result.raw_json,
            response_id=critical_result.response_id,
            input_tokens=(
                (prior_result.input_tokens or 0) + (critical_result.input_tokens or 0)
                if prior_result.input_tokens is not None
                or critical_result.input_tokens is not None
                else None
            ),
            output_tokens=(
                (prior_result.output_tokens or 0) + (critical_result.output_tokens or 0)
                if prior_result.output_tokens is not None
                or critical_result.output_tokens is not None
                else None
            ),
            duration_ms=prior_result.duration_ms + critical_result.duration_ms,
            provider_name=critical_result.provider_name,
            model_name=critical_result.model_name,
            route_name=f"integrity_recovery->{critical_result.route_name}",
            reasoning_effort=critical_result.reasoning_effort,
        )

    def analyze(self, snapshot: dict[str, Any]) -> ProviderResult:
        if self._profile_is_critical(snapshot):
            return self._call_critical_with_fallback(snapshot)

        primary_result = self.primary.analyze(snapshot)
        if self._analysis_needs_review(primary_result.analysis):
            reviewed_snapshot = dict(snapshot)
            event_type = primary_result.analysis.event_type.lower()
            if event_type in {"earnings", "guidance", "fomc", "fed_speech"}:
                reviewed_snapshot["analysis_profile"] = (
                    "fomc" if event_type in {"fomc", "fed_speech"} else event_type
                )
            return self._call_critical_with_fallback(
                reviewed_snapshot,
                primary_result=primary_result,
            )
        return primary_result


_POSITIVE = {
    "beat": 1.0,
    "beats": 1.0,
    "raise": 0.8,
    "raises": 0.8,
    "raised": 0.8,
    "surge": 0.7,
    "soaring": 0.7,
    "gain": 0.5,
    "growth": 0.5,
    "approval": 0.7,
    "contract": 0.4,
    "upgrade": 0.6,
    "record": 0.4,
}
_NEGATIVE = {
    "miss": -1.0,
    "misses": -1.0,
    "cut": -0.8,
    "cuts": -0.8,
    "downgrade": -0.6,
    "decline": -0.4,
    "falls": -0.4,
    "shock": -0.5,
    "investigation": -0.7,
    "offering": -0.5,
    "cyclicality": -0.3,
    "warning": -0.6,
    "war": -0.7,
    "attack": -0.6,
    "airstrike": -0.6,
    "missile": -0.5,
    "sanctions": -0.5,
    "tariffs": -0.4,
    "cyberattack": -0.5,
}


class HeuristicNewsAnalyzer:
    """Deterministic test provider. It is not a substitute for an LLM."""

    provider_name = "heuristic"
    model_name = "heuristic-v1"

    def analyze(self, snapshot: dict[str, Any]) -> ProviderResult:
        started = time.perf_counter()
        headline = str(snapshot.get("headline", ""))
        symbol = str(snapshot.get("primary_symbol", "UNKNOWN")).upper()
        lower = headline.lower()

        if is_obvious_market_price_update(headline):
            analysis = NewsAnalysis(
                primary_symbol=symbol,
                related_symbols=[symbol],
                event_type="market_price_update",
                event_subtype="automated_or_descriptive_price_move",
                sentiment="neutral",
                causal_signal=False,
                prediction_eligible=False,
                analysis_priority="low",
                impact_score=0.1,
                novelty_score=0.1,
                source_quality=0.6,
                confidence=0.92,
                time_horizon="intraday",
                summary="The headline primarily reports an observed price move and is not treated as a causal catalyst.",
                bull_case=[],
                bear_case=[],
                data_limitations=["Headline-only causal classification."],
                predictions=[],
            )
        else:
            tokens = re.findall(r"[a-z]+", lower)
            score = sum(_POSITIVE.get(t, 0.0) + _NEGATIVE.get(t, 0.0) for t in tokens)
            score = max(-2.5, min(2.5, score))
            direction = score / 2.5
            if direction >= 0.55:
                sentiment = "strong_positive"
            elif direction > 0.12:
                sentiment = "positive"
            elif direction <= -0.55:
                sentiment = "strong_negative"
            elif direction < -0.12:
                sentiment = "negative"
            else:
                sentiment = "mixed"

            event_type = "other"
            subtype = "general_corporate_news"
            if any(k in lower for k in ("fomc", "federal reserve", "fed decision", "powell", "dot plot")):
                event_type, subtype = "fomc", "federal_reserve_policy_event"
            elif "guidance" in lower or "outlook" in lower or "forecast" in lower:
                event_type, subtype = "guidance", "forward_outlook_change"
            elif any(k in lower for k in ("earnings", "eps", "revenue", "quarter")):
                event_type, subtype = "earnings", "earnings_or_revenue_update"
            elif any(k in lower for k in ("registers", "registration statement", "form s-3", "form s3")):
                event_type, subtype = "securities_filing", "registration_or_shelf_filing"
            elif any(k in lower for k in ("upgrade", "downgrade", "price target")):
                event_type, subtype = "analyst_action", "rating_or_target_change"
            elif any(k in lower for k in ("merger", "acquire", "acquisition")):
                event_type, subtype = "m_and_a", "transaction_news"
            elif any(
                k in lower
                for k in (
                    "geopolitical",
                    "war",
                    "missile",
                    "airstrike",
                    "attack",
                    "invasion",
                    "sanction",
                    "tariff",
                    "export control",
                    "trade war",
                    "red sea",
                    "taiwan",
                    "strait of hormuz",
                    "cyberattack",
                )
            ):
                event_type, subtype = "geopolitical", "geopolitical_risk_event"

            base_scale = 0.012 * direction
            horizon_scales = {
                "15m": 0.35,
                "1h": 0.60,
                "close": 0.85,
                "1d": 1.00,
                "3d": 0.80,
            }
            predictions: list[HorizonPrediction] = []
            up = max(0.10, min(0.80, 0.5 + 0.28 * direction))
            down = max(0.10, min(0.80, 0.5 - 0.28 * direction))
            flat = 0.16
            total = up + down + flat
            for horizon, scale in horizon_scales.items():
                expected = base_scale * scale
                width = 0.02 + abs(expected) * 1.5
                predictions.append(
                    HorizonPrediction(
                        symbol=symbol,
                        horizon=horizon,
                        expected_return=expected,
                        bear_return=max(-0.99, expected - width),
                        bull_return=expected + width,
                        up_probability=up / total,
                        flat_probability=flat / total,
                        down_probability=down / total,
                        confidence=0.35,
                    )
                )

            analysis = NewsAnalysis(
                primary_symbol=symbol,
                related_symbols=[symbol],
                event_type=event_type,
                event_subtype=subtype,
                sentiment=sentiment,
                causal_signal=True,
                prediction_eligible=True,
                analysis_priority="normal",
                impact_score=min(0.75, 0.35 + abs(direction) * 0.35),
                novelty_score=0.45,
                source_quality=0.5,
                confidence=0.35,
                time_horizon="1_to_3_days",
                summary="Deterministic keyword-based test analysis; use the OpenAI provider for production analysis.",
                bull_case=["Positive wording can support near-term sentiment."] if direction > 0 else [],
                bear_case=["Negative wording can pressure near-term sentiment."] if direction < 0 else [],
                data_limitations=["Heuristic test provider", "No semantic or contextual reasoning"],
                predictions=predictions,
            )

        duration_ms = int((time.perf_counter() - started) * 1000)
        raw = analysis.model_dump_json()
        return ProviderResult(
            analysis=analysis,
            raw_json=raw,
            response_id=None,
            input_tokens=None,
            output_tokens=None,
            duration_ms=duration_ms,
            provider_name=self.provider_name,
            model_name=self.model_name,
            route_name=str(snapshot.get("analysis_profile", "heuristic")),
            reasoning_effort="none",
        )


class FailingAnalyzer:
    """Test helper used by offline resilience tests."""

    provider_name = "test"
    model_name = "failing-test"

    def __init__(self, failures: int, permanent: bool = False) -> None:
        self.remaining = failures
        self.permanent = permanent

    def analyze(self, snapshot: dict[str, Any]) -> ProviderResult:
        if self.remaining > 0:
            self.remaining -= 1
            if self.permanent:
                raise PermanentAnalysisError("simulated permanent failure")
            raise TransientAnalysisError("simulated transient failure")
        return HeuristicNewsAnalyzer().analyze(snapshot)
