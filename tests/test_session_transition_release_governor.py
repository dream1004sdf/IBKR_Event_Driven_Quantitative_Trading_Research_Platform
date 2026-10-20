from __future__ import annotations

import json
from pathlib import Path

from recorder.market_transition_operations import (
    build_session_transition_release_governor_from_paths,
    build_session_transition_release_governor_report,
    session_transition_release_governor_markdown,
    write_session_transition_release_governor_json,
    write_session_transition_release_governor_markdown,
)
from recorder.trading_dashboard import build_trading_dashboard_snapshot
from main import _run_dashboard_session_transition_release_governor


def _transition(decision: str = "release_candidate") -> dict:
    return {
        "market_data_session_transition_gate_version": "market-data-session-transition-gate-v1",
        "generated_at": "2026-07-01T14:35:00+00:00",
        "decision": decision,
        "severity": "green",
        "transition": {"previous_session": "pre_market", "current_session": "regular", "transition_type": "off_hours_to_regular"},
        "controls": {
            "rebuild_required_before_auto_release": False,
            "ordinary_alpha_entries_allowed_after_transition": decision == "release_candidate",
            "max_new_entry_size_multiplier": 0.7,
        },
    }


def _risk_gate() -> dict:
    return {
        "market_data_risk_gate_version": "market-data-risk-gate-v1",
        "generated_at": "2026-07-01T14:35:01+00:00",
        "gate_status": "watch",
        "action": "allow_with_market_data_haircut",
        "allowed": True,
        "new_entries_allowed": True,
        "alpha_entries_allowed": True,
        "entry_size_multiplier": 0.6,
    }


def _operating_gate() -> dict:
    return {
        "autonomous_execution_gate_version": "market-data-operating-auto-gate-v1",
        "generated_at": "2026-07-01T14:35:02+00:00",
        "auto_ready_count": 3,
        "market_data_operating_mode": {
            "mode": "normal",
            "ordinary_alpha_entries_allowed": True,
            "max_new_entry_size_multiplier": 0.8,
        },
    }


def _portfolio() -> dict:
    return {
        "paper_portfolio_risk_state_version": "paper-portfolio-risk-state-v1",
        "generated_at": "2026-07-01T14:35:03+00:00",
        "risk_state": "within_limits",
        "new_entries_allowed": True,
        "breach_count": 0,
    }


def _evidence(allowed: bool = True) -> dict:
    return {
        "evidence_preflight_version": "evidence-preflight-v1",
        "generated_at": "2026-07-01T14:35:04+00:00",
        "gate_status": "pass" if allowed else "block",
        "allowed": allowed,
        "action": "allow_normal" if allowed else "block_new_paper_entries",
        "entry_size_multiplier": 1.0 if allowed else 0.0,
        "reason": "" if allowed else "evidence_stale_sources",
    }


def test_release_governor_blocks_when_transition_not_candidate() -> None:
    report = build_session_transition_release_governor_report(
        transition_gate_payload=_transition("hold_defensive"),
        market_data_risk_gate_payload=_risk_gate(),
        market_data_operating_auto_gate_payload=_operating_gate(),
        portfolio_risk_state_payload=_portfolio(),
        evidence_preflight_payload=_evidence(),
        now="2026-07-01T14:36:00+00:00",
    )

    assert report["release_status"] == "blocked"
    assert report["ordinary_alpha_release_allowed"] is False
    assert "transition_gate_not_release_candidate" in report["reason"]
    assert report["safety"]["ibkr_order_submitted"] is False


def test_release_governor_allows_with_minimum_haircut() -> None:
    report = build_session_transition_release_governor_report(
        transition_gate_payload=_transition(),
        market_data_risk_gate_payload=_risk_gate(),
        market_data_operating_auto_gate_payload=_operating_gate(),
        portfolio_risk_state_payload=_portfolio(),
        evidence_preflight_payload=_evidence(),
        now="2026-07-01T14:36:00+00:00",
    )

    assert report["release_status"] == "release_allowed_with_haircut"
    assert report["ordinary_alpha_release_allowed"] is True
    assert report["max_new_entry_size_multiplier"] == 0.6
    assert "Session Transition Release Governor" in session_transition_release_governor_markdown(report)


def test_release_governor_allows_transition_maintain_haircut() -> None:
    transition = _transition("maintain_haircut")
    transition["controls"]["ordinary_alpha_entries_allowed_after_transition"] = True
    transition["controls"]["max_new_entry_size_multiplier"] = 0.5
    report = build_session_transition_release_governor_report(
        transition_gate_payload=transition,
        market_data_risk_gate_payload=_risk_gate(),
        market_data_operating_auto_gate_payload=_operating_gate(),
        portfolio_risk_state_payload=_portfolio(),
        evidence_preflight_payload=_evidence(),
        now="2026-07-01T14:36:00+00:00",
    )

    assert report["release_status"] == "release_allowed_with_haircut"
    assert report["ordinary_alpha_release_allowed"] is True
    assert report["max_new_entry_size_multiplier"] == 0.5
    assert report["checks"][0]["passed"] is True


def test_release_governor_evidence_preflight_can_block() -> None:
    report = build_session_transition_release_governor_report(
        transition_gate_payload=_transition(),
        market_data_risk_gate_payload=_risk_gate(),
        market_data_operating_auto_gate_payload=_operating_gate(),
        portfolio_risk_state_payload=_portfolio(),
        evidence_preflight_payload=_evidence(False),
    )

    assert report["release_status"] == "blocked"
    assert report["ordinary_alpha_release_allowed"] is False
    assert "evidence_preflight_blocks_entries" in report["reason"]


def test_release_governor_paths_and_writers(tmp_path: Path) -> None:
    transition_path = tmp_path / "transition.json"
    risk_path = tmp_path / "risk.json"
    operating_path = tmp_path / "operating.json"
    portfolio_path = tmp_path / "portfolio.json"
    evidence_path = tmp_path / "evidence.json"
    transition_path.write_text(json.dumps(_transition()), encoding="utf-8")
    risk_path.write_text(json.dumps(_risk_gate()), encoding="utf-8")
    operating_path.write_text(json.dumps(_operating_gate()), encoding="utf-8")
    portfolio_path.write_text(json.dumps(_portfolio()), encoding="utf-8")
    evidence_path.write_text(json.dumps(_evidence()), encoding="utf-8")

    report = build_session_transition_release_governor_from_paths(
        transition_gate_path=transition_path,
        market_data_risk_gate_path=risk_path,
        market_data_operating_auto_gate_path=operating_path,
        portfolio_risk_state_path=portfolio_path,
        evidence_preflight_path=evidence_path,
    )
    json_path = write_session_transition_release_governor_json(tmp_path / "governor.json", report)
    md_path = write_session_transition_release_governor_markdown(tmp_path / "governor.md", report)

    assert json_path.exists()
    assert md_path.exists()


def test_dashboard_snapshot_surfaces_release_governor(tmp_path: Path) -> None:
    transition_path = tmp_path / "transition.json"
    governor_path = tmp_path / "governor.json"
    risk_path = tmp_path / "risk.json"
    operating_path = tmp_path / "operating.json"
    portfolio_path = tmp_path / "portfolio.json"
    evidence_path = tmp_path / "evidence.json"
    db_path = tmp_path / "missing.db"
    transition_path.write_text(json.dumps(_transition()), encoding="utf-8")
    risk_path.write_text(json.dumps(_risk_gate()), encoding="utf-8")
    operating_path.write_text(json.dumps(_operating_gate()), encoding="utf-8")
    portfolio_path.write_text(json.dumps(_portfolio()), encoding="utf-8")
    evidence_path.write_text(json.dumps(_evidence()), encoding="utf-8")
    governor = build_session_transition_release_governor_from_paths(
        transition_gate_path=transition_path,
        market_data_risk_gate_path=risk_path,
        market_data_operating_auto_gate_path=operating_path,
        portfolio_risk_state_path=portfolio_path,
        evidence_preflight_path=evidence_path,
    )
    governor_path.write_text(json.dumps(governor), encoding="utf-8")

    snapshot = build_trading_dashboard_snapshot(
        account_snapshot_path=None,
        paper_portfolio_state_path=None,
        portfolio_risk_state_path=portfolio_path,
        execution_intent_ledger_path=None,
        mark_quality_daemon_path=None,
        economic_desk_path=None,
        market_data_path=None,
        market_data_risk_gate_path=risk_path,
        market_data_operating_auto_gate_path=operating_path,
        market_data_session_transition_gate_path=transition_path,
        session_transition_release_governor_path=governor_path,
        database_path=db_path,
    )

    release = snapshot["market_data"]["session_transition_release_governor"]
    assert release["release_status"] == "release_allowed_with_haircut"
    assert snapshot["status"]["session_transition_release_allowed"] is True


def test_dashboard_release_governor_prefers_closed_loop_artifacts(tmp_path: Path) -> None:
    stale_transition_path = tmp_path / "stale_transition.json"
    fresh_transition_path = tmp_path / "fresh_transition.json"
    stale_risk_path = tmp_path / "stale_risk.json"
    fresh_risk_path = tmp_path / "fresh_risk.json"
    stale_operating_path = tmp_path / "stale_operating.json"
    fresh_operating_path = tmp_path / "fresh_operating.json"
    portfolio_path = tmp_path / "portfolio.json"
    evidence_path = tmp_path / "evidence.json"
    output_path = tmp_path / "release.md"
    json_output_path = tmp_path / "release.json"

    stale_transition_path.write_text(json.dumps(_transition("hold_defensive")), encoding="utf-8")
    fresh_transition_path.write_text(json.dumps(_transition("release_candidate")), encoding="utf-8")
    stale_risk_path.write_text(json.dumps({**_risk_gate(), "allowed": False, "alpha_entries_allowed": False, "entry_size_multiplier": 0.0}), encoding="utf-8")
    fresh_risk_path.write_text(json.dumps(_risk_gate()), encoding="utf-8")
    stale_operating_path.write_text(
        json.dumps(
            {
                **_operating_gate(),
                "auto_ready_count": 0,
                "market_data_operating_mode": {
                    "mode": "risk_reduction_only",
                    "ordinary_alpha_entries_allowed": False,
                    "max_new_entry_size_multiplier": 0.0,
                },
            }
        ),
        encoding="utf-8",
    )
    fresh_operating_path.write_text(json.dumps(_operating_gate()), encoding="utf-8")
    portfolio_path.write_text(json.dumps(_portfolio()), encoding="utf-8")
    evidence_path.write_text(json.dumps(_evidence()), encoding="utf-8")

    profile = {
        "session_transition_release_governor": {
            "enabled": True,
            "prefer_closed_loop_artifacts": True,
            "transition_gate_json": str(stale_transition_path),
            "market_data_risk_gate_json": str(stale_risk_path),
            "market_data_operating_auto_gate_json": str(stale_operating_path),
            "portfolio_risk_state_json": str(portfolio_path),
            "evidence_preflight_json": str(evidence_path),
            "output": str(output_path),
            "json_output_path": str(json_output_path),
        }
    }
    closed_loop_summary = {
        "artifacts": {
            "market_data_session_transition_gate_final_json": str(fresh_transition_path),
            "market_data_risk_gate_json": str(fresh_risk_path),
            "market_data_operating_auto_gate_json": str(fresh_operating_path),
        }
    }

    summary = _run_dashboard_session_transition_release_governor(
        profile=profile,
        profile_base=None,
        run_id="test_release_hook",
        closed_loop_summary=closed_loop_summary,
    )

    assert summary["enabled"] is True
    assert summary["applied"] is True
    assert summary["hook"] == "session_transition_release_governor"
    assert summary["release_status"] == "release_allowed_with_haircut"
    assert summary["ordinary_alpha_release_allowed"] is True
    assert summary["transition_gate_json"] == str(fresh_transition_path.resolve())
    assert summary["market_data_risk_gate_json"] == str(fresh_risk_path.resolve())
    assert summary["market_data_operating_auto_gate_json"] == str(fresh_operating_path.resolve())
    assert json_output_path.exists()
