"""Source desk context, live validation, ingest health, and online supervisor operations.

Middle-level module for source/evidence online-state control.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Callable

from ibapi.client import EClient
from ibapi.contract import Contract
from ibapi.wrapper import EWrapper

from .economic_indicator_desk import build_economic_indicator_desk, compact_economic_indicator_desk
from .evidence_operations import parse_datetime
from .external_market_sources import (
    AlphaVantageNewsClient,
    MarketauxClient,
    _normalize_alpha_time,
    _redact_error,
)
from .ibkr_historical_news import format_ibkr_news_datetime, parse_ibkr_news_time
from .macro_universe import FRED_SOURCE_DESK_CONTEXT_SERIES

CommandRunner = Callable[[list[str], Path, float], dict[str, Any]]



# --- merged from source_desk_context.py ---
UTC = timezone.utc

SOURCE_DESK_CONTEXT_VERSION = "source-desk-context-v2"

DEFAULT_FRED_CONTEXT_SERIES = FRED_SOURCE_DESK_CONTEXT_SERIES

FINANCIAL_CONTEXT_TAG_PRIORITY = (
    "Revenues",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "NetIncomeLoss",
    "EarningsPerShareDiluted",
    "OperatingIncomeLoss",
    "NetCashProvidedByUsedInOperatingActivities",
    "PaymentsToAcquirePropertyPlantAndEquipment",
    "Assets",
    "Liabilities",
    "StockholdersEquity",
)


def _parse_event_time(value: Any) -> datetime:
    text = str(value or "").strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return datetime.now(UTC)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _finite_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed != parsed or parsed in {float("inf"), float("-inf")}:
        return None
    return parsed


def _compact_quote(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": str(row.get("symbol") or "").upper(),
        "captured_at": str(row.get("captured_at") or ""),
        "selected_price": _finite_float(row.get("selected_price")),
        "selected_price_source": str(row.get("selected_price_source") or ""),
        "bid": _finite_float(row.get("bid")),
        "ask": _finite_float(row.get("ask")),
        "last": _finite_float(row.get("last")),
        "midpoint": _finite_float(row.get("midpoint")),
        "volume": _finite_float(row.get("volume")),
        "currency": str(row.get("currency") or ""),
        "sec_type": str(row.get("sec_type") or ""),
        "exchange": str(row.get("exchange") or ""),
        "market_data_type": row.get("market_data_type"),
        "quote_integrity_status": str(row.get("quote_integrity_status") or "not_checked"),
        "reaction_baseline_eligible": bool(row.get("reaction_baseline_eligible")),
        "spread_ratio": _finite_float(row.get("spread_ratio")),
    }


def _latest_financial_facts(db: Any, *, symbol: str, cutoff_date: str, limit: int = 10) -> list[dict[str, Any]]:
    if not symbol:
        return []
    placeholders = ",".join("?" for _ in FINANCIAL_CONTEXT_TAG_PRIORITY)
    params: list[Any] = [symbol.upper(), cutoff_date, *FINANCIAL_CONTEXT_TAG_PRIORITY, max(1, int(limit))]
    with db.connect() as conn:
        rows = conn.execute(
            f"""
            SELECT symbol,tag,label,unit,fiscal_year,fiscal_period,form,filed_at,
                   start_date,end_date,value,accession_number,ingestion_version
            FROM financial_statement_facts
            WHERE symbol=?
              AND filed_at!=''
              AND substr(filed_at,1,10)<=?
              AND tag IN ({placeholders})
            ORDER BY filed_at DESC,end_date DESC,
                     CASE tag
                       WHEN 'Revenues' THEN 1
                       WHEN 'RevenueFromContractWithCustomerExcludingAssessedTax' THEN 2
                       WHEN 'NetIncomeLoss' THEN 3
                       WHEN 'EarningsPerShareDiluted' THEN 4
                       WHEN 'OperatingIncomeLoss' THEN 5
                       WHEN 'NetCashProvidedByUsedInOperatingActivities' THEN 6
                       WHEN 'PaymentsToAcquirePropertyPlantAndEquipment' THEN 7
                       WHEN 'Assets' THEN 8
                       WHEN 'Liabilities' THEN 9
                       WHEN 'StockholdersEquity' THEN 10
                       ELSE 99
                     END,
                     id DESC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
    return [
        {
            "symbol": str(row["symbol"]),
            "tag": str(row["tag"]),
            "label": str(row["label"] or ""),
            "unit": str(row["unit"] or ""),
            "fiscal_year": row["fiscal_year"],
            "fiscal_period": str(row["fiscal_period"] or ""),
            "form": str(row["form"] or ""),
            "filed_at": str(row["filed_at"] or ""),
            "period_end": str(row["end_date"] or ""),
            "value": _finite_float(row["value"]),
            "accession_number": str(row["accession_number"] or ""),
            "ingestion_version": str(row["ingestion_version"] or ""),
        }
        for row in rows
    ]


def _latest_fred_observations(db: Any, *, cutoff_date: str, limit_per_series: int = 1) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    with db.connect() as conn:
        for series_id in DEFAULT_FRED_CONTEXT_SERIES:
            rows = conn.execute(
                """
                SELECT series_id,observation_date,value,realtime_start,realtime_end,ingestion_version
                FROM fred_observations
                WHERE series_id=? AND observation_date<=?
                ORDER BY observation_date DESC,id DESC
                LIMIT ?
                """,
                (series_id, cutoff_date, max(1, int(limit_per_series))),
            ).fetchall()
            for row in rows:
                results.append(
                    {
                        "series_id": str(row["series_id"]),
                        "observation_date": str(row["observation_date"]),
                        "value": _finite_float(row["value"]),
                        "realtime_start": str(row["realtime_start"] or ""),
                        "realtime_end": str(row["realtime_end"] or ""),
                        "ingestion_version": str(row["ingestion_version"] or ""),
                    }
                )
    return results


def _latest_technical_indicators(db: Any, *, symbol: str, cutoff_date: str) -> list[dict[str, Any]]:
    if not symbol:
        return []
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT symbol,as_of_date,price_source,close,return_1d,return_5d,return_20d,
                   sma_20,sma_50,macd,macd_signal,rsi_14,volatility_20,ingestion_version
            FROM technical_indicators
            WHERE symbol=? AND as_of_date<=?
            ORDER BY as_of_date DESC,
                     CASE price_source
                       WHEN 'ibkr' THEN 1
                       WHEN 'yahoo' THEN 2
                       WHEN 'stooq' THEN 3
                       ELSE 4
                     END,
                     id DESC
            LIMIT 3
            """,
            (symbol.upper(), cutoff_date),
        ).fetchall()
    return [
        {
            "symbol": str(row["symbol"]),
            "as_of_date": str(row["as_of_date"]),
            "price_source": str(row["price_source"]),
            "close": _finite_float(row["close"]),
            "return_1d": _finite_float(row["return_1d"]),
            "return_5d": _finite_float(row["return_5d"]),
            "return_20d": _finite_float(row["return_20d"]),
            "sma_20": _finite_float(row["sma_20"]),
            "sma_50": _finite_float(row["sma_50"]),
            "macd": _finite_float(row["macd"]),
            "macd_signal": _finite_float(row["macd_signal"]),
            "rsi_14": _finite_float(row["rsi_14"]),
            "volatility_20": _finite_float(row["volatility_20"]),
            "ingestion_version": str(row["ingestion_version"] or ""),
        }
        for row in rows
    ]


def _latest_external_market_signals(
    db: Any,
    *,
    snapshot: dict[str, Any],
    event_time: datetime,
    limit: int = 6,
) -> list[dict[str, Any]]:
    """Return compact, event-time-safe external geopolitical/news signals.

    The query intentionally takes a wider pre-event window from the local DB and
    ranks in Python so the desk can use asset overlap and headline/theme hints
    without adding fragile SQL JSON parsing.  It never reads rows after the news
    event timestamp.
    """

    event_iso = event_time.isoformat()
    target_assets = {
        str(item).strip().upper()
        for item in (
            snapshot.get("target_assets")
            or snapshot.get("primary_targets")
            or [snapshot.get("attributed_primary_symbol") or snapshot.get("primary_symbol") or ""]
        )
        if str(item).strip()
    }
    headline_text = " ".join(
        [
            str(snapshot.get("headline") or ""),
            str(snapshot.get("event_scope_hint") or ""),
            str(snapshot.get("deterministic_event_hint") or ""),
            str(snapshot.get("analysis_profile") or ""),
        ]
    ).lower()
    headline_tokens = {
        token
        for token in re.findall(r"[a-z0-9_]{3,}", headline_text)
        if token
        not in {
            "the",
            "and",
            "for",
            "with",
            "from",
            "that",
            "this",
            "market",
            "news",
        }
    }
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT source,title,url,published_at,region,theme,sentiment_score,
                   sentiment_label,confidence,assets_json,asset_map_reason,
                   market_reaction_status,prediction_gate_status,prediction_gate_reason
            FROM external_market_signals
            WHERE published_at!='' AND published_at<=?
            ORDER BY published_at DESC,id DESC
            LIMIT 80
            """,
            (event_iso,),
        ).fetchall()

    ranked: list[tuple[float, dict[str, Any]]] = []
    for row in rows:
        try:
            assets = [str(item).upper() for item in json.loads(row["assets_json"] or "[]")]
        except json.JSONDecodeError:
            assets = []
        asset_overlap = sorted(target_assets.intersection(assets))
        theme = str(row["theme"] or "")
        region = str(row["region"] or "")
        title = str(row["title"] or "")
        signal_tokens = set(re.findall(r"[a-z0-9_]{3,}", f"{title} {region} {theme}".lower()))
        text_overlap = sorted(headline_tokens.intersection(signal_tokens))
        score = 0.0
        if asset_overlap:
            score += 4.0
        if text_overlap:
            score += min(2.0, len(text_overlap) * 0.5)
        if region != "global":
            score += 0.75
        if theme not in {"market_news", "unknown"}:
            score += 0.5
        if str(row["prediction_gate_status"] or "") == "eligible":
            score += 0.5
        if score <= 0 and target_assets:
            continue
        ranked.append(
            (
                score,
                {
                    "source": str(row["source"] or ""),
                    "published_at": str(row["published_at"] or ""),
                    "region": region,
                    "theme": theme,
                    "sentiment_label": str(row["sentiment_label"] or "unknown"),
                    "sentiment_score": _finite_float(row["sentiment_score"]),
                    "confidence": _finite_float(row["confidence"]),
                    "assets": assets,
                    "asset_overlap": asset_overlap,
                    "text_overlap": text_overlap[:8],
                    "market_reaction_status": str(row["market_reaction_status"] or ""),
                    "prediction_gate_status": str(row["prediction_gate_status"] or ""),
                    "prediction_gate_reason": str(row["prediction_gate_reason"] or "")[:240],
                    "title": title[:220],
                    "url": str(row["url"] or ""),
                    "asset_map_reason": str(row["asset_map_reason"] or "")[:240],
                    "relevance_score": score,
                },
            )
        )
    ranked.sort(key=lambda item: (-item[0], item[1]["published_at"]), reverse=False)
    return [item[1] for item in ranked[: max(1, int(limit))]]


def _cross_provider_event_role_packet(
    *,
    snapshot: dict[str, Any],
    event_time: datetime,
    path: str = "exports/m236_cross_provider_event_roles.json",
    limit: int = 6,
) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {
            "available": False,
            "reason": "m236_cross_provider_event_roles_missing",
            "clusters": [],
        }
    target_assets = {
        str(item).strip().upper()
        for item in (
            snapshot.get("target_assets")
            or snapshot.get("primary_targets")
            or [snapshot.get("attributed_primary_symbol") or snapshot.get("primary_symbol") or ""]
        )
        if str(item).strip()
    }
    selected: list[dict[str, Any]] = []
    for cluster in payload.get("clusters") or []:
        safe_items: list[dict[str, Any]] = []
        cluster_assets: set[str] = set()
        for item in cluster.get("items") or []:
            item_time = _parse_event_time(item.get("published_at"))
            if item_time > event_time:
                continue
            assets = {str(asset).strip().upper() for asset in item.get("assets") or [] if str(asset).strip()}
            cluster_assets.update(assets)
            safe_items.append(item)
        if not safe_items:
            continue
        if target_assets and cluster_assets and not target_assets.intersection(cluster_assets):
            continue
        selected.append(
            {
                "provider_agreement": cluster.get("provider_agreement"),
                "provider_disagreement": cluster.get("provider_disagreement"),
                "canonical_event_id": cluster.get("canonical_event_id"),
                "canonical_event": cluster.get("canonical_event") or {},
                "event_role": cluster.get("event_role"),
                "role_family": cluster.get("role_family"),
                "providers": cluster.get("providers") or [],
                "provider_families": cluster.get("provider_families") or [],
                "provider_weighted_confirmation_score": cluster.get("provider_weighted_confirmation_score"),
                "direction_counts": cluster.get("direction_counts") or {},
                "average_direction_score": cluster.get("average_direction_score"),
                "sentiment_role_counts": cluster.get("sentiment_role_counts") or {},
                "average_ticker_inference_confidence": cluster.get("average_ticker_inference_confidence"),
                "adaptive_confidence_cap": cluster.get("adaptive_confidence_cap") or {},
                "confidence_adjustment": cluster.get("confidence_adjustment"),
                "items": safe_items[:4],
            }
        )
        if len(selected) >= max(1, int(limit)):
            break
    return {
        "available": True,
        "packet_version": "scorer-evidence-cross-provider-v3",
        "generated_at": payload.get("generated_at"),
        "clusters": selected,
        "cluster_count": len(selected),
        "m236_source_quality_learning": _m236_source_quality_learning_packet(),
        "anti_leakage_note": "M236 clusters are filtered to item timestamps at or before the event timestamp.",
    }


def _m236_source_quality_learning_packet(path: str = "exports/m236_news_learning_report.json") -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {
            "available": False,
            "reason": "m236_news_learning_report_missing",
        }
    summary = payload.get("summary") or {}
    return {
        "available": True,
        "packet_version": payload.get("m236_news_learning_version"),
        "generated_at": payload.get("generated_at"),
        "record_count": summary.get("record_count", 0),
        "tail_record_count": summary.get("tail_record_count", 0),
        "evaluated_index_record_count": summary.get("evaluated_index_record_count", 0),
        "evaluated_retained_record_count": summary.get("evaluated_retained_record_count", 0),
        "status_counts": summary.get("status_counts") or {},
        "due_horizon_counts": summary.get("due_horizon_counts") or {},
        "scorer_directive_counts": summary.get("scorer_directive_counts") or {},
        "ticker_review_queue_count": summary.get("ticker_review_queue_count", 0),
        "provider_profile": payload.get("provider_profile") or {},
        "provider_role_family_profile": payload.get("provider_role_family_profile") or {},
        "ticker_review_bucket_counts": ((payload.get("ticker_inference_verification") or {}).get("review_queue_bucket_counts") or {}),
        "macro_release_integration": payload.get("macro_release_integration") or {},
        "learning_velocity": payload.get("learning_velocity") or {},
        "scorer_impact_policy": (payload.get("scorer_impact") or {}).get("policy") or {},
        "anti_leakage_note": "M236 learning packet contains source-quality state only; it does not expose future outcomes at scoring time.",
    }


def _m237_macro_release_packet(path: str = "exports/m237_macro_release_interpreter.json") -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {
            "available": False,
            "reason": "m237_macro_release_interpreter_missing",
        }
    packet = payload.get("scorer_evidence_packet") or {}
    latest = packet.get("latest_macro_release") or {}
    return {
        "available": True,
        "packet_version": packet.get("packet_version") or payload.get("macro_release_interpreter_version"),
        "generated_at": payload.get("generated_at"),
        "latest_macro_release": latest,
        "upcoming_event_risk": packet.get("upcoming_event_risk") or [],
        "policy": packet.get("policy") or {},
        "anti_leakage_note": "Latest live macro release packet is for live scoring. Historical replay must use event-time filtered macro rows, not future upcoming calendar items.",
    }


def _official_document_summary(snapshot: dict[str, Any]) -> dict[str, Any]:
    evidence = snapshot.get("official_evidence") or []
    return {
        "attached_count": len(evidence),
        "source_types": sorted({str(item.get("source_type") or "") for item in evidence if item.get("source_type")}),
        "forms": sorted({str(item.get("form_type") or "") for item in evidence if item.get("form_type")}),
        "materiality": [
            {
                "title": str(item.get("title") or "")[:160],
                "classification": str(item.get("materiality_classification") or ""),
                "reason": str(item.get("materiality_reason") or "")[:240],
                "available_at": str(item.get("available_at") or ""),
            }
            for item in evidence[:3]
        ],
    }


def build_source_desk_context(db: Any, snapshot: dict[str, Any]) -> dict[str, Any]:
    """Build a compact, event-time-safe specialist-desk bundle for AI analysis.

    The context deliberately filters structured rows by the news timestamp so
    historical/backfilled analyses cannot see facts, FRED observations, or
    technical indicators dated after the immutable event-time snapshot.
    """

    event_time = _parse_event_time(snapshot.get("published_at_utc") or snapshot.get("received_at_utc"))
    cutoff_date = event_time.date().isoformat()
    symbol = str(snapshot.get("attributed_primary_symbol") or snapshot.get("primary_symbol") or "").upper()
    target_quotes = [
        _compact_quote(dict(row))
        for row in (snapshot.get("target_quote_audit_snapshot") or snapshot.get("baseline_market_snapshot") or [])[:3]
    ]
    delivery_quotes = [
        _compact_quote(dict(row))
        for row in (snapshot.get("delivery_market_snapshot") or [])[:3]
    ]
    financial_facts = _latest_financial_facts(db, symbol=symbol, cutoff_date=cutoff_date)
    fred_observations = _latest_fred_observations(db, cutoff_date=cutoff_date)
    economic_indicator_desk = compact_economic_indicator_desk(
        build_economic_indicator_desk(db, cutoff_date=cutoff_date),
        max_indicators=24,
    )
    technical_indicators = _latest_technical_indicators(db, symbol=symbol, cutoff_date=cutoff_date)
    external_signals = _latest_external_market_signals(db, snapshot=snapshot, event_time=event_time)
    cross_provider_packet = _cross_provider_event_role_packet(snapshot=snapshot, event_time=event_time)

    return {
        "context_version": SOURCE_DESK_CONTEXT_VERSION,
        "event_time_utc": event_time.isoformat(),
        "cutoff_date": cutoff_date,
        "anti_leakage_note": (
            "All structured desk rows are filtered to timestamps/dates available at or before "
            "the immutable event-time cutoff. Missing rows mean the local database had no "
            "event-time-safe context for that desk."
        ),
        "market_data_desk": {
            "target_quotes": target_quotes,
            "delivery_quotes": delivery_quotes,
            "target_quote_available": bool(target_quotes),
            "delivery_quote_available": bool(delivery_quotes),
        },
        "news_desk": {
            "provider": str(snapshot.get("provider") or ""),
            "source_tier_hint": str(snapshot.get("source_tier_hint") or "unknown"),
            "article_body_available": bool(snapshot.get("article_body_available_to_model")),
            "article_body_truncated": bool(snapshot.get("article_body_truncated")),
            "timestamp_quality": str((snapshot.get("timestamp_provenance") or {}).get("timestamp_quality") or ""),
        },
        "sec_filings_desk": _official_document_summary(snapshot),
        "financial_statements_desk": {
            "symbol": symbol,
            "facts": financial_facts,
            "fact_count": len(financial_facts),
        },
        "fred_macro_desk": {
            "observations": fred_observations,
            "series_count": len({row["series_id"] for row in fred_observations}),
        },
        "economic_indicator_desk": economic_indicator_desk,
        "macro_release_interpreter_desk": _m237_macro_release_packet(),
        "technical_indicators_desk": {
            "symbol": symbol,
            "indicators": technical_indicators,
            "indicator_count": len(technical_indicators),
        },
        "external_geopolitical_news_desk": {
            "signals": external_signals,
            "signal_count": len(external_signals),
            "cross_provider_event_role_packet": cross_provider_packet,
            "selection_note": (
                "Marketaux, Alpha Vantage, and any retained historical external rows are filtered to published_at at or before "
                "the event timestamp, then ranked by target-asset overlap, region/theme relevance, "
                "and prediction-gate status. These rows are cross-check context only, not realized outcomes."
            ),
        },
    }


# --- merged from source_ingest_health.py ---
SOURCE_INGEST_HEALTH_VERSION = "source-ingest-health-v3-session-aware-gap-split"
SOURCE_INGEST_HEALTH_LOOP_VERSION = "source-ingest-health-loop-v1"

DEFAULT_ONLINE_REQUIRED_SOURCES = ("market_data", "market_news", "geopolitical_signals")
DEFAULT_BATCH_SOURCES = ("standing_regimes", "economic_indicators", "technical_indicators")
MONITORING_ONLY_ONLINE_GAP_ROOTS = {
    "provider_returned_no_newer_rows_or_query_window_gap",
    "provider_ok_ingest_or_freshness_pipeline_lag",
    "source_lagging_waiting_for_next_refresh",
    "watchdog_cooldown_gap_remains",
}
MONITORING_ONLY_ONLINE_GAP_SOURCES = {"geopolitical_signals"}


@dataclass(frozen=True)
class SourceIngestHealthConfig:
    freshness_report_path: Path
    refresh_queue_path: Path | None = None
    watchdog_path: Path | None = None
    live_validation_path: Path | None = None
    online_required_sources: tuple[str, ...] = DEFAULT_ONLINE_REQUIRED_SOURCES
    batch_sources: tuple[str, ...] = DEFAULT_BATCH_SOURCES
    max_ticks: int = 1
    interval_seconds: float = 1.0
    sleep_between_ticks: bool = False


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _read_json(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        return {}
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _source_ingest_age_minutes(value: Any, *, now: datetime) -> float | None:
    parsed = parse_datetime(value)
    if parsed is None:
        return None
    return round((now - parsed).total_seconds() / 60.0, 3)


def _queue_jobs_by_source(queue: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    output: dict[str, list[dict[str, Any]]] = {}
    for job in queue.get("jobs") or []:
        if not isinstance(job, dict):
            continue
        for source_key in job.get("source_keys") or []:
            output.setdefault(str(source_key), []).append(job)
    return output


def _status_rank(status: str) -> int:
    return {"healthy": 0, "watch": 1, "degraded": 2, "offline": 3}.get(status, 1)


def _job_root_cause(jobs: list[dict[str, Any]]) -> tuple[str | None, dict[str, Any]]:
    if not jobs:
        return None, {}
    status_counts = Counter(str(job.get("status") or "unknown") for job in jobs)
    if status_counts.get("failed"):
        failed = next((job for job in jobs if job.get("status") == "failed"), {})
        return "refresh_job_failed", {"job_id": failed.get("job_id"), "status_counts": dict(status_counts), "stderr_tail": failed.get("stderr_tail") or ""}
    if status_counts.get("timeout"):
        failed = next((job for job in jobs if job.get("status") == "timeout"), {})
        return "refresh_job_timeout", {"job_id": failed.get("job_id"), "status_counts": dict(status_counts)}
    if status_counts.get("skipped_live_ibkr_disabled"):
        return "live_ibkr_refresh_disabled", {"status_counts": dict(status_counts)}
    if status_counts.get("skipped_max_jobs"):
        return "refresh_job_skipped_by_max_jobs", {"status_counts": dict(status_counts)}
    if status_counts and set(status_counts) <= {"succeeded"}:
        return "refresh_succeeded_gap_remains", {"status_counts": dict(status_counts)}
    if status_counts.get("planned"):
        return "refresh_planned_not_executed", {"status_counts": dict(status_counts)}
    return "refresh_status_mixed", {"status_counts": dict(status_counts)}


def _live_provider_summary(live_validation: dict[str, Any], source_key: str) -> dict[str, Any]:
    source_status = (live_validation.get("source_status") or {}).get(source_key)
    providers = [row for row in live_validation.get("providers") or [] if row.get("source_key") == source_key]
    ok_providers = [row for row in providers if row.get("status") == "ok"]
    return {
        "source_status": source_status,
        "provider_count": len(providers),
        "ok_provider_count": len(ok_providers),
        "provider_status_counts": dict(Counter(str(row.get("status") or "unknown") for row in providers)),
        "latest_provider_event_at": _latest_provider_event_at(providers),
    }


def _latest_provider_event_at(providers: list[dict[str, Any]]) -> str | None:
    parsed = [item for item in (parse_datetime(row.get("latest_event_at")) for row in providers) if item is not None]
    if not parsed:
        return None
    return max(parsed).isoformat()


def _root_cause(
    *,
    source: dict[str, Any],
    source_jobs: list[dict[str, Any]],
    watchdog: dict[str, Any],
    live_validation: dict[str, Any],
    online_required: bool,
) -> tuple[str, str, dict[str, Any]]:
    status = str(source.get("status") or "offline").lower()
    row_count = source.get("row_count")
    latest_event_at = source.get("latest_event_at")
    latest_ingested_at = source.get("latest_ingested_at")
    source_key = str(source.get("source_key") or "")
    provider_summary = _live_provider_summary(live_validation, source_key)
    job_cause, job_details = _job_root_cause(source_jobs)
    watchdog_status = str(watchdog.get("status") or watchdog.get("final_status") or "").lower()
    watchdog_lagging = set(watchdog.get("last_lagging_source_keys") or watchdog.get("final_lagging_source_keys") or [])
    watchdog_stale = set(watchdog.get("last_stale_source_keys") or watchdog.get("final_stale_source_keys") or [])
    source_details = source.get("details") if isinstance(source.get("details"), dict) else {}
    marketaux_overlay = source_details.get("marketaux_auxiliary_freshness") if isinstance(source_details, dict) else None

    if status == "fresh":
        return "healthy_online" if online_required else "healthy_batch", "healthy", {}
    if (
        source_key == "market_news"
        and status == "lagging"
        and isinstance(marketaux_overlay, dict)
        and marketaux_overlay.get("applied")
    ):
        return "market_news_lagging_but_marketaux_crosscheck_fresh", "healthy", marketaux_overlay
    if online_required and provider_summary.get("source_status") == "ok":
        return "provider_ok_ingest_or_freshness_pipeline_lag", "watch", provider_summary
    if online_required and provider_summary.get("source_status") in {"degraded", "no_rows", "error"}:
        return "provider_live_validation_problem", "degraded" if provider_summary.get("source_status") != "error" else "offline", provider_summary
    if row_count in (0, "0") or row_count is None and not latest_event_at and not latest_ingested_at:
        return "no_rows_or_source_missing", "offline" if online_required else "degraded", {}
    if not latest_event_at and latest_ingested_at:
        return "timestamp_gap_or_parse_issue", "degraded", {"latest_ingested_at": latest_ingested_at}
    if job_cause == "refresh_succeeded_gap_remains":
        return "provider_returned_no_newer_rows_or_query_window_gap", "watch" if status == "lagging" else "degraded", job_details
    if job_cause:
        severity = "degraded" if job_cause in {"refresh_job_failed", "refresh_job_timeout", "live_ibkr_refresh_disabled"} else "watch"
        return job_cause, severity, job_details
    if watchdog_status == "skipped_cooldown" and (source_key in watchdog_lagging or source_key in watchdog_stale):
        return "watchdog_cooldown_gap_remains", "watch" if status == "lagging" else "degraded", {
            "next_refresh_eligible_at": watchdog.get("next_refresh_eligible_at"),
            "watchdog_status": watchdog_status,
        }
    if status == "lagging":
        return "source_lagging_waiting_for_next_refresh", "watch", {}
    if status == "stale":
        return "source_stale_refresh_required", "degraded", {}
    return "source_offline_refresh_required", "offline" if online_required else "degraded", {}


def _recommended_action(source_key: str, root_cause: str, *, online_required: bool) -> str:
    if root_cause in {"healthy_online", "healthy_batch"}:
        return "continue_collection"
    if root_cause == "provider_returned_no_newer_rows_or_query_window_gap":
        return "expand_backfill_window_or_validate_provider_returns"
    if root_cause == "provider_ok_ingest_or_freshness_pipeline_lag":
        return "run_ingest_catchup_then_rebuild_freshness"
    if root_cause == "provider_live_validation_problem":
        return "inspect_provider_specific_live_validation_report"
    if root_cause == "timestamp_gap_or_parse_issue":
        return "inspect_timestamp_mapping_and_db_write_path"
    if root_cause in {"refresh_job_failed", "refresh_job_timeout"}:
        return "inspect_refresh_job_error_then_retry"
    if root_cause == "live_ibkr_refresh_disabled":
        return "enable_live_ibkr_read_only_for_this_source"
    if root_cause == "refresh_planned_not_executed":
        return "run_refresh_queue_execute_or_watchdog"
    if root_cause == "watchdog_cooldown_gap_remains":
        return "wait_for_cooldown_or_expand_source_specific_catchup"
    if root_cause == "market_news_lagging_but_marketaux_crosscheck_fresh":
        return "continue_broker_news_catchup_keep_marketaux_crosscheck"
    if online_required:
        return f"keep_{source_key}_online_and_run_catchup"
    return f"schedule_{source_key}_batch_refresh"


def _online_gap_is_monitoring_only(row: dict[str, Any]) -> bool:
    source_key = str(row.get("source_key") or "")
    root_cause = str(row.get("root_cause") or "")
    health_status = str(row.get("health_status") or "")
    if source_key not in MONITORING_ONLY_ONLINE_GAP_SOURCES:
        return False
    if root_cause not in MONITORING_ONLY_ONLINE_GAP_ROOTS:
        return False
    if health_status not in {"watch", "healthy"}:
        return False
    # If the ingest timestamp is fresh but the event timestamp is old, this is
    # usually a quiet geopolitical tape or provider query-window gap, not a dead
    # feed.  Keep it visible, but do not let it masquerade as a trade blocker.
    ingestion_age = row.get("ingestion_age_minutes")
    if ingestion_age is None:
        return bool(row.get("row_count"))
    try:
        return float(ingestion_age) <= 180.0 or bool(row.get("row_count"))
    except (TypeError, ValueError):
        return bool(row.get("row_count"))


def build_source_ingest_health_report(
    *,
    freshness_report: dict[str, Any],
    refresh_queue: dict[str, Any] | None = None,
    watchdog: dict[str, Any] | None = None,
    live_validation: dict[str, Any] | None = None,
    config: SourceIngestHealthConfig,
    now: str | datetime | None = None,
) -> dict[str, Any]:
    now_dt = parse_datetime(now) if isinstance(now, str) else now
    if now_dt is None:
        now_dt = datetime.now(UTC)
    now_dt = now_dt.astimezone(UTC)
    refresh_queue = refresh_queue or {}
    watchdog = watchdog or {}
    live_validation = live_validation or {}
    jobs_by_source = _queue_jobs_by_source(refresh_queue)
    online_required = set(config.online_required_sources)
    batch_sources = set(config.batch_sources)

    rows: list[dict[str, Any]] = []
    freshness_sources = {
        str(source.get("source_key") or ""): source
        for source in freshness_report.get("sources") or []
        if isinstance(source, dict)
    }
    all_source_keys = tuple(dict.fromkeys((*config.online_required_sources, *config.batch_sources, *freshness_sources.keys())))
    for source_key in all_source_keys:
        source = dict(freshness_sources.get(source_key) or {"source_key": source_key, "status": "offline", "row_count": None})
        is_online = source_key in online_required
        root_cause, health_status, details = _root_cause(
            source=source,
            source_jobs=jobs_by_source.get(source_key) or [],
            watchdog=watchdog,
            live_validation=live_validation,
            online_required=is_online,
        )
        latest_event_at = source.get("latest_event_at")
        latest_ingested_at = source.get("latest_ingested_at")
        rows.append(
            {
                "source_key": source_key,
                "label": source.get("label") or source_key,
                "freshness_status": source.get("status") or "offline",
                "health_status": health_status,
                "online_required": is_online,
                "batch_source": source_key in batch_sources,
                "latest_event_at": latest_event_at,
                "latest_ingested_at": latest_ingested_at,
                "event_age_minutes": _source_ingest_age_minutes(latest_event_at, now=now_dt),
                "ingestion_age_minutes": _source_ingest_age_minutes(latest_ingested_at, now=now_dt),
                "row_count": source.get("row_count"),
                "root_cause": root_cause,
                "recommended_action": _recommended_action(source_key, root_cause, online_required=is_online),
                "refresh_job_ids": [str(job.get("job_id")) for job in jobs_by_source.get(source_key) or []],
                "details": details,
            }
        )

    health_counts = Counter(str(row.get("health_status") or "watch") for row in rows)
    root_cause_counts = Counter(str(row.get("root_cause") or "unknown") for row in rows)
    online_gap_rows = [row for row in rows if row.get("online_required") and row.get("health_status") != "healthy"]
    monitoring_only_online_rows = [row for row in online_gap_rows if _online_gap_is_monitoring_only(row)]
    online_problem_rows = [row for row in online_gap_rows if row not in monitoring_only_online_rows]
    degraded_rows = [row for row in rows if _status_rank(str(row.get("health_status"))) >= 2]
    if any(row.get("health_status") == "offline" and row.get("online_required") for row in rows):
        overall_status = "red"
    elif degraded_rows:
        overall_status = "yellow"
    elif online_problem_rows:
        overall_status = "watch"
    elif monitoring_only_online_rows:
        overall_status = "watch"
    else:
        overall_status = "green"

    return {
        "source_ingest_health_version": SOURCE_INGEST_HEALTH_VERSION,
        "generated_at": now_dt.isoformat(),
        "overall_status": overall_status,
        "health_counts": dict(health_counts),
        "root_cause_counts": dict(root_cause_counts),
        "online_required_sources": list(config.online_required_sources),
        "online_problem_source_keys": [str(row["source_key"]) for row in online_problem_rows],
        "monitoring_only_online_source_keys": [str(row["source_key"]) for row in monitoring_only_online_rows],
        "online_gap_source_keys": [str(row["source_key"]) for row in online_gap_rows],
        "degraded_source_keys": [str(row["source_key"]) for row in degraded_rows],
        "freshness_report_status": freshness_report.get("overall_status"),
        "refresh_queue_status_counts": refresh_queue.get("status_counts") or {},
        "watchdog_status": watchdog.get("status") or watchdog.get("final_status"),
        "live_validation_status": live_validation.get("overall_status"),
        "live_validation_source_status": live_validation.get("source_status") or {},
        "sources": rows,
        "safety": {
            "read_only_ingest_health": True,
            "orders_submitted": False,
            "transmit_enabled": False,
            "scorer_rows_mutated": False,
        },
    }


def source_ingest_health_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Source Ingest Health",
        "",
        f"- Version: `{report.get('source_ingest_health_version')}`",
        f"- Generated at: `{report.get('generated_at')}`",
        f"- Overall status: `{report.get('overall_status')}`",
        f"- Health counts: `{report.get('health_counts') or {}}`",
        f"- Root cause counts: `{report.get('root_cause_counts') or {}}`",
        f"- Live validation: `{report.get('live_validation_status') or '-'}` `{report.get('live_validation_source_status') or {}}`",
        f"- Online required sources: `{','.join(report.get('online_required_sources') or [])}`",
        f"- Online problem sources: `{','.join(report.get('online_problem_source_keys') or []) or '-'}`",
        "",
        "| Source | Online | Freshness | Health | Age min | Root cause | Action |",
        "| --- | --- | --- | --- | ---: | --- | --- |",
    ]
    for row in report.get("sources") or []:
        lines.append(
            f"| {row.get('label') or row.get('source_key')} | {row.get('online_required')} | {row.get('freshness_status')} "
            f"| {row.get('health_status')} | {row.get('event_age_minutes')} | `{row.get('root_cause')}` | `{row.get('recommended_action')}` |"
        )
    lines.extend(["", "## Safety", "", "- Read-only source ingest diagnosis; no broker orders, transmit, portfolio mutation, or scorer-row mutation."])
    return "\n".join(lines) + "\n"


def write_source_ingest_health_json(output_path: Path, report: dict[str, Any]) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return output_path


def write_source_ingest_health_markdown(output_path: Path, report: dict[str, Any]) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(source_ingest_health_markdown(report), encoding="utf-8")
    return output_path


def run_source_ingest_health_loop(
    *,
    config: SourceIngestHealthConfig,
    output_path: Path | None = None,
    json_output_path: Path | None = None,
    tick_log_jsonl_path: Path | None = None,
    run_id: str | None = None,
    refresh_hook: Callable[[int, str], None] | None = None,
) -> dict[str, Any]:
    if config.max_ticks < 0:
        raise ValueError("max_ticks must be >= 0; use 0 only for externally interrupted continuous operation")
    if config.interval_seconds < 0:
        raise ValueError("interval_seconds must be >= 0")
    started_at = _iso_now()
    run_id = run_id or f"source_ingest_health_loop_{started_at.replace(':', '').replace('+', 'Z')}"
    tick_summaries: list[dict[str, Any]] = []
    latest_report: dict[str, Any] = {}
    tick_index = 0
    continuous = config.max_ticks == 0
    while continuous or tick_index < config.max_ticks:
        tick_index += 1
        tick_started = time.monotonic()
        if refresh_hook is not None:
            refresh_hook(tick_index, run_id)
        freshness_report = _read_json(config.freshness_report_path)
        refresh_queue = _read_json(config.refresh_queue_path)
        watchdog = _read_json(config.watchdog_path)
        live_validation = _read_json(config.live_validation_path)
        latest_report = build_source_ingest_health_report(
            freshness_report=freshness_report,
            refresh_queue=refresh_queue,
            watchdog=watchdog,
            live_validation=live_validation,
            config=config,
        )
        if output_path is not None:
            write_source_ingest_health_markdown(output_path, latest_report)
        if json_output_path is not None:
            write_source_ingest_health_json(json_output_path, latest_report)
        tick_summary = {
            "source_ingest_health_loop_version": SOURCE_INGEST_HEALTH_LOOP_VERSION,
            "run_id": run_id,
            "tick_index": tick_index,
            "completed_at": latest_report.get("generated_at"),
            "overall_status": latest_report.get("overall_status"),
            "online_problem_source_keys": latest_report.get("online_problem_source_keys") or [],
            "health_counts": latest_report.get("health_counts") or {},
            "root_cause_counts": latest_report.get("root_cause_counts") or {},
            "elapsed_seconds": round(time.monotonic() - tick_started, 6),
        }
        tick_summaries.append(tick_summary)
        if tick_log_jsonl_path is not None:
            tick_log_jsonl_path = tick_log_jsonl_path.expanduser().resolve()
            tick_log_jsonl_path.parent.mkdir(parents=True, exist_ok=True)
            with tick_log_jsonl_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(tick_summary, ensure_ascii=False, separators=(",", ":")) + "\n")
        if (continuous or tick_index < config.max_ticks) and config.sleep_between_ticks and config.interval_seconds > 0:
            sleep_for = max(0.0, config.interval_seconds - tick_summary["elapsed_seconds"])
            if sleep_for > 0:
                time.sleep(sleep_for)
    return {
        "source_ingest_health_loop_version": SOURCE_INGEST_HEALTH_LOOP_VERSION,
        "run_id": run_id,
        "started_at": started_at,
        "completed_at": _iso_now(),
        "executed_tick_count": len(tick_summaries),
        "max_ticks": config.max_ticks,
        "interval_seconds": config.interval_seconds,
        "sleep_between_ticks": config.sleep_between_ticks,
        "final_status": latest_report.get("overall_status"),
        "final_online_problem_source_keys": latest_report.get("online_problem_source_keys") or [],
        "tick_summaries": tick_summaries,
        "latest_report": latest_report,
        "safety": {
            "read_only_ingest_health_loop": True,
            "orders_submitted": False,
            "transmit_enabled": False,
            "scorer_rows_mutated": False,
        },
    }


# --- merged from source_live_validation.py ---
SOURCE_LIVE_VALIDATION_VERSION = "source-live-validation-v4"


@dataclass(frozen=True)
class SourceLiveValidationConfig:
    symbols: tuple[str, ...]
    providers: tuple[str, ...]
    external_symbols: tuple[str, ...]
    alpha_tickers: tuple[str, ...]
    marketaux_search: str | None = "geopolitical OR sanctions OR Taiwan OR oil OR war"
    marketaux_filter_entities: bool = True
    alpha_topics: str | None = None
    limit: int = 10
    ibkr_timeout_seconds: float = 20.0
    external_timeout_seconds: float = 20.0
    external_retry_count: int = 1
    external_retry_sleep_seconds: float = 0.5
    ibkr_client_id: int | None = None
    include_ibkr: bool = True
    include_marketaux: bool = True
    include_alpha_vantage: bool = False


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _safe_error(exc: BaseException) -> str:
    return _redact_error(f"{exc.__class__.__name__}: {str(exc)[:500]}")


def _source_live_parse_dt(value: Any) -> datetime | None:
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


def _latest(values: list[str]) -> str | None:
    parsed = [item for item in (_source_live_parse_dt(value) for value in values) if item is not None]
    if not parsed:
        return None
    return max(parsed).isoformat()


def _source_live_age_minutes(value: str | None) -> float | None:
    parsed = _source_live_parse_dt(value)
    if parsed is None:
        return None
    return round((datetime.now(UTC) - parsed).total_seconds() / 60.0, 3)


def _is_transient_ibkr_warning(error: str) -> bool:
    text = str(error or "").lower()
    transient_markers = (
        "2103",
        "2104",
        "2106",
        "2107",
        "2108",
        "2158",
        "market data farm connection is broken",
        "market data farm connection is ok",
        "hmds data farm connection",
        "sec-def data farm connection",
    )
    return any(marker in text for marker in transient_markers)


def _status(*, reachable: bool, row_count: int, errors: list[str], latest_event_at: str | None, max_age_minutes: float | None = None) -> str:
    if errors and not reachable:
        return "error"
    if not reachable:
        return "unreachable"
    if errors:
        return "degraded"
    if row_count <= 0:
        return "no_rows"
    if max_age_minutes is not None:
        age = _source_live_age_minutes(latest_event_at)
        if age is not None and age > max_age_minutes:
            return "stale_response"
    return "ok"


def _ibkr_news_status(*, reachable: bool, row_count: int, errors: list[str], latest_event_at: str | None) -> str:
    base_status = _status(
        reachable=reachable,
        row_count=row_count,
        errors=[] if errors and all(_is_transient_ibkr_warning(error) for error in errors) else errors,
        latest_event_at=latest_event_at,
        max_age_minutes=240.0,
    )
    if base_status == "ok" and errors and all(_is_transient_ibkr_warning(error) for error in errors):
        return "ok_with_transient_warnings"
    return base_status


class IBKRNewsProbe(EWrapper, EClient):
    def __init__(
        self,
        settings: Any,
        *,
        symbols: tuple[str, ...],
        providers: tuple[str, ...],
        limit: int,
        timeout_seconds: float,
        client_id: int | None,
    ) -> None:
        EClient.__init__(self, self)
        self.settings = settings
        self.symbols = tuple(dict.fromkeys(symbol.upper() for symbol in symbols if symbol.strip()))
        self.provider_config = providers
        self.limit = max(1, int(limit))
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.client_id = int(client_id if client_id is not None else settings.client_id + 2600)
        self._request_ids = iter(range(860000, 861000))
        self._providers: dict[str, str] = {}
        self._provider_received = False
        self._contracts_by_symbol: dict[str, Contract] = {}
        self._contract_req_to_symbol: dict[int, str] = {}
        self._contract_completed: set[int] = set()
        self._hist_req_to_symbol: dict[int, str] = {}
        self._hist_completed: set[int] = set()
        self._rows_by_symbol: Counter[str] = Counter()
        self._rows_by_provider: Counter[str] = Counter()
        self._latest_times: list[str] = []
        self._errors: list[str] = []
        self._done = threading.Event()
        self._api_thread: threading.Thread | None = None
        self._requests_started = False

    @staticmethod
    def _stock_contract(symbol: str) -> Contract:
        contract = Contract()
        contract.symbol = symbol
        contract.secType = "STK"
        contract.exchange = "SMART"
        contract.currency = "USD"
        return contract

    def selected_provider_codes(self) -> tuple[str, ...]:
        configured = self.provider_config
        if configured == ("AUTO",):
            return tuple(self._providers)
        return tuple(code for code in configured if not self._providers or code in self._providers)

    def nextValidId(self, orderId: int) -> None:  # noqa: N802
        self.reqNewsProviders()
        for symbol in self.symbols:
            req_id = next(self._request_ids)
            self._contract_req_to_symbol[req_id] = symbol
            self.reqContractDetails(req_id, self._stock_contract(symbol))

    def newsProviders(self, newsProviders):  # noqa: N802
        self._providers = {provider.code: provider.name for provider in newsProviders}
        self._provider_received = True
        self._try_start()

    def contractDetails(self, reqId, contractDetails):  # noqa: N802
        symbol = self._contract_req_to_symbol.get(reqId)
        if symbol and symbol not in self._contracts_by_symbol:
            self._contracts_by_symbol[symbol] = contractDetails.contract

    def contractDetailsEnd(self, reqId: int) -> None:  # noqa: N802
        self._contract_completed.add(reqId)
        symbol = self._contract_req_to_symbol.get(reqId)
        if symbol and symbol not in self._contracts_by_symbol:
            self._errors.append(f"No contract found for {symbol}")
        self._try_start()

    def _try_start(self) -> None:
        if self._requests_started or not self._provider_received:
            return
        if len(self._contract_completed) < len(self._contract_req_to_symbol):
            return
        self._requests_started = True
        provider_codes = self.selected_provider_codes()
        if not provider_codes:
            self._errors.append("No selected IBKR news providers available")
            self._done.set()
            return
        provider_code_string = "+".join(provider_codes)
        anchor = datetime.now(UTC)
        for symbol, contract in self._contracts_by_symbol.items():
            con_id = int(getattr(contract, "conId", 0) or 0)
            if con_id <= 0:
                self._errors.append(f"Contract for {symbol} has no conId")
                continue
            req_id = next(self._request_ids)
            self._hist_req_to_symbol[req_id] = symbol
            self.reqHistoricalNews(req_id, con_id, provider_code_string, format_ibkr_news_datetime(anchor), "", self.limit, [])
        if not self._hist_req_to_symbol:
            self._done.set()

    def historicalNews(self, requestId: int, time: str, providerCode: str, articleId: str, headline: str) -> None:  # noqa: N802
        symbol = self._hist_req_to_symbol.get(requestId, "UNKNOWN")
        self._rows_by_symbol[symbol] += 1
        self._rows_by_provider[str(providerCode)] += 1
        self._latest_times.append(parse_ibkr_news_time(time, fallback=datetime.now(UTC)).isoformat())

    def historicalNewsEnd(self, requestId: int, hasMore: bool) -> None:  # noqa: N802
        self._hist_completed.add(requestId)
        if len(self._hist_completed) >= len(self._hist_req_to_symbol):
            self._done.set()

    def error(self, reqId, *args) -> None:
        code = args[0] if args and isinstance(args[0], int) else None
        if code in {2104, 2106, 2107, 2108, 2158}:
            return
        self._errors.append(_redact_error(f"IBKR message reqId={reqId} details={args}"))
        if reqId in self._hist_req_to_symbol:
            self._hist_completed.add(reqId)
            if len(self._hist_completed) >= len(self._hist_req_to_symbol):
                self._done.set()

    def run_probe(self) -> dict[str, Any]:
        connected = False
        started = time.monotonic()
        try:
            self.connect(self.settings.host, self.settings.port, clientId=self.client_id)
            connected = True
            self._api_thread = threading.Thread(target=self.run, daemon=True)
            self._api_thread.start()
            timed_out = not self._done.wait(self.timeout_seconds)
        except Exception as exc:
            self._errors.append(_safe_error(exc))
            timed_out = False
        finally:
            if self.isConnected():
                self.disconnect()
            if self._api_thread is not None and self._api_thread.is_alive():
                time.sleep(0.1)
        rows = sum(self._rows_by_symbol.values())
        errors = list(dict.fromkeys(self._errors))
        if timed_out:
            errors.append(f"timeout_after_{self.timeout_seconds}_seconds")
        latest_event_at = _latest(self._latest_times)
        hard_errors = [error for error in errors if not _is_transient_ibkr_warning(error)]
        return {
            "provider": "ibkr_historical_news",
            "source_key": "market_news",
            "reachable": connected and not timed_out and not any("connection" in err.lower() for err in hard_errors),
            "status": _ibkr_news_status(
                reachable=connected and not timed_out,
                row_count=rows,
                errors=errors,
                latest_event_at=latest_event_at,
            ),
            "row_count": rows,
            "latest_event_at": latest_event_at,
            "latest_event_age_minutes": _source_live_age_minutes(latest_event_at),
            "configured_provider_codes": list(self.provider_config),
            "available_provider_codes": list(self._providers),
            "selected_provider_codes": list(self.selected_provider_codes()),
            "rows_by_symbol": dict(self._rows_by_symbol),
            "rows_by_provider": dict(self._rows_by_provider),
            "completed_symbols": len({self._hist_req_to_symbol[req_id] for req_id in self._hist_completed if req_id in self._hist_req_to_symbol}),
            "requested_symbols": len(self.symbols),
            "duration_seconds": round(time.monotonic() - started, 3),
            "transient_warning_count": sum(1 for error in errors if _is_transient_ibkr_warning(error)),
            "errors": errors,
        }


def validate_marketaux(
    *,
    api_key: str | None,
    symbols: tuple[str, ...],
    limit: int,
    timeout_seconds: float,
    search: str | None = "geopolitical OR sanctions OR Taiwan OR oil OR war",
    filter_entities: bool = True,
    retry_count: int = 1,
    retry_sleep_seconds: float = 0.5,
) -> dict[str, Any]:
    if not api_key:
        return {"provider": "marketaux", "source_key": "geopolitical_signals", "reachable": False, "status": "missing_credentials", "row_count": 0, "latest_event_at": None, "latest_event_age_minutes": None, "errors": ["MARKETAUX_API_KEY is not configured"]}
    errors: list[str] = []
    rows: list[dict[str, Any]] = []
    latest = None
    reachable = False
    attempts = max(1, int(retry_count) + 1)
    attempt_count = 0
    for attempt_index in range(attempts):
        attempt_count = attempt_index + 1
        try:
            payload = MarketauxClient(api_key=api_key, timeout_seconds=timeout_seconds).fetch(
                limit=limit,
                symbols=",".join(symbols) if symbols else None,
                search=search,
                filter_entities=filter_entities,
            )
            rows = payload.get("data", []) or []
            latest = _latest([str(item.get("published_at") or "") for item in rows])
            reachable = True
            errors = []
            break
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            errors.append(_safe_error(exc))
            if attempt_index + 1 < attempts and retry_sleep_seconds > 0:
                time.sleep(float(retry_sleep_seconds))
    return {
        "provider": "marketaux",
        "source_key": "geopolitical_signals",
        "reachable": reachable,
        "status": _status(reachable=reachable, row_count=len(rows), errors=errors, latest_event_at=latest, max_age_minutes=360.0),
        "row_count": len(rows),
        "latest_event_at": latest,
        "latest_event_age_minutes": _source_live_age_minutes(latest),
        "attempt_count": attempt_count,
        "symbols": list(symbols),
        "search": search,
        "filter_entities": filter_entities,
        "errors": errors,
    }


def validate_alpha_vantage(
    *,
    api_key: str | None,
    tickers: tuple[str, ...],
    limit: int,
    timeout_seconds: float,
    topics: str | None = None,
    retry_count: int = 1,
    retry_sleep_seconds: float = 0.5,
) -> dict[str, Any]:
    if not api_key:
        return {"provider": "alpha_vantage", "source_key": "geopolitical_signals", "reachable": False, "status": "missing_credentials", "row_count": 0, "latest_event_at": None, "latest_event_age_minutes": None, "errors": ["ALPHA_VANTAGE_API_KEY is not configured"]}
    errors: list[str] = []
    rows: list[dict[str, Any]] = []
    latest = None
    reachable = False
    attempts = max(1, int(retry_count) + 1)
    attempt_count = 0
    for attempt_index in range(attempts):
        attempt_count = attempt_index + 1
        try:
            payload = AlphaVantageNewsClient(api_key=api_key, timeout_seconds=timeout_seconds).fetch(
                tickers=",".join(tickers),
                topics=topics,
                limit=limit,
            )
            info = payload.get("Information") or payload.get("Note")
            if info:
                return {
                    "provider": "alpha_vantage",
                    "source_key": "geopolitical_signals",
                    "reachable": True,
                    "status": "provider_notice",
                    "row_count": 0,
                    "latest_event_at": None,
                    "latest_event_age_minutes": None,
                    "attempt_count": attempt_index + 1,
                    "tickers": list(tickers),
                    "topics": topics,
                    "errors": [_redact_error(info)],
                }
            rows = payload.get("feed", []) or []
            latest = _latest([_normalize_alpha_time(str(item.get("time_published") or "")) for item in rows])
            reachable = True
            errors = []
            break
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            errors.append(_safe_error(exc))
            if attempt_index + 1 < attempts and retry_sleep_seconds > 0:
                time.sleep(float(retry_sleep_seconds))
    return {
        "provider": "alpha_vantage",
        "source_key": "geopolitical_signals",
        "reachable": reachable,
        "status": _status(reachable=reachable, row_count=len(rows), errors=errors, latest_event_at=latest, max_age_minutes=720.0),
        "row_count": len(rows),
        "latest_event_at": latest,
        "latest_event_age_minutes": _source_live_age_minutes(latest),
        "attempt_count": attempt_count,
        "tickers": list(tickers),
        "topics": topics,
        "errors": errors,
    }


def build_source_live_validation_report(*, settings: Any, config: SourceLiveValidationConfig) -> dict[str, Any]:
    providers: list[dict[str, Any]] = []
    if config.include_ibkr:
        providers.append(
            IBKRNewsProbe(
                settings,
                symbols=config.symbols,
                providers=config.providers,
                limit=config.limit,
                timeout_seconds=config.ibkr_timeout_seconds,
                client_id=config.ibkr_client_id,
            ).run_probe()
        )
    if config.include_marketaux:
        providers.append(
            validate_marketaux(
                api_key=getattr(settings, "marketaux_api_key", None),
                symbols=config.external_symbols,
                limit=config.limit,
                timeout_seconds=config.external_timeout_seconds,
                search=config.marketaux_search,
                filter_entities=config.marketaux_filter_entities,
                retry_count=config.external_retry_count,
                retry_sleep_seconds=config.external_retry_sleep_seconds,
            )
        )
    if config.include_alpha_vantage:
        providers.append(
            validate_alpha_vantage(
                api_key=getattr(settings, "alpha_vantage_api_key", None),
                tickers=config.alpha_tickers,
                limit=config.limit,
                timeout_seconds=config.external_timeout_seconds,
                topics=config.alpha_topics,
                retry_count=config.external_retry_count,
                retry_sleep_seconds=config.external_retry_sleep_seconds,
            )
        )
    status_counts = Counter(str(row.get("status") or "unknown") for row in providers)
    source_status: dict[str, str] = {}
    for source_key in sorted({str(row.get("source_key") or "") for row in providers if row.get("source_key")}):
        rows = [row for row in providers if row.get("source_key") == source_key]
        if any(row.get("status") in {"ok", "ok_with_transient_warnings", "cached_ok_after_validation_error"} for row in rows):
            source_status[source_key] = "ok"
        elif any(row.get("status") in {"degraded", "stale_response", "provider_notice"} for row in rows):
            source_status[source_key] = "degraded"
        elif any(row.get("status") == "no_rows" for row in rows):
            source_status[source_key] = "no_rows"
        else:
            source_status[source_key] = "error"
    overall_status = "green" if source_status and all(value == "ok" for value in source_status.values()) else ("yellow" if any(value in {"ok", "degraded", "no_rows"} for value in source_status.values()) else "red")
    return {
        "source_live_validation_version": SOURCE_LIVE_VALIDATION_VERSION,
        "generated_at": _iso_now(),
        "overall_status": overall_status,
        "source_status": source_status,
        "provider_status_counts": dict(status_counts),
        "providers": providers,
        "safety": {
            "read_only_provider_probe": True,
            "news_rows_written": False,
            "external_signal_rows_written": False,
            "orders_submitted": False,
            "transmit_enabled": False,
        },
    }


def source_live_validation_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Source Live Validation",
        "",
        f"- Version: `{report.get('source_live_validation_version')}`",
        f"- Generated at: `{report.get('generated_at')}`",
        f"- Overall status: `{report.get('overall_status')}`",
        f"- Source status: `{report.get('source_status') or {}}`",
        f"- Provider status counts: `{report.get('provider_status_counts') or {}}`",
        "",
        "| Provider | Source | Status | Rows | Latest event | Age min | Errors |",
        "| --- | --- | --- | ---: | --- | ---: | --- |",
    ]
    for row in report.get("providers") or []:
        errors = "; ".join(str(item) for item in (row.get("errors") or [])[:2]) or "-"
        lines.append(
            f"| {row.get('provider')} | {row.get('source_key')} | {row.get('status')} | {row.get('row_count')} "
            f"| {row.get('latest_event_at') or '-'} | {row.get('latest_event_age_minutes')} | {errors} |"
        )
    lines.extend(["", "## Safety", "", "- Read-only provider validation; no DB writes, no broker orders, no transmit."])
    return "\n".join(lines) + "\n"


def write_source_live_validation_json(output_path: Path, report: dict[str, Any]) -> Path:
    resolved = output_path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return resolved


def write_source_live_validation_markdown(output_path: Path, report: dict[str, Any]) -> Path:
    resolved = output_path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(source_live_validation_markdown(report), encoding="utf-8")
    return resolved


# --- merged from source_online_supervisor.py ---
SOURCE_ONLINE_SUPERVISOR_VERSION = "source-online-supervisor-v14-detached-m236-outcome-attachment"
LOCAL_TIME_DISPLAY_VERSION = "computer-local-time-v1"
DISABLED_ALPHA_VANTAGE_STATE_VERSION = "m236-alpha-vantage-auto-disable-v1"
MARKETAUX_VALIDATION_ACTION_ID = "source_live_validation_marketaux"
LEGACY_MARKETAUX_ALPHA_VALIDATION_ACTION_ID = "source_live_validation_marketaux_alpha"

CommandRunner = Callable[[list[str], Path, float], dict[str, Any]]


@dataclass(frozen=True)
class SourceOnlineSupervisorConfig:
    live_validation_profile_path: Path
    live_validation_json_path: Path
    live_validation_markdown_path: Path
    freshness_profile_path: Path
    freshness_report_path: Path
    source_ingest_health_profile_path: Path
    source_ingest_health_json_path: Path
    source_ingest_health_markdown_path: Path
    output_dir: Path
    state_json_path: Path
    tick_log_jsonl_path: Path | None = None
    max_ticks: int = 1
    interval_seconds: float = 1.0
    sleep_between_ticks: bool = False
    execute_actions: bool = False
    include_live_ibkr: bool = True
    job_timeout_seconds: float = 180.0
    action_cooldown_seconds: float = 300.0
    market_data_action_cooldown_seconds: float = 1.0
    market_news_action_cooldown_seconds: float = 1.0
    unknown_theme_action_cooldown_seconds: float = 300.0
    geopolitical_action_cooldown_seconds: float = 300.0
    marketaux_alpha_action_cooldown_seconds: float = 60.0
    news_subscription_mesh_action_cooldown_seconds: float = 60.0
    cross_provider_event_roles_action_cooldown_seconds: float = 60.0
    m236_news_learning_action_cooldown_seconds: float = 60.0
    m236_outcome_attachment_action_cooldown_seconds: float = 300.0
    macro_release_interpreter_action_cooldown_seconds: float = 60.0
    macro_event_calendar_action_cooldown_seconds: float = 3600.0
    ibkr_validation_interval_seconds: float = 1.0
    marketaux_alpha_validation_interval_seconds: float = 60.0
    external_validation_interval_seconds: float = 300.0
    continuous_market_data: bool = True
    continuous_market_news: bool = True
    continuous_geopolitical: bool = True
    provider_rate_limit_cooldown_seconds: float = 900.0
    market_data_seconds: int = 16
    market_data_fallback_after_seconds: float = 4.0
    ibkr_news_limit_per_symbol: int = 25
    external_limit: int = 50
    external_timespan: str = "3d"
    continuous_unknown_theme_discovery: bool = True
    unknown_theme_lookback_minutes: int = 240
    unknown_theme_limit: int = 500
    continuous_news_subscription_mesh: bool = True
    news_subscription_mesh_lookback_minutes: int = 240
    news_subscription_mesh_max_temp_symbols: int = 40
    news_subscription_mesh_backfill_symbol_budget: int = 25
    continuous_cross_provider_event_roles: bool = True
    continuous_m236_news_learning: bool = True
    continuous_m236_outcome_attachment: bool = True
    continuous_macro_release_interpreter: bool = True
    continuous_macro_event_calendar: bool = True
    m236_outcome_attachment_max_records: int = 10
    m236_outcome_attachment_max_symbols: int = 4
    cross_provider_event_roles_lookback_minutes: int = 240
    rebuild_dashboard: bool = False
    dashboard_profile_path: Path | None = None
    live_json_output_path: Path | None = None


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _parse_dt(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        text = f"{text}T00:00:00+00:00"
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _local_time_text(value: Any, default: str = "-") -> str:
    parsed = _parse_dt(value)
    if parsed is None:
        return default
    local = parsed.astimezone()
    zone = local.tzname() or "local"
    return f"{local.strftime('%Y-%m-%d %H:%M:%S')} {zone} ({local.strftime('%z')})"


def _today() -> str:
    return datetime.now(UTC).date().isoformat()


def _start_date(value: Any, *, default_days: int = 3) -> str:
    parsed = _parse_dt(value)
    if parsed is None:
        parsed = datetime.now(UTC) - timedelta(days=default_days)
    return parsed.date().isoformat()


def _read_json(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        return {}
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _profile_path(value: Any, *, base: Path | None = None) -> Path | None:
    text = str(value or "").strip()
    if not text:
        return None
    path = Path(text).expanduser()
    if not path.is_absolute() and base is not None:
        path = base / path
    return path


def _dashboard_account_snapshot_path(dashboard_profile_path: Path | None) -> Path | None:
    if dashboard_profile_path is None:
        return None
    profile_path = dashboard_profile_path.expanduser()
    profile = _read_json(profile_path)
    paths = profile.get("paths") if isinstance(profile.get("paths"), dict) else {}
    return _profile_path(paths.get("account_snapshot_json"), base=profile_path.parent)


def _csv_symbols(values: Any) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        symbol = str(value or "").strip().upper()
        if symbol and symbol not in seen:
            seen.add(symbol)
            out.append(symbol)
    return out


def _market_news_symbols_from_mesh(settings: Any, config: SourceOnlineSupervisorConfig) -> list[str]:
    base = _csv_symbols(getattr(settings, "all_news_symbols", getattr(settings, "watch_symbols", ())))
    mesh = _read_json(config.output_dir / "m235_news_subscription_mesh.json")
    temp_symbols = _csv_symbols(((mesh.get("broadtape_temp_subscription_queue") or {}).get("symbols") or []))
    core = mesh.get("core_universe") or {}
    anchors = _csv_symbols(core.get("core_anchors") or [])
    core_live = _csv_symbols(core.get("core_live_symbols") or [])
    ordered = _csv_symbols([*base, *temp_symbols, *anchors, *core_live])
    budget = int(config.news_subscription_mesh_backfill_symbol_budget or 0)
    if budget > 0:
        return ordered[:budget]
    return ordered


def _write_json(path: Path, payload: dict[str, Any]) -> Path:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return resolved


def _alpha_vantage_disabled(config: SourceOnlineSupervisorConfig) -> dict[str, Any] | None:
    state = _read_json(config.output_dir / "m236_alpha_vantage_disabled.json")
    if not state:
        return None
    if not state.get("disabled"):
        return None
    return state


def _write_alpha_vantage_disabled(config: SourceOnlineSupervisorConfig, *, reason: str, source_action: str) -> None:
    now = _iso_now()
    _write_json(
        config.output_dir / "m236_alpha_vantage_disabled.json",
        {
            "version": DISABLED_ALPHA_VANTAGE_STATE_VERSION,
            "disabled": True,
            "disabled_at": now,
            "disabled_at_local": _local_time_text(now),
            "reason": reason,
            "source_action": source_action,
            "reactivation": "manual: delete this file or set disabled=false after quota/plan recovers",
        },
    )


def _looks_like_alpha_limit_error(action: dict[str, Any]) -> str | None:
    if str(action.get("action_id") or "") not in {LEGACY_MARKETAUX_ALPHA_VALIDATION_ACTION_ID, "external_market_sync_alpha_vantage"}:
        return None
    text = json.dumps(action, ensure_ascii=False).lower()
    if "alpha_vantage" not in text:
        return None
    limit_markers = ("standard api call frequency", "rate limit", "api call volume", "thank you for using alpha vantage", "premium", "provider_notice", "note")
    if any(marker in text for marker in limit_markers):
        return "Alpha Vantage returned a provider notice/limit response during live validation or sync."
    return None


def _append_jsonl(path: Path | None, payload: dict[str, Any]) -> None:
    if path is None:
        return
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    with resolved.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")


def _tail(value: Any, limit: int = 3000) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[-limit:]


def _default_command_runner(command: list[str], cwd: Path, timeout_seconds: float) -> dict[str, Any]:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            [str(item) for item in command],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=float(timeout_seconds),
        )
        return {
            "status": "succeeded" if completed.returncode == 0 else "failed",
            "returncode": completed.returncode,
            "stdout_tail": _tail(completed.stdout),
            "stderr_tail": _tail(completed.stderr),
            "duration_seconds": round(time.monotonic() - started, 3),
            "error": None,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "status": "timeout",
            "returncode": None,
            "stdout_tail": _tail(exc.stdout),
            "stderr_tail": _tail(exc.stderr),
            "duration_seconds": round(time.monotonic() - started, 3),
            "error": f"timeout_after_{timeout_seconds}_seconds",
        }
    except Exception as exc:  # pragma: no cover - defensive execution audit
        return {
            "status": "failed",
            "returncode": None,
            "stdout_tail": "",
            "stderr_tail": "",
            "duration_seconds": round(time.monotonic() - started, 3),
            "error": f"{exc.__class__.__name__}: {str(exc)[:500]}",
        }


def _command_action(action_id: str, description: str, command: list[str], *, source_key: str, priority: int, live_ibkr: bool = False) -> dict[str, Any]:
    return {
        "action_id": action_id,
        "priority": int(priority),
        "source_key": source_key,
        "description": description,
        "live_ibkr": bool(live_ibkr),
        "hot_lane": action_id in {"source_live_validation_ibkr", "market_news_ibkr_catchup", "benzinga_external_signal_conversion"},
        "command": command,
        "status": "planned",
        "started_at": None,
        "finished_at": None,
        "duration_seconds": None,
        "returncode": None,
        "stdout_tail": "",
        "stderr_tail": "",
        "error": None,
    }


def _note_action(action_id: str, description: str, *, source_key: str, priority: int, status: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    action = _command_action(action_id, description, [], source_key=source_key, priority=priority)
    action["status"] = status
    action["details"] = details or {}
    return action


def _run_action(action: dict[str, Any], *, cwd: Path, runner: CommandRunner, timeout_seconds: float) -> dict[str, Any]:
    if action.get("status") != "planned" or not action.get("command"):
        return action
    action["started_at"] = _iso_now()
    action["started_at_local"] = _local_time_text(action["started_at"])
    action["status"] = "running"
    result = runner([str(item) for item in action.get("command") or []], cwd, timeout_seconds)
    action.update(result)
    action["finished_at"] = _iso_now()
    action["finished_at_local"] = _local_time_text(action["finished_at"])
    return action


def _pid_is_running(pid: Any) -> bool:
    try:
        value = int(pid)
    except (TypeError, ValueError):
        return False
    if value <= 0:
        return False
    try:
        os.kill(value, 0)
    except OSError:
        return False
    return True


def _run_detached_m236_outcome_attachment(action: dict[str, Any], *, cwd: Path, config: SourceOnlineSupervisorConfig) -> dict[str, Any]:
    if action.get("status") != "planned" or not action.get("command"):
        return action
    lock_path = (config.output_dir / "m236_outcome_attachment_detached.lock").expanduser().resolve()
    existing = _read_json(lock_path)
    existing_pid = existing.get("pid")
    if _pid_is_running(existing_pid):
        now = _iso_now()
        action.update(
            {
                "status": "detached_already_running",
                "started_at": now,
                "started_at_local": _local_time_text(now),
                "finished_at": now,
                "finished_at_local": _local_time_text(now),
                "duration_seconds": 0.0,
                "returncode": None,
                "stdout_tail": "",
                "stderr_tail": "",
                "error": None,
                "details": {
                    "detached": True,
                    "existing_pid": int(existing_pid),
                    "lock_path": str(lock_path),
                    "report_json_path": str(config.output_dir / "m236_outcome_attachment_report.json"),
                    "policy": "Heavy M236 outcome attachment is allowed to keep running without blocking the 1-second source supervisor loop.",
                },
            }
        )
        return action
    if lock_path.exists():
        try:
            lock_path.unlink()
        except OSError:
            pass
    log_dir = (cwd / "logs" / "m236_outcome_attachment_detached").resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    started_at = _iso_now()
    stamp = started_at.replace(":", "").replace("+", "Z")
    stdout_path = log_dir / f"m236_outcome_attachment_{stamp}.out.log"
    stderr_path = log_dir / f"m236_outcome_attachment_{stamp}.err.log"
    stdout_handle = stdout_path.open("a", encoding="utf-8")
    stderr_handle = stderr_path.open("a", encoding="utf-8")
    creationflags = 0
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        creationflags |= subprocess.CREATE_NO_WINDOW
    if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
        creationflags |= subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        proc = subprocess.Popen(
            [str(item) for item in action.get("command") or []],
            cwd=str(cwd),
            stdout=stdout_handle,
            stderr=stderr_handle,
            text=True,
            creationflags=creationflags,
        )
    except Exception as exc:  # pragma: no cover - defensive process spawn audit
        stdout_handle.close()
        stderr_handle.close()
        action.update(
            {
                "status": "failed",
                "started_at": started_at,
                "started_at_local": _local_time_text(started_at),
                "finished_at": _iso_now(),
                "finished_at_local": _local_time_text(_iso_now()),
                "duration_seconds": 0.0,
                "returncode": None,
                "stdout_tail": "",
                "stderr_tail": "",
                "error": f"{exc.__class__.__name__}: {str(exc)[:500]}",
            }
        )
        return action
    stdout_handle.close()
    stderr_handle.close()
    _write_json(
        lock_path,
        {
            "lock_version": "detached-m236-outcome-attachment-v1",
            "pid": proc.pid,
            "started_at": started_at,
            "started_at_local": _local_time_text(started_at),
            "command": [str(item) for item in action.get("command") or []],
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "report_json_path": str(config.output_dir / "m236_outcome_attachment_report.json"),
            "safety": {
                "broker_orders_submitted": False,
                "transmit_enabled": False,
                "source_supervisor_waits_for_completion": False,
            },
        },
    )
    finished_at = _iso_now()
    action.update(
        {
            "status": "dispatched_detached",
            "started_at": started_at,
            "started_at_local": _local_time_text(started_at),
            "finished_at": finished_at,
            "finished_at_local": _local_time_text(finished_at),
            "duration_seconds": 0.0,
            "returncode": None,
            "stdout_tail": "",
            "stderr_tail": "",
            "error": None,
            "details": {
                "detached": True,
                "pid": proc.pid,
                "lock_path": str(lock_path),
                "stdout_path": str(stdout_path),
                "stderr_path": str(stderr_path),
                "report_json_path": str(config.output_dir / "m236_outcome_attachment_report.json"),
                "policy": "Detached heavy batch; source supervisor remains online and monitors future artifact freshness.",
            },
        }
    )
    return action


def _source_rows(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(row.get("source_key") or ""): row
        for row in report.get("sources") or []
        if isinstance(row, dict)
    }


def _provider_rows(live_validation: dict[str, Any], source_key: str) -> list[dict[str, Any]]:
    return [
        row
        for row in live_validation.get("providers") or []
        if isinstance(row, dict) and str(row.get("source_key") or "") == source_key
    ]


def _has_error_text(row: dict[str, Any], pattern: str) -> bool:
    needle = pattern.lower()
    return any(needle in str(error).lower() for error in row.get("errors") or [])


def _cooldown_aliases(action_id: str) -> tuple[str, ...]:
    if action_id == MARKETAUX_VALIDATION_ACTION_ID:
        return (MARKETAUX_VALIDATION_ACTION_ID, LEGACY_MARKETAUX_ALPHA_VALIDATION_ACTION_ID)
    if action_id == LEGACY_MARKETAUX_ALPHA_VALIDATION_ACTION_ID:
        return (LEGACY_MARKETAUX_ALPHA_VALIDATION_ACTION_ID, MARKETAUX_VALIDATION_ACTION_ID)
    return (action_id,)


def _cooldown_active(state: dict[str, Any], action_id: str, now_dt: datetime) -> tuple[bool, str | None]:
    next_by_action = state.get("next_eligible_by_action") or {}
    latest_next_dt: datetime | None = None
    for candidate_id in _cooldown_aliases(action_id):
        next_dt = _parse_dt(next_by_action.get(candidate_id))
        if next_dt is None:
            continue
        if latest_next_dt is None or next_dt > latest_next_dt:
            latest_next_dt = next_dt
    if latest_next_dt is not None and now_dt < latest_next_dt:
        return True, latest_next_dt.isoformat()
    return False, latest_next_dt.isoformat() if latest_next_dt else None


def _mark_cooldown(state: dict[str, Any], action_id: str, *, now_dt: datetime, seconds: float) -> None:
    next_by_action = dict(state.get("next_eligible_by_action") or {})
    next_by_action[action_id] = (now_dt + timedelta(seconds=float(seconds))).isoformat()
    state["next_eligible_by_action"] = next_by_action


def _is_due(state: dict[str, Any], action_id: str, now_dt: datetime) -> bool:
    active, _next_at = _cooldown_active(state, action_id, now_dt)
    return not active


def _cooldown_seconds_for_action(config: SourceOnlineSupervisorConfig, action_id: str, source_key: str) -> float:
    if action_id == "source_live_validation_ibkr":
        return config.ibkr_validation_interval_seconds
    if action_id in {MARKETAUX_VALIDATION_ACTION_ID, LEGACY_MARKETAUX_ALPHA_VALIDATION_ACTION_ID}:
        return config.marketaux_alpha_validation_interval_seconds
    if source_key == "market_data":
        return config.market_data_action_cooldown_seconds
    if source_key == "market_news" or action_id == "benzinga_external_signal_conversion":
        return config.market_news_action_cooldown_seconds
    if action_id == "unknown_theme_discovery":
        return config.unknown_theme_action_cooldown_seconds
    if action_id == "news_subscription_mesh":
        return config.news_subscription_mesh_action_cooldown_seconds
    if action_id == "cross_provider_event_roles":
        return config.cross_provider_event_roles_action_cooldown_seconds
    if action_id == "m236_news_learning":
        return config.m236_news_learning_action_cooldown_seconds
    if action_id == "m236_outcome_attachment":
        return config.m236_outcome_attachment_action_cooldown_seconds
    if action_id == "macro_release_interpreter":
        return config.macro_release_interpreter_action_cooldown_seconds
    if action_id == "macro_event_calendar":
        return config.macro_event_calendar_action_cooldown_seconds
    if action_id in {"external_market_sync_marketaux", "external_market_sync_alpha_vantage"}:
        return config.marketaux_alpha_action_cooldown_seconds
    if source_key == "geopolitical_signals":
        return config.geopolitical_action_cooldown_seconds
    return config.action_cooldown_seconds


def _source_status_from_provider_rows(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "not_checked"
    statuses = {str(row.get("status") or "unknown") for row in rows}
    if statuses & {"ok", "ok_with_transient_warnings", "cached_ok_after_validation_error"}:
        return "ok"
    if statuses <= {"no_rows"}:
        return "no_rows"
    if statuses <= {"error", "unreachable"}:
        return "error"
    return "degraded"


def _provider_update_should_preserve_previous(update_row: dict[str, Any], previous_row: dict[str, Any] | None) -> bool:
    if not previous_row:
        return False
    provider = str(update_row.get("provider") or "")
    if provider not in {"marketaux", "alpha_vantage"}:
        return False
    update_status = str(update_row.get("status") or "")
    previous_status = str(previous_row.get("status") or "")
    if update_status not in {"error", "unreachable"}:
        return False
    return previous_status in {"ok", "ok_with_transient_warnings", "cached_ok_after_validation_error", "stale_response"}


def _preserved_provider_row(previous_row: dict[str, Any], update_row: dict[str, Any]) -> dict[str, Any]:
    row = dict(previous_row)
    row["status"] = "cached_ok_after_validation_error"
    row["reachable"] = bool(previous_row.get("reachable"))
    row["preserved_from_previous_validation"] = True
    row["last_validation_status"] = update_row.get("status")
    row["last_validation_errors"] = update_row.get("errors") or []
    row["last_validation_attempt_count"] = update_row.get("attempt_count")
    row["last_validation_checked_at"] = update_row.get("validated_at")
    return row


def _overall_live_validation_status(source_status: dict[str, str]) -> str:
    values = set(source_status.values())
    if not values:
        return "not_checked"
    if values <= {"ok"}:
        return "green"
    if "error" in values and not ("ok" in values or "degraded" in values or "no_rows" in values):
        return "red"
    return "yellow"


def _merge_live_validation_payloads(base: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    provider_by_name: dict[str, dict[str, Any]] = {
        str(row.get("provider") or ""): dict(row)
        for row in base.get("providers") or []
        if isinstance(row, dict) and row.get("provider") and str(row.get("provider") or "") != "gdelt"
    }
    for row in update.get("providers") or []:
        if isinstance(row, dict) and row.get("provider") and str(row.get("provider") or "") != "gdelt":
            provider = str(row.get("provider"))
            update_row = dict(row)
            update_row.setdefault("validated_at", update.get("generated_at") or _iso_now())
            previous_row = provider_by_name.get(provider)
            if _provider_update_should_preserve_previous(update_row, previous_row):
                provider_by_name[provider] = _preserved_provider_row(previous_row or {}, update_row)
            else:
                provider_by_name[provider] = update_row
    providers = list(provider_by_name.values())
    source_keys = sorted({str(row.get("source_key") or "") for row in providers if row.get("source_key")})
    source_status = {
        source_key: _source_status_from_provider_rows([row for row in providers if str(row.get("source_key") or "") == source_key])
        for source_key in source_keys
    }
    merged = dict(base)
    merged.update(
        {
            "source_live_validation_version": update.get("source_live_validation_version") or base.get("source_live_validation_version"),
            "generated_at": update.get("generated_at") or _iso_now(),
            "overall_status": _overall_live_validation_status(source_status),
            "source_status": source_status,
            "providers": providers,
            "provider_status_counts": dict(Counter(str(row.get("status") or "unknown") for row in providers)),
            "partial_merge": True,
            "partial_merge_updated_at": _iso_now(),
        }
    )
    safety = dict(base.get("safety") or {})
    safety.update(update.get("safety") or {})
    merged["safety"] = safety
    return merged


def _merge_live_validation_file(target_path: Path, update_path: Path) -> dict[str, Any]:
    base = _read_json(target_path)
    update = _read_json(update_path)
    if not update:
        return base
    merged = _merge_live_validation_payloads(base, update)
    _write_json(target_path, merged)
    return merged


def _make_skipped_cooldown_action(action_id: str, description: str, *, source_key: str, priority: int, next_at: str | None) -> dict[str, Any]:
    return _note_action(
        action_id,
        description,
        source_key=source_key,
        priority=priority,
        status="skipped_cooldown",
        details={"next_eligible_at": next_at, "next_eligible_at_local": _local_time_text(next_at)},
    )


def _build_validation_commands(config: SourceOnlineSupervisorConfig, *, state: dict[str, Any], now_dt: datetime) -> list[dict[str, Any]]:
    py = sys.executable or "python"
    actions: list[dict[str, Any]] = []
    ibkr_update = config.output_dir / "m163_source_live_validation_ibkr.json"
    ibkr_md = config.output_dir / "m163_source_live_validation_ibkr.md"
    marketaux_update = config.output_dir / "m165_source_live_validation_marketaux.json"
    marketaux_md = config.output_dir / "m165_source_live_validation_marketaux.md"
    active, next_at = _cooldown_active(state, "source_live_validation_ibkr", now_dt)
    if active:
        actions.append(
            _make_skipped_cooldown_action(
                "source_live_validation_ibkr",
                "Skip short-interval IBKR/Benzinga validation until its cadence allows another probe.",
                source_key="control",
                priority=1,
                next_at=next_at,
            )
        )
    else:
        action = _command_action(
            "source_live_validation_ibkr",
            "Validate IBKR/Benzinga market-news provider on the short cadence.",
            [
                py,
                "main.py",
                "source-live-validation",
                "--profile-json",
                str(config.live_validation_profile_path),
                "--skip-marketaux",
                "--skip-alpha-vantage",
                "--json-output-path",
                str(ibkr_update),
                "--output",
                str(ibkr_md),
            ],
            source_key="control",
            priority=1,
            live_ibkr=True,
        )
        action["details"] = {"merge_target_json": str(config.live_validation_json_path), "partial_json": str(ibkr_update)}
        actions.append(action)
    active, next_at = _cooldown_active(state, MARKETAUX_VALIDATION_ACTION_ID, now_dt)
    if active:
        actions.append(
            _make_skipped_cooldown_action(
                MARKETAUX_VALIDATION_ACTION_ID,
                "Skip Marketaux validation until the faster external-news cadence allows another probe.",
                source_key="control",
                priority=3,
                next_at=next_at,
            )
        )
    else:
        alpha_disabled = _alpha_vantage_disabled(config)
        validation_command = [
            py,
            "main.py",
            "source-live-validation",
            "--profile-json",
            str(config.live_validation_profile_path),
            "--skip-ibkr",
            "--skip-alpha-vantage",
            "--json-output-path",
            str(marketaux_update),
            "--output",
            str(marketaux_md),
        ]
        action = _command_action(
            MARKETAUX_VALIDATION_ACTION_ID,
            "Validate Marketaux on the faster external-news cadence.",
            validation_command,
            source_key="control",
            priority=3,
        )
        action["details"] = {
            "merge_target_json": str(config.live_validation_json_path),
            "partial_json": str(marketaux_update),
            "legacy_action_id": LEGACY_MARKETAUX_ALPHA_VALIDATION_ACTION_ID,
            "alpha_vantage_disabled": bool(alpha_disabled),
            "alpha_vantage_disabled_reason": (alpha_disabled or {}).get("reason"),
        }
        actions.append(action)
    return actions


def _build_diagnosis_commands(config: SourceOnlineSupervisorConfig) -> list[dict[str, Any]]:
    py = sys.executable or "python"
    return [
        _command_action(
            "evidence_freshness_rebuild_pre",
            "Rebuild Evidence Freshness before Source Doctor diagnosis.",
            [
                py,
                "main.py",
                "evidence-freshness-desk",
                "--profile-json",
                str(config.freshness_profile_path),
                "--json-output-path",
                str(config.freshness_report_path),
                "--output",
                str(config.freshness_report_path.with_suffix(".md")),
            ],
            source_key="control",
            priority=3,
        ),
        _command_action(
            "source_ingest_health_pre",
            "Run Source Doctor after provider validation and freshness rebuild.",
            [
                py,
                "main.py",
                "source-ingest-health",
                "--profile-json",
                str(config.source_ingest_health_profile_path),
                "--live-validation-json",
                str(config.live_validation_json_path),
                "--json-output-path",
                str(config.source_ingest_health_json_path),
                "--output",
                str(config.source_ingest_health_markdown_path),
            ],
            source_key="control",
            priority=4,
        ),
    ]


def _build_market_data_source_actions(
    *,
    config: SourceOnlineSupervisorConfig,
    state: dict[str, Any],
    rows: dict[str, dict[str, Any]],
    now_dt: datetime,
    py: str,
) -> list[dict[str, Any]]:
    market_data = rows.get("market_data") or {}
    market_data_due = config.continuous_market_data and _is_due(state, "market_data_fresh_tick", now_dt)
    if not market_data_due and market_data.get("health_status") == "healthy":
        return []
    account_snapshot_path = _dashboard_account_snapshot_path(config.dashboard_profile_path)
    market_data_command = [
        py,
        "main.py",
        "market-data-desk",
        "--seconds",
        str(config.market_data_seconds),
        "--refresh-seconds",
        "1",
        "--market-data-type",
        "1",
        "--fallback-after-seconds",
        f"{config.market_data_fallback_after_seconds:g}",
    ]
    if account_snapshot_path is not None:
        market_data_command.extend(["--account-snapshot-json", str(account_snapshot_path)])
    market_data_command.extend(
        [
            "--output",
            str(config.output_dir / "m153_market_data_desk_refresh.md"),
            "--json-output-path",
            str(config.output_dir / "m153_market_data_desk_refresh.json"),
        ]
    )
    return [
        _command_action(
            "market_data_fresh_tick",
            "Refresh read-only Market Data Desk because market_data is due or not healthy.",
            market_data_command,
            source_key="market_data",
            priority=10,
            live_ibkr=True,
        )
    ]


def _market_news_ibkr_catchup_action(
    *,
    config: SourceOnlineSupervisorConfig,
    settings: Any,
    start: str,
    today: str,
    py: str,
) -> dict[str, Any]:
    selected_symbols = _market_news_symbols_from_mesh(settings, config)
    effective_limit_per_symbol = min(int(config.ibkr_news_limit_per_symbol), 10)
    action = _command_action(
        "market_news_ibkr_catchup",
        "Run short-cadence IBKR/Benzinga catch-up because market_news is due or lagging while provider is live.",
        [
            py,
            "main.py",
            "ibkr-news-backfill",
            "--symbols",
            ",".join(selected_symbols),
            "--providers",
            ",".join(getattr(settings, "news_provider_codes", ("AUTO",))),
            "--start",
            start,
            "--end",
            today,
            "--limit-per-symbol",
            str(effective_limit_per_symbol),
            "--fetch-article-body",
            "--max-pages-per-symbol",
            "1",
        ],
        source_key="market_news",
        priority=20,
        live_ibkr=True,
    )
    action["details"] = {
        "symbol_count": len(selected_symbols),
        "symbol_budget": int(config.news_subscription_mesh_backfill_symbol_budget or 0),
        "limit_per_symbol": effective_limit_per_symbol,
        "uses_news_subscription_mesh": True,
        "sample_symbols": selected_symbols[:20],
    }
    return action


def _build_market_news_mapping_actions(
    *,
    config: SourceOnlineSupervisorConfig,
    state: dict[str, Any],
    now_dt: datetime,
    start: str,
    today: str,
    market_news_unhealthy: bool,
    py: str,
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    if config.continuous_geopolitical or market_news_unhealthy:
        actions.append(
            _command_action(
                "benzinga_external_signal_conversion",
                "Convert fresh IBKR/Benzinga headlines into geopolitical/external signal rows when relevant.",
                [
                    py,
                    "main.py",
                    "benzinga-external-signals-build",
                    "--start",
                    start,
                    "--end",
                    today,
                    "--limit",
                    "10000",
                ],
                source_key="geopolitical_signals",
                priority=30,
            )
        )
    if config.continuous_unknown_theme_discovery and _is_due(state, "unknown_theme_discovery", now_dt):
        actions.append(
            _command_action(
                "unknown_theme_discovery",
                "Queue low-confidence BroadTape/news asset mappings for unknown-theme discovery.",
                [
                    py,
                    "main.py",
                    "unknown-theme-discovery",
                    "--lookback-minutes",
                    str(config.unknown_theme_lookback_minutes),
                    "--limit",
                    str(config.unknown_theme_limit),
                    "--output",
                    str(config.output_dir / "m234_unknown_theme_discovery.md"),
                    "--json-output-path",
                    str(config.output_dir / "m234_unknown_theme_discovery.json"),
                ],
                source_key="market_news",
                priority=35,
            )
        )
    if config.continuous_news_subscription_mesh and _is_due(state, "news_subscription_mesh", now_dt):
        actions.append(
            _command_action(
                "news_subscription_mesh",
                "Build core universe plus BroadTape temporary subscription planner and Dow Jones geopolitical confirmation candidates.",
                [
                    py,
                    "main.py",
                    "news-subscription-mesh",
                    "--profile-json",
                    "config/news_subscription_mesh_profile_v1.json",
                    "--lookback-minutes",
                    str(config.news_subscription_mesh_lookback_minutes),
                    "--max-temp-symbols",
                    str(config.news_subscription_mesh_max_temp_symbols),
                    "--output",
                    str(config.output_dir / "m235_news_subscription_mesh.md"),
                    "--json-output-path",
                    str(config.output_dir / "m235_news_subscription_mesh.json"),
                ],
                source_key="market_news",
                priority=36,
            )
        )
    if config.continuous_cross_provider_event_roles and _is_due(state, "cross_provider_event_roles", now_dt):
        actions.append(
            _command_action(
                "cross_provider_event_roles",
                "Build M236 event-role/provider-agreement/conflict scorer evidence packet artifact.",
                [
                    py,
                    "main.py",
                    "cross-provider-event-roles",
                    "--profile-json",
                    "config/cross_provider_event_roles_profile_v1.json",
                    "--lookback-minutes",
                    str(config.cross_provider_event_roles_lookback_minutes),
                    "--output",
                    str(config.output_dir / "m236_cross_provider_event_roles.md"),
                    "--json-output-path",
                    str(config.output_dir / "m236_cross_provider_event_roles.json"),
                ],
                source_key="market_news",
                priority=37,
            )
        )
    return actions


def _build_market_news_learning_macro_actions(
    *,
    config: SourceOnlineSupervisorConfig,
    state: dict[str, Any],
    now_dt: datetime,
    py: str,
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    if config.continuous_m236_news_learning and _is_due(state, "m236_news_learning", now_dt):
        actions.append(
            _command_action(
                "m236_news_learning",
                "Build M236 source-quality learning report from cross-provider outcome records.",
                [
                    py,
                    "main.py",
                    "m236-news-learning",
                    "--profile-json",
                    "config/m236_news_learning_profile_v1.json",
                    "--output",
                    str(config.output_dir / "m236_news_learning_report.md"),
                    "--json-output-path",
                    str(config.output_dir / "m236_news_learning_report.json"),
                    "--provider-profile-output-path",
                    str(config.output_dir / "m236_provider_weight_profile.json"),
                ],
                source_key="market_news",
                priority=38,
            )
        )
    if config.continuous_m236_outcome_attachment and _is_due(state, "m236_outcome_attachment", now_dt):
        actions.append(
            _command_action(
                "m236_outcome_attachment",
                "Attach due M236 5M/15M/30M market outcomes with bounded read-only IBKR historical price fetches.",
                [
                    py,
                    "main.py",
                    "m236-outcome-attach",
                    "--profile-json",
                    "config/m236_outcome_attachment_profile_v1.json",
                    "--max-records",
                    str(config.m236_outcome_attachment_max_records),
                    "--max-symbols",
                    str(config.m236_outcome_attachment_max_symbols),
                    "--output",
                    str(config.output_dir / "m236_outcome_attachment_report.md"),
                    "--json-output-path",
                    str(config.output_dir / "m236_outcome_attachment_report.json"),
                ],
                source_key="market_news",
                priority=39,
            )
        )
    if config.continuous_macro_event_calendar and _is_due(state, "macro_event_calendar", now_dt):
        actions.append(
            _command_action(
                "macro_event_calendar",
                "Refresh upcoming critical macro/FOMC calendar for volatility-aware position management.",
                [
                    py,
                    "main.py",
                    "macro-event-calendar",
                    "--output",
                    str(config.output_dir / "m237_macro_event_calendar.md"),
                    "--json-output-path",
                    str(config.output_dir / "m237_macro_event_calendar.json"),
                ],
                source_key="economic_indicators",
                priority=40,
            )
        )
    if config.continuous_macro_release_interpreter and _is_due(state, "macro_release_interpreter", now_dt):
        actions.append(
            _command_action(
                "macro_release_interpreter",
                "Parse latest macro release headlines into surprise/evidence packet and attach immediate market reaction.",
                [
                    py,
                    "main.py",
                    "macro-release-interpreter",
                    "--lookback-minutes",
                    "240",
                    "--price-source",
                    "ibkr",
                    "--output",
                    str(config.output_dir / "m237_macro_release_interpreter.md"),
                    "--json-output-path",
                    str(config.output_dir / "m237_macro_release_interpreter.json"),
                ],
                source_key="economic_indicators",
                priority=41,
            )
        )
    return actions


def _build_market_news_source_actions(
    *,
    config: SourceOnlineSupervisorConfig,
    settings: Any,
    state: dict[str, Any],
    rows: dict[str, dict[str, Any]],
    live_source_status: dict[str, Any],
    now_dt: datetime,
    today: str,
    py: str,
) -> list[dict[str, Any]]:
    market_news = rows.get("market_news") or {}
    market_news_unhealthy = bool(market_news) and market_news.get("health_status") != "healthy"
    market_news_due = config.continuous_market_news and _is_due(state, "market_news_ibkr_catchup", now_dt)
    if not (market_news_unhealthy or market_news_due):
        return []
    if live_source_status.get("market_news") != "ok":
        return [
            _note_action(
                "market_news_provider_unhealthy",
                "Market news is due or unhealthy but provider validation is not ok; skip catch-up until provider recovers.",
                source_key="market_news",
                priority=20,
                status="skipped_provider_not_ok",
                details={"live_source_status": live_source_status.get("market_news")},
            )
        ]

    actions: list[dict[str, Any]] = []
    start = _start_date(market_news.get("latest_event_at"), default_days=3)
    actions.append(
        _market_news_ibkr_catchup_action(
            config=config,
            settings=settings,
            start=start,
            today=today,
            py=py,
        )
    )
    actions.extend(
        _build_market_news_mapping_actions(
            config=config,
            state=state,
            now_dt=now_dt,
            start=start,
            today=today,
            market_news_unhealthy=market_news_unhealthy,
            py=py,
        )
    )
    actions.extend(_build_market_news_learning_macro_actions(config=config, state=state, now_dt=now_dt, py=py))
    return actions


def _build_geopolitical_source_actions(
    *,
    config: SourceOnlineSupervisorConfig,
    settings: Any,
    live_validation: dict[str, Any],
    rows: dict[str, dict[str, Any]],
    state: dict[str, Any],
    now_dt: datetime,
    py: str,
) -> list[dict[str, Any]]:
    geopolitical = rows.get("geopolitical_signals") or {}
    geopolitical_unhealthy = bool(geopolitical) and geopolitical.get("health_status") != "healthy"
    geopolitical_due = config.continuous_geopolitical and _is_due(state, "external_market_sync_marketaux", now_dt)
    if not geopolitical_unhealthy and not geopolitical_due:
        return []

    actions: list[dict[str, Any]] = []
    providers = _provider_rows(live_validation, "geopolitical_signals")
    if not providers:
        actions.append(
            _command_action(
                "external_market_sync_all",
                "No provider-specific validation rows exist; run a conservative external-market sync.",
                [
                    py,
                    "main.py",
                    "external-market-sync",
                    "--source",
                    "all",
                    "--limit",
                    str(config.external_limit),
                    "--alpha-tickers",
                    ",".join(getattr(settings, "geopolitical_news_symbols", ())),
                ],
                source_key="geopolitical_signals",
                priority=40,
            )
        )
    for provider in providers:
        provider_name = str(provider.get("provider") or "")
        provider_status = str(provider.get("status") or "")
        if provider_name == "alpha_vantage" and _alpha_vantage_disabled(config):
            actions.append(
                _note_action(
                    "external_market_sync_alpha_vantage",
                    "Skip Alpha Vantage because M236 disabled it after a provider limit/notice response.",
                    source_key="geopolitical_signals",
                    priority=45,
                    status="skipped_provider_rate_limited",
                    details=_alpha_vantage_disabled(config) or {},
                )
            )
            continue
        if provider_name in {"marketaux", "alpha_vantage"} and (
            provider_status in {"stale_response", "no_rows", "degraded", "error"}
            or (geopolitical_due and provider_name in {"marketaux", "alpha_vantage"} and provider_status == "ok")
        ):
            command = [
                py,
                "main.py",
                "external-market-sync",
                "--source",
                provider_name,
                "--limit",
                str(config.external_limit),
                "--alpha-tickers",
                ",".join(getattr(settings, "geopolitical_news_symbols", ())),
            ]
            if provider_name == "marketaux":
                command.append("--marketaux-latest")
            if provider_name == "alpha_vantage":
                command.extend(["--alpha-topics", "financial_markets,economy_macro,economy_monetary,energy_transportation,technology"])
            actions.append(
                _command_action(
                    f"external_market_sync_{provider_name}",
                    f"Refresh external geopolitical provider `{provider_name}` because validation status is `{provider_status}`.",
                    command,
                    source_key="geopolitical_signals",
                    priority=45,
                )
            )
    return actions


def _build_post_source_actions(*, config: SourceOnlineSupervisorConfig, py: str) -> list[dict[str, Any]]:
    actions = [
        _command_action(
            "evidence_freshness_rebuild_post",
            "Rebuild Evidence Freshness after source actions.",
            [
                py,
                "main.py",
                "evidence-freshness-desk",
                "--profile-json",
                str(config.freshness_profile_path),
                "--json-output-path",
                str(config.freshness_report_path),
                "--output",
                str(config.freshness_report_path.with_suffix(".md")),
            ],
            source_key="control",
            priority=90,
        ),
        _command_action(
            "source_ingest_health_post",
            "Re-run Source Doctor after source actions and freshness rebuild.",
            [
                py,
                "main.py",
                "source-ingest-health",
                "--profile-json",
                str(config.source_ingest_health_profile_path),
                "--live-validation-json",
                str(config.live_validation_json_path),
                "--json-output-path",
                str(config.source_ingest_health_json_path),
                "--output",
                str(config.source_ingest_health_markdown_path),
            ],
            source_key="control",
            priority=95,
        ),
    ]
    if config.rebuild_dashboard and config.dashboard_profile_path is not None:
        actions.append(
            _command_action(
                "dashboard_rebuild",
                "Rebuild Trading Command Center after source supervisor actions.",
                [
                    py,
                    "main.py",
                    "trading-dashboard-html",
                    "--profile-json",
                    str(config.dashboard_profile_path),
                    "--json-output-path",
                    str(config.output_dir / "m162_trading_dashboard_snapshot.json"),
                    "--output",
                    str(config.output_dir / "trading_command_center.html"),
                    "--json",
                ],
                source_key="dashboard",
                priority=100,
            )
        )
    return actions


def _build_source_actions(
    *,
    config: SourceOnlineSupervisorConfig,
    settings: Any,
    live_validation: dict[str, Any],
    source_health: dict[str, Any],
    state: dict[str, Any],
    now_dt: datetime,
) -> list[dict[str, Any]]:
    py = sys.executable or "python"
    today = _today()
    rows = _source_rows(source_health)
    live_source_status = live_validation.get("source_status") or {}
    actions: list[dict[str, Any]] = []

    actions.extend(_build_market_data_source_actions(config=config, state=state, rows=rows, now_dt=now_dt, py=py))
    actions.extend(
        _build_market_news_source_actions(
            config=config,
            settings=settings,
            state=state,
            rows=rows,
            live_source_status=live_source_status,
            now_dt=now_dt,
            today=today,
            py=py,
        )
    )
    actions.extend(
        _build_geopolitical_source_actions(
            config=config,
            settings=settings,
            live_validation=live_validation,
            rows=rows,
            state=state,
            now_dt=now_dt,
            py=py,
        )
    )
    if actions:
        actions.extend(_build_post_source_actions(config=config, py=py))
    return sorted(actions, key=lambda item: int(item.get("priority") or 0))


def _apply_action_controls(actions: list[dict[str, Any]], *, config: SourceOnlineSupervisorConfig, state: dict[str, Any], now_dt: datetime) -> list[dict[str, Any]]:
    controlled: list[dict[str, Any]] = []
    for action in actions:
        action_id = str(action.get("action_id") or "")
        if action.get("live_ibkr") and not config.include_live_ibkr and action.get("status") == "planned":
            action["status"] = "skipped_live_ibkr_disabled"
        active, next_at = _cooldown_active(state, action_id, now_dt)
        if active and action.get("status") == "planned":
            action["status"] = "skipped_cooldown"
            action["details"] = {"next_eligible_at": next_at, "next_eligible_at_local": _local_time_text(next_at)}
        if not config.execute_actions and action.get("status") == "planned":
            action["status"] = "planned_dry_run"
        controlled.append(action)
    return controlled


def _status_from_actions(actions: list[dict[str, Any]], final_health: dict[str, Any]) -> str:
    statuses = {str(action.get("status") or "") for action in actions}
    if statuses & {"failed", "timeout"}:
        return "red"
    health_green = str(final_health.get("overall_status") or "").lower() in {"green"}
    non_cooldown_statuses = statuses - {"skipped_cooldown"}
    if health_green and non_cooldown_statuses <= {"succeeded", "dispatched_detached", "detached_already_running", ""}:
        return "green"
    if str(final_health.get("overall_status") or "").lower() in {"green"} and not (
        statuses & {"planned_dry_run", "skipped_provider_rate_limited", "skipped_cooldown", "skipped_provider_not_ok", "skipped_live_ibkr_disabled"}
    ):
        return "green"
    if str(final_health.get("overall_status") or "").lower() in {"green"} and not actions:
        return "green"
    if statuses & {"succeeded", "dispatched_detached", "detached_already_running", "planned_dry_run", "skipped_provider_rate_limited", "skipped_cooldown", "skipped_provider_not_ok"}:
        return "yellow"
    if str(final_health.get("overall_status") or "").lower() in {"green"}:
        return "green"
    return "watch"


def _state_template(started_at: str) -> dict[str, Any]:
    return {
        "source_online_supervisor_version": SOURCE_ONLINE_SUPERVISOR_VERSION,
        "started_at": started_at,
        "updated_at": started_at,
        "last_tick_index": 0,
        "next_eligible_by_action": {},
        "last_status": "initialized",
        "tick_summaries": [],
        "safety": {
            "read_only_provider_probe": True,
            "read_only_broker_market_data": True,
            "evidence_db_rows_may_be_written_when_execute_actions_true": True,
            "ibkr_order_submitted": False,
            "transmit_enabled": False,
            "scorer_rows_mutated": False,
            "real_portfolio_mutated": False,
            "secrets_written": False,
        },
    }


def run_source_online_supervisor(
    *,
    config: SourceOnlineSupervisorConfig,
    settings: Any,
    cwd: Path | None = None,
    run_id: str | None = None,
    command_runner: CommandRunner | None = None,
    now_sequence: list[str] | None = None,
) -> dict[str, Any]:
    cwd = (cwd or Path.cwd()).expanduser().resolve()
    runner = command_runner or _default_command_runner
    started_at = _iso_now()
    run_id = run_id or f"source_online_supervisor_{started_at.replace(':', '').replace('+', 'Z')}"
    state = _read_json(config.state_json_path) or _state_template(started_at)
    state["source_online_supervisor_version"] = SOURCE_ONLINE_SUPERVISOR_VERSION
    state["local_time_display_version"] = LOCAL_TIME_DISPLAY_VERSION
    state["started_at_local"] = _local_time_text(started_at)
    state.setdefault("next_eligible_by_action", {})
    tick_summaries: list[dict[str, Any]] = []
    tick_index = 0
    continuous = int(config.max_ticks) == 0
    while continuous or tick_index < int(config.max_ticks):
        tick_index += 1
        now_raw = now_sequence[tick_index - 1] if now_sequence and tick_index - 1 < len(now_sequence) else _iso_now()
        now_dt = _parse_dt(now_raw) or datetime.now(UTC)
        control_actions = _build_validation_commands(config, state=state, now_dt=now_dt)
        if not config.include_live_ibkr:
            for action in control_actions:
                if action.get("live_ibkr"):
                    action["status"] = "skipped_live_ibkr_disabled"
        for action in control_actions:
            if action.get("status") == "planned":
                _run_action(action, cwd=cwd, runner=runner, timeout_seconds=config.job_timeout_seconds)
                alpha_disable_reason = _looks_like_alpha_limit_error(action)
                if alpha_disable_reason:
                    _write_alpha_vantage_disabled(config, reason=alpha_disable_reason, source_action=str(action.get("action_id") or ""))
                    action.setdefault("details", {})
                    action["details"]["alpha_vantage_disabled_by_m236"] = True
                    action["details"]["alpha_vantage_disable_reason"] = alpha_disable_reason
                if action.get("status") == "succeeded" and str(action.get("action_id") or "").startswith("source_live_validation_"):
                    partial_path = Path(str((action.get("details") or {}).get("partial_json") or ""))
                    merged = _merge_live_validation_file(config.live_validation_json_path, partial_path)
                    action.setdefault("details", {})
                    action["details"]["merged_provider_count"] = len(merged.get("providers") or [])
                    _mark_cooldown(
                        state,
                        str(action.get("action_id")),
                        now_dt=now_dt,
                        seconds=_cooldown_seconds_for_action(config, str(action.get("action_id")), str(action.get("source_key") or "")),
                    )
        diagnosis_actions = _build_diagnosis_commands(config)
        for action in diagnosis_actions:
            _run_action(action, cwd=cwd, runner=runner, timeout_seconds=config.job_timeout_seconds)
        control_actions.extend(diagnosis_actions)

        live_validation = _read_json(config.live_validation_json_path)
        source_health = _read_json(config.source_ingest_health_json_path)
        source_actions = _build_source_actions(
            config=config,
            settings=settings,
            live_validation=live_validation,
            source_health=source_health,
            state=state,
            now_dt=now_dt,
        )
        source_actions = _apply_action_controls(source_actions, config=config, state=state, now_dt=now_dt)
        for action in source_actions:
            if action.get("status") == "planned":
                if str(action.get("action_id") or "") == "m236_outcome_attachment" and runner is _default_command_runner:
                    _run_detached_m236_outcome_attachment(action, cwd=cwd, config=config)
                else:
                    _run_action(action, cwd=cwd, runner=runner, timeout_seconds=config.job_timeout_seconds)
                alpha_disable_reason = _looks_like_alpha_limit_error(action)
                if alpha_disable_reason:
                    _write_alpha_vantage_disabled(config, reason=alpha_disable_reason, source_action=str(action.get("action_id") or ""))
                    action.setdefault("details", {})
                    action["details"]["alpha_vantage_disabled_by_m236"] = True
                    action["details"]["alpha_vantage_disable_reason"] = alpha_disable_reason
                if action.get("status") in {"succeeded", "dispatched_detached", "detached_already_running"}:
                    _mark_cooldown(
                        state,
                        str(action.get("action_id")),
                        now_dt=now_dt,
                        seconds=_cooldown_seconds_for_action(config, str(action.get("action_id")), str(action.get("source_key") or "")),
                    )

        final_health = _read_json(config.source_ingest_health_json_path)
        all_actions = control_actions + source_actions
        status_counts = Counter(str(action.get("status") or "unknown") for action in all_actions)
        hot_lane_actions = [
            action
            for action in all_actions
            if action.get("hot_lane") or str(action.get("action_id") or "") in {"source_live_validation_ibkr", "market_news_ibkr_catchup", "benzinga_external_signal_conversion"}
        ]
        hot_lane_status_counts = Counter(str(action.get("status") or "unknown") for action in hot_lane_actions)
        tick_status = _status_from_actions(source_actions, final_health)
        alpha_disabled_state = _alpha_vantage_disabled(config) or {"disabled": False}
        tick_summary = {
            "source_online_supervisor_version": SOURCE_ONLINE_SUPERVISOR_VERSION,
            "local_time_display_version": LOCAL_TIME_DISPLAY_VERSION,
            "run_id": run_id,
            "tick_index": tick_index,
            "checked_at": now_dt.isoformat(),
            "checked_at_local": _local_time_text(now_dt.isoformat()),
            "status": tick_status,
            "execute_actions": config.execute_actions,
            "action_status_counts": dict(status_counts),
            "hot_lane": {
                "enabled": True,
                "action_ids": [str(action.get("action_id") or "") for action in hot_lane_actions],
                "status_counts": dict(hot_lane_status_counts),
                "ibkr_benzinga_interval_seconds": config.market_news_action_cooldown_seconds,
                "validation_interval_seconds": config.ibkr_validation_interval_seconds,
                "cooldown_skip_is_healthy_when_ingest_green": True,
            },
            "live_validation_status": live_validation.get("overall_status"),
            "live_validation_source_status": live_validation.get("source_status") or {},
            "source_ingest_health_status": final_health.get("overall_status"),
            "online_problem_source_keys": final_health.get("online_problem_source_keys") or [],
            "monitoring_only_online_source_keys": final_health.get("monitoring_only_online_source_keys") or [],
            "online_gap_source_keys": final_health.get("online_gap_source_keys") or [],
            "degraded_source_keys": final_health.get("degraded_source_keys") or [],
            "root_cause_counts": final_health.get("root_cause_counts") or {},
            "alpha_vantage_disabled": alpha_disabled_state,
            "actions": all_actions,
        }
        tick_summaries.append(tick_summary)
        _append_jsonl(config.tick_log_jsonl_path, tick_summary)
        state["updated_at"] = _iso_now()
        state["last_tick_index"] = tick_index
        state["last_status"] = tick_status
        state["last_live_validation_status"] = live_validation.get("overall_status")
        state["last_source_ingest_health_status"] = final_health.get("overall_status")
        state["last_online_problem_source_keys"] = final_health.get("online_problem_source_keys") or []
        state["last_monitoring_only_online_source_keys"] = final_health.get("monitoring_only_online_source_keys") or []
        state["tick_summaries"] = (state.get("tick_summaries") or [])[-20:] + [tick_summary]
        _write_json(config.state_json_path, state)
        if config.live_json_output_path is not None:
            completed_at = _iso_now()
            _write_json(
                config.live_json_output_path,
                {
                    "source_online_supervisor_version": SOURCE_ONLINE_SUPERVISOR_VERSION,
                    "local_time_display_version": LOCAL_TIME_DISPLAY_VERSION,
                    "run_id": run_id,
                    "started_at": started_at,
                    "started_at_local": _local_time_text(started_at),
                    "completed_at": completed_at,
                    "completed_at_local": _local_time_text(completed_at),
                    "executed_tick_count": tick_index,
                    "max_ticks": config.max_ticks,
                    "interval_seconds": config.interval_seconds,
                    "sleep_between_ticks": config.sleep_between_ticks,
                    "final_status": tick_summary.get("status"),
                    "final_action_status_counts": tick_summary.get("action_status_counts") or {},
                    "final_hot_lane": tick_summary.get("hot_lane") or {},
                    "final_live_validation_status": tick_summary.get("live_validation_status"),
                    "final_source_ingest_health_status": tick_summary.get("source_ingest_health_status"),
                    "final_online_problem_source_keys": tick_summary.get("online_problem_source_keys") or [],
                    "final_monitoring_only_online_source_keys": tick_summary.get("monitoring_only_online_source_keys") or [],
                    "final_online_gap_source_keys": tick_summary.get("online_gap_source_keys") or [],
                    "final_alpha_vantage_disabled": tick_summary.get("alpha_vantage_disabled") or {"disabled": False},
                    "state_json_path": str(config.state_json_path.expanduser().resolve()),
                    "tick_log_jsonl_path": str(config.tick_log_jsonl_path.expanduser().resolve()) if config.tick_log_jsonl_path else None,
                    "tick_summaries": tick_summaries[-20:],
                    "safety": state.get("safety") or {},
                },
            )
        if (continuous or tick_index < int(config.max_ticks)) and config.sleep_between_ticks and config.interval_seconds > 0:
            time.sleep(float(config.interval_seconds))

    final_summary = tick_summaries[-1] if tick_summaries else {}
    completed_at = _iso_now()
    return {
        "source_online_supervisor_version": SOURCE_ONLINE_SUPERVISOR_VERSION,
        "local_time_display_version": LOCAL_TIME_DISPLAY_VERSION,
        "run_id": run_id,
        "started_at": started_at,
        "started_at_local": _local_time_text(started_at),
        "completed_at": completed_at,
        "completed_at_local": _local_time_text(completed_at),
        "executed_tick_count": tick_index,
        "max_ticks": config.max_ticks,
        "interval_seconds": config.interval_seconds,
        "sleep_between_ticks": config.sleep_between_ticks,
        "final_status": final_summary.get("status"),
        "final_action_status_counts": final_summary.get("action_status_counts") or {},
        "final_hot_lane": final_summary.get("hot_lane") or {},
        "final_live_validation_status": final_summary.get("live_validation_status"),
        "final_source_ingest_health_status": final_summary.get("source_ingest_health_status"),
        "final_online_problem_source_keys": final_summary.get("online_problem_source_keys") or [],
        "final_monitoring_only_online_source_keys": final_summary.get("monitoring_only_online_source_keys") or [],
        "final_online_gap_source_keys": final_summary.get("online_gap_source_keys") or [],
        "final_alpha_vantage_disabled": final_summary.get("alpha_vantage_disabled") or {"disabled": False},
        "state_json_path": str(config.state_json_path.expanduser().resolve()),
        "tick_log_jsonl_path": str(config.tick_log_jsonl_path.expanduser().resolve()) if config.tick_log_jsonl_path else None,
        "tick_summaries": tick_summaries,
        "safety": state.get("safety") or {},
    }


def source_online_supervisor_markdown(result: dict[str, Any]) -> str:
    lines = [
        "# Source Online Supervisor",
        "",
        f"- Version: `{result.get('source_online_supervisor_version')}`",
        f"- Run ID: `{result.get('run_id')}`",
        f"- Started / completed local: `{result.get('started_at_local') or _local_time_text(result.get('started_at'))}` / `{result.get('completed_at_local') or _local_time_text(result.get('completed_at'))}`",
        f"- Final status: `{result.get('final_status')}`",
        f"- Executed ticks: `{result.get('executed_tick_count')}`",
        f"- Final live validation: `{result.get('final_live_validation_status')}`",
        f"- Final source ingest health: `{result.get('final_source_ingest_health_status')}`",
        f"- Final online problems: `{','.join(result.get('final_online_problem_source_keys') or []) or '-'}`",
        f"- Monitoring-only online gaps: `{','.join(result.get('final_monitoring_only_online_source_keys') or []) or '-'}`",
        f"- Alpha Vantage disabled: `{(result.get('final_alpha_vantage_disabled') or {}).get('disabled', False)}` reason `{(result.get('final_alpha_vantage_disabled') or {}).get('reason') or '-'}`",
        f"- Final action status counts: `{result.get('final_action_status_counts') or {}}`",
        "",
    ]
    for tick in result.get("tick_summaries") or []:
        lines.extend(
            [
                f"## Tick {tick.get('tick_index')}",
                "",
                f"- Status: `{tick.get('status')}`",
                f"- Checked local: `{tick.get('checked_at_local') or _local_time_text(tick.get('checked_at'))}`",
                f"- Checked raw UTC: `{tick.get('checked_at') or '-'}`",
                f"- Execute actions: `{tick.get('execute_actions')}`",
                f"- Live source status: `{tick.get('live_validation_source_status') or {}}`",
                f"- Alpha Vantage disabled: `{(tick.get('alpha_vantage_disabled') or {}).get('disabled', False)}` reason `{(tick.get('alpha_vantage_disabled') or {}).get('reason') or '-'}`",
                f"- Root causes: `{tick.get('root_cause_counts') or {}}`",
                "",
                "| Priority | Action | Source | Status | Next eligible local | Description |",
                "| ---: | --- | --- | --- | --- | --- |",
            ]
        )
        for action in tick.get("actions") or []:
            details = action.get("details") or {}
            lines.append(
                f"| {action.get('priority')} | `{action.get('action_id')}` | {action.get('source_key')} | "
                f"{action.get('status')} | `{details.get('next_eligible_at_local') or _local_time_text(details.get('next_eligible_at'))}` | {action.get('description')} |"
            )
        lines.append("")
    lines.extend(
        [
            "## Safety",
            "",
            "- Supervisor may refresh evidence rows only when `execute_actions=true`; it never submits broker orders, enables transmit, mutates scorer rows, or mutates a real portfolio.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_source_online_supervisor_json(output_path: Path, result: dict[str, Any]) -> Path:
    return _write_json(output_path, result)


def write_source_online_supervisor_markdown(output_path: Path, result: dict[str, Any]) -> Path:
    resolved = output_path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(source_online_supervisor_markdown(result), encoding="utf-8")
    return resolved
