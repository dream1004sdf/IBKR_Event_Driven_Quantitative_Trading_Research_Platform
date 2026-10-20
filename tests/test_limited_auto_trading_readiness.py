from __future__ import annotations

from datetime import UTC, datetime

from recorder.limited_auto_trading_readiness import (
    build_limited_auto_trading_readiness_report,
    limited_auto_trading_readiness_markdown,
)


NOW = datetime(2026, 7, 3, 14, 35, tzinfo=UTC)
NOW_ISO = NOW.isoformat()


def _capital() -> dict:
    return {
        "generated_at": NOW_ISO,
        "status": "green",
        "status_reasons": ["capital_margin_buffer_normal"],
        "leverage_mode": "cash_only",
        "selected_account": "DU123",
        "snapshot_generated_at": NOW_ISO,
        "snapshot_age_seconds": 5,
        "metrics": {"net_liquidation": 500.0, "total_cash_value": 500.0, "available_funds": 490.0},
        "budgets": {
            "deployable_cash_after_reserve": 300.0,
            "max_new_initial_margin": 100.0,
            "core_risk_budget": 100.0,
            "max_single_position_notional": 60.0,
            "options_premium_budget": 120.0,
        },
        "withdrawal_guard": {"requires_fresh_account_snapshot": False, "max_safe_withdrawal": 0.0},
        "safety": {"ibkr_order_submitted": False, "transmit_enabled": False},
    }


def _pipeline(status: str = "green") -> dict:
    return {"generated_at": NOW_ISO, "status": status, "safety": {"ibkr_order_submitted": False, "transmit_enabled": False}}


def _strategy() -> dict:
    return {
        "generated_at": NOW_ISO,
        "summary": {"paper_trade_ready_count": 1, "blocked_count": 0},
        "contexts": {
            "micro_policy": {
                "operating_mode": "micro_cash_only",
                "permissions": {
                    "allow_fractional_stock_long": True,
                    "allow_long_option": True,
                    "allow_defined_risk_spread": False,
                    "allow_short_stock": False,
                    "allow_margin_heavy": False,
                },
            }
        },
        "safety": {"ibkr_order_submitted": False, "transmit_enabled": False, "live_routing_enabled": False},
    }


def _m247(*, expected_deferred: bool = False) -> dict:
    return {
        "generated_at": NOW_ISO,
        "status": "expected_deferred" if expected_deferred else "green",
        "expected_deferred": expected_deferred,
        "summary": {"candidate_count": 1, "ready_count": 0 if expected_deferred else 1, "micro_policy_operating_mode": "micro_cash_only"},
        "contexts": {
            "micro_policy": {
                "operating_mode": "micro_cash_only",
                "permissions": {
                    "allow_fractional_stock_long": True,
                    "allow_long_option": True,
                    "allow_defined_risk_spread": False,
                    "allow_short_stock": False,
                    "allow_margin_heavy": False,
                },
            }
        },
        "safety": {"ibkr_order_submitted": False, "transmit_enabled": False, "live_routing_enabled": False},
    }


def _m248(*, expected_deferred: bool = False) -> dict:
    return {
        "generated_at": NOW_ISO,
        "status": "expected_deferred" if expected_deferred else "green",
        "expected_deferred": expected_deferred,
        "summary": {"ticket_count": 0 if expected_deferred else 1, "ready_ticket_count": 0 if expected_deferred else 1},
        "safety": {
            "ibkr_order_submitted": False,
            "transmit_enabled": False,
            "live_routing_enabled": False,
            "whatif_request_submitted_by_m248": False,
        },
    }


def _m249(*, status: str = "green", expected_deferred: bool = False) -> dict:
    return {
        "generated_at": NOW_ISO,
        "status": status,
        "expected_deferred": expected_deferred,
        "summary": {
            "validation_item_count": 0 if expected_deferred else 1,
            "passed_count": 0 if expected_deferred else 1,
            "whatif_pending_count": 0,
            "blocked_count": 0,
            "quote_fresh": not expected_deferred,
            "option_session_expected_deferred": expected_deferred,
            "require_broker_whatif": True,
        },
        "safety": {
            "read_only_or_paper_validation": True,
            "ibkr_order_api_called_by_m249": False,
            "what_if_only": True,
            "live_order_transmitted": False,
            "ibkr_order_submitted": False,
            "transmit_enabled_for_live_orders": False,
            "live_routing_enabled": False,
            "portfolio_mutated": False,
        },
    }


def _market(*, status: str = "green") -> dict:
    return {"generated_at": NOW_ISO, "status": status, "summary": {"risk_flags": []}, "safety": {"ibkr_order_submitted": False}}


def _risk_gate(*, allow: bool = True) -> dict:
    return {
        "generated_at": NOW_ISO,
        "gate_status": "allow" if allow else "block",
        "action": "allow_new_entries" if allow else "block_new_entries",
        "new_entries_allowed": allow,
        "alpha_entries_allowed": allow,
        "hedge_entries_allowed": allow,
        "market_data_session_context": {
            "available": True,
            "is_regular_session": True,
            "session_priority_class": "monitorable_now_stress",
        },
    }


def _supervisor(*, quote_allowed: bool = True) -> dict:
    return {
        "completed_at": NOW_ISO,
        "final_status": "green" if quote_allowed else "yellow",
        "final_session": "regular" if quote_allowed else "closed",
        "final_option_quote_allowed": quote_allowed,
        "final_quote_fresh": quote_allowed,
        "final_quote_usable": quote_allowed,
    }


def _learning() -> tuple[dict, dict]:
    scorer = {
        "completed_at": NOW_ISO,
        "status": "collecting",
        "shadow_only": False,
        "safety": {"broker_orders_submitted": False, "transmit_enabled": False, "threshold_promoted": False},
    }
    evaluator = {
        "completed_at": NOW_ISO,
        "shadow_only": False,
        "promotion_ready_count": 1,
        "promotion_gate": {"enabled": True, "blocked_count": 0, "waitlist_count": 1, "watchlist_count": 0},
        "shadow_promotion_trial": {"ready_for_real_promotion_count": 1},
        "safety": {"broker_orders_submitted": False, "transmit_enabled": False, "threshold_promoted": False},
    }
    return scorer, evaluator


def _ops() -> tuple[dict, dict, dict]:
    evidence = {"generated_at": NOW_ISO, "effective_status": "green"}
    source = {"completed_at": NOW_ISO, "effective_status": "green"}
    safety_cache = {
        "completed_at": NOW_ISO,
        "final_status": "green",
        "final_cache_ready_for_all_dependent_order_paths": True,
        "safety": {"ibkr_order_submitted": False, "live_order_transmitted": False, "live_order_routing_enabled": False},
    }
    return evidence, source, safety_cache


def _base_payloads() -> dict:
    scorer, evaluator = _learning()
    evidence, source, safety_cache = _ops()
    return {
        "capital_state_payload": _capital(),
        "capital_pipeline_orchestrator_payload": _pipeline(),
        "strategy_allocation_payload": _strategy(),
        "live_read_only_capital_validation_payload": {"generated_at": NOW_ISO, "status": "green", "safety": {"ibkr_order_submitted": False, "transmit_enabled": False}},
        "micro_option_candidates_payload": _m247(),
        "paper_option_ticket_preview_payload": _m248(),
        "option_whatif_safety_validation_payload": _m249(),
        "market_data_payload": _market(),
        "market_data_risk_gate_payload": _risk_gate(),
        "options_market_structure_supervisor_payload": _supervisor(),
        "live_scorer_learning_payload": scorer,
        "threshold_candidate_evaluator_payload": evaluator,
        "evidence_freshness_payload": evidence,
        "source_online_supervisor_payload": source,
        "execution_safety_cache_supervisor_payload": safety_cache,
        "now": NOW,
    }


def test_m250_marks_limited_live_eligible_but_keeps_live_routing_locked() -> None:
    report = build_limited_auto_trading_readiness_report(**_base_payloads())

    assert report["status"] == "green"
    assert report["readiness_level"] == "limited_live_eligible"
    assert report["summary"]["hard_block_count"] == 0
    assert report["summary"]["trade_now_allowed"] is True
    assert report["summary"]["live_eligibility_clear"] is True
    assert report["execution_permissions"]["limited_auto_trade_candidate"] is True
    assert report["execution_permissions"]["live_execution_enabled"] is False
    assert report["safety"]["live_routing_enabled"] is False
    assert "M250 Limited Auto-Trading" in limited_auto_trading_readiness_markdown(report)


def test_m250_treats_closed_option_session_as_expected_deferred() -> None:
    payloads = _base_payloads()
    payloads["micro_option_candidates_payload"] = _m247(expected_deferred=True)
    payloads["paper_option_ticket_preview_payload"] = _m248(expected_deferred=True)
    payloads["option_whatif_safety_validation_payload"] = _m249(status="expected_deferred", expected_deferred=True)
    payloads["options_market_structure_supervisor_payload"] = _supervisor(quote_allowed=False)

    report = build_limited_auto_trading_readiness_report(**payloads)

    assert report["status"] == "expected_deferred"
    assert report["readiness_level"] == "session_deferred_normal"
    assert report["summary"]["expected_deferred"] is True
    assert report["summary"]["hard_block_count"] == 0
    assert any(row["code"] == "option_session_deferred_normal" for row in report["soft_warnings"])
    assert report["execution_permissions"]["live_execution_enabled"] is False


def test_m250_downgrades_m211_option_only_blocks_during_deferred_option_session() -> None:
    payloads = _base_payloads()
    payloads["micro_option_candidates_payload"] = _m247(expected_deferred=True)
    payloads["paper_option_ticket_preview_payload"] = _m248(expected_deferred=True)
    payloads["option_whatif_safety_validation_payload"] = _m249(status="expected_deferred", expected_deferred=True)
    payloads["options_market_structure_supervisor_payload"] = _supervisor(quote_allowed=False)
    payloads["execution_safety_cache_supervisor_payload"] = {
        "completed_at": NOW_ISO,
        "final_status": "yellow",
        "final_cache_ready_for_all_dependent_order_paths": False,
        "final_blocked_order_paths": ["new_option_entries", "protective_option_ticketing_without_fresh_quotes"],
        "safety": {"ibkr_order_submitted": False, "live_order_transmitted": False, "live_order_routing_enabled": False},
    }

    report = build_limited_auto_trading_readiness_report(**payloads)

    assert report["status"] == "expected_deferred"
    assert report["summary"]["hard_block_count"] == 0
    assert not any(row["code"] == "execution_safety_cache_not_ready" for row in report["hard_blocks"])
    assert any(row["code"] == "execution_safety_cache_option_session_deferred" for row in report["soft_warnings"])


def test_m250_treats_market_gate_block_as_trade_now_restriction_not_system_fatal() -> None:
    payloads = _base_payloads()
    payloads["market_data_payload"] = _market(status="red")
    payloads["market_data_risk_gate_payload"] = _risk_gate(allow=False)

    report = build_limited_auto_trading_readiness_report(**payloads)

    assert report["status"] == "yellow"
    assert report["readiness_level"] == "whatif_ready"
    assert report["summary"]["hard_block_count"] == 0
    assert report["summary"]["trade_now_allowed"] is False
    assert any(row["code"] == "market_risk_gate_blocks_new_entries" for row in report["trade_now_blockers"])
    assert report["execution_permissions"]["limited_auto_trade_candidate"] is False
    assert report["execution_permissions"]["paper_execution_enabled"] is False


def test_m250_uses_paper_planning_level_when_only_fractional_stock_micro_policy_is_available() -> None:
    payloads = _base_payloads()
    payloads["paper_option_ticket_preview_payload"] = {
        **_m248(),
        "summary": {"ticket_count": 0, "ready_ticket_count": 0},
    }
    payloads["option_whatif_safety_validation_payload"] = {
        **_m249(status="yellow"),
        "summary": {
            "validation_item_count": 0,
            "passed_count": 0,
            "whatif_pending_count": 0,
            "blocked_count": 0,
            "quote_fresh": True,
            "option_session_expected_deferred": False,
            "require_broker_whatif": True,
        },
    }

    report = build_limited_auto_trading_readiness_report(**payloads)

    assert report["status"] == "yellow"
    assert report["readiness_level"] == "paper_planning_ready"
    assert "fractional_stock_long_micro_paper" in report["summary"]["eligible_strategy_types"]


def test_m250_treats_capital_pipeline_yellow_as_refresh_needed_not_safety_fatal() -> None:
    payloads = _base_payloads()
    payloads["capital_pipeline_orchestrator_payload"] = _pipeline(status="yellow")

    report = build_limited_auto_trading_readiness_report(**payloads)

    assert report["status"] == "yellow"
    assert report["summary"]["hard_block_count"] == 0
    assert any(row["code"] == "capital_pipeline_not_green" for row in report["trade_now_blockers"])


def test_m250_treats_promotion_gate_not_clear_as_live_only_blocker() -> None:
    payloads = _base_payloads()
    scorer, evaluator = _learning()
    evaluator["promotion_ready_count"] = 0
    evaluator["promotion_gate"]["blocked_count"] = 1
    evaluator["promotion_gate"]["waitlist_count"] = 0
    evaluator["shadow_promotion_trial"]["ready_for_real_promotion_count"] = 0
    payloads["live_scorer_learning_payload"] = scorer
    payloads["threshold_candidate_evaluator_payload"] = evaluator

    report = build_limited_auto_trading_readiness_report(**payloads)

    assert report["status"] == "yellow"
    assert report["summary"]["hard_block_count"] == 0
    assert any(row["code"] == "promotion_gate_not_clear" for row in report["live_eligibility_blockers"])
    assert report["execution_permissions"]["limited_auto_trade_candidate"] is False


def test_m250_treats_promotion_gate_option_session_deferred_as_expected_soft_state() -> None:
    payloads = _base_payloads()
    scorer, evaluator = _learning()
    evaluator["promotion_ready_count"] = 0
    evaluator["promotion_gate"] = {
        "enabled": True,
        "blocked_count": 0,
        "waitlist_count": 0,
        "watchlist_count": 1,
        "watchlist_detail_reason_counts": {"option_structure_expected_deferred_stale": 1},
    }
    evaluator["shadow_promotion_trial"]["ready_for_real_promotion_count"] = 0
    payloads["live_scorer_learning_payload"] = scorer
    payloads["threshold_candidate_evaluator_payload"] = evaluator
    payloads["micro_option_candidates_payload"] = _m247(expected_deferred=True)
    payloads["paper_option_ticket_preview_payload"] = _m248(expected_deferred=True)
    payloads["option_whatif_safety_validation_payload"] = _m249(status="expected_deferred", expected_deferred=True)
    payloads["options_market_structure_supervisor_payload"] = _supervisor(quote_allowed=False)

    report = build_limited_auto_trading_readiness_report(**payloads)

    assert report["status"] == "expected_deferred"
    assert report["readiness_level"] == "session_deferred_normal"
    assert not any(row["code"] == "promotion_gate_not_clear" for row in report["live_eligibility_blockers"])
    assert any(row["code"] == "promotion_gate_option_session_deferred" for row in report["soft_warnings"])


def test_m250_downgrades_non_regular_stale_market_data_to_expected_deferred() -> None:
    payloads = _base_payloads()
    payloads["market_data_payload"] = {
        "generated_at": NOW_ISO,
        "status": "red",
        "summary": {
            "risk_flags": ["stale_market_data_present", "close_only_quotes_present", "market_temperature_elevated"],
            "missing_price_count": 0,
            "error_count": 0,
            "stale_count": 20,
            "close_only_count": 5,
        },
        "safety": {"ibkr_order_submitted": False},
    }
    payloads["market_data_risk_gate_payload"] = {
        **_risk_gate(allow=False),
        "market_data_session_context": {
            "available": True,
            "is_regular_session": False,
            "session_priority_class": "overnight_monitoring_active",
        },
    }

    report = build_limited_auto_trading_readiness_report(**payloads)

    assert report["status"] == "expected_deferred"
    assert report["readiness_level"] == "session_deferred_normal"
    assert "market_data_session_deferred_normal" in report["summary"]["expected_deferred_reasons"]
    assert not any(row["code"] == "market_data_red" for row in report["trade_now_blockers"])
    assert any(row["code"] == "market_data_session_deferred_normal" for row in report["soft_warnings"])


def test_m250_downgrades_regular_dependent_gap_alerts_outside_regular_session() -> None:
    payloads = _base_payloads()
    payloads["market_data_payload"] = {
        "generated_at": NOW_ISO,
        "status": "red",
        "summary": {
            "risk_flags": [
                "stale_market_data_present",
                "close_only_quotes_present",
                "gap_or_sudden_move_alerts",
                "market_temperature_elevated",
            ],
            "alerted_instruments": ["VIX"],
            "market_move_alert_count": 1,
            "scored_market_move_alert_count": 1,
            "missing_price_count": 0,
            "error_count": 0,
            "stale_count": 20,
            "close_only_count": 5,
        },
        "safety": {"ibkr_order_submitted": False},
    }
    payloads["market_data_risk_gate_payload"] = {
        **_risk_gate(allow=False),
        "market_data_session_context": {
            "available": True,
            "is_regular_session": False,
            "session_priority_class": "regular_dependent_after_hours_context",
            "regular_dependent_alert_instruments": ["VIX"],
        },
    }

    report = build_limited_auto_trading_readiness_report(**payloads)

    assert report["status"] == "expected_deferred"
    assert "market_data_session_deferred_normal" in report["summary"]["expected_deferred_reasons"]
    assert not any(row["code"] == "market_data_red" for row in report["trade_now_blockers"])
    assert any(row["code"] == "market_data_session_deferred_normal" for row in report["soft_warnings"])


def test_m250_treats_closed_session_expected_stale_gate_as_normal_deferred_even_with_qqq_alert() -> None:
    payloads = _base_payloads()
    payloads["market_data_payload"] = {
        "generated_at": NOW_ISO,
        "status": "red",
        "summary": {
            "risk_flags": [
                "stale_market_data_present",
                "close_only_quotes_present",
                "gap_or_sudden_move_alerts",
                "market_temperature_elevated",
            ],
            "alerted_instruments": ["QQQ", "VIX"],
            "market_move_alert_count": 2,
            "scored_market_move_alert_count": 2,
            "missing_price_count": 0,
            "error_count": 0,
            "stale_count": 6,
            "close_only_count": 26,
        },
        "safety": {"ibkr_order_submitted": False},
    }
    payloads["market_data_risk_gate_payload"] = {
        **_risk_gate(allow=True),
        "action": "allow_with_market_data_haircut",
        "market_data_session_context": {
            "available": True,
            "is_regular_session": False,
            "session_priority_class": "overnight_monitoring_active",
        },
        "market_data": {
            "closed_session_expected_stale": {
                "active": True,
                "reason": "closed_session_expected_stale_until_next_regular_open",
                "next_regular_open_wait_hours": 57.0,
            }
        },
    }

    report = build_limited_auto_trading_readiness_report(**payloads)

    assert report["status"] == "expected_deferred"
    assert report["readiness_level"] == "session_deferred_normal"
    assert "market_data_session_deferred_normal" in report["summary"]["expected_deferred_reasons"]
    assert not any(row["code"] == "market_data_red" for row in report["trade_now_blockers"])
    assert not any(row["code"] == "market_risk_gate_blocks_new_entries" for row in report["trade_now_blockers"])
    assert any(row["code"] == "market_data_session_deferred_normal" for row in report["soft_warnings"])


def test_m250_exposes_realistic_gate_severity_split() -> None:
    payloads = _base_payloads()
    payloads["market_data_payload"] = _market(status="yellow")
    payloads["market_data_payload"]["summary"] = {"risk_flags": ["market_temperature_elevated"]}
    payloads["market_data_payload"]["temperature"] = {"label": "hot", "score": 0.72, "risk_flags": ["market_temperature_elevated"]}
    payloads["market_data_risk_gate_payload"] = {
        **_risk_gate(allow=True),
        "action": "allow_with_market_data_haircut",
        "entry_size_multiplier": 0.45,
    }
    payloads["evidence_freshness_payload"] = {
        "generated_at": NOW_ISO,
        "effective_status": "yellow",
        "safety": {"ibkr_order_submitted": False},
    }

    report = build_limited_auto_trading_readiness_report(**payloads)
    gate_policy = report["contexts"]["gate_severity_policy"]

    assert gate_policy["haircut_count"] >= 1
    assert gate_policy["confidence_discount_count"] >= 1
    assert gate_policy["monitoring_only_count"] >= 0
    assert report["summary"]["gate_execution_posture"] in {"size_reduced", "confidence_discounted"}
    assert report["summary"]["gate_size_multiplier"] <= 0.5
    assert not any(row["code"] == "market_risk_gate_blocks_new_entries" for row in report["trade_now_blockers"])
