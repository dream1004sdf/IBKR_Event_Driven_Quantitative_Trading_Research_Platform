from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any


CommandTable = Mapping[str, Callable[..., Any]]


def handle_live_options_command(args: Any, settings: Any, commands: CommandTable) -> bool:
    """Dispatch live/read-only option commands.

    Command implementations remain in main.py for this staged cleanup. Passing a
    command table avoids importing main.py and keeps this refactor free of
    circular imports.
    """

    if args.command == "ibkr-options-chain-scan":
        commands["command_ibkr_options_chain_scan"](
            settings,
            args.symbols,
            args.reference_prices,
            args.underlying_sec_types,
            args.underlying_exchanges,
            args.min_dte,
            args.max_dte,
            args.strikes_around,
            args.max_expirations,
            args.timeout,
            args.client_id,
            args.output,
            args.json_output_path,
            args.json,
        )
        return True
    if args.command == "ibkr-options-quote-snapshot":
        commands["command_ibkr_options_quote_snapshot"](
            settings,
            args.symbols,
            args.reference_prices,
            args.underlying_sec_types,
            args.underlying_exchanges,
            args.min_dte,
            args.max_dte,
            args.strikes_around,
            args.max_expirations,
            args.max_quote_contracts,
            args.market_data_type,
            args.generic_ticks,
            args.chain_timeout,
            args.quote_seconds,
            args.max_spread_pct,
            args.client_id,
            args.output,
            args.json_output_path,
            args.json,
        )
        return True
    if args.command == "options-market-structure-desk":
        commands["command_options_market_structure_desk"](
            args.quote_json,
            args.market_data_json,
            args.pin_near_pct,
            args.proximity_near_pct,
            args.max_snapshot_age_minutes,
            args.no_volume_fallback,
            args.output,
            args.json_output_path,
            args.json,
        )
        return True
    if args.command == "options-market-structure-supervisor":
        commands["command_options_market_structure_supervisor"](
            args.profile_json,
            args.symbols,
            args.max_ticks,
            args.interval_seconds,
            args.sleep_between_ticks,
            args.execute_actions,
            args.max_snapshot_age_minutes,
            args.run_id,
            args.output,
            args.json_output_path,
            args.json,
        )
        return True
    if args.command == "option-structure-outcome-build":
        commands["command_option_structure_outcome_build"](
            settings,
            args.options_market_structure_json,
            args.horizons,
            args.symbols,
            args.flat_threshold,
            args.price_source,
            args.ibkr_client_id,
            args.ibkr_timeout,
            args.ibkr_what_to_show,
            args.ibkr_bar_size,
            args.ibkr_all_sessions,
            args.output,
            args.json_output_path,
            args.json,
        )
        return True
    if args.command == "option-structure-outcome-report":
        commands["command_option_structure_outcome_report"](
            settings,
            args.limit,
            args.symbol,
            args.price_source,
            args.json,
        )
        return True
    if args.command == "single-stock-option-watchlist":
        commands["command_single_stock_option_watchlist"](
            settings,
            args.profile_json,
            args.database_path,
            args.execute_actions,
            args.output,
            args.json_output_path,
            args.json,
        )
        return True
    if args.command == "single-stock-option-watchlist-loop":
        commands["command_single_stock_option_watchlist_loop"](
            settings,
            args.profile_json,
            args.database_path,
            args.execute_actions,
            args.output,
            args.json_output_path,
            args.interval_seconds,
            args.max_ticks,
        )
        return True
    return False
