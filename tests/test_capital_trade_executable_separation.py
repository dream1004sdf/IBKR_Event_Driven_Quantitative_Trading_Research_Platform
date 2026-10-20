from __future__ import annotations

from recorder.capital_trade_executable_separation import (
    build_capital_trade_executable_separation_report,
    capital_trade_executable_separation_markdown,
)


def test_m245_summarizes_capital_feasible_but_not_trade_executable() -> None:
    report = build_capital_trade_executable_separation_report(
        {
            "live_read_only_capital_validation_version": "m243-live-read-only-capital-validation-v1",
            "trade_executable_split_version": "m245-capital-feasible-vs-trade-executable-v1",
            "generated_at": "2026-07-04T00:00:00+00:00",
            "status": "yellow",
            "current_size_feasible_count": 1,
            "resize_required_count": 0,
            "blocked_count": 0,
            "trade_executable_current_size_count": 0,
            "trade_executable_after_resize_count": 0,
            "not_trade_executable_count": 1,
            "validation_items": [
                {
                    "symbol": "SPY",
                    "action": "BUY",
                    "ticket_type": "stock",
                    "status": "live_read_only_current_size_feasible",
                    "current_size_feasible": True,
                    "resize_possible": False,
                    "execution_gate": {
                        "status": "not_trade_executable",
                        "trade_executable": False,
                        "trade_executable_after_resize": False,
                        "capital_gate_ready": True,
                        "source_gate_ready": False,
                        "market_gate_ready": False,
                        "session_gate_ready": True,
                        "cash_only_gate_ready": True,
                        "broker_gate_ready": True,
                        "micro_policy_gate_ready": True,
                        "execution_block_reasons": ["source_gate_not_ready", "market_gate_not_ready"],
                    },
                }
            ],
        },
        now="2026-07-04T00:01:00+00:00",
    )

    assert report["capital_trade_executable_separation_version"] == "m245-capital-feasible-vs-trade-executable-summary-v2-gate-attribution"
    assert report["status"] == "yellow"
    assert report["summary"]["capital_feasible_but_not_trade_executable_count"] == 1
    assert report["gate_counts"]["source"] == 1
    assert report["gate_counts"]["market"] == 1
    assert "capital_capacity" not in report["gate_counts"]
    assert report["safety"]["ibkr_order_submitted"] is False
    assert "Capital Feasible vs Trade Executable" in capital_trade_executable_separation_markdown(report)
