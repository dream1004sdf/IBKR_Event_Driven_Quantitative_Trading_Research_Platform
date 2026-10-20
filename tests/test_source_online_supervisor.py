from __future__ import annotations

import json
from pathlib import Path

from recorder.source_operations import (
    SourceOnlineSupervisorConfig,
    _merge_live_validation_payloads,
    run_source_online_supervisor,
    source_online_supervisor_markdown,
)


class _Settings:
    watch_symbols = ("NVDA", "TSM")
    news_provider_codes = ("BZ",)
    geopolitical_news_symbols = ("SPY", "QQQ", "GLD", "XLE")

    @property
    def all_news_symbols(self) -> tuple[str, ...]:
        return ("NVDA", "TSM", "SPY", "QQQ", "GLD", "XLE")


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _json_output_path(command: list[str]) -> Path:
    return Path(command[command.index("--json-output-path") + 1])


def _config(tmp_path: Path, *, execute_actions: bool = False) -> SourceOnlineSupervisorConfig:
    return SourceOnlineSupervisorConfig(
        live_validation_profile_path=tmp_path / "source_live_profile.json",
        live_validation_json_path=tmp_path / "live.json",
        live_validation_markdown_path=tmp_path / "live.md",
        freshness_profile_path=tmp_path / "freshness_profile.json",
        freshness_report_path=tmp_path / "freshness.json",
        source_ingest_health_profile_path=tmp_path / "source_health_profile.json",
        source_ingest_health_json_path=tmp_path / "health.json",
        source_ingest_health_markdown_path=tmp_path / "health.md",
        output_dir=tmp_path,
        state_json_path=tmp_path / "state.json",
        tick_log_jsonl_path=tmp_path / "ticks.jsonl",
        execute_actions=execute_actions,
        max_ticks=1,
        job_timeout_seconds=30.0,
    )


def _config_two_ticks(tmp_path: Path, *, execute_actions: bool = True) -> SourceOnlineSupervisorConfig:
    return SourceOnlineSupervisorConfig(
        live_validation_profile_path=tmp_path / "source_live_profile.json",
        live_validation_json_path=tmp_path / "live.json",
        live_validation_markdown_path=tmp_path / "live.md",
        freshness_profile_path=tmp_path / "freshness_profile.json",
        freshness_report_path=tmp_path / "freshness.json",
        source_ingest_health_profile_path=tmp_path / "source_health_profile.json",
        source_ingest_health_json_path=tmp_path / "health.json",
        source_ingest_health_markdown_path=tmp_path / "health.md",
        output_dir=tmp_path,
        state_json_path=tmp_path / "state.json",
        tick_log_jsonl_path=tmp_path / "ticks.jsonl",
        execute_actions=execute_actions,
        max_ticks=2,
        sleep_between_ticks=False,
        job_timeout_seconds=30.0,
        market_data_action_cooldown_seconds=1.0,
        market_news_action_cooldown_seconds=1.0,
        geopolitical_action_cooldown_seconds=300.0,
        ibkr_validation_interval_seconds=1.0,
        marketaux_alpha_validation_interval_seconds=60.0,
        external_validation_interval_seconds=60.0,
    )


def _live_validation() -> dict:
    return {
        "overall_status": "yellow",
        "source_status": {"market_news": "ok", "geopolitical_signals": "degraded"},
        "providers": [
            {
                "provider": "ibkr_historical_news",
                "source_key": "market_news",
                "status": "ok",
                "row_count": 50,
                "latest_event_at": "2026-06-30T10:23:37+00:00",
            },
            {
                "provider": "marketaux",
                "source_key": "geopolitical_signals",
                "status": "stale_response",
                "row_count": 10,
                "latest_event_at": "2026-04-06T12:29:50+00:00",
            },
            {
                "provider": "alpha_vantage",
                "source_key": "geopolitical_signals",
                "status": "no_rows",
                "row_count": 0,
            },
        ],
    }


def _source_health() -> dict:
    return {
        "overall_status": "yellow",
        "online_problem_source_keys": ["market_news", "geopolitical_signals"],
        "degraded_source_keys": ["geopolitical_signals"],
        "root_cause_counts": {
            "healthy_online": 1,
            "provider_ok_ingest_or_freshness_pipeline_lag": 1,
            "provider_live_validation_problem": 1,
        },
        "sources": [
            {
                "source_key": "market_data",
                "health_status": "healthy",
                "freshness_status": "fresh",
            },
            {
                "source_key": "market_news",
                "health_status": "watch",
                "freshness_status": "lagging",
                "latest_event_at": "2026-06-30T07:18:59+00:00",
                "root_cause": "provider_ok_ingest_or_freshness_pipeline_lag",
            },
            {
                "source_key": "geopolitical_signals",
                "health_status": "degraded",
                "freshness_status": "lagging",
                "latest_event_at": "2026-06-30T07:18:59+00:00",
                "root_cause": "provider_live_validation_problem",
            },
        ],
    }


def _source_health_with_market_data_gap() -> dict:
    payload = _source_health()
    payload["online_problem_source_keys"] = ["market_data", "market_news", "geopolitical_signals"]
    payload["sources"][0] = {
        "source_key": "market_data",
        "health_status": "degraded",
        "freshness_status": "stale",
        "latest_event_at": "2026-06-30T10:00:00+00:00",
        "root_cause": "source_stale_refresh_required",
    }
    return payload


def test_source_online_supervisor_plans_provider_aware_actions_without_execution(tmp_path: Path) -> None:
    config = _config(tmp_path, execute_actions=False)

    def runner(command: list[str], cwd: Path, timeout_seconds: float) -> dict:
        if "source-live-validation" in command:
            _write_json(_json_output_path(command), _live_validation())
        if "source-ingest-health" in command:
            _write_json(config.source_ingest_health_json_path, _source_health())
        if "evidence-freshness-desk" in command:
            _write_json(config.freshness_report_path, {"overall_status": "watch"})
        return {"status": "succeeded", "returncode": 0, "stdout_tail": "ok", "stderr_tail": "", "duration_seconds": 0.1, "error": None}

    result = run_source_online_supervisor(
        config=config,
        settings=_Settings(),
        cwd=tmp_path,
        run_id="m162_test_plan",
        command_runner=runner,
        now_sequence=["2026-06-30T10:30:00+00:00"],
    )
    tick = result["tick_summaries"][0]
    actions = {row["action_id"]: row for row in tick["actions"]}
    markdown = source_online_supervisor_markdown(result)

    assert actions["source_live_validation_ibkr"]["status"] == "succeeded"
    assert actions["source_live_validation_marketaux"]["status"] == "succeeded"
    assert actions["market_data_fresh_tick"]["status"] == "planned_dry_run"
    assert actions["market_news_ibkr_catchup"]["status"] == "planned_dry_run"
    assert actions["benzinga_external_signal_conversion"]["status"] == "planned_dry_run"
    assert actions["external_market_sync_marketaux"]["status"] == "planned_dry_run"
    assert actions["external_market_sync_alpha_vantage"]["status"] == "planned_dry_run"
    assert result["final_status"] == "yellow"
    assert tick["checked_at_local"]
    assert result["started_at_local"]
    assert result["completed_at_local"]
    assert config.tick_log_jsonl_path.exists()
    assert "Source Online Supervisor" in markdown
    assert "Checked local" in markdown
    assert "Checked raw UTC" in markdown


def test_source_online_supervisor_rolls_transient_ibkr_warning_up_as_ok() -> None:
    merged = _merge_live_validation_payloads(
        {},
        {
            "source_live_validation_version": "source-live-validation-v3",
            "generated_at": "2026-06-30T10:00:00+00:00",
            "providers": [
                {
                    "provider": "ibkr_historical_news",
                    "source_key": "market_news",
                    "status": "ok_with_transient_warnings",
                    "row_count": 50,
                    "latest_event_at": "2026-06-30T09:59:00+00:00",
                    "transient_warning_count": 1,
                    "errors": ["IBKR message reqId=-1 details=(2103, 'Market data farm connection is broken:usfuture')"],
                }
            ],
        },
    )

    assert merged["source_status"]["market_news"] == "ok"
    assert merged["overall_status"] == "green"


def test_source_online_supervisor_preserves_previous_marketaux_ok_on_timeout_update() -> None:
    merged = _merge_live_validation_payloads(
        {
            "source_live_validation_version": "source-live-validation-v4",
            "generated_at": "2026-06-30T10:00:00+00:00",
            "providers": [
                {
                    "provider": "marketaux",
                    "source_key": "geopolitical_signals",
                    "reachable": True,
                    "status": "ok",
                    "row_count": 10,
                    "latest_event_at": "2026-06-30T09:55:00+00:00",
                    "latest_event_age_minutes": 5.0,
                    "errors": [],
                },
                {
                    "provider": "alpha_vantage",
                    "source_key": "geopolitical_signals",
                    "reachable": True,
                    "status": "provider_notice",
                    "row_count": 0,
                    "errors": ["rate limit"],
                },
            ],
        },
        {
            "source_live_validation_version": "source-live-validation-v4",
            "generated_at": "2026-06-30T10:01:00+00:00",
            "providers": [
                {
                    "provider": "marketaux",
                    "source_key": "geopolitical_signals",
                    "reachable": False,
                    "status": "error",
                    "row_count": 0,
                    "latest_event_at": None,
                    "attempt_count": 2,
                    "errors": ["TimeoutError: The read operation timed out"],
                }
            ],
        },
    )
    providers = {row["provider"]: row for row in merged["providers"]}

    assert merged["source_status"]["geopolitical_signals"] == "ok"
    assert merged["overall_status"] == "green"
    assert providers["marketaux"]["status"] == "cached_ok_after_validation_error"
    assert providers["marketaux"]["latest_event_at"] == "2026-06-30T09:55:00+00:00"
    assert providers["marketaux"]["last_validation_status"] == "error"
    assert providers["marketaux"]["last_validation_attempt_count"] == 2


def test_source_online_supervisor_market_data_refresh_uses_freshness_standard_path(tmp_path: Path) -> None:
    config = _config(tmp_path, execute_actions=False)

    def runner(command: list[str], cwd: Path, timeout_seconds: float) -> dict:
        if "source-live-validation" in command:
            _write_json(_json_output_path(command), _live_validation())
        if "source-ingest-health" in command:
            _write_json(config.source_ingest_health_json_path, _source_health_with_market_data_gap())
        if "evidence-freshness-desk" in command:
            _write_json(config.freshness_report_path, {"overall_status": "watch"})
        return {"status": "succeeded", "returncode": 0, "stdout_tail": "ok", "stderr_tail": "", "duration_seconds": 0.1, "error": None}

    result = run_source_online_supervisor(
        config=config,
        settings=_Settings(),
        cwd=tmp_path,
        run_id="m162_test_market_data_path",
        command_runner=runner,
        now_sequence=["2026-06-30T10:30:00+00:00"],
    )
    actions = {row["action_id"]: row for row in result["tick_summaries"][0]["actions"]}
    market_data_command = actions["market_data_fresh_tick"]["command"]

    assert actions["market_data_fresh_tick"]["status"] == "planned_dry_run"
    assert market_data_command[market_data_command.index("--seconds") + 1] == "16"
    assert market_data_command[market_data_command.index("--fallback-after-seconds") + 1] == "4"
    assert "m153_market_data_desk_refresh.json" in " ".join(market_data_command)
    assert "m162_market_data_desk_refresh.json" not in " ".join(market_data_command)


def test_source_online_supervisor_market_data_refresh_passes_dashboard_account_snapshot(tmp_path: Path) -> None:
    account_snapshot = tmp_path / "account.json"
    _write_json(account_snapshot, {"positions": [{"contract": {"symbol": "EWY", "sec_type": "OPT"}}]})
    dashboard_profile = tmp_path / "dashboard_profile.json"
    _write_json(dashboard_profile, {"paths": {"account_snapshot_json": str(account_snapshot)}})
    config = SourceOnlineSupervisorConfig(
        **{
            **_config(tmp_path, execute_actions=False).__dict__,
            "dashboard_profile_path": dashboard_profile,
        }
    )

    def runner(command: list[str], cwd: Path, timeout_seconds: float) -> dict:
        if "source-live-validation" in command:
            _write_json(_json_output_path(command), _live_validation())
        if "source-ingest-health" in command:
            _write_json(config.source_ingest_health_json_path, _source_health_with_market_data_gap())
        if "evidence-freshness-desk" in command:
            _write_json(config.freshness_report_path, {"overall_status": "watch"})
        return {"status": "succeeded", "returncode": 0, "stdout_tail": "ok", "stderr_tail": "", "duration_seconds": 0.1, "error": None}

    result = run_source_online_supervisor(
        config=config,
        settings=_Settings(),
        cwd=tmp_path,
        run_id="m215_test_market_data_account_underlyings",
        command_runner=runner,
        now_sequence=["2026-06-30T10:30:00+00:00"],
    )
    actions = {row["action_id"]: row for row in result["tick_summaries"][0]["actions"]}
    market_data_command = actions["market_data_fresh_tick"]["command"]

    assert "--account-snapshot-json" in market_data_command
    assert market_data_command[market_data_command.index("--account-snapshot-json") + 1] == str(account_snapshot)


def test_source_online_supervisor_executes_catchup_and_sets_cooldown(tmp_path: Path) -> None:
    config = _config(tmp_path, execute_actions=True)
    executed: list[str] = []

    def runner(command: list[str], cwd: Path, timeout_seconds: float) -> dict:
        executed.append(" ".join(command))
        if "source-live-validation" in command:
            _write_json(_json_output_path(command), _live_validation())
        if "source-ingest-health" in command:
            _write_json(config.source_ingest_health_json_path, _source_health())
        if "evidence-freshness-desk" in command:
            _write_json(config.freshness_report_path, {"overall_status": "watch"})
        return {"status": "succeeded", "returncode": 0, "stdout_tail": "ok", "stderr_tail": "", "duration_seconds": 0.1, "error": None}

    result = run_source_online_supervisor(
        config=config,
        settings=_Settings(),
        cwd=tmp_path,
        run_id="m162_test_execute",
        command_runner=runner,
        now_sequence=["2026-06-30T10:30:00+00:00"],
    )
    actions = {row["action_id"]: row for row in result["tick_summaries"][0]["actions"]}
    state = json.loads(config.state_json_path.read_text(encoding="utf-8"))

    assert actions["market_news_ibkr_catchup"]["status"] == "succeeded"
    assert actions["market_data_fresh_tick"]["status"] == "succeeded"
    assert actions["external_market_sync_marketaux"]["status"] == "succeeded"
    assert "market_news_ibkr_catchup" in state["next_eligible_by_action"]
    assert "source_live_validation_ibkr" in state["next_eligible_by_action"]
    assert "gdelt_rate_limited" not in state["next_eligible_by_action"]
    assert any("ibkr-news-backfill" in item for item in executed)
    assert result["safety"]["ibkr_order_submitted"] is False


def test_source_online_supervisor_one_second_ibkr_cadence_skips_half_second_tick(tmp_path: Path) -> None:
    config = _config_two_ticks(tmp_path, execute_actions=True)

    def runner(command: list[str], cwd: Path, timeout_seconds: float) -> dict:
        if "source-live-validation" in command:
            _write_json(_json_output_path(command), _live_validation())
        if "source-ingest-health" in command:
            _write_json(config.source_ingest_health_json_path, _source_health())
        if "evidence-freshness-desk" in command:
            _write_json(config.freshness_report_path, {"overall_status": "watch"})
        return {"status": "succeeded", "returncode": 0, "stdout_tail": "ok", "stderr_tail": "", "duration_seconds": 0.1, "error": None}

    result = run_source_online_supervisor(
        config=config,
        settings=_Settings(),
        cwd=tmp_path,
        run_id="m164_test_one_second_ibkr_cadence",
        command_runner=runner,
        now_sequence=["2026-06-30T10:30:00+00:00", "2026-06-30T10:30:00.500000+00:00"],
    )
    first_actions = {row["action_id"]: row for row in result["tick_summaries"][0]["actions"]}
    second_actions = {row["action_id"]: row for row in result["tick_summaries"][1]["actions"]}

    assert first_actions["source_live_validation_ibkr"]["status"] == "succeeded"
    assert first_actions["market_news_ibkr_catchup"]["status"] == "succeeded"
    assert second_actions["source_live_validation_ibkr"]["status"] == "skipped_cooldown"
    assert second_actions["market_news_ibkr_catchup"]["status"] == "skipped_cooldown"
    assert second_actions["source_live_validation_marketaux"]["status"] == "skipped_cooldown"
    assert second_actions["source_live_validation_ibkr"]["details"]["next_eligible_at_local"]
    assert result["tick_summaries"][1]["status"] == "yellow"
    assert result["tick_summaries"][1]["hot_lane"]["status_counts"]["skipped_cooldown"] >= 2
    assert result["final_hot_lane"]["cooldown_skip_is_healthy_when_ingest_green"] is True
