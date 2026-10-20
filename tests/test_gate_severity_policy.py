from __future__ import annotations

from recorder.gate_severity_policy import build_gate_severity_policy_report, gate_policy_from_market_context


def test_gate_severity_policy_splits_cautions_from_hard_blocks() -> None:
    report = build_gate_severity_policy_report(
        hard_blocks=[{"code": "safety_invariant_violation", "source": "m250"}],
        trade_now_blockers=[{"code": "market_risk_gate_blocks_new_entries", "source": "m175"}],
        live_eligibility_blockers=[{"code": "promotion_gate_not_clear", "source": "m226"}],
        soft_warnings=[
            {"code": "market_temperature_elevated", "source": "m153"},
            {"code": "evidence_freshness_yellow", "source": "m152"},
            {"code": "option_session_deferred_normal", "source": "m247/m248/m249"},
        ],
    )

    assert report["hard_block_count"] == 1
    assert report["trade_now_blocker_count"] == 1
    assert report["haircut_count"] == 1
    assert report["confidence_discount_count"] == 2
    assert report["monitoring_only_count"] == 1
    assert report["recommended_execution_posture"] == "hard_blocked"
    assert report["live_order_submission_allowed"] is False


def test_gate_policy_from_market_context_haircuts_without_blocking() -> None:
    report = gate_policy_from_market_context(
        {
            "market_data_status": "yellow",
            "risk_gate_action": "allow_with_market_data_haircut",
            "new_entries_allowed": True,
            "entry_size_multiplier": 0.4,
            "temperature_label": "hot",
            "delayed_count": 0,
            "missing_price_count": 0,
        }
    )

    assert report["trade_now_blocker_count"] == 0
    assert report["haircut_count"] >= 1
    assert report["confidence_discount_count"] >= 1
    assert report["size_multiplier"] == 0.4
    assert report["new_entries_allowed_now"] is True


def test_gate_severity_policy_uses_defaults_when_config_values_are_invalid() -> None:
    report = build_gate_severity_policy_report(
        soft_warnings=[
            {"code": "market_temperature_elevated", "source": "m153"},
            {"code": "promotion_gate_not_clear", "source": "m226"},
        ],
        config={
            "haircut_size_multipliers": {"market_temperature": "not-a-number"},
            "confidence_multipliers": {"promotion_gate": "bad"},
        },
    )

    assert report["size_multiplier"] == 0.5
    assert report["confidence_multiplier"] == 0.68
    assert report["new_entries_allowed_now"] is True
