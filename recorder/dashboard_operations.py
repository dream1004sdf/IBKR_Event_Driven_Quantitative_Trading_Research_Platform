"""Dashboard operational recovery planning, execution, and validation.

Middle-level module for dashboard recovery workflows. Broker order routing remains
out of scope; these utilities are recovery/reporting helpers only.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

CommandRunner = Callable[[list[str], Path, float], dict[str, Any]]


# --- merged from dashboard_recovery_plan.py ---
DASHBOARD_RECOVERY_PLAN_VERSION = "dashboard-recovery-plan-v1"


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _severity_rank(value: Any) -> int:
    return {"green": 0, "watch": 1, "yellow": 2, "red": 3}.get(str(value or "").lower(), 0)


def _phase_for_action(action: dict[str, Any]) -> str:
    domain = str(action.get("domain") or "")
    target = str(action.get("auto_recovery_target") or "")
    if domain == "session_transition_release_governor":
        if target == "session_transition_closed_loop":
            return "release_transition_rebuild"
        if target == "operating_auto_gate":
            return "operating_gate_rebuild"
        if target == "market_data_risk_gate":
            return "market_data_recovery"
        if target == "evidence_refresh_watchdog":
            return "evidence_recovery"
        if target == "risk_review":
            return "risk_review"
        return "release_governor_review"
    if domain == "paper_execution_path":
        return "release_governed_ledger_rebuild"
    if domain in {"source_ingest_health", "source_online_supervisor"} or target == "source_online_supervisor":
        return "source_recovery"
    if domain in {"market_data", "market_data_risk_gate"} or target == "market_data_risk_gate":
        return "market_data_recovery"
    if domain in {"evidence_freshness", "evidence_auto_recovery"} or target == "evidence_refresh_watchdog":
        return "evidence_recovery"
    if domain in {"mark_quality"} or target == "mark_refresh":
        return "mark_quality_recovery"
    if domain in {"portfolio_risk"} or target == "risk_review":
        return "risk_review"
    return "dashboard_review"


def _cooldown_seconds_for_phase(phase: str) -> float:
    return {
        "source_recovery": 60.0,
        "market_data_recovery": 1.0,
        "evidence_recovery": 300.0,
        "release_transition_rebuild": 1.0,
        "release_governed_ledger_rebuild": 1.0,
        "operating_gate_rebuild": 1.0,
        "release_governor_review": 1.0,
        "mark_quality_recovery": 30.0,
        "risk_review": 300.0,
        "dashboard_review": 60.0,
    }.get(phase, 60.0)


def _command_preview_for_action(action: dict[str, Any], phase: str) -> list[str]:
    domain = str(action.get("domain") or "")
    action_id = str(action.get("action_id") or "")
    if phase == "source_recovery":
        return [
            "python main.py source-online-supervisor --profile-json config/source_online_supervisor_profile_v1.json --max-ticks 1 --interval-seconds 0",
            "python main.py source-ingest-health --profile-json config/source_ingest_health_profile_v1.json",
        ]
    if phase == "market_data_recovery":
        return [
            "python main.py market-data-desk --seconds 25 --refresh-seconds 1 --market-data-type 1 --gap-threshold-pct 1.0 --tick-move-threshold-pct 0.5 --fallback-after-seconds 4.0 --output exports/m153_market_data_desk_refresh.md --json-output-path exports/m153_market_data_desk_refresh.json",
            "python main.py market-data-risk-gate --profile-json config/market_data_risk_gate_profile_v1.json",
        ]
    if phase == "evidence_recovery":
        return [
            "python main.py evidence-refresh-watchdog --profile-json config/evidence_refresh_watchdog_profile_v1.json --max-ticks 1 --interval-seconds 0",
            "python main.py evidence-freshness-desk --profile-json config/evidence_freshness_desk_profile_v1.json",
        ]
    if phase == "release_transition_rebuild":
        return [
            "python main.py market-data-session-transition-closed-loop --market-data-json exports/m153_market_data_desk_refresh.json --market-data-session-overlay-json exports/m194_market_data_session_overlay.json --preview-json exports/m112_m111_order_preview.json --short-availability-json exports/m113_ibkr_short_availability.json --evidence-preflight-json exports/m156_evidence_preflight_latest.json --output-dir exports",
            "python main.py session-transition-release-governor --transition-gate-json exports/m199_market_data_session_transition_gate_final.json --market-data-risk-gate-json exports/m199_market_data_risk_gate.json --market-data-operating-auto-gate-json exports/m199_market_data_operating_auto_gate.json --portfolio-risk-state-json exports/m123_paper_portfolio_risk_state.json --evidence-preflight-json exports/m156_evidence_preflight_latest.json",
        ]
    if phase == "operating_gate_rebuild":
        return [
            "python main.py replay-score-auto-execution-gate --preview-json exports/m112_m111_order_preview.json --short-availability-json exports/m113_ibkr_short_availability.json --evidence-preflight-json exports/m156_evidence_preflight_latest.json --market-data-risk-gate-json exports/m175_dashboard_market_data_risk_gate_latest.json --allow-fractional-shares --allow-short-stock --max-auto-orders 10 --min-cash-buffer 50",
            "python main.py market-data-operating-auto-gate --auto-gate-json exports/m175_market_data_risk_auto_gate_latest.json --defensive-mode-json exports/m181_market_data_defensive_mode_latest.json",
            "python main.py session-transition-release-governor",
        ]
    if phase == "release_governor_review":
        return [
            "python main.py session-transition-release-governor --transition-gate-json exports/m199_market_data_session_transition_gate_final.json --market-data-risk-gate-json exports/m199_market_data_risk_gate.json --market-data-operating-auto-gate-json exports/m199_market_data_operating_auto_gate.json --portfolio-risk-state-json exports/m123_paper_portfolio_risk_state.json --evidence-preflight-json exports/m156_evidence_preflight_latest.json",
            "python main.py trading-dashboard-snapshot --profile-json config/trading_dashboard_snapshot_profile_v1.json",
        ]
    if phase == "release_governed_ledger_rebuild":
        return [
            "python main.py replay-score-execution-intent-ledger --auto-gate-json exports/m182_market_data_operating_auto_gate_latest.json --portfolio-risk-state-json exports/m123_paper_portfolio_risk_state.json --session-transition-release-governor-json exports/m201_session_transition_release_governor.json --output exports/m204_session_transition_release_governed_execution_intent_ledger.md --json-output-path exports/m204_session_transition_release_governed_execution_intent_ledger.json",
            "python main.py replay-score-paper-order-router --intent-ledger-json exports/m204_session_transition_release_governed_execution_intent_ledger.json --operating-auto-gate-json exports/m182_market_data_operating_auto_gate_latest.json --session-transition-release-governor-json exports/m201_session_transition_release_governor.json --require-portfolio-risk-gate --output exports/m204_session_transition_release_governed_paper_order_router.md --json-output-path exports/m204_session_transition_release_governed_paper_order_router.json",
        ]
    if phase == "mark_quality_recovery":
        return [
            "python main.py paper-cycle-watch-daemon --profile-json config/paper_cycle_watch_preflight_profile_v1.json --max-ticks 1",
        ]
    if phase == "risk_review":
        return [
            "python main.py ibkr-account-snapshot --json-output-path exports/m110_ibkr_account_snapshot.json",
            "python main.py paper-portfolio-risk-state --json-output-path exports/m123_paper_portfolio_risk_state.json",
        ]
    if domain == "dashboard" or action_id.startswith("review_"):
        return [
            "python main.py trading-dashboard-loop --profile-json config/trading_dashboard_snapshot_profile_v1.json --max-ticks 1 --interval-seconds 0 --disable-telegram",
        ]
    return [str(action.get("command_hint") or "manual dashboard review")]


def _preconditions_for_action(action: dict[str, Any], phase: str) -> list[str]:
    preconditions = [
        "dry_run_only",
        "read_only_paths_only",
        "do_not_submit_broker_orders",
        "do_not_enable_transmit",
    ]
    if phase in {"source_recovery", "market_data_recovery"}:
        preconditions.append("ibkr_read_only_session_available_if_needed")
    if phase == "evidence_recovery":
        preconditions.append("source_recovery_not_currently_red")
    if phase == "risk_review":
        preconditions.append("account_snapshot_refresh_is_read_only")
    if phase in {"release_transition_rebuild", "operating_gate_rebuild", "release_governor_review", "release_governed_ledger_rebuild"}:
        preconditions.append("paper_only_release_governor")
        preconditions.append("ordinary_alpha_remains_closed_until_governor_allows")
    return preconditions


def _step_status(action: dict[str, Any], phase: str) -> str:
    if phase == "risk_review":
        return "manual_review_required"
    if action.get("can_auto_execute") is True:
        return "blocked_by_m177_dry_run_policy"
    return "planned_dry_run"


def _step_for_action(action: dict[str, Any], index: int) -> dict[str, Any]:
    phase = _phase_for_action(action)
    severity = str(action.get("severity") or "green")
    priority_score = int(action.get("priority_score") or 0)
    return {
        "step_id": f"m177_step_{index:03d}_{str(action.get('action_id') or 'action').replace(':', '_')}",
        "source_action_id": action.get("action_id"),
        "phase": phase,
        "sequence": index,
        "priority_score": priority_score,
        "severity": severity,
        "severity_rank": _severity_rank(severity),
        "domain": action.get("domain"),
        "source_key": action.get("source_key"),
        "title": action.get("title"),
        "reason": action.get("reason"),
        "recommendation": action.get("recommendation"),
        "status": _step_status(action, phase),
        "dry_run_only": True,
        "would_execute": False,
        "command_preview": _command_preview_for_action(action, phase),
        "preconditions": _preconditions_for_action(action, phase),
        "cooldown_seconds": _cooldown_seconds_for_phase(phase),
        "post_validation": [
            "rebuild_dashboard_snapshot",
            "rebuild_dashboard_action_queue",
            "compare_status_color_and_reason_delta",
        ],
        "safety": {
            "read_only": True,
            "can_auto_execute": False,
            "broker_order_submitted": False,
            "transmit_enabled": False,
            "live_order_routing_enabled": False,
        },
        "details": action.get("details") or {},
    }


def _sort_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    phase_order = {
        "source_recovery": 0,
        "market_data_recovery": 1,
        "release_transition_rebuild": 2,
        "operating_gate_rebuild": 3,
        "release_governed_ledger_rebuild": 4,
        "evidence_recovery": 5,
        "mark_quality_recovery": 6,
        "risk_review": 7,
        "release_governor_review": 8,
        "dashboard_review": 9,
    }
    ordered = sorted(
        steps,
        key=lambda item: (
            phase_order.get(str(item.get("phase") or ""), 9),
            -int(item.get("severity_rank") or 0),
            -int(item.get("priority_score") or 0),
            str(item.get("step_id") or ""),
        ),
    )
    for index, step in enumerate(ordered, start=1):
        step["execution_order"] = index
    return ordered


def build_dashboard_recovery_plan(
    action_queue: dict[str, Any],
    *,
    max_steps: int = 10,
    dry_run: bool = True,
    now: str | None = None,
) -> dict[str, Any]:
    actions = [item for item in action_queue.get("actions") or [] if isinstance(item, dict)]
    steps = [_step_for_action(action, index) for index, action in enumerate(actions, start=1)]
    steps = _sort_steps(steps)[: max(0, int(max_steps))]
    phase_counts = Counter(str(step.get("phase") or "unknown") for step in steps)
    status_counts = Counter(str(step.get("status") or "unknown") for step in steps)
    severity_counts = Counter(str(step.get("severity") or "unknown") for step in steps)
    highest = (
        sorted(
            steps,
            key=lambda item: (
                -int(item.get("severity_rank") or 0),
                -int(item.get("priority_score") or 0),
                int(item.get("execution_order") or 9999),
            ),
        )[0]
        if steps
        else {}
    )
    return {
        "dashboard_recovery_plan_version": DASHBOARD_RECOVERY_PLAN_VERSION,
        "generated_at": now or _iso_now(),
        "dry_run": bool(dry_run),
        "source_action_queue_version": action_queue.get("dashboard_action_queue_version"),
        "source_action_queue_generated_at": action_queue.get("generated_at"),
        "source_status_color": action_queue.get("status_color"),
        "source_status_reasons": action_queue.get("status_reasons") or [],
        "source_total_action_count": action_queue.get("total_action_count") or 0,
        "planned_step_count": len(steps),
        "max_steps": int(max_steps),
        "highest_priority_step_id": highest.get("step_id"),
        "highest_severity": highest.get("severity") if highest else "green",
        "phase_counts": dict(phase_counts),
        "status_counts": dict(status_counts),
        "severity_counts": dict(severity_counts),
        "steps": steps,
        "execution_policy": {
            "mode": "dry_run_plan_only",
            "execute_subprocesses": False,
            "execute_refresh_jobs": False,
            "submit_broker_orders": False,
            "transmit_enabled": False,
            "mutate_scorer_rows": False,
            "mutate_real_portfolio_state": False,
        },
        "safety": {
            "read_only_plan": True,
            "dry_run_only": True,
            "can_auto_execute": False,
            "broker_order_submitted": False,
            "transmit_enabled": False,
            "live_order_routing_enabled": False,
        },
    }


def dashboard_recovery_plan_summary(plan: dict[str, Any] | None) -> dict[str, Any]:
    data = plan or {}
    return {
        "version": data.get("dashboard_recovery_plan_version"),
        "generated_at": data.get("generated_at"),
        "dry_run": data.get("dry_run", True),
        "planned_step_count": data.get("planned_step_count") or 0,
        "highest_severity": data.get("highest_severity") or "green",
        "highest_priority_step_id": data.get("highest_priority_step_id"),
        "phase_counts": data.get("phase_counts") or {},
        "status_counts": data.get("status_counts") or {},
        "top_steps": (data.get("steps") or [])[:5],
    }


def dashboard_recovery_plan_markdown(plan: dict[str, Any]) -> str:
    lines = [
        "# Dashboard Recovery Plan",
        "",
        f"- Version: `{plan.get('dashboard_recovery_plan_version')}`",
        f"- Generated at: `{plan.get('generated_at')}`",
        f"- Mode: `dry_run_plan_only`",
        f"- Source status: `{plan.get('source_status_color')}` reasons `{plan.get('source_status_reasons') or []}`",
        f"- Planned steps: `{plan.get('planned_step_count')}` / max `{plan.get('max_steps')}`",
        f"- Highest severity: `{plan.get('highest_severity')}` step `{plan.get('highest_priority_step_id') or '-'}`",
        "- Safety: read-only dry-run plan; no subprocess execution, broker order submission, or transmit.",
        "",
        "| Order | Priority | Severity | Phase | Status | Source action | Command preview |",
        "| ---: | ---: | --- | --- | --- | --- | --- |",
    ]
    for step in plan.get("steps") or []:
        commands = "<br>".join(str(item).replace("|", "/") for item in step.get("command_preview") or [])
        lines.append(
            f"| {step.get('execution_order')} | {step.get('priority_score')} | {step.get('severity')} | {step.get('phase')} | "
            f"{step.get('status')} | `{step.get('source_action_id')}` | {commands or '-'} |"
        )
    if not plan.get("steps"):
        lines.append("| - | - | green | - | no_action_required | `none` | - |")
    return "\n".join(lines) + "\n"


def write_dashboard_recovery_plan_json(output_path: Path, plan: dict[str, Any]) -> Path:
    resolved = output_path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")
    return resolved


def write_dashboard_recovery_plan_markdown(output_path: Path, plan: dict[str, Any]) -> Path:
    resolved = output_path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(dashboard_recovery_plan_markdown(plan), encoding="utf-8")
    return resolved


# --- merged from dashboard_recovery_executor.py ---
DASHBOARD_RECOVERY_EXECUTOR_VERSION = "dashboard-recovery-executor-v1"

CommandRunner = Callable[[list[str], Path, float], dict[str, Any]]

DEFAULT_ALLOWED_PHASES = {"source_recovery", "market_data_recovery", "evidence_recovery"}
DENY_TOKENS = {
    "order",
    "orders",
    "transmit",
    "live-order",
    "route-paper-orders",
    "paper-order-routing",
    "place-order",
    "submit-order",
}


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _tail(value: Any, limit: int = 3000) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[-limit:]


def _split_command(command: str) -> list[str]:
    # M177 command previews are intentionally simple, space-delimited internal commands.
    # Keep parsing conservative instead of accepting shell syntax.
    return [item for item in str(command or "").strip().split() if item]


def _canonical_command(command: str) -> str:
    return " ".join(_split_command(command))


def _allowed_command_catalog() -> dict[str, set[str]]:
    return {
        "source_recovery": {
            "python main.py source-online-supervisor --profile-json config/source_online_supervisor_profile_v1.json --max-ticks 1 --interval-seconds 0",
            "python main.py source-ingest-health --profile-json config/source_ingest_health_profile_v1.json",
        },
        "market_data_recovery": {
            "python main.py market-data-desk --seconds 25 --refresh-seconds 1 --market-data-type 1 --gap-threshold-pct 1.0 --tick-move-threshold-pct 0.5 --fallback-after-seconds 4.0 --output exports/m153_market_data_desk_refresh.md --json-output-path exports/m153_market_data_desk_refresh.json",
            "python main.py market-data-risk-gate --profile-json config/market_data_risk_gate_profile_v1.json",
        },
        "evidence_recovery": {
            "python main.py evidence-refresh-watchdog --profile-json config/evidence_refresh_watchdog_profile_v1.json --max-ticks 1 --interval-seconds 0",
            "python main.py evidence-freshness-desk --profile-json config/evidence_freshness_desk_profile_v1.json",
        },
    }


def _has_denied_token(tokens: list[str]) -> str | None:
    for token in tokens:
        normalized = token.strip().lower()
        if normalized in DENY_TOKENS:
            return normalized
    return None


def _tokens_for_subprocess(tokens: list[str]) -> list[str]:
    if tokens and tokens[0].lower() in {"python", "python.exe"}:
        return [sys.executable] + tokens[1:]
    return tokens


def _default_runner(command: list[str], cwd: Path, timeout_seconds: float) -> dict[str, Any]:
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
    except Exception as exc:  # pragma: no cover - defensive execution audit
        return {
            "status": "failed",
            "returncode": None,
            "stdout_tail": "",
            "stderr_tail": "",
            "duration_seconds": round(time.monotonic() - started, 3),
            "error": f"{exc.__class__.__name__}: {str(exc)[:500]}",
        }


def _command_decision(command: str, phase: str, allowed_phases: set[str]) -> dict[str, Any]:
    canonical = _canonical_command(command)
    tokens = _split_command(canonical)
    denied = _has_denied_token(tokens)
    if denied:
        return {
            "allowed": False,
            "reason": "denied_token",
            "denied_token": denied,
            "canonical_command": canonical,
            "tokens": tokens,
        }
    if phase not in allowed_phases:
        return {
            "allowed": False,
            "reason": "phase_not_allowlisted",
            "canonical_command": canonical,
            "tokens": tokens,
        }
    allowed_for_phase = _allowed_command_catalog().get(phase, set())
    if canonical not in allowed_for_phase:
        return {
            "allowed": False,
            "reason": "command_not_allowlisted",
            "canonical_command": canonical,
            "tokens": tokens,
        }
    return {
        "allowed": True,
        "reason": "allowlisted_read_only_recovery_command",
        "canonical_command": canonical,
        "tokens": tokens,
    }


def run_dashboard_recovery_executor(
    recovery_plan: dict[str, Any],
    *,
    execute: bool = False,
    max_steps: int = 3,
    allowed_phases: set[str] | None = None,
    timeout_seconds: float = 120.0,
    cwd: Path | None = None,
    runner: CommandRunner | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    allowed_phases = set(allowed_phases or DEFAULT_ALLOWED_PHASES)
    cwd = (cwd or Path.cwd()).expanduser().resolve()
    runner = runner or _default_runner
    steps = [item for item in recovery_plan.get("steps") or [] if isinstance(item, dict)]
    selected_steps = steps[: max(0, int(max_steps))]
    step_results: list[dict[str, Any]] = []
    command_results: list[dict[str, Any]] = []
    seen_allowed_commands: set[str] = set()
    for step in selected_steps:
        phase = str(step.get("phase") or "")
        step_result = {
            "step_id": step.get("step_id"),
            "source_action_id": step.get("source_action_id"),
            "phase": phase,
            "severity": step.get("severity"),
            "status": "planned",
            "execute_requested": bool(execute),
            "allowed_phase": phase in allowed_phases,
            "command_count": 0,
            "executed_command_count": 0,
            "skipped_command_count": 0,
            "commands": [],
        }
        if step.get("status") == "manual_review_required":
            step_result["status"] = "skipped_manual_review_required"
        for command in step.get("command_preview") or []:
            decision = _command_decision(str(command), phase, allowed_phases)
            command_result = {
                "step_id": step.get("step_id"),
                "source_action_id": step.get("source_action_id"),
                "phase": phase,
                "command": str(command),
                "canonical_command": decision.get("canonical_command"),
                "allowed": bool(decision.get("allowed")),
                "allow_reason": decision.get("reason"),
                "execute_requested": bool(execute),
                "status": "planned",
                "returncode": None,
                "duration_seconds": None,
                "stdout_tail": "",
                "stderr_tail": "",
                "error": None,
            }
            if not decision.get("allowed"):
                command_result["status"] = f"skipped_{decision.get('reason')}"
                if decision.get("denied_token"):
                    command_result["denied_token"] = decision.get("denied_token")
            elif str(decision.get("canonical_command") or "") in seen_allowed_commands:
                command_result["status"] = "skipped_duplicate_command"
            elif not execute:
                command_result["status"] = "skipped_dry_run"
            elif step.get("status") == "manual_review_required":
                command_result["status"] = "skipped_manual_review_required"
            else:
                result = runner(_tokens_for_subprocess(decision.get("tokens") or []), cwd, float(timeout_seconds))
                command_result.update(result)
            if decision.get("allowed") and command_result["status"] != "skipped_duplicate_command":
                seen_allowed_commands.add(str(decision.get("canonical_command") or ""))
            step_result["commands"].append(command_result)
            command_results.append(command_result)
        step_result["command_count"] = len(step_result["commands"])
        step_result["executed_command_count"] = sum(1 for item in step_result["commands"] if item.get("status") in {"succeeded", "failed", "timeout"})
        step_result["skipped_command_count"] = step_result["command_count"] - step_result["executed_command_count"]
        statuses = {str(item.get("status") or "") for item in step_result["commands"]}
        if step_result["status"] == "skipped_manual_review_required":
            pass
        elif any(status == "failed" for status in statuses):
            step_result["status"] = "failed"
        elif any(status == "timeout" for status in statuses):
            step_result["status"] = "timeout"
        elif statuses and statuses <= {"succeeded"}:
            step_result["status"] = "succeeded"
        elif statuses and statuses <= {"skipped_dry_run"}:
            step_result["status"] = "skipped_dry_run"
        elif statuses and all(status.startswith("skipped_") for status in statuses):
            step_result["status"] = "skipped_not_executable"
        else:
            step_result["status"] = "mixed"
        step_results.append(step_result)
    status_counts = Counter(str(item.get("status") or "unknown") for item in step_results)
    command_status_counts = Counter(str(item.get("status") or "unknown") for item in command_results)
    executed_count = sum(1 for item in command_results if item.get("status") in {"succeeded", "failed", "timeout"})
    failure_count = sum(1 for item in command_results if item.get("status") in {"failed", "timeout"})
    final_status = "failed" if failure_count else ("executed" if executed_count else "dry_run")
    if not execute:
        final_status = "dry_run"
    return {
        "dashboard_recovery_executor_version": DASHBOARD_RECOVERY_EXECUTOR_VERSION,
        "generated_at": now or _iso_now(),
        "source_recovery_plan_version": recovery_plan.get("dashboard_recovery_plan_version"),
        "source_recovery_plan_generated_at": recovery_plan.get("generated_at"),
        "execute_requested": bool(execute),
        "final_status": final_status,
        "allowed_phases": sorted(allowed_phases),
        "max_steps": int(max_steps),
        "selected_step_count": len(selected_steps),
        "executed_command_count": executed_count,
        "failure_count": failure_count,
        "status_counts": dict(status_counts),
        "command_status_counts": dict(command_status_counts),
        "step_results": step_results,
        "command_results": command_results,
        "execution_policy": {
            "allowlist_only": True,
            "execute_subprocesses": bool(execute),
            "submit_broker_orders": False,
            "transmit_enabled": False,
            "mutate_scorer_rows": False,
            "mutate_real_portfolio_state": False,
            "deny_tokens": sorted(DENY_TOKENS),
        },
        "safety": {
            "read_only_recovery_executor": True,
            "allowlist_enforced": True,
            "broker_order_submitted": False,
            "transmit_enabled": False,
            "live_order_routing_enabled": False,
        },
    }


def dashboard_recovery_executor_summary(report: dict[str, Any] | None) -> dict[str, Any]:
    data = report or {}
    return {
        "version": data.get("dashboard_recovery_executor_version"),
        "generated_at": data.get("generated_at"),
        "execute_requested": bool(data.get("execute_requested")),
        "final_status": data.get("final_status"),
        "selected_step_count": data.get("selected_step_count") or 0,
        "executed_command_count": data.get("executed_command_count") or 0,
        "failure_count": data.get("failure_count") or 0,
        "status_counts": data.get("status_counts") or {},
        "command_status_counts": data.get("command_status_counts") or {},
        "top_step_results": (data.get("step_results") or [])[:5],
    }


def dashboard_recovery_executor_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Dashboard Recovery Executor",
        "",
        f"- Version: `{report.get('dashboard_recovery_executor_version')}`",
        f"- Generated at: `{report.get('generated_at')}`",
        f"- Execute requested: `{report.get('execute_requested')}`",
        f"- Final status: `{report.get('final_status')}`",
        f"- Selected steps: `{report.get('selected_step_count')}` / max `{report.get('max_steps')}`",
        f"- Executed commands: `{report.get('executed_command_count')}` failures `{report.get('failure_count')}`",
        f"- Allowed phases: `{report.get('allowed_phases') or []}`",
        "- Safety: allowlist-only read-only recovery executor; no broker order submission or transmit.",
        "",
        "| Step | Phase | Status | Commands |",
        "| --- | --- | --- | ---: |",
    ]
    for item in report.get("step_results") or []:
        lines.append(
            f"| `{item.get('step_id')}` | {item.get('phase')} | {item.get('status')} | {item.get('command_count')} |"
        )
    if not report.get("step_results"):
        lines.append("| `none` | - | no_steps_selected | 0 |")
    lines.extend(["", "## Commands", "", "| Status | Allowed | Phase | Command |", "| --- | --- | --- | --- |"])
    for item in report.get("command_results") or []:
        lines.append(
            f"| {item.get('status')} | {item.get('allowed')} | {item.get('phase')} | `{str(item.get('canonical_command') or item.get('command') or '').replace('|', '/')}` |"
        )
    if not report.get("command_results"):
        lines.append("| - | - | - | - |")
    return "\n".join(lines) + "\n"


def write_dashboard_recovery_executor_json(output_path: Path, report: dict[str, Any]) -> Path:
    resolved = output_path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return resolved


def write_dashboard_recovery_executor_markdown(output_path: Path, report: dict[str, Any]) -> Path:
    resolved = output_path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(dashboard_recovery_executor_markdown(report), encoding="utf-8")
    return resolved


# --- merged from dashboard_recovery_post_validation.py ---
DASHBOARD_RECOVERY_POST_VALIDATION_VERSION = "dashboard-recovery-post-validation-v1"

_RANK = {"green": 0, "watch": 1, "yellow": 2, "red": 3}


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _rank(value: Any) -> int:
    return _RANK.get(str(value or "").lower(), 0)


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


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


def _snapshot_summary(payload: dict[str, Any]) -> dict[str, Any]:
    status = payload.get("status") or {}
    action_queue = payload.get("action_queue") or {}
    recovery_plan = payload.get("recovery_plan") or {}
    recovery_executor = payload.get("recovery_executor") or {}
    return {
        "version": payload.get("dashboard_snapshot_version"),
        "generated_at": payload.get("generated_at"),
        "status_color": status.get("color"),
        "status_rank": _rank(status.get("color")),
        "reason_count": len(status.get("reasons") or []),
        "reasons": status.get("reasons") or [],
        "action_queue_total": action_queue.get("total_action_count") or 0,
        "recovery_plan_steps": recovery_plan.get("planned_step_count") or 0,
        "recovery_executor_status": recovery_executor.get("final_status"),
        "recovery_executor_failures": recovery_executor.get("failure_count") or 0,
    }


def _queue_summary(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": payload.get("dashboard_action_queue_version"),
        "generated_at": payload.get("generated_at"),
        "status_color": payload.get("status_color"),
        "status_rank": _rank(payload.get("status_color")),
        "visible_action_count": payload.get("visible_action_count") or 0,
        "total_action_count": payload.get("total_action_count") or 0,
        "highest_severity": payload.get("highest_severity"),
        "highest_severity_rank": _rank(payload.get("highest_severity")),
        "highest_priority_score": payload.get("highest_priority_score") or 0,
        "top_action_ids": [item.get("action_id") for item in payload.get("actions") or []][:5],
    }


def _plan_summary(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": payload.get("dashboard_recovery_plan_version"),
        "generated_at": payload.get("generated_at"),
        "planned_step_count": payload.get("planned_step_count") or 0,
        "highest_severity": payload.get("highest_severity"),
        "highest_severity_rank": _rank(payload.get("highest_severity")),
        "highest_priority_step_id": payload.get("highest_priority_step_id"),
        "phase_counts": payload.get("phase_counts") or {},
    }


def _executor_summary(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": payload.get("dashboard_recovery_executor_version"),
        "generated_at": payload.get("generated_at"),
        "execute_requested": bool(payload.get("execute_requested")),
        "final_status": payload.get("final_status"),
        "selected_step_count": payload.get("selected_step_count") or 0,
        "executed_command_count": payload.get("executed_command_count") or 0,
        "failure_count": payload.get("failure_count") or 0,
        "command_status_counts": payload.get("command_status_counts") or {},
    }


def _delta(before: dict[str, Any], after: dict[str, Any], key: str) -> float:
    return _num(after.get(key)) - _num(before.get(key))


def _metric_delta(name: str, before: dict[str, Any], after: dict[str, Any], key: str, *, lower_is_better: bool = True) -> dict[str, Any]:
    before_value = before.get(key)
    after_value = after.get(key)
    delta = _delta(before, after, key)
    if delta == 0:
        direction = "unchanged"
    elif (delta < 0 and lower_is_better) or (delta > 0 and not lower_is_better):
        direction = "improved"
    else:
        direction = "worsened"
    return {
        "metric": name,
        "before": before_value,
        "after": after_value,
        "delta": delta,
        "direction": direction,
        "lower_is_better": lower_is_better,
    }


def _outcome(deltas: list[dict[str, Any]], *, executor_after: dict[str, Any]) -> str:
    if not deltas:
        return "insufficient_baseline"
    if _num(executor_after.get("failure_count")) > 0:
        return "worsened"
    improved = sum(1 for item in deltas if item.get("direction") == "improved")
    worsened = sum(1 for item in deltas if item.get("direction") == "worsened")
    if worsened > improved:
        return "worsened"
    if improved > 0 and worsened == 0:
        return "improved"
    if improved > worsened:
        return "mixed_improved"
    if worsened > 0:
        return "mixed_worsened"
    return "unchanged"


def build_dashboard_recovery_post_validation(
    *,
    before_snapshot: dict[str, Any] | None = None,
    after_snapshot: dict[str, Any] | None = None,
    before_action_queue: dict[str, Any] | None = None,
    after_action_queue: dict[str, Any] | None = None,
    before_recovery_plan: dict[str, Any] | None = None,
    after_recovery_plan: dict[str, Any] | None = None,
    recovery_executor: dict[str, Any] | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    before_snapshot_summary = _snapshot_summary(before_snapshot or {})
    after_snapshot_summary = _snapshot_summary(after_snapshot or {})
    before_queue_summary = _queue_summary(before_action_queue or {})
    after_queue_summary = _queue_summary(after_action_queue or {})
    before_plan_summary = _plan_summary(before_recovery_plan or {})
    after_plan_summary = _plan_summary(after_recovery_plan or {})
    executor_summary = _executor_summary(recovery_executor or {})

    deltas: list[dict[str, Any]] = []
    if before_snapshot_summary.get("version") or after_snapshot_summary.get("version"):
        deltas.extend(
            [
                _metric_delta("snapshot_status_rank", before_snapshot_summary, after_snapshot_summary, "status_rank"),
                _metric_delta("snapshot_reason_count", before_snapshot_summary, after_snapshot_summary, "reason_count"),
            ]
        )
    if before_queue_summary.get("version") or after_queue_summary.get("version"):
        deltas.extend(
            [
                _metric_delta("action_queue_total_action_count", before_queue_summary, after_queue_summary, "total_action_count"),
                _metric_delta("action_queue_highest_severity_rank", before_queue_summary, after_queue_summary, "highest_severity_rank"),
                _metric_delta("action_queue_highest_priority_score", before_queue_summary, after_queue_summary, "highest_priority_score"),
            ]
        )
    if before_plan_summary.get("version") or after_plan_summary.get("version"):
        deltas.extend(
            [
                _metric_delta("recovery_plan_step_count", before_plan_summary, after_plan_summary, "planned_step_count"),
                _metric_delta("recovery_plan_highest_severity_rank", before_plan_summary, after_plan_summary, "highest_severity_rank"),
            ]
        )
    outcome = _outcome(deltas, executor_after=executor_summary)
    return {
        "dashboard_recovery_post_validation_version": DASHBOARD_RECOVERY_POST_VALIDATION_VERSION,
        "generated_at": now or _iso_now(),
        "outcome": outcome,
        "before": {
            "snapshot": before_snapshot_summary,
            "action_queue": before_queue_summary,
            "recovery_plan": before_plan_summary,
        },
        "after": {
            "snapshot": after_snapshot_summary,
            "action_queue": after_queue_summary,
            "recovery_plan": after_plan_summary,
            "recovery_executor": executor_summary,
        },
        "deltas": deltas,
        "summary": {
            "improved_metric_count": sum(1 for item in deltas if item.get("direction") == "improved"),
            "worsened_metric_count": sum(1 for item in deltas if item.get("direction") == "worsened"),
            "unchanged_metric_count": sum(1 for item in deltas if item.get("direction") == "unchanged"),
            "executor_failure_count": executor_summary.get("failure_count") or 0,
            "executor_executed_command_count": executor_summary.get("executed_command_count") or 0,
            "executor_execute_requested": bool(executor_summary.get("execute_requested")),
        },
        "safety": {
            "read_only_validation": True,
            "subprocess_executed": False,
            "broker_order_submitted": False,
            "transmit_enabled": False,
            "live_order_routing_enabled": False,
        },
    }


def build_dashboard_recovery_post_validation_from_paths(
    *,
    before_snapshot_path: Path | None = None,
    after_snapshot_path: Path | None = None,
    before_action_queue_path: Path | None = None,
    after_action_queue_path: Path | None = None,
    before_recovery_plan_path: Path | None = None,
    after_recovery_plan_path: Path | None = None,
    recovery_executor_path: Path | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    return build_dashboard_recovery_post_validation(
        before_snapshot=_read_json(before_snapshot_path),
        after_snapshot=_read_json(after_snapshot_path),
        before_action_queue=_read_json(before_action_queue_path),
        after_action_queue=_read_json(after_action_queue_path),
        before_recovery_plan=_read_json(before_recovery_plan_path),
        after_recovery_plan=_read_json(after_recovery_plan_path),
        recovery_executor=_read_json(recovery_executor_path),
        now=now,
    )


def dashboard_recovery_post_validation_summary(report: dict[str, Any] | None) -> dict[str, Any]:
    data = report or {}
    summary = data.get("summary") or {}
    return {
        "version": data.get("dashboard_recovery_post_validation_version"),
        "generated_at": data.get("generated_at"),
        "outcome": data.get("outcome"),
        "improved_metric_count": summary.get("improved_metric_count") or 0,
        "worsened_metric_count": summary.get("worsened_metric_count") or 0,
        "unchanged_metric_count": summary.get("unchanged_metric_count") or 0,
        "executor_failure_count": summary.get("executor_failure_count") or 0,
        "executor_executed_command_count": summary.get("executor_executed_command_count") or 0,
    }


def dashboard_recovery_post_validation_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Dashboard Recovery Post-Validation",
        "",
        f"- Version: `{report.get('dashboard_recovery_post_validation_version')}`",
        f"- Generated at: `{report.get('generated_at')}`",
        f"- Outcome: `{report.get('outcome')}`",
        f"- Summary: `{report.get('summary') or {}}`",
        "- Safety: read-only validation; no subprocess execution, broker order submission, or transmit.",
        "",
        "| Metric | Before | After | Delta | Direction |",
        "| --- | ---: | ---: | ---: | --- |",
    ]
    for item in report.get("deltas") or []:
        lines.append(
            f"| {item.get('metric')} | {item.get('before')} | {item.get('after')} | {item.get('delta')} | {item.get('direction')} |"
        )
    if not report.get("deltas"):
        lines.append("| - | - | - | - | insufficient_baseline |")
    return "\n".join(lines) + "\n"


def write_dashboard_recovery_post_validation_json(output_path: Path, report: dict[str, Any]) -> Path:
    resolved = output_path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return resolved


def write_dashboard_recovery_post_validation_markdown(output_path: Path, report: dict[str, Any]) -> Path:
    resolved = output_path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(dashboard_recovery_post_validation_markdown(report), encoding="utf-8")
    return resolved


# --- merged from dashboard_recovery_closed_loop.py ---
DASHBOARD_RECOVERY_CLOSED_LOOP_VERSION = "dashboard-recovery-closed-loop-v1"

CommandRunner = Callable[[list[str], Path, float], dict[str, Any]]


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _parse_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


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


def _tail(value: Any, limit: int = 3000) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[-limit:]


def _tokens_for_subprocess(command: list[str]) -> list[str]:
    if command and command[0].lower() in {"python", "python.exe"}:
        return [sys.executable] + command[1:]
    return command


def _default_runner(command: list[str], cwd: Path, timeout_seconds: float) -> dict[str, Any]:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            [str(item) for item in _tokens_for_subprocess(command)],
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
    except Exception as exc:  # pragma: no cover - defensive audit wrapper
        return {
            "status": "failed",
            "returncode": None,
            "stdout_tail": "",
            "stderr_tail": "",
            "duration_seconds": round(time.monotonic() - started, 3),
            "error": f"{exc.__class__.__name__}: {str(exc)[:500]}",
        }


def _status_rank(value: Any) -> int:
    return {"green": 0, "watch": 1, "yellow": 2, "red": 3}.get(str(value or "").lower(), 0)


def _signature(snapshot: dict[str, Any], action_queue: dict[str, Any], recovery_plan: dict[str, Any]) -> dict[str, Any]:
    status = snapshot.get("status") or {}
    actions = action_queue.get("actions") or []
    steps = recovery_plan.get("steps") or []
    source_keys: list[str] = []
    for item in actions:
        key = item.get("source_key")
        if key and key not in source_keys:
            source_keys.append(str(key))
    return {
        "status_color": status.get("color"),
        "status_rank": _status_rank(status.get("color")),
        "status_reasons": sorted(str(item) for item in (status.get("reasons") or [])),
        "action_ids": [str(item.get("action_id")) for item in actions[:8] if item.get("action_id")],
        "step_ids": [str(item.get("step_id")) for item in steps[:8] if item.get("step_id")],
        "source_keys": source_keys[:8],
        "highest_severity": action_queue.get("highest_severity"),
        "highest_priority_score": action_queue.get("highest_priority_score") or 0,
    }


def _signature_key(signature: dict[str, Any]) -> str:
    return json.dumps(signature, sort_keys=True, ensure_ascii=True)


def _load_state(path: Path | None) -> dict[str, Any]:
    data = _read_json(path)
    if data.get("dashboard_recovery_closed_loop_state_version") == DASHBOARD_RECOVERY_CLOSED_LOOP_VERSION:
        return data
    return {
        "dashboard_recovery_closed_loop_state_version": DASHBOARD_RECOVERY_CLOSED_LOOP_VERSION,
        "updated_at": None,
        "attempts": {},
    }


def _cooldown_decision(
    *,
    state: dict[str, Any],
    signature_key: str,
    now_dt: datetime,
    cooldown_seconds: float,
) -> dict[str, Any]:
    previous = (state.get("attempts") or {}).get(signature_key) or {}
    last_attempt_at = _parse_time(previous.get("last_attempt_at"))
    if last_attempt_at is None:
        return {
            "allowed": True,
            "reason": "no_prior_attempt_for_signature",
            "previous_attempt": previous,
            "next_eligible_at": now_dt.isoformat(),
            "cooldown_remaining_seconds": 0.0,
        }
    next_eligible = last_attempt_at + timedelta(seconds=max(0.0, float(cooldown_seconds)))
    remaining = max(0.0, (next_eligible - now_dt).total_seconds())
    if now_dt >= next_eligible:
        return {
            "allowed": True,
            "reason": "cooldown_elapsed",
            "previous_attempt": previous,
            "next_eligible_at": now_dt.isoformat(),
            "cooldown_remaining_seconds": 0.0,
        }
    return {
        "allowed": False,
        "reason": "cooldown_active_for_same_recovery_signature",
        "previous_attempt": previous,
        "next_eligible_at": next_eligible.isoformat(),
        "cooldown_remaining_seconds": round(remaining, 3),
    }


def _dashboard_refresh_command(profile_json: Path) -> list[str]:
    return [
        "python",
        "main.py",
        "trading-dashboard-loop",
        "--profile-json",
        str(profile_json),
        "--max-ticks",
        "1",
        "--interval-seconds",
        "0",
        "--disable-alerts",
        "--disable-telegram",
    ]


def run_dashboard_recovery_closed_loop(
    *,
    snapshot_path: Path,
    action_queue_path: Path,
    recovery_plan_path: Path,
    profile_json_path: Path,
    state_path: Path | None = None,
    output_executor_path: Path | None = None,
    execute: bool = False,
    refresh_after: bool = True,
    max_steps: int = 3,
    cooldown_seconds: float = 300.0,
    timeout_seconds: float = 120.0,
    allowed_phases: set[str] | None = None,
    cwd: Path | None = None,
    runner: CommandRunner | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    cwd = (cwd or Path.cwd()).expanduser().resolve()
    runner = runner or _default_runner
    now_text = now or _iso_now()
    now_dt = _parse_time(now_text) or datetime.now(UTC)
    allowed_phases = set(allowed_phases or DEFAULT_ALLOWED_PHASES)

    before_snapshot = _read_json(snapshot_path)
    before_action_queue = _read_json(action_queue_path)
    before_recovery_plan = _read_json(recovery_plan_path)
    signature = _signature(before_snapshot, before_action_queue, before_recovery_plan)
    signature_key = _signature_key(signature)
    state = _load_state(state_path)
    cooldown = _cooldown_decision(
        state=state,
        signature_key=signature_key,
        now_dt=now_dt,
        cooldown_seconds=cooldown_seconds,
    )

    executor_report: dict[str, Any]
    if cooldown.get("allowed"):
        executor_report = run_dashboard_recovery_executor(
            before_recovery_plan,
            execute=execute,
            max_steps=max_steps,
            allowed_phases=allowed_phases,
            timeout_seconds=timeout_seconds,
            cwd=cwd,
            runner=runner,
            now=now_text,
        )
    else:
        executor_report = {
            "dashboard_recovery_executor_version": "dashboard-recovery-executor-v1",
            "generated_at": now_text,
            "source_recovery_plan_version": before_recovery_plan.get("dashboard_recovery_plan_version"),
            "source_recovery_plan_generated_at": before_recovery_plan.get("generated_at"),
            "execute_requested": bool(execute),
            "final_status": "skipped_cooldown",
            "allowed_phases": sorted(allowed_phases),
            "max_steps": int(max_steps),
            "selected_step_count": 0,
            "executed_command_count": 0,
            "failure_count": 0,
            "status_counts": {"skipped_cooldown": 1},
            "command_status_counts": {},
            "step_results": [],
            "command_results": [],
            "execution_policy": {
                "allowlist_only": True,
                "execute_subprocesses": False,
                "submit_broker_orders": False,
                "transmit_enabled": False,
                "mutate_scorer_rows": False,
                "mutate_real_portfolio_state": False,
            },
            "safety": {
                "read_only_recovery_executor": True,
                "allowlist_enforced": True,
                "broker_order_submitted": False,
                "transmit_enabled": False,
                "live_order_routing_enabled": False,
            },
        }
    executor_json_path = _write_json(output_executor_path, executor_report) if output_executor_path else None

    refresh_report = {
        "enabled": bool(refresh_after),
        "status": "skipped",
        "command": None,
        "returncode": None,
        "duration_seconds": None,
        "stdout_tail": "",
        "stderr_tail": "",
        "error": None,
    }
    if refresh_after:
        command = _dashboard_refresh_command(profile_json_path)
        refresh_result = runner(command, cwd, timeout_seconds)
        refresh_report.update(refresh_result)
        refresh_report["command"] = " ".join(command)

    after_snapshot = _read_json(snapshot_path)
    after_action_queue = _read_json(action_queue_path)
    after_recovery_plan = _read_json(recovery_plan_path)
    post_validation = build_dashboard_recovery_post_validation(
        before_snapshot=before_snapshot,
        after_snapshot=after_snapshot,
        before_action_queue=before_action_queue,
        after_action_queue=after_action_queue,
        before_recovery_plan=before_recovery_plan,
        after_recovery_plan=after_recovery_plan,
        recovery_executor=executor_report,
        now=now_text,
    )

    attempts = state.setdefault("attempts", {})
    attempts[signature_key] = {
        "signature": signature,
        "last_attempt_at": now_text,
        "execute_requested": bool(execute),
        "executor_final_status": executor_report.get("final_status"),
        "post_validation_outcome": post_validation.get("outcome"),
        "improved_metric_count": (post_validation.get("summary") or {}).get("improved_metric_count") or 0,
        "worsened_metric_count": (post_validation.get("summary") or {}).get("worsened_metric_count") or 0,
        "cooldown_seconds": float(cooldown_seconds),
        "next_eligible_at": (now_dt + timedelta(seconds=max(0.0, float(cooldown_seconds)))).isoformat(),
    }
    state["updated_at"] = now_text
    state_json_path = _write_json(state_path, state) if state_path else None

    outcome = post_validation.get("outcome") or "unknown"
    if executor_report.get("failure_count"):
        loop_status = "failed"
    elif not cooldown.get("allowed"):
        loop_status = "skipped_cooldown"
    elif outcome in {"improved", "mixed_improved"}:
        loop_status = "improved"
    elif outcome in {"worsened", "mixed_worsened"}:
        loop_status = "needs_manual_review"
    else:
        loop_status = "unchanged"

    return {
        "dashboard_recovery_closed_loop_version": DASHBOARD_RECOVERY_CLOSED_LOOP_VERSION,
        "generated_at": now_text,
        "status": loop_status,
        "execute_requested": bool(execute),
        "refresh_after": bool(refresh_after),
        "max_steps": int(max_steps),
        "cooldown_seconds": float(cooldown_seconds),
        "signature": signature,
        "signature_key": signature_key,
        "cooldown_decision": cooldown,
        "executor": executor_report,
        "executor_json_path": str(executor_json_path) if executor_json_path else None,
        "refresh": refresh_report,
        "post_validation": post_validation,
        "state_json_path": str(state_json_path) if state_json_path else None,
        "artifacts": {
            "snapshot_path": str(snapshot_path.expanduser().resolve()),
            "action_queue_path": str(action_queue_path.expanduser().resolve()),
            "recovery_plan_path": str(recovery_plan_path.expanduser().resolve()),
            "profile_json_path": str(profile_json_path.expanduser().resolve()),
        },
        "safety": {
            "closed_loop_controller": True,
            "allowlist_executor_only": True,
            "dashboard_refresh_read_only": bool(refresh_after),
            "broker_order_submitted": False,
            "transmit_enabled": False,
            "live_order_routing_enabled": False,
            "mutate_real_portfolio_state": False,
            "mutate_scorer_rows": False,
        },
    }


def dashboard_recovery_closed_loop_markdown(report: dict[str, Any]) -> str:
    executor = report.get("executor") or {}
    post = report.get("post_validation") or {}
    summary = post.get("summary") or {}
    refresh = report.get("refresh") or {}
    cooldown = report.get("cooldown_decision") or {}
    lines = [
        "# Dashboard Recovery Closed Loop",
        "",
        f"- Version: `{report.get('dashboard_recovery_closed_loop_version')}`",
        f"- Generated at: `{report.get('generated_at')}`",
        f"- Status: `{report.get('status')}`",
        f"- Execute requested: `{report.get('execute_requested')}`",
        f"- Cooldown decision: `{cooldown.get('reason')}` allowed `{cooldown.get('allowed')}` next `{cooldown.get('next_eligible_at')}`",
        f"- Executor: `{executor.get('final_status')}` commands `{executor.get('executed_command_count') or 0}` failures `{executor.get('failure_count') or 0}`",
        f"- Refresh after: `{refresh.get('enabled')}` status `{refresh.get('status')}`",
        f"- Post-validation: `{post.get('outcome')}` improved `{summary.get('improved_metric_count') or 0}` worsened `{summary.get('worsened_metric_count') or 0}` unchanged `{summary.get('unchanged_metric_count') or 0}`",
        "- Safety: allowlist-only read-only recovery loop; no broker order submission or transmit.",
        "",
        "## Recovery signature",
        "",
        f"- Status color: `{(report.get('signature') or {}).get('status_color')}`",
        f"- Reasons: `{(report.get('signature') or {}).get('status_reasons') or []}`",
        f"- Action IDs: `{(report.get('signature') or {}).get('action_ids') or []}`",
        f"- Source keys: `{(report.get('signature') or {}).get('source_keys') or []}`",
        "",
        "## Post-validation deltas",
        "",
        "| Metric | Before | After | Direction | Delta |",
        "| --- | ---: | ---: | --- | ---: |",
    ]
    for item in post.get("deltas") or []:
        lines.append(
            f"| {item.get('metric')} | {item.get('before')} | {item.get('after')} | {item.get('direction')} | {item.get('delta')} |"
        )
    if not post.get("deltas"):
        lines.append("| - | - | - | insufficient_baseline | - |")
    return "\n".join(lines) + "\n"


def write_dashboard_recovery_closed_loop_json(output_path: Path, report: dict[str, Any]) -> Path:
    return _write_json(output_path, report)


def write_dashboard_recovery_closed_loop_markdown(output_path: Path, report: dict[str, Any]) -> Path:
    resolved = output_path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(dashboard_recovery_closed_loop_markdown(report), encoding="utf-8")
    return resolved


# --- merged from dashboard_latency_safety_audit.py ---
DASHBOARD_LATENCY_SAFETY_AUDIT_VERSION = "dashboard-latency-safety-audit-v1"


SAFETY_STAGE_ROLES: dict[str, str] = {
    "evidence_refresh_watchdog": "Refreshes evidence freshness/recovery artifacts; stale evidence remains visible to gates and dashboard status.",
    "source_ingest_health": "Classifies online-required source health from freshness, refresh queue, watchdog, and provider validation artifacts.",
    "market_data_risk_gate": "Turns Market Data Desk quality/temperature into entry block, haircut, or defensive controls.",
    "market_data_session_transition_closed_loop": "Rebuilds session-transition market-data/risk/operating artifacts before ordinary alpha release checks.",
    "session_transition_release_governor": "Final ordinary-alpha release gate after session-transition, market-data, portfolio, and evidence checks.",
    "release_governed_recovery_closed_loop": "Rebuilds the release-governed paper ledger/router and verifies pending/fill safety.",
}


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


def _stage_policy(profile: dict[str, Any], stage_key: str) -> dict[str, Any]:
    budget = profile.get("dashboard_latency_budget") if isinstance(profile.get("dashboard_latency_budget"), dict) else {}
    stages = budget.get("stages") if isinstance(budget.get("stages"), dict) else {}
    stage = stages.get(stage_key) if isinstance(stages.get(stage_key), dict) else {}
    return {
        "latency_budget_enabled": bool(budget.get("enabled", False)),
        "stage_budget_enabled": bool(stage.get("enabled", False)),
        "mode": stage.get("mode") or ("always_run" if not stage else "not_configured"),
        "max_artifact_age_seconds": stage.get("max_artifact_age_seconds"),
        "safety_fallback": stage.get("safety_fallback"),
    }


def _section_enabled(profile: dict[str, Any], section: str) -> bool:
    value = profile.get(section)
    if not isinstance(value, dict):
        return False
    return bool(value.get("enabled", False)) and not bool(value.get("ignore", False))


def _tick_elapsed_seconds(summary: dict[str, Any]) -> list[float]:
    values: list[float] = []
    for item in summary.get("tick_summaries") or []:
        try:
            values.append(float(item.get("elapsed_seconds")))
        except (TypeError, ValueError):
            continue
    return values


def _pre_snapshot_stage_counts(summary: dict[str, Any], stage_key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in summary.get("pre_snapshot_refresh_summaries") or []:
        stage = item.get(stage_key) or {}
        if stage_key == "source_ingest_health":
            stage = ((item.get("evidence_refresh_watchdog") or {}).get("source_ingest_health") or {})
        status = str(stage.get("status") or ("applied" if stage.get("applied") else "not_present"))
        counts[status] = counts.get(status, 0) + 1
    return counts


def build_dashboard_latency_safety_audit(
    *,
    profile_payload: dict[str, Any],
    loop_summary_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    loop_summary_payload = loop_summary_payload or {}
    stages = [
        "evidence_refresh_watchdog",
        "source_ingest_health",
        "market_data_risk_gate",
        "market_data_session_transition_closed_loop",
        "session_transition_release_governor",
        "release_governed_recovery_closed_loop",
    ]
    stage_audits: list[dict[str, Any]] = []
    violations: list[str] = []
    for stage_key in stages:
        policy = _stage_policy(profile_payload, stage_key)
        section_key = stage_key
        if stage_key == "market_data_risk_gate":
            section_key = "market_data_risk_gate"
        enabled = _section_enabled(profile_payload, section_key)
        cheap_safety_gate = stage_key in {
            "market_data_risk_gate",
            "market_data_session_transition_closed_loop",
            "session_transition_release_governor",
            "release_governed_recovery_closed_loop",
        }
        latency_optimized = bool(policy.get("stage_budget_enabled")) or cheap_safety_gate
        safety_kept = enabled or stage_key in {"evidence_refresh_watchdog", "source_ingest_health"}
        if stage_key in {"session_transition_release_governor", "release_governed_recovery_closed_loop"} and not enabled:
            violations.append(f"{stage_key}_disabled")
            safety_kept = False
        stage_audits.append(
            {
                "stage_key": stage_key,
                "role": SAFETY_STAGE_ROLES.get(stage_key),
                "enabled": enabled,
                "safety_kept": safety_kept,
                "latency_optimized": latency_optimized,
                "cheap_safety_gate_kept_inline": cheap_safety_gate,
                "latency_policy": policy,
                "observed_status_counts": _pre_snapshot_stage_counts(loop_summary_payload, stage_key),
            }
        )

    elapsed = _tick_elapsed_seconds(loop_summary_payload)
    latency_summary = {
        "observed_tick_count": len(elapsed),
        "first_tick_elapsed_seconds": elapsed[0] if elapsed else None,
        "last_tick_elapsed_seconds": elapsed[-1] if elapsed else None,
        "min_tick_elapsed_seconds": min(elapsed) if elapsed else None,
        "max_tick_elapsed_seconds": max(elapsed) if elapsed else None,
        "latency_budget_enabled": bool((profile_payload.get("dashboard_latency_budget") or {}).get("enabled", False)),
    }
    safety_invariants = {
        "broker_order_submission_allowed": False,
        "transmit_allowed": False,
        "live_order_routing_allowed": False,
        "stale_or_missing_artifacts_fail_closed": True,
        "cheap_final_gates_kept_inline": True,
    }
    status = "pass" if not violations else "review"
    return {
        "dashboard_latency_safety_audit_version": DASHBOARD_LATENCY_SAFETY_AUDIT_VERSION,
        "status": status,
        "violations": violations,
        "latency_summary": latency_summary,
        "stage_audits": stage_audits,
        "safety_invariants": safety_invariants,
    }


def build_dashboard_latency_safety_audit_from_paths(
    *,
    profile_json_path: Path,
    loop_summary_json_path: Path | None = None,
) -> dict[str, Any]:
    return build_dashboard_latency_safety_audit(
        profile_payload=_read_json(profile_json_path),
        loop_summary_payload=_read_json(loop_summary_json_path),
    )


def dashboard_latency_safety_audit_markdown(report: dict[str, Any]) -> str:
    latency = report.get("latency_summary") or {}
    lines = [
        "# Dashboard Latency Safety Audit",
        "",
        f"- Version: `{report.get('dashboard_latency_safety_audit_version')}`",
        f"- Status: `{report.get('status')}`",
        f"- Violations: `{report.get('violations') or []}`",
        f"- Latency budget enabled: `{latency.get('latency_budget_enabled')}`",
        f"- Tick elapsed first/last/min/max: `{latency.get('first_tick_elapsed_seconds')}` / `{latency.get('last_tick_elapsed_seconds')}` / `{latency.get('min_tick_elapsed_seconds')}` / `{latency.get('max_tick_elapsed_seconds')}`",
        "",
        "## Stage audit",
        "",
        "| Stage | Enabled | Safety kept | Latency optimized | Policy | Observed statuses |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for item in report.get("stage_audits") or []:
        policy = item.get("latency_policy") or {}
        policy_text = f"{policy.get('mode')} age={policy.get('max_artifact_age_seconds')}"
        lines.append(
            f"| `{item.get('stage_key')}` | `{item.get('enabled')}` | `{item.get('safety_kept')}` | `{item.get('latency_optimized')}` | `{policy_text}` | `{item.get('observed_status_counts') or {}}` |"
        )
    lines.extend(
        [
            "",
            "## Safety invariants",
            "",
        ]
    )
    for key, value in (report.get("safety_invariants") or {}).items():
        lines.append(f"- `{key}`: `{value}`")
    return "\n".join(lines) + "\n"


def write_dashboard_latency_safety_audit_json(output_path: Path, report: dict[str, Any]) -> Path:
    resolved = output_path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return resolved


def write_dashboard_latency_safety_audit_markdown(output_path: Path, report: dict[str, Any]) -> Path:
    resolved = output_path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(dashboard_latency_safety_audit_markdown(report), encoding="utf-8")
    return resolved


# --- merged from dashboard_supervisor_handoff_audit.py ---
DASHBOARD_SUPERVISOR_HANDOFF_AUDIT_VERSION = "dashboard-supervisor-handoff-audit-v1"


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


def _path_from_profile(profile: dict[str, Any], key: str, profile_base: Path | None) -> Path | None:
    handoff = profile.get("dashboard_source_supervisor_handoff") if isinstance(profile.get("dashboard_source_supervisor_handoff"), dict) else {}
    value = handoff.get(key)
    if not value:
        return None
    path = Path(str(value))
    if not path.is_absolute() and profile_base is not None:
        path = profile_base / path
    return path.expanduser().resolve()


def _age_seconds(path: Path | None) -> float | None:
    if path is None or not path.exists():
        return None
    return max(0.0, time.time() - path.stat().st_mtime)


def _section_enabled(profile: dict[str, Any], section: str) -> bool:
    value = profile.get(section)
    return isinstance(value, dict) and bool(value.get("enabled", False)) and not bool(value.get("ignore", False))


def _section_ignored(profile: dict[str, Any], section: str) -> bool:
    value = profile.get(section)
    return isinstance(value, dict) and bool(value.get("ignore", False))


def _artifact_status(path: Path | None, max_age_seconds: float) -> dict[str, Any]:
    exists = bool(path and path.exists())
    age = _age_seconds(path)
    fresh = bool(exists and age is not None and age <= float(max_age_seconds))
    return {
        "path": str(path) if path else None,
        "exists": exists,
        "age_seconds": round(age, 3) if age is not None else None,
        "max_age_seconds": float(max_age_seconds),
        "fresh": fresh,
    }


def build_dashboard_supervisor_handoff_audit(
    *,
    profile_payload: dict[str, Any],
    profile_base: Path | None = None,
    loop_summary_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    handoff = profile_payload.get("dashboard_source_supervisor_handoff") if isinstance(profile_payload.get("dashboard_source_supervisor_handoff"), dict) else {}
    loop_summary_payload = loop_summary_payload or {}
    supervisor_path = _path_from_profile(profile_payload, "source_online_supervisor_json", profile_base)
    evidence_path = _path_from_profile(profile_payload, "evidence_watchdog_json", profile_base)
    source_health_path = _path_from_profile(profile_payload, "source_ingest_health_json", profile_base)
    supervisor_payload = _read_json(supervisor_path)
    evidence_payload = _read_json(evidence_path)
    source_health_payload = _read_json(source_health_path)

    artifacts = {
        "source_online_supervisor": _artifact_status(supervisor_path, float(handoff.get("max_supervisor_age_seconds", 120.0))),
        "evidence_watchdog": _artifact_status(evidence_path, float(handoff.get("max_evidence_watchdog_age_seconds", 120.0))),
        "source_ingest_health": _artifact_status(source_health_path, float(handoff.get("max_source_health_age_seconds", 120.0))),
    }
    supervisor_fresh = bool(artifacts["source_online_supervisor"].get("fresh"))
    source_health_fresh = bool(artifacts["source_ingest_health"].get("fresh"))
    supervisor_status = str(supervisor_payload.get("final_status") or "").lower()
    supervisor_source_health = str(supervisor_payload.get("final_source_ingest_health_status") or "").lower()
    source_health_status = str(source_health_payload.get("overall_status") or "").lower()
    watchdog_superseded_by_supervisor = (
        bool(handoff.get("allow_stale_evidence_watchdog_when_supervisor_green", False))
        and supervisor_fresh
        and source_health_fresh
        and supervisor_status in {"green", "ok", "healthy"}
        and supervisor_source_health in {"green", "healthy"}
        and source_health_status in {"green", "healthy"}
    )
    artifacts["evidence_watchdog"]["superseded_by_fresh_supervisor_green"] = watchdog_superseded_by_supervisor
    inline_heavy_disabled = (
        not _section_enabled(profile_payload, "evidence_refresh_watchdog")
        and not _section_enabled(profile_payload, "source_ingest_health")
        and (_section_ignored(profile_payload, "evidence_refresh_watchdog") or not bool((profile_payload.get("evidence_refresh_watchdog") or {}).get("enabled", False)))
    )
    final_gates_inline = all(
        _section_enabled(profile_payload, key)
        for key in (
            "market_data_risk_gate",
            "market_data_session_transition_closed_loop",
            "session_transition_release_governor",
            "release_governed_recovery_closed_loop",
        )
    )
    pre_snapshot = loop_summary_payload.get("pre_snapshot_refresh_summaries") or []
    heavy_inline_seen = any(
        bool((item.get("evidence_refresh_watchdog") or {}).get("applied"))
        or bool(((item.get("evidence_refresh_watchdog") or {}).get("source_ingest_health") or {}).get("applied"))
        for item in pre_snapshot
    )
    release_governed_seen = any((item.get("release_governed_recovery_closed_loop") or {}).get("status") == "pass" for item in pre_snapshot)
    violations: list[str] = []
    if not bool(handoff.get("enabled", False)):
        violations.append("handoff_not_enabled")
    if not inline_heavy_disabled:
        violations.append("heavy_source_refresh_not_disabled_in_dashboard_profile")
    if not final_gates_inline:
        violations.append("cheap_or_final_safety_gate_not_inline")
    for key, status in artifacts.items():
        if not status.get("exists"):
            violations.append(f"{key}_artifact_missing")
        elif not status.get("fresh") and not (key == "evidence_watchdog" and watchdog_superseded_by_supervisor):
            violations.append(f"{key}_artifact_stale")
    if heavy_inline_seen:
        violations.append("loop_summary_shows_heavy_source_refresh_ran_inline")
    if pre_snapshot and not release_governed_seen:
        violations.append("loop_summary_missing_release_governed_pass")

    return {
        "dashboard_supervisor_handoff_audit_version": DASHBOARD_SUPERVISOR_HANDOFF_AUDIT_VERSION,
        "status": "pass" if not violations else "review",
        "violations": violations,
        "handoff_enabled": bool(handoff.get("enabled", False)),
        "inline_heavy_source_refresh_disabled": inline_heavy_disabled,
        "cheap_final_safety_gates_inline": final_gates_inline,
        "heavy_inline_seen_in_loop_summary": heavy_inline_seen,
        "release_governed_pass_seen_in_loop_summary": release_governed_seen if pre_snapshot else None,
        "watchdog_superseded_by_fresh_supervisor_green": watchdog_superseded_by_supervisor,
        "artifacts": artifacts,
        "source_online_supervisor_summary": {
            "version": supervisor_payload.get("source_online_supervisor_version"),
            "final_status": supervisor_payload.get("final_status"),
            "final_source_ingest_health_status": supervisor_payload.get("final_source_ingest_health_status"),
            "final_online_problem_source_keys": supervisor_payload.get("final_online_problem_source_keys") or [],
        },
        "evidence_watchdog_summary": {
            "version": evidence_payload.get("evidence_refresh_watchdog_version"),
            "final_status": evidence_payload.get("final_status"),
            "final_freshness_status": evidence_payload.get("final_freshness_status"),
            "final_stale_source_keys": evidence_payload.get("final_stale_source_keys") or [],
            "final_lagging_source_keys": evidence_payload.get("final_lagging_source_keys") or [],
        },
        "source_ingest_health_summary": {
            "version": source_health_payload.get("source_ingest_health_version"),
            "overall_status": source_health_payload.get("overall_status"),
            "online_problem_source_keys": source_health_payload.get("online_problem_source_keys") or [],
            "degraded_source_keys": source_health_payload.get("degraded_source_keys") or [],
        },
        "safety_invariants": {
            "dashboard_submits_orders": False,
            "source_supervisor_submits_orders": False,
            "transmit_enabled": False,
            "live_order_routing_enabled": False,
            "dashboard_reads_artifacts_only_for_source_evidence": True,
            "final_entry_release_gates_kept_inline": final_gates_inline,
        },
    }


def build_dashboard_supervisor_handoff_audit_from_paths(
    *,
    profile_json_path: Path,
    loop_summary_json_path: Path | None = None,
) -> dict[str, Any]:
    profile_json_path = profile_json_path.expanduser().resolve()
    return build_dashboard_supervisor_handoff_audit(
        profile_payload=_read_json(profile_json_path),
        profile_base=profile_json_path.parent,
        loop_summary_payload=_read_json(loop_summary_json_path),
    )


def dashboard_supervisor_handoff_audit_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Dashboard Supervisor Handoff Audit",
        "",
        f"- Version: `{report.get('dashboard_supervisor_handoff_audit_version')}`",
        f"- Status: `{report.get('status')}`",
        f"- Violations: `{report.get('violations') or []}`",
        f"- Heavy source refresh disabled inline: `{report.get('inline_heavy_source_refresh_disabled')}`",
        f"- Cheap/final safety gates inline: `{report.get('cheap_final_safety_gates_inline')}`",
        f"- Heavy inline seen in loop summary: `{report.get('heavy_inline_seen_in_loop_summary')}`",
        f"- Stale watchdog superseded by fresh supervisor green: `{report.get('watchdog_superseded_by_fresh_supervisor_green')}`",
        "",
        "## Artifacts",
        "",
        "| Artifact | Exists | Fresh | Superseded | Age seconds | Max age | Path |",
        "| --- | --- | --- | --- | ---: | ---: | --- |",
    ]
    for key, item in (report.get("artifacts") or {}).items():
        lines.append(
            f"| `{key}` | `{item.get('exists')}` | `{item.get('fresh')}` | `{item.get('superseded_by_fresh_supervisor_green') or False}` | `{item.get('age_seconds')}` | `{item.get('max_age_seconds')}` | `{item.get('path')}` |"
        )
    lines.extend(["", "## Safety invariants", ""])
    for key, value in (report.get("safety_invariants") or {}).items():
        lines.append(f"- `{key}`: `{value}`")
    return "\n".join(lines) + "\n"


def write_dashboard_supervisor_handoff_audit_json(output_path: Path, report: dict[str, Any]) -> Path:
    resolved = output_path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return resolved


def write_dashboard_supervisor_handoff_audit_markdown(output_path: Path, report: dict[str, Any]) -> Path:
    resolved = output_path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(dashboard_supervisor_handoff_audit_markdown(report), encoding="utf-8")
    return resolved
