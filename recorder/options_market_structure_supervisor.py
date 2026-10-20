from __future__ import annotations

import json
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from recorder.operating_defensive import evaluate_option_quote_session_gate, next_regular_option_session_open


OPTIONS_MARKET_STRUCTURE_SUPERVISOR_VERSION = "options-market-structure-refresh-supervisor-v9-operational-runbook-tuning"
CRITICAL_CORE_OPTION_RETRY_SYMBOLS = {"SPY", "QQQ", "IWM", "DIA"}

CommandRunner = Callable[[list[str], Path, float], dict[str, Any]]


@dataclass(frozen=True)
class OptionsMarketStructureSupervisorConfig:
    symbols: list[str]
    optional_index_symbols: list[str]
    reference_prices: dict[str, float]
    underlying_sec_types: dict[str, str]
    underlying_exchanges: dict[str, str]
    quote_json_path: Path
    quote_markdown_path: Path
    structure_json_path: Path
    structure_markdown_path: Path
    index_option_families: list[dict[str, Any]] = field(default_factory=list)
    optional_index_quote_json_path: Path | None = None
    optional_index_quote_markdown_path: Path | None = None
    optional_index_structure_json_path: Path | None = None
    optional_index_structure_markdown_path: Path | None = None
    option_structure_outcome_enabled: bool = True
    option_structure_outcome_price_source: str = "ibkr"
    option_structure_outcome_horizons: str = "5M,15M,30M,EOD"
    option_structure_outcome_flat_threshold: float = 0.001
    option_structure_outcome_json_path: Path | None = None
    option_structure_outcome_markdown_path: Path | None = None
    optional_index_option_structure_outcome_json_path: Path | None = None
    optional_index_option_structure_outcome_markdown_path: Path | None = None
    index_option_capability_probe_json_path: Path | None = None
    index_option_capability_probe_markdown_path: Path | None = None
    market_data_json_path: Path | None = None
    state_json_path: Path | None = None
    tick_log_jsonl_path: Path | None = None
    max_ticks: int = 1
    interval_seconds: float = 1.0
    sleep_between_ticks: bool = False
    execute_actions: bool = False
    regular_session_only: bool = True
    timezone_name: str = "America/New_York"
    regular_start: str = "09:30"
    regular_end: str = "16:00"
    max_snapshot_age_minutes: float = 30.0
    warmup_max_snapshot_age_minutes: float = 45.0
    close_window_max_snapshot_age_minutes: float = 10.0
    open_warmup_minutes: float = 15.0
    close_refresh_window_minutes: float = 60.0
    quote_refresh_cooldown_seconds: float = 300.0
    proactive_quote_refresh_ratio: float = 0.55
    structure_refresh_cooldown_seconds: float = 30.0
    capability_probe_cooldown_seconds: float = 600.0
    symbol_failure_backoff_seconds: float = 120.0
    low_quality_backoff_seconds: float = 60.0
    max_symbol_failures_before_isolation: int = 3
    symbol_isolation_seconds: float = 900.0
    symbol_status_ttl_hours: float = 72.0
    symbol_failure_note_ttl_hours: float = 24.0
    min_symbol_quoted_contracts: int = 2
    min_symbol_quoted_rate: float = 0.20
    min_dte: int = 0
    max_dte: int = 45
    strikes_around: int = 4
    max_expirations: int = 3
    max_quote_contracts: int = 240
    max_quote_contracts_by_phase: dict[str, int] | None = None
    optional_index_max_quote_contracts_by_phase: dict[str, int] | None = None
    index_family_quote_budget_weights: dict[str, float] | None = None
    market_data_type: int = 1
    generic_ticks: str = "100,101,104,106"
    chain_timeout_seconds: float = 20.0
    quote_seconds: float = 10.0
    max_spread_pct: float = 0.30
    client_id: int | None = None
    pin_near_pct: float = 0.5
    proximity_near_pct: float = 1.0
    job_timeout_seconds: float = 240.0


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _parse_dt(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _age_seconds(value: Any, *, now_dt: datetime | None = None) -> float | None:
    parsed = _parse_dt(value)
    if parsed is None:
        return None
    now_dt = now_dt or datetime.now(UTC)
    return max(0.0, (now_dt - parsed).total_seconds())


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


def _append_jsonl(path: Path | None, payload: dict[str, Any]) -> None:
    if path is None:
        return
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    with resolved.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")


def _profile_path(value: Any, *, base: Path | None = None) -> Path | None:
    text = str(value or "").strip()
    if not text:
        return None
    path = Path(text).expanduser()
    if not path.is_absolute() and base is not None:
        path = base / path
    return path


def _default_index_option_families() -> list[dict[str, Any]]:
    return [
        {
            "family_id": "sp500",
            "label": "S&P 500",
            "direct_symbols": ["SPX"],
            "mini_symbols": ["XSP"],
            "proxy_symbols": ["SPY", "ES"],
            "notes": "Prefer direct SPX when available; XSP is a mini index fallback; SPY/ES remain proxy coverage.",
        },
        {
            "family_id": "nasdaq100",
            "label": "Nasdaq 100",
            "direct_symbols": ["NDX"],
            "mini_symbols": ["XND"],
            "proxy_symbols": ["QQQ", "NQ"],
            "notes": "Prefer direct NDX when available; XND is a mini index fallback; QQQ/NQ remain proxy coverage.",
        },
        {
            "family_id": "russell2000",
            "label": "Russell 2000",
            "direct_symbols": ["RUT"],
            "mini_symbols": [],
            "proxy_symbols": ["IWM", "RTY"],
            "notes": "RUT direct coverage is optional; IWM/RTY proxy coverage remains usable when direct RUT is unavailable.",
        },
        {
            "family_id": "dow30",
            "label": "Dow / Industrials",
            "direct_symbols": [],
            "mini_symbols": [],
            "proxy_symbols": ["DIA", "YM"],
            "notes": "DIA/YM are proxy-only context for Dow-style broad-market option pressure.",
        },
        {
            "family_id": "volatility",
            "label": "Volatility",
            "direct_symbols": ["VIX"],
            "mini_symbols": [],
            "proxy_symbols": ["VXX", "VIXY", "UVXY"],
            "notes": "VIX direct options are preferred; VXX/VIXY/UVXY are ETF/ETN proxy candidates when permissions or contract mapping block VIX.",
        },
    ]


def _normalize_symbol_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [part.strip().upper() for part in value.split(",") if part.strip()]
    if isinstance(value, list):
        return [str(item).strip().upper() for item in value if str(item).strip()]
    return []


def _normalize_index_option_families(value: Any) -> list[dict[str, Any]]:
    source = value if isinstance(value, list) and value else _default_index_option_families()
    families: list[dict[str, Any]] = []
    for idx, row in enumerate(source):
        if not isinstance(row, dict):
            continue
        family_id = str(row.get("family_id") or row.get("id") or row.get("label") or f"family_{idx + 1}").strip().lower().replace(" ", "_")
        if not family_id:
            continue
        families.append(
            {
                "family_id": family_id,
                "label": str(row.get("label") or family_id).strip(),
                "direct_symbols": _normalize_symbol_list(row.get("direct_symbols") or row.get("direct") or []),
                "mini_symbols": _normalize_symbol_list(row.get("mini_symbols") or row.get("mini") or []),
                "proxy_symbols": _normalize_symbol_list(row.get("proxy_symbols") or row.get("proxies") or row.get("proxy") or []),
                "notes": str(row.get("notes") or "").strip(),
            }
        )
    return families or _default_index_option_families()


def _int_mapping(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, int] = {}
    for key, raw in value.items():
        try:
            result[str(key)] = int(raw)
        except (TypeError, ValueError):
            continue
    return result


def _float_mapping(value: Any) -> dict[str, float]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, float] = {}
    for key, raw in value.items():
        try:
            result[str(key)] = float(raw)
        except (TypeError, ValueError):
            continue
    return result


def _phase_quote_contract_budget(config: OptionsMarketStructureSupervisorConfig, phase: str, *, optional_index: bool = False) -> int:
    phase = str(phase or "").strip()
    mapping = config.optional_index_max_quote_contracts_by_phase if optional_index else config.max_quote_contracts_by_phase
    if isinstance(mapping, dict):
        for key in (phase, phase.replace("regular_", ""), "default"):
            if key in mapping and mapping[key] is not None:
                return max(1, int(mapping[key]))
    return max(1, int(config.max_quote_contracts))


def _family_all_symbols(family: dict[str, Any]) -> list[str]:
    return list(
        dict.fromkeys(
            _normalize_symbol_list(family.get("direct_symbols"))
            + _normalize_symbol_list(family.get("mini_symbols"))
            + _normalize_symbol_list(family.get("proxy_symbols"))
        )
    )


def _index_probe_symbols(config: OptionsMarketStructureSupervisorConfig) -> list[str]:
    configured = set(config.symbols or []) | set(config.optional_index_symbols or [])
    symbols: list[str] = []
    for family in config.index_option_families or _default_index_option_families():
        for symbol in _family_all_symbols(family):
            # Futures proxies such as ES/NQ/YM/RTY are market-data context, not the
            # stock/index option-chain scanner's underlying contract target.
            if symbol in {"ES", "NQ", "YM", "RTY"}:
                continue
            if symbol in configured or symbol in set(config.underlying_sec_types) or symbol in {"SPY", "QQQ", "IWM", "DIA", "VXX", "VIXY", "UVXY"}:
                symbols.append(symbol)
    return sorted(dict.fromkeys(symbols))


def _index_family_budget_allocation(config: OptionsMarketStructureSupervisorConfig, phase: str, total_budget: int) -> dict[str, int]:
    families = config.index_option_families or _default_index_option_families()
    default_weights = {
        "sp500": 0.35,
        "nasdaq100": 0.30,
        "russell2000": 0.15,
        "volatility": 0.15,
        "dow30": 0.05,
    }
    if str(phase) == "regular_close_window":
        default_weights.update({"sp500": 0.34, "nasdaq100": 0.29, "volatility": 0.22, "russell2000": 0.10, "dow30": 0.05})
    weights = dict(default_weights)
    weights.update(config.index_family_quote_budget_weights or {})
    active_ids = [str(family.get("family_id") or "").strip() for family in families if str(family.get("family_id") or "").strip()]
    weight_sum = sum(max(0.0, float(weights.get(family_id, 0.0))) for family_id in active_ids) or 1.0
    allocation: dict[str, int] = {}
    remaining = max(0, int(total_budget))
    for idx, family_id in enumerate(active_ids):
        if idx == len(active_ids) - 1:
            allocation[family_id] = max(0, remaining)
            break
        amount = int(round(max(0.0, float(weights.get(family_id, 0.0))) / weight_sum * int(total_budget)))
        allocation[family_id] = max(0, amount)
        remaining -= allocation[family_id]
    return allocation


def _probe_status_map(payload: dict[str, Any] | None, expected_symbols: list[str]) -> dict[str, dict[str, Any]]:
    data = payload or {}
    by_symbol = {
        str(row.get("symbol") or "").upper(): row
        for row in data.get("by_symbol") or []
        if str(row.get("symbol") or "").strip()
    }
    underlying_by_symbol: dict[str, list[dict[str, Any]]] = {}
    for row in data.get("underlying_contracts") or []:
        symbol = str(row.get("symbol") or "").upper()
        if symbol:
            underlying_by_symbol.setdefault(symbol, []).append(row)
    chain_by_symbol: dict[str, list[dict[str, Any]]] = {}
    for row in data.get("chain_rows") or []:
        symbol = str(row.get("symbol") or "").upper()
        if symbol:
            chain_by_symbol.setdefault(symbol, []).append(row)
    errors_by_symbol: dict[str, list[dict[str, Any]]] = {}
    for row in data.get("errors") or []:
        symbol = str(row.get("symbol") or "").upper()
        if symbol:
            errors_by_symbol.setdefault(symbol, []).append(row)
    requested = {str(symbol).upper() for symbol in data.get("symbols") or []}
    result: dict[str, dict[str, Any]] = {}
    for symbol in [str(item).strip().upper() for item in expected_symbols if str(item).strip()]:
        row = by_symbol.get(symbol) or {}
        underlying = underlying_by_symbol.get(symbol) or []
        chains = chain_by_symbol.get(symbol) or []
        errors = errors_by_symbol.get(symbol) or []
        candidates = int(row.get("candidate_contracts") or 0)
        chain_count = int(row.get("chain_rows") or len(chains) or 0)
        if symbol not in requested and data:
            status = "not_requested"
            reason = "not_requested_by_probe_scope"
        elif candidates > 0:
            status = "candidate_contracts_available"
            reason = "candidate_contracts_available"
        elif chain_count > 0:
            status = "chain_available_no_candidates"
            reason = "chain_available_but_no_candidate_contracts_selected"
        elif underlying:
            status = "underlying_resolved_no_chain"
            reason = "underlying_resolved_but_no_option_chain"
        elif errors:
            status = "probe_error"
            text = " ".join(str(error.get("message") or error.get("error") or "") for error in errors).lower()
            if "permission" in text or "subscription" in text:
                reason = "market_data_permission_or_contract_permission"
            elif "no security definition" in text or "ambiguous" in text or "contract" in text:
                reason = "contract_unresolved"
            else:
                reason = "probe_error"
        else:
            status = "missing_probe"
            reason = "no_probe_rows"
        result[symbol] = {
            "symbol": symbol,
            "status": status,
            "reason": reason,
            "underlying_contract_count": len(underlying),
            "chain_rows": chain_count,
            "candidate_contracts": candidates,
            "nearest_expirations": row.get("nearest_expirations") or [],
            "min_strike": row.get("min_strike"),
            "max_strike": row.get("max_strike"),
            "errors": errors,
        }
    return result


def _default_command_runner(command: list[str], cwd: Path, timeout_seconds: float) -> dict[str, Any]:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            [str(item) for item in command],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=float(timeout_seconds),
        )
        return {
            "status": "succeeded" if completed.returncode == 0 else "failed",
            "returncode": completed.returncode,
            "stdout_tail": str(completed.stdout or "")[-3000:],
            "stderr_tail": str(completed.stderr or "")[-3000:],
            "duration_seconds": round(time.monotonic() - started, 3),
            "error": None,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "status": "timeout",
            "returncode": None,
            "stdout_tail": str(exc.stdout or "")[-3000:],
            "stderr_tail": str(exc.stderr or "")[-3000:],
            "duration_seconds": round(time.monotonic() - started, 3),
            "error": f"timeout_after_{timeout_seconds}_seconds",
        }
    except Exception as exc:  # pragma: no cover - defensive execution audit
        return {
            "status": "failed",
            "returncode": None,
            "stdout_tail": "",
            "stderr_tail": "",
            "duration_seconds": round(time.monotonic() - started, 3),
            "error": f"{exc.__class__.__name__}: {str(exc)[:500]}",
        }


def _command_action(
    action_id: str,
    description: str,
    command: list[str],
    *,
    priority: int,
    live_ibkr: bool,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "action_id": action_id,
        "priority": int(priority),
        "description": description,
        "live_ibkr": bool(live_ibkr),
        "command": command,
        "details": details or {},
        "status": "planned",
        "started_at": None,
        "finished_at": None,
        "duration_seconds": None,
        "returncode": None,
        "stdout_tail": "",
        "stderr_tail": "",
        "error": None,
    }


def _note_action(action_id: str, description: str, *, priority: int, status: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    action = _command_action(action_id, description, [], priority=priority, live_ibkr=False)
    action["status"] = status
    action["details"] = details or {}
    return action


def _run_action(action: dict[str, Any], *, cwd: Path, runner: CommandRunner, timeout_seconds: float) -> dict[str, Any]:
    if action.get("status") != "planned" or not action.get("command"):
        return action
    action["started_at"] = _iso_now()
    action["status"] = "running"
    result = runner([str(item) for item in action.get("command") or []], cwd, timeout_seconds)
    action.update(result)
    action["failure_reason"] = _classify_action_failure(action)
    action["finished_at"] = _iso_now()
    return action


def _cooldown_active(state: dict[str, Any], action_id: str, now_dt: datetime) -> tuple[bool, str | None]:
    next_raw = (state.get("next_eligible_by_action") or {}).get(action_id)
    next_dt = _parse_dt(next_raw)
    if next_dt is not None and now_dt < next_dt:
        return True, next_dt.isoformat()
    return False, next_dt.isoformat() if next_dt else None


def _mark_cooldown(state: dict[str, Any], action_id: str, *, now_dt: datetime, seconds: float) -> None:
    next_by_action = dict(state.get("next_eligible_by_action") or {})
    next_by_action[action_id] = (now_dt.timestamp() + float(seconds))
    next_by_action[action_id] = datetime.fromtimestamp(float(next_by_action[action_id]), tz=UTC).isoformat()
    state["next_eligible_by_action"] = next_by_action


def _symbol_action_key(action_id: str, symbol: str) -> str:
    return f"{action_id}:{str(symbol).strip().upper()}"


def _symbol_cooldown_active(state: dict[str, Any], action_id: str, symbol: str, now_dt: datetime) -> tuple[bool, str | None]:
    next_raw = (state.get("next_eligible_by_symbol_action") or {}).get(_symbol_action_key(action_id, symbol))
    next_dt = _parse_dt(next_raw)
    if next_dt is not None and now_dt < next_dt:
        return True, next_dt.isoformat()
    return False, next_dt.isoformat() if next_dt else None


def _mark_symbol_cooldown(
    state: dict[str, Any],
    action_id: str,
    symbol: str,
    *,
    now_dt: datetime,
    seconds: float,
    reason: str | None = None,
) -> None:
    symbol = str(symbol).strip().upper()
    if not symbol:
        return
    next_by_symbol_action = dict(state.get("next_eligible_by_symbol_action") or {})
    next_by_symbol_action[_symbol_action_key(action_id, symbol)] = datetime.fromtimestamp(
        now_dt.timestamp() + float(seconds),
        tz=UTC,
    ).isoformat()
    state["next_eligible_by_symbol_action"] = next_by_symbol_action
    failure_notes = dict(state.get("symbol_failure_notes") or {})
    failure_notes[_symbol_action_key(action_id, symbol)] = {
        "symbol": symbol,
        "action_id": action_id,
        "reason": reason or "symbol_backoff",
        "recorded_at": now_dt.isoformat(),
        "next_eligible_at": next_by_symbol_action[_symbol_action_key(action_id, symbol)],
    }
    state["symbol_failure_notes"] = failure_notes


def _symbol_isolation_active(state: dict[str, Any], action_id: str, symbol: str, now_dt: datetime) -> tuple[bool, str | None, str | None]:
    isolated = dict(state.get("isolated_symbols_by_action") or {})
    row = isolated.get(_symbol_action_key(action_id, symbol)) or {}
    until_dt = _parse_dt(row.get("isolated_until"))
    if until_dt is not None and now_dt < until_dt:
        return True, until_dt.isoformat(), row.get("reason")
    return False, until_dt.isoformat() if until_dt else None, row.get("reason")


def _mark_symbol_isolated(
    state: dict[str, Any],
    action_id: str,
    symbol: str,
    *,
    now_dt: datetime,
    seconds: float,
    reason: str,
    failure_streak: int,
) -> str | None:
    symbol = str(symbol).strip().upper()
    if not symbol or float(seconds) <= 0:
        return None
    until = datetime.fromtimestamp(now_dt.timestamp() + float(seconds), tz=UTC).isoformat()
    isolated = dict(state.get("isolated_symbols_by_action") or {})
    isolated[_symbol_action_key(action_id, symbol)] = {
        "symbol": symbol,
        "action_id": action_id,
        "reason": reason,
        "failure_streak": int(failure_streak),
        "isolated_at": now_dt.isoformat(),
        "isolated_until": until,
    }
    state["isolated_symbols_by_action"] = isolated
    return until


def _clear_symbol_isolation(state: dict[str, Any], action_id: str, symbol: str) -> None:
    isolated = dict(state.get("isolated_symbols_by_action") or {})
    key = _symbol_action_key(action_id, symbol)
    if key in isolated:
        isolated.pop(key, None)
        state["isolated_symbols_by_action"] = isolated


def _update_symbol_status(
    state: dict[str, Any],
    *,
    action_id: str,
    symbols: list[str],
    scope: str,
    status: str,
    now_dt: datetime,
    reason: str | None = None,
    quality: dict[str, Any] | None = None,
    max_failures_before_isolation: int | None = None,
    isolation_seconds: float | None = None,
) -> None:
    symbol_status = dict(state.get("symbol_status") or {})
    quality_by_symbol = {
        str(row.get("symbol") or "").upper(): row
        for row in (quality or {}).get("symbol_quality") or []
        if str(row.get("symbol") or "").strip()
    }
    problem_symbols = {str(symbol).upper() for symbol in (quality or {}).get("problem_symbols") or []}
    for symbol in [str(item).strip().upper() for item in symbols if str(item).strip()]:
        previous = dict(symbol_status.get(symbol) or {})
        row_quality = quality_by_symbol.get(symbol) or {}
        symbol_reason = reason
        symbol_state = status
        if status == "succeeded" and symbol in problem_symbols:
            symbol_state = "low_quality"
            symbol_reason = "low_quote_snapshot_quality"
        elif status == "succeeded":
            symbol_reason = None
        cooldown_key = _symbol_action_key(action_id, symbol)
        cooldown_until = (state.get("next_eligible_by_symbol_action") or {}).get(cooldown_key)
        failure_streak = int(previous.get("failure_streak") or 0)
        isolation_until = previous.get("isolation_until")
        isolation_reason = previous.get("isolation_reason")
        updated = {
            **previous,
            "symbol": symbol,
            "scope": scope,
            "last_action_id": action_id,
            "last_status": symbol_state,
            "last_checked_at": now_dt.isoformat(),
            "last_reason": symbol_reason,
            "last_quality_status": row_quality.get("quality_status"),
            "last_quoted": row_quality.get("quoted"),
            "last_requested": row_quality.get("requested"),
            "last_quoted_rate": row_quality.get("quoted_rate"),
            "last_avg_spread_pct": row_quality.get("avg_spread_pct"),
            "last_data_latency_status": row_quality.get("data_latency_status"),
            "last_delayed": row_quality.get("delayed"),
            "last_delayed_count": row_quality.get("delayed_count"),
            "cooldown_until": cooldown_until,
        }
        if symbol_state == "succeeded":
            _clear_symbol_isolation(state, action_id, symbol)
            failure_streak = 0
            isolation_until = None
            isolation_reason = None
            updated["last_success_at"] = now_dt.isoformat()
            updated["last_success_action_id"] = action_id
            updated["last_failure_at"] = previous.get("last_failure_at")
            updated["last_failure_reason"] = previous.get("last_failure_reason")
        elif symbol_state in {"failed", "timeout", "low_quality"}:
            failure_streak += 1
            updated["last_failure_at"] = now_dt.isoformat()
            updated["last_failure_action_id"] = action_id
            updated["last_failure_reason"] = symbol_reason or symbol_state
            if (
                max_failures_before_isolation is not None
                and int(max_failures_before_isolation) > 0
                and failure_streak >= int(max_failures_before_isolation)
            ):
                isolation_reason = symbol_reason or symbol_state
                isolation_until = _mark_symbol_isolated(
                    state,
                    action_id,
                    symbol,
                    now_dt=now_dt,
                    seconds=float(isolation_seconds or 0.0),
                    reason=isolation_reason,
                    failure_streak=failure_streak,
                )
                if isolation_until:
                    updated["last_status"] = "isolated"
                    updated["last_reason"] = isolation_reason
        updated["failure_streak"] = failure_streak
        updated["isolation_until"] = isolation_until
        updated["isolation_reason"] = isolation_reason
        symbol_status[symbol] = updated
    state["symbol_status"] = symbol_status


def _prune_symbol_state(
    state: dict[str, Any],
    *,
    now_dt: datetime,
    symbol_status_ttl_hours: float,
    symbol_failure_note_ttl_hours: float,
) -> None:
    status_cutoff_seconds = max(0.0, float(symbol_status_ttl_hours)) * 3600.0
    note_cutoff_seconds = max(0.0, float(symbol_failure_note_ttl_hours)) * 3600.0
    if status_cutoff_seconds > 0:
        pruned_status: dict[str, Any] = {}
        for symbol, row in (state.get("symbol_status") or {}).items():
            last_seen = _parse_dt((row or {}).get("last_checked_at"))
            isolation_until = _parse_dt((row or {}).get("isolation_until"))
            cooldown_until = _parse_dt((row or {}).get("cooldown_until"))
            keep_active = (isolation_until is not None and now_dt < isolation_until) or (cooldown_until is not None and now_dt < cooldown_until)
            if keep_active or last_seen is None or (now_dt - last_seen).total_seconds() <= status_cutoff_seconds:
                pruned_status[symbol] = row
        state["symbol_status"] = pruned_status
    if note_cutoff_seconds > 0:
        pruned_notes: dict[str, Any] = {}
        for key, row in (state.get("symbol_failure_notes") or {}).items():
            recorded = _parse_dt((row or {}).get("recorded_at"))
            next_eligible = _parse_dt((row or {}).get("next_eligible_at"))
            keep_active = next_eligible is not None and now_dt < next_eligible
            if keep_active or recorded is None or (now_dt - recorded).total_seconds() <= note_cutoff_seconds:
                pruned_notes[key] = row
        state["symbol_failure_notes"] = pruned_notes
    isolated = {}
    for key, row in (state.get("isolated_symbols_by_action") or {}).items():
        until = _parse_dt((row or {}).get("isolated_until"))
        if until is not None and now_dt < until:
            isolated[key] = row
    state["isolated_symbols_by_action"] = isolated


def _eligible_symbols_for_action(
    state: dict[str, Any],
    action_id: str,
    symbols: list[str],
    now_dt: datetime,
    *,
    critical_retry_symbols: set[str] | None = None,
) -> tuple[list[str], list[dict[str, Any]]]:
    eligible: list[str] = []
    cooldown_rows: list[dict[str, Any]] = []
    normalized_symbols = [str(item).strip().upper() for item in symbols if str(item).strip()]
    critical_retry_symbols = {str(item).strip().upper() for item in (critical_retry_symbols or set()) if str(item).strip()}
    allow_critical_retry = len(normalized_symbols) >= 3
    for symbol in normalized_symbols:
        isolated, isolated_until, isolation_reason = _symbol_isolation_active(state, action_id, symbol, now_dt)
        if isolated:
            row = {"symbol": symbol, "next_eligible_at": isolated_until, "reason": "isolated", "isolation_reason": isolation_reason}
            if allow_critical_retry and symbol in critical_retry_symbols:
                row["override"] = "critical_core_low_budget_retry"
                eligible.append(symbol)
            cooldown_rows.append(row)
            continue
        active, next_at = _symbol_cooldown_active(state, action_id, symbol, now_dt)
        if active:
            row = {"symbol": symbol, "next_eligible_at": next_at, "reason": "cooldown"}
            if allow_critical_retry and symbol in critical_retry_symbols:
                row["override"] = "critical_core_low_budget_retry"
                eligible.append(symbol)
            cooldown_rows.append(row)
        else:
            eligible.append(symbol)
    return eligible, cooldown_rows


def _action_symbols(action: dict[str, Any]) -> list[str]:
    details = action.get("details") if isinstance(action.get("details"), dict) else {}
    symbols = details.get("symbols")
    if isinstance(symbols, list):
        return [str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()]
    command = [str(item) for item in action.get("command") or []]
    for idx, item in enumerate(command):
        if item == "--symbols" and idx + 1 < len(command):
            return [part.strip().upper() for part in command[idx + 1].split(",") if part.strip()]
    return []


def _classify_action_failure(action: dict[str, Any]) -> str | None:
    status = str(action.get("status") or "").lower()
    if status not in {"failed", "timeout"}:
        return None
    if status == "timeout":
        return "timeout"
    text = " ".join(
        str(action.get(key) or "")
        for key in ("error", "stdout_tail", "stderr_tail")
    ).lower()
    if "no underlying contract resolved" in text or "contract_details" in text or "ambiguous contract" in text or "no security definition" in text:
        return "contract_unresolved"
    if "market data farm connection is broken" in text or "not connected" in text or "connection refused" in text or "socket" in text:
        return "ibkr_connection"
    if "market data subscription" in text or "not subscribed" in text or "permission" in text or "permissions" in text or "no market data permissions" in text:
        return "market_data_permission"
    if "no option chain" in text or "candidate_contracts" in text and "0" in text:
        return "no_option_chain"
    if "no_fresh_option_bid_ask_quotes" in text or "bid/ask" in text or "bid ask" in text:
        return "no_bid_ask"
    if "delayed" in text and "only" in text:
        return "delayed_only"
    if "index unavailable" in text:
        return "contract_or_permission_unavailable"
    return "process_error"


def _quote_quality_report(
    payload: dict[str, Any] | None,
    *,
    expected_symbols: list[str],
    min_symbol_quoted_contracts: int,
    min_symbol_quoted_rate: float,
    max_spread_pct: float,
) -> dict[str, Any]:
    data = payload or {}
    expected = [str(symbol).strip().upper() for symbol in expected_symbols if str(symbol).strip()]
    if not data:
        return {
            "quality_status": "missing",
            "quality_score": 0.0,
            "requested_quotes": 0,
            "quoted_contracts": 0,
            "quoted_rate": None,
            "avg_spread_pct": None,
            "live_contracts": 0,
            "delayed_contracts": 0,
            "data_latency_status": "missing",
            "delayed_symbols": expected,
            "market_data_type_counts": {},
            "symbol_quality": [],
            "problem_symbols": expected,
            "reasons": ["quote_snapshot_missing"],
            "reason_counts": {"quote_snapshot_missing": 1},
            "reason_summary": [
                {
                    "reason": "quote_snapshot_missing",
                    "count": 1,
                    "symbols": expected[:8],
                }
            ],
        }
    requested = int(data.get("requested_quotes") or len(data.get("quotes") or []) or 0)
    quoted = int(data.get("quoted_contracts") or 0)
    quoted_rate = data.get("quoted_rate")
    avg_spread = data.get("avg_spread_pct")
    quotes = data.get("quotes") or []
    dte_bucket_counts: Counter[str] = Counter()
    for quote in quotes:
        try:
            dte = int(quote.get("dte"))
        except (TypeError, ValueError):
            dte = 9999
        if dte <= 0:
            bucket = "0dte"
        elif dte <= 7:
            bucket = "weekly_1_7dte"
        elif dte <= 35:
            bucket = "front_month_8_35dte"
        else:
            bucket = "longer_36dte_plus"
        dte_bucket_counts[bucket] += 1
    delayed_quotes = [
        quote for quote in quotes
        if bool(quote.get("delayed")) or int(quote.get("market_data_type") or 0) in {3, 4}
    ]
    market_data_type_counts = dict(Counter(str(quote.get("market_data_type") if quote.get("market_data_type") is not None else "unknown") for quote in quotes))
    delayed_symbols = sorted({str(quote.get("symbol") or "").upper() for quote in delayed_quotes if str(quote.get("symbol") or "").strip()})
    delayed_count = len(delayed_quotes)
    live_count = max(0, len(quotes) - delayed_count)
    if not quotes:
        data_latency_status = "unknown"
    elif delayed_count <= 0:
        data_latency_status = "live"
    elif delayed_count >= len(quotes):
        data_latency_status = "delayed_only"
    else:
        data_latency_status = "mixed_delayed"
    by_symbol_source = {
        str(row.get("symbol") or "").upper(): row
        for row in data.get("by_symbol") or []
        if str(row.get("symbol") or "").strip()
    }
    symbol_rows: list[dict[str, Any]] = []
    problem_symbols: list[str] = []
    for symbol in expected or sorted(by_symbol_source):
        row = by_symbol_source.get(symbol, {})
        symbol_requested = int(row.get("requested") or 0)
        symbol_quoted = int(row.get("quoted") or 0)
        symbol_rate = row.get("quoted_rate")
        symbol_spread = row.get("avg_spread_pct")
        symbol_status_counts = row.get("status_counts") if isinstance(row.get("status_counts"), dict) else {}
        symbol_error_count = int(row.get("error_count") or 0)
        symbol_no_security_count = int(row.get("no_security_definition_error_count") or 0)
        symbol_valid_contract_rate = row.get("valid_contract_rate")
        symbol_quotes = [quote for quote in quotes if str(quote.get("symbol") or "").upper() == symbol]
        symbol_delayed_count = len([
            quote for quote in symbol_quotes
            if bool(quote.get("delayed")) or int(quote.get("market_data_type") or 0) in {3, 4}
        ])
        symbol_delayed = bool(symbol_delayed_count)
        if not symbol_quotes:
            symbol_latency = "unknown"
        elif symbol_delayed_count <= 0:
            symbol_latency = "live"
        elif symbol_delayed_count >= len(symbol_quotes):
            symbol_latency = "delayed_only"
        else:
            symbol_latency = "mixed_delayed"
        reasons: list[str] = []
        if symbol not in by_symbol_source:
            reasons.append("symbol_missing_from_snapshot")
        if symbol_requested <= 0:
            reasons.append("no_requested_contracts")
        if symbol_quoted < int(min_symbol_quoted_contracts):
            reasons.append("too_few_quoted_contracts")
        if symbol_rate is not None and float(symbol_rate) < float(min_symbol_quoted_rate):
            reasons.append("low_quoted_rate")
        if symbol_spread is not None and float(symbol_spread) > float(max_spread_pct):
            reasons.append("wide_average_spread")
        if symbol_no_security_count:
            reasons.append("contract_definition_errors")
        if symbol_latency == "delayed_only":
            reasons.append("delayed_only_market_data")
        elif symbol_latency == "mixed_delayed":
            reasons.append("mixed_delayed_market_data")
        status = "green"
        if {"symbol_missing_from_snapshot", "no_requested_contracts", "too_few_quoted_contracts", "delayed_only_market_data"} & set(reasons):
            status = "red"
        elif reasons:
            status = "yellow"
        if status != "green":
            problem_symbols.append(symbol)
        symbol_rows.append(
            {
                "symbol": symbol,
                "requested": symbol_requested,
                "quoted": symbol_quoted,
                "quoted_rate": symbol_rate,
                "avg_spread_pct": symbol_spread,
                "status_counts": symbol_status_counts,
                "error_count": symbol_error_count,
                "no_security_definition_error_count": symbol_no_security_count,
                "valid_contract_rate": symbol_valid_contract_rate,
                "diagnostic_status": row.get("diagnostic_status"),
                "delayed": symbol_delayed,
                "delayed_count": symbol_delayed_count,
                "data_latency_status": symbol_latency,
                "quality_status": status,
                "reasons": reasons,
            }
        )
    top_reasons = Counter(reason for row in symbol_rows for reason in row.get("reasons") or [])
    reason_summary = [
        {
            "reason": reason,
            "count": count,
            "symbols": [
                row.get("symbol")
                for row in symbol_rows
                if reason in (row.get("reasons") or [])
            ][:8],
        }
        for reason, count in top_reasons.most_common(6)
    ]
    if requested <= 0:
        overall = "red"
    elif quoted <= 0:
        overall = "red"
    elif any(row["quality_status"] == "red" for row in symbol_rows):
        overall = "yellow" if any(row["quality_status"] == "green" for row in symbol_rows) else "red"
    elif any(row["quality_status"] == "yellow" for row in symbol_rows):
        overall = "yellow"
    else:
        overall = "green"
    if data_latency_status == "delayed_only":
        overall = "red"
    elif data_latency_status == "mixed_delayed" and overall == "green":
        overall = "yellow"
    rate_score = float(quoted_rate if quoted_rate is not None else (quoted / requested if requested else 0.0))
    spread_penalty = 0.0
    if avg_spread is not None and max_spread_pct > 0:
        spread_penalty = min(0.35, max(0.0, (float(avg_spread) - float(max_spread_pct)) / float(max_spread_pct) * 0.35))
    symbol_penalty = min(0.40, 0.10 * len(problem_symbols))
    latency_penalty = 0.0
    if data_latency_status == "delayed_only":
        latency_penalty = 35.0
    elif data_latency_status == "mixed_delayed":
        latency_penalty = 15.0
    quality_score = max(0.0, min(100.0, (rate_score * 100.0) - (spread_penalty * 100.0) - (symbol_penalty * 100.0) - latency_penalty))
    return {
        "quality_status": overall,
        "quality_score": round(quality_score, 3),
        "requested_quotes": requested,
        "quoted_contracts": quoted,
        "quoted_rate": quoted_rate,
        "avg_spread_pct": avg_spread,
        "live_contracts": live_count,
        "delayed_contracts": delayed_count,
        "data_latency_status": data_latency_status,
        "delayed_symbols": delayed_symbols,
        "market_data_type_counts": market_data_type_counts,
        "symbol_quality": symbol_rows,
        "problem_symbols": problem_symbols,
        "reasons": sorted(top_reasons),
        "reason_counts": dict(top_reasons),
        "reason_summary": reason_summary,
        "dte_bucket_counts": dict(dte_bucket_counts),
    }


def _symbol_quality_lookup(snapshot_status: dict[str, Any]) -> dict[str, dict[str, Any]]:
    quality = snapshot_status.get("quote_quality") or {}
    return {
        str(row.get("symbol") or "").upper(): row
        for row in quality.get("symbol_quality") or []
        if str(row.get("symbol") or "").strip()
    }


def _role_coverage_status(
    symbols: list[str],
    *,
    quality_maps: list[dict[str, dict[str, Any]]],
    symbol_status: dict[str, Any],
    probe_map: dict[str, dict[str, Any]] | None = None,
    requested_symbols: set[str] | None = None,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    ok_symbols: list[str] = []
    probe_candidate_symbols: list[str] = []
    failed_symbols: list[str] = []
    missing_symbols: list[str] = []
    delayed_symbols: list[str] = []
    isolated_symbols: list[str] = []
    for symbol in [str(item).strip().upper() for item in symbols if str(item).strip()]:
        quality_row: dict[str, Any] | None = None
        for quality_map in quality_maps:
            if symbol in quality_map:
                quality_row = quality_map[symbol]
                break
        status_row = symbol_status.get(symbol) or {}
        probe_row = (probe_map or {}).get(symbol) or {}
        quality_status = str((quality_row or {}).get("quality_status") or "").lower()
        latency = str((quality_row or {}).get("data_latency_status") or "").lower()
        last_status = str(status_row.get("last_status") or "").lower()
        usable = quality_status in {"green", "yellow"} and latency not in {"delayed_only"}
        if not usable and probe_row:
            symbol_reason = probe_row.get("reason")
        elif quality_row is None and requested_symbols is not None and symbol not in requested_symbols:
            symbol_reason = "not_requested_due_to_family_budget_or_scope"
        else:
            symbol_reason = status_row.get("last_failure_reason") or status_row.get("last_reason")
        if usable:
            ok_symbols.append(symbol)
        elif int(probe_row.get("candidate_contracts") or 0) > 0 or str(probe_row.get("status") or "") == "candidate_contracts_available":
            probe_candidate_symbols.append(symbol)
        elif last_status == "isolated":
            isolated_symbols.append(symbol)
            failed_symbols.append(symbol)
        elif quality_row is None:
            missing_symbols.append(symbol)
        else:
            failed_symbols.append(symbol)
        if latency in {"delayed_only", "mixed_delayed"}:
            delayed_symbols.append(symbol)
        issue = _classify_index_option_symbol_issue(
            symbol=symbol,
            role="unknown",
            usable=bool(usable),
            quality_status=quality_status,
            data_latency_status=latency,
            last_status=last_status,
            probe_status=probe_row.get("status"),
            probe_reason=probe_row.get("reason"),
            failure_reason=symbol_reason,
        )
        rows.append(
            {
                "symbol": symbol,
                "quality_status": quality_status or None,
                "data_latency_status": latency or None,
                "last_status": last_status or None,
                "probe_status": probe_row.get("status"),
                "probe_reason": probe_row.get("reason"),
                "candidate_contracts": probe_row.get("candidate_contracts"),
                "chain_rows": probe_row.get("chain_rows"),
                "usable": bool(usable),
                "isolated": last_status == "isolated",
                "failure_reason": symbol_reason,
                "issue_category": issue["category"],
                "issue_severity": issue["severity"],
                "issue_detail": issue["detail"],
                "next_action": issue["next_action"],
            }
        )
    if ok_symbols:
        status = "ok"
    elif isolated_symbols:
        status = "isolated"
    elif failed_symbols:
        status = "failed"
    elif missing_symbols:
        status = "missing"
    else:
        status = "not_configured"
    return {
        "status": status,
        "usable": bool(ok_symbols),
        "symbols": [str(item).strip().upper() for item in symbols if str(item).strip()],
        "ok_symbols": ok_symbols,
        "probe_candidate_symbols": probe_candidate_symbols,
        "failed_symbols": failed_symbols,
        "missing_symbols": missing_symbols,
        "delayed_symbols": delayed_symbols,
        "isolated_symbols": isolated_symbols,
        "rows": rows,
    }


def _classify_index_option_symbol_issue(
    *,
    symbol: str,
    role: str,
    usable: bool,
    quality_status: Any,
    data_latency_status: Any,
    last_status: Any,
    probe_status: Any,
    probe_reason: Any,
    failure_reason: Any,
) -> dict[str, Any]:
    if usable:
        return {
            "symbol": symbol,
            "role": role,
            "category": "usable",
            "severity": "green",
            "detail": "usable_live_or_acceptable_quote_context",
            "next_action": "use_for_family_context",
        }
    quality = str(quality_status or "").lower()
    latency = str(data_latency_status or "").lower()
    last = str(last_status or "").lower()
    probe = str(probe_status or "").lower()
    reason = str(failure_reason or probe_reason or "").lower()
    if latency in {"delayed_only", "mixed_delayed"}:
        category = "delayed_or_subscription"
        detail = f"option quote latency is {latency}"
        next_action = "check_ibkr_live_option_market_data_subscription_or_data_type"
        severity = "red" if latency == "delayed_only" else "yellow"
    elif reason == "not_requested_due_to_family_budget_or_scope" or probe == "not_requested":
        category = "budget_or_scope_not_requested"
        detail = "symbol was not requested by the current family budget or probe scope"
        next_action = "increase_family_budget_or_add_symbol_to_probe_scope_if_needed"
        severity = "yellow"
    elif "permission" in reason or "subscription" in reason:
        category = "permission_or_subscription"
        detail = reason or "IBKR permission/subscription issue suspected"
        next_action = "verify_ibkr_option_market_data_permissions"
        severity = "red"
    elif "contract_unresolved" in reason or "no security definition" in reason or "ambiguous" in reason:
        category = "contract_mapping"
        detail = reason or "IBKR contract mapping unresolved"
        next_action = "repair_ibkr_contract_mapping_trading_class_exchange_sec_type"
        severity = "red"
    elif probe == "underlying_resolved_no_chain" or "no_option_chain" in reason or "no_chain" in reason:
        category = "chain_unavailable"
        detail = reason or "underlying resolved but no option chain returned"
        next_action = "probe_option_chain_exchange_trading_class_or_use_proxy"
        severity = "red" if role in {"direct", "mini"} else "yellow"
    elif probe == "chain_available_no_candidates" or "no_candidate_contracts" in reason:
        category = "chain_available_no_selected_contracts"
        detail = reason or "chain exists but no contracts matched the scan window"
        next_action = "widen_expiry_strike_or_contract_budget_before_retry"
        severity = "yellow"
    elif last == "isolated":
        category = "symbol_isolated_after_repeated_failures"
        detail = reason or "symbol is temporarily isolated after repeated failures"
        next_action = "wait_for_isolation_expiry_then_retry_symbol_specific_probe"
        severity = "red"
    elif quality in {"red", "missing"}:
        category = "low_or_missing_quote_quality"
        detail = reason or f"quote quality is {quality or 'unknown'}"
        next_action = "retry_during_liquid_regular_session_or_use_proxy"
        severity = "red" if quality == "red" else "yellow"
    elif quality == "yellow":
        category = "thin_quote_quality"
        detail = reason or "quote quality is yellow"
        next_action = "use_with_lower_confidence_and_refresh_on_next_cycle"
        severity = "yellow"
    elif probe == "missing_probe":
        category = "missing_probe_rows"
        detail = "no probe rows were available for this symbol"
        next_action = "run_index_option_capability_probe"
        severity = "yellow"
    else:
        category = "unknown_needs_probe"
        detail = reason or "no explicit failure reason available"
        next_action = "run_symbol_level_quote_and_contract_probe"
        severity = "yellow"
    return {
        "symbol": symbol,
        "role": role,
        "category": category,
        "severity": severity,
        "detail": detail,
        "next_action": next_action,
    }


def _role_issue_summary(role_name: str, role_row: dict[str, Any]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for row in role_row.get("rows") or []:
        if row.get("usable"):
            continue
        issue = {
            "symbol": row.get("symbol"),
            "role": role_name,
            "category": row.get("issue_category") or "unknown_needs_probe",
            "severity": row.get("issue_severity") or "yellow",
            "detail": row.get("issue_detail") or row.get("failure_reason") or row.get("probe_reason") or "-",
            "next_action": row.get("next_action") or "run_symbol_level_quote_and_contract_probe",
            "quality_status": row.get("quality_status"),
            "data_latency_status": row.get("data_latency_status"),
            "probe_status": row.get("probe_status"),
            "probe_reason": row.get("probe_reason"),
            "candidate_contracts": row.get("candidate_contracts"),
            "chain_rows": row.get("chain_rows"),
        }
        issues.append(issue)
    return issues


def _family_repair_plan(family: dict[str, Any], *, direct: dict[str, Any], mini: dict[str, Any], proxy: dict[str, Any]) -> dict[str, Any]:
    issues = (
        _role_issue_summary("direct", direct)
        + _role_issue_summary("mini", mini)
        + _role_issue_summary("proxy", proxy)
    )
    selected = str(family.get("selected_source_type") or "none")
    selected_symbols = family.get("selected_symbols") or []
    direct_or_mini_usable = bool(direct.get("usable") or mini.get("usable"))
    proxy_usable = bool(proxy.get("usable"))
    proxy_probe_backed = bool(proxy.get("probe_candidate_symbols"))
    if selected in {"direct", "mini"}:
        operating_mode = "direct_or_mini_usable"
        operator_summary = f"{family.get('label') or family.get('family_id')} can use {selected} symbols {selected_symbols}."
        next_action = "maintain_current_family_mapping_and_monitor_quote_quality"
        severity = "green"
    elif proxy_usable:
        operating_mode = "proxy_fallback_active"
        operator_summary = f"{family.get('label') or family.get('family_id')} is using proxy symbols {selected_symbols}; direct/mini repair is non-blocking."
        next_action = "keep_proxy_for_live_context_and_schedule_direct_mini_repair"
        severity = "yellow"
    elif proxy_probe_backed:
        operating_mode = "proxy_probe_backed_quote_gap"
        operator_summary = f"{family.get('label') or family.get('family_id')} has proxy option contracts available but live quotes are currently too thin or missing."
        next_action = "keep_probe_backed_proxy_on_dashboard_but_do_not_use_as_high_confidence_live_quote_context"
        severity = "yellow"
    elif direct_or_mini_usable:
        operating_mode = "direct_available_proxy_missing"
        operator_summary = f"{family.get('label') or family.get('family_id')} has direct/mini coverage but proxy coverage is weak."
        next_action = "repair_proxy_mapping_if_needed_but_direct_context_is_available"
        severity = "yellow"
    else:
        operating_mode = "coverage_unavailable"
        operator_summary = f"{family.get('label') or family.get('family_id')} has no usable direct, mini, or proxy coverage."
        next_action = "repair_contract_mapping_permission_or_budget_before_using_family_context"
        severity = "red"
    categories = dict(Counter(str(issue.get("category") or "unknown") for issue in issues))
    return {
        "family_id": family.get("family_id"),
        "label": family.get("label"),
        "operating_mode": operating_mode,
        "severity": severity,
        "selected_source_type": selected,
        "selected_symbols": selected_symbols,
        "issue_count": len(issues),
        "issue_category_counts": categories,
        "issues": issues,
        "operator_summary": operator_summary,
        "next_action": next_action,
        "proxy_fallback_active": bool(proxy_usable and not direct_or_mini_usable),
        "proxy_probe_backed": bool(proxy_probe_backed and not proxy_usable and not direct_or_mini_usable),
        "direct_or_mini_usable": direct_or_mini_usable,
        "proxy_usable": proxy_usable,
    }


def _index_option_coverage_matrix(
    config: OptionsMarketStructureSupervisorConfig,
    *,
    core_snapshot_status: dict[str, Any],
    optional_snapshot_status: dict[str, Any],
    symbol_status: dict[str, Any],
    probe_payload: dict[str, Any] | None = None,
    requested_symbols: set[str] | None = None,
    quote_contract_budgets: dict[str, Any] | None = None,
    family_budget_allocation: dict[str, int] | None = None,
) -> dict[str, Any]:
    core_quality = _symbol_quality_lookup(core_snapshot_status)
    optional_quality = _symbol_quality_lookup(optional_snapshot_status)
    quality_maps = [optional_quality, core_quality]
    probe_symbols = sorted({symbol for family in (config.index_option_families or _default_index_option_families()) for symbol in _family_all_symbols(family)})
    probe_map = _probe_status_map(probe_payload, probe_symbols)
    families: list[dict[str, Any]] = []
    confidence_counts: Counter[str] = Counter()
    status_counts: Counter[str] = Counter()
    direct_ok_count = 0
    proxy_ok_count = 0
    for family in config.index_option_families or _default_index_option_families():
        family_id = str(family.get("family_id") or "").strip()
        direct = _role_coverage_status(family.get("direct_symbols") or [], quality_maps=quality_maps, symbol_status=symbol_status, probe_map=probe_map, requested_symbols=requested_symbols)
        mini = _role_coverage_status(family.get("mini_symbols") or [], quality_maps=quality_maps, symbol_status=symbol_status, probe_map=probe_map, requested_symbols=requested_symbols)
        proxy = _role_coverage_status(family.get("proxy_symbols") or [], quality_maps=quality_maps, symbol_status=symbol_status, probe_map=probe_map, requested_symbols=requested_symbols)
        direct_ok = bool(direct["usable"] or mini["usable"])
        proxy_ok = bool(proxy["usable"])
        proxy_probe_backed = bool(proxy.get("probe_candidate_symbols"))
        if direct["usable"]:
            selected_source_type = "direct"
            selected_symbols = direct["ok_symbols"]
            fallback_reason = "direct_index_usable"
        elif mini["usable"]:
            selected_source_type = "mini"
            selected_symbols = mini["ok_symbols"]
            fallback_reason = "direct_unavailable_using_mini_index"
        elif proxy["usable"]:
            selected_source_type = "proxy"
            selected_symbols = proxy["ok_symbols"]
            fallback_reason = "direct_and_mini_unavailable_using_proxy"
        elif proxy_probe_backed:
            selected_source_type = "proxy_probe"
            selected_symbols = proxy.get("probe_candidate_symbols") or []
            fallback_reason = "direct_and_mini_unavailable_using_probe_backed_proxy_quote_gap"
        else:
            selected_source_type = "none"
            selected_symbols = []
            fallback_reason = "no_usable_direct_mini_or_proxy_coverage"
        if direct_ok and proxy_ok:
            coverage_status = "direct_and_proxy_ok"
            confidence = "high"
        elif direct_ok:
            coverage_status = "direct_ok_proxy_missing"
            confidence = "medium"
        elif proxy_ok:
            coverage_status = "proxy_only"
            confidence = "medium"
        elif proxy_probe_backed:
            coverage_status = "proxy_probe_only"
            confidence = "low"
        elif direct["status"] in {"failed", "isolated"} or mini["status"] in {"failed", "isolated"}:
            coverage_status = "direct_failed_proxy_unavailable"
            confidence = "low"
        else:
            coverage_status = "unavailable"
            confidence = "red"
        direct_ok_count += 1 if direct_ok else 0
        proxy_ok_count += 1 if proxy_ok else 0
        confidence_counts[confidence] += 1
        status_counts[coverage_status] += 1
        effective_operating_status = (
            "direct_active"
            if selected_source_type in {"direct", "mini"}
            else "proxy_active_warning"
            if selected_source_type == "proxy"
            else "proxy_probe_watch_only"
            if selected_source_type == "proxy_probe"
            else "unavailable"
        )
        scorer_use_strength = (
            "strong"
            if confidence == "high"
            else "medium"
            if confidence == "medium" and selected_source_type in {"direct", "mini", "proxy"}
            else "observe_only"
            if selected_source_type == "proxy_probe"
            else "disabled"
        )
        family_row = {
            "family_id": family.get("family_id"),
            "label": family.get("label"),
            "coverage_status": coverage_status,
            "confidence": confidence,
            "effective_operating_status": effective_operating_status,
            "scorer_use_strength": scorer_use_strength,
            "direct_failure_non_blocking": bool(not direct_ok and proxy_ok),
            "selected_source_type": selected_source_type,
            "selected_symbols": selected_symbols,
            "fallback_reason": fallback_reason,
            "family_quote_budget": (family_budget_allocation or {}).get(family_id),
            "direct": direct,
            "mini": mini,
            "proxy": proxy,
            "non_blocking_note": "Direct/mini index failure is non-blocking while proxy coverage remains usable." if proxy_ok and not direct_ok else "Proxy has option contracts but live quote quality is currently too thin; use as low-confidence context only." if proxy_probe_backed and not direct_ok else None,
            "notes": family.get("notes"),
        }
        family_row["repair_plan"] = _family_repair_plan(family_row, direct=direct, mini=mini, proxy=proxy)
        families.append(
            family_row
        )
    if confidence_counts.get("red"):
        overall = "yellow" if proxy_ok_count or direct_ok_count else "red"
    elif confidence_counts.get("low"):
        overall = "yellow"
    else:
        overall = "green"
    issue_counts: Counter[str] = Counter()
    repair_plans = []
    for family in families:
        plan = family.get("repair_plan") or {}
        repair_plans.append(plan)
        issue_counts.update(plan.get("issue_category_counts") or {})
    return {
        "version": "index-option-family-coverage-v1",
        "overall_status": overall,
        "family_count": len(families),
        "direct_or_mini_ok_count": direct_ok_count,
        "proxy_ok_count": proxy_ok_count,
        "confidence_counts": dict(confidence_counts),
        "coverage_status_counts": dict(status_counts),
        "families": families,
        "family_repair_plan": repair_plans,
        "issue_category_counts": dict(issue_counts),
        "quote_contract_budgets": quote_contract_budgets or {},
        "family_quote_budget_allocation": family_budget_allocation or {},
        "probe_summary": {
            "generated_at": (probe_payload or {}).get("generated_at"),
            "completed": (probe_payload or {}).get("completed"),
            "probed_symbols": (probe_payload or {}).get("symbols") or [],
        },
        "resolver_policy": {
            "candidate_order": "direct_index_then_mini_index_then_etf_or_futures_proxy",
            "direct_index_failure_blocks_core_proxy": False,
            "use_proxy_when_direct_unavailable": True,
        },
    }


def _defer_index_option_coverage_matrix(matrix: dict[str, Any], *, reason: str) -> dict[str, Any]:
    deferred = json.loads(json.dumps(matrix))
    deferred["overall_status"] = "deferred_until_regular_session"
    deferred["confidence_counts"] = {"deferred": len(deferred.get("families") or [])}
    deferred["coverage_status_counts"] = {"deferred_until_regular_session": len(deferred.get("families") or [])}
    deferred["deferred_reason"] = reason
    for family in deferred.get("families") or []:
        family["coverage_status"] = "deferred_until_regular_session"
        family["confidence"] = "deferred"
        family["non_blocking_note"] = "Index option family coverage is deferred until regular listed-option quote refresh is allowed."
    return deferred


def _requested_symbols_from_snapshots(*payloads: dict[str, Any]) -> set[str]:
    symbols: set[str] = set()
    for payload in payloads:
        if not payload:
            continue
        for symbol in (payload.get("config") or {}).get("symbols") or []:
            symbols.add(str(symbol).upper())
        for row in payload.get("by_symbol") or []:
            if str(row.get("symbol") or "").strip():
                symbols.add(str(row.get("symbol")).upper())
        for row in payload.get("quotes") or []:
            if str(row.get("symbol") or "").strip():
                symbols.add(str(row.get("symbol")).upper())
    return symbols


def _m220_live_validation_checklist(matrix: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for family in matrix.get("families") or []:
        label = family.get("label") or family.get("family_id")
        for role in ("direct", "mini", "proxy"):
            role_row = family.get(role) or {}
            symbols = role_row.get("symbols") or []
            if not symbols:
                continue
            rows.append(
                {
                    "family_id": family.get("family_id"),
                    "family": label,
                    "role": role,
                    "symbols": symbols,
                    "passed": bool(role_row.get("usable")),
                    "status": role_row.get("status"),
                    "ok_symbols": role_row.get("ok_symbols") or [],
                    "failed_symbols": role_row.get("failed_symbols") or [],
                    "delayed_symbols": role_row.get("delayed_symbols") or [],
                    "check": f"{label} {role} option coverage usable",
                    "next_action": (
                        "use_for_family_context"
                        if role_row.get("usable")
                        else _role_issue_summary(role, role_row)[0]["next_action"]
                        if _role_issue_summary(role, role_row)
                        else "probe_contract_chain_or_fallback_to_next_role"
                    ),
                    "issue_categories": sorted(
                        set(
                            str(issue.get("category"))
                            for issue in _role_issue_summary(role, role_row)
                            if issue.get("category")
                        )
                    ),
                }
            )
    return rows


def _index_family_budget_tuning_observations(matrix: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for family in matrix.get("families") or []:
        direct = family.get("direct") or {}
        mini = family.get("mini") or {}
        proxy = family.get("proxy") or {}
        coverage = str(family.get("coverage_status") or "")
        confidence = str(family.get("confidence") or "")
        selected = str(family.get("selected_source_type") or "none")
        budget = family.get("family_quote_budget")
        role_rows = list(direct.get("rows") or []) + list(mini.get("rows") or []) + list(proxy.get("rows") or [])
        requested_count = len([row for row in role_rows if str((row or {}).get("failure_reason") or "") != "not_requested_due_to_family_budget_or_scope"])
        usable_count = len([row for row in role_rows if bool((row or {}).get("usable"))])
        candidate_contracts = sum(int((row or {}).get("candidate_contracts") or 0) for row in role_rows)
        chain_rows = sum(int((row or {}).get("chain_rows") or 0) for row in role_rows)
        not_requested = [
            row.get("symbol")
            for row in role_rows
            if str((row or {}).get("failure_reason") or "") == "not_requested_due_to_family_budget_or_scope"
        ]
        if confidence == "deferred" or coverage == "deferred_until_regular_session":
            suggestion = "hold_until_regular_session_validation"
        elif selected == "proxy_probe":
            suggestion = "increase_proxy_quote_budget_or_refresh_when_liquidity_improves"
        elif confidence in {"red", "low"}:
            suggestion = "increase_or_reallocate_budget_after_contract_probe"
        elif selected in {"direct", "mini"} and usable_count > 0:
            suggestion = "maintain_or_slightly_increase_if_close_window_relevant"
        elif selected == "proxy":
            suggestion = "maintain_proxy_budget_but_prioritize_direct_mini_repair"
        else:
            suggestion = "keep_minimum_budget_until_more_live_samples"
        rows.append(
            {
                "family_id": family.get("family_id"),
                "label": family.get("label"),
                "allocated_budget": budget,
                "selected_source_type": selected,
                "selected_symbols": family.get("selected_symbols") or [],
                "coverage_status": coverage,
                "confidence": confidence,
                "usable_role_symbol_count": usable_count,
                "requested_role_symbol_count": requested_count,
                "not_requested_symbols": [symbol for symbol in not_requested if symbol],
                "candidate_contracts": candidate_contracts,
                "chain_rows": chain_rows,
                "suggested_budget_bias": suggestion,
                "repair_next_action": (family.get("repair_plan") or {}).get("next_action"),
                "issue_category_counts": (family.get("repair_plan") or {}).get("issue_category_counts") or {},
            }
        )
    return rows


def _m220_family_issue_summary(matrix: dict[str, Any]) -> dict[str, Any]:
    plans = matrix.get("family_repair_plan") or []
    severity_counts = Counter(str(plan.get("severity") or "yellow") for plan in plans)
    issue_counts = Counter()
    proxy_fallback_families = []
    proxy_probe_backed_families = []
    unavailable_families = []
    effective_usage_rows = []
    effective_status_counts = Counter()
    scorer_strength_counts = Counter()
    family_lookup = {str(row.get("family_id") or ""): row for row in matrix.get("families") or []}
    for plan in plans:
        issue_counts.update(plan.get("issue_category_counts") or {})
        family = family_lookup.get(str(plan.get("family_id") or "")) or {}
        effective_status = str(family.get("effective_operating_status") or plan.get("operating_mode") or "unknown")
        scorer_strength = str(family.get("scorer_use_strength") or "observe_only")
        effective_status_counts[effective_status] += 1
        scorer_strength_counts[scorer_strength] += 1
        effective_usage_rows.append(
            {
                "family_id": plan.get("family_id"),
                "label": plan.get("label"),
                "effective_operating_status": effective_status,
                "scorer_use_strength": scorer_strength,
                "selected_source_type": plan.get("selected_source_type"),
                "selected_symbols": plan.get("selected_symbols") or [],
                "direct_failure_non_blocking": bool(family.get("direct_failure_non_blocking")),
                "severity": plan.get("severity"),
                "operator_summary": plan.get("operator_summary"),
            }
        )
        if plan.get("proxy_fallback_active"):
            proxy_fallback_families.append(plan.get("label") or plan.get("family_id"))
        if plan.get("proxy_probe_backed"):
            proxy_probe_backed_families.append(plan.get("label") or plan.get("family_id"))
        if plan.get("operating_mode") == "coverage_unavailable":
            unavailable_families.append(plan.get("label") or plan.get("family_id"))
    return {
        "version": "m220-family-issue-summary-v1",
        "family_count": len(plans),
        "severity_counts": dict(severity_counts),
        "issue_category_counts": dict(issue_counts),
        "proxy_fallback_families": [item for item in proxy_fallback_families if item],
        "proxy_probe_backed_families": [item for item in proxy_probe_backed_families if item],
        "coverage_unavailable_families": [item for item in unavailable_families if item],
        "effective_operating_status_counts": dict(effective_status_counts),
        "scorer_use_strength_counts": dict(scorer_strength_counts),
        "effective_family_usage_rows": effective_usage_rows,
        "repair_plan": plans,
    }


def _optional_index_effective_quality_status(raw_status: Any, matrix: dict[str, Any]) -> dict[str, Any]:
    raw = str(raw_status or "missing").lower()
    summary = _m220_family_issue_summary(matrix)
    usage_counts = summary.get("effective_operating_status_counts") or {}
    proxy_active = int(usage_counts.get("proxy_active_warning") or 0)
    direct_active = int(usage_counts.get("direct_active") or 0)
    unavailable = len(summary.get("coverage_unavailable_families") or [])
    if raw in {"red", "missing"} and unavailable == 0 and (proxy_active or direct_active):
        return {
            "status": "proxy_active_warning",
            "display_status": "yellow",
            "raw_status": raw,
            "reason": "optional_direct_index_quotes_are_weak_but_family_proxy_or_direct_coverage_is_operational",
        }
    return {
        "status": raw,
        "display_status": raw,
        "raw_status": raw,
        "reason": "raw_optional_index_quality_status_applies",
    }


def _option_quote_deferred_operating_state(tick: dict[str, Any]) -> dict[str, Any]:
    session_gate = tick.get("session_gate") or {}
    action_counts = tick.get("action_status_counts") or {}
    failed_count = int(tick.get("core_failed_action_count") or 0) + int(tick.get("optional_failed_action_count") or 0)
    quote_allowed = bool(session_gate.get("allowed"))
    deferred_count = int(action_counts.get("deferred_until_regular_session") or 0)
    blocked_missing_count = int(action_counts.get("blocked_missing_quote_snapshot") or 0)
    session = str(session_gate.get("session") or "").lower()
    if not quote_allowed and deferred_count > 0 and failed_count == 0:
        status = "normal_session_deferred"
        severity = "info"
        actionable_now = False
        interpretation = "Listed option bid/ask and index-option capability probe are regular-session dependent; current deferred actions are expected outside regular option hours."
    elif quote_allowed and (deferred_count > 0 or blocked_missing_count > 0):
        status = "unexpected_regular_session_gap"
        severity = "yellow"
        actionable_now = True
        interpretation = "Option evidence is still deferred or blocked even though the option session appears eligible; run the M220 live validation command."
    else:
        status = "not_deferred"
        severity = "green"
        actionable_now = False
        interpretation = "No regular-session option quote deferral is active."
    return {
        "status": status,
        "severity": severity,
        "actionable_now": actionable_now,
        "session": session,
        "quote_allowed": quote_allowed,
        "deferred_action_count": deferred_count,
        "blocked_missing_quote_snapshot_count": blocked_missing_count,
        "next_regular_open": tick.get("next_regular_open"),
        "interpretation": interpretation,
        "alert_policy": "do_not_alert_as_failure" if status == "normal_session_deferred" else "alert_if_actionable_gap",
    }


def _m220_operational_runbook(result: dict[str, Any]) -> dict[str, Any]:
    deferred = result.get("option_quote_deferred_operating_state") or {}
    checklist = result.get("m220_live_validation_checklist") or []
    command = (
        ".\\.venv\\Scripts\\python.exe main.py options-market-structure-supervisor "
        "--profile-json config\\options_market_structure_supervisor_profile_v1.json --execute-actions "
        "--output exports\\m220_options_market_structure_live_validation.md "
        "--json-output-path exports\\m220_options_market_structure_live_validation.json"
    )
    dashboard_command = (
        ".\\.venv\\Scripts\\python.exe main.py trading-dashboard-snapshot "
        "--profile-json config\\trading_dashboard_snapshot_profile_v1.json "
        "--output exports\\m220_trading_dashboard_snapshot.md "
        "--json-output-path exports\\m220_trading_dashboard_snapshot.json"
    )
    html_command = (
        ".\\.venv\\Scripts\\python.exe main.py trading-dashboard-html "
        "--profile-json config\\trading_dashboard_snapshot_profile_v1.json "
        "--snapshot-json exports\\m220_trading_dashboard_snapshot.json "
        "--output exports\\trading_command_center.html --refresh-seconds 1"
    )
    return {
        "version": "m220-options-live-validation-runbook-v1",
        "regular_session_required": True,
        "current_deferred_state": deferred,
        "one_command": command,
        "follow_up_commands": [dashboard_command, html_command],
        "success_criteria": [
            "index_option_capability_probe succeeds or returns explicit per-symbol contract/permission/no-chain reasons",
            "core ETF quote quality is green/yellow and not delayed_only",
            "index family matrix selects direct or mini where available and proxy fallback where not available",
            "M220 checklist rows pass for at least one usable source per important family",
            "dashboard Options Market Structure panel shows selected source, budget, and checklist without stale-live-use ambiguity",
        ],
        "checklist_row_count": len(checklist),
        "safe_to_run_now": bool(deferred.get("actionable_now")) or bool(result.get("final_option_quote_allowed")),
        "operator_note": (
            "If current state is normal_session_deferred, wait until regular listed-option hours before expecting live probe/quote rows."
        ),
    }


def _minute_of_day_from_hhmm(value: str) -> int:
    hour, minute = str(value).split(":", 1)
    return int(hour) * 60 + int(minute)


def _session_refresh_policy(
    config: OptionsMarketStructureSupervisorConfig,
    session_gate: dict[str, Any],
) -> dict[str, Any]:
    session = str(session_gate.get("session") or "unknown")
    max_age = float(config.max_snapshot_age_minutes)
    phase = session
    priority = "normal"
    reason = "default_snapshot_freshness_policy"
    minutes_since_open: float | None = None
    minutes_to_close: float | None = None
    if session == "regular":
        try:
            now_local = datetime.fromisoformat(str(session_gate.get("now_local") or "").replace("Z", "+00:00"))
        except ValueError:
            now_local = None
        if now_local is not None:
            minute = now_local.hour * 60 + now_local.minute + now_local.second / 60.0
            start_min = float(_minute_of_day_from_hhmm(config.regular_start))
            end_min = float(_minute_of_day_from_hhmm(config.regular_end))
            minutes_since_open = round(max(0.0, minute - start_min), 3)
            minutes_to_close = round(max(0.0, end_min - minute), 3)
            if minutes_since_open < float(config.open_warmup_minutes):
                phase = "regular_open_warmup"
                priority = "warmup"
                max_age = float(config.warmup_max_snapshot_age_minutes)
                reason = "option_quotes_can_be_unstable_immediately_after_open"
            elif minutes_to_close <= float(config.close_refresh_window_minutes):
                phase = "regular_close_window"
                priority = "elevated"
                max_age = min(float(config.max_snapshot_age_minutes), float(config.close_window_max_snapshot_age_minutes))
                reason = "gamma_pin_and_max_pain_context_has_higher_close_window_value"
            else:
                phase = "regular_midday"
                priority = "normal"
                reason = "regular_session_normal_refresh_policy"
    elif session in {"pre_market", "after_hours", "closed"}:
        reason = "option_quote_refresh_deferred_outside_regular_session"
    return {
        "phase": phase,
        "priority": priority,
        "max_snapshot_age_minutes": float(max_age),
        "open_warmup_minutes": float(config.open_warmup_minutes),
        "close_refresh_window_minutes": float(config.close_refresh_window_minutes),
        "minutes_since_open": minutes_since_open,
        "minutes_to_close": minutes_to_close,
        "reason": reason,
    }


def _artifact_delta(before: dict[str, Any] | None, after: dict[str, Any] | None) -> dict[str, Any]:
    before = before or {}
    after = after or {}
    before_quality = before.get("quote_quality") or {}
    after_quality = after.get("quote_quality") or {}

    def _num_or_none(value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    before_quote_age = _num_or_none(before.get("quote_age_seconds"))
    after_quote_age = _num_or_none(after.get("quote_age_seconds"))
    before_structure_age = _num_or_none(before.get("structure_age_seconds"))
    after_structure_age = _num_or_none(after.get("structure_age_seconds"))
    before_score = _num_or_none(before_quality.get("quality_score"))
    after_score = _num_or_none(after_quality.get("quality_score"))
    before_problem_count = len(before_quality.get("problem_symbols") or [])
    after_problem_count = len(after_quality.get("problem_symbols") or [])
    quote_age_delta = None if before_quote_age is None or after_quote_age is None else round(after_quote_age - before_quote_age, 3)
    structure_age_delta = None if before_structure_age is None or after_structure_age is None else round(after_structure_age - before_structure_age, 3)
    score_delta = None if before_score is None or after_score is None else round(after_score - before_score, 3)
    problem_delta = before_problem_count - after_problem_count
    improved_signals = 0
    worsened_signals = 0
    if before_quote_age is None and after_quote_age is not None:
        improved_signals += 1
    elif quote_age_delta is not None:
        if quote_age_delta < -1.0:
            improved_signals += 1
        elif quote_age_delta > 60.0:
            worsened_signals += 1
    if before_structure_age is None and after_structure_age is not None:
        improved_signals += 1
    elif structure_age_delta is not None:
        if structure_age_delta < -1.0:
            improved_signals += 1
        elif structure_age_delta > 60.0:
            worsened_signals += 1
    if score_delta is not None:
        if score_delta > 0.01:
            improved_signals += 1
        elif score_delta < -0.01:
            worsened_signals += 1
    if problem_delta > 0:
        improved_signals += 1
    elif problem_delta < 0:
        worsened_signals += 1
    if improved_signals and not worsened_signals:
        outcome = "improved"
    elif worsened_signals and not improved_signals:
        outcome = "worsened"
    elif improved_signals and worsened_signals:
        outcome = "mixed"
    elif before or after:
        outcome = "unchanged"
    else:
        outcome = "unknown"
    return {
        "outcome": outcome,
        "improved_signal_count": improved_signals,
        "worsened_signal_count": worsened_signals,
        "quote_age_delta_seconds": quote_age_delta,
        "structure_age_delta_seconds": structure_age_delta,
        "quote_quality_score_delta": score_delta,
        "problem_symbol_count_before": before_problem_count,
        "problem_symbol_count_after": after_problem_count,
        "problem_symbol_count_delta_improvement": problem_delta,
        "before_quality_status": before_quality.get("quality_status"),
        "after_quality_status": after_quality.get("quality_status"),
    }


def _symbol_csv(symbols: list[str]) -> str:
    return ",".join(sorted({str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()}))


def _reference_price_csv(reference_prices: dict[str, float]) -> str:
    return ",".join(f"{symbol}={price}" for symbol, price in sorted(reference_prices.items()) if price is not None)


def _market_data_reference_prices(path: Path | None, symbols: list[str]) -> dict[str, float]:
    payload = _read_json(path)
    wanted = {str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()}
    if not payload or not wanted:
        return {}
    rows = payload.get("rows") or []
    prices: dict[str, float] = {}
    by_key: dict[str, float] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = str(row.get("key") or row.get("symbol") or "").strip().upper()
        if not key:
            continue
        price = row.get("price")
        try:
            value = float(price)
        except (TypeError, ValueError):
            continue
        if value > 0:
            by_key[key] = value
    proxy_price_map = {
        "SPX": ("SPX", "ES"),
        "XSP": ("XSP", "SPY"),
        "NDX": ("NDX", "NQ"),
        "XND": ("XND", "QQQ"),
        "RUT": ("RUT", "RTY", "IWM"),
        "VIX": ("VIX",),
        "VXX": ("VXX",),
        "VIXY": ("VIXY", "VXX"),
        "UVXY": ("UVXY", "VXX"),
        "SPY": ("SPY",),
        "QQQ": ("QQQ",),
        "IWM": ("IWM",),
        "DIA": ("DIA", "YM"),
    }
    for symbol in wanted:
        for source in proxy_price_map.get(symbol, (symbol,)):
            if source in by_key:
                prices[symbol] = by_key[source]
                break
    return prices


def _effective_reference_prices(config: OptionsMarketStructureSupervisorConfig, symbols: list[str]) -> dict[str, float]:
    prices = _market_data_reference_prices(config.market_data_json_path, symbols)
    prices.update({str(k).upper(): float(v) for k, v in (config.reference_prices or {}).items() if v is not None})
    return {symbol: value for symbol, value in prices.items() if symbol in {str(item).upper() for item in symbols}}


def _quote_command(
    config: OptionsMarketStructureSupervisorConfig,
    *,
    symbols: list[str] | None = None,
    quote_json_path: Path | None = None,
    quote_markdown_path: Path | None = None,
    include_underlying_specs: bool = False,
    max_quote_contracts: int | None = None,
) -> list[str]:
    symbols = symbols or config.symbols
    quote_json_path = quote_json_path or config.quote_json_path
    quote_markdown_path = quote_markdown_path or config.quote_markdown_path
    command = [
        sys.executable,
        "main.py",
        "ibkr-options-quote-snapshot",
        "--symbols",
        _symbol_csv(symbols),
        "--min-dte",
        str(config.min_dte),
        "--max-dte",
        str(config.max_dte),
        "--strikes-around",
        str(config.strikes_around),
        "--max-expirations",
        str(config.max_expirations),
        "--max-quote-contracts",
        str(max_quote_contracts if max_quote_contracts is not None else config.max_quote_contracts),
        "--market-data-type",
        str(config.market_data_type),
        "--generic-ticks",
        str(config.generic_ticks),
        "--chain-timeout",
        str(config.chain_timeout_seconds),
        "--quote-seconds",
        str(config.quote_seconds),
        "--max-spread-pct",
        str(config.max_spread_pct),
        "--output",
        str(quote_markdown_path),
        "--json-output-path",
        str(quote_json_path),
    ]
    references = _reference_price_csv(_effective_reference_prices(config, symbols))
    if references:
        command.extend(["--reference-prices", references])
    if config.client_id is not None:
        command.extend(["--client-id", str(config.client_id)])
    if include_underlying_specs:
        sec_types = {symbol: config.underlying_sec_types.get(symbol) for symbol in symbols if config.underlying_sec_types.get(symbol)}
        exchanges = {symbol: config.underlying_exchanges.get(symbol) for symbol in symbols if config.underlying_exchanges.get(symbol)}
        if sec_types:
            command.extend(["--underlying-sec-types", ",".join(f"{symbol}={value}" for symbol, value in sorted(sec_types.items()))])
        if exchanges:
            command.extend(["--underlying-exchanges", ",".join(f"{symbol}={value}" for symbol, value in sorted(exchanges.items()))])
    return command


def _chain_probe_command(
    config: OptionsMarketStructureSupervisorConfig,
    *,
    symbols: list[str],
) -> list[str]:
    command = [
        sys.executable,
        "main.py",
        "ibkr-options-chain-scan",
        "--symbols",
        _symbol_csv(symbols),
        "--min-dte",
        str(config.min_dte),
        "--max-dte",
        str(config.max_dte),
        "--strikes-around",
        str(config.strikes_around),
        "--max-expirations",
        str(config.max_expirations),
        "--timeout",
        str(config.chain_timeout_seconds),
    ]
    references = _reference_price_csv(_effective_reference_prices(config, symbols))
    if references:
        command.extend(["--reference-prices", references])
    if config.client_id is not None:
        command.extend(["--client-id", str(config.client_id)])
    sec_types = {symbol: config.underlying_sec_types.get(symbol) for symbol in symbols if config.underlying_sec_types.get(symbol)}
    exchanges = {symbol: config.underlying_exchanges.get(symbol) for symbol in symbols if config.underlying_exchanges.get(symbol)}
    if sec_types:
        command.extend(["--underlying-sec-types", ",".join(f"{symbol}={value}" for symbol, value in sorted(sec_types.items()))])
    if exchanges:
        command.extend(["--underlying-exchanges", ",".join(f"{symbol}={value}" for symbol, value in sorted(exchanges.items()))])
    if config.index_option_capability_probe_markdown_path is not None:
        command.extend(["--output", str(config.index_option_capability_probe_markdown_path)])
    if config.index_option_capability_probe_json_path is not None:
        command.extend(["--json-output-path", str(config.index_option_capability_probe_json_path)])
    return command


def _structure_command(
    config: OptionsMarketStructureSupervisorConfig,
    *,
    quote_json_path: Path | None = None,
    structure_json_path: Path | None = None,
    structure_markdown_path: Path | None = None,
) -> list[str]:
    quote_json_path = quote_json_path or config.quote_json_path
    structure_json_path = structure_json_path or config.structure_json_path
    structure_markdown_path = structure_markdown_path or config.structure_markdown_path
    command = [
        sys.executable,
        "main.py",
        "options-market-structure-desk",
        "--quote-json",
        str(quote_json_path),
        "--pin-near-pct",
        str(config.pin_near_pct),
        "--proximity-near-pct",
        str(config.proximity_near_pct),
        "--max-snapshot-age-minutes",
        str(config.max_snapshot_age_minutes),
        "--output",
        str(structure_markdown_path),
        "--json-output-path",
        str(structure_json_path),
    ]
    if config.market_data_json_path is not None:
        command.extend(["--market-data-json", str(config.market_data_json_path)])
    return command


def _option_structure_outcome_command(
    config: OptionsMarketStructureSupervisorConfig,
    *,
    structure_json_path: Path | None = None,
    outcome_json_path: Path | None = None,
    outcome_markdown_path: Path | None = None,
    symbols: list[str] | None = None,
) -> list[str]:
    structure_json_path = structure_json_path or config.structure_json_path
    command = [
        sys.executable,
        "main.py",
        "option-structure-outcome-build",
        "--options-market-structure-json",
        str(structure_json_path),
        "--horizons",
        str(config.option_structure_outcome_horizons),
        "--flat-threshold",
        str(config.option_structure_outcome_flat_threshold),
        "--price-source",
        str(config.option_structure_outcome_price_source),
    ]
    if symbols:
        command.extend(["--symbols", _symbol_csv(symbols)])
    if config.client_id is not None:
        command.extend(["--ibkr-client-id", str(config.client_id + 1200)])
    if outcome_markdown_path is not None:
        command.extend(["--output", str(outcome_markdown_path)])
    if outcome_json_path is not None:
        command.extend(["--json-output-path", str(outcome_json_path)])
    return command


def load_options_market_structure_supervisor_config(profile_path: Path | None = None, *, overrides: dict[str, Any] | None = None) -> OptionsMarketStructureSupervisorConfig:
    profile: dict[str, Any] = {}
    base: Path | None = None
    if profile_path is not None:
        resolved = profile_path.expanduser().resolve()
        profile = _read_json(resolved)
        base = resolved.parent
    overrides = overrides or {}
    symbol_groups = profile.get("symbol_groups") if isinstance(profile.get("symbol_groups"), dict) else {}
    symbols = overrides.get("symbols") or profile.get("symbols") or symbol_groups.get("core_etf_proxies") or ["SPY", "QQQ", "IWM", "DIA"]
    if isinstance(symbols, str):
        symbols = [item.strip().upper() for item in symbols.split(",") if item.strip()]
    optional_index_symbols = symbol_groups.get("optional_index_options") or profile.get("optional_index_symbols") or []
    if isinstance(optional_index_symbols, str):
        optional_index_symbols = [item.strip().upper() for item in optional_index_symbols.split(",") if item.strip()]
    index_option_families = _normalize_index_option_families(profile.get("index_option_families"))
    references = profile.get("reference_prices") if isinstance(profile.get("reference_prices"), dict) else {}
    references = {str(k).upper(): float(v) for k, v in references.items() if v is not None}
    underlying_sec_types = profile.get("underlying_sec_types") if isinstance(profile.get("underlying_sec_types"), dict) else {}
    underlying_exchanges = profile.get("underlying_exchanges") if isinstance(profile.get("underlying_exchanges"), dict) else {}
    underlying_sec_types = {str(k).upper(): str(v).upper() for k, v in underlying_sec_types.items() if v}
    underlying_exchanges = {str(k).upper(): str(v).upper() for k, v in underlying_exchanges.items() if v}
    paths = profile.get("paths") if isinstance(profile.get("paths"), dict) else {}
    quote = profile.get("quote_snapshot") if isinstance(profile.get("quote_snapshot"), dict) else {}
    structure = profile.get("options_market_structure") if isinstance(profile.get("options_market_structure"), dict) else {}
    outcome = profile.get("option_structure_outcomes") if isinstance(profile.get("option_structure_outcomes"), dict) else {}
    schedule = profile.get("schedule") if isinstance(profile.get("schedule"), dict) else {}
    outputs = profile.get("outputs") if isinstance(profile.get("outputs"), dict) else {}
    return OptionsMarketStructureSupervisorConfig(
        symbols=[str(item).strip().upper() for item in symbols if str(item).strip()],
        optional_index_symbols=[str(item).strip().upper() for item in optional_index_symbols if str(item).strip()],
        index_option_families=index_option_families,
        reference_prices=references,
        underlying_sec_types=underlying_sec_types,
        underlying_exchanges=underlying_exchanges,
        quote_json_path=_profile_path(paths.get("quote_json_path") or outputs.get("quote_json_path") or "../exports/m218_fresh_option_quote_snapshot.json", base=base) or Path("exports/m218_fresh_option_quote_snapshot.json"),
        quote_markdown_path=_profile_path(paths.get("quote_markdown_path") or outputs.get("quote_markdown_path") or "../exports/m218_fresh_option_quote_snapshot.md", base=base) or Path("exports/m218_fresh_option_quote_snapshot.md"),
        structure_json_path=_profile_path(paths.get("structure_json_path") or outputs.get("structure_json_path") or "../exports/m217_options_market_structure_desk.json", base=base) or Path("exports/m217_options_market_structure_desk.json"),
        structure_markdown_path=_profile_path(paths.get("structure_markdown_path") or outputs.get("structure_markdown_path") or "../exports/m217_options_market_structure_desk.md", base=base) or Path("exports/m217_options_market_structure_desk.md"),
        optional_index_quote_json_path=_profile_path(paths.get("optional_index_quote_json_path") or "../exports/m219_optional_index_option_quote_snapshot.json", base=base),
        optional_index_quote_markdown_path=_profile_path(paths.get("optional_index_quote_markdown_path") or "../exports/m219_optional_index_option_quote_snapshot.md", base=base),
        optional_index_structure_json_path=_profile_path(paths.get("optional_index_structure_json_path") or "../exports/m219_optional_index_options_market_structure_desk.json", base=base),
        optional_index_structure_markdown_path=_profile_path(paths.get("optional_index_structure_markdown_path") or "../exports/m219_optional_index_options_market_structure_desk.md", base=base),
        option_structure_outcome_enabled=bool(outcome.get("enabled", True)),
        option_structure_outcome_price_source=str(outcome.get("price_source", "ibkr")),
        option_structure_outcome_horizons=str(outcome.get("horizons", "5M,15M,30M,EOD")),
        option_structure_outcome_flat_threshold=float(outcome.get("flat_threshold", 0.001)),
        option_structure_outcome_json_path=_profile_path(paths.get("option_structure_outcome_json_path") or outcome.get("json_path") or "../exports/m222_option_structure_outcome_log.json", base=base),
        option_structure_outcome_markdown_path=_profile_path(paths.get("option_structure_outcome_markdown_path") or outcome.get("markdown_path") or "../exports/m222_option_structure_outcome_log.md", base=base),
        optional_index_option_structure_outcome_json_path=_profile_path(paths.get("optional_index_option_structure_outcome_json_path") or outcome.get("optional_index_json_path") or "../exports/m222_optional_index_option_structure_outcome_log.json", base=base),
        optional_index_option_structure_outcome_markdown_path=_profile_path(paths.get("optional_index_option_structure_outcome_markdown_path") or outcome.get("optional_index_markdown_path") or "../exports/m222_optional_index_option_structure_outcome_log.md", base=base),
        index_option_capability_probe_json_path=_profile_path(paths.get("index_option_capability_probe_json_path") or "../exports/m219_index_option_capability_probe.json", base=base),
        index_option_capability_probe_markdown_path=_profile_path(paths.get("index_option_capability_probe_markdown_path") or "../exports/m219_index_option_capability_probe.md", base=base),
        market_data_json_path=_profile_path(paths.get("market_data_json_path") or paths.get("market_data_json"), base=base),
        state_json_path=_profile_path(paths.get("state_json_path") or outputs.get("state_json_path") or "../exports/m218_options_market_structure_supervisor_state.json", base=base),
        tick_log_jsonl_path=_profile_path(paths.get("tick_log_jsonl_path") or outputs.get("tick_log_jsonl_path") or "../exports/m218_options_market_structure_supervisor_ticks.jsonl", base=base),
        max_ticks=int(overrides.get("max_ticks") if overrides.get("max_ticks") is not None else schedule.get("max_ticks", 1)),
        interval_seconds=float(overrides.get("interval_seconds") if overrides.get("interval_seconds") is not None else schedule.get("interval_seconds", 60.0)),
        sleep_between_ticks=bool(overrides.get("sleep_between_ticks") if overrides.get("sleep_between_ticks") is not None else schedule.get("sleep_between_ticks", False)),
        execute_actions=bool(overrides.get("execute_actions") if overrides.get("execute_actions") is not None else schedule.get("execute_actions", False)),
        regular_session_only=bool(schedule.get("regular_session_only", True)),
        timezone_name=str(schedule.get("timezone", "America/New_York")),
        regular_start=str(schedule.get("regular_start", "09:30")),
        regular_end=str(schedule.get("regular_end", "16:00")),
        max_snapshot_age_minutes=float(overrides.get("max_snapshot_age_minutes") if overrides.get("max_snapshot_age_minutes") is not None else schedule.get("max_snapshot_age_minutes", structure.get("max_snapshot_age_minutes", 30.0))),
        warmup_max_snapshot_age_minutes=float(schedule.get("warmup_max_snapshot_age_minutes", 45.0)),
        close_window_max_snapshot_age_minutes=float(schedule.get("close_window_max_snapshot_age_minutes", 10.0)),
        open_warmup_minutes=float(schedule.get("open_warmup_minutes", 15.0)),
        close_refresh_window_minutes=float(schedule.get("close_refresh_window_minutes", 60.0)),
        quote_refresh_cooldown_seconds=float(schedule.get("quote_refresh_cooldown_seconds", 300.0)),
        proactive_quote_refresh_ratio=float(schedule.get("proactive_quote_refresh_ratio", 0.55)),
        structure_refresh_cooldown_seconds=float(schedule.get("structure_refresh_cooldown_seconds", 30.0)),
        capability_probe_cooldown_seconds=float(schedule.get("capability_probe_cooldown_seconds", 600.0)),
        symbol_failure_backoff_seconds=float(schedule.get("symbol_failure_backoff_seconds", 120.0)),
        low_quality_backoff_seconds=float(schedule.get("low_quality_backoff_seconds", 60.0)),
        max_symbol_failures_before_isolation=int(schedule.get("max_symbol_failures_before_isolation", 3)),
        symbol_isolation_seconds=float(schedule.get("symbol_isolation_seconds", 900.0)),
        symbol_status_ttl_hours=float(schedule.get("symbol_status_ttl_hours", 72.0)),
        symbol_failure_note_ttl_hours=float(schedule.get("symbol_failure_note_ttl_hours", 24.0)),
        min_symbol_quoted_contracts=int(quote.get("min_symbol_quoted_contracts", 2)),
        min_symbol_quoted_rate=float(quote.get("min_symbol_quoted_rate", 0.20)),
        min_dte=int(quote.get("min_dte", 0)),
        max_dte=int(quote.get("max_dte", 45)),
        strikes_around=int(quote.get("strikes_around", 4)),
        max_expirations=int(quote.get("max_expirations", 3)),
        max_quote_contracts=int(quote.get("max_quote_contracts", 240)),
        max_quote_contracts_by_phase=_int_mapping(quote.get("max_quote_contracts_by_phase")),
        optional_index_max_quote_contracts_by_phase=_int_mapping(quote.get("optional_index_max_quote_contracts_by_phase")),
        index_family_quote_budget_weights=_float_mapping(quote.get("index_family_quote_budget_weights")),
        market_data_type=int(quote.get("market_data_type", 1)),
        generic_ticks=str(quote.get("generic_ticks", "100,101,104,106")),
        chain_timeout_seconds=float(quote.get("chain_timeout_seconds", 20.0)),
        quote_seconds=float(quote.get("quote_seconds", 10.0)),
        max_spread_pct=float(quote.get("max_spread_pct", 0.30)),
        client_id=int(quote["client_id"]) if quote.get("client_id") is not None else None,
        pin_near_pct=float(structure.get("pin_near_pct", 0.5)),
        proximity_near_pct=float(structure.get("proximity_near_pct", 1.0)),
        job_timeout_seconds=float(schedule.get("job_timeout_seconds", 240.0)),
    )


def _snapshot_status(
    config: OptionsMarketStructureSupervisorConfig,
    now_dt: datetime,
    *,
    quote_json_path: Path | None = None,
    structure_json_path: Path | None = None,
    expected_symbols: list[str] | None = None,
    max_snapshot_age_minutes: float | None = None,
) -> dict[str, Any]:
    quote_payload = _read_json(quote_json_path or config.quote_json_path)
    structure_payload = _read_json(structure_json_path or config.structure_json_path)
    quote_generated_at = quote_payload.get("generated_at")
    structure_generated_at = structure_payload.get("generated_at")
    quote_age_seconds = _age_seconds(quote_generated_at, now_dt=now_dt)
    structure_age_seconds = _age_seconds(structure_generated_at, now_dt=now_dt)
    effective_max_age = float(max_snapshot_age_minutes if max_snapshot_age_minutes is not None else config.max_snapshot_age_minutes)
    quote_fresh = quote_age_seconds is not None and quote_age_seconds <= effective_max_age * 60.0
    proactive_refresh_age_seconds = max(60.0, effective_max_age * 60.0 * max(0.1, min(0.95, float(config.proactive_quote_refresh_ratio))))
    quote_refresh_due = quote_age_seconds is None or quote_age_seconds >= proactive_refresh_age_seconds
    quality = _quote_quality_report(
        quote_payload,
        expected_symbols=expected_symbols or config.symbols,
        min_symbol_quoted_contracts=config.min_symbol_quoted_contracts,
        min_symbol_quoted_rate=config.min_symbol_quoted_rate,
        max_spread_pct=config.max_spread_pct,
    )
    quote_usable = bool(quote_fresh) and quality.get("quality_status") in {"green", "yellow"}
    return {
        "quote_exists": bool(quote_payload),
        "quote_generated_at": quote_generated_at,
        "quote_age_seconds": round(quote_age_seconds, 3) if quote_age_seconds is not None else None,
        "quote_fresh": bool(quote_fresh),
        "quote_refresh_due": bool(quote_refresh_due),
        "proactive_refresh_age_seconds": round(proactive_refresh_age_seconds, 3),
        "quote_usable": bool(quote_usable),
        "quote_quality": quality,
        "structure_exists": bool(structure_payload),
        "structure_generated_at": structure_generated_at,
        "structure_age_seconds": round(structure_age_seconds, 3) if structure_age_seconds is not None else None,
        "structure_status": structure_payload.get("status"),
        "structure_freshness_status": (structure_payload.get("session_guard") or {}).get("freshness_status"),
        "max_snapshot_age_minutes": effective_max_age,
    }


def _plan_tick(config: OptionsMarketStructureSupervisorConfig, *, state: dict[str, Any], now: str | None = None) -> dict[str, Any]:
    now_dt = _parse_dt(now) or datetime.now(UTC)
    session_gate = evaluate_option_quote_session_gate(
        now=now_dt.isoformat(),
        timezone_name=config.timezone_name,
        regular_start=config.regular_start,
        regular_end=config.regular_end,
        regular_session_only=config.regular_session_only,
    )
    next_open = next_regular_option_session_open(
        now=now_dt.isoformat(),
        timezone_name=config.timezone_name,
        regular_start=config.regular_start,
        regular_end=config.regular_end,
    )
    session_refresh_policy = _session_refresh_policy(config, session_gate)
    effective_max_age = float(session_refresh_policy.get("max_snapshot_age_minutes") or config.max_snapshot_age_minutes)
    phase = str(session_refresh_policy.get("phase") or "")
    core_quote_contract_budget = _phase_quote_contract_budget(config, phase, optional_index=False)
    optional_index_quote_contract_budget = _phase_quote_contract_budget(config, phase, optional_index=True)
    snapshot = _snapshot_status(config, now_dt, expected_symbols=config.symbols, max_snapshot_age_minutes=effective_max_age)
    optional_snapshot = _snapshot_status(
        config,
        now_dt,
        quote_json_path=config.optional_index_quote_json_path,
        structure_json_path=config.optional_index_structure_json_path,
        expected_symbols=config.optional_index_symbols,
        max_snapshot_age_minutes=effective_max_age,
    ) if config.optional_index_symbols else {}
    actions: list[dict[str, Any]] = []
    proactive_refresh_allowed = phase != "regular_open_warmup"
    quote_needed = (not bool(snapshot.get("quote_usable"))) or (bool(snapshot.get("quote_refresh_due")) and proactive_refresh_allowed)
    probe_symbols = _index_probe_symbols(config)
    probe_cd_active, probe_next = _cooldown_active(state, "index_option_capability_probe", now_dt)
    quote_cd_active, quote_next = _cooldown_active(state, "fresh_option_quote_snapshot", now_dt)
    structure_cd_active, structure_next = _cooldown_active(state, "options_market_structure_rebuild", now_dt)
    if config.optional_index_symbols and probe_symbols:
        if not bool(session_gate.get("allowed")):
            actions.append(
                _note_action(
                    "index_option_capability_probe",
                    "Index option capability probe is deferred until regular listed-option session.",
                    priority=5,
                    status="deferred_until_regular_session",
                    details={"next_regular_open": next_open, "symbols": probe_symbols, "session_gate": session_gate},
                )
            )
        elif probe_cd_active:
            actions.append(
                _note_action(
                    "index_option_capability_probe",
                    "Index option capability probe cooldown is still active.",
                    priority=5,
                    status="cooldown",
                    details={"next_eligible_at": probe_next, "symbols": probe_symbols},
                )
            )
        else:
            actions.append(
                _command_action(
                    "index_option_capability_probe",
                    "Run read-only IBKR option-chain capability probe for direct, mini, and proxy index-option families.",
                    _chain_probe_command(config, symbols=probe_symbols),
                    priority=5,
                    live_ibkr=True,
                    details={
                        "symbols": probe_symbols,
                        "scope": "index_option_family_capability_probe",
                        "family_quote_budget_allocation": _index_family_budget_allocation(config, phase, optional_index_quote_contract_budget),
                    },
                )
            )
    if quote_needed:
        if not bool(session_gate.get("allowed")):
            actions.append(
                _note_action(
                    "fresh_option_quote_snapshot",
                    "Option quote snapshot is stale/missing, but listed-option bid/ask refresh is deferred until regular session.",
                    priority=10,
                    status="deferred_until_regular_session",
                    details={"next_regular_open": next_open, "session_gate": session_gate},
                )
            )
        elif quote_cd_active:
            actions.append(
                _note_action(
                    "fresh_option_quote_snapshot",
                    "Fresh option quote snapshot is due but cooldown is still active.",
                    priority=10,
                    status="cooldown",
                    details={"next_eligible_at": quote_next},
                )
            )
        else:
            eligible_symbols, symbol_cooldowns = _eligible_symbols_for_action(
                state,
                "fresh_option_quote_snapshot",
                config.symbols,
                now_dt,
                critical_retry_symbols=CRITICAL_CORE_OPTION_RETRY_SYMBOLS.intersection({str(item).strip().upper() for item in config.symbols}),
            )
            if symbol_cooldowns:
                actions.append(
                    _note_action(
                        "fresh_option_quote_snapshot_symbol_cooldowns",
                        "Some core ETF symbols are temporarily backed off after a prior quote refresh failure or low-quality snapshot.",
                        priority=9,
                        status="symbol_cooldown",
                        details={"symbols": symbol_cooldowns},
                    )
                )
            if not eligible_symbols:
                actions.append(
                    _note_action(
                        "fresh_option_quote_snapshot",
                        "Fresh option quote snapshot is due but all core ETF symbols are in symbol-level cooldown.",
                        priority=10,
                        status="cooldown",
                        details={"symbol_cooldowns": symbol_cooldowns},
                    )
                )
            else:
                actions.append(
                    _command_action(
                        "fresh_option_quote_snapshot",
                        "Run read-only IBKR option quote snapshot for option market-structure symbols.",
                        _quote_command(config, symbols=eligible_symbols, max_quote_contracts=core_quote_contract_budget),
                        priority=10,
                        live_ibkr=True,
                        details={
                            "symbols": eligible_symbols,
                            "scope": "core_etf_proxy",
                            "max_quote_contracts": core_quote_contract_budget,
                            "critical_retry_symbols": [
                                row.get("symbol")
                                for row in symbol_cooldowns
                                if row.get("override") == "critical_core_low_budget_retry"
                            ],
                        },
                    )
                )
    else:
        actions.append(
            _note_action(
                "fresh_option_quote_snapshot",
                "Existing option quote snapshot is fresh enough for market-structure use.",
                priority=10,
                status="not_needed",
                details=snapshot,
            )
        )
    if structure_cd_active:
        actions.append(
            _note_action(
                "options_market_structure_rebuild",
                "Options market-structure rebuild cooldown is still active.",
                priority=20,
                status="cooldown",
                details={"next_eligible_at": structure_next},
            )
        )
    elif snapshot.get("quote_exists"):
        actions.append(
            _command_action(
                "options_market_structure_rebuild",
                "Rebuild M217 option market-structure report from the latest available quote snapshot.",
                _structure_command(config),
                priority=20,
                live_ibkr=False,
            )
        )
    else:
        actions.append(
            _note_action(
                "options_market_structure_rebuild",
                "No option quote snapshot exists yet, so the M217 structure report cannot be rebuilt.",
                priority=20,
                status="blocked_missing_quote_snapshot",
                details={"quote_json_path": str(config.quote_json_path)},
            )
        )
    if config.optional_index_symbols:
        optional_quote_needed = (not bool(optional_snapshot.get("quote_usable"))) or (bool(optional_snapshot.get("quote_refresh_due")) and proactive_refresh_allowed)
        optional_quote_cd_active, optional_quote_next = _cooldown_active(state, "optional_index_option_quote_snapshot", now_dt)
        optional_structure_cd_active, optional_structure_next = _cooldown_active(state, "optional_index_options_market_structure_rebuild", now_dt)
        if optional_quote_needed:
            if not bool(session_gate.get("allowed")):
                actions.append(
                    _note_action(
                        "optional_index_option_quote_snapshot",
                        "Optional index option quote snapshot is stale/missing, but refresh is deferred until regular session. ETF proxy coverage is kept separate.",
                        priority=30,
                        status="deferred_until_regular_session",
                        details={"next_regular_open": next_open, "session_gate": session_gate, "symbols": config.optional_index_symbols},
                    )
                )
            elif optional_quote_cd_active:
                actions.append(
                    _note_action(
                        "optional_index_option_quote_snapshot",
                        "Optional index option quote snapshot is due but cooldown is still active.",
                        priority=30,
                        status="cooldown",
                        details={"next_eligible_at": optional_quote_next},
                    )
                )
            else:
                eligible_symbols, symbol_cooldowns = _eligible_symbols_for_action(
                    state,
                    "optional_index_option_quote_snapshot",
                    config.optional_index_symbols,
                    now_dt,
                )
                if symbol_cooldowns:
                    actions.append(
                        _note_action(
                            "optional_index_option_quote_snapshot_symbol_cooldowns",
                            "Some optional index symbols are temporarily backed off after a prior quote refresh failure or low-quality snapshot.",
                            priority=29,
                            status="symbol_cooldown",
                            details={"symbols": symbol_cooldowns},
                        )
                    )
                if not eligible_symbols:
                    actions.append(
                        _note_action(
                            "optional_index_option_quote_snapshot",
                            "Optional index option quote snapshot is due but all optional index symbols are in symbol-level cooldown.",
                            priority=30,
                            status="cooldown",
                            details={"symbol_cooldowns": symbol_cooldowns},
                        )
                    )
                else:
                    actions.append(
                        _command_action(
                            "optional_index_option_quote_snapshot",
                            "Run optional read-only IBKR index option quote snapshot. Failure does not block ETF proxy structure coverage.",
                            _quote_command(
                                config,
                                symbols=eligible_symbols,
                                quote_json_path=config.optional_index_quote_json_path,
                                quote_markdown_path=config.optional_index_quote_markdown_path,
                                include_underlying_specs=True,
                                max_quote_contracts=optional_index_quote_contract_budget,
                            ),
                            priority=30,
                            live_ibkr=True,
                            details={"symbols": eligible_symbols, "scope": "optional_index", "max_quote_contracts": optional_index_quote_contract_budget},
                        )
                    )
        else:
            actions.append(
                _note_action(
                    "optional_index_option_quote_snapshot",
                    "Existing optional index option quote snapshot is fresh enough.",
                    priority=30,
                    status="not_needed",
                    details=optional_snapshot,
                )
            )
        if optional_structure_cd_active:
            actions.append(
                _note_action(
                    "optional_index_options_market_structure_rebuild",
                    "Optional index market-structure rebuild cooldown is still active.",
                    priority=40,
                    status="cooldown",
                    details={"next_eligible_at": optional_structure_next},
                )
            )
        elif optional_snapshot.get("quote_exists"):
            actions.append(
                _command_action(
                    "optional_index_options_market_structure_rebuild",
                    "Rebuild optional index option market-structure report from latest available index quote snapshot.",
                    _structure_command(
                        config,
                        quote_json_path=config.optional_index_quote_json_path,
                        structure_json_path=config.optional_index_structure_json_path,
                        structure_markdown_path=config.optional_index_structure_markdown_path,
                    ),
                    priority=40,
                    live_ibkr=False,
                )
            )
        else:
            actions.append(
                _note_action(
                    "optional_index_options_market_structure_rebuild",
                    "No optional index quote snapshot exists yet, so optional index structure cannot be rebuilt.",
                    priority=40,
                    status="blocked_missing_quote_snapshot",
                    details={"quote_json_path": str(config.optional_index_quote_json_path), "symbols": config.optional_index_symbols},
                )
            )
    return {
        "checked_at": now_dt.isoformat(),
        "session_gate": session_gate,
        "session_refresh_policy": session_refresh_policy,
        "quote_contract_budgets": {
            "core": core_quote_contract_budget,
            "optional_index": optional_index_quote_contract_budget,
        },
        "next_regular_open": next_open,
        "snapshot_status": snapshot,
        "optional_index_snapshot_status": optional_snapshot,
        "actions": sorted(actions, key=lambda row: int(row.get("priority") or 0)),
    }


def _supervisor_result_from_ticks(
    *,
    config: OptionsMarketStructureSupervisorConfig,
    run_id: str,
    started_at: str,
    completed_at: str,
    tick_summaries: list[dict[str, Any]],
    state: dict[str, Any],
) -> dict[str, Any]:
    final_tick = tick_summaries[-1] if tick_summaries else {}
    m220_checklist = _m220_live_validation_checklist(final_tick.get("index_option_coverage_matrix") or {})
    m220_family_issue_summary = _m220_family_issue_summary(final_tick.get("index_option_coverage_matrix") or {})
    optional_effective_quality = _optional_index_effective_quality_status(
        final_tick.get("optional_index_quote_quality_status"),
        final_tick.get("index_option_coverage_matrix") or {},
    )
    result = {
        "options_market_structure_supervisor_version": OPTIONS_MARKET_STRUCTURE_SUPERVISOR_VERSION,
        "run_id": run_id,
        "started_at": started_at,
        "completed_at": completed_at,
        "executed_tick_count": len(tick_summaries),
        "max_ticks": int(config.max_ticks),
        "interval_seconds": float(config.interval_seconds),
        "sleep_between_ticks": bool(config.sleep_between_ticks),
        "execute_actions": bool(config.execute_actions),
        "final_status": final_tick.get("status"),
        "final_session": (final_tick.get("session_gate") or {}).get("session"),
        "final_session_refresh_phase": (final_tick.get("session_refresh_policy") or {}).get("phase"),
        "final_session_refresh_priority": (final_tick.get("session_refresh_policy") or {}).get("priority"),
        "final_effective_max_snapshot_age_minutes": (final_tick.get("session_refresh_policy") or {}).get("max_snapshot_age_minutes"),
        "final_option_quote_allowed": (final_tick.get("session_gate") or {}).get("allowed"),
        "final_quote_fresh": (final_tick.get("snapshot_status") or {}).get("quote_fresh"),
        "final_quote_usable": (final_tick.get("snapshot_status") or {}).get("quote_usable"),
        "final_quote_quality_status": final_tick.get("quote_quality_status"),
        "final_quote_quality_score": final_tick.get("quote_quality_score"),
        "final_quote_data_latency_status": final_tick.get("quote_data_latency_status"),
        "final_quote_delayed_contracts": final_tick.get("quote_delayed_contracts"),
        "final_quote_delayed_symbols": final_tick.get("quote_delayed_symbols") or [],
        "final_quote_quality_reason_counts": final_tick.get("quote_quality_reason_counts") or {},
        "final_quote_quality_reason_summary": final_tick.get("quote_quality_reason_summary") or [],
        "final_quote_dte_bucket_counts": final_tick.get("quote_dte_bucket_counts") or {},
        "final_structure_status": (final_tick.get("snapshot_status") or {}).get("structure_status"),
        "final_optional_index_quote_fresh": (final_tick.get("optional_index_snapshot_status") or {}).get("quote_fresh"),
        "final_optional_index_quote_usable": (final_tick.get("optional_index_snapshot_status") or {}).get("quote_usable"),
        "final_optional_index_quote_quality_status": final_tick.get("optional_index_quote_quality_status"),
        "final_optional_index_effective_quote_quality_status": optional_effective_quality.get("status"),
        "final_optional_index_effective_quote_quality_display_status": optional_effective_quality.get("display_status"),
        "final_optional_index_effective_quote_quality_reason": optional_effective_quality.get("reason"),
        "final_optional_index_quote_quality_score": final_tick.get("optional_index_quote_quality_score"),
        "final_optional_index_quote_data_latency_status": final_tick.get("optional_index_quote_data_latency_status"),
        "final_optional_index_quote_delayed_contracts": final_tick.get("optional_index_quote_delayed_contracts"),
        "final_optional_index_quote_delayed_symbols": final_tick.get("optional_index_quote_delayed_symbols") or [],
        "final_optional_index_quote_quality_reason_counts": final_tick.get("optional_index_quote_quality_reason_counts") or {},
        "final_optional_index_quote_quality_reason_summary": final_tick.get("optional_index_quote_quality_reason_summary") or [],
        "final_optional_index_quote_dte_bucket_counts": final_tick.get("optional_index_quote_dte_bucket_counts") or {},
        "final_optional_index_structure_status": (final_tick.get("optional_index_snapshot_status") or {}).get("structure_status"),
        "final_action_status_counts": final_tick.get("action_status_counts") or {},
        "final_failure_reason_counts": final_tick.get("failure_reason_counts") or {},
        "final_m222_option_structure_outcome_actions": final_tick.get("m222_option_structure_outcome_actions") or [],
        "final_m222_failed_action_count": final_tick.get("m222_failed_action_count") or 0,
        "final_artifact_delta": final_tick.get("artifact_delta") or {},
        "final_optional_index_artifact_delta": final_tick.get("optional_index_artifact_delta") or {},
        "final_index_option_coverage_status": (final_tick.get("index_option_coverage_matrix") or {}).get("overall_status"),
        "final_index_option_confidence_counts": (final_tick.get("index_option_coverage_matrix") or {}).get("confidence_counts") or {},
        "index_option_coverage_matrix": final_tick.get("index_option_coverage_matrix") or {},
        "index_option_capability_cache": state.get("index_option_capability_cache") or {},
        "final_quote_contract_budgets": final_tick.get("quote_contract_budgets") or {},
        "index_family_quote_budget_allocation": final_tick.get("index_family_quote_budget_allocation") or {},
        "index_family_budget_tuning_observations": final_tick.get("index_family_budget_tuning_observations") or [],
        "m220_family_issue_summary": m220_family_issue_summary,
        "m220_family_repair_plan": m220_family_issue_summary.get("repair_plan") or [],
        "option_quote_deferred_operating_state": final_tick.get("option_quote_deferred_operating_state") or {},
        "m220_live_validation_checklist": m220_checklist,
        "symbol_failure_notes": state.get("symbol_failure_notes") or {},
        "symbol_status": state.get("symbol_status") or {},
        "isolated_symbols_by_action": state.get("isolated_symbols_by_action") or {},
        "isolated_symbol_count": len(state.get("isolated_symbols_by_action") or {}),
        "failed_actions": [action for tick in tick_summaries for action in tick.get("actions") or [] if action.get("status") in {"failed", "timeout"}],
        "tick_summaries": tick_summaries,
        "config": {
            "symbols": config.symbols,
            "optional_index_symbols": config.optional_index_symbols,
            "index_option_families": config.index_option_families,
            "quote_json_path": str(config.quote_json_path),
            "structure_json_path": str(config.structure_json_path),
            "optional_index_quote_json_path": str(config.optional_index_quote_json_path) if config.optional_index_quote_json_path else None,
            "optional_index_structure_json_path": str(config.optional_index_structure_json_path) if config.optional_index_structure_json_path else None,
            "option_structure_outcome_enabled": bool(config.option_structure_outcome_enabled),
            "option_structure_outcome_price_source": config.option_structure_outcome_price_source,
            "option_structure_outcome_horizons": config.option_structure_outcome_horizons,
            "option_structure_outcome_json_path": str(config.option_structure_outcome_json_path) if config.option_structure_outcome_json_path else None,
            "optional_index_option_structure_outcome_json_path": str(config.optional_index_option_structure_outcome_json_path) if config.optional_index_option_structure_outcome_json_path else None,
            "index_option_capability_probe_json_path": str(config.index_option_capability_probe_json_path) if config.index_option_capability_probe_json_path else None,
            "regular_session_only": config.regular_session_only,
            "max_snapshot_age_minutes": config.max_snapshot_age_minutes,
            "warmup_max_snapshot_age_minutes": config.warmup_max_snapshot_age_minutes,
            "close_window_max_snapshot_age_minutes": config.close_window_max_snapshot_age_minutes,
            "proactive_quote_refresh_ratio": config.proactive_quote_refresh_ratio,
            "open_warmup_minutes": config.open_warmup_minutes,
            "close_refresh_window_minutes": config.close_refresh_window_minutes,
            "min_symbol_quoted_contracts": config.min_symbol_quoted_contracts,
            "min_symbol_quoted_rate": config.min_symbol_quoted_rate,
            "symbol_failure_backoff_seconds": config.symbol_failure_backoff_seconds,
            "low_quality_backoff_seconds": config.low_quality_backoff_seconds,
            "max_symbol_failures_before_isolation": config.max_symbol_failures_before_isolation,
            "symbol_isolation_seconds": config.symbol_isolation_seconds,
            "symbol_status_ttl_hours": config.symbol_status_ttl_hours,
            "symbol_failure_note_ttl_hours": config.symbol_failure_note_ttl_hours,
            "max_quote_contracts_by_phase": config.max_quote_contracts_by_phase or {},
            "optional_index_max_quote_contracts_by_phase": config.optional_index_max_quote_contracts_by_phase or {},
            "index_family_quote_budget_weights": config.index_family_quote_budget_weights or {},
        },
        "safety": {
            "read_only": True,
            "option_quote_snapshot_uses_reqMktData_only": True,
            "ibkr_order_submitted": False,
            "transmit_enabled": False,
            "live_order_routing_enabled": False,
            "portfolio_mutated": False,
        },
    }
    result["m220_operational_runbook"] = _m220_operational_runbook(result)
    return result


def run_options_market_structure_supervisor(
    *,
    config: OptionsMarketStructureSupervisorConfig,
    cwd: Path,
    command_runner: CommandRunner | None = None,
    now: str | None = None,
    run_id: str | None = None,
    live_json_output_path: Path | None = None,
    live_markdown_output_path: Path | None = None,
) -> dict[str, Any]:
    runner = command_runner or _default_command_runner
    started_at = _iso_now()
    run_id = run_id or f"options_market_structure_supervisor_{started_at.replace(':', '').replace('+', 'Z')}"
    state = _read_json(config.state_json_path)
    state["options_market_structure_supervisor_version"] = OPTIONS_MARKET_STRUCTURE_SUPERVISOR_VERSION
    state.setdefault("next_eligible_by_action", {})
    state.setdefault("next_eligible_by_symbol_action", {})
    state.setdefault("symbol_failure_notes", {})
    state.setdefault("symbol_status", {})
    state.setdefault("isolated_symbols_by_action", {})
    _prune_symbol_state(
        state,
        now_dt=_parse_dt(now) or datetime.now(UTC),
        symbol_status_ttl_hours=config.symbol_status_ttl_hours,
        symbol_failure_note_ttl_hours=config.symbol_failure_note_ttl_hours,
    )
    tick_summaries: list[dict[str, Any]] = []
    tick_index = 0
    continuous = int(config.max_ticks) == 0
    while continuous or tick_index < int(config.max_ticks):
        tick_index += 1
        planned = _plan_tick(config, state=state, now=now)
        actions = planned["actions"]
        executed: list[dict[str, Any]] = []
        tick_now_dt = _parse_dt(planned["checked_at"]) or datetime.now(UTC)
        for action in actions:
            if config.execute_actions and action.get("status") == "planned":
                action = _run_action(action, cwd=cwd, runner=runner, timeout_seconds=config.job_timeout_seconds)
                action_id = str(action.get("action_id") or "")
                if action_id == "fresh_option_quote_snapshot" and action.get("status") == "succeeded":
                    _mark_cooldown(state, "fresh_option_quote_snapshot", now_dt=tick_now_dt, seconds=config.quote_refresh_cooldown_seconds)
                if action_id == "index_option_capability_probe" and action.get("status") == "succeeded":
                    _mark_cooldown(state, "index_option_capability_probe", now_dt=tick_now_dt, seconds=config.capability_probe_cooldown_seconds)
                if action_id == "options_market_structure_rebuild" and action.get("status") == "succeeded":
                    _mark_cooldown(state, "options_market_structure_rebuild", now_dt=tick_now_dt, seconds=config.structure_refresh_cooldown_seconds)
                if action_id == "optional_index_option_quote_snapshot" and action.get("status") == "succeeded":
                    _mark_cooldown(state, "optional_index_option_quote_snapshot", now_dt=tick_now_dt, seconds=config.quote_refresh_cooldown_seconds)
                if action_id == "optional_index_options_market_structure_rebuild" and action.get("status") == "succeeded":
                    _mark_cooldown(state, "optional_index_options_market_structure_rebuild", now_dt=tick_now_dt, seconds=config.structure_refresh_cooldown_seconds)
                if action.get("status") in {"failed", "timeout"}:
                    action_symbols = _action_symbols(action)
                    for symbol in action_symbols:
                        _mark_symbol_cooldown(
                            state,
                            action_id,
                            symbol,
                            now_dt=tick_now_dt,
                            seconds=config.symbol_failure_backoff_seconds,
                            reason=action.get("failure_reason") or str(action.get("status") or "failed"),
                        )
                    _update_symbol_status(
                        state,
                        action_id=action_id,
                        symbols=action_symbols,
                        scope=str((action.get("details") or {}).get("scope") or "unknown"),
                        status=str(action.get("status") or "failed"),
                        now_dt=tick_now_dt,
                        reason=action.get("failure_reason") or str(action.get("status") or "failed"),
                        max_failures_before_isolation=config.max_symbol_failures_before_isolation,
                        isolation_seconds=config.symbol_isolation_seconds,
                    )
                if action_id in {"fresh_option_quote_snapshot", "optional_index_option_quote_snapshot"} and action.get("status") == "succeeded":
                    quote_path = config.quote_json_path if action_id == "fresh_option_quote_snapshot" else config.optional_index_quote_json_path
                    action_symbols = _action_symbols(action)
                    quality = _quote_quality_report(
                        _read_json(quote_path),
                        expected_symbols=action_symbols,
                        min_symbol_quoted_contracts=config.min_symbol_quoted_contracts,
                        min_symbol_quoted_rate=config.min_symbol_quoted_rate,
                        max_spread_pct=config.max_spread_pct,
                    )
                    action["quote_quality_after_action"] = quality
                    for symbol in quality.get("problem_symbols") or []:
                        _mark_symbol_cooldown(
                            state,
                            action_id,
                            symbol,
                            now_dt=tick_now_dt,
                            seconds=config.low_quality_backoff_seconds,
                            reason="low_quote_snapshot_quality",
                        )
                    _update_symbol_status(
                        state,
                        action_id=action_id,
                        symbols=action_symbols,
                        scope=str((action.get("details") or {}).get("scope") or "unknown"),
                        status="succeeded",
                        now_dt=tick_now_dt,
                        quality=quality,
                        max_failures_before_isolation=config.max_symbol_failures_before_isolation,
                        isolation_seconds=config.symbol_isolation_seconds,
                    )
            elif action.get("status") == "planned":
                action["status"] = "planned_execute_actions_false"
            executed.append(action)
            if (
                config.execute_actions
                and config.option_structure_outcome_enabled
                and action.get("status") == "succeeded"
                and str(action.get("action_id") or "") in {"options_market_structure_rebuild", "optional_index_options_market_structure_rebuild"}
            ):
                is_optional_index = str(action.get("action_id") or "") == "optional_index_options_market_structure_rebuild"
                outcome_action_id = "m222_optional_index_option_structure_outcome_log" if is_optional_index else "m222_option_structure_outcome_log"
                outcome_action = _command_action(
                    outcome_action_id,
                    "Log option-structure features against later 5/15/30 minute and EOD market outcomes for M222 learning.",
                    _option_structure_outcome_command(
                        config,
                        structure_json_path=config.optional_index_structure_json_path if is_optional_index else config.structure_json_path,
                        outcome_json_path=config.optional_index_option_structure_outcome_json_path if is_optional_index else config.option_structure_outcome_json_path,
                        outcome_markdown_path=config.optional_index_option_structure_outcome_markdown_path if is_optional_index else config.option_structure_outcome_markdown_path,
                        symbols=config.optional_index_symbols if is_optional_index else config.symbols,
                    ),
                    priority=45 if is_optional_index else 25,
                    live_ibkr=str(config.option_structure_outcome_price_source).strip().lower() == "ibkr",
                    details={
                        "scope": "optional_index" if is_optional_index else "core_etf_proxy",
                        "triggered_by": action.get("action_id"),
                        "horizons": config.option_structure_outcome_horizons,
                        "price_source": config.option_structure_outcome_price_source,
                    },
                )
                outcome_action = _run_action(outcome_action, cwd=cwd, runner=runner, timeout_seconds=config.job_timeout_seconds)
                executed.append(outcome_action)
        session_refresh_policy = planned.get("session_refresh_policy") or {}
        effective_max_age = float(session_refresh_policy.get("max_snapshot_age_minutes") or config.max_snapshot_age_minutes)
        post_snapshot_status = _snapshot_status(
            config,
            tick_now_dt,
            expected_symbols=config.symbols,
            max_snapshot_age_minutes=effective_max_age,
        )
        post_optional_snapshot_status = _snapshot_status(
            config,
            tick_now_dt,
            quote_json_path=config.optional_index_quote_json_path,
            structure_json_path=config.optional_index_structure_json_path,
            expected_symbols=config.optional_index_symbols,
            max_snapshot_age_minutes=effective_max_age,
        ) if config.optional_index_symbols else {}
        artifact_delta = _artifact_delta(planned.get("snapshot_status") or {}, post_snapshot_status)
        optional_artifact_delta = _artifact_delta(planned.get("optional_index_snapshot_status") or {}, post_optional_snapshot_status) if config.optional_index_symbols else {}
        core_quote_payload = _read_json(config.quote_json_path)
        optional_quote_payload = _read_json(config.optional_index_quote_json_path)
        probe_payload = _read_json(config.index_option_capability_probe_json_path)
        quote_budgets = planned.get("quote_contract_budgets") or {}
        family_budget_allocation = _index_family_budget_allocation(
            config,
            str(session_refresh_policy.get("phase") or ""),
            int(quote_budgets.get("optional_index") or config.max_quote_contracts),
        )
        coverage_matrix = _index_option_coverage_matrix(
            config,
            core_snapshot_status=post_snapshot_status,
            optional_snapshot_status=post_optional_snapshot_status,
            symbol_status=state.get("symbol_status") or {},
            probe_payload=probe_payload,
            requested_symbols=_requested_symbols_from_snapshots(core_quote_payload, optional_quote_payload),
            quote_contract_budgets=quote_budgets,
            family_budget_allocation=family_budget_allocation,
        )
        if not bool((planned.get("session_gate") or {}).get("allowed")) and not bool(post_snapshot_status.get("quote_usable")):
            coverage_matrix = _defer_index_option_coverage_matrix(
                coverage_matrix,
                reason="option_quote_refresh_deferred_outside_regular_session",
            )
        state["index_option_capability_cache"] = {
            "version": coverage_matrix.get("version"),
            "updated_at": tick_now_dt.isoformat(),
            "overall_status": coverage_matrix.get("overall_status"),
            "coverage_status_counts": coverage_matrix.get("coverage_status_counts") or {},
            "confidence_counts": coverage_matrix.get("confidence_counts") or {},
            "families": [
                {
                    "family_id": row.get("family_id"),
                    "label": row.get("label"),
                    "coverage_status": row.get("coverage_status"),
                    "confidence": row.get("confidence"),
                    "selected_source_type": row.get("selected_source_type"),
                    "selected_symbols": row.get("selected_symbols") or [],
                    "fallback_reason": row.get("fallback_reason"),
                    "family_quote_budget": row.get("family_quote_budget"),
                    "direct_ok_symbols": ((row.get("direct") or {}).get("ok_symbols") or []) + ((row.get("mini") or {}).get("ok_symbols") or []),
                    "proxy_ok_symbols": (row.get("proxy") or {}).get("ok_symbols") or [],
                    "direct_failed_symbols": ((row.get("direct") or {}).get("failed_symbols") or []) + ((row.get("mini") or {}).get("failed_symbols") or []),
                    "proxy_failed_symbols": (row.get("proxy") or {}).get("failed_symbols") or [],
                    "proxy_probe_candidate_symbols": (row.get("proxy") or {}).get("probe_candidate_symbols") or [],
                }
                for row in coverage_matrix.get("families") or []
            ],
            "probe_summary": coverage_matrix.get("probe_summary") or {},
            "family_quote_budget_allocation": coverage_matrix.get("family_quote_budget_allocation") or {},
        }
        status_counts = dict(Counter(str(action.get("status") or "unknown") for action in executed))
        failed_actions = [action for action in executed if action.get("status") in {"failed", "timeout"}]
        m222_failed_actions = [
            action
            for action in failed_actions
            if str(action.get("action_id") or "").startswith("m222_")
        ]
        core_failed_actions = [
            action
            for action in failed_actions
            if not str(action.get("action_id") or "").startswith("optional_index_")
            and not str(action.get("action_id") or "").startswith("m222_")
        ]
        optional_failed_actions = [
            action
            for action in failed_actions
            if str(action.get("action_id") or "").startswith("optional_index_")
        ]
        status = "red" if core_failed_actions else "yellow" if optional_failed_actions or m222_failed_actions or any(action.get("status") in {"deferred_until_regular_session", "blocked_missing_quote_snapshot"} for action in executed) else "green"
        core_quality = (post_snapshot_status.get("quote_quality") or {})
        optional_quality = (post_optional_snapshot_status.get("quote_quality") or {})
        if status == "green" and core_quality.get("quality_status") in {"red", "missing"}:
            status = "yellow"
        tick = {
            "tick_index": tick_index,
            "checked_at": planned["checked_at"],
            "status": status,
            "execute_actions": bool(config.execute_actions),
            "session_gate": planned["session_gate"],
            "session_refresh_policy": session_refresh_policy,
            "next_regular_open": planned["next_regular_open"],
            "snapshot_status": post_snapshot_status,
            "optional_index_snapshot_status": post_optional_snapshot_status,
            "artifact_delta": artifact_delta,
            "optional_index_artifact_delta": optional_artifact_delta,
            "index_option_coverage_matrix": coverage_matrix,
            "quote_contract_budgets": planned.get("quote_contract_budgets") or {},
            "index_family_quote_budget_allocation": family_budget_allocation,
            "action_status_counts": status_counts,
            "core_failed_action_count": len(core_failed_actions),
            "optional_failed_action_count": len(optional_failed_actions),
            "m222_failed_action_count": len(m222_failed_actions),
            "m222_option_structure_outcome_actions": [action for action in executed if str(action.get("action_id") or "").startswith("m222_")],
            "failure_reason_counts": dict(Counter(str(action.get("failure_reason") or "unknown") for action in failed_actions)),
            "quote_quality_status": core_quality.get("quality_status"),
            "quote_quality_score": core_quality.get("quality_score"),
            "quote_data_latency_status": core_quality.get("data_latency_status"),
            "quote_delayed_contracts": core_quality.get("delayed_contracts"),
            "quote_delayed_symbols": core_quality.get("delayed_symbols") or [],
            "quote_quality_problem_symbols": core_quality.get("problem_symbols") or [],
            "quote_quality_reason_counts": core_quality.get("reason_counts") or {},
            "quote_quality_reason_summary": core_quality.get("reason_summary") or [],
            "quote_dte_bucket_counts": core_quality.get("dte_bucket_counts") or {},
            "optional_index_quote_quality_status": optional_quality.get("quality_status"),
            "optional_index_quote_quality_score": optional_quality.get("quality_score"),
            "optional_index_quote_data_latency_status": optional_quality.get("data_latency_status"),
            "optional_index_quote_delayed_contracts": optional_quality.get("delayed_contracts"),
            "optional_index_quote_delayed_symbols": optional_quality.get("delayed_symbols") or [],
            "optional_index_quote_quality_problem_symbols": optional_quality.get("problem_symbols") or [],
            "optional_index_quote_quality_reason_counts": optional_quality.get("reason_counts") or {},
            "optional_index_quote_quality_reason_summary": optional_quality.get("reason_summary") or [],
            "optional_index_quote_dte_bucket_counts": optional_quality.get("dte_bucket_counts") or {},
            "actions": executed,
        }
        tick["index_family_budget_tuning_observations"] = _index_family_budget_tuning_observations(coverage_matrix)
        tick["option_quote_deferred_operating_state"] = _option_quote_deferred_operating_state(tick)
        tick_summaries.append(tick)
        _append_jsonl(config.tick_log_jsonl_path, tick)
        _write_json(config.state_json_path, state) if config.state_json_path else None
        if live_json_output_path is not None:
            live_result = _supervisor_result_from_ticks(
                config=config,
                run_id=run_id,
                started_at=started_at,
                completed_at=_iso_now(),
                tick_summaries=tick_summaries[-20:],
                state=state,
            )
            write_options_market_structure_supervisor_json(live_json_output_path, live_result)
            if live_markdown_output_path is not None:
                write_options_market_structure_supervisor_markdown(live_markdown_output_path, live_result)
        if (continuous or tick_index < int(config.max_ticks)) and config.sleep_between_ticks:
            time.sleep(max(0.0, float(config.interval_seconds)))
    completed_at = _iso_now()
    _write_json(config.state_json_path, state) if config.state_json_path else None
    return _supervisor_result_from_ticks(
        config=config,
        run_id=run_id,
        started_at=started_at,
        completed_at=completed_at,
        tick_summaries=tick_summaries,
        state=state,
    )


def options_market_structure_supervisor_markdown(result: dict[str, Any]) -> str:
    lines = [
        "# Options Market Structure Refresh Supervisor",
        "",
        f"- Version: `{result.get('options_market_structure_supervisor_version')}`",
        f"- Run ID: `{result.get('run_id')}`",
        f"- Final status: `{result.get('final_status')}`",
        f"- Final session: `{result.get('final_session')}` phase `{result.get('final_session_refresh_phase')}` priority `{result.get('final_session_refresh_priority')}` quote allowed `{result.get('final_option_quote_allowed')}`",
        f"- Effective max snapshot age: `{result.get('final_effective_max_snapshot_age_minutes')}` minutes",
        f"- Final quote fresh/usable: `{result.get('final_quote_fresh')}` / `{result.get('final_quote_usable')}` structure status `{result.get('final_structure_status')}`",
        f"- Final quote quality: `{result.get('final_quote_quality_status')}` score `{result.get('final_quote_quality_score')}` latency `{result.get('final_quote_data_latency_status')}` delayed `{result.get('final_quote_delayed_contracts')}`",
        f"- Final quote quality reasons: `{result.get('final_quote_quality_reason_counts') or {}}`",
        f"- Optional index quote fresh/usable: `{result.get('final_optional_index_quote_fresh')}` / `{result.get('final_optional_index_quote_usable')}` structure status `{result.get('final_optional_index_structure_status')}`",
        f"- Optional index quote quality: `{result.get('final_optional_index_quote_quality_status')}` score `{result.get('final_optional_index_quote_quality_score')}` latency `{result.get('final_optional_index_quote_data_latency_status')}` delayed `{result.get('final_optional_index_quote_delayed_contracts')}`",
        f"- Optional index quote quality reasons: `{result.get('final_optional_index_quote_quality_reason_counts') or {}}`",
        f"- M222 option-structure outcome logging: actions `{len(result.get('final_m222_option_structure_outcome_actions') or [])}` failed `{result.get('final_m222_failed_action_count') or 0}`",
        f"- Core ETF symbols: `{(result.get('config') or {}).get('symbols') or []}` optional index symbols `{(result.get('config') or {}).get('optional_index_symbols') or []}`",
        f"- Isolated symbols by action: `{result.get('isolated_symbol_count') or 0}`",
        f"- Action status counts: `{result.get('final_action_status_counts') or {}}`",
        f"- Failure reason counts: `{result.get('final_failure_reason_counts') or {}}`",
        f"- Artifact delta: core `{(result.get('final_artifact_delta') or {}).get('outcome') or '-'}` optional index `{(result.get('final_optional_index_artifact_delta') or {}).get('outcome') or '-'}`",
        f"- Index option coverage: `{result.get('final_index_option_coverage_status') or '-'}` confidence `{result.get('final_index_option_confidence_counts') or {}}` budgets `{result.get('final_quote_contract_budgets') or {}}` family allocation `{result.get('index_family_quote_budget_allocation') or {}}`",
        f"- M220 issue categories: `{(result.get('m220_family_issue_summary') or {}).get('issue_category_counts') or {}}` proxy fallback `{(result.get('m220_family_issue_summary') or {}).get('proxy_fallback_families') or []}` probe-backed proxy `{(result.get('m220_family_issue_summary') or {}).get('proxy_probe_backed_families') or []}` unavailable `{(result.get('m220_family_issue_summary') or {}).get('coverage_unavailable_families') or []}`",
        f"- Deferred operating state: `{(result.get('option_quote_deferred_operating_state') or {}).get('status') or '-'}` alert policy `{(result.get('option_quote_deferred_operating_state') or {}).get('alert_policy') or '-'}` next open `{(result.get('option_quote_deferred_operating_state') or {}).get('next_regular_open') or '-'}`",
        "",
        "| Tick | Status | Session phase | Quote fresh/usable | Quality / latency | Delta | Problem symbols | Actions |",
        "| ---: | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for tick in result.get("tick_summaries") or []:
        lines.append(
            f"| {tick.get('tick_index')} | `{tick.get('status')}` | `{(tick.get('session_refresh_policy') or {}).get('phase') or (tick.get('session_gate') or {}).get('session')}` | "
            f"{(tick.get('snapshot_status') or {}).get('quote_fresh')} / {(tick.get('snapshot_status') or {}).get('quote_usable')} "
            f"({(tick.get('optional_index_snapshot_status') or {}).get('quote_fresh')} / {(tick.get('optional_index_snapshot_status') or {}).get('quote_usable')}) | "
            f"`{tick.get('quote_quality_status')}` `{tick.get('quote_data_latency_status')}` / `{tick.get('optional_index_quote_quality_status')}` `{tick.get('optional_index_quote_data_latency_status')}` | "
            f"`{(tick.get('artifact_delta') or {}).get('outcome')}` / `{(tick.get('optional_index_artifact_delta') or {}).get('outcome')}` | "
            f"`{tick.get('quote_quality_problem_symbols') or []}` / `{tick.get('optional_index_quote_quality_problem_symbols') or []}` | "
            f"`{tick.get('action_status_counts') or {}}` |"
        )
    if not result.get("tick_summaries"):
        lines.append("| - | - | - | - | - |")
    lines.extend(["", "## Latest actions", "", "| Action | Status | Failure reason | Live IBKR | Description |", "| --- | --- | --- | --- | --- |"])
    latest = (result.get("tick_summaries") or [{}])[-1]
    for action in latest.get("actions") or []:
        lines.append(
            f"| `{action.get('action_id')}` | `{action.get('status')}` | `{action.get('failure_reason') or '-'}` | "
            f"{action.get('live_ibkr')} | {action.get('description')} |"
        )
    if not latest.get("actions"):
        lines.append("| - | - | - | - | - |")
    coverage = result.get("index_option_coverage_matrix") or {}
    lines.extend(
        [
            "",
            "## Index option family coverage",
            "",
            f"- Overall status: `{coverage.get('overall_status') or '-'}`",
            f"- Resolver policy: `{coverage.get('resolver_policy') or {}}`",
            "",
            "| Family | Direct | Mini | Proxy | Selected | Budget | Coverage | Confidence | Note |",
            "| --- | --- | --- | --- | --- | ---: | --- | --- | --- |",
        ]
    )
    for family in coverage.get("families") or []:
        direct = family.get("direct") or {}
        mini = family.get("mini") or {}
        proxy = family.get("proxy") or {}
        lines.append(
            f"| `{family.get('label') or family.get('family_id')}` | "
            f"`{direct.get('status')}` {direct.get('ok_symbols') or direct.get('symbols') or []} | "
            f"`{mini.get('status')}` {mini.get('ok_symbols') or mini.get('symbols') or []} | "
            f"`{proxy.get('status')}` {proxy.get('ok_symbols') or proxy.get('symbols') or []} | "
            f"`{family.get('selected_source_type')}` {family.get('selected_symbols') or []} | "
            f"{family.get('family_quote_budget') if family.get('family_quote_budget') is not None else '-'} | "
            f"`{family.get('coverage_status')}` | `{family.get('confidence')}` | {family.get('fallback_reason') or family.get('non_blocking_note') or family.get('notes') or '-'} |"
        )
    if not coverage.get("families"):
        lines.append("| - | - | - | - | - | - | - | - | - |")
    repair_plans = result.get("m220_family_repair_plan") or []
    lines.extend(["", "## M220 family repair plan", "", "| Family | Mode | Severity | Selected | Issue categories | Next action | Summary |", "| --- | --- | --- | --- | --- | --- | --- |"])
    for plan in repair_plans:
        lines.append(
            f"| `{plan.get('label') or plan.get('family_id')}` | `{plan.get('operating_mode')}` | `{plan.get('severity')}` | "
            f"`{plan.get('selected_source_type')}` {plan.get('selected_symbols') or []} | "
            f"`{plan.get('issue_category_counts') or {}}` | `{plan.get('next_action')}` | {plan.get('operator_summary') or '-'} |"
        )
    if not repair_plans:
        lines.append("| - | - | - | - | - | - | - |")
    budget_rows = result.get("index_family_budget_tuning_observations") or []
    lines.extend(["", "## Family budget tuning observations", "", "| Family | Budget | Selected | Usable / requested | Candidates / chains | Not requested | Suggestion |", "| --- | ---: | --- | ---: | ---: | --- | --- |"])
    for row in budget_rows:
        lines.append(
            f"| `{row.get('label') or row.get('family_id')}` | {row.get('allocated_budget') if row.get('allocated_budget') is not None else '-'} | "
            f"`{row.get('selected_source_type')}` {row.get('selected_symbols') or []} | "
            f"{int(row.get('usable_role_symbol_count') or 0)} / {int(row.get('requested_role_symbol_count') or 0)} | "
            f"{int(row.get('candidate_contracts') or 0)} / {int(row.get('chain_rows') or 0)} | "
            f"`{row.get('not_requested_symbols') or []}` | `{row.get('suggested_budget_bias') or '-'}` |"
        )
    if not budget_rows:
        lines.append("| - | - | - | - | - | - | - |")
    runbook = result.get("m220_operational_runbook") or {}
    lines.extend(
        [
            "",
            "## M220 operational runbook",
            "",
            f"- One command: `{runbook.get('one_command') or '-'}`",
            f"- Safe to run now: `{runbook.get('safe_to_run_now')}` regular session required `{runbook.get('regular_session_required')}`",
            f"- Operator note: {runbook.get('operator_note') or '-'}",
            "",
            "| Step | Command / criterion |",
            "| --- | --- |",
        ]
    )
    for idx, command in enumerate(runbook.get("follow_up_commands") or [], start=1):
        lines.append(f"| follow-up {idx} | `{command}` |")
    for idx, criterion in enumerate(runbook.get("success_criteria") or [], start=1):
        lines.append(f"| success {idx} | {criterion} |")
    if not runbook:
        lines.append("| - | - |")
    checklist = result.get("m220_live_validation_checklist") or []
    lines.extend(["", "## M220 live validation checklist", "", "| Family | Role | Symbols | Passed | Status | Next action |", "| --- | --- | --- | --- | --- | --- |"])
    for row in checklist:
        lines.append(
            f"| `{row.get('family')}` | `{row.get('role')}` | `{row.get('symbols') or []}` | "
            f"`{row.get('passed')}` | `{row.get('status')}` | `{row.get('next_action')}` |"
        )
    if not checklist:
        lines.append("| - | - | - | - | - | - |")
    symbol_notes = result.get("symbol_failure_notes") or {}
    lines.extend(["", "## Symbol backoff / diagnostics", "", "| Symbol | Action | Reason | Next eligible |", "| --- | --- | --- | --- |"])
    for note in symbol_notes.values():
        lines.append(
            f"| `{note.get('symbol')}` | `{note.get('action_id')}` | `{note.get('reason')}` | `{note.get('next_eligible_at')}` |"
        )
    if not symbol_notes:
        lines.append("| - | - | - | - |")
    isolated = result.get("isolated_symbols_by_action") or {}
    lines.extend(["", "## Isolated symbols", "", "| Symbol | Action | Reason | Streak | Isolated until |", "| --- | --- | --- | ---: | --- |"])
    for row in isolated.values():
        lines.append(
            f"| `{row.get('symbol')}` | `{row.get('action_id')}` | `{row.get('reason')}` | "
            f"{int(row.get('failure_streak') or 0)} | `{row.get('isolated_until')}` |"
        )
    if not isolated:
        lines.append("| - | - | - | - | - |")
    symbol_status = result.get("symbol_status") or {}
    lines.extend(["", "## Symbol status history", "", "| Symbol | Scope | Last status | Streak | Last success | Last failure | Reason | Quality / latency | Cooldown / isolation until |", "| --- | --- | --- | ---: | --- | --- | --- | --- | --- |"])
    for symbol, row in sorted(symbol_status.items()):
        lines.append(
            f"| `{symbol}` | `{row.get('scope') or '-'}` | `{row.get('last_status') or '-'}` | "
            f"{int(row.get('failure_streak') or 0)} | "
            f"`{row.get('last_success_at') or '-'}` | `{row.get('last_failure_at') or '-'}` | "
            f"`{row.get('last_failure_reason') or row.get('last_reason') or '-'}` | `{row.get('last_quality_status') or '-'}` / `{row.get('last_data_latency_status') or '-'}` | "
            f"`{row.get('cooldown_until') or '-'}` / `{row.get('isolation_until') or '-'}` |"
        )
    if not symbol_status:
        lines.append("| - | - | - | - | - | - | - | - | - |")
    lines.extend(
        [
            "",
            "## Safety",
            "",
            "- Read-only option quote snapshot and market-structure rebuild supervisor.",
            "- It defers IBKR option bid/ask refresh outside the configured regular option session.",
            "- It does not create option tickets, submit IBKR orders, enable transmit, route live orders, mutate scorer rows, or mutate the real portfolio.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_options_market_structure_supervisor_json(output_path: Path, result: dict[str, Any]) -> Path:
    return _write_json(output_path, result)


def write_options_market_structure_supervisor_markdown(output_path: Path, result: dict[str, Any]) -> Path:
    resolved = output_path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(options_market_structure_supervisor_markdown(result), encoding="utf-8")
    return resolved
