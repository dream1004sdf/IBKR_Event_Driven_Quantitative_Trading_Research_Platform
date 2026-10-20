"""Market-data session transition, release governance, and governed recovery operations.

Middle-level module for market session transition safety gates and release-controlled recovery.
Public APIs from the previous focused modules are preserved; private helpers are namespaced
per section to avoid silent helper collisions.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from recorder.options_analysis import (
    build_execution_intent_ledger_report,
    build_paper_order_router_report,
    execution_intent_ledger_markdown,
    paper_order_router_markdown,
)



# --- merged from market_data_session_transition_gate.py ---
MARKET_DATA_SESSION_TRANSITION_GATE_VERSION = "market-data-session-transition-gate-v1"

OFF_HOURS_SESSIONS = {"pre_market", "after_hours", "overnight", "closed"}


def _mdstg_iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _mdstg_result(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    result = payload.get("result")
    return result if isinstance(result, dict) else payload


def _mdstg_read_json(path: Path | None) -> dict[str, Any]:
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


def _mdstg_parse_dt(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _mdstg_age_seconds(value: Any, now_dt: datetime) -> float | None:
    parsed = _mdstg_parse_dt(value)
    if parsed is None:
        return None
    return round(max(0.0, (now_dt - parsed).total_seconds()), 3)


def _mdstg_session_name(payload: dict[str, Any]) -> str | None:
    data = _mdstg_result(payload)
    session = data.get("session")
    if isinstance(session, dict):
        return session.get("session")
    return data.get("session_name")


def _mdstg_session_context_from_overlay(payload: dict[str, Any]) -> dict[str, Any]:
    data = _mdstg_result(payload)
    session = data.get("session") if isinstance(data.get("session"), dict) else {}
    summary = data.get("summary") or {}
    return {
        "available": bool(data),
        "session_name": _mdstg_session_name(data),
        "is_regular_session": bool(session.get("is_regular_session") if session else data.get("is_regular_session")),
        "risk_interpretation": data.get("risk_interpretation"),
        "generated_at": data.get("generated_at"),
        "source_market_data_generated_at": data.get("source_market_data_generated_at"),
        "next_regular_open": data.get("next_regular_open") or {},
        "stock_cash_overnight_monitoring_enabled": data.get("stock_cash_overnight_monitoring_enabled"),
        "option_quotes_regular_session_dependent": data.get("option_quotes_regular_session_dependent"),
        "monitorability_counts": data.get("monitorability_counts") or summary.get("monitorability_counts") or {},
        "monitorable_alert_instruments": data.get("monitorable_alert_instruments") or summary.get("monitorable_alert_instruments") or [],
        "regular_dependent_alert_instruments": data.get("regular_dependent_alert_instruments") or summary.get("regular_dependent_alert_instruments") or [],
    }


def _mdstg_state_session_context(state_payload: dict[str, Any] | None) -> dict[str, Any]:
    data = _mdstg_result(state_payload)
    if not data:
        return {"available": False, "session_name": None}
    return {
        "available": True,
        "session_name": data.get("last_session_name"),
        "is_regular_session": bool(data.get("last_is_regular_session")),
        "risk_interpretation": data.get("last_risk_interpretation"),
        "generated_at": data.get("last_session_overlay_generated_at"),
        "decision": data.get("last_decision"),
    }


def _mdstg_transition_type(previous_session: str | None, current_session: str | None) -> str:
    if not previous_session or not current_session:
        return "unknown"
    if previous_session == current_session:
        return "unchanged"
    if previous_session in OFF_HOURS_SESSIONS and current_session == "regular":
        return "off_hours_to_regular"
    if previous_session == "regular" and current_session in OFF_HOURS_SESSIONS:
        return "regular_to_off_hours"
    if previous_session in OFF_HOURS_SESSIONS and current_session in OFF_HOURS_SESSIONS:
        return "off_hours_to_off_hours"
    return "session_changed"


def _mdstg_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(default if value is None or value == "" else value)
    except (TypeError, ValueError):
        return float(default)


def _mdstg_risk_gate_context(payload: dict[str, Any], now_dt: datetime) -> dict[str, Any]:
    data = _mdstg_result(payload)
    session = data.get("market_data_session_context") or {}
    return {
        "available": bool(data),
        "generated_at": data.get("generated_at"),
        "age_seconds": _mdstg_age_seconds(data.get("generated_at"), now_dt),
        "gate_status": data.get("gate_status"),
        "action": data.get("action"),
        "allowed": data.get("allowed"),
        "new_entries_allowed": data.get("new_entries_allowed"),
        "alpha_entries_allowed": data.get("alpha_entries_allowed"),
        "hedge_entries_allowed": data.get("hedge_entries_allowed"),
        "entry_size_multiplier": data.get("entry_size_multiplier"),
        "session_name": session.get("session_name"),
        "session_priority_class": session.get("session_priority_class"),
        "session_adjusted_priority": bool(data.get("session_adjusted_priority")),
        "blocking_reasons": data.get("blocking_reasons") or [],
        "defensive_reasons": data.get("defensive_reasons") or [],
        "haircut_reasons": data.get("haircut_reasons") or [],
    }


def _mdstg_operating_gate_context(payload: dict[str, Any], now_dt: datetime) -> dict[str, Any]:
    data = _mdstg_result(payload)
    mode = data.get("market_data_operating_mode") or {}
    if not isinstance(mode, dict):
        mode = {"mode": mode}
    return {
        "available": bool(data),
        "generated_at": data.get("generated_at"),
        "age_seconds": _mdstg_age_seconds(data.get("generated_at"), now_dt),
        "auto_ready_count": int(data.get("auto_ready_count") or 0),
        "mode": mode.get("mode") or (data.get("market_data_defensive_mode") or {}).get("mode"),
        "severity": mode.get("severity") or (data.get("market_data_defensive_mode") or {}).get("severity"),
        "ordinary_alpha_entries_allowed": mode.get("ordinary_alpha_entries_allowed"),
        "hedge_or_defensive_entries_allowed": mode.get("hedge_or_defensive_entries_allowed"),
        "max_new_entry_size_multiplier": mode.get("max_new_entry_size_multiplier"),
        "session_priority_class": (data.get("market_data_session_context") or {}).get("session_priority_class"),
        "session_adjusted_priority": bool(data.get("market_data_session_adjusted_priority")),
    }


def _mdstg_command_list(
    *,
    market_data_json: str,
    session_overlay_json: str,
    risk_gate_json: str,
    auto_gate_json: str,
    defensive_mode_json: str,
    operating_auto_gate_json: str,
) -> list[dict[str, Any]]:
    return [
        {
            "step": 1,
            "reason": "refresh_session_interpretation_after_session_boundary",
            "command": (
                f"python main.py market-data-session-overlay --market-data-json {market_data_json} "
                f"--output exports/m198_market_data_session_overlay.md --json-output-path {session_overlay_json}"
            ),
        },
        {
            "step": 2,
            "reason": "rebuild_market_data_risk_gate_with_fresh_session_context",
            "command": (
                f"python main.py market-data-risk-gate --market-data-json {market_data_json} "
                f"--market-data-session-overlay-json {session_overlay_json} "
                f"--output exports/m198_market_data_risk_gate.md --json-output-path {risk_gate_json}"
            ),
        },
        {
            "step": 3,
            "reason": "replay_paper_auto_gate_against_the_fresh_market_data_gate",
            "command": (
                "python main.py replay-score-auto-execution-gate "
                "--preview-json exports/m112_m111_order_preview.json "
                "--short-availability-json exports/m113_ibkr_short_availability.json "
                "--evidence-preflight-json exports/m156_evidence_preflight_latest.json "
                f"--market-data-risk-gate-json {risk_gate_json} "
                "--allow-fractional-shares --allow-short-stock --max-auto-orders 10 "
                "--min-cash-buffer 50 --allowed-ticket-types stock --allowed-actions BUY,SELL "
                f"--output exports/m198_market_data_risk_auto_gate.md --json-output-path {auto_gate_json}"
            ),
        },
        {
            "step": 4,
            "reason": "derive_session_aware_defensive_mode",
            "command": (
                f"python main.py market-data-defensive-mode --market-data-risk-gate-json {risk_gate_json} "
                "--state-json-path exports/m198_market_data_defensive_mode_state.json "
                "--output exports/m198_market_data_defensive_mode.md "
                f"--json-output-path {defensive_mode_json}"
            ),
        },
        {
            "step": 5,
            "reason": "apply_operating_mode_to_auto_gate_before_any_paper_intents",
            "command": (
                f"python main.py market-data-operating-auto-gate --auto-gate-json {auto_gate_json} "
                f"--defensive-mode-json {defensive_mode_json} "
                "--output exports/m198_market_data_operating_auto_gate.md "
                f"--json-output-path {operating_auto_gate_json}"
            ),
        },
    ]


def build_market_data_session_transition_gate_report(
    current_session_overlay_payload: dict[str, Any] | None,
    *,
    previous_state_payload: dict[str, Any] | None = None,
    market_data_risk_gate_payload: dict[str, Any] | None = None,
    market_data_operating_auto_gate_payload: dict[str, Any] | None = None,
    now: str | None = None,
    max_session_overlay_age_seconds: float = 180.0,
    max_risk_gate_age_seconds: float = 180.0,
    max_operating_gate_age_seconds: float = 180.0,
    market_data_json: str = "exports/m153_market_data_desk_refresh.json",
    session_overlay_json: str = "exports/m198_market_data_session_overlay.json",
    risk_gate_json: str = "exports/m198_market_data_risk_gate.json",
    auto_gate_json: str = "exports/m198_market_data_risk_auto_gate.json",
    defensive_mode_json: str = "exports/m198_market_data_defensive_mode.json",
    operating_auto_gate_json: str = "exports/m198_market_data_operating_auto_gate.json",
) -> dict[str, Any]:
    now_dt = _mdstg_parse_dt(now) or datetime.now(UTC)
    generated_at = now_dt.isoformat()
    current = _mdstg_session_context_from_overlay(current_session_overlay_payload or {})
    previous = _mdstg_state_session_context(previous_state_payload)
    risk_gate = _mdstg_risk_gate_context(market_data_risk_gate_payload or {}, now_dt)
    operating_gate = _mdstg_operating_gate_context(market_data_operating_auto_gate_payload or {}, now_dt)
    transition_type = _mdstg_transition_type(previous.get("session_name"), current.get("session_name"))

    session_age = _mdstg_age_seconds(current.get("generated_at"), now_dt)
    session_stale = session_age is None or session_age > float(max_session_overlay_age_seconds)
    risk_gate_stale = (
        not risk_gate["available"]
        or risk_gate["age_seconds"] is None
        or risk_gate["age_seconds"] > float(max_risk_gate_age_seconds)
    )
    operating_gate_stale = (
        not operating_gate["available"]
        or operating_gate["age_seconds"] is None
        or operating_gate["age_seconds"] > float(max_operating_gate_age_seconds)
    )
    gate_session_mismatch = bool(
        risk_gate.get("session_name")
        and current.get("session_name")
        and risk_gate.get("session_name") != current.get("session_name")
    )
    session_transition_detected = transition_type not in {"unchanged", "unknown"}
    rebuild_required = (
        not current["available"]
        or session_stale
        or risk_gate_stale
        or operating_gate_stale
        or gate_session_mismatch
        or session_transition_detected
    )

    decision = "monitor_only"
    reason = "session_context_current_no_transition_detected"
    ordinary_alpha_allowed = False
    max_multiplier = 0.0
    if rebuild_required:
        decision = "rebuild_required"
        reason = "fresh_session_risk_and_operating_gates_required_before_auto_release"
    else:
        gate_status = str(risk_gate.get("gate_status") or "").lower()
        mode = str(operating_gate.get("mode") or "").lower()
        alpha_gate = bool(risk_gate.get("alpha_entries_allowed"))
        operating_alpha = bool(operating_gate.get("ordinary_alpha_entries_allowed"))
        max_multiplier = min(
            _mdstg_float(risk_gate.get("entry_size_multiplier"), 0.0),
            _mdstg_float(operating_gate.get("max_new_entry_size_multiplier"), 0.0),
        )
        ordinary_alpha_allowed = alpha_gate and operating_alpha and max_multiplier > 0.0
        if gate_status in {"block", "defensive"} or mode in {"hedge_defensive_only", "risk_reduction_only"}:
            decision = "hold_defensive"
            reason = "fresh_gate_still_blocks_or_defends_against_market_data_stress"
        elif current.get("is_regular_session") and ordinary_alpha_allowed:
            decision = "release_candidate"
            reason = "fresh_regular_session_gate_allows_alpha_entries"
        elif gate_status == "watch":
            decision = "maintain_haircut"
            reason = "fresh_gate_allows_entries_only_with_size_or_session_haircut"
        else:
            decision = "monitor_only"
            reason = "fresh_gate_does_not_require_transition_action"

    recommended_commands = _mdstg_command_list(
        market_data_json=market_data_json,
        session_overlay_json=session_overlay_json,
        risk_gate_json=risk_gate_json,
        auto_gate_json=auto_gate_json,
        defensive_mode_json=defensive_mode_json,
        operating_auto_gate_json=operating_auto_gate_json,
    )
    if not rebuild_required:
        recommended_commands = []

    severity = "green"
    if decision == "rebuild_required":
        severity = "yellow" if transition_type == "off_hours_to_regular" else "watch"
    if decision == "hold_defensive":
        severity = "red"
    if decision == "maintain_haircut":
        severity = "yellow"

    return {
        "market_data_session_transition_gate_version": MARKET_DATA_SESSION_TRANSITION_GATE_VERSION,
        "generated_at": generated_at,
        "decision": decision,
        "severity": severity,
        "reason": reason,
        "transition": {
            "previous_session": previous.get("session_name"),
            "current_session": current.get("session_name"),
            "transition_type": transition_type,
            "transition_detected": session_transition_detected,
        },
        "current_session_context": current,
        "previous_session_context": previous,
        "artifact_freshness": {
            "session_overlay_age_seconds": session_age,
            "session_overlay_stale": session_stale,
            "max_session_overlay_age_seconds": float(max_session_overlay_age_seconds),
            "risk_gate_age_seconds": risk_gate.get("age_seconds"),
            "risk_gate_stale": risk_gate_stale,
            "max_risk_gate_age_seconds": float(max_risk_gate_age_seconds),
            "operating_gate_age_seconds": operating_gate.get("age_seconds"),
            "operating_gate_stale": operating_gate_stale,
            "max_operating_gate_age_seconds": float(max_operating_gate_age_seconds),
            "gate_session_mismatch": gate_session_mismatch,
        },
        "risk_gate": risk_gate,
        "operating_auto_gate": operating_gate,
        "controls": {
            "rebuild_required_before_auto_release": rebuild_required,
            "ordinary_alpha_entries_allowed_after_transition": bool(ordinary_alpha_allowed and not rebuild_required),
            "hedge_or_defensive_entries_allowed": bool(risk_gate.get("hedge_entries_allowed")),
            "max_new_entry_size_multiplier": 0.0 if rebuild_required else max_multiplier,
            "regular_session_release_requires_fresh_gate": True,
            "after_hours_monitorable_stress_keeps_defensive": True,
        },
        "recommended_commands": recommended_commands,
        "state_update": {
            "market_data_session_transition_gate_version": MARKET_DATA_SESSION_TRANSITION_GATE_VERSION,
            "updated_at": generated_at,
            "last_session_name": current.get("session_name"),
            "last_is_regular_session": current.get("is_regular_session"),
            "last_risk_interpretation": current.get("risk_interpretation"),
            "last_session_overlay_generated_at": current.get("generated_at"),
            "last_risk_gate_generated_at": risk_gate.get("generated_at"),
            "last_operating_gate_generated_at": operating_gate.get("generated_at"),
            "last_decision": decision,
            "last_transition_type": transition_type,
            "last_severity": severity,
        },
        "safety": {
            "read_only_transition_gate": True,
            "paper_only_control_surface": True,
            "ibkr_order_submitted": False,
            "transmit_enabled": False,
            "live_order_routing_enabled": False,
            "portfolio_mutated": False,
        },
    }


def market_data_session_transition_gate_markdown(report: dict[str, Any]) -> str:
    transition = report.get("transition") or {}
    freshness = report.get("artifact_freshness") or {}
    controls = report.get("controls") or {}
    current = report.get("current_session_context") or {}
    risk = report.get("risk_gate") or {}
    operating = report.get("operating_auto_gate") or {}
    lines = [
        "# Market Data Session Transition Gate",
        "",
        f"- Version: `{report.get('market_data_session_transition_gate_version')}`",
        f"- Generated at: `{report.get('generated_at')}`",
        f"- Decision: `{report.get('decision')}`",
        f"- Severity: `{report.get('severity')}`",
        f"- Reason: {report.get('reason')}",
        f"- Transition: `{transition.get('previous_session') or '-'}` → `{transition.get('current_session') or '-'}` / `{transition.get('transition_type')}`",
        f"- Current risk interpretation: `{current.get('risk_interpretation')}`",
        f"- Rebuild required before auto release: `{controls.get('rebuild_required_before_auto_release')}`",
        f"- Ordinary alpha allowed after transition: `{controls.get('ordinary_alpha_entries_allowed_after_transition')}`",
        f"- Max new-entry size multiplier: `{controls.get('max_new_entry_size_multiplier')}`",
        "",
        "## Freshness checks",
        "",
        f"- Session overlay age/stale: `{freshness.get('session_overlay_age_seconds')}` / `{freshness.get('session_overlay_stale')}`",
        f"- Risk gate age/stale: `{freshness.get('risk_gate_age_seconds')}` / `{freshness.get('risk_gate_stale')}`",
        f"- Operating gate age/stale: `{freshness.get('operating_gate_age_seconds')}` / `{freshness.get('operating_gate_stale')}`",
        f"- Gate session mismatch: `{freshness.get('gate_session_mismatch')}`",
        "",
        "## Current gate state",
        "",
        f"- Risk gate: `{risk.get('gate_status')}` / `{risk.get('action')}` / alpha `{risk.get('alpha_entries_allowed')}` / multiplier `{risk.get('entry_size_multiplier')}`",
        f"- Operating gate: `{operating.get('mode')}` / severity `{operating.get('severity')}` / auto-ready `{operating.get('auto_ready_count')}`",
        "",
        "## Recommended rebuild commands",
        "",
    ]
    commands = report.get("recommended_commands") or []
    if commands:
        for item in commands:
            lines.append(f"{item.get('step')}. `{item.get('command')}`")
    else:
        lines.append("- No rebuild command required.")
    lines.extend(["", "## Safety", "", "- Read-only session transition gate; no broker order submission, transmit, or portfolio mutation."])
    return "\n".join(lines) + "\n"


def write_market_data_session_transition_gate_json(output_path: Path, report: dict[str, Any]) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return output_path


def write_market_data_session_transition_gate_markdown(output_path: Path, report: dict[str, Any]) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(market_data_session_transition_gate_markdown(report), encoding="utf-8")
    return output_path


def write_market_data_session_transition_state(output_path: Path, report: dict[str, Any]) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    state = dict(report.get("state_update") or {})
    previous_payload = _mdstg_read_json(output_path)
    history = list(previous_payload.get("history") or []) if previous_payload else []
    if previous_payload:
        history.append(
            {
                "updated_at": previous_payload.get("updated_at"),
                "last_session_name": previous_payload.get("last_session_name"),
                "last_decision": previous_payload.get("last_decision"),
                "last_transition_type": previous_payload.get("last_transition_type"),
                "last_severity": previous_payload.get("last_severity"),
            }
        )
    state["history"] = history[-25:]
    output_path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    return output_path


def build_market_data_session_transition_gate_from_paths(
    *,
    current_session_overlay_path: Path,
    state_path: Path | None = None,
    market_data_risk_gate_path: Path | None = None,
    market_data_operating_auto_gate_path: Path | None = None,
    now: str | None = None,
    max_session_overlay_age_seconds: float = 180.0,
    max_risk_gate_age_seconds: float = 180.0,
    max_operating_gate_age_seconds: float = 180.0,
) -> dict[str, Any]:
    return build_market_data_session_transition_gate_report(
        _mdstg_read_json(current_session_overlay_path),
        previous_state_payload=_mdstg_read_json(state_path),
        market_data_risk_gate_payload=_mdstg_read_json(market_data_risk_gate_path),
        market_data_operating_auto_gate_payload=_mdstg_read_json(market_data_operating_auto_gate_path),
        now=now,
        max_session_overlay_age_seconds=max_session_overlay_age_seconds,
        max_risk_gate_age_seconds=max_risk_gate_age_seconds,
        max_operating_gate_age_seconds=max_operating_gate_age_seconds,
    )


# --- merged from market_data_session_transition_closed_loop.py ---
MARKET_DATA_SESSION_TRANSITION_CLOSED_LOOP_VERSION = "market-data-session-transition-closed-loop-v1"


def _mdstcl_iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _mdstcl_read_json(path: Path | None) -> dict[str, Any]:
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


def _mdstcl_result(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    return payload.get("result") or payload


def _mdstcl_write_json(path: Path, payload: dict[str, Any]) -> Path:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return resolved


def _mdstcl_write_text(path: Path, text: str) -> Path:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(text, encoding="utf-8")
    return resolved


def _mdstcl_step(step_id: str, status: str, *, output_json: Path | None = None, output_markdown: Path | None = None, reason: str | None = None) -> dict[str, Any]:
    return {
        "step_id": step_id,
        "status": status,
        "reason": reason,
        "output_json": str(output_json) if output_json else None,
        "output_markdown": str(output_markdown) if output_markdown else None,
    }


def run_market_data_session_transition_closed_loop(
    *,
    market_data_json_path: Path,
    current_session_overlay_path: Path,
    previous_state_path: Path | None,
    market_data_risk_gate_path: Path | None,
    market_data_operating_auto_gate_path: Path | None,
    preview_json_path: Path,
    short_availability_json_path: Path | None,
    evidence_preflight_json_path: Path | None,
    output_dir: Path,
    execute: bool = True,
    max_session_overlay_age_seconds: float = 180.0,
    max_risk_gate_age_seconds: float = 180.0,
    max_operating_gate_age_seconds: float = 180.0,
    allow_fractional_shares: bool = True,
    allow_short_stock: bool = True,
    require_short_availability_for_shorts: bool = True,
    require_broker_whatif: bool = False,
    max_auto_orders: int = 10,
    min_cash_buffer: float = 50.0,
    allowed_ticket_types: tuple[str, ...] = ("stock",),
    allowed_actions: tuple[str, ...] = ("BUY", "SELL"),
    sample_limit: int = 20,
    now: str | None = None,
) -> dict[str, Any]:
    from recorder.market_data_defensive_mode import (
        build_market_data_defensive_mode_report,
        market_data_defensive_mode_markdown,
    )
    from recorder.market_data_operating_auto_gate import (
        build_market_data_operating_auto_gate_report,
        market_data_operating_auto_gate_markdown,
    )
    from recorder.market_data_risk_gate import (
        build_market_data_risk_gate_report,
        market_data_risk_gate_markdown,
    )
    from recorder.market_data_session_overlay import (
        build_market_data_session_overlay,
        market_data_session_overlay_markdown,
    )
    from recorder.options_analysis import (
        autonomous_execution_gate_markdown,
        build_autonomous_execution_gate_report,
    )

    generated_at = now or _mdstcl_iso_now()
    output_dir = output_dir.expanduser().resolve()
    initial_overlay = _mdstcl_read_json(current_session_overlay_path)
    initial_state = _mdstcl_read_json(previous_state_path)
    initial_risk_gate = _mdstcl_read_json(market_data_risk_gate_path)
    initial_operating_gate = _mdstcl_read_json(market_data_operating_auto_gate_path)
    initial_transition = build_market_data_session_transition_gate_report(
        initial_overlay,
        previous_state_payload=initial_state,
        market_data_risk_gate_payload=initial_risk_gate,
        market_data_operating_auto_gate_payload=initial_operating_gate,
        now=generated_at,
        max_session_overlay_age_seconds=max_session_overlay_age_seconds,
        max_risk_gate_age_seconds=max_risk_gate_age_seconds,
        max_operating_gate_age_seconds=max_operating_gate_age_seconds,
    )

    steps: list[dict[str, Any]] = []
    artifacts: dict[str, str | None] = {}
    if initial_transition.get("decision") != "rebuild_required":
        final_transition = initial_transition
        status = "skipped_no_rebuild_required"
        reason = "initial_transition_gate_does_not_require_rebuild"
    elif not execute:
        final_transition = initial_transition
        status = "dry_run_rebuild_required"
        reason = "dry_run_requested"
    else:
        market_data_payload = _mdstcl_read_json(market_data_json_path)
        overlay = build_market_data_session_overlay(market_data_payload, now=now)
        overlay_json = _mdstcl_write_json(output_dir / "m199_market_data_session_overlay.json", overlay)
        overlay_md = _mdstcl_write_text(output_dir / "m199_market_data_session_overlay.md", market_data_session_overlay_markdown(overlay))
        steps.append(_mdstcl_step("market_data_session_overlay", "succeeded", output_json=overlay_json, output_markdown=overlay_md))

        risk_gate = build_market_data_risk_gate_report(
            market_data_payload,
            market_data_session_overlay_payload=overlay,
        )
        risk_json = _mdstcl_write_json(output_dir / "m199_market_data_risk_gate.json", risk_gate)
        risk_md = _mdstcl_write_text(output_dir / "m199_market_data_risk_gate.md", market_data_risk_gate_markdown(risk_gate))
        steps.append(_mdstcl_step("market_data_risk_gate", "succeeded", output_json=risk_json, output_markdown=risk_md))

        preview_payload = _mdstcl_read_json(preview_json_path)
        short_payload = _mdstcl_read_json(short_availability_json_path)
        evidence_payload = _mdstcl_read_json(evidence_preflight_json_path)
        auto_gate = build_autonomous_execution_gate_report(
            preview_payload,
            allow_fractional_shares=allow_fractional_shares,
            allow_short_stock=allow_short_stock,
            require_short_availability_for_shorts=require_short_availability_for_shorts,
            short_availability_payload=short_payload,
            require_broker_whatif=require_broker_whatif,
            broker_whatif_payload=None,
            max_auto_orders=max_auto_orders,
            min_cash_buffer=min_cash_buffer,
            max_local_initial_margin_total=None,
            allowed_ticket_types=allowed_ticket_types,
            allowed_actions=allowed_actions,
            evidence_preflight_payload=evidence_payload,
            market_data_risk_gate_payload=risk_gate,
            sample_limit=sample_limit,
        )
        auto_filters = {
            "preview_json": str(preview_json_path),
            "short_availability_json": str(short_availability_json_path) if short_availability_json_path else None,
            "evidence_preflight_json": str(evidence_preflight_json_path) if evidence_preflight_json_path else None,
            "market_data_risk_gate_json": str(risk_json),
            "allow_fractional_shares": allow_fractional_shares,
            "allow_short_stock": allow_short_stock,
            "max_auto_orders": max_auto_orders,
            "min_cash_buffer": min_cash_buffer,
            "allowed_ticket_types": ",".join(allowed_ticket_types),
            "allowed_actions": ",".join(allowed_actions),
            "sample_limit": sample_limit,
        }
        auto_payload = {"result": auto_gate, "filters": auto_filters}
        auto_json = _mdstcl_write_json(output_dir / "m199_market_data_risk_auto_gate.json", auto_payload)
        auto_md = _mdstcl_write_text(output_dir / "m199_market_data_risk_auto_gate.md", autonomous_execution_gate_markdown(auto_gate, filters=auto_filters))
        steps.append(_mdstcl_step("replay_score_auto_execution_gate", "succeeded", output_json=auto_json, output_markdown=auto_md))

        defensive = build_market_data_defensive_mode_report(
            risk_gate,
            state_path=output_dir / "m199_market_data_defensive_mode_state.json",
            now=generated_at,
        )
        defensive_json = _mdstcl_write_json(output_dir / "m199_market_data_defensive_mode.json", defensive)
        defensive_md = _mdstcl_write_text(output_dir / "m199_market_data_defensive_mode.md", market_data_defensive_mode_markdown(defensive))
        steps.append(_mdstcl_step("market_data_defensive_mode", "succeeded", output_json=defensive_json, output_markdown=defensive_md))

        operating = build_market_data_operating_auto_gate_report(
            auto_payload,
            defensive,
            sample_limit=sample_limit,
            now=generated_at,
        )
        operating_json = _mdstcl_write_json(output_dir / "m199_market_data_operating_auto_gate.json", operating)
        operating_md = _mdstcl_write_text(
            output_dir / "m199_market_data_operating_auto_gate.md",
            market_data_operating_auto_gate_markdown(operating, filters={"auto_gate_json": str(auto_json), "defensive_mode_json": str(defensive_json)}),
        )
        steps.append(_mdstcl_step("market_data_operating_auto_gate", "succeeded", output_json=operating_json, output_markdown=operating_md))

        final_transition = build_market_data_session_transition_gate_report(
            overlay,
            previous_state_payload=initial_state,
            market_data_risk_gate_payload=risk_gate,
            market_data_operating_auto_gate_payload=operating,
            now=generated_at,
            max_session_overlay_age_seconds=max_session_overlay_age_seconds,
            max_risk_gate_age_seconds=max_risk_gate_age_seconds,
            max_operating_gate_age_seconds=max_operating_gate_age_seconds,
        )
        final_transition_json = write_market_data_session_transition_gate_json(output_dir / "m199_market_data_session_transition_gate_final.json", final_transition)
        final_transition_md = write_market_data_session_transition_gate_markdown(output_dir / "m199_market_data_session_transition_gate_final.md", final_transition)
        steps.append(_mdstcl_step("market_data_session_transition_gate_final", "succeeded", output_json=final_transition_json, output_markdown=final_transition_md))
        if previous_state_path is not None:
            state_path = write_market_data_session_transition_state(previous_state_path, final_transition)
            artifacts["state_json"] = str(state_path)
        artifacts.update(
            {
                "market_data_session_overlay_json": str(overlay_json),
                "market_data_risk_gate_json": str(risk_json),
                "market_data_risk_auto_gate_json": str(auto_json),
                "market_data_defensive_mode_json": str(defensive_json),
                "market_data_operating_auto_gate_json": str(operating_json),
                "market_data_session_transition_gate_final_json": str(final_transition_json),
            }
        )
        status = "rebuild_completed"
        reason = "safe_read_only_rebuild_chain_completed"

    return {
        "market_data_session_transition_closed_loop_version": MARKET_DATA_SESSION_TRANSITION_CLOSED_LOOP_VERSION,
        "generated_at": generated_at,
        "status": status,
        "reason": reason,
        "execute_requested": bool(execute),
        "initial_transition_gate": initial_transition,
        "final_transition_gate": final_transition,
        "step_count": len(steps),
        "steps": steps,
        "artifacts": artifacts,
        "controls": {
            "initial_decision": initial_transition.get("decision"),
            "final_decision": final_transition.get("decision"),
            "final_rebuild_required_before_auto_release": (final_transition.get("controls") or {}).get("rebuild_required_before_auto_release"),
            "final_ordinary_alpha_entries_allowed_after_transition": (final_transition.get("controls") or {}).get("ordinary_alpha_entries_allowed_after_transition"),
            "final_max_new_entry_size_multiplier": (final_transition.get("controls") or {}).get("max_new_entry_size_multiplier"),
        },
        "safety": {
            "read_only_closed_loop": True,
            "paper_only_control_surface": True,
            "uses_internal_builders_not_shell_commands": True,
            "ibkr_order_submitted": False,
            "transmit_enabled": False,
            "live_order_routing_enabled": False,
            "portfolio_mutated": False,
            "scorer_rows_mutated": False,
        },
    }


def market_data_session_transition_closed_loop_markdown(report: dict[str, Any]) -> str:
    controls = report.get("controls") or {}
    final_gate = report.get("final_transition_gate") or {}
    lines = [
        "# Market Data Session Transition Closed Loop",
        "",
        f"- Version: `{report.get('market_data_session_transition_closed_loop_version')}`",
        f"- Generated at: `{report.get('generated_at')}`",
        f"- Status: `{report.get('status')}`",
        f"- Reason: {report.get('reason')}",
        f"- Execute requested: `{report.get('execute_requested')}`",
        f"- Initial decision: `{controls.get('initial_decision')}`",
        f"- Final decision: `{controls.get('final_decision')}`",
        f"- Final rebuild required before auto release: `{controls.get('final_rebuild_required_before_auto_release')}`",
        f"- Final alpha allowed after transition: `{controls.get('final_ordinary_alpha_entries_allowed_after_transition')}`",
        f"- Final max new-entry size multiplier: `{controls.get('final_max_new_entry_size_multiplier')}`",
        "",
        "## Steps",
        "",
        "| Step | Status | JSON | Markdown |",
        "| --- | --- | --- | --- |",
    ]
    for step in report.get("steps") or []:
        lines.append(
            f"| `{step.get('step_id')}` | `{step.get('status')}` | `{step.get('output_json') or '-'}` | `{step.get('output_markdown') or '-'}` |"
        )
    if not report.get("steps"):
        lines.append("| - | - | - | - |")
    lines.extend(
        [
            "",
            "## Final transition gate",
            "",
            market_data_session_transition_gate_markdown(final_gate).strip()
            if final_gate.get("market_data_session_transition_gate_version") == MARKET_DATA_SESSION_TRANSITION_GATE_VERSION
            else "- no final transition gate",
            "",
            "## Safety",
            "",
            "- Read-only/paper-only closed loop; no broker order submission, live routing, transmit, scorer mutation, or portfolio mutation.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_market_data_session_transition_closed_loop_json(output_path: Path, report: dict[str, Any]) -> Path:
    return _mdstcl_write_json(output_path, report)


def write_market_data_session_transition_closed_loop_markdown(output_path: Path, report: dict[str, Any]) -> Path:
    return _mdstcl_write_text(output_path, market_data_session_transition_closed_loop_markdown(report))


# --- merged from session_transition_release_governor.py ---
SESSION_TRANSITION_RELEASE_GOVERNOR_VERSION = "session-transition-release-governor-v1"


def _strg_iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _strg_result(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    result = payload.get("result")
    return result if isinstance(result, dict) else payload


def _strg_read_json(path: Path | None) -> dict[str, Any]:
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


def _strg_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "allow", "allowed", "pass"}:
        return True
    if text in {"0", "false", "no", "n", "block", "blocked", "fail"}:
        return False
    return bool(default)


def _strg_float(value: Any, default: float = 1.0) -> float:
    try:
        number = float(default if value is None or value == "" else value)
    except (TypeError, ValueError):
        number = float(default)
    return max(0.0, min(1.0, number))


def _strg_check(name: str, passed: bool, *, value: Any = None, limit: Any = None, reason: str | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "passed": bool(passed),
        "value": value,
        "limit": limit,
        "reason": reason or "",
    }


def _strg_transition_context(payload: dict[str, Any]) -> dict[str, Any]:
    data = _strg_result(payload)
    controls = data.get("controls") or {}
    transition = data.get("transition") or {}
    return {
        "available": bool(data),
        "version": data.get("market_data_session_transition_gate_version") or data.get("version"),
        "generated_at": data.get("generated_at"),
        "decision": data.get("decision"),
        "severity": data.get("severity"),
        "reason": data.get("reason"),
        "transition_type": transition.get("transition_type"),
        "previous_session": transition.get("previous_session"),
        "current_session": transition.get("current_session"),
        "rebuild_required_before_auto_release": _strg_bool(controls.get("rebuild_required_before_auto_release"), True),
        "ordinary_alpha_entries_allowed_after_transition": _strg_bool(
            controls.get("ordinary_alpha_entries_allowed_after_transition"),
            False,
        ),
        "max_new_entry_size_multiplier": _strg_float(controls.get("max_new_entry_size_multiplier"), 0.0),
    }


def _strg_risk_gate_context(payload: dict[str, Any]) -> dict[str, Any]:
    data = _strg_result(payload)
    return {
        "available": bool(data),
        "version": data.get("market_data_risk_gate_version") or data.get("version"),
        "generated_at": data.get("generated_at"),
        "gate_status": data.get("gate_status"),
        "action": data.get("action"),
        "allowed": _strg_bool(data.get("allowed"), False),
        "new_entries_allowed": _strg_bool(data.get("new_entries_allowed"), False),
        "alpha_entries_allowed": _strg_bool(data.get("alpha_entries_allowed"), False),
        "entry_size_multiplier": _strg_float(data.get("entry_size_multiplier"), 0.0),
        "blocking_reasons": data.get("blocking_reasons") or [],
        "defensive_reasons": data.get("defensive_reasons") or [],
        "haircut_reasons": data.get("haircut_reasons") or [],
    }


def _strg_operating_gate_context(payload: dict[str, Any]) -> dict[str, Any]:
    data = _strg_result(payload)
    mode = data.get("market_data_operating_mode") or {}
    if not isinstance(mode, dict):
        mode = {"mode": mode}
    defensive_mode = data.get("market_data_defensive_mode") or {}
    controls = defensive_mode.get("controls") or {}
    return {
        "available": bool(data),
        "version": data.get("autonomous_execution_gate_version") or data.get("version"),
        "generated_at": data.get("generated_at"),
        "auto_ready_count": int(data.get("auto_ready_count") or 0),
        "mode": mode.get("mode") or defensive_mode.get("mode"),
        "severity": mode.get("severity") or defensive_mode.get("severity"),
        "ordinary_alpha_entries_allowed": _strg_bool(
            mode.get("ordinary_alpha_entries_allowed", controls.get("ordinary_alpha_entries_allowed")),
            False,
        ),
        "max_new_entry_size_multiplier": _strg_float(
            mode.get("max_new_entry_size_multiplier", controls.get("max_new_entry_size_multiplier")),
            0.0,
        ),
        "block_reason_counts": data.get("auto_gate_block_reason_counts") or data.get("block_reason_counts") or {},
        "status_counts": data.get("auto_gate_status_counts") or data.get("status_counts") or {},
    }


def _strg_portfolio_context(payload: dict[str, Any]) -> dict[str, Any]:
    data = _strg_result(payload)
    return {
        "available": bool(data),
        "version": data.get("paper_portfolio_risk_state_version") or data.get("version"),
        "generated_at": data.get("generated_at"),
        "risk_state": data.get("risk_state"),
        "recommended_action": data.get("recommended_action"),
        "new_entries_allowed": _strg_bool(data.get("new_entries_allowed"), True if data else False),
        "breach_count": int(data.get("breach_count") or 0),
        "warning_count": int(data.get("warning_count") or 0),
        "breaches": data.get("breaches") or [],
        "warnings": data.get("warnings") or [],
    }


def _strg_evidence_context(payload: dict[str, Any]) -> dict[str, Any]:
    data = _strg_result(payload)
    return {
        "available": bool(data),
        "version": data.get("evidence_preflight_version") or data.get("version"),
        "generated_at": data.get("generated_at"),
        "gate_status": data.get("gate_status"),
        "allowed": _strg_bool(data.get("allowed"), True if data else False),
        "action": data.get("action"),
        "entry_size_multiplier": _strg_float(data.get("entry_size_multiplier"), 1.0 if data else 0.0),
        "reason": data.get("reason"),
        "warnings": data.get("warnings") or [],
        "freshness": data.get("freshness") or {},
        "auto_recovery": data.get("auto_recovery") or {},
    }


def _strg_final_preflight_context(payload: dict[str, Any]) -> dict[str, Any]:
    data = _strg_result(payload)
    if not data:
        return {"available": False, "blocking_count": 0, "allowed": True, "summary": {}}
    blocked = int(data.get("blocked_count") or data.get("operating_final_preflight_blocked_count") or 0)
    applied = int(data.get("applied_count") or data.get("operating_final_preflight_applied_count") or 0)
    allowed = blocked == 0
    return {
        "available": True,
        "version": data.get("version") or data.get("operating_final_preflight_version"),
        "generated_at": data.get("generated_at"),
        "applied_count": applied,
        "blocking_count": blocked,
        "allowed": allowed,
        "summary": {
            "status_counts": data.get("status_counts") or {},
            "blocked_reasons": data.get("blocked_reasons") or data.get("block_reason_counts") or {},
        },
    }


def build_session_transition_release_governor_report(
    *,
    transition_gate_payload: dict[str, Any] | None,
    market_data_risk_gate_payload: dict[str, Any] | None = None,
    market_data_operating_auto_gate_payload: dict[str, Any] | None = None,
    portfolio_risk_state_payload: dict[str, Any] | None = None,
    evidence_preflight_payload: dict[str, Any] | None = None,
    operating_final_preflight_payload: dict[str, Any] | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """Final read-only release governor for session-transition ordinary alpha release.

    M198/M199 can say the current session-transition chain is a release candidate.
    This governor is deliberately stricter: it requires the transition gate, market
    data risk gate, operating auto gate, portfolio risk state, evidence preflight,
    and optional final preflight all to agree before ordinary paper alpha can be
    released. It writes metadata only; it does not route orders or mutate ledgers.
    """

    generated_at = now or _strg_iso_now()
    transition = _strg_transition_context(transition_gate_payload or {})
    risk_gate = _strg_risk_gate_context(market_data_risk_gate_payload or {})
    operating_gate = _strg_operating_gate_context(market_data_operating_auto_gate_payload or {})
    portfolio = _strg_portfolio_context(portfolio_risk_state_payload or {})
    evidence = _strg_evidence_context(evidence_preflight_payload or {})
    final_preflight = _strg_final_preflight_context(operating_final_preflight_payload or {})
    transition_decision = str(transition.get("decision") or "").lower()
    transition_release_decisions = {"release_candidate", "maintain_haircut"}

    checks: list[dict[str, Any]] = []
    checks.append(
        _strg_check(
            "transition_gate_is_release_candidate",
            transition["available"] and transition_decision in transition_release_decisions,
            value=transition.get("decision"),
            limit="release_candidate_or_maintain_haircut",
            reason="transition_gate_not_release_candidate",
        )
    )
    checks.append(
        _strg_check(
            "transition_gate_rebuild_not_required",
            transition["available"] and not transition["rebuild_required_before_auto_release"],
            value=transition.get("rebuild_required_before_auto_release"),
            limit=False,
            reason="transition_gate_requires_rebuild",
        )
    )
    checks.append(
        _strg_check(
            "transition_gate_alpha_allowed",
            transition["available"] and transition["ordinary_alpha_entries_allowed_after_transition"] and transition["max_new_entry_size_multiplier"] > 0.0,
            value=transition.get("ordinary_alpha_entries_allowed_after_transition"),
            limit=True,
            reason="transition_gate_blocks_alpha_after_transition",
        )
    )
    checks.append(
        _strg_check(
            "market_data_risk_gate_allows_alpha",
            risk_gate["available"]
            and risk_gate["allowed"]
            and risk_gate["new_entries_allowed"]
            and risk_gate["alpha_entries_allowed"]
            and risk_gate["entry_size_multiplier"] > 0.0,
            value={
                "gate_status": risk_gate.get("gate_status"),
                "action": risk_gate.get("action"),
                "entry_size_multiplier": risk_gate.get("entry_size_multiplier"),
            },
            limit="allowed_alpha_entries_with_positive_size",
            reason="market_data_risk_gate_blocks_alpha",
        )
    )
    checks.append(
        _strg_check(
            "operating_auto_gate_has_ready_alpha",
            operating_gate["available"]
            and operating_gate["ordinary_alpha_entries_allowed"]
            and operating_gate["auto_ready_count"] > 0
            and operating_gate["max_new_entry_size_multiplier"] > 0.0,
            value={
                "mode": operating_gate.get("mode"),
                "auto_ready_count": operating_gate.get("auto_ready_count"),
                "max_new_entry_size_multiplier": operating_gate.get("max_new_entry_size_multiplier"),
            },
            limit="ordinary_alpha_allowed_and_auto_ready_count_gt_0",
            reason="operating_auto_gate_blocks_or_has_no_ready_alpha",
        )
    )
    checks.append(
        _strg_check(
            "portfolio_risk_state_allows_new_entries",
            portfolio["available"]
            and portfolio["new_entries_allowed"]
            and str(portfolio.get("risk_state") or "").lower() in {"within_limits", "watch", "monitor", "normal", "ok"},
            value={
                "risk_state": portfolio.get("risk_state"),
                "new_entries_allowed": portfolio.get("new_entries_allowed"),
                "breach_count": portfolio.get("breach_count"),
            },
            limit="within_limits_or_watch_with_new_entries_allowed",
            reason="portfolio_risk_state_blocks_new_entries",
        )
    )
    checks.append(
        _strg_check(
            "evidence_preflight_allows_entries",
            evidence["available"] and evidence["allowed"] and evidence["entry_size_multiplier"] > 0.0,
            value={
                "gate_status": evidence.get("gate_status"),
                "action": evidence.get("action"),
                "entry_size_multiplier": evidence.get("entry_size_multiplier"),
            },
            limit="allowed_with_positive_size",
            reason="evidence_preflight_blocks_entries",
        )
    )
    if final_preflight["available"]:
        checks.append(
            _strg_check(
                "operating_final_preflight_has_no_blocks",
                final_preflight["allowed"],
                value=final_preflight.get("blocking_count"),
                limit=0,
                reason="operating_final_preflight_has_blocks",
            )
        )

    failed_reasons = [item["reason"] for item in checks if not item["passed"] and item.get("reason")]
    release_allowed = not failed_reasons
    size_multiplier = min(
        transition["max_new_entry_size_multiplier"],
        risk_gate["entry_size_multiplier"],
        operating_gate["max_new_entry_size_multiplier"],
        evidence["entry_size_multiplier"],
    )
    if not release_allowed:
        size_multiplier = 0.0

    release_status = "release_allowed"
    severity = "green"
    if not release_allowed:
        release_status = "blocked"
        severity = "red" if "transition_gate_not_release_candidate" not in failed_reasons else "yellow"
    elif size_multiplier < 1.0:
        release_status = "release_allowed_with_haircut"
        severity = "yellow"

    return {
        "session_transition_release_governor_version": SESSION_TRANSITION_RELEASE_GOVERNOR_VERSION,
        "generated_at": generated_at,
        "release_status": release_status,
        "severity": severity,
        "ordinary_alpha_release_allowed": bool(release_allowed),
        "max_new_entry_size_multiplier": size_multiplier,
        "reason": ",".join(failed_reasons) if failed_reasons else "all_release_governor_checks_passed",
        "checks": checks,
        "contexts": {
            "session_transition_gate": transition,
            "market_data_risk_gate": risk_gate,
            "market_data_operating_auto_gate": operating_gate,
            "portfolio_risk_state": portfolio,
            "evidence_preflight": evidence,
            "operating_final_preflight": final_preflight,
        },
        "controls": {
            "ordinary_alpha_release_allowed": bool(release_allowed),
            "max_new_entry_size_multiplier": size_multiplier,
            "release_requires_transition_candidate_or_haircut_candidate": True,
            "release_requires_fresh_market_data_risk_gate": True,
            "release_requires_operating_auto_gate": True,
            "release_requires_portfolio_risk_state": True,
            "release_requires_evidence_preflight": True,
            "release_requires_zero_final_preflight_blocks_when_available": True,
        },
        "safety": {
            "read_only_release_governor": True,
            "paper_only_control_surface": True,
            "ibkr_order_submitted": False,
            "transmit_enabled": False,
            "live_order_routing_enabled": False,
            "portfolio_mutated": False,
            "scorer_rows_mutated": False,
            "execution_ledger_mutated": False,
        },
    }


def build_session_transition_release_governor_from_paths(
    *,
    transition_gate_path: Path,
    market_data_risk_gate_path: Path | None = None,
    market_data_operating_auto_gate_path: Path | None = None,
    portfolio_risk_state_path: Path | None = None,
    evidence_preflight_path: Path | None = None,
    operating_final_preflight_path: Path | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    return build_session_transition_release_governor_report(
        transition_gate_payload=_strg_read_json(transition_gate_path),
        market_data_risk_gate_payload=_strg_read_json(market_data_risk_gate_path),
        market_data_operating_auto_gate_payload=_strg_read_json(market_data_operating_auto_gate_path),
        portfolio_risk_state_payload=_strg_read_json(portfolio_risk_state_path),
        evidence_preflight_payload=_strg_read_json(evidence_preflight_path),
        operating_final_preflight_payload=_strg_read_json(operating_final_preflight_path),
        now=now,
    )


def session_transition_release_governor_markdown(report: dict[str, Any]) -> str:
    contexts = report.get("contexts") or {}
    transition = contexts.get("session_transition_gate") or {}
    risk = contexts.get("market_data_risk_gate") or {}
    operating = contexts.get("market_data_operating_auto_gate") or {}
    portfolio = contexts.get("portfolio_risk_state") or {}
    evidence = contexts.get("evidence_preflight") or {}
    lines = [
        "# Session Transition Release Governor",
        "",
        f"- Version: `{report.get('session_transition_release_governor_version')}`",
        f"- Generated at: `{report.get('generated_at')}`",
        f"- Release status: `{report.get('release_status')}`",
        f"- Severity: `{report.get('severity')}`",
        f"- Ordinary alpha release allowed: `{report.get('ordinary_alpha_release_allowed')}`",
        f"- Max new-entry size multiplier: `{report.get('max_new_entry_size_multiplier')}`",
        f"- Reason: `{report.get('reason') or '-'}`",
        "",
        "## Gate context",
        "",
        f"- Transition gate: `{transition.get('decision')}` severity `{transition.get('severity')}` size `{transition.get('max_new_entry_size_multiplier')}`",
        f"- Market data risk gate: `{risk.get('gate_status')}` action `{risk.get('action')}` alpha `{risk.get('alpha_entries_allowed')}` size `{risk.get('entry_size_multiplier')}`",
        f"- Operating auto gate: `{operating.get('mode')}` auto-ready `{operating.get('auto_ready_count')}` alpha `{operating.get('ordinary_alpha_entries_allowed')}` size `{operating.get('max_new_entry_size_multiplier')}`",
        f"- Portfolio risk state: `{portfolio.get('risk_state')}` new entries `{portfolio.get('new_entries_allowed')}` breaches `{portfolio.get('breach_count')}`",
        f"- Evidence preflight: `{evidence.get('gate_status')}` action `{evidence.get('action')}` size `{evidence.get('entry_size_multiplier')}`",
        "",
        "## Checks",
        "",
    ]
    for item in report.get("checks") or []:
        icon = "PASS" if item.get("passed") else "BLOCK"
        lines.append(
            f"- {icon} `{item.get('name')}` value `{item.get('value')}` limit `{item.get('limit')}`"
            + (f" reason `{item.get('reason')}`" if item.get("reason") and not item.get("passed") else "")
        )
    lines.extend(
        [
            "",
            "## Safety",
            "",
            "- Read-only/paper metadata gate only; no IBKR order submission, transmit, scorer mutation, execution-ledger mutation, or real portfolio mutation.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_session_transition_release_governor_json(output_path: Path, report: dict[str, Any]) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return output_path


def write_session_transition_release_governor_markdown(output_path: Path, report: dict[str, Any]) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(session_transition_release_governor_markdown(report), encoding="utf-8")
    return output_path


# --- merged from release_governed_recovery_closed_loop.py ---
RELEASE_GOVERNED_RECOVERY_CLOSED_LOOP_VERSION = "release-governed-recovery-closed-loop-v1"


def _rgrcl_iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _rgrcl_read_json(path: Path | None) -> dict[str, Any]:
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


def _rgrcl_result(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    result = payload.get("result")
    return result if isinstance(result, dict) else payload


def _rgrcl_write_json(path: Path, payload: dict[str, Any]) -> Path:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return resolved


def _rgrcl_write_text(path: Path, text: str) -> Path:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(text, encoding="utf-8")
    return resolved


def _rgrcl_plan_from_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    plan = snapshot.get("buy_sell_plan")
    return plan if isinstance(plan, dict) else {}


def _rgrcl_count_pending(payload: dict[str, Any]) -> int:
    data = _rgrcl_result(payload)
    if "pending_paper_execution_count" in data:
        try:
            return int(data.get("pending_paper_execution_count") or 0)
        except (TypeError, ValueError):
            return 0
    return sum(1 for item in data.get("intents") or [] if str(item.get("state") or "") == "pending_paper_execution")


def _rgrcl_count_release_blocks(payload: dict[str, Any]) -> int:
    data = _rgrcl_result(payload)
    if "blocked_by_session_transition_release_governor_count" in data:
        try:
            return int(data.get("blocked_by_session_transition_release_governor_count") or 0)
        except (TypeError, ValueError):
            return 0
    return sum(1 for item in data.get("intents") or [] if str(item.get("state") or "") == "blocked_by_session_transition_release_governor")


def _rgrcl_count_fills(payload: dict[str, Any]) -> int:
    data = _rgrcl_result(payload)
    if "fill_count" in data:
        try:
            return int(data.get("fill_count") or 0)
        except (TypeError, ValueError):
            return 0
    return len(data.get("fills") or [])


def _rgrcl_step(step_id: str, status: str, *, output_json: Path | None = None, output_markdown: Path | None = None, reason: str | None = None) -> dict[str, Any]:
    return {
        "step_id": step_id,
        "status": status,
        "reason": reason,
        "output_json": str(output_json) if output_json else None,
        "output_markdown": str(output_markdown) if output_markdown else None,
    }


def run_release_governed_recovery_closed_loop(
    *,
    transition_gate_path: Path,
    market_data_risk_gate_path: Path | None,
    market_data_operating_auto_gate_path: Path,
    portfolio_risk_state_path: Path | None,
    evidence_preflight_path: Path | None,
    operating_final_preflight_path: Path | None = None,
    snapshot_path: Path | None = None,
    existing_ledger_path: Path | None = None,
    existing_router_path: Path | None = None,
    marks_path: Path | None = None,
    release_governor_payload: dict[str, Any] | None = None,
    release_governor_json_output_path: Path | None = None,
    release_governor_markdown_output_path: Path | None = None,
    ledger_json_output_path: Path | None = None,
    ledger_markdown_output_path: Path | None = None,
    router_json_output_path: Path | None = None,
    router_markdown_output_path: Path | None = None,
    monitor_size_multiplier: float = 0.50,
    max_intents: int = 10,
    max_orders: int = 10,
    sample_limit: int = 20,
    require_operating_preflight: bool = True,
    allow_non_operating_source_ledger: bool = False,
    require_portfolio_risk_gate: bool = True,
    require_session_transition_release_governor: bool = True,
    fill_price_source: str = "local_estimate",
    now: str | None = None,
) -> dict[str, Any]:
    generated_at = now or _rgrcl_iso_now()
    steps: list[dict[str, Any]] = []

    snapshot = _rgrcl_read_json(snapshot_path)
    before_plan = _rgrcl_plan_from_snapshot(snapshot)
    before_ledger = _rgrcl_read_json(existing_ledger_path)
    before_router = _rgrcl_read_json(existing_router_path)

    release_governor = _rgrcl_result(release_governor_payload)
    release_step_status = "reused" if release_governor else "succeeded"
    if not release_governor:
        release_governor = build_session_transition_release_governor_from_paths(
            transition_gate_path=transition_gate_path,
            market_data_risk_gate_path=market_data_risk_gate_path,
            market_data_operating_auto_gate_path=market_data_operating_auto_gate_path,
            portfolio_risk_state_path=portfolio_risk_state_path,
            evidence_preflight_path=evidence_preflight_path,
            operating_final_preflight_path=operating_final_preflight_path,
            now=generated_at,
        )
    release_json = _rgrcl_write_json(release_governor_json_output_path, release_governor) if release_governor_json_output_path else None
    release_md = _rgrcl_write_text(release_governor_markdown_output_path, session_transition_release_governor_markdown(release_governor)) if release_governor_markdown_output_path else None
    steps.append(_rgrcl_step("refresh_session_transition_release_governor", release_step_status, output_json=release_json, output_markdown=release_md))

    auto_gate_payload = _rgrcl_read_json(market_data_operating_auto_gate_path)
    portfolio_payload = _rgrcl_read_json(portfolio_risk_state_path)
    ledger = build_execution_intent_ledger_report(
        auto_gate_payload,
        existing_ledger_payload=None,
        portfolio_risk_state_payload=portfolio_payload,
        session_transition_release_governor_payload=release_governor,
        monitor_size_multiplier=monitor_size_multiplier,
        max_intents=max_intents,
        ledger_run_id=f"m206_release_governed_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}",
        sample_limit=sample_limit,
    )
    ledger_json = _rgrcl_write_json(ledger_json_output_path, ledger) if ledger_json_output_path else None
    ledger_md = _rgrcl_write_text(
        ledger_markdown_output_path,
        execution_intent_ledger_markdown(
            ledger,
            filters={
                "auto_gate_json": str(market_data_operating_auto_gate_path),
                "portfolio_risk_state_json": str(portfolio_risk_state_path) if portfolio_risk_state_path else None,
                "session_transition_release_governor_json": str(release_governor_json_output_path) if release_governor_json_output_path else None,
                "max_intents": max_intents,
                "sample_limit": sample_limit,
            },
        ),
    ) if ledger_markdown_output_path else None
    steps.append(_rgrcl_step("rebuild_release_governed_execution_intent_ledger", "succeeded", output_json=ledger_json, output_markdown=ledger_md))

    router = build_paper_order_router_report(
        ledger,
        existing_router_payload=None,
        marks_payload=_rgrcl_read_json(marks_path),
        operating_auto_gate_payload=auto_gate_payload,
        session_transition_release_governor_payload=release_governor,
        require_operating_preflight=require_operating_preflight,
        allow_non_operating_source_ledger=allow_non_operating_source_ledger,
        require_session_transition_release_governor=require_session_transition_release_governor,
        fill_price_source=fill_price_source,
        require_portfolio_risk_gate=require_portfolio_risk_gate,
        max_orders=max_orders,
        router_run_id=f"m206_release_governed_router_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}",
        sample_limit=sample_limit,
    )
    router_json = _rgrcl_write_json(router_json_output_path, router) if router_json_output_path else None
    router_md = _rgrcl_write_text(
        router_markdown_output_path,
        paper_order_router_markdown(
            router,
            filters={
                "intent_ledger_json": str(ledger_json_output_path) if ledger_json_output_path else "in_memory_m206_ledger",
                "operating_auto_gate_json": str(market_data_operating_auto_gate_path),
                "session_transition_release_governor_json": str(release_governor_json_output_path) if release_governor_json_output_path else None,
                "require_operating_preflight": require_operating_preflight,
                "require_session_transition_release_governor": require_session_transition_release_governor,
                "require_portfolio_risk_gate": require_portfolio_risk_gate,
                "max_orders": max_orders,
            },
        ),
    ) if router_markdown_output_path else None
    steps.append(_rgrcl_step("rebuild_release_governed_paper_order_router", "succeeded", output_json=router_json, output_markdown=router_md))

    before_pending = int(before_plan.get("pending_paper_execution_count") or _rgrcl_count_pending(before_ledger) or 0)
    rebuilt_pending = _rgrcl_count_pending(ledger)
    rebuilt_release_blocks = _rgrcl_count_release_blocks(ledger)
    router_fill_count = _rgrcl_count_fills(router)
    release_allowed = bool(release_governor.get("ordinary_alpha_release_allowed"))
    release_configured = bool(release_governor.get("session_transition_release_governor_version"))
    stale_pending_cleared = rebuilt_pending == 0 if before_pending else True
    release_governed_path_active = release_configured and (
        rebuilt_release_blocks > 0 or release_allowed or str(release_governor.get("release_status") or "") in {"blocked", "haircut", "allowed"}
    )
    no_forbidden_fills = router_fill_count == 0 if not release_allowed else True
    outcome = "pass" if stale_pending_cleared and release_governed_path_active and no_forbidden_fills else "review"
    post_validation = {
        "outcome": outcome,
        "before_dashboard_pending_paper_execution_count": before_pending,
        "before_existing_ledger_pending_paper_execution_count": _rgrcl_count_pending(before_ledger),
        "before_existing_router_fill_count": _rgrcl_count_fills(before_router),
        "rebuilt_ledger_pending_paper_execution_count": rebuilt_pending,
        "rebuilt_ledger_release_governor_block_count": rebuilt_release_blocks,
        "rebuilt_router_fill_count": router_fill_count,
        "release_governor_status": release_governor.get("release_status"),
        "release_governor_allowed": release_allowed,
        "release_governor_size_multiplier": release_governor.get("max_new_entry_size_multiplier"),
        "stale_pending_cleared": stale_pending_cleared,
        "release_governed_path_active": release_governed_path_active,
        "no_forbidden_fills_when_release_blocked": no_forbidden_fills,
    }
    steps.append(_rgrcl_step("post_validate_release_governed_path", outcome, reason="release_governed_path_active" if outcome == "pass" else "manual_review_required"))

    return {
        "release_governed_recovery_closed_loop_version": RELEASE_GOVERNED_RECOVERY_CLOSED_LOOP_VERSION,
        "generated_at": generated_at,
        "status": outcome,
        "reason": "release_governed_recovery_path_validated" if outcome == "pass" else "release_governed_recovery_path_needs_review",
        "step_count": len(steps),
        "steps": steps,
        "artifacts": {
            "session_transition_release_governor_json": str(release_json) if release_json else None,
            "session_transition_release_governor_markdown": str(release_md) if release_md else None,
            "release_governed_execution_intent_ledger_json": str(ledger_json) if ledger_json else None,
            "release_governed_execution_intent_ledger_markdown": str(ledger_md) if ledger_md else None,
            "release_governed_paper_order_router_json": str(router_json) if router_json else None,
            "release_governed_paper_order_router_markdown": str(router_md) if router_md else None,
        },
        "post_validation": post_validation,
        "release_governor": {
            "version": release_governor.get("session_transition_release_governor_version"),
            "status": release_governor.get("release_status"),
            "allowed": release_allowed,
            "size_multiplier": release_governor.get("max_new_entry_size_multiplier"),
            "failed_check_count": release_governor.get("failed_check_count")
            if release_governor.get("failed_check_count") is not None
            else sum(1 for item in release_governor.get("checks") or [] if isinstance(item, dict) and not item.get("passed")),
            "reason": release_governor.get("reason"),
        },
        "ledger_summary": {
            "version": ledger.get("execution_intent_ledger_version"),
            "intent_count": ledger.get("intent_count"),
            "pending_paper_execution_count": ledger.get("pending_paper_execution_count"),
            "blocked_by_session_transition_release_governor_count": ledger.get("blocked_by_session_transition_release_governor_count"),
            "action_counts": ledger.get("action_counts") or {},
        },
        "router_summary": {
            "version": router.get("paper_order_router_version"),
            "paper_order_count": router.get("paper_order_count"),
            "fill_count": _rgrcl_count_fills(router),
            "skipped_counts": router.get("skipped_counts") or {},
            "session_transition_release_governor_applied": router.get("session_transition_release_governor_applied"),
            "session_transition_release_governor_allowed": router.get("session_transition_release_governor_allowed"),
            "session_transition_release_governor_action": router.get("session_transition_release_governor_action"),
        },
        "safety": {
            "read_only_closed_loop": True,
            "paper_only_control_surface": True,
            "uses_internal_builders_not_shell_commands": True,
            "ibkr_order_submitted": False,
            "transmit_enabled": False,
            "live_order_routing_enabled": False,
            "portfolio_mutated": False,
            "scorer_rows_mutated": False,
        },
    }


def release_governed_recovery_closed_loop_markdown(report: dict[str, Any]) -> str:
    post = report.get("post_validation") or {}
    release = report.get("release_governor") or {}
    ledger = report.get("ledger_summary") or {}
    router = report.get("router_summary") or {}
    lines = [
        "# Release-Governed Recovery Closed Loop",
        "",
        f"- Version: `{report.get('release_governed_recovery_closed_loop_version')}`",
        f"- Generated at: `{report.get('generated_at')}`",
        f"- Status: `{report.get('status')}`",
        f"- Reason: `{report.get('reason')}`",
        f"- Release governor: `{release.get('status')}` allowed `{release.get('allowed')}` size `{release.get('size_multiplier')}` failed `{release.get('failed_check_count')}`",
        f"- Rebuilt ledger pending: `{ledger.get('pending_paper_execution_count')}`",
        f"- Rebuilt ledger M204 release blocks: `{ledger.get('blocked_by_session_transition_release_governor_count')}`",
        f"- Rebuilt router fills: `{router.get('fill_count')}`",
        f"- Stale pending cleared: `{post.get('stale_pending_cleared')}`",
        f"- Release-governed path active: `{post.get('release_governed_path_active')}`",
        "",
        "## Steps",
        "",
        "| Step | Status | Reason | JSON | Markdown |",
        "| --- | --- | --- | --- | --- |",
    ]
    for step in report.get("steps") or []:
        lines.append(
            f"| `{step.get('step_id')}` | `{step.get('status')}` | {step.get('reason') or '-'} | `{step.get('output_json') or '-'}` | `{step.get('output_markdown') or '-'}` |"
        )
    if not report.get("steps"):
        lines.append("| - | - | - | - | - |")
    lines.extend(
        [
            "",
            "## Post validation",
            "",
            f"- Before dashboard pending: `{post.get('before_dashboard_pending_paper_execution_count')}`",
            f"- Before existing-ledger pending: `{post.get('before_existing_ledger_pending_paper_execution_count')}`",
            f"- Rebuilt pending: `{post.get('rebuilt_ledger_pending_paper_execution_count')}`",
            f"- Rebuilt release blocks: `{post.get('rebuilt_ledger_release_governor_block_count')}`",
            f"- Router fills: `{post.get('rebuilt_router_fill_count')}`",
            f"- No forbidden fills while release blocked: `{post.get('no_forbidden_fills_when_release_blocked')}`",
            "",
            "## Safety",
            "",
            "- Read-only/paper-only closed loop. It rebuilds metadata reports only; no IBKR order submission, transmit, live routing, scorer mutation, or portfolio mutation.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_release_governed_recovery_closed_loop_json(output_path: Path, report: dict[str, Any]) -> Path:
    return _rgrcl_write_json(output_path, report)


def write_release_governed_recovery_closed_loop_markdown(output_path: Path, report: dict[str, Any]) -> Path:
    return _rgrcl_write_text(output_path, release_governed_recovery_closed_loop_markdown(report))
