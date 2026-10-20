from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta

from recorder.ai_costs import build_ai_cost_report
from recorder.artifact_cleanup import build_artifact_cleanup_report
from recorder.capital_aware_action_gate import build_capital_aware_action_gate_report, capital_aware_action_gate_markdown
from recorder.capital_margin_state import build_capital_margin_state, capital_margin_state_markdown
from recorder.code_health import build_code_health_report
from recorder.earnings_calendar import build_future_earnings_calendar
from recorder.prediction_trends import build_prediction_trend_report


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def test_artifact_cleanup_keeps_latest_logical_export(tmp_path) -> None:
    exports_dir = tmp_path / "exports"
    work_dir = tmp_path / "work"
    exports_dir.mkdir()
    work_dir.mkdir()
    base = datetime(2026, 1, 1, tzinfo=UTC).timestamp()
    for index, name in enumerate(["m1_report.md", "m2_report.md", "m3_report.md"]):
        path = exports_dir / name
        path.write_text(name, encoding="utf-8")
        os.utime(path, (base + index, base + index))

    dry_run = build_artifact_cleanup_report(
        exports_dir=exports_dir,
        work_dir=work_dir,
        keep_latest_per_logical_key=1,
        include_work=False,
        execute=False,
        now=datetime(2026, 1, 2, tzinfo=UTC),
    )
    assert dry_run["candidate_count"] == 2
    assert (exports_dir / "m1_report.md").exists()

    executed = build_artifact_cleanup_report(
        exports_dir=exports_dir,
        work_dir=work_dir,
        keep_latest_per_logical_key=1,
        include_work=False,
        execute=True,
        now=datetime(2026, 1, 2, tzinfo=UTC),
    )
    assert executed["deleted_count"] == 2
    assert sorted(path.name for path in exports_dir.iterdir()) == ["m3_report.md"]


def test_ai_cost_report_aggregates_existing_token_columns(tmp_path) -> None:
    db_path = tmp_path / "events.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE news_analyses(
            provider TEXT,
            model_name TEXT,
            input_tokens INTEGER,
            output_tokens INTEGER,
            completed_at TEXT,
            created_at TEXT
        )
        """
    )
    conn.execute(
        """
        INSERT INTO news_analyses(provider, model_name, input_tokens, output_tokens, completed_at, created_at)
        VALUES ('openai', 'test-model', 1000, 2000, '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')
        """
    )
    conn.execute(
        """
        CREATE TABLE m223_live_learning_adjustment_notes(
            analysis_provider TEXT,
            analysis_model TEXT,
            billing_payer TEXT,
            input_tokens INTEGER,
            output_tokens INTEGER,
            created_at TEXT
        )
        """
    )
    conn.execute(
        """
        INSERT INTO m223_live_learning_adjustment_notes(analysis_provider, analysis_model, billing_payer, input_tokens, output_tokens, created_at)
        VALUES ('openai', 'note-model', '', NULL, NULL, '2026-01-01T01:00:00+00:00')
        """
    )
    conn.execute(
        """
        INSERT INTO m223_live_learning_adjustment_notes(analysis_provider, analysis_model, billing_payer, input_tokens, output_tokens, created_at)
        VALUES ('openai', 'note-model', 'openai', 100, 50, '2026-01-01T02:00:00+00:00')
        """
    )
    conn.commit()
    conn.close()
    pricing = tmp_path / "pricing.json"
    pricing.write_text(
        json.dumps(
            {
                "currency": "USD",
                "models": {
                    "test-model": {"input_per_1m": 1.0, "output_per_1m": 2.0},
                    "note-model": {"input_per_1m": 1.0, "output_per_1m": 2.0},
                },
            }
        ),
        encoding="utf-8",
    )

    report = build_ai_cost_report(database_path=db_path, pricing_path=pricing)

    assert report["row_count"] == 2
    assert report["total_tokens"] == 3150
    assert report["estimated_cost"] == 0.005
    assert report["usage_equivalent_estimated_cost"] == 0.0052
    assert report["cost_status"] == "estimated"
    assert report["unmetered_row_count"] == 1
    assert report["unmetered_model_rows"][0]["model_name"] == "note-model"
    assert any(
        row["source_table"] == "m223_live_learning_adjustment_notes"
        and row["model_name"] == "note-model"
        and row["total_tokens"] == 150
        and row["billing_payer"] == "openai"
        and row["estimated_cost"] == 0.0
        and row["cost_status"] == "non_billable_openai_payer"
        for row in report["daily_model_rows"]
    )
    assert "lower-bound" in report["coverage_warning"]


def test_prediction_trend_report_compares_current_and_previous_windows(tmp_path) -> None:
    db_path = tmp_path / "events.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE replay_model_scores(
            scorer_name TEXT,
            model_name TEXT,
            scorer_version TEXT,
            horizon TEXT,
            event_type TEXT,
            direction_hit INTEGER,
            absolute_raw_error REAL,
            absolute_excess_error REAL,
            confidence REAL,
            updated_at TEXT,
            created_at TEXT,
            replay_timestamp TEXT,
            prediction_cutoff_at TEXT
        )
        """
    )
    rows = [
        ("2026-01-01T00:00:00+00:00", 1),
        ("2026-01-02T00:00:00+00:00", 0),
        ("2026-01-08T00:00:00+00:00", 1),
        ("2026-01-09T00:00:00+00:00", 1),
    ]
    for ts, hit in rows:
        conn.execute(
            """
            INSERT INTO replay_model_scores(
                scorer_name,model_name,scorer_version,horizon,event_type,direction_hit,
                absolute_raw_error,absolute_excess_error,confidence,
                updated_at,created_at,replay_timestamp,prediction_cutoff_at
            ) VALUES ('openai','test-model','v1','1D','earnings',?,0.1,0.1,0.8,?,?,?,?)
            """,
            (hit, ts, ts, ts, ts),
        )
    conn.commit()
    conn.close()

    report = build_prediction_trend_report(
        database_path=db_path,
        window_days=7,
        min_rows=1,
        now="2026-01-15T00:00:00+00:00",
    )

    assert report["comparison_count"] == 1
    comparison = report["comparisons"][0]
    assert comparison["current"]["direction_accuracy"] == 1.0
    assert comparison["previous"]["direction_accuracy"] == 0.5
    assert comparison["trend"] == "improving"


def test_future_earnings_calendar_collapses_nearby_sec_events(tmp_path) -> None:
    db_path = tmp_path / "events.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE official_documents(
            id INTEGER PRIMARY KEY,
            primary_document TEXT,
            materiality_reason TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE historical_events(
            id INTEGER PRIMARY KEY,
            event_type TEXT,
            event_subtype TEXT,
            actual_subject_symbol TEXT,
            official_document_id INTEGER,
            event_timestamp TEXT
        )
        """
    )
    timestamps = [
        "2025-01-02T14:00:00+00:00",
        "2025-01-29T21:00:00+00:00",
        "2025-04-02T14:00:00+00:00",
        "2025-04-22T20:00:00+00:00",
        "2025-07-02T14:00:00+00:00",
        "2025-07-23T20:00:00+00:00",
        "2025-10-02T14:00:00+00:00",
        "2025-10-22T20:00:00+00:00",
        "2026-01-02T14:00:00+00:00",
        "2026-01-28T21:00:00+00:00",
        "2026-04-02T14:00:00+00:00",
        "2026-04-22T20:00:00+00:00",
    ]
    for doc_id, timestamp in enumerate(timestamps, start=1):
        document_date = timestamp[:10].replace("-", "")
        conn.execute(
            "INSERT INTO official_documents(id, primary_document, materiality_reason) VALUES (?, ?, ?)",
            (doc_id, f"tsla-{document_date}.htm", "8-K item/text contains financial results and forward guidance"),
        )
        conn.execute(
            """
            INSERT INTO historical_events(
                event_type,event_subtype,actual_subject_symbol,official_document_id,event_timestamp
            ) VALUES ('earnings','quarterly_results_with_guidance','TSLA',?,?)
            """,
            (doc_id, timestamp),
        )
    conn.commit()
    conn.close()

    report = build_future_earnings_calendar(
        database_path=db_path,
        symbols=("TSLA",),
        horizon_days=120,
        now="2026-07-03T00:00:00+00:00",
    )

    assert report["projection_count"] == 1
    projection = report["projections"][0]
    assert projection["history_count"] == 6
    assert 89 <= projection["median_cycle_days"] <= 92
    assert projection["projected_date"] == "2026-07-22"


def test_code_health_report_groups_large_middle_layer_files(tmp_path) -> None:
    recorder_dir = tmp_path / "recorder"
    prediction_dir = tmp_path / "prediction_evaluation"
    recorder_dir.mkdir()
    prediction_dir.mkdir()
    (recorder_dir / "options_analysis.py").write_text(
        "def large_function():\n" + "\n".join("    x = 1" for _ in range(20)),
        encoding="utf-8",
    )
    (prediction_dir / "evaluation.py").write_text("def small():\n    return 1\n", encoding="utf-8")

    report = build_code_health_report(
        repo_root=tmp_path,
        top_n=5,
        large_file_threshold=10,
        long_function_threshold=10,
    )

    assert report["large_file_count"] == 1
    assert report["long_function_count"] == 1
    assert report["top_large_files"][0]["group"] == "options"
    assert report["top_long_functions"][0]["name"] == "large_function"


def test_capital_margin_state_green_budget_from_account_snapshot(tmp_path) -> None:
    snapshot_path = tmp_path / "account_snapshot.json"
    snapshot_path.write_text(
        json.dumps(
            {
                "generated_at": "2026-01-15T14:30:00+00:00",
                "selected_account": "U1",
                "accounts": [
                    {
                        "account": "U1",
                        "tags": {
                            "NetLiquidation": {"numeric_value": 100000.0, "currency": "USD"},
                            "TotalCashValue": {"numeric_value": 35000.0, "currency": "USD"},
                            "AvailableFunds": {"numeric_value": 50000.0, "currency": "USD"},
                            "BuyingPower": {"numeric_value": 200000.0, "currency": "USD"},
                            "ExcessLiquidity": {"numeric_value": 65000.0, "currency": "USD"},
                            "InitMarginReq": {"numeric_value": 15000.0, "currency": "USD"},
                            "MaintMarginReq": {"numeric_value": 10000.0, "currency": "USD"},
                        },
                    }
                ],
                "positions": [
                    {"account": "U1", "contract": {"sec_type": "STK", "symbol": "SPY"}, "position": 10},
                    {"account": "U1", "contract": {"sec_type": "OPT", "symbol": "SPY"}, "position": 1},
                ],
            }
        ),
        encoding="utf-8",
    )

    report = build_capital_margin_state(
        account_snapshot_path=snapshot_path,
        previous_state_path=None,
        now=datetime(2026, 1, 15, 14, 40, tzinfo=UTC),
    )

    assert report["status"] == "green"
    assert report["leverage_mode"] == "normal_margin"
    assert report["budgets"]["options_premium_budget"] == 3000.0
    assert report["withdrawal_guard"]["status"] == "safe_withdrawal_capacity_available"
    assert report["withdrawal_guard"]["max_safe_withdrawal"] == 18718.75
    assert report["withdrawal_guard"]["binding_constraints"][0]["name"] == "minimum_options_premium_cash_capacity"
    assert report["positions"]["option_position_count"] == 1
    assert report["safety"]["ibkr_order_submitted"] is False
    assert "M237/M238 Capital & Margin State" in capital_margin_state_markdown(report)


def test_capital_margin_state_uses_micro_cash_budget_overlay_for_tiny_cash_account(tmp_path) -> None:
    snapshot_path = tmp_path / "account_snapshot.json"
    snapshot_path.write_text(
        json.dumps(
            {
                "generated_at": "2026-01-15T14:30:00+00:00",
                "selected_account": "U1",
                "accounts": [
                    {
                        "account": "U1",
                        "tags": {
                            "NetLiquidation": {"numeric_value": 380.64, "currency": "USD"},
                            "TotalCashValue": {"numeric_value": 380.33, "currency": "USD"},
                            "AvailableFunds": {"numeric_value": 379.19, "currency": "USD"},
                            "BuyingPower": {"numeric_value": 379.19, "currency": "USD"},
                            "ExcessLiquidity": {"numeric_value": 379.19, "currency": "USD"},
                            "InitMarginReq": {"numeric_value": 1.41, "currency": "USD"},
                            "MaintMarginReq": {"numeric_value": 1.41, "currency": "USD"},
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    report = build_capital_margin_state(
        account_snapshot_path=snapshot_path,
        previous_state_path=None,
        now=datetime(2026, 1, 15, 14, 31, tzinfo=UTC),
    )
    budgets = report["budgets"]
    overlay = budgets["micro_cash_budget_overlay"]

    assert report["status"] == "green"
    assert report["leverage_mode"] == "cash_only"
    assert budgets["budget_model"] == "micro_cash_overlay"
    assert budgets["options_premium_budget"] == 106.47
    assert budgets["core_risk_budget"] == 106.47
    assert overlay["eligible"] is True
    assert overlay["reason"] == "eligible"
    assert overlay["general_options_premium_budget"] == 11.42
    assert overlay["general_core_risk_budget"] == 30.45
    assert overlay["cash_fraction_budget"] == 106.47
    assert overlay["net_liq_fraction_budget"] == 114.19
    assert "micro_cash_overlay" in capital_margin_state_markdown(report)


def test_capital_margin_state_detects_withdrawal_candidate(tmp_path) -> None:
    snapshot_path = tmp_path / "account_snapshot.json"
    previous_path = tmp_path / "previous_state.json"
    previous_path.write_text(
        json.dumps({"metrics": {"net_liquidation": 100000.0, "total_cash_value": 50000.0}}),
        encoding="utf-8",
    )
    snapshot_path.write_text(
        json.dumps(
            {
                "generated_at": "2026-01-15T14:30:00+00:00",
                "selected_account": "U1",
                "accounts": [
                    {
                        "account": "U1",
                        "tags": {
                            "NetLiquidation": {"numeric_value": 78000.0},
                            "TotalCashValue": {"numeric_value": 25000.0},
                            "AvailableFunds": {"numeric_value": 22000.0},
                            "BuyingPower": {"numeric_value": 90000.0},
                            "ExcessLiquidity": {"numeric_value": 26000.0},
                            "InitMarginReq": {"numeric_value": 10000.0},
                            "MaintMarginReq": {"numeric_value": 8000.0},
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    report = build_capital_margin_state(
        account_snapshot_path=snapshot_path,
        previous_state_path=previous_path,
        now=datetime(2026, 1, 15, 14, 35, tzinfo=UTC),
    )

    assert report["cash_flow"]["external_cash_flow_candidate"] is True
    assert report["cash_flow"]["direction"] == "withdrawal_candidate"
    assert "Rebaseline portfolio budgets" in " ".join(report["action_guidance"])


def test_capital_margin_state_missing_snapshot_is_red(tmp_path) -> None:
    report = build_capital_margin_state(
        account_snapshot_path=tmp_path / "missing.json",
        previous_state_path=None,
        now=datetime(2026, 1, 15, 14, 35, tzinfo=UTC),
    )

    assert report["status"] == "red"
    assert "missing_account_snapshot" in report["status_reasons"]
    assert report["budgets"]["core_risk_budget"] == 0.0
    assert report["withdrawal_guard"]["max_safe_withdrawal"] == 0.0
    assert report["withdrawal_guard"]["requires_fresh_account_snapshot"] is True


def test_capital_margin_state_stale_snapshot_blocks_safe_withdrawal_but_reports_theoretical(tmp_path) -> None:
    snapshot_path = tmp_path / "account_snapshot.json"
    snapshot_path.write_text(
        json.dumps(
            {
                "generated_at": "2026-01-15T13:00:00+00:00",
                "selected_account": "U1",
                "accounts": [
                    {
                        "account": "U1",
                        "tags": {
                            "NetLiquidation": {"numeric_value": 100000.0},
                            "TotalCashValue": {"numeric_value": 35000.0},
                            "AvailableFunds": {"numeric_value": 50000.0},
                            "BuyingPower": {"numeric_value": 200000.0},
                            "ExcessLiquidity": {"numeric_value": 65000.0},
                            "InitMarginReq": {"numeric_value": 15000.0},
                            "MaintMarginReq": {"numeric_value": 10000.0},
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    report = build_capital_margin_state(
        account_snapshot_path=snapshot_path,
        previous_state_path=None,
        now=datetime(2026, 1, 15, 14, 35, tzinfo=UTC),
    )

    assert report["status"] == "red"
    assert report["withdrawal_guard"]["status"] == "blocked_stale_or_missing_snapshot"
    assert report["withdrawal_guard"]["max_safe_withdrawal"] == 0.0
    assert report["withdrawal_guard"]["theoretical_max_withdrawal_if_snapshot_fresh"] == 18718.75


def _capital_state_for_m239(**budget_overrides):
    budgets = {
        "deployable_cash_after_reserve": 300.0,
        "core_risk_budget": 30.0,
        "options_premium_budget": 11.0,
        "max_single_position_notional": 45.0,
        "max_new_initial_margin": 95.0,
    }
    budgets.update(budget_overrides)
    return {
        "capital_margin_state_version": "m238-capital-withdrawal-guard-v1",
        "generated_at": "2026-01-15T14:30:00+00:00",
        "status": "green",
        "status_reasons": ["capital_margin_buffer_normal"],
        "leverage_mode": "cash_only",
        "selected_account": "U1",
        "budgets": budgets,
        "withdrawal_guard": {"max_safe_withdrawal": 0.0, "requires_fresh_account_snapshot": False},
    }


def _auto_gate_item(*, ticket_type="stock", action="BUY", quantity=10.0, cash=-1000.0, margin=1000.0, risk=20.0, notional=1000.0):
    return {
        "auto_execution_status": "auto_execution_ready",
        "symbol": "SPY",
        "ticket_type": ticket_type,
        "action": action,
        "quantity": quantity,
        "checks": [],
        "block_reasons": [],
        "local_margin_estimate": {
            "estimated_cash_impact": cash,
            "estimated_initial_margin_change": margin,
            "estimated_maintenance_margin_change": margin,
            "estimated_max_loss_proxy": risk,
        },
        "order_intent": {
            "order_preview": {
                "action": "BUY" if action == "BUY" else "SELL",
                "source_order_action": action,
                "total_quantity": quantity,
                "what_if": True,
                "transmit": False,
            }
        },
        "source_preview_item": {
            "ticket_draft": {
                "ticket_type": ticket_type,
                "order_action": action,
                "quantity": quantity,
                "estimated_notional": notional,
                "estimated_max_loss": risk,
            },
            "ibkr_what_if_order_preview": {
                "action": "BUY" if action == "BUY" else "SELL",
                "source_order_action": action,
                "total_quantity": quantity,
                "what_if": True,
                "transmit": False,
            },
        },
    }


def test_capital_aware_action_gate_resizes_stock_to_fit_micro_budget() -> None:
    auto_gate = {
        "autonomous_execution_gate_version": "paper-autonomous-execution-gate-v1",
        "auto_execution_items": [_auto_gate_item()],
    }

    report = build_capital_aware_action_gate_report(
        auto_gate,
        _capital_state_for_m239(),
        reduce_to_fit_capacity=True,
        allow_fractional_shares=True,
    )

    item = report["auto_execution_items"][0]
    assert report["capital_ready_count"] == 1
    assert report["capital_resized_count"] == 1
    assert item["capital_gate_status"] == "capital_resized_ready"
    assert item["quantity"] == 0.45
    assert item["order_intent"]["order_preview"]["total_quantity"] == 0.45
    assert report["used_budget"]["single_position_notional_budget"] == 45.0
    assert "M239 Capital-Aware Action Sizing Gate" in capital_aware_action_gate_markdown(report)


def test_capital_aware_action_gate_blocks_option_premium_over_budget() -> None:
    auto_gate = {
        "autonomous_execution_gate_version": "paper-autonomous-execution-gate-v1",
        "auto_execution_items": [
            _auto_gate_item(ticket_type="option_combo_debit_spread", quantity=1.0, cash=-60.0, margin=60.0, risk=60.0, notional=60.0)
        ],
    }

    report = build_capital_aware_action_gate_report(auto_gate, _capital_state_for_m239(), reduce_to_fit_capacity=True)

    item = report["auto_execution_items"][0]
    assert report["capital_ready_count"] == 0
    assert item["auto_execution_status"] == "auto_execution_blocked"
    assert "option_contract_cannot_be_fractionally_resized" in item["capital_block_reasons"]
    assert "options_premium_budget_available" in item["capital_block_reasons"]


def test_capital_aware_action_gate_blocks_stale_or_red_capital_state() -> None:
    capital_state = _capital_state_for_m239()
    capital_state["status"] = "red"
    capital_state["status_reasons"] = ["stale_account_snapshot"]
    capital_state["withdrawal_guard"]["requires_fresh_account_snapshot"] = True
    auto_gate = {
        "autonomous_execution_gate_version": "paper-autonomous-execution-gate-v1",
        "auto_execution_items": [_auto_gate_item(quantity=0.01, cash=-1.0, margin=1.0, risk=0.1, notional=1.0)],
    }

    report = build_capital_aware_action_gate_report(auto_gate, capital_state)

    item = report["auto_execution_items"][0]
    assert report["capital_ready_count"] == 0
    assert item["capital_gate_status"] == "capital_blocked"
    assert "capital_state_red_blocks_new_entries" in item["capital_block_reasons"]
    assert "capital_state_snapshot_not_fresh" in item["capital_block_reasons"]
