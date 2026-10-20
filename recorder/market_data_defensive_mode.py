from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


MARKET_DATA_DEFENSIVE_MODE_VERSION = "market-data-defensive-operating-mode-v1"


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _parse_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


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


def _write_json(path: Path, payload: dict[str, Any]) -> Path:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return resolved


def _float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _signature(gate: dict[str, Any]) -> dict[str, Any]:
    market = gate.get("market_data") or {}
    counts = market.get("counts") or {}
    instruments = market.get("instruments") or {}
    return {
        "gate_status": gate.get("gate_status"),
        "action": gate.get("action"),
        "reason": gate.get("reason"),
        "blocking_reasons": sorted(str(item) for item in _list(gate.get("blocking_reasons"))),
        "defensive_reasons": sorted(str(item) for item in _list(gate.get("defensive_reasons"))),
        "haircut_reasons": sorted(str(item) for item in _list(gate.get("haircut_reasons"))),
        "temperature_label": market.get("temperature_label"),
        "risk_flags": sorted(str(item) for item in _list(market.get("risk_flags"))),
        "warming_up": sorted(str(item) for item in _list(instruments.get("warming_up"))),
        "delayed": sorted(str(item) for item in _list(instruments.get("delayed"))),
        "missing": sorted(str(item) for item in _list(instruments.get("missing"))),
        "errors": sorted(str(item) for item in _list(instruments.get("errors"))),
        "live_count": counts.get("live_count") or 0,
        "delayed_count": counts.get("delayed_count") or 0,
        "missing_price_count": counts.get("missing_price_count") or 0,
        "warming_up_count": counts.get("warming_up_count") or 0,
        "error_count": counts.get("error_count") or 0,
    }


def _signature_key(signature: dict[str, Any]) -> str:
    return json.dumps(signature, sort_keys=True, ensure_ascii=True)


def _load_state(path: Path | None) -> dict[str, Any]:
    data = _read_json(path)
    if data.get("market_data_defensive_mode_state_version") == MARKET_DATA_DEFENSIVE_MODE_VERSION:
        return data
    return {
        "market_data_defensive_mode_state_version": MARKET_DATA_DEFENSIVE_MODE_VERSION,
        "updated_at": None,
        "signatures": {},
    }


def _duration_state(
    *,
    state: dict[str, Any],
    signature_key: str,
    signature: dict[str, Any],
    now_text: str,
) -> dict[str, Any]:
    now_dt = _parse_time(now_text) or datetime.now(UTC)
    signatures = state.setdefault("signatures", {})
    previous = signatures.get(signature_key) or {}
    first_seen = _parse_time(previous.get("first_seen_at")) or now_dt
    seen_count = int(previous.get("seen_count") or 0) + 1
    duration_seconds = max(0.0, (now_dt - first_seen).total_seconds())
    current = {
        "signature": signature,
        "first_seen_at": first_seen.isoformat(),
        "last_seen_at": now_text,
        "seen_count": seen_count,
        "duration_seconds": round(duration_seconds, 3),
    }
    signatures[signature_key] = current
    state["updated_at"] = now_text
    return current


def _mode_for_gate(
    gate: dict[str, Any],
    *,
    persistent_warning_seconds: float,
    persistent_critical_seconds: float,
    duration_seconds: float,
) -> tuple[str, str]:
    status = str(gate.get("gate_status") or "").lower()
    action = str(gate.get("action") or "").lower()
    blocking = _list(gate.get("blocking_reasons"))
    defensive = _list(gate.get("defensive_reasons"))
    if status == "block" or action == "block_new_entries" or blocking:
        mode = "risk_reduction_only"
        severity = "red"
    elif status == "defensive" or action == "defensive_or_hedge_only" or defensive:
        mode = "hedge_defensive_only"
        severity = "red"
    elif status == "watch" or action in {"allow_with_market_data_haircut", "allow_with_session_context_haircut"}:
        mode = "reduced_size_defined_risk"
        severity = "yellow"
    elif status == "pass" or action == "allow_normal":
        mode = "normal"
        severity = "green"
    else:
        mode = "fail_closed_market_data_unknown"
        severity = "red"
    if severity == "red" and duration_seconds >= float(persistent_critical_seconds):
        mode = f"{mode}_persistent_critical"
    elif severity == "red" and duration_seconds >= float(persistent_warning_seconds):
        mode = f"{mode}_persistent_warning"
    return mode, severity


def _controls_for_mode(gate: dict[str, Any], mode: str, severity: str) -> dict[str, Any]:
    status = str(gate.get("gate_status") or "").lower()
    action = str(gate.get("action") or "").lower()
    blocking = _list(gate.get("blocking_reasons"))
    data_quality_block = bool(blocking)
    size_multiplier = _float(gate.get("entry_size_multiplier"), 0.0)
    defensive_multiplier = _float(gate.get("defensive_entry_size_multiplier"), 0.0)

    normal = mode == "normal"
    reduced = mode.startswith("reduced_size_defined_risk")
    defensive = mode.startswith("hedge_defensive_only")
    risk_reduction = mode.startswith("risk_reduction_only") or mode.startswith("fail_closed")

    ordinary_alpha_allowed = normal or reduced
    hedge_ticketing_allowed = (defensive or reduced or normal) and not data_quality_block
    protective_option_analysis_allowed = severity in {"yellow", "red"} or normal
    protective_option_ticketing_allowed = hedge_ticketing_allowed and not risk_reduction

    if risk_reduction:
        max_new_entry_multiplier = 0.0
    elif defensive:
        max_new_entry_multiplier = max(0.0, min(1.0, defensive_multiplier))
    elif reduced:
        max_new_entry_multiplier = max(0.0, min(1.0, size_multiplier))
    else:
        max_new_entry_multiplier = 1.0

    if risk_reduction:
        option_structures = [
            "protective_put_analysis_only",
            "collar_analysis_only",
            "put_spread_hedge_analysis_only",
            "covered_call_risk_reduction_analysis_only",
        ]
    elif defensive:
        option_structures = [
            "protective_put",
            "collar",
            "put_debit_spread_hedge",
            "defined_risk_index_or_sector_hedge",
        ]
    elif reduced:
        option_structures = [
            "defined_risk_debit_spread_preferred",
            "small_notional_long_option_only_if_liquid",
            "avoid_unhedged_large_delta_stock_adds",
        ]
    else:
        option_structures = ["normal_stock_or_option_policy"]

    return {
        "ordinary_alpha_entries_allowed": bool(ordinary_alpha_allowed),
        "hedge_or_defensive_entries_allowed": bool(hedge_ticketing_allowed),
        "exit_or_risk_reduction_allowed": True,
        "protective_option_analysis_allowed": bool(protective_option_analysis_allowed),
        "protective_option_ticketing_allowed": bool(protective_option_ticketing_allowed),
        "max_new_entry_size_multiplier": max_new_entry_multiplier,
        "ordinary_alpha_size_multiplier": max_new_entry_multiplier if ordinary_alpha_allowed else 0.0,
        "hedge_or_defensive_size_multiplier": max_new_entry_multiplier if hedge_ticketing_allowed else 0.0,
        "data_quality_block": data_quality_block,
        "source_gate_status": status,
        "source_gate_action": action,
        "allowed_option_structures": option_structures,
    }


def build_market_data_defensive_mode_report(
    market_data_risk_gate_payload: dict[str, Any] | None,
    *,
    state_path: Path | None = None,
    persistent_warning_seconds: float = 300.0,
    persistent_critical_seconds: float = 900.0,
    now: str | None = None,
) -> dict[str, Any]:
    gate = market_data_risk_gate_payload or {}
    now_text = now or _iso_now()
    state = _load_state(state_path)
    signature = _signature(gate)
    signature_key = _signature_key(signature)
    duration = _duration_state(state=state, signature_key=signature_key, signature=signature, now_text=now_text)
    if state_path is not None:
        _write_json(state_path, state)
    mode, severity = _mode_for_gate(
        gate,
        persistent_warning_seconds=persistent_warning_seconds,
        persistent_critical_seconds=persistent_critical_seconds,
        duration_seconds=_float(duration.get("duration_seconds")),
    )
    controls = _controls_for_mode(gate, mode, severity)
    market = gate.get("market_data") or {}
    session_context = gate.get("market_data_session_context") or {}
    counts = market.get("counts") or {}
    instruments = market.get("instruments") or {}

    recommendations: list[str] = []
    if controls["data_quality_block"]:
        recommendations.append("Do not create new risk tickets while market data quality is blocking.")
        recommendations.append("Allow exits and risk-reduction workflows only; keep protective options as analysis-only until quotes are clean.")
    elif mode.startswith("hedge_defensive_only"):
        recommendations.append("Block ordinary alpha entries and allow only explicit hedge/defensive candidates.")
        recommendations.append("Prefer protective puts, collars, and defined-risk debit spreads with live option quote validation.")
    elif mode.startswith("reduced_size_defined_risk"):
        recommendations.append("Allow reduced-size entries only and prefer defined-risk structures.")
        recommendations.append("Require stronger scorer/evidence confirmation before adding directional exposure.")
    elif mode == "normal":
        recommendations.append("Normal market-data operating mode.")
    else:
        recommendations.append("Fail closed until market-data gate is understood.")
    if _float(duration.get("duration_seconds")) >= float(persistent_warning_seconds) and severity == "red":
        recommendations.append("Persistent red condition: surface operator review and avoid repeated recovery loops without new evidence.")

    return {
        "market_data_defensive_mode_version": MARKET_DATA_DEFENSIVE_MODE_VERSION,
        "generated_at": now_text,
        "mode": mode,
        "severity": severity,
        "source_market_data_risk_gate_version": gate.get("market_data_risk_gate_version"),
        "signature_key": signature_key,
        "signature": signature,
        "duration": duration,
        "controls": controls,
        "market_data": {
            "status": market.get("status"),
            "temperature_label": market.get("temperature_label"),
            "temperature_score": market.get("temperature_score"),
            "counts": counts,
            "attention_instruments": {
                "warming_up": _list(instruments.get("warming_up")),
                "delayed": _list(instruments.get("delayed")),
                "missing": _list(instruments.get("missing")),
                "errors": _list(instruments.get("errors")),
                "severe_wide_spread": _list(instruments.get("severe_wide_spread")),
                "alerted": _list(instruments.get("alerted")),
            },
            "top_moves": _list(market.get("top_moves"))[:8],
        },
        "market_data_session_context": session_context,
        "session_adjusted_priority": bool(gate.get("session_adjusted_priority")),
        "scorer_overlay": {
            "directional_alpha_requires_extra_confirmation": severity in {"yellow", "red"},
            "raise_min_confidence": 0.05 if severity == "yellow" else (0.15 if severity == "red" else 0.0),
            "require_market_data_confirmation": severity in {"yellow", "red"},
            "require_evidence_packet_freshness": severity == "red",
            "prefer_defensive_or_hedged_expression": severity in {"yellow", "red"},
            "market_data_session_priority_class": session_context.get("session_priority_class"),
            "session_adjusted_priority": bool(gate.get("session_adjusted_priority")),
        },
        "recommendations": recommendations,
        "parameters": {
            "persistent_warning_seconds": float(persistent_warning_seconds),
            "persistent_critical_seconds": float(persistent_critical_seconds),
        },
        "safety": {
            "read_only_operating_mode": True,
            "broker_order_submitted": False,
            "transmit_enabled": False,
            "live_order_routing_enabled": False,
            "portfolio_mutated": False,
            "scorer_rows_mutated": False,
        },
    }


def build_market_data_defensive_mode_from_paths(
    *,
    market_data_risk_gate_path: Path,
    state_path: Path | None = None,
    persistent_warning_seconds: float = 300.0,
    persistent_critical_seconds: float = 900.0,
    now: str | None = None,
) -> dict[str, Any]:
    return build_market_data_defensive_mode_report(
        _read_json(market_data_risk_gate_path),
        state_path=state_path,
        persistent_warning_seconds=persistent_warning_seconds,
        persistent_critical_seconds=persistent_critical_seconds,
        now=now,
    )


def market_data_defensive_mode_markdown(report: dict[str, Any]) -> str:
    controls = report.get("controls") or {}
    duration = report.get("duration") or {}
    market = report.get("market_data") or {}
    attention = market.get("attention_instruments") or {}
    overlay = report.get("scorer_overlay") or {}
    session = report.get("market_data_session_context") or {}
    lines = [
        "# Market Data Defensive Operating Mode",
        "",
        f"- Version: `{report.get('market_data_defensive_mode_version')}`",
        f"- Generated at: `{report.get('generated_at')}`",
        f"- Mode: `{report.get('mode')}`",
        f"- Severity: `{report.get('severity')}`",
        f"- Duration seconds: `{duration.get('duration_seconds')}` first seen `{duration.get('first_seen_at')}` seen `{duration.get('seen_count')}`",
        f"- Market data: `{market.get('status')}` / `{market.get('temperature_label')}` score `{market.get('temperature_score')}`",
        f"- Market session priority class: `{session.get('session_priority_class') or '-'}` adjusted `{report.get('session_adjusted_priority')}`",
        "",
        "## Controls",
        "",
        f"- Ordinary alpha entries allowed: `{controls.get('ordinary_alpha_entries_allowed')}`",
        f"- Hedge/defensive entries allowed: `{controls.get('hedge_or_defensive_entries_allowed')}`",
        f"- Exit/risk reduction allowed: `{controls.get('exit_or_risk_reduction_allowed')}`",
        f"- Protective option analysis allowed: `{controls.get('protective_option_analysis_allowed')}`",
        f"- Protective option ticketing allowed: `{controls.get('protective_option_ticketing_allowed')}`",
        f"- Max new-entry size multiplier: `{controls.get('max_new_entry_size_multiplier')}`",
        f"- Allowed option structures: `{controls.get('allowed_option_structures') or []}`",
        "",
        "## Scorer overlay",
        "",
        f"- Raise min confidence: `{overlay.get('raise_min_confidence')}`",
        f"- Require market-data confirmation: `{overlay.get('require_market_data_confirmation')}`",
        f"- Prefer defensive/hedged expression: `{overlay.get('prefer_defensive_or_hedged_expression')}`",
        "",
        "## Attention instruments",
        "",
        f"- Warming up: `{','.join(str(item) for item in attention.get('warming_up') or []) or '-'}`",
        f"- Delayed: `{','.join(str(item) for item in attention.get('delayed') or []) or '-'}`",
        f"- Missing: `{','.join(str(item) for item in attention.get('missing') or []) or '-'}`",
        f"- Errors: `{','.join(str(item) for item in attention.get('errors') or []) or '-'}`",
        f"- Severe wide spread: `{','.join(str(item) for item in attention.get('severe_wide_spread') or []) or '-'}`",
        f"- Alerted: `{','.join(str(item) for item in attention.get('alerted') or []) or '-'}`",
        "",
        "## Recommendations",
        "",
    ]
    lines.extend(f"- {item}" for item in report.get("recommendations") or [])
    lines.extend(["", "## Safety", "", "- Read-only operating mode; no broker order submission, transmit, scorer mutation, or portfolio mutation."])
    return "\n".join(lines) + "\n"


def write_market_data_defensive_mode_json(output_path: Path, report: dict[str, Any]) -> Path:
    return _write_json(output_path, report)


def write_market_data_defensive_mode_markdown(output_path: Path, report: dict[str, Any]) -> Path:
    resolved = output_path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(market_data_defensive_mode_markdown(report), encoding="utf-8")
    return resolved
