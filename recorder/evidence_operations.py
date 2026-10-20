"""Evidence freshness, preflight, refresh queue, and watchdog operations.

Middle-level module for evidence-source operational health and refresh control.
Evidence packet construction remains in evidence_packet.py.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

CommandRunner = Callable[[list[str], Path, float], dict[str, Any]]


# --- merged from evidence_freshness.py ---
EVIDENCE_FRESHNESS_DESK_VERSION = "evidence-freshness-desk-v2-marketaux-crosscheck-no-alpha"
SCORER_EVIDENCE_GAP_GUARD_VERSION = "scorer-evidence-gap-guard-v1"


@dataclass(frozen=True)
class FreshnessPolicy:
    source_key: str
    label: str
    fresh_minutes: float
    lagging_minutes: float
    offline_minutes: float
    expected_update: str
    stale_weight: float


DEFAULT_POLICIES: dict[str, FreshnessPolicy] = {
    "market_news": FreshnessPolicy(
        "market_news",
        "Market / breaking news",
        fresh_minutes=60.0,
        lagging_minutes=240.0,
        offline_minutes=4320.0,
        expected_update="intraday",
        stale_weight=0.35,
    ),
    "geopolitical_signals": FreshnessPolicy(
        "geopolitical_signals",
        "Geopolitical / external signals",
        fresh_minutes=120.0,
        lagging_minutes=360.0,
        offline_minutes=4320.0,
        expected_update="intraday",
        stale_weight=0.35,
    ),
    "standing_regimes": FreshnessPolicy(
        "standing_regimes",
        "Standing market regimes",
        fresh_minutes=2880.0,
        lagging_minutes=10080.0,
        offline_minutes=43200.0,
        expected_update="multi-day confirmation",
        stale_weight=0.60,
    ),
    "economic_indicators": FreshnessPolicy(
        "economic_indicators",
        "Economic Indicator Desk",
        fresh_minutes=10080.0,
        lagging_minutes=43200.0,
        offline_minutes=129600.0,
        expected_update="release-calendar dependent",
        stale_weight=0.65,
    ),
    "market_data": FreshnessPolicy(
        "market_data",
        "Market Data Desk",
        fresh_minutes=2.0,
        lagging_minutes=10.0,
        offline_minutes=60.0,
        expected_update="real-time dashboard tick",
        stale_weight=0.20,
    ),
    "technical_indicators": FreshnessPolicy(
        "technical_indicators",
        "Technical indicators",
        fresh_minutes=2880.0,
        lagging_minutes=10080.0,
        offline_minutes=43200.0,
        expected_update="daily or intraday batch",
        stale_weight=0.55,
    ),
}


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def parse_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
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


def _latest_datetime(*values: Any) -> str | None:
    parsed = [item for item in (parse_datetime(value) for value in values) if item is not None]
    if not parsed:
        return None
    return max(parsed).isoformat()


def _age_minutes(value: Any, *, now: datetime) -> float | None:
    parsed = parse_datetime(value)
    if parsed is None:
        return None
    return round((now - parsed).total_seconds() / 60.0, 3)


def _status_from_age(age_minutes: float | None, policy: FreshnessPolicy) -> str:
    if age_minutes is None:
        return "offline"
    if age_minutes <= policy.fresh_minutes:
        return "fresh"
    if age_minutes <= policy.lagging_minutes:
        return "lagging"
    if age_minutes <= policy.offline_minutes:
        return "stale"
    return "offline"


def _scorer_usability(status: str, *, source_key: str) -> str:
    if status == "fresh":
        return "normal"
    if status == "lagging":
        return "downweight_latest_claims"
    if status == "stale":
        if source_key == "standing_regimes":
            return "use_as_background_only_unconfirmed"
        return "do_not_use_as_latest"
    return "requires_backfill_before_latest_scoring"


def _weight(status: str, policy: FreshnessPolicy) -> float:
    if status == "fresh":
        return 1.0
    if status == "lagging":
        return max(policy.stale_weight, 0.70)
    if status == "stale":
        return policy.stale_weight
    return 0.0


def _warning(source_key: str, status: str) -> str | None:
    if status in {"fresh", "lagging"}:
        return None
    if source_key == "market_news":
        return "Latest headlines are stale; scorer must not explain live market moves from old news."
    if source_key == "geopolitical_signals":
        return "Geopolitical overlay is stale; use market-data reaction but mark cause as unknown until backfill refreshes."
    if source_key == "standing_regimes":
        return "Standing regime may remain active, but severity/confidence should be treated as unconfirmed."
    if source_key == "economic_indicators":
        return "Economic desk is old; use only as slow-moving macro context until refreshed."
    if source_key == "market_data":
        return "Market Data Desk snapshot is stale; live dashboard decisions need a fresh tick."
    return "Evidence source is stale or offline."


def _remediation(source_key: str, status: str) -> list[str]:
    if status in {"fresh", "lagging"}:
        return []
    if source_key == "market_news":
        return ["run IBKR/Benzinga/Dow Jones news refresh and Marketaux crosscheck", "rebuild dashboard snapshot"]
    if source_key == "geopolitical_signals":
        return ["run Marketaux geopolitical refresh", "rebuild standing regimes and overlay packets"]
    if source_key == "standing_regimes":
        return ["rebuild standing regime episodes from refreshed external signals"]
    if source_key == "economic_indicators":
        return ["run global economic indicator backfill/update", "rebuild Economic Indicator Desk"]
    if source_key == "market_data":
        return ["run market-data-desk for a fresh 1-second dashboard tick"]
    return ["refresh this source before treating it as latest evidence"]


def _row(
    *,
    source_key: str,
    latest_event_at: str | None,
    latest_ingested_at: str | None,
    row_count: int | None,
    now: datetime,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    policy = DEFAULT_POLICIES[source_key]
    event_age = _age_minutes(latest_event_at, now=now)
    ingest_age = _age_minutes(latest_ingested_at, now=now)
    preferred_age = event_age if event_age is not None else ingest_age
    status = _status_from_age(preferred_age, policy)
    return {
        "source_key": source_key,
        "label": policy.label,
        "latest_event_at": latest_event_at,
        "latest_ingested_at": latest_ingested_at,
        "age_minutes": preferred_age,
        "ingestion_age_minutes": ingest_age,
        "status": status,
        "scorer_usability": _scorer_usability(status, source_key=source_key),
        "scorer_weight": round(_weight(status, policy), 3),
        "expected_update": policy.expected_update,
        "row_count": row_count,
        "warning": _warning(source_key, status),
        "remediation": _remediation(source_key, status),
        "details": details or {},
    }


def _apply_marketaux_market_news_overlay(rows: list[dict[str, Any]]) -> None:
    """Let fresh Marketaux external signals soften stale BZ/DJ/IBKR market-news gaps.

    This does not pretend that ticker-specific broker news is fresh. It marks the
    market-news row as crosschecked by a fresh external aggregator so live scorer
    packets can downweight only latest-headline claims instead of treating the
    whole news sector as blind/offline.
    """
    market_row = next((row for row in rows if row.get("source_key") == "market_news"), None)
    external_row = next((row for row in rows if row.get("source_key") == "geopolitical_signals"), None)
    if not market_row or not external_row:
        return
    if market_row.get("status") != "lagging":
        return
    external_age = external_row.get("age_minutes")
    if external_row.get("status") not in {"fresh", "lagging"}:
        return
    if external_age is None:
        return
    policy = DEFAULT_POLICIES["market_news"]
    if float(external_age) > policy.fresh_minutes:
        return

    details = dict(market_row.get("details") or {})
    details["marketaux_auxiliary_freshness"] = {
        "applied": True,
        "source_key": "geopolitical_signals",
        "latest_event_at": external_row.get("latest_event_at"),
        "latest_ingested_at": external_row.get("latest_ingested_at"),
        "age_minutes": external_age,
        "reason": "Marketaux external signals are fresh while broker/ticker news is lagging.",
    }
    market_row["details"] = details
    market_row["scorer_usability"] = "downweight_latest_claims_marketaux_crosscheck_available"
    market_row["scorer_weight"] = max(float(market_row.get("scorer_weight") or 0.0), 0.85)
    market_row["warning"] = None
    market_row["remediation"] = [
        "keep IBKR/Benzinga/Dow Jones catchup active",
        "allow Marketaux as temporary cross-provider headline freshness check",
    ]


def _max_columns(conn: sqlite3.Connection, table: str, columns: list[str]) -> tuple[dict[str, str | None], int | None]:
    try:
        row_count = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    except sqlite3.Error:
        return {}, None
    output: dict[str, str | None] = {}
    for column in columns:
        try:
            value = conn.execute(f"SELECT MAX({column}) FROM {table}").fetchone()[0]
        except sqlite3.Error:
            value = None
        output[column] = str(value) if value not in (None, "") else None
    return output, row_count


def _read_json(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        return None
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _economic_json_details(payload: dict[str, Any] | None) -> tuple[str | None, str | None, dict[str, Any]]:
    if not payload:
        return None, None, {}
    result = payload.get("result") if isinstance(payload.get("result"), dict) else payload
    cutoff_date = result.get("cutoff_date")
    latest_dates = []
    for item in result.get("indicators") or []:
        if isinstance(item, dict) and item.get("latest_date"):
            latest_dates.append(item.get("latest_date"))
    latest_event_at = _latest_datetime(cutoff_date, *latest_dates)
    details = {
        "desk_version": result.get("desk_version"),
        "cutoff_date": cutoff_date,
        "series_count": result.get("series_count"),
        "covered_series_count": result.get("covered_series_count"),
        "freshness_counts": result.get("freshness_counts") or {},
        "risk_bias": result.get("risk_bias"),
    }
    return latest_event_at, result.get("generated_at") or cutoff_date, details


def _market_data_json_details(payload: dict[str, Any] | None) -> tuple[str | None, str | None, dict[str, Any]]:
    if not payload:
        return None, None, {}
    result = payload.get("result") if isinstance(payload.get("result"), dict) else payload
    generated_at = result.get("generated_at")
    summary = result.get("summary") or {}
    details = {
        "version": result.get("market_data_desk_version") or result.get("desk_version"),
        "status": result.get("status") or summary.get("status"),
        "temperature": result.get("temperature") or {},
        "summary": summary,
    }
    return generated_at, generated_at, details


def build_evidence_freshness_report(
    *,
    database_path: Path | None,
    market_data_json_path: Path | None = None,
    economic_desk_json_path: Path | None = None,
    now: str | datetime | None = None,
) -> dict[str, Any]:
    now_dt = parse_datetime(now) if isinstance(now, str) else now
    if now_dt is None:
        now_dt = datetime.now(UTC)
    now_dt = now_dt.astimezone(UTC)
    rows: list[dict[str, Any]] = []
    errors: list[str] = []

    if database_path is not None and database_path.expanduser().resolve().exists():
        conn = sqlite3.connect(database_path.expanduser().resolve(), timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            values, count = _max_columns(conn, "news_articles", ["provider_published_at", "published_at", "received_at", "created_at"])
            rows.append(
                _row(
                    source_key="market_news",
                    latest_event_at=_latest_datetime(values.get("provider_published_at"), values.get("published_at"), values.get("received_at")),
                    latest_ingested_at=values.get("created_at"),
                    row_count=count,
                    now=now_dt,
                    details={"max_columns": values},
                )
            )
            values, count = _max_columns(conn, "external_market_signals", ["published_at", "created_at", "updated_at"])
            rows.append(
                _row(
                    source_key="geopolitical_signals",
                    latest_event_at=values.get("published_at"),
                    latest_ingested_at=_latest_datetime(values.get("created_at"), values.get("updated_at")),
                    row_count=count,
                    now=now_dt,
                    details={"max_columns": values},
                )
            )
            values, count = _max_columns(conn, "standing_market_regimes", ["last_seen_at", "created_at", "updated_at"])
            rows.append(
                _row(
                    source_key="standing_regimes",
                    latest_event_at=values.get("last_seen_at"),
                    latest_ingested_at=_latest_datetime(values.get("created_at"), values.get("updated_at")),
                    row_count=count,
                    now=now_dt,
                    details={"max_columns": values},
                )
            )
            values, count = _max_columns(conn, "technical_indicators", ["as_of_date", "created_at", "updated_at"])
            rows.append(
                _row(
                    source_key="technical_indicators",
                    latest_event_at=values.get("as_of_date"),
                    latest_ingested_at=_latest_datetime(values.get("created_at"), values.get("updated_at")),
                    row_count=count,
                    now=now_dt,
                    details={"max_columns": values},
                )
            )
            values, count = _max_columns(conn, "economic_indicator_observations", ["observation_date", "created_at", "updated_at"])
            db_economic_row = _row(
                source_key="economic_indicators",
                latest_event_at=values.get("observation_date"),
                latest_ingested_at=_latest_datetime(values.get("created_at"), values.get("updated_at")),
                row_count=count,
                now=now_dt,
                details={"max_columns": values, "source": "database"},
            )
        finally:
            conn.close()
    else:
        errors.append(f"database_missing:{database_path}")
        for source_key in ("market_news", "geopolitical_signals", "standing_regimes", "technical_indicators"):
            rows.append(
                _row(
                    source_key=source_key,
                    latest_event_at=None,
                    latest_ingested_at=None,
                    row_count=None,
                    now=now_dt,
                    details={"source": "database_missing"},
                )
            )
        db_economic_row = None

    economic_payload = _read_json(economic_desk_json_path)
    economic_event_at, economic_ingested_at, economic_details = _economic_json_details(economic_payload)
    if economic_event_at or economic_ingested_at:
        economic_row = _row(
            source_key="economic_indicators",
            latest_event_at=economic_event_at,
            latest_ingested_at=economic_ingested_at,
            row_count=economic_details.get("covered_series_count"),
            now=now_dt,
            details={**economic_details, "source": "economic_desk_json"},
        )
    else:
        economic_row = db_economic_row or _row(
            source_key="economic_indicators",
            latest_event_at=None,
            latest_ingested_at=None,
            row_count=None,
            now=now_dt,
            details={"source": "missing"},
        )
    rows.append(economic_row)

    market_payload = _read_json(market_data_json_path)
    market_event_at, market_ingested_at, market_details = _market_data_json_details(market_payload)
    rows.append(
        _row(
            source_key="market_data",
            latest_event_at=market_event_at,
            latest_ingested_at=market_ingested_at,
            row_count=(market_details.get("summary") or {}).get("row_count"),
            now=now_dt,
            details=market_details if market_details else {"source": "missing"},
        )
    )
    _apply_marketaux_market_news_overlay(rows)

    status_counts = {status: 0 for status in ("fresh", "lagging", "stale", "offline")}
    for item in rows:
        status_counts[str(item.get("status") or "offline")] = status_counts.get(str(item.get("status") or "offline"), 0) + 1
    stale_sources = [item for item in rows if item.get("status") in {"stale", "offline"}]
    lagging_sources = [item for item in rows if item.get("status") == "lagging"]
    if any(item.get("status") == "offline" for item in rows):
        overall_status = "red"
    elif stale_sources:
        overall_status = "yellow"
    elif lagging_sources:
        overall_status = "watch"
    else:
        overall_status = "green"

    scorer_weights = {item["source_key"]: item["scorer_weight"] for item in rows}
    warnings = [str(item["warning"]) for item in rows if item.get("warning")]
    return {
        "evidence_freshness_desk_version": EVIDENCE_FRESHNESS_DESK_VERSION,
        "scorer_evidence_gap_guard_version": SCORER_EVIDENCE_GAP_GUARD_VERSION,
        "generated_at": now_dt.isoformat(),
        "overall_status": overall_status,
        "status_counts": status_counts,
        "stale_source_keys": [str(item["source_key"]) for item in stale_sources],
        "lagging_source_keys": [str(item["source_key"]) for item in lagging_sources],
        "source_freshness_weights": scorer_weights,
        "evidence_gap_warning": bool(stale_sources),
        "scorer_guard": {
            "action": "block_latest_explanation" if any(item.get("status") == "offline" for item in rows) else ("downweight_stale_sources" if stale_sources else "normal"),
            "do_not_use_as_latest": [
                str(item["source_key"])
                for item in rows
                if item.get("scorer_usability") in {"do_not_use_as_latest", "requires_backfill_before_latest_scoring"}
            ],
            "background_only": [
                str(item["source_key"])
                for item in rows
                if item.get("scorer_usability") == "use_as_background_only_unconfirmed"
            ],
            "weights": scorer_weights,
            "warnings": warnings,
        },
        "sources": rows,
        "errors": errors,
        "safety": {
            "read_only": True,
            "scorer_rows_mutated": False,
            "orders_submitted": False,
        },
    }


def evidence_freshness_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Evidence Freshness Desk",
        "",
        f"- Version: `{report.get('evidence_freshness_desk_version')}`",
        f"- Generated at: `{report.get('generated_at')}`",
        f"- Overall status: `{report.get('overall_status')}`",
        f"- Evidence gap warning: `{report.get('evidence_gap_warning')}`",
        f"- Scorer guard action: `{(report.get('scorer_guard') or {}).get('action')}`",
        "",
        "## Source freshness",
        "",
        "| Source | Status | Age min | Latest event | Latest ingest | Scorer use | Weight |",
        "| --- | --- | ---: | --- | --- | --- | ---: |",
    ]
    for item in report.get("sources") or []:
        lines.append(
            f"| {item.get('label') or item.get('source_key')} | {item.get('status')} | {item.get('age_minutes')} "
            f"| {item.get('latest_event_at') or '-'} | {item.get('latest_ingested_at') or '-'} "
            f"| {item.get('scorer_usability')} | {item.get('scorer_weight')} |"
        )
    warnings = (report.get("scorer_guard") or {}).get("warnings") or []
    if warnings:
        lines.extend(["", "## Scorer evidence gap warnings", ""])
        lines.extend(f"- {item}" for item in warnings)
    lines.extend(
        [
            "",
            "## Safety",
            "",
            "- Read-only freshness report; no broker orders, portfolio mutations, or scorer-row mutation.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_evidence_freshness_json(output_path: Path, report: dict[str, Any]) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return output_path


def write_evidence_freshness_markdown(output_path: Path, report: dict[str, Any]) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(evidence_freshness_markdown(report), encoding="utf-8")
    return output_path


def evidence_gap_guard_from_packet(packet: dict[str, Any], *, now: str | datetime | None = None) -> dict[str, Any]:
    """Build a lightweight point-in-time guard for scorer evidence packets.

    This intentionally uses only the packet content already attached to the event. It does not look
    forward into current DB state, so replay packets keep the existing anti-leakage contract.
    """

    now_dt = parse_datetime(now) if isinstance(now, str) else now
    event = packet.get("event") or {}
    if now_dt is None:
        now_dt = parse_datetime(event.get("event_time_utc")) or datetime.now(UTC)
    now_dt = now_dt.astimezone(UTC)
    checks: list[dict[str, Any]] = []

    overlay = packet.get("overlay") or {}
    overlay_latest = None
    for item in overlay.get("top_signals") or []:
        if isinstance(item, dict):
            overlay_latest = _latest_datetime(overlay_latest, item.get("published_at"))
    checks.append(
        _row(
            source_key="geopolitical_signals",
            latest_event_at=overlay_latest,
            latest_ingested_at=None,
            row_count=overlay.get("matched_signal_count"),
            now=now_dt,
            details={"source": "packet_overlay"},
        )
    )

    macro_dates = [item.get("observation_date") for item in packet.get("macro_context") or [] if isinstance(item, dict)]
    checks.append(
        _row(
            source_key="economic_indicators",
            latest_event_at=_latest_datetime(*macro_dates),
            latest_ingested_at=None,
            row_count=len(macro_dates),
            now=now_dt,
            details={"source": "packet_macro_context"},
        )
    )

    tech_dates = [item.get("as_of_date") for item in packet.get("technical_context") or [] if isinstance(item, dict)]
    checks.append(
        _row(
            source_key="technical_indicators",
            latest_event_at=_latest_datetime(*tech_dates),
            latest_ingested_at=None,
            row_count=len(tech_dates),
            now=now_dt,
            details={"source": "packet_technical_context"},
        )
    )

    standing = packet.get("standing_regime_overlay") or {}
    regime_dates = [item.get("last_seen_at") for item in standing.get("active_regimes") or [] if isinstance(item, dict)]
    checks.append(
        _row(
            source_key="standing_regimes",
            latest_event_at=_latest_datetime(*regime_dates),
            latest_ingested_at=None,
            row_count=standing.get("matched_regime_count"),
            now=now_dt,
            details={"source": "packet_standing_regimes"},
        )
    )

    stale = [item for item in checks if item.get("status") in {"stale", "offline"}]
    return {
        "guard_version": SCORER_EVIDENCE_GAP_GUARD_VERSION,
        "generated_at": now_dt.isoformat(),
        "evidence_gap_warning": bool(stale),
        "action": "downweight_stale_sources" if stale else "normal",
        "do_not_use_as_latest": [
            str(item["source_key"])
            for item in checks
            if item.get("scorer_usability") in {"do_not_use_as_latest", "requires_backfill_before_latest_scoring"}
        ],
        "background_only": [
            str(item["source_key"])
            for item in checks
            if item.get("scorer_usability") == "use_as_background_only_unconfirmed"
        ],
        "weights": {item["source_key"]: item["scorer_weight"] for item in checks},
        "checks": checks,
        "warnings": [str(item["warning"]) for item in checks if item.get("warning")],
    }


# --- merged from evidence_preflight.py ---
EVIDENCE_PREFLIGHT_VERSION = "evidence-preflight-v1"


def _payload_result(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not payload:
        return {}
    return payload.get("result") or payload


def _status_counts(report: dict[str, Any]) -> dict[str, int]:
    counts = Counter()
    for item in report.get("sources") or []:
        counts[str(item.get("status") or "unknown")] += 1
    for key, value in (report.get("status_counts") or {}).items():
        counts[str(key)] = int(value or 0)
    return dict(counts)


def _source_keys(report: dict[str, Any], key: str, statuses: set[str]) -> list[str]:
    explicit = report.get(key)
    if isinstance(explicit, list):
        return [str(item) for item in explicit]
    return [
        str(item.get("source_key") or "")
        for item in report.get("sources") or []
        if str(item.get("status") or "").lower() in statuses and item.get("source_key")
    ]


def _scorer_guard_sources(report: dict[str, Any], key: str) -> list[str]:
    guard = report.get("scorer_guard") or {}
    values = guard.get(key) or []
    return [str(item) for item in values]


def build_evidence_preflight_report(
    freshness_payload: dict[str, Any] | None,
    *,
    watchdog_payload: dict[str, Any] | None = None,
    block_on_stale: bool = True,
    block_on_offline: bool = True,
    block_on_watchdog_failure: bool = True,
    allow_lagging_with_haircut: bool = True,
    lagging_size_multiplier: float = 0.50,
    min_source_weight: float = 0.25,
) -> dict[str, Any]:
    """Convert evidence freshness/recovery state into an operating preflight gate.

    This is intentionally read-only. It does not rebuild sources or mutate scorer/paper rows.
    Downstream gates can use the resulting action to block new paper entries or apply a size
    haircut when the evidence stack is not fresh enough for full-risk operation.
    """

    freshness = _payload_result(freshness_payload)
    watchdog = _payload_result(watchdog_payload)
    generated_at = datetime.now(UTC).isoformat()
    overall = str(freshness.get("overall_status") or "unknown").lower()
    status_counts = _status_counts(freshness)
    stale_sources = _source_keys(freshness, "stale_source_keys", {"stale"})
    offline_sources = _source_keys(freshness, "offline_source_keys", {"offline"})
    lagging_sources = _source_keys(freshness, "lagging_source_keys", {"lagging"})
    do_not_use_latest = _scorer_guard_sources(freshness, "do_not_use_as_latest")
    background_only = _scorer_guard_sources(freshness, "background_only")
    weights = {
        str(key): float(value)
        for key, value in (freshness.get("source_freshness_weights") or {}).items()
        if isinstance(value, (int, float))
    }
    underweight_sources = [key for key, value in sorted(weights.items()) if value < float(min_source_weight)]
    watchdog_status = str(watchdog.get("status") or "").lower()
    watchdog_failures = int(watchdog.get("consecutive_failures") or 0)
    watchdog_failed = watchdog_status in {"refresh_failed", "timeout", "failed", "error"} or watchdog_failures > 0
    reasons: list[str] = []
    warnings: list[str] = []

    if not freshness:
        reasons.append("evidence_freshness_report_missing")
    if block_on_offline and (offline_sources or overall == "red"):
        reasons.append("evidence_offline_sources")
    if block_on_stale and stale_sources:
        reasons.append("evidence_stale_sources")
    if block_on_watchdog_failure and watchdog_failed:
        reasons.append("evidence_auto_recovery_failed")
    if underweight_sources:
        warnings.append("evidence_sources_below_min_weight")
    if lagging_sources:
        warnings.append("evidence_lagging_sources")
    if watchdog_status in {"refresh_completed_gap_remains", "skipped_cooldown"} and (lagging_sources or stale_sources):
        warnings.append("evidence_recovery_gap_remains")

    if reasons:
        gate_status = "block"
        action = "block_new_paper_entries"
        allowed = False
        size_multiplier = 0.0
    elif lagging_sources and allow_lagging_with_haircut:
        gate_status = "watch"
        action = "allow_with_evidence_haircut"
        allowed = True
        size_multiplier = max(0.0, min(1.0, float(lagging_size_multiplier)))
    elif warnings:
        gate_status = "watch"
        action = "allow_with_evidence_watch"
        allowed = True
        size_multiplier = 1.0
    else:
        gate_status = "pass"
        action = "allow_normal"
        allowed = True
        size_multiplier = 1.0

    return {
        "evidence_preflight_version": EVIDENCE_PREFLIGHT_VERSION,
        "generated_at": generated_at,
        "gate_status": gate_status,
        "allowed": allowed,
        "action": action,
        "entry_size_multiplier": size_multiplier,
        "reason": ",".join(reasons) if reasons else "",
        "warnings": warnings,
        "freshness": {
            "overall_status": freshness.get("overall_status"),
            "status_counts": status_counts,
            "stale_source_keys": stale_sources,
            "offline_source_keys": offline_sources,
            "lagging_source_keys": lagging_sources,
            "do_not_use_as_latest": do_not_use_latest,
            "background_only": background_only,
            "underweight_sources": underweight_sources,
            "scorer_guard_action": (freshness.get("scorer_guard") or {}).get("action"),
        },
        "auto_recovery": {
            "status": watchdog.get("status"),
            "last_freshness_status": watchdog.get("last_freshness_status"),
            "consecutive_failures": watchdog_failures,
            "next_refresh_eligible_at": watchdog.get("next_refresh_eligible_at"),
            "last_stale_source_keys": watchdog.get("last_stale_source_keys") or [],
            "last_lagging_source_keys": watchdog.get("last_lagging_source_keys") or [],
        },
        "parameters": {
            "block_on_stale": bool(block_on_stale),
            "block_on_offline": bool(block_on_offline),
            "block_on_watchdog_failure": bool(block_on_watchdog_failure),
            "allow_lagging_with_haircut": bool(allow_lagging_with_haircut),
            "lagging_size_multiplier": float(lagging_size_multiplier),
            "min_source_weight": float(min_source_weight),
        },
        "safety": {
            "read_only_preflight": True,
            "scorer_rows_mutated": False,
            "ibkr_order_submitted": False,
            "transmit_enabled": False,
            "portfolio_mutated": False,
        },
    }


def evidence_preflight_markdown(report: dict[str, Any]) -> str:
    freshness = report.get("freshness") or {}
    recovery = report.get("auto_recovery") or {}
    lines = [
        "# Evidence Preflight Gate",
        "",
        f"- Version: `{report.get('evidence_preflight_version')}`",
        f"- Generated at: `{report.get('generated_at')}`",
        f"- Gate status: `{report.get('gate_status')}`",
        f"- Allowed: `{report.get('allowed')}`",
        f"- Action: `{report.get('action')}`",
        f"- Entry size multiplier: `{report.get('entry_size_multiplier')}`",
        f"- Reason: `{report.get('reason') or '-'}`",
        f"- Warnings: `{','.join(report.get('warnings') or []) or '-'}`",
        "",
        "## Evidence state",
        "",
        f"- Overall freshness: `{freshness.get('overall_status') or '-'}`",
        f"- Status counts: `{freshness.get('status_counts') or {}}`",
        f"- Stale sources: `{','.join(freshness.get('stale_source_keys') or []) or '-'}`",
        f"- Offline sources: `{','.join(freshness.get('offline_source_keys') or []) or '-'}`",
        f"- Lagging sources: `{','.join(freshness.get('lagging_source_keys') or []) or '-'}`",
        f"- Scorer guard action: `{freshness.get('scorer_guard_action') or '-'}`",
        "",
        "## Auto-recovery state",
        "",
        f"- Watchdog status: `{recovery.get('status') or '-'}`",
        f"- Last freshness status: `{recovery.get('last_freshness_status') or '-'}`",
        f"- Consecutive failures: `{recovery.get('consecutive_failures')}`",
        f"- Next refresh eligible at: `{recovery.get('next_refresh_eligible_at') or '-'}`",
        "",
        "## Safety",
        "",
        "- Read-only preflight report; no broker order submission, transmit, scorer-row mutation, or portfolio mutation.",
    ]
    return "\n".join(lines) + "\n"


def write_evidence_preflight_json(output_path: Path, report: dict[str, Any]) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return output_path


def write_evidence_preflight_markdown(output_path: Path, report: dict[str, Any]) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(evidence_preflight_markdown(report), encoding="utf-8")
    return output_path


# --- merged from evidence_refresh_queue.py ---
EVIDENCE_REFRESH_QUEUE_VERSION = "evidence-refresh-queue-v1"


@dataclass(frozen=True)
class EvidenceRefreshQueueConfig:
    freshness_report_path: Path
    evidence_freshness_profile_path: Path
    dashboard_profile_path: Path
    output_dir: Path
    market_data_seconds: int = 16
    external_limit: int = 50
    external_timespan: str = "3d"
    ibkr_news_limit_per_symbol: int = 25
    technical_start_days: int = 14
    execute: bool = False
    max_jobs: int | None = None
    job_timeout_seconds: float = 120.0
    include_live_ibkr: bool = True


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


def _date_for_start(value: Any, *, default_days: int) -> str:
    parsed = _parse_dt(value)
    if parsed is None:
        parsed = datetime.now(UTC) - timedelta(days=default_days)
    return parsed.date().isoformat()


def _today() -> str:
    return datetime.now(UTC).date().isoformat()


def _source_map(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(item.get("source_key") or ""): item for item in report.get("sources") or []}


def _needs(report: dict[str, Any], source_key: str, *, include_lagging: bool = True) -> bool:
    source = _source_map(report).get(source_key) or {}
    status = str(source.get("status") or "").lower()
    if include_lagging:
        return status in {"lagging", "stale", "offline"}
    return status in {"stale", "offline"}


def _job(job_id: str, description: str, command: list[str], *, source_keys: list[str], priority: int, live_ibkr: bool = False) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "priority": priority,
        "description": description,
        "source_keys": source_keys,
        "live_ibkr": bool(live_ibkr),
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


def build_evidence_refresh_jobs(
    *,
    freshness_report: dict[str, Any],
    settings: Any,
    config: EvidenceRefreshQueueConfig,
) -> list[dict[str, Any]]:
    py = sys.executable or "python"
    main_py = "main.py"
    output_dir = config.output_dir
    today = _today()
    sources = _source_map(freshness_report)
    jobs: list[dict[str, Any]] = []

    if _needs(freshness_report, "market_data", include_lagging=True):
        jobs.append(
            _job(
                "market_data_fresh_tick",
                "Refresh the read-only Market Data Desk snapshot so dashboard/scorer no longer rely on an old market-data tick.",
                [
                    py,
                    main_py,
                    "market-data-desk",
                    "--seconds",
                    str(config.market_data_seconds),
                    "--refresh-seconds",
                    "1",
                    "--market-data-type",
                    "1",
                    "--fallback-after-seconds",
                    "4",
                    "--output",
                    str(output_dir / "m153_market_data_desk_refresh.md"),
                    "--json-output-path",
                    str(output_dir / "m153_market_data_desk_refresh.json"),
                ],
                source_keys=["market_data"],
                priority=10,
                live_ibkr=True,
            )
        )

    if _needs(freshness_report, "market_news", include_lagging=False):
        start = _date_for_start((sources.get("market_news") or {}).get("latest_event_at"), default_days=3)
        symbols = ",".join(getattr(settings, "all_news_symbols", getattr(settings, "watch_symbols", ())))
        providers = ",".join(getattr(settings, "news_provider_codes", ("AUTO",)))
        jobs.append(
            _job(
                "ibkr_news_gap_backfill",
                "Backfill the stale IBKR/Benzinga market-news gap from the latest stored headline to today.",
                [
                    py,
                    main_py,
                    "ibkr-news-backfill",
                    "--symbols",
                    symbols,
                    "--providers",
                    providers,
                    "--start",
                    start,
                    "--end",
                    today,
                    "--limit-per-symbol",
                    str(config.ibkr_news_limit_per_symbol),
                    "--fetch-article-body",
                    "--max-pages-per-symbol",
                    "1",
                ],
                source_keys=["market_news"],
                priority=20,
                live_ibkr=True,
            )
        )
        jobs.append(
            _job(
                "benzinga_external_signal_conversion",
                "Convert newly stored Benzinga/IBKR headlines into geopolitical/external overlay rows when they contain relevant risk themes.",
                [
                    py,
                    main_py,
                    "benzinga-external-signals-build",
                    "--start",
                    start,
                    "--end",
                    today,
                    "--limit",
                    "10000",
                ],
                source_keys=["market_news", "geopolitical_signals"],
                priority=35,
            )
        )

    if _needs(freshness_report, "geopolitical_signals", include_lagging=False):
        jobs.append(
            _job(
                "external_market_sync",
                "Refresh Marketaux and Alpha Vantage geopolitical/news sentiment signals.",
                [
                    py,
                    main_py,
                    "external-market-sync",
                    "--source",
                    "all",
                    "--limit",
                    str(config.external_limit),
                    "--alpha-tickers",
                    ",".join(getattr(settings, "geopolitical_news_symbols", ())),
                ],
                source_keys=["geopolitical_signals"],
                priority=30,
            )
        )

    if _needs(freshness_report, "technical_indicators", include_lagging=True):
        start = (datetime.now(UTC).date() - timedelta(days=max(1, int(config.technical_start_days)))).isoformat()
        jobs.append(
            _job(
                "technical_indicator_refresh",
                "Refresh daily technical indicators for watch, benchmark, and geopolitical symbols.",
                [
                    py,
                    main_py,
                    "technical-indicators-build",
                    "--symbols",
                    ",".join(getattr(settings, "all_price_symbols", getattr(settings, "watch_symbols", ()))),
                    "--start",
                    start,
                    "--end",
                    today,
                    "--price-source",
                    "yahoo",
                    "--lookback-days",
                    "120",
                    "--limit-per-symbol",
                    "260",
                ],
                source_keys=["technical_indicators"],
                priority=40,
            )
        )

    if _needs(freshness_report, "economic_indicators", include_lagging=True):
        start = _date_for_start((sources.get("economic_indicators") or {}).get("latest_event_at"), default_days=7)
        jobs.extend(
            [
                _job(
                    "fred_universe_sync",
                    "Refresh the curated FRED universe when macro evidence is lagging or stale.",
                    [
                        py,
                        main_py,
                        "fred-universe-sync",
                        "--universe",
                        "global_core",
                        "--start",
                        start,
                        "--end",
                        today,
                    ],
                    source_keys=["economic_indicators"],
                    priority=50,
                ),
                _job(
                    "international_macro_sync",
                    "Refresh direct international macro feeds for Japan, Europe, Korea/BOK fallbacks where configured.",
                    [
                        py,
                        main_py,
                        "international-macro-sync",
                        "--source",
                        "all",
                        "--start",
                        start,
                        "--end",
                        today,
                    ],
                    source_keys=["economic_indicators"],
                    priority=55,
                ),
            ]
        )

    if _needs(freshness_report, "standing_regimes", include_lagging=True) or _needs(freshness_report, "geopolitical_signals", include_lagging=False):
        jobs.append(
            _job(
                "standing_regime_rebuild",
                "Rebuild standing regimes after geopolitical/news refresh so long-running risk/favorable regimes have current confirmation.",
                [
                    py,
                    main_py,
                    "standing-regime-build",
                    "--start",
                    "2025-01-01",
                    "--end",
                    today,
                    "--limit",
                    "100000",
                ],
                source_keys=["standing_regimes", "geopolitical_signals"],
                priority=70,
            )
        )

    jobs.extend(
        [
            _job(
                "economic_indicator_desk_rebuild",
                "Rebuild the Economic Indicator Desk JSON/Markdown view used by the dashboard and scorer packets.",
                [
                    py,
                    main_py,
                    "economic-indicator-desk",
                    "--universe",
                    "global_core",
                    "--cutoff-date",
                    today,
                    "--output",
                    str(output_dir / "m63_global_rates_economic_indicator_desk.json"),
                    "--json",
                ],
                source_keys=["economic_indicators"],
                priority=80,
            ),
            _job(
                "evidence_freshness_recheck",
                "Rebuild the M152 Evidence Freshness Desk after refresh jobs complete.",
                [
                    py,
                    main_py,
                    "evidence-freshness-desk",
                    "--profile-json",
                    str(config.evidence_freshness_profile_path),
                ],
                source_keys=["all"],
                priority=90,
            ),
            _job(
                "dashboard_rebuild",
                "Rebuild dashboard snapshot and HTML so the operator sees the refreshed evidence state.",
                [
                    py,
                    main_py,
                    "trading-dashboard-html",
                    "--profile-json",
                    str(config.dashboard_profile_path),
                    "--json-output-path",
                    str(output_dir / "m143_trading_dashboard_snapshot.json"),
                    "--output",
                    str(output_dir / "trading_command_center.html"),
                    "--json",
                ],
                source_keys=["dashboard"],
                priority=100,
            ),
        ]
    )
    jobs = sorted(jobs, key=lambda item: int(item.get("priority") or 0))
    if not config.include_live_ibkr:
        for job in jobs:
            if job.get("live_ibkr"):
                job["status"] = "skipped_live_ibkr_disabled"
    if config.max_jobs is not None:
        allowed = {job["job_id"] for job in jobs[: max(0, int(config.max_jobs))]}
        for job in jobs:
            if job["job_id"] not in allowed and job.get("status") == "planned":
                job["status"] = "skipped_max_jobs"
    return jobs


def _tail(text: str, limit: int = 3000) -> str:
    if len(text) <= limit:
        return text
    return text[-limit:]


def execute_refresh_jobs(jobs: list[dict[str, Any]], *, cwd: Path, timeout_seconds: float) -> list[dict[str, Any]]:
    executed: list[dict[str, Any]] = []
    for job in jobs:
        if job.get("status") != "planned":
            executed.append(job)
            continue
        started = time.monotonic()
        job["started_at"] = _iso_now()
        job["status"] = "running"
        try:
            completed = subprocess.run(
                [str(item) for item in job.get("command") or []],
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=float(timeout_seconds),
            )
            job["returncode"] = completed.returncode
            job["stdout_tail"] = _tail(completed.stdout or "")
            job["stderr_tail"] = _tail(completed.stderr or "")
            job["status"] = "succeeded" if completed.returncode == 0 else "failed"
        except subprocess.TimeoutExpired as exc:
            job["status"] = "timeout"
            job["returncode"] = None
            job["stdout_tail"] = _tail(exc.stdout or "")
            job["stderr_tail"] = _tail(exc.stderr or "")
            job["error"] = f"timeout_after_{timeout_seconds}_seconds"
        except Exception as exc:  # pragma: no cover - defensive execution audit
            job["status"] = "failed"
            job["error"] = f"{exc.__class__.__name__}: {str(exc)[:500]}"
        finally:
            job["finished_at"] = _iso_now()
            job["duration_seconds"] = round(time.monotonic() - started, 3)
        executed.append(job)
    return executed


def build_evidence_refresh_queue(
    *,
    freshness_report: dict[str, Any],
    settings: Any,
    config: EvidenceRefreshQueueConfig,
    cwd: Path | None = None,
) -> dict[str, Any]:
    cwd = (cwd or Path.cwd()).expanduser().resolve()
    jobs = build_evidence_refresh_jobs(freshness_report=freshness_report, settings=settings, config=config)
    if config.execute:
        jobs = execute_refresh_jobs(jobs, cwd=cwd, timeout_seconds=config.job_timeout_seconds)
    status_counts: dict[str, int] = {}
    for job in jobs:
        status = str(job.get("status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
    return {
        "evidence_refresh_queue_version": EVIDENCE_REFRESH_QUEUE_VERSION,
        "generated_at": _iso_now(),
        "mode": "execute" if config.execute else "plan",
        "freshness_report_path": str(config.freshness_report_path),
        "initial_freshness_status": freshness_report.get("overall_status"),
        "initial_stale_source_keys": freshness_report.get("stale_source_keys") or [],
        "initial_lagging_source_keys": freshness_report.get("lagging_source_keys") or [],
        "job_count": len(jobs),
        "status_counts": status_counts,
        "jobs": jobs,
        "safety": {
            "read_only_refresh_or_rebuild": True,
            "ibkr_order_submitted": False,
            "transmit_enabled": False,
            "scorer_rows_mutated": False,
            "secrets_written": False,
        },
    }


def evidence_refresh_queue_markdown(result: dict[str, Any]) -> str:
    lines = [
        "# Evidence Refresh Queue",
        "",
        f"- Version: `{result.get('evidence_refresh_queue_version')}`",
        f"- Generated at: `{result.get('generated_at')}`",
        f"- Mode: `{result.get('mode')}`",
        f"- Initial freshness: `{result.get('initial_freshness_status')}`",
        f"- Initial stale sources: `{','.join(result.get('initial_stale_source_keys') or []) or '-'}`",
        f"- Initial lagging sources: `{','.join(result.get('initial_lagging_source_keys') or []) or '-'}`",
        f"- Status counts: `{result.get('status_counts') or {}}`",
        "",
        "| Priority | Job | Status | Live IBKR | Sources | Description |",
        "| ---: | --- | --- | --- | --- | --- |",
    ]
    for job in result.get("jobs") or []:
        lines.append(
            f"| {job.get('priority')} | `{job.get('job_id')}` | {job.get('status')} | {job.get('live_ibkr')} "
            f"| {','.join(job.get('source_keys') or [])} | {job.get('description')} |"
        )
    lines.extend(["", "## Safety", ""])
    lines.append("- Read-only refresh/backfill/rebuild queue; no order placement, no transmit, no portfolio mutation, and no secret values written to artifacts.")
    return "\n".join(lines) + "\n"


def write_evidence_refresh_queue_json(output_path: Path, result: dict[str, Any]) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return output_path


def write_evidence_refresh_queue_markdown(output_path: Path, result: dict[str, Any]) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(evidence_refresh_queue_markdown(result), encoding="utf-8")
    return output_path


# --- merged from evidence_refresh_watchdog.py ---
EVIDENCE_REFRESH_WATCHDOG_VERSION = "evidence-refresh-watchdog-v1"


@dataclass(frozen=True)
class EvidenceRefreshWatchdogConfig:
    freshness_profile_path: Path
    refresh_queue_profile_path: Path
    freshness_report_path: Path
    refresh_queue_json_path: Path
    refresh_queue_markdown_path: Path
    state_json_path: Path
    tick_log_jsonl_path: Path | None = None
    max_ticks: int = 1
    interval_seconds: float = 1.0
    sleep_between_ticks: bool = False
    cooldown_seconds: float = 300.0
    initial_backoff_seconds: float = 120.0
    max_backoff_seconds: float = 1800.0
    queue_timeout_seconds: float = 240.0
    execute_refresh: bool = True
    run_freshness_recheck: bool = True
    dashboard_rebuild_command: list[str] | None = None


CommandRunner = Callable[[list[str], Path, float], dict[str, Any]]


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


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


def _tail(text: Any, limit: int = 2200) -> str:
    value = str(text or "")
    return value if len(value) <= limit else value[-limit:]


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


def _read_json(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        return {}
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> Path:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return resolved


def _append_jsonl(path: Path | None, payload: dict[str, Any]) -> None:
    if path is None:
        return
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    with resolved.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")


def _freshness_needs_refresh(report: dict[str, Any], *, include_lagging: bool = True) -> bool:
    if bool(report.get("evidence_gap_warning")):
        return True
    if report.get("stale_source_keys"):
        return True
    return bool(include_lagging and report.get("lagging_source_keys"))


def _status_counts_failed(queue_result: dict[str, Any]) -> int:
    counts = queue_result.get("status_counts") or {}
    return sum(int(counts.get(key) or 0) for key in ("failed", "timeout"))


def _state_template(config: EvidenceRefreshWatchdogConfig, *, run_id: str, started_at: str) -> dict[str, Any]:
    return {
        "evidence_refresh_watchdog_version": EVIDENCE_REFRESH_WATCHDOG_VERSION,
        "run_id": run_id,
        "started_at": started_at,
        "updated_at": started_at,
        "status": "initialized",
        "last_tick_index": 0,
        "last_refresh_started_at": None,
        "last_refresh_finished_at": None,
        "last_refresh_status": None,
        "last_refresh_queue_status_counts": {},
        "last_freshness_status": None,
        "last_stale_source_keys": [],
        "last_lagging_source_keys": [],
        "consecutive_failures": 0,
        "current_backoff_seconds": float(config.initial_backoff_seconds),
        "cooldown_seconds": float(config.cooldown_seconds),
        "next_refresh_eligible_at": None,
        "tick_summaries": [],
        "safety": {
            "read_only_refresh_or_rebuild": True,
            "ibkr_order_submitted": False,
            "transmit_enabled": False,
            "scorer_rows_mutated": False,
            "secrets_written": False,
        },
    }


def _is_cooldown_active(state: dict[str, Any], now_dt: datetime) -> bool:
    eligible = _parse_dt(state.get("next_refresh_eligible_at"))
    return bool(eligible and now_dt < eligible)


def _seconds_until(state: dict[str, Any], now_dt: datetime) -> float | None:
    eligible = _parse_dt(state.get("next_refresh_eligible_at"))
    if eligible is None:
        return None
    return max(0.0, round((eligible - now_dt).total_seconds(), 3))


def _command_with_meta(command: list[str], *, description: str) -> dict[str, Any]:
    return {
        "description": description,
        "command": command,
        "status": "planned",
        "returncode": None,
        "duration_seconds": None,
        "stdout_tail": "",
        "stderr_tail": "",
        "error": None,
    }


def _run_command(
    command_meta: dict[str, Any],
    *,
    cwd: Path,
    timeout_seconds: float,
    runner: CommandRunner,
) -> dict[str, Any]:
    command_meta["started_at"] = _iso_now()
    result = runner([str(item) for item in command_meta.get("command") or []], cwd, timeout_seconds)
    command_meta.update(result)
    command_meta["finished_at"] = _iso_now()
    return command_meta


def _refresh_queue_command(config: EvidenceRefreshWatchdogConfig) -> list[str]:
    command = [
        sys.executable or "python",
        "main.py",
        "evidence-refresh-queue",
        "--profile-json",
        str(config.refresh_queue_profile_path),
        "--json-output-path",
        str(config.refresh_queue_json_path),
        "--output",
        str(config.refresh_queue_markdown_path),
        "--json",
    ]
    if config.execute_refresh:
        command.append("--execute")
    command.extend(["--job-timeout-seconds", str(int(config.queue_timeout_seconds))])
    return command


def _freshness_command(config: EvidenceRefreshWatchdogConfig) -> list[str]:
    return [
        sys.executable or "python",
        "main.py",
        "evidence-freshness-desk",
        "--profile-json",
        str(config.freshness_profile_path),
        "--json-output-path",
        str(config.freshness_report_path),
        "--output",
        str(Path(str(config.freshness_report_path)).with_suffix(".md")),
    ]


def run_evidence_refresh_watchdog(
    *,
    config: EvidenceRefreshWatchdogConfig,
    cwd: Path | None = None,
    run_id: str | None = None,
    command_runner: CommandRunner | None = None,
    now_sequence: list[str] | None = None,
) -> dict[str, Any]:
    cwd = (cwd or Path.cwd()).expanduser().resolve()
    runner = command_runner or _default_command_runner
    started_at = _iso_now()
    run_id = run_id or f"evidence_refresh_watchdog_{started_at.replace(':', '').replace('+', 'Z')}"
    state = _read_json(config.state_json_path) or _state_template(config, run_id=run_id, started_at=started_at)
    state.setdefault("evidence_refresh_watchdog_version", EVIDENCE_REFRESH_WATCHDOG_VERSION)
    state["run_id"] = run_id
    tick_summaries: list[dict[str, Any]] = []
    continuous = int(config.max_ticks) == 0
    tick_index = 0
    while continuous or tick_index < int(config.max_ticks):
        tick_index += 1
        now_raw = now_sequence[tick_index - 1] if now_sequence and tick_index - 1 < len(now_sequence) else _iso_now()
        now_dt = _parse_dt(now_raw) or datetime.now(UTC)
        commands: list[dict[str, Any]] = []
        if config.run_freshness_recheck:
            freshness_meta = _command_with_meta(_freshness_command(config), description="Rebuild Evidence Freshness Desk before watchdog decision.")
            commands.append(_run_command(freshness_meta, cwd=cwd, timeout_seconds=60.0, runner=runner))
        freshness = _read_json(config.freshness_report_path)
        needs_refresh = _freshness_needs_refresh(freshness)
        cooldown_active = _is_cooldown_active(state, now_dt)
        tick_status = "healthy_no_refresh_needed"
        queue_result: dict[str, Any] = {}
        if needs_refresh and cooldown_active:
            tick_status = "skipped_cooldown"
        elif needs_refresh:
            refresh_meta = _command_with_meta(_refresh_queue_command(config), description="Run Evidence Refresh Queue because freshness is stale or lagging.")
            commands.append(_run_command(refresh_meta, cwd=cwd, timeout_seconds=config.queue_timeout_seconds + 60.0, runner=runner))
            queue_result = _read_json(config.refresh_queue_json_path)
            command_ok = refresh_meta.get("status") == "succeeded"
            queue_failures = _status_counts_failed(queue_result)
            if command_ok and queue_failures == 0:
                refreshed = _read_json(config.freshness_report_path)
                recovered = not _freshness_needs_refresh(refreshed)
                tick_status = "recovered" if recovered else "refresh_completed_gap_remains"
                state["consecutive_failures"] = 0
                state["current_backoff_seconds"] = float(config.initial_backoff_seconds)
                state["next_refresh_eligible_at"] = (now_dt + timedelta(seconds=float(config.cooldown_seconds))).isoformat()
            else:
                tick_status = "refresh_failed"
                state["consecutive_failures"] = int(state.get("consecutive_failures") or 0) + 1
                backoff = min(float(config.max_backoff_seconds), max(float(config.initial_backoff_seconds), float(state.get("current_backoff_seconds") or config.initial_backoff_seconds) * 2.0))
                state["current_backoff_seconds"] = backoff
                state["next_refresh_eligible_at"] = (now_dt + timedelta(seconds=backoff)).isoformat()
        else:
            state["consecutive_failures"] = 0
            state["current_backoff_seconds"] = float(config.initial_backoff_seconds)

        final_freshness = _read_json(config.freshness_report_path)
        if config.dashboard_rebuild_command:
            dashboard_meta = _command_with_meta(config.dashboard_rebuild_command, description="Optional dashboard rebuild after watchdog tick.")
            commands.append(_run_command(dashboard_meta, cwd=cwd, timeout_seconds=60.0, runner=runner))
        tick_summary = {
            "tick_index": tick_index,
            "checked_at": now_dt.isoformat(),
            "status": tick_status,
            "needs_refresh": needs_refresh,
            "cooldown_active": cooldown_active,
            "cooldown_remaining_seconds": _seconds_until(state, now_dt),
            "freshness_status": final_freshness.get("overall_status"),
            "stale_source_keys": final_freshness.get("stale_source_keys") or [],
            "lagging_source_keys": final_freshness.get("lagging_source_keys") or [],
            "refresh_queue_status_counts": queue_result.get("status_counts") or {},
            "commands": commands,
        }
        tick_summaries.append(tick_summary)
        _append_jsonl(config.tick_log_jsonl_path, tick_summary)
        state["updated_at"] = _iso_now()
        state["status"] = tick_status
        state["last_tick_index"] = tick_index
        state["last_freshness_status"] = final_freshness.get("overall_status")
        state["last_stale_source_keys"] = final_freshness.get("stale_source_keys") or []
        state["last_lagging_source_keys"] = final_freshness.get("lagging_source_keys") or []
        state["last_refresh_queue_status_counts"] = queue_result.get("status_counts") or state.get("last_refresh_queue_status_counts") or {}
        if queue_result:
            state["last_refresh_started_at"] = queue_result.get("generated_at")
            state["last_refresh_finished_at"] = _iso_now()
            state["last_refresh_status"] = "failed" if _status_counts_failed(queue_result) else "succeeded"
        state["tick_summaries"] = (state.get("tick_summaries") or [])[-20:] + [tick_summary]
        _write_json(config.state_json_path, state)
        if continuous or tick_index < int(config.max_ticks):
            if config.sleep_between_ticks:
                time.sleep(max(0.0, float(config.interval_seconds)))
    return {
        "evidence_refresh_watchdog_version": EVIDENCE_REFRESH_WATCHDOG_VERSION,
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": _iso_now(),
        "executed_tick_count": tick_index,
        "final_status": state.get("status"),
        "final_freshness_status": state.get("last_freshness_status"),
        "final_stale_source_keys": state.get("last_stale_source_keys") or [],
        "final_lagging_source_keys": state.get("last_lagging_source_keys") or [],
        "consecutive_failures": state.get("consecutive_failures"),
        "next_refresh_eligible_at": state.get("next_refresh_eligible_at"),
        "state_json_path": str(config.state_json_path.expanduser().resolve()),
        "tick_log_jsonl_path": str(config.tick_log_jsonl_path.expanduser().resolve()) if config.tick_log_jsonl_path else None,
        "tick_summaries": tick_summaries,
        "safety": state.get("safety") or {},
    }


def evidence_refresh_watchdog_markdown(result: dict[str, Any]) -> str:
    lines = [
        "# Evidence Refresh Watchdog",
        "",
        f"- Version: `{result.get('evidence_refresh_watchdog_version')}`",
        f"- Run ID: `{result.get('run_id')}`",
        f"- Final status: `{result.get('final_status')}`",
        f"- Final freshness: `{result.get('final_freshness_status')}`",
        f"- Stale sources: `{','.join(result.get('final_stale_source_keys') or []) or '-'}`",
        f"- Lagging sources: `{','.join(result.get('final_lagging_source_keys') or []) or '-'}`",
        f"- Consecutive failures: `{result.get('consecutive_failures')}`",
        f"- Next refresh eligible at: `{result.get('next_refresh_eligible_at') or '-'}`",
        "",
        "| Tick | Status | Freshness | Stale | Lagging | Cooldown | Queue counts |",
        "| ---: | --- | --- | --- | --- | ---: | --- |",
    ]
    for tick in result.get("tick_summaries") or []:
        lines.append(
            f"| {tick.get('tick_index')} | {tick.get('status')} | {tick.get('freshness_status')} "
            f"| {','.join(tick.get('stale_source_keys') or []) or '-'} "
            f"| {','.join(tick.get('lagging_source_keys') or []) or '-'} "
            f"| {tick.get('cooldown_remaining_seconds')} | {tick.get('refresh_queue_status_counts') or {}} |"
        )
    lines.extend(["", "## Safety", ""])
    lines.append("- Watchdog runs read-only evidence refresh/rebuild commands only; no order placement, no transmit, no scorer-row mutation, no portfolio mutation, and no secret values written.")
    return "\n".join(lines) + "\n"


def write_evidence_refresh_watchdog_json(output_path: Path, result: dict[str, Any]) -> Path:
    return _write_json(output_path, result)


def write_evidence_refresh_watchdog_markdown(output_path: Path, result: dict[str, Any]) -> Path:
    resolved = output_path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(evidence_refresh_watchdog_markdown(result), encoding="utf-8")
    return resolved
