from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .singleton_lock import cleanup_stale_singleton_locks, read_singleton_lock_status


OPERATIONAL_LOCK_STATUS_VERSION = "operational-lock-status-v1"

EXPECTED_SINGLETON_NAMES = (
    "market-data-desk",
    "source-online-supervisor",
    "live-scorer-learning",
    "live-learning-llm-notes",
    "threshold-candidate-evaluator",
    "live_learning_control_plane",
    "trading-dashboard-loop",
)

LOOP_OUTPUT_STALE_SECONDS = {
    "market-data-desk": 15.0,
    "source-online-supervisor": 120.0,
    "live-scorer-learning": 300.0,
    "live-learning-llm-notes": 1800.0,
    "threshold-candidate-evaluator": 1200.0,
    "live_learning_control_plane": 180.0,
    "trading-dashboard-loop": 30.0,
}


def repo_root_from_here() -> Path:
    return Path(__file__).resolve().parent.parent


def singleton_artifact_paths(repo_root: Path) -> dict[str, Path]:
    return {
        "market-data-desk": repo_root / "exports" / "m153_market_data_desk_refresh.json",
        "source-online-supervisor": repo_root / "exports" / "m173_source_online_supervisor.json",
        "live-scorer-learning": repo_root / "exports" / "m223_live_scorer_learning.json",
        "live-learning-llm-notes": repo_root / "exports" / "m225_live_learning_llm_notes.json",
        "threshold-candidate-evaluator": repo_root / "exports" / "m226_threshold_candidate_evaluator_summary.json",
        "live_learning_control_plane": repo_root / "exports" / "m232_hot_cache.json",
        "trading-dashboard-loop": repo_root / "exports" / "m220_trading_dashboard_snapshot.json",
    }


def build_operational_lock_status(
    *,
    repo_root: Path | None = None,
    stale_age_seconds: float = 900.0,
    recent_event_window_seconds: float = 900.0,
) -> dict[str, Any]:
    root = (repo_root or repo_root_from_here()).expanduser().resolve()
    status = read_singleton_lock_status(
        lock_dir=root / "locks",
        event_log_path=root / "exports" / "m230_singleton_lock_events.jsonl",
        expected_names=EXPECTED_SINGLETON_NAMES,
        artifact_paths=singleton_artifact_paths(root),
        artifact_stale_seconds=LOOP_OUTPUT_STALE_SECONDS,
        stale_age_seconds=stale_age_seconds,
        recent_event_limit=100,
        recent_event_window_seconds=recent_event_window_seconds,
    )
    status["operational_lock_status_version"] = OPERATIONAL_LOCK_STATUS_VERSION
    status["repo_root"] = str(root)
    status["cleanup_command_hint"] = (
        "main.py singleton-lock-cleanup --execute removes only stale lock files whose recorded PID is not running."
    )
    return status


def build_operational_lock_cleanup_report(
    *,
    repo_root: Path | None = None,
    execute: bool = False,
    min_age_seconds: float = 30.0,
) -> dict[str, Any]:
    root = (repo_root or repo_root_from_here()).expanduser().resolve()
    status = build_operational_lock_status(repo_root=root)
    cleanup = cleanup_stale_singleton_locks(
        lock_dir=root / "locks",
        status=status,
        min_age_seconds=min_age_seconds,
        execute=execute,
        event_log_path=root / "exports" / "m230_singleton_lock_events.jsonl",
    )
    return {
        "operational_lock_cleanup_report_version": "operational-lock-cleanup-report-v1",
        "repo_root": str(root),
        "status_before": status,
        "cleanup": cleanup,
        "status_after": build_operational_lock_status(repo_root=root) if execute else None,
    }


def operational_lock_status_markdown(status: dict[str, Any]) -> str:
    truth = status.get("truth_summary") or {}
    lines = [
        "# Operational Lock Status",
        "",
        f"- Version: `{status.get('operational_lock_status_version') or status.get('status_version')}`",
        f"- Generated at: `{status.get('generated_at')}`",
        f"- Overall: `{status.get('overall_status')}`",
        f"- Repo: `{status.get('repo_root') or '-'}`",
        f"- Online: `{status.get('online_count')}` / `{status.get('lock_count')}`",
        f"- Stale/missing: `{status.get('stale_or_missing_count')}`",
        f"- Zombie output stale: `{status.get('zombie_output_stale_count')}`",
        "",
        "## Truth Summary",
        "",
        f"- Process online: `{truth.get('process_online_count')}`",
        f"- Lock files: `{truth.get('lock_file_count')}`",
        f"- Stale locks: `{truth.get('stale_lock_count')}`",
        f"- Missing locks: `{truth.get('missing_lock_count')}`",
        f"- Artifact fresh/stale: `{truth.get('artifact_fresh_count')}` / `{truth.get('artifact_stale_count')}`",
        f"- Fresh artifact without process: `{truth.get('fresh_artifact_without_process_count')}` {truth.get('fresh_artifact_without_process_services') or []}",
        f"- Interpretation: {truth.get('interpretation')}",
        "",
        "## Locks",
        "",
        "| Service | Status | PID | PID running | Lock age | Artifact age | Artifact stale |",
        "| --- | --- | ---: | --- | ---: | ---: | --- |",
    ]
    for row in status.get("locks") or []:
        lines.append(
            f"| `{row.get('name')}` | `{row.get('status')}` | {row.get('writer_pid') or row.get('pid') or '-'} | "
            f"{row.get('pid_running')} | {row.get('age_seconds') if row.get('age_seconds') is not None else row.get('lock_file_age_seconds')} | "
            f"{row.get('artifact_age_seconds') if row.get('artifact_age_seconds') is not None else '-'} | {row.get('artifact_stale')} |"
        )
    lines.extend(
        [
            "",
            "## Safety",
            "",
            "- Lock PID is treated as the writer PID.",
            "- Duplicate-looking Windows venv parent/child Python processes are not treated as duplicate services by themselves.",
            "- Use cleanup only for stale locks whose recorded PID is no longer running.",
        ]
    )
    return "\n".join(lines)


def operational_lock_cleanup_markdown(report: dict[str, Any]) -> str:
    cleanup = report.get("cleanup") or {}
    lines = [
        "# Operational Lock Cleanup",
        "",
        f"- Version: `{report.get('operational_lock_cleanup_report_version')}`",
        f"- Repo: `{report.get('repo_root')}`",
        f"- Execute: `{cleanup.get('execute')}`",
        f"- Candidates: `{cleanup.get('candidate_count')}`",
        f"- Removed: `{cleanup.get('removed_count')}`",
        f"- Skipped: `{cleanup.get('skipped_count')}`",
        "",
        "| Service | PID | Path | Action |",
        "| --- | ---: | --- | --- |",
    ]
    removed_paths = {row.get("lock_path") for row in cleanup.get("removed") or []}
    for row in cleanup.get("candidates") or []:
        action = "removed" if row.get("lock_path") in removed_paths else "would_remove"
        lines.append(f"| `{row.get('name')}` | {row.get('pid') or '-'} | `{row.get('lock_path')}` | `{action}` |")
    for row in cleanup.get("skipped") or []:
        lines.append(f"| `{row.get('name')}` | - | `{row.get('lock_path')}` | `skipped:{row.get('reason')}` |")
    return "\n".join(lines)


def write_operational_lock_status_json(path: Path, status: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(status, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def write_operational_lock_status_markdown(path: Path, status: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(operational_lock_status_markdown(status), encoding="utf-8")
    return path


def write_operational_lock_cleanup_json(path: Path, report: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def write_operational_lock_cleanup_markdown(path: Path, report: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(operational_lock_cleanup_markdown(report), encoding="utf-8")
    return path
