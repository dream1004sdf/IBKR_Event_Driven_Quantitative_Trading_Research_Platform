from __future__ import annotations

import json
import os
import sqlite3
import time
from collections import Counter
from datetime import UTC, date, datetime, time as dt_time
from html import escape
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .geopolitical_intelligence import build_geopolitical_heatmap_from_connection
from .operational_lock_status import build_operational_lock_status
from .standing_regimes import standing_regime_runtime_metrics
from .trading_dashboard_alerts import (
    _atomic_write_jsonl,
    _rotate_jsonl_if_needed,
    _read_dashboard_alert_state,
    _telegram_alert_class,
    dashboard_alert_conditions,
    deliver_dashboard_alerts_to_telegram,
    evaluate_dashboard_alerts,
    write_dashboard_alert_state,
)
from .trading_dashboard_view import (
    html_badge as _html_badge,
    html_num as _html_num,
    html_table as _html_table,
    html_text as _html_text,
    _trading_dashboard_html_script,
    _trading_dashboard_html_style,
    _render_dashboard_account_market_cards,
    _render_dashboard_macro_learning_cards,
    _render_dashboard_plan_evidence_source_cards,
    _render_dashboard_positions_options_cards,
    _render_dashboard_status_recovery_cards,
    trading_dashboard_markdown,
)
TRADING_DASHBOARD_SNAPSHOT_VERSION = "trading-dashboard-snapshot-v1"
TRADING_DASHBOARD_SCHEMA_MARKER_VERSION = "trading-dashboard-schema-marker-v1"
TRADING_DASHBOARD_HTML_VERSION = "trading-dashboard-html-v1"
TRADING_DASHBOARD_LOOP_VERSION = "trading-dashboard-loop-v1"
TRADING_DASHBOARD_ALERT_VERSION = "trading-dashboard-alert-v1"
TRADING_DASHBOARD_TELEGRAM_VERSION = "trading-dashboard-telegram-alert-v1"
TRADING_DASHBOARD_RUNTIME_GUARD_VERSION = "trading-dashboard-runtime-guard-v1"
MARKET_DATA_ALERT_REARM_SECONDS = 120.0
SOURCE_ARTIFACT_STALE_SECONDS = 300.0
CORE_EVIDENCE_ONLINE_SOURCE_KEYS = {
    "market_data",
    "market_news",
    "geopolitical_signals",
}
DEFAULT_DASHBOARD_TICK_LOG_MAX_BYTES = 128 * 1024 * 1024
DEFAULT_DASHBOARD_TICK_LOG_BACKUPS = 5
REQUIRED_OPTIONS_SUPERVISOR_FIELDS = (
    "final_optional_index_effective_quote_quality_status",
    "m220_family_issue_summary.effective_operating_status_counts",
    "m220_family_issue_summary.scorer_use_strength_counts",
    "m220_family_issue_summary.effective_family_usage_rows",
)


def _read_json_file(path: Path | None) -> tuple[dict[str, Any] | None, str | None]:
    if path is None:
        return None, "path_not_configured"
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        return None, f"missing:{resolved}"
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except Exception as exc:
        return None, f"read_error:{resolved}:{exc}"
    return payload, None


def _result(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    result = payload.get("result")
    return result if isinstance(result, dict) else payload


def _has_nested_key(payload: dict[str, Any], dotted_path: str) -> bool:
    current: Any = payload
    for part in dotted_path.split("."):
        if not isinstance(current, dict) or part not in current:
            return False
        current = current.get(part)
    return True


def _missing_nested_fields(payload: dict[str, Any], dotted_paths: tuple[str, ...] | list[str]) -> list[str]:
    return [path for path in dotted_paths if not _has_nested_key(payload, path)]


def _runtime_version_guard(*, options_market_structure_supervisor: dict[str, Any]) -> dict[str, Any]:
    missing = _missing_nested_fields(options_market_structure_supervisor, REQUIRED_OPTIONS_SUPERVISOR_FIELDS)
    restart_recommended = bool(missing)
    return {
        "guard_version": TRADING_DASHBOARD_RUNTIME_GUARD_VERSION,
        "status": "restart_recommended" if restart_recommended else "ok",
        "restart_recommended": restart_recommended,
        "checked_components": ["options_market_structure_supervisor"],
        "options_market_structure_supervisor": {
            "version": options_market_structure_supervisor.get("version"),
            "completed_at": options_market_structure_supervisor.get("completed_at"),
            "age_seconds": options_market_structure_supervisor.get("age_seconds"),
            "missing_fields": missing,
            "interpretation": (
                "Supervisor artifact lacks fields expected by the current dashboard code; restart the supervisor/dashboard loops so new code writes fresh artifacts."
                if restart_recommended
                else "Supervisor artifact exposes the expected current fields."
            ),
        },
        "safety": {
            "diagnostic_only": True,
            "broker_order_submitted": False,
            "transmit_enabled": False,
        },
    }


def _dashboard_schema_integrity(snapshot: dict[str, Any]) -> dict[str, Any]:
    required_fields = (
        "evidence_freshness.effective_status",
        "evidence_freshness.artifact_stale",
        "source_ingest_health.effective_status",
        "source_ingest_health.artifact_stale",
        "source_online_supervisor.effective_status",
        "source_online_supervisor.artifact_stale",
        "singleton_locks.expected_names",
        "threshold_candidate_evaluator.version",
    )
    missing = [field for field in required_fields if not _has_nested_key(snapshot, field)]
    return {
        "schema_marker_version": TRADING_DASHBOARD_SCHEMA_MARKER_VERSION,
        "checked_at": snapshot.get("generated_at") or _iso_now(),
        "status": "schema_mismatch" if missing else "ok",
        "missing_required_fields": missing,
        "required_fields": list(required_fields),
        "purpose": "detect stale dashboard writers or profile/current-code mismatches on the live HTML/snapshot path",
    }


def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _parse_dt(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _age_seconds(value: Any) -> float | None:
    parsed = _parse_dt(value)
    if parsed is None:
        return None
    return round((datetime.now(UTC) - parsed).total_seconds(), 3)


def _current_us_option_session_context(now: datetime | None = None) -> dict[str, Any]:
    now_utc = (now or datetime.now(UTC)).astimezone(UTC)
    try:
        now_et = now_utc.astimezone(ZoneInfo("America/New_York"))
        timezone_name = "America/New_York"
    except Exception:
        now_et = now_utc
        timezone_name = "UTC_fallback"
    local_time = now_et.timetz().replace(tzinfo=None)
    is_weekday = now_et.weekday() < 5
    is_regular = bool(is_weekday and dt_time(9, 30) <= local_time < dt_time(16, 0))
    return {
        "checked_at": now_utc.isoformat(),
        "checked_at_et": now_et.isoformat(),
        "timezone": timezone_name,
        "session": "regular" if is_regular else "closed_or_extended",
        "is_weekday": is_weekday,
        "is_regular_option_session": is_regular,
        "regular_session_window_et": "09:30-16:00",
        "holiday_calendar_applied": False,
    }


def _market_data_alert_rearm_seconds(condition_key: str) -> float:
    if condition_key in {
        "market_data_temperature_non_green",
        "market_data_delayed_instruments",
        "market_data_missing_prices",
        "market_data_gap_or_sudden_move_alerts",
    }:
        return MARKET_DATA_ALERT_REARM_SECONDS
    if condition_key.startswith("status_reason:market_data_"):
        return MARKET_DATA_ALERT_REARM_SECONDS
    return 0.0


def _account_summary(payload: dict[str, Any] | None) -> dict[str, Any]:
    data = _result(payload)
    selected_account = data.get("selected_account")
    selected = None
    for row in data.get("accounts") or []:
        if row.get("account") == selected_account:
            selected = row
            break
    if selected is None and (data.get("accounts") or []):
        selected = (data.get("accounts") or [])[0]
    tags = selected.get("tags") if isinstance(selected, dict) else {}

    def tag_value(name: str) -> float | None:
        item = tags.get(name) if isinstance(tags, dict) else None
        if isinstance(item, dict):
            return _num(item.get("numeric_value") if item.get("numeric_value") is not None else item.get("value"))
        return _num(item)

    positions = []
    for row in data.get("positions") or []:
        contract = row.get("contract") or {}
        positions.append(
            {
                "account": row.get("account"),
                "symbol": contract.get("symbol"),
                "sec_type": contract.get("sec_type"),
                "exchange": contract.get("exchange"),
                "currency": contract.get("currency"),
                "expiry": contract.get("last_trade_date_or_contract_month"),
                "right": contract.get("right"),
                "strike": contract.get("strike"),
                "position": row.get("position"),
                "avg_cost": row.get("avg_cost"),
            }
        )
    return {
        "snapshot_version": data.get("snapshot_version"),
        "generated_at": data.get("generated_at"),
        "age_seconds": _age_seconds(data.get("generated_at")),
        "selected_account": selected_account,
        "account_count": data.get("account_count"),
        "net_liquidation": data.get("equity") if data.get("equity") is not None else tag_value("NetLiquidation"),
        "cash": data.get("cash") if data.get("cash") is not None else tag_value("TotalCashValue"),
        "buying_power": data.get("buying_power") if data.get("buying_power") is not None else tag_value("BuyingPower"),
        "available_funds": tag_value("AvailableFunds"),
        "excess_liquidity": tag_value("ExcessLiquidity"),
        "initial_margin": tag_value("InitMarginReq"),
        "maintenance_margin": tag_value("MaintMarginReq"),
        "position_count": data.get("position_count"),
        "open_order_count": data.get("open_order_count"),
        "positions": positions[:50],
        "open_orders": data.get("open_orders") or [],
    }


def _paper_portfolio_summary(payload: dict[str, Any] | None) -> dict[str, Any]:
    data = _result(payload)
    rows = data.get("portfolio_rows") or []
    positions = []
    for row in rows:
        positions.append(
            {
                "symbol": row.get("symbol"),
                "status": row.get("position_status"),
                "open_net_quantity": row.get("open_net_quantity"),
                "open_long_quantity": row.get("open_long_quantity"),
                "open_short_quantity": row.get("open_short_quantity"),
                "latest_mark_price": row.get("latest_mark_price"),
                "latest_mark_source": row.get("latest_mark_source"),
                "open_market_value": row.get("open_market_value"),
                "realized_pnl": row.get("realized_pnl"),
                "unrealized_pnl": row.get("unrealized_pnl"),
                "total_pnl": row.get("total_pnl"),
            }
        )
    return {
        "version": data.get("paper_portfolio_state_version"),
        "generated_at": data.get("generated_at"),
        "age_seconds": _age_seconds(data.get("generated_at")),
        "state_run_id": data.get("state_run_id"),
        "totals": data.get("totals") or {},
        "status_counts": data.get("status_counts") or {},
        "positions": positions,
    }


def _risk_summary(payload: dict[str, Any] | None) -> dict[str, Any]:
    data = _result(payload)
    return {
        "version": data.get("paper_portfolio_risk_state_version") or data.get("version"),
        "generated_at": data.get("generated_at"),
        "age_seconds": _age_seconds(data.get("generated_at")),
        "risk_run_id": data.get("risk_run_id"),
        "risk_state": data.get("risk_state"),
        "recommended_action": data.get("recommended_action"),
        "new_entries_allowed": data.get("new_entries_allowed"),
        "breach_count": data.get("breach_count"),
        "warning_count": data.get("warning_count"),
        "metrics": data.get("metrics") or {},
        "breaches": data.get("breaches") or [],
        "warnings": data.get("warnings") or [],
        "row_risk": (data.get("row_risk") or [])[:50],
    }


def _plan_summary(payload: dict[str, Any] | None) -> dict[str, Any]:
    data = _result(payload)
    intents = data.get("intents") or []
    rows = []
    for item in intents[:50]:
        rows.append(
            {
                "intent_id": item.get("intent_id"),
                "state": item.get("state"),
                "symbol": item.get("symbol"),
                "ticket_type": item.get("ticket_type"),
                "action": item.get("action"),
                "quantity": item.get("quantity"),
                "original_quantity": item.get("original_quantity"),
                "capital_aware_gate_applied": item.get("capital_aware_gate_applied"),
                "capital_gate_status": item.get("capital_gate_status"),
                "capital_quantity_scale": item.get("capital_quantity_scale"),
                "portfolio_risk_gate_applied": item.get("portfolio_risk_gate_applied"),
                "session_transition_release_governor_applied": item.get("session_transition_release_governor_applied"),
                "session_transition_release_governor_exempt": item.get("session_transition_release_governor_exempt"),
                "created_at": item.get("created_at"),
            }
        )
    return {
        "version": data.get("execution_intent_ledger_version"),
        "generated_at": data.get("generated_at"),
        "age_seconds": _age_seconds(data.get("generated_at")),
        "ledger_run_id": data.get("ledger_run_id"),
        "intent_count": data.get("intent_count"),
        "pending_paper_execution_count": data.get("pending_paper_execution_count"),
        "blocked_by_portfolio_risk_state_count": data.get("blocked_by_portfolio_risk_state_count"),
        "blocked_by_session_transition_release_governor_count": data.get("blocked_by_session_transition_release_governor_count"),
        "require_capital_aware_gate": data.get("require_capital_aware_gate"),
        "source_has_capital_aware_gate": data.get("source_has_capital_aware_gate"),
        "capital_aware_gate_applied": data.get("capital_aware_gate_applied"),
        "source_capital_ready_count": data.get("source_capital_ready_count"),
        "source_capital_resized_count": data.get("source_capital_resized_count"),
        "source_capital_blocked_count": data.get("source_capital_blocked_count"),
        "capital_aware_gate": data.get("capital_aware_gate") or {},
        "intent_state_counts": data.get("intent_state_counts") or {},
        "action_counts": data.get("action_counts") or {},
        "symbol_counts": data.get("symbol_counts") or {},
        "portfolio_risk_gate": data.get("portfolio_risk_gate") or {},
        "session_transition_release_governor": data.get("session_transition_release_governor") or {},
        "intents": rows,
    }


def _option_contract_label(row: dict[str, Any]) -> str:
    symbol = str(row.get("symbol") or "-")
    expiry = str(row.get("expiry") or row.get("expiration") or "-")
    right = str(row.get("right") or "-")
    strike = row.get("strike")
    return f"{symbol} {expiry} {strike}{right}"


def _option_position_key(row: dict[str, Any]) -> tuple[str, str]:
    return (str(row.get("symbol") or ""), str(row.get("expiry") or ""))


def _option_side(quantity: Any) -> str:
    value = _num(quantity)
    if value is None:
        return "unknown"
    return "long" if value > 0 else ("short" if value < 0 else "flat")


def _parse_option_expiry_date(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    candidates = [text]
    if len(text) >= 8 and text[:8].isdigit():
        candidates.append(text[:8])
    for candidate, fmt in ((item, "%Y%m%d") for item in candidates):
        try:
            return datetime.strptime(candidate, fmt).date()
        except ValueError:
            continue
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    return None


def _option_dte(expiry: Any, *, as_of: date | None = None) -> int | None:
    expiry_date = _parse_option_expiry_date(expiry)
    if expiry_date is None:
        return None
    return (expiry_date - (as_of or datetime.now(UTC).date())).days


def _option_expiry_risk(dte: int | None) -> tuple[str, str | None]:
    if dte is None:
        return "unknown", "option_expiry_unknown"
    if dte < 0:
        return "expired", "option_expired"
    if dte == 0:
        return "expires_today", "option_expires_today"
    if dte <= 3:
        return "expires_soon", "option_expires_soon"
    if dte <= 7:
        return "near_expiry", "option_near_expiry"
    return "normal", None


def _risk_level_worse(left: str | None, right: str | None) -> str:
    order = {"green": 0, "yellow": 1, "red": 2}
    left = left if left in order else "green"
    right = right if right in order else "green"
    return left if order[left] >= order[right] else right


def _underlying_price_map(
    *,
    market_data: dict[str, Any],
    paper_portfolio: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    prices: dict[str, dict[str, Any]] = {}

    def add(symbol: Any, price: Any, *, source: str, delayed: Any = None, status: Any = None) -> None:
        key = str(symbol or "").strip().upper()
        numeric = _num(price)
        if not key or numeric is None:
            return
        prices.setdefault(
            key,
            {
                "price": numeric,
                "source": source,
                "delayed": bool(delayed) if delayed is not None else None,
                "status": status,
            },
        )

    for row in market_data.get("rows") or []:
        if not isinstance(row, dict):
            continue
        price = (
            row.get("price")
            if row.get("price") is not None
            else row.get("last_price")
            if row.get("last_price") is not None
            else row.get("mark")
            if row.get("mark") is not None
            else row.get("close")
        )
        source = "market_data_desk"
        if row.get("market_data_type_label"):
            source = f"{source}:{row.get('market_data_type_label')}"
        for key in (row.get("key"), row.get("symbol"), row.get("underlying"), row.get("ticker")):
            add(key, price, source=source, delayed=row.get("delayed"), status=row.get("status"))

    for row in (paper_portfolio or {}).get("positions") or []:
        if not isinstance(row, dict):
            continue
        add(
            row.get("symbol"),
            row.get("latest_mark_price"),
            source=f"paper_portfolio:{row.get('latest_mark_source') or 'mark'}",
            status=row.get("status"),
        )
    return prices


def _enrich_option_position_risk(
    row: dict[str, Any],
    *,
    underlying_prices: dict[str, dict[str, Any]],
    as_of: date | None = None,
) -> dict[str, Any]:
    enriched = dict(row)
    symbol = str(enriched.get("symbol") or "").strip().upper()
    side = str(enriched.get("side") or _option_side(enriched.get("position"))).lower()
    right = str(enriched.get("right") or "").upper()
    strike = _num(enriched.get("strike"))
    dte = _option_dte(enriched.get("expiry"), as_of=as_of)
    expiry_risk, expiry_alert = _option_expiry_risk(dte)
    alerts: list[str] = []
    risk_level = "green"

    if expiry_alert:
        alerts.append(expiry_alert)
    if expiry_risk in {"expired", "expires_today"}:
        risk_level = _risk_level_worse(risk_level, "red")
    elif expiry_risk in {"unknown", "expires_soon", "near_expiry"}:
        risk_level = _risk_level_worse(risk_level, "yellow")

    price_info = underlying_prices.get(symbol) or {}
    underlying_price = _num(price_info.get("price"))
    underlying_vs_strike_pct = None
    moneyness_pct = None
    intrinsic_value = None
    in_the_money = None
    if underlying_price is None or strike in (None, 0):
        alerts.append("underlying_price_unavailable_for_moneyness")
        risk_level = _risk_level_worse(risk_level, "yellow")
    else:
        underlying_vs_strike_pct = round((underlying_price / strike - 1.0) * 100.0, 3)
        if right == "C":
            in_the_money = underlying_price > strike
            intrinsic_value = round(max(0.0, underlying_price - strike), 4)
            moneyness_pct = underlying_vs_strike_pct
        elif right == "P":
            in_the_money = underlying_price < strike
            intrinsic_value = round(max(0.0, strike - underlying_price), 4)
            moneyness_pct = round((strike / underlying_price - 1.0) * 100.0, 3) if underlying_price else None
        else:
            alerts.append("option_right_unknown_for_moneyness")
            risk_level = _risk_level_worse(risk_level, "yellow")

    if side == "short":
        if in_the_money:
            alerts.append("short_option_assignment_risk")
            risk_level = _risk_level_worse(risk_level, "red" if dte is None or dte <= 7 else "yellow")
        if dte is not None and dte <= 3:
            alerts.append("short_option_expiry_risk")
            risk_level = _risk_level_worse(risk_level, "red")
    elif side == "long" and dte is not None and dte <= 3:
        alerts.append("long_option_time_decay_risk")
        risk_level = _risk_level_worse(risk_level, "yellow")

    enriched.update(
        {
            "dte": dte,
            "expiry_risk": expiry_risk,
            "underlying_price": underlying_price,
            "underlying_price_source": price_info.get("source"),
            "underlying_price_delayed": price_info.get("delayed"),
            "underlying_vs_strike_pct": underlying_vs_strike_pct,
            "moneyness_pct": moneyness_pct,
            "intrinsic_value": intrinsic_value,
            "in_the_money": in_the_money,
            "position_risk_level": risk_level,
            "position_risk_alerts": list(dict.fromkeys(alerts)),
        }
    )
    return enriched


def _option_position_risk_summary(
    positions: list[dict[str, Any]],
    *,
    quote_gate: dict[str, Any],
) -> dict[str, Any]:
    level_counts = Counter(str(row.get("position_risk_level") or "unknown") for row in positions)
    alert_counts = Counter(
        alert
        for row in positions
        for alert in (row.get("position_risk_alerts") or [])
    )
    quote_assessment_blocked = bool(
        quote_gate.get("option_quote_deferred")
        or int(_num(quote_gate.get("best_option_quoted_count")) or 0) <= 0
        or str(quote_gate.get("gate_action") or "").startswith("block")
    )
    return {
        "version": "option-position-risk-alerts-v1",
        "position_count": len(positions),
        "risk_level_counts": dict(level_counts),
        "alert_counts": dict(alert_counts),
        "red_count": level_counts.get("red", 0),
        "yellow_count": level_counts.get("yellow", 0),
        "green_count": level_counts.get("green", 0),
        "short_option_count": sum(1 for row in positions if row.get("side") == "short"),
        "short_itm_count": sum(1 for row in positions if row.get("side") == "short" and row.get("in_the_money") is True),
        "expiring_soon_count": sum(1 for row in positions if (row.get("dte") is not None and row.get("dte") <= 3)),
        "near_expiry_count": sum(1 for row in positions if (row.get("dte") is not None and row.get("dte") <= 7)),
        "moneyness_unavailable_count": alert_counts.get("underlying_price_unavailable_for_moneyness", 0),
        "quote_assessment_blocked": quote_assessment_blocked,
        "quote_assessment_alert": "option_quote_unavailable_for_risk_assessment" if quote_assessment_blocked and positions else None,
    }


def _infer_option_structure(legs: list[dict[str, Any]]) -> str:
    if not legs:
        return "none"
    if len(legs) == 1:
        right = str(legs[0].get("right") or "").upper()
        side = str(legs[0].get("side") or _option_side(legs[0].get("quantity")))
        if right == "C":
            return f"{side}_call"
        if right == "P":
            return f"{side}_put"
        return f"{side}_option"
    symbols = {str(leg.get("symbol") or "") for leg in legs}
    expiries = {str(leg.get("expiry") or leg.get("expiration") or "") for leg in legs}
    rights = {str(leg.get("right") or "").upper() for leg in legs}
    strikes = {_num(leg.get("strike")) for leg in legs}
    sides = {str(leg.get("side") or _option_side(leg.get("quantity"))) for leg in legs}
    if len(legs) == 2 and len(symbols) == 1 and len(expiries) == 1 and len(rights) == 1 and "long" in sides and "short" in sides:
        return "vertical_spread"
    if len(legs) == 2 and len(symbols) == 1 and len(expiries) == 1 and rights == {"C", "P"} and len(strikes) == 1:
        return "straddle"
    if len(legs) == 2 and len(symbols) == 1 and len(expiries) == 1 and rights == {"C", "P"}:
        return "strangle"
    if len(legs) == 2 and len(symbols) == 1 and len(rights) == 1 and len(strikes) == 1 and len(expiries) > 1:
        return "calendar_spread"
    if len(legs) >= 3:
        return "multi_leg_complex"
    return "multi_leg_option"


def _normalise_option_leg(leg: dict[str, Any], *, fallback_symbol: Any = None) -> dict[str, Any]:
    quantity = leg.get("quantity", leg.get("qty", leg.get("ratio")))
    action = str(leg.get("action") or leg.get("side") or "").upper()
    side = str(leg.get("side") or "").lower()
    if side not in {"long", "short"}:
        if action in {"BUY", "BOT", "LONG"}:
            side = "long"
        elif action in {"SELL", "SLD", "SHORT", "SELL_SHORT"}:
            side = "short"
        else:
            side = _option_side(quantity)
    return {
        "symbol": leg.get("symbol") or leg.get("underlying") or fallback_symbol,
        "expiry": leg.get("expiry") or leg.get("expiration") or leg.get("last_trade_date_or_contract_month"),
        "right": leg.get("right") or leg.get("call_put"),
        "strike": leg.get("strike"),
        "side": side,
        "action": action or ("BUY" if side == "long" else "SELL" if side == "short" else None),
        "quantity": quantity,
        "ratio": leg.get("ratio"),
        "bid": leg.get("bid"),
        "ask": leg.get("ask"),
        "mid": leg.get("mid") or leg.get("mark"),
        "delta": leg.get("delta"),
        "gamma": leg.get("gamma"),
        "theta": leg.get("theta"),
        "vega": leg.get("vega"),
        "dte": leg.get("dte"),
        "expiry_risk": leg.get("expiry_risk"),
        "underlying_price": leg.get("underlying_price"),
        "underlying_price_source": leg.get("underlying_price_source"),
        "in_the_money": leg.get("in_the_money"),
        "moneyness_pct": leg.get("moneyness_pct"),
        "position_risk_level": leg.get("position_risk_level"),
        "position_risk_alerts": leg.get("position_risk_alerts") or [],
    }


def _extract_option_legs(item: dict[str, Any], *, fallback_symbol: Any = None) -> list[dict[str, Any]]:
    for key in ("legs", "option_legs", "strategy_legs", "spread_legs", "contracts"):
        value = item.get(key)
        if isinstance(value, list):
            return [
                _normalise_option_leg(leg, fallback_symbol=fallback_symbol)
                for leg in value
                if isinstance(leg, dict)
            ][:12]
    return []


def _strategy_risk_summary(item: dict[str, Any]) -> dict[str, Any]:
    nested = item.get("risk") if isinstance(item.get("risk"), dict) else {}
    return {
        "net_debit": item.get("net_debit", nested.get("net_debit")),
        "net_credit": item.get("net_credit", nested.get("net_credit")),
        "max_loss": item.get("max_loss") or item.get("max_risk") or nested.get("max_loss") or nested.get("max_risk"),
        "max_profit": item.get("max_profit") or item.get("max_reward") or nested.get("max_profit") or nested.get("max_reward"),
        "reward_to_risk": item.get("reward_to_risk", nested.get("reward_to_risk")),
        "break_even": item.get("break_even") or item.get("breakeven") or nested.get("break_even") or nested.get("breakeven"),
        "probability": item.get("probability") or item.get("probability_of_profit") or nested.get("probability") or nested.get("probability_of_profit"),
        "delta": item.get("delta", nested.get("delta")),
        "gamma": item.get("gamma", nested.get("gamma")),
        "theta": item.get("theta", nested.get("theta")),
        "vega": item.get("vega", nested.get("vega")),
    }


def _execution_safety_cache_summary(payload: dict[str, Any] | None) -> dict[str, Any]:
    data = _result(payload)
    if not data:
        return {}
    report = data.get("final_report") if isinstance(data.get("final_report"), dict) else data
    cache_reports = report.get("cache_reports") or []
    cache_by_key = {
        str(item.get("cache_key") or ""): item
        for item in cache_reports
        if isinstance(item, dict)
    }
    option_cache = cache_by_key.get("option_quote_liquidity_cache") or {}
    option_status = option_cache.get("status_summary") or {}
    option_quote_gate = {
        "cache_key": option_cache.get("cache_key"),
        "cache_state": option_cache.get("cache_state"),
        "fresh": option_cache.get("fresh"),
        "age_seconds": option_cache.get("age_seconds"),
        "max_age_seconds": option_cache.get("max_age_seconds"),
        "selected_path": option_cache.get("selected_path"),
        "gate_action": option_cache.get("gate_action"),
        "regular_session_dependent": option_cache.get("regular_session_dependent"),
        "best_option_quoted_count": option_status.get("best_option_quoted_count"),
        "option_quote_deferred": option_status.get("option_quote_deferred"),
        "option_quote_session": option_status.get("option_quote_session"),
        "strict_ledger_pending_count": option_status.get("strict_ledger_pending_count"),
        "strict_ledger_blocked_count": option_status.get("strict_ledger_blocked_count"),
        "availability_block_reasons": option_cache.get("availability_block_reasons") or [],
        "warnings": option_cache.get("warnings") or [],
        "violations": option_cache.get("violations") or [],
        "refresh_action_status": (option_cache.get("refresh_action") or {}).get("status"),
    }
    return {
        "version": report.get("execution_safety_cache_supervisor_version") or data.get("execution_safety_cache_supervisor_loop_version"),
        "generated_at": report.get("generated_at") or data.get("completed_at") or data.get("generated_at"),
        "age_seconds": _age_seconds(report.get("generated_at") or data.get("completed_at") or data.get("generated_at")),
        "status": report.get("status") or data.get("final_status"),
        "cache_ready_for_all_dependent_order_paths": report.get("cache_ready_for_all_dependent_order_paths") if report.get("cache_ready_for_all_dependent_order_paths") is not None else data.get("final_cache_ready_for_all_dependent_order_paths"),
        "blocked_order_paths": report.get("blocked_order_paths") or data.get("final_blocked_order_paths") or [],
        "cache_state_counts": report.get("cache_state_counts") or data.get("final_cache_state_counts") or {},
        "warnings": report.get("warnings") or [],
        "violations": report.get("violations") or [],
        "option_quote_gate": option_quote_gate,
    }


def _options_market_structure_summary(payload: dict[str, Any] | None) -> dict[str, Any]:
    data = _result(payload)
    if not data:
        return {}
    symbols = data.get("symbols") or []
    return {
        "version": data.get("options_market_structure_version"),
        "generated_at": data.get("generated_at"),
        "age_seconds": _age_seconds(data.get("generated_at")),
        "source_quote_snapshot_version": data.get("source_quote_snapshot_version"),
        "source_quote_generated_at": data.get("source_quote_generated_at"),
        "status": data.get("status"),
        "quote_count": data.get("quote_count"),
        "symbol_count": data.get("symbol_count"),
        "fallback_to_volume": data.get("fallback_to_volume"),
        "pin_near_pct": data.get("pin_near_pct"),
        "proximity_near_pct": data.get("proximity_near_pct"),
        "session_guard": data.get("session_guard") or {},
        "summary": data.get("summary") or {},
        "symbols": symbols[:25],
        "scorer_overlay": data.get("scorer_overlay") or {},
        "safety": data.get("safety") or {},
    }


def _options_market_structure_supervisor_summary(payload: dict[str, Any] | None) -> dict[str, Any]:
    data = _result(payload)
    if not data:
        return {}
    latest_tick = (data.get("tick_summaries") or [{}])[-1] if isinstance(data.get("tick_summaries"), list) else {}
    return {
        "version": data.get("options_market_structure_supervisor_version"),
        "completed_at": data.get("completed_at"),
        "age_seconds": _age_seconds(data.get("completed_at")),
        "final_status": data.get("final_status"),
        "final_session": data.get("final_session"),
        "final_session_refresh_phase": data.get("final_session_refresh_phase"),
        "final_session_refresh_priority": data.get("final_session_refresh_priority"),
        "final_effective_max_snapshot_age_minutes": data.get("final_effective_max_snapshot_age_minutes"),
        "final_option_quote_allowed": data.get("final_option_quote_allowed"),
        "final_quote_fresh": data.get("final_quote_fresh"),
        "final_quote_usable": data.get("final_quote_usable"),
        "final_quote_quality_status": data.get("final_quote_quality_status"),
        "final_quote_quality_score": data.get("final_quote_quality_score"),
        "final_quote_data_latency_status": data.get("final_quote_data_latency_status"),
        "final_quote_delayed_contracts": data.get("final_quote_delayed_contracts"),
        "final_quote_delayed_symbols": data.get("final_quote_delayed_symbols") or [],
        "final_quote_quality_reason_counts": data.get("final_quote_quality_reason_counts") or {},
        "final_quote_quality_reason_summary": data.get("final_quote_quality_reason_summary") or [],
        "final_quote_dte_bucket_counts": data.get("final_quote_dte_bucket_counts") or {},
        "final_structure_status": data.get("final_structure_status"),
        "final_optional_index_quote_fresh": data.get("final_optional_index_quote_fresh"),
        "final_optional_index_quote_usable": data.get("final_optional_index_quote_usable"),
        "final_optional_index_quote_quality_status": data.get("final_optional_index_quote_quality_status"),
        "final_optional_index_effective_quote_quality_status": data.get("final_optional_index_effective_quote_quality_status"),
        "final_optional_index_effective_quote_quality_display_status": data.get("final_optional_index_effective_quote_quality_display_status"),
        "final_optional_index_effective_quote_quality_reason": data.get("final_optional_index_effective_quote_quality_reason"),
        "final_optional_index_quote_quality_score": data.get("final_optional_index_quote_quality_score"),
        "final_optional_index_quote_data_latency_status": data.get("final_optional_index_quote_data_latency_status"),
        "final_optional_index_quote_delayed_contracts": data.get("final_optional_index_quote_delayed_contracts"),
        "final_optional_index_quote_delayed_symbols": data.get("final_optional_index_quote_delayed_symbols") or [],
        "final_optional_index_quote_quality_reason_counts": data.get("final_optional_index_quote_quality_reason_counts") or {},
        "final_optional_index_quote_quality_reason_summary": data.get("final_optional_index_quote_quality_reason_summary") or [],
        "final_optional_index_quote_dte_bucket_counts": data.get("final_optional_index_quote_dte_bucket_counts") or {},
        "final_optional_index_structure_status": data.get("final_optional_index_structure_status"),
        "final_action_status_counts": data.get("final_action_status_counts") or {},
        "final_failure_reason_counts": data.get("final_failure_reason_counts") or {},
        "final_artifact_delta": data.get("final_artifact_delta") or {},
        "final_optional_index_artifact_delta": data.get("final_optional_index_artifact_delta") or {},
        "final_index_option_coverage_status": data.get("final_index_option_coverage_status"),
        "final_index_option_confidence_counts": data.get("final_index_option_confidence_counts") or {},
        "index_option_coverage_matrix": data.get("index_option_coverage_matrix") or {},
        "index_option_capability_cache": data.get("index_option_capability_cache") or {},
        "final_quote_contract_budgets": data.get("final_quote_contract_budgets") or {},
        "index_family_quote_budget_allocation": data.get("index_family_quote_budget_allocation") or {},
        "index_family_budget_tuning_observations": data.get("index_family_budget_tuning_observations") or [],
        "m220_family_issue_summary": data.get("m220_family_issue_summary") or {},
        "m220_family_repair_plan": data.get("m220_family_repair_plan") or [],
        "option_quote_deferred_operating_state": data.get("option_quote_deferred_operating_state") or {},
        "m220_operational_runbook": data.get("m220_operational_runbook") or {},
        "m220_live_validation_checklist": data.get("m220_live_validation_checklist") or [],
        "symbol_failure_notes": data.get("symbol_failure_notes") or {},
        "symbol_status": data.get("symbol_status") or {},
        "isolated_symbols_by_action": data.get("isolated_symbols_by_action") or {},
        "isolated_symbol_count": data.get("isolated_symbol_count") or len(data.get("isolated_symbols_by_action") or {}),
        "failed_action_count": len(data.get("failed_actions") or []),
        "latest_actions": (latest_tick.get("actions") or [])[:8],
        "config": data.get("config") or {},
        "safety": data.get("safety") or {},
    }


def _single_stock_option_watchlist_summary(payload: dict[str, Any] | None) -> dict[str, Any]:
    data = _result(payload)
    if not data:
        return {}
    rows = []
    for row in data.get("candidate_rows") or []:
        if not isinstance(row, dict):
            continue
        q = row.get("quote_quality") if isinstance(row.get("quote_quality"), dict) else {}
        s = row.get("structure_quality") if isinstance(row.get("structure_quality"), dict) else {}
        o = row.get("outcome_quality") if isinstance(row.get("outcome_quality"), dict) else {}
        rows.append(
            {
                "symbol": row.get("symbol"),
                "tier": row.get("tier"),
                "score": row.get("score"),
                "ttl_minutes": row.get("ttl_minutes"),
                "expires_at": row.get("expires_at"),
                "themes": row.get("themes") or [],
                "trade_permission": row.get("trade_permission") or q.get("trade_permission"),
                "quote_quality_score": q.get("quote_quality_score"),
                "oi_proxy_confidence": q.get("oi_proxy_confidence"),
                "gamma_regime": s.get("gamma_regime"),
                "structure_confidence": s.get("confidence"),
                "outcome_completed": o.get("completed"),
                "outcome_pending": o.get("pending"),
                "outcome_blocked": o.get("blocked"),
                "promotion_status": row.get("promotion_status"),
                "source_counts": row.get("source_counts") or {},
            }
        )
    return {
        "version": data.get("watchlist_version"),
        "generated_at": data.get("generated_at"),
        "age_seconds": _age_seconds(data.get("generated_at")),
        "status": data.get("status"),
        "selected_symbols": data.get("selected_symbols") or [],
        "selected_count": data.get("selected_count"),
        "tier_counts": data.get("tier_counts") or {},
        "action_status_counts": data.get("action_status_counts") or {},
        "adaptive_feedback": data.get("adaptive_feedback") or {},
        "policy": data.get("policy") or {},
        "rows": rows,
        "safety": data.get("safety") or {},
    }


def _option_readiness_summary(
    *,
    option_quote_regular_dependent: bool,
    ticketing_allowed: bool,
    market_data_risk_gate: dict[str, Any],
    market_data_operating_auto_gate: dict[str, Any],
    execution_safety_cache: dict[str, Any],
    base_block_reasons: list[str],
    advanced_strategy_count: int,
    protective_candidate_count: int,
) -> dict[str, Any]:
    quote_gate = execution_safety_cache.get("option_quote_gate") or {}
    blocked_order_paths = set(execution_safety_cache.get("blocked_order_paths") or [])
    availability_block_reasons = list(quote_gate.get("availability_block_reasons") or [])
    score = 100
    reasons = list(dict.fromkeys(base_block_reasons))
    checks: list[dict[str, Any]] = []

    cache_loaded = bool(execution_safety_cache)
    option_cache_fresh = bool(quote_gate.get("fresh"))
    quoted_count = int(_num(quote_gate.get("best_option_quoted_count")) or 0)
    option_deferred = bool(quote_gate.get("option_quote_deferred"))
    hedge_allowed = bool((market_data_operating_auto_gate.get("defensive_mode") or {}).get("hedge_or_defensive_entries_allowed", False))
    risk_gate_allows_entries = bool(market_data_risk_gate.get("new_entries_allowed", False))
    quote_gate_action = str(quote_gate.get("gate_action") or "")
    option_path_blocked = bool(
        {"new_option_entries", "protective_option_ticketing_without_fresh_quotes"} & blocked_order_paths
        or quote_gate_action.startswith("block")
    )

    def add_check(name: str, passed: bool, impact: int, reason: str, value: Any = None) -> None:
        nonlocal score
        checks.append({"name": name, "passed": passed, "impact": impact, "reason": reason, "value": value})
        if not passed:
            score -= impact
            reasons.append(reason)

    add_check("safety_cache_loaded", cache_loaded, 20, "execution_safety_cache_not_loaded", execution_safety_cache.get("status"))
    add_check("option_quote_cache_fresh", option_cache_fresh, 25, "option_quote_cache_not_fresh", quote_gate.get("cache_state"))
    add_check("option_bid_ask_available", quoted_count > 0, 35, "no_fresh_option_bid_ask_quotes", quoted_count)
    add_check("option_quote_session_open", not option_deferred and not option_quote_regular_dependent, 20, "option_quote_deferred_or_regular_session_dependent", quote_gate.get("option_quote_session"))
    add_check("protective_ticketing_allowed", ticketing_allowed, 15, "protective_option_ticketing_disabled", ticketing_allowed)
    add_check("market_data_risk_gate_allows_entries", risk_gate_allows_entries, 15, "market_data_risk_gate_blocks_new_entries", market_data_risk_gate.get("gate_status"))
    add_check("hedge_or_defensive_entries_allowed", hedge_allowed, 10, "hedge_or_defensive_entries_disabled", hedge_allowed)
    add_check("safety_cache_option_paths_open", not option_path_blocked, 25, "safety_cache_blocks_option_paths", sorted(blocked_order_paths))
    add_check("advanced_or_protective_candidates_present", (advanced_strategy_count + protective_candidate_count) > 0, 5, "no_option_strategy_candidates_present", advanced_strategy_count + protective_candidate_count)

    for reason in availability_block_reasons:
        reasons.append(str(reason))
    for warning in quote_gate.get("warnings") or []:
        reasons.append(str(warning))
    for violation in quote_gate.get("violations") or execution_safety_cache.get("violations") or []:
        reasons.append(str(violation))
        score -= 20

    score = max(0, min(100, score))
    if option_deferred or quoted_count <= 0:
        score = min(score, 40)
    if option_path_blocked:
        score = min(score, 45)
    if not ticketing_allowed:
        score = min(score, 60)
    if score >= 80:
        level = "ready"
    elif score >= 50:
        level = "watch"
    else:
        level = "blocked"
    return {
        "score": score,
        "level": level,
        "checks": checks,
        "reasons": list(dict.fromkeys(reason for reason in reasons if reason)),
        "option_quote_gate": quote_gate,
        "option_path_blocked": option_path_blocked,
        "quoted_contract_count": quoted_count,
        "safety_cache_status": execution_safety_cache.get("status"),
    }


def _effective_option_quote_session_context(
    *,
    market_data_session_overlay: dict[str, Any],
    quote_gate: dict[str, Any],
) -> dict[str, Any]:
    overlay_session_name = market_data_session_overlay.get("session_name")
    overlay_regular_dependent = bool(market_data_session_overlay.get("option_quotes_regular_session_dependent"))
    quote_session_name = quote_gate.get("option_quote_session")
    quote_deferred = bool(quote_gate.get("option_quote_deferred"))
    quoted_count = int(_num(quote_gate.get("best_option_quoted_count")) or 0)
    quote_gate_fresh = bool(quote_gate.get("fresh"))

    session_name = quote_session_name or overlay_session_name
    regular_dependent = overlay_regular_dependent
    source = "market_data_session_overlay"

    if quote_session_name:
        source = "option_quote_gate"
        if str(quote_session_name).lower() == "regular" and not quote_deferred and (quote_gate_fresh or quoted_count > 0):
            regular_dependent = False
        elif quote_deferred or str(quote_session_name).lower() in {"pre_market", "after_hours", "overnight", "closed"}:
            regular_dependent = True

    return {
        "option_session_name": session_name,
        "option_quotes_regular_session_dependent": regular_dependent,
        "source": source,
        "overlay_option_session_name": overlay_session_name,
        "overlay_option_quotes_regular_session_dependent": overlay_regular_dependent,
        "quote_gate_option_session_name": quote_session_name,
        "quote_gate_option_quote_deferred": quote_deferred,
        "quote_gate_fresh": quote_gate_fresh,
        "quote_gate_quoted_contract_count": quoted_count,
    }


def _options_desk_summary(
    *,
    account: dict[str, Any],
    paper_portfolio: dict[str, Any],
    plan: dict[str, Any],
    market_data: dict[str, Any],
    market_data_session_overlay: dict[str, Any],
    market_data_risk_gate: dict[str, Any],
    market_data_operating_auto_gate: dict[str, Any],
    operating_defensive_action_plan: dict[str, Any],
    operating_defensive_intent_ledger: dict[str, Any],
    execution_safety_cache: dict[str, Any],
) -> dict[str, Any]:
    underlying_prices = _underlying_price_map(market_data=market_data, paper_portfolio=paper_portfolio)
    quote_gate = execution_safety_cache.get("option_quote_gate") or {}
    as_of = datetime.now(UTC).date()
    account_option_positions: list[dict[str, Any]] = []
    for row in account.get("positions") or []:
        if str(row.get("sec_type") or "").upper() != "OPT":
            continue
        position = {
            **row,
            "side": _option_side(row.get("position")),
            "contract_label": _option_contract_label(row),
        }
        account_option_positions.append(
            _enrich_option_position_risk(position, underlying_prices=underlying_prices, as_of=as_of)
        )

    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in account_option_positions:
        groups.setdefault(_option_position_key(row), []).append(row)
    position_strategy_groups: list[dict[str, Any]] = []
    for (symbol, expiry), legs in sorted(groups.items()):
        normalised_legs = [
            _normalise_option_leg(
                {
                    "symbol": leg.get("symbol"),
                    "expiry": leg.get("expiry"),
                    "right": leg.get("right"),
                    "strike": leg.get("strike"),
                    "quantity": leg.get("position"),
                    "dte": leg.get("dte"),
                    "expiry_risk": leg.get("expiry_risk"),
                    "underlying_price": leg.get("underlying_price"),
                    "underlying_price_source": leg.get("underlying_price_source"),
                    "in_the_money": leg.get("in_the_money"),
                    "moneyness_pct": leg.get("moneyness_pct"),
                    "position_risk_level": leg.get("position_risk_level"),
                    "position_risk_alerts": leg.get("position_risk_alerts") or [],
                },
                fallback_symbol=symbol,
            )
            for leg in legs
        ]
        group_alerts = list(dict.fromkeys(alert for leg in normalised_legs for alert in (leg.get("position_risk_alerts") or [])))
        group_risk_level = "green"
        for leg in normalised_legs:
            group_risk_level = _risk_level_worse(group_risk_level, leg.get("position_risk_level"))
        dte_values = [leg.get("dte") for leg in normalised_legs if leg.get("dte") is not None]
        position_strategy_groups.append(
            {
                "strategy_id": f"{symbol}:{expiry}",
                "symbol": symbol,
                "expiry": expiry,
                "leg_count": len(normalised_legs),
                "structure": _infer_option_structure(normalised_legs),
                "legs": normalised_legs,
                "net_contracts": sum((_num(leg.get("position")) or 0.0) for leg in legs),
                "risk_level": group_risk_level,
                "risk_alerts": group_alerts,
                "min_dte": min(dte_values) if dte_values else None,
                "short_leg_count": sum(1 for leg in normalised_legs if leg.get("side") == "short"),
                "short_itm_count": sum(1 for leg in normalised_legs if leg.get("side") == "short" and leg.get("in_the_money") is True),
            }
        )

    option_plan_intents = [
        item
        for item in plan.get("intents") or []
        if str(item.get("ticket_type") or "").lower() in {"option", "options", "option_spread", "multi_leg_option"}
    ][:25]
    protective_packet = market_data_operating_auto_gate.get("protective_analysis_packet") or {}
    protective_candidates = []
    for item in protective_packet.get("sample_candidates") or []:
        legs = _extract_option_legs(item, fallback_symbol=item.get("symbol"))
        protective_candidates.append(
            {
                "symbol": item.get("symbol"),
                "source_action": item.get("source_action"),
                "strategy": item.get("strategy") or _infer_option_structure(legs),
                "analysis_status": item.get("analysis_status"),
                "ticketing_allowed": item.get("ticketing_allowed"),
                "reason": item.get("reason"),
                "leg_count": len(legs),
                "legs": legs,
                "risk": _strategy_risk_summary(item),
            }
        )
        if len(protective_candidates) >= 20:
            break

    advanced_strategy_rows = []
    for item in operating_defensive_action_plan.get("top_actions") or []:
        strategy = str(item.get("strategy") or "")
        legs = _extract_option_legs(item, fallback_symbol=item.get("symbol"))
        if item.get("requires_option_chain") or legs or "option" in strategy or "spread" in strategy or "put" in strategy or "call" in strategy:
            advanced_strategy_rows.append(
                {
                    "source": "defensive_action_plan",
                    "symbol": item.get("symbol"),
                    "action_type": item.get("action_type"),
                    "strategy": item.get("strategy") or _infer_option_structure(legs),
                    "priority_score": item.get("priority_score"),
                    "ticketing_allowed": item.get("ticketing_allowed"),
                    "requires_option_chain": item.get("requires_option_chain"),
                    "requires_margin_whatif": item.get("requires_margin_whatif"),
                    "leg_count": len(legs),
                    "legs": legs,
                    "risk": _strategy_risk_summary(item),
                    "reason": item.get("reason"),
                }
            )
    for item in operating_defensive_intent_ledger.get("defensive_intents") or []:
        strategy = str(item.get("strategy") or "")
        legs = _extract_option_legs(item, fallback_symbol=item.get("symbol"))
        if legs or "option" in strategy or "spread" in strategy or "put" in strategy or "call" in strategy:
            advanced_strategy_rows.append(
                {
                    "source": "defensive_intent_ledger",
                    "symbol": item.get("symbol"),
                    "state": item.get("state"),
                    "strategy": item.get("strategy") or _infer_option_structure(legs),
                    "priority_score": item.get("priority_score"),
                    "block_reasons": item.get("block_reasons") or [],
                    "paper_action": item.get("paper_action"),
                    "quantity": item.get("quantity"),
                    "leg_count": len(legs),
                    "legs": legs,
                    "risk": _strategy_risk_summary(item),
                }
            )
    option_session_context = _effective_option_quote_session_context(
        market_data_session_overlay=market_data_session_overlay,
        quote_gate=quote_gate,
    )
    option_quote_regular_dependent = bool(option_session_context.get("option_quotes_regular_session_dependent"))
    ticketing_allowed = bool(protective_packet.get("ticketing_allowed"))
    new_option_entries_allowed = (
        bool(market_data_risk_gate.get("new_entries_allowed", False))
        and bool((market_data_operating_auto_gate.get("defensive_mode") or {}).get("hedge_or_defensive_entries_allowed", False))
        and ticketing_allowed
        and not option_quote_regular_dependent
    )
    block_reasons: list[str] = []
    if option_quote_regular_dependent:
        block_reasons.append("option_quotes_regular_session_dependent")
    if not ticketing_allowed:
        block_reasons.append("protective_option_ticketing_disabled")
    if not market_data_risk_gate.get("new_entries_allowed", False):
        block_reasons.append("market_data_risk_gate_blocks_new_entries")
    safety_block_paths = set(execution_safety_cache.get("blocked_order_paths") or [])
    if "new_option_entries" in safety_block_paths:
        block_reasons.append("safety_cache_blocks_new_option_entries")
    if "protective_option_ticketing_without_fresh_quotes" in safety_block_paths:
        block_reasons.append("safety_cache_blocks_protective_option_ticketing")
    readiness = _option_readiness_summary(
        option_quote_regular_dependent=option_quote_regular_dependent,
        ticketing_allowed=ticketing_allowed,
        market_data_risk_gate=market_data_risk_gate,
        market_data_operating_auto_gate=market_data_operating_auto_gate,
        execution_safety_cache=execution_safety_cache,
        base_block_reasons=block_reasons,
        advanced_strategy_count=len(advanced_strategy_rows),
        protective_candidate_count=protective_packet.get("candidate_count") or len(protective_candidates),
    )
    new_option_entries_allowed = new_option_entries_allowed and readiness.get("level") == "ready"
    position_risk_summary = _option_position_risk_summary(
        account_option_positions,
        quote_gate=readiness.get("option_quote_gate") or quote_gate,
    )
    return {
        "version": "options-desk-dashboard-v3-position-risk",
        "account_option_position_count": len(account_option_positions),
        "account_option_positions": account_option_positions[:50],
        "position_strategy_group_count": len(position_strategy_groups),
        "position_strategy_groups": position_strategy_groups[:20],
        "position_risk_summary": position_risk_summary,
        "position_risk_alert_count": sum(position_risk_summary.get("alert_counts", {}).values()) if isinstance(position_risk_summary.get("alert_counts"), dict) else 0,
        "option_plan_intent_count": len(option_plan_intents),
        "option_plan_intents": option_plan_intents,
        "protective_candidate_count": protective_packet.get("candidate_count") or len(protective_candidates),
        "protective_ticketing_allowed": ticketing_allowed,
        "protective_candidates": protective_candidates,
        "advanced_strategy_count": len(advanced_strategy_rows),
        "advanced_strategies": advanced_strategy_rows[:25],
        "allowed_option_structures": protective_packet.get("allowed_option_structures") or (market_data_operating_auto_gate.get("defensive_mode") or {}).get("allowed_option_structures") or [],
        "option_strategy_bias": market_data_risk_gate.get("option_strategy_bias"),
        "option_quotes_regular_session_dependent": option_quote_regular_dependent,
        "option_session_name": option_session_context.get("option_session_name"),
        "option_session_context": option_session_context,
        "next_regular_open": (market_data_session_overlay.get("next_regular_open") or {}).get("next_open_local"),
        "new_option_entries_allowed": new_option_entries_allowed,
        "blocked_order_paths": block_reasons,
        "readiness": readiness,
        "readiness_score": readiness.get("score"),
        "readiness_level": readiness.get("level"),
        "readiness_reasons": readiness.get("reasons") or [],
        "quote_gate": readiness.get("option_quote_gate") or {},
        "execution_safety_cache": execution_safety_cache,
        "safety": {
            "dashboard_only": True,
            "ticket_created": False,
            "ibkr_order_submitted": False,
            "live_order_transmitted": False,
        },
    }


def _mark_quality_summary(payload: dict[str, Any] | None) -> dict[str, Any]:
    data = _result(payload)
    return {
        "version": data.get("paper_cycle_watch_daemon_version"),
        "generated_at": data.get("generated_at"),
        "age_seconds": _age_seconds(data.get("generated_at")),
        "daemon_run_id": data.get("daemon_run_id"),
        "executed_tick_count": data.get("executed_tick_count"),
        "skipped_tick_count": data.get("skipped_tick_count"),
        "preflight_skipped_tick_count": data.get("preflight_skipped_tick_count"),
        "mark_refresh_skipped_tick_count": data.get("mark_refresh_skipped_tick_count"),
        "quality": data.get("mark_refresh_quality_summary") or {},
        "final": data.get("final_daemon_summary") or {},
        "batches": data.get("batch_summaries") or [],
    }


def _economic_summary(payload: dict[str, Any] | None) -> dict[str, Any]:
    data = _result(payload)
    indicators = data.get("indicators") or []
    return {
        "desk_version": data.get("desk_version"),
        "universe": data.get("universe"),
        "cutoff_date": data.get("cutoff_date"),
        "series_count": data.get("series_count"),
        "covered_series_count": data.get("covered_series_count"),
        "missing_series_count": data.get("missing_series_count"),
        "freshness_counts": data.get("freshness_counts") or {},
        "risk_bias": data.get("risk_bias"),
        "macro_flags": data.get("macro_flags") or [],
        "scorer_takeaways": data.get("scorer_takeaways") or [],
        "top_indicators": indicators[:10],
    }


def _macro_release_summary(payload: dict[str, Any] | None, calendar_payload: dict[str, Any] | None) -> dict[str, Any]:
    data = _result(payload)
    calendar = _result(calendar_payload)
    summary = data.get("summary") or {}
    latest_cluster = (data.get("clusters") or [{}])[0] if data.get("clusters") else {}
    next_event = summary.get("next_known_macro_event") or ((calendar.get("upcoming_events") or [{}])[0] if calendar.get("upcoming_events") else {})
    return {
        "version": data.get("macro_release_interpreter_version"),
        "generated_at": data.get("generated_at"),
        "generated_at_local": data.get("generated_at_local") or _local_time_text(data.get("generated_at")),
        "status": data.get("status"),
        "latest_event_line": data.get("latest_event_line"),
        "latest_macro_surprise_label": summary.get("latest_macro_surprise_label"),
        "latest_release_time_local": summary.get("latest_release_time_local"),
        "asset_bias": latest_cluster.get("asset_bias") or {},
        "market_reaction_status": (latest_cluster.get("market_reaction") or data.get("market_reaction_attachment") or {}).get("status"),
        "parsed_indicator_count": summary.get("parsed_indicator_count"),
        "cluster_count": summary.get("cluster_count"),
        "next_event": next_event,
        "calendar_version": calendar.get("macro_event_calendar_version"),
        "calendar_generated_at_local": calendar.get("generated_at_local") or _local_time_text(calendar.get("generated_at")),
        "critical_event_count": (calendar.get("summary") or {}).get("critical_event_count"),
        "risk_windows": (calendar.get("risk_windows") or [])[:5],
    }


def _latest_database_rows(database_path: Path | None, *, news_limit: int, regime_limit: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None]:
    if database_path is None:
        return [], [], "database_path_not_configured"
    resolved = database_path.expanduser().resolve()
    if not resolved.exists():
        return [], [], f"database_missing:{resolved}"
    news: list[dict[str, Any]] = []
    regimes: list[dict[str, Any]] = []
    try:
        con = sqlite3.connect(str(resolved), timeout=30)
        con.row_factory = sqlite3.Row
        try:
            rows = con.execute(
                """
                SELECT provider, headline, primary_symbol, published_at, received_at, provider_published_at
                FROM news_articles
                ORDER BY COALESCE(provider_published_at, published_at, received_at, created_at) DESC
                LIMIT ?
                """,
                (int(news_limit),),
            ).fetchall()
            news.extend(dict(row) for row in rows)
        except sqlite3.Error:
            pass
        try:
            rows = con.execute(
                """
                SELECT source, title, published_at, region, theme, sentiment_label, assets_json
                FROM external_market_signals
                ORDER BY COALESCE(published_at, created_at) DESC
                LIMIT ?
                """,
                (int(news_limit),),
            ).fetchall()
            for row in rows:
                item = dict(row)
                item["provider"] = item.pop("source", None)
                item["headline"] = item.pop("title", None)
                news.append(item)
        except sqlite3.Error:
            pass
        try:
            rows = con.execute(
                """
                SELECT regime_key, family, regime_name, directional_bias, status, region, theme,
                       started_at, last_seen_at, resolved_at, severity_score, confidence,
                       affected_assets_json, evidence_count, latest_titles_json
                FROM standing_market_regimes
                WHERE resolved_at IS NULL OR status IN ('active', 'monitor', 'escalating')
                ORDER BY COALESCE(severity_score, 0) DESC, COALESCE(last_seen_at, started_at) DESC
                LIMIT ?
                """,
                (int(regime_limit),),
            ).fetchall()
            for row in rows:
                item = dict(row)
                item.update(standing_regime_runtime_metrics(item))
                regimes.append(item)
        except sqlite3.Error:
            pass
        con.close()
    except Exception as exc:
        return news[:news_limit], regimes, f"database_read_error:{exc}"
    news.sort(key=lambda item: str(item.get("provider_published_at") or item.get("published_at") or item.get("received_at") or ""), reverse=True)
    return news[:news_limit], regimes, None


def _geopolitical_desk_summary(database_path: Path | None, *, lookback_hours: float = 72.0, limit: int = 12) -> dict[str, Any]:
    if database_path is None:
        return {"version": "geopolitical-desk-dashboard-v1", "available": False, "reason": "database_path_not_configured", "top_buckets": []}
    resolved = database_path.expanduser().resolve()
    if not resolved.exists():
        return {"version": "geopolitical-desk-dashboard-v1", "available": False, "reason": f"database_missing:{resolved}", "top_buckets": []}
    try:
        con = sqlite3.connect(str(resolved), timeout=30)
        con.row_factory = sqlite3.Row
        try:
            heatmap = build_geopolitical_heatmap_from_connection(con, lookback_hours=lookback_hours, limit=limit)
        finally:
            con.close()
    except Exception as exc:
        return {"version": "geopolitical-desk-dashboard-v1", "available": False, "reason": f"{exc.__class__.__name__}:{str(exc)[:300]}", "top_buckets": []}
    top_buckets = heatmap.get("top_buckets") or []
    return {
        **heatmap,
        "dashboard_version": "geopolitical-desk-dashboard-v1",
        "available": True,
        "hot_bucket_count": sum(1 for row in top_buckets if row.get("heat_color") in {"red", "yellow"}),
        "reason": "ok",
    }


def _market_data_summary(payload: dict[str, Any] | None) -> dict[str, Any]:
    data = _result(payload)
    if not data:
        return {}
    rows = data.get("rows") or data.get("instruments") or data.get("items") or []
    summary = data.get("summary") or data.get("desk_summary") or {}
    return {
        "version": data.get("market_data_desk_version") or data.get("desk_version"),
        "temperature_version": data.get("market_data_temperature_version"),
        "generated_at": data.get("generated_at"),
        "age_seconds": _age_seconds(data.get("generated_at")),
        "status": data.get("status") or data.get("desk_status"),
        "temperature": data.get("temperature") or {
            "label": summary.get("temperature_label"),
            "score": summary.get("temperature_score"),
            "risk_flags": summary.get("risk_flags") or [],
        },
        "summary": summary,
        "rows": rows[:50],
    }


_M220_OPTION_FAMILY_MAP: list[dict[str, Any]] = [
    {
        "family_id": "sp500",
        "label": "S&P 500",
        "market_keys": {"SPY", "SPX", "XSP", "ES", "MES"},
        "option_symbols": {"SPY", "SPX", "XSP"},
    },
    {
        "family_id": "nasdaq100",
        "label": "Nasdaq 100",
        "market_keys": {"QQQ", "NDX", "XND", "NQ", "MNQ"},
        "option_symbols": {"QQQ", "NDX", "XND"},
    },
    {
        "family_id": "russell2000",
        "label": "Russell 2000",
        "market_keys": {"IWM", "RUT", "RTY", "M2K"},
        "option_symbols": {"IWM", "RUT"},
    },
    {
        "family_id": "dow30",
        "label": "Dow / Industrials",
        "market_keys": {"DIA", "DJIA", "YM", "MYM"},
        "option_symbols": {"DIA"},
    },
    {
        "family_id": "volatility",
        "label": "Volatility",
        "market_keys": {"VIX", "VVIX", "VXX", "VIXY", "UVXY"},
        "option_symbols": {"VIX", "VXX", "VIXY", "UVXY"},
    },
]


def _m220_family_for_market_key(key: Any, group: Any = None) -> dict[str, Any] | None:
    token = str(key or "").strip().upper()
    group_text = str(group or "").lower()
    for family in _M220_OPTION_FAMILY_MAP:
        if token in family["market_keys"]:
            return family
    if "nasdaq" in group_text:
        return _M220_OPTION_FAMILY_MAP[1]
    if "russell" in group_text:
        return _M220_OPTION_FAMILY_MAP[2]
    if "dow" in group_text:
        return _M220_OPTION_FAMILY_MAP[3]
    if "volatility" in group_text or token in {"VIX", "VVIX"}:
        return _M220_OPTION_FAMILY_MAP[4]
    if "sp500" in group_text or "s&p" in group_text or token in {"ES", "MES", "SPY", "SPX", "XSP"}:
        return _M220_OPTION_FAMILY_MAP[0]
    return None


def _m220_structure_symbol_lookup(
    options_market_structure: dict[str, Any],
    options_market_structure_supervisor: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    lookup: dict[str, dict[str, Any]] = {}
    for row in options_market_structure.get("symbols") or []:
        symbol = str(row.get("symbol") or "").strip().upper()
        if symbol:
            lookup[symbol] = row
    for path_key in ("optional_index_structure_json_path", "structure_json_path"):
        path_text = str((options_market_structure_supervisor.get("config") or {}).get(path_key) or "").strip()
        if not path_text:
            continue
        payload, _error = _read_json_file(Path(path_text))
        data = _result(payload)
        for row in data.get("symbols") or []:
            symbol = str(row.get("symbol") or "").strip().upper()
            if symbol and symbol not in lookup:
                lookup[symbol] = row
    return lookup


def _m220_selected_family_sources(options_market_structure_supervisor: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for family in (options_market_structure_supervisor.get("index_option_coverage_matrix") or {}).get("families") or []:
        family_id = str(family.get("family_id") or "").strip()
        if family_id:
            rows[family_id] = family
    return rows


def _m220_option_structure_freshness(
    options_market_structure: dict[str, Any],
    options_market_structure_supervisor: dict[str, Any],
) -> dict[str, Any]:
    session_guard = options_market_structure.get("session_guard") or {}
    guard_status = str(session_guard.get("freshness_status") or "").lower()
    max_age_minutes = _num(session_guard.get("max_snapshot_age_minutes"))
    if max_age_minutes is None:
        max_age_minutes = _num(options_market_structure_supervisor.get("final_effective_max_snapshot_age_minutes"))
    if max_age_minutes is None:
        max_age_minutes = 30.0
    max_age_seconds = float(max_age_minutes) * 60.0
    structure_age = _age_seconds(options_market_structure.get("generated_at"))
    quote_age = _age_seconds(options_market_structure.get("source_quote_generated_at"))
    supervisor_age = _age_seconds(options_market_structure_supervisor.get("completed_at"))
    age_candidates = [value for value in (structure_age, quote_age, supervisor_age) if value is not None]
    observed_age = max(age_candidates) if age_candidates else None
    stale_reasons: list[str] = []
    if observed_age is None:
        stale_reasons.append("option_structure_timestamp_missing")
    elif observed_age > max_age_seconds:
        stale_reasons.append("option_structure_artifact_age_exceeded")
    if guard_status and guard_status not in {"green", "fresh", "ok"}:
        stale_reasons.append(f"session_guard_{guard_status}")
    supervisor_fresh = bool(options_market_structure_supervisor.get("final_quote_fresh"))
    supervisor_fresh_trusted = supervisor_fresh and supervisor_age is not None and supervisor_age <= max_age_seconds
    status = "fresh" if not stale_reasons and (guard_status in {"green", "fresh", "ok"} or supervisor_fresh_trusted) else "stale"
    if not options_market_structure:
        status = "missing"
        stale_reasons.append("option_structure_missing")
    session_context = _current_us_option_session_context()
    expected_deferred_stale = bool(
        status == "stale"
        and "option_structure_artifact_age_exceeded" in stale_reasons
        and not session_context.get("is_regular_option_session")
    )
    if expected_deferred_stale:
        status = "expected_deferred_stale"
    return {
        "status": status,
        "fresh": status == "fresh",
        "expected_deferred_stale": expected_deferred_stale,
        "current_option_session": session_context,
        "guard_status": guard_status or None,
        "structure_age_seconds": structure_age,
        "source_quote_age_seconds": quote_age,
        "supervisor_age_seconds": supervisor_age,
        "observed_age_seconds": round(observed_age, 3) if observed_age is not None else None,
        "max_age_seconds": round(max_age_seconds, 3),
        "max_age_minutes": max_age_minutes,
        "stale_reasons": list(dict.fromkeys(stale_reasons)),
        "supervisor_final_quote_fresh": supervisor_fresh,
        "supervisor_final_quote_fresh_trusted": supervisor_fresh_trusted,
    }


def _m220_move_option_interpretation(
    *,
    move_direction: str,
    move_magnitude_pct: float,
    structure: dict[str, Any] | None,
    family_row: dict[str, Any] | None,
    structure_fresh: bool,
    structure_freshness_status: str = "",
) -> dict[str, Any]:
    if not structure:
        return {
            "explanation_status": "insufficient_option_structure",
            "bias": "unknown",
            "confidence": "low",
            "interpretation": "No matching option-structure row is available for this market move.",
        }
    gamma_regime = str(structure.get("gamma_regime") or "neutral_or_missing_gamma")
    proximity_alerts = list(structure.get("proximity_alerts") or [])
    confidence = str(structure.get("confidence") or "unknown")
    selected_type = str((family_row or {}).get("selected_source_type") or "")
    coverage_status = str((family_row or {}).get("coverage_status") or "")
    proxy_penalty = selected_type in {"proxy_probe", "none"} or coverage_status in {"proxy_probe_only", "unavailable"}
    if not structure_fresh:
        if structure_freshness_status == "expected_deferred_stale":
            return {
                "explanation_status": "expected_deferred_option_structure_context",
                "bias": "wait_for_regular_session_refresh",
                "confidence": "low",
                "interpretation": "Option market is outside regular session; stale option structure is expected and should be refreshed when options reopen.",
            }
        return {
            "explanation_status": "stale_option_structure_context",
            "bias": "do_not_use_for_live_causality",
            "confidence": "low",
            "interpretation": "Option-structure context exists but the freshness guard is not green.",
        }
    if gamma_regime == "negative_gamma_trend_amplification_bias":
        status = "confirmed_trend_amplification"
        bias = "trend_amplification"
        interpretation = "Negative gamma context can amplify the observed market move; avoid fading it without other evidence."
    elif gamma_regime == "positive_gamma_mean_reversion_bias":
        status = "explains_mean_reversion_or_pin_risk"
        bias = "mean_reversion_or_pin"
        interpretation = "Positive gamma context can explain a newsless move as dealer hedging/pinning pressure; watch wall/max-pain levels for fade risk."
    else:
        status = "partial_context"
        bias = "neutral_or_missing_gamma"
        interpretation = "Option structure is available, but gamma regime is neutral or missing."
    if proximity_alerts:
        status = f"{status}_near_wall_or_pin"
        interpretation += " Spot is near at least one wall/flip/max-pain proximity level."
    if proxy_penalty:
        status = "partial_proxy_or_probe_context"
        confidence = "low"
        interpretation += " Family coverage is proxy/probe-backed, so treat this as lower-confidence context."
    if move_magnitude_pct < 0.25 and not proximity_alerts:
        status = "context_available_move_small"
        interpretation += " The observed move is still small, so the overlay is context rather than a strong explanation."
    return {
        "explanation_status": status,
        "bias": bias,
        "confidence": confidence,
        "interpretation": interpretation,
    }


def _m220_market_move_option_structure_overlay(
    *,
    market_data: dict[str, Any],
    options_market_structure: dict[str, Any],
    options_market_structure_supervisor: dict[str, Any],
) -> dict[str, Any]:
    structure_lookup = _m220_structure_symbol_lookup(options_market_structure, options_market_structure_supervisor)
    family_rows = _m220_selected_family_sources(options_market_structure_supervisor)
    freshness_report = _m220_option_structure_freshness(options_market_structure, options_market_structure_supervisor)
    structure_fresh = bool(freshness_report.get("fresh"))
    rows: list[dict[str, Any]] = []
    status_counts: Counter[str] = Counter()
    for row in market_data.get("rows") or []:
        key = str(row.get("key") or row.get("symbol") or "").strip().upper()
        family = _m220_family_for_market_key(key, row.get("group"))
        if not family:
            continue
        price = _num(row.get("price"))
        close = _num(row.get("close"))
        gap_pct = _num(row.get("gap_pct"))
        if gap_pct is None and price is not None and close not in (None, 0):
            gap_pct = (price / close - 1.0) * 100.0
        tick_move_pct = _num(row.get("tick_move_pct"))
        move_magnitude = max(abs(gap_pct or 0.0), abs(tick_move_pct or 0.0))
        alerts = [str(item) for item in row.get("alerts") or []]
        has_move_alert = any(item.startswith(("gap_", "sudden_move")) for item in alerts)
        if move_magnitude < 0.15 and not has_move_alert:
            continue
        direction = "up" if (gap_pct or 0.0) > 0 else "down" if (gap_pct or 0.0) < 0 else "unknown"
        family_row = family_rows.get(str(family.get("family_id") or ""))
        selected_symbols = [
            str(item).strip().upper()
            for item in ((family_row or {}).get("selected_symbols") or [])
            if str(item).strip()
        ]
        candidate_symbols = [key] + selected_symbols + sorted(family["option_symbols"])
        matched_symbol = next((symbol for symbol in candidate_symbols if symbol in structure_lookup), None)
        structure = structure_lookup.get(matched_symbol or "")
        interpretation = _m220_move_option_interpretation(
            move_direction=direction,
            move_magnitude_pct=float(move_magnitude),
            structure=structure,
            family_row=family_row,
            structure_fresh=structure_fresh,
            structure_freshness_status=str(freshness_report.get("status") or ""),
        )
        status_counts[interpretation["explanation_status"]] += 1
        rows.append(
            {
                "market_key": key,
                "family_id": family.get("family_id"),
                "family_label": family.get("label"),
                "price": price,
                "gap_pct": round(float(gap_pct), 3) if gap_pct is not None else None,
                "tick_move_pct": round(float(tick_move_pct), 3) if tick_move_pct is not None else None,
                "move_magnitude_pct": round(float(move_magnitude), 3),
                "move_direction": direction,
                "market_alerts": alerts,
                "matched_option_symbol": matched_symbol,
                "selected_source_type": (family_row or {}).get("selected_source_type"),
                "coverage_status": (family_row or {}).get("coverage_status"),
                "gamma_regime": (structure or {}).get("gamma_regime"),
                "call_wall": (structure or {}).get("call_wall"),
                "put_wall": (structure or {}).get("put_wall"),
                "max_pain": (structure or {}).get("max_pain"),
                "distance_to_pin_pct": (structure or {}).get("distance_to_pin_pct"),
                "proximity_alerts": (structure or {}).get("proximity_alerts") or [],
                **interpretation,
            }
        )
    rows.sort(key=lambda item: float(item.get("move_magnitude_pct") or 0.0), reverse=True)
    if not rows:
        overall = "no_material_index_moves"
        note = "No mapped market-data move was large enough to require option-structure interpretation."
    elif any(str(row.get("explanation_status") or "").startswith("insufficient") for row in rows):
        overall = "needs_option_structure_context"
        note = "At least one mapped market move lacks option-structure context."
    elif any("trend_amplification" in str(row.get("explanation_status") or "") for row in rows):
        overall = "option_structure_confirms_trend_risk"
        note = "At least one mapped market move aligns with negative-gamma trend-amplification context."
    elif any("mean_reversion" in str(row.get("explanation_status") or "") or "pin" in str(row.get("explanation_status") or "") for row in rows):
        overall = "option_structure_explains_mean_reversion_or_pin_risk"
        note = "At least one mapped market move has positive-gamma, wall, or pin context."
    else:
        overall = "partial_or_low_confidence_context"
        note = "Option structure is available, but current evidence is partial or low-confidence."
    return {
        "version": "m220-market-move-option-structure-overlay-v1",
        "overall_status": overall,
        "summary_note": note,
        "structure_freshness_status": freshness_report.get("status"),
        "structure_freshness": freshness_report,
        "structure_symbol_count": len(structure_lookup),
        "mapped_move_count": len(rows),
        "status_counts": dict(status_counts),
        "rows": rows[:20],
        "safety": {
            "read_only_overlay": True,
            "ibkr_order_submitted": False,
            "transmit_enabled": False,
            "live_order_routing_enabled": False,
        },
    }


def _market_data_session_overlay_summary(payload: dict[str, Any] | None) -> dict[str, Any]:
    data = _result(payload)
    if not data:
        return {}
    session = data.get("session") or {}
    summary = data.get("summary") or {}
    rows = data.get("rows") or []
    return {
        "version": data.get("market_data_session_overlay_version"),
        "generated_at": data.get("generated_at"),
        "age_seconds": _age_seconds(data.get("generated_at")),
        "source_market_data_version": data.get("source_market_data_version"),
        "source_market_data_generated_at": data.get("source_market_data_generated_at"),
        "source_market_data_age_seconds": _age_seconds(data.get("source_market_data_generated_at")),
        "session": session,
        "session_name": session.get("session"),
        "is_regular_session": session.get("is_regular_session"),
        "is_extended_or_overnight": session.get("is_extended_or_overnight"),
        "next_regular_open": data.get("next_regular_open") or {},
        "risk_interpretation": data.get("risk_interpretation"),
        "stock_cash_overnight_monitoring_enabled": data.get("stock_cash_overnight_monitoring_enabled"),
        "option_quotes_regular_session_dependent": data.get("option_quotes_regular_session_dependent"),
        "summary": summary,
        "session_bucket_counts": summary.get("session_bucket_counts") or {},
        "monitorability_counts": summary.get("monitorability_counts") or {},
        "monitorable_alert_instruments": summary.get("monitorable_alert_instruments") or [],
        "regular_dependent_alert_instruments": summary.get("regular_dependent_alert_instruments") or [],
        "interpretation_notes": summary.get("interpretation_notes") or [],
        "rows": rows[:50],
    }


def _market_data_risk_gate_summary(payload: dict[str, Any] | None) -> dict[str, Any]:
    data = _result(payload)
    if not data:
        return {}
    market = data.get("market_data") or {}
    return {
        "version": data.get("market_data_risk_gate_version"),
        "artifact_role": data.get("artifact_role"),
        "canonical_latest_artifact": data.get("canonical_latest_artifact"),
        "canonical_latest_policy": data.get("canonical_latest_policy"),
        "generated_at": data.get("generated_at"),
        "age_seconds": _age_seconds(data.get("generated_at")),
        "gate_status": data.get("gate_status"),
        "allowed": data.get("allowed"),
        "new_entries_allowed": data.get("new_entries_allowed"),
        "alpha_entries_allowed": data.get("alpha_entries_allowed"),
        "hedge_entries_allowed": data.get("hedge_entries_allowed"),
        "exit_or_risk_reduction_allowed": data.get("exit_or_risk_reduction_allowed"),
        "action": data.get("action"),
        "entry_size_multiplier": data.get("entry_size_multiplier"),
        "defensive_entry_size_multiplier": data.get("defensive_entry_size_multiplier"),
        "option_strategy_bias": data.get("option_strategy_bias"),
        "reason": data.get("reason"),
        "blocking_reasons": data.get("blocking_reasons") or [],
        "defensive_reasons": data.get("defensive_reasons") or [],
        "haircut_reasons": data.get("haircut_reasons") or [],
        "monitoring_only_reasons": data.get("monitoring_only_reasons") or [],
        "reason_breakdown": data.get("reason_breakdown") or {},
        "operator_summary_lines": data.get("operator_summary_lines") or [],
        "macro_adjusted_priority": data.get("macro_adjusted_priority"),
        "session_adjusted_priority": data.get("session_adjusted_priority"),
        "market_temperature_label": market.get("temperature_label"),
        "market_temperature_score": market.get("temperature_score"),
        "market_data_status": market.get("status"),
        "source_freshness": market.get("source_freshness") or {},
        "macro_reaction_assessment": market.get("macro_reaction_assessment") or {},
        "option_structure_context": market.get("option_structure_context") or {},
        "close_only_proxy_status": market.get("close_only_proxy_status") or {},
        "counts": market.get("counts") or {},
        "primary_close_only_instruments": market.get("primary_close_only_instruments") or [],
        "monitoring_only_close_only_instruments": market.get("monitoring_only_close_only_instruments") or [],
        "macro_expected_reaction_moves": market.get("macro_expected_reaction_moves") or [],
    }


def _market_data_operating_auto_gate_summary(payload: dict[str, Any] | None) -> dict[str, Any]:
    data = _result(payload)
    if not data:
        return {}
    defensive_mode = data.get("market_data_defensive_mode") or {}
    controls = defensive_mode.get("controls") or {}
    protective_packet = data.get("protective_analysis_packet") or {}
    operating_mode_raw = data.get("market_data_operating_mode")
    operating_mode = (
        operating_mode_raw.get("mode")
        if isinstance(operating_mode_raw, dict)
        else operating_mode_raw
    ) or defensive_mode.get("mode")
    sample_items = []
    for item in data.get("sample_auto_execution_items") or data.get("auto_execution_items") or []:
        sample_items.append(
            {
                "symbol": item.get("symbol"),
                "action": item.get("action"),
                "ticket_type": item.get("ticket_type"),
                "status": item.get("auto_execution_status"),
                "block_reasons": item.get("block_reasons") or [],
                "entry_size_multiplier": item.get("market_data_entry_size_multiplier"),
            }
        )
        if len(sample_items) >= 20:
            break
    protective_sample = []
    for item in protective_packet.get("sample_candidates") or protective_packet.get("candidates") or []:
        legs = _extract_option_legs(item, fallback_symbol=item.get("symbol"))
        protective_sample.append(
            {
                "symbol": item.get("symbol"),
                "source_action": item.get("source_action"),
                "analysis_status": item.get("analysis_status"),
                "ticketing_allowed": item.get("ticketing_allowed"),
                "reason": item.get("reason"),
                "strategy": item.get("strategy") or item.get("option_strategy") or item.get("structure"),
                "leg_count": len(legs),
                "legs": legs,
                "risk": _strategy_risk_summary(item),
            }
        )
        if len(protective_sample) >= 20:
            break
    return {
        "version": data.get("autonomous_execution_gate_version") or data.get("version"),
        "generated_at": data.get("generated_at"),
        "age_seconds": _age_seconds(data.get("generated_at")),
        "source_autonomous_execution_gate_version": data.get("source_autonomous_execution_gate_version"),
        "auto_ready_count": data.get("auto_ready_count"),
        "source_preview_item_count": data.get("source_preview_item_count"),
        "used_budget": data.get("used_budget"),
        "status_counts": data.get("auto_gate_status_counts") or {},
        "block_reason_counts": data.get("auto_gate_block_reason_counts") or {},
        "operating_mode": operating_mode,
        "defensive_mode": {
            "version": defensive_mode.get("market_data_defensive_mode_version"),
            "mode": defensive_mode.get("mode"),
            "severity": defensive_mode.get("severity"),
            "source_gate_status": controls.get("source_gate_status"),
            "source_gate_action": controls.get("source_gate_action"),
            "ordinary_alpha_entries_allowed": controls.get("ordinary_alpha_entries_allowed"),
            "hedge_or_defensive_entries_allowed": controls.get("hedge_or_defensive_entries_allowed"),
            "exit_or_risk_reduction_allowed": controls.get("exit_or_risk_reduction_allowed"),
            "protective_option_analysis_allowed": controls.get("protective_option_analysis_allowed"),
            "protective_option_ticketing_allowed": controls.get("protective_option_ticketing_allowed"),
            "max_new_entry_size_multiplier": controls.get("max_new_entry_size_multiplier"),
            "allowed_option_structures": controls.get("allowed_option_structures") or [],
            "market_data": defensive_mode.get("market_data") or {},
            "signature": defensive_mode.get("signature") or {},
        },
        "protective_analysis_packet": {
            "candidate_count": protective_packet.get("candidate_count"),
            "sample_count": len(protective_sample),
            "ticketing_allowed": protective_packet.get("ticketing_allowed"),
            "allowed_option_structures": protective_packet.get("allowed_option_structures") or [],
            "sample_candidates": protective_sample,
            "safety": protective_packet.get("safety") or {},
        },
        "sample_auto_execution_items": sample_items,
        "safety": data.get("safety") or {},
    }


def _capital_margin_state_summary(payload: dict[str, Any] | None) -> dict[str, Any]:
    data = _result(payload)
    if not data:
        return {}
    metrics = data.get("metrics") if isinstance(data.get("metrics"), dict) else {}
    budgets = data.get("budgets") if isinstance(data.get("budgets"), dict) else {}
    withdrawal = data.get("withdrawal_guard") if isinstance(data.get("withdrawal_guard"), dict) else {}
    flow = data.get("flow_detection") if isinstance(data.get("flow_detection"), dict) else {}
    return {
        "version": data.get("capital_margin_state_version") or data.get("version"),
        "generated_at": data.get("generated_at"),
        "age_seconds": _age_seconds(data.get("generated_at")),
        "status": data.get("status"),
        "status_reasons": data.get("status_reasons") or [],
        "leverage_mode": data.get("leverage_mode"),
        "selected_account": data.get("selected_account"),
        "metrics": {
            "net_liquidation": metrics.get("net_liquidation"),
            "total_cash_value": metrics.get("total_cash_value"),
            "available_funds": metrics.get("available_funds"),
            "buying_power": metrics.get("buying_power"),
            "excess_liquidity": metrics.get("excess_liquidity"),
            "initial_margin": metrics.get("initial_margin"),
            "maintenance_margin": metrics.get("maintenance_margin"),
            "available_funds_ratio": metrics.get("available_funds_ratio"),
            "excess_liquidity_ratio": metrics.get("excess_liquidity_ratio"),
            "maintenance_margin_ratio": metrics.get("maintenance_margin_ratio"),
        },
        "budgets": {
            "budget_model": budgets.get("budget_model"),
            "reserve_cash": budgets.get("reserve_cash"),
            "deployable_cash_after_reserve": budgets.get("deployable_cash_after_reserve"),
            "core_risk_budget": budgets.get("core_risk_budget"),
            "options_premium_budget": budgets.get("options_premium_budget"),
            "hedge_budget": budgets.get("hedge_budget"),
            "max_single_position_notional": budgets.get("max_single_position_notional"),
            "max_new_initial_margin": budgets.get("max_new_initial_margin"),
            "micro_cash_budget_overlay": budgets.get("micro_cash_budget_overlay") or {},
        },
        "withdrawal_guard": {
            "status": withdrawal.get("status"),
            "max_safe_withdrawal": withdrawal.get("max_safe_withdrawal"),
            "theoretical_max_withdrawal_if_snapshot_fresh": withdrawal.get("theoretical_max_withdrawal_if_snapshot_fresh"),
            "requires_fresh_account_snapshot": withdrawal.get("requires_fresh_account_snapshot"),
            "binding_constraints": (withdrawal.get("binding_constraints") or [])[:8],
        },
        "flow_detection": {
            "status": flow.get("status"),
            "external_cash_flow_candidate": flow.get("external_cash_flow_candidate"),
            "reason": flow.get("reason"),
        },
        "position_counts": data.get("position_counts") or {},
        "action_guidance": data.get("action_guidance") or [],
        "safety": data.get("safety") or {},
    }


def _capital_aware_action_gate_summary(payload: dict[str, Any] | None) -> dict[str, Any]:
    data = _result(payload)
    if not data:
        return {}
    capital_state = data.get("capital_state") if isinstance(data.get("capital_state"), dict) else {}
    withdrawal = capital_state.get("withdrawal_guard") if isinstance(capital_state.get("withdrawal_guard"), dict) else {}
    sample_items = []
    for item in data.get("sample_auto_execution_items") or data.get("auto_execution_items") or []:
        requirements = item.get("capital_requirements") if isinstance(item.get("capital_requirements"), dict) else {}
        remaining_after = item.get("capital_remaining_after") if isinstance(item.get("capital_remaining_after"), dict) else {}
        source_snapshot = item.get("source_gate_snapshot") if isinstance(item.get("source_gate_snapshot"), dict) else {}
        sample_items.append(
            {
                "symbol": item.get("symbol"),
                "action": item.get("action"),
                "ticket_type": item.get("ticket_type"),
                "status": item.get("capital_gate_status") or item.get("auto_execution_status"),
                "status_display": item.get("capital_gate_status_display") or item.get("capital_gate_status") or item.get("auto_execution_status"),
                "block_category": item.get("capital_block_category"),
                "source_status": source_snapshot.get("source_auto_execution_status"),
                "quantity": item.get("capital_adjusted_quantity") if item.get("capital_adjusted_quantity") is not None else item.get("quantity"),
                "quantity_scale": item.get("capital_quantity_scale"),
                "block_reasons": item.get("capital_block_reasons") or item.get("block_reasons") or [],
                "cash_required": requirements.get("cash_required"),
                "initial_margin_required": requirements.get("initial_margin_required"),
                "risk_required": requirements.get("risk_required"),
                "option_premium_required": requirements.get("option_premium_required"),
                "remaining_cash_budget": remaining_after.get("cash_budget"),
                "remaining_initial_margin_budget": remaining_after.get("new_initial_margin_budget"),
            }
        )
        if len(sample_items) >= 20:
            break
    return {
        "version": data.get("capital_aware_action_gate_version") or data.get("autonomous_execution_gate_version") or data.get("version"),
        "generated_at": data.get("generated_at"),
        "age_seconds": _age_seconds(data.get("generated_at")),
        "source_autonomous_execution_gate_version": data.get("source_autonomous_execution_gate_version"),
        "source_auto_execution_item_count": data.get("source_auto_execution_item_count"),
        "source_gate_summary": data.get("source_gate_summary") or {},
        "auto_ready_count": data.get("auto_ready_count"),
        "capital_ready_count": data.get("capital_ready_count"),
        "capital_resized_count": data.get("capital_resized_count"),
        "capital_blocked_count": data.get("capital_blocked_count"),
        "capital_capacity_blocked_count": data.get("capital_capacity_blocked_count"),
        "capital_action_blocked_count": data.get("capital_action_blocked_count"),
        "capital_block_category_counts": data.get("capital_block_category_counts") or {},
        "status_counts": data.get("capital_gate_status_counts") or data.get("auto_gate_status_counts") or {},
        "block_reason_counts": data.get("capital_gate_block_reason_counts") or data.get("auto_gate_block_reason_counts") or {},
        "capital_state": {
            "version": capital_state.get("version"),
            "generated_at": capital_state.get("generated_at"),
            "status": capital_state.get("status"),
            "status_reasons": capital_state.get("status_reasons") or [],
            "leverage_mode": capital_state.get("leverage_mode"),
            "selected_account": capital_state.get("selected_account"),
            "budgets": capital_state.get("budgets") or {},
            "withdrawal_guard": {
                "status": withdrawal.get("status"),
                "max_safe_withdrawal": withdrawal.get("max_safe_withdrawal"),
                "theoretical_max_withdrawal_if_snapshot_fresh": withdrawal.get("theoretical_max_withdrawal_if_snapshot_fresh"),
                "requires_fresh_account_snapshot": withdrawal.get("requires_fresh_account_snapshot"),
            },
        },
        "initial_budget": data.get("initial_budget") or {},
        "used_budget": data.get("used_budget") or {},
        "remaining_budget": data.get("remaining_budget") or {},
        "capital_gate_parameters": data.get("capital_gate_parameters") or {},
        "sample_auto_execution_items": sample_items,
        "safety": data.get("safety") or {},
    }


def _strategy_allocation_engine_summary(payload: dict[str, Any] | None) -> dict[str, Any]:
    data = _result(payload)
    if not data:
        return {}
    summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
    contexts = data.get("contexts") if isinstance(data.get("contexts"), dict) else {}
    recommendations = []
    for item in data.get("recommendations") or []:
        if not isinstance(item, dict):
            continue
        recommendations.append(
            {
                "symbol": item.get("symbol"),
                "direction": item.get("direction"),
                "recommendation": item.get("recommendation"),
                "selected_expression": item.get("selected_expression"),
                "candidate_source": item.get("candidate_source"),
                "synthetic_candidate": item.get("synthetic_candidate"),
                "intent_role": item.get("intent_role"),
                "confidence": item.get("confidence"),
                "capital_gate_status": item.get("capital_gate_status"),
                "ticker_candidate_rank": item.get("ticker_candidate_rank") or {},
                "cross_asset_expression": item.get("cross_asset_expression") or {},
                "reasons": (item.get("reasons") or [])[:8],
                "option_structure": item.get("option_structure") or {},
                "market_gate": item.get("market_gate") or {},
                "micro_policy": item.get("micro_policy") or {},
                "safety": item.get("safety") or {},
            }
        )
        if len(recommendations) >= 20:
            break
    return {
        "version": data.get("strategy_allocation_engine_version") or data.get("version"),
        "generated_at": data.get("generated_at"),
        "age_seconds": _age_seconds(data.get("generated_at")),
        "mode": data.get("mode"),
        "candidate_count": summary.get("candidate_count"),
        "recommendation_count": summary.get("recommendation_count"),
        "paper_trade_ready_count": summary.get("paper_trade_ready_count"),
        "hedge_only_count": summary.get("hedge_only_count"),
        "blocked_count": summary.get("blocked_count"),
        "recommendation_counts": summary.get("recommendation_counts") or {},
        "expression_counts": summary.get("expression_counts") or {},
        "confidence_counts": summary.get("confidence_counts") or {},
        "top_reason_counts": summary.get("top_reason_counts") or {},
        "micro_policy_operating_mode": summary.get("micro_policy_operating_mode"),
        "micro_policy_ready_count": summary.get("micro_policy_ready_count"),
        "micro_policy_blocked_count": summary.get("micro_policy_blocked_count"),
        "micro_policy_block_reason_counts": summary.get("micro_policy_block_reason_counts") or {},
        "m239_source_candidate_count": summary.get("m239_source_candidate_count"),
        "m241_6_synthetic_appended_count": summary.get("m241_6_synthetic_appended_count"),
        "cross_asset_seed_count": (contexts.get("ticker_action_candidate_synthesis") or {}).get("cross_asset_seed_count"),
        "cross_asset_injected_count": (contexts.get("ticker_action_candidate_synthesis") or {}).get("cross_asset_injected_count"),
        "top_cross_asset_expressions": (contexts.get("ticker_action_candidate_synthesis") or {}).get("top_cross_asset_expressions") or [],
        "capital_context": contexts.get("capital") or {},
        "micro_policy_context": contexts.get("micro_policy") or {},
        "market_context": contexts.get("market") or {},
        "options_context": contexts.get("options") or {},
        "scorer_context": contexts.get("scorer") or {},
        "ticker_candidate_ranking_context": contexts.get("ticker_candidate_ranking") or {},
        "ticker_action_candidate_synthesis_context": contexts.get("ticker_action_candidate_synthesis") or {},
        "news_context": contexts.get("news") or {},
        "recommendations": recommendations,
        "latest_news": (data.get("latest_news") or [])[:8],
        "safety": data.get("safety") or {},
        "policy": data.get("policy") or {},
    }


def _micro_option_candidate_selector_summary(payload: dict[str, Any] | None) -> dict[str, Any]:
    data = _result(payload)
    if not data:
        return {}
    summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
    contexts = data.get("contexts") if isinstance(data.get("contexts"), dict) else {}
    candidates = []
    for item in data.get("candidates") or []:
        if not isinstance(item, dict):
            continue
        candidates.append(
            {
                "symbol": item.get("symbol"),
                "strategy": item.get("strategy"),
                "right": item.get("right"),
                "expiration": item.get("expiration"),
                "dte": item.get("dte"),
                "strike": item.get("strike"),
                "exchange": item.get("exchange"),
                "bid": item.get("bid"),
                "ask": item.get("ask"),
                "mid": item.get("mid"),
                "total_ask_debit": item.get("total_ask_debit"),
                "max_loss": item.get("max_loss"),
                "spread_pct": item.get("spread_pct"),
                "volume": item.get("volume"),
                "open_interest": item.get("open_interest"),
                "direction_context": item.get("direction_context"),
                "direction_match": item.get("direction_match"),
                "candidate_status": item.get("candidate_status"),
                "paper_candidate_ready": item.get("paper_candidate_ready"),
                "requires_fresh_quote": item.get("requires_fresh_quote"),
                "score": item.get("score"),
                "reasons": (item.get("reasons") or [])[:8],
                "structure_context": item.get("structure_context") or {},
                "micro_policy": item.get("micro_policy") or {},
                "safety": item.get("safety") or {},
            }
        )
        if len(candidates) >= 20:
            break
    return {
        "version": data.get("micro_option_candidate_selector_version") or data.get("version"),
        "generated_at": data.get("generated_at"),
        "age_seconds": _age_seconds(data.get("generated_at")),
        "status": data.get("status"),
        "mode": data.get("mode"),
        "candidate_count": summary.get("candidate_count"),
        "ready_count": summary.get("ready_count"),
        "stale_preview_count": summary.get("stale_preview_count"),
        "blocked_count": summary.get("blocked_count"),
        "status_counts": summary.get("status_counts") or {},
        "right_counts": summary.get("right_counts") or {},
        "symbol_counts": summary.get("symbol_counts") or {},
        "top_reason_counts": summary.get("top_reason_counts") or {},
        "option_budget": summary.get("option_budget"),
        "max_option_ticket_debit": summary.get("max_option_ticket_debit"),
        "micro_policy_operating_mode": summary.get("micro_policy_operating_mode"),
        "quote_fresh": summary.get("quote_fresh"),
        "quote_age_seconds": summary.get("quote_age_seconds"),
        "quote_generated_at": summary.get("quote_generated_at"),
        "allowed_symbols": summary.get("allowed_symbols") or [],
        "capital_context": contexts.get("capital") or {},
        "micro_policy_context": contexts.get("micro_policy") or {},
        "option_quote_context": contexts.get("option_quote_snapshot") or {},
        "options_supervisor_context": contexts.get("options_supervisor") or {},
        "candidates": candidates,
        "policy": data.get("policy") or {},
        "safety": data.get("safety") or {},
    }


def _paper_option_ticket_preview_summary(payload: dict[str, Any] | None) -> dict[str, Any]:
    data = _result(payload)
    if not data:
        return {}
    summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
    source = data.get("source") if isinstance(data.get("source"), dict) else {}
    ticket_rows = []
    for ticket in data.get("ticket_previews") or []:
        if not isinstance(ticket, dict):
            continue
        order = ticket.get("order_preview") if isinstance(ticket.get("order_preview"), dict) else {}
        risk = ticket.get("risk_preview") if isinstance(ticket.get("risk_preview"), dict) else {}
        legs = ticket.get("legs") if isinstance(ticket.get("legs"), list) else []
        ticket_rows.append(
            {
                "ticket_id": ticket.get("ticket_id"),
                "ticket_type": ticket.get("ticket_type"),
                "strategy": ticket.get("strategy"),
                "symbol": (ticket.get("underlying") or {}).get("symbol") if isinstance(ticket.get("underlying"), dict) else None,
                "leg_count": len([row for row in legs if isinstance(row, dict)]),
                "limit_price": order.get("limit_price"),
                "estimated_total_debit": order.get("estimated_total_debit"),
                "estimated_max_loss": order.get("estimated_max_loss"),
                "max_ticket_debit": risk.get("max_ticket_debit"),
                "transmit": order.get("transmit"),
                "what_if": order.get("what_if"),
            }
        )
        if len(ticket_rows) >= 20:
            break
    review_rows = []
    for review in data.get("candidate_reviews") or []:
        if not isinstance(review, dict):
            continue
        review_rows.append(
            {
                "candidate_symbol": review.get("candidate_symbol"),
                "strategy": review.get("strategy"),
                "preview_status": review.get("preview_status"),
                "score": review.get("score"),
                "limit_price": review.get("limit_price"),
                "total_limit_debit": review.get("total_limit_debit"),
                "block_reasons": (review.get("block_reasons") or [])[:8],
            }
        )
        if len(review_rows) >= 20:
            break
    return {
        "version": data.get("paper_option_ticket_preview_version") or data.get("version"),
        "generated_at": data.get("generated_at"),
        "age_seconds": _age_seconds(data.get("generated_at")),
        "status": data.get("status"),
        "expected_deferred": data.get("expected_deferred"),
        "mode": data.get("mode"),
        "source": source,
        "ticket_count": summary.get("ticket_count"),
        "candidate_review_count": summary.get("candidate_review_count"),
        "ready_ticket_count": summary.get("ready_ticket_count"),
        "blocked_ticket_count": summary.get("blocked_ticket_count"),
        "skipped_ticket_count": summary.get("skipped_ticket_count"),
        "single_leg_ticket_count": summary.get("single_leg_ticket_count"),
        "multi_leg_ticket_count": summary.get("multi_leg_ticket_count"),
        "status_counts": summary.get("status_counts") or {},
        "top_block_reason_counts": summary.get("top_block_reason_counts") or {},
        "option_budget": summary.get("option_budget"),
        "max_ticket_debit": summary.get("max_ticket_debit"),
        "limit_price_policy": summary.get("limit_price_policy"),
        "require_fresh_quote": summary.get("require_fresh_quote"),
        "default_quantity": summary.get("default_quantity"),
        "ticket_previews": ticket_rows,
        "candidate_reviews": review_rows,
        "policy": data.get("policy") or {},
        "safety": data.get("safety") or {},
    }


def _option_whatif_safety_validation_summary(payload: dict[str, Any] | None) -> dict[str, Any]:
    data = _result(payload)
    if not data:
        return {}
    summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
    contexts = data.get("contexts") if isinstance(data.get("contexts"), dict) else {}
    policy = data.get("policy") if isinstance(data.get("policy"), dict) else {}
    validation_rows = []
    for item in data.get("validation_items") or []:
        if not isinstance(item, dict):
            continue
        ticket = item.get("ticket") if isinstance(item.get("ticket"), dict) else {}
        legs = ticket.get("legs") if isinstance(ticket.get("legs"), list) else []
        validation_rows.append(
            {
                "ticket_id": item.get("ticket_id"),
                "symbol": item.get("symbol"),
                "strategy": item.get("strategy"),
                "status": item.get("status"),
                "final_paper_executable": item.get("final_paper_executable"),
                "local_safety_passed": item.get("local_safety_passed"),
                "broker_whatif_status": item.get("broker_whatif_status"),
                "estimated_total_debit": item.get("estimated_total_debit"),
                "estimated_max_loss": item.get("estimated_max_loss"),
                "leg_count": len([row for row in legs if isinstance(row, dict)]),
                "block_reasons": (item.get("block_reasons") or [])[:8],
            }
        )
        if len(validation_rows) >= 20:
            break
    return {
        "version": data.get("option_whatif_safety_validation_version") or data.get("version"),
        "generated_at": data.get("generated_at"),
        "age_seconds": _age_seconds(data.get("generated_at")),
        "status": data.get("status"),
        "expected_deferred": data.get("expected_deferred"),
        "mode": data.get("mode"),
        "validation_item_count": summary.get("validation_item_count"),
        "passed_count": summary.get("passed_count"),
        "whatif_pending_count": summary.get("whatif_pending_count"),
        "blocked_count": summary.get("blocked_count"),
        "status_counts": summary.get("status_counts") or {},
        "top_block_reason_counts": summary.get("top_block_reason_counts") or {},
        "require_broker_whatif": summary.get("require_broker_whatif"),
        "execute_whatif_refresh": summary.get("execute_whatif_refresh"),
        "quote_fresh": summary.get("quote_fresh"),
        "option_session_expected_deferred": summary.get("option_session_expected_deferred"),
        "budgets": summary.get("budgets") or {},
        "quote_snapshot_freshness": contexts.get("quote_snapshot_freshness") or {},
        "option_session": contexts.get("option_session") or {},
        "broker_whatif_source": contexts.get("broker_whatif_source") or {},
        "single_leg_option_enabled": policy.get("single_leg_option_enabled"),
        "multi_leg_combo_enabled": policy.get("multi_leg_combo_enabled"),
        "multi_leg_combo_scope": policy.get("multi_leg_combo_scope"),
        "broker_combo_whatif_enabled": policy.get("broker_combo_whatif_enabled"),
        "broker_combo_whatif_preparation": data.get("broker_combo_whatif_preparation") or {},
        "validation_items": validation_rows,
        "policy": policy,
        "safety": data.get("safety") or {},
    }


def _limited_auto_trading_readiness_summary(payload: dict[str, Any] | None) -> dict[str, Any]:
    data = _result(payload)
    if not data:
        return {}
    summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
    permissions = data.get("execution_permissions") if isinstance(data.get("execution_permissions"), dict) else {}
    contexts = data.get("contexts") if isinstance(data.get("contexts"), dict) else {}
    gate_severity_policy = contexts.get("gate_severity_policy") if isinstance(contexts.get("gate_severity_policy"), dict) else {}
    hard_blocks = []
    for row in data.get("hard_blocks") or []:
        if isinstance(row, dict):
            hard_blocks.append(
                {
                    "code": row.get("code"),
                    "source": row.get("source"),
                    "message": row.get("message"),
                    "value": row.get("value"),
                }
            )
        if len(hard_blocks) >= 12:
            break
    soft_warnings = []
    for row in data.get("soft_warnings") or []:
        if isinstance(row, dict):
            soft_warnings.append(
                {
                    "code": row.get("code"),
                    "source": row.get("source"),
                    "message": row.get("message"),
                    "value": row.get("value"),
                }
            )
        if len(soft_warnings) >= 12:
            break
    return {
        "version": data.get("limited_auto_trading_readiness_version") or data.get("version"),
        "generated_at": data.get("generated_at"),
        "age_seconds": _age_seconds(data.get("generated_at")),
        "mode": data.get("mode"),
        "status": data.get("status"),
        "readiness_level": data.get("readiness_level"),
        "hard_block_count": summary.get("hard_block_count"),
        "trade_now_blocker_count": summary.get("trade_now_blocker_count"),
        "live_eligibility_blocker_count": summary.get("live_eligibility_blocker_count"),
        "soft_warning_count": summary.get("soft_warning_count"),
        "trade_now_allowed": summary.get("trade_now_allowed"),
        "live_eligibility_clear": summary.get("live_eligibility_clear"),
        "expected_deferred": summary.get("expected_deferred"),
        "expected_deferred_reasons": summary.get("expected_deferred_reasons") or [],
        "gate_severity_policy": gate_severity_policy,
        "gate_execution_posture": summary.get("gate_execution_posture") or gate_severity_policy.get("recommended_execution_posture"),
        "gate_haircut_count": summary.get("gate_haircut_count") or gate_severity_policy.get("haircut_count"),
        "gate_confidence_discount_count": summary.get("gate_confidence_discount_count") or gate_severity_policy.get("confidence_discount_count"),
        "gate_monitoring_only_count": summary.get("gate_monitoring_only_count") or gate_severity_policy.get("monitoring_only_count"),
        "gate_size_multiplier": summary.get("gate_size_multiplier") or gate_severity_policy.get("size_multiplier"),
        "gate_confidence_multiplier": summary.get("gate_confidence_multiplier") or gate_severity_policy.get("confidence_multiplier"),
        "eligible_strategy_types": summary.get("eligible_strategy_types") or [],
        "max_allowed_premium": summary.get("max_allowed_premium"),
        "max_allowed_single_position_notional": summary.get("max_allowed_single_position_notional"),
        "max_allowed_new_initial_margin": summary.get("max_allowed_new_initial_margin"),
        "artifact_status_counts": summary.get("artifact_status_counts") or {},
        "stale_artifact_count": summary.get("stale_artifact_count"),
        "missing_required_artifact_count": summary.get("missing_required_artifact_count"),
        "live_execution_enabled": permissions.get("live_execution_enabled"),
        "limited_auto_trade_candidate": permissions.get("limited_auto_trade_candidate"),
        "paper_execution_enabled": permissions.get("paper_execution_enabled"),
        "whatif_validation_enabled": permissions.get("whatif_validation_enabled"),
        "live_routing_status": permissions.get("live_routing_status"),
        "trade_now_blockers": (data.get("trade_now_blockers") or [])[:12],
        "live_eligibility_blockers": (data.get("live_eligibility_blockers") or [])[:12],
        "hard_blocks": hard_blocks,
        "soft_warnings": soft_warnings,
        "next_required_steps": (data.get("next_required_steps") or [])[:8],
        "safety": data.get("safety") or {},
        "policy": data.get("policy") or {},
    }


def _live_read_only_capital_validation_summary(payload: dict[str, Any] | None) -> dict[str, Any]:
    data = _result(payload)
    if not data:
        return {}
    capital = data.get("capital_state") if isinstance(data.get("capital_state"), dict) else {}
    account_capacity = data.get("account_capacity") if isinstance(data.get("account_capacity"), dict) else {}
    broker_whatif = data.get("broker_whatif") if isinstance(data.get("broker_whatif"), dict) else {}
    sample_items: list[dict[str, Any]] = []
    for item in data.get("sample_validation_items") or data.get("validation_items") or []:
        if not isinstance(item, dict):
            continue
        sample_items.append(
            {
                "symbol": item.get("symbol"),
                "action": item.get("action"),
                "ticket_type": item.get("ticket_type"),
                "source_quantity": item.get("source_quantity"),
                "max_feasible_quantity": item.get("max_feasible_quantity"),
                "status": item.get("status"),
                "binding_constraint": item.get("binding_constraint"),
                "current_size_feasible": item.get("current_size_feasible"),
                "resize_possible": item.get("resize_possible"),
                "capital_gate_status": item.get("capital_gate_status"),
                "execution_status": (item.get("execution_gate") or {}).get("status") if isinstance(item.get("execution_gate"), dict) else None,
                "trade_executable": (item.get("execution_gate") or {}).get("trade_executable") if isinstance(item.get("execution_gate"), dict) else None,
                "blocked_by": (item.get("execution_gate") or {}).get("blocked_by") if isinstance(item.get("execution_gate"), dict) else {},
                "micro_policy": (item.get("execution_gate") or {}).get("micro_policy_current") if isinstance(item.get("execution_gate"), dict) else {},
                "broker_whatif_available": item.get("broker_whatif_available"),
                "broker_whatif_status": item.get("broker_whatif_status"),
                "reasons": ((item.get("execution_gate") or {}).get("execution_block_reasons") if isinstance(item.get("execution_gate"), dict) else None) or (item.get("reasons") or [])[:8],
            }
        )
        if len(sample_items) >= 20:
            break
    return {
        "version": data.get("live_read_only_capital_validation_version") or data.get("version"),
        "generated_at": data.get("generated_at"),
        "age_seconds": _age_seconds(data.get("generated_at")),
        "status": data.get("status"),
        "capital_state": {
            "version": capital.get("version"),
            "generated_at": capital.get("generated_at"),
            "status": capital.get("status"),
            "status_reasons": capital.get("status_reasons") or [],
            "leverage_mode": capital.get("leverage_mode"),
            "selected_account": capital.get("selected_account"),
            "snapshot_age_seconds": capital.get("snapshot_age_seconds"),
        },
        "account_capacity": {
            "deployable_cash_after_reserve": account_capacity.get("deployable_cash_after_reserve"),
            "max_new_initial_margin": account_capacity.get("max_new_initial_margin"),
            "core_risk_budget": account_capacity.get("core_risk_budget"),
            "max_single_position_notional": account_capacity.get("max_single_position_notional"),
            "options_premium_budget": account_capacity.get("options_premium_budget"),
            "max_safe_withdrawal": account_capacity.get("max_safe_withdrawal"),
            "requires_fresh_account_snapshot": account_capacity.get("requires_fresh_account_snapshot"),
        },
        "broker_whatif": {
            "available": broker_whatif.get("available"),
            "safe_to_use": broker_whatif.get("safe_to_use"),
            "response_count": broker_whatif.get("response_count"),
            "requested_count": broker_whatif.get("requested_count"),
            "read_only_mode_detected": broker_whatif.get("read_only_mode_detected"),
            "generated_at": broker_whatif.get("generated_at"),
            "issues": broker_whatif.get("issues") or [],
        },
        "validation_item_count": data.get("validation_item_count"),
        "current_size_feasible_count": data.get("current_size_feasible_count"),
        "resize_required_count": data.get("resize_required_count"),
        "blocked_count": data.get("blocked_count"),
        "validation_status_counts": data.get("validation_status_counts") or {},
        "block_reason_counts": data.get("block_reason_counts") or {},
        "trade_executable_split_version": data.get("trade_executable_split_version"),
        "trade_executable_current_size_count": data.get("trade_executable_current_size_count"),
        "trade_executable_after_resize_count": data.get("trade_executable_after_resize_count"),
        "not_trade_executable_count": data.get("not_trade_executable_count"),
        "execution_status_counts": data.get("execution_status_counts") or {},
        "execution_block_reason_counts": data.get("execution_block_reason_counts") or {},
        "blocked_by_gate_counts": data.get("blocked_by_gate_counts") or {},
        "micro_cash_policy": data.get("micro_cash_policy") or {},
        "micro_policy_state": data.get("micro_policy_state") or {},
        "micro_policy_operating_mode": data.get("micro_policy_operating_mode"),
        "micro_policy_current_ready_count": data.get("micro_policy_current_ready_count"),
        "micro_policy_after_resize_ready_count": data.get("micro_policy_after_resize_ready_count"),
        "micro_policy_blocked_count": data.get("micro_policy_blocked_count"),
        "micro_policy_block_reason_counts": data.get("micro_policy_block_reason_counts") or {},
        "capital_vs_execution_summary": data.get("capital_vs_execution_summary") or {},
        "sample_validation_items": sample_items,
        "safety": data.get("safety") or {},
    }


def _capital_pipeline_orchestrator_summary(payload: dict[str, Any] | None) -> dict[str, Any]:
    data = _result(payload)
    if not data:
        return {}
    artifacts = []
    for row in data.get("artifacts") or []:
        if not isinstance(row, dict):
            continue
        artifacts.append(
            {
                "artifact_id": row.get("artifact_id"),
                "status": row.get("status"),
                "age_seconds": row.get("age_seconds"),
                "dependency_lag_seconds": row.get("dependency_lag_seconds"),
                "rebuild_required": row.get("rebuild_required"),
                "reasons": (row.get("reasons") or [])[:8],
            }
        )
    execution = data.get("execution") if isinstance(data.get("execution"), dict) else {}
    return {
        "version": data.get("capital_pipeline_orchestrator_version") or data.get("version"),
        "generated_at": data.get("generated_at"),
        "age_seconds": _age_seconds(data.get("generated_at")),
        "mode": data.get("mode"),
        "status": data.get("status"),
        "artifact_count": data.get("artifact_count"),
        "stale_or_missing_count": data.get("stale_or_missing_count"),
        "rebuild_required_count": data.get("rebuild_required_count"),
        "rebuild_steps": data.get("rebuild_steps") or [],
        "status_counts": data.get("status_counts") or {},
        "reason_counts": data.get("reason_counts") or {},
        "artifacts": artifacts[:10],
        "execution": {
            "executed_rebuild": execution.get("executed_rebuild"),
            "executed_steps": execution.get("executed_steps") or [],
        },
        "safety": data.get("safety") or {},
    }


def _market_data_session_transition_gate_summary(payload: dict[str, Any] | None) -> dict[str, Any]:
    data = _result(payload)
    if not data:
        return {}
    transition = data.get("transition") or {}
    controls = data.get("controls") or {}
    freshness = data.get("artifact_freshness") or {}
    current = data.get("current_session_context") or {}
    return {
        "version": data.get("market_data_session_transition_gate_version"),
        "generated_at": data.get("generated_at"),
        "age_seconds": _age_seconds(data.get("generated_at")),
        "decision": data.get("decision"),
        "severity": data.get("severity"),
        "reason": data.get("reason"),
        "transition_type": transition.get("transition_type"),
        "previous_session": transition.get("previous_session"),
        "current_session": transition.get("current_session"),
        "transition_detected": transition.get("transition_detected"),
        "current_risk_interpretation": current.get("risk_interpretation"),
        "rebuild_required_before_auto_release": controls.get("rebuild_required_before_auto_release"),
        "ordinary_alpha_entries_allowed_after_transition": controls.get("ordinary_alpha_entries_allowed_after_transition"),
        "hedge_or_defensive_entries_allowed": controls.get("hedge_or_defensive_entries_allowed"),
        "max_new_entry_size_multiplier": controls.get("max_new_entry_size_multiplier"),
        "artifact_freshness": freshness,
        "recommended_command_count": len(data.get("recommended_commands") or []),
        "recommended_commands": (data.get("recommended_commands") or [])[:10],
        "safety": data.get("safety") or {},
    }


def _session_transition_release_governor_summary(payload: dict[str, Any] | None) -> dict[str, Any]:
    data = _result(payload)
    if not data:
        return {}
    controls = data.get("controls") or {}
    contexts = data.get("contexts") or {}
    transition = contexts.get("session_transition_gate") or {}
    risk = contexts.get("market_data_risk_gate") or {}
    operating = contexts.get("market_data_operating_auto_gate") or {}
    portfolio = contexts.get("portfolio_risk_state") or {}
    evidence = contexts.get("evidence_preflight") or {}
    return {
        "version": data.get("session_transition_release_governor_version"),
        "generated_at": data.get("generated_at"),
        "age_seconds": _age_seconds(data.get("generated_at")),
        "release_status": data.get("release_status"),
        "severity": data.get("severity"),
        "ordinary_alpha_release_allowed": data.get("ordinary_alpha_release_allowed"),
        "max_new_entry_size_multiplier": data.get("max_new_entry_size_multiplier"),
        "reason": data.get("reason"),
        "failed_check_count": len([item for item in data.get("checks") or [] if not item.get("passed")]),
        "check_count": len(data.get("checks") or []),
        "transition_decision": transition.get("decision"),
        "market_data_risk_gate_status": risk.get("gate_status"),
        "operating_mode": operating.get("mode"),
        "operating_auto_ready_count": operating.get("auto_ready_count"),
        "portfolio_risk_state": portfolio.get("risk_state"),
        "evidence_gate_status": evidence.get("gate_status"),
        "checks": (data.get("checks") or [])[:20],
        "controls": controls,
        "safety": data.get("safety") or {},
    }


def _defensive_action_plan_summary(payload: dict[str, Any] | None) -> dict[str, Any]:
    data = _result(payload)
    if not data:
        return {}
    top_actions = []
    for item in data.get("top_actions") or []:
        legs = _extract_option_legs(item, fallback_symbol=item.get("symbol"))
        top_actions.append(
            {
                "symbol": item.get("symbol"),
                "action_type": item.get("action_type"),
                "strategy": item.get("strategy"),
                "priority_score": item.get("priority_score"),
                "ticketing_allowed": item.get("ticketing_allowed"),
                "requires_option_chain": item.get("requires_option_chain"),
                "requires_margin_whatif": item.get("requires_margin_whatif"),
                "reason": item.get("reason"),
                "leg_count": len(legs),
                "legs": legs,
                "risk": _strategy_risk_summary(item),
            }
        )
        if len(top_actions) >= 20:
            break
    summary = data.get("summary") or {}
    return {
        "version": data.get("operating_defensive_action_plan_version"),
        "generated_at": data.get("generated_at"),
        "age_seconds": _age_seconds(data.get("generated_at")),
        "operating_mode": data.get("operating_mode"),
        "operating_severity": data.get("operating_severity"),
        "action_count": data.get("action_count"),
        "recommended_posture": summary.get("recommended_posture"),
        "highest_priority_score": summary.get("highest_priority_score"),
        "requires_option_chain_count": summary.get("requires_option_chain_count"),
        "requires_margin_whatif_count": summary.get("requires_margin_whatif_count"),
        "analysis_only_count": summary.get("analysis_only_count"),
        "action_type_counts": data.get("action_type_counts") or {},
        "strategy_counts": data.get("strategy_counts") or {},
        "symbol_counts": data.get("symbol_counts") or {},
        "top_actions": top_actions,
        "safety": data.get("safety") or {},
    }


def _defensive_intent_ledger_summary(payload: dict[str, Any] | None) -> dict[str, Any]:
    data = _result(payload)
    if not data:
        return {}
    rows = []
    for item in data.get("defensive_intents") or []:
        paper = item.get("paper_intent") or {}
        legs = _extract_option_legs(item, fallback_symbol=item.get("symbol"))
        rows.append(
            {
                "symbol": item.get("symbol"),
                "state": item.get("state"),
                "strategy": item.get("strategy"),
                "priority_score": item.get("priority_score"),
                "block_reasons": item.get("block_reasons") or [],
                "paper_action": paper.get("action"),
                "quantity": paper.get("quantity"),
                "ticketing_allowed": item.get("ticketing_allowed"),
                "requires_option_chain": item.get("requires_option_chain"),
                "requires_margin_whatif": item.get("requires_margin_whatif"),
                "leg_count": len(legs),
                "legs": legs,
                "risk": _strategy_risk_summary(item),
            }
        )
        if len(rows) >= 20:
            break
    summary = data.get("summary") or {}
    return {
        "version": data.get("operating_defensive_intent_ledger_version"),
        "generated_at": data.get("generated_at"),
        "age_seconds": _age_seconds(data.get("generated_at")),
        "ledger_run_id": data.get("ledger_run_id"),
        "candidate_count": data.get("candidate_count"),
        "pending_defensive_paper_execution_count": data.get("pending_defensive_paper_execution_count"),
        "blocked_count": data.get("blocked_count"),
        "recommended_posture": summary.get("recommended_posture"),
        "strict_gate_mode": summary.get("strict_gate_mode"),
        "missing_whatif_block_count": summary.get("missing_whatif_block_count"),
        "missing_option_quote_block_count": summary.get("missing_option_quote_block_count"),
        "analysis_only_block_count": summary.get("analysis_only_block_count"),
        "state_counts": data.get("state_counts") or {},
        "strategy_counts": data.get("strategy_counts") or {},
        "symbol_counts": data.get("symbol_counts") or {},
        "defensive_intents": rows,
        "safety": data.get("safety") or {},
    }


def _evidence_freshness_summary(payload: dict[str, Any] | None) -> dict[str, Any]:
    data = _result(payload)
    if not data:
        return {}
    age_seconds = _age_seconds(data.get("generated_at"))
    artifact_stale = bool(age_seconds is not None and age_seconds > SOURCE_ARTIFACT_STALE_SECONDS)
    raw_status = data.get("overall_status")
    effective_status = "red" if artifact_stale else raw_status
    return {
        "version": data.get("evidence_freshness_desk_version"),
        "guard_version": data.get("scorer_evidence_gap_guard_version"),
        "generated_at": data.get("generated_at"),
        "age_seconds": age_seconds,
        "artifact_stale": artifact_stale,
        "artifact_stale_threshold_seconds": SOURCE_ARTIFACT_STALE_SECONDS,
        "overall_status": raw_status,
        "effective_status": effective_status,
        "effective_status_reason": "artifact_stale" if artifact_stale else "artifact_current",
        "status_counts": data.get("status_counts") or {},
        "stale_source_keys": data.get("stale_source_keys") or [],
        "lagging_source_keys": data.get("lagging_source_keys") or [],
        "evidence_gap_warning": bool(data.get("evidence_gap_warning")),
        "scorer_guard": data.get("scorer_guard") or {},
        "sources": (data.get("sources") or [])[:50],
    }


def _evidence_auto_recovery_summary(payload: dict[str, Any] | None) -> dict[str, Any]:
    data = _result(payload)
    if not data:
        return {}
    status = data.get("status") or data.get("final_status")
    freshness_status = data.get("last_freshness_status") or data.get("final_freshness_status")
    stale_source_keys = data.get("last_stale_source_keys") or data.get("final_stale_source_keys") or []
    lagging_source_keys = data.get("last_lagging_source_keys") or data.get("final_lagging_source_keys") or []
    return {
        "version": data.get("evidence_refresh_watchdog_version"),
        "run_id": data.get("run_id"),
        "started_at": data.get("started_at"),
        "updated_at": data.get("updated_at"),
        "finished_at": data.get("finished_at"),
        "age_seconds": _age_seconds(data.get("updated_at") or data.get("finished_at")),
        "status": status,
        "last_tick_index": data.get("last_tick_index"),
        "executed_tick_count": data.get("executed_tick_count"),
        "last_refresh_status": data.get("last_refresh_status"),
        "last_refresh_queue_status_counts": data.get("last_refresh_queue_status_counts") or {},
        "last_freshness_status": freshness_status,
        "last_stale_source_keys": stale_source_keys,
        "last_lagging_source_keys": lagging_source_keys,
        "consecutive_failures": int(data.get("consecutive_failures") or 0),
        "current_backoff_seconds": data.get("current_backoff_seconds"),
        "cooldown_seconds": data.get("cooldown_seconds"),
        "next_refresh_eligible_at": data.get("next_refresh_eligible_at"),
        "gap_remains_after_refresh": status in {"refresh_completed_gap_remains", "skipped_cooldown"} and bool(stale_source_keys or lagging_source_keys),
        "tick_summaries": (data.get("tick_summaries") or [])[-5:],
    }


def _source_ingest_health_summary(payload: dict[str, Any] | None) -> dict[str, Any]:
    data = _result(payload)
    if not data:
        return {}
    rows = data.get("sources") or []
    age_seconds = _age_seconds(data.get("generated_at"))
    artifact_stale = bool(age_seconds is not None and age_seconds > SOURCE_ARTIFACT_STALE_SECONDS)
    raw_status = data.get("overall_status")
    effective_status = "red" if artifact_stale else raw_status
    return {
        "version": data.get("source_ingest_health_version"),
        "generated_at": data.get("generated_at"),
        "age_seconds": age_seconds,
        "artifact_stale": artifact_stale,
        "artifact_stale_threshold_seconds": SOURCE_ARTIFACT_STALE_SECONDS,
        "overall_status": raw_status,
        "effective_status": effective_status,
        "effective_status_reason": "artifact_stale" if artifact_stale else "artifact_current",
        "health_counts": data.get("health_counts") or {},
        "root_cause_counts": data.get("root_cause_counts") or {},
        "online_required_sources": data.get("online_required_sources") or [],
        "online_problem_source_keys": data.get("online_problem_source_keys") or [],
        "degraded_source_keys": data.get("degraded_source_keys") or [],
        "sources": rows[:50],
    }


def _source_online_supervisor_summary(payload: dict[str, Any] | None) -> dict[str, Any]:
    data = _result(payload)
    if not data:
        return {}
    tick_summaries = data.get("tick_summaries") or []
    latest_tick = tick_summaries[-1] if tick_summaries else {}
    actions = latest_tick.get("actions") or []
    failed_actions = [
        action
        for action in actions
        if str(action.get("status") or "").lower() in {"failed", "timeout", "skipped_live_ibkr_disabled"}
    ]
    provider_cooldown_actions = [
        action
        for action in actions
        if str(action.get("status") or "").lower() == "skipped_provider_rate_limited"
    ]
    completed_at = data.get("completed_at") or data.get("updated_at")
    age_seconds = _age_seconds(completed_at)
    artifact_stale = bool(age_seconds is not None and age_seconds > SOURCE_ARTIFACT_STALE_SECONDS)
    raw_status = data.get("final_status") or data.get("last_status")
    effective_status = "red" if artifact_stale else raw_status
    action_counts = data.get("final_action_status_counts") or latest_tick.get("action_status_counts") or {}
    online_problem_keys = data.get("final_online_problem_source_keys") or latest_tick.get("online_problem_source_keys") or []
    source_health_status = data.get("final_source_ingest_health_status") or latest_tick.get("source_ingest_health_status")
    yellow_reasons: list[str] = []
    if artifact_stale:
        yellow_reasons.append("artifact_stale")
    if str(raw_status or "").lower() in {"yellow", "watch"}:
        yellow_reasons.append("supervisor_reported_yellow")
    if provider_cooldown_actions:
        yellow_reasons.append("provider_rate_limit_or_cooldown")
    if action_counts.get("skipped_provider_rate_limited"):
        yellow_reasons.append("provider_rate_limited_actions_present")
    if source_health_status and str(source_health_status).lower() in {"yellow", "watch"}:
        yellow_reasons.append("source_ingest_health_watch")
    if online_problem_keys:
        yellow_reasons.append("online_problem_sources_present")
    if failed_actions:
        yellow_reasons.append("failed_actions_present")
    if not yellow_reasons and str(effective_status or "").lower() == "green":
        yellow_reasons.append("all_online_checks_green")
    operator_summary = (
        "Source supervisor is current; yellow is caused by provider cooldown/rate-limit or non-blocking watch items."
        if any(reason in yellow_reasons for reason in ("provider_rate_limit_or_cooldown", "provider_rate_limited_actions_present", "supervisor_reported_yellow"))
        and not failed_actions
        and not online_problem_keys
        else "Source supervisor is green/current."
        if yellow_reasons == ["all_online_checks_green"]
        else f"Source supervisor needs attention: {', '.join(yellow_reasons)}."
    )
    return {
        "version": data.get("source_online_supervisor_version"),
        "run_id": data.get("run_id"),
        "started_at": data.get("started_at"),
        "started_at_local": data.get("started_at_local") or _local_time_text(data.get("started_at")),
        "completed_at": completed_at,
        "completed_at_local": data.get("completed_at_local") or _local_time_text(completed_at),
        "age_seconds": age_seconds,
        "artifact_stale": artifact_stale,
        "artifact_stale_threshold_seconds": SOURCE_ARTIFACT_STALE_SECONDS,
        "executed_tick_count": data.get("executed_tick_count") or data.get("last_tick_index"),
        "max_ticks": data.get("max_ticks"),
        "interval_seconds": data.get("interval_seconds"),
        "sleep_between_ticks": data.get("sleep_between_ticks"),
        "final_status": raw_status,
        "effective_status": effective_status,
        "effective_status_reason": "artifact_stale" if artifact_stale else "artifact_current",
        "status_explanation": {
            "yellow_reasons": list(dict.fromkeys(yellow_reasons)),
            "operator_summary": operator_summary,
            "failed_action_count": len(failed_actions),
            "provider_cooldown_action_count": len(provider_cooldown_actions),
            "online_problem_source_keys": online_problem_keys,
            "source_ingest_health_status": source_health_status,
        },
        "final_action_status_counts": action_counts,
        "final_live_validation_status": data.get("final_live_validation_status") or latest_tick.get("live_validation_status"),
        "final_source_ingest_health_status": source_health_status,
        "final_online_problem_source_keys": online_problem_keys,
        "final_alpha_vantage_disabled": data.get("final_alpha_vantage_disabled") or latest_tick.get("alpha_vantage_disabled") or {"disabled": False},
        "latest_tick": {
            "tick_index": latest_tick.get("tick_index"),
            "checked_at": latest_tick.get("checked_at"),
            "checked_at_local": latest_tick.get("checked_at_local") or _local_time_text(latest_tick.get("checked_at")),
            "status": latest_tick.get("status"),
            "execute_actions": latest_tick.get("execute_actions"),
            "action_status_counts": latest_tick.get("action_status_counts") or {},
            "live_validation_status": latest_tick.get("live_validation_status"),
            "live_validation_source_status": latest_tick.get("live_validation_source_status") or {},
            "source_ingest_health_status": latest_tick.get("source_ingest_health_status"),
            "online_problem_source_keys": latest_tick.get("online_problem_source_keys") or [],
            "degraded_source_keys": latest_tick.get("degraded_source_keys") or [],
            "root_cause_counts": latest_tick.get("root_cause_counts") or {},
            "alpha_vantage_disabled": latest_tick.get("alpha_vantage_disabled") or data.get("final_alpha_vantage_disabled") or {"disabled": False},
            "actions": actions[-30:],
        },
        "failed_actions": failed_actions[-20:],
        "provider_cooldown_actions": provider_cooldown_actions[-20:],
        "state_json_path": data.get("state_json_path"),
        "tick_log_jsonl_path": data.get("tick_log_jsonl_path"),
    }


def _m236_news_learning_summary(payload: dict[str, Any] | None) -> dict[str, Any]:
    data = _result(payload)
    if not data:
        return {}
    summary = data.get("summary") or {}
    provider_profile = data.get("provider_profile") or {}
    top_providers = []
    for family, item in provider_profile.items():
        weight = item.get("learned_weight") or {}
        top_providers.append(
            {
                "family": family,
                "sample_count": item.get("sample_count", 0),
                "evaluated_horizon_count": item.get("evaluated_horizon_count", 0),
                "hit_rate": item.get("hit_rate"),
                "overall_weight": weight.get("overall"),
                "learning_status": item.get("learning_status"),
            }
        )
    top_providers.sort(key=lambda item: (item.get("sample_count") or 0, item.get("overall_weight") or 0), reverse=True)
    scorer_impact = data.get("scorer_impact") or {}
    ticker = data.get("ticker_inference_verification") or {}
    canonical = data.get("canonical_dedup") or {}
    retention = data.get("retention") or {}
    evaluated_index = data.get("evaluated_index") or {}
    learning_velocity = data.get("learning_velocity") or {}
    macro_release = data.get("macro_release_integration") or {}
    role_family_profile = data.get("provider_role_family_profile") or {}
    role_family_readiness = {}
    for role_family, providers in role_family_profile.items():
        evaluated = sum(int((profile or {}).get("evaluated_horizon_count") or 0) for profile in (providers or {}).values())
        active = sum(1 for profile in (providers or {}).values() if (profile or {}).get("learning_status") == "active")
        role_family_readiness[role_family] = {
            "evaluated_horizon_count": evaluated,
            "active_provider_count": active,
            "provider_count": len(providers or {}),
        }
    return {
        "version": data.get("m236_news_learning_version"),
        "generated_at": data.get("generated_at"),
        "generated_at_local": data.get("generated_at_local") or _local_time_text(data.get("generated_at")),
        "status": data.get("status"),
        "record_count": summary.get("record_count", 0),
        "tail_record_count": summary.get("tail_record_count", 0),
        "evaluated_index_record_count": summary.get("evaluated_index_record_count", 0),
        "evaluated_retained_record_count": summary.get("evaluated_retained_record_count", 0),
        "status_counts": summary.get("status_counts") or {},
        "due_horizon_counts": summary.get("due_horizon_counts") or {},
        "ticker_review_queue_count": summary.get("ticker_review_queue_count", 0),
        "repeated_canonical_event_count": summary.get("repeated_canonical_event_count", 0),
        "scorer_directive_counts": summary.get("scorer_directive_counts") or {},
        "top_providers": top_providers[:8],
        "ticker_confidence_buckets": ticker.get("confidence_buckets") or {},
        "ticker_review_bucket_counts": ticker.get("review_queue_bucket_counts") or {},
        "ticker_review_queue": (ticker.get("review_queue") or [])[:8],
        "top_repeated_events": (canonical.get("top_repeated_events") or [])[:8],
        "top_directives": (scorer_impact.get("top_directives") or [])[:8],
        "retention": retention,
        "evaluated_index": evaluated_index,
        "learning_velocity": learning_velocity,
        "macro_release_integration": macro_release,
        "role_family_readiness": role_family_readiness,
        "learning_plan": data.get("learning_plan") or {},
        "safety": data.get("safety") or {},
    }


def _live_scorer_learning_summary(payload: dict[str, Any] | None) -> dict[str, Any]:
    data = _result(payload)
    if not data:
        return {}
    counts = data.get("latest_counts") or {}
    candidate = data.get("latest_candidate") or {}
    tick_summaries = data.get("tick_summaries") or []
    latest_tick = tick_summaries[-1] if tick_summaries else {}
    completed_at = data.get("completed_at")
    return {
        "version": data.get("live_scorer_learning_version"),
        "status": data.get("latest_status") or latest_tick.get("status"),
        "shadow_only": bool(data.get("shadow_only", True)),
        "promotion_mode": data.get("promotion_mode") or "shadow_only",
        "evidence_packet_version": data.get("evidence_packet_version"),
        "scorer_name": data.get("scorer_name"),
        "scorer_version": data.get("scorer_version"),
        "started_at_local": data.get("started_at_local") or _local_time_text(data.get("started_at")),
        "completed_at": completed_at,
        "completed_at_local": data.get("completed_at_local") or _local_time_text(completed_at),
        "age_seconds": _age_seconds(completed_at),
        "prediction_count": counts.get("prediction_count", 0),
        "evidence_packet_v2_prediction_count": counts.get("evidence_packet_v2_prediction_count", 0),
        "packet_quality_counts": counts.get("packet_quality_counts") or {},
        "asset_mapping_confidence_counts": counts.get("asset_mapping_confidence_counts") or {},
        "timestamp_alignment_counts": counts.get("timestamp_alignment_counts") or {},
        "late_baseline_count": counts.get("late_baseline_count", 0),
        "avg_packet_quality_score": counts.get("avg_packet_quality_score"),
        "pending_outcome_count": counts.get("pending_outcome_count", 0),
        "blocked_outcome_count": counts.get("blocked_outcome_count", 0),
        "evaluated_outcome_count": counts.get("evaluated_outcome_count", 0),
        "adjustment_note_count": counts.get("adjustment_note_count", 0),
        "candidate_sample_count": candidate.get("sample_count", 0),
        "candidate_hit_rate": candidate.get("hit_rate"),
        "candidate_avg_absolute_error": candidate.get("avg_absolute_error"),
        "m232_hot_cache_status": latest_tick.get("m232_hot_cache_status"),
        "m232_hot_cache_usable": latest_tick.get("m232_hot_cache_usable"),
        "m232_hot_cache_age_seconds_at_read": latest_tick.get("m232_hot_cache_age_seconds"),
        "m232_hot_cache_adaptive_tier": latest_tick.get("m232_hot_cache_adaptive_tier"),
        "m232_consumer_report_age_seconds": _age_seconds(completed_at),
        "latest_tick": latest_tick,
        "safety": data.get("safety") or {},
    }


def _live_learning_llm_notes_summary(payload: dict[str, Any] | None) -> dict[str, Any]:
    data = _result(payload)
    if not data:
        return {}
    return {
        "version": data.get("m225_live_learning_llm_notes_version"),
        "model": data.get("model"),
        "note_version": data.get("note_version"),
        "candidate_count": data.get("candidate_count", 0),
        "created_count": data.get("created_count", 0),
        "skipped_count": data.get("skipped_count", 0),
        "rejected_count": data.get("rejected_count", 0),
        "error_count": data.get("error_count", 0),
        "status": data.get("status"),
        "backlog_count": data.get("backlog_count", 0),
        "adaptive_schedule": data.get("adaptive_schedule") or {},
        "error_type_counts": data.get("error_type_counts") or {},
        "cooldown_reasons": data.get("cooldown_reasons") or [],
        "next_eligible_at": data.get("next_eligible_at"),
        "next_eligible_at_local": _local_time_text(data.get("next_eligible_at")),
        "consecutive_error_ticks": data.get("consecutive_error_ticks", 0),
        "note_quality_counts_this_tick": data.get("note_quality_counts_this_tick") or {},
        "note_counts": data.get("note_counts") or {},
        "threshold_candidate_summary": data.get("threshold_candidate_summary") or {},
        "errors": data.get("errors") or [],
        "shadow_only": bool(data.get("shadow_only", True)),
        "completed_at": data.get("completed_at"),
        "completed_at_local": _local_time_text(data.get("completed_at")),
        "safety": data.get("safety") or {},
    }


def _threshold_candidate_evaluator_summary(payload: dict[str, Any] | None) -> dict[str, Any]:
    data = _result(payload)
    if not data:
        return {}
    return {
        "version": data.get("m226_threshold_candidate_evaluator_version"),
        "evaluated_at": data.get("evaluated_at"),
        "evaluated_at_local": _local_time_text(data.get("evaluated_at")),
        "candidate_count": data.get("candidate_count", 0),
        "candidate_count_total": data.get("candidate_count_total", data.get("candidate_count", 0)),
        "raw_candidate_count": data.get("raw_candidate_count", data.get("candidate_count", 0)),
        "semantic_family_count": data.get("semantic_family_count", data.get("candidate_count", 0)),
        "semantic_family_summary": data.get("semantic_family_summary") or {},
        "adaptive_refresh": data.get("adaptive_refresh") or {},
        "tick_count": data.get("tick_count", 1),
        "status_counts": data.get("status_counts") or {},
        "promotion_ready_count": data.get("promotion_ready_count", 0),
        "needs_more_samples_count": data.get("needs_more_samples_count", 0),
        "watch_count": data.get("watch_count", 0),
        "shadow_candidate_count": data.get("shadow_candidate_count", 0),
        "reject_count": data.get("reject_count", 0),
        "blocker_counts": data.get("blocker_counts") or {},
        "blocker_severity_counts": data.get("blocker_severity_counts") or {},
        "m225_focus_feedback": data.get("m225_focus_feedback") or {},
        "shadow_promotion_trial": data.get("shadow_promotion_trial") or {},
        "promotion_gate": data.get("promotion_gate") or {},
        "cycle_report": data.get("cycle_report") or {},
        "top_candidates": data.get("top_candidates") or [],
        "shadow_only": bool(data.get("shadow_only", True)),
        "safety": data.get("safety") or {},
    }


def _m232_hot_cache_summary(payload: dict[str, Any] | None) -> dict[str, Any]:
    data = _result(payload)
    if not data:
        return {}
    generated_at = data.get("generated_at")
    age = _age_seconds(generated_at)
    try:
        max_age = float(data.get("max_consumer_age_seconds") or 180.0)
    except (TypeError, ValueError):
        max_age = 180.0
    stale = age is None or age > max_age
    schedule = data.get("adaptive_schedule") or {}
    scorer = data.get("scorer_overlay_cache") or {}
    reflex = data.get("reflex_hot_cache") or {}
    option = data.get("option_structure_cache") or {}
    action = data.get("action_gate_cache") or {}
    geopolitical = data.get("geopolitical_hot_cache") or scorer.get("geopolitical_hot_cache") or {}
    context_refresh = data.get("context_learning_refresh") or {}
    return {
        "version": data.get("m232_hot_cache_version"),
        "status": "stale" if stale else "fresh",
        "usable": not stale,
        "generated_at": generated_at,
        "generated_at_local": _local_time_text(generated_at),
        "age_seconds": age,
        "max_consumer_age_seconds": max_age,
        "mode": data.get("mode"),
        "source_state": data.get("source_state") or {},
        "adaptive_schedule": schedule,
        "adaptive_tier": schedule.get("tier"),
        "adaptive_next_interval_seconds": schedule.get("next_interval_seconds"),
        "adaptive_changed_keys": schedule.get("changed_keys") or [],
        "adaptive_full_report_due": bool(schedule.get("full_report_due")),
        "full_report_latest_generated_at": data.get("full_report_latest_generated_at"),
        "full_report_next_due_in_seconds": data.get("full_report_next_due_in_seconds"),
        "tick_index": data.get("tick_index"),
        "source_counts": {
            "emergency_count": (data.get("source_state") or {}).get("emergency_count"),
            "option_outcome_count": (data.get("source_state") or {}).get("option_outcome_count"),
            "option_outcome_complete_count": (data.get("source_state") or {}).get("option_outcome_complete_count"),
            "m223_prediction_count": (data.get("source_state") or {}).get("m223_prediction_count"),
            "m223_evaluated_count": (data.get("source_state") or {}).get("m223_evaluated_count"),
        },
        "source_latest": {
            "emergency_latest": (data.get("source_state") or {}).get("emergency_latest"),
            "option_outcome_latest": (data.get("source_state") or {}).get("option_outcome_latest"),
            "latest_prediction_id": (data.get("source_state") or {}).get("m223_latest_prediction_id"),
            "latest_outcome_id": (data.get("source_state") or {}).get("m223_latest_outcome_id"),
        },
        "paper_scorer_ready": bool(scorer.get("paper_scorer_ready")),
        "conditional_overlay_available": bool(scorer.get("conditional_overlay_available")),
        "waitlist_count": int(scorer.get("waitlist_count") or 0),
        "watchlist_count": int(scorer.get("watchlist_count") or 0),
        "blocked_count": int(scorer.get("blocked_count") or 0),
        "withhold_reasons": scorer.get("withhold_reasons") or {},
        "reflex_candidate_count": int(reflex.get("reflex_candidate_count") or 0),
        "reflex_memory_count": int(reflex.get("memory_count") or 0),
        "reflex_top_assets": reflex.get("top_assets") or {},
        "option_memory_count": int(option.get("memory_count") or 0),
        "option_complete_rows": int(option.get("complete_rows") or 0),
        "option_scorer_use_strength_counts": option.get("scorer_use_strength_counts") or {},
        "option_threshold_candidate_context_count": int(option.get("threshold_candidate_context_count") or 0),
        "option_context_only_count": int(option.get("context_only_count") or 0),
        "geopolitical_hot_bucket_count": int(geopolitical.get("hot_bucket_count") or 0),
        "geopolitical_heat_color_counts": geopolitical.get("heat_color_counts") or {},
        "geopolitical_top_buckets": (geopolitical.get("top_buckets") or [])[:5],
        "action_gate_memory_count": int(action.get("memory_count") or 0),
        "action_gate_paper_entry_candidate_count": int(action.get("paper_entry_candidate_count") or 0),
        "action_gate_block_or_second_pass_count": int(action.get("block_or_second_pass_count") or 0),
        "context_learning_refresh_enabled": bool(context_refresh.get("enabled")),
        "context_learning_refresh_ok": context_refresh.get("ok"),
        "context_learning_refresh_errors": context_refresh.get("errors") or [],
        "safety": data.get("safety") or {},
    }


def _learning_pipeline_health(
    live_scorer_learning: dict[str, Any],
    live_learning_llm_notes: dict[str, Any],
    threshold_candidate_evaluator: dict[str, Any],
    m232_hot_cache: dict[str, Any] | None = None,
) -> dict[str, Any]:
    reasons: list[str] = []
    warnings: list[str] = []
    session_context = _current_us_option_session_context()
    m232_hot_cache = m232_hot_cache or {}
    if not live_scorer_learning:
        reasons.append("live_scorer_learning_missing")
    else:
        scorer_status = str(live_scorer_learning.get("status") or "").lower()
        if scorer_status not in {"collecting", "ok", "success", "completed"}:
            warnings.append(f"live_scorer_learning_status_{scorer_status or 'unknown'}")
    if not live_learning_llm_notes:
        reasons.append("live_learning_llm_notes_missing")
    else:
        note_status = str(live_learning_llm_notes.get("status") or "").lower()
        error_count = int(live_learning_llm_notes.get("error_count") or 0)
        consecutive_errors = int(live_learning_llm_notes.get("consecutive_error_ticks") or 0)
        if error_count > 0 or consecutive_errors > 0:
            reasons.append("live_learning_llm_notes_errors")
        elif note_status not in {"ok", "cooldown", "idle", "no_candidates", "success", "completed"}:
            warnings.append(f"live_learning_llm_notes_status_{note_status or 'unknown'}")
    if not threshold_candidate_evaluator:
        reasons.append("threshold_candidate_evaluator_missing")
    promotion_gate = threshold_candidate_evaluator.get("promotion_gate") or {}
    blocked_detail_reasons = promotion_gate.get("blocked_detail_reason_counts") or {}
    watch_detail_reasons = promotion_gate.get("watchlist_detail_reason_counts") or {}
    expected_deferred_count = int(blocked_detail_reasons.get("option_structure_expected_deferred_stale") or 0) + int(
        watch_detail_reasons.get("option_structure_expected_deferred_stale") or 0
    )
    stale_count = int(blocked_detail_reasons.get("option_structure_stale") or 0) + int(
        watch_detail_reasons.get("option_structure_stale") or 0
    )
    if expected_deferred_count:
        warnings.append("option_structure_waiting_for_regular_session_refresh")
    if stale_count and session_context.get("is_regular_option_session"):
        reasons.append("option_structure_stale_during_regular_session")
    elif stale_count:
        warnings.append("legacy_option_structure_stale_outside_regular_session")
    if m232_hot_cache:
        if not bool(m232_hot_cache.get("usable")):
            warnings.append("m232_hot_cache_stale_or_unusable")
    elif threshold_candidate_evaluator:
        warnings.append("m232_hot_cache_missing")
    if reasons:
        status = "red"
        label = "learning_pipeline_attention_required"
    elif warnings:
        status = "yellow"
        label = "learning_pipeline_healthy_with_wait_states"
    else:
        status = "green"
        label = "learning_pipeline_healthy"
    return {
        "version": "learning-pipeline-health-v1",
        "status": status,
        "label": label,
        "healthy": status in {"green", "yellow"},
        "blocking_reasons": reasons,
        "warnings": warnings,
        "option_session": session_context,
        "prediction_count": live_scorer_learning.get("prediction_count", 0),
        "adjustment_note_count": live_scorer_learning.get("adjustment_note_count", 0),
        "m225_backlog_count": live_learning_llm_notes.get("backlog_count", 0),
        "m225_next_interval_seconds": (live_learning_llm_notes.get("adaptive_schedule") or {}).get("next_interval_seconds"),
        "m226_promotion_ready_count": threshold_candidate_evaluator.get("promotion_ready_count", 0),
        "m226_gate_blocked_count": promotion_gate.get("blocked_count", 0),
        "m226_gate_watchlist_count": promotion_gate.get("watchlist_count", 0),
        "m226_expected_deferred_option_count": expected_deferred_count,
        "m226_stale_option_count": stale_count,
        "m232_hot_cache_status": m232_hot_cache.get("status"),
        "m232_hot_cache_usable": m232_hot_cache.get("usable"),
        "m232_hot_cache_age_seconds": m232_hot_cache.get("age_seconds"),
        "m232_hot_cache_adaptive_tier": m232_hot_cache.get("adaptive_tier"),
        "m232_hot_cache_next_interval_seconds": m232_hot_cache.get("adaptive_next_interval_seconds"),
        "m232_hot_cache_changed_keys": m232_hot_cache.get("adaptive_changed_keys") or [],
        "m232_paper_scorer_ready": m232_hot_cache.get("paper_scorer_ready"),
        "m232_conditional_overlay_available": m232_hot_cache.get("conditional_overlay_available"),
        "m232_scorer_read_status": live_scorer_learning.get("m232_hot_cache_status"),
        "m232_scorer_read_usable": live_scorer_learning.get("m232_hot_cache_usable"),
        "m232_age_at_last_scorer_read_seconds": live_scorer_learning.get("m232_hot_cache_age_seconds_at_read"),
        "m232_scorer_report_age_seconds": live_scorer_learning.get("m232_consumer_report_age_seconds"),
    }


def _singleton_lock_dashboard_status() -> dict[str, Any]:
    repo_root = Path(__file__).resolve().parent.parent
    return build_operational_lock_status(repo_root=repo_root)


def _dashboard_operational_runbook(snapshot: dict[str, Any]) -> dict[str, Any]:
    repo_root = Path(__file__).resolve().parent.parent
    py = repo_root / ".venv" / "Scripts" / "python.exe"
    singleton = snapshot.get("singleton_locks") or {}
    action_queue = snapshot.get("action_queue") or {}
    promotion_gate = ((snapshot.get("threshold_candidate_evaluator") or {}).get("promotion_gate") or {})
    zombie_count = int(singleton.get("zombie_output_stale_count") or 0)
    stale_count = int(singleton.get("stale_or_missing_count") or 0)
    hidden_summary = action_queue.get("hidden_action_summary") or {}
    immediate_checks = []
    if zombie_count:
        immediate_checks.append("Loop PID is alive but output artifact is stale; restart the affected loop before trusting the dashboard.")
    if stale_count:
        immediate_checks.append("One or more singleton locks are stale/missing; confirm background loops before market-open automation.")
    if int(hidden_summary.get("promotion_repair_hidden_count") or 0):
        immediate_checks.append("Promotion repair/recycle-return actions exist outside the visible action queue; inspect full_queue before promotion.")
    if not immediate_checks:
        immediate_checks.append("No immediate runbook blocker detected by singleton/action-queue checks.")
    return {
        "runbook_version": "trading-dashboard-operational-runbook-v1",
        "generated_at": snapshot.get("generated_at") or _iso_now(),
        "repo_root": str(repo_root),
        "python": str(py),
        "status": "attention_required" if zombie_count or stale_count else "ready_for_monitoring",
        "immediate_checks": immediate_checks,
        "start_commands": [
            f'cd /d "{repo_root}"',
            f'"{py}" main.py live-learning-llm-notes --profile-json config\\live_learning_llm_notes_profile_v1.json',
            f'"{py}" main.py threshold-candidate-evaluator --profile-json config\\threshold_candidate_evaluator_profile_v1.json',
            f'"{py}" main.py trading-dashboard-loop --profile-json config\\trading_dashboard_snapshot_profile_v1.json --max-ticks 0 --interval-seconds 1 --sleep-between-ticks --snapshot-json-output-path exports\\m220_trading_dashboard_snapshot.json --markdown-output-path exports\\m220_trading_dashboard_snapshot.md --html-output-path exports\\trading_command_center.html --summary-json-output-path exports\\m220_trading_dashboard_loop_summary.json --summary-markdown-output-path exports\\m220_trading_dashboard_loop_summary.md --tick-log-jsonl-path exports\\m220_trading_dashboard_loop_ticks.jsonl',
        ],
        "stop_commands": [
            "Stop Python processes whose command line contains this repo path and main.py, then remove locks\\*.lock only after confirming those processes stopped.",
        ],
        "verification_commands": [
            f'"{py}" main.py threshold-candidate-evaluator --profile-json config\\threshold_candidate_evaluator_profile_v1.json --max-ticks 1 --json-output-path exports\\m226_threshold_candidate_evaluator.json --output exports\\m226_threshold_candidate_evaluator.md',
            f'"{py}" main.py trading-dashboard-loop --profile-json config\\trading_dashboard_snapshot_profile_v1.json --max-ticks 1 --disable-telegram --snapshot-json-output-path exports\\m220_trading_dashboard_snapshot.json --markdown-output-path exports\\m220_trading_dashboard_snapshot.md --html-output-path exports\\trading_command_center.html --summary-json-output-path exports\\m220_trading_dashboard_loop_summary.json --summary-markdown-output-path exports\\m220_trading_dashboard_loop_summary.md --tick-log-jsonl-path exports\\m220_trading_dashboard_loop_ticks.jsonl',
        ],
        "success_criteria": [
            "singleton_locks.overall_status is green or yellow with no zombie_output_stale loops.",
            "Action Queue shows hidden promotion repair count separately from visible actions.",
            "Promotion Gate recycle_graduation_report is present.",
            "Telegram alerts include class counts: critical/warning/info.",
        ],
        "promotion_gate_reference": {
            "waitlist_count": promotion_gate.get("waitlist_count"),
            "watchlist_count": promotion_gate.get("watchlist_count"),
            "blocked_count": promotion_gate.get("blocked_count"),
            "recycle_graduation_report": promotion_gate.get("recycle_graduation_report") or {},
        },
        "safety": {
            "read_only_runbook": True,
            "broker_order_submitted": False,
            "transmit_enabled": False,
            "live_threshold_table_mutated": False,
        },
    }


def _option_confidence_recovery_plan(snapshot: dict[str, Any]) -> dict[str, Any]:
    promotion_gate = ((snapshot.get("threshold_candidate_evaluator") or {}).get("promotion_gate") or {})
    root_summary = promotion_gate.get("option_confidence_root_causes") or {}
    recycle = promotion_gate.get("recycle_learning") or {}
    supervisor = snapshot.get("options_market_structure_supervisor") or {}
    root_counts = root_summary.get("root_cause_counts") or {}
    repair_actions = [
        action
        for action in ((snapshot.get("action_queue") or {}).get("full_queue") or {}).get("actions", [])
        if str(action.get("action_id") or "").startswith("promotion_gate_repair_option_root:")
        or str(action.get("action_id") or "") == "promotion_gate_refresh_option_market_structure"
    ]
    steps: list[dict[str, Any]] = []
    if root_counts.get("index_or_family_coverage_gap"):
        steps.append(
            {
                "step": "repair_index_family_coverage",
                "why": "Direct/proxy index-family option coverage is weak.",
                "command_hint": "options-market-structure-supervisor --execute-actions with SPY/QQQ/IWM/DIA and optional SPX/NDX/RUT/VIX probes",
            }
        )
    if root_counts.get("open_interest_missing_or_volume_proxy"):
        steps.append(
            {
                "step": "refresh_open_interest_or_volume_proxy",
                "why": "Open interest is missing or volume proxy is required before option confidence can rise.",
                "command_hint": "fresh option snapshot during liquid regular-session minutes; preserve volume proxy fallback when OI is unavailable",
            }
        )
    if root_counts.get("wide_spread_or_liquidity_thin"):
        steps.append(
            {
                "step": "wait_or_retry_liquid_window",
                "why": "Wide spreads or thin liquidity are suppressing confidence.",
                "command_hint": "retry M218/M219 snapshots after open warmup or during the most liquid index/ETF option window",
            }
        )
    if not steps:
        steps.append(
            {
                "step": "monitor_option_confidence",
                "why": "No active option-confidence root cause is blocking promotion.",
                "command_hint": "continue normal M218/M219/M226 loop",
            }
        )
    terminal_count = int(((promotion_gate.get("recycle_graduation_report") or {}).get("terminal_candidate_count")) or 0)
    recycle_report = promotion_gate.get("recycle_graduation_report") or {}
    revival_review_queue: list[dict[str, Any]] = []
    for index, row in enumerate(recycle_report.get("dormant_or_retired_candidates") or [], start=1):
        candidate_key = str(row.get("candidate_key") or f"terminal_candidate_{index}")
        revival_review_queue.append(
            {
                "queue_id": f"revival_review:{index}:{candidate_key[:80]}",
                "candidate_key": candidate_key,
                "queue_status": "awaiting_option_confidence_revalidation",
                "lifecycle_status": row.get("lifecycle_status") or "terminal",
                "operational_gate_status": row.get("operational_gate_status"),
                "root_causes": row.get("root_causes") or [],
                "blocked_by": row.get("blocked_by") or [],
                "new_sample_count": row.get("new_sample_count"),
                "consecutive_option_confidence_ticks": row.get("consecutive_option_confidence_ticks"),
                "auto_promote": False,
                "required_checks": [
                    "fresh M218/M219 option market-structure snapshot",
                    "M225 fresh samples after recycle",
                    "M226 Promotion Gate retest",
                    "no drawdown/regime/freshness/option-structure blocker",
                ],
            }
        )
    for index, row in enumerate(recycle_report.get("near_graduation_candidates") or [], start=1):
        candidate_key = str(row.get("candidate_key") or f"near_graduation_{index}")
        revival_review_queue.append(
            {
                "queue_id": f"revival_near:{index}:{candidate_key[:80]}",
                "candidate_key": candidate_key,
                "queue_status": "near_revival_recheck",
                "lifecycle_status": row.get("lifecycle_status") or "near_graduation",
                "operational_gate_status": row.get("operational_gate_status"),
                "root_causes": row.get("root_causes") or [],
                "blocked_by": row.get("blocked_by") or [],
                "new_sample_count": row.get("new_sample_count"),
                "consecutive_option_confidence_ticks": row.get("consecutive_option_confidence_ticks"),
                "auto_promote": False,
                "required_checks": [
                    "confirm fresh outcome samples",
                    "confirm option confidence medium/high",
                    "operator-visible Promotion Gate review before activation",
                ],
            }
        )
    revival_revalidation = {
        "enabled": bool(root_counts or terminal_count),
        "status": "revival_review_required" if terminal_count else ("revalidate_root_causes" if root_counts else "not_required"),
        "terminal_candidate_count": terminal_count,
        "queue_count": len(revival_review_queue),
        "auto_promote_after_revalidation": False,
        "review_path": [
            "refresh option market-structure snapshot during liquid session",
            "compare root_cause_counts and quote quality against previous snapshot",
            "if improved, move to revival_review queue, not direct waitlist",
            "require fresh M225 samples before any Promotion Gate return",
        ],
        "minimum_success_conditions": [
            "core ETF option quote quality not red",
            "index/proxy family coverage no longer unavailable",
            "wide spread/liquidity root cause reduced",
            "new_samples_after_recycle improves",
        ],
    }
    status = "attention_required" if root_counts or int(recycle.get("recycle_candidate_count") or 0) else "healthy"
    return {
        "plan_version": "option-confidence-recovery-plan-v1",
        "status": status,
        "root_cause_counts": root_counts,
        "recycle_candidate_count": recycle.get("recycle_candidate_count"),
        "near_return_count": recycle.get("near_return_count"),
        "final_quote_quality_status": supervisor.get("final_quote_quality_status"),
        "final_optional_index_quote_quality_status": supervisor.get("final_optional_index_quote_quality_status"),
        "family_issue_summary": supervisor.get("m220_family_issue_summary") or {},
        "repair_action_count": len(repair_actions),
        "repair_actions": repair_actions[:10],
        "steps": steps,
        "revival_revalidation": revival_revalidation,
        "revival_review_queue": revival_review_queue[:25],
        "revival_review_queue_count": len(revival_review_queue),
        "success_criteria": [
            "option confidence medium/high family count increases",
            "Promotion Gate root_cause_counts shrink",
            "recycle_return_criteria has fewer blockers",
            "dormant candidates move to near_graduation or watchlist only after fresh samples",
        ],
        "safety": {
            "read_only_plan": True,
            "broker_order_submitted": False,
            "transmit_enabled": False,
            "live_threshold_table_mutated": False,
        },
    }


def _evidence_core_sources_currently_ready(
    *,
    evidence_freshness: dict[str, Any] | None = None,
    source_ingest_health: dict[str, Any] | None = None,
    source_online_supervisor: dict[str, Any] | None = None,
    closed_session_market_news_quiet: bool = False,
) -> bool:
    """Return True when current required online evidence sources are usable."""

    freshness = evidence_freshness or {}
    freshness_status = str(freshness.get("overall_status") or "").lower()
    if freshness_status not in {"green", "watch", "yellow"}:
        return False
    if freshness.get("stale_source_keys") or freshness.get("evidence_gap_warning"):
        return False
    scorer_guard = freshness.get("scorer_guard") if isinstance(freshness.get("scorer_guard"), dict) else {}
    scorer_guard_action = str(scorer_guard.get("action") or "").lower()
    if scorer_guard_action and scorer_guard_action != "normal":
        return False

    source_health = source_ingest_health or {}
    configured_online_sources = {
        str(item)
        for item in (source_health.get("online_required_sources") or [])
        if str(item)
    }
    core_online_sources = configured_online_sources or set(CORE_EVIDENCE_ONLINE_SOURCE_KEYS)
    lagging_source_keys = {str(item) for item in (freshness.get("lagging_source_keys") or []) if str(item)}
    lagging_core_sources = lagging_source_keys & core_online_sources
    if lagging_core_sources and not (closed_session_market_news_quiet and lagging_core_sources <= {"market_news"}):
        return False

    if source_health:
        source_health_status = str(source_health.get("overall_status") or "").lower()
        if source_health_status and source_health_status not in {"green", "watch", "yellow"}:
            return False
        source_problem_keys = {str(item) for item in (source_health.get("online_problem_source_keys") or []) if str(item)}
        source_degraded_keys = {str(item) for item in (source_health.get("degraded_source_keys") or []) if str(item)}
        if (source_problem_keys or source_degraded_keys) and not (
            closed_session_market_news_quiet and (source_problem_keys | source_degraded_keys) <= {"market_news"}
        ):
            return False

    supervisor = source_online_supervisor or {}
    if supervisor:
        supervisor_status = str(supervisor.get("final_status") or "").lower()
        supervisor_source_health = str(supervisor.get("final_source_ingest_health_status") or "").lower()
        if supervisor_status and supervisor_status not in {"green", "yellow"}:
            return False
        supervisor_problem_keys = {str(item) for item in (supervisor.get("final_online_problem_source_keys") or []) if str(item)}
        if supervisor_source_health and supervisor_source_health != "green" and not (
            closed_session_market_news_quiet
            and supervisor_source_health in {"yellow", "watch"}
            and supervisor_problem_keys <= {"market_news"}
        ):
            return False
        if supervisor.get("failed_actions"):
            return False
        if supervisor_problem_keys and not (closed_session_market_news_quiet and supervisor_problem_keys <= {"market_news"}):
            return False

    return True


def _closed_session_market_news_quiet(
    *,
    market_data_risk_gate: dict[str, Any] | None = None,
    evidence_freshness: dict[str, Any] | None = None,
    source_ingest_health: dict[str, Any] | None = None,
    source_online_supervisor: dict[str, Any] | None = None,
) -> bool:
    gate_market = (market_data_risk_gate or {}).get("market_data") if isinstance((market_data_risk_gate or {}).get("market_data"), dict) else {}
    closed_session = gate_market.get("closed_session_expected_stale") if isinstance(gate_market.get("closed_session_expected_stale"), dict) else {}
    if not closed_session.get("active"):
        return False
    freshness = evidence_freshness or {}
    if freshness.get("stale_source_keys") or freshness.get("evidence_gap_warning"):
        return False
    scorer_guard = freshness.get("scorer_guard") if isinstance(freshness.get("scorer_guard"), dict) else {}
    if str(scorer_guard.get("action") or "").lower() not in {"", "normal"}:
        return False
    lagging = {str(item) for item in (freshness.get("lagging_source_keys") or []) if str(item)}
    source_health = source_ingest_health or {}
    online_required = {str(item) for item in (source_health.get("online_required_sources") or []) if str(item)}
    core_sources = online_required or set(CORE_EVIDENCE_ONLINE_SOURCE_KEYS)
    if (lagging & core_sources) - {"market_news"}:
        return False
    source_problem = {str(item) for item in (source_health.get("online_problem_source_keys") or []) if str(item)}
    source_degraded = {str(item) for item in (source_health.get("degraded_source_keys") or []) if str(item)}
    if (source_problem | source_degraded) - {"market_news"}:
        return False
    supervisor = source_online_supervisor or {}
    supervisor_problem = {str(item) for item in (supervisor.get("final_online_problem_source_keys") or []) if str(item)}
    if supervisor_problem - {"market_news"}:
        return False
    if supervisor.get("failed_actions"):
        return False
    return bool("market_news" in lagging or "market_news" in source_problem or "market_news" in source_degraded or "market_news" in supervisor_problem)


def _evidence_auto_recovery_gap_is_currently_resolved(
    *,
    evidence_freshness: dict[str, Any] | None = None,
    evidence_auto_recovery: dict[str, Any] | None = None,
    source_ingest_health: dict[str, Any] | None = None,
    source_online_supervisor: dict[str, Any] | None = None,
    closed_session_market_news_quiet: bool = False,
) -> bool:
    """Return True when an old watchdog gap is superseded by current source state."""

    auto = evidence_auto_recovery or {}
    if not auto.get("gap_remains_after_refresh"):
        return False
    if int(auto.get("consecutive_failures") or 0) > 0:
        return False
    return _evidence_core_sources_currently_ready(
        evidence_freshness=evidence_freshness,
        source_ingest_health=source_ingest_health,
        source_online_supervisor=source_online_supervisor,
        closed_session_market_news_quiet=closed_session_market_news_quiet,
    )


def _status_lamp(
    *,
    risk: dict[str, Any],
    mark_quality: dict[str, Any],
    account: dict[str, Any],
    market_data: dict[str, Any] | None = None,
    market_data_risk_gate: dict[str, Any] | None = None,
    market_data_operating_auto_gate: dict[str, Any] | None = None,
    session_transition_release_governor: dict[str, Any] | None = None,
    evidence_freshness: dict[str, Any] | None = None,
    evidence_auto_recovery: dict[str, Any] | None = None,
    source_ingest_health: dict[str, Any] | None = None,
    source_online_supervisor: dict[str, Any] | None = None,
) -> dict[str, Any]:
    reasons: list[str] = []
    color = "green"
    risk_state = str(risk.get("risk_state") or "unknown")
    quality = mark_quality.get("quality") or {}
    refresh_counts = quality.get("refresh_status_counts") or {}
    action_counts = quality.get("quality_action_counts") or {}
    delayed = int(quality.get("delayed_row_count") or 0)
    missing = int(quality.get("missing_mark_count") or 0)
    fallback_ticks = int(quality.get("fallback_tick_count") or 0)
    buying_power = _num(account.get("buying_power"))
    available = _num(account.get("available_funds"))
    market_summary = (market_data or {}).get("summary") or {}
    market_status = str((market_data or {}).get("status") or market_summary.get("status") or "").lower()
    market_missing = int(market_summary.get("missing_price_count") or 0)
    market_warming = int(market_summary.get("warming_up_count") or 0)
    market_delayed = int(market_summary.get("delayed_count") or 0)
    market_errors = int(market_summary.get("error_count") or 0)
    market_alerts = int(market_summary.get("alert_count") or 0)
    market_gate_status = str((market_data_risk_gate or {}).get("gate_status") or "").lower()
    market_gate_action = str((market_data_risk_gate or {}).get("action") or "").lower()
    operating_mode = str((market_data_operating_auto_gate or {}).get("operating_mode") or "").lower()
    operating_defensive = (market_data_operating_auto_gate or {}).get("defensive_mode") or {}
    operating_auto_ready = _num((market_data_operating_auto_gate or {}).get("auto_ready_count"))
    operating_source_count = _num((market_data_operating_auto_gate or {}).get("source_preview_item_count"))
    operating_blocks_alpha = operating_defensive.get("ordinary_alpha_entries_allowed") is False
    release_status = str((session_transition_release_governor or {}).get("release_status") or "").lower()
    release_allows_alpha = (
        release_status in {"release_allowed", "release_allowed_with_haircut"}
        and (session_transition_release_governor or {}).get("ordinary_alpha_release_allowed") is True
    )
    evidence_status = str((evidence_freshness or {}).get("effective_status") or (evidence_freshness or {}).get("overall_status") or "").lower()
    evidence_artifact_stale = bool((evidence_freshness or {}).get("artifact_stale"))
    evidence_stale_count = len((evidence_freshness or {}).get("stale_source_keys") or [])
    evidence_gap_warning = bool((evidence_freshness or {}).get("evidence_gap_warning"))
    auto_status = str((evidence_auto_recovery or {}).get("status") or "").lower()
    auto_failures = int((evidence_auto_recovery or {}).get("consecutive_failures") or 0)
    auto_gap_remains = bool((evidence_auto_recovery or {}).get("gap_remains_after_refresh"))
    closed_session_news_quiet = _closed_session_market_news_quiet(
        market_data_risk_gate=market_data_risk_gate,
        evidence_freshness=evidence_freshness,
        source_ingest_health=source_ingest_health,
        source_online_supervisor=source_online_supervisor,
    )
    auto_gap_currently_resolved = _evidence_auto_recovery_gap_is_currently_resolved(
        evidence_freshness=evidence_freshness,
        evidence_auto_recovery=evidence_auto_recovery,
        source_ingest_health=source_ingest_health,
        source_online_supervisor=source_online_supervisor,
        closed_session_market_news_quiet=closed_session_news_quiet,
    )
    evidence_core_sources_ready = _evidence_core_sources_currently_ready(
        evidence_freshness=evidence_freshness,
        source_ingest_health=source_ingest_health,
        source_online_supervisor=source_online_supervisor,
        closed_session_market_news_quiet=closed_session_news_quiet,
    )
    source_health_status = str((source_ingest_health or {}).get("effective_status") or (source_ingest_health or {}).get("overall_status") or "").lower()
    source_health_artifact_stale = bool((source_ingest_health or {}).get("artifact_stale"))
    source_health_online_problems = len((source_ingest_health or {}).get("online_problem_source_keys") or [])
    source_health_monitoring_only = len((source_ingest_health or {}).get("monitoring_only_online_source_keys") or [])
    source_health_degraded = len((source_ingest_health or {}).get("degraded_source_keys") or [])
    supervisor_status = str((source_online_supervisor or {}).get("effective_status") or (source_online_supervisor or {}).get("final_status") or "").lower()
    supervisor_artifact_stale = bool((source_online_supervisor or {}).get("artifact_stale"))
    supervisor_source_health = str((source_online_supervisor or {}).get("final_source_ingest_health_status") or "").lower()
    supervisor_online_problems = len((source_online_supervisor or {}).get("final_online_problem_source_keys") or [])
    supervisor_monitoring_only = len((source_online_supervisor or {}).get("final_monitoring_only_online_source_keys") or [])
    supervisor_failed_actions = len((source_online_supervisor or {}).get("failed_actions") or [])
    if risk_state == "reduce_risk" or refresh_counts.get("block") or action_counts.get("block_new_entries"):
        color = "red"
        reasons.append("risk_or_mark_quality_blocks_new_entries")
    if missing > 0 or delayed > 0:
        color = "red"
        reasons.append("mark_quality_has_missing_or_delayed_rows")
    if market_errors > 0 or market_missing >= 5:
        color = "red"
        reasons.append("market_data_desk_red_or_missing_core_prices")
    elif market_status == "red":
        color = "red"
        reasons.append("market_data_desk_temperature_red")
    if market_gate_status == "block" or market_gate_action == "block_new_entries":
        color = "red"
        reasons.append("market_data_risk_gate_blocks_new_entries")
    elif market_gate_status == "defensive" or market_gate_action == "defensive_or_hedge_only":
        color = "red"
        reasons.append("market_data_risk_gate_defensive_only")
    if (operating_mode == "risk_reduction_only" or operating_blocks_alpha) and not release_allows_alpha:
        color = "red"
        reasons.append("market_data_operating_mode_blocks_alpha_entries")
    elif color != "red" and operating_source_count and operating_auto_ready == 0 and not release_allows_alpha:
        color = "yellow"
        reasons.append("market_data_operating_gate_has_no_auto_ready_intents")
    if release_status == "blocked":
        color = "red"
        reasons.append("session_transition_release_governor_blocks_alpha")
    elif color != "red" and release_status == "release_allowed_with_haircut":
        color = "yellow"
        reasons.append("session_transition_release_governor_haircut_active")
    if color != "red" and (risk_state == "monitor" or refresh_counts.get("warn") or action_counts.get("size_reduce") or fallback_ticks > 0):
        color = "yellow"
        reasons.append("monitor_or_size_reduce_condition_active")
    if color != "red" and (market_status == "yellow" or market_delayed > 0 or market_missing > 0 or market_alerts > 0):
        color = "yellow"
        reasons.append("market_data_desk_watch_condition_active")
    if color != "red" and market_gate_action == "allow_with_market_data_haircut":
        color = "yellow"
        reasons.append("market_data_risk_gate_haircut_active")
    if evidence_artifact_stale:
        color = "red"
        reasons.append("evidence_freshness_artifact_stale")
    elif color != "red" and (evidence_status in {"yellow", "watch"} or evidence_stale_count > 0 or evidence_gap_warning):
        color = "yellow"
        reasons.append(
            "evidence_freshness_closed_session_market_news_quiet"
            if closed_session_news_quiet
            else
            "evidence_freshness_background_lag_watch"
            if evidence_core_sources_ready
            else "evidence_freshness_gap_guard_active"
        )
    if color != "red" and auto_gap_remains and not auto_gap_currently_resolved:
        color = "yellow"
        reasons.append("evidence_auto_recovery_gap_remains_after_refresh")
    if source_health_artifact_stale:
        color = "red"
        reasons.append("source_ingest_health_artifact_stale")
    elif source_health_status == "red":
        color = "red"
        reasons.append("source_ingest_health_red")
    elif color != "red" and source_health_online_problems > 0:
        color = "yellow"
        reasons.append("source_ingest_health_closed_session_market_news_quiet" if closed_session_news_quiet else "source_ingest_health_online_gap")
    elif color != "red" and source_health_status in {"yellow", "watch"}:
        color = "yellow"
        reasons.append(
            "source_ingest_health_closed_session_market_news_quiet"
            if closed_session_news_quiet
            else "source_ingest_health_monitoring_only_gap"
            if source_health_monitoring_only
            else "source_ingest_health_online_gap"
        )
    if supervisor_artifact_stale:
        color = "red"
        reasons.append("source_online_supervisor_artifact_stale")
    elif supervisor_status == "red" or supervisor_failed_actions > 0:
        color = "red"
        reasons.append("source_online_supervisor_failure")
    elif color != "red" and supervisor_online_problems > 0:
        color = "yellow"
        reasons.append("source_online_supervisor_closed_session_market_news_quiet" if closed_session_news_quiet else "source_online_supervisor_online_gap")
    elif color != "red" and supervisor_source_health in {"red", "yellow", "watch"}:
        color = "yellow"
        reasons.append(
            "source_online_supervisor_closed_session_market_news_quiet"
            if closed_session_news_quiet
            else "source_online_supervisor_monitoring_only_gap"
            if supervisor_monitoring_only
            else "source_online_supervisor_online_gap"
        )
    if auto_failures >= 2 or auto_status in {"refresh_failed", "timeout"}:
        color = "red" if auto_failures >= 3 else ("yellow" if color != "red" else color)
        reasons.append("evidence_auto_recovery_needs_attention")
    if color == "green" and (buying_power is not None and buying_power < 100):
        color = "yellow"
        reasons.append("buying_power_below_100")
    if color == "green" and (available is not None and available < 100):
        color = "yellow"
        reasons.append("available_funds_below_100")
    if not reasons:
        reasons.append("all_primary_dashboard_checks_green")
    reason_categories = _status_reason_categories(reasons)
    final_alpha_entry_allowed = bool(
        (market_data_risk_gate or {}).get("new_entries_allowed")
        and not operating_blocks_alpha
        and (release_allows_alpha or not release_status or release_status in {"release_allowed", "release_allowed_with_haircut"})
        and not (refresh_counts.get("block") or action_counts.get("block_new_entries"))
    )
    alpha_entry_flow = {
        "flow_version": "final-alpha-entry-flow-v1",
        "market_data_risk_gate_allows": bool((market_data_risk_gate or {}).get("new_entries_allowed")),
        "market_data_risk_gate_status": (market_data_risk_gate or {}).get("gate_status"),
        "market_data_risk_gate_action": (market_data_risk_gate or {}).get("action"),
        "operating_mode_blocks": bool(operating_blocks_alpha or operating_mode == "risk_reduction_only"),
        "operating_mode": (market_data_operating_auto_gate or {}).get("operating_mode"),
        "session_governor_blocks": bool(release_status == "blocked" or ((session_transition_release_governor or {}) and not release_allows_alpha)),
        "session_release_status": (session_transition_release_governor or {}).get("release_status"),
        "risk_or_mark_quality_blocks": bool(refresh_counts.get("block") or action_counts.get("block_new_entries")),
        "final_alpha_entry_allowed": final_alpha_entry_allowed,
        "final_state": "alpha_allowed" if final_alpha_entry_allowed else "alpha_blocked_trade_restricted",
        "summary": (
            "Market-data gate allows entries, but operating/session safety still blocks final alpha entry."
            if (market_data_risk_gate or {}).get("new_entries_allowed") and not final_alpha_entry_allowed
            else "Final alpha entry path is open."
            if final_alpha_entry_allowed
            else "Final alpha entry path is blocked before market-data approval."
        ),
    }
    return {
        "color": color,
        "reasons": reasons,
        "reason_categories": reason_categories,
        "dominant_reason_category": reason_categories[0].get("category") if reason_categories else "normal",
        "risk_state": risk_state,
        "new_entries_allowed": risk.get("new_entries_allowed"),
        "mark_refresh_status_counts": refresh_counts,
        "quality_action_counts": action_counts,
        "market_data_status": market_status or None,
        "market_data_counts": {
            "delayed": market_delayed,
            "missing": market_missing,
            "warming_up": market_warming,
            "errors": market_errors,
            "alerts": market_alerts,
        },
        "market_data_risk_gate_status": (market_data_risk_gate or {}).get("gate_status"),
        "market_data_risk_gate_action": (market_data_risk_gate or {}).get("action"),
        "market_data_risk_gate_allowed": (market_data_risk_gate or {}).get("allowed"),
        "market_data_new_entries_allowed": (market_data_risk_gate or {}).get("new_entries_allowed"),
        "market_data_entry_size_multiplier": (market_data_risk_gate or {}).get("entry_size_multiplier"),
        "market_data_option_strategy_bias": (market_data_risk_gate or {}).get("option_strategy_bias"),
        "market_data_operating_mode": (market_data_operating_auto_gate or {}).get("operating_mode"),
        "market_data_operating_auto_ready_count": (market_data_operating_auto_gate or {}).get("auto_ready_count"),
        "market_data_operating_protective_candidate_count": ((market_data_operating_auto_gate or {}).get("protective_analysis_packet") or {}).get("candidate_count"),
        "market_data_operating_protective_ticketing_allowed": ((market_data_operating_auto_gate or {}).get("protective_analysis_packet") or {}).get("ticketing_allowed"),
        "session_transition_release_status": (session_transition_release_governor or {}).get("release_status"),
        "session_transition_release_allowed": (session_transition_release_governor or {}).get("ordinary_alpha_release_allowed"),
        "session_transition_release_size_multiplier": (session_transition_release_governor or {}).get("max_new_entry_size_multiplier"),
        "evidence_freshness_status": evidence_status or None,
        "evidence_freshness_artifact_stale": evidence_artifact_stale,
        "evidence_stale_source_count": evidence_stale_count,
        "evidence_auto_recovery_status": auto_status or None,
        "evidence_auto_recovery_failures": auto_failures,
        "evidence_auto_recovery_gap_remains_after_refresh": auto_gap_remains,
        "evidence_auto_recovery_gap_currently_resolved": auto_gap_currently_resolved,
        "evidence_core_sources_ready": evidence_core_sources_ready,
        "closed_session_market_news_quiet": closed_session_news_quiet,
        "source_ingest_health_status": source_health_status or None,
        "source_ingest_health_artifact_stale": source_health_artifact_stale,
        "source_ingest_health_online_problem_count": source_health_online_problems,
        "source_ingest_health_monitoring_only_online_count": source_health_monitoring_only,
        "source_ingest_health_degraded_count": source_health_degraded,
        "source_online_supervisor_status": supervisor_status or None,
        "source_online_supervisor_artifact_stale": supervisor_artifact_stale,
        "source_online_supervisor_failed_action_count": supervisor_failed_actions,
        "source_online_supervisor_online_problem_count": supervisor_online_problems,
        "source_online_supervisor_monitoring_only_online_count": supervisor_monitoring_only,
        "alpha_entry_flow": alpha_entry_flow,
    }


def _status_reason_category(reason: str) -> tuple[str, str]:
    reason = str(reason or "")
    if reason in {"all_primary_dashboard_checks_green"}:
        return ("normal", "Normal")
    if reason.startswith("source_") or reason.startswith("evidence_") or "freshness" in reason or "online_gap" in reason:
        return ("data_freshness", "Data freshness")
    if reason.startswith("market_data_desk") or reason in {"mark_quality_has_missing_or_delayed_rows"}:
        return ("market_stress", "Market stress")
    if reason.startswith("market_data_risk_gate") or reason.startswith("market_data_operating") or reason.startswith("risk_or_mark_quality"):
        return ("safety_gate_block", "Safety gate block")
    if reason.startswith("session_transition_release_governor"):
        return ("session_transition_block", "Session transition block")
    if "buying_power" in reason or "available_funds" in reason:
        return ("account_margin", "Account / margin")
    if "failure" in reason or "error" in reason:
        return ("system_error", "System error")
    return ("other", "Other")


def _status_reason_categories(reasons: list[str]) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}
    order = {
        "system_error": 0,
        "data_freshness": 1,
        "market_stress": 2,
        "safety_gate_block": 3,
        "session_transition_block": 4,
        "account_margin": 5,
        "normal": 9,
        "other": 8,
    }
    for reason in reasons:
        category, label = _status_reason_category(reason)
        bucket = buckets.setdefault(category, {"category": category, "label": label, "count": 0, "reasons": []})
        bucket["count"] += 1
        bucket["reasons"].append(reason)
    return sorted(buckets.values(), key=lambda item: (order.get(str(item.get("category")), 8), -int(item.get("count") or 0)))


def _market_data_temperature_diagnostics(market_data: dict[str, Any] | None) -> dict[str, Any]:
    data = market_data or {}
    summary = data.get("summary") or {}
    temperature = data.get("temperature") or {}
    components = temperature.get("components") or summary.get("component_temperatures") or {}
    rows = data.get("rows") or []
    component_rows = []
    for name, component in components.items():
        if not isinstance(component, dict):
            continue
        component_rows.append(
            {
                "component": str(name),
                "status": component.get("status"),
                "label": component.get("label"),
                "score": component.get("score"),
                "counts": {
                    key: value
                    for key, value in component.items()
                    if key.endswith("_count") or key in {"missing_count", "delayed_count", "error_count", "wide_spread_count", "market_move_alert_count"}
                },
            }
        )
    top_rows = []
    for row in rows:
        alerts = row.get("alerts") or []
        score = abs(float(row.get("gap_pct") or 0.0)) + abs(float(row.get("tick_move_pct") or 0.0))
        if alerts:
            score += len(alerts) * 10.0
        if row.get("error"):
            score += 50.0
        if row.get("delayed"):
            score += 20.0
        if score <= 0:
            continue
        top_rows.append(
            {
                "key": row.get("key"),
                "group": row.get("group"),
                "status": row.get("status"),
                "price": row.get("price"),
                "gap_pct": row.get("gap_pct"),
                "tick_move_pct": row.get("tick_move_pct"),
                "quote_spread_pct": row.get("quote_spread_pct"),
                "alerts": alerts,
                "score": round(score, 4),
            }
        )
    top_rows.sort(key=lambda item: float(item.get("score") or 0.0), reverse=True)
    root_causes = []
    if int(summary.get("error_count") or 0):
        root_causes.append("market_data_errors")
    if int(summary.get("missing_price_count") or 0):
        root_causes.append("missing_prices")
    if int(summary.get("delayed_count") or 0):
        root_causes.append("delayed_data")
    if int(summary.get("wide_spread_count") or 0):
        root_causes.append("wide_spread_quotes")
    if int(summary.get("market_move_alert_count") or 0):
        root_causes.append("gap_or_sudden_move_alerts")
    if int(summary.get("close_only_count") or 0):
        root_causes.append("close_only_quotes_present")
    return {
        "diagnostics_version": "market-data-temperature-diagnostics-v1",
        "status": data.get("status") or summary.get("status"),
        "label": temperature.get("label") or summary.get("temperature_label"),
        "score": temperature.get("score") if temperature.get("score") is not None else summary.get("temperature_score"),
        "risk_flags": temperature.get("risk_flags") or summary.get("risk_flags") or [],
        "root_causes": root_causes,
        "component_rows": component_rows,
        "top_contributors": top_rows[:10],
        "summary_counts": {
            "alerts": summary.get("alert_count"),
            "market_move_alerts": summary.get("market_move_alert_count"),
            "liquidity_alerts": summary.get("liquidity_alert_count"),
            "data_alerts": summary.get("data_alert_count"),
            "wide_spread": summary.get("wide_spread_count"),
            "severe_wide_spread": summary.get("severe_wide_spread_count"),
            "missing": summary.get("missing_price_count"),
            "delayed": summary.get("delayed_count"),
            "errors": summary.get("error_count"),
        },
    }


def _market_data_session_thresholds(market_data_session_overlay: dict[str, Any] | None = None) -> dict[str, Any]:
    session = market_data_session_overlay or {}
    session_name = str(session.get("session_name") or "unknown")
    is_regular = bool(session.get("is_regular_session"))
    if is_regular:
        values = {
            "wide_spread_pct": 0.75,
            "gap_only_pct": 1.25,
            "confirming_tick_move_pct": 0.12,
            "hard_gap_pct": 2.0,
            "monitoring_release_candidate_max": 0,
            "re_escalate_confirming_row_count": 1,
        }
    else:
        values = {
            "wide_spread_pct": 0.50,
            "gap_only_pct": 0.75,
            "confirming_tick_move_pct": 0.18,
            "hard_gap_pct": 2.5,
            "monitoring_release_candidate_max": 0,
            "re_escalate_confirming_row_count": 2,
        }
    return {
        "threshold_version": "market-data-session-threshold-table-v1",
        "session_name": session_name,
        "is_regular_session": is_regular,
        "values": values,
        "table": [
            {"metric": "wide_spread_pct", "regular": 0.75, "non_regular": 0.50, "unit": "pct"},
            {"metric": "gap_only_pct", "regular": 1.25, "non_regular": 0.75, "unit": "pct"},
            {"metric": "confirming_tick_move_pct", "regular": 0.12, "non_regular": 0.18, "unit": "pct"},
            {"metric": "hard_gap_pct", "regular": 2.0, "non_regular": 2.5, "unit": "pct"},
            {"metric": "re_escalate_confirming_row_count", "regular": 1, "non_regular": 2, "unit": "rows"},
        ],
        "policy": {
            "session_aware": True,
            "used_for_monitoring_only_red": True,
            "read_only": True,
        },
    }


def _market_data_symbol_anomaly_quarantine(
    market_data: dict[str, Any] | None,
    market_data_session_overlay: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = market_data or {}
    rows = data.get("rows") or []
    session = market_data_session_overlay or {}
    threshold_table = _market_data_session_thresholds(session)
    thresholds = threshold_table.get("values") or {}
    is_regular = bool(session.get("is_regular_session"))
    candidates = []
    for row in rows:
        alerts = [str(item) for item in (row.get("alerts") or [])]
        spread = _num(row.get("quote_spread_pct")) or 0.0
        gap = abs(_num(row.get("gap_pct")) or 0.0)
        tick = abs(_num(row.get("tick_move_pct")) or 0.0)
        close_only = bool(row.get("selected_source_is_close")) or "close_only" in " ".join(alerts)
        wide_spread = spread >= float(thresholds.get("wide_spread_pct") or 0.75) or any("wide_spread" in item for item in alerts)
        gap_only_thin = (
            gap >= float(thresholds.get("gap_only_pct") or 1.25)
            and tick < float(thresholds.get("confirming_tick_move_pct") or 0.12)
            and (wide_spread or close_only)
        )
        if not (wide_spread or gap_only_thin or close_only):
            continue
        severity = "quarantine_candidate"
        if row.get("error") or row.get("delayed"):
            severity = "data_quality_candidate"
        elif is_regular and spread < float(thresholds.get("hard_gap_pct") or 2.0) and gap >= float(thresholds.get("hard_gap_pct") or 2.0):
            severity = "watch_only"
        reason = []
        if wide_spread:
            reason.append("wide_spread_or_thin_quote")
        if gap_only_thin:
            reason.append("gap_without_confirming_tick_move")
        if close_only:
            reason.append("close_only_or_reference_quote")
        normal_tick_proxy = bool(
            not row.get("error")
            and not row.get("delayed")
            and not close_only
            and spread < float(thresholds.get("wide_spread_pct") or 0.75)
            and tick >= float(thresholds.get("confirming_tick_move_pct") or 0.12)
        )
        lifecycle_status = "quarantine_candidate"
        if severity == "watch_only":
            lifecycle_status = "watch_only"
        elif normal_tick_proxy:
            lifecycle_status = "eligible_for_release_review"
        candidates.append(
            {
                "key": row.get("key"),
                "group": row.get("group"),
                "severity": severity,
                "lifecycle_status": lifecycle_status,
                "reasons": reason,
                "quote_spread_pct": row.get("quote_spread_pct"),
                "gap_pct": row.get("gap_pct"),
                "tick_move_pct": row.get("tick_move_pct"),
                "alerts": alerts,
                "session_name": session.get("session_name"),
                "is_regular_session": is_regular,
                "release_check": {
                    "current_normal_tick_proxy": normal_tick_proxy,
                    "requires_consecutive_normal_ticks": 3,
                    "requires_no_error_or_delay": True,
                    "requires_non_close_price_source": True,
                },
            }
        )
    candidate_count = len(candidates)
    return {
        "quarantine_version": "market-data-symbol-anomaly-quarantine-v1",
        "status": "active" if candidate_count else "clear",
        "candidate_count": candidate_count,
        "candidate_keys": [item.get("key") for item in candidates[:20]],
        "candidates": candidates[:20],
        "policy": {
            "read_only": True,
            "does_not_remove_rows": True,
            "use_for_temperature_interpretation": True,
            "session_aware": True,
            "thresholds": threshold_table,
            "lifecycle": {
                "candidate": "Isolate likely thin/stale symbol contribution from desk-level RED interpretation.",
                "release_review": "Requires at least 3 consecutive normal ticks before leaving quarantine context.",
                "watch_only": "Keep visible but do not automatically block trading solely from this symbol.",
            },
        },
    }


def _market_temperature_session_adjustment(
    market_data: dict[str, Any] | None,
    market_data_session_overlay: dict[str, Any] | None,
    anomaly_quarantine: dict[str, Any] | None,
) -> dict[str, Any]:
    data = market_data or {}
    session = market_data_session_overlay or {}
    diagnostics = data.get("temperature_diagnostics") or {}
    summary = data.get("summary") or {}
    status = str(data.get("status") or diagnostics.get("status") or summary.get("status") or "").lower()
    root_causes = set(str(item) for item in (diagnostics.get("root_causes") or []))
    data_hard = bool(root_causes.intersection({"market_data_errors", "missing_prices", "delayed_data"}))
    session_name = str(session.get("session_name") or "")
    is_regular = bool(session.get("is_regular_session"))
    anomaly_count = int((anomaly_quarantine or {}).get("candidate_count") or 0)
    threshold_table = _market_data_session_thresholds(session)
    thresholds = threshold_table.get("values") or {}
    confirming_rows = []
    for row in data.get("rows") or []:
        gap = abs(_num(row.get("gap_pct")) or 0.0)
        tick = abs(_num(row.get("tick_move_pct")) or 0.0)
        if gap >= float(thresholds.get("gap_only_pct") or 1.25) and tick >= float(thresholds.get("confirming_tick_move_pct") or 0.12):
            confirming_rows.append(
                {
                    "key": row.get("key"),
                    "group": row.get("group"),
                    "gap_pct": row.get("gap_pct"),
                    "tick_move_pct": row.get("tick_move_pct"),
                }
            )
    confirming_row_count = len(confirming_rows)
    soft_roots = root_causes and root_causes.issubset({"wide_spread_quotes", "gap_or_sudden_move_alerts", "close_only_quotes_present"})
    false_positive_risk = bool(status == "red" and soft_roots and not data_hard and (not is_regular or anomaly_count > 0))
    monitoring_release_conditions = {
        "no_hard_data_root_causes": not data_hard,
        "no_quarantine_candidates": anomaly_count <= int(thresholds.get("monitoring_release_candidate_max") or 0),
        "not_red_or_soft_only": status != "red" or bool(soft_roots),
    }
    re_escalation_conditions = {
        "hard_data_root_causes_present": data_hard,
        "regular_session_confirming_market_move": bool(
            is_regular
            and confirming_row_count >= int(thresholds.get("re_escalate_confirming_row_count") or 1)
        ),
        "confirming_row_count": confirming_row_count,
        "confirming_rows": confirming_rows[:10],
    }
    re_escalate_now = bool(
        status == "red"
        and (
            re_escalation_conditions["hard_data_root_causes_present"]
            or re_escalation_conditions["regular_session_confirming_market_move"]
        )
    )
    adjusted_interpretation = "trade_blocking_market_stress"
    adjusted_trade_blocking = status == "red"
    adjusted_status = status or "unknown"
    if false_positive_risk and not re_escalate_now:
        adjusted_interpretation = "monitoring_only_temperature_red"
        adjusted_trade_blocking = False
        adjusted_status = "watch"
    elif re_escalate_now:
        adjusted_interpretation = "trade_blocking_market_stress_reescalated"
        adjusted_trade_blocking = True
        adjusted_status = "red"
    current_decision = "trade_blocking"
    if adjusted_interpretation == "monitoring_only_temperature_red":
        current_decision = "monitoring_only"
    elif status not in {"red", "yellow"}:
        current_decision = "released_or_clear"
    return {
        "adjustment_version": "market-temperature-session-adjustment-v1",
        "raw_status": status or None,
        "adjusted_status": adjusted_status,
        "adjusted_interpretation": adjusted_interpretation,
        "trade_blocking_market_temperature": bool(adjusted_trade_blocking),
        "false_positive_risk": false_positive_risk,
        "re_escalate_to_trade_blocking_now": re_escalate_now,
        "current_decision": current_decision,
        "session_name": session_name,
        "is_regular_session": is_regular,
        "root_causes": sorted(root_causes),
        "anomaly_quarantine_candidate_count": anomaly_count,
        "thresholds": threshold_table,
        "monitoring_only_lifecycle": {
            "release_conditions": monitoring_release_conditions,
            "release_now": all(bool(value) for value in monitoring_release_conditions.values()),
            "re_escalation_conditions": re_escalation_conditions,
            "re_escalate_now": re_escalate_now,
            "policy": {
                "monitoring_only_red_is_not_a_trade_block": True,
                "hard_data_or_confirmed_regular_session_move_reescalates": True,
                "stateful_clear_requires_future_ticks": True,
            },
        },
        "reason": (
            "Market temperature RED is treated as monitoring-only because it is driven by spread/gap/close-only signals "
            "without hard missing/error/delayed data in the current session context."
            if false_positive_risk and not re_escalate_now
            else "Market temperature was re-escalated because hard data roots or confirming regular-session market moves are present."
            if re_escalate_now
            else "Market temperature remains trade-blocking or non-red after session-aware review."
        ),
    }


def _status_trade_blocking_summary(status: dict[str, Any], market_adjustment: dict[str, Any] | None = None) -> dict[str, Any]:
    market_trade_blocking = bool((market_adjustment or {}).get("trade_blocking_market_temperature", True))
    non_blocking_restriction_reasons = {
        "evidence_freshness_closed_session_market_news_quiet",
        "evidence_freshness_background_lag_watch",
        "market_data_risk_gate_haircut_active",
        "session_transition_release_governor_haircut_active",
        "source_ingest_health_closed_session_market_news_quiet",
        "source_ingest_health_monitoring_only_gap",
        "source_online_supervisor_closed_session_market_news_quiet",
        "source_online_supervisor_monitoring_only_gap",
    }
    trade_blocking: list[str] = []
    monitoring_only: list[str] = []
    for reason in status.get("reasons") or []:
        category, _ = _status_reason_category(str(reason))
        if str(reason) in non_blocking_restriction_reasons:
            monitoring_only.append(str(reason))
        elif category == "market_stress" and not market_trade_blocking:
            monitoring_only.append(str(reason))
        elif category in {"normal"}:
            monitoring_only.append(str(reason))
        else:
            trade_blocking.append(str(reason))
    return {
        "summary_version": "status-trade-blocking-split-v1",
        "trade_blocking": bool(trade_blocking),
        "trade_blocking_reasons": trade_blocking,
        "monitoring_only_reasons": monitoring_only,
        "trade_blocking_count": len(trade_blocking),
        "monitoring_only_count": len(monitoring_only),
        "market_temperature_trade_blocking": market_trade_blocking,
    }


def _status_operational_readiness_overlay(status: dict[str, Any]) -> dict[str, Any]:
    """Split raw dashboard status into process health vs trading readiness.

    The raw status intentionally stays conservative for old consumers.  The
    display/alert layer is less noisy: a normal safety gate that blocks alpha
    entries should not look like a broken data collector.
    """

    reasons = [str(item) for item in (status.get("reasons") or [])]
    split = status.get("trade_blocking_summary") or {}
    trade_blocking_reasons = [str(item) for item in (split.get("trade_blocking_reasons") or [])]
    monitoring_only_reasons = [str(item) for item in (split.get("monitoring_only_reasons") or [])]

    operational_red_reasons: list[str] = []
    operational_yellow_reasons: list[str] = []
    readiness_block_reasons: list[str] = []
    readiness_watch_reasons: list[str] = []

    hard_operational_prefixes = (
        "evidence_freshness_artifact_stale",
        "source_ingest_health_artifact_stale",
        "source_online_supervisor_artifact_stale",
        "source_online_supervisor_failure",
        "source_ingest_health_red",
        "mark_quality_has_missing_or_delayed_rows",
        "market_data_desk_red_or_missing_core_prices",
    )
    soft_operational_prefixes = (
        "evidence_freshness_background_lag_watch",
        "evidence_freshness_gap_guard_active",
        "evidence_auto_recovery_gap_remains_after_refresh",
        "evidence_auto_recovery_needs_attention",
        "source_ingest_health_online_gap",
        "source_online_supervisor_online_gap",
        "market_data_desk_watch_condition_active",
        "market_data_desk_temperature_red",
        "monitor_or_size_reduce_condition_active",
    )
    readiness_prefixes = (
        "risk_or_mark_quality_blocks_new_entries",
        "market_data_risk_gate",
        "market_data_operating",
        "session_transition_release_governor",
        "buying_power_below_100",
        "available_funds_below_100",
    )

    for reason in reasons:
        if reason == "all_primary_dashboard_checks_green":
            continue
        if reason in trade_blocking_reasons and reason.startswith(readiness_prefixes):
            readiness_block_reasons.append(reason)
            continue
        if reason.startswith(hard_operational_prefixes):
            operational_red_reasons.append(reason)
            continue
        if reason in monitoring_only_reasons or reason.startswith(soft_operational_prefixes):
            operational_yellow_reasons.append(reason)
            continue
        if reason in trade_blocking_reasons:
            readiness_block_reasons.append(reason)
        else:
            readiness_watch_reasons.append(reason)

    raw_color = str(status.get("color") or "green").lower()
    if operational_red_reasons:
        operational_color = "red"
        operational_label = "operator_attention_required"
    elif operational_yellow_reasons or readiness_watch_reasons:
        operational_color = "yellow"
        operational_label = "online_with_watch_items"
    else:
        operational_color = "green"
        operational_label = "online"

    if readiness_block_reasons:
        readiness_color = "red"
        readiness_label = "alpha_entries_blocked"
    elif trade_blocking_reasons:
        readiness_color = "red"
        readiness_label = "trade_blocking"
    elif monitoring_only_reasons or readiness_watch_reasons:
        readiness_color = "yellow"
        readiness_label = "watch_or_restricted"
    else:
        readiness_color = "green"
        readiness_label = "ready"

    if operational_color == "red":
        display_color = "red"
    elif readiness_color == "red":
        display_color = "yellow"
    elif operational_color == "yellow" or readiness_color == "yellow" or raw_color == "yellow":
        display_color = "yellow"
    else:
        display_color = "green"

    return {
        "overlay_version": "status-operational-readiness-overlay-v1",
        "raw_color": raw_color,
        "display_color": display_color,
        "alert_color": display_color,
        "display_label": (
            "system_attention_required"
            if display_color == "red"
            else "system_online_trade_restricted"
            if display_color == "yellow" and readiness_color == "red"
            else "online_watch"
            if display_color == "yellow"
            else "online"
        ),
        "operational_status": {
            "color": operational_color,
            "label": operational_label,
            "red_reasons": operational_red_reasons,
            "watch_reasons": operational_yellow_reasons + readiness_watch_reasons,
            "online": operational_color != "red",
        },
        "trading_readiness": {
            "color": readiness_color,
            "label": readiness_label,
            "alpha_entries_allowed": not bool(readiness_block_reasons or trade_blocking_reasons),
            "defensive_or_hedge_only": bool(readiness_block_reasons or trade_blocking_reasons),
            "blocking_reasons": readiness_block_reasons or trade_blocking_reasons,
            "watch_reasons": readiness_watch_reasons,
        },
        "policy": {
            "raw_status_preserved_as_raw_color": True,
            "status_color_is_effective_display_color": True,
            "display_status_separates_system_health_from_trade_safety": True,
            "normal_safety_blocks_are_not_system_failures": True,
        },
    }


def _status_natural_language_ko(status: dict[str, Any]) -> str:
    overlay = status.get("operational_readiness_overlay") or {}
    operational = overlay.get("operational_status") or {}
    readiness = overlay.get("trading_readiness") or {}
    if operational.get("color") in {"green", "yellow"} and readiness.get("color") == "red":
        return (
            "데이터/대시보드 수집은 온라인 상태로 보입니다. 다만 현재는 안전 게이트나 세션 전환 조건 때문에 "
            "신규 알파 진입만 차단되어 있고, 방어/헤지/리스크 축소 경로를 우선 검토하는 상태입니다."
        )
    split = status.get("trade_blocking_summary") or {}
    cats = {str(item.get("category")) for item in status.get("reason_categories") or []}
    if status.get("color") == "green":
        return "현재 핵심 전광판은 정상입니다. 그래도 실제 주문 전에는 별도 안전 게이트를 확인해야 합니다."
    parts = []
    if "market_stress" in cats:
        parts.append("시장 스트레스")
    if "safety_gate_block" in cats:
        parts.append("안전 게이트 차단")
    if "session_transition_block" in cats:
        parts.append("세션 전환 미해제")
    if "data_freshness" in cats:
        parts.append("데이터 신선도 문제")
    if "system_error" in cats:
        parts.append("시스템 오류")
    label = " + ".join(parts) if parts else "주의 조건"
    if split.get("trade_blocking"):
        return f"현재 신규 진입 금지 이유는 {label}입니다. 방어/헤지/리스크 축소 경로만 우선 검토하는 상태입니다."
    return f"현재 RED/주의 표시는 {label} 때문이지만, trade-blocking보다는 monitoring-only 성격이 큽니다. 추가 확인 후 제한적으로 판단하십시오."


def _safety_gate_latency_budget(
    stages: list[dict[str, Any]],
    source_read_latencies: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    total = sum(float(item.get("elapsed_ms") or 0.0) for item in stages)
    top = sorted(stages, key=lambda item: float(item.get("elapsed_ms") or 0.0), reverse=True)[:10]
    source_rows = source_read_latencies or []
    slow_sources = sorted(source_rows, key=lambda item: float(item.get("elapsed_ms") or 0.0), reverse=True)[:10]
    status = "green"
    if total > 1000:
        status = "red"
    elif total > 250:
        status = "yellow"
    return {
        "budget_version": "safety-gate-latency-budget-v1",
        "status": status,
        "total_elapsed_ms": round(total, 3),
        "stage_count": len(stages),
        "top_stages": top,
        "stages": stages,
        "source_read_latencies": source_rows,
        "slowest_sources": slow_sources,
        "budgets_ms": {
            "green_total_under": 250,
            "yellow_total_under": 1000,
            "single_source_warn_over": 50,
        },
        "safety": {
            "measurement_only": True,
            "does_not_skip_gates": True,
            "broker_order_submitted": False,
            "transmit_enabled": False,
        },
    }


def _dashboard_status_timeline(
    tick_log_jsonl_path: Path | None = None,
    *,
    limit: int = 120,
) -> dict[str, Any]:
    path = tick_log_jsonl_path or (Path(__file__).resolve().parent.parent / "exports" / "m220_trading_dashboard_loop_ticks.jsonl")
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        return {
            "timeline_version": "dashboard-status-timeline-v1",
            "status": "not_available",
            "source_path": str(resolved),
            "window_row_count": 0,
            "transition_count": 0,
            "transitions": [],
            "rows": [],
        }
    try:
        raw_lines = resolved.read_text(encoding="utf-8", errors="replace").splitlines()[-max(1, int(limit)) :]
    except Exception as exc:
        return {
            "timeline_version": "dashboard-status-timeline-v1",
            "status": "read_error",
            "source_path": str(resolved),
            "error": f"{exc.__class__.__name__}: {str(exc)[:300]}",
            "window_row_count": 0,
            "transition_count": 0,
            "transitions": [],
            "rows": [],
        }
    rows: list[dict[str, Any]] = []
    for line in raw_lines:
        try:
            payload = json.loads(line)
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        rows.append(
            {
                "tick_index": payload.get("tick_index"),
                "completed_at": payload.get("completed_at"),
                "status_color": payload.get("status_color"),
                "status_reasons": payload.get("status_reasons") or [],
                "action_queue_highest_severity": payload.get("action_queue_highest_severity"),
                "market_temperature_label": payload.get("market_temperature_label"),
                "market_temperature_score": payload.get("market_temperature_score"),
                "market_temperature_adjusted_interpretation": payload.get("market_temperature_adjusted_interpretation"),
                "trade_blocking": payload.get("trade_blocking"),
                "symbol_quarantine_count": payload.get("symbol_quarantine_count"),
                "latency_total_ms": payload.get("safety_gate_latency_total_ms"),
            }
        )
    transitions: list[dict[str, Any]] = []
    previous: dict[str, Any] | None = None
    for row in rows:
        if previous is not None:
            previous_tick = _num(previous.get("tick_index"))
            current_tick = _num(row.get("tick_index"))
            run_boundary = bool(previous_tick is not None and current_tick is not None and current_tick <= previous_tick)
            changed = []
            for key in (
                "status_color",
                "action_queue_highest_severity",
                "market_temperature_label",
                "market_temperature_adjusted_interpretation",
                "trade_blocking",
            ):
                if previous.get(key) != row.get(key):
                    changed.append(key)
            if changed and not run_boundary:
                transitions.append(
                    {
                        "from_tick": previous.get("tick_index"),
                        "to_tick": row.get("tick_index"),
                        "at": row.get("completed_at"),
                        "changed_fields": changed,
                        "from_status": previous.get("status_color"),
                        "to_status": row.get("status_color"),
                        "to_reasons": row.get("status_reasons") or [],
                        "to_action_queue_severity": row.get("action_queue_highest_severity"),
                    }
                )
        previous = row
    return {
        "timeline_version": "dashboard-status-timeline-v1",
        "status": "ok",
        "source_path": str(resolved),
        "window_row_count": len(rows),
        "latest_status": rows[-1].get("status_color") if rows else None,
        "transition_count": len(transitions),
        "transitions": transitions[-20:],
        "rows": rows[-20:],
    }


def _menus() -> list[dict[str, Any]]:
    return [
        {"id": "overview", "label": "Overview", "description": "Top status, AI opinion, and operating mode."},
        {"id": "status_timeline", "label": "Status Timeline", "description": "Recent dashboard state transitions and RED/YELLOW/monitoring-only changes."},
        {"id": "action_queue", "label": "Action Queue", "description": "Prioritized read-only recovery and risk-response actions."},
        {"id": "recovery_plan", "label": "Recovery Plan", "description": "Read-only dry-run recovery plan derived from the action queue."},
        {"id": "recovery_executor", "label": "Recovery Executor", "description": "Allowlist-only read-only recovery executor status."},
        {"id": "recovery_post_validation", "label": "Post-Validation", "description": "Before/after recovery validation and delta summary."},
        {"id": "account", "label": "Account / Margin", "description": "Cash, buying power, margin, open orders, and live account positions."},
        {"id": "positions", "label": "Positions", "description": "Paper and account positions with marks and PnL."},
        {"id": "plans", "label": "Buy/Sell Plan", "description": "Pending paper intents and blocked/allowed reasons."},
        {"id": "options_desk", "label": "Options Desk", "description": "Option positions, protective candidates, multi-leg structures, spreads, and quote-session readiness."},
        {"id": "adaptive_single_stock_options", "label": "Single-Stock Options", "description": "Adaptive institution-style single-stock option watchlist with tier, TTL, quote quality, and learning feedback."},
        {"id": "options_market_structure", "label": "Options Structure", "description": "Gamma exposure, call/put walls, max pain, and pin-risk overlay from option-market snapshots."},
        {"id": "risk", "label": "Risk", "description": "Portfolio risk state, breaches, warnings, and exposure metrics."},
        {"id": "news", "label": "News Ticker", "description": "Latest one-line IBKR/external headlines."},
        {"id": "geopolitical_desk", "label": "Geo Desk", "description": "Region/theme geopolitical heatmap, adaptive assets, and desk action context."},
        {"id": "regimes", "label": "Long Regimes", "description": "Standing market regimes and threshold context."},
        {"id": "market_data", "label": "Market Data", "description": "Mark refresh, live/delayed status, and market desk data."},
        {"id": "market_data_session", "label": "Market Session", "description": "Session-aware market-data interpretation for overnight stock/ETF monitoring and option quote deferral."},
        {"id": "operating_gate", "label": "Operating Gate", "description": "M182 operating-mode auto gate, execution blocking, and protective option analysis state."},
        {"id": "live_capital_validation", "label": "Live Capital", "description": "M243 live-account read-only capacity validation and max feasible size by candidate."},
        {"id": "capital_pipeline", "label": "Capital Pipeline", "description": "M244 freshness/dependency audit for M237-M243 capital, allocation, paper execution, and live read-only validation artifacts."},
        {"id": "strategy_allocation", "label": "Strategy Allocation", "description": "M241 paper/read-only allocation across stock, options, defined-risk spreads, hedge-only, and blocked candidates."},
        {"id": "micro_option_candidates", "label": "Micro Options", "description": "M247 micro-cash long-option candidate selector using stored quote snapshots, capital budgets, and M246 policy."},
        {"id": "paper_option_ticket_preview", "label": "Option Tickets", "description": "M248 paper-only option ticket previews, including single-leg and defined-risk multi-leg spread counts."},
        {"id": "option_whatif_safety_validation", "label": "Option What-If", "description": "M249 local option safety and broker what-if readiness; live routing remains disabled."},
        {"id": "limited_auto_trading_readiness", "label": "Auto Readiness", "description": "M250 final read-only/paper limited auto-trading readiness guard; live routing stays locked."},
        {"id": "evidence_freshness", "label": "Evidence Freshness", "description": "Fresh/lagging/stale status for news, geopolitical, macro, regime, and market-data evidence."},
        {"id": "auto_recovery", "label": "Auto-Recovery", "description": "Evidence refresh watchdog status, cooldown, backoff, and latest recovery outcome."},
        {"id": "source_doctor", "label": "Data Source Doctor", "description": "Source ingest health, root causes, and online-required evidence gaps."},
        {"id": "source_supervisor", "label": "Source Supervisor", "description": "Continuous online-required source loop, catch-up actions, and provider cooldown state."},
        {"id": "m236_news_learning", "label": "M236 News Learning", "description": "Provider weights, ticker confidence, canonical freshness, and scorer source-quality directives."},
        {"id": "macro_release_interpreter", "label": "Macro Releases", "description": "Structured macro release surprise, asset-specific bias, market reaction, and upcoming FOMC/indicator calendar."},
        {"id": "live_learning", "label": "Live Learning", "description": "M223 shadow scorer learning, outcomes, adjustment notes, and threshold candidates."},
        {"id": "m232_control_plane", "label": "M232 Control Plane", "description": "Adaptive scorer/reflex/options/action-gate hot-cache status for live learning application."},
        {"id": "macro", "label": "Macro / Economic", "description": "Economic desk risk bias, flags, and takeaways."},
        {"id": "logs", "label": "Logs", "description": "Recent daemon/daily JSONL output and data freshness."},
        {"id": "settings", "label": "Settings", "description": "Profiles, safety flags, and output paths."},
    ]


def _ai_opinion(status: dict[str, Any], *, risk: dict[str, Any], mark_quality: dict[str, Any], news: list[dict[str, Any]], regimes: list[dict[str, Any]]) -> dict[str, Any]:
    color = status.get("color")
    readiness = ((status.get("operational_readiness_overlay") or {}).get("trading_readiness") or {})
    trade_blocking = bool((status.get("trade_blocking_summary") or {}).get("trade_blocking"))
    if color == "red" or trade_blocking or readiness.get("color") == "red":
        stance = "do_not_add_new_risk"
        note = "새 진입보다 데이터 품질/리스크 원인 확인이 우선입니다."
    elif color == "yellow":
        stance = "reduced_size_only"
        note = "운영은 가능하지만 sizing 축소와 원인 확인을 병행하는 편이 안전합니다."
    else:
        stance = "normal_paper_operation"
        note = "핵심 계기판은 정상입니다. 그래도 live 주문 전에는 별도 승인/가드가 필요합니다."
    return {
        "stance": stance,
        "note_ko": status.get("natural_language_ko") or note,
        "focus": [
            "mark_refresh_quality",
            "market_data_temperature",
            "portfolio_risk_state",
            "cash_and_margin",
            "latest_breaking_news",
            "standing_regime_context",
        ],
        "watch_items": [
            "fallback_tick_count" if (mark_quality.get("quality") or {}).get("fallback_tick_count") else None,
            "risk_breaches" if risk.get("breach_count") else None,
            "news_velocity" if len(news) >= 5 else None,
            "active_regime_count" if regimes else None,
        ],
    }


def build_trading_dashboard_snapshot(
    *,
    account_snapshot_path: Path | None,
    paper_portfolio_state_path: Path | None,
    portfolio_risk_state_path: Path | None,
    execution_intent_ledger_path: Path | None,
    mark_quality_daemon_path: Path | None,
    economic_desk_path: Path | None,
    market_data_path: Path | None,
    database_path: Path | None,
    market_data_session_overlay_path: Path | None = None,
    market_data_risk_gate_path: Path | None = None,
    market_data_operating_auto_gate_path: Path | None = None,
    capital_margin_state_path: Path | None = None,
    capital_aware_action_gate_path: Path | None = None,
    live_read_only_capital_validation_path: Path | None = None,
    capital_pipeline_orchestrator_path: Path | None = None,
    market_data_session_transition_gate_path: Path | None = None,
    session_transition_release_governor_path: Path | None = None,
    market_data_operating_execution_intent_ledger_path: Path | None = None,
    operating_defensive_action_plan_path: Path | None = None,
    operating_defensive_intent_ledger_path: Path | None = None,
    evidence_freshness_path: Path | None = None,
    evidence_refresh_watchdog_path: Path | None = None,
    source_ingest_health_path: Path | None = None,
    source_online_supervisor_path: Path | None = None,
    live_scorer_learning_path: Path | None = None,
    live_learning_llm_notes_path: Path | None = None,
    threshold_candidate_evaluator_path: Path | None = None,
    m232_hot_cache_path: Path | None = None,
    execution_safety_cache_supervisor_path: Path | None = None,
    options_market_structure_path: Path | None = None,
    options_market_structure_supervisor_path: Path | None = None,
    single_stock_option_watchlist_path: Path | None = None,
    strategy_allocation_engine_path: Path | None = None,
    micro_option_candidates_path: Path | None = None,
    paper_option_ticket_preview_path: Path | None = None,
    option_whatif_safety_validation_path: Path | None = None,
    limited_auto_trading_readiness_path: Path | None = None,
    news_limit: int = 8,
    regime_limit: int = 8,
    mode: str = "paper",
) -> dict[str, Any]:
    build_started = time.perf_counter()
    last_stage = build_started
    latency_stages: list[dict[str, Any]] = []
    source_read_latencies: list[dict[str, Any]] = []

    def mark_stage(name: str) -> None:
        nonlocal last_stage
        now = time.perf_counter()
        latency_stages.append({"stage": name, "elapsed_ms": round((now - last_stage) * 1000.0, 3)})
        last_stage = now

    sources: dict[str, Any] = {}
    missing_sources: list[str] = []

    loaded: dict[str, dict[str, Any] | None] = {}
    if single_stock_option_watchlist_path is None:
        default_single_stock_watchlist = Path("exports/m238_single_stock_option_watchlist_loop.json")
        if default_single_stock_watchlist.exists():
            single_stock_option_watchlist_path = default_single_stock_watchlist
    if limited_auto_trading_readiness_path is None:
        default_limited_auto_readiness = Path("exports/m250_limited_auto_trading_readiness.json")
        if default_limited_auto_readiness.exists():
            limited_auto_trading_readiness_path = default_limited_auto_readiness
    if paper_option_ticket_preview_path is None:
        default_paper_option_ticket_preview = Path("exports/m248_paper_option_ticket_preview.json")
        if default_paper_option_ticket_preview.exists():
            paper_option_ticket_preview_path = default_paper_option_ticket_preview
    if option_whatif_safety_validation_path is None:
        default_option_whatif_safety_validation = Path("exports/m249_option_whatif_safety_validation.json")
        if default_option_whatif_safety_validation.exists():
            option_whatif_safety_validation_path = default_option_whatif_safety_validation
    for key, path in {
        "account_snapshot": account_snapshot_path,
        "paper_portfolio_state": paper_portfolio_state_path,
        "portfolio_risk_state": portfolio_risk_state_path,
        "execution_intent_ledger": execution_intent_ledger_path,
        "mark_quality_daemon": mark_quality_daemon_path,
        "economic_desk": economic_desk_path,
        "market_data": market_data_path,
        "market_data_session_overlay": market_data_session_overlay_path,
        "market_data_risk_gate": market_data_risk_gate_path,
        "market_data_operating_auto_gate": market_data_operating_auto_gate_path,
        "capital_margin_state": capital_margin_state_path,
        "capital_aware_action_gate": capital_aware_action_gate_path,
        "live_read_only_capital_validation": live_read_only_capital_validation_path,
        "capital_pipeline_orchestrator": capital_pipeline_orchestrator_path,
        "market_data_session_transition_gate": market_data_session_transition_gate_path,
        "session_transition_release_governor": session_transition_release_governor_path,
        "market_data_operating_execution_intent_ledger": market_data_operating_execution_intent_ledger_path,
        "operating_defensive_action_plan": operating_defensive_action_plan_path,
        "operating_defensive_intent_ledger": operating_defensive_intent_ledger_path,
        "evidence_freshness": evidence_freshness_path,
        "evidence_refresh_watchdog": evidence_refresh_watchdog_path,
        "source_ingest_health": source_ingest_health_path,
        "source_online_supervisor": source_online_supervisor_path,
        "live_scorer_learning": live_scorer_learning_path,
        "live_learning_llm_notes": live_learning_llm_notes_path,
        "threshold_candidate_evaluator": threshold_candidate_evaluator_path,
        "m232_hot_cache": m232_hot_cache_path,
        "execution_safety_cache_supervisor": execution_safety_cache_supervisor_path,
        "options_market_structure": options_market_structure_path,
        "options_market_structure_supervisor": options_market_structure_supervisor_path,
        "single_stock_option_watchlist": single_stock_option_watchlist_path,
        "strategy_allocation_engine": strategy_allocation_engine_path,
        "micro_option_candidates": micro_option_candidates_path,
        "paper_option_ticket_preview": paper_option_ticket_preview_path,
        "option_whatif_safety_validation": option_whatif_safety_validation_path,
        "limited_auto_trading_readiness": limited_auto_trading_readiness_path,
    }.items():
        source_started = time.perf_counter()
        payload, error = _read_json_file(path)
        source_read_latencies.append(
            {
                "source": key,
                "elapsed_ms": round((time.perf_counter() - source_started) * 1000.0, 3),
                "loaded": error is None,
            }
        )
        sources[key] = {"path": str(path.expanduser().resolve()) if path else None, "loaded": error is None, "error": error}
        if error and not (
            key
            in {
                "market_data_risk_gate",
                "market_data_session_overlay",
                "market_data_operating_auto_gate",
                "capital_margin_state",
                "capital_aware_action_gate",
                "live_read_only_capital_validation",
                "capital_pipeline_orchestrator",
                "market_data_session_transition_gate",
                "session_transition_release_governor",
                "market_data_operating_execution_intent_ledger",
                "operating_defensive_action_plan",
                "operating_defensive_intent_ledger",
                "evidence_freshness",
                "evidence_refresh_watchdog",
                "source_ingest_health",
                "source_online_supervisor",
                "live_scorer_learning",
                "live_learning_llm_notes",
                "threshold_candidate_evaluator",
                "m232_hot_cache",
                "execution_safety_cache_supervisor",
                "options_market_structure",
                "options_market_structure_supervisor",
                "single_stock_option_watchlist",
                "strategy_allocation_engine",
                "micro_option_candidates",
                "paper_option_ticket_preview",
                "option_whatif_safety_validation",
                "limited_auto_trading_readiness",
            }
            and path is None
        ):
            missing_sources.append(f"{key}:{error}")
        loaded[key] = payload
    mark_stage("read_json_sources")

    latest_news, regimes, db_error = _latest_database_rows(database_path, news_limit=news_limit, regime_limit=regime_limit)
    geopolitical_desk = _geopolitical_desk_summary(database_path)
    sources["database"] = {"path": str(database_path.expanduser().resolve()) if database_path else None, "loaded": db_error is None, "error": db_error}
    if db_error:
        missing_sources.append(f"database:{db_error}")
    mark_stage("read_database_context")

    account = _account_summary(loaded["account_snapshot"])
    paper_portfolio = _paper_portfolio_summary(loaded["paper_portfolio_state"])
    risk = _risk_summary(loaded["portfolio_risk_state"])
    plan = _plan_summary(loaded["execution_intent_ledger"])
    operating_plan = _plan_summary(loaded["market_data_operating_execution_intent_ledger"])
    mark_quality = _mark_quality_summary(loaded["mark_quality_daemon"])
    economic = _economic_summary(loaded["economic_desk"])
    market_data = _market_data_summary(loaded["market_data"])
    market_data_session_overlay = _market_data_session_overlay_summary(loaded["market_data_session_overlay"])
    market_data["temperature_diagnostics"] = _market_data_temperature_diagnostics(market_data)
    market_data["symbol_anomaly_quarantine"] = _market_data_symbol_anomaly_quarantine(market_data, market_data_session_overlay)
    market_data["temperature_session_adjustment"] = _market_temperature_session_adjustment(
        market_data,
        market_data_session_overlay,
        market_data.get("symbol_anomaly_quarantine") or {},
    )
    market_data_risk_gate = _market_data_risk_gate_summary(loaded["market_data_risk_gate"])
    market_data_operating_auto_gate = _market_data_operating_auto_gate_summary(loaded["market_data_operating_auto_gate"])
    capital_margin_state = _capital_margin_state_summary(loaded["capital_margin_state"])
    capital_aware_action_gate = _capital_aware_action_gate_summary(loaded["capital_aware_action_gate"])
    live_read_only_capital_validation = _live_read_only_capital_validation_summary(loaded["live_read_only_capital_validation"])
    capital_pipeline_orchestrator = _capital_pipeline_orchestrator_summary(loaded["capital_pipeline_orchestrator"])
    market_data_session_transition_gate = _market_data_session_transition_gate_summary(loaded["market_data_session_transition_gate"])
    session_transition_release_governor = _session_transition_release_governor_summary(loaded["session_transition_release_governor"])
    operating_defensive_action_plan = _defensive_action_plan_summary(loaded["operating_defensive_action_plan"])
    operating_defensive_intent_ledger = _defensive_intent_ledger_summary(loaded["operating_defensive_intent_ledger"])
    evidence_freshness = _evidence_freshness_summary(loaded["evidence_freshness"])
    evidence_auto_recovery = _evidence_auto_recovery_summary(loaded["evidence_refresh_watchdog"])
    source_ingest_health = _source_ingest_health_summary(loaded["source_ingest_health"])
    source_online_supervisor = _source_online_supervisor_summary(loaded["source_online_supervisor"])
    m236_news_learning_payload, _m236_news_learning_error = _read_json_file(Path("exports/m236_news_learning_report.json"))
    m236_news_learning = _m236_news_learning_summary(m236_news_learning_payload)
    macro_release_payload, _macro_release_error = _read_json_file(Path("exports/m237_macro_release_interpreter.json"))
    macro_calendar_payload, _macro_calendar_error = _read_json_file(Path("exports/m237_macro_event_calendar.json"))
    macro_release_interpreter = _macro_release_summary(macro_release_payload, macro_calendar_payload)
    live_scorer_learning = _live_scorer_learning_summary(loaded["live_scorer_learning"])
    live_learning_llm_notes = _live_learning_llm_notes_summary(loaded["live_learning_llm_notes"])
    threshold_candidate_evaluator = _threshold_candidate_evaluator_summary(loaded["threshold_candidate_evaluator"])
    m232_hot_cache = _m232_hot_cache_summary(loaded["m232_hot_cache"])
    learning_pipeline = _learning_pipeline_health(
        live_scorer_learning,
        live_learning_llm_notes,
        threshold_candidate_evaluator,
        m232_hot_cache,
    )
    singleton_locks = _singleton_lock_dashboard_status()
    execution_safety_cache = _execution_safety_cache_summary(loaded["execution_safety_cache_supervisor"])
    options_market_structure = _options_market_structure_summary(loaded["options_market_structure"])
    options_market_structure_supervisor = _options_market_structure_supervisor_summary(loaded["options_market_structure_supervisor"])
    single_stock_option_watchlist = _single_stock_option_watchlist_summary(loaded["single_stock_option_watchlist"])
    strategy_allocation_engine = _strategy_allocation_engine_summary(loaded["strategy_allocation_engine"])
    micro_option_candidates = _micro_option_candidate_selector_summary(loaded["micro_option_candidates"])
    paper_option_ticket_preview = _paper_option_ticket_preview_summary(loaded["paper_option_ticket_preview"])
    option_whatif_safety_validation = _option_whatif_safety_validation_summary(loaded["option_whatif_safety_validation"])
    limited_auto_trading_readiness = _limited_auto_trading_readiness_summary(loaded["limited_auto_trading_readiness"])
    runtime_version_guard = _runtime_version_guard(options_market_structure_supervisor=options_market_structure_supervisor)
    mark_stage("summarize_sources")
    m220_market_move_option_overlay = _m220_market_move_option_structure_overlay(
        market_data=market_data,
        options_market_structure=options_market_structure,
        options_market_structure_supervisor=options_market_structure_supervisor,
    )
    options_desk = _options_desk_summary(
        account=account,
        paper_portfolio=paper_portfolio,
        plan=plan,
        market_data=market_data,
        market_data_session_overlay=market_data_session_overlay,
        market_data_risk_gate=market_data_risk_gate,
        market_data_operating_auto_gate=market_data_operating_auto_gate,
        operating_defensive_action_plan=operating_defensive_action_plan,
        operating_defensive_intent_ledger=operating_defensive_intent_ledger,
        execution_safety_cache=execution_safety_cache,
    )
    status = _status_lamp(
        risk=risk,
        mark_quality=mark_quality,
        account=account,
        market_data=market_data,
        market_data_risk_gate=market_data_risk_gate,
        market_data_operating_auto_gate=market_data_operating_auto_gate,
        session_transition_release_governor=session_transition_release_governor,
        evidence_freshness=evidence_freshness,
        evidence_auto_recovery=evidence_auto_recovery,
        source_ingest_health=source_ingest_health,
        source_online_supervisor=source_online_supervisor,
    )
    status["trade_blocking_summary"] = _status_trade_blocking_summary(
        status,
        market_data.get("temperature_session_adjustment") or {},
    )
    status["operational_readiness_overlay"] = _status_operational_readiness_overlay(status)
    raw_status_color = status.get("color")
    status["raw_color"] = raw_status_color
    status["display_color"] = (status["operational_readiness_overlay"] or {}).get("display_color") or raw_status_color
    status["alert_color"] = (status["operational_readiness_overlay"] or {}).get("alert_color") or status.get("display_color") or raw_status_color
    status["color"] = status.get("display_color") or raw_status_color
    status["display_label"] = (status["operational_readiness_overlay"] or {}).get("display_label")
    status["natural_language_ko"] = _status_natural_language_ko(status)
    mark_stage("build_gates_and_status")
    opinion = _ai_opinion(status, risk=risk, mark_quality=mark_quality, news=latest_news, regimes=regimes)
    snapshot = {
        "dashboard_snapshot_version": TRADING_DASHBOARD_SNAPSHOT_VERSION,
        "dashboard_schema_marker_version": TRADING_DASHBOARD_SCHEMA_MARKER_VERSION,
        "generated_at": _iso_now(),
        "mode": mode,
        "status": status,
        "ai_opinion": opinion,
        "menus": _menus(),
        "account": account,
        "positions": {
            "account_positions": account.get("positions") or [],
            "paper_positions": paper_portfolio.get("positions") or [],
            "paper_totals": paper_portfolio.get("totals") or {},
        },
        "options_desk": options_desk,
        "options_market_structure": options_market_structure,
        "options_market_structure_supervisor": options_market_structure_supervisor,
        "single_stock_option_watchlist": single_stock_option_watchlist,
        "m220_market_move_option_structure_overlay": m220_market_move_option_overlay,
        "capital_margin_state": capital_margin_state,
        "capital_aware_action_gate": capital_aware_action_gate,
        "live_read_only_capital_validation": live_read_only_capital_validation,
        "capital_pipeline_orchestrator": capital_pipeline_orchestrator,
        "strategy_allocation_engine": strategy_allocation_engine,
        "micro_option_candidates": micro_option_candidates,
        "paper_option_ticket_preview": paper_option_ticket_preview,
        "option_whatif_safety_validation": option_whatif_safety_validation,
        "limited_auto_trading_readiness": limited_auto_trading_readiness,
        "risk": risk,
        "buy_sell_plan": plan,
        "news_ticker": latest_news,
        "geopolitical_desk": geopolitical_desk,
        "standing_regimes": regimes,
        "market_data": {
            "mark_refresh_quality": mark_quality,
            "market_data_desk": market_data,
            "market_data_session_overlay": market_data_session_overlay,
            "market_data_risk_gate": market_data_risk_gate,
            "market_data_operating_auto_gate": market_data_operating_auto_gate,
            "capital_margin_state": capital_margin_state,
            "capital_aware_action_gate": capital_aware_action_gate,
            "live_read_only_capital_validation": live_read_only_capital_validation,
            "capital_pipeline_orchestrator": capital_pipeline_orchestrator,
            "strategy_allocation_engine": strategy_allocation_engine,
            "micro_option_candidates": micro_option_candidates,
            "paper_option_ticket_preview": paper_option_ticket_preview,
            "option_whatif_safety_validation": option_whatif_safety_validation,
            "limited_auto_trading_readiness": limited_auto_trading_readiness,
            "market_data_session_transition_gate": market_data_session_transition_gate,
            "session_transition_release_governor": session_transition_release_governor,
            "market_data_operating_execution_intent_ledger": operating_plan,
            "operating_defensive_action_plan": operating_defensive_action_plan,
            "operating_defensive_intent_ledger": operating_defensive_intent_ledger,
        },
        "macro_economic": economic,
        "evidence_freshness": evidence_freshness,
        "evidence_auto_recovery": evidence_auto_recovery,
        "source_ingest_health": source_ingest_health,
        "source_online_supervisor": source_online_supervisor,
        "m236_news_learning": m236_news_learning,
        "macro_release_interpreter": macro_release_interpreter,
        "learning_pipeline": learning_pipeline,
        "live_scorer_learning": live_scorer_learning,
        "live_learning_llm_notes": live_learning_llm_notes,
        "threshold_candidate_evaluator": threshold_candidate_evaluator,
        "m232_hot_cache": m232_hot_cache,
        "singleton_locks": singleton_locks,
        "runtime_version_guard": runtime_version_guard,
        "execution_safety_cache_supervisor": execution_safety_cache,
        "sources": sources,
        "missing_sources": missing_sources,
        "safety": {
            "paper_only_dashboard": True,
            "read_only_snapshot_builder": True,
            "ibkr_order_submitted": False,
            "transmit_enabled": False,
            "live_order_routing_enabled": False,
        },
    }
    snapshot["schema_integrity"] = _dashboard_schema_integrity(snapshot)
    mark_stage("assemble_snapshot")
    try:
        from recorder.dashboard_action_queue import build_dashboard_action_queue, dashboard_action_queue_summary

        queue = build_dashboard_action_queue(snapshot)
        snapshot["action_queue"] = dashboard_action_queue_summary(queue)
        snapshot["action_queue"]["full_queue"] = queue
    except Exception as exc:  # pragma: no cover - dashboard should survive queue rendering failures
        snapshot["action_queue"] = {
            "version": "dashboard-action-queue-v3",
            "status_color": status.get("color"),
            "total_action_count": 0,
            "visible_action_count": 0,
            "highest_severity": "unknown",
            "highest_priority_score": 0,
            "error": f"{exc.__class__.__name__}: {str(exc)[:500]}",
        }
    mark_stage("build_action_queue")
    snapshot["option_confidence_recovery_plan"] = _option_confidence_recovery_plan(snapshot)
    snapshot["operational_runbook"] = _dashboard_operational_runbook(snapshot)
    mark_stage("build_recovery_and_runbook")
    snapshot["safety_gate_latency_budget"] = _safety_gate_latency_budget(latency_stages, source_read_latencies)
    snapshot["dashboard_status_timeline"] = _dashboard_status_timeline()
    return snapshot




def write_trading_dashboard_snapshot_json(output_path: Path, snapshot: dict[str, Any]) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False), encoding="utf-8")
    return output_path


def write_trading_dashboard_markdown(output_path: Path, snapshot: dict[str, Any]) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(trading_dashboard_markdown(snapshot), encoding="utf-8")
    return output_path


def _local_time_text(value: Any, default: str = "-") -> str:
    parsed = _parse_dt(value)
    if parsed is None:
        return str(value or default)
    return parsed.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _option_legs_brief(legs: list[dict[str, Any]] | None) -> str:
    parts = []
    for leg in legs or []:
        side = leg.get("side") or _option_side(leg.get("quantity"))
        qty = leg.get("quantity") if leg.get("quantity") is not None else leg.get("ratio")
        right = leg.get("right") or "?"
        strike = leg.get("strike") if leg.get("strike") is not None else "?"
        expiry = leg.get("expiry") or "-"
        symbol = leg.get("symbol") or "-"
        risk = leg.get("position_risk_level")
        dte = leg.get("dte")
        flags = []
        if risk:
            flags.append(f"risk={risk}")
        if dte is not None:
            flags.append(f"dte={dte}")
        if leg.get("in_the_money") is not None:
            flags.append(f"itm={leg.get('in_the_money')}")
        suffix = f" ({', '.join(flags)})" if flags else ""
        parts.append(f"{side} {qty if qty is not None else ''} {symbol} {expiry} {right}{strike}{suffix}".strip())
    return "; ".join(parts) or "-"


def _option_risk_brief(risk: dict[str, Any] | None) -> str:
    risk = risk or {}
    fields = []
    for key in ("net_debit", "net_credit", "max_loss", "max_profit", "reward_to_risk", "break_even"):
        value = risk.get(key)
        if value not in (None, ""):
            fields.append(f"{key}={value}")
    return ", ".join(fields) or "-"
















def _dashboard_html_options_rows(
    snapshot: dict[str, Any],
    positions: dict[str, Any],
    options_desk: dict[str, Any],
    options_market_structure: dict[str, Any],
    options_market_structure_supervisor: dict[str, Any],
    m220_market_move_option_overlay: dict[str, Any],
) -> dict[str, Any]:
    paper_position_rows = [
        [
            row.get("symbol"),
            row.get("status"),
            row.get("open_net_quantity"),
            row.get("latest_mark_price"),
            row.get("latest_mark_source"),
            row.get("open_market_value"),
            row.get("unrealized_pnl"),
        ]
        for row in positions.get("paper_positions") or []
    ]
    account_position_rows = [
        [
            row.get("symbol"),
            row.get("sec_type"),
            row.get("currency"),
            row.get("position"),
            row.get("avg_cost"),
            row.get("expiry"),
            row.get("right"),
            row.get("strike"),
        ]
        for row in positions.get("account_positions") or []
    ]
    option_position_rows = [
        [
            row.get("contract_label") or _option_contract_label(row),
            row.get("position_risk_level"),
            row.get("side") or _option_side(row.get("position")),
            row.get("position"),
            row.get("dte"),
            row.get("in_the_money"),
            row.get("underlying_price"),
            row.get("underlying_price_source"),
            row.get("expiry"),
            row.get("right"),
            row.get("strike"),
            ",".join(row.get("position_risk_alerts") or []) or "-",
        ]
        for row in options_desk.get("account_option_positions") or []
    ]
    option_readiness = options_desk.get("readiness") or {}
    option_quote_gate = options_desk.get("quote_gate") or {}
    option_position_risk = options_desk.get("position_risk_summary") or {}
    option_readiness_rows = [
        [row.get("name"), row.get("passed"), row.get("impact"), row.get("value"), row.get("reason")]
        for row in option_readiness.get("checks") or []
    ]
    option_group_rows = [
        [
            row.get("strategy_id"),
            row.get("risk_level"),
            row.get("structure"),
            row.get("leg_count"),
            row.get("min_dte"),
            row.get("short_itm_count"),
            row.get("net_contracts"),
            ",".join(row.get("risk_alerts") or []) or "-",
            _option_legs_brief(row.get("legs")),
        ]
        for row in options_desk.get("position_strategy_groups") or []
    ]
    option_plan_rows = [
        [
            row.get("symbol"),
            row.get("action"),
            row.get("quantity"),
            row.get("state"),
            row.get("ticket_type"),
            _option_legs_brief(_extract_option_legs(row, fallback_symbol=row.get("symbol"))),
        ]
        for row in options_desk.get("option_plan_intents") or []
    ]
    advanced_option_rows = []
    for row in options_desk.get("advanced_strategies") or []:
        ticket_state = row.get("ticketing_allowed")
        if ticket_state is None:
            ticket_state = row.get("state")
        reason = row.get("reason") or ",".join(row.get("block_reasons") or []) or "-"
        advanced_option_rows.append(
            [
                row.get("source"),
                row.get("symbol"),
                row.get("strategy"),
                row.get("leg_count"),
                ticket_state,
                f"chain={row.get('requires_option_chain', '-')}; what_if={row.get('requires_margin_whatif', '-')}",
                _option_risk_brief(row.get("risk")),
                reason,
            ]
        )
    protective_option_rows = [
        [
            row.get("symbol"),
            row.get("source_action"),
            row.get("strategy"),
            row.get("analysis_status"),
            row.get("ticketing_allowed"),
            row.get("leg_count"),
            _option_legs_brief(row.get("legs")),
            _option_risk_brief(row.get("risk")),
            row.get("reason"),
        ]
        for row in options_desk.get("protective_candidates") or []
    ]
    single_stock_watchlist_rows = []
    for row in (snapshot.get("single_stock_option_watchlist") or {}).get("rows") or []:
        outcomes = f"C{row.get('outcome_completed') or 0}/P{row.get('outcome_pending') or 0}/B{row.get('outcome_blocked') or 0}"
        single_stock_watchlist_rows.append(
            [
                row.get("symbol"),
                row.get("tier"),
                row.get("score"),
                row.get("ttl_minutes"),
                row.get("trade_permission") or "-",
                row.get("quote_quality_score"),
                row.get("oi_proxy_confidence"),
                row.get("gamma_regime") or "-",
                outcomes,
                row.get("promotion_status") or "-",
                ",".join(row.get("themes") or []) or "-",
            ]
        )
    options_market_structure_rows = []
    options_market_structure_bucket_rows = []
    for row in options_market_structure.get("symbols") or []:
        options_market_structure_rows.append(
            [
                row.get("symbol"),
                row.get("spot"),
                row.get("gamma_regime"),
                row.get("total_gamma_exposure"),
                row.get("gamma_flip"),
                row.get("call_wall"),
                row.get("put_wall"),
                row.get("max_pain"),
                row.get("distance_to_pin_pct"),
                ",".join(row.get("proximity_alerts") or []) or "-",
                row.get("confidence"),
                row.get("confidence_score"),
                ",".join(row.get("warnings") or []) or "-",
            ]
        )
        for bucket in row.get("expiry_buckets") or []:
            dte_range = "-"
            if bucket.get("min_dte") is not None or bucket.get("max_dte") is not None:
                dte_range = f"{bucket.get('min_dte')}-{bucket.get('max_dte')}"
            options_market_structure_bucket_rows.append(
                [
                    row.get("symbol"),
                    bucket.get("bucket"),
                    bucket.get("quote_count"),
                    dte_range,
                    bucket.get("gamma_regime"),
                    bucket.get("total_gamma_exposure"),
                    bucket.get("call_wall"),
                    bucket.get("put_wall"),
                    bucket.get("max_pain"),
                    bucket.get("pin_risk"),
                    ",".join(bucket.get("proximity_alerts") or []) or "-",
                ]
            )
    m220_move_overlay_rows = []
    for row in m220_market_move_option_overlay.get("rows") or []:
        walls = (
            f"call={row.get('call_wall') if row.get('call_wall') is not None else '-'}; "
            f"put={row.get('put_wall') if row.get('put_wall') is not None else '-'}; "
            f"pain={row.get('max_pain') if row.get('max_pain') is not None else '-'}"
        )
        move = f"{row.get('move_direction') or '-'} {row.get('move_magnitude_pct')}%"
        if row.get("gap_pct") is not None:
            move += f"; gap={row.get('gap_pct')}%"
        if row.get("tick_move_pct") is not None:
            move += f"; tick={row.get('tick_move_pct')}%"
        m220_move_overlay_rows.append(
            [
                row.get("market_key"),
                row.get("family_label") or row.get("family_id"),
                move,
                row.get("matched_option_symbol") or "-",
                row.get("selected_source_type") or "-",
                row.get("gamma_regime") or "-",
                row.get("explanation_status") or "-",
                walls,
                ",".join(row.get("proximity_alerts") or []) or "-",
                row.get("interpretation") or "-",
            ]
        )
    index_option_coverage_rows = []
    for family in (options_market_structure_supervisor.get("index_option_coverage_matrix") or {}).get("families") or []:
        direct = family.get("direct") or {}
        mini = family.get("mini") or {}
        proxy = family.get("proxy") or {}
        index_option_coverage_rows.append(
            [
                family.get("label") or family.get("family_id"),
                f"{direct.get('status')}: {direct.get('ok_symbols') or direct.get('symbols') or []}",
                f"{mini.get('status')}: {mini.get('ok_symbols') or mini.get('symbols') or []}",
                f"{proxy.get('status')}: {proxy.get('ok_symbols') or proxy.get('symbols') or []}",
                f"{family.get('selected_source_type')}: {family.get('selected_symbols') or []}",
                family.get("family_quote_budget"),
                family.get("coverage_status"),
                family.get("confidence"),
            ]
        )
    family_repair_rows = [
        [
            row.get("label") or row.get("family_id"),
            row.get("operating_mode"),
            row.get("severity"),
            f"{row.get('selected_source_type')}: {row.get('selected_symbols') or []}",
            row.get("issue_category_counts") or {},
            row.get("next_action"),
            row.get("operator_summary"),
        ]
        for row in options_market_structure_supervisor.get("m220_family_repair_plan") or []
    ]
    family_usage_rows = [
        [
            row.get("label") or row.get("family_id"),
            row.get("effective_operating_status"),
            row.get("scorer_use_strength"),
            f"{row.get('selected_source_type')}: {row.get('selected_symbols') or []}",
            row.get("direct_failure_non_blocking"),
            row.get("severity"),
            row.get("operator_summary"),
        ]
        for row in (options_market_structure_supervisor.get("m220_family_issue_summary") or {}).get("effective_family_usage_rows") or []
    ]
    m220_validation_rows = [
        [
            row.get("family") or row.get("family_id"),
            row.get("role"),
            row.get("symbols") or [],
            row.get("passed"),
            row.get("status"),
            row.get("issue_categories") or [],
            row.get("next_action"),
        ]
        for row in options_market_structure_supervisor.get("m220_live_validation_checklist") or []
    ]
    budget_tuning_rows = [
        [
            row.get("label") or row.get("family_id"),
            row.get("allocated_budget"),
            f"{row.get('selected_source_type')}: {row.get('selected_symbols') or []}",
            f"{int(row.get('usable_role_symbol_count') or 0)} / {int(row.get('requested_role_symbol_count') or 0)}",
            f"{int(row.get('candidate_contracts') or 0)} / {int(row.get('chain_rows') or 0)}",
            row.get("not_requested_symbols") or [],
            row.get("suggested_budget_bias"),
        ]
        for row in options_market_structure_supervisor.get("index_family_budget_tuning_observations") or []
    ]
    option_quote_quality_rows = []
    for symbol, row in sorted((options_market_structure_supervisor.get("symbol_status") or {}).items()):
        if not isinstance(row, dict):
            continue
        option_quote_quality_rows.append(
            [
                symbol,
                row.get("scope"),
                row.get("last_status"),
                row.get("last_quality_status"),
                f"{row.get('last_quoted') if row.get('last_quoted') is not None else '-'} / {row.get('last_requested') if row.get('last_requested') is not None else '-'}",
                row.get("last_quoted_rate"),
                row.get("last_avg_spread_pct"),
                row.get("last_data_latency_status"),
                row.get("failure_streak"),
                row.get("isolation_until") or "-",
                row.get("last_reason") or "-",
            ]
        )
    return {
        "paper_position_rows": paper_position_rows,
        "account_position_rows": account_position_rows,
        "option_position_rows": option_position_rows,
        "option_readiness": option_readiness,
        "option_quote_gate": option_quote_gate,
        "option_position_risk": option_position_risk,
        "option_readiness_rows": option_readiness_rows,
        "option_group_rows": option_group_rows,
        "option_plan_rows": option_plan_rows,
        "advanced_option_rows": advanced_option_rows,
        "protective_option_rows": protective_option_rows,
        "options_market_structure_rows": options_market_structure_rows,
        "options_market_structure_bucket_rows": options_market_structure_bucket_rows,
        "single_stock_watchlist_rows": single_stock_watchlist_rows,
        "m220_move_overlay_rows": m220_move_overlay_rows,
        "index_option_coverage_rows": index_option_coverage_rows,
        "family_repair_rows": family_repair_rows,
        "family_usage_rows": family_usage_rows,
        "m220_validation_rows": m220_validation_rows,
        "budget_tuning_rows": budget_tuning_rows,
        "option_quote_quality_rows": option_quote_quality_rows,
    }


def _dashboard_html_status_context(ctx: dict[str, Any]) -> dict[str, Any]:
    status = ctx['status']
    dashboard_status_timeline = ctx['dashboard_status_timeline']
    market_temperature_adjustment = ctx['market_temperature_adjustment']
    market_temperature_diagnostics = ctx['market_temperature_diagnostics']
    market_symbol_quarantine = ctx['market_symbol_quarantine']
    option_confidence_recovery = ctx['option_confidence_recovery']
    safety_latency = ctx['safety_latency']
    status_category_rows = []
    for item in status.get("reason_categories") or []:
        status_category_rows.append([item.get("label") or item.get("category"), item.get("count"), item.get("reasons") or []])
    status_timeline_rows = []
    for item in dashboard_status_timeline.get("transitions") or []:
        status_timeline_rows.append(
            [
                _local_time_text(item.get("at")) if item.get("at") else "-",
                item.get("from_status"),
                item.get("to_status"),
                item.get("changed_fields") or [],
                item.get("to_reasons") or [],
            ]
        )
    threshold_rows = []
    for item in ((market_temperature_adjustment.get("thresholds") or {}).get("table") or []):
        threshold_rows.append([item.get("metric"), item.get("regular"), item.get("non_regular"), item.get("unit")])
    market_temperature_component_rows = []
    for item in market_temperature_diagnostics.get("component_rows") or []:
        market_temperature_component_rows.append([item.get("component"), item.get("status"), item.get("score"), item.get("counts") or {}])
    market_temperature_contributor_rows = []
    for item in (market_temperature_diagnostics.get("top_contributors") or [])[:10]:
        market_temperature_contributor_rows.append(
            [
                item.get("key"),
                item.get("group"),
                item.get("status"),
                item.get("gap_pct"),
                item.get("tick_move_pct"),
                item.get("quote_spread_pct"),
                item.get("alerts") or [],
            ]
        )
    market_symbol_quarantine_rows = []
    for item in market_symbol_quarantine.get("candidates") or []:
        market_symbol_quarantine_rows.append(
            [
                item.get("key"),
                item.get("group"),
                item.get("severity"),
                item.get("lifecycle_status"),
                item.get("quote_spread_pct"),
                item.get("gap_pct"),
                item.get("reasons") or [],
            ]
        )
    option_confidence_recovery_rows = []
    for item in option_confidence_recovery.get("steps") or []:
        option_confidence_recovery_rows.append([item.get("step"), item.get("why"), item.get("command_hint")])
    revival_review_rows = []
    for item in option_confidence_recovery.get("revival_review_queue") or []:
        revival_review_rows.append(
            [
                item.get("candidate_key"),
                item.get("queue_status"),
                item.get("lifecycle_status"),
                item.get("new_sample_count"),
                item.get("root_causes") or [],
                item.get("required_checks") or [],
            ]
        )
    latency_rows = []
    for item in safety_latency.get("top_stages") or []:
        latency_rows.append([item.get("stage"), item.get("elapsed_ms")])
    latency_source_rows = []
    for item in safety_latency.get("slowest_sources") or []:
        latency_source_rows.append([item.get("source"), item.get("elapsed_ms"), item.get("loaded")])

    return {
        "status_category_rows": status_category_rows,
        "status_timeline_rows": status_timeline_rows,
        "threshold_rows": threshold_rows,
        "market_temperature_component_rows": market_temperature_component_rows,
        "market_temperature_contributor_rows": market_temperature_contributor_rows,
        "market_symbol_quarantine_rows": market_symbol_quarantine_rows,
        "option_confidence_recovery_rows": option_confidence_recovery_rows,
        "revival_review_rows": revival_review_rows,
        "latency_rows": latency_rows,
        "latency_source_rows": latency_source_rows,
    }

def _dashboard_html_learning_ops_context(ctx: dict[str, Any]) -> dict[str, Any]:
    promotion_gate = ctx['promotion_gate']
    singleton_locks = ctx['singleton_locks']
    operational_runbook = ctx['operational_runbook']
    promotion_gate_rows = []
    for row in (promotion_gate.get("watchlist_candidates") or [])[:5]:
        history = row.get("history_state") or {}
        conditional = row.get("conditional_promotion") or {}
        requirements = row.get("promotion_requirements") or []
        promotion_gate_rows.append(
            [
                row.get("gate_status"),
                row.get("operational_gate_status") or ((row.get("stale_watchlist_policy") or {}).get("operational_gate_status")),
                row.get("semantic_family_key"),
                row.get("detail_reasons") or {},
                requirements[0].get("requirement") if requirements else "-",
                history.get("status_age_review_count"),
                history.get("watchlist_stale"),
                conditional.get("mode"),
            ]
        )
    recycle_state_rows = []
    recycle_learning = promotion_gate.get("recycle_learning") or {}
    scorer_policies = {
        str(policy.get("candidate_key") or ""): policy
        for policy in ((promotion_gate.get("scorer_threshold_policy_summary") or {}).get("policies") or [])
        if isinstance(policy, dict)
    }
    for state in (recycle_learning.get("active_states") or [])[:8]:
        key = str(state.get("candidate_key") or "")
        policy = scorer_policies.get(key) or {}
        recycle_state_rows.append(
            [
                key,
                state.get("status"),
                state.get("lifecycle_status"),
                f"{state.get('new_sample_count')}/{state.get('target_new_sample_count')}",
                f"{state.get('consecutive_option_confidence_ticks')}/{state.get('target_confidence_ticks')}",
                state.get("quarantine_remaining_ticks"),
                state.get("root_causes") or [],
                policy.get("scorer_action") or "-",
                policy.get("confidence_multiplier") if policy.get("confidence_multiplier") is not None else "-",
            ]
        )
    recycle_graduation = promotion_gate.get("recycle_graduation_report") or {}
    recycle_graduation_rows = []
    for row in (recycle_graduation.get("dormant_or_retired_candidates") or [])[:8]:
        recycle_graduation_rows.append(
            [
                row.get("candidate_key"),
                row.get("lifecycle_status"),
                row.get("operational_gate_status"),
                row.get("new_sample_count"),
                row.get("consecutive_option_confidence_ticks"),
                row.get("blocked_by") or [],
                row.get("scorer_action") or "-",
            ]
        )
    for row in (recycle_graduation.get("near_graduation_candidates") or [])[:5]:
        recycle_graduation_rows.append(
            [
                row.get("candidate_key"),
                "near_graduation",
                row.get("operational_gate_status"),
                f"{row.get('new_sample_count')}/{row.get('target_new_sample_count')}",
                f"{row.get('consecutive_option_confidence_ticks')}/{row.get('target_confidence_ticks')}",
                row.get("blocked_by") or [],
                row.get("recommendation") or "-",
            ]
        )
    truth_summary = singleton_locks.get("truth_summary") or {}
    singleton_truth_rows = [
        ["process_online_count", truth_summary.get("process_online_count")],
        ["lock_file_count", truth_summary.get("lock_file_count")],
        ["stale_lock_count", truth_summary.get("stale_lock_count")],
        ["missing_lock_count", truth_summary.get("missing_lock_count")],
        ["artifact_fresh_count", truth_summary.get("artifact_fresh_count")],
        ["artifact_stale_count", truth_summary.get("artifact_stale_count")],
        ["fresh_artifact_without_process_count", truth_summary.get("fresh_artifact_without_process_count")],
        ["fresh_artifact_without_process_services", truth_summary.get("fresh_artifact_without_process_services") or []],
    ]
    singleton_rows = []
    for row in singleton_locks.get("locks") or []:
        singleton_rows.append(
            [
                row.get("name"),
                row.get("status"),
                row.get("writer_pid") or row.get("pid"),
                row.get("age_seconds"),
                row.get("stale_by_age"),
                row.get("artifact_age_seconds"),
                row.get("artifact_stale"),
                row.get("cwd"),
            ]
        )
    singleton_service_group_rows = []
    for row in singleton_locks.get("service_groups") or []:
        singleton_service_group_rows.append(
            [
                row.get("service"),
                row.get("group_status"),
                row.get("lock_holder_writer_pid"),
                row.get("pid_running"),
                row.get("artifact_age_seconds"),
                row.get("artifact_stale"),
                row.get("process_count_interpretation"),
            ]
        )
    operational_runbook_rows = []
    for item in operational_runbook.get("immediate_checks") or []:
        operational_runbook_rows.append(["check", item])
    for item in operational_runbook.get("success_criteria") or []:
        operational_runbook_rows.append(["success", item])
    operational_runbook_command_rows = []
    for item in (operational_runbook.get("verification_commands") or [])[:4]:
        operational_runbook_command_rows.append(["verify", item])
    for item in (operational_runbook.get("start_commands") or [])[:4]:
        operational_runbook_command_rows.append(["start", item])

    return {
        "promotion_gate_rows": promotion_gate_rows,
        "recycle_state_rows": recycle_state_rows,
        "recycle_graduation": recycle_graduation,
        "recycle_graduation_rows": recycle_graduation_rows,
        "singleton_truth_rows": singleton_truth_rows,
        "singleton_rows": singleton_rows,
        "singleton_service_group_rows": singleton_service_group_rows,
        "operational_runbook_rows": operational_runbook_rows,
        "operational_runbook_command_rows": operational_runbook_command_rows,
    }

def _dashboard_html_options_context(ctx: dict[str, Any]) -> dict[str, Any]:
    snapshot = ctx['snapshot']
    positions = ctx['positions']
    options_desk = ctx['options_desk']
    options_market_structure = ctx['options_market_structure']
    options_market_structure_supervisor = ctx['options_market_structure_supervisor']
    m220_market_move_option_overlay = ctx['m220_market_move_option_overlay']
    options_rows = _dashboard_html_options_rows(
        snapshot,
        positions,
        options_desk,
        options_market_structure,
        options_market_structure_supervisor,
        m220_market_move_option_overlay,
    )
    paper_position_rows = options_rows["paper_position_rows"]
    account_position_rows = options_rows["account_position_rows"]
    option_position_rows = options_rows["option_position_rows"]
    option_readiness = options_rows["option_readiness"]
    option_quote_gate = options_rows["option_quote_gate"]
    option_position_risk = options_rows["option_position_risk"]
    option_readiness_rows = options_rows["option_readiness_rows"]
    option_group_rows = options_rows["option_group_rows"]
    option_plan_rows = options_rows["option_plan_rows"]
    advanced_option_rows = options_rows["advanced_option_rows"]
    protective_option_rows = options_rows["protective_option_rows"]
    options_market_structure_rows = options_rows["options_market_structure_rows"]
    options_market_structure_bucket_rows = options_rows["options_market_structure_bucket_rows"]
    single_stock_watchlist_rows = options_rows["single_stock_watchlist_rows"]
    m220_move_overlay_rows = options_rows["m220_move_overlay_rows"]
    index_option_coverage_rows = options_rows["index_option_coverage_rows"]
    family_repair_rows = options_rows["family_repair_rows"]
    family_usage_rows = options_rows["family_usage_rows"]
    m220_validation_rows = options_rows["m220_validation_rows"]
    budget_tuning_rows = options_rows["budget_tuning_rows"]
    option_quote_quality_rows = options_rows["option_quote_quality_rows"]
    return {
        "options_rows": options_rows,
        "paper_position_rows": paper_position_rows,
        "account_position_rows": account_position_rows,
        "option_position_rows": option_position_rows,
        "option_readiness": option_readiness,
        "option_quote_gate": option_quote_gate,
        "option_position_risk": option_position_risk,
        "option_readiness_rows": option_readiness_rows,
        "option_group_rows": option_group_rows,
        "option_plan_rows": option_plan_rows,
        "advanced_option_rows": advanced_option_rows,
        "protective_option_rows": protective_option_rows,
        "options_market_structure_rows": options_market_structure_rows,
        "options_market_structure_bucket_rows": options_market_structure_bucket_rows,
        "single_stock_watchlist_rows": single_stock_watchlist_rows,
        "m220_move_overlay_rows": m220_move_overlay_rows,
        "index_option_coverage_rows": index_option_coverage_rows,
        "family_repair_rows": family_repair_rows,
        "family_usage_rows": family_usage_rows,
        "m220_validation_rows": m220_validation_rows,
        "budget_tuning_rows": budget_tuning_rows,
        "option_quote_quality_rows": option_quote_quality_rows,
    }

def _dashboard_html_plan_rows_context(ctx: dict[str, Any]) -> dict[str, Any]:
    plan = ctx['plan']
    market_data_operating_auto_gate = ctx['market_data_operating_auto_gate']
    capital_aware_action_gate = ctx['capital_aware_action_gate']
    live_read_only_capital_validation = ctx['live_read_only_capital_validation']
    capital_pipeline_orchestrator = ctx['capital_pipeline_orchestrator']
    strategy_allocation_engine = ctx['strategy_allocation_engine']
    micro_option_candidates = ctx.get('micro_option_candidates') or {}
    paper_option_ticket_preview = ctx.get('paper_option_ticket_preview') or {}
    option_whatif_safety_validation = ctx.get('option_whatif_safety_validation') or {}
    limited_auto_trading_readiness = ctx.get('limited_auto_trading_readiness') or {}
    operating_protective_packet = ctx['operating_protective_packet']
    operating_defensive_plan = ctx['operating_defensive_plan']
    operating_defensive_ledger = ctx['operating_defensive_ledger']
    plan_rows = []
    for row in plan.get("intents") or []:
        plan_rows.append(
            [
                row.get("symbol"),
                row.get("action"),
                row.get("quantity"),
                row.get("state"),
                row.get("ticket_type"),
                row.get("capital_gate_status") or "-",
                row.get("capital_quantity_scale") if row.get("capital_quantity_scale") is not None else "-",
                row.get("portfolio_risk_gate_applied"),
            ]
        )
    operating_gate_rows = []
    for row in market_data_operating_auto_gate.get("sample_auto_execution_items") or []:
        operating_gate_rows.append(
            [
                row.get("symbol"),
                row.get("action"),
                row.get("ticket_type"),
                row.get("status"),
                ",".join(row.get("block_reasons") or []) or "-",
                row.get("entry_size_multiplier"),
            ]
        )
    capital_gate_rows = []
    for row in capital_aware_action_gate.get("sample_auto_execution_items") or []:
        capital_gate_rows.append(
            [
                row.get("symbol"),
                row.get("action"),
                row.get("ticket_type"),
                row.get("status_display") or row.get("status"),
                row.get("block_category") or "-",
                row.get("quantity"),
                row.get("cash_required"),
                row.get("initial_margin_required"),
                ",".join(row.get("block_reasons") or []) or "-",
            ]
        )
    strategy_allocation_rows = []
    for row in strategy_allocation_engine.get("recommendations") or []:
        option_structure = row.get("option_structure") if isinstance(row.get("option_structure"), dict) else {}
        micro_policy = row.get("micro_policy") if isinstance(row.get("micro_policy"), dict) else {}
        ticker_rank = row.get("ticker_candidate_rank") if isinstance(row.get("ticker_candidate_rank"), dict) else {}
        cross_asset = row.get("cross_asset_expression") if isinstance(row.get("cross_asset_expression"), dict) else {}
        strategy_allocation_rows.append(
            [
                row.get("symbol"),
                row.get("candidate_source") or "-",
                row.get("direction"),
                row.get("recommendation"),
                row.get("selected_expression"),
                f"{cross_asset.get('scenario') or '-'}/{cross_asset.get('expression_hint') or '-'}",
                row.get("confidence"),
                ticker_rank.get("rank") or "-",
                option_structure.get("gamma_regime") or "-",
                micro_policy.get("status") or "-",
                row.get("capital_gate_status") or "-",
                ",".join(row.get("reasons") or []) or "-",
            ]
        )
    micro_option_candidate_rows = []
    for row in micro_option_candidates.get("candidates") or []:
        micro_option_candidate_rows.append(
            [
                row.get("candidate_status"),
                row.get("score"),
                row.get("symbol"),
                row.get("strategy"),
                f"{row.get('expiration')}/{row.get('dte')}",
                row.get("strike"),
                row.get("total_ask_debit"),
                row.get("spread_pct"),
                f"{row.get('volume')}/{row.get('open_interest')}",
                ",".join(row.get("reasons") or []) or "-",
            ]
        )
    paper_option_ticket_rows = []
    for row in paper_option_ticket_preview.get("ticket_previews") or []:
        paper_option_ticket_rows.append(
            [
                row.get("ticket_id"),
                row.get("symbol"),
                row.get("strategy"),
                row.get("ticket_type"),
                row.get("leg_count"),
                row.get("limit_price"),
                row.get("estimated_total_debit"),
                row.get("estimated_max_loss"),
                row.get("max_ticket_debit"),
                row.get("transmit"),
            ]
        )
    paper_option_ticket_review_rows = []
    for row in paper_option_ticket_preview.get("candidate_reviews") or []:
        paper_option_ticket_review_rows.append(
            [
                row.get("candidate_symbol"),
                row.get("strategy"),
                row.get("preview_status"),
                row.get("score"),
                row.get("limit_price"),
                row.get("total_limit_debit"),
                ",".join(row.get("block_reasons") or []) or "-",
            ]
        )
    option_whatif_validation_rows = []
    for row in option_whatif_safety_validation.get("validation_items") or []:
        option_whatif_validation_rows.append(
            [
                row.get("ticket_id"),
                row.get("symbol"),
                row.get("strategy"),
                row.get("leg_count"),
                row.get("status"),
                row.get("final_paper_executable"),
                row.get("local_safety_passed"),
                row.get("broker_whatif_status"),
                row.get("estimated_total_debit"),
                ",".join(row.get("block_reasons") or []) or "-",
            ]
        )
    live_capital_validation_rows = []
    for row in live_read_only_capital_validation.get("sample_validation_items") or []:
        micro_policy = row.get("micro_policy") if isinstance(row.get("micro_policy"), dict) else {}
        live_capital_validation_rows.append(
            [
                row.get("symbol"),
                row.get("action"),
                row.get("ticket_type"),
                row.get("source_quantity"),
                row.get("max_feasible_quantity"),
                row.get("status"),
                row.get("execution_status"),
                micro_policy.get("status") or "-",
                row.get("binding_constraint"),
                ",".join(row.get("reasons") or []) or "-",
            ]
        )
    capital_pipeline_rows = []
    for row in capital_pipeline_orchestrator.get("artifacts") or []:
        capital_pipeline_rows.append(
            [
                row.get("artifact_id"),
                row.get("status"),
                row.get("age_seconds"),
                row.get("dependency_lag_seconds"),
                row.get("rebuild_required"),
                ",".join(row.get("reasons") or []) or "-",
            ]
        )
    limited_auto_readiness_block_rows = []
    for row in limited_auto_trading_readiness.get("hard_blocks") or []:
        limited_auto_readiness_block_rows.append(
            [
                "hard",
                row.get("code"),
                row.get("source"),
                row.get("message"),
                row.get("value"),
            ]
        )
    for row in limited_auto_trading_readiness.get("trade_now_blockers") or []:
        limited_auto_readiness_block_rows.append(
            [
                "trade_now",
                row.get("code"),
                row.get("source"),
                row.get("message"),
                row.get("value"),
            ]
        )
    for row in limited_auto_trading_readiness.get("live_eligibility_blockers") or []:
        limited_auto_readiness_block_rows.append(
            [
                "live_eligibility",
                row.get("code"),
                row.get("source"),
                row.get("message"),
                row.get("value"),
            ]
        )
    for row in limited_auto_trading_readiness.get("soft_warnings") or []:
        limited_auto_readiness_block_rows.append(
            [
                "soft",
                row.get("code"),
                row.get("source"),
                row.get("message"),
                row.get("value"),
            ]
        )
    gate_policy = limited_auto_trading_readiness.get("gate_severity_policy") or {}
    for row in (gate_policy.get("signals") or [])[:20]:
        limited_auto_readiness_block_rows.append(
            [
                row.get("category") or "gate_policy",
                row.get("code"),
                row.get("source"),
                f"posture={gate_policy.get('recommended_execution_posture')} size_x={row.get('size_multiplier')} confidence_x={row.get('confidence_multiplier')}",
                row.get("origin"),
            ]
        )
    limited_auto_readiness_step_rows = [["next", step] for step in (limited_auto_trading_readiness.get("next_required_steps") or [])]
    protective_candidate_rows = []
    for row in operating_protective_packet.get("sample_candidates") or []:
        protective_candidate_rows.append(
            [
                row.get("symbol"),
                row.get("source_action"),
                row.get("analysis_status"),
                row.get("ticketing_allowed"),
                row.get("reason"),
            ]
        )
    defensive_action_rows = []
    for row in operating_defensive_plan.get("top_actions") or []:
        defensive_action_rows.append(
            [
                row.get("priority_score"),
                row.get("symbol"),
                row.get("action_type"),
                row.get("strategy"),
                row.get("ticketing_allowed"),
                row.get("requires_option_chain"),
                row.get("requires_margin_whatif"),
                row.get("reason"),
            ]
        )
    defensive_intent_rows = []
    for row in operating_defensive_ledger.get("defensive_intents") or []:
        defensive_intent_rows.append(
            [
                row.get("state"),
                row.get("symbol"),
                row.get("strategy"),
                row.get("priority_score"),
                ",".join(row.get("block_reasons") or []) or "-",
                row.get("paper_action") or "-",
                row.get("quantity") if row.get("quantity") is not None else "-",
            ]
        )
    return {
        "plan_rows": plan_rows,
        "operating_gate_rows": operating_gate_rows,
        "capital_gate_rows": capital_gate_rows,
        "live_capital_validation_rows": live_capital_validation_rows,
        "capital_pipeline_rows": capital_pipeline_rows,
        "strategy_allocation_rows": strategy_allocation_rows,
        "micro_option_candidate_rows": micro_option_candidate_rows,
        "paper_option_ticket_rows": paper_option_ticket_rows,
        "paper_option_ticket_review_rows": paper_option_ticket_review_rows,
        "option_whatif_validation_rows": option_whatif_validation_rows,
        "limited_auto_readiness_block_rows": limited_auto_readiness_block_rows,
        "limited_auto_readiness_step_rows": limited_auto_readiness_step_rows,
        "protective_candidate_rows": protective_candidate_rows,
        "defensive_action_rows": defensive_action_rows,
        "defensive_intent_rows": defensive_intent_rows,
    }

def _dashboard_html_source_market_rows_context(ctx: dict[str, Any]) -> dict[str, Any]:
    snapshot = ctx['snapshot']
    evidence_freshness = ctx['evidence_freshness']
    evidence_auto_recovery = ctx['evidence_auto_recovery']
    source_ingest_health = ctx['source_ingest_health']
    source_online_supervisor = ctx['source_online_supervisor']
    market_data_desk = ctx['market_data_desk']
    market_data_session_overlay = ctx['market_data_session_overlay']
    news_rows = []
    for item in snapshot.get("news_ticker") or []:
        news_rows.append(
            [
                item.get("published_at") or item.get("provider_published_at") or item.get("received_at"),
                item.get("provider"),
                item.get("primary_symbol") or item.get("region") or "-",
                item.get("headline"),
            ]
        )
    geopolitical_heat_rows = []
    for item in (snapshot.get("geopolitical_desk") or {}).get("top_buckets") or []:
        geopolitical_heat_rows.append(
            [
                item.get("heat_color"),
                item.get("heat_score"),
                item.get("region"),
                item.get("theme"),
                item.get("signal_count"),
                item.get("urgency_counts"),
                item.get("top_assets"),
                item.get("desk_action"),
                item.get("latest_event_at"),
                (item.get("latest_titles") or ["-"])[0],
            ]
        )
    regime_rows = []
    for item in snapshot.get("standing_regimes") or []:
        regime_rows.append(
            [
                item.get("regime_name") or item.get("regime_key"),
                item.get("status"),
                item.get("family"),
                item.get("region"),
                item.get("directional_bias"),
                item.get("severity_score"),
                item.get("confidence"),
                item.get("effective_weight"),
                item.get("freshness_status"),
                item.get("age_days"),
                item.get("last_seen_at"),
            ]
        )
    evidence_rows = []
    for item in evidence_freshness.get("sources") or []:
        evidence_rows.append(
            [
                item.get("label") or item.get("source_key"),
                item.get("status"),
                item.get("age_minutes"),
                item.get("scorer_usability"),
                item.get("scorer_weight"),
                item.get("latest_event_at"),
            ]
        )
    auto_recovery_rows = []
    for item in evidence_auto_recovery.get("tick_summaries") or []:
        auto_recovery_rows.append(
            [
                item.get("tick_index"),
                item.get("status"),
                item.get("freshness_status"),
                ",".join(item.get("stale_source_keys") or []) or "-",
                ",".join(item.get("lagging_source_keys") or []) or "-",
                item.get("cooldown_remaining_seconds"),
            ]
        )
    source_doctor_rows = []
    for item in source_ingest_health.get("sources") or []:
        source_doctor_rows.append(
            [
                item.get("source_key"),
                "yes" if item.get("online_required") else "no",
                item.get("freshness_status"),
                item.get("health_status"),
                item.get("event_age_minutes"),
                item.get("root_cause"),
                item.get("recommended_action"),
            ]
        )
    source_supervisor_rows = []
    latest_supervisor_tick = source_online_supervisor.get("latest_tick") or {}
    for item in latest_supervisor_tick.get("actions") or []:
        details = item.get("details") or {}
        source_supervisor_rows.append(
            [
                item.get("action_id"),
                item.get("source_key"),
                item.get("status"),
                item.get("priority"),
                details.get("next_eligible_at_local") or _local_time_text(details.get("next_eligible_at")),
                item.get("description"),
            ]
        )
    market_data_rows = []
    for row in market_data_desk.get("rows") or []:
        gap_pct = row.get("gap_pct")
        if gap_pct is None:
            price = _num(row.get("price"))
            close = _num(row.get("close"))
            gap_pct = round((price / close - 1.0) * 100.0, 3) if price is not None and close not in (None, 0) else None
        market_data_rows.append(
            [
                row.get("key"),
                row.get("group"),
                row.get("price"),
                gap_pct,
                row.get("market_data_type_label"),
                row.get("contract_candidate_label") or "base",
                "yes" if row.get("delayed") else "no",
                ",".join(row.get("alerts") or []) or "-",
                row.get("status"),
            ]
        )
    market_data_session_rows = []
    for row in market_data_session_overlay.get("rows") or []:
        market_data_session_rows.append(
            [
                row.get("key"),
                row.get("session_bucket"),
                row.get("monitorability"),
                row.get("price"),
                "yes" if row.get("price_present") else "no",
                "yes" if row.get("delayed") else "no",
                ",".join(row.get("alerts") or []) or "-",
                row.get("status"),
            ]
        )
    source_rows = []
    for key, value in (snapshot.get("sources") or {}).items():
        source_rows.append([key, value.get("loaded"), value.get("error") or "ok", value.get("path")])
    return {
        "news_rows": news_rows,
        "geopolitical_heat_rows": geopolitical_heat_rows,
        "regime_rows": regime_rows,
        "evidence_rows": evidence_rows,
        "auto_recovery_rows": auto_recovery_rows,
        "source_doctor_rows": source_doctor_rows,
        "source_supervisor_rows": source_supervisor_rows,
        "market_data_rows": market_data_rows,
        "market_data_session_rows": market_data_session_rows,
        "source_rows": source_rows,
    }

def _dashboard_html_recovery_rows_context(ctx: dict[str, Any]) -> dict[str, Any]:
    action_queue = ctx['action_queue']
    recovery_plan = ctx['recovery_plan']
    recovery_executor = ctx['recovery_executor']
    action_queue_rows = []
    for item in action_queue.get("top_actions") or []:
        action_queue_rows.append(
            [
                item.get("priority_score"),
                item.get("severity"),
                item.get("domain"),
                item.get("action_id"),
                item.get("recommendation"),
            ]
        )
    recovery_plan_rows = []
    for item in recovery_plan.get("top_steps") or []:
        recovery_plan_rows.append(
            [
                item.get("execution_order"),
                item.get("severity"),
                item.get("phase"),
                item.get("status"),
                item.get("source_action_id"),
                "; ".join(str(command) for command in item.get("command_preview") or []),
            ]
        )
    recovery_executor_rows = []
    for item in recovery_executor.get("top_step_results") or []:
        recovery_executor_rows.append(
            [
                item.get("step_id"),
                item.get("phase"),
                item.get("status"),
                item.get("command_count"),
                item.get("executed_command_count"),
            ]
        )

    return {
        "action_queue_rows": action_queue_rows,
        "recovery_plan_rows": recovery_plan_rows,
        "recovery_executor_rows": recovery_executor_rows,
    }

def _dashboard_html_plan_source_context(ctx: dict[str, Any]) -> dict[str, Any]:
    generated: dict[str, Any] = {}
    for builder in (
        _dashboard_html_plan_rows_context,
        _dashboard_html_source_market_rows_context,
        _dashboard_html_recovery_rows_context,
    ):
        generated.update(builder(ctx))
    return generated

def _dashboard_html_chrome_context(ctx: dict[str, Any]) -> dict[str, Any]:
    snapshot = ctx['snapshot']
    status = ctx['status']
    raw_color = ctx['raw_color']
    color = ctx['color']
    status_overlay = ctx['status_overlay']
    opinion = ctx['opinion']
    snapshot_json_path = ctx.get("snapshot_json_path")
    menu_links = "\n".join(
        f'<a href="#{escape(str(item.get("id")))}">{_html_text(item.get("label"))}</a>' for item in snapshot.get("menus") or []
    )
    display_label = status.get("display_label") or status_overlay.get("display_label") or raw_color
    reasons = "".join(_html_badge(item, class_name="reason") for item in status.get("reasons") or [])
    lamp_summary = (
        _html_badge(f"display {color}", class_name="reason")
        + _html_badge(f"raw {raw_color}", class_name=f"reason raw-status raw-status-{escape(raw_color)}")
        + _html_badge(display_label, class_name="reason")
    )
    watch_items = "".join(_html_badge(item) for item in opinion.get("watch_items") or [] if item)
    focus_items = "".join(_html_badge(item) for item in opinion.get("focus") or [])
    missing_sources = snapshot.get("missing_sources") or []
    missing_html = (
        "<ul>" + "".join(f"<li>{_html_text(item)}</li>" for item in missing_sources) + "</ul>"
        if missing_sources
        else '<p class="muted">No missing source paths reported.</p>'
    )
    snapshot_json_label = _html_text(snapshot_json_path) if snapshot_json_path else "-"
    generated_at_raw = str(snapshot.get("generated_at") or "")
    generated_at_attr = escape(generated_at_raw, quote=True)
    generated_at_local = _local_time_text(generated_at_raw)
    embedded_snapshot = json.dumps(snapshot, ensure_ascii=False).replace("</", "<\\/")

    return {
        "menu_links": menu_links,
        "display_label": display_label,
        "reasons": reasons,
        "lamp_summary": lamp_summary,
        "watch_items": watch_items,
        "focus_items": focus_items,
        "missing_sources": missing_sources,
        "missing_html": missing_html,
        "snapshot_json_label": snapshot_json_label,
        "generated_at_raw": generated_at_raw,
        "generated_at_attr": generated_at_attr,
        "generated_at_local": generated_at_local,
        "embedded_snapshot": embedded_snapshot,
    }

def _dashboard_html_context(ctx: dict[str, Any]) -> dict[str, Any]:
    generated: dict[str, Any] = {}
    for builder in (
        _dashboard_html_status_context,
        _dashboard_html_learning_ops_context,
        _dashboard_html_options_context,
        _dashboard_html_plan_source_context,
        _dashboard_html_chrome_context,
    ):
        generated.update(builder(ctx))
    return {**ctx, **generated}

def trading_dashboard_html(
    snapshot: dict[str, Any],
    *,
    refresh_seconds: int = 15,
    snapshot_json_path: Path | str | None = None,
    title: str = "Trading Command Center",
) -> str:
    status = snapshot.get("status") or {}
    raw_color = str(status.get("raw_color") or status.get("color") or "unknown").lower()
    color = str(status.get("display_color") or status.get("alert_color") or status.get("color") or raw_color).lower()
    status_overlay = status.get("operational_readiness_overlay") or {}
    operational_status = status_overlay.get("operational_status") or {}
    trading_readiness = status_overlay.get("trading_readiness") or {}
    account = snapshot.get("account") or {}
    positions = snapshot.get("positions") or {}
    options_desk = snapshot.get("options_desk") or {}
    options_market_structure = snapshot.get("options_market_structure") or {}
    options_market_structure_supervisor = snapshot.get("options_market_structure_supervisor") or {}
    runtime_version_guard = snapshot.get("runtime_version_guard") or {}
    m220_market_move_option_overlay = snapshot.get("m220_market_move_option_structure_overlay") or {}
    risk = snapshot.get("risk") or {}
    plan = snapshot.get("buy_sell_plan") or {}
    mark_quality = ((snapshot.get("market_data") or {}).get("mark_refresh_quality") or {}).get("quality") or {}
    market_data_desk = ((snapshot.get("market_data") or {}).get("market_data_desk") or {})
    market_data_session_overlay = ((snapshot.get("market_data") or {}).get("market_data_session_overlay") or {})
    market_data_risk_gate = ((snapshot.get("market_data") or {}).get("market_data_risk_gate") or {})
    market_data_operating_auto_gate = ((snapshot.get("market_data") or {}).get("market_data_operating_auto_gate") or {})
    capital_margin_state = snapshot.get("capital_margin_state") or ((snapshot.get("market_data") or {}).get("capital_margin_state") or {})
    capital_aware_action_gate = snapshot.get("capital_aware_action_gate") or ((snapshot.get("market_data") or {}).get("capital_aware_action_gate") or {})
    live_read_only_capital_validation = snapshot.get("live_read_only_capital_validation") or ((snapshot.get("market_data") or {}).get("live_read_only_capital_validation") or {})
    capital_pipeline_orchestrator = snapshot.get("capital_pipeline_orchestrator") or ((snapshot.get("market_data") or {}).get("capital_pipeline_orchestrator") or {})
    strategy_allocation_engine = snapshot.get("strategy_allocation_engine") or ((snapshot.get("market_data") or {}).get("strategy_allocation_engine") or {})
    micro_option_candidates = snapshot.get("micro_option_candidates") or ((snapshot.get("market_data") or {}).get("micro_option_candidates") or {})
    paper_option_ticket_preview = snapshot.get("paper_option_ticket_preview") or ((snapshot.get("market_data") or {}).get("paper_option_ticket_preview") or {})
    option_whatif_safety_validation = snapshot.get("option_whatif_safety_validation") or ((snapshot.get("market_data") or {}).get("option_whatif_safety_validation") or {})
    limited_auto_trading_readiness = snapshot.get("limited_auto_trading_readiness") or ((snapshot.get("market_data") or {}).get("limited_auto_trading_readiness") or {})
    market_data_session_transition_gate = ((snapshot.get("market_data") or {}).get("market_data_session_transition_gate") or {})
    session_transition_release_governor = ((snapshot.get("market_data") or {}).get("session_transition_release_governor") or {})
    market_data_operating_plan = ((snapshot.get("market_data") or {}).get("market_data_operating_execution_intent_ledger") or {})
    operating_defensive_plan = ((snapshot.get("market_data") or {}).get("operating_defensive_action_plan") or {})
    operating_defensive_ledger = ((snapshot.get("market_data") or {}).get("operating_defensive_intent_ledger") or {})
    operating_defensive_mode = market_data_operating_auto_gate.get("defensive_mode") or {}
    operating_protective_packet = market_data_operating_auto_gate.get("protective_analysis_packet") or {}
    market_summary = market_data_desk.get("summary") or {}
    market_temperature = market_data_desk.get("temperature") or {}
    market_temperature_diagnostics = market_data_desk.get("temperature_diagnostics") or {}
    market_temperature_adjustment = market_data_desk.get("temperature_session_adjustment") or {}
    market_symbol_quarantine = market_data_desk.get("symbol_anomaly_quarantine") or {}
    market_components = market_temperature.get("components") or market_summary.get("component_temperatures") or {}
    market_session_summary = market_data_session_overlay.get("summary") or {}
    market_session_monitorability = market_data_session_overlay.get("monitorability_counts") or market_session_summary.get("monitorability_counts") or {}
    market_session_buckets = market_data_session_overlay.get("session_bucket_counts") or market_session_summary.get("session_bucket_counts") or {}
    market_session_next_open = market_data_session_overlay.get("next_regular_open") or {}
    macro = snapshot.get("macro_economic") or {}
    evidence_freshness = snapshot.get("evidence_freshness") or {}
    evidence_guard = evidence_freshness.get("scorer_guard") or {}
    evidence_auto_recovery = snapshot.get("evidence_auto_recovery") or {}
    source_ingest_health = snapshot.get("source_ingest_health") or {}
    source_online_supervisor = snapshot.get("source_online_supervisor") or {}
    m236_news_learning = snapshot.get("m236_news_learning") or {}
    macro_release_interpreter = snapshot.get("macro_release_interpreter") or {}
    learning_pipeline = snapshot.get("learning_pipeline") or {}
    live_scorer_learning = snapshot.get("live_scorer_learning") or {}
    live_learning_llm_notes = snapshot.get("live_learning_llm_notes") or {}
    threshold_candidate_evaluator = snapshot.get("threshold_candidate_evaluator") or {}
    m232_hot_cache = snapshot.get("m232_hot_cache") or {}
    singleton_locks = snapshot.get("singleton_locks") or {}
    promotion_gate = threshold_candidate_evaluator.get("promotion_gate") or {}
    action_queue = snapshot.get("action_queue") or {}
    action_queue_hidden = action_queue.get("hidden_action_summary") or {}
    recovery_plan = snapshot.get("recovery_plan") or {}
    recovery_executor = snapshot.get("recovery_executor") or {}
    recovery_post_validation = snapshot.get("recovery_post_validation") or {}
    operational_runbook = snapshot.get("operational_runbook") or {}
    option_confidence_recovery = snapshot.get("option_confidence_recovery_plan") or {}
    safety_latency = snapshot.get("safety_gate_latency_budget") or {}
    dashboard_status_timeline = snapshot.get("dashboard_status_timeline") or {}
    opinion = snapshot.get("ai_opinion") or {}
    safety = snapshot.get("safety") or {}
    refresh_seconds = max(0, int(refresh_seconds or 0))

    html_context = _dashboard_html_context(locals())
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  {_trading_dashboard_html_style()}
</head>
<body class="status-{escape(color)}">
<div class="layout">
  <nav>
    <h1>Command Center</h1>
    {html_context["menu_links"]}
    <p class="muted small">Refresh: <span id="refresh-counter">{refresh_seconds}</span>s</p>
    <p class="muted small">Snapshot JSON: <code>{html_context["snapshot_json_label"]}</code></p>
  </nav>
  <main>
    <section class="topbar" id="overview">
      <div class="title">
        <h2>{escape(title)}</h2>
        <p>Generated <code><span class="local-time" data-iso="{html_context["generated_at_attr"]}">{_html_text(html_context["generated_at_local"])}</span></code> &middot; Mode <code>{_html_text(snapshot.get("mode"))}</code> &middot; UI <code>{TRADING_DASHBOARD_HTML_VERSION}</code></p>
      </div>
      <div class="lamp">
        <div class="lamp-status"><span class="dot"></span><strong>{_html_text(color).upper()}</strong></div>
        <div class="lamp-reasons">{html_context["lamp_summary"]}{html_context["reasons"]}</div>
      </div>
    </section>

    <section class="grid">
      {_render_dashboard_status_recovery_cards(html_context)}

      {_render_dashboard_account_market_cards(html_context)}
      {_render_dashboard_macro_learning_cards(html_context)}
      {_render_dashboard_positions_options_cards(html_context)}
      {_render_dashboard_plan_evidence_source_cards(html_context)}
    </section>
  </main>
</div>
<script id="dashboard-snapshot" type="application/json">{html_context["embedded_snapshot"]}</script>
{_trading_dashboard_html_script(refresh_seconds)}
</body>
</html>
"""


def write_trading_dashboard_html(
    output_path: Path,
    snapshot: dict[str, Any],
    *,
    refresh_seconds: int = 15,
    snapshot_json_path: Path | str | None = None,
    title: str = "Trading Command Center",
) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        trading_dashboard_html(
            snapshot,
            refresh_seconds=refresh_seconds,
            snapshot_json_path=snapshot_json_path,
            title=title,
        ),
        encoding="utf-8",
    )
    return output_path




def _dashboard_tick_summary(
    *,
    tick_index: int,
    snapshot: dict[str, Any],
    snapshot_json_path: Path | None,
    markdown_path: Path | None,
    html_path: Path | None,
    started_at: str,
    completed_at: str,
    elapsed_seconds: float,
) -> dict[str, Any]:
    status = snapshot.get("status") or {}
    account = snapshot.get("account") or {}
    risk = snapshot.get("risk") or {}
    plan = snapshot.get("buy_sell_plan") or {}
    mark_quality = ((snapshot.get("market_data") or {}).get("mark_refresh_quality") or {}).get("quality") or {}
    market_data = ((snapshot.get("market_data") or {}).get("market_data_desk") or {})
    market_data_session_overlay = ((snapshot.get("market_data") or {}).get("market_data_session_overlay") or {})
    market_data_risk_gate = ((snapshot.get("market_data") or {}).get("market_data_risk_gate") or {})
    market_data_operating_auto_gate = ((snapshot.get("market_data") or {}).get("market_data_operating_auto_gate") or {})
    capital_margin_state = snapshot.get("capital_margin_state") or ((snapshot.get("market_data") or {}).get("capital_margin_state") or {})
    capital_aware_action_gate = snapshot.get("capital_aware_action_gate") or ((snapshot.get("market_data") or {}).get("capital_aware_action_gate") or {})
    live_read_only_capital_validation = snapshot.get("live_read_only_capital_validation") or ((snapshot.get("market_data") or {}).get("live_read_only_capital_validation") or {})
    capital_pipeline_orchestrator = snapshot.get("capital_pipeline_orchestrator") or ((snapshot.get("market_data") or {}).get("capital_pipeline_orchestrator") or {})
    strategy_allocation_engine = snapshot.get("strategy_allocation_engine") or ((snapshot.get("market_data") or {}).get("strategy_allocation_engine") or {})
    market_data_session_transition_gate = ((snapshot.get("market_data") or {}).get("market_data_session_transition_gate") or {})
    session_transition_release_governor = ((snapshot.get("market_data") or {}).get("session_transition_release_governor") or {})
    market_data_operating_plan = ((snapshot.get("market_data") or {}).get("market_data_operating_execution_intent_ledger") or {})
    operating_defensive_plan = ((snapshot.get("market_data") or {}).get("operating_defensive_action_plan") or {})
    operating_defensive_ledger = ((snapshot.get("market_data") or {}).get("operating_defensive_intent_ledger") or {})
    operating_defensive_mode = market_data_operating_auto_gate.get("defensive_mode") or {}
    operating_protective_packet = market_data_operating_auto_gate.get("protective_analysis_packet") or {}
    market_summary = market_data.get("summary") or {}
    market_temperature_adjustment = market_data.get("temperature_session_adjustment") or {}
    market_symbol_quarantine = market_data.get("symbol_anomaly_quarantine") or {}
    market_session_monitorability = market_data_session_overlay.get("monitorability_counts") or {}
    evidence_freshness = snapshot.get("evidence_freshness") or {}
    evidence_auto_recovery = snapshot.get("evidence_auto_recovery") or {}
    source_ingest_health = snapshot.get("source_ingest_health") or {}
    source_online_supervisor = snapshot.get("source_online_supervisor") or {}
    action_queue = snapshot.get("action_queue") or {}
    recovery_plan = snapshot.get("recovery_plan") or {}
    recovery_executor = snapshot.get("recovery_executor") or {}
    recovery_post_validation = snapshot.get("recovery_post_validation") or {}
    trade_split = status.get("trade_blocking_summary") or {}
    safety_latency = snapshot.get("safety_gate_latency_budget") or {}
    return {
        "tick_index": tick_index,
        "started_at": started_at,
        "completed_at": completed_at,
        "elapsed_seconds": round(float(elapsed_seconds), 6),
        "status_color": status.get("color"),
        "status_raw_color": status.get("raw_color"),
        "status_display_color": status.get("display_color"),
        "status_alert_color": status.get("alert_color"),
        "status_display_label": status.get("display_label"),
        "operational_status_color": ((status.get("operational_readiness_overlay") or {}).get("operational_status") or {}).get("color"),
        "trading_readiness_label": ((status.get("operational_readiness_overlay") or {}).get("trading_readiness") or {}).get("label"),
        "status_reasons": status.get("reasons") or [],
        "trade_blocking": trade_split.get("trade_blocking"),
        "trade_blocking_reasons": trade_split.get("trade_blocking_reasons") or [],
        "monitoring_only_reasons": trade_split.get("monitoring_only_reasons") or [],
        "ai_stance": (snapshot.get("ai_opinion") or {}).get("stance"),
        "risk_state": risk.get("risk_state"),
        "new_entries_allowed": risk.get("new_entries_allowed"),
        "buying_power": account.get("buying_power"),
        "available_funds": account.get("available_funds"),
        "pending_paper_execution_count": plan.get("pending_paper_execution_count"),
        "release_governed_plan_blocked_count": plan.get("blocked_by_session_transition_release_governor_count"),
        "news_count": len(snapshot.get("news_ticker") or []),
        "standing_regime_count": len(snapshot.get("standing_regimes") or []),
        "mark_refresh_status_counts": mark_quality.get("refresh_status_counts") or {},
        "quality_action_counts": mark_quality.get("quality_action_counts") or {},
        "market_data_status": market_data.get("status") or market_summary.get("status"),
        "market_temperature_label": (market_data.get("temperature") or {}).get("label") or market_summary.get("temperature_label"),
        "market_temperature_score": (market_data.get("temperature") or {}).get("score") if (market_data.get("temperature") or {}).get("score") is not None else market_summary.get("temperature_score"),
        "market_temperature_adjusted_interpretation": market_temperature_adjustment.get("adjusted_interpretation"),
        "market_temperature_reescalated": market_temperature_adjustment.get("re_escalate_to_trade_blocking_now"),
        "symbol_quarantine_count": market_symbol_quarantine.get("candidate_count"),
        "market_data_risk_gate_status": market_data_risk_gate.get("gate_status"),
        "market_data_risk_gate_action": market_data_risk_gate.get("action"),
        "market_data_entry_size_multiplier": market_data_risk_gate.get("entry_size_multiplier"),
        "market_data_operating_mode": market_data_operating_auto_gate.get("operating_mode"),
        "market_data_operating_auto_ready_count": market_data_operating_auto_gate.get("auto_ready_count"),
        "capital_margin_status": capital_margin_state.get("status"),
        "capital_leverage_mode": capital_margin_state.get("leverage_mode"),
        "capital_max_safe_withdrawal": (capital_margin_state.get("withdrawal_guard") or {}).get("max_safe_withdrawal"),
        "capital_cash_budget": (capital_margin_state.get("budgets") or {}).get("deployable_cash_after_reserve"),
        "capital_options_premium_budget": (capital_margin_state.get("budgets") or {}).get("options_premium_budget"),
        "capital_aware_ready_count": capital_aware_action_gate.get("capital_ready_count"),
        "capital_aware_resized_count": capital_aware_action_gate.get("capital_resized_count"),
        "capital_aware_blocked_count": capital_aware_action_gate.get("capital_blocked_count"),
        "capital_aware_top_block_reasons": dict(list((capital_aware_action_gate.get("block_reason_counts") or {}).items())[:5]),
        "live_capital_validation_status": live_read_only_capital_validation.get("status"),
        "live_capital_feasible_count": live_read_only_capital_validation.get("current_size_feasible_count"),
        "live_capital_resize_required_count": live_read_only_capital_validation.get("resize_required_count"),
        "live_capital_blocked_count": live_read_only_capital_validation.get("blocked_count"),
        "live_capital_top_block_reasons": dict(list((live_read_only_capital_validation.get("block_reason_counts") or {}).items())[:5]),
        "trade_executable_current_size_count": live_read_only_capital_validation.get("trade_executable_current_size_count"),
        "trade_executable_after_resize_count": live_read_only_capital_validation.get("trade_executable_after_resize_count"),
        "not_trade_executable_count": live_read_only_capital_validation.get("not_trade_executable_count"),
        "trade_executable_blocked_by_gate_counts": live_read_only_capital_validation.get("blocked_by_gate_counts") or {},
        "micro_policy_operating_mode": live_read_only_capital_validation.get("micro_policy_operating_mode") or strategy_allocation_engine.get("micro_policy_operating_mode"),
        "micro_policy_exit_transition": (live_read_only_capital_validation.get("micro_policy_state") or {}).get("transition"),
        "micro_policy_exit_confirmations": (live_read_only_capital_validation.get("micro_policy_state") or {}).get("consecutive_exit_confirmations"),
        "micro_policy_blocked_count": live_read_only_capital_validation.get("micro_policy_blocked_count"),
        "capital_pipeline_status": capital_pipeline_orchestrator.get("status"),
        "capital_pipeline_rebuild_required_count": capital_pipeline_orchestrator.get("rebuild_required_count"),
        "capital_pipeline_stale_or_missing_count": capital_pipeline_orchestrator.get("stale_or_missing_count"),
        "capital_pipeline_rebuild_steps": capital_pipeline_orchestrator.get("rebuild_steps") or [],
        "strategy_allocation_ready_count": strategy_allocation_engine.get("paper_trade_ready_count"),
        "strategy_allocation_hedge_only_count": strategy_allocation_engine.get("hedge_only_count"),
        "strategy_allocation_blocked_count": strategy_allocation_engine.get("blocked_count"),
        "strategy_allocation_micro_policy_blocked_count": strategy_allocation_engine.get("micro_policy_blocked_count"),
        "strategy_allocation_recommendation_counts": strategy_allocation_engine.get("recommendation_counts") or {},
        "strategy_allocation_expression_counts": strategy_allocation_engine.get("expression_counts") or {},
        "market_data_operating_pending_paper_execution_count": market_data_operating_plan.get("pending_paper_execution_count"),
        "market_data_operating_release_governed_blocked_count": market_data_operating_plan.get("blocked_by_session_transition_release_governor_count"),
        "market_data_operating_defensive_severity": operating_defensive_mode.get("severity"),
        "market_data_operating_alpha_allowed": operating_defensive_mode.get("ordinary_alpha_entries_allowed"),
        "market_data_operating_hedge_allowed": operating_defensive_mode.get("hedge_or_defensive_entries_allowed"),
        "market_data_operating_exit_risk_reduction_allowed": operating_defensive_mode.get("exit_or_risk_reduction_allowed"),
        "market_data_operating_max_new_entry_size_multiplier": operating_defensive_mode.get("max_new_entry_size_multiplier"),
        "market_data_operating_protective_candidate_count": operating_protective_packet.get("candidate_count"),
        "market_data_operating_protective_ticketing_allowed": operating_protective_packet.get("ticketing_allowed"),
        "operating_defensive_action_count": operating_defensive_plan.get("action_count"),
        "operating_defensive_highest_priority_score": operating_defensive_plan.get("highest_priority_score"),
        "operating_defensive_requires_option_chain_count": operating_defensive_plan.get("requires_option_chain_count"),
        "operating_defensive_requires_margin_whatif_count": operating_defensive_plan.get("requires_margin_whatif_count"),
        "operating_defensive_intent_pending_count": operating_defensive_ledger.get("pending_defensive_paper_execution_count"),
        "operating_defensive_intent_blocked_count": operating_defensive_ledger.get("blocked_count"),
        "operating_defensive_intent_strict_gate_mode": operating_defensive_ledger.get("strict_gate_mode"),
        "market_data_delayed_count": market_summary.get("delayed_count"),
        "market_data_missing_price_count": market_summary.get("missing_price_count"),
        "market_data_warming_up_count": market_summary.get("warming_up_count"),
        "market_data_alert_count": market_summary.get("alert_count"),
        "market_data_session": market_data_session_overlay.get("session_name"),
        "market_data_session_risk_interpretation": market_data_session_overlay.get("risk_interpretation"),
        "market_data_session_monitorable_now_count": market_session_monitorability.get("monitorable_now"),
        "market_data_session_regular_dependent_count": market_session_monitorability.get("monitor_if_fresh_price_present"),
        "market_data_stock_cash_overnight_monitoring_enabled": market_data_session_overlay.get("stock_cash_overnight_monitoring_enabled"),
        "market_data_option_quotes_regular_session_dependent": market_data_session_overlay.get("option_quotes_regular_session_dependent"),
        "market_data_session_transition_decision": market_data_session_transition_gate.get("decision"),
        "market_data_session_transition_severity": market_data_session_transition_gate.get("severity"),
        "market_data_session_transition_type": market_data_session_transition_gate.get("transition_type"),
        "market_data_session_transition_rebuild_required": market_data_session_transition_gate.get("rebuild_required_before_auto_release"),
        "market_data_session_transition_alpha_allowed": market_data_session_transition_gate.get("ordinary_alpha_entries_allowed_after_transition"),
        "session_transition_release_status": session_transition_release_governor.get("release_status"),
        "session_transition_release_allowed": session_transition_release_governor.get("ordinary_alpha_release_allowed"),
        "session_transition_release_failed_check_count": session_transition_release_governor.get("failed_check_count"),
        "evidence_freshness_status": evidence_freshness.get("overall_status"),
        "evidence_stale_source_count": len(evidence_freshness.get("stale_source_keys") or []),
        "evidence_guard_action": (evidence_freshness.get("scorer_guard") or {}).get("action"),
        "evidence_auto_recovery_status": evidence_auto_recovery.get("status"),
        "evidence_auto_recovery_freshness_status": evidence_auto_recovery.get("last_freshness_status"),
        "evidence_auto_recovery_failures": evidence_auto_recovery.get("consecutive_failures"),
        "evidence_auto_recovery_stale_source_count": len(evidence_auto_recovery.get("last_stale_source_keys") or []),
        "evidence_auto_recovery_lagging_source_count": len(evidence_auto_recovery.get("last_lagging_source_keys") or []),
        "evidence_auto_recovery_gap_currently_resolved": (status or {}).get("evidence_auto_recovery_gap_currently_resolved"),
        "source_ingest_health_status": source_ingest_health.get("overall_status"),
        "source_ingest_online_problem_count": len(source_ingest_health.get("online_problem_source_keys") or []),
        "source_ingest_degraded_count": len(source_ingest_health.get("degraded_source_keys") or []),
        "source_online_supervisor_status": source_online_supervisor.get("final_status"),
        "source_online_supervisor_action_status_counts": source_online_supervisor.get("final_action_status_counts") or {},
        "source_online_supervisor_failed_action_count": len(source_online_supervisor.get("failed_actions") or []),
        "source_online_supervisor_online_problem_count": len(source_online_supervisor.get("final_online_problem_source_keys") or []),
        "action_queue_total_action_count": action_queue.get("total_action_count") or 0,
        "action_queue_visible_action_count": action_queue.get("visible_action_count") or 0,
        "action_queue_highest_severity": action_queue.get("highest_severity"),
        "action_queue_highest_priority_score": action_queue.get("highest_priority_score") or 0,
        "action_queue_top_action_ids": [item.get("action_id") for item in action_queue.get("top_actions") or []],
        "recovery_plan_planned_step_count": recovery_plan.get("planned_step_count") or 0,
        "recovery_plan_highest_severity": recovery_plan.get("highest_severity"),
        "recovery_plan_highest_priority_step_id": recovery_plan.get("highest_priority_step_id"),
        "recovery_plan_top_step_ids": [item.get("step_id") for item in recovery_plan.get("top_steps") or []],
        "recovery_executor_final_status": recovery_executor.get("final_status"),
        "recovery_executor_execute_requested": bool(recovery_executor.get("execute_requested")),
        "recovery_executor_selected_step_count": recovery_executor.get("selected_step_count") or 0,
        "recovery_executor_executed_command_count": recovery_executor.get("executed_command_count") or 0,
        "recovery_executor_failure_count": recovery_executor.get("failure_count") or 0,
        "recovery_post_validation_outcome": recovery_post_validation.get("outcome"),
        "recovery_post_validation_improved_metric_count": recovery_post_validation.get("improved_metric_count") or 0,
        "recovery_post_validation_worsened_metric_count": recovery_post_validation.get("worsened_metric_count") or 0,
        "recovery_post_validation_unchanged_metric_count": recovery_post_validation.get("unchanged_metric_count") or 0,
        "safety_gate_latency_status": safety_latency.get("status"),
        "safety_gate_latency_total_ms": safety_latency.get("total_elapsed_ms"),
        "missing_source_count": len(snapshot.get("missing_sources") or []),
        "snapshot_json_path": str(snapshot_json_path) if snapshot_json_path else None,
        "markdown_path": str(markdown_path) if markdown_path else None,
        "html_path": str(html_path) if html_path else None,
    }


def run_trading_dashboard_loop(
    *,
    account_snapshot_path: Path | None,
    paper_portfolio_state_path: Path | None,
    portfolio_risk_state_path: Path | None,
    execution_intent_ledger_path: Path | None,
    mark_quality_daemon_path: Path | None,
    economic_desk_path: Path | None,
    market_data_path: Path | None,
    database_path: Path | None,
    snapshot_json_output_path: Path,
    markdown_output_path: Path | None,
    html_output_path: Path,
    tick_log_jsonl_path: Path | None,
    alert_state_json_path: Path | None = None,
    alert_events_jsonl_path: Path | None = None,
    telegram_delivery_jsonl_path: Path | None = None,
    summary_json_output_path: Path | None = None,
    action_queue_json_output_path: Path | None = None,
    action_queue_markdown_output_path: Path | None = None,
    recovery_plan_json_output_path: Path | None = None,
    recovery_plan_markdown_output_path: Path | None = None,
    recovery_executor_json_output_path: Path | None = None,
    recovery_executor_markdown_output_path: Path | None = None,
    recovery_post_validation_json_output_path: Path | None = None,
    recovery_post_validation_markdown_output_path: Path | None = None,
    market_data_session_overlay_path: Path | None = None,
    market_data_risk_gate_path: Path | None = None,
    market_data_operating_auto_gate_path: Path | None = None,
    capital_margin_state_path: Path | None = None,
    capital_aware_action_gate_path: Path | None = None,
    live_read_only_capital_validation_path: Path | None = None,
    capital_pipeline_orchestrator_path: Path | None = None,
    market_data_session_transition_gate_path: Path | None = None,
    session_transition_release_governor_path: Path | None = None,
    market_data_operating_execution_intent_ledger_path: Path | None = None,
    operating_defensive_action_plan_path: Path | None = None,
    operating_defensive_intent_ledger_path: Path | None = None,
    evidence_freshness_path: Path | None = None,
    evidence_refresh_watchdog_path: Path | None = None,
    source_ingest_health_path: Path | None = None,
    source_online_supervisor_path: Path | None = None,
    live_scorer_learning_path: Path | None = None,
    live_learning_llm_notes_path: Path | None = None,
    threshold_candidate_evaluator_path: Path | None = None,
    m232_hot_cache_path: Path | None = None,
    execution_safety_cache_supervisor_path: Path | None = None,
    options_market_structure_path: Path | None = None,
    options_market_structure_supervisor_path: Path | None = None,
    strategy_allocation_engine_path: Path | None = None,
    micro_option_candidates_path: Path | None = None,
    paper_option_ticket_preview_path: Path | None = None,
    option_whatif_safety_validation_path: Path | None = None,
    news_limit: int = 8,
    regime_limit: int = 8,
    mode: str = "paper",
    refresh_seconds: int = 1,
    title: str = "Trading Command Center",
    max_ticks: int = 1,
    interval_seconds: float = 1.0,
    sleep_between_ticks: bool = False,
    alerts_enabled: bool = True,
    emit_resolution_alerts: bool = False,
    alert_buying_power_threshold: float = 100.0,
    alert_available_funds_threshold: float = 100.0,
    alert_worsening_min_delta: float = 1.0,
    alert_worsening_relative: float = 0.10,
    telegram_enabled: bool = False,
    telegram_bot_token: str | None = None,
    telegram_chat_id: str | None = None,
    telegram_dry_run: bool = False,
    telegram_timeout_seconds: float = 10.0,
    tick_log_max_bytes: int | None = DEFAULT_DASHBOARD_TICK_LOG_MAX_BYTES,
    tick_log_backups: int = DEFAULT_DASHBOARD_TICK_LOG_BACKUPS,
    pre_snapshot_hook: Callable[[int, str], dict[str, Any] | None] | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    if max_ticks < 0:
        raise ValueError("max_ticks must be >= 0; use 0 only for externally interrupted continuous operation")
    if interval_seconds < 0:
        raise ValueError("interval_seconds must be >= 0")
    started_at = _iso_now()
    run_id = run_id or f"trading_dashboard_loop_{started_at.replace(':', '').replace('+', 'Z')}"
    tick_summaries: list[dict[str, Any]] = []
    tick_log_rotation_events: list[dict[str, Any]] = []
    pre_snapshot_refresh_summaries: list[dict[str, Any]] = []
    alert_summaries: list[dict[str, Any]] = []
    telegram_delivery_summaries: list[dict[str, Any]] = []
    alert_state = _read_dashboard_alert_state(alert_state_json_path) if alerts_enabled else {
        "dashboard_alert_version": TRADING_DASHBOARD_ALERT_VERSION,
        "active_conditions": {},
        "alerts_disabled": True,
    }
    tick_index = 0
    last_snapshot: dict[str, Any] | None = None
    continuous = max_ticks == 0
    while continuous or tick_index < max_ticks:
        tick_index += 1
        tick_started_monotonic = time.monotonic()
        tick_started_at = _iso_now()
        previous_snapshot_payload, _previous_snapshot_error = _read_json_file(snapshot_json_output_path)
        previous_action_queue_payload, _previous_action_queue_error = _read_json_file(action_queue_json_output_path)
        previous_recovery_plan_payload, _previous_recovery_plan_error = _read_json_file(recovery_plan_json_output_path)
        pre_snapshot_refresh: dict[str, Any] | None = None
        if pre_snapshot_hook is not None:
            try:
                pre_snapshot_refresh = pre_snapshot_hook(tick_index, run_id) or {
                    "enabled": True,
                    "applied": False,
                    "reason": "hook_returned_no_summary",
                }
            except Exception as exc:
                pre_snapshot_refresh = {
                    "enabled": True,
                    "applied": False,
                    "status": "failed",
                    "reason": "pre_snapshot_hook_error",
                    "error": str(exc),
                }
            pre_snapshot_refresh["tick_index"] = tick_index
            pre_snapshot_refresh_summaries.append(pre_snapshot_refresh)
        snapshot = build_trading_dashboard_snapshot(
            account_snapshot_path=account_snapshot_path,
            paper_portfolio_state_path=paper_portfolio_state_path,
            portfolio_risk_state_path=portfolio_risk_state_path,
            execution_intent_ledger_path=execution_intent_ledger_path,
            mark_quality_daemon_path=mark_quality_daemon_path,
            economic_desk_path=economic_desk_path,
            market_data_path=market_data_path,
            market_data_session_overlay_path=market_data_session_overlay_path,
            market_data_risk_gate_path=market_data_risk_gate_path,
            market_data_operating_auto_gate_path=market_data_operating_auto_gate_path,
            capital_margin_state_path=capital_margin_state_path,
            capital_aware_action_gate_path=capital_aware_action_gate_path,
            live_read_only_capital_validation_path=live_read_only_capital_validation_path,
            capital_pipeline_orchestrator_path=capital_pipeline_orchestrator_path,
            market_data_session_transition_gate_path=market_data_session_transition_gate_path,
            session_transition_release_governor_path=session_transition_release_governor_path,
            market_data_operating_execution_intent_ledger_path=market_data_operating_execution_intent_ledger_path,
            operating_defensive_action_plan_path=operating_defensive_action_plan_path,
            operating_defensive_intent_ledger_path=operating_defensive_intent_ledger_path,
            database_path=database_path,
            evidence_freshness_path=evidence_freshness_path,
            evidence_refresh_watchdog_path=evidence_refresh_watchdog_path,
            source_ingest_health_path=source_ingest_health_path,
            source_online_supervisor_path=source_online_supervisor_path,
            live_scorer_learning_path=live_scorer_learning_path,
            live_learning_llm_notes_path=live_learning_llm_notes_path,
            threshold_candidate_evaluator_path=threshold_candidate_evaluator_path,
            m232_hot_cache_path=m232_hot_cache_path,
            execution_safety_cache_supervisor_path=execution_safety_cache_supervisor_path,
            options_market_structure_path=options_market_structure_path,
            options_market_structure_supervisor_path=options_market_structure_supervisor_path,
            strategy_allocation_engine_path=strategy_allocation_engine_path,
            micro_option_candidates_path=micro_option_candidates_path,
            paper_option_ticket_preview_path=paper_option_ticket_preview_path,
            option_whatif_safety_validation_path=option_whatif_safety_validation_path,
            news_limit=news_limit,
            regime_limit=regime_limit,
            mode=mode,
        )
        snapshot["loop"] = {
            "dashboard_loop_version": TRADING_DASHBOARD_LOOP_VERSION,
            "run_id": run_id,
            "tick_index": tick_index,
            "max_ticks": max_ticks,
            "interval_seconds": interval_seconds,
            "sleep_between_ticks": bool(sleep_between_ticks),
            "alerts_enabled": bool(alerts_enabled),
            "telegram_enabled": bool(telegram_enabled),
            "pre_snapshot_refresh": pre_snapshot_refresh,
        }
        action_queue_payload = ((snapshot.get("action_queue") or {}).get("full_queue") or {})
        action_queue_json_path: Path | None = None
        action_queue_markdown_path: Path | None = None
        recovery_plan_json_path: Path | None = None
        recovery_plan_markdown_path: Path | None = None
        recovery_executor_json_path: Path | None = None
        recovery_executor_markdown_path: Path | None = None
        recovery_post_validation_json_path: Path | None = None
        recovery_post_validation_markdown_path: Path | None = None
        if action_queue_payload:
            from recorder.dashboard_action_queue import (
                write_dashboard_action_queue_json,
                write_dashboard_action_queue_markdown,
            )
            from recorder.dashboard_operations import (
                build_dashboard_recovery_plan,
                dashboard_recovery_plan_summary,
                write_dashboard_recovery_plan_json,
                write_dashboard_recovery_plan_markdown,
            )
            from recorder.dashboard_operations import (
                dashboard_recovery_executor_summary,
                run_dashboard_recovery_executor,
                write_dashboard_recovery_executor_json,
                write_dashboard_recovery_executor_markdown,
            )
            from recorder.dashboard_operations import (
                build_dashboard_recovery_post_validation,
                dashboard_recovery_post_validation_summary,
                write_dashboard_recovery_post_validation_json,
                write_dashboard_recovery_post_validation_markdown,
            )

            if action_queue_json_output_path is not None:
                action_queue_json_path = write_dashboard_action_queue_json(action_queue_json_output_path, action_queue_payload)
            if action_queue_markdown_output_path is not None:
                action_queue_markdown_path = write_dashboard_action_queue_markdown(action_queue_markdown_output_path, action_queue_payload)
            snapshot["action_queue"]["json_output_path"] = str(action_queue_json_path) if action_queue_json_path else None
            snapshot["action_queue"]["markdown_output_path"] = str(action_queue_markdown_path) if action_queue_markdown_path else None
            recovery_plan_payload = build_dashboard_recovery_plan(action_queue_payload, dry_run=True)
            snapshot["recovery_plan"] = dashboard_recovery_plan_summary(recovery_plan_payload)
            snapshot["recovery_plan"]["full_plan"] = recovery_plan_payload
            if recovery_plan_json_output_path is not None:
                recovery_plan_json_path = write_dashboard_recovery_plan_json(recovery_plan_json_output_path, recovery_plan_payload)
            if recovery_plan_markdown_output_path is not None:
                recovery_plan_markdown_path = write_dashboard_recovery_plan_markdown(recovery_plan_markdown_output_path, recovery_plan_payload)
            snapshot["recovery_plan"]["json_output_path"] = str(recovery_plan_json_path) if recovery_plan_json_path else None
            snapshot["recovery_plan"]["markdown_output_path"] = str(recovery_plan_markdown_path) if recovery_plan_markdown_path else None
            recovery_executor_payload = run_dashboard_recovery_executor(recovery_plan_payload, execute=False, max_steps=3)
            snapshot["recovery_executor"] = dashboard_recovery_executor_summary(recovery_executor_payload)
            snapshot["recovery_executor"]["full_report"] = recovery_executor_payload
            if recovery_executor_json_output_path is not None:
                recovery_executor_json_path = write_dashboard_recovery_executor_json(recovery_executor_json_output_path, recovery_executor_payload)
            if recovery_executor_markdown_output_path is not None:
                recovery_executor_markdown_path = write_dashboard_recovery_executor_markdown(recovery_executor_markdown_output_path, recovery_executor_payload)
            snapshot["recovery_executor"]["json_output_path"] = str(recovery_executor_json_path) if recovery_executor_json_path else None
            snapshot["recovery_executor"]["markdown_output_path"] = str(recovery_executor_markdown_path) if recovery_executor_markdown_path else None
            recovery_post_validation_payload = build_dashboard_recovery_post_validation(
                before_snapshot=previous_snapshot_payload or {},
                after_snapshot=snapshot,
                before_action_queue=previous_action_queue_payload or {},
                after_action_queue=action_queue_payload,
                before_recovery_plan=previous_recovery_plan_payload or {},
                after_recovery_plan=recovery_plan_payload,
                recovery_executor=recovery_executor_payload,
            )
            snapshot["recovery_post_validation"] = dashboard_recovery_post_validation_summary(recovery_post_validation_payload)
            snapshot["recovery_post_validation"]["full_report"] = recovery_post_validation_payload
            if recovery_post_validation_json_output_path is not None:
                recovery_post_validation_json_path = write_dashboard_recovery_post_validation_json(recovery_post_validation_json_output_path, recovery_post_validation_payload)
            if recovery_post_validation_markdown_output_path is not None:
                recovery_post_validation_markdown_path = write_dashboard_recovery_post_validation_markdown(recovery_post_validation_markdown_output_path, recovery_post_validation_payload)
            snapshot["recovery_post_validation"]["json_output_path"] = str(recovery_post_validation_json_path) if recovery_post_validation_json_path else None
            snapshot["recovery_post_validation"]["markdown_output_path"] = str(recovery_post_validation_markdown_path) if recovery_post_validation_markdown_path else None
        snapshot_json_path = write_trading_dashboard_snapshot_json(snapshot_json_output_path, snapshot)
        markdown_path = write_trading_dashboard_markdown(markdown_output_path, snapshot) if markdown_output_path else None
        html_path = write_trading_dashboard_html(
            html_output_path,
            snapshot,
            refresh_seconds=refresh_seconds,
            snapshot_json_path=snapshot_json_path,
            title=title,
        )
        tick_completed_at = _iso_now()
        elapsed = time.monotonic() - tick_started_monotonic
        tick_summary = _dashboard_tick_summary(
            tick_index=tick_index,
            snapshot=snapshot,
            snapshot_json_path=snapshot_json_path,
            markdown_path=markdown_path,
            html_path=html_path,
            started_at=tick_started_at,
            completed_at=tick_completed_at,
            elapsed_seconds=elapsed,
        )
        tick_summary["run_id"] = run_id
        tick_summary["dashboard_loop_version"] = TRADING_DASHBOARD_LOOP_VERSION
        if pre_snapshot_refresh is not None:
            tick_summary["pre_snapshot_refresh"] = pre_snapshot_refresh
        if action_queue_json_path is not None or action_queue_markdown_path is not None:
            tick_summary["action_queue_json_path"] = str(action_queue_json_path) if action_queue_json_path else None
            tick_summary["action_queue_markdown_path"] = str(action_queue_markdown_path) if action_queue_markdown_path else None
        if recovery_plan_json_path is not None or recovery_plan_markdown_path is not None:
            tick_summary["recovery_plan_json_path"] = str(recovery_plan_json_path) if recovery_plan_json_path else None
            tick_summary["recovery_plan_markdown_path"] = str(recovery_plan_markdown_path) if recovery_plan_markdown_path else None
        if recovery_executor_json_path is not None or recovery_executor_markdown_path is not None:
            tick_summary["recovery_executor_json_path"] = str(recovery_executor_json_path) if recovery_executor_json_path else None
            tick_summary["recovery_executor_markdown_path"] = str(recovery_executor_markdown_path) if recovery_executor_markdown_path else None
        if recovery_post_validation_json_path is not None or recovery_post_validation_markdown_path is not None:
            tick_summary["recovery_post_validation_json_path"] = str(recovery_post_validation_json_path) if recovery_post_validation_json_path else None
            tick_summary["recovery_post_validation_markdown_path"] = str(recovery_post_validation_markdown_path) if recovery_post_validation_markdown_path else None
        alerts: list[dict[str, Any]] = []
        if alerts_enabled:
            alert_state, alerts = evaluate_dashboard_alerts(
                snapshot,
                alert_state,
                now=tick_completed_at,
                buying_power_threshold=alert_buying_power_threshold,
                available_funds_threshold=alert_available_funds_threshold,
                worsening_min_delta=alert_worsening_min_delta,
                worsening_relative=alert_worsening_relative,
                emit_resolution_alerts=emit_resolution_alerts,
                run_id=run_id,
                tick_index=tick_index,
            )
            alert_state["run_id"] = run_id
            if alert_state_json_path is not None:
                write_dashboard_alert_state(alert_state_json_path, alert_state)
            for alert in alerts:
                if alert_events_jsonl_path is not None:
                    _atomic_write_jsonl(alert_events_jsonl_path, alert)
                alert_summaries.append(alert)
            if telegram_enabled and alerts:
                delivery = deliver_dashboard_alerts_to_telegram(
                    alerts,
                    bot_token=telegram_bot_token,
                    chat_id=telegram_chat_id,
                    title=title,
                    status_color=(snapshot.get("status") or {}).get("color"),
                    run_id=run_id,
                    tick_index=tick_index,
                    timeout_seconds=telegram_timeout_seconds,
                    dry_run=telegram_dry_run,
                )
                telegram_delivery_summaries.append(delivery)
                if telegram_delivery_jsonl_path is not None:
                    _atomic_write_jsonl(telegram_delivery_jsonl_path, delivery)
        tick_summary["alert_count"] = len(alerts)
        tick_summary["alert_event_types"] = [alert.get("event_type") for alert in alerts]
        tick_summary["alert_condition_keys"] = [alert.get("condition_key") for alert in alerts]
        tick_summary["telegram_delivery_count"] = 1 if telegram_enabled and alerts else 0
        if telegram_delivery_summaries and telegram_delivery_summaries[-1].get("tick_index") == tick_index:
            tick_summary["telegram_delivery_status"] = telegram_delivery_summaries[-1].get("status")
        tick_summaries.append(tick_summary)
        if tick_log_jsonl_path is not None:
            rotation = _rotate_jsonl_if_needed(
                tick_log_jsonl_path,
                max_bytes=tick_log_max_bytes,
                backups=tick_log_backups,
            )
            if rotation.get("rotated") or rotation.get("error"):
                rotation["tick_index"] = tick_index
                rotation["checked_at"] = tick_completed_at
                tick_log_rotation_events.append(rotation)
                tick_summary["tick_log_rotation"] = rotation
            _atomic_write_jsonl(tick_log_jsonl_path, tick_summary)
        last_snapshot = snapshot
        if (continuous or tick_index < max_ticks) and sleep_between_ticks and interval_seconds > 0:
            sleep_for = max(0.0, interval_seconds - elapsed)
            if sleep_for > 0:
                time.sleep(sleep_for)
    completed_at = _iso_now()
    final_status = (last_snapshot or {}).get("status") or {}
    result = {
        "dashboard_loop_version": TRADING_DASHBOARD_LOOP_VERSION,
        "run_id": run_id,
        "started_at": started_at,
        "completed_at": completed_at,
        "max_ticks": max_ticks,
        "interval_seconds": interval_seconds,
        "sleep_between_ticks": bool(sleep_between_ticks),
        "executed_tick_count": len(tick_summaries),
        "pre_snapshot_refresh_enabled": pre_snapshot_hook is not None,
        "pre_snapshot_refresh_count": len(pre_snapshot_refresh_summaries),
        "pre_snapshot_refresh_summaries": pre_snapshot_refresh_summaries,
        "final_status": final_status,
        "final_ai_stance": ((last_snapshot or {}).get("ai_opinion") or {}).get("stance"),
        "final_snapshot_json_path": str(snapshot_json_output_path.expanduser().resolve()),
        "final_markdown_path": str(markdown_output_path.expanduser().resolve()) if markdown_output_path else None,
        "final_html_path": str(html_output_path.expanduser().resolve()),
        "tick_log_jsonl_path": str(tick_log_jsonl_path.expanduser().resolve()) if tick_log_jsonl_path else None,
        "tick_log_rotation": {
            "enabled": bool(tick_log_jsonl_path is not None and tick_log_max_bytes is not None and int(tick_log_max_bytes or 0) > 0),
            "max_bytes": int(tick_log_max_bytes) if tick_log_max_bytes is not None else None,
            "backups": int(tick_log_backups),
            "rotation_event_count": len(tick_log_rotation_events),
            "events": tick_log_rotation_events[-10:],
        },
        "alert_state_json_path": str(alert_state_json_path.expanduser().resolve()) if alert_state_json_path else None,
        "alert_events_jsonl_path": str(alert_events_jsonl_path.expanduser().resolve()) if alert_events_jsonl_path else None,
        "alerts_enabled": bool(alerts_enabled),
        "alert_event_count": len(alert_summaries),
        "alert_events": alert_summaries,
        "telegram_enabled": bool(telegram_enabled),
        "telegram_dry_run": bool(telegram_dry_run),
        "telegram_delivery_jsonl_path": str(telegram_delivery_jsonl_path.expanduser().resolve()) if telegram_delivery_jsonl_path else None,
        "telegram_delivery_count": len(telegram_delivery_summaries),
        "telegram_deliveries": telegram_delivery_summaries,
        "final_action_queue": (last_snapshot or {}).get("action_queue") or {},
        "final_recovery_plan": (last_snapshot or {}).get("recovery_plan") or {},
        "final_recovery_executor": (last_snapshot or {}).get("recovery_executor") or {},
        "final_recovery_post_validation": (last_snapshot or {}).get("recovery_post_validation") or {},
        "action_queue_json_path": str(action_queue_json_output_path.expanduser().resolve()) if action_queue_json_output_path else None,
        "action_queue_markdown_path": str(action_queue_markdown_output_path.expanduser().resolve()) if action_queue_markdown_output_path else None,
        "recovery_plan_json_path": str(recovery_plan_json_output_path.expanduser().resolve()) if recovery_plan_json_output_path else None,
        "recovery_plan_markdown_path": str(recovery_plan_markdown_output_path.expanduser().resolve()) if recovery_plan_markdown_output_path else None,
        "recovery_executor_json_path": str(recovery_executor_json_output_path.expanduser().resolve()) if recovery_executor_json_output_path else None,
        "recovery_executor_markdown_path": str(recovery_executor_markdown_output_path.expanduser().resolve()) if recovery_executor_markdown_output_path else None,
        "recovery_post_validation_json_path": str(recovery_post_validation_json_output_path.expanduser().resolve()) if recovery_post_validation_json_output_path else None,
        "recovery_post_validation_markdown_path": str(recovery_post_validation_markdown_output_path.expanduser().resolve()) if recovery_post_validation_markdown_output_path else None,
        "tick_summaries": tick_summaries,
        "safety": {
            "read_only_dashboard_loop": True,
            "paper_only_control_surface": True,
            "ibkr_order_submitted": False,
            "transmit_enabled": False,
            "live_order_routing_enabled": False,
        },
    }
    if summary_json_output_path is not None:
        write_trading_dashboard_snapshot_json(summary_json_output_path, result)
    return result


def trading_dashboard_loop_markdown(result: dict[str, Any]) -> str:
    final_action_queue = result.get("final_action_queue") or {}
    final_recovery_plan = result.get("final_recovery_plan") or {}
    final_recovery_executor = result.get("final_recovery_executor") or {}
    final_recovery_post_validation = result.get("final_recovery_post_validation") or {}
    final_tick = (result.get("tick_summaries") or [{}])[-1] if result.get("tick_summaries") else {}
    lines = [
        "# Trading Command Center Loop Summary",
        "",
        f"- Version: `{result.get('dashboard_loop_version')}`",
        f"- Run ID: `{result.get('run_id')}`",
        f"- Started / completed: `{result.get('started_at')}` / `{result.get('completed_at')}`",
        f"- Executed ticks: `{result.get('executed_tick_count')}`",
        f"- Interval seconds: `{result.get('interval_seconds')}` / sleep `{result.get('sleep_between_ticks')}`",
        f"- Final status: `{(result.get('final_status') or {}).get('color')}` reasons `{(result.get('final_status') or {}).get('reasons') or []}`",
        f"- Final AI stance: `{result.get('final_ai_stance')}`",
        f"- Snapshot JSON: `{result.get('final_snapshot_json_path')}`",
        f"- HTML dashboard: `{result.get('final_html_path')}`",
        f"- Action queue: `{final_action_queue.get('highest_severity') or '-'}` actions `{final_action_queue.get('visible_action_count') or 0}` / `{final_action_queue.get('total_action_count') or 0}` priority `{final_action_queue.get('highest_priority_score') or 0}`",
        f"- Action queue JSON: `{result.get('action_queue_json_path') or '-'}`",
        f"- Recovery plan: `{final_recovery_plan.get('highest_severity') or '-'}` steps `{final_recovery_plan.get('planned_step_count') or 0}` dry_run `{final_recovery_plan.get('dry_run', True)}`",
        f"- Recovery plan JSON: `{result.get('recovery_plan_json_path') or '-'}`",
        f"- Recovery executor: `{final_recovery_executor.get('final_status') or '-'}` execute `{final_recovery_executor.get('execute_requested', False)}` commands `{final_recovery_executor.get('executed_command_count') or 0}` failures `{final_recovery_executor.get('failure_count') or 0}`",
        f"- Recovery executor JSON: `{result.get('recovery_executor_json_path') or '-'}`",
        f"- Recovery post-validation: `{final_recovery_post_validation.get('outcome') or '-'}` improved `{final_recovery_post_validation.get('improved_metric_count') or 0}` worsened `{final_recovery_post_validation.get('worsened_metric_count') or 0}`",
        f"- Recovery post-validation JSON: `{result.get('recovery_post_validation_json_path') or '-'}`",
        f"- Operating gate: `{final_tick.get('market_data_operating_mode') or '-'}` auto-ready `{final_tick.get('market_data_operating_auto_ready_count')}` pending `{final_tick.get('market_data_operating_pending_paper_execution_count')}` protective `{final_tick.get('market_data_operating_protective_candidate_count')}` ticketing `{final_tick.get('market_data_operating_protective_ticketing_allowed')}`",
        f"- Capital-aware gate: capital `{final_tick.get('capital_margin_status') or '-'}` leverage `{final_tick.get('capital_leverage_mode') or '-'}` ready/resized/blocked `{final_tick.get('capital_aware_ready_count')}`/`{final_tick.get('capital_aware_resized_count')}`/`{final_tick.get('capital_aware_blocked_count')}` cash/options budget `{final_tick.get('capital_cash_budget')}`/`{final_tick.get('capital_options_premium_budget')}` max withdrawal `{final_tick.get('capital_max_safe_withdrawal')}`",
        f"- Defensive action plan: actions `{final_tick.get('operating_defensive_action_count')}` priority `{final_tick.get('operating_defensive_highest_priority_score')}` chain/what-if `{final_tick.get('operating_defensive_requires_option_chain_count')}` / `{final_tick.get('operating_defensive_requires_margin_whatif_count')}`",
        f"- Tick log JSONL: `{result.get('tick_log_jsonl_path') or '-'}`",
        f"- Pre-snapshot refresh: `{result.get('pre_snapshot_refresh_enabled')}` / ticks `{result.get('pre_snapshot_refresh_count')}`",
        f"- Alerts enabled: `{result.get('alerts_enabled')}` / emitted `{result.get('alert_event_count')}`",
        f"- Alert state JSON: `{result.get('alert_state_json_path') or '-'}`",
        f"- Alert events JSONL: `{result.get('alert_events_jsonl_path') or '-'}`",
        f"- Telegram enabled: `{result.get('telegram_enabled')}` / deliveries `{result.get('telegram_delivery_count')}` / dry-run `{result.get('telegram_dry_run')}`",
        f"- Telegram delivery JSONL: `{result.get('telegram_delivery_jsonl_path') or '-'}`",
        "- Safety: read-only dashboard loop; no broker order submission or transmit.",
        "",
        "## Ticks",
        "",
        "| Tick | Status | Reasons | Risk | AI stance | Operating gate | Capital gate | Action queue | Recovery plan | Executor | Post-val | Evidence refresh | Buying power | Pending | News | Regimes | Alerts | Elapsed s |",
        "| ---: | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in result.get("tick_summaries") or []:
        pre_refresh = row.get("pre_snapshot_refresh") or {}
        pre_refresh_label = (
            f"{pre_refresh.get('status') or pre_refresh.get('reason') or '-'}"
            f"/{pre_refresh.get('freshness_status') or '-'}"
            if pre_refresh
            else "-"
        )
        lines.append(
            f"| {row.get('tick_index')} | {row.get('status_color')} | {', '.join(row.get('status_reasons') or []) or '-'} | "
            f"{row.get('risk_state')} | {row.get('ai_stance')} | "
            f"{row.get('market_data_operating_mode') or '-'}:{row.get('market_data_operating_auto_ready_count')} "
            f"/ protective {row.get('market_data_operating_protective_candidate_count')} | "
            f"{row.get('capital_leverage_mode') or '-'}:{row.get('capital_aware_ready_count')}/{row.get('capital_aware_resized_count')}/{row.get('capital_aware_blocked_count')} | "
            f"{row.get('action_queue_highest_severity') or '-'}:{row.get('action_queue_visible_action_count') or 0} "
            f"`{', '.join(row.get('action_queue_top_action_ids') or []) or '-'}` | "
            f"{row.get('recovery_plan_highest_severity') or '-'}:{row.get('recovery_plan_planned_step_count') or 0} "
            f"`{', '.join(row.get('recovery_plan_top_step_ids') or []) or '-'}` | "
            f"{row.get('recovery_executor_final_status') or '-'}:{row.get('recovery_executor_executed_command_count') or 0}/{row.get('recovery_executor_failure_count') or 0} | "
            f"{row.get('recovery_post_validation_outcome') or '-'}:{row.get('recovery_post_validation_improved_metric_count') or 0}/{row.get('recovery_post_validation_worsened_metric_count') or 0} | "
            f"{pre_refresh_label} | {row.get('buying_power')} | "
            f"{row.get('pending_paper_execution_count')} | {row.get('news_count')} | {row.get('standing_regime_count')} | "
            f"{row.get('alert_count') or 0} | {row.get('elapsed_seconds')} |"
        )
    if not result.get("tick_summaries"):
        lines.append("| - | - | - | - | - | - | - | - | - | - | - | - | - | - | - | - | - | - |")
    if result.get("alert_events"):
        lines.extend(
            [
                "",
                "## Alert events",
                "",
                "| Tick | Type | Severity | Condition | Message |",
                "| ---: | --- | --- | --- | --- |",
            ]
        )
        for alert in result.get("alert_events") or []:
            lines.append(
                f"| {alert.get('tick_index')} | {alert.get('event_type')} | {alert.get('severity')} | "
                f"`{alert.get('condition_key')}` | {alert.get('message')} |"
            )
    if result.get("telegram_deliveries"):
        lines.extend(
            [
                "",
                "## Telegram deliveries",
                "",
                "| Tick | Status | Sent | Alert count | Dry run |",
                "| ---: | --- | --- | ---: | --- |",
            ]
        )
        for row in result.get("telegram_deliveries") or []:
            lines.append(
                f"| {row.get('tick_index')} | {row.get('status')} | {row.get('sent')} | "
                f"{row.get('alert_count')} | {row.get('dry_run')} |"
            )
    return "\n".join(lines) + "\n"


def write_trading_dashboard_loop_markdown(output_path: Path, result: dict[str, Any]) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(trading_dashboard_loop_markdown(result), encoding="utf-8")
    return output_path
