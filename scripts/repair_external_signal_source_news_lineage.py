from __future__ import annotations

import argparse
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def _default_db_path() -> Path:
    return (
        Path(__file__).resolve().parents[1]
        / "work"
        / "canonical_research_backfill"
        / "m254_canonical_20250101_20260706_v1"
        / "canonical_market_events.db"
    )


def _audit(conn: sqlite3.Connection) -> dict[str, int]:
    cur = conn.cursor()
    return {
        "benzinga_missing_or_bad_source_news_id": cur.execute(
            """
            SELECT COUNT(*)
            FROM external_market_signals s
            LEFT JOIN raw_news_articles r
              ON r.id=s.source_news_id
            WHERE s.source='benzinga'
              AND (
                s.source_news_id IS NULL
                OR r.id IS NULL
                OR r.provider!='BZ'
              )
            """
        ).fetchone()[0],
        "dow_jones_missing_or_bad_source_news_id": cur.execute(
            """
            SELECT COUNT(*)
            FROM external_market_signals s
            LEFT JOIN raw_news_articles r
              ON r.id=s.source_news_id
            WHERE s.source='dow_jones'
              AND (
                s.source_news_id IS NULL
                OR r.id IS NULL
                OR upper(r.provider) NOT LIKE 'DJ%'
              )
            """
        ).fetchone()[0],
        "benzinga_bad_source_link": cur.execute(
            """
            SELECT COUNT(*)
            FROM external_signal_source_links l
            JOIN external_market_signals s
              ON s.id=l.signal_id
            LEFT JOIN raw_news_articles r
              ON r.id=l.news_id
            WHERE s.source='benzinga'
              AND (r.id IS NULL OR r.provider!='BZ')
            """
        ).fetchone()[0],
        "dow_jones_bad_source_link": cur.execute(
            """
            SELECT COUNT(*)
            FROM external_signal_source_links l
            JOIN external_market_signals s
              ON s.id=l.signal_id
            LEFT JOIN raw_news_articles r
              ON r.id=l.news_id
            WHERE s.source='dow_jones'
              AND (r.id IS NULL OR upper(r.provider) NOT LIKE 'DJ%')
            """
        ).fetchone()[0],
    }


def _repair_source(conn: sqlite3.Connection, *, source: str, prefix: str, provider_predicate: str) -> dict[str, int]:
    cur = conn.cursor()
    rows = cur.execute(
        f"""
        SELECT s.id AS signal_id,
               s.source_news_id AS old_news_id,
               r.id AS correct_news_id
        FROM external_market_signals s
        JOIN raw_news_articles r
          ON r.legacy_news_id = CAST(substr(s.external_id, ?) AS INTEGER)
        WHERE s.source=?
          AND s.external_id LIKE ?
          AND {provider_predicate}
        """,
        (len(prefix) + 1, source, f"{prefix}%"),
    ).fetchall()
    updates = 0
    link_updates = 0
    now = datetime.now(timezone.utc).isoformat()
    for row in rows:
        signal_id = int(row["signal_id"])
        correct_news_id = int(row["correct_news_id"])
        old_news_id = row["old_news_id"]
        if old_news_id != correct_news_id:
            cur.execute(
                """
                UPDATE external_market_signals
                   SET source_news_id=?,
                       updated_at=?
                 WHERE id=?
                """,
                (correct_news_id, now, signal_id),
            )
            updates += cur.rowcount
        # Keep the source-link table consistent with source_news_id.  Delete any
        # primary_source links for this signal and insert the canonical one.
        cur.execute(
            """
            DELETE FROM external_signal_source_links
             WHERE signal_id=?
               AND link_type='primary_source'
               AND news_id<>?
            """,
            (signal_id, correct_news_id),
        )
        link_updates += cur.rowcount
        cur.execute(
            """
            INSERT OR IGNORE INTO external_signal_source_links(
                signal_id,news_id,link_type,confidence,created_at
            ) VALUES(?,?, 'primary_source', 1.0, ?)
            """,
            (signal_id, correct_news_id, now),
        )
        link_updates += cur.rowcount
    return {
        f"{source}_candidate_rows": len(rows),
        f"{source}_source_news_id_updates": updates,
        f"{source}_source_link_updates": link_updates,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Repair M251 external signal source_news_id lineage for BZ/DJ derived signals."
    )
    parser.add_argument("--db", type=Path, default=_default_db_path())
    parser.add_argument("--no-backup", action="store_true")
    args = parser.parse_args()

    db_path = args.db
    if not db_path.exists():
        raise SystemExit(f"DB not found: {db_path}")
    if not args.no_backup:
        backup = db_path.with_suffix(db_path.suffix + f".lineage-repair-{datetime.now().strftime('%Y%m%d_%H%M%S')}.bak")
        shutil.copy2(db_path, backup)
        print(f"backup={backup}")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        before = _audit(conn)
        with conn:
            bz = _repair_source(
                conn,
                source="benzinga",
                prefix="bz:",
                provider_predicate="r.provider='BZ'",
            )
            dj = _repair_source(
                conn,
                source="dow_jones",
                prefix="dj:",
                provider_predicate="upper(r.provider) LIKE 'DJ%'",
            )
        after = _audit(conn)
    finally:
        conn.close()

    print(f"before={before}")
    print(f"repair={{**{bz}, **{dj}}}")
    print(f"after={after}")
    if any(after.values()):
        raise SystemExit(2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
