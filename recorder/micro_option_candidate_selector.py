from __future__ import annotations

import json
import math
from collections import Counter
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from recorder.capital_micro_policy import (
    MICRO_CASH_POLICY_VERSION,
    build_micro_cash_policy,
    evaluate_micro_policy_for_candidate,
)


MICRO_OPTION_CANDIDATE_SELECTOR_VERSION = "m247-micro-option-candidate-selector-v2-ticket-risk-cap"
DEFAULT_SYMBOLS = ("SPY", "QQQ", "IWM", "DIA", "NVDA", "TSLA", "AAPL")


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _parse_iso(value: Any) -> datetime | None:
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
    parsed = _parse_iso(value)
    if parsed is None:
        return None
    return max(0.0, (now - parsed).total_seconds())


def _parse_expiration(value: Any) -> date | None:
    text = str(value or "").strip()
    for fmt in ("%Y%m%d", "%Y%m"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


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


def _capital_source(capital_state_payload: dict[str, Any], strategy_allocation_payload: dict[str, Any]) -> dict[str, Any]:
    capital_state = _result(capital_state_payload)
    if capital_state:
        return capital_state
    strategy = _result(strategy_allocation_payload)
    contexts = strategy.get("contexts") if isinstance(strategy.get("contexts"), dict) else {}
    capital = contexts.get("capital") if isinstance(contexts.get("capital"), dict) else {}
    if not capital:
        return {}
    return {
        "status": capital.get("status"),
        "leverage_mode": capital.get("leverage_mode"),
        "selected_account": capital.get("selected_account"),
        "metrics": {
            "net_liquidation": capital.get("net_liquidation"),
            "total_cash_value": capital.get("cash"),
            "available_funds": capital.get("available_funds"),
        },
        "budgets": capital.get("budgets") or {},
        "withdrawal_guard": {"max_safe_withdrawal": capital.get("max_safe_withdrawal")},
    }


def _strategy_directions(strategy_allocation_payload: dict[str, Any]) -> dict[str, Counter[str]]:
    strategy = _result(strategy_allocation_payload)
    directions: dict[str, Counter[str]] = {}
    for row in strategy.get("recommendations") or []:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").upper()
        direction = str(row.get("direction") or "").lower()
        if not symbol or direction not in {"bullish", "bearish", "risk_reduction", "neutral"}:
            continue
        directions.setdefault(symbol, Counter())[direction] += 1
    return directions


def _strategy_recommendation_context(strategy_allocation_payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    strategy = _result(strategy_allocation_payload)
    rows: dict[str, dict[str, Any]] = {}
    for row in strategy.get("recommendations") or []:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").upper()
        if not symbol:
            continue
        gate_policy = row.get("gate_policy") if isinstance(row.get("gate_policy"), dict) else {}
        rows[symbol] = {
            "recommendation": row.get("recommendation"),
            "selected_expression": row.get("selected_expression"),
            "confidence": row.get("confidence"),
            "recommended_size_multiplier": row.get("recommended_size_multiplier"),
            "gate_policy": gate_policy,
            "gate_confidence_multiplier": gate_policy.get("confidence_multiplier"),
            "gate_posture": gate_policy.get("recommended_execution_posture"),
        }
    return rows


def _dominant_direction(symbol: str, directions: dict[str, Counter[str]]) -> str:
    counter = directions.get(symbol.upper()) or Counter()
    if not counter:
        return "two_sided_watch"
    direction, _count = counter.most_common(1)[0]
    return direction


def _structure_by_symbol(options_market_structure_payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    structure = _result(options_market_structure_payload)
    rows = structure.get("symbols") if isinstance(structure.get("symbols"), list) else []
    return {str(row.get("symbol") or "").upper(): row for row in rows if isinstance(row, dict) and row.get("symbol")}


def _quote_contract_key(row: dict[str, Any]) -> tuple[str, str, float, str]:
    return (
        str(row.get("symbol") or "").upper(),
        str(row.get("expiration") or ""),
        float(_as_float(row.get("strike")) or 0.0),
        str(row.get("right") or "").upper(),
    )


def _quote_sort_key(row: dict[str, Any]) -> tuple[float, float, float, int]:
    spread = _as_float(row.get("spread_pct"))
    ask = _as_float(row.get("ask"))
    volume = _as_float(row.get("volume")) or 0.0
    exchange_bonus = 0 if str(row.get("exchange") or "").upper() == "SMART" else 1
    return (
        spread if spread is not None else 999.0,
        ask if ask is not None else 999_999.0,
        -volume,
        exchange_bonus,
    )


def _best_quote_rows(option_quote_snapshot_payload: dict[str, Any], *, allowed_symbols: set[str]) -> list[dict[str, Any]]:
    snapshot = _result(option_quote_snapshot_payload)
    best: dict[tuple[str, str, float, str], dict[str, Any]] = {}
    for row in snapshot.get("quotes") or []:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").upper()
        if allowed_symbols and symbol not in allowed_symbols:
            continue
        key = _quote_contract_key(row)
        existing = best.get(key)
        if existing is None or _quote_sort_key(row) < _quote_sort_key(existing):
            best[key] = row
    return list(best.values())


def _contract_delta(row: dict[str, Any]) -> float | None:
    computations = row.get("option_computation") if isinstance(row.get("option_computation"), dict) else {}
    for key in ("MODEL_OPTION", "BID_OPTION_COMPUTATION", "ASK_OPTION_COMPUTATION", "LAST_OPTION_COMPUTATION"):
        block = computations.get(key) if isinstance(computations.get(key), dict) else {}
        delta = _as_float(block.get("delta"))
        if delta is not None:
            return delta
    return None


def _expression_for_right(right: str) -> str:
    return "long_call" if str(right).upper() == "C" else "long_put"


def _direction_match(direction: str, right: str) -> str:
    right = str(right).upper()
    if direction == "bullish" and right == "C":
        return "aligned"
    if direction == "bearish" and right == "P":
        return "aligned"
    if direction == "risk_reduction" and right == "P":
        return "hedge_aligned"
    if direction == "two_sided_watch":
        return "watch"
    return "contrarian_or_unmatched"


def _liquidity_score(row: dict[str, Any]) -> float:
    volume = _positive(row.get("volume"))
    oi = _positive(row.get("open_interest"))
    bid_size = _positive(row.get("bid_size"))
    ask_size = _positive(row.get("ask_size"))
    return min(volume, 10_000.0) / 100.0 + min(oi, 10_000.0) / 250.0 + min(bid_size + ask_size, 1_000.0) / 50.0


def _candidate_score(
    *,
    row: dict[str, Any],
    structure: dict[str, Any],
    direction: str,
    ask_debit: float,
    option_budget: float,
    quote_fresh: bool,
) -> float:
    spread_pct = _as_float(row.get("spread_pct")) or 1.0
    dte = int(_as_float(row.get("dte")) or 0)
    score = 100.0
    score -= min(60.0, spread_pct * 100.0)
    score += min(35.0, _liquidity_score(row))
    if 1 <= dte <= 14:
        score += 12.0
    elif dte == 0:
        score -= 18.0
    elif dte > 45:
        score -= 8.0
    if ask_debit <= option_budget * 0.50:
        score += 8.0
    elif ask_debit <= option_budget:
        score += 3.0
    elif option_budget > 0:
        score -= min(50.0, ((ask_debit / option_budget) - 1.0) * 30.0)
    if _direction_match(direction, str(row.get("right") or "")) in {"aligned", "hedge_aligned"}:
        score += 10.0
    confidence = str(structure.get("confidence") or "").lower()
    if confidence == "high":
        score += 8.0
    elif confidence == "medium":
        score += 4.0
    elif confidence == "low":
        score -= 6.0
    if structure.get("pin_risk"):
        score -= 8.0
    if not quote_fresh:
        score -= 25.0
    return round(min(100.0, max(0.0, score)), 3)


def _status_from_reasons(reasons: list[str]) -> str:
    if not reasons:
        return "ready"
    stale_only = set(reasons).issubset({"quote_snapshot_stale", "quote_snapshot_age_unknown"})
    if stale_only:
        return "stale_preview"
    if "quote_snapshot_stale" in reasons and not any(
        reason
        for reason in reasons
        if reason not in {"quote_snapshot_stale", "quote_snapshot_age_unknown", "zero_dte_risk"}
    ):
        return "stale_preview"
    return "blocked"


def _build_candidate(
    *,
    row: dict[str, Any],
    direction: str,
    structure: dict[str, Any],
    micro_policy: dict[str, Any],
    option_budget: float,
    quote_fresh: bool,
    quote_age_unknown: bool,
    max_spread_pct: float,
    min_volume: float,
    min_open_interest: float,
    min_dte: int,
    max_dte: int,
    max_premium_budget_fraction: float,
    strategy_context: dict[str, Any] | None,
    now_date: date,
) -> dict[str, Any]:
    right = str(row.get("right") or "").upper()
    multiplier = _as_float(row.get("multiplier")) or 100.0
    mid = _as_float(row.get("mid"))
    ask = _as_float(row.get("ask"))
    bid = _as_float(row.get("bid"))
    reference_price = _as_float(row.get("reference_price"))
    expiry_date = _parse_expiration(row.get("expiration"))
    snapshot_dte = int(_as_float(row.get("dte")) or 0)
    dte = (expiry_date - now_date).days if expiry_date is not None else snapshot_dte
    spread_pct = _as_float(row.get("spread_pct"))
    volume = _positive(row.get("volume"))
    oi = _positive(row.get("open_interest"))
    mid_debit = round((mid if mid is not None else ask or 0.0) * multiplier, 2)
    ask_debit = round((ask if ask is not None else mid or 0.0) * multiplier, 2)
    expression = _expression_for_right(right)
    strategy_context = strategy_context or {}
    gate_size_multiplier = _as_float(strategy_context.get("recommended_size_multiplier"))
    if gate_size_multiplier is None:
        gate_policy = strategy_context.get("gate_policy") if isinstance(strategy_context.get("gate_policy"), dict) else {}
        gate_size_multiplier = _as_float(gate_policy.get("size_multiplier"))
    gate_size_multiplier = max(0.0, min(1.0, gate_size_multiplier if gate_size_multiplier is not None else 1.0))
    effective_max_premium_budget_fraction = max(0.0, float(max_premium_budget_fraction) * gate_size_multiplier)
    requirements = {
        "cash_required": ask_debit,
        "initial_margin_required": ask_debit,
        "risk_required": ask_debit,
        "stock_notional_required": 0.0,
        "option_premium_required": ask_debit,
    }
    micro_result = evaluate_micro_policy_for_candidate(
        micro_policy,
        action="BUY_TO_OPEN",
        ticket_type="option",
        requirements=requirements,
        selected_expression=expression,
        intent_role="ordinary_alpha",
    )
    risk_caps = micro_result.get("risk_caps") if isinstance(micro_result.get("risk_caps"), dict) else {}
    reasons: list[str] = []
    if not quote_fresh:
        reasons.append("quote_snapshot_stale")
    if quote_age_unknown:
        reasons.append("quote_snapshot_age_unknown")
    if str(row.get("quote_status") or "") != "quoted" or not row.get("has_bid_ask"):
        reasons.append("quote_not_bid_ask_quoted")
    if ask_debit <= 0.0:
        reasons.append("missing_or_zero_ask_debit")
    if ask_debit > option_budget * effective_max_premium_budget_fraction + 1e-9:
        reasons.append("premium_exceeds_micro_option_budget")
    cautions: list[str] = []
    if gate_size_multiplier < 1.0:
        cautions.append("gate_severity_option_budget_haircut_active")
    if spread_pct is None or spread_pct > max_spread_pct:
        reasons.append("spread_pct_above_limit")
    if volume < min_volume and oi < min_open_interest:
        reasons.append("liquidity_below_minimum")
    if dte < min_dte:
        reasons.append("dte_below_minimum")
    if dte < 0:
        reasons.append("expiration_past")
    if dte > max_dte:
        reasons.append("dte_above_maximum")
    if dte == 0:
        reasons.append("zero_dte_risk")
    if bool(row.get("delayed")):
        reasons.append("delayed_option_quote")
    reasons.extend(str(reason) for reason in micro_result.get("block_reasons") or [])
    reasons = list(dict.fromkeys(reasons))
    status = _status_from_reasons(reasons)
    return {
        "symbol": str(row.get("symbol") or "").upper(),
        "direction_context": direction,
        "direction_match": _direction_match(direction, right),
        "strategy": expression,
        "right": right,
        "expiration": row.get("expiration"),
        "dte": dte,
        "strike": _as_float(row.get("strike")),
        "exchange": row.get("exchange"),
        "trading_class": row.get("trading_class"),
        "reference_price": reference_price,
        "moneyness": _as_float(row.get("moneyness")),
        "bid": bid,
        "ask": ask,
        "mid": mid,
        "total_mid_debit": mid_debit,
        "total_ask_debit": ask_debit,
        "max_loss": ask_debit,
        "spread": _as_float(row.get("spread")),
        "spread_pct": spread_pct,
        "volume": volume,
        "open_interest": oi,
        "bid_size": _as_float(row.get("bid_size")),
        "ask_size": _as_float(row.get("ask_size")),
        "delta": _contract_delta(row),
        "quote_status": row.get("quote_status"),
        "delayed": bool(row.get("delayed")),
        "candidate_status": status,
        "paper_candidate_ready": status == "ready",
        "requires_fresh_quote": "quote_snapshot_stale" in reasons or "quote_snapshot_age_unknown" in reasons,
        "score": _candidate_score(
            row=row,
            structure=structure,
            direction=direction,
            ask_debit=ask_debit,
            option_budget=option_budget,
            quote_fresh=quote_fresh,
        ),
        "structure_context": {
            "available": bool(structure),
            "confidence": structure.get("confidence"),
            "gamma_regime": structure.get("gamma_regime"),
            "pin_risk": structure.get("pin_risk"),
            "call_wall": structure.get("call_wall"),
            "put_wall": structure.get("put_wall"),
            "max_pain": structure.get("max_pain"),
            "proximity_alerts": structure.get("proximity_alerts") or [],
            "warnings": structure.get("warnings") or [],
        },
        "micro_policy": micro_result,
        "max_option_ticket_debit": risk_caps.get("max_option_ticket_debit"),
        "gate_severity": {
            "recommended_size_multiplier": gate_size_multiplier,
            "effective_max_premium_budget_fraction": effective_max_premium_budget_fraction,
            "confidence_multiplier": strategy_context.get("gate_confidence_multiplier"),
            "posture": strategy_context.get("gate_posture"),
        },
        "requirements": requirements,
        "candidate_contract": row.get("candidate_contract") or {},
        "cautions": cautions,
        "reasons": reasons,
        "safety": {
            "paper_read_only_candidate": True,
            "ibkr_order_submitted": False,
            "transmit_enabled": False,
            "live_routing_enabled": False,
        },
    }


def build_micro_option_candidate_selector_report(
    *,
    capital_state_payload: dict[str, Any],
    strategy_allocation_payload: dict[str, Any],
    option_quote_snapshot_payload: dict[str, Any],
    options_market_structure_payload: dict[str, Any] | None = None,
    options_market_structure_supervisor_payload: dict[str, Any] | None = None,
    previous_micro_policy_state_payload: dict[str, Any] | None = None,
    symbols: tuple[str, ...] = DEFAULT_SYMBOLS,
    min_option_budget: float = 100.0,
    min_spread_budget: float = 150.0,
    max_quote_age_minutes: float = 30.0,
    max_spread_pct: float = 0.30,
    min_volume: float = 1.0,
    min_open_interest: float = 0.0,
    min_dte: int = 0,
    max_dte: int = 21,
    max_premium_budget_fraction: float = 1.0,
    max_candidates: int = 25,
    now: datetime | None = None,
) -> dict[str, Any]:
    now_utc = (now or datetime.now(UTC)).astimezone(UTC)
    capital_source = _capital_source(capital_state_payload, strategy_allocation_payload)
    micro_policy = build_micro_cash_policy(
        capital_source,
        previous_policy_state=previous_micro_policy_state_payload,
        min_option_budget=float(min_option_budget),
        min_spread_budget=float(min_spread_budget),
    )
    option_budget = _positive((capital_source.get("budgets") or {}).get("options_premium_budget"))
    micro_thresholds = micro_policy.get("thresholds") if isinstance(micro_policy.get("thresholds"), dict) else {}
    max_option_ticket_debit = _positive(micro_thresholds.get("max_option_ticket_debit"))
    allowed_symbols = {str(symbol).upper().strip() for symbol in symbols if str(symbol).strip()}
    quote_snapshot = _result(option_quote_snapshot_payload)
    quote_generated_at = quote_snapshot.get("generated_at")
    quote_age = _age_seconds(quote_generated_at, now=now_utc)
    quote_age_unknown = quote_age is None
    quote_fresh = bool(quote_age is not None and quote_age <= float(max_quote_age_minutes) * 60.0)
    directions = _strategy_directions(strategy_allocation_payload)
    strategy_context_by_symbol = _strategy_recommendation_context(strategy_allocation_payload)
    structures = _structure_by_symbol(options_market_structure_payload or {})
    raw_rows = _best_quote_rows(quote_snapshot, allowed_symbols=allowed_symbols)
    candidates: list[dict[str, Any]] = []
    for row in raw_rows:
        symbol = str(row.get("symbol") or "").upper()
        direction = _dominant_direction(symbol, directions)
        structure = structures.get(symbol) or {}
        candidates.append(
            _build_candidate(
                row=row,
                direction=direction,
                structure=structure,
                micro_policy=micro_policy,
                option_budget=option_budget,
                quote_fresh=quote_fresh,
                quote_age_unknown=quote_age_unknown,
                max_spread_pct=float(max_spread_pct),
                min_volume=float(min_volume),
                min_open_interest=float(min_open_interest),
                min_dte=int(min_dte),
                max_dte=int(max_dte),
                max_premium_budget_fraction=float(max_premium_budget_fraction),
                strategy_context=strategy_context_by_symbol.get(symbol),
                now_date=now_utc.date(),
            )
        )
    candidates.sort(
        key=lambda row: (
            {"ready": 0, "stale_preview": 1, "blocked": 2}.get(str(row.get("candidate_status")), 9),
            -float(row.get("score") or 0.0),
            float(row.get("total_ask_debit") or 999_999.0),
            str(row.get("symbol") or ""),
        )
    )
    candidates = candidates[: max(0, int(max_candidates))]
    status_counts = Counter(row.get("candidate_status") for row in candidates)
    right_counts = Counter(row.get("right") for row in candidates)
    symbol_counts = Counter(row.get("symbol") for row in candidates)
    reason_counts = Counter()
    for row in candidates:
        for reason in row.get("reasons") or []:
            reason_counts[reason] += 1
    supervisor = _result(options_market_structure_supervisor_payload or {})
    option_quote_allowed = supervisor.get("final_option_quote_allowed")
    supervisor_session = str(supervisor.get("final_session") or "").lower()
    expected_deferred = bool(
        not quote_fresh
        and (
            option_quote_allowed is False
            or supervisor_session in {"closed", "pre_market", "post_market", "holiday", "weekend", "overnight"}
        )
    )
    if status_counts.get("ready", 0) > 0:
        status = "green"
    elif status_counts.get("stale_preview", 0) > 0:
        status = "yellow"
    elif candidates:
        status = "red"
    else:
        status = "red"
        reason_counts["no_option_candidates_from_snapshot"] += 1
    if expected_deferred and status != "green":
        status = "expected_deferred"
        reason_counts["option_market_closed_or_quotes_deferred"] += 1
    return {
        "micro_option_candidate_selector_version": MICRO_OPTION_CANDIDATE_SELECTOR_VERSION,
        "generated_at": now_utc.isoformat(),
        "status": status,
        "expected_deferred": expected_deferred,
        "mode": "paper_read_only_micro_option_candidate_selection",
        "summary": {
            "candidate_count": len(candidates),
            "ready_count": int(status_counts.get("ready", 0)),
            "stale_preview_count": int(status_counts.get("stale_preview", 0)),
            "blocked_count": int(status_counts.get("blocked", 0)),
            "status_counts": dict(status_counts),
            "right_counts": dict(right_counts),
            "symbol_counts": dict(symbol_counts),
            "top_reason_counts": dict(reason_counts.most_common(12)),
            "option_budget": option_budget,
            "max_option_ticket_debit": max_option_ticket_debit,
            "gate_severity_budget_haircut_count": sum(
                1
                for row in candidates
                if ((row.get("gate_severity") or {}).get("recommended_size_multiplier") or 1.0) < 1.0
            ),
            "micro_policy_operating_mode": micro_policy.get("operating_mode"),
            "micro_policy_profile": micro_policy.get("profile"),
            "quote_fresh": quote_fresh,
            "quote_age_seconds": round(quote_age, 3) if quote_age is not None else None,
            "quote_generated_at": quote_generated_at,
            "expected_deferred": expected_deferred,
            "deferred_reason": "option_market_closed_or_quotes_deferred" if expected_deferred else None,
            "allowed_symbols": sorted(allowed_symbols),
        },
        "contexts": {
            "capital": {
                "status": capital_source.get("status"),
                "leverage_mode": capital_source.get("leverage_mode"),
                "selected_account": capital_source.get("selected_account"),
                "budgets": capital_source.get("budgets") or {},
                "metrics": capital_source.get("metrics") or {},
            },
            "micro_policy": micro_policy,
            "strategy_directions": {symbol: dict(counter) for symbol, counter in directions.items()},
            "strategy_gate_context": strategy_context_by_symbol,
            "option_quote_snapshot": {
                "snapshot_version": quote_snapshot.get("snapshot_version"),
                "generated_at": quote_generated_at,
                "age_seconds": round(quote_age, 3) if quote_age is not None else None,
                "fresh": quote_fresh,
                "max_quote_age_minutes": float(max_quote_age_minutes),
                "requested_quotes": quote_snapshot.get("requested_quotes"),
                "quoted_contracts": quote_snapshot.get("quoted_contracts"),
                "quoted_rate": quote_snapshot.get("quoted_rate"),
                "avg_spread_pct": quote_snapshot.get("avg_spread_pct"),
                "status_counts": quote_snapshot.get("status_counts") or {},
            },
            "options_supervisor": {
                "final_status": supervisor.get("final_status"),
                "final_session": supervisor.get("final_session"),
                "final_option_quote_allowed": supervisor.get("final_option_quote_allowed"),
                "final_quote_fresh": supervisor.get("final_quote_fresh"),
                "final_quote_usable": supervisor.get("final_quote_usable"),
                "final_quote_quality_status": supervisor.get("final_quote_quality_status"),
            },
        },
        "candidates": candidates,
        "policy": {
            "symbols": sorted(allowed_symbols),
            "min_option_budget": float(min_option_budget),
            "min_spread_budget": float(min_spread_budget),
            "max_option_ticket_debit": max_option_ticket_debit,
            "max_quote_age_minutes": float(max_quote_age_minutes),
            "max_spread_pct": float(max_spread_pct),
            "min_volume": float(min_volume),
            "min_open_interest": float(min_open_interest),
            "min_dte": int(min_dte),
            "max_dte": int(max_dte),
            "max_premium_budget_fraction": float(max_premium_budget_fraction),
            "candidate_order": ["ready", "stale_preview", "blocked", "score desc", "ask debit asc"],
        },
        "safety": {
            "paper_read_only_candidate_selection": True,
            "ibkr_order_submitted": False,
            "transmit_enabled": False,
            "live_routing_enabled": False,
            "live_thresholds_mutated": False,
            "uses_stored_quote_snapshot_only": True,
        },
    }


def _fmt_money(value: Any) -> str:
    parsed = _as_float(value)
    if parsed is None:
        return "n/a"
    return f"${parsed:,.2f}"


def micro_option_candidate_selector_markdown(report: dict[str, Any]) -> str:
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    contexts = report.get("contexts") if isinstance(report.get("contexts"), dict) else {}
    quote = contexts.get("option_quote_snapshot") if isinstance(contexts.get("option_quote_snapshot"), dict) else {}
    capital = contexts.get("capital") if isinstance(contexts.get("capital"), dict) else {}
    micro_policy = contexts.get("micro_policy") if isinstance(contexts.get("micro_policy"), dict) else {}
    lines = [
        "# M247 Micro Cash Option Candidate Selector",
        "",
        f"- Generated: `{report.get('generated_at')}`",
        f"- Status: `{report.get('status')}`",
        f"- Mode: `{report.get('mode')}`",
        f"- Candidates ready/stale/blocked: `{summary.get('ready_count')}` / `{summary.get('stale_preview_count')}` / `{summary.get('blocked_count')}`",
        f"- Option budget / max ticket debit: `{_fmt_money(summary.get('option_budget'))}` / `{_fmt_money(summary.get('max_option_ticket_debit'))}`",
        f"- Capital: `{capital.get('status')}` leverage `{capital.get('leverage_mode')}`",
        f"- Micro policy: `{micro_policy.get('operating_mode')}` profile `{micro_policy.get('profile')}` version `{micro_policy.get('micro_cash_policy_version') or MICRO_CASH_POLICY_VERSION}`",
        f"- Quote fresh: `{quote.get('fresh')}` age `{quote.get('age_seconds')}` seconds from `{quote.get('generated_at')}`",
        f"- Top reasons: `{summary.get('top_reason_counts') or {}}`",
        "- Safety: read-only/paper candidate selection only; no broker order submission or transmit.",
        "",
        "## Candidate contracts",
        "",
        "| Status | Score | Symbol | Strategy | Exp/DTE | Strike | Bid/Ask | Debit | Spread | Vol/OI | Direction | Reasons |",
        "| --- | ---: | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    for row in report.get("candidates") or []:
        reasons = ", ".join(row.get("reasons") or []) or "-"
        bid_ask = f"{row.get('bid')}/{row.get('ask')}"
        expiry = f"{row.get('expiration')}/{row.get('dte')}"
        vol_oi = f"{row.get('volume')}/{row.get('open_interest')}"
        lines.append(
            f"| `{row.get('candidate_status')}` | {row.get('score')} | `{row.get('symbol')}` | `{row.get('strategy')}` | "
            f"`{expiry}` | {row.get('strike')} | {bid_ask} | {_fmt_money(row.get('total_ask_debit'))} | "
            f"{row.get('spread_pct')} | {vol_oi} | `{row.get('direction_context')}` | {reasons.replace('|', '/')} |"
        )
    if not report.get("candidates"):
        lines.append("| - | - | - | - | - | - | - | - | - | - | - | No candidates available. |")
    lines.extend(
        [
            "",
            "## Policy",
            "",
            f"- `{report.get('policy') or {}}`",
        ]
    )
    return "\n".join(lines) + "\n"


def write_micro_option_candidate_selector_json(path: Path, report: dict[str, Any]) -> Path:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return resolved


def write_micro_option_candidate_selector_markdown(path: Path, report: dict[str, Any]) -> Path:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(micro_option_candidate_selector_markdown(report), encoding="utf-8")
    return resolved
