from __future__ import annotations

import json
import math
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


PAPER_OPTION_TICKET_PREVIEW_VERSION = "m248-paper-option-ticket-preview-v2-risk-cap-multileg"


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


def _capital_option_budget(capital_state_payload: dict[str, Any] | None, m247_payload: dict[str, Any]) -> float:
    summary = m247_payload.get("summary") if isinstance(m247_payload.get("summary"), dict) else {}
    budget = _as_float(summary.get("option_budget"))
    if budget is not None:
        return max(0.0, budget)
    capital = _result(capital_state_payload or {})
    return _positive((capital.get("budgets") or {}).get("options_premium_budget"))


def _m247_max_ticket_debit(m247_payload: dict[str, Any]) -> float:
    summary = m247_payload.get("summary") if isinstance(m247_payload.get("summary"), dict) else {}
    parsed = _as_float(summary.get("max_option_ticket_debit"))
    if parsed is not None and parsed > 0:
        return parsed
    contexts = m247_payload.get("contexts") if isinstance(m247_payload.get("contexts"), dict) else {}
    micro = contexts.get("micro_policy") if isinstance(contexts.get("micro_policy"), dict) else {}
    thresholds = micro.get("thresholds") if isinstance(micro.get("thresholds"), dict) else {}
    return _positive(thresholds.get("max_option_ticket_debit"))


def _strategy_action(strategy: str, right: str) -> str:
    strategy = str(strategy or "").lower()
    right = str(right or "").upper()
    if strategy in {"long_put", "long_call"} or right in {"C", "P"}:
        return "BUY_TO_OPEN"
    return "UNKNOWN"


def _contract_from_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    contract = candidate.get("candidate_contract") if isinstance(candidate.get("candidate_contract"), dict) else {}
    if contract:
        return {
            "symbol": str(contract.get("symbol") or candidate.get("symbol") or "").upper(),
            "secType": str(contract.get("secType") or "OPT"),
            "exchange": str(contract.get("exchange") or candidate.get("exchange") or "SMART"),
            "currency": str(contract.get("currency") or "USD"),
            "lastTradeDateOrContractMonth": str(
                contract.get("lastTradeDateOrContractMonth") or candidate.get("expiration") or ""
            ),
            "strike": _as_float(contract.get("strike") if contract.get("strike") is not None else candidate.get("strike")),
            "right": str(contract.get("right") or candidate.get("right") or "").upper(),
            "multiplier": str(contract.get("multiplier") or "100"),
            "tradingClass": str(contract.get("tradingClass") or candidate.get("trading_class") or candidate.get("symbol") or "").upper(),
        }
    return {
        "symbol": str(candidate.get("symbol") or "").upper(),
        "secType": "OPT",
        "exchange": str(candidate.get("exchange") or "SMART"),
        "currency": "USD",
        "lastTradeDateOrContractMonth": str(candidate.get("expiration") or ""),
        "strike": _as_float(candidate.get("strike")),
        "right": str(candidate.get("right") or "").upper(),
        "multiplier": "100",
        "tradingClass": str(candidate.get("trading_class") or candidate.get("symbol") or "").upper(),
    }


def _limit_price_for_buy(candidate: dict[str, Any], *, policy: str) -> float | None:
    ask = _as_float(candidate.get("ask"))
    mid = _as_float(candidate.get("mid"))
    bid = _as_float(candidate.get("bid"))
    policy = str(policy or "conservative_debit").lower()
    if policy == "midpoint" and mid is not None and mid > 0:
        return round(mid, 2)
    if policy == "mid_to_ask" and ask is not None and ask > 0:
        if mid is not None and mid > 0:
            return round(min(ask, mid + max(0.01, (ask - (bid or mid)) * 0.50)), 2)
        return round(ask, 2)
    if ask is not None and ask > 0:
        return round(ask, 2)
    if mid is not None and mid > 0:
        return round(mid, 2)
    return None


def _ticket_block_reasons(
    candidate: dict[str, Any],
    *,
    require_fresh_quote: bool,
    option_budget: float,
    quantity: int,
    limit_price: float | None,
    max_ticket_debit: float,
) -> list[str]:
    reasons: list[str] = []
    candidate_status = str(candidate.get("candidate_status") or "").lower()
    if candidate_status != "ready" or not bool(candidate.get("paper_candidate_ready")):
        reasons.append("m247_candidate_not_ready")
    if require_fresh_quote and bool(candidate.get("requires_fresh_quote")):
        reasons.append("fresh_quote_required")
    if "quote_snapshot_stale" in set(candidate.get("reasons") or []):
        reasons.append("quote_snapshot_stale")
    if bool(candidate.get("delayed")):
        reasons.append("delayed_option_quote")
    if limit_price is None or limit_price <= 0:
        reasons.append("missing_limit_price")
    total_debit = (limit_price or 0.0) * 100.0 * max(1, int(quantity))
    if option_budget > 0 and total_debit > option_budget + 1e-9:
        reasons.append("ticket_debit_exceeds_option_budget")
    if max_ticket_debit > 0 and total_debit > max_ticket_debit + 1e-9:
        reasons.append("ticket_debit_exceeds_max_ticket_debit")
    if str(candidate.get("quote_status") or "") != "quoted":
        reasons.append("quote_not_quoted")
    contract = _contract_from_candidate(candidate)
    for field in ("symbol", "lastTradeDateOrContractMonth", "right"):
        if not contract.get(field):
            reasons.append(f"missing_contract_{field}")
    if contract.get("strike") is None:
        reasons.append("missing_contract_strike")
    return list(dict.fromkeys(reasons))


def _build_ticket(
    candidate: dict[str, Any],
    *,
    ticket_index: int,
    option_budget: float,
    quantity: int,
    limit_price_policy: str,
    require_fresh_quote: bool,
    max_ticket_debit: float,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    quantity = max(1, int(quantity))
    gate_severity = candidate.get("gate_severity") if isinstance(candidate.get("gate_severity"), dict) else {}
    parsed_gate_size_multiplier = _as_float(gate_severity.get("recommended_size_multiplier"))
    gate_size_multiplier = max(0.0, min(1.0, parsed_gate_size_multiplier if parsed_gate_size_multiplier is not None else 1.0))
    effective_max_ticket_debit = max_ticket_debit
    if max_ticket_debit > 0 and gate_size_multiplier < 1.0:
        effective_max_ticket_debit = max(5.0, round(float(max_ticket_debit) * gate_size_multiplier, 2))
    strategy = str(candidate.get("strategy") or "")
    right = str(candidate.get("right") or "").upper()
    action = _strategy_action(strategy, right)
    limit_price = _limit_price_for_buy(candidate, policy=limit_price_policy)
    multiplier = 100.0
    total_debit = round((limit_price or 0.0) * multiplier * quantity, 2)
    block_reasons = _ticket_block_reasons(
        candidate,
        require_fresh_quote=require_fresh_quote,
        option_budget=option_budget,
        quantity=quantity,
        limit_price=limit_price,
        max_ticket_debit=effective_max_ticket_debit,
    )
    if gate_size_multiplier <= 0.0:
        block_reasons.append("gate_severity_policy_blocks_new_entries")
    status = "ticket_ready" if not block_reasons and action == "BUY_TO_OPEN" else "ticket_blocked"
    if action != "BUY_TO_OPEN":
        block_reasons.append("unsupported_option_action")
        status = "ticket_blocked"
    contract = _contract_from_candidate(candidate)
    review = {
        "candidate_symbol": candidate.get("symbol"),
        "strategy": strategy,
        "candidate_status": candidate.get("candidate_status"),
        "paper_candidate_ready": bool(candidate.get("paper_candidate_ready")),
        "preview_status": status,
        "score": candidate.get("score"),
        "limit_price": limit_price,
        "total_limit_debit": total_debit,
        "gate_severity_size_multiplier": gate_size_multiplier,
        "effective_max_ticket_debit": effective_max_ticket_debit,
        "block_reasons": block_reasons,
        "source_reasons": candidate.get("reasons") or [],
    }
    if status != "ticket_ready":
        return None, review
    ticket_id = (
        f"m248_{str(candidate.get('symbol') or '').upper()}_"
        f"{str(contract.get('lastTradeDateOrContractMonth') or '')}_"
        f"{str(contract.get('right') or '')}{str(contract.get('strike') or '').replace('.', '_')}_"
        f"{ticket_index:02d}"
    )
    ticket = {
        "ticket_id": ticket_id,
        "ticket_type": "single_long_option",
        "strategy": strategy,
        "source": {
            "milestone": "M247",
            "candidate_symbol": candidate.get("symbol"),
            "candidate_score": candidate.get("score"),
            "direction_context": candidate.get("direction_context"),
            "direction_match": candidate.get("direction_match"),
            "source_reasons": candidate.get("reasons") or [],
            "source_cautions": candidate.get("cautions") or [],
            "gate_severity": gate_severity,
        },
        "underlying": {
            "symbol": str(candidate.get("symbol") or "").upper(),
            "reference_price": candidate.get("reference_price"),
        },
        "legs": [
            {
                "leg_id": "leg_1",
                "action": action,
                "ratio": 1,
                "quantity": quantity,
                "contract": contract,
                "quote": {
                    "bid": candidate.get("bid"),
                    "ask": candidate.get("ask"),
                    "mid": candidate.get("mid"),
                    "spread": candidate.get("spread"),
                    "spread_pct": candidate.get("spread_pct"),
                    "quote_status": candidate.get("quote_status"),
                    "delayed": bool(candidate.get("delayed")),
                },
            }
        ],
        "order_preview": {
            "order_type": "LMT",
            "side": "BUY",
            "tif": "DAY",
            "outside_rth": False,
            "limit_price_policy": limit_price_policy,
            "limit_price": limit_price,
            "estimated_total_debit": total_debit,
            "estimated_max_loss": total_debit,
            "transmit": False,
            "what_if": False,
        },
        "risk_preview": {
            "max_loss": total_debit,
            "option_premium_budget": option_budget,
            "max_ticket_debit": max_ticket_debit,
            "effective_max_ticket_debit": effective_max_ticket_debit,
            "gate_severity_size_multiplier": gate_size_multiplier,
            "budget_remaining_after_ticket": round(max(0.0, option_budget - total_debit), 2),
            "dte": candidate.get("dte"),
            "volume": candidate.get("volume"),
            "open_interest": candidate.get("open_interest"),
            "structure_context": candidate.get("structure_context") or {},
            "micro_policy": candidate.get("micro_policy") or {},
            "requirements": candidate.get("requirements") or {},
        },
        "next_step": "M249_IBKR_what_if_margin_option_safety_validation",
        "safety": {
            "paper_ticket_preview_only": True,
            "ibkr_order_submitted": False,
            "transmit_enabled": False,
            "live_routing_enabled": False,
            "whatif_request_submitted_by_m248": False,
            "portfolio_mutated": False,
        },
    }
    return ticket, review


def _spread_sort_key(candidate: dict[str, Any]) -> tuple[str, str, str, float]:
    return (
        str(candidate.get("symbol") or "").upper(),
        str(candidate.get("expiration") or ""),
        str(candidate.get("right") or "").upper(),
        float(_as_float(candidate.get("strike")) or 0.0),
    )


def _spread_pair_candidates(candidates: list[dict[str, Any]]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    ready = [row for row in candidates if row.get("candidate_status") == "ready" and bool(row.get("paper_candidate_ready"))]
    ready.sort(key=lambda row: (-float(row.get("score") or 0.0), _spread_sort_key(row)))
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for long_leg in ready:
        symbol, expiry, right, strike = _spread_sort_key(long_leg)
        direction = str(long_leg.get("direction_context") or "").lower()
        candidates_same = [
            row
            for row in ready
            if row is not long_leg
            and str(row.get("symbol") or "").upper() == symbol
            and str(row.get("expiration") or "") == expiry
            and str(row.get("right") or "").upper() == right
        ]
        if right == "C" and direction in {"bullish", "two_sided_watch"}:
            short_choices = [row for row in candidates_same if float(_as_float(row.get("strike")) or 0.0) > strike]
            short_choices.sort(key=lambda row: float(_as_float(row.get("strike")) or 0.0))
        elif right == "P" and direction in {"bearish", "risk_reduction", "two_sided_watch"}:
            short_choices = [row for row in candidates_same if float(_as_float(row.get("strike")) or 0.0) < strike]
            short_choices.sort(key=lambda row: -float(_as_float(row.get("strike")) or 0.0))
        else:
            short_choices = []
        if short_choices:
            pairs.append((long_leg, short_choices[0]))
    return pairs


def _build_debit_spread_ticket(
    long_candidate: dict[str, Any],
    short_candidate: dict[str, Any],
    *,
    ticket_index: int,
    option_budget: float,
    quantity: int,
    limit_price_policy: str,
    require_fresh_quote: bool,
    max_ticket_debit: float,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    quantity = max(1, int(quantity))
    long_gate = long_candidate.get("gate_severity") if isinstance(long_candidate.get("gate_severity"), dict) else {}
    short_gate = short_candidate.get("gate_severity") if isinstance(short_candidate.get("gate_severity"), dict) else {}
    parsed_long_gate_size = _as_float(long_gate.get("recommended_size_multiplier"))
    parsed_short_gate_size = _as_float(short_gate.get("recommended_size_multiplier"))
    gate_size_multiplier = min(
        max(0.0, min(1.0, parsed_long_gate_size if parsed_long_gate_size is not None else 1.0)),
        max(0.0, min(1.0, parsed_short_gate_size if parsed_short_gate_size is not None else 1.0)),
    )
    effective_max_ticket_debit = max_ticket_debit
    if max_ticket_debit > 0 and gate_size_multiplier < 1.0:
        effective_max_ticket_debit = max(5.0, round(float(max_ticket_debit) * gate_size_multiplier, 2))
    long_contract = _contract_from_candidate(long_candidate)
    short_contract = _contract_from_candidate(short_candidate)
    long_price = _limit_price_for_buy(long_candidate, policy=limit_price_policy)
    short_bid = _as_float(short_candidate.get("bid"))
    short_mid = _as_float(short_candidate.get("mid"))
    short_credit = short_bid if short_bid is not None and short_bid > 0 else short_mid
    spread_debit = None
    if long_price is not None and short_credit is not None:
        spread_debit = round(max(0.0, long_price - short_credit), 2)
    total_debit = round((spread_debit or 0.0) * 100.0 * quantity, 2)
    width = abs(float(_as_float(long_candidate.get("strike")) or 0.0) - float(_as_float(short_candidate.get("strike")) or 0.0))
    max_loss = total_debit
    max_profit = round(max(0.0, width * 100.0 * quantity - total_debit), 2)
    block_reasons: list[str] = []
    for prefix, candidate in (("long", long_candidate), ("short", short_candidate)):
        if str(candidate.get("candidate_status") or "") != "ready" or not bool(candidate.get("paper_candidate_ready")):
            block_reasons.append(f"{prefix}_leg_m247_candidate_not_ready")
        if require_fresh_quote and bool(candidate.get("requires_fresh_quote")):
            block_reasons.append(f"{prefix}_leg_fresh_quote_required")
        if bool(candidate.get("delayed")):
            block_reasons.append(f"{prefix}_leg_delayed_option_quote")
        if str(candidate.get("quote_status") or "") != "quoted":
            block_reasons.append(f"{prefix}_leg_quote_not_quoted")
    if spread_debit is None or spread_debit <= 0:
        block_reasons.append("missing_or_zero_spread_debit")
    if option_budget > 0 and total_debit > option_budget + 1e-9:
        block_reasons.append("ticket_debit_exceeds_option_budget")
    if gate_size_multiplier <= 0.0:
        block_reasons.append("gate_severity_policy_blocks_new_entries")
    if effective_max_ticket_debit > 0 and max_loss > effective_max_ticket_debit + 1e-9:
        block_reasons.append("ticket_debit_exceeds_max_ticket_debit")
    if width <= 0:
        block_reasons.append("invalid_spread_width")
    block_reasons = list(dict.fromkeys(block_reasons))
    strategy = "bull_call_debit_spread" if str(long_candidate.get("right") or "").upper() == "C" else "bear_put_debit_spread"
    review = {
        "candidate_symbol": long_candidate.get("symbol"),
        "strategy": strategy,
        "candidate_status": "paired",
        "paper_candidate_ready": not block_reasons,
        "preview_status": "ticket_ready" if not block_reasons else "ticket_blocked",
        "score": min(float(long_candidate.get("score") or 0.0), float(short_candidate.get("score") or 0.0)),
        "limit_price": spread_debit,
        "total_limit_debit": total_debit,
        "gate_severity_size_multiplier": gate_size_multiplier,
        "effective_max_ticket_debit": effective_max_ticket_debit,
        "block_reasons": block_reasons,
        "source_reasons": list(dict.fromkeys([*(long_candidate.get("reasons") or []), *(short_candidate.get("reasons") or [])])),
    }
    if block_reasons:
        return None, review
    ticket_id = (
        f"m248_{str(long_candidate.get('symbol') or '').upper()}_"
        f"{str(long_contract.get('lastTradeDateOrContractMonth') or '')}_"
        f"{strategy}_{ticket_index:02d}"
    )
    ticket = {
        "ticket_id": ticket_id,
        "ticket_type": "defined_risk_debit_spread",
        "strategy": strategy,
        "source": {
            "milestone": "M247",
            "candidate_symbol": long_candidate.get("symbol"),
            "candidate_score": review["score"],
            "direction_context": long_candidate.get("direction_context"),
            "direction_match": long_candidate.get("direction_match"),
            "source_reasons": review["source_reasons"],
            "source_cautions": list(dict.fromkeys([*(long_candidate.get("cautions") or []), *(short_candidate.get("cautions") or [])])),
            "gate_severity": {"recommended_size_multiplier": gate_size_multiplier, "long": long_gate, "short": short_gate},
        },
        "underlying": {
            "symbol": str(long_candidate.get("symbol") or "").upper(),
            "reference_price": long_candidate.get("reference_price"),
        },
        "legs": [
            {
                "leg_id": "leg_1_long",
                "action": "BUY_TO_OPEN",
                "ratio": 1,
                "quantity": quantity,
                "contract": long_contract,
                "quote": {
                    "bid": long_candidate.get("bid"),
                    "ask": long_candidate.get("ask"),
                    "mid": long_candidate.get("mid"),
                    "spread": long_candidate.get("spread"),
                    "spread_pct": long_candidate.get("spread_pct"),
                    "quote_status": long_candidate.get("quote_status"),
                    "delayed": bool(long_candidate.get("delayed")),
                },
            },
            {
                "leg_id": "leg_2_short",
                "action": "SELL_TO_OPEN",
                "ratio": 1,
                "quantity": quantity,
                "contract": short_contract,
                "quote": {
                    "bid": short_candidate.get("bid"),
                    "ask": short_candidate.get("ask"),
                    "mid": short_candidate.get("mid"),
                    "spread": short_candidate.get("spread"),
                    "spread_pct": short_candidate.get("spread_pct"),
                    "quote_status": short_candidate.get("quote_status"),
                    "delayed": bool(short_candidate.get("delayed")),
                },
            },
        ],
        "order_preview": {
            "order_type": "LMT",
            "side": "BUY",
            "tif": "DAY",
            "outside_rth": False,
            "limit_price_policy": limit_price_policy,
            "limit_price": spread_debit,
            "estimated_total_debit": total_debit,
            "estimated_max_loss": max_loss,
            "estimated_max_profit": max_profit,
            "spread_width": width,
            "transmit": False,
            "what_if": False,
        },
        "risk_preview": {
            "max_loss": max_loss,
            "max_profit": max_profit,
            "spread_width": width,
            "option_premium_budget": option_budget,
            "max_ticket_debit": max_ticket_debit,
            "effective_max_ticket_debit": effective_max_ticket_debit,
            "gate_severity_size_multiplier": gate_size_multiplier,
            "budget_remaining_after_ticket": round(max(0.0, option_budget - total_debit), 2),
            "defined_risk": True,
            "assignment_risk_note": "Short option leg creates assignment risk; live routing remains disabled until combo what-if and assignment checks pass.",
            "long_leg_requirements": long_candidate.get("requirements") or {},
            "short_leg_requirements": short_candidate.get("requirements") or {},
        },
        "next_step": "M249_local_combo_safety_then_IBKR_combo_what_if_future",
        "safety": {
            "paper_ticket_preview_only": True,
            "ibkr_order_submitted": False,
            "transmit_enabled": False,
            "live_routing_enabled": False,
            "whatif_request_submitted_by_m248": False,
            "portfolio_mutated": False,
        },
    }
    return ticket, review


def build_paper_option_ticket_preview_report(
    *,
    micro_option_candidates_payload: dict[str, Any],
    capital_state_payload: dict[str, Any] | None = None,
    max_tickets: int = 10,
    default_quantity: int = 1,
    limit_price_policy: str = "conservative_debit",
    require_fresh_quote: bool = True,
    max_ticket_debit: float = 0.0,
    enable_debit_spread_previews: bool = True,
    now: datetime | None = None,
) -> dict[str, Any]:
    now_utc = (now or datetime.now(UTC)).astimezone(UTC)
    m247 = _result(micro_option_candidates_payload)
    option_budget = _capital_option_budget(capital_state_payload, m247)
    effective_max_ticket_debit = float(max_ticket_debit) if float(max_ticket_debit or 0.0) > 0 else _m247_max_ticket_debit(m247)
    candidates = [row for row in (m247.get("candidates") or []) if isinstance(row, dict)]
    ticket_previews: list[dict[str, Any]] = []
    candidate_reviews: list[dict[str, Any]] = []
    for candidate in candidates:
        if len(ticket_previews) >= max(0, int(max_tickets)):
            candidate_reviews.append(
                {
                    "candidate_symbol": candidate.get("symbol"),
                    "strategy": candidate.get("strategy"),
                    "candidate_status": candidate.get("candidate_status"),
                    "preview_status": "ticket_skipped",
                    "block_reasons": ["max_ticket_count_reached"],
                    "source_reasons": candidate.get("reasons") or [],
                }
            )
            continue
        ticket, review = _build_ticket(
            candidate,
            ticket_index=len(ticket_previews) + 1,
            option_budget=option_budget,
            quantity=int(default_quantity),
            limit_price_policy=limit_price_policy,
            require_fresh_quote=bool(require_fresh_quote),
            max_ticket_debit=effective_max_ticket_debit,
        )
        candidate_reviews.append(review)
        if ticket is not None:
            ticket_previews.append(ticket)
    if enable_debit_spread_previews and len(ticket_previews) < max(0, int(max_tickets)):
        for long_candidate, short_candidate in _spread_pair_candidates(candidates):
            if len(ticket_previews) >= max(0, int(max_tickets)):
                break
            ticket, review = _build_debit_spread_ticket(
                long_candidate,
                short_candidate,
                ticket_index=len(ticket_previews) + 1,
                option_budget=option_budget,
                quantity=int(default_quantity),
                limit_price_policy=limit_price_policy,
                require_fresh_quote=bool(require_fresh_quote),
                max_ticket_debit=effective_max_ticket_debit,
            )
            candidate_reviews.append(review)
            if ticket is not None:
                ticket_previews.append(ticket)
    status_counts = Counter(row.get("preview_status") for row in candidate_reviews)
    reason_counts = Counter()
    for row in candidate_reviews:
        for reason in row.get("block_reasons") or []:
            reason_counts[reason] += 1
    source_status = str(m247.get("status") or "missing")
    source_expected_deferred = bool(m247.get("expected_deferred"))
    if ticket_previews:
        status = "green"
    elif source_expected_deferred or source_status == "expected_deferred":
        status = "expected_deferred"
        reason_counts["option_market_closed_or_quotes_deferred"] += 1
    elif any(row.get("preview_status") == "ticket_blocked" for row in candidate_reviews):
        status = "red"
    else:
        status = "red"
        reason_counts["no_m247_candidates_available"] += 1
    return {
        "paper_option_ticket_preview_version": PAPER_OPTION_TICKET_PREVIEW_VERSION,
        "generated_at": now_utc.isoformat(),
        "status": status,
        "expected_deferred": bool(status == "expected_deferred"),
        "mode": "paper_option_ticket_preview_only",
        "source": {
            "m247_status": source_status,
            "m247_expected_deferred": source_expected_deferred,
            "m247_generated_at": m247.get("generated_at"),
            "m247_candidate_count": len(candidates),
        },
        "summary": {
            "ticket_count": len(ticket_previews),
            "candidate_review_count": len(candidate_reviews),
            "ready_ticket_count": int(status_counts.get("ticket_ready", 0)),
            "blocked_ticket_count": int(status_counts.get("ticket_blocked", 0)),
            "skipped_ticket_count": int(status_counts.get("ticket_skipped", 0)),
            "status_counts": dict(status_counts),
            "top_block_reason_counts": dict(reason_counts.most_common(12)),
            "option_budget": option_budget,
            "limit_price_policy": limit_price_policy,
            "require_fresh_quote": bool(require_fresh_quote),
            "max_ticket_debit": effective_max_ticket_debit,
            "gate_severity_ticket_haircut_count": sum(
                1
                for row in ticket_previews
                if ((row.get("risk_preview") or {}).get("gate_severity_size_multiplier") or 1.0) < 1.0
            ),
            "default_quantity": int(default_quantity),
            "single_leg_ticket_count": sum(1 for row in ticket_previews if row.get("ticket_type") == "single_long_option"),
            "multi_leg_ticket_count": sum(1 for row in ticket_previews if len(row.get("legs") or []) > 1),
        },
        "ticket_previews": ticket_previews,
        "candidate_reviews": candidate_reviews,
        "policy": {
            "accepted_source_status": "M247 candidate_status=ready and paper_candidate_ready=true",
            "order_type": "LMT",
            "single_leg_quantity": int(default_quantity),
            "limit_price_policy": limit_price_policy,
            "max_tickets": int(max_tickets),
            "effective_max_ticket_debit": effective_max_ticket_debit,
            "fresh_quote_required": bool(require_fresh_quote),
            "whatif_validation_deferred_to": "M249",
            "multi_leg_ready_schema": True,
            "debit_spread_previews_enabled": bool(enable_debit_spread_previews),
        },
        "safety": {
            "paper_ticket_preview_only": True,
            "ibkr_order_submitted": False,
            "transmit_enabled": False,
            "live_routing_enabled": False,
            "whatif_request_submitted_by_m248": False,
            "portfolio_mutated": False,
            "live_thresholds_mutated": False,
        },
    }


def _fmt_money(value: Any) -> str:
    parsed = _as_float(value)
    if parsed is None:
        return "n/a"
    return f"${parsed:,.2f}"


def paper_option_ticket_preview_markdown(report: dict[str, Any]) -> str:
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    source = report.get("source") if isinstance(report.get("source"), dict) else {}
    lines = [
        "# M248 Paper Option Ticket Preview",
        "",
        f"- Generated: `{report.get('generated_at')}`",
        f"- Status: `{report.get('status')}`",
        f"- Mode: `{report.get('mode')}`",
        f"- M247 source: `{source.get('m247_status')}` candidates `{source.get('m247_candidate_count')}` expected deferred `{source.get('m247_expected_deferred')}`",
        f"- Ticket previews: `{summary.get('ticket_count')}` ready / blocked / skipped `{summary.get('ready_ticket_count')}` / `{summary.get('blocked_ticket_count')}` / `{summary.get('skipped_ticket_count')}`",
        f"- Option budget / max ticket debit: `{_fmt_money(summary.get('option_budget'))}` / `{_fmt_money(summary.get('max_ticket_debit'))}`",
        f"- Limit policy: `{summary.get('limit_price_policy')}` fresh quote required `{summary.get('require_fresh_quote')}`",
        f"- Single-leg / multi-leg tickets: `{summary.get('single_leg_ticket_count')}` / `{summary.get('multi_leg_ticket_count')}`",
        f"- Top block reasons: `{summary.get('top_block_reason_counts') or {}}`",
        "- Safety: paper ticket preview only; no IBKR order, no transmit, no live routing, no what-if request in M248.",
        "",
        "## Ticket previews",
        "",
        "| Ticket | Strategy | Underlying | Legs | Qty | Limit | Est. debit/max loss | Next step |",
        "| --- | --- | --- | --- | ---: | ---: | ---: | --- |",
    ]
    for ticket in report.get("ticket_previews") or []:
        legs = ticket.get("legs") if isinstance(ticket.get("legs"), list) else []
        leg = legs[0] if legs and isinstance(legs[0], dict) else {}
        order = ticket.get("order_preview") if isinstance(ticket.get("order_preview"), dict) else {}
        contract_labels = []
        for raw_leg in legs:
            contract = raw_leg.get("contract") if isinstance(raw_leg, dict) and isinstance(raw_leg.get("contract"), dict) else {}
            contract_labels.append(
                f"{raw_leg.get('action')} {contract.get('lastTradeDateOrContractMonth')} "
                f"{contract.get('right')}{contract.get('strike')}"
            )
        contract_label = "<br>".join(contract_labels) if contract_labels else "-"
        lines.append(
            f"| `{ticket.get('ticket_id')}` | `{ticket.get('strategy')}` | `{(ticket.get('underlying') or {}).get('symbol')}` | "
            f"`{contract_label}` | {leg.get('quantity')} | {order.get('limit_price')} | "
            f"{_fmt_money(order.get('estimated_total_debit'))} | `{ticket.get('next_step')}` |"
        )
    if not report.get("ticket_previews"):
        lines.append("| - | - | - | - | - | - | - | No paper option ticket previews. |")
    lines.extend(
        [
            "",
            "## Candidate reviews",
            "",
            "| Preview | Symbol | Strategy | Candidate | Limit | Debit | Reasons |",
            "| --- | --- | --- | --- | ---: | ---: | --- |",
        ]
    )
    for row in report.get("candidate_reviews") or []:
        reasons = ", ".join(row.get("block_reasons") or row.get("source_reasons") or []) or "-"
        lines.append(
            f"| `{row.get('preview_status')}` | `{row.get('candidate_symbol')}` | `{row.get('strategy')}` | "
            f"`{row.get('candidate_status')}` | {row.get('limit_price')} | {_fmt_money(row.get('total_limit_debit'))} | "
            f"{reasons.replace('|', '/')} |"
        )
    if not report.get("candidate_reviews"):
        lines.append("| - | - | - | - | - | - | No M247 candidate reviews. |")
    return "\n".join(lines) + "\n"


def write_paper_option_ticket_preview_json(path: Path, report: dict[str, Any]) -> Path:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return resolved


def write_paper_option_ticket_preview_markdown(path: Path, report: dict[str, Any]) -> Path:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(paper_option_ticket_preview_markdown(report), encoding="utf-8")
    return resolved
