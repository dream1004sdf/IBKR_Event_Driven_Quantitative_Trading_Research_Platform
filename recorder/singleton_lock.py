from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


UTC = timezone.utc
SINGLETON_LOCK_STATUS_VERSION = "singleton-lock-status-v1"
SINGLETON_LOCK_EVENT_VERSION = "singleton-lock-event-v1"


class AlreadyRunningError(RuntimeError):
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        super().__init__(str(payload.get("message") or "another instance is already running"))


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            return str(pid) in (result.stdout or "")
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
    except Exception:
        return False


def _read_lock(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _path_mtime(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    except OSError:
        return None


def _parse_iso(value: Any) -> datetime | None:
    try:
        text = str(value or "")
        if not text:
            return None
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None


def append_singleton_event(path: Path, event: dict[str, Any]) -> None:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "event_version": SINGLETON_LOCK_EVENT_VERSION,
        "recorded_at": _iso_now(),
        **event,
    }
    with resolved.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def read_singleton_lock_status(
    *,
    lock_dir: Path,
    event_log_path: Path | None = None,
    expected_names: list[str] | tuple[str, ...] = (),
    artifact_paths: dict[str, str | Path] | None = None,
    artifact_stale_seconds: dict[str, float] | None = None,
    stale_age_seconds: float = 300.0,
    recent_event_limit: int = 50,
    recent_event_window_seconds: float = 900.0,
) -> dict[str, Any]:
    now = datetime.now(UTC)
    lock_dir = lock_dir.expanduser().resolve()
    expected = [str(item) for item in expected_names]
    artifact_paths = artifact_paths or {}
    artifact_stale_seconds = artifact_stale_seconds or {}
    rows: list[dict[str, Any]] = []
    names = sorted(set(expected) | {path.stem for path in lock_dir.glob("*.lock")} if lock_dir.exists() else set(expected))
    for name in names:
        path = lock_dir / f"{name}.lock"
        payload = _read_lock(path) if path.exists() else {}
        lock_mtime_dt = _path_mtime(path) if path.exists() else None
        lock_file_age_seconds = (now - lock_mtime_dt).total_seconds() if lock_mtime_dt else None
        pid = int(payload.get("pid") or 0)
        acquired_at = _parse_iso(payload.get("acquired_at"))
        age_seconds = (now - acquired_at).total_seconds() if acquired_at else None
        running = _pid_is_running(pid)
        artifact_path_value = artifact_paths.get(name)
        artifact_path = Path(artifact_path_value).expanduser().resolve() if artifact_path_value else None
        artifact_exists = bool(artifact_path and artifact_path.exists())
        artifact_mtime = None
        artifact_age_seconds = None
        if artifact_exists and artifact_path is not None:
            try:
                artifact_mtime_dt = datetime.fromtimestamp(artifact_path.stat().st_mtime, tz=UTC)
                artifact_mtime = artifact_mtime_dt.isoformat()
                artifact_age_seconds = max(0.0, (now - artifact_mtime_dt).total_seconds())
            except Exception:
                artifact_mtime = None
                artifact_age_seconds = None
        artifact_threshold = float(artifact_stale_seconds.get(name, stale_age_seconds))
        artifact_warmup_complete = bool(age_seconds is not None and age_seconds > artifact_threshold)
        artifact_stale = bool(
            artifact_path is not None
            and artifact_warmup_complete
            and (
                not artifact_exists
                or (artifact_age_seconds is not None and artifact_age_seconds > artifact_threshold)
            )
        )
        status = "online" if running else ("stale_lock" if path.exists() else "missing_lock")
        if running and artifact_stale:
            status = "zombie_output_stale"
        rows.append(
            {
                "name": name,
                "status": status,
                "pid": pid or None,
                "writer_pid": pid or None,
                "pid_role": "singleton_lock_holder_writer",
                "pid_running": running,
                "lock_exists": path.exists(),
                "lock_path": str(path),
                "lock_mtime": lock_mtime_dt.isoformat() if lock_mtime_dt else None,
                "lock_file_age_seconds": round(lock_file_age_seconds, 3) if lock_file_age_seconds is not None else None,
                "acquired_at": payload.get("acquired_at"),
                "age_seconds": round(age_seconds, 3) if age_seconds is not None else None,
                "stale_age_seconds": float(stale_age_seconds),
                "stale_by_age": bool(age_seconds is not None and age_seconds > float(stale_age_seconds)),
                "artifact_path": str(artifact_path) if artifact_path else None,
                "artifact_exists": artifact_exists if artifact_path else None,
                "artifact_mtime": artifact_mtime,
                "artifact_age_seconds": round(artifact_age_seconds, 3) if artifact_age_seconds is not None else None,
                "artifact_stale_seconds": artifact_threshold if artifact_path else None,
                "artifact_warmup_complete": artifact_warmup_complete if artifact_path else None,
                "artifact_stale": artifact_stale if artifact_path else False,
                "cwd": payload.get("cwd"),
            }
        )
    recent_events: list[dict[str, Any]] = []
    duplicate_block_count = 0
    if event_log_path is not None:
        resolved_event = event_log_path.expanduser().resolve()
        if resolved_event.exists():
            try:
                lines = resolved_event.read_text(encoding="utf-8").splitlines()[-max(1, int(recent_event_limit)) :]
                for line in lines:
                    try:
                        event = json.loads(line)
                    except Exception:
                        continue
                    if isinstance(event, dict):
                        recent_events.append(event)
                        event_at = _parse_iso(event.get("recorded_at"))
                        in_recent_window = bool(
                            event_at is not None
                            and (now - event_at).total_seconds() <= float(recent_event_window_seconds)
                        )
                        if event.get("event_type") == "already_running_blocked" and in_recent_window:
                            duplicate_block_count += 1
            except Exception:
                pass
    status_counts: dict[str, int] = {}
    for row in rows:
        status = str(row.get("status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
    service_groups: list[dict[str, Any]] = []
    for row in rows:
        service_groups.append(
            {
                "service": row.get("name"),
                "group_status": row.get("status"),
                "lock_holder_writer_pid": row.get("writer_pid"),
                "pid_running": row.get("pid_running"),
                "artifact_path": row.get("artifact_path"),
                "artifact_age_seconds": row.get("artifact_age_seconds"),
                "artifact_stale": row.get("artifact_stale"),
                "process_count_interpretation": "singleton_lock_holder_writer_pid_only",
                "windows_venv_parent_child_note": (
                    "Duplicate-looking python.exe rows can be Windows venv parent/child wrappers; treat duplicate-block events and stale artifacts as stronger evidence than raw python.exe count."
                ),
            }
        )
    service_group_status_counts: dict[str, int] = {}
    for row in service_groups:
        status = str(row.get("group_status") or "unknown")
        service_group_status_counts[status] = service_group_status_counts.get(status, 0) + 1
    artifact_configured_rows = [row for row in rows if row.get("artifact_path")]
    artifact_fresh_rows = [
        row
        for row in artifact_configured_rows
        if row.get("artifact_exists") and not row.get("artifact_stale")
    ]
    artifact_stale_rows = [row for row in artifact_configured_rows if row.get("artifact_stale")]
    fresh_artifact_without_process_rows = [
        row
        for row in artifact_fresh_rows
        if not row.get("pid_running")
    ]
    truth_summary = {
        "process_online_count": status_counts.get("online", 0),
        "expected_service_count": len(expected),
        "lock_file_count": sum(1 for row in rows if row.get("lock_exists")),
        "stale_lock_count": status_counts.get("stale_lock", 0),
        "missing_lock_count": status_counts.get("missing_lock", 0),
        "online_but_output_stale_count": status_counts.get("zombie_output_stale", 0),
        "artifact_configured_count": len(artifact_configured_rows),
        "artifact_fresh_count": len(artifact_fresh_rows),
        "artifact_stale_count": len(artifact_stale_rows),
        "fresh_artifact_without_process_count": len(fresh_artifact_without_process_rows),
        "fresh_artifact_without_process_services": [
            str(row.get("name") or "")
            for row in fresh_artifact_without_process_rows
            if row.get("name")
        ],
        "interpretation": (
            "Process, lock, and artifact freshness are separate signals. "
            "A fresh artifact without a running writer means the displayed data can look fresh even though collection is offline."
        ),
    }
    overall = "green"
    if status_counts.get("zombie_output_stale"):
        overall = "red"
    elif status_counts.get("stale_lock") or status_counts.get("missing_lock"):
        overall = "yellow"
    if not rows:
        overall = "unknown"
    return {
        "status_version": SINGLETON_LOCK_STATUS_VERSION,
        "generated_at": _iso_now(),
        "overall_status": overall,
        "status_counts": status_counts,
        "expected_names": expected,
        "lock_count": len(rows),
        "online_count": status_counts.get("online", 0),
        "stale_or_missing_count": status_counts.get("stale_lock", 0) + status_counts.get("missing_lock", 0),
        "zombie_output_stale_count": status_counts.get("zombie_output_stale", 0),
        "service_group_count": len(service_groups),
        "service_group_status_counts": service_group_status_counts,
        "service_groups": service_groups,
        "truth_summary": truth_summary,
        "process_identity_policy": {
            "group_by": "singleton_lock_name",
            "primary_pid_field": "lock_holder_writer_pid",
            "raw_python_process_count_is_not_duplicate_proof": True,
            "duplicate_signal": "recent already_running_blocked events or stale output while PID is alive",
        },
        "duplicate_block_count_recent": duplicate_block_count,
        "duplicate_block_window_seconds": float(recent_event_window_seconds),
        "locks": rows,
        "recent_events": recent_events[-10:],
    }


def cleanup_stale_singleton_locks(
    *,
    lock_dir: Path,
    status: dict[str, Any] | None = None,
    min_age_seconds: float = 30.0,
    execute: bool = False,
    event_log_path: Path | None = None,
) -> dict[str, Any]:
    """Dry-run or remove lock files whose recorded PID is no longer alive.

    Active locks are never deleted. Recently-created malformed/stale-looking
    locks are skipped to avoid racing a process that is still starting.
    """

    lock_dir = lock_dir.expanduser().resolve()
    status_payload = status or read_singleton_lock_status(lock_dir=lock_dir)
    candidates: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    for row in status_payload.get("locks") or []:
        name = str(row.get("name") or "")
        lock_path = Path(str(row.get("lock_path") or ""))
        if row.get("status") != "stale_lock":
            skipped.append({"name": name, "lock_path": str(lock_path), "reason": f"status_{row.get('status')}"})
            continue
        if row.get("pid_running"):
            skipped.append({"name": name, "lock_path": str(lock_path), "reason": "pid_still_running"})
            continue
        try:
            lock_path.resolve().relative_to(lock_dir)
        except ValueError:
            skipped.append({"name": name, "lock_path": str(lock_path), "reason": "outside_lock_dir"})
            continue
        if not lock_path.exists():
            skipped.append({"name": name, "lock_path": str(lock_path), "reason": "lock_file_missing"})
            continue
        age = row.get("age_seconds")
        if age is None:
            age = row.get("lock_file_age_seconds")
        try:
            age_float = float(age)
        except (TypeError, ValueError):
            age_float = None
        if age_float is not None and age_float < float(min_age_seconds):
            skipped.append(
                {
                    "name": name,
                    "lock_path": str(lock_path),
                    "reason": "too_recent_to_cleanup",
                    "age_seconds": round(age_float, 3),
                    "min_age_seconds": float(min_age_seconds),
                }
            )
            continue
        item = {
            "name": name,
            "lock_path": str(lock_path),
            "pid": row.get("pid"),
            "acquired_at": row.get("acquired_at"),
            "age_seconds": age_float,
        }
        candidates.append(item)
        if execute:
            try:
                lock_path.unlink()
                removed.append(item)
            except OSError as exc:
                skipped.append(
                    {
                        "name": name,
                        "lock_path": str(lock_path),
                        "reason": f"delete_failed:{exc.__class__.__name__}:{exc}",
                    }
                )
    report = {
        "cleanup_version": "singleton-lock-cleanup-v1",
        "generated_at": _iso_now(),
        "lock_dir": str(lock_dir),
        "execute": bool(execute),
        "min_age_seconds": float(min_age_seconds),
        "candidate_count": len(candidates),
        "removed_count": len(removed),
        "skipped_count": len(skipped),
        "candidates": candidates,
        "removed": removed,
        "skipped": skipped,
        "safety": {
            "active_pid_locks_never_deleted": True,
            "dry_run_default": True,
            "lock_dir_containment_checked": True,
            "recent_locks_skipped": True,
        },
    }
    if execute and event_log_path is not None:
        append_singleton_event(
            event_log_path,
            {
                "event_type": "stale_locks_cleanup",
                "candidate_count": len(candidates),
                "removed_count": len(removed),
                "skipped_count": len(skipped),
                "removed": removed,
            },
        )
    return report


class SingleInstanceLock:
    """Small cross-platform PID lock for long-running local CLI loops.

    This intentionally avoids destructive process cleanup.  A second instance exits
    when the recorded PID is still alive; stale lock files are reclaimed only when
    the PID is no longer running.
    """

    def __init__(self, name: str, path: Path) -> None:
        self.name = str(name)
        self.path = path.expanduser().resolve()
        self.acquired = False

    def __enter__(self) -> "SingleInstanceLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "lock_version": "singleton-pid-lock-v1",
            "name": self.name,
            "pid": os.getpid(),
            "acquired_at": _iso_now(),
            "cwd": str(Path.cwd()),
        }
        while True:
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, indent=2, ensure_ascii=False)
                self.acquired = True
                return self
            except FileExistsError:
                existing = _read_lock(self.path)
                existing_pid = int(existing.get("pid") or 0)
                if existing_pid and _pid_is_running(existing_pid):
                    existing["lock_path"] = str(self.path)
                    existing["current_pid"] = os.getpid()
                    existing["message"] = f"{self.name} is already running with pid {existing_pid}"
                    raise AlreadyRunningError(existing)
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    continue

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if not self.acquired:
            return
        try:
            existing = _read_lock(self.path)
            if int(existing.get("pid") or 0) == os.getpid():
                self.path.unlink()
        except FileNotFoundError:
            pass
        finally:
            self.acquired = False
