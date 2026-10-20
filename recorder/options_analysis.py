from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from recorder.final_entry_size_resolver import resolve_final_entry_size


OPTIONS_ANALYSIS_VERSION = "paper-options-analysis-v1"
STRATEGY_ALLOCATION_VERSION = "paper-stock-option-strategy-allocation-v1"


def paper_cycle_watch_tick_markdown(
    result: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
    title: str = "Paper Cycle Watch Tick Report",
) -> str:
    filters = filters or {}
    summary = result.get("final_scheduler_summary") or {}
    checkpoint_summary = result.get("checkpoint_summary") or {}
    lines = [
        f"# {title}",
        "",
        f"- Generated at: `{result.get('generated_at')}`",
        f"- Watch run ID: `{result.get('watch_run_id')}`",
        f"- Action: `{result.get('action')}`",
        f"- Skipped reason: `{result.get('skipped_reason') or '-'}`",
        f"- Session gate: `{(result.get('session_gate') or {}).get('session') or '-'}` / allowed `{(result.get('session_gate') or {}).get('allowed')}`",
        f"- Evidence refresh watchdog: `{(result.get('evidence_refresh_watchdog') or {}).get('final_status') or '-'}` / freshness `{(result.get('evidence_refresh_watchdog') or {}).get('final_freshness_status') or '-'}`",
        f"- Mark refresh: `{(result.get('mark_refresh') or {}).get('status') or '-'}` / allowed `{(result.get('mark_refresh') or {}).get('allowed')}`",
        f"- Preflight gate: `{(result.get('preflight_gate') or {}).get('gate_status') or '-'}` / allowed `{(result.get('preflight_gate') or {}).get('allowed')}`",
        f"- Evidence preflight: `{(result.get('evidence_preflight_gate') or {}).get('gate_status') or '-'}` / action `{(result.get('evidence_preflight_gate') or {}).get('action') or '-'}`",
        f"- Market data risk gate: `{(result.get('market_data_risk_gate') or {}).get('gate_status') or '-'}` / action `{(result.get('market_data_risk_gate') or {}).get('action') or '-'}`",
        f"- Scheduler run ID: `{result.get('scheduler_run_id') or '-'}`",
        f"- Runs completed this invocation: `{result.get('runs_completed')}`",
        f"- Runs completed total: `{result.get('runs_completed_total')}`",
        f"- Next scheduler tick index: `{result.get('next_scheduler_tick_index')}`",
        f"- Total new paper fills: `{summary.get('total_new_paper_fills')}`",
        f"- Final cumulative paper fills: `{summary.get('final_cumulative_paper_fills')}`",
        f"- Operating final preflight applied/blocked cycles: `{summary.get('operating_final_preflight_applied_count')}` / `{summary.get('operating_final_preflight_blocked_count')}`",
        f"- Final risk state: `{summary.get('final_risk_state') or checkpoint_summary.get('final_risk_state')}`",
        f"- Final gate action: `{summary.get('final_gate_action')}`",
        "- Safety: paper watch tick only; no broker order API call or transmit.",
        "",
        "## Inputs",
        "",
    ]
    if filters:
        for key, value in filters.items():
            lines.append(f"- `{key}`: `{value if value is not None else '-'}`")
    else:
        lines.append("- none")
    snapshot = result.get("input_snapshot") or {}
    files = snapshot.get("files") or []
    lines.extend(
        [
            "",
            "## Watched files",
            "",
            "| Role | Path | Exists | Size | Modified UTC | Digest |",
            "| --- | --- | --- | ---: | --- | --- |",
        ]
    )
    for row in files:
        lines.append(
            f"| {row.get('role')} | `{row.get('path')}` | {row.get('exists')} | {row.get('size')} | `{row.get('modified_utc')}` | `{row.get('digest')}` |"
        )
    if not files:
        lines.append("| - | - | - | - | - | - |")
    mark_refresh = result.get("mark_refresh") or {}
    if mark_refresh:
        policy_eval = mark_refresh.get("policy_evaluation") or {}
        fallback_eval = policy_eval.get("fallback") or {}
        execution_action = mark_refresh.get("execution_quality_action") or {}
        lines.extend(
            [
                "",
                "## Mark refresh",
                "",
                f"- Version: `{mark_refresh.get('paper_mark_refresh_version') or '-'}`",
                f"- Mode: `{mark_refresh.get('mode') or '-'}`",
                f"- Status: `{mark_refresh.get('status') or '-'}`",
                f"- Allowed: `{mark_refresh.get('allowed')}`",
                f"- Reason: `{mark_refresh.get('reason') or '-'}`",
                f"- Symbols: `{','.join(mark_refresh.get('symbols') or []) or '-'}`",
                f"- Output JSON targets: `{','.join(mark_refresh.get('output_json_paths') or []) or '-'}`",
                f"- Policy version: `{policy_eval.get('policy_version') or '-'}`",
                f"- Policy block reasons: `{','.join(policy_eval.get('block_reasons') or []) or '-'}`",
                f"- Fallback applied symbols: `{','.join(fallback_eval.get('applied_symbols') or []) or '-'}`",
                f"- Execution quality action: `{execution_action.get('action') or '-'}` / applied `{execution_action.get('applied')}`",
                f"- Effective monitor size multiplier: `{execution_action.get('effective_monitor_size_multiplier') if execution_action else '-'}`",
            ]
        )
    preflight = result.get("preflight_gate") or {}
    if preflight:
        lines.extend(
            [
                "",
                "## Preflight gate",
                "",
                f"- Version: `{preflight.get('paper_data_quality_preflight_version') or '-'}`",
                f"- Status: `{preflight.get('gate_status') or '-'}`",
                f"- Allowed: `{preflight.get('allowed')}`",
                f"- Reason: `{preflight.get('reason') or '-'}`",
                f"- Missing marks: `{(preflight.get('summary') or {}).get('missing_mark_count')}`",
                f"- Delayed rows: `{(preflight.get('summary') or {}).get('delayed_count')}`",
                f"- Stale mark rows: `{(preflight.get('summary') or {}).get('stale_mark_count')}`",
            ]
        )
    evidence_watchdog = result.get("evidence_refresh_watchdog") or {}
    if evidence_watchdog:
        lines.extend(
            [
                "",
                "## Evidence refresh watchdog",
                "",
                f"- Version: `{evidence_watchdog.get('evidence_refresh_watchdog_version') or '-'}`",
                f"- Status: `{evidence_watchdog.get('final_status') or '-'}`",
                f"- Freshness: `{evidence_watchdog.get('final_freshness_status') or '-'}`",
                f"- Stale sources: `{','.join(evidence_watchdog.get('final_stale_source_keys') or []) or '-'}`",
                f"- Lagging sources: `{','.join(evidence_watchdog.get('final_lagging_source_keys') or []) or '-'}`",
                f"- Consecutive failures: `{evidence_watchdog.get('consecutive_failures')}`",
                f"- Next refresh eligible at: `{evidence_watchdog.get('next_refresh_eligible_at') or '-'}`",
            ]
        )
    evidence_preflight = result.get("evidence_preflight_gate") or {}
    if evidence_preflight:
        evidence = evidence_preflight.get("freshness") or {}
        lines.extend(
            [
                "",
                "## Evidence preflight gate",
                "",
                f"- Version: `{evidence_preflight.get('evidence_preflight_version') or '-'}`",
                f"- Status: `{evidence_preflight.get('gate_status') or '-'}`",
                f"- Allowed: `{evidence_preflight.get('allowed')}`",
                f"- Action: `{evidence_preflight.get('action') or '-'}`",
                f"- Entry size multiplier: `{evidence_preflight.get('entry_size_multiplier')}`",
                f"- Reason: `{evidence_preflight.get('reason') or '-'}`",
                f"- Lagging sources: `{','.join(evidence.get('lagging_source_keys') or []) or '-'}`",
                f"- Stale sources: `{','.join(evidence.get('stale_source_keys') or []) or '-'}`",
            ]
        )
    market_data_risk = result.get("market_data_risk_gate") or {}
    if market_data_risk:
        market = market_data_risk.get("market_data") or {}
        counts = market.get("counts") or {}
        lines.extend(
            [
                "",
                "## Market data risk gate",
                "",
                f"- Version: `{market_data_risk.get('market_data_risk_gate_version') or '-'}`",
                f"- Status: `{market_data_risk.get('gate_status') or '-'}`",
                f"- Allowed: `{market_data_risk.get('allowed')}`",
                f"- Action: `{market_data_risk.get('action') or '-'}`",
                f"- Entry size multiplier: `{market_data_risk.get('entry_size_multiplier')}`",
                f"- Reason: `{market_data_risk.get('reason') or '-'}`",
                f"- Market temperature: `{market.get('temperature_label') or '-'}` score `{market.get('temperature_score')}` status `{market.get('status') or '-'}`",
                f"- Delayed/missing/warming/errors/stale: `{counts.get('delayed_count')}` / `{counts.get('missing_price_count')}` / `{counts.get('warming_up_count')}` / `{counts.get('error_count')}` / `{counts.get('stale_count')}`",
            ]
        )
    lines.extend(
        [
            "",
            "## Interpretation note",
            "",
            "M131 is a file-backed, finite paper-only watch tick. It can skip unchanged inputs or run one M130 scheduler tick, then write a restart checkpoint atomically for the next invocation.",
        ]
    )
    return "\n".join(lines) + "\n"

def _normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def _black_scholes(
    *,
    spot: float,
    strike: float,
    dte: int,
    implied_vol: float,
    risk_free_rate: float,
    option_type: str,
) -> dict[str, float]:
    spot = max(0.0001, float(spot))
    strike = max(0.0001, float(strike))
    time_years = max(1.0 / 365.0, float(dte) / 365.0)
    implied_vol = max(0.0001, float(implied_vol))
    sqrt_t = math.sqrt(time_years)
    d1 = (math.log(spot / strike) + (float(risk_free_rate) + 0.5 * implied_vol * implied_vol) * time_years) / (
        implied_vol * sqrt_t
    )
    d2 = d1 - implied_vol * sqrt_t
    if option_type == "call":
        price = spot * _normal_cdf(d1) - strike * math.exp(-risk_free_rate * time_years) * _normal_cdf(d2)
        delta = _normal_cdf(d1)
        theta = (
            -spot * math.exp(-0.5 * d1 * d1) / math.sqrt(2.0 * math.pi) * implied_vol / (2.0 * sqrt_t)
            - risk_free_rate * strike * math.exp(-risk_free_rate * time_years) * _normal_cdf(d2)
        ) / 365.0
    else:
        price = strike * math.exp(-risk_free_rate * time_years) * _normal_cdf(-d2) - spot * _normal_cdf(-d1)
        delta = _normal_cdf(d1) - 1.0
        theta = (
            -spot * math.exp(-0.5 * d1 * d1) / math.sqrt(2.0 * math.pi) * implied_vol / (2.0 * sqrt_t)
            + risk_free_rate * strike * math.exp(-risk_free_rate * time_years) * _normal_cdf(-d2)
        ) / 365.0
    gamma = math.exp(-0.5 * d1 * d1) / math.sqrt(2.0 * math.pi) / (spot * implied_vol * sqrt_t)
    vega = spot * math.exp(-0.5 * d1 * d1) / math.sqrt(2.0 * math.pi) * sqrt_t / 100.0
    return {
        "price": max(0.0, price),
        "delta": delta,
        "gamma": gamma,
        "theta": theta,
        "vega": vega,
        "d1": d1,
        "d2": d2,
    }


def _intrinsic(spot: float, strike: float, option_type: str) -> float:
    if option_type == "call":
        return max(0.0, spot - strike)
    return max(0.0, strike - spot)


def _horizon_to_dte(horizon: str, *, min_dte: int) -> int:
    value = str(horizon or "").upper()
    if value.endswith("D"):
        try:
            return max(min_dte, int(value[:-1]) + 3)
        except ValueError:
            return min_dte
    if value.endswith("H") or value.endswith("M"):
        return min_dte
    return min_dte


def _trade_action_from_row(row: dict[str, Any]) -> str:
    try:
        raw = json.loads(str(row.get("raw_json") or "{}"))
    except json.JSONDecodeError:
        raw = {}
    debias = raw.get("debias") or {}
    return str(debias.get("trade_action") or debias.get("nuanced_action") or debias.get("method") or "")


def _is_tradeable_row(row: dict[str, Any]) -> bool:
    action = _trade_action_from_row(row)
    if action in {"observe_only", "second_pass_review", "reflex_observe_only", "neutral_candidate"}:
        return False
    return str(row.get("predicted_direction") or "").lower() in {"up", "down"}


def _fmt_pct(value: Any) -> str:
    if value is None:
        return "-"
    return f"{float(value) * 100:.2f}%"


def _fmt_signed_pct(value: Any) -> str:
    if value is None:
        return "-"
    return f"{float(value) * 100:+.2f}%"


def _fmt_money(value: Any) -> str:
    if value is None:
        return "-"
    return f"{float(value):+.2f}"


def _long_option_candidate(
    row: dict[str, Any],
    *,
    option_type: str,
    spot: float,
    dte: int,
    implied_vol: float,
    risk_free_rate: float,
    contract_multiplier: float,
    moneyness_offset_pct: float,
) -> dict[str, Any]:
    expected_return = float(row.get("expected_return") or 0.0)
    actual_return = row.get("actual_excess_return")
    target_spot = spot * (1.0 + expected_return)
    actual_spot = spot * (1.0 + float(actual_return)) if actual_return is not None else None
    strike = spot * (1.0 + moneyness_offset_pct) if option_type == "call" else spot * (1.0 - moneyness_offset_pct)
    model = _black_scholes(
        spot=spot,
        strike=strike,
        dte=dte,
        implied_vol=implied_vol,
        risk_free_rate=risk_free_rate,
        option_type=option_type,
    )
    premium = model["price"]
    target_value = _intrinsic(target_spot, strike, option_type)
    estimated_pnl = (target_value - premium) * contract_multiplier
    realized_pnl = None
    if actual_spot is not None:
        realized_pnl = (_intrinsic(actual_spot, strike, option_type) - premium) * contract_multiplier
    max_loss = premium * contract_multiplier
    break_even = strike + premium if option_type == "call" else strike - premium
    confidence = float(row.get("confidence") or 0.0)
    score = confidence + max(-0.5, min(0.5, estimated_pnl / max(max_loss, 1.0))) - min(0.25, premium / spot)
    return {
        "strategy": f"long_{option_type}",
        "option_structure": "single_long_option",
        "option_type": option_type,
        "dte": dte,
        "spot": spot,
        "long_strike": strike,
        "short_strike": None,
        "net_debit": premium,
        "max_loss": max_loss,
        "max_profit": None,
        "break_even": break_even,
        "delta": model["delta"],
        "gamma": model["gamma"],
        "theta": model["theta"],
        "vega": model["vega"],
        "estimated_pnl": estimated_pnl,
        "realized_pnl": realized_pnl,
        "estimated_return_on_risk": estimated_pnl / max(max_loss, 1.0),
        "realized_return_on_risk": realized_pnl / max(max_loss, 1.0) if realized_pnl is not None else None,
        "score": score,
        "risk_note": "debit_paid_is_max_loss",
    }


def _debit_spread_candidate(
    row: dict[str, Any],
    *,
    direction: str,
    spot: float,
    dte: int,
    implied_vol: float,
    risk_free_rate: float,
    contract_multiplier: float,
    spread_width_pct: float,
) -> dict[str, Any]:
    expected_return = float(row.get("expected_return") or 0.0)
    actual_return = row.get("actual_excess_return")
    target_spot = spot * (1.0 + expected_return)
    actual_spot = spot * (1.0 + float(actual_return)) if actual_return is not None else None
    width = max(0.01, spot * spread_width_pct)
    if direction == "up":
        option_type = "call"
        long_strike = spot
        short_strike = spot + width
    else:
        option_type = "put"
        long_strike = spot
        short_strike = spot - width
    long_model = _black_scholes(
        spot=spot,
        strike=long_strike,
        dte=dte,
        implied_vol=implied_vol,
        risk_free_rate=risk_free_rate,
        option_type=option_type,
    )
    short_model = _black_scholes(
        spot=spot,
        strike=short_strike,
        dte=dte,
        implied_vol=implied_vol,
        risk_free_rate=risk_free_rate,
        option_type=option_type,
    )
    net_debit = max(0.0, long_model["price"] - short_model["price"])
    target_value = _intrinsic(target_spot, long_strike, option_type) - _intrinsic(target_spot, short_strike, option_type)
    estimated_pnl = (target_value - net_debit) * contract_multiplier
    realized_pnl = None
    if actual_spot is not None:
        realized_value = _intrinsic(actual_spot, long_strike, option_type) - _intrinsic(actual_spot, short_strike, option_type)
        realized_pnl = (realized_value - net_debit) * contract_multiplier
    max_loss = net_debit * contract_multiplier
    max_profit = max(0.0, (abs(short_strike - long_strike) - net_debit) * contract_multiplier)
    break_even = long_strike + net_debit if direction == "up" else long_strike - net_debit
    confidence = float(row.get("confidence") or 0.0)
    score = confidence + max(-0.5, min(0.5, estimated_pnl / max(max_loss, 1.0))) + min(0.15, max_profit / max(max_loss, 1.0) / 20.0)
    return {
        "strategy": "bull_call_debit_spread" if direction == "up" else "bear_put_debit_spread",
        "option_structure": "defined_risk_debit_spread",
        "option_type": option_type,
        "dte": dte,
        "spot": spot,
        "long_strike": long_strike,
        "short_strike": short_strike,
        "net_debit": net_debit,
        "max_loss": max_loss,
        "max_profit": max_profit,
        "break_even": break_even,
        "delta": long_model["delta"] - short_model["delta"],
        "gamma": long_model["gamma"] - short_model["gamma"],
        "theta": long_model["theta"] - short_model["theta"],
        "vega": long_model["vega"] - short_model["vega"],
        "estimated_pnl": estimated_pnl,
        "realized_pnl": realized_pnl,
        "estimated_return_on_risk": estimated_pnl / max(max_loss, 1.0),
        "realized_return_on_risk": realized_pnl / max(max_loss, 1.0) if realized_pnl is not None else None,
        "score": score,
        "risk_note": "defined_risk_debit_spread",
    }


def analyze_replay_row_options(
    row: dict[str, Any],
    *,
    underlying_reference_price: float = 100.0,
    min_dte: int = 7,
    base_implied_vol: float = 0.55,
    risk_free_rate: float = 0.04,
    contract_multiplier: float = 100.0,
    moneyness_offset_pct: float = 0.0,
    spread_width_pct: float = 0.05,
    min_option_score: float = 0.35,
) -> dict[str, Any]:
    direction = str(row.get("predicted_direction") or "").lower()
    dte = _horizon_to_dte(str(row.get("horizon") or ""), min_dte=min_dte)
    candidates: list[dict[str, Any]] = []
    if _is_tradeable_row(row) and direction in {"up", "down"}:
        option_type = "call" if direction == "up" else "put"
        candidates.append(
            _long_option_candidate(
                row,
                option_type=option_type,
                spot=underlying_reference_price,
                dte=dte,
                implied_vol=base_implied_vol,
                risk_free_rate=risk_free_rate,
                contract_multiplier=contract_multiplier,
                moneyness_offset_pct=moneyness_offset_pct,
            )
        )
        candidates.append(
            _debit_spread_candidate(
                row,
                direction=direction,
                spot=underlying_reference_price,
                dte=dte,
                implied_vol=base_implied_vol,
                risk_free_rate=risk_free_rate,
                contract_multiplier=contract_multiplier,
                spread_width_pct=spread_width_pct,
            )
        )
    candidates.sort(
        key=lambda item: (
            float(item.get("score") or 0.0),
            float(item.get("estimated_return_on_risk") or 0.0),
            -float(item.get("max_loss") or 0.0),
        ),
        reverse=True,
    )
    selected = candidates[0] if candidates and float(candidates[0].get("score") or 0.0) >= float(min_option_score) else None
    return {
        "analysis_version": OPTIONS_ANALYSIS_VERSION,
        "score_id": row.get("id"),
        "outcome_id": row.get("outcome_id"),
        "symbol": row.get("symbol"),
        "horizon": row.get("horizon"),
        "replay_timestamp": row.get("replay_timestamp"),
        "predicted_direction": row.get("predicted_direction"),
        "expected_return": row.get("expected_return"),
        "confidence": row.get("confidence"),
        "actual_excess_return": row.get("actual_excess_return"),
        "direction_hit": row.get("direction_hit"),
        "trade_action": _trade_action_from_row(row),
        "option_candidate_status": "candidate" if selected else "blocked",
        "block_reason": None if selected else "no_tradeable_option_candidate",
        "selected_strategy": selected,
        "strategy_candidates": candidates,
        "safety": {
            "paper_only": True,
            "uses_live_option_chain": False,
            "ibkr_order_submitted": False,
            "account_or_portfolio_checked": False,
            "notes": "Theoretical option strategy analysis only; no live chain, quote, spread, liquidity, assignment, margin, or execution check.",
        },
    }


def _group_analyses(analyses: list[dict[str, Any]], key_fn: Any, *, min_rows: int) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in analyses:
        groups.setdefault(str(key_fn(item) or "unknown"), []).append(item)
    rows: list[dict[str, Any]] = []
    for bucket, members in groups.items():
        selected = [item for item in members if item.get("selected_strategy")]
        realized = [
            float((item.get("selected_strategy") or {}).get("realized_pnl"))
            for item in selected
            if (item.get("selected_strategy") or {}).get("realized_pnl") is not None
        ]
        if len(members) < min_rows:
            continue
        rows.append(
            {
                "bucket": bucket,
                "rows": len(members),
                "option_candidates": len(selected),
                "candidate_rate": len(selected) / len(members) if members else None,
                "positive_realized": sum(1 for value in realized if value > 0.0),
                "evaluated": len(realized),
                "positive_realized_rate": sum(1 for value in realized if value > 0.0) / len(realized) if realized else None,
                "total_realized_pnl": sum(realized),
                "avg_realized_pnl": sum(realized) / len(realized) if realized else None,
                "avg_max_loss": (
                    sum(float((item.get("selected_strategy") or {}).get("max_loss") or 0.0) for item in selected) / len(selected)
                    if selected
                    else None
                ),
            }
        )
    rows.sort(
        key=lambda item: (
            -float(item.get("total_realized_pnl") or 0.0),
            -int(item.get("option_candidates") or 0),
            item["bucket"],
        )
    )
    return rows


def options_analysis_summary(
    rows: list[dict[str, Any]],
    *,
    underlying_reference_price: float = 100.0,
    min_dte: int = 7,
    base_implied_vol: float = 0.55,
    risk_free_rate: float = 0.04,
    contract_multiplier: float = 100.0,
    moneyness_offset_pct: float = 0.0,
    spread_width_pct: float = 0.05,
    min_option_score: float = 0.35,
    min_group_rows: int = 1,
    sample_limit: int = 20,
) -> dict[str, Any]:
    analyses = [
        analyze_replay_row_options(
            row,
            underlying_reference_price=underlying_reference_price,
            min_dte=min_dte,
            base_implied_vol=base_implied_vol,
            risk_free_rate=risk_free_rate,
            contract_multiplier=contract_multiplier,
            moneyness_offset_pct=moneyness_offset_pct,
            spread_width_pct=spread_width_pct,
            min_option_score=min_option_score,
        )
        for row in rows
    ]
    selected = [item for item in analyses if item.get("selected_strategy")]
    realized = [
        float((item.get("selected_strategy") or {}).get("realized_pnl"))
        for item in selected
        if (item.get("selected_strategy") or {}).get("realized_pnl") is not None
    ]
    strategy_counts = Counter(str((item.get("selected_strategy") or {}).get("strategy") or "blocked") for item in analyses)
    return {
        "rows": len(rows),
        "option_candidates": len(selected),
        "candidate_rate": len(selected) / len(rows) if rows else None,
        "evaluated_candidates": len(realized),
        "positive_realized": sum(1 for value in realized if value > 0.0),
        "positive_realized_rate": sum(1 for value in realized if value > 0.0) / len(realized) if realized else None,
        "total_realized_pnl": sum(realized),
        "avg_realized_pnl": sum(realized) / len(realized) if realized else None,
        "strategy_counts": dict(strategy_counts),
        "parameters": {
            "underlying_reference_price": float(underlying_reference_price),
            "min_dte": int(min_dte),
            "base_implied_vol": float(base_implied_vol),
            "risk_free_rate": float(risk_free_rate),
            "contract_multiplier": float(contract_multiplier),
            "moneyness_offset_pct": float(moneyness_offset_pct),
            "spread_width_pct": float(spread_width_pct),
            "min_option_score": float(min_option_score),
        },
        "by_strategy": _group_analyses(
            analyses,
            lambda item: (item.get("selected_strategy") or {}).get("strategy") or "blocked",
            min_rows=min_group_rows,
        ),
        "by_symbol": _group_analyses(analyses, lambda item: item.get("symbol"), min_rows=min_group_rows),
        "by_direction": _group_analyses(analyses, lambda item: item.get("predicted_direction"), min_rows=min_group_rows),
        "sample_analyses": analyses[: max(0, int(sample_limit))],
        "analyses": analyses,
        "safety": {
            "mode": "read_only_theoretical_options_analysis",
            "policy": "No live option chain, quote, spread, liquidity, assignment, margin, IBKR order, or account check.",
        },
    }


def _append_group_table(lines: list[str], title: str, rows: list[dict[str, Any]]) -> None:
    lines.extend(
        [
            "",
            f"## {title}",
            "",
            "| Bucket | Rows | Candidates | Candidate rate | Positive/eval | Positive rate | Total realized PnL | Avg realized PnL | Avg max loss |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    if not rows:
        lines.append("| - | 0 | 0 | - | 0/0 | - | - | - | - |")
    for row in rows:
        lines.append(
            f"| {row['bucket']} | {row['rows']} | {row['option_candidates']} "
            f"| {_fmt_pct(row.get('candidate_rate'))} "
            f"| {row['positive_realized']}/{row['evaluated']} "
            f"| {_fmt_pct(row.get('positive_realized_rate'))} "
            f"| {_fmt_money(row.get('total_realized_pnl'))} "
            f"| {_fmt_money(row.get('avg_realized_pnl'))} "
            f"| {_fmt_money(row.get('avg_max_loss'))} |"
        )


def options_analysis_markdown(
    summary: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
    title: str = "Replay Score Options Analysis Report",
) -> str:
    filters = filters or {}
    lines = [
        f"# {title}",
        "",
        f"- Generated at: `{datetime.now(UTC).isoformat()}`",
        f"- Rows: `{summary.get('rows')}`",
        f"- Option candidates: `{summary.get('option_candidates')}`",
        f"- Candidate rate: `{_fmt_pct(summary.get('candidate_rate'))}`",
        f"- Positive realized option PnL: `{summary.get('positive_realized')}/{summary.get('evaluated_candidates')} = {_fmt_pct(summary.get('positive_realized_rate'))}`",
        f"- Total realized option PnL: `{_fmt_money(summary.get('total_realized_pnl'))}`",
        f"- Avg realized option PnL: `{_fmt_money(summary.get('avg_realized_pnl'))}`",
        "- Safety: theoretical paper options analysis only; no live chain, quote, spread, liquidity, assignment, margin, IBKR order, or account check.",
        "",
        "## Filters",
        "",
    ]
    if filters:
        for key, value in filters.items():
            lines.append(f"- `{key}`: `{value if value is not None else '-'}`")
    else:
        lines.append("- none")
    params = summary.get("parameters") or {}
    lines.extend(
        [
            "",
            "## Model parameters",
            "",
            f"- `underlying_reference_price`: `{params.get('underlying_reference_price')}`",
            f"- `min_dte`: `{params.get('min_dte')}`",
            f"- `base_implied_vol`: `{params.get('base_implied_vol')}`",
            f"- `risk_free_rate`: `{params.get('risk_free_rate')}`",
            f"- `contract_multiplier`: `{params.get('contract_multiplier')}`",
            f"- `moneyness_offset_pct`: `{params.get('moneyness_offset_pct')}`",
            f"- `spread_width_pct`: `{params.get('spread_width_pct')}`",
            f"- `min_option_score`: `{params.get('min_option_score')}`",
            "",
            "## Strategy counts",
            "",
        ]
    )
    for key, value in (summary.get("strategy_counts") or {}).items():
        lines.append(f"- `{key}`: `{value}`")
    _append_group_table(lines, "By selected strategy", summary.get("by_strategy") or [])
    _append_group_table(lines, "By symbol", summary.get("by_symbol") or [])
    _append_group_table(lines, "By predicted direction", summary.get("by_direction") or [])
    lines.extend(
        [
            "",
            "## Sample option analyses",
            "",
            "| Symbol | Horizon | Direction | Strategy | Score | DTE | Long strike | Short strike | Net debit | Max loss | Realized PnL |",
            "| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for item in summary.get("sample_analyses") or []:
        selected = item.get("selected_strategy") or {}
        lines.append(
            f"| {item.get('symbol')} | {item.get('horizon')} | {item.get('predicted_direction')} "
            f"| {selected.get('strategy') or 'blocked'} "
            f"| {float(selected.get('score') or 0.0):.3f} "
            f"| {selected.get('dte') or '-'} "
            f"| {float(selected.get('long_strike') or 0.0):.2f} "
            f"| {float(selected.get('short_strike') or 0.0):.2f} "
            f"| {float(selected.get('net_debit') or 0.0):.2f} "
            f"| {float(selected.get('max_loss') or 0.0):.2f} "
            f"| {_fmt_money(selected.get('realized_pnl'))} |"
        )
    lines.extend(
        [
            "",
            "## Interpretation note",
            "",
            "This is a bridge layer for adding options to the trading stack. It uses theoretical Black-Scholes assumptions and replay returns to compare defined-risk option structures. It is not an option-chain selector yet and should not be treated as execution-ready.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_options_analysis_markdown(
    output_path: Path,
    summary: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(options_analysis_markdown(summary, filters=filters), encoding="utf-8")
    return output_path


def _signed_expected_return(row: dict[str, Any]) -> float:
    direction = str(row.get("predicted_direction") or "").lower()
    expected_return = float(row.get("expected_return") or 0.0)
    if direction == "up":
        return expected_return
    if direction == "down":
        return -expected_return
    return 0.0


def _signed_actual_return(row: dict[str, Any]) -> float | None:
    direction = str(row.get("predicted_direction") or "").lower()
    actual = row.get("actual_excess_return")
    if actual is None:
        return None
    actual_value = float(actual)
    if direction == "up":
        return actual_value
    if direction == "down":
        return -actual_value
    return None


def _stock_expression_candidate(
    row: dict[str, Any],
    *,
    underlying_reference_price: float,
    stock_quantity: float,
    stock_stop_loss_pct: float,
    min_confidence: float,
    min_stock_expected_return: float,
) -> dict[str, Any] | None:
    if not _is_tradeable_row(row):
        return None
    direction = str(row.get("predicted_direction") or "").lower()
    if direction not in {"up", "down"}:
        return None
    confidence = float(row.get("confidence") or 0.0)
    signed_expected = _signed_expected_return(row)
    if confidence < float(min_confidence) or signed_expected < float(min_stock_expected_return):
        return None
    notional = max(0.0, float(underlying_reference_price) * float(stock_quantity))
    max_loss = max(1.0, notional * max(0.0001, float(stock_stop_loss_pct)))
    expected_pnl = signed_expected * notional
    actual_return = _signed_actual_return(row)
    realized_pnl = actual_return * notional if actual_return is not None else None
    expected_return_on_risk = expected_pnl / max_loss
    score = confidence + max(-0.50, min(0.50, expected_return_on_risk / 2.0))
    return {
        "instrument": "stock",
        "strategy": "stock_long" if direction == "up" else "stock_short",
        "action": "BUY_STOCK" if direction == "up" else "SELL_OR_SHORT_STOCK",
        "quantity": float(stock_quantity),
        "notional": notional,
        "max_loss": max_loss,
        "stop_loss_pct": float(stock_stop_loss_pct),
        "expected_pnl": expected_pnl,
        "realized_pnl": realized_pnl,
        "estimated_return_on_risk": expected_return_on_risk,
        "realized_return_on_risk": realized_pnl / max_loss if realized_pnl is not None else None,
        "score": score,
        "risk_note": "stock_expression_with_stop_loss_proxy",
    }


def _option_expression_candidate(
    option_analysis: dict[str, Any],
    *,
    min_option_return_on_risk: float,
) -> dict[str, Any] | None:
    selected = option_analysis.get("selected_strategy") or None
    if not selected:
        return None
    if float(selected.get("estimated_return_on_risk") or 0.0) < float(min_option_return_on_risk):
        return None
    return {
        "instrument": "option",
        "strategy": selected.get("strategy"),
        "action": "BUY_OPTION_STRUCTURE",
        "quantity": 1.0,
        "notional": None,
        "max_loss": selected.get("max_loss"),
        "stop_loss_pct": None,
        "expected_pnl": selected.get("estimated_pnl"),
        "realized_pnl": selected.get("realized_pnl"),
        "estimated_return_on_risk": selected.get("estimated_return_on_risk"),
        "realized_return_on_risk": selected.get("realized_return_on_risk"),
        "score": selected.get("score"),
        "risk_note": selected.get("risk_note"),
        "option_details": selected,
    }


def analyze_replay_row_strategy_allocation(
    row: dict[str, Any],
    *,
    underlying_reference_price: float = 100.0,
    stock_quantity: float = 10.0,
    stock_stop_loss_pct: float = 0.02,
    min_confidence: float = 0.35,
    min_stock_expected_return: float = 0.0025,
    min_dte: int = 7,
    base_implied_vol: float = 0.55,
    risk_free_rate: float = 0.04,
    contract_multiplier: float = 100.0,
    moneyness_offset_pct: float = 0.0,
    spread_width_pct: float = 0.05,
    min_option_score: float = 0.35,
    min_option_return_on_risk: float = 0.0,
    option_preference_edge: float = 0.05,
) -> dict[str, Any]:
    option_analysis = analyze_replay_row_options(
        row,
        underlying_reference_price=underlying_reference_price,
        min_dte=min_dte,
        base_implied_vol=base_implied_vol,
        risk_free_rate=risk_free_rate,
        contract_multiplier=contract_multiplier,
        moneyness_offset_pct=moneyness_offset_pct,
        spread_width_pct=spread_width_pct,
        min_option_score=min_option_score,
    )
    stock_candidate = _stock_expression_candidate(
        row,
        underlying_reference_price=underlying_reference_price,
        stock_quantity=stock_quantity,
        stock_stop_loss_pct=stock_stop_loss_pct,
        min_confidence=min_confidence,
        min_stock_expected_return=min_stock_expected_return,
    )
    option_candidate = _option_expression_candidate(
        option_analysis,
        min_option_return_on_risk=min_option_return_on_risk,
    )
    candidates = [candidate for candidate in (stock_candidate, option_candidate) if candidate is not None]
    selected: dict[str, Any] | None = None
    block_reason = None
    if stock_candidate and option_candidate:
        stock_score = float(stock_candidate.get("score") or 0.0)
        option_score = float(option_candidate.get("score") or 0.0)
        selected = option_candidate if option_score >= stock_score + float(option_preference_edge) else stock_candidate
    elif candidates:
        selected = candidates[0]
    else:
        block_reason = "no_stock_or_option_expression_candidate"
    realized_candidates = [candidate for candidate in candidates if candidate.get("realized_pnl") is not None]
    hindsight_best = max(realized_candidates, key=lambda item: float(item.get("realized_pnl") or 0.0), default=None)
    selected_realized = selected.get("realized_pnl") if selected else None
    regret = None
    if hindsight_best is not None and selected_realized is not None:
        regret = float(hindsight_best.get("realized_pnl") or 0.0) - float(selected_realized)
    return {
        "allocation_version": STRATEGY_ALLOCATION_VERSION,
        "score_id": row.get("id"),
        "outcome_id": row.get("outcome_id"),
        "symbol": row.get("symbol"),
        "horizon": row.get("horizon"),
        "replay_timestamp": row.get("replay_timestamp"),
        "predicted_direction": row.get("predicted_direction"),
        "expected_return": row.get("expected_return"),
        "confidence": row.get("confidence"),
        "actual_excess_return": row.get("actual_excess_return"),
        "direction_hit": row.get("direction_hit"),
        "trade_action": _trade_action_from_row(row),
        "allocation_status": "candidate" if selected else "blocked",
        "block_reason": block_reason,
        "selected_expression": selected,
        "expression_candidates": candidates,
        "stock_candidate": stock_candidate,
        "option_candidate": option_candidate,
        "hindsight_best_expression": hindsight_best,
        "allocation_regret": regret,
        "safety": {
            "paper_only": True,
            "uses_live_option_chain": False,
            "uses_live_option_quotes": False,
            "ibkr_order_submitted": False,
            "account_or_portfolio_checked": False,
            "notes": "Paper-only stock-vs-option expression allocation. Selection uses signal-time fields only; realized PnL and regret are evaluation fields.",
        },
    }


def _group_allocations(allocations: list[dict[str, Any]], key_fn: Any, *, min_rows: int) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in allocations:
        groups.setdefault(str(key_fn(item) or "unknown"), []).append(item)
    rows: list[dict[str, Any]] = []
    for bucket, members in groups.items():
        if len(members) < min_rows:
            continue
        selected = [item for item in members if item.get("selected_expression")]
        realized = [
            float((item.get("selected_expression") or {}).get("realized_pnl"))
            for item in selected
            if (item.get("selected_expression") or {}).get("realized_pnl") is not None
        ]
        regrets = [float(item.get("allocation_regret")) for item in selected if item.get("allocation_regret") is not None]
        rows.append(
            {
                "bucket": bucket,
                "rows": len(members),
                "selected": len(selected),
                "selection_rate": len(selected) / len(members) if members else None,
                "positive_realized": sum(1 for value in realized if value > 0.0),
                "evaluated": len(realized),
                "positive_realized_rate": sum(1 for value in realized if value > 0.0) / len(realized) if realized else None,
                "total_realized_pnl": sum(realized),
                "avg_realized_pnl": sum(realized) / len(realized) if realized else None,
                "avg_regret": sum(regrets) / len(regrets) if regrets else None,
            }
        )
    rows.sort(key=lambda item: (-float(item.get("total_realized_pnl") or 0.0), -int(item.get("selected") or 0), item["bucket"]))
    return rows


def strategy_allocation_summary(
    rows: list[dict[str, Any]],
    *,
    underlying_reference_price: float = 100.0,
    stock_quantity: float = 10.0,
    stock_stop_loss_pct: float = 0.02,
    min_confidence: float = 0.35,
    min_stock_expected_return: float = 0.0025,
    min_dte: int = 7,
    base_implied_vol: float = 0.55,
    risk_free_rate: float = 0.04,
    contract_multiplier: float = 100.0,
    moneyness_offset_pct: float = 0.0,
    spread_width_pct: float = 0.05,
    min_option_score: float = 0.35,
    min_option_return_on_risk: float = 0.0,
    option_preference_edge: float = 0.05,
    min_group_rows: int = 1,
    sample_limit: int = 20,
) -> dict[str, Any]:
    allocations = [
        analyze_replay_row_strategy_allocation(
            row,
            underlying_reference_price=underlying_reference_price,
            stock_quantity=stock_quantity,
            stock_stop_loss_pct=stock_stop_loss_pct,
            min_confidence=min_confidence,
            min_stock_expected_return=min_stock_expected_return,
            min_dte=min_dte,
            base_implied_vol=base_implied_vol,
            risk_free_rate=risk_free_rate,
            contract_multiplier=contract_multiplier,
            moneyness_offset_pct=moneyness_offset_pct,
            spread_width_pct=spread_width_pct,
            min_option_score=min_option_score,
            min_option_return_on_risk=min_option_return_on_risk,
            option_preference_edge=option_preference_edge,
        )
        for row in rows
    ]
    selected = [item for item in allocations if item.get("selected_expression")]
    realized = [
        float((item.get("selected_expression") or {}).get("realized_pnl"))
        for item in selected
        if (item.get("selected_expression") or {}).get("realized_pnl") is not None
    ]
    regrets = [float(item.get("allocation_regret")) for item in selected if item.get("allocation_regret") is not None]
    return {
        "allocation_version": STRATEGY_ALLOCATION_VERSION,
        "rows": len(rows),
        "selected_allocations": len(selected),
        "selection_rate": len(selected) / len(rows) if rows else None,
        "evaluated_allocations": len(realized),
        "positive_realized": sum(1 for value in realized if value > 0.0),
        "positive_realized_rate": sum(1 for value in realized if value > 0.0) / len(realized) if realized else None,
        "total_realized_pnl": sum(realized),
        "avg_realized_pnl": sum(realized) / len(realized) if realized else None,
        "avg_allocation_regret": sum(regrets) / len(regrets) if regrets else None,
        "selected_instrument_counts": dict(Counter(str((item.get("selected_expression") or {}).get("instrument") or "blocked") for item in allocations)),
        "selected_strategy_counts": dict(Counter(str((item.get("selected_expression") or {}).get("strategy") or "blocked") for item in allocations)),
        "parameters": {
            "underlying_reference_price": float(underlying_reference_price),
            "stock_quantity": float(stock_quantity),
            "stock_stop_loss_pct": float(stock_stop_loss_pct),
            "min_confidence": float(min_confidence),
            "min_stock_expected_return": float(min_stock_expected_return),
            "min_dte": int(min_dte),
            "base_implied_vol": float(base_implied_vol),
            "risk_free_rate": float(risk_free_rate),
            "contract_multiplier": float(contract_multiplier),
            "moneyness_offset_pct": float(moneyness_offset_pct),
            "spread_width_pct": float(spread_width_pct),
            "min_option_score": float(min_option_score),
            "min_option_return_on_risk": float(min_option_return_on_risk),
            "option_preference_edge": float(option_preference_edge),
        },
        "by_instrument": _group_allocations(allocations, lambda item: (item.get("selected_expression") or {}).get("instrument") or "blocked", min_rows=min_group_rows),
        "by_strategy": _group_allocations(allocations, lambda item: (item.get("selected_expression") or {}).get("strategy") or "blocked", min_rows=min_group_rows),
        "by_symbol": _group_allocations(allocations, lambda item: item.get("symbol"), min_rows=min_group_rows),
        "by_direction": _group_allocations(allocations, lambda item: item.get("predicted_direction"), min_rows=min_group_rows),
        "sample_allocations": allocations[: max(0, int(sample_limit))],
        "allocations": allocations,
        "safety": {
            "mode": "read_only_paper_stock_option_strategy_allocation",
            "policy": "No live chain, quote, liquidity, margin, account, portfolio, IBKR order, or scorer-row mutation.",
        },
    }


def _append_allocation_group_table(lines: list[str], title: str, rows: list[dict[str, Any]]) -> None:
    lines.extend(
        [
            "",
            f"## {title}",
            "",
            "| Bucket | Rows | Selected | Selection rate | Positive/eval | Positive rate | Total realized PnL | Avg realized PnL | Avg regret |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    if not rows:
        lines.append("| - | 0 | 0 | - | 0/0 | - | - | - | - |")
    for row in rows:
        lines.append(
            f"| {row['bucket']} | {row['rows']} | {row['selected']} "
            f"| {_fmt_pct(row.get('selection_rate'))} "
            f"| {row['positive_realized']}/{row['evaluated']} "
            f"| {_fmt_pct(row.get('positive_realized_rate'))} "
            f"| {_fmt_money(row.get('total_realized_pnl'))} "
            f"| {_fmt_money(row.get('avg_realized_pnl'))} "
            f"| {_fmt_money(row.get('avg_regret'))} |"
        )


def strategy_allocation_markdown(
    summary: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
    title: str = "Replay Score Stock/Option Strategy Allocation Report",
) -> str:
    filters = filters or {}
    lines = [
        f"# {title}",
        "",
        f"- Generated at: `{datetime.now(UTC).isoformat()}`",
        f"- Rows: `{summary.get('rows')}`",
        f"- Selected allocations: `{summary.get('selected_allocations')}`",
        f"- Selection rate: `{_fmt_pct(summary.get('selection_rate'))}`",
        f"- Positive realized PnL: `{summary.get('positive_realized')}/{summary.get('evaluated_allocations')} = {_fmt_pct(summary.get('positive_realized_rate'))}`",
        f"- Total realized PnL: `{_fmt_money(summary.get('total_realized_pnl'))}`",
        f"- Avg realized PnL: `{_fmt_money(summary.get('avg_realized_pnl'))}`",
        f"- Avg allocation regret vs hindsight best available expression: `{_fmt_money(summary.get('avg_allocation_regret'))}`",
        "- Safety: paper-only stock/option allocation; no live option chain, quote, liquidity, margin, account, portfolio, or order submission.",
        "",
        "## Filters",
        "",
    ]
    if filters:
        for key, value in filters.items():
            lines.append(f"- `{key}`: `{value if value is not None else '-'}`")
    else:
        lines.append("- none")
    lines.extend(["", "## Allocation parameters", ""])
    for key, value in (summary.get("parameters") or {}).items():
        lines.append(f"- `{key}`: `{value}`")
    lines.extend(["", "## Selected instrument counts", ""])
    for key, value in (summary.get("selected_instrument_counts") or {}).items():
        lines.append(f"- `{key}`: `{value}`")
    lines.extend(["", "## Selected strategy counts", ""])
    for key, value in (summary.get("selected_strategy_counts") or {}).items():
        lines.append(f"- `{key}`: `{value}`")
    _append_allocation_group_table(lines, "By selected instrument", summary.get("by_instrument") or [])
    _append_allocation_group_table(lines, "By selected strategy", summary.get("by_strategy") or [])
    _append_allocation_group_table(lines, "By symbol", summary.get("by_symbol") or [])
    _append_allocation_group_table(lines, "By predicted direction", summary.get("by_direction") or [])
    lines.extend(
        [
            "",
            "## Sample allocations",
            "",
            "| Symbol | Horizon | Direction | Instrument | Strategy | Score | Expected PnL | Max loss | Realized PnL | Hindsight best | Regret |",
            "| --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: | --- | ---: |",
        ]
    )
    for item in summary.get("sample_allocations") or []:
        selected = item.get("selected_expression") or {}
        hindsight = item.get("hindsight_best_expression") or {}
        lines.append(
            f"| {item.get('symbol')} | {item.get('horizon')} | {item.get('predicted_direction')} "
            f"| {selected.get('instrument') or 'blocked'} "
            f"| {selected.get('strategy') or item.get('block_reason') or 'blocked'} "
            f"| {float(selected.get('score') or 0.0):.3f} "
            f"| {_fmt_money(selected.get('expected_pnl'))} "
            f"| {_fmt_money(selected.get('max_loss'))} "
            f"| {_fmt_money(selected.get('realized_pnl'))} "
            f"| {hindsight.get('strategy') or '-'} "
            f"| {_fmt_money(item.get('allocation_regret'))} |"
        )
    lines.extend(
        [
            "",
            "## Interpretation note",
            "",
            "This layer chooses how to express a scorer signal on paper: stock/short-stock proxy, a single long option, or a defined-risk debit spread. The allocation decision uses only signal-time fields; realized PnL and regret are evaluation fields for later tuning.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_strategy_allocation_markdown(
    output_path: Path,
    summary: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(strategy_allocation_markdown(summary, filters=filters), encoding="utf-8")
    return output_path


def write_strategy_allocation_json(output_path: Path, summary: dict[str, Any]) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    return output_path


def _sort_strategy_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        rows,
        key=lambda row: (
            str(row.get("replay_timestamp") or ""),
            str(row.get("symbol") or ""),
            int(row.get("id") or row.get("outcome_id") or 0),
        ),
    )


def _allocation_optimizer_score(
    summary: dict[str, Any],
    *,
    min_selection_rate: float,
    pnl_weight: float,
    regret_penalty_weight: float,
    option_usage_penalty_weight: float,
) -> float:
    rows = max(1, int(summary.get("rows") or 0))
    selection_rate = float(summary.get("selection_rate") or 0.0)
    positive_rate = float(summary.get("positive_realized_rate") or 0.0)
    avg_pnl = float(summary.get("avg_realized_pnl") or 0.0)
    avg_regret = float(summary.get("avg_allocation_regret") or 0.0)
    option_count = int((summary.get("selected_instrument_counts") or {}).get("option") or 0)
    option_rate = option_count / rows
    shortfall = max(0.0, float(min_selection_rate) - selection_rate)
    return (
        positive_rate
        + float(pnl_weight) * (avg_pnl / 100.0)
        - float(regret_penalty_weight) * (avg_regret / 100.0)
        - float(option_usage_penalty_weight) * option_rate
        - 0.75 * shortfall
    )


def tune_strategy_allocation_parameters(
    rows: list[dict[str, Any]],
    *,
    underlying_reference_price: float = 100.0,
    stock_quantity: float = 10.0,
    stock_stop_loss_pct: float = 0.02,
    min_confidence_grid: tuple[float, ...] = (0.30, 0.35, 0.40),
    min_stock_expected_return_grid: tuple[float, ...] = (0.0025, 0.005, 0.01),
    min_option_score_grid: tuple[float, ...] = (0.30, 0.35, 0.40, 0.45),
    min_option_return_on_risk_grid: tuple[float, ...] = (0.0, 0.10, 0.25),
    option_preference_edge_grid: tuple[float, ...] = (0.0, 0.05, 0.10, 0.20),
    min_dte: int = 7,
    base_implied_vol: float = 0.55,
    risk_free_rate: float = 0.04,
    contract_multiplier: float = 100.0,
    moneyness_offset_pct: float = 0.0,
    spread_width_pct: float = 0.05,
    min_selection_rate: float = 0.45,
    pnl_weight: float = 0.35,
    regret_penalty_weight: float = 0.25,
    option_usage_penalty_weight: float = 0.05,
    top_n: int = 20,
) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    for min_confidence in min_confidence_grid:
        for min_stock_expected_return in min_stock_expected_return_grid:
            for min_option_score in min_option_score_grid:
                for min_option_return_on_risk in min_option_return_on_risk_grid:
                    for option_preference_edge in option_preference_edge_grid:
                        summary = strategy_allocation_summary(
                            rows,
                            underlying_reference_price=underlying_reference_price,
                            stock_quantity=stock_quantity,
                            stock_stop_loss_pct=stock_stop_loss_pct,
                            min_confidence=float(min_confidence),
                            min_stock_expected_return=float(min_stock_expected_return),
                            min_dte=min_dte,
                            base_implied_vol=base_implied_vol,
                            risk_free_rate=risk_free_rate,
                            contract_multiplier=contract_multiplier,
                            moneyness_offset_pct=moneyness_offset_pct,
                            spread_width_pct=spread_width_pct,
                            min_option_score=float(min_option_score),
                            min_option_return_on_risk=float(min_option_return_on_risk),
                            option_preference_edge=float(option_preference_edge),
                            min_group_rows=1,
                            sample_limit=0,
                        )
                        score = _allocation_optimizer_score(
                            summary,
                            min_selection_rate=min_selection_rate,
                            pnl_weight=pnl_weight,
                            regret_penalty_weight=regret_penalty_weight,
                            option_usage_penalty_weight=option_usage_penalty_weight,
                        )
                        candidates.append(
                            {
                                "params": {
                                    "min_confidence": float(min_confidence),
                                    "min_stock_expected_return": float(min_stock_expected_return),
                                    "min_option_score": float(min_option_score),
                                    "min_option_return_on_risk": float(min_option_return_on_risk),
                                    "option_preference_edge": float(option_preference_edge),
                                },
                                "score": score,
                                "passes_min_selection_rate": float(summary.get("selection_rate") or 0.0) >= float(min_selection_rate),
                                "selected_allocations": summary.get("selected_allocations"),
                                "selection_rate": summary.get("selection_rate"),
                                "positive_realized_rate": summary.get("positive_realized_rate"),
                                "positive_realized": summary.get("positive_realized"),
                                "evaluated_allocations": summary.get("evaluated_allocations"),
                                "total_realized_pnl": summary.get("total_realized_pnl"),
                                "avg_realized_pnl": summary.get("avg_realized_pnl"),
                                "avg_allocation_regret": summary.get("avg_allocation_regret"),
                                "selected_instrument_counts": summary.get("selected_instrument_counts"),
                                "selected_strategy_counts": summary.get("selected_strategy_counts"),
                            }
                        )
    candidates.sort(
        key=lambda item: (
            not bool(item.get("passes_min_selection_rate")),
            -float(item.get("score") or 0.0),
            -float(item.get("positive_realized_rate") or 0.0),
            -float(item.get("total_realized_pnl") or 0.0),
            float(item.get("avg_allocation_regret") or 0.0),
        )
    )
    selected = candidates[0] if candidates else None
    selected_summary = None
    if selected is not None:
        params = selected.get("params") or {}
        selected_summary = strategy_allocation_summary(
            rows,
            underlying_reference_price=underlying_reference_price,
            stock_quantity=stock_quantity,
            stock_stop_loss_pct=stock_stop_loss_pct,
            min_confidence=float(params.get("min_confidence") or 0.0),
            min_stock_expected_return=float(params.get("min_stock_expected_return") or 0.0),
            min_dte=min_dte,
            base_implied_vol=base_implied_vol,
            risk_free_rate=risk_free_rate,
            contract_multiplier=contract_multiplier,
            moneyness_offset_pct=moneyness_offset_pct,
            spread_width_pct=spread_width_pct,
            min_option_score=float(params.get("min_option_score") or 0.0),
            min_option_return_on_risk=float(params.get("min_option_return_on_risk") or 0.0),
            option_preference_edge=float(params.get("option_preference_edge") or 0.0),
            min_group_rows=1,
            sample_limit=20,
        )
    return {
        "rows": len(rows),
        "grid": {
            "min_confidence_grid": list(min_confidence_grid),
            "min_stock_expected_return_grid": list(min_stock_expected_return_grid),
            "min_option_score_grid": list(min_option_score_grid),
            "min_option_return_on_risk_grid": list(min_option_return_on_risk_grid),
            "option_preference_edge_grid": list(option_preference_edge_grid),
            "min_selection_rate": float(min_selection_rate),
            "pnl_weight": float(pnl_weight),
            "regret_penalty_weight": float(regret_penalty_weight),
            "option_usage_penalty_weight": float(option_usage_penalty_weight),
        },
        "fixed_parameters": {
            "underlying_reference_price": float(underlying_reference_price),
            "stock_quantity": float(stock_quantity),
            "stock_stop_loss_pct": float(stock_stop_loss_pct),
            "min_dte": int(min_dte),
            "base_implied_vol": float(base_implied_vol),
            "risk_free_rate": float(risk_free_rate),
            "contract_multiplier": float(contract_multiplier),
            "moneyness_offset_pct": float(moneyness_offset_pct),
            "spread_width_pct": float(spread_width_pct),
        },
        "candidates_evaluated": len(candidates),
        "selected": selected,
        "selected_summary": selected_summary,
        "top_candidates": candidates[: max(0, int(top_n))],
        "safety": {
            "mode": "read_only_paper_stock_option_allocation_parameter_tuning",
            "policy": "No live chain, quote, liquidity, margin, account, portfolio, IBKR order, or scorer-row mutation.",
        },
    }


def _allocation_summary_from_allocations(
    allocations: list[dict[str, Any]],
    *,
    parameters: dict[str, Any],
    min_group_rows: int = 1,
    sample_limit: int = 20,
) -> dict[str, Any]:
    selected = [item for item in allocations if item.get("selected_expression")]
    realized = [
        float((item.get("selected_expression") or {}).get("realized_pnl"))
        for item in selected
        if (item.get("selected_expression") or {}).get("realized_pnl") is not None
    ]
    regrets = [float(item.get("allocation_regret")) for item in selected if item.get("allocation_regret") is not None]
    return {
        "allocation_version": STRATEGY_ALLOCATION_VERSION,
        "rows": len(allocations),
        "selected_allocations": len(selected),
        "selection_rate": len(selected) / len(allocations) if allocations else None,
        "evaluated_allocations": len(realized),
        "positive_realized": sum(1 for value in realized if value > 0.0),
        "positive_realized_rate": sum(1 for value in realized if value > 0.0) / len(realized) if realized else None,
        "total_realized_pnl": sum(realized),
        "avg_realized_pnl": sum(realized) / len(realized) if realized else None,
        "avg_allocation_regret": sum(regrets) / len(regrets) if regrets else None,
        "selected_instrument_counts": dict(Counter(str((item.get("selected_expression") or {}).get("instrument") or "blocked") for item in allocations)),
        "selected_strategy_counts": dict(Counter(str((item.get("selected_expression") or {}).get("strategy") or "blocked") for item in allocations)),
        "parameters": parameters,
        "by_instrument": _group_allocations(allocations, lambda item: (item.get("selected_expression") or {}).get("instrument") or "blocked", min_rows=min_group_rows),
        "by_strategy": _group_allocations(allocations, lambda item: (item.get("selected_expression") or {}).get("strategy") or "blocked", min_rows=min_group_rows),
        "by_symbol": _group_allocations(allocations, lambda item: item.get("symbol"), min_rows=min_group_rows),
        "by_direction": _group_allocations(allocations, lambda item: item.get("predicted_direction"), min_rows=min_group_rows),
        "sample_allocations": allocations[: max(0, int(sample_limit))],
        "allocations": allocations,
        "safety": {
            "mode": "read_only_paper_stock_option_strategy_allocation",
            "policy": "No live chain, quote, liquidity, margin, account, portfolio, IBKR order, or scorer-row mutation.",
        },
    }


def walk_forward_strategy_allocation_tuning(
    rows: list[dict[str, Any]],
    *,
    min_train_rows: int = 240,
    test_window_rows: int = 80,
    step_rows: int | None = None,
    rolling_train_rows: int | None = None,
    underlying_reference_price: float = 100.0,
    stock_quantity: float = 10.0,
    stock_stop_loss_pct: float = 0.02,
    min_confidence_grid: tuple[float, ...] = (0.30, 0.35, 0.40),
    min_stock_expected_return_grid: tuple[float, ...] = (0.0025, 0.005, 0.01),
    min_option_score_grid: tuple[float, ...] = (0.30, 0.35, 0.40, 0.45),
    min_option_return_on_risk_grid: tuple[float, ...] = (0.0, 0.10, 0.25),
    option_preference_edge_grid: tuple[float, ...] = (0.0, 0.05, 0.10, 0.20),
    min_dte: int = 7,
    base_implied_vol: float = 0.55,
    risk_free_rate: float = 0.04,
    contract_multiplier: float = 100.0,
    moneyness_offset_pct: float = 0.0,
    spread_width_pct: float = 0.05,
    min_selection_rate: float = 0.45,
    pnl_weight: float = 0.35,
    regret_penalty_weight: float = 0.25,
    option_usage_penalty_weight: float = 0.05,
    top_n: int = 5,
    min_group_rows: int = 1,
    sample_limit: int = 20,
    checkpoint_path: Path | None = None,
) -> dict[str, Any]:
    ordered = _sort_strategy_rows(rows)
    min_train_rows = max(0, int(min_train_rows))
    test_window_rows = max(1, int(test_window_rows))
    step_rows = max(1, int(step_rows if step_rows is not None else test_window_rows))
    rolling_train_rows = int(rolling_train_rows) if rolling_train_rows is not None else None
    if rolling_train_rows is not None:
        rolling_train_rows = max(1, rolling_train_rows)
    folds: list[dict[str, Any]] = []
    all_test_allocations: list[dict[str, Any]] = []
    fold_index = 1
    test_start = min_train_rows
    fixed_parameters = {
        "underlying_reference_price": float(underlying_reference_price),
        "stock_quantity": float(stock_quantity),
        "stock_stop_loss_pct": float(stock_stop_loss_pct),
        "min_dte": int(min_dte),
        "base_implied_vol": float(base_implied_vol),
        "risk_free_rate": float(risk_free_rate),
        "contract_multiplier": float(contract_multiplier),
        "moneyness_offset_pct": float(moneyness_offset_pct),
        "spread_width_pct": float(spread_width_pct),
    }
    checkpoint_parameters = {
        "rows": len(ordered),
        "min_train_rows": min_train_rows,
        "test_window_rows": test_window_rows,
        "step_rows": step_rows,
        "rolling_train_rows": rolling_train_rows,
        "min_confidence_grid": list(min_confidence_grid),
        "min_stock_expected_return_grid": list(min_stock_expected_return_grid),
        "min_option_score_grid": list(min_option_score_grid),
        "min_option_return_on_risk_grid": list(min_option_return_on_risk_grid),
        "option_preference_edge_grid": list(option_preference_edge_grid),
        "min_selection_rate": float(min_selection_rate),
        "pnl_weight": float(pnl_weight),
        "regret_penalty_weight": float(regret_penalty_weight),
        "option_usage_penalty_weight": float(option_usage_penalty_weight),
        "top_n": int(top_n),
        **fixed_parameters,
    }
    checkpoint_signature = json.dumps(checkpoint_parameters, sort_keys=True, ensure_ascii=False)
    checkpoint_path = checkpoint_path.expanduser().resolve() if checkpoint_path is not None else None
    checkpoint_folds: dict[int, dict[str, Any]] = {}
    if checkpoint_path is not None and checkpoint_path.exists():
        try:
            checkpoint_payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            checkpoint_payload = {}
        if checkpoint_payload.get("checkpoint_signature") == checkpoint_signature:
            checkpoint_folds = {
                int(fold.get("fold")): fold
                for fold in checkpoint_payload.get("folds") or []
                if fold.get("fold") is not None
            }
    while test_start < len(ordered):
        test_end = min(len(ordered), test_start + test_window_rows)
        train_start = 0 if rolling_train_rows is None else max(0, test_start - rolling_train_rows)
        train_rows = ordered[train_start:test_start]
        test_rows = ordered[test_start:test_end]
        if not train_rows or not test_rows:
            test_start += step_rows
            continue
        if fold_index in checkpoint_folds:
            folds.append(checkpoint_folds[fold_index])
            fold_index += 1
            test_start += step_rows
            continue
        train_tuning = tune_strategy_allocation_parameters(
            train_rows,
            underlying_reference_price=underlying_reference_price,
            stock_quantity=stock_quantity,
            stock_stop_loss_pct=stock_stop_loss_pct,
            min_confidence_grid=min_confidence_grid,
            min_stock_expected_return_grid=min_stock_expected_return_grid,
            min_option_score_grid=min_option_score_grid,
            min_option_return_on_risk_grid=min_option_return_on_risk_grid,
            option_preference_edge_grid=option_preference_edge_grid,
            min_dte=min_dte,
            base_implied_vol=base_implied_vol,
            risk_free_rate=risk_free_rate,
            contract_multiplier=contract_multiplier,
            moneyness_offset_pct=moneyness_offset_pct,
            spread_width_pct=spread_width_pct,
            min_selection_rate=min_selection_rate,
            pnl_weight=pnl_weight,
            regret_penalty_weight=regret_penalty_weight,
            option_usage_penalty_weight=option_usage_penalty_weight,
            top_n=top_n,
        )
        selected = train_tuning.get("selected")
        if selected is None:
            test_start += step_rows
            continue
        params = selected.get("params") or {}
        test_summary = strategy_allocation_summary(
            test_rows,
            underlying_reference_price=underlying_reference_price,
            stock_quantity=stock_quantity,
            stock_stop_loss_pct=stock_stop_loss_pct,
            min_confidence=float(params.get("min_confidence") or 0.0),
            min_stock_expected_return=float(params.get("min_stock_expected_return") or 0.0),
            min_dte=min_dte,
            base_implied_vol=base_implied_vol,
            risk_free_rate=risk_free_rate,
            contract_multiplier=contract_multiplier,
            moneyness_offset_pct=moneyness_offset_pct,
            spread_width_pct=spread_width_pct,
            min_option_score=float(params.get("min_option_score") or 0.0),
            min_option_return_on_risk=float(params.get("min_option_return_on_risk") or 0.0),
            option_preference_edge=float(params.get("option_preference_edge") or 0.0),
            min_group_rows=min_group_rows,
            sample_limit=0,
        )
        test_allocations = list(test_summary.get("allocations") or [])
        for allocation in test_allocations:
            allocation["fold"] = fold_index
            allocation["walk_forward_selected_params"] = params
        all_test_allocations.extend(test_allocations)
        folds.append(
            {
                "fold": fold_index,
                "train_start": train_rows[0].get("replay_timestamp"),
                "train_end": train_rows[-1].get("replay_timestamp"),
                "test_start": test_rows[0].get("replay_timestamp"),
                "test_end": test_rows[-1].get("replay_timestamp"),
                "train_rows": len(train_rows),
                "test_rows": len(test_rows),
                "selected": selected,
                "test_summary": test_summary,
            }
        )
        if checkpoint_path is not None:
            checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
            checkpoint_path.write_text(
                json.dumps(
                    {
                        "checkpoint_version": "stock-option-walk-forward-checkpoint-v1",
                        "checkpoint_signature": checkpoint_signature,
                        "updated_at": datetime.now(UTC).isoformat(),
                        "parameters": checkpoint_parameters,
                        "folds": folds,
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        fold_index += 1
        test_start += step_rows
    all_test_allocations = [
        allocation
        for fold in folds
        for allocation in ((fold.get("test_summary") or {}).get("allocations") or [])
    ]
    aggregate_test_summary = _allocation_summary_from_allocations(
        all_test_allocations,
        parameters={**fixed_parameters, "walk_forward": True},
        min_group_rows=min_group_rows,
        sample_limit=sample_limit,
    )
    return {
        "rows": len(ordered),
        "folds": folds,
        "fold_count": len(folds),
        "aggregate_test_summary": aggregate_test_summary,
        "parameters": {
            "min_train_rows": min_train_rows,
            "test_window_rows": test_window_rows,
            "step_rows": step_rows,
            "rolling_train_rows": rolling_train_rows,
            "min_confidence_grid": list(min_confidence_grid),
            "min_stock_expected_return_grid": list(min_stock_expected_return_grid),
            "min_option_score_grid": list(min_option_score_grid),
            "min_option_return_on_risk_grid": list(min_option_return_on_risk_grid),
            "option_preference_edge_grid": list(option_preference_edge_grid),
            "min_selection_rate": float(min_selection_rate),
            "pnl_weight": float(pnl_weight),
            "regret_penalty_weight": float(regret_penalty_weight),
            "option_usage_penalty_weight": float(option_usage_penalty_weight),
            "top_n": int(top_n),
            "checkpoint_path": str(checkpoint_path) if checkpoint_path is not None else None,
            **fixed_parameters,
        },
        "safety": {
            "mode": "read_only_walk_forward_stock_option_strategy_allocation_tuning",
            "policy": "Each fold tunes on earlier rows and tests on later rows; no live chain, quote, liquidity, margin, account, portfolio, IBKR order, or scorer mutation.",
        },
    }


def strategy_allocation_parameter_tuning_markdown(
    result: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
    title: str = "Stock/Option Strategy Allocation Parameter Tuning Report",
) -> str:
    filters = filters or {}
    selected = result.get("selected") or {}
    selected_params = selected.get("params") or {}
    selected_summary = result.get("selected_summary") or {}
    lines = [
        f"# {title}",
        "",
        f"- Generated at: `{datetime.now(UTC).isoformat()}`",
        f"- Rows: `{result.get('rows')}`",
        f"- Candidates evaluated: `{result.get('candidates_evaluated')}`",
        f"- Selected params: `{selected_params}`",
        f"- Selected score: `{float(selected.get('score') or 0.0):.4f}`",
        f"- Selected allocations: `{selected_summary.get('selected_allocations')}`",
        f"- Selected positive rate: `{selected_summary.get('positive_realized')}/{selected_summary.get('evaluated_allocations')} = {_fmt_pct(selected_summary.get('positive_realized_rate'))}`",
        f"- Selected total PnL: `{_fmt_money(selected_summary.get('total_realized_pnl'))}`",
        "- Safety: read-only paper tuning; no live option chain, quote, liquidity, margin, account, portfolio, or order submission.",
        "",
        "## Filters",
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
            "## Top candidates",
            "",
            "| Rank | Params | Score | Selected | Selection rate | Positive rate | Total PnL | Avg PnL | Avg regret | Instruments |",
            "| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    for index, candidate in enumerate(result.get("top_candidates") or [], start=1):
        lines.append(
            f"| {index} | `{candidate.get('params')}` "
            f"| {float(candidate.get('score') or 0.0):.4f} "
            f"| {candidate.get('selected_allocations')} "
            f"| {_fmt_pct(candidate.get('selection_rate'))} "
            f"| {_fmt_pct(candidate.get('positive_realized_rate'))} "
            f"| {_fmt_money(candidate.get('total_realized_pnl'))} "
            f"| {_fmt_money(candidate.get('avg_realized_pnl'))} "
            f"| {_fmt_money(candidate.get('avg_allocation_regret'))} "
            f"| `{candidate.get('selected_instrument_counts')}` |"
        )
    _append_allocation_group_table(lines, "Selected summary by instrument", selected_summary.get("by_instrument") or [])
    _append_allocation_group_table(lines, "Selected summary by strategy", selected_summary.get("by_strategy") or [])
    lines.extend(
        [
            "",
            "## Interpretation note",
            "",
            "This full-sample tuner is useful for scanning parameter space, but it can overfit. Prefer the walk-forward report for safer operating estimates.",
        ]
    )
    return "\n".join(lines) + "\n"


def walk_forward_strategy_allocation_tuning_markdown(
    result: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
    title: str = "Walk-Forward Stock/Option Strategy Allocation Tuning Report",
) -> str:
    filters = filters or {}
    aggregate = result.get("aggregate_test_summary") or {}
    params = result.get("parameters") or {}
    lines = [
        f"# {title}",
        "",
        f"- Generated at: `{datetime.now(UTC).isoformat()}`",
        f"- Rows: `{result.get('rows')}`",
        f"- Folds: `{result.get('fold_count')}`",
        f"- Aggregate test selected allocations: `{aggregate.get('selected_allocations')}`",
        f"- Aggregate test selection rate: `{_fmt_pct(aggregate.get('selection_rate'))}`",
        f"- Aggregate test positive rate: `{aggregate.get('positive_realized')}/{aggregate.get('evaluated_allocations')} = {_fmt_pct(aggregate.get('positive_realized_rate'))}`",
        f"- Aggregate test total PnL: `{_fmt_money(aggregate.get('total_realized_pnl'))}`",
        f"- Aggregate test avg PnL: `{_fmt_money(aggregate.get('avg_realized_pnl'))}`",
        f"- Aggregate test avg regret: `{_fmt_money(aggregate.get('avg_allocation_regret'))}`",
        "- Safety: read-only walk-forward tuning; no live option chain, quote, liquidity, margin, account, portfolio, or order submission.",
        "",
        "## Filters",
        "",
    ]
    if filters:
        for key, value in filters.items():
            lines.append(f"- `{key}`: `{value if value is not None else '-'}`")
    else:
        lines.append("- none")
    lines.extend(["", "## Walk-forward setup", ""])
    for key, value in params.items():
        lines.append(f"- `{key}`: `{value if value is not None else 'expanding'}`")
    lines.extend(
        [
            "",
            "## Fold results",
            "",
            "| Fold | Train window | Test window | Train rows | Test rows | Selected params | Train score | Test selected | Test positive rate | Test total PnL | Test avg regret |",
            "| ---: | --- | --- | ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    if not result.get("folds"):
        lines.append("| - | - | - | 0 | 0 | - | - | 0 | - | - | - |")
    for fold in result.get("folds") or []:
        selected = fold.get("selected") or {}
        test_summary = fold.get("test_summary") or {}
        lines.append(
            f"| {fold.get('fold')} "
            f"| {fold.get('train_start')} to {fold.get('train_end')} "
            f"| {fold.get('test_start')} to {fold.get('test_end')} "
            f"| {fold.get('train_rows')} "
            f"| {fold.get('test_rows')} "
            f"| `{selected.get('params')}` "
            f"| {float(selected.get('score') or 0.0):.4f} "
            f"| {test_summary.get('selected_allocations')} "
            f"| {test_summary.get('positive_realized')}/{test_summary.get('evaluated_allocations')} = {_fmt_pct(test_summary.get('positive_realized_rate'))} "
            f"| {_fmt_money(test_summary.get('total_realized_pnl'))} "
            f"| {_fmt_money(test_summary.get('avg_allocation_regret'))} |"
        )
    _append_allocation_group_table(lines, "Aggregate test by selected instrument", aggregate.get("by_instrument") or [])
    _append_allocation_group_table(lines, "Aggregate test by selected strategy", aggregate.get("by_strategy") or [])
    _append_allocation_group_table(lines, "Aggregate test by symbol", aggregate.get("by_symbol") or [])
    lines.extend(
        [
            "",
            "## Interpretation note",
            "",
            "Each fold selects stock/option allocation parameters using only earlier replay rows, then applies those selected parameters to later rows. Treat aggregate test results as a safer paper operating estimate than full-sample tuning.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_strategy_allocation_parameter_tuning_markdown(
    output_path: Path,
    result: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(strategy_allocation_parameter_tuning_markdown(result, filters=filters), encoding="utf-8")
    return output_path


def write_walk_forward_strategy_allocation_tuning_markdown(
    output_path: Path,
    result: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(walk_forward_strategy_allocation_tuning_markdown(result, filters=filters), encoding="utf-8")
    return output_path


def option_liquidity_profile_from_quote_snapshot(
    quote_snapshot: dict[str, Any],
    *,
    max_spread_pct: float = 0.30,
    min_quoted_contracts_per_symbol: int = 1,
    min_volume: int = 0,
    min_open_interest: int = 0,
    require_iv: bool = False,
) -> dict[str, dict[str, Any]]:
    profiles: dict[str, dict[str, Any]] = {}
    for quote in quote_snapshot.get("quotes") or []:
        symbol = str(quote.get("symbol") or "").upper()
        if not symbol:
            continue
        profile = profiles.setdefault(
            symbol,
            {
                "symbol": symbol,
                "contracts_seen": 0,
                "quoted_contracts": 0,
                "close_only_contracts": 0,
                "no_quote_contracts": 0,
                "wide_spread_contracts": 0,
                "spread_pcts": [],
                "volume_values": [],
                "open_interest_values": [],
                "iv_values": [],
                "market_data_types": [],
                "delayed_values": [],
            },
        )
        profile["contracts_seen"] += 1
        status = str(quote.get("quote_status") or "unknown")
        if status == "quoted":
            profile["quoted_contracts"] += 1
        elif status == "wide_spread":
            profile["wide_spread_contracts"] += 1
        elif status == "close_only":
            profile["close_only_contracts"] += 1
        elif status == "no_quote":
            profile["no_quote_contracts"] += 1
        if quote.get("spread_pct") is not None:
            profile["spread_pcts"].append(float(quote.get("spread_pct")))
        if quote.get("volume") is not None:
            profile["volume_values"].append(int(quote.get("volume") or 0))
        if quote.get("open_interest") is not None:
            profile["open_interest_values"].append(int(quote.get("open_interest") or 0))
        option_comp = quote.get("option_computation") or {}
        for comp in option_comp.values():
            if isinstance(comp, dict) and comp.get("implied_vol") is not None:
                profile["iv_values"].append(float(comp.get("implied_vol")))
        if quote.get("market_data_type") is not None:
            profile["market_data_types"].append(int(quote.get("market_data_type")))
        if quote.get("delayed") is not None:
            profile["delayed_values"].append(bool(quote.get("delayed")))
    for profile in profiles.values():
        spreads = profile.pop("spread_pcts")
        volume_values = profile.pop("volume_values")
        oi_values = profile.pop("open_interest_values")
        iv_values = profile.pop("iv_values")
        profile["avg_spread_pct"] = sum(spreads) / len(spreads) if spreads else None
        profile["max_volume"] = max(volume_values) if volume_values else None
        profile["max_open_interest"] = max(oi_values) if oi_values else None
        profile["iv_count"] = len(iv_values)
        profile["market_data_types"] = sorted(set(profile["market_data_types"]))
        profile["has_delayed"] = any(profile.pop("delayed_values"))
        spread_ok = profile["avg_spread_pct"] is not None and float(profile["avg_spread_pct"]) <= float(max_spread_pct)
        quoted_ok = int(profile["quoted_contracts"]) >= int(min_quoted_contracts_per_symbol)
        volume_ok = int(profile["max_volume"] or 0) >= int(min_volume)
        oi_ok = int(profile["max_open_interest"] or 0) >= int(min_open_interest)
        iv_ok = bool(profile["iv_count"]) if require_iv else True
        profile["liquidity_pass"] = bool(quoted_ok and spread_ok and volume_ok and oi_ok and iv_ok)
        failed = []
        if not quoted_ok:
            failed.append("insufficient_quoted_contracts")
        if not spread_ok:
            failed.append("spread_unavailable_or_too_wide")
        if not volume_ok:
            failed.append("insufficient_volume")
        if not oi_ok:
            failed.append("insufficient_open_interest")
        if not iv_ok:
            failed.append("missing_implied_volatility")
        profile["failed_reasons"] = failed
    return profiles


def apply_option_liquidity_gate_to_allocations(
    allocation_payload: dict[str, Any],
    quote_snapshot: dict[str, Any],
    *,
    max_spread_pct: float = 0.30,
    min_quoted_contracts_per_symbol: int = 1,
    min_volume: int = 0,
    min_open_interest: int = 0,
    require_iv: bool = False,
    fallback_to_stock: bool = True,
    min_group_rows: int = 1,
    sample_limit: int = 20,
) -> dict[str, Any]:
    if "result" in allocation_payload:
        source_result = allocation_payload.get("result") or {}
    else:
        source_result = allocation_payload
    source_summary = source_result.get("aggregate_test_summary") or source_payload_summary(source_result)
    source_allocations = source_summary.get("allocations") or []
    profiles = option_liquidity_profile_from_quote_snapshot(
        quote_snapshot,
        max_spread_pct=max_spread_pct,
        min_quoted_contracts_per_symbol=min_quoted_contracts_per_symbol,
        min_volume=min_volume,
        min_open_interest=min_open_interest,
        require_iv=require_iv,
    )
    gated_allocations: list[dict[str, Any]] = []
    option_seen = option_passed = option_downgraded = option_blocked = 0
    for item in source_allocations:
        allocation = json.loads(json.dumps(item))
        selected = allocation.get("selected_expression") or {}
        if selected.get("instrument") != "option":
            allocation["liquidity_gate"] = {
                "status": "not_required",
                "reason": "selected_expression_is_not_option",
            }
            gated_allocations.append(allocation)
            continue
        option_seen += 1
        symbol = str(allocation.get("symbol") or "").upper()
        profile = profiles.get(symbol)
        if profile and profile.get("liquidity_pass"):
            option_passed += 1
            allocation["liquidity_gate"] = {
                "status": "passed",
                "symbol": symbol,
                "profile": profile,
            }
            gated_allocations.append(allocation)
            continue
        failed_reasons = list((profile or {}).get("failed_reasons") or ["missing_symbol_quote_profile"])
        stock_candidate = allocation.get("stock_candidate")
        if fallback_to_stock and stock_candidate:
            option_downgraded += 1
            allocation["selected_expression"] = stock_candidate
            allocation["allocation_status"] = "candidate"
            allocation["liquidity_gate"] = {
                "status": "downgraded_to_stock",
                "symbol": symbol,
                "failed_reasons": failed_reasons,
                "profile": profile,
            }
        else:
            option_blocked += 1
            allocation["selected_expression"] = None
            allocation["allocation_status"] = "blocked"
            allocation["block_reason"] = "option_liquidity_gate_failed"
            allocation["liquidity_gate"] = {
                "status": "blocked_option",
                "symbol": symbol,
                "failed_reasons": failed_reasons,
                "profile": profile,
            }
        gated_allocations.append(allocation)
    parameters = dict((source_summary.get("parameters") or {}))
    parameters["liquidity_gate"] = {
        "max_spread_pct": float(max_spread_pct),
        "min_quoted_contracts_per_symbol": int(min_quoted_contracts_per_symbol),
        "min_volume": int(min_volume),
        "min_open_interest": int(min_open_interest),
        "require_iv": bool(require_iv),
        "fallback_to_stock": bool(fallback_to_stock),
    }
    gated_summary = _allocation_summary_from_allocations(
        gated_allocations,
        parameters=parameters,
        min_group_rows=min_group_rows,
        sample_limit=sample_limit,
    )
    return {
        "liquidity_gate_version": "paper-option-liquidity-gate-v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "source_rows": source_summary.get("rows"),
        "source_selected_allocations": source_summary.get("selected_allocations"),
        "quote_snapshot_version": quote_snapshot.get("snapshot_version"),
        "quote_generated_at": quote_snapshot.get("generated_at"),
        "liquidity_profiles": profiles,
        "option_gate_counts": {
            "option_allocations_seen": option_seen,
            "option_allocations_passed": option_passed,
            "option_allocations_downgraded_to_stock": option_downgraded,
            "option_allocations_blocked": option_blocked,
        },
        "gated_summary": gated_summary,
        "safety": {
            "mode": "read_only_option_liquidity_gate_over_paper_allocations",
            "policy": "Uses stored allocation JSON and read-only IBKR option quote snapshot JSON; no live order, account, margin, or portfolio action.",
        },
    }


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _option_quote_leg_is_eligible(quote: dict[str, Any], *, max_leg_spread_pct: float) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    bid = _float_or_none(quote.get("bid"))
    ask = _float_or_none(quote.get("ask"))
    spread_pct = _float_or_none(quote.get("spread_pct"))
    if str(quote.get("quote_status") or "").lower() != "quoted":
        reasons.append("quote_status_not_quoted")
    if bid is None or ask is None or bid <= 0.0 or ask <= 0.0:
        reasons.append("missing_positive_bid_ask")
    if bid is not None and ask is not None and ask < bid:
        reasons.append("ask_below_bid")
    if spread_pct is None or spread_pct > float(max_leg_spread_pct):
        reasons.append("spread_unavailable_or_too_wide")
    return not reasons, reasons


def _option_quotes_by_symbol_right(quote_snapshot: dict[str, Any], *, max_leg_spread_pct: float) -> dict[str, dict[str, list[dict[str, Any]]]]:
    grouped: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for raw_quote in quote_snapshot.get("quotes") or []:
        if not isinstance(raw_quote, dict):
            continue
        quote = dict(raw_quote)
        symbol = str(quote.get("symbol") or "").upper()
        right = str(quote.get("right") or "").upper()
        if not symbol or right not in {"C", "P"}:
            continue
        eligible, reasons = _option_quote_leg_is_eligible(quote, max_leg_spread_pct=max_leg_spread_pct)
        quote["leg_quote_eligible"] = eligible
        quote["leg_quote_failed_reasons"] = reasons
        grouped.setdefault(symbol, {}).setdefault(right, []).append(quote)
    for by_right in grouped.values():
        for quotes in by_right.values():
            quotes.sort(
                key=lambda item: (
                    str(item.get("expiration") or ""),
                    int(item.get("dte") or 0),
                    abs(float(item.get("moneyness") or 0.0)),
                    float(item.get("strike") or 0.0),
                )
            )
    return grouped


def _quote_leg_payload(quote: dict[str, Any], *, role: str) -> dict[str, Any]:
    return {
        "role": role,
        "symbol": quote.get("symbol"),
        "expiration": quote.get("expiration"),
        "dte": quote.get("dte"),
        "strike": quote.get("strike"),
        "right": quote.get("right"),
        "bid": quote.get("bid"),
        "ask": quote.get("ask"),
        "mid": quote.get("mid"),
        "spread_pct": quote.get("spread_pct"),
        "volume": quote.get("volume"),
        "open_interest": quote.get("open_interest"),
        "delayed": quote.get("delayed"),
        "market_data_type": quote.get("market_data_type"),
        "request_id": quote.get("request_id"),
        "trading_class": quote.get("trading_class"),
        "multiplier": quote.get("multiplier"),
    }


def _leg_multiplier(*quotes: dict[str, Any]) -> float:
    for quote in quotes:
        multiplier = _float_or_none(quote.get("multiplier"))
        if multiplier and multiplier > 0:
            return multiplier
    return 100.0


def _select_option_leg_quote_match(
    allocation: dict[str, Any],
    quotes_by_symbol_right: dict[str, dict[str, list[dict[str, Any]]]],
    *,
    max_leg_spread_pct: float,
) -> dict[str, Any]:
    symbol = str(allocation.get("symbol") or "").upper()
    selected = allocation.get("selected_expression") or {}
    option_details = selected.get("option_details") or {}
    strategy = str(option_details.get("strategy") or selected.get("strategy") or "").lower()
    if not symbol:
        return {"status": "failed", "failed_reasons": ["missing_allocation_symbol"]}
    if not strategy:
        return {"status": "failed", "symbol": symbol, "failed_reasons": ["missing_option_strategy"]}
    if "call" in strategy:
        right = "C"
        spread_side = "higher"
    elif "put" in strategy:
        right = "P"
        spread_side = "lower"
    else:
        return {"status": "failed", "symbol": symbol, "failed_reasons": [f"unsupported_strategy:{strategy}"]}

    quotes = quotes_by_symbol_right.get(symbol, {}).get(right, [])
    eligible_quotes = [
        quote
        for quote in quotes
        if quote.get("leg_quote_eligible")
        and _float_or_none(quote.get("strike")) is not None
        and _float_or_none(quote.get("ask")) is not None
        and _float_or_none(quote.get("bid")) is not None
    ]
    if not eligible_quotes:
        rejected = [quote.get("leg_quote_failed_reasons") for quote in quotes[:5]]
        return {
            "status": "failed",
            "symbol": symbol,
            "strategy": strategy,
            "right": right,
            "failed_reasons": ["no_eligible_long_leg_quote"],
            "sample_rejected_reasons": rejected,
        }

    long_quote = min(
        eligible_quotes,
        key=lambda item: (
            str(item.get("expiration") or ""),
            abs(float(item.get("moneyness") or 0.0)),
            abs(float(item.get("strike") or 0.0) - float(item.get("reference_price") or item.get("strike") or 0.0)),
            float(item.get("strike") or 0.0),
        ),
    )
    long_strike = float(long_quote.get("strike") or 0.0)
    long_ask = float(long_quote.get("ask") or 0.0)
    same_expiry = [
        quote
        for quote in eligible_quotes
        if str(quote.get("expiration") or "") == str(long_quote.get("expiration") or "")
        and quote is not long_quote
    ]
    is_spread = "spread" in strategy
    if is_spread:
        if spread_side == "higher":
            short_candidates = [quote for quote in same_expiry if float(quote.get("strike") or 0.0) > long_strike]
            short_candidates.sort(key=lambda item: float(item.get("strike") or 0.0))
        else:
            short_candidates = [quote for quote in same_expiry if float(quote.get("strike") or 0.0) < long_strike]
            short_candidates.sort(key=lambda item: float(item.get("strike") or 0.0), reverse=True)
        if not short_candidates:
            return {
                "status": "failed",
                "symbol": symbol,
                "strategy": strategy,
                "right": right,
                "long_leg": _quote_leg_payload(long_quote, role="long"),
                "failed_reasons": [f"missing_{spread_side}_short_leg_same_expiration"],
                "eligible_contracts_same_right": len(eligible_quotes),
                "max_leg_spread_pct": float(max_leg_spread_pct),
            }
        short_quote = short_candidates[0]
        short_bid = float(short_quote.get("bid") or 0.0)
        net_debit = max(0.0, long_ask - short_bid)
        width = abs(float(short_quote.get("strike") or 0.0) - long_strike)
        multiplier = _leg_multiplier(long_quote, short_quote)
        max_loss = net_debit * multiplier
        max_profit = max(0.0, width - net_debit) * multiplier
        return {
            "status": "passed",
            "symbol": symbol,
            "strategy": strategy,
            "right": right,
            "legs": [
                _quote_leg_payload(long_quote, role="long"),
                _quote_leg_payload(short_quote, role="short"),
            ],
            "estimated_live_net_debit": net_debit,
            "estimated_live_width": width,
            "estimated_live_max_loss": max_loss,
            "estimated_live_max_profit": max_profit,
            "multiplier": multiplier,
            "max_leg_spread_pct": float(max_leg_spread_pct),
        }

    multiplier = _leg_multiplier(long_quote)
    return {
        "status": "passed",
        "symbol": symbol,
        "strategy": strategy,
        "right": right,
        "legs": [_quote_leg_payload(long_quote, role="long")],
        "estimated_live_net_debit": long_ask,
        "estimated_live_max_loss": long_ask * multiplier,
        "estimated_live_max_profit": None,
        "multiplier": multiplier,
        "max_leg_spread_pct": float(max_leg_spread_pct),
    }


def apply_option_leg_quote_gate_to_allocations(
    allocation_payload: dict[str, Any],
    quote_snapshot: dict[str, Any],
    *,
    max_leg_spread_pct: float = 0.30,
    fallback_to_stock: bool = True,
    min_group_rows: int = 1,
    sample_limit: int = 20,
) -> dict[str, Any]:
    if "result" in allocation_payload:
        source_result = allocation_payload.get("result") or {}
    else:
        source_result = allocation_payload
    source_summary = source_result.get("aggregate_test_summary") or source_payload_summary(source_result)
    source_allocations = source_summary.get("allocations") or []
    quotes_by_symbol_right = _option_quotes_by_symbol_right(quote_snapshot, max_leg_spread_pct=max_leg_spread_pct)
    gated_allocations: list[dict[str, Any]] = []
    option_seen = option_passed = option_downgraded = option_blocked = 0
    failed_reason_counts: Counter[str] = Counter()
    matched_strategy_counts: Counter[str] = Counter()
    for item in source_allocations:
        allocation = json.loads(json.dumps(item))
        selected = allocation.get("selected_expression") or {}
        if selected.get("instrument") != "option":
            allocation["leg_quote_gate"] = {
                "status": "not_required",
                "reason": "selected_expression_is_not_option",
            }
            gated_allocations.append(allocation)
            continue
        option_seen += 1
        match = _select_option_leg_quote_match(
            allocation,
            quotes_by_symbol_right,
            max_leg_spread_pct=max_leg_spread_pct,
        )
        if match.get("status") == "passed":
            option_passed += 1
            strategy = str(match.get("strategy") or selected.get("strategy") or "unknown")
            matched_strategy_counts[strategy] += 1
            allocation["leg_quote_gate"] = match
            selected["live_leg_quote_match"] = match
            if match.get("estimated_live_net_debit") is not None:
                selected["estimated_live_net_debit"] = match.get("estimated_live_net_debit")
            if match.get("estimated_live_max_loss") is not None:
                selected["estimated_live_max_loss"] = match.get("estimated_live_max_loss")
            if match.get("estimated_live_max_profit") is not None:
                selected["estimated_live_max_profit"] = match.get("estimated_live_max_profit")
            allocation["selected_expression"] = selected
            gated_allocations.append(allocation)
            continue

        failed_reasons = list(match.get("failed_reasons") or ["unknown_leg_quote_gate_failure"])
        for reason in failed_reasons:
            failed_reason_counts[str(reason)] += 1
        stock_candidate = allocation.get("stock_candidate")
        if fallback_to_stock and stock_candidate:
            option_downgraded += 1
            allocation["selected_expression"] = stock_candidate
            allocation["allocation_status"] = "candidate"
            allocation["leg_quote_gate"] = {
                **match,
                "status": "downgraded_to_stock",
                "failed_reasons": failed_reasons,
            }
        else:
            option_blocked += 1
            allocation["selected_expression"] = None
            allocation["allocation_status"] = "blocked"
            allocation["block_reason"] = "option_leg_quote_gate_failed"
            allocation["leg_quote_gate"] = {
                **match,
                "status": "blocked_option",
                "failed_reasons": failed_reasons,
            }
        gated_allocations.append(allocation)

    parameters = dict((source_summary.get("parameters") or {}))
    parameters["option_leg_quote_gate"] = {
        "max_leg_spread_pct": float(max_leg_spread_pct),
        "fallback_to_stock": bool(fallback_to_stock),
        "matching_note": "Uses current live quote snapshot legs around actual market ATM; replay theoretical strikes are normalized and are not mapped one-to-one to live strikes.",
    }
    gated_summary = _allocation_summary_from_allocations(
        gated_allocations,
        parameters=parameters,
        min_group_rows=min_group_rows,
        sample_limit=sample_limit,
    )
    return {
        "leg_quote_gate_version": "paper-option-leg-quote-gate-v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "source_rows": source_summary.get("rows"),
        "source_selected_allocations": source_summary.get("selected_allocations"),
        "quote_snapshot_version": quote_snapshot.get("snapshot_version"),
        "quote_generated_at": quote_snapshot.get("generated_at"),
        "quote_requested_contracts": quote_snapshot.get("requested_quotes"),
        "quote_quoted_contracts": quote_snapshot.get("quoted_contracts"),
        "quote_quoted_rate": quote_snapshot.get("quoted_rate"),
        "quote_avg_spread_pct": quote_snapshot.get("avg_spread_pct"),
        "option_gate_counts": {
            "option_allocations_seen": option_seen,
            "option_allocations_passed": option_passed,
            "option_allocations_downgraded_to_stock": option_downgraded,
            "option_allocations_blocked": option_blocked,
        },
        "matched_strategy_counts": dict(matched_strategy_counts),
        "failed_reason_counts": dict(failed_reason_counts),
        "gated_summary": gated_summary,
        "safety": {
            "mode": "read_only_option_leg_quote_gate_over_paper_allocations",
            "policy": "Uses stored allocation JSON and read-only IBKR option quote snapshot JSON; no live order, account, margin, or portfolio action.",
        },
    }


def source_payload_summary(source_result: dict[str, Any]) -> dict[str, Any]:
    if source_result.get("summary"):
        return source_result.get("summary") or {}
    return source_result


def option_liquidity_gate_markdown(
    result: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
    title: str = "Paper Option Liquidity Gate Report",
) -> str:
    filters = filters or {}
    gated = result.get("gated_summary") or {}
    counts = result.get("option_gate_counts") or {}
    lines = [
        f"# {title}",
        "",
        f"- Generated at: `{result.get('generated_at')}`",
        f"- Quote generated at: `{result.get('quote_generated_at')}`",
        f"- Source selected allocations: `{result.get('source_selected_allocations')}`",
        f"- Gated selected allocations: `{gated.get('selected_allocations')}`",
        f"- Gated selection rate: `{_fmt_pct(gated.get('selection_rate'))}`",
        f"- Gated positive rate: `{gated.get('positive_realized')}/{gated.get('evaluated_allocations')} = {_fmt_pct(gated.get('positive_realized_rate'))}`",
        f"- Gated total PnL: `{_fmt_money(gated.get('total_realized_pnl'))}`",
        f"- Option allocations seen: `{counts.get('option_allocations_seen')}`",
        f"- Option passed: `{counts.get('option_allocations_passed')}`",
        f"- Option downgraded to stock: `{counts.get('option_allocations_downgraded_to_stock')}`",
        f"- Option blocked: `{counts.get('option_allocations_blocked')}`",
        "- Safety: read-only liquidity gate over paper allocations; no account, margin, portfolio, or order submission.",
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
            "## Liquidity profiles",
            "",
            "| Symbol | Contracts | Quoted | Close only | No quote | Avg spread | Max vol | Max OI | IV count | Pass | Failed reasons |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |",
        ]
    )
    for symbol, profile in sorted((result.get("liquidity_profiles") or {}).items()):
        lines.append(
            f"| {symbol} | {profile.get('contracts_seen')} | {profile.get('quoted_contracts')} "
            f"| {profile.get('close_only_contracts')} | {profile.get('no_quote_contracts')} "
            f"| {_fmt_pct(profile.get('avg_spread_pct'))} "
            f"| {profile.get('max_volume') if profile.get('max_volume') is not None else '-'} "
            f"| {profile.get('max_open_interest') if profile.get('max_open_interest') is not None else '-'} "
            f"| {profile.get('iv_count')} | {profile.get('liquidity_pass')} "
            f"| {','.join(profile.get('failed_reasons') or []) or '-'} |"
        )
    _append_allocation_group_table(lines, "Gated summary by selected instrument", gated.get("by_instrument") or [])
    _append_allocation_group_table(lines, "Gated summary by selected strategy", gated.get("by_strategy") or [])
    lines.extend(
        [
            "",
            "## Interpretation note",
            "",
            "This gate is deliberately strict: an option allocation must have symbol-level option quotes with positive bid/ask and acceptable spread before it remains an option. If enabled, stock fallback turns failed option selections into their stock expression when a stock candidate exists.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_option_liquidity_gate_markdown(
    output_path: Path,
    result: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(option_liquidity_gate_markdown(result, filters=filters), encoding="utf-8")
    return output_path


def option_leg_quote_gate_markdown(
    result: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
    title: str = "Paper Option Leg Quote Gate Report",
) -> str:
    filters = filters or {}
    gated = result.get("gated_summary") or {}
    counts = result.get("option_gate_counts") or {}
    lines = [
        f"# {title}",
        "",
        f"- Generated at: `{result.get('generated_at')}`",
        f"- Quote generated at: `{result.get('quote_generated_at')}`",
        f"- Source selected allocations: `{result.get('source_selected_allocations')}`",
        f"- Gated selected allocations: `{gated.get('selected_allocations')}`",
        f"- Gated selection rate: `{_fmt_pct(gated.get('selection_rate'))}`",
        f"- Gated positive rate: `{gated.get('positive_realized')}/{gated.get('evaluated_allocations')} = {_fmt_pct(gated.get('positive_realized_rate'))}`",
        f"- Gated total PnL: `{_fmt_money(gated.get('total_realized_pnl'))}`",
        f"- Quote contracts: `{result.get('quote_quoted_contracts')}/{result.get('quote_requested_contracts')} = {_fmt_pct(result.get('quote_quoted_rate'))}`",
        f"- Quote average spread: `{_fmt_pct(result.get('quote_avg_spread_pct'))}`",
        f"- Option allocations seen: `{counts.get('option_allocations_seen')}`",
        f"- Option passed with live leg match: `{counts.get('option_allocations_passed')}`",
        f"- Option downgraded to stock: `{counts.get('option_allocations_downgraded_to_stock')}`",
        f"- Option blocked: `{counts.get('option_allocations_blocked')}`",
        "- Safety: read-only leg quote gate over paper allocations; no account, margin, portfolio, or order submission.",
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
            "## Matched strategies",
            "",
            "| Strategy | Matches |",
            "| --- | ---: |",
        ]
    )
    matched = result.get("matched_strategy_counts") or {}
    if matched:
        for strategy, count in sorted(matched.items()):
            lines.append(f"| {strategy} | {count} |")
    else:
        lines.append("| - | 0 |")
    lines.extend(
        [
            "",
            "## Failed reasons",
            "",
            "| Reason | Count |",
            "| --- | ---: |",
        ]
    )
    failed = result.get("failed_reason_counts") or {}
    if failed:
        for reason, count in sorted(failed.items(), key=lambda item: (-int(item[1]), item[0])):
            lines.append(f"| {reason} | {count} |")
    else:
        lines.append("| - | 0 |")
    _append_allocation_group_table(lines, "Gated summary by selected instrument", gated.get("by_instrument") or [])
    _append_allocation_group_table(lines, "Gated summary by selected strategy", gated.get("by_strategy") or [])
    lines.extend(
        [
            "",
            "## Interpretation note",
            "",
            "This M104 gate is stricter than symbol-level liquidity. A paper option allocation only remains an option if its required live leg structure can be formed from the current IBKR option quote snapshot. Bull call debit spreads require a quoted call plus a higher-strike quoted call in the same expiration; bear put debit spreads require a quoted put plus a lower-strike quoted put in the same expiration. The historical replay option strikes are normalized to the replay reference price, so this gate matches the live structure around current ATM rather than pretending the normalized strikes are directly tradable.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_option_leg_quote_gate_markdown(
    output_path: Path,
    result: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(option_leg_quote_gate_markdown(result, filters=filters), encoding="utf-8")
    return output_path


def strategy_allocation_leg_aware_summary(
    rows: list[dict[str, Any]],
    quote_snapshot: dict[str, Any],
    *,
    max_leg_spread_pct: float = 0.30,
    fallback_to_stock: bool = True,
    underlying_reference_price: float = 100.0,
    stock_quantity: float = 10.0,
    stock_stop_loss_pct: float = 0.02,
    min_confidence: float = 0.35,
    min_stock_expected_return: float = 0.0025,
    min_dte: int = 7,
    base_implied_vol: float = 0.55,
    risk_free_rate: float = 0.04,
    contract_multiplier: float = 100.0,
    moneyness_offset_pct: float = 0.0,
    spread_width_pct: float = 0.05,
    min_option_score: float = 0.35,
    min_option_return_on_risk: float = 0.0,
    option_preference_edge: float = 0.05,
    min_group_rows: int = 1,
    sample_limit: int = 20,
) -> dict[str, Any]:
    theoretical_summary = strategy_allocation_summary(
        rows,
        underlying_reference_price=underlying_reference_price,
        stock_quantity=stock_quantity,
        stock_stop_loss_pct=stock_stop_loss_pct,
        min_confidence=min_confidence,
        min_stock_expected_return=min_stock_expected_return,
        min_dte=min_dte,
        base_implied_vol=base_implied_vol,
        risk_free_rate=risk_free_rate,
        contract_multiplier=contract_multiplier,
        moneyness_offset_pct=moneyness_offset_pct,
        spread_width_pct=spread_width_pct,
        min_option_score=min_option_score,
        min_option_return_on_risk=min_option_return_on_risk,
        option_preference_edge=option_preference_edge,
        min_group_rows=min_group_rows,
        sample_limit=0,
    )
    gate_result = apply_option_leg_quote_gate_to_allocations(
        {"aggregate_test_summary": theoretical_summary},
        quote_snapshot,
        max_leg_spread_pct=max_leg_spread_pct,
        fallback_to_stock=fallback_to_stock,
        min_group_rows=min_group_rows,
        sample_limit=sample_limit,
    )
    gated_summary = gate_result.get("gated_summary") or {}
    parameters = dict(gated_summary.get("parameters") or {})
    parameters["leg_aware_allocator"] = {
        "enabled": True,
        "max_leg_spread_pct": float(max_leg_spread_pct),
        "fallback_to_stock": bool(fallback_to_stock),
        "quote_snapshot_version": quote_snapshot.get("snapshot_version"),
        "quote_generated_at": quote_snapshot.get("generated_at"),
    }
    gated_summary["parameters"] = parameters
    gated_summary["leg_aware_version"] = "paper-stock-option-leg-aware-allocation-v1"
    gated_summary["pre_gate_selected_allocations"] = theoretical_summary.get("selected_allocations")
    gated_summary["option_gate_counts"] = gate_result.get("option_gate_counts") or {}
    gated_summary["matched_strategy_counts"] = gate_result.get("matched_strategy_counts") or {}
    gated_summary["failed_reason_counts"] = gate_result.get("failed_reason_counts") or {}
    gated_summary["quote_snapshot_version"] = gate_result.get("quote_snapshot_version")
    gated_summary["quote_generated_at"] = gate_result.get("quote_generated_at")
    gated_summary["quote_requested_contracts"] = gate_result.get("quote_requested_contracts")
    gated_summary["quote_quoted_contracts"] = gate_result.get("quote_quoted_contracts")
    gated_summary["quote_quoted_rate"] = gate_result.get("quote_quoted_rate")
    gated_summary["quote_avg_spread_pct"] = gate_result.get("quote_avg_spread_pct")
    gated_summary["safety"] = {
        "mode": "read_only_paper_stock_option_leg_aware_allocation",
        "policy": "Theoretical paper allocation is immediately passed through a read-only live-leg quote gate; no account, margin, portfolio, position, or order action.",
    }
    return gated_summary


def tune_strategy_allocation_leg_aware_parameters(
    rows: list[dict[str, Any]],
    quote_snapshot: dict[str, Any],
    *,
    max_leg_spread_pct: float = 0.30,
    fallback_to_stock: bool = True,
    underlying_reference_price: float = 100.0,
    stock_quantity: float = 10.0,
    stock_stop_loss_pct: float = 0.02,
    min_confidence_grid: tuple[float, ...] = (0.30, 0.35, 0.40),
    min_stock_expected_return_grid: tuple[float, ...] = (0.0025, 0.005, 0.01),
    min_option_score_grid: tuple[float, ...] = (0.30, 0.35, 0.40, 0.45),
    min_option_return_on_risk_grid: tuple[float, ...] = (0.0, 0.10, 0.25),
    option_preference_edge_grid: tuple[float, ...] = (0.0, 0.05, 0.10, 0.20),
    min_dte: int = 7,
    base_implied_vol: float = 0.55,
    risk_free_rate: float = 0.04,
    contract_multiplier: float = 100.0,
    moneyness_offset_pct: float = 0.0,
    spread_width_pct: float = 0.05,
    min_selection_rate: float = 0.45,
    pnl_weight: float = 0.35,
    regret_penalty_weight: float = 0.25,
    option_usage_penalty_weight: float = 0.05,
    top_n: int = 20,
) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    for min_confidence in min_confidence_grid:
        for min_stock_expected_return in min_stock_expected_return_grid:
            for min_option_score in min_option_score_grid:
                for min_option_return_on_risk in min_option_return_on_risk_grid:
                    for option_preference_edge in option_preference_edge_grid:
                        summary = strategy_allocation_leg_aware_summary(
                            rows,
                            quote_snapshot,
                            max_leg_spread_pct=max_leg_spread_pct,
                            fallback_to_stock=fallback_to_stock,
                            underlying_reference_price=underlying_reference_price,
                            stock_quantity=stock_quantity,
                            stock_stop_loss_pct=stock_stop_loss_pct,
                            min_confidence=float(min_confidence),
                            min_stock_expected_return=float(min_stock_expected_return),
                            min_dte=min_dte,
                            base_implied_vol=base_implied_vol,
                            risk_free_rate=risk_free_rate,
                            contract_multiplier=contract_multiplier,
                            moneyness_offset_pct=moneyness_offset_pct,
                            spread_width_pct=spread_width_pct,
                            min_option_score=float(min_option_score),
                            min_option_return_on_risk=float(min_option_return_on_risk),
                            option_preference_edge=float(option_preference_edge),
                            min_group_rows=1,
                            sample_limit=0,
                        )
                        score = _allocation_optimizer_score(
                            summary,
                            min_selection_rate=min_selection_rate,
                            pnl_weight=pnl_weight,
                            regret_penalty_weight=regret_penalty_weight,
                            option_usage_penalty_weight=option_usage_penalty_weight,
                        )
                        candidates.append(
                            {
                                "params": {
                                    "min_confidence": float(min_confidence),
                                    "min_stock_expected_return": float(min_stock_expected_return),
                                    "min_option_score": float(min_option_score),
                                    "min_option_return_on_risk": float(min_option_return_on_risk),
                                    "option_preference_edge": float(option_preference_edge),
                                },
                                "score": score,
                                "passes_min_selection_rate": float(summary.get("selection_rate") or 0.0) >= float(min_selection_rate),
                                "selected_allocations": summary.get("selected_allocations"),
                                "pre_gate_selected_allocations": summary.get("pre_gate_selected_allocations"),
                                "selection_rate": summary.get("selection_rate"),
                                "positive_realized_rate": summary.get("positive_realized_rate"),
                                "positive_realized": summary.get("positive_realized"),
                                "evaluated_allocations": summary.get("evaluated_allocations"),
                                "total_realized_pnl": summary.get("total_realized_pnl"),
                                "avg_realized_pnl": summary.get("avg_realized_pnl"),
                                "avg_allocation_regret": summary.get("avg_allocation_regret"),
                                "selected_instrument_counts": summary.get("selected_instrument_counts"),
                                "selected_strategy_counts": summary.get("selected_strategy_counts"),
                                "option_gate_counts": summary.get("option_gate_counts"),
                                "matched_strategy_counts": summary.get("matched_strategy_counts"),
                                "failed_reason_counts": summary.get("failed_reason_counts"),
                            }
                        )
    candidates.sort(
        key=lambda item: (
            not bool(item.get("passes_min_selection_rate")),
            -float(item.get("score") or 0.0),
            -float(item.get("positive_realized_rate") or 0.0),
            -float(item.get("total_realized_pnl") or 0.0),
            float(item.get("avg_allocation_regret") or 0.0),
        )
    )
    selected = candidates[0] if candidates else None
    selected_summary = None
    if selected is not None:
        params = selected.get("params") or {}
        selected_summary = strategy_allocation_leg_aware_summary(
            rows,
            quote_snapshot,
            max_leg_spread_pct=max_leg_spread_pct,
            fallback_to_stock=fallback_to_stock,
            underlying_reference_price=underlying_reference_price,
            stock_quantity=stock_quantity,
            stock_stop_loss_pct=stock_stop_loss_pct,
            min_confidence=float(params.get("min_confidence") or 0.0),
            min_stock_expected_return=float(params.get("min_stock_expected_return") or 0.0),
            min_dte=min_dte,
            base_implied_vol=base_implied_vol,
            risk_free_rate=risk_free_rate,
            contract_multiplier=contract_multiplier,
            moneyness_offset_pct=moneyness_offset_pct,
            spread_width_pct=spread_width_pct,
            min_option_score=float(params.get("min_option_score") or 0.0),
            min_option_return_on_risk=float(params.get("min_option_return_on_risk") or 0.0),
            option_preference_edge=float(params.get("option_preference_edge") or 0.0),
            min_group_rows=1,
            sample_limit=20,
        )
    return {
        "rows": len(rows),
        "leg_aware_tuning_version": "paper-stock-option-leg-aware-parameter-tuning-v1",
        "quote_snapshot_version": quote_snapshot.get("snapshot_version"),
        "quote_generated_at": quote_snapshot.get("generated_at"),
        "grid": {
            "min_confidence_grid": list(min_confidence_grid),
            "min_stock_expected_return_grid": list(min_stock_expected_return_grid),
            "min_option_score_grid": list(min_option_score_grid),
            "min_option_return_on_risk_grid": list(min_option_return_on_risk_grid),
            "option_preference_edge_grid": list(option_preference_edge_grid),
            "min_selection_rate": float(min_selection_rate),
            "pnl_weight": float(pnl_weight),
            "regret_penalty_weight": float(regret_penalty_weight),
            "option_usage_penalty_weight": float(option_usage_penalty_weight),
            "max_leg_spread_pct": float(max_leg_spread_pct),
            "fallback_to_stock": bool(fallback_to_stock),
        },
        "fixed_parameters": {
            "underlying_reference_price": float(underlying_reference_price),
            "stock_quantity": float(stock_quantity),
            "stock_stop_loss_pct": float(stock_stop_loss_pct),
            "min_dte": int(min_dte),
            "base_implied_vol": float(base_implied_vol),
            "risk_free_rate": float(risk_free_rate),
            "contract_multiplier": float(contract_multiplier),
            "moneyness_offset_pct": float(moneyness_offset_pct),
            "spread_width_pct": float(spread_width_pct),
        },
        "candidates_evaluated": len(candidates),
        "selected": selected,
        "selected_summary": selected_summary,
        "top_candidates": candidates[: max(0, int(top_n))],
        "safety": {
            "mode": "read_only_paper_stock_option_leg_aware_parameter_tuning",
            "policy": "Every candidate is evaluated after a read-only option leg quote gate; no account, margin, portfolio, position, or order action.",
        },
    }


def walk_forward_strategy_leg_aware_tuning(
    rows: list[dict[str, Any]],
    quote_snapshot: dict[str, Any],
    *,
    max_leg_spread_pct: float = 0.30,
    fallback_to_stock: bool = True,
    min_train_rows: int = 240,
    test_window_rows: int = 80,
    step_rows: int | None = None,
    rolling_train_rows: int | None = None,
    underlying_reference_price: float = 100.0,
    stock_quantity: float = 10.0,
    stock_stop_loss_pct: float = 0.02,
    min_confidence_grid: tuple[float, ...] = (0.30, 0.35, 0.40),
    min_stock_expected_return_grid: tuple[float, ...] = (0.0025, 0.005, 0.01),
    min_option_score_grid: tuple[float, ...] = (0.30, 0.35, 0.40, 0.45),
    min_option_return_on_risk_grid: tuple[float, ...] = (0.0, 0.10, 0.25),
    option_preference_edge_grid: tuple[float, ...] = (0.0, 0.05, 0.10, 0.20),
    min_dte: int = 7,
    base_implied_vol: float = 0.55,
    risk_free_rate: float = 0.04,
    contract_multiplier: float = 100.0,
    moneyness_offset_pct: float = 0.0,
    spread_width_pct: float = 0.05,
    min_selection_rate: float = 0.45,
    pnl_weight: float = 0.35,
    regret_penalty_weight: float = 0.25,
    option_usage_penalty_weight: float = 0.05,
    top_n: int = 5,
    min_group_rows: int = 1,
    sample_limit: int = 20,
    checkpoint_path: Path | None = None,
) -> dict[str, Any]:
    ordered = _sort_strategy_rows(rows)
    min_train_rows = max(0, int(min_train_rows))
    test_window_rows = max(1, int(test_window_rows))
    step_rows = max(1, int(step_rows if step_rows is not None else test_window_rows))
    rolling_train_rows = int(rolling_train_rows) if rolling_train_rows is not None else None
    if rolling_train_rows is not None:
        rolling_train_rows = max(1, rolling_train_rows)
    fixed_parameters = {
        "underlying_reference_price": float(underlying_reference_price),
        "stock_quantity": float(stock_quantity),
        "stock_stop_loss_pct": float(stock_stop_loss_pct),
        "min_dte": int(min_dte),
        "base_implied_vol": float(base_implied_vol),
        "risk_free_rate": float(risk_free_rate),
        "contract_multiplier": float(contract_multiplier),
        "moneyness_offset_pct": float(moneyness_offset_pct),
        "spread_width_pct": float(spread_width_pct),
    }
    checkpoint_parameters = {
        "rows": len(ordered),
        "quote_snapshot_version": quote_snapshot.get("snapshot_version"),
        "quote_generated_at": quote_snapshot.get("generated_at"),
        "max_leg_spread_pct": float(max_leg_spread_pct),
        "fallback_to_stock": bool(fallback_to_stock),
        "min_train_rows": min_train_rows,
        "test_window_rows": test_window_rows,
        "step_rows": step_rows,
        "rolling_train_rows": rolling_train_rows,
        "min_confidence_grid": list(min_confidence_grid),
        "min_stock_expected_return_grid": list(min_stock_expected_return_grid),
        "min_option_score_grid": list(min_option_score_grid),
        "min_option_return_on_risk_grid": list(min_option_return_on_risk_grid),
        "option_preference_edge_grid": list(option_preference_edge_grid),
        "min_selection_rate": float(min_selection_rate),
        "pnl_weight": float(pnl_weight),
        "regret_penalty_weight": float(regret_penalty_weight),
        "option_usage_penalty_weight": float(option_usage_penalty_weight),
        "top_n": int(top_n),
        **fixed_parameters,
    }
    checkpoint_signature = json.dumps(checkpoint_parameters, sort_keys=True, ensure_ascii=False)
    checkpoint_path = checkpoint_path.expanduser().resolve() if checkpoint_path is not None else None
    checkpoint_folds: dict[int, dict[str, Any]] = {}
    if checkpoint_path is not None and checkpoint_path.exists():
        try:
            checkpoint_payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            checkpoint_payload = {}
        if checkpoint_payload.get("checkpoint_signature") == checkpoint_signature:
            checkpoint_folds = {
                int(fold.get("fold")): fold
                for fold in checkpoint_payload.get("folds") or []
                if fold.get("fold") is not None
            }
    folds: list[dict[str, Any]] = []
    fold_index = 1
    test_start = min_train_rows
    while test_start < len(ordered):
        test_end = min(len(ordered), test_start + test_window_rows)
        train_start = 0 if rolling_train_rows is None else max(0, test_start - rolling_train_rows)
        train_rows = ordered[train_start:test_start]
        test_rows = ordered[test_start:test_end]
        if not train_rows or not test_rows:
            test_start += step_rows
            continue
        if fold_index in checkpoint_folds:
            folds.append(checkpoint_folds[fold_index])
            fold_index += 1
            test_start += step_rows
            continue
        train_tuning = tune_strategy_allocation_leg_aware_parameters(
            train_rows,
            quote_snapshot,
            max_leg_spread_pct=max_leg_spread_pct,
            fallback_to_stock=fallback_to_stock,
            underlying_reference_price=underlying_reference_price,
            stock_quantity=stock_quantity,
            stock_stop_loss_pct=stock_stop_loss_pct,
            min_confidence_grid=min_confidence_grid,
            min_stock_expected_return_grid=min_stock_expected_return_grid,
            min_option_score_grid=min_option_score_grid,
            min_option_return_on_risk_grid=min_option_return_on_risk_grid,
            option_preference_edge_grid=option_preference_edge_grid,
            min_dte=min_dte,
            base_implied_vol=base_implied_vol,
            risk_free_rate=risk_free_rate,
            contract_multiplier=contract_multiplier,
            moneyness_offset_pct=moneyness_offset_pct,
            spread_width_pct=spread_width_pct,
            min_selection_rate=min_selection_rate,
            pnl_weight=pnl_weight,
            regret_penalty_weight=regret_penalty_weight,
            option_usage_penalty_weight=option_usage_penalty_weight,
            top_n=top_n,
        )
        selected = train_tuning.get("selected")
        if selected is None:
            test_start += step_rows
            continue
        params = selected.get("params") or {}
        test_summary = strategy_allocation_leg_aware_summary(
            test_rows,
            quote_snapshot,
            max_leg_spread_pct=max_leg_spread_pct,
            fallback_to_stock=fallback_to_stock,
            underlying_reference_price=underlying_reference_price,
            stock_quantity=stock_quantity,
            stock_stop_loss_pct=stock_stop_loss_pct,
            min_confidence=float(params.get("min_confidence") or 0.0),
            min_stock_expected_return=float(params.get("min_stock_expected_return") or 0.0),
            min_dte=min_dte,
            base_implied_vol=base_implied_vol,
            risk_free_rate=risk_free_rate,
            contract_multiplier=contract_multiplier,
            moneyness_offset_pct=moneyness_offset_pct,
            spread_width_pct=spread_width_pct,
            min_option_score=float(params.get("min_option_score") or 0.0),
            min_option_return_on_risk=float(params.get("min_option_return_on_risk") or 0.0),
            option_preference_edge=float(params.get("option_preference_edge") or 0.0),
            min_group_rows=min_group_rows,
            sample_limit=0,
        )
        for allocation in test_summary.get("allocations") or []:
            allocation["fold"] = fold_index
            allocation["walk_forward_selected_params"] = params
        folds.append(
            {
                "fold": fold_index,
                "train_start": train_rows[0].get("replay_timestamp"),
                "train_end": train_rows[-1].get("replay_timestamp"),
                "test_start": test_rows[0].get("replay_timestamp"),
                "test_end": test_rows[-1].get("replay_timestamp"),
                "train_rows": len(train_rows),
                "test_rows": len(test_rows),
                "selected": selected,
                "test_summary": test_summary,
            }
        )
        if checkpoint_path is not None:
            checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
            checkpoint_path.write_text(
                json.dumps(
                    {
                        "checkpoint_version": "stock-option-leg-aware-walk-forward-checkpoint-v1",
                        "checkpoint_signature": checkpoint_signature,
                        "updated_at": datetime.now(UTC).isoformat(),
                        "parameters": checkpoint_parameters,
                        "folds": folds,
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        fold_index += 1
        test_start += step_rows
    all_test_allocations = [
        allocation
        for fold in folds
        for allocation in ((fold.get("test_summary") or {}).get("allocations") or [])
    ]
    aggregate_test_summary = _allocation_summary_from_allocations(
        all_test_allocations,
        parameters={
            **fixed_parameters,
            "walk_forward": True,
            "leg_aware_allocator": True,
            "max_leg_spread_pct": float(max_leg_spread_pct),
            "fallback_to_stock": bool(fallback_to_stock),
            "quote_snapshot_version": quote_snapshot.get("snapshot_version"),
            "quote_generated_at": quote_snapshot.get("generated_at"),
        },
        min_group_rows=min_group_rows,
        sample_limit=sample_limit,
    )
    option_gate_counts = Counter()
    matched_strategy_counts = Counter()
    failed_reason_counts = Counter()
    for fold in folds:
        summary = fold.get("test_summary") or {}
        option_gate_counts.update(summary.get("option_gate_counts") or {})
        matched_strategy_counts.update(summary.get("matched_strategy_counts") or {})
        failed_reason_counts.update(summary.get("failed_reason_counts") or {})
    aggregate_test_summary["option_gate_counts"] = dict(option_gate_counts)
    aggregate_test_summary["matched_strategy_counts"] = dict(matched_strategy_counts)
    aggregate_test_summary["failed_reason_counts"] = dict(failed_reason_counts)
    aggregate_test_summary["quote_snapshot_version"] = quote_snapshot.get("snapshot_version")
    aggregate_test_summary["quote_generated_at"] = quote_snapshot.get("generated_at")
    return {
        "rows": len(ordered),
        "leg_aware_walk_forward_version": "paper-stock-option-leg-aware-walk-forward-v1",
        "folds": folds,
        "fold_count": len(folds),
        "aggregate_test_summary": aggregate_test_summary,
        "parameters": {
            "max_leg_spread_pct": float(max_leg_spread_pct),
            "fallback_to_stock": bool(fallback_to_stock),
            "min_train_rows": min_train_rows,
            "test_window_rows": test_window_rows,
            "step_rows": step_rows,
            "rolling_train_rows": rolling_train_rows,
            "min_confidence_grid": list(min_confidence_grid),
            "min_stock_expected_return_grid": list(min_stock_expected_return_grid),
            "min_option_score_grid": list(min_option_score_grid),
            "min_option_return_on_risk_grid": list(min_option_return_on_risk_grid),
            "option_preference_edge_grid": list(option_preference_edge_grid),
            "min_selection_rate": float(min_selection_rate),
            "pnl_weight": float(pnl_weight),
            "regret_penalty_weight": float(regret_penalty_weight),
            "option_usage_penalty_weight": float(option_usage_penalty_weight),
            "top_n": int(top_n),
            "checkpoint_path": str(checkpoint_path) if checkpoint_path is not None else None,
            "quote_snapshot_version": quote_snapshot.get("snapshot_version"),
            "quote_generated_at": quote_snapshot.get("generated_at"),
            **fixed_parameters,
        },
        "safety": {
            "mode": "read_only_walk_forward_stock_option_leg_aware_tuning",
            "policy": "Each fold tunes on earlier rows and tests on later rows after a read-only option leg quote gate; no account, margin, portfolio, position, order, or scorer mutation.",
        },
    }


def walk_forward_strategy_leg_aware_tuning_markdown(
    result: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
    title: str = "Walk-Forward Stock/Option Leg-Aware Strategy Tuning Report",
) -> str:
    filters = filters or {}
    aggregate = result.get("aggregate_test_summary") or {}
    counts = aggregate.get("option_gate_counts") or {}
    lines = [
        f"# {title}",
        "",
        f"- Generated at: `{datetime.now(UTC).isoformat()}`",
        f"- Rows: `{result.get('rows')}`",
        f"- Folds: `{result.get('fold_count')}`",
        f"- Quote generated at: `{aggregate.get('quote_generated_at')}`",
        f"- Aggregate future-test selected allocations: `{aggregate.get('selected_allocations')}`",
        f"- Aggregate future-test selection rate: `{_fmt_pct(aggregate.get('selection_rate'))}`",
        f"- Aggregate positive realized PnL: `{aggregate.get('positive_realized')}/{aggregate.get('evaluated_allocations')} = {_fmt_pct(aggregate.get('positive_realized_rate'))}`",
        f"- Aggregate total realized PnL: `{_fmt_money(aggregate.get('total_realized_pnl'))}`",
        f"- Aggregate avg realized PnL: `{_fmt_money(aggregate.get('avg_realized_pnl'))}`",
        f"- Aggregate avg allocation regret: `{_fmt_money(aggregate.get('avg_allocation_regret'))}`",
        f"- Option allocations seen by gate: `{counts.get('option_allocations_seen')}`",
        f"- Option allocations passed live-leg gate: `{counts.get('option_allocations_passed')}`",
        f"- Option allocations downgraded to stock: `{counts.get('option_allocations_downgraded_to_stock')}`",
        f"- Option allocations blocked: `{counts.get('option_allocations_blocked')}`",
        "- Safety: read-only leg-aware paper walk-forward; no account, margin, portfolio, position, or order submission.",
        "",
        "## Filters",
        "",
    ]
    if filters:
        for key, value in filters.items():
            lines.append(f"- `{key}`: `{value if value is not None else '-'}`")
    else:
        lines.append("- none")
    lines.extend(["", "## Parameters", ""])
    for key, value in (result.get("parameters") or {}).items():
        lines.append(f"- `{key}`: `{value}`")
    _append_allocation_group_table(lines, "Aggregate future-test by selected instrument", aggregate.get("by_instrument") or [])
    _append_allocation_group_table(lines, "Aggregate future-test by selected strategy", aggregate.get("by_strategy") or [])
    lines.extend(
        [
            "",
            "## Matched live-leg strategies",
            "",
            "| Strategy | Matches |",
            "| --- | ---: |",
        ]
    )
    matched = aggregate.get("matched_strategy_counts") or {}
    if matched:
        for strategy, count in sorted(matched.items()):
            lines.append(f"| {strategy} | {count} |")
    else:
        lines.append("| - | 0 |")
    lines.extend(
        [
            "",
            "## Live-leg gate failed reasons",
            "",
            "| Reason | Count |",
            "| --- | ---: |",
        ]
    )
    failed = aggregate.get("failed_reason_counts") or {}
    if failed:
        for reason, count in sorted(failed.items(), key=lambda item: (-int(item[1]), item[0])):
            lines.append(f"| {reason} | {count} |")
    else:
        lines.append("| - | 0 |")
    lines.extend(
        [
            "",
            "## Fold summaries",
            "",
            "| Fold | Train rows | Test rows | Selected params | Selected | Positive rate | Total PnL | Option gate pass/down/block |",
            "| ---: | ---: | ---: | --- | ---: | ---: | ---: | --- |",
        ]
    )
    for fold in result.get("folds") or []:
        selected = fold.get("selected") or {}
        summary = fold.get("test_summary") or {}
        fold_counts = summary.get("option_gate_counts") or {}
        lines.append(
            f"| {fold.get('fold')} | {fold.get('train_rows')} | {fold.get('test_rows')} "
            f"| `{selected.get('params')}` "
            f"| {summary.get('selected_allocations')} "
            f"| {_fmt_pct(summary.get('positive_realized_rate'))} "
            f"| {_fmt_money(summary.get('total_realized_pnl'))} "
            f"| {fold_counts.get('option_allocations_passed', 0)}/{fold_counts.get('option_allocations_downgraded_to_stock', 0)}/{fold_counts.get('option_allocations_blocked', 0)} |"
        )
    lines.extend(
        [
            "",
            "## Interpretation note",
            "",
            "M105 tunes the stock/option selector after applying the M104 live-leg quote gate inside every candidate evaluation. That means option strategies compete only after their live-leg tradability penalty or fallback has already been reflected in the objective.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_walk_forward_strategy_leg_aware_tuning_markdown(
    output_path: Path,
    result: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(walk_forward_strategy_leg_aware_tuning_markdown(result, filters=filters), encoding="utf-8")
    return output_path


def _allocation_payload_summary(allocation_payload: dict[str, Any]) -> dict[str, Any]:
    if "result" in allocation_payload:
        result = allocation_payload.get("result") or {}
    elif "summary" in allocation_payload:
        result = allocation_payload.get("summary") or {}
    else:
        result = allocation_payload
    return result.get("aggregate_test_summary") or source_payload_summary(result)


def _execution_check(name: str, passed: bool, *, value: Any = None, limit: Any = None, severity: str = "hard") -> dict[str, Any]:
    return {
        "name": name,
        "passed": bool(passed),
        "value": value,
        "limit": limit,
        "severity": severity,
    }


def _execution_readiness_status(checks: list[dict[str, Any]], warnings: list[str], *, human_review_required: bool) -> str:
    hard_failed = any(not check.get("passed") and check.get("severity") == "hard" for check in checks)
    if hard_failed:
        return "not_ready"
    if human_review_required or warnings:
        return "needs_human_review"
    return "ready"


def _option_ticket_from_live_match(
    allocation: dict[str, Any],
    *,
    max_option_max_loss: float,
    max_net_debit: float,
    min_reward_to_risk: float,
    slippage_buffer_pct: float,
    min_dte: int,
) -> dict[str, Any]:
    selected = allocation.get("selected_expression") or {}
    match = selected.get("live_leg_quote_match") or allocation.get("leg_quote_gate") or {}
    checks: list[dict[str, Any]] = []
    warnings: list[str] = []
    legs = match.get("legs") or []
    checks.append(_execution_check("live_leg_quote_match_present", bool(match and match.get("status") == "passed" and legs)))
    net_debit = _float_or_none(match.get("estimated_live_net_debit"))
    live_max_loss = _float_or_none(match.get("estimated_live_max_loss"))
    live_max_profit = _float_or_none(match.get("estimated_live_max_profit"))
    reward_to_risk = live_max_profit / live_max_loss if live_max_profit is not None and live_max_loss and live_max_loss > 0 else None
    checks.extend(
        [
            _execution_check("positive_net_debit", net_debit is not None and net_debit > 0, value=net_debit),
            _execution_check("max_loss_within_policy", live_max_loss is not None and live_max_loss <= float(max_option_max_loss), value=live_max_loss, limit=max_option_max_loss),
            _execution_check("net_debit_within_policy", net_debit is not None and net_debit * float(match.get("multiplier") or 100.0) <= float(max_net_debit), value=(net_debit * float(match.get("multiplier") or 100.0)) if net_debit is not None else None, limit=max_net_debit),
            _execution_check("reward_to_risk_within_policy", reward_to_risk is not None and reward_to_risk >= float(min_reward_to_risk), value=reward_to_risk, limit=min_reward_to_risk),
        ]
    )
    dtes = [int(leg.get("dte") or 0) for leg in legs if leg.get("dte") is not None]
    min_leg_dte = min(dtes) if dtes else None
    checks.append(_execution_check("minimum_dte", min_leg_dte is not None and min_leg_dte >= int(min_dte), value=min_leg_dte, limit=min_dte))
    if any(leg.get("role") == "short" for leg in legs):
        warnings.append("short_option_leg_assignment_and_exercise_risk_requires_human_review")
    if any(leg.get("delayed") for leg in legs):
        warnings.append("delayed_option_quote_requires_human_review")
    if any(_float_or_none(leg.get("open_interest")) == 0 for leg in legs):
        warnings.append("zero_open_interest_seen_in_snapshot_confirm_liquidity_before_execution")
    limit_debit = round(float(net_debit or 0.0) * (1.0 + float(slippage_buffer_pct)), 2)
    ticket_legs = []
    for leg in legs:
        role = str(leg.get("role") or "")
        ticket_legs.append(
            {
                "action": "BUY" if role == "long" else "SELL",
                "ratio": 1,
                "symbol": leg.get("symbol"),
                "expiration": leg.get("expiration"),
                "right": leg.get("right"),
                "strike": leg.get("strike"),
                "exchange": "SMART",
                "currency": "USD",
                "multiplier": leg.get("multiplier") or match.get("multiplier") or "100",
                "bid": leg.get("bid"),
                "ask": leg.get("ask"),
                "mid": leg.get("mid"),
                "spread_pct": leg.get("spread_pct"),
            }
        )
    status = _execution_readiness_status(checks, warnings, human_review_required=True)
    return {
        "readiness_status": status,
        "human_review_required": True,
        "review_reasons": warnings,
        "checks": checks,
        "ticket_draft": {
            "ticket_type": "option_combo_debit_spread",
            "paper_only": True,
            "symbol": allocation.get("symbol"),
            "strategy": selected.get("strategy"),
            "order_action": "BUY_COMBO",
            "order_type": "NET_DEBIT_LIMIT",
            "quantity": selected.get("quantity") or 1,
            "estimated_net_debit": net_debit,
            "limit_debit_with_slippage_buffer": limit_debit,
            "slippage_buffer_pct": float(slippage_buffer_pct),
            "estimated_max_loss": live_max_loss,
            "estimated_max_profit": live_max_profit,
            "estimated_reward_to_risk": reward_to_risk,
            "legs": ticket_legs,
        },
    }


def _stock_ticket_from_allocation(
    allocation: dict[str, Any],
    *,
    max_stock_notional: float,
    max_stock_stop_loss: float,
) -> dict[str, Any]:
    selected = allocation.get("selected_expression") or {}
    strategy = str(selected.get("strategy") or "")
    notional = _float_or_none(selected.get("notional"))
    max_loss = _float_or_none(selected.get("max_loss"))
    is_short = "short" in strategy
    checks = [
        _execution_check("stock_notional_within_policy", notional is not None and notional <= float(max_stock_notional), value=notional, limit=max_stock_notional),
        _execution_check("stock_stop_loss_within_policy", max_loss is not None and max_loss <= float(max_stock_stop_loss), value=max_loss, limit=max_stock_stop_loss),
    ]
    warnings = []
    if is_short:
        warnings.append("short_stock_requires_borrow_margin_and_account_review")
    status = _execution_readiness_status(checks, warnings, human_review_required=is_short)
    return {
        "readiness_status": status,
        "human_review_required": bool(is_short),
        "review_reasons": warnings,
        "checks": checks,
        "ticket_draft": {
            "ticket_type": "stock",
            "paper_only": True,
            "symbol": allocation.get("symbol"),
            "strategy": strategy,
            "order_action": "SELL_SHORT" if is_short else "BUY",
            "order_type": "MARKET_OR_LIMIT_REQUIRED_BY_EXECUTION_LAYER",
            "quantity": selected.get("quantity"),
            "estimated_notional": notional,
            "stop_loss_proxy": max_loss,
        },
    }


def build_execution_readiness_report(
    allocation_payload: dict[str, Any],
    *,
    max_option_max_loss: float = 500.0,
    max_option_net_debit: float = 500.0,
    min_option_reward_to_risk: float = 0.20,
    option_slippage_buffer_pct: float = 0.05,
    min_option_dte: int = 7,
    max_stock_notional: float = 5000.0,
    max_stock_stop_loss: float = 250.0,
    min_group_rows: int = 1,
    sample_limit: int = 20,
) -> dict[str, Any]:
    source_summary = _allocation_payload_summary(allocation_payload)
    source_allocations = source_summary.get("allocations") or []
    readiness_items: list[dict[str, Any]] = []
    status_counts: Counter[str] = Counter()
    instrument_counts: Counter[str] = Counter()
    ticket_type_counts: Counter[str] = Counter()
    review_reason_counts: Counter[str] = Counter()
    check_failure_counts: Counter[str] = Counter()
    for raw in source_allocations:
        allocation = json.loads(json.dumps(raw))
        selected = allocation.get("selected_expression") or {}
        instrument = str(selected.get("instrument") or "blocked")
        if not selected:
            readiness = {
                "readiness_status": "not_candidate",
                "human_review_required": False,
                "review_reasons": [allocation.get("block_reason") or "no_selected_expression"],
                "checks": [],
                "ticket_draft": None,
            }
        elif instrument == "option":
            readiness = _option_ticket_from_live_match(
                allocation,
                max_option_max_loss=max_option_max_loss,
                max_net_debit=max_option_net_debit,
                min_reward_to_risk=min_option_reward_to_risk,
                slippage_buffer_pct=option_slippage_buffer_pct,
                min_dte=min_option_dte,
            )
        elif instrument == "stock":
            readiness = _stock_ticket_from_allocation(
                allocation,
                max_stock_notional=max_stock_notional,
                max_stock_stop_loss=max_stock_stop_loss,
            )
        else:
            readiness = {
                "readiness_status": "not_ready",
                "human_review_required": True,
                "review_reasons": [f"unsupported_instrument:{instrument}"],
                "checks": [],
                "ticket_draft": None,
            }
        status = str(readiness.get("readiness_status") or "unknown")
        status_counts[status] += 1
        instrument_counts[instrument] += 1
        ticket = readiness.get("ticket_draft") or {}
        if ticket:
            ticket_type_counts[str(ticket.get("ticket_type") or "unknown")] += 1
        for reason in readiness.get("review_reasons") or []:
            review_reason_counts[str(reason)] += 1
        for check in readiness.get("checks") or []:
            if not check.get("passed"):
                check_failure_counts[str(check.get("name") or "unknown_check")] += 1
        allocation["execution_readiness"] = readiness
        readiness_items.append(allocation)
    ticketed = [item for item in readiness_items if (item.get("execution_readiness") or {}).get("ticket_draft")]
    actionable = [
        item
        for item in ticketed
        if (item.get("execution_readiness") or {}).get("readiness_status") in {"ready", "needs_human_review"}
    ]
    summary = _allocation_summary_from_allocations(
        readiness_items,
        parameters={
            "execution_readiness": {
                "max_option_max_loss": float(max_option_max_loss),
                "max_option_net_debit": float(max_option_net_debit),
                "min_option_reward_to_risk": float(min_option_reward_to_risk),
                "option_slippage_buffer_pct": float(option_slippage_buffer_pct),
                "min_option_dte": int(min_option_dte),
                "max_stock_notional": float(max_stock_notional),
                "max_stock_stop_loss": float(max_stock_stop_loss),
            }
        },
        min_group_rows=min_group_rows,
        sample_limit=sample_limit,
    )
    return {
        "execution_readiness_version": "paper-execution-readiness-v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "source_rows": source_summary.get("rows"),
        "source_selected_allocations": source_summary.get("selected_allocations"),
        "readiness_counts": dict(status_counts),
        "instrument_counts": dict(instrument_counts),
        "ticket_type_counts": dict(ticket_type_counts),
        "review_reason_counts": dict(review_reason_counts),
        "check_failure_counts": dict(check_failure_counts),
        "ticket_drafts": len(ticketed),
        "actionable_ticket_drafts": len(actionable),
        "summary": summary,
        "readiness_items": readiness_items,
        "sample_readiness_items": readiness_items[: max(0, int(sample_limit))],
        "safety": {
            "mode": "read_only_paper_execution_readiness",
            "policy": "Creates paper ticket drafts from stored allocations only; no account, margin, portfolio, position, open-order, or IBKR order action.",
        },
    }


def execution_readiness_markdown(
    result: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
    title: str = "Paper Execution Readiness Report",
) -> str:
    filters = filters or {}
    summary = result.get("summary") or {}
    lines = [
        f"# {title}",
        "",
        f"- Generated at: `{result.get('generated_at')}`",
        f"- Source rows: `{result.get('source_rows')}`",
        f"- Source selected allocations: `{result.get('source_selected_allocations')}`",
        f"- Ticket drafts: `{result.get('ticket_drafts')}`",
        f"- Actionable ticket drafts after readiness checks: `{result.get('actionable_ticket_drafts')}`",
        f"- Positive realized PnL among source selected: `{summary.get('positive_realized')}/{summary.get('evaluated_allocations')} = {_fmt_pct(summary.get('positive_realized_rate'))}`",
        f"- Total realized PnL among source selected: `{_fmt_money(summary.get('total_realized_pnl'))}`",
        "- Safety: paper ticket drafts only; no account, margin, portfolio, position, open-order, or order submission.",
        "",
        "## Inputs",
        "",
    ]
    if filters:
        for key, value in filters.items():
            lines.append(f"- `{key}`: `{value if value is not None else '-'}`")
    else:
        lines.append("- none")
    for title_text, counts in (
        ("Readiness counts", result.get("readiness_counts") or {}),
        ("Instrument counts", result.get("instrument_counts") or {}),
        ("Ticket type counts", result.get("ticket_type_counts") or {}),
        ("Review reason counts", result.get("review_reason_counts") or {}),
        ("Check failure counts", result.get("check_failure_counts") or {}),
    ):
        lines.extend(["", f"## {title_text}", "", "| Bucket | Count |", "| --- | ---: |"])
        if counts:
            for key, value in sorted(counts.items(), key=lambda item: (-int(item[1]), item[0])):
                lines.append(f"| {key} | {value} |")
        else:
            lines.append("| - | 0 |")
    lines.extend(
        [
            "",
            "## Sample ticket drafts",
            "",
            "| Symbol | Instrument | Strategy | Readiness | Human review | Ticket type | Action | Key risk |",
            "| --- | --- | --- | --- | --- | --- | --- | ---: |",
        ]
    )
    sample_count = 0
    for item in result.get("readiness_items") or []:
        readiness = item.get("execution_readiness") or {}
        ticket = readiness.get("ticket_draft") or {}
        if not ticket:
            continue
        selected = item.get("selected_expression") or {}
        risk = ticket.get("estimated_max_loss") if ticket.get("estimated_max_loss") is not None else ticket.get("stop_loss_proxy")
        lines.append(
            f"| {item.get('symbol')} | {selected.get('instrument')} | {selected.get('strategy')} "
            f"| {readiness.get('readiness_status')} | {readiness.get('human_review_required')} "
            f"| {ticket.get('ticket_type')} | {ticket.get('order_action')} | {_fmt_money(risk)} |"
        )
        sample_count += 1
        if sample_count >= 20:
            break
    if sample_count == 0:
        lines.append("| - | - | - | - | - | - | - | - |")
    lines.extend(
        [
            "",
            "## Interpretation note",
            "",
            "M108 is a pre-execution readiness layer, not an execution layer. It drafts paper tickets and flags mechanical failures or human-review requirements. Live execution still requires account, margin, position, order-routing, exercise/assignment, and risk approval layers.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_execution_readiness_markdown(
    output_path: Path,
    result: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(execution_readiness_markdown(result, filters=filters), encoding="utf-8")
    return output_path


def _readiness_payload_items(readiness_payload: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if "result" in readiness_payload:
        result = readiness_payload.get("result") or {}
    else:
        result = readiness_payload
    return result, list(result.get("readiness_items") or [])


def _ticket_trade_risk(ticket: dict[str, Any]) -> float:
    for key in ("estimated_max_loss", "stop_loss_proxy", "estimated_notional"):
        value = _float_or_none(ticket.get(key))
        if value is not None:
            return max(0.0, value)
    return 0.0


def _ticket_gross_notional(ticket: dict[str, Any]) -> float:
    ticket_type = str(ticket.get("ticket_type") or "")
    if ticket_type == "stock":
        return max(0.0, _float_or_none(ticket.get("estimated_notional")) or 0.0)
    if ticket_type == "option_combo_debit_spread":
        return max(0.0, _float_or_none(ticket.get("estimated_max_loss")) or 0.0)
    return max(0.0, _float_or_none(ticket.get("estimated_notional")) or _float_or_none(ticket.get("estimated_max_loss")) or 0.0)


def build_mock_portfolio_readiness_report(
    readiness_payload: dict[str, Any],
    *,
    mock_account_equity: float = 100000.0,
    mock_cash: float = 100000.0,
    max_single_trade_risk_pct: float = 0.005,
    max_total_open_risk_pct: float = 0.05,
    max_gross_notional_pct: float = 1.0,
    max_option_risk_pct: float = 0.02,
    max_short_notional_pct: float = 0.20,
    max_symbol_risk_pct: float = 0.01,
    max_approved_tickets: int = 25,
    sample_limit: int = 20,
) -> dict[str, Any]:
    source_result, readiness_items = _readiness_payload_items(readiness_payload)
    equity = max(1.0, float(mock_account_equity))
    cash = max(0.0, float(mock_cash))
    limits = {
        "single_trade_risk": equity * float(max_single_trade_risk_pct),
        "total_open_risk": equity * float(max_total_open_risk_pct),
        "gross_notional": equity * float(max_gross_notional_pct),
        "option_risk": equity * float(max_option_risk_pct),
        "short_notional": equity * float(max_short_notional_pct),
        "symbol_risk": equity * float(max_symbol_risk_pct),
        "cash": cash,
        "approved_tickets": int(max_approved_tickets),
    }
    used = {
        "total_open_risk": 0.0,
        "gross_notional": 0.0,
        "option_risk": 0.0,
        "short_notional": 0.0,
        "cash": 0.0,
        "approved_tickets": 0,
    }
    symbol_risk: Counter[str] = Counter()
    status_counts: Counter[str] = Counter()
    failure_counts: Counter[str] = Counter()
    review_counts: Counter[str] = Counter()
    approved_items: list[dict[str, Any]] = []
    portfolio_items: list[dict[str, Any]] = []
    for raw in readiness_items:
        item = json.loads(json.dumps(raw))
        readiness = item.get("execution_readiness") or {}
        ticket = readiness.get("ticket_draft") or None
        checks: list[dict[str, Any]] = []
        review_reasons: list[str] = []
        prior_status = str(readiness.get("readiness_status") or "unknown")
        if prior_status not in {"ready", "needs_human_review"} or not ticket:
            status = "not_candidate" if prior_status == "not_candidate" else "not_ready"
            checks.append(_execution_check("prior_execution_readiness_actionable", False, value=prior_status, limit="ready_or_needs_human_review"))
        else:
            selected = item.get("selected_expression") or {}
            symbol = str(item.get("symbol") or ticket.get("symbol") or "UNKNOWN").upper()
            ticket_type = str(ticket.get("ticket_type") or "unknown")
            order_action = str(ticket.get("order_action") or "")
            trade_risk = _ticket_trade_risk(ticket)
            gross_notional = _ticket_gross_notional(ticket)
            option_risk_add = trade_risk if ticket_type == "option_combo_debit_spread" else 0.0
            short_notional_add = gross_notional if order_action == "SELL_SHORT" else 0.0
            cash_add = gross_notional if order_action in {"BUY", "BUY_COMBO"} else 0.0
            checks.extend(
                [
                    _execution_check("single_trade_risk_within_mock_policy", trade_risk <= limits["single_trade_risk"], value=trade_risk, limit=limits["single_trade_risk"]),
                    _execution_check("total_open_risk_within_mock_policy", used["total_open_risk"] + trade_risk <= limits["total_open_risk"], value=used["total_open_risk"] + trade_risk, limit=limits["total_open_risk"]),
                    _execution_check("gross_notional_within_mock_policy", used["gross_notional"] + gross_notional <= limits["gross_notional"], value=used["gross_notional"] + gross_notional, limit=limits["gross_notional"]),
                    _execution_check("option_risk_within_mock_policy", used["option_risk"] + option_risk_add <= limits["option_risk"], value=used["option_risk"] + option_risk_add, limit=limits["option_risk"]),
                    _execution_check("short_notional_within_mock_policy", used["short_notional"] + short_notional_add <= limits["short_notional"], value=used["short_notional"] + short_notional_add, limit=limits["short_notional"]),
                    _execution_check("symbol_risk_within_mock_policy", float(symbol_risk[symbol]) + trade_risk <= limits["symbol_risk"], value=float(symbol_risk[symbol]) + trade_risk, limit=limits["symbol_risk"]),
                    _execution_check("cash_usage_within_mock_policy", used["cash"] + cash_add <= limits["cash"], value=used["cash"] + cash_add, limit=limits["cash"]),
                    _execution_check("approved_ticket_count_within_mock_policy", used["approved_tickets"] + 1 <= limits["approved_tickets"], value=used["approved_tickets"] + 1, limit=limits["approved_tickets"]),
                ]
            )
            failed = [check for check in checks if not check.get("passed")]
            if failed:
                status = "portfolio_not_ready"
            else:
                used["total_open_risk"] += trade_risk
                used["gross_notional"] += gross_notional
                used["option_risk"] += option_risk_add
                used["short_notional"] += short_notional_add
                used["cash"] += cash_add
                used["approved_tickets"] += 1
                symbol_risk[symbol] += trade_risk
                if prior_status == "needs_human_review" or readiness.get("human_review_required"):
                    status = "portfolio_needs_human_review"
                    review_reasons.extend(readiness.get("review_reasons") or [])
                else:
                    status = "portfolio_ready"
                approved_items.append(item)
        for check in checks:
            if not check.get("passed"):
                failure_counts[str(check.get("name") or "unknown_check")] += 1
        for reason in review_reasons:
            review_counts[str(reason)] += 1
        item["mock_portfolio_readiness"] = {
            "status": status,
            "checks": checks,
            "review_reasons": review_reasons,
            "mock_account_equity": equity,
            "mock_cash": cash,
        }
        status_counts[status] += 1
        portfolio_items.append(item)
    return {
        "mock_portfolio_readiness_version": "paper-mock-portfolio-readiness-v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "source_execution_readiness_version": source_result.get("execution_readiness_version"),
        "source_rows": source_result.get("source_rows"),
        "source_selected_allocations": source_result.get("source_selected_allocations"),
        "source_ticket_drafts": source_result.get("ticket_drafts"),
        "source_actionable_ticket_drafts": source_result.get("actionable_ticket_drafts"),
        "mock_account": {
            "equity": equity,
            "cash": cash,
        },
        "limits": limits,
        "used_budget": {**used, "symbol_risk": dict(symbol_risk)},
        "portfolio_status_counts": dict(status_counts),
        "portfolio_check_failure_counts": dict(failure_counts),
        "portfolio_review_reason_counts": dict(review_counts),
        "approved_ticket_count": len(approved_items),
        "portfolio_items": portfolio_items,
        "sample_portfolio_items": portfolio_items[: max(0, int(sample_limit))],
        "safety": {
            "mode": "read_only_mock_portfolio_readiness",
            "policy": "Applies mock account and portfolio budgets to stored paper ticket drafts only; no account, margin, portfolio, position, open-order, or IBKR order action.",
        },
    }


def mock_portfolio_readiness_markdown(
    result: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
    title: str = "Paper Mock Portfolio Readiness Report",
) -> str:
    filters = filters or {}
    lines = [
        f"# {title}",
        "",
        f"- Generated at: `{result.get('generated_at')}`",
        f"- Source selected allocations: `{result.get('source_selected_allocations')}`",
        f"- Source ticket drafts: `{result.get('source_ticket_drafts')}`",
        f"- Source actionable ticket drafts: `{result.get('source_actionable_ticket_drafts')}`",
        f"- Mock equity: `{_fmt_money((result.get('mock_account') or {}).get('equity'))}`",
        f"- Mock cash: `{_fmt_money((result.get('mock_account') or {}).get('cash'))}`",
        f"- Approved ticket count under mock budgets: `{result.get('approved_ticket_count')}`",
        "- Safety: mock portfolio budget checks only; no account, margin, portfolio, position, open-order, or order submission.",
        "",
        "## Inputs",
        "",
    ]
    if filters:
        for key, value in filters.items():
            lines.append(f"- `{key}`: `{value if value is not None else '-'}`")
    else:
        lines.append("- none")
    lines.extend(["", "## Mock limits", "", "| Limit | Value |", "| --- | ---: |"])
    for key, value in (result.get("limits") or {}).items():
        lines.append(f"| {key} | {_fmt_money(value) if isinstance(value, (int, float)) else value} |")
    for title_text, counts in (
        ("Portfolio status counts", result.get("portfolio_status_counts") or {}),
        ("Portfolio check failure counts", result.get("portfolio_check_failure_counts") or {}),
        ("Portfolio review reason counts", result.get("portfolio_review_reason_counts") or {}),
    ):
        lines.extend(["", f"## {title_text}", "", "| Bucket | Count |", "| --- | ---: |"])
        if counts:
            for key, value in sorted(counts.items(), key=lambda item: (-int(item[1]), item[0])):
                lines.append(f"| {key} | {value} |")
        else:
            lines.append("| - | 0 |")
    lines.extend(
        [
            "",
            "## Used mock budget",
            "",
            "| Budget | Used |",
            "| --- | ---: |",
        ]
    )
    used = result.get("used_budget") or {}
    for key in ("total_open_risk", "gross_notional", "option_risk", "short_notional", "cash", "approved_tickets"):
        value = used.get(key)
        lines.append(f"| {key} | {_fmt_money(value) if isinstance(value, (int, float)) else value} |")
    lines.extend(
        [
            "",
            "## Sample portfolio readiness",
            "",
            "| Symbol | Instrument | Strategy | Prior readiness | Portfolio status | Ticket risk |",
            "| --- | --- | --- | --- | --- | ---: |",
        ]
    )
    for item in result.get("sample_portfolio_items") or []:
        selected = item.get("selected_expression") or {}
        prior = item.get("execution_readiness") or {}
        portfolio = item.get("mock_portfolio_readiness") or {}
        ticket = prior.get("ticket_draft") or {}
        lines.append(
            f"| {item.get('symbol')} | {selected.get('instrument') or 'blocked'} | {selected.get('strategy') or '-'} "
            f"| {prior.get('readiness_status')} | {portfolio.get('status')} | {_fmt_money(_ticket_trade_risk(ticket))} |"
        )
    lines.extend(
        [
            "",
            "## Interpretation note",
            "",
            "M109 is a mock portfolio-budget layer, not a real account adapter. It intentionally uses configurable mock equity/cash and sequential batch budgeting over stored paper ticket drafts. The next step can replace mock inputs with read-only IBKR account and portfolio data while keeping this gate separate from order submission.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_mock_portfolio_readiness_markdown(
    output_path: Path,
    result: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(mock_portfolio_readiness_markdown(result, filters=filters), encoding="utf-8")
    return output_path


def build_account_backed_portfolio_readiness_report(
    readiness_payload: dict[str, Any],
    account_snapshot: dict[str, Any],
    *,
    equity_haircut_pct: float = 0.0,
    cash_haircut_pct: float = 0.0,
    max_single_trade_risk_pct: float = 0.005,
    max_total_open_risk_pct: float = 0.05,
    max_gross_notional_pct: float = 1.0,
    max_option_risk_pct: float = 0.02,
    max_short_notional_pct: float = 0.20,
    max_symbol_risk_pct: float = 0.01,
    max_approved_tickets: int = 25,
    sample_limit: int = 20,
) -> dict[str, Any]:
    equity, cash, effective_equity, effective_cash = _effective_account_budget(
        account_snapshot,
        equity_haircut_pct=equity_haircut_pct,
        cash_haircut_pct=cash_haircut_pct,
    )
    result = build_mock_portfolio_readiness_report(
        readiness_payload,
        mock_account_equity=effective_equity,
        mock_cash=effective_cash,
        max_single_trade_risk_pct=max_single_trade_risk_pct,
        max_total_open_risk_pct=max_total_open_risk_pct,
        max_gross_notional_pct=max_gross_notional_pct,
        max_option_risk_pct=max_option_risk_pct,
        max_short_notional_pct=max_short_notional_pct,
        max_symbol_risk_pct=max_symbol_risk_pct,
        max_approved_tickets=max_approved_tickets,
        sample_limit=sample_limit,
    )
    result["account_backed_readiness_version"] = "paper-account-backed-portfolio-readiness-v1"
    result["account_snapshot"] = {
        "snapshot_version": account_snapshot.get("snapshot_version"),
        "generated_at": account_snapshot.get("generated_at"),
        "selected_account": account_snapshot.get("selected_account"),
        "raw_equity": equity,
        "raw_cash": cash,
        "buying_power": account_snapshot.get("buying_power"),
        "position_count": account_snapshot.get("position_count"),
        "open_order_count": account_snapshot.get("open_order_count"),
        "errors_count": len(account_snapshot.get("errors") or []),
        "equity_haircut_pct": float(equity_haircut_pct),
        "cash_haircut_pct": float(cash_haircut_pct),
        "effective_equity": effective_equity,
        "effective_cash": effective_cash,
    }
    result["safety"] = {
        "mode": "read_only_account_backed_portfolio_readiness",
        "policy": "Uses a read-only IBKR account snapshot as budget input for paper ticket drafts; no order, margin change, portfolio mutation, or position change.",
    }
    return result


def _effective_account_budget(
    account_snapshot: dict[str, Any],
    *,
    equity_haircut_pct: float = 0.0,
    cash_haircut_pct: float = 0.0,
) -> tuple[float, float, float, float]:
    equity = _float_or_none(account_snapshot.get("equity"))
    cash = _float_or_none(account_snapshot.get("cash"))
    if equity is None or equity <= 0:
        equity = 1.0
    if cash is None or cash < 0:
        cash = 0.0
    effective_equity = max(1.0, equity * (1.0 - float(equity_haircut_pct)))
    effective_cash = max(0.0, cash * (1.0 - float(cash_haircut_pct)))
    return equity, cash, effective_equity, effective_cash


def _account_budget_limits(
    *,
    equity: float,
    cash: float,
    max_single_trade_risk_pct: float,
    max_total_open_risk_pct: float,
    max_gross_notional_pct: float,
    max_option_risk_pct: float,
    max_short_notional_pct: float,
    max_symbol_risk_pct: float,
) -> dict[str, float]:
    return {
        "single_trade_risk": max(0.0, float(equity) * float(max_single_trade_risk_pct)),
        "total_open_risk": max(0.0, float(equity) * float(max_total_open_risk_pct)),
        "gross_notional": max(0.0, float(equity) * float(max_gross_notional_pct)),
        "option_risk": max(0.0, float(equity) * float(max_option_risk_pct)),
        "short_notional": max(0.0, float(equity) * float(max_short_notional_pct)),
        "symbol_risk": max(0.0, float(equity) * float(max_symbol_risk_pct)),
        "cash": max(0.0, float(cash)),
    }


def _rounded_stock_quantity(
    quantity: float,
    *,
    allow_fractional_shares: bool,
    stock_quantity_increment: float,
) -> float:
    if allow_fractional_shares:
        increment = max(0.000001, float(stock_quantity_increment))
        return math.floor(max(0.0, float(quantity)) / increment) * increment
    return float(math.floor(max(0.0, float(quantity))))


def _size_readiness_item_for_account(
    raw_item: dict[str, Any],
    *,
    limits: dict[str, float],
    max_stock_notional_pct: float,
    max_cash_per_trade_pct: float,
    sizing_safety_buffer_pct: float,
    allow_fractional_shares: bool,
    min_stock_quantity: float,
    stock_quantity_increment: float,
) -> dict[str, Any]:
    item = json.loads(json.dumps(raw_item))
    readiness = item.get("execution_readiness") or {}
    ticket = readiness.get("ticket_draft") or None
    prior_status = str(readiness.get("readiness_status") or "unknown")
    sizing = {
        "status": "not_sized",
        "method": "account_scaled_position_sizing_v1",
        "reasons": [],
        "original_trade_risk": _ticket_trade_risk(ticket or {}),
        "original_gross_notional": _ticket_gross_notional(ticket or {}),
        "sized_trade_risk": None,
        "sized_gross_notional": None,
        "scale_factor": None,
    }
    if prior_status not in {"ready", "needs_human_review"} or not ticket:
        sizing["status"] = "not_actionable_source"
        sizing["reasons"] = ["source_execution_readiness_not_actionable"]
        item["account_position_sizing"] = sizing
        return item

    sized_readiness = json.loads(json.dumps(readiness))
    sized_ticket = json.loads(json.dumps(ticket))
    ticket_type = str(ticket.get("ticket_type") or "")
    order_action = str(ticket.get("order_action") or "")
    original_risk = max(0.0, _ticket_trade_risk(ticket))
    original_gross = max(0.0, _ticket_gross_notional(ticket))
    risk_cap = min(limits["single_trade_risk"], limits["symbol_risk"]) * (1.0 - float(sizing_safety_buffer_pct))
    risk_cap = max(0.0, risk_cap)
    checks: list[dict[str, Any]] = []
    review_reasons = list(sized_readiness.get("review_reasons") or [])

    if ticket_type == "stock":
        original_qty = _float_or_none(ticket.get("quantity"))
        if original_qty is None or original_qty <= 0:
            sizing["status"] = "not_sized"
            sizing["reasons"] = ["stock_quantity_missing"]
            sized_readiness["readiness_status"] = "not_ready"
            sized_readiness["review_reasons"] = list(dict.fromkeys(review_reasons + sizing["reasons"]))
            item["execution_readiness"] = sized_readiness
            item["account_position_sizing"] = sizing
            return item
        per_trade_notional_cap = max(0.0, limits["gross_notional"] * float(max_stock_notional_pct))
        if order_action == "BUY":
            per_trade_notional_cap = min(per_trade_notional_cap, limits["cash"] * float(max_cash_per_trade_pct))
        elif order_action == "SELL_SHORT":
            per_trade_notional_cap = min(per_trade_notional_cap, limits["short_notional"] * float(max_stock_notional_pct))
        scale_candidates = [1.0]
        if original_risk > 0:
            scale_candidates.append(risk_cap / original_risk)
        if original_gross > 0:
            scale_candidates.append(per_trade_notional_cap / original_gross)
        scale = max(0.0, min(scale_candidates))
        sized_qty = _rounded_stock_quantity(
            original_qty * scale,
            allow_fractional_shares=allow_fractional_shares,
            stock_quantity_increment=stock_quantity_increment,
        )
        if sized_qty > original_qty:
            sized_qty = original_qty
        if sized_qty < float(min_stock_quantity):
            sizing["status"] = "not_sized"
            sizing["reasons"] = ["sized_stock_quantity_below_minimum"]
            sized_readiness["readiness_status"] = "not_ready"
            sized_readiness["review_reasons"] = list(dict.fromkeys(review_reasons + sizing["reasons"]))
        else:
            ratio = sized_qty / original_qty
            sized_risk = round(original_risk * ratio, 6)
            sized_gross = round(original_gross * ratio, 6)
            sized_ticket["quantity"] = round(sized_qty, 6)
            if sized_ticket.get("estimated_notional") is not None:
                sized_ticket["estimated_notional"] = sized_gross
            if sized_ticket.get("stop_loss_proxy") is not None:
                sized_ticket["stop_loss_proxy"] = sized_risk
            sizing.update(
                {
                    "status": "sized",
                    "scale_factor": ratio,
                    "sized_trade_risk": sized_risk,
                    "sized_gross_notional": sized_gross,
                    "original_quantity": original_qty,
                    "sized_quantity": round(sized_qty, 6),
                    "allow_fractional_shares": bool(allow_fractional_shares),
                }
            )
            checks.extend(
                [
                    _execution_check("sized_trade_risk_within_single_trade_cap", sized_risk <= limits["single_trade_risk"], value=sized_risk, limit=limits["single_trade_risk"]),
                    _execution_check("sized_trade_risk_within_symbol_cap", sized_risk <= limits["symbol_risk"], value=sized_risk, limit=limits["symbol_risk"]),
                    _execution_check("sized_stock_notional_within_per_trade_cap", sized_gross <= per_trade_notional_cap, value=sized_gross, limit=per_trade_notional_cap),
                ]
            )
            if allow_fractional_shares and not float(sized_qty).is_integer():
                review_reasons.append("fractional_share_execution_support_requires_account_confirmation")
            sized_readiness["ticket_draft"] = sized_ticket
            sized_readiness["checks"] = list(sized_readiness.get("checks") or []) + checks
            sized_readiness["human_review_required"] = bool(sized_readiness.get("human_review_required")) or bool(review_reasons)
            sized_readiness["review_reasons"] = list(dict.fromkeys(review_reasons))
            sized_readiness["readiness_status"] = "needs_human_review" if sized_readiness["human_review_required"] else "ready"
    elif ticket_type == "option_combo_debit_spread":
        original_qty = _float_or_none(ticket.get("quantity")) or 1.0
        risk_per_contract = original_risk / max(1.0, original_qty) if original_risk > 0 else 0.0
        option_risk_cap = min(risk_cap, limits["option_risk"]) * (1.0 - float(sizing_safety_buffer_pct))
        if risk_per_contract <= 0:
            sizing["status"] = "not_sized"
            sizing["reasons"] = ["option_risk_missing"]
            sized_readiness["readiness_status"] = "not_ready"
        else:
            max_contracts = math.floor(option_risk_cap / risk_per_contract)
            if max_contracts < 1:
                sizing["status"] = "not_sized"
                sizing["reasons"] = ["minimum_option_contract_exceeds_account_risk_budget"]
                sized_readiness["readiness_status"] = "not_ready"
            else:
                sized_qty = min(original_qty, float(max_contracts))
                ratio = sized_qty / original_qty
                sized_risk = round(original_risk * ratio, 6)
                sized_gross = round(original_gross * ratio, 6)
                sized_ticket["quantity"] = int(sized_qty)
                for key in ("estimated_max_loss", "estimated_max_profit"):
                    if sized_ticket.get(key) is not None:
                        sized_ticket[key] = round(float(sized_ticket[key]) * ratio, 6)
                if sized_ticket.get("estimated_net_debit") is not None:
                    sized_ticket["estimated_net_debit_total"] = round(float(sized_ticket["estimated_net_debit"]) * sized_qty, 6)
                sizing.update(
                    {
                        "status": "sized",
                        "scale_factor": ratio,
                        "sized_trade_risk": sized_risk,
                        "sized_gross_notional": sized_gross,
                        "original_quantity": original_qty,
                        "sized_quantity": int(sized_qty),
                    }
                )
                checks.extend(
                    [
                        _execution_check("sized_option_risk_within_single_trade_cap", sized_risk <= limits["single_trade_risk"], value=sized_risk, limit=limits["single_trade_risk"]),
                        _execution_check("sized_option_risk_within_option_cap", sized_risk <= limits["option_risk"], value=sized_risk, limit=limits["option_risk"]),
                    ]
                )
                sized_readiness["ticket_draft"] = sized_ticket
                sized_readiness["checks"] = list(sized_readiness.get("checks") or []) + checks
                sized_readiness["human_review_required"] = True
                sized_readiness["review_reasons"] = list(dict.fromkeys(review_reasons))
                sized_readiness["readiness_status"] = "needs_human_review"
        if sizing["status"] != "sized":
            sized_readiness["review_reasons"] = list(dict.fromkeys(review_reasons + sizing["reasons"]))
            sized_readiness["ticket_draft"] = None
    else:
        sizing["status"] = "not_sized"
        sizing["reasons"] = [f"unsupported_ticket_type:{ticket_type or 'unknown'}"]
        sized_readiness["readiness_status"] = "not_ready"
        sized_readiness["review_reasons"] = list(dict.fromkeys(review_reasons + sizing["reasons"]))
        sized_readiness["ticket_draft"] = None

    item["execution_readiness"] = sized_readiness
    item["account_position_sizing"] = sizing
    return item


def build_account_scaled_position_sizing_report(
    readiness_payload: dict[str, Any],
    account_snapshot: dict[str, Any],
    *,
    equity_haircut_pct: float = 0.0,
    cash_haircut_pct: float = 0.0,
    max_single_trade_risk_pct: float = 0.005,
    max_total_open_risk_pct: float = 0.05,
    max_gross_notional_pct: float = 1.0,
    max_option_risk_pct: float = 0.02,
    max_short_notional_pct: float = 0.20,
    max_symbol_risk_pct: float = 0.01,
    max_stock_notional_pct: float = 0.02,
    max_cash_per_trade_pct: float = 0.02,
    sizing_safety_buffer_pct: float = 0.05,
    allow_fractional_shares: bool = True,
    min_stock_quantity: float = 0.01,
    stock_quantity_increment: float = 0.01,
    max_approved_tickets: int = 25,
    sample_limit: int = 20,
) -> dict[str, Any]:
    source_result, readiness_items = _readiness_payload_items(readiness_payload)
    equity, cash, effective_equity, effective_cash = _effective_account_budget(
        account_snapshot,
        equity_haircut_pct=equity_haircut_pct,
        cash_haircut_pct=cash_haircut_pct,
    )
    limits = _account_budget_limits(
        equity=effective_equity,
        cash=effective_cash,
        max_single_trade_risk_pct=max_single_trade_risk_pct,
        max_total_open_risk_pct=max_total_open_risk_pct,
        max_gross_notional_pct=max_gross_notional_pct,
        max_option_risk_pct=max_option_risk_pct,
        max_short_notional_pct=max_short_notional_pct,
        max_symbol_risk_pct=max_symbol_risk_pct,
    )
    sized_items = [
        _size_readiness_item_for_account(
            item,
            limits=limits,
            max_stock_notional_pct=max_stock_notional_pct,
            max_cash_per_trade_pct=max_cash_per_trade_pct,
            sizing_safety_buffer_pct=sizing_safety_buffer_pct,
            allow_fractional_shares=allow_fractional_shares,
            min_stock_quantity=min_stock_quantity,
            stock_quantity_increment=stock_quantity_increment,
        )
        for item in readiness_items
    ]
    sizing_status_counts: Counter[str] = Counter()
    sizing_reason_counts: Counter[str] = Counter()
    readiness_counts: Counter[str] = Counter()
    check_failure_counts: Counter[str] = Counter()
    ticketed = []
    actionable = []
    for item in sized_items:
        sizing = item.get("account_position_sizing") or {}
        sizing_status_counts[str(sizing.get("status") or "unknown")] += 1
        for reason in sizing.get("reasons") or []:
            sizing_reason_counts[str(reason)] += 1
        readiness = item.get("execution_readiness") or {}
        status = str(readiness.get("readiness_status") or "unknown")
        readiness_counts[status] += 1
        if readiness.get("ticket_draft"):
            ticketed.append(item)
        if readiness.get("ticket_draft") and status in {"ready", "needs_human_review"}:
            actionable.append(item)
        for check in readiness.get("checks") or []:
            if not check.get("passed"):
                check_failure_counts[str(check.get("name") or "unknown_check")] += 1
    sized_execution_readiness = {
        "execution_readiness_version": "paper-account-scaled-execution-readiness-v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "source_execution_readiness_version": source_result.get("execution_readiness_version"),
        "source_rows": source_result.get("source_rows"),
        "source_selected_allocations": source_result.get("source_selected_allocations"),
        "readiness_counts": dict(readiness_counts),
        "check_failure_counts": dict(check_failure_counts),
        "ticket_drafts": len(ticketed),
        "actionable_ticket_drafts": len(actionable),
        "readiness_items": sized_items,
        "sample_readiness_items": sized_items[: max(0, int(sample_limit))],
        "safety": {
            "mode": "read_only_account_scaled_position_sizing",
            "policy": "Resizes stored paper ticket drafts using read-only account budget inputs only; no IBKR order action.",
        },
    }
    account_backed = build_account_backed_portfolio_readiness_report(
        sized_execution_readiness,
        account_snapshot,
        equity_haircut_pct=equity_haircut_pct,
        cash_haircut_pct=cash_haircut_pct,
        max_single_trade_risk_pct=max_single_trade_risk_pct,
        max_total_open_risk_pct=max_total_open_risk_pct,
        max_gross_notional_pct=max_gross_notional_pct,
        max_option_risk_pct=max_option_risk_pct,
        max_short_notional_pct=max_short_notional_pct,
        max_symbol_risk_pct=max_symbol_risk_pct,
        max_approved_tickets=max_approved_tickets,
        sample_limit=sample_limit,
    )
    return {
        "account_scaled_position_sizing_version": "paper-account-scaled-position-sizing-v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "account_snapshot": {
            "snapshot_version": account_snapshot.get("snapshot_version"),
            "generated_at": account_snapshot.get("generated_at"),
            "selected_account": account_snapshot.get("selected_account"),
            "raw_equity": equity,
            "raw_cash": cash,
            "buying_power": account_snapshot.get("buying_power"),
            "position_count": account_snapshot.get("position_count"),
            "open_order_count": account_snapshot.get("open_order_count"),
            "errors_count": len(account_snapshot.get("errors") or []),
            "effective_equity": effective_equity,
            "effective_cash": effective_cash,
        },
        "sizing_parameters": {
            "equity_haircut_pct": float(equity_haircut_pct),
            "cash_haircut_pct": float(cash_haircut_pct),
            "max_stock_notional_pct": float(max_stock_notional_pct),
            "max_cash_per_trade_pct": float(max_cash_per_trade_pct),
            "sizing_safety_buffer_pct": float(sizing_safety_buffer_pct),
            "allow_fractional_shares": bool(allow_fractional_shares),
            "min_stock_quantity": float(min_stock_quantity),
            "stock_quantity_increment": float(stock_quantity_increment),
        },
        "limits": limits,
        "sizing_status_counts": dict(sizing_status_counts),
        "sizing_reason_counts": dict(sizing_reason_counts),
        "sized_execution_readiness": sized_execution_readiness,
        "account_backed_portfolio_readiness": account_backed,
        "sample_sized_items": sized_items[: max(0, int(sample_limit))],
        "safety": {
            "mode": "read_only_account_scaled_position_sizing",
            "policy": "Position sizing is applied to paper ticket drafts only. It does not create, preview, submit, transmit, cancel, or modify IBKR orders.",
        },
    }


def account_scaled_position_sizing_markdown(
    result: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
    title: str = "Paper Account-Scaled Position Sizing Report",
) -> str:
    filters = filters or {}
    account = result.get("account_snapshot") or {}
    portfolio = result.get("account_backed_portfolio_readiness") or {}
    lines = [
        f"# {title}",
        "",
        f"- Generated at: `{result.get('generated_at')}`",
        f"- Selected account: `{account.get('selected_account')}`",
        f"- Effective equity: `{_fmt_money(account.get('effective_equity'))}`",
        f"- Effective cash: `{_fmt_money(account.get('effective_cash'))}`",
        f"- Sized ticket drafts: `{(result.get('sized_execution_readiness') or {}).get('ticket_drafts')}`",
        f"- Sized actionable ticket drafts: `{(result.get('sized_execution_readiness') or {}).get('actionable_ticket_drafts')}`",
        f"- Approved ticket count after account-backed portfolio gate: `{portfolio.get('approved_ticket_count')}`",
        "- Safety: read-only account inputs and paper ticket resizing only; no order submission.",
        "",
        "## Inputs",
        "",
    ]
    if filters:
        for key, value in filters.items():
            lines.append(f"- `{key}`: `{value if value is not None else '-'}`")
    else:
        lines.append("- none")
    lines.extend(["", "## Sizing parameters", "", "| Parameter | Value |", "| --- | ---: |"])
    for key, value in (result.get("sizing_parameters") or {}).items():
        lines.append(f"| {key} | {value} |")
    for title_text, counts in (
        ("Sizing status counts", result.get("sizing_status_counts") or {}),
        ("Sizing reason counts", result.get("sizing_reason_counts") or {}),
        ("Sized readiness counts", (result.get("sized_execution_readiness") or {}).get("readiness_counts") or {}),
        ("Portfolio status counts after sizing", portfolio.get("portfolio_status_counts") or {}),
        ("Portfolio check failures after sizing", portfolio.get("portfolio_check_failure_counts") or {}),
    ):
        lines.extend(["", f"## {title_text}", "", "| Bucket | Count |", "| --- | ---: |"])
        if counts:
            for key, value in sorted(counts.items(), key=lambda item: (-int(item[1]), item[0])):
                lines.append(f"| {key} | {value} |")
        else:
            lines.append("| - | 0 |")
    lines.extend(
        [
            "",
            "## Used portfolio budget after sizing",
            "",
            "| Budget | Used |",
            "| --- | ---: |",
        ]
    )
    used = portfolio.get("used_budget") or {}
    for key in ("total_open_risk", "gross_notional", "option_risk", "short_notional", "cash", "approved_tickets"):
        value = used.get(key)
        lines.append(f"| {key} | {_fmt_money(value) if isinstance(value, (int, float)) else value} |")
    lines.extend(
        [
            "",
            "## Sample sized tickets",
            "",
            "| Symbol | Ticket | Sizing status | Original risk | Sized risk | Original gross | Sized gross |",
            "| --- | --- | --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for item in result.get("sample_sized_items") or []:
        readiness = item.get("execution_readiness") or {}
        ticket = readiness.get("ticket_draft") or {}
        sizing = item.get("account_position_sizing") or {}
        lines.append(
            f"| {item.get('symbol')} | {ticket.get('ticket_type') or '-'} | {sizing.get('status')} "
            f"| {_fmt_money(sizing.get('original_trade_risk'))} | {_fmt_money(sizing.get('sized_trade_risk'))} "
            f"| {_fmt_money(sizing.get('original_gross_notional'))} | {_fmt_money(sizing.get('sized_gross_notional'))} |"
        )
    lines.extend(
        [
            "",
            "## Interpretation note",
            "",
            "M111 solves the M110 fixed-ticket-size problem by resizing paper ticket drafts before applying account-backed portfolio budgets. Options remain conservative: if one minimum contract exceeds the account risk cap, the option ticket is blocked rather than forced through.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_account_scaled_position_sizing_markdown(
    output_path: Path,
    result: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(account_scaled_position_sizing_markdown(result, filters=filters), encoding="utf-8")
    return output_path


def _m111_payload_result(payload: dict[str, Any]) -> dict[str, Any]:
    return payload.get("result") or payload


def _approved_portfolio_items_from_sizing_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    result = _m111_payload_result(payload)
    portfolio = result.get("account_backed_portfolio_readiness") or result
    items = list(portfolio.get("portfolio_items") or [])
    approved = [
        item
        for item in items
        if (item.get("mock_portfolio_readiness") or {}).get("status") in {"portfolio_ready", "portfolio_needs_human_review"}
    ]
    return result, approved


def _is_fractional_quantity(quantity: Any) -> bool:
    value = _float_or_none(quantity)
    if value is None:
        return False
    return abs(value - round(value)) > 1e-9


def _local_margin_estimate_for_ticket(ticket: dict[str, Any]) -> dict[str, Any]:
    ticket_type = str(ticket.get("ticket_type") or "")
    action = str(ticket.get("order_action") or "")
    gross = max(0.0, _ticket_gross_notional(ticket))
    risk = max(0.0, _ticket_trade_risk(ticket))
    if ticket_type == "stock" and action == "BUY":
        return {
            "estimate_method": "local_stock_cash_conservative_v1",
            "estimated_cash_impact": -gross,
            "estimated_initial_margin_change": gross,
            "estimated_maintenance_margin_change": gross,
            "estimated_max_loss_proxy": risk,
            "note": "Local estimate only; IBKR what-if margin preview should be used before order routing.",
        }
    if ticket_type == "stock" and action == "SELL_SHORT":
        return {
            "estimate_method": "local_short_stock_conservative_v1",
            "estimated_cash_impact": 0.0,
            "estimated_initial_margin_change": gross * 1.5,
            "estimated_maintenance_margin_change": gross * 1.3,
            "estimated_max_loss_proxy": risk,
            "note": "Conservative local short-stock estimate; borrow, locate, and IBKR what-if are required.",
        }
    if ticket_type == "option_combo_debit_spread":
        max_loss = _float_or_none(ticket.get("estimated_max_loss")) or risk
        return {
            "estimate_method": "local_defined_risk_option_debit_spread_v1",
            "estimated_cash_impact": -max_loss,
            "estimated_initial_margin_change": max_loss,
            "estimated_maintenance_margin_change": max_loss,
            "estimated_max_loss_proxy": max_loss,
            "note": "Local defined-risk estimate only; combo what-if and assignment/exercise review required.",
        }
    return {
        "estimate_method": "unsupported_ticket_type",
        "estimated_cash_impact": None,
        "estimated_initial_margin_change": None,
        "estimated_maintenance_margin_change": None,
        "estimated_max_loss_proxy": risk,
        "note": f"No local margin estimate for ticket_type={ticket_type!r}.",
    }


def _stock_preview_contract(symbol: str) -> dict[str, Any]:
    return {
        "sec_type": "STK",
        "symbol": symbol.upper(),
        "exchange": "SMART",
        "currency": "USD",
    }


def _preview_order_for_ticket(ticket: dict[str, Any], *, account: str | None = None) -> dict[str, Any]:
    action = str(ticket.get("order_action") or "")
    ib_action = "SELL" if action == "SELL_SHORT" else action
    order = {
        "action": ib_action,
        "source_order_action": action,
        "order_type": "MKT",
        "order_type_note": "MKT is used for margin preview only; execution layer must choose live limit/market policy separately.",
        "total_quantity": ticket.get("quantity"),
        "tif": "DAY",
        "what_if": True,
        "transmit": False,
        "outside_rth": False,
    }
    if account:
        order["account"] = account
    return order


def build_order_preview_report(
    sizing_payload: dict[str, Any],
    *,
    max_preview_items: int = 25,
    include_human_review: bool = True,
    sample_limit: int = 20,
) -> dict[str, Any]:
    source_result, approved_items = _approved_portfolio_items_from_sizing_payload(sizing_payload)
    account = source_result.get("account_snapshot") or {}
    selected_account = account.get("selected_account")
    preview_items: list[dict[str, Any]] = []
    status_counts: Counter[str] = Counter()
    reason_counts: Counter[str] = Counter()
    ticket_type_counts: Counter[str] = Counter()
    for item in approved_items[: max(0, int(max_preview_items))]:
        readiness = item.get("execution_readiness") or {}
        ticket = readiness.get("ticket_draft") or {}
        ticket_type = str(ticket.get("ticket_type") or "unknown")
        symbol = str(item.get("symbol") or ticket.get("symbol") or "").upper()
        reasons = list(readiness.get("review_reasons") or [])
        status = "preview_ready"
        contract = None
        order = None
        if ticket_type == "stock":
            contract = _stock_preview_contract(symbol)
            order = _preview_order_for_ticket(ticket, account=selected_account)
            if _is_fractional_quantity(ticket.get("quantity")):
                reasons.append("fractional_quantity_requires_ibkr_account_support_confirmation")
            if ticket.get("order_action") == "SELL_SHORT":
                reasons.append("short_stock_requires_borrow_locate_and_margin_what_if")
        else:
            reasons.append(f"live_what_if_builder_not_implemented_for_ticket_type:{ticket_type}")
        reasons = list(dict.fromkeys(str(reason) for reason in reasons if reason))
        if reasons and not include_human_review:
            status = "preview_blocked_human_review"
        elif reasons:
            status = "preview_needs_human_review"
        preview = {
            "preview_status": status,
            "symbol": symbol,
            "source_score_id": item.get("score_id"),
            "source_outcome_id": item.get("outcome_id"),
            "portfolio_status": (item.get("mock_portfolio_readiness") or {}).get("status"),
            "ticket_type": ticket_type,
            "ticket_draft": ticket,
            "contract_preview": contract,
            "ibkr_what_if_order_preview": order,
            "local_margin_estimate": _local_margin_estimate_for_ticket(ticket),
            "review_reasons": reasons,
            "safety": {
                "what_if_only": True,
                "transmit": False,
                "order_submitted": False,
                "local_preview_only": True,
            },
        }
        preview_items.append(preview)
        status_counts[status] += 1
        ticket_type_counts[ticket_type] += 1
        for reason in reasons:
            reason_counts[reason] += 1
    total_initial_margin = sum(
        float((item.get("local_margin_estimate") or {}).get("estimated_initial_margin_change") or 0.0)
        for item in preview_items
    )
    total_maintenance_margin = sum(
        float((item.get("local_margin_estimate") or {}).get("estimated_maintenance_margin_change") or 0.0)
        for item in preview_items
    )
    total_cash_impact = sum(
        float((item.get("local_margin_estimate") or {}).get("estimated_cash_impact") or 0.0)
        for item in preview_items
    )
    return {
        "order_preview_version": "paper-order-preview-margin-whatif-v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "source_position_sizing_version": source_result.get("account_scaled_position_sizing_version"),
        "source_approved_ticket_count": len(approved_items),
        "preview_item_count": len(preview_items),
        "account_snapshot": account,
        "preview_status_counts": dict(status_counts),
        "ticket_type_counts": dict(ticket_type_counts),
        "preview_review_reason_counts": dict(reason_counts),
        "local_margin_totals": {
            "estimated_initial_margin_change": total_initial_margin,
            "estimated_maintenance_margin_change": total_maintenance_margin,
            "estimated_cash_impact": total_cash_impact,
        },
        "preview_items": preview_items,
        "sample_preview_items": preview_items[: max(0, int(sample_limit))],
        "safety": {
            "mode": "read_only_paper_order_preview_queue",
            "policy": "Builds order-preview and local margin-estimate records from paper tickets only. No IBKR order API call is made by this report.",
            "ibkr_order_submitted": False,
            "ibkr_transmit_enabled": False,
        },
    }


def order_preview_markdown(
    result: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
    title: str = "Paper Order Preview / Margin What-If Queue",
) -> str:
    filters = filters or {}
    account = result.get("account_snapshot") or {}
    totals = result.get("local_margin_totals") or {}
    lines = [
        f"# {title}",
        "",
        f"- Generated at: `{result.get('generated_at')}`",
        f"- Selected account: `{account.get('selected_account')}`",
        f"- Source approved ticket count: `{result.get('source_approved_ticket_count')}`",
        f"- Preview item count: `{result.get('preview_item_count')}`",
        f"- Local estimated initial margin change: `{_fmt_money(totals.get('estimated_initial_margin_change'))}`",
        f"- Local estimated maintenance margin change: `{_fmt_money(totals.get('estimated_maintenance_margin_change'))}`",
        f"- Local estimated cash impact: `{_fmt_money(totals.get('estimated_cash_impact'))}`",
        "- Safety: local preview queue only; IBKR live what-if is a separate explicit command; transmit is false.",
        "",
        "## Inputs",
        "",
    ]
    if filters:
        for key, value in filters.items():
            lines.append(f"- `{key}`: `{value if value is not None else '-'}`")
    else:
        lines.append("- none")
    for title_text, counts in (
        ("Preview status counts", result.get("preview_status_counts") or {}),
        ("Ticket type counts", result.get("ticket_type_counts") or {}),
        ("Preview review reason counts", result.get("preview_review_reason_counts") or {}),
    ):
        lines.extend(["", f"## {title_text}", "", "| Bucket | Count |", "| --- | ---: |"])
        if counts:
            for key, value in sorted(counts.items(), key=lambda item: (-int(item[1]), item[0])):
                lines.append(f"| {key} | {value} |")
        else:
            lines.append("| - | 0 |")
    lines.extend(
        [
            "",
            "## Sample preview items",
            "",
            "| Symbol | Status | Ticket | Action | Quantity | Local init margin | Review reasons |",
            "| --- | --- | --- | --- | ---: | ---: | --- |",
        ]
    )
    for item in result.get("sample_preview_items") or []:
        ticket = item.get("ticket_draft") or {}
        order = item.get("ibkr_what_if_order_preview") or {}
        margin = item.get("local_margin_estimate") or {}
        reasons = ", ".join(item.get("review_reasons") or []) or "-"
        lines.append(
            f"| {item.get('symbol')} | {item.get('preview_status')} | {item.get('ticket_type')} "
            f"| {order.get('action') or ticket.get('order_action')} | {order.get('total_quantity') or ticket.get('quantity')} "
            f"| {_fmt_money(margin.get('estimated_initial_margin_change'))} | {reasons} |"
        )
    lines.extend(
        [
            "",
            "## Interpretation note",
            "",
            "M112 local preview prepares what-if-safe order drafts and local margin estimates. It is not a live order layer. Use the explicit IBKR what-if command to request broker-side margin preview, and keep `whatIf=True` and `transmit=False`.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_order_preview_markdown(
    output_path: Path,
    result: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(order_preview_markdown(result, filters=filters), encoding="utf-8")
    return output_path


def _order_preview_payload_result(payload: dict[str, Any]) -> dict[str, Any]:
    return payload.get("result") or payload


def _auto_gate_check(name: str, passed: bool, *, value: Any = None, limit: Any = None) -> dict[str, Any]:
    return {
        "name": name,
        "passed": bool(passed),
        "value": value,
        "limit": limit,
        "severity": "hard",
    }


def build_autonomous_execution_gate_report(
    preview_payload: dict[str, Any],
    *,
    allow_fractional_shares: bool = False,
    allow_short_stock: bool = False,
    require_short_availability_for_shorts: bool = True,
    short_availability_payload: dict[str, Any] | None = None,
    require_broker_whatif: bool = False,
    broker_whatif_payload: dict[str, Any] | None = None,
    max_auto_orders: int = 10,
    min_cash_buffer: float = 0.0,
    max_local_initial_margin_total: float | None = None,
    allowed_ticket_types: tuple[str, ...] = ("stock",),
    allowed_actions: tuple[str, ...] = ("BUY", "SELL"),
    evidence_preflight_payload: dict[str, Any] | None = None,
    market_data_risk_gate_payload: dict[str, Any] | None = None,
    sample_limit: int = 20,
) -> dict[str, Any]:
    preview = _order_preview_payload_result(preview_payload)
    evidence_preflight = (evidence_preflight_payload or {}).get("result") or (evidence_preflight_payload or {})
    evidence_preflight_configured = bool(evidence_preflight)
    evidence_allowed = bool(evidence_preflight.get("allowed", True))
    evidence_action = str(evidence_preflight.get("action") or "not_configured")
    evidence_size_multiplier = float(evidence_preflight.get("entry_size_multiplier", 1.0) if evidence_preflight else 1.0)
    evidence_size_multiplier = max(0.0, min(1.0, evidence_size_multiplier))
    market_data_risk_gate = (market_data_risk_gate_payload or {}).get("result") or (market_data_risk_gate_payload or {})
    market_data_risk_gate_configured = bool(market_data_risk_gate)
    market_data_allowed = bool(market_data_risk_gate.get("allowed", True))
    market_data_action = str(market_data_risk_gate.get("action") or "not_configured")
    market_data_size_multiplier = float(market_data_risk_gate.get("entry_size_multiplier", 1.0) if market_data_risk_gate else 1.0)
    market_data_size_multiplier = max(0.0, min(1.0, market_data_size_multiplier))
    account = preview.get("account_snapshot") or {}
    effective_cash = _float_or_none(account.get("effective_cash")) or _float_or_none(account.get("raw_cash")) or 0.0
    whatif = (broker_whatif_payload or {}).get("result") or (broker_whatif_payload or {})
    whatif_symbols = {
        str(row.get("symbol") or "").upper()
        for row in whatif.get("responses") or []
        if row.get("symbol")
    }
    short_payload = (short_availability_payload or {}).get("result") or (short_availability_payload or {})
    short_rows = {
        str(row.get("symbol") or "").upper(): row
        for row in short_payload.get("rows") or []
        if row.get("symbol")
    }
    status_counts: Counter[str] = Counter()
    reason_counts: Counter[str] = Counter()
    auto_items: list[dict[str, Any]] = []
    approved_items: list[dict[str, Any]] = []
    cumulative_cash_impact = 0.0
    cumulative_initial_margin = 0.0
    for raw in preview.get("preview_items") or []:
        item = json.loads(json.dumps(raw))
        symbol = str(item.get("symbol") or "").upper()
        ticket = item.get("ticket_draft") or {}
        order = item.get("ibkr_what_if_order_preview") or {}
        local_margin = item.get("local_margin_estimate") or {}
        ticket_type = str(item.get("ticket_type") or ticket.get("ticket_type") or "")
        action = str(order.get("source_order_action") or ticket.get("order_action") or order.get("action") or "")
        quantity = _float_or_none(order.get("total_quantity") if order else ticket.get("quantity"))
        cash_impact = float(local_margin.get("estimated_cash_impact") or 0.0)
        initial_margin = float(local_margin.get("estimated_initial_margin_change") or 0.0)
        is_fractional = _is_fractional_quantity(quantity)
        is_short = action == "SELL_SHORT"
        short_row = short_rows.get(symbol)
        shortable_shares = _float_or_none((short_row or {}).get("shortable_shares"))
        shortable_score = _float_or_none((short_row or {}).get("shortable_score"))
        if not is_short:
            short_availability_passed = True
            short_availability_value: Any = "not_short"
        elif short_row is None:
            short_availability_passed = not require_short_availability_for_shorts
            short_availability_value = "missing"
        elif shortable_shares is not None:
            short_availability_passed = quantity is not None and shortable_shares >= quantity
            short_availability_value = shortable_shares
        else:
            short_availability_passed = shortable_score is not None and shortable_score > 2.5
            short_availability_value = shortable_score
        checks = [
            _auto_gate_check("preview_status_acceptable", item.get("preview_status") in {"preview_ready", "preview_needs_human_review"}, value=item.get("preview_status"), limit="preview_ready_or_preview_needs_human_review"),
            _auto_gate_check("ticket_type_allowed", ticket_type in set(allowed_ticket_types), value=ticket_type, limit=",".join(allowed_ticket_types)),
            _auto_gate_check("action_allowed", (("SELL" if is_short else action) in set(allowed_actions)) or (action in set(allowed_actions)), value=action, limit=",".join(allowed_actions)),
            _auto_gate_check("whatif_order_flag_present", bool(order.get("what_if") is True), value=order.get("what_if"), limit=True),
            _auto_gate_check("transmit_false", bool(order.get("transmit") is False), value=order.get("transmit"), limit=False),
            _auto_gate_check("positive_quantity", quantity is not None and quantity > 0, value=quantity, limit=">0"),
            _auto_gate_check("fractional_share_policy", allow_fractional_shares or not is_fractional, value=quantity, limit="fractional_allowed" if allow_fractional_shares else "whole_shares_only"),
            _auto_gate_check("short_stock_policy", allow_short_stock or not is_short, value=action, limit="short_allowed" if allow_short_stock else "long_only"),
            _auto_gate_check("short_availability_confirmed", short_availability_passed, value=short_availability_value, limit="shares>=quantity_or_shortable_score>2.5"),
            _auto_gate_check("local_margin_estimate_present", local_margin.get("estimated_initial_margin_change") is not None, value=local_margin.get("estimate_method"), limit="present"),
            _auto_gate_check("cash_buffer_after_order", effective_cash + cumulative_cash_impact + cash_impact >= float(min_cash_buffer), value=effective_cash + cumulative_cash_impact + cash_impact, limit=min_cash_buffer),
        ]
        if evidence_preflight_configured:
            checks.append(
                _auto_gate_check(
                    "evidence_preflight_allows_new_paper_entries",
                    evidence_allowed,
                    value=evidence_action,
                    limit="allow_normal_or_allow_with_evidence_haircut",
                )
            )
        if market_data_risk_gate_configured:
            checks.append(
                _auto_gate_check(
                    "market_data_risk_gate_allows_new_paper_entries",
                    market_data_allowed,
                    value=market_data_action,
                    limit="allow_normal_or_allow_with_market_data_haircut",
                )
            )
        if max_local_initial_margin_total is not None:
            checks.append(
                _auto_gate_check(
                    "local_initial_margin_total_within_cap",
                    cumulative_initial_margin + initial_margin <= float(max_local_initial_margin_total),
                    value=cumulative_initial_margin + initial_margin,
                    limit=max_local_initial_margin_total,
                )
            )
        if require_broker_whatif:
            checks.append(
                _auto_gate_check(
                    "broker_whatif_response_present",
                    symbol in whatif_symbols,
                    value=symbol,
                    limit="ibkr_whatif_response",
                )
            )
        if len(approved_items) >= int(max_auto_orders):
            checks.append(
                _auto_gate_check(
                    "max_auto_orders_not_exceeded",
                    False,
                    value=len(approved_items) + 1,
                    limit=max_auto_orders,
                )
            )
        else:
            checks.append(
                _auto_gate_check(
                    "max_auto_orders_not_exceeded",
                    True,
                    value=len(approved_items) + 1,
                    limit=max_auto_orders,
                )
            )
        failed = [check for check in checks if not check.get("passed")]
        if failed:
            status = "auto_execution_blocked"
            if any(check.get("name") == "max_auto_orders_not_exceeded" for check in failed):
                status = "auto_execution_capacity_blocked"
        else:
            status = "auto_execution_ready"
            cumulative_cash_impact += cash_impact
            cumulative_initial_margin += initial_margin
            approved_items.append(item)
        reasons = [str(check.get("name")) for check in failed]
        auto_item = {
            "auto_execution_status": status,
            "symbol": symbol,
            "ticket_type": ticket_type,
            "action": action,
            "quantity": quantity,
            "checks": checks,
            "block_reasons": reasons,
            "evidence_preflight": evidence_preflight if evidence_preflight_configured else None,
            "evidence_preflight_action": evidence_action if evidence_preflight_configured else None,
            "evidence_entry_size_multiplier": evidence_size_multiplier if evidence_preflight_configured else 1.0,
            "market_data_risk_gate": market_data_risk_gate if market_data_risk_gate_configured else None,
            "market_data_risk_gate_action": market_data_action if market_data_risk_gate_configured else None,
            "market_data_entry_size_multiplier": market_data_size_multiplier if market_data_risk_gate_configured else 1.0,
            "local_margin_estimate": local_margin,
            "order_intent": {
                "contract_preview": item.get("contract_preview"),
                "order_preview": order,
                "transmit": False,
                "what_if": True,
            },
            "source_preview_item": item,
            "safety": {
                "auto_gate_only": True,
                "ibkr_order_submitted": False,
                "transmit": False,
            },
        }
        auto_items.append(auto_item)
        status_counts[status] += 1
        for reason in reasons:
            reason_counts[reason] += 1
    return {
        "autonomous_execution_gate_version": "paper-autonomous-execution-gate-v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "source_order_preview_version": preview.get("order_preview_version"),
        "source_preview_item_count": preview.get("preview_item_count"),
        "auto_ready_count": len(approved_items),
        "auto_gate_status_counts": dict(status_counts),
        "auto_gate_block_reason_counts": dict(reason_counts),
        "gate_parameters": {
            "allow_fractional_shares": bool(allow_fractional_shares),
            "allow_short_stock": bool(allow_short_stock),
            "require_broker_whatif": bool(require_broker_whatif),
            "max_auto_orders": int(max_auto_orders),
            "min_cash_buffer": float(min_cash_buffer),
            "max_local_initial_margin_total": max_local_initial_margin_total,
            "allowed_ticket_types": list(allowed_ticket_types),
            "allowed_actions": list(allowed_actions),
            "require_short_availability_for_shorts": bool(require_short_availability_for_shorts),
            "evidence_preflight_configured": evidence_preflight_configured,
            "evidence_preflight_action": evidence_action if evidence_preflight_configured else None,
            "evidence_entry_size_multiplier": evidence_size_multiplier if evidence_preflight_configured else 1.0,
            "market_data_risk_gate_configured": market_data_risk_gate_configured,
            "market_data_risk_gate_action": market_data_action if market_data_risk_gate_configured else None,
            "market_data_entry_size_multiplier": market_data_size_multiplier if market_data_risk_gate_configured else 1.0,
        },
        "evidence_preflight": evidence_preflight if evidence_preflight_configured else None,
        "market_data_risk_gate": market_data_risk_gate if market_data_risk_gate_configured else None,
        "used_budget": {
            "cumulative_cash_impact": cumulative_cash_impact,
            "cumulative_initial_margin": cumulative_initial_margin,
            "effective_cash": effective_cash,
        },
        "auto_execution_items": auto_items,
        "sample_auto_execution_items": auto_items[: max(0, int(sample_limit))],
        "safety": {
            "mode": "paper_autonomous_execution_gate",
            "policy": "Converts preview review reasons and optional evidence preflight state into deterministic hard gates and emits paper auto-execution intents only. No IBKR order is submitted or transmitted.",
            "ibkr_order_submitted": False,
            "transmit_enabled": False,
        },
    }


def autonomous_execution_gate_markdown(
    result: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
    title: str = "Paper Autonomous Execution Gate Report",
) -> str:
    filters = filters or {}
    used = result.get("used_budget") or {}
    lines = [
        f"# {title}",
        "",
        f"- Generated at: `{result.get('generated_at')}`",
        f"- Source preview item count: `{result.get('source_preview_item_count')}`",
        f"- Auto-ready count: `{result.get('auto_ready_count')}`",
        f"- Cumulative local initial margin: `{_fmt_money(used.get('cumulative_initial_margin'))}`",
        f"- Cumulative cash impact: `{_fmt_money(used.get('cumulative_cash_impact'))}`",
        f"- Evidence preflight: `{(result.get('evidence_preflight') or {}).get('gate_status') or 'not_configured'}` / action `{(result.get('evidence_preflight') or {}).get('action') or '-'}`",
        f"- Market data risk gate: `{(result.get('market_data_risk_gate') or {}).get('gate_status') or 'not_configured'}` / action `{(result.get('market_data_risk_gate') or {}).get('action') or '-'}`",
        "- Safety: deterministic gate only; no IBKR order submission or transmit.",
        "",
        "## Inputs",
        "",
    ]
    if filters:
        for key, value in filters.items():
            lines.append(f"- `{key}`: `{value if value is not None else '-'}`")
    else:
        lines.append("- none")
    lines.extend(["", "## Gate parameters", "", "| Parameter | Value |", "| --- | --- |"])
    for key, value in (result.get("gate_parameters") or {}).items():
        lines.append(f"| {key} | {value} |")
    for title_text, counts in (
        ("Auto gate status counts", result.get("auto_gate_status_counts") or {}),
        ("Auto gate block reason counts", result.get("auto_gate_block_reason_counts") or {}),
    ):
        lines.extend(["", f"## {title_text}", "", "| Bucket | Count |", "| --- | ---: |"])
        if counts:
            for key, value in sorted(counts.items(), key=lambda item: (-int(item[1]), item[0])):
                lines.append(f"| {key} | {value} |")
        else:
            lines.append("| - | 0 |")
    lines.extend(
        [
            "",
            "## Sample auto execution intents",
            "",
            "| Symbol | Status | Action | Quantity | Block reasons |",
            "| --- | --- | --- | ---: | --- |",
        ]
    )
    for item in result.get("sample_auto_execution_items") or []:
        reasons = ", ".join(item.get("block_reasons") or []) or "-"
        lines.append(
        f"| {item.get('symbol')} | {item.get('auto_execution_status')} | {item.get('action')} | {item.get('quantity')} | {reasons} |"
        )
    lines.extend(
        [
            "",
            "## Interpretation note",
            "",
            "M113 moves toward automated trading by replacing human review labels with deterministic machine gates. Passing this report means a paper order intent is auto-ready, not that a live order may be transmitted. Live transmission remains a separate kill-switch controlled layer.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_autonomous_execution_gate_markdown(
    output_path: Path,
    result: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(autonomous_execution_gate_markdown(result, filters=filters), encoding="utf-8")
    return output_path


def _auto_gate_payload_result(payload: dict[str, Any]) -> dict[str, Any]:
    return payload.get("result") or payload


CAPITAL_AWARE_READY_STATUSES = {"capital_ready", "capital_resized_ready"}


def _is_capital_aware_gate_payload(auto_gate: dict[str, Any]) -> bool:
    return bool(
        auto_gate.get("capital_aware_action_gate_version")
        or auto_gate.get("capital_gate_status_counts")
        or auto_gate.get("capital_gate_block_reason_counts")
        or auto_gate.get("capital_state")
    )


def _capital_aware_gate_snapshot(auto_gate: dict[str, Any]) -> dict[str, Any]:
    capital_state = auto_gate.get("capital_state") if isinstance(auto_gate.get("capital_state"), dict) else {}
    return {
        "configured": _is_capital_aware_gate_payload(auto_gate),
        "version": auto_gate.get("capital_aware_action_gate_version") or auto_gate.get("autonomous_execution_gate_version"),
        "generated_at": auto_gate.get("generated_at"),
        "capital_ready_count": auto_gate.get("capital_ready_count"),
        "capital_resized_count": auto_gate.get("capital_resized_count"),
        "capital_blocked_count": auto_gate.get("capital_blocked_count"),
        "capital_gate_status_counts": auto_gate.get("capital_gate_status_counts") or {},
        "capital_gate_block_reason_counts": auto_gate.get("capital_gate_block_reason_counts") or {},
        "capital_state_status": capital_state.get("status"),
        "capital_leverage_mode": capital_state.get("leverage_mode"),
        "selected_account": capital_state.get("selected_account"),
        "initial_budget": auto_gate.get("initial_budget") or {},
        "used_budget": auto_gate.get("used_budget") or {},
        "remaining_budget": auto_gate.get("remaining_budget") or {},
    }


def _intent_dedupe_key(auto_item: dict[str, Any]) -> str:
    source = auto_item.get("source_preview_item") or {}
    order_intent = auto_item.get("order_intent") or {}
    order = order_intent.get("order_preview") or {}
    contract = order_intent.get("contract_preview") or {}
    payload = {
        "score_id": source.get("source_score_id"),
        "outcome_id": source.get("source_outcome_id"),
        "symbol": auto_item.get("symbol") or contract.get("symbol"),
        "ticket_type": auto_item.get("ticket_type"),
        "action": auto_item.get("action") or order.get("source_order_action") or order.get("action"),
        "quantity": auto_item.get("quantity") or order.get("total_quantity"),
        "order_type": order.get("order_type"),
        "account": order.get("account"),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _intent_id_from_key(dedupe_key: str) -> str:
    return "intent_" + hashlib.sha256(dedupe_key.encode("utf-8")).hexdigest()[:20]


def _existing_intent_ids(existing_ledger_payload: dict[str, Any] | None) -> set[str]:
    if not existing_ledger_payload:
        return set()
    ledger = existing_ledger_payload.get("result") or existing_ledger_payload
    return {str(item.get("intent_id")) for item in ledger.get("intents") or [] if item.get("intent_id")}


def _paper_portfolio_risk_state_payload_result(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not payload:
        return {}
    return payload.get("result") or payload


def _portfolio_entry_risk_gate_snapshot(
    portfolio_risk_state_payload: dict[str, Any] | None,
    *,
    monitor_size_multiplier: float = 0.50,
) -> dict[str, Any]:
    risk = _paper_portfolio_risk_state_payload_result(portfolio_risk_state_payload)
    if not risk:
        return {
            "mode": "not_configured",
            "portfolio_risk_state": None,
            "source_risk_run_id": None,
            "new_entries_allowed": True,
            "action": "allow_without_portfolio_risk_state",
            "entry_size_multiplier": 1.0,
            "reason": "No M123 portfolio risk-state JSON was provided.",
        }
    risk_state = str(risk.get("risk_state") or "unknown")
    new_entries_allowed = bool(risk.get("new_entries_allowed", risk_state != "reduce_risk"))
    multiplier = max(0.0, min(1.0, float(monitor_size_multiplier)))
    if risk_state == "reduce_risk" or not new_entries_allowed:
        action = "block_new_entries"
        effective_multiplier = 0.0
        reason = "M123 portfolio risk state requires reducing risk before opening new paper entries."
    elif risk_state == "monitor":
        action = "allow_with_monitor_haircut"
        effective_multiplier = multiplier
        reason = "M123 portfolio risk state is monitor, so new paper entries are size-reduced."
    elif risk_state == "within_limits":
        action = "allow_new_entries"
        effective_multiplier = 1.0
        reason = "M123 portfolio risk state is within limits."
    else:
        action = "block_new_entries"
        effective_multiplier = 0.0
        reason = f"Unknown M123 portfolio risk state {risk_state!r}; fail closed."
    return {
        "mode": "m123_portfolio_risk_state",
        "portfolio_risk_state": risk_state,
        "source_risk_run_id": risk.get("risk_run_id"),
        "source_portfolio_state_run_id": risk.get("source_portfolio_state_run_id"),
        "new_entries_allowed": new_entries_allowed,
        "breach_count": risk.get("breach_count"),
        "warning_count": risk.get("warning_count"),
        "recommended_action": risk.get("recommended_action"),
        "action": action,
        "entry_size_multiplier": effective_multiplier,
        "monitor_size_multiplier": multiplier,
        "reason": reason,
    }


def _scale_margin_estimate_for_entry_risk_gate(
    margin: dict[str, Any],
    multiplier: float,
    *,
    multiplier_field: str = "portfolio_risk_gate_size_multiplier",
) -> dict[str, Any]:
    scaled = json.loads(json.dumps(margin or {}))
    for key in (
        "estimated_cash_impact",
        "estimated_initial_margin_change",
        "estimated_maintenance_margin_change",
        "estimated_max_loss_proxy",
    ):
        value = _float_or_none(scaled.get(key))
        if value is not None:
            scaled[key] = value * float(multiplier)
    scaled[multiplier_field] = float(multiplier)
    return scaled


def _scale_order_preview_for_entry_risk_gate(
    order: dict[str, Any],
    multiplier: float,
    *,
    multiplier_field: str = "portfolio_risk_gate_size_multiplier",
) -> dict[str, Any]:
    scaled = json.loads(json.dumps(order or {}))
    quantity = _float_or_none(scaled.get("total_quantity"))
    if quantity is not None:
        scaled["total_quantity"] = quantity * float(multiplier)
    scaled[multiplier_field] = float(multiplier)
    return scaled


def _bool_from_any(value: Any, default: bool = False) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "allow", "allowed", "pass"}:
        return True
    if text in {"0", "false", "no", "n", "block", "blocked", "fail"}:
        return False
    return bool(default)


def _session_transition_release_governor_snapshot(payload: dict[str, Any] | None) -> dict[str, Any]:
    data = (payload.get("result") if isinstance(payload, dict) and isinstance(payload.get("result"), dict) else payload) if isinstance(payload, dict) else {}
    if not data:
        return {
            "configured": False,
            "ordinary_alpha_release_allowed": True,
            "max_new_entry_size_multiplier": 1.0,
            "release_status": "not_configured",
            "reason": "session_transition_release_governor_not_configured",
        }
    controls = data.get("controls") or {}
    allowed = _bool_from_any(
        controls.get("ordinary_alpha_release_allowed", data.get("ordinary_alpha_release_allowed")),
        False,
    )
    multiplier = _float_or_none(controls.get("max_new_entry_size_multiplier", data.get("max_new_entry_size_multiplier")))
    if multiplier is None:
        multiplier = 1.0 if allowed else 0.0
    failed_checks = [
        item.get("name")
        for item in data.get("checks") or []
        if isinstance(item, dict) and not item.get("passed")
    ]
    return {
        "configured": True,
        "version": data.get("session_transition_release_governor_version") or data.get("version"),
        "generated_at": data.get("generated_at"),
        "release_status": data.get("release_status"),
        "severity": data.get("severity"),
        "ordinary_alpha_release_allowed": allowed,
        "max_new_entry_size_multiplier": max(0.0, min(1.0, float(multiplier))),
        "reason": data.get("reason"),
        "failed_checks": failed_checks,
    }


def _is_release_governor_exempt_intent(item: dict[str, Any]) -> bool:
    action = str(item.get("action") or "").upper()
    if action in {"SELL", "BUY_TO_COVER", "SELL_TO_CLOSE", "CLOSE", "REDUCE", "TRIM", "EXIT"}:
        return True
    text_parts: list[str] = []
    for key in (
        "intent_role",
        "ticket_role",
        "trade_role",
        "action_type",
        "strategy",
        "strategy_type",
        "recommended_posture",
        "source",
        "reason",
    ):
        value = item.get(key)
        if value is not None:
            text_parts.append(str(value))
    for container_key in ("tags", "labels", "block_reasons", "state_history"):
        value = item.get(container_key)
        if isinstance(value, list):
            text_parts.extend(str(entry) for entry in value)
    text = " ".join(text_parts).lower()
    return any(
        token in text
        for token in (
            "hedge",
            "defensive",
            "protective",
            "risk_reduction",
            "risk-reduction",
            "exit",
            "reduce",
            "close",
            "collar",
            "covered_call",
        )
    )


def build_execution_intent_ledger_report(
    auto_gate_payload: dict[str, Any],
    *,
    existing_ledger_payload: dict[str, Any] | None = None,
    portfolio_risk_state_payload: dict[str, Any] | None = None,
    session_transition_release_governor_payload: dict[str, Any] | None = None,
    require_capital_aware_gate: bool = False,
    monitor_size_multiplier: float = 0.50,
    max_intents: int = 10,
    ledger_run_id: str | None = None,
    sample_limit: int = 20,
) -> dict[str, Any]:
    auto_gate = _auto_gate_payload_result(auto_gate_payload)
    source_has_capital_aware_gate = _is_capital_aware_gate_payload(auto_gate)
    capital_aware_gate = _capital_aware_gate_snapshot(auto_gate)
    existing_ids = _existing_intent_ids(existing_ledger_payload)
    portfolio_risk_gate = _portfolio_entry_risk_gate_snapshot(
        portfolio_risk_state_payload,
        monitor_size_multiplier=monitor_size_multiplier,
    )
    release_governor = _session_transition_release_governor_snapshot(session_transition_release_governor_payload)
    run_id = ledger_run_id or datetime.now(UTC).strftime("ledger_%Y%m%dT%H%M%SZ")
    intents: list[dict[str, Any]] = []
    status_counts: Counter[str] = Counter()
    action_counts: Counter[str] = Counter()
    symbol_counts: Counter[str] = Counter()
    source_items = list(auto_gate.get("auto_execution_items") or [])
    if require_capital_aware_gate and not source_has_capital_aware_gate:
        ready_items = []
    else:
        ready_items = [
            item
            for item in source_items
            if item.get("auto_execution_status") == "auto_execution_ready"
            and (
                not source_has_capital_aware_gate
                or str(item.get("capital_gate_status") or "") in CAPITAL_AWARE_READY_STATUSES
            )
        ]
    for sequence, item in enumerate(ready_items[: max(0, int(max_intents))], start=1):
        dedupe_key = _intent_dedupe_key(item)
        intent_id = _intent_id_from_key(dedupe_key)
        source = item.get("source_preview_item") or {}
        order_intent = item.get("order_intent") or {}
        order = order_intent.get("order_preview") or {}
        contract = order_intent.get("contract_preview") or {}
        duplicate = intent_id in existing_ids
        capital_gate_status = str(item.get("capital_gate_status") or "")
        capital_aware_applied = source_has_capital_aware_gate and capital_gate_status in CAPITAL_AWARE_READY_STATUSES
        risk_action = str(portfolio_risk_gate.get("action") or "")
        risk_multiplier = float(portfolio_risk_gate.get("entry_size_multiplier") or 0.0)
        evidence_multiplier = float(item.get("evidence_entry_size_multiplier") if item.get("evidence_entry_size_multiplier") is not None else 1.0)
        evidence_multiplier = max(0.0, min(1.0, evidence_multiplier))
        evidence_action = str(item.get("evidence_preflight_action") or "")
        local_margin = item.get("local_margin_estimate") or {}
        quantity = _float_or_none(item.get("quantity"))
        order_for_intent = order
        margin_for_intent = local_margin
        risk_gate_applied = False
        evidence_gate_applied = False
        market_data_gate_applied = False
        gate_severity_applied = False
        release_governor_applied = False
        release_governor_exempt = _is_release_governor_exempt_intent(item)
        market_data_multiplier = float(item.get("market_data_entry_size_multiplier") if item.get("market_data_entry_size_multiplier") is not None else 1.0)
        market_data_multiplier = max(0.0, min(1.0, market_data_multiplier))
        market_data_action = str(item.get("market_data_risk_gate_action") or "")
        gate_severity_multiplier = float(item.get("gate_severity_size_multiplier") if item.get("gate_severity_size_multiplier") is not None else 1.0)
        gate_severity_multiplier = max(0.0, min(1.0, gate_severity_multiplier))
        gate_severity_confidence_multiplier = float(item.get("gate_severity_confidence_multiplier") if item.get("gate_severity_confidence_multiplier") is not None else 1.0)
        gate_severity_confidence_multiplier = max(0.0, min(1.0, gate_severity_confidence_multiplier))
        gate_severity_posture = str(item.get("gate_severity_posture") or "")
        final_size_resolution: dict[str, Any] | None = None
        if duplicate:
            state = "duplicate_existing_intent"
            state_reason = "dedupe_match_existing_ledger"
        elif risk_action == "block_new_entries":
            state = "blocked_by_portfolio_risk_state"
            state_reason = "portfolio_risk_state_blocks_new_entries"
        elif gate_severity_multiplier <= 0.0 or gate_severity_posture in {"hard_blocked", "new_entries_blocked_now"}:
            state = "blocked_by_gate_severity_policy"
            state_reason = f"gate_severity_policy_{gate_severity_posture or 'blocks_new_entries'}"
            gate_severity_applied = True
        elif (
            release_governor.get("configured")
            and not release_governor.get("ordinary_alpha_release_allowed")
            and not release_governor_exempt
        ):
            state = "blocked_by_session_transition_release_governor"
            state_reason = "session_transition_release_governor_blocks_ordinary_alpha"
            release_governor_applied = True
        else:
            state = "pending_paper_execution"
            state_reasons = []
            multiplier_inputs: dict[str, float] = {}
            if risk_action == "allow_with_monitor_haircut":
                multiplier_inputs["portfolio_risk_gate_size_multiplier"] = risk_multiplier
                risk_gate_applied = True
                state_reasons.append("portfolio_risk_monitor_haircut")
            if evidence_multiplier < 1.0:
                multiplier_inputs["evidence_preflight_size_multiplier"] = evidence_multiplier
                evidence_gate_applied = True
                state_reasons.append("evidence_preflight_haircut")
            if market_data_multiplier < 1.0:
                multiplier_inputs["market_data_risk_gate_size_multiplier"] = market_data_multiplier
                market_data_gate_applied = True
                state_reasons.append("market_data_risk_haircut")
            if gate_severity_multiplier < 1.0:
                multiplier_inputs["gate_severity_size_multiplier"] = gate_severity_multiplier
                gate_severity_applied = True
                state_reasons.append("gate_severity_policy_haircut")
            release_multiplier = float(release_governor.get("max_new_entry_size_multiplier") or 1.0)
            release_multiplier = max(0.0, min(1.0, release_multiplier))
            if release_governor.get("configured") and not release_governor_exempt and release_multiplier < 1.0:
                multiplier_inputs["session_transition_release_governor_size_multiplier"] = release_multiplier
                release_governor_applied = True
                state_reasons.append("session_transition_release_governor_haircut")
            final_size_resolution = resolve_final_entry_size(
                source_quantity=quantity,
                ticket_type=item.get("ticket_type"),
                multipliers=multiplier_inputs,
            )
            combined_multiplier = float(final_size_resolution.get("final_size_multiplier") or 0.0)
            if combined_multiplier < 1.0:
                applied_gate_count = sum(
                    1
                    for applied in (
                        risk_gate_applied,
                        evidence_gate_applied,
                        market_data_gate_applied,
                        gate_severity_applied,
                        release_governor_applied,
                    )
                    if applied
                )
                if applied_gate_count > 1:
                    multiplier_field = "combined_entry_gate_size_multiplier"
                elif gate_severity_applied:
                    multiplier_field = "gate_severity_policy_size_multiplier"
                elif release_governor_applied:
                    multiplier_field = "session_transition_release_governor_size_multiplier"
                elif market_data_gate_applied:
                    multiplier_field = "market_data_risk_gate_size_multiplier"
                elif evidence_gate_applied:
                    multiplier_field = "evidence_preflight_size_multiplier"
                else:
                    multiplier_field = "portfolio_risk_gate_size_multiplier"
                quantity = final_size_resolution.get("final_quantity")
                order_for_intent = _scale_order_preview_for_entry_risk_gate(order, combined_multiplier, multiplier_field=multiplier_field)
                margin_for_intent = _scale_margin_estimate_for_entry_risk_gate(local_margin, combined_multiplier, multiplier_field=multiplier_field)
            state_reason = "_and_".join(state_reasons) + "_intent_created" if state_reasons else "auto_gate_ready_intent_created"
        if final_size_resolution is None:
            final_size_resolution = resolve_final_entry_size(
                source_quantity=_float_or_none(item.get("quantity")),
                ticket_type=item.get("ticket_type"),
                multipliers={
                    "gate_severity_size_multiplier": gate_severity_multiplier,
                }
                if gate_severity_multiplier < 1.0
                else {},
            )
        intent = {
            "intent_id": intent_id,
            "dedupe_key_sha256": hashlib.sha256(dedupe_key.encode("utf-8")).hexdigest(),
            "ledger_run_id": run_id,
            "ledger_sequence": sequence,
            "state": state,
            "created_at": datetime.now(UTC).isoformat(),
            "symbol": item.get("symbol"),
            "ticket_type": item.get("ticket_type"),
            "action": item.get("action"),
            "quantity": quantity,
            "original_quantity": item.get("quantity"),
            "portfolio_risk_gate_applied": risk_gate_applied,
            "portfolio_risk_gate_snapshot": portfolio_risk_gate,
            "evidence_preflight_gate_applied": evidence_gate_applied,
            "evidence_preflight_action": evidence_action or None,
            "evidence_entry_size_multiplier": evidence_multiplier,
            "evidence_preflight_snapshot": item.get("evidence_preflight"),
            "market_data_risk_gate_applied": market_data_gate_applied,
            "market_data_risk_gate_action": market_data_action or None,
            "market_data_entry_size_multiplier": market_data_multiplier,
            "market_data_risk_gate_snapshot": item.get("market_data_risk_gate"),
            "gate_severity_policy_applied": gate_severity_applied,
            "gate_severity_posture": gate_severity_posture or None,
            "gate_severity_size_multiplier": gate_severity_multiplier,
            "gate_severity_confidence_multiplier": gate_severity_confidence_multiplier,
            "gate_severity_policy_snapshot": item.get("gate_severity_policy") or item.get("strategy_allocation_recommendation"),
            "final_entry_size_resolution": final_size_resolution,
            "capital_aware_gate_applied": capital_aware_applied,
            "capital_gate_status": item.get("capital_gate_status"),
            "capital_quantity_scale": item.get("capital_quantity_scale"),
            "capital_adjusted_quantity": item.get("capital_adjusted_quantity"),
            "capital_block_reasons": item.get("capital_block_reasons") or [],
            "capital_requirements": item.get("capital_requirements") or {},
            "capital_remaining_before": item.get("capital_remaining_before") or {},
            "capital_remaining_after": item.get("capital_remaining_after") or {},
            "capital_aware_gate_snapshot": capital_aware_gate if source_has_capital_aware_gate else None,
            "session_transition_release_governor_applied": release_governor_applied,
            "session_transition_release_governor_exempt": release_governor_exempt,
            "session_transition_release_governor_snapshot": release_governor if release_governor.get("configured") else None,
            "source_score_id": source.get("source_score_id"),
            "source_outcome_id": source.get("source_outcome_id"),
            "contract_preview": contract,
            "order_preview": order_for_intent,
            "local_margin_estimate": margin_for_intent,
            "gate_snapshot": {
                "autonomous_execution_gate_version": auto_gate.get("autonomous_execution_gate_version"),
                "gate_parameters": auto_gate.get("gate_parameters"),
                "checks": item.get("checks"),
                "used_budget": auto_gate.get("used_budget"),
                "evidence_preflight": auto_gate.get("evidence_preflight"),
                "market_data_risk_gate": auto_gate.get("market_data_risk_gate"),
                "capital_aware_gate": capital_aware_gate if source_has_capital_aware_gate else None,
                "session_transition_release_governor": release_governor if release_governor.get("configured") else None,
            },
            "state_history": [
                {
                    "state": state,
                    "at": datetime.now(UTC).isoformat(),
                    "reason": state_reason,
                }
            ],
            "safety": {
                "paper_intent_only": True,
                "ibkr_order_submitted": False,
                "transmit": False,
                "live_order_routing_enabled": False,
            },
        }
        intents.append(intent)
        status_counts[state] += 1
        action_counts[str(intent.get("action") or "unknown")] += 1
        symbol_counts[str(intent.get("symbol") or "UNKNOWN")] += 1
    skipped_ready = max(0, len(ready_items) - len(intents))
    return {
        "execution_intent_ledger_version": "paper-execution-intent-ledger-v2-portfolio-risk-gated",
        "generated_at": datetime.now(UTC).isoformat(),
        "ledger_run_id": run_id,
        "source_autonomous_execution_gate_version": auto_gate.get("autonomous_execution_gate_version"),
        "require_capital_aware_gate": bool(require_capital_aware_gate),
        "source_has_capital_aware_gate": bool(source_has_capital_aware_gate),
        "capital_aware_gate_applied": bool(source_has_capital_aware_gate),
        "capital_aware_gate": capital_aware_gate,
        "source_capital_ready_count": auto_gate.get("capital_ready_count"),
        "source_capital_resized_count": auto_gate.get("capital_resized_count"),
        "source_capital_blocked_count": auto_gate.get("capital_blocked_count"),
        "blocked_by_missing_capital_aware_gate_count": len(source_items) if require_capital_aware_gate and not source_has_capital_aware_gate else 0,
        "source_auto_ready_count": len(ready_items),
        "intent_count": len(intents),
        "pending_paper_execution_count": sum(1 for intent in intents if intent.get("state") == "pending_paper_execution"),
        "blocked_by_portfolio_risk_state_count": sum(1 for intent in intents if intent.get("state") == "blocked_by_portfolio_risk_state"),
        "blocked_by_session_transition_release_governor_count": sum(1 for intent in intents if intent.get("state") == "blocked_by_session_transition_release_governor"),
        "skipped_ready_count_due_to_max_intents": skipped_ready,
        "intent_state_counts": dict(status_counts),
        "action_counts": dict(action_counts),
        "symbol_counts": dict(symbol_counts),
        "existing_intent_count_seen": len(existing_ids),
        "portfolio_risk_gate": portfolio_risk_gate,
        "session_transition_release_governor": release_governor,
        "evidence_preflight": auto_gate.get("evidence_preflight"),
            "market_data_risk_gate": auto_gate.get("market_data_risk_gate"),
            "capital_aware_gate": capital_aware_gate,
            "intents": intents,
        "sample_intents": intents[: max(0, int(sample_limit))],
        "safety": {
            "mode": "paper_execution_intent_ledger",
            "policy": "Creates durable paper execution intents from capital-aware auto-ready gate items after optional portfolio, evidence, market-data, and session gates. No broker API call, order submission, transmit, or portfolio mutation.",
            "ibkr_order_submitted": False,
            "transmit_enabled": False,
            "live_order_routing_enabled": False,
        },
    }


def execution_intent_ledger_markdown(
    result: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
    title: str = "Paper Execution Intent Ledger",
) -> str:
    filters = filters or {}
    portfolio_risk_gate = result.get("portfolio_risk_gate") or {}
    release_governor = result.get("session_transition_release_governor") or {}
    capital_aware_gate = result.get("capital_aware_gate") or {}
    lines = [
        f"# {title}",
        "",
        f"- Generated at: `{result.get('generated_at')}`",
        f"- Ledger run ID: `{result.get('ledger_run_id')}`",
        f"- Capital-aware gate: required `{result.get('require_capital_aware_gate')}` source_has `{result.get('source_has_capital_aware_gate')}` applied `{result.get('capital_aware_gate_applied')}`",
        f"- Capital-ready/resized/blocked source counts: `{capital_aware_gate.get('capital_ready_count')}` / `{capital_aware_gate.get('capital_resized_count')}` / `{capital_aware_gate.get('capital_blocked_count')}`",
        f"- Source auto-ready count: `{result.get('source_auto_ready_count')}`",
        f"- Intent count: `{result.get('intent_count')}`",
        f"- Pending paper execution count: `{result.get('pending_paper_execution_count')}`",
        f"- Portfolio-risk blocked count: `{result.get('blocked_by_portfolio_risk_state_count')}`",
        f"- Session-transition release blocked count: `{result.get('blocked_by_session_transition_release_governor_count')}`",
        f"- Existing intent count seen: `{result.get('existing_intent_count_seen')}`",
        f"- Skipped ready due to max intents: `{result.get('skipped_ready_count_due_to_max_intents')}`",
        f"- Portfolio risk gate: `{portfolio_risk_gate.get('action') or 'not_configured'}`",
        f"- Portfolio risk state: `{portfolio_risk_gate.get('portfolio_risk_state') or '-'}`",
        f"- Session-transition release governor: `{release_governor.get('release_status') or 'not_configured'}` ordinary alpha `{release_governor.get('ordinary_alpha_release_allowed')}` size `{release_governor.get('max_new_entry_size_multiplier')}`",
        f"- Evidence preflight: `{(result.get('evidence_preflight') or {}).get('gate_status') or 'not_configured'}` / action `{(result.get('evidence_preflight') or {}).get('action') or '-'}`",
        "- Safety: paper intent ledger only; no broker order API call or transmit.",
        "",
        "## Inputs",
        "",
    ]
    if filters:
        for key, value in filters.items():
            lines.append(f"- `{key}`: `{value if value is not None else '-'}`")
    else:
        lines.append("- none")
    for title_text, counts in (
        ("Intent state counts", result.get("intent_state_counts") or {}),
        ("Action counts", result.get("action_counts") or {}),
        ("Symbol counts", result.get("symbol_counts") or {}),
    ):
        lines.extend(["", f"## {title_text}", "", "| Bucket | Count |", "| --- | ---: |"])
        if counts:
            for key, value in sorted(counts.items(), key=lambda item: (-int(item[1]), item[0])):
                lines.append(f"| {key} | {value} |")
        else:
            lines.append("| - | 0 |")
    lines.extend(
        [
            "",
            "## Sample intents",
            "",
            "| Intent ID | State | Symbol | Action | Quantity | Risk gate | Source score |",
            "| --- | --- | --- | --- | ---: | --- | ---: |",
        ]
    )
    for item in result.get("sample_intents") or []:
        gate = item.get("portfolio_risk_gate_snapshot") or {}
        lines.append(
            f"| {item.get('intent_id')} | {item.get('state')} | {item.get('symbol')} | {item.get('action')} | {item.get('quantity')} | {gate.get('action') or '-'} | {item.get('source_score_id')} |"
        )
    lines.extend(
        [
            "",
            "## Interpretation note",
            "",
            "M124 extends the M114 ledger by applying the optional M123 portfolio risk-state gate before a paper router can consume new entry intents. `reduce_risk` blocks new paper entries, `monitor` can size-reduce them, and `within_limits` allows normal pending paper execution.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_execution_intent_ledger_markdown(
    output_path: Path,
    result: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(execution_intent_ledger_markdown(result, filters=filters), encoding="utf-8")
    return output_path


def _intent_ledger_payload_result(payload: dict[str, Any]) -> dict[str, Any]:
    return payload.get("result") or payload


def _existing_paper_order_intent_ids(existing_router_payload: dict[str, Any] | None) -> set[str]:
    if not existing_router_payload:
        return set()
    router = existing_router_payload.get("result") or existing_router_payload
    return {
        str(order.get("intent_id"))
        for order in router.get("paper_orders") or []
        if order.get("intent_id")
    }


def _paper_execution_notional(intent: dict[str, Any]) -> float | None:
    quantity = _float_or_none(intent.get("quantity"))
    if quantity is None or quantity <= 0:
        return None
    margin = intent.get("local_margin_estimate") or {}
    action = str(intent.get("action") or "")
    cash_impact = _float_or_none(margin.get("estimated_cash_impact"))
    initial_margin = _float_or_none(margin.get("estimated_initial_margin_change"))
    method = str(margin.get("estimate_method") or "")
    if action == "BUY" and cash_impact is not None and cash_impact < 0:
        return abs(cash_impact)
    if action == "SELL_SHORT" and initial_margin is not None:
        if method == "local_short_stock_conservative_v1":
            return max(0.0, initial_margin / 1.5)
        return max(0.0, initial_margin)
    if initial_margin is not None:
        return max(0.0, initial_margin)
    return None


def _paper_side_from_action(action: str) -> str:
    if action == "BUY":
        return "long"
    if action == "SELL_SHORT":
        return "short"
    return "unknown"


def _pending_intents_for_router(ledger: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        intent
        for intent in ledger.get("intents") or []
        if str(intent.get("state") or "") == "pending_paper_execution"
    ]


def _paper_order_router_result_payload(
    *,
    ledger: dict[str, Any],
    run_id: str,
    source_portfolio_risk_gate: dict[str, Any],
    source_has_portfolio_risk_gate: bool,
    require_portfolio_risk_gate: bool,
    source_has_capital_aware_gate: bool,
    require_capital_aware_gate: bool,
    require_operating_preflight: bool,
    operating_final_preflight: dict[str, Any] | None,
    session_transition_release_governor: dict[str, Any],
    require_session_transition_release_governor: bool,
    fill_price_source: str,
    marks: dict[str, dict[str, Any]],
    paper_orders: list[dict[str, Any]],
    fills: list[dict[str, Any]],
    position_rows: list[dict[str, Any]],
    status_counts: Counter[str],
    action_counts: Counter[str],
    skipped_counts: Counter[str],
    fill_reference_counts: Counter[str],
    sample_limit: int,
) -> dict[str, Any]:
    totals = {
        "paper_order_count": len(paper_orders),
        "paper_fill_count": len(fills),
        "gross_fill_notional": sum(float(fill.get("fill_notional") or 0.0) for fill in fills),
        "commission": sum(float(fill.get("commission") or 0.0) for fill in fills),
        "long_notional": sum(float(fill.get("fill_notional") or 0.0) for fill in fills if fill.get("side") == "long"),
        "short_notional": sum(float(fill.get("fill_notional") or 0.0) for fill in fills if fill.get("side") == "short"),
        "paper_unrealized_pnl": 0.0,
    }
    preflight_applied = operating_final_preflight is not None
    return {
        "paper_order_router_version": "paper-order-router-sim-fill-v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "router_run_id": run_id,
        "source_execution_intent_ledger_version": ledger.get("execution_intent_ledger_version"),
        "source_intent_count": ledger.get("intent_count"),
        "paper_order_count": len(paper_orders),
        "paper_fill_count": len(fills),
        "paper_position_count": len(position_rows),
        "order_state_counts": dict(status_counts),
        "action_counts": dict(action_counts),
        "skipped_counts": dict(skipped_counts),
        "fill_reference_counts": dict(fill_reference_counts),
        "fill_price_source": fill_price_source,
        "input_mark_count": len(marks),
        "require_portfolio_risk_gate": bool(require_portfolio_risk_gate),
        "source_has_portfolio_risk_gate": source_has_portfolio_risk_gate,
        "source_portfolio_risk_gate": source_portfolio_risk_gate,
        "require_capital_aware_gate": bool(require_capital_aware_gate),
        "source_has_capital_aware_gate": bool(source_has_capital_aware_gate),
        "source_capital_aware_gate": ledger.get("capital_aware_gate") or {},
        "require_operating_preflight": bool(require_operating_preflight),
        "operating_final_preflight_applied": preflight_applied,
        "operating_final_preflight_allowed": operating_final_preflight.get("allowed") if operating_final_preflight else None,
        "operating_final_preflight_action": operating_final_preflight.get("action") if operating_final_preflight else None,
        "operating_final_preflight_reasons": operating_final_preflight.get("reasons") if operating_final_preflight else [],
        "operating_final_preflight": operating_final_preflight,
        "require_session_transition_release_governor": bool(require_session_transition_release_governor),
        "session_transition_release_governor": session_transition_release_governor,
        "session_transition_release_governor_applied": bool(session_transition_release_governor.get("configured")),
        "session_transition_release_governor_allowed": session_transition_release_governor.get("ordinary_alpha_release_allowed"),
        "session_transition_release_governor_action": session_transition_release_governor.get("release_status"),
        "totals": totals,
        "paper_orders": paper_orders,
        "paper_fills": fills,
        "paper_positions": position_rows,
        "sample_paper_orders": paper_orders[: max(0, int(sample_limit))],
        "sample_paper_positions": position_rows[: max(0, int(sample_limit))],
        "safety": {
            "mode": "paper_order_router_simulated_fills",
            "policy": "Consumes pending paper execution intents and creates simulated paper orders, fills, and positions only after optional M184 operating final preflight. No broker API call, order submission, transmit, or real portfolio mutation.",
            "operating_final_preflight_applied": preflight_applied,
            "capital_aware_gate_required": bool(require_capital_aware_gate),
            "capital_aware_gate_applied": bool(source_has_capital_aware_gate),
            "session_transition_release_governor_applied": bool(session_transition_release_governor.get("configured")),
            "ibkr_order_submitted": False,
            "transmit_enabled": False,
            "live_order_routing_enabled": False,
        },
    }


def _paper_router_position_rows(positions: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    position_rows = []
    for row in positions.values():
        net_quantity = float(row.get("long_quantity") or 0.0) - float(row.get("short_quantity") or 0.0)
        long_avg = (row["long_cost_basis"] / row["long_quantity"]) if row["long_quantity"] else None
        short_avg = (row["short_proceeds"] / row["short_quantity"]) if row["short_quantity"] else None
        position_rows.append(
            {
                **row,
                "net_quantity": net_quantity,
                "long_avg_price": long_avg,
                "short_avg_price": short_avg,
                "paper_unrealized_pnl": 0.0,
                "valuation_note": "No market mark applied in M115; positions are opened at simulated fill price and PnL starts at zero before later mark-to-market.",
            }
        )
    return position_rows


def _paper_router_process_intents(
    *,
    ledger: dict[str, Any],
    existing_intent_ids: set[str],
    marks: dict[str, dict[str, Any]],
    fill_price_source: str,
    fill_model: str,
    require_capital_aware_gate: bool,
    require_portfolio_risk_gate: bool,
    max_orders: int,
    slippage_bps: float,
    commission_per_order: float,
    run_id: str,
    source_has_portfolio_risk_gate: bool,
    release_governor: dict[str, Any],
    status_counts: Counter[str],
    action_counts: Counter[str],
    skipped_counts: Counter[str],
    fill_reference_counts: Counter[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, dict[str, Any]]]:
    paper_orders: list[dict[str, Any]] = []
    fills: list[dict[str, Any]] = []
    positions: dict[str, dict[str, Any]] = {}
    for sequence, intent in enumerate(ledger.get("intents") or [], start=1):
        if len(paper_orders) >= int(max_orders):
            skipped_counts["max_orders_reached"] += 1
            continue
        intent_id = str(intent.get("intent_id") or "")
        state = str(intent.get("state") or "")
        if state != "pending_paper_execution":
            skipped_counts[f"intent_state:{state or 'unknown'}"] += 1
            continue
        if require_capital_aware_gate:
            capital_gate_status = str(intent.get("capital_gate_status") or "")
            if not intent.get("capital_aware_gate_applied") or capital_gate_status not in CAPITAL_AWARE_READY_STATUSES:
                skipped_counts[f"capital_aware_gate_status:{capital_gate_status or 'missing'}"] += 1
                continue
        intent_portfolio_risk_gate = intent.get("portfolio_risk_gate_snapshot") or {}
        intent_gate_action = str(intent_portfolio_risk_gate.get("action") or "")
        if require_portfolio_risk_gate:
            if not source_has_portfolio_risk_gate or not intent_portfolio_risk_gate:
                skipped_counts["missing_required_portfolio_risk_gate"] += 1
                continue
            if intent_gate_action not in {"allow_new_entries", "allow_with_monitor_haircut"}:
                skipped_counts[f"portfolio_risk_gate_action:{intent_gate_action or 'unknown'}"] += 1
                continue
        if intent_id in existing_intent_ids:
            skipped_counts["intent_already_paper_routed"] += 1
            continue
        release_exempt = _is_release_governor_exempt_intent(intent)
        if release_governor.get("configured") and not release_exempt:
            if not intent.get("session_transition_release_governor_snapshot"):
                skipped_counts["missing_session_transition_release_governor_on_intent"] += 1
                continue
            if not release_governor.get("ordinary_alpha_release_allowed"):
                skipped_counts["session_transition_release_governor_blocked_ordinary_alpha"] += 1
                continue
        quantity = _float_or_none(intent.get("quantity"))
        notional = _paper_execution_notional(intent)
        if quantity is None or quantity <= 0 or notional is None or notional <= 0:
            skipped_counts["missing_quantity_or_notional"] += 1
            continue
        action = str(intent.get("action") or "")
        side = _paper_side_from_action(action)
        if side == "unknown":
            skipped_counts[f"unsupported_action:{action or 'unknown'}"] += 1
            continue
        symbol = str(intent.get("symbol") or "UNKNOWN").upper()
        mark = marks.get(symbol)
        local_estimate_price = notional / quantity
        base_price = local_estimate_price
        reference_source = "local_estimate"
        reference_mark: dict[str, Any] | None = None
        if fill_price_source in {"marks_or_local_estimate", "require_marks"}:
            if mark and _float_or_none(mark.get("mark_price")) is not None:
                base_price = float(mark["mark_price"])
                reference_source = str(mark.get("mark_source") or "marks_json")
                reference_mark = mark
            elif fill_price_source == "require_marks":
                skipped_counts["missing_required_fill_mark"] += 1
                continue
        slippage_multiplier = 1.0 + (float(slippage_bps) / 10000.0)
        if side == "short":
            slippage_multiplier = 1.0 - (float(slippage_bps) / 10000.0)
        fill_price = round(max(0.000001, base_price * slippage_multiplier), 6)
        fill_notional = round(fill_price * quantity, 6)
        paper_order_id = "paper_order_" + hashlib.sha256(f"{run_id}:{intent_id}".encode("utf-8")).hexdigest()[:20]
        fill_id = "paper_fill_" + hashlib.sha256(f"{paper_order_id}:fill".encode("utf-8")).hexdigest()[:20]
        submitted_at = datetime.now(UTC).isoformat()
        order = {
            "paper_order_id": paper_order_id,
            "intent_id": intent_id,
            "router_run_id": run_id,
            "router_sequence": len(paper_orders) + 1,
            "state": "paper_filled",
            "submitted_at": submitted_at,
            "filled_at": submitted_at,
            "symbol": intent.get("symbol"),
            "action": action,
            "side": side,
            "quantity": quantity,
            "order_type": (intent.get("order_preview") or {}).get("order_type"),
            "fill_model": fill_model,
            "fill_price_source": fill_price_source,
            "reference_source": reference_source,
            "reference_mark": reference_mark,
            "estimated_reference_notional": notional,
            "local_estimate_price": round(local_estimate_price, 6),
            "reference_price": round(base_price, 6),
            "fill_price": fill_price,
            "fill_notional": fill_notional,
            "commission": float(commission_per_order),
            "portfolio_risk_gate_snapshot": intent_portfolio_risk_gate,
            "capital_aware_gate_snapshot": intent.get("capital_aware_gate_snapshot"),
            "capital_gate_status": intent.get("capital_gate_status"),
            "capital_requirements": intent.get("capital_requirements") or {},
            "capital_quantity_scale": intent.get("capital_quantity_scale"),
            "session_transition_release_governor_snapshot": intent.get("session_transition_release_governor_snapshot"),
            "source_intent": intent,
            "safety": {
                "paper_order_only": True,
                "ibkr_order_submitted": False,
                "transmit": False,
            },
        }
        fill = {
            "paper_fill_id": fill_id,
            "paper_order_id": paper_order_id,
            "intent_id": intent_id,
            "symbol": intent.get("symbol"),
            "side": side,
            "quantity": quantity,
            "fill_price": fill_price,
            "fill_notional": fill_notional,
            "commission": float(commission_per_order),
            "filled_at": submitted_at,
            "fill_model": fill_model,
            "fill_price_source": fill_price_source,
            "reference_source": reference_source,
        }
        position = positions.setdefault(
            symbol,
            {
                "symbol": symbol,
                "long_quantity": 0.0,
                "short_quantity": 0.0,
                "long_cost_basis": 0.0,
                "short_proceeds": 0.0,
                "commission": 0.0,
                "fills": [],
            },
        )
        if side == "long":
            position["long_quantity"] += quantity
            position["long_cost_basis"] += fill_notional
        elif side == "short":
            position["short_quantity"] += quantity
            position["short_proceeds"] += fill_notional
        position["commission"] += float(commission_per_order)
        position["fills"].append(fill_id)
        paper_orders.append(order)
        fills.append(fill)
        status_counts["paper_filled"] += 1
        action_counts[action] += 1
        fill_reference_counts[reference_source] += 1
    return paper_orders, fills, positions


def build_paper_order_router_report(
    intent_ledger_payload: dict[str, Any],
    *,
    existing_router_payload: dict[str, Any] | None = None,
    marks_payload: dict[str, Any] | None = None,
    operating_auto_gate_payload: dict[str, Any] | None = None,
    session_transition_release_governor_payload: dict[str, Any] | None = None,
    require_operating_preflight: bool = False,
    allow_non_operating_source_ledger: bool = False,
    require_session_transition_release_governor: bool = False,
    fill_price_source: str = "local_estimate",
    fill_model: str = "immediate_local_estimate_fill_v1",
    require_capital_aware_gate: bool = False,
    require_portfolio_risk_gate: bool = False,
    max_orders: int = 10,
    slippage_bps: float = 0.0,
    commission_per_order: float = 0.0,
    router_run_id: str | None = None,
    sample_limit: int = 20,
) -> dict[str, Any]:
    ledger = _intent_ledger_payload_result(intent_ledger_payload)
    existing_intent_ids = _existing_paper_order_intent_ids(existing_router_payload)
    marks = paper_mark_prices_from_payload(marks_payload)
    fill_price_source = str(fill_price_source or "local_estimate")
    if fill_price_source not in {"local_estimate", "marks_or_local_estimate", "require_marks"}:
        raise ValueError("fill_price_source must be local_estimate, marks_or_local_estimate, or require_marks")
    run_id = router_run_id or datetime.now(UTC).strftime("paper_router_%Y%m%dT%H%M%SZ")
    status_counts: Counter[str] = Counter()
    action_counts: Counter[str] = Counter()
    skipped_counts: Counter[str] = Counter()
    fill_reference_counts: Counter[str] = Counter()
    source_portfolio_risk_gate = ledger.get("portfolio_risk_gate") or {}
    source_has_portfolio_risk_gate = bool(source_portfolio_risk_gate)
    source_has_capital_aware_gate = bool(
        ledger.get("capital_aware_gate_applied")
        or ledger.get("source_has_capital_aware_gate")
        or any(intent.get("capital_aware_gate_applied") for intent in ledger.get("intents") or [])
    )
    release_governor = _session_transition_release_governor_snapshot(session_transition_release_governor_payload)
    if require_capital_aware_gate and not source_has_capital_aware_gate:
        pending_count = len(_pending_intents_for_router(ledger))
        if pending_count:
            skipped_counts["missing_required_capital_aware_gate"] += pending_count
        return _paper_order_router_result_payload(
            ledger=ledger,
            run_id=run_id,
            source_portfolio_risk_gate=source_portfolio_risk_gate,
            source_has_portfolio_risk_gate=source_has_portfolio_risk_gate,
            require_portfolio_risk_gate=require_portfolio_risk_gate,
            source_has_capital_aware_gate=source_has_capital_aware_gate,
            require_capital_aware_gate=require_capital_aware_gate,
            require_operating_preflight=require_operating_preflight,
            operating_final_preflight=None,
            session_transition_release_governor=release_governor,
            require_session_transition_release_governor=require_session_transition_release_governor,
            fill_price_source=fill_price_source,
            marks=marks,
            paper_orders=[],
            fills=[],
            position_rows=[],
            status_counts=status_counts,
            action_counts=action_counts,
            skipped_counts=skipped_counts,
            fill_reference_counts=fill_reference_counts,
            sample_limit=sample_limit,
        )
    if require_session_transition_release_governor and not release_governor.get("configured"):
        pending_count = len(_pending_intents_for_router(ledger))
        if pending_count:
            skipped_counts["missing_required_session_transition_release_governor"] += pending_count
        return _paper_order_router_result_payload(
            ledger=ledger,
            run_id=run_id,
            source_portfolio_risk_gate=source_portfolio_risk_gate,
            source_has_portfolio_risk_gate=source_has_portfolio_risk_gate,
            require_portfolio_risk_gate=require_portfolio_risk_gate,
            source_has_capital_aware_gate=source_has_capital_aware_gate,
            require_capital_aware_gate=require_capital_aware_gate,
            require_operating_preflight=require_operating_preflight,
            operating_final_preflight=None,
            session_transition_release_governor=release_governor,
            require_session_transition_release_governor=require_session_transition_release_governor,
            fill_price_source=fill_price_source,
            marks=marks,
            paper_orders=[],
            fills=[],
            position_rows=[],
            status_counts=status_counts,
            action_counts=action_counts,
            skipped_counts=skipped_counts,
            fill_reference_counts=fill_reference_counts,
            sample_limit=sample_limit,
        )
    operating_final_preflight: dict[str, Any] | None = None
    if require_operating_preflight or operating_auto_gate_payload is not None:
        from recorder.operating_final_preflight import build_operating_final_preflight_report

        operating_final_preflight = build_operating_final_preflight_report(
            intent_ledger_payload,
            operating_auto_gate_payload,
            require_operating_gate=bool(require_operating_preflight),
            allow_non_operating_source_ledger=bool(allow_non_operating_source_ledger),
            sample_limit=sample_limit,
        )
        if not operating_final_preflight.get("allowed"):
            blocked_pending = len(_pending_intents_for_router(ledger))
            if blocked_pending:
                skipped_counts["operating_final_preflight_blocked"] += blocked_pending
            return _paper_order_router_result_payload(
                ledger=ledger,
                run_id=run_id,
                source_portfolio_risk_gate=source_portfolio_risk_gate,
                source_has_portfolio_risk_gate=source_has_portfolio_risk_gate,
                require_portfolio_risk_gate=require_portfolio_risk_gate,
                source_has_capital_aware_gate=source_has_capital_aware_gate,
                require_capital_aware_gate=require_capital_aware_gate,
                require_operating_preflight=require_operating_preflight,
                operating_final_preflight=operating_final_preflight,
                session_transition_release_governor=release_governor,
                require_session_transition_release_governor=require_session_transition_release_governor,
                fill_price_source=fill_price_source,
                marks=marks,
                paper_orders=[],
                fills=[],
                position_rows=[],
                status_counts=status_counts,
                action_counts=action_counts,
                skipped_counts=skipped_counts,
                fill_reference_counts=fill_reference_counts,
                sample_limit=sample_limit,
            )
    paper_orders, fills, positions = _paper_router_process_intents(
        ledger=ledger,
        existing_intent_ids=existing_intent_ids,
        marks=marks,
        fill_price_source=fill_price_source,
        fill_model=fill_model,
        require_capital_aware_gate=require_capital_aware_gate,
        require_portfolio_risk_gate=require_portfolio_risk_gate,
        max_orders=max_orders,
        slippage_bps=slippage_bps,
        commission_per_order=commission_per_order,
        run_id=run_id,
        source_has_portfolio_risk_gate=source_has_portfolio_risk_gate,
        release_governor=release_governor,
        status_counts=status_counts,
        action_counts=action_counts,
        skipped_counts=skipped_counts,
        fill_reference_counts=fill_reference_counts,
    )
    position_rows = _paper_router_position_rows(positions)
    return _paper_order_router_result_payload(
        ledger=ledger,
        run_id=run_id,
        source_portfolio_risk_gate=source_portfolio_risk_gate,
        source_has_portfolio_risk_gate=source_has_portfolio_risk_gate,
        require_portfolio_risk_gate=require_portfolio_risk_gate,
        source_has_capital_aware_gate=source_has_capital_aware_gate,
        require_capital_aware_gate=require_capital_aware_gate,
        require_operating_preflight=require_operating_preflight,
        operating_final_preflight=operating_final_preflight,
        session_transition_release_governor=release_governor,
        require_session_transition_release_governor=require_session_transition_release_governor,
        fill_price_source=fill_price_source,
        marks=marks,
        paper_orders=paper_orders,
        fills=fills,
        position_rows=position_rows,
        status_counts=status_counts,
        action_counts=action_counts,
        skipped_counts=skipped_counts,
        fill_reference_counts=fill_reference_counts,
        sample_limit=sample_limit,
    )


def paper_order_router_markdown(
    result: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
    title: str = "Paper Order Router / Simulated Fill Report",
) -> str:
    filters = filters or {}
    totals = result.get("totals") or {}
    lines = [
        f"# {title}",
        "",
        f"- Generated at: `{result.get('generated_at')}`",
        f"- Router run ID: `{result.get('router_run_id')}`",
        f"- Source intent count: `{result.get('source_intent_count')}`",
        f"- Fill price source mode: `{result.get('fill_price_source')}`",
        f"- Input marks: `{result.get('input_mark_count')}`",
        f"- Require capital-aware gate: `{result.get('require_capital_aware_gate')}`",
        f"- Source has capital-aware gate: `{result.get('source_has_capital_aware_gate')}`",
        f"- Require portfolio risk gate: `{result.get('require_portfolio_risk_gate')}`",
        f"- Source has portfolio risk gate: `{result.get('source_has_portfolio_risk_gate')}`",
        f"- Operating final preflight: applied `{result.get('operating_final_preflight_applied')}` allowed `{result.get('operating_final_preflight_allowed')}` action `{result.get('operating_final_preflight_action') or '-'}`",
        f"- Operating final preflight reasons: `{result.get('operating_final_preflight_reasons') or []}`",
        f"- Session-transition release governor: applied `{result.get('session_transition_release_governor_applied')}` allowed `{result.get('session_transition_release_governor_allowed')}` action `{result.get('session_transition_release_governor_action') or '-'}`",
        f"- Paper orders: `{result.get('paper_order_count')}`",
        f"- Paper fills: `{result.get('paper_fill_count')}`",
        f"- Paper positions: `{result.get('paper_position_count')}`",
        f"- Gross fill notional: `{_fmt_money(totals.get('gross_fill_notional'))}`",
        f"- Long notional: `{_fmt_money(totals.get('long_notional'))}`",
        f"- Short notional: `{_fmt_money(totals.get('short_notional'))}`",
        "- Safety: simulated paper fills only; no broker order API call or transmit.",
        "",
        "## Inputs",
        "",
    ]
    if filters:
        for key, value in filters.items():
            lines.append(f"- `{key}`: `{value if value is not None else '-'}`")
    else:
        lines.append("- none")
    for title_text, counts in (
        ("Order state counts", result.get("order_state_counts") or {}),
        ("Action counts", result.get("action_counts") or {}),
        ("Fill reference counts", result.get("fill_reference_counts") or {}),
        ("Skipped counts", result.get("skipped_counts") or {}),
    ):
        lines.extend(["", f"## {title_text}", "", "| Bucket | Count |", "| --- | ---: |"])
        if counts:
            for key, value in sorted(counts.items(), key=lambda item: (-int(item[1]), item[0])):
                lines.append(f"| {key} | {value} |")
        else:
            lines.append("| - | 0 |")
    lines.extend(
        [
            "",
            "## Sample paper orders",
            "",
            "| Paper order ID | Symbol | Action | Quantity | Ref source | Fill price | Fill notional |",
            "| --- | --- | --- | ---: | --- | ---: | ---: |",
        ]
    )
    for order in result.get("sample_paper_orders") or []:
        lines.append(
            f"| {order.get('paper_order_id')} | {order.get('symbol')} | {order.get('action')} | {order.get('quantity')} | {order.get('reference_source')} | {_fmt_money(order.get('fill_price'))} | {_fmt_money(order.get('fill_notional'))} |"
        )
    lines.extend(
        [
            "",
            "## Paper positions",
            "",
            "| Symbol | Net quantity | Long quantity | Short quantity | Long avg | Short avg |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for position in result.get("paper_positions") or []:
        lines.append(
            f"| {position.get('symbol')} | {position.get('net_quantity')} | {position.get('long_quantity')} | {position.get('short_quantity')} | {_fmt_money(position.get('long_avg_price'))} | {_fmt_money(position.get('short_avg_price'))} |"
        )
    lines.extend(
        [
            "",
            "## Interpretation note",
            "",
            "M125 promotes the M124 portfolio-risk-gated ledger as the standard entry path when `require_portfolio_risk_gate` is true. The router then consumes only pending paper execution intents that carry an allowing M123/M124 portfolio-risk gate snapshot.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_paper_order_router_markdown(
    output_path: Path,
    result: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(paper_order_router_markdown(result, filters=filters), encoding="utf-8")
    return output_path


def _paper_router_payload_result(payload: dict[str, Any]) -> dict[str, Any]:
    return payload.get("result") or payload


def paper_mark_prices_from_payload(payload: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not payload:
        return {}
    source_payload = payload.get("result") if isinstance(payload.get("result"), dict) else payload
    rows: list[Any]
    if isinstance(source_payload.get("marks"), list):
        rows = list(source_payload.get("marks") or [])
    elif isinstance(source_payload.get("mark_prices"), list):
        rows = list(source_payload.get("mark_prices") or [])
    elif isinstance(source_payload.get("prices"), list):
        rows = list(source_payload.get("prices") or [])
    else:
        rows = []
        for key, value in source_payload.items():
            if isinstance(value, (int, float, str)):
                rows.append({"symbol": key, "mark_price": value})
            elif isinstance(value, dict) and _float_or_none(value.get("mark_price") or value.get("price") or value.get("last")) is not None:
                item = dict(value)
                item.setdefault("symbol", key)
                rows.append(item)
    marks: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or row.get("ticker") or "").upper().strip()
        mark_price = _float_or_none(row.get("mark_price") or row.get("price") or row.get("last") or row.get("close"))
        if not symbol or mark_price is None or mark_price <= 0.0:
            continue
        marks[symbol] = {
            "symbol": symbol,
            "mark_price": mark_price,
            "mark_source": row.get("mark_source") or row.get("source") or "marks_json",
            "mark_timestamp": row.get("mark_timestamp") or row.get("timestamp") or row.get("as_of"),
            "raw_mark": row,
        }
    return marks


def build_paper_position_mark_report(
    router_payload: dict[str, Any],
    *,
    marks_payload: dict[str, Any] | None = None,
    inline_marks: dict[str, float] | None = None,
    take_profit_pct: float = 0.03,
    stop_loss_pct: float = 0.02,
    max_loss_dollars: float | None = None,
    mark_run_id: str | None = None,
    sample_limit: int = 20,
) -> dict[str, Any]:
    router = _paper_router_payload_result(router_payload)
    marks = paper_mark_prices_from_payload(marks_payload)
    for symbol, price in (inline_marks or {}).items():
        mark_price = _float_or_none(price)
        if mark_price is not None and mark_price > 0.0:
            marks[str(symbol).upper()] = {
                "symbol": str(symbol).upper(),
                "mark_price": mark_price,
                "mark_source": "inline_marks",
                "mark_timestamp": None,
                "raw_mark": {"symbol": str(symbol).upper(), "mark_price": mark_price},
            }
    run_id = mark_run_id or datetime.now(UTC).strftime("paper_mark_%Y%m%dT%H%M%SZ")
    marked_positions: list[dict[str, Any]] = []
    signal_counts: Counter[str] = Counter()
    missing_mark_count = 0
    total_long_market_value = 0.0
    total_short_market_value = 0.0
    total_unrealized_pnl = 0.0
    total_gross_basis = 0.0
    for position in router.get("paper_positions") or []:
        symbol = str(position.get("symbol") or "").upper()
        long_quantity = _float_or_none(position.get("long_quantity")) or 0.0
        short_quantity = _float_or_none(position.get("short_quantity")) or 0.0
        long_avg = _float_or_none(position.get("long_avg_price"))
        short_avg = _float_or_none(position.get("short_avg_price"))
        mark = marks.get(symbol)
        if not mark:
            missing_mark_count += 1
            row = {
                **position,
                "mark_status": "missing_mark",
                "mark_price": None,
                "mark_source": None,
                "long_market_value": None,
                "short_market_value": None,
                "paper_unrealized_pnl": None,
                "paper_unrealized_pnl_pct": None,
                "exit_signal": "mark_missing",
                "exit_reason": "No mark price was provided for this symbol.",
                "suggested_close_action": None,
                "suggested_close_quantity": 0.0,
            }
            marked_positions.append(row)
            signal_counts["mark_missing"] += 1
            continue
        mark_price = float(mark["mark_price"])
        long_basis = long_quantity * long_avg if long_avg is not None else 0.0
        short_basis = short_quantity * short_avg if short_avg is not None else 0.0
        long_market_value = long_quantity * mark_price
        short_market_value = short_quantity * mark_price
        long_pnl = (mark_price - long_avg) * long_quantity if long_avg is not None and long_quantity else 0.0
        short_pnl = (short_avg - mark_price) * short_quantity if short_avg is not None and short_quantity else 0.0
        unrealized_pnl = long_pnl + short_pnl
        gross_basis = long_basis + short_basis
        pnl_pct = unrealized_pnl / gross_basis if gross_basis > 0.0 else None
        total_long_market_value += long_market_value
        total_short_market_value += short_market_value
        total_unrealized_pnl += unrealized_pnl
        total_gross_basis += gross_basis
        net_quantity = long_quantity - short_quantity
        if net_quantity > 0:
            close_action = "SELL"
            close_quantity = net_quantity
        elif net_quantity < 0:
            close_action = "BUY_TO_COVER"
            close_quantity = abs(net_quantity)
        else:
            close_action = None
            close_quantity = 0.0
        exit_signal = "hold"
        exit_reason = "PnL is inside configured take-profit and stop-loss bands."
        if pnl_pct is not None and pnl_pct >= float(take_profit_pct):
            exit_signal = "take_profit"
            exit_reason = f"Unrealized PnL pct {pnl_pct:.4f} >= take-profit threshold {float(take_profit_pct):.4f}."
        elif pnl_pct is not None and pnl_pct <= -float(stop_loss_pct):
            exit_signal = "stop_loss"
            exit_reason = f"Unrealized PnL pct {pnl_pct:.4f} <= stop-loss threshold {-float(stop_loss_pct):.4f}."
        if max_loss_dollars is not None and unrealized_pnl <= -abs(float(max_loss_dollars)):
            exit_signal = "max_loss_dollars"
            exit_reason = f"Unrealized loss {unrealized_pnl:.2f} breached max loss {abs(float(max_loss_dollars)):.2f}."
        signal_counts[exit_signal] += 1
        marked_positions.append(
            {
                **position,
                "mark_status": "marked",
                "mark_price": mark_price,
                "mark_source": mark.get("mark_source"),
                "mark_timestamp": mark.get("mark_timestamp"),
                "long_market_value": long_market_value,
                "short_market_value": short_market_value,
                "long_unrealized_pnl": long_pnl,
                "short_unrealized_pnl": short_pnl,
                "paper_unrealized_pnl": unrealized_pnl,
                "paper_unrealized_pnl_pct": pnl_pct,
                "exit_signal": exit_signal,
                "exit_reason": exit_reason,
                "suggested_close_action": close_action,
                "suggested_close_quantity": close_quantity,
            }
        )
    return {
        "paper_position_mark_version": "paper-position-mark-to-market-v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "mark_run_id": run_id,
        "source_router_version": router.get("paper_order_router_version"),
        "source_router_run_id": router.get("router_run_id"),
        "source_position_count": len(router.get("paper_positions") or []),
        "marked_position_count": len([row for row in marked_positions if row.get("mark_status") == "marked"]),
        "missing_mark_count": missing_mark_count,
        "exit_signal_counts": dict(signal_counts),
        "thresholds": {
            "take_profit_pct": float(take_profit_pct),
            "stop_loss_pct": float(stop_loss_pct),
            "max_loss_dollars": max_loss_dollars,
        },
        "totals": {
            "long_market_value": total_long_market_value,
            "short_market_value": total_short_market_value,
            "gross_market_value": total_long_market_value + total_short_market_value,
            "gross_basis": total_gross_basis,
            "paper_unrealized_pnl": total_unrealized_pnl,
            "paper_unrealized_pnl_pct": total_unrealized_pnl / total_gross_basis if total_gross_basis > 0.0 else None,
        },
        "marked_positions": marked_positions,
        "sample_marked_positions": marked_positions[: max(0, int(sample_limit))],
        "safety": {
            "mode": "paper_position_mark_to_market",
            "policy": "Marks simulated paper positions and emits paper exit signals only. No broker API call, order submission, transmit, or real portfolio mutation.",
            "ibkr_order_submitted": False,
            "transmit_enabled": False,
            "live_order_routing_enabled": False,
        },
    }


def paper_position_mark_markdown(
    result: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
    title: str = "Paper Position Mark-to-Market / Exit Signal Report",
) -> str:
    filters = filters or {}
    totals = result.get("totals") or {}
    lines = [
        f"# {title}",
        "",
        f"- Generated at: `{result.get('generated_at')}`",
        f"- Mark run ID: `{result.get('mark_run_id')}`",
        f"- Source router run ID: `{result.get('source_router_run_id')}`",
        f"- Source positions: `{result.get('source_position_count')}`",
        f"- Marked positions: `{result.get('marked_position_count')}`",
        f"- Missing marks: `{result.get('missing_mark_count')}`",
        f"- Gross market value: `{_fmt_money(totals.get('gross_market_value'))}`",
        f"- Gross basis: `{_fmt_money(totals.get('gross_basis'))}`",
        f"- Unrealized PnL: `{_fmt_money(totals.get('paper_unrealized_pnl'))}`",
        f"- Unrealized PnL pct: `{_fmt_signed_pct(totals.get('paper_unrealized_pnl_pct'))}`",
        "- Safety: paper mark-to-market and exit signals only; no broker order API call or transmit.",
        "",
        "## Inputs",
        "",
    ]
    if filters:
        for key, value in filters.items():
            lines.append(f"- `{key}`: `{value if value is not None else '-'}`")
    else:
        lines.append("- none")
    lines.extend(["", "## Exit signal counts", "", "| Signal | Count |", "| --- | ---: |"])
    counts = result.get("exit_signal_counts") or {}
    if counts:
        for key, value in sorted(counts.items(), key=lambda item: (-int(item[1]), item[0])):
            lines.append(f"| {key} | {value} |")
    else:
        lines.append("| - | 0 |")
    lines.extend(
        [
            "",
            "## Marked positions",
            "",
            "| Symbol | Mark | Net qty | PnL | PnL pct | Exit signal | Close action |",
            "| --- | ---: | ---: | ---: | ---: | --- | --- |",
        ]
    )
    for row in result.get("sample_marked_positions") or []:
        close = row.get("suggested_close_action") or "-"
        if row.get("suggested_close_quantity"):
            close = f"{close} {row.get('suggested_close_quantity')}"
        lines.append(
            f"| {row.get('symbol')} | {_fmt_money(row.get('mark_price'))} | {row.get('net_quantity')} | {_fmt_money(row.get('paper_unrealized_pnl'))} | {_fmt_signed_pct(row.get('paper_unrealized_pnl_pct'))} | {row.get('exit_signal')} | {close} |"
        )
    lines.extend(
        [
            "",
            "## Interpretation note",
            "",
            "M116 converts M115 simulated paper fills into a marked paper position ledger with unrealized PnL and deterministic paper exit signals. These are advisory paper signals only; a later milestone can decide whether an exit intent ledger should consume them.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_paper_position_mark_markdown(
    output_path: Path,
    result: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(paper_position_mark_markdown(result, filters=filters), encoding="utf-8")
    return output_path


def _mark_snapshot_generated_at(payload: dict[str, Any]) -> Any:
    source_payload = payload.get("result") if isinstance(payload.get("result"), dict) else payload
    return source_payload.get("generated_at") or source_payload.get("as_of") or source_payload.get("timestamp")


def build_paper_position_mark_timeseries_report(
    router_payload: dict[str, Any],
    mark_snapshot_payloads: list[dict[str, Any]],
    *,
    take_profit_pct: float = 0.03,
    stop_loss_pct: float = 0.02,
    max_loss_dollars: float | None = None,
    series_run_id: str | None = None,
    sample_limit: int = 20,
) -> dict[str, Any]:
    router = _paper_router_payload_result(router_payload)
    run_id = series_run_id or datetime.now(UTC).strftime("paper_mark_series_%Y%m%dT%H%M%SZ")
    timeline: list[dict[str, Any]] = []
    position_ledger_rows: list[dict[str, Any]] = []
    pnl_values: list[float] = []
    signal_counts: Counter[str] = Counter()
    missing_mark_steps = 0
    peak_pnl: float | None = None
    max_drawdown = 0.0
    for sequence, marks_payload in enumerate(mark_snapshot_payloads, start=1):
        mark_report = build_paper_position_mark_report(
            router,
            marks_payload=marks_payload,
            take_profit_pct=take_profit_pct,
            stop_loss_pct=stop_loss_pct,
            max_loss_dollars=max_loss_dollars,
            mark_run_id=f"{run_id}_step_{sequence}",
            sample_limit=sample_limit,
        )
        mark_generated_at = _mark_snapshot_generated_at(marks_payload) or mark_report.get("generated_at")
        totals = mark_report.get("totals") or {}
        pnl = _float_or_none(totals.get("paper_unrealized_pnl"))
        if pnl is not None:
            pnl_values.append(pnl)
            peak_pnl = pnl if peak_pnl is None else max(peak_pnl, pnl)
            max_drawdown = max(max_drawdown, (peak_pnl or 0.0) - pnl)
        step_signal_counts = mark_report.get("exit_signal_counts") or {}
        signal_counts.update({str(key): int(value) for key, value in step_signal_counts.items()})
        if int(mark_report.get("missing_mark_count") or 0) > 0:
            missing_mark_steps += 1
        timeline.append(
            {
                "sequence": sequence,
                "mark_generated_at": mark_generated_at,
                "mark_report_run_id": mark_report.get("mark_run_id"),
                "marked_position_count": mark_report.get("marked_position_count"),
                "missing_mark_count": mark_report.get("missing_mark_count"),
                "exit_signal_counts": step_signal_counts,
                "totals": totals,
            }
        )
        for position in mark_report.get("marked_positions") or []:
            position_ledger_rows.append(
                {
                    "sequence": sequence,
                    "mark_generated_at": mark_generated_at,
                    "symbol": position.get("symbol"),
                    "net_quantity": position.get("net_quantity"),
                    "long_quantity": position.get("long_quantity"),
                    "short_quantity": position.get("short_quantity"),
                    "mark_price": position.get("mark_price"),
                    "mark_source": position.get("mark_source"),
                    "paper_unrealized_pnl": position.get("paper_unrealized_pnl"),
                    "paper_unrealized_pnl_pct": position.get("paper_unrealized_pnl_pct"),
                    "exit_signal": position.get("exit_signal"),
                    "suggested_close_action": position.get("suggested_close_action"),
                    "suggested_close_quantity": position.get("suggested_close_quantity"),
                }
            )
    first_pnl = pnl_values[0] if pnl_values else None
    final_pnl = pnl_values[-1] if pnl_values else None
    return {
        "paper_position_mark_timeseries_version": "paper-position-mark-timeseries-v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "series_run_id": run_id,
        "source_router_version": router.get("paper_order_router_version"),
        "source_router_run_id": router.get("router_run_id"),
        "source_position_count": len(router.get("paper_positions") or []),
        "snapshot_count": len(mark_snapshot_payloads),
        "ledger_row_count": len(position_ledger_rows),
        "missing_mark_steps": missing_mark_steps,
        "aggregate_exit_signal_counts": dict(signal_counts),
        "thresholds": {
            "take_profit_pct": float(take_profit_pct),
            "stop_loss_pct": float(stop_loss_pct),
            "max_loss_dollars": max_loss_dollars,
        },
        "pnl_summary": {
            "first_unrealized_pnl": first_pnl,
            "final_unrealized_pnl": final_pnl,
            "delta_unrealized_pnl": final_pnl - first_pnl if first_pnl is not None and final_pnl is not None else None,
            "min_unrealized_pnl": min(pnl_values) if pnl_values else None,
            "max_unrealized_pnl": max(pnl_values) if pnl_values else None,
            "max_drawdown_from_peak_pnl": max_drawdown if pnl_values else None,
        },
        "timeline": timeline,
        "position_ledger_rows": position_ledger_rows,
        "sample_position_ledger_rows": position_ledger_rows[: max(0, int(sample_limit))],
        "latest_positions": [
            row for row in position_ledger_rows if timeline and row.get("sequence") == timeline[-1].get("sequence")
        ],
        "safety": {
            "mode": "paper_position_mark_timeseries",
            "policy": "Builds a time-series ledger from simulated paper positions and normalized marks only. No broker API call, order submission, transmit, or real portfolio mutation.",
            "ibkr_order_submitted": False,
            "transmit_enabled": False,
            "live_order_routing_enabled": False,
        },
    }


def paper_position_mark_timeseries_markdown(
    result: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
    title: str = "Paper Position Mark-to-Market Time Series Ledger",
) -> str:
    filters = filters or {}
    pnl = result.get("pnl_summary") or {}
    lines = [
        f"# {title}",
        "",
        f"- Generated at: `{result.get('generated_at')}`",
        f"- Series run ID: `{result.get('series_run_id')}`",
        f"- Source router run ID: `{result.get('source_router_run_id')}`",
        f"- Source positions: `{result.get('source_position_count')}`",
        f"- Snapshots: `{result.get('snapshot_count')}`",
        f"- Ledger rows: `{result.get('ledger_row_count')}`",
        f"- Missing-mark steps: `{result.get('missing_mark_steps')}`",
        f"- First PnL: `{_fmt_money(pnl.get('first_unrealized_pnl'))}`",
        f"- Final PnL: `{_fmt_money(pnl.get('final_unrealized_pnl'))}`",
        f"- Delta PnL: `{_fmt_money(pnl.get('delta_unrealized_pnl'))}`",
        f"- Max drawdown from peak PnL: `{_fmt_money(pnl.get('max_drawdown_from_peak_pnl'))}`",
        "- Safety: paper time-series ledger only; no broker order API call or transmit.",
        "",
        "## Inputs",
        "",
    ]
    if filters:
        for key, value in filters.items():
            lines.append(f"- `{key}`: `{value if value is not None else '-'}`")
    else:
        lines.append("- none")
    lines.extend(["", "## Aggregate exit signal counts", "", "| Signal | Count |", "| --- | ---: |"])
    counts = result.get("aggregate_exit_signal_counts") or {}
    if counts:
        for key, value in sorted(counts.items(), key=lambda item: (-int(item[1]), item[0])):
            lines.append(f"| {key} | {value} |")
    else:
        lines.append("| - | 0 |")
    lines.extend(
        [
            "",
            "## Timeline",
            "",
            "| Step | Mark time | PnL | PnL pct | Exit signals |",
            "| ---: | --- | ---: | ---: | --- |",
        ]
    )
    for step in result.get("timeline") or []:
        totals = step.get("totals") or {}
        signals = ", ".join(f"{key}:{value}" for key, value in sorted((step.get("exit_signal_counts") or {}).items())) or "-"
        lines.append(
            f"| {step.get('sequence')} | {step.get('mark_generated_at')} | {_fmt_money(totals.get('paper_unrealized_pnl'))} | {_fmt_signed_pct(totals.get('paper_unrealized_pnl_pct'))} | {signals} |"
        )
    lines.extend(
        [
            "",
            "## Latest positions",
            "",
            "| Symbol | Mark | Net qty | PnL | PnL pct | Exit signal |",
            "| --- | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    for row in result.get("latest_positions") or []:
        lines.append(
            f"| {row.get('symbol')} | {_fmt_money(row.get('mark_price'))} | {row.get('net_quantity')} | {_fmt_money(row.get('paper_unrealized_pnl'))} | {_fmt_signed_pct(row.get('paper_unrealized_pnl_pct'))} | {row.get('exit_signal')} |"
        )
    lines.extend(
        [
            "",
            "## Interpretation note",
            "",
            "M119 turns repeated M116 mark-to-market evaluations into a time-series paper ledger. The ledger is still advisory and paper-only; later milestones can consume exit signals into a separate paper exit-intent ledger.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_paper_position_mark_timeseries_markdown(
    output_path: Path,
    result: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(paper_position_mark_timeseries_markdown(result, filters=filters), encoding="utf-8")
    return output_path


def _paper_mark_timeseries_payload_result(payload: dict[str, Any]) -> dict[str, Any]:
    return payload.get("result") or payload


def _existing_paper_exit_intent_ids(existing_exit_ledger_payload: dict[str, Any] | None) -> set[str]:
    if not existing_exit_ledger_payload:
        return set()
    ledger = existing_exit_ledger_payload.get("result") or existing_exit_ledger_payload
    return {
        str(intent.get("exit_intent_id"))
        for intent in ledger.get("exit_intents") or []
        if intent.get("exit_intent_id")
    }


def _paper_exit_intent_id(*, source_router_run_id: str, symbol: str, action: str, quantity: float, exit_signal: str) -> str:
    raw = f"{source_router_run_id}:{symbol.upper()}:{action}:{quantity:.8f}:{exit_signal}"
    return "paper_exit_intent_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def build_paper_exit_intent_ledger_report(
    timeseries_payload: dict[str, Any],
    *,
    existing_exit_ledger_payload: dict[str, Any] | None = None,
    exit_signals: tuple[str, ...] = ("take_profit", "stop_loss", "max_loss_dollars"),
    max_intents: int = 10,
    ledger_run_id: str | None = None,
    sample_limit: int = 20,
) -> dict[str, Any]:
    series = _paper_mark_timeseries_payload_result(timeseries_payload)
    existing_ids = _existing_paper_exit_intent_ids(existing_exit_ledger_payload)
    allowed_signals = {str(signal).strip() for signal in exit_signals if str(signal).strip()}
    run_id = ledger_run_id or datetime.now(UTC).strftime("paper_exit_ledger_%Y%m%dT%H%M%SZ")
    latest_positions = list(series.get("latest_positions") or [])
    exit_intents: list[dict[str, Any]] = []
    skipped_counts: Counter[str] = Counter()
    signal_counts: Counter[str] = Counter()
    action_counts: Counter[str] = Counter()
    symbol_counts: Counter[str] = Counter()
    source_router_run_id = str(series.get("source_router_run_id") or "")
    for position in latest_positions:
        if len(exit_intents) >= int(max_intents):
            skipped_counts["max_intents_reached"] += 1
            continue
        symbol = str(position.get("symbol") or "").upper()
        exit_signal = str(position.get("exit_signal") or "")
        if exit_signal not in allowed_signals:
            skipped_counts[f"exit_signal:{exit_signal or 'unknown'}"] += 1
            continue
        action = str(position.get("suggested_close_action") or "")
        quantity = _float_or_none(position.get("suggested_close_quantity"))
        if not symbol or not action or quantity is None or quantity <= 0.0:
            skipped_counts["missing_close_action_or_quantity"] += 1
            continue
        exit_intent_id = _paper_exit_intent_id(
            source_router_run_id=source_router_run_id,
            symbol=symbol,
            action=action,
            quantity=quantity,
            exit_signal=exit_signal,
        )
        state = "pending_paper_exit"
        if exit_intent_id in existing_ids:
            state = "duplicate_existing_exit_intent"
            skipped_counts["duplicate_existing_exit_intent"] += 1
        intent = {
            "exit_intent_id": exit_intent_id,
            "ledger_run_id": run_id,
            "state": state,
            "created_at": datetime.now(UTC).isoformat(),
            "source_timeseries_version": series.get("paper_position_mark_timeseries_version"),
            "source_series_run_id": series.get("series_run_id"),
            "source_router_run_id": source_router_run_id,
            "source_position_sequence": position.get("sequence"),
            "mark_generated_at": position.get("mark_generated_at"),
            "symbol": symbol,
            "exit_signal": exit_signal,
            "action": action,
            "quantity": quantity,
            "mark_price": position.get("mark_price"),
            "mark_source": position.get("mark_source"),
            "paper_unrealized_pnl": position.get("paper_unrealized_pnl"),
            "paper_unrealized_pnl_pct": position.get("paper_unrealized_pnl_pct"),
            "source_position": position,
            "order_preview": {
                "action": action,
                "total_quantity": quantity,
                "order_type": "MKT",
                "what_if": True,
                "transmit": False,
                "paper_exit_only": True,
            },
            "safety": {
                "paper_exit_intent_only": True,
                "ibkr_order_submitted": False,
                "transmit": False,
                "live_order_routing_enabled": False,
            },
        }
        exit_intents.append(intent)
        if state == "pending_paper_exit":
            signal_counts[exit_signal] += 1
            action_counts[action] += 1
            symbol_counts[symbol] += 1
    intent_state_counts = Counter(str(intent.get("state") or "unknown") for intent in exit_intents)
    return {
        "paper_exit_intent_ledger_version": "paper-exit-intent-ledger-v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "ledger_run_id": run_id,
        "source_timeseries_version": series.get("paper_position_mark_timeseries_version"),
        "source_series_run_id": series.get("series_run_id"),
        "source_router_run_id": source_router_run_id,
        "source_latest_position_count": len(latest_positions),
        "exit_signal_filter": sorted(allowed_signals),
        "exit_intent_count": len(exit_intents),
        "pending_exit_intent_count": sum(1 for intent in exit_intents if intent.get("state") == "pending_paper_exit"),
        "existing_exit_intent_count_seen": len(existing_ids),
        "intent_state_counts": dict(intent_state_counts),
        "exit_signal_counts": dict(signal_counts),
        "action_counts": dict(action_counts),
        "symbol_counts": dict(symbol_counts),
        "skipped_counts": dict(skipped_counts),
        "exit_intents": exit_intents,
        "sample_exit_intents": exit_intents[: max(0, int(sample_limit))],
        "safety": {
            "mode": "paper_exit_intent_ledger",
            "policy": "Records paper exit intents from latest paper mark-to-market exit signals only. No broker API call, order submission, transmit, or real portfolio mutation.",
            "ibkr_order_submitted": False,
            "transmit_enabled": False,
            "live_order_routing_enabled": False,
        },
    }


def paper_exit_intent_ledger_markdown(
    result: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
    title: str = "Paper Exit Intent Ledger",
) -> str:
    filters = filters or {}
    lines = [
        f"# {title}",
        "",
        f"- Generated at: `{result.get('generated_at')}`",
        f"- Ledger run ID: `{result.get('ledger_run_id')}`",
        f"- Source series run ID: `{result.get('source_series_run_id')}`",
        f"- Source router run ID: `{result.get('source_router_run_id')}`",
        f"- Source latest positions: `{result.get('source_latest_position_count')}`",
        f"- Exit intents: `{result.get('exit_intent_count')}`",
        f"- Pending paper exits: `{result.get('pending_exit_intent_count')}`",
        "- Safety: paper exit-intent ledger only; no broker order API call or transmit.",
        "",
        "## Inputs",
        "",
    ]
    if filters:
        for key, value in filters.items():
            lines.append(f"- `{key}`: `{value if value is not None else '-'}`")
    else:
        lines.append("- none")
    for title_text, counts in (
        ("Intent state counts", result.get("intent_state_counts") or {}),
        ("Exit signal counts", result.get("exit_signal_counts") or {}),
        ("Action counts", result.get("action_counts") or {}),
        ("Symbol counts", result.get("symbol_counts") or {}),
        ("Skipped counts", result.get("skipped_counts") or {}),
    ):
        lines.extend(["", f"## {title_text}", "", "| Bucket | Count |", "| --- | ---: |"])
        if counts:
            for key, value in sorted(counts.items(), key=lambda item: (-int(item[1]), item[0])):
                lines.append(f"| {key} | {value} |")
        else:
            lines.append("| - | 0 |")
    lines.extend(
        [
            "",
            "## Sample exit intents",
            "",
            "| Exit intent ID | State | Symbol | Signal | Action | Quantity | Mark | PnL |",
            "| --- | --- | --- | --- | --- | ---: | ---: | ---: |",
        ]
    )
    for intent in result.get("sample_exit_intents") or []:
        lines.append(
            f"| {intent.get('exit_intent_id')} | {intent.get('state')} | {intent.get('symbol')} | {intent.get('exit_signal')} | {intent.get('action')} | {intent.get('quantity')} | {_fmt_money(intent.get('mark_price'))} | {_fmt_money(intent.get('paper_unrealized_pnl'))} |"
        )
    if not result.get("sample_exit_intents"):
        lines.append("| - | - | - | - | - | - | - | - |")
    lines.extend(
        [
            "",
            "## Interpretation note",
            "",
            "M120 creates a paper-only exit intent ledger from the latest M119 mark-to-market positions. It is the audit trail before any paper exit router is introduced, and it deliberately does not close positions or submit orders.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_paper_exit_intent_ledger_markdown(
    output_path: Path,
    result: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(paper_exit_intent_ledger_markdown(result, filters=filters), encoding="utf-8")
    return output_path


def _paper_exit_ledger_payload_result(payload: dict[str, Any]) -> dict[str, Any]:
    return payload.get("result") or payload


def _existing_paper_exit_routed_intent_ids(existing_exit_router_payload: dict[str, Any] | None) -> set[str]:
    if not existing_exit_router_payload:
        return set()
    router = existing_exit_router_payload.get("result") or existing_exit_router_payload
    return {
        str(order.get("exit_intent_id"))
        for order in router.get("paper_exit_orders") or []
        if order.get("exit_intent_id")
    }


def _paper_exit_fill_side_adjustment(action: str, mark_price: float, slippage_bps: float) -> tuple[float, float]:
    bps = float(slippage_bps) / 10000.0
    if action == "SELL":
        fill_price = max(0.000001, mark_price * (1.0 - bps))
        pnl_adjustment_per_share = fill_price - mark_price
    elif action == "BUY_TO_COVER":
        fill_price = max(0.000001, mark_price * (1.0 + bps))
        pnl_adjustment_per_share = mark_price - fill_price
    else:
        fill_price = mark_price
        pnl_adjustment_per_share = 0.0
    return round(fill_price, 6), pnl_adjustment_per_share


def build_paper_exit_router_report(
    exit_ledger_payload: dict[str, Any],
    *,
    existing_exit_router_payload: dict[str, Any] | None = None,
    fill_model: str = "immediate_mark_close_fill_v1",
    max_exits: int = 10,
    slippage_bps: float = 0.0,
    commission_per_exit: float = 0.0,
    router_run_id: str | None = None,
    sample_limit: int = 20,
) -> dict[str, Any]:
    ledger = _paper_exit_ledger_payload_result(exit_ledger_payload)
    existing_ids = _existing_paper_exit_routed_intent_ids(existing_exit_router_payload)
    run_id = router_run_id or datetime.now(UTC).strftime("paper_exit_router_%Y%m%dT%H%M%SZ")
    exit_orders: list[dict[str, Any]] = []
    exit_fills: list[dict[str, Any]] = []
    realized_rows: list[dict[str, Any]] = []
    status_counts: Counter[str] = Counter()
    signal_counts: Counter[str] = Counter()
    action_counts: Counter[str] = Counter()
    symbol_counts: Counter[str] = Counter()
    skipped_counts: Counter[str] = Counter()
    for intent in ledger.get("exit_intents") or []:
        if len(exit_orders) >= int(max_exits):
            skipped_counts["max_exits_reached"] += 1
            continue
        intent_id = str(intent.get("exit_intent_id") or "")
        state = str(intent.get("state") or "")
        if state != "pending_paper_exit":
            skipped_counts[f"exit_intent_state:{state or 'unknown'}"] += 1
            continue
        if intent_id in existing_ids:
            skipped_counts["exit_intent_already_paper_routed"] += 1
            continue
        symbol = str(intent.get("symbol") or "").upper()
        action = str(intent.get("action") or "")
        quantity = _float_or_none(intent.get("quantity"))
        mark_price = _float_or_none(intent.get("mark_price"))
        mark_pnl = _float_or_none(intent.get("paper_unrealized_pnl")) or 0.0
        if not symbol or action not in {"SELL", "BUY_TO_COVER"} or quantity is None or quantity <= 0.0 or mark_price is None:
            skipped_counts["missing_close_action_quantity_or_mark"] += 1
            continue
        fill_price, pnl_adjustment_per_share = _paper_exit_fill_side_adjustment(action, mark_price, slippage_bps)
        fill_notional = round(fill_price * quantity, 6)
        slippage_pnl_adjustment = pnl_adjustment_per_share * quantity
        realized_pnl = mark_pnl + slippage_pnl_adjustment - float(commission_per_exit)
        paper_exit_order_id = "paper_exit_order_" + hashlib.sha256(f"{run_id}:{intent_id}".encode("utf-8")).hexdigest()[:20]
        paper_exit_fill_id = "paper_exit_fill_" + hashlib.sha256(f"{paper_exit_order_id}:fill".encode("utf-8")).hexdigest()[:20]
        filled_at = datetime.now(UTC).isoformat()
        order = {
            "paper_exit_order_id": paper_exit_order_id,
            "exit_intent_id": intent_id,
            "router_run_id": run_id,
            "state": "paper_exit_filled",
            "submitted_at": filled_at,
            "filled_at": filled_at,
            "symbol": symbol,
            "exit_signal": intent.get("exit_signal"),
            "action": action,
            "quantity": quantity,
            "order_type": (intent.get("order_preview") or {}).get("order_type") or "MKT",
            "fill_model": fill_model,
            "mark_price": mark_price,
            "fill_price": fill_price,
            "fill_notional": fill_notional,
            "commission": float(commission_per_exit),
            "estimated_mark_unrealized_pnl": mark_pnl,
            "slippage_pnl_adjustment": slippage_pnl_adjustment,
            "realized_pnl": realized_pnl,
            "source_exit_intent": intent,
            "safety": {
                "paper_exit_order_only": True,
                "ibkr_order_submitted": False,
                "transmit": False,
            },
        }
        fill = {
            "paper_exit_fill_id": paper_exit_fill_id,
            "paper_exit_order_id": paper_exit_order_id,
            "exit_intent_id": intent_id,
            "symbol": symbol,
            "action": action,
            "quantity": quantity,
            "fill_price": fill_price,
            "fill_notional": fill_notional,
            "commission": float(commission_per_exit),
            "filled_at": filled_at,
            "fill_model": fill_model,
        }
        realized_row = {
            "paper_exit_fill_id": paper_exit_fill_id,
            "exit_intent_id": intent_id,
            "symbol": symbol,
            "exit_signal": intent.get("exit_signal"),
            "action": action,
            "closed_quantity": quantity,
            "mark_price": mark_price,
            "fill_price": fill_price,
            "mark_unrealized_pnl": mark_pnl,
            "slippage_pnl_adjustment": slippage_pnl_adjustment,
            "commission": float(commission_per_exit),
            "realized_pnl": realized_pnl,
            "realized_at": filled_at,
        }
        exit_orders.append(order)
        exit_fills.append(fill)
        realized_rows.append(realized_row)
        status_counts["paper_exit_filled"] += 1
        signal_counts[str(intent.get("exit_signal") or "unknown")] += 1
        action_counts[action] += 1
        symbol_counts[symbol] += 1
    totals = {
        "paper_exit_order_count": len(exit_orders),
        "paper_exit_fill_count": len(exit_fills),
        "gross_exit_notional": sum(float(fill.get("fill_notional") or 0.0) for fill in exit_fills),
        "commission": sum(float(fill.get("commission") or 0.0) for fill in exit_fills),
        "mark_unrealized_pnl": sum(float(row.get("mark_unrealized_pnl") or 0.0) for row in realized_rows),
        "slippage_pnl_adjustment": sum(float(row.get("slippage_pnl_adjustment") or 0.0) for row in realized_rows),
        "realized_pnl": sum(float(row.get("realized_pnl") or 0.0) for row in realized_rows),
    }
    return {
        "paper_exit_router_version": "paper-exit-router-sim-close-fill-v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "router_run_id": run_id,
        "source_exit_ledger_version": ledger.get("paper_exit_intent_ledger_version"),
        "source_exit_ledger_run_id": ledger.get("ledger_run_id"),
        "source_exit_intent_count": ledger.get("exit_intent_count"),
        "paper_exit_order_count": len(exit_orders),
        "paper_exit_fill_count": len(exit_fills),
        "realized_pnl_row_count": len(realized_rows),
        "order_state_counts": dict(status_counts),
        "exit_signal_counts": dict(signal_counts),
        "action_counts": dict(action_counts),
        "symbol_counts": dict(symbol_counts),
        "skipped_counts": dict(skipped_counts),
        "totals": totals,
        "paper_exit_orders": exit_orders,
        "paper_exit_fills": exit_fills,
        "realized_pnl_ledger": realized_rows,
        "sample_paper_exit_orders": exit_orders[: max(0, int(sample_limit))],
        "sample_realized_pnl_rows": realized_rows[: max(0, int(sample_limit))],
        "safety": {
            "mode": "paper_exit_router_simulated_close_fills",
            "policy": "Consumes pending paper exit intents and creates simulated close fills plus realized PnL ledger rows only. No broker API call, order submission, transmit, or real portfolio mutation.",
            "ibkr_order_submitted": False,
            "transmit_enabled": False,
            "live_order_routing_enabled": False,
        },
    }


def paper_exit_router_markdown(
    result: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
    title: str = "Paper Exit Router / Simulated Close Fill Report",
) -> str:
    filters = filters or {}
    totals = result.get("totals") or {}
    lines = [
        f"# {title}",
        "",
        f"- Generated at: `{result.get('generated_at')}`",
        f"- Router run ID: `{result.get('router_run_id')}`",
        f"- Source exit ledger run ID: `{result.get('source_exit_ledger_run_id')}`",
        f"- Source exit intents: `{result.get('source_exit_intent_count')}`",
        f"- Paper exit orders: `{result.get('paper_exit_order_count')}`",
        f"- Paper exit fills: `{result.get('paper_exit_fill_count')}`",
        f"- Realized PnL rows: `{result.get('realized_pnl_row_count')}`",
        f"- Gross exit notional: `{_fmt_money(totals.get('gross_exit_notional'))}`",
        f"- Realized PnL: `{_fmt_money(totals.get('realized_pnl'))}`",
        "- Safety: simulated paper close fills only; no broker order API call or transmit.",
        "",
        "## Inputs",
        "",
    ]
    if filters:
        for key, value in filters.items():
            lines.append(f"- `{key}`: `{value if value is not None else '-'}`")
    else:
        lines.append("- none")
    for title_text, counts in (
        ("Order state counts", result.get("order_state_counts") or {}),
        ("Exit signal counts", result.get("exit_signal_counts") or {}),
        ("Action counts", result.get("action_counts") or {}),
        ("Symbol counts", result.get("symbol_counts") or {}),
        ("Skipped counts", result.get("skipped_counts") or {}),
    ):
        lines.extend(["", f"## {title_text}", "", "| Bucket | Count |", "| --- | ---: |"])
        if counts:
            for key, value in sorted(counts.items(), key=lambda item: (-int(item[1]), item[0])):
                lines.append(f"| {key} | {value} |")
        else:
            lines.append("| - | 0 |")
    lines.extend(
        [
            "",
            "## Realized PnL ledger sample",
            "",
            "| Symbol | Signal | Action | Quantity | Fill | Realized PnL |",
            "| --- | --- | --- | ---: | ---: | ---: |",
        ]
    )
    for row in result.get("sample_realized_pnl_rows") or []:
        lines.append(
            f"| {row.get('symbol')} | {row.get('exit_signal')} | {row.get('action')} | {row.get('closed_quantity')} | {_fmt_money(row.get('fill_price'))} | {_fmt_money(row.get('realized_pnl'))} |"
        )
    if not result.get("sample_realized_pnl_rows"):
        lines.append("| - | - | - | - | - | - |")
    lines.extend(
        [
            "",
            "## Interpretation note",
            "",
            "M121 simulates paper close fills from M120 paper exit intents and records realized PnL rows. It deliberately does not close real positions or submit broker orders.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_paper_exit_router_markdown(
    output_path: Path,
    result: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(paper_exit_router_markdown(result, filters=filters), encoding="utf-8")
    return output_path


def _paper_exit_router_payload_result(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not payload:
        return {}
    return payload.get("result") or payload


def _latest_marks_from_timeseries_payload(timeseries_payload: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not timeseries_payload:
        return {}
    series = timeseries_payload.get("result") or timeseries_payload
    return {
        str(row.get("symbol") or "").upper(): row
        for row in series.get("latest_positions") or []
        if row.get("symbol")
    }


def build_paper_portfolio_state_report(
    entry_router_payload: dict[str, Any],
    *,
    exit_router_payload: dict[str, Any] | None = None,
    latest_timeseries_payload: dict[str, Any] | None = None,
    state_run_id: str | None = None,
    sample_limit: int = 20,
) -> dict[str, Any]:
    entry_router = _paper_router_payload_result(entry_router_payload)
    exit_router = _paper_exit_router_payload_result(exit_router_payload)
    latest_marks = _latest_marks_from_timeseries_payload(latest_timeseries_payload)
    run_id = state_run_id or datetime.now(UTC).strftime("paper_portfolio_state_%Y%m%dT%H%M%SZ")
    rows_by_symbol: dict[str, dict[str, Any]] = {}
    for position in entry_router.get("paper_positions") or []:
        symbol = str(position.get("symbol") or "").upper()
        if not symbol:
            continue
        long_qty = _float_or_none(position.get("long_quantity")) or 0.0
        short_qty = _float_or_none(position.get("short_quantity")) or 0.0
        long_cost = _float_or_none(position.get("long_cost_basis"))
        short_proceeds = _float_or_none(position.get("short_proceeds"))
        long_avg = _float_or_none(position.get("long_avg_price"))
        short_avg = _float_or_none(position.get("short_avg_price"))
        if long_cost is None and long_avg is not None:
            long_cost = long_qty * long_avg
        if short_proceeds is None and short_avg is not None:
            short_proceeds = short_qty * short_avg
        rows_by_symbol[symbol] = {
            "symbol": symbol,
            "entry_long_quantity": long_qty,
            "entry_short_quantity": short_qty,
            "entry_long_cost_basis": long_cost or 0.0,
            "entry_short_proceeds": short_proceeds or 0.0,
            "long_avg_price": long_avg,
            "short_avg_price": short_avg,
            "closed_long_quantity": 0.0,
            "closed_short_quantity": 0.0,
            "realized_pnl": 0.0,
            "exit_fill_count": 0,
            "exit_intent_ids": [],
        }
    unmatched_exit_rows: list[dict[str, Any]] = []
    overclose_counts: Counter[str] = Counter()
    for realized in exit_router.get("realized_pnl_ledger") or []:
        symbol = str(realized.get("symbol") or "").upper()
        action = str(realized.get("action") or "")
        quantity = _float_or_none(realized.get("closed_quantity")) or 0.0
        if not symbol or quantity <= 0.0:
            continue
        row = rows_by_symbol.get(symbol)
        if row is None:
            unmatched_exit_rows.append(realized)
            continue
        if action == "SELL":
            row["closed_long_quantity"] += quantity
            if row["closed_long_quantity"] > row["entry_long_quantity"] + 1e-9:
                overclose_counts[f"{symbol}:long"] += 1
        elif action == "BUY_TO_COVER":
            row["closed_short_quantity"] += quantity
            if row["closed_short_quantity"] > row["entry_short_quantity"] + 1e-9:
                overclose_counts[f"{symbol}:short"] += 1
        else:
            unmatched_exit_rows.append(realized)
            continue
        row["realized_pnl"] += _float_or_none(realized.get("realized_pnl")) or 0.0
        row["exit_fill_count"] += 1
        if realized.get("exit_intent_id"):
            row["exit_intent_ids"].append(realized.get("exit_intent_id"))
    portfolio_rows: list[dict[str, Any]] = []
    status_counts: Counter[str] = Counter()
    total_entry_basis = 0.0
    total_open_basis = 0.0
    total_open_market_value = 0.0
    total_realized_pnl = 0.0
    total_unrealized_pnl = 0.0
    for row in rows_by_symbol.values():
        open_long = max(0.0, float(row["entry_long_quantity"]) - float(row["closed_long_quantity"]))
        open_short = max(0.0, float(row["entry_short_quantity"]) - float(row["closed_short_quantity"]))
        long_avg = _float_or_none(row.get("long_avg_price"))
        short_avg = _float_or_none(row.get("short_avg_price"))
        entry_basis = float(row["entry_long_cost_basis"]) + float(row["entry_short_proceeds"])
        open_basis = (open_long * long_avg if long_avg is not None else 0.0) + (
            open_short * short_avg if short_avg is not None else 0.0
        )
        latest = latest_marks.get(row["symbol"]) or {}
        mark_price = _float_or_none(latest.get("mark_price"))
        open_market_value = 0.0
        unrealized_pnl = None
        if mark_price is not None:
            open_market_value = (open_long + open_short) * mark_price
            long_unrealized = (mark_price - long_avg) * open_long if long_avg is not None else 0.0
            short_unrealized = (short_avg - mark_price) * open_short if short_avg is not None else 0.0
            unrealized_pnl = long_unrealized + short_unrealized
            total_unrealized_pnl += unrealized_pnl
            total_open_market_value += open_market_value
        total_entry_basis += entry_basis
        total_open_basis += open_basis
        total_realized_pnl += float(row["realized_pnl"])
        if open_long > 1e-9 or open_short > 1e-9:
            status = "open"
        elif row["entry_long_quantity"] > 0.0 or row["entry_short_quantity"] > 0.0:
            status = "closed"
        else:
            status = "flat"
        status_counts[status] += 1
        portfolio_rows.append(
            {
                **row,
                "open_long_quantity": open_long,
                "open_short_quantity": open_short,
                "open_net_quantity": open_long - open_short,
                "open_basis": open_basis,
                "latest_mark_price": mark_price,
                "latest_mark_source": latest.get("mark_source"),
                "latest_mark_generated_at": latest.get("mark_generated_at"),
                "open_market_value": open_market_value if mark_price is not None else None,
                "unrealized_pnl": unrealized_pnl,
                "total_pnl": float(row["realized_pnl"]) + (unrealized_pnl or 0.0),
                "position_status": status,
            }
        )
    totals = {
        "symbol_count": len(portfolio_rows),
        "open_symbol_count": status_counts.get("open", 0),
        "closed_symbol_count": status_counts.get("closed", 0),
        "entry_basis": total_entry_basis,
        "open_basis": total_open_basis,
        "open_market_value": total_open_market_value,
        "realized_pnl": total_realized_pnl,
        "unrealized_pnl": total_unrealized_pnl,
        "total_pnl": total_realized_pnl + total_unrealized_pnl,
    }
    return {
        "paper_portfolio_state_version": "paper-portfolio-state-reconciler-v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "state_run_id": run_id,
        "source_entry_router_version": entry_router.get("paper_order_router_version"),
        "source_entry_router_run_id": entry_router.get("router_run_id"),
        "source_exit_router_version": exit_router.get("paper_exit_router_version"),
        "source_exit_router_run_id": exit_router.get("router_run_id"),
        "source_timeseries_version": (latest_timeseries_payload or {}).get("result", latest_timeseries_payload or {}).get("paper_position_mark_timeseries_version"),
        "source_timeseries_run_id": (latest_timeseries_payload or {}).get("result", latest_timeseries_payload or {}).get("series_run_id"),
        "portfolio_rows": portfolio_rows,
        "sample_portfolio_rows": portfolio_rows[: max(0, int(sample_limit))],
        "status_counts": dict(status_counts),
        "overclose_counts": dict(overclose_counts),
        "unmatched_exit_row_count": len(unmatched_exit_rows),
        "unmatched_exit_rows": unmatched_exit_rows[: max(0, int(sample_limit))],
        "totals": totals,
        "safety": {
            "mode": "paper_portfolio_state_reconciler",
            "policy": "Reconciles simulated entry fills, simulated close fills, and paper marks into a paper portfolio state only. No broker API call, order submission, transmit, or real portfolio mutation.",
            "ibkr_order_submitted": False,
            "transmit_enabled": False,
            "live_order_routing_enabled": False,
        },
    }


def paper_portfolio_state_markdown(
    result: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
    title: str = "Paper Portfolio State Reconciler",
) -> str:
    filters = filters or {}
    totals = result.get("totals") or {}
    lines = [
        f"# {title}",
        "",
        f"- Generated at: `{result.get('generated_at')}`",
        f"- State run ID: `{result.get('state_run_id')}`",
        f"- Source entry router: `{result.get('source_entry_router_run_id')}`",
        f"- Source exit router: `{result.get('source_exit_router_run_id')}`",
        f"- Source mark series: `{result.get('source_timeseries_run_id')}`",
        f"- Symbols: `{totals.get('symbol_count')}`",
        f"- Open symbols: `{totals.get('open_symbol_count')}`",
        f"- Closed symbols: `{totals.get('closed_symbol_count')}`",
        f"- Realized PnL: `{_fmt_money(totals.get('realized_pnl'))}`",
        f"- Unrealized PnL: `{_fmt_money(totals.get('unrealized_pnl'))}`",
        f"- Total PnL: `{_fmt_money(totals.get('total_pnl'))}`",
        "- Safety: paper portfolio state only; no broker order API call or transmit.",
        "",
        "## Inputs",
        "",
    ]
    if filters:
        for key, value in filters.items():
            lines.append(f"- `{key}`: `{value if value is not None else '-'}`")
    else:
        lines.append("- none")
    lines.extend(["", "## Status counts", "", "| Status | Count |", "| --- | ---: |"])
    counts = result.get("status_counts") or {}
    if counts:
        for key, value in sorted(counts.items(), key=lambda item: (-int(item[1]), item[0])):
            lines.append(f"| {key} | {value} |")
    else:
        lines.append("| - | 0 |")
    lines.extend(
        [
            "",
            "## Portfolio rows",
            "",
            "| Symbol | Status | Open net | Mark | Realized | Unrealized | Total PnL |",
            "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for row in result.get("sample_portfolio_rows") or []:
        lines.append(
            f"| {row.get('symbol')} | {row.get('position_status')} | {row.get('open_net_quantity')} | {_fmt_money(row.get('latest_mark_price'))} | {_fmt_money(row.get('realized_pnl'))} | {_fmt_money(row.get('unrealized_pnl'))} | {_fmt_money(row.get('total_pnl'))} |"
        )
    if not result.get("sample_portfolio_rows"):
        lines.append("| - | - | - | - | - | - | - |")
    lines.extend(
        [
            "",
            "## Interpretation note",
            "",
            "M122 reconciles paper entries, paper exits, and latest paper marks into one current paper portfolio state. It is an accounting layer only and does not change any real or broker portfolio.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_paper_portfolio_state_markdown(
    output_path: Path,
    result: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(paper_portfolio_state_markdown(result, filters=filters), encoding="utf-8")
    return output_path


def _paper_portfolio_state_payload_result(payload: dict[str, Any]) -> dict[str, Any]:
    return payload.get("result") or payload


def _paper_portfolio_row_exposure(row: dict[str, Any]) -> float:
    market_value = _float_or_none(row.get("open_market_value"))
    if market_value is not None:
        return abs(market_value)
    basis = _float_or_none(row.get("open_basis"))
    return abs(basis or 0.0)


def _paper_portfolio_row_short_exposure(row: dict[str, Any]) -> float:
    short_qty = _float_or_none(row.get("open_short_quantity")) or 0.0
    if short_qty <= 0.0:
        return 0.0
    mark = _float_or_none(row.get("latest_mark_price"))
    avg = _float_or_none(row.get("short_avg_price"))
    price = mark if mark is not None else avg
    return short_qty * float(price or 0.0)


def _risk_limit_breach(name: str, value: float, limit: float, *, severity: str, recommendation: str) -> dict[str, Any]:
    return {
        "name": name,
        "severity": severity,
        "value": value,
        "limit": limit,
        "excess": value - limit,
        "recommendation": recommendation,
    }


def build_paper_portfolio_risk_state_report(
    portfolio_state_payload: dict[str, Any],
    *,
    max_gross_exposure: float = 100000.0,
    max_symbol_exposure: float = 25000.0,
    max_short_exposure: float = 25000.0,
    max_unrealized_loss: float = 1000.0,
    max_total_loss: float = 1000.0,
    max_open_symbols: int = 25,
    warning_ratio: float = 0.80,
    risk_run_id: str | None = None,
    sample_limit: int = 20,
) -> dict[str, Any]:
    state = _paper_portfolio_state_payload_result(portfolio_state_payload)
    rows = list(state.get("portfolio_rows") or [])
    run_id = risk_run_id or datetime.now(UTC).strftime("paper_portfolio_risk_%Y%m%dT%H%M%SZ")
    gross_exposure = sum(_paper_portfolio_row_exposure(row) for row in rows)
    short_exposure = sum(_paper_portfolio_row_short_exposure(row) for row in rows)
    open_symbol_count = sum(1 for row in rows if row.get("position_status") == "open")
    totals = state.get("totals") or {}
    unrealized_pnl = _float_or_none(totals.get("unrealized_pnl")) or 0.0
    total_pnl = _float_or_none(totals.get("total_pnl")) or 0.0
    risk_checks: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    breaches: list[dict[str, Any]] = []

    def add_check(name: str, value: float, limit: float, *, recommendation: str, inverse_loss: bool = False) -> None:
        threshold_value = abs(limit)
        if inverse_loss:
            breached = value <= -threshold_value
            warned = value <= -(threshold_value * float(warning_ratio))
            utilization = abs(value) / threshold_value if threshold_value > 0.0 else None
        else:
            breached = value > threshold_value
            warned = value >= threshold_value * float(warning_ratio)
            utilization = value / threshold_value if threshold_value > 0.0 else None
        status = "breach" if breached else "warning" if warned else "ok"
        check = {
            "name": name,
            "value": value,
            "limit": threshold_value,
            "utilization": utilization,
            "status": status,
            "recommendation": recommendation if status != "ok" else "within_limit",
        }
        risk_checks.append(check)
        if breached:
            breaches.append(_risk_limit_breach(name, abs(value) if inverse_loss else value, threshold_value, severity="hard", recommendation=recommendation))
        elif warned:
            warnings.append(_risk_limit_breach(name, abs(value) if inverse_loss else value, threshold_value, severity="warning", recommendation=recommendation))

    add_check("gross_exposure", gross_exposure, max_gross_exposure, recommendation="pause_new_entries_and_reduce_gross_exposure")
    add_check("short_exposure", short_exposure, max_short_exposure, recommendation="reduce_short_exposure")
    add_check("open_symbol_count", float(open_symbol_count), float(max_open_symbols), recommendation="pause_new_entries_or_consolidate_positions")
    add_check("unrealized_loss", unrealized_pnl, max_unrealized_loss, recommendation="review_stop_loss_or_reduce_positions", inverse_loss=True)
    add_check("total_loss", total_pnl, max_total_loss, recommendation="pause_trading_and_reduce_risk", inverse_loss=True)

    row_risk: list[dict[str, Any]] = []
    for row in rows:
        symbol = str(row.get("symbol") or "").upper()
        exposure = _paper_portfolio_row_exposure(row)
        short_symbol_exposure = _paper_portfolio_row_short_exposure(row)
        missing_mark = row.get("position_status") == "open" and _float_or_none(row.get("latest_mark_price")) is None
        row_status = "ok"
        row_reasons: list[str] = []
        if exposure > float(max_symbol_exposure):
            row_status = "breach"
            row_reasons.append("symbol_exposure_limit_breached")
            breaches.append(
                _risk_limit_breach(
                    f"symbol_exposure:{symbol}",
                    exposure,
                    float(max_symbol_exposure),
                    severity="hard",
                    recommendation="reduce_symbol_exposure",
                )
            )
        elif exposure >= float(max_symbol_exposure) * float(warning_ratio):
            row_status = "warning"
            row_reasons.append("symbol_exposure_near_limit")
            warnings.append(
                _risk_limit_breach(
                    f"symbol_exposure:{symbol}",
                    exposure,
                    float(max_symbol_exposure),
                    severity="warning",
                    recommendation="monitor_symbol_exposure",
                )
            )
        if missing_mark:
            row_status = "warning" if row_status == "ok" else row_status
            row_reasons.append("latest_mark_missing")
            warnings.append(
                _risk_limit_breach(
                    f"missing_mark:{symbol}",
                    1.0,
                    0.0,
                    severity="warning",
                    recommendation="refresh_market_marks_before_new_entries",
                )
            )
        row_risk.append(
            {
                "symbol": symbol,
                "position_status": row.get("position_status"),
                "gross_exposure": exposure,
                "short_exposure": short_symbol_exposure,
                "realized_pnl": row.get("realized_pnl"),
                "unrealized_pnl": row.get("unrealized_pnl"),
                "total_pnl": row.get("total_pnl"),
                "risk_status": row_status,
                "risk_reasons": row_reasons,
            }
        )

    if breaches:
        risk_state = "reduce_risk"
        new_entries_allowed = False
        recommended_action = "pause_new_entries_and_reduce_flagged_exposures"
    elif warnings:
        risk_state = "monitor"
        new_entries_allowed = True
        recommended_action = "allow_new_entries_with_caution_and_refresh_marks"
    else:
        risk_state = "within_limits"
        new_entries_allowed = True
        recommended_action = "allow_new_entries"

    return {
        "version": "paper-portfolio-risk-state-v1",
        "paper_portfolio_risk_state_version": "paper-portfolio-risk-state-v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "risk_run_id": run_id,
        "source_portfolio_state_version": state.get("paper_portfolio_state_version"),
        "source_portfolio_state_run_id": state.get("state_run_id"),
        "risk_state": risk_state,
        "recommended_action": recommended_action,
        "new_entries_allowed": new_entries_allowed,
        "breach_count": len(breaches),
        "warning_count": len(warnings),
        "limits": {
            "max_gross_exposure": float(max_gross_exposure),
            "max_symbol_exposure": float(max_symbol_exposure),
            "max_short_exposure": float(max_short_exposure),
            "max_unrealized_loss": float(max_unrealized_loss),
            "max_total_loss": float(max_total_loss),
            "max_open_symbols": int(max_open_symbols),
            "warning_ratio": float(warning_ratio),
        },
        "metrics": {
            "gross_exposure": gross_exposure,
            "short_exposure": short_exposure,
            "open_symbol_count": open_symbol_count,
            "realized_pnl": _float_or_none(totals.get("realized_pnl")) or 0.0,
            "unrealized_pnl": unrealized_pnl,
            "total_pnl": total_pnl,
        },
        "risk_checks": risk_checks,
        "breaches": breaches[: max(0, int(sample_limit))],
        "warnings": warnings[: max(0, int(sample_limit))],
        "row_risk": row_risk,
        "sample_row_risk": row_risk[: max(0, int(sample_limit))],
        "safety": {
            "mode": "paper_portfolio_risk_state",
            "policy": "Evaluates paper portfolio state against paper risk limits only. No broker API call, order submission, transmit, or real portfolio mutation.",
            "ibkr_order_submitted": False,
            "transmit_enabled": False,
            "live_order_routing_enabled": False,
        },
    }


def paper_portfolio_risk_state_markdown(
    result: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
    title: str = "Paper Portfolio Risk State",
) -> str:
    filters = filters or {}
    metrics = result.get("metrics") or {}
    lines = [
        f"# {title}",
        "",
        f"- Generated at: `{result.get('generated_at')}`",
        f"- Risk run ID: `{result.get('risk_run_id')}`",
        f"- Source portfolio state: `{result.get('source_portfolio_state_run_id')}`",
        f"- Risk state: `{result.get('risk_state')}`",
        f"- Recommended action: `{result.get('recommended_action')}`",
        f"- New entries allowed: `{result.get('new_entries_allowed')}`",
        f"- Breaches: `{result.get('breach_count')}`",
        f"- Warnings: `{result.get('warning_count')}`",
        f"- Gross exposure: `{_fmt_money(metrics.get('gross_exposure'))}`",
        f"- Short exposure: `{_fmt_money(metrics.get('short_exposure'))}`",
        f"- Total PnL: `{_fmt_money(metrics.get('total_pnl'))}`",
        "- Safety: paper risk state only; no broker order API call or transmit.",
        "",
        "## Inputs",
        "",
    ]
    if filters:
        for key, value in filters.items():
            lines.append(f"- `{key}`: `{value if value is not None else '-'}`")
    else:
        lines.append("- none")
    lines.extend(["", "## Risk checks", "", "| Check | Status | Value | Limit | Utilization |", "| --- | --- | ---: | ---: | ---: |"])
    for check in result.get("risk_checks") or []:
        utilization = check.get("utilization")
        lines.append(
            f"| {check.get('name')} | {check.get('status')} | {_fmt_money(check.get('value'))} | {_fmt_money(check.get('limit'))} | {_fmt_pct(utilization) if utilization is not None else '-'} |"
        )
    if not result.get("risk_checks"):
        lines.append("| - | - | - | - | - |")
    lines.extend(
        [
            "",
            "## Row risk",
            "",
            "| Symbol | Status | Exposure | Short exposure | Total PnL | Risk status | Reasons |",
            "| --- | --- | ---: | ---: | ---: | --- | --- |",
        ]
    )
    for row in result.get("sample_row_risk") or []:
        reasons = ", ".join(row.get("risk_reasons") or []) or "-"
        lines.append(
            f"| {row.get('symbol')} | {row.get('position_status')} | {_fmt_money(row.get('gross_exposure'))} | {_fmt_money(row.get('short_exposure'))} | {_fmt_money(row.get('total_pnl'))} | {row.get('risk_status')} | {reasons} |"
        )
    if not result.get("sample_row_risk"):
        lines.append("| - | - | - | - | - | - | - |")
    lines.extend(
        [
            "",
            "## Interpretation note",
            "",
            "M123 evaluates the current paper portfolio state against configurable risk limits. It can pause new paper entries or recommend risk reduction, but it does not place or close any real orders.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_paper_portfolio_risk_state_markdown(
    output_path: Path,
    result: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(paper_portfolio_risk_state_markdown(result, filters=filters), encoding="utf-8")
    return output_path


def build_paper_portfolio_risk_loop_report(
    entry_router_payload: dict[str, Any],
    mark_snapshot_payloads: list[dict[str, Any]],
    *,
    exit_router_payload: dict[str, Any] | None = None,
    take_profit_pct: float = 0.03,
    stop_loss_pct: float = 0.02,
    max_loss_dollars: float | None = None,
    max_gross_exposure: float = 100000.0,
    max_symbol_exposure: float = 25000.0,
    max_short_exposure: float = 25000.0,
    max_unrealized_loss: float = 1000.0,
    max_total_loss: float = 1000.0,
    max_open_symbols: int = 25,
    warning_ratio: float = 0.80,
    monitor_size_multiplier: float = 0.50,
    loop_run_id: str | None = None,
    sample_limit: int = 20,
) -> dict[str, Any]:
    run_id = loop_run_id or datetime.now(UTC).strftime("paper_portfolio_risk_loop_%Y%m%dT%H%M%SZ")
    mark_timeseries = build_paper_position_mark_timeseries_report(
        entry_router_payload,
        mark_snapshot_payloads,
        take_profit_pct=take_profit_pct,
        stop_loss_pct=stop_loss_pct,
        max_loss_dollars=max_loss_dollars,
        series_run_id=f"{run_id}_mark_series",
        sample_limit=sample_limit,
    )
    portfolio_state = build_paper_portfolio_state_report(
        entry_router_payload,
        exit_router_payload=exit_router_payload,
        latest_timeseries_payload=mark_timeseries,
        state_run_id=f"{run_id}_portfolio_state",
        sample_limit=sample_limit,
    )
    portfolio_risk_state = build_paper_portfolio_risk_state_report(
        portfolio_state,
        max_gross_exposure=max_gross_exposure,
        max_symbol_exposure=max_symbol_exposure,
        max_short_exposure=max_short_exposure,
        max_unrealized_loss=max_unrealized_loss,
        max_total_loss=max_total_loss,
        max_open_symbols=max_open_symbols,
        warning_ratio=warning_ratio,
        risk_run_id=f"{run_id}_risk_state",
        sample_limit=sample_limit,
    )
    next_entry_gate = _portfolio_entry_risk_gate_snapshot(
        portfolio_risk_state,
        monitor_size_multiplier=monitor_size_multiplier,
    )
    router = _paper_router_payload_result(entry_router_payload)
    return {
        "paper_portfolio_risk_loop_version": "paper-portfolio-risk-loop-v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "loop_run_id": run_id,
        "source_entry_router_version": router.get("paper_order_router_version"),
        "source_entry_router_run_id": router.get("router_run_id"),
        "source_mark_snapshot_count": len(mark_snapshot_payloads),
        "source_exit_router_version": (_paper_exit_router_payload_result(exit_router_payload) or {}).get("paper_exit_router_version"),
        "source_exit_router_run_id": (_paper_exit_router_payload_result(exit_router_payload) or {}).get("router_run_id"),
        "mark_timeseries": mark_timeseries,
        "portfolio_state": portfolio_state,
        "portfolio_risk_state": portfolio_risk_state,
        "next_entry_gate": next_entry_gate,
        "component_run_ids": {
            "mark_series_run_id": mark_timeseries.get("series_run_id"),
            "portfolio_state_run_id": portfolio_state.get("state_run_id"),
            "portfolio_risk_run_id": portfolio_risk_state.get("risk_run_id"),
        },
        "loop_summary": {
            "risk_state": portfolio_risk_state.get("risk_state"),
            "recommended_action": portfolio_risk_state.get("recommended_action"),
            "new_entries_allowed": portfolio_risk_state.get("new_entries_allowed"),
            "next_entry_gate_action": next_entry_gate.get("action"),
            "paper_order_count": router.get("paper_order_count"),
            "paper_fill_count": router.get("paper_fill_count"),
            "paper_position_count": router.get("paper_position_count"),
            "open_symbol_count": (portfolio_risk_state.get("metrics") or {}).get("open_symbol_count"),
            "gross_exposure": (portfolio_risk_state.get("metrics") or {}).get("gross_exposure"),
            "short_exposure": (portfolio_risk_state.get("metrics") or {}).get("short_exposure"),
            "total_pnl": (portfolio_risk_state.get("metrics") or {}).get("total_pnl"),
            "breach_count": portfolio_risk_state.get("breach_count"),
            "warning_count": portfolio_risk_state.get("warning_count"),
        },
        "safety": {
            "mode": "paper_portfolio_risk_loop",
            "policy": "Links simulated paper fills, paper mark-to-market, paper portfolio state, and paper portfolio risk state for the next entry gate only. No broker API call, order submission, transmit, or real portfolio mutation.",
            "ibkr_order_submitted": False,
            "transmit_enabled": False,
            "live_order_routing_enabled": False,
        },
    }


def paper_portfolio_risk_loop_markdown(
    result: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
    title: str = "Paper Portfolio Risk Loop",
) -> str:
    filters = filters or {}
    summary = result.get("loop_summary") or {}
    ids = result.get("component_run_ids") or {}
    lines = [
        f"# {title}",
        "",
        f"- Generated at: `{result.get('generated_at')}`",
        f"- Loop run ID: `{result.get('loop_run_id')}`",
        f"- Source entry router: `{result.get('source_entry_router_run_id')}`",
        f"- Mark snapshots: `{result.get('source_mark_snapshot_count')}`",
        f"- Mark series run ID: `{ids.get('mark_series_run_id')}`",
        f"- Portfolio state run ID: `{ids.get('portfolio_state_run_id')}`",
        f"- Portfolio risk run ID: `{ids.get('portfolio_risk_run_id')}`",
        f"- Risk state: `{summary.get('risk_state')}`",
        f"- Next entry gate action: `{summary.get('next_entry_gate_action')}`",
        f"- New entries allowed: `{summary.get('new_entries_allowed')}`",
        f"- Breaches: `{summary.get('breach_count')}`",
        f"- Warnings: `{summary.get('warning_count')}`",
        f"- Gross exposure: `{_fmt_money(summary.get('gross_exposure'))}`",
        f"- Short exposure: `{_fmt_money(summary.get('short_exposure'))}`",
        f"- Total PnL: `{_fmt_money(summary.get('total_pnl'))}`",
        "- Safety: paper loop only; no broker order API call or transmit.",
        "",
        "## Inputs",
        "",
    ]
    if filters:
        for key, value in filters.items():
            lines.append(f"- `{key}`: `{value if value is not None else '-'}`")
    else:
        lines.append("- none")
    state = result.get("portfolio_state") or {}
    risk = result.get("portfolio_risk_state") or {}
    lines.extend(["", "## Portfolio rows", "", "| Symbol | Status | Open net | Mark | Total PnL | Risk |", "| --- | --- | ---: | ---: | ---: | --- |"])
    risk_by_symbol = {str(row.get("symbol") or "").upper(): row for row in risk.get("row_risk") or []}
    for row in state.get("sample_portfolio_rows") or []:
        symbol = str(row.get("symbol") or "").upper()
        risk_row = risk_by_symbol.get(symbol) or {}
        lines.append(
            f"| {symbol} | {row.get('position_status')} | {row.get('open_net_quantity')} | {_fmt_money(row.get('latest_mark_price'))} | {_fmt_money(row.get('total_pnl'))} | {risk_row.get('risk_status') or '-'} |"
        )
    if not state.get("sample_portfolio_rows"):
        lines.append("| - | - | - | - | - | - |")
    lines.extend(
        [
            "",
            "## Next-cycle handoff",
            "",
            "Use the nested `portfolio_risk_state` object, or the optional M126 component risk JSON output, as the next cycle's `--portfolio-risk-state-json` input to the M124 execution-intent ledger.",
            "",
            "## Interpretation note",
            "",
            "M126 closes the paper feedback loop: M125 simulated fills are marked, reconciled into current paper portfolio state, converted to M123-compatible risk state, and summarized as the next entry gate input.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_paper_portfolio_risk_loop_markdown(
    output_path: Path,
    result: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(paper_portfolio_risk_loop_markdown(result, filters=filters), encoding="utf-8")
    return output_path


def build_paper_cycle_runner_report(
    auto_gate_payload: dict[str, Any],
    portfolio_risk_state_payload: dict[str, Any],
    fill_marks_payload: dict[str, Any],
    loop_mark_snapshot_payloads: list[dict[str, Any]],
    *,
    existing_ledger_payload: dict[str, Any] | None = None,
    existing_router_payload: dict[str, Any] | None = None,
    exit_router_payload: dict[str, Any] | None = None,
    operating_auto_gate_payload: dict[str, Any] | None = None,
    session_transition_release_governor_payload: dict[str, Any] | None = None,
    require_operating_preflight: bool = False,
    allow_non_operating_source_ledger: bool = False,
    require_session_transition_release_governor: bool = False,
    monitor_size_multiplier: float = 0.50,
    max_intents: int = 10,
    fill_price_source: str = "require_marks",
    fill_model: str = "ibkr_mark_reference_fill_v1",
    max_orders: int = 10,
    slippage_bps: float = 0.0,
    commission_per_order: float = 0.0,
    take_profit_pct: float = 0.03,
    stop_loss_pct: float = 0.02,
    max_loss_dollars: float | None = None,
    max_gross_exposure: float = 100000.0,
    max_symbol_exposure: float = 25000.0,
    max_short_exposure: float = 25000.0,
    max_unrealized_loss: float = 1000.0,
    max_total_loss: float = 1000.0,
    max_open_symbols: int = 25,
    warning_ratio: float = 0.80,
    cycle_run_id: str | None = None,
    sample_limit: int = 20,
) -> dict[str, Any]:
    run_id = cycle_run_id or datetime.now(UTC).strftime("paper_cycle_%Y%m%dT%H%M%SZ")
    entry_ledger = build_execution_intent_ledger_report(
        auto_gate_payload,
        existing_ledger_payload=existing_ledger_payload,
        portfolio_risk_state_payload=portfolio_risk_state_payload,
        session_transition_release_governor_payload=session_transition_release_governor_payload,
        monitor_size_multiplier=monitor_size_multiplier,
        max_intents=max_intents,
        ledger_run_id=f"{run_id}_entry_ledger",
        sample_limit=sample_limit,
    )
    entry_router = build_paper_order_router_report(
        entry_ledger,
        existing_router_payload=existing_router_payload,
        marks_payload=fill_marks_payload,
        operating_auto_gate_payload=operating_auto_gate_payload,
        session_transition_release_governor_payload=session_transition_release_governor_payload,
        require_operating_preflight=require_operating_preflight,
        allow_non_operating_source_ledger=allow_non_operating_source_ledger,
        require_session_transition_release_governor=require_session_transition_release_governor,
        fill_price_source=fill_price_source,
        fill_model=fill_model,
        require_portfolio_risk_gate=True,
        max_orders=max_orders,
        slippage_bps=slippage_bps,
        commission_per_order=commission_per_order,
        router_run_id=f"{run_id}_entry_router",
        sample_limit=sample_limit,
    )
    risk_loop = build_paper_portfolio_risk_loop_report(
        entry_router,
        loop_mark_snapshot_payloads,
        exit_router_payload=exit_router_payload,
        take_profit_pct=take_profit_pct,
        stop_loss_pct=stop_loss_pct,
        max_loss_dollars=max_loss_dollars,
        max_gross_exposure=max_gross_exposure,
        max_symbol_exposure=max_symbol_exposure,
        max_short_exposure=max_short_exposure,
        max_unrealized_loss=max_unrealized_loss,
        max_total_loss=max_total_loss,
        max_open_symbols=max_open_symbols,
        warning_ratio=warning_ratio,
        monitor_size_multiplier=monitor_size_multiplier,
        loop_run_id=f"{run_id}_risk_loop",
        sample_limit=sample_limit,
    )
    initial_risk = _paper_portfolio_risk_state_payload_result(portfolio_risk_state_payload)
    cycle_summary = {
        "initial_risk_state": initial_risk.get("risk_state"),
        "entry_gate_action": (entry_ledger.get("portfolio_risk_gate") or {}).get("action"),
        "entry_release_status": (entry_ledger.get("session_transition_release_governor") or {}).get("release_status"),
        "entry_release_allowed": (entry_ledger.get("session_transition_release_governor") or {}).get("ordinary_alpha_release_allowed"),
        "entry_release_size_multiplier": (entry_ledger.get("session_transition_release_governor") or {}).get("max_new_entry_size_multiplier"),
        "entry_intent_count": entry_ledger.get("intent_count"),
        "pending_entry_intent_count": entry_ledger.get("pending_paper_execution_count"),
        "blocked_entry_intent_count": entry_ledger.get("blocked_by_portfolio_risk_state_count"),
        "release_governor_blocked_entry_intent_count": entry_ledger.get("blocked_by_session_transition_release_governor_count"),
        "paper_order_count": entry_router.get("paper_order_count"),
        "paper_fill_count": entry_router.get("paper_fill_count"),
        "paper_position_count": entry_router.get("paper_position_count"),
        "session_transition_release_governor_applied": entry_router.get("session_transition_release_governor_applied"),
        "session_transition_release_governor_allowed": entry_router.get("session_transition_release_governor_allowed"),
        "session_transition_release_governor_action": entry_router.get("session_transition_release_governor_action"),
        "operating_final_preflight_applied": entry_router.get("operating_final_preflight_applied"),
        "operating_final_preflight_allowed": entry_router.get("operating_final_preflight_allowed"),
        "operating_final_preflight_action": entry_router.get("operating_final_preflight_action"),
        "operating_final_preflight_reasons": entry_router.get("operating_final_preflight_reasons") or [],
        "post_cycle_risk_state": (risk_loop.get("loop_summary") or {}).get("risk_state"),
        "post_cycle_gate_action": (risk_loop.get("loop_summary") or {}).get("next_entry_gate_action"),
        "post_cycle_new_entries_allowed": (risk_loop.get("loop_summary") or {}).get("new_entries_allowed"),
        "post_cycle_gross_exposure": (risk_loop.get("loop_summary") or {}).get("gross_exposure"),
        "post_cycle_short_exposure": (risk_loop.get("loop_summary") or {}).get("short_exposure"),
        "post_cycle_total_pnl": (risk_loop.get("loop_summary") or {}).get("total_pnl"),
        "post_cycle_breach_count": (risk_loop.get("loop_summary") or {}).get("breach_count"),
        "post_cycle_warning_count": (risk_loop.get("loop_summary") or {}).get("warning_count"),
    }
    return {
        "paper_cycle_runner_version": "paper-cycle-runner-v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "cycle_run_id": run_id,
        "component_run_ids": {
            "entry_ledger_run_id": entry_ledger.get("ledger_run_id"),
            "entry_router_run_id": entry_router.get("router_run_id"),
            "risk_loop_run_id": risk_loop.get("loop_run_id"),
            "next_risk_run_id": ((risk_loop.get("portfolio_risk_state") or {}).get("risk_run_id")),
        },
        "cycle_summary": cycle_summary,
        "entry_ledger": entry_ledger,
        "entry_router": entry_router,
        "risk_loop": risk_loop,
        "next_portfolio_risk_state": risk_loop.get("portfolio_risk_state"),
        "next_entry_gate": risk_loop.get("next_entry_gate"),
        "safety": {
            "mode": "paper_cycle_runner",
            "policy": "Runs one paper-only cycle from risk-gated entry intents through simulated fills and next-cycle risk state. No broker API call, order submission, transmit, or real portfolio mutation.",
            "ibkr_order_submitted": False,
            "transmit_enabled": False,
            "live_order_routing_enabled": False,
        },
    }


def paper_cycle_runner_markdown(
    result: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
    title: str = "Paper Cycle Runner",
) -> str:
    filters = filters or {}
    summary = result.get("cycle_summary") or {}
    ids = result.get("component_run_ids") or {}
    lines = [
        f"# {title}",
        "",
        f"- Generated at: `{result.get('generated_at')}`",
        f"- Cycle run ID: `{result.get('cycle_run_id')}`",
        f"- Entry ledger run ID: `{ids.get('entry_ledger_run_id')}`",
        f"- Entry router run ID: `{ids.get('entry_router_run_id')}`",
        f"- Risk loop run ID: `{ids.get('risk_loop_run_id')}`",
        f"- Next risk run ID: `{ids.get('next_risk_run_id')}`",
        f"- Initial risk state: `{summary.get('initial_risk_state')}`",
        f"- Entry gate action: `{summary.get('entry_gate_action')}`",
        f"- Session-transition release: `{summary.get('entry_release_status') or '-'}` allowed `{summary.get('entry_release_allowed')}` size `{summary.get('entry_release_size_multiplier')}`",
        f"- Pending entry intents: `{summary.get('pending_entry_intent_count')}`",
        f"- Blocked entry intents: `{summary.get('blocked_entry_intent_count')}`",
        f"- Release-governor blocked entry intents: `{summary.get('release_governor_blocked_entry_intent_count')}`",
        f"- Release-governor router preflight: applied `{summary.get('session_transition_release_governor_applied')}` / allowed `{summary.get('session_transition_release_governor_allowed')}` / action `{summary.get('session_transition_release_governor_action') or '-'}`",
        f"- Operating final preflight: applied `{summary.get('operating_final_preflight_applied')}` / allowed `{summary.get('operating_final_preflight_allowed')}` / action `{summary.get('operating_final_preflight_action') or '-'}`",
        f"- Operating final preflight reasons: `{summary.get('operating_final_preflight_reasons') or []}`",
        f"- Paper fills: `{summary.get('paper_fill_count')}`",
        f"- Post-cycle risk state: `{summary.get('post_cycle_risk_state')}`",
        f"- Post-cycle gate action: `{summary.get('post_cycle_gate_action')}`",
        f"- Post-cycle gross exposure: `{_fmt_money(summary.get('post_cycle_gross_exposure'))}`",
        f"- Post-cycle short exposure: `{_fmt_money(summary.get('post_cycle_short_exposure'))}`",
        f"- Post-cycle total PnL: `{_fmt_money(summary.get('post_cycle_total_pnl'))}`",
        "- Safety: paper cycle only; no broker order API call or transmit.",
        "",
        "## Inputs",
        "",
    ]
    if filters:
        for key, value in filters.items():
            lines.append(f"- `{key}`: `{value if value is not None else '-'}`")
    else:
        lines.append("- none")
    router = result.get("entry_router") or {}
    skipped = router.get("skipped_counts") or {}
    lines.extend(["", "## Router skipped counts", "", "| Reason | Count |", "| --- | ---: |"])
    if skipped:
        for key, value in sorted(skipped.items(), key=lambda item: (-int(item[1]), item[0])):
            lines.append(f"| {key} | {value} |")
    else:
        lines.append("| - | 0 |")
    state = ((result.get("risk_loop") or {}).get("portfolio_state") or {})
    lines.extend(["", "## Post-cycle portfolio rows", "", "| Symbol | Status | Open net | Mark | Total PnL |", "| --- | --- | ---: | ---: | ---: |"])
    for row in state.get("sample_portfolio_rows") or []:
        lines.append(
            f"| {row.get('symbol')} | {row.get('position_status')} | {row.get('open_net_quantity')} | {_fmt_money(row.get('latest_mark_price'))} | {_fmt_money(row.get('total_pnl'))} |"
        )
    if not state.get("sample_portfolio_rows"):
        lines.append("| - | - | - | - | - |")
    lines.extend(
        [
            "",
            "## Next-cycle handoff",
            "",
            "Use `next_portfolio_risk_state`, or the optional next-risk component JSON output, as the next cycle's `--portfolio-risk-state-json` input.",
            "",
            "## Interpretation note",
            "",
            "M127 runs one complete paper cycle: M124 risk-gated entry ledger, M125 required-gate router, and M126 portfolio risk loop. It is still fully paper-only.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_paper_cycle_runner_markdown(
    output_path: Path,
    result: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(paper_cycle_runner_markdown(result, filters=filters), encoding="utf-8")
    return output_path


def _aggregate_paper_entry_routers(router_payloads: list[dict[str, Any]], *, aggregate_run_id: str) -> dict[str, Any]:
    paper_orders: list[dict[str, Any]] = []
    paper_fills: list[dict[str, Any]] = []
    positions: dict[str, dict[str, Any]] = {}
    order_state_counts: Counter[str] = Counter()
    action_counts: Counter[str] = Counter()
    skipped_counts: Counter[str] = Counter()
    fill_reference_counts: Counter[str] = Counter()
    for payload in router_payloads:
        router = _paper_router_payload_result(payload)
        paper_orders.extend(list(router.get("paper_orders") or []))
        paper_fills.extend(list(router.get("paper_fills") or []))
        order_state_counts.update({str(k): int(v) for k, v in (router.get("order_state_counts") or {}).items()})
        action_counts.update({str(k): int(v) for k, v in (router.get("action_counts") or {}).items()})
        skipped_counts.update({str(k): int(v) for k, v in (router.get("skipped_counts") or {}).items()})
        fill_reference_counts.update({str(k): int(v) for k, v in (router.get("fill_reference_counts") or {}).items()})
        for position in router.get("paper_positions") or []:
            symbol = str(position.get("symbol") or "").upper()
            if not symbol:
                continue
            row = positions.setdefault(
                symbol,
                {
                    "symbol": symbol,
                    "long_quantity": 0.0,
                    "short_quantity": 0.0,
                    "long_cost_basis": 0.0,
                    "short_proceeds": 0.0,
                    "commission": 0.0,
                    "fills": [],
                },
            )
            row["long_quantity"] += _float_or_none(position.get("long_quantity")) or 0.0
            row["short_quantity"] += _float_or_none(position.get("short_quantity")) or 0.0
            row["long_cost_basis"] += _float_or_none(position.get("long_cost_basis")) or 0.0
            row["short_proceeds"] += _float_or_none(position.get("short_proceeds")) or 0.0
            row["commission"] += _float_or_none(position.get("commission")) or 0.0
            row["fills"].extend(list(position.get("fills") or []))
    position_rows: list[dict[str, Any]] = []
    for row in positions.values():
        long_quantity = float(row.get("long_quantity") or 0.0)
        short_quantity = float(row.get("short_quantity") or 0.0)
        long_avg = (float(row["long_cost_basis"]) / long_quantity) if long_quantity else None
        short_avg = (float(row["short_proceeds"]) / short_quantity) if short_quantity else None
        position_rows.append(
            {
                **row,
                "net_quantity": long_quantity - short_quantity,
                "long_avg_price": long_avg,
                "short_avg_price": short_avg,
                "paper_unrealized_pnl": 0.0,
                "valuation_note": "Aggregated simulated paper entry router across one or more M127/M128 cycles before mark-to-market.",
            }
        )
    totals = {
        "paper_order_count": len(paper_orders),
        "paper_fill_count": len(paper_fills),
        "gross_fill_notional": sum(float(fill.get("fill_notional") or 0.0) for fill in paper_fills),
        "commission": sum(float(fill.get("commission") or 0.0) for fill in paper_fills),
        "long_notional": sum(float(fill.get("fill_notional") or 0.0) for fill in paper_fills if fill.get("side") == "long"),
        "short_notional": sum(float(fill.get("fill_notional") or 0.0) for fill in paper_fills if fill.get("side") == "short"),
        "paper_unrealized_pnl": 0.0,
    }
    return {
        "paper_order_router_version": "paper-order-router-aggregate-v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "router_run_id": aggregate_run_id,
        "source_router_count": len(router_payloads),
        "source_router_run_ids": [
            (_paper_router_payload_result(payload).get("router_run_id"))
            for payload in router_payloads
        ],
        "paper_order_count": len(paper_orders),
        "paper_fill_count": len(paper_fills),
        "paper_position_count": len(position_rows),
        "order_state_counts": dict(order_state_counts),
        "action_counts": dict(action_counts),
        "skipped_counts": dict(skipped_counts),
        "fill_reference_counts": dict(fill_reference_counts),
        "fill_price_source": "aggregate",
        "input_mark_count": 0,
        "require_portfolio_risk_gate": True,
        "source_has_portfolio_risk_gate": True,
        "source_portfolio_risk_gate": None,
        "totals": totals,
        "paper_orders": paper_orders,
        "paper_fills": paper_fills,
        "paper_positions": position_rows,
        "sample_paper_orders": paper_orders[:20],
        "sample_paper_positions": position_rows[:20],
        "safety": {
            "mode": "paper_order_router_aggregate",
            "policy": "Aggregates simulated paper router outputs only. No broker API call, order submission, transmit, or real portfolio mutation.",
            "ibkr_order_submitted": False,
            "transmit_enabled": False,
            "live_order_routing_enabled": False,
        },
    }


def build_paper_multi_cycle_runner_report(
    auto_gate_payload: dict[str, Any],
    initial_portfolio_risk_state_payload: dict[str, Any],
    fill_marks_payload: dict[str, Any],
    loop_mark_snapshot_payloads: list[dict[str, Any]],
    *,
    existing_ledger_payload: dict[str, Any] | None = None,
    existing_router_payload: dict[str, Any] | None = None,
    exit_router_payload: dict[str, Any] | None = None,
    operating_auto_gate_payload: dict[str, Any] | None = None,
    session_transition_release_governor_payload: dict[str, Any] | None = None,
    require_operating_preflight: bool = False,
    allow_non_operating_source_ledger: bool = False,
    require_session_transition_release_governor: bool = False,
    cycle_count: int = 3,
    dedupe_across_cycles: bool = False,
    monitor_size_multiplier: float = 0.50,
    max_intents: int = 10,
    fill_price_source: str = "require_marks",
    fill_model: str = "ibkr_mark_reference_fill_v1",
    max_orders: int = 10,
    slippage_bps: float = 0.0,
    commission_per_order: float = 0.0,
    take_profit_pct: float = 0.03,
    stop_loss_pct: float = 0.02,
    max_loss_dollars: float | None = None,
    max_gross_exposure: float = 100000.0,
    max_symbol_exposure: float = 25000.0,
    max_short_exposure: float = 25000.0,
    max_unrealized_loss: float = 1000.0,
    max_total_loss: float = 1000.0,
    max_open_symbols: int = 25,
    warning_ratio: float = 0.80,
    multi_cycle_run_id: str | None = None,
    sample_limit: int = 20,
) -> dict[str, Any]:
    run_id = multi_cycle_run_id or datetime.now(UTC).strftime("paper_multi_cycle_%Y%m%dT%H%M%SZ")
    current_risk_payload = _paper_portfolio_risk_state_payload_result(initial_portfolio_risk_state_payload)
    cycle_reports: list[dict[str, Any]] = []
    cycle_summaries: list[dict[str, Any]] = []
    entry_ledgers: list[dict[str, Any]] = []
    entry_routers: list[dict[str, Any]] = []
    cumulative_router_payloads: list[dict[str, Any]] = []
    accumulated_ledger_payload = existing_ledger_payload
    accumulated_router_payload = existing_router_payload
    if existing_router_payload is not None:
        cumulative_router_payloads.append(existing_router_payload)
    final_risk_loop: dict[str, Any] | None = None
    for cycle_index in range(1, max(0, int(cycle_count)) + 1):
        cycle_run_id = f"{run_id}_cycle_{cycle_index:02d}"
        entry_ledger = build_execution_intent_ledger_report(
            auto_gate_payload,
            existing_ledger_payload=accumulated_ledger_payload if dedupe_across_cycles else existing_ledger_payload,
            portfolio_risk_state_payload=current_risk_payload,
            session_transition_release_governor_payload=session_transition_release_governor_payload,
            monitor_size_multiplier=monitor_size_multiplier,
            max_intents=max_intents,
            ledger_run_id=f"{cycle_run_id}_entry_ledger",
            sample_limit=sample_limit,
        )
        entry_router = build_paper_order_router_report(
            entry_ledger,
            existing_router_payload=accumulated_router_payload if dedupe_across_cycles else existing_router_payload,
            marks_payload=fill_marks_payload,
            operating_auto_gate_payload=operating_auto_gate_payload,
            session_transition_release_governor_payload=session_transition_release_governor_payload,
            require_operating_preflight=require_operating_preflight,
            allow_non_operating_source_ledger=allow_non_operating_source_ledger,
            require_session_transition_release_governor=require_session_transition_release_governor,
            fill_price_source=fill_price_source,
            fill_model=fill_model,
            require_portfolio_risk_gate=True,
            max_orders=max_orders,
            slippage_bps=slippage_bps,
            commission_per_order=commission_per_order,
            router_run_id=f"{cycle_run_id}_entry_router",
            sample_limit=sample_limit,
        )
        entry_ledgers.append(entry_ledger)
        entry_routers.append(entry_router)
        if int(entry_router.get("paper_fill_count") or 0) > 0:
            cumulative_router_payloads.append(entry_router)
        cumulative_router = _aggregate_paper_entry_routers(
            cumulative_router_payloads,
            aggregate_run_id=f"{cycle_run_id}_cumulative_entry_router",
        )
        risk_loop = build_paper_portfolio_risk_loop_report(
            cumulative_router,
            loop_mark_snapshot_payloads,
            exit_router_payload=exit_router_payload,
            take_profit_pct=take_profit_pct,
            stop_loss_pct=stop_loss_pct,
            max_loss_dollars=max_loss_dollars,
            max_gross_exposure=max_gross_exposure,
            max_symbol_exposure=max_symbol_exposure,
            max_short_exposure=max_short_exposure,
            max_unrealized_loss=max_unrealized_loss,
            max_total_loss=max_total_loss,
            max_open_symbols=max_open_symbols,
            warning_ratio=warning_ratio,
            monitor_size_multiplier=monitor_size_multiplier,
            loop_run_id=f"{cycle_run_id}_risk_loop",
            sample_limit=sample_limit,
        )
        final_risk_loop = risk_loop
        current_risk_payload = risk_loop.get("portfolio_risk_state") or {}
        summary = {
            "cycle_index": cycle_index,
            "cycle_run_id": cycle_run_id,
            "initial_risk_state": (entry_ledger.get("portfolio_risk_gate") or {}).get("portfolio_risk_state"),
            "entry_gate_action": (entry_ledger.get("portfolio_risk_gate") or {}).get("action"),
            "entry_release_status": (entry_ledger.get("session_transition_release_governor") or {}).get("release_status"),
            "entry_release_allowed": (entry_ledger.get("session_transition_release_governor") or {}).get("ordinary_alpha_release_allowed"),
            "entry_release_size_multiplier": (entry_ledger.get("session_transition_release_governor") or {}).get("max_new_entry_size_multiplier"),
            "pending_entry_intents": entry_ledger.get("pending_paper_execution_count"),
            "blocked_entry_intents": entry_ledger.get("blocked_by_portfolio_risk_state_count"),
            "release_governor_blocked_entry_intent_count": entry_ledger.get("blocked_by_session_transition_release_governor_count"),
            "paper_fills": entry_router.get("paper_fill_count"),
            "cumulative_paper_fills": cumulative_router.get("paper_fill_count"),
            "session_transition_release_governor_applied": entry_router.get("session_transition_release_governor_applied"),
            "session_transition_release_governor_allowed": entry_router.get("session_transition_release_governor_allowed"),
            "session_transition_release_governor_action": entry_router.get("session_transition_release_governor_action"),
            "operating_final_preflight_applied": entry_router.get("operating_final_preflight_applied"),
            "operating_final_preflight_allowed": entry_router.get("operating_final_preflight_allowed"),
            "operating_final_preflight_action": entry_router.get("operating_final_preflight_action"),
            "operating_final_preflight_reasons": entry_router.get("operating_final_preflight_reasons") or [],
            "post_cycle_risk_state": (risk_loop.get("loop_summary") or {}).get("risk_state"),
            "post_cycle_gate_action": (risk_loop.get("loop_summary") or {}).get("next_entry_gate_action"),
            "post_cycle_gross_exposure": (risk_loop.get("loop_summary") or {}).get("gross_exposure"),
            "post_cycle_short_exposure": (risk_loop.get("loop_summary") or {}).get("short_exposure"),
            "post_cycle_total_pnl": (risk_loop.get("loop_summary") or {}).get("total_pnl"),
            "post_cycle_breach_count": (risk_loop.get("loop_summary") or {}).get("breach_count"),
            "post_cycle_warning_count": (risk_loop.get("loop_summary") or {}).get("warning_count"),
        }
        cycle_summaries.append(summary)
        cycle_reports.append(
            {
                "cycle_index": cycle_index,
                "cycle_run_id": cycle_run_id,
                "entry_ledger": entry_ledger,
                "entry_router": entry_router,
                "cumulative_entry_router": cumulative_router,
                "risk_loop": risk_loop,
                "cycle_summary": summary,
            }
        )
        if dedupe_across_cycles:
            accumulated_ledger_payload = entry_ledger
            accumulated_router_payload = _aggregate_paper_entry_routers(
                cumulative_router_payloads,
                aggregate_run_id=f"{cycle_run_id}_dedupe_existing_router",
            )
    final_summary = cycle_summaries[-1] if cycle_summaries else {}
    return {
        "paper_multi_cycle_runner_version": "paper-multi-cycle-runner-v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "multi_cycle_run_id": run_id,
        "cycle_count_requested": int(cycle_count),
        "cycle_count_completed": len(cycle_reports),
        "dedupe_across_cycles": bool(dedupe_across_cycles),
        "cycle_summaries": cycle_summaries,
        "sample_cycle_reports": cycle_reports[: max(0, int(sample_limit))],
        "entry_ledgers": entry_ledgers,
        "entry_routers": entry_routers,
        "final_risk_loop": final_risk_loop,
        "next_portfolio_risk_state": (final_risk_loop or {}).get("portfolio_risk_state"),
        "next_entry_gate": (final_risk_loop or {}).get("next_entry_gate"),
        "multi_cycle_summary": {
            "final_risk_state": final_summary.get("post_cycle_risk_state"),
            "final_gate_action": final_summary.get("post_cycle_gate_action"),
            "final_gross_exposure": final_summary.get("post_cycle_gross_exposure"),
            "final_short_exposure": final_summary.get("post_cycle_short_exposure"),
            "final_total_pnl": final_summary.get("post_cycle_total_pnl"),
            "total_new_paper_fills": sum(int(summary.get("paper_fills") or 0) for summary in cycle_summaries),
            "final_cumulative_paper_fills": final_summary.get("cumulative_paper_fills"),
            "release_governor_applied_count": sum(1 for summary in cycle_summaries if summary.get("session_transition_release_governor_applied")),
            "release_governor_blocked_entry_intent_count": sum(int(summary.get("release_governor_blocked_entry_intent_count") or 0) for summary in cycle_summaries),
            "release_governor_router_blocked_count": sum(
                1
                for summary in cycle_summaries
                if summary.get("session_transition_release_governor_applied") and not summary.get("session_transition_release_governor_allowed")
            ),
            "operating_final_preflight_applied_count": sum(1 for summary in cycle_summaries if summary.get("operating_final_preflight_applied")),
            "operating_final_preflight_blocked_count": sum(
                1
                for summary in cycle_summaries
                if summary.get("operating_final_preflight_applied") and not summary.get("operating_final_preflight_allowed")
            ),
            "any_reduce_risk": any(summary.get("post_cycle_risk_state") == "reduce_risk" for summary in cycle_summaries),
            "any_monitor": any(summary.get("post_cycle_risk_state") == "monitor" for summary in cycle_summaries),
        },
        "safety": {
            "mode": "paper_multi_cycle_runner",
            "policy": "Runs repeated paper-only cycles with risk-state handoff and cumulative paper portfolio accounting. No broker API call, order submission, transmit, or real portfolio mutation.",
            "ibkr_order_submitted": False,
            "transmit_enabled": False,
            "live_order_routing_enabled": False,
        },
    }


def paper_multi_cycle_runner_markdown(
    result: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
    title: str = "Paper Multi-Cycle Runner",
) -> str:
    filters = filters or {}
    summary = result.get("multi_cycle_summary") or {}
    lines = [
        f"# {title}",
        "",
        f"- Generated at: `{result.get('generated_at')}`",
        f"- Multi-cycle run ID: `{result.get('multi_cycle_run_id')}`",
        f"- Cycles requested: `{result.get('cycle_count_requested')}`",
        f"- Cycles completed: `{result.get('cycle_count_completed')}`",
        f"- Dedupe across cycles: `{result.get('dedupe_across_cycles')}`",
        f"- Total new paper fills: `{summary.get('total_new_paper_fills')}`",
        f"- Final cumulative paper fills: `{summary.get('final_cumulative_paper_fills')}`",
        f"- Release governor applied / router-blocked cycles: `{summary.get('release_governor_applied_count')}` / `{summary.get('release_governor_router_blocked_count')}`",
        f"- Release-governor blocked entry intents: `{summary.get('release_governor_blocked_entry_intent_count')}`",
        f"- Operating final preflight applied/blocked cycles: `{summary.get('operating_final_preflight_applied_count')}` / `{summary.get('operating_final_preflight_blocked_count')}`",
        f"- Final risk state: `{summary.get('final_risk_state')}`",
        f"- Final gate action: `{summary.get('final_gate_action')}`",
        f"- Final gross exposure: `{_fmt_money(summary.get('final_gross_exposure'))}`",
        f"- Final short exposure: `{_fmt_money(summary.get('final_short_exposure'))}`",
        f"- Final total PnL: `{_fmt_money(summary.get('final_total_pnl'))}`",
        "- Safety: paper multi-cycle only; no broker order API call or transmit.",
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
            "## Cycle history",
            "",
            "| Cycle | Initial risk | Gate | Release | New fills | Cumulative fills | Post risk | Exposure | Total PnL |",
            "| ---: | --- | --- | --- | ---: | ---: | --- | ---: | ---: |",
        ]
    )
    for row in result.get("cycle_summaries") or []:
        lines.append(
            f"| {row.get('cycle_index')} | {row.get('initial_risk_state')} | {row.get('entry_gate_action')} | {row.get('entry_release_status') or '-'} | {row.get('paper_fills')} | {row.get('cumulative_paper_fills')} | {row.get('post_cycle_risk_state')} | {_fmt_money(row.get('post_cycle_gross_exposure'))} | {_fmt_money(row.get('post_cycle_total_pnl'))} |"
        )
    if not result.get("cycle_summaries"):
        lines.append("| - | - | - | - | - | - | - | - | - |")
    lines.extend(
        [
            "",
            "## Next-cycle handoff",
            "",
            "Use `next_portfolio_risk_state`, or the optional next-risk component JSON output, as the next multi-cycle run's starting risk input.",
            "",
            "## Interpretation note",
            "",
            "M128 repeats the M127 paper cycle with risk-state handoff. The cumulative paper router lets post-cycle risk evaluate the aggregate paper portfolio instead of only the newest fills.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_paper_multi_cycle_runner_markdown(
    output_path: Path,
    result: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(paper_multi_cycle_runner_markdown(result, filters=filters), encoding="utf-8")
    return output_path


def build_paper_cycle_scheduler_report(
    auto_gate_payload: dict[str, Any],
    initial_portfolio_risk_state_payload: dict[str, Any],
    fill_marks_payload: dict[str, Any],
    loop_mark_snapshot_payloads: list[dict[str, Any]],
    *,
    existing_ledger_payload: dict[str, Any] | None = None,
    existing_router_payload: dict[str, Any] | None = None,
    exit_router_payload: dict[str, Any] | None = None,
    operating_auto_gate_payload: dict[str, Any] | None = None,
    session_transition_release_governor_payload: dict[str, Any] | None = None,
    require_operating_preflight: bool = False,
    allow_non_operating_source_ledger: bool = False,
    require_session_transition_release_governor: bool = False,
    max_runs: int = 3,
    cycles_per_run: int = 1,
    interval_seconds: float = 0.0,
    dedupe_across_cycles: bool = True,
    carry_dedupe_state_across_runs: bool = True,
    monitor_size_multiplier: float = 0.50,
    max_intents: int = 10,
    fill_price_source: str = "require_marks",
    fill_model: str = "ibkr_mark_reference_fill_v1",
    max_orders: int = 10,
    slippage_bps: float = 0.0,
    commission_per_order: float = 0.0,
    take_profit_pct: float = 0.03,
    stop_loss_pct: float = 0.02,
    max_loss_dollars: float | None = None,
    max_gross_exposure: float = 100000.0,
    max_symbol_exposure: float = 25000.0,
    max_short_exposure: float = 25000.0,
    max_unrealized_loss: float = 1000.0,
    max_total_loss: float = 1000.0,
    max_open_symbols: int = 25,
    warning_ratio: float = 0.80,
    scheduler_run_id: str | None = None,
    resume_prior_runs_completed: int = 0,
    sample_limit: int = 20,
) -> dict[str, Any]:
    run_id = scheduler_run_id or datetime.now(UTC).strftime("paper_cycle_scheduler_%Y%m%dT%H%M%SZ")
    current_risk_payload = _paper_portfolio_risk_state_payload_result(initial_portfolio_risk_state_payload)
    accumulated_ledger_payload = existing_ledger_payload
    accumulated_router_payload = existing_router_payload
    scheduler_runs: list[dict[str, Any]] = []
    tick_summaries: list[dict[str, Any]] = []
    final_next_risk: dict[str, Any] | None = None
    final_next_gate: dict[str, Any] | None = None
    prior_runs_completed = max(0, int(resume_prior_runs_completed or 0))
    for tick_index in range(1, max(0, int(max_runs)) + 1):
        scheduler_tick_index = prior_runs_completed + tick_index
        tick_run_id = f"{run_id}_tick_{scheduler_tick_index:02d}"
        tick_report = build_paper_multi_cycle_runner_report(
            auto_gate_payload,
            current_risk_payload,
            fill_marks_payload,
            loop_mark_snapshot_payloads,
            existing_ledger_payload=accumulated_ledger_payload if carry_dedupe_state_across_runs else existing_ledger_payload,
            existing_router_payload=accumulated_router_payload if carry_dedupe_state_across_runs else existing_router_payload,
            exit_router_payload=exit_router_payload,
            operating_auto_gate_payload=operating_auto_gate_payload,
            session_transition_release_governor_payload=session_transition_release_governor_payload,
            require_operating_preflight=require_operating_preflight,
            allow_non_operating_source_ledger=allow_non_operating_source_ledger,
            require_session_transition_release_governor=require_session_transition_release_governor,
            cycle_count=cycles_per_run,
            dedupe_across_cycles=dedupe_across_cycles,
            monitor_size_multiplier=monitor_size_multiplier,
            max_intents=max_intents,
            fill_price_source=fill_price_source,
            fill_model=fill_model,
            max_orders=max_orders,
            slippage_bps=slippage_bps,
            commission_per_order=commission_per_order,
            take_profit_pct=take_profit_pct,
            stop_loss_pct=stop_loss_pct,
            max_loss_dollars=max_loss_dollars,
            max_gross_exposure=max_gross_exposure,
            max_symbol_exposure=max_symbol_exposure,
            max_short_exposure=max_short_exposure,
            max_unrealized_loss=max_unrealized_loss,
            max_total_loss=max_total_loss,
            max_open_symbols=max_open_symbols,
            warning_ratio=warning_ratio,
            multi_cycle_run_id=tick_run_id,
            sample_limit=sample_limit,
        )
        scheduler_runs.append(tick_report)
        final_next_risk = tick_report.get("next_portfolio_risk_state") or {}
        final_next_gate = tick_report.get("next_entry_gate") or {}
        current_risk_payload = final_next_risk
        if carry_dedupe_state_across_runs:
            ledgers = list(tick_report.get("entry_ledgers") or [])
            routers = list(tick_report.get("entry_routers") or [])
            if ledgers:
                accumulated_ledger_payload = ledgers[-1]
            if routers:
                prior_routers: list[dict[str, Any]] = []
                if accumulated_router_payload is not None:
                    prior_routers.append(accumulated_router_payload)
                prior_routers.extend(routers)
                accumulated_router_payload = _aggregate_paper_entry_routers(
                    prior_routers,
                    aggregate_run_id=f"{tick_run_id}_scheduler_existing_router",
                )
        multi_summary = tick_report.get("multi_cycle_summary") or {}
        tick_summaries.append(
            {
                "tick_index": tick_index,
                "scheduler_tick_index": scheduler_tick_index,
                "tick_run_id": tick_run_id,
                "scheduled_interval_seconds": float(interval_seconds),
                "cycles_completed": tick_report.get("cycle_count_completed"),
                "total_new_paper_fills": multi_summary.get("total_new_paper_fills"),
                "final_cumulative_paper_fills": multi_summary.get("final_cumulative_paper_fills"),
                "release_governor_applied_count": multi_summary.get("release_governor_applied_count"),
                "release_governor_blocked_entry_intent_count": multi_summary.get("release_governor_blocked_entry_intent_count"),
                "release_governor_router_blocked_count": multi_summary.get("release_governor_router_blocked_count"),
                "operating_final_preflight_applied_count": multi_summary.get("operating_final_preflight_applied_count"),
                "operating_final_preflight_blocked_count": multi_summary.get("operating_final_preflight_blocked_count"),
                "final_risk_state": multi_summary.get("final_risk_state"),
                "final_gate_action": multi_summary.get("final_gate_action"),
                "final_gross_exposure": multi_summary.get("final_gross_exposure"),
                "final_short_exposure": multi_summary.get("final_short_exposure"),
                "final_total_pnl": multi_summary.get("final_total_pnl"),
            }
        )
    scheduler_summary = tick_summaries[-1] if tick_summaries else {}
    runs_completed_total = prior_runs_completed + len(scheduler_runs)
    resume_state = {
        "paper_cycle_scheduler_resume_state_version": "paper-cycle-scheduler-resume-state-v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "source_scheduler_run_id": run_id,
        "runs_completed_this_invocation": len(scheduler_runs),
        "runs_completed_total": runs_completed_total,
        "next_scheduler_tick_index": runs_completed_total + 1,
        "next_portfolio_risk_state": final_next_risk,
        "next_entry_gate": final_next_gate,
        "existing_ledger": accumulated_ledger_payload,
        "existing_router": accumulated_router_payload,
        "last_tick_summary": scheduler_summary,
    }
    return {
        "paper_cycle_scheduler_version": "paper-cycle-scheduler-v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "scheduler_run_id": run_id,
        "max_runs_requested": int(max_runs),
        "runs_completed": len(scheduler_runs),
        "resume_prior_runs_completed": prior_runs_completed,
        "runs_completed_total": runs_completed_total,
        "cycles_per_run": int(cycles_per_run),
        "interval_seconds": float(interval_seconds),
        "dedupe_across_cycles": bool(dedupe_across_cycles),
        "carry_dedupe_state_across_runs": bool(carry_dedupe_state_across_runs),
        "tick_summaries": tick_summaries,
        "sample_scheduler_runs": scheduler_runs[: max(0, int(sample_limit))],
        "final_scheduler_summary": {
            "final_risk_state": scheduler_summary.get("final_risk_state"),
            "final_gate_action": scheduler_summary.get("final_gate_action"),
            "final_gross_exposure": scheduler_summary.get("final_gross_exposure"),
            "final_short_exposure": scheduler_summary.get("final_short_exposure"),
            "final_total_pnl": scheduler_summary.get("final_total_pnl"),
            "total_new_paper_fills": sum(int(row.get("total_new_paper_fills") or 0) for row in tick_summaries),
            "final_cumulative_paper_fills": scheduler_summary.get("final_cumulative_paper_fills"),
            "release_governor_applied_count": sum(int(row.get("release_governor_applied_count") or 0) for row in tick_summaries),
            "release_governor_blocked_entry_intent_count": sum(int(row.get("release_governor_blocked_entry_intent_count") or 0) for row in tick_summaries),
            "release_governor_router_blocked_count": sum(int(row.get("release_governor_router_blocked_count") or 0) for row in tick_summaries),
            "operating_final_preflight_applied_count": sum(int(row.get("operating_final_preflight_applied_count") or 0) for row in tick_summaries),
            "operating_final_preflight_blocked_count": sum(int(row.get("operating_final_preflight_blocked_count") or 0) for row in tick_summaries),
        },
        "next_portfolio_risk_state": final_next_risk,
        "next_entry_gate": final_next_gate,
        "resume_state": resume_state,
        "safety": {
            "mode": "paper_cycle_scheduler",
            "policy": "Runs finite scheduled paper-only cycle batches with risk-state handoff. No broker API call, order submission, transmit, or real portfolio mutation.",
            "ibkr_order_submitted": False,
            "transmit_enabled": False,
            "live_order_routing_enabled": False,
        },
    }


def extract_paper_cycle_scheduler_resume_state(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not payload:
        return {}
    candidate = payload.get("result") if isinstance(payload.get("result"), dict) else payload
    if isinstance(candidate.get("resume_state"), dict):
        return candidate.get("resume_state") or {}
    if candidate.get("paper_cycle_scheduler_checkpoint_version"):
        return {
            "paper_cycle_scheduler_resume_state_version": "paper-cycle-scheduler-resume-state-v1",
            "generated_at": candidate.get("generated_at"),
            "source_scheduler_run_id": candidate.get("source_scheduler_run_id"),
            "runs_completed_this_invocation": candidate.get("runs_completed_this_invocation"),
            "runs_completed_total": candidate.get("runs_completed_total"),
            "next_scheduler_tick_index": candidate.get("next_scheduler_tick_index"),
            "next_portfolio_risk_state": candidate.get("next_portfolio_risk_state"),
            "next_entry_gate": candidate.get("next_entry_gate"),
            "existing_ledger": candidate.get("existing_ledger"),
            "existing_router": candidate.get("existing_router"),
            "last_tick_summary": candidate.get("last_tick_summary"),
        }
    if candidate.get("paper_cycle_scheduler_resume_state_version"):
        return candidate
    return {}


def _preflight_parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except (TypeError, ValueError):
        return None


def _preflight_latest_mark_timestamp(row: dict[str, Any]) -> datetime | None:
    candidates: list[datetime] = []
    mark_timestamp = _preflight_parse_dt(row.get("mark_timestamp"))
    if mark_timestamp is not None:
        candidates.append(mark_timestamp)
    for tick in row.get("received_ticks") or []:
        if not isinstance(tick, dict):
            continue
        tick_timestamp = _preflight_parse_dt(tick.get("received_at"))
        if tick_timestamp is not None:
            candidates.append(tick_timestamp)
    return max(candidates) if candidates else None


def build_paper_data_quality_preflight_report(
    *,
    fill_marks_payload: dict[str, Any],
    loop_mark_payloads: list[dict[str, Any]] | None = None,
    now: str | datetime | None = None,
    max_snapshot_age_seconds: float = 28800.0,
    max_tick_age_seconds: float | None = None,
    allow_delayed: bool = False,
    require_all_marked: bool = True,
    min_marked_ratio: float = 1.0,
    enabled: bool = True,
) -> dict[str, Any]:
    now_dt = now.astimezone(UTC) if isinstance(now, datetime) else _preflight_parse_dt(now)
    if now_dt is None:
        now_dt = datetime.now(UTC)
    max_tick_age = float(max_tick_age_seconds if max_tick_age_seconds is not None else max_snapshot_age_seconds)
    snapshots = [("fill_marks", fill_marks_payload)]
    for index, payload in enumerate(loop_mark_payloads or [], start=1):
        snapshots.append((f"loop_marks_{index}", payload))
    snapshot_summaries: list[dict[str, Any]] = []
    block_reasons: list[str] = []
    warn_reasons: list[str] = []
    total_rows = total_marked = total_missing = total_delayed = total_stale_rows = 0
    for role, payload in snapshots:
        generated_at = _preflight_parse_dt(payload.get("generated_at"))
        snapshot_age_seconds = (now_dt - generated_at).total_seconds() if generated_at is not None else None
        rows = list(payload.get("marks") or [])
        marked_rows = [
            row
            for row in rows
            if str(row.get("mark_status") or "").lower() == "marked" and row.get("mark_price") not in (None, "")
        ]
        missing_rows = [row for row in rows if row not in marked_rows]
        delayed_rows = [
            row
            for row in rows
            if bool(row.get("delayed")) or row.get("market_data_type") in {3, 4}
        ]
        stale_rows = []
        for row in rows:
            latest = _preflight_latest_mark_timestamp(row)
            if latest is None:
                stale_rows.append(row)
                continue
            if (now_dt - latest).total_seconds() > max_tick_age:
                stale_rows.append(row)
        marked_ratio = (len(marked_rows) / len(rows)) if rows else 0.0
        snapshot_reasons: list[str] = []
        if generated_at is None:
            snapshot_reasons.append("missing_snapshot_generated_at")
        elif snapshot_age_seconds is not None and snapshot_age_seconds > float(max_snapshot_age_seconds):
            snapshot_reasons.append("stale_snapshot")
        if require_all_marked and missing_rows:
            snapshot_reasons.append("missing_marks")
        if marked_ratio < float(min_marked_ratio):
            snapshot_reasons.append("marked_ratio_below_minimum")
        if delayed_rows and not allow_delayed:
            snapshot_reasons.append("delayed_market_data_not_allowed")
        if stale_rows:
            snapshot_reasons.append("stale_mark_ticks")
        total_rows += len(rows)
        total_marked += len(marked_rows)
        total_missing += len(missing_rows)
        total_delayed += len(delayed_rows)
        total_stale_rows += len(stale_rows)
        snapshot_summaries.append(
            {
                "role": role,
                "snapshot_version": payload.get("stock_mark_snapshot_version"),
                "generated_at": payload.get("generated_at"),
                "snapshot_age_seconds": round(snapshot_age_seconds, 3) if snapshot_age_seconds is not None else None,
                "row_count": len(rows),
                "marked_count": len(marked_rows),
                "missing_mark_count": len(missing_rows),
                "delayed_count": len(delayed_rows),
                "stale_mark_count": len(stale_rows),
                "marked_ratio": round(marked_ratio, 4),
                "market_data_type_labels": sorted({str(row.get("market_data_type_label") or "unknown") for row in rows}),
                "mark_sources": sorted({str(row.get("mark_source") or "unknown") for row in rows}),
                "reasons": snapshot_reasons,
            }
        )
        for reason in snapshot_reasons:
            target = block_reasons if reason in {
                "missing_snapshot_generated_at",
                "stale_snapshot",
                "missing_marks",
                "marked_ratio_below_minimum",
                "delayed_market_data_not_allowed",
                "stale_mark_ticks",
            } else warn_reasons
            target.append(f"{role}:{reason}")
    overall_marked_ratio = (total_marked / total_rows) if total_rows else 0.0
    gate_status = "disabled"
    allowed = True
    if enabled:
        gate_status = "block" if block_reasons else ("warn" if warn_reasons else "pass")
        allowed = gate_status in {"pass", "warn"}
    return {
        "paper_data_quality_preflight_version": "paper-data-quality-preflight-v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "enabled": bool(enabled),
        "allowed": bool(allowed),
        "gate_status": gate_status,
        "reason": "; ".join(block_reasons or warn_reasons) or ("Preflight gate disabled." if not enabled else "All configured data quality checks passed."),
        "evaluated_at": now_dt.isoformat(),
        "config": {
            "max_snapshot_age_seconds": float(max_snapshot_age_seconds),
            "max_tick_age_seconds": float(max_tick_age),
            "allow_delayed": bool(allow_delayed),
            "require_all_marked": bool(require_all_marked),
            "min_marked_ratio": float(min_marked_ratio),
        },
        "summary": {
            "snapshot_count": len(snapshot_summaries),
            "row_count": total_rows,
            "marked_count": total_marked,
            "missing_mark_count": total_missing,
            "delayed_count": total_delayed,
            "stale_mark_count": total_stale_rows,
            "marked_ratio": round(overall_marked_ratio, 4),
            "block_reason_count": len(block_reasons),
            "warn_reason_count": len(warn_reasons),
        },
        "block_reasons": block_reasons,
        "warn_reasons": warn_reasons,
        "snapshots": snapshot_summaries,
        "safety": {
            "mode": "paper_data_quality_preflight",
            "policy": "Evaluates existing paper market-data snapshots only. No broker order API call, order submission, transmit, or real portfolio mutation.",
            "ibkr_order_submitted": False,
            "transmit_enabled": False,
            "live_order_routing_enabled": False,
        },
    }


def paper_data_quality_preflight_markdown(result: dict[str, Any]) -> str:
    summary = result.get("summary") or {}
    lines = [
        "# Paper Data Quality Preflight Report",
        "",
        f"- Generated at: `{result.get('generated_at')}`",
        f"- Enabled: `{result.get('enabled')}`",
        f"- Gate status: `{result.get('gate_status')}`",
        f"- Allowed: `{result.get('allowed')}`",
        f"- Reason: `{result.get('reason')}`",
        f"- Evaluated at: `{result.get('evaluated_at')}`",
        f"- Snapshots: `{summary.get('snapshot_count')}`",
        f"- Rows: `{summary.get('row_count')}`",
        f"- Marked ratio: `{summary.get('marked_ratio')}`",
        f"- Missing marks: `{summary.get('missing_mark_count')}`",
        f"- Delayed rows: `{summary.get('delayed_count')}`",
        f"- Stale mark rows: `{summary.get('stale_mark_count')}`",
        "",
        "## Snapshot checks",
        "",
        "| Role | Generated at | Age seconds | Rows | Marked | Missing | Delayed | Stale | Status reasons |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in result.get("snapshots") or []:
        lines.append(
            f"| {row.get('role')} | `{row.get('generated_at') or '-'}` | {row.get('snapshot_age_seconds')} | {row.get('row_count')} | {row.get('marked_count')} | {row.get('missing_mark_count')} | {row.get('delayed_count')} | {row.get('stale_mark_count')} | {', '.join(row.get('reasons') or []) or '-'} |"
        )
    if not result.get("snapshots"):
        lines.append("| - | - | - | - | - | - | - | - | - |")
    lines.extend(
        [
            "",
            "## Interpretation note",
            "",
            "M137 checks the paper market-data snapshots already on disk before scheduler execution. It does not request market data, submit orders, or mutate a real portfolio.",
        ]
    )
    return "\n".join(lines) + "\n"


def build_paper_cycle_scheduler_checkpoint(
    scheduler_report_payload: dict[str, Any],
    *,
    checkpoint_run_id: str | None = None,
) -> dict[str, Any]:
    report = scheduler_report_payload.get("result") if isinstance(scheduler_report_payload.get("result"), dict) else scheduler_report_payload
    resume_state = extract_paper_cycle_scheduler_resume_state(report)
    checkpoint_id = checkpoint_run_id or f"{report.get('scheduler_run_id') or 'paper_cycle_scheduler'}_checkpoint"
    return {
        "paper_cycle_scheduler_checkpoint_version": "paper-cycle-scheduler-checkpoint-v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "checkpoint_run_id": checkpoint_id,
        "source_scheduler_run_id": report.get("scheduler_run_id") or resume_state.get("source_scheduler_run_id"),
        "source_scheduler_version": report.get("paper_cycle_scheduler_version"),
        "runs_completed_this_invocation": report.get("runs_completed"),
        "runs_completed_total": resume_state.get("runs_completed_total", report.get("runs_completed_total")),
        "next_scheduler_tick_index": resume_state.get("next_scheduler_tick_index"),
        "next_portfolio_risk_state": resume_state.get("next_portfolio_risk_state") or report.get("next_portfolio_risk_state"),
        "next_entry_gate": resume_state.get("next_entry_gate") or report.get("next_entry_gate"),
        "existing_ledger": resume_state.get("existing_ledger"),
        "existing_router": resume_state.get("existing_router"),
        "last_tick_summary": resume_state.get("last_tick_summary") or (list(report.get("tick_summaries") or [])[-1] if report.get("tick_summaries") else {}),
        "final_scheduler_summary": report.get("final_scheduler_summary") or {},
        "safety": {
            "mode": "paper_cycle_scheduler_checkpoint",
            "policy": "Stores paper-only scheduler resume state for restart-safe continuation. No broker order API call, order submission, transmit, or real portfolio mutation.",
            "ibkr_order_submitted": False,
            "transmit_enabled": False,
            "live_order_routing_enabled": False,
        },
    }


def build_paper_cycle_watch_tick_report(
    *,
    watch_run_id: str,
    input_snapshot: dict[str, Any],
    action: str,
    scheduler_result: dict[str, Any] | None = None,
    checkpoint: dict[str, Any] | None = None,
    skipped_reason: str | None = None,
    session_gate: dict[str, Any] | None = None,
    preflight_gate: dict[str, Any] | None = None,
    evidence_refresh_watchdog: dict[str, Any] | None = None,
    evidence_preflight_gate: dict[str, Any] | None = None,
    market_data_risk_gate: dict[str, Any] | None = None,
    mark_refresh: dict[str, Any] | None = None,
) -> dict[str, Any]:
    scheduler_result = scheduler_result or {}
    checkpoint = checkpoint or {}
    summary = scheduler_result.get("final_scheduler_summary") or {}
    checkpoint_final_summary = checkpoint.get("final_scheduler_summary") or {}
    if summary:
        display_summary = summary
    elif checkpoint_final_summary:
        display_summary = dict(checkpoint_final_summary)
        display_summary["total_new_paper_fills"] = 0
    else:
        display_summary = {}
    return {
        "paper_cycle_watch_tick_version": "paper-cycle-watch-tick-v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "watch_run_id": watch_run_id,
        "action": action,
        "skipped_reason": skipped_reason,
        "session_gate": session_gate,
        "preflight_gate": preflight_gate,
        "evidence_refresh_watchdog": evidence_refresh_watchdog,
        "evidence_preflight_gate": evidence_preflight_gate,
        "market_data_risk_gate": market_data_risk_gate,
        "mark_refresh": mark_refresh,
        "input_snapshot": input_snapshot,
        "scheduler_run_id": scheduler_result.get("scheduler_run_id"),
        "runs_completed": scheduler_result.get("runs_completed", 0),
        "runs_completed_total": scheduler_result.get("runs_completed_total") or checkpoint.get("runs_completed_total"),
        "next_scheduler_tick_index": (scheduler_result.get("resume_state") or {}).get("next_scheduler_tick_index")
        or checkpoint.get("next_scheduler_tick_index"),
        "final_scheduler_summary": display_summary,
        "checkpoint_summary": {
            "checkpoint_run_id": checkpoint.get("checkpoint_run_id"),
            "source_scheduler_run_id": checkpoint.get("source_scheduler_run_id"),
            "runs_completed_total": checkpoint.get("runs_completed_total"),
            "next_scheduler_tick_index": checkpoint.get("next_scheduler_tick_index"),
            "final_risk_state": checkpoint_final_summary.get("final_risk_state") or summary.get("final_risk_state"),
            "final_cumulative_paper_fills": checkpoint_final_summary.get("final_cumulative_paper_fills")
            or summary.get("final_cumulative_paper_fills"),
        },
        "scheduler_result": scheduler_result if scheduler_result else None,
        "checkpoint": checkpoint if checkpoint else None,
        "safety": {
            "mode": "paper_cycle_watch_tick",
            "policy": "Runs at most one finite paper scheduler tick per watch invocation and persists paper-only checkpoint state atomically when requested. No broker order API call, order submission, transmit, or real portfolio mutation.",
            "ibkr_order_submitted": False,
            "transmit_enabled": False,
            "live_order_routing_enabled": False,
        },
    }




def write_paper_cycle_watch_tick_markdown(
    output_path: Path,
    result: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(paper_cycle_watch_tick_markdown(result, filters=filters), encoding="utf-8")
    return output_path


def _mark_refresh_quality_metrics_from_tick(result: dict[str, Any]) -> dict[str, Any]:
    mark_refresh = result.get("mark_refresh") or {}
    policy_eval = mark_refresh.get("policy_evaluation") or {}
    policy_summary = policy_eval.get("summary") or {}
    fallback = policy_eval.get("fallback") or {}
    execution_action = mark_refresh.get("execution_quality_action") or {}
    snapshot = mark_refresh.get("snapshot") or {}
    missing_count = int(policy_summary.get("missing_mark_count") or snapshot.get("missing_mark_count") or 0)
    delayed_count = int(policy_summary.get("delayed_count") or snapshot.get("delayed_count") or 0)
    fallback_symbols = fallback.get("applied_symbols") or []
    return {
        "mark_refresh_status": mark_refresh.get("status"),
        "mark_refresh_allowed": mark_refresh.get("allowed"),
        "mark_refresh_policy_status": policy_eval.get("status"),
        "mark_refresh_policy_allowed": policy_eval.get("allowed"),
        "mark_refresh_quality_action": execution_action.get("action"),
        "mark_refresh_quality_action_applied": execution_action.get("applied"),
        "effective_monitor_size_multiplier": execution_action.get("effective_monitor_size_multiplier"),
        "mark_refresh_missing_mark_count": missing_count,
        "mark_refresh_delayed_count": delayed_count,
        "mark_refresh_fallback_applied_symbol_count": len(fallback_symbols),
        "mark_refresh_fallback_applied_symbols": fallback_symbols,
        "mark_refresh_fallback_reason": fallback.get("reason"),
    }


def _summarize_mark_refresh_quality(rows: list[dict[str, Any]]) -> dict[str, Any]:
    refresh_status_counts: Counter[str] = Counter()
    policy_status_counts: Counter[str] = Counter()
    quality_action_counts: Counter[str] = Counter()
    fallback_symbol_counts: Counter[str] = Counter()
    fallback_tick_count = 0
    delayed_row_count = 0
    missing_mark_count = 0
    quality_action_applied_count = 0
    observed_tick_count = 0
    for row in rows:
        status = row.get("mark_refresh_status")
        if status:
            observed_tick_count += 1
            refresh_status_counts[str(status)] += 1
        policy_status = row.get("mark_refresh_policy_status")
        if policy_status:
            policy_status_counts[str(policy_status)] += 1
        action = row.get("mark_refresh_quality_action")
        if action:
            quality_action_counts[str(action)] += 1
        if row.get("mark_refresh_quality_action_applied"):
            quality_action_applied_count += 1
        delayed_row_count += int(row.get("mark_refresh_delayed_count") or 0)
        missing_mark_count += int(row.get("mark_refresh_missing_mark_count") or 0)
        symbols = row.get("mark_refresh_fallback_applied_symbols") or []
        if symbols:
            fallback_tick_count += 1
            for symbol in symbols:
                fallback_symbol_counts[str(symbol)] += 1
    return {
        "observed_tick_count": observed_tick_count,
        "refresh_status_counts": dict(sorted(refresh_status_counts.items())),
        "policy_status_counts": dict(sorted(policy_status_counts.items())),
        "quality_action_counts": dict(sorted(quality_action_counts.items())),
        "quality_action_applied_count": quality_action_applied_count,
        "delayed_row_count": delayed_row_count,
        "missing_mark_count": missing_mark_count,
        "fallback_tick_count": fallback_tick_count,
        "fallback_symbol_counts": dict(sorted(fallback_symbol_counts.items())),
    }


def _combine_mark_refresh_quality_summaries(summaries: list[dict[str, Any]]) -> dict[str, Any]:
    refresh_status_counts: Counter[str] = Counter()
    policy_status_counts: Counter[str] = Counter()
    quality_action_counts: Counter[str] = Counter()
    fallback_symbol_counts: Counter[str] = Counter()
    combined = {
        "observed_tick_count": 0,
        "quality_action_applied_count": 0,
        "delayed_row_count": 0,
        "missing_mark_count": 0,
        "fallback_tick_count": 0,
    }
    for summary in summaries:
        combined["observed_tick_count"] += int(summary.get("observed_tick_count") or 0)
        combined["quality_action_applied_count"] += int(summary.get("quality_action_applied_count") or 0)
        combined["delayed_row_count"] += int(summary.get("delayed_row_count") or 0)
        combined["missing_mark_count"] += int(summary.get("missing_mark_count") or 0)
        combined["fallback_tick_count"] += int(summary.get("fallback_tick_count") or 0)
        refresh_status_counts.update({str(k): int(v or 0) for k, v in (summary.get("refresh_status_counts") or {}).items()})
        policy_status_counts.update({str(k): int(v or 0) for k, v in (summary.get("policy_status_counts") or {}).items()})
        quality_action_counts.update({str(k): int(v or 0) for k, v in (summary.get("quality_action_counts") or {}).items()})
        fallback_symbol_counts.update({str(k): int(v or 0) for k, v in (summary.get("fallback_symbol_counts") or {}).items()})
    return {
        **combined,
        "refresh_status_counts": dict(sorted(refresh_status_counts.items())),
        "policy_status_counts": dict(sorted(policy_status_counts.items())),
        "quality_action_counts": dict(sorted(quality_action_counts.items())),
        "fallback_symbol_counts": dict(sorted(fallback_symbol_counts.items())),
    }


def build_paper_cycle_watch_runner_report(
    *,
    runner_run_id: str,
    tick_results: list[dict[str, Any]],
    max_ticks_requested: int,
    interval_seconds: float = 0.0,
    sleep_between_ticks: bool = False,
    profile_json: str | None = None,
) -> dict[str, Any]:
    tick_summaries: list[dict[str, Any]] = []
    for index, result in enumerate(tick_results, start=1):
        summary = result.get("final_scheduler_summary") or {}
        session_gate = result.get("session_gate") or {}
        preflight_gate = result.get("preflight_gate") or {}
        evidence_refresh_watchdog = result.get("evidence_refresh_watchdog") or {}
        evidence_preflight_gate = result.get("evidence_preflight_gate") or {}
        market_data_risk_gate = result.get("market_data_risk_gate") or {}
        mark_refresh = result.get("mark_refresh") or {}
        refresh_metrics = _mark_refresh_quality_metrics_from_tick(result)
        tick_summaries.append(
            {
                "tick_index": index,
                "watch_run_id": result.get("watch_run_id"),
                "action": result.get("action"),
                "session": session_gate.get("session"),
                "session_allowed": session_gate.get("allowed"),
                **refresh_metrics,
                "preflight_status": preflight_gate.get("gate_status"),
                "preflight_allowed": preflight_gate.get("allowed"),
                "evidence_refresh_watchdog_status": evidence_refresh_watchdog.get("final_status"),
                "evidence_refresh_freshness_status": evidence_refresh_watchdog.get("final_freshness_status"),
                "evidence_preflight_status": evidence_preflight_gate.get("gate_status"),
                "evidence_preflight_action": evidence_preflight_gate.get("action"),
                "evidence_preflight_allowed": evidence_preflight_gate.get("allowed"),
                "evidence_entry_size_multiplier": evidence_preflight_gate.get("entry_size_multiplier"),
                "market_data_risk_status": market_data_risk_gate.get("gate_status"),
                "market_data_risk_action": market_data_risk_gate.get("action"),
                "market_data_risk_allowed": market_data_risk_gate.get("allowed"),
                "market_data_entry_size_multiplier": market_data_risk_gate.get("entry_size_multiplier"),
                "runs_completed": result.get("runs_completed"),
                "runs_completed_total": result.get("runs_completed_total"),
                "next_scheduler_tick_index": result.get("next_scheduler_tick_index"),
                "total_new_paper_fills": summary.get("total_new_paper_fills"),
                "final_cumulative_paper_fills": summary.get("final_cumulative_paper_fills"),
                "operating_final_preflight_applied_count": summary.get("operating_final_preflight_applied_count"),
                "operating_final_preflight_blocked_count": summary.get("operating_final_preflight_blocked_count"),
                "final_risk_state": summary.get("final_risk_state"),
                "final_gate_action": summary.get("final_gate_action"),
                "final_total_pnl": summary.get("final_total_pnl"),
            }
        )
    executed_count = sum(1 for row in tick_summaries if row.get("action") == "executed_scheduler_tick")
    skipped_count = sum(1 for row in tick_summaries if str(row.get("action") or "").startswith("skipped"))
    session_skipped_count = sum(1 for row in tick_summaries if row.get("action") == "skipped_session_gate")
    preflight_skipped_count = sum(1 for row in tick_summaries if row.get("action") == "skipped_preflight_gate")
    mark_refresh_skipped_count = sum(1 for row in tick_summaries if row.get("action") == "skipped_mark_refresh_gate")
    mark_refresh_quality_summary = _summarize_mark_refresh_quality(tick_summaries)
    evidence_refresh_watchdog_status_counts = Counter(str(row.get("evidence_refresh_watchdog_status") or "not_configured") for row in tick_summaries)
    evidence_preflight_action_counts = Counter(str(row.get("evidence_preflight_action") or "not_configured") for row in tick_summaries)
    market_data_risk_action_counts = Counter(str(row.get("market_data_risk_action") or "not_configured") for row in tick_summaries)
    final_tick = tick_summaries[-1] if tick_summaries else {}
    return {
        "paper_cycle_watch_runner_version": "paper-cycle-watch-runner-v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "runner_run_id": runner_run_id,
        "profile_json": profile_json,
        "max_ticks_requested": int(max_ticks_requested),
        "ticks_completed": len(tick_summaries),
        "interval_seconds": float(interval_seconds),
        "sleep_between_ticks": bool(sleep_between_ticks),
        "executed_tick_count": executed_count,
        "skipped_tick_count": skipped_count,
        "session_skipped_tick_count": session_skipped_count,
        "preflight_skipped_tick_count": preflight_skipped_count,
        "mark_refresh_skipped_tick_count": mark_refresh_skipped_count,
        "mark_refresh_quality_summary": mark_refresh_quality_summary,
        "evidence_refresh_watchdog_status_counts": dict(sorted(evidence_refresh_watchdog_status_counts.items())),
        "evidence_preflight_action_counts": dict(sorted(evidence_preflight_action_counts.items())),
        "market_data_risk_action_counts": dict(sorted(market_data_risk_action_counts.items())),
        "tick_summaries": tick_summaries,
        "final_runner_summary": {
            "final_action": final_tick.get("action"),
            "final_runs_completed_total": final_tick.get("runs_completed_total"),
            "final_next_scheduler_tick_index": final_tick.get("next_scheduler_tick_index"),
            "total_new_paper_fills": sum(int(row.get("total_new_paper_fills") or 0) for row in tick_summaries),
            "final_cumulative_paper_fills": final_tick.get("final_cumulative_paper_fills"),
            "operating_final_preflight_applied_count": sum(int(row.get("operating_final_preflight_applied_count") or 0) for row in tick_summaries),
            "operating_final_preflight_blocked_count": sum(int(row.get("operating_final_preflight_blocked_count") or 0) for row in tick_summaries),
            "final_risk_state": final_tick.get("final_risk_state"),
            "final_gate_action": final_tick.get("final_gate_action"),
            "final_evidence_refresh_watchdog_status": final_tick.get("evidence_refresh_watchdog_status"),
            "final_evidence_refresh_freshness_status": final_tick.get("evidence_refresh_freshness_status"),
            "final_evidence_preflight_action": final_tick.get("evidence_preflight_action"),
            "final_evidence_entry_size_multiplier": final_tick.get("evidence_entry_size_multiplier"),
            "final_market_data_risk_action": final_tick.get("market_data_risk_action"),
            "final_market_data_entry_size_multiplier": final_tick.get("market_data_entry_size_multiplier"),
            "final_total_pnl": final_tick.get("final_total_pnl"),
        },
        "safety": {
            "mode": "paper_cycle_watch_runner",
            "policy": "Runs a finite sequence of M131 paper watch ticks using profile/checkpoint handoff. No broker order API call, order submission, transmit, or real portfolio mutation.",
            "ibkr_order_submitted": False,
            "transmit_enabled": False,
            "live_order_routing_enabled": False,
        },
    }


def paper_cycle_watch_runner_markdown(
    result: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
    title: str = "Paper Cycle Watch Runner Report",
) -> str:
    filters = filters or {}
    summary = result.get("final_runner_summary") or {}
    quality_summary = result.get("mark_refresh_quality_summary") or {}
    lines = [
        f"# {title}",
        "",
        f"- Generated at: `{result.get('generated_at')}`",
        f"- Runner run ID: `{result.get('runner_run_id')}`",
        f"- Profile JSON: `{result.get('profile_json') or '-'}`",
        f"- Ticks requested: `{result.get('max_ticks_requested')}`",
        f"- Ticks completed: `{result.get('ticks_completed')}`",
        f"- Executed ticks: `{result.get('executed_tick_count')}`",
        f"- Skipped ticks: `{result.get('skipped_tick_count')}`",
        f"- Session-gated skipped ticks: `{result.get('session_skipped_tick_count')}`",
        f"- Preflight-gated skipped ticks: `{result.get('preflight_skipped_tick_count')}`",
        f"- Mark-refresh skipped ticks: `{result.get('mark_refresh_skipped_tick_count')}`",
        f"- Mark-refresh quality observed ticks: `{quality_summary.get('observed_tick_count')}`",
        f"- Mark-refresh status counts: `{quality_summary.get('refresh_status_counts') or {}}`",
        f"- Mark-refresh policy status counts: `{quality_summary.get('policy_status_counts') or {}}`",
        f"- Mark-refresh quality action counts: `{quality_summary.get('quality_action_counts') or {}}`",
        f"- Mark-refresh delayed rows: `{quality_summary.get('delayed_row_count')}`",
        f"- Mark-refresh missing marks: `{quality_summary.get('missing_mark_count')}`",
        f"- Mark-refresh fallback ticks: `{quality_summary.get('fallback_tick_count')}`",
        f"- Evidence refresh watchdog status counts: `{result.get('evidence_refresh_watchdog_status_counts') or {}}`",
        f"- Evidence preflight action counts: `{result.get('evidence_preflight_action_counts') or {}}`",
        f"- Market data risk action counts: `{result.get('market_data_risk_action_counts') or {}}`",
        f"- Interval seconds: `{result.get('interval_seconds')}`",
        f"- Sleep between ticks: `{result.get('sleep_between_ticks')}`",
        f"- Total new paper fills: `{summary.get('total_new_paper_fills')}`",
        f"- Final cumulative paper fills: `{summary.get('final_cumulative_paper_fills')}`",
        f"- Operating final preflight applied/blocked cycles: `{summary.get('operating_final_preflight_applied_count')}` / `{summary.get('operating_final_preflight_blocked_count')}`",
        f"- Final risk state: `{summary.get('final_risk_state')}`",
        f"- Final gate action: `{summary.get('final_gate_action')}`",
        f"- Final evidence refresh watchdog: `{summary.get('final_evidence_refresh_watchdog_status') or '-'}` / freshness `{summary.get('final_evidence_refresh_freshness_status') or '-'}`",
        f"- Final evidence preflight action: `{summary.get('final_evidence_preflight_action') or '-'}`",
        f"- Final evidence size multiplier: `{summary.get('final_evidence_entry_size_multiplier') if summary.get('final_evidence_entry_size_multiplier') is not None else '-'}`",
        f"- Final market data risk action: `{summary.get('final_market_data_risk_action') or '-'}`",
        f"- Final market data size multiplier: `{summary.get('final_market_data_entry_size_multiplier') if summary.get('final_market_data_entry_size_multiplier') is not None else '-'}`",
        "- Safety: finite paper watch runner only; no broker order API call or transmit.",
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
            "## Tick history",
            "",
            "| Tick | Watch run ID | Action | Session | Refresh | Quality action | Preflight | Evidence refresh | Evidence action | Evidence size | Operating preflight | Delayed | Missing | Fallback symbols | New fills | Cumulative fills | Risk | Next index |",
            "| ---: | --- | --- | --- | --- | --- | --- | --- | --- | ---: | --- | ---: | ---: | --- | ---: | ---: | --- | ---: |",
        ]
    )
    for row in result.get("tick_summaries") or []:
        operating_preflight_cell = f"{row.get('operating_final_preflight_applied_count') or 0}/{row.get('operating_final_preflight_blocked_count') or 0}"
        lines.append(
            f"| {row.get('tick_index')} | `{row.get('watch_run_id')}` | {row.get('action')} | {row.get('session') or '-'} | {row.get('mark_refresh_status') or '-'} | {row.get('mark_refresh_quality_action') or '-'} | {row.get('preflight_status') or '-'} | {row.get('evidence_refresh_watchdog_status') or '-'} | {row.get('evidence_preflight_action') or '-'} | {row.get('evidence_entry_size_multiplier') if row.get('evidence_entry_size_multiplier') is not None else '-'} | {operating_preflight_cell} | {row.get('mark_refresh_delayed_count') or 0} | {row.get('mark_refresh_missing_mark_count') or 0} | {', '.join(row.get('mark_refresh_fallback_applied_symbols') or []) or '-'} | {row.get('total_new_paper_fills')} | {row.get('final_cumulative_paper_fills')} | {row.get('final_risk_state')} | {row.get('next_scheduler_tick_index')} |"
        )
    if not result.get("tick_summaries"):
        lines.append("| - | - | - | - | - | - | - | - | - | - | - | - | - | - | - | - | - | - |")
    lines.extend(
        [
            "",
            "## Interpretation note",
            "",
            "M133 repeats the M132/M131 profile-backed watch tick a finite number of times. M134/M135 can gate ticks by configured market session/calendar before any paper scheduler tick is executed. M138-M142 mark refresh, when enabled, runs after the session gate and before M137 data-quality preflight.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_paper_cycle_watch_runner_markdown(
    output_path: Path,
    result: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(paper_cycle_watch_runner_markdown(result, filters=filters), encoding="utf-8")
    return output_path


def build_paper_cycle_watch_daemon_report(
    *,
    daemon_run_id: str,
    batch_results: list[dict[str, Any]],
    max_batches_requested: int,
    ticks_per_batch: int,
    batch_interval_seconds: float = 0.0,
    sleep_between_batches: bool = False,
    profile_json: str | None = None,
    daily_log_jsonl: str | None = None,
) -> dict[str, Any]:
    batch_summaries: list[dict[str, Any]] = []
    for index, result in enumerate(batch_results, start=1):
        summary = result.get("final_runner_summary") or {}
        quality_summary = result.get("mark_refresh_quality_summary") or {}
        batch_summaries.append(
            {
                "batch_index": index,
                "runner_run_id": result.get("runner_run_id"),
                "ticks_completed": result.get("ticks_completed"),
                "executed_tick_count": result.get("executed_tick_count"),
                "skipped_tick_count": result.get("skipped_tick_count"),
                "session_skipped_tick_count": result.get("session_skipped_tick_count"),
                "preflight_skipped_tick_count": result.get("preflight_skipped_tick_count"),
                "mark_refresh_skipped_tick_count": result.get("mark_refresh_skipped_tick_count"),
                "mark_refresh_quality_summary": quality_summary,
                "final_action": summary.get("final_action"),
                "total_new_paper_fills": summary.get("total_new_paper_fills"),
                "final_cumulative_paper_fills": summary.get("final_cumulative_paper_fills"),
                "operating_final_preflight_applied_count": summary.get("operating_final_preflight_applied_count"),
                "operating_final_preflight_blocked_count": summary.get("operating_final_preflight_blocked_count"),
                "final_risk_state": summary.get("final_risk_state"),
                "final_gate_action": summary.get("final_gate_action"),
                "final_total_pnl": summary.get("final_total_pnl"),
            }
        )
    final_batch = batch_summaries[-1] if batch_summaries else {}
    mark_refresh_quality_summary = _combine_mark_refresh_quality_summaries(
        [row.get("mark_refresh_quality_summary") or {} for row in batch_summaries]
    )
    return {
        "paper_cycle_watch_daemon_version": "paper-cycle-watch-daemon-v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "daemon_run_id": daemon_run_id,
        "profile_json": profile_json,
        "max_batches_requested": int(max_batches_requested),
        "batches_completed": len(batch_summaries),
        "ticks_per_batch": int(ticks_per_batch),
        "batch_interval_seconds": float(batch_interval_seconds),
        "sleep_between_batches": bool(sleep_between_batches),
        "daily_log_jsonl": daily_log_jsonl,
        "executed_tick_count": sum(int(row.get("executed_tick_count") or 0) for row in batch_summaries),
        "skipped_tick_count": sum(int(row.get("skipped_tick_count") or 0) for row in batch_summaries),
        "session_skipped_tick_count": sum(int(row.get("session_skipped_tick_count") or 0) for row in batch_summaries),
        "preflight_skipped_tick_count": sum(int(row.get("preflight_skipped_tick_count") or 0) for row in batch_summaries),
        "mark_refresh_skipped_tick_count": sum(int(row.get("mark_refresh_skipped_tick_count") or 0) for row in batch_summaries),
        "mark_refresh_quality_summary": mark_refresh_quality_summary,
        "batch_summaries": batch_summaries,
        "final_daemon_summary": {
            "final_action": final_batch.get("final_action"),
            "total_new_paper_fills": sum(int(row.get("total_new_paper_fills") or 0) for row in batch_summaries),
            "final_cumulative_paper_fills": final_batch.get("final_cumulative_paper_fills"),
            "operating_final_preflight_applied_count": sum(int(row.get("operating_final_preflight_applied_count") or 0) for row in batch_summaries),
            "operating_final_preflight_blocked_count": sum(int(row.get("operating_final_preflight_blocked_count") or 0) for row in batch_summaries),
            "final_risk_state": final_batch.get("final_risk_state"),
            "final_gate_action": final_batch.get("final_gate_action"),
            "final_total_pnl": final_batch.get("final_total_pnl"),
        },
        "safety": {
            "mode": "paper_cycle_watch_daemon",
            "policy": "Runs finite batches of M133/M135 paper watch runners with checkpoint handoff. No broker order API call, order submission, transmit, or real portfolio mutation.",
            "ibkr_order_submitted": False,
            "transmit_enabled": False,
            "live_order_routing_enabled": False,
        },
    }


def paper_cycle_watch_daemon_markdown(
    result: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
    title: str = "Paper Cycle Watch Daemon Report",
) -> str:
    filters = filters or {}
    summary = result.get("final_daemon_summary") or {}
    quality_summary = result.get("mark_refresh_quality_summary") or {}
    lines = [
        f"# {title}",
        "",
        f"- Generated at: `{result.get('generated_at')}`",
        f"- Daemon run ID: `{result.get('daemon_run_id')}`",
        f"- Profile JSON: `{result.get('profile_json') or '-'}`",
        f"- Batches requested: `{result.get('max_batches_requested')}`",
        f"- Batches completed: `{result.get('batches_completed')}`",
        f"- Ticks per batch: `{result.get('ticks_per_batch')}`",
        f"- Executed ticks: `{result.get('executed_tick_count')}`",
        f"- Skipped ticks: `{result.get('skipped_tick_count')}`",
        f"- Session-gated skipped ticks: `{result.get('session_skipped_tick_count')}`",
        f"- Preflight-gated skipped ticks: `{result.get('preflight_skipped_tick_count')}`",
        f"- Mark-refresh skipped ticks: `{result.get('mark_refresh_skipped_tick_count')}`",
        f"- Mark-refresh quality observed ticks: `{quality_summary.get('observed_tick_count')}`",
        f"- Mark-refresh status counts: `{quality_summary.get('refresh_status_counts') or {}}`",
        f"- Mark-refresh policy status counts: `{quality_summary.get('policy_status_counts') or {}}`",
        f"- Mark-refresh quality action counts: `{quality_summary.get('quality_action_counts') or {}}`",
        f"- Mark-refresh delayed rows: `{quality_summary.get('delayed_row_count')}`",
        f"- Mark-refresh missing marks: `{quality_summary.get('missing_mark_count')}`",
        f"- Mark-refresh fallback ticks: `{quality_summary.get('fallback_tick_count')}`",
        f"- Batch interval seconds: `{result.get('batch_interval_seconds')}`",
        f"- Sleep between batches: `{result.get('sleep_between_batches')}`",
        f"- Daily log JSONL: `{result.get('daily_log_jsonl') or '-'}`",
        f"- Total new paper fills: `{summary.get('total_new_paper_fills')}`",
        f"- Final cumulative paper fills: `{summary.get('final_cumulative_paper_fills')}`",
        f"- Operating final preflight applied/blocked cycles: `{summary.get('operating_final_preflight_applied_count')}` / `{summary.get('operating_final_preflight_blocked_count')}`",
        f"- Final risk state: `{summary.get('final_risk_state')}`",
        f"- Final gate action: `{summary.get('final_gate_action')}`",
        "- Safety: finite paper watch daemon wrapper only; no broker order API call or transmit.",
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
            "## Batch history",
            "",
            "| Batch | Runner run ID | Ticks | Executed | Skipped | Session skipped | Refresh skipped | Preflight skipped | Operating preflight | Refresh statuses | Quality actions | Delayed | Missing | New fills | Cumulative fills | Risk | Action |",
            "| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- | --- | ---: | ---: | ---: | ---: | --- | --- |",
        ]
    )
    for row in result.get("batch_summaries") or []:
        batch_quality = row.get("mark_refresh_quality_summary") or {}
        operating_preflight_cell = f"{row.get('operating_final_preflight_applied_count') or 0}/{row.get('operating_final_preflight_blocked_count') or 0}"
        lines.append(
            f"| {row.get('batch_index')} | `{row.get('runner_run_id')}` | {row.get('ticks_completed')} | {row.get('executed_tick_count')} | {row.get('skipped_tick_count')} | {row.get('session_skipped_tick_count')} | {row.get('mark_refresh_skipped_tick_count')} | {row.get('preflight_skipped_tick_count')} | {operating_preflight_cell} | {batch_quality.get('refresh_status_counts') or {}} | {batch_quality.get('quality_action_counts') or {}} | {batch_quality.get('delayed_row_count') or 0} | {batch_quality.get('missing_mark_count') or 0} | {row.get('total_new_paper_fills')} | {row.get('final_cumulative_paper_fills')} | {row.get('final_risk_state')} | {row.get('final_action')} |"
        )
    if not result.get("batch_summaries"):
        lines.append("| - | - | - | - | - | - | - | - | - | - | - | - | - | - | - | - | - |")
    lines.extend(
        [
            "",
            "## Interpretation note",
            "",
            "M136 wraps the M133/M135 profile-backed watch runner into finite daemon-style batches. Calendar/session gates still run before any paper scheduler tick, M138-M142 mark refresh can update or policy-fallback paper mark snapshots, aggregate live quality telemetry, and apply paper-only quality sizing overlays before M137 preflight/scheduler execution; checkpoint/watch-state handoff remains the duplicate-control boundary.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_paper_cycle_watch_daemon_markdown(
    output_path: Path,
    result: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(paper_cycle_watch_daemon_markdown(result, filters=filters), encoding="utf-8")
    return output_path


def paper_cycle_scheduler_markdown(
    result: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
    title: str = "Paper Cycle Scheduler / Daemon Report",
) -> str:
    filters = filters or {}
    summary = result.get("final_scheduler_summary") or {}
    lines = [
        f"# {title}",
        "",
        f"- Generated at: `{result.get('generated_at')}`",
        f"- Scheduler run ID: `{result.get('scheduler_run_id')}`",
        f"- Runs requested: `{result.get('max_runs_requested')}`",
        f"- Runs completed: `{result.get('runs_completed')}`",
        f"- Resume prior runs completed: `{result.get('resume_prior_runs_completed')}`",
        f"- Runs completed total: `{result.get('runs_completed_total')}`",
        f"- Cycles per run: `{result.get('cycles_per_run')}`",
        f"- Interval seconds: `{result.get('interval_seconds')}`",
        f"- Dedupe across cycles: `{result.get('dedupe_across_cycles')}`",
        f"- Carry dedupe state across runs: `{result.get('carry_dedupe_state_across_runs')}`",
        f"- Total new paper fills: `{summary.get('total_new_paper_fills')}`",
        f"- Final cumulative paper fills: `{summary.get('final_cumulative_paper_fills')}`",
        f"- Operating final preflight applied/blocked cycles: `{summary.get('operating_final_preflight_applied_count')}` / `{summary.get('operating_final_preflight_blocked_count')}`",
        f"- Final risk state: `{summary.get('final_risk_state')}`",
        f"- Final gate action: `{summary.get('final_gate_action')}`",
        f"- Final gross exposure: `{_fmt_money(summary.get('final_gross_exposure'))}`",
        f"- Final short exposure: `{_fmt_money(summary.get('final_short_exposure'))}`",
        f"- Final total PnL: `{_fmt_money(summary.get('final_total_pnl'))}`",
        "- Safety: paper scheduler only; no broker order API call or transmit.",
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
            "## Tick history",
            "",
            "| Tick | Runs | New fills | Cumulative fills | Final risk | Exposure | Total PnL |",
            "| ---: | ---: | ---: | ---: | --- | ---: | ---: |",
        ]
    )
    for row in result.get("tick_summaries") or []:
        lines.append(
            f"| {row.get('tick_index')} | {row.get('cycles_completed')} | {row.get('total_new_paper_fills')} | {row.get('final_cumulative_paper_fills')} | {row.get('final_risk_state')} | {_fmt_money(row.get('final_gross_exposure'))} | {_fmt_money(row.get('final_total_pnl'))} |"
        )
    if not result.get("tick_summaries"):
        lines.append("| - | - | - | - | - | - | - |")
    lines.extend(
        [
            "",
            "## Next-cycle handoff",
            "",
            "Use `next_portfolio_risk_state`, the optional next-risk JSON output, or the M130 checkpoint output as the next scheduler run's starting state.",
            "",
            "## Interpretation note",
            "",
            "M129 is a finite paper-only scheduler/daemon wrapper around M128. M130 adds restart-safe checkpoint handoff for the latest risk, ledger, router, and tick index state without touching real orders.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_paper_cycle_scheduler_markdown(
    output_path: Path,
    result: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(paper_cycle_scheduler_markdown(result, filters=filters), encoding="utf-8")
    return output_path


def account_backed_portfolio_readiness_markdown(
    result: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
    title: str = "Paper Account-Backed Portfolio Readiness Report",
) -> str:
    lines = mock_portfolio_readiness_markdown(result, filters=filters, title=title).splitlines()
    account = result.get("account_snapshot") or {}
    insert_at = 10 if len(lines) > 10 else len(lines)
    account_lines = [
        "",
        "## Read-only account snapshot input",
        "",
        f"- Snapshot generated at: `{account.get('generated_at')}`",
        f"- Selected account: `{account.get('selected_account')}`",
        f"- Raw equity: `{_fmt_money(account.get('raw_equity'))}`",
        f"- Raw cash: `{_fmt_money(account.get('raw_cash'))}`",
        f"- Buying power: `{_fmt_money(account.get('buying_power'))}`",
        f"- Effective equity after haircut: `{_fmt_money(account.get('effective_equity'))}`",
        f"- Effective cash after haircut: `{_fmt_money(account.get('effective_cash'))}`",
        f"- Positions seen: `{account.get('position_count')}`",
        f"- Open orders seen: `{account.get('open_order_count')}`",
        f"- Snapshot errors/warnings: `{account.get('errors_count')}`",
    ]
    lines[insert_at:insert_at] = account_lines
    return "\n".join(lines) + "\n"


def write_account_backed_portfolio_readiness_markdown(
    output_path: Path,
    result: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(account_backed_portfolio_readiness_markdown(result, filters=filters), encoding="utf-8")
    return output_path
