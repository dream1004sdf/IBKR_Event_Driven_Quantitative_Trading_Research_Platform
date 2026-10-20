from __future__ import annotations

import json
from pathlib import Path

from recorder.market_data_defensive_mode import (
    build_market_data_defensive_mode_from_paths,
    build_market_data_defensive_mode_report,
    market_data_defensive_mode_markdown,
    write_market_data_defensive_mode_json,
    write_market_data_defensive_mode_markdown,
)


def _gate(**overrides: object) -> dict:
    gate = {
        "market_data_risk_gate_version": "market-data-risk-gate-v1",
        "gate_status": "watch",
        "action": "allow_with_market_data_haircut",
        "new_entries_allowed": True,
        "alpha_entries_allowed": True,
        "hedge_entries_allowed": True,
        "entry_size_multiplier": 0.5,
        "defensive_entry_size_multiplier": 1.0,
        "reason": "market_temperature_watch_or_hot",
        "blocking_reasons": [],
        "defensive_reasons": [],
        "haircut_reasons": ["market_temperature_watch_or_hot"],
        "market_data": {
            "status": "yellow",
            "temperature_label": "hot",
            "temperature_score": 20.0,
            "risk_flags": ["market_temperature_elevated"],
            "counts": {
                "live_count": 26,
                "delayed_count": 0,
                "missing_price_count": 0,
                "warming_up_count": 0,
                "error_count": 0,
            },
            "instruments": {
                "warming_up": [],
                "delayed": [],
                "missing": [],
                "errors": [],
                "severe_wide_spread": [],
                "alerted": ["VIX"],
            },
            "top_moves": [],
        },
    }
    gate.update(overrides)
    return gate


def test_defensive_mode_reduces_size_in_watch_market(tmp_path: Path) -> None:
    report = build_market_data_defensive_mode_report(
        _gate(),
        state_path=tmp_path / "state.json",
        now="2026-07-01T00:00:00+00:00",
    )

    assert report["market_data_defensive_mode_version"] == "market-data-defensive-operating-mode-v1"
    assert report["mode"] == "reduced_size_defined_risk"
    assert report["severity"] == "yellow"
    assert report["controls"]["ordinary_alpha_entries_allowed"] is True
    assert report["controls"]["max_new_entry_size_multiplier"] == 0.5
    assert report["controls"]["protective_option_analysis_allowed"] is True
    assert report["scorer_overlay"]["raise_min_confidence"] == 0.05
    assert "defined_risk_debit_spread_preferred" in report["controls"]["allowed_option_structures"]


def test_defensive_mode_reduces_size_for_session_context_haircut(tmp_path: Path) -> None:
    report = build_market_data_defensive_mode_report(
        _gate(
            action="allow_with_session_context_haircut",
            entry_size_multiplier=0.6,
            reason="market_temperature_stress_regular_dependent_after_hours_context",
            haircut_reasons=["market_temperature_stress_regular_dependent_after_hours_context"],
            market_data_session_context={
                "session_priority_class": "regular_dependent_after_hours_context",
                "session_name": "overnight",
                "is_regular_session": False,
                "monitorable_alert_instruments": [],
                "regular_dependent_alert_instruments": ["VIX", "VVIX"],
            },
            session_adjusted_priority=True,
        ),
        state_path=tmp_path / "state.json",
        now="2026-07-01T00:00:00+00:00",
    )

    assert report["mode"] == "reduced_size_defined_risk"
    assert report["severity"] == "yellow"
    assert report["controls"]["ordinary_alpha_entries_allowed"] is True
    assert report["controls"]["max_new_entry_size_multiplier"] == 0.6
    assert report["market_data_session_context"]["session_priority_class"] == "regular_dependent_after_hours_context"
    assert report["session_adjusted_priority"] is True
    assert report["scorer_overlay"]["session_adjusted_priority"] is True
    assert "regular_dependent_after_hours_context" in market_data_defensive_mode_markdown(report)


def test_defensive_mode_blocks_ticketing_when_data_quality_blocks(tmp_path: Path) -> None:
    report = build_market_data_defensive_mode_report(
        _gate(
            gate_status="block",
            action="block_new_entries",
            new_entries_allowed=False,
            alpha_entries_allowed=False,
            hedge_entries_allowed=False,
            entry_size_multiplier=0.0,
            reason="market_data_subscriptions_warming_up",
            blocking_reasons=["market_data_subscriptions_warming_up"],
            defensive_reasons=["market_temperature_stress"],
            market_data={
                "status": "red",
                "temperature_label": "stress",
                "temperature_score": 31.0,
                "risk_flags": ["market_data_subscriptions_warming_up", "market_temperature_elevated"],
                "counts": {"live_count": 23, "delayed_count": 0, "missing_price_count": 0, "warming_up_count": 3, "error_count": 0},
                "instruments": {"warming_up": ["USDCNH", "EURUSD"], "delayed": [], "missing": [], "errors": [], "severe_wide_spread": ["NIKKEI"], "alerted": ["VIX"]},
                "top_moves": [],
            },
        ),
        state_path=tmp_path / "state.json",
        now="2026-07-01T00:00:00+00:00",
    )

    assert report["mode"] == "risk_reduction_only"
    assert report["severity"] == "red"
    assert report["controls"]["ordinary_alpha_entries_allowed"] is False
    assert report["controls"]["hedge_or_defensive_entries_allowed"] is False
    assert report["controls"]["protective_option_analysis_allowed"] is True
    assert report["controls"]["protective_option_ticketing_allowed"] is False
    assert report["controls"]["data_quality_block"] is True
    assert "protective_put_analysis_only" in report["controls"]["allowed_option_structures"]


def test_defensive_mode_tracks_persistent_red_duration(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    payload = _gate(
        gate_status="defensive",
        action="defensive_or_hedge_only",
        defensive_reasons=["market_temperature_stress"],
        market_data={
            "status": "red",
            "temperature_label": "stress",
            "temperature_score": 35.0,
            "risk_flags": ["market_temperature_elevated"],
            "counts": {"live_count": 26, "delayed_count": 0, "missing_price_count": 0, "warming_up_count": 0, "error_count": 0},
            "instruments": {"warming_up": [], "delayed": [], "missing": [], "errors": [], "severe_wide_spread": [], "alerted": ["VIX"]},
            "top_moves": [],
        },
    )
    first = build_market_data_defensive_mode_report(
        payload,
        state_path=state,
        persistent_warning_seconds=300,
        persistent_critical_seconds=900,
        now="2026-07-01T00:00:00+00:00",
    )
    second = build_market_data_defensive_mode_report(
        payload,
        state_path=state,
        persistent_warning_seconds=300,
        persistent_critical_seconds=900,
        now="2026-07-01T00:06:00+00:00",
    )

    assert first["mode"] == "hedge_defensive_only"
    assert second["mode"] == "hedge_defensive_only_persistent_warning"
    assert second["duration"]["seen_count"] == 2
    assert second["duration"]["duration_seconds"] == 360.0


def test_defensive_mode_from_paths_and_writers(tmp_path: Path) -> None:
    gate_path = tmp_path / "gate.json"
    gate_path.write_text(json.dumps(_gate()), encoding="utf-8")
    report = build_market_data_defensive_mode_from_paths(
        market_data_risk_gate_path=gate_path,
        state_path=tmp_path / "state.json",
        now="2026-07-01T00:00:00+00:00",
    )
    markdown = market_data_defensive_mode_markdown(report)
    assert "Market Data Defensive Operating Mode" in markdown
    assert "Protective option analysis allowed" in markdown
    json_path = write_market_data_defensive_mode_json(tmp_path / "mode.json", report)
    md_path = write_market_data_defensive_mode_markdown(tmp_path / "mode.md", report)
    assert json_path.exists()
    assert md_path.exists()
