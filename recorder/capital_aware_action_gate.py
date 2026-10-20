from __future__ import annotations

import json
import math
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


CAPITAL_AWARE_ACTION_GATE_VERSION = "m239-capital-aware-action-sizing-gate-v1"


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _payload_result(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not payload:
        return {}
    result = payload.get("result")
    return result if isinstance(result, dict) else payload


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, dict):
        for key in ("numeric_value", "value", "amount"):
            parsed = _as_float(value.get(key))
            if parsed is not None:
                return parsed
        return None
    try:
        parsed = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed):
        return None
    return parsed


def _fmt_money(value: Any) -> str:
    parsed = _as_float(value)
    if parsed is None:
        return "n/a"
    return f"${parsed:,.2f}"


def _deepcopy(payload: Any) -> Any:
    return json.loads(json.dumps(payload, ensure_ascii=False))


def _check(name: str, passed: bool, *, value: Any = None, limit: Any = None, severity: str = "hard") -> dict[str, Any]:
    return {
        "name": name,
        "passed": bool(passed),
        "value": value,
        "limit": limit,
        "severity": severity,
    }


def _reason_from_failed_check(name: str) -> str:
    return {
        "source_auto_gate_ready": "source_auto_gate_not_ready",
        "capital_state_allows_new_entries": "capital_state_blocks_new_entries",
        "market_data_defensive_mode_allows_intent_role": "market_data_defensive_mode_blocks_intent_role",
        "max_auto_orders_not_exceeded": "source_max_auto_orders_exceeded",
    }.get(name, name)


def _clamp_multiplier(value: float) -> float:
    if not math.isfinite(value):
        return 0.0
    return max(0.0, min(1.0, value))


def _round_down_to_increment(value: float, increment: float) -> float:
    if increment <= 0:
        return max(0.0, value)
    return max(0.0, math.floor((value + 1e-12) / increment) * increment)


def _item_ticket(item: dict[str, Any]) -> dict[str, Any]:
    source = item.get("source_preview_item") if isinstance(item.get("source_preview_item"), dict) else {}
    ticket = source.get("ticket_draft") if isinstance(source.get("ticket_draft"), dict) else {}
    if ticket:
        return ticket
    intent = item.get("order_intent") if isinstance(item.get("order_intent"), dict) else {}
    order = intent.get("order_preview") if isinstance(intent.get("order_preview"), dict) else {}
    return order if order else {}


def _item_order(item: dict[str, Any]) -> dict[str, Any]:
    intent = item.get("order_intent") if isinstance(item.get("order_intent"), dict) else {}
    order = intent.get("order_preview") if isinstance(intent.get("order_preview"), dict) else {}
    if order:
        return order
    source = item.get("source_preview_item") if isinstance(item.get("source_preview_item"), dict) else {}
    order = source.get("ibkr_what_if_order_preview") if isinstance(source.get("ibkr_what_if_order_preview"), dict) else {}
    return order if order else {}


def _item_local_margin(item: dict[str, Any]) -> dict[str, Any]:
    margin = item.get("local_margin_estimate") if isinstance(item.get("local_margin_estimate"), dict) else {}
    if margin:
        return margin
    source = item.get("source_preview_item") if isinstance(item.get("source_preview_item"), dict) else {}
    margin = source.get("local_margin_estimate") if isinstance(source.get("local_margin_estimate"), dict) else {}
    return margin if margin else {}


def _item_quantity(item: dict[str, Any]) -> float | None:
    order = _item_order(item)
    ticket = _item_ticket(item)
    return _as_float(item.get("quantity")) or _as_float(order.get("total_quantity")) or _as_float(ticket.get("quantity"))


def _item_action(item: dict[str, Any]) -> str:
    order = _item_order(item)
    ticket = _item_ticket(item)
    return str(
        item.get("action")
        or order.get("source_order_action")
        or ticket.get("order_action")
        or order.get("action")
        or ""
    ).upper()


def _item_ticket_type(item: dict[str, Any]) -> str:
    ticket = _item_ticket(item)
    return str(item.get("ticket_type") or ticket.get("ticket_type") or "").lower()


def _is_option_ticket(ticket_type: str) -> bool:
    return "option" in ticket_type


def _is_stock_ticket(ticket_type: str) -> bool:
    return ticket_type in {"stock", "stk", "equity", "etf"}


def _is_source_ready(item: dict[str, Any]) -> bool:
    return item.get("auto_execution_status") == "auto_execution_ready"


def _is_fractional(value: float | None) -> bool:
    if value is None:
        return False
    return abs(value - round(value)) > 1e-9


def _ticket_notional(item: dict[str, Any], margin: dict[str, Any]) -> float:
    ticket = _item_ticket(item)
    for key in ("estimated_notional", "notional", "gross_notional", "sized_gross_notional"):
        value = _as_float(ticket.get(key))
        if value is not None:
            return max(0.0, value)
    init_margin = _as_float(margin.get("estimated_initial_margin_change"))
    return max(0.0, init_margin or 0.0)


def _risk_required(item: dict[str, Any], margin: dict[str, Any]) -> float:
    ticket = _item_ticket(item)
    for key in ("estimated_max_loss", "max_loss", "sized_trade_risk", "estimated_stop_loss", "stop_loss_proxy"):
        value = _as_float(ticket.get(key))
        if value is not None:
            return max(0.0, value)
    for key in ("estimated_max_loss_proxy", "estimated_initial_margin_change"):
        value = _as_float(margin.get(key))
        if value is not None:
            return max(0.0, value)
    return 0.0


def _capital_budget(capital_state: dict[str, Any], key: str) -> float:
    budgets = capital_state.get("budgets") if isinstance(capital_state.get("budgets"), dict) else {}
    return max(0.0, _as_float(budgets.get(key)) or 0.0)


def _global_capital_allows_entries(capital_state: dict[str, Any]) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    status = str(capital_state.get("status") or "unknown")
    withdrawal_guard = capital_state.get("withdrawal_guard") if isinstance(capital_state.get("withdrawal_guard"), dict) else {}
    if status == "red":
        reasons.append("capital_state_red_blocks_new_entries")
    if withdrawal_guard.get("requires_fresh_account_snapshot"):
        reasons.append("capital_state_snapshot_not_fresh")
    if "stale_account_snapshot" in set(capital_state.get("status_reasons") or []):
        reasons.append("capital_state_stale_snapshot")
    return not reasons, reasons


def _scale_requirements(requirements: dict[str, float], scale: float) -> dict[str, float]:
    return {key: round(max(0.0, value * scale), 6) for key, value in requirements.items()}


def _apply_quantity_scale(item: dict[str, Any], scale: float, *, allow_fractional_shares: bool, min_stock_quantity: float, stock_quantity_increment: float) -> tuple[dict[str, Any], float, list[str]]:
    quantity = _item_quantity(item)
    ticket_type = _item_ticket_type(item)
    reasons: list[str] = []
    if quantity is None or quantity <= 0:
        return item, 0.0, ["missing_or_non_positive_quantity"]
    raw_quantity = quantity * _clamp_multiplier(scale)
    if _is_option_ticket(ticket_type):
        adjusted_quantity = math.floor(raw_quantity + 1e-12)
        if adjusted_quantity < 1:
            return item, 0.0, ["option_contract_cannot_be_fractionally_resized"]
    elif _is_stock_ticket(ticket_type):
        source_fractional = _is_fractional(quantity)
        if allow_fractional_shares or source_fractional:
            adjusted_quantity = _round_down_to_increment(raw_quantity, stock_quantity_increment)
        else:
            adjusted_quantity = math.floor(raw_quantity + 1e-12)
        if adjusted_quantity < min_stock_quantity:
            return item, 0.0, ["resized_stock_quantity_below_minimum"]
    else:
        return item, 0.0, [f"unsupported_ticket_type_for_capital_resize:{ticket_type or 'unknown'}"]
    if adjusted_quantity <= 0:
        return item, 0.0, ["resized_quantity_zero"]
    effective_scale = adjusted_quantity / quantity
    adjusted = _deepcopy(item)
    adjusted["source_quantity"] = quantity
    adjusted["quantity"] = round(adjusted_quantity, 6)
    adjusted["capital_adjusted_quantity"] = round(adjusted_quantity, 6)
    adjusted["capital_quantity_scale"] = round(effective_scale, 6)
    ticket = _item_ticket(adjusted)
    if ticket:
        ticket["source_quantity"] = ticket.get("quantity")
        ticket["quantity"] = round(adjusted_quantity, 6)
        for key in ("estimated_notional", "notional", "gross_notional", "estimated_max_loss", "max_loss", "sized_trade_risk"):
            value = _as_float(ticket.get(key))
            if value is not None:
                ticket[key] = round(value * effective_scale, 6)
    order = _item_order(adjusted)
    if order:
        order["source_total_quantity"] = order.get("total_quantity")
        order["total_quantity"] = round(adjusted_quantity, 6)
    source = adjusted.get("source_preview_item") if isinstance(adjusted.get("source_preview_item"), dict) else {}
    if isinstance(source.get("ticket_draft"), dict):
        source["ticket_draft"] = ticket
    if isinstance(source.get("ibkr_what_if_order_preview"), dict):
        source["ibkr_what_if_order_preview"]["source_total_quantity"] = source["ibkr_what_if_order_preview"].get("total_quantity")
        source["ibkr_what_if_order_preview"]["total_quantity"] = round(adjusted_quantity, 6)
    margin = _item_local_margin(adjusted)
    if margin:
        for key in (
            "estimated_cash_impact",
            "estimated_initial_margin_change",
            "estimated_maintenance_margin_change",
            "estimated_max_loss_proxy",
        ):
            value = _as_float(margin.get(key))
            if value is not None:
                margin[key] = round(value * effective_scale, 6)
        adjusted["local_margin_estimate"] = margin
        if isinstance(source.get("local_margin_estimate"), dict):
            source["local_margin_estimate"] = margin
    return adjusted, effective_scale, reasons


def _requirements_for_item(item: dict[str, Any]) -> dict[str, float]:
    margin = _item_local_margin(item)
    ticket_type = _item_ticket_type(item)
    cash_required = max(0.0, -(_as_float(margin.get("estimated_cash_impact")) or 0.0))
    initial_margin = max(0.0, _as_float(margin.get("estimated_initial_margin_change")) or 0.0)
    risk = _risk_required(item, margin)
    notional = _ticket_notional(item, margin)
    return {
        "cash_required": cash_required,
        "initial_margin_required": initial_margin,
        "risk_required": risk,
        "stock_notional_required": notional if _is_stock_ticket(ticket_type) else 0.0,
        "option_premium_required": max(cash_required, risk, initial_margin) if _is_option_ticket(ticket_type) else 0.0,
    }


def _fit_scale(requirements: dict[str, float], remaining: dict[str, float], ticket_type: str) -> tuple[float, list[dict[str, Any]]]:
    checks: list[dict[str, Any]] = []
    scale = 1.0
    mapping = [
        ("cash_budget", requirements["cash_required"], remaining["cash_budget"]),
        ("new_initial_margin_budget", requirements["initial_margin_required"], remaining["new_initial_margin_budget"]),
        ("core_risk_budget", requirements["risk_required"], remaining["core_risk_budget"]),
    ]
    if _is_stock_ticket(ticket_type):
        mapping.append(("single_position_notional_budget", requirements["stock_notional_required"], remaining["single_position_notional_budget"]))
    if _is_option_ticket(ticket_type):
        mapping.append(("options_premium_budget", requirements["option_premium_required"], remaining["options_premium_budget"]))
    for name, required, budget in mapping:
        passed = required <= budget + 1e-9
        checks.append(_check(f"{name}_available", passed, value=round(required, 6), limit=round(budget, 6)))
        if required > 0:
            scale = min(scale, budget / required)
    return _clamp_multiplier(scale), checks


def build_capital_aware_action_gate_report(
    auto_gate_payload: dict[str, Any],
    capital_state_payload: dict[str, Any],
    *,
    reduce_to_fit_capacity: bool = True,
    allow_fractional_shares: bool = False,
    min_stock_quantity: float = 0.01,
    stock_quantity_increment: float = 0.01,
    max_ready_actions: int = 10,
    sample_limit: int = 20,
    now: str | None = None,
) -> dict[str, Any]:
    source_gate = _payload_result(auto_gate_payload)
    capital_state = _payload_result(capital_state_payload)
    capital_allows, global_reasons = _global_capital_allows_entries(capital_state)
    leverage_mode = str(capital_state.get("leverage_mode") or "unknown")
    initial_remaining = {
        "cash_budget": _capital_budget(capital_state, "deployable_cash_after_reserve"),
        "new_initial_margin_budget": _capital_budget(capital_state, "max_new_initial_margin"),
        "core_risk_budget": _capital_budget(capital_state, "core_risk_budget"),
        "single_position_notional_budget": _capital_budget(capital_state, "max_single_position_notional"),
        "options_premium_budget": _capital_budget(capital_state, "options_premium_budget"),
    }
    remaining = dict(initial_remaining)
    used = {key: 0.0 for key in initial_remaining}
    items: list[dict[str, Any]] = []
    ready_items: list[dict[str, Any]] = []
    status_counts: Counter[str] = Counter()
    reason_counts: Counter[str] = Counter()
    resized_count = 0
    for raw in source_gate.get("auto_execution_items") or []:
        original = _deepcopy(raw)
        item = _deepcopy(raw)
        ticket_type = _item_ticket_type(item)
        action = _item_action(item)
        checks = list(item.get("checks") or [])
        reasons = [_reason_from_failed_check(str(reason)) for reason in (item.get("block_reasons") or [])]
        requirements = _requirements_for_item(item)
        item["capital_requirements"] = requirements
        item["capital_remaining_before"] = {key: round(value, 6) for key, value in remaining.items()}
        source_ready = _is_source_ready(item)
        checks.append(_check("source_auto_gate_ready", source_ready, value=item.get("auto_execution_status"), limit="auto_execution_ready"))
        checks.append(_check("capital_state_allows_new_entries", capital_allows, value=capital_state.get("status"), limit="green_and_fresh"))
        if leverage_mode == "cash_only" and action == "SELL_SHORT":
            checks.append(_check("cash_only_blocks_short_stock", False, value=leverage_mode, limit="no_short_stock"))
        if leverage_mode == "restricted_margin":
            checks.append(_check("restricted_margin_blocks_new_entries", False, value=leverage_mode, limit="non_restricted"))
        scale, budget_checks = _fit_scale(requirements, remaining, ticket_type)
        checks.extend(budget_checks)
        failed = [check for check in checks if not check.get("passed")]
        for reason in global_reasons:
            if reason not in reasons:
                reasons.append(reason)
        for check in failed:
            reason = _reason_from_failed_check(str(check.get("name")))
            if reason not in reasons:
                reasons.append(reason)
        adjusted_scale = 1.0
        if failed and source_ready and capital_allows and reduce_to_fit_capacity and scale > 0.0:
            adjustable_failures = {
                "cash_budget_available",
                "new_initial_margin_budget_available",
                "core_risk_budget_available",
                "single_position_notional_budget_available",
                "options_premium_budget_available",
            }
            non_adjustable = [check for check in failed if str(check.get("name")) not in adjustable_failures]
            if not non_adjustable:
                adjusted_item, adjusted_scale, resize_reasons = _apply_quantity_scale(
                    item,
                    scale,
                    allow_fractional_shares=allow_fractional_shares,
                    min_stock_quantity=min_stock_quantity,
                    stock_quantity_increment=stock_quantity_increment,
                )
                if adjusted_scale > 0 and not resize_reasons:
                    item = adjusted_item
                    requirements = _scale_requirements(requirements, adjusted_scale)
                    item["capital_requirements"] = requirements
                    checks.append(
                        _check(
                            "capital_resize_to_fit_applied",
                            True,
                            value=round(adjusted_scale, 6),
                            limit="<=1.0",
                            severity="info",
                        )
                    )
                    failed = []
                    reasons = [reason for reason in reasons if reason not in {_reason_from_failed_check(name) for name in adjustable_failures}]
                    resized_count += 1
                else:
                    for reason in resize_reasons:
                        if reason not in reasons:
                            reasons.append(reason)
                    checks.append(_check("capital_resize_to_fit_applied", False, value=resize_reasons, limit="positive_resized_quantity"))
                    failed = [check for check in checks if not check.get("passed")]
        if not failed and len(ready_items) >= int(max_ready_actions):
            checks.append(_check("max_capital_ready_actions_not_exceeded", False, value=len(ready_items) + 1, limit=max_ready_actions))
            reasons.append("max_capital_ready_actions_not_exceeded")
            failed = [check for check in checks if not check.get("passed")]
        if failed or not source_ready:
            status = "capital_blocked_source_gate" if not source_ready else "capital_blocked"
            block_category = "capital_action" if not source_ready else "capital_capacity"
            status_display = "capital_action_blocked_source_gate" if not source_ready else "capital_capacity_blocked"
            item["auto_execution_status"] = "auto_execution_blocked"
        else:
            status = "capital_resized_ready" if adjusted_scale < 0.999999 else "capital_ready"
            block_category = "ready"
            status_display = status
            item["auto_execution_status"] = "auto_execution_ready"
            for key, required_key in (
                ("cash_budget", "cash_required"),
                ("new_initial_margin_budget", "initial_margin_required"),
                ("core_risk_budget", "risk_required"),
                ("single_position_notional_budget", "stock_notional_required"),
                ("options_premium_budget", "option_premium_required"),
            ):
                used[key] += requirements.get(required_key, 0.0)
                remaining[key] = max(0.0, remaining[key] - requirements.get(required_key, 0.0))
            ready_items.append(item)
        item["capital_gate_status"] = status
        item["capital_gate_status_display"] = status_display
        item["capital_block_category"] = block_category
        item["capital_capacity_ready"] = bool(status in {"capital_ready", "capital_resized_ready"})
        item["capital_action_gate_ready"] = bool(source_ready and status in {"capital_ready", "capital_resized_ready"})
        item["capital_block_reasons"] = list(dict.fromkeys(reasons))
        item["block_reasons"] = list(dict.fromkeys(reasons))
        item["checks"] = checks
        item["capital_remaining_after"] = {key: round(value, 6) for key, value in remaining.items()}
        item["source_auto_execution_status"] = original.get("auto_execution_status")
        item["source_gate_snapshot"] = {
            "source_auto_execution_status": original.get("auto_execution_status"),
            "source_block_reasons": original.get("block_reasons") or [],
            "source_market_data_action": original.get("market_data_risk_gate_action"),
            "source_market_data_defensive_mode": original.get("market_data_defensive_mode_action"),
        }
        for bulky_key in (
            "evidence_preflight",
            "market_data_risk_gate",
            "market_data_defensive_mode",
            "market_data_session_context",
            "protective_option_analysis",
            "protective_analysis_candidates",
        ):
            item.pop(bulky_key, None)
        item["safety"] = {
            **(item.get("safety") if isinstance(item.get("safety"), dict) else {}),
            "capital_gate_only": True,
            "ibkr_order_submitted": False,
            "transmit": False,
        }
        items.append(item)
        status_counts[status] += 1
        for reason in item["capital_block_reasons"]:
            reason_counts[reason] += 1
    source_gate_summary = {
        "autonomous_execution_gate_version": source_gate.get("autonomous_execution_gate_version"),
        "generated_at": source_gate.get("generated_at"),
        "source_preview_item_count": source_gate.get("source_preview_item_count"),
        "auto_ready_count": source_gate.get("auto_ready_count"),
        "auto_gate_status_counts": source_gate.get("auto_gate_status_counts") or {},
        "auto_gate_block_reason_counts": source_gate.get("auto_gate_block_reason_counts") or {},
        "gate_parameters": source_gate.get("gate_parameters") or {},
        "used_budget": source_gate.get("used_budget") or {},
    }
    result = {
        "source_gate_summary": source_gate_summary,
    }
    block_category_counts = Counter(str(item.get("capital_block_category") or "unknown") for item in items)
    result.update(
        {
            "capital_aware_action_gate_version": CAPITAL_AWARE_ACTION_GATE_VERSION,
            "autonomous_execution_gate_version": CAPITAL_AWARE_ACTION_GATE_VERSION,
            "source_autonomous_execution_gate_version": source_gate.get("autonomous_execution_gate_version"),
            "generated_at": now or _iso_now(),
            "source_auto_execution_item_count": len(source_gate.get("auto_execution_items") or []),
            "auto_ready_count": len(ready_items),
            "capital_ready_count": len(ready_items),
            "capital_resized_count": resized_count,
            "capital_blocked_count": len(items) - len(ready_items),
            "capital_capacity_blocked_count": int(block_category_counts.get("capital_capacity", 0)),
            "capital_action_blocked_count": int(block_category_counts.get("capital_action", 0)),
            "capital_block_category_counts": dict(block_category_counts),
            "auto_gate_status_counts": dict(status_counts),
            "auto_gate_block_reason_counts": dict(reason_counts),
            "capital_gate_status_counts": dict(status_counts),
            "capital_gate_block_reason_counts": dict(reason_counts),
            "capital_state": {
                "version": capital_state.get("capital_margin_state_version"),
                "generated_at": capital_state.get("generated_at"),
                "status": capital_state.get("status"),
                "status_reasons": capital_state.get("status_reasons") or [],
                "leverage_mode": leverage_mode,
                "selected_account": capital_state.get("selected_account"),
                "budgets": capital_state.get("budgets") or {},
                "withdrawal_guard": capital_state.get("withdrawal_guard") or {},
            },
            "capital_gate_parameters": {
                "reduce_to_fit_capacity": bool(reduce_to_fit_capacity),
                "allow_fractional_shares": bool(allow_fractional_shares),
                "min_stock_quantity": float(min_stock_quantity),
                "stock_quantity_increment": float(stock_quantity_increment),
                "max_ready_actions": int(max_ready_actions),
            },
            "used_budget": {key: round(value, 6) for key, value in used.items()},
            "remaining_budget": {key: round(value, 6) for key, value in remaining.items()},
            "initial_budget": {key: round(value, 6) for key, value in initial_remaining.items()},
            "auto_execution_items": items,
            "sample_auto_execution_items": items[: max(0, int(sample_limit))],
            "safety": {
                "mode": "m239_capital_aware_action_sizing_gate",
                "policy": "Applies M238 capital/margin/withdrawal budgets to paper auto-execution intents. It may resize paper quantities or block intents, but it never submits, transmits, cancels, or modifies broker orders.",
                "ibkr_order_submitted": False,
                "transmit_enabled": False,
                "live_routing_enabled": False,
            },
        }
    )
    return result


def capital_aware_action_gate_markdown(result: dict[str, Any]) -> str:
    capital = result.get("capital_state") if isinstance(result.get("capital_state"), dict) else {}
    withdrawal = capital.get("withdrawal_guard") if isinstance(capital.get("withdrawal_guard"), dict) else {}
    lines = [
        "# M239 Capital-Aware Action Sizing Gate",
        "",
        f"- Generated: `{result.get('generated_at')}`",
        f"- Capital status: `{capital.get('status')}`",
        f"- Leverage mode: `{capital.get('leverage_mode')}`",
        f"- Selected account: `{capital.get('selected_account')}`",
        f"- Source auto items: `{result.get('source_auto_execution_item_count')}`",
        f"- Capital-ready items: `{result.get('capital_ready_count')}`",
        f"- Capital-resized items: `{result.get('capital_resized_count')}`",
        f"- Blocked items: `{result.get('capital_blocked_count')}`",
        f"- Capacity-blocked items: `{result.get('capital_capacity_blocked_count')}`",
        f"- Action/source-blocked items: `{result.get('capital_action_blocked_count')}`",
        f"- Max safe withdrawal: `{_fmt_money(withdrawal.get('max_safe_withdrawal'))}`",
        "- Safety: paper gate only; no order submission or transmit.",
        "",
        "## Budget",
        "",
        "| Budget | Initial | Used | Remaining |",
        "| --- | ---: | ---: | ---: |",
    ]
    initial = result.get("initial_budget") or {}
    used = result.get("used_budget") or {}
    remaining = result.get("remaining_budget") or {}
    for key in ("cash_budget", "new_initial_margin_budget", "core_risk_budget", "single_position_notional_budget", "options_premium_budget"):
        lines.append(f"| {key} | {_fmt_money(initial.get(key))} | {_fmt_money(used.get(key))} | {_fmt_money(remaining.get(key))} |")
    for title, counts in (
        ("Capital gate status counts", result.get("capital_gate_status_counts") or {}),
        ("Capital block category counts", result.get("capital_block_category_counts") or {}),
        ("Capital gate block reason counts", result.get("capital_gate_block_reason_counts") or {}),
    ):
        lines.extend(["", f"## {title}", "", "| Bucket | Count |", "| --- | ---: |"])
        if counts:
            for key, value in sorted(counts.items(), key=lambda item: (-int(item[1]), item[0])):
                lines.append(f"| `{key}` | {value} |")
        else:
            lines.append("| - | 0 |")
    lines.extend(
        [
            "",
            "## Sample items",
            "",
            "| Symbol | Status | Category | Action | Ticket | Quantity | Reasons |",
            "| --- | --- | --- | --- | --- | ---: | --- |",
        ]
    )
    for item in result.get("sample_auto_execution_items") or []:
        reasons = ", ".join(item.get("capital_block_reasons") or []) or "-"
        lines.append(
            f"| `{item.get('symbol')}` | `{item.get('capital_gate_status_display') or item.get('capital_gate_status')}` | `{item.get('capital_block_category') or '-'}` | `{item.get('action')}` | `{item.get('ticket_type')}` | {item.get('quantity')} | {reasons} |"
        )
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "M239 sits after the scorer/auto-gate path and before intent ledgering. `capital_capacity` means the account budget cannot safely carry the size. `capital_action` means an upstream source/market/action gate is not ready, even if account capacity might be sufficient.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_capital_aware_action_gate_json(path: Path, result: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def write_capital_aware_action_gate_markdown(path: Path, result: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(capital_aware_action_gate_markdown(result), encoding="utf-8")
    return path
