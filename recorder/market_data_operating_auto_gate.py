from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


MARKET_DATA_OPERATING_AUTO_GATE_VERSION = "market-data-operating-auto-gate-v1"


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


def _payload_result(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not payload:
        return {}
    return payload.get("result") or payload


def _clamp_multiplier(value: Any, default: float = 1.0) -> float:
    try:
        number = float(default if value is None or value == "" else value)
    except (TypeError, ValueError):
        number = float(default)
    return max(0.0, min(1.0, number))


def _is_ready(item: dict[str, Any]) -> bool:
    return item.get("auto_execution_status") == "auto_execution_ready"


def _is_risk_reduction_or_hedge(item: dict[str, Any]) -> bool:
    source = item.get("source_preview_item") or {}
    ticket = source.get("ticket_draft") or {}
    role_fields = [
        item.get("strategy_role"),
        item.get("intent_role"),
        item.get("risk_role"),
        source.get("strategy_role"),
        source.get("intent_role"),
        source.get("risk_role"),
        ticket.get("strategy_role"),
        ticket.get("intent_role"),
        ticket.get("risk_role"),
    ]
    roles = {str(value or "").lower() for value in role_fields if value}
    if roles & {"hedge", "defensive", "risk_reduction", "exit", "protective"}:
        return True
    action = str(item.get("action") or "").upper()
    # Existing M113 items are entry intents. Treat plain BUY/SELL_SHORT as alpha,
    # and only allow explicit close/reduce labels through this M182 overlay.
    return action in {"CLOSE", "BUY_TO_COVER", "SELL_TO_CLOSE", "REDUCE"}


def _check(name: str, passed: bool, *, value: Any = None, limit: Any = None) -> dict[str, Any]:
    return {"name": name, "passed": bool(passed), "value": value, "limit": limit}


def _protective_candidate(item: dict[str, Any], defensive_mode: dict[str, Any]) -> dict[str, Any]:
    controls = defensive_mode.get("controls") or {}
    source = item.get("source_preview_item") or {}
    return {
        "symbol": item.get("symbol"),
        "source_action": item.get("action"),
        "source_ticket_type": item.get("ticket_type"),
        "source_auto_execution_status": item.get("auto_execution_status"),
        "analysis_status": "analysis_only" if controls.get("protective_option_analysis_allowed") else "blocked",
        "ticketing_allowed": bool(controls.get("protective_option_ticketing_allowed")),
        "allowed_option_structures": controls.get("allowed_option_structures") or [],
        "reason": "market_data_defensive_mode",
        "source_score_id": source.get("source_score_id"),
        "source_outcome_id": source.get("source_outcome_id"),
        "safety": {
            "analysis_only": not bool(controls.get("protective_option_ticketing_allowed")),
            "ticket_created": False,
            "ibkr_order_submitted": False,
            "transmit_enabled": False,
        },
    }


def build_market_data_operating_auto_gate_report(
    auto_gate_payload: dict[str, Any],
    defensive_mode_payload: dict[str, Any],
    *,
    sample_limit: int = 20,
    now: str | None = None,
) -> dict[str, Any]:
    source_gate = _payload_result(auto_gate_payload)
    defensive_mode = _payload_result(defensive_mode_payload)
    controls = defensive_mode.get("controls") or {}
    overlay = defensive_mode.get("scorer_overlay") or {}
    source_market_data_risk_gate = source_gate.get("market_data_risk_gate") or {}
    market_data_session_context = source_market_data_risk_gate.get("market_data_session_context") or {}
    session_adjusted_priority = bool(source_market_data_risk_gate.get("session_adjusted_priority"))
    ordinary_allowed = bool(controls.get("ordinary_alpha_entries_allowed", True))
    hedge_allowed = bool(controls.get("hedge_or_defensive_entries_allowed", True))
    max_multiplier = _clamp_multiplier(controls.get("max_new_entry_size_multiplier"), 1.0)
    protective_analysis_allowed = bool(controls.get("protective_option_analysis_allowed"))
    protective_candidates: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []
    ready_items: list[dict[str, Any]] = []
    status_counts: Counter[str] = Counter()
    reason_counts: Counter[str] = Counter()

    for raw in source_gate.get("auto_execution_items") or []:
        item = json.loads(json.dumps(raw))
        original_status = item.get("auto_execution_status")
        checks = list(item.get("checks") or [])
        reasons = list(item.get("block_reasons") or [])
        role_allowed = hedge_allowed if _is_risk_reduction_or_hedge(item) else ordinary_allowed
        operating_multiplier = max_multiplier if role_allowed else 0.0
        existing_market_multiplier = _clamp_multiplier(item.get("market_data_entry_size_multiplier"), 1.0)
        combined_market_multiplier = min(existing_market_multiplier, operating_multiplier)
        check_passed = role_allowed and combined_market_multiplier > 0.0
        checks.append(
            _check(
                "market_data_defensive_mode_allows_intent_role",
                check_passed,
                value=defensive_mode.get("mode"),
                limit="ordinary_alpha_or_explicit_hedge_allowed",
            )
        )
        if _is_ready(item) and not check_passed:
            item["auto_execution_status"] = "auto_execution_blocked"
            if not role_allowed:
                reasons.append("market_data_defensive_mode_blocks_ordinary_alpha_entries")
            else:
                reasons.append("market_data_defensive_mode_zero_size_multiplier")
        elif _is_ready(item):
            item["market_data_entry_size_multiplier"] = combined_market_multiplier
            item["market_data_defensive_mode_size_multiplier"] = operating_multiplier
            ready_items.append(item)
        item["checks"] = checks
        item["block_reasons"] = reasons
        item["source_auto_execution_status"] = original_status
        item["market_data_defensive_mode"] = defensive_mode
        item["market_data_session_context"] = market_data_session_context
        item["market_data_session_adjusted_priority"] = session_adjusted_priority
        item["market_data_defensive_mode_action"] = defensive_mode.get("mode")
        item["market_data_defensive_mode_severity"] = defensive_mode.get("severity")
        item["market_data_defensive_mode_size_multiplier"] = operating_multiplier
        item["protective_option_analysis_candidate"] = bool(protective_analysis_allowed and not check_passed)
        if item["protective_option_analysis_candidate"]:
            protective_candidates.append(_protective_candidate(item, defensive_mode))
        items.append(item)
        status_counts[str(item.get("auto_execution_status") or "unknown")] += 1
        for reason in reasons:
            reason_counts[str(reason)] += 1

    result = json.loads(json.dumps(source_gate))
    result["autonomous_execution_gate_version"] = MARKET_DATA_OPERATING_AUTO_GATE_VERSION
    result["source_autonomous_execution_gate_version"] = source_gate.get("autonomous_execution_gate_version")
    result["generated_at"] = now or _iso_now()
    result["auto_ready_count"] = len(ready_items)
    result["auto_gate_status_counts"] = dict(status_counts)
    result["auto_gate_block_reason_counts"] = dict(reason_counts)
    result["auto_execution_items"] = items
    result["sample_auto_execution_items"] = items[: max(0, int(sample_limit))]
    result["market_data_defensive_mode"] = defensive_mode
    result["market_data_session_context"] = market_data_session_context
    result["market_data_session_adjusted_priority"] = session_adjusted_priority
    result["market_data_operating_mode"] = {
        "mode": defensive_mode.get("mode"),
        "severity": defensive_mode.get("severity"),
        "ordinary_alpha_entries_allowed": ordinary_allowed,
        "hedge_or_defensive_entries_allowed": hedge_allowed,
        "protective_option_analysis_allowed": protective_analysis_allowed,
        "protective_option_ticketing_allowed": bool(controls.get("protective_option_ticketing_allowed")),
        "max_new_entry_size_multiplier": max_multiplier,
        "scorer_overlay": overlay,
        "market_data_session_context": market_data_session_context,
        "session_adjusted_priority": session_adjusted_priority,
    }
    result["protective_analysis_packet"] = {
        "packet_version": "market-data-protective-analysis-packet-v1",
        "generated_at": result["generated_at"],
        "mode": defensive_mode.get("mode"),
        "severity": defensive_mode.get("severity"),
        "candidate_count": len(protective_candidates),
        "ticketing_allowed": bool(controls.get("protective_option_ticketing_allowed")),
        "candidates": protective_candidates[: max(0, int(sample_limit))],
        "safety": {
            "analysis_only": not bool(controls.get("protective_option_ticketing_allowed")),
            "ticket_created": False,
            "ibkr_order_submitted": False,
            "transmit_enabled": False,
        },
    }
    params = dict(result.get("gate_parameters") or {})
    params["market_data_defensive_mode_configured"] = True
    params["market_data_defensive_mode"] = defensive_mode.get("mode")
    params["market_data_defensive_mode_severity"] = defensive_mode.get("severity")
    params["market_data_defensive_mode_size_multiplier"] = max_multiplier
    params["market_data_session_priority_class"] = market_data_session_context.get("session_priority_class")
    params["market_data_session_adjusted_priority"] = session_adjusted_priority
    result["gate_parameters"] = params
    result["safety"] = {
        **(result.get("safety") or {}),
        "mode": "paper_autonomous_execution_gate_with_market_data_defensive_mode",
        "market_data_defensive_mode_applied": True,
        "market_data_session_context_applied": bool(market_data_session_context),
        "protective_option_packet_analysis_only": not bool(controls.get("protective_option_ticketing_allowed")),
        "ibkr_order_submitted": False,
        "transmit_enabled": False,
        "live_order_routing_enabled": False,
    }
    return result


def build_market_data_operating_auto_gate_from_paths(
    *,
    auto_gate_path: Path,
    defensive_mode_path: Path,
    sample_limit: int = 20,
    now: str | None = None,
) -> dict[str, Any]:
    return build_market_data_operating_auto_gate_report(
        _read_json(auto_gate_path),
        _read_json(defensive_mode_path),
        sample_limit=sample_limit,
        now=now,
    )


def market_data_operating_auto_gate_markdown(report: dict[str, Any], *, filters: dict[str, Any] | None = None) -> str:
    mode = report.get("market_data_operating_mode") or {}
    packet = report.get("protective_analysis_packet") or {}
    session = report.get("market_data_session_context") or mode.get("market_data_session_context") or {}
    lines = [
        "# Market Data Operating-Mode Auto Gate",
        "",
        f"- Version: `{report.get('autonomous_execution_gate_version')}`",
        f"- Source gate: `{report.get('source_autonomous_execution_gate_version')}`",
        f"- Generated at: `{report.get('generated_at')}`",
        f"- Auto-ready count: `{report.get('auto_ready_count')}`",
        f"- Mode: `{mode.get('mode')}` severity `{mode.get('severity')}`",
        f"- Ordinary alpha allowed: `{mode.get('ordinary_alpha_entries_allowed')}`",
        f"- Hedge/defensive allowed: `{mode.get('hedge_or_defensive_entries_allowed')}`",
        f"- Max new-entry size multiplier: `{mode.get('max_new_entry_size_multiplier')}`",
        f"- Market session priority class: `{session.get('session_priority_class') or '-'}` adjusted `{mode.get('session_adjusted_priority')}`",
        f"- Protective analysis candidates: `{packet.get('candidate_count')}` ticketing `{packet.get('ticketing_allowed')}`",
        "- Safety: paper auto-gate metadata only; no IBKR order submission or transmit.",
        "",
        "## Inputs",
        "",
    ]
    for key, value in (filters or {}).items():
        lines.append(f"- `{key}`: `{value if value is not None else '-'}`")
    if not filters:
        lines.append("- none")
    lines.extend(["", "## Status counts", "", "| Bucket | Count |", "| --- | ---: |"])
    for key, value in sorted((report.get("auto_gate_status_counts") or {}).items(), key=lambda item: (-int(item[1]), item[0])):
        lines.append(f"| {key} | {value} |")
    if not report.get("auto_gate_status_counts"):
        lines.append("| - | 0 |")
    lines.extend(["", "## Block reasons", "", "| Reason | Count |", "| --- | ---: |"])
    for key, value in sorted((report.get("auto_gate_block_reason_counts") or {}).items(), key=lambda item: (-int(item[1]), item[0])):
        lines.append(f"| {key} | {value} |")
    if not report.get("auto_gate_block_reason_counts"):
        lines.append("| - | 0 |")
    lines.extend(["", "## Sample auto gate items", "", "| Symbol | Status | Action | Qty | Defensive mode | Reasons |", "| --- | --- | --- | ---: | --- | --- |"])
    for item in report.get("sample_auto_execution_items") or []:
        lines.append(
            f"| {item.get('symbol')} | {item.get('auto_execution_status')} | {item.get('action')} | {item.get('quantity')} | "
            f"{item.get('market_data_defensive_mode_action')} | {', '.join(item.get('block_reasons') or []) or '-'} |"
        )
    lines.extend(["", "## Protective analysis packet", "", "| Symbol | Source action | Status | Ticketing | Structures |", "| --- | --- | --- | --- | --- |"])
    for item in packet.get("candidates") or []:
        lines.append(
            f"| {item.get('symbol')} | {item.get('source_action')} | {item.get('analysis_status')} | {item.get('ticketing_allowed')} | "
            f"{', '.join(item.get('allowed_option_structures') or []) or '-'} |"
        )
    if not packet.get("candidates"):
        lines.append("| - | - | - | - | - |")
    return "\n".join(lines) + "\n"


def write_market_data_operating_auto_gate_json(output_path: Path, report: dict[str, Any]) -> Path:
    resolved = output_path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return resolved


def write_market_data_operating_auto_gate_markdown(output_path: Path, report: dict[str, Any], *, filters: dict[str, Any] | None = None) -> Path:
    resolved = output_path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(market_data_operating_auto_gate_markdown(report, filters=filters), encoding="utf-8")
    return resolved
