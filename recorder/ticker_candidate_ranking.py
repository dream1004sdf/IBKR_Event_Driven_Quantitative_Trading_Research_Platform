from __future__ import annotations

import json
import math
import sqlite3
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from recorder.news_asset_mapper import NewsAssetMapping, infer_news_assets


TICKER_CANDIDATE_RANKING_VERSION = "m253-cluster-ticker-candidate-ranking-v1"


_LIQUIDITY_PRIOR: dict[str, float] = {
    "SPY": 20.0,
    "QQQ": 20.0,
    "IWM": 18.0,
    "DIA": 17.0,
    "NVDA": 18.0,
    "AAPL": 17.0,
    "MSFT": 17.0,
    "AMZN": 16.0,
    "META": 16.0,
    "GOOGL": 16.0,
    "TSLA": 16.0,
    "AVGO": 15.0,
    "AMD": 15.0,
    "MU": 14.0,
    "TSM": 14.0,
    "SMH": 15.0,
    "SOXX": 15.0,
    "XLE": 14.0,
    "USO": 13.0,
    "XOM": 14.0,
    "CVX": 14.0,
    "GLD": 14.0,
    "TLT": 14.0,
    "UUP": 11.0,
    "VIX": 10.0,
    "VXX": 8.0,
    "ITA": 10.0,
    "XAR": 9.0,
    "EWY": 8.0,
}

_OPTIONABLE_PRIOR = {
    "SPY",
    "QQQ",
    "IWM",
    "DIA",
    "NVDA",
    "AAPL",
    "MSFT",
    "AMZN",
    "META",
    "GOOGL",
    "TSLA",
    "AVGO",
    "AMD",
    "MU",
    "TSM",
    "SMH",
    "SOXX",
    "XLE",
    "USO",
    "XOM",
    "CVX",
    "GLD",
    "TLT",
    "ITA",
    "XAR",
}


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _result(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    result = payload.get("result")
    return result if isinstance(result, dict) else payload


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, dict):
        for key in ("numeric_value", "value", "amount", "score"):
            parsed = _as_float(value.get(key))
            if parsed is not None:
                return parsed
        return None
    try:
        parsed = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _parse_dt(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    raw = str(value).strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                parsed = datetime.strptime(raw, fmt)
                break
            except ValueError:
                parsed = None  # type: ignore[assignment]
        if parsed is None:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _event_time(row: dict[str, Any]) -> datetime | None:
    best: datetime | None = None
    for key in ("provider_published_at", "published_at", "received_at", "created_at"):
        parsed = _parse_dt(row.get(key))
        if parsed and (best is None or parsed > best):
            best = parsed
    return best


def _recency_factor(event_at: datetime | None, now: datetime, half_life_hours: float) -> float:
    if event_at is None:
        return 0.35
    age_hours = max(0.0, (now - event_at).total_seconds() / 3600.0)
    half_life = max(0.25, float(half_life_hours))
    return 1.0 / (1.0 + (age_hours / half_life))


def _confidence_factor(confidence: str) -> float:
    return {
        "high": 1.0,
        "medium": 0.72,
        "low": 0.42,
        "unknown": 0.35,
    }.get(str(confidence or "unknown").lower(), 0.35)


def _provider_key(provider: str) -> str:
    raw = str(provider or "").strip().lower()
    if raw in {"bz", "benzinga", "broadtape:bz"} or "benzinga" in raw:
        return "benzinga"
    if raw.startswith("dj") or "dow" in raw or "barron" in raw:
        return "dow_jones"
    if "marketaux" in raw:
        return "marketaux"
    return raw or "unknown"


def _provider_weight(provider: str, news_learning: dict[str, Any]) -> float:
    profile = news_learning.get("provider_profile") if isinstance(news_learning.get("provider_profile"), dict) else {}
    item = profile.get(_provider_key(provider)) if isinstance(profile, dict) else {}
    learned = item.get("learned_weight") if isinstance(item, dict) and isinstance(item.get("learned_weight"), dict) else {}
    parsed = _as_float(learned.get("overall"))
    if parsed is not None:
        return max(0.10, min(1.25, parsed))
    return {
        "benzinga": 0.86,
        "dow_jones": 0.88,
        "marketaux": 0.36,
        "unknown": 0.50,
    }.get(_provider_key(provider), 0.50)


def _safe_json(value: Any) -> Any:
    if not value:
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except Exception:
        return None


def _assets_from_payload(value: Any) -> tuple[str, ...]:
    payload = _safe_json(value)
    symbols: list[str] = []

    def add(item: Any) -> None:
        if isinstance(item, str):
            symbols.append(item)
        elif isinstance(item, dict):
            for key in ("symbol", "ticker", "asset", "key"):
                if item.get(key):
                    symbols.append(str(item.get(key)))
                    break

    if isinstance(payload, list):
        for item in payload:
            add(item)
    elif isinstance(payload, dict):
        for key in ("assets", "symbols", "target_assets", "target_candidates"):
            values = payload.get(key)
            if isinstance(values, list):
                for item in values:
                    add(item)
    return tuple(dict.fromkeys(str(item or "").upper().strip() for item in symbols if str(item or "").strip()))


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?",
        (table_name,),
    ).fetchone() is not None


def _candidate_confidence_label(value: Any) -> str:
    parsed = _as_float(value)
    if parsed is None:
        return "unknown"
    if parsed >= 0.75:
        return "high"
    if parsed >= 0.50:
        return "medium"
    if parsed > 0:
        return "low"
    return "unknown"


def _safe_json_list(value: Any) -> list[Any]:
    payload = _safe_json(value)
    return payload if isinstance(payload, list) else []


def _read_cluster_events(database_path: Path, *, since: datetime, limit: int) -> list[dict[str, Any]]:
    if not database_path.exists():
        return []
    con = sqlite3.connect(database_path)
    con.row_factory = sqlite3.Row
    try:
        required = {
            "news_event_clusters",
            "news_event_cluster_roles",
            "news_asset_candidates",
        }
        if not all(_table_exists(con, table) for table in required):
            return []
        rows = con.execute(
            """
            SELECT c.event_cluster_id,
                   c.primary_headline AS headline,
                   c.first_seen_at_utc AS published_at,
                   c.last_seen_at_utc AS received_at,
                   c.event_class,
                   c.canonical_entities_json,
                   r.event_role,
                   r.role_family,
                   r.direction,
                   r.role_confidence,
                   r.provider_agreement,
                   r.provider_confirmation_score,
                   r.provider_conflict_score,
                   r.marketaux_noise_score,
                   r.provider_count,
                   r.article_count,
                   r.source_news_count,
                   r.external_signal_count,
                   r.provider_families_json,
                   r.direction_counts_json,
                   r.shared_assets_json,
                   r.source_news_ids_json,
                   r.external_signal_ids_json,
                   r.double_count_suppressed_signal_ids_json
              FROM news_event_clusters c
              JOIN news_event_cluster_roles r ON r.event_cluster_id=c.event_cluster_id
             WHERE COALESCE(NULLIF(c.last_seen_at_utc,''),c.first_seen_at_utc) >= ?
             ORDER BY COALESCE(NULLIF(c.last_seen_at_utc,''),c.first_seen_at_utc) DESC,
                      c.event_cluster_id DESC
             LIMIT ?
            """,
            (since.isoformat(), max(1, int(limit))),
        ).fetchall()
        events: list[dict[str, Any]] = []
        for row in rows:
            cluster_id = int(row["event_cluster_id"])
            if _table_exists(con, "v_cluster_asset_candidate_aggregates"):
                candidate_rows = con.execute(
                    """
                    SELECT symbol,con_id,asset_type,role,source,confidence,
                           '' AS evidence,resolver_status,tradable_candidate,
                           supporting_news_count,raw_candidate_rows
                      FROM v_cluster_asset_candidate_aggregates
                     WHERE event_cluster_id=?
                       AND symbol IS NOT NULL
                       AND symbol <> ''
                       AND role IN ('primary','secondary','proxy')
                       AND source NOT IN ('fallback_context','ibkr_route')
                       AND (
                             (role IN ('primary','secondary')
                              AND resolver_status='resolved'
                              AND tradable_candidate=1
                              AND confidence>=0.50)
                          OR (role='proxy'
                              AND resolver_status IN ('proxy_only','resolved')
                              AND tradable_candidate=1
                              AND confidence>=0.35)
                       )
                     ORDER BY symbol,confidence DESC,supporting_news_count DESC,raw_candidate_rows DESC
                    """,
                    (cluster_id,),
                ).fetchall()
            else:
                candidate_rows = con.execute(
                    """
                    SELECT symbol,con_id,asset_type,role,source,confidence,evidence,
                           resolver_status,tradable_candidate,
                           1 AS supporting_news_count,1 AS raw_candidate_rows
                      FROM news_asset_candidates
                     WHERE event_cluster_id=?
                       AND symbol IS NOT NULL
                       AND symbol <> ''
                       AND role IN ('primary','secondary','proxy')
                       AND source NOT IN ('fallback_context','ibkr_route')
                       AND (
                             (role IN ('primary','secondary')
                              AND resolver_status='resolved'
                              AND tradable_candidate=1
                              AND confidence>=0.50)
                          OR (role='proxy'
                              AND resolver_status IN ('proxy_only','resolved')
                              AND tradable_candidate=1
                              AND confidence>=0.35)
                       )
                     ORDER BY symbol,confidence DESC,id
                    """,
                    (cluster_id,),
                ).fetchall()
            by_symbol: dict[str, dict[str, Any]] = {}
            for candidate in candidate_rows:
                symbol = str(candidate["symbol"] or "").upper().strip()
                if not symbol or ":" in symbol:
                    continue
                existing = by_symbol.get(symbol)
                if existing is not None:
                    existing_confidence = float(existing.get("confidence") or 0.0)
                    candidate_confidence = float(candidate["confidence"] or 0.0)
                    existing_support = int(existing.get("supporting_news_count") or 0)
                    candidate_support = int(candidate["supporting_news_count"] or 0)
                    if (existing_confidence, existing_support) >= (candidate_confidence, candidate_support):
                        continue
                by_symbol[symbol] = dict(candidate)
            if not by_symbol:
                continue
            item = dict(row)
            item["id"] = cluster_id
            item["source_table"] = "news_event_clusters"
            item["title"] = item.get("headline")
            item["provider"] = ",".join(str(x) for x in _safe_json_list(item.get("provider_families_json"))) or "cluster"
            item["asset_candidates"] = list(by_symbol.values())
            events.append(item)
        return events
    except sqlite3.Error:
        return []
    finally:
        con.close()


def _read_news_events(database_path: Path, *, since: datetime, limit: int) -> list[dict[str, Any]]:
    if not database_path.exists():
        return []
    rows: list[dict[str, Any]] = []
    con = sqlite3.connect(database_path)
    con.row_factory = sqlite3.Row
    try:
        for row in con.execute(
            """
            SELECT id, provider, headline, primary_symbol, extra_data,
                   provider_published_at, published_at, received_at, created_at
              FROM news_articles
             WHERE COALESCE(provider_published_at, published_at, received_at, created_at) >= ?
             ORDER BY COALESCE(provider_published_at, published_at, received_at, created_at) DESC
             LIMIT ?
            """,
            (since.isoformat(), max(1, int(limit))),
        ):
            item = dict(row)
            item["source_table"] = "news_articles"
            item["title"] = item.get("headline")
            rows.append(item)
        for row in con.execute(
            """
            SELECT id, source AS provider, title, region, theme, sentiment_label,
                   assets_json, published_at, created_at
              FROM external_market_signals
             WHERE COALESCE(published_at, created_at) >= ?
             ORDER BY COALESCE(published_at, created_at) DESC
             LIMIT ?
            """,
            (since.isoformat(), max(1, int(limit))),
        ):
            item = dict(row)
            item["source_table"] = "external_market_signals"
            item["headline"] = item.get("title")
            rows.append(item)
    except sqlite3.Error:
        return rows
    finally:
        con.close()
    rows.sort(key=lambda item: _event_time(item) or datetime.min.replace(tzinfo=UTC), reverse=True)
    return rows[: max(1, int(limit))]


def _market_rows_by_symbol(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    market = _result(payload)
    rows = market.get("rows") if isinstance(market.get("rows"), list) else []
    mapped: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = str(row.get("key") or row.get("symbol") or "").upper()
        if key:
            mapped[key] = row
    return mapped


def _option_rows_by_symbol(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    options = _result(payload)
    rows = options.get("symbols") if isinstance(options.get("symbols"), list) else []
    return {str(row.get("symbol") or "").upper(): row for row in rows if isinstance(row, dict) and row.get("symbol")}


def _market_score(symbol: str, market_rows: dict[str, dict[str, Any]]) -> tuple[float, list[str], dict[str, Any]]:
    row = market_rows.get(symbol.upper()) or {}
    if not row:
        return 0.0, ["no_direct_market_data_row"], {}
    status = str(row.get("status") or "").lower()
    score = 0.0
    reasons: list[str] = []
    if status == "live":
        score += 6.0
        reasons.append("live_market_data")
    elif status == "delayed":
        score += 2.0
        reasons.append("delayed_market_data")
    elif status:
        score -= 4.0
        reasons.append(f"market_data_status_{status}")
    if row.get("delayed"):
        score -= 1.0
        reasons.append("delayed_flag")
    if row.get("alerts"):
        score += min(3.0, float(len(row.get("alerts") or [])))
        reasons.append("active_market_alerts")
    return score, reasons, {
        "status": row.get("status"),
        "price": row.get("price"),
        "delayed": row.get("delayed"),
        "alerts": row.get("alerts") or [],
        "group": row.get("group"),
    }


def _option_score(symbol: str, option_rows: dict[str, dict[str, Any]]) -> tuple[float, list[str], dict[str, Any]]:
    row = option_rows.get(symbol.upper()) or {}
    score = 0.0
    reasons: list[str] = []
    if row:
        confidence = str(row.get("confidence") or "").lower()
        score += {"high": 11.0, "medium": 7.0, "low": 3.0}.get(confidence, 2.0)
        quote_count = _as_float(row.get("quote_count")) or 0.0
        score += min(8.0, quote_count / 20.0)
        reasons.append(f"option_structure_{confidence or 'available'}")
        gamma = str(row.get("gamma_regime") or "")
        if gamma == "negative_gamma_trend_amplification":
            score += 3.0
            reasons.append("negative_gamma_can_amplify_move")
        elif gamma == "positive_gamma_mean_reversion_bias":
            score += 1.0
            reasons.append("positive_gamma_mean_reversion_context")
        if row.get("pin_risk"):
            score -= 1.5
            reasons.append("pin_risk_near_wall_or_max_pain")
        return score, reasons, {
            "available": True,
            "confidence": row.get("confidence"),
            "quote_count": row.get("quote_count"),
            "gamma_regime": row.get("gamma_regime"),
            "pin_risk": row.get("pin_risk"),
            "call_wall": row.get("call_wall"),
            "put_wall": row.get("put_wall"),
            "max_pain": row.get("max_pain"),
        }
    if symbol.upper() in _OPTIONABLE_PRIOR:
        return 4.0, ["optionable_prior_no_fresh_structure"], {"available": False, "optionable_prior": True}
    return 0.0, ["no_option_structure"], {"available": False}


def _capital_score(capital_payload: dict[str, Any]) -> tuple[float, list[str], dict[str, Any]]:
    capital = _result(capital_payload)
    status = capital.get("status")
    budgets = capital.get("budgets") if isinstance(capital.get("budgets"), dict) else {}
    score = 0.0
    reasons: list[str] = []
    if status == "green":
        score += 5.0
        reasons.append("capital_state_green")
    elif status == "yellow":
        score += 1.0
        reasons.append("capital_state_yellow")
    elif status:
        score -= 8.0
        reasons.append(f"capital_state_{status}")
    options_budget = _as_float(budgets.get("options_premium_budget")) or 0.0
    if options_budget >= 150.0:
        score += 3.0
        reasons.append("spread_budget_available")
    elif options_budget >= 25.0:
        score += 1.0
        reasons.append("small_option_budget_available")
    return score, reasons, {
        "status": status,
        "leverage_mode": capital.get("leverage_mode"),
        "options_premium_budget": options_budget,
        "max_single_position_notional": budgets.get("max_single_position_notional"),
    }


def _mapping_for_event(event: dict[str, Any]) -> NewsAssetMapping:
    headline = str(event.get("headline") or event.get("title") or "")
    primary_symbol = str(event.get("primary_symbol") or "")
    extra_data = event.get("extra_data")
    if not extra_data and event.get("assets_json"):
        extra_data = {"asset_mapping": {"target_candidates": list(_assets_from_payload(event.get("assets_json"))), "confidence": "medium", "reason": "external signal assets_json", "source_mode": "external_signal"}}
    return infer_news_assets(
        headline,
        primary_symbol=primary_symbol,
        provider=str(event.get("provider") or ""),
        extra_data=extra_data,
    )


def build_ticker_candidate_ranking_report(
    *,
    database_path: Path,
    market_data_payload: dict[str, Any] | None = None,
    options_market_structure_payload: dict[str, Any] | None = None,
    options_market_structure_supervisor_payload: dict[str, Any] | None = None,
    capital_state_payload: dict[str, Any] | None = None,
    news_learning_payload: dict[str, Any] | None = None,
    lookback_hours: float = 24.0,
    recency_half_life_hours: float = 6.0,
    max_news_events: int = 250,
    max_ranked_candidates: int = 50,
    min_total_score: float = 0.0,
    now: str | None = None,
) -> dict[str, Any]:
    now_dt = _parse_dt(now) or datetime.now(UTC)
    since = now_dt - timedelta(hours=max(0.25, float(lookback_hours)))
    news_learning = _result(news_learning_payload)
    market_rows = _market_rows_by_symbol(market_data_payload or {})
    option_rows = _option_rows_by_symbol(options_market_structure_payload or {})
    capital_score, capital_reasons, capital_context = _capital_score(capital_state_payload or {})
    resolved_database_path = database_path.expanduser().resolve()
    events = _read_cluster_events(resolved_database_path, since=since, limit=max_news_events)
    input_mode = "lineage_v2_cluster"
    if not events:
        events = _read_news_events(resolved_database_path, since=since, limit=max_news_events)
        input_mode = "legacy_article_signal"

    buckets: dict[str, dict[str, Any]] = {}
    for event in events:
        event_at = _event_time(event)
        recency = _recency_factor(event_at, now_dt, recency_half_life_hours)
        if event.get("source_table") == "news_event_clusters":
            provider_families = [
                str(item or "")
                for item in _safe_json_list(event.get("provider_families_json"))
                if str(item or "").strip()
            ] or ["unknown"]
            provider = ",".join(provider_families)
            provider_confirmation = max(0.0, min(1.0, _as_float(event.get("provider_confirmation_score")) or 0.0))
            provider_conflict = max(0.0, min(1.0, _as_float(event.get("provider_conflict_score")) or 0.0))
            role_confidence = max(0.10, min(1.0, _as_float(event.get("role_confidence")) or 0.35))
            provider_weight = max(0.35, min(1.15, 0.55 + provider_confirmation * 0.60 - provider_conflict * 0.30))
            cluster_quality = role_confidence
            base_event_score = 42.0 * provider_weight * cluster_quality * recency
            target_candidates = tuple(event.get("asset_candidates") or ())
            for candidate in target_candidates:
                if not isinstance(candidate, dict):
                    continue
                sym = str(candidate.get("symbol") or "").upper().strip()
                if not sym:
                    continue
                role = str(candidate.get("role") or "").lower()
                mapping_confidence = max(0.0, min(1.0, _as_float(candidate.get("confidence")) or 0.0))
                bucket = buckets.setdefault(
                    sym,
                    {
                        "symbol": sym,
                        "event_score": 0.0,
                        "event_count": 0,
                        "provider_counts": Counter(),
                        "theme_counts": Counter(),
                        "mapping_confidence_counts": Counter(),
                        "direct_symbol_hit_count": 0,
                        "inferred_symbol_hit_count": 0,
                        "cluster_confirmation_score": 0.0,
                        "provider_agreements": Counter(),
                        "event_contributions": [],
                    },
                )
                symbol_bonus = 0.0
                relation_reasons: list[str] = []
                if role in {"primary", "secondary"}:
                    symbol_bonus += (15.0 if role == "primary" else 9.0) * recency
                    bucket["direct_symbol_hit_count"] += 1
                    relation_reasons.append(f"lineage_{role}_content_entity")
                elif role == "proxy":
                    symbol_bonus += 5.0 * recency
                    bucket["inferred_symbol_hit_count"] += 1
                    relation_reasons.append("lineage_event_proxy")
                contribution = (base_event_score * max(0.30, mapping_confidence)) + symbol_bonus
                bucket["event_score"] += contribution
                bucket["event_count"] += 1
                for family in provider_families:
                    bucket["provider_counts"][family or "unknown"] += 1
                if event.get("role_family"):
                    bucket["theme_counts"][str(event.get("role_family"))] += 1
                if event.get("event_role"):
                    bucket["theme_counts"][str(event.get("event_role"))] += 1
                bucket["mapping_confidence_counts"][_candidate_confidence_label(mapping_confidence)] += 1
                bucket["cluster_confirmation_score"] += max(0.0, min(8.0, provider_confirmation * 8.0))
                bucket["provider_agreements"][str(event.get("provider_agreement") or "unknown")] += 1
                bucket["event_contributions"].append(
                    {
                        "source_table": event.get("source_table"),
                        "source_id": event.get("event_cluster_id") or event.get("id"),
                        "provider": provider,
                        "headline": event.get("headline") or event.get("title"),
                        "event_role": event.get("event_role"),
                        "role_family": event.get("role_family"),
                        "direction": event.get("direction"),
                        "provider_agreement": event.get("provider_agreement"),
                        "event_at": event_at.isoformat() if event_at else None,
                        "recency_factor": round(recency, 4),
                        "provider_weight": round(provider_weight, 4),
                        "mapping_confidence": _candidate_confidence_label(mapping_confidence),
                        "mapping_reason": candidate.get("evidence"),
                        "themes": [
                            value
                            for value in (event.get("role_family"), event.get("event_role"))
                            if value
                        ],
                        "score": round(contribution, 4),
                        "relation_reasons": relation_reasons,
                    }
                )
            continue

        mapping = _mapping_for_event(event)
        provider = str(event.get("provider") or "")
        provider_weight = _provider_weight(provider, news_learning)
        confidence_factor = _confidence_factor(mapping.confidence)
        base_event_score = 42.0 * provider_weight * confidence_factor * recency
        primary = str(event.get("primary_symbol") or "").upper()
        target_candidates = tuple(mapping.target_candidates or ())
        for symbol in target_candidates:
            sym = str(symbol or "").upper().strip()
            if not sym:
                continue
            bucket = buckets.setdefault(
                sym,
                {
                    "symbol": sym,
                    "event_score": 0.0,
                    "event_count": 0,
                    "provider_counts": Counter(),
                    "theme_counts": Counter(),
                    "mapping_confidence_counts": Counter(),
                    "direct_symbol_hit_count": 0,
                    "inferred_symbol_hit_count": 0,
                    "event_contributions": [],
                },
            )
            symbol_bonus = 0.0
            relation_reasons: list[str] = []
            if primary and primary == sym:
                symbol_bonus += 18.0 * recency
                bucket["direct_symbol_hit_count"] += 1
                relation_reasons.append("primary_symbol_direct")
            if sym in mapping.inferred_symbols:
                symbol_bonus += 10.0 * recency
                bucket["inferred_symbol_hit_count"] += 1
                relation_reasons.append("headline_symbol_or_alias")
            if sym in mapping.inferred_assets and sym not in mapping.inferred_symbols:
                symbol_bonus += 4.0 * recency
                relation_reasons.append("theme_asset_proxy")
            contribution = base_event_score + symbol_bonus
            bucket["event_score"] += contribution
            bucket["event_count"] += 1
            bucket["provider_counts"][provider or "unknown"] += 1
            for theme in mapping.themes:
                bucket["theme_counts"][theme] += 1
            bucket["mapping_confidence_counts"][mapping.confidence or "unknown"] += 1
            bucket["event_contributions"].append(
                {
                    "source_table": event.get("source_table"),
                    "source_id": event.get("id"),
                    "provider": provider,
                    "headline": event.get("headline") or event.get("title"),
                    "sentiment_label": event.get("sentiment_label"),
                    "region": event.get("region"),
                    "theme": event.get("theme"),
                    "event_at": event_at.isoformat() if event_at else None,
                    "recency_factor": round(recency, 4),
                    "provider_weight": round(provider_weight, 4),
                    "mapping_confidence": mapping.confidence,
                    "mapping_reason": mapping.reason,
                    "themes": list(mapping.themes),
                    "score": round(contribution, 4),
                    "relation_reasons": relation_reasons,
                }
            )

    ranked: list[dict[str, Any]] = []
    for symbol, bucket in buckets.items():
        market_score, market_reasons, market_context = _market_score(symbol, market_rows)
        option_score, option_reasons, option_context = _option_score(symbol, option_rows)
        liquidity_score = _LIQUIDITY_PRIOR.get(symbol, 6.0)
        provider_confirmation_score = (
            min(8.0, float(bucket["cluster_confirmation_score"]))
            if "cluster_confirmation_score" in bucket
            else min(8.0, 2.5 * len(bucket["provider_counts"]))
        )
        total_score = (
            float(bucket["event_score"])
            + liquidity_score
            + option_score
            + market_score
            + capital_score
            + provider_confirmation_score
        )
        if total_score < float(min_total_score):
            continue
        confidence = "high" if total_score >= 75 and bucket["direct_symbol_hit_count"] else "medium" if total_score >= 45 else "low"
        cautions: list[str] = []
        if bucket["mapping_confidence_counts"].get("low"):
            cautions.append("some_low_confidence_ticker_inference")
        if option_context.get("pin_risk"):
            cautions.append("option_pin_risk")
        if market_context.get("delayed"):
            cautions.append("market_data_delayed")
        if not market_context:
            cautions.append("no_direct_market_data_row")
        if bucket.get("provider_agreements") and bucket["provider_agreements"].get("conflict"):
            cautions.append("cluster_provider_conflict")
        if bucket.get("provider_agreements") and bucket["provider_agreements"].get("marketaux_noise_filtered"):
            cautions.append("marketaux_noise_filtered_cluster")
        ranked.append(
            {
                "symbol": symbol,
                "total_score": round(total_score, 4),
                "confidence": confidence,
                "score_components": {
                    "event_score": round(float(bucket["event_score"]), 4),
                    "liquidity_score": round(liquidity_score, 4),
                    "option_structure_score": round(option_score, 4),
                    "market_data_score": round(market_score, 4),
                    "capital_score": round(capital_score, 4),
                    "provider_confirmation_score": round(provider_confirmation_score, 4),
                },
                "event_count": int(bucket["event_count"]),
                "providers": dict(bucket["provider_counts"]),
                "themes": dict(bucket["theme_counts"]),
                "mapping_confidence_counts": dict(bucket["mapping_confidence_counts"]),
                "direct_symbol_hit_count": int(bucket["direct_symbol_hit_count"]),
                "inferred_symbol_hit_count": int(bucket["inferred_symbol_hit_count"]),
                "market_data": market_context,
                "option_structure": option_context,
                "ranking_reasons": list(dict.fromkeys(
                    [
                        *(market_reasons if market_reasons != ["no_direct_market_data_row"] else []),
                        *option_reasons,
                        *capital_reasons,
                        (
                            "lineage_v2_cluster_confirmed"
                            if bucket.get("provider_agreements") and bucket["provider_agreements"].get("confirmed")
                            else "multi_provider_confirmation"
                            if len(bucket["provider_counts"]) > 1
                            else "single_provider_or_theme"
                        ),
                    ]
                )),
                "cautions": cautions,
                "recent_events": sorted(bucket["event_contributions"], key=lambda item: item.get("event_at") or "", reverse=True)[:5],
            }
        )

    ranked.sort(
        key=lambda item: (
            float(item.get("total_score") or 0.0),
            int(item.get("direct_symbol_hit_count") or 0),
            int(item.get("event_count") or 0),
        ),
        reverse=True,
    )
    for idx, item in enumerate(ranked[: max(0, int(max_ranked_candidates))], start=1):
        item["rank"] = idx
    ranked = ranked[: max(0, int(max_ranked_candidates))]
    review_queue = [
        {
            "symbol": item.get("symbol"),
            "rank": item.get("rank"),
            "reason": ",".join(item.get("cautions") or []),
            "score": item.get("total_score"),
        }
        for item in ranked
        if item.get("cautions")
    ][:20]
    options_supervisor = _result(options_market_structure_supervisor_payload or {})
    return {
        "ticker_candidate_ranking_version": TICKER_CANDIDATE_RANKING_VERSION,
        "milestone": "M241.5",
        "generated_at": _iso_now(),
        "input_window": {
            "lookback_hours": float(lookback_hours),
            "since": since.isoformat(),
            "max_news_events": int(max_news_events),
            "source_event_count": len(events),
            "input_mode": input_mode,
        },
        "summary": {
            "ranked_candidate_count": len(ranked),
            "top_symbols": [item.get("symbol") for item in ranked[:10]],
            "review_queue_count": len(review_queue),
            "options_supervisor_status": options_supervisor.get("final_status"),
            "options_quote_usable": options_supervisor.get("final_quote_usable"),
            "capital_status": capital_context.get("status"),
        },
        "ranking": ranked,
        "review_queue": review_queue,
        "capital_context": capital_context,
        "policy": {
            "role": "rank ticker candidates before M241 strategy expression selection",
            "m253_lineage_cutover": "uses event clusters and stored asset candidates when M251/M252 lineage is available; falls back to legacy article/signal input only for older DBs",
            "does_not_submit_orders": True,
            "does_not_enable_transmit": True,
            "does_not_mutate_live_thresholds": True,
            "m241_consumption": "M241 may attach rank/context to existing action candidates; M241.5 does not synthesize broker orders.",
        },
        "safety": {
            "read_only": True,
            "paper_recommendation_only": True,
            "ibkr_order_submitted": False,
            "transmit_enabled": False,
            "live_routing_enabled": False,
        },
    }


def ticker_candidate_ranking_markdown(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    lines = [
        "# M241.5 Ticker Candidate Ranking",
        "",
        f"- Version: `{report.get('ticker_candidate_ranking_version')}`",
        f"- Generated: `{report.get('generated_at')}`",
        f"- Source events: `{(report.get('input_window') or {}).get('source_event_count')}`",
        f"- Ranked candidates: `{summary.get('ranked_candidate_count')}`",
        f"- Top symbols: `{summary.get('top_symbols') or []}`",
        f"- Options supervisor: `{summary.get('options_supervisor_status')}` quote usable `{summary.get('options_quote_usable')}`",
        "- Safety: read-only ranking; no broker order submission, no transmit, no live routing, no threshold mutation.",
        "",
        "| Rank | Symbol | Score | Confidence | Events | Providers | Reasons | Cautions |",
        "| ---: | --- | ---: | --- | ---: | --- | --- | --- |",
    ]
    for item in report.get("ranking") or []:
        providers = ",".join((item.get("providers") or {}).keys())
        reasons = ", ".join(item.get("ranking_reasons") or [])[:180].replace("|", "/")
        cautions = ", ".join(item.get("cautions") or [])[:120].replace("|", "/")
        lines.append(
            f"| {item.get('rank')} | `{item.get('symbol')}` | {item.get('total_score')} | `{item.get('confidence')}` | "
            f"{item.get('event_count')} | {providers or '-'} | {reasons or '-'} | {cautions or '-'} |"
        )
    if not report.get("ranking"):
        lines.append("| - | - | - | - | - | - | No ranked ticker candidates. | - |")
    if report.get("review_queue"):
        lines.extend(["", "## Review queue", ""])
        for row in report.get("review_queue") or []:
            lines.append(f"- `{row.get('symbol')}` rank `{row.get('rank')}` score `{row.get('score')}` reason `{row.get('reason')}`")
    return "\n".join(lines) + "\n"


def write_ticker_candidate_ranking_json(path: Path, report: dict[str, Any]) -> Path:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return resolved


def write_ticker_candidate_ranking_markdown(path: Path, report: dict[str, Any]) -> Path:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(ticker_candidate_ranking_markdown(report), encoding="utf-8")
    return resolved
