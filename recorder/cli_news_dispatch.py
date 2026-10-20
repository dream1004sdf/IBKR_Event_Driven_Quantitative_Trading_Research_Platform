from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any


CommandTable = Mapping[str, Callable[..., Any]]


def handle_news_source_command(args: Any, settings: Any, commands: CommandTable) -> bool:
    """Dispatch news/source-analysis commands registered by cli_news_commands.

    The command implementations still live in main.py. The command table keeps
    this module free of main.py imports and avoids circular imports during the
    staged refactor.
    """

    if args.command == "ibkr-news-broadtape-probe":
        commands["command_ibkr_news_broadtape_probe"](
            settings,
            args.providers,
            args.seconds,
            args.save_to_db,
            args.ibkr_client_id,
            args.output,
            args.json_output_path,
            args.json,
        )
        return True
    if args.command == "news-subscription-mesh":
        commands["command_news_subscription_mesh"](
            settings,
            args.profile_json,
            args.lookback_minutes,
            args.max_temp_symbols,
            args.output,
            args.json_output_path,
            args.json,
        )
        return True
    if args.command == "cross-provider-event-roles":
        commands["command_cross_provider_event_roles"](
            settings,
            args.profile_json,
            args.lookback_minutes,
            args.output,
            args.json_output_path,
            args.json,
        )
        return True
    if args.command == "m236-news-learning":
        commands["command_m236_news_learning"](
            args.profile_json,
            args.output,
            args.json_output_path,
            args.provider_profile_output_path,
            args.json,
        )
        return True
    if args.command == "m236-outcome-attach":
        commands["command_m236_outcome_attach"](
            args.profile_json,
            args.price_source,
            args.max_records,
            args.max_symbols,
            args.horizons,
            args.dry_run,
            args.output,
            args.json_output_path,
            args.json,
        )
        return True
    if args.command == "macro-event-calendar":
        commands["command_macro_event_calendar"](
            args.output,
            args.json_output_path,
            args.horizon_days,
            args.no_internet_refresh,
            args.json,
        )
        return True
    if args.command == "macro-release-interpreter":
        commands["command_macro_release_interpreter"](
            args.database_path,
            args.output,
            args.json_output_path,
            args.lookback_minutes,
            args.max_rows,
            args.no_market_reaction,
            args.price_source,
            args.json,
        )
        return True
    if args.command == "unknown-theme-discovery":
        commands["command_unknown_theme_discovery"](
            settings,
            args.database_path,
            args.lookback_minutes,
            args.min_group_events,
            args.limit,
            args.include_medium,
            args.ai_review,
            args.openai_model,
            args.reasoning_effort,
            args.max_ai_groups,
            args.output,
            args.json_output_path,
            args.json,
        )
        return True
    return False


def handle_external_market_command(args: Any, settings: Any, commands: CommandTable) -> bool:
    """Dispatch external-market/geopolitical/regime commands."""

    if args.command == "external-market-sync":
        commands["command_external_market_sync"](
            settings,
            args.source,
            args.limit,
            args.alpha_tickers,
            args.marketaux_symbols,
            args.marketaux_latest,
            args.marketaux_search,
            args.marketaux_no_filter_entities,
            args.alpha_topics,
        )
        return True
    if args.command == "external-market-backfill":
        commands["command_external_market_backfill"](
            settings,
            args.source,
            args.start,
            args.end,
            args.window_days,
            args.limit_per_window,
            args.max_windows,
            args.pause_seconds,
            args.alpha_tickers,
            args.marketaux_symbols,
            args.marketaux_mode,
            args.marketaux_query_name,
            args.marketaux_entity_types,
            args.marketaux_countries,
            not args.marketaux_no_group_similar,
            not args.marketaux_allow_no_entities,
        )
        return True
    if args.command == "external-market-coverage":
        commands["command_external_market_coverage"](settings, args.start, args.end)
        return True
    if args.command == "standing-regime-build":
        commands["command_standing_regime_build"](
            settings,
            args.start,
            args.end,
            args.max_gap_days,
            args.active_gap_days,
            args.easing_gap_days,
            args.limit,
        )
        return True
    if args.command == "standing-regime-report":
        commands["command_standing_regime_report"](
            settings,
            args.cutoff_at,
            args.symbol,
            args.status,
            args.limit,
            args.output,
            args.json,
        )
        return True
    if args.command == "standing-regime-discover-blind":
        commands["command_standing_regime_discover_blind"](
            settings,
            args.start,
            args.end,
            args.include_known,
            args.min_evidence,
            args.min_duration_days,
            args.min_active_months,
            args.max_gap_days,
            args.exclude_themes,
            args.limit,
            args.top_n,
            args.output,
            args.json,
            args.ai_review,
            args.openai_model,
            args.reasoning_effort,
            args.max_ai_candidates,
        )
        return True
    if args.command == "standing-regime-outcomes":
        commands["command_standing_regime_outcomes"](
            settings,
            args.symbols,
            args.horizons_minutes,
            args.max_outcome_lag_minutes,
            args.evaluation_limit,
            not args.no_record_current_snapshot,
            not args.no_evaluate_pending,
            args.output,
            args.json_output_path,
            args.json,
        )
        return True
    if args.command == "geopolitical-intelligence":
        commands["command_geopolitical_intelligence"](
            settings,
            args.lookback_hours,
            args.horizons_minutes,
            args.signal_limit,
            args.max_assets_per_signal,
            args.max_outcome_lag_minutes,
            args.evaluation_limit,
            not args.no_record_current,
            not args.no_evaluate_pending,
            args.output,
            args.json_output_path,
            args.json,
        )
        return True
    if args.command == "benzinga-external-signals-build":
        commands["command_benzinga_external_signals_build"](
            settings,
            args.start,
            args.end,
            args.symbols,
            args.limit,
            args.include_non_geopolitical,
            args.dynamic_total_limit,
            args.dynamic_per_symbol_limit,
            args.dynamic_priority_per_symbol_limit,
            args.dynamic_priority_symbols,
        )
        return True
    if args.command == "dow-jones-external-signals-build":
        commands["command_dow_jones_external_signals_build"](
            settings,
            args.start,
            args.end,
            args.symbols,
            args.limit,
            args.include_non_geopolitical,
            args.dynamic_total_limit,
            args.dynamic_per_symbol_limit,
            args.dynamic_priority_per_symbol_limit,
            args.dynamic_priority_symbols,
        )
        return True
    if args.command == "news-lineage-v2-rebuild":
        commands["command_news_lineage_v2_rebuild"](
            settings,
            args.start,
            args.end,
            args.limit,
            args.bucket_minutes,
            args.output,
            args.json_output_path,
            args.json,
        )
        return True
    if args.command == "reaction-label-foundation-build":
        commands["command_reaction_label_foundation_build"](
            settings,
            args.start,
            args.end,
            args.limit,
            args.windows,
            args.decision_lags_seconds,
            not args.no_core,
            not args.no_event_proxies,
            not args.no_label_stubs,
            args.output,
            args.json_output_path,
            args.json,
        )
        return True
    if args.command == "breaking-news-ingest":
        commands["command_breaking_news_ingest"](
            settings,
            args.headline,
            args.symbols,
            args.published_at,
            args.source_label,
            args.url,
            args.sentiment_score,
            args.confidence,
        )
        return True
    if args.command == "emergency-breaking-backfill":
        commands["command_emergency_breaking_backfill"](
            settings,
            args.start,
            args.end,
            args.source,
            args.limit,
            args.min_urgency,
        )
        return True
    if args.command == "external-market-report":
        commands["command_external_market_report"](settings, args.source, args.region, args.theme, args.limit, args.json)
        return True
    return False
