from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


MARKET_DATA_RISK_GATE_VERSION = "market-data-risk-gate-v1"

CORE_PROTECTED_MARKET_KEYS = {"VIX", "VXX", "ES", "NQ", "SPY", "QQQ"}

CLOSED_SESSION_REGULAR_DEPENDENT_MOVE_GROUPS = {
    "asia_equity",
    "fx_macro",
    "rates",
    "us_equity_index_etf",
    "volatility",
    "volatility_etf",
}

MARKET_MOVE_OPTION_SYMBOLS = {
    "SPY": ("SPY", "XSP", "SPX", "ES"),
    "ES": ("SPY", "XSP", "SPX", "ES"),
    "QQQ": ("QQQ", "XND", "NDX", "NQ"),
    "NQ": ("QQQ", "XND", "NDX", "NQ"),
    "IWM": ("IWM", "RUT", "RTY"),
    "RTY": ("IWM", "RUT", "RTY"),
    "DIA": ("DIA", "DJX", "YM"),
    "YM": ("DIA", "DJX", "YM"),
    "VIX": ("VIX", "VXX", "UVXY", "VIXY"),
    "VXX": ("VXX", "VIX", "UVXY", "VIXY", "SPY", "QQQ"),
    "UVXY": ("UVXY", "VXX", "VIX", "VIXY", "SPY", "QQQ"),
}

CLOSE_ONLY_PROXY_MAP = {
    "BRENT": ("WTI",),
    "USDCNH": ("USDKRW", "USDJPY", "DXY"),
    "KOSDAQ": ("KOSPI_FUT", "KOSPI", "EWY"),
}


def _payload_result(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not payload:
        return {}
    return payload.get("result") or payload


def _summary_from_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    data = _payload_result(payload)
    return data.get("summary") or data


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _instrument_key(value: Any) -> str:
    return str(value or "").strip().upper()


def _instrument_key_set(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        raw = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        raw = value
    else:
        raw = []
    return {_instrument_key(item) for item in raw if _instrument_key(item)}


def _regular_session_only_set(value: Any) -> set[str]:
    return _instrument_key_set(value)


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def _age_seconds(value: Any, *, now: datetime | None = None) -> float | None:
    parsed = _parse_time(value)
    if parsed is None:
        return None
    now = now or datetime.now(UTC)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return max(0.0, (now - parsed).total_seconds())


def _direction_from_move(item: dict[str, Any]) -> str:
    value = item.get("gap_pct")
    if value is None:
        value = item.get("tick_move_pct")
    try:
        number = float(value or 0.0)
    except (TypeError, ValueError):
        return "flat"
    if number > 0:
        return "up"
    if number < 0:
        return "down"
    return "flat"


def _fresh_market_row(row: dict[str, Any] | None) -> bool:
    if not row:
        return False
    if str(row.get("status") or "").lower() not in {"live", "subscribed"}:
        return False
    if row.get("price") is None:
        return False
    if bool(row.get("delayed")):
        return False
    if bool(row.get("selected_source_is_close")):
        return False
    return True


def _macro_family_priority(families: set[str]) -> int:
    priority = 0
    if families.intersection({"fed_policy", "fomc", "rates"}):
        priority = max(priority, 100)
    if families.intersection({"labor_growth", "labor_slack", "inflation"}):
        priority = max(priority, 90)
    if families.intersection({"growth_activity", "pmi", "consumer"}):
        priority = max(priority, 60)
    if families:
        priority = max(priority, 30)
    return priority


def _first_two_numbers(text: str) -> tuple[float, float] | None:
    matches = re.findall(r"[-+]?\d+(?:\.\d+)?", text.replace(",", ""))
    if len(matches) < 2:
        return None
    try:
        return float(matches[0]), float(matches[1])
    except ValueError:
        return None


def _infer_macro_item_signal(item: dict[str, Any]) -> str | None:
    name = str(item.get("indicator_name") or "").lower()
    headline = str(item.get("headline") or "")
    numbers = _first_two_numbers(headline)
    if numbers is None:
        return None
    actual, estimate = numbers
    lower_is_weak = any(token in name for token in ("payroll", "factory", "orders", "pmi", "sales", "production"))
    lower_is_strong = any(token in name for token in ("jobless", "claims", "unemployment"))
    if lower_is_weak:
        return "weak_dovish" if actual < estimate else "strong_hawkish" if actual > estimate else "neutral"
    if lower_is_strong:
        return "strong_hawkish" if actual < estimate else "weak_dovish" if actual > estimate else "neutral"
    return None


def _macro_cluster_signal(items: list[dict[str, Any]], families: set[str]) -> str | None:
    weak = 0
    strong = 0
    for item in items:
        signal = _infer_macro_item_signal(item)
        weight = 3 if "nonfarm payroll" in str(item.get("indicator_name") or "").lower() else 1
        if signal == "weak_dovish":
            weak += weight
        elif signal == "strong_hawkish":
            strong += weight
    if weak > strong:
        return "growth weak / rates dovish"
    if strong > weak:
        return "growth strong / rates hawkish"
    if "inflation" in families:
        return "inflation mixed / rates uncertain"
    return None


def _latest_macro_event_window(macro_release_interpreter_payload: dict[str, Any] | None, *, now: datetime | None = None, window_minutes: float = 180.0) -> dict[str, Any]:
    data = _payload_result(macro_release_interpreter_payload)
    if not data:
        return {
            "available": False,
            "active": False,
            "reason": "macro_release_interpreter_missing",
        }
    now = now or datetime.now(UTC)
    clusters = data.get("clusters") if isinstance(data.get("clusters"), list) else []
    latest_item: dict[str, Any] | None = None
    latest_at: datetime | None = None
    families: set[str] = set()
    indicators: list[str] = []
    cluster_drivers: list[dict[str, Any]] = []
    for cluster in clusters:
        if not isinstance(cluster, dict):
            continue
        cluster_families = {str(item) for item in ((cluster.get("families") or {}).keys())}
        cluster_items = [item for item in (cluster.get("items") or []) if isinstance(item, dict)]
        cluster_latest_at: datetime | None = None
        for family in (cluster.get("families") or {}).keys():
            families.add(str(family))
        for item in cluster_items:
            published = _parse_time(item.get("published_at"))
            if published is None:
                continue
            if latest_at is None or published > latest_at:
                latest_at = published
                latest_item = item
            if cluster_latest_at is None or published > cluster_latest_at:
                cluster_latest_at = published
            name = str(item.get("indicator_name") or "").strip()
            if name and name not in indicators:
                indicators.append(name)
        if cluster_items:
            signal = _macro_cluster_signal(cluster_items, cluster_families)
            cluster_drivers.append(
                {
                    "families": sorted(cluster_families),
                    "indicator_names": [str(item.get("indicator_name") or "").strip() for item in cluster_items if str(item.get("indicator_name") or "").strip()][:12],
                    "item_count": len(cluster_items),
                    "latest_event_at": cluster_latest_at.isoformat() if cluster_latest_at else None,
                    "priority": _macro_family_priority(cluster_families),
                    "inferred_macro_surprise_label": signal,
                    "sample_headlines": [str(item.get("headline") or "").strip() for item in cluster_items[:3] if str(item.get("headline") or "").strip()],
                }
            )
    if latest_at is None:
        return {
            "available": True,
            "active": False,
            "reason": "macro_release_time_missing",
            "summary": data.get("summary") or {},
        }
    age_minutes = max(0.0, (now - latest_at).total_seconds() / 60.0)
    summary = data.get("summary") or {}
    latest_packet = (data.get("scorer_evidence_packet") or {}).get("latest_macro_release") or {}
    asset_bias = latest_packet.get("asset_bias") or {}
    cluster_drivers.sort(key=lambda item: (int(item.get("priority") or 0), int(item.get("item_count") or 0), str(item.get("latest_event_at") or "")), reverse=True)
    dominant_driver = cluster_drivers[0] if cluster_drivers else {}
    dominant_label = dominant_driver.get("inferred_macro_surprise_label") or summary.get("latest_macro_surprise_label") or latest_packet.get("macro_surprise_label")
    rates_sensitive = bool(
        {"labor_growth", "labor_slack", "inflation", "fed_policy", "rates"}.intersection(families)
        or any(key in asset_bias for key in ("TLT", "UUP", "GLD", "VXX"))
    )
    active = bool(age_minutes <= float(window_minutes) and rates_sensitive)
    return {
        "available": True,
        "active": active,
        "window_minutes": float(window_minutes),
        "age_minutes": round(age_minutes, 3),
        "latest_event_at": latest_at.isoformat(),
        "latest_indicator": (latest_item or {}).get("indicator_name"),
        "latest_headline": (latest_item or {}).get("headline"),
        "latest_macro_surprise_label": summary.get("latest_macro_surprise_label") or latest_packet.get("macro_surprise_label"),
        "dominant_macro_surprise_label": dominant_label,
        "dominant_macro_driver": dominant_driver,
        "secondary_macro_drivers": cluster_drivers[1:4],
        "asset_bias": asset_bias,
        "families": sorted(families),
        "indicator_names": indicators[:12],
        "rates_sensitive": rates_sensitive,
        "reason": "active_rates_sensitive_macro_window" if active else "outside_rates_sensitive_macro_window",
    }


def _expected_macro_direction(key: str, group: str, macro_event_window: dict[str, Any]) -> str | None:
    key = _instrument_key(key)
    group = str(group or "").lower()
    label = str(macro_event_window.get("dominant_macro_surprise_label") or macro_event_window.get("latest_macro_surprise_label") or "").lower()
    asset_bias = macro_event_window.get("asset_bias") if isinstance(macro_event_window.get("asset_bias"), dict) else {}
    direct_bias = str(asset_bias.get(key) or "").lower()
    if direct_bias in {"positive", "bullish", "up"}:
        return "up"
    if direct_bias in {"negative", "bearish", "down"}:
        return "down"
    dovish = any(token in label for token in ("dovish", "growth weak", "labor weak", "slack", "cool"))
    hawkish = any(token in label for token in ("hawkish", "inflation hot", "growth strong", "labor strong", "hot"))
    if dovish:
        if group == "rates" or key in {"US02Y", "US10Y", "US30Y", "US03M"}:
            return "down"
        if key in {"DXY", "UUP", "USDJPY"}:
            return "down"
        if key in {"VIX", "VXX", "UVXY", "VVIX"}:
            return "down"
        if key in {"GC", "GLD", "SPY", "QQQ", "ES", "NQ", "IWM", "RTY"}:
            return "up"
    if hawkish:
        if group == "rates" or key in {"US02Y", "US10Y", "US30Y", "US03M"}:
            return "up"
        if key in {"DXY", "UUP", "USDJPY", "VIX", "VXX", "UVXY", "VVIX"}:
            return "up"
        if key in {"GC", "GLD", "SPY", "QQQ", "ES", "NQ", "IWM", "RTY"}:
            return "down"
    return None


def _macro_reaction_assessment(macro_event_window: dict[str, Any], top_moves: list[Any]) -> dict[str, Any]:
    if not macro_event_window.get("active"):
        return {"available": bool(macro_event_window.get("available")), "status": "inactive"}
    aligned: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    unclassified: list[dict[str, Any]] = []
    for item in top_moves:
        if not isinstance(item, dict):
            continue
        key = _instrument_key(item.get("key"))
        if not key:
            continue
        observed = _direction_from_move(item)
        if observed == "flat":
            continue
        expected = _expected_macro_direction(key, str(item.get("group") or ""), macro_event_window)
        row = {
            "key": key,
            "group": item.get("group"),
            "observed_direction": observed,
            "expected_direction": expected,
            "gap_pct": item.get("gap_pct"),
            "alerts": _list(item.get("alerts")),
        }
        if expected is None:
            unclassified.append(row)
        elif expected == observed:
            aligned.append(row)
        else:
            conflicts.append(row)
    if conflicts:
        status = "conflict"
    elif aligned:
        status = "aligned"
    else:
        status = "unclassified"
    return {
        "available": True,
        "status": status,
        "aligned_count": len(aligned),
        "conflict_count": len(conflicts),
        "unclassified_count": len(unclassified),
        "aligned_moves": aligned[:10],
        "conflict_moves": conflicts[:10],
        "unclassified_moves": unclassified[:10],
        "latest_macro_surprise_label": macro_event_window.get("latest_macro_surprise_label"),
        "dominant_macro_surprise_label": macro_event_window.get("dominant_macro_surprise_label"),
        "dominant_macro_driver": macro_event_window.get("dominant_macro_driver") or {},
        "secondary_macro_drivers": macro_event_window.get("secondary_macro_drivers") or [],
    }


def _option_structure_context(
    options_market_structure_payload: dict[str, Any] | None,
    options_market_structure_supervisor_payload: dict[str, Any] | None,
    top_moves: list[Any],
) -> dict[str, Any]:
    structure_data = _payload_result(options_market_structure_payload)
    supervisor_data = _payload_result(options_market_structure_supervisor_payload)
    generated_at = structure_data.get("generated_at") or structure_data.get("source_quote_generated_at")
    age = _age_seconds(generated_at)
    max_age_minutes = _float(((structure_data.get("session_guard") or {}).get("max_snapshot_age_minutes")) or supervisor_data.get("final_effective_max_snapshot_age_minutes") or 30.0)
    fresh = bool(age is not None and age <= max_age_minutes * 60.0)
    lookup: dict[str, dict[str, Any]] = {}
    for row in structure_data.get("symbols") or []:
        if isinstance(row, dict) and _instrument_key(row.get("symbol")):
            lookup[_instrument_key(row.get("symbol"))] = row
    selected_by_family: dict[str, list[str]] = {}
    for family in ((supervisor_data.get("index_option_coverage_matrix") or {}).get("families") or []):
        if isinstance(family, dict):
            selected_by_family[str(family.get("family_id") or "")] = [_instrument_key(item) for item in family.get("selected_symbols") or [] if _instrument_key(item)]
    rows: list[dict[str, Any]] = []
    for item in top_moves:
        if not isinstance(item, dict):
            continue
        key = _instrument_key(item.get("key"))
        if key not in MARKET_MOVE_OPTION_SYMBOLS:
            continue
        candidates = list(MARKET_MOVE_OPTION_SYMBOLS.get(key) or ())
        matched = next((symbol for symbol in candidates if symbol in lookup), None)
        structure = lookup.get(matched or "")
        gamma = str((structure or {}).get("gamma_regime") or "").lower()
        proximity = _list((structure or {}).get("proximity_alerts"))
        status = "missing_option_structure"
        if structure and not fresh:
            status = "stale_option_structure"
        elif structure and "negative_gamma" in gamma:
            status = "trend_amplification_risk"
        elif structure and ("positive_gamma" in gamma or proximity):
            status = "mean_reversion_or_pin_risk"
        elif structure:
            status = "neutral_or_partial_option_context"
        rows.append(
            {
                "market_key": key,
                "matched_option_symbol": matched,
                "status": status,
                "fresh": fresh,
                "gamma_regime": (structure or {}).get("gamma_regime"),
                "call_wall": (structure or {}).get("call_wall"),
                "put_wall": (structure or {}).get("put_wall"),
                "max_pain": (structure or {}).get("max_pain"),
                "proximity_alerts": proximity,
                "gap_pct": item.get("gap_pct"),
                "alerts": _list(item.get("alerts")),
            }
        )
    status_counts: dict[str, int] = {}
    for row in rows:
        status_counts[str(row.get("status"))] = status_counts.get(str(row.get("status")), 0) + 1
    if not structure_data:
        overall = "missing"
    elif not fresh:
        overall = "stale"
    elif any(row.get("status") == "trend_amplification_risk" for row in rows):
        overall = "trend_amplification_risk"
    elif any(row.get("status") == "mean_reversion_or_pin_risk" for row in rows):
        overall = "mean_reversion_or_pin_risk"
    elif rows:
        overall = "partial"
    else:
        overall = "no_mapped_core_moves"
    return {
        "available": bool(structure_data),
        "overall_status": overall,
        "fresh": fresh,
        "age_seconds": round(age, 3) if age is not None else None,
        "max_age_seconds": round(max_age_minutes * 60.0, 3),
        "structure_symbol_count": len(lookup),
        "mapped_move_count": len(rows),
        "status_counts": status_counts,
        "rows": rows[:10],
        "supervisor_family_selected_symbols": selected_by_family,
    }


def _close_only_proxy_status(rows_by_key: dict[str, dict[str, Any]], close_only_instruments: list[Any]) -> dict[str, Any]:
    rows = []
    proxy_available_count = 0
    for raw_key in close_only_instruments:
        key = _instrument_key(raw_key)
        proxies = list(CLOSE_ONLY_PROXY_MAP.get(key) or ())
        fresh = [proxy for proxy in proxies if _fresh_market_row(rows_by_key.get(proxy))]
        if fresh:
            proxy_available_count += 1
        rows.append(
            {
                "key": key,
                "proxy_candidates": proxies,
                "fresh_proxy_members": fresh,
                "status": "proxy_available" if fresh else "reference_only_no_live_proxy",
            }
        )
    return {
        "version": "close-only-proxy-status-v1",
        "close_only_count": len(rows),
        "proxy_available_count": proxy_available_count,
        "all_have_proxy": bool(rows) and proxy_available_count == len(rows),
        "rows": rows,
    }


def _proxy_group_definitions(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    groups = []
    for item in value:
        if not isinstance(item, dict):
            continue
        primary = _instrument_key(item.get("primary") or item.get("key"))
        members = sorted(_instrument_key_set(item.get("members")))
        if not primary or not members:
            continue
        groups.append(
            {
                "primary": primary,
                "members": members,
                "label": item.get("label") or primary,
                "require_all_stale": bool(item.get("require_all_stale", True)),
            }
        )
    return groups


def _row_is_fresh_proxy(row: dict[str, Any] | None, stale_keys: set[str]) -> bool:
    if not row:
        return False
    key = _instrument_key(row.get("key"))
    if key in stale_keys:
        return False
    if str(row.get("status") or "").lower() not in {"live", "subscribed"}:
        return False
    if row.get("price") is None:
        return False
    if bool(row.get("delayed")):
        return False
    if bool(row.get("selected_source_is_close")):
        return False
    if _int(row.get("non_close_price_tick_count")) <= 0:
        return False
    return True


def _clip_multiplier(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _session_context(session_overlay_payload: dict[str, Any] | None) -> dict[str, Any]:
    data = _payload_result(session_overlay_payload)
    if not data:
        return {
            "available": False,
            "session_priority_class": "no_session_overlay",
        }
    summary = data.get("summary") or {}
    monitorability_counts = data.get("monitorability_counts") or summary.get("monitorability_counts") or {}
    monitorable_alert_instruments = data.get("monitorable_alert_instruments") or summary.get("monitorable_alert_instruments") or []
    regular_dependent_alert_instruments = data.get("regular_dependent_alert_instruments") or summary.get("regular_dependent_alert_instruments") or []
    session_data = data.get("session") if isinstance(data.get("session"), dict) else {}
    next_regular_open = data.get("next_regular_open") if isinstance(data.get("next_regular_open"), dict) else {}
    wait_hours = _float_or_none(next_regular_open.get("wait_hours"))
    wait_seconds = _float_or_none(next_regular_open.get("wait_seconds"))
    if wait_hours is None and wait_seconds is not None:
        wait_hours = wait_seconds / 3600.0
    if data.get("is_regular_session") is not None:
        is_regular_session = bool(data.get("is_regular_session"))
    else:
        is_regular_session = bool(session_data.get("is_regular_session"))
    regular_dependent_only = (
        not is_regular_session
        and len(monitorable_alert_instruments) == 0
        and (
            len(regular_dependent_alert_instruments) > 0
            or (int(monitorability_counts.get("monitorable_now") or 0) == 0 and int(monitorability_counts.get("monitor_if_fresh_price_present") or 0) > 0)
        )
    )
    session_priority_class = "monitorable_now_stress"
    if regular_dependent_only:
        session_priority_class = "regular_dependent_after_hours_context"
    elif not is_regular_session:
        session_priority_class = "overnight_monitoring_active"
    return {
        "available": True,
        "session_name": data.get("session_name") or session_data.get("session"),
        "is_regular_session": is_regular_session,
        "risk_interpretation": data.get("risk_interpretation"),
        "stock_cash_overnight_monitoring_enabled": data.get("stock_cash_overnight_monitoring_enabled"),
        "option_quotes_regular_session_dependent": data.get("option_quotes_regular_session_dependent"),
        "monitorability_counts": monitorability_counts,
        "monitorable_alert_instruments": monitorable_alert_instruments,
        "regular_dependent_alert_instruments": regular_dependent_alert_instruments,
        "session_priority_class": session_priority_class,
        "next_regular_open": next_regular_open,
        "next_regular_open_wait_hours": round(wait_hours, 6) if wait_hours is not None else None,
    }


def _refresh_stale_interpretation(
    stale_instrument_keys: list[str],
    stale_detail_rows: list[dict[str, Any]],
    *,
    hard_block_stale: list[str],
    monitoring_only_stale: list[str],
) -> dict[str, Any]:
    hard_set = set(hard_block_stale)
    monitoring_set = set(monitoring_only_stale)
    for row in stale_detail_rows:
        key = _instrument_key(row.get("key"))
        row["hard_block"] = key in hard_set
        row["monitoring_only"] = key in monitoring_set
        row["trade_blocking"] = key in hard_set
    return {
        "hard_block_count": len(hard_block_stale),
        "monitoring_only_count": len(monitoring_only_stale),
        "trade_blocking": bool(hard_block_stale),
        "summary": "core stale blocks new entries"
        if hard_block_stale
        else "reference stale, not trade-blocking"
        if monitoring_only_stale
        else "no stale instruments",
        "rows": stale_detail_rows,
        "stale_instrument_count": len(stale_instrument_keys),
    }


def _closed_session_expected_stale_context(
    session_context: dict[str, Any],
    counts: dict[str, int],
    instruments: dict[str, list[Any]],
    top_moves: list[Any],
    *,
    hard_block_stale: list[str],
    min_wait_hours: float,
) -> dict[str, Any]:
    """Identify holiday/weekend/full-closed cash-session stale as expected context.

    This is deliberately not used during ordinary liquid sessions. It only
    downgrades stale/close-only diagnostics when the next regular cash session is
    far enough away and no missing/error/warming-up provider problem exists.
    """

    wait_hours = _float_or_none(session_context.get("next_regular_open_wait_hours"))
    session_name = str(session_context.get("session_name") or "").lower()
    risk_interpretation = str(session_context.get("risk_interpretation") or "").lower()
    non_regular = bool(session_context.get("available")) and not bool(session_context.get("is_regular_session"))
    provider_problem = bool(
        counts.get("delayed_count", 0) > 0
        or counts.get("missing_price_count", 0) > 0
        or counts.get("warming_up_count", 0) > 0
        or counts.get("error_count", 0) > 0
    )
    long_wait = bool(wait_hours is not None and wait_hours >= float(min_wait_hours))
    explicit_closed = session_name in {"closed", "holiday", "weekend"} or "holiday" in risk_interpretation or "closed" in risk_interpretation
    active = bool(non_regular and not provider_problem and (long_wait or explicit_closed))
    stale_keys = sorted({_instrument_key(item) for item in hard_block_stale if _instrument_key(item)})
    close_only_keys = sorted({_instrument_key(item) for item in instruments.get("close_only") or [] if _instrument_key(item)})
    gap_alert_keys = sorted(
        {
            _instrument_key(item.get("key"))
            for item in top_moves
            if isinstance(item, dict)
            and _instrument_key(item.get("key"))
            and _list(item.get("alerts"))
            and str(item.get("group") or "").lower() in CLOSED_SESSION_REGULAR_DEPENDENT_MOVE_GROUPS
        }
    )
    return {
        "version": "closed-session-expected-market-data-v1",
        "active": active,
        "reason": "closed_session_expected_stale_until_next_regular_open" if active else "not_closed_session_expected_stale",
        "session_name": session_name or None,
        "risk_interpretation": session_context.get("risk_interpretation"),
        "next_regular_open_wait_hours": round(wait_hours, 6) if wait_hours is not None else None,
        "min_wait_hours": float(min_wait_hours),
        "provider_problem": provider_problem,
        "downgraded_stale_hard_block_instruments": stale_keys if active else [],
        "downgraded_close_only_instruments": close_only_keys if active else [],
        "downgraded_gap_alert_instruments": gap_alert_keys if active else [],
        "policy": "downgrade stale/close-only/gap cash-session artifacts to monitoring-only while separate session gates keep trading closed",
    }




def _stale_proxy_monitoring_context(
    data: dict[str, Any],
    summary: dict[str, Any],
    instruments: dict[str, list[Any]],
    session_context: dict[str, Any],
    *,
    stale_hard_block_instruments: tuple[str, ...] | list[str] | set[str] | str | None,
    monitoring_only_instruments: tuple[str, ...] | list[str] | set[str] | str | None,
    regular_session_only_stale_hard_block_instruments: tuple[str, ...] | list[str] | set[str] | str | None,
    stale_hard_block_proxy_groups: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    stale_hard_block_set = _instrument_key_set(stale_hard_block_instruments)
    proxy_groups = _proxy_group_definitions(stale_hard_block_proxy_groups)
    configured_monitoring_only_set = _instrument_key_set(monitoring_only_instruments)
    summary_monitoring_only_set = _instrument_key_set(summary.get("monitoring_only_instruments"))
    summary_reference_close_only_set = _instrument_key_set(summary.get("reference_close_only_instruments"))
    regular_session_only_stale_hard_block_set = _regular_session_only_set(regular_session_only_stale_hard_block_instruments)
    session_is_regular = bool(session_context.get("is_regular_session"))
    session_downgraded_stale_hard_blocks: list[str] = []
    effective_stale_hard_block_set = set(stale_hard_block_set)
    monitoring_only_set = set(configured_monitoring_only_set) | summary_monitoring_only_set | summary_reference_close_only_set
    if regular_session_only_stale_hard_block_set and session_context.get("available") and not session_is_regular:
        session_downgraded = sorted(effective_stale_hard_block_set & regular_session_only_stale_hard_block_set)
        session_downgraded_stale_hard_blocks = session_downgraded
        effective_stale_hard_block_set -= regular_session_only_stale_hard_block_set
        monitoring_only_set |= regular_session_only_stale_hard_block_set
    monitoring_only_set -= CORE_PROTECTED_MARKET_KEYS
    stale_instrument_keys = [_instrument_key(item) for item in instruments["stale"] if _instrument_key(item)]
    stale_key_set = set(stale_instrument_keys)
    if effective_stale_hard_block_set:
        hard_block_stale = [item for item in stale_instrument_keys if item in effective_stale_hard_block_set]
        monitoring_only_stale = [item for item in stale_instrument_keys if item not in effective_stale_hard_block_set]
    else:
        hard_block_stale = [] if regular_session_only_stale_hard_block_set and not session_is_regular else stale_instrument_keys
        monitoring_only_stale = stale_instrument_keys if regular_session_only_stale_hard_block_set and not session_is_regular else []
    rows = data.get("rows") if isinstance(data.get("rows"), list) else []
    rows_by_key = {_instrument_key(row.get("key")): row for row in rows if isinstance(row, dict) and _instrument_key(row.get("key"))}
    stale_detail_rows = []
    for key in stale_instrument_keys:
        row = rows_by_key.get(key) or {}
        stale_detail_rows.append(
            {
                "key": key,
                "group": row.get("group"),
                "price": row.get("price"),
                "price_source": row.get("price_source"),
                "quote_age_seconds": row.get("quote_age_seconds"),
                "stale_threshold_seconds": row.get("stale_threshold_seconds"),
                "importance": row.get("market_data_importance"),
                "hard_block": key in hard_block_stale,
                "monitoring_only": key in monitoring_only_stale,
                "trade_blocking": key in hard_block_stale,
            }
        )
    stale_interpretation = {
        "hard_block_count": len(hard_block_stale),
        "monitoring_only_count": len(monitoring_only_stale),
        "trade_blocking": bool(hard_block_stale),
        "summary": "core stale blocks new entries"
        if hard_block_stale
        else "reference stale, not trade-blocking"
        if monitoring_only_stale
        else "no stale instruments",
        "rows": stale_detail_rows,
    }
    proxy_group_evaluations = []
    proxy_group_downgraded_stale: list[str] = []
    for group in proxy_groups:
        primary = str(group.get("primary") or "")
        members = [str(item) for item in group.get("members") or []]
        present_members = [member for member in members if member in rows_by_key or member in stale_key_set]
        fresh_members = [member for member in members if _row_is_fresh_proxy(rows_by_key.get(member), stale_key_set)]
        stale_members = [member for member in members if member in stale_key_set]
        primary_stale = primary in hard_block_stale or primary in stale_key_set
        all_present_stale = bool(present_members) and all(member in stale_key_set for member in present_members)
        should_downgrade = bool(primary_stale and fresh_members and group.get("require_all_stale", True))
        if should_downgrade and primary in hard_block_stale:
            hard_block_stale = [item for item in hard_block_stale if item != primary]
            if primary not in monitoring_only_stale:
                monitoring_only_stale.append(primary)
            proxy_group_downgraded_stale.append(primary)
        proxy_group_evaluations.append(
            {
                "label": group.get("label"),
                "primary": primary,
                "members": members,
                "present_members": present_members,
                "fresh_members": fresh_members,
                "stale_members": stale_members,
                "primary_stale": primary_stale,
                "all_present_members_stale": all_present_stale,
                "downgraded_primary_from_hard_block": should_downgrade,
                "policy": "hard_block_only_when_all_present_proxy_members_are_stale",
            }
        )
    return {
        "stale_hard_block_set": stale_hard_block_set,
        "proxy_groups": proxy_groups,
        "configured_monitoring_only_set": configured_monitoring_only_set,
        "summary_monitoring_only_set": summary_monitoring_only_set,
        "summary_reference_close_only_set": summary_reference_close_only_set,
        "regular_session_only_stale_hard_block_set": regular_session_only_stale_hard_block_set,
        "session_is_regular": session_is_regular,
        "session_downgraded_stale_hard_blocks": session_downgraded_stale_hard_blocks,
        "effective_stale_hard_block_set": effective_stale_hard_block_set,
        "monitoring_only_set": monitoring_only_set,
        "stale_instrument_keys": stale_instrument_keys,
        "stale_key_set": stale_key_set,
        "hard_block_stale": hard_block_stale,
        "monitoring_only_stale": monitoring_only_stale,
        "rows": rows,
        "rows_by_key": rows_by_key,
        "stale_detail_rows": stale_detail_rows,
        "stale_interpretation": stale_interpretation,
        "proxy_group_evaluations": proxy_group_evaluations,
        "proxy_group_downgraded_stale": proxy_group_downgraded_stale,
    }


def _market_data_gate_counts(summary: dict[str, Any]) -> dict[str, int]:
    return {
        "row_count": _int(summary.get("row_count")),
        "live_count": _int(summary.get("live_count")),
        "delayed_count": _int(summary.get("delayed_count")),
        "missing_price_count": _int(summary.get("missing_price_count")),
        "warming_up_count": _int(summary.get("warming_up_count")),
        "error_count": _int(summary.get("error_count")),
        "alert_count": _int(summary.get("alert_count")),
        "market_move_alert_count": _int(summary.get("market_move_alert_count")),
        "scored_market_move_alert_count": _int(summary.get("scored_market_move_alert_count")),
        "quality_excluded_market_move_alert_count": _int(summary.get("quality_excluded_market_move_alert_count")),
        "liquidity_alert_count": _int(summary.get("liquidity_alert_count")),
        "data_alert_count": _int(summary.get("data_alert_count")),
        "close_only_count": _int(summary.get("close_only_count")),
        "stale_count": _int(summary.get("stale_count")),
        "wide_spread_count": _int(summary.get("wide_spread_count")),
        "severe_wide_spread_count": _int(summary.get("severe_wide_spread_count")),
    }


def _market_data_gate_instruments(summary: dict[str, Any]) -> dict[str, list[Any]]:
    return {
        "delayed": _list(summary.get("delayed_instruments")),
        "missing": _list(summary.get("missing_instruments")),
        "warming_up": _list(summary.get("warming_up_instruments")),
        "errors": _list(summary.get("error_instruments")),
        "stale": _list(summary.get("stale_instruments")),
        "close_only": _list(summary.get("close_only_instruments")),
        "wide_spread": _list(summary.get("wide_spread_instruments")),
        "severe_wide_spread": _list(summary.get("severe_wide_spread_instruments")),
        "alerted": _list(summary.get("alerted_instruments")),
    }


def _market_data_gate_move_context(
    summary: dict[str, Any],
    *,
    monitoring_only_set: set[str],
    instruments: dict[str, list[Any]],
    macro_event_window: dict[str, Any],
) -> dict[str, Any]:
    top_moves = _list(summary.get("top_moves"))
    primary_alert_moves = [
        item
        for item in top_moves
        if _instrument_key(item.get("key")) and _instrument_key(item.get("key")) not in monitoring_only_set and _list(item.get("alerts"))
    ]
    monitoring_only_alert_moves = [
        item
        for item in top_moves
        if _instrument_key(item.get("key")) in monitoring_only_set and _list(item.get("alerts"))
    ]
    primary_scored_market_moves = [
        item for item in primary_alert_moves if bool(item.get("market_move_score_eligible"))
    ]
    monitoring_only_scored_market_moves = [
        item for item in monitoring_only_alert_moves if bool(item.get("market_move_score_eligible"))
    ]
    primary_quality_excluded_market_moves = [
        item
        for item in primary_alert_moves
        if not bool(item.get("market_move_score_eligible"))
        and any("gap_" in str(alert) or "move" in str(alert) for alert in _list(item.get("alerts")))
    ]
    monitoring_only_quality_excluded_market_moves = [
        item
        for item in monitoring_only_alert_moves
        if not bool(item.get("market_move_score_eligible"))
        and any("gap_" in str(alert) or "move" in str(alert) for alert in _list(item.get("alerts")))
    ]
    macro_expected_groups = {"rates", "fx", "fx_macro", "volatility", "volatility_etf", "commodities"}
    macro_expected_reaction_moves = [
        item
        for item in top_moves
        if macro_event_window.get("active")
        and str(item.get("group") or "").lower() in macro_expected_groups
        and any(str(alert).startswith(("gap_", "sudden_move")) for alert in _list(item.get("alerts")))
    ]
    return {
        "top_moves": top_moves,
        "primary_alert_moves": primary_alert_moves,
        "monitoring_only_alert_moves": monitoring_only_alert_moves,
        "primary_scored_market_moves": primary_scored_market_moves,
        "monitoring_only_scored_market_moves": monitoring_only_scored_market_moves,
        "primary_quality_excluded_market_moves": primary_quality_excluded_market_moves,
        "monitoring_only_quality_excluded_market_moves": monitoring_only_quality_excluded_market_moves,
        "primary_wide_spread": [item for item in (_instrument_key(v) for v in instruments["wide_spread"]) if item and item not in monitoring_only_set],
        "monitoring_only_wide_spread": [item for item in (_instrument_key(v) for v in instruments["wide_spread"]) if item and item in monitoring_only_set],
        "primary_severe_wide_spread": [item for item in (_instrument_key(v) for v in instruments["severe_wide_spread"]) if item and item not in monitoring_only_set],
        "monitoring_only_severe_wide_spread": [item for item in (_instrument_key(v) for v in instruments["severe_wide_spread"]) if item and item in monitoring_only_set],
        "primary_close_only": [item for item in (_instrument_key(v) for v in instruments["close_only"]) if item and item not in monitoring_only_set],
        "monitoring_only_close_only": [item for item in (_instrument_key(v) for v in instruments["close_only"]) if item and item in monitoring_only_set],
        "macro_expected_reaction_moves": macro_expected_reaction_moves,
    }


def _market_data_gate_decision(
    *,
    blocking_reasons: list[str],
    defensive_reasons: list[str],
    haircut_reasons: list[str],
    temperature_label: str,
    temperature_score: float,
    session_adjusted_priority: bool,
    watch_size_multiplier: float,
    hot_size_multiplier: float,
    defensive_size_multiplier: float,
    session_dependent_size_multiplier: float,
) -> dict[str, Any]:
    if blocking_reasons:
        return {
            "gate_status": "block",
            "action": "block_new_entries",
            "allowed": False,
            "alpha_entries_allowed": False,
            "hedge_entries_allowed": False,
            "size_multiplier": 0.0,
            "defensive_entry_size_multiplier": 0.0,
            "option_strategy_bias": "no_new_risk",
        }
    if defensive_reasons:
        return {
            "gate_status": "defensive",
            "action": "defensive_or_hedge_only",
            "allowed": False,
            "alpha_entries_allowed": False,
            "hedge_entries_allowed": True,
            "size_multiplier": 0.0,
            "defensive_entry_size_multiplier": _clip_multiplier(defensive_size_multiplier),
            "option_strategy_bias": "defensive_defined_risk_or_hedge_only",
        }
    if haircut_reasons:
        if session_adjusted_priority:
            size_multiplier = _clip_multiplier(session_dependent_size_multiplier)
        elif temperature_label == "hot" or temperature_score >= 18.0:
            size_multiplier = _clip_multiplier(hot_size_multiplier)
        else:
            size_multiplier = _clip_multiplier(watch_size_multiplier)
        return {
            "gate_status": "watch",
            "action": "allow_with_session_context_haircut" if session_adjusted_priority else "allow_with_market_data_haircut",
            "allowed": True,
            "alpha_entries_allowed": True,
            "hedge_entries_allowed": True,
            "size_multiplier": size_multiplier,
            "defensive_entry_size_multiplier": 1.0,
            "option_strategy_bias": "defined_risk_preferred",
        }
    return {
        "gate_status": "pass",
        "action": "allow_normal",
        "allowed": True,
        "alpha_entries_allowed": True,
        "hedge_entries_allowed": True,
        "size_multiplier": 1.0,
        "defensive_entry_size_multiplier": 1.0,
        "option_strategy_bias": "normal",
    }
def build_market_data_risk_gate_report(
    market_data_payload: dict[str, Any] | None,
    *,
    market_data_session_overlay_payload: dict[str, Any] | None = None,
    block_on_delayed: bool = True,
    block_on_missing: bool = True,
    block_on_warming_up: bool = True,
    block_on_errors: bool = True,
    block_on_stale: bool = True,
    block_on_close_only: bool = False,
    block_on_severe_wide_spread: bool = False,
    watch_size_multiplier: float = 0.70,
    hot_size_multiplier: float = 0.50,
    defensive_size_multiplier: float = 0.0,
    session_dependent_size_multiplier: float = 0.70,
    stress_score_threshold: float = 30.0,
    stale_hard_block_instruments: tuple[str, ...] | list[str] | set[str] | str | None = None,
    monitoring_only_instruments: tuple[str, ...] | list[str] | set[str] | str | None = None,
    regular_session_only_stale_hard_block_instruments: tuple[str, ...] | list[str] | set[str] | str | None = None,
    stale_hard_block_proxy_groups: list[dict[str, Any]] | None = None,
    macro_release_interpreter_payload: dict[str, Any] | None = None,
    options_market_structure_payload: dict[str, Any] | None = None,
    options_market_structure_supervisor_payload: dict[str, Any] | None = None,
    macro_event_window_minutes: float = 180.0,
    max_market_data_source_age_seconds: float = 30.0,
    closed_session_expected_stale_min_wait_hours: float = 24.0,
) -> dict[str, Any]:
    """Convert Market Data Desk health into a deterministic trading action gate.

    The gate is read-only. It turns live market-data quality and market-temperature
    state into actions downstream paper/live controllers can consume before creating
    new entry intents.
    """

    data = _payload_result(market_data_payload)
    summary = _summary_from_payload(market_data_payload)
    generated_at = datetime.now(UTC).isoformat()
    generated_dt = _parse_time(generated_at) or datetime.now(UTC)
    market_data_generated_at = data.get("generated_at")
    market_data_source_age_seconds = _age_seconds(market_data_generated_at, now=generated_dt)
    source_freshness = {
        "market_data_generated_at": market_data_generated_at,
        "risk_gate_generated_at": generated_at,
        "market_data_source_age_seconds": round(market_data_source_age_seconds, 3) if market_data_source_age_seconds is not None else None,
        "max_market_data_source_age_seconds": float(max_market_data_source_age_seconds),
        "status": "fresh"
        if market_data_source_age_seconds is not None and market_data_source_age_seconds <= float(max_market_data_source_age_seconds)
        else "stale_or_unknown",
        "aligned_with_current_market_data": bool(market_data_source_age_seconds is not None and market_data_source_age_seconds <= float(max_market_data_source_age_seconds)),
    }
    market_status = str((data.get("status") or summary.get("status") or "unknown")).lower()
    temperature = data.get("temperature") if isinstance(data.get("temperature"), dict) else {}
    temperature_label = str(temperature.get("label") or summary.get("temperature_label") or "unknown").lower()
    temperature_score = _float(temperature.get("score") if temperature.get("score") is not None else summary.get("temperature_score"))
    session_context = _session_context(market_data_session_overlay_payload)
    macro_event_window = _latest_macro_event_window(macro_release_interpreter_payload, window_minutes=macro_event_window_minutes)

    counts = _market_data_gate_counts(summary)
    instruments = _market_data_gate_instruments(summary)
    stale_context = _stale_proxy_monitoring_context(
        data,
        summary,
        instruments,
        session_context,
        stale_hard_block_instruments=stale_hard_block_instruments,
        monitoring_only_instruments=monitoring_only_instruments,
        regular_session_only_stale_hard_block_instruments=regular_session_only_stale_hard_block_instruments,
        stale_hard_block_proxy_groups=stale_hard_block_proxy_groups,
    )
    stale_hard_block_set = stale_context["stale_hard_block_set"]
    proxy_groups = stale_context["proxy_groups"]
    configured_monitoring_only_set = stale_context["configured_monitoring_only_set"]
    summary_monitoring_only_set = stale_context["summary_monitoring_only_set"]
    summary_reference_close_only_set = stale_context["summary_reference_close_only_set"]
    regular_session_only_stale_hard_block_set = stale_context["regular_session_only_stale_hard_block_set"]
    session_is_regular = stale_context["session_is_regular"]
    session_downgraded_stale_hard_blocks = stale_context["session_downgraded_stale_hard_blocks"]
    effective_stale_hard_block_set = stale_context["effective_stale_hard_block_set"]
    monitoring_only_set = stale_context["monitoring_only_set"]
    stale_instrument_keys = stale_context["stale_instrument_keys"]
    stale_key_set = stale_context["stale_key_set"]
    hard_block_stale = stale_context["hard_block_stale"]
    monitoring_only_stale = stale_context["monitoring_only_stale"]
    rows = stale_context["rows"]
    rows_by_key = stale_context["rows_by_key"]
    stale_detail_rows = stale_context["stale_detail_rows"]
    stale_interpretation = stale_context["stale_interpretation"]
    proxy_group_evaluations = stale_context["proxy_group_evaluations"]
    proxy_group_downgraded_stale = stale_context["proxy_group_downgraded_stale"]
    closed_session_expected_stale = _closed_session_expected_stale_context(
        session_context,
        counts,
        instruments,
        _list(summary.get("top_moves")),
        hard_block_stale=hard_block_stale,
        min_wait_hours=closed_session_expected_stale_min_wait_hours,
    )
    if closed_session_expected_stale.get("active"):
        downgraded_stale = [str(item) for item in closed_session_expected_stale.get("downgraded_stale_hard_block_instruments") or []]
        downgraded_close_only = [str(item) for item in closed_session_expected_stale.get("downgraded_close_only_instruments") or []]
        downgraded_gap_alerts = [str(item) for item in closed_session_expected_stale.get("downgraded_gap_alert_instruments") or []]
        hard_block_stale = [item for item in hard_block_stale if item not in set(downgraded_stale)]
        monitoring_only_stale = sorted(set(monitoring_only_stale) | set(downgraded_stale))
        monitoring_only_set |= set(downgraded_stale) | set(downgraded_close_only) | set(downgraded_gap_alerts)
        stale_interpretation = _refresh_stale_interpretation(
            stale_instrument_keys,
            stale_detail_rows,
            hard_block_stale=hard_block_stale,
            monitoring_only_stale=monitoring_only_stale,
        )
    move_context = _market_data_gate_move_context(
        summary,
        monitoring_only_set=monitoring_only_set,
        instruments=instruments,
        macro_event_window=macro_event_window,
    )
    top_moves = move_context["top_moves"]
    primary_alert_moves = move_context["primary_alert_moves"]
    monitoring_only_alert_moves = move_context["monitoring_only_alert_moves"]
    primary_scored_market_moves = move_context["primary_scored_market_moves"]
    monitoring_only_scored_market_moves = move_context["monitoring_only_scored_market_moves"]
    primary_quality_excluded_market_moves = move_context["primary_quality_excluded_market_moves"]
    monitoring_only_quality_excluded_market_moves = move_context["monitoring_only_quality_excluded_market_moves"]
    primary_wide_spread = move_context["primary_wide_spread"]
    monitoring_only_wide_spread = move_context["monitoring_only_wide_spread"]
    primary_severe_wide_spread = move_context["primary_severe_wide_spread"]
    monitoring_only_severe_wide_spread = move_context["monitoring_only_severe_wide_spread"]
    primary_close_only = move_context["primary_close_only"]
    monitoring_only_close_only = move_context["monitoring_only_close_only"]
    macro_expected_reaction_moves = move_context["macro_expected_reaction_moves"]
    macro_reaction = _macro_reaction_assessment(macro_event_window, top_moves)
    option_structure_context = _option_structure_context(options_market_structure_payload, options_market_structure_supervisor_payload, top_moves)
    close_only_proxy_status = _close_only_proxy_status(rows_by_key, instruments["close_only"])

    blocking_reasons: list[str] = []
    haircut_reasons: list[str] = []
    defensive_reasons: list[str] = []
    monitoring_only_reasons: list[str] = []
    macro_adjusted_priority = False
    closed_session_expected_temperature_only = bool(
        closed_session_expected_stale.get("active")
        and not hard_block_stale
        and not primary_scored_market_moves
        and not primary_quality_excluded_market_moves
        and not primary_wide_spread
        and not primary_severe_wide_spread
        and not primary_close_only
        and counts["delayed_count"] <= 0
        and counts["missing_price_count"] <= 0
        and counts["warming_up_count"] <= 0
        and counts["error_count"] <= 0
    )

    if not data or not summary:
        blocking_reasons.append("market_data_report_missing")
    if counts["row_count"] <= 0:
        blocking_reasons.append("market_data_rows_missing")
    if source_freshness["status"] != "fresh":
        monitoring_only_reasons.append("market_data_risk_gate_source_stale_or_unknown")
    if block_on_delayed and counts["delayed_count"] > 0:
        blocking_reasons.append("delayed_market_data_present")
    if block_on_missing and counts["missing_price_count"] > 0:
        blocking_reasons.append("missing_market_prices")
    if block_on_warming_up and counts["warming_up_count"] > 0:
        blocking_reasons.append("market_data_subscriptions_warming_up")
    if block_on_errors and counts["error_count"] > 0:
        blocking_reasons.append("ibkr_contract_or_subscription_errors")
    if block_on_stale and hard_block_stale:
        blocking_reasons.append("stale_market_data_present")
    elif block_on_stale and monitoring_only_stale:
        haircut_reasons.append("stale_monitoring_only_present")
    if session_downgraded_stale_hard_blocks and monitoring_only_stale:
        monitoring_only_reasons.append("session_aware_stale_hard_blocks_downgraded_after_hours")
    if proxy_group_downgraded_stale:
        monitoring_only_reasons.append("proxy_group_fresh_member_downgraded_stale_hard_block")
    if closed_session_expected_stale.get("active"):
        monitoring_only_reasons.append("closed_session_expected_stale_or_close_only_downgraded")
        if closed_session_expected_stale.get("downgraded_stale_hard_block_instruments") or closed_session_expected_stale.get("downgraded_close_only_instruments"):
            haircut_reasons.append("closed_session_expected_stale_or_close_only_context")
    if macro_event_window.get("active"):
        monitoring_only_reasons.append("macro_event_window_rates_sensitive")
    if macro_expected_reaction_moves:
        monitoring_only_reasons.append("expected_macro_reaction_context")
    if macro_reaction.get("status") == "aligned":
        monitoring_only_reasons.append("macro_reaction_direction_aligned")
    elif macro_reaction.get("status") == "conflict":
        haircut_reasons.append("macro_reaction_direction_conflict")
    if close_only_proxy_status.get("proxy_available_count"):
        monitoring_only_reasons.append("reference_close_only_proxy_available")
    if option_structure_context.get("overall_status") == "trend_amplification_risk":
        haircut_reasons.append("option_structure_trend_amplification_context")
    elif option_structure_context.get("overall_status") == "mean_reversion_or_pin_risk":
        monitoring_only_reasons.append("option_structure_mean_reversion_or_pin_context")
    elif option_structure_context.get("overall_status") in {"missing", "stale"} and primary_alert_moves:
        monitoring_only_reasons.append("option_structure_context_missing_or_stale_for_core_move")
    if block_on_close_only and primary_close_only:
        blocking_reasons.append("close_only_quotes_present")
    elif block_on_close_only and monitoring_only_close_only:
        monitoring_only_reasons.append("monitoring_only_close_only_quotes_present")
    if block_on_severe_wide_spread and counts["severe_wide_spread_count"] > 0:
        blocking_reasons.append("severe_wide_spread_quotes_present")

    if market_status == "red" or temperature_label == "stress" or temperature_score >= float(stress_score_threshold):
        if closed_session_expected_temperature_only:
            monitoring_only_reasons.append("closed_session_market_temperature_expected_deferred")
        else:
            defensive_reasons.append("market_temperature_stress")
    if primary_scored_market_moves:
        haircut_reasons.append("gap_or_sudden_move_alerts")
    elif monitoring_only_scored_market_moves:
        monitoring_only_reasons.append("monitoring_only_gap_or_sudden_move_alerts")
    if primary_quality_excluded_market_moves:
        haircut_reasons.append("quality_excluded_market_move_alerts")
    elif monitoring_only_quality_excluded_market_moves:
        monitoring_only_reasons.append("monitoring_only_quality_excluded_market_move_alerts")
    if primary_wide_spread:
        haircut_reasons.append("wide_spread_quotes_present")
    elif monitoring_only_wide_spread:
        monitoring_only_reasons.append("monitoring_only_wide_spread_quotes_present")
    if primary_close_only:
        haircut_reasons.append("close_only_quotes_present")
    elif monitoring_only_close_only:
        monitoring_only_reasons.append("monitoring_only_close_only_quotes_present")
    if primary_severe_wide_spread:
        haircut_reasons.append("severe_wide_spread_quotes_present")
    elif monitoring_only_severe_wide_spread:
        monitoring_only_reasons.append("monitoring_only_severe_wide_spread_quotes_present")
    if primary_wide_spread or primary_severe_wide_spread:
        haircut_reasons.append("liquidity_alerts_present")
    elif monitoring_only_wide_spread or monitoring_only_severe_wide_spread:
        monitoring_only_reasons.append("monitoring_only_liquidity_alerts_present")
    if (
        market_status == "yellow"
        or temperature_label in {"watch", "hot"}
        or temperature_score >= 8.0
    ) and (haircut_reasons or not monitoring_only_set):
        haircut_reasons.append("market_temperature_watch_or_hot")
    elif market_status == "yellow" or temperature_label in {"watch", "hot"} or temperature_score >= 8.0:
        monitoring_only_reasons.append("monitoring_only_market_temperature_watch_or_hot")

    session_adjusted_priority = False
    if (
        not blocking_reasons
        and session_context.get("session_priority_class") == "regular_dependent_after_hours_context"
        and defensive_reasons == ["market_temperature_stress"]
    ):
        defensive_reasons = []
        if "market_temperature_stress_regular_dependent_after_hours_context" not in haircut_reasons:
            haircut_reasons.append("market_temperature_stress_regular_dependent_after_hours_context")
        session_adjusted_priority = True
    if (
        not blocking_reasons
        and not hard_block_stale
        and not primary_severe_wide_spread
        and macro_event_window.get("active")
        and macro_expected_reaction_moves
        and defensive_reasons == ["market_temperature_stress"]
    ):
        defensive_reasons = []
        if "market_temperature_stress_explained_by_macro_window" not in haircut_reasons:
            haircut_reasons.append("market_temperature_stress_explained_by_macro_window")
        macro_adjusted_priority = True

    decision = _market_data_gate_decision(
        blocking_reasons=blocking_reasons,
        defensive_reasons=defensive_reasons,
        haircut_reasons=haircut_reasons,
        temperature_label=temperature_label,
        temperature_score=temperature_score,
        session_adjusted_priority=session_adjusted_priority,
        watch_size_multiplier=watch_size_multiplier,
        hot_size_multiplier=hot_size_multiplier,
        defensive_size_multiplier=defensive_size_multiplier,
        session_dependent_size_multiplier=session_dependent_size_multiplier,
    )
    gate_status = decision["gate_status"]
    action = decision["action"]
    allowed = decision["allowed"]
    alpha_entries_allowed = decision["alpha_entries_allowed"]
    hedge_entries_allowed = decision["hedge_entries_allowed"]
    size_multiplier = decision["size_multiplier"]
    defensive_entry_size_multiplier = decision["defensive_entry_size_multiplier"]
    option_strategy_bias = decision["option_strategy_bias"]

    recommended_controls: list[str] = []
    if gate_status == "block":
        recommended_controls.append("block ordinary new entries until Market Data Desk refreshes cleanly")
        recommended_controls.append("allow exits/risk-reduction workflows only")
    elif gate_status == "defensive":
        recommended_controls.append("block ordinary alpha entries")
        recommended_controls.append("allow only explicit hedge/defensive strategies after separate risk approval")
    elif gate_status == "watch":
        if session_adjusted_priority:
            recommended_controls.append("apply session-context entry haircut until regular-session confirmation")
            recommended_controls.append("keep overnight-monitorable market-data rows active; do not mute stock/ETF, futures, FX, commodity, or crypto monitoring")
        else:
            recommended_controls.append("apply entry size haircut before paper/live intent creation")
        recommended_controls.append("prefer defined-risk option structures over naked directional exposure")
    else:
        recommended_controls.append("normal entry sizing allowed")

    data_quality_reasons = [
        item
        for item in blocking_reasons + haircut_reasons
        if item
        in {
            "delayed_market_data_present",
            "missing_market_prices",
            "market_data_subscriptions_warming_up",
            "ibkr_contract_or_subscription_errors",
            "stale_market_data_present",
            "stale_monitoring_only_present",
            "closed_session_expected_stale_or_close_only_context",
            "close_only_quotes_present",
        }
    ]
    market_move_reasons = [
        item
        for item in defensive_reasons + haircut_reasons
        if item
        in {
            "market_temperature_stress",
            "gap_or_sudden_move_alerts",
            "market_temperature_watch_or_hot",
            "market_temperature_stress_explained_by_macro_window",
            "quality_excluded_market_move_alerts",
        }
    ]
    liquidity_reasons = [
        item
        for item in blocking_reasons + haircut_reasons
        if item in {"wide_spread_quotes_present", "severe_wide_spread_quotes_present", "liquidity_alerts_present"}
    ]
    macro_context_reasons = [
        item
        for item in monitoring_only_reasons + haircut_reasons
        if item
        in {
            "macro_event_window_rates_sensitive",
            "expected_macro_reaction_context",
            "market_temperature_stress_explained_by_macro_window",
            "macro_reaction_direction_aligned",
            "macro_reaction_direction_conflict",
        }
    ]
    option_context_reasons = [
        item
        for item in monitoring_only_reasons + haircut_reasons
        if item
        in {
            "option_structure_trend_amplification_context",
            "option_structure_mean_reversion_or_pin_context",
            "option_structure_context_missing_or_stale_for_core_move",
        }
    ]
    reason_breakdown = {
        "data_quality": data_quality_reasons,
        "market_move": market_move_reasons,
        "liquidity": liquidity_reasons,
        "macro_context": macro_context_reasons,
        "option_structure": option_context_reasons,
        "monitoring_only": monitoring_only_reasons,
        "blocking": blocking_reasons,
        "defensive": defensive_reasons,
        "haircut": haircut_reasons,
    }
    operator_summary_lines = []
    if not data_quality_reasons and source_freshness["status"] == "fresh":
        operator_summary_lines.append("Data quality: fresh and usable.")
    elif data_quality_reasons == ["stale_monitoring_only_present"] and not hard_block_stale:
        operator_summary_lines.append(f"Data quality: {stale_interpretation['summary']} ({', '.join(monitoring_only_stale) or '-'}).")
    else:
        operator_summary_lines.append(f"Data quality: attention needed ({', '.join(data_quality_reasons or [source_freshness['status']])}).")
    if market_move_reasons:
        primary_keys = [str(item.get("key")) for item in primary_alert_moves[:5]]
        operator_summary_lines.append(f"Market move: {'/'.join(primary_keys) or 'primary market'} is driving the trade haircut.")
    else:
        operator_summary_lines.append("Market move: no primary trade-blocking move pressure.")
    if macro_context_reasons:
        driver = macro_reaction.get("dominant_macro_driver") or {}
        driver_name = ", ".join(driver.get("indicator_names") or []) or macro_event_window.get("latest_indicator") or "latest macro release"
        operator_summary_lines.append(f"Macro context: {macro_reaction.get('status')} using dominant driver {driver_name}.")
    if option_context_reasons:
        operator_summary_lines.append(f"Option structure: {option_structure_context.get('overall_status')}.")
    if monitoring_only_reasons:
        operator_summary_lines.append(f"Monitoring-only: {len(monitoring_only_reasons)} context flags are visible but not hard-blocking.")
    operator_summary_lines.append(f"Action: {action if 'action' in locals() else 'pending'} with size multiplier {locals().get('size_multiplier', '-')}.")

    return {
        "market_data_risk_gate_version": MARKET_DATA_RISK_GATE_VERSION,
        "generated_at": generated_at,
        "gate_status": gate_status,
        "allowed": allowed,
        "new_entries_allowed": allowed,
        "alpha_entries_allowed": alpha_entries_allowed,
        "hedge_entries_allowed": hedge_entries_allowed,
        "exit_or_risk_reduction_allowed": True,
        "action": action,
        "entry_size_multiplier": size_multiplier,
        "defensive_entry_size_multiplier": defensive_entry_size_multiplier,
        "option_strategy_bias": option_strategy_bias,
        "reason": ",".join(blocking_reasons or defensive_reasons or haircut_reasons),
        "blocking_reasons": blocking_reasons,
        "defensive_reasons": defensive_reasons,
        "haircut_reasons": haircut_reasons,
        "monitoring_only_reasons": monitoring_only_reasons,
        "reason_breakdown": reason_breakdown,
        "operator_summary_lines": operator_summary_lines,
        "market_data_session_context": session_context,
        "session_adjusted_priority": session_adjusted_priority,
        "macro_adjusted_priority": macro_adjusted_priority,
        "market_data": {
            "source_version": data.get("market_data_desk_version"),
            "generated_at": data.get("generated_at"),
            "status": market_status,
            "temperature_label": temperature_label,
            "temperature_score": temperature_score,
            "risk_flags": _list(summary.get("risk_flags")),
            "source_freshness": source_freshness,
            "counts": counts,
            "instruments": instruments,
            "hard_block_stale_instruments": hard_block_stale,
            "monitoring_only_stale_instruments": monitoring_only_stale,
            "stale_interpretation": stale_interpretation,
            "closed_session_expected_stale": closed_session_expected_stale,
            "stale_hard_block_instruments": sorted(stale_hard_block_set),
            "effective_stale_hard_block_instruments": sorted(effective_stale_hard_block_set),
            "regular_session_only_stale_hard_block_instruments": sorted(regular_session_only_stale_hard_block_set),
            "session_downgraded_stale_hard_block_instruments": session_downgraded_stale_hard_blocks,
            "proxy_group_downgraded_stale_hard_block_instruments": proxy_group_downgraded_stale,
            "proxy_group_evaluations": proxy_group_evaluations,
            "macro_event_window": macro_event_window,
            "macro_reaction_assessment": macro_reaction,
            "macro_expected_reaction_moves": macro_expected_reaction_moves[:10],
            "option_structure_context": option_structure_context,
            "close_only_proxy_status": close_only_proxy_status,
            "group_summaries": summary.get("group_summaries") or {},
            "top_moves": top_moves[:10],
            "monitoring_only_instruments": sorted(monitoring_only_set),
            "configured_monitoring_only_instruments": sorted(configured_monitoring_only_set),
            "summary_monitoring_only_instruments": sorted(summary_monitoring_only_set),
            "summary_reference_close_only_instruments": sorted(summary_reference_close_only_set),
            "primary_alert_moves": primary_alert_moves[:10],
            "monitoring_only_alert_moves": monitoring_only_alert_moves[:10],
            "primary_wide_spread_instruments": primary_wide_spread,
            "monitoring_only_wide_spread_instruments": monitoring_only_wide_spread,
            "primary_severe_wide_spread_instruments": primary_severe_wide_spread,
            "monitoring_only_severe_wide_spread_instruments": monitoring_only_severe_wide_spread,
            "primary_close_only_instruments": primary_close_only,
            "monitoring_only_close_only_instruments": monitoring_only_close_only,
        },
        "recommended_controls": recommended_controls,
        "parameters": {
            "block_on_delayed": bool(block_on_delayed),
            "block_on_missing": bool(block_on_missing),
            "block_on_warming_up": bool(block_on_warming_up),
            "block_on_errors": bool(block_on_errors),
            "block_on_stale": bool(block_on_stale),
            "block_on_close_only": bool(block_on_close_only),
            "block_on_severe_wide_spread": bool(block_on_severe_wide_spread),
            "watch_size_multiplier": float(watch_size_multiplier),
            "hot_size_multiplier": float(hot_size_multiplier),
            "defensive_size_multiplier": float(defensive_size_multiplier),
            "session_dependent_size_multiplier": float(session_dependent_size_multiplier),
            "stress_score_threshold": float(stress_score_threshold),
            "stale_hard_block_instruments": sorted(stale_hard_block_set),
            "effective_stale_hard_block_instruments": sorted(effective_stale_hard_block_set),
            "regular_session_only_stale_hard_block_instruments": sorted(regular_session_only_stale_hard_block_set),
            "stale_hard_block_proxy_groups": proxy_groups,
            "stale_hard_block_policy": "configured_core_only" if stale_hard_block_set else "legacy_any_stale_blocks",
            "monitoring_only_instruments": sorted(monitoring_only_set),
            "configured_monitoring_only_instruments": sorted(configured_monitoring_only_set),
            "summary_monitoring_only_instruments": sorted(summary_monitoring_only_set),
            "macro_event_window_minutes": float(macro_event_window_minutes),
            "max_market_data_source_age_seconds": float(max_market_data_source_age_seconds),
            "closed_session_expected_stale_min_wait_hours": float(closed_session_expected_stale_min_wait_hours),
        },
        "safety": {
            "read_only_gate": True,
            "ibkr_order_submitted": False,
            "transmit_enabled": False,
            "portfolio_mutated": False,
            "allows_exit_or_risk_reduction": True,
        },
    }


def market_data_risk_gate_markdown(report: dict[str, Any]) -> str:
    market = report.get("market_data") or {}
    counts = market.get("counts") or {}
    instruments = market.get("instruments") or {}
    hard_block_stale = market.get("hard_block_stale_instruments") or []
    monitoring_only_stale = market.get("monitoring_only_stale_instruments") or []
    session = report.get("market_data_session_context") or {}
    source_freshness = market.get("source_freshness") or {}
    macro_reaction = market.get("macro_reaction_assessment") or {}
    option_context = market.get("option_structure_context") or {}
    close_proxy = market.get("close_only_proxy_status") or {}
    lines = [
        "# Market Data Risk Action Gate",
        "",
        f"- Version: `{report.get('market_data_risk_gate_version')}`",
        f"- Generated at: `{report.get('generated_at')}`",
        f"- Gate status: `{report.get('gate_status')}`",
        f"- Action: `{report.get('action')}`",
        f"- New entries allowed: `{report.get('new_entries_allowed')}`",
        f"- Alpha entries allowed: `{report.get('alpha_entries_allowed')}`",
        f"- Hedge entries allowed: `{report.get('hedge_entries_allowed')}`",
        f"- Entry size multiplier: `{report.get('entry_size_multiplier')}`",
        f"- Defensive entry size multiplier: `{report.get('defensive_entry_size_multiplier')}`",
        f"- Option strategy bias: `{report.get('option_strategy_bias')}`",
        f"- Reason: `{report.get('reason') or '-'}`",
        f"- Monitoring-only reasons: `{','.join(str(item) for item in report.get('monitoring_only_reasons') or []) or '-'}`",
        f"- Reason breakdown: `{report.get('reason_breakdown') or {}}`",
        f"- Operator summary: `{' | '.join(str(item) for item in report.get('operator_summary_lines') or []) or '-'}`",
        f"- Session priority class: `{session.get('session_priority_class') or '-'}` adjusted `{report.get('session_adjusted_priority')}`",
        "",
        "## Market data state",
        "",
        f"- Market Data Desk status: `{market.get('status') or '-'}`",
        f"- Temperature: `{market.get('temperature_label') or '-'}` score `{market.get('temperature_score')}`",
        f"- Live/delayed/missing/warming/errors: `{counts.get('live_count')}` / `{counts.get('delayed_count')}` / `{counts.get('missing_price_count')}` / `{counts.get('warming_up_count')}` / `{counts.get('error_count')}`",
        f"- Alerts/scored moves/wide/severe wide/stale/close-only: `{counts.get('alert_count')}` / `{counts.get('scored_market_move_alert_count')}` / `{counts.get('wide_spread_count')}` / `{counts.get('severe_wide_spread_count')}` / `{counts.get('stale_count')}` / `{counts.get('close_only_count')}`",
        f"- Risk flags: `{','.join(market.get('risk_flags') or []) or '-'}`",
        f"- Source freshness: `{source_freshness.get('status') or '-'}` market-data age `{source_freshness.get('market_data_source_age_seconds')}` max `{source_freshness.get('max_market_data_source_age_seconds')}`",
        f"- Macro reaction: `{macro_reaction.get('status') or '-'}` aligned `{macro_reaction.get('aligned_count')}` conflicts `{macro_reaction.get('conflict_count')}`",
        f"- Option structure context: `{option_context.get('overall_status') or '-'}` fresh `{option_context.get('fresh')}` mapped moves `{option_context.get('mapped_move_count')}`",
        f"- Close-only proxy status: proxies `{close_proxy.get('proxy_available_count')}` / close-only `{close_proxy.get('close_only_count')}`",
        f"- Session: `{session.get('session_name') or '-'}` regular `{session.get('is_regular_session')}` monitorable alerts `{','.join(str(item) for item in session.get('monitorable_alert_instruments') or []) or '-'}` regular-dependent alerts `{','.join(str(item) for item in session.get('regular_dependent_alert_instruments') or []) or '-'}`",
        "",
        "## Instruments requiring attention",
        "",
        f"- Delayed: `{','.join(str(item) for item in instruments.get('delayed') or []) or '-'}`",
        f"- Missing: `{','.join(str(item) for item in instruments.get('missing') or []) or '-'}`",
        f"- Warming up: `{','.join(str(item) for item in instruments.get('warming_up') or []) or '-'}`",
        f"- Errors: `{','.join(str(item) for item in instruments.get('errors') or []) or '-'}`",
        f"- Stale: `{','.join(str(item) for item in instruments.get('stale') or []) or '-'}`",
        f"- Stale hard-block / monitoring-only: `{','.join(str(item) for item in hard_block_stale) or '-'}` / `{','.join(str(item) for item in monitoring_only_stale) or '-'}`",
        f"- Severe wide spread: `{','.join(str(item) for item in instruments.get('severe_wide_spread') or []) or '-'}`",
        "",
        "## Controls",
        "",
    ]
    lines.extend(f"- {item}" for item in report.get("recommended_controls") or [])
    lines.extend(
        [
            "",
            "## Safety",
            "",
            "- Read-only action gate; no broker order submission, transmit, or portfolio mutation.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_market_data_risk_gate_json(output_path: Path, report: dict[str, Any]) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return output_path


def write_market_data_risk_gate_markdown(output_path: Path, report: dict[str, Any]) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(market_data_risk_gate_markdown(report), encoding="utf-8")
    return output_path
