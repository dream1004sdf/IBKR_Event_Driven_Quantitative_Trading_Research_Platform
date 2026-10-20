from __future__ import annotations

import json
import os
import time
from pathlib import Path

from recorder.dashboard_operations import (
    build_dashboard_supervisor_handoff_audit,
    build_dashboard_supervisor_handoff_audit_from_paths,
    dashboard_supervisor_handoff_audit_markdown,
    write_dashboard_supervisor_handoff_audit_json,
    write_dashboard_supervisor_handoff_audit_markdown,
)


def _write(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    now = time.time()
    os.utime(path, (now, now))
    return path


def _profile(base: Path) -> dict:
    return {
        "dashboard_source_supervisor_handoff": {
            "enabled": True,
            "source_online_supervisor_json": str(base / "supervisor.json"),
            "evidence_watchdog_json": str(base / "watchdog.json"),
            "source_ingest_health_json": str(base / "health.json"),
            "max_supervisor_age_seconds": 120.0,
            "max_source_health_age_seconds": 120.0,
            "max_evidence_watchdog_age_seconds": 120.0,
        },
        "evidence_refresh_watchdog": {"enabled": False, "ignore": True},
        "source_ingest_health": {"enabled": False, "ignore": True},
        "market_data_risk_gate": {"enabled": True},
        "market_data_session_transition_closed_loop": {"enabled": True},
        "session_transition_release_governor": {"enabled": True},
        "release_governed_recovery_closed_loop": {"enabled": True},
    }


def _summary() -> dict:
    return {
        "pre_snapshot_refresh_summaries": [
            {
                "market_data_risk_gate": {"gate_status": "watch"},
                "market_data_session_transition_closed_loop": {"status": "rebuild_completed"},
                "session_transition_release_governor": {"release_status": "blocked"},
                "release_governed_recovery_closed_loop": {"status": "pass"},
            }
        ]
    }


def test_dashboard_supervisor_handoff_audit_passes_split_role(tmp_path: Path) -> None:
    _write(tmp_path / "supervisor.json", {"source_online_supervisor_version": "source-online-supervisor-v8", "final_status": "green", "final_source_ingest_health_status": "green"})
    _write(tmp_path / "watchdog.json", {"evidence_refresh_watchdog_version": "evidence-refresh-watchdog-v1", "final_status": "healthy_no_refresh_needed", "final_freshness_status": "green"})
    _write(tmp_path / "health.json", {"source_ingest_health_version": "source-ingest-health-v1", "overall_status": "green"})

    report = build_dashboard_supervisor_handoff_audit(profile_payload=_profile(tmp_path), loop_summary_payload=_summary())

    assert report["dashboard_supervisor_handoff_audit_version"] == "dashboard-supervisor-handoff-audit-v1"
    assert report["status"] == "pass"
    assert report["inline_heavy_source_refresh_disabled"] is True
    assert report["cheap_final_safety_gates_inline"] is True
    assert report["heavy_inline_seen_in_loop_summary"] is False
    assert report["release_governed_pass_seen_in_loop_summary"] is True

    markdown = dashboard_supervisor_handoff_audit_markdown(report)
    assert "Dashboard Supervisor Handoff Audit" in markdown
    assert write_dashboard_supervisor_handoff_audit_json(tmp_path / "audit.json", report).exists()
    assert write_dashboard_supervisor_handoff_audit_markdown(tmp_path / "audit.md", report).exists()


def test_dashboard_supervisor_handoff_audit_from_paths_flags_stale_supervisor(tmp_path: Path) -> None:
    supervisor_path = _write(tmp_path / "supervisor.json", {"source_online_supervisor_version": "source-online-supervisor-v8", "final_status": "green"})
    old = time.time() - 3600
    os.utime(supervisor_path, (old, old))
    _write(tmp_path / "watchdog.json", {"evidence_refresh_watchdog_version": "evidence-refresh-watchdog-v1", "final_status": "healthy_no_refresh_needed"})
    _write(tmp_path / "health.json", {"source_ingest_health_version": "source-ingest-health-v1", "overall_status": "green"})
    profile_path = _write(tmp_path / "profile.json", _profile(tmp_path))
    summary_path = _write(tmp_path / "summary.json", _summary())

    report = build_dashboard_supervisor_handoff_audit_from_paths(
        profile_json_path=profile_path,
        loop_summary_json_path=summary_path,
    )

    assert report["status"] == "review"
    assert "source_online_supervisor_artifact_stale" in report["violations"]


def test_dashboard_supervisor_handoff_audit_allows_stale_watchdog_when_supervisor_green(tmp_path: Path) -> None:
    _write(tmp_path / "supervisor.json", {"source_online_supervisor_version": "source-online-supervisor-v8", "final_status": "green", "final_source_ingest_health_status": "green"})
    watchdog_path = _write(tmp_path / "watchdog.json", {"evidence_refresh_watchdog_version": "evidence-refresh-watchdog-v1", "final_status": "refresh_completed_gap_remains"})
    old = time.time() - 3600
    os.utime(watchdog_path, (old, old))
    _write(tmp_path / "health.json", {"source_ingest_health_version": "source-ingest-health-v1", "overall_status": "green"})
    profile = _profile(tmp_path)
    profile["dashboard_source_supervisor_handoff"]["allow_stale_evidence_watchdog_when_supervisor_green"] = True

    report = build_dashboard_supervisor_handoff_audit(profile_payload=profile, loop_summary_payload=_summary())

    assert report["status"] == "pass"
    assert report["watchdog_superseded_by_fresh_supervisor_green"] is True
    assert "evidence_watchdog_artifact_stale" not in report["violations"]
