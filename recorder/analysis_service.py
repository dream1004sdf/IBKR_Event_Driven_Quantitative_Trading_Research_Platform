from __future__ import annotations

import json
import logging
import os
import random
import signal
import socket
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .ai_provider import (
    HeuristicNewsAnalyzer,
    NewsAnalyzer,
    OpenAINewsAnalyzer,
    OpenAIModelRouter,
    PermanentAnalysisError,
    ProviderResult,
    TransientAnalysisError,
)
from .analysis_database import AnalysisDatabase, ClaimedJob
from .analysis_models import NewsAnalysis, validate_primary_horizons
from .attribution import attribute_event_targets
from .config import Settings
from .economic_plausibility import (
    EconomicPlausibilityConfig,
    assess_economic_plausibility,
)
from .official_sources import OfficialSourceFetcher, assess_source, extract_official_urls, official_source_kind
from .news_asset_mapper import infer_news_assets
from .text_integrity import (
    IntegrityResult,
    suppress_forecasts_for_integrity,
    validate_analysis_text,
)
from .prompting import (
    build_input_snapshot,
    detect_event_profile,
    is_obvious_market_price_update,
    prompt_hash,
    sha256_text,
    stable_json,
)
from .source_operations import build_source_desk_context

UTC = timezone.utc


@dataclass(frozen=True)
class IntegrityRecoveryDecision:
    attempt: bool
    status: str = "not_attempted"
    reason: str = ""


class AIAnalysisService:
    def __init__(
        self,
        settings: Settings,
        analyzer: NewsAnalyzer | None = None,
        db: AnalysisDatabase | None = None,
        worker_id: str | None = None,
    ) -> None:
        self.settings = settings
        self.db = db or AnalysisDatabase(settings.database_path)
        self.analyzer = analyzer or self._build_analyzer(settings)
        self.rules_analyzer = HeuristicNewsAnalyzer()
        self.worker_id = worker_id or self._default_worker_id()
        self.stop_event = threading.Event()
        self.log = logging.getLogger(self.__class__.__name__)
        self._last_idle_log = 0.0
        self.official_fetcher = OfficialSourceFetcher(
            timeout_seconds=settings.official_source_timeout_seconds,
            user_agent=settings.official_source_user_agent,
            max_bytes=settings.official_source_max_bytes,
        )

    @staticmethod
    def _default_worker_id() -> str:
        return (
            f"{socket.gethostname()}-{os.getpid()}-"
            f"{uuid.uuid4().hex[:8]}"
        )

    @staticmethod
    def _build_analyzer(settings: Settings) -> NewsAnalyzer:
        if settings.ai_provider == "heuristic":
            return HeuristicNewsAnalyzer()
        if settings.ai_provider == "openai":
            primary = OpenAINewsAnalyzer(
                api_key=settings.openai_api_key or "",
                model=settings.ai_model,
                timeout_seconds=settings.ai_api_timeout_seconds,
                reasoning_effort=settings.ai_primary_reasoning_effort,
                profile_name="general",
            )
            critical = None
            if settings.ai_enable_critical_model:
                critical = OpenAINewsAnalyzer(
                    api_key=settings.openai_api_key or "",
                    model=settings.ai_critical_model,
                    timeout_seconds=settings.ai_critical_api_timeout_seconds,
                    reasoning_effort=settings.ai_critical_reasoning_effort,
                    profile_name="critical",
                )
            return OpenAIModelRouter(
                primary=primary,
                critical=critical,
                critical_event_types=settings.ai_critical_event_types,
                escalate_impact_score=settings.ai_escalate_impact_score,
                escalate_confidence_below=settings.ai_escalate_confidence_below,
                fallback_to_primary=settings.ai_critical_fallback_to_primary,
                fallback_on_transient=settings.ai_critical_fallback_on_transient,
            )
        raise PermanentAnalysisError(
            f"Unsupported AI_PROVIDER={settings.ai_provider!r}. Use openai or heuristic."
        )

    @property
    def provider_name(self) -> str:
        return str(getattr(self.analyzer, "provider_name", self.settings.ai_provider))

    @property
    def model_name(self) -> str:
        return str(getattr(self.analyzer, "model_name", self.settings.ai_model))

    @property
    def analysis_key(self) -> str:
        # Keep the configuration key stable. Local prefilters remain auditable inside
        # the analysis record without creating duplicate jobs.
        return self.settings.analysis_key

    def install_signal_handlers(self) -> None:
        def _handler(signum, _frame) -> None:
            self.log.info("Shutdown signal %s received", signum)
            self.stop_event.set()

        for sig in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
            if sig is None:
                continue
            try:
                signal.signal(sig, _handler)
            except (ValueError, OSError):
                # Signal handlers can only be installed in the main thread.
                pass

    def enqueue_available_news(self) -> int:
        return self.db.enqueue_jobs(
            analysis_key=self.analysis_key,
            provider=self.settings.ai_provider,
            model_name=self.settings.ai_model,
            prompt_version=self.settings.ai_prompt_version,
            max_age_days=self.settings.ai_backfill_max_age_days,
            limit=self.settings.ai_backfill_limit,
        )

    def _claim(self) -> ClaimedJob | None:
        return self.db.claim_next_job(
            worker_id=self.worker_id,
            analysis_key=self.analysis_key,
            stale_seconds=self.settings.ai_job_stale_seconds,
            body_wait_seconds=self.settings.ai_body_wait_seconds,
        )

    def _build_snapshot(self, job: ClaimedJob) -> tuple[dict[str, Any], str, str, str, str]:
        linked_symbols, baselines = self.db.get_news_context(job.news_id)
        headline = str(job.news.get("headline", ""))
        body = str(job.news.get("body") or "")
        profile = detect_event_profile(headline, body)

        available_symbols = [str(item.get("symbol") or "").upper() for item in baselines if item.get("symbol")]
        attribution = attribute_event_targets(
            headline=headline,
            body=body,
            original_symbol=str(job.news.get("primary_symbol", "UNKNOWN")),
            available_symbols=available_symbols,
        )
        attribution_record_id, _ = self.db.record_event_attribution(
            news_id=job.news_id,
            headline=headline,
            body=body,
            available_symbols=available_symbols,
            decision=attribution,
        )

        for url in extract_official_urls(body):
            kind = official_source_kind(url) or "official"
            self.db.register_official_candidate(job.news_id, kind, url)

        documents = self.db.official_documents_for_news(job.news_id)
        if self.settings.official_source_auto_fetch:
            for document in documents:
                if str(document.get("status")) != "candidate" or not document.get("source_url"):
                    continue
                try:
                    kind, content, digest = self.official_fetcher.fetch(str(document["source_url"]))
                    self.db.update_official_document(
                        int(document["id"]), content=content, content_hash=digest, status="fetched"
                    )
                    self.log.info("Fetched official %s document for news #%s", kind, job.news_id)
                except Exception as exc:
                    self.db.update_official_document(
                        int(document["id"]), content=None, content_hash=None, status="error", last_error=str(exc)[:1000]
                    )
                    self.log.warning("Official-source fetch failed for news #%s: %s", job.news_id, exc)
            documents = self.db.official_documents_for_news(job.news_id)

        source_assessment = assess_source(
            str(job.news.get("provider", "")), headline, body, documents
        )
        include_body = self.settings.ai_use_article_body or (
            profile.critical and self.settings.ai_critical_use_article_body
        )
        max_body_chars = (
            self.settings.ai_critical_max_body_chars
            if profile.critical
            else self.settings.ai_max_body_chars
        )
        snapshot = build_input_snapshot(
            news=job.news,
            linked_symbols=linked_symbols,
            baselines=baselines,
            required_horizons=self.settings.ai_horizons,
            max_body_chars=max_body_chars,
            include_article_body=include_body,
            event_profile=profile,
            attribution=attribution,
            official_documents=documents,
            source_assessment=source_assessment,
        )
        if str(job.news.get("primary_symbol") or "").upper().startswith("BROADTAPE:"):
            snapshot["broadtape_asset_mapping"] = infer_news_assets(
                headline,
                body,
                primary_symbol=str(job.news.get("primary_symbol") or ""),
                provider=str(job.news.get("provider") or ""),
                extra_data=str(job.news.get("extra_data") or ""),
            ).as_dict()
        snapshot["source_desk_context"] = build_source_desk_context(self.db, snapshot)
        # The immutable prediction input references the exact deterministic
        # attribution record used for the run.  The raw model response remains
        # untouched while later engine versions can append a new record.
        snapshot["attribution_record_id"] = attribution_record_id
        snapshot["attribution_version"] = "v0.14.0"
        snapshot["actual_subject_name"] = attribution.subject_name or attribution.subject_entity
        snapshot["actual_subject_symbol"] = attribution.subject_symbol
        snapshot["affected_symbols"] = list(attribution.affected_symbols)
        snapshot["benchmark_symbols"] = list(attribution.benchmark_symbols)
        input_json = stable_json(snapshot)
        return (
            snapshot,
            input_json,
            sha256_text(input_json),
            sha256_text(headline),
            sha256_text(body),
        )

    def _analyze(self, snapshot: dict[str, Any]) -> tuple[ProviderResult, str, str]:
        headline = str(snapshot.get("headline", ""))
        if is_obvious_market_price_update(headline):
            result = self.rules_analyzer.analyze(snapshot)
            return result, "rules", "market-update-filter-v2"

        try:
            result = self.analyzer.analyze(snapshot)
            actual_provider = (
                result.provider_name
                if result.provider_name != "unknown"
                else self.provider_name
            )
            actual_model = (
                result.model_name
                if result.model_name != "unknown"
                else self.model_name
            )
            return result, actual_provider, actual_model
        except (TransientAnalysisError, PermanentAnalysisError):
            if not self.settings.ai_allow_heuristic_fallback:
                raise
            self.log.exception(
                "Primary AI provider failed; explicit heuristic fallback is enabled"
            )
            result = self.rules_analyzer.analyze(snapshot)
            analysis = result.analysis
            if "Primary AI provider failed; heuristic fallback used" not in analysis.data_limitations:
                analysis.data_limitations.append(
                    "Primary AI provider failed; heuristic fallback used"
                )
            return result, "heuristic_fallback", self.rules_analyzer.model_name

    @staticmethod
    def _has_filing_materiality(analysis: NewsAnalysis) -> bool:
        text = " ".join(
            [analysis.event_subtype, analysis.summary]
            + [fact.name + " " + fact.actual + " " + fact.evidence for fact in analysis.key_facts]
        ).lower()
        materiality_terms = (
            "percent of float", "percentage of float", "ownership percentage",
            "average daily volume", "average trading volume", "market value",
            "10b5-1", "beneficial ownership", "completed sale", "sale proceeds",
        )
        return sum(term in text for term in materiality_terms) >= 2

    @staticmethod
    def _is_routine_form144(analysis: NewsAnalysis) -> bool:
        text = f"{analysis.event_type} {analysis.event_subtype} {analysis.summary}".lower()
        return "form 144" in text or (
            analysis.event_type == "securities_filing"
            and any(term in text for term in ("restricted shares", "holder notice", "potential sale"))
        )

    @staticmethod
    def _apply_quality_guards(analysis: NewsAnalysis, snapshot: dict[str, Any]) -> None:
        body_available = bool(snapshot.get("article_body_available_to_model"))
        official_evidence = snapshot.get("official_evidence") or []
        analysis.analysis_depth = "event_document" if official_evidence else ("article_body" if body_available else "headline_only")
        analysis.event_scope = str(snapshot.get("event_scope_hint") or analysis.event_scope)
        analysis.subject_entity = str(snapshot.get("subject_entity_hint") or analysis.subject_entity)
        analysis.event_subject_symbol = str(snapshot.get("event_subject_symbol_hint") or "").upper()
        analysis.subject_asset_type = str(snapshot.get("subject_asset_type_hint") or "unknown")
        analysis.target_asset_type = str(snapshot.get("target_asset_type_hint") or "unknown")
        analysis.target_role = str(snapshot.get("target_role_hint") or "unknown")
        analysis.target_assets = [str(x).upper() for x in (snapshot.get("target_assets") or [])]
        analysis.attribution_status = str(snapshot.get("attribution_status_hint") or "unresolved")
        analysis.attribution_confidence = float(snapshot.get("attribution_confidence_hint") or 0.0)
        analysis.original_subscription_symbol = str(
            snapshot.get("original_subscription_symbol_hint")
            or snapshot.get("original_subscription_symbol")
            or ""
        ).upper()
        analysis.subject_target_available = bool(snapshot.get("subject_target_available_hint", True))
        analysis.attribution_reason = str(snapshot.get("attribution_reason") or "")
        analysis.article_scope = str(snapshot.get("article_scope_hint") or "unknown")
        analysis.article_primary_subject = str(
            snapshot.get("article_primary_subject_hint") or ""
        )
        analysis.mentioned_symbols = [
            str(x).upper() for x in (snapshot.get("mentioned_symbols_hint") or [])
        ]
        analysis.subscription_entity_role = str(
            snapshot.get("subscription_entity_role_hint") or "delivery_route"
        )

        target_rows = snapshot.get("target_quote_audit_snapshot") or snapshot.get("baseline_market_snapshot") or []
        if target_rows:
            target_row = target_rows[0]
            analysis.target_quote_integrity_status = str(
                target_row.get("quote_integrity_status") or "not_checked"
            )
            raw_reasons = target_row.get("quote_integrity_reasons_json") or "[]"
            try:
                parsed_reasons = json.loads(raw_reasons) if isinstance(raw_reasons, str) else raw_reasons
            except (TypeError, ValueError, json.JSONDecodeError):
                parsed_reasons = [str(raw_reasons)]
            analysis.target_quote_integrity_reasons = [str(x) for x in (parsed_reasons or [])]
        else:
            analysis.target_quote_integrity_status = (
                "not_available_for_actual_subject"
                if analysis.attribution_status == "mismatch" or not analysis.subject_target_available
                else "not_available"
            )
            analysis.target_quote_integrity_reasons = [
                "No integrity-eligible attributed target quote was available"
            ]

        delivery_rows = snapshot.get("delivery_market_snapshot") or []
        if delivery_rows:
            delivery_row = delivery_rows[0]
            analysis.delivery_reference_symbol = str(
                delivery_row.get("symbol") or analysis.original_subscription_symbol
            ).upper()
            for key in ("selected_price", "midpoint", "last", "ask", "bid"):
                value = delivery_row.get(key)
                try:
                    parsed = float(value)
                except (TypeError, ValueError):
                    continue
                if parsed > 0:
                    analysis.delivery_reference_price = parsed
                    analysis.delivery_reference_price_source = str(
                        delivery_row.get("selected_price_source") or key
                    )
                    break

        if analysis.attribution_status in {"mismatch", "multi_subject", "unknown", "unresolved"} or not analysis.subject_target_available:
            analysis.prediction_eligible = False
            analysis.predictions = []
            analysis.reaction_observation_eligible = False
            analysis.target_assets = []
            analysis.target_role = "unavailable_subject"
            analysis.causal_signal = False
            if "deterministic_attribution_mismatch" not in analysis.interpretation_flags:
                analysis.interpretation_flags.append("deterministic_attribution_mismatch")
            limitation = (
                "Actual article subject differs from the IBKR subscription symbol and no event-time "
                "baseline exists for the true subject; forecasts and reaction calibration are suppressed"
            )
            if limitation not in analysis.data_limitations:
                analysis.data_limitations.append(limitation)

        if analysis.event_scope == "macro":
            macro_type_map = {
                "fomc": "fomc",
                "cpi": "cpi",
                "employment": "employment",
                "pce": "macro",
                "gdp": "macro",
                "retail_sales": "macro",
                "ism": "macro",
            }
            analysis.event_type = macro_type_map.get(analysis.subject_entity.lower(), "macro")
        if snapshot.get("deterministic_event_hint") == "geopolitical" and analysis.event_type == "other":
            analysis.event_type = "geopolitical"
        analysis.source_tier = str(snapshot.get("source_tier_hint") or analysis.source_tier)
        analysis.official_source_status = (
            "attached" if official_evidence else ("candidate_found" if snapshot.get("official_source_urls") else "not_available")
        )
        if analysis.source_tier == "official_primary":
            analysis.source_quality = max(analysis.source_quality, 0.95)
        elif analysis.source_tier == "reputable_secondary":
            analysis.source_quality = max(analysis.source_quality, 0.80)

        if analysis.event_type == "market_price_update":
            analysis.causal_signal = False
            analysis.prediction_eligible = False
            analysis.predictions = []
            analysis.event_stage = "retrospective"

        if analysis.event_type in {"earnings", "guidance", "fomc", "fed_speech", "cpi", "employment", "macro", "geopolitical"}:
            if not body_available and not official_evidence:
                limitation = (
                    "Critical event was analyzed from headline-only data; detailed figures, "
                    "consensus comparisons, and event-stage context may be incomplete"
                )
                if limitation not in analysis.data_limitations:
                    analysis.data_limitations.append(limitation)
                analysis.confidence = min(analysis.confidence, 0.62)
                for prediction in analysis.predictions:
                    prediction.confidence = min(prediction.confidence, 0.58)
            if not analysis.key_facts:
                limitation = "No explicit event metrics were available in the supplied snapshot"
                if limitation not in analysis.data_limitations:
                    analysis.data_limitations.append(limitation)
                analysis.confidence = min(analysis.confidence, 0.65)

        # Routine Form 144 notices without materiality evidence are recorded as events,
        # but are not allowed to create directional forecasts.  This avoids treating a
        # filing permission as a confirmed sale or fundamental catalyst.
        if AIAnalysisService._is_routine_form144(analysis) and not AIAnalysisService._has_filing_materiality(analysis):
            analysis.prediction_eligible = False
            analysis.predictions = []
            analysis.reaction_observation_eligible = True
            analysis.impact_score = min(analysis.impact_score, 0.25)
            if "routine_filing_no_materiality" not in analysis.interpretation_flags:
                analysis.interpretation_flags.append("routine_filing_no_materiality")
            limitation = (
                "Routine Form 144/holder-sale notice lacked confirmed execution and materiality context; forecasts suppressed"
            )
            if limitation not in analysis.data_limitations:
                analysis.data_limitations.append(limitation)

        if analysis.event_stage == "retrospective":
            analysis.reaction_observation_eligible = analysis.event_type != "market_price_update"
            analysis.prediction_eligible = False
            analysis.predictions = []

    @staticmethod
    def _has_attribution_mismatch(analysis: NewsAnalysis) -> bool:
        if analysis.attribution_status in {"mismatch", "multi_subject", "unknown", "unresolved"} or not analysis.subject_target_available:
            return True
        text = " ".join(
            [
                analysis.event_subtype,
                analysis.summary,
                *analysis.data_limitations,
                *analysis.interpretation_flags,
            ]
        ).lower()
        markers = (
            "headline/body mismatch",
            "headline body mismatch",
            "attribution mismatch",
            "attribution conflict",
            "body does not match",
            "body is about",
            "unrelated article",
        )
        return any(marker in text for marker in markers)

    @staticmethod
    def _is_low_signal_recovery_event(analysis: NewsAnalysis) -> bool:
        if analysis.event_type in {"market_price_update", "securities_filing", "ownership"}:
            return True
        flags = {flag.lower() for flag in analysis.interpretation_flags}
        return bool(
            {
                "routine_filing_no_materiality",
                "market_price_update",
                "non_causal",
            }
            & flags
        )

    def _integrity_recovery_decision(
        self,
        analysis: NewsAnalysis,
        integrity: IntegrityResult,
        *,
        raw_fact_count: int,
        raw_high_importance_count: int,
    ) -> IntegrityRecoveryDecision:
        """Choose whether the critical model must re-read the immutable source.

        Recovery eligibility intentionally does *not* depend on validated fact count.
        A result with zero validated facts is exactly the case where a fresh critical
        pass may be needed. Fact thresholds are applied only after that pass.
        """
        if integrity.status == "valid":
            return IntegrityRecoveryDecision(
                False,
                "not_needed_integrity_valid",
                "text integrity validation passed; recovery was not required",
            )
        if not self.settings.ai_integrity_recovery_enabled:
            return IntegrityRecoveryDecision(
                False,
                "not_attempted_disabled",
                "integrity recovery is disabled",
            )
        if self._has_attribution_mismatch(analysis):
            return IntegrityRecoveryDecision(
                False,
                "not_attempted_attribution_mismatch",
                "headline/body or entity attribution mismatch",
            )
        if analysis.event_stage == "retrospective":
            return IntegrityRecoveryDecision(
                False,
                "not_attempted_retrospective",
                "retrospective event is not eligible for a fresh forecast recovery",
            )
        if (
            self._is_low_signal_recovery_event(analysis)
            or analysis.event_stage not in {"initial_release", "follow_up", "press_conference"}
        ):
            return IntegrityRecoveryDecision(
                False,
                "not_attempted_low_signal",
                "event type or stage is low-signal for integrity recovery",
            )
        if analysis.impact_score < self.settings.ai_integrity_recovery_min_impact:
            return IntegrityRecoveryDecision(
                False,
                "not_attempted_low_impact",
                (
                    f"impact {analysis.impact_score:.2f} is below recovery threshold "
                    f"{self.settings.ai_integrity_recovery_min_impact:.2f}"
                ),
            )
        if not analysis.causal_signal or not analysis.prediction_eligible:
            return IntegrityRecoveryDecision(
                False,
                "not_attempted_low_signal",
                "event is non-causal or not forecast eligible",
            )

        # A failed, high-signal initial result always gets one recovery attempt,
        # even when the first validator rejected every fact.
        if integrity.status == "failed":
            return IntegrityRecoveryDecision(True)

        # Warnings are recovered only when a material share of the raw facts were
        # rejected. Raw counts are used because validation may have removed them.
        if (
            integrity.status == "warning"
            and integrity.rejected_ratio
            >= self.settings.ai_integrity_recovery_warning_reject_ratio
            and raw_fact_count > 0
        ):
            return IntegrityRecoveryDecision(True)

        return IntegrityRecoveryDecision(False)

    @staticmethod
    def _add_integrity_flag(analysis: NewsAnalysis, flag: str) -> None:
        if flag not in analysis.interpretation_flags:
            analysis.interpretation_flags.append(flag)

    @staticmethod
    def _set_not_attempted_recovery(
        analysis: NewsAnalysis,
        *,
        status: str,
        reason: str,
        failed_integrity: bool,
    ) -> None:
        analysis.integrity_recovery_status = status  # type: ignore[assignment]
        analysis.integrity_recovery_attempts = 0
        analysis.integrity_recovery_model = ""
        if status != "not_attempted":
            AIAnalysisService._add_integrity_flag(analysis, status)
        if (
            reason
            and status != "not_needed_integrity_valid"
            and reason not in analysis.data_limitations
        ):
            analysis.data_limitations.append(reason)
        if failed_integrity:
            suppress_forecasts_for_integrity(
                analysis,
                recovery_status=status,
                reason=(
                    f"Text integrity validation failed; recovery was not attempted: {reason}"
                    if reason
                    else "Text integrity validation failed; recovery was not attempted"
                ),
            )

    @staticmethod
    def _enforce_recovery_state_invariants(analysis: NewsAnalysis) -> None:
        attempted = {"recovered", "partial", "hard_failed", "unavailable"}
        status = analysis.integrity_recovery_status
        if status in attempted:
            if analysis.integrity_recovery_attempts < 1:
                raise RuntimeError(
                    f"Invalid recovery state {status!r}: attempted status requires attempts >= 1"
                )
            if not analysis.integrity_recovery_model:
                raise RuntimeError(
                    f"Invalid recovery state {status!r}: attempted status requires a model"
                )
        elif status.startswith("not_attempted") or status.startswith("not_needed"):
            if analysis.integrity_recovery_attempts != 0 or analysis.integrity_recovery_model:
                raise RuntimeError(
                    f"Invalid recovery state {status!r}: not-attempted status requires attempts=0 and no model"
                )

    def _recover_integrity_if_needed(
        self,
        *,
        result: ProviderResult,
        snapshot: dict[str, Any],
        initial_integrity: IntegrityResult,
        raw_fact_count: int,
        raw_high_importance_count: int,
    ) -> tuple[ProviderResult, IntegrityResult]:
        analysis = result.analysis
        analysis.integrity_initial_status = initial_integrity.status  # type: ignore[assignment]
        decision = self._integrity_recovery_decision(
            analysis,
            initial_integrity,
            raw_fact_count=raw_fact_count,
            raw_high_importance_count=raw_high_importance_count,
        )
        if not decision.attempt:
            self._set_not_attempted_recovery(
                analysis,
                status=decision.status,
                reason=decision.reason,
                failed_integrity=initial_integrity.status == "failed",
            )
            self._enforce_recovery_state_invariants(analysis)
            return result, initial_integrity

        recovery = getattr(self.analyzer, "recover_integrity", None)
        if not callable(recovery):
            limitation = (
                "Integrity recovery was required but the configured analyzer has no critical recovery model"
            )
            self._set_not_attempted_recovery(
                analysis,
                status="not_attempted_no_recovery_model",
                reason=limitation,
                failed_integrity=initial_integrity.status == "failed",
            )
            self._enforce_recovery_state_invariants(analysis)
            return result, initial_integrity

        try:
            recovered = recovery(
                snapshot,
                prior_result=result,
                integrity_notes=initial_integrity.notes,
            )
            recovered.analysis.integrity_initial_status = initial_integrity.status  # type: ignore[assignment]
            recovered.analysis.integrity_recovery_attempts = 1
            recovered.analysis.integrity_recovery_model = (
                recovered.model_name or self.settings.ai_critical_model
            )
            self._apply_quality_guards(recovered.analysis, snapshot)
            final_integrity = validate_analysis_text(
                recovered.analysis, snapshot, suppress_on_failure=False
            )

            if (
                final_integrity.status == "valid"
                and final_integrity.unsupported_numeric_count == 0
                and final_integrity.validated_fact_count
                >= self.settings.ai_integrity_recovery_min_validated_facts
                and final_integrity.validated_high_importance_count
                >= self.settings.ai_integrity_recovery_min_high_facts
            ):
                recovered.analysis.integrity_recovery_status = "recovered"
                self._add_integrity_flag(recovered.analysis, "integrity_recovered")
                self._enforce_recovery_state_invariants(recovered.analysis)
                return recovered, final_integrity

            partial_ok = (
                final_integrity.status in {"valid", "warning"}
                and final_integrity.unsupported_numeric_count == 0
                and final_integrity.validated_fact_count
                >= self.settings.ai_integrity_recovery_min_validated_facts
                and final_integrity.validated_high_importance_count >= 1
            )
            if partial_ok:
                recovered.analysis.integrity_recovery_status = "partial"
                self._add_integrity_flag(
                    recovered.analysis, "integrity_recovery_partial_calibration_only"
                )
                limitation = (
                    "Integrity recovery retained a partially grounded forecast; "
                    "directional use is disabled"
                )
                if limitation not in recovered.analysis.data_limitations:
                    recovered.analysis.data_limitations.append(limitation)
                self._enforce_recovery_state_invariants(recovered.analysis)
                return recovered, final_integrity

            recovered.analysis.integrity_recovery_status = "hard_failed"
            self._add_integrity_flag(recovered.analysis, "integrity_recovery_hard_failed")
            suppress_forecasts_for_integrity(
                recovered.analysis,
                recovery_status="hard_failed",
                reason="Critical integrity recovery failed source-grounding validation",
            )
            self._enforce_recovery_state_invariants(recovered.analysis)
            return recovered, final_integrity
        except (PermanentAnalysisError, TransientAnalysisError) as exc:
            # This state represents a real attempted call. Therefore attempts=1 and a
            # recovery model are always recorded; unavailable(0:-) is impossible.
            self.log.warning(
                "Integrity recovery unavailable for %s: %s",
                snapshot.get("headline", "unknown event"),
                exc,
            )
            analysis.integrity_recovery_status = "unavailable"
            analysis.integrity_recovery_attempts = 1
            analysis.integrity_recovery_model = self.settings.ai_critical_model
            self._add_integrity_flag(analysis, "integrity_recovery_unavailable")
            limitation = f"Integrity recovery unavailable: {exc.__class__.__name__}"
            if limitation not in analysis.data_limitations:
                analysis.data_limitations.append(limitation)
            if initial_integrity.status == "failed":
                suppress_forecasts_for_integrity(
                    analysis, recovery_status="unavailable", reason=limitation
                )
            else:
                self._add_integrity_flag(
                    analysis, "integrity_recovery_unavailable_observe_only"
                )
            self._enforce_recovery_state_invariants(analysis)
            return result, initial_integrity

    def process_one(self) -> bool:
        job = self._claim()
        if job is None:
            return False

        started_at = datetime.now(UTC)
        try:
            snapshot, input_json, input_hash, headline_hash, body_hash = self._build_snapshot(job)
            result, actual_provider, actual_model = self._analyze(snapshot)
            self._apply_quality_guards(result.analysis, snapshot)
            raw_fact_count = len(result.analysis.key_facts)
            raw_high_importance_count = sum(
                1
                for fact in result.analysis.key_facts
                if fact.importance in {"high", "critical"}
            )
            initial_integrity = validate_analysis_text(
                result.analysis, snapshot, suppress_on_failure=False
            )
            result, final_integrity = self._recover_integrity_if_needed(
                result=result,
                snapshot=snapshot,
                initial_integrity=initial_integrity,
                raw_fact_count=raw_fact_count,
                raw_high_importance_count=raw_high_importance_count,
            )
            assess_economic_plausibility(
                result.analysis,
                snapshot,
                EconomicPlausibilityConfig(
                    enabled=self.settings.economic_plausibility_enabled,
                    target_max_price_multiple=self.settings.economic_target_max_price_multiple,
                    target_min_price_multiple=self.settings.economic_target_min_price_multiple,
                    target_max_change_ratio=self.settings.economic_target_max_change_ratio,
                    fundamental_max_actual_expected_ratio=(
                        self.settings.economic_fundamental_max_actual_expected_ratio
                    ),
                    fundamental_max_actual_prior_ratio=(
                        self.settings.economic_fundamental_max_actual_prior_ratio
                    ),
                ),
            )
            actual_provider = result.provider_name or actual_provider
            actual_model = result.model_name or actual_model
            analysis = validate_primary_horizons(
                result.analysis,
                expected_symbol=str(snapshot["attributed_primary_symbol"]),
                required_horizons=self.settings.ai_horizons,
            )
            completed_at = datetime.now(UTC)
            analysis_id = self.db.complete_job(
                job=job,
                worker_id=self.worker_id,
                analysis=analysis,
                prompt_hash=prompt_hash(self.settings.ai_prompt_version),
                input_hash=input_hash,
                input_snapshot_json=input_json,
                baseline_snapshot_json=stable_json(snapshot["baseline_market_snapshot"]),
                headline_hash=headline_hash,
                body_hash=body_hash,
                raw_json=analysis.model_dump_json(),
                response_id=result.response_id,
                provider=actual_provider,
                model_name=actual_model,
                prompt_version=self.settings.ai_prompt_version,
                started_at=started_at,
                completed_at=completed_at,
                duration_ms=result.duration_ms,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                quality_config={
                    "min_analysis_confidence": self.settings.prediction_min_analysis_confidence,
                    "min_prediction_confidence": self.settings.prediction_min_confidence,
                    "min_top_probability": self.settings.prediction_min_top_probability,
                    "min_probability_edge": self.settings.prediction_min_probability_edge,
                    "max_range_width_by_horizon": {
                        "15m": self.settings.prediction_max_range_15m,
                        "1h": self.settings.prediction_max_range_1h,
                        "close": self.settings.prediction_max_range_close,
                        "1d": self.settings.prediction_max_range_1d,
                        "3d": self.settings.prediction_max_range_3d,
                    },
                    "official_required_event_types": self.settings.prediction_official_required_event_types,
                    "min_validated_facts_without_official": self.settings.prediction_min_validated_facts_without_official,
                    "integrity_partial_calibration_only": self.settings.ai_integrity_recovery_partial_calibration_only,
                },
            )
            cluster_id = self.db.assign_event_cluster(
                analysis_id=analysis_id,
                news_id=job.news_id,
                analysis=analysis,
                headline=str(job.news.get("headline", "")),
                body=str(job.news.get("body") or ""),
                published_at=str(job.news.get("published_at")),
                similarity_threshold=self.settings.event_cluster_similarity_threshold,
                window_hours=self.settings.event_cluster_window_hours,
            )
            self.log.info(
                "Analysis #%s completed for news #%s [%s] event=%s eligible=%s predictions=%s",
                analysis_id,
                job.news_id,
                analysis.primary_symbol,
                analysis.event_type,
                analysis.prediction_eligible,
                len(analysis.predictions),
            )
            self.log.info("Analysis #%s assigned to event cluster #%s", analysis_id, cluster_id)
            return True
        except PermanentAnalysisError as exc:
            self._handle_failure(job, exc, permanent=True)
            return True
        except TransientAnalysisError as exc:
            self._handle_failure(job, exc, permanent=False)
            return True
        except Exception as exc:
            # Schema, SQLite, SDK compatibility, and unexpected implementation errors
            # are persisted and retried instead of terminating the daemon.
            self.log.exception("Unexpected analysis failure for news #%s", job.news_id)
            self._handle_failure(
                job,
                TransientAnalysisError(
                    f"Unexpected {exc.__class__.__name__}: {str(exc)[:1500]}"
                ),
                permanent=False,
            )
            return True

    def _handle_failure(
        self,
        job: ClaimedJob,
        exc: Exception,
        *,
        permanent: bool,
    ) -> None:
        exponent = max(0, job.attempts - 1)
        delay = min(
            self.settings.ai_retry_max_seconds,
            self.settings.ai_retry_base_seconds * (2**exponent),
        )
        # Jitter prevents several workers from retrying the same provider simultaneously.
        delay += random.uniform(0, min(5.0, self.settings.ai_retry_base_seconds))
        retry_at = datetime.now(UTC) + timedelta(seconds=delay)
        status = self.db.fail_job(
            job,
            self.worker_id,
            f"{exc.__class__.__name__}: {str(exc)[:1800]}",
            retry_at=retry_at,
            permanent=permanent,
            max_attempts=self.settings.ai_max_attempts,
        )
        log_method = self.log.error if status == "dead" else self.log.warning
        log_method(
            "Analysis job %s for news #%s -> %s after attempt %s: %s",
            job.job_id,
            job.news_id,
            status,
            job.attempts,
            exc,
        )

    def process_batch(self, limit: int | None = None) -> int:
        limit = limit or self.settings.ai_batch_size
        processed = 0
        for _ in range(limit):
            if self.stop_event.is_set():
                break
            if not self.process_one():
                break
            processed += 1
        return processed

    def run_once(self, limit: int | None = None) -> int:
        self.enqueue_available_news()
        return self.process_batch(limit)

    def run_forever(self) -> None:
        self.install_signal_handlers()
        self.log.info(
            "AI News Analysis Service started worker=%s provider=%s model_router=%s key=%s",
            self.worker_id,
            self.settings.ai_provider,
            self.settings.ai_model,
            self.analysis_key,
        )
        loop_failures = 0
        while not self.stop_event.is_set():
            try:
                enqueued = self.enqueue_available_news()
                processed = self.process_batch()
                loop_failures = 0
                if enqueued or processed:
                    self.log.info("AI queue cycle: enqueued=%s processed=%s", enqueued, processed)
                    continue

                now_monotonic = time.monotonic()
                if now_monotonic - self._last_idle_log >= self.settings.ai_idle_log_seconds:
                    summary = self.db.analysis_summary(self.analysis_key)
                    self.log.info("AI service idle and healthy: %s", json.dumps(summary))
                    self._last_idle_log = now_monotonic
                self.stop_event.wait(self.settings.ai_poll_seconds)
            except Exception:
                loop_failures += 1
                delay = min(60.0, 2.0**min(loop_failures, 6))
                self.log.exception(
                    "AI service loop survived an unexpected failure; restarting in %.1fs",
                    delay,
                )
                self.stop_event.wait(delay)

        self.log.info("AI News Analysis Service stopped")
