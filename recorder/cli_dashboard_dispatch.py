from __future__ import annotations

from typing import Any

from .cli_auto_dispatch import dispatch_by_signature


DASHBOARD_COMMANDS = {
    "trading-dashboard-snapshot",
    "trading-dashboard-html",
    "dashboard-action-queue",
    "dashboard-recovery-plan",
    "dashboard-recovery-executor",
    "dashboard-recovery-post-validation",
    "dashboard-recovery-closed-loop",
    "dashboard-latency-safety-audit",
    "dashboard-supervisor-handoff-audit",
    "execution-hot-path-latency-audit",
    "trading-dashboard-loop",
}


def handle_dashboard_command(args: Any, settings: Any, commands: dict[str, Any]) -> bool:
    return dispatch_by_signature(
        args,
        settings,
        commands,
        DASHBOARD_COMMANDS,
        aliases={
            "profile_json_path": "profile_json",
            "json_output": "json",
        },
        value_overrides={
            "refresh_after": lambda parsed: not parsed.no_refresh_after,
        },
    )
