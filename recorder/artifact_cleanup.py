from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


ARTIFACT_CLEANUP_VERSION = "artifact-cleanup-v2-status-exposure"
_MILESTONE_PREFIX = re.compile(r"^m\d+_")
_ROTATED_SUFFIX = re.compile(r"\.\d+$")


@dataclass(frozen=True)
class CleanupCandidate:
    path: Path
    reason: str
    size_bytes: int
    last_write_time: str


def _now(now: datetime | None = None) -> datetime:
    value = now or datetime.now(UTC)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _file_mtime(path: Path) -> datetime:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _logical_export_key(path: Path) -> str:
    name = _ROTATED_SUFFIX.sub("", path.name)
    return _MILESTONE_PREFIX.sub("", name)


def _safe_child(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _candidate(path: Path, reason: str) -> CleanupCandidate:
    return CleanupCandidate(
        path=path,
        reason=reason,
        size_bytes=path.stat().st_size if path.is_file() else _dir_size(path),
        last_write_time=_iso(_file_mtime(path)),
    )


def _dir_size(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        if item.is_file():
            try:
                total += item.stat().st_size
            except OSError:
                continue
    return total


def _delete_path(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def _cleanup_exports_candidates(
    exports_dir: Path,
    *,
    keep_latest_per_logical_key: int,
) -> list[CleanupCandidate]:
    if not exports_dir.exists():
        return []
    groups: dict[str, list[Path]] = {}
    for path in exports_dir.iterdir():
        if not path.is_file():
            continue
        if path.name.startswith("."):
            continue
        key = _logical_export_key(path)
        groups.setdefault(key, []).append(path)

    candidates: list[CleanupCandidate] = []
    keep = max(0, int(keep_latest_per_logical_key))
    for key, paths in groups.items():
        ordered = sorted(paths, key=lambda item: item.stat().st_mtime, reverse=True)
        for stale in ordered[keep:]:
            candidates.append(_candidate(stale, f"exports_keep_latest_per_logical_key:{key}:keep={keep}"))
    return candidates


def _cleanup_work_candidates(
    work_dir: Path,
    *,
    now: datetime,
    work_max_age_days: int,
) -> list[CleanupCandidate]:
    if not work_dir.exists():
        return []
    cutoff = now - timedelta(days=max(0, int(work_max_age_days)))
    candidates: list[CleanupCandidate] = []
    for path in work_dir.iterdir():
        if path.name in {".", ".."}:
            continue
        try:
            mtime = _file_mtime(path)
        except OSError:
            continue
        if mtime < cutoff:
            candidates.append(_candidate(path, f"work_older_than_{work_max_age_days}_days"))
    return candidates


def build_artifact_cleanup_report(
    *,
    exports_dir: str | Path = "exports",
    work_dir: str | Path = "work",
    keep_latest_per_logical_key: int = 5,
    include_work: bool = True,
    work_max_age_days: int = 14,
    execute: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    now_dt = _now(now)
    exports_root = Path(exports_dir).expanduser().resolve()
    work_root = Path(work_dir).expanduser().resolve()
    directory_sizes = {
        "exports_size_bytes": _dir_size(exports_root) if exports_root.exists() else 0,
        "work_size_bytes": _dir_size(work_root) if work_root.exists() else 0,
    }
    candidates = _cleanup_exports_candidates(
        exports_root,
        keep_latest_per_logical_key=keep_latest_per_logical_key,
    )
    if include_work:
        candidates.extend(
            _cleanup_work_candidates(
                work_root,
                now=now_dt,
                work_max_age_days=work_max_age_days,
            )
        )

    deleted: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for item in candidates:
        root = work_root if _safe_child(item.path, work_root) else exports_root
        if not _safe_child(item.path, root):
            skipped.append({"path": str(item.path), "reason": "outside_allowed_cleanup_roots"})
            continue
        if execute:
            try:
                _delete_path(item.path)
                deleted.append({"path": str(item.path), "reason": item.reason, "size_bytes": item.size_bytes})
            except Exception as exc:  # pragma: no cover - defensive cleanup reporting
                skipped.append({"path": str(item.path), "reason": f"delete_failed:{exc.__class__.__name__}:{exc}"})

    candidate_rows = [
        {
            "path": str(item.path),
            "reason": item.reason,
            "size_bytes": item.size_bytes,
            "last_write_time": item.last_write_time,
        }
        for item in candidates
    ]
    return {
        "artifact_cleanup_version": ARTIFACT_CLEANUP_VERSION,
        "generated_at": _iso(now_dt),
        "execute": bool(execute),
        "exports_dir": str(exports_root),
        "work_dir": str(work_root),
        "include_work": bool(include_work),
        "policy": {
            "keep_latest_per_logical_key": int(keep_latest_per_logical_key),
            "work_max_age_days": int(work_max_age_days),
            "default_mode": "dry_run_requires_execute",
        },
        "candidate_count": len(candidate_rows),
        "candidate_size_bytes": sum(int(row["size_bytes"]) for row in candidate_rows),
        "directory_sizes": directory_sizes,
        "directory_size_mb": {key.replace("_bytes", "_mb"): round(value / 1024 / 1024, 3) for key, value in directory_sizes.items()},
        "deleted_count": len(deleted),
        "deleted_size_bytes": sum(int(row["size_bytes"]) for row in deleted),
        "skipped_count": len(skipped),
        "candidates": candidate_rows,
        "deleted": deleted,
        "skipped": skipped,
        "safety": {
            "dry_run_default": True,
            "requires_execute_to_delete": True,
            "cleanup_roots_limited_to_exports_and_work": True,
        },
    }


def artifact_cleanup_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Artifact Cleanup Report",
        "",
        f"- Version: `{report.get('artifact_cleanup_version')}`",
        f"- Generated at: `{report.get('generated_at')}`",
        f"- Execute: `{report.get('execute')}`",
        f"- Candidate count: `{report.get('candidate_count')}`",
        f"- Candidate size MB: `{(int(report.get('candidate_size_bytes') or 0) / 1024 / 1024):.2f}`",
        f"- Directory size MB: `{report.get('directory_size_mb') or {}}`",
        f"- Deleted count: `{report.get('deleted_count')}`",
        f"- Deleted size MB: `{(int(report.get('deleted_size_bytes') or 0) / 1024 / 1024):.2f}`",
        "",
        "| Path | Size MB | Reason |",
        "| --- | ---: | --- |",
    ]
    for row in (report.get("candidates") or [])[:200]:
        lines.append(
            f"| `{row.get('path')}` | {(int(row.get('size_bytes') or 0) / 1024 / 1024):.2f} | {row.get('reason') or '-'} |"
        )
    if len(report.get("candidates") or []) > 200:
        lines.append(f"| ... | ... | {len(report.get('candidates') or []) - 200} more candidates omitted |")
    lines.append("")
    lines.append("- Safety: dry-run by default; actual deletion requires `--execute`.")
    return "\n".join(lines)


def write_artifact_cleanup_json(output_path: Path, report: dict[str, Any]) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return output_path


def write_artifact_cleanup_markdown(output_path: Path, report: dict[str, Any]) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(artifact_cleanup_markdown(report), encoding="utf-8")
    return output_path
