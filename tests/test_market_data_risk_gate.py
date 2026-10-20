from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from recorder.market_data_risk_gate import (
    build_market_data_risk_gate_report,
    market_data_risk_gate_markdown,
    write_market_data_risk_gate_json,
)
from recorder.options_analysis import (
    build_autonomous_execution_gate_report,
    build_execution_intent_ledger_report,
)
from main import _build_watch_market_data_risk_auto_gate


def _market_data_payload(*, rows: list[dict] | None = None, **summary_overrides: object) -> dict:
    summary = {
        "status": "green",
        "temperature_label": "calm",
        "temperature_score": 0.0,
        "row_count": 3,
        "live_count": 3,
        "delayed_count": 0,
        "missing_price_count": 0,
        "warming_up_count": 0,
        "error_count": 0,
        "alert_count": 0,
        "market_move_alert_count": 0,
        "scored_market_move_alert_count": 0,
        "quality_excluded_market_move_alert_count": 0,
        "liquidity_alert_count": 0,
        "data_alert_count": 0,
        "close_only_count": 0,
        "stale_count": 0,
        "wide_spread_count": 0,
        "severe_wide_spread_count": 0,
        "risk_flags": ["market_data_temperature_normal"],
        "delayed_instruments": [],
        "missing_instruments": [],
        "warming_up_instruments": [],
        "error_instruments": [],
        "stale_instruments": [],
        "close_only_instruments": [],
        "wide_spread_instruments": [],
        "severe_wide_spread_instruments": [],
        "alerted_instruments": [],
        "group_summaries": {},
        "top_moves": [],
    }
    summary.update(summary_overrides)
    return {
        "market_data_desk_version": "market-data-desk-v4",
        "generated_at": "2026-06-30T12:00:00+00:00",
        "status": summary["status"],
        "temperature": {
            "label": summary["temperature_label"],
            "score": summary["temperature_score"],
            "risk_flags": summary["risk_flags"],
        },
        "summary": summary,
        "rows": rows or [],
    }


def _preview_payload() -> dict:
    return {
        "order_preview_version": "paper-order-preview-margin-whatif-v1",
        "preview_item_count": 1,
        "account_snapshot": {"effective_cash": 1000.0},
        "preview_items": [
            {
                "preview_status": "preview_ready",
                "symbol": "NVDA",
                "ticket_type": "stock",
                "ticket_draft": {
                    "ticket_type": "stock",
                    "order_action": "BUY",
                    "quantity": 10,
                    "estimated_notional": 100.0,
                },
                "contract_preview": {"sec_type": "STK", "symbol": "NVDA", "exchange": "SMART", "currency": "USD"},
                "ibkr_what_if_order_preview": {
                    "action": "BUY",
                    "source_order_action": "BUY",
                    "total_quantity": 10,
                    "what_if": True,
                    "transmit": False,
                },
                "local_margin_estimate": {
                    "estimated_initial_margin_change": 100.0,
                    "estimated_cash_impact": -100.0,
                },
            }
        ],
    }


def _session_overlay(
    *,
    monitorable_alerts: list[str] | None = None,
    regular_dependent_alerts: list[str] | None = None,
    is_regular_session: bool = False,
) -> dict:
    monitorable_alerts = monitorable_alerts or []
    regular_dependent_alerts = regular_dependent_alerts or []
    return {
        "market_data_session_overlay_version": "market-data-session-overlay-v1",
        "generated_at": "2026-07-01T02:00:00+00:00",
        "session_name": "regular" if is_regular_session else "overnight",
        "is_regular_session": is_regular_session,
        "risk_interpretation": "overnight_monitoring_active",
        "stock_cash_overnight_monitoring_enabled": True,
        "option_quotes_regular_session_dependent": True,
        "monitorability_counts": {
            "monitorable_now": len(monitorable_alerts),
            "monitor_if_fresh_price_present": len(regular_dependent_alerts),
        },
        "monitorable_alert_instruments": monitorable_alerts,
        "regular_dependent_alert_instruments": regular_dependent_alerts,
    }


def test_market_data_risk_gate_session_downgrades_regular_session_only_stale_after_hours() -> None:
    report = build_market_data_risk_gate_report(
        _market_data_payload(
            status="yellow",
            temperature_label="watch",
            temperature_score=9.0,
            stale_count=2,
            stale_instruments=["US10Y", "VXX"],
            risk_flags=["stale_market_data_present"],
        ),
        market_data_session_overlay_payload=_session_overlay(is_regular_session=False, monitorable_alerts=["USDJPY"]),
        stale_hard_block_instruments=["US10Y", "VXX", "SPY"],
        regular_session_only_stale_hard_block_instruments=["US10Y", "VXX"],
    )

    assert report["gate_status"] == "watch"
    assert "stale_market_data_present" not in report["blocking_reasons"]
    assert "stale_monitoring_only_present" in report["haircut_reasons"]
    assert "session_aware_stale_hard_blocks_downgraded_after_hours" in report["monitoring_only_reasons"]
    assert report["market_data"]["hard_block_stale_instruments"] == []
    assert report["market_data"]["session_downgraded_stale_hard_block_instruments"] == ["US10Y", "VXX"]


def test_market_data_risk_gate_keeps_regular_session_only_stale_hard_block_during_regular_session() -> None:
    report = build_market_data_risk_gate_report(
        _market_data_payload(
            status="yellow",
            temperature_label="watch",
            temperature_score=9.0,
            stale_count=1,
            stale_instruments=["US10Y"],
            risk_flags=["stale_market_data_present"],
        ),
        market_data_session_overlay_payload=_session_overlay(is_regular_session=True, monitorable_alerts=["US10Y"]),
        stale_hard_block_instruments=["US10Y"],
        regular_session_only_stale_hard_block_instruments=["US10Y"],
    )

    assert report["gate_status"] == "block"
    assert "stale_market_data_present" in report["blocking_reasons"]
    assert report["market_data"]["hard_block_stale_instruments"] == ["US10Y"]


def test_market_data_risk_gate_blocks_bad_data_quality(tmp_path: Path) -> None:
    report = build_market_data_risk_gate_report(
        _market_data_payload(
            status="yellow",
            temperature_label="watch",
            temperature_score=8.0,
            delayed_count=1,
            delayed_instruments=["ES"],
        )
    )
    written = write_market_data_risk_gate_json(tmp_path / "gate.json", report)
    round_trip = json.loads(written.read_text(encoding="utf-8"))

    assert report["market_data_risk_gate_version"] == "market-data-risk-gate-v1"
    assert report["gate_status"] == "block"
    assert report["action"] == "block_new_entries"
    assert report["new_entries_allowed"] is False
    assert report["entry_size_multiplier"] == 0.0
    assert "delayed_market_data_present" in report["blocking_reasons"]
    assert round_trip["market_data"]["instruments"]["delayed"] == ["ES"]


def test_market_data_risk_gate_allows_monitoring_only_stale_with_core_policy() -> None:
    report = build_market_data_risk_gate_report(
        _market_data_payload(
            status="yellow",
            temperature_label="watch",
            temperature_score=9.0,
            stale_count=3,
            stale_instruments=["KOSPI", "VIX", "US10Y"],
            risk_flags=["stale_market_data_present"],
        ),
        stale_hard_block_instruments=["ES", "NQ", "SPY", "QQQ"],
        watch_size_multiplier=0.65,
    )

    assert report["gate_status"] == "watch"
    assert report["alpha_entries_allowed"] is True
    assert "stale_market_data_present" not in report["blocking_reasons"]
    assert "stale_monitoring_only_present" in report["haircut_reasons"]
    assert report["market_data"]["hard_block_stale_instruments"] == []
    assert report["market_data"]["monitoring_only_stale_instruments"] == ["KOSPI", "VIX", "US10Y"]
    assert report["entry_size_multiplier"] == 0.65


def test_market_data_risk_gate_blocks_core_stale_with_core_policy() -> None:
    report = build_market_data_risk_gate_report(
        _market_data_payload(
            status="yellow",
            temperature_label="watch",
            temperature_score=9.0,
            stale_count=2,
            stale_instruments=["NQ", "VIX"],
            risk_flags=["stale_market_data_present"],
        ),
        stale_hard_block_instruments=["ES", "NQ", "SPY", "QQQ"],
    )

    assert report["gate_status"] == "block"
    assert report["alpha_entries_allowed"] is False
    assert "stale_market_data_present" in report["blocking_reasons"]
    assert report["market_data"]["hard_block_stale_instruments"] == ["NQ"]
    assert report["market_data"]["monitoring_only_stale_instruments"] == ["VIX"]


def test_market_data_risk_gate_downgrades_us02y_hard_block_when_proxy_group_has_fresh_member() -> None:
    report = build_market_data_risk_gate_report(
        _market_data_payload(
            status="yellow",
            temperature_label="watch",
            temperature_score=9.0,
            stale_count=1,
            stale_instruments=["US02Y"],
            risk_flags=["stale_market_data_present"],
            rows=[
                {
                    "key": "US02Y",
                    "status": "live",
                    "price": 103.1,
                    "price_source": "close",
                    "selected_source_is_close": True,
                    "non_close_price_tick_count": 0,
                    "delayed": False,
                },
                {
                    "key": "SHY",
                    "status": "live",
                    "price": 82.5,
                    "price_source": "last",
                    "selected_source_is_close": False,
                    "non_close_price_tick_count": 4,
                    "delayed": False,
                },
            ],
        ),
        stale_hard_block_instruments=["US02Y"],
        stale_hard_block_proxy_groups=[
            {
                "primary": "US02Y",
                "label": "US 2Y Treasury live proxy group",
                "members": ["US02Y", "UTWO", "SHY", "US02Y_2YY", "US02Y_ZT"],
                "require_all_stale": True,
            }
        ],
    )

    assert report["gate_status"] == "watch"
    assert "stale_market_data_present" not in report["blocking_reasons"]
    assert "stale_monitoring_only_present" in report["haircut_reasons"]
    assert "proxy_group_fresh_member_downgraded_stale_hard_block" in report["monitoring_only_reasons"]
    assert report["market_data"]["hard_block_stale_instruments"] == []
    assert report["market_data"]["monitoring_only_stale_instruments"] == ["US02Y"]
    assert report["market_data"]["proxy_group_downgraded_stale_hard_block_instruments"] == ["US02Y"]
    evaluation = report["market_data"]["proxy_group_evaluations"][0]
    assert evaluation["fresh_members"] == ["SHY"]
    assert evaluation["downgraded_primary_from_hard_block"] is True


def test_market_data_risk_gate_marks_rates_sensitive_macro_event_window_as_monitoring_context() -> None:
    now = datetime.now(UTC).isoformat()
    report = build_market_data_risk_gate_report(
        _market_data_payload(),
        macro_release_interpreter_payload={
            "summary": {"latest_macro_surprise_label": "growth weak / rates dovish"},
            "clusters": [
                {
                    "families": {"labor_growth": {"score": -1.0}},
                    "items": [
                        {
                            "indicator_name": "US Nonfarm Payrolls",
                            "headline": "Employment report moved Treasury yields",
                            "published_at": now,
                        }
                    ],
                }
            ],
            "scorer_evidence_packet": {
                "latest_macro_release": {
                    "macro_surprise_label": "growth weak / rates dovish",
                    "asset_bias": {"TLT": "positive", "UUP": "negative"},
                }
            },
        },
        macro_event_window_minutes=180.0,
    )

    assert report["gate_status"] == "pass"
    assert "macro_event_window_rates_sensitive" in report["monitoring_only_reasons"]
    assert report["market_data"]["macro_event_window"]["active"] is True
    assert report["market_data"]["macro_event_window"]["latest_indicator"] == "US Nonfarm Payrolls"


def test_market_data_risk_gate_explains_macro_stress_without_hard_defensive_block() -> None:
    now = datetime.now(UTC).isoformat()
    report = build_market_data_risk_gate_report(
        _market_data_payload(
            status="red",
            temperature_label="stress",
            temperature_score=35.0,
            alert_count=1,
            market_move_alert_count=1,
            scored_market_move_alert_count=1,
            risk_flags=["gap_or_sudden_move_alerts", "market_temperature_elevated"],
            top_moves=[
                {
                    "key": "VIX",
                    "group": "volatility",
                    "alerts": ["gap_down:-4.0%"],
                    "market_move_score_eligible": True,
                }
            ],
        ),
        macro_release_interpreter_payload={
            "summary": {"latest_macro_surprise_label": "growth weak / rates dovish"},
            "clusters": [
                {
                    "families": {"labor_growth": {"score": -1.0}},
                    "items": [
                        {
                            "indicator_name": "US Nonfarm Payrolls",
                            "headline": "Employment report moved volatility and Treasury yields",
                            "published_at": now,
                        }
                    ],
                }
            ],
            "scorer_evidence_packet": {
                "latest_macro_release": {
                    "macro_surprise_label": "growth weak / rates dovish",
                    "asset_bias": {"TLT": "positive", "VXX": "negative"},
                }
            },
        },
    )

    assert report["gate_status"] == "watch"
    assert report["macro_adjusted_priority"] is True
    assert "market_temperature_stress_explained_by_macro_window" in report["haircut_reasons"]
    assert "expected_macro_reaction_context" in report["monitoring_only_reasons"]
    assert report["market_data"]["macro_expected_reaction_moves"][0]["key"] == "VIX"
    assert "macro_context" in report["reason_breakdown"]


def test_market_data_risk_gate_keeps_vix_core_without_session_overlay_and_reports_source_freshness() -> None:
    now = datetime.now(UTC).isoformat()
    report = build_market_data_risk_gate_report(
        _market_data_payload(
            rows=[
                {
                    "key": "VIX",
                    "status": "live",
                    "price": 16.0,
                    "price_source": "last",
                    "selected_source_is_close": False,
                    "non_close_price_tick_count": 3,
                    "delayed": False,
                }
            ],
            status="yellow",
            temperature_label="watch",
            temperature_score=10.0,
            alert_count=1,
            market_move_alert_count=1,
            scored_market_move_alert_count=1,
            top_moves=[
                {"key": "VIX", "group": "volatility", "alerts": ["gap_down:-4.0%"], "market_move_score_eligible": True}
            ],
        )
        | {"generated_at": now},
        stale_hard_block_instruments=["VIX"],
        regular_session_only_stale_hard_block_instruments=["VIX"],
    )

    assert report["market_data"]["source_freshness"]["status"] == "fresh"
    assert [item["key"] for item in report["market_data"]["primary_alert_moves"]] == ["VIX"]
    assert report["market_data"]["monitoring_only_alert_moves"] == []


def test_market_data_risk_gate_adds_macro_direction_option_and_close_proxy_context() -> None:
    now = datetime.now(UTC).isoformat()
    report = build_market_data_risk_gate_report(
        _market_data_payload(
            rows=[
                {
                    "key": "WTI",
                    "status": "live",
                    "price": 67.7,
                    "price_source": "last",
                    "selected_source_is_close": False,
                    "non_close_price_tick_count": 5,
                    "delayed": False,
                },
                {
                    "key": "BRENT",
                    "status": "live",
                    "price": 72.95,
                    "price_source": "close",
                    "selected_source_is_close": True,
                    "non_close_price_tick_count": 0,
                    "delayed": False,
                },
            ],
            status="yellow",
            temperature_label="hot",
            temperature_score=20.0,
            close_only_count=1,
            close_only_instruments=["BRENT"],
            alert_count=1,
            market_move_alert_count=1,
            scored_market_move_alert_count=1,
            risk_flags=["gap_or_sudden_move_alerts", "close_only_quotes_present"],
            top_moves=[
                {"key": "SPY", "group": "us_equity_index_etf", "gap_pct": 1.0, "alerts": ["gap_up:+1.0%"], "market_move_score_eligible": True}
            ],
        )
        | {"generated_at": now},
        macro_release_interpreter_payload={
            "summary": {"latest_macro_surprise_label": "growth weak / rates dovish"},
            "clusters": [
                {
                    "families": {"labor_growth": {"score": -1.0}},
                    "items": [{"indicator_name": "US Nonfarm Payrolls", "headline": "Weak jobs", "published_at": now}],
                }
            ],
            "scorer_evidence_packet": {
                "latest_macro_release": {
                    "macro_surprise_label": "growth weak / rates dovish",
                    "asset_bias": {"SPY": "positive"},
                }
            },
        },
        options_market_structure_payload={
            "generated_at": now,
            "session_guard": {"max_snapshot_age_minutes": 30},
            "symbols": [
                {
                    "symbol": "SPY",
                    "gamma_regime": "negative_gamma_trend_amplification_bias",
                    "call_wall": 760.0,
                    "put_wall": 740.0,
                    "max_pain": 750.0,
                    "proximity_alerts": [],
                }
            ],
        },
    )

    assert report["market_data"]["macro_reaction_assessment"]["status"] == "aligned"
    assert "macro_reaction_direction_aligned" in report["monitoring_only_reasons"]
    assert report["market_data"]["option_structure_context"]["overall_status"] == "trend_amplification_risk"
    assert "option_structure_trend_amplification_context" in report["haircut_reasons"]
    assert report["market_data"]["close_only_proxy_status"]["rows"][0]["fresh_proxy_members"] == ["WTI"]
    assert "reference_close_only_proxy_available" in report["monitoring_only_reasons"]
    assert report["operator_summary_lines"]


def test_market_data_risk_gate_uses_dominant_macro_cluster_over_latest_minor_release() -> None:
    now = datetime.now(UTC).isoformat()
    earlier = datetime.now(UTC).replace(minute=30).isoformat()
    report = build_market_data_risk_gate_report(
        _market_data_payload(
            status="yellow",
            temperature_label="hot",
            temperature_score=20.0,
            alert_count=2,
            market_move_alert_count=2,
            scored_market_move_alert_count=2,
            top_moves=[
                {"key": "VIX", "group": "volatility", "gap_pct": -4.0, "alerts": ["gap_down:-4.0%"], "market_move_score_eligible": True},
                {"key": "GC", "group": "commodities", "gap_pct": 1.5, "alerts": ["gap_up:+1.5%"], "market_move_score_eligible": True},
            ],
        )
        | {"generated_at": now},
        macro_release_interpreter_payload={
            "summary": {"latest_macro_surprise_label": "growth strong / rates hawkish"},
            "clusters": [
                {
                    "families": {"growth_activity": 1},
                    "items": [
                        {
                            "indicator_name": "Factory Orders (MoM)",
                            "headline": "USA Factory Orders (MoM) For May -1.3% Vs -1.7% Est",
                            "published_at": now,
                        }
                    ],
                },
                {
                    "families": {"labor_growth": 5, "labor_slack": 3},
                    "items": [
                        {
                            "indicator_name": "Nonfarm Payrolls",
                            "headline": "USA Nonfarm Payrolls For June 57K Vs 114K Est.",
                            "published_at": earlier,
                        }
                    ],
                },
            ],
        },
    )

    macro = report["market_data"]["macro_reaction_assessment"]
    assert macro["dominant_macro_surprise_label"] == "growth weak / rates dovish"
    assert macro["status"] == "aligned"
    assert "macro_reaction_direction_aligned" in report["monitoring_only_reasons"]


def test_market_data_risk_gate_downgrades_regional_alerts_for_us_trading() -> None:
    report = build_market_data_risk_gate_report(
        _market_data_payload(
            status="yellow",
            temperature_label="hot",
            temperature_score=20.0,
            alert_count=4,
            market_move_alert_count=4,
            scored_market_move_alert_count=3,
            quality_excluded_market_move_alert_count=1,
            wide_spread_count=1,
            severe_wide_spread_count=1,
            liquidity_alert_count=1,
            alerted_instruments=["KOSPI", "KOSPI_FUT", "KOSDAQ", "NIKKEI"],
            wide_spread_instruments=["NIKKEI"],
            severe_wide_spread_instruments=["NIKKEI"],
            risk_flags=["gap_or_sudden_move_alerts", "wide_spread_quotes_present"],
            top_moves=[
                {"key": "KOSPI", "alerts": ["gap_down:-8.0%"], "market_move_score_eligible": True},
                {"key": "KOSPI_FUT", "alerts": ["gap_down:-7.0%"], "market_move_score_eligible": True},
                {"key": "KOSDAQ", "alerts": ["gap_down:-6.5%"], "market_move_score_eligible": True},
                {"key": "NIKKEI", "alerts": ["wide_spread:20.0%", "gap_down:-2.0%"], "market_move_score_eligible": False},
            ],
        ),
        monitoring_only_instruments=["KOSPI", "KOSPI_FUT", "KOSDAQ", "NIKKEI"],
    )

    assert report["gate_status"] == "pass"
    assert report["alpha_entries_allowed"] is True
    assert "gap_or_sudden_move_alerts" not in report["haircut_reasons"]
    assert "wide_spread_quotes_present" not in report["haircut_reasons"]
    assert "monitoring_only_gap_or_sudden_move_alerts" in report["monitoring_only_reasons"]
    assert "monitoring_only_wide_spread_quotes_present" in report["monitoring_only_reasons"]
    assert "monitoring_only_market_temperature_watch_or_hot" in report["monitoring_only_reasons"]
    assert [item["key"] for item in report["market_data"]["monitoring_only_alert_moves"]] == [
        "KOSPI",
        "KOSPI_FUT",
        "KOSDAQ",
        "NIKKEI",
    ]


def test_market_data_risk_gate_keeps_core_us_relevant_alerts_as_haircut() -> None:
    report = build_market_data_risk_gate_report(
        _market_data_payload(
            status="yellow",
            temperature_label="hot",
            temperature_score=20.0,
            alert_count=1,
            market_move_alert_count=1,
            scored_market_move_alert_count=1,
            alerted_instruments=["WTI"],
            top_moves=[
                {"key": "WTI", "alerts": ["gap_down:-1.5%"], "market_move_score_eligible": True},
            ],
        ),
        monitoring_only_instruments=["KOSPI", "KOSPI_FUT", "KOSDAQ", "NIKKEI"],
        hot_size_multiplier=0.5,
    )

    assert report["gate_status"] == "watch"
    assert report["entry_size_multiplier"] == 0.5
    assert "gap_or_sudden_move_alerts" in report["haircut_reasons"]


def test_market_data_risk_gate_downgrades_monitoring_only_close_only_quotes() -> None:
    report = build_market_data_risk_gate_report(
        _market_data_payload(
            status="yellow",
            temperature_label="watch",
            temperature_score=9.0,
            close_only_count=3,
            close_only_instruments=["US03M", "US30Y", "VVIX"],
            risk_flags=["close_only_quotes_present"],
        ),
        monitoring_only_instruments=["US03M", "US30Y", "VVIX"],
    )

    assert report["gate_status"] == "pass"
    assert "close_only_quotes_present" not in report["haircut_reasons"]
    assert "monitoring_only_close_only_quotes_present" in report["monitoring_only_reasons"]
    assert report["market_data"]["primary_close_only_instruments"] == []
    assert report["market_data"]["monitoring_only_close_only_instruments"] == ["US03M", "US30Y", "VVIX"]


def test_market_data_risk_gate_keeps_primary_close_only_quotes_as_haircut() -> None:
    report = build_market_data_risk_gate_report(
        _market_data_payload(
            status="yellow",
            temperature_label="watch",
            temperature_score=9.0,
            close_only_count=2,
            close_only_instruments=["VIX", "US10Y"],
            risk_flags=["close_only_quotes_present"],
        ),
        monitoring_only_instruments=["US03M", "US30Y", "VVIX"],
    )

    assert report["gate_status"] == "watch"
    assert "close_only_quotes_present" in report["haircut_reasons"]
    assert report["market_data"]["primary_close_only_instruments"] == ["VIX", "US10Y"]


def test_market_data_risk_gate_defensive_only_for_stress_without_data_block() -> None:
    report = build_market_data_risk_gate_report(
        _market_data_payload(
            status="red",
            temperature_label="stress",
            temperature_score=35.0,
            alert_count=2,
            market_move_alert_count=2,
            scored_market_move_alert_count=2,
        )
    )

    assert report["gate_status"] == "defensive"
    assert report["action"] == "defensive_or_hedge_only"
    assert report["alpha_entries_allowed"] is False
    assert report["hedge_entries_allowed"] is True
    assert report["exit_or_risk_reduction_allowed"] is True


def test_market_data_risk_gate_session_adjusts_regular_dependent_after_hours_stress() -> None:
    report = build_market_data_risk_gate_report(
        _market_data_payload(
            status="red",
            temperature_label="stress",
            temperature_score=35.0,
            alert_count=2,
            market_move_alert_count=2,
            scored_market_move_alert_count=2,
            alerted_instruments=["VIX", "VVIX"],
        ),
        market_data_session_overlay_payload=_session_overlay(regular_dependent_alerts=["VIX", "VVIX"]),
        session_dependent_size_multiplier=0.6,
    )

    assert report["gate_status"] == "watch"
    assert report["action"] == "allow_with_session_context_haircut"
    assert report["allowed"] is True
    assert report["alpha_entries_allowed"] is True
    assert report["entry_size_multiplier"] == 0.6
    assert report["session_adjusted_priority"] is True
    assert report["market_data_session_context"]["session_priority_class"] == "regular_dependent_after_hours_context"
    assert "market_temperature_stress_regular_dependent_after_hours_context" in report["haircut_reasons"]
    assert "Session priority class" in market_data_risk_gate_markdown(report)


def test_market_data_risk_gate_keeps_defensive_for_monitorable_overnight_stress() -> None:
    report = build_market_data_risk_gate_report(
        _market_data_payload(
            status="red",
            temperature_label="stress",
            temperature_score=35.0,
            alert_count=2,
            market_move_alert_count=2,
            scored_market_move_alert_count=2,
            alerted_instruments=["NQ", "WTI"],
        ),
        market_data_session_overlay_payload=_session_overlay(monitorable_alerts=["NQ", "WTI"]),
    )

    assert report["gate_status"] == "defensive"
    assert report["action"] == "defensive_or_hedge_only"
    assert report["allowed"] is False
    assert report["session_adjusted_priority"] is False
    assert report["market_data_session_context"]["session_priority_class"] == "overnight_monitoring_active"


def test_market_data_risk_gate_downgrades_holiday_closed_session_stale_cash_data() -> None:
    session_overlay = _session_overlay(monitorable_alerts=["QQQ"], regular_dependent_alerts=["VIX"], is_regular_session=False)
    session_overlay["next_regular_open"] = {"wait_hours": 57.0, "wait_seconds": 205200.0}
    report = build_market_data_risk_gate_report(
        _market_data_payload(
            status="red",
            temperature_label="stress",
            temperature_score=44.0,
            stale_count=4,
            stale_instruments=["USDJPY", "VIX", "SPY", "QQQ"],
            close_only_count=3,
            close_only_instruments=["DXY", "ES", "NQ"],
            alert_count=2,
            market_move_alert_count=2,
            scored_market_move_alert_count=2,
            alerted_instruments=["VIX", "QQQ"],
            risk_flags=[
                "stale_market_data_present",
                "close_only_quotes_present",
                "gap_or_sudden_move_alerts",
                "market_temperature_elevated",
            ],
            top_moves=[
                {"key": "VIX", "group": "volatility", "gap_pct": -4.7, "alerts": ["gap_down:-4.7%"], "market_move_score_eligible": True},
                {"key": "QQQ", "group": "us_equity_index_etf", "gap_pct": -1.5, "alerts": ["gap_down:-1.5%"], "market_move_score_eligible": True},
            ],
        ),
        market_data_session_overlay_payload=session_overlay,
        stale_hard_block_instruments=["USDJPY", "VIX", "SPY", "QQQ"],
        closed_session_expected_stale_min_wait_hours=24.0,
    )

    assert report["gate_status"] == "watch"
    assert report["action"] == "allow_with_market_data_haircut"
    assert report["alpha_entries_allowed"] is True
    assert "stale_market_data_present" not in report["blocking_reasons"]
    assert "market_temperature_stress" not in report["defensive_reasons"]
    assert "gap_or_sudden_move_alerts" not in report["haircut_reasons"]
    assert "closed_session_expected_stale_or_close_only_context" in report["haircut_reasons"]
    assert "closed_session_market_temperature_expected_deferred" in report["monitoring_only_reasons"]
    assert report["market_data"]["closed_session_expected_stale"]["active"] is True
    assert report["market_data"]["closed_session_expected_stale"]["downgraded_gap_alert_instruments"] == ["QQQ", "VIX"]
    assert report["market_data"]["hard_block_stale_instruments"] == []
    assert report["market_data"]["monitoring_only_stale_instruments"] == ["QQQ", "SPY", "USDJPY", "VIX"]
    assert [item["key"] for item in report["market_data"]["primary_alert_moves"]] == []
    assert [item["key"] for item in report["market_data"]["monitoring_only_alert_moves"]] == ["VIX", "QQQ"]


def test_market_data_risk_gate_haircuts_watch_or_hot_markets() -> None:
    watch = build_market_data_risk_gate_report(
        _market_data_payload(
            status="yellow",
            temperature_label="watch",
            temperature_score=12.0,
            alert_count=1,
            scored_market_move_alert_count=1,
        ),
        watch_size_multiplier=0.65,
        hot_size_multiplier=0.40,
    )
    hot = build_market_data_risk_gate_report(
        _market_data_payload(
            status="yellow",
            temperature_label="hot",
            temperature_score=22.0,
            alert_count=2,
            scored_market_move_alert_count=2,
        ),
        watch_size_multiplier=0.65,
        hot_size_multiplier=0.40,
    )

    assert watch["gate_status"] == "watch"
    assert watch["action"] == "allow_with_market_data_haircut"
    assert watch["entry_size_multiplier"] == 0.65
    assert hot["entry_size_multiplier"] == 0.40
    assert "Market Data Risk Action Gate" in market_data_risk_gate_markdown(watch)


def test_market_data_risk_gate_allows_normal_market_data() -> None:
    report = build_market_data_risk_gate_report(_market_data_payload())

    assert report["gate_status"] == "pass"
    assert report["action"] == "allow_normal"
    assert report["new_entries_allowed"] is True
    assert report["entry_size_multiplier"] == 1.0


def test_auto_gate_and_intent_ledger_apply_market_data_haircut() -> None:
    market_gate = build_market_data_risk_gate_report(
        _market_data_payload(
            status="yellow",
            temperature_label="hot",
            temperature_score=20.0,
            alert_count=1,
            scored_market_move_alert_count=1,
        ),
        hot_size_multiplier=0.5,
    )

    auto_gate = build_autonomous_execution_gate_report(
        _preview_payload(),
        allow_fractional_shares=True,
        max_auto_orders=5,
        market_data_risk_gate_payload=market_gate,
    )
    ledger = build_execution_intent_ledger_report(auto_gate, max_intents=5)

    assert auto_gate["auto_ready_count"] == 1
    item = auto_gate["auto_execution_items"][0]
    assert item["market_data_entry_size_multiplier"] == 0.5
    intent = ledger["intents"][0]
    assert intent["market_data_risk_gate_applied"] is True
    assert intent["quantity"] == 5
    assert intent["order_preview"]["total_quantity"] == 5


def test_auto_gate_blocks_when_market_data_gate_blocks() -> None:
    market_gate = build_market_data_risk_gate_report(
        _market_data_payload(delayed_count=1, delayed_instruments=["NQ"]),
    )

    auto_gate = build_autonomous_execution_gate_report(
        _preview_payload(),
        allow_fractional_shares=True,
        max_auto_orders=5,
        market_data_risk_gate_payload=market_gate,
    )

    assert auto_gate["auto_ready_count"] == 0
    assert auto_gate["auto_gate_status_counts"]["auto_execution_blocked"] == 1
    assert auto_gate["auto_gate_block_reason_counts"]["market_data_risk_gate_allows_new_paper_entries"] == 1


def test_watch_helper_builds_market_data_risk_aware_auto_gate(tmp_path: Path) -> None:
    preview_path = tmp_path / "preview.json"
    market_data_path = tmp_path / "market_data.json"
    source_auto_path = tmp_path / "source_auto_gate.json"
    preview_path.write_text(json.dumps(_preview_payload()), encoding="utf-8")
    market_data_path.write_text(
        json.dumps(
            _market_data_payload(
                status="yellow",
                temperature_label="watch",
                temperature_score=12.0,
                alert_count=1,
                scored_market_move_alert_count=1,
            )
        ),
        encoding="utf-8",
    )
    source_auto_path.write_text(
        json.dumps(
            {
                "result": {
                    "autonomous_execution_gate_version": "paper-autonomous-execution-gate-v1",
                    "gate_parameters": {
                        "allow_fractional_shares": True,
                        "allow_short_stock": False,
                        "require_short_availability_for_shorts": True,
                        "require_broker_whatif": False,
                        "max_auto_orders": 5,
                        "min_cash_buffer": 0.0,
                        "allowed_ticket_types": ["stock"],
                        "allowed_actions": ["BUY", "SELL"],
                    },
                },
                "filters": {"preview_json": str(preview_path)},
            }
        ),
        encoding="utf-8",
    )
    profile = {
        "market_data_risk_gate": {
            "enabled": True,
            "market_data_json": str(market_data_path),
            "gate_json_output_path": str(tmp_path / "m175_gate.json"),
            "gate_output": str(tmp_path / "m175_gate.md"),
            "auto_gate_json_output_path": str(tmp_path / "m175_auto_gate.json"),
            "auto_gate_output": str(tmp_path / "m175_auto_gate.md"),
            "watch_size_multiplier": 0.6,
        }
    }

    auto_gate_path, gate, summary = _build_watch_market_data_risk_auto_gate(
        profile=profile,
        profile_base=tmp_path,
        source_auto_gate_json_path=source_auto_path,
        run_id="test_m175",
    )
    payload = json.loads(auto_gate_path.read_text(encoding="utf-8"))["result"]

    assert summary["applied"] is True
    assert gate["action"] == "allow_with_market_data_haircut"
    assert gate["entry_size_multiplier"] == 0.6
    assert payload["gate_parameters"]["market_data_risk_gate_configured"] is True
    assert payload["gate_parameters"]["market_data_entry_size_multiplier"] == 0.6
    assert payload["auto_ready_count"] == 1
    assert payload["auto_execution_items"][0]["market_data_entry_size_multiplier"] == 0.6
    assert (tmp_path / "m175_gate.md").exists()
    assert (tmp_path / "m175_auto_gate.md").exists()
