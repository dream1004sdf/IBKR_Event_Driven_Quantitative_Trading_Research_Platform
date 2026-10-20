from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from recorder.news_lineage_v2 import ensure_news_lineage_v2_schema


def _utc_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%d_%H%M%S")


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=60)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        is not None
    )


def _count(conn: sqlite3.Connection, table: str) -> int:
    if not _table_exists(conn, table):
        return 0
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _rows(conn: sqlite3.Connection, sql: str, *, limit: int = 20) -> list[dict[str, Any]]:
    limited = f"SELECT * FROM ({sql.rstrip().rstrip(';')}) LIMIT {int(limit)}"
    return [dict(row) for row in conn.execute(limited).fetchall()]


def _scalar(conn: sqlite3.Connection, sql: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM ({sql.rstrip().rstrip(';')})").fetchone()[0])


AUDIT_SQL = {
    "duplicate_mapping_run_key": """
        SELECT build_id, mapping_run_key, COUNT(*) AS cnt
          FROM news_asset_mapping_runs
         GROUP BY build_id, mapping_run_key
        HAVING COUNT(*) > 1
    """,
    "duplicate_candidate_key": """
        SELECT mapping_run_id, candidate_key, COUNT(*) AS cnt
          FROM news_asset_candidates
         GROUP BY mapping_run_id, candidate_key
        HAVING COUNT(*) > 1
    """,
    "same_article_same_version_mapping_run": """
        SELECT
          build_id,
          news_id,
          COALESCE(event_cluster_id, -1) AS event_cluster_id_norm,
          mapping_scope,
          mapper_version,
          resolver_version,
          security_master_version,
          input_hash,
          config_hash,
          COUNT(*) AS cnt
        FROM news_asset_mapping_runs
        GROUP BY
          build_id,
          news_id,
          COALESCE(event_cluster_id, -1),
          mapping_scope,
          mapper_version,
          resolver_version,
          security_master_version,
          input_hash,
          config_hash
        HAVING COUNT(*) > 1
    """,
    "ranking_input_duplicate_risk": """
        SELECT
          c.event_cluster_id,
          a.symbol,
          a.role,
          a.source,
          COUNT(*) AS cnt
        FROM news_asset_candidates a
        JOIN news_asset_mapping_runs r
          ON a.mapping_run_id = r.mapping_run_id
        LEFT JOIN news_article_cluster_links c
          ON r.news_id = c.news_id
        GROUP BY
          c.event_cluster_id,
          a.symbol,
          a.role,
          a.source
        HAVING COUNT(*) > 1
        ORDER BY cnt DESC
    """,
    "marketaux_signal_missing_source_news_id": """
        SELECT id, source, external_id, title, source_news_id
          FROM external_market_signals
         WHERE source='marketaux'
           AND (source_news_id IS NULL OR source_news_id=0)
    """,
    "candidate_missing_mapping_run_id": """
        SELECT id, news_id, event_cluster_id, symbol, role, source
          FROM news_asset_candidates
         WHERE mapping_run_id IS NULL
    """,
    "bad_spy_fallback_context": """
        SELECT id, news_id, event_cluster_id, symbol, role, source, tradable_candidate, evidence
          FROM news_asset_candidates
         WHERE upper(symbol)='SPY'
           AND (
                lower(COALESCE(source,'')) LIKE '%fallback%'
             OR lower(COALESCE(source,'')) LIKE '%context%'
             OR lower(COALESCE(role,'')) LIKE '%fallback%'
             OR lower(COALESCE(role,'')) LIKE '%context%'
             OR lower(COALESCE(evidence,'')) LIKE '%fallback%'
             OR lower(COALESCE(evidence,'')) LIKE '%context%'
           )
           AND (
                tradable_candidate=1
             OR lower(COALESCE(role,'')) IN ('primary','proxy','direct_entity')
           )
    """,
    "bad_route_only_tradable": """
        SELECT id, news_id, event_cluster_id, symbol, role, source, tradable_candidate, evidence
          FROM news_asset_candidates
         WHERE lower(COALESCE(role,''))='route_only'
           AND tradable_candidate=1
    """,
}


def _backup_database(database_path: Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"before_asset_candidate_idempotency_repair_{_utc_stamp()}_{database_path.name}"
    source = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    dest = sqlite3.connect(backup_path, timeout=60)
    try:
        source.backup(dest)
    finally:
        dest.close()
        source.close()
    return backup_path


def _audit(conn: sqlite3.Connection, *, sample_limit: int) -> dict[str, Any]:
    audit: dict[str, Any] = {}
    for name, sql in AUDIT_SQL.items():
        if name in {
            "marketaux_signal_missing_source_news_id",
        } and not _table_exists(conn, "external_market_signals"):
            audit[name] = {"count": 0, "samples": []}
            continue
        if "news_asset" in sql and not _table_exists(conn, "news_asset_candidates"):
            audit[name] = {"count": 0, "samples": []}
            continue
        if "news_article_cluster_links" in sql and not _table_exists(conn, "news_article_cluster_links"):
            audit[name] = {"count": 0, "samples": []}
            continue
        try:
            count = _scalar(conn, sql)
            samples = _rows(conn, sql, limit=sample_limit) if count else []
        except sqlite3.Error as exc:
            count = -1
            samples = [{"error": str(exc)}]
        audit[name] = {
            "count": count,
            "samples": samples,
        }
    return audit


def repair_asset_candidate_idempotency(
    database_path: Path,
    *,
    backup_dir: Path,
    sample_limit: int = 20,
    audit_only: bool = False,
) -> dict[str, Any]:
    if not database_path.exists():
        raise FileNotFoundError(database_path)
    backup_path: Path | None = None
    if not audit_only:
        backup_path = _backup_database(database_path, backup_dir)

    with _connect(database_path) as conn:
        before_counts = {
            table: _count(conn, table)
            for table in (
                "raw_news_articles",
                "external_market_signals",
                "provider_entities",
                "news_asset_mapping_runs",
                "news_asset_candidates",
                "news_event_clusters",
            )
        }
        before_audit = _audit(conn, sample_limit=sample_limit)
        if not audit_only:
            ensure_news_lineage_v2_schema(conn)
            conn.commit()
        after_counts = {
            table: _count(conn, table)
            for table in (
                "raw_news_articles",
                "external_market_signals",
                "provider_entities",
                "news_asset_mapping_runs",
                "news_asset_candidates",
                "news_event_clusters",
            )
        }
        after_audit = _audit(conn, sample_limit=sample_limit)

    return {
        "database_path": str(database_path),
        "backup_path": str(backup_path) if backup_path else "",
        "audit_only": audit_only,
        "before_counts": before_counts,
        "after_counts": after_counts,
        "before_audit": before_audit,
        "after_audit": after_audit,
        "hard_gate_pass": all(
            int(after_audit[name]["count"]) == 0
            for name in (
                "duplicate_mapping_run_key",
                "duplicate_candidate_key",
                "same_article_same_version_mapping_run",
                "marketaux_signal_missing_source_news_id",
                "candidate_missing_mapping_run_id",
                "bad_spy_fallback_context",
                "bad_route_only_tradable",
            )
        ),
        "ranking_duplicate_risk_note": (
            "ranking_input_duplicate_risk can be nonzero because separate articles in the same cluster can support "
            "the same symbol; ranking must consume cluster-level aggregates, not raw candidate row counts."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Repair M251 asset mapping/candidate idempotency.")
    parser.add_argument(
        "--database-path",
        type=Path,
        default=Path(os.environ.get("DATABASE_PATH", "market_events.db")),
    )
    parser.add_argument("--backup-dir", type=Path, default=Path("backups"))
    parser.add_argument("--sample-limit", type=int, default=20)
    parser.add_argument("--audit-only", action="store_true")
    parser.add_argument("--json-output", type=Path)
    args = parser.parse_args()

    payload = repair_asset_candidate_idempotency(
        args.database_path,
        backup_dir=args.backup_dir,
        sample_limit=args.sample_limit,
        audit_only=bool(args.audit_only),
    )
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if payload.get("hard_gate_pass") else 1


if __name__ == "__main__":
    raise SystemExit(main())
