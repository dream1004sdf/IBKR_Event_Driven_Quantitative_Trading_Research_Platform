from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from .market_outcomes import IntradayBar, _first_intraday_geq, _latest_intraday_leq


M236_NEWS_LEARNING_VERSION = "m236-news-source-quality-learning-v3-no-alpha-active-training"
M236_OUTCOME_ATTACHMENT_VERSION = "m236-news-outcome-attachment-v2-adaptive-eod"
DEFAULT_HORIZON_MINUTES = {"5M": 5, "15M": 15, "30M": 30, "EOD": 390}
BASE_PROVIDER_WEIGHTS = {
    "dow_jones": {"reliability": 0.92, "speed": 0.78, "independence": 0.95},
    "benzinga": {"reliability": 0.78, "speed": 0.88, "independence": 0.85},
    "emergency_breaking_news": {"reliability": 0.62, "speed": 0.96, "independence": 0.55},
    "marketaux": {"reliability": 0.46, "speed": 0.42, "independence": 0.35},
}
DISABLED_PROVIDER_FAMILIES = {"alpha_vantage"}
M236_INDEX_PROXY_ALIASES = {
    "SPX": "SPY",
    "SPXW": "SPY",
    "XSP": "SPY",
    "NDX": "QQQ",
    "XND": "QQQ",
    "RUT": "IWM",
    "VIX": "VXX",
}
M236_ROLE_PROXY_ASSETS = {
    "macro": ("SPY", "QQQ", "TLT", "UUP", "GLD"),
    "geopolitical": ("USO", "XLE", "GLD", "VXX", "SPY"),
    "sector": ("QQQ", "SPY", "SOXX", "SMH"),
    "options": ("SPY", "QQQ", "IWM"),
    "market_color": ("SPY", "QQQ", "IWM", "DIA"),
    "context": ("SPY",),
}
BROAD_MARKET_PROXY_ASSETS = {"SPY", "QQQ", "IWM", "DIA", "TLT", "GLD", "VXX", "UUP"}
BROAD_PROXY_GUARD_ROLE_FAMILIES = {"analyst", "earnings", "single_stock_catalyst", "context"}
US_EASTERN = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class M236NewsLearningConfig:
    event_roles_json_path: Path = Path("exports/m236_cross_provider_event_roles.json")
    outcome_learning_jsonl_path: Path = Path("exports/m236_cross_provider_outcome_learning.jsonl")
    evaluated_index_jsonl_path: Path = Path("exports/m236_evaluated_outcome_index.jsonl")
    macro_release_interpreter_json_path: Path = Path("exports/m237_macro_release_interpreter.json")
    signal_classification_notes_jsonl_path: Path = Path("exports/clean_backfill_signal_classification_notes.jsonl")
    provider_profile_output_path: Path = Path("exports/m236_provider_weight_profile.json")
    report_json_output_path: Path = Path("exports/m236_news_learning_report.json")
    report_markdown_output_path: Path = Path("exports/m236_news_learning_report.md")
    max_records: int = 5000
    evaluated_sample_records: int = 2000
    max_evaluated_index_records: int = 20000
    flat_return_threshold: float = 0.001


class IntradayPriceSource(Protocol):
    name: str

    def get_intraday_bars(self, symbol: str, start: datetime, end: datetime) -> list[IntradayBar]:
        ...


@dataclass(frozen=True)
class M236OutcomeAttachmentConfig:
    outcome_learning_jsonl_path: Path = Path("exports/m236_cross_provider_outcome_learning.jsonl")
    report_json_output_path: Path = Path("exports/m236_outcome_attachment_report.json")
    report_markdown_output_path: Path = Path("exports/m236_outcome_attachment_report.md")
    max_records: int = 25
    max_symbols: int = 10
    horizons: tuple[str, ...] = ("5M", "15M", "30M")
    flat_return_threshold: float = 0.001
    lookback_records: int = 5000
    adaptive_batch_enabled: bool = True
    adaptive_backlog_mid: int = 500
    adaptive_backlog_high: int = 2000
    adaptive_max_records_mid: int = 50
    adaptive_max_records_high: int = 100
    adaptive_max_symbols_mid: int = 12
    adaptive_max_symbols_high: int = 20
    dry_run: bool = False


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _local_time_text(value: str | None = None) -> str:
    try:
        dt = datetime.fromisoformat((value or _iso_now()).replace("Z", "+00:00"))
    except ValueError:
        dt = datetime.now(UTC)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z (%z)")


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


def _read_json(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        payload = json.loads(path.expanduser().read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _read_jsonl(path: Path, max_records: int) -> list[dict[str, Any]]:
    try:
        lines = path.expanduser().read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    rows: list[dict[str, Any]] = []
    for line in lines[-max(1, int(max_records)) :]:
        try:
            payload = json.loads(line)
        except Exception:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _signal_classification_review(path: Path, max_records: int = 2000) -> dict[str, Any]:
    rows = _read_jsonl(path, max_records)
    category_counts = Counter(str(row.get("category") or "unknown") for row in rows)
    source_counts = Counter(str(row.get("source") or "unknown") for row in rows)
    severity_counts = Counter(str(row.get("severity") or "unknown") for row in rows)
    return {
        "notes_path": str(path),
        "note_count": len(rows),
        "by_category": dict(category_counts),
        "by_source": dict(source_counts),
        "by_severity": dict(severity_counts),
        "top_notes": rows[:50],
        "training_use": {
            "role": "source_quality_theme_ticker_review",
            "scorer_effect": "review_only_context",
            "policy": "Use these notes to explain source/theme/ticker quality failures during final learning; do not directly promote live thresholds.",
        },
    }


def _record_key(record: dict[str, Any]) -> str:
    return str(
        record.get("learning_record_key")
        or record.get("canonical_event_id")
        or record.get("canonical_event_key")
        or ""
    )


def _is_evaluated_record(record: dict[str, Any]) -> bool:
    outcomes = record.get("outcomes") if isinstance(record.get("outcomes"), dict) else {}
    return any(isinstance(value, dict) and value.get("raw_return") is not None for value in outcomes.values())


def _merge_records_by_key(*record_sets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for records in record_sets:
        for record in records:
            if not isinstance(record, dict):
                continue
            key = _record_key(record)
            if not key:
                key = f"_unkeyed_{len(order)}"
            if key not in merged:
                order.append(key)
            # Later sources are fresher. The report passes evaluated-index first,
            # then current tail, so current tail wins while older evaluated rows
            # remain visible after they age out of the tail.
            merged[key] = record
    return [merged[key] for key in order]


def _provider_families_for_record(record: dict[str, Any]) -> set[str]:
    families: set[str] = set()
    for key in ("provider_family", "provider"):
        value = record.get(key)
        if isinstance(value, str) and value:
            families.add(value.lower())
    for key in ("provider_families", "providers"):
        values = record.get(key)
        if isinstance(values, list):
            families.update(str(item).lower() for item in values if str(item))
    for item in record.get("items") or []:
        if not isinstance(item, dict):
            continue
        for key in ("provider_family", "provider", "source"):
            value = item.get(key)
            if isinstance(value, str) and value:
                families.add(value.lower())
    return families


def _active_training_records(records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    active: list[dict[str, Any]] = []
    excluded = 0
    for record in records:
        if _provider_families_for_record(record) & DISABLED_PROVIDER_FAMILIES:
            excluded += 1
            continue
        active.append(record)
    return active, excluded


def _evaluated_index_path_for(config: M236NewsLearningConfig) -> Path:
    default = Path("exports/m236_evaluated_outcome_index.jsonl")
    configured = Path(config.evaluated_index_jsonl_path)
    outcome_path = Path(config.outcome_learning_jsonl_path)
    if configured == default and outcome_path.parent != Path("exports"):
        return outcome_path.parent / "m236_evaluated_outcome_index.jsonl"
    return configured


def _load_learning_records(config: M236NewsLearningConfig) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    evaluated_index_path = _evaluated_index_path_for(config)
    tail = _read_jsonl(config.outcome_learning_jsonl_path, config.max_records)
    evaluated_index = _read_jsonl(evaluated_index_path, config.evaluated_sample_records)
    merged_records = _merge_records_by_key(evaluated_index, tail)
    records, alpha_excluded_count = _active_training_records(merged_records)
    return records, {
        "tail_record_count": len(tail),
        "evaluated_index_record_count": len(evaluated_index),
        "merged_record_count": len(merged_records),
        "active_training_record_count": len(records),
        "alpha_vantage_excluded_record_count": alpha_excluded_count,
        "disabled_provider_families": sorted(DISABLED_PROVIDER_FAMILIES),
        "evaluated_retention_policy": "merge latest outcome-learning tail with evaluated sidecar index so hit rates do not reset when evaluated rows age out",
        "evaluated_index_jsonl_path": str(evaluated_index_path),
    }


def _write_evaluated_index(path: Path, records: list[dict[str, Any]], *, max_records: int) -> dict[str, Any]:
    existing = _read_jsonl(path, max_records)
    evaluated = [record for record in records if _is_evaluated_record(record)]
    merged = _merge_records_by_key(existing, evaluated)
    merged_evaluated = [record for record in merged if _is_evaluated_record(record)]

    def sort_key(record: dict[str, Any]) -> str:
        return str(record.get("event_time") or record.get("generated_at") or "")

    trimmed = sorted(merged_evaluated, key=sort_key)[-max(1, int(max_records)) :]
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    tmp = resolved.with_suffix(resolved.suffix + ".tmp")
    tmp.write_text(
        "\n".join(json.dumps(row, sort_keys=True, ensure_ascii=False) for row in trimmed) + ("\n" if trimmed else ""),
        encoding="utf-8",
    )
    tmp.replace(resolved)
    return {
        "evaluated_index_jsonl_path": str(path),
        "existing_evaluated_index_count": len(existing),
        "new_evaluated_records_seen": len(evaluated),
        "final_evaluated_index_count": len(trimmed),
        "max_evaluated_index_records": int(max_records),
    }


def _read_jsonl_with_prefix(path: Path, max_records: int) -> tuple[list[str], list[dict[str, Any]]]:
    try:
        lines = path.expanduser().read_text(encoding="utf-8").splitlines()
    except Exception:
        return [], []
    tail_count = max(1, int(max_records))
    prefix = lines[:-tail_count] if len(lines) > tail_count else []
    tail = lines[-tail_count:]
    rows: list[dict[str, Any]] = []
    for line in tail:
        try:
            payload = json.loads(line)
        except Exception:
            payload = {"_raw_unparseable_line": line}
        rows.append(payload if isinstance(payload, dict) else {"_raw_non_object_line": line})
    return prefix, rows


def _write_jsonl_with_prefix(path: Path, prefix: list[str], rows: list[dict[str, Any]]) -> None:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    tmp = resolved.with_suffix(resolved.suffix + ".tmp")
    lines = [*prefix, *(json.dumps(row, sort_keys=True, ensure_ascii=False) for row in rows)]
    tmp.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    tmp.replace(resolved)


def _direction_from_return(value: Any, flat_threshold: float) -> str:
    try:
        ret = float(value)
    except (TypeError, ValueError):
        return "unknown"
    if ret >= float(flat_threshold):
        return "up"
    if ret <= -float(flat_threshold):
        return "down"
    return "flat"


def _horizon_minutes(horizon: str) -> int:
    normalized = str(horizon or "").strip().upper()
    if normalized == "EOD":
        return DEFAULT_HORIZON_MINUTES["EOD"]
    if normalized.endswith("M"):
        return int(normalized[:-1])
    if normalized.endswith("H"):
        return int(normalized[:-1]) * 60
    raise ValueError(f"Unsupported M236 intraday horizon {horizon!r}")


def _next_regular_close_utc(event_time: datetime) -> datetime:
    local = event_time.astimezone(US_EASTERN)
    close_local = local.replace(hour=16, minute=0, second=0, microsecond=0)
    if local >= close_local:
        close_local = close_local + timedelta(days=1)
    while close_local.weekday() >= 5:
        close_local = close_local + timedelta(days=1)
    return close_local.astimezone(UTC)


def _horizon_target_time(event_time: datetime, horizon: str) -> datetime:
    if str(horizon or "").strip().upper() == "EOD":
        return _next_regular_close_utc(event_time)
    return event_time + timedelta(minutes=_horizon_minutes(horizon))


def _broad_proxy_guard(record: dict[str, Any]) -> bool:
    role_family = str(record.get("role_family") or "").strip().lower()
    if role_family not in BROAD_PROXY_GUARD_ROLE_FAMILIES:
        return False
    try:
        ticker_confidence = float(record.get("ticker_inference_confidence") or 0.0)
    except (TypeError, ValueError):
        ticker_confidence = 0.0
    if ticker_confidence >= 0.50:
        return False
    mapping = record.get("target_asset_mapping") if isinstance(record.get("target_asset_mapping"), dict) else {}
    direct_assets = {str(asset or "").upper() for asset in mapping.get("direct_assets") or [] if str(asset or "").strip()}
    target_assets = {str(asset or "").upper() for asset in record.get("target_assets") or [] if str(asset or "").strip()}
    mapped_assets = {str(asset or "").upper() for asset in mapping.get("mapped_assets") or [] if str(asset or "").strip()}
    if direct_assets:
        return False
    return bool((target_assets | mapped_assets).intersection(BROAD_MARKET_PROXY_ASSETS))


def _safe_asset_candidates(record: dict[str, Any]) -> list[str]:
    blocked = {"", "MARKET", "UNKNOWN"}
    assets: list[str] = []
    raw_candidates: list[Any] = []
    guarded = _broad_proxy_guard(record)
    raw_candidates.extend(record.get("target_assets") or [])
    mapping = record.get("target_asset_mapping") if isinstance(record.get("target_asset_mapping"), dict) else {}
    raw_candidates.extend(mapping.get("mapped_assets") or [])
    raw_candidates.extend(mapping.get("direct_assets") or [])
    if not guarded:
        raw_candidates.extend(mapping.get("role_family_proxy_assets") or [])
    canonical_key = str(record.get("canonical_event_key") or "")
    if "|" in canonical_key:
        raw_candidates.append(canonical_key.split("|", 1)[0])
    role_family = str(record.get("role_family") or "").strip().lower()
    raw_candidates.extend(M236_ROLE_PROXY_ASSETS.get(role_family, ()))
    for raw in raw_candidates:
        asset = str(raw or "").strip().upper()
        if not asset:
            continue
        if asset.startswith("BROADTAPE:"):
            asset = "SPY"
        asset = M236_INDEX_PROXY_ALIASES.get(asset, asset)
        if guarded and asset in BROAD_MARKET_PROXY_ASSETS:
            continue
        if asset in blocked:
            continue
        if any(ch in asset for ch in ("/", " ", ":", "=")):
            continue
        if asset not in assets:
            assets.append(asset)
    return assets


def _attach_record_outcomes(
    record: dict[str, Any],
    *,
    price_source: IntradayPriceSource,
    bar_cache: dict[tuple[str, str, str], list[IntradayBar]],
    horizons: tuple[str, ...],
    flat_threshold: float,
) -> tuple[dict[str, Any], dict[str, Any]]:
    event_time = _parse_dt(record.get("event_time"))
    if event_time is None:
        return record, {"status": "blocked", "reason": "missing_event_time"}
    outcomes = record.get("outcomes") if isinstance(record.get("outcomes"), dict) else {}
    due = []
    now = datetime.now(UTC)
    for horizon in horizons:
        if horizon in outcomes and isinstance(outcomes[horizon], dict) and outcomes[horizon].get("raw_return") is not None:
            continue
        minutes = _horizon_minutes(horizon)
        if now >= event_time + timedelta(minutes=minutes):
            due.append(horizon)
    if not due:
        return record, {"status": "skipped", "reason": "no_due_horizons"}
    symbols = _safe_asset_candidates(record)
    if not symbols:
        return record, {"status": "blocked", "reason": "no_supported_target_asset", "due_horizons": due}
    symbol = symbols[0]
    target_times = {horizon: _horizon_target_time(event_time, horizon) for horizon in due}
    fetch_start = event_time - timedelta(minutes=15)
    fetch_end = max(target_times.values()) + timedelta(minutes=30)
    cache_key = (symbol, fetch_start.isoformat(), fetch_end.isoformat())
    if cache_key not in bar_cache:
        try:
            bar_cache[cache_key] = price_source.get_intraday_bars(symbol, fetch_start, fetch_end)
        except Exception as exc:
            return record, {
                "status": "blocked",
                "reason": "price_source_error",
                "symbol": symbol,
                "due_horizons": due,
                "error": f"{type(exc).__name__}: {str(exc)[:240]}",
            }
    bars = bar_cache.get(cache_key) or []
    baseline = _latest_intraday_leq(bars, event_time)
    if baseline is None:
        return record, {"status": "blocked", "reason": "baseline_bar_missing", "symbol": symbol, "due_horizons": due}
    attached = 0
    blocked: dict[str, str] = {}
    new_outcomes = dict(outcomes)
    for horizon in due:
        target = target_times[horizon]
        reaction = _first_intraday_geq(bars, target)
        if reaction is None:
            blocked[horizon] = "reaction_bar_missing"
            continue
        raw_return = reaction.close / baseline.close - 1.0 if baseline.close > 0 else None
        direction = _direction_from_return(raw_return, flat_threshold)
        new_outcomes[horizon] = {
            "outcome_version": M236_OUTCOME_ATTACHMENT_VERSION,
            "price_source": getattr(price_source, "name", "unknown_intraday"),
            "symbol": symbol,
            "baseline_at": baseline.timestamp.isoformat(),
            "reaction_at": reaction.timestamp.isoformat(),
            "baseline_price": baseline.close,
            "reaction_price": reaction.close,
            "raw_return": round(float(raw_return), 8) if raw_return is not None else None,
            "direction_label": direction,
            "horizon_minutes": _horizon_minutes(horizon),
            "target_at": target.isoformat(),
            "session_aware_eod": str(horizon).upper() == "EOD",
            "attached_at": _iso_now(),
            "leakage_guard": "attached only after horizon was due at attachment time",
        }
        attached += 1
    updated = dict(record)
    updated["outcomes"] = new_outcomes
    updated["outcome_attachment"] = {
        "outcome_attachment_version": M236_OUTCOME_ATTACHMENT_VERSION,
        "last_attempt_at": _iso_now(),
        "last_attempt_at_local": _local_time_text(),
        "price_source": getattr(price_source, "name", "unknown_intraday"),
        "symbol": symbol,
        "attached_horizon_count": attached,
        "blocked_horizons": blocked,
        "dry_run": False,
    }
    return updated, {
        "status": "attached" if attached else "blocked",
        "symbol": symbol,
        "attached_horizon_count": attached,
        "blocked_horizons": blocked,
        "due_horizons": due,
    }


def _expected_direction(record: dict[str, Any]) -> str:
    counts = record.get("direction_counts") or {}
    if not isinstance(counts, dict):
        return "unknown"
    positive = int(counts.get("positive") or 0)
    negative = int(counts.get("negative") or 0)
    if positive > negative:
        return "up"
    if negative > positive:
        return "down"
    return "flat"


def _record_horizons(record: dict[str, Any]) -> list[str]:
    raw = [str(item or "").strip().upper() for item in (record.get("horizons") or []) if str(item or "").strip()]
    horizons = list(dict.fromkeys(raw or list(DEFAULT_HORIZON_MINUTES)))
    if "EOD" not in horizons:
        horizons.append("EOD")
    return horizons


def _record_outcome_state(record: dict[str, Any], *, now: datetime, flat_threshold: float) -> dict[str, Any]:
    event_time = _parse_dt(record.get("event_time"))
    outcome_by_horizon = record.get("outcomes") if isinstance(record.get("outcomes"), dict) else {}
    due: list[str] = []
    evaluated: list[str] = []
    blocked: list[str] = []
    expected = _expected_direction(record)
    hits = 0
    scored = 0
    for horizon in _record_horizons(record):
        horizon_key = str(horizon)
        outcome = outcome_by_horizon.get(horizon_key) if isinstance(outcome_by_horizon, dict) else None
        if isinstance(outcome, dict) and outcome.get("raw_return") is not None:
            realized = str(outcome.get("direction_label") or _direction_from_return(outcome.get("raw_return"), flat_threshold))
            if expected != "unknown" and realized != "unknown":
                scored += 1
                if expected == realized or (expected == "flat" and realized == "flat"):
                    hits += 1
            evaluated.append(horizon_key)
            continue
        if event_time is None:
            blocked.append(horizon_key)
            continue
        target = _horizon_target_time(event_time, horizon_key)
        if now >= target:
            due.append(horizon_key)
    if evaluated:
        status = "evaluated"
    elif blocked and len(blocked) == len(_record_horizons(record)):
        status = "blocked_missing_event_time"
    elif due:
        status = "ready_for_outcome_attachment"
    else:
        status = "pending_market_outcome"
    return {
        "status": status,
        "due_horizons": due,
        "evaluated_horizons": evaluated,
        "blocked_horizons": blocked,
        "expected_direction": expected,
        "hit_count": hits,
        "scored_horizon_count": scored,
        "hit_rate": round(hits / scored, 4) if scored else None,
    }


def _provider_profile(records: list[dict[str, Any]], states: dict[str, dict[str, Any]]) -> dict[str, Any]:
    stats: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "sample_count": 0,
            "evaluated_horizon_count": 0,
            "hit_count": 0,
            "pending_count": 0,
            "ready_for_outcome_attachment_count": 0,
            "conflict_count": 0,
            "noise_count": 0,
        }
    )
    for record in records:
        state = states.get(str(record.get("learning_record_key") or "")) or {}
        for family in record.get("provider_families") or ["unknown"]:
            family_key = str(family or "unknown")
            item = stats[family_key]
            item["sample_count"] += 1
            item["evaluated_horizon_count"] += int(state.get("scored_horizon_count") or 0)
            item["hit_count"] += int(state.get("hit_count") or 0)
            if state.get("status") == "pending_market_outcome":
                item["pending_count"] += 1
            if state.get("status") == "ready_for_outcome_attachment":
                item["ready_for_outcome_attachment_count"] += 1
            if record.get("provider_agreement") == "conflict":
                item["conflict_count"] += 1
            if record.get("provider_agreement") == "marketaux_noise_filtered":
                item["noise_count"] += 1
    profile: dict[str, Any] = {}
    for family, item in sorted(stats.items()):
        base = BASE_PROVIDER_WEIGHTS.get(family, {"reliability": 0.30, "speed": 0.25, "independence": 0.20})
        evaluated = int(item["evaluated_horizon_count"])
        hit_rate = (int(item["hit_count"]) / evaluated) if evaluated else None
        reliability_delta = 0.0
        if hit_rate is not None and evaluated >= 10:
            reliability_delta = max(-0.12, min(0.12, (hit_rate - 0.50) * 0.24))
        noise_penalty = min(0.08, int(item["noise_count"]) / max(1, int(item["sample_count"])) * 0.08)
        conflict_penalty = min(0.06, int(item["conflict_count"]) / max(1, int(item["sample_count"])) * 0.06)
        reliability = max(0.0, min(1.0, float(base["reliability"]) + reliability_delta - noise_penalty - conflict_penalty))
        speed = float(base["speed"])
        independence = float(base["independence"])
        profile[family] = {
            **item,
            "base": base,
            "hit_rate": round(hit_rate, 4) if hit_rate is not None else None,
            "learned_weight": {
                "reliability": round(reliability, 4),
                "speed": round(speed, 4),
                "independence": round(independence, 4),
                "overall": round(reliability * 0.55 + speed * 0.25 + independence * 0.20, 4),
            },
            "learning_status": "active" if evaluated >= 10 else "bootstrap_until_outcomes_attach",
        }
    return profile


def _provider_role_family_profile(records: list[dict[str, Any]], states: dict[str, dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        grouped[str(record.get("role_family") or "unknown")].append(record)
    return {role_family: _provider_profile(role_records, states) for role_family, role_records in sorted(grouped.items())}


def _ticker_verification(records: list[dict[str, Any]], states: dict[str, dict[str, Any]]) -> dict[str, Any]:
    buckets = Counter()
    review_queue: list[dict[str, Any]] = []
    for record in records:
        confidence = record.get("ticker_inference_confidence")
        try:
            score = float(confidence)
        except (TypeError, ValueError):
            score = 0.0
        if score >= 0.80:
            bucket = "high"
        elif score >= 0.50:
            bucket = "medium"
        elif score > 0:
            bucket = "low"
        else:
            bucket = "missing"
        buckets[bucket] += 1
        state = states.get(str(record.get("learning_record_key") or "")) or {}
        if bucket in {"low", "missing"}:
            families = {str(item or "") for item in record.get("provider_families") or []}
            target_assets = record.get("target_assets") or []
            if record.get("provider_agreement") == "marketaux_noise_filtered":
                queue_bucket = "marketaux_noise"
            elif _broad_proxy_guard(record):
                queue_bucket = "broad_proxy_guarded"
            elif families.intersection({"benzinga", "dow_jones", "emergency_breaking_news"}):
                queue_bucket = "primary_provider_low_confidence"
            elif target_assets:
                queue_bucket = "alias_candidate"
            else:
                queue_bucket = "unknown_theme_or_company"
            review_queue.append(
                {
                    "learning_record_key": record.get("learning_record_key"),
                    "canonical_event_id": record.get("canonical_event_id"),
                    "target_assets": record.get("target_assets") or [],
                    "ticker_inference_confidence": score,
                    "status": state.get("status"),
                    "reason": "low_or_missing_ticker_confidence",
                    "review_bucket": queue_bucket,
                    "auto_triage": {
                        "temp_subscription_candidate": queue_bucket in {"primary_provider_low_confidence", "alias_candidate", "unknown_theme_or_company"},
                        "suppress_scorer_boost": queue_bucket in {"marketaux_noise", "broad_proxy_guarded"},
                    },
                }
            )
    queue_bucket_counts = Counter(str(item.get("review_bucket") or "unknown") for item in review_queue)
    return {
        "confidence_buckets": dict(buckets),
        "review_queue": review_queue[:50],
        "review_queue_count": len(review_queue),
        "review_queue_bucket_counts": dict(queue_bucket_counts),
    }


def _canonical_dedup(records: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        grouped[str(record.get("canonical_event_id") or record.get("canonical_event_key") or "unknown")].append(record)
    repeated = [
        {
            "canonical_event_id": key,
            "count": len(items),
            "canonical_event_key": items[0].get("canonical_event_key"),
            "latest_event_time": max(str(item.get("event_time") or "") for item in items),
            "supersedes_policy": "same canonical_event_id rows should update a single learning state; newer rows supersede old rows unless they add new facts",
            "freshness_policy": "first_breaking_can_boost; repeats_require_new_facts_or_market_confirmation",
        }
        for key, items in grouped.items()
        if len(items) > 1
    ]
    repeated.sort(key=lambda item: item["count"], reverse=True)
    return {
        "canonical_event_count": len(grouped),
        "repeated_canonical_event_count": len(repeated),
        "top_repeated_events": repeated[:30],
    }


def _scorer_impact(record: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    agreement = str(record.get("provider_agreement") or "")
    cap = record.get("adaptive_confidence_cap") or {}
    ticker_confidence = float(record.get("ticker_inference_confidence") or 0.0)
    directive = {
        "direction_boost": 0.0,
        "confidence_boost": 0.0,
        "volatility_warning": False,
        "do_not_trade_noise": False,
        "requires_market_confirmation": False,
        "reason": "neutral_source_quality",
    }
    if agreement == "confirmed" and ticker_confidence >= 0.50:
        directive["confidence_boost"] = min(0.12, float(record.get("confidence_adjustment") or 0.0), float(cap.get("positive_max") or 0.0))
        directive["direction_boost"] = directive["confidence_boost"] * 0.5
        directive["reason"] = "confirmed_source_quality"
    elif agreement == "weak_confirmation":
        directive["confidence_boost"] = min(0.02, float(record.get("confidence_adjustment") or 0.0))
        directive["requires_market_confirmation"] = True
        directive["reason"] = "weak_confirmation_market_confirmation_required"
    elif agreement == "conflict":
        directive["volatility_warning"] = True
        directive["requires_market_confirmation"] = True
        directive["reason"] = "provider_conflict_direction_unreliable"
    elif agreement == "marketaux_noise_filtered":
        directive["do_not_trade_noise"] = True
        directive["reason"] = "marketaux_noise_filtered"
    elif agreement == "external_only_or_unconfirmed":
        directive["requires_market_confirmation"] = True
        directive["reason"] = "external_only_unconfirmed"
    if ticker_confidence < 0.35:
        directive["direction_boost"] = 0.0
        directive["confidence_boost"] = min(float(directive["confidence_boost"]), 0.01)
        directive["requires_market_confirmation"] = True
        directive["reason"] = f"{directive['reason']}_low_ticker_confidence"
    if _broad_proxy_guard(record):
        directive["direction_boost"] = 0.0
        directive["confidence_boost"] = 0.0
        directive["requires_market_confirmation"] = True
        directive["reason"] = f"{directive['reason']}_broad_proxy_guard"
    if state.get("status") == "ready_for_outcome_attachment":
        directive["learning_status"] = "ready_for_outcome_attachment"
    else:
        directive["learning_status"] = state.get("status") or "unknown"
    return directive


def _macro_release_integration(records: list[dict[str, Any]], macro_release_interpreter_json_path: Path) -> dict[str, Any]:
    payload = _read_json(macro_release_interpreter_json_path)
    latest = payload.get("latest_macro_release") if isinstance(payload.get("latest_macro_release"), dict) else {}
    latest_time = _parse_dt(latest.get("event_time") or latest.get("event_time_utc") or payload.get("latest_event_time"))
    matched = 0
    macro_records = 0
    role_counts = Counter()
    for record in records:
        role_family = str(record.get("role_family") or "").lower()
        role = str(record.get("event_role") or "").lower()
        if role_family != "macro" and not role.startswith("macro"):
            continue
        macro_records += 1
        role_counts.update([role or role_family])
        event_time = _parse_dt(record.get("event_time"))
        if latest_time is not None and event_time is not None and abs((event_time - latest_time).total_seconds()) <= 90 * 60:
            matched += 1
    return {
        "macro_release_interpreter_json_path": str(macro_release_interpreter_json_path),
        "macro_release_interpreter_status": payload.get("status") or ("missing" if not payload else "ok"),
        "latest_macro_surprise_label": payload.get("latest_macro_surprise_label"),
        "latest_event_line": payload.get("latest_event_line"),
        "latest_asset_bias": payload.get("asset_bias") or {},
        "macro_record_count": macro_records,
        "macro_records_near_latest_release_count": matched,
        "macro_role_counts": dict(role_counts),
        "integration_policy": "Macro/news clusters should use M237 release surprise and market reaction context before M236 source-quality boosts are applied.",
    }


def _learning_velocity(records: list[dict[str, Any]], states: dict[str, dict[str, Any]], attachment_report_path: Path = Path("exports/m236_outcome_attachment_report.json")) -> dict[str, Any]:
    pending = sum(1 for state in states.values() if state.get("status") == "pending_market_outcome")
    ready = sum(1 for state in states.values() if state.get("status") == "ready_for_outcome_attachment")
    evaluated = sum(1 for state in states.values() if state.get("status") == "evaluated")
    due_horizons = sum(len(state.get("due_horizons") or []) for state in states.values())
    attachment = _read_json(attachment_report_path)
    attached_records = int(((attachment.get("summary") or {}).get("attached_records") or 0))
    processed_records = int(((attachment.get("summary") or {}).get("processed_records") or 0))
    estimated_cycles = None
    if processed_records > 0:
        estimated_cycles = round(ready / max(1, processed_records), 2)
    return {
        "pending_record_count": pending,
        "ready_for_attachment_record_count": ready,
        "evaluated_record_count": evaluated,
        "due_horizon_total": due_horizons,
        "last_attachment_processed_records": processed_records,
        "last_attachment_attached_records": attached_records,
        "estimated_cycles_to_drain_ready_backlog": estimated_cycles,
        "dashboard_policy": "Show bootstrap/evaluated/pending backlog, attachment velocity, and provider hit-rate readiness.",
    }


def _scorer_impact_summary(records: list[dict[str, Any]], states: dict[str, dict[str, Any]]) -> dict[str, Any]:
    directives: list[dict[str, Any]] = []
    reason_counts = Counter()
    for record in records:
        state = states.get(str(record.get("learning_record_key") or "")) or {}
        directive = _scorer_impact(record, state)
        reason_counts.update([str(directive.get("reason") or "unknown")])
        directives.append(
            {
                "learning_record_key": record.get("learning_record_key"),
                "canonical_event_id": record.get("canonical_event_id"),
                "target_assets": record.get("target_assets") or [],
                "provider_agreement": record.get("provider_agreement"),
                "role_family": record.get("role_family"),
                **directive,
            }
        )
    return {
        "directive_counts": dict(reason_counts),
        "top_directives": directives[:50],
        "policy": {
            "direction_boost": "Small source-quality nudge only; final up/down remains scorer-owned.",
            "confidence_boost": "Capped by adaptive source/ticker/canonical quality.",
            "volatility_warning": "Conflict increases caution, not directional conviction.",
            "do_not_trade_noise": "Noise rows should not independently trigger action.",
            "requires_market_confirmation": "Use market data/options/economic evidence before acting.",
        },
    }


def build_m236_news_learning_report(config: M236NewsLearningConfig) -> dict[str, Any]:
    generated_at = _iso_now()
    event_roles = _read_json(config.event_roles_json_path)
    records, retention = _load_learning_records(config)
    now = datetime.now(UTC)
    states = {
        str(record.get("learning_record_key") or ""): _record_outcome_state(
            record,
            now=now,
            flat_threshold=config.flat_return_threshold,
        )
        for record in records
    }
    status_counts = Counter(str(state.get("status") or "unknown") for state in states.values())
    due_horizon_counts = Counter(h for state in states.values() for h in (state.get("due_horizons") or []))
    provider_profile = _provider_profile(records, states)
    provider_role_family_profile = _provider_role_family_profile(records, states)
    ticker = _ticker_verification(records, states)
    canonical = _canonical_dedup(records)
    scorer_impact = _scorer_impact_summary(records, states)
    macro_release = _macro_release_integration(records, config.macro_release_interpreter_json_path)
    signal_classification_review = _signal_classification_review(config.signal_classification_notes_jsonl_path)
    learning_velocity = _learning_velocity(records, states)
    evaluated_index_path = _evaluated_index_path_for(config)
    evaluated_index = _write_evaluated_index(
        evaluated_index_path,
        records,
        max_records=config.max_evaluated_index_records,
    )
    learning_plan = {
        "objective": "Reduce scorer input noise by learning source quality, ticker inference reliability, canonical freshness, and cross-provider conflict before final market-direction scoring.",
        "separation_from_scorer": {
            "m236_learns": ["provider reliability", "ticker inference confidence", "canonical event freshness", "external sentiment role", "noise/conflict penalties"],
            "scorer_learns": ["up/down/neutral", "expected move", "confidence threshold", "action gate timing"],
        },
        "cycle": [
            "Collect BZ/DJ/IBKR/Marketaux/emergency news; Alpha Vantage is excluded from active training.",
            "Build M236 source-quality packet and pending outcome records.",
            "Attach 5M/15M/30M/EOD market outcomes when available.",
            "Update provider/ticker/canonical source-quality profiles.",
            "Feed only source-quality directives into scorer evidence packets.",
            "Keep all promotion shadow-only until scorer/action-gate promotion checks pass.",
        ],
        "llm_usage": {
            "online_fast_path": "Avoid LLM before reflex/scorer when latency matters.",
            "post_outcome": "Use LLM for adjustment notes explaining source-quality or ticker-mapping failures.",
        },
    }
    return {
        "m236_news_learning_version": M236_NEWS_LEARNING_VERSION,
        "generated_at": generated_at,
        "generated_at_local": _local_time_text(generated_at),
        "status": "ok",
        "source_artifacts": {
            "event_roles_json_path": str(config.event_roles_json_path),
            "event_roles_version": event_roles.get("version"),
            "outcome_learning_jsonl_path": str(config.outcome_learning_jsonl_path),
            "provider_profile_output_path": str(config.provider_profile_output_path),
            "evaluated_index_jsonl_path": str(evaluated_index_path),
            "macro_release_interpreter_json_path": str(config.macro_release_interpreter_json_path),
            "signal_classification_notes_jsonl_path": str(config.signal_classification_notes_jsonl_path),
        },
        "summary": {
            "record_count": len(records),
            "tail_record_count": retention.get("tail_record_count", 0),
            "evaluated_index_record_count": retention.get("evaluated_index_record_count", 0),
            "evaluated_retained_record_count": evaluated_index.get("final_evaluated_index_count", 0),
            "status_counts": dict(status_counts),
            "due_horizon_counts": dict(due_horizon_counts),
            "provider_family_count": len(provider_profile),
            "ticker_review_queue_count": ticker.get("review_queue_count", 0),
            "repeated_canonical_event_count": canonical.get("repeated_canonical_event_count", 0),
            "scorer_directive_counts": scorer_impact.get("directive_counts") or {},
            "signal_classification_note_count": signal_classification_review.get("note_count", 0),
        },
        "retention": retention,
        "evaluated_index": evaluated_index,
        "provider_profile": provider_profile,
        "provider_role_family_profile": provider_role_family_profile,
        "ticker_inference_verification": ticker,
        "canonical_dedup": canonical,
        "macro_release_integration": macro_release,
        "signal_classification_review": signal_classification_review,
        "learning_velocity": learning_velocity,
        "scorer_impact": scorer_impact,
        "learning_plan": learning_plan,
        "safety": {
            "read_only_report": True,
            "mutates_evaluated_index_sidecar": True,
            "broker_orders_submitted": False,
            "transmit_enabled": False,
            "mutates_live_scorer": False,
            "outcome_leakage_guard": "Pending records contain no future outcomes; outcome attachment must run after horizon completion.",
        },
    }


def attach_m236_news_outcomes(config: M236OutcomeAttachmentConfig, price_source: IntradayPriceSource | None = None) -> dict[str, Any]:
    generated_at = _iso_now()
    prefix, rows = _read_jsonl_with_prefix(config.outcome_learning_jsonl_path, config.lookback_records)
    now_dt = datetime.now(UTC)
    pre_states = {
        str(record.get("learning_record_key") or ""): _record_outcome_state(
            record,
            now=now_dt,
            flat_threshold=config.flat_return_threshold,
        )
        for record in rows
        if "_raw_unparseable_line" not in record and "_raw_non_object_line" not in record
    }
    ready_backlog = sum(1 for state in pre_states.values() if state.get("status") == "ready_for_outcome_attachment")
    effective_max_records = int(config.max_records)
    effective_max_symbols = int(config.max_symbols)
    adaptive_tier = "off"
    if config.adaptive_batch_enabled:
        adaptive_tier = "base"
        if ready_backlog >= int(config.adaptive_backlog_high):
            adaptive_tier = "high_backlog"
            effective_max_records = max(effective_max_records, int(config.adaptive_max_records_high))
            effective_max_symbols = max(effective_max_symbols, int(config.adaptive_max_symbols_high))
        elif ready_backlog >= int(config.adaptive_backlog_mid):
            adaptive_tier = "mid_backlog"
            effective_max_records = max(effective_max_records, int(config.adaptive_max_records_mid))
            effective_max_symbols = max(effective_max_symbols, int(config.adaptive_max_symbols_mid))
    if price_source is None:
        return {
            "m236_outcome_attachment_version": M236_OUTCOME_ATTACHMENT_VERSION,
            "generated_at": generated_at,
            "generated_at_local": _local_time_text(generated_at),
            "status": "blocked",
            "reason": "price_source_not_configured",
            "record_count": len(rows),
            "ready_backlog_record_count": ready_backlog,
            "adaptive_batch": {
                "enabled": bool(config.adaptive_batch_enabled),
                "tier": adaptive_tier,
                "effective_max_records": effective_max_records,
                "effective_max_symbols": effective_max_symbols,
            },
            "safety": {
                "read_only_when_no_price_source": True,
                "broker_orders_submitted": False,
                "transmit_enabled": False,
            },
        }
    states = pre_states
    allowed_horizons = tuple(dict.fromkeys(str(h).strip().upper() for h in config.horizons if str(h).strip()))
    bar_cache: dict[tuple[str, str, str], list[IntradayBar]] = {}
    processed_records = 0
    attached_records = 0
    attached_horizons = 0
    blocked_records = 0
    skipped_records = 0
    touched_symbols: set[str] = set()
    action_rows: list[dict[str, Any]] = []
    updated_rows = list(rows)
    for index, record in enumerate(rows):
        if processed_records >= effective_max_records:
            break
        if "_raw_unparseable_line" in record or "_raw_non_object_line" in record:
            continue
        key = str(record.get("learning_record_key") or "")
        state = states.get(key) or {}
        due = [h for h in allowed_horizons if h in set(state.get("due_horizons") or [])]
        if not due:
            skipped_records += 1
            continue
        symbols = _safe_asset_candidates(record)
        if not symbols:
            skipped_records += 1
            action_rows.append(
                {
                    "learning_record_key": key,
                    "status": "skipped",
                    "reason": "no_reliable_target_asset",
                    "due_horizons": due,
                    "role_family": record.get("role_family"),
                    "target_assets": record.get("target_assets") or [],
                }
            )
            continue
        if symbols and symbols[0] not in touched_symbols and len(touched_symbols) >= effective_max_symbols:
            skipped_records += 1
            action_rows.append(
                {
                    "learning_record_key": key,
                    "status": "skipped",
                    "reason": "max_symbols_budget_reached",
                    "candidate_symbol": symbols[0],
                }
            )
            continue
        processed_records += 1
        touched_symbols.add(symbols[0])
        if config.dry_run:
            action_rows.append(
                {
                    "learning_record_key": key,
                    "status": "dry_run_due",
                    "candidate_symbol": symbols[0] if symbols else None,
                    "due_horizons": due,
                }
            )
            continue
        updated, result = _attach_record_outcomes(
            record,
            price_source=price_source,
            bar_cache=bar_cache,
            horizons=tuple(due),
            flat_threshold=config.flat_return_threshold,
        )
        updated_rows[index] = updated
        action_rows.append({"learning_record_key": key, **result})
        if result.get("status") == "attached":
            attached_records += 1
            attached_horizons += int(result.get("attached_horizon_count") or 0)
        elif result.get("status") == "blocked":
            blocked_records += 1
        else:
            skipped_records += 1
    if not config.dry_run:
        _write_jsonl_with_prefix(config.outcome_learning_jsonl_path, prefix, updated_rows)
    status_counts = Counter(str(item.get("status") or "unknown") for item in action_rows)
    return {
        "m236_outcome_attachment_version": M236_OUTCOME_ATTACHMENT_VERSION,
        "generated_at": generated_at,
        "generated_at_local": _local_time_text(generated_at),
        "status": "ok",
        "dry_run": bool(config.dry_run),
        "price_source": getattr(price_source, "name", "unknown_intraday"),
        "config": {
            "max_records": int(config.max_records),
            "max_symbols": int(config.max_symbols),
            "effective_max_records": effective_max_records,
            "effective_max_symbols": effective_max_symbols,
            "horizons": list(allowed_horizons),
            "lookback_records": int(config.lookback_records),
            "flat_return_threshold": float(config.flat_return_threshold),
            "adaptive_batch_enabled": bool(config.adaptive_batch_enabled),
        },
        "summary": {
            "record_count": len(rows),
            "ready_backlog_record_count": ready_backlog,
            "adaptive_batch_tier": adaptive_tier,
            "processed_records": processed_records,
            "attached_records": attached_records,
            "attached_horizons": attached_horizons,
            "blocked_records": blocked_records,
            "skipped_records": skipped_records,
            "touched_symbols": sorted(touched_symbols),
            "action_status_counts": dict(status_counts),
        },
        "adaptive_batch": {
            "enabled": bool(config.adaptive_batch_enabled),
            "tier": adaptive_tier,
            "backlog_mid_threshold": int(config.adaptive_backlog_mid),
            "backlog_high_threshold": int(config.adaptive_backlog_high),
            "effective_max_records": effective_max_records,
            "effective_max_symbols": effective_max_symbols,
            "policy": "Increase bounded read-only outcome attachment batch size only when due backlog is large; no broker orders are submitted.",
        },
        "actions": action_rows[:100],
        "safety": {
            "read_only_price_fetch": True,
            "mutates_outcome_learning_jsonl": not bool(config.dry_run),
            "broker_orders_submitted": False,
            "transmit_enabled": False,
            "live_order_routing_enabled": False,
        },
    }


def render_m236_outcome_attachment_markdown(payload: dict[str, Any]) -> str:
    summary = payload.get("summary") or {}
    lines = [
        "# M236 News Outcome Attachment",
        "",
        f"- Version: `{payload.get('m236_outcome_attachment_version')}`",
        f"- Generated: `{payload.get('generated_at_local') or payload.get('generated_at')}`",
        f"- Status: `{payload.get('status')}`",
        f"- Dry run: `{payload.get('dry_run')}`",
        f"- Price source: `{payload.get('price_source')}`",
        f"- Processed / attached records: `{summary.get('processed_records')}` / `{summary.get('attached_records')}`",
        f"- Attached horizons: `{summary.get('attached_horizons')}`",
        f"- Blocked / skipped: `{summary.get('blocked_records')}` / `{summary.get('skipped_records')}`",
        f"- Touched symbols: `{summary.get('touched_symbols') or []}`",
        f"- Action statuses: `{summary.get('action_status_counts') or {}}`",
        "",
        "## Sample actions",
        "",
    ]
    for action in (payload.get("actions") or [])[:20]:
        lines.append(
            f"- `{action.get('status')}` `{action.get('learning_record_key')}` "
            f"symbol `{action.get('symbol') or action.get('candidate_symbol') or '-'}` "
            f"attached `{action.get('attached_horizon_count') or 0}` blocked `{action.get('blocked_horizons') or {}}`"
        )
    lines.extend(
        [
            "",
            "## Safety",
            "",
            "- Read-only price fetch; no broker orders or transmit.",
            "- Mutates only M236 outcome-learning JSONL when dry_run=false.",
        ]
    )
    return "\n".join(lines) + "\n"


def render_m236_news_learning_markdown(payload: dict[str, Any]) -> str:
    summary = payload.get("summary") or {}
    lines = [
        "# M236 News Source-Quality Learning",
        "",
        f"- Version: `{payload.get('m236_news_learning_version')}`",
        f"- Generated: `{payload.get('generated_at_local') or payload.get('generated_at')}`",
        f"- Status: `{payload.get('status')}`",
        f"- Records: `{summary.get('record_count')}`",
        f"- Status counts: `{summary.get('status_counts') or {}}`",
        f"- Due horizons: `{summary.get('due_horizon_counts') or {}}`",
        f"- Provider families: `{summary.get('provider_family_count')}`",
        f"- Ticker review queue: `{summary.get('ticker_review_queue_count')}`",
        f"- Repeated canonical events: `{summary.get('repeated_canonical_event_count')}`",
        f"- Scorer directives: `{summary.get('scorer_directive_counts') or {}}`",
        "",
        "## Provider profile",
        "",
    ]
    for family, profile in (payload.get("provider_profile") or {}).items():
        lines.append(
            f"- `{family}` samples `{profile.get('sample_count')}` evaluated `{profile.get('evaluated_horizon_count')}` "
            f"hit `{profile.get('hit_rate')}` weight `{(profile.get('learned_weight') or {}).get('overall')}` "
            f"status `{profile.get('learning_status')}`"
        )
    lines.extend(
        [
            "",
            "## Scorer impact policy",
            "",
        ]
    )
    for key, value in (payload.get("scorer_impact") or {}).get("policy", {}).items():
        lines.append(f"- `{key}`: {value}")
    lines.extend(
        [
            "",
            "## Learning plan",
            "",
        ]
    )
    for step in (payload.get("learning_plan") or {}).get("cycle", []):
        lines.append(f"- {step}")
    lines.extend(
        [
            "",
            "## Safety",
            "",
            "- Read-only source-quality learning report.",
            "- No broker orders, transmit, or live scorer mutation.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_m236_news_learning_json(path: Path, payload: dict[str, Any]) -> Path:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return resolved


def write_m236_news_learning_markdown(path: Path, payload: dict[str, Any]) -> Path:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(render_m236_news_learning_markdown(payload), encoding="utf-8")
    return resolved


def write_m236_provider_profile(path: Path, payload: dict[str, Any]) -> Path:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    profile = {
        "m236_provider_weight_profile_version": "m236-provider-weight-profile-v1",
        "generated_at": payload.get("generated_at"),
        "generated_at_local": payload.get("generated_at_local"),
        "provider_profile": payload.get("provider_profile") or {},
        "safety": payload.get("safety") or {},
    }
    resolved.write_text(json.dumps(profile, indent=2, sort_keys=True), encoding="utf-8")
    return resolved


def write_m236_outcome_attachment_json(path: Path, payload: dict[str, Any]) -> Path:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return resolved


def write_m236_outcome_attachment_markdown(path: Path, payload: dict[str, Any]) -> Path:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(render_m236_outcome_attachment_markdown(payload), encoding="utf-8")
    return resolved
