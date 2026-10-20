from __future__ import annotations

from pathlib import Path

from recorder.dashboard_action_queue import (
    build_dashboard_action_queue,
    dashboard_action_queue_markdown,
    write_dashboard_action_queue_json,
    write_dashboard_action_queue_markdown,
)
from recorder.dashboard_operations import build_dashboard_recovery_plan


def test_dashboard_action_queue_prioritizes_market_and_source_gaps(tmp_path: Path) -> None:
    snapshot = {
        "dashboard_snapshot_version": "trading-dashboard-snapshot-v1",
        "generated_at": "2026-06-30T10:00:00+00:00",
        "status": {
            "color": "yellow",
            "reasons": ["market_data_risk_gate_haircut_active", "source_ingest_health_online_gap"],
        },
        "account": {"buying_power": 1000.0, "available_funds": 1000.0},
        "risk": {"risk_state": "within_limits", "new_entries_allowed": True},
        "market_data": {
            "mark_refresh_quality": {"quality": {"quality_action_counts": {"allow": 1}}},
            "market_data_desk": {
                "status": "yellow",
                "temperature": {"label": "watch", "score": 22.0},
                "summary": {
                    "status": "yellow",
                    "temperature_score": 22.0,
                    "alert_count": 3,
                    "delayed_count": 0,
                    "missing_price_count": 0,
                    "error_count": 0,
                    "risk_flags": ["gap_or_sudden_move_alerts"],
                },
            },
            "market_data_risk_gate": {
                "gate_status": "watch",
                "action": "allow_with_market_data_haircut",
                "allowed": True,
                "new_entries_allowed": True,
                "entry_size_multiplier": 0.7,
                "option_strategy_bias": "defined_risk_preferred",
                "reason": "gap_or_sudden_move_alerts",
                "haircut_reasons": ["gap_or_sudden_move_alerts"],
            },
        },
        "evidence_freshness": {"overall_status": "green", "stale_source_keys": [], "lagging_source_keys": []},
        "evidence_auto_recovery": {"status": "healthy_no_refresh_needed", "consecutive_failures": 0},
        "source_ingest_health": {
            "overall_status": "yellow",
            "online_problem_source_keys": ["market_news"],
            "degraded_source_keys": ["market_news"],
            "root_cause_counts": {"stale": 1},
        },
        "source_online_supervisor": {
            "final_status": "yellow",
            "final_source_ingest_health_status": "yellow",
            "final_online_problem_source_keys": ["market_news"],
            "latest_tick": {
                "actions": [
                    {
                        "action_id": "market_news_ibkr_catchup",
                        "source_key": "market_news",
                        "status": "failed",
                        "description": "Catch up IBKR/Benzinga news.",
                    }
                ]
            },
            "failed_actions": [
                {
                    "action_id": "market_news_ibkr_catchup",
                    "source_key": "market_news",
                    "status": "failed",
                    "description": "Catch up IBKR/Benzinga news.",
                }
            ],
        },
    }

    queue = build_dashboard_action_queue(snapshot, max_actions=8, now="2026-06-30T10:01:00+00:00")

    assert queue["dashboard_action_queue_version"] == "dashboard-action-queue-v3"
    assert queue["status_color"] == "yellow"
    assert queue["total_action_count"] >= 4
    assert queue["visible_action_count"] <= 8
    assert queue["safety"]["read_only_queue"] is True
    assert queue["safety"]["broker_order_submitted"] is False
    assert all(action["can_auto_execute"] is False for action in queue["actions"])
    assert queue["actions"] == sorted(queue["actions"], key=lambda item: (-item["priority_score"], item["action_id"]))
    assert any(action["domain"] == "market_data_risk_gate" for action in queue["actions"])
    assert any(action["domain"] == "source_online_supervisor" for action in queue["actions"])

    markdown = dashboard_action_queue_markdown(queue)
    assert "Dashboard Action Queue" in markdown
    assert "read-only queue" in markdown

    json_path = write_dashboard_action_queue_json(tmp_path / "queue.json", queue)
    md_path = write_dashboard_action_queue_markdown(tmp_path / "queue.md", queue)
    assert json_path.exists()
    assert md_path.exists()


def test_dashboard_action_queue_session_adjusts_regular_dependent_after_hours_market_stress() -> None:
    snapshot = {
        "dashboard_snapshot_version": "trading-dashboard-snapshot-v1",
        "generated_at": "2026-06-30T22:00:00+00:00",
        "status": {
            "color": "yellow",
            "reasons": ["market_data_desk_temperature_yellow"],
        },
        "account": {"buying_power": 1000.0, "available_funds": 1000.0},
        "risk": {"risk_state": "within_limits", "new_entries_allowed": True},
        "market_data": {
            "mark_refresh_quality": {"quality": {"quality_action_counts": {"allow": 1}}},
            "market_data_desk": {
                "status": "red",
                "temperature": {"label": "stress", "score": 35.0},
                "summary": {
                    "status": "red",
                    "temperature_score": 35.0,
                    "alert_count": 2,
                    "delayed_count": 0,
                    "missing_price_count": 0,
                    "error_count": 0,
                    "risk_flags": ["regular_reference_after_hours_alerts"],
                    "alerted_instruments": ["VIX", "VVIX"],
                },
            },
            "market_data_session_overlay": {
                "session_name": "overnight",
                "is_regular_session": False,
                "risk_interpretation": "overnight_monitoring_active",
                "stock_cash_overnight_monitoring_enabled": True,
                "option_quotes_regular_session_dependent": True,
                "monitorability_counts": {"monitorable_now": 0, "monitor_if_fresh_price_present": 2},
                "monitorable_alert_instruments": [],
                "regular_dependent_alert_instruments": ["VIX", "VVIX"],
            },
            "market_data_risk_gate": {
                "gate_status": "red",
                "action": "defensive_or_hedge_only",
                "allowed": True,
                "new_entries_allowed": False,
                "entry_size_multiplier": 0.0,
                "option_strategy_bias": "defensive_only",
                "reason": "regular_reference_after_hours_alerts",
                "defensive_reasons": ["regular_reference_after_hours_alerts"],
            },
        },
        "evidence_freshness": {"overall_status": "green", "stale_source_keys": [], "lagging_source_keys": []},
        "evidence_auto_recovery": {"status": "healthy_no_refresh_needed", "consecutive_failures": 0},
        "source_ingest_health": {"overall_status": "green", "online_problem_source_keys": [], "degraded_source_keys": []},
        "source_online_supervisor": {"final_status": "green", "final_online_problem_source_keys": [], "failed_actions": []},
    }

    queue = build_dashboard_action_queue(snapshot, max_actions=8, now="2026-06-30T22:01:00+00:00")
    market_actions = [action for action in queue["actions"] if action["domain"] in {"market_data", "market_data_risk_gate"}]

    assert queue["market_data_session_context"]["session_priority_class"] == "regular_dependent_after_hours_context"
    assert market_actions
    assert all(action["severity"] in {"watch", "yellow"} for action in market_actions)
    assert all(action["priority_score"] < 3000 for action in market_actions)
    assert any((action["details"] or {}).get("session_adjusted_priority") is True for action in market_actions)
    assert any("regular-session" in action["recommendation"] or "regular session" in action["recommendation"] for action in market_actions)


def test_dashboard_action_queue_keeps_monitorable_overnight_market_stress_high_priority() -> None:
    snapshot = {
        "dashboard_snapshot_version": "trading-dashboard-snapshot-v1",
        "generated_at": "2026-06-30T22:00:00+00:00",
        "status": {"color": "red", "reasons": ["market_data_desk_temperature_red"]},
        "account": {"buying_power": 1000.0, "available_funds": 1000.0},
        "risk": {"risk_state": "within_limits", "new_entries_allowed": True},
        "market_data": {
            "mark_refresh_quality": {"quality": {"quality_action_counts": {"allow": 1}}},
            "market_data_desk": {
                "status": "red",
                "temperature": {"label": "stress", "score": 35.0},
                "summary": {
                    "status": "red",
                    "temperature_score": 35.0,
                    "alert_count": 2,
                    "delayed_count": 0,
                    "missing_price_count": 0,
                    "error_count": 0,
                    "risk_flags": ["monitorable_overnight_alerts"],
                    "alerted_instruments": ["NQ", "WTI"],
                },
            },
            "market_data_session_overlay": {
                "session_name": "overnight",
                "is_regular_session": False,
                "risk_interpretation": "overnight_monitoring_active",
                "stock_cash_overnight_monitoring_enabled": True,
                "option_quotes_regular_session_dependent": True,
                "monitorability_counts": {"monitorable_now": 2, "monitor_if_fresh_price_present": 0},
                "monitorable_alert_instruments": ["NQ", "WTI"],
                "regular_dependent_alert_instruments": [],
            },
            "market_data_risk_gate": {
                "gate_status": "red",
                "action": "defensive_or_hedge_only",
                "allowed": True,
                "new_entries_allowed": False,
                "entry_size_multiplier": 0.0,
                "option_strategy_bias": "defensive_only",
                "reason": "monitorable_overnight_alerts",
                "defensive_reasons": ["monitorable_overnight_alerts"],
            },
        },
        "evidence_freshness": {"overall_status": "green", "stale_source_keys": [], "lagging_source_keys": []},
        "evidence_auto_recovery": {"status": "healthy_no_refresh_needed", "consecutive_failures": 0},
        "source_ingest_health": {"overall_status": "green", "online_problem_source_keys": [], "degraded_source_keys": []},
        "source_online_supervisor": {"final_status": "green", "final_online_problem_source_keys": [], "failed_actions": []},
    }

    queue = build_dashboard_action_queue(snapshot, max_actions=8, now="2026-06-30T22:01:00+00:00")
    market_actions = [action for action in queue["actions"] if action["domain"] in {"market_data", "market_data_risk_gate"}]

    assert queue["market_data_session_context"]["session_priority_class"] == "overnight_monitoring_active"
    assert market_actions
    assert any(action["severity"] == "red" for action in market_actions)
    assert any(action["priority_score"] >= 3000 for action in market_actions)
    assert not any((action["details"] or {}).get("session_adjusted_priority") is True for action in market_actions)


def test_dashboard_action_queue_adds_options_market_structure_supervisor_actions() -> None:
    snapshot = {
        "dashboard_snapshot_version": "trading-dashboard-snapshot-v1",
        "generated_at": "2026-07-01T15:00:00+00:00",
        "status": {"color": "yellow", "reasons": ["options_market_structure_quality_gap"]},
        "account": {"buying_power": 1000.0, "available_funds": 1000.0},
        "risk": {"risk_state": "within_limits", "new_entries_allowed": True},
        "market_data": {
            "mark_refresh_quality": {"quality": {"quality_action_counts": {"allow": 1}}},
            "market_data_desk": {"summary": {"status": "green", "alert_count": 0}},
        },
        "evidence_freshness": {"overall_status": "green", "stale_source_keys": [], "lagging_source_keys": []},
        "evidence_auto_recovery": {"status": "healthy_no_refresh_needed", "consecutive_failures": 0},
        "source_ingest_health": {"overall_status": "green", "online_problem_source_keys": [], "degraded_source_keys": []},
        "source_online_supervisor": {"final_status": "green", "final_online_problem_source_keys": [], "failed_actions": []},
        "options_market_structure_supervisor": {
            "final_status": "red",
            "final_session": "regular",
            "final_session_refresh_phase": "regular_close_window",
            "final_option_quote_allowed": True,
            "final_quote_usable": False,
            "final_quote_quality_status": "red",
            "final_quote_data_latency_status": "delayed_only",
            "final_quote_delayed_contracts": 4,
            "final_quote_delayed_symbols": ["SPY"],
            "final_optional_index_quote_usable": False,
            "final_optional_index_quote_quality_status": "missing",
            "final_optional_index_quote_data_latency_status": "live",
            "final_action_status_counts": {"failed": 1},
            "final_failure_reason_counts": {"contract_unresolved": 1},
            "final_artifact_delta": {"outcome": "worsened", "worsened_signal_count": 1},
            "final_optional_index_artifact_delta": {"outcome": "unchanged"},
            "index_option_coverage_matrix": {
                "overall_status": "yellow",
                "resolver_policy": {"direct_index_failure_blocks_core_proxy": False},
                "families": [
                    {
                        "family_id": "sp500",
                        "label": "S&P 500",
                        "coverage_status": "proxy_only",
                        "confidence": "medium",
                        "direct": {"status": "failed", "symbols": ["SPX"], "ok_symbols": []},
                        "mini": {"status": "missing", "symbols": ["XSP"], "ok_symbols": []},
                        "proxy": {"status": "ok", "symbols": ["SPY"], "ok_symbols": ["SPY"]},
                    }
                ],
            },
            "m220_family_repair_plan": [
                {
                    "family_id": "sp500",
                    "label": "S&P 500",
                    "operating_mode": "proxy_fallback_active",
                    "severity": "yellow",
                    "selected_source_type": "proxy",
                    "selected_symbols": ["SPY"],
                    "issue_count": 1,
                    "issue_category_counts": {"contract_mapping": 1},
                    "issues": [{"symbol": "SPX", "role": "direct", "category": "contract_mapping", "severity": "red"}],
                    "operator_summary": "S&P 500 is using proxy symbols ['SPY']; direct/mini repair is non-blocking.",
                    "next_action": "keep_proxy_for_live_context_and_schedule_direct_mini_repair",
                    "proxy_fallback_active": True,
                }
            ],
            "symbol_status": {
                "SPX": {
                    "symbol": "SPX",
                    "scope": "optional_index",
                    "last_status": "failed",
                    "last_failure_reason": "contract_unresolved",
                }
            },
            "symbol_failure_notes": {
                "optional_index_option_quote_snapshot:SPX": {
                    "symbol": "SPX",
                    "action_id": "optional_index_option_quote_snapshot",
                    "reason": "contract_unresolved",
                }
            },
            "isolated_symbols_by_action": {
                "optional_index_option_quote_snapshot:SPX": {
                    "symbol": "SPX",
                    "action_id": "optional_index_option_quote_snapshot",
                    "reason": "contract_unresolved",
                    "failure_streak": 3,
                    "isolated_until": "2026-07-01T15:20:00+00:00",
                }
            },
            "config": {
                "symbols": ["SPY", "QQQ", "IWM", "DIA"],
                "optional_index_symbols": ["SPX", "NDX", "RUT", "VIX"],
            },
        },
    }

    queue = build_dashboard_action_queue(snapshot, max_actions=20, now="2026-07-01T15:01:00+00:00")
    action_ids = {action["action_id"] for action in queue["actions"]}

    assert "refresh_core_option_market_structure_quotes" in action_ids
    assert "repair_optional_index_option_market_structure_quotes" in action_ids
    assert "check_ibkr_option_contract_mapping" in action_ids
    assert "check_option_quote_live_data_status" in action_ids
    assert "review_isolated_option_structure_symbols" in action_ids
    assert "review_close_window_option_structure_artifact_delta" in action_ids
    assert "review_index_option_family_coverage_matrix" in action_ids
    assert "m220_repair_index_option_family_sp500" in action_ids
    assert queue["domain_counts"]["options_market_structure_supervisor"] >= 3
    assert any(action["domain"] == "options_market_structure_supervisor" and action["severity"] == "red" for action in queue["actions"])


def test_dashboard_action_queue_treats_off_session_option_deferred_as_monitor_not_failure() -> None:
    snapshot = {
        "dashboard_snapshot_version": "trading-dashboard-snapshot-v1",
        "generated_at": "2026-07-01T08:00:00+00:00",
        "status": {"color": "yellow", "reasons": ["options_market_structure_deferred"]},
        "account": {"buying_power": 1000.0, "available_funds": 1000.0},
        "risk": {"risk_state": "within_limits", "new_entries_allowed": True},
        "market_data": {"mark_refresh_quality": {"quality": {"quality_action_counts": {"allow": 1}}}, "market_data_desk": {"summary": {"status": "green", "alert_count": 0}}},
        "evidence_freshness": {"overall_status": "green", "stale_source_keys": [], "lagging_source_keys": []},
        "evidence_auto_recovery": {"status": "healthy_no_refresh_needed", "consecutive_failures": 0},
        "source_ingest_health": {"overall_status": "green", "online_problem_source_keys": [], "degraded_source_keys": []},
        "source_online_supervisor": {"final_status": "green", "final_online_problem_source_keys": [], "failed_actions": []},
        "options_market_structure_supervisor": {
            "final_status": "yellow",
            "final_session": "pre_market",
            "final_session_refresh_phase": "pre_market",
            "final_option_quote_allowed": False,
            "final_quote_usable": False,
            "final_quote_quality_status": "missing",
            "final_action_status_counts": {"deferred_until_regular_session": 3, "blocked_missing_quote_snapshot": 2},
            "option_quote_deferred_operating_state": {
                "status": "normal_session_deferred",
                "alert_policy": "do_not_alert_as_failure",
                "actionable_now": False,
            },
            "m220_operational_runbook": {"one_command": "python main.py options-market-structure-supervisor --execute-actions"},
            "config": {"symbols": ["SPY", "QQQ"], "optional_index_symbols": ["SPX"]},
        },
    }

    queue = build_dashboard_action_queue(snapshot, max_actions=8, now="2026-07-01T08:01:00+00:00")
    action = next(item for item in queue["actions"] if item["action_id"] == "monitor_regular_session_deferred_option_quotes")

    assert action["severity"] == "watch"
    assert action["source_key"] == "regular_session_deferred_context"
    assert action["details"]["normal_off_session_wait"] is True
    assert action["details"]["deferred_operating_state"]["alert_policy"] == "do_not_alert_as_failure"
    assert not any(item["action_id"] == "refresh_core_option_market_structure_quotes" for item in queue["actions"])


def test_dashboard_action_queue_decomposes_session_release_governor_blocks() -> None:
    snapshot = {
        "dashboard_snapshot_version": "trading-dashboard-snapshot-v1",
        "generated_at": "2026-07-01T15:00:00+00:00",
        "status": {"color": "red", "reasons": ["session_transition_release_governor_blocks_alpha"]},
        "account": {"buying_power": 1000.0, "available_funds": 1000.0},
        "risk": {"risk_state": "within_limits", "new_entries_allowed": True},
        "buy_sell_plan": {
            "execution_intent_ledger_version": "paper-execution-intent-ledger-v2-portfolio-risk-gated",
            "pending_paper_execution_count": 0,
            "blocked_by_session_transition_release_governor_count": 2,
            "session_transition_release_governor": {
                "release_status": "blocked",
                "ordinary_alpha_release_allowed": False,
            },
        },
        "market_data": {
            "mark_refresh_quality": {"quality": {"quality_action_counts": {"allow": 1}}},
            "market_data_desk": {"summary": {"status": "green", "alert_count": 0}},
            "session_transition_release_governor": {
                "release_status": "blocked",
                "severity": "red",
                "ordinary_alpha_release_allowed": False,
                "max_new_entry_size_multiplier": 0.0,
                "reason": "transition_gate_not_release_candidate,operating_auto_gate_blocks_or_has_no_ready_alpha,evidence_preflight_blocks_entries",
                "failed_check_count": 3,
                "check_count": 7,
                "transition_decision": "maintain_haircut",
                "market_data_risk_gate_status": "watch",
                "operating_mode": "risk_reduction_only",
                "operating_auto_ready_count": 0,
                "portfolio_risk_state": "within_limits",
                "evidence_gate_status": "block",
                "checks": [
                    {
                        "name": "transition_gate_is_release_candidate",
                        "passed": False,
                        "value": "maintain_haircut",
                        "limit": "release_candidate",
                        "reason": "transition_gate_not_release_candidate",
                    },
                    {
                        "name": "operating_auto_gate_has_ready_alpha",
                        "passed": False,
                        "value": {"mode": "risk_reduction_only", "auto_ready_count": 0},
                        "limit": "ordinary_alpha_allowed_and_auto_ready_count_gt_0",
                        "reason": "operating_auto_gate_blocks_or_has_no_ready_alpha",
                    },
                    {
                        "name": "evidence_preflight_allows_entries",
                        "passed": False,
                        "value": {"gate_status": "block", "action": "block_new_paper_entries"},
                        "limit": "allowed_with_positive_size",
                        "reason": "evidence_preflight_blocks_entries",
                    },
                    {
                        "name": "portfolio_risk_state_allows_new_entries",
                        "passed": True,
                        "value": {"risk_state": "within_limits"},
                        "limit": "within_limits_or_watch_with_new_entries_allowed",
                    },
                ],
            },
        },
        "evidence_freshness": {"overall_status": "green", "stale_source_keys": [], "lagging_source_keys": []},
        "evidence_auto_recovery": {"status": "healthy_no_refresh_needed", "consecutive_failures": 0},
        "source_ingest_health": {"overall_status": "green", "online_problem_source_keys": [], "degraded_source_keys": []},
        "source_online_supervisor": {"final_status": "green", "final_online_problem_source_keys": [], "failed_actions": []},
    }

    queue = build_dashboard_action_queue(snapshot, max_actions=12, now="2026-07-01T15:01:00+00:00")
    release_actions = [action for action in queue["actions"] if action["domain"] == "session_transition_release_governor"]
    execution_path_actions = [action for action in queue["actions"] if action["domain"] == "paper_execution_path"]
    action_ids = {action["action_id"] for action in release_actions}

    assert "resolve_session_transition_release_transition_gate_is_release_candidate" in action_ids
    assert "resolve_session_transition_release_operating_auto_gate_has_ready_alpha" in action_ids
    assert "resolve_session_transition_release_evidence_preflight_allows_entries" in action_ids
    assert all(action["can_auto_execute"] is False for action in release_actions)
    assert any(action["auto_recovery_target"] == "session_transition_closed_loop" for action in release_actions)
    assert any(action["auto_recovery_target"] == "operating_auto_gate" for action in release_actions)
    assert any(action["auto_recovery_target"] == "evidence_refresh_watchdog" for action in release_actions)
    assert any(action["action_id"] == "review_m204_release_governed_primary_ledger_blocks" for action in execution_path_actions)
    assert queue["domain_counts"]["paper_execution_path"] >= 1

    plan = build_dashboard_recovery_plan(queue, max_steps=12, now="2026-07-01T15:02:00+00:00")
    phases = {step["phase"] for step in plan["steps"]}

    assert "release_transition_rebuild" in phases
    assert "operating_gate_rebuild" in phases
    assert "release_governed_ledger_rebuild" in phases
    assert "evidence_recovery" in phases
    assert any("session-transition-release-governor" in " ".join(step["command_preview"]) for step in plan["steps"])
