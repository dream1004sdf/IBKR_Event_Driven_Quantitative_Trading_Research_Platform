from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from dotenv import load_dotenv


def _csv(value: str | None, default: Iterable[str]) -> tuple[str, ...]:
    if not value:
        return tuple(default)
    return tuple(item.strip().upper() for item in value.split(",") if item.strip())


def _bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _int(value: str | None, default: int, minimum: int | None = None) -> int:
    parsed = int(value) if value is not None and value.strip() else default
    if minimum is not None and parsed < minimum:
        raise ValueError(f"Expected integer >= {minimum}, got {parsed}")
    return parsed


def _float(value: str | None, default: float, minimum: float | None = None) -> float:
    parsed = float(value) if value is not None and value.strip() else default
    if minimum is not None and parsed < minimum:
        raise ValueError(f"Expected number >= {minimum}, got {parsed}")
    return parsed


@dataclass(frozen=True)
class Settings:
    # IBKR recorder
    host: str = "127.0.0.1"
    port: int = 4001
    client_id: int = 21
    market_data_type: int = 1
    ibkr_reconnect_enabled: bool = True
    ibkr_reconnect_connect_timeout_seconds: float = 15.0
    ibkr_reconnect_check_seconds: float = 5.0
    ibkr_reconnect_initial_delay_seconds: float = 3.0
    ibkr_reconnect_max_delay_seconds: float = 60.0
    ibkr_reconnect_backoff_multiplier: float = 2.0
    ibkr_connection_status_json_path: Path = Path("exports/ibkr_connection_status.json")
    watch_symbols: tuple[str, ...] = ("MU", "NVDA", "AVGO", "QQQ", "SPY")
    benchmark_symbols: tuple[str, ...] = ("SPY", "QQQ", "SOXX", "IWM", "TLT")
    geopolitical_news_symbols: tuple[str, ...] = (
        "SPY",
        "QQQ",
        "TLT",
        "SOXX",
        "UUP",
        "GLD",
        "USO",
        "XLE",
        "ITA",
        "TSM",
        "NVDA",
        "XOM",
        "CVX",
        "EWY",
    )
    news_provider_codes: tuple[str, ...] = ("BZ", "DJ-N", "DJ-RT", "DJ-RTA", "DJ-RTE", "DJ-RTG", "DJNL")
    ibkr_broadtape_enabled: bool = False
    ibkr_broadtape_provider_codes: tuple[str, ...] = ("BZ",)
    fetch_article_body: bool = True
    database_path: Path = Path("market_events.db")
    log_level: str = "INFO"
    hash_window_hours: int = 24
    fuzzy_window_minutes: int = 30
    fuzzy_threshold: float = 91.0
    fuzzy_require_same_symbol: bool = True
    price_max_age_seconds: int = 180
    reaction_poll_seconds: int = 5
    reaction_max_lateness_seconds: int = 180
    reaction_max_attempts: int = 5
    reaction_retry_base_seconds: int = 5
    reaction_retry_max_seconds: int = 60
    reaction_horizons: tuple[str, ...] = (
        "1m",
        "5m",
        "15m",
        "1h",
        "next_open",
        "close",
        "1d",
        "3d",
        "5d",
    )

    # AI News Agent + immutable Prediction Recorder
    ai_provider: str = "openai"
    # AI_MODEL remains a backward-compatible alias for the primary model.
    ai_model: str = "gpt-5.4-mini"
    ai_critical_model: str = "gpt-5.5"
    ai_primary_reasoning_effort: str = "low"
    ai_critical_reasoning_effort: str = "high"
    ai_enable_critical_model: bool = True
    ai_critical_event_types: tuple[str, ...] = (
        "EARNINGS",
        "GUIDANCE",
        "FOMC",
        "FED_SPEECH",
        "MACRO",
        "CPI",
        "EMPLOYMENT",
        "GEOPOLITICAL",
    )
    ai_escalate_impact_score: float = 0.75
    ai_escalate_confidence_below: float = 0.55
    ai_critical_fallback_to_primary: bool = True
    ai_critical_fallback_on_transient: bool = False
    ai_critical_use_article_body: bool = False
    ai_critical_max_body_chars: int = 30000
    ai_critical_api_timeout_seconds: float = 180.0
    ai_prompt_version: str = "news-agent-v12-external-signal-source-desk"
    openai_api_key: str | None = None
    ai_poll_seconds: int = 5
    ai_batch_size: int = 3
    ai_max_attempts: int = 5
    ai_retry_base_seconds: int = 15
    ai_retry_max_seconds: int = 900
    ai_job_stale_seconds: int = 300
    ai_body_wait_seconds: int = 8
    ai_backfill_limit: int = 1000
    ai_backfill_max_age_days: int = 30
    ai_api_timeout_seconds: float = 90.0
    ai_max_body_chars: int = 12000
    ai_use_article_body: bool = False
    ai_allow_heuristic_fallback: bool = False
    ai_horizons: tuple[str, ...] = ("15M", "1H", "CLOSE", "1D", "3D")
    ai_idle_log_seconds: int = 300
    ai_integrity_recovery_enabled: bool = True
    ai_integrity_recovery_min_impact: float = 0.40
    ai_integrity_recovery_min_validated_facts: int = 2
    ai_integrity_recovery_min_high_facts: int = 1
    ai_integrity_recovery_warning_reject_ratio: float = 0.45
    ai_integrity_recovery_partial_calibration_only: bool = True

    # Event grounding, official-source enrichment, clustering, and prediction quality
    official_source_auto_fetch: bool = False
    official_source_timeout_seconds: float = 15.0
    official_source_max_bytes: int = 2_000_000
    official_source_user_agent: str = "IBKRMarketEventRecorder/0.11 local-user"
    sec_edgar_timeout_seconds: float = 20.0
    sec_edgar_max_bytes: int = 4_000_000
    sec_edgar_min_interval_seconds: float = 0.2
    sec_edgar_user_agent: str = "IBKRMarketEventRecorder/0.14 (configure SEC_EDGAR_USER_AGENT with your contact email)"
    fred_api_key: str | None = None
    marketaux_api_key: str | None = None
    alpha_vantage_api_key: str | None = None
    kosis_api_key: str | None = None
    bok_ecos_api_key: str | None = None
    estat_app_id: str | None = None
    external_source_timeout_seconds: float = 20.0
    event_cluster_window_hours: int = 48
    event_cluster_similarity_threshold: float = 84.0
    prediction_min_analysis_confidence: float = 0.45
    prediction_min_confidence: float = 0.45
    prediction_min_top_probability: float = 0.50
    prediction_min_probability_edge: float = 0.08
    prediction_max_range_15m: float = 0.05
    prediction_max_range_1h: float = 0.08
    prediction_max_range_close: float = 0.12
    prediction_max_range_1d: float = 0.16
    prediction_max_range_3d: float = 0.24
    prediction_official_required_event_types: tuple[str, ...] = (
        "FOMC", "CPI", "EMPLOYMENT", "MACRO"
    )
    prediction_min_validated_facts_without_official: int = 2

    # Source-number economic plausibility checks. These complement text integrity:
    # a value can be copied correctly from a headline and still be economically suspect.
    economic_plausibility_enabled: bool = True
    economic_target_max_price_multiple: float = 3.0
    economic_target_min_price_multiple: float = 0.10
    economic_target_max_change_ratio: float = 1.0
    economic_fundamental_max_actual_expected_ratio: float = 5.0
    economic_fundamental_max_actual_prior_ratio: float = 10.0

    # Contract and event-time quote integrity. Failed quotes are preserved for audit
    # but cannot become reaction baselines or economic reference prices.
    quote_integrity_enabled: bool = True
    quote_integrity_max_spread_ratio: float = 0.05
    quote_integrity_max_last_midpoint_deviation: float = 0.10
    quote_integrity_max_close_reference_deviation: float = 0.75
    quote_integrity_require_stock_contract: bool = True

    def __post_init__(self) -> None:
        allowed_efforts = {"none", "low", "medium", "high", "xhigh"}
        if self.ai_primary_reasoning_effort not in allowed_efforts:
            raise ValueError(
                f"Invalid AI_PRIMARY_REASONING_EFFORT={self.ai_primary_reasoning_effort!r}"
            )
        if self.ai_critical_reasoning_effort not in allowed_efforts:
            raise ValueError(
                f"Invalid AI_CRITICAL_REASONING_EFFORT={self.ai_critical_reasoning_effort!r}"
            )
        for name, value in (
            ("AI_ESCALATE_IMPACT_SCORE", self.ai_escalate_impact_score),
            ("AI_ESCALATE_CONFIDENCE_BELOW", self.ai_escalate_confidence_below),
            ("AI_INTEGRITY_RECOVERY_MIN_IMPACT", self.ai_integrity_recovery_min_impact),
            ("AI_INTEGRITY_RECOVERY_WARNING_REJECT_RATIO", self.ai_integrity_recovery_warning_reject_ratio),
        ):
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be between 0 and 1, got {value}")
        for name, value in (
            ("PREDICTION_MIN_ANALYSIS_CONFIDENCE", self.prediction_min_analysis_confidence),
            ("PREDICTION_MIN_CONFIDENCE", self.prediction_min_confidence),
            ("PREDICTION_MIN_TOP_PROBABILITY", self.prediction_min_top_probability),
            ("PREDICTION_MIN_PROBABILITY_EDGE", self.prediction_min_probability_edge),
        ):
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be between 0 and 1, got {value}")
        for name, value in (
            ("ECONOMIC_TARGET_MAX_PRICE_MULTIPLE", self.economic_target_max_price_multiple),
            ("ECONOMIC_TARGET_MIN_PRICE_MULTIPLE", self.economic_target_min_price_multiple),
            ("ECONOMIC_TARGET_MAX_CHANGE_RATIO", self.economic_target_max_change_ratio),
            ("ECONOMIC_FUNDAMENTAL_MAX_ACTUAL_EXPECTED_RATIO", self.economic_fundamental_max_actual_expected_ratio),
            ("ECONOMIC_FUNDAMENTAL_MAX_ACTUAL_PRIOR_RATIO", self.economic_fundamental_max_actual_prior_ratio),
            ("QUOTE_INTEGRITY_MAX_SPREAD_RATIO", self.quote_integrity_max_spread_ratio),
            ("QUOTE_INTEGRITY_MAX_LAST_MIDPOINT_DEVIATION", self.quote_integrity_max_last_midpoint_deviation),
            ("QUOTE_INTEGRITY_MAX_CLOSE_REFERENCE_DEVIATION", self.quote_integrity_max_close_reference_deviation),
        ):
            if value <= 0:
                raise ValueError(f"{name} must be > 0, got {value}")
        if self.economic_target_min_price_multiple >= self.economic_target_max_price_multiple:
            raise ValueError("Economic target minimum multiple must be below maximum multiple")
        if self.reaction_retry_max_seconds < self.reaction_retry_base_seconds:
            raise ValueError("REACTION_RETRY_MAX_SECONDS must be >= REACTION_RETRY_BASE_SECONDS")
        if self.ibkr_reconnect_connect_timeout_seconds <= 0:
            raise ValueError("IBKR_RECONNECT_CONNECT_TIMEOUT_SECONDS must be > 0")
        if self.ibkr_reconnect_check_seconds <= 0:
            raise ValueError("IBKR_RECONNECT_CHECK_SECONDS must be > 0")
        if self.ibkr_reconnect_initial_delay_seconds < 0:
            raise ValueError("IBKR_RECONNECT_INITIAL_DELAY_SECONDS must be >= 0")
        if self.ibkr_reconnect_max_delay_seconds < self.ibkr_reconnect_initial_delay_seconds:
            raise ValueError("IBKR_RECONNECT_MAX_DELAY_SECONDS must be >= IBKR_RECONNECT_INITIAL_DELAY_SECONDS")
        if self.ibkr_reconnect_backoff_multiplier < 1.0:
            raise ValueError("IBKR_RECONNECT_BACKOFF_MULTIPLIER must be >= 1.0")
        if not self.ai_model:
            raise ValueError("AI primary model cannot be empty")
        if self.ai_enable_critical_model and not self.ai_critical_model:
            raise ValueError("AI critical model cannot be empty when enabled")

    @property
    def all_price_symbols(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((*self.watch_symbols, *self.benchmark_symbols, *self.geopolitical_news_symbols)))

    @property
    def all_news_symbols(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((*self.watch_symbols, *self.benchmark_symbols, *self.geopolitical_news_symbols)))

    @property
    def analysis_key(self) -> str:
        return (
            f"{self.ai_provider.lower()}|primary={self.ai_model}|"
            f"critical={self.ai_critical_model if self.ai_enable_critical_model else 'disabled'}|"
            f"prompt={self.ai_prompt_version}|"
            f"quality={self.prediction_min_analysis_confidence:.2f},"
            f"{self.prediction_min_confidence:.2f},"
            f"{self.prediction_min_top_probability:.2f},"
            f"{self.prediction_min_probability_edge:.2f}|"
            f"official={','.join(self.prediction_official_required_event_types)}|"
            f"facts={self.prediction_min_validated_facts_without_official}|"
            f"recovery={int(self.ai_integrity_recovery_enabled)},"
            f"{self.ai_integrity_recovery_min_impact:.2f},"
            f"{self.ai_integrity_recovery_min_validated_facts},"
            f"{self.ai_integrity_recovery_min_high_facts}|"
            f"economic={int(self.economic_plausibility_enabled)},"
            f"{self.economic_target_max_price_multiple:.2f},"
            f"{self.economic_target_min_price_multiple:.2f},"
            f"{self.economic_target_max_change_ratio:.2f},"
            f"{self.economic_fundamental_max_actual_expected_ratio:.2f},"
            f"{self.economic_fundamental_max_actual_prior_ratio:.2f}|"
            f"quote={int(self.quote_integrity_enabled)},"
            f"{self.quote_integrity_max_spread_ratio:.4f},"
            f"{self.quote_integrity_max_last_midpoint_deviation:.4f},"
            f"{self.quote_integrity_max_close_reference_deviation:.4f},"
            f"{int(self.quote_integrity_require_stock_contract)}"
        )

    @classmethod
    def from_env(cls, env_path: str | Path | None = None) -> "Settings":
        load_dotenv(dotenv_path=env_path, override=False)
        return cls(
            host=os.getenv("IBKR_HOST", "127.0.0.1"),
            port=_int(os.getenv("IBKR_PORT"), 4001, 1),
            client_id=_int(os.getenv("IBKR_CLIENT_ID"), 21, 0),
            market_data_type=_int(os.getenv("IBKR_MARKET_DATA_TYPE"), 1, 1),
            ibkr_reconnect_enabled=_bool(os.getenv("IBKR_RECONNECT_ENABLED"), True),
            ibkr_reconnect_connect_timeout_seconds=_float(
                os.getenv("IBKR_RECONNECT_CONNECT_TIMEOUT_SECONDS"), 15.0, 1.0
            ),
            ibkr_reconnect_check_seconds=_float(
                os.getenv("IBKR_RECONNECT_CHECK_SECONDS"), 5.0, 0.5
            ),
            ibkr_reconnect_initial_delay_seconds=_float(
                os.getenv("IBKR_RECONNECT_INITIAL_DELAY_SECONDS"), 3.0, 0.0
            ),
            ibkr_reconnect_max_delay_seconds=_float(
                os.getenv("IBKR_RECONNECT_MAX_DELAY_SECONDS"), 60.0, 1.0
            ),
            ibkr_reconnect_backoff_multiplier=_float(
                os.getenv("IBKR_RECONNECT_BACKOFF_MULTIPLIER"), 2.0, 1.0
            ),
            ibkr_connection_status_json_path=Path(
                os.getenv("IBKR_CONNECTION_STATUS_JSON_PATH", "exports/ibkr_connection_status.json")
            ),
            watch_symbols=_csv(os.getenv("WATCH_SYMBOLS"), cls.watch_symbols),
            benchmark_symbols=_csv(
                os.getenv("BENCHMARK_SYMBOLS"), cls.benchmark_symbols
            ),
            geopolitical_news_symbols=_csv(
                os.getenv("GEOPOLITICAL_NEWS_SYMBOLS"), cls.geopolitical_news_symbols
            ),
            news_provider_codes=_csv(
                os.getenv("NEWS_PROVIDER_CODES"), cls.news_provider_codes
            ),
            ibkr_broadtape_enabled=_bool(
                os.getenv("IBKR_BROADTAPE_ENABLED"), cls.ibkr_broadtape_enabled
            ),
            ibkr_broadtape_provider_codes=_csv(
                os.getenv("IBKR_BROADTAPE_PROVIDER_CODES"),
                cls.ibkr_broadtape_provider_codes,
            ),
            fetch_article_body=_bool(os.getenv("FETCH_ARTICLE_BODY"), True),
            database_path=Path(os.getenv("DATABASE_PATH", "market_events.db")),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            hash_window_hours=_int(os.getenv("HASH_DEDUP_WINDOW_HOURS"), 24, 1),
            fuzzy_window_minutes=_int(
                os.getenv("FUZZY_DEDUP_WINDOW_MINUTES"), 30, 1
            ),
            fuzzy_threshold=_float(os.getenv("FUZZY_DEDUP_THRESHOLD"), 91.0, 0),
            fuzzy_require_same_symbol=_bool(
                os.getenv("FUZZY_REQUIRE_SAME_SYMBOL"), True
            ),
            price_max_age_seconds=_int(os.getenv("PRICE_MAX_AGE_SECONDS"), 180, 1),
            reaction_poll_seconds=_int(os.getenv("REACTION_POLL_SECONDS"), 5, 1),
            reaction_max_lateness_seconds=_int(
                os.getenv("REACTION_MAX_LATENESS_SECONDS"), 180, 1
            ),
            reaction_max_attempts=_int(os.getenv("REACTION_MAX_ATTEMPTS"), 5, 1),
            reaction_retry_base_seconds=_int(os.getenv("REACTION_RETRY_BASE_SECONDS"), 5, 1),
            reaction_retry_max_seconds=_int(os.getenv("REACTION_RETRY_MAX_SECONDS"), 60, 1),
            reaction_horizons=_csv(
                os.getenv("REACTION_HORIZONS"), cls.reaction_horizons
            ),
            ai_provider=os.getenv("AI_PROVIDER", "openai").strip().lower(),
            ai_model=(
                os.getenv("AI_PRIMARY_MODEL")
                or os.getenv("AI_MODEL")
                or "gpt-5.4-mini"
            ).strip(),
            ai_critical_model=os.getenv("AI_CRITICAL_MODEL", "gpt-5.5").strip(),
            ai_primary_reasoning_effort=os.getenv(
                "AI_PRIMARY_REASONING_EFFORT", "low"
            ).strip().lower(),
            ai_critical_reasoning_effort=os.getenv(
                "AI_CRITICAL_REASONING_EFFORT", "high"
            ).strip().lower(),
            ai_enable_critical_model=_bool(
                os.getenv("AI_ENABLE_CRITICAL_MODEL"), True
            ),
            ai_critical_event_types=_csv(
                os.getenv("AI_CRITICAL_EVENT_TYPES"),
                cls.ai_critical_event_types,
            ),
            ai_escalate_impact_score=_float(
                os.getenv("AI_ESCALATE_IMPACT_SCORE"), 0.75, 0.0
            ),
            ai_escalate_confidence_below=_float(
                os.getenv("AI_ESCALATE_CONFIDENCE_BELOW"), 0.55, 0.0
            ),
            ai_critical_fallback_to_primary=_bool(
                os.getenv("AI_CRITICAL_FALLBACK_TO_PRIMARY"), True
            ),
            ai_critical_fallback_on_transient=_bool(
                os.getenv("AI_CRITICAL_FALLBACK_ON_TRANSIENT"), False
            ),
            ai_critical_use_article_body=_bool(
                os.getenv("AI_CRITICAL_USE_ARTICLE_BODY"), False
            ),
            ai_critical_max_body_chars=_int(
                os.getenv("AI_CRITICAL_MAX_BODY_CHARS"), 30000, 1000
            ),
            ai_critical_api_timeout_seconds=_float(
                os.getenv("AI_CRITICAL_API_TIMEOUT_SECONDS"), 180.0, 10.0
            ),
            ai_prompt_version=os.getenv(
                "AI_PROMPT_VERSION", "news-agent-v12-external-signal-source-desk"
            ).strip(),
            openai_api_key=os.getenv("OPENAI_API_KEY") or None,
            ai_poll_seconds=_int(os.getenv("AI_POLL_SECONDS"), 5, 1),
            ai_batch_size=_int(os.getenv("AI_BATCH_SIZE"), 3, 1),
            ai_max_attempts=_int(os.getenv("AI_MAX_ATTEMPTS"), 5, 1),
            ai_retry_base_seconds=_int(
                os.getenv("AI_RETRY_BASE_SECONDS"), 15, 1
            ),
            ai_retry_max_seconds=_int(
                os.getenv("AI_RETRY_MAX_SECONDS"), 900, 1
            ),
            ai_job_stale_seconds=_int(
                os.getenv("AI_JOB_STALE_SECONDS"), 300, 30
            ),
            ai_body_wait_seconds=_int(os.getenv("AI_BODY_WAIT_SECONDS"), 8, 0),
            ai_backfill_limit=_int(os.getenv("AI_BACKFILL_LIMIT"), 1000, 1),
            ai_backfill_max_age_days=_int(
                os.getenv("AI_BACKFILL_MAX_AGE_DAYS"), 30, 1
            ),
            ai_api_timeout_seconds=_float(
                os.getenv("AI_API_TIMEOUT_SECONDS"), 90.0, 5.0
            ),
            ai_max_body_chars=_int(os.getenv("AI_MAX_BODY_CHARS"), 12000, 500),
            ai_use_article_body=_bool(os.getenv("AI_USE_ARTICLE_BODY"), False),
            ai_allow_heuristic_fallback=_bool(
                os.getenv("AI_ALLOW_HEURISTIC_FALLBACK"), False
            ),
            ai_horizons=_csv(os.getenv("AI_HORIZONS"), cls.ai_horizons),
            ai_idle_log_seconds=_int(
                os.getenv("AI_IDLE_LOG_SECONDS"), 300, 30
            ),
            ai_integrity_recovery_enabled=_bool(
                os.getenv("AI_INTEGRITY_RECOVERY_ENABLED"), True
            ),
            ai_integrity_recovery_min_impact=_float(
                os.getenv("AI_INTEGRITY_RECOVERY_MIN_IMPACT"), 0.40, 0.0
            ),
            ai_integrity_recovery_min_validated_facts=_int(
                os.getenv("AI_INTEGRITY_RECOVERY_MIN_VALIDATED_FACTS"), 2, 0
            ),
            ai_integrity_recovery_min_high_facts=_int(
                os.getenv("AI_INTEGRITY_RECOVERY_MIN_HIGH_FACTS"), 1, 0
            ),
            ai_integrity_recovery_warning_reject_ratio=_float(
                os.getenv("AI_INTEGRITY_RECOVERY_WARNING_REJECT_RATIO"), 0.45, 0.0
            ),
            ai_integrity_recovery_partial_calibration_only=_bool(
                os.getenv("AI_INTEGRITY_RECOVERY_PARTIAL_CALIBRATION_ONLY"), True
            ),
            official_source_auto_fetch=_bool(os.getenv("OFFICIAL_SOURCE_AUTO_FETCH"), False),
            official_source_timeout_seconds=_float(os.getenv("OFFICIAL_SOURCE_TIMEOUT_SECONDS"), 15.0, 1.0),
            official_source_max_bytes=_int(os.getenv("OFFICIAL_SOURCE_MAX_BYTES"), 2_000_000, 10_000),
            official_source_user_agent=os.getenv("OFFICIAL_SOURCE_USER_AGENT", "IBKRMarketEventRecorder/0.11 local-user").strip(),
            sec_edgar_timeout_seconds=_float(os.getenv("SEC_EDGAR_TIMEOUT_SECONDS"), 20.0, 1.0),
            sec_edgar_max_bytes=_int(os.getenv("SEC_EDGAR_MAX_BYTES"), 4_000_000, 10_000),
            sec_edgar_min_interval_seconds=_float(os.getenv("SEC_EDGAR_MIN_INTERVAL_SECONDS"), 0.2, 0.1),
            sec_edgar_user_agent=os.getenv(
                "SEC_EDGAR_USER_AGENT",
                "IBKRMarketEventRecorder/0.14 (configure SEC_EDGAR_USER_AGENT with your contact email)",
            ).strip(),
            fred_api_key=os.getenv("FRED_API_KEY") or None,
            marketaux_api_key=os.getenv("MARKETAUX_API_KEY") or None,
            alpha_vantage_api_key=os.getenv("ALPHA_VANTAGE_API_KEY") or None,
            kosis_api_key=os.getenv("KOSIS_API_KEY") or None,
            bok_ecos_api_key=os.getenv("BOK_ECOS_API_KEY") or None,
            estat_app_id=os.getenv("ESTAT_APP_ID") or None,
            external_source_timeout_seconds=_float(os.getenv("EXTERNAL_SOURCE_TIMEOUT_SECONDS"), 20.0, 5.0),
            event_cluster_window_hours=_int(os.getenv("EVENT_CLUSTER_WINDOW_HOURS"), 48, 1),
            event_cluster_similarity_threshold=_float(os.getenv("EVENT_CLUSTER_SIMILARITY_THRESHOLD"), 84.0, 0.0),
            prediction_min_analysis_confidence=_float(os.getenv("PREDICTION_MIN_ANALYSIS_CONFIDENCE"), 0.45, 0.0),
            prediction_min_confidence=_float(os.getenv("PREDICTION_MIN_CONFIDENCE"), 0.45, 0.0),
            prediction_min_top_probability=_float(os.getenv("PREDICTION_MIN_TOP_PROBABILITY"), 0.50, 0.0),
            prediction_min_probability_edge=_float(os.getenv("PREDICTION_MIN_PROBABILITY_EDGE"), 0.08, 0.0),
            prediction_max_range_15m=_float(os.getenv("PREDICTION_MAX_RANGE_15M"), 0.05, 0.0),
            prediction_max_range_1h=_float(os.getenv("PREDICTION_MAX_RANGE_1H"), 0.08, 0.0),
            prediction_max_range_close=_float(os.getenv("PREDICTION_MAX_RANGE_CLOSE"), 0.12, 0.0),
            prediction_max_range_1d=_float(os.getenv("PREDICTION_MAX_RANGE_1D"), 0.16, 0.0),
            prediction_max_range_3d=_float(os.getenv("PREDICTION_MAX_RANGE_3D"), 0.24, 0.0),
            prediction_official_required_event_types=_csv(
                os.getenv("PREDICTION_OFFICIAL_REQUIRED_EVENT_TYPES"),
                cls.prediction_official_required_event_types,
            ),
            prediction_min_validated_facts_without_official=_int(
                os.getenv("PREDICTION_MIN_VALIDATED_FACTS_WITHOUT_OFFICIAL"), 2, 0
            ),
            economic_plausibility_enabled=_bool(
                os.getenv("ECONOMIC_PLAUSIBILITY_ENABLED"), True
            ),
            economic_target_max_price_multiple=_float(
                os.getenv("ECONOMIC_TARGET_MAX_PRICE_MULTIPLE"), 3.0, 0.01
            ),
            economic_target_min_price_multiple=_float(
                os.getenv("ECONOMIC_TARGET_MIN_PRICE_MULTIPLE"), 0.10, 0.001
            ),
            economic_target_max_change_ratio=_float(
                os.getenv("ECONOMIC_TARGET_MAX_CHANGE_RATIO"), 1.0, 0.01
            ),
            economic_fundamental_max_actual_expected_ratio=_float(
                os.getenv("ECONOMIC_FUNDAMENTAL_MAX_ACTUAL_EXPECTED_RATIO"), 5.0, 0.01
            ),
            economic_fundamental_max_actual_prior_ratio=_float(
                os.getenv("ECONOMIC_FUNDAMENTAL_MAX_ACTUAL_PRIOR_RATIO"), 10.0, 0.01
            ),
            quote_integrity_enabled=_bool(
                os.getenv("QUOTE_INTEGRITY_ENABLED"), True
            ),
            quote_integrity_max_spread_ratio=_float(
                os.getenv("QUOTE_INTEGRITY_MAX_SPREAD_RATIO"), 0.05, 0.0001
            ),
            quote_integrity_max_last_midpoint_deviation=_float(
                os.getenv("QUOTE_INTEGRITY_MAX_LAST_MIDPOINT_DEVIATION"), 0.10, 0.0001
            ),
            quote_integrity_max_close_reference_deviation=_float(
                os.getenv("QUOTE_INTEGRITY_MAX_CLOSE_REFERENCE_DEVIATION"), 0.75, 0.0001
            ),
            quote_integrity_require_stock_contract=_bool(
                os.getenv("QUOTE_INTEGRITY_REQUIRE_STOCK_CONTRACT"), True
            ),
        )
