from __future__ import annotations

from typing import Any


SAFETY_INVARIANT_TRUE_KEYS = {
    "ibkr_order_submitted",
    "broker_orders_submitted",
    "live_order_transmitted",
    "transmit_enabled",
    "transmit_enabled_for_live_orders",
    "live_routing_enabled",
    "live_order_routing_enabled",
    "portfolio_mutated",
    "real_portfolio_mutated",
}


def paper_read_only_safety_block(**overrides: Any) -> dict[str, Any]:
    """Return the shared safety shape for paper/read-only execution readiness artifacts."""

    block: dict[str, Any] = {
        "read_only_or_paper_only": True,
        "read_only_or_paper_readiness": True,
        "paper_only": True,
        "ibkr_order_submitted": False,
        "broker_orders_submitted": False,
        "live_order_transmitted": False,
        "transmit_enabled": False,
        "transmit_enabled_for_live_orders": False,
        "live_routing_enabled": False,
        "live_order_routing_enabled": False,
        "portfolio_mutated": False,
        "real_portfolio_mutated": False,
    }
    block.update(overrides)
    return block


def collect_safety_invariant_violations(named_payloads: dict[str, dict[str, Any] | None]) -> list[dict[str, Any]]:
    """Find live-order/transmit/portfolio mutation flags that should stay false through M250."""

    violations: list[dict[str, Any]] = []
    for source, payload in named_payloads.items():
        if not isinstance(payload, dict):
            continue
        data = payload.get("result") if isinstance(payload.get("result"), dict) else payload
        safety = data.get("safety") if isinstance(data.get("safety"), dict) else {}
        for key in sorted(SAFETY_INVARIANT_TRUE_KEYS):
            if bool(safety.get(key)):
                violations.append({"source": source, "key": key, "value": safety.get(key)})
    return violations
