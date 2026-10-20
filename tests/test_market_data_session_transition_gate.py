from __future__ import annotations

import json
from pathlib import Path

from recorder.market_transition_operations import (
    build_market_data_session_transition_gate_from_paths,
    build_market_data_session_transition_gate_report,
    market_data_session_transition_gate_markdown,
    write_market_data_session_transition_gate_json,
    write_market_data_session_transition_gate_markdown,
    write_market_data_session_transition_state,
)


def _session_overlay(session: str = "regular", generated_at: str = "2026-07-01T14:31:00+00:00") -> dict:
    return {
        "market_data_session_overlay_version": "market-data-session-overlay-v1",
        "generated_at": generated_at,
        "session": {"session": session, "is_regular_session": session == "regular"},
        "risk_interpretation": "full_regular_session" if session == "regular" else "overnight_monitoring_active",
        "source_market_data_generated_at": generated_at,
        "summary": {
            "monitorability_counts": {"monitorable_now": 2, "monitor_if_fresh_price_present": 1},
            "monitorable_alert_instruments": [] if session == "regular" else ["NQ"],
            "regular_dependent_alert_instruments": [],
        },
        "stock_cash_overnight_monitoring_enabled": True,
        "option_quotes_regular_session_dependent": session != "regular",
    }


def _state(session: str = "overnight") -> dict:
    return {
        "market_data_session_transition_gate_version": "market-data-session-transition-gate-v1",
        "updated_at": "2026-07-01T08:00:00+00:00",
        "last_session_name": session,
        "last_is_regular_session": session == "regular",
        "last_risk_interpretation": "full_regular_session" if session == "regular" else "overnight_monitoring_active",
        "last_session_overlay_generated_at": "2026-07-01T08:00:00+00:00",
        "last_decision": "hold_defensive",
    }


def _risk_gate(
    *,
    status: str = "pass",
    action: str = "allow_normal",
    generated_at: str = "2026-07-01T14:31:10+00:00",
    session: str = "regular",
    alpha_allowed: bool = True,
    multiplier: float = 1.0,
) -> dict:
    return {
        "market_data_risk_gate_version": "market-data-risk-gate-v1",
        "generated_at": generated_at,
        "gate_status": status,
        "action": action,
        "allowed": alpha_allowed,
        "new_entries_allowed": alpha_allowed,
        "alpha_entries_allowed": alpha_allowed,
        "hedge_entries_allowed": True,
        "entry_size_multiplier": multiplier,
        "market_data_session_context": {
            "session_name": session,
            "is_regular_session": session == "regular",
            "session_priority_class": "monitorable_now_stress" if status == "defensive" else "monitorable_now_stress",
        },
        "blocking_reasons": [],
        "defensive_reasons": ["market_temperature_stress"] if status == "defensive" else [],
        "haircut_reasons": [],
    }


def _operating_gate(
    *,
    mode: str = "normal",
    generated_at: str = "2026-07-01T14:31:20+00:00",
    alpha_allowed: bool = True,
    ready_count: int = 3,
    multiplier: float = 1.0,
) -> dict:
    return {
        "autonomous_execution_gate_version": "market-data-operating-auto-gate-v1",
        "generated_at": generated_at,
        "auto_ready_count": ready_count,
        "market_data_operating_mode": {
            "mode": mode,
            "severity": "green" if alpha_allowed else "red",
            "ordinary_alpha_entries_allowed": alpha_allowed,
            "hedge_or_defensive_entries_allowed": True,
            "max_new_entry_size_multiplier": multiplier,
        },
    }


def test_session_transition_gate_requires_rebuild_on_off_hours_to_regular_transition() -> None:
    report = build_market_data_session_transition_gate_report(
        _session_overlay("regular"),
        previous_state_payload=_state("overnight"),
        market_data_risk_gate_payload=_risk_gate(session="overnight"),
        market_data_operating_auto_gate_payload=_operating_gate(),
        now="2026-07-01T14:31:30+00:00",
    )

    assert report["decision"] == "rebuild_required"
    assert report["severity"] == "yellow"
    assert report["transition"]["transition_type"] == "off_hours_to_regular"
    assert report["artifact_freshness"]["gate_session_mismatch"] is True
    assert report["controls"]["ordinary_alpha_entries_allowed_after_transition"] is False
    assert len(report["recommended_commands"]) == 5
    assert "market-data-risk-gate" in report["recommended_commands"][1]["command"]
    assert "Market Data Session Transition Gate" in market_data_session_transition_gate_markdown(report)


def test_session_transition_gate_releases_only_after_fresh_regular_gates() -> None:
    report = build_market_data_session_transition_gate_report(
        _session_overlay("regular"),
        previous_state_payload=_state("regular"),
        market_data_risk_gate_payload=_risk_gate(session="regular"),
        market_data_operating_auto_gate_payload=_operating_gate(),
        now="2026-07-01T14:31:30+00:00",
    )

    assert report["decision"] == "release_candidate"
    assert report["severity"] == "green"
    assert report["transition"]["transition_type"] == "unchanged"
    assert report["controls"]["rebuild_required_before_auto_release"] is False
    assert report["controls"]["ordinary_alpha_entries_allowed_after_transition"] is True
    assert report["controls"]["max_new_entry_size_multiplier"] == 1.0
    assert report["recommended_commands"] == []


def test_session_transition_gate_holds_defensive_for_fresh_monitorable_overnight_stress() -> None:
    report = build_market_data_session_transition_gate_report(
        _session_overlay("overnight"),
        previous_state_payload=_state("overnight"),
        market_data_risk_gate_payload=_risk_gate(
            status="defensive",
            action="defensive_or_hedge_only",
            session="overnight",
            alpha_allowed=False,
            multiplier=0.0,
        ),
        market_data_operating_auto_gate_payload=_operating_gate(
            mode="hedge_defensive_only",
            alpha_allowed=False,
            ready_count=0,
            multiplier=0.0,
        ),
        now="2026-07-01T14:31:30+00:00",
    )

    assert report["decision"] == "hold_defensive"
    assert report["severity"] == "red"
    assert report["controls"]["ordinary_alpha_entries_allowed_after_transition"] is False
    assert report["controls"]["after_hours_monitorable_stress_keeps_defensive"] is True


def test_session_transition_gate_writers_and_state(tmp_path: Path) -> None:
    overlay_path = tmp_path / "overlay.json"
    state_path = tmp_path / "state.json"
    risk_path = tmp_path / "risk.json"
    operating_path = tmp_path / "operating.json"
    overlay_path.write_text(json.dumps(_session_overlay("regular")), encoding="utf-8")
    state_path.write_text(json.dumps(_state("regular")), encoding="utf-8")
    risk_path.write_text(json.dumps(_risk_gate(session="regular")), encoding="utf-8")
    operating_path.write_text(json.dumps(_operating_gate()), encoding="utf-8")

    report = build_market_data_session_transition_gate_from_paths(
        current_session_overlay_path=overlay_path,
        state_path=state_path,
        market_data_risk_gate_path=risk_path,
        market_data_operating_auto_gate_path=operating_path,
        now="2026-07-01T14:31:30+00:00",
    )
    json_path = write_market_data_session_transition_gate_json(tmp_path / "gate.json", report)
    md_path = write_market_data_session_transition_gate_markdown(tmp_path / "gate.md", report)
    written_state = write_market_data_session_transition_state(state_path, report)

    assert json_path.exists()
    assert md_path.exists()
    assert written_state.exists()
    state_payload = json.loads(state_path.read_text(encoding="utf-8"))
    assert state_payload["last_session_name"] == "regular"
    assert state_payload["history"]
