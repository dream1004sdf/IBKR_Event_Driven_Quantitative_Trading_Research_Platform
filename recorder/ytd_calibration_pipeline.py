from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .ai_provider import PermanentAnalysisError, TransientAnalysisError
from .analysis_database import AnalysisDatabase
from .external_market_sources import ExternalMarketSignalCollector
from .fred_sources import FredCollector, FredCsvClient
from .ibkr_historical_news import IBKRHistoricalNewsBackfiller, parse_cli_datetime
from .macro_universe import FRED_YTD_PIPELINE_SERIES
from .macro_sources import MacroOfficialClient, MacroOfficialCollector
from .market_outcomes import ReplayMarketOutcomeBuilder, YahooChartDailyPriceSource, parse_horizons
from .replay_corpus import ReplayCorpusBuilder
from .replay_scoring import HeuristicReplayScorer, OpenAIReplayScorer, ReplayScoringEngine
from .sec_edgar import SecEdgarClient, SecEdgarCollector
from .sec_financials import DEFAULT_FINANCIAL_FACT_TAGS, SecCompanyFactsClient, SecFinancialFactsCollector
from .surprise_adjustments import OpenAISurpriseAdjustmentAnalyzer, ReplaySurpriseAdjustmentEngine
from .technical_indicators import TechnicalIndicatorBuilder


DEFAULT_YTD_SYMBOLS = (
    "MU",
    "NVDA",
    "AVGO",
    "AAPL",
    "MSFT",
    "AMZN",
    "META",
    "GOOGL",
    "TSLA",
    "AMD",
    "NFLX",
    "ORCL",
    "INTC",
    "QCOM",
    "CRM",
    "ADBE",
    "IBM",
    "CSCO",
    "TXN",
    "NOW",
    "INTU",
    "SNOW",
    "LRCX",
    "SPY",
    "QQQ",
    "SOXX",
    "IWM",
    "TLT",
    "UUP",
    "GLD",
    "USO",
    "XLE",
    "ITA",
    "TSM",
    "XOM",
    "CVX",
    "EWY",
)

DEFAULT_FRED_SERIES = FRED_YTD_PIPELINE_SERIES

YTD_PIPELINE_VERSION = "ytd-calibration-pipeline-v1"


@dataclass(frozen=True)
class YtdCalibrationConfig:
    start: str
    end: str
    symbols: tuple[str, ...]
    stages: tuple[str, ...]
    sec_limit_per_symbol: int = 5
    financial_limit_per_symbol: int = 250
    macro_limit: int = 5
    fred_limit_per_series: int = 260
    external_limit: int = 50
    ibkr_news_limit_per_symbol: int = 25
    ibkr_news_providers: tuple[str, ...] | None = ("BZ",)
    technical_limit_per_symbol: int = 260
    replay_event_type: str | None = None
    replay_limit: int = 5000
    outcome_limit: int = 5000
    score_limit: int = 1000
    openai_score_limit: int = 0
    adjustment_scorer: str = "auto"
    adjustment_limit: int = 1000
    openai_adjustment_limit: int = 0
    output_dir: Path = Path("exports")
    openai_model: str | None = None
    openai_scorer_version: str | None = None
    reasoning_effort: str = "low"
    max_input_chars: int = 16000
    dry_run: bool = False


def parse_csv(value: str | None, default: tuple[str, ...]) -> tuple[str, ...]:
    if not value:
        return default
    return tuple(dict.fromkeys(item.strip().upper() for item in value.split(",") if item.strip()))


def parse_stages(value: str | None) -> tuple[str, ...]:
    default = (
        "sec",
        "financials",
        "macro",
        "fred",
        "external",
        "ibkr_news",
        "technical",
        "corpus",
        "outcomes",
        "scoring",
        "adjustments",
        "reports",
    )
    if not value or value.strip().lower() == "all":
        return default
    return tuple(item.strip().lower() for item in value.split(",") if item.strip())


def parse_replay_event_type(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if not normalized or normalized == "all":
        return None
    return normalized


class YtdCalibrationPipeline:
    def __init__(self, settings: Any, config: YtdCalibrationConfig) -> None:
        self.settings = settings
        self.config = config
        self.db = AnalysisDatabase(settings.database_path)

    def run(self) -> dict[str, Any]:
        started_at = datetime.now(UTC).isoformat()
        results: dict[str, Any] = {
            "pipeline_version": YTD_PIPELINE_VERSION,
            "started_at": started_at,
            "start": self.config.start,
            "end": self.config.end,
            "symbols": list(self.config.symbols),
            "stages": list(self.config.stages),
            "replay_event_type": self.config.replay_event_type or "all",
            "dry_run": self.config.dry_run,
            "stage_results": {},
            "errors": [],
        }
        if self.config.dry_run:
            results["planned_actions"] = self._planned_actions()
            return self._write_manifest(results)

        for stage in self.config.stages:
            try:
                handler = getattr(self, f"_stage_{stage}", None)
                if handler is None:
                    results["errors"].append({"stage": stage, "error": "unknown_stage"})
                    continue
                results["stage_results"][stage] = handler()
            except (PermanentAnalysisError, TransientAnalysisError) as exc:
                results["errors"].append({"stage": stage, "error": f"{exc.__class__.__name__}: {str(exc)[:1000]}"})
            except Exception as exc:  # Keep long YTD runs moving and audit the failed stage.
                results["errors"].append({"stage": stage, "error": f"{exc.__class__.__name__}: {str(exc)[:1000]}"})
        results["finished_at"] = datetime.now(UTC).isoformat()
        return self._write_manifest(results)

    def _planned_actions(self) -> list[str]:
        return [
            "SEC EDGAR 8-K/10-Q/10-K sync",
            "SEC Company Facts financial-statement sync",
            "Fed/BLS/BEA official macro snapshot sync",
            "FRED macro time-series sync",
            "Marketaux/Alpha Vantage external signal sync",
            "IBKR/Benzinga historical-news backfill",
            "Yahoo daily technical-indicator build",
            "Official-document replay corpus build (all replay-eligible event types unless filtered)",
            "Yahoo 1D/5D replay outcome build before scoring",
            "Heuristic and optional OpenAI replay scoring",
            "Deterministic and optional OpenAI surprise-adjustment note build after scoring",
            "Leaderboard/adjustment report export",
        ]

    def _write_manifest(self, results: dict[str, Any]) -> dict[str, Any]:
        self.config.output_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        path = self.config.output_dir / f"m46_ytd_calibration_run_{stamp}.json"
        path.write_text(json.dumps(results, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
        results["manifest_path"] = str(path)
        return results

    def _stage_sec(self) -> dict[str, Any]:
        client = SecEdgarClient(
            user_agent=self.settings.sec_edgar_user_agent,
            timeout_seconds=self.settings.sec_edgar_timeout_seconds,
            max_bytes=self.settings.sec_edgar_max_bytes,
            min_interval_seconds=self.settings.sec_edgar_min_interval_seconds,
        )
        return SecEdgarCollector(self.db, client).sync_symbols(
            self.config.symbols,
            forms=("8-K", "10-Q", "10-K"),
            limit_per_symbol=self.config.sec_limit_per_symbol,
            force=False,
        )

    def _stage_financials(self) -> dict[str, Any]:
        client = SecCompanyFactsClient(
            user_agent=self.settings.sec_edgar_user_agent,
            timeout_seconds=self.settings.sec_edgar_timeout_seconds,
            max_bytes=max(self.settings.sec_edgar_max_bytes, 8_000_000),
            min_interval_seconds=self.settings.sec_edgar_min_interval_seconds,
        )
        return SecFinancialFactsCollector(self.db, client).sync_symbols(
            self.config.symbols,
            tags=DEFAULT_FINANCIAL_FACT_TAGS,
            limit_per_symbol=self.config.financial_limit_per_symbol,
        )

    def _stage_macro(self) -> dict[str, Any]:
        client = MacroOfficialClient(
            user_agent=self.settings.official_source_user_agent,
            timeout_seconds=self.settings.official_source_timeout_seconds,
            max_bytes=self.settings.official_source_max_bytes,
            min_interval_seconds=0.2,
        )
        return MacroOfficialCollector(self.db, client).sync("all", limit=self.config.macro_limit)

    def _stage_fred(self) -> dict[str, Any]:
        collector = FredCollector(
            self.db,
            FredCsvClient(timeout_seconds=self.settings.official_source_timeout_seconds, retries=2, api_key=self.settings.fred_api_key),
        )
        return collector.sync(
            DEFAULT_FRED_SERIES,
            start=self.config.start,
            end=self.config.end,
            limit_per_series=self.config.fred_limit_per_series,
        )

    def _stage_external(self) -> dict[str, Any]:
        collector = ExternalMarketSignalCollector(self.db, timeout_seconds=self.settings.external_source_timeout_seconds)
        result: dict[str, Any] = {}
        if self.settings.marketaux_api_key:
            result["marketaux"] = collector.collect_marketaux(
                api_key=self.settings.marketaux_api_key,
                limit=self.config.external_limit,
                symbols=",".join(self.config.symbols),
            )
        else:
            result["marketaux"] = {"requested": 0, "created": 0, "updated": 0, "errors": ["MARKETAUX_API_KEY missing"]}
        if self.settings.alpha_vantage_api_key:
            result["alpha_vantage"] = collector.collect_alpha_vantage(
                api_key=self.settings.alpha_vantage_api_key,
                tickers=",".join(self.config.symbols[:12]),
                limit=self.config.external_limit,
            )
        else:
            result["alpha_vantage"] = {"requested": 0, "created": 0, "updated": 0, "errors": ["ALPHA_VANTAGE_API_KEY missing"]}
        return result

    def _stage_ibkr_news(self) -> dict[str, Any]:
        backfiller = IBKRHistoricalNewsBackfiller(
            self.settings,
            symbols=self.config.symbols,
            start=parse_cli_datetime(self.config.start),
            end=parse_cli_datetime(self.config.end, end_of_day=True),
            provider_codes=self.config.ibkr_news_providers,
            limit_per_symbol=self.config.ibkr_news_limit_per_symbol,
            fetch_article_body=True,
            client_id=self.settings.client_id + 2600,
            timeout_seconds=45.0,
        )
        return backfiller.run_backfill().as_dict()

    def _stage_technical(self) -> dict[str, Any]:
        from datetime import datetime as dt

        builder = TechnicalIndicatorBuilder(self.db, YahooChartDailyPriceSource())
        return builder.build(
            symbols=self.config.symbols,
            start=dt.strptime(self.config.start, "%Y-%m-%d").date(),
            end=dt.strptime(self.config.end, "%Y-%m-%d").date(),
            lookback_days=120,
            limit_per_symbol=self.config.technical_limit_per_symbol,
        )

    def _stage_corpus(self) -> dict[str, Any]:
        builder = ReplayCorpusBuilder(self.db)
        return builder.build(
            start=self.config.start,
            end=self.config.end,
            event_type=self.config.replay_event_type,
            limit=self.config.replay_limit,
        )

    def _stage_outcomes(self) -> dict[str, Any]:
        builder = ReplayMarketOutcomeBuilder(self.db, YahooChartDailyPriceSource())
        return builder.build(
            start=self.config.start,
            end=self.config.end,
            event_type=self.config.replay_event_type,
            horizons=parse_horizons("1D,5D"),
            benchmark_symbol="SPY",
            limit=self.config.outcome_limit,
        )

    def _stage_scoring(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        result["heuristic"] = ReplayScoringEngine(self.db, HeuristicReplayScorer()).build(
            event_type=self.config.replay_event_type,
            price_source="yahoo_chart_daily_adjclose",
            limit=self.config.score_limit,
            only_missing=True,
        )
        if self.config.openai_score_limit > 0:
            scorer = OpenAIReplayScorer(
                api_key=self.settings.openai_api_key or "",
                model=self.config.openai_model or self.settings.ai_model,
                timeout_seconds=self.settings.ai_api_timeout_seconds,
                reasoning_effort=self.config.reasoning_effort,
                max_input_chars=self.config.max_input_chars,
                scorer_version=self.config.openai_scorer_version or None,
            )
            result["openai"] = ReplayScoringEngine(self.db, scorer).build(
                event_type=self.config.replay_event_type,
                price_source="yahoo_chart_daily_adjclose",
                limit=self.config.openai_score_limit,
                only_missing=True,
            )
        else:
            result["openai"] = {"skipped": "openai_score_limit is 0"}
        return result

    def _stage_adjustments(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        scorer_name, scorer_version = self._adjustment_score_target()
        result["deterministic"] = ReplaySurpriseAdjustmentEngine(self.db).build(
            event_type=self.config.replay_event_type,
            price_source="yahoo_chart_daily_adjclose",
            scorer_name=scorer_name,
            scorer_version=scorer_version,
            start=self.config.start,
            end=self.config.end,
            limit=self.config.adjustment_limit,
            misses_only=True,
        )
        if self.config.openai_adjustment_limit > 0:
            analyzer = OpenAISurpriseAdjustmentAnalyzer(
                api_key=self.settings.openai_api_key or "",
                model=self.config.openai_model or self.settings.ai_model,
                timeout_seconds=self.settings.ai_api_timeout_seconds,
                reasoning_effort=self.config.reasoning_effort,
                max_input_chars=self.config.max_input_chars,
            )
            result["openai"] = ReplaySurpriseAdjustmentEngine(self.db, analyzer=analyzer).build(
                event_type=self.config.replay_event_type,
                price_source="yahoo_chart_daily_adjclose",
                scorer_name=scorer_name,
                scorer_version=scorer_version,
                start=self.config.start,
                end=self.config.end,
                limit=self.config.openai_adjustment_limit,
                misses_only=True,
            )
        else:
            result["openai"] = {"skipped": "openai_adjustment_limit is 0"}
        result["score_target"] = {"scorer_name": scorer_name, "scorer_version": scorer_version}
        return result

    def _stage_reports(self) -> dict[str, Any]:
        from .replay_scoring import write_leaderboard_markdown
        from .surprise_adjustments import write_adjustment_markdown

        self.config.output_dir.mkdir(parents=True, exist_ok=True)
        leaderboard = self.db.replay_score_leaderboard_rows(
            event_type=self.config.replay_event_type,
            price_source="yahoo_chart_daily_adjclose",
            min_rows=1,
            limit=50,
        )
        adjustment_rows = self.db.recent_replay_surprise_adjustment_notes(
            scorer_name=self._adjustment_score_target()[0],
            scorer_version=self._adjustment_score_target()[1],
            adjustment_version="surprise-adjustment-notes-v3",
            limit=100,
        )
        leaderboard_path = write_leaderboard_markdown(
            self.config.output_dir / "m46_ytd_replay_score_leaderboard.md",
            leaderboard,
            filters={"pipeline": YTD_PIPELINE_VERSION, "start": self.config.start, "end": self.config.end},
        )
        adjustment_path = write_adjustment_markdown(
            self.config.output_dir / "m46_ytd_surprise_adjustments.md",
            adjustment_rows,
            filters={"pipeline": YTD_PIPELINE_VERSION, "start": self.config.start, "end": self.config.end},
        )
        return {
            "leaderboard_rows": len(leaderboard),
            "adjustment_rows": len(adjustment_rows),
            "leaderboard_path": str(leaderboard_path),
            "adjustment_path": str(adjustment_path),
        }

    def _adjustment_score_target(self) -> tuple[str, str]:
        requested = self.config.adjustment_scorer.strip().lower()
        if requested == "auto":
            requested = "openai" if self.config.openai_score_limit > 0 else "heuristic"
        if requested == "heuristic":
            heuristic = HeuristicReplayScorer()
            return heuristic.scorer_name, heuristic.scorer_version
        if requested == "openai":
            return "openai", self.config.openai_scorer_version or "openai-replay-score-v6"
        raise ValueError("adjustment_scorer must be one of: auto, heuristic, openai")
