from __future__ import annotations

import json
from pathlib import Path

from recorder.dashboard_operations import (
    dashboard_recovery_closed_loop_markdown,
    run_dashboard_recovery_closed_loop,
    write_dashboard_recovery_closed_loop_json,
    write_dashboard_recovery_closed_loop_markdown,
)


def _write(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def _snapshot(color: str = "yellow", reasons: list[str] | None = None) -> dict:
    return {
        "dashboard_snapshot_version": "trading-dashboard-snapshot-v1",
        "generated_at": "2026-06-30T10:00:00+00:00",
        "status": {"color": color, "reasons": reasons or ["source_ingest_health_online_gap"]},
    }


def _queue(severity: str = "yellow", total: int = 2, priority: int = 2110) -> dict:
    return {
        "dashboard_action_queue_version": "dashboard-action-queue-v1",
        "generated_at": "2026-06-30T10:00:01+00:00",
        "status_color": severity,
        "status_reasons": ["source_ingest_health_online_gap"],
        "highest_severity": severity,
        "highest_priority_score": priority,
        "visible_action_count": total,
        "total_action_count": total,
        "actions": [
            {
                "action_id": "repair_source_ingest_health_online_required_gap",
                "priority_score": priority,
                "severity": severity,
                "domain": "source_ingest_health",
                "source_key": "market_news",
                "auto_recovery_target": "source_online_supervisor",
                "read_only": True,
            }
        ],
    }


def _plan(severity: str = "yellow", steps: int = 1) -> dict:
    return {
        "dashboard_recovery_plan_version": "dashboard-recovery-plan-v1",
        "generated_at": "2026-06-30T10:00:02+00:00",
        "dry_run": True,
        "planned_step_count": steps,
        "highest_severity": severity,
        "highest_priority_step_id": "m177_step_001_repair_source_ingest_health_online_required_gap",
        "steps": [
            {
                "step_id": "m177_step_001_repair_source_ingest_health_online_required_gap",
                "source_action_id": "repair_source_ingest_health_online_required_gap",
                "phase": "source_recovery",
                "severity": severity,
                "status": "planned_dry_run",
                "command_preview": [
                    "python main.py source-online-supervisor --profile-json config/source_online_supervisor_profile_v1.json --max-ticks 1 --interval-seconds 0"
                ],
            }
        ],
    }


def test_dashboard_recovery_closed_loop_executes_refresh_and_validates(tmp_path: Path) -> None:
    snapshot_path = _write(tmp_path / "snapshot.json", _snapshot("red", ["source_ingest_health_online_gap", "market_data_risk_gate_defensive_only"]))
    queue_path = _write(tmp_path / "queue.json", _queue("red", total=3, priority=3330))
    plan_path = _write(tmp_path / "plan.json", _plan("red"))
    profile_path = _write(tmp_path / "profile.json", {"profile": "test"})
    state_path = tmp_path / "state.json"
    executor_path = tmp_path / "executor.json"

    calls: list[list[str]] = []

    def runner(command: list[str], cwd: Path, timeout: float) -> dict:
        calls.append(command)
        if "trading-dashboard-loop" in command:
            _write(snapshot_path, _snapshot("yellow", ["source_ingest_health_online_gap"]))
            _write(queue_path, _queue("yellow", total=1, priority=2110))
            _write(plan_path, _plan("yellow"))
        return {
            "status": "succeeded",
            "returncode": 0,
            "stdout_tail": "ok",
            "stderr_tail": "",
            "duration_seconds": 0.01,
            "error": None,
        }

    report = run_dashboard_recovery_closed_loop(
        snapshot_path=snapshot_path,
        action_queue_path=queue_path,
        recovery_plan_path=plan_path,
        profile_json_path=profile_path,
        state_path=state_path,
        output_executor_path=executor_path,
        execute=True,
        refresh_after=True,
        max_steps=1,
        cooldown_seconds=60,
        runner=runner,
        cwd=tmp_path,
        now="2026-06-30T10:05:00+00:00",
    )

    assert report["dashboard_recovery_closed_loop_version"] == "dashboard-recovery-closed-loop-v1"
    assert report["status"] == "improved"
    assert report["executor"]["final_status"] == "executed"
    assert report["executor"]["executed_command_count"] == 1
    assert report["refresh"]["status"] == "succeeded"
    assert report["post_validation"]["outcome"] in {"improved", "mixed_improved"}
    assert state_path.exists()
    assert executor_path.exists()
    assert any("trading-dashboard-loop" in call for call in calls)

    markdown = dashboard_recovery_closed_loop_markdown(report)
    assert "Dashboard Recovery Closed Loop" in markdown
    json_path = write_dashboard_recovery_closed_loop_json(tmp_path / "closed_loop.json", report)
    md_path = write_dashboard_recovery_closed_loop_markdown(tmp_path / "closed_loop.md", report)
    assert json_path.exists()
    assert md_path.exists()


def test_dashboard_recovery_closed_loop_skips_same_signature_during_cooldown(tmp_path: Path) -> None:
    snapshot_path = _write(tmp_path / "snapshot.json", _snapshot())
    queue_path = _write(tmp_path / "queue.json", _queue())
    plan_path = _write(tmp_path / "plan.json", _plan())
    profile_path = _write(tmp_path / "profile.json", {"profile": "test"})
    state_path = tmp_path / "state.json"

    def runner(command: list[str], cwd: Path, timeout: float) -> dict:
        return {
            "status": "succeeded",
            "returncode": 0,
            "stdout_tail": "ok",
            "stderr_tail": "",
            "duration_seconds": 0.01,
            "error": None,
        }

    first = run_dashboard_recovery_closed_loop(
        snapshot_path=snapshot_path,
        action_queue_path=queue_path,
        recovery_plan_path=plan_path,
        profile_json_path=profile_path,
        state_path=state_path,
        execute=False,
        refresh_after=False,
        runner=runner,
        cwd=tmp_path,
        now="2026-06-30T10:00:00+00:00",
    )
    second = run_dashboard_recovery_closed_loop(
        snapshot_path=snapshot_path,
        action_queue_path=queue_path,
        recovery_plan_path=plan_path,
        profile_json_path=profile_path,
        state_path=state_path,
        execute=True,
        refresh_after=False,
        runner=runner,
        cwd=tmp_path,
        now="2026-06-30T10:01:00+00:00",
    )

    assert first["cooldown_decision"]["allowed"] is True
    assert second["status"] == "skipped_cooldown"
    assert second["cooldown_decision"]["allowed"] is False
    assert second["executor"]["final_status"] == "skipped_cooldown"
    assert second["executor"]["executed_command_count"] == 0
