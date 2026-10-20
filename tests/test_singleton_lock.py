from __future__ import annotations

import json
import os

import pytest

from recorder.singleton_lock import (
    AlreadyRunningError,
    SingleInstanceLock,
    cleanup_stale_singleton_locks,
    read_singleton_lock_status,
)


def test_singleton_lock_blocks_active_pid(tmp_path) -> None:
    lock_path = tmp_path / "loop.lock"
    with SingleInstanceLock("test-loop", lock_path):
        with pytest.raises(AlreadyRunningError) as raised:
            with SingleInstanceLock("test-loop", lock_path):
                pass
    assert raised.value.payload["pid"] == os.getpid()
    assert not lock_path.exists()


def test_singleton_lock_reclaims_stale_pid(tmp_path) -> None:
    lock_path = tmp_path / "loop.lock"
    lock_path.write_text(
        json.dumps({"lock_version": "singleton-pid-lock-v1", "name": "test-loop", "pid": 99999999}),
        encoding="utf-8",
    )
    with SingleInstanceLock("test-loop", lock_path):
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
        assert payload["pid"] == os.getpid()
    assert not lock_path.exists()


def test_singleton_lock_status_separates_process_lock_and_artifact_truth(tmp_path) -> None:
    lock_dir = tmp_path / "locks"
    lock_dir.mkdir()
    artifact = tmp_path / "artifact.json"
    artifact.write_text("{}", encoding="utf-8")
    lock_path = lock_dir / "loop.lock"
    lock_path.write_text(
        json.dumps(
            {
                "lock_version": "singleton-pid-lock-v1",
                "name": "loop",
                "pid": 99999999,
                "acquired_at": "2026-01-01T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )

    status = read_singleton_lock_status(
        lock_dir=lock_dir,
        expected_names=("loop",),
        artifact_paths={"loop": artifact},
        artifact_stale_seconds={"loop": 999999999.0},
    )

    assert status["status_counts"]["stale_lock"] == 1
    assert status["truth_summary"]["process_online_count"] == 0
    assert status["truth_summary"]["lock_file_count"] == 1
    assert status["truth_summary"]["artifact_fresh_count"] == 1
    assert status["truth_summary"]["fresh_artifact_without_process_count"] == 1


def test_cleanup_stale_singleton_locks_removes_only_dead_pid_locks(tmp_path) -> None:
    lock_dir = tmp_path / "locks"
    lock_dir.mkdir()
    stale_path = lock_dir / "stale.lock"
    stale_path.write_text(
        json.dumps(
            {
                "lock_version": "singleton-pid-lock-v1",
                "name": "stale",
                "pid": 99999999,
                "acquired_at": "2026-01-01T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    active_path = lock_dir / "active.lock"
    with SingleInstanceLock("active", active_path):
        dry_run = cleanup_stale_singleton_locks(
            lock_dir=lock_dir,
            status=read_singleton_lock_status(lock_dir=lock_dir, expected_names=("stale", "active")),
            min_age_seconds=0,
            execute=False,
        )
        assert dry_run["candidate_count"] == 1
        assert stale_path.exists()
        assert active_path.exists()

        executed = cleanup_stale_singleton_locks(
            lock_dir=lock_dir,
            status=read_singleton_lock_status(lock_dir=lock_dir, expected_names=("stale", "active")),
            min_age_seconds=0,
            execute=True,
        )
        assert executed["removed_count"] == 1
        assert not stale_path.exists()
        assert active_path.exists()
