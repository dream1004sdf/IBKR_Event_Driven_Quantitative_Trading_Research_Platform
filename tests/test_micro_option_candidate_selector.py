from __future__ import annotations

from datetime import UTC, datetime

from recorder.micro_option_candidate_selector import (
    build_micro_option_candidate_selector_report,
    micro_option_candidate_selector_markdown,
)


def _capital_state() -> dict:
    return {
        "status": "green",
        "leverage_mode": "cash_only",
        "selected_account": "U1",
        "metrics": {
            "net_liquidation": 380.64,
            "total_cash_value": 380.33,
            "available_funds": 379.19,
        },
        "budgets": {
            "deployable_cash_after_reserve": 304.20,
            "core_risk_budget": 106.47,
            "options_premium_budget": 106.47,
            "max_single_position_notional": 45.68,
            "max_new_initial_margin": 94.80,
            "hedge_budget": 7.61,
        },
    }


def _quote_row(*, expiration: str, dte: int, right: str = "C", ask: float = 0.90) -> dict:
    return {
        "symbol": "SPY",
        "expiration": expiration,
        "dte": dte,
        "strike": 744.0,
        "right": right,
        "reference_price": 744.37,
        "moneyness": -0.0005,
        "exchange": "SMART",
        "trading_class": "SPY",
        "multiplier": "100",
        "candidate_contract": {
            "symbol": "SPY",
            "secType": "OPT",
            "exchange": "SMART",
            "currency": "USD",
            "lastTradeDateOrContractMonth": expiration,
            "strike": 744.0,
            "right": right,
            "multiplier": "100",
            "tradingClass": "SPY",
        },
        "market_data_type": 1,
        "bid": round(max(0.01, ask - 0.03), 2),
        "ask": ask,
        "last": ask,
        "bid_size": 10,
        "ask_size": 20,
        "volume": 1000,
        "open_interest": 250,
        "quote_status": "quoted",
        "mid": round(ask - 0.015, 3),
        "spread": 0.03,
        "spread_pct": 0.033,
        "has_bid_ask": True,
        "delayed": False,
    }


def test_micro_option_candidate_selector_allows_fresh_budget_fit_long_call() -> None:
    report = build_micro_option_candidate_selector_report(
        capital_state_payload=_capital_state(),
        strategy_allocation_payload={
            "recommendations": [{"symbol": "SPY", "direction": "bullish"}],
        },
        option_quote_snapshot_payload={
            "snapshot_version": "ibkr-option-quote-snapshot-v1",
            "generated_at": "2026-07-03T14:00:00+00:00",
            "quotes": [_quote_row(expiration="20260710", dte=7, right="C", ask=0.90)],
        },
        options_market_structure_payload={
            "symbols": [
                {
                    "symbol": "SPY",
                    "confidence": "medium",
                    "gamma_regime": "negative_gamma_trend_amplification",
                    "pin_risk": False,
                }
            ]
        },
        symbols=("SPY",),
        now=datetime(2026, 7, 3, 14, 10, tzinfo=UTC),
    )

    assert report["status"] == "green"
    assert report["summary"]["ready_count"] == 1
    assert report["summary"]["option_budget"] == 106.47
    candidate = report["candidates"][0]
    assert candidate["candidate_status"] == "ready"
    assert candidate["strategy"] == "long_call"
    assert candidate["total_ask_debit"] == 90.0
    assert candidate["direction_match"] == "aligned"
    assert candidate["micro_policy"]["ready"] is True
    assert "M247 Micro Cash Option Candidate Selector" in micro_option_candidate_selector_markdown(report)


def test_micro_option_candidate_selector_blocks_expired_contracts() -> None:
    report = build_micro_option_candidate_selector_report(
        capital_state_payload=_capital_state(),
        strategy_allocation_payload={},
        option_quote_snapshot_payload={
            "snapshot_version": "ibkr-option-quote-snapshot-v1",
            "generated_at": "2026-07-03T14:00:00+00:00",
            "quotes": [_quote_row(expiration="20260702", dte=0, right="P", ask=0.20)],
        },
        symbols=("SPY",),
        now=datetime(2026, 7, 3, 14, 10, tzinfo=UTC),
    )

    assert report["status"] == "red"
    assert report["summary"]["blocked_count"] == 1
    candidate = report["candidates"][0]
    assert candidate["candidate_status"] == "blocked"
    assert candidate["dte"] == -1
    assert "expiration_past" in candidate["reasons"]


def test_micro_option_candidate_selector_marks_closed_option_session_as_expected_deferred() -> None:
    report = build_micro_option_candidate_selector_report(
        capital_state_payload=_capital_state(),
        strategy_allocation_payload={},
        option_quote_snapshot_payload={
            "snapshot_version": "ibkr-option-quote-snapshot-v1",
            "generated_at": "2026-07-03T14:00:00+00:00",
            "quotes": [_quote_row(expiration="20260710", dte=7, right="C", ask=0.90)],
        },
        options_market_structure_supervisor_payload={
            "final_session": "post_market",
            "final_option_quote_allowed": False,
        },
        symbols=("SPY",),
        now=datetime(2026, 7, 4, 0, 10, tzinfo=UTC),
    )

    assert report["status"] == "expected_deferred"
    assert report["expected_deferred"] is True
    assert report["summary"]["deferred_reason"] == "option_market_closed_or_quotes_deferred"
    assert "option_market_closed_or_quotes_deferred" in report["summary"]["top_reason_counts"]


def test_micro_option_candidate_selector_applies_gate_haircut_as_caution_not_block() -> None:
    report = build_micro_option_candidate_selector_report(
        capital_state_payload=_capital_state(),
        strategy_allocation_payload={
            "recommendations": [
                {
                    "symbol": "SPY",
                    "direction": "bullish",
                    "recommended_size_multiplier": 0.5,
                    "gate_policy": {
                        "confidence_multiplier": 0.85,
                        "recommended_execution_posture": "size_reduced",
                    },
                }
            ],
        },
        option_quote_snapshot_payload={
            "snapshot_version": "ibkr-option-quote-snapshot-v1",
            "generated_at": "2026-07-03T14:00:00+00:00",
            "quotes": [_quote_row(expiration="20260710", dte=7, right="C", ask=0.30)],
        },
        symbols=("SPY",),
        now=datetime(2026, 7, 3, 14, 10, tzinfo=UTC),
    )

    candidate = report["candidates"][0]

    assert report["status"] == "green"
    assert report["summary"]["ready_count"] == 1
    assert report["summary"]["gate_severity_budget_haircut_count"] == 1
    assert candidate["candidate_status"] == "ready"
    assert candidate["gate_severity"]["recommended_size_multiplier"] == 0.5
    assert candidate["gate_severity"]["effective_max_premium_budget_fraction"] == 0.5
    assert "gate_severity_option_budget_haircut_active" in candidate["cautions"]
    assert "gate_severity_option_budget_haircut_active" not in candidate["reasons"]
