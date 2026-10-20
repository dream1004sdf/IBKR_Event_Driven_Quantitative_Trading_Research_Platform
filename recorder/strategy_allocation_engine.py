from __future__ import annotations

import json
import math
import sqlite3
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from recorder.capital_micro_policy import (
    MICRO_CASH_POLICY_VERSION,
    build_micro_cash_policy,
    evaluate_micro_policy_for_candidate,
)
from recorder.gate_severity_policy import gate_policy_from_market_context


STRATEGY_ALLOCATION_ENGINE_VERSION = "m241-strategy-allocation-engine-v2-gate-severity-policy"


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _load_json(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        return {}
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _result(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(payload, dict):
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


def _latest_news(database_path: Path | None, *, symbols: set[str], limit: int = 12) -> list[dict[str, Any]]:
    if database_path is None:
        return []
    resolved = database_path.expanduser().resolve()
    if not resolved.exists():
        return []
    try:
        con = sqlite3.connect(resolved)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            """
            SELECT provider, headline, primary_symbol, published_at, provider_published_at, received_at, created_at
              FROM news_articles
             ORDER BY COALESCE(provider_published_at, published_at, received_at, created_at) DESC
             LIMIT ?
            """,
            (max(1, int(limit)) * 4,),
        ).fetchall()
        con.close()
    except sqlite3.Error:
        return []
    news: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        symbol = str(item.get("primary_symbol") or "").upper()
        if symbols and symbol and symbol not in symbols and len(news) >= limit:
            continue
        news.append(item)
        if len(news) >= limit:
            break
    return news


def _source_ticket(item: dict[str, Any]) -> dict[str, Any]:
    source = item.get("source_preview_item") if isinstance(item.get("source_preview_item"), dict) else {}
    ticket = source.get("ticket_draft") if isinstance(source.get("ticket_draft"), dict) else {}
    if ticket:
        return ticket
    return {}


def _candidate_action(item: dict[str, Any]) -> str:
    ticket = _source_ticket(item)
    order = item.get("order_intent", {}).get("order_preview") if isinstance(item.get("order_intent"), dict) else {}
    return str(
        item.get("action")
        or ticket.get("order_action")
        or (order or {}).get("source_order_action")
        or (order or {}).get("action")
        or ""
    ).upper()


def _candidate_symbol(item: dict[str, Any]) -> str:
    ticket = _source_ticket(item)
    source = item.get("source_preview_item") if isinstance(item.get("source_preview_item"), dict) else {}
    return str(item.get("symbol") or source.get("symbol") or ticket.get("symbol") or "").upper()


def _direction_from_action(action: str) -> str:
    if action in {"BUY", "BUY_TO_OPEN"}:
        return "bullish"
    if action in {"SELL_SHORT", "SELL", "SELL_TO_OPEN"}:
        return "bearish"
    if "COVER" in action:
        return "risk_reduction"
    return "neutral"


def _option_structure_by_symbol(options_market_structure: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(row.get("symbol") or "").upper(): row for row in options_market_structure.get("symbols") or [] if isinstance(row, dict)}


def _option_quote_context(options_market_structure: dict[str, Any], options_supervisor: dict[str, Any]) -> dict[str, Any]:
    summary = options_market_structure.get("summary") if isinstance(options_market_structure.get("summary"), dict) else {}
    return {
        "structure_status": options_market_structure.get("status"),
        "structure_generated_at": options_market_structure.get("generated_at"),
        "symbol_count": options_market_structure.get("symbol_count"),
        "quote_count": options_market_structure.get("quote_count"),
        "supervisor_status": options_supervisor.get("final_status"),
        "quote_allowed": options_supervisor.get("final_option_quote_allowed"),
        "quote_fresh": options_supervisor.get("final_quote_fresh"),
        "quote_usable": options_supervisor.get("final_quote_usable"),
        "quote_quality_status": options_supervisor.get("final_quote_quality_status"),
        "optional_index_quote_usable": options_supervisor.get("final_optional_index_quote_usable"),
        "gamma_regime_counts": summary.get("gamma_regime_counts") or {},
        "pin_risk_symbol_count": summary.get("pin_risk_symbol_count"),
        "confidence_counts": summary.get("confidence_counts") or {},
    }


def _capital_context(capital_state: dict[str, Any], capital_gate: dict[str, Any]) -> dict[str, Any]:
    capital_from_gate = capital_gate.get("capital_state") if isinstance(capital_gate.get("capital_state"), dict) else {}
    source = capital_state or capital_from_gate
    budgets = source.get("budgets") if isinstance(source.get("budgets"), dict) else {}
    withdrawal = source.get("withdrawal_guard") if isinstance(source.get("withdrawal_guard"), dict) else {}
    metrics = source.get("metrics") if isinstance(source.get("metrics"), dict) else {}
    return {
        "status": source.get("status"),
        "status_reasons": source.get("status_reasons") or [],
        "leverage_mode": source.get("leverage_mode"),
        "selected_account": source.get("selected_account"),
        "net_liquidation": metrics.get("net_liquidation"),
        "cash": metrics.get("total_cash_value"),
        "available_funds": metrics.get("available_funds"),
        "budgets": budgets,
        "max_safe_withdrawal": withdrawal.get("max_safe_withdrawal"),
        "capital_ready_count": capital_gate.get("capital_ready_count"),
        "capital_resized_count": capital_gate.get("capital_resized_count"),
        "capital_blocked_count": capital_gate.get("capital_blocked_count"),
        "block_reason_counts": capital_gate.get("capital_gate_block_reason_counts") or capital_gate.get("auto_gate_block_reason_counts") or {},
    }


def _market_context(market_data: dict[str, Any], market_data_risk_gate: dict[str, Any]) -> dict[str, Any]:
    summary = market_data.get("summary") if isinstance(market_data.get("summary"), dict) else {}
    temperature = market_data.get("temperature") if isinstance(market_data.get("temperature"), dict) else {}
    return {
        "market_data_status": market_data.get("status") or summary.get("status"),
        "temperature_label": temperature.get("label") or summary.get("temperature_label"),
        "temperature_score": temperature.get("score") if temperature.get("score") is not None else summary.get("temperature_score"),
        "risk_flags": temperature.get("risk_flags") or summary.get("risk_flags") or [],
        "delayed_count": summary.get("delayed_count"),
        "missing_price_count": summary.get("missing_price_count"),
        "alert_count": summary.get("alert_count"),
        "risk_gate_status": market_data_risk_gate.get("gate_status"),
        "risk_gate_action": market_data_risk_gate.get("action"),
        "new_entries_allowed": market_data_risk_gate.get("new_entries_allowed"),
        "alpha_entries_allowed": market_data_risk_gate.get("alpha_entries_allowed"),
        "hedge_entries_allowed": market_data_risk_gate.get("hedge_entries_allowed"),
        "option_strategy_bias": market_data_risk_gate.get("option_strategy_bias"),
        "entry_size_multiplier": market_data_risk_gate.get("entry_size_multiplier"),
    }


def _scorer_context(live_scorer_learning: dict[str, Any], news_learning: dict[str, Any]) -> dict[str, Any]:
    return {
        "live_scorer_status": live_scorer_learning.get("status") or live_scorer_learning.get("latest_status"),
        "shadow_only": live_scorer_learning.get("shadow_only"),
        "candidate_hit_rate": live_scorer_learning.get("candidate_hit_rate"),
        "candidate_avg_absolute_error": live_scorer_learning.get("candidate_avg_absolute_error"),
        "avg_packet_quality_score": live_scorer_learning.get("avg_packet_quality_score"),
        "packet_quality_counts": live_scorer_learning.get("packet_quality_counts") or {},
        "news_provider_weights": {
            row.get("provider"): row.get("weight")
            for row in news_learning.get("provider_rows") or news_learning.get("providers") or []
            if isinstance(row, dict) and row.get("provider")
        },
        "news_learning_status": news_learning.get("status") or news_learning.get("overall_status"),
    }


def _ticker_ranking_context(ticker_candidate_ranking: dict[str, Any]) -> dict[str, Any]:
    ranking = ticker_candidate_ranking.get("ranking") if isinstance(ticker_candidate_ranking.get("ranking"), list) else []
    summary = ticker_candidate_ranking.get("summary") if isinstance(ticker_candidate_ranking.get("summary"), dict) else {}
    return {
        "version": ticker_candidate_ranking.get("ticker_candidate_ranking_version"),
        "generated_at": ticker_candidate_ranking.get("generated_at"),
        "ranked_candidate_count": summary.get("ranked_candidate_count") if summary else len(ranking),
        "top_symbols": summary.get("top_symbols") or [row.get("symbol") for row in ranking[:10] if isinstance(row, dict)],
        "review_queue_count": summary.get("review_queue_count"),
        "policy": (ticker_candidate_ranking.get("policy") or {}).get("m241_consumption"),
    }


def _ranking_by_symbol(ticker_candidate_ranking: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = ticker_candidate_ranking.get("ranking") if isinstance(ticker_candidate_ranking.get("ranking"), list) else []
    return {str(row.get("symbol") or "").upper(): row for row in rows if isinstance(row, dict) and row.get("symbol")}


def _ticker_action_synthesis_context(ticker_action_candidate_synthesis: dict[str, Any]) -> dict[str, Any]:
    summary = ticker_action_candidate_synthesis.get("summary") if isinstance(ticker_action_candidate_synthesis.get("summary"), dict) else {}
    return {
        "version": ticker_action_candidate_synthesis.get("ticker_action_candidate_synthesis_version"),
        "generated_at": ticker_action_candidate_synthesis.get("generated_at"),
        "synthetic_candidate_count": summary.get("synthetic_candidate_count"),
        "injectable_candidate_count": summary.get("injectable_candidate_count"),
        "cross_asset_seed_count": summary.get("cross_asset_seed_count"),
        "cross_asset_injected_count": summary.get("cross_asset_injected_count"),
        "top_cross_asset_symbols": summary.get("top_cross_asset_symbols") or [],
        "top_cross_asset_expressions": summary.get("top_cross_asset_expressions") or [],
        "top_injected_symbols": summary.get("top_injected_symbols") or [],
        "action_counts": summary.get("action_counts") or {},
        "policy": (ticker_action_candidate_synthesis.get("policy") or {}).get("m241_consumption"),
    }


def _ticker_action_candidates(ticker_action_candidate_synthesis: dict[str, Any]) -> list[dict[str, Any]]:
    rows = ticker_action_candidate_synthesis.get("m241_injection_candidates")
    if not isinstance(rows, list):
        rows = ticker_action_candidate_synthesis.get("synthetic_candidates")
    return [
        row
        for row in (rows or [])
        if isinstance(row, dict) and row.get("inject_to_m241") and row.get("symbol")
    ]


def _source_item_ready_for_priority(item: dict[str, Any]) -> bool:
    status = str(item.get("capital_gate_status") or item.get("auto_execution_status") or "")
    return _is_ready_status(status)


def _is_ready_status(status: str | None) -> bool:
    return str(status or "") in {"capital_ready", "capital_resized_ready", "auto_execution_ready"}


def _score_option_usable(
    *,
    symbol: str,
    option_rows: dict[str, dict[str, Any]],
    option_context: dict[str, Any],
    options_budget: float,
    min_option_budget: float,
) -> tuple[bool, list[str], dict[str, Any]]:
    reasons: list[str] = []
    structure = option_rows.get(symbol) or {}
    confidence = str(structure.get("confidence") or "").lower()
    if options_budget < min_option_budget:
        reasons.append("options_budget_below_minimum")
    if option_context.get("quote_usable") is False:
        reasons.append("option_quote_context_not_usable")
    if option_context.get("quote_fresh") is False:
        reasons.append("option_quote_context_not_fresh")
    if confidence in {"", "low"}:
        reasons.append("option_structure_confidence_low_or_missing")
    warnings = set(structure.get("warnings") or [])
    if "some_contracts_missing_bid_ask" in warnings or "source_quote_snapshot_stale" in warnings:
        reasons.append("option_structure_quote_quality_warning")
    return not reasons, reasons, structure


def _ticket_type_for_expression(source_ticket_type: str, expression: str) -> str:
    normalized = str(expression or "").lower()
    if normalized in {"long_call", "long_put"}:
        return "option"
    if "spread" in normalized:
        return "option_combo_debit_spread"
    if normalized in {"stock_long", "fractional_stock_long_micro"}:
        return "stock"
    return source_ticket_type or "unknown"


def _discount_confidence(confidence: str, multiplier: float) -> str:
    normalized = str(confidence or "low").lower()
    if multiplier >= 0.95:
        return normalized
    if multiplier >= 0.75:
        return {"high": "medium", "medium": "medium", "low": "low"}.get(normalized, "low")
    return {"high": "medium", "medium": "low", "low": "low"}.get(normalized, "low")


def _select_expression(
    *,
    item: dict[str, Any],
    capital: dict[str, Any],
    market: dict[str, Any],
    option_rows: dict[str, dict[str, Any]],
    option_context: dict[str, Any],
    ticker_ranking_rows: dict[str, dict[str, Any]] | None,
    micro_policy: dict[str, Any],
    gate_policy: dict[str, Any],
    min_option_budget: float,
    min_spread_budget: float,
) -> dict[str, Any]:
    symbol = _candidate_symbol(item)
    action = _candidate_action(item)
    direction = _direction_from_action(action)
    ticket = _source_ticket(item)
    cross_asset_expression = item.get("m241_7_cross_asset_expression") if isinstance(item.get("m241_7_cross_asset_expression"), dict) else {}
    ticker_rank = (ticker_ranking_rows or {}).get(symbol) if symbol else {}
    source_ticket_type = str(item.get("ticket_type") or ticket.get("ticket_type") or "").lower()
    source_strategy = ticket.get("strategy") or cross_asset_expression.get("expression_hint")
    preferred_expression = str(cross_asset_expression.get("expression_hint") or "").lower()
    capital_status = item.get("capital_gate_status") or item.get("auto_execution_status")
    block_reasons = list(dict.fromkeys(item.get("capital_block_reasons") or item.get("block_reasons") or []))
    budgets = capital.get("budgets") or {}
    options_budget = _as_float(budgets.get("options_premium_budget")) or 0.0
    hedge_budget = _as_float(budgets.get("hedge_budget")) or 0.0
    risk_gate_action = str(market.get("risk_gate_action") or "")
    alpha_allowed = market.get("alpha_entries_allowed")
    hedge_allowed = market.get("hedge_entries_allowed")
    option_ok, option_reasons, structure = _score_option_usable(
        symbol=symbol,
        option_rows=option_rows,
        option_context=option_context,
        options_budget=options_budget,
        min_option_budget=min_option_budget,
    )
    gamma_regime = structure.get("gamma_regime")
    pin_risk = bool(structure.get("pin_risk"))
    gate_size_multiplier = float(gate_policy.get("size_multiplier") or 1.0)
    gate_confidence_multiplier = float(gate_policy.get("confidence_multiplier") or 1.0)
    gate_posture = str(gate_policy.get("recommended_execution_posture") or "normal")
    recommendation = "block"
    expression = "blocked"
    intent_role = "ordinary_alpha"
    confidence = "low"
    reasons: list[str] = []
    if capital.get("status") == "red":
        reasons.append("capital_state_red")
    if not _is_ready_status(str(capital_status)):
        reasons.append("capital_or_source_gate_not_ready")
        reasons.extend(block_reasons[:6])
    if risk_gate_action in {"block_new_entries", "defensive_or_hedge_only"} or alpha_allowed is False:
        reasons.append("market_gate_blocks_ordinary_alpha")
    if reasons:
        if hedge_allowed and hedge_budget > 0 and direction in {"bearish", "risk_reduction"}:
            recommendation = "hedge_only"
            expression = "put_spread_hedge_watch"
            intent_role = "hedge_or_risk_reduction"
            confidence = "medium" if option_ok else "low"
        else:
            recommendation = "block"
            expression = "blocked"
            confidence = "low"
    elif direction == "bullish":
        if preferred_expression in {"stock_long", "fractional_stock_long_micro"}:
            recommendation = "stock"
            expression = "stock_long"
            confidence = "medium"
            reasons.append("m241_7_prefers_stock_or_etf_proxy")
            if option_reasons:
                reasons.append("option_not_required_for_cross_asset_proxy")
        elif option_ok and options_budget >= min_spread_budget:
            recommendation = "defined_risk_spread"
            expression = "bull_call_debit_spread"
            confidence = "medium"
            if gamma_regime == "negative_gamma_trend_amplification":
                confidence = "high"
        elif option_ok:
            recommendation = "long_option"
            expression = "long_call"
            confidence = "medium"
        else:
            recommendation = "stock"
            expression = "stock_long"
            confidence = "medium"
            reasons.extend(option_reasons)
    elif direction == "bearish":
        if preferred_expression == "long_put" and option_ok and options_budget < min_spread_budget:
            recommendation = "long_option"
            expression = "long_put"
            confidence = "medium"
            reasons.append("m241_7_prefers_direct_put_expression")
        elif option_ok and options_budget >= min_spread_budget:
            recommendation = "defined_risk_spread"
            expression = "bear_put_debit_spread"
            confidence = "medium"
        elif option_ok:
            recommendation = "long_option"
            expression = "long_put"
            confidence = "medium"
        else:
            recommendation = "block"
            expression = "blocked"
            confidence = "low"
            reasons.extend(option_reasons)
            if str(capital.get("leverage_mode") or "") == "cash_only":
                reasons.append("cash_only_blocks_short_stock_without_put_expression")
    else:
        recommendation = "block"
        expression = "blocked"
        reasons.append("neutral_or_unknown_direction")

    if pin_risk and recommendation in {"long_option", "defined_risk_spread"}:
        reasons.append("pin_risk_near_wall_or_max_pain")
        if gamma_regime == "positive_gamma_mean_reversion_bias":
            confidence = "medium" if confidence == "high" else confidence

    if ticker_rank and recommendation != "block":
        rank_value = int(ticker_rank.get("rank") or 0)
        if rank_value and rank_value <= 10:
            if confidence == "medium":
                confidence = "high"
            reasons.append("m241_5_top_ranked_ticker")
        elif rank_value > 25 and confidence == "high":
            confidence = "medium"
            reasons.append("m241_5_lower_rank_confidence_haircut")
    elif symbol and recommendation != "block":
        reasons.append("m241_5_no_current_rank_context")

    if cross_asset_expression and recommendation != "block":
        reasons.append(
            "m241_7_cross_asset_expression:"
            f"{cross_asset_expression.get('scenario') or 'unknown'}:"
            f"rank_{cross_asset_expression.get('cross_asset_rank') or '-'}"
        )

    if recommendation != "block":
        if gate_size_multiplier < 1.0:
            reasons.append("gate_policy_size_haircut_active")
        if gate_confidence_multiplier < 1.0:
            discounted = _discount_confidence(confidence, gate_confidence_multiplier)
            if discounted != confidence:
                reasons.append("gate_policy_confidence_discount_applied")
            confidence = discounted

    if (
        recommendation == "stock"
        and expression == "stock_long"
        and direction == "bullish"
        and micro_policy.get("operating_mode") == "micro_cash_only"
    ):
        expression = "fractional_stock_long_micro"

    micro_policy_result = evaluate_micro_policy_for_candidate(
        micro_policy,
        action=action,
        ticket_type=_ticket_type_for_expression(source_ticket_type, expression),
        requirements=item.get("capital_requirements") or {},
        selected_expression=expression,
        intent_role=intent_role,
    )
    if not micro_policy_result.get("ready"):
        reasons.extend(str(reason) for reason in micro_policy_result.get("block_reasons") or [])
        if recommendation not in {"block"}:
            recommendation = "block"
            expression = "blocked"
            confidence = "low"

    return {
        "symbol": symbol,
        "candidate_source": item.get("candidate_origin") or item.get("source_milestone") or "m239_capital_aware_action_gate",
        "synthetic_candidate": bool(item.get("synthetic_candidate")),
        "source_action": action,
        "direction": direction,
        "source_strategy": source_strategy,
        "recommendation": recommendation,
        "selected_expression": expression,
        "intent_role": intent_role,
        "confidence": confidence,
        "capital_gate_status": capital_status,
        "capital_block_reasons": block_reasons,
        "ticker_candidate_rank": {
            "available": bool(ticker_rank),
            "rank": ticker_rank.get("rank") if ticker_rank else None,
            "total_score": ticker_rank.get("total_score") if ticker_rank else None,
            "confidence": ticker_rank.get("confidence") if ticker_rank else None,
            "top_reasons": (ticker_rank.get("ranking_reasons") or [])[:6] if ticker_rank else [],
            "cautions": (ticker_rank.get("cautions") or [])[:6] if ticker_rank else [],
        },
        "cross_asset_expression": cross_asset_expression,
        "option_structure": {
            "available": bool(structure),
            "confidence": structure.get("confidence"),
            "gamma_regime": gamma_regime,
            "pin_risk": pin_risk,
            "call_wall": structure.get("call_wall"),
            "put_wall": structure.get("put_wall"),
            "max_pain": structure.get("max_pain"),
            "warnings": structure.get("warnings") or [],
        },
        "market_gate": {
            "risk_gate_action": market.get("risk_gate_action"),
            "alpha_entries_allowed": market.get("alpha_entries_allowed"),
            "hedge_entries_allowed": market.get("hedge_entries_allowed"),
            "option_strategy_bias": market.get("option_strategy_bias"),
        },
        "micro_policy": micro_policy_result,
        "capital_requirements": item.get("capital_requirements") or {},
        "gate_policy": {
            "recommended_execution_posture": gate_posture,
            "size_multiplier": gate_size_multiplier,
            "confidence_multiplier": gate_confidence_multiplier,
            "category_counts": gate_policy.get("category_counts") or {},
        },
        "recommended_size_multiplier": gate_size_multiplier if recommendation != "block" else 0.0,
        "reasons": list(dict.fromkeys(reasons))[:12],
        "safety": {
            "paper_recommendation_only": True,
            "ibkr_order_submitted": False,
            "transmit_enabled": False,
        },
    }


def build_strategy_allocation_engine_report(
    *,
    capital_aware_gate_payload: dict[str, Any],
    capital_state_payload: dict[str, Any],
    market_data_payload: dict[str, Any],
    market_data_risk_gate_payload: dict[str, Any],
    options_market_structure_payload: dict[str, Any],
    options_market_structure_supervisor_payload: dict[str, Any],
    live_scorer_learning_payload: dict[str, Any],
    news_learning_payload: dict[str, Any],
    ticker_candidate_ranking_payload: dict[str, Any] | None = None,
    ticker_action_candidate_synthesis_payload: dict[str, Any] | None = None,
    previous_micro_policy_state_payload: dict[str, Any] | None = None,
    database_path: Path | None = None,
    min_option_budget: float = 100.0,
    min_spread_budget: float = 150.0,
    max_recommendations: int = 25,
    latest_news_limit: int = 12,
    now: str | None = None,
) -> dict[str, Any]:
    capital_gate = _result(capital_aware_gate_payload)
    capital_state = _result(capital_state_payload)
    market_data = _result(market_data_payload)
    market_data_risk_gate = _result(market_data_risk_gate_payload)
    options_market_structure = _result(options_market_structure_payload)
    options_supervisor = _result(options_market_structure_supervisor_payload)
    live_scorer_learning = _result(live_scorer_learning_payload)
    news_learning = _result(news_learning_payload)
    ticker_candidate_ranking = _result(ticker_candidate_ranking_payload)
    ticker_action_candidate_synthesis = _result(ticker_action_candidate_synthesis_payload)
    capital = _capital_context(capital_state, capital_gate)
    micro_policy_source = capital_state or (capital_gate.get("capital_state") if isinstance(capital_gate.get("capital_state"), dict) else {})
    micro_policy = build_micro_cash_policy(
        micro_policy_source,
        previous_policy_state=previous_micro_policy_state_payload,
        min_option_budget=float(min_option_budget),
        min_spread_budget=float(min_spread_budget),
    )
    market = _market_context(market_data, market_data_risk_gate)
    gate_policy = gate_policy_from_market_context(market)
    option_context = _option_quote_context(options_market_structure, options_supervisor)
    scorer = _scorer_context(live_scorer_learning, news_learning)
    ticker_ranking_context = _ticker_ranking_context(ticker_candidate_ranking)
    ticker_ranking_rows = _ranking_by_symbol(ticker_candidate_ranking)
    ticker_action_synthesis_context = _ticker_action_synthesis_context(ticker_action_candidate_synthesis)
    synthetic_source_items = _ticker_action_candidates(ticker_action_candidate_synthesis)
    option_rows = _option_structure_by_symbol(options_market_structure)
    source_items = capital_gate.get("auto_execution_items") or capital_gate.get("sample_auto_execution_items") or []
    existing_symbols = {_candidate_symbol(item) for item in source_items if isinstance(item, dict) and _candidate_symbol(item)}
    appended_synthetic_items = [
        item
        for item in synthetic_source_items
        if _candidate_symbol(item) and _candidate_symbol(item) not in existing_symbols
    ]
    ready_source_items = [item for item in source_items if isinstance(item, dict) and _source_item_ready_for_priority(item)]
    blocked_or_watch_source_items = [item for item in source_items if isinstance(item, dict) and not _source_item_ready_for_priority(item)]
    combined_source_items = ready_source_items + appended_synthetic_items + blocked_or_watch_source_items
    recommendations = []
    symbols = set()
    for item in combined_source_items[: max(0, int(max_recommendations))]:
        if not isinstance(item, dict):
            continue
        symbol = _candidate_symbol(item)
        if symbol:
            symbols.add(symbol)
        recommendations.append(
            _select_expression(
                item=item,
                capital=capital,
                market=market,
                option_rows=option_rows,
                option_context=option_context,
                ticker_ranking_rows=ticker_ranking_rows,
                micro_policy=micro_policy,
                gate_policy=gate_policy,
                min_option_budget=float(min_option_budget),
                min_spread_budget=float(min_spread_budget),
            )
        )
    latest_news = _latest_news(database_path, symbols=symbols, limit=latest_news_limit)
    recommendation_counts = Counter(row.get("recommendation") for row in recommendations)
    expression_counts = Counter(row.get("selected_expression") for row in recommendations)
    confidence_counts = Counter(row.get("confidence") for row in recommendations)
    micro_policy_status_counts = Counter((row.get("micro_policy") or {}).get("status") for row in recommendations)
    micro_policy_reason_counts = Counter()
    block_reasons = Counter()
    for row in recommendations:
        for reason in (row.get("micro_policy") or {}).get("block_reasons") or []:
            micro_policy_reason_counts[reason] += 1
        for reason in row.get("reasons") or []:
            block_reasons[reason] += 1
    ready_count = recommendation_counts.get("stock", 0) + recommendation_counts.get("long_option", 0) + recommendation_counts.get("defined_risk_spread", 0)
    result = {
        "strategy_allocation_engine_version": STRATEGY_ALLOCATION_ENGINE_VERSION,
        "generated_at": now or _iso_now(),
        "mode": "paper_read_only_strategy_selection",
        "summary": {
            "candidate_count": len(combined_source_items),
            "m239_source_candidate_count": len(source_items),
            "m239_ready_source_candidate_count": len(ready_source_items),
            "m241_6_synthetic_appended_count": len(appended_synthetic_items),
            "recommendation_count": len(recommendations),
            "paper_trade_ready_count": ready_count,
            "hedge_only_count": recommendation_counts.get("hedge_only", 0),
            "blocked_count": recommendation_counts.get("block", 0),
            "recommendation_counts": dict(recommendation_counts),
            "expression_counts": dict(expression_counts),
            "confidence_counts": dict(confidence_counts),
            "top_reason_counts": dict(block_reasons.most_common(12)),
            "micro_policy_operating_mode": micro_policy.get("operating_mode"),
            "micro_policy_ready_count": int(micro_policy_status_counts.get("allowed") or 0),
            "micro_policy_blocked_count": int(micro_policy_status_counts.get("blocked") or 0),
            "micro_policy_block_reason_counts": dict(micro_policy_reason_counts.most_common(12)),
            "gate_execution_posture": gate_policy.get("recommended_execution_posture"),
            "gate_size_multiplier": gate_policy.get("size_multiplier"),
            "gate_confidence_multiplier": gate_policy.get("confidence_multiplier"),
            "gate_category_counts": gate_policy.get("category_counts") or {},
        },
        "contexts": {
            "capital": capital,
            "micro_policy": micro_policy,
            "market": market,
            "gate_severity_policy": gate_policy,
            "options": option_context,
            "scorer": scorer,
            "ticker_candidate_ranking": ticker_ranking_context,
            "ticker_action_candidate_synthesis": ticker_action_synthesis_context,
            "news": {
                "latest_news_count": len(latest_news),
                "candidate_symbols": sorted(symbols),
            },
        },
        "recommendations": recommendations,
        "latest_news": latest_news,
        "policy": {
            "min_option_budget": float(min_option_budget),
            "min_spread_budget": float(min_spread_budget),
            "ordering": [
                "hard safety/source/capital gates",
                "market risk gate",
                "option quote and structure quality",
                "capital budget and leverage mode",
                "M241.7 cross-asset expression preference",
                "selected expression",
                "M246 micro cash-only policy",
                "M241.6 synthetic ticker candidates after M239 source candidates",
                "gate severity policy applies size haircuts and confidence discounts without turning every caution into a hard block",
            ],
        },
        "safety": {
            "paper_recommendation_only": True,
            "ibkr_order_submitted": False,
            "transmit_enabled": False,
            "live_routing_enabled": False,
            "live_thresholds_mutated": False,
        },
    }
    return result


def strategy_allocation_engine_markdown(result: dict[str, Any]) -> str:
    summary = result.get("summary") or {}
    contexts = result.get("contexts") or {}
    capital = contexts.get("capital") or {}
    micro_policy = contexts.get("micro_policy") or {}
    market = contexts.get("market") or {}
    options = contexts.get("options") or {}
    scorer = contexts.get("scorer") or {}
    gate_policy = contexts.get("gate_severity_policy") or {}
    ticker_ranking = contexts.get("ticker_candidate_ranking") or {}
    ticker_action_synthesis = contexts.get("ticker_action_candidate_synthesis") or {}
    lines = [
        "# M241 Strategy Allocation Engine",
        "",
        f"- Generated: `{result.get('generated_at')}`",
        f"- Mode: `{result.get('mode')}`",
        f"- Candidates / recommendations: `{summary.get('candidate_count')}` / `{summary.get('recommendation_count')}`",
        f"- Trade-ready / hedge-only / blocked: `{summary.get('paper_trade_ready_count')}` / `{summary.get('hedge_only_count')}` / `{summary.get('blocked_count')}`",
        f"- Recommendation counts: `{summary.get('recommendation_counts') or {}}`",
        f"- Expression counts: `{summary.get('expression_counts') or {}}`",
        f"- Capital: `{capital.get('status')}` leverage `{capital.get('leverage_mode')}` options budget `{_fmt_money((capital.get('budgets') or {}).get('options_premium_budget'))}` max withdrawal `{_fmt_money(capital.get('max_safe_withdrawal'))}`",
        f"- Micro policy: mode `{summary.get('micro_policy_operating_mode')}` ready `{summary.get('micro_policy_ready_count')}` blocked `{summary.get('micro_policy_blocked_count')}` reasons `{summary.get('micro_policy_block_reason_counts') or {}}`",
        f"- Market: `{market.get('market_data_status')}` temp `{market.get('temperature_label')}` risk gate `{market.get('risk_gate_action')}` alpha `{market.get('alpha_entries_allowed')}` hedge `{market.get('hedge_entries_allowed')}`",
        f"- Gate severity: posture `{summary.get('gate_execution_posture')}` size x`{summary.get('gate_size_multiplier')}` confidence x`{summary.get('gate_confidence_multiplier')}` counts `{summary.get('gate_category_counts') or {}}`",
        f"- Options: structure `{options.get('structure_status')}` quote usable `{options.get('quote_usable')}` quality `{options.get('quote_quality_status')}`",
        f"- Scorer: `{scorer.get('live_scorer_status')}` shadow `{scorer.get('shadow_only')}` hit `{scorer.get('candidate_hit_rate')}` packet quality `{scorer.get('avg_packet_quality_score')}`",
        f"- Ticker ranking: `{ticker_ranking.get('ranked_candidate_count')}` candidates top `{ticker_ranking.get('top_symbols') or []}`",
        f"- Cross-asset ranking: seeds `{ticker_action_synthesis.get('cross_asset_seed_count')}` injected `{ticker_action_synthesis.get('cross_asset_injected_count')}` top `{ticker_action_synthesis.get('top_cross_asset_expressions') or []}`",
        f"- M241.6 synthetic: `{summary.get('m241_6_synthetic_appended_count')}` appended / `{ticker_action_synthesis.get('injectable_candidate_count')}` injectable top `{ticker_action_synthesis.get('top_injected_symbols') or []}`",
        "- Safety: paper recommendation only; no broker order submission or transmit.",
        "",
        "## Recommendations",
        "",
        "| Symbol | Source | Direction | Recommendation | Expression | Confidence | Size x | Capital status | M241.5 rank | Reasons |",
        "| --- | --- | --- | --- | --- | --- | ---: | --- | ---: | --- |",
    ]
    for row in result.get("recommendations") or []:
        reasons = ", ".join(row.get("reasons") or []) or "-"
        ticker_rank = row.get("ticker_candidate_rank") or {}
        lines.append(
            f"| `{row.get('symbol') or '-'}` | `{row.get('candidate_source') or '-'}` | `{row.get('direction') or '-'}` | `{row.get('recommendation') or '-'}` | "
            f"`{row.get('selected_expression') or '-'}` | `{row.get('confidence') or '-'}` | `{row.get('recommended_size_multiplier')}` | `{row.get('capital_gate_status') or '-'}` | "
            f"{ticker_rank.get('rank') or '-'} | {reasons.replace('|', '/')} |"
        )
    if not result.get("recommendations"):
        lines.append("| - | - | - | - | - | - | - | - | - | No candidates available. |")
    lines.extend(
        [
            "",
            "## Latest news context",
            "",
            "| Provider | Symbol | Time | Headline |",
            "| --- | --- | --- | --- |",
        ]
    )
    for item in result.get("latest_news") or []:
        timestamp = item.get("provider_published_at") or item.get("published_at") or item.get("received_at") or item.get("created_at")
        headline = str(item.get("headline") or "-").replace("|", "/")
        lines.append(f"| `{item.get('provider') or '-'}` | `{item.get('primary_symbol') or '-'}` | `{timestamp or '-'}` | {headline} |")
    if not result.get("latest_news"):
        lines.append("| - | - | - | No latest news rows available. |")
    lines.extend(
        [
            "",
            "## Policy",
            "",
            f"- M246 policy: `{micro_policy.get('micro_cash_policy_version') or MICRO_CASH_POLICY_VERSION}` mode `{micro_policy.get('operating_mode')}`",
            f"- Gate severity policy: `{gate_policy.get('version')}` posture `{gate_policy.get('recommended_execution_posture')}`",
            f"- `{result.get('policy') or {}}`",
        ]
    )
    return "\n".join(lines) + "\n"


def write_strategy_allocation_engine_json(path: Path, result: dict[str, Any]) -> Path:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return resolved


def write_strategy_allocation_engine_markdown(path: Path, result: dict[str, Any]) -> Path:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(strategy_allocation_engine_markdown(result), encoding="utf-8")
    return resolved
