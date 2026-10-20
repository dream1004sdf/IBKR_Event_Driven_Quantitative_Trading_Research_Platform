from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import json


OPERATING_FINAL_PREFLIGHT_VERSION = "operating-final-preflight-v1"
EXPECTED_OPERATING_AUTO_GATE_VERSION = "market-data-operating-auto-gate-v1"


def _result(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    result = payload.get("result")
    return result if isinstance(result, dict) else payload


def _pending_intents(ledger: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        item
        for item in ledger.get("intents") or []
        if str(item.get("state") or "") == "pending_paper_execution"
    ]


def _operating_controls(operating_gate: dict[str, Any]) -> dict[str, Any]:
    defensive_mode = operating_gate.get("market_data_defensive_mode") or {}
    return defensive_mode.get("controls") or {}


def _operating_mode(operating_gate: dict[str, Any]) -> str | None:
    raw = operating_gate.get("market_data_operating_mode")
    if isinstance(raw, dict):
        return raw.get("mode") or (operating_gate.get("market_data_defensive_mode") or {}).get("mode")
    return raw or (operating_gate.get("market_data_defensive_mode") or {}).get("mode")


def build_operating_final_preflight_report(
    intent_ledger_payload: dict[str, Any],
    operating_auto_gate_payload: dict[str, Any] | None = None,
    *,
    require_operating_gate: bool = True,
    allow_non_operating_source_ledger: bool = False,
    sample_limit: int = 20,
    now: str | None = None,
) -> dict[str, Any]:
    ledger = _result(intent_ledger_payload)
    operating_gate = _result(operating_auto_gate_payload)
    pending_intents = _pending_intents(ledger)
    controls = _operating_controls(operating_gate)
    protective_packet = operating_gate.get("protective_analysis_packet") or {}
    reasons: list[str] = []
    warnings: list[str] = []

    source_gate_version = ledger.get("source_autonomous_execution_gate_version")
    operating_gate_version = operating_gate.get("autonomous_execution_gate_version")
    operating_mode = _operating_mode(operating_gate)
    operating_auto_ready_count = int(operating_gate.get("auto_ready_count") or 0) if operating_gate else None
    pending_count = len(pending_intents)

    if require_operating_gate and not operating_gate:
        reasons.append("missing_required_operating_auto_gate")
    if operating_gate:
        if operating_gate_version != EXPECTED_OPERATING_AUTO_GATE_VERSION:
            reasons.append("unexpected_operating_auto_gate_version")
        if (
            source_gate_version != operating_gate_version
            and not allow_non_operating_source_ledger
            and pending_count > 0
        ):
            reasons.append("intent_ledger_not_derived_from_operating_auto_gate")
        if pending_count > (operating_auto_ready_count or 0):
            reasons.append("pending_intents_exceed_operating_auto_ready_count")
        if operating_auto_ready_count == 0 and pending_count > 0:
            reasons.append("operating_auto_gate_has_no_auto_ready_intents")
        if operating_mode == "risk_reduction_only" and pending_count > 0:
            reasons.append("market_data_operating_mode_risk_reduction_only")
        if controls.get("ordinary_alpha_entries_allowed") is False and pending_count > 0:
            reasons.append("operating_gate_blocks_ordinary_alpha_entries")
        if controls.get("hedge_or_defensive_entries_allowed") is False and pending_count > 0:
            warnings.append("operating_gate_blocks_hedge_or_defensive_new_entries")
        if controls.get("protective_option_ticketing_allowed") is False:
            warnings.append("protective_option_ticketing_disabled")
    elif not require_operating_gate:
        warnings.append("operating_auto_gate_not_supplied")

    reason_counts = Counter(reasons)
    allowed = not reasons
    action = "allow_router_execution" if allowed else "block_router_execution"
    if not pending_intents and not reasons:
        action = "allow_no_pending_intents"

    sample_intents = [
        {
            "intent_id": item.get("intent_id"),
            "symbol": item.get("symbol"),
            "action": item.get("action"),
            "quantity": item.get("quantity"),
            "state": item.get("state"),
        }
        for item in pending_intents[: max(0, int(sample_limit))]
    ]
    report = {
        "operating_final_preflight_version": OPERATING_FINAL_PREFLIGHT_VERSION,
        "generated_at": now or datetime.now(UTC).isoformat(),
        "allowed": allowed,
        "action": action,
        "reasons": reasons,
        "reason_counts": dict(reason_counts),
        "warnings": warnings,
        "require_operating_gate": bool(require_operating_gate),
        "allow_non_operating_source_ledger": bool(allow_non_operating_source_ledger),
        "source_execution_intent_ledger_version": ledger.get("execution_intent_ledger_version"),
        "source_intent_count": ledger.get("intent_count"),
        "source_pending_paper_execution_count": ledger.get("pending_paper_execution_count"),
        "actual_pending_paper_execution_count": pending_count,
        "source_autonomous_execution_gate_version": source_gate_version,
        "operating_auto_gate_version": operating_gate_version,
        "operating_mode": operating_mode,
        "operating_auto_ready_count": operating_auto_ready_count,
        "operating_source_preview_item_count": operating_gate.get("source_preview_item_count") if operating_gate else None,
        "operating_status_counts": operating_gate.get("auto_gate_status_counts") or {},
        "operating_block_reason_counts": operating_gate.get("auto_gate_block_reason_counts") or {},
        "operating_controls": {
            "ordinary_alpha_entries_allowed": controls.get("ordinary_alpha_entries_allowed"),
            "hedge_or_defensive_entries_allowed": controls.get("hedge_or_defensive_entries_allowed"),
            "exit_or_risk_reduction_allowed": controls.get("exit_or_risk_reduction_allowed"),
            "protective_option_analysis_allowed": controls.get("protective_option_analysis_allowed"),
            "protective_option_ticketing_allowed": controls.get("protective_option_ticketing_allowed"),
            "max_new_entry_size_multiplier": controls.get("max_new_entry_size_multiplier"),
        },
        "protective_analysis_packet": {
            "candidate_count": protective_packet.get("candidate_count"),
            "ticketing_allowed": protective_packet.get("ticketing_allowed"),
            "allowed_option_structures": protective_packet.get("allowed_option_structures") or [],
        },
        "sample_pending_intents": sample_intents,
        "safety": {
            "mode": "read_only_operating_final_preflight",
            "policy": "Validates a pending paper/live execution ledger against the latest M182 operating auto-gate before any router consumes it.",
            "ibkr_order_submitted": False,
            "transmit_enabled": False,
            "live_order_routing_enabled": False,
            "portfolio_mutated": False,
        },
    }
    return report


def operating_final_preflight_markdown(
    report: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
    title: str = "Operating Final Preflight Report",
) -> str:
    filters = filters or {}
    controls = report.get("operating_controls") or {}
    protective = report.get("protective_analysis_packet") or {}
    lines = [
        f"# {title}",
        "",
        f"- Version: `{report.get('operating_final_preflight_version')}`",
        f"- Generated at: `{report.get('generated_at')}`",
        f"- Allowed: `{report.get('allowed')}`",
        f"- Action: `{report.get('action')}`",
        f"- Reasons: `{report.get('reasons') or []}`",
        f"- Warnings: `{report.get('warnings') or []}`",
        f"- Source ledger: `{report.get('source_execution_intent_ledger_version')}` from gate `{report.get('source_autonomous_execution_gate_version')}`",
        f"- Operating gate: `{report.get('operating_auto_gate_version')}` mode `{report.get('operating_mode')}` auto-ready `{report.get('operating_auto_ready_count')}`",
        f"- Pending intents: `{report.get('actual_pending_paper_execution_count')}`",
        f"- Protective candidates / ticketing: `{protective.get('candidate_count')}` / `{protective.get('ticketing_allowed')}`",
        f"- Alpha / hedge / exit permissions: `{controls.get('ordinary_alpha_entries_allowed')}` / `{controls.get('hedge_or_defensive_entries_allowed')}` / `{controls.get('exit_or_risk_reduction_allowed')}`",
        f"- Max new-entry size multiplier: `{controls.get('max_new_entry_size_multiplier')}`",
        "- Safety: read-only final preflight; no broker order submission, transmit, scorer mutation, or real portfolio mutation.",
        "",
        "## Inputs",
        "",
    ]
    if filters:
        for key, value in filters.items():
            lines.append(f"- `{key}`: `{value if value is not None else '-'}`")
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "## Reason counts",
            "",
            "| Reason | Count |",
            "| --- | ---: |",
        ]
    )
    reason_counts = report.get("reason_counts") or {}
    if reason_counts:
        for reason, count in sorted(reason_counts.items()):
            lines.append(f"| `{reason}` | {count} |")
    else:
        lines.append("| `none` | 0 |")
    lines.extend(
        [
            "",
            "## Sample pending intents",
            "",
            "| Intent | Symbol | Action | Qty | State |",
            "| --- | --- | --- | ---: | --- |",
        ]
    )
    for item in report.get("sample_pending_intents") or []:
        lines.append(
            f"| `{item.get('intent_id')}` | {item.get('symbol')} | {item.get('action')} | {item.get('quantity')} | {item.get('state')} |"
        )
    if not report.get("sample_pending_intents"):
        lines.append("| - | - | - | - | - |")
    return "\n".join(lines) + "\n"


def write_operating_final_preflight_json(output_path: Path, report: dict[str, Any]) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return output_path


def write_operating_final_preflight_markdown(
    output_path: Path,
    report: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(operating_final_preflight_markdown(report, filters=filters), encoding="utf-8")
    return output_path
