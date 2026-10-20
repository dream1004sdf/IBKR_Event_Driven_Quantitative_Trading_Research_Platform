from __future__ import annotations

import json
import logging
import math
import threading
import time
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


OPTION_WHATIF_SAFETY_VALIDATION_VERSION = "m249-option-whatif-safety-validation-v2-local-combo-risk-cap"


@dataclass(frozen=True)
class OptionTicketWhatIfConfig:
    host: str = "127.0.0.1"
    port: int = 4001
    client_id: int = 9921
    timeout_seconds: float = 20.0
    max_tickets: int = 3


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


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


def _positive(value: Any) -> float:
    parsed = _as_float(value)
    return max(0.0, parsed or 0.0)


def _result(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    result = payload.get("result")
    return result if isinstance(result, dict) else payload


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _age_seconds(value: Any, *, now: datetime) -> float | None:
    parsed = _parse_dt(value)
    if parsed is None:
        return None
    return max(0.0, (now - parsed).total_seconds())


def _capital_budgets(capital_state_payload: dict[str, Any] | None) -> dict[str, float]:
    capital = _result(capital_state_payload or {})
    budgets = capital.get("budgets") if isinstance(capital.get("budgets"), dict) else {}
    return {
        "option_premium_budget": _positive(budgets.get("options_premium_budget")),
        "cash_budget": _positive(budgets.get("deployable_cash_after_reserve")),
        "core_risk_budget": _positive(budgets.get("core_risk_budget")),
        "new_initial_margin_budget": _positive(budgets.get("max_new_initial_margin")),
    }


def _quote_snapshot_freshness(
    option_quote_snapshot_payload: dict[str, Any] | None,
    *,
    max_quote_age_minutes: float,
    now: datetime,
) -> dict[str, Any]:
    snapshot = _result(option_quote_snapshot_payload or {})
    generated_at = snapshot.get("generated_at")
    age = _age_seconds(generated_at, now=now)
    return {
        "generated_at": generated_at,
        "age_seconds": round(age, 3) if age is not None else None,
        "fresh": bool(age is not None and age <= float(max_quote_age_minutes) * 60.0),
        "max_quote_age_minutes": float(max_quote_age_minutes),
        "status_counts": snapshot.get("status_counts") or {},
        "quoted_rate": snapshot.get("quoted_rate"),
    }


def _option_session_context(supervisor_payload: dict[str, Any] | None) -> dict[str, Any]:
    supervisor = _result(supervisor_payload or {})
    session = str(supervisor.get("final_session") or "").lower()
    quote_allowed = supervisor.get("final_option_quote_allowed")
    expected_deferred = bool(quote_allowed is False or session in {"closed", "pre_market", "post_market", "holiday", "weekend", "overnight"})
    return {
        "final_status": supervisor.get("final_status"),
        "final_session": supervisor.get("final_session"),
        "final_option_quote_allowed": quote_allowed,
        "final_quote_fresh": supervisor.get("final_quote_fresh"),
        "final_quote_usable": supervisor.get("final_quote_usable"),
        "final_quote_quality_status": supervisor.get("final_quote_quality_status"),
        "expected_deferred": expected_deferred,
    }


def _ticket_leg(ticket: dict[str, Any]) -> dict[str, Any]:
    legs = ticket.get("legs") if isinstance(ticket.get("legs"), list) else []
    return legs[0] if legs and isinstance(legs[0], dict) else {}


def _ticket_legs(ticket: dict[str, Any]) -> list[dict[str, Any]]:
    return [row for row in (ticket.get("legs") if isinstance(ticket.get("legs"), list) else []) if isinstance(row, dict)]


def _contract(leg: dict[str, Any]) -> dict[str, Any]:
    contract = leg.get("contract") if isinstance(leg.get("contract"), dict) else {}
    return contract


def _order(ticket: dict[str, Any]) -> dict[str, Any]:
    order = ticket.get("order_preview") if isinstance(ticket.get("order_preview"), dict) else {}
    return order


def _quote(leg: dict[str, Any]) -> dict[str, Any]:
    quote = leg.get("quote") if isinstance(leg.get("quote"), dict) else {}
    return quote


def _contract_complete(contract: dict[str, Any]) -> bool:
    required = ("symbol", "secType", "exchange", "currency", "lastTradeDateOrContractMonth", "right", "multiplier")
    return all(str(contract.get(key) or "").strip() for key in required) and _as_float(contract.get("strike")) is not None


def _is_defined_risk_combo(ticket: dict[str, Any]) -> bool:
    ticket_type = str(ticket.get("ticket_type") or "").lower()
    strategy = str(ticket.get("strategy") or "").lower()
    legs = _ticket_legs(ticket)
    actions = {str(leg.get("action") or "").upper() for leg in legs}
    return bool(
        len(legs) == 2
        and ("spread" in ticket_type or "spread" in strategy)
        and {"BUY_TO_OPEN", "SELL_TO_OPEN"}.issubset(actions)
    )


def _leg_combo_action(action: Any) -> str:
    action_text = str(action or "").upper()
    if action_text in {"SELL_TO_OPEN", "SELL_TO_CLOSE", "SELL"}:
        return "SELL"
    return "BUY"


def _broker_combo_whatif_preparation(ticket: dict[str, Any]) -> dict[str, Any]:
    legs = _ticket_legs(ticket)
    if len(legs) <= 1:
        return {
            "ticket_id": ticket.get("ticket_id"),
            "status": "not_applicable",
            "prepared": False,
            "reason": "single_leg_ticket",
            "leg_count": len(legs),
        }
    missing: list[str] = []
    combo_legs: list[dict[str, Any]] = []
    for index, leg in enumerate(legs, start=1):
        contract = _contract(leg)
        con_id = contract.get("conId") if contract.get("conId") is not None else contract.get("conid")
        parsed_con_id = _as_float(con_id)
        if parsed_con_id is None or parsed_con_id <= 0:
            missing.append(f"leg_{index}_contract_conId")
        ratio = int(_positive(leg.get("ratio")) or _positive(leg.get("quantity")) or 1)
        combo_legs.append(
            {
                "leg_id": leg.get("leg_id") or f"leg_{index}",
                "conId": int(parsed_con_id) if parsed_con_id is not None and parsed_con_id > 0 else None,
                "ratio": max(1, ratio),
                "action": _leg_combo_action(leg.get("action")),
                "exchange": str(contract.get("exchange") or "SMART"),
                "contract": {
                    "symbol": contract.get("symbol"),
                    "lastTradeDateOrContractMonth": contract.get("lastTradeDateOrContractMonth"),
                    "strike": contract.get("strike"),
                    "right": contract.get("right"),
                    "tradingClass": contract.get("tradingClass"),
                },
            }
        )
    underlying = ticket.get("underlying") if isinstance(ticket.get("underlying"), dict) else {}
    combo_contract = {
        "symbol": str(underlying.get("symbol") or (_contract(legs[0]).get("symbol") if legs else "") or "").upper(),
        "secType": "BAG",
        "exchange": "SMART",
        "currency": str((_contract(legs[0]).get("currency") if legs else None) or "USD"),
        "comboLegs": combo_legs,
    }
    defined_risk_combo = _is_defined_risk_combo(ticket)
    if not defined_risk_combo:
        missing.append("defined_risk_combo_structure")
    missing = list(dict.fromkeys(missing))
    return {
        "ticket_id": ticket.get("ticket_id"),
        "status": "prepared" if not missing else "missing_contract_details",
        "prepared": not missing,
        "reason": None if not missing else missing[0],
        "missing_fields": missing,
        "leg_count": len(legs),
        "defined_risk_combo": defined_risk_combo,
        "broker_combo_whatif_enabled": False,
        "combo_contract_preview": combo_contract,
        "safety_note": "Preparation only; broker combo what-if execution remains disabled.",
    }


def _max_leg_spread_pct(legs: list[dict[str, Any]]) -> float | None:
    spreads = []
    for leg in legs:
        quote = _quote(leg)
        parsed = _as_float(quote.get("spread_pct"))
        if parsed is not None:
            spreads.append(parsed)
    return max(spreads) if spreads else None


def _all_quotes_usable(legs: list[dict[str, Any]]) -> bool:
    if not legs:
        return False
    for leg in legs:
        quote = _quote(leg)
        if str(quote.get("quote_status") or "") != "quoted":
            return False
        if _as_float(quote.get("bid")) is None or _as_float(quote.get("ask")) is None:
            return False
        if bool(quote.get("delayed")):
            return False
    return True


def _local_checks(
    ticket: dict[str, Any],
    *,
    budgets: dict[str, float],
    max_spread_pct: float,
    require_fresh_quote: bool,
    quote_freshness: dict[str, Any],
    session_context: dict[str, Any],
) -> dict[str, Any]:
    legs = _ticket_legs(ticket)
    leg = legs[0] if legs else {}
    contract = _contract(leg)
    order = _order(ticket)
    quote = _quote(leg)
    estimated_debit = _positive(order.get("estimated_total_debit"))
    max_loss = _positive(order.get("estimated_max_loss"))
    spread_pct = _max_leg_spread_pct(legs)
    risk_preview = ticket.get("risk_preview") if isinstance(ticket.get("risk_preview"), dict) else {}
    max_ticket_debit = _positive(risk_preview.get("max_ticket_debit"))
    defined_risk_combo = _is_defined_risk_combo(ticket)
    all_contracts_complete = bool(legs) and all(_contract_complete(_contract(row)) for row in legs)
    all_quotes_usable = _all_quotes_usable(legs)
    checks = {
        "ticket_schema": {
            "passed": bool(ticket.get("ticket_id") and str(ticket.get("ticket_type") or "") and isinstance(ticket.get("legs"), list)),
            "reason": None,
        },
        "legs_supported": {
            "passed": len(legs) == 1 or defined_risk_combo,
            "reason": None,
        },
        "contract_complete": {
            "passed": all_contracts_complete,
            "reason": None,
        },
        "order_preview_safe": {
            "passed": (
                str(order.get("order_type") or "").upper() == "LMT"
                and str(order.get("side") or "").upper() == "BUY"
                and _as_float(order.get("limit_price")) is not None
                and not bool(order.get("transmit"))
            ),
            "reason": None,
            "details": {
                "order_type": order.get("order_type"),
                "side": order.get("side"),
                "limit_price": order.get("limit_price"),
                "transmit": order.get("transmit"),
            },
        },
        "quote_usable": {
            "passed": all_quotes_usable,
            "reason": None,
            "details": {"leg_count": len(legs), "first_quote": quote},
        },
        "spread_within_limit": {
            "passed": spread_pct is not None and spread_pct <= float(max_spread_pct),
            "reason": None,
            "details": {"spread_pct": spread_pct, "max_spread_pct": float(max_spread_pct)},
        },
        "quote_snapshot_fresh": {
            "passed": (not require_fresh_quote) or bool(quote_freshness.get("fresh")),
            "reason": None,
            "details": quote_freshness,
        },
        "option_session_usable": {
            "passed": not bool(session_context.get("expected_deferred")),
            "reason": None,
            "details": session_context,
        },
        "option_premium_budget": {
            "passed": budgets["option_premium_budget"] <= 0 or estimated_debit <= budgets["option_premium_budget"] + 1e-9,
            "reason": None,
            "details": {"estimated_debit": estimated_debit, "budget": budgets["option_premium_budget"]},
        },
        "cash_budget": {
            "passed": budgets["cash_budget"] <= 0 or estimated_debit <= budgets["cash_budget"] + 1e-9,
            "reason": None,
            "details": {"estimated_debit": estimated_debit, "budget": budgets["cash_budget"]},
        },
        "max_loss_budget": {
            "passed": budgets["core_risk_budget"] <= 0 or max_loss <= budgets["core_risk_budget"] + 1e-9,
            "reason": None,
            "details": {"max_loss": max_loss, "budget": budgets["core_risk_budget"]},
        },
        "max_ticket_debit": {
            "passed": max_ticket_debit <= 0 or max_loss <= max_ticket_debit + 1e-9,
            "reason": None,
            "details": {"max_loss": max_loss, "max_ticket_debit": max_ticket_debit},
        },
        "defined_risk_combo": {
            "passed": len(legs) == 1 or (defined_risk_combo and max_loss > 0 and estimated_debit <= max_loss + 1e-9),
            "reason": None,
            "details": {"defined_risk_combo": defined_risk_combo, "leg_count": len(legs), "max_loss": max_loss},
        },
    }
    reason_map = {
        "ticket_schema": "invalid_ticket_schema",
        "legs_supported": "unsupported_option_leg_structure",
        "contract_complete": "incomplete_option_contract",
        "order_preview_safe": "unsafe_or_incomplete_order_preview",
        "quote_usable": "option_quote_not_usable",
        "spread_within_limit": "option_spread_above_limit",
        "quote_snapshot_fresh": "option_quote_snapshot_not_fresh",
        "option_session_usable": "option_market_closed_or_quotes_deferred",
        "option_premium_budget": "option_premium_budget_exceeded",
        "cash_budget": "cash_budget_exceeded",
        "max_loss_budget": "core_risk_budget_exceeded",
        "max_ticket_debit": "max_ticket_debit_exceeded",
        "defined_risk_combo": "undefined_or_invalid_combo_risk",
    }
    for key, check in checks.items():
        if not check.get("passed"):
            check["reason"] = reason_map[key]
    failed = [str(check.get("reason")) for check in checks.values() if check.get("reason")]
    return {
        "passed": not failed,
        "failed_reasons": failed,
        "checks": checks,
    }


def _whatif_response_key_from_ticket(ticket: dict[str, Any]) -> tuple[str, str, str, float, str]:
    leg = _ticket_leg(ticket)
    contract = _contract(leg)
    return (
        str((ticket.get("underlying") or {}).get("symbol") or contract.get("symbol") or "").upper(),
        str(contract.get("lastTradeDateOrContractMonth") or ""),
        str(contract.get("right") or "").upper(),
        float(_as_float(contract.get("strike")) or 0.0),
        str(contract.get("secType") or "OPT").upper(),
    )


def _whatif_response_key_from_response(response: dict[str, Any]) -> tuple[str, str, str, float, str]:
    contract = response.get("contract") if isinstance(response.get("contract"), dict) else {}
    return (
        str(response.get("ticket_symbol") or response.get("symbol") or contract.get("symbol") or "").upper(),
        str(contract.get("lastTradeDateOrContractMonth") or contract.get("last_trade_date_or_contract_month") or ""),
        str(contract.get("right") or "").upper(),
        float(_as_float(contract.get("strike")) or 0.0),
        str(contract.get("secType") or contract.get("sec_type") or "OPT").upper(),
    )


def _matching_broker_response(ticket: dict[str, Any], broker_whatif_payload: dict[str, Any] | None) -> dict[str, Any] | None:
    payload = _result(broker_whatif_payload or {})
    ticket_id = str(ticket.get("ticket_id") or "")
    candidates: list[dict[str, Any]] = []
    for key in ("ticket_results", "validation_items", "responses", "whatif_results"):
        rows = payload.get(key)
        if isinstance(rows, list):
            candidates.extend(row for row in rows if isinstance(row, dict))
    for row in candidates:
        if ticket_id and str(row.get("ticket_id") or "") == ticket_id:
            return row
    ticket_key = _whatif_response_key_from_ticket(ticket)
    for row in candidates:
        if _whatif_response_key_from_response(row) == ticket_key:
            return row
    return None


def _order_state(response: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(response, dict):
        return {}
    state = response.get("order_state") if isinstance(response.get("order_state"), dict) else {}
    if state:
        return state
    state = response.get("broker_order_state") if isinstance(response.get("broker_order_state"), dict) else {}
    return state


def _whatif_validation(
    ticket: dict[str, Any],
    *,
    broker_response: dict[str, Any] | None,
    require_broker_whatif: bool,
    budgets: dict[str, float],
) -> dict[str, Any]:
    if broker_response is None:
        return {
            "status": "missing_required" if require_broker_whatif else "pending",
            "passed": not require_broker_whatif,
            "required": bool(require_broker_whatif),
            "reason": "broker_whatif_missing",
            "response": None,
        }
    state = _order_state(broker_response)
    warning = str(state.get("warningText") or state.get("warning_text") or "")
    status = str(state.get("status") or broker_response.get("status") or "")
    init_change = _positive(state.get("initMarginChange") if state.get("initMarginChange") is not None else state.get("init_margin_change"))
    debit = _positive((_order(ticket)).get("estimated_total_debit"))
    reasons: list[str] = []
    if status and status.lower() not in {"presubmitted", "submitted", "pendingsubmit", "inactive"}:
        reasons.append("broker_whatif_status_not_accepted")
    if warning:
        reasons.append("broker_whatif_warning_text")
    if debit > 0 and init_change > debit * 1.25:
        reasons.append("broker_whatif_margin_exceeds_long_option_debit")
    if budgets["new_initial_margin_budget"] > 0 and init_change > budgets["new_initial_margin_budget"] + 1e-9 and init_change > debit + 1e-9:
        reasons.append("broker_whatif_margin_budget_exceeded")
    return {
        "status": "passed" if not reasons else "blocked",
        "passed": not reasons,
        "required": bool(require_broker_whatif),
        "reason": None if not reasons else reasons[0],
        "reasons": reasons,
        "response": broker_response,
        "order_state": state,
        "metrics": {
            "init_margin_change": init_change,
            "estimated_debit": debit,
            "warning_text": warning,
            "broker_status": status,
        },
    }


def build_option_whatif_safety_validation_report(
    *,
    paper_option_ticket_preview_payload: dict[str, Any],
    capital_state_payload: dict[str, Any] | None = None,
    option_quote_snapshot_payload: dict[str, Any] | None = None,
    options_market_structure_supervisor_payload: dict[str, Any] | None = None,
    broker_whatif_payload: dict[str, Any] | None = None,
    require_broker_whatif: bool = True,
    max_spread_pct: float = 0.30,
    max_quote_age_minutes: float = 30.0,
    require_fresh_quote: bool = True,
    execute_whatif_refresh: bool = False,
    whatif_config: OptionTicketWhatIfConfig | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    now_utc = (now or datetime.now(UTC)).astimezone(UTC)
    m248 = _result(paper_option_ticket_preview_payload)
    tickets = [row for row in (m248.get("ticket_previews") or []) if isinstance(row, dict)]
    budgets = _capital_budgets(capital_state_payload)
    quote_freshness = _quote_snapshot_freshness(
        option_quote_snapshot_payload,
        max_quote_age_minutes=max_quote_age_minutes,
        now=now_utc,
    )
    session_context = _option_session_context(options_market_structure_supervisor_payload)
    live_whatif = None
    if execute_whatif_refresh and tickets:
        config = whatif_config or OptionTicketWhatIfConfig()
        live_whatif = run_ibkr_option_ticket_whatif(tickets, config)
        broker_whatif_payload = live_whatif
    validation_items: list[dict[str, Any]] = []
    combo_preparations: list[dict[str, Any]] = []
    for ticket in tickets:
        combo_preparation = _broker_combo_whatif_preparation(ticket)
        if combo_preparation.get("status") != "not_applicable":
            combo_preparations.append(combo_preparation)
        local = _local_checks(
            ticket,
            budgets=budgets,
            max_spread_pct=max_spread_pct,
            require_fresh_quote=require_fresh_quote,
            quote_freshness=quote_freshness,
            session_context=session_context,
        )
        response = _matching_broker_response(ticket, broker_whatif_payload)
        whatif = _whatif_validation(ticket, broker_response=response, require_broker_whatif=require_broker_whatif, budgets=budgets)
        local_passed = bool(local.get("passed"))
        whatif_passed = bool(whatif.get("passed")) and whatif.get("status") != "pending"
        if local_passed and whatif_passed:
            status = "passed"
        elif local_passed and whatif.get("status") == "pending":
            status = "whatif_pending"
        else:
            status = "blocked"
        final_paper_executable = bool(status == "passed")
        reasons = list(local.get("failed_reasons") or [])
        if whatif.get("reason"):
            reasons.append(str(whatif.get("reason")))
        validation_items.append(
            {
                "ticket_id": ticket.get("ticket_id"),
                "symbol": (ticket.get("underlying") or {}).get("symbol"),
                "strategy": ticket.get("strategy"),
                "status": status,
                "final_paper_executable": final_paper_executable,
                "local_safety_passed": local_passed,
                "broker_whatif_status": whatif.get("status"),
                "estimated_total_debit": (_order(ticket)).get("estimated_total_debit"),
                "estimated_max_loss": (_order(ticket)).get("estimated_max_loss"),
                "block_reasons": list(dict.fromkeys(reasons)),
                "local_safety": local,
                "broker_whatif": whatif,
                "broker_combo_whatif_preparation": combo_preparation,
                "ticket": ticket,
            }
        )
    status_counts = Counter(row.get("status") for row in validation_items)
    reason_counts = Counter()
    for row in validation_items:
        for reason in row.get("block_reasons") or []:
            reason_counts[reason] += 1
    combo_status_counts = Counter(str(row.get("status") or "unknown") for row in combo_preparations)
    combo_missing_reason_counts = Counter()
    for row in combo_preparations:
        for reason in row.get("missing_fields") or []:
            combo_missing_reason_counts[str(reason)] += 1
    source_expected_deferred = bool(m248.get("expected_deferred") or m248.get("status") == "expected_deferred")
    if not tickets and source_expected_deferred:
        status = "expected_deferred"
        reason_counts["m248_expected_deferred_no_tickets"] += 1
    elif validation_items and all(row.get("status") == "passed" for row in validation_items):
        status = "green"
    elif any(row.get("status") == "blocked" for row in validation_items):
        status = "red"
    elif any(row.get("status") == "whatif_pending" for row in validation_items):
        status = "yellow"
    else:
        status = "red"
        reason_counts["no_m248_ticket_previews"] += 1
    return {
        "option_whatif_safety_validation_version": OPTION_WHATIF_SAFETY_VALIDATION_VERSION,
        "generated_at": now_utc.isoformat(),
        "status": status,
        "expected_deferred": bool(status == "expected_deferred"),
        "mode": "paper_option_ticket_whatif_margin_safety_validation",
        "source": {
            "m248_status": m248.get("status"),
            "m248_expected_deferred": m248.get("expected_deferred"),
            "m248_generated_at": m248.get("generated_at"),
            "m248_ticket_count": len(tickets),
        },
        "summary": {
            "validation_item_count": len(validation_items),
            "passed_count": int(status_counts.get("passed", 0)),
            "whatif_pending_count": int(status_counts.get("whatif_pending", 0)),
            "blocked_count": int(status_counts.get("blocked", 0)),
            "status_counts": dict(status_counts),
            "top_block_reason_counts": dict(reason_counts.most_common(12)),
            "require_broker_whatif": bool(require_broker_whatif),
            "execute_whatif_refresh": bool(execute_whatif_refresh),
            "quote_fresh": quote_freshness.get("fresh"),
            "option_session_expected_deferred": session_context.get("expected_deferred"),
            "budgets": budgets,
            "broker_combo_preparation_count": len(combo_preparations),
            "broker_combo_prepared_count": int(combo_status_counts.get("prepared", 0)),
            "broker_combo_missing_contract_detail_count": int(combo_status_counts.get("missing_contract_details", 0)),
        },
        "contexts": {
            "capital_budgets": budgets,
            "quote_snapshot_freshness": quote_freshness,
            "option_session": session_context,
            "broker_whatif_source": {
                "provided": bool(broker_whatif_payload),
                "live_refresh_executed": bool(live_whatif),
                "version": (_result(broker_whatif_payload or {})).get("option_ticket_whatif_version")
                or (_result(broker_whatif_payload or {})).get("whatif_preview_version"),
                "generated_at": (_result(broker_whatif_payload or {})).get("generated_at"),
            },
        },
        "validation_items": validation_items,
        "live_whatif_refresh": live_whatif,
        "broker_combo_whatif_preparation": {
            "enabled": False,
            "scope": "preparation_only_no_broker_submission",
            "preparation_count": len(combo_preparations),
            "prepared_count": int(combo_status_counts.get("prepared", 0)),
            "missing_contract_detail_count": int(combo_status_counts.get("missing_contract_details", 0)),
            "status_counts": dict(combo_status_counts),
            "missing_reason_counts": dict(combo_missing_reason_counts),
            "items": combo_preparations[:20],
            "safety_note": "M249 does not execute broker combo what-if; this only reports whether BAG/ComboLeg inputs are complete.",
        },
        "policy": {
            "single_leg_option_enabled": True,
            "multi_leg_combo_enabled": True,
            "multi_leg_combo_scope": "local_defined_risk_debit_spread_validation_only",
            "broker_combo_whatif_enabled": False,
            "fresh_quote_required": bool(require_fresh_quote),
            "max_spread_pct": float(max_spread_pct),
            "max_quote_age_minutes": float(max_quote_age_minutes),
            "broker_whatif_required_for_final_pass": bool(require_broker_whatif),
            "execute_whatif_refresh_requires_explicit_flag": True,
        },
        "safety": {
            "read_only_or_paper_validation": True,
            "ibkr_order_api_called_by_m249": bool(live_whatif and (live_whatif.get("requested_count") or 0) > 0),
            "what_if_only": True,
            "ibkr_whatif_api_transmit_flag_may_be_true_when_executed": bool(live_whatif),
            "live_order_transmitted": False,
            "ibkr_order_submitted": False,
            "transmit_enabled_for_live_orders": False,
            "live_routing_enabled": False,
            "portfolio_mutated": False,
            "live_thresholds_mutated": False,
        },
    }


def _ibkr_contract_from_ticket(ticket: dict[str, Any]) -> Any | None:
    from ibapi.contract import Contract

    if len(_ticket_legs(ticket)) != 1:
        return None
    leg = _ticket_leg(ticket)
    contract_payload = _contract(leg)
    if not _contract_complete(contract_payload):
        return None
    contract = Contract()
    contract.symbol = str(contract_payload.get("symbol") or "").upper()
    contract.secType = str(contract_payload.get("secType") or "OPT").upper()
    contract.exchange = str(contract_payload.get("exchange") or "SMART")
    contract.currency = str(contract_payload.get("currency") or "USD")
    contract.lastTradeDateOrContractMonth = str(contract_payload.get("lastTradeDateOrContractMonth") or "")
    contract.strike = float(_as_float(contract_payload.get("strike")) or 0.0)
    contract.right = str(contract_payload.get("right") or "").upper()
    contract.multiplier = str(contract_payload.get("multiplier") or "100")
    if contract_payload.get("tradingClass"):
        contract.tradingClass = str(contract_payload.get("tradingClass"))
    return contract


def _ibkr_order_from_ticket(ticket: dict[str, Any]) -> Any | None:
    from ibapi.order import Order

    if len(_ticket_legs(ticket)) != 1:
        return None
    leg = _ticket_leg(ticket)
    order_preview = _order(ticket)
    quantity = _positive(leg.get("quantity"))
    limit_price = _as_float(order_preview.get("limit_price"))
    if quantity <= 0 or limit_price is None or limit_price <= 0:
        return None
    order = Order()
    order.action = "BUY"
    order.orderType = "LMT"
    order.totalQuantity = int(quantity)
    order.lmtPrice = float(limit_price)
    order.tif = str(order_preview.get("tif") or "DAY")
    order.outsideRth = bool(order_preview.get("outside_rth") or False)
    # IBKR requires transmit=True for broker-side what-if calculation, but
    # order.whatIf=True prevents live routing. M249 keeps this behind an
    # explicit operator flag and reports it separately from live transmit.
    order.transmit = True
    order.whatIf = True
    order.eTradeOnly = False
    order.firmQuoteOnly = False
    return order


def _ibkr_contract_payload(contract: Any) -> dict[str, Any]:
    return {
        "symbol": getattr(contract, "symbol", None),
        "secType": getattr(contract, "secType", None),
        "exchange": getattr(contract, "exchange", None),
        "currency": getattr(contract, "currency", None),
        "lastTradeDateOrContractMonth": getattr(contract, "lastTradeDateOrContractMonth", None),
        "strike": getattr(contract, "strike", None),
        "right": getattr(contract, "right", None),
        "multiplier": getattr(contract, "multiplier", None),
        "tradingClass": getattr(contract, "tradingClass", None),
    }


def _ibkr_order_payload(order: Any) -> dict[str, Any]:
    return {
        "action": getattr(order, "action", None),
        "order_type": getattr(order, "orderType", None),
        "total_quantity": getattr(order, "totalQuantity", None),
        "limit_price": getattr(order, "lmtPrice", None),
        "tif": getattr(order, "tif", None),
        "what_if": bool(getattr(order, "whatIf", False)),
        "transmit": bool(getattr(order, "transmit", False)),
    }


def _ibkr_order_state_payload(order_state: Any) -> dict[str, Any]:
    keys = [
        "status",
        "initMarginBefore",
        "maintMarginBefore",
        "equityWithLoanBefore",
        "initMarginChange",
        "maintMarginChange",
        "equityWithLoanChange",
        "initMarginAfter",
        "maintMarginAfter",
        "equityWithLoanAfter",
        "commission",
        "minCommission",
        "maxCommission",
        "commissionCurrency",
        "warningText",
    ]
    return {key: getattr(order_state, key, None) for key in keys}


class IBKROptionTicketWhatIfApp:  # wrapper composed dynamically to keep imports local
    def __init__(self, config: OptionTicketWhatIfConfig, tickets: list[dict[str, Any]]):
        from ibapi.client import EClient
        from ibapi.wrapper import EWrapper

        class _App(EWrapper, EClient):
            pass

        self._app = _App()
        EClient.__init__(self._app, self._app)
        self.config = config
        self.tickets = tickets[: max(0, int(config.max_tickets))]
        self.log = logging.getLogger(self.__class__.__name__)
        self._thread: threading.Thread | None = None
        self._connected = threading.Event()
        self._next_order_id: int | None = None
        self._requests: dict[int, dict[str, Any]] = {}
        self._responses: dict[int, dict[str, Any]] = {}
        self._statuses: list[dict[str, Any]] = []
        self._errors: list[dict[str, Any]] = []
        self._skipped: list[dict[str, Any]] = []
        self._install_callbacks()

    def _install_callbacks(self) -> None:
        def next_valid_id(order_id: int) -> None:
            self._next_order_id = int(order_id)
            self._connected.set()
            current = int(order_id)
            for ticket in self.tickets:
                contract = _ibkr_contract_from_ticket(ticket)
                order = _ibkr_order_from_ticket(ticket)
                if contract is None or order is None:
                    self._skipped.append({"ticket_id": ticket.get("ticket_id"), "reason": "unsupported_or_incomplete_option_ticket"})
                    continue
                order.orderId = current
                self._requests[current] = {
                    "order_id": current,
                    "ticket_id": ticket.get("ticket_id"),
                    "ticket_symbol": (ticket.get("underlying") or {}).get("symbol"),
                    "contract": _ibkr_contract_payload(contract),
                    "order": _ibkr_order_payload(order),
                    "submitted_at": _iso_now(),
                }
                self._app.placeOrder(current, contract, order)
                current += 1

        def error(req_id: int, code: int, message: str, advanced_order_reject_json: str = "") -> None:
            self._errors.append(
                {
                    "request_id": req_id,
                    "code": code,
                    "message": message,
                    "advanced_order_reject_json": advanced_order_reject_json or None,
                    "received_at": _iso_now(),
                }
            )

        def open_order(order_id: int, contract: Any, order: Any, order_state: Any) -> None:
            request = self._requests.get(int(order_id), {})
            self._responses[int(order_id)] = {
                "order_id": int(order_id),
                "ticket_id": request.get("ticket_id"),
                "ticket_symbol": request.get("ticket_symbol"),
                "contract": _ibkr_contract_payload(contract),
                "order": _ibkr_order_payload(order),
                "order_state": _ibkr_order_state_payload(order_state),
                "received_at": _iso_now(),
            }

        def order_status(
            order_id: int,
            status: str,
            filled: float,
            remaining: float,
            avg_fill_price: float,
            perm_id: int,
            parent_id: int,
            last_fill_price: float,
            client_id: int,
            why_held: str,
            market_cap_price: float,
        ) -> None:
            self._statuses.append(
                {
                    "order_id": int(order_id),
                    "status": status,
                    "filled": filled,
                    "remaining": remaining,
                    "avg_fill_price": avg_fill_price,
                    "perm_id": perm_id,
                    "client_id": client_id,
                    "why_held": why_held,
                    "market_cap_price": market_cap_price,
                    "received_at": _iso_now(),
                }
            )

        self._app.nextValidId = next_valid_id
        self._app.error = error
        self._app.openOrder = open_order
        self._app.orderStatus = order_status

    def start(self) -> None:
        self._app.connect(self.config.host, int(self.config.port), int(self.config.client_id))
        self._thread = threading.Thread(target=self._app.run, daemon=True)
        self._thread.start()

    def wait(self) -> None:
        deadline = time.monotonic() + float(self.config.timeout_seconds)
        self._connected.wait(timeout=min(5.0, float(self.config.timeout_seconds)))
        while time.monotonic() < deadline:
            if self._requests and len(self._responses) >= len(self._requests):
                break
            time.sleep(0.25)

    def stop(self) -> None:
        try:
            self._app.disconnect()
        finally:
            if self._thread and self._thread.is_alive():
                self._thread.join(timeout=2.0)

    def summary(self) -> dict[str, Any]:
        return {
            "option_ticket_whatif_version": "m249-ibkr-option-ticket-whatif-v1",
            "generated_at": _iso_now(),
            "requested_count": len(self._requests),
            "response_count": len(self._responses),
            "skipped_count": len(self._skipped),
            "requests": list(self._requests.values()),
            "responses": list(self._responses.values()),
            "order_statuses": self._statuses,
            "skipped_items": self._skipped,
            "errors": self._errors,
            "config": {
                "host": self.config.host,
                "port": self.config.port,
                "client_id": self.config.client_id,
                "timeout_seconds": self.config.timeout_seconds,
                "max_tickets": self.config.max_tickets,
            },
            "safety": {
                "ibkr_order_api_called": bool(self._requests),
                "what_if_only": True,
                "ibkr_whatif_api_transmit_flag": True,
                "live_order_transmitted": False,
                "ibkr_order_submitted": False,
            },
        }


def run_ibkr_option_ticket_whatif(tickets: list[dict[str, Any]], config: OptionTicketWhatIfConfig) -> dict[str, Any]:
    app = IBKROptionTicketWhatIfApp(config, tickets)
    app.start()
    try:
        app.wait()
        return app.summary()
    finally:
        app.stop()


def _fmt_money(value: Any) -> str:
    parsed = _as_float(value)
    if parsed is None:
        return "n/a"
    return f"${parsed:,.2f}"


def option_whatif_safety_validation_markdown(report: dict[str, Any]) -> str:
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    source = report.get("source") if isinstance(report.get("source"), dict) else {}
    combo_prep = report.get("broker_combo_whatif_preparation") if isinstance(report.get("broker_combo_whatif_preparation"), dict) else {}
    lines = [
        "# M249 Option What-If / Margin / Safety Validation",
        "",
        f"- Generated: `{report.get('generated_at')}`",
        f"- Status: `{report.get('status')}`",
        f"- Mode: `{report.get('mode')}`",
        f"- M248 source: `{source.get('m248_status')}` tickets `{source.get('m248_ticket_count')}` expected deferred `{source.get('m248_expected_deferred')}`",
        f"- Validation passed/pending/blocked: `{summary.get('passed_count')}` / `{summary.get('whatif_pending_count')}` / `{summary.get('blocked_count')}`",
        f"- Broker what-if required: `{summary.get('require_broker_whatif')}` executed now `{summary.get('execute_whatif_refresh')}`",
        f"- Broker combo what-if prep: enabled `{combo_prep.get('enabled')}` prepared `{combo_prep.get('prepared_count')}` missing-details `{combo_prep.get('missing_contract_detail_count')}`",
        f"- Quote fresh/session deferred: `{summary.get('quote_fresh')}` / `{summary.get('option_session_expected_deferred')}`",
        f"- Top block reasons: `{summary.get('top_block_reason_counts') or {}}`",
        "- Safety: read-only/paper validation; no live order transmitted, no live routing, no portfolio mutation.",
        "",
        "## Validation items",
        "",
        "| Status | Executable | Ticket | Symbol | Strategy | Debit | Max loss | Broker what-if | Reasons |",
        "| --- | --- | --- | --- | --- | ---: | ---: | --- | --- |",
    ]
    for row in report.get("validation_items") or []:
        reasons = ", ".join(row.get("block_reasons") or []) or "-"
        lines.append(
            f"| `{row.get('status')}` | `{row.get('final_paper_executable')}` | `{row.get('ticket_id')}` | "
            f"`{row.get('symbol')}` | `{row.get('strategy')}` | {_fmt_money(row.get('estimated_total_debit'))} | "
            f"{_fmt_money(row.get('estimated_max_loss'))} | `{row.get('broker_whatif_status')}` | {reasons.replace('|', '/')} |"
        )
    if not report.get("validation_items"):
        lines.append("| - | - | - | - | - | - | - | - | No M248 ticket previews to validate. |")
    return "\n".join(lines) + "\n"


def write_option_whatif_safety_validation_json(path: Path, report: dict[str, Any]) -> Path:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return resolved


def write_option_whatif_safety_validation_markdown(path: Path, report: dict[str, Any]) -> Path:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(option_whatif_safety_validation_markdown(report), encoding="utf-8")
    return resolved
