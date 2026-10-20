from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


MARKET_DATA_SESSION_OVERLAY_VERSION = "market-data-session-overlay-v1"


def _result(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    result = payload.get("result")
    return result if isinstance(result, dict) else payload


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _minutes(value: str) -> int:
    hour, minute = str(value).split(":", 1)
    return int(hour) * 60 + int(minute)


def evaluate_us_market_session(
    *,
    now: str | None = None,
    timezone_name: str = "America/New_York",
    pre_start: str = "04:00",
    regular_start: str = "09:30",
    regular_end: str = "16:00",
    after_end: str = "20:00",
) -> dict[str, Any]:
    tz = ZoneInfo(timezone_name)
    if now:
        parsed = datetime.fromisoformat(str(now).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=tz)
        now_local = parsed.astimezone(tz)
    else:
        now_local = datetime.now(UTC).astimezone(tz)

    minute = now_local.hour * 60 + now_local.minute
    is_weekday = now_local.weekday() < 5
    pre = _minutes(pre_start)
    start = _minutes(regular_start)
    end = _minutes(regular_end)
    after = _minutes(after_end)
    if not is_weekday:
        session = "closed"
        reason = "US equity/options regular session is closed on weekend."
    elif pre <= minute < start:
        session = "pre_market"
        reason = "US cash equity extended-hours pre-market session."
    elif start <= minute < end:
        session = "regular"
        reason = "US cash equity and listed options regular session."
    elif end <= minute < after:
        session = "after_hours"
        reason = "US cash equity extended-hours after-hours session; listed option bid/ask liquidity is regular-session dependent."
    else:
        session = "overnight"
        reason = "US cash equity overnight/closed period; some stocks/ETFs and 24h markets can still print prices, but listed option bid/ask retry should wait for regular session."
    return {
        "market_session_version": "us-market-session-v1",
        "timezone": timezone_name,
        "now_local": now_local.isoformat(),
        "session": session,
        "is_regular_session": session == "regular",
        "is_extended_or_overnight": session in {"pre_market", "after_hours", "overnight"},
        "reason": reason,
        "schedule": {
            "pre_start": pre_start,
            "regular_start": regular_start,
            "regular_end": regular_end,
            "after_end": after_end,
        },
    }


def next_regular_us_market_open(now: str | None = None) -> dict[str, Any]:
    session = evaluate_us_market_session(now=now)
    now_local = datetime.fromisoformat(str(session["now_local"]))
    tz = ZoneInfo(session["timezone"])
    start_hour, start_minute = (9, 30)
    open_today = now_local.replace(hour=start_hour, minute=start_minute, second=0, microsecond=0)
    if session["session"] == "regular":
        next_open = now_local
        status = "ready_now"
    elif now_local.weekday() < 5 and now_local < open_today:
        next_open = open_today
        status = "waiting_for_today_regular_open"
    else:
        next_day = (now_local + timedelta(days=1)).date()
        while next_day.weekday() >= 5:
            next_day = next_day + timedelta(days=1)
        next_open = datetime(next_day.year, next_day.month, next_day.day, start_hour, start_minute, tzinfo=tz)
        status = "waiting_for_next_regular_open"
    wait_seconds = max(0.0, (next_open - now_local).total_seconds()) if status != "ready_now" else 0.0
    return {
        "status": status,
        "next_open_local": next_open.isoformat(),
        "next_open_utc": next_open.astimezone(UTC).isoformat(),
        "wait_seconds": wait_seconds,
        "wait_hours": wait_seconds / 3600.0,
    }


def classify_market_data_row(row: dict[str, Any]) -> dict[str, Any]:
    key = str(row.get("key") or "").upper()
    group = str(row.get("group") or row.get("desk_group") or "").lower()
    sec_type = str(row.get("sec_type") or row.get("security_type") or "").upper()
    if group in {"fx", "us_equity_futures", "commodities"} or key in {"US02Y", "KOSPI_FUT", "NIKKEI"} or sec_type in {"CASH", "FUT"}:
        bucket = "near_24h_market"
        monitorability = "monitorable_now"
        reason = "FX, futures, or commodity proxy can trade or update outside US cash regular hours."
    elif group == "crypto":
        bucket = "stock_proxy_for_24h_underlying"
        monitorability = "monitorable_now"
        reason = "Crypto underlying is 24h; current IBKR row is an ETF/proxy, so overnight prints remain useful but proxy limitations should be noted."
    elif group in {"us_equity_index_etf", "volatility_etf", "account_option_underlying"} or sec_type == "STK" or key in {"DXY", "BTC", "ETH"}:
        bucket = "cash_equity_or_etf_proxy"
        monitorability = "monitorable_now"
        reason = "Stock/ETF cash instruments can print extended-hours or overnight prices, so they remain monitorable outside regular session."
    elif group in {"volatility", "rates", "asia_equity"} or sec_type == "IND":
        bucket = "reference_or_regional_session_dependent"
        monitorability = "monitor_if_fresh_price_present"
        reason = "Index/reference or regional-session row may update outside US regular hours only when the underlying venue/feed is active."
    else:
        bucket = "unclassified"
        monitorability = "monitor_if_fresh_price_present"
        reason = "No explicit session class was found; preserve observed prices but avoid over-interpreting liquidity."
    return {
        "key": key,
        "group": group,
        "session_bucket": bucket,
        "monitorability": monitorability,
        "reason": reason,
    }


def build_market_data_session_overlay(
    market_data_payload: dict[str, Any] | None,
    *,
    now: str | None = None,
) -> dict[str, Any]:
    payload = _result(market_data_payload)
    rows = payload.get("rows") if isinstance(payload.get("rows"), list) else []
    summary = payload.get("summary") or {}
    session = evaluate_us_market_session(now=now)
    next_open = next_regular_us_market_open(now=now)
    row_interpretations: list[dict[str, Any]] = []
    bucket_counter: Counter[str] = Counter()
    monitorability_counter: Counter[str] = Counter()
    monitorable_alerts: list[str] = []
    regular_dependent_alerts: list[str] = []
    for row in rows:
        interp = classify_market_data_row(row)
        alerts = [str(item) for item in (row.get("alerts") or [])]
        priced = row.get("price") is not None
        interp.update(
            {
                "price_present": priced,
                "alerts": alerts,
                "status": row.get("status"),
                "price": row.get("price"),
                "delayed": bool(row.get("delayed")),
            }
        )
        row_interpretations.append(interp)
        bucket_counter[interp["session_bucket"]] += 1
        monitorability_counter[interp["monitorability"]] += 1
        if alerts:
            if interp["monitorability"] == "monitorable_now":
                monitorable_alerts.append(interp["key"])
            else:
                regular_dependent_alerts.append(interp["key"])

    option_quotes_regular_dependent = not bool(session.get("is_regular_session"))
    risk_interpretation = "full_regular_session"
    if session["session"] in {"pre_market", "after_hours", "overnight", "closed"}:
        risk_interpretation = "overnight_monitoring_active"
    notes = [
        "Stock/ETF cash instruments and proxies remain monitored when IBKR provides extended-hours or overnight prices.",
        "FX, futures, commodity proxies, and crypto-related rows remain important outside US cash regular hours.",
        "Listed option bid/ask quality remains regular-session dependent and should not be confused with stock overnight price monitoring.",
    ]
    return {
        "market_data_session_overlay_version": MARKET_DATA_SESSION_OVERLAY_VERSION,
        "generated_at": _iso_now(),
        "source_market_data_version": payload.get("market_data_desk_version"),
        "source_market_data_generated_at": payload.get("generated_at"),
        "session": session,
        "next_regular_open": next_open,
        "risk_interpretation": risk_interpretation,
        "stock_cash_overnight_monitoring_enabled": True,
        "option_quotes_regular_session_dependent": option_quotes_regular_dependent,
        "summary": {
            "source_temperature_label": (payload.get("temperature") or {}).get("label") or summary.get("temperature_label"),
            "source_temperature_score": (payload.get("temperature") or {}).get("score") or summary.get("temperature_score"),
            "row_count": len(rows),
            "session_bucket_counts": dict(bucket_counter),
            "monitorability_counts": dict(monitorability_counter),
            "monitorable_alert_instruments": sorted(set(monitorable_alerts)),
            "regular_dependent_alert_instruments": sorted(set(regular_dependent_alerts)),
            "interpretation_notes": notes,
        },
        "rows": row_interpretations,
        "safety": {
            "read_only_overlay": True,
            "ibkr_order_submitted": False,
            "live_order_transmitted": False,
            "portfolio_mutated": False,
        },
    }


def market_data_session_overlay_markdown(report: dict[str, Any]) -> str:
    session = report.get("session") or {}
    summary = report.get("summary") or {}
    lines = [
        "# Market Data Session Overlay",
        "",
        f"- Version: `{report.get('market_data_session_overlay_version')}`",
        f"- Generated at: `{report.get('generated_at')}`",
        f"- US market session: `{session.get('session')}`",
        f"- Risk interpretation: `{report.get('risk_interpretation')}`",
        f"- Stock cash overnight monitoring: `{report.get('stock_cash_overnight_monitoring_enabled')}`",
        f"- Option quotes regular-session dependent: `{report.get('option_quotes_regular_session_dependent')}`",
        f"- Source temperature: `{summary.get('source_temperature_label')}` score `{summary.get('source_temperature_score')}`",
        f"- Bucket counts: `{summary.get('session_bucket_counts') or {}}`",
        f"- Monitorability counts: `{summary.get('monitorability_counts') or {}}`",
        f"- Monitorable alert instruments: `{', '.join(summary.get('monitorable_alert_instruments') or []) or '-'}`",
        f"- Regular-dependent alert instruments: `{', '.join(summary.get('regular_dependent_alert_instruments') or []) or '-'}`",
        "",
        "## Interpretation notes",
        "",
    ]
    lines.extend(f"- {item}" for item in summary.get("interpretation_notes") or [])
    lines.extend(
        [
            "",
            "## Rows",
            "",
            "| Key | Bucket | Monitorability | Price | Alerts | Reason |",
            "| --- | --- | --- | ---: | --- | --- |",
        ]
    )
    for row in report.get("rows") or []:
        lines.append(
            f"| `{row.get('key')}` | `{row.get('session_bucket')}` | `{row.get('monitorability')}` | "
            f"{row.get('price') if row.get('price') is not None else '-'} | "
            f"{', '.join(row.get('alerts') or []) or '-'} | {str(row.get('reason') or '-').replace('|', '/')} |"
        )
    if not report.get("rows"):
        lines.append("| - | - | - | - | - | - |")
    lines.extend(["", "## Safety", "", "- Read-only session overlay; no broker order submission or portfolio mutation."])
    return "\n".join(lines) + "\n"


def write_market_data_session_overlay_json(output_path: Path, report: dict[str, Any]) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return output_path


def write_market_data_session_overlay_markdown(output_path: Path, report: dict[str, Any]) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(market_data_session_overlay_markdown(report), encoding="utf-8")
    return output_path
