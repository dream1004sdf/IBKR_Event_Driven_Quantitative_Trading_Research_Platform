from __future__ import annotations

import json
import math
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from recorder.capital_micro_policy import (
    MICRO_CASH_POLICY_VERSION,
    build_micro_cash_policy,
    evaluate_micro_policy_for_candidate,
    micro_cash_policy_state,
    scaled_requirements,
)


LIVE_READ_ONLY_CAPITAL_VALIDATION_VERSION = "m243-live-read-only-capital-validation-v2-gate-attribution"
TRADE_EXECUTABLE_SPLIT_VERSION = "m245-capital-feasible-vs-trade-executable-v2-gate-attribution"

CAPITAL_READY_STATUSES = {"capital_ready", "capital_resized_ready"}
SOURCE_READY_STATUSES = {"auto_execution_ready"}


def _now_iso() -> str:
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


def _fmt_qty(value: Any) -> str:
    parsed = _as_float(value)
    if parsed is None:
        return "n/a"
    if abs(parsed - round(parsed)) < 1e-9:
        return str(int(round(parsed)))
    return f"{parsed:,.4f}".rstrip("0").rstrip(".")


def _deepcopy(payload: Any) -> Any:
    return json.loads(json.dumps(payload, ensure_ascii=False))


def _budget(capital_state: dict[str, Any], key: str) -> float:
    budgets = capital_state.get("budgets") if isinstance(capital_state.get("budgets"), dict) else {}
    return max(0.0, _as_float(budgets.get(key)) or 0.0)


def _live_budget_snapshot(capital_state: dict[str, Any]) -> dict[str, float]:
    return {
        "cash_budget": _budget(capital_state, "deployable_cash_after_reserve"),
        "new_initial_margin_budget": _budget(capital_state, "max_new_initial_margin"),
        "core_risk_budget": _budget(capital_state, "core_risk_budget"),
        "single_position_notional_budget": _budget(capital_state, "max_single_position_notional"),
        "options_premium_budget": _budget(capital_state, "options_premium_budget"),
    }


def _capital_allows_validation(capital_state: dict[str, Any]) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    status = str(capital_state.get("status") or "unknown")
    withdrawal_guard = capital_state.get("withdrawal_guard") if isinstance(capital_state.get("withdrawal_guard"), dict) else {}
    if status == "red":
        reasons.append("capital_state_red")
    if withdrawal_guard.get("requires_fresh_account_snapshot"):
        reasons.append("requires_fresh_account_snapshot")
    if "stale_account_snapshot" in set(capital_state.get("status_reasons") or []):
        reasons.append("stale_account_snapshot")
    if "missing_account_snapshot" in set(capital_state.get("status_reasons") or []):
        reasons.append("missing_account_snapshot")
    return not reasons, reasons


def _item_source(item: dict[str, Any]) -> dict[str, Any]:
    source = item.get("source_preview_item")
    return source if isinstance(source, dict) else {}


def _item_ticket(item: dict[str, Any]) -> dict[str, Any]:
    source = _item_source(item)
    ticket = source.get("ticket_draft") if isinstance(source.get("ticket_draft"), dict) else {}
    if ticket:
        return ticket
    intent = item.get("order_intent") if isinstance(item.get("order_intent"), dict) else {}
    order = intent.get("order_preview") if isinstance(intent.get("order_preview"), dict) else {}
    return order if isinstance(order, dict) else {}


def _item_order(item: dict[str, Any]) -> dict[str, Any]:
    intent = item.get("order_intent") if isinstance(item.get("order_intent"), dict) else {}
    order = intent.get("order_preview") if isinstance(intent.get("order_preview"), dict) else {}
    if order:
        return order
    source = _item_source(item)
    order = source.get("ibkr_what_if_order_preview") if isinstance(source.get("ibkr_what_if_order_preview"), dict) else {}
    return order if isinstance(order, dict) else {}


def _item_local_margin(item: dict[str, Any]) -> dict[str, Any]:
    margin = item.get("local_margin_estimate") if isinstance(item.get("local_margin_estimate"), dict) else {}
    if margin:
        return margin
    source = _item_source(item)
    margin = source.get("local_margin_estimate") if isinstance(source.get("local_margin_estimate"), dict) else {}
    return margin if isinstance(margin, dict) else {}


def _item_symbol(item: dict[str, Any]) -> str:
    ticket = _item_ticket(item)
    order = _item_order(item)
    return str(item.get("symbol") or ticket.get("symbol") or order.get("symbol") or "").upper()


def _item_action(item: dict[str, Any]) -> str:
    ticket = _item_ticket(item)
    order = _item_order(item)
    return str(item.get("action") or order.get("source_order_action") or ticket.get("order_action") or order.get("action") or "").upper()


def _item_ticket_type(item: dict[str, Any]) -> str:
    ticket = _item_ticket(item)
    return str(item.get("ticket_type") or ticket.get("ticket_type") or "").lower()


def _item_quantity(item: dict[str, Any]) -> float | None:
    ticket = _item_ticket(item)
    order = _item_order(item)
    return _as_float(item.get("quantity")) or _as_float(order.get("total_quantity")) or _as_float(ticket.get("quantity"))


def _is_stock_ticket(ticket_type: str) -> bool:
    return ticket_type in {"stock", "stk", "equity", "etf"}


def _is_option_ticket(ticket_type: str) -> bool:
    return "option" in ticket_type


def _risk_required(item: dict[str, Any], margin: dict[str, Any]) -> float:
    ticket = _item_ticket(item)
    for key in ("estimated_max_loss", "max_loss", "sized_trade_risk", "estimated_stop_loss", "stop_loss_proxy"):
        parsed = _as_float(ticket.get(key))
        if parsed is not None:
            return max(0.0, parsed)
    for key in ("estimated_max_loss_proxy", "estimated_initial_margin_change"):
        parsed = _as_float(margin.get(key))
        if parsed is not None:
            return max(0.0, parsed)
    return 0.0


def _ticket_notional(item: dict[str, Any], margin: dict[str, Any]) -> float:
    ticket = _item_ticket(item)
    for key in ("estimated_notional", "notional", "gross_notional", "sized_gross_notional"):
        parsed = _as_float(ticket.get(key))
        if parsed is not None:
            return max(0.0, parsed)
    parsed = _as_float(margin.get("estimated_initial_margin_change"))
    return max(0.0, parsed or 0.0)


def _requirements_for_item(item: dict[str, Any]) -> dict[str, float]:
    existing = item.get("capital_requirements") if isinstance(item.get("capital_requirements"), dict) else {}
    if existing:
        return {
            "cash_required": max(0.0, _as_float(existing.get("cash_required")) or 0.0),
            "initial_margin_required": max(0.0, _as_float(existing.get("initial_margin_required")) or 0.0),
            "risk_required": max(0.0, _as_float(existing.get("risk_required")) or 0.0),
            "stock_notional_required": max(0.0, _as_float(existing.get("stock_notional_required")) or 0.0),
            "option_premium_required": max(0.0, _as_float(existing.get("option_premium_required")) or 0.0),
        }
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


def _cash_like_capacity_requirements(
    requirements: dict[str, float],
    *,
    leverage_mode: str,
    action: str,
    ticket_type: str,
) -> dict[str, float]:
    adjusted = dict(requirements)
    if not _is_stock_ticket(ticket_type):
        return adjusted
    if str(leverage_mode or "") != "cash_only":
        return adjusted
    if str(action or "").upper() not in {"BUY", "BUY_TO_OPEN"}:
        return adjusted
    initial_margin = max(0.0, _as_float(adjusted.get("initial_margin_required")) or 0.0)
    cash_required = max(0.0, _as_float(adjusted.get("cash_required")) or 0.0)
    stock_notional = max(0.0, _as_float(adjusted.get("stock_notional_required")) or 0.0)
    if initial_margin <= max(cash_required, stock_notional) + 1e-9:
        adjusted["initial_margin_required"] = 0.0
    return adjusted


def _round_down_to_increment(value: float, increment: float) -> float:
    if increment <= 0:
        return max(0.0, value)
    return max(0.0, math.floor((value + 1e-12) / increment) * increment)


def _quantity_increment(ticket_type: str, *, allow_fractional_shares: bool, stock_quantity_increment: float) -> float:
    if _is_option_ticket(ticket_type):
        return 1.0
    if _is_stock_ticket(ticket_type):
        return max(0.000001, float(stock_quantity_increment if allow_fractional_shares else 1.0))
    return 1.0


def _capacity_constraints(requirements: dict[str, float], budgets: dict[str, float], ticket_type: str) -> list[dict[str, Any]]:
    mapping = [
        ("cash_budget", requirements["cash_required"], budgets["cash_budget"]),
        ("new_initial_margin_budget", requirements["initial_margin_required"], budgets["new_initial_margin_budget"]),
        ("core_risk_budget", requirements["risk_required"], budgets["core_risk_budget"]),
    ]
    if _is_stock_ticket(ticket_type):
        mapping.append(("single_position_notional_budget", requirements["stock_notional_required"], budgets["single_position_notional_budget"]))
    if _is_option_ticket(ticket_type):
        mapping.append(("options_premium_budget", requirements["option_premium_required"], budgets["options_premium_budget"]))
    return [
        {
            "name": name,
            "required_at_source_quantity": round(required, 6),
            "budget": round(budget, 6),
            "passed_current_quantity": required <= budget + 1e-9,
        }
        for name, required, budget in mapping
    ]


def _max_quantity_from_constraints(
    *,
    source_quantity: float,
    constraints: list[dict[str, Any]],
    ticket_type: str,
    allow_fractional_shares: bool,
    stock_quantity_increment: float,
) -> tuple[float, str | None, list[dict[str, Any]]]:
    if source_quantity <= 0:
        return 0.0, "missing_or_non_positive_quantity", constraints
    max_quantity = float("inf")
    binding: str | None = None
    enriched: list[dict[str, Any]] = []
    for constraint in constraints:
        required = _as_float(constraint.get("required_at_source_quantity")) or 0.0
        budget = _as_float(constraint.get("budget")) or 0.0
        candidate = float("inf") if required <= 0 else source_quantity * (budget / required)
        row = dict(constraint)
        row["max_quantity_by_constraint"] = None if not math.isfinite(candidate) else round(max(0.0, candidate), 6)
        enriched.append(row)
        if candidate < max_quantity:
            max_quantity = candidate
            binding = str(constraint.get("name"))
    if not math.isfinite(max_quantity):
        max_quantity = source_quantity
    increment = _quantity_increment(
        ticket_type,
        allow_fractional_shares=allow_fractional_shares,
        stock_quantity_increment=stock_quantity_increment,
    )
    rounded = _round_down_to_increment(max_quantity, increment)
    if _is_option_ticket(ticket_type) and rounded < 1:
        rounded = 0.0
    return round(rounded, 6), binding, enriched


def _broker_whatif_safe(payload: dict[str, Any] | None) -> tuple[dict[str, Any], bool, list[str]]:
    result = _payload_result(payload)
    if not result:
        return {}, False, ["broker_whatif_missing"]
    safety = result.get("safety") if isinstance(result.get("safety"), dict) else {}
    issues: list[str] = []
    if safety.get("what_if_only") is not True:
        issues.append("broker_whatif_not_marked_what_if_only")
    if safety.get("live_order_transmitted") is True:
        issues.append("broker_whatif_live_order_transmitted_true")
    if safety.get("ibkr_order_submitted") is True:
        issues.append("broker_whatif_ibkr_order_submitted_true")
    return result, not issues, issues


def _broker_whatif_rows(payload: dict[str, Any] | None) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    result, safe, issues = _broker_whatif_safe(payload)
    if not result or not safe:
        return {}, {
            "available": bool(result),
            "safe_to_use": bool(safe),
            "issues": issues,
            "response_count": int(result.get("response_count") or 0) if result else 0,
        }
    rows: dict[str, dict[str, Any]] = {}
    for row in result.get("responses") or []:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or ((row.get("contract") or {}).get("symbol") if isinstance(row.get("contract"), dict) else "")).upper()
        if not symbol:
            continue
        rows[symbol] = row
    return rows, {
        "available": True,
        "safe_to_use": True,
        "issues": [],
        "response_count": int(result.get("response_count") or len(rows) or 0),
        "requested_count": int(result.get("requested_count") or 0),
        "read_only_mode_detected": bool(result.get("read_only_mode_detected")),
        "generated_at": result.get("generated_at"),
    }


def _broker_requirement_constraint(
    row: dict[str, Any] | None,
    *,
    source_quantity: float,
    budgets: dict[str, float],
) -> dict[str, Any] | None:
    if not row:
        return None
    state = row.get("order_state") if isinstance(row.get("order_state"), dict) else {}
    order = row.get("order") if isinstance(row.get("order"), dict) else {}
    broker_quantity = _as_float(order.get("source_total_quantity")) or _as_float(order.get("total_quantity")) or source_quantity
    init_margin_change = abs(_as_float(state.get("initMarginChange")) or 0.0)
    maint_margin_change = abs(_as_float(state.get("maintMarginChange")) or 0.0)
    required = max(init_margin_change, maint_margin_change)
    if required <= 0 or broker_quantity <= 0:
        return {
            "name": "broker_whatif_margin_budget",
            "required_at_source_quantity": 0.0,
            "budget": round(budgets["new_initial_margin_budget"], 6),
            "passed_current_quantity": True,
            "broker_status": state.get("status"),
            "warning_text": state.get("warningText"),
        }
    required_at_source_quantity = required * (source_quantity / broker_quantity)
    return {
        "name": "broker_whatif_margin_budget",
        "required_at_source_quantity": round(required_at_source_quantity, 6),
        "budget": round(budgets["new_initial_margin_budget"], 6),
        "passed_current_quantity": required_at_source_quantity <= budgets["new_initial_margin_budget"] + 1e-9,
        "broker_status": state.get("status"),
        "warning_text": state.get("warningText"),
    }


def _source_auto_status(item: dict[str, Any]) -> str:
    snapshot = item.get("source_gate_snapshot") if isinstance(item.get("source_gate_snapshot"), dict) else {}
    return str(snapshot.get("source_auto_execution_status") or item.get("auto_execution_status") or item.get("source_auto_execution_status") or "")


def _source_market_action(item: dict[str, Any]) -> str:
    snapshot = item.get("source_gate_snapshot") if isinstance(item.get("source_gate_snapshot"), dict) else {}
    return str(snapshot.get("source_market_data_action") or item.get("market_data_action") or "")


def _source_market_mode(item: dict[str, Any]) -> str:
    snapshot = item.get("source_gate_snapshot") if isinstance(item.get("source_gate_snapshot"), dict) else {}
    return str(snapshot.get("source_market_data_defensive_mode") or item.get("market_data_defensive_mode") or "")


def _source_gate_reasons(item: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    for key in ("capital_block_reasons", "block_reasons", "reasons"):
        values = item.get(key)
        if isinstance(values, list):
            reasons.extend(str(value) for value in values if value)
    snapshot = item.get("source_gate_snapshot") if isinstance(item.get("source_gate_snapshot"), dict) else {}
    if isinstance(snapshot.get("source_block_reasons"), list):
        reasons.extend(str(value) for value in snapshot.get("source_block_reasons") if value)
    return list(dict.fromkeys(reasons))


def _reason_category_flags(reasons: list[str]) -> dict[str, bool]:
    lowered = [reason.lower() for reason in reasons]
    return {
        "source": any(reason.startswith("source_") or "source_auto_gate" in reason for reason in lowered),
        "market": any(reason.startswith("market_data_") or reason.startswith("market_gate_") for reason in lowered),
        "session": any(reason.startswith("session_") or "session_transition" in reason for reason in lowered),
        "cash_only": any("cash_only" in reason for reason in lowered),
        "broker": any(reason.startswith("broker_whatif_") for reason in lowered),
        "capital": any(
            reason.startswith("capital_")
            or reason.startswith("restricted_margin")
            or reason.endswith("_insufficient")
            or reason in {"requires_fresh_account_snapshot", "stale_account_snapshot", "missing_account_snapshot", "missing_or_non_positive_quantity"}
            for reason in lowered
        ),
    }


def build_live_read_only_capital_validation_report(
    *,
    capital_state_payload: dict[str, Any],
    capital_aware_gate_payload: dict[str, Any] | None = None,
    broker_whatif_payload: dict[str, Any] | None = None,
    allow_fractional_shares: bool = True,
    stock_quantity_increment: float = 0.01,
    max_items: int = 50,
    sample_limit: int = 20,
    previous_micro_policy_state_payload: dict[str, Any] | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    capital_state = _payload_result(capital_state_payload)
    capital_gate = _payload_result(capital_aware_gate_payload)
    budgets = _live_budget_snapshot(capital_state)
    generated_at = now or _now_iso()
    micro_policy = build_micro_cash_policy(capital_state, previous_policy_state=previous_micro_policy_state_payload)
    micro_policy_state = micro_cash_policy_state(micro_policy, generated_at=generated_at)
    capital_allows, capital_block_reasons = _capital_allows_validation(capital_state)
    broker_rows, broker_summary = _broker_whatif_rows(broker_whatif_payload)
    items: list[dict[str, Any]] = []
    status_counts: Counter[str] = Counter()
    execution_status_counts: Counter[str] = Counter()
    block_reason_counts: Counter[str] = Counter()
    execution_block_reason_counts: Counter[str] = Counter()
    blocked_by_counts: Counter[str] = Counter()
    micro_policy_block_reason_counts: Counter[str] = Counter()
    micro_policy_current_ready_count = 0
    micro_policy_after_resize_ready_count = 0
    micro_policy_blocked_count = 0
    source_items = [row for row in (capital_gate.get("auto_execution_items") or []) if isinstance(row, dict)]
    for raw in source_items[: max(0, int(max_items))]:
        item = _deepcopy(raw)
        symbol = _item_symbol(item)
        action = _item_action(item)
        ticket_type = _item_ticket_type(item)
        quantity = _item_quantity(item) or 0.0
        leverage_mode = str(capital_state.get("leverage_mode") or "unknown")
        requirements = _requirements_for_item(item)
        capacity_requirements = _cash_like_capacity_requirements(
            requirements,
            leverage_mode=leverage_mode,
            action=action,
            ticket_type=ticket_type,
        )
        constraints = _capacity_constraints(capacity_requirements, budgets, ticket_type)
        broker_constraint = _broker_requirement_constraint(
            broker_rows.get(symbol),
            source_quantity=quantity,
            budgets=budgets,
        )
        broker_whatif_available = broker_constraint is not None
        if broker_constraint is not None:
            constraints.append(broker_constraint)
        max_quantity, binding_constraint, enriched_constraints = _max_quantity_from_constraints(
            source_quantity=quantity,
            constraints=constraints,
            ticket_type=ticket_type,
            allow_fractional_shares=allow_fractional_shares,
            stock_quantity_increment=stock_quantity_increment,
        )
        micro_policy_current = evaluate_micro_policy_for_candidate(
            micro_policy,
            action=action,
            ticket_type=ticket_type,
            requirements=requirements,
        )
        resize_ratio = (max_quantity / quantity) if quantity > 0 else 0.0
        micro_policy_after_resize = evaluate_micro_policy_for_candidate(
            micro_policy,
            action=action,
            ticket_type=ticket_type,
            requirements=scaled_requirements(requirements, resize_ratio),
        )
        item_block_reasons: list[str] = []
        source_gate_reasons = _source_gate_reasons(item)
        source_auto_status = _source_auto_status(item)
        capital_gate_status = str(item.get("capital_gate_status") or "")
        source_market_action = _source_market_action(item)
        source_market_mode = _source_market_mode(item)
        micro_policy_current_ready = bool(micro_policy_current.get("ready"))
        micro_policy_after_resize_ready = bool(micro_policy_after_resize.get("ready"))
        micro_policy_reasons = list(micro_policy_current.get("block_reasons") or [])
        if micro_policy_current_ready:
            micro_policy_current_ready_count += 1
        else:
            micro_policy_blocked_count += 1
        if micro_policy_after_resize_ready:
            micro_policy_after_resize_ready_count += 1
        for reason in micro_policy_reasons:
            micro_policy_block_reason_counts[reason] += 1
        if leverage_mode == "cash_only" and action == "SELL_SHORT":
            item_block_reasons.append("cash_only_blocks_short_stock")
        if leverage_mode == "restricted_margin":
            item_block_reasons.append("restricted_margin_blocks_new_entries")
        item_allows = bool(capital_allows and not item_block_reasons)
        current_feasible = bool(item_allows and quantity > 0 and max_quantity + 1e-9 >= quantity)
        resize_possible = bool(item_allows and quantity > 0 and 0 < max_quantity < quantity)
        reasons = list(capital_block_reasons)
        reasons.extend(item_block_reasons)
        if quantity <= 0:
            reasons.append("missing_or_non_positive_quantity")
        failed_constraints = [row["name"] for row in enriched_constraints if not row.get("passed_current_quantity")]
        reasons.extend(f"{name}_insufficient" for name in failed_constraints)
        if broker_summary.get("available") and not broker_summary.get("safe_to_use"):
            reasons.extend(str(issue) for issue in broker_summary.get("issues") or [])
        reasons.extend(micro_policy_reasons)
        all_execution_reasons = list(dict.fromkeys([*source_gate_reasons, *reasons]))
        category_flags = _reason_category_flags(all_execution_reasons)
        capital_gate_ready = capital_gate_status in CAPITAL_READY_STATUSES
        source_gate_ready = source_auto_status in SOURCE_READY_STATUSES and not category_flags["source"]
        market_gate_ready = not category_flags["market"]
        session_gate_ready = not category_flags["session"]
        cash_only_ready = not category_flags["cash_only"]
        broker_gate_ready = not category_flags["broker"]
        capital_current_size_feasible = current_feasible
        capital_resize_possible = resize_possible
        capital_capacity_ready = capital_current_size_feasible
        capital_action_gate_ready = capital_gate_ready
        trade_executable = bool(
            capital_current_size_feasible
            and capital_action_gate_ready
            and source_gate_ready
            and market_gate_ready
            and session_gate_ready
            and cash_only_ready
            and broker_gate_ready
            and micro_policy_current_ready
        )
        trade_executable_after_resize = bool(
            capital_resize_possible
            and capital_action_gate_ready
            and source_gate_ready
            and market_gate_ready
            and session_gate_ready
            and cash_only_ready
            and broker_gate_ready
            and micro_policy_after_resize_ready
        )
        execution_reasons: list[str] = []
        if not capital_current_size_feasible:
            if capital_resize_possible:
                execution_reasons.append("capital_resize_required_before_trade_executable")
            else:
                execution_reasons.append("capital_capacity_blocks_trade_executable")
        if not capital_action_gate_ready:
            execution_reasons.append("capital_action_gate_not_ready")
        if not source_gate_ready:
            execution_reasons.append("source_gate_not_ready")
        if not market_gate_ready:
            execution_reasons.append("market_gate_not_ready")
        if not session_gate_ready:
            execution_reasons.append("session_gate_not_ready")
        if not cash_only_ready:
            execution_reasons.append("cash_only_policy_blocks_trade_executable")
        if not broker_gate_ready:
            execution_reasons.append("broker_whatif_safety_blocks_trade_executable")
        if not micro_policy_current_ready:
            if capital_resize_possible and micro_policy_after_resize_ready:
                execution_reasons.append("micro_policy_resize_required_before_trade_executable")
            else:
                execution_reasons.append("micro_policy_blocks_trade_executable")
        execution_reasons.extend(all_execution_reasons)
        execution_reasons = list(dict.fromkeys(execution_reasons))
        if trade_executable:
            execution_status = "trade_executable_current_size"
        elif trade_executable_after_resize:
            execution_status = "trade_executable_after_resize"
        else:
            execution_status = "not_trade_executable"
        if current_feasible:
            status = "live_read_only_current_size_feasible"
        elif resize_possible:
            status = "live_read_only_resize_required"
        else:
            status = "live_read_only_blocked"
        status_counts[status] += 1
        execution_status_counts[execution_status] += 1
        for reason in dict.fromkeys(reasons):
            block_reason_counts[reason] += 1
        for reason in execution_reasons:
            execution_block_reason_counts[reason] += 1
        if not capital_current_size_feasible:
            blocked_by_counts["capital_capacity"] += 1
            blocked_by_counts["capital"] += 1
        if not capital_action_gate_ready:
            blocked_by_counts["capital_action"] += 1
        if not source_gate_ready:
            blocked_by_counts["source"] += 1
        if not market_gate_ready:
            blocked_by_counts["market"] += 1
        if not session_gate_ready:
            blocked_by_counts["session"] += 1
        if not cash_only_ready:
            blocked_by_counts["cash_only"] += 1
        if not broker_gate_ready:
            blocked_by_counts["broker"] += 1
        if not micro_policy_current_ready:
            blocked_by_counts["micro_policy"] += 1
        items.append(
            {
                "symbol": symbol,
                "action": action,
                "ticket_type": ticket_type,
                "source_quantity": round(quantity, 6),
                "max_feasible_quantity": max_quantity,
                "current_size_feasible": current_feasible,
                "resize_possible": resize_possible,
                "binding_constraint": binding_constraint,
                "status": status,
                "reasons": list(dict.fromkeys(reasons)),
                "capital_gate_status": item.get("capital_gate_status"),
                "source_auto_execution_status": source_auto_status,
                "execution_gate": {
                    "trade_executable_split_version": TRADE_EXECUTABLE_SPLIT_VERSION,
                    "status": execution_status,
                    "trade_executable": trade_executable,
                    "trade_executable_after_resize": trade_executable_after_resize,
                    "capital_current_size_feasible": capital_current_size_feasible,
                    "capital_resize_possible": capital_resize_possible,
                    "capital_capacity_ready": capital_capacity_ready,
                    "capital_action_gate_ready": capital_action_gate_ready,
                    "capital_gate_ready": capital_gate_ready,
                    "source_gate_ready": source_gate_ready,
                    "market_gate_ready": market_gate_ready,
                    "session_gate_ready": session_gate_ready,
                    "cash_only_ready": cash_only_ready,
                    "broker_gate_ready": broker_gate_ready,
                    "micro_policy_ready": micro_policy_current_ready,
                    "micro_policy_after_resize_ready": micro_policy_after_resize_ready,
                    "micro_policy_operating_mode": micro_policy.get("operating_mode"),
                    "micro_policy_current": micro_policy_current,
                    "micro_policy_after_resize": micro_policy_after_resize,
                    "source_auto_execution_status": source_auto_status,
                    "source_market_data_action": source_market_action,
                    "source_market_data_defensive_mode": source_market_mode,
                    "source_gate_reasons": source_gate_reasons,
                    "execution_block_reasons": execution_reasons,
                    "blocked_by": {
                        "capital": not capital_current_size_feasible,
                        "capital_capacity": not capital_current_size_feasible,
                        "capital_action": not capital_action_gate_ready,
                        "source": not source_gate_ready,
                        "market": not market_gate_ready,
                        "session": not session_gate_ready,
                        "cash_only": not cash_only_ready,
                        "broker": not broker_gate_ready,
                        "micro_policy": not micro_policy_current_ready,
                    },
                },
                "requirements_at_source_quantity": {key: round(value, 6) for key, value in requirements.items()},
                "capacity_requirements_used": {key: round(value, 6) for key, value in capacity_requirements.items()},
                "constraints": enriched_constraints,
                "broker_whatif_available": broker_whatif_available,
                "broker_whatif_status": broker_constraint.get("broker_status") if broker_constraint else None,
                "broker_whatif_warning": broker_constraint.get("warning_text") if broker_constraint else None,
                "safety": {
                    "live_read_only_validation": True,
                    "ibkr_order_submitted": False,
                    "transmit_enabled": False,
                    "live_order_transmitted": False,
                },
            }
        )
    withdrawal_guard = capital_state.get("withdrawal_guard") if isinstance(capital_state.get("withdrawal_guard"), dict) else {}
    result = {
        "live_read_only_capital_validation_version": LIVE_READ_ONLY_CAPITAL_VALIDATION_VERSION,
        "trade_executable_split_version": TRADE_EXECUTABLE_SPLIT_VERSION,
        "generated_at": generated_at,
        "status": (
            "red"
            if not capital_allows
            else (
                "yellow"
                if status_counts.get("live_read_only_resize_required") or status_counts.get("live_read_only_blocked") or micro_policy_blocked_count
                else "green"
            )
        ),
        "capital_state": {
            "version": capital_state.get("capital_margin_state_version"),
            "generated_at": capital_state.get("generated_at"),
            "status": capital_state.get("status"),
            "status_reasons": capital_state.get("status_reasons") or [],
            "leverage_mode": capital_state.get("leverage_mode"),
            "selected_account": capital_state.get("selected_account"),
            "snapshot_age_seconds": capital_state.get("snapshot_age_seconds"),
            "metrics": capital_state.get("metrics") or {},
            "budgets": capital_state.get("budgets") or {},
            "withdrawal_guard": withdrawal_guard,
        },
        "source_capital_aware_gate": {
            "version": capital_gate.get("capital_aware_action_gate_version") or capital_gate.get("autonomous_execution_gate_version"),
            "generated_at": capital_gate.get("generated_at"),
            "capital_ready_count": capital_gate.get("capital_ready_count"),
            "capital_resized_count": capital_gate.get("capital_resized_count"),
            "capital_blocked_count": capital_gate.get("capital_blocked_count"),
            "source_item_count": len(source_items),
        },
        "broker_whatif": broker_summary,
        "micro_cash_policy": micro_policy,
        "micro_policy_state": micro_policy_state,
        "live_budgets": budgets,
        "account_capacity": {
            "deployable_cash_after_reserve": budgets["cash_budget"],
            "max_new_initial_margin": budgets["new_initial_margin_budget"],
            "core_risk_budget": budgets["core_risk_budget"],
            "max_single_position_notional": budgets["single_position_notional_budget"],
            "options_premium_budget": budgets["options_premium_budget"],
            "max_safe_withdrawal": withdrawal_guard.get("max_safe_withdrawal"),
            "requires_fresh_account_snapshot": withdrawal_guard.get("requires_fresh_account_snapshot"),
        },
        "validation_item_count": len(items),
        "current_size_feasible_count": int(status_counts.get("live_read_only_current_size_feasible") or 0),
        "resize_required_count": int(status_counts.get("live_read_only_resize_required") or 0),
        "blocked_count": int(status_counts.get("live_read_only_blocked") or 0),
        "validation_status_counts": dict(status_counts),
        "block_reason_counts": dict(block_reason_counts),
        "trade_executable_current_size_count": int(execution_status_counts.get("trade_executable_current_size") or 0),
        "trade_executable_after_resize_count": int(execution_status_counts.get("trade_executable_after_resize") or 0),
        "not_trade_executable_count": int(execution_status_counts.get("not_trade_executable") or 0),
        "execution_status_counts": dict(execution_status_counts),
        "execution_block_reason_counts": dict(execution_block_reason_counts),
        "blocked_by_gate_counts": dict(blocked_by_counts),
        "micro_policy_version": MICRO_CASH_POLICY_VERSION,
        "micro_policy_operating_mode": micro_policy.get("operating_mode"),
        "micro_policy_current_ready_count": int(micro_policy_current_ready_count),
        "micro_policy_after_resize_ready_count": int(micro_policy_after_resize_ready_count),
        "micro_policy_blocked_count": int(micro_policy_blocked_count),
        "micro_policy_block_reason_counts": dict(micro_policy_block_reason_counts),
        "capital_vs_execution_summary": {
            "capital_current_size_feasible_count": int(status_counts.get("live_read_only_current_size_feasible") or 0),
            "capital_resize_possible_count": int(status_counts.get("live_read_only_resize_required") or 0),
            "capital_blocked_count": int(status_counts.get("live_read_only_blocked") or 0),
            "trade_executable_current_size_count": int(execution_status_counts.get("trade_executable_current_size") or 0),
            "trade_executable_after_resize_count": int(execution_status_counts.get("trade_executable_after_resize") or 0),
            "not_trade_executable_count": int(execution_status_counts.get("not_trade_executable") or 0),
            "blocked_by_gate_counts": dict(blocked_by_counts),
            "micro_policy_operating_mode": micro_policy.get("operating_mode"),
            "micro_policy_blocked_count": int(micro_policy_blocked_count),
            "note": "Capital-feasible means the live account can carry the size. Trade-executable additionally requires source, market, session, cash-only, broker, micro-policy, and capital gates to be ready.",
        },
        "validation_items": items,
        "sample_validation_items": items[: max(0, int(sample_limit))],
        "parameters": {
            "allow_fractional_shares": bool(allow_fractional_shares),
            "stock_quantity_increment": float(stock_quantity_increment),
            "max_items": int(max_items),
            "sample_limit": int(sample_limit),
        },
        "safety": {
            "mode": "m243_live_read_only_capital_validation",
            "policy": "Validates live-account capital capacity from account snapshot and optional broker what-if evidence. It does not submit, transmit, cancel, or modify broker orders.",
            "read_only": True,
            "account_snapshot_read_only": True,
            "whatif_cache_consumed_only": True,
            "whatif_request_submitted_by_m243": False,
            "ibkr_order_submitted": False,
            "transmit_enabled": False,
            "live_order_transmitted": False,
            "live_routing_enabled": False,
        },
    }
    return result


def live_read_only_capital_validation_markdown(report: dict[str, Any]) -> str:
    capital = report.get("capital_state") if isinstance(report.get("capital_state"), dict) else {}
    capacity = report.get("account_capacity") if isinstance(report.get("account_capacity"), dict) else {}
    broker = report.get("broker_whatif") if isinstance(report.get("broker_whatif"), dict) else {}
    micro_policy = report.get("micro_cash_policy") if isinstance(report.get("micro_cash_policy"), dict) else {}
    micro_state = report.get("micro_policy_state") if isinstance(report.get("micro_policy_state"), dict) else {}
    exit_hysteresis = micro_policy.get("exit_hysteresis") if isinstance(micro_policy.get("exit_hysteresis"), dict) else {}
    lines = [
        "# M243 Live Read-Only Capital Validation",
        "",
        f"- Generated: `{report.get('generated_at')}`",
        f"- Status: `{report.get('status')}`",
        f"- Account: `{capital.get('selected_account')}`",
        f"- Capital state: `{capital.get('status')}` / leverage `{capital.get('leverage_mode')}`",
        f"- Candidate validation: feasible `{report.get('current_size_feasible_count')}`, resize `{report.get('resize_required_count')}`, blocked `{report.get('blocked_count')}`",
        f"- Trade executable split: current `{report.get('trade_executable_current_size_count')}`, after resize `{report.get('trade_executable_after_resize_count')}`, not executable `{report.get('not_trade_executable_count')}`",
        f"- Micro cash policy: mode `{report.get('micro_policy_operating_mode')}` ready `{report.get('micro_policy_current_ready_count')}` after-resize ready `{report.get('micro_policy_after_resize_ready_count')}` blocked `{report.get('micro_policy_blocked_count')}`",
        f"- Micro exit hysteresis: transition `{micro_state.get('transition')}` confirmations `{micro_state.get('consecutive_exit_confirmations')}` / `{micro_state.get('exit_confirmations_required')}` exit-ready `{micro_state.get('exit_ready')}` hold `{micro_state.get('exit_hold_active')}`",
        f"- Blocked by gates: `{report.get('blocked_by_gate_counts') or {}}`",
        "- Gate attribution: `capital_capacity` means account capacity is insufficient; `capital_action` means upstream action/source/session gates are not ready; `source`/`market` identify the originating non-capital blockers.",
        f"- Micro policy reasons: `{report.get('micro_policy_block_reason_counts') or {}}`",
        f"- Broker what-if cache: available `{broker.get('available')}` safe `{broker.get('safe_to_use')}` responses `{broker.get('response_count')}`",
        "- Safety: live read-only validation only; no broker order submission, no transmit, no live routing.",
        "",
        "## Live account capacity",
        "",
        "| Capacity | Amount |",
        "| --- | ---: |",
        f"| Deployable cash after reserve | {_fmt_money(capacity.get('deployable_cash_after_reserve'))} |",
        f"| Max new initial margin | {_fmt_money(capacity.get('max_new_initial_margin'))} |",
        f"| Core risk budget | {_fmt_money(capacity.get('core_risk_budget'))} |",
        f"| Max single-position notional | {_fmt_money(capacity.get('max_single_position_notional'))} |",
        f"| Options premium budget | {_fmt_money(capacity.get('options_premium_budget'))} |",
        f"| Max safe withdrawal | {_fmt_money(capacity.get('max_safe_withdrawal'))} |",
        f"| M246 operating mode | {micro_policy.get('operating_mode') or 'n/a'} |",
        f"| M246 candidate mode | {micro_policy.get('candidate_operating_mode') or 'n/a'} |",
        f"| M246 exit confirmations | {exit_hysteresis.get('consecutive_exit_confirmations') or 0} / {exit_hysteresis.get('exit_confirmations_required') or 0} |",
        "",
        "## Candidate capacity",
        "",
        "| Symbol | Action | Ticket | Source qty | Max feasible qty | Capital status | Execution status | Binding constraint | Broker what-if | Reasons |",
        "| --- | --- | --- | ---: | ---: | --- | --- | --- | --- | --- |",
    ]
    for item in report.get("sample_validation_items") or []:
        reasons = ", ".join(item.get("reasons") or []) or "-"
        execution_gate = item.get("execution_gate") if isinstance(item.get("execution_gate"), dict) else {}
        execution_reasons = ", ".join(execution_gate.get("execution_block_reasons") or []) or "-"
        broker_state = item.get("broker_whatif_status") or ("available" if item.get("broker_whatif_available") else "missing")
        lines.append(
            f"| `{item.get('symbol')}` | `{item.get('action')}` | `{item.get('ticket_type')}` "
            f"| {_fmt_qty(item.get('source_quantity'))} | {_fmt_qty(item.get('max_feasible_quantity'))} "
            f"| `{item.get('status')}` | `{execution_gate.get('status')}` | `{item.get('binding_constraint')}` | `{broker_state}` | {execution_reasons if execution_reasons != '-' else reasons} |"
        )
    if not report.get("sample_validation_items"):
        lines.append("| - | - | - | - | - | - | - | - | - | - |")
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "M245 separates capital-feasible from trade-executable. M246 adds a micro cash-only policy so tiny-account/cash-only mode can still allow small fractional stock-long candidates while blocking short-stock, margin-heavy, and insufficient-option-budget expressions.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_live_read_only_capital_validation_json(path: Path, report: dict[str, Any]) -> Path:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def write_live_read_only_capital_validation_markdown(path: Path, report: dict[str, Any]) -> Path:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(live_read_only_capital_validation_markdown(report), encoding="utf-8")
    return path
