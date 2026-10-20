from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any


GATE_SEVERITY_POLICY_VERSION = "gate-severity-policy-v1-realistic-auto-trading-split"
DEFAULT_GATE_SEVERITY_POLICY_CONFIG_PATH = Path("config/gate_severity_policy_v1.json")


SEVERITY_ORDER = (
    "hard_block",
    "trade_now_blocker",
    "haircut",
    "confidence_discount",
    "monitoring_only",
)


_HARD_BLOCK_EXACT = {
    "capital_state_not_green",
    "capital_snapshot_requires_refresh",
    "capital_snapshot_not_fresh",
    "capital_state_red",
    "m237_capital_margin_state_missing",
    "m237_capital_margin_state_stale",
    "m243_live_capital_validation_red",
    "execution_safety_cache_not_ready",
    "safety_invariant_violation",
}

_HARD_BLOCK_PREFIXES = (
    "safety_invariant_",
    "broker_order_",
    "live_order_",
    "transmit_enabled",
    "portfolio_mutation",
)

_TRADE_NOW_EXACT = {
    "capital_pipeline_not_green",
    "market_data_red",
    "market_risk_gate_blocks_new_entries",
    "m153_market_data_desk_missing",
    "m153_market_data_desk_stale",
    "m249_option_whatif_safety_validation_stale",
    "m249_not_passed",
    "evidence_freshness_red",
    "source_supervisor_red",
}

_TRADE_NOW_PREFIXES = (
    "market_risk_gate_block",
    "capital_pipeline_not_green",
)

_HAIRCUT_EXACT = {
    "market_temperature_elevated",
    "market_data_yellow",
    "market_data_risk_gate_haircut_active",
    "session_transition_release_governor_haircut_active",
    "m243_live_capital_validation_yellow",
    "option_quotes_not_fresh",
    "wide_spread_quotes_present",
    "close_only_quotes_present",
    "stale_monitoring_only_present",
    "allow_with_market_data_haircut",
    "allow_with_session_context_haircut",
}

_HAIRCUT_SUBSTRINGS = (
    "haircut",
    "wide_spread",
    "close_only",
    "market_temperature",
)

_CONFIDENCE_DISCOUNT_EXACT = {
    "evidence_freshness_yellow",
    "source_supervisor_yellow",
    "learning_shadow_only",
    "promotion_gate_not_clear",
    "promotion_gate_no_ready_candidate",
    "capital_pipeline_missing",
    "m249_missing_or_not_ready",
}

_CONFIDENCE_SUBSTRINGS = (
    "yellow",
    "shadow_only",
    "promotion_gate",
    "evidence_freshness",
    "source_supervisor",
    "not_fresh",
)

_MONITORING_ONLY_EXACT = {
    "option_session_deferred_normal",
    "market_data_session_deferred_normal",
    "market_risk_gate_session_deferred_normal",
    "promotion_gate_option_session_deferred",
    "execution_safety_cache_option_session_deferred",
    "closed_session_expected_stale",
    "source_ingest_health_monitoring_only_gap",
    "source_online_supervisor_monitoring_only_gap",
}

_MONITORING_SUBSTRINGS = (
    "monitoring_only",
    "session_deferred_normal",
    "expected_deferred",
    "closed_session",
)


def load_gate_severity_policy_config(path: Path | None = None) -> dict[str, Any]:
    resolved = (path or DEFAULT_GATE_SEVERITY_POLICY_CONFIG_PATH).expanduser()
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _config_float(table: dict[str, Any], key: str, default: float) -> float:
    try:
        return float(table.get(key, default))
    except (TypeError, ValueError):
        return float(default)


def _code(signal: dict[str, Any]) -> str:
    return str(signal.get("code") or signal.get("reason") or signal.get("action") or "").strip()


def _category_from_code(code: str, *, origin: str) -> str:
    normalized = code.strip().lower()
    if not normalized:
        return "confidence_discount" if origin == "soft_warning" else origin
    if normalized in _HARD_BLOCK_EXACT or any(normalized.startswith(prefix) for prefix in _HARD_BLOCK_PREFIXES):
        return "hard_block"
    if normalized in _MONITORING_ONLY_EXACT or any(part in normalized for part in _MONITORING_SUBSTRINGS):
        return "monitoring_only"
    if normalized in _TRADE_NOW_EXACT or any(normalized.startswith(prefix) for prefix in _TRADE_NOW_PREFIXES):
        return "trade_now_blocker"
    if normalized in _HAIRCUT_EXACT or any(part in normalized for part in _HAIRCUT_SUBSTRINGS):
        return "haircut"
    if normalized in _CONFIDENCE_DISCOUNT_EXACT or any(part in normalized for part in _CONFIDENCE_SUBSTRINGS):
        return "confidence_discount"
    if origin in {"hard_block", "trade_now_blocker"}:
        return origin
    if origin == "live_eligibility_blocker":
        return "confidence_discount"
    return "monitoring_only" if origin == "soft_warning" else "confidence_discount"


def _size_multiplier_for(code: str, category: str, value: Any = None, config: dict[str, Any] | None = None) -> float:
    if category in {"hard_block", "trade_now_blocker"}:
        return 0.0
    if category != "haircut":
        return 1.0
    cfg = config or {}
    table = cfg.get("haircut_size_multipliers") if isinstance(cfg.get("haircut_size_multipliers"), dict) else {}
    normalized = code.strip().lower()
    if isinstance(value, dict):
        explicit = value.get("entry_size_multiplier") or value.get("size_multiplier")
        if explicit is not None:
            try:
                return max(0.0, min(1.0, float(explicit)))
            except (TypeError, ValueError):
                pass
    if "market_temperature" in normalized:
        return _config_float(table, "market_temperature", 0.50)
    if "wide_spread" in normalized:
        return _config_float(table, "wide_spread", 0.60)
    if "option_quotes_not_fresh" in normalized:
        return _config_float(table, "option_quotes_not_fresh", 0.60)
    if "close_only" in normalized:
        return _config_float(table, "close_only", 0.70)
    if "stale" in normalized:
        return _config_float(table, "stale", 0.70)
    return _config_float(table, "default", 0.75)


def _confidence_multiplier_for(code: str, category: str, config: dict[str, Any] | None = None) -> float:
    normalized = code.strip().lower()
    cfg = config or {}
    table = cfg.get("confidence_multipliers") if isinstance(cfg.get("confidence_multipliers"), dict) else {}
    if category == "hard_block":
        return 0.0
    if category == "trade_now_blocker":
        return _config_float(table, "trade_now_blocker", 0.60)
    if category == "haircut":
        return _config_float(table, "haircut", 0.85)
    if category == "confidence_discount":
        if "promotion_gate" in normalized:
            return _config_float(table, "promotion_gate", 0.80)
        if "evidence" in normalized or "source_supervisor" in normalized:
            return _config_float(table, "evidence_or_source", 0.85)
        return _config_float(table, "confidence_discount_default", 0.90)
    return 1.0


def classify_gate_signal(signal: dict[str, Any], *, origin: str, config: dict[str, Any] | None = None) -> dict[str, Any]:
    code = _code(signal)
    category = _category_from_code(code, origin=origin)
    return {
        "category": category,
        "origin": origin,
        "code": code,
        "source": signal.get("source"),
        "message": signal.get("message"),
        "value": signal.get("value"),
        "size_multiplier": _size_multiplier_for(code, category, signal.get("value"), config),
        "confidence_multiplier": _confidence_multiplier_for(code, category, config),
        "blocks_order_submission": category == "hard_block",
        "blocks_new_entries_now": category in {"hard_block", "trade_now_blocker"},
        "blocks_live_eligibility": category in {"hard_block", "trade_now_blocker"} or origin == "live_eligibility_blocker",
    }


def build_gate_severity_policy_report(
    *,
    hard_blocks: list[dict[str, Any]] | None = None,
    trade_now_blockers: list[dict[str, Any]] | None = None,
    live_eligibility_blockers: list[dict[str, Any]] | None = None,
    soft_warnings: list[dict[str, Any]] | None = None,
    extra_signals: list[dict[str, Any]] | None = None,
    config: dict[str, Any] | None = None,
    config_path: Path | None = None,
) -> dict[str, Any]:
    policy_config = config if config is not None else load_gate_severity_policy_config(config_path)
    signals: list[dict[str, Any]] = []
    for origin, rows in (
        ("hard_block", hard_blocks or []),
        ("trade_now_blocker", trade_now_blockers or []),
        ("live_eligibility_blocker", live_eligibility_blockers or []),
        ("soft_warning", soft_warnings or []),
    ):
        for row in rows:
            if isinstance(row, dict):
                signals.append(classify_gate_signal(row, origin=origin, config=policy_config))
    for row in extra_signals or []:
        if isinstance(row, dict):
            origin = str(row.get("origin") or "soft_warning")
            signals.append(classify_gate_signal(row, origin=origin, config=policy_config))

    category_counts = Counter(row["category"] for row in signals)
    origin_counts = Counter(row["origin"] for row in signals)
    code_counts = Counter(row["code"] for row in signals if row.get("code"))

    haircut_multipliers = [float(row["size_multiplier"]) for row in signals if row["category"] == "haircut"]
    confidence_multipliers = [
        float(row["confidence_multiplier"])
        for row in signals
        if row["category"] in {"trade_now_blocker", "haircut", "confidence_discount"}
    ]
    size_multiplier = min(haircut_multipliers) if haircut_multipliers else 1.0
    confidence_multiplier = 1.0
    for value in confidence_multipliers:
        confidence_multiplier *= value
    confidence_multiplier = max(0.35, min(1.0, confidence_multiplier))

    hard_count = int(category_counts.get("hard_block") or 0)
    trade_count = int(category_counts.get("trade_now_blocker") or 0)
    haircut_count = int(category_counts.get("haircut") or 0)
    confidence_count = int(category_counts.get("confidence_discount") or 0)
    monitoring_count = int(category_counts.get("monitoring_only") or 0)
    if hard_count:
        posture = "hard_blocked"
    elif trade_count:
        posture = "new_entries_blocked_now"
    elif haircut_count:
        posture = "size_reduced"
    elif confidence_count:
        posture = "confidence_discounted"
    elif monitoring_count:
        posture = "monitoring_only"
    else:
        posture = "normal"

    return {
        "version": GATE_SEVERITY_POLICY_VERSION,
        "config_version": policy_config.get("version"),
        "config": policy_config,
        "recommended_execution_posture": posture,
        "category_counts": {key: int(category_counts.get(key) or 0) for key in SEVERITY_ORDER},
        "origin_counts": dict(origin_counts),
        "top_code_counts": dict(code_counts.most_common(16)),
        "hard_block_count": hard_count,
        "trade_now_blocker_count": trade_count,
        "haircut_count": haircut_count,
        "confidence_discount_count": confidence_count,
        "monitoring_only_count": monitoring_count,
        "size_multiplier": round(size_multiplier, 4),
        "confidence_multiplier": round(confidence_multiplier, 4),
        "paper_planning_allowed": hard_count == 0,
        "new_entries_allowed_now": hard_count == 0 and trade_count == 0,
        "live_order_submission_allowed": False,
        "signals": signals[:80],
        "taxonomy": {
            "hard_block": "True safety/capital/order invariant danger. Stop the path.",
            "trade_now_blocker": "Do not open fresh entries now, but keep monitoring, paper planning, and learning alive.",
            "haircut": "Allow only reduced size while the condition remains active.",
            "confidence_discount": "Lower scorer confidence/threshold aggressiveness, but do not block by itself.",
            "monitoring_only": "Show on dashboard and logs only; no direct block.",
        },
        "safety_anchor": {
            "live_execution_enabled": False,
            "broker_order_submit_allowed": False,
            "transmit_enabled_for_live_orders": False,
        },
    }


def _safe_int(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def gate_policy_from_market_context(market: dict[str, Any], *, config: dict[str, Any] | None = None, config_path: Path | None = None) -> dict[str, Any]:
    signals: list[dict[str, Any]] = []
    action = str(market.get("risk_gate_action") or "").strip()
    if action in {"block_new_entries", "defensive_or_hedge_only"} or market.get("new_entries_allowed") is False:
        signals.append({"code": "market_risk_gate_blocks_new_entries", "source": "m175", "message": "Market gate blocks new entries.", "origin": "trade_now_blocker"})
    elif action in {"allow_with_market_data_haircut", "allow_with_session_context_haircut"}:
        signals.append(
            {
                "code": action,
                "source": "m175",
                "message": "Market gate allows entries only with reduced size.",
                "origin": "soft_warning",
                "value": {"entry_size_multiplier": market.get("entry_size_multiplier")},
            }
        )
    if str(market.get("market_data_status") or "").lower() == "yellow":
        signals.append({"code": "market_data_yellow", "source": "m153", "message": "Market data is cautionary.", "origin": "soft_warning"})
        signals.append(
            {
                "code": "market_data_confidence_discount_yellow",
                "source": "m153",
                "message": "Market data caution should lower scorer confidence, not hard-block by itself.",
                "origin": "soft_warning",
            }
        )
    if str(market.get("temperature_label") or "").lower() in {"hot", "stress"}:
        signals.append({"code": "market_temperature_elevated", "source": "m153", "message": "Market temperature is elevated.", "origin": "soft_warning"})
    if _safe_int(market.get("delayed_count")) > 0:
        signals.append({"code": "market_data_delayed_rows", "source": "m153", "message": "Some market data rows are delayed.", "origin": "soft_warning"})
    if _safe_int(market.get("missing_price_count")) > 0:
        signals.append({"code": "market_data_missing_prices", "source": "m153", "message": "Some market prices are missing.", "origin": "trade_now_blocker"})
    return build_gate_severity_policy_report(extra_signals=signals, config=config, config_path=config_path)
