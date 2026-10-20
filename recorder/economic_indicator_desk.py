from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from .macro_universe import MacroSeries, metadata_by_series, series_ids_for_universe


ECONOMIC_INDICATOR_DESK_VERSION = "economic-indicator-desk-v1"

DESK_BY_CATEGORY = {
    "policy_rates": "rates_policy",
    "rates_curve": "rates_policy",
    "housing_credit": "rates_policy",
    "credit_risk": "liquidity_credit",
    "liquidity": "liquidity_credit",
    "credit": "liquidity_credit",
    "fx_dollar": "global_fx_commodities",
    "fx": "global_fx_commodities",
    "commodities_energy": "global_fx_commodities",
    "inflation": "inflation",
    "inflation_expectations": "inflation",
    "consumption": "consumer_housing",
    "housing": "consumer_housing",
    "labor": "labor",
    "labor_high_frequency": "labor",
    "labor_manufacturing": "labor",
    "labor_wages": "labor",
    "growth": "growth_activity",
    "manufacturing_industry": "growth_activity",
    "consumer_survey": "consumer_housing",
    "regional_manufacturing_survey": "growth_activity",
    "volatility": "risk_sentiment",
}

FRESHNESS_DAYS_BY_CATEGORY = {
    "policy_rates": 10,
    "rates_curve": 75,
    "credit_risk": 10,
    "volatility": 10,
    "fx_dollar": 10,
    "fx": 10,
    "commodities_energy": 10,
    "labor_high_frequency": 21,
    "housing_credit": 21,
    "liquidity": 45,
    "credit": 45,
    "inflation": 75,
    "inflation_expectations": 75,
    "consumption": 75,
    "labor": 75,
    "labor_manufacturing": 75,
    "labor_wages": 75,
    "manufacturing_industry": 75,
    "consumer_survey": 75,
    "regional_manufacturing_survey": 75,
    "housing": 75,
    "growth": 130,
}


def _parse_date(value: str | None) -> date:
    text = str(value or "").strip()
    if not text:
        return datetime.now(UTC).date()
    return datetime.strptime(text[:10], "%Y-%m-%d").date()


def _finite_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed != parsed or parsed in {float("inf"), float("-inf")}:
        return None
    return parsed


def _pct_change(latest: float | None, previous: float | None) -> float | None:
    if latest is None or previous in (None, 0.0):
        return None
    return (latest - float(previous)) / abs(float(previous))


def _freshness_status(item: MacroSeries, latest_date: str, cutoff: date) -> tuple[int | None, str]:
    if not latest_date:
        return None, "missing"
    try:
        observed = _parse_date(latest_date)
    except ValueError:
        return None, "unknown"
    age = (cutoff - observed).days
    threshold = FRESHNESS_DAYS_BY_CATEGORY.get(item.category, 90)
    if age <= threshold:
        return age, "fresh"
    if age <= threshold * 2:
        return age, "lagging"
    return age, "stale"


def _trend_label(series_id: str, item: MacroSeries, latest: float | None, previous: float | None) -> str:
    if latest is None or previous is None:
        return "insufficient_history"
    delta = latest - previous
    pct = _pct_change(latest, previous)
    abs_pct = abs(pct or 0.0)
    sid = series_id.upper()
    category = item.category
    if abs(delta) < 1e-9:
        return "flat"
    if category in {"policy_rates", "rates_curve", "housing_credit"}:
        if delta >= 0.10:
            return "rates_up"
        if delta <= -0.10:
            return "rates_down"
    if category in {"credit_risk", "volatility"}:
        if pct is not None and pct >= 0.05:
            return "risk_pressure_up"
        if pct is not None and pct <= -0.05:
            return "risk_pressure_down"
    if category in {"commodities_energy", "fx", "fx_dollar"}:
        if abs_pct >= 0.02:
            return "up" if delta > 0 else "down"
    if sid in {"ICSA", "IC4WSA", "CCSA"}:
        if pct is not None and pct >= 0.08:
            return "labor_softening"
        if pct is not None and pct <= -0.08:
            return "labor_tightening"
    if sid == "UNRATE":
        if delta >= 0.20:
            return "labor_softening"
        if delta <= -0.20:
            return "labor_tightening"
    if category in {"consumer_survey", "regional_manufacturing_survey"}:
        if pct is not None and pct >= 0.05:
            return "survey_improving"
        if pct is not None and pct <= -0.05:
            return "survey_weakening"
    if category in {"growth", "manufacturing_industry", "consumption", "housing"}:
        if pct is not None and pct >= 0.02:
            return "activity_improving"
        if pct is not None and pct <= -0.02:
            return "activity_weakening"
    return "up" if delta > 0 else "down"


def _macro_flags(indicators: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_series = {str(row["series_id"]).upper(): row for row in indicators}
    flags: list[dict[str, Any]] = []

    def value(series_id: str) -> float | None:
        return _finite_float((by_series.get(series_id) or {}).get("latest_value"))

    dgs10 = value("DGS10")
    dgs2 = value("DGS2")
    dgs3mo = value("DGS3MO")
    if dgs10 is not None and dgs10 >= 4.5:
        flags.append({"flag": "long_yield_elevated", "severity": "medium", "evidence": f"DGS10={dgs10:.2f}"})
    if dgs2 is not None and dgs10 is not None and dgs2 > dgs10:
        flags.append({"flag": "yield_curve_inverted_2s10s", "severity": "medium", "evidence": f"DGS2={dgs2:.2f} > DGS10={dgs10:.2f}"})
    if dgs3mo is not None and dgs10 is not None and dgs3mo > dgs10:
        flags.append({"flag": "yield_curve_inverted_3m10y", "severity": "high", "evidence": f"DGS3MO={dgs3mo:.2f} > DGS10={dgs10:.2f}"})

    vix = value("VIXCLS")
    if vix is not None and vix >= 25:
        flags.append({"flag": "volatility_elevated", "severity": "high" if vix >= 30 else "medium", "evidence": f"VIXCLS={vix:.2f}"})

    for sid in ("ICSA", "IC4WSA", "CCSA", "UNRATE"):
        row = by_series.get(sid)
        if not row:
            continue
        if row.get("trend_label") == "labor_softening":
            flags.append({"flag": "labor_softening", "severity": "medium", "evidence": f"{sid} latest_vs_previous={row.get('delta')}"})

    mich = value("MICH")
    if mich is not None and mich >= 3.3:
        flags.append({"flag": "inflation_expectations_elevated", "severity": "medium", "evidence": f"MICH={mich:.2f}"})

    for sid in ("DCOILWTICO", "DCOILBRENTEU"):
        row = by_series.get(sid)
        if row and (row.get("pct_change") or 0.0) >= 0.05:
            flags.append({"flag": "energy_price_pressure", "severity": "medium", "evidence": f"{sid} pct_change={row.get('pct_change'):.2%}"})

    dollar = by_series.get("DTWEXBGS")
    if dollar and (dollar.get("pct_change") or 0.0) >= 0.01:
        flags.append({"flag": "dollar_strengthening", "severity": "medium", "evidence": f"DTWEXBGS pct_change={dollar.get('pct_change'):.2%}"})

    return flags[:12]


def _risk_bias(flags: list[dict[str, Any]]) -> str:
    high = sum(1 for item in flags if item.get("severity") == "high")
    medium = sum(1 for item in flags if item.get("severity") == "medium")
    if high or medium >= 3:
        return "macro_risk_off"
    if medium:
        return "macro_mixed"
    return "macro_neutral"


def _scorer_takeaways(flags: list[dict[str, Any]], missing_count: int) -> list[str]:
    if not flags:
        output = ["No strong macro warning flag was detected from the currently stored indicator universe."]
    else:
        output = []
        names = {str(item.get("flag") or "") for item in flags}
        if names & {"yield_curve_inverted_2s10s", "yield_curve_inverted_3m10y", "long_yield_elevated"}:
            output.append("Rates/curve desk is restrictive; up calls need stronger company-specific growth evidence.")
        if "labor_softening" in names:
            output.append("Labor desk is softening; cyclical and high-beta risk should receive a confidence haircut.")
        if "volatility_elevated" in names:
            output.append("Volatility desk is elevated; prefer smaller directional confidence unless evidence is unusually clean.")
        if "energy_price_pressure" in names or "inflation_expectations_elevated" in names:
            output.append("Inflation/energy desk is pressuring financial conditions; check whether good news is offset by rates or margin risk.")
        if "dollar_strengthening" in names:
            output.append("Dollar desk is strengthening; multinational, commodity, and EM-sensitive assets need FX-aware interpretation.")
    if missing_count:
        output.append(f"{missing_count} configured indicator series had no event-window observation; treat those desks as incomplete rather than neutral.")
    return output[:8]


def _indicator_rows_for_series(conn: Any, *, series_id: str, cutoff_date: str, limit: int) -> list[dict[str, Any]]:
    direct_rows = [
        {
            "series_id": str(row["canonical_series_id"]),
            "observation_date": str(row["observation_date"]),
            "value": row["value"],
            "realtime_start": str(row["realtime_start"] or ""),
            "realtime_end": str(row["realtime_end"] or ""),
            "ingestion_version": str(row["ingestion_version"] or ""),
            "provider": str(row["provider"] or ""),
            "native_series_id": str(row["native_series_id"] or ""),
            "unit": str(row["unit"] or ""),
            "frequency": str(row["frequency"] or ""),
            "provider_priority": int(row["provider_priority"] or 50),
        }
        for row in conn.execute(
            """
            SELECT canonical_series_id,provider,native_series_id,observation_date,value,
                   unit,frequency,provider_priority,realtime_start,realtime_end,ingestion_version
            FROM economic_indicator_observations
            WHERE canonical_series_id=? AND observation_date<=?
            ORDER BY observation_date DESC,provider_priority ASC,id DESC
            LIMIT ?
            """,
            (series_id, cutoff_date, limit),
        ).fetchall()
    ]
    fred_rows = [
        {
            "series_id": str(row["series_id"]),
            "observation_date": str(row["observation_date"]),
            "value": row["value"],
            "realtime_start": str(row["realtime_start"] or ""),
            "realtime_end": str(row["realtime_end"] or ""),
            "ingestion_version": str(row["ingestion_version"] or ""),
            "provider": "fred",
            "native_series_id": str(row["series_id"]),
            "unit": "",
            "frequency": "",
            "provider_priority": 40,
        }
        for row in conn.execute(
            """
            SELECT series_id,observation_date,value,realtime_start,realtime_end,ingestion_version
            FROM fred_observations
            WHERE series_id=? AND observation_date<=?
            ORDER BY observation_date DESC,id DESC
            LIMIT ?
            """,
            (series_id, cutoff_date, limit),
        ).fetchall()
    ]
    combined = [*direct_rows, *fred_rows]
    combined.sort(key=lambda row: (str(row["observation_date"]), -int(row["provider_priority"])), reverse=True)
    return combined[:limit]


def build_economic_indicator_desk(
    db: Any,
    *,
    cutoff_date: str | None = None,
    universe: str = "global_core",
    lookback_observations: int = 4,
) -> dict[str, Any]:
    cutoff = _parse_date(cutoff_date)
    series_ids = series_ids_for_universe(universe)
    metadata = metadata_by_series(universe)
    indicators: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    limit = max(2, int(lookback_observations))
    with db.connect() as conn:
        for series_id in series_ids:
            item = metadata[series_id]
            rows = _indicator_rows_for_series(conn, series_id=series_id, cutoff_date=cutoff.isoformat(), limit=limit)
            if not rows:
                missing.append(
                    {
                        "series_id": series_id,
                        "label": item.label,
                        "region": item.region,
                        "category": item.category,
                        "desk": DESK_BY_CATEGORY.get(item.category, "other"),
                        "importance": item.importance,
                        "freshness_note": item.freshness_note,
                    }
                )
                continue
            latest = rows[0]
            previous = rows[1] if len(rows) > 1 else None
            latest_value = _finite_float(latest["value"])
            previous_value = None if previous is None else _finite_float(previous["value"])
            delta = None if latest_value is None or previous_value is None else latest_value - previous_value
            pct = _pct_change(latest_value, previous_value)
            age_days, freshness = _freshness_status(item, str(latest["observation_date"]), cutoff)
            indicators.append(
                {
                    "series_id": series_id,
                    "label": item.label,
                    "region": item.region,
                    "category": item.category,
                    "desk": DESK_BY_CATEGORY.get(item.category, "other"),
                    "source_family": item.source_family,
                    "importance": item.importance,
                    "latest_date": str(latest["observation_date"]),
                    "latest_value": latest_value,
                    "previous_date": "" if previous is None else str(previous["observation_date"]),
                    "previous_value": previous_value,
                    "provider": str(latest.get("provider") or "fred"),
                    "native_series_id": str(latest.get("native_series_id") or series_id),
                    "unit": str(latest.get("unit") or ""),
                    "frequency": str(latest.get("frequency") or ""),
                    "delta": delta,
                    "pct_change": pct,
                    "trend_label": _trend_label(series_id, item, latest_value, previous_value),
                    "freshness_status": freshness,
                    "age_days": age_days,
                    "freshness_note": item.freshness_note,
                    "ingestion_version": str(latest["ingestion_version"] or ""),
                }
            )
    indicators.sort(key=lambda row: (-int(row["importance"]), row["desk"], row["series_id"]))
    category_counter = Counter(str(row["desk"]) for row in indicators)
    freshness_counter = Counter(str(row["freshness_status"]) for row in indicators)
    flags = _macro_flags(indicators)
    missing_count = len(missing)
    return {
        "desk_version": ECONOMIC_INDICATOR_DESK_VERSION,
        "universe": universe,
        "cutoff_date": cutoff.isoformat(),
        "anti_leakage_note": "Only FRED observations with observation_date <= cutoff_date are included.",
        "series_count": len(series_ids),
        "covered_series_count": len(indicators),
        "missing_series_count": missing_count,
        "desk_counts": dict(sorted(category_counter.items())),
        "freshness_counts": dict(sorted(freshness_counter.items())),
        "provider_counts": dict(sorted(Counter(str(row.get("provider") or "") for row in indicators).items())),
        "risk_bias": _risk_bias(flags),
        "macro_flags": flags,
        "scorer_takeaways": _scorer_takeaways(flags, missing_count),
        "indicators": indicators,
        "missing_series": missing,
    }


def compact_economic_indicator_desk(desk: dict[str, Any], *, max_indicators: int = 24) -> dict[str, Any]:
    indicators = list(desk.get("indicators") or [])
    indicators.sort(
        key=lambda row: (
            str(row.get("freshness_status") or "") == "stale",
            -int(row.get("importance") or 0),
            str(row.get("desk") or ""),
            str(row.get("series_id") or ""),
        )
    )
    compact = dict(desk)
    compact["indicators"] = indicators[: max(0, int(max_indicators))]
    compact["indicator_sample_count"] = len(compact["indicators"])
    compact["sample_note"] = "Indicators are importance-ranked compact samples; use economic-indicator-desk CLI for the full report."
    compact["missing_series"] = list(desk.get("missing_series") or [])[:12]
    return compact


def economic_indicator_desk_markdown(desk: dict[str, Any], *, title: str = "Economic Indicator Specialist Desk Report") -> str:
    lines = [
        f"# {title}",
        "",
        f"- Generated at: `{datetime.now(UTC).isoformat()}`",
        f"- Desk version: `{desk['desk_version']}`",
        f"- Universe: `{desk['universe']}`",
        f"- Cutoff date: `{desk['cutoff_date']}`",
        f"- Coverage: `{desk['covered_series_count']}/{desk['series_count']}`",
        f"- Risk bias: `{desk['risk_bias']}`",
        f"- Anti-leakage: {desk['anti_leakage_note']}",
        "",
        "## Macro flags",
        "",
        "| Severity | Flag | Evidence |",
        "| --- | --- | --- |",
    ]
    if not desk["macro_flags"]:
        lines.append("| - | none | - |")
    for flag in desk["macro_flags"]:
        lines.append(f"| {flag.get('severity')} | {flag.get('flag')} | {str(flag.get('evidence') or '').replace('|', '/')} |")

    lines.extend(["", "## Scorer takeaways", ""])
    lines.extend(f"- {item}" for item in desk["scorer_takeaways"])

    lines.extend(
        [
            "",
            "## Desk coverage",
            "",
            "| Desk | Covered series |",
            "| --- | ---: |",
        ]
    )
    for desk_name, count in sorted((desk.get("desk_counts") or {}).items()):
        lines.append(f"| {desk_name} | {count} |")

    lines.extend(
        [
            "",
            "## Indicators",
            "",
            "| Series | Provider | Desk | Region | Latest | Previous | Change | Trend | Freshness | Label |",
            "| --- | --- | --- | --- | ---: | ---: | ---: | --- | --- | --- |",
        ]
    )
    for row in desk["indicators"]:
        pct = row.get("pct_change")
        change = "-" if pct is None else f"{pct:+.2%}"
        latest = "-" if row.get("latest_value") is None else f"{float(row['latest_value']):.4g}"
        previous = "-" if row.get("previous_value") is None else f"{float(row['previous_value']):.4g}"
        lines.append(
            f"| {row['series_id']} | {row.get('provider') or '-'} | {row['desk']} | {row['region']} | {latest} `{row['latest_date']}` "
            f"| {previous} `{row['previous_date'] or '-'}` | {change} | {row['trend_label']} "
            f"| {row['freshness_status']} | {str(row['label']).replace('|', '/')} |"
        )

    lines.extend(
        [
            "",
            "## Missing or stale-source marker series",
            "",
            "| Series | Region | Category | Reason |",
            "| --- | --- | --- | --- |",
        ]
    )
    if not desk["missing_series"]:
        lines.append("| - | - | - | - |")
    for row in desk["missing_series"]:
        lines.append(
            f"| {row['series_id']} | {row['region']} | {row['category']} | {str(row.get('freshness_note') or 'no observation at cutoff').replace('|', '/')} |"
        )
    return "\n".join(lines) + "\n"


def write_economic_indicator_desk_report(output_path: Path, desk: dict[str, Any]) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    suffix = output_path.suffix.lower()
    if suffix == ".json":
        output_path.write_text(json.dumps(desk, indent=2, ensure_ascii=False), encoding="utf-8")
    else:
        output_path.write_text(economic_indicator_desk_markdown(desk), encoding="utf-8")
    return output_path
