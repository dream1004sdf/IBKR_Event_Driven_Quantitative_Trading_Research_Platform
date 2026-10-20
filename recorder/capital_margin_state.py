from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


CAPITAL_MARGIN_STATE_VERSION = "m238-capital-withdrawal-guard-v1"

CORE_ACCOUNT_TAGS = (
    "NetLiquidation",
    "TotalCashValue",
    "AvailableFunds",
    "BuyingPower",
    "ExcessLiquidity",
    "InitMarginReq",
    "MaintMarginReq",
)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


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


def _safe_ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or denominator == 0:
        return None
    return numerator / denominator


def _first_available(*values: float | None) -> float | None:
    for value in values:
        if value is not None:
            return value
    return None


def _selected_account_row(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    accounts = [row for row in snapshot.get("accounts", []) if isinstance(row, dict)]
    selected_account = snapshot.get("selected_account")
    if selected_account:
        for row in accounts:
            if row.get("account") == selected_account:
                return row
    if accounts:
        return max(
            accounts,
            key=lambda row: _as_float(((row.get("tags") or {}).get("NetLiquidation") or {}).get("numeric_value")) or float("-inf"),
        )
    return None


def _tag_value(tags: dict[str, Any], tag: str) -> float | None:
    return _as_float(tags.get(tag))


def _snapshot_age_seconds(snapshot: dict[str, Any], now: datetime) -> float | None:
    generated_at = snapshot.get("generated_at")
    if not generated_at:
        return None
    try:
        parsed = datetime.fromisoformat(str(generated_at).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return max(0.0, (now - parsed.astimezone(UTC)).total_seconds())


def _position_counts(snapshot: dict[str, Any], account: str | None) -> dict[str, int]:
    positions = [row for row in snapshot.get("positions", []) if isinstance(row, dict)]
    if account:
        positions = [row for row in positions if row.get("account") in (None, account)]
    option_count = 0
    stock_count = 0
    other_count = 0
    for row in positions:
        contract = row.get("contract") if isinstance(row.get("contract"), dict) else {}
        sec_type = str(contract.get("sec_type") or contract.get("secType") or "").upper()
        if sec_type == "OPT":
            option_count += 1
        elif sec_type in {"STK", "ETF"}:
            stock_count += 1
        else:
            other_count += 1
    return {
        "position_count": len(positions),
        "stock_position_count": stock_count,
        "option_position_count": option_count,
        "other_position_count": other_count,
    }


def _worst_status(*statuses: str) -> str:
    rank = {"green": 0, "yellow": 1, "red": 2}
    return max(statuses, key=lambda status: rank.get(status, 0))


def _status_from_metrics(metrics: dict[str, float | None], warnings: list[str]) -> tuple[str, list[str]]:
    reasons: list[str] = []
    net_liq = metrics.get("net_liquidation")
    available_ratio = metrics.get("available_funds_ratio")
    excess_ratio = metrics.get("excess_liquidity_ratio")
    maint_ratio = metrics.get("maintenance_margin_ratio")
    if net_liq is None or net_liq <= 0:
        return "red", ["missing_or_non_positive_net_liquidation"]
    if "missing_core_account_tags" in warnings:
        reasons.append("core_account_tags_incomplete")
    if "stale_account_snapshot" in warnings or "missing_account_snapshot" in warnings:
        reasons.append("account_snapshot_not_fresh")
    if available_ratio is not None and available_ratio < 0.05:
        reasons.append("available_funds_below_5pct_net_liq")
    if excess_ratio is not None and excess_ratio < 0.10:
        reasons.append("excess_liquidity_below_10pct_net_liq")
    if maint_ratio is not None and maint_ratio > 0.75:
        reasons.append("maintenance_margin_above_75pct_net_liq")
    if reasons:
        return "red", reasons

    if available_ratio is not None and available_ratio < 0.10:
        reasons.append("available_funds_below_10pct_net_liq")
    if excess_ratio is not None and excess_ratio < 0.20:
        reasons.append("excess_liquidity_below_20pct_net_liq")
    if maint_ratio is not None and maint_ratio > 0.50:
        reasons.append("maintenance_margin_above_50pct_net_liq")
    if reasons:
        return "yellow", reasons
    return "green", ["capital_margin_buffer_normal"]


def _leverage_mode(metrics: dict[str, float | None], status: str) -> str:
    if status == "red":
        return "restricted_margin"
    buying_power_ratio = metrics.get("buying_power_to_net_liq")
    initial_ratio = metrics.get("initial_margin_ratio")
    if buying_power_ratio is None or buying_power_ratio <= 1.05:
        return "cash_only"
    if status == "yellow":
        return "conservative_margin"
    if initial_ratio is not None and initial_ratio <= 0.10:
        return "cash_or_light_margin"
    return "normal_margin"


def _budget_multiplier(status: str) -> float:
    if status == "red":
        return 0.0
    if status == "yellow":
        return 0.35
    return 1.0


def _build_budgets(
    *,
    metrics: dict[str, float | None],
    status: str,
    leverage_mode: str,
    reserve_fraction: float,
    core_risk_fraction: float,
    options_premium_fraction: float,
    hedge_fraction: float,
    single_position_fraction: float,
    new_margin_fraction: float,
    micro_cash_budget_enabled: bool,
    micro_cash_net_liq_threshold: float,
    micro_cash_option_cash_fraction: float,
    micro_cash_option_net_liq_fraction: float,
    micro_cash_option_budget_cap: float,
) -> dict[str, Any]:
    net_liq = max(0.0, metrics.get("net_liquidation") or 0.0)
    cash = max(0.0, metrics.get("total_cash_value") or 0.0)
    available = max(0.0, metrics.get("available_funds") or 0.0)
    excess = max(0.0, metrics.get("excess_liquidity") or 0.0)
    multiplier = _budget_multiplier(status)
    reserve_cash = net_liq * max(0.0, reserve_fraction)
    deployable_cash_after_reserve = max(0.0, cash - reserve_cash)
    general_core_risk_budget = net_liq * max(0.0, core_risk_fraction) * multiplier
    general_options_premium_budget = min(
        deployable_cash_after_reserve,
        net_liq * max(0.0, options_premium_fraction) * multiplier,
    )
    micro_cash_option_budget_candidate = 0.0
    micro_cash_eligible = (
        bool(micro_cash_budget_enabled)
        and status == "green"
        and leverage_mode == "cash_only"
        and 0.0 < net_liq < max(0.0, micro_cash_net_liq_threshold)
    )
    if micro_cash_eligible:
        cash_fraction_budget = deployable_cash_after_reserve * max(0.0, micro_cash_option_cash_fraction)
        net_liq_fraction_budget = net_liq * max(0.0, micro_cash_option_net_liq_fraction)
        micro_cash_option_budget_candidate = max(
            0.0,
            min(
                cash_fraction_budget,
                net_liq_fraction_budget,
                max(0.0, micro_cash_option_budget_cap),
            ),
        )
    else:
        cash_fraction_budget = 0.0
        net_liq_fraction_budget = 0.0
    options_premium_budget = max(general_options_premium_budget, micro_cash_option_budget_candidate)
    core_risk_budget = max(general_core_risk_budget, options_premium_budget) if micro_cash_eligible else general_core_risk_budget
    max_new_initial_margin = min(
        available * max(0.0, new_margin_fraction) * multiplier,
        excess * 0.50 * multiplier,
    )
    if not micro_cash_budget_enabled:
        micro_reason = "disabled"
    elif status != "green":
        micro_reason = f"capital_status_{status}"
    elif leverage_mode != "cash_only":
        micro_reason = f"leverage_mode_{leverage_mode}"
    elif net_liq <= 0:
        micro_reason = "missing_or_zero_net_liquidation"
    elif net_liq >= max(0.0, micro_cash_net_liq_threshold):
        micro_reason = "above_micro_cash_net_liq_threshold"
    else:
        micro_reason = "eligible"
    return {
        "policy": {
            "reserve_fraction": reserve_fraction,
            "core_risk_fraction": core_risk_fraction,
            "options_premium_fraction": options_premium_fraction,
            "hedge_fraction": hedge_fraction,
            "single_position_fraction": single_position_fraction,
            "new_margin_fraction": new_margin_fraction,
            "status_multiplier": multiplier,
        },
        "budget_model": "micro_cash_overlay" if micro_cash_eligible else "standard_fractional",
        "reserve_cash": round(reserve_cash, 2),
        "deployable_cash_after_reserve": round(deployable_cash_after_reserve, 2),
        "core_risk_budget": round(core_risk_budget, 2),
        "options_premium_budget": round(options_premium_budget, 2),
        "hedge_budget": round(net_liq * max(0.0, hedge_fraction) * max(0.35, multiplier), 2),
        "max_single_position_notional": round(net_liq * max(0.0, single_position_fraction) * multiplier, 2),
        "max_new_initial_margin": round(max_new_initial_margin, 2),
        "micro_cash_budget_overlay": {
            "enabled": bool(micro_cash_budget_enabled),
            "eligible": micro_cash_eligible,
            "reason": micro_reason,
            "general_core_risk_budget": round(general_core_risk_budget, 2),
            "general_options_premium_budget": round(general_options_premium_budget, 2),
            "cash_fraction_budget": round(cash_fraction_budget, 2),
            "net_liq_fraction_budget": round(net_liq_fraction_budget, 2),
            "option_budget_cap": round(max(0.0, micro_cash_option_budget_cap), 2),
            "micro_options_premium_budget_candidate": round(micro_cash_option_budget_candidate, 2),
            "selected_core_risk_budget": round(core_risk_budget, 2),
            "selected_options_premium_budget": round(options_premium_budget, 2),
            "policy": {
                "net_liq_threshold": micro_cash_net_liq_threshold,
                "option_cash_fraction": micro_cash_option_cash_fraction,
                "option_net_liq_fraction": micro_cash_option_net_liq_fraction,
                "option_budget_cap": micro_cash_option_budget_cap,
                "requires_status": "green",
                "requires_leverage_mode": "cash_only",
            },
        },
    }


def _previous_metrics(previous_state: dict[str, Any] | None) -> dict[str, Any] | None:
    if not previous_state:
        return None
    metrics = previous_state.get("metrics")
    if isinstance(metrics, dict):
        return metrics
    account = previous_state.get("account")
    if isinstance(account, dict):
        return account
    return None


def _flow_detection(
    *,
    previous_state: dict[str, Any] | None,
    current_metrics: dict[str, float | None],
    min_cash_flow: float,
    min_cash_flow_fraction: float,
) -> dict[str, Any]:
    previous = _previous_metrics(previous_state)
    if not previous:
        return {
            "status": "baseline_only",
            "external_cash_flow_candidate": False,
            "reason": "No previous M237 state was available before this run.",
        }
    prev_cash = _as_float(previous.get("total_cash_value") or previous.get("cash"))
    prev_net_liq = _as_float(previous.get("net_liquidation") or previous.get("equity"))
    cur_cash = current_metrics.get("total_cash_value")
    cur_net_liq = current_metrics.get("net_liquidation")
    if prev_cash is None or cur_cash is None:
        return {
            "status": "insufficient_cash_fields",
            "external_cash_flow_candidate": False,
            "reason": "Previous or current cash field is unavailable.",
        }
    cash_delta = cur_cash - prev_cash
    net_liq_delta = (cur_net_liq - prev_net_liq) if cur_net_liq is not None and prev_net_liq is not None else None
    threshold = max(min_cash_flow, abs(prev_net_liq or 0.0) * min_cash_flow_fraction)
    external_candidate = abs(cash_delta) >= threshold
    direction = "deposit_candidate" if cash_delta > 0 else "withdrawal_candidate" if cash_delta < 0 else "unchanged"
    return {
        "status": "candidate_detected" if external_candidate else "normal_drift",
        "external_cash_flow_candidate": external_candidate,
        "direction": direction if external_candidate else "none",
        "cash_delta": round(cash_delta, 2),
        "net_liquidation_delta": round(net_liq_delta, 2) if net_liq_delta is not None else None,
        "threshold": round(threshold, 2),
        "heuristic_note": "This is a cash-flow candidate, not a confirmed deposit/withdrawal ledger entry.",
    }


def _bounded_withdrawal(value: float | None) -> float:
    if value is None or not math.isfinite(value):
        return 0.0
    return max(0.0, value)


def _ratio_withdrawal_capacity(metric: float | None, net_liq: float | None, minimum_ratio: float) -> float:
    if metric is None or net_liq is None or minimum_ratio >= 1.0:
        return 0.0
    return (metric - minimum_ratio * net_liq) / (1.0 - minimum_ratio)


def _margin_ratio_withdrawal_capacity(net_liq: float | None, margin_requirement: float | None, maximum_ratio: float) -> float:
    if net_liq is None or margin_requirement is None or maximum_ratio <= 0:
        return 0.0
    return net_liq - (margin_requirement / maximum_ratio)


def _budget_floor_withdrawal_capacity(
    *,
    net_liq: float | None,
    cash: float | None,
    reserve_fraction: float,
    budget_fraction: float,
    minimum_budget: float,
) -> tuple[float, float]:
    if minimum_budget <= 0:
        return float("inf"), float("inf")
    reserve_capacity = 0.0
    if cash is not None and net_liq is not None and reserve_fraction < 1.0:
        reserve_capacity = (cash - reserve_fraction * net_liq - minimum_budget) / (1.0 - reserve_fraction)
    fraction_capacity = 0.0
    if net_liq is not None and budget_fraction > 0:
        fraction_capacity = net_liq - (minimum_budget / budget_fraction)
    return reserve_capacity, fraction_capacity


def _build_withdrawal_guard(
    *,
    metrics: dict[str, float | None],
    status: str,
    status_reasons: list[str],
    reserve_fraction: float,
    core_risk_fraction: float,
    options_premium_fraction: float,
    minimum_operating_net_liq: float,
    minimum_cash_floor: float,
    minimum_available_funds_ratio: float,
    minimum_excess_liquidity_ratio: float,
    maximum_maintenance_margin_ratio: float,
    maximum_initial_margin_ratio: float,
    minimum_options_premium_budget: float,
    minimum_core_risk_budget: float,
) -> dict[str, Any]:
    net_liq = metrics.get("net_liquidation")
    cash = metrics.get("total_cash_value")
    available = metrics.get("available_funds")
    excess = metrics.get("excess_liquidity")
    init_margin = metrics.get("initial_margin_requirement")
    maint_margin = metrics.get("maintenance_margin_requirement")
    constraints = [
        {
            "name": "minimum_operating_net_liquidation",
            "max_withdrawal": _bounded_withdrawal((net_liq or 0.0) - minimum_operating_net_liq),
            "reason": "Keep minimum account equity for any automated/paper operating cycle.",
        },
        {
            "name": "minimum_cash_floor",
            "max_withdrawal": _bounded_withdrawal((cash or 0.0) - minimum_cash_floor),
            "reason": "Keep a cash floor after withdrawal.",
        },
        {
            "name": "cash_reserve_ratio",
            "max_withdrawal": _bounded_withdrawal(
                ((cash or 0.0) - reserve_fraction * (net_liq or 0.0)) / (1.0 - reserve_fraction)
                if reserve_fraction < 1.0
                else 0.0
            ),
            "reason": "Keep reserve cash as a percentage of post-withdrawal net liquidation.",
        },
        {
            "name": "available_funds_ratio",
            "max_withdrawal": _bounded_withdrawal(
                _ratio_withdrawal_capacity(available, net_liq, minimum_available_funds_ratio)
            ),
            "reason": "Keep AvailableFunds above the minimum post-withdrawal ratio.",
        },
        {
            "name": "excess_liquidity_ratio",
            "max_withdrawal": _bounded_withdrawal(
                _ratio_withdrawal_capacity(excess, net_liq, minimum_excess_liquidity_ratio)
            ),
            "reason": "Keep ExcessLiquidity above the minimum post-withdrawal ratio.",
        },
        {
            "name": "maintenance_margin_ratio",
            "max_withdrawal": _bounded_withdrawal(
                _margin_ratio_withdrawal_capacity(net_liq, maint_margin, maximum_maintenance_margin_ratio)
            ),
            "reason": "Keep maintenance margin below the maximum post-withdrawal ratio.",
        },
        {
            "name": "initial_margin_ratio",
            "max_withdrawal": _bounded_withdrawal(
                _margin_ratio_withdrawal_capacity(net_liq, init_margin, maximum_initial_margin_ratio)
            ),
            "reason": "Keep initial margin below the maximum post-withdrawal ratio.",
        },
    ]
    options_reserve_capacity, options_fraction_capacity = _budget_floor_withdrawal_capacity(
        net_liq=net_liq,
        cash=cash,
        reserve_fraction=reserve_fraction,
        budget_fraction=options_premium_fraction,
        minimum_budget=minimum_options_premium_budget,
    )
    core_reserve_capacity, core_fraction_capacity = _budget_floor_withdrawal_capacity(
        net_liq=net_liq,
        cash=cash,
        reserve_fraction=reserve_fraction,
        budget_fraction=core_risk_fraction,
        minimum_budget=minimum_core_risk_budget,
    )
    constraints.extend(
        [
            {
                "name": "minimum_options_premium_cash_capacity",
                "max_withdrawal": _bounded_withdrawal(options_reserve_capacity),
                "reason": "Keep enough deployable cash after reserve for minimum option-premium operation.",
            },
            {
                "name": "minimum_options_premium_fraction_capacity",
                "max_withdrawal": _bounded_withdrawal(options_fraction_capacity),
                "reason": "Keep enough post-withdrawal net liquidation to support the minimum option-premium budget.",
            },
            {
                "name": "minimum_core_risk_cash_capacity",
                "max_withdrawal": _bounded_withdrawal(core_reserve_capacity),
                "reason": "Keep enough deployable cash after reserve for minimum core-risk operation.",
            },
            {
                "name": "minimum_core_risk_fraction_capacity",
                "max_withdrawal": _bounded_withdrawal(core_fraction_capacity),
                "reason": "Keep enough post-withdrawal net liquidation to support the minimum core-risk budget.",
            },
        ]
    )
    finite_constraints = [row for row in constraints if math.isfinite(float(row["max_withdrawal"]))]
    theoretical = min((float(row["max_withdrawal"]) for row in finite_constraints), default=0.0)
    theoretical = _bounded_withdrawal(theoretical)
    for row in constraints:
        row["max_withdrawal"] = round(_bounded_withdrawal(float(row["max_withdrawal"])), 2)
        row["binding"] = abs(row["max_withdrawal"] - round(theoretical, 2)) <= 0.01
    freshness_block = any(reason in set(status_reasons) for reason in ("missing_account_snapshot", "unknown_account_snapshot_age", "stale_account_snapshot"))
    if freshness_block:
        guard_status = "blocked_stale_or_missing_snapshot"
    elif status != "green":
        guard_status = f"blocked_capital_state_{status}"
    elif theoretical <= 0:
        guard_status = "no_safe_withdrawal_capacity"
    else:
        guard_status = "safe_withdrawal_capacity_available"
    max_safe = theoretical if guard_status == "safe_withdrawal_capacity_available" else 0.0
    post_net_liq = (net_liq - max_safe) if net_liq is not None else None
    post_cash = (cash - max_safe) if cash is not None else None
    return {
        "withdrawal_guard_version": "m238-withdrawal-guard-v1",
        "status": guard_status,
        "max_safe_withdrawal": round(max_safe, 2),
        "theoretical_max_withdrawal_if_snapshot_fresh": round(theoretical, 2),
        "requires_fresh_account_snapshot": freshness_block,
        "requires_rebaseline_after_withdrawal": max_safe > 0,
        "minimum_operating_requirements": {
            "minimum_operating_net_liq": minimum_operating_net_liq,
            "minimum_cash_floor": minimum_cash_floor,
            "minimum_available_funds_ratio": minimum_available_funds_ratio,
            "minimum_excess_liquidity_ratio": minimum_excess_liquidity_ratio,
            "maximum_maintenance_margin_ratio": maximum_maintenance_margin_ratio,
            "maximum_initial_margin_ratio": maximum_initial_margin_ratio,
            "minimum_options_premium_budget": minimum_options_premium_budget,
            "minimum_core_risk_budget": minimum_core_risk_budget,
        },
        "binding_constraints": [row for row in constraints if row.get("binding")],
        "constraints": sorted(constraints, key=lambda row: row["max_withdrawal"]),
        "post_withdrawal_at_safe_limit": {
            "net_liquidation": round(post_net_liq, 2) if post_net_liq is not None else None,
            "total_cash_value": round(post_cash, 2) if post_cash is not None else None,
            "cash_to_net_liq_ratio": round(_safe_ratio(post_cash, post_net_liq) or 0.0, 6)
            if post_cash is not None and post_net_liq
            else None,
        },
        "note": "max_safe_withdrawal is zero unless the account snapshot is fresh and the capital state is green.",
    }


def _action_guidance(status: str, leverage_mode: str, flow: dict[str, Any], withdrawal_guard: dict[str, Any]) -> list[str]:
    guidance: list[str] = []
    if status == "red":
        guidance.extend(
            [
                "Block new risk-increasing entries.",
                "Allow hedge/exit/reduce-only review paths only.",
                "Refresh account snapshot and require what-if/margin preview before any routing stage.",
            ]
        )
    elif status == "yellow":
        guidance.extend(
            [
                "Apply sizing haircut and avoid leverage expansion.",
                "Require fresh account snapshot plus what-if/margin preview before paper/live routing stages.",
            ]
        )
    else:
        guidance.extend(
            [
                "Sizing engine may use normal paper/live-read-only budgets.",
                "Live routing remains gated by what-if/margin/option-safety checks.",
            ]
        )
    if leverage_mode in {"cash_only", "restricted_margin"}:
        guidance.append("Prefer cash-defined or premium-defined strategies until margin buffers improve.")
    if flow.get("external_cash_flow_candidate"):
        guidance.append("Rebaseline portfolio budgets after confirming the deposit/withdrawal candidate.")
    if withdrawal_guard.get("max_safe_withdrawal", 0) > 0:
        guidance.append(
            f"Maximum model-safe withdrawal is about ${withdrawal_guard.get('max_safe_withdrawal'):,.2f}; rerun M238 immediately after withdrawal."
        )
    else:
        guidance.append("Do not treat any withdrawal as safe until M238 reports positive fresh withdrawal capacity.")
    return guidance


def build_capital_margin_state(
    *,
    account_snapshot_path: str | Path = Path("exports/m110_ibkr_account_snapshot.json"),
    previous_state_path: str | Path | None = Path("exports/m237_capital_margin_state.json"),
    max_snapshot_age_minutes: int = 30,
    reserve_fraction: float = 0.20,
    core_risk_fraction: float = 0.08,
    options_premium_fraction: float = 0.03,
    hedge_fraction: float = 0.02,
    single_position_fraction: float = 0.12,
    new_margin_fraction: float = 0.25,
    min_cash_flow: float = 100.0,
    min_cash_flow_fraction: float = 0.01,
    minimum_operating_net_liq: float = 500.0,
    minimum_cash_floor: float = 100.0,
    minimum_available_funds_ratio: float = 0.15,
    minimum_excess_liquidity_ratio: float = 0.20,
    maximum_maintenance_margin_ratio: float = 0.35,
    maximum_initial_margin_ratio: float = 0.50,
    minimum_options_premium_budget: float = 25.0,
    minimum_core_risk_budget: float = 25.0,
    micro_cash_budget_enabled: bool = True,
    micro_cash_net_liq_threshold: float = 2000.0,
    micro_cash_option_cash_fraction: float = 0.35,
    micro_cash_option_net_liq_fraction: float = 0.30,
    micro_cash_option_budget_cap: float = 150.0,
    now: datetime | None = None,
) -> dict[str, Any]:
    now_utc = (now or datetime.now(UTC)).astimezone(UTC)
    snapshot_path = Path(account_snapshot_path)
    previous_path = Path(previous_state_path) if previous_state_path else None
    snapshot = _load_json(snapshot_path)
    previous_state = _load_json(previous_path) if previous_path else None
    warnings: list[str] = []
    if snapshot is None:
        warnings.append("missing_account_snapshot")
        snapshot = {}
    account_row = _selected_account_row(snapshot) or {}
    tags = account_row.get("tags") if isinstance(account_row.get("tags"), dict) else {}
    selected_account = account_row.get("account") or snapshot.get("selected_account")
    missing_tags = [tag for tag in CORE_ACCOUNT_TAGS if _tag_value(tags, tag) is None]
    if missing_tags:
        warnings.append("missing_core_account_tags")
    snapshot_age = _snapshot_age_seconds(snapshot, now_utc) if snapshot else None
    if snapshot_age is None:
        warnings.append("unknown_account_snapshot_age")
    elif snapshot_age > max_snapshot_age_minutes * 60:
        warnings.append("stale_account_snapshot")
    metrics: dict[str, float | None] = {
        "net_liquidation": _first_available(_tag_value(tags, "NetLiquidation"), _as_float(snapshot.get("equity"))),
        "total_cash_value": _first_available(_tag_value(tags, "TotalCashValue"), _as_float(snapshot.get("cash"))),
        "available_funds": _tag_value(tags, "AvailableFunds"),
        "buying_power": _first_available(_tag_value(tags, "BuyingPower"), _as_float(snapshot.get("buying_power"))),
        "excess_liquidity": _tag_value(tags, "ExcessLiquidity"),
        "initial_margin_requirement": _tag_value(tags, "InitMarginReq"),
        "maintenance_margin_requirement": _tag_value(tags, "MaintMarginReq"),
    }
    net_liq = metrics["net_liquidation"]
    metrics.update(
        {
            "cash_to_net_liq_ratio": _safe_ratio(metrics["total_cash_value"], net_liq),
            "available_funds_ratio": _safe_ratio(metrics["available_funds"], net_liq),
            "excess_liquidity_ratio": _safe_ratio(metrics["excess_liquidity"], net_liq),
            "initial_margin_ratio": _safe_ratio(metrics["initial_margin_requirement"], net_liq),
            "maintenance_margin_ratio": _safe_ratio(metrics["maintenance_margin_requirement"], net_liq),
            "buying_power_to_net_liq": _safe_ratio(metrics["buying_power"], net_liq),
        }
    )
    metric_status, reasons = _status_from_metrics(metrics, warnings)
    freshness_status = "green"
    if "missing_account_snapshot" in warnings or "unknown_account_snapshot_age" in warnings:
        freshness_status = "red"
    elif "stale_account_snapshot" in warnings:
        freshness_status = "yellow"
    status = _worst_status(metric_status, freshness_status)
    leverage_mode = _leverage_mode(metrics, status)
    budgets = _build_budgets(
        metrics=metrics,
        status=status,
        leverage_mode=leverage_mode,
        reserve_fraction=reserve_fraction,
        core_risk_fraction=core_risk_fraction,
        options_premium_fraction=options_premium_fraction,
        hedge_fraction=hedge_fraction,
        single_position_fraction=single_position_fraction,
        new_margin_fraction=new_margin_fraction,
        micro_cash_budget_enabled=micro_cash_budget_enabled,
        micro_cash_net_liq_threshold=micro_cash_net_liq_threshold,
        micro_cash_option_cash_fraction=micro_cash_option_cash_fraction,
        micro_cash_option_net_liq_fraction=micro_cash_option_net_liq_fraction,
        micro_cash_option_budget_cap=micro_cash_option_budget_cap,
    )
    flow = _flow_detection(
        previous_state=previous_state,
        current_metrics=metrics,
        min_cash_flow=min_cash_flow,
        min_cash_flow_fraction=min_cash_flow_fraction,
    )
    status_reasons = sorted(set(reasons + warnings))
    withdrawal_guard = _build_withdrawal_guard(
        metrics=metrics,
        status=status,
        status_reasons=status_reasons,
        reserve_fraction=reserve_fraction,
        core_risk_fraction=core_risk_fraction,
        options_premium_fraction=options_premium_fraction,
        minimum_operating_net_liq=minimum_operating_net_liq,
        minimum_cash_floor=minimum_cash_floor,
        minimum_available_funds_ratio=minimum_available_funds_ratio,
        minimum_excess_liquidity_ratio=minimum_excess_liquidity_ratio,
        maximum_maintenance_margin_ratio=maximum_maintenance_margin_ratio,
        maximum_initial_margin_ratio=maximum_initial_margin_ratio,
        minimum_options_premium_budget=minimum_options_premium_budget,
        minimum_core_risk_budget=minimum_core_risk_budget,
    )
    counts = _position_counts(snapshot, str(selected_account) if selected_account else None)
    safety = {
        "read_only": True,
        "ibkr_order_submitted": False,
        "broker_connection_opened_by_m237": False,
        "transmit_enabled": False,
        "live_routing_enabled": False,
        "source": "existing_account_snapshot_file",
    }
    return {
        "capital_margin_state_version": CAPITAL_MARGIN_STATE_VERSION,
        "generated_at": now_utc.isoformat(),
        "status": status,
        "status_reasons": status_reasons,
        "leverage_mode": leverage_mode,
        "selected_account": selected_account,
        "account_snapshot_path": str(snapshot_path),
        "previous_state_path": str(previous_path) if previous_path else None,
        "snapshot_generated_at": snapshot.get("generated_at"),
        "snapshot_age_seconds": round(snapshot_age, 2) if snapshot_age is not None else None,
        "max_snapshot_age_minutes": max_snapshot_age_minutes,
        "missing_tags": missing_tags,
        "metrics": {key: round(value, 6) if isinstance(value, float) else value for key, value in metrics.items()},
        "budgets": budgets,
        "withdrawal_guard": withdrawal_guard,
        "cash_flow": flow,
        "positions": counts,
        "action_guidance": _action_guidance(status, leverage_mode, flow, withdrawal_guard),
        "safety": safety,
    }


def _fmt_money(value: Any) -> str:
    parsed = _as_float(value)
    if parsed is None:
        return "n/a"
    return f"${parsed:,.2f}"


def _fmt_pct(value: Any) -> str:
    parsed = _as_float(value)
    if parsed is None:
        return "n/a"
    return f"{parsed * 100:.1f}%"


def capital_margin_state_markdown(report: dict[str, Any]) -> str:
    metrics = report.get("metrics") if isinstance(report.get("metrics"), dict) else {}
    budgets = report.get("budgets") if isinstance(report.get("budgets"), dict) else {}
    micro_overlay = budgets.get("micro_cash_budget_overlay") if isinstance(budgets.get("micro_cash_budget_overlay"), dict) else {}
    withdrawal_guard = report.get("withdrawal_guard") if isinstance(report.get("withdrawal_guard"), dict) else {}
    cash_flow = report.get("cash_flow") if isinstance(report.get("cash_flow"), dict) else {}
    positions = report.get("positions") if isinstance(report.get("positions"), dict) else {}
    lines = [
        "# M237/M238 Capital & Margin State",
        "",
        f"- Generated: `{report.get('generated_at')}`",
        f"- Status: `{report.get('status')}`",
        f"- Leverage mode: `{report.get('leverage_mode')}`",
        f"- Selected account: `{report.get('selected_account')}`",
        f"- Snapshot age: `{report.get('snapshot_age_seconds')}` seconds",
        f"- Safety: read-only=`{(report.get('safety') or {}).get('read_only')}`, order submitted=`{(report.get('safety') or {}).get('ibkr_order_submitted')}`",
        "",
        "## Account Metrics",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
        f"| Net liquidation | {_fmt_money(metrics.get('net_liquidation'))} |",
        f"| Total cash value | {_fmt_money(metrics.get('total_cash_value'))} |",
        f"| Available funds | {_fmt_money(metrics.get('available_funds'))} |",
        f"| Excess liquidity | {_fmt_money(metrics.get('excess_liquidity'))} |",
        f"| Buying power | {_fmt_money(metrics.get('buying_power'))} |",
        f"| Initial margin requirement | {_fmt_money(metrics.get('initial_margin_requirement'))} |",
        f"| Maintenance margin requirement | {_fmt_money(metrics.get('maintenance_margin_requirement'))} |",
        f"| Available funds / net liq | {_fmt_pct(metrics.get('available_funds_ratio'))} |",
        f"| Excess liquidity / net liq | {_fmt_pct(metrics.get('excess_liquidity_ratio'))} |",
        f"| Maintenance margin / net liq | {_fmt_pct(metrics.get('maintenance_margin_ratio'))} |",
        "",
        "## Recommended Budgets",
        "",
        f"- Budget model: `{budgets.get('budget_model', 'standard_fractional')}`",
        f"- Micro cash overlay: enabled=`{micro_overlay.get('enabled')}`, eligible=`{micro_overlay.get('eligible')}`, reason=`{micro_overlay.get('reason')}`",
        "",
        "| Budget | Value |",
        "| --- | ---: |",
        f"| Reserve cash | {_fmt_money(budgets.get('reserve_cash'))} |",
        f"| Deployable cash after reserve | {_fmt_money(budgets.get('deployable_cash_after_reserve'))} |",
        f"| Core risk budget | {_fmt_money(budgets.get('core_risk_budget'))} |",
        f"| Options premium budget | {_fmt_money(budgets.get('options_premium_budget'))} |",
        f"| General core risk budget | {_fmt_money(micro_overlay.get('general_core_risk_budget'))} |",
        f"| General options premium budget | {_fmt_money(micro_overlay.get('general_options_premium_budget'))} |",
        f"| Micro option candidate | {_fmt_money(micro_overlay.get('micro_options_premium_budget_candidate'))} |",
        f"| Hedge budget | {_fmt_money(budgets.get('hedge_budget'))} |",
        f"| Max single-position notional | {_fmt_money(budgets.get('max_single_position_notional'))} |",
        f"| Max new initial margin | {_fmt_money(budgets.get('max_new_initial_margin'))} |",
        "",
        "## M238 Withdrawal Guard",
        "",
        f"- Status: `{withdrawal_guard.get('status')}`",
        f"- Max safe withdrawal now: `{_fmt_money(withdrawal_guard.get('max_safe_withdrawal'))}`",
        f"- Theoretical max if snapshot is fresh: `{_fmt_money(withdrawal_guard.get('theoretical_max_withdrawal_if_snapshot_fresh'))}`",
        f"- Requires fresh account snapshot: `{withdrawal_guard.get('requires_fresh_account_snapshot')}`",
        "",
        "| Binding constraint | Max withdrawal | Reason |",
        "| --- | ---: | --- |",
    ]
    for row in withdrawal_guard.get("binding_constraints", [])[:5]:
        lines.append(f"| `{row.get('name')}` | {_fmt_money(row.get('max_withdrawal'))} | {row.get('reason')} |")
    lines.extend(
        [
            "",
            "## Cash Flow Check",
            "",
            f"- Status: `{cash_flow.get('status')}`",
            f"- Direction: `{cash_flow.get('direction', 'n/a')}`",
            f"- Cash delta: `{_fmt_money(cash_flow.get('cash_delta'))}`",
            f"- External cash-flow candidate: `{cash_flow.get('external_cash_flow_candidate')}`",
            "",
            "## Positions",
            "",
            f"- Stock positions: `{positions.get('stock_position_count', 0)}`",
            f"- Option positions: `{positions.get('option_position_count', 0)}`",
            f"- Other positions: `{positions.get('other_position_count', 0)}`",
            "",
            "## Action Guidance",
            "",
        ]
    )
    for item in report.get("action_guidance", []):
        lines.append(f"- {item}")
    lines.extend(["", "## Status Reasons", ""])
    for item in report.get("status_reasons", []):
        lines.append(f"- `{item}`")
    return "\n".join(lines) + "\n"


def write_capital_margin_state_json(path: Path, report: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def write_capital_margin_state_markdown(path: Path, report: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(capital_margin_state_markdown(report), encoding="utf-8")
    return path
