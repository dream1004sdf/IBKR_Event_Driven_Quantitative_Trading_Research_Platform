from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from recorder.config import Settings
from .cli_profile_utils import (
    coalesce_profile_bool as _coalesce_profile_bool,
    coalesce_profile_path as _coalesce_profile_path,
    coalesce_profile_path_list as _coalesce_profile_path_list,
    coalesce_profile_value as _coalesce_profile_value,
    load_profile_json as _load_profile_json,
    profile_lookup as _profile_lookup,
    profile_path_list_value as _profile_path_list_value,
    profile_path_value as _profile_path_value,
)


from .cli_dashboard_hook_helpers import (
    _write_markdown_text_atomic,
    _run_watch_evidence_refresh_watchdog,
    _latency_budget_stage_policy,
    _latency_budget_artifact_skip,
    _latency_budget_read_json,
    _run_dashboard_source_ingest_health,
    _run_dashboard_market_data_session_overlay,
    _run_dashboard_market_data_risk_gate,
    _run_dashboard_session_transition_closed_loop,
    _run_dashboard_session_transition_release_governor,
    _run_dashboard_release_governed_recovery_closed_loop,
)

def _parse_path_list(value: str | None) -> list[Path]:
    if not value:
        return []
    return [Path(item.strip()) for item in value.split(",") if item.strip()]


def _write_component_json(path: Path | None, result: dict[str, Any], filters: dict[str, Any]) -> Path | None:
    if path is None:
        return None
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"result": result, "filters": filters}, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def _write_component_json_atomic(path: Path | None, result: dict[str, Any], filters: dict[str, Any]) -> Path | None:
    if path is None:
        return None
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"result": result, "filters": filters}, indent=2, ensure_ascii=False)
    tmp = path.with_name(f".{path.name}.{int(time.time() * 1000)}.tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(path)
    return path


def _load_operating_auto_gate_payload(
    operating_auto_gate_json_path: Path | None,
    *,
    require_operating_preflight: bool,
) -> tuple[dict[str, Any] | None, Path | None]:
    if operating_auto_gate_json_path is None:
        if require_operating_preflight:
            raise ValueError("M184 operating final preflight requires --operating-auto-gate-json or profile paths.operating_auto_gate_json")
        return None, None
    resolved = operating_auto_gate_json_path.expanduser().resolve()
    if not resolved.exists():
        if require_operating_preflight:
            raise FileNotFoundError(f"M184 operating auto-gate JSON not found: {resolved}")
        return None, resolved
    return json.loads(resolved.read_text(encoding="utf-8")), resolved


def _payload_result(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not payload:
        return {}
    return payload.get("result") if isinstance(payload.get("result"), dict) else payload


def _path_from_text(value: Any, profile_base: Path | None) -> Path | None:
    if value in {None, ""}:
        return None
    path = Path(str(value))
    if not path.is_absolute() and profile_base is not None:
        path = profile_base / path
    return path.expanduser().resolve()




def _build_watch_evidence_preflight_auto_gate(
    *,
    profile: dict[str, Any],
    profile_base: Path | None,
    source_auto_gate_json_path: Path,
    run_id: str,
) -> tuple[Path, dict[str, Any] | None, dict[str, Any]]:
    gate_profile = profile.get("evidence_preflight_gate") if isinstance(profile.get("evidence_preflight_gate"), dict) else {}
    if not bool(gate_profile.get("enabled", False)) or bool(gate_profile.get("ignore", False)):
        return source_auto_gate_json_path, None, {"enabled": bool(gate_profile.get("enabled", False)), "applied": False, "reason": "disabled_or_ignored"}

    from recorder.evidence_operations import (
        build_evidence_preflight_report,
        evidence_preflight_markdown,
        write_evidence_preflight_json,
    )
    from recorder.options_analysis import (
        autonomous_execution_gate_markdown,
        build_autonomous_execution_gate_report,
    )

    source_auto_payload = json.loads(source_auto_gate_json_path.read_text(encoding="utf-8"))
    source_auto_gate = _payload_result(source_auto_payload)
    source_filters = source_auto_payload.get("filters") if isinstance(source_auto_payload.get("filters"), dict) else {}
    source_params = source_auto_gate.get("gate_parameters") or {}

    freshness_path = _profile_path_value(
        gate_profile.get("freshness_report_json") or gate_profile.get("evidence_freshness_json") or "../exports/m152_evidence_freshness_desk.json",
        profile_base,
    )
    watchdog_path = _profile_path_value(
        gate_profile.get("watchdog_state_json") or gate_profile.get("evidence_refresh_watchdog_json") or "../exports/m154_evidence_refresh_watchdog_state.json",
        profile_base,
    )
    preflight_json_path = _profile_path_value(gate_profile.get("preflight_json_output_path") or "../exports/m156_evidence_preflight_latest.json", profile_base)
    preflight_md_path = _profile_path_value(gate_profile.get("preflight_output") or "../exports/m156_evidence_preflight_latest.md", profile_base)
    auto_gate_output_path = _profile_path_value(gate_profile.get("auto_gate_json_output_path") or "../exports/m156_evidence_auto_gate_latest.json", profile_base)
    auto_gate_md_path = _profile_path_value(gate_profile.get("auto_gate_output") or "../exports/m156_evidence_auto_gate_latest.md", profile_base)

    freshness_payload = json.loads(freshness_path.expanduser().resolve().read_text(encoding="utf-8")) if freshness_path and freshness_path.expanduser().resolve().exists() else {}
    watchdog_payload = json.loads(watchdog_path.expanduser().resolve().read_text(encoding="utf-8")) if watchdog_path and watchdog_path.expanduser().resolve().exists() else {}
    preflight = build_evidence_preflight_report(
        freshness_payload,
        watchdog_payload=watchdog_payload,
        block_on_stale=bool(gate_profile.get("block_on_stale", True)),
        block_on_offline=bool(gate_profile.get("block_on_offline", True)),
        block_on_watchdog_failure=bool(gate_profile.get("block_on_watchdog_failure", True)),
        allow_lagging_with_haircut=bool(gate_profile.get("allow_lagging_with_haircut", True)),
        lagging_size_multiplier=float(gate_profile.get("lagging_size_multiplier", 0.5)),
        min_source_weight=float(gate_profile.get("min_source_weight", 0.25)),
    )
    write_evidence_preflight_json(preflight_json_path, preflight)
    _write_markdown_text_atomic(preflight_md_path, evidence_preflight_markdown(preflight))

    preview_path = _path_from_text(gate_profile.get("preview_json") or source_filters.get("preview_json"), profile_base)
    if preview_path is None or not preview_path.exists():
        raise FileNotFoundError("M156 evidence preflight gate needs a source preview JSON. Set evidence_preflight_gate.preview_json or use an M113 JSON that contains filters.preview_json.")
    broker_path = _path_from_text(gate_profile.get("broker_whatif_json") or source_filters.get("broker_whatif_json"), profile_base)
    short_path = _path_from_text(gate_profile.get("short_availability_json") or source_filters.get("short_availability_json"), profile_base)

    preview_payload = json.loads(preview_path.read_text(encoding="utf-8"))
    broker_payload = json.loads(broker_path.read_text(encoding="utf-8")) if broker_path and broker_path.exists() else None
    short_payload = json.loads(short_path.read_text(encoding="utf-8")) if short_path and short_path.exists() else None

    allowed_ticket_types_value = gate_profile.get("allowed_ticket_types") or source_params.get("allowed_ticket_types") or ["stock"]
    allowed_actions_value = gate_profile.get("allowed_actions") or source_params.get("allowed_actions") or ["BUY", "SELL"]
    if isinstance(allowed_ticket_types_value, str):
        allowed_ticket_types_value = [item.strip() for item in allowed_ticket_types_value.split(",")]
    if isinstance(allowed_actions_value, str):
        allowed_actions_value = [item.strip() for item in allowed_actions_value.split(",")]
    allowed_ticket_types = tuple(str(item).strip() for item in allowed_ticket_types_value if str(item).strip())
    allowed_actions = tuple(str(item).strip() for item in allowed_actions_value if str(item).strip())
    result = build_autonomous_execution_gate_report(
        preview_payload,
        allow_fractional_shares=bool(gate_profile.get("allow_fractional_shares", source_params.get("allow_fractional_shares", False))),
        allow_short_stock=bool(gate_profile.get("allow_short_stock", source_params.get("allow_short_stock", False))),
        require_short_availability_for_shorts=bool(
            gate_profile.get("require_short_availability_for_shorts", source_params.get("require_short_availability_for_shorts", True))
        ),
        short_availability_payload=short_payload,
        require_broker_whatif=bool(gate_profile.get("require_broker_whatif", source_params.get("require_broker_whatif", False))),
        broker_whatif_payload=broker_payload,
        max_auto_orders=int(gate_profile.get("max_auto_orders", source_params.get("max_auto_orders", 10))),
        min_cash_buffer=float(gate_profile.get("min_cash_buffer", source_params.get("min_cash_buffer", 0.0))),
        max_local_initial_margin_total=gate_profile.get("max_local_initial_margin_total", source_params.get("max_local_initial_margin_total")),
        allowed_ticket_types=allowed_ticket_types,
        allowed_actions=allowed_actions,
        evidence_preflight_payload=preflight,
        sample_limit=int(gate_profile.get("sample_limit", source_params.get("sample_limit", 20) or 20)),
    )
    filters = {
        "source_auto_gate_json": str(source_auto_gate_json_path),
        "preview_json": str(preview_path),
        "broker_whatif_json": str(broker_path) if broker_path else None,
        "short_availability_json": str(short_path) if short_path else None,
        "evidence_preflight_json": str(preflight_json_path.expanduser().resolve()) if preflight_json_path else None,
        "watch_run_id": run_id,
    }
    _write_component_json_atomic(auto_gate_output_path, result, filters)
    _write_markdown_text_atomic(auto_gate_md_path, autonomous_execution_gate_markdown(result, filters=filters))
    return (
        auto_gate_output_path.expanduser().resolve(),
        preflight,
        {
            "enabled": True,
            "applied": True,
            "source_auto_gate_json": str(source_auto_gate_json_path),
            "auto_gate_json": str(auto_gate_output_path.expanduser().resolve()),
            "preflight_json": str(preflight_json_path.expanduser().resolve()) if preflight_json_path else None,
            "preflight_status": preflight.get("gate_status"),
            "preflight_action": preflight.get("action"),
            "entry_size_multiplier": preflight.get("entry_size_multiplier"),
        },
    )


def _build_watch_market_data_risk_auto_gate(
    *,
    profile: dict[str, Any],
    profile_base: Path | None,
    source_auto_gate_json_path: Path,
    run_id: str,
) -> tuple[Path, dict[str, Any] | None, dict[str, Any]]:
    gate_profile = profile.get("market_data_risk_gate") if isinstance(profile.get("market_data_risk_gate"), dict) else {}
    if not bool(gate_profile.get("enabled", False)) or bool(gate_profile.get("ignore", False)):
        return source_auto_gate_json_path, None, {"enabled": bool(gate_profile.get("enabled", False)), "applied": False, "reason": "disabled_or_ignored"}

    from recorder.market_data_risk_gate import (
        build_market_data_risk_gate_report,
        market_data_risk_gate_markdown,
        write_market_data_risk_gate_json,
    )
    from recorder.options_analysis import (
        autonomous_execution_gate_markdown,
        build_autonomous_execution_gate_report,
    )

    source_auto_payload = json.loads(source_auto_gate_json_path.read_text(encoding="utf-8"))
    source_auto_gate = _payload_result(source_auto_payload)
    source_filters = source_auto_payload.get("filters") if isinstance(source_auto_payload.get("filters"), dict) else {}
    source_params = source_auto_gate.get("gate_parameters") or {}

    market_data_path = _profile_path_value(gate_profile.get("market_data_json") or "../exports/m153_market_data_desk_refresh.json", profile_base)
    market_data_session_overlay_path = _profile_path_value(
        gate_profile.get("market_data_session_overlay_json")
        or (profile.get("paths") or {}).get("market_data_session_overlay_json")
        or "../exports/m194_market_data_session_overlay.json",
        profile_base,
    )
    gate_json_path = _profile_path_value(gate_profile.get("gate_json_output_path") or "../exports/m175_market_data_risk_gate_latest.json", profile_base)
    gate_md_path = _profile_path_value(gate_profile.get("gate_output") or "../exports/m175_market_data_risk_gate_latest.md", profile_base)
    auto_gate_output_path = _profile_path_value(gate_profile.get("auto_gate_json_output_path") or "../exports/m175_market_data_risk_auto_gate_latest.json", profile_base)
    auto_gate_md_path = _profile_path_value(gate_profile.get("auto_gate_output") or "../exports/m175_market_data_risk_auto_gate_latest.md", profile_base)
    if market_data_path is None or not market_data_path.expanduser().resolve().exists():
        raise FileNotFoundError("M175 market data risk gate needs a Market Data Desk JSON. Set market_data_risk_gate.market_data_json.")
    market_data_payload = json.loads(market_data_path.expanduser().resolve().read_text(encoding="utf-8"))
    market_data_session_overlay_payload = None
    if market_data_session_overlay_path and market_data_session_overlay_path.expanduser().resolve().exists():
        market_data_session_overlay_payload = json.loads(market_data_session_overlay_path.expanduser().resolve().read_text(encoding="utf-8"))
    gate = build_market_data_risk_gate_report(
        market_data_payload,
        market_data_session_overlay_payload=market_data_session_overlay_payload,
        block_on_delayed=bool(gate_profile.get("block_on_delayed", True)),
        block_on_missing=bool(gate_profile.get("block_on_missing", True)),
        block_on_warming_up=bool(gate_profile.get("block_on_warming_up", True)),
        block_on_errors=bool(gate_profile.get("block_on_errors", True)),
        block_on_stale=bool(gate_profile.get("block_on_stale", True)),
        block_on_close_only=bool(gate_profile.get("block_on_close_only", False)),
        block_on_severe_wide_spread=bool(gate_profile.get("block_on_severe_wide_spread", False)),
        watch_size_multiplier=float(gate_profile.get("watch_size_multiplier", 0.70)),
        hot_size_multiplier=float(gate_profile.get("hot_size_multiplier", 0.50)),
        defensive_size_multiplier=float(gate_profile.get("defensive_size_multiplier", 0.0)),
        stress_score_threshold=float(gate_profile.get("stress_score_threshold", 30.0)),
        stale_hard_block_instruments=gate_profile.get("stale_hard_block_instruments"),
        monitoring_only_instruments=gate_profile.get("monitoring_only_instruments"),
        regular_session_only_stale_hard_block_instruments=gate_profile.get("regular_session_only_stale_hard_block_instruments"),
    )
    write_market_data_risk_gate_json(gate_json_path, gate)
    _write_markdown_text_atomic(gate_md_path, market_data_risk_gate_markdown(gate))

    preview_path = _path_from_text(gate_profile.get("preview_json") or source_filters.get("preview_json"), profile_base)
    if preview_path is None or not preview_path.exists():
        raise FileNotFoundError("M175 market data risk gate needs a source preview JSON. Set market_data_risk_gate.preview_json or use an M113 JSON that contains filters.preview_json.")
    broker_path = _path_from_text(gate_profile.get("broker_whatif_json") or source_filters.get("broker_whatif_json"), profile_base)
    short_path = _path_from_text(gate_profile.get("short_availability_json") or source_filters.get("short_availability_json"), profile_base)

    preview_payload = json.loads(preview_path.read_text(encoding="utf-8"))
    broker_payload = json.loads(broker_path.read_text(encoding="utf-8")) if broker_path and broker_path.exists() else None
    short_payload = json.loads(short_path.read_text(encoding="utf-8")) if short_path and short_path.exists() else None
    evidence_preflight_payload = source_auto_gate.get("evidence_preflight") if source_auto_gate.get("evidence_preflight") else None

    allowed_ticket_types_value = gate_profile.get("allowed_ticket_types") or source_params.get("allowed_ticket_types") or ["stock"]
    allowed_actions_value = gate_profile.get("allowed_actions") or source_params.get("allowed_actions") or ["BUY", "SELL"]
    if isinstance(allowed_ticket_types_value, str):
        allowed_ticket_types_value = [item.strip() for item in allowed_ticket_types_value.split(",")]
    if isinstance(allowed_actions_value, str):
        allowed_actions_value = [item.strip() for item in allowed_actions_value.split(",")]
    allowed_ticket_types = tuple(str(item).strip() for item in allowed_ticket_types_value if str(item).strip())
    allowed_actions = tuple(str(item).strip() for item in allowed_actions_value if str(item).strip())
    result = build_autonomous_execution_gate_report(
        preview_payload,
        allow_fractional_shares=bool(gate_profile.get("allow_fractional_shares", source_params.get("allow_fractional_shares", False))),
        allow_short_stock=bool(gate_profile.get("allow_short_stock", source_params.get("allow_short_stock", False))),
        require_short_availability_for_shorts=bool(
            gate_profile.get("require_short_availability_for_shorts", source_params.get("require_short_availability_for_shorts", True))
        ),
        short_availability_payload=short_payload,
        require_broker_whatif=bool(gate_profile.get("require_broker_whatif", source_params.get("require_broker_whatif", False))),
        broker_whatif_payload=broker_payload,
        max_auto_orders=int(gate_profile.get("max_auto_orders", source_params.get("max_auto_orders", 10))),
        min_cash_buffer=float(gate_profile.get("min_cash_buffer", source_params.get("min_cash_buffer", 0.0))),
        max_local_initial_margin_total=gate_profile.get("max_local_initial_margin_total", source_params.get("max_local_initial_margin_total")),
        allowed_ticket_types=allowed_ticket_types,
        allowed_actions=allowed_actions,
        evidence_preflight_payload=evidence_preflight_payload,
        market_data_risk_gate_payload=gate,
        sample_limit=int(gate_profile.get("sample_limit", source_params.get("sample_limit", 20) or 20)),
    )
    filters = {
        "source_auto_gate_json": str(source_auto_gate_json_path),
        "preview_json": str(preview_path),
        "broker_whatif_json": str(broker_path) if broker_path else None,
        "short_availability_json": str(short_path) if short_path else None,
        "market_data_json": str(market_data_path.expanduser().resolve()) if market_data_path else None,
        "market_data_risk_gate_json": str(gate_json_path.expanduser().resolve()) if gate_json_path else None,
        "watch_run_id": run_id,
    }
    _write_component_json_atomic(auto_gate_output_path, result, filters)
    _write_markdown_text_atomic(auto_gate_md_path, autonomous_execution_gate_markdown(result, filters=filters))
    return (
        auto_gate_output_path.expanduser().resolve(),
        gate,
        {
            "enabled": True,
            "applied": True,
            "source_auto_gate_json": str(source_auto_gate_json_path),
            "auto_gate_json": str(auto_gate_output_path.expanduser().resolve()),
            "market_data_risk_gate_json": str(gate_json_path.expanduser().resolve()) if gate_json_path else None,
            "gate_status": gate.get("gate_status"),
            "gate_action": gate.get("action"),
            "entry_size_multiplier": gate.get("entry_size_multiplier"),
        },
    )












def _watched_file_fingerprint(role: str, path: Path | None) -> dict[str, Any]:
    if path is None:
        return {"role": role, "path": None, "exists": False, "size": None, "modified_utc": None, "digest": None}
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        return {"role": role, "path": str(resolved), "exists": False, "size": None, "modified_utc": None, "digest": None}
    stat = resolved.stat()
    digest = hashlib.sha256(resolved.read_bytes()).hexdigest()[:24]
    return {
        "role": role,
        "path": str(resolved),
        "exists": True,
        "size": stat.st_size,
        "modified_utc": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        "digest": digest,
    }












def _paper_watch_input_snapshot(
    *,
    auto_gate_json_path: Path,
    operating_auto_gate_json_path: Path | None,
    portfolio_risk_state_json_path: Path,
    fill_marks_json_path: Path,
    loop_mark_paths: list[Path],
    existing_ledger_json_path: Path | None,
    existing_router_json_path: Path | None,
    exit_router_json_path: Path | None,
    checkpoint_path: Path | None,
) -> dict[str, Any]:
    rows = [
        _watched_file_fingerprint("auto_gate_json", auto_gate_json_path),
        _watched_file_fingerprint("operating_auto_gate_json", operating_auto_gate_json_path),
        _watched_file_fingerprint("portfolio_risk_state_json", portfolio_risk_state_json_path),
        _watched_file_fingerprint("fill_marks_json", fill_marks_json_path),
    ]
    rows.extend(_watched_file_fingerprint(f"loop_marks_json_{index}", path) for index, path in enumerate(loop_mark_paths, start=1))
    rows.extend(
        [
            _watched_file_fingerprint("existing_ledger_json", existing_ledger_json_path),
            _watched_file_fingerprint("existing_router_json", existing_router_json_path),
            _watched_file_fingerprint("exit_router_json", exit_router_json_path),
            _watched_file_fingerprint("checkpoint_json", checkpoint_path),
        ]
    )
    signature_payload = [
        {
            "role": row.get("role"),
            "path": row.get("path"),
            "exists": row.get("exists"),
            "size": row.get("size"),
            "modified_utc": row.get("modified_utc"),
            "digest": row.get("digest"),
        }
        for row in rows
        if row.get("role") != "checkpoint_json"
    ]
    signature = hashlib.sha256(json.dumps(signature_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")).hexdigest()
    return {
        "input_snapshot_version": "paper-watch-input-snapshot-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "signature": signature,
        "files": rows,
    }














def _parse_hhmm(value: Any, default: str) -> tuple[int, int]:
    text = str(value or default)
    hour_text, minute_text = text.split(":", 1)
    return int(hour_text), int(minute_text)


def _session_minutes(value: tuple[int, int]) -> int:
    return int(value[0]) * 60 + int(value[1])


def _parse_session_gate_now(now_value: str | None, tz: ZoneInfo) -> datetime:
    if now_value:
        text = now_value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=tz)
        return parsed.astimezone(tz)
    return datetime.now(timezone.utc).astimezone(tz)


def _load_session_gate_calendar(gate: dict[str, Any], profile_base: Path | None) -> tuple[dict[str, Any], str | None]:
    calendar: dict[str, Any] = {}
    calendar_json_value = gate.get("calendar_json") or gate.get("calendar_path")
    calendar_json_path = _profile_path_value(calendar_json_value, profile_base)
    if calendar_json_path is not None:
        calendar_json_path = calendar_json_path.expanduser().resolve()
        calendar = json.loads(calendar_json_path.read_text(encoding="utf-8"))
        if not isinstance(calendar, dict):
            raise ValueError(f"Session gate calendar JSON must contain an object: {calendar_json_path}")

    inline_calendar = gate.get("calendar")
    if isinstance(inline_calendar, dict):
        calendar = {**calendar, **inline_calendar}
    return calendar, str(calendar_json_path) if calendar_json_path is not None else None


def _session_calendar_date_map(items: Any) -> dict[str, str | None]:
    date_map: dict[str, str | None] = {}
    if not isinstance(items, list):
        return date_map
    for item in items:
        if isinstance(item, str):
            date_map[item] = None
        elif isinstance(item, dict):
            date_text = item.get("date") or item.get("local_date")
            if date_text:
                date_map[str(date_text)] = str(item.get("name") or item.get("label") or "") or None
    return date_map


def _session_calendar_early_close_map(items: Any) -> dict[str, dict[str, Any]]:
    early_close_map: dict[str, dict[str, Any]] = {}
    if not isinstance(items, list):
        return early_close_map
    for item in items:
        if not isinstance(item, dict):
            continue
        date_text = item.get("date") or item.get("local_date")
        if date_text:
            early_close_map[str(date_text)] = item
    return early_close_map


def _evaluate_paper_session_gate(
    profile: dict[str, Any],
    *,
    now_value: str | None = None,
    ignore_gate: bool = False,
    profile_base: Path | None = None,
) -> dict[str, Any]:
    gate = profile.get("session_gate") if isinstance(profile.get("session_gate"), dict) else {}
    calendar, calendar_json_path = _load_session_gate_calendar(gate, profile_base)
    enabled = bool(gate.get("enabled", False)) and not bool(ignore_gate)
    timezone_name = str(gate.get("timezone") or calendar.get("timezone") or "America/New_York")
    tz = ZoneInfo(timezone_name)
    now_local = _parse_session_gate_now(now_value or gate.get("now"), tz)
    allowed_sessions = [str(item).lower() for item in gate.get("allowed_sessions", ["regular"])]
    weekday = now_local.weekday()
    allowed_weekdays = [int(item) for item in gate.get("weekdays", [0, 1, 2, 3, 4])]
    holiday_map = _session_calendar_date_map(calendar.get("holidays"))
    holiday_map.update(_session_calendar_date_map(gate.get("holidays", [])))
    early_close_map = _session_calendar_early_close_map(calendar.get("early_closes"))
    early_close_map.update(_session_calendar_early_close_map(gate.get("early_closes")))
    today = now_local.date().isoformat()
    early_close = early_close_map.get(today)
    minute = now_local.hour * 60 + now_local.minute
    calendar_schedule = calendar.get("regular_hours") if isinstance(calendar.get("regular_hours"), dict) else {}
    pre_start_text = str(gate.get("pre_start") or calendar_schedule.get("pre_start") or "04:00")
    regular_start_text = str(gate.get("regular_start") or calendar_schedule.get("regular_start") or "09:30")
    regular_end_text = str(gate.get("regular_end") or calendar_schedule.get("regular_end") or "16:00")
    after_end_text = str(gate.get("after_end") or calendar_schedule.get("after_end") or "20:00")
    if isinstance(early_close, dict):
        regular_end_text = str(early_close.get("regular_end") or early_close.get("close") or regular_end_text)
        after_end_text = str(early_close.get("after_end") or early_close.get("after_market_end") or after_end_text)
    pre_start = _session_minutes(_parse_hhmm(pre_start_text, "04:00"))
    regular_start = _session_minutes(_parse_hhmm(regular_start_text, "09:30"))
    regular_end = _session_minutes(_parse_hhmm(regular_end_text, "16:00"))
    after_end = _session_minutes(_parse_hhmm(after_end_text, "20:00"))
    session = "closed"
    reason = "Session gate is disabled."
    if weekday not in allowed_weekdays:
        session = "closed"
        reason = "Local date is outside configured weekdays."
    elif today in holiday_map:
        session = "closed"
        holiday_name = holiday_map.get(today)
        reason = f"Local date is in configured market holidays{': ' + holiday_name if holiday_name else ''}."
    elif pre_start <= minute < regular_start:
        session = "pre_market"
        reason = "Current local time is in pre-market session."
    elif regular_start <= minute < regular_end:
        session = "regular"
        reason = "Current local time is in regular session."
        if early_close:
            reason = "Current local time is in regular session on an early-close date."
    elif regular_end <= minute < after_end:
        session = "after_hours"
        reason = "Current local time is in after-hours session."
        if early_close:
            reason = "Current local time is after the early regular close."
    else:
        session = "closed"
        reason = "Current local time is outside configured trading sessions."
    allowed = (not enabled) or session in allowed_sessions
    if enabled and not allowed:
        reason = f"{reason} Allowed sessions: {','.join(allowed_sessions) or '-'}."
    return {
        "session_gate_version": "paper-session-gate-v1",
        "enabled": enabled,
        "ignored": bool(ignore_gate),
        "allowed": bool(allowed),
        "session": session,
        "reason": reason,
        "timezone": timezone_name,
        "evaluated_at_local": now_local.isoformat(),
        "allowed_sessions": allowed_sessions,
        "weekdays": allowed_weekdays,
        "holidays": sorted(holiday_map),
        "calendar": {
            "calendar_json": calendar_json_path,
            "calendar_version": calendar.get("calendar_version"),
            "market": calendar.get("market"),
            "holiday_name": holiday_map.get(today),
            "early_close": bool(early_close),
            "early_close_name": early_close.get("name") if isinstance(early_close, dict) else None,
        },
        "schedule": {
            "pre_start": pre_start_text,
            "regular_start": regular_start_text,
            "regular_end": regular_end_text,
            "after_end": after_end_text,
        },
    }






def _parse_profile_symbol_list(value: Any) -> list[str]:
    raw_items: list[Any]
    if value in (None, ""):
        raw_items = []
    elif isinstance(value, list):
        raw_items = value
    else:
        raw_items = str(value).split(",")
    symbols: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        symbol = str(item or "").strip().upper()
        if symbol and symbol not in seen:
            seen.add(symbol)
            symbols.append(symbol)
    return symbols


def _mark_refresh_symbols_from_snapshot(payload: dict[str, Any]) -> list[str]:
    symbols: list[str] = []
    seen: set[str] = set()
    for item in payload.get("symbols_requested") or []:
        symbol = str(item or "").strip().upper()
        if symbol and symbol not in seen:
            seen.add(symbol)
            symbols.append(symbol)
    for row in payload.get("marks") or []:
        symbol = str(row.get("symbol") or "").strip().upper() if isinstance(row, dict) else ""
        if symbol and symbol not in seen:
            seen.add(symbol)
            symbols.append(symbol)
    return symbols


def _freshened_mark_snapshot_payload(payload: dict[str, Any], *, now_iso: str) -> dict[str, Any]:
    refreshed = json.loads(json.dumps(payload))
    refreshed["generated_at"] = now_iso
    refreshed["mark_refresh_source"] = "copy_existing"
    for row in refreshed.get("marks") or []:
        if not isinstance(row, dict):
            continue
        row["mark_timestamp"] = now_iso
        for tick in row.get("received_ticks") or []:
            if isinstance(tick, dict):
                tick["received_at"] = now_iso
    return refreshed


def _parse_utc_datetime(value: Any) -> datetime | None:
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
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _mark_snapshot_age_seconds(payload: dict[str, Any], *, now: datetime) -> float | None:
    generated_at = _parse_utc_datetime(payload.get("generated_at"))
    if generated_at is None:
        return None
    return round((now - generated_at).total_seconds(), 3)


def _mark_refresh_snapshot_summary(payload: dict[str, Any]) -> dict[str, Any]:
    marks = [row for row in payload.get("marks") or [] if isinstance(row, dict)]
    row_count = len(marks)
    marked_count = sum(1 for row in marks if row.get("mark_status") == "marked" and row.get("mark_price") is not None)
    missing_count = row_count - marked_count
    delayed_count = sum(1 for row in marks if bool(row.get("delayed")) or row.get("market_data_type") in {3, 4})
    marked_ratio = round(marked_count / row_count, 6) if row_count else 0.0
    status_counts: dict[str, int] = {}
    for row in marks:
        status = str(row.get("mark_status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
    return {
        "generated_at": payload.get("generated_at"),
        "row_count": row_count,
        "marked_count": marked_count,
        "missing_mark_count": missing_count,
        "delayed_count": delayed_count,
        "marked_ratio": marked_ratio,
        "status_counts": status_counts,
        "market_data_type_label": ((payload.get("config") or {}).get("market_data_type_label")),
    }


def _mark_refresh_apply_partial_fallback(
    summary: dict[str, Any],
    fallback_payload: dict[str, Any],
    *,
    enabled: bool,
) -> tuple[dict[str, Any], list[str]]:
    if not enabled or not fallback_payload:
        return summary, []
    fallback_by_symbol = {
        str(row.get("symbol") or "").strip().upper(): row
        for row in fallback_payload.get("marks") or []
        if isinstance(row, dict) and str(row.get("symbol") or "").strip()
    }
    if not fallback_by_symbol:
        return summary, []
    merged = json.loads(json.dumps(summary))
    applied: list[str] = []
    for row in merged.get("marks") or []:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        is_marked = row.get("mark_status") == "marked" and row.get("mark_price") is not None
        if is_marked or symbol not in fallback_by_symbol:
            continue
        fallback_row = json.loads(json.dumps(fallback_by_symbol[symbol]))
        fallback_row["mark_refresh_fallback_applied"] = True
        fallback_row["mark_refresh_fallback_reason"] = "partial_refresh_missing_mark"
        fallback_row["mark_refresh_original_status"] = row.get("mark_status")
        fallback_row["mark_refresh_original_request_id"] = row.get("request_id")
        row.clear()
        row.update(fallback_row)
        applied.append(symbol)
    if applied:
        counts = _mark_refresh_snapshot_summary(merged)
        merged["marked_count"] = counts["marked_count"]
        merged["missing_mark_count"] = counts["missing_mark_count"]
        merged["status_counts"] = counts["status_counts"]
        merged["mark_refresh_fallback_symbols"] = applied
        merged["mark_refresh_fallback_source_generated_at"] = fallback_payload.get("generated_at")
    return merged, applied


def _evaluate_mark_refresh_policy(
    summary: dict[str, Any],
    *,
    refresh_profile: dict[str, Any],
    fallback_payload: dict[str, Any],
    fallback_applied_symbols: list[str],
    fallback_reason: str | None,
    now: datetime,
    required: bool,
) -> dict[str, Any]:
    fallback_profile = refresh_profile.get("fallback") if isinstance(refresh_profile.get("fallback"), dict) else {}
    fallback_enabled = bool(fallback_profile.get("enabled", False))
    fallback_source_age_seconds = _mark_snapshot_age_seconds(fallback_payload, now=now) if fallback_payload else None
    max_fallback_age_value = fallback_profile.get("max_source_age_seconds", refresh_profile.get("max_snapshot_age_seconds"))
    max_fallback_age_seconds = float(max_fallback_age_value) if max_fallback_age_value is not None else None
    fallback_source_allowed = bool(fallback_payload)
    fallback_block_reasons: list[str] = []
    if fallback_enabled:
        if not fallback_payload:
            fallback_source_allowed = False
            fallback_block_reasons.append("fallback_source_missing")
        elif fallback_source_age_seconds is None:
            fallback_source_allowed = False
            fallback_block_reasons.append("fallback_source_unparseable_generated_at")
        elif max_fallback_age_seconds is not None and fallback_source_age_seconds > max_fallback_age_seconds:
            fallback_source_allowed = False
            fallback_block_reasons.append("fallback_source_stale")
    counts = _mark_refresh_snapshot_summary(summary)
    min_marked_ratio = float(fallback_profile.get("min_marked_ratio", refresh_profile.get("min_marked_ratio", 1.0)))
    max_missing_symbols = int(fallback_profile.get("max_missing_symbols", refresh_profile.get("max_missing_symbols", 0)))
    allow_delayed = bool(fallback_profile.get("allow_delayed", refresh_profile.get("allow_delayed", False)))
    block_reasons: list[str] = []
    warn_reasons: list[str] = []
    if counts["row_count"] <= 0:
        block_reasons.append("no_mark_rows")
    if counts["marked_ratio"] < min_marked_ratio:
        block_reasons.append("marked_ratio_below_min")
    if int(counts["missing_mark_count"]) > max_missing_symbols:
        block_reasons.append("missing_marks_above_max")
    if int(counts["delayed_count"]) > 0 and not allow_delayed:
        block_reasons.append("delayed_data_not_allowed")
    if (fallback_applied_symbols or fallback_reason) and fallback_enabled and not fallback_source_allowed:
        block_reasons.append("fallback_source_not_allowed")
    if fallback_applied_symbols:
        warn_reasons.append("fallback_rows_applied")
    if fallback_reason:
        warn_reasons.append(fallback_reason)
    allowed = not block_reasons if required else True
    status = "pass"
    reason = "Mark refresh completed within policy."
    if block_reasons:
        status = "block" if required else "warn"
        reason = "Mark refresh policy blocked: " + ", ".join(block_reasons)
    elif warn_reasons:
        status = "warn"
        reason = "Mark refresh completed with warnings: " + ", ".join(warn_reasons)
    return {
        "policy_version": "paper-mark-refresh-fallback-policy-v1",
        "allowed": bool(allowed),
        "status": status,
        "reason": reason,
        "block_reasons": block_reasons,
        "warn_reasons": warn_reasons,
        "summary": counts,
        "fallback": {
            "enabled": fallback_enabled,
            "source_available": bool(fallback_payload),
            "source_generated_at": fallback_payload.get("generated_at") if fallback_payload else None,
            "source_age_seconds": fallback_source_age_seconds,
            "max_source_age_seconds": max_fallback_age_seconds,
            "source_allowed": fallback_source_allowed,
            "source_block_reasons": fallback_block_reasons,
            "applied_symbols": fallback_applied_symbols,
            "reason": fallback_reason,
        },
        "thresholds": {
            "min_marked_ratio": min_marked_ratio,
            "max_missing_symbols": max_missing_symbols,
            "allow_delayed": allow_delayed,
        },
    }


def _mark_refresh_quality_execution_action(
    mark_refresh: dict[str, Any] | None,
    profile: dict[str, Any],
    *,
    base_monitor_size_multiplier: float,
) -> dict[str, Any]:
    refresh_profile = profile.get("mark_refresh") if isinstance(profile.get("mark_refresh"), dict) else {}
    action_profile = refresh_profile.get("quality_actions") if isinstance(refresh_profile.get("quality_actions"), dict) else {}
    enabled = bool(action_profile.get("enabled", False))
    status = str(((mark_refresh or {}).get("policy_evaluation") or {}).get("status") or (mark_refresh or {}).get("status") or "not_configured").lower()
    actions = action_profile.get("actions") if isinstance(action_profile.get("actions"), dict) else {}
    default_by_status = {
        "pass": {"action": "allow", "portfolio_risk_state_override": None, "monitor_size_multiplier": base_monitor_size_multiplier},
        "warn": {"action": "size_reduce", "portfolio_risk_state_override": "monitor", "monitor_size_multiplier": 0.50},
        "block": {"action": "block_new_entries", "portfolio_risk_state_override": "reduce_risk", "monitor_size_multiplier": 0.0},
    }
    configured = actions.get(status) if isinstance(actions.get(status), dict) else {}
    selected = {**default_by_status.get(status, default_by_status["pass"]), **configured}
    action = str(selected.get("action") or "allow")
    configured_multiplier = selected.get("monitor_size_multiplier", base_monitor_size_multiplier)
    try:
        effective_multiplier = max(0.0, min(1.0, float(configured_multiplier)))
    except (TypeError, ValueError):
        effective_multiplier = max(0.0, min(1.0, float(base_monitor_size_multiplier)))
    if not enabled or mark_refresh is None:
        return {
            "quality_action_version": "paper-mark-refresh-quality-action-v1",
            "enabled": False,
            "status": status,
            "action": "allow",
            "applied": False,
            "portfolio_risk_state_override": None,
            "base_monitor_size_multiplier": float(base_monitor_size_multiplier),
            "effective_monitor_size_multiplier": float(base_monitor_size_multiplier),
            "reason": "Mark-refresh quality actions are disabled or mark refresh is not configured.",
        }
    applied = action != "allow" or selected.get("portfolio_risk_state_override") is not None or effective_multiplier != float(base_monitor_size_multiplier)
    return {
        "quality_action_version": "paper-mark-refresh-quality-action-v1",
        "enabled": True,
        "status": status,
        "action": action,
        "applied": bool(applied),
        "portfolio_risk_state_override": selected.get("portfolio_risk_state_override"),
        "base_monitor_size_multiplier": float(base_monitor_size_multiplier),
        "effective_monitor_size_multiplier": effective_multiplier,
        "reason": str(selected.get("reason") or f"Mark-refresh quality status {status!r} mapped to action {action!r}."),
    }


def _apply_mark_refresh_quality_action_to_portfolio_risk(
    payload: dict[str, Any],
    action: dict[str, Any],
) -> dict[str, Any]:
    if not action.get("enabled") or not action.get("applied"):
        return payload
    adjusted = json.loads(json.dumps(payload or {}))
    target = adjusted.get("result") if isinstance(adjusted.get("result"), dict) else adjusted
    if not isinstance(target, dict):
        return adjusted
    current_state = str(target.get("risk_state") or "unknown")
    override = action.get("portfolio_risk_state_override")
    if override == "monitor" and current_state == "within_limits":
        target["risk_state"] = "monitor"
        target["new_entries_allowed"] = True
        target["warning_count"] = int(target.get("warning_count") or 0) + 1
        target["recommended_action"] = "monitor_mark_refresh_quality"
        target["mark_refresh_quality_overlay_applied"] = True
        target["mark_refresh_quality_overlay"] = action
        warnings = target.get("warnings")
        if not isinstance(warnings, list):
            warnings = []
        warnings.append(
            {
                "type": "mark_refresh_quality_warn",
                "reason": action.get("reason"),
                "effective_monitor_size_multiplier": action.get("effective_monitor_size_multiplier"),
            }
        )
        target["warnings"] = warnings
    elif override == "reduce_risk":
        target["risk_state"] = "reduce_risk"
        target["new_entries_allowed"] = False
        target["recommended_action"] = "block_new_entries_for_mark_refresh_quality"
        target["mark_refresh_quality_overlay_applied"] = True
        target["mark_refresh_quality_overlay"] = action
    else:
        target["mark_refresh_quality_overlay_applied"] = bool(action.get("applied"))
        target["mark_refresh_quality_overlay"] = action
    return adjusted


def _write_json_to_targets(payload: dict[str, Any], paths: list[Path]) -> list[str]:
    written: list[str] = []
    seen: set[Path] = set()
    for path in paths:
        resolved = path.expanduser().resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        written.append(str(resolved))
    return written


def _run_paper_mark_refresh(
    *,
    settings: Settings | None,
    profile: dict[str, Any],
    profile_base: Path | None,
    fill_marks_json_path: Path,
    loop_mark_paths: list[Path],
    mode_override: str | None,
    ignore_refresh: bool | None,
) -> dict[str, Any] | None:
    refresh_profile = profile.get("mark_refresh") if isinstance(profile.get("mark_refresh"), dict) else {}
    fallback_profile = refresh_profile.get("fallback") if isinstance(refresh_profile.get("fallback"), dict) else {}
    enabled = bool(refresh_profile.get("enabled", False))
    if ignore_refresh:
        enabled = False
    mode = str(mode_override or refresh_profile.get("mode") or "ibkr").strip().lower()
    if not enabled or mode in {"", "disabled", "off", "none"}:
        return None
    required = bool(refresh_profile.get("required", True))
    write_fill = bool(refresh_profile.get("write_fill_marks_json", True))
    write_loop = bool(refresh_profile.get("write_loop_marks_jsons", True))
    targets: list[Path] = []
    if write_fill:
        targets.append(fill_marks_json_path)
    if write_loop:
        targets.extend(loop_mark_paths)
    symbols = _parse_profile_symbol_list(refresh_profile.get("symbols"))
    existing_payload: dict[str, Any] = {}
    source_json_path = _profile_path_value(refresh_profile.get("source_json"), profile_base)
    if source_json_path is not None and source_json_path.expanduser().resolve().exists():
        existing_payload = json.loads(source_json_path.expanduser().resolve().read_text(encoding="utf-8"))
    elif fill_marks_json_path.exists():
        existing_payload = json.loads(fill_marks_json_path.read_text(encoding="utf-8"))
    if not symbols:
        symbols = _mark_refresh_symbols_from_snapshot(existing_payload)
    base_report = {
        "paper_mark_refresh_version": "paper-mark-refresh-v2",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "enabled": enabled,
        "mode": mode,
        "required": required,
        "symbols": symbols,
        "output_json_paths": [str(path.expanduser().resolve()) for path in targets],
        "allowed": True,
        "status": "pass",
        "reason": "Mark refresh completed.",
        "snapshot": None,
        "policy_evaluation": None,
        "safety": {
            "mode": "paper_mark_refresh",
            "policy": "Refreshes paper mark snapshot files before paper-only scheduler execution. IBKR mode uses read-only market data only; no broker order API call, order submission, transmit, or real portfolio mutation.",
            "ibkr_order_submitted": False,
            "transmit_enabled": False,
            "live_order_routing_enabled": False,
        },
    }
    if not symbols:
        base_report.update(
            {
                "allowed": not required,
                "status": "block" if required else "warn",
                "reason": "Mark refresh requested but no symbols were available.",
            }
        )
        return base_report
    try:
        if mode == "copy_existing":
            if not existing_payload:
                raise ValueError("copy_existing mark refresh requires an existing fill mark snapshot")
            summary = _freshened_mark_snapshot_payload(existing_payload, now_iso=datetime.now(timezone.utc).isoformat())
        elif mode == "ibkr":
            from recorder.ibkr_stock_marks import StockMarkSnapshotConfig, run_ibkr_stock_mark_snapshot

            active_settings = settings or Settings.from_env()
            summary = run_ibkr_stock_mark_snapshot(
                StockMarkSnapshotConfig(
                    host=active_settings.host,
                    port=active_settings.port,
                    client_id=int(refresh_profile.get("client_id") or (active_settings.client_id + int(refresh_profile.get("client_id_offset", 9400)))),
                    symbols=tuple(symbols),
                    quote_seconds=float(refresh_profile.get("quote_seconds", 8.0)),
                    market_data_type=int(refresh_profile.get("market_data_type", 1)),
                    exchange=str(refresh_profile.get("exchange") or "SMART"),
                    currency=str(refresh_profile.get("currency") or "USD"),
                )
            )
        else:
            raise ValueError(f"Unsupported mark_refresh mode: {mode}")
        now = datetime.now(timezone.utc)
        summary, fallback_applied_symbols = _mark_refresh_apply_partial_fallback(
            summary,
            existing_payload,
            enabled=bool(fallback_profile.get("enabled", False)) and bool(fallback_profile.get("reuse_on_partial", True)),
        )
        policy_evaluation = _evaluate_mark_refresh_policy(
            summary,
            refresh_profile=refresh_profile,
            fallback_payload=existing_payload,
            fallback_applied_symbols=fallback_applied_symbols,
            fallback_reason="partial_refresh_fallback" if fallback_applied_symbols else None,
            now=now,
            required=required,
        )
        written = _write_json_to_targets(summary, targets)
        base_report["output_json_paths"] = written
        base_report["allowed"] = policy_evaluation["allowed"]
        base_report["status"] = policy_evaluation["status"]
        base_report["reason"] = policy_evaluation["reason"]
        base_report["policy_evaluation"] = policy_evaluation
        base_report["snapshot"] = {
            "generated_at": summary.get("generated_at"),
            "row_count": summary.get("row_count"),
            "marked_count": summary.get("marked_count"),
            "missing_mark_count": summary.get("missing_mark_count"),
            "delayed_count": _mark_refresh_snapshot_summary(summary).get("delayed_count"),
            "status_counts": summary.get("status_counts"),
            "market_data_type_label": ((summary.get("config") or {}).get("market_data_type_label")),
        }
        return base_report
    except Exception as exc:
        if bool(fallback_profile.get("enabled", False)) and bool(fallback_profile.get("reuse_on_failure", True)) and existing_payload:
            now = datetime.now(timezone.utc)
            summary = json.loads(json.dumps(existing_payload))
            summary["mark_refresh_fallback_reason"] = "refresh_failure_reused_existing_snapshot"
            summary["mark_refresh_failure"] = str(exc)
            policy_evaluation = _evaluate_mark_refresh_policy(
                summary,
                refresh_profile=refresh_profile,
                fallback_payload=existing_payload,
                fallback_applied_symbols=_mark_refresh_symbols_from_snapshot(existing_payload),
                fallback_reason="refresh_failure_fallback",
                now=now,
                required=required,
            )
            written = _write_json_to_targets(summary, targets)
            base_report["output_json_paths"] = written
            base_report["allowed"] = policy_evaluation["allowed"]
            base_report["status"] = policy_evaluation["status"]
            base_report["reason"] = policy_evaluation["reason"]
            base_report["policy_evaluation"] = policy_evaluation
            base_report["snapshot"] = {
                **_mark_refresh_snapshot_summary(summary),
                "market_data_type_label": ((summary.get("config") or {}).get("market_data_type_label")),
            }
            return base_report
        base_report.update(
            {
                "allowed": not required,
                "status": "block" if required else "warn",
                "reason": f"Mark refresh failed: {exc}",
            }
        )
        return base_report
