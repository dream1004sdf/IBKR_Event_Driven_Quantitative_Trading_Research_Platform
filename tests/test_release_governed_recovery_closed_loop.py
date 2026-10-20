from __future__ import annotations

import json
from pathlib import Path

from recorder.market_transition_operations import (
    release_governed_recovery_closed_loop_markdown,
    run_release_governed_recovery_closed_loop,
    write_release_governed_recovery_closed_loop_json,
    write_release_governed_recovery_closed_loop_markdown,
)


def _write(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def _transition_gate(decision: str = "rebuild_required") -> dict:
    return {
        "market_data_session_transition_gate_version": "market-data-session-transition-gate-v1",
        "generated_at": "2026-07-01T13:30:00+00:00",
        "decision": decision,
        "severity": "red" if decision == "rebuild_required" else "green",
        "reason": "test_transition_gate",
        "transition": {"transition_type": "off_hours_to_regular", "previous_session": "off_hours", "current_session": "regular"},
        "controls": {
            "rebuild_required_before_auto_release": decision != "release_candidate",
            "ordinary_alpha_entries_allowed_after_transition": decision == "release_candidate",
            "max_new_entry_size_multiplier": 1.0 if decision == "release_candidate" else 0.0,
        },
    }


def _risk_gate() -> dict:
    return {
        "market_data_risk_gate_version": "market-data-risk-gate-v1",
        "generated_at": "2026-07-01T13:30:01+00:00",
        "gate_status": "pass",
        "allowed": True,
        "new_entries_allowed": True,
        "alpha_entries_allowed": True,
        "action": "allow_normal",
        "entry_size_multiplier": 1.0,
    }


def _operating_gate() -> dict:
    return {
        "autonomous_execution_gate_version": "market-data-operating-auto-gate-v1",
        "generated_at": "2026-07-01T13:30:02+00:00",
        "source_preview_item_count": 1,
        "auto_ready_count": 1,
        "auto_gate_status_counts": {"auto_execution_ready": 1},
        "auto_gate_block_reason_counts": {},
        "market_data_operating_mode": {
            "mode": "normal",
            "ordinary_alpha_entries_allowed": True,
            "max_new_entry_size_multiplier": 1.0,
        },
        "market_data_defensive_mode": {
            "mode": "normal",
            "controls": {
                "ordinary_alpha_entries_allowed": True,
                "hedge_or_defensive_entries_allowed": True,
                "exit_or_risk_reduction_allowed": True,
                "protective_option_ticketing_allowed": True,
                "max_new_entry_size_multiplier": 1.0,
            },
        },
        "auto_execution_items": [
            {
                "auto_execution_status": "auto_execution_ready",
                "symbol": "MSFT",
                "ticket_type": "stock",
                "action": "BUY",
                "quantity": 1.0,
                "evidence_preflight_action": "allow_normal",
                "evidence_entry_size_multiplier": 1.0,
                "market_data_risk_gate_action": "allow_normal",
                "market_data_entry_size_multiplier": 1.0,
                "local_margin_estimate": {"estimated_initial_margin_change": 10.0},
                "order_intent": {
                    "contract_preview": {"sec_type": "STK", "symbol": "MSFT", "exchange": "SMART", "currency": "USD"},
                    "order_preview": {"action": "BUY", "total_quantity": 1.0, "what_if": True, "transmit": False},
                },
                "source_preview_item": {"source_score_id": 101, "source_outcome_id": 202},
                "checks": [{"name": "test_ready", "passed": True}],
            }
        ],
    }


def _portfolio() -> dict:
    return {
        "paper_portfolio_risk_state_version": "paper-portfolio-risk-state-v1",
        "generated_at": "2026-07-01T13:30:03+00:00",
        "risk_state": "within_limits",
        "recommended_action": "allow_new_entries",
        "new_entries_allowed": True,
        "breach_count": 0,
        "warning_count": 0,
    }


def _evidence() -> dict:
    return {
        "evidence_preflight_version": "evidence-preflight-v1",
        "generated_at": "2026-07-01T13:30:04+00:00",
        "gate_status": "pass",
        "allowed": True,
        "action": "allow_normal",
        "entry_size_multiplier": 1.0,
    }


def test_release_governed_recovery_closed_loop_blocks_stale_pending_and_writes_artifacts(tmp_path: Path) -> None:
    transition_path = _write(tmp_path / "transition.json", _transition_gate("rebuild_required"))
    risk_path = _write(tmp_path / "risk.json", _risk_gate())
    operating_path = _write(tmp_path / "operating.json", _operating_gate())
    portfolio_path = _write(tmp_path / "portfolio.json", _portfolio())
    evidence_path = _write(tmp_path / "evidence.json", _evidence())
    snapshot_path = _write(
        tmp_path / "snapshot.json",
        {
            "dashboard_snapshot_version": "trading-dashboard-snapshot-v1",
            "buy_sell_plan": {"pending_paper_execution_count": 10},
        },
    )
    existing_ledger_path = _write(
        tmp_path / "old_ledger.json",
        {"execution_intent_ledger_version": "legacy", "pending_paper_execution_count": 10, "intents": []},
    )

    report = run_release_governed_recovery_closed_loop(
        transition_gate_path=transition_path,
        market_data_risk_gate_path=risk_path,
        market_data_operating_auto_gate_path=operating_path,
        portfolio_risk_state_path=portfolio_path,
        evidence_preflight_path=evidence_path,
        snapshot_path=snapshot_path,
        existing_ledger_path=existing_ledger_path,
        release_governor_json_output_path=tmp_path / "m206_release.json",
        release_governor_markdown_output_path=tmp_path / "m206_release.md",
        ledger_json_output_path=tmp_path / "m206_ledger.json",
        ledger_markdown_output_path=tmp_path / "m206_ledger.md",
        router_json_output_path=tmp_path / "m206_router.json",
        router_markdown_output_path=tmp_path / "m206_router.md",
        now="2026-07-01T13:31:00+00:00",
    )

    assert report["release_governed_recovery_closed_loop_version"] == "release-governed-recovery-closed-loop-v1"
    assert report["status"] == "pass"
    assert report["post_validation"]["before_dashboard_pending_paper_execution_count"] == 10
    assert report["post_validation"]["rebuilt_ledger_pending_paper_execution_count"] == 0
    assert report["post_validation"]["rebuilt_ledger_release_governor_block_count"] == 1
    assert report["post_validation"]["rebuilt_router_fill_count"] == 0
    assert report["post_validation"]["stale_pending_cleared"] is True
    assert report["post_validation"]["release_governed_path_active"] is True
    assert (tmp_path / "m206_release.json").exists()
    assert (tmp_path / "m206_ledger.json").exists()
    assert (tmp_path / "m206_router.json").exists()

    markdown = release_governed_recovery_closed_loop_markdown(report)
    assert "Release-Governed Recovery Closed Loop" in markdown
    json_path = write_release_governed_recovery_closed_loop_json(tmp_path / "closed_loop.json", report)
    md_path = write_release_governed_recovery_closed_loop_markdown(tmp_path / "closed_loop.md", report)
    assert json_path.exists()
    assert md_path.exists()


def test_release_governed_recovery_closed_loop_reuses_prebuilt_release_governor(tmp_path: Path) -> None:
    transition_path = _write(tmp_path / "transition.json", _transition_gate("release_candidate"))
    risk_path = _write(tmp_path / "risk.json", _risk_gate())
    operating_path = _write(tmp_path / "operating.json", _operating_gate())
    portfolio_path = _write(tmp_path / "portfolio.json", _portfolio())
    evidence_path = _write(tmp_path / "evidence.json", _evidence())
    prebuilt_release = {
        "session_transition_release_governor_version": "session-transition-release-governor-v1",
        "generated_at": "2026-07-01T13:30:05+00:00",
        "release_status": "blocked",
        "severity": "yellow",
        "ordinary_alpha_release_allowed": False,
        "max_new_entry_size_multiplier": 0.0,
        "reason": "prebuilt_test_block",
        "checks": [{"name": "prebuilt", "passed": False}],
        "controls": {"ordinary_alpha_release_allowed": False, "max_new_entry_size_multiplier": 0.0},
    }

    report = run_release_governed_recovery_closed_loop(
        transition_gate_path=transition_path,
        market_data_risk_gate_path=risk_path,
        market_data_operating_auto_gate_path=operating_path,
        portfolio_risk_state_path=portfolio_path,
        evidence_preflight_path=evidence_path,
        release_governor_payload=prebuilt_release,
        ledger_json_output_path=tmp_path / "ledger.json",
        router_json_output_path=tmp_path / "router.json",
        now="2026-07-01T13:31:00+00:00",
    )

    assert report["steps"][0]["status"] == "reused"
    assert report["release_governor"]["reason"] == "prebuilt_test_block"
    assert report["post_validation"]["rebuilt_ledger_release_governor_block_count"] == 1
    assert report["post_validation"]["rebuilt_router_fill_count"] == 0
