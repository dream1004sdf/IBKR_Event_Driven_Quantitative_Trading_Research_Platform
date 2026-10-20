from __future__ import annotations

import json
import os
import time
from pathlib import Path

from recorder.execution_safety_cache_supervisor import (
    build_execution_safety_cache_supervisor,
    execution_safety_cache_supervisor_markdown,
    run_execution_safety_cache_supervisor,
    write_execution_safety_cache_supervisor_json,
    write_execution_safety_cache_supervisor_markdown,
)


def _write(path: Path, payload: dict, *, age_seconds: float = 0.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    ts = time.time() - age_seconds
    os.utime(path, (ts, ts))
    return path


def _profile(base: Path, *, allow_execute: bool = False) -> dict:
    return {
        "safety_cache_supervisor": {
            "enabled": True,
            "allowed_refresh_commands": [
                "ibkr-order-whatif-preview",
                "ibkr-short-availability",
                "operating-defensive-gate-retry",
            ],
            "caches": {
                "broker_margin_whatif_cache": {
                    "json_path": str(base / "whatif.json"),
                    "required": False,
                    "max_age_seconds": 120.0,
                    "warning_age_seconds": 60.0,
                    "block_order_paths_when_stale": ["margin_sensitive_entries"],
                    "allow_execute_refresh": allow_execute,
                    "refresh_command": ["{python}", "main.py", "ibkr-order-whatif-preview"],
                },
                "short_availability_cache": {
                    "json_path": str(base / "short.json"),
                    "required": False,
                    "max_age_seconds": 120.0,
                    "warning_age_seconds": 60.0,
                    "block_order_paths_when_stale": ["new_short_stock_entries"],
                    "allow_execute_refresh": allow_execute,
                    "refresh_command": ["{python}", "main.py", "ibkr-short-availability"],
                },
                "option_quote_liquidity_cache": {
                    "json_path": str(base / "option.json"),
                    "required": False,
                    "max_age_seconds": 120.0,
                    "warning_age_seconds": 60.0,
                    "block_order_paths_when_stale": ["new_option_entries"],
                    "allow_execute_refresh": allow_execute,
                    "refresh_command": ["{python}", "main.py", "operating-defensive-gate-retry"],
                },
            },
        }
    }


def _write_fresh_caches(base: Path) -> None:
    _write(
        base / "whatif.json",
        {
            "whatif_preview_version": "ibkr-order-whatif-preview-v2",
            "requested_count": 1,
            "response_count": 1,
            "safety": {
                "ibkr_order_api_called": True,
                "what_if_only": True,
                "ibkr_whatif_api_transmit_flag": True,
                "live_order_transmitted": False,
                "ibkr_order_submitted": False,
            },
        },
    )
    _write(
        base / "short.json",
        {
            "short_availability_version": "ibkr-short-availability-v1",
            "symbols_requested": ["AMD"],
            "row_count": 1,
            "rows": [{"symbol": "AMD", "short_available": True}],
            "safety": {"read_only_market_data": True, "ibkr_order_submitted": False},
        },
    )
    _write(
        base / "option.json",
        {
            "operating_defensive_gate_retry_version": "operating-defensive-gate-retry-v1",
            "summary": {"best_option_quoted_count": 3, "option_quote_deferred": False},
            "safety": {
                "ticket_created": False,
                "ibkr_order_submitted": False,
                "live_order_transmitted": False,
                "live_order_routing_enabled": False,
                "portfolio_mutated": False,
            },
        },
    )


def test_execution_safety_cache_supervisor_green_when_caches_fresh(tmp_path: Path, monkeypatch) -> None:
    _write_fresh_caches(tmp_path)
    monkeypatch.setenv("IBKR_ALLOW_AUTOMATED_BROKER_REFRESH", "I_UNDERSTAND")

    report = build_execution_safety_cache_supervisor(profile_payload=_profile(tmp_path), profile_base=tmp_path)

    assert report["execution_safety_cache_supervisor_version"] == "execution-safety-cache-supervisor-v1"
    assert report["status"] == "green"
    assert report["cache_ready_for_all_dependent_order_paths"] is True
    assert report["blocked_order_paths"] == []
    assert report["violations"] == []

    markdown = execution_safety_cache_supervisor_markdown(report)
    assert "Execution Safety Cache Supervisor" in markdown
    assert write_execution_safety_cache_supervisor_json(tmp_path / "report.json", report).exists()
    assert write_execution_safety_cache_supervisor_markdown(tmp_path / "report.md", report).exists()


def test_execution_safety_cache_supervisor_blocks_dependent_paths_when_optional_cache_stale(tmp_path: Path) -> None:
    _write_fresh_caches(tmp_path)
    _write(
        tmp_path / "whatif.json",
        {"safety": {"ibkr_order_api_called": True, "what_if_only": True, "live_order_transmitted": False, "ibkr_order_submitted": False}},
        age_seconds=3600.0,
    )

    report = build_execution_safety_cache_supervisor(profile_payload=_profile(tmp_path), profile_base=tmp_path)

    assert report["status"] == "yellow"
    assert report["cache_ready_for_all_dependent_order_paths"] is False
    assert "margin_sensitive_entries" in report["blocked_order_paths"]
    assert "broker_margin_whatif_cache:optional_cache_stale_blocks_dependent_paths" in report["warnings"]
    assert report["violations"] == []


def test_execution_safety_cache_supervisor_flags_unsafe_whatif_cache(tmp_path: Path) -> None:
    _write_fresh_caches(tmp_path)
    _write(
        tmp_path / "whatif.json",
        {"safety": {"ibkr_order_api_called": True, "what_if_only": False, "live_order_transmitted": True, "ibkr_order_submitted": False}},
    )

    report = build_execution_safety_cache_supervisor(profile_payload=_profile(tmp_path), profile_base=tmp_path)

    assert report["status"] == "red"
    assert "broker_margin_whatif_cache:broker_api_called_without_what_if_only" in report["violations"]
    assert "broker_margin_whatif_cache:live_order_transmitted_true" in report["violations"]


def test_execution_safety_cache_supervisor_blocks_option_paths_when_quotes_deferred(tmp_path: Path) -> None:
    _write_fresh_caches(tmp_path)
    _write(
        tmp_path / "option.json",
        {
            "operating_defensive_gate_retry_version": "operating-defensive-gate-retry-v1",
            "summary": {"best_option_quoted_count": 0, "option_quote_deferred": True, "option_quote_session": "pre_market"},
            "safety": {
                "ibkr_order_submitted": False,
                "live_order_transmitted": False,
                "live_order_routing_enabled": False,
                "portfolio_mutated": False,
            },
        },
    )

    report = build_execution_safety_cache_supervisor(profile_payload=_profile(tmp_path), profile_base=tmp_path)

    assert report["status"] == "yellow"
    assert "new_option_entries" in report["blocked_order_paths"]
    assert "option_quote_liquidity_cache:option_quote_deferred_until_regular_session" in report["warnings"]
    option_cache = next(cache for cache in report["cache_reports"] if cache["cache_key"] == "option_quote_liquidity_cache")
    assert "no_fresh_option_bid_ask_quotes" in option_cache["availability_block_reasons"]


def test_execution_safety_cache_supervisor_executes_only_profile_allowed_refresh(tmp_path: Path, monkeypatch) -> None:
    _write_fresh_caches(tmp_path)
    monkeypatch.setenv("IBKR_ALLOW_AUTOMATED_BROKER_REFRESH", "I_UNDERSTAND")
    _write(
        tmp_path / "whatif.json",
        {"safety": {"ibkr_order_api_called": True, "what_if_only": True, "live_order_transmitted": False, "ibkr_order_submitted": False}},
        age_seconds=3600.0,
    )
    calls: list[list[str]] = []

    def fake_runner(command: list[str], cwd: Path, timeout_seconds: float) -> dict:
        calls.append(command)
        return {"status": "succeeded", "returncode": 0, "duration_seconds": 0.01, "stdout_tail": "ok", "stderr_tail": "", "error": None}

    report = build_execution_safety_cache_supervisor(
        profile_payload=_profile(tmp_path, allow_execute=True),
        profile_base=tmp_path,
        execute_refresh=True,
        cwd=tmp_path,
        runner=fake_runner,
    )

    whatif = next(cache for cache in report["cache_reports"] if cache["cache_key"] == "broker_margin_whatif_cache")
    assert whatif["refresh_action"]["executed"] is True
    assert whatif["refresh_action"]["status"] == "succeeded"
    assert calls and calls[0][2] == "ibkr-order-whatif-preview"


def test_execution_safety_cache_supervisor_disabled_profile_blocks_requested_refresh(tmp_path: Path) -> None:
    _write_fresh_caches(tmp_path)
    profile = _profile(tmp_path, allow_execute=True)
    profile["safety_cache_supervisor"]["enabled"] = False
    calls: list[list[str]] = []

    def fake_runner(command: list[str], cwd: Path, timeout_seconds: float) -> dict:
        calls.append(command)
        return {"status": "succeeded", "returncode": 0, "duration_seconds": 0.01, "stdout_tail": "ok", "stderr_tail": "", "error": None}

    report = build_execution_safety_cache_supervisor(
        profile_payload=profile,
        profile_base=tmp_path,
        execute_refresh=True,
        cwd=tmp_path,
        runner=fake_runner,
    )

    assert report["enabled"] is False
    assert report["execute_refresh_requested"] is True
    assert report["execute_refresh_effective"] is False
    assert report["disabled_reason"] == "profile_disabled_fail_closed"
    assert "profile_disabled_fail_closed" in report["warnings"]
    assert calls == []


def test_environment_interlock_blocks_enabled_profile_refresh(tmp_path: Path, monkeypatch) -> None:
    _write_fresh_caches(tmp_path)
    monkeypatch.delenv("IBKR_ALLOW_AUTOMATED_BROKER_REFRESH", raising=False)
    calls: list[list[str]] = []

    def fake_runner(command: list[str], cwd: Path, timeout_seconds: float) -> dict:
        calls.append(command)
        return {"status": "succeeded", "returncode": 0, "duration_seconds": 0.01, "stdout_tail": "ok", "stderr_tail": "", "error": None}

    report = build_execution_safety_cache_supervisor(
        profile_payload=_profile(tmp_path, allow_execute=True),
        profile_base=tmp_path,
        execute_refresh=True,
        cwd=tmp_path,
        runner=fake_runner,
    )

    assert report["enabled"] is True
    assert report["environment_interlock_enabled"] is False
    assert report["execute_refresh_effective"] is False
    assert report["disabled_reason"] == "environment_interlock_locked"
    assert calls == []


def test_execution_safety_cache_supervisor_loop_writes_tick_log(tmp_path: Path, monkeypatch) -> None:
    _write_fresh_caches(tmp_path)
    monkeypatch.setenv("IBKR_ALLOW_AUTOMATED_BROKER_REFRESH", "I_UNDERSTAND")
    profile_path = _write(tmp_path / "profile.json", _profile(tmp_path))

    report = run_execution_safety_cache_supervisor(
        profile_json_path=profile_path,
        max_ticks=1,
        tick_log_jsonl_path=tmp_path / "ticks.jsonl",
        cwd=tmp_path,
    )

    assert report["final_status"] == "green"
    assert report["executed_tick_count"] == 1
    assert (tmp_path / "ticks.jsonl").exists()


def test_repository_defaults_disable_automated_broker_refresh() -> None:
    root = Path(__file__).resolve().parents[1]
    profile = json.loads((root / "config" / "execution_safety_cache_supervisor_profile_v1.json").read_text(encoding="utf-8"))
    supervisor = profile["safety_cache_supervisor"]

    assert supervisor["enabled"] is False
    assert supervisor["allowed_refresh_commands"] == []
    assert all(cache["allow_execute_refresh"] is False for cache in supervisor["caches"].values())

    dedicated_launcher = (root / "run_execution_safety_cache_supervisor_windows.bat").read_text(encoding="utf-8")
    realtime_launcher = (root / "run_realtime_ops_windows.bat").read_text(encoding="utf-8")
    capital_loop = (root / "scripts" / "run_capital_refresh_loop.ps1").read_text(encoding="utf-8")
    scheduled_validation = (root / "scripts" / "run_option_validation_2240_kst.ps1").read_text(encoding="utf-8")

    assert "call run_with_restart_windows.bat" not in dedicated_launcher
    assert "--execute-refresh" not in dedicated_launcher
    assert 'start "Execution Safety Cache Supervisor"' not in realtime_launcher
    assert '"execution-safety-cache-supervisor"' not in capital_loop
    assert "Invoke-SafetyCacheSupervisor" not in capital_loop
    assert 'IBKR_ALLOW_SCHEDULED_BROKER_REFRESH -ne "I_UNDERSTAND"' in scheduled_validation
