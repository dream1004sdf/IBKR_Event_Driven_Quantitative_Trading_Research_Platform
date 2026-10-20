from __future__ import annotations

import copy
import math
from typing import Any


FINAL_ENTRY_SIZE_RESOLVER_VERSION = "final-entry-size-resolver-v1"


DEFAULT_RESOLVER_CONFIG = {
    "mode": "product_with_floor",
    "soft_floor_multiplier": 0.25,
    "multi_haircut_floor_factor": 0.65,
    "stock_min_quantity": 0.01,
    "stock_quantity_increment": 0.01,
    "option_min_contract_quantity": 1,
}


def _as_float(value: Any, default: float | None = None) -> float | None:
    if value is None:
        return default
    try:
        parsed = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return default
    if not math.isfinite(parsed):
        return default
    return parsed


def _clamp(value: Any, default: float = 1.0) -> float:
    parsed = _as_float(value, default)
    if parsed is None:
        parsed = default
    return max(0.0, min(1.0, float(parsed)))


def _round_down_to_increment(quantity: float, increment: float) -> float:
    increment = max(0.000001, float(increment or 0.000001))
    return math.floor(max(0.0, float(quantity)) / increment) * increment


def resolve_final_entry_size(
    *,
    source_quantity: float | None,
    ticket_type: str | None = None,
    multipliers: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cfg = {**DEFAULT_RESOLVER_CONFIG, **(config or {})}
    raw_multipliers = multipliers or {}
    normalized = {
        key: _clamp(value)
        for key, value in raw_multipliers.items()
        if value is not None and _as_float(value) is not None
    }
    source_qty = _as_float(source_quantity)
    hard_or_trade_block = any(value <= 0.0 for value in normalized.values())
    positive_haircuts = [value for value in normalized.values() if 0.0 < value < 1.0]
    product = 1.0
    for value in positive_haircuts:
        product *= value
    min_haircut = min(positive_haircuts) if positive_haircuts else 1.0
    if hard_or_trade_block:
        final_multiplier = 0.0
    elif len(positive_haircuts) >= 2:
        floor = max(float(cfg["soft_floor_multiplier"]), min_haircut * float(cfg["multi_haircut_floor_factor"]))
        final_multiplier = max(product, min(min_haircut, floor))
    elif positive_haircuts:
        final_multiplier = product
    else:
        final_multiplier = 1.0
    final_multiplier = round(max(0.0, min(1.0, final_multiplier)), 6)

    final_quantity: float | int | None
    reasons: list[str] = []
    normalized_ticket = str(ticket_type or "").lower()
    if source_qty is None:
        final_quantity = None
        reasons.append("source_quantity_missing")
    elif final_multiplier <= 0.0:
        final_quantity = 0.0
        reasons.append("final_multiplier_zero")
    elif "option" in normalized_ticket or "combo" in normalized_ticket or "spread" in normalized_ticket:
        raw_qty = source_qty * final_multiplier
        min_contract = max(1, int(cfg["option_min_contract_quantity"]))
        if raw_qty < min_contract:
            final_quantity = min_contract
            reasons.append("option_quantity_floor_kept_at_one_contract")
        else:
            final_quantity = int(math.floor(raw_qty))
    else:
        raw_qty = source_qty * final_multiplier
        final_quantity = round(
            _round_down_to_increment(raw_qty, float(cfg["stock_quantity_increment"])),
            6,
        )
        if final_quantity < float(cfg["stock_min_quantity"]):
            final_quantity = 0.0
            reasons.append("sized_stock_quantity_below_minimum")

    return {
        "version": FINAL_ENTRY_SIZE_RESOLVER_VERSION,
        "mode": cfg["mode"],
        "source_quantity": source_qty,
        "ticket_type": ticket_type,
        "input_multipliers": normalized,
        "positive_haircut_count": len(positive_haircuts),
        "product_multiplier": round(product if not hard_or_trade_block else 0.0, 6),
        "min_haircut_multiplier": round(min_haircut, 6),
        "final_size_multiplier": final_multiplier,
        "final_quantity": final_quantity,
        "reasons": reasons,
        "policy": cfg,
    }


def strategy_recommendation_index(strategy_allocation_payload: dict[str, Any] | None) -> dict[tuple[str, str], dict[str, Any]]:
    payload = strategy_allocation_payload or {}
    if isinstance(payload.get("result"), dict):
        payload = payload["result"]
    index: dict[tuple[str, str], dict[str, Any]] = {}
    for row in payload.get("recommendations") or []:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").upper()
        action = str(row.get("source_action") or row.get("action") or "").upper()
        if symbol:
            index[(symbol, action)] = row
            index[(symbol, "")] = row
    return index


def apply_strategy_gate_modifiers_to_capital_gate(
    capital_gate_payload: dict[str, Any],
    strategy_allocation_payload: dict[str, Any],
    *,
    resolver_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    gate = copy.deepcopy(capital_gate_payload or {})
    index = strategy_recommendation_index(strategy_allocation_payload)
    applied = 0
    for item in gate.get("auto_execution_items") or []:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or "").upper()
        action = str(item.get("action") or "").upper()
        rec = index.get((symbol, action)) or index.get((symbol, ""))
        if not rec:
            continue
        gate_policy = rec.get("gate_policy") if isinstance(rec.get("gate_policy"), dict) else {}
        size_multiplier = _clamp(rec.get("recommended_size_multiplier"), 1.0)
        confidence_multiplier = _clamp((gate_policy or {}).get("confidence_multiplier"), 1.0)
        item["gate_severity_size_multiplier"] = size_multiplier
        item["gate_severity_confidence_multiplier"] = confidence_multiplier
        item["gate_severity_posture"] = (gate_policy or {}).get("recommended_execution_posture")
        item["strategy_allocation_recommendation"] = {
            "recommendation": rec.get("recommendation"),
            "selected_expression": rec.get("selected_expression"),
            "confidence": rec.get("confidence"),
            "direction": rec.get("direction"),
            "reasons": rec.get("reasons") or [],
        }
        item["final_entry_size_preview"] = resolve_final_entry_size(
            source_quantity=_as_float(item.get("quantity")),
            ticket_type=item.get("ticket_type"),
            multipliers={
                "gate_severity_size_multiplier": size_multiplier,
                "evidence_entry_size_multiplier": item.get("evidence_entry_size_multiplier"),
                "market_data_entry_size_multiplier": item.get("market_data_entry_size_multiplier"),
            },
            config=resolver_config,
        )
        applied += 1
    summary = gate.setdefault("final_entry_size_resolver", {})
    summary.update(
        {
            "version": FINAL_ENTRY_SIZE_RESOLVER_VERSION,
            "strategy_gate_modifier_applied_count": applied,
            "resolver_config": {**DEFAULT_RESOLVER_CONFIG, **(resolver_config or {})},
        }
    )
    return gate
