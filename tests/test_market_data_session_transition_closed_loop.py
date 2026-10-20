from __future__ import annotations

import json
from pathlib import Path

from recorder.market_transition_operations import (
    market_data_session_transition_closed_loop_markdown,
    run_market_data_session_transition_closed_loop,
    write_market_data_session_transition_closed_loop_json,
    write_market_data_session_transition_closed_loop_markdown,
)
from main import _run_dashboard_session_transition_closed_loop


def _market_data_payload() -> dict:
    summary = {
        "status": "red",
        "temperature_label": "stress",
        "temperature_score": 35.0,
        "row_count": 2,
        "live_count": 2,
        "delayed_count": 0,
        "missing_price_count": 0,
        "warming_up_count": 0,
        "error_count": 0,
        "alert_count": 1,
        "market_move_alert_count": 1,
        "scored_market_move_alert_count": 1,
        "quality_excluded_market_move_alert_count": 0,
        "liquidity_alert_count": 0,
        "data_alert_count": 0,
        "close_only_count": 0,
        "stale_count": 0,
        "wide_spread_count": 0,
        "severe_wide_spread_count": 0,
        "risk_flags": ["market_data_temperature_stress"],
        "delayed_instruments": [],
        "missing_instruments": [],
        "warming_up_instruments": [],
        "error_instruments": [],
        "stale_instruments": [],
        "close_only_instruments": [],
        "wide_spread_instruments": [],
        "severe_wide_spread_instruments": [],
        "alerted_instruments": ["NQ"],
        "group_summaries": {},
        "top_moves": [],
    }
    return {
        "market_data_desk_version": "market-data-desk-v4",
        "generated_at": "2026-07-01T02:00:00+00:00",
        "status": "red",
        "temperature": {"label": "stress", "score": 35.0, "risk_flags": ["market_data_temperature_stress"]},
        "summary": summary,
        "rows": [
            {"key": "NQ", "group": "us_equity_futures", "sec_type": "FUT", "price": 100.0, "alerts": ["gap_up:+2.0%"], "status": "live"},
            {"key": "VIX", "group": "volatility", "sec_type": "IND", "price": 20.0, "alerts": [], "status": "live"},
        ],
    }


def _stale_overlay() -> dict:
    return {
        "market_data_session_overlay_version": "market-data-session-overlay-v1",
        "generated_at": "2026-07-01T01:00:00+00:00",
        "session": {"session": "overnight", "is_regular_session": False},
        "risk_interpretation": "overnight_monitoring_active",
        "summary": {"monitorability_counts": {"monitorable_now": 1}, "monitorable_alert_instruments": ["NQ"]},
    }


def _preview_payload() -> dict:
    return {
        "order_preview_version": "paper-order-preview-margin-whatif-v1",
        "preview_item_count": 1,
        "account_snapshot": {"effective_cash": 1000.0},
        "preview_items": [
            {
                "preview_status": "preview_ready",
                "symbol": "NVDA",
                "ticket_type": "stock",
                "ticket_draft": {
                    "ticket_type": "stock",
                    "order_action": "BUY",
                    "quantity": 1,
                    "estimated_notional": 100.0,
                },
                "contract_preview": {"sec_type": "STK", "symbol": "NVDA", "exchange": "SMART", "currency": "USD"},
                "ibkr_what_if_order_preview": {
                    "action": "BUY",
                    "source_order_action": "BUY",
                    "total_quantity": 1,
                    "what_if": True,
                    "transmit": False,
                },
                "local_margin_estimate": {
                    "estimated_initial_margin_change": 100.0,
                    "estimated_cash_impact": -100.0,
                },
            }
        ],
    }


def test_session_transition_closed_loop_rebuilds_and_holds_defensive(tmp_path: Path) -> None:
    market_path = tmp_path / "market.json"
    overlay_path = tmp_path / "overlay.json"
    preview_path = tmp_path / "preview.json"
    market_path.write_text(json.dumps(_market_data_payload()), encoding="utf-8")
    overlay_path.write_text(json.dumps(_stale_overlay()), encoding="utf-8")
    preview_path.write_text(json.dumps(_preview_payload()), encoding="utf-8")

    report = run_market_data_session_transition_closed_loop(
        market_data_json_path=market_path,
        current_session_overlay_path=overlay_path,
        previous_state_path=tmp_path / "state.json",
        market_data_risk_gate_path=None,
        market_data_operating_auto_gate_path=None,
        preview_json_path=preview_path,
        short_availability_json_path=None,
        evidence_preflight_json_path=None,
        output_dir=tmp_path,
        now="2026-07-01T02:05:00+00:00",
    )

    assert report["status"] == "rebuild_completed"
    assert report["controls"]["initial_decision"] == "rebuild_required"
    assert report["controls"]["final_decision"] == "hold_defensive"
    assert report["controls"]["final_rebuild_required_before_auto_release"] is False
    assert report["step_count"] == 6
    assert Path(report["artifacts"]["market_data_operating_auto_gate_json"]).exists()
    assert "Market Data Session Transition Closed Loop" in market_data_session_transition_closed_loop_markdown(report)


def test_session_transition_closed_loop_dry_run_does_not_rebuild(tmp_path: Path) -> None:
    market_path = tmp_path / "market.json"
    overlay_path = tmp_path / "overlay.json"
    preview_path = tmp_path / "preview.json"
    market_path.write_text(json.dumps(_market_data_payload()), encoding="utf-8")
    overlay_path.write_text(json.dumps(_stale_overlay()), encoding="utf-8")
    preview_path.write_text(json.dumps(_preview_payload()), encoding="utf-8")

    report = run_market_data_session_transition_closed_loop(
        market_data_json_path=market_path,
        current_session_overlay_path=overlay_path,
        previous_state_path=tmp_path / "state.json",
        market_data_risk_gate_path=None,
        market_data_operating_auto_gate_path=None,
        preview_json_path=preview_path,
        short_availability_json_path=None,
        evidence_preflight_json_path=None,
        output_dir=tmp_path,
        execute=False,
        now="2026-07-01T02:05:00+00:00",
    )

    assert report["status"] == "dry_run_rebuild_required"
    assert report["step_count"] == 0
    assert not (tmp_path / "m199_market_data_operating_auto_gate.json").exists()


def test_session_transition_closed_loop_writers(tmp_path: Path) -> None:
    report = {
        "market_data_session_transition_closed_loop_version": "market-data-session-transition-closed-loop-v1",
        "generated_at": "2026-07-01T02:05:00+00:00",
        "status": "skipped_no_rebuild_required",
        "reason": "test",
        "execute_requested": False,
        "controls": {"initial_decision": "monitor_only", "final_decision": "monitor_only"},
        "steps": [],
        "final_transition_gate": {},
    }
    json_path = write_market_data_session_transition_closed_loop_json(tmp_path / "loop.json", report)
    md_path = write_market_data_session_transition_closed_loop_markdown(tmp_path / "loop.md", report)

    assert json_path.exists()
    assert md_path.exists()


def test_dashboard_session_transition_closed_loop_hook_profile(tmp_path: Path) -> None:
    market_path = tmp_path / "market.json"
    overlay_path = tmp_path / "overlay.json"
    preview_path = tmp_path / "preview.json"
    state_path = tmp_path / "state.json"
    output_dir = tmp_path / "exports"
    market_path.write_text(json.dumps(_market_data_payload()), encoding="utf-8")
    overlay_path.write_text(json.dumps(_stale_overlay()), encoding="utf-8")
    preview_path.write_text(json.dumps(_preview_payload()), encoding="utf-8")
    profile = {
        "market_data_session_transition_closed_loop": {
            "enabled": True,
            "market_data_json": str(market_path),
            "market_data_session_overlay_json": str(overlay_path),
            "state_json_path": str(state_path),
            "preview_json": str(preview_path),
            "output_dir": str(output_dir),
            "output": str(tmp_path / "loop.md"),
            "json_output_path": str(tmp_path / "loop.json"),
            "max_session_overlay_age_seconds": 180.0,
            "max_risk_gate_age_seconds": 180.0,
            "max_operating_gate_age_seconds": 180.0,
        }
    }

    summary = _run_dashboard_session_transition_closed_loop(
        profile=profile,
        profile_base=None,
        run_id="test_dashboard_hook",
    )

    assert summary["enabled"] is True
    assert summary["applied"] is True
    assert summary["hook"] == "market_data_session_transition_closed_loop"
    assert summary["status"] == "rebuild_completed"
    assert summary["initial_decision"] == "rebuild_required"
    assert summary["final_decision"] == "hold_defensive"
    assert summary["final_rebuild_required_before_auto_release"] is False
    assert Path(summary["json_output_path"]).exists()
    assert Path(summary["final_transition_gate_json"]).exists()
