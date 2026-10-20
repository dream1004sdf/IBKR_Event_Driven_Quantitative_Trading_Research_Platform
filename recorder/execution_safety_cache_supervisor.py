from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable


EXECUTION_SAFETY_CACHE_SUPERVISOR_VERSION = "execution-safety-cache-supervisor-v1"
AUTOMATED_BROKER_REFRESH_ENV = "IBKR_ALLOW_AUTOMATED_BROKER_REFRESH"
AUTOMATED_BROKER_REFRESH_ACK = "I_UNDERSTAND"

CommandRunner = Callable[[list[str], Path, float], dict[str, Any]]


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _read_json(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        return {}
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> Path:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return resolved


def _append_jsonl(path: Path | None, payload: dict[str, Any]) -> None:
    if path is None:
        return
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    with resolved.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")


def _resolve_path(value: Any, base: Path | None) -> Path | None:
    if value in (None, ""):
        return None
    path = Path(str(value))
    if not path.is_absolute() and base is not None:
        path = base / path
    return path.expanduser().resolve()


def _artifact_age_seconds(path: Path | None) -> float | None:
    if path is None or not path.exists():
        return None
    return max(0.0, time.time() - path.stat().st_mtime)


def _tail(value: Any, limit: int = 3000) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[-limit:]


def _default_command_runner(command: list[str], cwd: Path, timeout_seconds: float) -> dict[str, Any]:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            [str(item) for item in command],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=float(timeout_seconds),
        )
        return {
            "status": "succeeded" if completed.returncode == 0 else "failed",
            "returncode": completed.returncode,
            "stdout_tail": _tail(completed.stdout),
            "stderr_tail": _tail(completed.stderr),
            "duration_seconds": round(time.monotonic() - started, 3),
            "error": None,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "status": "timeout",
            "returncode": None,
            "stdout_tail": _tail(exc.stdout),
            "stderr_tail": _tail(exc.stderr),
            "duration_seconds": round(time.monotonic() - started, 3),
            "error": f"timeout_after_{timeout_seconds}_seconds",
        }
    except Exception as exc:  # pragma: no cover - defensive subprocess wrapper
        return {
            "status": "failed",
            "returncode": None,
            "stdout_tail": "",
            "stderr_tail": "",
            "duration_seconds": round(time.monotonic() - started, 3),
            "error": f"{exc.__class__.__name__}: {str(exc)[:500]}",
        }


def _normalise_command(command: list[Any]) -> list[str]:
    normalised = []
    for item in command:
        text = str(item)
        normalised.append(sys.executable if text == "{python}" else text)
    return normalised


def _command_name(command: list[str]) -> str:
    if len(command) >= 3 and Path(command[1]).name.lower() == "main.py":
        return command[2]
    if len(command) >= 2 and Path(command[0]).name.lower().startswith("python") and Path(command[1]).name.lower() == "main.py":
        return command[2] if len(command) > 2 else ""
    return command[0] if command else ""


def _safety_payloads(payload: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    safety = payload.get("safety")
    if isinstance(safety, dict):
        items.append(safety)
    result = payload.get("result")
    if isinstance(result, dict) and isinstance(result.get("safety"), dict):
        items.append(result["safety"])
    summary = payload.get("summary")
    if isinstance(summary, dict) and isinstance(summary.get("safety"), dict):
        items.append(summary["safety"])
    return items


def _safety_findings(cache_key: str, payload: dict[str, Any]) -> dict[str, Any]:
    observed: dict[str, Any] = {}
    violations: list[str] = []
    for safety in _safety_payloads(payload):
        for key in (
            "ibkr_order_submitted",
            "live_order_transmitted",
            "live_order_routing_enabled",
            "portfolio_mutated",
            "scorer_rows_mutated",
            "account_or_portfolio_mutated",
        ):
            if key in safety:
                observed[key] = safety.get(key)
                if bool(safety.get(key)):
                    violations.append(f"{key}_true")
        if "transmit_enabled" in safety:
            observed["transmit_enabled"] = safety.get("transmit_enabled")
            if bool(safety.get("transmit_enabled")):
                violations.append("transmit_enabled_true")
        if cache_key == "broker_margin_whatif_cache":
            api_called = bool(safety.get("ibkr_order_api_called"))
            if api_called:
                observed["ibkr_order_api_called"] = True
                what_if_only = safety.get("what_if_only") is True
                observed["what_if_only"] = what_if_only
                if not what_if_only:
                    violations.append("broker_api_called_without_what_if_only")
                if bool(safety.get("live_order_transmitted")):
                    violations.append("whatif_live_order_transmitted_true")
    return {
        "observed": observed,
        "violations": sorted(set(violations)),
    }


def _status_summary(cache_key: str, payload: dict[str, Any]) -> dict[str, Any]:
    result = payload.get("result") if isinstance(payload.get("result"), dict) else payload
    if cache_key == "broker_margin_whatif_cache":
        return {
            "whatif_preview_version": result.get("whatif_preview_version"),
            "requested_count": result.get("requested_count"),
            "response_count": result.get("response_count"),
            "read_only_mode_detected": result.get("read_only_mode_detected"),
            "error_count": len(result.get("errors") or []),
        }
    if cache_key == "short_availability_cache":
        rows = result.get("rows") or []
        return {
            "short_availability_version": result.get("short_availability_version"),
            "symbols_requested": len(result.get("symbols_requested") or []),
            "row_count": result.get("row_count"),
            "short_available_count": sum(1 for row in rows if isinstance(row, dict) and bool(row.get("short_available"))),
            "error_count": len(result.get("errors") or []),
        }
    if cache_key == "option_quote_liquidity_cache":
        summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
        return {
            "version": result.get("operating_defensive_gate_retry_version") or result.get("option_quote_resume_plan_version"),
            "best_option_quoted_count": summary.get("best_option_quoted_count"),
            "option_quote_deferred": summary.get("option_quote_deferred"),
            "option_quote_session": summary.get("option_quote_session"),
            "strict_ledger_pending_count": summary.get("strict_ledger_pending_count"),
            "strict_ledger_blocked_count": summary.get("strict_ledger_blocked_count"),
        }
    return {
        "generated_at": result.get("generated_at"),
        "status": result.get("status") or result.get("final_status"),
    }


def _availability_block_reasons(cache_key: str, status_summary: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    if cache_key == "option_quote_liquidity_cache":
        try:
            quoted_count = int(status_summary.get("best_option_quoted_count") or 0)
        except (TypeError, ValueError):
            quoted_count = 0
        if bool(status_summary.get("option_quote_deferred")):
            reasons.append("option_quote_deferred_until_regular_session")
        if quoted_count <= 0:
            reasons.append("no_fresh_option_bid_ask_quotes")
    return sorted(set(reasons))


def _cache_report(
    *,
    cache_key: str,
    cache_policy: dict[str, Any],
    profile_base: Path | None,
    allowed_refresh_commands: set[str],
    execute_refresh: bool,
    cwd: Path,
    runner: CommandRunner,
    timeout_seconds: float,
) -> dict[str, Any]:
    primary = _resolve_path(cache_policy.get("json_path"), profile_base)
    fallback = _resolve_path(cache_policy.get("fallback_json_path"), profile_base)
    selected = primary if primary and primary.exists() else fallback
    exists = bool(selected and selected.exists())
    payload = _read_json(selected)
    age = _artifact_age_seconds(selected)
    max_age = float(cache_policy.get("max_age_seconds", 300.0) or 300.0)
    warning_age = float(cache_policy.get("warning_age_seconds", max_age) or max_age)
    fresh = bool(exists and age is not None and age <= max_age)
    warning_stale = bool(exists and age is not None and age > warning_age)
    safety = _safety_findings(cache_key, payload)
    status_summary = _status_summary(cache_key, payload)
    block_paths = list(cache_policy.get("block_order_paths_when_stale") or [])
    required = bool(cache_policy.get("required", False))
    cache_state = "fresh" if fresh else ("missing" if not exists else "stale")
    availability_block_reasons = _availability_block_reasons(cache_key, status_summary)
    gate_action = (
        "allow_dependent_order_paths"
        if fresh and not safety.get("violations") and not availability_block_reasons
        else "block_dependent_order_paths"
    )
    refresh_needed = bool(cache_state != "fresh" or warning_stale)
    command = _normalise_command(list(cache_policy.get("refresh_command") or []))
    command_name = _command_name(command)
    command_allowed = bool(command and command_name in allowed_refresh_commands and bool(cache_policy.get("allow_execute_refresh", False)))
    action: dict[str, Any] = {
        "action_id": f"refresh_{cache_key}",
        "status": "not_needed" if not refresh_needed else "planned",
        "command": command,
        "command_name": command_name,
        "command_allowed": command_allowed,
        "execute_requested": bool(execute_refresh),
        "executed": False,
        "returncode": None,
        "duration_seconds": None,
        "error": None,
        "stdout_tail": "",
        "stderr_tail": "",
    }
    if refresh_needed and execute_refresh:
        if not command_allowed:
            action["status"] = "blocked"
            action["error"] = "refresh_command_not_allowlisted_or_cache_disallows_execution"
        else:
            result = runner(command, cwd, timeout_seconds)
            action.update(result)
            action["executed"] = True
            if action.get("status") == "succeeded":
                selected = primary if primary and primary.exists() else fallback
                exists = bool(selected and selected.exists())
                payload = _read_json(selected)
                age = _artifact_age_seconds(selected)
                fresh = bool(exists and age is not None and age <= max_age)
                warning_stale = bool(exists and age is not None and age > warning_age)
                safety = _safety_findings(cache_key, payload)
                status_summary = _status_summary(cache_key, payload)
                availability_block_reasons = _availability_block_reasons(cache_key, status_summary)
                cache_state = "fresh" if fresh else ("missing" if not exists else "stale")
                gate_action = "allow_dependent_order_paths" if fresh and not safety.get("violations") else "block_dependent_order_paths"
                gate_action = (
                    "allow_dependent_order_paths"
                    if fresh and not safety.get("violations") and not availability_block_reasons
                    else "block_dependent_order_paths"
                )
    violations = list(safety.get("violations") or [])
    warnings: list[str] = []
    if required and not exists:
        violations.append("required_cache_missing")
    elif required and not fresh:
        violations.append("required_cache_stale")
    if not required and not exists:
        warnings.append("optional_cache_missing")
    elif not required and not fresh:
        warnings.append("optional_cache_stale_blocks_dependent_paths")
    elif warning_stale:
        warnings.append("cache_refresh_recommended")
    for reason in availability_block_reasons:
        warnings.append(reason)
    return {
        "cache_key": cache_key,
        "role": cache_policy.get("role"),
        "primary_path": str(primary) if primary else None,
        "fallback_path": str(fallback) if fallback else None,
        "selected_path": str(selected) if selected else None,
        "exists": exists,
        "cache_state": cache_state,
        "fresh": fresh,
        "required": required,
        "age_seconds": round(age, 3) if age is not None else None,
        "max_age_seconds": max_age,
        "warning_age_seconds": warning_age,
        "warning_stale": warning_stale,
        "regular_session_dependent": bool(cache_policy.get("regular_session_dependent", False)),
        "gate_action": gate_action,
        "block_order_paths_when_stale": block_paths if gate_action != "allow_dependent_order_paths" else [],
        "status_summary": status_summary,
        "availability_block_reasons": sorted(set(availability_block_reasons)),
        "safety": safety,
        "warnings": sorted(set(warnings)),
        "violations": sorted(set(violations)),
        "refresh_action": action,
    }


def build_execution_safety_cache_supervisor(
    *,
    profile_payload: dict[str, Any],
    profile_base: Path | None = None,
    execute_refresh: bool = False,
    cwd: Path | None = None,
    runner: CommandRunner | None = None,
    timeout_seconds: float = 180.0,
) -> dict[str, Any]:
    supervisor = profile_payload.get("safety_cache_supervisor") if isinstance(profile_payload.get("safety_cache_supervisor"), dict) else {}
    caches = supervisor.get("caches") if isinstance(supervisor.get("caches"), dict) else {}
    supervisor_enabled = bool(supervisor.get("enabled", False))
    environment_interlock_enabled = os.getenv(AUTOMATED_BROKER_REFRESH_ENV) == AUTOMATED_BROKER_REFRESH_ACK
    refresh_interlock_enabled = bool(supervisor_enabled and environment_interlock_enabled)
    effective_execute_refresh = bool(execute_refresh and refresh_interlock_enabled)
    if not supervisor_enabled:
        disabled_reason = "profile_disabled_fail_closed"
    elif not environment_interlock_enabled:
        disabled_reason = "environment_interlock_locked"
    else:
        disabled_reason = None
    allowed = {str(item) for item in supervisor.get("allowed_refresh_commands") or []}
    cwd = (cwd or Path.cwd()).expanduser().resolve()
    runner = runner or _default_command_runner
    cache_reports = [
        _cache_report(
            cache_key=key,
            cache_policy=value,
            profile_base=profile_base,
            allowed_refresh_commands=allowed,
            execute_refresh=effective_execute_refresh,
            cwd=cwd,
            runner=runner,
            timeout_seconds=timeout_seconds,
        )
        for key, value in caches.items()
        if isinstance(value, dict)
    ]
    if execute_refresh and not refresh_interlock_enabled:
        for cache in cache_reports:
            action = cache.get("refresh_action") or {}
            action["execute_requested"] = True
            if action.get("status") == "planned":
                action["status"] = "blocked"
                action["error"] = disabled_reason
    violations: list[str] = []
    warnings: list[str] = []
    blocked_paths: set[str] = set()
    for cache in cache_reports:
        for violation in cache.get("violations") or []:
            violations.append(f"{cache.get('cache_key')}:{violation}")
        for warning in cache.get("warnings") or []:
            warnings.append(f"{cache.get('cache_key')}:{warning}")
        for path in cache.get("block_order_paths_when_stale") or []:
            blocked_paths.add(str(path))
        action = cache.get("refresh_action") or {}
        if action.get("status") in {"failed", "timeout", "blocked"}:
            warnings.append(f"{cache.get('cache_key')}:refresh_{action.get('status')}")
    if not refresh_interlock_enabled:
        warnings.append(disabled_reason or "automated_broker_refresh_disabled")
        for cache_policy in caches.values():
            if isinstance(cache_policy, dict):
                for path in cache_policy.get("block_order_paths_when_stale") or []:
                    blocked_paths.add(str(path))
    any_stale_or_missing = any(cache.get("cache_state") != "fresh" for cache in cache_reports)
    any_safety_violation = bool(violations)
    if any_safety_violation:
        status = "red"
    elif any_stale_or_missing or blocked_paths:
        status = "yellow"
    else:
        status = "green"
    action_counts = Counter(str((cache.get("refresh_action") or {}).get("status") or "unknown") for cache in cache_reports)
    cache_state_counts = Counter(str(cache.get("cache_state") or "unknown") for cache in cache_reports)
    return {
        "execution_safety_cache_supervisor_version": EXECUTION_SAFETY_CACHE_SUPERVISOR_VERSION,
        "generated_at": _iso_now(),
        "status": status,
        "enabled": supervisor_enabled,
        "environment_interlock_enabled": environment_interlock_enabled,
        "refresh_interlock_enabled": refresh_interlock_enabled,
        "execute_refresh_requested": bool(execute_refresh),
        "execute_refresh_effective": effective_execute_refresh,
        "disabled_reason": disabled_reason,
        "cache_ready_for_all_dependent_order_paths": not bool(blocked_paths) and not any_safety_violation,
        "blocked_order_paths": sorted(blocked_paths),
        "cache_state_counts": dict(cache_state_counts),
        "refresh_action_status_counts": dict(action_counts),
        "violations": sorted(set(violations)),
        "warnings": sorted(set(warnings)),
        "cache_reports": cache_reports,
        "safety_invariants": {
            "supervisor_submits_orders": False,
            "live_order_transmitted": False,
            "live_order_routing_enabled": False,
            "portfolio_mutated": False,
            "scorer_rows_mutated": False,
            "stale_cache_blocks_dependent_order_paths": True,
            "broker_whatif_requires_what_if_only": True,
            "automated_broker_refresh_enabled": effective_execute_refresh,
        },
    }


def build_execution_safety_cache_supervisor_from_paths(
    *,
    profile_json_path: Path,
    execute_refresh: bool = False,
    cwd: Path | None = None,
    runner: CommandRunner | None = None,
    timeout_seconds: float = 180.0,
) -> dict[str, Any]:
    profile_json_path = profile_json_path.expanduser().resolve()
    return build_execution_safety_cache_supervisor(
        profile_payload=_read_json(profile_json_path),
        profile_base=profile_json_path.parent,
        execute_refresh=execute_refresh,
        cwd=cwd,
        runner=runner,
        timeout_seconds=timeout_seconds,
    )


def run_execution_safety_cache_supervisor(
    *,
    profile_json_path: Path,
    max_ticks: int = 1,
    interval_seconds: float = 1.0,
    sleep_between_ticks: bool = False,
    execute_refresh: bool = False,
    tick_log_jsonl_path: Path | None = None,
    cwd: Path | None = None,
    runner: CommandRunner | None = None,
    timeout_seconds: float = 180.0,
) -> dict[str, Any]:
    ticks: list[dict[str, Any]] = []
    requested_max_ticks = int(max_ticks)
    tick_index = 0
    while requested_max_ticks == 0 or tick_index < max(1, requested_max_ticks):
        tick_index += 1
        started = time.monotonic()
        report = build_execution_safety_cache_supervisor_from_paths(
            profile_json_path=profile_json_path,
            execute_refresh=execute_refresh,
            cwd=cwd,
            runner=runner,
            timeout_seconds=timeout_seconds,
        )
        tick = {
            "tick_index": tick_index,
            "generated_at": report.get("generated_at"),
            "status": report.get("status"),
            "cache_ready_for_all_dependent_order_paths": report.get("cache_ready_for_all_dependent_order_paths"),
            "blocked_order_paths": report.get("blocked_order_paths") or [],
            "cache_state_counts": report.get("cache_state_counts") or {},
            "refresh_action_status_counts": report.get("refresh_action_status_counts") or {},
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "report": report,
        }
        ticks.append(tick)
        _append_jsonl(tick_log_jsonl_path, tick)
        should_continue = requested_max_ticks == 0 or tick_index < max(1, requested_max_ticks)
        if should_continue and sleep_between_ticks and interval_seconds > 0:
            time.sleep(float(interval_seconds))
    final_report = ticks[-1]["report"] if ticks else {}
    return {
        "execution_safety_cache_supervisor_loop_version": "execution-safety-cache-supervisor-loop-v1",
        "started_at": ticks[0]["generated_at"] if ticks else _iso_now(),
        "completed_at": _iso_now(),
        "executed_tick_count": len(ticks),
        "max_ticks": requested_max_ticks,
        "interval_seconds": float(interval_seconds),
        "sleep_between_ticks": bool(sleep_between_ticks),
        "execute_refresh_requested": bool(execute_refresh),
        "final_status": final_report.get("status"),
        "final_cache_ready_for_all_dependent_order_paths": final_report.get("cache_ready_for_all_dependent_order_paths"),
        "final_blocked_order_paths": final_report.get("blocked_order_paths") or [],
        "final_cache_state_counts": final_report.get("cache_state_counts") or {},
        "final_refresh_action_status_counts": final_report.get("refresh_action_status_counts") or {},
        "final_report": final_report,
        "tick_summaries": [
            {key: value for key, value in tick.items() if key != "report"}
            for tick in ticks
        ],
        "safety": {
            "read_only_supervisor": True,
            "ibkr_order_submitted": False,
            "live_order_transmitted": False,
            "live_order_routing_enabled": False,
            "portfolio_mutated": False,
            "scorer_rows_mutated": False,
        },
    }


def execution_safety_cache_supervisor_markdown(report: dict[str, Any]) -> str:
    final = report.get("final_report") if isinstance(report.get("final_report"), dict) else report
    lines = [
        "# Execution Safety Cache Supervisor",
        "",
        f"- Version: `{final.get('execution_safety_cache_supervisor_version') or report.get('execution_safety_cache_supervisor_loop_version')}`",
        f"- Status: `{final.get('status') or report.get('final_status')}`",
        f"- Cache ready for all dependent order paths: `{final.get('cache_ready_for_all_dependent_order_paths')}`",
        f"- Blocked order paths: `{final.get('blocked_order_paths') or []}`",
        f"- Cache state counts: `{final.get('cache_state_counts') or {}}`",
        f"- Refresh action counts: `{final.get('refresh_action_status_counts') or {}}`",
        f"- Violations: `{final.get('violations') or []}`",
        f"- Warnings: `{final.get('warnings') or []}`",
        "",
        "## Cache reports",
        "",
        "| Cache | State | Fresh | Age sec | Gate action | Blocked paths | Refresh | Status summary |",
        "| --- | --- | --- | ---: | --- | --- | --- | --- |",
    ]
    for cache in final.get("cache_reports") or []:
        action = cache.get("refresh_action") or {}
        lines.append(
            f"| `{cache.get('cache_key')}` | `{cache.get('cache_state')}` | `{cache.get('fresh')}` | `{cache.get('age_seconds')}` | `{cache.get('gate_action')}` | `{cache.get('block_order_paths_when_stale') or []}` | `{action.get('status')}` | `{cache.get('status_summary') or {}}` |"
        )
    lines.extend(["", "## Safety invariants", ""])
    for key, value in (final.get("safety_invariants") or report.get("safety") or {}).items():
        lines.append(f"- `{key}`: `{value}`")
    return "\n".join(lines) + "\n"


def write_execution_safety_cache_supervisor_json(output_path: Path, report: dict[str, Any]) -> Path:
    return _write_json(output_path, report)


def write_execution_safety_cache_supervisor_markdown(output_path: Path, report: dict[str, Any]) -> Path:
    resolved = output_path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(execution_safety_cache_supervisor_markdown(report), encoding="utf-8")
    return resolved
