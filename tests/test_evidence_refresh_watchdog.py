from __future__ import annotations

import json
from pathlib import Path

from recorder.evidence_operations import (
    EvidenceRefreshWatchdogConfig,
    evidence_refresh_watchdog_markdown,
    run_evidence_refresh_watchdog,
)
from main import _run_watch_evidence_refresh_watchdog


def _write_json(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def _stale_report() -> dict:
    return {
        "overall_status": "yellow",
        "evidence_gap_warning": True,
        "stale_source_keys": ["market_news"],
        "lagging_source_keys": [],
        "status_counts": {"fresh": 5, "stale": 1},
    }


def _green_report() -> dict:
    return {
        "overall_status": "green",
        "evidence_gap_warning": False,
        "stale_source_keys": [],
        "lagging_source_keys": [],
        "status_counts": {"fresh": 6},
    }


def _config(tmp_path: Path, *, max_ticks: int = 1) -> EvidenceRefreshWatchdogConfig:
    return EvidenceRefreshWatchdogConfig(
        freshness_profile_path=tmp_path / "freshness_profile.json",
        refresh_queue_profile_path=tmp_path / "queue_profile.json",
        freshness_report_path=tmp_path / "freshness.json",
        refresh_queue_json_path=tmp_path / "queue.json",
        refresh_queue_markdown_path=tmp_path / "queue.md",
        state_json_path=tmp_path / "watchdog_state.json",
        tick_log_jsonl_path=tmp_path / "watchdog_ticks.jsonl",
        max_ticks=max_ticks,
        cooldown_seconds=300,
        initial_backoff_seconds=120,
        max_backoff_seconds=600,
        run_freshness_recheck=False,
    )


def test_evidence_refresh_watchdog_recovers_stale_sources(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _write_json(config.freshness_report_path, _stale_report())

    def runner(command: list[str], cwd: Path, timeout_seconds: float) -> dict:
        assert "evidence-refresh-queue" in command
        _write_json(config.refresh_queue_json_path, {"status_counts": {"succeeded": 4}, "generated_at": "2026-06-30T10:00:00+00:00"})
        _write_json(config.freshness_report_path, _green_report())
        return {"status": "succeeded", "returncode": 0, "stdout_tail": "ok", "stderr_tail": "", "duration_seconds": 0.1, "error": None}

    result = run_evidence_refresh_watchdog(
        config=config,
        cwd=tmp_path,
        run_id="watchdog_test",
        command_runner=runner,
        now_sequence=["2026-06-30T10:00:00+00:00"],
    )
    markdown = evidence_refresh_watchdog_markdown(result)
    state = json.loads(config.state_json_path.read_text(encoding="utf-8"))

    assert result["final_status"] == "recovered"
    assert result["final_freshness_status"] == "green"
    assert result["final_stale_source_keys"] == []
    assert state["last_refresh_status"] == "succeeded"
    assert state["consecutive_failures"] == 0
    assert "Evidence Refresh Watchdog" in markdown
    assert config.tick_log_jsonl_path.exists()


def test_evidence_refresh_watchdog_backoff_after_failure(tmp_path: Path) -> None:
    config = _config(tmp_path, max_ticks=2)
    _write_json(config.freshness_report_path, _stale_report())

    def runner(command: list[str], cwd: Path, timeout_seconds: float) -> dict:
        _write_json(config.refresh_queue_json_path, {"status_counts": {"failed": 1}, "generated_at": "2026-06-30T10:00:00+00:00"})
        return {"status": "succeeded", "returncode": 0, "stdout_tail": "failed", "stderr_tail": "", "duration_seconds": 0.1, "error": None}

    result = run_evidence_refresh_watchdog(
        config=config,
        cwd=tmp_path,
        run_id="watchdog_fail_test",
        command_runner=runner,
        now_sequence=["2026-06-30T10:00:00+00:00", "2026-06-30T10:01:00+00:00"],
    )
    state = json.loads(config.state_json_path.read_text(encoding="utf-8"))

    assert result["tick_summaries"][0]["status"] == "refresh_failed"
    assert result["tick_summaries"][1]["status"] == "skipped_cooldown"
    assert state["consecutive_failures"] == 1
    assert state["next_refresh_eligible_at"] is not None


def test_watch_profile_can_run_evidence_refresh_watchdog_before_preflight(tmp_path: Path) -> None:
    freshness_path = tmp_path / "freshness.json"
    state_path = tmp_path / "state.json"
    json_output = tmp_path / "m157_watchdog.json"
    markdown_output = tmp_path / "m157_watchdog.md"
    _write_json(freshness_path, _green_report())
    profile = {
        "evidence_refresh_watchdog": {
            "enabled": True,
            "freshness_profile_json": str(tmp_path / "freshness_profile.json"),
            "refresh_queue_profile_json": str(tmp_path / "queue_profile.json"),
            "freshness_report_json": str(freshness_path),
            "refresh_queue_json": str(tmp_path / "queue.json"),
            "refresh_queue_markdown": str(tmp_path / "queue.md"),
            "state_json_path": str(state_path),
            "tick_log_jsonl_path": str(tmp_path / "ticks.jsonl"),
            "output": str(markdown_output),
            "json_output_path": str(json_output),
            "max_ticks": 1,
            "dry_run": True,
            "skip_freshness_recheck": True,
        }
    }

    result, summary = _run_watch_evidence_refresh_watchdog(
        profile=profile,
        profile_base=tmp_path,
        run_id="watch_m157_test",
    )

    assert result is not None
    assert summary["applied"] is True
    assert summary["status"] == "healthy_no_refresh_needed"
    assert summary["freshness_status"] == "green"
    assert state_path.exists()
    assert json_output.exists()
    assert markdown_output.exists()
