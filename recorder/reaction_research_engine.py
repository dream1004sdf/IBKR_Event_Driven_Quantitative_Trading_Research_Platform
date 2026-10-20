from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from .news_lineage_v2 import (
    NEWS_ASSET_MAPPER_VERSION,
    NEWS_EVENT_CLUSTERER_VERSION,
    REACTION_RESEARCH_ENGINE_VERSION,
    SECURITY_MASTER_VERSION,
    ensure_news_lineage_v2_schema,
)

CORE_SCOPE_VERSION = "core-reaction-scope-v1"
EVENT_PROXY_MAP_VERSION = "event-proxy-map-v1"
REACTION_LABEL_OUTCOME_BUILDER_VERSION = "m253-reaction-label-stub-v1"
CALENDAR_VERSION = "us-equity-session-approx-v1"

DEFAULT_CORE_ASSETS: tuple[tuple[str, str, str], ...] = (
    ("SPY", "core_index", "broad_us_equity"),
    ("QQQ", "core_index", "nasdaq_growth"),
    ("IWM", "core_index", "small_caps"),
    ("DIA", "core_index", "dow_industrials"),
    ("TLT", "core_macro", "rates"),
    ("UUP", "core_macro", "dollar"),
    ("GLD", "core_macro", "gold"),
    ("USO", "core_macro", "oil"),
    ("XLK", "sector_context", "technology"),
    ("XLE", "sector_context", "energy"),
    ("XLF", "sector_context", "financials"),
    ("SMH", "sector_context", "semis"),
    ("NVDA", "core_bigtech", "ai_semis"),
    ("AAPL", "core_bigtech", "mega_cap"),
    ("MSFT", "core_bigtech", "mega_cap"),
    ("AMZN", "core_bigtech", "mega_cap"),
    ("META", "core_bigtech", "mega_cap"),
    ("TSLA", "core_bigtech", "mega_cap"),
)

EVENT_PROXY_RULES: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("semis", ("SMH", "SOXX", "NVDA", "TSM", "AVGO"), ("semiconductor", "chip", "nvidia", "tsm", "ai chip", "taiwan")),
    ("oil", ("USO", "XLE", "XOP", "XOM", "CVX"), ("oil", "crude", "petroleum", "eia", "gasoline", "opec", "hormuz")),
    ("rates", ("TLT", "IEF", "SHY", "TIP", "UUP", "GLD"), ("fed", "fomc", "cpi", "pce", "inflation", "rate", "yield", "payroll", "jobless")),
    ("defense", ("ITA", "XAR", "LMT", "RTX", "NOC"), ("war", "missile", "attack", "nato", "defense", "houthi", "ukraine", "israel")),
    ("cyber", ("CIBR", "HACK", "PANW", "CRWD", "FTNT"), ("cyber", "hack", "breach", "ransomware")),
    ("china", ("FXI", "KWEB", "EWT", "EWY", "TSM"), ("china", "taiwan", "tariff", "export control", "beijing")),
    ("grains", ("DBA", "WEAT", "CORN"), ("grain", "wheat", "corn", "crop", "wasde")),
    ("europe", ("VGK", "FEZ", "EUFN", "FXE"), ("ecb", "euro", "eurozone", "germany", "bund")),
    ("japan", ("EWJ", "DXJ", "FXY", "UUP"), ("boj", "japan", "tokyo cpi", "tankan", "yen", "jgb")),
)


@dataclass(frozen=True)
class ReactionAsset:
    asset_symbol: str
    con_id: int | None
    asset_role: str
    proxy_family: str
    inclusion_reason: str
    inclusion_source: str
    tradable_candidate: bool
    label_scope: str


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _window_delta(window: str) -> timedelta | None:
    normalized = str(window or "").strip().lower()
    if normalized in {"5m", "5min", "5"}:
        return timedelta(minutes=5)
    if normalized in {"15m", "15min", "15"}:
        return timedelta(minutes=15)
    if normalized in {"1h", "60m", "60min"}:
        return timedelta(hours=1)
    if normalized in {"1d", "next_day"}:
        return timedelta(days=1)
    if normalized in {"5d"}:
        return timedelta(days=5)
    if normalized in {"close", "eod"}:
        return timedelta(hours=6, minutes=30)
    return None


def _session_bucket(value: datetime | None) -> str:
    if value is None:
        return "unknown"
    minutes = value.hour * 60 + value.minute
    # Approximate NYSE regular session in UTC.  This is a label-quality hint;
    # strict calendar handling belongs to the eventual outcome builder.
    if 14 * 60 + 30 <= minutes <= 21 * 60:
        return "regular"
    if 9 * 60 <= minutes < 14 * 60 + 30:
        return "premarket"
    return "afterhours"


def ensure_reaction_research_schema(conn: sqlite3.Connection) -> None:
    ensure_news_lineage_v2_schema(conn)


def _cluster_context(conn: sqlite3.Connection, event_cluster_id: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM news_event_clusters WHERE event_cluster_id=?",
        (int(event_cluster_id),),
    ).fetchone()
    if row is None:
        raise ValueError(f"event_cluster_id={event_cluster_id} does not exist")
    role = conn.execute(
        "SELECT * FROM news_event_cluster_roles WHERE event_cluster_id=?",
        (int(event_cluster_id),),
    ).fetchone()
    text_parts = [
        row["primary_headline"],
        row["primary_event_name"] if "primary_event_name" in row.keys() else "",
        row["event_class"],
        role["event_role"] if role else "",
        role["role_family"] if role else "",
    ]
    return {
        "row": row,
        "role": role,
        "text": " ".join(str(part or "") for part in text_parts).lower(),
        "anchor_time": str(row["first_seen_at_utc"] or row["last_seen_at_utc"] or row["created_at"] or ""),
    }


def _direct_assets(conn: sqlite3.Connection, event_cluster_id: int) -> list[ReactionAsset]:
    if _table_has_rows(conn, "v_cluster_asset_candidate_aggregates"):
        rows = conn.execute(
            """SELECT symbol,con_id,confidence,'' AS evidence,source,
                      supporting_news_count,raw_candidate_rows
                 FROM v_cluster_asset_candidate_aggregates
                WHERE event_cluster_id=?
                  AND symbol IS NOT NULL
                  AND symbol <> ''
                  AND role IN ('primary','secondary')
                  AND source NOT IN ('ibkr_route','fallback_context')
                  AND resolver_status='resolved'
                  AND tradable_candidate=1
                  AND confidence>=0.50
                ORDER BY confidence DESC,
                         supporting_news_count DESC,
                         raw_candidate_rows DESC,
                         symbol ASC""",
            (int(event_cluster_id),),
        ).fetchall()
    elif _table_has_rows(conn, "news_asset_candidates"):
        rows = conn.execute(
            """SELECT symbol,con_id,confidence,evidence,source,
                      1 AS supporting_news_count,1 AS raw_candidate_rows
                 FROM news_asset_candidates
                WHERE event_cluster_id=?
                  AND role IN ('primary','secondary')
                  AND source NOT IN ('ibkr_route','fallback_context')
                  AND resolver_status='resolved'
                  AND tradable_candidate=1
                  AND confidence>=0.50
                GROUP BY symbol
                ORDER BY MAX(confidence) DESC, symbol ASC""",
            (int(event_cluster_id),),
        ).fetchall()
    else:
        return []
    assets: list[ReactionAsset] = []
    seen_symbols: set[str] = set()
    for row in rows:
        symbol = str(row["symbol"] or "").upper()
        if not symbol or symbol in seen_symbols:
            continue
        seen_symbols.add(symbol)
        assets.append(
            ReactionAsset(
                asset_symbol=symbol,
                con_id=int(row["con_id"]) if row["con_id"] is not None else None,
                asset_role="direct_entity",
                proxy_family="",
                inclusion_reason=f"resolved content entity via {row['source']}",
                inclusion_source="provider_entity" if "entity" in str(row["source"] or "") else "direct_mention",
                tradable_candidate=True,
                label_scope="direct",
            )
        )
    return assets


def _core_assets() -> list[ReactionAsset]:
    return [
        ReactionAsset(
            asset_symbol=symbol,
            con_id=None,
            asset_role=role,
            proxy_family=family,
            inclusion_reason=f"{CORE_SCOPE_VERSION}:{family}",
            inclusion_source="core_scope",
            tradable_candidate=False,
            label_scope="core_context",
        )
        for symbol, role, family in DEFAULT_CORE_ASSETS
    ]


def _event_proxy_assets(context_text: str, event_class: str) -> list[ReactionAsset]:
    haystack = f"{context_text} {event_class}".lower()
    assets: dict[tuple[str, str], ReactionAsset] = {}
    for family, symbols, needles in EVENT_PROXY_RULES:
        if any(needle in haystack for needle in needles):
            for symbol in symbols:
                assets[(symbol, family)] = ReactionAsset(
                    asset_symbol=symbol,
                    con_id=None,
                    asset_role="event_proxy",
                    proxy_family=family,
                    inclusion_reason=f"{event_class or 'unknown'} matched {family} in {EVENT_PROXY_MAP_VERSION}",
                    inclusion_source="event_proxy_map",
                    tradable_candidate=False,
                    label_scope="event_proxy",
                )
    return list(assets.values())


def _table_has_rows(conn: sqlite3.Connection, table: str) -> bool:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?",
        (table,),
    ).fetchone()
    if not exists:
        return False
    row = conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
    return row is not None


def _insert_asset(conn: sqlite3.Connection, reaction_plan_id: int, event_cluster_id: int, asset: ReactionAsset) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO reaction_universe_assets(
               reaction_plan_id,event_cluster_id,asset_symbol,con_id,asset_role,proxy_family,
               inclusion_reason,inclusion_source,tradable_candidate,label_scope,created_at
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (
            int(reaction_plan_id),
            int(event_cluster_id),
            asset.asset_symbol.upper(),
            asset.con_id,
            asset.asset_role,
            asset.proxy_family,
            asset.inclusion_reason,
            asset.inclusion_source,
            1 if asset.tradable_candidate else 0,
            asset.label_scope,
            _utc_now_iso(),
        ),
    )


def create_reaction_universe_plan(
    conn: sqlite3.Connection,
    event_cluster_id: int,
    *,
    plan_version: str = REACTION_RESEARCH_ENGINE_VERSION,
    include_core: bool = True,
    include_event_proxies: bool = True,
) -> int:
    """Create the canonical reaction plan for one event cluster.

    M253's central guardrail is that labels are generated only for assets that
    were selected by this plan.  This preserves why a direct/core/event-proxy
    asset was observed and prevents after-the-fact proxy selection.
    """

    ensure_reaction_research_schema(conn)
    context = _cluster_context(conn, int(event_cluster_id))
    cluster = context["row"]
    if str(cluster["cluster_type"] or "") in {"macro_release", "central_bank_event"}:
        canonical_macro = conn.execute(
            """SELECT 1
                 FROM v_canonical_reaction_trigger_macro_events
                WHERE event_cluster_id=?
                LIMIT 1""",
            (int(event_cluster_id),),
        ).fetchone()
        if canonical_macro is None:
            raise ValueError(
                "reaction plan rejected: macro cluster is not present in "
                "v_canonical_reaction_trigger_macro_events"
            )
    available_cutoff = str(cluster["first_seen_at_utc"] or cluster["last_seen_at_utc"] or "")
    now = _utc_now_iso()
    conn.execute(
        """INSERT OR IGNORE INTO reaction_universe_plans(
               event_cluster_id,plan_version,created_at,available_at_cutoff,core_scope_version,
               event_proxy_map_version,security_master_version,mapper_version,classifier_version,reason
           ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            int(event_cluster_id),
            plan_version,
            now,
            available_cutoff,
            CORE_SCOPE_VERSION,
            EVENT_PROXY_MAP_VERSION,
            SECURITY_MASTER_VERSION,
            NEWS_ASSET_MAPPER_VERSION,
            NEWS_EVENT_CLUSTERER_VERSION,
            "M253 event_cluster-centered reaction plan",
        ),
    )
    plan_row = conn.execute(
        "SELECT reaction_plan_id FROM reaction_universe_plans WHERE event_cluster_id=? AND plan_version=?",
        (int(event_cluster_id), plan_version),
    ).fetchone()
    if plan_row is None:
        raise RuntimeError(f"failed to create reaction plan for event_cluster_id={event_cluster_id}")
    reaction_plan_id = int(plan_row["reaction_plan_id"])

    for asset in _direct_assets(conn, int(event_cluster_id)):
        _insert_asset(conn, reaction_plan_id, int(event_cluster_id), asset)
    if include_core:
        for asset in _core_assets():
            _insert_asset(conn, reaction_plan_id, int(event_cluster_id), asset)
    if include_event_proxies:
        for asset in _event_proxy_assets(context["text"], str(cluster["event_class"] or "")):
            _insert_asset(conn, reaction_plan_id, int(event_cluster_id), asset)
    return reaction_plan_id


def _planned_asset(
    conn: sqlite3.Connection,
    reaction_plan_id: int,
    asset_symbol: str,
    asset_role: str,
    proxy_family: str,
) -> sqlite3.Row | None:
    return conn.execute(
        """SELECT *
             FROM reaction_universe_assets
            WHERE reaction_plan_id=?
              AND upper(asset_symbol)=upper(?)
              AND asset_role=?
              AND COALESCE(proxy_family,'')=COALESCE(?, '')
            LIMIT 1""",
        (int(reaction_plan_id), asset_symbol, asset_role, proxy_family),
    ).fetchone()


def store_event_reaction_label(
    conn: sqlite3.Connection,
    *,
    reaction_plan_id: int,
    asset_symbol: str,
    asset_role: str,
    proxy_family: str = "",
    window: str,
    decision_lag_seconds: int = 0,
    start_at_utc: str = "",
    end_at_utc: str = "",
    session_bucket: str = "unknown",
    raw_return: float | None = None,
    market_adjusted_return: float | None = None,
    sector_adjusted_return: float | None = None,
    proxy_spread_return: float | None = None,
    residual_return: float | None = None,
    return_zscore: float | None = None,
    volume_zscore: float | None = None,
    realized_volatility: float | None = None,
    mfe: float | None = None,
    mae: float | None = None,
    max_drawdown_in_window: float | None = None,
    spread_estimate: float | None = None,
    slippage_estimate: float | None = None,
    liquidity_score: float | None = None,
    label_quality_flag: str = "pending_market_data",
    label_quality_reason: str = "foundation_stub_reaction_plan_only",
    outcome_builder_version: str = REACTION_LABEL_OUTCOME_BUILDER_VERSION,
    market_data_source: str = "",
    calendar_version: str = CALENDAR_VERSION,
) -> int:
    ensure_reaction_research_schema(conn)
    planned = _planned_asset(conn, int(reaction_plan_id), asset_symbol, asset_role, proxy_family)
    if planned is None:
        raise ValueError(
            "reaction label rejected: asset is not present in reaction_universe_plan "
            f"(plan={reaction_plan_id}, symbol={asset_symbol}, role={asset_role}, proxy_family={proxy_family})"
        )
    event_cluster_id = int(planned["event_cluster_id"])
    cluster = conn.execute(
        "SELECT cluster_type FROM news_event_clusters WHERE event_cluster_id=?",
        (event_cluster_id,),
    ).fetchone()
    if cluster and str(cluster["cluster_type"] or "") in {"macro_release", "central_bank_event"}:
        canonical_macro = conn.execute(
            """SELECT intraday_label_eligible,release_time_quality
                 FROM v_canonical_reaction_trigger_macro_events
                WHERE event_cluster_id=?
                LIMIT 1""",
            (event_cluster_id,),
        ).fetchone()
        if canonical_macro is None:
            raise ValueError("reaction label rejected: inactive or non-canonical macro cluster")
        if str(window) in {"5m", "15m", "1h", "close"} and (
            int(canonical_macro["intraday_label_eligible"] or 0) != 1
            or str(canonical_macro["release_time_quality"] or "") not in {"official_exact", "provider_exact"}
        ):
            raise ValueError(
                "reaction label rejected: intraday macro label requires exact official/provider release time"
            )
    now = _utc_now_iso()
    conn.execute(
        """INSERT OR IGNORE INTO event_reaction_labels(
               event_cluster_id,reaction_plan_id,asset_symbol,asset_role,proxy_family,window,
               decision_lag_seconds,start_at_utc,end_at_utc,session_bucket,raw_return,
               market_adjusted_return,sector_adjusted_return,proxy_spread_return,residual_return,
               return_zscore,volume_zscore,realized_volatility,mfe,mae,max_drawdown_in_window,
               spread_estimate,slippage_estimate,liquidity_score,label_quality_flag,
               label_quality_reason,outcome_builder_version,market_data_source,calendar_version,created_at
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            event_cluster_id,
            int(reaction_plan_id),
            asset_symbol.upper(),
            asset_role,
            proxy_family,
            window,
            int(decision_lag_seconds),
            start_at_utc,
            end_at_utc,
            session_bucket,
            raw_return,
            market_adjusted_return,
            sector_adjusted_return,
            proxy_spread_return,
            residual_return,
            return_zscore,
            volume_zscore,
            realized_volatility,
            mfe,
            mae,
            max_drawdown_in_window,
            spread_estimate,
            slippage_estimate,
            liquidity_score,
            label_quality_flag,
            label_quality_reason,
            outcome_builder_version,
            market_data_source,
            calendar_version,
            now,
        ),
    )
    row = conn.execute(
        """SELECT reaction_label_id
             FROM event_reaction_labels
            WHERE reaction_plan_id=? AND upper(asset_symbol)=upper(?) AND asset_role=?
              AND COALESCE(proxy_family,'')=COALESCE(?, '')
              AND window=? AND decision_lag_seconds=?
            LIMIT 1""",
        (int(reaction_plan_id), asset_symbol, asset_role, proxy_family, window, int(decision_lag_seconds)),
    ).fetchone()
    return int(row["reaction_label_id"])


def generate_reaction_label_stubs(
    conn: sqlite3.Connection,
    reaction_plan_id: int,
    *,
    windows: Iterable[str] = ("5m", "15m", "1h", "close"),
    decision_lags_seconds: Iterable[int] = (0, 30, 60, 180),
) -> int:
    plan = conn.execute(
        "SELECT * FROM reaction_universe_plans WHERE reaction_plan_id=?",
        (int(reaction_plan_id),),
    ).fetchone()
    if plan is None:
        raise ValueError(f"reaction_plan_id={reaction_plan_id} does not exist")
    start_anchor = _parse_iso(str(plan["available_at_cutoff"] or ""))
    created = 0
    assets = conn.execute(
        """SELECT asset_symbol,asset_role,proxy_family
             FROM reaction_universe_assets
            WHERE reaction_plan_id=?
            ORDER BY label_scope,asset_role,asset_symbol""",
        (int(reaction_plan_id),),
    ).fetchall()
    before = int(conn.execute("SELECT COUNT(*) AS c FROM event_reaction_labels").fetchone()["c"])
    for asset in assets:
        for lag in decision_lags_seconds:
            lagged_start = start_anchor + timedelta(seconds=int(lag)) if start_anchor else None
            for window in windows:
                delta = _window_delta(window)
                end = lagged_start + delta if lagged_start and delta else None
                store_event_reaction_label(
                    conn,
                    reaction_plan_id=int(reaction_plan_id),
                    asset_symbol=str(asset["asset_symbol"]),
                    asset_role=str(asset["asset_role"]),
                    proxy_family=str(asset["proxy_family"] or ""),
                    window=str(window),
                    decision_lag_seconds=int(lag),
                    start_at_utc=lagged_start.isoformat() if lagged_start else "",
                    end_at_utc=end.isoformat() if end else "",
                    session_bucket=_session_bucket(lagged_start),
                )
    after = int(conn.execute("SELECT COUNT(*) AS c FROM event_reaction_labels").fetchone()["c"])
    created += max(0, after - before)
    return created


def _eligible_event_cluster_ids(
    conn: sqlite3.Connection,
    *,
    start: str | None,
    end: str | None,
    limit: int | None,
) -> list[int]:
    where: list[str] = [
        "c.reaction_trigger_eligible=1",
        "(c.cluster_type NOT IN ('macro_release','central_bank_event') "
        "OR EXISTS (SELECT 1 FROM v_canonical_reaction_trigger_macro_events v "
        "WHERE v.event_cluster_id=c.event_cluster_id))",
    ]
    params: list[Any] = []
    time_expr = "COALESCE(NULLIF(c.first_seen_at_utc,''),c.created_at)"
    if start:
        where.append(f"{time_expr} >= ?")
        params.append(start)
    if end:
        where.append(f"{time_expr} <= ?")
        params.append(end)
    sql = "SELECT c.event_cluster_id FROM news_event_clusters c"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY {time_expr} ASC,event_cluster_id ASC"
    if limit is not None and int(limit) > 0:
        sql += " LIMIT ?"
        params.append(int(limit))
    return [int(row["event_cluster_id"]) for row in conn.execute(sql, params).fetchall()]


def _eligible_windows_for_cluster(
    conn: sqlite3.Connection,
    event_cluster_id: int,
    windows: Iterable[str],
) -> tuple[str, ...]:
    requested = tuple(str(window) for window in windows)
    cluster = conn.execute(
        "SELECT cluster_type FROM news_event_clusters WHERE event_cluster_id=?",
        (int(event_cluster_id),),
    ).fetchone()
    if cluster is None or str(cluster["cluster_type"] or "") not in {"macro_release", "central_bank_event"}:
        return requested
    rows = conn.execute(
        """SELECT intraday_label_eligible,release_time_quality
             FROM v_canonical_reaction_trigger_macro_events
            WHERE event_cluster_id=?""",
        (int(event_cluster_id),),
    ).fetchall()
    if not rows:
        return ()
    exact = any(
        int(row["intraday_label_eligible"] or 0) == 1
        and str(row["release_time_quality"] or "") in {"official_exact", "provider_exact"}
        for row in rows
    )
    if exact:
        return requested
    return tuple(window for window in requested if window in {"1d", "5d", "next_session"})


def build_reaction_label_foundation(
    database_path: str | Path,
    *,
    start: str | None = None,
    end: str | None = None,
    limit: int | None = None,
    windows: Iterable[str] = ("5m", "15m", "1h", "close"),
    decision_lags_seconds: Iterable[int] = (0, 30, 60, 180),
    include_core: bool = True,
    include_event_proxies: bool = True,
    create_label_stubs: bool = True,
) -> dict[str, Any]:
    db_path = Path(database_path)
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        ensure_reaction_research_schema(conn)
        cluster_ids = _eligible_event_cluster_ids(conn, start=start, end=end, limit=limit)
        requested_windows = tuple(str(window) for window in windows)
        requested_lags = tuple(int(lag) for lag in decision_lags_seconds)
        plan_ids: list[int] = []
        suppressed_macro_window_count = 0
        labels_before = int(conn.execute("SELECT COUNT(*) AS c FROM event_reaction_labels").fetchone()["c"])
        assets_before = int(conn.execute("SELECT COUNT(*) AS c FROM reaction_universe_assets").fetchone()["c"])
        for event_cluster_id in cluster_ids:
            plan_id = create_reaction_universe_plan(
                conn,
                event_cluster_id,
                include_core=include_core,
                include_event_proxies=include_event_proxies,
            )
            plan_ids.append(plan_id)
            if create_label_stubs:
                eligible_windows = _eligible_windows_for_cluster(conn, event_cluster_id, requested_windows)
                suppressed_macro_window_count += max(0, len(requested_windows) - len(eligible_windows))
                generate_reaction_label_stubs(
                    conn,
                    plan_id,
                    windows=eligible_windows,
                    decision_lags_seconds=requested_lags,
                )
        labels_after = int(conn.execute("SELECT COUNT(*) AS c FROM event_reaction_labels").fetchone()["c"])
        assets_after = int(conn.execute("SELECT COUNT(*) AS c FROM reaction_universe_assets").fetchone()["c"])
        scope_rows = [
            dict(row)
            for row in conn.execute(
                """SELECT label_scope,asset_role,COUNT(*) AS count
                     FROM reaction_universe_assets
                    GROUP BY label_scope,asset_role
                    ORDER BY label_scope,asset_role"""
            ).fetchall()
        ]
        return {
            "version": REACTION_RESEARCH_ENGINE_VERSION,
            "database_path": str(db_path),
            "generated_at": _utc_now_iso(),
            "start": start,
            "end": end,
            "event_clusters_seen": len(cluster_ids),
            "reaction_plans_touched": len(set(plan_ids)),
            "reaction_assets_created": max(0, assets_after - assets_before),
            "reaction_labels_created": max(0, labels_after - labels_before),
            "suppressed_macro_window_count": suppressed_macro_window_count,
            "reaction_asset_scope_counts": scope_rows,
            "hard_rules": {
                "labels_from_plan_only": True,
                "core_event_as_label_scope": True,
                "fallback_spy_not_direct_target": True,
                "route_only_excluded_from_direct": True,
                "canonical_macro_view_only": True,
                "macro_intraday_exact_time_only": True,
            },
        }
    finally:
        conn.close()


def render_reaction_label_foundation_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# M253 Reaction Label Foundation",
        "",
        f"- Generated: {payload.get('generated_at')}",
        f"- Version: `{payload.get('version')}`",
        f"- Database: `{payload.get('database_path')}`",
        f"- Window: `{payload.get('start') or 'all'}` -> `{payload.get('end') or 'all'}`",
        f"- Event clusters seen: {payload.get('event_clusters_seen')}",
        f"- Reaction plans touched: {payload.get('reaction_plans_touched')}",
        f"- Reaction assets created: {payload.get('reaction_assets_created')}",
        f"- Reaction label stubs created: {payload.get('reaction_labels_created')}",
        "",
        "## Scope counts",
        "",
    ]
    counts = payload.get("reaction_asset_scope_counts") or []
    if counts:
        for item in counts:
            lines.append(f"- {item.get('label_scope')} / {item.get('asset_role')}: {item.get('count')}")
    else:
        lines.append("- No reaction assets yet.")
    lines.extend(
        [
            "",
            "## Hard rules",
            "",
            "- Reaction labels are generated only from `reaction_universe_plan` assets.",
            "- `core` and `event` are label scopes, not separate backfill stages.",
            "- `route_only` and `fallback_context` are excluded from direct-entity reaction labels.",
            "- This command creates foundation/stub labels only; no broker order, live routing, threshold promotion, or live trading action is used.",
        ]
    )
    return "\n".join(lines) + "\n"
