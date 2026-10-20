from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from recorder.db_backup import backup_database

CLEAN_DERIVED_REBUILD_VERSION = "clean-derived-rebuild-v1"

DEFAULT_DERIVED_TABLES: tuple[str, ...] = (
    "event_reaction_labels",
    "reaction_universe_assets",
    "reaction_universe_plans",
    "replay_outcomes",
    "replay_features",
    "macro_event_cluster_links",
    "news_event_cluster_items",
    "news_event_cluster_roles",
    "news_event_gates",
    "news_event_classifications",
    "news_article_cluster_links",
    "news_event_clusters",
    "news_article_dedup_links",
    "external_signal_source_links",
    "news_asset_candidates",
    "news_asset_mapping_runs",
    "news_enrichment_jobs",
    "replay_surprise_adjustment_notes",
    "replay_model_scores",
    "replay_market_outcomes",
    "replay_corpus",
    "standing_regime_outcomes",
    "standing_market_regimes",
    "geopolitical_signal_outcomes",
    "geopolitical_adaptive_asset_map",
    "technical_indicators",
)

DEFAULT_REBUILDABLE_EXTERNAL_SIGNAL_SOURCES: tuple[str, ...] = (
    "benzinga",
    "dow_jones",
    "emergency_breaking_news",
    "alpha_vantage",
    "gdelt",
)

RAW_TABLES_PRESERVED_BY_CLEAN_DERIVED_REBUILD: tuple[str, ...] = (
    "news_articles",
    "raw_news_articles",
    "provider_entities",
    "macro_release_calendar",
    "macro_event_releases",
    "macro_observations",
    "central_bank_events",
    "news_analyses",
    "official_documents",
    "financial_statement_facts",
    "fred_observations",
    "economic_indicator_observations",
    "international_macro_observations",
    "market_baselines",
    "market_reactions",
    "historical_events",
    "option_structure_outcome_logs",
    "m223_live_learning_predictions",
    "m223_live_learning_outcomes",
    "m223_live_learning_adjustment_notes",
)


def clean_derived_rebuild_reset(
    database_path: str | Path,
    backup_dir: str | Path,
    *,
    report_path: str | Path | None = None,
    keep_backups: int = 14,
    reset_marketaux_signals: bool = False,
    vacuum_after_reset: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Backup the DB, then remove only rebuildable derived training artifacts.

    The reset intentionally preserves raw/source tables and live/paper learning
    state.  It deletes only layers that the historical backfill/training runner
    can regenerate from stored source data or inexpensive derived rebuild steps.

    Marketaux rows are preserved by default because, unlike stored IBKR
    Benzinga/Dow Jones rows, they currently do not have a separate canonical raw
    article table in this project.  Pass ``reset_marketaux_signals=True`` only
    when the caller also plans to rerun Marketaux network backfill.
    """

    db_path = Path(database_path)
    if not db_path.exists():
        raise FileNotFoundError(f"Database not found: {db_path}")

    backup_result = backup_database(
        db_path,
        backup_dir,
        keep=keep_backups,
        now=now,
    )

    signal_sources = list(DEFAULT_REBUILDABLE_EXTERNAL_SIGNAL_SOURCES)
    preserved_external_signal_sources: list[str] = []
    if reset_marketaux_signals:
        signal_sources.append("marketaux")
    else:
        preserved_external_signal_sources.append("marketaux")

    operations: list[dict[str, Any]] = []
    conn = sqlite3.connect(db_path, timeout=60)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("BEGIN IMMEDIATE")
        for table in DEFAULT_DERIVED_TABLES:
            operations.append(_delete_table_rows(conn, table))
        operations.append(_clear_macro_event_cluster_refs(conn))

        operations.append(
            _delete_external_market_signals(
                conn,
                sources=tuple(signal_sources),
                preserved_sources=tuple(preserved_external_signal_sources),
            )
        )
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        try:
            conn.execute("PRAGMA foreign_keys=ON")
        finally:
            conn.close()

    vacuum_result: dict[str, Any] = {"enabled": bool(vacuum_after_reset), "ran": False}
    if vacuum_after_reset:
        vacuum_result = _vacuum_database(db_path)

    report: dict[str, Any] = {
        "version": CLEAN_DERIVED_REBUILD_VERSION,
        "database_path": str(db_path),
        "backup": backup_result,
        "operations": operations,
        "reset_marketaux_signals": bool(reset_marketaux_signals),
        "preserved_external_signal_sources": preserved_external_signal_sources,
        "preserved_raw_tables": list(RAW_TABLES_PRESERVED_BY_CLEAN_DERIVED_REBUILD),
        "vacuum": vacuum_result,
        "generated_at": (now or datetime.now(timezone.utc)).isoformat(),
        "safety": {
            "raw_source_tables_preserved": True,
            "live_learning_tables_preserved": True,
            "broker_orders_submitted": False,
            "transmit_enabled": False,
            "live_order_routing_enabled": False,
        },
    }
    if report_path:
        output = Path(report_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return report


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    if not _table_exists(conn, table):
        return False
    return column in {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _count_rows(conn: sqlite3.Connection, table: str, where_sql: str = "", params: tuple[Any, ...] = ()) -> int:
    row = conn.execute(f"SELECT COUNT(*) AS c FROM {table} {where_sql}", params).fetchone()
    return int(row["c"])


def _delete_table_rows(conn: sqlite3.Connection, table: str) -> dict[str, Any]:
    if not _table_exists(conn, table):
        return {
            "operation": "delete_table_rows",
            "table": table,
            "exists": False,
            "rows_before": None,
            "rows_deleted": 0,
            "rows_after": None,
        }
    before = _count_rows(conn, table)
    conn.execute(f"DELETE FROM {table}")
    after = _count_rows(conn, table)
    return {
        "operation": "delete_table_rows",
        "table": table,
        "exists": True,
        "rows_before": before,
        "rows_deleted": before - after,
        "rows_after": after,
    }


def _clear_macro_event_cluster_refs(conn: sqlite3.Connection) -> dict[str, Any]:
    table = "macro_event_releases"
    if not _column_exists(conn, table, "event_cluster_id"):
        return {
            "operation": "clear_macro_event_cluster_refs",
            "table": table,
            "exists": _table_exists(conn, table),
            "rows_before": None,
            "rows_updated": 0,
            "rows_after": None,
        }
    before = _count_rows(conn, table, "WHERE event_cluster_id IS NOT NULL")
    conn.execute("UPDATE macro_event_releases SET event_cluster_id=NULL WHERE event_cluster_id IS NOT NULL")
    after = _count_rows(conn, table, "WHERE event_cluster_id IS NOT NULL")
    return {
        "operation": "clear_macro_event_cluster_refs",
        "table": table,
        "exists": True,
        "rows_before": before,
        "rows_updated": before - after,
        "rows_after": after,
    }


def _delete_external_market_signals(
    conn: sqlite3.Connection,
    *,
    sources: tuple[str, ...],
    preserved_sources: tuple[str, ...],
) -> dict[str, Any]:
    table = "external_market_signals"
    if not _table_exists(conn, table):
        return {
            "operation": "delete_external_market_signals",
            "table": table,
            "exists": False,
            "sources": list(sources),
            "preserved_sources": list(preserved_sources),
            "rows_before": None,
            "rows_deleted": 0,
            "rows_after": None,
        }
    normalized = tuple(source.lower() for source in sources)
    placeholders = ",".join("?" for _ in normalized)
    where_sql = f"WHERE lower(source) IN ({placeholders})"
    before = _count_rows(conn, table, where_sql, normalized)
    conn.execute(f"DELETE FROM {table} {where_sql}", normalized)
    after = _count_rows(conn, table, where_sql, normalized)
    return {
        "operation": "delete_external_market_signals",
        "table": table,
        "exists": True,
        "sources": list(sources),
        "preserved_sources": list(preserved_sources),
        "rows_before": before,
        "rows_deleted": before - after,
        "rows_after": after,
    }


def _vacuum_database(db_path: Path) -> dict[str, Any]:
    started = datetime.now(timezone.utc)
    conn = sqlite3.connect(db_path, timeout=120)
    try:
        conn.execute("VACUUM")
    finally:
        conn.close()
    ended = datetime.now(timezone.utc)
    return {
        "enabled": True,
        "ran": True,
        "started_at": started.isoformat(),
        "ended_at": ended.isoformat(),
        "duration_seconds": round((ended - started).total_seconds(), 3),
    }
