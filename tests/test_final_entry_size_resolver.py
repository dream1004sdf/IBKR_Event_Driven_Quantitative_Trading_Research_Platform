from __future__ import annotations

from recorder.final_entry_size_resolver import (
    apply_strategy_gate_modifiers_to_capital_gate,
    resolve_final_entry_size,
)


def test_final_entry_size_resolver_combines_multiple_haircuts_with_floor() -> None:
    result = resolve_final_entry_size(
        source_quantity=10,
        ticket_type="stock",
        multipliers={
            "market_data_entry_size_multiplier": 0.5,
            "gate_severity_size_multiplier": 0.5,
        },
    )

    assert result["final_size_multiplier"] == 0.325
    assert result["final_quantity"] == 3.25
    assert result["positive_haircut_count"] == 2


def test_final_entry_size_resolver_keeps_min_one_option_contract_when_not_blocked() -> None:
    result = resolve_final_entry_size(
        source_quantity=1,
        ticket_type="single_long_option",
        multipliers={"gate_severity_size_multiplier": 0.35},
    )

    assert result["final_size_multiplier"] == 0.35
    assert result["final_quantity"] == 1
    assert "option_quantity_floor_kept_at_one_contract" in result["reasons"]


def test_final_entry_size_resolver_can_zero_when_trade_now_blocked() -> None:
    result = resolve_final_entry_size(
        source_quantity=1,
        ticket_type="single_long_option",
        multipliers={"trade_now_blocker": 0.0},
    )

    assert result["final_size_multiplier"] == 0.0
    assert result["final_quantity"] == 0.0
    assert "final_multiplier_zero" in result["reasons"]


def test_apply_strategy_gate_modifiers_to_capital_gate_attaches_final_size_preview() -> None:
    capital_gate = {
        "auto_execution_items": [
            {
                "symbol": "SPY",
                "action": "BUY",
                "ticket_type": "stock",
                "quantity": 2.0,
                "evidence_entry_size_multiplier": 1.0,
            }
        ]
    }
    strategy = {
        "recommendations": [
            {
                "symbol": "SPY",
                "source_action": "BUY",
                "recommendation": "stock",
                "recommended_size_multiplier": 0.5,
                "gate_policy": {
                    "confidence_multiplier": 0.85,
                    "recommended_execution_posture": "size_reduced",
                },
            }
        ]
    }

    result = apply_strategy_gate_modifiers_to_capital_gate(capital_gate, strategy)
    item = result["auto_execution_items"][0]

    assert item["gate_severity_size_multiplier"] == 0.5
    assert item["gate_severity_confidence_multiplier"] == 0.85
    assert item["gate_severity_posture"] == "size_reduced"
    assert item["final_entry_size_preview"]["final_quantity"] == 1.0
    assert result["final_entry_size_resolver"]["strategy_gate_modifier_applied_count"] == 1
