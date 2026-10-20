from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Optional, Sequence

from .models import NewsInput, QuoteSnapshot
from .news_lineage_v2 import (
    ensure_news_lineage_v2_schema,
    sync_legacy_news_article_to_raw,
)
from .quote_integrity import QuoteIntegrityResult, assess_quote_integrity

UTC = timezone.utc


def utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        try:
            yield conn
        finally:
            conn.close()

    def initialize(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS news_articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider TEXT NOT NULL,
            article_id TEXT NOT NULL,
            headline TEXT NOT NULL,
            normalized_headline TEXT NOT NULL,
            headline_hash TEXT NOT NULL,
            body TEXT,
            published_at TEXT NOT NULL,
            received_at TEXT NOT NULL,
            provider_published_at TEXT,
            source_accepted_at TEXT,
            first_seen_at TEXT,
            content_updated_at TEXT,
            timestamp_quality TEXT NOT NULL DEFAULT 'receipt_only',
            timestamp_source TEXT NOT NULL DEFAULT 'received_at',
            primary_symbol TEXT NOT NULL,
            con_id INTEGER,
            extra_data TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            UNIQUE(provider, article_id)
        );

        CREATE INDEX IF NOT EXISTS idx_news_hash_time
            ON news_articles(headline_hash, received_at);
        CREATE INDEX IF NOT EXISTS idx_news_symbol_time
            ON news_articles(primary_symbol, received_at);

        CREATE TABLE IF NOT EXISTS news_symbols (
            news_id INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            con_id INTEGER,
            first_seen_at TEXT NOT NULL,
            PRIMARY KEY(news_id, symbol),
            FOREIGN KEY(news_id) REFERENCES news_articles(id)
        );

        CREATE INDEX IF NOT EXISTS idx_news_symbols_symbol
            ON news_symbols(symbol, news_id);

        CREATE TABLE IF NOT EXISTS news_duplicates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            canonical_news_id INTEGER NOT NULL,
            duplicate_provider TEXT NOT NULL,
            duplicate_article_id TEXT NOT NULL,
            duplicate_headline TEXT NOT NULL,
            duplicate_received_at TEXT NOT NULL,
            reason TEXT NOT NULL,
            similarity REAL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(canonical_news_id) REFERENCES news_articles(id),
            UNIQUE(duplicate_provider, duplicate_article_id)
        );

        CREATE TABLE IF NOT EXISTS news_providers (
            code TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            seen_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS market_baselines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            news_id INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            captured_at TEXT NOT NULL,
            bid REAL,
            ask REAL,
            last REAL,
            midpoint REAL,
            selected_price REAL,
            selected_price_source TEXT,
            volume REAL,
            currency TEXT NOT NULL DEFAULT 'unknown',
            con_id INTEGER,
            contract_symbol TEXT NOT NULL DEFAULT '',
            sec_type TEXT NOT NULL DEFAULT '',
            exchange TEXT NOT NULL DEFAULT '',
            primary_exchange TEXT NOT NULL DEFAULT '',
            multiplier TEXT NOT NULL DEFAULT '',
            market_data_type INTEGER,
            quote_integrity_status TEXT NOT NULL DEFAULT 'not_checked',
            quote_integrity_reasons_json TEXT NOT NULL DEFAULT '[]',
            reaction_baseline_eligible INTEGER NOT NULL DEFAULT 0,
            spread_ratio REAL,
            last_midpoint_deviation REAL,
            close_reference_deviation REAL,
            FOREIGN KEY(news_id) REFERENCES news_articles(id),
            UNIQUE(news_id, symbol)
        );

        CREATE TABLE IF NOT EXISTS reaction_targets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            news_id INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            horizon TEXT NOT NULL,
            due_at TEXT NOT NULL,
            next_attempt_at TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            updated_at TEXT NOT NULL,
            usage_status TEXT NOT NULL DEFAULT 'provisional',
            usage_reason TEXT NOT NULL DEFAULT '',
            governance_analysis_id INTEGER,
            calibration_eligible INTEGER NOT NULL DEFAULT 0,
            observation_eligible INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY(news_id) REFERENCES news_articles(id),
            UNIQUE(news_id, symbol, horizon)
        );

        CREATE INDEX IF NOT EXISTS idx_targets_due
            ON reaction_targets(status, due_at);

        CREATE TABLE IF NOT EXISTS market_reactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            target_id INTEGER NOT NULL UNIQUE,
            news_id INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            horizon TEXT NOT NULL,
            due_at TEXT NOT NULL,
            captured_at TEXT NOT NULL,
            baseline_price REAL NOT NULL,
            reaction_price REAL NOT NULL,
            raw_return REAL NOT NULL,
            price_source TEXT NOT NULL,
            created_at TEXT NOT NULL,
            usage_status TEXT NOT NULL DEFAULT 'provisional',
            usage_reason TEXT NOT NULL DEFAULT '',
            governance_analysis_id INTEGER,
            calibration_eligible INTEGER NOT NULL DEFAULT 0,
            observation_eligible INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY(target_id) REFERENCES reaction_targets(id),
            FOREIGN KEY(news_id) REFERENCES news_articles(id)
        );
        """
        with self.connect() as conn:
            conn.executescript(schema)
            news_columns = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(news_articles)").fetchall()
            }
            provenance_columns = {
                "provider_published_at": "TEXT",
                "source_accepted_at": "TEXT",
                "first_seen_at": "TEXT",
                "content_updated_at": "TEXT",
                "timestamp_quality": "TEXT NOT NULL DEFAULT 'receipt_only'",
                "timestamp_source": "TEXT NOT NULL DEFAULT 'received_at'",
            }
            for column, definition in provenance_columns.items():
                if column not in news_columns:
                    conn.execute(f"ALTER TABLE news_articles ADD COLUMN {column} {definition}")
            # Historical rows have no trustworthy publication provenance.  Do not
            # infer it from a legacy published_at value that may have been filled
            # from receipt time by an upstream provider.
            conn.execute(
                "UPDATE news_articles SET first_seen_at=received_at "
                "WHERE first_seen_at IS NULL OR first_seen_at=''"
            )
            conn.execute(
                "UPDATE news_articles SET timestamp_quality='receipt_only',timestamp_source='received_at' "
                "WHERE timestamp_quality IS NULL OR timestamp_quality=''"
            )
            columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(market_baselines)").fetchall()}
            if "currency" not in columns:
                conn.execute("ALTER TABLE market_baselines ADD COLUMN currency TEXT NOT NULL DEFAULT 'unknown'")
            baseline_columns = {
                "con_id": "INTEGER",
                "contract_symbol": "TEXT NOT NULL DEFAULT ''",
                "sec_type": "TEXT NOT NULL DEFAULT ''",
                "exchange": "TEXT NOT NULL DEFAULT ''",
                "primary_exchange": "TEXT NOT NULL DEFAULT ''",
                "multiplier": "TEXT NOT NULL DEFAULT ''",
                "market_data_type": "INTEGER",
                "quote_integrity_status": "TEXT NOT NULL DEFAULT 'not_checked'",
                "quote_integrity_reasons_json": "TEXT NOT NULL DEFAULT '[]'",
                "reaction_baseline_eligible": "INTEGER NOT NULL DEFAULT 0",
                "spread_ratio": "REAL",
                "last_midpoint_deviation": "REAL",
                "close_reference_deviation": "REAL",
            }
            columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(market_baselines)").fetchall()}
            for column, definition in baseline_columns.items():
                if column not in columns:
                    conn.execute(f"ALTER TABLE market_baselines ADD COLUMN {column} {definition}")

            target_columns = {
                "next_attempt_at": "TEXT",
                "usage_status": "TEXT NOT NULL DEFAULT 'provisional'",
                "usage_reason": "TEXT NOT NULL DEFAULT ''",
                "governance_analysis_id": "INTEGER",
                "calibration_eligible": "INTEGER NOT NULL DEFAULT 0",
                "observation_eligible": "INTEGER NOT NULL DEFAULT 0",
            }
            columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(reaction_targets)").fetchall()}
            for column, definition in target_columns.items():
                if column not in columns:
                    conn.execute(f"ALTER TABLE reaction_targets ADD COLUMN {column} {definition}")
            columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(market_reactions)").fetchall()}
            for column, definition in target_columns.items():
                if column not in columns:
                    conn.execute(f"ALTER TABLE market_reactions ADD COLUMN {column} {definition}")
            conn.execute("UPDATE reaction_targets SET next_attempt_at=due_at WHERE next_attempt_at IS NULL")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_targets_usage_due ON reaction_targets(usage_status,status,next_attempt_at)")
            ensure_news_lineage_v2_schema(conn)

    def get_news_by_identity(self, provider: str, article_id: str):
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM news_articles WHERE provider=? AND article_id=?",
                (provider, article_id),
            ).fetchone()

    def get_recent_news_by_hash(
        self, headline_hash: str, received_at: datetime, hours: int
    ):
        cutoff = received_at - timedelta(hours=hours)
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT * FROM news_articles
                WHERE headline_hash=? AND received_at>=? AND received_at<=?
                ORDER BY received_at DESC LIMIT 1
                """,
                (headline_hash, utc_iso(cutoff), utc_iso(received_at)),
            ).fetchone()

    def get_recent_headlines(
        self,
        received_at: datetime,
        minutes: int,
        symbol: Optional[str] = None,
    ) -> Sequence[sqlite3.Row]:
        cutoff = received_at - timedelta(minutes=minutes)
        sql = """
            SELECT id, provider, article_id, headline, normalized_headline,
                   received_at, primary_symbol
            FROM news_articles
            WHERE received_at>=? AND received_at<=?
        """
        params: list[object] = [utc_iso(cutoff), utc_iso(received_at)]
        if symbol:
            sql += " AND primary_symbol=?"
            params.append(symbol)
        sql += " ORDER BY received_at DESC"
        with self.connect() as conn:
            return conn.execute(sql, params).fetchall()

    def insert_news(
        self, item: NewsInput, normalized_headline: str, headline_hash: str
    ) -> int:
        now = utc_iso(datetime.now(UTC))
        provider_published_at = (
            utc_iso(item.provider_published_at)
            if item.provider_published_at is not None
            else None
        )
        source_accepted_at = (
            utc_iso(item.source_accepted_at)
            if item.source_accepted_at is not None
            else None
        )
        first_seen_at = utc_iso(item.first_seen_at or item.received_at)
        content_updated_at = (
            utc_iso(item.content_updated_at)
            if item.content_updated_at is not None
            else None
        )
        if source_accepted_at:
            timestamp_quality = "source_accepted"
            timestamp_source = "source_accepted_at"
        elif provider_published_at:
            timestamp_quality = "provider_published"
            timestamp_source = "provider_published_at"
        else:
            timestamp_quality = "receipt_only"
            timestamp_source = "received_at"
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO news_articles(
                    provider, article_id, headline, normalized_headline,
                    headline_hash, published_at, received_at,provider_published_at,
                    source_accepted_at,first_seen_at,content_updated_at,
                    timestamp_quality,timestamp_source,primary_symbol,
                    con_id, extra_data, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.provider,
                    item.article_id,
                    item.headline,
                    normalized_headline,
                    headline_hash,
                    utc_iso(item.published_at),
                    utc_iso(item.received_at),
                    provider_published_at,
                    source_accepted_at,
                    first_seen_at,
                    content_updated_at,
                    timestamp_quality,
                    timestamp_source,
                    item.primary_symbol,
                    item.con_id,
                    item.extra_data,
                    now,
                ),
            )
            news_id = int(cursor.lastrowid)
            sync_legacy_news_article_to_raw(conn, news_id)
            return news_id

    def add_news_symbol(
        self, news_id: int, symbol: str, con_id: Optional[int], seen_at: datetime
    ) -> bool:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO news_symbols(
                    news_id, symbol, con_id, first_seen_at
                ) VALUES (?, ?, ?, ?)
                """,
                (news_id, symbol, con_id, utc_iso(seen_at)),
            )
            return cursor.rowcount > 0

    def insert_duplicate(
        self,
        canonical_news_id: int,
        item: NewsInput,
        reason: str,
        similarity: Optional[float],
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO news_duplicates(
                    canonical_news_id, duplicate_provider, duplicate_article_id,
                    duplicate_headline, duplicate_received_at, reason,
                    similarity, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    canonical_news_id,
                    item.provider,
                    item.article_id,
                    item.headline,
                    utc_iso(item.received_at),
                    reason,
                    similarity,
                    utc_iso(datetime.now(UTC)),
                ),
            )

    def update_article_body(
        self, news_id: int, body: str, *, updated_at: datetime | None = None
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE news_articles SET body=?,content_updated_at=? WHERE id=?",
                (body, utc_iso(updated_at or datetime.now(UTC)), news_id),
            )

    def upsert_provider(self, code: str, name: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO news_providers(code, name, seen_at)
                VALUES (?, ?, ?)
                ON CONFLICT(code) DO UPDATE SET name=excluded.name,
                    seen_at=excluded.seen_at
                """,
                (code, name, utc_iso(datetime.now(UTC))),
            )

    def insert_baseline(
        self,
        news_id: int,
        quote: QuoteSnapshot,
        integrity: QuoteIntegrityResult | None = None,
    ) -> bool:
        integrity = integrity or assess_quote_integrity(quote)
        raw_price, raw_source, raw_selected_at = quote.selected_price_with_time()
        selected_price = integrity.selected_price if integrity.eligible else None
        selected_source = integrity.selected_source if integrity.eligible else None
        selected_at = integrity.selected_at or raw_selected_at or quote.timestamp
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO market_baselines(
                    news_id, symbol, captured_at, bid, ask, last, midpoint,
                    selected_price, selected_price_source, volume, currency,
                    con_id,contract_symbol,sec_type,exchange,primary_exchange,multiplier,
                    market_data_type,quote_integrity_status,quote_integrity_reasons_json,
                    reaction_baseline_eligible,spread_ratio,last_midpoint_deviation,
                    close_reference_deviation
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    news_id,
                    quote.symbol,
                    utc_iso(selected_at),
                    quote.bid,
                    quote.ask,
                    quote.last,
                    quote.midpoint,
                    selected_price,
                    selected_source,
                    quote.volume,
                    str(quote.currency or "unknown").upper(),
                    quote.con_id,
                    str(quote.contract_symbol or "").upper(),
                    str(quote.sec_type or "").upper(),
                    str(quote.exchange or "").upper(),
                    str(quote.primary_exchange or "").upper(),
                    str(quote.multiplier or ""),
                    quote.market_data_type,
                    integrity.status,
                    json.dumps(list(integrity.reasons), ensure_ascii=False),
                    int(integrity.eligible),
                    integrity.spread_ratio,
                    integrity.last_midpoint_deviation,
                    integrity.close_reference_deviation,
                ),
            )
        return integrity.eligible and selected_price is not None

    def create_reaction_target(
        self, news_id: int, symbol: str, horizon: str, due_at: datetime
    ) -> None:
        now = utc_iso(datetime.now(UTC))
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO reaction_targets(
                    news_id, symbol, horizon, due_at, next_attempt_at, status, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', ?)
                """,
                (news_id, symbol, horizon, utc_iso(due_at), utc_iso(due_at), now),
            )

    def get_due_targets(self, now: datetime, limit: int = 100):
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT rt.*,
                       CASE WHEN mb.reaction_baseline_eligible=1
                            THEN mb.selected_price ELSE NULL END AS baseline_price
                FROM reaction_targets rt
                LEFT JOIN market_baselines mb
                  ON mb.news_id=rt.news_id AND mb.symbol=rt.symbol
                WHERE rt.status='pending' AND rt.usage_status!='excluded' AND COALESCE(rt.next_attempt_at,rt.due_at)<=?
                ORDER BY rt.due_at ASC LIMIT ?
                """,
                (utc_iso(now), limit),
            ).fetchall()

    def schedule_target_retry(
        self,
        target_id: int,
        message: str,
        *,
        retry_at: datetime,
        max_attempts: int,
    ) -> str:
        now = utc_iso(datetime.now(UTC))
        with self.connect() as conn:
            row = conn.execute(
                "SELECT attempts,status,usage_status FROM reaction_targets WHERE id=?", (target_id,)
            ).fetchone()
            if row is None:
                return "missing"
            if str(row["status"]) != "pending" or str(row["usage_status"]) == "excluded":
                return "ignored"
            attempts = int(row["attempts"]) + 1
            status = "failed" if attempts >= max_attempts else "pending"
            conn.execute(
                """UPDATE reaction_targets SET attempts=?,status=?,last_error=?,next_attempt_at=?,updated_at=?
                   WHERE id=? AND status='pending' AND usage_status!='excluded'""",
                (attempts, status, message[:500], utc_iso(retry_at), now, target_id),
            )
            return status

    def mark_target_terminal(self, target_id: int, status: str, message: str) -> None:
        if status not in {"missed", "failed", "suppressed"}:
            raise ValueError(f"Invalid terminal reaction target status: {status}")
        with self.connect() as conn:
            conn.execute(
                """UPDATE reaction_targets SET status=?,attempts=attempts+1,last_error=?,updated_at=?
                   WHERE id=?""",
                (status, message[:500], utc_iso(datetime.now(UTC)), target_id),
            )

    def finalize_overdue_targets(self, *, now: datetime, max_lateness_seconds: int) -> int:
        cutoff = utc_iso(now - timedelta(seconds=max_lateness_seconds))
        with self.connect() as conn:
            cursor = conn.execute(
                """UPDATE reaction_targets SET status='missed',
                       last_error='historical backfill required; evaluation window expired',updated_at=?
                   WHERE status='pending' AND usage_status!='excluded' AND due_at<?""",
                (utc_iso(now), cutoff),
            )
            return int(cursor.rowcount)

    def complete_target(
        self,
        target_id: int,
        news_id: int,
        symbol: str,
        horizon: str,
        due_at: datetime,
        captured_at: datetime,
        baseline_price: float,
        reaction_price: float,
        price_source: str,
    ) -> None:
        raw_return = (reaction_price / baseline_price) - 1.0
        now = utc_iso(datetime.now(UTC))
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            governance = conn.execute(
                """SELECT status,usage_status,usage_reason,governance_analysis_id,
                          calibration_eligible,observation_eligible
                   FROM reaction_targets WHERE id=?""",
                (target_id,),
            ).fetchone()
            if governance is None:
                conn.execute("ROLLBACK")
                return
            if str(governance["status"]) != "pending" or str(governance["usage_status"]) == "excluded":
                if str(governance["status"]) == "pending":
                    conn.execute(
                        "UPDATE reaction_targets SET status='suppressed',updated_at=? WHERE id=?",
                        (now, target_id),
                    )
                conn.execute("COMMIT")
                return
            usage_status = str(governance["usage_status"])
            usage_reason = str(governance["usage_reason"] or "")
            governance_analysis_id = governance["governance_analysis_id"] if governance else None
            calibration_eligible = int(governance["calibration_eligible"] if governance else 0)
            observation_eligible = int(governance["observation_eligible"] if governance else 0)
            conn.execute(
                """
                INSERT OR IGNORE INTO market_reactions(
                    target_id, news_id, symbol, horizon, due_at, captured_at,
                    baseline_price, reaction_price, raw_return,
                    price_source, created_at,usage_status,usage_reason,governance_analysis_id,
                    calibration_eligible,observation_eligible
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    target_id,
                    news_id,
                    symbol,
                    horizon,
                    utc_iso(due_at),
                    utc_iso(captured_at),
                    baseline_price,
                    reaction_price,
                    raw_return,
                    price_source,
                    now,
                    usage_status,
                    usage_reason,
                    governance_analysis_id,
                    calibration_eligible,
                    observation_eligible,
                ),
            )
            conn.execute(
                """
                UPDATE reaction_targets
                SET status='complete', last_error=NULL, next_attempt_at=NULL, updated_at=?
                WHERE id=?
                """,
                (now, target_id),
            )
            conn.execute("COMMIT")

    def apply_reaction_governance(
        self,
        *,
        news_id: int,
        analysis_id: int,
        usage_status: str,
        reason: str,
    ) -> None:
        if usage_status not in {"eligible", "observation_only", "excluded"}:
            raise ValueError(f"Invalid reaction usage status: {usage_status}")
        calibration = int(usage_status == "eligible")
        observation = int(usage_status in {"eligible", "observation_only"})
        now = utc_iso(datetime.now(UTC))
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                UPDATE reaction_targets
                SET usage_status=?,usage_reason=?,governance_analysis_id=?,
                    calibration_eligible=?,observation_eligible=?,updated_at=?,
                    status=CASE
                        WHEN ?='excluded' AND status='pending' THEN 'suppressed'
                        WHEN ?!='excluded' AND status='suppressed' THEN 'pending'
                        ELSE status END
                WHERE news_id=?
                """,
                (usage_status, reason[:500], analysis_id, calibration, observation, now,
                 usage_status, usage_status, news_id),
            )
            conn.execute(
                """
                UPDATE market_reactions
                SET usage_status=?,usage_reason=?,governance_analysis_id=?,
                    calibration_eligible=?,observation_eligible=?
                WHERE news_id=?
                """,
                (usage_status, reason[:500], analysis_id, calibration, observation, news_id),
            )
            conn.execute("COMMIT")

    def reaction_usage_summary(self) -> dict[str, int]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT usage_status,COUNT(*) n FROM reaction_targets GROUP BY usage_status"
            ).fetchall()
            return {str(row["usage_status"]): int(row["n"]) for row in rows}

    def list_providers(self):
        with self.connect() as conn:
            return conn.execute(
                "SELECT code, name, seen_at FROM news_providers ORDER BY code"
            ).fetchall()

    def summary(self) -> dict[str, int]:
        with self.connect() as conn:
            return {
                "news": conn.execute("SELECT COUNT(*) FROM news_articles").fetchone()[0],
                "duplicates": conn.execute(
                    "SELECT COUNT(*) FROM news_duplicates"
                ).fetchone()[0],
                "news_symbol_links": conn.execute(
                    "SELECT COUNT(*) FROM news_symbols"
                ).fetchone()[0],
                "baselines": conn.execute(
                    "SELECT COUNT(*) FROM market_baselines"
                ).fetchone()[0],
                "pending_targets": conn.execute(
                    "SELECT COUNT(*) FROM reaction_targets WHERE status='pending'"
                ).fetchone()[0],
                "suppressed_targets": conn.execute(
                    "SELECT COUNT(*) FROM reaction_targets WHERE status='suppressed'"
                ).fetchone()[0],
                "missed_targets": conn.execute(
                    "SELECT COUNT(*) FROM reaction_targets WHERE status='missed'"
                ).fetchone()[0],
                "failed_targets": conn.execute(
                    "SELECT COUNT(*) FROM reaction_targets WHERE status='failed'"
                ).fetchone()[0],
                "completed_reactions": conn.execute(
                    "SELECT COUNT(*) FROM market_reactions"
                ).fetchone()[0],
            }

    def recent_news(self, limit: int = 20):
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT id, provider, article_id, primary_symbol, headline,
                       published_at, received_at
                FROM news_articles ORDER BY received_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
