from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def add_news_source_commands(sub: Any) -> None:
    """Register news/source-analysis CLI commands.

    This module intentionally only owns argparse wiring. Command execution stays
    in main.py so this refactor remains behavior-preserving.
    """

    ibkr_news_broadtape_probe = sub.add_parser(
        "ibkr-news-broadtape-probe",
        help="Read-only IBKR NEWS BroadTape probe and comparison against ticker-specific news rows",
    )
    ibkr_news_broadtape_probe.add_argument(
        "--providers",
        default=None,
        help="Comma-separated BroadTape provider codes, e.g. BZ or AUTO. Unsupported codes are reported separately.",
    )
    ibkr_news_broadtape_probe.add_argument("--seconds", type=float, default=45.0)
    ibkr_news_broadtape_probe.add_argument(
        "--save-to-db",
        action="store_true",
        help="Store observed BroadTape headlines as primary_symbol=BROADTAPE:<provider> for comparison/training.",
    )
    ibkr_news_broadtape_probe.add_argument("--ibkr-client-id", type=int, help="Override IBKR clientId; defaults to IBKR_CLIENT_ID + 2600")
    ibkr_news_broadtape_probe.add_argument("--output", type=Path, default=Path("exports/m233_ibkr_broadtape_probe.md"))
    ibkr_news_broadtape_probe.add_argument("--json-output-path", type=Path, default=Path("exports/m233_ibkr_broadtape_probe.json"))
    ibkr_news_broadtape_probe.add_argument("--json", action="store_true", help="Print machine-readable probe JSON")

    news_subscription_mesh = sub.add_parser(
        "news-subscription-mesh",
        help="Build the M235 core universe plus BroadTape temporary subscription planner artifact",
    )
    news_subscription_mesh.add_argument("--profile-json", type=Path, default=Path("config/news_subscription_mesh_profile_v1.json"))
    news_subscription_mesh.add_argument("--lookback-minutes", type=int)
    news_subscription_mesh.add_argument("--max-temp-symbols", type=int)
    news_subscription_mesh.add_argument("--output", type=Path)
    news_subscription_mesh.add_argument("--json-output-path", type=Path)
    news_subscription_mesh.add_argument("--json", action="store_true")

    cross_provider_event_roles = sub.add_parser(
        "cross-provider-event-roles",
        help="Build M236 provider event-role, agreement/conflict, and scorer evidence packet artifact",
    )
    cross_provider_event_roles.add_argument("--profile-json", type=Path, default=Path("config/cross_provider_event_roles_profile_v1.json"))
    cross_provider_event_roles.add_argument("--lookback-minutes", type=int)
    cross_provider_event_roles.add_argument("--output", type=Path)
    cross_provider_event_roles.add_argument("--json-output-path", type=Path)
    cross_provider_event_roles.add_argument("--json", action="store_true")

    m236_news_learning = sub.add_parser(
        "m236-news-learning",
        help="Build M236 source-quality learning report from pending/evaluated cross-provider outcome records",
    )
    m236_news_learning.add_argument("--profile-json", type=Path, default=Path("config/m236_news_learning_profile_v1.json"))
    m236_news_learning.add_argument("--output", type=Path)
    m236_news_learning.add_argument("--json-output-path", type=Path)
    m236_news_learning.add_argument("--provider-profile-output-path", type=Path)
    m236_news_learning.add_argument("--json", action="store_true")

    m236_outcome_attach = sub.add_parser(
        "m236-outcome-attach",
        help="Attach due 5M/15M/30M market outcomes to M236 source-quality learning records",
    )
    m236_outcome_attach.add_argument("--profile-json", type=Path, default=Path("config/m236_outcome_attachment_profile_v1.json"))
    m236_outcome_attach.add_argument("--price-source", choices=("ibkr", "none"))
    m236_outcome_attach.add_argument("--max-records", type=int)
    m236_outcome_attach.add_argument("--max-symbols", type=int)
    m236_outcome_attach.add_argument("--horizons", help="Comma-separated intraday horizons, e.g. 5M,15M,30M")
    m236_outcome_attach.add_argument("--dry-run", action="store_true")
    m236_outcome_attach.add_argument("--output", type=Path)
    m236_outcome_attach.add_argument("--json-output-path", type=Path)
    m236_outcome_attach.add_argument("--json", action="store_true")

    macro_event_calendar = sub.add_parser(
        "macro-event-calendar",
        help="Build M237 upcoming critical macro/FOMC event calendar for volatility-aware position management",
    )
    macro_event_calendar.add_argument("--output", type=Path, default=Path("exports/m237_macro_event_calendar.md"))
    macro_event_calendar.add_argument("--json-output-path", type=Path, default=Path("exports/m237_macro_event_calendar.json"))
    macro_event_calendar.add_argument("--horizon-days", type=int, default=120)
    macro_event_calendar.add_argument("--no-internet-refresh", action="store_true")
    macro_event_calendar.add_argument("--json", action="store_true")

    macro_release_interpreter = sub.add_parser(
        "macro-release-interpreter",
        help="Parse macro release headlines into surprise scores, asset-specific bias, market reaction, and scorer evidence",
    )
    macro_release_interpreter.add_argument("--database-path", type=Path)
    macro_release_interpreter.add_argument("--output", type=Path, default=Path("exports/m237_macro_release_interpreter.md"))
    macro_release_interpreter.add_argument("--json-output-path", type=Path, default=Path("exports/m237_macro_release_interpreter.json"))
    macro_release_interpreter.add_argument("--lookback-minutes", type=int, default=240)
    macro_release_interpreter.add_argument("--max-rows", type=int, default=500)
    macro_release_interpreter.add_argument("--no-market-reaction", action="store_true")
    macro_release_interpreter.add_argument("--price-source", choices=("ibkr", "none"), default="ibkr")
    macro_release_interpreter.add_argument("--json", action="store_true")

    unknown_theme_discovery = sub.add_parser(
        "unknown-theme-discovery",
        help="Queue low-confidence news asset mappings as unknown theme candidates and optionally ask OpenAI for provisional asset-map review",
    )
    unknown_theme_discovery.add_argument("--database-path", type=Path)
    unknown_theme_discovery.add_argument("--lookback-minutes", type=int, default=240)
    unknown_theme_discovery.add_argument("--min-group-events", type=int, default=1)
    unknown_theme_discovery.add_argument("--limit", type=int, default=500)
    unknown_theme_discovery.add_argument("--include-medium", action="store_true", help="Also queue medium-confidence mappings for AI review")
    unknown_theme_discovery.add_argument("--ai-review", action="store_true", help="Use OpenAI to review grouped unknown-theme candidates")
    unknown_theme_discovery.add_argument("--openai-model", help="OpenAI model for unknown-theme review; defaults to AI_MODEL/AI_PRIMARY_MODEL")
    unknown_theme_discovery.add_argument("--reasoning-effort", default="low", choices=("none", "low", "medium", "high", "xhigh"))
    unknown_theme_discovery.add_argument("--max-ai-groups", type=int, default=20)
    unknown_theme_discovery.add_argument("--output", type=Path, default=Path("exports/m234_unknown_theme_discovery.md"))
    unknown_theme_discovery.add_argument("--json-output-path", type=Path, default=Path("exports/m234_unknown_theme_discovery.json"))
    unknown_theme_discovery.add_argument("--json", action="store_true")


def add_external_market_commands(sub: Any) -> None:
    """Register external market/geopolitical/regime CLI commands."""

    external_market_sync = sub.add_parser(
        "external-market-sync",
        help="Collect Marketaux geopolitical/news sentiment signals; Alpha Vantage is disabled in the active pipeline",
    )
    external_market_sync.add_argument("--source", choices=("all", "marketaux", "alpha_vantage"), default="marketaux")
    external_market_sync.add_argument("--limit", type=int, default=25)
    external_market_sync.add_argument(
        "--alpha-tickers",
        default="SPY,QQQ,TLT,SOXX,TSM,NVDA,XLE,GLD,USO,ITA",
        help="Legacy/disabled Alpha Vantage tickers for NEWS_SENTIMENT",
    )
    external_market_sync.add_argument(
        "--marketaux-symbols",
        help="Comma-separated Marketaux symbols for entity-filtered news (default: GEOPOLITICAL_NEWS_SYMBOLS)",
    )
    external_market_sync.add_argument(
        "--marketaux-latest",
        action="store_true",
        help="Use latest-news Marketaux mode: no symbols, no search filter, and entity filtering disabled",
    )
    external_market_sync.add_argument("--marketaux-search", help="Override Marketaux search expression; ignored by --marketaux-latest")
    external_market_sync.add_argument("--marketaux-no-filter-entities", action="store_true", help="Disable Marketaux entity filtering")
    external_market_sync.add_argument(
        "--alpha-topics",
        help="Legacy/disabled Alpha Vantage NEWS_SENTIMENT topics, e.g. financial_markets,economy_macro,technology",
    )

    external_market_backfill = sub.add_parser(
        "external-market-backfill",
        help="Backfill Marketaux external geopolitical/news signals over date windows; Alpha Vantage returns disabled notice",
    )
    external_market_backfill.add_argument("--source", choices=("all", "marketaux", "alpha_vantage"), default="marketaux")
    external_market_backfill.add_argument("--start", required=True, help="Inclusive YYYY-MM-DD start date")
    external_market_backfill.add_argument("--end", required=True, help="Inclusive YYYY-MM-DD end date")
    external_market_backfill.add_argument("--window-days", type=int, default=14, help="Days per API window")
    external_market_backfill.add_argument("--limit-per-window", type=int, default=25)
    external_market_backfill.add_argument("--max-windows", type=int, help="Optional safety cap on date windows per source")
    external_market_backfill.add_argument("--pause-seconds", type=float, default=0.0, help="Optional pause after each API request")
    external_market_backfill.add_argument(
        "--alpha-tickers",
        default="SPY,QQQ,TLT,SOXX,SMH,TSM,NVDA,XLE,GLD,USO,ITA,EWY,UUP",
        help="Legacy/disabled Alpha Vantage tickers for NEWS_SENTIMENT",
    )
    external_market_backfill.add_argument(
        "--marketaux-symbols",
        help="Comma-separated Marketaux symbols for entity-filtered news (default: GEOPOLITICAL_NEWS_SYMBOLS)",
    )
    external_market_backfill.add_argument(
        "--marketaux-mode",
        choices=("query", "broad", "hybrid"),
        default="query",
        help=(
            "Marketaux backfill mode. query preserves legacy geopolitical searches; "
            "broad uses news/all entity discovery; hybrid runs broad first, then query."
        ),
    )
    external_market_backfill.add_argument(
        "--marketaux-query-name",
        help=(
            "Run only one named Marketaux query shard. "
            "Used by the canonical backfill dynamic worker queue."
        ),
    )
    external_market_backfill.add_argument(
        "--marketaux-entity-types",
        default="equity,index,etf",
        help="Comma-separated Marketaux entity_types for broad/hybrid discovery",
    )
    external_market_backfill.add_argument(
        "--marketaux-countries",
        default="us",
        help="Comma-separated Marketaux countries filter for broad/hybrid discovery",
    )
    external_market_backfill.add_argument(
        "--marketaux-no-group-similar",
        action="store_true",
        help="Disable Marketaux group_similar=true for broad/hybrid discovery",
    )
    external_market_backfill.add_argument(
        "--marketaux-allow-no-entities",
        action="store_true",
        help="Disable must_have_entities=true for broad/hybrid discovery",
    )

    external_market_coverage = sub.add_parser(
        "external-market-coverage",
        help="Show date/source/month coverage for stored external geopolitical/news signals",
    )
    external_market_coverage.add_argument("--start", default="2025-01-01")
    external_market_coverage.add_argument("--end", default=datetime.now(timezone.utc).date().isoformat())

    standing_regime_build = sub.add_parser(
        "standing-regime-build",
        help="Build persistent favorable/risk background regimes from stored external market signals",
    )
    standing_regime_build.add_argument("--start", default="2025-01-01", help="Inclusive YYYY-MM-DD start date")
    standing_regime_build.add_argument("--end", default=datetime.now(timezone.utc).date().isoformat(), help="Inclusive YYYY-MM-DD end date")
    standing_regime_build.add_argument("--max-gap-days", type=int, default=30, help="Start a new episode after this many quiet days")
    standing_regime_build.add_argument("--active-gap-days", type=int, default=14, help="Keep episode active if evidence appeared within this many days")
    standing_regime_build.add_argument("--easing-gap-days", type=int, default=45, help="Mark episode resolved after this many quiet days")
    standing_regime_build.add_argument("--limit", type=int, default=100000, help="Maximum external_market_signals rows to scan")

    standing_regime_report = sub.add_parser(
        "standing-regime-report",
        help="Report persistent favorable/risk background regimes such as AI boom, tariff war, and conflicts",
    )
    standing_regime_report.add_argument("--cutoff-at", help="Optional ISO/YYY-MM-DD cutoff for point-in-time regime lookup")
    standing_regime_report.add_argument("--symbol", help="Optional impacted asset/symbol filter")
    standing_regime_report.add_argument("--status", choices=("active", "easing", "resolved"), help="Optional lifecycle status filter")
    standing_regime_report.add_argument("--limit", type=int, default=50)
    standing_regime_report.add_argument("--output", type=Path, help="Optional Markdown report path")
    standing_regime_report.add_argument("--json", action="store_true", help="Print machine-readable JSON")

    standing_regime_blind = sub.add_parser(
        "standing-regime-discover-blind",
        help="Report-only blind discovery of long-running regime candidates not covered by predefined rules",
    )
    standing_regime_blind.add_argument("--start", default="2025-01-01", help="Inclusive YYYY-MM-DD start date")
    standing_regime_blind.add_argument("--end", default=datetime.now(timezone.utc).date().isoformat(), help="Inclusive YYYY-MM-DD end date")
    standing_regime_blind.add_argument("--include-known", action="store_true", help="Include signals already matched by predefined standing-regime rules")
    standing_regime_blind.add_argument("--min-evidence", type=int, default=12)
    standing_regime_blind.add_argument("--min-duration-days", type=int, default=45)
    standing_regime_blind.add_argument("--min-active-months", type=int, default=3)
    standing_regime_blind.add_argument("--max-gap-days", type=int, default=45)
    standing_regime_blind.add_argument("--exclude-themes", help="Comma-separated external_market_signals themes to exclude as routine noise, e.g. energy_supply")
    standing_regime_blind.add_argument("--limit", type=int, default=100000)
    standing_regime_blind.add_argument("--top-n", type=int, default=40)
    standing_regime_blind.add_argument("--output", type=Path, help="Optional Markdown report path")
    standing_regime_blind.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    standing_regime_blind.add_argument("--ai-review", action="store_true", help="Use OpenAI to classify blind candidates as promotable/routine/known/noise")
    standing_regime_blind.add_argument("--openai-model", help="OpenAI model for blind candidate review; defaults to AI_MODEL/AI_PRIMARY_MODEL")
    standing_regime_blind.add_argument("--reasoning-effort", default="low", choices=("none", "low", "medium", "high", "xhigh"))
    standing_regime_blind.add_argument("--max-ai-candidates", type=int, default=20, help="Top candidates to send to OpenAI review")

    standing_regime_outcomes = sub.add_parser(
        "standing-regime-outcomes",
        help="Record/evaluate report-only standing-regime outcome logs for later threshold learning",
    )
    standing_regime_outcomes.add_argument(
        "--symbols",
        default="SPY,QQQ,IWM,DIA,SMH,SOXX,USO,XLE,GLD,VIX",
        help="Comma-separated symbols to observe for active/easing standing-regime impact",
    )
    standing_regime_outcomes.add_argument("--horizons-minutes", default="5,15,30,390")
    standing_regime_outcomes.add_argument("--max-outcome-lag-minutes", type=float, default=1440.0)
    standing_regime_outcomes.add_argument("--evaluation-limit", type=int, default=2000)
    standing_regime_outcomes.add_argument("--no-record-current-snapshot", action="store_true")
    standing_regime_outcomes.add_argument("--no-evaluate-pending", action="store_true")
    standing_regime_outcomes.add_argument("--output", type=Path, default=Path("exports/standing_regime_outcomes.md"))
    standing_regime_outcomes.add_argument("--json-output-path", type=Path, default=Path("exports/standing_regime_outcomes.json"))
    standing_regime_outcomes.add_argument("--json", action="store_true", help="Print machine-readable JSON")

    geopolitical_intelligence = sub.add_parser(
        "geopolitical-intelligence",
        help="Build report-only geopolitical outcome logs, adaptive asset map, and Geo Desk heatmap",
    )
    geopolitical_intelligence.add_argument("--lookback-hours", type=float, default=168.0)
    geopolitical_intelligence.add_argument("--horizons-minutes", default="5,15,30,390")
    geopolitical_intelligence.add_argument("--signal-limit", type=int, default=2000)
    geopolitical_intelligence.add_argument("--max-assets-per-signal", type=int, default=6)
    geopolitical_intelligence.add_argument("--max-outcome-lag-minutes", type=float, default=1440.0)
    geopolitical_intelligence.add_argument("--evaluation-limit", type=int, default=5000)
    geopolitical_intelligence.add_argument("--no-record-current", action="store_true")
    geopolitical_intelligence.add_argument("--no-evaluate-pending", action="store_true")
    geopolitical_intelligence.add_argument("--output", type=Path, default=Path("exports/geopolitical_intelligence.md"))
    geopolitical_intelligence.add_argument("--json-output-path", type=Path, default=Path("exports/geopolitical_intelligence.json"))
    geopolitical_intelligence.add_argument("--json", action="store_true", help="Print machine-readable JSON")

    benzinga_external_signals = sub.add_parser(
        "benzinga-external-signals-build",
        help="Convert stored IBKR/Benzinga news_articles rows into external_market_signals overlay rows",
    )
    benzinga_external_signals.add_argument("--start", required=True, help="Inclusive YYYY-MM-DD start date")
    benzinga_external_signals.add_argument("--end", required=True, help="Inclusive YYYY-MM-DD end date")
    benzinga_external_signals.add_argument("--symbols", help="Comma-separated BZ primary symbols to include")
    benzinga_external_signals.add_argument("--limit", type=int, default=5000)
    benzinga_external_signals.add_argument("--include-non-geopolitical", action="store_true")
    benzinga_external_signals.add_argument("--dynamic-total-limit", type=int, default=0)
    benzinga_external_signals.add_argument("--dynamic-per-symbol-limit", type=int, default=0)
    benzinga_external_signals.add_argument("--dynamic-priority-per-symbol-limit", type=int, default=0)
    benzinga_external_signals.add_argument("--dynamic-priority-symbols")

    dow_jones_external_signals = sub.add_parser(
        "dow-jones-external-signals-build",
        help="Convert stored IBKR/Dow Jones news_articles rows into external_market_signals overlay rows",
    )
    dow_jones_external_signals.add_argument("--start", required=True, help="Inclusive YYYY-MM-DD start date")
    dow_jones_external_signals.add_argument("--end", required=True, help="Inclusive YYYY-MM-DD end date")
    dow_jones_external_signals.add_argument("--symbols", help="Comma-separated DJ primary symbols to include")
    dow_jones_external_signals.add_argument("--limit", type=int, default=5000)
    dow_jones_external_signals.add_argument("--include-non-geopolitical", action="store_true")
    dow_jones_external_signals.add_argument("--dynamic-total-limit", type=int, default=0)
    dow_jones_external_signals.add_argument("--dynamic-per-symbol-limit", type=int, default=0)
    dow_jones_external_signals.add_argument("--dynamic-priority-per-symbol-limit", type=int, default=0)
    dow_jones_external_signals.add_argument("--dynamic-priority-symbols")

    lineage_rebuild = sub.add_parser(
        "news-lineage-v2-rebuild",
        help="M253: derive raw->mapping->cluster lineage from stored news without network fetches",
    )
    lineage_rebuild.add_argument("--start", help="Inclusive timestamp/date lower bound")
    lineage_rebuild.add_argument("--end", help="Inclusive timestamp/date upper bound")
    lineage_rebuild.add_argument("--limit", type=int, help="Optional max legacy/raw rows to process")
    lineage_rebuild.add_argument("--bucket-minutes", type=int, default=60)
    lineage_rebuild.add_argument("--output", type=Path, default=Path("exports/m253_news_lineage_v2_rebuild.md"))
    lineage_rebuild.add_argument("--json-output-path", type=Path, default=Path("exports/m253_news_lineage_v2_rebuild.json"))
    lineage_rebuild.add_argument("--json", action="store_true", help="Print machine-readable JSON")

    reaction_foundation = sub.add_parser(
        "reaction-label-foundation-build",
        help="M253: build event-cluster reaction_universe_plan/assets and plan-gated reaction label stubs",
    )
    reaction_foundation.add_argument("--start", help="Inclusive timestamp/date lower bound")
    reaction_foundation.add_argument("--end", help="Inclusive timestamp/date upper bound")
    reaction_foundation.add_argument("--limit", type=int, help="Optional max event clusters to process")
    reaction_foundation.add_argument("--windows", default="5m,15m,1h,close")
    reaction_foundation.add_argument("--decision-lags-seconds", default="0,30,60,180")
    reaction_foundation.add_argument("--no-core", action="store_true", help="Do not add always-on core context assets")
    reaction_foundation.add_argument("--no-event-proxies", action="store_true", help="Do not add event/theme proxy assets")
    reaction_foundation.add_argument("--no-label-stubs", action="store_true", help="Create plans/assets only")
    reaction_foundation.add_argument("--output", type=Path, default=Path("exports/m253_reaction_label_foundation.md"))
    reaction_foundation.add_argument("--json-output-path", type=Path, default=Path("exports/m253_reaction_label_foundation.json"))
    reaction_foundation.add_argument("--json", action="store_true", help="Print machine-readable JSON")

    breaking_news_ingest = sub.add_parser(
        "breaking-news-ingest",
        help="Store an urgent one-line headline as an immediate scorer/overlay emergency signal",
    )
    breaking_news_ingest.add_argument("--headline", required=True, help="One-line breaking headline")
    breaking_news_ingest.add_argument("--symbols", help="Comma-separated impacted tickers/assets, e.g. SPY,QQQ,VIX")
    breaking_news_ingest.add_argument("--published-at", help="ISO timestamp; defaults to now UTC")
    breaking_news_ingest.add_argument("--source-label", default="manual", help="Original source label, e.g. ibkr_bz_live")
    breaking_news_ingest.add_argument("--url", default="")
    breaking_news_ingest.add_argument("--sentiment-score", type=float)
    breaking_news_ingest.add_argument("--confidence", type=float)

    emergency_breaking_backfill = sub.add_parser(
        "emergency-breaking-backfill",
        help="Backfill emergency_breaking_news signals from stored historical headlines/signals",
    )
    emergency_breaking_backfill.add_argument("--start", required=True, help="Inclusive YYYY-MM-DD start date")
    emergency_breaking_backfill.add_argument("--end", required=True, help="Inclusive YYYY-MM-DD end date")
    emergency_breaking_backfill.add_argument(
        "--source",
        choices=("all", "news_articles", "external_market_signals"),
        default="all",
        help="Stored source table(s) to scan",
    )
    emergency_breaking_backfill.add_argument("--limit", type=int, default=100000)
    emergency_breaking_backfill.add_argument(
        "--min-urgency",
        choices=("low", "medium", "high", "critical"),
        default="high",
        help="Minimum urgency to store as emergency backdata",
    )

    external_market_report = sub.add_parser("external-market-report", help="Show stored external geopolitical/news sentiment signals")
    external_market_report.add_argument("--source", choices=("marketaux", "alpha_vantage", "benzinga", "dow_jones", "emergency_breaking_news"))
    external_market_report.add_argument("--region")
    external_market_report.add_argument("--theme")
    external_market_report.add_argument("--limit", type=int, default=30)
    external_market_report.add_argument("--json", action="store_true", help="Print machine-readable JSON")
