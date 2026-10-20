from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

UTC = timezone.utc


def backup_database(
    source_path: str | Path,
    backup_dir: str | Path,
    keep: int = 14,
    now: datetime | None = None,
) -> dict[str, object]:
    """Create a consistent point-in-time copy of the SQLite database and prune old ones.

    Uses ``VACUUM INTO`` rather than a raw file copy so a backup taken while the
    recorder/AI/supervisor processes are actively writing (WAL mode) is still a
    valid, non-torn SQLite file.
    """
    source = Path(source_path)
    if not source.exists():
        raise FileNotFoundError(f"Database not found: {source}")

    target_dir = Path(backup_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    timestamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    backup_path = target_dir / f"{source.stem}_{timestamp}.db"
    if backup_path.exists():
        raise FileExistsError(f"Backup already exists for this timestamp: {backup_path}")

    conn = sqlite3.connect(source, timeout=30)
    try:
        conn.execute("VACUUM INTO ?", (str(backup_path),))
    finally:
        conn.close()

    removed = _prune_old_backups(target_dir, source.stem, keep)

    return {
        "source": str(source),
        "backup_path": str(backup_path),
        "backup_size_bytes": backup_path.stat().st_size,
        "kept": keep,
        "removed": [str(p) for p in removed],
    }


def _prune_old_backups(target_dir: Path, stem: str, keep: int) -> list[Path]:
    existing = sorted(
        target_dir.glob(f"{stem}_*.db"),
        key=lambda p: p.name,
        reverse=True,
    )
    stale = existing[keep:] if keep >= 0 else []
    for path in stale:
        path.unlink(missing_ok=True)
    return stale
