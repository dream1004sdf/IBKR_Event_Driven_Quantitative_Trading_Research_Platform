from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any


CommandTable = Mapping[str, Callable[..., Any]]


def handle_ibkr_broker_evidence_command(args: Any, settings: Any, commands: CommandTable) -> bool:
    """Dispatch read-only IBKR broker evidence commands."""

    if args.command == "ibkr-account-snapshot":
        commands["command_ibkr_account_snapshot"](
            settings,
            args.account,
            args.tags,
            args.timeout_seconds,
            not args.no_positions,
            not args.no_open_orders,
            args.client_id,
            args.output,
            args.json_output_path,
            args.json,
        )
        return True
    if args.command == "ibkr-order-whatif-preview":
        commands["command_ibkr_order_whatif_preview"](
            settings,
            args.preview_json,
            args.max_orders,
            args.include_human_review,
            args.allow_fractional,
            args.timeout_seconds,
            args.client_id,
            args.output,
            args.json_output_path,
            args.json,
        )
        return True
    if args.command == "ibkr-short-availability":
        commands["command_ibkr_short_availability"](
            settings,
            args.symbols,
            args.timeout_seconds,
            args.market_data_type,
            args.client_id,
            args.output,
            args.json_output_path,
            args.json,
        )
        return True
    if args.command == "ibkr-stock-mark-snapshot":
        commands["command_ibkr_stock_mark_snapshot"](
            settings,
            args.symbols,
            args.router_json,
            args.quote_seconds,
            args.market_data_type,
            args.client_id,
            args.output,
            args.json_output_path,
            args.json,
        )
        return True
    return False


def handle_operating_defensive_safety_command(args: Any, settings: Any, commands: CommandTable) -> bool:
    """Dispatch paper-only operating defensive and safety-cache commands."""

    if args.command == "operating-defensive-action-plan":
        commands["command_operating_defensive_action_plan"](
            args.operating_auto_gate_json,
            args.paper_portfolio_state_json,
            args.account_positions_json,
            args.max_candidates,
            args.max_actions_per_symbol,
            args.hedge_notional_fraction,
            args.output,
            args.json_output_path,
            args.json,
        )
        return True
    if args.command == "operating-defensive-intent-ledger":
        commands["command_operating_defensive_intent_ledger"](
            args.defensive_action_plan_json,
            args.existing_ledger_json,
            args.option_quote_gate_json,
            args.broker_whatif_json,
            args.allow_missing_whatif,
            args.allow_missing_option_quotes,
            args.max_intents,
            args.ledger_run_id,
            args.sample_limit,
            args.output,
            args.json_output_path,
            args.json,
        )
        return True
    if args.command == "operating-defensive-gate-evidence":
        commands["command_operating_defensive_gate_evidence"](
            args.defensive_action_plan_json,
            args.broker_whatif_json,
            args.option_quote_json,
            args.option_chain_json,
            args.account,
            args.max_preview_items,
            args.sample_limit,
            args.order_preview_json_output_path,
            args.output,
            args.json_output_path,
            args.json,
        )
        return True
    if args.command == "operating-defensive-gate-retry":
        commands["command_operating_defensive_gate_retry"](
            settings,
            args.defensive_action_plan_json,
            args.account,
            args.max_preview_items,
            args.existing_whatif_json,
            args.existing_option_quote_json,
            args.run_ibkr_whatif,
            args.whatif_attempts,
            args.include_human_review,
            args.allow_fractional,
            args.whatif_timeout_seconds,
            args.run_option_quotes,
            args.regular_session_only_option_quotes,
            args.option_session_gate_now,
            args.option_symbols,
            args.option_attempts,
            args.option_market_data_types,
            args.option_quote_seconds,
            args.option_chain_timeout_seconds,
            args.min_dte,
            args.max_dte,
            args.strikes_around,
            args.max_expirations,
            args.max_quote_contracts,
            args.max_spread_pct,
            args.sleep_seconds,
            args.output_dir,
            args.output,
            args.json_output_path,
            args.json,
        )
        return True
    if args.command == "operating-option-quote-resume-plan":
        commands["command_operating_option_quote_resume_plan"](
            args.latest_retry_json,
            args.defensive_action_plan_json,
            args.existing_whatif_json,
            args.existing_option_quote_json,
            args.account,
            args.m192_output_dir,
            args.m192_output,
            args.m192_json_output_path,
            args.option_attempts,
            args.option_market_data_types,
            args.option_quote_seconds,
            args.option_chain_timeout_seconds,
            args.max_quote_contracts,
            args.now,
            args.output,
            args.json_output_path,
            args.json,
        )
        return True
    if args.command == "execution-safety-cache-supervisor":
        commands["command_execution_safety_cache_supervisor"](
            args.profile_json,
            args.max_ticks,
            args.interval_seconds,
            args.sleep_between_ticks,
            args.execute_refresh,
            args.timeout_seconds,
            args.tick_log_jsonl_path,
            args.output,
            args.json_output_path,
            args.json,
        )
        return True
    return False
