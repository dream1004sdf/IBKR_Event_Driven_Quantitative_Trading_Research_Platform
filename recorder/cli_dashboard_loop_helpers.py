from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from .cli_profile_utils import (
    coalesce_profile_path as _coalesce_profile_path,
    coalesce_profile_value as _coalesce_profile_value,
    load_profile_json as _load_profile_json,
    profile_path_value as _profile_path_value,
)


from .cli_dashboard_hook_helpers import (
    _write_markdown_text_atomic,
    _run_watch_evidence_refresh_watchdog,
    _latency_budget_stage_policy,
    _latency_budget_artifact_skip,
    _latency_budget_read_json,
    _run_dashboard_source_ingest_health,
    _run_dashboard_market_data_session_overlay,
    _run_dashboard_market_data_risk_gate,
    _run_dashboard_session_transition_closed_loop,
    _run_dashboard_session_transition_release_governor,
    _run_dashboard_release_governed_recovery_closed_loop,
)

def _console_safe_text(text: str) -> str:
    return text.encode("ascii", errors="replace").decode("ascii")


def _singleton_lock(command_name: str):
    from recorder.singleton_lock import SingleInstanceLock

    lock_name = command_name.strip().replace(" ", "_")
    repo_root = Path(__file__).resolve().parents[1]
    return SingleInstanceLock(lock_name, repo_root / "locks" / f"{lock_name}.lock")


def _singleton_skip_payload(command_name: str, exc: Exception) -> dict[str, Any]:
    payload = getattr(exc, "payload", None)
    if not isinstance(payload, dict):
        payload = {"message": str(exc)}
    return {
        "status": "skipped_already_running",
        "command": command_name,
        "reason": "singleton_lock_active",
        "singleton": payload,
        "safety": {
            "existing_process_left_running": True,
            "new_duplicate_process_started": False,
            "ibkr_order_submitted": False,
            "portfolio_mutated": False,
        },
    }


def _run_singleton(command_name: str, runner: Any) -> dict[str, Any]:
    from recorder.singleton_lock import AlreadyRunningError, append_singleton_event

    try:
        with _singleton_lock(command_name):
            return runner()
    except AlreadyRunningError as exc:
        payload = _singleton_skip_payload(command_name, exc)
        append_singleton_event(Path(__file__).resolve().parents[1] / "logs" / "singleton_events.jsonl", payload)
        return payload
