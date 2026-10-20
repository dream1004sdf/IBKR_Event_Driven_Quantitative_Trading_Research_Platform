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

from recorder.news_lineage_v2 import (  # noqa: E402
    NEWS_CLASSIFICATION_GATE_CANONICAL_VERSION,
    assign_article_to_event_cluster,
    ensure_news_lineage_v2_schema,
    link_external_signal_to_source_news,
    rebuild_canonical_classification_gate_tables,
    recompute_event_cluster_roles,
)

DEFAULT_DATABASE_PATH = (
    ROOT
    / "work"
    / "canonical_research_backfill"
    / "m254_canonical_20250101_20260706_v1"
    / "canonical_market_events.db"
)
DEFAULT_BACKUP_DIR = (
    ROOT
    / "work"
    / "canonical_research_backfill"
    / "m254_canonical_20250101_20260706_v1"
    / "backups"
)
S03_BZ_ORPHAN_EXTERNAL_IDS = ("bz:7501", "bz:7473", "bz:62557")

PROVIDERS: dict[str, dict[str, str]] = {
    "BZ": {
        "signal_prefix": "bz",
        "signal_source": "benzinga",
        "legacy_where": "provider='BZ'",
        "raw_where": "provider='BZ'",
    },
    "DJ": {
        "signal_prefix": "dj",
        "signal_source": "dow_jones",
        "legacy_where": "provider LIKE 'DJ%'",
        "raw_where": "provider LIKE 'DJ%'",
    },
}


def _utc_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%d_%H%M%S")


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=60000")
    return conn


def _backup_database(database_path: Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"before_s03_lineage_canonicalization_{_utc_stamp()}_{database_path.name}"
    source = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=60)
    dest = sqlite3.connect(backup_path, timeout=60)
    try:
        source.backup(dest)
    finally:
        dest.close()
        source.close()
    return backup_path


def _relation_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?",
            (name,),
        ).fetchone()
        is not None
    )


def _count_sql(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM ({sql.rstrip().rstrip(';')})", params).fetchone()[0])


def _sum_excess_sql(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> int:
    row = conn.execute(f"SELECT COALESCE(SUM(cnt - 1), 0) FROM ({sql.rstrip().rstrip(';')})", params).fetchone()
    return int(row[0] or 0)


def _samples(
    conn: sqlite3.Connection,
    sql: str,
    params: tuple[Any, ...] = (),
    *,
    limit: int = 20,
) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(f"SELECT * FROM ({sql.rstrip().rstrip(';')}) LIMIT {int(limit)}", params).fetchall()
    ]


def _group_audit(
    conn: sqlite3.Connection,
    sql: str,
    params: tuple[Any, ...] = (),
    *,
    sample_limit: int,
) -> dict[str, Any]:
    groups = _count_sql(conn, sql, params)
    return {
        "groups": groups,
        "excess_rows": _sum_excess_sql(conn, sql, params) if groups else 0,
        "samples": _samples(conn, sql, params, limit=sample_limit) if groups else [],
    }


def _provider_lineage_audit(
    conn: sqlite3.Connection,
    provider_name: str,
    *,
    sample_limit: int,
) -> dict[str, Any]:
    spec = PROVIDERS[provider_name]
    prefix = spec["signal_prefix"]
    source = spec["signal_source"]
    raw_where = spec["raw_where"]
    legacy_where = spec["legacy_where"]
    signal_where = "s.external_id LIKE ? AND s.source=?"
    signal_params = (f"{prefix}:%", source)

    legacy_total = _count_sql(conn, f"SELECT id FROM news_articles WHERE {legacy_where}")
    raw_total = _count_sql(conn, f"SELECT id FROM raw_news_articles WHERE {raw_where}")
    signal_total = _count_sql(
        conn,
        "SELECT s.id FROM external_market_signals s WHERE " + signal_where,
        signal_params,
    )
    legacy_with_signal = _count_sql(
        conn,
        f"""SELECT n.id
              FROM news_articles n
             WHERE {legacy_where}
               AND EXISTS (
                   SELECT 1
                     FROM external_market_signals s
                    WHERE s.external_id=? || ':' || n.id
                      AND s.source=?
               )""",
        (prefix, source),
    )
    source_raw_no_match_sql = (
        "SELECT s.id,s.external_id,s.source_news_id,s.event_cluster_id,s.title "
        "FROM external_market_signals s "
        "LEFT JOIN raw_news_articles r ON r.id=s.source_news_id "
        f"WHERE {signal_where} AND (s.source_news_id IS NULL OR r.id IS NULL) "
        "ORDER BY s.id"
    )
    source_raw_wrong_provider_sql = (
        "SELECT s.id,s.external_id,s.source_news_id,r.provider,s.event_cluster_id,s.title "
        "FROM external_market_signals s "
        "JOIN raw_news_articles r ON r.id=s.source_news_id "
        f"WHERE {signal_where} AND NOT ({raw_where.replace('provider', 'r.provider')}) "
        "ORDER BY s.id"
    )
    missing_cluster_sql = (
        "SELECT s.id,s.external_id,s.source_news_id,s.event_cluster_id,s.title "
        "FROM external_market_signals s "
        "LEFT JOIN news_event_clusters c ON c.event_cluster_id=s.event_cluster_id "
        f"WHERE {signal_where} AND (s.event_cluster_id IS NULL OR c.event_cluster_id IS NULL) "
        "ORDER BY s.id"
    )
    missing_primary_link_sql = (
        "SELECT s.id,s.external_id,s.source_news_id,s.event_cluster_id,s.title "
        "FROM external_market_signals s "
        "LEFT JOIN external_signal_source_links l "
        "  ON l.signal_id=s.id AND l.news_id=s.source_news_id AND l.link_type='primary_source' "
        f"WHERE {signal_where} AND s.source_news_id IS NOT NULL AND l.signal_id IS NULL "
        "ORDER BY s.id"
    )
    missing_cluster_article_link_sql = (
        "SELECT s.id,s.external_id,s.source_news_id,s.event_cluster_id,s.title "
        "FROM external_market_signals s "
        "LEFT JOIN news_article_cluster_links l "
        "  ON l.news_id=s.source_news_id AND l.event_cluster_id=s.event_cluster_id "
        f"WHERE {signal_where} "
        "  AND s.source_news_id IS NOT NULL "
        "  AND s.event_cluster_id IS NOT NULL "
        "  AND l.news_id IS NULL "
        "ORDER BY s.id"
    )
    missing_current_classification_sql = (
        "SELECT DISTINCT s.event_cluster_id "
        "FROM external_market_signals s "
        "LEFT JOIN v_current_event_classifications c "
        "  ON c.event_cluster_id=s.event_cluster_id "
        " AND c.news_id IS NULL "
        " AND c.classification_scope='cluster' "
        f"WHERE {signal_where} AND s.event_cluster_id IS NOT NULL AND c.classification_id IS NULL "
        "ORDER BY s.event_cluster_id"
    )
    missing_current_gate_sql = (
        "SELECT DISTINCT s.event_cluster_id "
        "FROM external_market_signals s "
        "LEFT JOIN v_current_event_gates g "
        "  ON g.event_cluster_id=s.event_cluster_id "
        " AND g.news_id IS NULL "
        " AND g.gate_scope='cluster' "
        f"WHERE {signal_where} AND s.event_cluster_id IS NOT NULL AND g.gate_id IS NULL "
        "ORDER BY s.event_cluster_id"
    )
    return {
        "legacy_total": legacy_total,
        "raw_total": raw_total,
        "signal_total": signal_total,
        "legacy_with_signal": legacy_with_signal,
        "signal_completion_pct": round((legacy_with_signal / legacy_total * 100.0), 6) if legacy_total else 0.0,
        "source_raw_no_match": {
            "count": _count_sql(conn, source_raw_no_match_sql, signal_params),
            "samples": _samples(conn, source_raw_no_match_sql, signal_params, limit=sample_limit),
        },
        "source_raw_wrong_provider": {
            "count": _count_sql(conn, source_raw_wrong_provider_sql, signal_params),
            "samples": _samples(conn, source_raw_wrong_provider_sql, signal_params, limit=sample_limit),
        },
        "missing_cluster": {
            "count": _count_sql(conn, missing_cluster_sql, signal_params),
            "samples": _samples(conn, missing_cluster_sql, signal_params, limit=sample_limit),
        },
        "missing_primary_link": {
            "count": _count_sql(conn, missing_primary_link_sql, signal_params),
            "samples": _samples(conn, missing_primary_link_sql, signal_params, limit=sample_limit),
        },
        "missing_cluster_article_link": {
            "count": _count_sql(conn, missing_cluster_article_link_sql, signal_params),
            "samples": _samples(conn, missing_cluster_article_link_sql, signal_params, limit=sample_limit),
        },
        "missing_current_classification": {
            "count": _count_sql(conn, missing_current_classification_sql, signal_params),
            "samples": _samples(conn, missing_current_classification_sql, signal_params, limit=sample_limit),
        },
        "missing_current_gate": {
            "count": _count_sql(conn, missing_current_gate_sql, signal_params),
            "samples": _samples(conn, missing_current_gate_sql, signal_params, limit=sample_limit),
        },
    }


def _duplicate_audit(conn: sqlite3.Connection, *, sample_limit: int) -> dict[str, Any]:
    signal_bz_dj = "external_id LIKE 'bz:%' OR external_id LIKE 'dj:%'"
    audits: dict[str, Any] = {}
    audits["legacy_article_id"] = _group_audit(
        conn,
        """SELECT provider,article_id,COUNT(*) AS cnt
             FROM news_articles
            WHERE provider='BZ' OR provider LIKE 'DJ%'
            GROUP BY provider,article_id
           HAVING COUNT(*)>1
            ORDER BY cnt DESC,provider,article_id""",
        sample_limit=sample_limit,
    )
    audits["raw_provider_article_id"] = _group_audit(
        conn,
        """SELECT provider,provider_article_id,COUNT(*) AS cnt
             FROM raw_news_articles
            WHERE (provider='BZ' OR provider LIKE 'DJ%')
              AND COALESCE(provider_article_id,'')<>''
            GROUP BY provider,provider_article_id
           HAVING COUNT(*)>1
            ORDER BY cnt DESC,provider,provider_article_id""",
        sample_limit=sample_limit,
    )
    audits["raw_content_hash"] = _group_audit(
        conn,
        """SELECT provider,content_hash,COUNT(*) AS cnt
             FROM raw_news_articles
            WHERE provider='BZ' OR provider LIKE 'DJ%'
            GROUP BY provider,content_hash
           HAVING COUNT(*)>1
            ORDER BY cnt DESC,provider""",
        sample_limit=sample_limit,
    )
    audits["signal_external_id"] = _group_audit(
        conn,
        f"""SELECT external_id,COUNT(*) AS cnt
              FROM external_market_signals
             WHERE {signal_bz_dj}
             GROUP BY external_id
            HAVING COUNT(*)>1
             ORDER BY cnt DESC,external_id""",
        sample_limit=sample_limit,
    )
    audits["signal_source_news_id"] = _group_audit(
        conn,
        f"""SELECT source,source_news_id,COUNT(*) AS cnt
              FROM external_market_signals
             WHERE ({signal_bz_dj})
               AND source_news_id IS NOT NULL
             GROUP BY source,source_news_id
            HAVING COUNT(*)>1
             ORDER BY cnt DESC,source,source_news_id""",
        sample_limit=sample_limit,
    )
    audits["legacy_headline_hash"] = _group_audit(
        conn,
        """SELECT provider,headline_hash,COUNT(*) AS cnt
             FROM news_articles
            WHERE provider='BZ' OR provider LIKE 'DJ%'
            GROUP BY provider,headline_hash
           HAVING COUNT(*)>1
            ORDER BY cnt DESC,provider""",
        sample_limit=sample_limit,
    )
    return audits


def _classification_gate_audit(conn: sqlite3.Connection, *, sample_limit: int) -> dict[str, Any]:
    classification_key_group = """
        SELECT
          COALESCE(event_cluster_id,-1) AS event_cluster_id,
          COALESCE(news_id,-1) AS news_id,
          COALESCE(classification_scope,'') AS classification_scope,
          COALESCE(classifier_name,'') AS classifier_name,
          COALESCE(classifier_version,'') AS classifier_version,
          COUNT(*) AS cnt
        FROM news_event_classifications
        GROUP BY
          COALESCE(event_cluster_id,-1),
          COALESCE(news_id,-1),
          COALESCE(classification_scope,''),
          COALESCE(classifier_name,''),
          COALESCE(classifier_version,'')
        HAVING COUNT(*)>1
        ORDER BY cnt DESC,event_cluster_id
    """
    classification_semantic_group = """
        SELECT
          COALESCE(event_cluster_id,-1) AS event_cluster_id,
          COALESCE(news_id,-1) AS news_id,
          COALESCE(classification_scope,'') AS classification_scope,
          COALESCE(classifier_name,'') AS classifier_name,
          COALESCE(classifier_version,'') AS classifier_version,
          COALESCE(event_class,'') AS event_class,
          COALESCE(region,'') AS region,
          COALESCE(theme,'') AS theme,
          COALESCE(direction,'') AS direction,
          COALESCE(severity,'') AS severity,
          COALESCE(sentiment_score,'') AS sentiment_score,
          COALESCE(classification_confidence,'') AS classification_confidence,
          COUNT(*) AS cnt
        FROM news_event_classifications
        GROUP BY
          COALESCE(event_cluster_id,-1),
          COALESCE(news_id,-1),
          COALESCE(classification_scope,''),
          COALESCE(classifier_name,''),
          COALESCE(classifier_version,''),
          COALESCE(event_class,''),
          COALESCE(region,''),
          COALESCE(theme,''),
          COALESCE(direction,''),
          COALESCE(severity,''),
          COALESCE(sentiment_score,''),
          COALESCE(classification_confidence,'')
        HAVING COUNT(*)>1
        ORDER BY cnt DESC,event_cluster_id
    """
    classification_conflict_group = """
        SELECT event_cluster_id,news_id,classification_scope,classifier_name,classifier_version,
               COUNT(*) AS payload_variants
          FROM (
            SELECT DISTINCT
              COALESCE(event_cluster_id,-1) AS event_cluster_id,
              COALESCE(news_id,-1) AS news_id,
              COALESCE(classification_scope,'') AS classification_scope,
              COALESCE(classifier_name,'') AS classifier_name,
              COALESCE(classifier_version,'') AS classifier_version,
              COALESCE(event_class,'') || '|' || COALESCE(region,'') || '|' ||
              COALESCE(theme,'') || '|' || COALESCE(direction,'') || '|' ||
              COALESCE(severity,'') || '|' || COALESCE(sentiment_score,'') || '|' ||
              COALESCE(classification_confidence,'') AS payload
            FROM news_event_classifications
          )
         GROUP BY event_cluster_id,news_id,classification_scope,classifier_name,classifier_version
        HAVING COUNT(*)>1
         ORDER BY payload_variants DESC,event_cluster_id
    """
    gate_key_group = """
        SELECT
          COALESCE(event_cluster_id,-1) AS event_cluster_id,
          COALESCE(news_id,-1) AS news_id,
          COALESCE(gate_scope,'') AS gate_scope,
          COALESCE(gate_version,'') AS gate_version,
          COUNT(*) AS cnt
        FROM news_event_gates
        GROUP BY
          COALESCE(event_cluster_id,-1),
          COALESCE(news_id,-1),
          COALESCE(gate_scope,''),
          COALESCE(gate_version,'')
        HAVING COUNT(*)>1
        ORDER BY cnt DESC,event_cluster_id
    """
    gate_semantic_group = """
        SELECT
          COALESCE(event_cluster_id,-1) AS event_cluster_id,
          COALESCE(news_id,-1) AS news_id,
          COALESCE(gate_scope,'') AS gate_scope,
          COALESCE(gate_version,'') AS gate_version,
          COALESCE(gate_status,'') AS gate_status,
          COALESCE(gate_score,'') AS gate_score,
          COALESCE(blocked_reason,'') AS blocked_reason,
          COUNT(*) AS cnt
        FROM news_event_gates
        GROUP BY
          COALESCE(event_cluster_id,-1),
          COALESCE(news_id,-1),
          COALESCE(gate_scope,''),
          COALESCE(gate_version,''),
          COALESCE(gate_status,''),
          COALESCE(gate_score,''),
          COALESCE(blocked_reason,'')
        HAVING COUNT(*)>1
        ORDER BY cnt DESC,event_cluster_id
    """
    gate_conflict_group = """
        SELECT event_cluster_id,news_id,gate_scope,gate_version,COUNT(*) AS payload_variants
          FROM (
            SELECT DISTINCT
              COALESCE(event_cluster_id,-1) AS event_cluster_id,
              COALESCE(news_id,-1) AS news_id,
              COALESCE(gate_scope,'') AS gate_scope,
              COALESCE(gate_version,'') AS gate_version,
              COALESCE(gate_status,'') || '|' || COALESCE(gate_score,'') || '|' ||
              COALESCE(blocked_reason,'') AS payload
            FROM news_event_gates
          )
         GROUP BY event_cluster_id,news_id,gate_scope,gate_version
        HAVING COUNT(*)>1
         ORDER BY payload_variants DESC,event_cluster_id
    """
    canonical_current_classification_group = """
        SELECT build_id,event_cluster_id,COALESCE(news_id,-1) AS news_id,classification_scope,COUNT(*) AS cnt
          FROM v_current_event_classifications
         GROUP BY build_id,event_cluster_id,COALESCE(news_id,-1),classification_scope
        HAVING COUNT(*)>1
         ORDER BY cnt DESC,event_cluster_id
    """
    canonical_current_gate_group = """
        SELECT build_id,event_cluster_id,COALESCE(news_id,-1) AS news_id,gate_scope,COUNT(*) AS cnt
          FROM v_current_event_gates
         GROUP BY build_id,event_cluster_id,COALESCE(news_id,-1),gate_scope
        HAVING COUNT(*)>1
         ORDER BY cnt DESC,event_cluster_id
    """
    return {
        "legacy_classification_key_duplicates": _group_audit(
            conn, classification_key_group, sample_limit=sample_limit
        ),
        "legacy_classification_exact_semantic_duplicates": _group_audit(
            conn, classification_semantic_group, sample_limit=sample_limit
        ),
        "legacy_classification_same_key_conflicts": {
            "groups": _count_sql(conn, classification_conflict_group),
            "samples": _samples(conn, classification_conflict_group, limit=sample_limit),
        },
        "legacy_gate_key_duplicates": _group_audit(conn, gate_key_group, sample_limit=sample_limit),
        "legacy_gate_exact_semantic_duplicates": _group_audit(
            conn, gate_semantic_group, sample_limit=sample_limit
        ),
        "legacy_gate_same_key_conflicts": {
            "groups": _count_sql(conn, gate_conflict_group),
            "samples": _samples(conn, gate_conflict_group, limit=sample_limit),
        },
        "canonical": {
            "version": NEWS_CLASSIFICATION_GATE_CANONICAL_VERSION,
            "classification_rows": _count_sql(conn, "SELECT classification_id FROM news_event_classifications_canonical"),
            "gate_rows": _count_sql(conn, "SELECT gate_id FROM news_event_gates_canonical"),
            "current_classification_rows": _count_sql(conn, "SELECT classification_id FROM v_current_event_classifications"),
            "current_gate_rows": _count_sql(conn, "SELECT gate_id FROM v_current_event_gates"),
            "classification_key_duplicates": _group_audit(
                conn,
                """SELECT classification_key,COUNT(*) AS cnt
                     FROM news_event_classifications_canonical
                    GROUP BY classification_key
                   HAVING COUNT(*)>1""",
                sample_limit=sample_limit,
            ),
            "gate_key_duplicates": _group_audit(
                conn,
                """SELECT gate_key,COUNT(*) AS cnt
                     FROM news_event_gates_canonical
                    GROUP BY gate_key
                   HAVING COUNT(*)>1""",
                sample_limit=sample_limit,
            ),
            "current_classification_duplicates": _group_audit(
                conn, canonical_current_classification_group, sample_limit=sample_limit
            ),
            "current_gate_duplicates": _group_audit(conn, canonical_current_gate_group, sample_limit=sample_limit),
            "conflict_rows": _count_sql(conn, "SELECT conflict_id FROM classification_gate_conflicts"),
        },
    }


def _asset_audit(conn: sqlite3.Connection, *, sample_limit: int) -> dict[str, Any]:
    return {
        "mapping_run_key_duplicates": _group_audit(
            conn,
            """SELECT build_id,mapping_run_key,COUNT(*) AS cnt
                 FROM news_asset_mapping_runs
                GROUP BY build_id,mapping_run_key
               HAVING COUNT(*)>1
                ORDER BY cnt DESC""",
            sample_limit=sample_limit,
        ),
        "candidate_key_duplicates": _group_audit(
            conn,
            """SELECT mapping_run_id,candidate_key,COUNT(*) AS cnt
                 FROM news_asset_candidates
                GROUP BY mapping_run_id,candidate_key
               HAVING COUNT(*)>1
                ORDER BY cnt DESC""",
            sample_limit=sample_limit,
        ),
        "candidate_missing_mapping_run_id": {
            "count": _count_sql(
                conn,
                """SELECT a.id
                     FROM news_asset_candidates a
                     LEFT JOIN news_asset_mapping_runs r
                       ON r.mapping_run_id=a.mapping_run_id
                    WHERE a.mapping_run_id IS NULL OR r.mapping_run_id IS NULL""",
            ),
            "samples": _samples(
                conn,
                """SELECT a.id,a.news_id,a.event_cluster_id,a.symbol,a.role,a.source,a.mapping_run_id
                     FROM news_asset_candidates a
                     LEFT JOIN news_asset_mapping_runs r
                       ON r.mapping_run_id=a.mapping_run_id
                    WHERE a.mapping_run_id IS NULL OR r.mapping_run_id IS NULL
                    ORDER BY a.id""",
                limit=sample_limit,
            ),
        },
        "aggregate_rows": _count_sql(conn, "SELECT event_cluster_id FROM v_cluster_asset_candidate_aggregates")
        if _relation_exists(conn, "v_cluster_asset_candidate_aggregates")
        else 0,
    }


def _stage_audit(conn: sqlite3.Connection, *, sample_limit: int) -> dict[str, Any]:
    if not _relation_exists(conn, "research_backfill_stage_runs"):
        return {"stage_table_present": False, "s03_completed": False, "s04_or_later_runs": 0, "samples": []}
    s03_completed = (
        conn.execute(
            """SELECT 1
                 FROM research_backfill_stage_runs
                WHERE stage_name='S03_m251a_news_raw_backfill'
                  AND status='completed'
                LIMIT 1"""
        ).fetchone()
        is not None
    )
    later_sql = """
        SELECT stage_run_id,run_id,stage_name,status,started_at,completed_at,error_message
          FROM research_backfill_stage_runs
         WHERE stage_name LIKE 'S04%'
            OR stage_name LIKE '%S04%'
            OR stage_name LIKE 'M253%'
            OR stage_name LIKE 'M254%'
            OR stage_name LIKE 'M255%'
         ORDER BY stage_run_id
    """
    return {
        "stage_table_present": True,
        "s03_completed": s03_completed,
        "s04_or_m253plus_runs": _count_sql(conn, later_sql),
        "samples": _samples(conn, later_sql, limit=sample_limit),
    }


def _audit(conn: sqlite3.Connection, *, sample_limit: int) -> dict[str, Any]:
    return {
        "providers": {
            provider: _provider_lineage_audit(conn, provider, sample_limit=sample_limit)
            for provider in ("BZ", "DJ")
        },
        "duplicates": _duplicate_audit(conn, sample_limit=sample_limit),
        "classification_gate": _classification_gate_audit(conn, sample_limit=sample_limit),
        "asset_candidates": _asset_audit(conn, sample_limit=sample_limit),
        "stages": _stage_audit(conn, sample_limit=sample_limit),
    }


def _repair_bz_orphans(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for external_id in S03_BZ_ORPHAN_EXTERNAL_IDS:
        signal = conn.execute(
            "SELECT * FROM external_market_signals WHERE source='benzinga' AND external_id=?",
            (external_id,),
        ).fetchone()
        if signal is None:
            results.append({"external_id": external_id, "status": "missing_signal"})
            continue
        news_id = signal["source_news_id"]
        raw = None
        if news_id is not None:
            raw = conn.execute("SELECT * FROM raw_news_articles WHERE id=?", (int(news_id),)).fetchone()
        if raw is None:
            results.append(
                {
                    "external_id": external_id,
                    "signal_id": int(signal["id"]),
                    "status": "missing_source_raw",
                    "source_news_id": news_id,
                }
            )
            continue
        if str(raw["provider"]) != "BZ":
            results.append(
                {
                    "external_id": external_id,
                    "signal_id": int(signal["id"]),
                    "status": "wrong_raw_provider",
                    "source_news_id": int(news_id),
                    "raw_provider": raw["provider"],
                }
            )
            continue

        cluster_id = assign_article_to_event_cluster(conn, int(news_id), recompute_roles=False)
        link_external_signal_to_source_news(conn, signal_id=int(signal["id"]), news_id=int(news_id))
        conn.execute(
            """UPDATE external_market_signals
                  SET source_news_id=?,
                      event_cluster_id=?,
                      updated_at=?
                WHERE id=?""",
            (int(news_id), int(cluster_id), datetime.now(UTC).isoformat(), int(signal["id"])),
        )
        role_payload = recompute_event_cluster_roles(conn, int(cluster_id))
        results.append(
            {
                "external_id": external_id,
                "signal_id": int(signal["id"]),
                "source_news_id": int(news_id),
                "event_cluster_id": int(cluster_id),
                "status": "repaired",
                "canonical_classification_key": role_payload.get("canonical_classification_key", ""),
                "canonical_gate_key": role_payload.get("canonical_gate_key", ""),
            }
        )
    return results


def _hard_gate_pass(audit: dict[str, Any]) -> bool:
    providers = audit["providers"]
    for provider in ("BZ", "DJ"):
        provider_audit = providers[provider]
        for key in (
            "source_raw_no_match",
            "source_raw_wrong_provider",
            "missing_cluster",
            "missing_primary_link",
            "missing_cluster_article_link",
            "missing_current_classification",
            "missing_current_gate",
        ):
            if int(provider_audit[key]["count"]) != 0:
                return False

    dup = audit["duplicates"]
    for key in (
        "legacy_article_id",
        "raw_provider_article_id",
        "raw_content_hash",
        "signal_external_id",
    ):
        if int(dup[key]["groups"]) != 0:
            return False

    cg = audit["classification_gate"]["canonical"]
    for key in (
        "classification_key_duplicates",
        "gate_key_duplicates",
        "current_classification_duplicates",
        "current_gate_duplicates",
    ):
        if int(cg[key]["groups"]) != 0:
            return False
    if int(cg["conflict_rows"]) != 0:
        return False

    asset = audit["asset_candidates"]
    if int(asset["mapping_run_key_duplicates"]["groups"]) != 0:
        return False
    if int(asset["candidate_key_duplicates"]["groups"]) != 0:
        return False
    if int(asset["candidate_missing_mapping_run_id"]["count"]) != 0:
        return False

    stages = audit["stages"]
    if stages.get("stage_table_present") and int(stages.get("s04_or_m253plus_runs", 0)) != 0:
        return False
    return True


def repair_s03_lineage_canonicalization(
    database_path: Path,
    *,
    backup_dir: Path,
    sample_limit: int = 20,
    audit_only: bool = False,
    limit_canonical_clusters: int | None = None,
) -> dict[str, Any]:
    if not database_path.exists():
        raise FileNotFoundError(database_path)

    backup_path: Path | None = None
    if not audit_only:
        backup_path = _backup_database(database_path, backup_dir)

    with _connect(database_path) as conn:
        if not audit_only:
            ensure_news_lineage_v2_schema(conn)
            conn.commit()
        before = _audit(conn, sample_limit=sample_limit)
        repair_results: dict[str, Any] = {}
        if not audit_only:
            repair_results["bz_orphan_repairs"] = _repair_bz_orphans(conn)
            repair_results["canonical_rebuild"] = rebuild_canonical_classification_gate_tables(
                conn,
                limit=limit_canonical_clusters,
            )
            conn.commit()
        after = _audit(conn, sample_limit=sample_limit)

    return {
        "database_path": str(database_path),
        "backup_path": str(backup_path) if backup_path else "",
        "audit_only": audit_only,
        "canonical_version": NEWS_CLASSIFICATION_GATE_CANONICAL_VERSION,
        "before": before,
        "repair_results": repair_results,
        "after": after,
        "hard_gate_pass": _hard_gate_pass(after),
        "legacy_duplicate_note": (
            "news_event_classifications/news_event_gates are append-only history tables; "
            "downstream readers should use v_current_event_classifications and v_current_event_gates."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Repair/audit S03 BZ/DJ lineage and canonical classification/gates.")
    parser.add_argument(
        "--database-path",
        type=Path,
        default=Path(os.environ.get("DATABASE_PATH", str(DEFAULT_DATABASE_PATH))),
    )
    parser.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR)
    parser.add_argument("--sample-limit", type=int, default=20)
    parser.add_argument("--audit-only", action="store_true")
    parser.add_argument("--limit-canonical-clusters", type=int)
    parser.add_argument("--json-output", type=Path)
    args = parser.parse_args()

    payload = repair_s03_lineage_canonicalization(
        args.database_path,
        backup_dir=args.backup_dir,
        sample_limit=args.sample_limit,
        audit_only=bool(args.audit_only),
        limit_canonical_clusters=args.limit_canonical_clusters,
    )
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if payload.get("hard_gate_pass") else 1


if __name__ == "__main__":
    raise SystemExit(main())
