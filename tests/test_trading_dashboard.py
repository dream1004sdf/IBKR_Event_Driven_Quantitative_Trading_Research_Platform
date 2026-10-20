from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from freezegun import freeze_time

from recorder.trading_dashboard import (
    _effective_option_quote_session_context,
    _limited_auto_trading_readiness_summary,
    _m220_family_for_market_key,
    _status_lamp,
    _status_operational_readiness_overlay,
    _status_trade_blocking_summary,
    build_trading_dashboard_snapshot,
    deliver_dashboard_alerts_to_telegram,
    evaluate_dashboard_alerts,
    run_trading_dashboard_loop,
    trading_dashboard_html,
    trading_dashboard_loop_markdown,
    trading_dashboard_markdown,
)


def _write_json(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def test_m220_market_move_overlay_does_not_map_non_us_indexes_to_sp500() -> None:
    assert _m220_family_for_market_key("NIKKEI", "asia_equity_index") is None
    assert _m220_family_for_market_key("KOSPI_FUT", "korea_equity_futures") is None
    assert (_m220_family_for_market_key("ES", "us_equity_futures") or {})["family_id"] == "sp500"
    assert (_m220_family_for_market_key("NQ", "us_equity_futures") or {})["family_id"] == "nasdaq100"


def test_option_session_context_prefers_regular_live_quote_gate_over_stale_overlay() -> None:
    context = _effective_option_quote_session_context(
        market_data_session_overlay={
            "session_name": "overnight",
            "option_quotes_regular_session_dependent": True,
        },
        quote_gate={
            "fresh": True,
            "best_option_quoted_count": 24,
            "option_quote_deferred": False,
            "option_quote_session": "regular",
        },
    )

    assert context["source"] == "option_quote_gate"
    assert context["option_session_name"] == "regular"
    assert context["option_quotes_regular_session_dependent"] is False


def test_option_session_context_preserves_off_hours_deferred_gate() -> None:
    context = _effective_option_quote_session_context(
        market_data_session_overlay={
            "session_name": "after_hours",
            "option_quotes_regular_session_dependent": True,
        },
        quote_gate={
            "fresh": True,
            "best_option_quoted_count": 0,
            "option_quote_deferred": True,
            "option_quote_session": "after_hours",
        },
    )

    assert context["source"] == "option_quote_gate"
    assert context["option_session_name"] == "after_hours"
    assert context["option_quotes_regular_session_dependent"] is True


def test_dashboard_status_preserves_raw_red_but_uses_effective_display_color() -> None:
    status = {
        "color": "red",
        "reasons": ["market_data_desk_temperature_red"],
        "trade_blocking_summary": {
            "trade_blocking": False,
            "trade_blocking_reasons": [],
            "monitoring_only_reasons": ["market_data_desk_temperature_red"],
        },
    }

    overlay = _status_operational_readiness_overlay(status)
    raw_status_color = status.get("color")
    status["raw_color"] = raw_status_color
    status["display_color"] = overlay.get("display_color") or raw_status_color
    status["alert_color"] = overlay.get("alert_color") or status.get("display_color") or raw_status_color
    status["color"] = status.get("display_color") or raw_status_color

    assert status["raw_color"] == "red"
    assert status["color"] == "yellow"
    assert status["display_color"] == "yellow"
    assert status["alert_color"] == "yellow"
    assert overlay["policy"]["raw_status_preserved_as_raw_color"] is True
    assert overlay["policy"]["status_color_is_effective_display_color"] is True


def test_limited_auto_trading_readiness_summary_preserves_gate_severity_policy() -> None:
    summary = _limited_auto_trading_readiness_summary(
        {
            "limited_auto_trading_readiness_version": "m250-test",
            "generated_at": "2026-07-03T14:00:00+00:00",
            "status": "yellow",
            "readiness_level": "paper_only",
            "summary": {
                "hard_block_count": 0,
                "trade_now_blocker_count": 0,
                "soft_warning_count": 1,
                "gate_execution_posture": "size_reduced",
                "gate_haircut_count": 1,
                "gate_confidence_discount_count": 1,
                "gate_monitoring_only_count": 1,
                "gate_size_multiplier": 0.5,
                "gate_confidence_multiplier": 0.85,
            },
            "contexts": {
                "gate_severity_policy": {
                    "recommended_execution_posture": "size_reduced",
                    "size_multiplier": 0.5,
                    "confidence_multiplier": 0.85,
                    "signals": [{"code": "market_temperature_elevated", "category": "haircut"}],
                }
            },
            "execution_permissions": {"live_routing_status": "locked"},
        }
    )

    assert summary["gate_execution_posture"] == "size_reduced"
    assert summary["gate_haircut_count"] == 1
    assert summary["gate_confidence_discount_count"] == 1
    assert summary["gate_monitoring_only_count"] == 1
    assert summary["gate_size_multiplier"] == 0.5
    assert summary["gate_confidence_multiplier"] == 0.85
    assert summary["gate_severity_policy"]["signals"][0]["category"] == "haircut"


def test_trading_dashboard_alerts_include_options_market_structure_supervisor_conditions() -> None:
    snapshot = {
        "status": {"color": "green", "reasons": ["all_primary_dashboard_checks_green"]},
        "account": {"buying_power": 1000.0, "available_funds": 1000.0},
        "risk": {"risk_state": "within_limits"},
        "market_data": {
            "mark_refresh_quality": {"quality": {"quality_action_counts": {"allow": 1}}},
            "market_data_desk": {"summary": {"status": "green", "alert_count": 0}},
        },
        "evidence_freshness": {"overall_status": "green", "stale_source_keys": [], "lagging_source_keys": []},
        "evidence_auto_recovery": {"status": "healthy_no_refresh_needed", "consecutive_failures": 0},
        "source_ingest_health": {"overall_status": "green", "online_problem_source_keys": [], "degraded_source_keys": []},
        "source_online_supervisor": {"final_status": "green", "failed_actions": []},
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
            "final_failure_reason_counts": {"contract_unresolved": 1},
            "final_artifact_delta": {"outcome": "worsened", "worsened_signal_count": 2},
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
            "isolated_symbols_by_action": {
                "fresh_option_quote_snapshot:SPY": {
                    "symbol": "SPY",
                    "action_id": "fresh_option_quote_snapshot",
                    "reason": "delayed_only_market_data",
                    "failure_streak": 3,
                    "isolated_until": "2026-07-01T15:20:00+00:00",
                }
            },
            "config": {"symbols": ["SPY", "QQQ"], "optional_index_symbols": ["SPX"]},
        },
    }

    _state, alerts = evaluate_dashboard_alerts(snapshot, now="2026-07-01T15:01:00+00:00")
    keys = {alert["condition_key"] for alert in alerts}

    assert "options_market_structure_core_quote_quality_gap" in keys
    assert "options_market_structure_core_quote_delayed_data" in keys
    assert "options_market_structure_symbol_isolation_active" in keys
    assert "options_market_structure_close_window_artifact_delta_worsened" in keys
    assert "options_market_structure_index_family_coverage_weak" in keys
    assert any(alert["severity"] == "red" for alert in alerts if str(alert["condition_key"]).startswith("options_market_structure"))


@freeze_time("2026-06-30 22:15:00")
def test_trading_dashboard_snapshot_combines_core_sections(tmp_path: Path) -> None:
    account = _write_json(
        tmp_path / "account.json",
        {
            "snapshot_version": "ibkr-account-read-only-snapshot-v1",
            "generated_at": "2026-06-30T10:00:00+00:00",
            "selected_account": "U1",
            "account_count": 1,
            "equity": 1000.0,
            "cash": 500.0,
            "buying_power": 500.0,
            "position_count": 3,
            "open_order_count": 0,
            "accounts": [{"account": "U1", "tags": {"AvailableFunds": {"numeric_value": 500.0}, "MaintMarginReq": {"numeric_value": 10.0}}}],
            "positions": [
                {"account": "U1", "contract": {"symbol": "SPY", "sec_type": "STK", "currency": "USD"}, "position": 1, "avg_cost": 500.0},
                {
                    "account": "U1",
                    "contract": {
                        "symbol": "SPY",
                        "sec_type": "OPT",
                        "currency": "USD",
                        "last_trade_date_or_contract_month": "20260717",
                        "right": "C",
                        "strike": 505.0,
                    },
                    "position": 1,
                    "avg_cost": 2.25,
                },
                {
                    "account": "U1",
                    "contract": {
                        "symbol": "SPY",
                        "sec_type": "OPT",
                        "currency": "USD",
                        "last_trade_date_or_contract_month": "20260717",
                        "right": "C",
                        "strike": 515.0,
                    },
                    "position": -1,
                    "avg_cost": 0.95,
                },
            ],
        },
    )
    portfolio = _write_json(
        tmp_path / "portfolio.json",
        {
            "result": {
                "paper_portfolio_state_version": "paper-portfolio-state-reconciler-v1",
                "generated_at": "2026-06-30T10:01:00+00:00",
                "totals": {"open_symbol_count": 1, "total_pnl": 2.5},
                "portfolio_rows": [
                    {
                        "symbol": "SPY",
                        "position_status": "open",
                        "open_net_quantity": 1.0,
                        "latest_mark_price": 501.0,
                        "open_market_value": 501.0,
                        "unrealized_pnl": 1.0,
                    }
                ],
            }
        },
    )
    risk = _write_json(
        tmp_path / "risk.json",
        {
            "result": {
                "paper_portfolio_risk_state_version": "paper-portfolio-risk-state-v1",
                "generated_at": "2026-06-30T10:02:00+00:00",
                "risk_state": "within_limits",
                "recommended_action": "allow_new_entries",
                "new_entries_allowed": True,
                "breach_count": 0,
                "warning_count": 0,
                "metrics": {"gross_exposure": 501.0},
            }
        },
    )
    plan = _write_json(
        tmp_path / "plan.json",
        {
            "result": {
                "execution_intent_ledger_version": "paper-execution-intent-ledger-v1",
                "generated_at": "2026-06-30T10:03:00+00:00",
                "intent_count": 1,
                "pending_paper_execution_count": 1,
                "action_counts": {"BUY": 1},
                "intents": [{"intent_id": "i1", "state": "pending_paper_execution", "symbol": "SPY", "action": "BUY", "quantity": 1, "ticket_type": "stock"}],
            }
        },
    )
    mark_quality = _write_json(
        tmp_path / "mark_quality.json",
        {
            "result": {
                "paper_cycle_watch_daemon_version": "paper-cycle-watch-daemon-v1",
                "generated_at": "2026-06-30T10:04:00+00:00",
                "executed_tick_count": 2,
                "skipped_tick_count": 0,
                "mark_refresh_quality_summary": {
                    "observed_tick_count": 2,
                    "refresh_status_counts": {"pass": 2},
                    "policy_status_counts": {"pass": 2},
                    "quality_action_counts": {"allow": 2},
                    "delayed_row_count": 0,
                    "missing_mark_count": 0,
                    "fallback_tick_count": 0,
                },
            }
        },
    )
    economic = _write_json(
        tmp_path / "economic.json",
        {"desk_version": "economic-indicator-desk-v1", "risk_bias": "neutral", "scorer_takeaways": ["steady"], "indicators": []},
    )
    market_data = _write_json(
        tmp_path / "market_data.json",
        {
            "market_data_desk_version": "market-data-specialist-desk-v1",
            "market_data_temperature_version": "market-data-temperature-v1",
            "generated_at": "2026-06-30T10:05:00+00:00",
            "status": "green",
            "temperature": {"label": "calm", "score": 0.0, "risk_flags": ["market_data_temperature_normal"]},
            "summary": {
                "status": "green",
                "temperature_label": "calm",
                "temperature_score": 0.0,
                "row_count": 3,
                "priced_count": 3,
                "live_count": 3,
                "delayed_count": 0,
                "missing_price_count": 0,
                "error_count": 0,
                "alert_count": 0,
                "risk_flags": ["market_data_temperature_normal"],
            },
            "rows": [
                {"key": "SPY", "symbol": "SPY", "group": "us_equity", "price": 520.0, "gap_pct": 0.2, "market_data_type_label": "real_time", "delayed": False, "alerts": [], "status": "live"},
                {"key": "ES", "group": "us_equity_futures", "price": 6100.0, "gap_pct": 0.1, "market_data_type_label": "real_time", "delayed": False, "alerts": [], "status": "live"},
                {"key": "VIX", "group": "volatility", "price": 15.0, "gap_pct": -0.1, "market_data_type_label": "real_time", "delayed": False, "alerts": [], "status": "live"},
            ],
        },
    )
    m232_hot_cache = _write_json(
        tmp_path / "m232_hot_cache.json",
        {
            "m232_hot_cache_version": "m232-hot-cache-v1",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "max_consumer_age_seconds": 300.0,
            "mode": "adaptive_lightweight_cache",
            "adaptive_schedule": {"tier": "idle", "next_interval_seconds": 60.0},
            "scorer_overlay_cache": {
                "paper_scorer_ready": False,
                "conditional_overlay_available": True,
                "waitlist_count": 0,
                "watchlist_count": 2,
                "blocked_count": 0,
                "withhold_reasons": {"watchlist": {"option_structure_expected_deferred_stale": 2}, "blocked": {}},
            },
            "reflex_hot_cache": {"reflex_candidate_count": 0, "memory_count": 3, "top_assets": {"SPY": 2}},
            "option_structure_cache": {"memory_count": 6, "complete_rows": 22},
            "action_gate_cache": {"memory_count": 33, "paper_entry_candidate_count": 10, "block_or_second_pass_count": 3},
            "safety": {"broker_orders_submitted": False, "transmit_enabled": False},
        },
    )
    market_data_session_overlay = _write_json(
        tmp_path / "market_data_session_overlay.json",
        {
            "market_data_session_overlay_version": "market-data-session-overlay-v1",
            "generated_at": "2026-06-30T22:15:00+00:00",
            "source_market_data_version": "market-data-specialist-desk-v1",
            "source_market_data_generated_at": "2026-06-30T22:14:30+00:00",
            "session": {
                "market_session_version": "us-market-session-v1",
                "timezone": "America/New_York",
                "now_local": "2026-06-30T18:15:00-04:00",
                "session": "after_hours",
                "is_regular_session": False,
                "is_extended_or_overnight": True,
                "reason": "Stocks/ETFs can still print extended-hours prices; listed option bid/ask retry waits for regular session.",
            },
            "next_regular_open": {
                "status": "waiting_for_next_regular_open",
                "next_open_local": "2026-07-01T09:30:00-04:00",
                "next_open_utc": "2026-07-01T13:30:00+00:00",
                "wait_hours": 15.25,
            },
            "risk_interpretation": "overnight_monitoring_active",
            "stock_cash_overnight_monitoring_enabled": True,
            "option_quotes_regular_session_dependent": True,
            "summary": {
                "source_temperature_label": "calm",
                "source_temperature_score": 0.0,
                "row_count": 2,
                "session_bucket_counts": {"near_24h_market": 1, "reference_or_regional_session_dependent": 1},
                "monitorability_counts": {"monitorable_now": 1, "monitor_if_fresh_price_present": 1},
                "monitorable_alert_instruments": ["ES"],
                "regular_dependent_alert_instruments": ["VIX"],
                "interpretation_notes": [
                    "Stock/ETF cash instruments and proxies remain monitored when IBKR provides extended-hours or overnight prices.",
                    "Listed option bid/ask quality remains regular-session dependent.",
                ],
            },
            "rows": [
                {
                    "key": "ES",
                    "group": "us_equity_futures",
                    "session_bucket": "near_24h_market",
                    "monitorability": "monitorable_now",
                    "price_present": True,
                    "alerts": ["gap_up"],
                    "status": "live",
                    "price": 6100.0,
                    "delayed": False,
                },
                {
                    "key": "VIX",
                    "group": "volatility",
                    "session_bucket": "reference_or_regional_session_dependent",
                    "monitorability": "monitor_if_fresh_price_present",
                    "price_present": True,
                    "alerts": ["volatility_reference_after_hours"],
                    "status": "live",
                    "price": 15.0,
                    "delayed": False,
                },
            ],
        },
    )
    operating_auto_gate = _write_json(
        tmp_path / "operating_auto_gate.json",
        {
            "autonomous_execution_gate_version": "market-data-operating-auto-gate-v1",
            "generated_at": "2026-06-30T10:05:30+00:00",
            "source_autonomous_execution_gate_version": "paper-autonomous-execution-gate-v1",
            "source_preview_item_count": 2,
            "auto_ready_count": 0,
            "auto_gate_status_counts": {"auto_execution_blocked": 2},
            "auto_gate_block_reason_counts": {"market_data_defensive_mode_blocks_ordinary_alpha_entries": 2},
            "market_data_operating_mode": "risk_reduction_only",
            "market_data_defensive_mode": {
                "market_data_defensive_mode_version": "market-data-defensive-operating-mode-v1",
                "mode": "risk_reduction_only",
                "severity": "red",
                "controls": {
                    "ordinary_alpha_entries_allowed": False,
                    "hedge_or_defensive_entries_allowed": False,
                    "exit_or_risk_reduction_allowed": True,
                    "protective_option_analysis_allowed": True,
                    "protective_option_ticketing_allowed": False,
                    "max_new_entry_size_multiplier": 0.0,
                    "allowed_option_structures": ["protective_put_analysis_only"],
                },
            },
            "protective_analysis_packet": {
                "candidate_count": 2,
                "ticketing_allowed": False,
                "sample_candidates": [
                    {
                        "symbol": "SPY",
                        "source_action": "BUY",
                        "analysis_status": "analysis_only",
                        "ticketing_allowed": False,
                        "reason": "market_data_defensive_mode",
                    }
                ],
                "safety": {"analysis_only": True, "ticket_created": False, "ibkr_order_submitted": False, "transmit_enabled": False},
            },
            "sample_auto_execution_items": [
                {
                    "symbol": "SPY",
                    "action": "BUY",
                    "ticket_type": "stock",
                    "auto_execution_status": "auto_execution_blocked",
                    "block_reasons": ["market_data_defensive_mode_blocks_ordinary_alpha_entries"],
                    "market_data_entry_size_multiplier": 0.0,
                }
            ],
            "safety": {"ibkr_order_submitted": False, "transmit_enabled": False},
        },
    )
    capital_state = _write_json(
        tmp_path / "capital_state.json",
        {
            "capital_margin_state_version": "m238-capital-withdrawal-guard-v1",
            "generated_at": "2026-06-30T10:05:32+00:00",
            "status": "green",
            "status_reasons": ["capital_margin_buffer_normal"],
            "leverage_mode": "cash_only",
            "selected_account": "U1",
            "metrics": {
                "net_liquidation": 1000.0,
                "total_cash_value": 500.0,
                "available_funds": 500.0,
                "buying_power": 500.0,
                "excess_liquidity": 490.0,
                "initial_margin": 0.0,
                "maintenance_margin": 10.0,
            },
            "budgets": {
                "reserve_cash": 200.0,
                "deployable_cash_after_reserve": 300.0,
                "core_risk_budget": 80.0,
                "options_premium_budget": 30.0,
                "hedge_budget": 50.0,
                "max_single_position_notional": 120.0,
                "max_new_initial_margin": 125.0,
            },
            "withdrawal_guard": {
                "status": "no_safe_withdrawal_capacity",
                "max_safe_withdrawal": 0.0,
                "theoretical_max_withdrawal_if_snapshot_fresh": 0.0,
                "requires_fresh_account_snapshot": False,
                "binding_constraints": [],
            },
            "position_counts": {"stock_position_count": 1, "option_position_count": 2},
            "action_guidance": ["Cash-only mode: avoid short stock and oversized option premiums."],
            "safety": {"read_only": True, "ibkr_order_submitted": False, "transmit_enabled": False},
        },
    )
    capital_aware_gate = _write_json(
        tmp_path / "capital_aware_gate.json",
        {
            "capital_aware_action_gate_version": "m239-capital-aware-action-sizing-gate-v1",
            "autonomous_execution_gate_version": "m239-capital-aware-action-sizing-gate-v1",
            "source_autonomous_execution_gate_version": "market-data-operating-auto-gate-v1",
            "generated_at": "2026-06-30T10:05:33+00:00",
            "source_auto_execution_item_count": 2,
            "auto_ready_count": 0,
            "capital_ready_count": 0,
            "capital_resized_count": 0,
            "capital_blocked_count": 2,
            "capital_gate_status_counts": {"capital_blocked_source_gate": 2},
            "capital_gate_block_reason_counts": {
                "market_data_defensive_mode_blocks_ordinary_alpha_entries": 2,
                "source_auto_gate_not_ready": 2,
            },
            "capital_state": {
                "version": "m238-capital-withdrawal-guard-v1",
                "generated_at": "2026-06-30T10:05:32+00:00",
                "status": "green",
                "status_reasons": ["capital_margin_buffer_normal"],
                "leverage_mode": "cash_only",
                "selected_account": "U1",
                "budgets": {
                    "deployable_cash_after_reserve": 300.0,
                    "core_risk_budget": 80.0,
                    "options_premium_budget": 30.0,
                    "max_single_position_notional": 120.0,
                    "max_new_initial_margin": 125.0,
                },
                "withdrawal_guard": {"status": "no_safe_withdrawal_capacity", "max_safe_withdrawal": 0.0, "requires_fresh_account_snapshot": False},
            },
            "initial_budget": {
                "cash_budget": 300.0,
                "new_initial_margin_budget": 125.0,
                "core_risk_budget": 80.0,
                "single_position_notional_budget": 120.0,
                "options_premium_budget": 30.0,
            },
            "used_budget": {
                "cash_budget": 0.0,
                "new_initial_margin_budget": 0.0,
                "core_risk_budget": 0.0,
                "single_position_notional_budget": 0.0,
                "options_premium_budget": 0.0,
            },
            "remaining_budget": {
                "cash_budget": 300.0,
                "new_initial_margin_budget": 125.0,
                "core_risk_budget": 80.0,
                "single_position_notional_budget": 120.0,
                "options_premium_budget": 30.0,
            },
            "sample_auto_execution_items": [
                {
                    "symbol": "SPY",
                    "action": "BUY",
                    "ticket_type": "stock",
                    "quantity": 1,
                    "capital_gate_status": "capital_blocked_source_gate",
                    "auto_execution_status": "auto_execution_blocked",
                    "capital_requirements": {
                        "cash_required": 0.0,
                        "initial_margin_required": 0.0,
                        "risk_required": 0.0,
                        "stock_notional_required": 0.0,
                        "option_premium_required": 0.0,
                    },
                    "capital_remaining_after": {
                        "cash_budget": 300.0,
                        "new_initial_margin_budget": 125.0,
                    },
                    "capital_block_reasons": [
                        "market_data_defensive_mode_blocks_ordinary_alpha_entries",
                        "source_auto_gate_not_ready",
                    ],
                    "source_gate_snapshot": {"source_auto_execution_status": "auto_execution_blocked"},
                }
            ],
            "safety": {"ibkr_order_submitted": False, "transmit_enabled": False, "live_routing_enabled": False},
        },
    )
    live_capital_validation = _write_json(
        tmp_path / "live_capital_validation.json",
        {
            "live_read_only_capital_validation_version": "m243-live-read-only-capital-validation-v1",
            "generated_at": "2026-06-30T10:05:34+00:00",
            "status": "yellow",
            "capital_state": {
                "version": "m238-capital-withdrawal-guard-v1",
                "generated_at": "2026-06-30T10:05:32+00:00",
                "status": "green",
                "status_reasons": ["capital_margin_buffer_normal"],
                "leverage_mode": "cash_only",
                "selected_account": "U1",
                "snapshot_age_seconds": 90.0,
            },
            "account_capacity": {
                "deployable_cash_after_reserve": 300.0,
                "max_new_initial_margin": 125.0,
                "core_risk_budget": 80.0,
                "max_single_position_notional": 120.0,
                "options_premium_budget": 30.0,
                "max_safe_withdrawal": 0.0,
                "requires_fresh_account_snapshot": False,
            },
            "broker_whatif": {
                "available": True,
                "safe_to_use": True,
                "response_count": 1,
                "requested_count": 1,
                "read_only_mode_detected": False,
                "generated_at": "2026-06-30T10:05:20+00:00",
                "issues": [],
            },
            "validation_item_count": 2,
            "current_size_feasible_count": 1,
            "resize_required_count": 0,
            "blocked_count": 1,
            "validation_status_counts": {
                "live_read_only_current_size_feasible": 1,
                "live_read_only_blocked": 1,
            },
            "block_reason_counts": {"cash_only_blocks_short_stock": 1},
            "sample_validation_items": [
                {
                    "symbol": "SPY",
                    "action": "BUY",
                    "ticket_type": "stock",
                    "source_quantity": 1,
                    "max_feasible_quantity": 1.2,
                    "status": "live_read_only_current_size_feasible",
                    "binding_constraint": "single_position_notional_budget",
                    "current_size_feasible": True,
                    "resize_possible": False,
                    "capital_gate_status": "capital_ready",
                    "broker_whatif_available": True,
                    "broker_whatif_status": "PreSubmitted",
                    "reasons": [],
                },
                {
                    "symbol": "QQQ",
                    "action": "SELL_SHORT",
                    "ticket_type": "stock",
                    "source_quantity": 1,
                    "max_feasible_quantity": 1.2,
                    "status": "live_read_only_blocked",
                    "binding_constraint": "single_position_notional_budget",
                    "current_size_feasible": False,
                    "resize_possible": False,
                    "capital_gate_status": "capital_ready",
                    "broker_whatif_available": False,
                    "broker_whatif_status": None,
                    "reasons": ["cash_only_blocks_short_stock"],
                },
            ],
            "safety": {
                "read_only": True,
                "whatif_request_submitted_by_m243": False,
                "ibkr_order_submitted": False,
                "transmit_enabled": False,
                "live_order_transmitted": False,
                "live_routing_enabled": False,
            },
        },
    )
    operating_ledger = _write_json(
        tmp_path / "operating_ledger.json",
        {
            "result": {
                "execution_intent_ledger_version": "paper-execution-intent-ledger-v2-portfolio-risk-gated",
                "generated_at": "2026-06-30T10:05:40+00:00",
                "source_autonomous_execution_gate_version": "market-data-operating-auto-gate-v1",
                "source_auto_ready_count": 0,
                "intent_count": 0,
                "pending_paper_execution_count": 0,
                "intents": [],
            }
        },
    )
    defensive_action_plan = _write_json(
        tmp_path / "defensive_action_plan.json",
        {
            "operating_defensive_action_plan_version": "operating-defensive-action-plan-v1",
            "generated_at": "2026-06-30T10:05:35+00:00",
            "action_count": 1,
            "operating_mode": "risk_reduction_only",
            "operating_severity": "red",
            "summary": {
                "recommended_posture": "protect_and_wait",
                "highest_priority_score": 80,
                "requires_option_chain_count": 1,
                "requires_margin_whatif_count": 1,
                "analysis_only_count": 1,
            },
            "top_actions": [
                {
                    "symbol": "SPY",
                    "action_type": "protective_option_analysis",
                    "strategy": "bull_call_debit_spread",
                    "priority_score": 80,
                    "ticketing_allowed": False,
                    "requires_option_chain": True,
                    "requires_margin_whatif": True,
                    "reason": "defined_risk_option_expression",
                    "net_debit": 1.30,
                    "max_loss": 130.0,
                    "max_profit": 870.0,
                    "reward_to_risk": 6.69,
                    "legs": [
                        {"symbol": "SPY", "expiry": "20260717", "right": "C", "strike": 505.0, "side": "long", "quantity": 1},
                        {"symbol": "SPY", "expiry": "20260717", "right": "C", "strike": 515.0, "side": "short", "quantity": -1},
                    ],
                }
            ],
        },
    )
    execution_safety_cache = _write_json(
        tmp_path / "execution_safety_cache.json",
        {
            "execution_safety_cache_supervisor_version": "execution-safety-cache-supervisor-v1",
            "generated_at": "2026-06-30T10:05:45+00:00",
            "status": "yellow",
            "cache_ready_for_all_dependent_order_paths": False,
            "blocked_order_paths": ["new_option_entries", "protective_option_ticketing_without_fresh_quotes"],
            "cache_state_counts": {"fresh": 3},
            "warnings": ["option_quote_liquidity_cache:no_fresh_option_bid_ask_quotes"],
            "violations": [],
            "cache_reports": [
                {
                    "cache_key": "option_quote_liquidity_cache",
                    "cache_state": "fresh",
                    "fresh": True,
                    "age_seconds": 10.0,
                    "max_age_seconds": 300.0,
                    "gate_action": "block_dependent_order_paths",
                    "regular_session_dependent": True,
                    "status_summary": {
                        "version": "operating-defensive-gate-retry-v2",
                        "best_option_quoted_count": 0,
                        "option_quote_deferred": True,
                        "option_quote_session": "pre_market",
                        "strict_ledger_pending_count": 1,
                        "strict_ledger_blocked_count": 2,
                    },
                    "availability_block_reasons": ["no_fresh_option_bid_ask_quotes", "option_quote_deferred_until_regular_session"],
                    "warnings": ["no_fresh_option_bid_ask_quotes"],
                    "violations": [],
                    "refresh_action": {"status": "not_needed"},
                }
            ],
        },
    )
    options_market_structure = _write_json(
        tmp_path / "options_market_structure.json",
        {
            "options_market_structure_version": "options-market-structure-desk-v1",
            "generated_at": "2026-06-30T10:05:50+00:00",
            "source_quote_snapshot_version": "ibkr-option-quote-snapshot-v1",
            "source_quote_generated_at": "2026-06-30T10:05:10+00:00",
            "quote_count": 4,
            "symbol_count": 1,
            "status": "yellow",
            "fallback_to_volume": True,
            "summary": {
                "gamma_regime_counts": {"positive_gamma_mean_reversion_bias": 1},
                "pin_risk_symbol_count": 1,
                "confidence_counts": {"medium": 1},
                "missing_open_interest_symbols": ["SPY"],
            },
            "symbols": [
                {
                    "symbol": "SPY",
                    "spot": 520.0,
                    "quote_count": 4,
                    "strike_count": 3,
                    "total_gamma_exposure": 25000.0,
                    "gamma_regime": "positive_gamma_mean_reversion_bias",
                    "gamma_flip": 505.0,
                    "call_wall": 525.0,
                    "put_wall": 515.0,
                    "max_pain": 520.0,
                    "pin_level": 520.0,
                    "distance_to_pin_pct": 0.0,
                    "pin_risk": True,
                    "confidence": "medium",
                    "warnings": ["open_interest_missing_using_volume_proxy"],
                }
            ],
            "safety": {"read_only": True, "ibkr_order_submitted": False, "transmit_enabled": False},
        },
    )
    options_market_structure_supervisor = _write_json(
        tmp_path / "options_market_structure_supervisor.json",
        {
            "options_market_structure_supervisor_version": "options-market-structure-refresh-supervisor-v9-operational-runbook-tuning",
            "run_id": "oms_supervisor_test",
            "started_at": "2026-06-30T10:06:10+00:00",
            "completed_at": "2026-06-30T10:06:11+00:00",
            "final_status": "yellow",
            "final_session": "pre_market",
            "final_option_quote_allowed": False,
            "final_quote_fresh": False,
            "final_optional_index_quote_fresh": False,
            "final_structure_status": "yellow",
            "final_optional_index_structure_status": "missing",
            "final_action_status_counts": {
                "deferred_until_regular_session": 2,
                "blocked_missing_quote_snapshot": 2,
            },
            "option_quote_deferred_operating_state": {
                "status": "normal_session_deferred",
                "alert_policy": "do_not_alert_as_failure",
                "next_regular_open": "2026-07-01T09:30:00-04:00",
            },
            "m220_operational_runbook": {
                "one_command": ".\\.venv\\Scripts\\python.exe main.py options-market-structure-supervisor --profile-json config\\options_market_structure_supervisor_profile_v1.json --execute-actions",
                "regular_session_required": True,
                "safe_to_run_now": False,
            },
            "index_family_budget_tuning_observations": [
                {
                    "family_id": "sp500",
                    "label": "S&P 500",
                    "allocated_budget": 42,
                    "selected_source_type": "none",
                    "selected_symbols": [],
                    "usable_role_symbol_count": 0,
                    "requested_role_symbol_count": 2,
                    "candidate_contracts": 0,
                    "chain_rows": 0,
                    "suggested_budget_bias": "hold_until_regular_session_validation",
                }
            ],
            "m220_family_issue_summary": {
                "version": "m220-family-issue-summary-v1",
                "issue_category_counts": {"missing_probe_rows": 1},
                "proxy_fallback_families": [],
                "coverage_unavailable_families": ["S&P 500"],
            },
            "m220_family_repair_plan": [
                {
                    "family_id": "sp500",
                    "label": "S&P 500",
                    "operating_mode": "coverage_unavailable",
                    "severity": "red",
                    "selected_source_type": "none",
                    "selected_symbols": [],
                    "issue_category_counts": {"missing_probe_rows": 1},
                    "next_action": "repair_contract_mapping_permission_or_budget_before_using_family_context",
                    "operator_summary": "S&P 500 has no usable direct, mini, or proxy coverage.",
                }
            ],
            "m220_live_validation_checklist": [
                {
                    "family": "S&P 500",
                    "role": "direct",
                    "symbols": ["SPX"],
                    "passed": False,
                    "status": "missing",
                    "issue_categories": ["missing_probe_rows"],
                    "next_action": "probe_contract_chain_or_fallback_to_next_role",
                }
            ],
            "failed_action_count": 0,
            "tick_summaries": [
                {
                    "tick_index": 1,
                    "checked_at": "2026-06-30T10:06:11+00:00",
                    "status": "yellow",
                    "session": "pre_market",
                    "option_quote_allowed": False,
                    "actions": [
                        {"action_id": "core_option_quote_snapshot", "status": "deferred_until_regular_session"},
                        {"action_id": "optional_index_option_quote_snapshot", "status": "deferred_until_regular_session"},
                    ],
                }
            ],
            "config": {
                "symbols": ["SPY", "QQQ", "IWM", "DIA"],
                "optional_index_symbols": ["SPX", "NDX", "RUT", "VIX"],
                "underlying_sec_types": {"SPX": "IND", "NDX": "IND", "RUT": "IND", "VIX": "IND"},
                "underlying_exchanges": {"SPX": "CBOE", "NDX": "NASDAQ", "RUT": "CBOE", "VIX": "CBOE"},
            },
            "safety": {"read_only": True, "ibkr_order_submitted": False, "transmit_enabled": False},
        },
    )
    evidence_freshness = _write_json(
        tmp_path / "evidence_freshness.json",
        {
            "evidence_freshness_desk_version": "evidence-freshness-desk-v1",
            "scorer_evidence_gap_guard_version": "scorer-evidence-gap-guard-v1",
            "generated_at": "2026-06-30T10:06:00+00:00",
            "overall_status": "green",
            "status_counts": {"fresh": 6, "lagging": 0, "stale": 0, "offline": 0},
            "stale_source_keys": [],
            "lagging_source_keys": [],
            "evidence_gap_warning": False,
            "source_freshness_weights": {"market_news": 1.0},
            "scorer_guard": {"action": "normal", "warnings": [], "weights": {"market_news": 1.0}},
            "sources": [
                {
                    "source_key": "market_news",
                    "label": "Market / breaking news",
                    "latest_event_at": "2026-06-30T10:05:00+00:00",
                    "age_minutes": 1.0,
                    "status": "fresh",
                    "scorer_usability": "normal",
                    "scorer_weight": 1.0,
                }
            ],
        },
    )
    evidence_watchdog = _write_json(
        tmp_path / "evidence_watchdog.json",
        {
            "evidence_refresh_watchdog_version": "evidence-refresh-watchdog-v1",
            "run_id": "watchdog_test",
            "started_at": "2026-06-30T10:06:30+00:00",
            "updated_at": "2026-06-30T10:06:40+00:00",
            "status": "healthy_no_refresh_needed",
            "last_tick_index": 1,
            "last_refresh_status": "succeeded",
            "last_refresh_queue_status_counts": {"succeeded": 3},
            "last_freshness_status": "green",
            "last_stale_source_keys": [],
            "last_lagging_source_keys": [],
            "consecutive_failures": 0,
            "next_refresh_eligible_at": "2026-06-30T10:11:40+00:00",
            "tick_summaries": [
                {
                    "tick_index": 1,
                    "status": "healthy_no_refresh_needed",
                    "freshness_status": "green",
                    "stale_source_keys": [],
                    "lagging_source_keys": [],
                    "cooldown_remaining_seconds": 0,
                }
            ],
        },
    )
    source_supervisor = _write_json(
        tmp_path / "source_supervisor.json",
        {
            "source_online_supervisor_version": "source-online-supervisor-v5",
            "run_id": "source_supervisor_test",
            "started_at": "2026-06-30T10:06:50+00:00",
            "completed_at": "2026-06-30T10:06:55+00:00",
            "executed_tick_count": 1,
            "max_ticks": 1,
            "interval_seconds": 1.0,
            "sleep_between_ticks": False,
            "final_status": "green",
            "final_action_status_counts": {"succeeded": 3},
            "final_live_validation_status": "green",
            "final_source_ingest_health_status": "green",
            "final_online_problem_source_keys": [],
            "tick_summaries": [
                {
                    "source_online_supervisor_version": "source-online-supervisor-v5",
                    "run_id": "source_supervisor_test",
                    "tick_index": 1,
                    "checked_at": "2026-06-30T10:06:55+00:00",
                    "status": "green",
                    "execute_actions": True,
                    "action_status_counts": {"succeeded": 3},
                    "live_validation_status": "green",
                    "live_validation_source_status": {"market_news": "ok", "market_data": "ok"},
                    "source_ingest_health_status": "green",
                    "online_problem_source_keys": [],
                    "degraded_source_keys": [],
                    "root_cause_counts": {},
                    "actions": [
                        {"action_id": "source_live_validation_ibkr", "source_key": "market_news", "status": "succeeded", "priority": 1, "description": "Validate IBKR/Benzinga news.", "details": {"next_eligible_at": "2026-06-30T10:07:00+00:00"}},
                        {"action_id": "market_news_ibkr_catchup", "source_key": "market_news", "status": "succeeded", "priority": 20, "description": "Catch up IBKR/Benzinga news."},
                        {"action_id": "external_market_sync_marketaux", "source_key": "market_news", "status": "succeeded", "priority": 30, "description": "Sync Marketaux."},
                    ],
                }
            ],
        },
    )
    strategy_allocation = _write_json(
        tmp_path / "strategy_allocation.json",
        {
            "strategy_allocation_engine_version": "m241-strategy-allocation-engine-v1",
            "generated_at": "2026-06-30T10:07:00+00:00",
            "mode": "paper_read_only_strategy_selection",
            "summary": {
                "candidate_count": 2,
                "recommendation_count": 2,
                "paper_trade_ready_count": 1,
                "hedge_only_count": 0,
                "blocked_count": 1,
                "recommendation_counts": {"defined_risk_spread": 1, "block": 1},
                "expression_counts": {"bull_call_debit_spread": 1, "blocked": 1},
                "confidence_counts": {"medium": 1, "low": 1},
                "top_reason_counts": {"market_gate_blocks_ordinary_alpha": 1},
            },
            "contexts": {
                "capital": {"status": "green", "leverage_mode": "cash_only"},
                "market": {"risk_gate_action": "allow_new_entries"},
                "options": {"quote_usable": True, "quote_quality_status": "green"},
                "scorer": {"live_scorer_status": "collecting"},
                "news": {"latest_news_count": 1, "candidate_symbols": ["SPY"]},
            },
            "recommendations": [
                {
                    "symbol": "SPY",
                    "direction": "bullish",
                    "recommendation": "defined_risk_spread",
                    "selected_expression": "bull_call_debit_spread",
                    "intent_role": "ordinary_alpha",
                    "confidence": "medium",
                    "capital_gate_status": "capital_ready",
                    "reasons": [],
                    "option_structure": {"gamma_regime": "positive_gamma_mean_reversion_bias"},
                    "market_gate": {"risk_gate_action": "allow_new_entries"},
                    "safety": {"paper_recommendation_only": True, "ibkr_order_submitted": False, "transmit_enabled": False},
                },
                {
                    "symbol": "QQQ",
                    "direction": "bearish",
                    "recommendation": "block",
                    "selected_expression": "blocked",
                    "intent_role": "ordinary_alpha",
                    "confidence": "low",
                    "capital_gate_status": "capital_blocked_source_gate",
                    "reasons": ["market_gate_blocks_ordinary_alpha"],
                    "option_structure": {},
                    "market_gate": {"risk_gate_action": "block_new_entries"},
                    "safety": {"paper_recommendation_only": True, "ibkr_order_submitted": False, "transmit_enabled": False},
                },
            ],
            "latest_news": [],
            "safety": {"paper_recommendation_only": True, "ibkr_order_submitted": False, "transmit_enabled": False},
        },
    )
    db_path = tmp_path / "market_events.db"
    con = sqlite3.connect(db_path)
    now_iso = datetime.now(timezone.utc).isoformat()
    con.execute("CREATE TABLE news_articles (provider TEXT, headline TEXT, primary_symbol TEXT, published_at TEXT, received_at TEXT, provider_published_at TEXT, created_at TEXT)")
    con.execute(
        """
        CREATE TABLE external_market_signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT,
            external_id TEXT,
            title TEXT,
            published_at TEXT,
            region TEXT,
            theme TEXT,
            sentiment_score REAL,
            sentiment_label TEXT,
            confidence REAL,
            assets_json TEXT,
            market_reaction_status TEXT,
            prediction_gate_status TEXT,
            raw_json TEXT,
            created_at TEXT,
            updated_at TEXT
        )
        """
    )
    con.execute("CREATE TABLE standing_market_regimes (regime_key TEXT, family TEXT, regime_name TEXT, directional_bias TEXT, status TEXT, region TEXT, theme TEXT, started_at TEXT, last_seen_at TEXT, resolved_at TEXT, severity_score REAL, confidence REAL, affected_assets_json TEXT, evidence_count INTEGER, latest_titles_json TEXT)")
    con.execute("INSERT INTO news_articles VALUES (?,?,?,?,?,?,?)", ("BZ", "SPY rallies on test headline", "SPY", now_iso, None, None, now_iso))
    con.execute(
        """
        INSERT INTO external_market_signals(
            source,external_id,title,published_at,region,theme,sentiment_score,
            sentiment_label,confidence,assets_json,market_reaction_status,
            prediction_gate_status,raw_json,created_at,updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "marketaux",
            "geo-1",
            "Missile strike near Hormuz raises oil supply risk",
            now_iso,
            "middle_east",
            "energy_supply",
            -0.8,
            "negative",
            0.9,
            '["USO","GLD","SPY"]',
            "available",
            "eligible",
            '{"urgency":{"urgency":"high"}}',
            now_iso,
            now_iso,
        ),
    )
    con.execute("INSERT INTO standing_market_regimes VALUES ('ai_boom','technology','AI boom','positive','active','US','AI','2025-01-01','2026-06-30',NULL,0.8,0.9,'[]',10,'[]')")
    con.commit()
    con.close()

    snapshot = build_trading_dashboard_snapshot(
        account_snapshot_path=account,
        paper_portfolio_state_path=portfolio,
        portfolio_risk_state_path=risk,
        execution_intent_ledger_path=plan,
        mark_quality_daemon_path=mark_quality,
        economic_desk_path=economic,
        market_data_path=market_data,
        market_data_session_overlay_path=market_data_session_overlay,
        market_data_operating_auto_gate_path=operating_auto_gate,
        capital_margin_state_path=capital_state,
        capital_aware_action_gate_path=capital_aware_gate,
        live_read_only_capital_validation_path=live_capital_validation,
        market_data_operating_execution_intent_ledger_path=operating_ledger,
        operating_defensive_action_plan_path=defensive_action_plan,
        database_path=db_path,
        evidence_freshness_path=evidence_freshness,
        evidence_refresh_watchdog_path=evidence_watchdog,
        source_online_supervisor_path=source_supervisor,
        m232_hot_cache_path=m232_hot_cache,
        execution_safety_cache_supervisor_path=execution_safety_cache,
        options_market_structure_path=options_market_structure,
        options_market_structure_supervisor_path=options_market_structure_supervisor,
        strategy_allocation_engine_path=strategy_allocation,
        news_limit=3,
        regime_limit=3,
        mode="paper",
    )
    report = trading_dashboard_markdown(snapshot)
    html = trading_dashboard_html(snapshot, refresh_seconds=5, snapshot_json_path=tmp_path / "snapshot.json")

    assert snapshot["dashboard_snapshot_version"] == "trading-dashboard-snapshot-v1"
    assert snapshot["status"]["color"] == "red"
    assert snapshot["action_queue"]["status_color"] == snapshot["status"]["display_color"]
    assert snapshot["action_queue"]["raw_status_color"] == snapshot["status"]["raw_color"]
    assert "market_data_operating_mode_blocks_alpha_entries" in snapshot["status"]["reasons"]
    assert snapshot["ai_opinion"]["stance"] == "do_not_add_new_risk"
    assert snapshot["account"]["buying_power"] == 500.0
    assert snapshot["positions"]["paper_positions"][0]["symbol"] == "SPY"
    assert snapshot["buy_sell_plan"]["pending_paper_execution_count"] == 1
    assert snapshot["news_ticker"][0]["headline"] == "SPY rallies on test headline"
    assert snapshot["geopolitical_desk"]["available"] is True
    assert snapshot["geopolitical_desk"]["top_buckets"][0]["region"] == "middle_east"
    assert snapshot["standing_regimes"][0]["regime_key"] == "ai_boom"
    assert snapshot["market_data"]["market_data_desk"]["temperature"]["label"] == "calm"
    assert snapshot["market_data"]["market_data_session_overlay"]["session_name"] == "after_hours"
    assert snapshot["market_data"]["market_data_session_overlay"]["stock_cash_overnight_monitoring_enabled"] is True
    assert snapshot["market_data"]["market_data_session_overlay"]["option_quotes_regular_session_dependent"] is True
    assert snapshot["market_data"]["market_data_session_overlay"]["monitorability_counts"]["monitorable_now"] == 1
    assert snapshot["market_data"]["market_data_operating_auto_gate"]["operating_mode"] == "risk_reduction_only"
    assert snapshot["market_data"]["market_data_operating_auto_gate"]["auto_ready_count"] == 0
    assert snapshot["market_data"]["market_data_operating_auto_gate"]["protective_analysis_packet"]["candidate_count"] == 2
    assert snapshot["capital_margin_state"]["leverage_mode"] == "cash_only"
    assert snapshot["capital_margin_state"]["budgets"]["options_premium_budget"] == 30.0
    assert snapshot["capital_aware_action_gate"]["capital_blocked_count"] == 2
    assert snapshot["capital_aware_action_gate"]["sample_auto_execution_items"][0]["status"] == "capital_blocked_source_gate"
    assert snapshot["live_read_only_capital_validation"]["status"] == "yellow"
    assert snapshot["live_read_only_capital_validation"]["blocked_count"] == 1
    assert snapshot["live_read_only_capital_validation"]["sample_validation_items"][1]["reasons"] == ["cash_only_blocks_short_stock"]
    assert snapshot["strategy_allocation_engine"]["paper_trade_ready_count"] == 1
    assert snapshot["strategy_allocation_engine"]["expression_counts"]["bull_call_debit_spread"] == 1
    assert snapshot["market_data"]["market_data_operating_execution_intent_ledger"]["pending_paper_execution_count"] == 0
    assert snapshot["options_desk"]["account_option_position_count"] == 2
    assert snapshot["options_desk"]["position_strategy_groups"][0]["structure"] == "vertical_spread"
    assert snapshot["options_desk"]["position_risk_summary"]["short_itm_count"] == 1
    assert snapshot["options_desk"]["position_risk_summary"]["yellow_count"] >= 1
    assert "short_option_assignment_risk" in snapshot["options_desk"]["position_strategy_groups"][0]["risk_alerts"]
    assert any(row["position_risk_level"] in {"yellow", "red"} for row in snapshot["options_desk"]["account_option_positions"])
    assert snapshot["options_desk"]["advanced_strategies"][0]["leg_count"] == 2
    assert snapshot["options_desk"]["advanced_strategies"][0]["risk"]["max_loss"] == 130.0
    assert snapshot["options_desk"]["readiness_level"] == "blocked"
    assert snapshot["options_desk"]["readiness_score"] <= 40
    assert snapshot["options_desk"]["quote_gate"]["best_option_quoted_count"] == 0
    assert "safety_cache_blocks_new_option_entries" in snapshot["options_desk"]["blocked_order_paths"]
    assert snapshot["options_market_structure"]["status"] == "yellow"
    assert snapshot["options_market_structure"]["summary"]["pin_risk_symbol_count"] == 1
    assert snapshot["options_market_structure"]["symbols"][0]["call_wall"] == 525.0
    assert snapshot["options_market_structure_supervisor"]["final_status"] == "yellow"
    assert snapshot["options_market_structure_supervisor"]["config"]["optional_index_symbols"] == ["SPX", "NDX", "RUT", "VIX"]
    assert snapshot["m220_market_move_option_structure_overlay"]["mapped_move_count"] >= 1
    assert snapshot["m220_market_move_option_structure_overlay"]["rows"][0]["market_key"] == "SPY"
    assert snapshot["m220_market_move_option_structure_overlay"]["rows"][0]["matched_option_symbol"] == "SPY"
    assert snapshot["evidence_freshness"]["overall_status"] == "green"
    assert snapshot["evidence_auto_recovery"]["status"] == "healthy_no_refresh_needed"
    assert snapshot["source_online_supervisor"]["final_status"] == "green"
    assert snapshot["source_online_supervisor"]["started_at_local"]
    assert snapshot["source_online_supervisor"]["latest_tick"]["checked_at_local"]
    assert snapshot["m232_hot_cache"]["usable"] is True
    assert snapshot["m232_hot_cache"]["conditional_overlay_available"] is True
    assert snapshot["learning_pipeline"]["m232_hot_cache_status"] == "fresh"
    assert any(menu["id"] == "positions" for menu in snapshot["menus"])
    assert any(menu["id"] == "source_supervisor" for menu in snapshot["menus"])
    assert any(menu["id"] == "m232_control_plane" for menu in snapshot["menus"])
    assert any(menu["id"] == "strategy_allocation" for menu in snapshot["menus"])
    assert any(menu["id"] == "live_capital_validation" for menu in snapshot["menus"])
    assert "Trading Command Center Snapshot" in report
    assert "M232 Live Learning Control Plane" in report
    assert "Trading Command Center" in html
    assert "M232 Control Plane" in html
    assert "Live Read-Only Capital Validation" in html
    assert "cash_only_blocks_short_stock" in html
    assert "Latest tick local" in html
    assert "Next eligible local" in html
    assert "status-red" in html
    assert "AI opinion" in html
    assert "local-time" in html
    assert "applyLocalTimes" in html
    assert "lamp-reasons" in html
    assert "flex: 0 0 clamp(360px, 38vw, 620px)" in html
    assert "height: clamp(78px, 5.2vw, 96px)" in html
    assert "trading-command-center-ui-state" in html
    assert 'http-equiv="refresh"' not in html
    assert "refreshDashboardSections" in html
    assert "X-Dashboard-Partial-Refresh" in html
    assert "applyResponsiveDensity" in html
    assert "density-compact" in html
    assert "density-wide" in html
    assert "table-wrap" in html
    assert "table-shell" in html
    assert "initializeDashboardTables" in html
    assert "table-page-next" in html
    assert "showing ${startRow}-${endRow}" in html
    assert "max-height: min(48vh, 520px)" in html
    assert "@media (max-width: 1450px)" in html
    assert "restoreScrollPosition" in html
    assert "restoreDetailsState" in html
    assert "Market temperature" in html
    assert "Market Data Session Overlay" in report
    assert "Market Data Session Overlay" in html
    assert "Market Data Session monitorability" in html
    assert "Operating Gate" in html
    assert "Capital-Aware Gate" in html
    assert "Capital-aware gate" in report
    assert "Strategy Allocation Engine" in html
    assert "Strategy allocation" in report
    assert "cash_only" in html
    assert "Options Desk" in report
    assert "Options Desk" in html
    assert "Options Market Structure" in report
    assert "Options Market Structure" in html
    assert "M220 one-command" in report
    assert "M220 one-command" in html
    assert "M220 market move option explanation" in report
    assert "M220 market move option explanation" in html
    assert "M220 family repair plan" in html
    assert "coverage_unavailable" in html
    assert "missing_probe_rows" in html
    assert "Family budget tuning observations" in html
    assert "<details open>" in html
    assert "do_not_alert_as_failure" in html
    assert "optional index" in report
    assert "SPX" in html
    assert "positive_gamma_mean_reversion_bias" in html
    assert "short_option_assignment_risk" in html
    assert "Readiness checks" in html
    assert "no_fresh_option_bid_ask_quotes" in html
    assert "vertical_spread" in html
    assert "bull_call_debit_spread" in html
    assert "Protective option analysis packet" in html
    assert "Evidence freshness" in html
    assert "Auto-recovery watchdog" in html
    assert "Source Online Supervisor" in html
    assert "Geopolitical Desk" in html
    assert "Geopolitical Desk" in report
    assert "Market Data Desk watchlist" in html
    assert "SPY rallies on test headline" in html
    assert "trading-dashboard-html-v1" in html

    loop_result = run_trading_dashboard_loop(
        account_snapshot_path=account,
        paper_portfolio_state_path=portfolio,
        portfolio_risk_state_path=risk,
        execution_intent_ledger_path=plan,
        mark_quality_daemon_path=mark_quality,
        economic_desk_path=economic,
        market_data_path=market_data,
        market_data_session_overlay_path=market_data_session_overlay,
        market_data_operating_auto_gate_path=operating_auto_gate,
        capital_margin_state_path=capital_state,
        capital_aware_action_gate_path=capital_aware_gate,
        market_data_operating_execution_intent_ledger_path=operating_ledger,
        database_path=db_path,
        evidence_freshness_path=evidence_freshness,
        evidence_refresh_watchdog_path=evidence_watchdog,
        source_online_supervisor_path=source_supervisor,
        options_market_structure_supervisor_path=options_market_structure_supervisor,
        strategy_allocation_engine_path=strategy_allocation,
        snapshot_json_output_path=tmp_path / "dashboard.json",
        markdown_output_path=tmp_path / "dashboard.md",
        html_output_path=tmp_path / "dashboard.html",
        tick_log_jsonl_path=tmp_path / "dashboard_ticks.jsonl",
        summary_json_output_path=tmp_path / "dashboard_loop_summary.json",
        max_ticks=2,
        interval_seconds=1.0,
        sleep_between_ticks=False,
        refresh_seconds=1,
        run_id="test_dashboard_loop",
    )
    loop_report = trading_dashboard_loop_markdown(loop_result)
    tick_rows = (tmp_path / "dashboard_ticks.jsonl").read_text(encoding="utf-8").strip().splitlines()

    assert loop_result["dashboard_loop_version"] == "trading-dashboard-loop-v1"
    assert loop_result["executed_tick_count"] == 2
    assert loop_result["final_status"]["color"] == "red"
    assert loop_result["tick_summaries"][0]["source_online_supervisor_status"] == "green"
    assert loop_result["tick_summaries"][0]["market_data_operating_mode"] == "risk_reduction_only"
    assert loop_result["tick_summaries"][0]["market_data_session"] == "after_hours"
    assert loop_result["tick_summaries"][0]["market_data_stock_cash_overnight_monitoring_enabled"] is True
    assert loop_result["tick_summaries"][0]["market_data_option_quotes_regular_session_dependent"] is True
    assert loop_result["tick_summaries"][0]["market_data_operating_auto_ready_count"] == 0
    assert loop_result["tick_summaries"][0]["capital_leverage_mode"] == "cash_only"
    assert loop_result["tick_summaries"][0]["capital_aware_blocked_count"] == 2
    assert loop_result["tick_summaries"][0]["market_data_operating_protective_candidate_count"] == 2
    assert loop_result["final_ai_stance"] == "do_not_add_new_risk"
    assert (tmp_path / "dashboard.json").exists()
    assert (tmp_path / "dashboard.md").exists()
    assert (tmp_path / "dashboard.html").exists()
    assert (tmp_path / "dashboard_loop_summary.json").exists()
    assert len(tick_rows) == 2
    assert "Trading Command Center Loop Summary" in loop_report

    def refresh_watchdog_before_snapshot(tick_index: int, loop_run_id: str) -> dict:
        _write_json(
            evidence_watchdog,
            {
                "evidence_refresh_watchdog_version": "evidence-refresh-watchdog-v1",
                "run_id": f"{loop_run_id}_tick_{tick_index}_watchdog",
                "finished_at": "2026-06-30T10:07:00+00:00",
                "executed_tick_count": 1,
                "final_status": "refresh_completed_gap_remains",
                "final_freshness_status": "watch",
                "final_stale_source_keys": [],
                "final_lagging_source_keys": ["market_news"],
                "consecutive_failures": 0,
                "next_refresh_eligible_at": "2026-06-30T10:12:00+00:00",
            },
        )
        return {
            "enabled": True,
            "applied": True,
            "status": "refresh_completed_gap_remains",
            "freshness_status": "watch",
            "lagging_source_keys": ["market_news"],
        }

    pre_snapshot_loop_result = run_trading_dashboard_loop(
        account_snapshot_path=account,
        paper_portfolio_state_path=portfolio,
        portfolio_risk_state_path=risk,
        execution_intent_ledger_path=plan,
        mark_quality_daemon_path=mark_quality,
        economic_desk_path=economic,
        market_data_path=market_data,
        database_path=db_path,
        evidence_freshness_path=evidence_freshness,
        evidence_refresh_watchdog_path=evidence_watchdog,
        snapshot_json_output_path=tmp_path / "pre_snapshot_dashboard.json",
        markdown_output_path=tmp_path / "pre_snapshot_dashboard.md",
        html_output_path=tmp_path / "pre_snapshot_dashboard.html",
        tick_log_jsonl_path=tmp_path / "pre_snapshot_dashboard_ticks.jsonl",
        summary_json_output_path=tmp_path / "pre_snapshot_dashboard_loop_summary.json",
        max_ticks=1,
        interval_seconds=1.0,
        sleep_between_ticks=False,
        refresh_seconds=1,
        alerts_enabled=False,
        pre_snapshot_hook=refresh_watchdog_before_snapshot,
        run_id="test_pre_snapshot_refresh_loop",
    )
    pre_snapshot = json.loads((tmp_path / "pre_snapshot_dashboard.json").read_text(encoding="utf-8"))

    assert pre_snapshot_loop_result["pre_snapshot_refresh_enabled"] is True
    assert pre_snapshot_loop_result["pre_snapshot_refresh_summaries"][0]["status"] == "refresh_completed_gap_remains"
    assert pre_snapshot_loop_result["tick_summaries"][0]["pre_snapshot_refresh"]["freshness_status"] == "watch"
    assert pre_snapshot["evidence_auto_recovery"]["status"] == "refresh_completed_gap_remains"
    assert pre_snapshot["evidence_auto_recovery"]["last_lagging_source_keys"] == ["market_news"]

    warning_snapshot = dict(snapshot)
    warning_snapshot["status"] = {"color": "yellow", "reasons": ["buying_power_below_100"]}
    warning_snapshot["account"] = dict(snapshot["account"])
    warning_snapshot["account"]["buying_power"] = 95.0
    warning_snapshot["account"]["available_funds"] = 95.0
    state, first_alerts = evaluate_dashboard_alerts(
        warning_snapshot,
        {},
        now="2026-06-30T10:10:00+00:00",
        run_id="test_alerts",
        tick_index=1,
    )
    state, repeat_alerts = evaluate_dashboard_alerts(
        warning_snapshot,
        state,
        now="2026-06-30T10:10:01+00:00",
        run_id="test_alerts",
        tick_index=2,
    )
    worse_snapshot = dict(warning_snapshot)
    worse_snapshot["account"] = dict(warning_snapshot["account"])
    worse_snapshot["account"]["buying_power"] = 70.0
    worse_snapshot["account"]["available_funds"] = 70.0
    state, worse_alerts = evaluate_dashboard_alerts(
        worse_snapshot,
        state,
        now="2026-06-30T10:10:02+00:00",
        run_id="test_alerts",
        tick_index=3,
    )

    assert {alert["condition_key"] for alert in first_alerts} >= {
        "dashboard_status_non_green",
        "account_buying_power_below_threshold",
    }
    assert repeat_alerts == []
    assert {alert["condition_key"] for alert in worse_alerts} >= {
        "account_buying_power_below_threshold",
        "account_available_funds_below_threshold",
    }
    assert all(alert["event_type"] == "worsened_magnitude" for alert in worse_alerts)

    telegram_dry_run = deliver_dashboard_alerts_to_telegram(
        first_alerts,
        bot_token="test-token",
        chat_id="12345",
        title="Test Dashboard",
        status_color="yellow",
        run_id="test_alerts",
        tick_index=1,
        dry_run=True,
    )
    telegram_missing = deliver_dashboard_alerts_to_telegram(
        first_alerts,
        bot_token=None,
        chat_id=None,
        title="Test Dashboard",
        status_color="yellow",
        run_id="test_alerts",
        tick_index=1,
    )

    assert telegram_dry_run["dashboard_telegram_version"] == "trading-dashboard-telegram-alert-v1"
    assert telegram_dry_run["status"] == "dry_run_not_sent"
    assert telegram_dry_run["alert_count"] == len(first_alerts)
    assert "test-token" not in json.dumps(telegram_dry_run)
    assert telegram_missing["status"] == "skipped_missing_credentials"

    telegram_loop_result = run_trading_dashboard_loop(
        account_snapshot_path=account,
        paper_portfolio_state_path=portfolio,
        portfolio_risk_state_path=risk,
        execution_intent_ledger_path=plan,
        mark_quality_daemon_path=mark_quality,
        economic_desk_path=economic,
        market_data_path=None,
        database_path=db_path,
        snapshot_json_output_path=tmp_path / "telegram_dashboard.json",
        markdown_output_path=tmp_path / "telegram_dashboard.md",
        html_output_path=tmp_path / "telegram_dashboard.html",
        tick_log_jsonl_path=tmp_path / "telegram_dashboard_ticks.jsonl",
        alert_state_json_path=tmp_path / "telegram_alert_state.json",
        alert_events_jsonl_path=tmp_path / "telegram_alert_events.jsonl",
        telegram_delivery_jsonl_path=tmp_path / "telegram_deliveries.jsonl",
        max_ticks=1,
        interval_seconds=1.0,
        sleep_between_ticks=False,
        refresh_seconds=1,
        alerts_enabled=True,
        alert_buying_power_threshold=600.0,
        alert_available_funds_threshold=600.0,
        telegram_enabled=True,
        telegram_bot_token="test-token",
        telegram_chat_id="12345",
        telegram_dry_run=True,
        run_id="test_telegram_loop",
    )

    assert telegram_loop_result["telegram_delivery_count"] == 1
    assert telegram_loop_result["telegram_deliveries"][0]["status"] == "dry_run_not_sent"
    assert (tmp_path / "telegram_deliveries.jsonl").exists()


def test_trading_dashboard_alerts_once_for_m157_watchdog_gap_then_realerts_on_worsening() -> None:
    snapshot = {
        "status": {"color": "yellow", "reasons": ["evidence_auto_recovery_gap_remains_after_refresh"]},
        "account": {"buying_power": 1000.0, "available_funds": 1000.0},
        "risk": {"risk_state": "within_limits"},
        "market_data": {"mark_refresh_quality": {"quality": {}}, "market_data_desk": {"summary": {}}},
        "evidence_freshness": {
            "overall_status": "watch",
            "stale_source_keys": [],
            "lagging_source_keys": ["market_news"],
            "scorer_guard": {"action": "downweight_stale_sources"},
        },
        "evidence_auto_recovery": {
            "status": "refresh_completed_gap_remains",
            "last_freshness_status": "watch",
            "last_stale_source_keys": [],
            "last_lagging_source_keys": ["market_news"],
            "consecutive_failures": 0,
            "next_refresh_eligible_at": "2026-06-30T10:15:00+00:00",
            "last_refresh_queue_status_counts": {"succeeded": 4},
        },
        "missing_sources": [],
    }
    state, first_alerts = evaluate_dashboard_alerts(
        snapshot,
        {},
        now="2026-06-30T10:10:00+00:00",
        run_id="m158_alert_test",
        tick_index=1,
    )
    state, repeat_alerts = evaluate_dashboard_alerts(
        snapshot,
        state,
        now="2026-06-30T10:10:01+00:00",
        run_id="m158_alert_test",
        tick_index=2,
    )
    cooldown_snapshot = dict(snapshot)
    cooldown_snapshot["evidence_auto_recovery"] = dict(snapshot["evidence_auto_recovery"])
    cooldown_snapshot["evidence_auto_recovery"]["status"] = "skipped_cooldown"
    state, cooldown_alerts = evaluate_dashboard_alerts(
        cooldown_snapshot,
        state,
        now="2026-06-30T10:10:01.500000+00:00",
        run_id="m158_alert_test",
        tick_index=3,
    )
    worse_snapshot = dict(snapshot)
    worse_snapshot["evidence_auto_recovery"] = dict(snapshot["evidence_auto_recovery"])
    worse_snapshot["evidence_auto_recovery"]["last_stale_source_keys"] = ["market_data"]
    worse_snapshot["evidence_auto_recovery"]["last_lagging_source_keys"] = ["market_news", "geopolitical_signals"]
    state, worse_alerts = evaluate_dashboard_alerts(
        worse_snapshot,
        state,
        now="2026-06-30T10:10:02+00:00",
        run_id="m158_alert_test",
        tick_index=4,
    )

    first_keys = {alert["condition_key"] for alert in first_alerts}
    worse_by_key = {alert["condition_key"]: alert for alert in worse_alerts}
    assert "evidence_auto_recovery_gap_remains_after_refresh" in first_keys
    assert repeat_alerts == []
    assert cooldown_alerts == []
    assert worse_by_key["evidence_auto_recovery_gap_remains_after_refresh"]["event_type"] == "worsened_severity"


def test_trading_dashboard_suppresses_market_data_flap_realert_unless_worse() -> None:
    base_snapshot = {
        "status": {"color": "green", "reasons": []},
        "account": {"buying_power": 1000.0, "available_funds": 1000.0},
        "risk": {"risk_state": "within_limits"},
        "market_data": {
            "mark_refresh_quality": {"quality": {}},
            "market_data_desk": {"status": "green", "summary": {}},
        },
        "evidence_freshness": {},
        "evidence_auto_recovery": {},
        "source_ingest_health": {},
        "source_online_supervisor": {},
        "missing_sources": [],
    }
    missing_snapshot = dict(base_snapshot)
    missing_snapshot["market_data"] = {
        "mark_refresh_quality": {"quality": {}},
        "market_data_desk": {
            "status": "yellow",
            "summary": {
                "missing_price_count": 3,
                "missing_instruments": ["VIX", "VVIX", "KOSPI"],
            },
        },
    }
    state, first_alerts = evaluate_dashboard_alerts(
        missing_snapshot,
        {},
        now="2026-07-01T11:35:00+00:00",
        run_id="market_data_flap_test",
        tick_index=1,
    )
    state, resolved_alerts = evaluate_dashboard_alerts(
        base_snapshot,
        state,
        now="2026-07-01T11:35:10+00:00",
        run_id="market_data_flap_test",
        tick_index=2,
    )
    state, flap_alerts = evaluate_dashboard_alerts(
        missing_snapshot,
        state,
        now="2026-07-01T11:35:20+00:00",
        run_id="market_data_flap_test",
        tick_index=3,
    )
    worse_snapshot = dict(base_snapshot)
    worse_snapshot["market_data"] = {
        "mark_refresh_quality": {"quality": {}},
        "market_data_desk": {
            "status": "red",
            "summary": {
                "missing_price_count": 7,
                "missing_instruments": ["VIX", "VVIX", "KOSPI", "ES", "NQ", "YM", "RTY"],
            },
        },
    }
    state, worse_alerts = evaluate_dashboard_alerts(
        worse_snapshot,
        state,
        now="2026-07-01T11:35:30+00:00",
        run_id="market_data_flap_test",
        tick_index=4,
    )

    first_by_key = {alert["condition_key"]: alert for alert in first_alerts}
    worse_by_key = {alert["condition_key"]: alert for alert in worse_alerts}
    assert "market_data_missing_prices" in first_by_key
    assert resolved_alerts == []
    assert "market_data_missing_prices" not in {alert["condition_key"] for alert in flap_alerts}
    assert worse_by_key["market_data_missing_prices"]["event_type"] == "worsened_severity"


def test_trading_dashboard_suppresses_resolved_stale_auto_recovery_gap() -> None:
    evidence_freshness = {
        "overall_status": "green",
        "stale_source_keys": [],
        "lagging_source_keys": [],
        "evidence_gap_warning": False,
    }
    evidence_auto_recovery = {
        "status": "refresh_completed_gap_remains",
        "last_freshness_status": "watch",
        "last_stale_source_keys": [],
        "last_lagging_source_keys": ["market_news"],
        "consecutive_failures": 0,
        "gap_remains_after_refresh": True,
    }
    source_ingest_health = {"overall_status": "green", "online_problem_source_keys": [], "degraded_source_keys": []}
    source_online_supervisor = {
        "final_status": "yellow",
        "final_source_ingest_health_status": "green",
        "final_online_problem_source_keys": [],
        "failed_actions": [],
    }
    status = _status_lamp(
        risk={"risk_state": "within_limits", "new_entries_allowed": True},
        mark_quality={"quality": {}},
        account={"buying_power": 1000.0, "available_funds": 1000.0},
        market_data={"summary": {"status": "green"}},
        evidence_freshness=evidence_freshness,
        evidence_auto_recovery=evidence_auto_recovery,
        source_ingest_health=source_ingest_health,
        source_online_supervisor=source_online_supervisor,
    )
    snapshot = {
        "status": status,
        "account": {"buying_power": 1000.0, "available_funds": 1000.0},
        "risk": {"risk_state": "within_limits"},
        "market_data": {"mark_refresh_quality": {"quality": {}}, "market_data_desk": {"summary": {}}},
        "evidence_freshness": evidence_freshness,
        "evidence_auto_recovery": evidence_auto_recovery,
        "source_ingest_health": source_ingest_health,
        "source_online_supervisor": source_online_supervisor,
        "missing_sources": [],
    }

    _, alerts = evaluate_dashboard_alerts(
        snapshot,
        {},
        now="2026-06-30T10:10:03+00:00",
        run_id="m168_resolved_gap_test",
        tick_index=1,
    )

    assert status["color"] == "green"
    assert status["evidence_auto_recovery_gap_currently_resolved"] is True
    assert "evidence_auto_recovery_gap_remains_after_refresh" not in status["reasons"]
    assert {alert["condition_key"] for alert in alerts} == set()


def test_trading_dashboard_suppresses_old_auto_recovery_gap_when_only_background_sources_lag() -> None:
    evidence_freshness = {
        "overall_status": "watch",
        "stale_source_keys": [],
        "lagging_source_keys": ["standing_regimes", "technical_indicators"],
        "evidence_gap_warning": False,
        "scorer_guard": {"action": "normal", "warnings": []},
    }
    evidence_auto_recovery = {
        "status": "refresh_completed_gap_remains",
        "last_freshness_status": "watch",
        "last_stale_source_keys": ["geopolitical_signals"],
        "last_lagging_source_keys": ["market_news"],
        "consecutive_failures": 0,
        "gap_remains_after_refresh": True,
    }
    source_ingest_health = {
        "overall_status": "green",
        "online_required_sources": ["market_data", "market_news", "geopolitical_signals"],
        "online_problem_source_keys": [],
        "degraded_source_keys": [],
    }
    source_online_supervisor = {
        "final_status": "green",
        "final_source_ingest_health_status": "green",
        "final_online_problem_source_keys": [],
        "failed_actions": [],
    }
    status = _status_lamp(
        risk={"risk_state": "within_limits", "new_entries_allowed": True},
        mark_quality={"quality": {}},
        account={"buying_power": 1000.0, "available_funds": 1000.0},
        market_data={"summary": {"status": "green"}},
        evidence_freshness=evidence_freshness,
        evidence_auto_recovery=evidence_auto_recovery,
        source_ingest_health=source_ingest_health,
        source_online_supervisor=source_online_supervisor,
    )

    assert status["color"] == "yellow"
    assert status["evidence_auto_recovery_gap_currently_resolved"] is True
    assert status["evidence_core_sources_ready"] is True
    assert "evidence_freshness_background_lag_watch" in status["reasons"]
    assert "evidence_freshness_gap_guard_active" not in status["reasons"]
    assert "evidence_auto_recovery_gap_remains_after_refresh" not in status["reasons"]
    split = _status_trade_blocking_summary(status)
    assert "evidence_freshness_background_lag_watch" in split["monitoring_only_reasons"]
    assert "evidence_freshness_background_lag_watch" not in split["trade_blocking_reasons"]


def test_trading_dashboard_treats_quiet_geopolitical_source_gap_as_monitoring_only() -> None:
    status = _status_lamp(
        risk={"risk_state": "within_limits", "new_entries_allowed": True},
        mark_quality={"quality": {}},
        account={"buying_power": 1000.0, "available_funds": 1000.0},
        market_data={"summary": {"status": "green"}},
        evidence_freshness={
            "overall_status": "watch",
            "stale_source_keys": [],
            "lagging_source_keys": ["geopolitical_signals"],
            "evidence_gap_warning": False,
            "scorer_guard": {"action": "normal", "warnings": []},
        },
        source_ingest_health={
            "overall_status": "watch",
            "online_problem_source_keys": [],
            "monitoring_only_online_source_keys": ["geopolitical_signals"],
            "degraded_source_keys": [],
        },
        source_online_supervisor={
            "final_status": "yellow",
            "final_source_ingest_health_status": "watch",
            "final_online_problem_source_keys": [],
            "final_monitoring_only_online_source_keys": ["geopolitical_signals"],
            "failed_actions": [],
        },
    )

    assert status["color"] == "yellow"
    assert "source_ingest_health_monitoring_only_gap" in status["reasons"]
    assert "source_online_supervisor_monitoring_only_gap" in status["reasons"]
    split = _status_trade_blocking_summary(status)
    assert "source_ingest_health_monitoring_only_gap" in split["monitoring_only_reasons"]
    assert "source_online_supervisor_monitoring_only_gap" in split["monitoring_only_reasons"]
    assert "source_ingest_health_monitoring_only_gap" not in split["trade_blocking_reasons"]


def test_trading_dashboard_keeps_auto_recovery_gap_when_core_online_source_lags() -> None:
    evidence_freshness = {
        "overall_status": "watch",
        "stale_source_keys": [],
        "lagging_source_keys": ["market_news"],
        "evidence_gap_warning": False,
        "scorer_guard": {"action": "normal", "warnings": []},
    }
    evidence_auto_recovery = {
        "status": "refresh_completed_gap_remains",
        "last_freshness_status": "watch",
        "last_stale_source_keys": [],
        "last_lagging_source_keys": ["market_news"],
        "consecutive_failures": 0,
        "gap_remains_after_refresh": True,
    }
    source_ingest_health = {
        "overall_status": "green",
        "online_required_sources": ["market_data", "market_news", "geopolitical_signals"],
        "online_problem_source_keys": [],
        "degraded_source_keys": [],
    }
    status = _status_lamp(
        risk={"risk_state": "within_limits", "new_entries_allowed": True},
        mark_quality={"quality": {}},
        account={"buying_power": 1000.0, "available_funds": 1000.0},
        market_data={"summary": {"status": "green"}},
        evidence_freshness=evidence_freshness,
        evidence_auto_recovery=evidence_auto_recovery,
        source_ingest_health=source_ingest_health,
        source_online_supervisor={"final_status": "green", "final_source_ingest_health_status": "green"},
    )

    assert status["evidence_auto_recovery_gap_currently_resolved"] is False
    assert status["evidence_core_sources_ready"] is False
    assert "evidence_freshness_gap_guard_active" in status["reasons"]
    assert "evidence_auto_recovery_gap_remains_after_refresh" in status["reasons"]


def test_trading_dashboard_downgrades_closed_session_quiet_market_news_to_monitoring_only() -> None:
    evidence_freshness = {
        "overall_status": "watch",
        "stale_source_keys": [],
        "lagging_source_keys": ["market_news"],
        "evidence_gap_warning": False,
        "scorer_guard": {"action": "normal", "warnings": []},
    }
    evidence_auto_recovery = {
        "status": "refresh_completed_gap_remains",
        "last_freshness_status": "watch",
        "last_stale_source_keys": [],
        "last_lagging_source_keys": ["market_news"],
        "consecutive_failures": 0,
        "gap_remains_after_refresh": True,
    }
    source_ingest_health = {
        "overall_status": "yellow",
        "online_required_sources": ["market_data", "market_news", "geopolitical_signals"],
        "online_problem_source_keys": ["market_news"],
        "degraded_source_keys": ["market_news"],
    }
    source_online_supervisor = {
        "final_status": "yellow",
        "final_source_ingest_health_status": "yellow",
        "final_online_problem_source_keys": ["market_news"],
        "failed_actions": [],
    }
    market_data_risk_gate = {
        "gate_status": "watch",
        "action": "allow_with_market_data_haircut",
        "new_entries_allowed": True,
        "market_data": {
            "closed_session_expected_stale": {
                "active": True,
                "reason": "closed_session_expected_stale_until_next_regular_open",
                "next_regular_open_wait_hours": 57.0,
            }
        },
    }

    status = _status_lamp(
        risk={"risk_state": "within_limits", "new_entries_allowed": True},
        mark_quality={"quality": {}},
        account={"buying_power": 1000.0, "available_funds": 1000.0},
        market_data={"summary": {"status": "yellow", "alert_count": 0}},
        market_data_risk_gate=market_data_risk_gate,
        evidence_freshness=evidence_freshness,
        evidence_auto_recovery=evidence_auto_recovery,
        source_ingest_health=source_ingest_health,
        source_online_supervisor=source_online_supervisor,
    )

    assert status["closed_session_market_news_quiet"] is True
    assert status["evidence_core_sources_ready"] is True
    assert status["evidence_auto_recovery_gap_currently_resolved"] is True
    assert "evidence_freshness_closed_session_market_news_quiet" in status["reasons"]
    assert "source_ingest_health_closed_session_market_news_quiet" in status["reasons"]
    assert "source_online_supervisor_closed_session_market_news_quiet" in status["reasons"]
    assert "evidence_freshness_gap_guard_active" not in status["reasons"]
    assert "evidence_auto_recovery_gap_remains_after_refresh" not in status["reasons"]
    split = _status_trade_blocking_summary(status, {"trade_blocking_market_temperature": False})
    assert not any(reason.startswith("evidence_") or reason.startswith("source_") for reason in split["trade_blocking_reasons"])
    assert "evidence_freshness_closed_session_market_news_quiet" in split["monitoring_only_reasons"]
    assert "source_ingest_health_closed_session_market_news_quiet" in split["monitoring_only_reasons"]
    assert "source_online_supervisor_closed_session_market_news_quiet" in split["monitoring_only_reasons"]


def test_trading_dashboard_alerts_source_online_supervisor_failures_once_then_worsening() -> None:
    snapshot = {
        "status": {"color": "red", "reasons": ["source_online_supervisor_failure"]},
        "account": {"buying_power": 1000.0, "available_funds": 1000.0},
        "risk": {"risk_state": "within_limits"},
        "market_data": {"mark_refresh_quality": {"quality": {}}, "market_data_desk": {"summary": {}}},
        "evidence_freshness": {},
        "evidence_auto_recovery": {},
        "source_ingest_health": {},
        "source_online_supervisor": {
            "final_status": "red",
            "final_source_ingest_health_status": "watch",
            "final_online_problem_source_keys": ["market_news"],
            "failed_actions": [
                {
                    "action_id": "market_news_ibkr_catchup",
                    "source_key": "market_news",
                    "status": "timeout",
                    "description": "Catch up IBKR/Benzinga news.",
                }
            ],
            "latest_tick": {
                "action_status_counts": {"timeout": 1},
                "root_cause_counts": {"collector_timeout": 1},
                "actions": [
                    {
                        "action_id": "market_news_ibkr_catchup",
                        "source_key": "market_news",
                        "status": "timeout",
                        "description": "Catch up IBKR/Benzinga news.",
                    }
                ],
            },
        },
        "missing_sources": [],
    }

    state, first_alerts = evaluate_dashboard_alerts(
        snapshot,
        {},
        now="2026-06-30T10:20:00+00:00",
        run_id="m166_supervisor_alert_test",
        tick_index=1,
    )
    state, repeat_alerts = evaluate_dashboard_alerts(
        snapshot,
        state,
        now="2026-06-30T10:20:01+00:00",
        run_id="m166_supervisor_alert_test",
        tick_index=2,
    )
    worse_snapshot = dict(snapshot)
    worse_snapshot["source_online_supervisor"] = dict(snapshot["source_online_supervisor"])
    worse_snapshot["source_online_supervisor"]["failed_actions"] = [
        *snapshot["source_online_supervisor"]["failed_actions"],
        {
            "action_id": "source_live_validation_ibkr",
            "source_key": "market_news",
            "status": "failed",
            "description": "Validate IBKR/Benzinga news.",
        },
        {
            "action_id": "external_market_sync_marketaux",
            "source_key": "market_news",
            "status": "failed",
            "description": "Sync Marketaux latest news.",
        },
    ]
    state, worse_alerts = evaluate_dashboard_alerts(
        worse_snapshot,
        state,
        now="2026-06-30T10:20:02+00:00",
        run_id="m166_supervisor_alert_test",
        tick_index=3,
    )

    first_keys = {alert["condition_key"] for alert in first_alerts}
    worse_keys = {alert["condition_key"] for alert in worse_alerts}

    assert "source_online_supervisor_failure" in first_keys
    assert "source_online_supervisor_online_gap" in first_keys
    assert "source_online_supervisor_ibkr_benzinga_news_issue" in first_keys
    assert repeat_alerts == []
    assert "source_online_supervisor_failure" in worse_keys
