from __future__ import annotations

import html
import json
import re
import sqlite3
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .market_outcomes import IntradayBar, _first_intraday_geq, _latest_intraday_leq


MACRO_RELEASE_INTERPRETER_VERSION = "m237-macro-release-interpreter-v1"
MACRO_EVENT_CALENDAR_VERSION = "m237-macro-event-calendar-v1"
EASTERN = ZoneInfo("America/New_York")
LOCAL_TZ = ZoneInfo("Asia/Seoul")
DEFAULT_REACTION_ASSETS = ("SPY", "QQQ", "TLT", "UUP", "GLD", "VXX")
IMPORTANT_RELEASE_PATTERNS = (
    "Employment Situation",
    "Consumer Price Index",
    "Producer Price Index",
    "Job Openings and Labor Turnover",
    "Employment Cost Index",
    "Productivity and Costs",
    "U.S. Import and Export Price Indexes",
)
SOURCE_URLS = {
    "fomc": "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
    "bls": "https://www.bls.gov/schedule/2026/home.htm",
    "ism": "https://www.ismworld.org/supply-management-news-and-reports/reports/rob-report-calendar/",
    "bea_pce": "https://www.bea.gov/data/personal-consumption-expenditures-price-index",
}
MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}


@dataclass(frozen=True)
class MacroReleaseInterpreterConfig:
    database_path: Path = Path("market_events.db")
    output_json_path: Path = Path("exports/m237_macro_release_interpreter.json")
    output_markdown_path: Path = Path("exports/m237_macro_release_interpreter.md")
    lookback_minutes: int = 240
    max_rows: int = 500
    cluster_window_minutes: int = 4
    attach_market_reaction: bool = True
    reaction_horizons: tuple[str, ...] = ("1M", "5M", "15M", "30M")
    reaction_assets: tuple[str, ...] = DEFAULT_REACTION_ASSETS
    flat_return_threshold: float = 0.001


@dataclass(frozen=True)
class MacroEventCalendarConfig:
    output_json_path: Path = Path("exports/m237_macro_event_calendar.json")
    output_markdown_path: Path = Path("exports/m237_macro_event_calendar.md")
    horizon_days: int = 120
    timeout_seconds: float = 12.0
    include_internet_refresh: bool = True


class IntradayPriceSourceLike:
    name: str

    def get_intraday_bars(self, symbol: str, start: datetime, end: datetime) -> list[IntradayBar]:
        raise NotImplementedError


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


def _local_text(value: datetime | str | None = None) -> str:
    dt = _parse_dt(value) if isinstance(value, str) else value
    if dt is None:
        dt = datetime.now(UTC)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S %Z (%z)")


def _read_url(url: str, timeout_seconds: float) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "IBKRMarketEventRecorder/0.14 macro-calendar"})
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        return response.read().decode("utf-8", errors="replace")


def _strip_html(text: str) -> str:
    text = re.sub(r"<script\b.*?</script>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<style\b.*?</style>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", "\n", text)
    text = html.unescape(text)
    return re.sub(r"[ \t\r\f\v]+", " ", text)


def _number(value: str) -> tuple[float | None, str]:
    raw = str(value or "").strip().replace(",", "")
    multiplier = 1.0
    unit = ""
    if raw.endswith("%"):
        unit = "%"
        raw = raw[:-1]
    elif raw.upper().endswith("K"):
        unit = "K"
        multiplier = 1000.0
        raw = raw[:-1]
    elif raw.upper().endswith("M"):
        unit = "M"
        multiplier = 1_000_000.0
        raw = raw[:-1]
    try:
        return float(raw) * multiplier, unit
    except ValueError:
        return None, unit


def _display_number(value: float | None, unit: str) -> str:
    if value is None:
        return "-"
    if unit == "K":
        return f"{value / 1000.0:g}K"
    if unit == "M":
        return f"{value / 1_000_000.0:g}M"
    if unit == "%":
        return f"{value:g}%"
    return f"{value:g}"


def _classify_indicator(name: str) -> dict[str, Any]:
    text = name.lower()
    if any(term in text for term in ("payroll", "employment change", "adp")) and "unemployment" not in text:
        return {"family": "labor_growth", "higher_is": "growth_stronger", "importance": "high"}
    if "unemployment" in text or "jobless claims" in text or "claims" in text:
        return {"family": "labor_slack", "higher_is": "labor_weaker", "importance": "high"}
    if any(term in text for term in ("cpi", "pce", "ppi", "prices", "inflation", "earnings")):
        return {"family": "inflation", "higher_is": "inflation_hotter", "importance": "high"}
    if any(term in text for term in ("ism", "pmi", "retail", "gdp", "construction spending", "factory orders")):
        return {"family": "growth_activity", "higher_is": "growth_stronger", "importance": "medium"}
    return {"family": "macro", "higher_is": "unknown", "importance": "medium"}


def _parse_macro_headline(headline: str, *, provider: str, primary_symbol: str, ts: datetime, source_table: str, source_id: int) -> dict[str, Any] | None:
    clean = re.sub(r"^\{A:[^}]+\}", "", str(headline or "")).strip()
    patterns = [
        re.compile(r"^USA (?P<name>.+?)(?: For (?P<period>.+?))? (?P<actual>[-+]?\d[\d,.]*(?:\.\d+)?[KkMm%]?)\s+V(?:s|S)\.?\s+(?P<expected>[-+]?\d[\d,.]*(?:\.\d+)?[KkMm%]?)\s+Est\.?", re.I),
        re.compile(r"^USA (?P<name>.+?) (?P<actual>[-+]?\d[\d,.]*(?:\.\d+)?[KkMm%]?)\s+V(?:s|S)\s+(?P<expected>[-+]?\d[\d,.]*(?:\.\d+)?[KkMm%]?)\s+Est\.?", re.I),
    ]
    revision = re.search(
        r"^USA (?P<name>.+?)(?: For (?P<period>.+?))? Revises Prior From (?P<prior_from>[-+]?\d[\d,.]*(?:\.\d+)?[KkMm%]?) To (?P<prior_to>[-+]?\d[\d,.]*(?:\.\d+)?[KkMm%]?)",
        clean,
        re.I,
    )
    if revision:
        prior_from, unit = _number(revision.group("prior_from"))
        prior_to, _unit_to = _number(revision.group("prior_to"))
        classification = _classify_indicator(revision.group("name"))
        delta = (prior_to - prior_from) if prior_to is not None and prior_from is not None else None
        return {
            "source_table": source_table,
            "source_id": source_id,
            "provider": provider,
            "primary_symbol": primary_symbol,
            "headline": clean,
            "published_at": ts.isoformat(),
            "published_at_local": _local_text(ts),
            "indicator_name": revision.group("name").strip(),
            "period": (revision.group("period") or "").strip(),
            "event_kind": "prior_revision",
            "prior_from": prior_from,
            "prior_to": prior_to,
            "unit": unit,
            "surprise": delta,
            "surprise_display": _display_number(delta, unit),
            **classification,
        }
    matched = None
    for pattern in patterns:
        matched = pattern.search(clean)
        if matched:
            break
    if not matched:
        return None
    actual, unit = _number(matched.group("actual"))
    expected, expected_unit = _number(matched.group("expected"))
    if not unit:
        unit = expected_unit
    surprise = (actual - expected) if actual is not None and expected is not None else None
    classification = _classify_indicator(matched.group("name"))
    severity = "unknown"
    if surprise is not None and expected not in (None, 0):
        ratio = abs(surprise) / max(abs(float(expected)), 1.0)
        severity = "high" if ratio >= 0.35 else "medium" if ratio >= 0.12 else "low"
    return {
        "source_table": source_table,
        "source_id": source_id,
        "provider": provider,
        "primary_symbol": primary_symbol,
        "headline": clean,
        "published_at": ts.isoformat(),
        "published_at_local": _local_text(ts),
        "indicator_name": matched.group("name").strip(),
        "period": (matched.groupdict().get("period") or "").strip(),
        "event_kind": "actual_vs_estimate",
        "actual": actual,
        "expected": expected,
        "unit": unit,
        "actual_display": _display_number(actual, unit),
        "expected_display": _display_number(expected, unit),
        "surprise": surprise,
        "surprise_display": _display_number(surprise, unit),
        "surprise_severity": severity,
        **classification,
    }


def _surprise_dimensions(item: dict[str, Any]) -> dict[str, float]:
    surprise = item.get("surprise")
    try:
        value = float(surprise)
    except (TypeError, ValueError):
        return {}
    if value == 0:
        return {}
    family = str(item.get("family") or "")
    higher_is = str(item.get("higher_is") or "")
    sign = 1.0 if value > 0 else -1.0
    dims: dict[str, float] = {}
    if family in {"labor_growth", "growth_activity"}:
        dims["growth"] = sign
        dims["rates"] = sign
    elif family == "labor_slack":
        dims["growth"] = -sign
        dims["rates"] = -sign
    elif family == "inflation":
        dims["inflation"] = sign
        dims["rates"] = sign
    elif higher_is == "growth_stronger":
        dims["growth"] = sign
    return dims


def _asset_bias_from_dimensions(dimensions: dict[str, float]) -> dict[str, str]:
    growth = dimensions.get("growth", 0.0)
    rates = dimensions.get("rates", 0.0)
    inflation = dimensions.get("inflation", 0.0)
    weak_growth = growth < -0.25
    strong_growth = growth > 0.25
    dovish = rates < -0.25
    hawkish = rates > 0.25 or inflation > 0.25
    if weak_growth and dovish:
        return {"SPY": "mixed", "QQQ": "mixed_to_up", "TLT": "up", "UUP": "down", "GLD": "up", "VXX": "initial_up"}
    if strong_growth and hawkish:
        return {"SPY": "mixed", "QQQ": "mixed_to_down", "TLT": "down", "UUP": "up", "GLD": "mixed", "VXX": "mixed"}
    if hawkish:
        return {"SPY": "down", "QQQ": "down", "TLT": "down", "UUP": "up", "GLD": "mixed", "VXX": "up"}
    if dovish:
        return {"SPY": "up_or_mixed", "QQQ": "up_or_mixed", "TLT": "up", "UUP": "down", "GLD": "up", "VXX": "down_or_mixed"}
    if weak_growth:
        return {"SPY": "down", "QQQ": "down", "TLT": "up", "UUP": "down", "GLD": "up", "VXX": "up"}
    if strong_growth:
        return {"SPY": "up", "QQQ": "up", "TLT": "mixed_to_down", "UUP": "mixed_to_up", "GLD": "mixed", "VXX": "down"}
    return {"SPY": "neutral", "QQQ": "neutral", "TLT": "neutral", "UUP": "neutral", "GLD": "neutral", "VXX": "neutral"}


def _macro_summary_label(dimensions: dict[str, float]) -> str:
    parts: list[str] = []
    if dimensions.get("growth", 0.0) < -0.25:
        parts.append("growth weak")
    elif dimensions.get("growth", 0.0) > 0.25:
        parts.append("growth strong")
    if dimensions.get("rates", 0.0) < -0.25:
        parts.append("rates dovish")
    elif dimensions.get("rates", 0.0) > 0.25:
        parts.append("rates hawkish")
    if dimensions.get("inflation", 0.0) > 0.25:
        parts.append("inflation hot")
    elif dimensions.get("inflation", 0.0) < -0.25:
        parts.append("inflation cool")
    return " / ".join(parts) if parts else "macro mixed/neutral"


def _load_macro_rows(config: MacroReleaseInterpreterConfig) -> list[dict[str, Any]]:
    since = (datetime.now(UTC) - timedelta(minutes=int(config.lookback_minutes))).isoformat()
    terms = [
        "payroll",
        "jobless",
        "unemployment",
        "employment",
        "claims",
        "ism",
        "pmi",
        "cpi",
        "pce",
        "ppi",
        "inflation",
        "gdp",
        "retail sales",
        "factory orders",
        "construction spending",
    ]
    rows: list[dict[str, Any]] = []
    con = sqlite3.connect(str(config.database_path), timeout=30)
    con.row_factory = sqlite3.Row
    try:
        where = " OR ".join(["lower(headline) LIKE ?" for _ in terms])
        for row in con.execute(
            f"""
            SELECT id, provider, primary_symbol, headline,
                   COALESCE(provider_published_at,published_at,received_at) AS published_at
              FROM news_articles
             WHERE COALESCE(provider_published_at,published_at,received_at) >= ?
               AND ({where})
             ORDER BY COALESCE(provider_published_at,published_at,received_at) DESC, id DESC
             LIMIT ?
            """,
            [since, *(f"%{term}%" for term in terms), int(config.max_rows)],
        ):
            ts = _parse_dt(row["published_at"])
            if ts is None:
                continue
            rows.append(
                {
                    "source_table": "news_articles",
                    "source_id": int(row["id"]),
                    "provider": str(row["provider"] or ""),
                    "primary_symbol": str(row["primary_symbol"] or ""),
                    "headline": str(row["headline"] or ""),
                    "published_at": ts,
                }
            )
        where2 = " OR ".join(["lower(title) LIKE ?" for _ in terms])
        for row in con.execute(
            f"""
            SELECT id, source, title, published_at
              FROM external_market_signals
             WHERE published_at >= ?
               AND ({where2})
             ORDER BY published_at DESC, id DESC
             LIMIT ?
            """,
            [since, *(f"%{term}%" for term in terms), int(config.max_rows)],
        ):
            ts = _parse_dt(row["published_at"])
            if ts is None:
                continue
            rows.append(
                {
                    "source_table": "external_market_signals",
                    "source_id": int(row["id"]),
                    "provider": str(row["source"] or ""),
                    "primary_symbol": "",
                    "headline": str(row["title"] or ""),
                    "published_at": ts,
                }
            )
    finally:
        con.close()
    return rows


def _cluster_parsed(parsed: list[dict[str, Any]], window_minutes: int) -> list[dict[str, Any]]:
    clusters: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in parsed:
        ts = _parse_dt(item.get("published_at")) or datetime.now(UTC)
        bucket = int(ts.timestamp() // max(60, int(window_minutes) * 60))
        key = f"{bucket}|macro_release"
        clusters[key].append(item)
    output: list[dict[str, Any]] = []
    for key, items in clusters.items():
        items_sorted = sorted(items, key=lambda row: str(row.get("published_at") or ""))
        unique_items: list[dict[str, Any]] = []
        seen_items: set[tuple[Any, ...]] = set()
        for item in items_sorted:
            dedupe_key = (
                item.get("event_kind"),
                item.get("indicator_name"),
                item.get("period"),
                item.get("actual"),
                item.get("expected"),
                item.get("prior_from"),
                item.get("prior_to"),
            )
            if dedupe_key in seen_items:
                continue
            seen_items.add(dedupe_key)
            unique_items.append(item)
        release_time = _parse_dt(items_sorted[0].get("published_at")) or datetime.now(UTC)
        dims: dict[str, float] = defaultdict(float)
        event_kinds = Counter(str(item.get("event_kind") or "") for item in unique_items)
        families = Counter(str(item.get("family") or "") for item in unique_items)
        providers = sorted({str(item.get("provider") or "") for item in items if item.get("provider")})
        for item in unique_items:
            for dim, value in _surprise_dimensions(item).items():
                weight = 1.0 if item.get("importance") == "high" else 0.65
                dims[dim] += float(value) * weight
        norm = max(1.0, sum(abs(v) for v in dims.values()))
        normalized_dims = {k: round(v / norm, 4) for k, v in dims.items()}
        summary_label = _macro_summary_label(normalized_dims)
        output.append(
            {
                "cluster_key": key,
                "release_time": release_time.isoformat(),
                "release_time_local": _local_text(release_time),
                "status": "parsed",
                "indicator_count": len(unique_items),
                "raw_item_count": len(items),
                "provider_count": len(providers),
                "providers": providers,
                "families": dict(families),
                "event_kinds": dict(event_kinds),
                "macro_surprise_dimensions": normalized_dims,
                "macro_surprise_label": summary_label,
                "asset_bias": _asset_bias_from_dimensions(normalized_dims),
                "items": unique_items[:24],
            }
        )
    output.sort(key=lambda row: str(row.get("release_time") or ""), reverse=True)
    return output


def _horizon_minutes(horizon: str) -> int:
    text = str(horizon or "").upper().strip()
    if text.endswith("M"):
        try:
            return int(text[:-1])
        except ValueError:
            return 5
    return {"1H": 60, "EOD": 390}.get(text, 5)


def _direction_from_return(value: float | None, flat_threshold: float) -> str:
    if value is None:
        return "unknown"
    if value >= flat_threshold:
        return "up"
    if value <= -flat_threshold:
        return "down"
    return "flat"


def _attach_market_reactions(
    clusters: list[dict[str, Any]],
    *,
    price_source: IntradayPriceSourceLike | None,
    config: MacroReleaseInterpreterConfig,
) -> dict[str, Any]:
    if not config.attach_market_reaction:
        return {"status": "disabled", "clusters_updated": 0}
    if price_source is None:
        return {"status": "blocked", "reason": "price_source_not_configured", "clusters_updated": 0}
    now = datetime.now(UTC)
    bar_cache: dict[tuple[str, str, str], list[IntradayBar]] = {}
    updated = 0
    errors: list[dict[str, Any]] = []
    for cluster in clusters:
        release_time = _parse_dt(cluster.get("release_time"))
        if release_time is None:
            continue
        due_horizons = [h for h in config.reaction_horizons if now >= release_time + timedelta(minutes=_horizon_minutes(h))]
        if not due_horizons:
            cluster["market_reaction"] = {"status": "pending", "due_horizons": []}
            continue
        max_minutes = max(_horizon_minutes(h) for h in due_horizons)
        start = release_time - timedelta(minutes=5)
        end = release_time + timedelta(minutes=max_minutes + 10)
        asset_results: dict[str, Any] = {}
        for asset in config.reaction_assets:
            cache_key = (asset, start.isoformat(), end.isoformat())
            try:
                if cache_key not in bar_cache:
                    bar_cache[cache_key] = price_source.get_intraday_bars(asset, start, end)
                bars = bar_cache.get(cache_key) or []
                baseline = _latest_intraday_leq(bars, release_time)
                if baseline is None:
                    asset_results[asset] = {"status": "blocked", "reason": "baseline_bar_missing"}
                    continue
                horizon_results: dict[str, Any] = {}
                for horizon in due_horizons:
                    reaction = _first_intraday_geq(bars, release_time + timedelta(minutes=_horizon_minutes(horizon)))
                    if reaction is None:
                        horizon_results[horizon] = {"status": "blocked", "reason": "reaction_bar_missing"}
                        continue
                    raw_return = reaction.close / baseline.close - 1.0 if baseline.close else None
                    horizon_results[horizon] = {
                        "status": "ok",
                        "baseline_at": baseline.timestamp.isoformat(),
                        "reaction_at": reaction.timestamp.isoformat(),
                        "baseline_price": baseline.close,
                        "reaction_price": reaction.close,
                        "raw_return": round(float(raw_return), 8) if raw_return is not None else None,
                        "direction_label": _direction_from_return(raw_return, config.flat_return_threshold),
                    }
                asset_results[asset] = {"status": "ok", "horizons": horizon_results}
            except Exception as exc:
                error = {"asset": asset, "error": f"{type(exc).__name__}: {str(exc)[:240]}"}
                errors.append(error)
                asset_results[asset] = {"status": "blocked", "reason": "price_source_error", **error}
        cluster["market_reaction"] = {
            "status": "attached",
            "price_source": getattr(price_source, "name", "unknown"),
            "assets": asset_results,
            "due_horizons": due_horizons,
        }
        updated += 1
    return {
        "status": "ok",
        "clusters_updated": updated,
        "error_count": len(errors),
        "sample_errors": errors[:5],
        "price_source": getattr(price_source, "name", "unknown"),
    }


def build_macro_release_interpreter_report(
    config: MacroReleaseInterpreterConfig,
    *,
    price_source: IntradayPriceSourceLike | None = None,
) -> dict[str, Any]:
    generated_at = _iso_now()
    rows = _load_macro_rows(config)
    parsed: list[dict[str, Any]] = []
    unparsed_macro_rows = 0
    for row in rows:
        item = _parse_macro_headline(
            row["headline"],
            provider=row["provider"],
            primary_symbol=row["primary_symbol"],
            ts=row["published_at"],
            source_table=row["source_table"],
            source_id=row["source_id"],
        )
        if item is None:
            unparsed_macro_rows += 1
            continue
        parsed.append(item)
    clusters = _cluster_parsed(parsed, config.cluster_window_minutes)
    reaction_report = _attach_market_reactions(clusters, price_source=price_source, config=config)
    latest = clusters[0] if clusters else {}
    calendar = _read_json(Path("exports/m237_macro_event_calendar.json"))
    upcoming_events = (calendar.get("upcoming_events") or [])[:8] if isinstance(calendar, dict) else []
    return {
        "macro_release_interpreter_version": MACRO_RELEASE_INTERPRETER_VERSION,
        "generated_at": generated_at,
        "generated_at_local": _local_text(generated_at),
        "status": "ok",
        "config": {
            "lookback_minutes": int(config.lookback_minutes),
            "cluster_window_minutes": int(config.cluster_window_minutes),
            "reaction_horizons": list(config.reaction_horizons),
            "reaction_assets": list(config.reaction_assets),
        },
        "summary": {
            "rows_scanned": len(rows),
            "parsed_indicator_count": len(parsed),
            "unparsed_macro_rows": unparsed_macro_rows,
            "cluster_count": len(clusters),
            "latest_macro_surprise_label": latest.get("macro_surprise_label"),
            "latest_release_time_local": latest.get("release_time_local"),
            "next_known_macro_event": upcoming_events[0] if upcoming_events else None,
        },
        "latest_event_line": _event_line(latest) if latest else "",
        "clusters": clusters,
        "upcoming_events": upcoming_events,
        "market_reaction_attachment": reaction_report,
        "scorer_evidence_packet": _scorer_packet(clusters, upcoming_events),
        "safety": {
            "read_only_price_fetch": bool(config.attach_market_reaction),
            "broker_orders_submitted": False,
            "transmit_enabled": False,
        },
    }


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _event_line(cluster: dict[str, Any]) -> str:
    if not cluster:
        return ""
    pieces = []
    for item in cluster.get("items") or []:
        if item.get("event_kind") == "actual_vs_estimate":
            pieces.append(f"{item.get('indicator_name')}: {item.get('actual_display')} vs {item.get('expected_display')}")
        elif item.get("event_kind") == "prior_revision":
            pieces.append(f"{item.get('indicator_name')} prior revised { _display_number(item.get('prior_from'), item.get('unit') or '') } to { _display_number(item.get('prior_to'), item.get('unit') or '') }")
    return f"{cluster.get('release_time_local')} | {'; '.join(pieces[:6])} | {cluster.get('macro_surprise_label')}"


def _scorer_packet(clusters: list[dict[str, Any]], upcoming_events: list[dict[str, Any]]) -> dict[str, Any]:
    latest = clusters[0] if clusters else {}
    return {
        "packet_version": "m237-macro-release-scorer-evidence-v1",
        "latest_macro_release": {
            "release_time": latest.get("release_time"),
            "release_time_local": latest.get("release_time_local"),
            "macro_surprise_dimensions": latest.get("macro_surprise_dimensions") or {},
            "macro_surprise_label": latest.get("macro_surprise_label"),
            "asset_bias": latest.get("asset_bias") or {},
            "market_reaction": latest.get("market_reaction") or {},
        },
        "upcoming_event_risk": upcoming_events[:5],
        "policy": {
            "macro_release_day": "Raise volatility assumptions and reduce position-size confidence unless market reaction confirms the interpretation.",
            "fomc_window": "Treat FOMC day and preceding sessions as elevated gap/volatility windows for position management.",
            "mixed_signal": "Keep equity direction mixed when growth and rates implications conflict; score asset-by-asset.",
        },
    }


def render_macro_release_interpreter_markdown(payload: dict[str, Any]) -> str:
    summary = payload.get("summary") or {}
    lines = [
        "# M237 Macro Release Interpreter",
        "",
        f"- Version: `{payload.get('macro_release_interpreter_version')}`",
        f"- Generated: `{payload.get('generated_at_local') or payload.get('generated_at')}`",
        f"- Status: `{payload.get('status')}`",
        f"- Parsed indicators: `{summary.get('parsed_indicator_count')}` / scanned `{summary.get('rows_scanned')}`",
        f"- Clusters: `{summary.get('cluster_count')}`",
        f"- Latest: `{payload.get('latest_event_line') or '-'}`",
        f"- Market reaction attach: `{(payload.get('market_reaction_attachment') or {}).get('status')}`",
        "",
        "## Latest clusters",
        "",
    ]
    for cluster in (payload.get("clusters") or [])[:8]:
        lines.append(f"- `{cluster.get('release_time_local')}` `{cluster.get('macro_surprise_label')}` assets `{cluster.get('asset_bias') or {}}`")
        for item in (cluster.get("items") or [])[:5]:
            if item.get("event_kind") == "actual_vs_estimate":
                lines.append(
                    f"  - {item.get('indicator_name')}: `{item.get('actual_display')}` vs `{item.get('expected_display')}` surprise `{item.get('surprise_display')}`"
                )
            elif item.get("event_kind") == "prior_revision":
                lines.append(
                    f"  - {item.get('indicator_name')} prior: `{_display_number(item.get('prior_from'), item.get('unit') or '')}` -> `{_display_number(item.get('prior_to'), item.get('unit') or '')}`"
                )
    lines.extend(["", "## Upcoming events", ""])
    for event in payload.get("upcoming_events") or []:
        lines.append(f"- `{event.get('event_time_local')}` `{event.get('name')}` risk `{event.get('risk_level')}` source `{event.get('source')}`")
    return "\n".join(lines) + "\n"


def _event(name: str, when_et: datetime, *, source: str, category: str, risk: str, source_url: str, notes: str = "") -> dict[str, Any]:
    when_utc = when_et.astimezone(UTC)
    return {
        "name": name,
        "category": category,
        "source": source,
        "source_url": source_url,
        "event_time": when_utc.isoformat(),
        "event_time_et": when_et.strftime("%Y-%m-%d %H:%M:%S %Z (%z)"),
        "event_time_local": _local_text(when_utc),
        "risk_level": risk,
        "notes": notes,
    }


def _fallback_calendar_events() -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for month, start_day, end_day, sep in [
        (7, 28, 29, False),
        (9, 15, 16, True),
        (10, 27, 28, False),
        (12, 8, 9, True),
    ]:
        events.append(
            _event(
                f"FOMC policy decision{' + SEP' if sep else ''}",
                datetime(2026, month, end_day, 14, 0, tzinfo=EASTERN),
                source="Federal Reserve",
                category="fomc",
                risk="critical",
                source_url=SOURCE_URLS["fomc"],
                notes=f"Meeting {start_day}-{end_day}; statement assumed 2:00 p.m. ET, press conference typically follows.",
            )
        )
    # Major BLS releases after 2026-07-02 that are known from the official annual schedule shape.
    for date_text, name, risk in [
        ("2026-07-10", "Consumer Price Index for June 2026", "critical"),
        ("2026-07-14", "Producer Price Index for June 2026", "high"),
        ("2026-07-29", "Job Openings and Labor Turnover Survey for June 2026", "high"),
        ("2026-08-07", "Employment Situation for July 2026", "critical"),
        ("2026-08-12", "Consumer Price Index for July 2026", "critical"),
    ]:
        year, month, day = (int(x) for x in date_text.split("-"))
        events.append(_event(name, datetime(year, month, day, 8, 30, tzinfo=EASTERN), source="BLS", category="economic_indicator", risk=risk, source_url=SOURCE_URLS["bls"]))
    events.append(_event("Personal Income and Outlays / PCE Price Index for June 2026", datetime(2026, 7, 30, 8, 30, tzinfo=EASTERN), source="BEA", category="economic_indicator", risk="critical", source_url=SOURCE_URLS["bea_pce"]))
    for date_text, name in [
        ("2026-07-06", "ISM Services PMI for June 2026"),
        ("2026-08-03", "ISM Manufacturing PMI for July 2026"),
        ("2026-08-05", "ISM Services PMI for July 2026"),
        ("2026-09-01", "ISM Manufacturing PMI for August 2026"),
        ("2026-09-03", "ISM Services PMI for August 2026"),
    ]:
        year, month, day = (int(x) for x in date_text.split("-"))
        events.append(_event(name, datetime(year, month, day, 10, 0, tzinfo=EASTERN), source="ISM", category="economic_indicator", risk="high", source_url=SOURCE_URLS["ism"]))
    return events


def _parse_time_et(date_text: str, time_text: str) -> datetime | None:
    try:
        parsed = datetime.strptime(f"{date_text} {time_text}", "%B %d, %Y %I:%M %p")
    except ValueError:
        return None
    return parsed.replace(tzinfo=EASTERN)


def _parse_fomc_events(text: str) -> list[dict[str, Any]]:
    section = re.search(r"2026 FOMC Meetings(?P<body>.*?)(?:2025 FOMC Meetings|Future Year|$)", text, re.I | re.S)
    body = section.group("body") if section else text
    events: list[dict[str, Any]] = []
    for match in re.finditer(r"\b(January|March|April|June|July|September|October|December)\s+(\d{1,2})(?:-(\d{1,2}))?(\*)?", body, re.I):
        month = MONTHS[match.group(1).lower()]
        start_day = int(match.group(2))
        end_day = int(match.group(3) or match.group(2))
        if month < 7:
            continue
        sep = bool(match.group(4))
        events.append(
            _event(
                f"FOMC policy decision{' + SEP' if sep else ''}",
                datetime(2026, month, end_day, 14, 0, tzinfo=EASTERN),
                source="Federal Reserve",
                category="fomc",
                risk="critical",
                source_url=SOURCE_URLS["fomc"],
                notes=f"Parsed from Federal Reserve FOMC calendar; meeting {start_day}-{end_day}.",
            )
        )
    return events


def _parse_bls_events(text: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    day_names = r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)"
    pattern = re.compile(
        rf"{day_names},\s+([A-Za-z]+ \d{{1,2}}, 2026)\s+(\d{{2}}:\d{{2}} [AP]M)\s+(.+?)(?={day_names},\s+[A-Za-z]+ \d{{1,2}}, 2026|NOTE:|Last Modified|$)",
        re.I | re.S,
    )
    for match in pattern.finditer(text):
        release = re.sub(r"\s+", " ", match.group(3)).strip()
        if not any(needle.lower() in release.lower() for needle in IMPORTANT_RELEASE_PATTERNS):
            continue
        when = _parse_time_et(match.group(1), match.group(2))
        if when is None:
            continue
        risk = "critical" if any(term in release for term in ("Employment Situation", "Consumer Price Index")) else "high"
        events.append(_event(release, when, source="BLS", category="economic_indicator", risk=risk, source_url=SOURCE_URLS["bls"], notes="Parsed from BLS annual release schedule."))
    return events


def _parse_ism_events(text: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for match in re.finditer(r"\b(January|February|March|April|May|June|July|August|September|October|November|December) 2026\s+(\d{1,2})\s+(\d{1,2})", text, re.I):
        month_name = match.group(1)
        month = MONTHS[month_name.lower()]
        if month < 7:
            continue
        manu_day = int(match.group(2))
        serv_day = int(match.group(3))
        events.append(_event(f"ISM Manufacturing PMI for {month_name} 2026", datetime(2026, month, manu_day, 10, 0, tzinfo=EASTERN), source="ISM", category="economic_indicator", risk="high", source_url=SOURCE_URLS["ism"], notes="Parsed from ISM PMI release calendar."))
        events.append(_event(f"ISM Services PMI for {month_name} 2026", datetime(2026, month, serv_day, 10, 0, tzinfo=EASTERN), source="ISM", category="economic_indicator", risk="high", source_url=SOURCE_URLS["ism"], notes="Parsed from ISM PMI release calendar."))
    return events


def _parse_bea_pce_events(text: str) -> list[dict[str, Any]]:
    match = re.search(r"Next release:\s*([A-Za-z]+ \d{1,2}, 2026)", text, re.I)
    if not match:
        return []
    when = _parse_time_et(match.group(1), "08:30 AM")
    if when is None:
        return []
    return [
        _event(
            "Personal Income and Outlays / PCE Price Index",
            when,
            source="BEA",
            category="economic_indicator",
            risk="critical",
            source_url=SOURCE_URLS["bea_pce"],
            notes="Parsed from BEA PCE page next release.",
        )
    ]


def _merge_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    merged: list[dict[str, Any]] = []
    for event in sorted(events, key=lambda row: str(row.get("event_time") or "")):
        key = (
            str(event.get("event_time") or "")[:16],
            f"{event.get('source') or ''}|{event.get('category') or ''}",
        )
        if key in seen:
            continue
        seen.add(key)
        merged.append(event)
    return merged


def build_macro_event_calendar_report(config: MacroEventCalendarConfig) -> dict[str, Any]:
    generated_at = _iso_now()
    now = datetime.now(UTC)
    events = _fallback_calendar_events()
    refresh_status: dict[str, Any] = {"status": "fallback_static", "sources": SOURCE_URLS}
    if config.include_internet_refresh:
        source_results: dict[str, str] = {}
        for source, url in SOURCE_URLS.items():
            try:
                text = _strip_html(_read_url(url, config.timeout_seconds))
                parsed_events: list[dict[str, Any]] = []
                if source == "fomc":
                    parsed_events = _parse_fomc_events(text)
                elif source == "bls":
                    parsed_events = _parse_bls_events(text)
                elif source == "ism":
                    parsed_events = _parse_ism_events(text)
                elif source == "bea_pce":
                    parsed_events = _parse_bea_pce_events(text)
                events.extend(parsed_events)
                source_results[source] = f"reachable {len(text)} chars"
            except Exception as exc:
                source_results[source] = f"error {type(exc).__name__}: {str(exc)[:160]}"
        refresh_status = {"status": "internet_parsed_with_static_fallback", "sources": SOURCE_URLS, "source_results": source_results}
    horizon_end = now + timedelta(days=int(config.horizon_days))
    upcoming = [event for event in _merge_events(events) if now <= (_parse_dt(event.get("event_time")) or now) <= horizon_end]
    next_event = upcoming[0] if upcoming else None
    risk_windows = []
    for event in upcoming:
        event_time = _parse_dt(event.get("event_time"))
        if event_time is None:
            continue
        risk_windows.append(
            {
                "name": event.get("name"),
                "risk_level": event.get("risk_level"),
                "event_time": event.get("event_time"),
                "event_time_local": event.get("event_time_local"),
                "position_management_window_start": (event_time - timedelta(days=3)).isoformat(),
                "position_management_window_start_local": _local_text(event_time - timedelta(days=3)),
                "event_day_mode": "reduce blind overnight exposure; require fresh market/options/evidence confirmation",
            }
        )
    return {
        "macro_event_calendar_version": MACRO_EVENT_CALENDAR_VERSION,
        "generated_at": generated_at,
        "generated_at_local": _local_text(generated_at),
        "status": "ok",
        "summary": {
            "upcoming_event_count": len(upcoming),
            "critical_event_count": sum(1 for event in upcoming if event.get("risk_level") == "critical"),
            "next_event": next_event,
            "horizon_days": int(config.horizon_days),
        },
        "upcoming_events": upcoming,
        "risk_windows": risk_windows,
        "refresh_status": refresh_status,
        "scorer_policy": {
            "pre_event": "Several days before critical macro/FOMC events, reduce position confidence and check options/market structure for gap risk.",
            "event_day": "Treat event time as high-volatility; scorer should require market reaction confirmation before upgrading conviction.",
            "post_event": "Use actual market reaction at 1M/5M/15M/30M to tune macro surprise thresholds.",
        },
    }


def render_macro_event_calendar_markdown(payload: dict[str, Any]) -> str:
    summary = payload.get("summary") or {}
    lines = [
        "# M237 Macro Event Calendar",
        "",
        f"- Version: `{payload.get('macro_event_calendar_version')}`",
        f"- Generated: `{payload.get('generated_at_local') or payload.get('generated_at')}`",
        f"- Status: `{payload.get('status')}`",
        f"- Upcoming events: `{summary.get('upcoming_event_count')}` critical `{summary.get('critical_event_count')}`",
        "",
        "## Upcoming critical/high events",
        "",
    ]
    for event in payload.get("upcoming_events") or []:
        lines.append(f"- `{event.get('event_time_local')}` `{event.get('name')}` risk `{event.get('risk_level')}` source `{event.get('source')}`")
    lines.extend(["", "## Position management windows", ""])
    for window in (payload.get("risk_windows") or [])[:12]:
        lines.append(f"- `{window.get('position_management_window_start_local')}` start for `{window.get('name')}`")
    return "\n".join(lines) + "\n"


def write_json(path: Path, payload: dict[str, Any]) -> Path:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8")
    return resolved


def write_macro_release_markdown(path: Path, payload: dict[str, Any]) -> Path:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(render_macro_release_interpreter_markdown(payload), encoding="utf-8")
    return resolved


def write_macro_calendar_markdown(path: Path, payload: dict[str, Any]) -> Path:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(render_macro_event_calendar_markdown(payload), encoding="utf-8")
    return resolved
