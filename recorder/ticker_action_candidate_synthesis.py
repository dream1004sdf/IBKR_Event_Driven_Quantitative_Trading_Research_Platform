from __future__ import annotations

import json
import math
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


TICKER_ACTION_CANDIDATE_SYNTHESIS_VERSION = "m241_6-ticker-action-candidate-synthesis-v1"

_HEDGE_LONG_SYMBOLS = {"GLD", "TLT", "VIX", "VXX", "UUP"}
_BROAD_EQUITY_SYMBOLS = {"SPY", "QQQ", "IWM", "DIA", "SMH", "SOXX"}

_BULLISH_WORDS = {
    "beat",
    "beats",
    "raise",
    "raises",
    "raised",
    "raising",
    "upgrade",
    "upgraded",
    "surge",
    "surges",
    "jump",
    "jumps",
    "rally",
    "rallies",
    "record",
    "strong",
    "growth",
    "demand",
    "approval",
    "approved",
    "deal",
    "partnership",
    "optimism",
    "eases",
    "cooling",
    "cuts",
}

_BEARISH_WORDS = {
    "miss",
    "misses",
    "cut",
    "cuts",
    "downgrade",
    "downgraded",
    "probe",
    "investigation",
    "recall",
    "lawsuit",
    "warning",
    "weak",
    "plunge",
    "plunges",
    "drop",
    "drops",
    "falls",
    "war",
    "attack",
    "attacks",
    "missile",
    "sanction",
    "sanctions",
    "tariff",
    "tariffs",
    "inflation",
    "hot",
    "yields",
    "rates",
    "hawkish",
    "risk",
    "crisis",
}


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _result(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    result = payload.get("result")
    return result if isinstance(result, dict) else payload


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, dict):
        for key in ("numeric_value", "value", "amount", "score"):
            parsed = _as_float(value.get(key))
            if parsed is not None:
                return parsed
        return None
    try:
        parsed = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _capital_source(capital_state_payload: dict[str, Any], capital_aware_gate_payload: dict[str, Any]) -> dict[str, Any]:
    capital_state = _result(capital_state_payload)
    if capital_state:
        return capital_state
    gate = _result(capital_aware_gate_payload)
    capital = gate.get("capital_state") if isinstance(gate.get("capital_state"), dict) else {}
    return capital


def _budget(capital: dict[str, Any], key: str) -> float:
    budgets = capital.get("budgets") if isinstance(capital.get("budgets"), dict) else {}
    return max(0.0, _as_float(budgets.get(key)) or 0.0)


def _existing_symbols(capital_aware_gate_payload: dict[str, Any]) -> set[str]:
    gate = _result(capital_aware_gate_payload)
    symbols: set[str] = set()
    for row in gate.get("auto_execution_items") or gate.get("sample_auto_execution_items") or []:
        if not isinstance(row, dict):
            continue
        source = row.get("source_preview_item") if isinstance(row.get("source_preview_item"), dict) else {}
        ticket = source.get("ticket_draft") if isinstance(source.get("ticket_draft"), dict) else {}
        symbol = str(row.get("symbol") or source.get("symbol") or ticket.get("symbol") or "").upper().strip()
        if symbol:
            symbols.add(symbol)
    return symbols


def _words(text: str) -> set[str]:
    clean = "".join(ch.lower() if ch.isalnum() else " " for ch in text)
    return {part for part in clean.split() if part}


def _headline_direction_score(row: dict[str, Any]) -> tuple[float, float, list[str]]:
    bullish = 0.0
    bearish = 0.0
    reasons: list[str] = []
    themes = row.get("themes") if isinstance(row.get("themes"), dict) else {}
    for theme in themes:
        t = str(theme or "").lower()
        if any(key in t for key in ("ai", "semiconductor", "earnings", "growth")):
            bullish += 0.6
            reasons.append(f"theme_positive:{theme}")
        if any(key in t for key in ("geopolitical", "war", "tariff", "inflation", "rates")):
            bearish += 0.7
            reasons.append(f"theme_risk:{theme}")
    for event in row.get("recent_events") or []:
        if not isinstance(event, dict):
            continue
        text = " ".join(
            str(event.get(key) or "")
            for key in ("headline", "sentiment_label", "mapping_reason")
        )
        tokens = _words(text)
        pos_hits = tokens & _BULLISH_WORDS
        neg_hits = tokens & _BEARISH_WORDS
        if pos_hits:
            bullish += 1.0 + min(1.5, 0.25 * len(pos_hits))
            reasons.append("headline_positive:" + ",".join(sorted(pos_hits)[:4]))
        if neg_hits:
            bearish += 1.0 + min(1.5, 0.25 * len(neg_hits))
            reasons.append("headline_negative:" + ",".join(sorted(neg_hits)[:4]))
    return bullish, bearish, list(dict.fromkeys(reasons))[:8]


def _infer_action(row: dict[str, Any]) -> tuple[str, str, str, list[str]]:
    symbol = str(row.get("symbol") or "").upper()
    bullish, bearish, reasons = _headline_direction_score(row)
    direct_hits = int(row.get("direct_symbol_hit_count") or 0)
    confidence = str(row.get("confidence") or "low").lower()
    if direct_hits and confidence == "high" and bullish >= bearish:
        bullish += 0.6
        reasons.append("direct_high_confidence_symbol_bias")

    if symbol in _HEDGE_LONG_SYMBOLS and bearish >= bullish + 0.5:
        return "BUY", "bullish", "risk_hedge_long", list(dict.fromkeys(["risk_off_hedge_asset_buy_bias", *reasons]))[:10]
    if bearish >= bullish + 0.9:
        return "SELL_SHORT", "bearish", "bearish_put_or_spread_review", reasons[:10]
    if bullish >= bearish + 0.6:
        return "BUY", "bullish", "bullish_stock_or_call_review", reasons[:10]
    if symbol in _BROAD_EQUITY_SYMBOLS and bearish > 0 and bullish == 0:
        return "SELL_SHORT", "bearish", "broad_equity_risk_review", reasons[:10]
    return "WATCH", "neutral", "two_sided_watch", reasons[:10]


def _stock_notional(*, capital: dict[str, Any], default_notional: float, min_notional: float) -> float:
    cash_budget = _budget(capital, "deployable_cash_after_reserve")
    single_budget = _budget(capital, "max_single_position_notional")
    caps = [max(0.0, float(default_notional))]
    if cash_budget > 0:
        caps.append(cash_budget)
    if single_budget > 0:
        caps.append(single_budget)
    notional = min(caps) if caps else 0.0
    if notional < float(min_notional):
        return 0.0
    return round(notional, 2)


def _option_probe_premium(*, capital: dict[str, Any], default_premium: float) -> float:
    options_budget = _budget(capital, "options_premium_budget")
    if options_budget <= 0:
        return 0.0
    return round(max(0.0, min(float(default_premium), options_budget)), 2)


def _expression_requires_option(expression_hint: str | None) -> bool:
    text = str(expression_hint or "").lower()
    return any(token in text for token in ("put", "call", "spread", "option"))


def _cross_asset_candidate_seeds(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    data = _result(payload)
    seeds = data.get("m241_6_candidate_seeds")
    if not isinstance(seeds, list):
        return []
    return [row for row in seeds if isinstance(row, dict) and row.get("symbol")]


def _ranking_by_symbol(ranking_rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        str(row.get("symbol") or "").upper(): row
        for row in ranking_rows
        if isinstance(row, dict) and row.get("symbol")
    }


def _seed_to_ranking_like_row(seed: dict[str, Any], ranking_by_symbol: dict[str, dict[str, Any]]) -> dict[str, Any]:
    symbol = str(seed.get("symbol") or "").upper()
    base = dict(ranking_by_symbol.get(symbol) or {})
    base.update(
        {
            "symbol": symbol,
            "rank": seed.get("cross_asset_rank") or base.get("rank"),
            "total_score": seed.get("cross_asset_score") or base.get("total_score"),
            "confidence": base.get("confidence") or "medium",
            "ranking_reasons": list(dict.fromkeys([
                "m241_7_cross_asset_expression_seed",
                *[str(reason) for reason in seed.get("reasons") or []],
                *[str(reason) for reason in base.get("ranking_reasons") or []],
            ]))[:12],
            "cautions": list(dict.fromkeys([
                *[str(reason) for reason in seed.get("cautions") or []],
                *[str(reason) for reason in base.get("cautions") or []],
            ]))[:12],
            "event_count": base.get("event_count") or 0,
            "providers": base.get("providers") or {},
            "recent_events": base.get("recent_events") or [],
        }
    )
    return base


def _make_candidate(
    row: dict[str, Any],
    *,
    capital: dict[str, Any],
    action: str,
    direction: str,
    expression_hint: str,
    direction_reasons: list[str],
    default_stock_notional: float,
    min_fractional_stock_cash: float,
    default_option_probe_premium: float,
    allow_neutral_watch_injection: bool,
    cross_asset_expression: dict[str, Any] | None = None,
) -> dict[str, Any]:
    symbol = str(row.get("symbol") or "").upper()
    stock_notional = _stock_notional(
        capital=capital,
        default_notional=default_stock_notional,
        min_notional=min_fractional_stock_cash,
    )
    option_premium = _option_probe_premium(capital=capital, default_premium=default_option_probe_premium)
    status = str(capital.get("status") or "unknown")
    requires_option_expression = bool((cross_asset_expression or {}).get("requires_option")) or _expression_requires_option(expression_hint)
    block_reasons: list[str] = []
    if status == "red":
        block_reasons.append("capital_state_red_blocks_synthetic_candidates")
    if action == "WATCH" and not allow_neutral_watch_injection:
        block_reasons.append("neutral_watch_review_only")
    if action == "BUY" and requires_option_expression and option_premium <= 0:
        block_reasons.append("option_expression_requires_option_probe_budget")
    if action == "BUY" and not requires_option_expression and stock_notional <= 0:
        block_reasons.append("insufficient_stock_budget")
    if action == "SELL_SHORT" and option_premium <= 0:
        block_reasons.append("bearish_synthetic_requires_option_probe_budget")

    injectable = not block_reasons
    ticket_type = "option_review" if requires_option_expression or action == "SELL_SHORT" else ("stock" if action == "BUY" and stock_notional > 0 else "option_review")
    if ticket_type == "stock":
        quantity = round(stock_notional / 100.0, 6) if stock_notional > 0 else 0.0
    elif action in {"BUY", "SELL_SHORT"} and option_premium > 0:
        quantity = 1.0
    else:
        quantity = 0.0
    requirements = {
        "cash_required": stock_notional if action == "BUY" and ticket_type == "stock" else 0.0,
        "initial_margin_required": 0.0,
        "risk_required": round(max(stock_notional * 0.08, option_premium), 2),
        "stock_notional_required": stock_notional if ticket_type == "stock" else 0.0,
        "option_premium_required": option_premium if ticket_type == "option_review" else 0.0,
    }
    strategy = "synthetic_watch_review" if action == "WATCH" else expression_hint
    return {
        "symbol": symbol,
        "action": action,
        "direction_hint": direction,
        "ticket_type": ticket_type,
        "quantity": quantity,
        "candidate_origin": "m241_6_cross_asset_expression_seed" if cross_asset_expression else "m241_6_ticker_action_candidate_synthesis",
        "synthetic_candidate": True,
        "inject_to_m241": bool(injectable),
        "auto_execution_status": "auto_execution_ready" if injectable else "auto_execution_blocked",
        "capital_gate_status": "capital_ready" if injectable else "capital_blocked_source_gate",
        "capital_gate_status_display": "m241_6_synthetic_ready" if injectable else "m241_6_review_only",
        "capital_block_category": "ready" if injectable else "synthetic_review",
        "capital_capacity_ready": bool(injectable),
        "capital_action_gate_ready": bool(injectable),
        "capital_requirements": requirements,
        "capital_block_reasons": block_reasons,
        "block_reasons": block_reasons,
        "checks": [
            {
                "name": "m241_6_directional_candidate",
                "passed": action in {"BUY", "SELL_SHORT"} or allow_neutral_watch_injection,
                "value": action,
                "limit": "directional_or_allowed_watch",
                "severity": "hard",
            },
            {
                "name": "m241_6_no_live_order_route",
                "passed": True,
                "value": False,
                "limit": "no_broker_order_submission",
                "severity": "info",
            },
        ],
        "source_preview_item": {
            "symbol": symbol,
            "ticket_type": ticket_type,
            "synthetic_candidate": True,
            "ticket_draft": {
                "symbol": symbol,
                "strategy": strategy,
                "order_action": action,
                "quantity": quantity,
                "estimated_notional": stock_notional if ticket_type == "stock" else 0.0,
                "estimated_option_probe_premium": option_premium,
                "paper_review_only": True,
            },
        },
        "ticker_candidate_rank": {
            "rank": row.get("rank"),
            "total_score": row.get("total_score"),
            "confidence": row.get("confidence"),
            "event_count": row.get("event_count"),
            "providers": row.get("providers") or {},
            "ranking_reasons": (row.get("ranking_reasons") or [])[:8],
            "cautions": (row.get("cautions") or [])[:8],
        },
        "m241_6_direction": {
            "action": action,
            "direction": direction,
            "expression_hint": expression_hint,
            "reasons": direction_reasons,
        },
        "m241_7_cross_asset_expression": cross_asset_expression or {},
        "recent_events": (row.get("recent_events") or [])[:3],
        "safety": {
            "paper_recommendation_only": True,
            "strategy_review_only": True,
            "ibkr_order_submitted": False,
            "transmit": False,
            "live_routing_enabled": False,
        },
    }


def build_ticker_action_candidate_synthesis_report(
    *,
    ticker_candidate_ranking_payload: dict[str, Any],
    cross_asset_expression_ranking_payload: dict[str, Any] | None = None,
    capital_aware_gate_payload: dict[str, Any] | None = None,
    capital_state_payload: dict[str, Any] | None = None,
    max_rank: int = 20,
    max_synthetic_candidates: int = 12,
    min_total_score: float = 45.0,
    min_confidence: str = "medium",
    include_existing_symbols: bool = False,
    allow_neutral_watch_injection: bool = False,
    default_stock_notional: float = 25.0,
    min_fractional_stock_cash: float = 10.0,
    default_option_probe_premium: float = 25.0,
    now: str | None = None,
) -> dict[str, Any]:
    ranking_payload = _result(ticker_candidate_ranking_payload)
    capital_gate = _result(capital_aware_gate_payload)
    capital = _capital_source(capital_state_payload or {}, capital_gate)
    ranking_rows = ranking_payload.get("ranking") if isinstance(ranking_payload.get("ranking"), list) else []
    ranking_by_symbol = _ranking_by_symbol(ranking_rows)
    cross_asset_seeds = _cross_asset_candidate_seeds(cross_asset_expression_ranking_payload)
    existing = _existing_symbols(capital_gate)
    confidence_floor = {"low": 0, "medium": 1, "high": 2}.get(str(min_confidence or "medium").lower(), 1)
    confidence_rank = {"low": 0, "medium": 1, "high": 2}

    candidates: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    used_symbols: set[str] = set()
    cross_asset_injected_count = 0
    for seed in cross_asset_seeds:
        symbol = str(seed.get("symbol") or "").upper().strip()
        rank = int(_as_float(seed.get("cross_asset_rank")) or 0)
        score = _as_float(seed.get("cross_asset_score")) or 0.0
        action = str(seed.get("action") or "WATCH").upper()
        direction = str(seed.get("direction") or "neutral")
        expression_hint = str(seed.get("expression_hint") or "cross_asset_review")
        reasons: list[str] = []
        if not symbol:
            reasons.append("missing_symbol")
        if rank <= 0 or rank > int(max_rank):
            reasons.append("cross_asset_rank_outside_policy")
        if score < float(min_total_score):
            reasons.append("cross_asset_score_below_policy")
        if symbol in existing and not include_existing_symbols:
            reasons.append("already_present_in_m239_source_candidates")
        if symbol in used_symbols:
            reasons.append("duplicate_cross_asset_seed_symbol")
        if reasons:
            skipped.append({"symbol": symbol, "rank": rank, "score": score, "confidence": "cross_asset", "reasons": reasons})
            continue
        row = _seed_to_ranking_like_row(seed, ranking_by_symbol)
        candidate = _make_candidate(
            row,
            capital=capital,
            action=action,
            direction=direction,
            expression_hint=expression_hint,
            direction_reasons=list(dict.fromkeys(["m241_7_cross_asset_expression_seed", *[str(reason) for reason in seed.get("reasons") or []]]))[:12],
            default_stock_notional=default_stock_notional,
            min_fractional_stock_cash=min_fractional_stock_cash,
            default_option_probe_premium=default_option_probe_premium,
            allow_neutral_watch_injection=allow_neutral_watch_injection,
            cross_asset_expression=seed,
        )
        candidates.append(candidate)
        used_symbols.add(symbol)
        cross_asset_injected_count += 1
        if len(candidates) >= max(0, int(max_synthetic_candidates)):
            break

    for row in ranking_rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").upper().strip()
        rank = int(_as_float(row.get("rank")) or 0)
        score = _as_float(row.get("total_score")) or 0.0
        confidence = str(row.get("confidence") or "low").lower()
        reasons: list[str] = []
        if not symbol:
            reasons.append("missing_symbol")
        if rank <= 0 or rank > int(max_rank):
            reasons.append("rank_outside_policy")
        if score < float(min_total_score):
            reasons.append("score_below_policy")
        if confidence_rank.get(confidence, 0) < confidence_floor:
            reasons.append("confidence_below_policy")
        if symbol in existing and not include_existing_symbols:
            reasons.append("already_present_in_m239_source_candidates")
        if symbol in used_symbols:
            reasons.append("already_added_from_m241_7_cross_asset_seed")
        if reasons:
            skipped.append({"symbol": symbol, "rank": rank, "score": score, "confidence": confidence, "reasons": reasons})
            continue
        action, direction, expression_hint, direction_reasons = _infer_action(row)
        candidate = _make_candidate(
            row,
            capital=capital,
            action=action,
            direction=direction,
            expression_hint=expression_hint,
            direction_reasons=direction_reasons,
            default_stock_notional=default_stock_notional,
            min_fractional_stock_cash=min_fractional_stock_cash,
            default_option_probe_premium=default_option_probe_premium,
            allow_neutral_watch_injection=allow_neutral_watch_injection,
        )
        candidates.append(candidate)
        used_symbols.add(symbol)
        if len(candidates) >= max(0, int(max_synthetic_candidates)):
            break

    action_counts = Counter(row.get("action") for row in candidates)
    injection_count = sum(1 for row in candidates if row.get("inject_to_m241"))
    review_queue = [
        {
            "symbol": row.get("symbol"),
            "rank": (row.get("ticker_candidate_rank") or {}).get("rank"),
            "action": row.get("action"),
            "direction": row.get("direction_hint"),
            "inject_to_m241": row.get("inject_to_m241"),
            "reasons": row.get("block_reasons") or (row.get("m241_6_direction") or {}).get("reasons") or [],
        }
        for row in candidates
        if not row.get("inject_to_m241") or row.get("action") == "WATCH"
    ][:20]
    return {
        "ticker_action_candidate_synthesis_version": TICKER_ACTION_CANDIDATE_SYNTHESIS_VERSION,
        "milestone": "M241.6",
        "generated_at": now or _iso_now(),
        "summary": {
            "source_ranked_candidate_count": len(ranking_rows),
            "cross_asset_seed_count": len(cross_asset_seeds),
            "cross_asset_injected_count": cross_asset_injected_count,
            "used_cross_asset_expression_ranking": bool(cross_asset_seeds),
            "existing_m239_symbol_count": len(existing),
            "synthetic_candidate_count": len(candidates),
            "injectable_candidate_count": injection_count,
            "review_queue_count": len(review_queue),
            "action_counts": dict(action_counts),
            "top_injected_symbols": [row.get("symbol") for row in candidates if row.get("inject_to_m241")][:10],
            "top_cross_asset_symbols": [
                row.get("symbol")
                for row in candidates
                if row.get("m241_7_cross_asset_expression")
            ][:10],
            "top_cross_asset_expressions": [
                f"{row.get('symbol')}:{(row.get('m241_7_cross_asset_expression') or {}).get('expression_hint')}"
                for row in candidates
                if row.get("m241_7_cross_asset_expression")
            ][:10],
        },
        "parameters": {
            "max_rank": int(max_rank),
            "max_synthetic_candidates": int(max_synthetic_candidates),
            "min_total_score": float(min_total_score),
            "min_confidence": min_confidence,
            "include_existing_symbols": bool(include_existing_symbols),
            "allow_neutral_watch_injection": bool(allow_neutral_watch_injection),
            "default_stock_notional": float(default_stock_notional),
            "min_fractional_stock_cash": float(min_fractional_stock_cash),
            "default_option_probe_premium": float(default_option_probe_premium),
            "cross_asset_expression_ranking_enabled": bool(cross_asset_seeds),
        },
        "capital_context": {
            "status": capital.get("status"),
            "leverage_mode": capital.get("leverage_mode"),
            "budgets": capital.get("budgets") or {},
        },
        "synthetic_candidates": candidates,
        "m241_injection_candidates": [row for row in candidates if row.get("inject_to_m241")],
        "review_queue": review_queue,
        "skipped_candidates": skipped[:50],
        "policy": {
            "role": "convert M241.5 ranked tickers into M241 strategy-review candidates when M239 has no source candidate",
            "m241_7_consumption": "If M241.7 cross-asset expression seeds exist, they are injected before raw M241.5 ticker-direction fallback.",
            "does_not_submit_orders": True,
            "does_not_enable_transmit": True,
            "does_not_create_m242_order_intents": True,
            "m241_consumption": "M241 may append m241_injection_candidates after M239 candidates for paper/read-only strategy allocation only.",
        },
        "safety": {
            "read_only": True,
            "paper_recommendation_only": True,
            "ibkr_order_submitted": False,
            "transmit_enabled": False,
            "live_routing_enabled": False,
            "real_portfolio_mutated": False,
        },
    }


def ticker_action_candidate_synthesis_markdown(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    lines = [
        "# M241.6 Ticker Action Candidate Synthesis",
        "",
        f"- Version: `{report.get('ticker_action_candidate_synthesis_version')}`",
        f"- Generated: `{report.get('generated_at')}`",
        f"- Source ranked candidates: `{summary.get('source_ranked_candidate_count')}`",
        f"- Cross-asset seeds / injected: `{summary.get('cross_asset_seed_count')}` / `{summary.get('cross_asset_injected_count')}` top `{summary.get('top_cross_asset_expressions') or []}`",
        f"- Synthetic / injectable candidates: `{summary.get('synthetic_candidate_count')}` / `{summary.get('injectable_candidate_count')}`",
        f"- Top injected symbols: `{summary.get('top_injected_symbols') or []}`",
        "- Safety: strategy-review only; no broker order submission, no transmit, no live routing.",
        "",
        "| Symbol | Rank | Action | Direction | Inject | Score | Reasons | Blocks |",
        "| --- | ---: | --- | --- | --- | ---: | --- | --- |",
    ]
    for row in report.get("synthetic_candidates") or []:
        rank = (row.get("ticker_candidate_rank") or {}).get("rank")
        score = (row.get("ticker_candidate_rank") or {}).get("total_score")
        direction = row.get("m241_6_direction") or {}
        reasons = ", ".join(direction.get("reasons") or [])[:140].replace("|", "/")
        blocks = ", ".join(row.get("block_reasons") or [])[:120].replace("|", "/")
        lines.append(
            f"| `{row.get('symbol')}` | {rank} | `{row.get('action')}` | `{row.get('direction_hint')}` | "
            f"`{row.get('inject_to_m241')}` | {score} | {reasons or '-'} | {blocks or '-'} |"
        )
    if not report.get("synthetic_candidates"):
        lines.append("| - | - | - | - | - | - | No synthetic candidates. | - |")
    if report.get("review_queue"):
        lines.extend(["", "## Review queue", ""])
        for row in report.get("review_queue") or []:
            lines.append(f"- `{row.get('symbol')}` action `{row.get('action')}` inject `{row.get('inject_to_m241')}` reasons `{row.get('reasons')}`")
    return "\n".join(lines) + "\n"


def write_ticker_action_candidate_synthesis_json(path: Path, report: dict[str, Any]) -> Path:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return resolved


def write_ticker_action_candidate_synthesis_markdown(path: Path, report: dict[str, Any]) -> Path:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(ticker_action_candidate_synthesis_markdown(report), encoding="utf-8")
    return resolved
