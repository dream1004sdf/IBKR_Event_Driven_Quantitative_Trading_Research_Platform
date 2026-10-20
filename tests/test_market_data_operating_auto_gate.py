from __future__ import annotations

import json
from pathlib import Path

from recorder.market_data_operating_auto_gate import (
    build_market_data_operating_auto_gate_from_paths,
    build_market_data_operating_auto_gate_report,
    market_data_operating_auto_gate_markdown,
    write_market_data_operating_auto_gate_json,
    write_market_data_operating_auto_gate_markdown,
)


def _auto_gate(status: str = "auto_execution_ready") -> dict:
    return {
        "autonomous_execution_gate_version": "paper-autonomous-execution-gate-v1",
        "generated_at": "2026-07-01T00:00:00+00:00",
        "source_preview_item_count": 1,
        "auto_ready_count": 1 if status == "auto_execution_ready" else 0,
        "auto_gate_status_counts": {status: 1},
        "auto_gate_block_reason_counts": {},
        "gate_parameters": {"max_auto_orders": 10},
        "auto_execution_items": [
            {
                "auto_execution_status": status,
                "symbol": "NVDA",
                "ticket_type": "stock",
                "action": "BUY",
                "quantity": 10,
                "checks": [{"name": "test", "passed": True}],
                "block_reasons": [],
                "market_data_entry_size_multiplier": 1.0,
                "source_preview_item": {"source_score_id": 1, "source_outcome_id": 2, "ticket_draft": {"ticket_type": "stock", "order_action": "BUY"}},
            }
        ],
        "sample_auto_execution_items": [],
        "safety": {"ibkr_order_submitted": False, "transmit_enabled": False},
    }


def _auto_gate_with_session_context() -> dict:
    payload = _auto_gate()
    payload["market_data_risk_gate"] = {
        "market_data_risk_gate_version": "market-data-risk-gate-v1",
        "action": "allow_with_session_context_haircut",
        "entry_size_multiplier": 0.6,
        "market_data_session_context": {
            "session_priority_class": "regular_dependent_after_hours_context",
            "session_name": "overnight",
            "is_regular_session": False,
            "monitorable_alert_instruments": [],
            "regular_dependent_alert_instruments": ["VIX", "VVIX"],
        },
        "session_adjusted_priority": True,
    }
    payload["auto_execution_items"][0]["market_data_risk_gate"] = payload["market_data_risk_gate"]
    payload["auto_execution_items"][0]["market_data_entry_size_multiplier"] = 0.6
    return payload


def _defensive_mode(mode: str = "risk_reduction_only") -> dict:
    risk_reduction = mode.startswith("risk_reduction")
    return {
        "market_data_defensive_mode_version": "market-data-defensive-operating-mode-v1",
        "generated_at": "2026-07-01T00:00:00+00:00",
        "mode": mode,
        "severity": "red" if risk_reduction else "yellow",
        "controls": {
            "ordinary_alpha_entries_allowed": not risk_reduction,
            "hedge_or_defensive_entries_allowed": not risk_reduction,
            "exit_or_risk_reduction_allowed": True,
            "protective_option_analysis_allowed": True,
            "protective_option_ticketing_allowed": False,
            "max_new_entry_size_multiplier": 0.0 if risk_reduction else 0.5,
            "allowed_option_structures": ["protective_put_analysis_only", "collar_analysis_only"],
        },
        "scorer_overlay": {"raise_min_confidence": 0.15 if risk_reduction else 0.05},
        "safety": {"read_only_operating_mode": True},
    }


def test_market_data_operating_auto_gate_blocks_alpha_and_builds_protective_packet() -> None:
    report = build_market_data_operating_auto_gate_report(
        _auto_gate(),
        _defensive_mode("risk_reduction_only"),
        now="2026-07-01T00:01:00+00:00",
    )

    assert report["autonomous_execution_gate_version"] == "market-data-operating-auto-gate-v1"
    assert report["source_autonomous_execution_gate_version"] == "paper-autonomous-execution-gate-v1"
    assert report["auto_ready_count"] == 0
    item = report["auto_execution_items"][0]
    assert item["auto_execution_status"] == "auto_execution_blocked"
    assert "market_data_defensive_mode_blocks_ordinary_alpha_entries" in item["block_reasons"]
    assert item["protective_option_analysis_candidate"] is True
    assert report["protective_analysis_packet"]["candidate_count"] == 1
    assert report["protective_analysis_packet"]["ticketing_allowed"] is False
    assert report["safety"]["ibkr_order_submitted"] is False


def test_market_data_operating_auto_gate_applies_reduced_size_multiplier() -> None:
    report = build_market_data_operating_auto_gate_report(
        _auto_gate(),
        _defensive_mode("reduced_size_defined_risk"),
        now="2026-07-01T00:01:00+00:00",
    )

    assert report["auto_ready_count"] == 1
    item = report["auto_execution_items"][0]
    assert item["auto_execution_status"] == "auto_execution_ready"
    assert item["market_data_entry_size_multiplier"] == 0.5
    assert item["market_data_defensive_mode_size_multiplier"] == 0.5
    assert report["protective_analysis_packet"]["candidate_count"] == 0


def test_market_data_operating_auto_gate_carries_session_priority_context() -> None:
    report = build_market_data_operating_auto_gate_report(
        _auto_gate_with_session_context(),
        _defensive_mode("reduced_size_defined_risk"),
        now="2026-07-01T00:01:00+00:00",
    )

    assert report["market_data_session_context"]["session_priority_class"] == "regular_dependent_after_hours_context"
    assert report["market_data_session_adjusted_priority"] is True
    assert report["market_data_operating_mode"]["session_adjusted_priority"] is True
    assert report["gate_parameters"]["market_data_session_priority_class"] == "regular_dependent_after_hours_context"
    item = report["auto_execution_items"][0]
    assert item["market_data_session_context"]["regular_dependent_alert_instruments"] == ["VIX", "VVIX"]
    assert item["market_data_session_adjusted_priority"] is True
    assert "regular_dependent_after_hours_context" in market_data_operating_auto_gate_markdown(report)


def test_market_data_operating_auto_gate_from_paths_and_writers(tmp_path: Path) -> None:
    auto_path = tmp_path / "auto.json"
    mode_path = tmp_path / "mode.json"
    auto_path.write_text(json.dumps(_auto_gate()), encoding="utf-8")
    mode_path.write_text(json.dumps(_defensive_mode()), encoding="utf-8")

    report = build_market_data_operating_auto_gate_from_paths(
        auto_gate_path=auto_path,
        defensive_mode_path=mode_path,
        now="2026-07-01T00:01:00+00:00",
    )
    markdown = market_data_operating_auto_gate_markdown(report)
    assert "Market Data Operating-Mode Auto Gate" in markdown
    assert "Protective analysis packet" in markdown
    json_path = write_market_data_operating_auto_gate_json(tmp_path / "m182.json", report)
    md_path = write_market_data_operating_auto_gate_markdown(tmp_path / "m182.md", report)
    assert json_path.exists()
    assert md_path.exists()
