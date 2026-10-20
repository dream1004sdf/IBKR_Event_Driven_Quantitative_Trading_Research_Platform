from __future__ import annotations

from pathlib import Path

from recorder.dashboard_operations import (
    build_dashboard_recovery_post_validation,
    build_dashboard_recovery_post_validation_from_paths,
    dashboard_recovery_post_validation_markdown,
    write_dashboard_recovery_post_validation_json,
    write_dashboard_recovery_post_validation_markdown,
)


def test_dashboard_recovery_post_validation_detects_improvement(tmp_path: Path) -> None:
    before_snapshot = {"dashboard_snapshot_version": "trading-dashboard-snapshot-v1", "status": {"color": "red", "reasons": ["a", "b", "c"]}}
    after_snapshot = {"dashboard_snapshot_version": "trading-dashboard-snapshot-v1", "status": {"color": "yellow", "reasons": ["a"]}}
    before_queue = {
        "dashboard_action_queue_version": "dashboard-action-queue-v1",
        "status_color": "red",
        "total_action_count": 7,
        "visible_action_count": 7,
        "highest_severity": "red",
        "highest_priority_score": 3100,
        "actions": [{"action_id": "a"}],
    }
    after_queue = {
        "dashboard_action_queue_version": "dashboard-action-queue-v1",
        "status_color": "yellow",
        "total_action_count": 2,
        "visible_action_count": 2,
        "highest_severity": "yellow",
        "highest_priority_score": 2100,
        "actions": [{"action_id": "b"}],
    }
    before_plan = {"dashboard_recovery_plan_version": "dashboard-recovery-plan-v1", "planned_step_count": 5, "highest_severity": "red"}
    after_plan = {"dashboard_recovery_plan_version": "dashboard-recovery-plan-v1", "planned_step_count": 2, "highest_severity": "yellow"}
    executor = {"dashboard_recovery_executor_version": "dashboard-recovery-executor-v1", "execute_requested": True, "executed_command_count": 2, "failure_count": 0}

    report = build_dashboard_recovery_post_validation(
        before_snapshot=before_snapshot,
        after_snapshot=after_snapshot,
        before_action_queue=before_queue,
        after_action_queue=after_queue,
        before_recovery_plan=before_plan,
        after_recovery_plan=after_plan,
        recovery_executor=executor,
        now="2026-06-30T10:00:00+00:00",
    )

    assert report["dashboard_recovery_post_validation_version"] == "dashboard-recovery-post-validation-v1"
    assert report["outcome"] == "improved"
    assert report["summary"]["improved_metric_count"] > 0
    assert report["summary"]["worsened_metric_count"] == 0
    assert report["safety"]["read_only_validation"] is True
    assert report["safety"]["subprocess_executed"] is False

    markdown = dashboard_recovery_post_validation_markdown(report)
    assert "Dashboard Recovery Post-Validation" in markdown
    assert "Outcome" in markdown

    json_path = write_dashboard_recovery_post_validation_json(tmp_path / "post.json", report)
    md_path = write_dashboard_recovery_post_validation_markdown(tmp_path / "post.md", report)
    assert json_path.exists()
    assert md_path.exists()


def test_dashboard_recovery_post_validation_from_paths(tmp_path: Path) -> None:
    before_snapshot = tmp_path / "before_snapshot.json"
    after_snapshot = tmp_path / "after_snapshot.json"
    before_snapshot.write_text('{"dashboard_snapshot_version":"trading-dashboard-snapshot-v1","status":{"color":"yellow","reasons":["a"]}}', encoding="utf-8")
    after_snapshot.write_text('{"dashboard_snapshot_version":"trading-dashboard-snapshot-v1","status":{"color":"yellow","reasons":["a"]}}', encoding="utf-8")

    report = build_dashboard_recovery_post_validation_from_paths(
        before_snapshot_path=before_snapshot,
        after_snapshot_path=after_snapshot,
        now="2026-06-30T10:00:00+00:00",
    )

    assert report["outcome"] == "unchanged"
    assert report["summary"]["unchanged_metric_count"] >= 1
