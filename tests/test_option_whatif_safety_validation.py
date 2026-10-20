from __future__ import annotations

from datetime import UTC, datetime

from recorder.option_whatif_safety_validation import (
    build_option_whatif_safety_validation_report,
    option_whatif_safety_validation_markdown,
)


def _capital_state() -> dict:
    return {
        "status": "green",
        "leverage_mode": "cash_only",
        "budgets": {
            "deployable_cash_after_reserve": 304.20,
            "core_risk_budget": 106.47,
            "options_premium_budget": 106.47,
            "max_new_initial_margin": 94.80,
        },
    }


def _ticket() -> dict:
    return {
        "ticket_id": "m248_SPY_20260710_C744_00",
        "ticket_type": "single_long_option",
        "strategy": "long_call",
        "underlying": {"symbol": "SPY", "reference_price": 744.37},
        "legs": [
            {
                "leg_id": "leg_1",
                "action": "BUY_TO_OPEN",
                "quantity": 1,
                "contract": {
                    "symbol": "SPY",
                    "secType": "OPT",
                    "exchange": "SMART",
                    "currency": "USD",
                    "lastTradeDateOrContractMonth": "20260710",
                    "strike": 744.0,
                    "right": "C",
                    "multiplier": "100",
                    "tradingClass": "SPY",
                },
                "quote": {
                    "bid": 0.87,
                    "ask": 0.90,
                    "mid": 0.885,
                    "spread": 0.03,
                    "spread_pct": 0.033,
                    "quote_status": "quoted",
                    "delayed": False,
                },
            }
        ],
        "order_preview": {
            "order_type": "LMT",
            "side": "BUY",
            "tif": "DAY",
            "outside_rth": False,
            "limit_price": 0.90,
            "estimated_total_debit": 90.0,
            "estimated_max_loss": 90.0,
            "transmit": False,
            "what_if": False,
        },
        "safety": {"paper_ticket_preview_only": True, "ibkr_order_submitted": False, "transmit_enabled": False},
    }


def _m248_payload(*, tickets: list[dict] | None = None, expected_deferred: bool = False) -> dict:
    return {
        "paper_option_ticket_preview_version": "m248-paper-option-ticket-preview-v1",
        "generated_at": "2026-07-03T14:10:00+00:00",
        "status": "expected_deferred" if expected_deferred else "green",
        "expected_deferred": expected_deferred,
        "summary": {"ticket_count": len(tickets or [])},
        "ticket_previews": tickets or [],
    }


def _fresh_quote_snapshot() -> dict:
    return {"generated_at": "2026-07-03T14:10:00+00:00", "status_counts": {"quoted": 1}, "quoted_rate": 1.0}


def test_m249_local_pass_but_marks_missing_broker_whatif_as_pending_when_allowed() -> None:
    report = build_option_whatif_safety_validation_report(
        paper_option_ticket_preview_payload=_m248_payload(tickets=[_ticket()]),
        capital_state_payload=_capital_state(),
        option_quote_snapshot_payload=_fresh_quote_snapshot(),
        require_broker_whatif=False,
        now=datetime(2026, 7, 3, 14, 15, tzinfo=UTC),
    )

    assert report["status"] == "yellow"
    item = report["validation_items"][0]
    assert item["status"] == "whatif_pending"
    assert item["local_safety_passed"] is True
    assert item["final_paper_executable"] is False
    assert item["broker_whatif_status"] == "pending"


def test_m249_passes_with_matching_broker_whatif_response() -> None:
    ticket = _ticket()
    broker_whatif = {
        "option_ticket_whatif_version": "m249-ibkr-option-ticket-whatif-v1",
        "generated_at": "2026-07-03T14:11:00+00:00",
        "ticket_results": [
            {
                "ticket_id": ticket["ticket_id"],
                "contract": ticket["legs"][0]["contract"],
                "order_state": {
                    "status": "PreSubmitted",
                    "initMarginChange": "90.00",
                    "maintMarginChange": "90.00",
                    "warningText": "",
                },
            }
        ],
    }

    report = build_option_whatif_safety_validation_report(
        paper_option_ticket_preview_payload=_m248_payload(tickets=[ticket]),
        capital_state_payload=_capital_state(),
        option_quote_snapshot_payload=_fresh_quote_snapshot(),
        broker_whatif_payload=broker_whatif,
        require_broker_whatif=True,
        now=datetime(2026, 7, 3, 14, 15, tzinfo=UTC),
    )

    assert report["status"] == "green"
    item = report["validation_items"][0]
    assert item["status"] == "passed"
    assert item["final_paper_executable"] is True
    assert item["broker_whatif_status"] == "passed"
    assert "M249 Option What-If" in option_whatif_safety_validation_markdown(report)


def test_m249_expected_deferred_when_m248_has_no_tickets_due_to_closed_option_session() -> None:
    report = build_option_whatif_safety_validation_report(
        paper_option_ticket_preview_payload=_m248_payload(tickets=[], expected_deferred=True),
        capital_state_payload=_capital_state(),
        now=datetime(2026, 7, 4, 0, 15, tzinfo=UTC),
    )

    assert report["status"] == "expected_deferred"
    assert report["expected_deferred"] is True
    assert report["summary"]["validation_item_count"] == 0
    assert report["safety"]["ibkr_order_submitted"] is False


def test_m249_local_checks_support_defined_risk_debit_spread_without_broker_combo_refresh() -> None:
    ticket = _ticket()
    ticket["ticket_id"] = "m248_SPY_20260710_bull_call_debit_spread_01"
    ticket["ticket_type"] = "defined_risk_debit_spread"
    ticket["strategy"] = "bull_call_debit_spread"
    ticket["legs"].append(
        {
            "leg_id": "leg_2_short",
            "action": "SELL_TO_OPEN",
            "quantity": 1,
            "contract": {
                **ticket["legs"][0]["contract"],
                "strike": 746.0,
            },
            "quote": {
                "bid": 0.40,
                "ask": 0.45,
                "mid": 0.425,
                "spread": 0.05,
                "spread_pct": 0.12,
                "quote_status": "quoted",
                "delayed": False,
            },
        }
    )
    ticket["order_preview"]["limit_price"] = 0.50
    ticket["order_preview"]["estimated_total_debit"] = 50.0
    ticket["order_preview"]["estimated_max_loss"] = 50.0
    ticket["order_preview"]["estimated_max_profit"] = 150.0
    ticket["risk_preview"] = {"max_ticket_debit": 75.0, "defined_risk": True}

    report = build_option_whatif_safety_validation_report(
        paper_option_ticket_preview_payload=_m248_payload(tickets=[ticket]),
        capital_state_payload=_capital_state(),
        option_quote_snapshot_payload=_fresh_quote_snapshot(),
        require_broker_whatif=False,
        now=datetime(2026, 7, 3, 14, 15, tzinfo=UTC),
    )

    item = report["validation_items"][0]
    assert item["local_safety_passed"] is True
    assert item["status"] == "whatif_pending"
    assert item["broker_combo_whatif_preparation"]["status"] == "missing_contract_details"
    assert "leg_1_contract_conId" in item["broker_combo_whatif_preparation"]["missing_fields"]
    assert report["summary"]["broker_combo_preparation_count"] == 1
    assert report["summary"]["broker_combo_missing_contract_detail_count"] == 1
    assert report["broker_combo_whatif_preparation"]["enabled"] is False
    assert report["policy"]["multi_leg_combo_enabled"] is True
    assert report["policy"]["broker_combo_whatif_enabled"] is False
