from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from .database import parse_utc
from .market_outcomes import IntradayBar, _first_intraday_geq, _latest_intraday_leq

OPTION_STRUCTURE_OUTCOME_VERSION = "m222-option-structure-outcome-log-v1"

OPTION_OUTCOME_PROXY_FALLBACKS: dict[str, list[str]] = {
    "SPX": ["XSP", "SPY"],
    "NDX": ["QQQ"],
    "XND": ["QQQ"],
    "RUT": ["IWM"],
    "VIX": ["VXX", "UVXY"],
    "VIXY": ["VXX", "UVXY"],
    "DIA": ["SPY", "QQQ"],
}


class IntradayPriceSource(Protocol):
    name: str

    def get_intraday_bars(self, symbol: str, start: datetime, end: datetime) -> list[IntradayBar]:
        ...


@dataclass(frozen=True)
class OptionStructureOutcomeLog:
    snapshot_id: str
    symbol: str
    source_quote_generated_at: str
    structure_generated_at: str
    horizon: str
    baseline_at: str | None
    reaction_at: str | None
    baseline_price: float | None
    reaction_price: float | None
    raw_return: float | None
    direction_label: str
    price_source: str
    price_status: str
    blocker_reason: str
    gamma_regime: str
    total_gamma_exposure: float | None
    gamma_flip: float | None
    call_wall: float | None
    put_wall: float | None
    max_pain: float | None
    distance_to_pin_pct: float | None
    pin_risk: bool
    proximity_alerts: list[str]
    expiry_bucket_summary: list[dict[str, Any]]
    confidence: str
    confidence_score: float | None
    warnings: list[str]
    structure: dict[str, Any]
    outcome_version: str = OPTION_STRUCTURE_OUTCOME_VERSION


def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_horizons(value: str | list[str] | tuple[str, ...]) -> tuple[str, ...]:
    parts = value.split(",") if isinstance(value, str) else list(value)
    horizons: list[str] = []
    for item in parts:
        normalized = str(item).strip().upper()
        if not normalized:
            continue
        if normalized in {"EOD", "CLOSE", "SESSION_CLOSE"}:
            horizons.append("EOD")
            continue
        if normalized.endswith(("M", "H")):
            amount = int(normalized[:-1])
            if amount <= 0:
                raise ValueError(f"Unsupported horizon {item!r}; horizon must be positive")
            horizons.append(f"{amount}{normalized[-1]}")
            continue
        raise ValueError(f"Unsupported M222 horizon {item!r}; use values like 5M, 15M, 30M, or EOD")
    if not horizons:
        raise ValueError("At least one M222 horizon is required")
    return tuple(dict.fromkeys(horizons))


def _horizon_target(snapshot_at: datetime, horizon: str) -> datetime:
    normalized = horizon.strip().upper()
    if normalized == "EOD":
        eastern = ZoneInfo("America/New_York")
        local_snapshot = snapshot_at.astimezone(eastern)
        close_local = datetime.combine(local_snapshot.date(), time(16, 0), tzinfo=eastern)
        if local_snapshot > close_local:
            close_local = close_local + timedelta(days=1)
        return close_local.astimezone(UTC)
    amount = int(normalized[:-1])
    if normalized.endswith("M"):
        return snapshot_at + timedelta(minutes=amount)
    if normalized.endswith("H"):
        return snapshot_at + timedelta(hours=amount)
    raise ValueError(f"Unsupported M222 horizon {horizon!r}")


def _direction_label(raw_return: float | None, threshold: float) -> str:
    if raw_return is None:
        return "unknown"
    if raw_return >= threshold:
        return "up"
    if raw_return <= -threshold:
        return "down"
    return "flat"


def option_structure_snapshot_id(report: dict[str, Any]) -> str:
    payload = {
        "version": report.get("options_market_structure_version"),
        "generated_at": report.get("generated_at"),
        "source_quote_snapshot_version": report.get("source_quote_snapshot_version"),
        "source_quote_generated_at": report.get("source_quote_generated_at"),
        "symbols": [
            {
                "symbol": item.get("symbol"),
                "spot": item.get("spot"),
                "gamma_regime": item.get("gamma_regime"),
                "total_gamma_exposure": item.get("total_gamma_exposure"),
                "gamma_flip": item.get("gamma_flip"),
                "call_wall": item.get("call_wall"),
                "put_wall": item.get("put_wall"),
                "max_pain": item.get("max_pain"),
                "distance_to_pin_pct": item.get("distance_to_pin_pct"),
                "proximity_alerts": item.get("proximity_alerts") or [],
            }
            for item in report.get("symbols") or []
            if isinstance(item, dict)
        ],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]


def _snapshot_datetime(report: dict[str, Any]) -> datetime:
    candidate = report.get("source_quote_generated_at") or report.get("generated_at")
    if candidate:
        return parse_utc(str(candidate))
    return datetime.now(UTC)


def _expiry_bucket_summary(symbol_row: dict[str, Any]) -> list[dict[str, Any]]:
    summary: list[dict[str, Any]] = []
    for bucket in symbol_row.get("expiry_buckets") or []:
        if not isinstance(bucket, dict):
            continue
        summary.append(
            {
                "bucket": bucket.get("bucket"),
                "quote_count": bucket.get("quote_count"),
                "min_dte": bucket.get("min_dte"),
                "max_dte": bucket.get("max_dte"),
                "gamma_regime": bucket.get("gamma_regime"),
                "total_gamma_exposure": bucket.get("total_gamma_exposure"),
                "call_wall": bucket.get("call_wall"),
                "put_wall": bucket.get("put_wall"),
                "max_pain": bucket.get("max_pain"),
                "pin_risk": bucket.get("pin_risk"),
                "proximity_alerts": bucket.get("proximity_alerts") or [],
            }
        )
    return summary[:20]


def _compact_structure(symbol_row: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": symbol_row.get("symbol"),
        "spot": symbol_row.get("spot"),
        "quote_count": symbol_row.get("quote_count"),
        "strike_count": symbol_row.get("strike_count"),
        "gamma_regime": symbol_row.get("gamma_regime"),
        "total_gamma_exposure": symbol_row.get("total_gamma_exposure"),
        "gamma_flip": symbol_row.get("gamma_flip"),
        "call_wall": symbol_row.get("call_wall"),
        "put_wall": symbol_row.get("put_wall"),
        "max_pain": symbol_row.get("max_pain"),
        "distance_to_pin_pct": symbol_row.get("distance_to_pin_pct"),
        "pin_risk": symbol_row.get("pin_risk"),
        "proximity": symbol_row.get("proximity") or {},
        "proximity_alerts": symbol_row.get("proximity_alerts") or [],
        "expiry_buckets": _expiry_bucket_summary(symbol_row),
        "confidence": symbol_row.get("confidence"),
        "confidence_score": symbol_row.get("confidence_score"),
        "warnings": symbol_row.get("warnings") or [],
    }


class OptionStructureOutcomeLogger:
    def __init__(
        self,
        database: Any,
        price_source: IntradayPriceSource | None = None,
        *,
        now: datetime | None = None,
    ) -> None:
        self.database = database
        self.price_source = price_source
        self.now = now

    def build(
        self,
        *,
        options_market_structure: dict[str, Any],
        horizons: str | tuple[str, ...] | list[str] = ("5M", "15M", "30M", "EOD"),
        flat_threshold: float = 0.001,
        symbols: str | list[str] | tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        parsed_horizons = _parse_horizons(horizons)
        requested_symbols = None
        if symbols:
            requested_symbols = {
                str(item).strip().upper()
                for item in (symbols.split(",") if isinstance(symbols, str) else symbols)
                if str(item).strip()
            }
        snapshot_id = option_structure_snapshot_id(options_market_structure)
        snapshot_at = _snapshot_datetime(options_market_structure)
        source_quote_generated_at = str(options_market_structure.get("source_quote_generated_at") or "")
        structure_generated_at = str(options_market_structure.get("generated_at") or "")
        rows = [
            row
            for row in options_market_structure.get("symbols") or []
            if isinstance(row, dict)
            and str(row.get("symbol") or "").strip()
            and (requested_symbols is None or str(row.get("symbol") or "").strip().upper() in requested_symbols)
        ]
        summary: dict[str, Any] = {
            "option_structure_outcome_version": OPTION_STRUCTURE_OUTCOME_VERSION,
            "snapshot_id": snapshot_id,
            "source_quote_generated_at": source_quote_generated_at,
            "structure_generated_at": structure_generated_at,
            "price_source": getattr(self.price_source, "name", "none"),
            "horizons": list(parsed_horizons),
            "symbol_count": len(rows),
            "created": 0,
            "updated": 0,
            "completed": 0,
            "pending": 0,
            "blocked": 0,
            "price_source_errors": {},
            "rows": [],
            "safety": {
                "read_only": True,
                "ibkr_order_submitted": False,
                "transmit_enabled": False,
                "live_order_routing_enabled": False,
                "portfolio_mutated": False,
            },
        }
        max_target = max(_horizon_target(snapshot_at, horizon) for horizon in parsed_horizons)
        try:
            for symbol_row in rows:
                symbol = str(symbol_row.get("symbol") or "").strip().upper()
                bars: list[IntradayBar] = []
                if self.price_source is not None:
                    try:
                        bars = self.price_source.get_intraday_bars(
                            symbol,
                            snapshot_at - timedelta(minutes=30),
                            max_target + timedelta(minutes=30),
                        )
                    except Exception as exc:  # pragma: no cover - exercised by live connectors
                        summary["price_source_errors"][symbol] = str(exc)
                for horizon in parsed_horizons:
                    log = self._build_log(
                        snapshot_id=snapshot_id,
                        source_quote_generated_at=source_quote_generated_at,
                        structure_generated_at=structure_generated_at,
                        snapshot_at=snapshot_at,
                        symbol_row=symbol_row,
                        bars=bars,
                        horizon=horizon,
                        flat_threshold=flat_threshold,
                    )
                    log_id, created = self.database.upsert_option_structure_outcome_log(log)
                    summary["created" if created else "updated"] += 1
                    if log.price_status == "complete":
                        summary["completed"] += 1
                    elif log.price_status == "pending":
                        summary["pending"] += 1
                    else:
                        summary["blocked"] += 1
                    summary["rows"].append(
                        {
                            "log_id": log_id,
                            "snapshot_id": log.snapshot_id,
                            "symbol": log.symbol,
                            "horizon": log.horizon,
                            "gamma_regime": log.gamma_regime,
                            "max_pain": log.max_pain,
                            "call_wall": log.call_wall,
                            "put_wall": log.put_wall,
                            "baseline_at": log.baseline_at,
                            "reaction_at": log.reaction_at,
                            "raw_return": log.raw_return,
                            "direction_label": log.direction_label,
                            "price_status": log.price_status,
                            "blocker_reason": log.blocker_reason,
                        }
                    )
            completed_symbols = {
                str(row.get("symbol") or "").upper()
                for row in summary.get("rows") or []
                if row.get("price_status") == "complete"
            }
            blocked_symbols = sorted(
                {
                    str(row.get("symbol") or "").upper()
                    for row in summary.get("rows") or []
                    if row.get("price_status") == "blocked"
                }
            )
            recommendations = []
            fallback_rows = []
            for symbol in blocked_symbols:
                candidates = OPTION_OUTCOME_PROXY_FALLBACKS.get(symbol, [])
                available = [candidate for candidate in candidates if candidate in completed_symbols]
                completed_rows = [
                    row
                    for row in summary.get("rows") or []
                    if row.get("price_status") == "complete" and str(row.get("symbol") or "").upper() in set(available)
                ]
                by_horizon: dict[str, dict[str, Any]] = {}
                for row in completed_rows:
                    horizon = str(row.get("horizon") or "")
                    current = by_horizon.get(horizon)
                    if current is None or abs(float(row.get("raw_return") or 0.0)) > abs(float(current.get("raw_return") or 0.0)):
                        by_horizon[horizon] = row
                for horizon, row in sorted(by_horizon.items()):
                    fallback_rows.append(
                        {
                            "source_symbol": symbol,
                            "fallback_symbol": row.get("symbol"),
                            "horizon": horizon,
                            "fallback_direction_label": row.get("direction_label"),
                            "fallback_raw_return": row.get("raw_return"),
                            "source_status": "blocked",
                            "proxy_outcome_status": "completed_proxy_available",
                            "recommended_use": "use_as_family_proxy_context_not_symbol_exact_truth",
                        }
                    )
                recommendations.append(
                    {
                        "symbol": symbol,
                        "fallback_candidates": candidates,
                        "completed_fallback_symbols": available,
                        "proxy_outcome_status": "completed_proxy_available" if available else "pending_proxy_collection",
                        "recommended_action": (
                            "use_completed_family_proxy_outcome_for_learning_context"
                            if available
                            else "collect_or_build_family_proxy_outcome"
                        ),
                        "scope": "family_proxy_outcome_fallback",
                    }
                )
            summary["family_proxy_fallback_recommendations"] = recommendations
            summary["family_proxy_fallback_rows"] = fallback_rows
            summary["family_proxy_fallback_count"] = len(recommendations)
            summary["family_proxy_completed_fallback_row_count"] = len(fallback_rows)
            return summary
        finally:
            close = getattr(self.price_source, "close", None)
            if callable(close):
                close()

    def _build_log(
        self,
        *,
        snapshot_id: str,
        source_quote_generated_at: str,
        structure_generated_at: str,
        snapshot_at: datetime,
        symbol_row: dict[str, Any],
        bars: list[IntradayBar],
        horizon: str,
        flat_threshold: float,
    ) -> OptionStructureOutcomeLog:
        symbol = str(symbol_row.get("symbol") or "").strip().upper()
        target = _horizon_target(snapshot_at, horizon)
        now = self.now or datetime.now(UTC)
        blockers: list[str] = []
        baseline_bar = _latest_intraday_leq(bars, snapshot_at) if bars else None
        reaction_bar = _first_intraday_geq(bars, target) if bars else None
        baseline_at = baseline_bar.timestamp.isoformat() if baseline_bar else None
        baseline_price = baseline_bar.close if baseline_bar else None
        if baseline_price is None and _num(symbol_row.get("spot")) is not None:
            baseline_at = snapshot_at.isoformat()
            baseline_price = _num(symbol_row.get("spot"))
            blockers.append("baseline_from_option_structure_spot")
        elif baseline_price is None:
            blockers.append("baseline_price_unavailable")
        if self.price_source is None:
            blockers.append("price_source_not_configured")
        reaction_not_due = reaction_bar is None and target > now
        if reaction_bar is None:
            blockers.append("reaction_target_not_due" if reaction_not_due else "reaction_price_unavailable")

        raw_return = None
        if baseline_price is not None and reaction_bar is not None and baseline_price > 0:
            raw_return = reaction_bar.close / baseline_price - 1.0
        if raw_return is not None:
            status = "complete"
        elif self.price_source is None or reaction_not_due:
            status = "pending"
        else:
            status = "blocked"

        return OptionStructureOutcomeLog(
            snapshot_id=snapshot_id,
            symbol=symbol,
            source_quote_generated_at=source_quote_generated_at,
            structure_generated_at=structure_generated_at,
            horizon=horizon,
            baseline_at=baseline_at,
            reaction_at=reaction_bar.timestamp.isoformat() if reaction_bar else None,
            baseline_price=baseline_price,
            reaction_price=reaction_bar.close if reaction_bar else None,
            raw_return=raw_return,
            direction_label=_direction_label(raw_return, flat_threshold),
            price_source=getattr(self.price_source, "name", "none"),
            price_status=status,
            blocker_reason=";".join(dict.fromkeys(blockers)),
            gamma_regime=str(symbol_row.get("gamma_regime") or ""),
            total_gamma_exposure=_num(symbol_row.get("total_gamma_exposure")),
            gamma_flip=_num(symbol_row.get("gamma_flip")),
            call_wall=_num(symbol_row.get("call_wall")),
            put_wall=_num(symbol_row.get("put_wall")),
            max_pain=_num(symbol_row.get("max_pain")),
            distance_to_pin_pct=_num(symbol_row.get("distance_to_pin_pct")),
            pin_risk=bool(symbol_row.get("pin_risk")),
            proximity_alerts=[str(item) for item in symbol_row.get("proximity_alerts") or []],
            expiry_bucket_summary=_expiry_bucket_summary(symbol_row),
            confidence=str(symbol_row.get("confidence") or ""),
            confidence_score=_num(symbol_row.get("confidence_score")),
            warnings=[str(item) for item in symbol_row.get("warnings") or []],
            structure=_compact_structure(symbol_row),
        )


def option_structure_outcome_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# M222 Option-Structure Outcome Log",
        "",
        f"- Version: `{report.get('option_structure_outcome_version')}`",
        f"- Snapshot: `{report.get('snapshot_id')}` source `{report.get('source_quote_generated_at') or '-'}` structure `{report.get('structure_generated_at') or '-'}`",
        f"- Price source: `{report.get('price_source')}`",
        f"- Horizons: `{report.get('horizons') or []}`",
        f"- Symbols: `{report.get('symbol_count')}` completed `{report.get('completed')}` pending `{report.get('pending')}` blocked `{report.get('blocked')}` created `{report.get('created')}` updated `{report.get('updated')}`",
        "",
        "| Symbol | Horizon | Gamma | Walls | Max pain | Baseline | Reaction | Return | Direction | Status | Blocker |",
        "| --- | --- | --- | --- | ---: | --- | --- | ---: | --- | --- | --- |",
    ]
    for row in report.get("rows") or []:
        walls = f"C {row.get('call_wall') if row.get('call_wall') is not None else '-'} / P {row.get('put_wall') if row.get('put_wall') is not None else '-'}"
        raw_return = row.get("raw_return")
        return_text = "-" if raw_return is None else f"{float(raw_return) * 100.0:.3f}%"
        lines.append(
            f"| `{row.get('symbol')}` | `{row.get('horizon')}` | `{row.get('gamma_regime') or '-'}` | {walls} | "
            f"{row.get('max_pain') if row.get('max_pain') is not None else '-'} | `{row.get('baseline_at') or '-'}` | "
            f"`{row.get('reaction_at') or '-'}` | {return_text} | `{row.get('direction_label')}` | "
            f"`{row.get('price_status')}` | `{row.get('blocker_reason') or ''}` |"
        )
    if not report.get("rows"):
        lines.append("| - | - | - | - | - | - | - | - | - | - | - |")
    lines.extend(
        [
            "",
            "## Family proxy fallback recommendations",
            "",
            "| Symbol | Fallback candidates | Completed fallback symbols | Status | Action |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    for row in report.get("family_proxy_fallback_recommendations") or []:
        lines.append(
            f"| `{row.get('symbol')}` | `{row.get('fallback_candidates') or []}` | "
            f"`{row.get('completed_fallback_symbols') or []}` | `{row.get('proxy_outcome_status') or '-'}` | `{row.get('recommended_action')}` |"
        )
    if not report.get("family_proxy_fallback_recommendations"):
        lines.append("| - | - | - | - | - |")
    lines.extend(
        [
            "",
            "## Completed proxy fallback rows",
            "",
            "| Source | Proxy | Horizon | Direction | Return | Use |",
            "| --- | --- | --- | --- | ---: | --- |",
        ]
    )
    for row in report.get("family_proxy_fallback_rows") or []:
        raw_return = row.get("fallback_raw_return")
        return_text = "-" if raw_return is None else f"{float(raw_return) * 100.0:.3f}%"
        lines.append(
            f"| `{row.get('source_symbol')}` | `{row.get('fallback_symbol')}` | `{row.get('horizon')}` | "
            f"`{row.get('fallback_direction_label')}` | {return_text} | `{row.get('recommended_use')}` |"
        )
    if not report.get("family_proxy_fallback_rows"):
        lines.append("| - | - | - | - | - | - |")
    lines.extend(
        [
            "",
            "## Safety",
            "",
            "- Read-only outcome logging only.",
            "- No broker orders, transmit, live routing, or portfolio mutation.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_option_structure_outcome_json(output_path: Path, report: dict[str, Any]) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return output_path


def write_option_structure_outcome_markdown(output_path: Path, report: dict[str, Any]) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(option_structure_outcome_markdown(report), encoding="utf-8")
    return output_path
