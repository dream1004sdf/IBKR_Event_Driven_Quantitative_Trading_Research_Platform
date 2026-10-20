from __future__ import annotations

from pathlib import Path

from recorder.dashboard_operations import (
    build_dashboard_recovery_plan,
    dashboard_recovery_plan_markdown,
    write_dashboard_recovery_plan_json,
    write_dashboard_recovery_plan_markdown,
)


def test_dashboard_recovery_plan_builds_dry_run_steps(tmp_path: Path) -> None:
    action_queue = {
        "dashboard_action_queue_version": "dashboard-action-queue-v1",
        "generated_at": "2026-06-30T10:00:00+00:00",
        "status_color": "yellow",
        "status_reasons": ["market_data_risk_gate_haircut_active"],
        "total_action_count": 3,
        "actions": [
            {
                "action_id": "repair_source_ingest_health_online_required_gap",
                "priority_score": 2110,
                "severity": "yellow",
                "domain": "source_ingest_health",
                "source_key": "online_required_sources",
                "title": "Recover Data Source Doctor gap",
                "reason": "Source ingest health has online-required gaps.",
                "recommendation": "Use the online-required source root cause to run the Source Online Supervisor first.",
                "auto_recovery_target": "source_online_supervisor",
                "can_auto_execute": False,
                "read_only": True,
                "details": {"online_problem_source_keys": ["market_news"]},
            },
            {
                "action_id": "apply_market_data_risk_gate_allow_with_market_data_haircut",
                "priority_score": 2137,
                "severity": "yellow",
                "domain": "market_data_risk_gate",
                "source_key": "market_data",
                "title": "Review Market Data Risk Gate sizing",
                "reason": "gap_or_sudden_move_alerts",
                "recommendation": "Keep the new-entry sizing haircut until resolved.",
                "auto_recovery_target": "market_data_risk_gate",
                "can_auto_execute": False,
                "read_only": True,
                "details": {"entry_size_multiplier": 0.7},
            },
            {
                "action_id": "risk_account_buying_power_below_threshold",
                "priority_score": 2174,
                "severity": "yellow",
                "domain": "portfolio_risk",
                "source_key": "account_or_portfolio",
                "title": "Review account and portfolio risk",
                "reason": "Buying power is below threshold.",
                "recommendation": "Inspect cash, margin, exposure, and open intents.",
                "auto_recovery_target": "risk_review",
                "can_auto_execute": False,
                "read_only": True,
            },
        ],
    }

    plan = build_dashboard_recovery_plan(action_queue, max_steps=5, now="2026-06-30T10:01:00+00:00")

    assert plan["dashboard_recovery_plan_version"] == "dashboard-recovery-plan-v1"
    assert plan["dry_run"] is True
    assert plan["planned_step_count"] == 3
    assert plan["execution_policy"]["execute_subprocesses"] is False
    assert plan["execution_policy"]["submit_broker_orders"] is False
    assert plan["safety"]["dry_run_only"] is True
    assert all(step["would_execute"] is False for step in plan["steps"])
    assert all(step["safety"]["broker_order_submitted"] is False for step in plan["steps"])
    assert plan["steps"][0]["phase"] == "source_recovery"
    assert any(step["phase"] == "market_data_recovery" for step in plan["steps"])
    assert any(step["status"] == "manual_review_required" for step in plan["steps"])

    markdown = dashboard_recovery_plan_markdown(plan)
    assert "Dashboard Recovery Plan" in markdown
    assert "dry-run plan" in markdown

    json_path = write_dashboard_recovery_plan_json(tmp_path / "plan.json", plan)
    md_path = write_dashboard_recovery_plan_markdown(tmp_path / "plan.md", plan)
    assert json_path.exists()
    assert md_path.exists()
