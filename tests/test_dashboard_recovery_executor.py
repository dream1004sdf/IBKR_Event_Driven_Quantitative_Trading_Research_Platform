from __future__ import annotations

from pathlib import Path

from recorder.dashboard_operations import (
    run_dashboard_recovery_executor,
    dashboard_recovery_executor_markdown,
    write_dashboard_recovery_executor_json,
    write_dashboard_recovery_executor_markdown,
)


def _plan() -> dict:
    return {
        "dashboard_recovery_plan_version": "dashboard-recovery-plan-v1",
        "generated_at": "2026-06-30T10:00:00+00:00",
        "steps": [
            {
                "step_id": "m177_step_001_source",
                "source_action_id": "repair_source_ingest_health_online_required_gap",
                "phase": "source_recovery",
                "severity": "yellow",
                "status": "planned_dry_run",
                "command_preview": [
                    "python main.py source-online-supervisor --profile-json config/source_online_supervisor_profile_v1.json --max-ticks 1 --interval-seconds 0",
                    "python main.py source-ingest-health --profile-json config/source_ingest_health_profile_v1.json",
                ],
            },
            {
                "step_id": "m177_step_002_market",
                "source_action_id": "review_market_data_temperature_non_green",
                "phase": "market_data_recovery",
                "severity": "yellow",
                "status": "planned_dry_run",
                "command_preview": [
                    "python main.py market-data-desk --seconds 25 --refresh-seconds 1 --market-data-type 1 --gap-threshold-pct 1.0 --tick-move-threshold-pct 0.5 --fallback-after-seconds 4.0 --output exports/m153_market_data_desk_refresh.md --json-output-path exports/m153_market_data_desk_refresh.json",
                    "python main.py market-data-risk-gate --profile-json config/market_data_risk_gate_profile_v1.json",
                ],
            },
            {
                "step_id": "m177_step_003_risk",
                "source_action_id": "risk_account_buying_power_below_threshold",
                "phase": "risk_review",
                "severity": "yellow",
                "status": "manual_review_required",
                "command_preview": [
                    "python main.py ibkr-account-snapshot --json-output-path exports/m110_ibkr_account_snapshot.json",
                ],
            },
            {
                "step_id": "m177_step_004_bad",
                "source_action_id": "bad_order_attempt",
                "phase": "source_recovery",
                "severity": "red",
                "status": "planned_dry_run",
                "command_preview": [
                    "python main.py submit-order --symbol SPY",
                ],
            },
        ],
    }


def test_dashboard_recovery_executor_dry_run_and_allowlist(tmp_path: Path) -> None:
    report = run_dashboard_recovery_executor(_plan(), execute=False, max_steps=4, cwd=tmp_path)

    assert report["dashboard_recovery_executor_version"] == "dashboard-recovery-executor-v1"
    assert report["execute_requested"] is False
    assert report["final_status"] == "dry_run"
    assert report["executed_command_count"] == 0
    assert report["safety"]["allowlist_enforced"] is True
    assert any(item["status"] == "skipped_dry_run" for item in report["command_results"])
    assert any(item["status"] == "skipped_phase_not_allowlisted" for item in report["command_results"])
    assert any(item["status"] == "skipped_denied_token" for item in report["command_results"])

    markdown = dashboard_recovery_executor_markdown(report)
    assert "Dashboard Recovery Executor" in markdown
    assert "allowlist-only" in markdown

    json_path = write_dashboard_recovery_executor_json(tmp_path / "executor.json", report)
    md_path = write_dashboard_recovery_executor_markdown(tmp_path / "executor.md", report)
    assert json_path.exists()
    assert md_path.exists()


def test_dashboard_recovery_executor_execute_uses_runner_for_allowed_commands(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def runner(command: list[str], cwd: Path, timeout_seconds: float) -> dict:
        calls.append(command)
        return {
            "status": "succeeded",
            "returncode": 0,
            "stdout_tail": "ok",
            "stderr_tail": "",
            "duration_seconds": 0.01,
            "error": None,
        }

    report = run_dashboard_recovery_executor(
        _plan(),
        execute=True,
        max_steps=2,
        cwd=tmp_path,
        runner=runner,
        timeout_seconds=5,
    )

    assert report["execute_requested"] is True
    assert report["final_status"] == "executed"
    assert report["executed_command_count"] == 4
    assert report["failure_count"] == 0
    assert len(calls) == 4
    assert all("main.py" in call for call in calls)


def test_dashboard_recovery_executor_skips_duplicate_allowed_commands(tmp_path: Path) -> None:
    plan = _plan()
    plan["steps"][1] = {**plan["steps"][0], "step_id": "m177_step_002_duplicate_source"}
    calls: list[list[str]] = []

    def runner(command: list[str], cwd: Path, timeout_seconds: float) -> dict:
        calls.append(command)
        return {
            "status": "succeeded",
            "returncode": 0,
            "stdout_tail": "ok",
            "stderr_tail": "",
            "duration_seconds": 0.01,
            "error": None,
        }

    report = run_dashboard_recovery_executor(
        plan,
        execute=True,
        max_steps=2,
        cwd=tmp_path,
        runner=runner,
        timeout_seconds=5,
    )

    assert report["executed_command_count"] == 2
    assert len(calls) == 2
    assert any(item["status"] == "skipped_duplicate_command" for item in report["command_results"])
