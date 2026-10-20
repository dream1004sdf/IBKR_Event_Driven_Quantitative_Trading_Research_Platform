from __future__ import annotations

import json
import os
import platform
import shutil
import sqlite3
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from recorder.db_audit import audit_database
from recorder.pipeline_diagnostics import PipelineDiagnostics

from .calibration import CalibrationEngine
from .config import EvaluationConfig
from .evaluation import PredictionEvaluationEngine
from .replay import HistoricalReplayEngine

UTC = timezone.utc


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")


def _safe_database_backup(source: Path, destination: Path) -> None:
    source_conn = sqlite3.connect(source, timeout=30)
    destination_conn = sqlite3.connect(destination)
    try:
        source_conn.execute("PRAGMA busy_timeout=10000")
        source_conn.backup(destination_conn)
    finally:
        destination_conn.close()
        source_conn.close()


def create_analysis_bundle(
    *,
    settings: Any,
    config: EvaluationConfig,
    output: str | Path | None = None,
    include_logs: bool = False,
) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    output_path = Path(output) if output else Path(f"ibkr_analysis_bundle_{timestamp}.zip")
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    diagnostics = PipelineDiagnostics(
        config.database_path,
        official_required_event_types=getattr(settings, "prediction_official_required_event_types", ()),
    )
    evaluation = PredictionEvaluationEngine(config)
    calibration = CalibrationEngine(config)
    replay = HistoricalReplayEngine(settings, config)

    with tempfile.TemporaryDirectory(prefix="ibkr-analysis-bundle-") as tmp:
        root = Path(tmp) / f"ibkr_analysis_bundle_{timestamp}"
        root.mkdir(parents=True)

        _safe_database_backup(Path(config.database_path), root / "market_events_snapshot.db")
        _write_json(root / "pipeline_report.json", diagnostics.report(recent_limit=100))
        _write_json(root / "official_readiness_report.json", diagnostics.official_readiness_report())
        _write_json(root / "replay_eligibility_report.json", diagnostics.replay_eligibility_report(recent_limit=200))
        _write_json(root / "evaluation_report.json", evaluation.report(show_exclusions=True))
        _write_json(root / "calibration_report.json", calibration.report())
        _write_json(root / "replay_report.json", replay.report())
        _write_json(root / "replay_audit.json", replay.audit(strict=True))
        _write_json(root / "database_audit.json", audit_database(config.database_path))
        _write_json(
            root / "environment.json",
            {
                "created_at": datetime.now(UTC).isoformat(),
                "python": sys.version,
                "python_executable": sys.executable,
                "platform": platform.platform(),
                "working_directory": os.getcwd(),
                "evaluation_policy_version": config.policy_version,
                "evaluation_code_version": config.code_version,
                "database_original_path": str(config.database_path),
                "note": "API keys, .env contents, account credentials, and tokens are intentionally excluded.",
            },
        )
        (root / "README.txt").write_text(
            "IBKR recorder/evaluation diagnostic bundle\n"
            "\n"
            "This bundle contains a transactionally consistent SQLite snapshot and read-only reports.\n"
            "It intentionally excludes .env, API keys, IBKR credentials, and authentication tokens.\n",
            encoding="utf-8",
        )

        if include_logs:
            logs_dir = Path("logs")
            if logs_dir.is_dir():
                target_logs = root / "logs"
                target_logs.mkdir()
                for source in sorted(logs_dir.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)[:10]:
                    # Copy only ordinary log files; do not follow arbitrary directories.
                    if source.is_file() and source.stat().st_size <= 20 * 1024 * 1024:
                        shutil.copy2(source, target_logs / source.name)

        with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for file_path in sorted(root.rglob("*")):
                if file_path.is_file():
                    archive.write(file_path, file_path.relative_to(root.parent))

    return output_path
