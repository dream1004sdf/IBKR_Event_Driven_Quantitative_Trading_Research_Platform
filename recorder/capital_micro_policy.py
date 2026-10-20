from __future__ import annotations

import math
from typing import Any


MICRO_CASH_POLICY_VERSION = "m246-micro-cash-only-policy-v2-profiles-risk-cap"

DEFAULT_MIN_OPTION_BUDGET = 100.0
DEFAULT_MIN_SPREAD_BUDGET = 150.0
DEFAULT_MIN_FRACTIONAL_STOCK_CASH = 10.0
DEFAULT_MICRO_NET_LIQ_THRESHOLD = 2_000.0
DEFAULT_MICRO_CASH_BUDGET_THRESHOLD = 500.0
DEFAULT_MICRO_SINGLE_POSITION_THRESHOLD = 250.0
DEFAULT_MICRO_EXIT_NET_LIQ_THRESHOLD = 2_500.0
DEFAULT_MICRO_EXIT_CASH_BUDGET_THRESHOLD = 650.0
DEFAULT_MICRO_EXIT_SINGLE_POSITION_THRESHOLD = 350.0
DEFAULT_MICRO_EXIT_OPTION_BUDGET_THRESHOLD = 150.0
DEFAULT_MICRO_EXIT_CONFIRMATIONS_REQUIRED = 3
DEFAULT_MICRO_POLICY_PROFILE = "micro_paper_exploration"

MICRO_POLICY_PROFILES: dict[str, dict[str, float | str]] = {
    "micro_paper_exploration": {
        "description": "Paper learning profile: allows tiny debit-option experiments while capping one-ticket max loss.",
        "max_option_ticket_net_liq_fraction": 0.25,
        "max_option_ticket_budget_fraction": 0.90,
        "max_option_ticket_debit_floor": 50.0,
        "max_option_ticket_debit_cap": 100.0,
    },
    "micro_paper_execution": {
        "description": "More realistic paper execution profile for small cash accounts.",
        "max_option_ticket_net_liq_fraction": 0.20,
        "max_option_ticket_budget_fraction": 0.75,
        "max_option_ticket_debit_floor": 25.0,
        "max_option_ticket_debit_cap": 75.0,
    },
    "micro_live_defensive": {
        "description": "Live-readiness defensive profile; live routing remains locked elsewhere.",
        "max_option_ticket_net_liq_fraction": 0.10,
        "max_option_ticket_budget_fraction": 0.50,
        "max_option_ticket_debit_floor": 0.0,
        "max_option_ticket_debit_cap": 50.0,
    },
    "normal_capital_mode": {
        "description": "Normal capital mode still keeps per-ticket option loss separated from total option budget.",
        "max_option_ticket_net_liq_fraction": 0.03,
        "max_option_ticket_budget_fraction": 0.50,
        "max_option_ticket_debit_floor": 100.0,
        "max_option_ticket_debit_cap": 1_000.0,
    },
}


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


def _budget(source: dict[str, Any], key: str) -> float | None:
    budgets = source.get("budgets") if isinstance(source.get("budgets"), dict) else {}
    parsed = _as_float(budgets.get(key))
    return max(0.0, parsed) if parsed is not None else None


def _metric(source: dict[str, Any], key: str) -> float | None:
    metrics = source.get("metrics") if isinstance(source.get("metrics"), dict) else {}
    parsed = _as_float(metrics.get(key))
    return parsed if parsed is not None else None


def _is_stock_ticket(ticket_type: str) -> bool:
    return str(ticket_type or "").lower() in {"stock", "stk", "equity", "etf"}


def _is_option_ticket(ticket_type: str, selected_expression: str | None = None) -> bool:
    text = f"{ticket_type or ''} {selected_expression or ''}".lower()
    return "option" in text or "call" in text or "put" in text or "spread" in text


def _is_spread_ticket(ticket_type: str, selected_expression: str | None = None) -> bool:
    text = f"{ticket_type or ''} {selected_expression or ''}".lower()
    return "spread" in text or "combo" in text


def _positive(value: float | None) -> float:
    return max(0.0, float(value or 0.0))


def _previous_mode(previous_policy_state: dict[str, Any] | None) -> str | None:
    if not isinstance(previous_policy_state, dict):
        return None
    nested = previous_policy_state.get("micro_policy_state")
    if isinstance(nested, dict):
        return _previous_mode(nested)
    return str(
        previous_policy_state.get("effective_operating_mode")
        or previous_policy_state.get("operating_mode")
        or previous_policy_state.get("last_effective_operating_mode")
        or ""
    ) or None


def _previous_exit_confirmations(previous_policy_state: dict[str, Any] | None) -> int:
    if not isinstance(previous_policy_state, dict):
        return 0
    nested = previous_policy_state.get("micro_policy_state")
    if isinstance(nested, dict):
        return _previous_exit_confirmations(nested)
    try:
        return max(0, int(previous_policy_state.get("consecutive_exit_confirmations") or 0))
    except (TypeError, ValueError):
        return 0


def _exit_check(label: str, value: float | None, threshold: float) -> dict[str, Any]:
    passed = value is not None and value >= float(threshold)
    return {
        "label": label,
        "value": value,
        "threshold": float(threshold),
        "passed": bool(passed),
    }


def _profile_config(profile: str | None) -> dict[str, float | str]:
    key = str(profile or DEFAULT_MICRO_POLICY_PROFILE).strip() or DEFAULT_MICRO_POLICY_PROFILE
    return dict(MICRO_POLICY_PROFILES.get(key, MICRO_POLICY_PROFILES[DEFAULT_MICRO_POLICY_PROFILE]), profile_name=key)


def _max_option_ticket_debit(
    *,
    profile_config: dict[str, float | str],
    net_liq: float | None,
    options_budget: float,
) -> float:
    if options_budget <= 0:
        return 0.0
    net_liq_fraction = float(profile_config.get("max_option_ticket_net_liq_fraction") or 0.0)
    budget_fraction = float(profile_config.get("max_option_ticket_budget_fraction") or 1.0)
    floor = float(profile_config.get("max_option_ticket_debit_floor") or 0.0)
    cap = float(profile_config.get("max_option_ticket_debit_cap") or options_budget)
    net_liq_cap = (max(0.0, float(net_liq)) * net_liq_fraction) if net_liq is not None else cap
    budget_cap = max(0.0, options_budget) * max(0.0, budget_fraction)
    candidate = max(floor, min(cap, net_liq_cap, budget_cap if budget_cap > 0 else cap))
    return round(min(max(0.0, options_budget), candidate), 2)


def build_micro_cash_policy(
    capital_state: dict[str, Any],
    *,
    previous_policy_state: dict[str, Any] | None = None,
    profile: str = DEFAULT_MICRO_POLICY_PROFILE,
    min_option_budget: float = DEFAULT_MIN_OPTION_BUDGET,
    min_spread_budget: float = DEFAULT_MIN_SPREAD_BUDGET,
    micro_net_liq_threshold: float = DEFAULT_MICRO_NET_LIQ_THRESHOLD,
    micro_cash_budget_threshold: float = DEFAULT_MICRO_CASH_BUDGET_THRESHOLD,
    micro_single_position_threshold: float = DEFAULT_MICRO_SINGLE_POSITION_THRESHOLD,
    micro_exit_net_liq_threshold: float = DEFAULT_MICRO_EXIT_NET_LIQ_THRESHOLD,
    micro_exit_cash_budget_threshold: float = DEFAULT_MICRO_EXIT_CASH_BUDGET_THRESHOLD,
    micro_exit_single_position_threshold: float = DEFAULT_MICRO_EXIT_SINGLE_POSITION_THRESHOLD,
    micro_exit_option_budget_threshold: float = DEFAULT_MICRO_EXIT_OPTION_BUDGET_THRESHOLD,
    micro_exit_confirmations_required: int = DEFAULT_MICRO_EXIT_CONFIRMATIONS_REQUIRED,
    min_single_stock_notional: float = 1.0,
    min_fractional_stock_cash: float = DEFAULT_MIN_FRACTIONAL_STOCK_CASH,
) -> dict[str, Any]:
    """Build a small-account/cash-only execution policy.

    M246 intentionally stays broker-read-only.  It does not size orders or route
    orders; it only gives later paper/live-read-only layers a consistent answer
    about which expressions are even eligible under a very small cash account.
    """

    source = capital_state if isinstance(capital_state, dict) else {}
    status = str(source.get("status") or "unknown")
    leverage_mode = str(source.get("leverage_mode") or "unknown")
    net_liq = _metric(source, "net_liquidation")
    total_cash = _metric(source, "total_cash_value")
    cash_budget = _positive(_budget(source, "deployable_cash_after_reserve"))
    margin_budget = _positive(_budget(source, "max_new_initial_margin"))
    core_risk_budget = _positive(_budget(source, "core_risk_budget"))
    single_position_budget = _positive(_budget(source, "max_single_position_notional"))
    options_budget = _positive(_budget(source, "options_premium_budget"))
    hedge_budget = _positive(_budget(source, "hedge_budget"))
    raw_budgets = source.get("budgets") if isinstance(source.get("budgets"), dict) else {}
    budget_presence = {
        key: key in raw_budgets
        for key in (
            "deployable_cash_after_reserve",
            "max_new_initial_margin",
            "core_risk_budget",
            "max_single_position_notional",
            "options_premium_budget",
            "hedge_budget",
        )
    }
    profile_cfg = _profile_config(profile)
    max_option_ticket_debit = _max_option_ticket_debit(
        profile_config=profile_cfg,
        net_liq=net_liq,
        options_budget=options_budget,
    )

    micro_triggers: list[str] = []
    if leverage_mode == "cash_only":
        if net_liq is not None and net_liq < float(micro_net_liq_threshold):
            micro_triggers.append("net_liquidation_below_micro_threshold")
        if cash_budget < float(micro_cash_budget_threshold):
            micro_triggers.append("cash_budget_below_micro_threshold")
        if single_position_budget and single_position_budget < float(micro_single_position_threshold):
            micro_triggers.append("single_position_budget_below_micro_threshold")
        if options_budget < float(min_option_budget):
            micro_triggers.append("options_budget_below_min_option_threshold")

    if status == "red":
        candidate_operating_mode = "capital_blocked"
    elif leverage_mode == "restricted_margin":
        candidate_operating_mode = "restricted_margin"
    elif leverage_mode == "cash_only" and micro_triggers:
        candidate_operating_mode = "micro_cash_only"
    elif leverage_mode == "cash_only":
        candidate_operating_mode = "cash_only"
    elif margin_budget <= 0:
        candidate_operating_mode = "conservative_cash_like"
    else:
        candidate_operating_mode = "normal_margin"

    exit_checks = [
        _exit_check("net_liquidation", net_liq, micro_exit_net_liq_threshold),
        _exit_check("deployable_cash_after_reserve", cash_budget, micro_exit_cash_budget_threshold),
        _exit_check("max_single_position_notional", single_position_budget, micro_exit_single_position_threshold),
        _exit_check("options_premium_budget", options_budget, micro_exit_option_budget_threshold),
    ]
    exit_ready = bool(candidate_operating_mode != "micro_cash_only" and all(row["passed"] for row in exit_checks))
    previous_mode = _previous_mode(previous_policy_state)
    previous_confirmations = _previous_exit_confirmations(previous_policy_state)
    required_confirmations = max(1, int(micro_exit_confirmations_required))
    consecutive_exit_confirmations = 0
    transition = "stateless_candidate"
    exit_hold_active = False
    operating_mode = candidate_operating_mode
    hold_reasons: list[str] = []
    if previous_mode == "micro_cash_only":
        if candidate_operating_mode == "micro_cash_only":
            transition = "micro_cash_only_continues"
        elif candidate_operating_mode in {"capital_blocked", "restricted_margin"}:
            transition = "stricter_mode_overrides_micro"
        elif exit_ready:
            consecutive_exit_confirmations = previous_confirmations + 1
            if consecutive_exit_confirmations >= required_confirmations:
                transition = "micro_cash_exit_confirmed"
            else:
                operating_mode = "micro_cash_only"
                transition = "micro_cash_exit_pending"
                exit_hold_active = True
                hold_reasons.append("micro_policy_exit_requires_consecutive_confirmations")
        else:
            operating_mode = "micro_cash_only"
            transition = "micro_cash_exit_not_ready"
            exit_hold_active = True
            hold_reasons.extend(
                f"micro_exit_check_failed_{row['label']}"
                for row in exit_checks
                if not row.get("passed")
            )
    elif candidate_operating_mode == "micro_cash_only":
        transition = "micro_cash_entered"

    allow_fractional_stock_long = bool(
        status != "red"
        and operating_mode != "restricted_margin"
        and cash_budget >= float(min_fractional_stock_cash)
        and (not single_position_budget or single_position_budget >= float(min_single_stock_notional))
    )
    allow_short_stock = bool(
        status != "red"
        and leverage_mode not in {"cash_only", "restricted_margin"}
        and operating_mode not in {"micro_cash_only", "capital_blocked"}
        and margin_budget > 0
    )
    allow_margin_heavy = bool(
        status != "red"
        and leverage_mode not in {"cash_only", "restricted_margin"}
        and operating_mode not in {"micro_cash_only", "capital_blocked"}
        and margin_budget > 0
    )
    allow_long_option = bool(status != "red" and operating_mode != "restricted_margin" and options_budget >= float(min_option_budget))
    allow_defined_risk_spread = bool(
        status != "red"
        and operating_mode != "restricted_margin"
        and options_budget >= float(min_spread_budget)
    )

    blocked_reasons: list[str] = []
    if not allow_short_stock:
        blocked_reasons.append("micro_policy_blocks_short_stock")
    if not allow_margin_heavy:
        blocked_reasons.append("micro_policy_blocks_margin_heavy")
    if options_budget < float(min_option_budget):
        blocked_reasons.append("micro_policy_option_budget_below_minimum")
    if options_budget < float(min_spread_budget):
        blocked_reasons.append("micro_policy_spread_budget_below_minimum")

    guidance: list[str] = []
    if operating_mode == "micro_cash_only":
        guidance.append("Prefer tiny fractional stock-long paper candidates when all source/market gates are ready.")
        guidance.append("Block short stock, margin-heavy entries, and option structures until the premium budget clears minimum thresholds.")
    elif operating_mode == "cash_only":
        guidance.append("Cash-only mode can consider stock-long and debit-defined-risk options when premium budgets are sufficient.")
    elif operating_mode in {"capital_blocked", "restricted_margin"}:
        guidance.append("Only reduce-risk or read-only validation should remain active.")

    return {
        "micro_cash_policy_version": MICRO_CASH_POLICY_VERSION,
        "profile": profile_cfg.get("profile_name"),
        "profile_description": profile_cfg.get("description"),
        "operating_mode": operating_mode,
        "candidate_operating_mode": candidate_operating_mode,
        "status": status,
        "leverage_mode": leverage_mode,
        "micro_triggers": micro_triggers,
        "thresholds": {
            "min_option_budget": float(min_option_budget),
            "min_spread_budget": float(min_spread_budget),
            "micro_net_liq_threshold": float(micro_net_liq_threshold),
            "micro_cash_budget_threshold": float(micro_cash_budget_threshold),
            "micro_single_position_threshold": float(micro_single_position_threshold),
            "micro_exit_net_liq_threshold": float(micro_exit_net_liq_threshold),
            "micro_exit_cash_budget_threshold": float(micro_exit_cash_budget_threshold),
            "micro_exit_single_position_threshold": float(micro_exit_single_position_threshold),
            "micro_exit_option_budget_threshold": float(micro_exit_option_budget_threshold),
            "micro_exit_confirmations_required": int(required_confirmations),
            "min_single_stock_notional": float(min_single_stock_notional),
            "min_fractional_stock_cash": float(min_fractional_stock_cash),
            "max_option_ticket_debit": float(max_option_ticket_debit),
            "max_option_ticket_net_liq_fraction": float(profile_cfg.get("max_option_ticket_net_liq_fraction") or 0.0),
            "max_option_ticket_budget_fraction": float(profile_cfg.get("max_option_ticket_budget_fraction") or 0.0),
            "max_option_ticket_debit_cap": float(profile_cfg.get("max_option_ticket_debit_cap") or 0.0),
            "max_option_ticket_debit_floor": float(profile_cfg.get("max_option_ticket_debit_floor") or 0.0),
        },
        "exit_hysteresis": {
            "previous_operating_mode": previous_mode,
            "previous_exit_confirmations": previous_confirmations,
            "candidate_operating_mode": candidate_operating_mode,
            "effective_operating_mode": operating_mode,
            "exit_checks": exit_checks,
            "exit_ready": exit_ready,
            "consecutive_exit_confirmations": consecutive_exit_confirmations,
            "exit_confirmations_required": int(required_confirmations),
            "exit_hold_active": exit_hold_active,
            "transition": transition,
            "hold_reasons": hold_reasons,
        },
        "metrics": {
            "net_liquidation": net_liq,
            "total_cash_value": total_cash,
        },
        "budgets": {
            "deployable_cash_after_reserve": cash_budget,
            "max_new_initial_margin": margin_budget,
            "core_risk_budget": core_risk_budget,
            "max_single_position_notional": single_position_budget,
            "options_premium_budget": options_budget,
            "hedge_budget": hedge_budget,
        },
        "budget_presence": budget_presence,
        "permissions": {
            "allow_fractional_stock_long": allow_fractional_stock_long,
            "allow_short_stock": allow_short_stock,
            "allow_margin_heavy": allow_margin_heavy,
            "allow_long_option": allow_long_option,
            "allow_defined_risk_spread": allow_defined_risk_spread,
            "allow_reduce_only": True,
            "max_option_ticket_debit": float(max_option_ticket_debit),
        },
        "default_blocked_reasons": blocked_reasons,
        "guidance": guidance,
        "safety": {
            "paper_or_read_only_policy": True,
            "ibkr_order_submitted": False,
            "transmit_enabled": False,
            "live_routing_enabled": False,
        },
    }


def micro_cash_policy_state(policy: dict[str, Any], *, generated_at: str | None = None) -> dict[str, Any]:
    exit_hysteresis = policy.get("exit_hysteresis") if isinstance(policy.get("exit_hysteresis"), dict) else {}
    return {
        "micro_cash_policy_version": policy.get("micro_cash_policy_version") or MICRO_CASH_POLICY_VERSION,
        "generated_at": generated_at,
        "profile": policy.get("profile"),
        "effective_operating_mode": policy.get("operating_mode"),
        "candidate_operating_mode": policy.get("candidate_operating_mode"),
        "consecutive_exit_confirmations": int(exit_hysteresis.get("consecutive_exit_confirmations") or 0),
        "exit_confirmations_required": int(exit_hysteresis.get("exit_confirmations_required") or DEFAULT_MICRO_EXIT_CONFIRMATIONS_REQUIRED),
        "exit_ready": bool(exit_hysteresis.get("exit_ready")),
        "exit_hold_active": bool(exit_hysteresis.get("exit_hold_active")),
        "transition": exit_hysteresis.get("transition"),
        "micro_triggers": policy.get("micro_triggers") or [],
        "thresholds": policy.get("thresholds") or {},
    }


def scaled_requirements(requirements: dict[str, Any], ratio: float) -> dict[str, float]:
    safe_ratio = max(0.0, float(ratio)) if math.isfinite(float(ratio)) else 0.0
    return {
        key: round(_positive(_as_float(value)) * safe_ratio, 6)
        for key, value in (requirements or {}).items()
        if key
    }


def evaluate_micro_policy_for_candidate(
    policy: dict[str, Any],
    *,
    action: str,
    ticket_type: str,
    requirements: dict[str, Any] | None = None,
    selected_expression: str | None = None,
    intent_role: str | None = None,
) -> dict[str, Any]:
    permissions = policy.get("permissions") if isinstance(policy.get("permissions"), dict) else {}
    budgets = policy.get("budgets") if isinstance(policy.get("budgets"), dict) else {}
    budget_presence = policy.get("budget_presence") if isinstance(policy.get("budget_presence"), dict) else {}
    thresholds = policy.get("thresholds") if isinstance(policy.get("thresholds"), dict) else {}
    req = requirements if isinstance(requirements, dict) else {}
    normalized_action = str(action or "").upper()
    normalized_ticket = str(ticket_type or "").lower()
    expression = str(selected_expression or "").lower()
    role = str(intent_role or "").lower()
    cash_required = _positive(_as_float(req.get("cash_required")))
    margin_required = _positive(_as_float(req.get("initial_margin_required")))
    risk_required = _positive(_as_float(req.get("risk_required")))
    stock_notional_required = _positive(_as_float(req.get("stock_notional_required")))
    option_premium_required = _positive(_as_float(req.get("option_premium_required")))
    cash_budget = _positive(_as_float(budgets.get("deployable_cash_after_reserve")))
    margin_budget = _positive(_as_float(budgets.get("max_new_initial_margin")))
    risk_budget = _positive(_as_float(budgets.get("core_risk_budget")))
    single_budget = _positive(_as_float(budgets.get("max_single_position_notional")))
    options_budget = _positive(_as_float(budgets.get("options_premium_budget")))
    min_option_budget = _positive(_as_float(thresholds.get("min_option_budget")))
    min_spread_budget = _positive(_as_float(thresholds.get("min_spread_budget")))
    min_stock_notional = _positive(_as_float(thresholds.get("min_single_stock_notional"))) or 1.0
    max_option_ticket_debit = _positive(_as_float(thresholds.get("max_option_ticket_debit")))

    is_stock = _is_stock_ticket(normalized_ticket) or expression in {"stock_long", "fractional_stock_long_micro"}
    is_option = _is_option_ticket(normalized_ticket, expression)
    is_spread = _is_spread_ticket(normalized_ticket, expression)
    is_reduce_role = role in {"hedge_or_risk_reduction", "risk_reduction", "reduce_only"} or "cover" in normalized_action
    reasons: list[str] = []
    expression_hint = selected_expression or "unspecified"

    if policy.get("operating_mode") == "capital_blocked" and not is_reduce_role:
        reasons.append("micro_policy_capital_state_blocks_new_entries")
    if policy.get("operating_mode") == "restricted_margin" and not is_reduce_role:
        reasons.append("micro_policy_restricted_margin_blocks_new_entries")
    if normalized_action == "SELL_SHORT" and is_stock and not permissions.get("allow_short_stock"):
        reasons.append("micro_policy_blocks_short_stock")
    if normalized_action in {"SELL", "SELL_TO_OPEN"} and is_stock and not is_reduce_role and not permissions.get("allow_short_stock"):
        reasons.append("micro_policy_blocks_unverified_bearish_stock_sale")

    if is_stock and normalized_action in {"BUY", "BUY_TO_OPEN"}:
        if not permissions.get("allow_fractional_stock_long"):
            reasons.append("micro_policy_fractional_stock_long_not_allowed")
        if cash_required and budget_presence.get("deployable_cash_after_reserve") and cash_required > cash_budget + 1e-9:
            reasons.append("micro_policy_cash_budget_insufficient")
        if stock_notional_required and budget_presence.get("max_single_position_notional") and single_budget and stock_notional_required > single_budget + 1e-9:
            reasons.append("micro_policy_single_position_budget_insufficient")
        if cash_required < min_stock_notional and stock_notional_required < min_stock_notional:
            reasons.append("micro_policy_candidate_below_minimum_stock_notional")
        if policy.get("operating_mode") == "micro_cash_only" and not reasons:
            expression_hint = "fractional_stock_long_micro"

    if is_option:
        if is_spread:
            if not permissions.get("allow_defined_risk_spread"):
                reasons.append("micro_policy_spread_budget_below_minimum")
            if options_budget < min_spread_budget:
                reasons.append("micro_policy_spread_budget_below_minimum")
        else:
            if not permissions.get("allow_long_option"):
                reasons.append("micro_policy_option_budget_below_minimum")
            if options_budget < min_option_budget:
                reasons.append("micro_policy_option_budget_below_minimum")
        if option_premium_required and budget_presence.get("options_premium_budget") and option_premium_required > options_budget + 1e-9:
            reasons.append("micro_policy_options_premium_budget_insufficient")
        if max_option_ticket_debit > 0 and option_premium_required > max_option_ticket_debit + 1e-9:
            reasons.append("micro_policy_option_ticket_risk_cap_exceeded")

    margin_is_premium_like = is_option and margin_required <= max(option_premium_required, cash_required, risk_required) + 1e-9
    margin_is_cash_stock_like = (
        is_stock
        and normalized_action in {"BUY", "BUY_TO_OPEN"}
        and margin_required <= max(cash_required, stock_notional_required) + 1e-9
    )
    margin_is_self_funded = margin_is_premium_like or margin_is_cash_stock_like
    if budget_presence.get("max_new_initial_margin") and margin_required > margin_budget + 1e-9 and not margin_is_self_funded:
        reasons.append("micro_policy_margin_budget_insufficient")
    if margin_required > 0 and not margin_is_self_funded and not permissions.get("allow_margin_heavy"):
        reasons.append("micro_policy_blocks_margin_heavy")
    if budget_presence.get("core_risk_budget") and risk_required > risk_budget + 1e-9:
        reasons.append("micro_policy_core_risk_budget_insufficient")

    reasons = list(dict.fromkeys(reasons))
    ready = not reasons
    return {
        "micro_cash_policy_version": policy.get("micro_cash_policy_version") or MICRO_CASH_POLICY_VERSION,
        "operating_mode": policy.get("operating_mode"),
        "ready": ready,
        "status": "allowed" if ready else "blocked",
        "selected_expression_hint": expression_hint,
        "block_reasons": reasons,
        "requirements_checked": {
            "cash_required": round(cash_required, 6),
            "initial_margin_required": round(margin_required, 6),
            "risk_required": round(risk_required, 6),
            "stock_notional_required": round(stock_notional_required, 6),
            "option_premium_required": round(option_premium_required, 6),
        },
        "permissions_snapshot": {
            "allow_fractional_stock_long": permissions.get("allow_fractional_stock_long"),
            "allow_short_stock": permissions.get("allow_short_stock"),
            "allow_margin_heavy": permissions.get("allow_margin_heavy"),
            "allow_long_option": permissions.get("allow_long_option"),
            "allow_defined_risk_spread": permissions.get("allow_defined_risk_spread"),
            "max_option_ticket_debit": permissions.get("max_option_ticket_debit"),
        },
        "risk_caps": {
            "max_option_ticket_debit": round(max_option_ticket_debit, 6),
        },
    }
