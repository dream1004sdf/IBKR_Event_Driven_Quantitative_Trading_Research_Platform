from __future__ import annotations

import hashlib
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from recorder.pipeline_diagnostics import GATE_AUDIT_SCHEMA

UTC = timezone.utc
MIGRATION_KEY = "prediction-evaluation-v0.1"
TIMESTAMP_PROVENANCE_MIGRATION_KEY = "prediction-evaluation-v0.14-timestamp-provenance"


def utc_iso(value: datetime | None = None) -> str:
    value = value or datetime.now(UTC)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


SCHEMA_SQL = r"""
CREATE TABLE IF NOT EXISTS schema_migrations (
    migration_key TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL,
    details TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS evaluation_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_uuid TEXT NOT NULL UNIQUE,
    run_type TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    policy_version TEXT NOT NULL,
    code_version TEXT NOT NULL,
    evaluation_as_of TEXT NOT NULL,
    eligible_count INTEGER NOT NULL DEFAULT 0,
    evaluated_count INTEGER NOT NULL DEFAULT 0,
    excluded_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    options_json TEXT NOT NULL DEFAULT '{}',
    error_message TEXT
);

CREATE INDEX IF NOT EXISTS idx_evaluation_runs_started
    ON evaluation_runs(started_at DESC);

CREATE TABLE IF NOT EXISTS replay_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_uuid TEXT NOT NULL UNIQUE,
    source_analysis_id INTEGER NOT NULL,
    news_id INTEGER NOT NULL,
    event_cluster_id INTEGER,
    symbol TEXT NOT NULL,
    event_type TEXT NOT NULL,
    replay_timestamp TEXT NOT NULL,
    prediction_cutoff_at TEXT NOT NULL,
    source_available_at TEXT NOT NULL DEFAULT '',
    timestamp_quality TEXT NOT NULL DEFAULT 'receipt_only',
    timestamp_source TEXT NOT NULL DEFAULT 'received_at',
    content_updated_at TEXT,
    status TEXT NOT NULL DEFAULT 'queued',
    attempts INTEGER NOT NULL DEFAULT 0,
    provider_name TEXT NOT NULL DEFAULT '',
    model_name TEXT NOT NULL DEFAULT '',
    prompt_version TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    evidence_hash TEXT NOT NULL DEFAULT '',
    leakage_check_status TEXT NOT NULL DEFAULT 'pending',
    leakage_reasons_json TEXT NOT NULL DEFAULT '[]',
    snapshot_json TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(source_analysis_id) REFERENCES news_analyses(id),
    FOREIGN KEY(news_id) REFERENCES news_articles(id),
    UNIQUE(source_analysis_id, replay_timestamp, prompt_version, input_hash)
);

CREATE INDEX IF NOT EXISTS idx_replay_jobs_status
    ON replay_jobs(status, replay_timestamp);

CREATE TABLE IF NOT EXISTS replay_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL,
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    published_at TEXT,
    available_at TEXT,
    updated_at TEXT,
    used_at TEXT,
    allowed_at_replay_time INTEGER NOT NULL,
    exclusion_reason TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY(job_id) REFERENCES replay_jobs(id) ON DELETE CASCADE,
    UNIQUE(job_id, source_type, source_id)
);

CREATE TABLE IF NOT EXISTS replay_analyses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL UNIQUE,
    provider_name TEXT NOT NULL,
    model_name TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    response_id TEXT,
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER,
    output_tokens INTEGER,
    event_type TEXT NOT NULL,
    event_stage TEXT NOT NULL,
    prediction_eligible INTEGER NOT NULL,
    attribution_status TEXT NOT NULL,
    source_tier TEXT NOT NULL,
    confidence REAL NOT NULL,
    raw_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(job_id) REFERENCES replay_jobs(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS replay_predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    replay_analysis_id INTEGER NOT NULL,
    job_id INTEGER NOT NULL,
    news_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    horizon TEXT NOT NULL,
    expected_return REAL NOT NULL,
    bear_return REAL NOT NULL,
    bull_return REAL NOT NULL,
    up_probability REAL NOT NULL,
    flat_probability REAL NOT NULL,
    down_probability REAL NOT NULL,
    confidence REAL NOT NULL,
    prediction_as_of TEXT NOT NULL,
    evaluation_due_at TEXT,
    evaluation_status TEXT NOT NULL,
    quality_reason TEXT,
    is_valid INTEGER NOT NULL,
    valid_for_directional_trading INTEGER NOT NULL DEFAULT 0,
    valid_for_probability_calibration INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    FOREIGN KEY(replay_analysis_id) REFERENCES replay_analyses(id) ON DELETE CASCADE,
    FOREIGN KEY(job_id) REFERENCES replay_jobs(id) ON DELETE CASCADE,
    FOREIGN KEY(news_id) REFERENCES news_articles(id),
    UNIQUE(replay_analysis_id, symbol, horizon)
);

CREATE TABLE IF NOT EXISTS prediction_outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    prediction_ref_type TEXT NOT NULL,
    prediction_ref_id INTEGER NOT NULL,
    prediction_id INTEGER,
    replay_prediction_id INTEGER,
    analysis_id INTEGER,
    news_id INTEGER NOT NULL,
    reaction_id INTEGER,
    target_id INTEGER,
    producer_type TEXT NOT NULL,
    producer_name TEXT NOT NULL,
    model_name TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    event_type TEXT NOT NULL,
    horizon TEXT NOT NULL,
    symbol TEXT NOT NULL,
    source_class TEXT NOT NULL,
    confidence_bucket TEXT NOT NULL,
    prediction_created_at TEXT NOT NULL,
    prediction_as_of TEXT NOT NULL,
    reaction_due_at TEXT,
    reaction_captured_at TEXT,
    anchor_price REAL,
    target_price REAL,
    expected_return REAL,
    realized_return REAL,
    predicted_direction TEXT,
    realized_direction TEXT,
    p_up REAL,
    p_flat REAL,
    p_down REAL,
    absolute_error REAL,
    squared_error REAL,
    signed_error REAL,
    bear_return REAL,
    base_return REAL,
    bull_return REAL,
    scenario_range_hit INTEGER,
    eligibility_status TEXT NOT NULL,
    exclusion_reason TEXT,
    quote_integrity TEXT NOT NULL,
    attribution_valid INTEGER NOT NULL,
    point_in_time_valid INTEGER NOT NULL,
    policy_version TEXT NOT NULL,
    evaluated_at TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES evaluation_runs(id),
    FOREIGN KEY(prediction_id) REFERENCES predictions(id),
    FOREIGN KEY(replay_prediction_id) REFERENCES replay_predictions(id),
    FOREIGN KEY(reaction_id) REFERENCES market_reactions(id),
    FOREIGN KEY(target_id) REFERENCES reaction_targets(id),
    UNIQUE(prediction_ref_type, prediction_ref_id, policy_version)
);

CREATE INDEX IF NOT EXISTS idx_prediction_outcomes_report
    ON prediction_outcomes(policy_version, eligibility_status, event_type, horizon, model_name);
CREATE INDEX IF NOT EXISTS idx_prediction_outcomes_symbol
    ON prediction_outcomes(symbol, evaluated_at);

CREATE TABLE IF NOT EXISTS model_performance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    policy_version TEXT NOT NULL,
    segment_level TEXT NOT NULL,
    producer_type TEXT,
    producer_name TEXT,
    model_name TEXT,
    prompt_version TEXT,
    event_type TEXT,
    horizon TEXT,
    symbol TEXT,
    source_class TEXT,
    confidence_bucket TEXT,
    sample_count INTEGER NOT NULL,
    direction_accuracy REAL,
    up_precision REAL,
    up_recall REAL,
    up_f1 REAL,
    down_precision REAL,
    down_recall REAL,
    down_f1 REAL,
    flat_precision REAL,
    flat_recall REAL,
    flat_f1 REAL,
    brier_score REAL,
    log_loss REAL,
    calibration_error REAL,
    mae REAL,
    rmse REAL,
    bias REAL,
    return_correlation REAL,
    scenario_range_coverage REAL,
    confusion_matrix_json TEXT NOT NULL,
    reliability_curve_json TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES evaluation_runs(id)
);

CREATE INDEX IF NOT EXISTS idx_model_performance_lookup
    ON model_performance(policy_version, segment_level, event_type, horizon, model_name);

CREATE TABLE IF NOT EXISTS calibration_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_uuid TEXT NOT NULL UNIQUE,
    version TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    status TEXT NOT NULL,
    method TEXT NOT NULL,
    target_class TEXT NOT NULL,
    fallback_level TEXT NOT NULL,
    producer_type TEXT,
    producer_name TEXT,
    model_name TEXT,
    prompt_version TEXT,
    event_type TEXT,
    horizon TEXT,
    symbol TEXT,
    source_class TEXT,
    training_start TEXT,
    training_end TEXT,
    validation_start TEXT,
    validation_end TEXT,
    sample_count INTEGER NOT NULL,
    validation_count INTEGER NOT NULL DEFAULT 0,
    bin_edges_json TEXT NOT NULL DEFAULT '[]',
    parameters_json TEXT NOT NULL DEFAULT '{}',
    metrics_before_json TEXT NOT NULL DEFAULT '{}',
    metrics_after_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_calibration_profiles_lookup
    ON calibration_profiles(version, status, target_class, event_type, horizon, model_name);
"""


class EvaluationDatabase:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=10000")
        try:
            yield conn
        finally:
            conn.close()

    def migrate(self) -> bool:
        checksum = hashlib.sha256(SCHEMA_SQL.encode("utf-8")).hexdigest()
        with self.connect() as conn:
            conn.executescript(SCHEMA_SQL)
            conn.executescript(GATE_AUDIT_SCHEMA)
            row = conn.execute(
                "SELECT 1 FROM schema_migrations WHERE migration_key=?", (MIGRATION_KEY,)
            ).fetchone()
            applied = row is None
            if applied:
                conn.execute(
                    "INSERT INTO schema_migrations(migration_key,applied_at,details) VALUES (?,?,?)",
                    (MIGRATION_KEY, utc_iso(), f"sha256={checksum}"),
                )
            replay_columns = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(replay_jobs)").fetchall()
            }
            provenance_columns = {
                "source_available_at": "TEXT NOT NULL DEFAULT ''",
                "timestamp_quality": "TEXT NOT NULL DEFAULT 'receipt_only'",
                "timestamp_source": "TEXT NOT NULL DEFAULT 'received_at'",
                "content_updated_at": "TEXT",
            }
            for column, definition in provenance_columns.items():
                if column not in replay_columns:
                    conn.execute(f"ALTER TABLE replay_jobs ADD COLUMN {column} {definition}")
            provenance_row = conn.execute(
                "SELECT 1 FROM schema_migrations WHERE migration_key=?",
                (TIMESTAMP_PROVENANCE_MIGRATION_KEY,),
            ).fetchone()
            if provenance_row is None:
                conn.execute(
                    "INSERT INTO schema_migrations(migration_key,applied_at,details) VALUES (?,?,?)",
                    (
                        TIMESTAMP_PROVENANCE_MIGRATION_KEY,
                        utc_iso(),
                        "added immutable replay timestamp provenance columns",
                    ),
                )
        return applied
