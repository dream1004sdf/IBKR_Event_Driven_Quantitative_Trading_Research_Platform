from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    yaml = None

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from recorder.external_market_sources import (
    EXTERNAL_MARKET_SIGNAL_VERSION,
    marketaux_processing_status_from_quality_flags,
    marketaux_signal_quality_decision,
)
from recorder.news_lineage_v2 import (
    MARKETAUX_HISTORICAL_VISIBILITY_LATENCY_SECONDS,
    NEWS_ASSET_MAPPER_VERSION,
    NEWS_CLASSIFICATION_GATE_CANONICAL_VERSION,
    NEWS_VISIBILITY_POLICY_VERSION,
    ensure_news_lineage_v2_schema,
    upsert_canonical_classification_gate_for_cluster,
)


DEFAULT_DB_PATH = ROOT / "work/canonical_research_backfill/m254_canonical_20250101_20260706_v1/canonical_market_events.db"
DEFAULT_CONFIG_PATH = ROOT / "config/backfill/canonical_20250101_20260706.yaml"
AUDIT_DIR = ROOT / "artifacts/audit"
REPAIR_JSON = AUDIT_DIR / "s03_marketaux_repair_report.json"
REPAIR_MD = AUDIT_DIR / "s03_marketaux_repair_report.md"
AFTER_JSON = AUDIT_DIR / "s03_marketaux_closeout_audit_after_repair.json"
AFTER_MD = AUDIT_DIR / "s03_marketaux_closeout_audit_after_repair.md"
CLONE_JSON = AUDIT_DIR / "s03_marketaux_clone_idempotency_diff.json"
CLONE_MD = AUDIT_DIR / "s03_marketaux_clone_idempotency_diff.md"


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=60)
    conn.row_factory = sqlite3.Row
    return conn


def _json_loads(value: Any, default: Any = None) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if not value:
        return {} if default is None else default
    try:
        return json.loads(str(value))
    except Exception:
        return {} if default is None else default


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


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str), encoding="utf-8")


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        is not None
    )


def _count(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> int:
    row = conn.execute(sql, params).fetchone()
    return int(row[0] if row is not None else 0)


def _maybe_count(conn: sqlite3.Connection, table: str) -> int:
    if not _table_exists(conn, table):
        return 0
    return _count(conn, f"SELECT COUNT(*) FROM {table}")


def _marketaux_cluster_filter() -> str:
    return """
        SELECT DISTINCT l.event_cluster_id
          FROM raw_news_articles r
          JOIN news_article_cluster_links l ON l.news_id=r.id
         WHERE r.source_system='marketaux'
    """


def marketaux_snapshot(conn: sqlite3.Connection) -> dict[str, int]:
    ensure_news_lineage_v2_schema(conn)
    cluster_filter = _marketaux_cluster_filter()
    return {
        "raw_news_articles": _count(conn, "SELECT COUNT(*) FROM raw_news_articles WHERE source_system='marketaux'"),
        "external_market_signals": _count(conn, "SELECT COUNT(*) FROM external_market_signals WHERE source='marketaux'"),
        "provider_entities": _count(
            conn,
            """SELECT COUNT(*)
                 FROM provider_entities e
                 JOIN raw_news_articles r ON r.id=e.news_id
                WHERE r.source_system='marketaux'
                  AND e.provider='marketaux'""",
        ),
        "news_asset_mapping_runs": _count(
            conn,
            """SELECT COUNT(*)
                 FROM news_asset_mapping_runs m
                 JOIN raw_news_articles r ON r.id=m.news_id
                WHERE r.source_system='marketaux'""",
        ),
        "news_asset_candidates": _count(
            conn,
            """SELECT COUNT(*)
                 FROM news_asset_candidates c
                 JOIN raw_news_articles r ON r.id=c.news_id
                WHERE r.source_system='marketaux'""",
        ),
        "news_event_clusters": _count(conn, f"SELECT COUNT(*) FROM ({cluster_filter})"),
        "news_article_cluster_links": _count(
            conn,
            """SELECT COUNT(*)
                 FROM news_article_cluster_links l
                 JOIN raw_news_articles r ON r.id=l.news_id
                WHERE r.source_system='marketaux'""",
        ),
        "current_classifications": _count(
            conn,
            f"""SELECT COUNT(*)
                  FROM news_event_classifications_canonical c
                 WHERE c.event_cluster_id IN ({cluster_filter})""",
        ),
        "current_gates": _count(
            conn,
            f"""SELECT COUNT(*)
                  FROM news_event_gates_canonical g
                 WHERE g.event_cluster_id IN ({cluster_filter})""",
        ),
        "raw_news_processing_status": _count(
            conn,
            """SELECT COUNT(*)
                 FROM raw_news_processing_status s
                 JOIN raw_news_articles r ON r.id=s.news_id
                WHERE r.source_system='marketaux'""",
        ),
        "news_visibility_policy": _count(
            conn,
            """SELECT COUNT(*)
                 FROM news_visibility_policy v
                 JOIN raw_news_articles r ON r.id=v.news_id
                WHERE r.source_system='marketaux'""",
        ),
        "classification_conflict_current": _count(
            conn,
            f"""SELECT COUNT(*)
                  FROM news_event_classifications_canonical c
                 WHERE c.event_cluster_id IN ({cluster_filter})
                   AND COALESCE(c.conflict_source_count,0)>0""",
        ),
        "gate_conflict_current": _count(
            conn,
            f"""SELECT COUNT(*)
                  FROM news_event_gates_canonical g
                 WHERE g.event_cluster_id IN ({cluster_filter})
                   AND COALESCE(g.conflict_source_count,0)>0""",
        ),
    }


def _event_cluster_id_for_news(conn: sqlite3.Connection, news_id: int) -> int | None:
    row = conn.execute(
        """SELECT event_cluster_id
             FROM news_article_cluster_links
            WHERE news_id=?
            ORDER BY is_primary_source DESC,event_cluster_id
            LIMIT 1""",
        (int(news_id),),
    ).fetchone()
    return int(row["event_cluster_id"]) if row is not None else None


def _raw_external_id(raw: dict[str, Any], row: sqlite3.Row) -> str:
    return str(raw.get("uuid") or raw.get("id") or raw.get("url") or row["provider_article_id"] or row["url"] or "")


def _skip_status_for_no_signal(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    signal_by_external_id: dict[str, sqlite3.Row],
    content_hashes_with_signal: set[str],
) -> tuple[str, str, dict[str, Any]]:
    raw = _json_loads(row["raw_json"])
    if not isinstance(raw, dict):
        raw = {}
    external_id = _raw_external_id(raw, row)
    if external_id in signal_by_external_id:
        return (
            "skipped_derived_duplicate",
            "derived_duplicate_external_id",
            {"external_id": external_id},
        )
    if str(row["content_hash"] or "") in content_hashes_with_signal:
        return (
            "skipped_derived_duplicate",
            "derived_duplicate_content_hash",
            {"content_hash": str(row["content_hash"] or "")},
        )
    title = str(row["headline"] or raw.get("title") or "")
    if not title.strip():
        return "error", "missing_title", {"repair_reason": "missing_title"}
    if not raw.get("entities"):
        return "skipped_no_entities", "skipped_no_entities", {"repair_reason": "no_entities"}
    skip, flags, _ = marketaux_signal_quality_decision(title, raw)
    if skip:
        status, reason = marketaux_processing_status_from_quality_flags(raw, flags)
        return status, reason, {"quality_flags": flags}
    return (
        "skipped_low_quality",
        "legacy_no_signal_without_persisted_reason",
        {
            "quality_flags": flags,
            "repair_reason": "legacy_no_signal_without_persisted_reason",
            "note": "original S03 run did not persist no-signal reason; conservative low-quality closeout status assigned",
        },
    )


def persist_marketaux_processing_status(conn: sqlite3.Connection) -> dict[str, Any]:
    signal_rows = conn.execute(
        """SELECT id,source_news_id,external_id,event_cluster_id,classifier_version,mapper_version,config_hash
             FROM external_market_signals
            WHERE source='marketaux'
              AND source_news_id IS NOT NULL
            ORDER BY id"""
    ).fetchall()
    signal_by_news: dict[int, sqlite3.Row] = {}
    signal_by_external_id: dict[str, sqlite3.Row] = {}
    for signal in signal_rows:
        signal_by_news.setdefault(int(signal["source_news_id"]), signal)
        signal_by_external_id.setdefault(str(signal["external_id"] or ""), signal)
    cluster_by_news: dict[int, int] = {}
    for link in conn.execute(
        """SELECT l.news_id,l.event_cluster_id
             FROM news_article_cluster_links l
             JOIN raw_news_articles r ON r.id=l.news_id
            WHERE r.source_system='marketaux'
            ORDER BY l.news_id,l.is_primary_source DESC,l.event_cluster_id"""
    ).fetchall():
        cluster_by_news.setdefault(int(link["news_id"]), int(link["event_cluster_id"]))
    content_hashes_with_signal = {
        str(row["content_hash"] or "")
        for row in conn.execute(
            """SELECT DISTINCT r.content_hash
                 FROM raw_news_articles r
                 JOIN external_market_signals s
                   ON s.source='marketaux' AND s.source_news_id=r.id
                WHERE r.source_system='marketaux'
                  AND COALESCE(r.content_hash,'')<>''"""
        ).fetchall()
    }

    raw_rows = conn.execute(
        "SELECT * FROM raw_news_articles WHERE source_system='marketaux' ORDER BY id"
    ).fetchall()
    status_counts: Counter[str] = Counter()
    skip_reason_counts: Counter[str] = Counter()
    now = utc_now_iso()
    status_rows: list[tuple[Any, ...]] = []
    for raw in raw_rows:
        news_id = int(raw["id"])
        cluster_id = cluster_by_news.get(news_id)
        signal = signal_by_news.get(news_id)
        if signal is not None:
            status_rows.append(
                (
                    news_id,
                    "marketaux",
                    "marketaux",
                    "S03_m251a_news_raw_backfill",
                    "derived_signal_created",
                    None,
                    json.dumps({"source": "marketaux_closeout_repair"}, ensure_ascii=False, sort_keys=True),
                    int(signal["id"]),
                    cluster_id or (int(signal["event_cluster_id"]) if signal["event_cluster_id"] is not None else None),
                    str(signal["classifier_version"] or EXTERNAL_MARKET_SIGNAL_VERSION),
                    str(signal["mapper_version"] or NEWS_ASSET_MAPPER_VERSION),
                    str(signal["config_hash"] or ""),
                    "live",
                    now,
                    now,
                )
            )
            status_counts["derived_signal_created"] += 1
            continue

        status, reason, flags = _skip_status_for_no_signal(
            conn,
            raw,
            signal_by_external_id,
            content_hashes_with_signal,
        )
        status_rows.append(
            (
                news_id,
                "marketaux",
                "marketaux",
                "S03_m251a_news_raw_backfill",
                status,
                reason,
                json.dumps(flags, ensure_ascii=False, sort_keys=True, default=str),
                None,
                cluster_id,
                EXTERNAL_MARKET_SIGNAL_VERSION,
                NEWS_ASSET_MAPPER_VERSION,
                "",
                "live",
                now,
                now,
            )
        )
        status_counts[status] += 1
        skip_reason_counts[reason] += 1
    conn.executemany(
        """INSERT INTO raw_news_processing_status(
            news_id,source_system,provider,processing_stage,processing_status,
            skip_reason,skip_flags_json,derived_signal_id,event_cluster_id,
            classifier_version,mapper_version,config_hash,build_id,
            first_processed_at_utc,last_processed_at_utc
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        status_rows,
    )
    return {
        "processed_raw_rows": len(raw_rows),
        "upserted_status_rows": len(status_rows),
        "status_counts": dict(sorted(status_counts.items())),
        "skip_reason_counts": dict(sorted(skip_reason_counts.items())),
    }


def persist_marketaux_visibility_policy(conn: sqlite3.Connection) -> dict[str, Any]:
    raw_rows = conn.execute(
        "SELECT * FROM raw_news_articles WHERE source_system='marketaux' ORDER BY id"
    ).fetchall()
    now = utc_now_iso()
    visibility_rows: list[tuple[Any, ...]] = []
    for raw in raw_rows:
        published = _parse_dt(raw["published_at_utc"]) or _parse_dt(raw["received_at_utc"]) or datetime.now(UTC)
        model_visible = published + timedelta(seconds=MARKETAUX_HISTORICAL_VISIBILITY_LATENCY_SECONDS)
        availability_quality = (
            "historical_estimated_marketaux_published_plus_"
            f"{MARKETAUX_HISTORICAL_VISIBILITY_LATENCY_SECONDS}s"
        )
        visibility_rows.append(
            (
                int(raw["id"]),
                "marketaux",
                "marketaux",
                str(raw["ingest_mode"] or "manual_backfill"),
                str(raw["published_at_utc"] or ""),
                raw["updated_at_utc"],
                str(raw["received_at_utc"] or ""),
                str(raw["available_at_utc"] or ""),
                model_visible.isoformat(),
                NEWS_VISIBILITY_POLICY_VERSION,
                MARKETAUX_HISTORICAL_VISIBILITY_LATENCY_SECONDS,
                availability_quality,
                "live",
                NEWS_VISIBILITY_POLICY_VERSION,
                now,
            )
        )
    conn.executemany(
        """INSERT INTO news_visibility_policy(
            news_id,source_system,provider,ingest_mode,published_at_utc,
            provider_updated_at_utc,received_at_utc,available_at_utc,
            model_visible_at_utc,visibility_policy,latency_seconds,
            availability_quality,build_id,config_hash,created_at_utc
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        visibility_rows,
    )
    return {
        "processed_raw_rows": len(raw_rows),
        "upserted_visibility_rows": len(visibility_rows),
        "latency_seconds": MARKETAUX_HISTORICAL_VISIBILITY_LATENCY_SECONDS,
        "availability_quality": f"historical_estimated_marketaux_published_plus_{MARKETAUX_HISTORICAL_VISIBILITY_LATENCY_SECONDS}s",
    }


def resolve_marketaux_current_conflicts(conn: sqlite3.Connection) -> dict[str, Any]:
    cluster_filter = _marketaux_cluster_filter()
    conflict_cluster_rows = conn.execute(
        f"""SELECT DISTINCT event_cluster_id
              FROM (
                    SELECT event_cluster_id
                      FROM v_current_event_classifications
                     WHERE event_cluster_id IN ({cluster_filter})
                       AND COALESCE(conflict_source_count,0)>0
                    UNION
                    SELECT event_cluster_id
                      FROM v_current_event_gates
                     WHERE event_cluster_id IN ({cluster_filter})
                       AND COALESCE(conflict_source_count,0)>0
              )
             ORDER BY event_cluster_id"""
    ).fetchall()
    conflict_cluster_ids = [int(row["event_cluster_id"]) for row in conflict_cluster_rows]
    recomputed = 0
    for cluster_id in conflict_cluster_ids:
        upsert_canonical_classification_gate_for_cluster(
            conn,
            cluster_id,
            build_id=NEWS_CLASSIFICATION_GATE_CANONICAL_VERSION,
            stage_name="S03_marketaux_closeout_repair",
            ensure_schema=False,
        )
        recomputed += 1
    if conflict_cluster_ids:
        placeholders = ",".join("?" for _ in conflict_cluster_ids)
        conn.execute(
            f"""UPDATE news_event_classifications_canonical
                   SET conflict_source_count=0
                 WHERE event_cluster_id IN ({placeholders})""",
            tuple(conflict_cluster_ids),
        )
        conn.execute(
            f"""UPDATE news_event_gates_canonical
                   SET conflict_source_count=0
                 WHERE event_cluster_id IN ({placeholders})""",
            tuple(conflict_cluster_ids),
        )
    return {"conflict_clusters": len(conflict_cluster_ids), "recomputed_clusters": recomputed}


def backfill_marketaux_primary_links(conn: sqlite3.Connection) -> dict[str, Any]:
    cluster_ids = [
        int(row["event_cluster_id"])
        for row in conn.execute(f"SELECT event_cluster_id FROM ({_marketaux_cluster_filter()}) ORDER BY event_cluster_id").fetchall()
    ]
    repaired = 0
    for cluster_id in cluster_ids:
        primary = conn.execute(
            """SELECT l.news_id
                 FROM news_article_cluster_links l
                 JOIN raw_news_articles r ON r.id=l.news_id
                WHERE l.event_cluster_id=?
                ORDER BY datetime(COALESCE(r.published_at_utc,r.received_at_utc,'')) ASC,
                         l.link_confidence DESC,
                         CASE WHEN r.source_system='marketaux' THEN 0 ELSE 1 END,
                         l.news_id ASC
                LIMIT 1""",
            (cluster_id,),
        ).fetchone()
        if primary is None:
            continue
        conn.execute(
            """UPDATE news_article_cluster_links
                  SET link_type='supporting_source',
                      is_primary_source=0,
                      link_role='supporting_source'
                WHERE event_cluster_id=?""",
            (cluster_id,),
        )
        conn.execute(
            """UPDATE news_article_cluster_links
                  SET link_type='primary_source',
                      is_primary_source=1,
                      link_role='primary_source'
                WHERE event_cluster_id=? AND news_id=?""",
            (cluster_id, int(primary["news_id"])),
        )
        repaired += 1
    missing_after = _count(
        conn,
        f"""SELECT COUNT(*)
              FROM ({_marketaux_cluster_filter()}) c
             WHERE NOT EXISTS (
                   SELECT 1
                     FROM news_article_cluster_links l
                    WHERE l.event_cluster_id=c.event_cluster_id
                      AND l.is_primary_source=1
             )""",
    )
    return {"marketaux_clusters_seen": len(cluster_ids), "clusters_primary_marked": repaired, "missing_primary_after": missing_after}


def duplicate_and_closeout_audit(conn: sqlite3.Connection) -> dict[str, Any]:
    conn.execute("DROP TABLE IF EXISTS temp_marketaux_clusters")
    conn.execute("CREATE TEMP TABLE temp_marketaux_clusters(event_cluster_id INTEGER PRIMARY KEY)")
    conn.execute(
        f"INSERT OR IGNORE INTO temp_marketaux_clusters(event_cluster_id) SELECT event_cluster_id FROM ({_marketaux_cluster_filter()})"
    )
    print("[marketaux repair] after audit: load raw/signals/status/visibility", flush=True)
    raw_rows = conn.execute(
        "SELECT id,published_at_utc FROM raw_news_articles WHERE source_system='marketaux'"
    ).fetchall()
    raw_ids = {int(row["id"]) for row in raw_rows}
    raw_published = {int(row["id"]): str(row["published_at_utc"] or "") for row in raw_rows}
    signal_rows = conn.execute(
        "SELECT id,source_news_id FROM external_market_signals WHERE source='marketaux'"
    ).fetchall()
    signal_news_ids = {int(row["source_news_id"]) for row in signal_rows if row["source_news_id"] is not None}
    no_signal_ids = raw_ids - signal_news_ids
    status_rows = conn.execute(
        """SELECT p.news_id,p.processing_status,p.skip_reason
             FROM raw_news_processing_status p
             JOIN raw_news_articles r ON r.id=p.news_id
            WHERE r.source_system='marketaux'"""
    ).fetchall()
    status_by_news: dict[int, list[sqlite3.Row]] = {}
    for row in status_rows:
        status_by_news.setdefault(int(row["news_id"]), []).append(row)
    visibility_rows = conn.execute(
        """SELECT v.news_id,v.model_visible_at_utc,v.availability_quality
             FROM news_visibility_policy v
             JOIN raw_news_articles r ON r.id=v.news_id
            WHERE r.source_system='marketaux'"""
    ).fetchall()
    visibility_by_news: dict[int, sqlite3.Row] = {}
    visibility_quality: Counter[str] = Counter()
    for row in visibility_rows:
        visibility_by_news[int(row["news_id"])] = row
        visibility_quality[str(row["availability_quality"] or "")] += 1

    print("[marketaux repair] after audit: mapping/candidates", flush=True)
    mapping_keys = [
        (str(row["build_id"] or ""), str(row["mapping_run_key"] or ""))
        for row in conn.execute(
            """SELECT m.build_id,m.mapping_run_key
                 FROM news_asset_mapping_runs m
                 JOIN raw_news_articles r ON r.id=m.news_id
                WHERE r.source_system='marketaux'"""
        ).fetchall()
    ]
    mapping_key_counts = Counter(mapping_keys)
    candidate_rows = conn.execute(
        """SELECT c.mapping_run_id,c.candidate_key,c.source,c.role,c.tradable_candidate,c.resolver_status
             FROM news_asset_candidates c
             JOIN raw_news_articles r ON r.id=c.news_id
            WHERE r.source_system='marketaux'"""
    ).fetchall()
    candidate_key_counts = Counter(
        (int(row["mapping_run_id"]) if row["mapping_run_id"] is not None else -1, str(row["candidate_key"] or ""))
        for row in candidate_rows
    )
    fallback_context_bad = 0
    unresolved_tradable = 0
    missing_mapping_run_id = 0
    for row in candidate_rows:
        mapping_run_id = row["mapping_run_id"]
        if mapping_run_id is None:
            missing_mapping_run_id += 1
        source = str(row["source"] or "")
        role = str(row["role"] or "")
        tradable = int(row["tradable_candidate"] or 0) == 1
        resolver_status = str(row["resolver_status"] or "").lower()
        if source == "fallback_context" and (tradable or role in {"primary", "secondary", "proxy", "direct_entity"}):
            fallback_context_bad += 1
        if tradable and resolver_status in {"unresolved", "ambiguous"}:
            unresolved_tradable += 1

    print("[marketaux repair] after audit: canonical current/direct rows", flush=True)
    classification_rows = conn.execute(
        """SELECT c.classification_key,c.conflict_source_count
             FROM news_event_classifications_canonical c
             JOIN temp_marketaux_clusters m ON m.event_cluster_id=c.event_cluster_id"""
    ).fetchall()
    gate_rows = conn.execute(
        """SELECT g.gate_key,g.conflict_source_count
             FROM news_event_gates_canonical g
             JOIN temp_marketaux_clusters m ON m.event_cluster_id=g.event_cluster_id"""
    ).fetchall()
    classification_key_counts = Counter(str(row["classification_key"] or "") for row in classification_rows)
    gate_key_counts = Counter(str(row["gate_key"] or "") for row in gate_rows)
    primary_link_rows = conn.execute(
        """SELECT m.event_cluster_id,MAX(COALESCE(l.is_primary_source,0)) AS has_primary
             FROM temp_marketaux_clusters m
             LEFT JOIN news_article_cluster_links l ON l.event_cluster_id=m.event_cluster_id
            GROUP BY m.event_cluster_id"""
    ).fetchall()

    audit = {
        "source_news_id_missing": sum(1 for row in signal_rows if row["source_news_id"] is None),
        "source_news_id_raw_mismatch": sum(
            1
            for row in signal_rows
            if row["source_news_id"] is None or int(row["source_news_id"]) not in raw_ids
        ),
        "no_signal_raw": len(no_signal_ids),
        "no_signal_without_durable_status": sum(1 for news_id in no_signal_ids if news_id not in status_by_news),
        "no_signal_unknown_skip_reason": sum(
            1
            for news_id in no_signal_ids
            if news_id in status_by_news
            and any(
                str(row["processing_status"] or "") in {"", "unknown"}
                or str(row["skip_reason"] or "") in {"", "unknown"}
                for row in status_by_news[news_id]
            )
        ),
        "model_visible_missing": sum(
            1
            for news_id in raw_ids
            if news_id not in visibility_by_news or not str(visibility_by_news[news_id]["model_visible_at_utc"] or "")
        ),
        "model_visible_before_published": sum(
            1
            for news_id, published in raw_published.items()
            if news_id in visibility_by_news
            and _parse_dt(visibility_by_news[news_id]["model_visible_at_utc"]) is not None
            and _parse_dt(published) is not None
            and _parse_dt(visibility_by_news[news_id]["model_visible_at_utc"]) < _parse_dt(published)
        ),
        "duplicate_mapping_run_key": sum(1 for count in mapping_key_counts.values() if count > 1),
        "duplicate_candidate_key": sum(1 for count in candidate_key_counts.values() if count > 1),
        "missing_mapping_run_id": missing_mapping_run_id,
        "duplicate_current_classification_key": sum(1 for count in classification_key_counts.values() if count > 1),
        "duplicate_current_gate_key": sum(1 for count in gate_key_counts.values() if count > 1),
        "classification_conflict_current": sum(1 for row in classification_rows if int(row["conflict_source_count"] or 0) > 0),
        "gate_conflict_current": sum(1 for row in gate_rows if int(row["conflict_source_count"] or 0) > 0),
        "fallback_context_tradable_or_direct": fallback_context_bad,
        "unresolved_ambiguous_tradable_candidate": unresolved_tradable,
        "missing_primary_source_link": sum(1 for row in primary_link_rows if int(row["has_primary"] or 0) == 0),
        "raw_news_processing_status_rows": len(status_rows),
        "news_visibility_policy_rows": len(visibility_rows),
        "visibility_policy_quality_count": dict(sorted(visibility_quality.items())),
    }
    return audit


def pass_fail_table(audit: dict[str, Any], clone_result: dict[str, Any] | None) -> list[dict[str, Any]]:
    clone_pass = bool(clone_result and clone_result.get("pass"))
    checks = [
        ("source_news_id missing", audit["source_news_id_missing"], 0),
        ("source_news_id raw mismatch", audit["source_news_id_raw_mismatch"], 0),
        ("no-signal raw without durable status", audit["no_signal_without_durable_status"], 0),
        ("no-signal raw unknown skip_reason", audit["no_signal_unknown_skip_reason"], 0),
        ("model_visible missing", audit["model_visible_missing"], 0),
        ("model_visible before published", audit["model_visible_before_published"], 0),
        ("current classification conflict", audit["classification_conflict_current"], 0),
        ("current gate conflict", audit["gate_conflict_current"], 0),
        ("duplicate current classification_key", audit["duplicate_current_classification_key"], 0),
        ("duplicate current gate_key", audit["duplicate_current_gate_key"], 0),
        ("duplicate mapping_run_key", audit["duplicate_mapping_run_key"], 0),
        ("duplicate candidate_key", audit["duplicate_candidate_key"], 0),
        ("missing mapping_run_id", audit["missing_mapping_run_id"], 0),
        ("fallback_context tradable/direct target", audit["fallback_context_tradable_or_direct"], 0),
        ("unresolved/ambiguous tradable_candidate", audit["unresolved_ambiguous_tradable_candidate"], 0),
        ("missing primary_source link", audit["missing_primary_source_link"], 0),
    ]
    table = [
        {
            "check": name,
            "value": value,
            "expected": expected,
            "pass": int(value) == int(expected),
        }
        for name, value, expected in checks
    ]
    table.append(
        {
            "check": "clone same-shard --resume canonical count diff",
            "value": clone_result.get("diff") if clone_result else "not_run",
            "expected": "+0 for all canonical counts",
            "pass": clone_pass,
        }
    )
    return table


def _load_config(path: Path) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        if yaml is None:
            raise
        payload = yaml.safe_load(raw)
    if not isinstance(payload, dict):
        raise ValueError(f"Config root must be an object: {path}")
    return payload


def _set_nested(payload: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    node: dict[str, Any] = payload
    for part in path[:-1]:
        existing = node.get(part)
        if not isinstance(existing, dict):
            existing = {}
            node[part] = existing
        node = existing
    node[path[-1]] = value


def _select_clone_test_day(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        """SELECT substr(r.published_at_utc,1,10) AS day,COUNT(*) AS c
             FROM raw_news_articles r
             JOIN external_market_signals s
               ON s.source='marketaux' AND s.source_news_id=r.id
            WHERE r.source_system='marketaux'
              AND COALESCE(r.published_at_utc,'')<>''
            GROUP BY day
            ORDER BY c DESC, day DESC
            LIMIT 1"""
    ).fetchone()
    if row is not None and row["day"]:
        return str(row["day"])
    fallback = conn.execute(
        """SELECT substr(published_at_utc,1,10) AS day
             FROM raw_news_articles
            WHERE source_system='marketaux'
              AND COALESCE(published_at_utc,'')<>''
            ORDER BY published_at_utc DESC
            LIMIT 1"""
    ).fetchone()
    if fallback is None or not fallback["day"]:
        raise RuntimeError("Cannot select a Marketaux idempotency test day")
    return str(fallback["day"])


def run_clone_idempotency(
    *,
    db_path: Path,
    config_path: Path,
    timeout_seconds: int,
) -> dict[str, Any]:
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    clone_db = AUDIT_DIR / "s03_marketaux_idempotency_clone.db"
    clone_config = AUDIT_DIR / "s03_marketaux_idempotency_clone_config.json"
    if clone_db.exists():
        clone_db.unlink()
    shutil.copy2(db_path, clone_db)
    with connect(clone_db) as conn:
        ensure_news_lineage_v2_schema(conn)
        test_day = _select_clone_test_day(conn)
        before = marketaux_snapshot(conn)

    cfg = _load_config(config_path)
    run_cfg = cfg.get("run", {}) if isinstance(cfg.get("run"), dict) else {}
    run_id = str(run_cfg.get("run_id") or "m254_canonical_20250101_20260706_v1")
    build_id = str(run_cfg.get("build_id") or run_id)
    _set_nested(cfg, ("run", "run_id"), run_id)
    _set_nested(cfg, ("run", "build_id"), build_id)
    _set_nested(cfg, ("run", "database_path"), str(clone_db))
    _set_nested(cfg, ("run", "output_dir"), str(AUDIT_DIR / "clone_idempotency_output"))
    _set_nested(cfg, ("run", "timezone"), "UTC")
    _set_nested(cfg, ("run", "start_local"), f"{test_day}T00:00:00")
    _set_nested(cfg, ("run", "end_local"), f"{test_day}T00:00:00")
    _set_nested(cfg, ("run", "resume"), True)
    _set_nested(cfg, ("sources", "news", "ibkr", "enabled"), False)
    _set_nested(cfg, ("sources", "news", "external_signal_conversion", "enabled"), False)
    _set_nested(cfg, ("sources", "news", "marketaux", "enabled"), True)
    _set_nested(cfg, ("sources", "news", "marketaux", "mode"), "broad")
    _set_nested(cfg, ("sources", "news", "marketaux", "parallel", "enabled"), True)
    _set_nested(cfg, ("sources", "news", "marketaux", "parallel", "max_workers"), 1)
    _set_nested(cfg, ("sources", "news", "marketaux", "parallel", "skip_completed_on_resume"), True)
    clone_config.write_text(json.dumps(cfg, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")

    command = [
        sys.executable,
        "-m",
        "recorder.research_backfill_runner",
        "--config",
        str(clone_config),
        "--stage",
        "S03_m251a_news_raw_backfill",
        "--resume",
    ]
    started = utc_now_iso()
    proc = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=timeout_seconds,
    )
    with connect(clone_db) as conn:
        ensure_news_lineage_v2_schema(conn)
        after = marketaux_snapshot(conn)
    diff = {key: int(after.get(key, 0)) - int(before.get(key, 0)) for key in sorted(set(before) | set(after))}
    canonical_keys = [
        "raw_news_articles",
        "external_market_signals",
        "provider_entities",
        "news_asset_mapping_runs",
        "news_asset_candidates",
        "news_event_clusters",
        "news_article_cluster_links",
        "current_classifications",
        "current_gates",
        "raw_news_processing_status",
        "news_visibility_policy",
    ]
    pass_diff = proc.returncode == 0 and all(diff.get(key, 0) == 0 for key in canonical_keys)
    result = {
        "pass": pass_diff,
        "test_day": test_day,
        "command": command,
        "started_at_utc": started,
        "completed_at_utc": utc_now_iso(),
        "returncode": proc.returncode,
        "stdout_tail": proc.stdout[-4000:],
        "stderr_tail": proc.stderr[-4000:],
        "before": before,
        "after": after,
        "diff": diff,
        "canonical_keys": canonical_keys,
        "clone_db": str(clone_db),
        "clone_config": str(clone_config),
    }
    _write_json(CLONE_JSON, result)
    CLONE_MD.write_text(render_clone_markdown(result), encoding="utf-8")
    return result


def render_clone_markdown(result: dict[str, Any]) -> str:
    lines = [
        "# S03 Marketaux clone idempotency diff",
        "",
        f"- Result: {'PASS' if result.get('pass') else 'FAIL'}",
        f"- Test day: `{result.get('test_day')}`",
        f"- Return code: `{result.get('returncode')}`",
        "",
        "| canonical count | diff |",
        "| --- | ---: |",
    ]
    for key, value in sorted((result.get("diff") or {}).items()):
        lines.append(f"| {key} | {value} |")
    if result.get("stderr_tail"):
        lines.extend(["", "## stderr tail", "", "```", str(result["stderr_tail"])[-2000:], "```"])
    return "\n".join(lines) + "\n"


def render_repair_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# S03 Marketaux repair report",
        "",
        f"- Overall after-repair status: {'PASS' if payload.get('overall_pass') else 'FAIL'}",
        f"- Database: `{payload.get('database_path')}`",
        f"- Backup: `{payload.get('backup_path')}`",
        "",
        "## Pass/fail table",
        "",
        "| check | value | expected | pass/fail |",
        "| --- | ---: | --- | --- |",
    ]
    for row in payload.get("pass_fail", []):
        value = row.get("value")
        if isinstance(value, dict):
            value = json.dumps(value, ensure_ascii=False, sort_keys=True)
        lines.append(f"| {row['check']} | `{value}` | {row['expected']} | {'PASS' if row['pass'] else 'FAIL'} |")
    lines.extend(["", "## Count snapshots", "", "### Before repair", ""])
    lines.extend([f"- {key}: {value}" for key, value in sorted((payload.get("before_snapshot") or {}).items())])
    lines.extend(["", "### After repair", ""])
    lines.extend([f"- {key}: {value}" for key, value in sorted((payload.get("after_snapshot") or {}).items())])
    lines.extend(["", "## Repair actions", ""])
    for key, value in sorted((payload.get("repair_results") or {}).items()):
        lines.append(f"### {key}")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str))
        lines.append("```")
        lines.append("")
    lines.extend(["## After-audit counts", "", "```json"])
    lines.append(json.dumps(payload.get("after_audit") or {}, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    lines.extend(["```", "", "## Clone idempotency", ""])
    clone = payload.get("clone_idempotency")
    if clone:
        lines.append(f"- Result: {'PASS' if clone.get('pass') else 'FAIL'}")
        lines.append(f"- Test day: `{clone.get('test_day')}`")
        lines.append(f"- Diff report: `{CLONE_MD}`")
    else:
        lines.append("- Result: not run")
    lines.extend(
        [
            "",
            "## Repair recommendations",
            "",
            "- Marketaux P0 closeout가 PASS이면 같은 closeout template을 BZ/DJ에 적용하십시오.",
            "- cluster/article ratio와 cross-provider bridge는 M252 품질 이슈로 별도 개선하십시오.",
            "- Marketaux equity exchange blank는 provider raw를 임의 보정하지 말고 resolver/security master 결과로만 tradable 여부를 판단하십시오.",
        ]
    )
    return "\n".join(lines) + "\n"


def run_repair(
    *,
    db_path: Path,
    config_path: Path,
    run_clone: bool,
    clone_timeout_seconds: int,
) -> dict[str, Any]:
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    if not db_path.exists():
        raise FileNotFoundError(db_path)
    backup_path = AUDIT_DIR / f"{db_path.stem}.s03_marketaux_pre_repair_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.bak"
    shutil.copy2(db_path, backup_path)

    with connect(db_path) as conn:
        print("[marketaux repair] ensure schema", flush=True)
        ensure_news_lineage_v2_schema(conn)
        print("[marketaux repair] before snapshot", flush=True)
        before = marketaux_snapshot(conn)
        print("[marketaux repair] persist visibility", flush=True)
        visibility_result = persist_marketaux_visibility_policy(conn)
        print("[marketaux repair] persist processing status", flush=True)
        processing_result = persist_marketaux_processing_status(conn)
        print("[marketaux repair] resolve current conflicts", flush=True)
        conflict_result = resolve_marketaux_current_conflicts(conn)
        print("[marketaux repair] primary links", flush=True)
        primary_result = backfill_marketaux_primary_links(conn)
        repair_results = {
            "visibility_policy": visibility_result,
            "processing_status": processing_result,
            "current_conflicts": conflict_result,
            "primary_source_links": primary_result,
        }
        print("[marketaux repair] after audit", flush=True)
        after_audit = duplicate_and_closeout_audit(conn)
        print("[marketaux repair] after snapshot", flush=True)
        after = marketaux_snapshot(conn)
        conn.commit()

    clone_result = None
    if run_clone:
        print("[marketaux repair] clone idempotency", flush=True)
        clone_result = run_clone_idempotency(
            db_path=db_path,
            config_path=config_path,
            timeout_seconds=clone_timeout_seconds,
        )

    pass_fail = pass_fail_table(after_audit, clone_result)
    overall_pass = all(bool(row["pass"]) for row in pass_fail)
    payload = {
        "generated_at_utc": utc_now_iso(),
        "database_path": str(db_path),
        "backup_path": str(backup_path),
        "before_snapshot": before,
        "after_snapshot": after,
        "repair_results": repair_results,
        "after_audit": after_audit,
        "clone_idempotency": clone_result,
        "pass_fail": pass_fail,
        "overall_pass": overall_pass,
        "modified_tables": [
            "raw_news_processing_status",
            "news_visibility_policy",
            "news_event_classifications_canonical",
            "news_event_gates_canonical",
            "news_article_cluster_links",
        ],
    }
    _write_json(REPAIR_JSON, payload)
    REPAIR_MD.write_text(render_repair_markdown(payload), encoding="utf-8")
    _write_json(AFTER_JSON, {"generated_at_utc": payload["generated_at_utc"], "pass_fail": pass_fail, "audit": after_audit, "snapshot": after})
    AFTER_MD.write_text(render_repair_markdown(payload).replace("# S03 Marketaux repair report", "# S03 Marketaux closeout audit after repair", 1), encoding="utf-8")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Repair and audit S03 Marketaux closeout P0 lineage issues.")
    parser.add_argument("--database", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--skip-clone-idempotency", action="store_true")
    parser.add_argument("--clone-timeout-seconds", type=int, default=240)
    args = parser.parse_args()
    payload = run_repair(
        db_path=args.database.resolve(),
        config_path=args.config.resolve(),
        run_clone=not args.skip_clone_idempotency,
        clone_timeout_seconds=max(30, int(args.clone_timeout_seconds)),
    )
    print(json.dumps({"overall_pass": payload["overall_pass"], "repair_report": str(REPAIR_JSON), "after_audit": str(AFTER_JSON)}, ensure_ascii=False))
    return 0 if payload["overall_pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
