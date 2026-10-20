from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from recorder.database import parse_utc, utc_iso
from recorder.market_outcomes import (
    DailyBar,
    DailyPriceSource,
    IntradayBar,
    IntradayPriceSource,
    YahooChartDailyPriceSource,
)
from recorder.news_symbol_tiers import (
    SymbolTierPolicy,
    candidate_allowed_by_symbol_tier,
    load_symbol_tier_policy,
    parse_tier_filter,
)

NEWS_MARKET_OUTCOME_BACKFILL_VERSION = "news-market-outcome-backfill-v1"
NEWS_MARKET_OUTCOME_LINEAGE_VERSION = "m253-lineage-v2-outcome-cutover-v1"
NY = ZoneInfo("America/New_York")
UTC = timezone.utc
SKIPPED_CLOSED_SESSION_STATUS = "skipped_closed_session"
SKIPPED_NO_TRADABLE_INTRADAY_STATUS = "skipped_no_tradable_intraday_session"
RETRY_TERMINAL_TARGET_STATUSES = {
    SKIPPED_CLOSED_SESSION_STATUS,
    SKIPPED_NO_TRADABLE_INTRADAY_STATUS,
}
EXTENDED_SESSION_OPEN = time(4, 0)
EXTENDED_SESSION_CLOSE = time(20, 0)
US_EQUITY_SPECIAL_CLOSURES: dict[date, str] = {
    date(2025, 1, 9): "national_day_of_mourning_carter",
}


@dataclass(frozen=True)
class NewsOutcomeCandidate:
    news_id: int
    provider: str
    article_id: str
    headline: str
    primary_symbol: str
    published_at: datetime
    symbol: str
    requested_horizons: tuple[str, ...] = ()
    raw_news_id: int | None = None
    event_cluster_id: int | None = None
    candidate_role: str = ""
    candidate_source: str = ""
    mapping_confidence: float = 0.0
    lineage_source: str = "legacy_news_symbols"


@dataclass(frozen=True)
class BuiltOutcome:
    candidate: NewsOutcomeCandidate
    horizon: str
    due_at: datetime
    captured_at: datetime | None
    baseline_at: datetime | None
    baseline_price: float | None
    reaction_price: float | None
    raw_return: float | None
    price_source: str
    status: str
    blocker_reason: str


def build_news_market_outcomes(
    database_path: str | Path,
    *,
    start: str,
    end: str,
    symbols: str | None = None,
    horizons: tuple[str, ...] = ("NEXT_DAY",),
    price_source: str = "yahoo",
    limit: int = 1000,
    max_groups: int = 0,
    only_missing: bool = True,
    include_pseudo_symbols: bool = False,
    flat_threshold: float = 0.001,
    close_retry_bypass_after_failures: int = 3,
    daily_price_source: DailyPriceSource | None = None,
    intraday_price_source: IntradayPriceSource | None = None,
    progress_jsonl_path: str | Path | None = None,
    symbol_tier_config: str | Path | None = None,
    symbol_tier_filter: str | None = None,
    event_proxy_policy: str = "matched-news-only",
    unknown_symbol_policy: str = "exclude",
) -> dict[str, Any]:
    db_path = Path(database_path)
    normalized_horizons = _normalize_horizons(horizons)
    daily_horizons = tuple(h for h in normalized_horizons if h == "NEXT_DAY")
    intraday_horizons = tuple(h for h in normalized_horizons if h != "NEXT_DAY")
    if price_source == "yahoo" and intraday_horizons:
        raise ValueError("Yahoo mode supports NEXT_DAY only. Use price_source='ibkr' for 5M/15M/1H/CLOSE.")
    selected_symbols = _parse_symbols(symbols)
    tier_policy = load_symbol_tier_policy(symbol_tier_config) if symbol_tier_config else None
    tier_filter = parse_tier_filter(symbol_tier_filter)
    if tier_policy is not None and tier_filter:
        tier_symbols = tier_policy.symbols_for_tiers(tier_filter)
        selected_symbols = selected_symbols & tier_symbols if selected_symbols else tier_symbols
    started_at = datetime.now(UTC)
    progress_path = Path(progress_jsonl_path) if progress_jsonl_path else None
    if progress_path:
        progress_path.parent.mkdir(parents=True, exist_ok=True)

    report: dict[str, Any] = {
        "version": NEWS_MARKET_OUTCOME_BACKFILL_VERSION,
        "lineage_version": NEWS_MARKET_OUTCOME_LINEAGE_VERSION,
        "database_path": str(db_path),
        "start": start,
        "end": end,
        "symbols": sorted(selected_symbols) if selected_symbols else None,
        "horizons": list(normalized_horizons),
        "price_source": price_source,
        "limit": int(limit),
        "max_groups": int(max_groups),
        "only_missing": bool(only_missing),
        "include_pseudo_symbols": bool(include_pseudo_symbols),
        "flat_threshold": float(flat_threshold),
        "close_retry_bypass_after_failures": max(0, int(close_retry_bypass_after_failures)),
        "close_retry_bypassed_count": 0,
        "symbol_tier_config": str(symbol_tier_config) if symbol_tier_config else None,
        "symbol_tier_filter": sorted(tier_filter),
        "event_proxy_policy": event_proxy_policy,
        "unknown_symbol_policy": unknown_symbol_policy,
        "started_at": started_at.isoformat(),
        "candidate_count": 0,
        "candidate_source_counts": {},
        "processed_candidates": 0,
        "completed_outcomes": 0,
        "blocked_outcomes": 0,
        "skipped_closed_session_outcomes": 0,
        "skipped_no_tradable_intraday_outcomes": 0,
        "inserted_or_updated_reactions": 0,
        "created_or_updated_targets": 0,
        "baseline_rows_inserted": 0,
        "groups_processed": 0,
        "source_errors": {},
        "source_error_categories": {},
        "status_counts": {},
        "horizon_counts": {},
        "symbol_counts": {},
        "sample_blockers": [],
        "safety": {
            "read_only_price_fetch": True,
            "writes_local_market_reactions_only": True,
            "usage_status": "observation_only",
            "broker_orders_submitted": False,
            "transmit_enabled": False,
            "live_order_routing_enabled": False,
        },
    }

    with sqlite3.connect(db_path, timeout=60) as conn:
        conn.row_factory = sqlite3.Row
        _ensure_indexes(conn)
        candidates = _load_candidates(
            conn,
            start=start,
            end=end,
            symbols=selected_symbols,
            horizons=normalized_horizons,
            limit=limit,
            only_missing=only_missing,
            include_pseudo_symbols=include_pseudo_symbols,
            tier_policy=tier_policy,
            tier_filter=tier_filter,
            event_proxy_policy=event_proxy_policy,
            unknown_symbol_policy=unknown_symbol_policy,
        )
    report["candidate_count"] = len(candidates)
    report["candidate_source_counts"] = _candidate_source_counts(candidates)
    report["requested_outcome_count"] = sum(len(candidate.requested_horizons or normalized_horizons) for candidate in candidates)
    report["candidate_tier_counts"] = _candidate_tier_counts(candidates, tier_policy)
    if not candidates:
        report["ended_at"] = datetime.now(UTC).isoformat()
        return report

    daily_source = daily_price_source
    intraday_source = intraday_price_source
    should_close_daily = False
    should_close_intraday = False
    try:
        if daily_horizons and daily_source is None:
            daily_source = YahooChartDailyPriceSource()
            should_close_daily = True
        if "CLOSE" in intraday_horizons and daily_source is None:
            daily_source = YahooChartDailyPriceSource()
            should_close_daily = True
        if intraday_horizons and intraday_source is None:
            raise ValueError("Intraday outcome backfill requires an IBKR intraday price source.")

        outcomes: list[BuiltOutcome] = []
        store_totals = _empty_store_totals()

        def store_batch(batch: list[BuiltOutcome]) -> None:
            if not batch:
                return
            with sqlite3.connect(db_path, timeout=60) as batch_conn:
                batch_conn.row_factory = sqlite3.Row
                _ensure_indexes(batch_conn)
                stored_batch = _store_outcomes(batch_conn, batch, flat_threshold=flat_threshold)
            _merge_store_totals(store_totals, stored_batch)

        if daily_horizons and daily_source is not None:
            outcomes.extend(
                _build_daily_outcomes(
                    candidates,
                    daily_source=daily_source,
                    horizons=daily_horizons,
                    report=report,
                    progress_path=progress_path,
                )
            )
        if intraday_horizons and intraday_source is not None:
            outcomes.extend(
                _build_intraday_outcomes(
                    candidates,
                    intraday_source=intraday_source,
                    horizons=intraday_horizons,
                    max_groups=max_groups,
                    report=report,
                    progress_path=progress_path,
                    store_batch=store_batch,
                    daily_close_source=daily_source,
                    close_retry_bypass_after_failures=max(0, int(close_retry_bypass_after_failures)),
                )
            )

        store_batch(outcomes)
        report.update(store_totals)
    finally:
        if should_close_daily:
            close = getattr(daily_source, "close", None)
            if callable(close):
                close()
        if should_close_intraday:
            close = getattr(intraday_source, "close", None)
            if callable(close):
                close()

    report["ended_at"] = datetime.now(UTC).isoformat()
    report["duration_seconds"] = round((parse_utc(report["ended_at"]) - started_at).total_seconds(), 3)
    return report


def _parse_symbols(value: str | None) -> set[str]:
    return {item.strip().upper() for item in (value or "").split(",") if item.strip()}


def _normalize_horizons(horizons: tuple[str, ...]) -> tuple[str, ...]:
    aliases = {
        "EOD": "CLOSE",
        "END_OF_DAY": "CLOSE",
        "1D": "NEXT_DAY",
        "NEXT_1D": "NEXT_DAY",
        "NEXT_CLOSE": "NEXT_DAY",
        "60M": "1H",
    }
    result: list[str] = []
    allowed = {"5M", "15M", "1H", "CLOSE", "NEXT_DAY"}
    for item in horizons:
        normalized = aliases.get(str(item).strip().upper(), str(item).strip().upper())
        if not normalized:
            continue
        if normalized not in allowed:
            raise ValueError(f"Unsupported news outcome horizon: {item!r}")
        result.append(normalized)
    return tuple(dict.fromkeys(result)) or ("NEXT_DAY",)


def _load_candidates(
    conn: sqlite3.Connection,
    *,
    start: str,
    end: str,
    symbols: set[str],
    horizons: tuple[str, ...],
    limit: int,
    only_missing: bool,
    include_pseudo_symbols: bool,
    tier_policy: SymbolTierPolicy | None = None,
    tier_filter: set[str] | None = None,
    event_proxy_policy: str = "matched-news-only",
    unknown_symbol_policy: str = "exclude",
) -> list[NewsOutcomeCandidate]:
    lineage_candidates = _load_lineage_v2_candidates(
        conn,
        start=start,
        end=end,
        symbols=symbols,
        horizons=horizons,
        limit=limit,
        only_missing=only_missing,
        include_pseudo_symbols=include_pseudo_symbols,
        tier_policy=tier_policy,
        tier_filter=tier_filter,
        event_proxy_policy=event_proxy_policy,
        unknown_symbol_policy=unknown_symbol_policy,
    )
    if lineage_candidates is not None:
        return lineage_candidates

    primary_symbol_expr = "n.primary_symbol" if _table_has_column(conn, "news_articles", "primary_symbol") else "''"
    where = [
        "n.published_at >= ?",
        "n.published_at <= ?",
        "ns.symbol IS NOT NULL",
        "ns.symbol <> ''",
    ]
    params: list[Any] = [start, end]
    if symbols:
        placeholders = ",".join("?" for _ in symbols)
        where.append(f"upper(ns.symbol) IN ({placeholders})")
        params.extend(sorted(symbols))
    if not include_pseudo_symbols:
        where.append("ns.symbol NOT LIKE '%:%'")
    if only_missing:
        placeholders = ",".join("?" for _ in horizons)
        where.append(
            f"""(
                    SELECT COUNT(DISTINCT upper(mr.horizon))
                      FROM market_reactions mr
                     WHERE mr.news_id=n.id
                       AND upper(mr.symbol)=upper(ns.symbol)
                       AND upper(mr.horizon) IN ({placeholders})
                ) < ?"""
        )
        params.extend(horizons)
        params.append(len(horizons))
    sql = f"""
        SELECT DISTINCT n.id AS news_id,
               n.provider,
               n.article_id,
               n.headline,
               {primary_symbol_expr} AS primary_symbol,
               n.published_at,
               upper(ns.symbol) AS symbol
          FROM news_articles n
          JOIN news_symbols ns ON ns.news_id=n.id
         WHERE {' AND '.join(where)}
         ORDER BY n.published_at ASC, n.id ASC, ns.symbol ASC
         LIMIT ?
    """
    params.append(max(1, int(limit)))
    rows = conn.execute(sql, params).fetchall()
    existing_horizons = _existing_outcome_horizons(conn, rows, horizons) if only_missing else {}
    candidates: list[NewsOutcomeCandidate] = []
    for row in rows:
        try:
            published = parse_utc(str(row["published_at"]))
        except Exception:
            continue
        news_id = int(row["news_id"])
        symbol = str(row["symbol"] or "").upper()
        requested_horizons = tuple(horizon for horizon in horizons if horizon not in existing_horizons.get((news_id, symbol), set()))
        if only_missing and not requested_horizons:
            continue
        candidates.append(
            NewsOutcomeCandidate(
                news_id=news_id,
                provider=str(row["provider"] or ""),
                article_id=str(row["article_id"] or ""),
                headline=str(row["headline"] or ""),
                primary_symbol=str(row["primary_symbol"] or ""),
                published_at=published,
                symbol=symbol,
                requested_horizons=requested_horizons or horizons,
                lineage_source="legacy_news_symbols",
            )
        )
    if tier_policy is None:
        return candidates
    allowed: list[NewsOutcomeCandidate] = []
    for candidate in candidates:
        if candidate_allowed_by_symbol_tier(
            symbol=candidate.symbol,
            headline=candidate.headline,
            primary_symbol=candidate.primary_symbol,
            policy=tier_policy,
            tier_filter=tier_filter or set(),
            event_proxy_policy=event_proxy_policy,
            unknown_symbol_policy=unknown_symbol_policy,
        ):
            allowed.append(candidate)
    return allowed


def _load_lineage_v2_candidates(
    conn: sqlite3.Connection,
    *,
    start: str,
    end: str,
    symbols: set[str],
    horizons: tuple[str, ...],
    limit: int,
    only_missing: bool,
    include_pseudo_symbols: bool,
    tier_policy: SymbolTierPolicy | None = None,
    tier_filter: set[str] | None = None,
    event_proxy_policy: str = "matched-news-only",
    unknown_symbol_policy: str = "exclude",
) -> list[NewsOutcomeCandidate] | None:
    required_tables = {
        "raw_news_articles",
        "news_asset_candidates",
        "news_event_clusters",
        "news_event_cluster_roles",
    }
    if not all(_table_exists(conn, table) for table in required_tables):
        return None
    time_expr = "COALESCE(NULLIF(r.published_at_utc,''),r.received_at_utc,r.created_at)"
    base_where = [
        f"{time_expr} >= ?",
        f"{time_expr} <= ?",
        "c.event_cluster_id IS NOT NULL",
        "c.symbol IS NOT NULL",
        "c.symbol <> ''",
        "c.role IN ('primary','secondary','proxy')",
        "c.source NOT IN ('fallback_context','ibkr_route')",
        "COALESCE(er.provider_agreement,'') <> 'marketaux_noise_filtered'",
        """(
              (c.role IN ('primary','secondary')
               AND c.resolver_status='resolved'
               AND c.tradable_candidate=1
               AND c.confidence>=0.50)
           OR (c.role='proxy'
               AND c.resolver_status IN ('proxy_only','resolved')
               AND c.tradable_candidate=1
               AND c.confidence>=0.35)
        )""",
    ]
    params: list[Any] = [start, end]
    if symbols:
        placeholders = ",".join("?" for _ in symbols)
        base_where.append(f"upper(c.symbol) IN ({placeholders})")
        params.extend(sorted(symbols))
    if not include_pseudo_symbols:
        base_where.append("c.symbol NOT LIKE '%:%'")
    raw_scope_sql = f"""
        SELECT COUNT(*) AS c
          FROM raw_news_articles r
          JOIN news_asset_candidates c ON c.news_id=r.id
          JOIN news_event_cluster_roles er ON er.event_cluster_id=c.event_cluster_id
         WHERE {time_expr} >= ? AND {time_expr} <= ?
           AND c.event_cluster_id IS NOT NULL
    """
    raw_scope_count = int(conn.execute(raw_scope_sql, (start, end)).fetchone()["c"])
    if raw_scope_count <= 0:
        return None

    sql = f"""
        WITH eligible AS (
            SELECT
                COALESCE(r.legacy_news_id,r.id) AS news_id,
                r.id AS raw_news_id,
                r.provider,
                r.provider_article_id AS article_id,
                r.headline,
                r.primary_route_symbol AS primary_symbol,
                {time_expr} AS published_at,
                upper(c.symbol) AS symbol,
                c.event_cluster_id,
                c.role AS candidate_role,
                c.source AS candidate_source,
                c.confidence AS mapping_confidence,
                c.resolver_status,
                c.tradable_candidate,
                er.provider_agreement,
                er.provider_confirmation_score,
                er.provider_conflict_score,
                CASE c.role
                    WHEN 'primary' THEN 0
                    WHEN 'secondary' THEN 1
                    ELSE 2
                END AS role_rank
              FROM raw_news_articles r
              JOIN news_asset_candidates c ON c.news_id=r.id
              JOIN news_event_clusters ec ON ec.event_cluster_id=c.event_cluster_id
              JOIN news_event_cluster_roles er ON er.event_cluster_id=c.event_cluster_id
             WHERE {' AND '.join(base_where)}
        ),
        ranked AS (
            SELECT *,
                   ROW_NUMBER() OVER (
                       PARTITION BY event_cluster_id,symbol
                       ORDER BY role_rank ASC,mapping_confidence DESC,published_at ASC,raw_news_id ASC
                   ) AS rn
              FROM eligible
        )
        SELECT *
          FROM ranked
         WHERE rn=1
         ORDER BY published_at ASC,raw_news_id ASC,symbol ASC
         LIMIT ?
    """
    query_params = [*params, max(1, int(limit))]
    rows = conn.execute(sql, query_params).fetchall()
    existing_horizons = _existing_outcome_horizons(conn, rows, horizons) if only_missing else {}
    candidates: list[NewsOutcomeCandidate] = []
    for row in rows:
        try:
            published = parse_utc(str(row["published_at"]))
        except Exception:
            continue
        news_id = int(row["news_id"])
        symbol = str(row["symbol"] or "").upper()
        requested_horizons = tuple(horizon for horizon in horizons if horizon not in existing_horizons.get((news_id, symbol), set()))
        if only_missing and not requested_horizons:
            continue
        try:
            mapping_confidence = float(row["mapping_confidence"] or 0.0)
        except (TypeError, ValueError):
            mapping_confidence = 0.0
        candidates.append(
            NewsOutcomeCandidate(
                news_id=news_id,
                provider=str(row["provider"] or ""),
                article_id=str(row["article_id"] or ""),
                headline=str(row["headline"] or ""),
                primary_symbol=str(row["primary_symbol"] or ""),
                published_at=published,
                symbol=symbol,
                requested_horizons=requested_horizons or horizons,
                raw_news_id=int(row["raw_news_id"]) if row["raw_news_id"] is not None else None,
                event_cluster_id=int(row["event_cluster_id"]) if row["event_cluster_id"] is not None else None,
                candidate_role=str(row["candidate_role"] or ""),
                candidate_source=str(row["candidate_source"] or ""),
                mapping_confidence=mapping_confidence,
                lineage_source="lineage_v2_cluster_candidate",
            )
        )
    if tier_policy is None:
        return candidates
    allowed: list[NewsOutcomeCandidate] = []
    for candidate in candidates:
        if candidate_allowed_by_symbol_tier(
            symbol=candidate.symbol,
            headline=candidate.headline,
            primary_symbol=candidate.primary_symbol,
            policy=tier_policy,
            tier_filter=tier_filter or set(),
            event_proxy_policy=event_proxy_policy,
            unknown_symbol_policy=unknown_symbol_policy,
        ):
            allowed.append(candidate)
    return allowed


def _existing_outcome_horizons(
    conn: sqlite3.Connection,
    candidate_rows: list[sqlite3.Row],
    horizons: tuple[str, ...],
) -> dict[tuple[int, str], set[str]]:
    keys = {(int(row["news_id"]), str(row["symbol"] or "").upper()) for row in candidate_rows}
    if not keys:
        return {}
    news_ids = sorted({news_id for news_id, _symbol in keys})
    horizon_placeholders = ",".join("?" for _ in horizons)
    result: dict[tuple[int, str], set[str]] = defaultdict(set)
    if _table_exists(conn, "market_reactions"):
        for index in range(0, len(news_ids), 800):
            chunk = news_ids[index : index + 800]
            id_placeholders = ",".join("?" for _ in chunk)
            rows = conn.execute(
                f"""
                SELECT news_id, upper(symbol) AS symbol, upper(horizon) AS horizon
                  FROM market_reactions
                 WHERE news_id IN ({id_placeholders})
                   AND upper(horizon) IN ({horizon_placeholders})
                """,
                [*chunk, *horizons],
            ).fetchall()
            for row in rows:
                key = (int(row["news_id"]), str(row["symbol"] or "").upper())
                if key in keys:
                    result[key].add(str(row["horizon"] or "").upper())
    if _table_exists(conn, "reaction_targets"):
        for index in range(0, len(news_ids), 800):
            chunk = news_ids[index : index + 800]
            id_placeholders = ",".join("?" for _ in chunk)
            rows = conn.execute(
                f"""
                SELECT news_id, upper(symbol) AS symbol, upper(horizon) AS horizon
                  FROM reaction_targets
                 WHERE news_id IN ({id_placeholders})
                   AND upper(horizon) IN ({horizon_placeholders})
                    AND status IN ({",".join("?" for _ in RETRY_TERMINAL_TARGET_STATUSES)})
                """,
                [*chunk, *horizons, *sorted(RETRY_TERMINAL_TARGET_STATUSES)],
            ).fetchall()
            for row in rows:
                key = (int(row["news_id"]), str(row["symbol"] or "").upper())
                if key in keys:
                    result[key].add(str(row["horizon"] or "").upper())
    return dict(result)


def _candidate_tier_counts(candidates: list[NewsOutcomeCandidate], tier_policy: SymbolTierPolicy | None) -> dict[str, int]:
    if tier_policy is None:
        return {}
    counts: Counter[str] = Counter()
    for candidate in candidates:
        counts[tier_policy.classify(candidate.symbol).tier] += 1
    return dict(counts)


def _candidate_source_counts(candidates: list[NewsOutcomeCandidate]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for candidate in candidates:
        counts[candidate.lineage_source or "unknown"] += 1
    return dict(counts)


def _table_has_column(conn: sqlite3.Connection, table_name: str, column_name: str) -> bool:
    return any(str(row[1]).lower() == column_name.lower() for row in conn.execute(f"PRAGMA table_info({table_name})"))


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table_name,)).fetchone() is not None


def _source_error_category(exc: Exception) -> str:
    classification = getattr(exc, "classification", None)
    category = getattr(classification, "category", None)
    if category:
        return str(category)
    text = str(exc)
    marker = "ibkr_category="
    if marker in text:
        tail = text.split(marker, 1)[1]
        return tail.split(";", 1)[0].split("]", 1)[0].strip() or "ibkr_historical_error"
    lowered = text.lower()
    if "no market data permissions" in lowered:
        return "permission_missing"
    if "unknown contract" in lowered:
        return "exchange_alias_retry_needed"
    if "timed out" in lowered or "timeout" in lowered:
        return "timeout"
    return "unclassified_source_error"


def _record_source_error(report: dict[str, Any], key: str, exc: Exception) -> None:
    report.setdefault("source_errors", {})[key] = str(exc)
    category = _source_error_category(exc)
    categories = report.setdefault("source_error_categories", {})
    categories[category] = int(categories.get(category) or 0) + 1


def _build_daily_outcomes(
    candidates: list[NewsOutcomeCandidate],
    *,
    daily_source: DailyPriceSource,
    horizons: tuple[str, ...],
    report: dict[str, Any],
    progress_path: Path | None,
) -> list[BuiltOutcome]:
    outcomes: list[BuiltOutcome] = []
    by_symbol: dict[str, list[NewsOutcomeCandidate]] = defaultdict(list)
    for candidate in candidates:
        by_symbol[candidate.symbol].append(candidate)
    for symbol, rows in sorted(by_symbol.items()):
        event_dates = [candidate.published_at.astimezone(NY).date() for candidate in rows]
        fetch_start = min(event_dates) - timedelta(days=10)
        fetch_end = max(event_dates) + timedelta(days=20)
        try:
            bars = daily_source.get_daily_bars(symbol, fetch_start, fetch_end)
        except Exception as exc:
            bars = []
            _record_source_error(report, symbol, exc)
        for candidate in rows:
            for horizon in _candidate_requested_horizons(candidate, horizons):
                outcomes.append(_build_next_day_outcome(candidate, bars, price_source=daily_source.name))
        _append_progress(progress_path, {"stage": "daily_symbol_complete", "symbol": symbol, "candidate_count": len(rows)})
    return outcomes


def _build_next_day_outcome(
    candidate: NewsOutcomeCandidate,
    bars: list[DailyBar],
    *,
    price_source: str,
) -> BuiltOutcome:
    event_ny = candidate.published_at.astimezone(NY)
    event_date = event_ny.date()
    baseline_index = _latest_daily_index_leq(bars, event_date) if event_ny.time() >= time(16, 0) else _latest_daily_index_lt(bars, event_date)
    blockers: list[str] = []
    baseline_bar: DailyBar | None = None
    reaction_bar: DailyBar | None = None
    if baseline_index is None:
        blockers.append("baseline_price_unavailable")
    else:
        baseline_bar = bars[baseline_index]
        event_date_has_bar = any(bar.date == event_date for bar in bars)
        # CLOSE is the first regular-session close after the news.  NEXT_DAY is
        # intentionally one trading close after that, so intraday/pre-close news
        # does not duplicate the CLOSE horizon.
        step = 2 if event_ny.time() < time(16, 0) and event_date_has_bar else 1
        reaction_index = baseline_index + step
        if reaction_index >= len(bars):
            blockers.append("reaction_price_unavailable")
        else:
            reaction_bar = bars[reaction_index]
    raw_return = None
    if baseline_bar and reaction_bar and baseline_bar.close > 0:
        raw_return = reaction_bar.close / baseline_bar.close - 1.0
    captured_at = _market_close_utc(reaction_bar.date) if reaction_bar else None
    due_at = captured_at or _market_close_utc(_next_trading_day(event_date))
    return BuiltOutcome(
        candidate=candidate,
        horizon="NEXT_DAY",
        due_at=due_at,
        captured_at=captured_at,
        baseline_at=_market_close_utc(baseline_bar.date) if baseline_bar else None,
        baseline_price=baseline_bar.close if baseline_bar else None,
        reaction_price=reaction_bar.close if reaction_bar else None,
        raw_return=raw_return,
        price_source=f"{price_source}_news_backfill",
        status="complete" if raw_return is not None else "blocked",
        blocker_reason=";".join(dict.fromkeys(blockers)),
    )


def _build_intraday_outcomes(
    candidates: list[NewsOutcomeCandidate],
    *,
    intraday_source: IntradayPriceSource,
    horizons: tuple[str, ...],
    max_groups: int,
    report: dict[str, Any],
    progress_path: Path | None,
    store_batch: Callable[[list[BuiltOutcome]], None] | None = None,
    daily_close_source: DailyPriceSource | None = None,
    close_retry_bypass_after_failures: int = 3,
) -> list[BuiltOutcome]:
    outcomes: list[BuiltOutcome] = []
    by_group: dict[tuple[str, date], list[NewsOutcomeCandidate]] = defaultdict(list)
    for candidate in candidates:
        by_group[(candidate.symbol, candidate.published_at.astimezone(NY).date())].append(candidate)
    close_retry_failures_by_symbol: Counter[str] = Counter()
    for index, ((symbol, event_day), rows) in enumerate(sorted(by_group.items()), start=1):
        if max_groups > 0 and index > max_groups:
            report["max_groups_reached"] = True
            break
        start_times = [candidate.published_at for candidate in rows]
        target_times: list[datetime] = []
        for candidate in rows:
            for horizon in _candidate_requested_horizons(candidate, horizons):
                target_times.append(_intraday_target(candidate.published_at, horizon))
        group_horizons = tuple(dict.fromkeys(h for candidate in rows for h in _candidate_requested_horizons(candidate, horizons)))
        fetch_mode = _intraday_fetch_mode(event_day, rows, group_horizons)
        fetch_start, fetch_end = _intraday_fetch_window(start_times, target_times, mode=fetch_mode)
        closed_session_reason = _us_equity_market_closure_reason(event_day)
        try:
            bars = intraday_source.get_intraday_bars(symbol, fetch_start, fetch_end)
        except Exception as exc:
            bars = []
            _record_source_error(report, f"{symbol}:{event_day.isoformat()}", exc)
        group_outcomes: list[BuiltOutcome] = []
        for candidate in rows:
            for horizon in _candidate_requested_horizons(candidate, horizons):
                group_outcomes.append(
                    _build_intraday_outcome(
                        candidate,
                        bars,
                        horizon=horizon,
                        price_source=intraday_source.name,
                        closed_session_reason=closed_session_reason if not bars else None,
                        no_tradable_intraday_reason=(
                            None
                            if closed_session_reason
                            else _no_tradable_intraday_reason(candidate.published_at, horizon)
                        ),
                    )
                )
        close_retry_needed = (
            "CLOSE" in group_horizons
            and fetch_mode != "wide"
            and any(outcome.horizon == "CLOSE" and outcome.status != "complete" for outcome in group_outcomes)
        )
        close_retry_bypassed = (
            close_retry_needed
            and daily_close_source is not None
            and close_retry_bypass_after_failures > 0
            and close_retry_failures_by_symbol[symbol] >= close_retry_bypass_after_failures
        )
        if close_retry_bypassed:
            report["close_retry_bypassed_count"] = int(report.get("close_retry_bypassed_count") or 0) + 1
            _append_progress(
                progress_path,
                {
                    "stage": "intraday_group_close_retry_bypassed",
                    "group_index": index,
                    "symbol": symbol,
                    "event_day": event_day.isoformat(),
                    "candidate_count": len(rows),
                    "failure_count": int(close_retry_failures_by_symbol[symbol]),
                    "bypass_after_failures": int(close_retry_bypass_after_failures),
                },
            )
        if close_retry_needed:
            if close_retry_bypassed:
                pass
            else:
                retry_start, retry_end = _intraday_fetch_window(start_times, target_times, mode="wide")
                try:
                    retry_bars = intraday_source.get_intraday_bars(symbol, retry_start, retry_end)
                except Exception as exc:
                    close_retry_failures_by_symbol[symbol] += 1
                    _record_source_error(report, f"{symbol}:{event_day.isoformat()}:close_retry", exc)
                    _append_progress(
                        progress_path,
                        {
                            "stage": "intraday_group_close_retry_failed",
                            "group_index": index,
                            "symbol": symbol,
                            "event_day": event_day.isoformat(),
                            "candidate_count": len(rows),
                            "failure_count": int(close_retry_failures_by_symbol[symbol]),
                            "error": str(exc),
                        },
                    )
                else:
                    close_retry_failures_by_symbol[symbol] = 0
                    bars = retry_bars
                    group_outcomes = []
                    for candidate in rows:
                        for horizon in _candidate_requested_horizons(candidate, horizons):
                            group_outcomes.append(
                                _build_intraday_outcome(
                                    candidate,
                                    bars,
                                    horizon=horizon,
                                    price_source=intraday_source.name,
                                    closed_session_reason=closed_session_reason if not bars else None,
                                    no_tradable_intraday_reason=(
                                        None
                                        if closed_session_reason
                                        else _no_tradable_intraday_reason(candidate.published_at, horizon)
                                    ),
                                )
                            )
                    _append_progress(
                        progress_path,
                        {
                            "stage": "intraday_group_close_retry",
                            "group_index": index,
                            "symbol": symbol,
                            "event_day": event_day.isoformat(),
                            "candidate_count": len(rows),
                            "bar_count": len(bars),
                        },
                    )
        close_daily_fallback_needed = (
            "CLOSE" in group_horizons
            and daily_close_source is not None
            and any(outcome.horizon == "CLOSE" and outcome.status != "complete" for outcome in group_outcomes)
        )
        if close_daily_fallback_needed:
            try:
                daily_bars = daily_close_source.get_daily_bars(symbol, event_day - timedelta(days=10), event_day + timedelta(days=20))
            except Exception as exc:
                _record_source_error(report, f"{symbol}:{event_day.isoformat()}:close_daily_fallback", exc)
                _append_progress(
                    progress_path,
                    {
                        "stage": "intraday_group_close_daily_fallback_failed",
                        "group_index": index,
                        "symbol": symbol,
                        "event_day": event_day.isoformat(),
                        "candidate_count": len(rows),
                        "error": str(exc),
                    },
                )
            else:
                replaced = 0
                refreshed: list[BuiltOutcome] = []
                for outcome in group_outcomes:
                    if outcome.horizon == "CLOSE" and outcome.status != "complete":
                        fallback = _build_close_daily_fallback(
                            outcome.candidate,
                            daily_bars,
                            price_source=daily_close_source.name,
                        )
                        if fallback.status == "complete":
                            refreshed.append(fallback)
                            replaced += 1
                            continue
                    refreshed.append(outcome)
                group_outcomes = refreshed
                _append_progress(
                    progress_path,
                    {
                        "stage": "intraday_group_close_daily_fallback",
                        "group_index": index,
                        "symbol": symbol,
                        "event_day": event_day.isoformat(),
                        "candidate_count": len(rows),
                        "replaced_count": replaced,
                    },
                )
        if store_batch is not None:
            store_batch(group_outcomes)
        else:
            outcomes.extend(group_outcomes)
        final_statuses = {outcome.status for outcome in group_outcomes}
        has_blocked_outcome = "blocked" in final_statuses
        skipped_no_tradable_count = sum(
            1 for outcome in group_outcomes if outcome.status == SKIPPED_NO_TRADABLE_INTRADAY_STATUS
        )
        if not bars and closed_session_reason:
            zero_bar_classification = "scheduled_market_closure"
        elif not bars and skipped_no_tradable_count and not has_blocked_outcome:
            zero_bar_classification = "no_tradable_intraday_session"
        elif not bars and skipped_no_tradable_count:
            zero_bar_classification = "partial_no_tradable_intraday_session"
        else:
            zero_bar_classification = None
        no_tradable_reasons = sorted(
            {
                outcome.blocker_reason.split(":", 1)[1]
                for outcome in group_outcomes
                if outcome.status == SKIPPED_NO_TRADABLE_INTRADAY_STATUS
                and outcome.blocker_reason.startswith("no_tradable_intraday_session:")
            }
        )
        report["groups_processed"] = int(report.get("groups_processed") or 0) + 1
        _append_progress(
            progress_path,
            {
                "stage": "intraday_group_complete",
                "group_index": index,
                "symbol": symbol,
                "event_day": event_day.isoformat(),
                "candidate_count": len(rows),
                "bar_count": len(bars),
                "fetch_mode": fetch_mode,
                "requested_horizons": list(group_horizons),
                "zero_bar_classification": zero_bar_classification,
                "closed_session_reason": closed_session_reason if not bars else None,
                "no_tradable_intraday_reason": ";".join(no_tradable_reasons) if no_tradable_reasons else None,
                "skipped_no_tradable_intraday_count": skipped_no_tradable_count,
                "zero_bar_is_error": not bars and not closed_session_reason and has_blocked_outcome,
            },
        )
    return outcomes


def _candidate_requested_horizons(candidate: NewsOutcomeCandidate, horizons: tuple[str, ...]) -> tuple[str, ...]:
    requested = tuple(horizon for horizon in (candidate.requested_horizons or horizons) if horizon in horizons)
    return requested or horizons


def _build_intraday_outcome(
    candidate: NewsOutcomeCandidate,
    bars: list[IntradayBar],
    *,
    horizon: str,
    price_source: str,
    closed_session_reason: str | None = None,
    no_tradable_intraday_reason: str | None = None,
) -> BuiltOutcome:
    target = _intraday_target(candidate.published_at, horizon)
    baseline_bar = _latest_intraday_leq(bars, candidate.published_at)
    if horizon == "CLOSE":
        reaction_bar = _next_regular_session_close_bar_after(bars, candidate.published_at)
        if reaction_bar is not None:
            target = reaction_bar.timestamp
    else:
        reaction_bar = _first_intraday_geq(bars, target)
    blockers: list[str] = []
    if baseline_bar is None:
        blockers.append("baseline_price_unavailable")
    if reaction_bar is None:
        blockers.append("reaction_price_unavailable")
    raw_return = None
    if baseline_bar and reaction_bar and baseline_bar.close > 0:
        raw_return = reaction_bar.close / baseline_bar.close - 1.0
    status = "complete" if raw_return is not None else "blocked"
    if not bars and closed_session_reason:
        blockers = [f"scheduled_market_closure:{closed_session_reason}"]
        status = SKIPPED_CLOSED_SESSION_STATUS
    elif raw_return is None and no_tradable_intraday_reason:
        blockers = [f"no_tradable_intraday_session:{no_tradable_intraday_reason}"]
        status = SKIPPED_NO_TRADABLE_INTRADAY_STATUS
    return BuiltOutcome(
        candidate=candidate,
        horizon=horizon,
        due_at=target,
        captured_at=reaction_bar.timestamp if reaction_bar else None,
        baseline_at=baseline_bar.timestamp if baseline_bar else None,
        baseline_price=baseline_bar.close if baseline_bar else None,
        reaction_price=reaction_bar.close if reaction_bar else None,
        raw_return=raw_return,
        price_source=f"{price_source}_news_backfill",
        status=status,
        blocker_reason=";".join(dict.fromkeys(blockers)),
    )


def _build_close_daily_fallback(
    candidate: NewsOutcomeCandidate,
    bars: list[DailyBar],
    *,
    price_source: str,
) -> BuiltOutcome:
    event_ny = candidate.published_at.astimezone(NY)
    event_date = event_ny.date()
    baseline_index = _latest_daily_index_leq(bars, event_date) if event_ny.time() >= time(16, 0) else _latest_daily_index_lt(bars, event_date)
    event_date_has_bar = any(bar.date == event_date for bar in bars)
    if event_date.weekday() >= 5 or not event_date_has_bar:
        baseline_index = _latest_daily_index_leq(bars, event_date)
    blockers: list[str] = []
    baseline_bar: DailyBar | None = None
    reaction_bar: DailyBar | None = None
    if baseline_index is None:
        blockers.append("baseline_price_unavailable")
    else:
        baseline_bar = bars[baseline_index]
        if event_date_has_bar and event_ny.time() < time(16, 0):
            reaction_index = _latest_daily_index_leq(bars, event_date)
        else:
            reaction_index = baseline_index + 1
        if reaction_index is None or reaction_index >= len(bars):
            blockers.append("reaction_price_unavailable")
        else:
            reaction_bar = bars[reaction_index]
    raw_return = None
    if baseline_bar and reaction_bar and baseline_bar.close > 0:
        raw_return = reaction_bar.close / baseline_bar.close - 1.0
    captured_at = _market_close_utc(reaction_bar.date) if reaction_bar else None
    due_at = captured_at or _market_close_utc(_next_weekday(event_date))
    return BuiltOutcome(
        candidate=candidate,
        horizon="CLOSE",
        due_at=due_at,
        captured_at=captured_at,
        baseline_at=_market_close_utc(baseline_bar.date) if baseline_bar else None,
        baseline_price=baseline_bar.close if baseline_bar else None,
        reaction_price=reaction_bar.close if reaction_bar else None,
        raw_return=raw_return,
        price_source=f"{price_source}_close_daily_fallback",
        status="complete" if raw_return is not None else "blocked",
        blocker_reason=";".join(dict.fromkeys(blockers)),
    )


def _store_outcomes(
    conn: sqlite3.Connection,
    outcomes: list[BuiltOutcome],
    *,
    flat_threshold: float,
) -> dict[str, Any]:
    now = utc_iso(datetime.now(UTC))
    status_counts: Counter[str] = Counter()
    horizon_counts: Counter[str] = Counter()
    symbol_counts: Counter[str] = Counter()
    samples: list[dict[str, Any]] = []
    completed = 0
    blocked = 0
    skipped_closed_session = 0
    skipped_no_tradable_intraday = 0
    reactions = 0
    targets = 0
    baselines = 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        for outcome in outcomes:
            status_counts[outcome.status] += 1
            horizon_counts[outcome.horizon] += 1
            symbol_counts[outcome.candidate.symbol] += 1
            if outcome.status == SKIPPED_CLOSED_SESSION_STATUS:
                skipped_closed_session += 1
                target_id = _upsert_reaction_target(
                    conn,
                    outcome,
                    now=now,
                    status=SKIPPED_CLOSED_SESSION_STATUS,
                    last_error=outcome.blocker_reason or "scheduled_market_closure",
                )
                targets += 1 if target_id else 0
                continue
            if outcome.status == SKIPPED_NO_TRADABLE_INTRADAY_STATUS:
                skipped_no_tradable_intraday += 1
                target_id = _upsert_reaction_target(
                    conn,
                    outcome,
                    now=now,
                    status=SKIPPED_NO_TRADABLE_INTRADAY_STATUS,
                    last_error=outcome.blocker_reason or "no_tradable_intraday_session",
                )
                targets += 1 if target_id else 0
                continue
            if outcome.status != "complete" or outcome.raw_return is None or outcome.baseline_price is None or outcome.reaction_price is None or outcome.captured_at is None or outcome.baseline_at is None:
                blocked += 1
                target_id = _upsert_reaction_target(conn, outcome, now=now, status="failed", last_error=outcome.blocker_reason or "outcome_unavailable")
                targets += 1 if target_id else 0
                if len(samples) < 20:
                    samples.append(_sample_outcome(outcome))
                continue
            completed += 1
            target_id = _upsert_reaction_target(conn, outcome, now=now, status="complete", last_error="")
            targets += 1
            baselines += _insert_baseline_if_missing(conn, outcome)
            direction = _direction_label(outcome.raw_return, flat_threshold)
            conn.execute(
                """
                INSERT INTO market_reactions(
                    target_id,news_id,symbol,horizon,due_at,captured_at,
                    baseline_price,reaction_price,raw_return,price_source,created_at,
                    usage_status,usage_reason,governance_analysis_id,calibration_eligible,observation_eligible
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(target_id) DO UPDATE SET
                    due_at=excluded.due_at,
                    captured_at=excluded.captured_at,
                    baseline_price=excluded.baseline_price,
                    reaction_price=excluded.reaction_price,
                    raw_return=excluded.raw_return,
                    price_source=excluded.price_source,
                    usage_status=excluded.usage_status,
                    usage_reason=excluded.usage_reason,
                    calibration_eligible=excluded.calibration_eligible,
                    observation_eligible=excluded.observation_eligible
                """,
                (
                    target_id,
                    outcome.candidate.news_id,
                    outcome.candidate.symbol,
                    outcome.horizon,
                    utc_iso(outcome.due_at),
                    utc_iso(outcome.captured_at),
                    outcome.baseline_price,
                    outcome.reaction_price,
                    outcome.raw_return,
                    outcome.price_source,
                    now,
                    "observation_only",
                    f"{NEWS_MARKET_OUTCOME_BACKFILL_VERSION};direction={direction}",
                    None,
                    0,
                    1,
                ),
            )
            reactions += 1
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {
        "processed_candidates": len({outcome.candidate.news_id for outcome in outcomes}),
        "completed_outcomes": completed,
        "blocked_outcomes": blocked,
        "skipped_closed_session_outcomes": skipped_closed_session,
        "skipped_no_tradable_intraday_outcomes": skipped_no_tradable_intraday,
        "inserted_or_updated_reactions": reactions,
        "created_or_updated_targets": targets,
        "baseline_rows_inserted": baselines,
        "status_counts": dict(status_counts),
        "horizon_counts": dict(horizon_counts),
        "symbol_counts": dict(symbol_counts.most_common(50)),
        "sample_blockers": samples,
    }


def _empty_store_totals() -> dict[str, Any]:
    return {
        "processed_candidates": 0,
        "completed_outcomes": 0,
        "blocked_outcomes": 0,
        "skipped_closed_session_outcomes": 0,
        "skipped_no_tradable_intraday_outcomes": 0,
        "inserted_or_updated_reactions": 0,
        "created_or_updated_targets": 0,
        "baseline_rows_inserted": 0,
        "status_counts": {},
        "horizon_counts": {},
        "symbol_counts": {},
        "sample_blockers": [],
    }


def _merge_store_totals(target: dict[str, Any], source: dict[str, Any]) -> None:
    for key in (
        "processed_candidates",
        "completed_outcomes",
        "blocked_outcomes",
        "skipped_closed_session_outcomes",
        "skipped_no_tradable_intraday_outcomes",
        "inserted_or_updated_reactions",
        "created_or_updated_targets",
        "baseline_rows_inserted",
    ):
        target[key] = int(target.get(key) or 0) + int(source.get(key) or 0)
    for key in ("status_counts", "horizon_counts", "symbol_counts"):
        merged = Counter(target.get(key) or {})
        merged.update(source.get(key) or {})
        target[key] = dict(merged.most_common(50)) if key == "symbol_counts" else dict(merged)
    samples = list(target.get("sample_blockers") or [])
    for sample in source.get("sample_blockers") or []:
        if len(samples) >= 20:
            break
        samples.append(sample)
    target["sample_blockers"] = samples


def _upsert_reaction_target(
    conn: sqlite3.Connection,
    outcome: BuiltOutcome,
    *,
    now: str,
    status: str,
    last_error: str,
) -> int:
    conn.execute(
        """
        INSERT OR IGNORE INTO reaction_targets(
            news_id,symbol,horizon,due_at,next_attempt_at,status,attempts,last_error,updated_at,
            usage_status,usage_reason,governance_analysis_id,calibration_eligible,observation_eligible
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            outcome.candidate.news_id,
            outcome.candidate.symbol,
            outcome.horizon,
            utc_iso(outcome.due_at),
            None if status in {"complete", *RETRY_TERMINAL_TARGET_STATUSES} else utc_iso(outcome.due_at),
            status,
            0,
            last_error[:500] if last_error else None,
            now,
            "observation_only",
            NEWS_MARKET_OUTCOME_BACKFILL_VERSION,
            None,
            0,
            1,
        ),
    )
    row = conn.execute(
        "SELECT id FROM reaction_targets WHERE news_id=? AND symbol=? AND horizon=?",
        (outcome.candidate.news_id, outcome.candidate.symbol, outcome.horizon),
    ).fetchone()
    target_id = int(row["id"])
    conn.execute(
        """
        UPDATE reaction_targets
           SET due_at=?,
               next_attempt_at=?,
               status=?,
               last_error=?,
               updated_at=?,
               usage_status='observation_only',
               usage_reason=?,
               calibration_eligible=0,
               observation_eligible=1
         WHERE id=?
        """,
        (
            utc_iso(outcome.due_at),
            None if status in {"complete", *RETRY_TERMINAL_TARGET_STATUSES} else utc_iso(outcome.due_at),
            status,
            last_error[:500] if last_error else None,
            now,
            NEWS_MARKET_OUTCOME_BACKFILL_VERSION,
            target_id,
        ),
    )
    return target_id


def _insert_baseline_if_missing(conn: sqlite3.Connection, outcome: BuiltOutcome) -> int:
    exists = conn.execute(
        "SELECT 1 FROM market_baselines WHERE news_id=? AND symbol=?",
        (outcome.candidate.news_id, outcome.candidate.symbol),
    ).fetchone()
    if exists:
        return 0
    assert outcome.baseline_at is not None
    assert outcome.baseline_price is not None
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO market_baselines(
            news_id,symbol,captured_at,bid,ask,last,midpoint,selected_price,
            selected_price_source,volume,currency,con_id,contract_symbol,sec_type,exchange,
            primary_exchange,multiplier,market_data_type,quote_integrity_status,
            quote_integrity_reasons_json,reaction_baseline_eligible,spread_ratio,
            last_midpoint_deviation,close_reference_deviation
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            outcome.candidate.news_id,
            outcome.candidate.symbol,
            utc_iso(outcome.baseline_at),
            None,
            None,
            outcome.baseline_price,
            outcome.baseline_price,
            outcome.baseline_price,
            outcome.price_source,
            None,
            "USD",
            None,
            outcome.candidate.symbol,
            "STK",
            "HISTORICAL",
            "",
            "",
            None,
            "historical_backfill",
            json.dumps([NEWS_MARKET_OUTCOME_BACKFILL_VERSION], ensure_ascii=False),
            1,
            None,
            None,
            None,
        ),
    )
    return int(cursor.rowcount > 0)


def _ensure_indexes(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE INDEX IF NOT EXISTS idx_market_reactions_news_symbol_horizon ON market_reactions(news_id,symbol,horizon)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_news_symbols_symbol_news ON news_symbols(symbol,news_id)")


def _intraday_target(event_time: datetime, horizon: str) -> datetime:
    horizon = horizon.upper()
    if horizon == "5M":
        return event_time + timedelta(minutes=5)
    if horizon == "15M":
        return event_time + timedelta(minutes=15)
    if horizon == "1H":
        return event_time + timedelta(hours=1)
    if horizon == "CLOSE":
        return _next_regular_close_after(event_time)
    raise ValueError(f"Unsupported intraday horizon: {horizon}")


def _no_tradable_intraday_reason(event_time: datetime, horizon: str) -> str | None:
    """Return a terminal skip reason for short intraday horizons outside common US equity hours.

    Major US stocks/ETFs can trade in extended hours, but 5M/15M/1H outcomes
    are not well-defined when the event itself is outside the common 04:00-20:00
    ET tradable window or when the requested short-horizon target falls outside
    that window.  CLOSE/NEXT_DAY remain available through their own paths.
    """
    horizon = str(horizon or "").upper()
    if horizon == "CLOSE":
        return None
    event_local = event_time.astimezone(NY)
    target_local = _intraday_target(event_time, horizon).astimezone(NY)
    event_local_time = event_local.time()
    target_local_time = target_local.time()
    if event_local_time < EXTENDED_SESSION_OPEN:
        return "event_before_extended_session_open"
    if event_local_time > EXTENDED_SESSION_CLOSE:
        return "event_after_extended_session_close"
    if target_local_time < EXTENDED_SESSION_OPEN:
        return "target_before_extended_session_open"
    if target_local_time > EXTENDED_SESSION_CLOSE:
        return "target_after_extended_session_close"
    return None


def _intraday_fetch_mode(event_day: date, rows: list[NewsOutcomeCandidate], horizons: tuple[str, ...]) -> str:
    if "CLOSE" in horizons and _us_equity_market_closure_reason(event_day):
        return "wide"
    for candidate in rows:
        local_time = candidate.published_at.astimezone(NY).time()
        if local_time < time(9, 30) or local_time > time(16, 0):
            return "overnight"
    return "normal"


def _intraday_fetch_window(start_times: list[datetime], target_times: list[datetime], *, mode: str) -> tuple[datetime, datetime]:
    if mode == "wide":
        return min(start_times) - timedelta(days=5), max(target_times) + timedelta(days=5)
    if mode == "overnight":
        return min(start_times) - timedelta(hours=18), max(target_times) + timedelta(hours=2)
    return min(start_times) - timedelta(hours=2), max(target_times) + timedelta(hours=2)


def _next_regular_close_after(value: datetime) -> datetime:
    local = value.astimezone(NY)
    candidate = local.date()
    if local.time() >= time(16, 0):
        candidate = _next_trading_day(candidate)
    while _us_equity_market_closure_reason(candidate):
        candidate = _next_trading_day(candidate)
    return _market_close_utc(candidate)


def _next_regular_session_close_bar_after(bars: list[IntradayBar], event_time: datetime) -> IntradayBar | None:
    event_utc = event_time.astimezone(UTC)
    by_session: dict[date, list[IntradayBar]] = defaultdict(list)
    for bar in bars:
        timestamp_utc = bar.timestamp.astimezone(UTC)
        if timestamp_utc <= event_utc:
            continue
        local = timestamp_utc.astimezone(NY)
        local_time = local.time()
        if time(9, 30) <= local_time <= time(16, 0):
            by_session[local.date()].append(bar)
    for session_date in sorted(by_session):
        session_bars = sorted(by_session[session_date], key=lambda item: item.timestamp)
        if session_bars:
            return session_bars[-1]
    return None


def _next_weekday(value: date) -> date:
    candidate = value + timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate


def _next_trading_day(value: date) -> date:
    candidate = value + timedelta(days=1)
    while _us_equity_market_closure_reason(candidate):
        candidate += timedelta(days=1)
    return candidate


def _us_equity_market_closure_reason(value: date) -> str | None:
    if value in US_EQUITY_SPECIAL_CLOSURES:
        return US_EQUITY_SPECIAL_CLOSURES[value]
    if value.weekday() >= 5:
        return "weekend"
    holidays = _us_equity_market_holidays(value.year)
    return holidays.get(value)


def _us_equity_market_holidays(year: int) -> dict[date, str]:
    holidays: dict[date, str] = {}

    def add(day: date, reason: str) -> None:
        holidays[day] = reason

    add(_observed_fixed_holiday(year, 1, 1), "new_years_day")
    add(_nth_weekday(year, 1, 0, 3), "martin_luther_king_jr_day")
    add(_nth_weekday(year, 2, 0, 3), "washingtons_birthday")
    add(_easter_sunday(year) - timedelta(days=2), "good_friday")
    add(_last_weekday(year, 5, 0), "memorial_day")
    add(_observed_fixed_holiday(year, 6, 19), "juneteenth")
    add(_observed_fixed_holiday(year, 7, 4), "independence_day")
    add(_nth_weekday(year, 9, 0, 1), "labor_day")
    add(_nth_weekday(year, 11, 3, 4), "thanksgiving_day")
    add(_observed_fixed_holiday(year, 12, 25), "christmas_day")
    return holidays


def _observed_fixed_holiday(year: int, month: int, day: int) -> date:
    actual = date(year, month, day)
    if actual.weekday() == 5:
        return actual - timedelta(days=1)
    if actual.weekday() == 6:
        return actual + timedelta(days=1)
    return actual


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    candidate = date(year, month, 1)
    while candidate.weekday() != weekday:
        candidate += timedelta(days=1)
    return candidate + timedelta(days=7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    if month == 12:
        candidate = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        candidate = date(year, month + 1, 1) - timedelta(days=1)
    while candidate.weekday() != weekday:
        candidate -= timedelta(days=1)
    return candidate


def _easter_sunday(year: int) -> date:
    # Anonymous Gregorian algorithm.
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _market_close_utc(value: date) -> datetime:
    return datetime.combine(value, time(16, 0), tzinfo=NY).astimezone(UTC)


def _latest_daily_index_leq(bars: list[DailyBar], target: date) -> int | None:
    answer = None
    for index, bar in enumerate(bars):
        if bar.date <= target:
            answer = index
        else:
            break
    return answer


def _latest_daily_index_lt(bars: list[DailyBar], target: date) -> int | None:
    answer = None
    for index, bar in enumerate(bars):
        if bar.date < target:
            answer = index
        else:
            break
    return answer


def _latest_intraday_leq(bars: list[IntradayBar], target: datetime) -> IntradayBar | None:
    answer = None
    target = target.astimezone(UTC)
    for bar in bars:
        if bar.timestamp.astimezone(UTC) <= target:
            answer = bar
        else:
            break
    return answer


def _first_intraday_geq(bars: list[IntradayBar], target: datetime) -> IntradayBar | None:
    target = target.astimezone(UTC)
    for bar in bars:
        if bar.timestamp.astimezone(UTC) >= target:
            return bar
    return None


def _direction_label(raw_return: float, threshold: float) -> str:
    if raw_return >= threshold:
        return "up"
    if raw_return <= -threshold:
        return "down"
    return "flat"


def _sample_outcome(outcome: BuiltOutcome) -> dict[str, Any]:
    return {
        "news_id": outcome.candidate.news_id,
        "symbol": outcome.candidate.symbol,
        "horizon": outcome.horizon,
        "published_at": outcome.candidate.published_at.isoformat(),
        "status": outcome.status,
        "blocker_reason": outcome.blocker_reason,
    }


def _append_progress(path: Path | None, payload: dict[str, Any]) -> None:
    if path is None:
        return
    record = {"generated_at": datetime.now(UTC).isoformat(), **payload}
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def news_market_outcome_backfill_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# News Market Outcome Backfill",
        "",
        f"- Version: `{report.get('version')}`",
        f"- Range: `{report.get('start')}` to `{report.get('end')}`",
        f"- Horizons: `{report.get('horizons')}`",
        f"- Price source: `{report.get('price_source')}`",
        f"- Symbol tier config: `{report.get('symbol_tier_config')}`",
        f"- Symbol tier filter: `{report.get('symbol_tier_filter') or []}`",
        f"- Candidate tier counts: `{report.get('candidate_tier_counts') or {}}`",
        f"- Candidates: `{report.get('candidate_count')}`",
        f"- Requested outcomes: `{report.get('requested_outcome_count')}`",
        f"- Completed outcomes: `{report.get('completed_outcomes')}`",
        f"- Blocked outcomes: `{report.get('blocked_outcomes')}`",
        f"- Skipped closed-session outcomes: `{report.get('skipped_closed_session_outcomes')}`",
        f"- Skipped no-tradable intraday outcomes: `{report.get('skipped_no_tradable_intraday_outcomes')}`",
        f"- Reactions inserted/updated: `{report.get('inserted_or_updated_reactions')}`",
        f"- Baselines inserted: `{report.get('baseline_rows_inserted')}`",
        f"- Groups processed: `{report.get('groups_processed')}`",
        f"- Close retry bypass: after `{report.get('close_retry_bypass_after_failures')}` failures, bypassed `{report.get('close_retry_bypassed_count')}` groups",
        "",
        "## Counts",
        "",
        f"- Status counts: `{report.get('status_counts') or {}}`",
        f"- Horizon counts: `{report.get('horizon_counts') or {}}`",
        f"- Source errors: `{len(report.get('source_errors') or {})}`",
        f"- Source error categories: `{report.get('source_error_categories') or {}}`",
        "",
        "## Sample blockers",
        "",
    ]
    for item in (report.get("sample_blockers") or [])[:20]:
        lines.append(f"- news `{item.get('news_id')}` `{item.get('symbol')}` `{item.get('horizon')}`: {item.get('blocker_reason')}")
    lines.extend(
        [
            "",
            "## Safety",
            "",
            "- Read-only historical price fetch.",
            "- Writes only local market outcome rows as `observation_only`.",
            "- No broker orders, no transmit, no live routing.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_news_market_outcome_backfill_report(
    report: dict[str, Any],
    *,
    output: Path | None,
    json_output_path: Path | None,
) -> None:
    if json_output_path:
        json_output_path.parent.mkdir(parents=True, exist_ok=True)
        json_output_path.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(news_market_outcome_backfill_markdown(report), encoding="utf-8")
