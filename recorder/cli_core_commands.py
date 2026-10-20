from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .cli_dashboard_commands import add_dashboard_commands
from .cli_news_commands import add_external_market_commands, add_news_source_commands
from .cli_replay_execution_commands import add_replay_execution_commands



def add_core_news_ai_commands(sub):
    ibkr_news_backfill = sub.add_parser(
        "ibkr-news-backfill",
        help="Read-only IBKR historical-news backfill into canonical news storage",
    )
    ibkr_news_backfill.add_argument("--symbols", help="Comma-separated ticker symbols (default: WATCH_SYMBOLS)")
    ibkr_news_backfill.add_argument("--providers", help="Comma-separated IBKR news provider codes; default uses NEWS_PROVIDER_CODES/AUTO")
    ibkr_news_backfill.add_argument("--start", required=True, help="Inclusive UTC date/time, e.g. 2026-01-01")
    ibkr_news_backfill.add_argument("--end", required=True, help="Inclusive UTC date/time/date, e.g. 2026-06-26")
    ibkr_news_backfill.add_argument("--limit-per-symbol", type=int, default=100)
    ibkr_news_backfill.add_argument("--fetch-article-body", action="store_true", help="Also request article bodies with reqNewsArticle")
    ibkr_news_backfill.add_argument("--ibkr-client-id", type=int, help="Override IBKR clientId; defaults to IBKR_CLIENT_ID + 2000")
    ibkr_news_backfill.add_argument("--timeout", type=float, default=30.0)
    ibkr_news_backfill.add_argument("--max-pages-per-symbol", type=int, default=1, help="Follow IBKR historical-news hasMore pages by moving the request anchor backward")
    add_news_source_commands(sub)
    report = sub.add_parser("report", help="Show recorder SQLite summary")
    report.add_argument("--limit", type=int, default=20)

    sub.add_parser(
        "ai-run",
        help="Continuously analyze canonical news and record immutable predictions",
    )
    ai_once = sub.add_parser(
        "ai-once", help="Analyze a bounded batch then exit"
    )
    ai_once.add_argument("--limit", type=int, default=3)
    ai_report = sub.add_parser("ai-report", help="Show AI queue and predictions")
    ai_report.add_argument("--limit", type=int, default=20)
    ai_retry = sub.add_parser("ai-retry-dead", help="Reset dead AI jobs")
    ai_retry.add_argument("--limit", type=int, default=1000)
    attribution_backfill = sub.add_parser(
        "attribution-backfill",
        help="Append deterministic actual-subject attribution records for stored events",
    )
    attribution_backfill.add_argument("--limit", type=int)
    attribution_report = sub.add_parser(
        "attribution-report",
        help="Show latest deterministic actual-subject attribution records",
    )
    attribution_report.add_argument("--limit", type=int, default=50)
    sub.add_parser(
        "ai-diagnose",
        help="Validate AI configuration without sending an API request",
    )
    cluster_report = sub.add_parser("cluster-report", help="Show event clusters and representative sources")
    cluster_report.add_argument("--limit", type=int, default=20)
    official_add = sub.add_parser("official-add", help="Attach a public official document to a news event")
    official_add.add_argument("--news-id", type=int, required=True)
    official_add.add_argument("--source-type", required=True)
    official_add.add_argument("--source-url", required=True)
    official_add.add_argument("--title", default="")
    official_add.add_argument("--file", type=Path, required=True)
    official_add.add_argument("--no-reanalyze", action="store_true")
    official_report = sub.add_parser("official-report", help="Show official-source enrichment status")
    official_report.add_argument("--news-id", type=int)

def add_core_official_macro_commands(sub):
    sec_sync = sub.add_parser(
        "sec-sync",
        help="Collect public SEC EDGAR primary filings without creating synthetic news events",
    )
    sec_sync.add_argument("--symbols", help="Comma-separated ticker symbols (default: WATCH_SYMBOLS)")
    sec_sync.add_argument("--forms", default="8-K", help="Comma-separated form types (default: 8-K)")
    sec_sync.add_argument("--limit-per-symbol", type=int, default=5)
    sec_sync.add_argument("--force", action="store_true", help="Refetch already stored SEC filings")
    sec_report = sub.add_parser("sec-report", help="Show stored SEC EDGAR official documents")
    sec_report.add_argument("--limit", type=int, default=20)
    macro_sync = sub.add_parser(
        "macro-sync",
        help="Collect official Federal Reserve, BLS, and BEA macro-release snapshots",
    )
    macro_sync.add_argument("--source", choices=("all", "fed", "bls", "bea"), default="all")
    macro_sync.add_argument("--limit", type=int, default=5, help="RSS items per source for Fed/BEA")
    macro_report = sub.add_parser("macro-report", help="Show stored official macro documents")
    macro_report.add_argument("--source", choices=("fed", "federal_reserve", "bls", "bea"))
    macro_report.add_argument("--limit", type=int, default=20)
    macro_lineage_rebuild = sub.add_parser(
        "macro-lineage-v1-rebuild",
        help="M251B: promote stored FRED/direct macro observations into macro event clusters",
    )
    macro_lineage_rebuild.add_argument("--source", choices=("all", "fred", "economic_indicators"), default="all")
    macro_lineage_rebuild.add_argument("--start", help="Inclusive YYYY-MM-DD observation date")
    macro_lineage_rebuild.add_argument("--end", help="Inclusive YYYY-MM-DD observation date")
    macro_lineage_rebuild.add_argument("--limit", type=int)
    macro_lineage_rebuild.add_argument("--universe", choices=("global_core", "us_core", "international_core"), default="global_core")
    macro_lineage_rebuild.add_argument("--output", type=Path, default=Path("exports/m251b_macro_event_lineage_rebuild.md"))
    macro_lineage_rebuild.add_argument("--json-output-path", type=Path, default=Path("exports/m251b_macro_event_lineage_rebuild.json"))
    macro_lineage_rebuild.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    financial_facts_sync = sub.add_parser(
        "financial-facts-sync",
        help="Collect structured SEC XBRL companyfacts for financial-statement metrics",
    )
    financial_facts_sync.add_argument("--symbols", required=True, help="Comma-separated ticker symbols")
    financial_facts_sync.add_argument("--tags", help="Comma-separated SEC XBRL tags; defaults to core financial tags")
    financial_facts_sync.add_argument("--limit-per-symbol", type=int, default=250)
    financial_facts_report = sub.add_parser("financial-facts-report", help="Show stored SEC financial statement facts")
    financial_facts_report.add_argument("--symbol", help="Ticker symbol to filter")
    financial_facts_report.add_argument("--tag", help="SEC XBRL tag to filter")
    financial_facts_report.add_argument("--limit", type=int, default=50)
    fred_sync = sub.add_parser("fred-sync", help="Collect public FRED macro time-series observations")
    fred_sync.add_argument("--series", required=True, help="Comma-separated FRED series IDs, e.g. FEDFUNDS,DGS10")
    fred_sync.add_argument("--start", help="Inclusive YYYY-MM-DD observation date")
    fred_sync.add_argument("--end", help="Inclusive YYYY-MM-DD observation date")
    fred_sync.add_argument("--limit-per-series", type=int, default=5000)
    fred_sync.add_argument("--timeout", type=float, default=45.0)
    fred_sync.add_argument("--api-key", help="FRED API key; defaults to FRED_API_KEY environment variable")
    fred_sync.add_argument("--build-macro-lineage", action="store_true", help="After sync, promote stored observations into M251B macro event lineage")
    fred_report = sub.add_parser("fred-report", help="Show stored FRED observations")
    fred_report.add_argument("--series", help="FRED series ID to filter")
    fred_report.add_argument("--limit", type=int, default=50)
    fred_universe_sync = sub.add_parser(
        "fred-universe-sync",
        help="Collect a curated FRED macro/economic indicator universe",
    )
    fred_universe_sync.add_argument("--universe", choices=("global_core", "us_core", "international_core"), default="global_core")
    fred_universe_sync.add_argument("--start", default="2025-01-01", help="Inclusive YYYY-MM-DD observation date")
    fred_universe_sync.add_argument("--end", default=datetime.now(timezone.utc).date().isoformat(), help="Inclusive YYYY-MM-DD observation date")
    fred_universe_sync.add_argument("--limit-per-series", type=int, default=5000)
    fred_universe_sync.add_argument("--timeout", type=float, default=45.0)
    fred_universe_sync.add_argument("--api-key", help="FRED API key; defaults to FRED_API_KEY environment variable")
    fred_universe_sync.add_argument("--build-macro-lineage", action="store_true", help="After sync, promote stored observations into M251B macro event lineage")
    fred_universe_report = sub.add_parser(
        "fred-universe-report",
        help="Show coverage for a curated FRED macro/economic indicator universe",
    )
    fred_universe_report.add_argument("--universe", choices=("global_core", "us_core", "international_core"), default="global_core")
    fred_universe_report.add_argument("--start", default="2025-01-01")
    fred_universe_report.add_argument("--end", default=datetime.now(timezone.utc).date().isoformat())
    economic_indicator_desk = sub.add_parser(
        "economic-indicator-desk",
        help="Build a read-only Economic Indicator Specialist Desk report from stored FRED observations",
    )
    economic_indicator_desk.add_argument("--universe", choices=("global_core", "us_core", "international_core"), default="global_core")
    economic_indicator_desk.add_argument("--cutoff-date", default=datetime.now(timezone.utc).date().isoformat())
    economic_indicator_desk.add_argument("--lookback-observations", type=int, default=4)
    economic_indicator_desk.add_argument("--output", type=Path, help="Optional .md or .json report path")
    economic_indicator_desk.add_argument("--json", action="store_true", help="Print machine-readable desk JSON")
    international_macro_sync = sub.add_parser(
        "international-macro-sync",
        help="Collect direct official international economic indicators outside stale FRED/OECD feeds",
    )
    international_macro_sync.add_argument("--source", choices=("all", "eurostat", "kosis", "bok_ecos", "estat_japan"), default="eurostat")
    international_macro_sync.add_argument("--start", default="2025-01-01", help="Inclusive YYYY-MM-DD observation date")
    international_macro_sync.add_argument("--end", default=datetime.now(timezone.utc).date().isoformat(), help="Inclusive YYYY-MM-DD observation date")
    international_macro_sync.add_argument("--series", help="Comma-separated canonical direct series IDs; default collects all supported source series")
    international_macro_sync.add_argument("--timeout", type=float, default=45.0)
    international_macro_sync.add_argument("--build-macro-lineage", action="store_true", help="After sync, promote stored direct observations into M251B macro event lineage")
    international_macro_report = sub.add_parser(
        "international-macro-report",
        help="Show stored direct official international economic indicator observations",
    )
    international_macro_report.add_argument("--provider", help="Provider filter, e.g. eurostat")
    international_macro_report.add_argument("--series", help="Canonical series ID to filter")
    international_macro_report.add_argument("--limit", type=int, default=50)
    technical_indicators_build = sub.add_parser(
        "technical-indicators-build",
        help="Build daily technical indicators from Yahoo, Stooq, or IBKR historical bars",
    )
    technical_indicators_build.add_argument("--symbols", required=True, help="Comma-separated ticker symbols")
    technical_indicators_build.add_argument("--start", required=True, help="Inclusive YYYY-MM-DD date")
    technical_indicators_build.add_argument("--end", required=True, help="Inclusive YYYY-MM-DD date")
    technical_indicators_build.add_argument("--price-source", choices=("yahoo", "stooq", "ibkr"), default="yahoo")
    technical_indicators_build.add_argument("--lookback-days", type=int, default=120)
    technical_indicators_build.add_argument("--limit-per-symbol", type=int, default=1000)
    technical_indicators_build.add_argument("--ibkr-client-id", type=int, help="Override IBKR historical-data clientId")
    technical_indicators_build.add_argument("--ibkr-timeout", type=float, default=20.0)
    technical_indicators_build.add_argument("--ibkr-what-to-show", default="TRADES")
    technical_indicators_report = sub.add_parser("technical-indicators-report", help="Show stored technical indicators")
    technical_indicators_report.add_argument("--symbol", help="Ticker symbol to filter")
    technical_indicators_report.add_argument("--price-source", help="Exact stored price_source to filter")
    technical_indicators_report.add_argument("--limit", type=int, default=50)

def add_core_external_historical_commands(sub):
    add_external_market_commands(sub)
    historical_import = sub.add_parser(
        "historical-import",
        help="Import official-document-backed historical events (M6: SEC earnings/guidance first)",
    )
    historical_import.add_argument("--event-type", choices=("earnings", "guidance", "earnings_guidance"), required=True)
    historical_import.add_argument("--symbols", required=True, help="Comma-separated ticker symbols")
    historical_import.add_argument("--start", required=True, help="Inclusive YYYY-MM-DD filing date")
    historical_import.add_argument("--end", required=True, help="Inclusive YYYY-MM-DD filing date")
    historical_import.add_argument("--limit-per-symbol", type=int, default=100)
    historical_report = sub.add_parser("historical-report", help="Show imported historical event rows")
    historical_report.add_argument("--event-type", choices=("earnings", "guidance", "earnings_guidance"))
    historical_report.add_argument("--limit", type=int, default=20)
    add_replay_execution_commands(sub)
    add_dashboard_commands(sub)

def add_core_replay_overlay_commands(sub):
    replay_market_overlay = sub.add_parser(
        "replay-market-overlay",
        help="Evaluate geopolitical/macro market overlay context against stored replay scores",
    )
    replay_market_overlay.add_argument("--event-type", choices=("earnings", "guidance", "earnings_guidance"))
    replay_market_overlay.add_argument("--symbols", help="Comma-separated ticker symbols to filter")
    replay_market_overlay.add_argument("--price-source", help="Exact stored price_source to filter")
    replay_market_overlay.add_argument("--horizon", help="Outcome horizon to filter, e.g. 30M, 2H, 1D, 5D")
    replay_market_overlay.add_argument("--scorer-name", help="Stored scorer_name to filter")
    replay_market_overlay.add_argument("--model-name", help="Stored model_name to filter")
    replay_market_overlay.add_argument("--scorer-version", help="Stored scorer_version to filter")
    replay_market_overlay.add_argument("--start", help="Inclusive replay timestamp/date lower bound")
    replay_market_overlay.add_argument("--end", help="Exclusive replay timestamp/date upper bound")
    replay_market_overlay.add_argument("--signal-limit", type=int, default=500, help="External market signal rows to inspect")
    replay_market_overlay.add_argument("--min-group-rows", type=int, default=1)
    replay_market_overlay.add_argument("--limit", type=int, default=1000)
    replay_market_overlay.add_argument("--output", type=Path, help="Optional Markdown market overlay report path")
    replay_market_overlay.add_argument("--json", action="store_true", help="Print machine-readable market overlay JSON")
    replay_emergency_overlay = sub.add_parser(
        "replay-emergency-overlay",
        help="Tune emergency breaking-news overlay gates against stored replay outcomes",
    )
    replay_emergency_overlay.add_argument("--event-type", choices=("earnings", "guidance", "earnings_guidance"))
    replay_emergency_overlay.add_argument("--symbols", help="Comma-separated ticker symbols to filter")
    replay_emergency_overlay.add_argument("--price-source", help="Exact stored price_source to filter")
    replay_emergency_overlay.add_argument("--horizon", help="Outcome horizon to filter, e.g. 30M, 2H, 1D, 5D")
    replay_emergency_overlay.add_argument("--scorer-name", help="Stored scorer_name to filter")
    replay_emergency_overlay.add_argument("--model-name", help="Stored model_name to filter")
    replay_emergency_overlay.add_argument("--scorer-version", help="Stored scorer_version to filter")
    replay_emergency_overlay.add_argument("--start", help="Inclusive replay timestamp/date lower bound")
    replay_emergency_overlay.add_argument("--end", help="Exclusive replay timestamp/date upper bound")
    replay_emergency_overlay.add_argument("--signal-limit", type=int, default=5000, help="Emergency signal rows to inspect")
    replay_emergency_overlay.add_argument("--min-group-rows", type=int, default=1)
    replay_emergency_overlay.add_argument("--limit", type=int, default=1000)
    replay_emergency_overlay.add_argument("--output", type=Path, help="Optional Markdown emergency overlay tuning report path")
    replay_emergency_overlay.add_argument("--json", action="store_true", help="Print machine-readable emergency overlay JSON")
    replay_evidence_packets = sub.add_parser(
        "replay-evidence-packets",
        help="Build scorer-ready overlay + macro/economic evidence packets over stored replay scores",
    )
    replay_evidence_packets.add_argument("--event-type", choices=("earnings", "guidance", "earnings_guidance"))
    replay_evidence_packets.add_argument("--symbols", help="Comma-separated ticker symbols to filter")
    replay_evidence_packets.add_argument("--price-source", help="Exact stored price_source to filter")
    replay_evidence_packets.add_argument("--horizon", help="Outcome horizon to filter, e.g. 30M, 2H, 1D, 5D")
    replay_evidence_packets.add_argument("--scorer-name", help="Stored scorer_name to filter")
    replay_evidence_packets.add_argument("--model-name", help="Stored model_name to filter")
    replay_evidence_packets.add_argument("--scorer-version", help="Stored scorer_version to filter")
    replay_evidence_packets.add_argument("--start", help="Inclusive replay timestamp/date lower bound")
    replay_evidence_packets.add_argument("--end", help="Exclusive replay timestamp/date upper bound")
    replay_evidence_packets.add_argument("--signal-limit", type=int, default=500, help="External market signal rows to inspect")
    replay_evidence_packets.add_argument("--min-group-rows", type=int, default=1)
    replay_evidence_packets.add_argument("--limit", type=int, default=1000)
    replay_evidence_packets.add_argument("--output", type=Path, help="Optional Markdown evidence-packet report path")
    replay_evidence_packets.add_argument("--json", action="store_true", help="Print machine-readable evidence-packet JSON")
    replay_surprise_build = sub.add_parser(
        "replay-surprise-adjustments-build",
        help="Create diagnostic adjustment notes for replay scores whose expected reaction differed from realized outcomes",
    )
    replay_surprise_build.add_argument("--event-type", choices=("earnings", "guidance", "earnings_guidance"))
    replay_surprise_build.add_argument("--symbols", help="Comma-separated ticker symbols to filter")
    replay_surprise_build.add_argument("--price-source", help="Exact stored price_source to filter")
    replay_surprise_build.add_argument("--horizon", help="Outcome horizon to filter, e.g. 30M, 2H, 1D, 5D")
    replay_surprise_build.add_argument("--scorer-name", help="Stored scorer_name to filter")
    replay_surprise_build.add_argument("--model-name", help="Stored model_name to filter")
    replay_surprise_build.add_argument("--scorer-version", help="Stored scorer_version to filter")
    replay_surprise_build.add_argument("--start", help="Inclusive replay timestamp/date lower bound")
    replay_surprise_build.add_argument("--end", help="Exclusive replay timestamp/date upper bound")
    replay_surprise_build.add_argument("--limit", type=int, default=100)
    replay_surprise_build.add_argument("--min-abs-error", type=float, help="Include only rows with at least this absolute raw/excess error")
    replay_surprise_build.add_argument("--include-hits", action="store_true", help="Also write notes for large-return-error hits")
    replay_surprise_build.add_argument(
        "--analysis-provider",
        choices=("deterministic", "openai"),
        default="deterministic",
        help="Cause-analysis engine for notes; OpenAI is optional and should be used on selected samples",
    )
    replay_surprise_build.add_argument("--openai-model", help="OpenAI model for surprise adjustment analysis; defaults to AI_MODEL/AI_PRIMARY_MODEL")
    replay_surprise_build.add_argument("--reasoning-effort", default="low", choices=("none", "low", "medium", "high", "xhigh"))
    replay_surprise_build.add_argument("--max-input-chars", type=int, default=18000)
    replay_surprise_report = sub.add_parser(
        "replay-surprise-adjustments-report",
        help="Show stored replay surprise adjustment notes",
    )
    replay_surprise_report.add_argument("--scorer-name", help="Stored scorer_name to filter")
    replay_surprise_report.add_argument("--scorer-version", help="Stored scorer_version to filter")
    replay_surprise_report.add_argument("--symbol", help="Ticker symbol to filter")
    replay_surprise_report.add_argument("--analysis-provider", choices=("deterministic", "openai"), help="Adjustment analysis provider to filter")
    replay_surprise_report.add_argument("--adjustment-version", help="Stored adjustment_version to filter")
    replay_surprise_report.add_argument("--limit", type=int, default=50)
    replay_surprise_report.add_argument("--output", type=Path, help="Optional Markdown report output path")
    replay_surprise_report.add_argument("--json", action="store_true", help="Print machine-readable adjustment notes")
    ytd_calibration = sub.add_parser(
        "ytd-calibration-run",
        help="Run a 2026 YTD data collection, replay backtest, scoring, and adjustment-note pipeline",
    )
    ytd_calibration.add_argument("--start", default="2026-01-01", help="Inclusive YYYY-MM-DD start date")
    ytd_calibration.add_argument("--end", default="2026-06-28", help="Exclusive/current YYYY-MM-DD end date")
    ytd_calibration.add_argument("--symbols", help="Comma-separated symbols; defaults to the M46 YTD universe")
    ytd_calibration.add_argument("--stages", help="Comma-separated stages or 'all' (default)")
    ytd_calibration.add_argument("--sec-limit-per-symbol", type=int, default=5)
    ytd_calibration.add_argument("--financial-limit-per-symbol", type=int, default=250)
    ytd_calibration.add_argument("--macro-limit", type=int, default=5)
    ytd_calibration.add_argument("--fred-limit-per-series", type=int, default=260)
    ytd_calibration.add_argument("--external-limit", type=int, default=50)
    ytd_calibration.add_argument("--ibkr-news-limit-per-symbol", type=int, default=25)
    ytd_calibration.add_argument("--ibkr-news-providers", default="BZ", help="Comma-separated IBKR historical-news providers; default BZ for Benzinga")
    ytd_calibration.add_argument("--technical-limit-per-symbol", type=int, default=260)
    ytd_calibration.add_argument(
        "--replay-event-type",
        default="all",
        choices=("all", "earnings", "guidance", "earnings_guidance"),
        help="Replay event filter; default all uses every replay-eligible historical event",
    )
    ytd_calibration.add_argument("--replay-limit", type=int, default=5000)
    ytd_calibration.add_argument("--outcome-limit", type=int, default=5000)
    ytd_calibration.add_argument("--score-limit", type=int, default=1000)
    ytd_calibration.add_argument("--openai-score-limit", type=int, default=0, help="Optional OpenAI scoring rows; 0 skips API scoring")
    ytd_calibration.add_argument(
        "--adjustment-scorer",
        default="auto",
        choices=("auto", "heuristic", "openai"),
        help="Scorer used for adjustment notes; auto follows OpenAI scoring when enabled, otherwise heuristic",
    )
    ytd_calibration.add_argument("--adjustment-limit", type=int, default=1000)
    ytd_calibration.add_argument("--openai-adjustment-limit", type=int, default=0, help="Optional OpenAI cause-analysis rows; 0 skips API cause analysis")
    ytd_calibration.add_argument("--output-dir", type=Path, default=Path("exports"))
    ytd_calibration.add_argument("--openai-model", help="OpenAI model for optional scoring/cause analysis")
    ytd_calibration.add_argument("--openai-scorer-version", default="openai-replay-score-v6", help="OpenAI scorer version for scoring/adjustments")
    ytd_calibration.add_argument("--reasoning-effort", default="low", choices=("none", "low", "medium", "high", "xhigh"))
    ytd_calibration.add_argument("--max-input-chars", type=int, default=16000)
    ytd_calibration.add_argument("--dry-run", action="store_true", help="Show planned stages without collecting/scoring")

def add_core_data_commands(sub):
    add_core_news_ai_commands(sub)
    add_core_official_macro_commands(sub)
    add_core_external_historical_commands(sub)
    add_core_replay_overlay_commands(sub)
