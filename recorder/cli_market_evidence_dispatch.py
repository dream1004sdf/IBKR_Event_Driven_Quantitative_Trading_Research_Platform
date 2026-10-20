from __future__ import annotations

from typing import Any

from .cli_auto_dispatch import dispatch_by_signature


MARKET_EVIDENCE_COMMANDS = {
    "market-data-desk",
    "evidence-freshness-desk",
    "evidence-refresh-queue",
    "evidence-refresh-watchdog",
    "source-ingest-health",
    "source-live-validation",
    "source-online-supervisor",
    "evidence-preflight",
    "market-data-risk-gate",
    "market-data-session-overlay",
    "market-data-session-transition-gate",
    "market-data-session-transition-closed-loop",
    "session-transition-release-governor",
    "release-governed-recovery-closed-loop",
    "market-data-defensive-mode",
}


def _inverse_optional(value: bool | None) -> bool | None:
    return None if value is None else not value


def _block_on_stale_override(parsed: Any) -> bool | None:
    if hasattr(parsed, "allow_stale_market_data"):
        return _inverse_optional(getattr(parsed, "allow_stale_market_data"))
    return not bool(getattr(parsed, "allow_stale_evidence", False))


def handle_market_evidence_command(args: Any, settings: Any, commands: dict[str, Any]) -> bool:
    return dispatch_by_signature(
        args,
        settings,
        commands,
        MARKET_EVIDENCE_COMMANDS,
        aliases={
            "profile_json_path": "profile_json",
            "market_data_json_path": "market_data_json",
            "json_output": "json",
        },
        value_overrides={
            "block_on_stale": _block_on_stale_override,
            "block_on_offline": lambda parsed: not parsed.allow_offline_evidence,
            "block_on_watchdog_failure": lambda parsed: not parsed.allow_watchdog_failure,
            "allow_lagging_with_haircut": lambda parsed: not parsed.no_lagging_haircut,
            "block_on_delayed": lambda parsed: _inverse_optional(parsed.allow_delayed_market_data),
            "block_on_missing": lambda parsed: _inverse_optional(parsed.allow_missing_market_prices),
            "block_on_warming_up": lambda parsed: _inverse_optional(parsed.allow_warming_up_market_data),
            "block_on_errors": lambda parsed: _inverse_optional(parsed.allow_market_data_errors),
            "block_on_close_only": lambda parsed: _inverse_optional(parsed.allow_close_only_quotes),
            "block_on_severe_wide_spread": lambda parsed: _inverse_optional(parsed.allow_severe_wide_spread),
            "require_operating_preflight": lambda parsed: not parsed.ignore_operating_preflight,
            "require_portfolio_risk_gate": lambda parsed: not parsed.ignore_portfolio_risk_gate,
            "require_session_transition_release_governor": (
                lambda parsed: not parsed.ignore_session_transition_release_governor
            ),
        },
    )
