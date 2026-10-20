from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any, Iterable

from .news_asset_mapper import NewsAssetMapping, infer_news_assets

NEWS_LINEAGE_V2_SCHEMA_VERSION = "m251-news-lineage-v2-foundation"
NEWS_ASSET_MAPPER_VERSION = "news-asset-mapper-v2-route-lineage"
NEWS_RESOLVER_VERSION = "rule-based-resolver-v1"
SECURITY_MASTER_VERSION = "security-master-unavailable-v0"
NEWS_EVENT_CLUSTERER_VERSION = "m252-news-event-cluster-v1"
NEWS_CLUSTER_ROLE_VERSION = "m252-cross-provider-confirmation-v1"
NEWS_CLUSTER_DEDUP_VERSION = "m252-derived-signal-double-count-v1"
NEWS_LINEAGE_V2_REBUILD_VERSION = "m253-news-lineage-v2-rebuild-v1"
MACRO_EVENT_LINEAGE_VERSION = "m251b-macro-event-lineage-v1"
REACTION_RESEARCH_ENGINE_VERSION = "m253-reaction-label-foundation-v1"
NEWS_ASSET_MAPPING_IDEMPOTENCY_MIGRATION = "m251_asset_mapping_idempotency_v1"
NEWS_ASSET_MAPPING_KEY_MIGRATION = "m251_asset_mapping_keys_v2"
NEWS_CLASSIFICATION_GATE_CANONICAL_VERSION = "s03-classification-gate-canonical-v1"
NEWS_PROCESSING_STATUS_STAGE = "S03_m251a_news_raw_backfill"
NEWS_VISIBILITY_POLICY_VERSION = "s03-marketaux-visibility-policy-v1"
MARKETAUX_HISTORICAL_VISIBILITY_LATENCY_SECONDS = 300
DEFAULT_NEWS_LINEAGE_BUILD_ID = "live"

_STOP_WORDS = {
    "the",
    "and",
    "for",
    "with",
    "from",
    "that",
    "this",
    "after",
    "into",
    "over",
    "under",
    "market",
    "stock",
    "stocks",
    "shares",
    "news",
    "says",
    "said",
    "company",
    "announces",
}
_POSITIVE_RE = re.compile(
    r"\b(raises?|raised|beats?|surges?|jumps?|rises?|gains?|upgrade|buy|outperform|"
    r"approval|record|strong|demand|positive|optimistic|higher)\b",
    re.I,
)
_NEGATIVE_RE = re.compile(
    r"\b(cuts?|misses?|falls?|drops?|sinks?|downgrade|sell|underperform|probe|lawsuit|"
    r"warning|risk|war|sanction|pressure|weak|lower|lowers|concern|negative)\b",
    re.I,
)
_MARKETAUX_NOISE_RE = re.compile(
    r"\b(sensex|nifty|bse|tadawul|arabian|lse|ftse|hk bourse|form 8\.5|plc|tsx|asx|jse|bovespa|"
    r"top\s+\d+\s+stocks?|stocks?\s+to\s+buy|hedge fund portfolio|analyst stock picks)\b",
    re.I,
)


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        is not None
    )


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    if not _table_exists(conn, table):
        return False
    return column in {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _add_column_if_missing(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    definition: str,
) -> None:
    if _table_exists(conn, table) and not _column_exists(conn, table, column):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _lineage_migration_applied(conn: sqlite3.Connection, migration_key: str) -> bool:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS news_lineage_schema_migrations (
            migration_key TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL,
            details TEXT NOT NULL DEFAULT ''
        )"""
    )
    return (
        conn.execute(
            "SELECT 1 FROM news_lineage_schema_migrations WHERE migration_key=?",
            (migration_key,),
        ).fetchone()
        is not None
    )


def _mark_lineage_migration_applied(
    conn: sqlite3.Connection,
    migration_key: str,
    *,
    details: str = "",
) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO news_lineage_schema_migrations(
            migration_key,applied_at,details
        ) VALUES (?, ?, ?)""",
        (migration_key, utc_now_iso(), details),
    )


def mapping_run_key_for_values(
    *,
    build_id: str,
    news_id: int,
    event_cluster_id: int | None,
    mapping_scope: str,
    mapper_version: str,
    resolver_version: str,
    security_master_version: str,
    input_hash: str,
    config_hash: str,
) -> str:
    return _stable_hash(
        {
            "build_id": str(build_id or DEFAULT_NEWS_LINEAGE_BUILD_ID),
            "news_id": int(news_id),
            "event_cluster_id": "NONE" if event_cluster_id is None else int(event_cluster_id),
            "mapping_scope": str(mapping_scope or "article"),
            "mapper_version": str(mapper_version or ""),
            "resolver_version": str(resolver_version or ""),
            "security_master_version": str(security_master_version or ""),
            "input_hash": str(input_hash or ""),
            "config_hash": str(config_hash or ""),
        }
    )


def candidate_key_for_values(
    *,
    mapping_run_key: str,
    symbol: str,
    con_id: int | None,
    asset_type: str | None,
    role: str,
    source: str,
    resolver_status: str,
    label_scope: str | None = None,
) -> str:
    normalized_symbol = str(symbol or "").strip().upper()
    try:
        normalized_con_id: int | str = "" if con_id in (None, "") else int(con_id)
    except (TypeError, ValueError):
        normalized_con_id = str(con_id or "")
    return _stable_hash(
        {
            "mapping_run_key": str(mapping_run_key or ""),
            "symbol": normalized_symbol,
            "con_id": normalized_con_id,
            "asset_type": str(asset_type or ""),
            "role": str(role or ""),
            "source": str(source or ""),
            "resolver_status": str(resolver_status or ""),
            "label_scope": str(label_scope or "NONE"),
        }
    )


def _backfill_asset_mapping_keys(conn: sqlite3.Connection) -> None:
    if not (_table_exists(conn, "news_asset_mapping_runs") and _table_exists(conn, "news_asset_candidates")):
        return
    conn.execute(
        """UPDATE news_asset_mapping_runs
              SET last_seen_at=COALESCE(NULLIF(last_seen_at,''), created_at, ?)
            WHERE last_seen_at IS NULL OR last_seen_at=''""",
        (utc_now_iso(),),
    )
    conn.execute(
        """UPDATE news_asset_candidates
              SET last_seen_at=COALESCE(NULLIF(last_seen_at,''), created_at, ?)
            WHERE last_seen_at IS NULL OR last_seen_at=''""",
        (utc_now_iso(),),
    )
    for row in conn.execute(
        """SELECT mapping_run_id,build_id,news_id,event_cluster_id,mapping_scope,mapper_version,
                  resolver_version,security_master_version,input_hash,config_hash
             FROM news_asset_mapping_runs
            WHERE mapping_run_key IS NULL OR mapping_run_key=''"""
    ).fetchall():
        (
            mapping_run_id,
            build_id,
            news_id,
            event_cluster_id,
            mapping_scope,
            mapper_version,
            resolver_version,
            security_master_version,
            input_hash,
            config_hash,
        ) = row
        conn.execute(
            "UPDATE news_asset_mapping_runs SET mapping_run_key=? WHERE mapping_run_id=?",
            (
                mapping_run_key_for_values(
                    build_id=str(build_id or DEFAULT_NEWS_LINEAGE_BUILD_ID),
                    news_id=int(news_id),
                    event_cluster_id=(
                        None
                        if str(mapping_scope or "article") == "article"
                        else (int(event_cluster_id) if event_cluster_id is not None else None)
                    ),
                    mapping_scope=str(mapping_scope or "article"),
                    mapper_version=str(mapper_version or ""),
                    resolver_version=str(resolver_version or ""),
                    security_master_version=str(security_master_version or ""),
                    input_hash=str(input_hash or ""),
                    config_hash=str(config_hash or ""),
                ),
                int(mapping_run_id),
            ),
        )
    for row in conn.execute(
        """SELECT a.id,r.mapping_run_key,a.symbol,a.con_id,a.asset_type,a.role,
                  a.source,a.resolver_status,a.tradable_candidate
             FROM news_asset_candidates a
             JOIN news_asset_mapping_runs r
               ON a.mapping_run_id=r.mapping_run_id
            WHERE a.candidate_key IS NULL OR a.candidate_key=''"""
    ).fetchall():
        (
            candidate_id,
            mapping_run_key,
            symbol,
            con_id,
            asset_type,
            role,
            source,
            resolver_status,
            tradable_candidate,
        ) = row
        conn.execute(
            "UPDATE news_asset_candidates SET candidate_key=? WHERE id=?",
            (
                candidate_key_for_values(
                    mapping_run_key=str(mapping_run_key or ""),
                    symbol=str(symbol or ""),
                    con_id=con_id,
                    asset_type=asset_type,
                    role=str(role or ""),
                    source=str(source or ""),
                    resolver_status=str(resolver_status or ""),
                ),
                int(candidate_id),
            ),
        )


def _dedupe_asset_mapping_lineage(conn: sqlite3.Connection) -> None:
    """Collapse duplicate article mapping runs/candidates before unique indexes.

    M251 stores article-level mapping as a derived artifact.  A resumed source
    shard must therefore be idempotent: the raw article, derived signal, mapping
    run, and asset candidates should be reused instead of appended again.  Older
    builds only looked for mapping runs whose event_cluster_id was still NULL;
    once M252 linked the article to a cluster, a resume could append another run.
    """

    if not (_table_exists(conn, "news_asset_mapping_runs") and _table_exists(conn, "news_asset_candidates")):
        return
    _backfill_asset_mapping_keys(conn)

    conn.executescript(
        """
        DROP TABLE IF EXISTS temp._m251_mapping_run_canonical;
        DROP TABLE IF EXISTS temp._m251_mapping_run_map;

        CREATE TEMP TABLE _m251_mapping_run_canonical AS
        SELECT
            MIN(mapping_run_id) AS canonical_mapping_run_id,
            build_id,
            mapping_run_key,
            MAX(event_cluster_id) AS canonical_event_cluster_id
          FROM news_asset_mapping_runs
         GROUP BY build_id,mapping_run_key;

        CREATE TEMP TABLE _m251_mapping_run_map AS
        SELECT
            r.mapping_run_id,
            c.canonical_mapping_run_id,
            c.canonical_event_cluster_id
          FROM news_asset_mapping_runs r
          JOIN _m251_mapping_run_canonical c
            ON c.build_id=r.build_id
           AND c.mapping_run_key=r.mapping_run_key;

        UPDATE news_asset_mapping_runs
           SET event_cluster_id=COALESCE(
               event_cluster_id,
               (SELECT canonical_event_cluster_id
                  FROM _m251_mapping_run_map m
                 WHERE m.mapping_run_id=news_asset_mapping_runs.mapping_run_id)
           )
         WHERE mapping_run_id IN (
               SELECT canonical_mapping_run_id
                 FROM _m251_mapping_run_canonical
                WHERE canonical_event_cluster_id IS NOT NULL
         );

        UPDATE news_asset_candidates
           SET mapping_run_id=(
               SELECT canonical_mapping_run_id
                 FROM _m251_mapping_run_map m
                WHERE m.mapping_run_id=news_asset_candidates.mapping_run_id
           )
         WHERE mapping_run_id IN (
               SELECT mapping_run_id
                 FROM _m251_mapping_run_map
                WHERE mapping_run_id<>canonical_mapping_run_id
         );

        UPDATE news_asset_candidates
           SET event_cluster_id=COALESCE(
               event_cluster_id,
               (SELECT canonical_event_cluster_id
                  FROM _m251_mapping_run_map m
                 WHERE m.mapping_run_id=news_asset_candidates.mapping_run_id)
           )
         WHERE event_cluster_id IS NULL
           AND mapping_run_id IN (
               SELECT canonical_mapping_run_id
                 FROM _m251_mapping_run_canonical
                WHERE canonical_event_cluster_id IS NOT NULL
           );

        DROP TABLE IF EXISTS temp._m251_candidate_keep;
        CREATE TEMP TABLE _m251_candidate_keep AS
        SELECT id
          FROM (
                SELECT id,
                       ROW_NUMBER() OVER (
                           PARTITION BY mapping_run_id,candidate_key
                           ORDER BY confidence DESC,id ASC
                       ) AS rn
                  FROM news_asset_candidates
               )
         WHERE rn=1;

        DELETE FROM news_asset_candidates
         WHERE id NOT IN (SELECT id FROM _m251_candidate_keep);
        DROP TABLE IF EXISTS temp._m251_candidate_keep;

        DELETE FROM news_asset_mapping_runs
         WHERE mapping_run_id NOT IN (
               SELECT canonical_mapping_run_id FROM _m251_mapping_run_canonical
         );

        DROP TABLE IF EXISTS temp._m251_mapping_run_map;
        DROP TABLE IF EXISTS temp._m251_mapping_run_canonical;
        """
    )


def _apply_asset_mapping_idempotency_migration(conn: sqlite3.Connection) -> None:
    if _lineage_migration_applied(conn, NEWS_ASSET_MAPPING_IDEMPOTENCY_MIGRATION):
        return
    _dedupe_asset_mapping_lineage(conn)
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS uq_news_asset_mapping_runs_article
              ON news_asset_mapping_runs(
                  news_id,mapping_scope,mapper_version,resolver_version,
                  security_master_version,input_hash,config_hash
              )"""
    )
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS uq_news_asset_candidates_mapping_key
              ON news_asset_candidates(
                  mapping_run_id,news_id,COALESCE(event_cluster_id,-1),
                  symbol,role,source,resolver_status,tradable_candidate
              )"""
    )
    _mark_lineage_migration_applied(
        conn,
        NEWS_ASSET_MAPPING_IDEMPOTENCY_MIGRATION,
        details="deduped mapping runs/candidates and added resume-safe unique indexes",
    )


def _apply_asset_mapping_key_migration(conn: sqlite3.Connection) -> None:
    if _lineage_migration_applied(conn, NEWS_ASSET_MAPPING_KEY_MIGRATION):
        return
    _dedupe_asset_mapping_lineage(conn)
    conn.execute("DROP INDEX IF EXISTS uq_news_asset_mapping_runs_article")
    conn.execute("DROP INDEX IF EXISTS uq_news_asset_candidates_mapping_key")
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS uq_news_asset_mapping_runs_key
              ON news_asset_mapping_runs(build_id,mapping_run_key)"""
    )
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS uq_news_asset_candidates_key
              ON news_asset_candidates(mapping_run_id,candidate_key)"""
    )
    _mark_lineage_migration_applied(
        conn,
        NEWS_ASSET_MAPPING_KEY_MIGRATION,
        details="backfilled mapping_run_key/candidate_key and added key-level unique indexes",
    )


def ensure_news_lineage_v2_schema(conn: sqlite3.Connection) -> None:
    """Create the M251 additive news-lineage foundation.

    This schema is deliberately side-by-side with the legacy `news_articles` and
    `external_market_signals` tables.  M251 stabilizes raw -> mapping lineage
    without cutting ranking/outcome consumers over; M252/M253 can later migrate
    those consumers once the lineage tables have been validated.
    """

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS raw_news_articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            legacy_news_id INTEGER UNIQUE,
            source_system TEXT NOT NULL,
            provider TEXT NOT NULL,
            publisher TEXT NOT NULL DEFAULT '',
            provider_article_id TEXT NOT NULL DEFAULT '',
            headline TEXT NOT NULL,
            body TEXT,
            url TEXT NOT NULL DEFAULT '',
            published_at_utc TEXT NOT NULL DEFAULT '',
            updated_at_utc TEXT,
            received_at_utc TEXT NOT NULL DEFAULT '',
            available_at_utc TEXT NOT NULL DEFAULT '',
            availability_quality TEXT NOT NULL DEFAULT 'unknown',
            ingest_mode TEXT NOT NULL,
            route_type TEXT NOT NULL,
            primary_route_symbol TEXT NOT NULL DEFAULT '',
            provider_primary_symbol TEXT NOT NULL DEFAULT '',
            raw_json TEXT NOT NULL DEFAULT '{}',
            content_hash TEXT NOT NULL,
            lineage_schema_version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(source_system,provider,provider_article_id,content_hash)
        );
        CREATE INDEX IF NOT EXISTS idx_raw_news_legacy
            ON raw_news_articles(legacy_news_id);
        CREATE INDEX IF NOT EXISTS idx_raw_news_provider_time
            ON raw_news_articles(source_system,provider,published_at_utc);
        CREATE INDEX IF NOT EXISTS idx_raw_news_route
            ON raw_news_articles(route_type,primary_route_symbol,published_at_utc);

        CREATE TABLE IF NOT EXISTS provider_entities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            news_id INTEGER NOT NULL,
            provider TEXT NOT NULL,
            raw_symbol TEXT NOT NULL DEFAULT '',
            raw_name TEXT NOT NULL DEFAULT '',
            raw_exchange TEXT NOT NULL DEFAULT '',
            raw_country TEXT NOT NULL DEFAULT '',
            raw_industry TEXT NOT NULL DEFAULT '',
            entity_type TEXT NOT NULL DEFAULT '',
            match_score REAL,
            sentiment_score REAL,
            raw_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            FOREIGN KEY(news_id) REFERENCES raw_news_articles(id),
            UNIQUE(news_id,provider,raw_symbol,raw_name,entity_type)
        );
        CREATE INDEX IF NOT EXISTS idx_provider_entities_news
            ON provider_entities(news_id,provider);
        CREATE INDEX IF NOT EXISTS idx_provider_entities_symbol
            ON provider_entities(raw_symbol,raw_exchange,raw_country);

        CREATE TABLE IF NOT EXISTS news_asset_mapping_runs (
            mapping_run_id INTEGER PRIMARY KEY AUTOINCREMENT,
            build_id TEXT NOT NULL DEFAULT 'live',
            mapping_run_key TEXT NOT NULL DEFAULT '',
            news_id INTEGER NOT NULL,
            event_cluster_id INTEGER,
            mapping_scope TEXT NOT NULL DEFAULT 'article',
            mapper_version TEXT NOT NULL,
            resolver_version TEXT NOT NULL,
            security_master_version TEXT NOT NULL,
            input_hash TEXT NOT NULL,
            config_hash TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL DEFAULT '',
            FOREIGN KEY(news_id) REFERENCES raw_news_articles(id)
        );
        CREATE INDEX IF NOT EXISTS idx_news_asset_mapping_runs_news
            ON news_asset_mapping_runs(news_id,mapping_scope,mapper_version,input_hash);

        CREATE TABLE IF NOT EXISTS news_asset_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mapping_run_id INTEGER NOT NULL,
            candidate_key TEXT NOT NULL DEFAULT '',
            news_id INTEGER NOT NULL,
            event_cluster_id INTEGER,
            symbol TEXT NOT NULL,
            con_id INTEGER,
            asset_type TEXT,
            role TEXT NOT NULL,
            source TEXT NOT NULL,
            confidence REAL NOT NULL DEFAULT 0,
            evidence TEXT NOT NULL DEFAULT '',
            resolver_status TEXT NOT NULL DEFAULT 'unresolved',
            tradable_candidate INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL DEFAULT '',
            FOREIGN KEY(mapping_run_id) REFERENCES news_asset_mapping_runs(mapping_run_id),
            FOREIGN KEY(news_id) REFERENCES raw_news_articles(id)
        );
        CREATE INDEX IF NOT EXISTS idx_news_asset_candidates_news
            ON news_asset_candidates(news_id,role,source);
        CREATE INDEX IF NOT EXISTS idx_news_asset_candidates_symbol
            ON news_asset_candidates(symbol,resolver_status,tradable_candidate);

        CREATE VIEW IF NOT EXISTS v_cluster_asset_candidates AS
            SELECT
                l.event_cluster_id,
                r.news_id,
                a.symbol,
                a.con_id,
                a.asset_type,
                a.role,
                a.source,
                a.resolver_status,
                a.tradable_candidate,
                MAX(a.confidence) AS confidence,
                COUNT(*) AS raw_candidate_rows
              FROM news_asset_candidates a
              JOIN news_asset_mapping_runs r
                ON a.mapping_run_id = r.mapping_run_id
              JOIN news_article_cluster_links l
                ON r.news_id = l.news_id
             GROUP BY
                l.event_cluster_id,
                r.news_id,
                a.symbol,
                a.con_id,
                a.asset_type,
                a.role,
                a.source,
                a.resolver_status,
                a.tradable_candidate;

        CREATE VIEW IF NOT EXISTS v_cluster_asset_candidate_aggregates AS
            SELECT
                event_cluster_id,
                symbol,
                con_id,
                asset_type,
                role,
                source,
                resolver_status,
                tradable_candidate,
                MAX(confidence) AS confidence,
                COUNT(DISTINCT news_id) AS supporting_news_count,
                SUM(raw_candidate_rows) AS raw_candidate_rows
              FROM v_cluster_asset_candidates
             GROUP BY
                event_cluster_id,
                symbol,
                con_id,
                asset_type,
                role,
                source,
                resolver_status,
                tradable_candidate;

        CREATE TABLE IF NOT EXISTS news_enrichment_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            news_id INTEGER NOT NULL,
            job_type TEXT NOT NULL DEFAULT 'enrich_news_article',
            dedup_key TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'queued',
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(news_id) REFERENCES raw_news_articles(id)
        );
        CREATE INDEX IF NOT EXISTS idx_news_enrichment_jobs_ready
            ON news_enrichment_jobs(status,job_type,id);

        CREATE TABLE IF NOT EXISTS external_signal_source_links (
            signal_id INTEGER NOT NULL,
            news_id INTEGER NOT NULL,
            link_type TEXT NOT NULL DEFAULT 'primary_source',
            confidence REAL NOT NULL DEFAULT 1.0,
            created_at TEXT NOT NULL,
            PRIMARY KEY(signal_id,news_id,link_type),
            FOREIGN KEY(news_id) REFERENCES raw_news_articles(id)
        );
        CREATE INDEX IF NOT EXISTS idx_external_signal_source_links_news
            ON external_signal_source_links(news_id,signal_id);

        CREATE TABLE IF NOT EXISTS raw_news_processing_status (
            status_id INTEGER PRIMARY KEY AUTOINCREMENT,
            news_id INTEGER NOT NULL,
            source_system TEXT NOT NULL,
            provider TEXT NOT NULL,
            processing_stage TEXT NOT NULL,
            processing_status TEXT NOT NULL,
            skip_reason TEXT,
            skip_flags_json TEXT,
            derived_signal_id INTEGER,
            event_cluster_id INTEGER,
            classifier_version TEXT,
            mapper_version TEXT,
            config_hash TEXT,
            build_id TEXT,
            first_processed_at_utc TEXT NOT NULL,
            last_processed_at_utc TEXT NOT NULL,
            FOREIGN KEY(news_id) REFERENCES raw_news_articles(id),
            UNIQUE(news_id,processing_stage,classifier_version,mapper_version,config_hash,build_id)
        );
        CREATE INDEX IF NOT EXISTS idx_raw_news_processing_status_news
            ON raw_news_processing_status(news_id,processing_stage,processing_status);
        CREATE INDEX IF NOT EXISTS idx_raw_news_processing_status_provider
            ON raw_news_processing_status(source_system,provider,processing_stage,processing_status);

        CREATE TABLE IF NOT EXISTS news_visibility_policy (
            visibility_id INTEGER PRIMARY KEY AUTOINCREMENT,
            news_id INTEGER NOT NULL,
            source_system TEXT NOT NULL,
            provider TEXT NOT NULL,
            ingest_mode TEXT NOT NULL,
            published_at_utc TEXT NOT NULL,
            provider_updated_at_utc TEXT,
            received_at_utc TEXT,
            available_at_utc TEXT,
            model_visible_at_utc TEXT NOT NULL,
            visibility_policy TEXT NOT NULL,
            latency_seconds INTEGER NOT NULL,
            availability_quality TEXT NOT NULL,
            build_id TEXT,
            config_hash TEXT,
            created_at_utc TEXT NOT NULL,
            FOREIGN KEY(news_id) REFERENCES raw_news_articles(id),
            UNIQUE(news_id,build_id,config_hash)
        );
        CREATE INDEX IF NOT EXISTS idx_news_visibility_policy_news
            ON news_visibility_policy(news_id,model_visible_at_utc);
        CREATE INDEX IF NOT EXISTS idx_news_visibility_policy_provider
            ON news_visibility_policy(source_system,provider,model_visible_at_utc);

        CREATE TABLE IF NOT EXISTS news_article_dedup_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            news_id INTEGER NOT NULL,
            duplicate_of_news_id INTEGER,
            dedup_scope TEXT NOT NULL DEFAULT 'article',
            dedup_reason TEXT NOT NULL DEFAULT '',
            confidence REAL NOT NULL DEFAULT 0,
            dedup_version TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY(news_id) REFERENCES raw_news_articles(id),
            FOREIGN KEY(duplicate_of_news_id) REFERENCES raw_news_articles(id)
        );

        CREATE TABLE IF NOT EXISTS news_event_clusters (
            event_cluster_id INTEGER PRIMARY KEY AUTOINCREMENT,
            cluster_key TEXT NOT NULL UNIQUE,
            primary_headline TEXT NOT NULL DEFAULT '',
            first_seen_at_utc TEXT NOT NULL DEFAULT '',
            last_seen_at_utc TEXT NOT NULL DEFAULT '',
            canonical_entities_json TEXT NOT NULL DEFAULT '[]',
            event_class TEXT NOT NULL DEFAULT 'unknown',
            provider_count INTEGER NOT NULL DEFAULT 0,
            article_count INTEGER NOT NULL DEFAULT 0,
            cluster_status TEXT NOT NULL DEFAULT 'candidate',
            clusterer_version TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS news_article_cluster_links (
            news_id INTEGER NOT NULL,
            event_cluster_id INTEGER NOT NULL,
            link_confidence REAL NOT NULL DEFAULT 0,
            link_reason TEXT NOT NULL DEFAULT '',
            link_type TEXT NOT NULL DEFAULT 'supporting_source',
            is_primary_source INTEGER NOT NULL DEFAULT 0,
            link_role TEXT NOT NULL DEFAULT 'supporting_source',
            clusterer_version TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            PRIMARY KEY(news_id,event_cluster_id),
            FOREIGN KEY(news_id) REFERENCES raw_news_articles(id),
            FOREIGN KEY(event_cluster_id) REFERENCES news_event_clusters(event_cluster_id)
        );

        CREATE TABLE IF NOT EXISTS news_event_classifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_cluster_id INTEGER,
            news_id INTEGER,
            classification_scope TEXT NOT NULL DEFAULT 'article',
            event_class TEXT NOT NULL DEFAULT 'unknown',
            region TEXT NOT NULL DEFAULT 'unknown',
            theme TEXT NOT NULL DEFAULT 'unknown',
            direction TEXT NOT NULL DEFAULT 'unclear',
            severity REAL NOT NULL DEFAULT 0,
            sentiment_score REAL,
            classification_confidence REAL NOT NULL DEFAULT 0,
            classifier_name TEXT NOT NULL DEFAULT '',
            classifier_version TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY(event_cluster_id) REFERENCES news_event_clusters(event_cluster_id),
            FOREIGN KEY(news_id) REFERENCES raw_news_articles(id)
        );
        CREATE INDEX IF NOT EXISTS idx_news_event_classifications_cluster_scope
            ON news_event_classifications(event_cluster_id,classification_scope,news_id);

        CREATE TABLE IF NOT EXISTS news_event_gates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_cluster_id INTEGER,
            news_id INTEGER,
            gate_scope TEXT NOT NULL DEFAULT 'article',
            gate_status TEXT NOT NULL DEFAULT 'review',
            gate_score REAL NOT NULL DEFAULT 0,
            blocked_reason TEXT NOT NULL DEFAULT '',
            gate_version TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY(event_cluster_id) REFERENCES news_event_clusters(event_cluster_id),
            FOREIGN KEY(news_id) REFERENCES raw_news_articles(id)
        );
        CREATE INDEX IF NOT EXISTS idx_news_event_gates_cluster_scope
            ON news_event_gates(event_cluster_id,gate_scope,news_id);

        CREATE TABLE IF NOT EXISTS news_event_classifications_canonical (
            classification_id INTEGER PRIMARY KEY AUTOINCREMENT,
            classification_key TEXT NOT NULL UNIQUE,
            build_id TEXT NOT NULL,
            event_cluster_id INTEGER NOT NULL,
            news_id INTEGER,
            classification_scope TEXT NOT NULL DEFAULT 'cluster',
            classifier_name TEXT NOT NULL,
            classifier_version TEXT NOT NULL,
            input_hash TEXT NOT NULL,
            config_hash TEXT NOT NULL,
            event_class TEXT NOT NULL DEFAULT 'unknown',
            region TEXT NOT NULL DEFAULT 'unknown',
            theme TEXT NOT NULL DEFAULT 'unknown',
            direction TEXT NOT NULL DEFAULT 'neutral',
            severity REAL NOT NULL DEFAULT 0,
            sentiment_score REAL,
            classification_confidence REAL NOT NULL DEFAULT 0,
            matched_rules_json TEXT NOT NULL DEFAULT '{}',
            payload_hash TEXT NOT NULL,
            source_classification_ids_json TEXT NOT NULL DEFAULT '[]',
            conflict_source_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            FOREIGN KEY(event_cluster_id) REFERENCES news_event_clusters(event_cluster_id),
            FOREIGN KEY(news_id) REFERENCES raw_news_articles(id)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS uq_event_classification_key
            ON news_event_classifications_canonical(classification_key);
        CREATE INDEX IF NOT EXISTS idx_event_classifications_canonical_cluster
            ON news_event_classifications_canonical(event_cluster_id,classification_scope);

        CREATE TABLE IF NOT EXISTS news_event_gates_canonical (
            gate_id INTEGER PRIMARY KEY AUTOINCREMENT,
            gate_key TEXT NOT NULL UNIQUE,
            build_id TEXT NOT NULL,
            event_cluster_id INTEGER NOT NULL,
            news_id INTEGER,
            gate_scope TEXT NOT NULL DEFAULT 'cluster',
            gate_version TEXT NOT NULL,
            classification_key TEXT NOT NULL,
            input_hash TEXT NOT NULL,
            config_hash TEXT NOT NULL,
            gate_status TEXT NOT NULL DEFAULT 'review',
            gate_score REAL NOT NULL DEFAULT 0,
            blocked_reason TEXT NOT NULL DEFAULT '',
            payload_hash TEXT NOT NULL,
            source_gate_ids_json TEXT NOT NULL DEFAULT '[]',
            conflict_source_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            FOREIGN KEY(event_cluster_id) REFERENCES news_event_clusters(event_cluster_id),
            FOREIGN KEY(news_id) REFERENCES raw_news_articles(id)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS uq_event_gate_key
            ON news_event_gates_canonical(gate_key);
        CREATE INDEX IF NOT EXISTS idx_event_gates_canonical_cluster
            ON news_event_gates_canonical(event_cluster_id,gate_scope);

        CREATE TABLE IF NOT EXISTS classification_gate_conflicts (
            conflict_id INTEGER PRIMARY KEY AUTOINCREMENT,
            table_name TEXT NOT NULL,
            key TEXT NOT NULL,
            existing_payload_hash TEXT NOT NULL,
            new_payload_hash TEXT NOT NULL,
            existing_row_json TEXT NOT NULL DEFAULT '{}',
            new_row_json TEXT NOT NULL DEFAULT '{}',
            build_id TEXT NOT NULL DEFAULT '',
            stage_name TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS news_event_cluster_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_cluster_id INTEGER NOT NULL,
            item_uid TEXT NOT NULL UNIQUE,
            item_type TEXT NOT NULL,
            news_id INTEGER,
            signal_id INTEGER,
            provider_family TEXT NOT NULL DEFAULT '',
            source_system TEXT NOT NULL DEFAULT '',
            provider TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            published_at_utc TEXT NOT NULL DEFAULT '',
            direction TEXT NOT NULL DEFAULT 'neutral',
            role TEXT NOT NULL DEFAULT 'context',
            included_in_confirmation INTEGER NOT NULL DEFAULT 0,
            duplicate_of_news_id INTEGER,
            double_count_status TEXT NOT NULL DEFAULT 'canonical_source',
            created_at TEXT NOT NULL,
            FOREIGN KEY(event_cluster_id) REFERENCES news_event_clusters(event_cluster_id),
            FOREIGN KEY(news_id) REFERENCES raw_news_articles(id)
        );
        CREATE INDEX IF NOT EXISTS idx_news_event_cluster_items_cluster
            ON news_event_cluster_items(event_cluster_id,item_type,included_in_confirmation);

        CREATE TABLE IF NOT EXISTS news_event_cluster_roles (
            event_cluster_id INTEGER PRIMARY KEY,
            event_role TEXT NOT NULL DEFAULT 'unknown',
            role_family TEXT NOT NULL DEFAULT 'unknown',
            direction TEXT NOT NULL DEFAULT 'neutral',
            role_confidence REAL NOT NULL DEFAULT 0,
            provider_agreement TEXT NOT NULL DEFAULT 'single_source',
            provider_confirmation_score REAL NOT NULL DEFAULT 0,
            provider_conflict_score REAL NOT NULL DEFAULT 0,
            marketaux_noise_score REAL NOT NULL DEFAULT 0,
            provider_count INTEGER NOT NULL DEFAULT 0,
            article_count INTEGER NOT NULL DEFAULT 0,
            source_news_count INTEGER NOT NULL DEFAULT 0,
            external_signal_count INTEGER NOT NULL DEFAULT 0,
            provider_families_json TEXT NOT NULL DEFAULT '[]',
            direction_counts_json TEXT NOT NULL DEFAULT '{}',
            shared_assets_json TEXT NOT NULL DEFAULT '[]',
            source_news_ids_json TEXT NOT NULL DEFAULT '[]',
            external_signal_ids_json TEXT NOT NULL DEFAULT '[]',
            double_count_suppressed_signal_ids_json TEXT NOT NULL DEFAULT '[]',
            clusterer_version TEXT NOT NULL,
            role_version TEXT NOT NULL,
            dedup_version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(event_cluster_id) REFERENCES news_event_clusters(event_cluster_id)
        );

        CREATE TABLE IF NOT EXISTS macro_release_calendar (
            calendar_event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            macro_calendar_key TEXT,
            source_system TEXT NOT NULL,
            release_name TEXT NOT NULL,
            country TEXT NOT NULL DEFAULT '',
            region TEXT NOT NULL DEFAULT '',
            currency TEXT NOT NULL DEFAULT '',
            event_type TEXT NOT NULL DEFAULT 'macro_data_release',
            scheduled_at_utc TEXT NOT NULL,
            expected_importance TEXT NOT NULL DEFAULT 'unknown',
            source_calendar TEXT NOT NULL DEFAULT '',
            calendar_version TEXT NOT NULL DEFAULT '',
            raw_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            UNIQUE(source_system,release_name,country,scheduled_at_utc,calendar_version)
        );
        CREATE INDEX IF NOT EXISTS idx_macro_release_calendar_time
            ON macro_release_calendar(scheduled_at_utc,source_system,event_type);

        CREATE TABLE IF NOT EXISTS macro_series_registry (
            registry_id INTEGER PRIMARY KEY AUTOINCREMENT,
            registry_key TEXT NOT NULL,
            source_system TEXT NOT NULL DEFAULT '*',
            series_id TEXT NOT NULL DEFAULT '',
            release_name TEXT NOT NULL DEFAULT '',
            semantic_type TEXT NOT NULL,
            event_type TEXT NOT NULL,
            event_cluster_eligible INTEGER NOT NULL DEFAULT 0,
            reaction_trigger_eligible INTEGER NOT NULL DEFAULT 0,
            regime_feature_eligible INTEGER NOT NULL DEFAULT 1,
            intraday_label_eligible INTEGER NOT NULL DEFAULT 0,
            release_calendar_source TEXT NOT NULL DEFAULT '',
            expected_frequency TEXT NOT NULL DEFAULT '',
            release_time_quality_required TEXT NOT NULL DEFAULT 'official_exact_or_provider_exact',
            registry_version TEXT NOT NULL,
            build_scope TEXT NOT NULL DEFAULT 'global_macro_v1',
            ibkr_market_data_policy TEXT NOT NULL DEFAULT 'read_only_contract_resolution',
            valid_from TEXT NOT NULL DEFAULT '',
            valid_to TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(registry_key)
        );
        CREATE INDEX IF NOT EXISTS idx_macro_series_registry_lookup
            ON macro_series_registry(source_system,series_id,registry_version,valid_from,valid_to);

        CREATE TABLE IF NOT EXISTS macro_event_releases (
            macro_event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            calendar_event_id INTEGER,
            event_cluster_id INTEGER,
            macro_event_key TEXT,
            source_system TEXT NOT NULL,
            release_name TEXT NOT NULL,
            country TEXT NOT NULL DEFAULT '',
            region TEXT NOT NULL DEFAULT '',
            currency TEXT NOT NULL DEFAULT '',
            event_type TEXT NOT NULL DEFAULT 'macro_data_release',
            scheduled_at_utc TEXT NOT NULL DEFAULT '',
            released_at_utc TEXT NOT NULL DEFAULT '',
            available_at_utc TEXT NOT NULL DEFAULT '',
            model_visible_at_utc TEXT NOT NULL DEFAULT '',
            release_time_quality TEXT NOT NULL DEFAULT 'unknown',
            importance TEXT NOT NULL DEFAULT 'unknown',
            source_url TEXT NOT NULL DEFAULT '',
            event_cluster_eligible INTEGER NOT NULL DEFAULT 0,
            reaction_trigger_eligible INTEGER NOT NULL DEFAULT 0,
            regime_feature_eligible INTEGER NOT NULL DEFAULT 0,
            intraday_label_eligible INTEGER NOT NULL DEFAULT 0,
            semantic_type TEXT NOT NULL DEFAULT 'unknown',
            registry_key TEXT NOT NULL DEFAULT '',
            registry_version TEXT NOT NULL DEFAULT '',
            release_time_policy TEXT NOT NULL DEFAULT '',
            event_eligibility_reason TEXT NOT NULL DEFAULT '',
            raw_json TEXT NOT NULL DEFAULT '{}',
            lineage_version TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY(calendar_event_id) REFERENCES macro_release_calendar(calendar_event_id),
            FOREIGN KEY(event_cluster_id) REFERENCES news_event_clusters(event_cluster_id),
            UNIQUE(source_system,release_name,country,scheduled_at_utc,event_type)
        );
        CREATE INDEX IF NOT EXISTS idx_macro_event_releases_cluster
            ON macro_event_releases(event_cluster_id,event_type,scheduled_at_utc);

        CREATE TABLE IF NOT EXISTS macro_observations (
            macro_observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
            macro_event_id INTEGER NOT NULL,
            macro_observation_key TEXT,
            series_id TEXT NOT NULL DEFAULT '',
            indicator_name TEXT NOT NULL DEFAULT '',
            actual_value REAL,
            forecast_value REAL,
            prior_value REAL,
            revised_prior_value REAL,
            unit TEXT NOT NULL DEFAULT '',
            period TEXT NOT NULL DEFAULT '',
            frequency TEXT NOT NULL DEFAULT '',
            seasonal_adjustment TEXT NOT NULL DEFAULT '',
            surprise_raw REAL,
            surprise_pct REAL,
            surprise_zscore REAL,
            revision_delta REAL,
            vintage_date TEXT NOT NULL DEFAULT '',
            vintage_quality TEXT NOT NULL DEFAULT 'unknown',
            data_source TEXT NOT NULL DEFAULT '',
            data_version TEXT NOT NULL DEFAULT '',
            strict_replay_eligible INTEGER NOT NULL DEFAULT 0,
            surprise_basis TEXT NOT NULL DEFAULT 'unknown',
            is_consensus_surprise INTEGER NOT NULL DEFAULT 0,
            raw_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            FOREIGN KEY(macro_event_id) REFERENCES macro_event_releases(macro_event_id)
        );
        CREATE INDEX IF NOT EXISTS idx_macro_observations_event
            ON macro_observations(macro_event_id,series_id,indicator_name);

        CREATE TABLE IF NOT EXISTS central_bank_events (
            macro_event_id INTEGER PRIMARY KEY,
            central_bank TEXT NOT NULL DEFAULT '',
            decision_type TEXT NOT NULL DEFAULT '',
            policy_rate_actual REAL,
            policy_rate_expected REAL,
            policy_rate_prior REAL,
            rate_surprise_bp REAL,
            statement_text TEXT,
            minutes_text TEXT,
            projection_json TEXT NOT NULL DEFAULT '{}',
            speaker TEXT NOT NULL DEFAULT '',
            hawkish_dovish_score REAL,
            text_classifier_version TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY(macro_event_id) REFERENCES macro_event_releases(macro_event_id)
        );

        CREATE TABLE IF NOT EXISTS macro_event_cluster_links (
            macro_cluster_link_key TEXT,
            macro_event_id INTEGER NOT NULL,
            event_cluster_id INTEGER NOT NULL,
            link_type TEXT NOT NULL DEFAULT 'primary_scheduled_event',
            confidence REAL NOT NULL DEFAULT 1.0,
            link_status TEXT NOT NULL DEFAULT 'active',
            link_reason TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            PRIMARY KEY(macro_event_id,event_cluster_id,link_type),
            FOREIGN KEY(macro_event_id) REFERENCES macro_event_releases(macro_event_id),
            FOREIGN KEY(event_cluster_id) REFERENCES news_event_clusters(event_cluster_id)
        );

        CREATE TABLE IF NOT EXISTS reaction_universe_plans (
            reaction_plan_id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_cluster_id INTEGER NOT NULL,
            plan_version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            available_at_cutoff TEXT NOT NULL DEFAULT '',
            core_scope_version TEXT NOT NULL DEFAULT '',
            event_proxy_map_version TEXT NOT NULL DEFAULT '',
            security_master_version TEXT NOT NULL DEFAULT '',
            mapper_version TEXT NOT NULL DEFAULT '',
            classifier_version TEXT NOT NULL DEFAULT '',
            reason TEXT NOT NULL DEFAULT '',
            FOREIGN KEY(event_cluster_id) REFERENCES news_event_clusters(event_cluster_id),
            UNIQUE(event_cluster_id,plan_version)
        );
        CREATE INDEX IF NOT EXISTS idx_reaction_universe_plans_cluster
            ON reaction_universe_plans(event_cluster_id,plan_version);

        CREATE TABLE IF NOT EXISTS reaction_universe_assets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reaction_plan_id INTEGER NOT NULL,
            event_cluster_id INTEGER NOT NULL,
            asset_symbol TEXT NOT NULL,
            con_id INTEGER,
            asset_role TEXT NOT NULL,
            proxy_family TEXT NOT NULL DEFAULT '',
            inclusion_reason TEXT NOT NULL DEFAULT '',
            inclusion_source TEXT NOT NULL DEFAULT '',
            tradable_candidate INTEGER NOT NULL DEFAULT 0,
            label_scope TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(reaction_plan_id) REFERENCES reaction_universe_plans(reaction_plan_id),
            FOREIGN KEY(event_cluster_id) REFERENCES news_event_clusters(event_cluster_id),
            UNIQUE(reaction_plan_id,asset_symbol,asset_role,label_scope,proxy_family)
        );
        CREATE INDEX IF NOT EXISTS idx_reaction_universe_assets_plan
            ON reaction_universe_assets(reaction_plan_id,label_scope,asset_role);
        CREATE INDEX IF NOT EXISTS idx_reaction_universe_assets_symbol
            ON reaction_universe_assets(asset_symbol,event_cluster_id,label_scope);

        CREATE TABLE IF NOT EXISTS event_reaction_labels (
            reaction_label_id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_cluster_id INTEGER NOT NULL,
            reaction_plan_id INTEGER NOT NULL,
            asset_symbol TEXT NOT NULL,
            asset_role TEXT NOT NULL,
            proxy_family TEXT NOT NULL DEFAULT '',
            window TEXT NOT NULL,
            decision_lag_seconds INTEGER NOT NULL DEFAULT 0,
            start_at_utc TEXT NOT NULL DEFAULT '',
            end_at_utc TEXT NOT NULL DEFAULT '',
            session_bucket TEXT NOT NULL DEFAULT 'unknown',
            raw_return REAL,
            market_adjusted_return REAL,
            sector_adjusted_return REAL,
            proxy_spread_return REAL,
            residual_return REAL,
            return_zscore REAL,
            volume_zscore REAL,
            realized_volatility REAL,
            mfe REAL,
            mae REAL,
            max_drawdown_in_window REAL,
            spread_estimate REAL,
            slippage_estimate REAL,
            liquidity_score REAL,
            market_data_profile_id TEXT NOT NULL DEFAULT '',
            entry_trade_observed INTEGER NOT NULL DEFAULT 0,
            exit_trade_observed INTEGER NOT NULL DEFAULT 0,
            entry_delay_seconds INTEGER,
            exit_delay_seconds INTEGER,
            observed_trade_count INTEGER NOT NULL DEFAULT 0,
            observed_bar_count INTEGER NOT NULL DEFAULT 0,
            window_trade_coverage_ratio REAL,
            no_trade_minute_count INTEGER NOT NULL DEFAULT 0,
            eth_sparse_trade_flag INTEGER NOT NULL DEFAULT 0,
            bad_tick_flag INTEGER NOT NULL DEFAULT 0,
            extreme_return_flag INTEGER NOT NULL DEFAULT 0,
            stale_trade_flag INTEGER NOT NULL DEFAULT 0,
            isolated_spike_flag INTEGER NOT NULL DEFAULT 0,
            price_quality_version TEXT NOT NULL DEFAULT '',
            price_quality_severity TEXT NOT NULL DEFAULT 'none',
            label_quality_flag TEXT NOT NULL DEFAULT 'pending_market_data',
            label_quality_reason TEXT NOT NULL DEFAULT '',
            label_exclusion_reason TEXT NOT NULL DEFAULT '',
            return_basis TEXT NOT NULL DEFAULT '',
            dividend_contaminated INTEGER NOT NULL DEFAULT 0,
            dividend_amount_in_window REAL NOT NULL DEFAULT 0,
            split_contaminated INTEGER NOT NULL DEFAULT 0,
            total_return_adjusted REAL,
            corporate_action_source TEXT NOT NULL DEFAULT '',
            corporate_action_version TEXT NOT NULL DEFAULT '',
            strict_training_eligible INTEGER NOT NULL DEFAULT 0,
            parent_meeting_id TEXT NOT NULL DEFAULT '',
            release_bundle_id TEXT NOT NULL DEFAULT '',
            overlap_group_id TEXT NOT NULL DEFAULT '',
            subevent_type TEXT NOT NULL DEFAULT '',
            sample_weight REAL NOT NULL DEFAULT 1.0,
            independent_sample_flag INTEGER NOT NULL DEFAULT 1,
            validation_split_group TEXT NOT NULL DEFAULT '',
            market_benchmark_symbol TEXT NOT NULL DEFAULT '',
            sector_benchmark_symbol TEXT NOT NULL DEFAULT '',
            beta_estimate REAL,
            estimation_window_start_at_utc TEXT NOT NULL DEFAULT '',
            estimation_window_end_at_utc TEXT NOT NULL DEFAULT '',
            residual_model_version TEXT NOT NULL DEFAULT '',
            return_math_version TEXT NOT NULL DEFAULT '',
            decision_lag_policy TEXT NOT NULL DEFAULT '',
            release_time_quality TEXT NOT NULL DEFAULT '',
            intraday_label_eligible INTEGER NOT NULL DEFAULT 0,
            horizon_anchor_policy TEXT NOT NULL DEFAULT '',
            first_regular_session_date TEXT NOT NULL DEFAULT '',
            entry_session_date TEXT NOT NULL DEFAULT '',
            exit_session_date TEXT NOT NULL DEFAULT '',
            execution_price_model TEXT NOT NULL DEFAULT '',
            spread_model_version TEXT NOT NULL DEFAULT '',
            slippage_model_version TEXT NOT NULL DEFAULT '',
            latency_model_version TEXT NOT NULL DEFAULT '',
            cost_estimate_quality TEXT NOT NULL DEFAULT '',
            outcome_builder_version TEXT NOT NULL DEFAULT '',
            market_data_source TEXT NOT NULL DEFAULT '',
            calendar_version TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY(event_cluster_id) REFERENCES news_event_clusters(event_cluster_id),
            FOREIGN KEY(reaction_plan_id) REFERENCES reaction_universe_plans(reaction_plan_id),
            UNIQUE(reaction_plan_id,asset_symbol,asset_role,proxy_family,window,decision_lag_seconds)
        );
        CREATE INDEX IF NOT EXISTS idx_event_reaction_labels_cluster
            ON event_reaction_labels(event_cluster_id,window,label_quality_flag);
        CREATE INDEX IF NOT EXISTS idx_event_reaction_labels_plan
            ON event_reaction_labels(reaction_plan_id,asset_symbol,window);

        CREATE TABLE IF NOT EXISTS replay_features (
            replay_id TEXT PRIMARY KEY,
            event_cluster_id INTEGER NOT NULL,
            as_of_time TEXT NOT NULL,
            model_visible_at TEXT NOT NULL,
            feature_snapshot_json TEXT NOT NULL DEFAULT '{}',
            feature_version TEXT NOT NULL DEFAULT '',
            proxy_map_version TEXT NOT NULL DEFAULT '',
            security_master_version TEXT NOT NULL DEFAULT '',
            mapper_version TEXT NOT NULL DEFAULT '',
            classifier_version TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY(event_cluster_id) REFERENCES news_event_clusters(event_cluster_id)
        );

        CREATE TABLE IF NOT EXISTS replay_outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            replay_id TEXT NOT NULL,
            outcome_window TEXT NOT NULL,
            asset_symbol TEXT NOT NULL,
            outcome_label TEXT NOT NULL DEFAULT '',
            pnl_label REAL,
            outcome_builder_version TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY(replay_id) REFERENCES replay_features(replay_id),
            UNIQUE(replay_id,outcome_window,asset_symbol,outcome_builder_version)
        );

        CREATE VIEW IF NOT EXISTS v_legacy_core_windows AS
            SELECT *
              FROM event_reaction_labels
             WHERE asset_role IN ('core_index','core_bigtech','core_macro','sector_context','benchmark');

        CREATE VIEW IF NOT EXISTS v_legacy_event_windows AS
            SELECT *
              FROM event_reaction_labels
             WHERE asset_role='event_proxy';
        """
    )

    # S05/M253 pilot label-quality contract. Existing databases receive the
    # same additive columns without rewriting append-only reaction rows.
    reaction_label_columns = {
        "market_data_profile_id": "TEXT NOT NULL DEFAULT ''",
        "entry_trade_observed": "INTEGER NOT NULL DEFAULT 0",
        "exit_trade_observed": "INTEGER NOT NULL DEFAULT 0",
        "entry_delay_seconds": "INTEGER",
        "exit_delay_seconds": "INTEGER",
        "observed_trade_count": "INTEGER NOT NULL DEFAULT 0",
        "observed_bar_count": "INTEGER NOT NULL DEFAULT 0",
        "window_trade_coverage_ratio": "REAL",
        "no_trade_minute_count": "INTEGER NOT NULL DEFAULT 0",
        "eth_sparse_trade_flag": "INTEGER NOT NULL DEFAULT 0",
        "bad_tick_flag": "INTEGER NOT NULL DEFAULT 0",
        "extreme_return_flag": "INTEGER NOT NULL DEFAULT 0",
        "stale_trade_flag": "INTEGER NOT NULL DEFAULT 0",
        "isolated_spike_flag": "INTEGER NOT NULL DEFAULT 0",
        "price_quality_version": "TEXT NOT NULL DEFAULT ''",
        "price_quality_severity": "TEXT NOT NULL DEFAULT 'none'",
        "label_exclusion_reason": "TEXT NOT NULL DEFAULT ''",
        "return_basis": "TEXT NOT NULL DEFAULT ''",
        "dividend_contaminated": "INTEGER NOT NULL DEFAULT 0",
        "dividend_amount_in_window": "REAL NOT NULL DEFAULT 0",
        "split_contaminated": "INTEGER NOT NULL DEFAULT 0",
        "total_return_adjusted": "REAL",
        "corporate_action_source": "TEXT NOT NULL DEFAULT ''",
        "corporate_action_version": "TEXT NOT NULL DEFAULT ''",
        "strict_training_eligible": "INTEGER NOT NULL DEFAULT 0",
        "parent_meeting_id": "TEXT NOT NULL DEFAULT ''",
        "release_bundle_id": "TEXT NOT NULL DEFAULT ''",
        "overlap_group_id": "TEXT NOT NULL DEFAULT ''",
        "subevent_type": "TEXT NOT NULL DEFAULT ''",
        "sample_weight": "REAL NOT NULL DEFAULT 1.0",
        "independent_sample_flag": "INTEGER NOT NULL DEFAULT 1",
        "validation_split_group": "TEXT NOT NULL DEFAULT ''",
        "market_benchmark_symbol": "TEXT NOT NULL DEFAULT ''",
        "sector_benchmark_symbol": "TEXT NOT NULL DEFAULT ''",
        "beta_estimate": "REAL",
        "estimation_window_start_at_utc": "TEXT NOT NULL DEFAULT ''",
        "estimation_window_end_at_utc": "TEXT NOT NULL DEFAULT ''",
        "residual_model_version": "TEXT NOT NULL DEFAULT ''",
        "return_math_version": "TEXT NOT NULL DEFAULT ''",
        "decision_lag_policy": "TEXT NOT NULL DEFAULT ''",
        "release_time_quality": "TEXT NOT NULL DEFAULT ''",
        "intraday_label_eligible": "INTEGER NOT NULL DEFAULT 0",
        "horizon_anchor_policy": "TEXT NOT NULL DEFAULT ''",
        "first_regular_session_date": "TEXT NOT NULL DEFAULT ''",
        "entry_session_date": "TEXT NOT NULL DEFAULT ''",
        "exit_session_date": "TEXT NOT NULL DEFAULT ''",
        "execution_price_model": "TEXT NOT NULL DEFAULT ''",
        "spread_model_version": "TEXT NOT NULL DEFAULT ''",
        "slippage_model_version": "TEXT NOT NULL DEFAULT ''",
        "latency_model_version": "TEXT NOT NULL DEFAULT ''",
        "cost_estimate_quality": "TEXT NOT NULL DEFAULT ''",
    }
    for column, definition in reaction_label_columns.items():
        _add_column_if_missing(conn, "event_reaction_labels", column, definition)

    conn.execute("DROP VIEW IF EXISTS v_current_event_classifications")
    conn.execute(
        """
        CREATE VIEW v_current_event_classifications AS
            SELECT c.*
              FROM news_event_classifications_canonical c
             WHERE NOT EXISTS (
                   SELECT 1
                     FROM news_event_classifications_canonical newer
                    WHERE newer.build_id = c.build_id
                      AND newer.event_cluster_id = c.event_cluster_id
                      AND COALESCE(newer.news_id, -1) = COALESCE(c.news_id, -1)
                      AND newer.classification_scope = c.classification_scope
                      AND (
                            newer.last_seen_at > c.last_seen_at
                         OR (newer.last_seen_at = c.last_seen_at
                             AND newer.classification_id > c.classification_id)
                      )
             );
        """
    )
    conn.execute("DROP VIEW IF EXISTS v_current_event_gates")
    conn.execute(
        """
        CREATE VIEW v_current_event_gates AS
            SELECT g.*
              FROM news_event_gates_canonical g
             WHERE NOT EXISTS (
                   SELECT 1
                     FROM news_event_gates_canonical newer
                    WHERE newer.build_id = g.build_id
                      AND newer.event_cluster_id = g.event_cluster_id
                      AND COALESCE(newer.news_id, -1) = COALESCE(g.news_id, -1)
                      AND newer.gate_scope = g.gate_scope
                      AND (
                            newer.last_seen_at > g.last_seen_at
                         OR (newer.last_seen_at = g.last_seen_at
                             AND newer.gate_id > g.gate_id)
                      )
             );
        """
    )

    _add_column_if_missing(conn, "news_event_clusters", "cluster_type", "TEXT NOT NULL DEFAULT 'news_event'")
    _add_column_if_missing(conn, "news_event_clusters", "primary_event_name", "TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing(conn, "news_event_clusters", "country", "TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing(conn, "news_event_clusters", "currency", "TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing(conn, "news_event_clusters", "macro_release_count", "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(conn, "news_event_clusters", "reaction_trigger_eligible", "INTEGER NOT NULL DEFAULT 1")
    _add_column_if_missing(conn, "news_event_clusters", "cluster_origin", "TEXT NOT NULL DEFAULT ''")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_news_event_clusters_type_class "
        "ON news_event_clusters(cluster_type,event_class,first_seen_at_utc)"
    )
    _add_column_if_missing(
        conn,
        "news_article_cluster_links",
        "link_type",
        "TEXT NOT NULL DEFAULT 'supporting_source'",
    )
    _add_column_if_missing(
        conn,
        "news_article_cluster_links",
        "is_primary_source",
        "INTEGER NOT NULL DEFAULT 0",
    )
    _add_column_if_missing(
        conn,
        "news_article_cluster_links",
        "link_role",
        "TEXT NOT NULL DEFAULT 'supporting_source'",
    )
    _add_column_if_missing(conn, "macro_release_calendar", "macro_calendar_key", "TEXT")
    _add_column_if_missing(conn, "macro_event_releases", "macro_event_key", "TEXT")
    _add_column_if_missing(conn, "macro_event_releases", "event_cluster_eligible", "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(conn, "macro_event_releases", "reaction_trigger_eligible", "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(conn, "macro_event_releases", "regime_feature_eligible", "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(conn, "macro_event_releases", "intraday_label_eligible", "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(conn, "macro_event_releases", "semantic_type", "TEXT NOT NULL DEFAULT 'unknown'")
    _add_column_if_missing(conn, "macro_event_releases", "registry_key", "TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing(conn, "macro_event_releases", "registry_version", "TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing(conn, "macro_event_releases", "release_time_policy", "TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing(conn, "macro_event_releases", "event_eligibility_reason", "TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing(conn, "macro_observations", "macro_observation_key", "TEXT")
    _add_column_if_missing(conn, "macro_observations", "strict_replay_eligible", "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(conn, "macro_observations", "surprise_basis", "TEXT NOT NULL DEFAULT 'unknown'")
    _add_column_if_missing(conn, "macro_observations", "is_consensus_surprise", "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(conn, "macro_event_cluster_links", "macro_cluster_link_key", "TEXT")
    _add_column_if_missing(conn, "macro_event_cluster_links", "link_status", "TEXT NOT NULL DEFAULT 'active'")
    _add_column_if_missing(conn, "macro_event_cluster_links", "link_reason", "TEXT NOT NULL DEFAULT ''")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_macro_release_calendar_key "
        "ON macro_release_calendar(macro_calendar_key) "
        "WHERE macro_calendar_key IS NOT NULL AND macro_calendar_key <> ''"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_macro_release_calendar_key_lookup "
        "ON macro_release_calendar(macro_calendar_key)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_macro_event_releases_key "
        "ON macro_event_releases(macro_event_key) "
        "WHERE macro_event_key IS NOT NULL AND macro_event_key <> ''"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_macro_event_releases_key_lookup "
        "ON macro_event_releases(macro_event_key)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_macro_observations_key "
        "ON macro_observations(macro_observation_key) "
        "WHERE macro_observation_key IS NOT NULL AND macro_observation_key <> ''"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_macro_observations_key_lookup "
        "ON macro_observations(macro_observation_key)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_macro_cluster_link_key "
        "ON macro_event_cluster_links(macro_cluster_link_key) "
        "WHERE macro_cluster_link_key IS NOT NULL AND macro_cluster_link_key <> ''"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_news_article_cluster_links_primary "
        "ON news_article_cluster_links(event_cluster_id,is_primary_source,link_type)"
    )
    conn.execute("DROP VIEW IF EXISTS v_canonical_intraday_reaction_trigger_macro_events")
    conn.execute("DROP VIEW IF EXISTS v_canonical_reaction_trigger_macro_events")
    conn.execute("DROP VIEW IF EXISTS v_macro_regime_features")
    conn.executescript(
        """
        CREATE VIEW v_canonical_reaction_trigger_macro_events AS
        SELECT
            r.macro_event_id,
            l.event_cluster_id,
            r.source_system,
            r.event_type,
            r.semantic_type,
            r.release_name,
            r.country,
            r.region,
            r.currency,
            r.scheduled_at_utc,
            r.released_at_utc,
            r.available_at_utc,
            r.model_visible_at_utc,
            r.release_time_quality,
            r.intraday_label_eligible,
            r.registry_key,
            r.registry_version,
            r.event_eligibility_reason,
            l.link_status,
            l.link_reason
        FROM macro_event_releases r
        JOIN macro_event_cluster_links l
          ON r.macro_event_id = l.macro_event_id
        JOIN news_event_clusters c
          ON l.event_cluster_id = c.event_cluster_id
        WHERE r.event_cluster_eligible = 1
          AND r.reaction_trigger_eligible = 1
          AND COALESCE(l.link_status,'active') = 'active'
          AND c.reaction_trigger_eligible = 1;

        CREATE VIEW v_canonical_intraday_reaction_trigger_macro_events AS
        SELECT *
        FROM v_canonical_reaction_trigger_macro_events
        WHERE intraday_label_eligible = 1
          AND release_time_quality IN ('official_exact','provider_exact');

        CREATE VIEW v_macro_regime_features AS
        SELECT
            r.macro_event_id,
            r.source_system,
            r.event_type,
            r.semantic_type,
            r.release_name,
            r.country,
            r.region,
            r.currency,
            r.scheduled_at_utc,
            r.released_at_utc,
            r.available_at_utc,
            r.model_visible_at_utc,
            r.release_time_quality,
            r.registry_key,
            r.registry_version,
            o.macro_observation_id,
            o.series_id,
            o.indicator_name,
            o.actual_value,
            o.unit,
            o.period,
            o.frequency,
            o.vintage_date,
            o.vintage_quality,
            o.strict_replay_eligible
        FROM macro_event_releases r
        LEFT JOIN macro_observations o
          ON r.macro_event_id = o.macro_event_id
        WHERE r.regime_feature_eligible = 1
          AND r.event_eligibility_reason NOT LIKE 'legacy_duplicate_of:%';
        """
    )

    _add_column_if_missing(conn, "news_asset_mapping_runs", "build_id", "TEXT NOT NULL DEFAULT 'live'")
    _add_column_if_missing(conn, "news_asset_mapping_runs", "mapping_run_key", "TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing(conn, "news_asset_mapping_runs", "last_seen_at", "TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing(conn, "news_asset_candidates", "candidate_key", "TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing(conn, "news_asset_candidates", "last_seen_at", "TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing(conn, "news_symbols", "link_type", "TEXT NOT NULL DEFAULT 'legacy_primary_symbol'")
    _add_column_if_missing(conn, "news_symbols", "role", "TEXT NOT NULL DEFAULT 'legacy'")
    _add_column_if_missing(conn, "news_symbols", "confidence", "REAL NOT NULL DEFAULT 1.0")
    _add_column_if_missing(conn, "news_symbols", "evidence", "TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing(conn, "news_symbols", "mapper_version", "TEXT NOT NULL DEFAULT ''")

    _add_column_if_missing(conn, "external_market_signals", "source_news_id", "INTEGER")
    _add_column_if_missing(conn, "external_market_signals", "event_cluster_id", "INTEGER")
    _add_column_if_missing(conn, "external_market_signals", "classifier_version", "TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing(conn, "external_market_signals", "mapper_version", "TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing(conn, "external_market_signals", "config_hash", "TEXT NOT NULL DEFAULT ''")
    _apply_asset_mapping_idempotency_migration(conn)
    _apply_asset_mapping_key_migration(conn)


def _json_loads(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if not value:
        return {}
    try:
        return json.loads(str(value))
    except Exception:
        return {}


def _stable_hash(parts: dict[str, Any]) -> str:
    payload = json.dumps(parts, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def canonical_classification_key_for_values(
    *,
    build_id: str,
    event_cluster_id: int,
    news_id: int | None,
    classification_scope: str,
    classifier_name: str,
    classifier_version: str,
    input_hash: str,
    config_hash: str,
) -> str:
    return _stable_hash(
        {
            "build_id": str(build_id or NEWS_CLASSIFICATION_GATE_CANONICAL_VERSION),
            "event_cluster_id": int(event_cluster_id),
            "news_id": "NONE" if news_id is None else int(news_id),
            "classification_scope": str(classification_scope or "cluster"),
            "classifier_name": str(classifier_name or ""),
            "classifier_version": str(classifier_version or ""),
            "input_hash": str(input_hash or ""),
            "config_hash": str(config_hash or ""),
        }
    )


def canonical_gate_key_for_values(
    *,
    build_id: str,
    event_cluster_id: int,
    news_id: int | None,
    gate_scope: str,
    gate_version: str,
    classification_key: str,
    input_hash: str,
    config_hash: str,
) -> str:
    return _stable_hash(
        {
            "build_id": str(build_id or NEWS_CLASSIFICATION_GATE_CANONICAL_VERSION),
            "event_cluster_id": int(event_cluster_id),
            "news_id": "NONE" if news_id is None else int(news_id),
            "gate_scope": str(gate_scope or "cluster"),
            "gate_version": str(gate_version or ""),
            "classification_key": str(classification_key or ""),
            "input_hash": str(input_hash or ""),
            "config_hash": str(config_hash or ""),
        }
    )


def _canonical_payload_hash(parts: dict[str, Any]) -> str:
    return _stable_hash(parts)


def _json_list_from_row_value(value: Any) -> list[Any]:
    payload = _json_loads(value)
    return payload if isinstance(payload, list) else []


def _canonical_cluster_input_hash(conn: sqlite3.Connection, event_cluster_id: int) -> str:
    cluster = conn.execute(
        "SELECT * FROM news_event_clusters WHERE event_cluster_id=?",
        (int(event_cluster_id),),
    ).fetchone()
    role = conn.execute(
        "SELECT * FROM news_event_cluster_roles WHERE event_cluster_id=?",
        (int(event_cluster_id),),
    ).fetchone()
    linked_news_ids = [
        int(row["news_id"])
        for row in conn.execute(
            """SELECT news_id
                 FROM news_article_cluster_links
                WHERE event_cluster_id=?
                ORDER BY news_id""",
            (int(event_cluster_id),),
        ).fetchall()
    ]
    signal_ids: list[int] = []
    if _table_exists(conn, "external_market_signals"):
        signal_ids = [
            int(row["id"])
            for row in conn.execute(
                """SELECT id
                     FROM external_market_signals
                    WHERE event_cluster_id=?
                    ORDER BY id""",
                (int(event_cluster_id),),
            ).fetchall()
        ]
    role_payload: dict[str, Any] = {}
    if role is not None:
        role_payload = {
            "event_role": role["event_role"],
            "role_family": role["role_family"],
            "direction": role["direction"],
            "role_confidence": role["role_confidence"],
            "provider_agreement": role["provider_agreement"],
            "provider_confirmation_score": role["provider_confirmation_score"],
            "provider_conflict_score": role["provider_conflict_score"],
            "marketaux_noise_score": role["marketaux_noise_score"],
            "provider_families": _json_list_from_row_value(role["provider_families_json"]),
            "direction_counts": _json_loads(role["direction_counts_json"]),
            "shared_assets": _json_list_from_row_value(role["shared_assets_json"]),
            "source_news_ids": _json_list_from_row_value(role["source_news_ids_json"]),
            "external_signal_ids": _json_list_from_row_value(role["external_signal_ids_json"]),
        }
    cluster_payload = {}
    if cluster is not None:
        cluster_payload = {
            "cluster_key": cluster["cluster_key"],
            "primary_headline": cluster["primary_headline"],
            "first_seen_at_utc": cluster["first_seen_at_utc"],
            "last_seen_at_utc": cluster["last_seen_at_utc"],
            "event_class": cluster["event_class"],
            "canonical_entities": _json_list_from_row_value(cluster["canonical_entities_json"]),
            "cluster_type": cluster["cluster_type"] if "cluster_type" in cluster.keys() else "news_event",
        }
    return _stable_hash(
        {
            "cluster": cluster_payload,
            "role": role_payload,
            "linked_news_ids": linked_news_ids,
            "signal_ids": signal_ids,
            "version": NEWS_CLASSIFICATION_GATE_CANONICAL_VERSION,
        }
    )


def _classification_config_hash() -> str:
    return _stable_hash(
        {
            "canonical_version": NEWS_CLASSIFICATION_GATE_CANONICAL_VERSION,
            "clusterer_version": NEWS_EVENT_CLUSTERER_VERSION,
            "role_version": NEWS_CLUSTER_ROLE_VERSION,
            "dedup_version": NEWS_CLUSTER_DEDUP_VERSION,
        }
    )


def _classification_source_summary(
    conn: sqlite3.Connection,
    event_cluster_id: int,
) -> tuple[list[int], int]:
    rows = conn.execute(
        """SELECT id,event_class,region,theme,direction,severity,sentiment_score,
                  classification_confidence,classifier_name,classifier_version
             FROM news_event_classifications
            WHERE event_cluster_id=?
              AND COALESCE(classification_scope,'cluster')='cluster'
              AND news_id IS NULL
            ORDER BY id""",
        (int(event_cluster_id),),
    ).fetchall()
    source_ids = [int(row["id"]) for row in rows]
    payloads = {
        _stable_hash(
            {
                "event_class": row["event_class"],
                "region": row["region"],
                "theme": row["theme"],
                "direction": row["direction"],
                "severity": row["severity"],
                "sentiment_score": row["sentiment_score"],
                "classification_confidence": row["classification_confidence"],
                "classifier_name": row["classifier_name"],
                "classifier_version": row["classifier_version"],
            }
        )
        for row in rows
    }
    return source_ids, max(0, len(payloads) - 1)


def _gate_source_summary(conn: sqlite3.Connection, event_cluster_id: int) -> tuple[list[int], int]:
    rows = conn.execute(
        """SELECT id,gate_status,gate_score,blocked_reason,gate_version
             FROM news_event_gates
            WHERE event_cluster_id=?
              AND COALESCE(gate_scope,'cluster')='cluster'
              AND news_id IS NULL
            ORDER BY id""",
        (int(event_cluster_id),),
    ).fetchall()
    source_ids = [int(row["id"]) for row in rows]
    payloads = {
        _stable_hash(
            {
                "gate_status": row["gate_status"],
                "gate_score": row["gate_score"],
                "blocked_reason": row["blocked_reason"],
                "gate_version": row["gate_version"],
            }
        )
        for row in rows
    }
    return source_ids, max(0, len(payloads) - 1)


def _record_canonical_conflict(
    conn: sqlite3.Connection,
    *,
    table_name: str,
    key: str,
    existing_payload_hash: str,
    new_payload_hash: str,
    existing_row: dict[str, Any],
    new_row: dict[str, Any],
    build_id: str,
    stage_name: str,
) -> None:
    conn.execute(
        """INSERT INTO classification_gate_conflicts(
            table_name,key,existing_payload_hash,new_payload_hash,
            existing_row_json,new_row_json,build_id,stage_name,created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            table_name,
            key,
            existing_payload_hash,
            new_payload_hash,
            json.dumps(existing_row, ensure_ascii=False, sort_keys=True, default=str),
            json.dumps(new_row, ensure_ascii=False, sort_keys=True, default=str),
            build_id,
            stage_name,
            utc_now_iso(),
        ),
    )


def upsert_canonical_classification_gate_for_cluster(
    conn: sqlite3.Connection,
    event_cluster_id: int,
    *,
    build_id: str = NEWS_CLASSIFICATION_GATE_CANONICAL_VERSION,
    stage_name: str = "S03_classification_gate_canonicalization",
    ensure_schema: bool = True,
) -> dict[str, Any]:
    """Recompute one current cluster classification/gate into canonical tables.

    The legacy history tables are intentionally left append-only.  This function
    writes deterministic current rows keyed by input/config hashes, and records
    conflicts instead of silently overwriting when the same key would produce a
    different payload.
    """

    if ensure_schema:
        ensure_news_lineage_v2_schema(conn)
    cluster = conn.execute(
        "SELECT * FROM news_event_clusters WHERE event_cluster_id=?",
        (int(event_cluster_id),),
    ).fetchone()
    if cluster is None:
        raise ValueError(f"event_cluster_id={event_cluster_id} does not exist")
    role = conn.execute(
        "SELECT * FROM news_event_cluster_roles WHERE event_cluster_id=?",
        (int(event_cluster_id),),
    ).fetchone()
    if role is None:
        role_family = "unknown"
        event_role = "unknown"
        direction = "neutral"
        role_confidence = 0.0
        provider_agreement = "no_evidence"
        provider_confirmation_score = 0.0
        provider_conflict_score = 0.0
    else:
        role_family = str(role["role_family"] or "unknown")
        event_role = str(role["event_role"] or "unknown")
        direction = str(role["direction"] or "neutral")
        role_confidence = float(role["role_confidence"] or 0.0)
        provider_agreement = str(role["provider_agreement"] or "no_evidence")
        provider_confirmation_score = float(role["provider_confirmation_score"] or 0.0)
        provider_conflict_score = float(role["provider_conflict_score"] or 0.0)

    event_class = f"{role_family}.{event_role}" if role_family != "unknown" else "unknown"
    input_hash = _canonical_cluster_input_hash(conn, int(event_cluster_id))
    config_hash = _classification_config_hash()
    classifier_name = "m252_cluster_role_builder"
    classifier_version = NEWS_CLUSTER_ROLE_VERSION
    classification_key = canonical_classification_key_for_values(
        build_id=build_id,
        event_cluster_id=int(event_cluster_id),
        news_id=None,
        classification_scope="cluster",
        classifier_name=classifier_name,
        classifier_version=classifier_version,
        input_hash=input_hash,
        config_hash=config_hash,
    )
    classification_payload = {
        "event_class": event_class,
        "region": "unknown",
        "theme": role_family,
        "direction": direction,
        "severity": provider_confirmation_score,
        "sentiment_score": None,
        "classification_confidence": role_confidence,
    }
    classification_payload_hash = _canonical_payload_hash(classification_payload)
    source_classification_ids, legacy_classification_conflict_source_count = _classification_source_summary(
        conn,
        int(event_cluster_id),
    )
    classification_conflict_source_count = 0
    now = utc_now_iso()
    classification_row = {
        "classification_key": classification_key,
        "build_id": build_id,
        "event_cluster_id": int(event_cluster_id),
        "news_id": None,
        "classification_scope": "cluster",
        "classifier_name": classifier_name,
        "classifier_version": classifier_version,
        "input_hash": input_hash,
        "config_hash": config_hash,
        **classification_payload,
        "matched_rules_json": "{}",
        "payload_hash": classification_payload_hash,
        "source_classification_ids_json": json.dumps(source_classification_ids, ensure_ascii=False),
        "conflict_source_count": classification_conflict_source_count,
    }
    existing = conn.execute(
        "SELECT * FROM news_event_classifications_canonical WHERE classification_key=?",
        (classification_key,),
    ).fetchone()
    if existing is not None and str(existing["payload_hash"]) != classification_payload_hash:
        _record_canonical_conflict(
            conn,
            table_name="news_event_classifications_canonical",
            key=classification_key,
            existing_payload_hash=str(existing["payload_hash"]),
            new_payload_hash=classification_payload_hash,
            existing_row=dict(existing),
            new_row=classification_row,
            build_id=build_id,
            stage_name=stage_name,
        )
        raise ValueError(f"classification canonical payload conflict for key={classification_key}")
    conn.execute(
        """INSERT INTO news_event_classifications_canonical(
            classification_key,build_id,event_cluster_id,news_id,classification_scope,
            classifier_name,classifier_version,input_hash,config_hash,event_class,
            region,theme,direction,severity,sentiment_score,classification_confidence,
            matched_rules_json,payload_hash,source_classification_ids_json,
            conflict_source_count,created_at,last_seen_at
        ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(classification_key) DO UPDATE SET
            payload_hash=excluded.payload_hash,
            source_classification_ids_json=excluded.source_classification_ids_json,
            conflict_source_count=excluded.conflict_source_count,
            last_seen_at=excluded.last_seen_at""",
        (
            classification_key,
            build_id,
            int(event_cluster_id),
            "cluster",
            classifier_name,
            classifier_version,
            input_hash,
            config_hash,
            classification_payload["event_class"],
            classification_payload["region"],
            classification_payload["theme"],
            classification_payload["direction"],
            classification_payload["severity"],
            classification_payload["sentiment_score"],
            classification_payload["classification_confidence"],
            "{}",
            classification_payload_hash,
            json.dumps(source_classification_ids, ensure_ascii=False),
            classification_conflict_source_count,
            now,
            now,
        ),
    )

    gate_status = "review"
    if provider_agreement == "confirmed" and provider_confirmation_score >= 0.75 and provider_conflict_score < 0.5:
        gate_status = "watch"
    elif provider_agreement == "conflict":
        gate_status = "review_conflict"
    elif provider_agreement == "marketaux_noise_filtered":
        gate_status = "ignore_noise"
    gate_score = max(0.0, provider_confirmation_score - provider_conflict_score)
    gate_version = NEWS_CLUSTER_DEDUP_VERSION
    gate_key = canonical_gate_key_for_values(
        build_id=build_id,
        event_cluster_id=int(event_cluster_id),
        news_id=None,
        gate_scope="cluster",
        gate_version=gate_version,
        classification_key=classification_key,
        input_hash=input_hash,
        config_hash=config_hash,
    )
    gate_payload = {
        "gate_status": gate_status,
        "gate_score": gate_score,
        "blocked_reason": provider_agreement,
    }
    gate_payload_hash = _canonical_payload_hash(gate_payload)
    source_gate_ids, legacy_gate_conflict_source_count = _gate_source_summary(conn, int(event_cluster_id))
    gate_conflict_source_count = 0
    gate_row = {
        "gate_key": gate_key,
        "build_id": build_id,
        "event_cluster_id": int(event_cluster_id),
        "news_id": None,
        "gate_scope": "cluster",
        "gate_version": gate_version,
        "classification_key": classification_key,
        "input_hash": input_hash,
        "config_hash": config_hash,
        **gate_payload,
        "payload_hash": gate_payload_hash,
        "source_gate_ids_json": json.dumps(source_gate_ids, ensure_ascii=False),
        "conflict_source_count": gate_conflict_source_count,
    }
    existing_gate = conn.execute(
        "SELECT * FROM news_event_gates_canonical WHERE gate_key=?",
        (gate_key,),
    ).fetchone()
    if existing_gate is not None and str(existing_gate["payload_hash"]) != gate_payload_hash:
        _record_canonical_conflict(
            conn,
            table_name="news_event_gates_canonical",
            key=gate_key,
            existing_payload_hash=str(existing_gate["payload_hash"]),
            new_payload_hash=gate_payload_hash,
            existing_row=dict(existing_gate),
            new_row=gate_row,
            build_id=build_id,
            stage_name=stage_name,
        )
        raise ValueError(f"gate canonical payload conflict for key={gate_key}")
    conn.execute(
        """INSERT INTO news_event_gates_canonical(
            gate_key,build_id,event_cluster_id,news_id,gate_scope,gate_version,
            classification_key,input_hash,config_hash,gate_status,gate_score,
            blocked_reason,payload_hash,source_gate_ids_json,conflict_source_count,
            created_at,last_seen_at
        ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(gate_key) DO UPDATE SET
            payload_hash=excluded.payload_hash,
            source_gate_ids_json=excluded.source_gate_ids_json,
            conflict_source_count=excluded.conflict_source_count,
            last_seen_at=excluded.last_seen_at""",
        (
            gate_key,
            build_id,
            int(event_cluster_id),
            "cluster",
            gate_version,
            classification_key,
            input_hash,
            config_hash,
            gate_status,
            gate_score,
            provider_agreement,
            gate_payload_hash,
            json.dumps(source_gate_ids, ensure_ascii=False),
            gate_conflict_source_count,
            now,
            now,
        ),
    )
    return {
        "event_cluster_id": int(event_cluster_id),
        "classification_key": classification_key,
        "gate_key": gate_key,
        "classification_conflict_source_count": classification_conflict_source_count,
        "gate_conflict_source_count": gate_conflict_source_count,
        "legacy_classification_conflict_source_count": legacy_classification_conflict_source_count,
        "legacy_gate_conflict_source_count": legacy_gate_conflict_source_count,
    }


def rebuild_canonical_classification_gate_tables(
    conn: sqlite3.Connection,
    *,
    build_id: str = NEWS_CLASSIFICATION_GATE_CANONICAL_VERSION,
    stage_name: str = "S03_classification_gate_canonicalization",
    limit: int | None = None,
) -> dict[str, Any]:
    """Batch rebuild current classification/gate rows without per-cluster DB scans."""

    ensure_news_lineage_v2_schema(conn)
    sql = "SELECT * FROM news_event_clusters ORDER BY event_cluster_id"
    params: tuple[Any, ...] = ()
    if limit is not None:
        sql += " LIMIT ?"
        params = (int(limit),)
    clusters = conn.execute(sql, params).fetchall()
    selected_ids = {int(row["event_cluster_id"]) for row in clusters}

    roles = {
        int(row["event_cluster_id"]): row
        for row in conn.execute("SELECT * FROM news_event_cluster_roles").fetchall()
        if int(row["event_cluster_id"]) in selected_ids
    }
    linked_news_ids: dict[int, list[int]] = {}
    for row in conn.execute(
        "SELECT event_cluster_id,news_id FROM news_article_cluster_links ORDER BY event_cluster_id,news_id"
    ).fetchall():
        event_cluster_id = int(row["event_cluster_id"])
        if event_cluster_id in selected_ids:
            linked_news_ids.setdefault(event_cluster_id, []).append(int(row["news_id"]))
    signal_ids: dict[int, list[int]] = {}
    if _table_exists(conn, "external_market_signals"):
        for row in conn.execute(
            """SELECT event_cluster_id,id
                 FROM external_market_signals
                WHERE event_cluster_id IS NOT NULL
                ORDER BY event_cluster_id,id"""
        ).fetchall():
            event_cluster_id = int(row["event_cluster_id"])
            if event_cluster_id in selected_ids:
                signal_ids.setdefault(event_cluster_id, []).append(int(row["id"]))

    classification_summaries: dict[int, tuple[list[int], int]] = {}
    classification_payloads: dict[int, set[str]] = {}
    for row in conn.execute(
        """SELECT id,event_cluster_id,event_class,region,theme,direction,severity,
                  sentiment_score,classification_confidence,classifier_name,classifier_version
             FROM news_event_classifications
            WHERE event_cluster_id IS NOT NULL
              AND COALESCE(classification_scope,'cluster')='cluster'
              AND news_id IS NULL
            ORDER BY event_cluster_id,id"""
    ).fetchall():
        event_cluster_id = int(row["event_cluster_id"])
        if event_cluster_id not in selected_ids:
            continue
        ids, _ = classification_summaries.setdefault(event_cluster_id, ([], 0))
        ids.append(int(row["id"]))
        classification_payloads.setdefault(event_cluster_id, set()).add(
            _stable_hash(
                {
                    "event_class": row["event_class"],
                    "region": row["region"],
                    "theme": row["theme"],
                    "direction": row["direction"],
                    "severity": row["severity"],
                    "sentiment_score": row["sentiment_score"],
                    "classification_confidence": row["classification_confidence"],
                    "classifier_name": row["classifier_name"],
                    "classifier_version": row["classifier_version"],
                }
            )
        )
    for event_cluster_id, payloads in classification_payloads.items():
        ids, _ = classification_summaries.get(event_cluster_id, ([], 0))
        classification_summaries[event_cluster_id] = (ids, max(0, len(payloads) - 1))

    gate_summaries: dict[int, tuple[list[int], int]] = {}
    gate_payloads: dict[int, set[str]] = {}
    for row in conn.execute(
        """SELECT id,event_cluster_id,gate_status,gate_score,blocked_reason,gate_version
             FROM news_event_gates
            WHERE event_cluster_id IS NOT NULL
              AND COALESCE(gate_scope,'cluster')='cluster'
              AND news_id IS NULL
            ORDER BY event_cluster_id,id"""
    ).fetchall():
        event_cluster_id = int(row["event_cluster_id"])
        if event_cluster_id not in selected_ids:
            continue
        ids, _ = gate_summaries.setdefault(event_cluster_id, ([], 0))
        ids.append(int(row["id"]))
        gate_payloads.setdefault(event_cluster_id, set()).add(
            _stable_hash(
                {
                    "gate_status": row["gate_status"],
                    "gate_score": row["gate_score"],
                    "blocked_reason": row["blocked_reason"],
                    "gate_version": row["gate_version"],
                }
            )
        )
    for event_cluster_id, payloads in gate_payloads.items():
        ids, _ = gate_summaries.get(event_cluster_id, ([], 0))
        gate_summaries[event_cluster_id] = (ids, max(0, len(payloads) - 1))

    existing_classifications = {
        str(row["classification_key"]): row
        for row in conn.execute("SELECT * FROM news_event_classifications_canonical").fetchall()
    }
    existing_gates = {
        str(row["gate_key"]): row
        for row in conn.execute("SELECT * FROM news_event_gates_canonical").fetchall()
    }

    now = utc_now_iso()
    config_hash = _classification_config_hash()
    classifier_name = "m252_cluster_role_builder"
    classifier_version = NEWS_CLUSTER_ROLE_VERSION
    gate_version = NEWS_CLUSTER_DEDUP_VERSION
    classification_rows: list[tuple[Any, ...]] = []
    gate_rows: list[tuple[Any, ...]] = []
    for cluster in clusters:
        event_cluster_id = int(cluster["event_cluster_id"])
        role = roles.get(event_cluster_id)
        if role is None:
            role_family = "unknown"
            event_role = "unknown"
            direction = "neutral"
            role_confidence = 0.0
            provider_agreement = "no_evidence"
            provider_confirmation_score = 0.0
            provider_conflict_score = 0.0
            role_payload: dict[str, Any] = {}
        else:
            role_family = str(role["role_family"] or "unknown")
            event_role = str(role["event_role"] or "unknown")
            direction = str(role["direction"] or "neutral")
            role_confidence = float(role["role_confidence"] or 0.0)
            provider_agreement = str(role["provider_agreement"] or "no_evidence")
            provider_confirmation_score = float(role["provider_confirmation_score"] or 0.0)
            provider_conflict_score = float(role["provider_conflict_score"] or 0.0)
            role_payload = {
                "event_role": role["event_role"],
                "role_family": role["role_family"],
                "direction": role["direction"],
                "role_confidence": role["role_confidence"],
                "provider_agreement": role["provider_agreement"],
                "provider_confirmation_score": role["provider_confirmation_score"],
                "provider_conflict_score": role["provider_conflict_score"],
                "marketaux_noise_score": role["marketaux_noise_score"],
                "provider_families": _json_list_from_row_value(role["provider_families_json"]),
                "direction_counts": _json_loads(role["direction_counts_json"]),
                "shared_assets": _json_list_from_row_value(role["shared_assets_json"]),
                "source_news_ids": _json_list_from_row_value(role["source_news_ids_json"]),
                "external_signal_ids": _json_list_from_row_value(role["external_signal_ids_json"]),
            }

        cluster_payload = {
            "cluster_key": cluster["cluster_key"],
            "primary_headline": cluster["primary_headline"],
            "first_seen_at_utc": cluster["first_seen_at_utc"],
            "last_seen_at_utc": cluster["last_seen_at_utc"],
            "event_class": cluster["event_class"],
            "canonical_entities": _json_list_from_row_value(cluster["canonical_entities_json"]),
            "cluster_type": cluster["cluster_type"] if "cluster_type" in cluster.keys() else "news_event",
        }
        input_hash = _stable_hash(
            {
                "cluster": cluster_payload,
                "role": role_payload,
                "linked_news_ids": linked_news_ids.get(event_cluster_id, []),
                "signal_ids": signal_ids.get(event_cluster_id, []),
                "version": NEWS_CLASSIFICATION_GATE_CANONICAL_VERSION,
            }
        )

        event_class = f"{role_family}.{event_role}" if role_family != "unknown" else "unknown"
        classification_key = canonical_classification_key_for_values(
            build_id=build_id,
            event_cluster_id=event_cluster_id,
            news_id=None,
            classification_scope="cluster",
            classifier_name=classifier_name,
            classifier_version=classifier_version,
            input_hash=input_hash,
            config_hash=config_hash,
        )
        classification_payload = {
            "event_class": event_class,
            "region": "unknown",
            "theme": role_family,
            "direction": direction,
            "severity": provider_confirmation_score,
            "sentiment_score": None,
            "classification_confidence": role_confidence,
        }
        classification_payload_hash = _canonical_payload_hash(classification_payload)
        source_classification_ids, legacy_classification_conflict_source_count = classification_summaries.get(
            event_cluster_id, ([], 0)
        )
        classification_conflict_source_count = 0
        existing = existing_classifications.get(classification_key)
        if existing is not None and str(existing["payload_hash"]) != classification_payload_hash:
            _record_canonical_conflict(
                conn,
                table_name="news_event_classifications_canonical",
                key=classification_key,
                existing_payload_hash=str(existing["payload_hash"]),
                new_payload_hash=classification_payload_hash,
                existing_row=dict(existing),
                new_row={"classification_key": classification_key, "event_cluster_id": event_cluster_id},
                build_id=build_id,
                stage_name=stage_name,
            )
            raise ValueError(f"classification canonical payload conflict for key={classification_key}")
        classification_rows.append(
            (
                classification_key,
                build_id,
                event_cluster_id,
                "cluster",
                classifier_name,
                classifier_version,
                input_hash,
                config_hash,
                classification_payload["event_class"],
                classification_payload["region"],
                classification_payload["theme"],
                classification_payload["direction"],
                classification_payload["severity"],
                classification_payload["sentiment_score"],
                classification_payload["classification_confidence"],
                "{}",
                classification_payload_hash,
                json.dumps(source_classification_ids, ensure_ascii=False),
                classification_conflict_source_count,
                now,
                now,
            )
        )

        gate_status = "review"
        if provider_agreement == "confirmed" and provider_confirmation_score >= 0.75 and provider_conflict_score < 0.5:
            gate_status = "watch"
        elif provider_agreement == "conflict":
            gate_status = "review_conflict"
        elif provider_agreement == "marketaux_noise_filtered":
            gate_status = "ignore_noise"
        gate_score = max(0.0, provider_confirmation_score - provider_conflict_score)
        gate_key = canonical_gate_key_for_values(
            build_id=build_id,
            event_cluster_id=event_cluster_id,
            news_id=None,
            gate_scope="cluster",
            gate_version=gate_version,
            classification_key=classification_key,
            input_hash=input_hash,
            config_hash=config_hash,
        )
        gate_payload = {
            "gate_status": gate_status,
            "gate_score": gate_score,
            "blocked_reason": provider_agreement,
        }
        gate_payload_hash = _canonical_payload_hash(gate_payload)
        source_gate_ids, legacy_gate_conflict_source_count = gate_summaries.get(event_cluster_id, ([], 0))
        gate_conflict_source_count = 0
        existing_gate = existing_gates.get(gate_key)
        if existing_gate is not None and str(existing_gate["payload_hash"]) != gate_payload_hash:
            _record_canonical_conflict(
                conn,
                table_name="news_event_gates_canonical",
                key=gate_key,
                existing_payload_hash=str(existing_gate["payload_hash"]),
                new_payload_hash=gate_payload_hash,
                existing_row=dict(existing_gate),
                new_row={"gate_key": gate_key, "event_cluster_id": event_cluster_id},
                build_id=build_id,
                stage_name=stage_name,
            )
            raise ValueError(f"gate canonical payload conflict for key={gate_key}")
        gate_rows.append(
            (
                gate_key,
                build_id,
                event_cluster_id,
                "cluster",
                gate_version,
                classification_key,
                input_hash,
                config_hash,
                gate_status,
                gate_score,
                provider_agreement,
                gate_payload_hash,
                json.dumps(source_gate_ids, ensure_ascii=False),
                gate_conflict_source_count,
                now,
                now,
            )
        )

    conn.executemany(
        """INSERT INTO news_event_classifications_canonical(
            classification_key,build_id,event_cluster_id,news_id,classification_scope,
            classifier_name,classifier_version,input_hash,config_hash,event_class,
            region,theme,direction,severity,sentiment_score,classification_confidence,
            matched_rules_json,payload_hash,source_classification_ids_json,
            conflict_source_count,created_at,last_seen_at
        ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(classification_key) DO UPDATE SET
            payload_hash=excluded.payload_hash,
            source_classification_ids_json=excluded.source_classification_ids_json,
            conflict_source_count=excluded.conflict_source_count,
            last_seen_at=excluded.last_seen_at""",
        classification_rows,
    )
    conn.executemany(
        """INSERT INTO news_event_gates_canonical(
            gate_key,build_id,event_cluster_id,news_id,gate_scope,gate_version,
            classification_key,input_hash,config_hash,gate_status,gate_score,
            blocked_reason,payload_hash,source_gate_ids_json,conflict_source_count,
            created_at,last_seen_at
        ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(gate_key) DO UPDATE SET
            payload_hash=excluded.payload_hash,
            source_gate_ids_json=excluded.source_gate_ids_json,
            conflict_source_count=excluded.conflict_source_count,
            last_seen_at=excluded.last_seen_at""",
        gate_rows,
    )
    return {
        "processed_clusters": len(clusters),
        "classification_canonical_rows": int(
            conn.execute("SELECT COUNT(*) AS c FROM news_event_classifications_canonical").fetchone()["c"]
        ),
        "gate_canonical_rows": int(
            conn.execute("SELECT COUNT(*) AS c FROM news_event_gates_canonical").fetchone()["c"]
        ),
        "current_classification_rows": int(
            conn.execute("SELECT COUNT(*) AS c FROM v_current_event_classifications").fetchone()["c"]
        ),
        "current_gate_rows": int(conn.execute("SELECT COUNT(*) AS c FROM v_current_event_gates").fetchone()["c"]),
        "conflicts": int(
            conn.execute("SELECT COUNT(*) AS c FROM classification_gate_conflicts").fetchone()["c"]
        ),
    }


def content_hash_for(
    *,
    source_system: str,
    provider: str,
    headline: str,
    body: str | None = None,
    url: str = "",
    published_at_utc: str = "",
) -> str:
    return _stable_hash(
        {
            "source_system": source_system,
            "provider": provider,
            "headline": " ".join(str(headline or "").split()),
            "body": str(body or "")[:2000],
            "url": str(url or ""),
            "published_at_utc": str(published_at_utc or ""),
        }
    )


def _source_system_for_provider(provider: str) -> str:
    value = str(provider or "").strip().lower()
    if value == "marketaux":
        return "marketaux"
    return "ibkr"


def _infer_ingest_mode(extra_payload: Any, *, source_system: str) -> str:
    if source_system == "marketaux":
        mode = str((extra_payload or {}).get("ingest_mode") or "").strip()
        return mode if mode in {"polling", "manual_backfill"} else "polling"
    if isinstance(extra_payload, dict):
        marker = " ".join(
            str(extra_payload.get(key) or "")
            for key in ("source", "source_mode", "ingest_mode", "route_type")
        ).lower()
        if "historical" in marker:
            return "historical_backfill"
        if "manual_backfill" in marker:
            return "manual_backfill"
        if "polling" in marker:
            return "polling"
    return "live"


def _infer_route_type(primary_route_symbol: str, *, source_system: str, extra_payload: Any) -> str:
    if source_system == "marketaux":
        return "external_api"
    if str(primary_route_symbol or "").upper().startswith("BROADTAPE:"):
        return "broadtape"
    if isinstance(extra_payload, dict):
        explicit = str(extra_payload.get("route_type") or "").strip()
        if explicit in {"ticker_specific", "broadtape", "external_api"}:
            return explicit
    return "ticker_specific"


def upsert_raw_news_article(
    conn: sqlite3.Connection,
    *,
    legacy_news_id: int | None = None,
    source_system: str,
    provider: str,
    publisher: str = "",
    provider_article_id: str = "",
    headline: str,
    body: str | None = None,
    url: str = "",
    published_at_utc: str = "",
    updated_at_utc: str | None = None,
    received_at_utc: str = "",
    available_at_utc: str = "",
    availability_quality: str = "unknown",
    ingest_mode: str,
    route_type: str,
    primary_route_symbol: str = "",
    provider_primary_symbol: str = "",
    raw_json: dict[str, Any] | None = None,
    content_hash: str | None = None,
) -> int:
    ensure_news_lineage_v2_schema(conn)
    now = utc_now_iso()
    source_system = str(source_system or "ibkr").strip().lower()
    provider = str(provider or "").strip() or source_system
    content_hash = content_hash or content_hash_for(
        source_system=source_system,
        provider=provider,
        headline=headline,
        body=body,
        url=url,
        published_at_utc=published_at_utc,
    )
    params = {
        "legacy_news_id": legacy_news_id,
        "source_system": source_system,
        "provider": provider,
        "publisher": str(publisher or ""),
        "provider_article_id": str(provider_article_id or ""),
        "headline": str(headline or ""),
        "body": body,
        "url": str(url or ""),
        "published_at_utc": str(published_at_utc or ""),
        "updated_at_utc": updated_at_utc,
        "received_at_utc": str(received_at_utc or now),
        "available_at_utc": str(available_at_utc or received_at_utc or now),
        "availability_quality": str(availability_quality or "unknown"),
        "ingest_mode": str(ingest_mode or "live"),
        "route_type": str(route_type or "ticker_specific"),
        "primary_route_symbol": str(primary_route_symbol or ""),
        "provider_primary_symbol": str(provider_primary_symbol or ""),
        "raw_json": json.dumps(raw_json or {}, ensure_ascii=False, default=str),
        "content_hash": content_hash,
        "lineage_schema_version": NEWS_LINEAGE_V2_SCHEMA_VERSION,
        "created_at": now,
    }
    conn.execute(
        """INSERT OR IGNORE INTO raw_news_articles(
            legacy_news_id,source_system,provider,publisher,provider_article_id,
            headline,body,url,published_at_utc,updated_at_utc,received_at_utc,
            available_at_utc,availability_quality,ingest_mode,route_type,
            primary_route_symbol,provider_primary_symbol,raw_json,content_hash,
            lineage_schema_version,created_at
        ) VALUES (
            :legacy_news_id,:source_system,:provider,:publisher,:provider_article_id,
            :headline,:body,:url,:published_at_utc,:updated_at_utc,:received_at_utc,
            :available_at_utc,:availability_quality,:ingest_mode,:route_type,
            :primary_route_symbol,:provider_primary_symbol,:raw_json,:content_hash,
            :lineage_schema_version,:created_at
        )""",
        params,
    )
    row = None
    if legacy_news_id is not None:
        row = conn.execute(
            "SELECT id FROM raw_news_articles WHERE legacy_news_id=?",
            (int(legacy_news_id),),
        ).fetchone()
    if row is None:
        row = conn.execute(
            """SELECT id FROM raw_news_articles
               WHERE source_system=? AND provider=? AND provider_article_id=? AND content_hash=?""",
            (params["source_system"], params["provider"], params["provider_article_id"], params["content_hash"]),
        ).fetchone()
    if row is None:
        raise RuntimeError("raw news article upsert did not return a row")
    return int(row["id"])


def upsert_raw_news_processing_status(
    conn: sqlite3.Connection,
    *,
    news_id: int,
    source_system: str,
    provider: str,
    processing_stage: str = NEWS_PROCESSING_STATUS_STAGE,
    processing_status: str,
    skip_reason: str | None = None,
    skip_flags: dict[str, Any] | list[Any] | None = None,
    derived_signal_id: int | None = None,
    event_cluster_id: int | None = None,
    classifier_version: str = "",
    mapper_version: str = NEWS_ASSET_MAPPER_VERSION,
    config_hash: str = "",
    build_id: str = DEFAULT_NEWS_LINEAGE_BUILD_ID,
    ensure_schema: bool = True,
) -> int:
    """Persist durable raw -> derived/skip status for closeout/audit replay."""

    if ensure_schema:
        ensure_news_lineage_v2_schema(conn)
    now = utc_now_iso()
    classifier_version = str(classifier_version or "")
    mapper_version = str(mapper_version or "")
    config_hash = str(config_hash or "")
    build_id = str(build_id or DEFAULT_NEWS_LINEAGE_BUILD_ID)
    payload = {
        "news_id": int(news_id),
        "source_system": str(source_system or "").strip().lower(),
        "provider": str(provider or "").strip().lower(),
        "processing_stage": str(processing_stage or NEWS_PROCESSING_STATUS_STAGE),
        "processing_status": str(processing_status or "unknown"),
        "skip_reason": skip_reason,
        "skip_flags_json": json.dumps(skip_flags or {}, ensure_ascii=False, sort_keys=True, default=str),
        "derived_signal_id": derived_signal_id,
        "event_cluster_id": event_cluster_id,
        "classifier_version": classifier_version,
        "mapper_version": mapper_version,
        "config_hash": config_hash,
        "build_id": build_id,
        "first_processed_at_utc": now,
        "last_processed_at_utc": now,
    }
    conn.execute(
        """INSERT INTO raw_news_processing_status(
            news_id,source_system,provider,processing_stage,processing_status,
            skip_reason,skip_flags_json,derived_signal_id,event_cluster_id,
            classifier_version,mapper_version,config_hash,build_id,
            first_processed_at_utc,last_processed_at_utc
        ) VALUES (
            :news_id,:source_system,:provider,:processing_stage,:processing_status,
            :skip_reason,:skip_flags_json,:derived_signal_id,:event_cluster_id,
            :classifier_version,:mapper_version,:config_hash,:build_id,
            :first_processed_at_utc,:last_processed_at_utc
        )
        ON CONFLICT(news_id,processing_stage,classifier_version,mapper_version,config_hash,build_id)
        DO UPDATE SET
            source_system=excluded.source_system,
            provider=excluded.provider,
            processing_status=excluded.processing_status,
            skip_reason=excluded.skip_reason,
            skip_flags_json=excluded.skip_flags_json,
            derived_signal_id=COALESCE(excluded.derived_signal_id, raw_news_processing_status.derived_signal_id),
            event_cluster_id=COALESCE(excluded.event_cluster_id, raw_news_processing_status.event_cluster_id),
            last_processed_at_utc=excluded.last_processed_at_utc""",
        payload,
    )
    row = conn.execute(
        """SELECT status_id
             FROM raw_news_processing_status
            WHERE news_id=?
              AND processing_stage=?
              AND classifier_version=?
              AND mapper_version=?
              AND config_hash=?
              AND build_id=?""",
        (int(news_id), payload["processing_stage"], classifier_version, mapper_version, config_hash, build_id),
    ).fetchone()
    if row is None:
        raise RuntimeError("raw news processing status upsert did not return a row")
    return int(row["status_id"])


def model_visible_at_for_news(
    *,
    source_system: str,
    provider: str,
    ingest_mode: str,
    published_at_utc: str,
    received_at_utc: str | None = None,
    latency_seconds: int = MARKETAUX_HISTORICAL_VISIBILITY_LATENCY_SECONDS,
) -> tuple[str, str]:
    """Return replay-visible time and availability quality for point-in-time features."""

    mode = str(ingest_mode or "").strip().lower()
    source = str(source_system or provider or "").strip().lower()
    published = _parse_dt(published_at_utc) or _parse_dt(received_at_utc) or datetime.now(UTC)
    received = _parse_dt(received_at_utc)
    if source == "marketaux" and mode != "live_polling":
        visible = published + timedelta(seconds=max(0, int(latency_seconds)))
        return visible.isoformat(), f"historical_estimated_marketaux_published_plus_{max(0, int(latency_seconds))}s"
    if received is not None:
        return received.isoformat(), "live_received"
    return (published + timedelta(seconds=max(0, int(latency_seconds)))).isoformat(), "estimated_published_plus_latency"


def upsert_news_visibility_policy(
    conn: sqlite3.Connection,
    *,
    news_id: int,
    source_system: str,
    provider: str,
    ingest_mode: str,
    published_at_utc: str,
    provider_updated_at_utc: str | None = None,
    received_at_utc: str | None = None,
    available_at_utc: str | None = None,
    latency_seconds: int = MARKETAUX_HISTORICAL_VISIBILITY_LATENCY_SECONDS,
    build_id: str = DEFAULT_NEWS_LINEAGE_BUILD_ID,
    config_hash: str = NEWS_VISIBILITY_POLICY_VERSION,
    ensure_schema: bool = True,
) -> int:
    """Persist the replay model-visible timestamp without mutating raw provider timestamps."""

    if ensure_schema:
        ensure_news_lineage_v2_schema(conn)
    model_visible_at_utc, availability_quality = model_visible_at_for_news(
        source_system=source_system,
        provider=provider,
        ingest_mode=ingest_mode,
        published_at_utc=published_at_utc,
        received_at_utc=received_at_utc,
        latency_seconds=latency_seconds,
    )
    now = utc_now_iso()
    payload = {
        "news_id": int(news_id),
        "source_system": str(source_system or "").strip().lower(),
        "provider": str(provider or "").strip().lower(),
        "ingest_mode": str(ingest_mode or ""),
        "published_at_utc": str(published_at_utc or ""),
        "provider_updated_at_utc": provider_updated_at_utc,
        "received_at_utc": received_at_utc,
        "available_at_utc": available_at_utc,
        "model_visible_at_utc": model_visible_at_utc,
        "visibility_policy": NEWS_VISIBILITY_POLICY_VERSION,
        "latency_seconds": max(0, int(latency_seconds)),
        "availability_quality": availability_quality,
        "build_id": str(build_id or DEFAULT_NEWS_LINEAGE_BUILD_ID),
        "config_hash": str(config_hash or NEWS_VISIBILITY_POLICY_VERSION),
        "created_at_utc": now,
    }
    conn.execute(
        """INSERT INTO news_visibility_policy(
            news_id,source_system,provider,ingest_mode,published_at_utc,
            provider_updated_at_utc,received_at_utc,available_at_utc,
            model_visible_at_utc,visibility_policy,latency_seconds,
            availability_quality,build_id,config_hash,created_at_utc
        ) VALUES (
            :news_id,:source_system,:provider,:ingest_mode,:published_at_utc,
            :provider_updated_at_utc,:received_at_utc,:available_at_utc,
            :model_visible_at_utc,:visibility_policy,:latency_seconds,
            :availability_quality,:build_id,:config_hash,:created_at_utc
        )
        ON CONFLICT(news_id,build_id,config_hash)
        DO UPDATE SET
            source_system=excluded.source_system,
            provider=excluded.provider,
            ingest_mode=excluded.ingest_mode,
            published_at_utc=excluded.published_at_utc,
            provider_updated_at_utc=excluded.provider_updated_at_utc,
            received_at_utc=excluded.received_at_utc,
            available_at_utc=excluded.available_at_utc,
            model_visible_at_utc=excluded.model_visible_at_utc,
            visibility_policy=excluded.visibility_policy,
            latency_seconds=excluded.latency_seconds,
            availability_quality=excluded.availability_quality""",
        payload,
    )
    row = conn.execute(
        "SELECT visibility_id FROM news_visibility_policy WHERE news_id=? AND build_id=? AND config_hash=?",
        (int(news_id), payload["build_id"], payload["config_hash"]),
    ).fetchone()
    if row is None:
        raise RuntimeError("news visibility policy upsert did not return a row")
    return int(row["visibility_id"])


def sync_legacy_news_article_to_raw(conn: sqlite3.Connection, legacy_news_id: int) -> int:
    ensure_news_lineage_v2_schema(conn)
    row = conn.execute("SELECT * FROM news_articles WHERE id=?", (int(legacy_news_id),)).fetchone()
    if row is None:
        raise ValueError(f"legacy news article {legacy_news_id} not found")
    provider = str(row["provider"] or "")
    extra_payload = _json_loads(row["extra_data"] if "extra_data" in row.keys() else "")
    if not isinstance(extra_payload, dict):
        extra_payload = {"legacy_extra_data": str(row["extra_data"] if "extra_data" in row.keys() else "")}
    source_system = str(extra_payload.get("source_system") or _source_system_for_provider(provider)).lower()
    primary_route_symbol = str(row["primary_symbol"] or "")
    route_type = _infer_route_type(primary_route_symbol, source_system=source_system, extra_payload=extra_payload)
    ingest_mode = _infer_ingest_mode(extra_payload, source_system=source_system)
    published_at = str(row["provider_published_at"] or row["published_at"] or "")
    received_at = str(row["received_at"] or "")
    available_at = str(row["source_accepted_at"] or row["first_seen_at"] or received_at or published_at)
    raw_id = upsert_raw_news_article(
        conn,
        legacy_news_id=int(legacy_news_id),
        source_system=source_system,
        provider=provider,
        publisher=str(extra_payload.get("publisher") or provider),
        provider_article_id=str(row["article_id"] or ""),
        headline=str(row["headline"] or ""),
        body=row["body"] if "body" in row.keys() else None,
        published_at_utc=published_at,
        received_at_utc=received_at,
        available_at_utc=available_at,
        availability_quality=str(row["timestamp_quality"] or "unknown"),
        ingest_mode=ingest_mode,
        route_type=route_type,
        primary_route_symbol=primary_route_symbol,
        provider_primary_symbol=str(extra_payload.get("provider_primary_symbol") or ""),
        raw_json={
            "legacy_news_id": int(legacy_news_id),
            "legacy_primary_symbol": primary_route_symbol,
            "legacy_extra_data": row["extra_data"] if "extra_data" in row.keys() else "",
            "source_table": "news_articles",
        },
    )
    enqueue_news_enrichment(conn, raw_id)
    return raw_id


def enqueue_news_enrichment(conn: sqlite3.Connection, news_id: int) -> None:
    ensure_news_lineage_v2_schema(conn)
    now = utc_now_iso()
    conn.execute(
        """INSERT OR IGNORE INTO news_enrichment_jobs(
            news_id,job_type,dedup_key,status,payload_json,created_at,updated_at
        ) VALUES (?, 'enrich_news_article', ?, 'queued', ?, ?, ?)""",
        (
            int(news_id),
            f"enrich:{int(news_id)}",
            json.dumps({"news_id": int(news_id), "schema": NEWS_LINEAGE_V2_SCHEMA_VERSION}, ensure_ascii=False),
            now,
            now,
        ),
    )


def record_provider_entities(
    conn: sqlite3.Connection,
    *,
    news_id: int,
    provider: str,
    entities: Iterable[dict[str, Any]] | None,
) -> int:
    ensure_news_lineage_v2_schema(conn)
    count = 0
    now = utc_now_iso()
    for entity in entities or ():
        if not isinstance(entity, dict):
            continue
        raw_symbol = str(entity.get("symbol") or entity.get("ticker") or "").upper()
        raw_name = str(entity.get("name") or entity.get("title") or "")
        entity_type = str(entity.get("type") or entity.get("entity_type") or "")
        if not raw_symbol and not raw_name:
            continue
        try:
            match_score = entity.get("match_score")
            match_score = float(match_score) if match_score is not None else None
        except (TypeError, ValueError):
            match_score = None
        try:
            sentiment_score = entity.get("sentiment_score")
            sentiment_score = float(sentiment_score) if sentiment_score is not None else None
        except (TypeError, ValueError):
            sentiment_score = None
        cursor = conn.execute(
            """INSERT OR IGNORE INTO provider_entities(
                news_id,provider,raw_symbol,raw_name,raw_exchange,raw_country,
                raw_industry,entity_type,match_score,sentiment_score,raw_json,created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                int(news_id),
                str(provider or ""),
                raw_symbol,
                raw_name,
                str(entity.get("exchange") or ""),
                str(entity.get("country") or ""),
                str(entity.get("industry") or ""),
                entity_type,
                match_score,
                sentiment_score,
                json.dumps(entity, ensure_ascii=False, default=str),
                now,
            ),
        )
        count += int(cursor.rowcount > 0)
    return count


def _mapping_confidence_value(mapping: NewsAssetMapping) -> float:
    return {
        "high": 0.88,
        "medium": 0.65,
        "low": 0.35,
    }.get(str(mapping.confidence or "").lower(), 0.25)


def _insert_asset_candidate(
    conn: sqlite3.Connection,
    *,
    mapping_run_id: int,
    mapping_run_key: str,
    news_id: int,
    event_cluster_id: int | None,
    symbol: str,
    con_id: int | None,
    asset_type: str | None,
    role: str,
    source: str,
    confidence: float,
    evidence: str,
    resolver_status: str,
    tradable_candidate: bool,
) -> None:
    normalized_symbol = str(symbol or "").strip().upper()
    if not normalized_symbol:
        return
    normalized_tradable = 1 if tradable_candidate else 0
    candidate_key = candidate_key_for_values(
        mapping_run_key=mapping_run_key,
        symbol=normalized_symbol,
        con_id=con_id,
        asset_type=asset_type,
        role=role,
        source=source,
        resolver_status=resolver_status,
    )
    conn.execute(
        """INSERT INTO news_asset_candidates(
            mapping_run_id,candidate_key,news_id,event_cluster_id,symbol,con_id,asset_type,
            role,source,confidence,evidence,resolver_status,tradable_candidate,created_at,last_seen_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(mapping_run_id,candidate_key) DO UPDATE SET
            event_cluster_id=COALESCE(news_asset_candidates.event_cluster_id, excluded.event_cluster_id),
            con_id=COALESCE(news_asset_candidates.con_id, excluded.con_id),
            asset_type=COALESCE(NULLIF(news_asset_candidates.asset_type,''), excluded.asset_type),
            confidence=MAX(news_asset_candidates.confidence, excluded.confidence),
            evidence=CASE
                WHEN excluded.evidence IS NULL OR excluded.evidence='' THEN news_asset_candidates.evidence
                WHEN news_asset_candidates.evidence IS NULL OR news_asset_candidates.evidence='' THEN excluded.evidence
                WHEN news_asset_candidates.evidence=excluded.evidence THEN news_asset_candidates.evidence
                WHEN instr(news_asset_candidates.evidence, excluded.evidence)>0 THEN news_asset_candidates.evidence
                ELSE news_asset_candidates.evidence || ' | ' || excluded.evidence
            END,
            tradable_candidate=MAX(news_asset_candidates.tradable_candidate, excluded.tradable_candidate),
            last_seen_at=excluded.last_seen_at""",
        (
            int(mapping_run_id),
            candidate_key,
            int(news_id),
            event_cluster_id,
            normalized_symbol,
            con_id,
            asset_type,
            role,
            source,
            max(0.0, min(1.0, float(confidence or 0.0))),
            evidence,
            resolver_status,
            normalized_tradable,
            utc_now_iso(),
            utc_now_iso(),
        ),
    )


def store_article_asset_mapping(
    conn: sqlite3.Connection,
    news_id: int,
    *,
    build_id: str = DEFAULT_NEWS_LINEAGE_BUILD_ID,
    mapper_version: str = NEWS_ASSET_MAPPER_VERSION,
    resolver_version: str = NEWS_RESOLVER_VERSION,
    security_master_version: str = SECURITY_MASTER_VERSION,
    config_hash: str = "",
) -> int:
    """Run and store article-level asset mapping for one raw news article.

    `news_id` is the M251 `raw_news_articles.id`.  `event_cluster_id` is left
    nullable in this foundation stage; M252 can backfill cluster-scope mappings.
    """

    ensure_news_lineage_v2_schema(conn)
    row = conn.execute("SELECT * FROM raw_news_articles WHERE id=?", (int(news_id),)).fetchone()
    if row is None:
        raise ValueError(f"raw news article {news_id} not found")
    input_hash = str(row["content_hash"] or "")
    normalized_build_id = str(build_id or DEFAULT_NEWS_LINEAGE_BUILD_ID)
    mapping_run_key = mapping_run_key_for_values(
        build_id=normalized_build_id,
        news_id=int(news_id),
        event_cluster_id=None,
        mapping_scope="article",
        mapper_version=mapper_version,
        resolver_version=resolver_version,
        security_master_version=security_master_version,
        input_hash=input_hash,
        config_hash=config_hash,
    )
    existing = conn.execute(
        """SELECT mapping_run_id FROM news_asset_mapping_runs
           WHERE build_id=? AND mapping_run_key=?
           ORDER BY CASE WHEN event_cluster_id IS NOT NULL THEN 0 ELSE 1 END,
                    mapping_run_id ASC
           LIMIT 1""",
        (
            normalized_build_id,
            mapping_run_key,
        ),
    ).fetchone()
    if existing is not None:
        conn.execute(
            "UPDATE news_asset_mapping_runs SET last_seen_at=? WHERE mapping_run_id=?",
            (utc_now_iso(), int(existing["mapping_run_id"])),
        )
        return int(existing["mapping_run_id"])

    raw_payload = _json_loads(row["raw_json"])
    legacy_extra = raw_payload.get("legacy_extra_data") if isinstance(raw_payload, dict) else None
    mapping = infer_news_assets(
        str(row["headline"] or ""),
        str(row["body"] or ""),
        primary_symbol=str(row["primary_route_symbol"] or ""),
        provider=str(row["provider"] or ""),
        extra_data=legacy_extra or raw_payload,
    )
    cursor = conn.execute(
        """INSERT OR IGNORE INTO news_asset_mapping_runs(
            build_id,mapping_run_key,news_id,event_cluster_id,mapping_scope,mapper_version,resolver_version,
            security_master_version,input_hash,config_hash,created_at,last_seen_at
        ) VALUES (?, ?, ?, NULL, 'article', ?, ?, ?, ?, ?, ?, ?)""",
        (
            normalized_build_id,
            mapping_run_key,
            int(news_id),
            mapper_version,
            resolver_version,
            security_master_version,
            input_hash,
            config_hash,
            utc_now_iso(),
            utc_now_iso(),
        ),
    )
    if cursor.rowcount == 0:
        row_existing = conn.execute(
            """SELECT mapping_run_id FROM news_asset_mapping_runs
               WHERE build_id=? AND mapping_run_key=?
               ORDER BY CASE WHEN event_cluster_id IS NOT NULL THEN 0 ELSE 1 END,
                        mapping_run_id ASC
               LIMIT 1""",
            (
                normalized_build_id,
                mapping_run_key,
            ),
        ).fetchone()
        if row_existing is None:
            raise RuntimeError(f"failed to create or retrieve asset mapping run for news_id={news_id}")
        conn.execute(
            "UPDATE news_asset_mapping_runs SET last_seen_at=? WHERE mapping_run_id=?",
            (utc_now_iso(), int(row_existing["mapping_run_id"])),
        )
        return int(row_existing["mapping_run_id"])
    mapping_run_id = int(cursor.lastrowid)
    route_type = str(row["route_type"] or "")
    primary_route_symbol = str(row["primary_route_symbol"] or "").strip().upper()
    if route_type == "ticker_specific" and primary_route_symbol and primary_route_symbol != "UNKNOWN":
        _insert_asset_candidate(
            conn,
            mapping_run_id=mapping_run_id,
            mapping_run_key=mapping_run_key,
            news_id=int(news_id),
            event_cluster_id=None,
            symbol=primary_route_symbol,
            con_id=None,
            asset_type=None,
            role="route_only",
            source="ibkr_route",
            confidence=0.25,
            evidence="ticker-specific subscription route; not content entity by itself",
            resolver_status="resolved",
            tradable_candidate=False,
        )

    reason_lower = str(mapping.reason or "").lower()
    route_symbol_content_confirmed = (
        reason_lower.startswith("direct ticker")
        or "company alias" in reason_lower
    )
    route_only_mapping = (
        route_type == "ticker_specific"
        and primary_route_symbol
        and not route_symbol_content_confirmed
    )
    mapping_confidence = _mapping_confidence_value(mapping)
    inserted_symbols: set[str] = set()
    for symbol in mapping.target_candidates:
        normalized = str(symbol or "").upper()
        if route_only_mapping and normalized == primary_route_symbol:
            continue
        role = "primary" if normalized in set(mapping.inferred_symbols) else "proxy"
        source = "content_entity" if role == "primary" else "theme_proxy"
        resolver_status = "resolved" if role == "primary" else "proxy_only"
        _insert_asset_candidate(
            conn,
            mapping_run_id=mapping_run_id,
            mapping_run_key=mapping_run_key,
            news_id=int(news_id),
            event_cluster_id=None,
            symbol=normalized,
            con_id=None,
            asset_type=None,
            role=role,
            source=source,
            confidence=mapping_confidence,
            evidence=mapping.reason,
            resolver_status=resolver_status,
            tradable_candidate=True,
        )
        inserted_symbols.add(normalized)

    for symbol in getattr(mapping, "context_assets", ()) or ():
        normalized = str(symbol or "").upper()
        if normalized in inserted_symbols:
            continue
        _insert_asset_candidate(
            conn,
            mapping_run_id=mapping_run_id,
            mapping_run_key=mapping_run_key,
            news_id=int(news_id),
            event_cluster_id=None,
            symbol=normalized,
            con_id=None,
            asset_type=None,
            role="context_only",
            source="fallback_context",
            confidence=0.15,
            evidence=mapping.reason,
            resolver_status="proxy_only",
            tradable_candidate=False,
        )

    provider_rows = conn.execute(
        """SELECT * FROM provider_entities
           WHERE news_id=? AND (raw_symbol<>'' OR raw_name<>'')""",
        (int(news_id),),
    ).fetchall()
    for provider_entity in provider_rows:
        symbol = str(provider_entity["raw_symbol"] or "").upper()
        entity_type = str(provider_entity["entity_type"] or "").lower()
        if not symbol or entity_type not in {"equity", "etf", "index", "stock", "fund"}:
            continue
        try:
            match_score = float(provider_entity["match_score"]) if provider_entity["match_score"] is not None else 0.55
        except (TypeError, ValueError):
            match_score = 0.55
        confidence = max(0.35, min(0.95, 0.45 + match_score * 0.5))
        _insert_asset_candidate(
            conn,
            mapping_run_id=mapping_run_id,
            mapping_run_key=mapping_run_key,
            news_id=int(news_id),
            event_cluster_id=None,
            symbol=symbol,
            con_id=None,
            asset_type=entity_type or None,
            role="primary" if match_score >= 0.70 else "secondary",
            source=f"{str(provider_entity['provider'] or '').lower()}_entity",
            confidence=confidence,
            evidence=f"provider entity {provider_entity['raw_symbol']} {provider_entity['raw_name']}".strip(),
            resolver_status="unresolved",
            tradable_candidate=False,
        )
    return mapping_run_id


def link_external_signal_to_source_news(
    conn: sqlite3.Connection,
    *,
    signal_id: int,
    news_id: int,
    link_type: str = "primary_source",
    confidence: float = 1.0,
) -> None:
    ensure_news_lineage_v2_schema(conn)
    conn.execute(
        """INSERT OR IGNORE INTO external_signal_source_links(
            signal_id,news_id,link_type,confidence,created_at
        ) VALUES (?, ?, ?, ?, ?)""",
        (int(signal_id), int(news_id), str(link_type or "primary_source"), float(confidence), utc_now_iso()),
    )


def _parse_dt(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _floor_to_minutes(value: datetime, minutes: int) -> datetime:
    minutes = max(1, int(minutes))
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    total_minutes = int((value - epoch).total_seconds() // 60)
    bucket_minutes = total_minutes - (total_minutes % minutes)
    return epoch + timedelta(minutes=bucket_minutes)


def _headline_tokens(text: str) -> tuple[str, ...]:
    return tuple(
        token
        for token in re.findall(r"[a-z0-9]{3,}", str(text or "").lower())
        if token not in _STOP_WORDS
    )


def _headline_fingerprint(text: str, *, max_tokens: int = 8) -> str:
    tokens = _headline_tokens(text)
    return "-".join(tokens[:max_tokens]) or "no-fingerprint"


def _provider_family(provider: str, source_system: str = "") -> str:
    text = f"{provider} {source_system}".strip().lower()
    if not text:
        return "unknown"
    if text.startswith("dj") or "dow" in text or "djnl" in text:
        return "dow_jones"
    if text.startswith("bz") or "benzinga" in text:
        return "benzinga"
    if "marketaux" in text:
        return "marketaux"
    if "emergency" in text or "breaking" in text:
        return "emergency_breaking_news"
    return re.sub(r"[^a-z0-9_]+", "_", text).strip("_") or "unknown"


def _provider_weight(provider_family: str) -> float:
    return {
        "dow_jones": 0.92,
        "benzinga": 0.78,
        "emergency_breaking_news": 0.62,
        "marketaux": 0.46,
    }.get(str(provider_family or ""), 0.30)


def _direction_from_text(text: str, sentiment_score: float | None = None) -> str:
    if sentiment_score is not None:
        if sentiment_score >= 0.25:
            return "positive"
        if sentiment_score <= -0.25:
            return "negative"
    positive = bool(_POSITIVE_RE.search(text or ""))
    negative = bool(_NEGATIVE_RE.search(text or ""))
    if positive and not negative:
        return "positive"
    if negative and not positive:
        return "negative"
    if positive and negative:
        return "mixed"
    return "neutral"


def _event_role_from_text(text: str) -> tuple[str, str, float]:
    value = str(text or "")
    rules: tuple[tuple[str, str, float, str], ...] = (
        ("analyst_action", "company_event", 0.86, r"\b(upgrade|downgrade|maintains|initiates|price target|pt raised|pt lowered|rating)\b"),
        ("earnings_guidance", "company_event", 0.84, r"\b(earnings|eps|revenue|guidance|outlook|forecast|beats estimates|misses estimates)\b"),
        ("macro_rates", "macro_event", 0.82, r"\b(fed|fomc|powell|rate cut|rate hike|rates|treasury|yield|inflation|cpi|pce|payrolls|jobs report)\b"),
        ("geopolitical", "macro_event", 0.80, r"\b(iran|israel|gaza|houthi|red sea|hormuz|taiwan|china|russia|ukraine|north korea|nato|war|missile|ceasefire|sanction)\b"),
        ("semiconductor_ai", "theme_event", 0.76, r"\b(ai|artificial intelligence|semiconductor|chip|chips|gpu|hbm|data center|datacenter|nvidia|tsmc|micron|broadcom)\b"),
        ("energy_oil", "theme_event", 0.74, r"\b(oil|crude|brent|wti|opec|energy|gasoline|distillates|xle|uso|exxon|chevron)\b"),
        ("market_color", "market_structure", 0.58, r"\b(futures|nasdaq|dow jones|s&p 500|wall street|market roundup|stocks rise|stocks fall)\b"),
    )
    for event_role, family, confidence, pattern in rules:
        if re.search(pattern, value, re.I):
            return event_role, family, confidence
    return "unknown", "unknown", 0.35


def _candidate_symbols_for_cluster(conn: sqlite3.Connection, news_id: int) -> tuple[str, ...]:
    rows = conn.execute(
        """SELECT symbol,role,confidence,resolver_status,tradable_candidate
           FROM news_asset_candidates
           WHERE news_id=?
           ORDER BY tradable_candidate DESC,confidence DESC,id""",
        (int(news_id),),
    ).fetchall()
    preferred: list[str] = []
    fallback: list[str] = []
    for row in rows:
        symbol = str(row["symbol"] or "").upper()
        role = str(row["role"] or "")
        if not symbol:
            continue
        if role in {"primary", "secondary"} and float(row["confidence"] or 0.0) >= 0.50:
            preferred.append(symbol)
        elif role == "proxy" and int(row["tradable_candidate"] or 0) == 1:
            fallback.append(symbol)
    return tuple(dict.fromkeys((*preferred, *fallback)))[:8]


def _cluster_key_for_article(
    conn: sqlite3.Connection,
    raw: sqlite3.Row,
    *,
    bucket_minutes: int = 60,
) -> tuple[str, str, str, datetime, tuple[str, ...], str]:
    published = _parse_dt(raw["published_at_utc"]) or _parse_dt(raw["received_at_utc"]) or datetime.now(UTC)
    bucket = _floor_to_minutes(published, bucket_minutes)
    text = f"{raw['headline'] or ''} {raw['body'] or ''}"[:20000]
    event_role, role_family, _ = _event_role_from_text(text)
    event_class = f"{role_family}.{event_role}" if role_family != "unknown" else "unknown"
    entities = _candidate_symbols_for_cluster(conn, int(raw["id"]))
    entity_part = ",".join(entities) if entities else "NO_ENTITY"
    fingerprint = _headline_fingerprint(str(raw["headline"] or ""))
    # Use entity + role + time as the main event boundary.  The fingerprint is
    # retained in the hash payload but intentionally shortened so related BZ/DJ
    # headlines can still cluster when they describe the same entity/event role.
    key_payload = {
        "event_class": event_class,
        "entities": entity_part,
        "bucket": bucket.isoformat(),
    }
    cluster_key = f"{NEWS_EVENT_CLUSTERER_VERSION}:{_stable_hash(key_payload)[:24]}"
    return cluster_key, event_class, event_role, bucket, entities, fingerprint


def assign_article_to_event_cluster(
    conn: sqlite3.Connection,
    news_id: int,
    *,
    bucket_minutes: int = 60,
    recompute_roles: bool = True,
) -> int:
    ensure_news_lineage_v2_schema(conn)
    store_article_asset_mapping(conn, int(news_id))
    raw = conn.execute("SELECT * FROM raw_news_articles WHERE id=?", (int(news_id),)).fetchone()
    if raw is None:
        raise ValueError(f"raw news article {news_id} not found")
    cluster_key, event_class, event_role, bucket, entities, fingerprint = _cluster_key_for_article(
        conn,
        raw,
        bucket_minutes=bucket_minutes,
    )
    published = _parse_dt(raw["published_at_utc"]) or _parse_dt(raw["received_at_utc"]) or datetime.now(UTC)
    now = utc_now_iso()
    conn.execute(
        """INSERT OR IGNORE INTO news_event_clusters(
            cluster_key,primary_headline,first_seen_at_utc,last_seen_at_utc,
            canonical_entities_json,event_class,provider_count,article_count,
            cluster_status,clusterer_version,created_at,updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, 0, 0, 'candidate', ?, ?, ?)""",
        (
            cluster_key,
            str(raw["headline"] or "")[:1000],
            published.isoformat(),
            published.isoformat(),
            json.dumps(list(entities), ensure_ascii=False),
            event_class,
            NEWS_EVENT_CLUSTERER_VERSION,
            now,
            now,
        ),
    )
    cluster = conn.execute(
        "SELECT * FROM news_event_clusters WHERE cluster_key=?",
        (cluster_key,),
    ).fetchone()
    if cluster is None:
        raise RuntimeError("event cluster insert did not return a row")
    cluster_id = int(cluster["event_cluster_id"])
    has_primary_link = (
        conn.execute(
            "SELECT 1 FROM news_article_cluster_links WHERE event_cluster_id=? AND is_primary_source=1 LIMIT 1",
            (cluster_id,),
        ).fetchone()
        is not None
    )
    link_type = "supporting_source" if has_primary_link else "primary_source"
    conn.execute(
        """INSERT OR IGNORE INTO news_article_cluster_links(
            news_id,event_cluster_id,link_confidence,link_reason,
            link_type,is_primary_source,link_role,clusterer_version,created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            int(news_id),
            cluster_id,
            0.92 if entities else 0.55,
            f"{event_role}; bucket={bucket.isoformat()}; fp={fingerprint}",
            link_type,
            0 if has_primary_link else 1,
            link_type,
            NEWS_EVENT_CLUSTERER_VERSION,
            now,
        ),
    )
    conn.execute(
        """UPDATE news_asset_mapping_runs
           SET event_cluster_id=?
           WHERE news_id=? AND mapping_scope='article' AND event_cluster_id IS NULL""",
        (cluster_id, int(news_id)),
    )
    conn.execute(
        """UPDATE news_asset_candidates
           SET event_cluster_id=?
           WHERE news_id=? AND event_cluster_id IS NULL""",
        (cluster_id, int(news_id)),
    )
    _refresh_cluster_counts(conn, cluster_id)
    if recompute_roles:
        recompute_event_cluster_roles(conn, cluster_id)
    return cluster_id


def _refresh_cluster_counts(conn: sqlite3.Connection, event_cluster_id: int) -> None:
    raw_rows = conn.execute(
        """SELECT r.*
           FROM raw_news_articles r
           JOIN news_article_cluster_links l ON l.news_id=r.id
           WHERE l.event_cluster_id=?""",
        (int(event_cluster_id),),
    ).fetchall()
    families = {
        _provider_family(str(row["provider"] or ""), str(row["source_system"] or ""))
        for row in raw_rows
    }
    providers = sorted(family for family in families if family and family != "unknown")
    first_seen = min((_parse_dt(row["published_at_utc"]) or _parse_dt(row["received_at_utc"]) or datetime.now(UTC)) for row in raw_rows) if raw_rows else datetime.now(UTC)
    last_seen = max((_parse_dt(row["published_at_utc"]) or _parse_dt(row["received_at_utc"]) or datetime.now(UTC)) for row in raw_rows) if raw_rows else first_seen
    entity_rows = conn.execute(
        """SELECT DISTINCT symbol
           FROM news_asset_candidates
           WHERE event_cluster_id=? AND role IN ('primary','secondary','proxy')""",
        (int(event_cluster_id),),
    ).fetchall()
    entities = [str(row["symbol"] or "").upper() for row in entity_rows if str(row["symbol"] or "").strip()]
    conn.execute(
        """UPDATE news_event_clusters
           SET provider_count=?,article_count=?,first_seen_at_utc=?,last_seen_at_utc=?,
               canonical_entities_json=?,updated_at=?
           WHERE event_cluster_id=?""",
        (
            len(providers),
            len(raw_rows),
            first_seen.isoformat(),
            last_seen.isoformat(),
            json.dumps(sorted(dict.fromkeys(entities)), ensure_ascii=False),
            utc_now_iso(),
            int(event_cluster_id),
        ),
    )


def _raw_cluster_items(conn: sqlite3.Connection, event_cluster_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT r.*
           FROM raw_news_articles r
           JOIN news_article_cluster_links l ON l.news_id=r.id
           WHERE l.event_cluster_id=?
           ORDER BY COALESCE(NULLIF(r.published_at_utc,''),r.received_at_utc),r.id""",
        (int(event_cluster_id),),
    ).fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        family = _provider_family(str(row["provider"] or ""), str(row["source_system"] or ""))
        text = f"{row['headline'] or ''} {row['body'] or ''}"[:20000]
        event_role, role_family, role_confidence = _event_role_from_text(text)
        items.append(
            {
                "item_uid": f"raw:{int(row['id'])}",
                "item_type": "raw_news",
                "news_id": int(row["id"]),
                "signal_id": None,
                "provider_family": family,
                "source_system": str(row["source_system"] or ""),
                "provider": str(row["provider"] or ""),
                "title": str(row["headline"] or ""),
                "published_at_utc": str(row["published_at_utc"] or row["received_at_utc"] or ""),
                "direction": _direction_from_text(text),
                "role": event_role,
                "role_family": role_family,
                "role_confidence": role_confidence,
                "included_in_confirmation": 1,
                "duplicate_of_news_id": None,
                "double_count_status": "canonical_source",
            }
        )
    return items


def _external_signal_items(conn: sqlite3.Connection, event_cluster_id: int, source_news_ids: set[int]) -> list[dict[str, Any]]:
    if not _table_exists(conn, "external_market_signals"):
        return []
    signal_rows = conn.execute(
        """SELECT DISTINCT s.*
           FROM external_market_signals s
           LEFT JOIN external_signal_source_links l ON l.signal_id=s.id
           WHERE s.event_cluster_id=?
              OR s.source_news_id IN (SELECT news_id FROM news_article_cluster_links WHERE event_cluster_id=?)
              OR l.news_id IN (SELECT news_id FROM news_article_cluster_links WHERE event_cluster_id=?)
           ORDER BY COALESCE(NULLIF(s.published_at,''),s.created_at),s.id""",
        (int(event_cluster_id), int(event_cluster_id), int(event_cluster_id)),
    ).fetchall()
    items: list[dict[str, Any]] = []
    for row in signal_rows:
        signal_id = int(row["id"])
        linked_rows = conn.execute(
            """SELECT news_id FROM external_signal_source_links WHERE signal_id=?""",
            (signal_id,),
        ).fetchall()
        linked_news_ids = {
            int(link["news_id"])
            for link in linked_rows
            if link["news_id"] is not None
        }
        if row["source_news_id"] is not None:
            linked_news_ids.add(int(row["source_news_id"]))
        duplicate_news_ids = sorted(linked_news_ids.intersection(source_news_ids))
        duplicate_of = duplicate_news_ids[0] if duplicate_news_ids else None
        source = str(row["source"] or "")
        family = _provider_family(source, source)
        text = str(row["title"] or "")
        score = row["sentiment_score"] if "sentiment_score" in row.keys() else None
        try:
            sentiment_score = float(score) if score is not None else None
        except (TypeError, ValueError):
            sentiment_score = None
        event_role, role_family, role_confidence = _event_role_from_text(text)
        derived_duplicate = duplicate_of is not None
        items.append(
            {
                "item_uid": f"signal:{signal_id}",
                "item_type": "external_signal",
                "news_id": duplicate_of,
                "signal_id": signal_id,
                "provider_family": family,
                "source_system": "derived_signal",
                "provider": source,
                "title": text,
                "published_at_utc": str(row["published_at"] or ""),
                "direction": _direction_from_text(text, sentiment_score),
                "role": event_role,
                "role_family": role_family,
                "role_confidence": role_confidence,
                "included_in_confirmation": 0 if derived_duplicate else 1,
                "duplicate_of_news_id": duplicate_of,
                "double_count_status": "derived_duplicate" if derived_duplicate else "independent_signal",
            }
        )
    return items


def recompute_event_cluster_roles(conn: sqlite3.Connection, event_cluster_id: int) -> dict[str, Any]:
    ensure_news_lineage_v2_schema(conn)
    raw_items = _raw_cluster_items(conn, int(event_cluster_id))
    source_news_ids = {int(item["news_id"]) for item in raw_items if item.get("news_id") is not None}
    signal_items = _external_signal_items(conn, int(event_cluster_id), source_news_ids)
    all_items = [*raw_items, *signal_items]
    conn.execute("DELETE FROM news_event_cluster_items WHERE event_cluster_id=?", (int(event_cluster_id),))
    now = utc_now_iso()
    for item in all_items:
        conn.execute(
            """INSERT OR REPLACE INTO news_event_cluster_items(
                event_cluster_id,item_uid,item_type,news_id,signal_id,provider_family,
                source_system,provider,title,published_at_utc,direction,role,
                included_in_confirmation,duplicate_of_news_id,double_count_status,created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                int(event_cluster_id),
                item["item_uid"],
                item["item_type"],
                item.get("news_id"),
                item.get("signal_id"),
                item["provider_family"],
                item["source_system"],
                item["provider"],
                item["title"][:1000],
                item["published_at_utc"],
                item["direction"],
                item["role"],
                int(item["included_in_confirmation"]),
                item.get("duplicate_of_news_id"),
                item["double_count_status"],
                now,
            ),
        )

    confirmation_items = [item for item in all_items if int(item.get("included_in_confirmation") or 0) == 1]
    families = sorted(
        dict.fromkeys(
            str(item["provider_family"] or "unknown")
            for item in confirmation_items
            if str(item["provider_family"] or "unknown") != "unknown"
        )
    )
    weights = [_provider_weight(family) for family in families]
    provider_confirmation_score = round(min(1.0, sum(weights) / 1.55), 4) if weights else 0.0
    direction_counts = Counter(str(item.get("direction") or "neutral") for item in confirmation_items)
    positive = direction_counts.get("positive", 0)
    negative = direction_counts.get("negative", 0)
    mixed = direction_counts.get("mixed", 0)
    provider_conflict_score = 0.0
    if positive and negative:
        provider_conflict_score = 1.0
    elif mixed and (positive or negative):
        provider_conflict_score = 0.55
    marketaux_noise_hits = [
        item
        for item in confirmation_items
        if item["provider_family"] == "marketaux" and _MARKETAUX_NOISE_RE.search(str(item.get("title") or ""))
    ]
    marketaux_only = bool(confirmation_items) and set(families) == {"marketaux"}
    marketaux_noise_score = round(len(marketaux_noise_hits) / max(1, len(confirmation_items)), 4)
    if marketaux_only and marketaux_noise_score >= 0.5:
        provider_agreement = "marketaux_noise_filtered"
    elif provider_conflict_score >= 0.5:
        provider_agreement = "conflict"
    elif len(families) >= 2:
        provider_agreement = "confirmed"
    elif len(families) == 1:
        provider_agreement = "single_source"
    else:
        provider_agreement = "no_evidence"
    if positive > negative:
        direction = "positive"
    elif negative > positive:
        direction = "negative"
    elif mixed:
        direction = "mixed"
    else:
        direction = "neutral"
    role_counter = Counter(str(item.get("role") or "unknown") for item in confirmation_items)
    role_family_counter = Counter(str(item.get("role_family") or "unknown") for item in confirmation_items)
    event_role = role_counter.most_common(1)[0][0] if role_counter else "unknown"
    role_family = role_family_counter.most_common(1)[0][0] if role_family_counter else "unknown"
    role_confidence_values = [float(item.get("role_confidence") or 0.0) for item in confirmation_items]
    role_confidence = round(sum(role_confidence_values) / max(1, len(role_confidence_values)), 4)
    asset_rows = conn.execute(
        """SELECT DISTINCT symbol FROM news_asset_candidates
           WHERE event_cluster_id=? AND role IN ('primary','secondary','proxy')""",
        (int(event_cluster_id),),
    ).fetchall()
    shared_assets = sorted(
        dict.fromkeys(str(row["symbol"] or "").upper() for row in asset_rows if str(row["symbol"] or "").strip())
    )
    external_signal_ids = sorted(
        int(item["signal_id"])
        for item in signal_items
        if item.get("signal_id") is not None
    )
    suppressed_signal_ids = sorted(
        int(item["signal_id"])
        for item in signal_items
        if item.get("signal_id") is not None and item.get("double_count_status") == "derived_duplicate"
    )
    payload = {
        "event_cluster_id": int(event_cluster_id),
        "event_role": event_role,
        "role_family": role_family,
        "direction": direction,
        "role_confidence": role_confidence,
        "provider_agreement": provider_agreement,
        "provider_confirmation_score": provider_confirmation_score,
        "provider_conflict_score": provider_conflict_score,
        "marketaux_noise_score": marketaux_noise_score,
        "provider_count": len(families),
        "article_count": len(raw_items),
        "source_news_count": len(source_news_ids),
        "external_signal_count": len(signal_items),
        "provider_families": families,
        "direction_counts": dict(direction_counts),
        "shared_assets": shared_assets,
        "source_news_ids": sorted(source_news_ids),
        "external_signal_ids": external_signal_ids,
        "double_count_suppressed_signal_ids": suppressed_signal_ids,
    }
    conn.execute(
        """INSERT INTO news_event_cluster_roles(
            event_cluster_id,event_role,role_family,direction,role_confidence,
            provider_agreement,provider_confirmation_score,provider_conflict_score,
            marketaux_noise_score,provider_count,article_count,source_news_count,
            external_signal_count,provider_families_json,direction_counts_json,
            shared_assets_json,source_news_ids_json,external_signal_ids_json,
            double_count_suppressed_signal_ids_json,clusterer_version,role_version,
            dedup_version,created_at,updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(event_cluster_id) DO UPDATE SET
            event_role=excluded.event_role,
            role_family=excluded.role_family,
            direction=excluded.direction,
            role_confidence=excluded.role_confidence,
            provider_agreement=excluded.provider_agreement,
            provider_confirmation_score=excluded.provider_confirmation_score,
            provider_conflict_score=excluded.provider_conflict_score,
            marketaux_noise_score=excluded.marketaux_noise_score,
            provider_count=excluded.provider_count,
            article_count=excluded.article_count,
            source_news_count=excluded.source_news_count,
            external_signal_count=excluded.external_signal_count,
            provider_families_json=excluded.provider_families_json,
            direction_counts_json=excluded.direction_counts_json,
            shared_assets_json=excluded.shared_assets_json,
            source_news_ids_json=excluded.source_news_ids_json,
            external_signal_ids_json=excluded.external_signal_ids_json,
            double_count_suppressed_signal_ids_json=excluded.double_count_suppressed_signal_ids_json,
            clusterer_version=excluded.clusterer_version,
            role_version=excluded.role_version,
            dedup_version=excluded.dedup_version,
            updated_at=excluded.updated_at""",
        (
            int(event_cluster_id),
            event_role,
            role_family,
            direction,
            role_confidence,
            provider_agreement,
            provider_confirmation_score,
            provider_conflict_score,
            marketaux_noise_score,
            len(families),
            len(raw_items),
            len(source_news_ids),
            len(signal_items),
            json.dumps(families, ensure_ascii=False),
            json.dumps(dict(direction_counts), ensure_ascii=False),
            json.dumps(shared_assets, ensure_ascii=False),
            json.dumps(sorted(source_news_ids), ensure_ascii=False),
            json.dumps(external_signal_ids, ensure_ascii=False),
            json.dumps(suppressed_signal_ids, ensure_ascii=False),
            NEWS_EVENT_CLUSTERER_VERSION,
            NEWS_CLUSTER_ROLE_VERSION,
            NEWS_CLUSTER_DEDUP_VERSION,
            now,
            now,
        ),
    )
    conn.execute(
        """INSERT INTO news_event_classifications(
            event_cluster_id,news_id,classification_scope,event_class,region,theme,
            direction,severity,sentiment_score,classification_confidence,
            classifier_name,classifier_version,created_at
        ) VALUES (?, NULL, 'cluster', ?, 'unknown', ?, ?, ?, NULL, ?, 'm252_cluster_role_builder', ?, ?)""",
        (
            int(event_cluster_id),
            f"{role_family}.{event_role}" if role_family != "unknown" else "unknown",
            role_family,
            direction,
            provider_confirmation_score,
            role_confidence,
            NEWS_CLUSTER_ROLE_VERSION,
            now,
        ),
    )
    gate_status = "review"
    if provider_agreement == "confirmed" and provider_confirmation_score >= 0.75 and provider_conflict_score < 0.5:
        gate_status = "watch"
    elif provider_agreement == "conflict":
        gate_status = "review_conflict"
    elif provider_agreement == "marketaux_noise_filtered":
        gate_status = "ignore_noise"
    conn.execute(
        """INSERT INTO news_event_gates(
            event_cluster_id,news_id,gate_scope,gate_status,gate_score,blocked_reason,gate_version,created_at
        ) VALUES (?, NULL, 'cluster', ?, ?, ?, ?, ?)""",
        (
            int(event_cluster_id),
            gate_status,
            max(0.0, provider_confirmation_score - provider_conflict_score),
            provider_agreement,
            NEWS_CLUSTER_DEDUP_VERSION,
            now,
        ),
    )
    canonical = upsert_canonical_classification_gate_for_cluster(conn, int(event_cluster_id))
    payload["canonical_classification_key"] = canonical["classification_key"]
    payload["canonical_gate_key"] = canonical["gate_key"]
    return payload


def assign_unclustered_articles(
    conn: sqlite3.Connection,
    *,
    limit: int | None = None,
    bucket_minutes: int = 60,
    start: str | None = None,
    end: str | None = None,
) -> dict[str, Any]:
    ensure_news_lineage_v2_schema(conn)
    where = ["l.news_id IS NULL"]
    params_list: list[Any] = []
    time_expr = "COALESCE(NULLIF(r.published_at_utc,''),r.received_at_utc,r.created_at)"
    if start:
        where.append(f"{time_expr} >= ?")
        params_list.append(str(start))
    if end:
        where.append(f"{time_expr} <= ?")
        params_list.append(str(end))
    sql = """
        SELECT r.id
        FROM raw_news_articles r
        LEFT JOIN news_article_cluster_links l ON l.news_id=r.id
        WHERE {where_sql}
        ORDER BY COALESCE(NULLIF(r.published_at_utc,''),r.received_at_utc),r.id
    """.format(where_sql=" AND ".join(where))
    params: tuple[Any, ...] = tuple(params_list)
    if limit is not None:
        sql += " LIMIT ?"
        params = (*params, max(0, int(limit)))
    rows = conn.execute(sql, params).fetchall()
    assigned = 0
    clusters: set[int] = set()
    for row in rows:
        cluster_id = assign_article_to_event_cluster(
            conn,
            int(row["id"]),
            bucket_minutes=bucket_minutes,
            recompute_roles=False,
        )
        assigned += 1
        clusters.add(cluster_id)
    for cluster_id in sorted(clusters):
        recompute_event_cluster_roles(conn, cluster_id)
    return {
        "assigned_articles": assigned,
        "clusters_touched": len(clusters),
        "cluster_ids": sorted(clusters),
    }


def _legacy_news_ids_for_rebuild(
    conn: sqlite3.Connection,
    *,
    start: str | None = None,
    end: str | None = None,
    limit: int | None = None,
) -> list[int]:
    if not _table_exists(conn, "news_articles"):
        return []
    where: list[str] = []
    params: list[Any] = []
    time_expr = "COALESCE(provider_published_at,published_at,received_at,created_at)"
    if start:
        where.append(f"{time_expr} >= ?")
        params.append(str(start))
    if end:
        where.append(f"{time_expr} <= ?")
        params.append(str(end))
    sql = f"SELECT id FROM news_articles"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY {time_expr}, id"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(max(0, int(limit)))
    return [int(row["id"]) for row in conn.execute(sql, params).fetchall()]


def _raw_news_rebuild_scope_count(
    conn: sqlite3.Connection,
    *,
    start: str | None = None,
    end: str | None = None,
) -> int:
    ensure_news_lineage_v2_schema(conn)
    where: list[str] = []
    params: list[Any] = []
    time_expr = "COALESCE(NULLIF(published_at_utc,''),received_at_utc,created_at)"
    if start:
        where.append(f"{time_expr} >= ?")
        params.append(str(start))
    if end:
        where.append(f"{time_expr} <= ?")
        params.append(str(end))
    sql = "SELECT COUNT(*) AS c FROM raw_news_articles"
    if where:
        sql += " WHERE " + " AND ".join(where)
    row = conn.execute(sql, params).fetchone()
    return int(row["c"] if row else 0)


def rebuild_news_lineage_v2(
    database_path: str,
    *,
    start: str | None = None,
    end: str | None = None,
    limit: int | None = None,
    bucket_minutes: int = 60,
) -> dict[str, Any]:
    """Backfill the M251/M252 lineage graph from preserved raw/source rows.

    This is the M253 bridge used by canonical baseline regeneration.  It does
    not fetch network data, submit broker orders, or promote thresholds; it only
    derives raw->mapping->cluster lineage from already stored rows.
    """

    conn = sqlite3.connect(database_path, timeout=60)
    conn.row_factory = sqlite3.Row
    synced_legacy = 0
    failed_legacy: list[dict[str, Any]] = []
    try:
        ensure_news_lineage_v2_schema(conn)
        legacy_ids = _legacy_news_ids_for_rebuild(conn, start=start, end=end, limit=limit)
        for legacy_id in legacy_ids:
            try:
                sync_legacy_news_article_to_raw(conn, legacy_id)
                synced_legacy += 1
            except Exception as exc:  # pragma: no cover - defensive audit path
                failed_legacy.append({"legacy_news_id": legacy_id, "error": str(exc)})
        assigned = assign_unclustered_articles(
            conn,
            limit=limit,
            bucket_minutes=bucket_minutes,
            start=start,
            end=end,
        )
        raw_scope_count = _raw_news_rebuild_scope_count(conn, start=start, end=end)
        role_count = int(
            conn.execute("SELECT COUNT(*) AS c FROM news_event_cluster_roles").fetchone()["c"]
        )
        candidate_count = int(
            conn.execute("SELECT COUNT(*) AS c FROM news_asset_candidates").fetchone()["c"]
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "version": NEWS_LINEAGE_V2_REBUILD_VERSION,
        "database_path": str(database_path),
        "start": start,
        "end": end,
        "limit": limit,
        "bucket_minutes": int(bucket_minutes),
        "synced_legacy_news_articles": synced_legacy,
        "failed_legacy_news_articles": failed_legacy[:20],
        "failed_legacy_news_article_count": len(failed_legacy),
        "raw_news_scope_count": raw_scope_count,
        "assigned_articles": assigned.get("assigned_articles", 0),
        "clusters_touched": assigned.get("clusters_touched", 0),
        "cluster_ids_sample": list(assigned.get("cluster_ids") or [])[:20],
        "event_cluster_role_count": role_count,
        "asset_candidate_count": candidate_count,
        "lineage_cutover": {
            "ranking": "news_event_clusters + news_event_cluster_roles + news_asset_candidates",
            "outcome": "deduplicated event_cluster_id/symbol candidates",
            "legacy_fallback": True,
        },
        "safety": {
            "network_fetch": False,
            "broker_orders_submitted": False,
            "transmit_enabled": False,
            "live_order_routing_enabled": False,
            "live_thresholds_promoted": False,
        },
        "generated_at": utc_now_iso(),
    }
