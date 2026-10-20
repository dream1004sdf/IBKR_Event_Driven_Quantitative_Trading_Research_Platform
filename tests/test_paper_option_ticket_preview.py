from __future__ import annotations

from datetime import UTC, datetime

from recorder.paper_option_ticket_preview import (
    build_paper_option_ticket_preview_report,
    paper_option_ticket_preview_markdown,
)


def _ready_candidate(**overrides) -> dict:
    row = {
        "symbol": "SPY",
        "direction_context": "bullish",
        "direction_match": "aligned",
        "strategy": "long_call",
        "right": "C",
        "expiration": "20260710",
        "dte": 7,
        "strike": 744.0,
        "exchange": "SMART",
        "trading_class": "SPY",
        "reference_price": 744.37,
        "bid": 0.87,
        "ask": 0.90,
        "mid": 0.885,
        "total_ask_debit": 90.0,
        "max_loss": 90.0,
        "spread": 0.03,
        "spread_pct": 0.033,
        "volume": 1000,
        "open_interest": 250,
        "quote_status": "quoted",
        "delayed": False,
        "candidate_status": "ready",
        "paper_candidate_ready": True,
        "requires_fresh_quote": False,
        "score": 93.5,
        "structure_context": {
            "confidence": "medium",
            "gamma_regime": "negative_gamma_trend_amplification",
            "pin_risk": False,
        },
        "micro_policy": {"ready": True, "status": "allowed", "operating_mode": "micro_cash_only"},
        "requirements": {
            "cash_required": 90.0,
            "initial_margin_required": 90.0,
            "risk_required": 90.0,
            "option_premium_required": 90.0,
        },
        "candidate_contract": {
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
        "reasons": [],
    }
    row.update(overrides)
    return row


def _m247_payload(candidate: dict, *, status: str = "green", expected_deferred: bool = False, option_budget: float = 106.47) -> dict:
    return {
        "micro_option_candidate_selector_version": "m247-micro-option-candidate-selector-v1",
        "generated_at": "2026-07-03T14:10:00+00:00",
        "status": status,
        "expected_deferred": expected_deferred,
        "summary": {"candidate_count": 1, "ready_count": int(candidate.get("paper_candidate_ready") is True), "option_budget": option_budget},
        "candidates": [candidate],
    }


def test_m248_builds_single_long_option_paper_ticket_from_ready_m247_candidate() -> None:
    report = build_paper_option_ticket_preview_report(
        micro_option_candidates_payload=_m247_payload(_ready_candidate()),
        max_tickets=3,
        now=datetime(2026, 7, 3, 14, 15, tzinfo=UTC),
    )

    assert report["status"] == "green"
    assert report["summary"]["ticket_count"] == 1
    ticket = report["ticket_previews"][0]
    assert ticket["ticket_type"] == "single_long_option"
    assert ticket["order_preview"]["order_type"] == "LMT"
    assert ticket["order_preview"]["limit_price"] == 0.90
    assert ticket["order_preview"]["estimated_total_debit"] == 90.0
    assert ticket["order_preview"]["transmit"] is False
    assert ticket["safety"]["ibkr_order_submitted"] is False
    leg = ticket["legs"][0]
    assert leg["action"] == "BUY_TO_OPEN"
    assert leg["contract"]["symbol"] == "SPY"
    assert leg["contract"]["right"] == "C"
    assert "M248 Paper Option Ticket Preview" in paper_option_ticket_preview_markdown(report)


def test_m248_marks_expected_deferred_source_without_ticket() -> None:
    candidate = _ready_candidate(
        candidate_status="blocked",
        paper_candidate_ready=False,
        requires_fresh_quote=True,
        reasons=["quote_snapshot_stale", "option_market_closed_or_quotes_deferred"],
    )

    report = build_paper_option_ticket_preview_report(
        micro_option_candidates_payload=_m247_payload(candidate, status="expected_deferred", expected_deferred=True),
        now=datetime(2026, 7, 4, 0, 15, tzinfo=UTC),
    )

    assert report["status"] == "expected_deferred"
    assert report["summary"]["ticket_count"] == 0
    assert report["safety"]["whatif_request_submitted_by_m248"] is False
    assert "option_market_closed_or_quotes_deferred" in report["summary"]["top_block_reason_counts"]


def test_m248_blocks_candidate_when_ticket_debit_exceeds_option_budget() -> None:
    report = build_paper_option_ticket_preview_report(
        micro_option_candidates_payload=_m247_payload(_ready_candidate(ask=1.20, mid=1.19), option_budget=100.0),
        now=datetime(2026, 7, 3, 14, 15, tzinfo=UTC),
    )

    assert report["status"] == "red"
    assert report["summary"]["ticket_count"] == 0
    review = report["candidate_reviews"][0]
    assert review["preview_status"] == "ticket_blocked"
    assert "ticket_debit_exceeds_option_budget" in review["block_reasons"]


def test_m248_can_build_defined_risk_debit_spread_preview_from_paired_candidates() -> None:
    long_call = _ready_candidate(strike=744.0, ask=1.00, bid=0.96, mid=0.98, total_ask_debit=100.0)
    short_call = _ready_candidate(strike=746.0, ask=0.45, bid=0.40, mid=0.425, total_ask_debit=45.0, score=80.0)
    report = build_paper_option_ticket_preview_report(
        micro_option_candidates_payload={
            "micro_option_candidate_selector_version": "m247-micro-option-candidate-selector-v2-ticket-risk-cap",
            "generated_at": "2026-07-03T14:10:00+00:00",
            "status": "green",
            "expected_deferred": False,
            "summary": {"candidate_count": 2, "ready_count": 2, "option_budget": 106.47, "max_option_ticket_debit": 80.0},
            "candidates": [long_call, short_call],
        },
        max_tickets=3,
        now=datetime(2026, 7, 3, 14, 15, tzinfo=UTC),
    )

    spreads = [row for row in report["ticket_previews"] if row["ticket_type"] == "defined_risk_debit_spread"]
    assert spreads
    spread = spreads[0]
    assert len(spread["legs"]) == 2
    assert spread["order_preview"]["estimated_total_debit"] <= 80.0
    assert spread["risk_preview"]["defined_risk"] is True


def test_m248_applies_gate_haircut_to_effective_max_ticket_debit_without_submitting_orders() -> None:
    candidate = _ready_candidate(
        ask=0.30,
        mid=0.295,
        total_ask_debit=30.0,
        gate_severity={
            "recommended_size_multiplier": 0.5,
            "confidence_multiplier": 0.85,
            "posture": "size_reduced",
        },
        cautions=["gate_severity_option_budget_haircut_active"],
    )

    report = build_paper_option_ticket_preview_report(
        micro_option_candidates_payload=_m247_payload(candidate, option_budget=100.0),
        max_ticket_debit=100.0,
        now=datetime(2026, 7, 3, 14, 15, tzinfo=UTC),
    )

    ticket = report["ticket_previews"][0]
    review = report["candidate_reviews"][0]

    assert report["status"] == "green"
    assert report["summary"]["gate_severity_ticket_haircut_count"] == 1
    assert review["effective_max_ticket_debit"] == 50.0
    assert ticket["risk_preview"]["effective_max_ticket_debit"] == 50.0
    assert ticket["risk_preview"]["gate_severity_size_multiplier"] == 0.5
    assert ticket["source"]["source_cautions"] == ["gate_severity_option_budget_haircut_active"]
    assert ticket["safety"]["ibkr_order_submitted"] is False
    assert ticket["order_preview"]["transmit"] is False


def test_m248_blocks_ticket_when_gate_haircut_reduces_ticket_budget_below_debit() -> None:
    candidate = _ready_candidate(
        ask=0.80,
        mid=0.795,
        total_ask_debit=80.0,
        gate_severity={"recommended_size_multiplier": 0.5},
    )

    report = build_paper_option_ticket_preview_report(
        micro_option_candidates_payload=_m247_payload(candidate, option_budget=100.0),
        max_ticket_debit=100.0,
        now=datetime(2026, 7, 3, 14, 15, tzinfo=UTC),
    )

    assert report["status"] == "red"
    assert report["summary"]["ticket_count"] == 0
    assert report["candidate_reviews"][0]["effective_max_ticket_debit"] == 50.0
    assert "ticket_debit_exceeds_max_ticket_debit" in report["candidate_reviews"][0]["block_reasons"]


def test_m248_blocks_ticket_when_gate_multiplier_is_zero() -> None:
    candidate = _ready_candidate(
        ask=0.01,
        mid=0.01,
        total_ask_debit=1.0,
        gate_severity={"recommended_size_multiplier": 0.0},
    )

    report = build_paper_option_ticket_preview_report(
        micro_option_candidates_payload=_m247_payload(candidate, option_budget=100.0),
        max_ticket_debit=100.0,
        now=datetime(2026, 7, 3, 14, 15, tzinfo=UTC),
    )

    assert report["status"] == "red"
    assert report["summary"]["ticket_count"] == 0
    assert report["candidate_reviews"][0]["gate_severity_size_multiplier"] == 0.0
    assert "gate_severity_policy_blocks_new_entries" in report["candidate_reviews"][0]["block_reasons"]
