from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


GEOPOLITICAL_INTELLIGENCE_VERSION = "geopolitical-intelligence-v1"
GEOPOLITICAL_OUTCOMES_VERSION = "geopolitical-signal-outcomes-v1"
GEOPOLITICAL_ADAPTIVE_MAP_VERSION = "geopolitical-adaptive-asset-map-v1"
GEOPOLITICAL_HEATMAP_VERSION = "geopolitical-desk-heatmap-v1"

DEFAULT_GEOPOLITICAL_HORIZONS = (5, 15, 30, 390)
DEFAULT_GEOPOLITICAL_SYMBOLS = ("SPY", "QQQ", "IWM", "DIA", "SMH", "SOXX", "USO", "XLE", "GLD", "VIX")

GEOPOLITICAL_RISK_UP_ASSETS = {
    "VIX",
    "VXX",
    "UVXY",
    "GLD",
    "UUP",
    "USO",
    "XLE",
    "XOM",
    "CVX",
    "WEAT",
    "DBA",
    "ITA",
    "XAR",
}
GEOPOLITICAL_RISK_DOWN_ASSETS = {
    "SPY",
    "QQQ",
    "IWM",
    "DIA",
    "SMH",
    "SOXX",
    "TSM",
    "NVDA",
    "EWY",
}

MARKETAUX_GEOPOLITICAL_OR_POLICY_RE = re.compile(
    r"\b(?:geopolitical|war|missile|airstrike|sanctions?|tariffs?|trade war|"
    r"export controls?|military|invasion|ceasefire|nato|red sea|hormuz|"
    r"drone|airspace|blockade|cyberattack|ransomware|critical minerals|"
    r"taiwan|china|russia|ukraine|iran|israel|north korea|middle east|"
    r"oil|crude|brent|wti|shipping|supply chain|semiconductor restriction|"
    r"chip restriction)\b",
    re.I,
)
MARKETAUX_HARD_EVENT_RE = re.compile(
    r"\b(?:war|missile|airstrike|sanctions?|tariffs?|trade war|export controls?|"
    r"invasion|ceasefire|nato|red sea|hormuz|drone|airspace|blockade|cyberattack|"
    r"ransomware|critical minerals|taiwan|china|russia|ukraine|iran|israel|"
    r"north korea|middle east|oil|crude|brent|wti|shipping|semiconductor restriction|"
    r"chip restriction)\b",
    re.I,
)
GEOPOLITICAL_HARD_EVENT_RE = re.compile(
    r"\b(?:war|missile|airstrike|sanctions?|tariffs?|trade war|export controls?|"
    r"invasion|ceasefire|nato|red sea|hormuz|drone|airspace|blockade|cyberattack|"
    r"ransomware|critical minerals|taiwan|china|russia|ukraine|iran|israel|"
    r"north korea|south korea|korea peninsula|middle east|gaza|houthi|lebanon|"
    r"yemen|black sea|oil supply|shipping chokepoint|semiconductor restriction|"
    r"chip restriction|military drill|strait)\b",
    re.I,
)
MACRO_POLICY_EVENT_RE = re.compile(
    r"\b(?:cpi|pce|payrolls?|jobs? report|unemployment|claims|fed|fomc|treasury|"
    r"yield|inflation|rate decision|rates?|gdp|ism|pmi|consumer sentiment|"
    r"michigan sentiment|retail sales)\b",
    re.I,
)
MARKET_NEWS_NOISE_RE = re.compile(
    r"\b(?:shares are trading|stock is trading|stock rallies|stock falls|"
    r"stock portfolio|stock picks?|best .*stocks? to buy|according to analysts|"
    r"hedge fund|price target|etf to buy|would be worth|dividend|"
    r"contract award|share repurchase|buyback|merger at special meeting)\b",
    re.I,
)
MARKETAUX_LOW_SIGNAL_RE = re.compile(
    r"\b(?:best\s+\w*\s*stocks?\s+to\s+buy|is\s+.+?\s+a\s+good\s+stock\s+to\s+buy|"
    r"stock\s+portfolio|stock\s+picks?|according\s+to\s+analysts|hedge\s+fund|"
    r"dividend\s+stocks?|penny\s+stocks?|zacks|motley\s+fool|insider\s+monkey|"
    r"price\s+target\s+roundup|wall\s+street\s+analysts?|top\s+\d+\s+stocks?)\b",
    re.I,
)

GEOPOLITICAL_THEMES = {
    "broad_geopolitical",
    "energy_supply",
    "shipping_chokepoint",
    "trade_conflict",
    "military_escalation",
    "food_supply",
    "defense",
    "cybersecurity",
    "semiconductor_supply_chain",
}
GEOPOLITICAL_REGIONS = {"middle_east", "taiwan_china", "korea_peninsula", "russia_ukraine", "latin_america"}


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _parse_dt(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _safe_json(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    try:
        parsed = json.loads(str(value))
    except Exception:
        return default
    return parsed


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    try:
        return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())
    except sqlite3.Error:
        return False


def _parse_horizons(value: str | list[int] | tuple[int, ...]) -> list[int]:
    if isinstance(value, (list, tuple)):
        parts = value
    else:
        parts = str(value or ",".join(str(item) for item in DEFAULT_GEOPOLITICAL_HORIZONS)).replace(";", ",").split(",")
    horizons: list[int] = []
    for part in parts:
        try:
            parsed = int(str(part).strip())
        except ValueError:
            continue
        if parsed > 0 and parsed not in horizons:
            horizons.append(parsed)
    return horizons or list(DEFAULT_GEOPOLITICAL_HORIZONS)


def _connect_from_db(db: Any) -> Any:
    return db.connect()


def signal_scope(row: dict[str, Any]) -> str:
    region = str(row.get("region") or "").lower()
    theme = str(row.get("theme") or "").lower()
    source = str(row.get("source") or "").lower()
    title_text = str(row.get("title") or "")
    article_text = _marketaux_article_text(row) if source == "marketaux" else title_text
    text = f"{title_text} {article_text}".strip()

    if MACRO_POLICY_EVENT_RE.search(text) and not GEOPOLITICAL_HARD_EVENT_RE.search(text):
        return "macro_policy"
    if MARKET_NEWS_NOISE_RE.search(text) and region in {"", "global", "unknown"} and not GEOPOLITICAL_HARD_EVENT_RE.search(text):
        return "emergency_market" if source == "emergency_breaking_news" else "market_noise"
    if source == "marketaux":
        if MARKETAUX_LOW_SIGNAL_RE.search(article_text) and not MARKETAUX_HARD_EVENT_RE.search(article_text):
            return "market_noise"
        if region in GEOPOLITICAL_REGIONS and (theme in GEOPOLITICAL_THEMES or GEOPOLITICAL_HARD_EVENT_RE.search(article_text)):
            return "geopolitical"
        if GEOPOLITICAL_HARD_EVENT_RE.search(article_text) and (theme in GEOPOLITICAL_THEMES or region not in {"", "global", "unknown"}):
            return "geopolitical"
        if MARKETAUX_GEOPOLITICAL_OR_POLICY_RE.search(article_text):
            return "geo_watch"
        return "market_noise"
    if source == "emergency_breaking_news":
        if region in GEOPOLITICAL_REGIONS and (theme in GEOPOLITICAL_THEMES or GEOPOLITICAL_HARD_EVENT_RE.search(text)):
            return "geopolitical"
        if GEOPOLITICAL_HARD_EVENT_RE.search(text) and theme in GEOPOLITICAL_THEMES:
            return "geopolitical"
        return "emergency_market"
    if region in GEOPOLITICAL_REGIONS and theme in GEOPOLITICAL_THEMES:
        return "geopolitical"
    if GEOPOLITICAL_HARD_EVENT_RE.search(text) and theme in GEOPOLITICAL_THEMES:
        return "geopolitical"
    return "macro_policy" if MACRO_POLICY_EVENT_RE.search(text) else "market_noise"


def _is_geopolitical_signal(row: dict[str, Any]) -> bool:
    return signal_scope(row) == "geopolitical"


def _marketaux_article_text(row: dict[str, Any]) -> str:
    raw = _safe_json(row.get("raw_json"), {})
    chunks = [str(row.get("title") or "")]
    if isinstance(raw, dict):
        for key in ("description", "snippet", "body", "summary"):
            if raw.get(key):
                chunks.append(str(raw.get(key)))
        source = raw.get("source")
        if isinstance(source, str):
            chunks.append(source)
        elif isinstance(source, dict):
            chunks.append(str(source.get("name") or source.get("domain") or ""))
        for entity in raw.get("entities", []) or []:
            if isinstance(entity, dict):
                chunks.append(str(entity.get("name") or ""))
                chunks.append(str(entity.get("symbol") or ""))
    return " ".join(chunk for chunk in chunks if chunk).strip()


def _assets_from_json(value: Any) -> list[str]:
    raw = _safe_json(value, [])
    assets: list[str] = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, dict):
            candidate = item.get("symbol") or item.get("asset") or item.get("ticker")
        else:
            candidate = item
        text = str(candidate or "").strip().upper()
        if text and text not in assets:
            assets.append(text)
    return assets


def _urgency_from_row(row: dict[str, Any]) -> str:
    raw = _safe_json(row.get("raw_json"), {})
    urgency = raw.get("urgency") if isinstance(raw, dict) else {}
    if isinstance(urgency, dict):
        value = str(urgency.get("urgency") or "").strip().lower()
        if value:
            return value
    return "ordinary"


def _expected_direction(region: str, theme: str, symbol: str, sentiment_score: float | None) -> str:
    symbol = str(symbol or "").upper()
    theme = str(theme or "").lower()
    sentiment = sentiment_score if sentiment_score is not None else -0.1
    easing_theme = any(token in theme for token in ("deescalation", "easing", "peace"))
    if easing_theme or sentiment > 0.35:
        if symbol in GEOPOLITICAL_RISK_DOWN_ASSETS:
            return "up"
        if symbol in GEOPOLITICAL_RISK_UP_ASSETS:
            return "down"
    if symbol in GEOPOLITICAL_RISK_UP_ASSETS:
        return "up"
    if symbol in GEOPOLITICAL_RISK_DOWN_ASSETS:
        return "down"
    return "neutral"


def _realized_direction(raw_return: float | None, *, flat_threshold_pct: float = 0.05) -> str:
    if raw_return is None:
        return "unknown"
    threshold = max(0.0, float(flat_threshold_pct)) / 100.0
    if raw_return > threshold:
        return "up"
    if raw_return < -threshold:
        return "down"
    return "flat"


def _direction_hit(expected: str, actual: str) -> int | None:
    if expected not in {"up", "down"} or actual == "unknown":
        return None
    if actual == "flat":
        return 0
    return 1 if expected == actual else 0


def ensure_geopolitical_intelligence_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS geopolitical_signal_outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_key TEXT NOT NULL,
            signal_id INTEGER,
            source TEXT NOT NULL,
            external_id TEXT NOT NULL DEFAULT '',
            region TEXT NOT NULL DEFAULT 'global',
            theme TEXT NOT NULL DEFAULT 'unknown',
            urgency TEXT NOT NULL DEFAULT 'ordinary',
            symbol TEXT NOT NULL,
            horizon_minutes INTEGER NOT NULL,
            event_at TEXT NOT NULL,
            due_at TEXT NOT NULL,
            baseline_price REAL,
            baseline_source TEXT,
            outcome_at TEXT,
            outcome_price REAL,
            raw_return REAL,
            signed_return REAL,
            expected_direction TEXT NOT NULL DEFAULT 'neutral',
            actual_direction TEXT NOT NULL DEFAULT 'unknown',
            direction_hit INTEGER,
            status TEXT NOT NULL,
            blocker TEXT,
            payload_json TEXT NOT NULL DEFAULT '{}',
            outcome_version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(snapshot_key, symbol, horizon_minutes)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_geopolitical_signal_outcomes_status
        ON geopolitical_signal_outcomes(status,due_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_geopolitical_signal_outcomes_group
        ON geopolitical_signal_outcomes(region,theme,symbol,horizon_minutes,status)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS geopolitical_adaptive_asset_map (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            region TEXT NOT NULL,
            theme TEXT NOT NULL,
            symbol TEXT NOT NULL,
            sample_count INTEGER NOT NULL DEFAULT 0,
            evaluated_count INTEGER NOT NULL DEFAULT 0,
            avg_abs_return REAL,
            avg_signed_return REAL,
            direction_hit_rate REAL,
            adaptive_weight REAL NOT NULL DEFAULT 0,
            usage_tier TEXT NOT NULL DEFAULT 'collect_more',
            last_evaluated_at TEXT NOT NULL DEFAULT '',
            map_version TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(region,theme,symbol,map_version)
        )
        """
    )
    conn.commit()


def _price_observations(conn: sqlite3.Connection, symbols: set[str]) -> dict[str, list[dict[str, Any]]]:
    if not symbols:
        return {}
    placeholders = ",".join("?" for _ in sorted(symbols))
    params = tuple(sorted(symbols))
    observations: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for table, time_col, price_col, source_col in (
        ("market_baselines", "captured_at", "selected_price", "selected_price_source"),
        ("market_reactions", "captured_at", "reaction_price", "price_source"),
    ):
        if not _table_exists(conn, table):
            continue
        try:
            rows = conn.execute(
                f"""
                SELECT symbol, {time_col} AS at, {price_col} AS price, {source_col} AS source
                  FROM {table}
                 WHERE symbol IN ({placeholders}) AND {price_col} IS NOT NULL
                """,
                params,
            ).fetchall()
        except sqlite3.Error:
            continue
        for row in rows:
            at = _parse_dt(row["at"])
            price = _as_float(row["price"])
            if at is not None and price is not None:
                observations[str(row["symbol"]).upper()].append({"at": at, "price": price, "source": row["source"] or table})
    for symbol, rows in observations.items():
        observations[symbol] = sorted(rows, key=lambda item: item["at"])
    return observations


def _select_baseline(rows: list[dict[str, Any]], event_at: datetime) -> dict[str, Any] | None:
    selected = None
    for row in rows:
        if row["at"] <= event_at:
            selected = row
        else:
            break
    return selected


def _select_outcome(rows: list[dict[str, Any]], due_at: datetime, *, max_lag_minutes: float) -> dict[str, Any] | None:
    max_dt = due_at + timedelta(minutes=max(0.0, float(max_lag_minutes)))
    for row in rows:
        if due_at <= row["at"] <= max_dt:
            return row
    return None


def _signal_snapshot_key(row: dict[str, Any], symbol: str) -> str:
    raw = json.dumps(
        {
            "source": row.get("source"),
            "external_id": row.get("external_id"),
            "id": row.get("id"),
            "published_at": row.get("published_at"),
            "symbol": symbol,
            "version": GEOPOLITICAL_OUTCOMES_VERSION,
        },
        sort_keys=True,
        ensure_ascii=True,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _external_signal_rows(conn: sqlite3.Connection, *, since: datetime, until: datetime, limit: int) -> list[dict[str, Any]]:
    if not _table_exists(conn, "external_market_signals"):
        return []
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT id,source,external_id,title,published_at,region,theme,sentiment_score,
                   sentiment_label,confidence,assets_json,market_reaction_status,
                   prediction_gate_status,raw_json,created_at,updated_at
              FROM external_market_signals
             WHERE published_at!='' AND published_at>=? AND published_at<=?
             ORDER BY published_at DESC,id DESC
             LIMIT ?
            """,
            (since.isoformat(), until.isoformat(), max(1, int(limit))),
        ).fetchall()
    ]


def _signal_rows_to_record(conn: sqlite3.Connection, *, since: datetime, until: datetime, limit: int) -> list[dict[str, Any]]:
    rows = _external_signal_rows(conn, since=since, until=until, limit=limit)
    return [row for row in rows if _is_geopolitical_signal(row)]


def _record_signal_outcomes(
    conn: sqlite3.Connection,
    *,
    rows: list[dict[str, Any]],
    horizons: list[int],
    observations: dict[str, list[dict[str, Any]]],
    max_assets_per_signal: int,
    now_iso: str,
) -> dict[str, Any]:
    recorded: list[dict[str, Any]] = []
    attempted_count = 0
    inserted_count = 0
    duplicate_skipped_count = 0
    for row in rows:
        event_at = _parse_dt(row.get("published_at"))
        if event_at is None:
            continue
        assets = _assets_from_json(row.get("assets_json"))[: max(1, int(max_assets_per_signal))]
        for symbol in assets:
            baseline = _select_baseline(observations.get(symbol) or [], event_at)
            expected = _expected_direction(str(row.get("region") or ""), str(row.get("theme") or ""), symbol, _as_float(row.get("sentiment_score")))
            for horizon in horizons:
                due_at = event_at + timedelta(minutes=int(horizon))
                status = "pending" if baseline else "baseline_unavailable"
                blocker = None if baseline else "baseline_unavailable"
                payload = {
                    "title": row.get("title"),
                    "sentiment_label": row.get("sentiment_label"),
                    "confidence": row.get("confidence"),
                    "prediction_gate_status": row.get("prediction_gate_status"),
                    "market_reaction_status": row.get("market_reaction_status"),
                    "recording_note": "report_only_geopolitical_learning_log_no_threshold_mutation",
                }
                attempted_count += 1
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO geopolitical_signal_outcomes (
                        snapshot_key,signal_id,source,external_id,region,theme,urgency,symbol,
                        horizon_minutes,event_at,due_at,baseline_price,baseline_source,
                        expected_direction,status,blocker,payload_json,outcome_version,created_at,updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        _signal_snapshot_key(row, symbol),
                        row.get("id"),
                        row.get("source") or "",
                        row.get("external_id") or "",
                        row.get("region") or "global",
                        row.get("theme") or "unknown",
                        _urgency_from_row(row),
                        symbol,
                        int(horizon),
                        event_at.isoformat(),
                        due_at.isoformat(),
                        baseline.get("price") if baseline else None,
                        baseline.get("source") if baseline else None,
                        expected,
                        status,
                        blocker,
                        json.dumps(payload, ensure_ascii=False, sort_keys=True),
                        GEOPOLITICAL_OUTCOMES_VERSION,
                        now_iso,
                        now_iso,
                    ),
                )
                if cursor.rowcount:
                    inserted_count += 1
                    recorded.append(
                        {
                            "source": row.get("source"),
                            "region": row.get("region"),
                            "theme": row.get("theme"),
                            "symbol": symbol,
                            "horizon_minutes": int(horizon),
                            "status": status,
                        }
                    )
                else:
                    duplicate_skipped_count += 1
    conn.commit()
    return {
        "attempted_count": attempted_count,
        "inserted_count": inserted_count,
        "duplicate_skipped_count": duplicate_skipped_count,
        "recorded": recorded,
    }


def _evaluate_pending(
    conn: sqlite3.Connection,
    *,
    now_dt: datetime,
    now_iso: str,
    max_outcome_lag_minutes: float,
    evaluation_limit: int,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT * FROM geopolitical_signal_outcomes
         WHERE status IN ('pending','outcome_unavailable')
         ORDER BY due_at ASC
         LIMIT ?
        """,
        (max(1, int(evaluation_limit)),),
    ).fetchall()
    symbols = {str(row["symbol"]).upper() for row in rows}
    observations = _price_observations(conn, symbols)
    evaluated: list[dict[str, Any]] = []
    for row in rows:
        due_at = _parse_dt(row["due_at"])
        baseline_price = _as_float(row["baseline_price"])
        if due_at is None or baseline_price is None or due_at > now_dt:
            continue
        outcome = _select_outcome(observations.get(str(row["symbol"]).upper()) or [], due_at, max_lag_minutes=max_outcome_lag_minutes)
        if outcome is None:
            max_dt = due_at + timedelta(minutes=max(0.0, float(max_outcome_lag_minutes)))
            if now_dt > max_dt:
                conn.execute(
                    "UPDATE geopolitical_signal_outcomes SET status=?, blocker=?, updated_at=? WHERE id=?",
                    ("outcome_unavailable", "outcome_unavailable", now_iso, row["id"]),
                )
            continue
        outcome_price = _as_float(outcome.get("price"))
        if outcome_price is None:
            continue
        raw_return = outcome_price / baseline_price - 1.0
        expected = str(row["expected_direction"] or "neutral")
        signed = -raw_return if expected == "down" else raw_return
        actual = _realized_direction(raw_return)
        hit = _direction_hit(expected, actual)
        conn.execute(
            """
            UPDATE geopolitical_signal_outcomes
               SET outcome_at=?,outcome_price=?,raw_return=?,signed_return=?,
                   actual_direction=?,direction_hit=?,status='evaluated',blocker=NULL,updated_at=?
             WHERE id=?
            """,
            (outcome["at"].isoformat(), outcome_price, raw_return, signed, actual, hit, now_iso, row["id"]),
        )
        evaluated.append(
            {
                "id": row["id"],
                "region": row["region"],
                "theme": row["theme"],
                "symbol": row["symbol"],
                "horizon_minutes": int(row["horizon_minutes"]),
                "raw_return": round(raw_return, 6),
                "signed_return": round(signed, 6),
                "direction_hit": hit,
            }
        )
    conn.commit()
    return evaluated


def rebuild_geopolitical_adaptive_asset_map(conn: sqlite3.Connection, *, min_samples: int = 3) -> dict[str, Any]:
    ensure_geopolitical_intelligence_schema(conn)
    rows = [
        dict(row)
        for row in conn.execute(
            """
            SELECT region,theme,symbol,
                   COUNT(*) AS sample_count,
                   SUM(CASE WHEN status='evaluated' THEN 1 ELSE 0 END) AS evaluated_count,
                   AVG(CASE WHEN status='evaluated' THEN ABS(raw_return) END) AS avg_abs_return,
                   AVG(CASE WHEN status='evaluated' THEN signed_return END) AS avg_signed_return,
                   AVG(CASE WHEN status='evaluated' AND direction_hit IS NOT NULL THEN direction_hit END) AS direction_hit_rate,
                   MAX(CASE WHEN status='evaluated' THEN outcome_at ELSE updated_at END) AS last_evaluated_at
              FROM geopolitical_signal_outcomes
             GROUP BY region,theme,symbol
            """
        ).fetchall()
    ]
    updated = 0
    for row in rows:
        evaluated = int(row.get("evaluated_count") or 0)
        avg_abs = _as_float(row.get("avg_abs_return")) or 0.0
        hit_rate = _as_float(row.get("direction_hit_rate"))
        weight = avg_abs * 10000.0
        if hit_rate is not None:
            weight *= 0.5 + hit_rate
        if evaluated >= max(1, int(min_samples)) and weight >= 3.0:
            tier = "primary_candidate"
        elif evaluated >= 1:
            tier = "context_candidate"
        else:
            tier = "collect_more"
        conn.execute(
            """
            INSERT INTO geopolitical_adaptive_asset_map (
                region,theme,symbol,sample_count,evaluated_count,avg_abs_return,
                avg_signed_return,direction_hit_rate,adaptive_weight,usage_tier,
                last_evaluated_at,map_version,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(region,theme,symbol,map_version) DO UPDATE SET
                sample_count=excluded.sample_count,
                evaluated_count=excluded.evaluated_count,
                avg_abs_return=excluded.avg_abs_return,
                avg_signed_return=excluded.avg_signed_return,
                direction_hit_rate=excluded.direction_hit_rate,
                adaptive_weight=excluded.adaptive_weight,
                usage_tier=excluded.usage_tier,
                last_evaluated_at=excluded.last_evaluated_at,
                updated_at=excluded.updated_at
            """,
            (
                row["region"],
                row["theme"],
                row["symbol"],
                int(row.get("sample_count") or 0),
                evaluated,
                row.get("avg_abs_return"),
                row.get("avg_signed_return"),
                row.get("direction_hit_rate"),
                round(weight, 4),
                tier,
                row.get("last_evaluated_at") or "",
                GEOPOLITICAL_ADAPTIVE_MAP_VERSION,
                _iso_now(),
            ),
        )
        updated += 1
    conn.commit()
    return {"adaptive_map_version": GEOPOLITICAL_ADAPTIVE_MAP_VERSION, "updated": updated}


def _geopolitical_coverage_audit(
    conn: sqlite3.Connection,
    *,
    candidate_rows: list[dict[str, Any]],
    geopolitical_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    scope_counts = Counter(signal_scope(row) for row in candidate_rows)
    source_scope_counts: dict[str, Counter[str]] = defaultdict(Counter)
    for row in candidate_rows:
        source_scope_counts[str(row.get("source") or "unknown")].update([signal_scope(row)])
    candidate_asset_counts: Counter[str] = Counter()
    for row in geopolitical_rows:
        candidate_asset_counts.update(_assets_from_json(row.get("assets_json"))[:10])

    status_counts: dict[str, int] = {}
    symbol_status_rows: list[dict[str, Any]] = []
    blocker_counts: dict[str, int] = {}
    baseline_unavailable_symbols: list[str] = []
    if _table_exists(conn, "geopolitical_signal_outcomes"):
        status_counts = dict(conn.execute("SELECT status,COUNT(*) FROM geopolitical_signal_outcomes GROUP BY status").fetchall())
        symbol_status_rows = [
            dict(row)
            for row in conn.execute(
                """
                SELECT symbol,status,COUNT(*) AS rows
                  FROM geopolitical_signal_outcomes
                 GROUP BY symbol,status
                 ORDER BY symbol,status
                """
            ).fetchall()
        ]
        blocker_counts = dict(
            conn.execute(
                """
                SELECT COALESCE(NULLIF(blocker,''),'none') AS blocker,COUNT(*)
                  FROM geopolitical_signal_outcomes
                 GROUP BY COALESCE(NULLIF(blocker,''),'none')
                """
            ).fetchall()
        )
        baseline_unavailable_symbols = [
            str(row["symbol"])
            for row in conn.execute(
                """
                SELECT symbol,COUNT(*) AS rows
                  FROM geopolitical_signal_outcomes
                 WHERE status='baseline_unavailable'
                 GROUP BY symbol
                 ORDER BY rows DESC,symbol
                 LIMIT 25
                """
            ).fetchall()
        ]
    return {
        "version": "geopolitical-coverage-audit-v1",
        "candidate_signal_count": len(candidate_rows),
        "geopolitical_signal_count": len(geopolitical_rows),
        "signal_scope_counts": dict(scope_counts),
        "source_scope_counts": {source: dict(counts) for source, counts in source_scope_counts.items()},
        "filtered_out_count": max(0, len(candidate_rows) - len(geopolitical_rows)),
        "filtered_out_counts": {
            key: value
            for key, value in dict(scope_counts).items()
            if key != "geopolitical"
        },
        "candidate_asset_counts": dict(candidate_asset_counts.most_common(25)),
        "outcome_status_counts": status_counts,
        "outcome_blocker_counts": blocker_counts,
        "baseline_unavailable_symbols": baseline_unavailable_symbols,
        "symbol_status_counts": symbol_status_rows[:200],
        "pending_count": int(status_counts.get("pending") or 0),
        "outcome_unavailable_count": int(status_counts.get("outcome_unavailable") or 0),
        "baseline_unavailable_count": int(status_counts.get("baseline_unavailable") or 0),
    }


def adaptive_assets_for_region_theme(
    db: Any,
    *,
    region: str,
    theme: str,
    fallback_assets: tuple[str, ...] | list[str],
    min_weight: float = 3.0,
    limit: int = 8,
) -> tuple[str, ...]:
    fallback = tuple(dict.fromkeys(str(item).strip().upper() for item in fallback_assets if str(item).strip()))
    try:
        with _connect_from_db(db) as conn:
            if not _table_exists(conn, "geopolitical_adaptive_asset_map"):
                return fallback
            rows = conn.execute(
                """
                SELECT symbol,adaptive_weight,usage_tier
                  FROM geopolitical_adaptive_asset_map
                 WHERE region=? AND theme=? AND map_version=?
                   AND adaptive_weight>=?
                   AND usage_tier IN ('primary_candidate','context_candidate')
                 ORDER BY adaptive_weight DESC,evaluated_count DESC
                 LIMIT ?
                """,
                (region, theme, GEOPOLITICAL_ADAPTIVE_MAP_VERSION, float(min_weight), max(1, int(limit))),
            ).fetchall()
    except Exception:
        return fallback
    adaptive = tuple(str(row["symbol"]).upper() for row in rows)
    return tuple(dict.fromkeys((*adaptive, *fallback)))[: max(len(fallback), int(limit))]


def build_geopolitical_intelligence_report(
    db: Any,
    *,
    lookback_hours: float = 168.0,
    horizons_minutes: str | list[int] | tuple[int, ...] = DEFAULT_GEOPOLITICAL_HORIZONS,
    signal_limit: int = 2000,
    max_assets_per_signal: int = 6,
    record_current: bool = True,
    evaluate_pending: bool = True,
    max_outcome_lag_minutes: float = 1440.0,
    evaluation_limit: int = 5000,
    now: str | datetime | None = None,
) -> dict[str, Any]:
    now_dt = _parse_dt(now) if not isinstance(now, datetime) else now.astimezone(UTC)
    now_dt = now_dt or datetime.now(UTC)
    since = now_dt - timedelta(hours=max(1.0, float(lookback_hours)))
    horizons = _parse_horizons(horizons_minutes)
    with _connect_from_db(db) as conn:
        ensure_geopolitical_intelligence_schema(conn)
        candidate_rows = _external_signal_rows(conn, since=since, until=now_dt, limit=signal_limit)
        rows = [row for row in candidate_rows if _is_geopolitical_signal(row)]
        symbols = {asset for row in rows for asset in _assets_from_json(row.get("assets_json"))[: max(1, int(max_assets_per_signal))]}
        observations = _price_observations(conn, symbols)
        recording_result = (
            _record_signal_outcomes(
                conn,
                rows=rows,
                horizons=horizons,
                observations=observations,
                max_assets_per_signal=max_assets_per_signal,
                now_iso=now_dt.isoformat(),
            )
            if record_current
            else {"attempted_count": 0, "inserted_count": 0, "duplicate_skipped_count": 0, "recorded": []}
        )
        recorded = recording_result.get("recorded") or []
        evaluated = (
            _evaluate_pending(
                conn,
                now_dt=now_dt,
                now_iso=now_dt.isoformat(),
                max_outcome_lag_minutes=max_outcome_lag_minutes,
                evaluation_limit=evaluation_limit,
            )
            if evaluate_pending
            else []
        )
        adaptive = rebuild_geopolitical_adaptive_asset_map(conn)
        heatmap = build_geopolitical_heatmap_from_connection(conn, lookback_hours=lookback_hours, limit=25)
        status_counts = dict(conn.execute("SELECT status,COUNT(*) FROM geopolitical_signal_outcomes GROUP BY status").fetchall())
        coverage_audit = _geopolitical_coverage_audit(conn, candidate_rows=candidate_rows, geopolitical_rows=rows)
        adaptive_rows = [
            dict(row)
            for row in conn.execute(
                """
                SELECT region,theme,symbol,evaluated_count,avg_abs_return,direction_hit_rate,adaptive_weight,usage_tier
                  FROM geopolitical_adaptive_asset_map
                 WHERE map_version=?
                 ORDER BY adaptive_weight DESC,evaluated_count DESC
                 LIMIT 50
                """,
                (GEOPOLITICAL_ADAPTIVE_MAP_VERSION,),
            ).fetchall()
        ]
    return {
        "geopolitical_intelligence_version": GEOPOLITICAL_INTELLIGENCE_VERSION,
        "generated_at": now_dt.isoformat(),
        "mode": "report_only_shadow_learning",
        "lookback_hours": float(lookback_hours),
        "horizons_minutes": horizons,
        "candidate_signal_count": len(candidate_rows),
        "source_signal_count": len(rows),
        "recorded_count": len(recorded),
        "recording_attempted_count": int(recording_result.get("attempted_count") or 0),
        "duplicate_skipped_count": int(recording_result.get("duplicate_skipped_count") or 0),
        "evaluated_count": len(evaluated),
        "status_counts": status_counts,
        "signal_scope_counts": coverage_audit.get("signal_scope_counts") or {},
        "coverage_audit": coverage_audit,
        "adaptive_map_update": adaptive,
        "adaptive_asset_map": adaptive_rows,
        "heatmap": heatmap,
        "recorded": recorded[:50],
        "evaluated": evaluated[:50],
        "safety": {
            "report_only": True,
            "scorer_threshold_mutated": False,
            "live_threshold_promoted": False,
            "paper_orders_created": False,
            "broker_orders_submitted": False,
            "transmit_enabled": False,
        },
    }


def _heat_color(score: float) -> str:
    if score >= 18:
        return "red"
    if score >= 9:
        return "yellow"
    if score >= 3:
        return "watch"
    return "quiet"


def build_geopolitical_heatmap_from_connection(
    conn: sqlite3.Connection,
    *,
    lookback_hours: float = 72.0,
    limit: int = 25,
) -> dict[str, Any]:
    now_dt = datetime.now(UTC)
    since = now_dt - timedelta(hours=max(1.0, float(lookback_hours)))
    signals: list[dict[str, Any]] = []
    if _table_exists(conn, "external_market_signals"):
        signals = [
            dict(row)
            for row in conn.execute(
                """
                SELECT source,title,published_at,region,theme,sentiment_score,sentiment_label,
                       confidence,assets_json,prediction_gate_status,raw_json
                  FROM external_market_signals
                 WHERE published_at!='' AND published_at>=?
                 ORDER BY published_at DESC,id DESC
                 LIMIT ?
                """,
                (since.isoformat(), max(1, int(limit)) * 20),
            ).fetchall()
        ]
        signals = [row for row in signals if _is_geopolitical_signal(row)]
    buckets: dict[tuple[str, str], dict[str, Any]] = {}
    for row in signals:
        key = (str(row.get("region") or "global"), str(row.get("theme") or "unknown"))
        event_at = _parse_dt(row.get("published_at")) or now_dt
        age_hours = max(0.0, (now_dt - event_at).total_seconds() / 3600.0)
        urgency = _urgency_from_row(row)
        sentiment = abs(_as_float(row.get("sentiment_score")) or 0.0)
        score = 1.0 + max(0.0, float(row.get("confidence") or 0.0))
        if urgency == "critical":
            score += 5.0
        elif urgency == "high":
            score += 3.0
        elif urgency == "medium":
            score += 1.2
        score += sentiment * 2.0
        if str(row.get("prediction_gate_status") or "") == "eligible":
            score += 1.0
        if age_hours <= 2:
            score *= 1.4
        elif age_hours <= 12:
            score *= 1.15
        item = buckets.setdefault(
            key,
            {
                "region": key[0],
                "theme": key[1],
                "signal_count": 0,
                "heat_score": 0.0,
                "urgency_counts": Counter(),
                "source_counts": Counter(),
                "asset_counts": Counter(),
                "latest_event_at": "",
                "latest_titles": [],
            },
        )
        item["signal_count"] += 1
        item["heat_score"] += score
        item["urgency_counts"].update([urgency])
        item["source_counts"].update([row.get("source") or "unknown"])
        item["asset_counts"].update(_assets_from_json(row.get("assets_json"))[:10])
        if not item["latest_event_at"] or str(row.get("published_at") or "") > item["latest_event_at"]:
            item["latest_event_at"] = str(row.get("published_at") or "")
        if len(item["latest_titles"]) < 3:
            item["latest_titles"].append(row.get("title"))
    adaptive: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    if _table_exists(conn, "geopolitical_adaptive_asset_map"):
        for row in conn.execute(
            """
            SELECT region,theme,symbol,adaptive_weight,usage_tier,evaluated_count,direction_hit_rate
              FROM geopolitical_adaptive_asset_map
             WHERE map_version=?
             ORDER BY adaptive_weight DESC,evaluated_count DESC
             LIMIT 200
            """,
            (GEOPOLITICAL_ADAPTIVE_MAP_VERSION,),
        ).fetchall():
            adaptive[(row["region"], row["theme"])].append(dict(row))
    heat_rows: list[dict[str, Any]] = []
    for key, item in buckets.items():
        score = round(float(item["heat_score"]), 3)
        top_assets = dict(item.pop("asset_counts").most_common(10))
        item["heat_score"] = score
        item["heat_color"] = _heat_color(score)
        item["urgency_counts"] = dict(item.pop("urgency_counts"))
        item["source_counts"] = dict(item.pop("source_counts").most_common(8))
        item["top_assets"] = top_assets
        item["adaptive_assets"] = adaptive.get(key, [])[:8]
        item["desk_action"] = (
            "risk_reduce_or_second_pass"
            if item["heat_color"] == "red"
            else "confidence_haircut"
            if item["heat_color"] == "yellow"
            else "monitor"
        )
        heat_rows.append(item)
    heat_rows.sort(key=lambda item: float(item.get("heat_score") or 0.0), reverse=True)
    return {
        "version": GEOPOLITICAL_HEATMAP_VERSION,
        "generated_at": now_dt.isoformat(),
        "lookback_hours": float(lookback_hours),
        "bucket_count": len(heat_rows),
        "heat_color_counts": dict(Counter(str(row.get("heat_color")) for row in heat_rows)),
        "region_counts": dict(Counter(str(row.get("region")) for row in heat_rows)),
        "top_buckets": heat_rows[: max(1, int(limit))],
        "safety": {
            "dashboard_context_only": True,
            "scorer_threshold_mutated": False,
            "broker_orders_submitted": False,
        },
    }


def build_geopolitical_heatmap(db: Any, *, lookback_hours: float = 72.0, limit: int = 25) -> dict[str, Any]:
    try:
        with _connect_from_db(db) as conn:
            ensure_geopolitical_intelligence_schema(conn)
            return build_geopolitical_heatmap_from_connection(conn, lookback_hours=lookback_hours, limit=limit)
    except Exception as exc:
        return {
            "version": GEOPOLITICAL_HEATMAP_VERSION,
            "available": False,
            "error": str(exc)[:300],
            "top_buckets": [],
        }


def geopolitical_intelligence_markdown(result: dict[str, Any]) -> str:
    heatmap = result.get("heatmap") or {}
    coverage = result.get("coverage_audit") or {}
    lines = [
        "# Geopolitical Intelligence Report",
        "",
        f"- Generated at: `{result.get('generated_at')}`",
        f"- Version: `{result.get('geopolitical_intelligence_version')}`",
        f"- Mode: `{result.get('mode')}`",
        f"- Candidate / geopolitical source signals: `{result.get('candidate_signal_count')}` / `{result.get('source_signal_count')}`",
        f"- Recorded / duplicate skipped / evaluated: `{result.get('recorded_count')}` / `{result.get('duplicate_skipped_count')}` / `{result.get('evaluated_count')}`",
        f"- Signal scope counts: `{result.get('signal_scope_counts') or {}}`",
        f"- Outcome status counts: `{result.get('status_counts') or {}}`",
        f"- Heat colors: `{heatmap.get('heat_color_counts') or {}}`",
        "- Safety: report-only learning and dashboard context; no order routing or live threshold mutation.",
        "",
        "## Coverage audit",
        "",
        f"- Filtered out: `{coverage.get('filtered_out_count')}` `{coverage.get('filtered_out_counts') or {}}`",
        f"- Outcome blockers: `{coverage.get('outcome_blocker_counts') or {}}`",
        f"- Baseline unavailable symbols: `{coverage.get('baseline_unavailable_symbols') or []}`",
        "",
        "| Source | Scope counts |",
        "| --- | --- |",
    ]
    for source, counts in (coverage.get("source_scope_counts") or {}).items():
        lines.append(f"| `{source}` | `{counts}` |")
    if not coverage.get("source_scope_counts"):
        lines.append("| - | - |")
    lines.extend(
        [
            "",
        "## Geo heatmap",
        "",
        "| Heat | Score | Region | Theme | Signals | Urgency | Top assets | Desk action | Latest titles |",
        "| --- | ---: | --- | --- | ---: | --- | --- | --- | --- |",
        ]
    )
    for row in (heatmap.get("top_buckets") or [])[:20]:
        lines.append(
            f"| `{row.get('heat_color')}` | {row.get('heat_score')} | `{row.get('region')}` | `{row.get('theme')}` | "
            f"{row.get('signal_count')} | `{row.get('urgency_counts')}` | `{row.get('top_assets')}` | "
            f"`{row.get('desk_action')}` | {str(row.get('latest_titles') or [])[:220].replace('|', '/')} |"
        )
    if not heatmap.get("top_buckets"):
        lines.append("| - | - | - | - | 0 | - | - | - | - |")
    lines.extend(
        [
            "",
            "## Adaptive asset map",
            "",
            "| Tier | Weight | Region | Theme | Symbol | Evaluated | Hit rate | Avg abs return |",
            "| --- | ---: | --- | --- | --- | ---: | ---: | ---: |",
        ]
    )
    for row in (result.get("adaptive_asset_map") or [])[:40]:
        hit = row.get("direction_hit_rate")
        avg_abs = row.get("avg_abs_return")
        lines.append(
            f"| `{row.get('usage_tier')}` | {row.get('adaptive_weight')} | `{row.get('region')}` | `{row.get('theme')}` | "
            f"`{row.get('symbol')}` | {row.get('evaluated_count')} | "
            f"{float(hit):.2f} | {float(avg_abs):.5f} |"
            if hit is not None and avg_abs is not None
            else f"| `{row.get('usage_tier')}` | {row.get('adaptive_weight')} | `{row.get('region')}` | `{row.get('theme')}` | `{row.get('symbol')}` | {row.get('evaluated_count')} | - | - |"
        )
    if not result.get("adaptive_asset_map"):
        lines.append("| - | - | - | - | - | 0 | - | - |")
    return "\n".join(lines) + "\n"


def write_geopolitical_intelligence_json(path: Path, result: dict[str, Any]) -> Path:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return resolved


def write_geopolitical_intelligence_markdown(path: Path, result: dict[str, Any]) -> Path:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(geopolitical_intelligence_markdown(result), encoding="utf-8")
    return resolved
