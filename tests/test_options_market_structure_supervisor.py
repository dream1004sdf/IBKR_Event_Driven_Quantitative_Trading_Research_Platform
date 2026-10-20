from __future__ import annotations

import json
from pathlib import Path

from recorder.options_market_structure_supervisor import (
    OptionsMarketStructureSupervisorConfig,
    load_options_market_structure_supervisor_config,
    run_options_market_structure_supervisor,
)


def _write_json(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def _config(tmp_path: Path, *, execute_actions: bool = False) -> OptionsMarketStructureSupervisorConfig:
    return OptionsMarketStructureSupervisorConfig(
        symbols=["SPY", "QQQ"],
        optional_index_symbols=[],
        reference_prices={},
        underlying_sec_types={},
        underlying_exchanges={},
        quote_json_path=tmp_path / "quote.json",
        quote_markdown_path=tmp_path / "quote.md",
        structure_json_path=tmp_path / "structure.json",
        structure_markdown_path=tmp_path / "structure.md",
        market_data_json_path=tmp_path / "market_data.json",
        state_json_path=tmp_path / "state.json",
        tick_log_jsonl_path=tmp_path / "ticks.jsonl",
        execute_actions=execute_actions,
        max_snapshot_age_minutes=30.0,
        quote_refresh_cooldown_seconds=1.0,
        structure_refresh_cooldown_seconds=1.0,
    )


def test_options_market_structure_supervisor_defers_quote_refresh_outside_regular_session(tmp_path: Path) -> None:
    _write_json(tmp_path / "quote.json", {"snapshot_version": "ibkr-option-quote-snapshot-v1", "generated_at": "2026-06-30T12:00:00+00:00", "quotes": []})
    config = _config(tmp_path)

    result = run_options_market_structure_supervisor(
        config=config,
        cwd=tmp_path,
        now="2026-07-01T08:00:00-04:00",
    )
    actions = result["tick_summaries"][0]["actions"]

    assert result["options_market_structure_supervisor_version"] == "options-market-structure-refresh-supervisor-v9-operational-runbook-tuning"
    assert result["final_status"] == "yellow"
    assert result["final_session"] == "pre_market"
    assert actions[0]["action_id"] == "fresh_option_quote_snapshot"
    assert actions[0]["status"] == "deferred_until_regular_session"
    assert actions[1]["action_id"] == "options_market_structure_rebuild"
    assert actions[1]["status"] == "planned_execute_actions_false"
    assert result["option_quote_deferred_operating_state"]["status"] == "normal_session_deferred"
    assert result["option_quote_deferred_operating_state"]["alert_policy"] == "do_not_alert_as_failure"
    assert result["m220_operational_runbook"]["regular_session_required"] is True
    assert "options-market-structure-supervisor" in result["m220_operational_runbook"]["one_command"]
    assert result["safety"]["ibkr_order_submitted"] is False


def test_options_market_structure_supervisor_executes_regular_session_refresh_and_rebuild(tmp_path: Path) -> None:
    _write_json(tmp_path / "quote.json", {"snapshot_version": "ibkr-option-quote-snapshot-v1", "generated_at": "2026-07-01T13:00:00+00:00", "quotes": []})
    config = _config(tmp_path, execute_actions=True)
    commands: list[list[str]] = []

    def runner(command: list[str], cwd: Path, timeout_seconds: float) -> dict:
        commands.append(command)
        if any("ibkr-options-quote-snapshot" in part for part in command):
            _write_json(
                tmp_path / "quote.json",
                {
                    "snapshot_version": "ibkr-option-quote-snapshot-v1",
                    "generated_at": "2026-07-01T14:00:00+00:00",
                    "requested_quotes": 4,
                    "quoted_contracts": 4,
                    "quoted_rate": 1.0,
                    "avg_spread_pct": 0.05,
                    "quotes": [{"symbol": "SPY"}, {"symbol": "SPY"}, {"symbol": "QQQ"}, {"symbol": "QQQ"}],
                    "by_symbol": [
                        {"symbol": "SPY", "requested": 2, "quoted": 2, "quoted_rate": 1.0, "avg_spread_pct": 0.05},
                        {"symbol": "QQQ", "requested": 2, "quoted": 2, "quoted_rate": 1.0, "avg_spread_pct": 0.05},
                    ],
                },
            )
        return {"status": "succeeded", "returncode": 0, "stdout_tail": "ok", "stderr_tail": "", "duration_seconds": 0.01, "error": None}

    result = run_options_market_structure_supervisor(
        config=config,
        cwd=tmp_path,
        command_runner=runner,
        now="2026-07-01T10:00:00-04:00",
    )
    actions = result["tick_summaries"][0]["actions"]

    assert result["final_status"] == "green"
    assert result["final_quote_quality_status"] == "green"
    assert result["final_artifact_delta"]["outcome"] == "improved"
    assert result["symbol_status"]["SPY"]["last_status"] == "succeeded"
    assert result["symbol_status"]["QQQ"]["last_success_at"] is not None
    assert result["final_session"] == "regular"
    assert [action["status"] for action in actions] == ["succeeded", "succeeded", "succeeded"]
    assert any("ibkr-options-quote-snapshot" in command for command in commands[0])
    assert any("options-market-structure-desk" in command for command in commands[1])
    assert any("option-structure-outcome-build" in command for command in commands[2])
    assert actions[2]["action_id"] == "m222_option_structure_outcome_log"
    assert result["final_m222_failed_action_count"] == 0
    assert len(result["final_m222_option_structure_outcome_actions"]) == 1
    assert (tmp_path / "state.json").exists()


def test_options_market_structure_supervisor_profile_resolves_paths(tmp_path: Path) -> None:
    profile = _write_json(
        tmp_path / "profile.json",
        {
            "symbols": ["SPY", "QQQ", "IWM"],
            "symbol_groups": {"optional_index_options": ["SPX", "NDX"]},
            "underlying_sec_types": {"SPX": "IND", "NDX": "IND"},
            "underlying_exchanges": {"SPX": "CBOE", "NDX": "NASDAQ"},
            "paths": {
                "quote_json_path": "quote.json",
                "quote_markdown_path": "quote.md",
                "structure_json_path": "structure.json",
                "structure_markdown_path": "structure.md",
                "market_data_json_path": "market.json",
                "index_option_capability_probe_json_path": "probe.json",
                "index_option_capability_probe_markdown_path": "probe.md",
            },
            "schedule": {"max_ticks": 2, "interval_seconds": 5, "execute_actions": False, "capability_probe_cooldown_seconds": 7.0},
            "quote_snapshot": {
                "strikes_around": 3,
                "max_quote_contracts": 120,
                "max_quote_contracts_by_phase": {"regular_close_window": 150},
                "optional_index_max_quote_contracts_by_phase": {"regular_close_window": 90},
                "index_family_quote_budget_weights": {"sp500": 1.0},
            },
            "index_option_families": [
                {
                    "family_id": "sp500",
                    "label": "S&P 500",
                    "direct_symbols": ["SPX"],
                    "mini_symbols": ["XSP"],
                    "proxy_symbols": ["SPY"],
                }
            ],
        },
    )

    config = load_options_market_structure_supervisor_config(profile)

    assert config.symbols == ["SPY", "QQQ", "IWM"]
    assert config.optional_index_symbols == ["SPX", "NDX"]
    assert config.underlying_sec_types == {"SPX": "IND", "NDX": "IND"}
    assert config.underlying_exchanges == {"SPX": "CBOE", "NDX": "NASDAQ"}
    assert config.quote_json_path == tmp_path / "quote.json"
    assert config.max_ticks == 2
    assert config.interval_seconds == 5
    assert config.strikes_around == 3
    assert config.max_quote_contracts == 120
    assert config.max_quote_contracts_by_phase == {"regular_close_window": 150}
    assert config.optional_index_max_quote_contracts_by_phase == {"regular_close_window": 90}
    assert config.index_family_quote_budget_weights == {"sp500": 1.0}
    assert config.capability_probe_cooldown_seconds == 7.0
    assert config.index_option_capability_probe_json_path == tmp_path / "probe.json"
    assert config.index_option_capability_probe_markdown_path == tmp_path / "probe.md"
    assert config.index_option_families[0]["direct_symbols"] == ["SPX"]
    assert config.option_structure_outcome_enabled is True
    assert config.option_structure_outcome_json_path is not None
    assert config.option_structure_outcome_json_path.name == "m222_option_structure_outcome_log.json"


def test_options_market_structure_supervisor_optional_index_failure_does_not_red_core(tmp_path: Path) -> None:
    _write_json(tmp_path / "quote.json", {"snapshot_version": "ibkr-option-quote-snapshot-v1", "generated_at": "2026-07-01T13:59:00+00:00", "quotes": []})
    config = OptionsMarketStructureSupervisorConfig(
        symbols=["SPY"],
        optional_index_symbols=["SPX"],
        reference_prices={},
        underlying_sec_types={"SPX": "IND"},
        underlying_exchanges={"SPX": "CBOE"},
        quote_json_path=tmp_path / "quote.json",
        quote_markdown_path=tmp_path / "quote.md",
        structure_json_path=tmp_path / "structure.json",
        structure_markdown_path=tmp_path / "structure.md",
        optional_index_quote_json_path=tmp_path / "index_quote.json",
        optional_index_quote_markdown_path=tmp_path / "index_quote.md",
        optional_index_structure_json_path=tmp_path / "index_structure.json",
        optional_index_structure_markdown_path=tmp_path / "index_structure.md",
        execute_actions=True,
        max_snapshot_age_minutes=30.0,
    )

    def runner(command: list[str], cwd: Path, timeout_seconds: float) -> dict:
        if "SPX" in command:
            assert "--underlying-sec-types" in command
            assert "SPX=IND" in command
            assert "--underlying-exchanges" in command
            assert "SPX=CBOE" in command
            return {"status": "failed", "returncode": 1, "stdout_tail": "", "stderr_tail": "index unavailable", "duration_seconds": 0.01, "error": None}
        return {"status": "succeeded", "returncode": 0, "stdout_tail": "ok", "stderr_tail": "", "duration_seconds": 0.01, "error": None}

    result = run_options_market_structure_supervisor(
        config=config,
        cwd=tmp_path,
        command_runner=runner,
        now="2026-07-01T10:00:00-04:00",
    )

    assert result["final_status"] == "yellow"
    assert result["tick_summaries"][0]["core_failed_action_count"] == 0
    assert result["tick_summaries"][0]["optional_failed_action_count"] == 1


def test_options_market_structure_supervisor_classifies_failure_and_backs_off_symbol(tmp_path: Path) -> None:
    config = OptionsMarketStructureSupervisorConfig(
        symbols=["SPY"],
        optional_index_symbols=[],
        reference_prices={},
        underlying_sec_types={},
        underlying_exchanges={},
        quote_json_path=tmp_path / "quote.json",
        quote_markdown_path=tmp_path / "quote.md",
        structure_json_path=tmp_path / "structure.json",
        structure_markdown_path=tmp_path / "structure.md",
        execute_actions=True,
        max_snapshot_age_minutes=30.0,
        symbol_failure_backoff_seconds=90.0,
    )

    def runner(command: list[str], cwd: Path, timeout_seconds: float) -> dict:
        return {
            "status": "failed",
            "returncode": 1,
            "stdout_tail": "",
            "stderr_tail": "No underlying contract resolved for SPY",
            "duration_seconds": 0.01,
            "error": None,
        }

    result = run_options_market_structure_supervisor(
        config=config,
        cwd=tmp_path,
        command_runner=runner,
        now="2026-07-01T10:00:00-04:00",
    )

    failed = result["failed_actions"][0]
    assert failed["failure_reason"] == "contract_unresolved"
    assert result["final_failure_reason_counts"] == {"contract_unresolved": 1}
    assert result["symbol_failure_notes"]["fresh_option_quote_snapshot:SPY"]["reason"] == "contract_unresolved"
    assert result["symbol_status"]["SPY"]["last_status"] == "failed"
    assert result["symbol_status"]["SPY"]["last_failure_reason"] == "contract_unresolved"
    assert result["final_status"] == "red"


def test_options_market_structure_supervisor_close_window_uses_stricter_freshness(tmp_path: Path) -> None:
    _write_json(
        tmp_path / "quote.json",
        {
            "snapshot_version": "ibkr-option-quote-snapshot-v1",
            "generated_at": "2026-07-01T18:45:00+00:00",
            "requested_quotes": 2,
            "quoted_contracts": 2,
            "quoted_rate": 1.0,
            "avg_spread_pct": 0.05,
            "quotes": [{"symbol": "SPY"}, {"symbol": "SPY"}],
            "by_symbol": [{"symbol": "SPY", "requested": 2, "quoted": 2, "quoted_rate": 1.0, "avg_spread_pct": 0.05}],
        },
    )
    config = OptionsMarketStructureSupervisorConfig(
        symbols=["SPY"],
        optional_index_symbols=[],
        reference_prices={},
        underlying_sec_types={},
        underlying_exchanges={},
        quote_json_path=tmp_path / "quote.json",
        quote_markdown_path=tmp_path / "quote.md",
        structure_json_path=tmp_path / "structure.json",
        structure_markdown_path=tmp_path / "structure.md",
        max_snapshot_age_minutes=30.0,
        close_window_max_snapshot_age_minutes=10.0,
        close_refresh_window_minutes=60.0,
    )

    result = run_options_market_structure_supervisor(
        config=config,
        cwd=tmp_path,
        now="2026-07-01T15:00:00-04:00",
    )

    tick = result["tick_summaries"][0]
    assert result["final_session_refresh_phase"] == "regular_close_window"
    assert result["final_effective_max_snapshot_age_minutes"] == 10.0
    assert tick["snapshot_status"]["quote_fresh"] is False
    assert tick["actions"][0]["action_id"] == "fresh_option_quote_snapshot"
    assert tick["actions"][0]["status"] == "planned_execute_actions_false"


def test_options_market_structure_supervisor_open_warmup_uses_relaxed_freshness(tmp_path: Path) -> None:
    _write_json(
        tmp_path / "quote.json",
        {
            "snapshot_version": "ibkr-option-quote-snapshot-v1",
            "generated_at": "2026-07-01T13:00:00+00:00",
            "requested_quotes": 2,
            "quoted_contracts": 2,
            "quoted_rate": 1.0,
            "avg_spread_pct": 0.05,
            "quotes": [{"symbol": "SPY"}, {"symbol": "SPY"}],
            "by_symbol": [{"symbol": "SPY", "requested": 2, "quoted": 2, "quoted_rate": 1.0, "avg_spread_pct": 0.05}],
        },
    )
    config = OptionsMarketStructureSupervisorConfig(
        symbols=["SPY"],
        optional_index_symbols=[],
        reference_prices={},
        underlying_sec_types={},
        underlying_exchanges={},
        quote_json_path=tmp_path / "quote.json",
        quote_markdown_path=tmp_path / "quote.md",
        structure_json_path=tmp_path / "structure.json",
        structure_markdown_path=tmp_path / "structure.md",
        max_snapshot_age_minutes=30.0,
        warmup_max_snapshot_age_minutes=45.0,
        open_warmup_minutes=15.0,
    )

    result = run_options_market_structure_supervisor(
        config=config,
        cwd=tmp_path,
        now="2026-07-01T09:40:00-04:00",
    )

    tick = result["tick_summaries"][0]
    assert result["final_session_refresh_phase"] == "regular_open_warmup"
    assert result["final_effective_max_snapshot_age_minutes"] == 45.0
    assert tick["snapshot_status"]["quote_fresh"] is True
    assert tick["actions"][0]["status"] == "not_needed"


def test_options_market_structure_supervisor_isolates_repeated_symbol_failures(tmp_path: Path) -> None:
    config = OptionsMarketStructureSupervisorConfig(
        symbols=["SPY"],
        optional_index_symbols=[],
        reference_prices={},
        underlying_sec_types={},
        underlying_exchanges={},
        quote_json_path=tmp_path / "quote.json",
        quote_markdown_path=tmp_path / "quote.md",
        structure_json_path=tmp_path / "structure.json",
        structure_markdown_path=tmp_path / "structure.md",
        state_json_path=tmp_path / "state.json",
        execute_actions=True,
        max_snapshot_age_minutes=30.0,
        symbol_failure_backoff_seconds=0.0,
        max_symbol_failures_before_isolation=2,
        symbol_isolation_seconds=600.0,
    )

    def runner(command: list[str], cwd: Path, timeout_seconds: float) -> dict:
        return {
            "status": "failed",
            "returncode": 1,
            "stdout_tail": "",
            "stderr_tail": "No underlying contract resolved for SPY",
            "duration_seconds": 0.01,
            "error": None,
        }

    first = run_options_market_structure_supervisor(
        config=config,
        cwd=tmp_path,
        command_runner=runner,
        now="2026-07-01T10:00:00-04:00",
    )
    second = run_options_market_structure_supervisor(
        config=config,
        cwd=tmp_path,
        command_runner=runner,
        now="2026-07-01T10:01:00-04:00",
    )
    third = run_options_market_structure_supervisor(
        config=config,
        cwd=tmp_path,
        command_runner=runner,
        now="2026-07-01T10:02:00-04:00",
    )

    assert first["symbol_status"]["SPY"]["failure_streak"] == 1
    assert second["symbol_status"]["SPY"]["last_status"] == "isolated"
    assert second["isolated_symbol_count"] == 1
    assert "fresh_option_quote_snapshot:SPY" in second["isolated_symbols_by_action"]
    assert third["tick_summaries"][0]["actions"][0]["status"] == "symbol_cooldown"
    assert third["tick_summaries"][0]["actions"][1]["status"] == "cooldown"


def test_options_market_structure_supervisor_flags_delayed_option_quotes(tmp_path: Path) -> None:
    _write_json(
        tmp_path / "quote.json",
        {
            "snapshot_version": "ibkr-option-quote-snapshot-v1",
            "generated_at": "2026-07-01T14:00:00+00:00",
            "requested_quotes": 2,
            "quoted_contracts": 2,
            "quoted_rate": 1.0,
            "avg_spread_pct": 0.05,
            "quotes": [
                {"symbol": "SPY", "quote_status": "quoted", "market_data_type": 3, "delayed": True},
                {"symbol": "SPY", "quote_status": "quoted", "market_data_type": 3, "delayed": True},
            ],
            "by_symbol": [{"symbol": "SPY", "requested": 2, "quoted": 2, "quoted_rate": 1.0, "avg_spread_pct": 0.05}],
        },
    )
    config = OptionsMarketStructureSupervisorConfig(
        symbols=["SPY"],
        optional_index_symbols=[],
        reference_prices={},
        underlying_sec_types={},
        underlying_exchanges={},
        quote_json_path=tmp_path / "quote.json",
        quote_markdown_path=tmp_path / "quote.md",
        structure_json_path=tmp_path / "structure.json",
        structure_markdown_path=tmp_path / "structure.md",
        max_snapshot_age_minutes=30.0,
    )

    result = run_options_market_structure_supervisor(
        config=config,
        cwd=tmp_path,
        now="2026-07-01T10:00:00-04:00",
    )

    assert result["final_quote_quality_status"] == "red"
    assert result["final_quote_data_latency_status"] == "delayed_only"
    assert result["final_quote_delayed_contracts"] == 2
    assert result["final_quote_delayed_symbols"] == ["SPY"]


def test_options_market_structure_supervisor_builds_index_family_coverage_and_budgets(tmp_path: Path) -> None:
    _write_json(
        tmp_path / "quote.json",
        {
            "snapshot_version": "ibkr-option-quote-snapshot-v1",
            "generated_at": "2026-07-01T19:50:00+00:00",
            "requested_quotes": 2,
            "quoted_contracts": 2,
            "quoted_rate": 1.0,
            "avg_spread_pct": 0.05,
            "quotes": [{"symbol": "SPY"}, {"symbol": "SPY"}],
            "by_symbol": [{"symbol": "SPY", "requested": 2, "quoted": 2, "quoted_rate": 1.0, "avg_spread_pct": 0.05}],
        },
    )
    config = OptionsMarketStructureSupervisorConfig(
        symbols=["SPY"],
        optional_index_symbols=["SPX", "XSP"],
        index_option_families=[
            {
                "family_id": "sp500",
                "label": "S&P 500",
                "direct_symbols": ["SPX"],
                "mini_symbols": ["XSP"],
                "proxy_symbols": ["SPY"],
            }
        ],
        reference_prices={},
        underlying_sec_types={"SPX": "IND", "XSP": "IND"},
        underlying_exchanges={"SPX": "CBOE", "XSP": "CBOE"},
        quote_json_path=tmp_path / "quote.json",
        quote_markdown_path=tmp_path / "quote.md",
        structure_json_path=tmp_path / "structure.json",
        structure_markdown_path=tmp_path / "structure.md",
        optional_index_quote_json_path=tmp_path / "index_quote.json",
        optional_index_quote_markdown_path=tmp_path / "index_quote.md",
        optional_index_structure_json_path=tmp_path / "index_structure.json",
        optional_index_structure_markdown_path=tmp_path / "index_structure.md",
        max_snapshot_age_minutes=30.0,
        close_window_max_snapshot_age_minutes=10.0,
        max_quote_contracts=240,
        max_quote_contracts_by_phase={"regular_close_window": 300},
        optional_index_max_quote_contracts_by_phase={"regular_close_window": 120},
    )

    result = run_options_market_structure_supervisor(
        config=config,
        cwd=tmp_path,
        now="2026-07-01T15:55:00-04:00",
    )

    matrix = result["index_option_coverage_matrix"]
    family = matrix["families"][0]
    assert result["final_session_refresh_phase"] == "regular_close_window"
    assert result["final_quote_contract_budgets"] == {"core": 300, "optional_index": 120}
    assert family["coverage_status"] == "proxy_only"
    assert family["confidence"] == "medium"
    assert family["proxy"]["ok_symbols"] == ["SPY"]
    assert family["repair_plan"]["operating_mode"] == "proxy_fallback_active"
    assert family["repair_plan"]["proxy_fallback_active"] is True
    assert result["m220_family_issue_summary"]["proxy_fallback_families"] == ["S&P 500"]
    assert result["index_option_capability_cache"]["families"][0]["proxy_ok_symbols"] == ["SPY"]


def test_options_market_structure_supervisor_uses_probe_mini_fallback_and_m220_checklist(tmp_path: Path) -> None:
    _write_json(
        tmp_path / "quote.json",
        {
            "snapshot_version": "ibkr-option-quote-snapshot-v1",
            "generated_at": "2026-07-01T14:00:00+00:00",
            "requested_quotes": 2,
            "quoted_contracts": 2,
            "quoted_rate": 1.0,
            "avg_spread_pct": 0.05,
            "config": {"symbols": ["SPY"]},
            "quotes": [{"symbol": "SPY"}, {"symbol": "SPY"}],
            "by_symbol": [{"symbol": "SPY", "requested": 2, "quoted": 2, "quoted_rate": 1.0, "avg_spread_pct": 0.05}],
        },
    )
    _write_json(
        tmp_path / "index_quote.json",
        {
            "snapshot_version": "ibkr-option-quote-snapshot-v1",
            "generated_at": "2026-07-01T14:00:00+00:00",
            "requested_quotes": 2,
            "quoted_contracts": 2,
            "quoted_rate": 1.0,
            "avg_spread_pct": 0.05,
            "config": {"symbols": ["XSP"]},
            "quotes": [{"symbol": "XSP"}, {"symbol": "XSP"}],
            "by_symbol": [{"symbol": "XSP", "requested": 2, "quoted": 2, "quoted_rate": 1.0, "avg_spread_pct": 0.05}],
        },
    )
    _write_json(
        tmp_path / "probe.json",
        {
            "generated_at": "2026-07-01T13:59:00+00:00",
            "completed": True,
            "symbols": ["SPX", "XSP", "SPY"],
            "by_symbol": [
                {"symbol": "SPX", "candidate_contracts": 0, "chain_rows": 1},
                {"symbol": "XSP", "candidate_contracts": 8, "chain_rows": 2},
                {"symbol": "SPY", "candidate_contracts": 12, "chain_rows": 2},
            ],
        },
    )
    config = OptionsMarketStructureSupervisorConfig(
        symbols=["SPY"],
        optional_index_symbols=["SPX", "XSP"],
        index_option_families=[
            {
                "family_id": "sp500",
                "label": "S&P 500",
                "direct_symbols": ["SPX"],
                "mini_symbols": ["XSP"],
                "proxy_symbols": ["SPY"],
            }
        ],
        reference_prices={},
        underlying_sec_types={"SPX": "IND", "XSP": "IND"},
        underlying_exchanges={"SPX": "CBOE", "XSP": "CBOE"},
        quote_json_path=tmp_path / "quote.json",
        quote_markdown_path=tmp_path / "quote.md",
        structure_json_path=tmp_path / "structure.json",
        structure_markdown_path=tmp_path / "structure.md",
        optional_index_quote_json_path=tmp_path / "index_quote.json",
        optional_index_quote_markdown_path=tmp_path / "index_quote.md",
        optional_index_structure_json_path=tmp_path / "index_structure.json",
        optional_index_structure_markdown_path=tmp_path / "index_structure.md",
        index_option_capability_probe_json_path=tmp_path / "probe.json",
        max_snapshot_age_minutes=30.0,
        max_quote_contracts_by_phase={"regular_midday": 220},
        optional_index_max_quote_contracts_by_phase={"regular_midday": 100},
        index_family_quote_budget_weights={"sp500": 1.0},
    )

    result = run_options_market_structure_supervisor(
        config=config,
        cwd=tmp_path,
        now="2026-07-01T10:00:00-04:00",
    )

    family = result["index_option_coverage_matrix"]["families"][0]
    assert result["final_quote_contract_budgets"] == {"core": 220, "optional_index": 100}
    assert result["index_family_quote_budget_allocation"] == {"sp500": 100}
    assert family["selected_source_type"] == "mini"
    assert family["selected_symbols"] == ["XSP"]
    assert family["fallback_reason"] == "direct_unavailable_using_mini_index"
    assert family["family_quote_budget"] == 100
    assert family["direct"]["rows"][0]["probe_status"] == "chain_available_no_candidates"
    assert family["direct"]["rows"][0]["failure_reason"] == "chain_available_but_no_candidate_contracts_selected"
    assert family["direct"]["rows"][0]["issue_category"] == "chain_available_no_selected_contracts"
    assert family["mini"]["rows"][0]["candidate_contracts"] == 8
    assert family["repair_plan"]["operating_mode"] == "direct_or_mini_usable"
    assert result["index_family_budget_tuning_observations"][0]["allocated_budget"] == 100
    assert result["index_family_budget_tuning_observations"][0]["selected_source_type"] == "mini"
    assert result["index_family_budget_tuning_observations"][0]["suggested_budget_bias"] == "maintain_or_slightly_increase_if_close_window_relevant"
    assert result["m220_family_issue_summary"]["issue_category_counts"]["chain_available_no_selected_contracts"] == 1
    assert result["m220_live_validation_checklist"][1]["role"] == "mini"
    assert result["m220_live_validation_checklist"][1]["passed"] is True
    assert result["m220_operational_runbook"]["checklist_row_count"] == 3
    assert result["index_option_capability_cache"]["families"][0]["selected_source_type"] == "mini"


def test_options_market_structure_supervisor_keeps_dia_as_probe_backed_dow_proxy_when_quotes_are_thin(tmp_path: Path) -> None:
    _write_json(
        tmp_path / "quote.json",
        {
            "snapshot_version": "ibkr-option-quote-snapshot-v1",
            "generated_at": "2026-07-01T14:00:00+00:00",
            "requested_quotes": 54,
            "quoted_contracts": 0,
            "quoted_rate": 0.0,
            "avg_spread_pct": None,
            "config": {"symbols": ["DIA"]},
            "quotes": [],
            "by_symbol": [{"symbol": "DIA", "requested": 54, "quoted": 0, "quoted_rate": 0.0, "avg_spread_pct": None}],
        },
    )
    _write_json(
        tmp_path / "probe.json",
        {
            "generated_at": "2026-07-01T13:59:00+00:00",
            "completed": True,
            "symbols": ["DIA"],
            "by_symbol": [
                {"symbol": "DIA", "status": "candidate_contracts_available", "candidate_contracts": 54, "chain_rows": 20}
            ],
        },
    )
    config = OptionsMarketStructureSupervisorConfig(
        symbols=["DIA"],
        optional_index_symbols=[],
        index_option_families=[
            {
                "family_id": "dow30",
                "label": "Dow / Industrials",
                "direct_symbols": [],
                "mini_symbols": [],
                "proxy_symbols": ["DIA", "YM"],
            }
        ],
        reference_prices={},
        underlying_sec_types={},
        underlying_exchanges={},
        quote_json_path=tmp_path / "quote.json",
        quote_markdown_path=tmp_path / "quote.md",
        structure_json_path=tmp_path / "structure.json",
        structure_markdown_path=tmp_path / "structure.md",
        index_option_capability_probe_json_path=tmp_path / "probe.json",
        max_snapshot_age_minutes=30.0,
    )

    result = run_options_market_structure_supervisor(
        config=config,
        cwd=tmp_path,
        now="2026-07-01T10:00:00-04:00",
    )

    family = result["index_option_coverage_matrix"]["families"][0]
    assert family["coverage_status"] == "proxy_probe_only"
    assert family["confidence"] == "low"
    assert family["selected_source_type"] == "proxy_probe"
    assert family["selected_symbols"] == ["DIA"]
    assert family["proxy"]["probe_candidate_symbols"] == ["DIA"]
    assert family["repair_plan"]["operating_mode"] == "proxy_probe_backed_quote_gap"
    assert family["repair_plan"]["proxy_probe_backed"] is True
    assert result["m220_family_issue_summary"]["proxy_probe_backed_families"] == ["Dow / Industrials"]
    assert result["m220_family_issue_summary"]["coverage_unavailable_families"] == []
    assert result["final_quote_quality_reason_counts"]["too_few_quoted_contracts"] == 1
    assert result["index_family_budget_tuning_observations"][0]["suggested_budget_bias"] == "increase_proxy_quote_budget_or_refresh_when_liquidity_improves"
