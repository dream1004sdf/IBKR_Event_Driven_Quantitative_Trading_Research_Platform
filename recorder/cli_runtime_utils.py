from __future__ import annotations

from pathlib import Path
from typing import Any


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _singleton_lock(command_name: str):
    from recorder.singleton_lock import SingleInstanceLock

    lock_name = command_name.strip().replace(" ", "_")
    return SingleInstanceLock(lock_name, _repo_root() / "locks" / f"{lock_name}.lock")


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
            "broker_orders_submitted": False,
            "transmit_enabled": False,
            "live_scorer_mutated": False,
        },
    }


def _run_singleton(command_name: str, runner: Any) -> dict[str, Any]:
    from recorder.singleton_lock import AlreadyRunningError, append_singleton_event

    try:
        with _singleton_lock(command_name):
            return runner()
    except AlreadyRunningError as exc:
        result = _singleton_skip_payload(command_name, exc)
        append_singleton_event(
            _repo_root() / "exports" / "m230_singleton_lock_events.jsonl",
            {
                "event_type": "already_running_blocked",
                "command": command_name,
                "status": result.get("status"),
                "singleton": result.get("singleton") or {},
            },
        )
        return result
