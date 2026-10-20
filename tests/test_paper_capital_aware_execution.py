from __future__ import annotations

from recorder.options_analysis import build_execution_intent_ledger_report, build_paper_order_router_report


def _auto_item(symbol: str, *, capital_status: str = "capital_ready", auto_status: str = "auto_execution_ready") -> dict:
    return {
        "symbol": symbol,
        "ticket_type": "stock",
        "action": "BUY",
        "quantity": 1,
        "auto_execution_status": auto_status,
        "capital_gate_status": capital_status,
        "capital_block_reasons": [] if capital_status in {"capital_ready", "capital_resized_ready"} else ["capital_budget_exceeded"],
        "capital_requirements": {"cash_required": 100.0, "initial_margin_required": 0.0, "risk_required": 25.0},
        "capital_remaining_before": {"cash_budget": 500.0},
        "capital_remaining_after": {"cash_budget": 400.0},
        "source_preview_item": {"source_score_id": 1, "source_outcome_id": 2},
        "order_intent": {
            "contract_preview": {"symbol": symbol, "sec_type": "STK", "currency": "USD"},
            "order_preview": {"source_order_action": "BUY", "total_quantity": 1, "order_type": "MKT"},
        },
        "local_margin_estimate": {
            "estimated_cash_impact": -100.0,
            "estimated_initial_margin_change": 0.0,
            "estimate_method": "unit_test",
        },
    }


def _capital_gate_payload() -> dict:
    return {
        "capital_aware_action_gate_version": "m239-capital-aware-action-sizing-gate-v1",
        "autonomous_execution_gate_version": "m239-capital-aware-action-sizing-gate-v1",
        "generated_at": "2026-07-03T10:00:00+00:00",
        "capital_ready_count": 1,
        "capital_resized_count": 0,
        "capital_blocked_count": 1,
        "capital_gate_status_counts": {"capital_ready": 1, "capital_blocked": 1},
        "capital_state": {"status": "green", "leverage_mode": "cash_only"},
        "auto_execution_items": [
            _auto_item("SPY", capital_status="capital_ready"),
            _auto_item("QQQ", capital_status="capital_blocked", auto_status="auto_execution_blocked"),
        ],
    }


def _portfolio_risk_within_limits() -> dict:
    return {"risk_state": "within_limits", "new_entries_allowed": True, "risk_run_id": "risk_1"}


def test_m242_ledger_and_router_only_execute_capital_ready_items() -> None:
    ledger = build_execution_intent_ledger_report(
        _capital_gate_payload(),
        portfolio_risk_state_payload=_portfolio_risk_within_limits(),
        require_capital_aware_gate=True,
        max_intents=10,
    )

    assert ledger["source_has_capital_aware_gate"] is True
    assert ledger["capital_aware_gate_applied"] is True
    assert ledger["source_capital_blocked_count"] == 1
    assert ledger["intent_count"] == 1
    assert ledger["pending_paper_execution_count"] == 1
    assert ledger["intents"][0]["symbol"] == "SPY"
    assert ledger["intents"][0]["capital_gate_status"] == "capital_ready"
    assert ledger["intents"][0]["capital_aware_gate_applied"] is True

    router = build_paper_order_router_report(
        {"result": ledger},
        require_capital_aware_gate=True,
        require_portfolio_risk_gate=True,
        require_operating_preflight=False,
        require_session_transition_release_governor=False,
    )

    assert router["require_capital_aware_gate"] is True
    assert router["source_has_capital_aware_gate"] is True
    assert router["paper_order_count"] == 1
    assert router["paper_fill_count"] == 1
    assert router["paper_orders"][0]["symbol"] == "SPY"
    assert router["paper_orders"][0]["capital_gate_status"] == "capital_ready"
    assert router["safety"]["ibkr_order_submitted"] is False


def test_m242_router_blocks_legacy_ledger_when_capital_gate_required() -> None:
    legacy_auto_gate = {
        "autonomous_execution_gate_version": "paper-autonomous-execution-gate-v1",
        "auto_execution_items": [_auto_item("SPY")],
    }
    ledger = build_execution_intent_ledger_report(
        legacy_auto_gate,
        portfolio_risk_state_payload=_portfolio_risk_within_limits(),
        require_capital_aware_gate=False,
        max_intents=10,
    )

    assert ledger["source_has_capital_aware_gate"] is False
    assert ledger["pending_paper_execution_count"] == 1

    router = build_paper_order_router_report(
        {"result": ledger},
        require_capital_aware_gate=True,
        require_portfolio_risk_gate=True,
        require_operating_preflight=False,
        require_session_transition_release_governor=False,
    )

    assert router["paper_order_count"] == 0
    assert router["skipped_counts"]["missing_required_capital_aware_gate"] == 1
    assert router["safety"]["ibkr_order_submitted"] is False
