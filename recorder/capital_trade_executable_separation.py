from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from recorder.execution_readiness_safety import paper_read_only_safety_block


CAPITAL_TRADE_EXECUTABLE_SEPARATION_VERSION = "m245-capital-feasible-vs-trade-executable-summary-v2-gate-attribution"
GATE_READY_KEYS = {
    "capital_capacity": ("capital_capacity_ready", "capital_current_size_feasible"),
    "capital_action": ("capital_action_gate_ready", "capital_gate_ready"),
    "source": ("source_gate_ready",),
    "market": ("market_gate_ready",),
    "session": ("session_gate_ready",),
    "cash_only": ("cash_only_gate_ready", "cash_only_ready"),
    "broker": ("broker_gate_ready",),
    "micro_policy": ("micro_policy_gate_ready", "micro_policy_ready"),
}
LEGACY_GATE_ALIASES = {
    "capital": "capital_capacity",
}


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _result(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    result = payload.get("result")
    return result if isinstance(result, dict) else payload


def build_capital_trade_executable_separation_report(
    live_read_only_capital_validation_payload: dict[str, Any] | None,
    *,
    sample_limit: int = 20,
    now: str | None = None,
) -> dict[str, Any]:
    source = _result(live_read_only_capital_validation_payload)
    generated_at = now or _iso_now()
    if not source:
        return {
            "capital_trade_executable_separation_version": CAPITAL_TRADE_EXECUTABLE_SEPARATION_VERSION,
            "generated_at": generated_at,
            "status": "red",
            "source": {"available": False, "source_generated_at": None, "source_status": None},
            "summary": {
                "capital_feasible_current_size_count": 0,
                "capital_resize_required_count": 0,
                "capital_blocked_count": 0,
                "trade_executable_current_size_count": 0,
                "trade_executable_after_resize_count": 0,
                "not_trade_executable_count": 0,
                "capital_feasible_but_not_trade_executable_count": 0,
            },
            "gate_counts": {},
            "execution_block_reason_counts": {},
            "sample_items": [],
            "interpretation": "M245 could not summarize M243 because the live read-only validation artifact is missing.",
            "safety": paper_read_only_safety_block(),
        }

    validation_items = [row for row in source.get("validation_items") or [] if isinstance(row, dict)]
    feasible_but_not_trade_executable: list[dict[str, Any]] = []
    execution_status_counts: Counter[str] = Counter()
    gate_counts: Counter[str] = Counter()
    block_reason_counts: Counter[str] = Counter()
    sample_items: list[dict[str, Any]] = []
    for row in validation_items:
        execution_gate = row.get("execution_gate") if isinstance(row.get("execution_gate"), dict) else {}
        blocked_by = execution_gate.get("blocked_by") if isinstance(execution_gate.get("blocked_by"), dict) else {}
        execution_status = str(execution_gate.get("status") or "unknown")
        execution_status_counts[execution_status] += 1
        if row.get("current_size_feasible") and not execution_gate.get("trade_executable"):
            feasible_but_not_trade_executable.append(row)
        for reason in execution_gate.get("execution_block_reasons") or []:
            block_reason_counts[str(reason)] += 1
        for gate_name, ready_keys in GATE_READY_KEYS.items():
            if blocked_by.get(gate_name) is True or any(execution_gate.get(key) is False for key in ready_keys):
                gate_counts[gate_name] += 1
        for legacy_name, split_name in LEGACY_GATE_ALIASES.items():
            if gate_counts.get(split_name) and blocked_by.get(split_name) is True:
                gate_counts[legacy_name] += 1
        if len(sample_items) < max(0, int(sample_limit)):
            blocked_gates = [
                gate
                for gate, ready_keys in GATE_READY_KEYS.items()
                if blocked_by.get(gate) is True or any(execution_gate.get(key) is False for key in ready_keys)
            ]
            sample_items.append(
                {
                    "symbol": row.get("symbol"),
                    "action": row.get("action"),
                    "ticket_type": row.get("ticket_type"),
                    "capital_status": row.get("status"),
                    "current_size_feasible": row.get("current_size_feasible"),
                    "resize_possible": row.get("resize_possible"),
                    "trade_executable_status": execution_status,
                    "trade_executable": execution_gate.get("trade_executable"),
                    "trade_executable_after_resize": execution_gate.get("trade_executable_after_resize"),
                    "blocked_gates": blocked_gates,
                    "reasons": row.get("reasons") or [],
                    "execution_block_reasons": execution_gate.get("execution_block_reasons") or [],
                }
            )

    status = "green"
    if source.get("status") == "red":
        status = "red"
    elif feasible_but_not_trade_executable or source.get("status") == "yellow":
        status = "yellow"
    return {
        "capital_trade_executable_separation_version": CAPITAL_TRADE_EXECUTABLE_SEPARATION_VERSION,
        "generated_at": generated_at,
        "status": status,
        "source": {
            "available": True,
            "source_version": source.get("live_read_only_capital_validation_version"),
            "source_trade_executable_split_version": source.get("trade_executable_split_version"),
            "source_generated_at": source.get("generated_at"),
            "source_status": source.get("status"),
        },
        "summary": {
            "capital_feasible_current_size_count": int(source.get("current_size_feasible_count") or 0),
            "capital_resize_required_count": int(source.get("resize_required_count") or 0),
            "capital_blocked_count": int(source.get("blocked_count") or 0),
            "trade_executable_current_size_count": int(source.get("trade_executable_current_size_count") or 0),
            "trade_executable_after_resize_count": int(source.get("trade_executable_after_resize_count") or 0),
            "not_trade_executable_count": int(source.get("not_trade_executable_count") or 0),
            "capital_feasible_but_not_trade_executable_count": len(feasible_but_not_trade_executable),
            "execution_status_counts": dict(execution_status_counts),
        },
        "gate_counts": dict(gate_counts),
        "gate_count_interpretation": {
            "capital_capacity": "Actual account capacity blocks the current size.",
            "capital_action": "The upstream capital-aware action gate is not ready, often because source or market gates are not ready.",
            "legacy_capital": "Backward-compatible alias for capital_capacity only.",
        },
        "execution_block_reason_counts": dict(block_reason_counts),
        "sample_items": sample_items,
        "interpretation": (
            "Capital-feasible means the account can carry the size. Trade-executable additionally requires source, "
            "market, session, cash-only, broker, micro-policy, and capital gates to be ready."
        ),
        "safety": paper_read_only_safety_block(),
    }


def capital_trade_executable_separation_markdown(report: dict[str, Any]) -> str:
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    lines = [
        "# M245 Capital Feasible vs Trade Executable",
        "",
        f"- Generated: `{report.get('generated_at')}`",
        f"- Status: `{report.get('status')}`",
        f"- Source M243: `{(report.get('source') or {}).get('source_status')}` at `{(report.get('source') or {}).get('source_generated_at')}`",
        f"- Capital feasible / resize / blocked: `{summary.get('capital_feasible_current_size_count')}` / `{summary.get('capital_resize_required_count')}` / `{summary.get('capital_blocked_count')}`",
        f"- Trade executable current / after resize / not executable: `{summary.get('trade_executable_current_size_count')}` / `{summary.get('trade_executable_after_resize_count')}` / `{summary.get('not_trade_executable_count')}`",
        f"- Capital-feasible but not trade-executable: `{summary.get('capital_feasible_but_not_trade_executable_count')}`",
        f"- Blocked gate counts: `{report.get('gate_counts') or {}}`",
        "- Gate attribution: `capital_capacity` means the account cannot carry the size; `capital_action` means the upstream action gate is not ready even if capacity exists.",
        "- Safety: read-only summary only; no broker order submission, no transmit, no live routing.",
        "",
        "## Sample items",
        "",
        "| Symbol | Action | Capital feasible | Trade status | Blocked gates | Reasons |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in report.get("sample_items") or []:
        lines.append(
            f"| `{row.get('symbol')}` | `{row.get('action')}` | `{row.get('current_size_feasible')}` | "
            f"`{row.get('trade_executable_status')}` | `{row.get('blocked_gates') or []}` | "
            f"`{row.get('execution_block_reasons') or row.get('reasons') or []}` |"
        )
    if not report.get("sample_items"):
        lines.append("| - | - | - | - | - | No M243 validation items. |")
    return "\n".join(lines) + "\n"


def write_capital_trade_executable_separation_json(path: Path, report: dict[str, Any]) -> Path:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return resolved


def write_capital_trade_executable_separation_markdown(path: Path, report: dict[str, Any]) -> Path:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(capital_trade_executable_separation_markdown(report), encoding="utf-8")
    return resolved
