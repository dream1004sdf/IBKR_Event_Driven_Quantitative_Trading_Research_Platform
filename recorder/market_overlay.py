from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


MARKET_OVERLAY_VERSION = "market-overlay-v1"
EMERGENCY_BREAKING_SOURCE = "emergency_breaking_news"

CHIP_ASSETS = {
    "NVDA",
    "AMD",
    "AVGO",
    "QCOM",
    "LRCX",
    "TXN",
    "INTC",
    "MU",
    "TSM",
    "SMH",
    "SOXX",
    "QQQ",
}
ENERGY_ASSETS = {"XOM", "CVX", "USO", "XLE", "WTI", "BRENT"}
DEFENSE_ASSETS = {"ITA", "XAR"}
BROAD_RISK_ASSETS = {"SPY", "QQQ", "IWM", "TLT", "GLD", "VIX", "UUP"}

THEME_SEVERITY = {
    "military_escalation": 2.0,
    "energy_supply": 1.8,
    "shipping_chokepoint": 1.6,
    "trade_conflict": 1.4,
    "semiconductor_supply_chain": 1.4,
    "cybersecurity": 1.2,
    "food_supply": 1.1,
    "defense": 1.0,
    "broad_geopolitical": 1.0,
    "market_news": 0.2,
}


def _parse_dt(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def _load_assets(value: Any) -> set[str]:
    if isinstance(value, (list, tuple, set)):
        return {str(item).strip().upper() for item in value if str(item).strip()}
    try:
        parsed = json.loads(str(value or "[]"))
    except json.JSONDecodeError:
        parsed = []
    if not isinstance(parsed, list):
        return set()
    return {str(item).strip().upper() for item in parsed if str(item).strip()}


def overlay_asset_family(symbol: str) -> set[str]:
    symbol = str(symbol or "").strip().upper()
    assets = {symbol} if symbol else set()
    if symbol in CHIP_ASSETS:
        assets.update({"SMH", "SOXX", "QQQ", "NVDA", "TSM"})
    if symbol in ENERGY_ASSETS:
        assets.update({"USO", "XLE", "XOM", "CVX", "VIX"})
    if symbol in DEFENSE_ASSETS:
        assets.update({"ITA", "XAR", "SPY"})
    if symbol in {"SPY", "QQQ", "IWM", "TLT", "GLD", "VIX", "UUP"}:
        assets.update(BROAD_RISK_ASSETS)
    assets.update({"SPY", "QQQ"})
    return assets


def _sentiment_edge(signal: dict[str, Any]) -> float:
    value = signal.get("sentiment_score")
    if value is None:
        label = str(signal.get("sentiment_label") or "").lower()
        if label == "negative":
            return 0.6
        if label == "positive":
            return 0.35
        return 0.0
    return abs(float(value))


def _technical_confirmation(signal: dict[str, Any]) -> tuple[bool, list[dict[str, Any]]]:
    try:
        payload = json.loads(str(signal.get("market_reaction_json") or "{}"))
    except json.JSONDecodeError:
        payload = {}
    indicators = payload.get("technical_indicators") if isinstance(payload, dict) else []
    if not isinstance(indicators, list):
        indicators = []
    compact = []
    for item in indicators[:5]:
        if not isinstance(item, dict):
            continue
        compact.append(
            {
                "symbol": str(item.get("symbol") or ""),
                "as_of_date": str(item.get("as_of_date") or ""),
                "return_1d": item.get("return_1d"),
                "return_5d": item.get("return_5d"),
                "volatility_20": item.get("volatility_20"),
            }
        )
    return bool(compact), compact


def _signal_raw_payload(signal: dict[str, Any]) -> dict[str, Any]:
    raw = signal.get("raw_json") or signal.get("raw")
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(str(raw or "{}"))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def score_overlay_signal(row: dict[str, Any], signal: dict[str, Any]) -> dict[str, Any] | None:
    signal_time = _parse_dt(signal.get("published_at"))
    event_time = _parse_dt(row.get("replay_timestamp") or row.get("prediction_cutoff_at"))
    if signal_time is None or event_time is None or signal_time > event_time:
        return None

    symbol = str(row.get("symbol") or row.get("actual_subject_symbol") or "").upper()
    target_assets = overlay_asset_family(symbol)
    signal_assets = _load_assets(signal.get("assets_json"))
    direct_overlap = sorted(target_assets.intersection(signal_assets))
    specific_overlap = sorted(set(direct_overlap) - BROAD_RISK_ASSETS)
    theme = str(signal.get("theme") or "unknown")
    region = str(signal.get("region") or "unknown")
    is_geopolitical = theme not in {"market_news", "unknown"} or region not in {"global", "unknown"}
    broad_overlap = bool(signal_assets.intersection(BROAD_RISK_ASSETS))
    if not direct_overlap and not (is_geopolitical and broad_overlap):
        return None

    age_hours = max(0.0, (event_time - signal_time).total_seconds() / 3600.0)
    if age_hours > 96.0:
        return None

    confidence = float(signal.get("confidence") or 0.0)
    sentiment_edge = _sentiment_edge(signal)
    market_confirmed, technical_context = _technical_confirmation(signal)
    source = str(signal.get("source") or "")
    raw_payload = _signal_raw_payload(signal)
    urgency_payload = raw_payload.get("urgency") if isinstance(raw_payload.get("urgency"), dict) else {}
    emergency = source == EMERGENCY_BREAKING_SOURCE or raw_payload.get("emergency") is True
    urgency = str(urgency_payload.get("urgency") or "unknown")
    prediction_gate_status = str(signal.get("prediction_gate_status") or "")
    severity = THEME_SEVERITY.get(theme, 0.5)
    if region not in {"global", "unknown"}:
        severity += 0.8
    if specific_overlap:
        severity += 1.2
    elif direct_overlap:
        severity += 0.6
    elif broad_overlap:
        severity += 0.5
    if confidence >= 0.70:
        severity += 0.5
    elif confidence < 0.50:
        severity -= 0.5
    if sentiment_edge >= 0.35:
        severity += 0.6
    if prediction_gate_status == "eligible":
        severity += 0.4
    elif prediction_gate_status == "blocked":
        severity -= 0.5
    if market_confirmed:
        severity += 0.5
    if emergency:
        severity += 1.0
        if urgency == "critical":
            severity += 1.4
        elif urgency == "high":
            severity += 0.9
        elif urgency == "medium":
            severity += 0.4
    if age_hours <= 24.0:
        severity += 0.7
    elif age_hours <= 48.0:
        severity += 0.3
    if emergency and age_hours <= 1.0:
        severity += 0.8
    elif emergency and age_hours <= 4.0:
        severity += 0.4
    if source == "alpha_vantage" and theme == "market_news":
        severity -= 0.4

    overlay_state = "normal"
    if severity >= 5.0 and specific_overlap:
        overlay_state = "asset_specific_shock"
    elif severity >= 4.2:
        overlay_state = "risk_off"
    elif severity >= 2.6:
        overlay_state = "caution"

    overlay_action = "none"
    if overlay_state == "asset_specific_shock":
        overlay_action = "risk_reduce"
    elif overlay_state == "risk_off":
        overlay_action = "risk_reduce"
    elif overlay_state == "caution":
        overlay_action = "caution"

    return {
        "overlay_version": MARKET_OVERLAY_VERSION,
        "overlay_state": overlay_state,
        "overlay_action": overlay_action,
        "overlay_score": round(severity, 3),
        "source": source,
        "published_at": signal_time.isoformat(),
        "age_hours": round(age_hours, 2),
        "region": region,
        "theme": theme,
        "sentiment_label": str(signal.get("sentiment_label") or "unknown"),
        "sentiment_score": signal.get("sentiment_score"),
        "confidence": confidence,
        "prediction_gate_status": prediction_gate_status,
        "asset_overlap": direct_overlap,
        "specific_asset_overlap": specific_overlap,
        "broad_overlap": broad_overlap,
        "market_confirmed": market_confirmed,
        "technical_context": technical_context,
        "emergency": emergency,
        "urgency": urgency if emergency else "",
        "latency_policy": str(raw_payload.get("latency_policy") or ""),
        "system_action": str(raw_payload.get("system_action") or ""),
        "title": str(signal.get("title") or "")[:220],
    }


def overlay_for_score(
    row: dict[str, Any],
    signals: list[dict[str, Any]],
) -> dict[str, Any]:
    candidates = [item for item in (score_overlay_signal(row, signal) for signal in signals) if item is not None]
    candidates.sort(key=lambda item: (float(item["overlay_score"]), -float(item["age_hours"])), reverse=True)
    if not candidates:
        return {
            "overlay_version": MARKET_OVERLAY_VERSION,
            "overlay_state": "normal",
            "overlay_action": "none",
            "overlay_score": 0.0,
            "matched_signal_count": 0,
            "top_signals": [],
        }
    top = candidates[0]
    return {
        **top,
        "matched_signal_count": len(candidates),
        "top_signals": candidates[:3],
    }


def _signed_return(row: dict[str, Any], key: str) -> float | None:
    direction = str(row.get("predicted_direction") or "").lower()
    if direction not in {"up", "down"}:
        return None
    value = row.get(key)
    if value is None:
        return None
    realized = float(value)
    return realized if direction == "up" else -realized


def _group_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    evaluated = [row for row in rows if row.get("direction_hit") is not None]
    directional = [row for row in evaluated if str(row.get("predicted_direction") or "") in {"up", "down"}]
    hits = sum(int(row["direction_hit"]) for row in evaluated)
    directional_hits = sum(int(row["direction_hit"]) for row in directional)
    raw_returns = [value for value in (_signed_return(row, "actual_raw_return") for row in directional) if value is not None]
    excess_returns = [value for value in (_signed_return(row, "actual_excess_return") for row in directional) if value is not None]
    return {
        "rows": len(rows),
        "evaluated": len(evaluated),
        "hits": hits,
        "accuracy": (hits / len(evaluated)) if evaluated else None,
        "directional_rows": len(directional),
        "directional_hits": directional_hits,
        "directional_accuracy": (directional_hits / len(directional)) if directional else None,
        "avg_signed_raw_return": (sum(raw_returns) / len(raw_returns)) if raw_returns else None,
        "avg_signed_excess_return": (sum(excess_returns) / len(excess_returns)) if excess_returns else None,
        "avg_overlay_score": (
            sum(float((row.get("_overlay") or {}).get("overlay_score") or 0.0) for row in rows) / len(rows)
            if rows
            else None
        ),
    }


def _group_rows(rows: list[dict[str, Any]], key_fn: Any, *, min_rows: int = 1) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(str(key_fn(row) or "unknown"), []).append(row)
    output = []
    for bucket, members in groups.items():
        metrics = _group_metrics(members)
        if metrics["rows"] >= min_rows:
            output.append({"bucket": bucket, **metrics})
    output.sort(key=lambda item: (-int(item["rows"]), item["bucket"]))
    return output


def market_overlay_summary(
    rows: list[dict[str, Any]],
    signals: list[dict[str, Any]],
    *,
    min_group_rows: int = 1,
) -> dict[str, Any]:
    enriched = []
    for row in rows:
        clone = dict(row)
        clone["_overlay"] = overlay_for_score(row, signals)
        enriched.append(clone)
    overlay_rows = [row for row in enriched if (row["_overlay"].get("overlay_state") != "normal")]
    risk_rows = [
        row
        for row in enriched
        if row["_overlay"].get("overlay_action") in {"risk_reduce", "observe_only"}
    ]
    no_risk_rows = [row for row in enriched if row not in risk_rows]
    return {
        "rows": len(enriched),
        "signals": len(signals),
        "all_rows": _group_metrics(enriched),
        "overlay_rows": _group_metrics(overlay_rows),
        "risk_overlay_rows": _group_metrics(risk_rows),
        "non_risk_rows": _group_metrics(no_risk_rows),
        "overlay_coverage": (len(overlay_rows) / len(enriched)) if enriched else None,
        "risk_overlay_coverage": (len(risk_rows) / len(enriched)) if enriched else None,
        "by_state": _group_rows(enriched, lambda row: row["_overlay"].get("overlay_state"), min_rows=min_group_rows),
        "by_action": _group_rows(enriched, lambda row: row["_overlay"].get("overlay_action"), min_rows=min_group_rows),
        "by_region": _group_rows(overlay_rows, lambda row: row["_overlay"].get("region"), min_rows=min_group_rows),
        "by_theme": _group_rows(overlay_rows, lambda row: row["_overlay"].get("theme"), min_rows=min_group_rows),
        "top_overlay_rows": sorted(
            overlay_rows,
            key=lambda row: float(row["_overlay"].get("overlay_score") or 0.0),
            reverse=True,
        )[:10],
    }


def _emergency_relation_to_prediction(row: dict[str, Any]) -> str:
    overlay = row.get("_overlay") or {}
    predicted = str(row.get("predicted_direction") or "").lower()
    action = str(overlay.get("overlay_action") or "")
    state = str(overlay.get("overlay_state") or "")
    if not overlay.get("emergency"):
        return "no_emergency"
    if predicted == "up" and action == "risk_reduce":
        return "emergency_conflicts_up"
    if predicted == "down" and action == "risk_reduce":
        return "emergency_confirms_down"
    if predicted == "flat" and action == "risk_reduce":
        return "emergency_conflicts_flat"
    if action == "caution" or state == "caution":
        return "emergency_caution"
    return "emergency_neutral"


def _emergency_age_bucket(row: dict[str, Any]) -> str:
    overlay = row.get("_overlay") or {}
    age = overlay.get("age_hours")
    if age is None:
        return "unknown"
    age_float = float(age)
    if age_float <= 1:
        return "<=1h"
    if age_float <= 4:
        return "1-4h"
    if age_float <= 24:
        return "4-24h"
    if age_float <= 96:
        return "24-96h"
    return ">96h"


def _emergency_gate_candidate(row: dict[str, Any]) -> str:
    overlay = row.get("_overlay") or {}
    relation = _emergency_relation_to_prediction(row)
    urgency = str(overlay.get("urgency") or "")
    score = float(overlay.get("overlay_score") or 0.0)
    age = float(overlay.get("age_hours") or 999.0)
    if relation == "emergency_conflicts_up" and urgency == "critical" and age <= 4 and score >= 5.5:
        return "critical_fresh_conflict_second_pass"
    if relation == "emergency_conflicts_up" and age <= 24 and score >= 4.5:
        return "fresh_conflict_confidence_haircut"
    if relation == "emergency_confirms_down" and age <= 24 and score >= 4.5:
        return "fresh_confirmation_preserve_down"
    if relation == "emergency_caution":
        return "caution_review"
    return "no_gate_change"


def _emergency_raw_method(row: dict[str, Any]) -> str:
    try:
        payload = json.loads(str(row.get("raw_json") or "{}"))
    except json.JSONDecodeError:
        return "unknown"
    debias = payload.get("debias") if isinstance(payload, dict) else {}
    if not isinstance(debias, dict):
        return "unknown"
    return str(debias.get("method") or "unknown")


def emergency_overlay_summary(
    rows: list[dict[str, Any]],
    emergency_signals: list[dict[str, Any]],
    *,
    min_group_rows: int = 1,
) -> dict[str, Any]:
    enriched = []
    for row in rows:
        clone = dict(row)
        clone["_overlay"] = overlay_for_score(row, emergency_signals)
        clone["_emergency_relation"] = _emergency_relation_to_prediction(clone)
        clone["_emergency_age_bucket"] = _emergency_age_bucket(clone)
        clone["_emergency_gate_candidate"] = _emergency_gate_candidate(clone)
        clone["_raw_method"] = _emergency_raw_method(clone)
        enriched.append(clone)
    emergency_rows = [row for row in enriched if (row["_overlay"].get("emergency") is True)]
    conflict_rows = [row for row in emergency_rows if row["_emergency_relation"] == "emergency_conflicts_up"]
    confirmation_rows = [row for row in emergency_rows if row["_emergency_relation"] == "emergency_confirms_down"]
    return {
        "rows": len(enriched),
        "signals": len(emergency_signals),
        "all_rows": _group_metrics(enriched),
        "emergency_rows": _group_metrics(emergency_rows),
        "conflict_rows": _group_metrics(conflict_rows),
        "confirmation_rows": _group_metrics(confirmation_rows),
        "emergency_coverage": (len(emergency_rows) / len(enriched)) if enriched else None,
        "by_relation": _group_rows(emergency_rows, lambda row: row["_emergency_relation"], min_rows=min_group_rows),
        "by_gate_candidate": _group_rows(emergency_rows, lambda row: row["_emergency_gate_candidate"], min_rows=min_group_rows),
        "by_urgency": _group_rows(emergency_rows, lambda row: row["_overlay"].get("urgency"), min_rows=min_group_rows),
        "by_age_bucket": _group_rows(emergency_rows, lambda row: row["_emergency_age_bucket"], min_rows=min_group_rows),
        "by_region": _group_rows(emergency_rows, lambda row: row["_overlay"].get("region"), min_rows=min_group_rows),
        "by_theme": _group_rows(emergency_rows, lambda row: row["_overlay"].get("theme"), min_rows=min_group_rows),
        "by_method": _group_rows(emergency_rows, lambda row: row["_raw_method"], min_rows=min_group_rows),
        "top_emergency_rows": sorted(
            emergency_rows,
            key=lambda row: (
                float(row["_overlay"].get("overlay_score") or 0.0),
                -float(row["_overlay"].get("age_hours") or 999.0),
            ),
            reverse=True,
        )[:12],
    }


def _pct(value: Any) -> str:
    if value is None:
        return "-"
    return f"{float(value):.2%}"


def _signed_pct(value: Any) -> str:
    if value is None:
        return "-"
    return f"{float(value):+.2%}"


def _append_table(lines: list[str], title: str, rows: list[dict[str, Any]]) -> None:
    lines.extend(
        [
            "",
            f"## {title}",
            "",
            "| Bucket | Rows | Directional rows | Directional hits | Directional acc | Avg signed excess | Avg overlay score |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    if not rows:
        lines.append("| - | 0 | 0 | 0 | - | - | - |")
        return
    for row in rows:
        lines.append(
            f"| {row['bucket']} | {row['rows']} | {row['directional_rows']} | {row['directional_hits']} "
            f"| {_pct(row['directional_accuracy'])} | {_signed_pct(row['avg_signed_excess_return'])} "
            f"| {row['avg_overlay_score']:.2f} |"
        )


def emergency_overlay_markdown(
    rows: list[dict[str, Any]],
    emergency_signals: list[dict[str, Any]],
    *,
    filters: dict[str, Any] | None = None,
    min_group_rows: int = 1,
    title: str = "Replay Emergency Breaking-News Overlay Tuning Report",
) -> str:
    summary = emergency_overlay_summary(rows, emergency_signals, min_group_rows=min_group_rows)
    filters = filters or {}
    all_rows = summary["all_rows"]
    emergency_rows = summary["emergency_rows"]
    conflict_rows = summary["conflict_rows"]
    confirmation_rows = summary["confirmation_rows"]
    lines = [
        f"# {title}",
        "",
        f"- Generated at: `{datetime.now(UTC).isoformat()}`",
        f"- Rows: `{summary['rows']}`",
        f"- Emergency signals available: `{summary['signals']}`",
        f"- Emergency overlay coverage: `{_pct(summary['emergency_coverage'])}`",
        f"- All directional accuracy: `{all_rows['directional_hits']}/{all_rows['directional_rows']} = {_pct(all_rows['directional_accuracy'])}`",
        f"- Emergency-row directional accuracy: `{emergency_rows['directional_hits']}/{emergency_rows['directional_rows']} = {_pct(emergency_rows['directional_accuracy'])}`",
        f"- Emergency-conflict directional accuracy: `{conflict_rows['directional_hits']}/{conflict_rows['directional_rows']} = {_pct(conflict_rows['directional_accuracy'])}`",
        f"- Emergency-confirmation directional accuracy: `{confirmation_rows['directional_hits']}/{confirmation_rows['directional_rows']} = {_pct(confirmation_rows['directional_accuracy'])}`",
        "- Anti-leakage: only emergency signals with `published_at` at or before each replay timestamp are eligible.",
        "- Safety: read-only tuning report over stored scores/signals; no OpenAI, IBKR account, portfolio, or order action.",
        "",
        "## Filters",
        "",
    ]
    if filters:
        for key, value in filters.items():
            lines.append(f"- `{key}`: `{value if value is not None else '-'}`")
    else:
        lines.append("- none")
    _append_table(lines, "By emergency relation to prediction", summary["by_relation"])
    _append_table(lines, "By proposed v15 gate candidate", summary["by_gate_candidate"])
    _append_table(lines, "By urgency", summary["by_urgency"])
    _append_table(lines, "By emergency signal age", summary["by_age_bucket"])
    _append_table(lines, "By region", summary["by_region"])
    _append_table(lines, "By theme", summary["by_theme"])
    _append_table(lines, "By stored scorer method", summary["by_method"])
    lines.extend(
        [
            "",
            "## Top emergency overlay rows",
            "",
            "| Symbol | Horizon | Replay timestamp | Pred | Actual | Hit | Relation | Gate candidate | Urgency | Age h | Score | Region | Theme | Signal title |",
            "| --- | --- | --- | --- | --- | ---: | --- | --- | --- | ---: | ---: | --- | --- | --- |",
        ]
    )
    for row in summary["top_emergency_rows"]:
        overlay = row["_overlay"]
        lines.append(
            f"| {row.get('symbol') or ''} | {row.get('horizon') or ''} | {row.get('replay_timestamp') or ''} "
            f"| {row.get('predicted_direction') or ''} | {row.get('actual_direction') or ''} "
            f"| {int(row.get('direction_hit') or 0)} | {row.get('_emergency_relation')} "
            f"| {row.get('_emergency_gate_candidate')} | {overlay.get('urgency')} "
            f"| {float(overlay.get('age_hours') or 0.0):.2f} | {float(overlay.get('overlay_score') or 0.0):.2f} "
            f"| {overlay.get('region')} | {overlay.get('theme')} "
            f"| {str(overlay.get('title') or '').replace('|', '/')[:180]} |"
        )
    lines.extend(
        [
            "",
            "## Interpretation note",
            "",
            "Use this report to tune a future emergency-news gate. A useful gate should isolate a relation or age/urgency bucket with worse directional accuracy or worse signed excess return than the full scorer, while preserving enough coverage to matter.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_emergency_overlay_markdown(
    output_path: Path,
    rows: list[dict[str, Any]],
    emergency_signals: list[dict[str, Any]],
    *,
    filters: dict[str, Any] | None = None,
    min_group_rows: int = 1,
) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        emergency_overlay_markdown(rows, emergency_signals, filters=filters, min_group_rows=min_group_rows),
        encoding="utf-8",
    )
    return output_path


def market_overlay_markdown(
    rows: list[dict[str, Any]],
    signals: list[dict[str, Any]],
    *,
    filters: dict[str, Any] | None = None,
    min_group_rows: int = 1,
    title: str = "Replay Market Overlay Report",
) -> str:
    summary = market_overlay_summary(rows, signals, min_group_rows=min_group_rows)
    filters = filters or {}
    all_rows = summary["all_rows"]
    overlay_rows = summary["overlay_rows"]
    risk_rows = summary["risk_overlay_rows"]
    non_risk = summary["non_risk_rows"]
    lines = [
        f"# {title}",
        "",
        f"- Generated at: `{datetime.now(UTC).isoformat()}`",
        f"- Rows: `{summary['rows']}`",
        f"- External signals available to report: `{summary['signals']}`",
        f"- Overlay coverage: `{_pct(summary['overlay_coverage'])}`",
        f"- Risk-overlay coverage: `{_pct(summary['risk_overlay_coverage'])}`",
        f"- All directional accuracy: `{all_rows['directional_hits']}/{all_rows['directional_rows']} = {_pct(all_rows['directional_accuracy'])}`",
        f"- Overlay directional accuracy: `{overlay_rows['directional_hits']}/{overlay_rows['directional_rows']} = {_pct(overlay_rows['directional_accuracy'])}`",
        f"- Risk-overlay directional accuracy: `{risk_rows['directional_hits']}/{risk_rows['directional_rows']} = {_pct(risk_rows['directional_accuracy'])}`",
        f"- Non-risk directional accuracy: `{non_risk['directional_hits']}/{non_risk['directional_rows']} = {_pct(non_risk['directional_accuracy'])}`",
        "- Anti-leakage: only external signals with `published_at` at or before each replay timestamp are eligible.",
        "- Safety: read-only overlay analysis over stored scores/signals; no OpenAI, IBKR, account, portfolio, or order action.",
        "",
        "## Filters",
        "",
    ]
    if filters:
        for key, value in filters.items():
            lines.append(f"- `{key}`: `{value if value is not None else '-'}`")
    else:
        lines.append("- none")
    _append_table(lines, "By overlay state", summary["by_state"])
    _append_table(lines, "By overlay action", summary["by_action"])
    _append_table(lines, "By region", summary["by_region"])
    _append_table(lines, "By theme", summary["by_theme"])
    lines.extend(
        [
            "",
            "## Top overlay rows",
            "",
            "| Symbol | Horizon | Replay timestamp | Pred | Actual | Hit | State | Action | Score | Region | Theme | Source | Signal title |",
            "| --- | --- | --- | --- | --- | ---: | --- | --- | ---: | --- | --- | --- | --- |",
        ]
    )
    for row in summary["top_overlay_rows"]:
        overlay = row["_overlay"]
        lines.append(
            f"| {row.get('symbol') or ''} | {row.get('horizon') or ''} | {row.get('replay_timestamp') or ''} "
            f"| {row.get('predicted_direction') or ''} | {row.get('actual_direction') or ''} "
            f"| {int(row.get('direction_hit') or 0)} | {overlay.get('overlay_state')} "
            f"| {overlay.get('overlay_action')} | {float(overlay.get('overlay_score') or 0.0):.2f} "
            f"| {overlay.get('region')} | {overlay.get('theme')} | {overlay.get('source')} "
            f"| {str(overlay.get('title') or '').replace('|', '/')[:160]} |"
        )
    lines.extend(
        [
            "",
            "## Interpretation note",
            "",
            "This report evaluates market/regime overlay coverage and correlation with stored replay outcomes. It should guide a future overlay policy, but it should not itself change historical labels or authorize live trades.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_market_overlay_markdown(
    output_path: Path,
    rows: list[dict[str, Any]],
    signals: list[dict[str, Any]],
    *,
    filters: dict[str, Any] | None = None,
    min_group_rows: int = 1,
) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        market_overlay_markdown(rows, signals, filters=filters, min_group_rows=min_group_rows),
        encoding="utf-8",
    )
    return output_path
