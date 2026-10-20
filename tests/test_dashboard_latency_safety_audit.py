from __future__ import annotations

import json
from pathlib import Path

from recorder.dashboard_operations import (
    build_dashboard_latency_safety_audit,
    build_dashboard_latency_safety_audit_from_paths,
    dashboard_latency_safety_audit_markdown,
    write_dashboard_latency_safety_audit_json,
    write_dashboard_latency_safety_audit_markdown,
)


def _write(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def _profile() -> dict:
    return {
        "dashboard_latency_budget": {
            "enabled": True,
            "stages": {
                "evidence_refresh_watchdog": {
                    "enabled": True,
                    "mode": "skip_when_recent_artifact",
                    "max_artifact_age_seconds": 30.0,
                },
                "source_ingest_health": {
                    "enabled": True,
                    "mode": "skip_when_recent_artifact",
                    "max_artifact_age_seconds": 10.0,
                },
            },
        },
        "market_data_risk_gate": {"enabled": True},
        "market_data_session_transition_closed_loop": {"enabled": True},
        "session_transition_release_governor": {"enabled": True},
        "release_governed_recovery_closed_loop": {"enabled": True},
    }


def _summary() -> dict:
    return {
        "tick_summaries": [
            {"elapsed_seconds": 39.8},
            {"elapsed_seconds": 0.245},
        ],
        "pre_snapshot_refresh_summaries": [
            {
                "evidence_refresh_watchdog": {
                    "status": "skipped_latency_budget_recent_artifact",
                    "source_ingest_health": {"status": "skipped_latency_budget_recent_artifact"},
                },
                "market_data_risk_gate": {"gate_status": "watch"},
                "market_data_session_transition_closed_loop": {"status": "rebuild_completed"},
                "session_transition_release_governor": {"release_status": "blocked"},
                "release_governed_recovery_closed_loop": {"status": "pass"},
            }
        ],
    }


def test_dashboard_latency_safety_audit_reports_policy_and_safety(tmp_path: Path) -> None:
    report = build_dashboard_latency_safety_audit(profile_payload=_profile(), loop_summary_payload=_summary())

    assert report["dashboard_latency_safety_audit_version"] == "dashboard-latency-safety-audit-v1"
    assert report["status"] == "pass"
    assert report["latency_summary"]["last_tick_elapsed_seconds"] == 0.245
    stages = {item["stage_key"]: item for item in report["stage_audits"]}
    assert stages["evidence_refresh_watchdog"]["latency_optimized"] is True
    assert stages["session_transition_release_governor"]["cheap_safety_gate_kept_inline"] is True
    assert report["safety_invariants"]["broker_order_submission_allowed"] is False

    markdown = dashboard_latency_safety_audit_markdown(report)
    assert "Dashboard Latency Safety Audit" in markdown
    json_path = write_dashboard_latency_safety_audit_json(tmp_path / "audit.json", report)
    md_path = write_dashboard_latency_safety_audit_markdown(tmp_path / "audit.md", report)
    assert json_path.exists()
    assert md_path.exists()


def test_dashboard_latency_safety_audit_from_paths(tmp_path: Path) -> None:
    profile_path = _write(tmp_path / "profile.json", _profile())
    summary_path = _write(tmp_path / "summary.json", _summary())

    report = build_dashboard_latency_safety_audit_from_paths(
        profile_json_path=profile_path,
        loop_summary_json_path=summary_path,
    )

    assert report["status"] == "pass"
    assert report["latency_summary"]["observed_tick_count"] == 2
