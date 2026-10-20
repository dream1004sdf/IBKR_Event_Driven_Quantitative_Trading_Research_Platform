"""Operating defensive planning, evidence, retry, and intent-ledger helpers.

Grouped at a middle level so defensive safety workflows are found in one place
instead of four small files.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


# --- merged from defensive_action_planner.py ---
DEFENSIVE_ACTION_PLAN_VERSION = "operating-defensive-action-plan-v1"


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


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


def _result(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    result = payload.get("result")
    return result if isinstance(result, dict) else payload


def _float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return float(default)
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _operating_mode(operating_gate: dict[str, Any]) -> str:
    raw = operating_gate.get("market_data_operating_mode")
    if isinstance(raw, dict):
        return str(raw.get("mode") or "")
    defensive = operating_gate.get("market_data_defensive_mode") or {}
    return str(raw or defensive.get("mode") or "")


def _operating_controls(operating_gate: dict[str, Any]) -> dict[str, Any]:
    defensive = operating_gate.get("market_data_defensive_mode") or {}
    return defensive.get("controls") or {}


def _severity(operating_gate: dict[str, Any]) -> str:
    defensive = operating_gate.get("market_data_defensive_mode") or {}
    raw = operating_gate.get("market_data_operating_mode")
    if isinstance(raw, dict):
        return str(raw.get("severity") or defensive.get("severity") or "")
    return str(defensive.get("severity") or "")


def _portfolio_rows(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    portfolio = _result(payload)
    if not portfolio:
        return []
    rows = portfolio.get("portfolio_rows")
    if isinstance(rows, list):
        return [row for row in rows if isinstance(row, dict)]
    positions = portfolio.get("positions")
    if isinstance(positions, dict):
        rows = positions.get("paper_positions")
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    if isinstance(positions, list):
        return [row for row in positions if isinstance(row, dict)]
    return []


def _protective_candidates(operating_gate: dict[str, Any]) -> list[dict[str, Any]]:
    packet = operating_gate.get("protective_analysis_packet") or {}
    rows = packet.get("candidates") or packet.get("sample_candidates") or []
    return [row for row in rows if isinstance(row, dict)]


def _allowed_structures(operating_gate: dict[str, Any]) -> list[str]:
    packet = operating_gate.get("protective_analysis_packet") or {}
    controls = _operating_controls(operating_gate)
    raw = packet.get("allowed_option_structures") or controls.get("allowed_option_structures") or []
    return [str(item) for item in raw if item]


def _structure_enabled(allowed: list[str], name: str) -> bool:
    if not allowed:
        return False
    name = name.lower()
    return any(name in item.lower() for item in allowed)


def _position_side(row: dict[str, Any]) -> str:
    net = _float(row.get("open_net_quantity"))
    if net > 0:
        return "long"
    if net < 0:
        return "short"
    return "flat"


def _position_notional(row: dict[str, Any]) -> float:
    value = row.get("open_market_value")
    if value is not None:
        return abs(_float(value))
    return abs(_float(row.get("open_net_quantity")) * _float(row.get("latest_mark_price")))


def _priority_base(mode: str, severity: str) -> int:
    mode = mode.lower()
    severity = severity.lower()
    score = 40
    if mode in {"risk_reduction_only", "critical", "blocked"}:
        score += 35
    elif mode in {"defensive", "hedge_only"}:
        score += 25
    elif mode in {"watch", "monitor"}:
        score += 10
    if severity in {"critical", "red", "stress"}:
        score += 20
    elif severity in {"warning", "yellow", "elevated"}:
        score += 10
    return score


def _action_row(
    *,
    symbol: str,
    action_type: str,
    strategy: str,
    reason: str,
    priority_score: int,
    source: str,
    position: dict[str, Any] | None = None,
    candidate: dict[str, Any] | None = None,
    ticketing_allowed: bool = False,
    estimated_cost: float | None = None,
    estimated_notional: float | None = None,
) -> dict[str, Any]:
    row = {
        "symbol": str(symbol or "UNKNOWN").upper(),
        "action_type": action_type,
        "strategy": strategy,
        "reason": reason,
        "priority_score": int(priority_score),
        "source": source,
        "ticketing_allowed": bool(ticketing_allowed),
        "requires_option_chain": strategy
        in {"protective_put", "collar", "put_spread_hedge", "covered_call_risk_reduction"},
        "requires_live_option_quotes": strategy
        in {"protective_put", "collar", "put_spread_hedge", "covered_call_risk_reduction"},
        "requires_margin_whatif": action_type in {"exit_or_reduce_position", "protective_option_analysis"},
        "estimated_cost": estimated_cost,
        "estimated_notional": estimated_notional,
        "source_position": position or {},
        "source_protective_candidate": candidate or {},
        "safety": {
            "analysis_only": not bool(ticketing_allowed),
            "ticket_created": False,
            "ibkr_order_submitted": False,
            "transmit_enabled": False,
            "live_order_routing_enabled": False,
            "portfolio_mutated": False,
        },
    }
    return row


def build_operating_defensive_action_plan(
    operating_auto_gate_payload: dict[str, Any],
    *,
    paper_portfolio_state_payload: dict[str, Any] | None = None,
    account_positions_payload: dict[str, Any] | None = None,
    max_candidates: int = 25,
    max_actions_per_symbol: int = 4,
    hedge_notional_fraction: float = 0.50,
    now: str | None = None,
) -> dict[str, Any]:
    operating_gate = _result(operating_auto_gate_payload)
    paper_rows = _portfolio_rows(paper_portfolio_state_payload)
    account_rows = _portfolio_rows(account_positions_payload)
    rows_by_symbol: dict[str, dict[str, Any]] = {}
    for row in [*paper_rows, *account_rows]:
        symbol = str(row.get("symbol") or "").upper()
        if symbol and symbol not in rows_by_symbol:
            rows_by_symbol[symbol] = row

    controls = _operating_controls(operating_gate)
    mode = _operating_mode(operating_gate)
    severity = _severity(operating_gate)
    allowed = _allowed_structures(operating_gate)
    packet = operating_gate.get("protective_analysis_packet") or {}
    ticketing_allowed = bool(packet.get("ticketing_allowed") and controls.get("protective_option_ticketing_allowed"))
    exit_allowed = bool(controls.get("exit_or_risk_reduction_allowed", True))
    hedge_allowed = bool(controls.get("hedge_or_defensive_entries_allowed", False))
    protective_analysis_allowed = bool(controls.get("protective_option_analysis_allowed", False) or packet)
    base_priority = _priority_base(mode, severity)

    action_rows: list[dict[str, Any]] = []
    per_symbol_counts: Counter[str] = Counter()

    for symbol, position in sorted(rows_by_symbol.items()):
        if position.get("position_status") not in {None, "", "open"}:
            continue
        side = _position_side(position)
        if side == "flat":
            continue
        notional = _position_notional(position)
        pnl = _float(position.get("total_pnl") if position.get("total_pnl") is not None else position.get("unrealized_pnl"))
        loss_boost = 15 if pnl < 0 else 0
        large_boost = min(15, int(notional // 1000))
        if exit_allowed and per_symbol_counts[symbol] < max_actions_per_symbol:
            strategy = "buy_to_cover_or_reduce_short" if side == "short" else "sell_or_reduce_long"
            action_rows.append(
                _action_row(
                    symbol=symbol,
                    action_type="exit_or_reduce_position",
                    strategy=strategy,
                    reason=f"{mode or 'operating_mode'} allows risk reduction while ordinary alpha may be blocked",
                    priority_score=base_priority + 30 + loss_boost + large_boost,
                    source="portfolio_state",
                    position=position,
                    ticketing_allowed=False,
                    estimated_notional=notional,
                )
            )
            per_symbol_counts[symbol] += 1
        if side == "long" and hedge_allowed and protective_analysis_allowed:
            hedge_notional = round(notional * float(hedge_notional_fraction), 6)
            for structure, boost in (
                ("protective_put", 24),
                ("collar", 20),
                ("put_spread_hedge", 18),
                ("covered_call_risk_reduction", 12),
            ):
                if per_symbol_counts[symbol] >= max_actions_per_symbol:
                    break
                if not _structure_enabled(allowed, structure):
                    continue
                action_rows.append(
                    _action_row(
                        symbol=symbol,
                        action_type="protective_option_analysis",
                        strategy=structure,
                        reason=f"{structure} is allowed by operating protective-analysis structures",
                        priority_score=base_priority + boost + loss_boost + large_boost,
                        source="portfolio_state+operating_gate",
                        position=position,
                        ticketing_allowed=ticketing_allowed,
                        estimated_notional=hedge_notional,
                    )
                )
                per_symbol_counts[symbol] += 1

    for candidate in _protective_candidates(operating_gate):
        symbol = str(candidate.get("symbol") or "").upper()
        if not symbol or per_symbol_counts[symbol] >= max_actions_per_symbol:
            continue
        position = rows_by_symbol.get(symbol)
        if position:
            continue
        for structure in candidate.get("allowed_option_structures") or allowed or ["protective_analysis_watchlist"]:
            if per_symbol_counts[symbol] >= max_actions_per_symbol:
                break
            strategy = str(structure).replace("_analysis_only", "")
            action_rows.append(
                _action_row(
                    symbol=symbol,
                    action_type="watchlist_defensive_analysis",
                    strategy=strategy,
                    reason="blocked alpha candidate retained as analysis-only defensive watchlist item",
                    priority_score=base_priority + 5,
                    source="protective_analysis_packet",
                    candidate=candidate,
                    ticketing_allowed=False,
                )
            )
            per_symbol_counts[symbol] += 1

    action_rows.sort(key=lambda row: (-int(row.get("priority_score") or 0), row.get("symbol") or "", row.get("strategy") or ""))
    action_rows = action_rows[: max(0, int(max_candidates))]
    action_counts = Counter(str(row.get("action_type") or "unknown") for row in action_rows)
    strategy_counts = Counter(str(row.get("strategy") or "unknown") for row in action_rows)
    symbol_counts = Counter(str(row.get("symbol") or "UNKNOWN") for row in action_rows)
    result = {
        "operating_defensive_action_plan_version": DEFENSIVE_ACTION_PLAN_VERSION,
        "generated_at": now or _iso_now(),
        "operating_auto_gate_version": operating_gate.get("autonomous_execution_gate_version"),
        "operating_mode": mode,
        "operating_severity": severity,
        "ordinary_alpha_entries_allowed": controls.get("ordinary_alpha_entries_allowed"),
        "hedge_or_defensive_entries_allowed": controls.get("hedge_or_defensive_entries_allowed"),
        "exit_or_risk_reduction_allowed": controls.get("exit_or_risk_reduction_allowed"),
        "protective_option_analysis_allowed": controls.get("protective_option_analysis_allowed"),
        "protective_option_ticketing_allowed": controls.get("protective_option_ticketing_allowed"),
        "effective_ticketing_allowed": ticketing_allowed,
        "allowed_option_structures": allowed,
        "source_protective_candidate_count": packet.get("candidate_count"),
        "source_open_position_count": len(rows_by_symbol),
        "action_count": len(action_rows),
        "action_type_counts": dict(action_counts),
        "strategy_counts": dict(strategy_counts),
        "symbol_counts": dict(symbol_counts),
        "top_actions": action_rows,
        "summary": {
            "recommended_posture": "risk_reduction_and_defensive_analysis" if action_rows else "no_defensive_action_candidates",
            "highest_priority_score": max((int(row.get("priority_score") or 0) for row in action_rows), default=0),
            "requires_option_chain_count": sum(1 for row in action_rows if row.get("requires_option_chain")),
            "requires_margin_whatif_count": sum(1 for row in action_rows if row.get("requires_margin_whatif")),
            "analysis_only_count": sum(1 for row in action_rows if (row.get("safety") or {}).get("analysis_only")),
        },
        "safety": {
            "mode": "paper_operating_defensive_action_plan",
            "policy": "Builds paper-only defensive action recommendations from M182 operating state, protective-analysis packet, and portfolio state. No tickets, broker orders, transmit, scorer mutation, or real portfolio mutation.",
            "ticket_created": False,
            "ibkr_order_submitted": False,
            "transmit_enabled": False,
            "live_order_routing_enabled": False,
            "portfolio_mutated": False,
        },
    }
    return result


def build_operating_defensive_action_plan_from_paths(
    *,
    operating_auto_gate_path: Path,
    paper_portfolio_state_path: Path | None = None,
    account_positions_path: Path | None = None,
    max_candidates: int = 25,
    max_actions_per_symbol: int = 4,
    hedge_notional_fraction: float = 0.50,
    now: str | None = None,
) -> dict[str, Any]:
    return build_operating_defensive_action_plan(
        _read_json(operating_auto_gate_path),
        paper_portfolio_state_payload=_read_json(paper_portfolio_state_path),
        account_positions_payload=_read_json(account_positions_path),
        max_candidates=max_candidates,
        max_actions_per_symbol=max_actions_per_symbol,
        hedge_notional_fraction=hedge_notional_fraction,
        now=now,
    )


def operating_defensive_action_plan_markdown(
    report: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
    title: str = "Operating Defensive Action Plan",
) -> str:
    filters = filters or {}
    summary = report.get("summary") or {}
    lines = [
        f"# {title}",
        "",
        f"- Version: `{report.get('operating_defensive_action_plan_version')}`",
        f"- Generated at: `{report.get('generated_at')}`",
        f"- Operating mode: `{report.get('operating_mode') or '-'}` severity `{report.get('operating_severity') or '-'}`",
        f"- Alpha / hedge / exit allowed: `{report.get('ordinary_alpha_entries_allowed')}` / `{report.get('hedge_or_defensive_entries_allowed')}` / `{report.get('exit_or_risk_reduction_allowed')}`",
        f"- Protective analysis / ticketing: `{report.get('protective_option_analysis_allowed')}` / `{report.get('effective_ticketing_allowed')}`",
        f"- Source protective candidates: `{report.get('source_protective_candidate_count')}`",
        f"- Source open positions: `{report.get('source_open_position_count')}`",
        f"- Action candidates: `{report.get('action_count')}`",
        f"- Recommended posture: `{summary.get('recommended_posture')}`",
        f"- Requires option chain / margin what-if: `{summary.get('requires_option_chain_count')}` / `{summary.get('requires_margin_whatif_count')}`",
        "- Safety: paper-only defensive plan; no tickets, broker order API call, transmit, scorer mutation, or real portfolio mutation.",
        "",
        "## Inputs",
        "",
    ]
    if filters:
        for key, value in filters.items():
            lines.append(f"- `{key}`: `{value if value is not None else '-'}`")
    else:
        lines.append("- none")
    for title_text, counts in (
        ("Action type counts", report.get("action_type_counts") or {}),
        ("Strategy counts", report.get("strategy_counts") or {}),
        ("Symbol counts", report.get("symbol_counts") or {}),
    ):
        lines.extend(["", f"## {title_text}", "", "| Bucket | Count |", "| --- | ---: |"])
        if counts:
            for key, value in sorted(counts.items(), key=lambda item: (-int(item[1]), item[0])):
                lines.append(f"| `{key}` | {value} |")
        else:
            lines.append("| `none` | 0 |")
    lines.extend(
        [
            "",
            "## Top defensive actions",
            "",
            "| Priority | Symbol | Action | Strategy | Ticketing | Needs chain | Needs what-if | Reason |",
            "| ---: | --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for row in report.get("top_actions") or []:
        lines.append(
            f"| {row.get('priority_score')} | `{row.get('symbol')}` | `{row.get('action_type')}` | `{row.get('strategy')}` | {row.get('ticketing_allowed')} | {row.get('requires_option_chain')} | {row.get('requires_margin_whatif')} | {row.get('reason')} |"
        )
    if not report.get("top_actions"):
        lines.append("| - | - | - | - | - | - | - | - |")
    lines.extend(
        [
            "",
            "## Interpretation note",
            "",
            "M186 converts the M182/M185 operating state into defensive, paper-only recommendations. It is intentionally one step before intent ledgering: option-chain lookup, quote gates, margin what-if, and any future ticket creation stay downstream.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_operating_defensive_action_plan_json(output_path: Path, report: dict[str, Any]) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return output_path


def write_operating_defensive_action_plan_markdown(
    output_path: Path,
    report: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(operating_defensive_action_plan_markdown(report, filters=filters), encoding="utf-8")
    return output_path


# --- merged from defensive_intent_ledger.py ---
DEFENSIVE_INTENT_LEDGER_VERSION = "operating-defensive-intent-ledger-v1"


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


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


def _result(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    result = payload.get("result")
    return result if isinstance(result, dict) else payload


def _float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return float(default)
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _safe_symbol(value: Any) -> str:
    symbol = str(value or "").strip().upper()
    return symbol or "UNKNOWN"


def _stable_intent_id(
    *,
    ledger_run_id: str,
    symbol: str,
    action_type: str,
    strategy: str,
    index: int,
) -> str:
    safe_parts = [
        str(ledger_run_id or "m187"),
        _safe_symbol(symbol),
        str(action_type or "action"),
        str(strategy or "strategy"),
        str(index),
    ]
    return "defensive-intent-" + "-".join(part.replace(" ", "_").lower() for part in safe_parts)


def _existing_intent_ids(existing_ledger_payload: dict[str, Any] | None) -> set[str]:
    data = _result(existing_ledger_payload)
    ids: set[str] = set()
    for key in ("intents", "defensive_intents", "ledger_rows", "records"):
        rows = data.get(key)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            intent_id = row.get("intent_id") or row.get("defensive_intent_id") or row.get("id")
            if intent_id:
                ids.add(str(intent_id))
    return ids


def _whatif_symbols(payload: dict[str, Any] | None) -> set[str]:
    data = _result(payload)
    symbols: set[str] = set()
    rows: list[Any] = []
    for key in ("what_if_results", "responses", "order_previews", "previews", "rows", "items"):
        raw = data.get(key)
        if isinstance(raw, list):
            rows.extend(raw)
    if not rows and data.get("symbol"):
        rows = [data]
    for row in rows:
        if not isinstance(row, dict):
            continue
        status = str(row.get("status") or row.get("preview_status") or row.get("whatif_status") or "ok").lower()
        if status in {"blocked", "error", "failed", "missing"}:
            continue
        symbol = row.get("symbol") or row.get("underlying_symbol") or row.get("localSymbol")
        if not symbol and isinstance(row.get("contract"), dict):
            symbol = (row.get("contract") or {}).get("symbol")
        if symbol:
            symbols.add(_safe_symbol(symbol))
    return symbols


def _option_quote_symbols(payload: dict[str, Any] | None) -> set[str]:
    data = _result(payload)
    symbols: set[str] = set()
    rows: list[Any] = []
    for key in ("option_quote_results", "quotes", "option_quotes", "quote_rows", "gated_candidates", "rows", "items"):
        raw = data.get(key)
        if isinstance(raw, list):
            rows.extend(raw)
    if not rows and (data.get("underlying_symbol") or data.get("symbol")):
        rows = [data]
    for row in rows:
        if not isinstance(row, dict):
            continue
        status = str(row.get("status") or row.get("quote_status") or row.get("gate_status") or "ok").lower()
        if status in {"blocked", "error", "failed", "missing", "no_quote"}:
            continue
        symbol = row.get("underlying_symbol") or row.get("symbol")
        if symbol:
            symbols.add(_safe_symbol(symbol))
    return symbols


def _exit_quantity(position: dict[str, Any], strategy: str) -> float:
    if strategy == "buy_to_cover_or_reduce_short":
        quantity = abs(_float(position.get("open_short_quantity")))
        return quantity or abs(_float(position.get("open_net_quantity")))
    quantity = abs(_float(position.get("open_long_quantity")))
    return quantity or abs(_float(position.get("open_net_quantity")))


def _order_action(strategy: str) -> str:
    if strategy == "buy_to_cover_or_reduce_short":
        return "BUY"
    if strategy == "sell_or_reduce_long":
        return "SELL"
    return "ANALYZE"


def _intent_payload(row: dict[str, Any], *, intent_id: str, quantity: float) -> dict[str, Any]:
    strategy = str(row.get("strategy") or "")
    symbol = _safe_symbol(row.get("symbol"))
    position = row.get("source_position") if isinstance(row.get("source_position"), dict) else {}
    action = _order_action(strategy)
    return {
        "intent_id": intent_id,
        "intent_type": "defensive_risk_reduction",
        "symbol": symbol,
        "asset_class": "stock",
        "action": action,
        "quantity": quantity,
        "quantity_source": "m186_source_position",
        "strategy": strategy,
        "source_action_type": row.get("action_type"),
        "source_priority_score": row.get("priority_score"),
        "source_reason": row.get("reason"),
        "estimated_notional": row.get("estimated_notional"),
        "reference_mark_price": position.get("latest_mark_price"),
        "reference_mark_source": position.get("latest_mark_source"),
        "reference_mark_generated_at": position.get("latest_mark_generated_at"),
        "order_preview": {
            "order_type": "MKT",
            "time_in_force": "DAY",
            "what_if": True,
            "transmit": False,
            "live_order_routing_enabled": False,
        },
    }


def build_operating_defensive_intent_ledger(
    defensive_action_plan_payload: dict[str, Any],
    *,
    existing_ledger_payload: dict[str, Any] | None = None,
    option_quote_gate_payload: dict[str, Any] | None = None,
    broker_whatif_payload: dict[str, Any] | None = None,
    allow_missing_whatif: bool = False,
    allow_missing_option_quotes: bool = False,
    max_intents: int = 10,
    ledger_run_id: str | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    plan = _result(defensive_action_plan_payload)
    actions = [row for row in plan.get("top_actions") or [] if isinstance(row, dict)]
    run_id = ledger_run_id or f"m187-{(now or _iso_now()).replace(':', '').replace('+', 'z')}"
    existing_ids = _existing_intent_ids(existing_ledger_payload)
    whatif_symbols = _whatif_symbols(broker_whatif_payload)
    option_quote_symbols = _option_quote_symbols(option_quote_gate_payload)

    rows: list[dict[str, Any]] = []
    pending_count = 0
    for index, row in enumerate(actions, start=1):
        symbol = _safe_symbol(row.get("symbol"))
        action_type = str(row.get("action_type") or "")
        strategy = str(row.get("strategy") or "")
        block_reasons: list[str] = []
        readiness = {
            "source_plan_present": True,
            "is_exit_or_reduce_position": action_type == "exit_or_reduce_position",
            "requires_option_chain": bool(row.get("requires_option_chain")),
            "requires_live_option_quotes": bool(row.get("requires_live_option_quotes")),
            "requires_margin_whatif": bool(row.get("requires_margin_whatif")),
            "broker_whatif_available": symbol in whatif_symbols,
            "option_quote_gate_available": symbol in option_quote_symbols,
            "allow_missing_whatif": bool(allow_missing_whatif),
            "allow_missing_option_quotes": bool(allow_missing_option_quotes),
        }
        if action_type != "exit_or_reduce_position":
            block_reasons.append("analysis_only_not_a_risk_reduction_order")
        if row.get("requires_margin_whatif") and symbol not in whatif_symbols and not allow_missing_whatif:
            block_reasons.append("missing_broker_margin_whatif")
        if row.get("requires_live_option_quotes") and symbol not in option_quote_symbols and not allow_missing_option_quotes:
            block_reasons.append("missing_live_option_quote_gate")
        if row.get("requires_option_chain") and symbol not in option_quote_symbols and not allow_missing_option_quotes:
            block_reasons.append("missing_option_chain_gate")
        position = row.get("source_position") if isinstance(row.get("source_position"), dict) else {}
        quantity = _exit_quantity(position, strategy)
        if action_type == "exit_or_reduce_position" and quantity <= 0:
            block_reasons.append("missing_exit_quantity")

        intent_id = _stable_intent_id(
            ledger_run_id=run_id,
            symbol=symbol,
            action_type=action_type,
            strategy=strategy,
            index=index,
        )
        if intent_id in existing_ids:
            block_reasons.append("duplicate_existing_intent")

        can_create = not block_reasons and pending_count < max(0, int(max_intents))
        if not can_create and not block_reasons and pending_count >= max(0, int(max_intents)):
            block_reasons.append("max_intents_reached")

        state = "pending_defensive_paper_execution" if can_create else "blocked"
        if can_create:
            pending_count += 1

        ledger_row = {
            "defensive_intent_id": intent_id,
            "symbol": symbol,
            "state": state,
            "action_type": action_type,
            "strategy": strategy,
            "priority_score": row.get("priority_score"),
            "block_reasons": block_reasons,
            "readiness_checks": readiness,
            "source_action": row,
            "paper_intent": _intent_payload(row, intent_id=intent_id, quantity=quantity) if can_create else None,
            "safety": {
                "paper_only": True,
                "ticket_created": False,
                "ibkr_order_submitted": False,
                "transmit_enabled": False,
                "live_order_routing_enabled": False,
                "portfolio_mutated": False,
            },
        }
        rows.append(ledger_row)

    state_counts = Counter(str(row.get("state") or "unknown") for row in rows)
    strategy_counts = Counter(str(row.get("strategy") or "unknown") for row in rows)
    symbol_counts = Counter(str(row.get("symbol") or "UNKNOWN") for row in rows)
    pending_rows = [row for row in rows if row.get("state") == "pending_defensive_paper_execution"]
    blocked_rows = [row for row in rows if row.get("state") != "pending_defensive_paper_execution"]
    result = {
        "operating_defensive_intent_ledger_version": DEFENSIVE_INTENT_LEDGER_VERSION,
        "generated_at": now or _iso_now(),
        "ledger_run_id": run_id,
        "source_action_plan_version": plan.get("operating_defensive_action_plan_version"),
        "source_generated_at": plan.get("generated_at"),
        "candidate_count": len(rows),
        "pending_defensive_paper_execution_count": len(pending_rows),
        "blocked_count": len(blocked_rows),
        "state_counts": dict(state_counts),
        "strategy_counts": dict(strategy_counts),
        "symbol_counts": dict(symbol_counts),
        "pending_intents": [row["paper_intent"] for row in pending_rows if row.get("paper_intent")],
        "defensive_intents": rows,
        "summary": {
            "recommended_posture": "paper_intents_ready_for_preview" if pending_rows else "no_defensive_paper_intents_ready",
            "strict_gate_mode": not bool(allow_missing_whatif or allow_missing_option_quotes),
            "missing_whatif_block_count": sum(
                1 for row in rows if "missing_broker_margin_whatif" in (row.get("block_reasons") or [])
            ),
            "missing_option_quote_block_count": sum(
                1
                for row in rows
                if "missing_live_option_quote_gate" in (row.get("block_reasons") or [])
                or "missing_option_chain_gate" in (row.get("block_reasons") or [])
            ),
            "analysis_only_block_count": sum(
                1 for row in rows if "analysis_only_not_a_risk_reduction_order" in (row.get("block_reasons") or [])
            ),
        },
        "safety": {
            "mode": "paper_defensive_intent_ledger",
            "policy": "Converts M186 defensive recommendations into paper-only defensive intent candidates after quote and broker what-if gates. It never creates tickets, submits IBKR orders, enables transmit, or mutates portfolio state.",
            "ticket_created": False,
            "ibkr_order_submitted": False,
            "transmit_enabled": False,
            "live_order_routing_enabled": False,
            "portfolio_mutated": False,
        },
    }
    return result


def build_operating_defensive_intent_ledger_from_paths(
    *,
    defensive_action_plan_path: Path,
    existing_ledger_path: Path | None = None,
    option_quote_gate_path: Path | None = None,
    broker_whatif_path: Path | None = None,
    allow_missing_whatif: bool = False,
    allow_missing_option_quotes: bool = False,
    max_intents: int = 10,
    ledger_run_id: str | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    return build_operating_defensive_intent_ledger(
        _read_json(defensive_action_plan_path),
        existing_ledger_payload=_read_json(existing_ledger_path),
        option_quote_gate_payload=_read_json(option_quote_gate_path),
        broker_whatif_payload=_read_json(broker_whatif_path),
        allow_missing_whatif=allow_missing_whatif,
        allow_missing_option_quotes=allow_missing_option_quotes,
        max_intents=max_intents,
        ledger_run_id=ledger_run_id,
        now=now,
    )


def operating_defensive_intent_ledger_markdown(
    report: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
    sample_limit: int = 20,
    title: str = "Operating Defensive Intent Ledger",
) -> str:
    filters = filters or {}
    summary = report.get("summary") or {}
    lines = [
        f"# {title}",
        "",
        f"- Version: `{report.get('operating_defensive_intent_ledger_version')}`",
        f"- Generated at: `{report.get('generated_at')}`",
        f"- Ledger run ID: `{report.get('ledger_run_id')}`",
        f"- Source plan: `{report.get('source_action_plan_version')}` generated `{report.get('source_generated_at')}`",
        f"- Candidates / pending / blocked: `{report.get('candidate_count')}` / `{report.get('pending_defensive_paper_execution_count')}` / `{report.get('blocked_count')}`",
        f"- Recommended posture: `{summary.get('recommended_posture')}`",
        f"- Strict gate mode: `{summary.get('strict_gate_mode')}`",
        f"- Missing what-if / option quote blocks: `{summary.get('missing_whatif_block_count')}` / `{summary.get('missing_option_quote_block_count')}`",
        "- Safety: paper-only ledger; no tickets, broker order API call, transmit, or portfolio mutation.",
        "",
        "## Inputs",
        "",
    ]
    if filters:
        for key, value in filters.items():
            lines.append(f"- `{key}`: `{value if value is not None else '-'}`")
    else:
        lines.append("- none")
    for title_text, counts in (
        ("State counts", report.get("state_counts") or {}),
        ("Strategy counts", report.get("strategy_counts") or {}),
        ("Symbol counts", report.get("symbol_counts") or {}),
    ):
        lines.extend(["", f"## {title_text}", "", "| Bucket | Count |", "| --- | ---: |"])
        if counts:
            for key, value in sorted(counts.items(), key=lambda item: (-int(item[1]), item[0])):
                lines.append(f"| `{key}` | {value} |")
        else:
            lines.append("| `none` | 0 |")
    lines.extend(
        [
            "",
            "## Defensive intent rows",
            "",
            "| State | Symbol | Strategy | Priority | Blocks | Paper action | Qty |",
            "| --- | --- | --- | ---: | --- | --- | ---: |",
        ]
    )
    rows = report.get("defensive_intents") or []
    for row in rows[: max(0, int(sample_limit))]:
        paper = row.get("paper_intent") or {}
        blocks = ", ".join(row.get("block_reasons") or []) or "-"
        lines.append(
            f"| `{row.get('state')}` | `{row.get('symbol')}` | `{row.get('strategy')}` | {row.get('priority_score')} | {blocks} | `{paper.get('action') or '-'}` | {paper.get('quantity') if paper else '-'} |"
        )
    if not rows:
        lines.append("| - | - | - | - | - | - | - |")
    lines.extend(
        [
            "",
            "## Interpretation note",
            "",
            "M187 is the paper intent narrowing layer after M186. Strict mode requires broker margin what-if and live option quote/chain evidence when the source action says they are required. `--allow-missing-whatif` is intentionally explicit and should be used only for dry paper rehearsal.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_operating_defensive_intent_ledger_json(output_path: Path, report: dict[str, Any]) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return output_path


def write_operating_defensive_intent_ledger_markdown(
    output_path: Path,
    report: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
    sample_limit: int = 20,
) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        operating_defensive_intent_ledger_markdown(report, filters=filters, sample_limit=sample_limit),
        encoding="utf-8",
    )
    return output_path


# --- merged from defensive_gate_evidence.py ---
DEFENSIVE_GATE_EVIDENCE_VERSION = "operating-defensive-gate-evidence-v1"
DEFENSIVE_ORDER_PREVIEW_VERSION = "operating-defensive-order-preview-v1"


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


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


def _result(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    result = payload.get("result")
    return result if isinstance(result, dict) else payload


def _float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return float(default)
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _symbol(value: Any) -> str:
    return str(value or "").strip().upper() or "UNKNOWN"


def _exit_quantity(position: dict[str, Any], strategy: str) -> float:
    if strategy == "buy_to_cover_or_reduce_short":
        return abs(_float(position.get("open_short_quantity"))) or abs(_float(position.get("open_net_quantity")))
    return abs(_float(position.get("open_long_quantity"))) or abs(_float(position.get("open_net_quantity")))


def _order_action(strategy: str) -> str:
    if strategy == "buy_to_cover_or_reduce_short":
        return "BUY"
    if strategy == "sell_or_reduce_long":
        return "SELL"
    return "ANALYZE"


def _action_rows(plan_payload: dict[str, Any]) -> list[dict[str, Any]]:
    plan = _result(plan_payload)
    return [row for row in plan.get("top_actions") or [] if isinstance(row, dict)]


def build_operating_defensive_order_preview(
    defensive_action_plan_payload: dict[str, Any],
    *,
    account: str | None = None,
    max_preview_items: int = 10,
    now: str | None = None,
) -> dict[str, Any]:
    plan = _result(defensive_action_plan_payload)
    preview_items: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for row in _action_rows(plan):
        symbol = _symbol(row.get("symbol"))
        action_type = str(row.get("action_type") or "")
        strategy = str(row.get("strategy") or "")
        if action_type != "exit_or_reduce_position":
            skipped.append({"symbol": symbol, "reason": "not_exit_or_reduce_position", "action_type": action_type, "strategy": strategy})
            continue
        position = row.get("source_position") if isinstance(row.get("source_position"), dict) else {}
        quantity = _exit_quantity(position, strategy)
        action = _order_action(strategy)
        if quantity <= 0 or action == "ANALYZE":
            skipped.append({"symbol": symbol, "reason": "missing_supported_exit_quantity", "action_type": action_type, "strategy": strategy})
            continue
        preview_items.append(
            {
                "preview_status": "preview_ready",
                "symbol": symbol,
                "ticket_type": "stock",
                "source": "m186_operating_defensive_action_plan",
                "source_action_type": action_type,
                "source_strategy": strategy,
                "source_priority_score": row.get("priority_score"),
                "contract_preview": {
                    "symbol": symbol,
                    "sec_type": "STK",
                    "exchange": "SMART",
                    "currency": "USD",
                },
                "ibkr_what_if_order_preview": {
                    "action": action,
                    "order_type": "MKT",
                    "total_quantity": quantity,
                    "tif": "DAY",
                    "account": account,
                    "what_if": True,
                    "transmit": False,
                    "outside_rth": False,
                    "order_type_note": "MKT is used for margin preview only; execution layer must choose live limit/market policy separately.",
                },
                "local_margin_estimate": {
                    "estimate_method": "m186_defensive_notional_reference_v1",
                    "estimated_notional": row.get("estimated_notional"),
                    "reference_mark_price": position.get("latest_mark_price"),
                    "reference_mark_source": position.get("latest_mark_source"),
                },
                "source_action": row,
                "safety": {
                    "what_if_only": True,
                    "local_preview_only": True,
                    "ticket_created": False,
                    "ibkr_order_submitted": False,
                    "transmit_enabled": False,
                    "live_order_routing_enabled": False,
                    "portfolio_mutated": False,
                },
            }
        )
        if len(preview_items) >= max(0, int(max_preview_items)):
            break
    action_counts = Counter(str((item.get("ibkr_what_if_order_preview") or {}).get("action") or "unknown") for item in preview_items)
    return {
        "order_preview_version": DEFENSIVE_ORDER_PREVIEW_VERSION,
        "generated_at": now or _iso_now(),
        "source_action_plan_version": plan.get("operating_defensive_action_plan_version"),
        "source_generated_at": plan.get("generated_at"),
        "selected_account": account,
        "preview_item_count": len(preview_items),
        "skipped_count": len(skipped),
        "preview_status_counts": {"preview_ready": len(preview_items)} if preview_items else {},
        "action_counts": dict(action_counts),
        "preview_items": preview_items,
        "sample_preview_items": preview_items[:20],
        "skipped_items": skipped,
        "safety": {
            "mode": "paper_defensive_order_preview_queue",
            "policy": "Builds IBKR what-if-compatible order preview rows from M186 defensive exit/reduce recommendations only. No IBKR call, ticket creation, transmit, or portfolio mutation is performed by this builder.",
            "what_if_only": True,
            "ticket_created": False,
            "ibkr_order_submitted": False,
            "transmit_enabled": False,
            "live_order_routing_enabled": False,
            "portfolio_mutated": False,
        },
    }


def _broker_whatif_rows(payload: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    data = _result(payload)
    rows: dict[str, dict[str, Any]] = {}
    for key in ("responses", "what_if_results", "order_previews", "previews", "rows", "items"):
        raw = data.get(key)
        if not isinstance(raw, list):
            continue
        for row in raw:
            if not isinstance(row, dict):
                continue
            symbol = row.get("symbol") or row.get("underlying_symbol")
            if not symbol:
                contract = row.get("contract") if isinstance(row.get("contract"), dict) else {}
                symbol = contract.get("symbol")
            if not symbol:
                continue
            status = str(row.get("status") or row.get("preview_status") or row.get("whatif_status") or "ok").lower()
            if status in {"blocked", "error", "failed", "missing"}:
                continue
            rows[_symbol(symbol)] = row
    return rows


def _option_quote_symbols(payload: dict[str, Any] | None) -> set[str]:
    data = _result(payload)
    symbols: set[str] = set()
    rows: list[Any] = []
    for key in ("quotes", "option_quotes", "quote_rows", "gated_candidates", "rows", "items"):
        raw = data.get(key)
        if isinstance(raw, list):
            rows.extend(raw)
    for row in rows:
        if not isinstance(row, dict):
            continue
        status = str(row.get("quote_status") or row.get("status") or row.get("gate_status") or "").lower()
        if status not in {"quoted", "wide_spread", "ok", "passed"}:
            continue
        symbol = row.get("symbol") or row.get("underlying_symbol")
        if symbol:
            symbols.add(_symbol(symbol))
    return symbols


def _option_snapshot_symbols(payload: dict[str, Any] | None) -> set[str]:
    data = _result(payload)
    symbols: set[str] = set()
    for key in ("quotes", "option_quotes", "quote_rows", "candidate_contracts", "rows", "items"):
        raw = data.get(key)
        if not isinstance(raw, list):
            continue
        for row in raw:
            if not isinstance(row, dict):
                continue
            symbol = row.get("symbol") or row.get("underlying_symbol")
            if symbol:
                symbols.add(_symbol(symbol))
    return symbols


def _option_chain_symbols(payload: dict[str, Any] | None) -> set[str]:
    data = _result(payload)
    symbols: set[str] = set()
    for key in ("candidate_contracts", "chains", "rows", "items"):
        raw = data.get(key)
        if not isinstance(raw, list):
            continue
        for row in raw:
            if not isinstance(row, dict):
                continue
            symbol = row.get("symbol") or row.get("underlying_symbol")
            if symbol:
                symbols.add(_symbol(symbol))
    return symbols


def build_operating_defensive_gate_evidence(
    defensive_action_plan_payload: dict[str, Any],
    *,
    broker_whatif_payload: dict[str, Any] | None = None,
    option_quote_payload: dict[str, Any] | None = None,
    option_chain_payload: dict[str, Any] | None = None,
    account: str | None = None,
    max_preview_items: int = 10,
    now: str | None = None,
) -> dict[str, Any]:
    plan = _result(defensive_action_plan_payload)
    actions = _action_rows(plan)
    order_preview = build_operating_defensive_order_preview(
        plan,
        account=account,
        max_preview_items=max_preview_items,
        now=now,
    )
    broker_rows = _broker_whatif_rows(broker_whatif_payload)
    quote_symbols = _option_quote_symbols(option_quote_payload)
    chain_symbols = _option_chain_symbols(option_chain_payload) | _option_snapshot_symbols(option_quote_payload)

    what_if_results: list[dict[str, Any]] = []
    option_quote_results: list[dict[str, Any]] = []
    for row in actions:
        symbol = _symbol(row.get("symbol"))
        action_type = str(row.get("action_type") or "")
        strategy = str(row.get("strategy") or "")
        requires_whatif = bool(row.get("requires_margin_whatif"))
        if action_type == "exit_or_reduce_position" or requires_whatif:
            broker_row = broker_rows.get(symbol)
            what_if_results.append(
                {
                    "symbol": symbol,
                    "status": "ok" if broker_row else "missing",
                    "action_type": action_type,
                    "strategy": strategy,
                    "requires_margin_whatif": requires_whatif,
                    "broker_response": broker_row or {},
                    "safety": {
                        "what_if_only": True,
                        "transmit_enabled": False,
                        "ibkr_order_submitted": False,
                    },
                }
            )
        if row.get("requires_option_chain") or row.get("requires_live_option_quotes"):
            has_quote = symbol in quote_symbols
            has_chain = symbol in chain_symbols or has_quote
            option_quote_results.append(
                {
                    "symbol": symbol,
                    "status": "ok" if has_quote and has_chain else "missing",
                    "action_type": action_type,
                    "strategy": strategy,
                    "option_chain_available": bool(has_chain),
                    "option_quote_available": bool(has_quote),
                    "requires_option_chain": bool(row.get("requires_option_chain")),
                    "requires_live_option_quotes": bool(row.get("requires_live_option_quotes")),
                    "safety": {
                        "read_only_option_quote_gate": True,
                        "ibkr_order_submitted": False,
                        "transmit_enabled": False,
                    },
                }
            )

    whatif_counts = Counter(row.get("status") for row in what_if_results)
    option_counts = Counter(row.get("status") for row in option_quote_results)
    return {
        "operating_defensive_gate_evidence_version": DEFENSIVE_GATE_EVIDENCE_VERSION,
        "generated_at": now or _iso_now(),
        "source_action_plan_version": plan.get("operating_defensive_action_plan_version"),
        "source_generated_at": plan.get("generated_at"),
        "order_preview": order_preview,
        "what_if_results": what_if_results,
        "option_quote_results": option_quote_results,
        "option_quotes": option_quote_results,
        "summary": {
            "order_preview_item_count": order_preview.get("preview_item_count"),
            "whatif_status_counts": dict(whatif_counts),
            "option_quote_status_counts": dict(option_counts),
            "whatif_ok_count": int(whatif_counts.get("ok") or 0),
            "option_quote_ok_count": int(option_counts.get("ok") or 0),
        },
        "safety": {
            "mode": "paper_defensive_gate_evidence_adapter",
            "policy": "Normalizes M186 defensive action rows, optional IBKR what-if responses, and optional read-only option quote/chain snapshots into M187 gate evidence. This adapter itself does not call IBKR, create tickets, submit orders, enable transmit, or mutate portfolio state.",
            "ticket_created": False,
            "ibkr_order_submitted": False,
            "transmit_enabled": False,
            "live_order_routing_enabled": False,
            "portfolio_mutated": False,
        },
    }


def build_operating_defensive_gate_evidence_from_paths(
    *,
    defensive_action_plan_path: Path,
    broker_whatif_path: Path | None = None,
    option_quote_path: Path | None = None,
    option_chain_path: Path | None = None,
    account: str | None = None,
    max_preview_items: int = 10,
    now: str | None = None,
) -> dict[str, Any]:
    return build_operating_defensive_gate_evidence(
        _read_json(defensive_action_plan_path),
        broker_whatif_payload=_read_json(broker_whatif_path),
        option_quote_payload=_read_json(option_quote_path),
        option_chain_payload=_read_json(option_chain_path),
        account=account,
        max_preview_items=max_preview_items,
        now=now,
    )


def operating_defensive_gate_evidence_markdown(
    report: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
    sample_limit: int = 20,
    title: str = "Operating Defensive Gate Evidence",
) -> str:
    filters = filters or {}
    summary = report.get("summary") or {}
    lines = [
        f"# {title}",
        "",
        f"- Version: `{report.get('operating_defensive_gate_evidence_version')}`",
        f"- Generated at: `{report.get('generated_at')}`",
        f"- Source plan: `{report.get('source_action_plan_version')}` generated `{report.get('source_generated_at')}`",
        f"- Order preview items: `{summary.get('order_preview_item_count')}`",
        f"- What-if status counts: `{summary.get('whatif_status_counts') or {}}`",
        f"- Option quote status counts: `{summary.get('option_quote_status_counts') or {}}`",
        "- Safety: evidence adapter only; no ticket, broker order submission, transmit, or portfolio mutation.",
        "",
        "## Inputs",
        "",
    ]
    if filters:
        for key, value in filters.items():
            lines.append(f"- `{key}`: `{value if value is not None else '-'}`")
    else:
        lines.append("- none")
    lines.extend(["", "## What-if evidence", "", "| Symbol | Status | Strategy | Requires what-if |", "| --- | --- | --- | --- |"])
    for row in (report.get("what_if_results") or [])[: max(0, int(sample_limit))]:
        lines.append(f"| `{row.get('symbol')}` | `{row.get('status')}` | `{row.get('strategy')}` | {row.get('requires_margin_whatif')} |")
    if not report.get("what_if_results"):
        lines.append("| - | - | - | - |")
    lines.extend(["", "## Option quote/chain evidence", "", "| Symbol | Status | Strategy | Chain | Quote |", "| --- | --- | --- | --- | --- |"])
    for row in (report.get("option_quote_results") or [])[: max(0, int(sample_limit))]:
        lines.append(
            f"| `{row.get('symbol')}` | `{row.get('status')}` | `{row.get('strategy')}` | {row.get('option_chain_available')} | {row.get('option_quote_available')} |"
        )
    if not report.get("option_quote_results"):
        lines.append("| - | - | - | - | - |")
    return "\n".join(lines) + "\n"


def write_operating_defensive_gate_evidence_json(output_path: Path, report: dict[str, Any]) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return output_path


def write_operating_defensive_order_preview_json(output_path: Path, report: dict[str, Any]) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report.get("order_preview") or report, indent=2, ensure_ascii=False), encoding="utf-8")
    return output_path


def write_operating_defensive_gate_evidence_markdown(
    output_path: Path,
    report: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
    sample_limit: int = 20,
) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        operating_defensive_gate_evidence_markdown(report, filters=filters, sample_limit=sample_limit),
        encoding="utf-8",
    )
    return output_path


# --- merged from defensive_gate_retry.py ---
DEFENSIVE_GATE_RETRY_VERSION = "operating-defensive-gate-retry-v2"
OPTION_QUOTE_RESUME_PLAN_VERSION = "operating-option-quote-resume-plan-v1"


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _result(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    result = payload.get("result")
    return result if isinstance(result, dict) else payload


def read_json(path: Path | None) -> dict[str, Any]:
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


def write_json(path: Path, payload: dict[str, Any]) -> Path:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def _symbol(value: Any) -> str:
    return str(value or "").strip().upper()


def defensive_option_symbols_from_plan(defensive_action_plan_payload: dict[str, Any]) -> list[str]:
    plan = _result(defensive_action_plan_payload)
    symbols: set[str] = set()
    for row in plan.get("top_actions") or []:
        if not isinstance(row, dict):
            continue
        if row.get("requires_option_chain") or row.get("requires_live_option_quotes"):
            symbol = _symbol(row.get("symbol"))
            if symbol:
                symbols.add(symbol)
    return sorted(symbols)


def detect_ibkr_read_only_mode(payload: dict[str, Any] | None) -> bool:
    data = _result(payload)
    if bool(data.get("read_only_mode_detected")):
        return True
    for error in data.get("errors") or []:
        if not isinstance(error, dict):
            continue
        message = str(error.get("message") or "").lower()
        if "read-only mode" in message or "read only mode" in message:
            return True
    return False


def evaluate_option_quote_session_gate(
    *,
    now: str | None = None,
    timezone_name: str = "America/New_York",
    regular_start: str = "09:30",
    regular_end: str = "16:00",
    regular_session_only: bool = True,
) -> dict[str, Any]:
    tz = ZoneInfo(timezone_name)
    if now:
        parsed = datetime.fromisoformat(str(now).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=tz)
        now_local = parsed.astimezone(tz)
    else:
        now_local = datetime.now(UTC).astimezone(tz)

    def _minutes(value: str) -> int:
        hour, minute = str(value).split(":", 1)
        return int(hour) * 60 + int(minute)

    minute_of_day = now_local.hour * 60 + now_local.minute
    start_min = _minutes(regular_start)
    end_min = _minutes(regular_end)
    is_weekday = now_local.weekday() < 5
    if not is_weekday:
        session = "closed"
        reason = "US option market is closed on weekend."
    elif minute_of_day < start_min:
        session = "pre_market"
        reason = "US option regular session has not opened yet."
    elif start_min <= minute_of_day < end_min:
        session = "regular"
        reason = "US option market is in regular session."
    else:
        session = "after_hours"
        reason = "US option regular session has ended."
    allowed = (not regular_session_only) or session == "regular"
    if not allowed:
        reason = f"{reason} Option bid/ask retries are deferred to regular session."
    return {
        "session_gate_version": "option-quote-session-gate-v1",
        "regular_session_only": bool(regular_session_only),
        "timezone": timezone_name,
        "now_local": now_local.isoformat(),
        "session": session,
        "allowed": bool(allowed),
        "reason": reason,
        "regular_start": regular_start,
        "regular_end": regular_end,
    }


def next_regular_option_session_open(
    *,
    now: str | None = None,
    timezone_name: str = "America/New_York",
    regular_start: str = "09:30",
    regular_end: str = "16:00",
) -> dict[str, Any]:
    gate = evaluate_option_quote_session_gate(
        now=now,
        timezone_name=timezone_name,
        regular_start=regular_start,
        regular_end=regular_end,
        regular_session_only=True,
    )
    tz = ZoneInfo(timezone_name)
    now_local = datetime.fromisoformat(str(gate["now_local"]))

    def _time_parts(value: str) -> tuple[int, int]:
        hour, minute = str(value).split(":", 1)
        return int(hour), int(minute)

    start_hour, start_minute = _time_parts(regular_start)
    end_hour, end_minute = _time_parts(regular_end)
    open_today = now_local.replace(hour=start_hour, minute=start_minute, second=0, microsecond=0)
    close_today = now_local.replace(hour=end_hour, minute=end_minute, second=0, microsecond=0)
    if gate["session"] == "regular":
        next_open = now_local
        status = "ready_now"
    elif now_local.weekday() < 5 and now_local < open_today:
        next_open = open_today
        status = "waiting_for_today_regular_open"
    else:
        next_day = (now_local + timedelta(days=1)).date()
        while next_day.weekday() >= 5:
            next_day = next_day + timedelta(days=1)
        next_open = datetime(next_day.year, next_day.month, next_day.day, start_hour, start_minute, tzinfo=tz)
        status = "waiting_for_next_regular_open"
    wait_seconds = max(0.0, (next_open - now_local).total_seconds()) if status != "ready_now" else 0.0
    return {
        "next_regular_option_session_open_version": "next-regular-option-session-open-v1",
        "status": status,
        "session_gate": gate,
        "next_open_local": next_open.isoformat(),
        "next_open_utc": next_open.astimezone(UTC).isoformat(),
        "regular_close_reference_local": close_today.isoformat(),
        "wait_seconds": wait_seconds,
        "wait_hours": wait_seconds / 3600.0,
    }


def _quote_command(
    *,
    defensive_action_plan_json: str,
    existing_whatif_json: str | None,
    existing_option_quote_json: str | None,
    account: str | None,
    output_dir: str,
    output: str,
    json_output_path: str,
    option_attempts: int,
    option_market_data_types: str,
    option_quote_seconds: float,
    option_chain_timeout_seconds: float,
    max_quote_contracts: int,
) -> str:
    parts = [
        "python",
        "main.py",
        "operating-defensive-gate-retry",
        "--defensive-action-plan-json",
        defensive_action_plan_json,
    ]
    if account:
        parts.extend(["--account", account])
    if existing_whatif_json:
        parts.extend(["--existing-whatif-json", existing_whatif_json])
    if existing_option_quote_json:
        parts.extend(["--existing-option-quote-json", existing_option_quote_json])
    parts.extend(
        [
            "--run-option-quotes",
            "--regular-session-only-option-quotes",
            "--option-attempts",
            str(int(option_attempts)),
            "--option-market-data-types",
            option_market_data_types,
            "--option-quote-seconds",
            str(float(option_quote_seconds)),
            "--option-chain-timeout-seconds",
            str(float(option_chain_timeout_seconds)),
            "--max-quote-contracts",
            str(int(max_quote_contracts)),
            "--output-dir",
            output_dir,
            "--output",
            output,
            "--json-output-path",
            json_output_path,
        ]
    )
    return " ".join(parts)


def build_option_quote_resume_plan(
    *,
    latest_retry_payload: dict[str, Any] | None = None,
    defensive_action_plan_payload: dict[str, Any] | None = None,
    defensive_action_plan_json: str = "exports/m186_operating_defensive_action_plan.json",
    existing_whatif_json: str | None = None,
    existing_option_quote_json: str | None = None,
    account: str | None = None,
    output_dir: str = "exports/m192_regular_session_option_quote_retry",
    output: str = "exports/m192_regular_session_option_quote_retry.md",
    json_output_path: str = "exports/m192_regular_session_option_quote_retry.json",
    option_attempts: int = 3,
    option_market_data_types: str = "1,3",
    option_quote_seconds: float = 20.0,
    option_chain_timeout_seconds: float = 30.0,
    max_quote_contracts: int = 60,
    now: str | None = None,
) -> dict[str, Any]:
    retry = _result(latest_retry_payload)
    plan = _result(defensive_action_plan_payload)
    retry_summary = retry.get("summary") or {}
    next_open = next_regular_option_session_open(now=now)
    option_symbols = defensive_option_symbols_from_plan(plan)
    best_whatif = int(retry_summary.get("best_whatif_response_count") or 0)
    pending_count = int(retry_summary.get("strict_ledger_pending_count") or 0)
    blocked_count = int(retry_summary.get("strict_ledger_blocked_count") or 0)
    deferred = bool(retry_summary.get("option_quote_deferred"))
    ready_for_regular_retry = bool(option_symbols and best_whatif > 0 and blocked_count > 0)
    command = _quote_command(
        defensive_action_plan_json=defensive_action_plan_json,
        existing_whatif_json=existing_whatif_json,
        existing_option_quote_json=existing_option_quote_json,
        account=account,
        output_dir=output_dir,
        output=output,
        json_output_path=json_output_path,
        option_attempts=option_attempts,
        option_market_data_types=option_market_data_types,
        option_quote_seconds=option_quote_seconds,
        option_chain_timeout_seconds=option_chain_timeout_seconds,
        max_quote_contracts=max_quote_contracts,
    )
    blockers: list[str] = []
    if not option_symbols:
        blockers.append("no_defensive_option_symbols")
    if best_whatif <= 0:
        blockers.append("missing_stock_whatif_evidence")
    if not deferred and int(retry_summary.get("best_option_quoted_count") or 0) <= 0:
        blockers.append("option_quotes_not_deferred_but_unavailable")
    return {
        "option_quote_resume_plan_version": OPTION_QUOTE_RESUME_PLAN_VERSION,
        "generated_at": _iso_now(),
        "status": "ready_for_next_regular_session" if ready_for_regular_retry and next_open.get("status") != "ready_now" else ("ready_now" if ready_for_regular_retry else "blocked"),
        "ready_for_regular_retry": ready_for_regular_retry,
        "next_regular_session": next_open,
        "source_retry_version": retry.get("operating_defensive_gate_retry_version"),
        "source_retry_generated_at": retry.get("generated_at"),
        "source_defensive_option_symbols": option_symbols,
        "summary": {
            "best_whatif_response_count": best_whatif,
            "best_option_quoted_count": int(retry_summary.get("best_option_quoted_count") or 0),
            "strict_ledger_pending_count": pending_count,
            "strict_ledger_blocked_count": blocked_count,
            "option_quote_deferred": deferred,
            "option_quote_session": retry_summary.get("option_quote_session"),
            "blockers": blockers,
        },
        "resume_command": command,
        "resume_parameters": {
            "defensive_action_plan_json": defensive_action_plan_json,
            "existing_whatif_json": existing_whatif_json,
            "existing_option_quote_json": existing_option_quote_json,
            "account": account,
            "output_dir": output_dir,
            "output": output,
            "json_output_path": json_output_path,
            "option_attempts": int(option_attempts),
            "option_market_data_types": option_market_data_types,
            "option_quote_seconds": float(option_quote_seconds),
            "option_chain_timeout_seconds": float(option_chain_timeout_seconds),
            "max_quote_contracts": int(max_quote_contracts),
        },
        "safety": {
            "mode": "option_quote_resume_plan",
            "read_only": True,
            "ibkr_order_submitted": False,
            "live_order_transmitted": False,
            "portfolio_mutated": False,
        },
    }


def option_quote_resume_plan_markdown(report: dict[str, Any]) -> str:
    session = report.get("next_regular_session") or {}
    summary = report.get("summary") or {}
    lines = [
        "# Option Quote Resume Plan",
        "",
        f"- Version: `{report.get('option_quote_resume_plan_version')}`",
        f"- Generated at: `{report.get('generated_at')}`",
        f"- Status: `{report.get('status')}`",
        f"- Ready for regular retry: `{report.get('ready_for_regular_retry')}`",
        f"- Defensive option symbols: `{', '.join(report.get('source_defensive_option_symbols') or []) or '-'}`",
        f"- What-if responses: `{summary.get('best_whatif_response_count')}`",
        f"- Strict ledger pending / blocked: `{summary.get('strict_ledger_pending_count')}` / `{summary.get('strict_ledger_blocked_count')}`",
        f"- Option quote deferred/session: `{summary.get('option_quote_deferred')}` / `{summary.get('option_quote_session') or '-'}`",
        f"- Next regular option open: `{session.get('next_open_local')}`",
        f"- Wait hours: `{round(float(session.get('wait_hours') or 0.0), 3)}`",
        f"- Blockers: `{', '.join(summary.get('blockers') or []) or '-'}`",
        "- Safety: read-only resume plan; no broker order submission or live order transmission.",
        "",
        "## Resume command",
        "",
        "```powershell",
        str(report.get("resume_command") or ""),
        "```",
    ]
    return "\n".join(lines) + "\n"


def write_option_quote_resume_plan_json(output_path: Path, report: dict[str, Any]) -> Path:
    return write_json(output_path, report)


def write_option_quote_resume_plan_markdown(output_path: Path, report: dict[str, Any]) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(option_quote_resume_plan_markdown(report), encoding="utf-8")
    return output_path


def _whatif_attempt_summary(payload: dict[str, Any] | None, *, attempt_index: int) -> dict[str, Any]:
    data = _result(payload)
    errors = data.get("errors") or []
    return {
        "attempt_index": attempt_index,
        "generated_at": data.get("generated_at"),
        "requested_count": int(data.get("requested_count") or 0),
        "response_count": int(data.get("response_count") or 0),
        "skipped_count": int(data.get("skipped_count") or 0),
        "error_count": len(errors) if isinstance(errors, list) else 0,
        "read_only_mode_detected": detect_ibkr_read_only_mode(data),
    }


def _option_quote_attempt_summary(payload: dict[str, Any] | None, *, attempt_index: int) -> dict[str, Any]:
    data = _result(payload)
    status_counts = data.get("status_counts") or {}
    quoted_contracts = data.get("quoted_contracts")
    if quoted_contracts is None:
        quoted_contracts = int(status_counts.get("quoted") or 0) + int(status_counts.get("wide_spread") or 0)
    requested = data.get("requested_quotes")
    if requested is None:
        requested = len(data.get("quotes") or [])
    return {
        "attempt_index": attempt_index,
        "generated_at": data.get("generated_at"),
        "requested_quotes": int(requested or 0),
        "quoted_contracts": int(quoted_contracts or 0),
        "status_counts": status_counts,
        "error_count": len(data.get("errors") or []),
    }


def build_operating_defensive_gate_retry_report(
    *,
    defensive_action_plan_payload: dict[str, Any] | None = None,
    order_preview_payload: dict[str, Any] | None = None,
    whatif_attempt_payloads: list[dict[str, Any]] | None = None,
    option_quote_attempt_payloads: list[dict[str, Any]] | None = None,
    gate_evidence_payload: dict[str, Any] | None = None,
    intent_ledger_payload: dict[str, Any] | None = None,
    retry_policy: dict[str, Any] | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    plan = _result(defensive_action_plan_payload)
    order_preview = _result(order_preview_payload)
    evidence = _result(gate_evidence_payload)
    ledger = _result(intent_ledger_payload)
    whatif_attempts = [
        _whatif_attempt_summary(payload, attempt_index=index)
        for index, payload in enumerate(whatif_attempt_payloads or [], start=1)
    ]
    option_attempts = [
        _option_quote_attempt_summary(payload, attempt_index=index)
        for index, payload in enumerate(option_quote_attempt_payloads or [], start=1)
    ]
    read_only_detected = any(item.get("read_only_mode_detected") for item in whatif_attempts)
    best_whatif_response_count = max((int(item.get("response_count") or 0) for item in whatif_attempts), default=0)
    best_option_quoted_count = max((int(item.get("quoted_contracts") or 0) for item in option_attempts), default=0)
    retry_policy = retry_policy or {}
    option_session_gate = retry_policy.get("option_quote_session_gate") if isinstance(retry_policy.get("option_quote_session_gate"), dict) else {}
    option_quote_deferred = bool(option_session_gate) and not bool(option_session_gate.get("allowed", True))
    evidence_summary = evidence.get("summary") or {}
    pending_count = int(ledger.get("pending_defensive_paper_execution_count") or 0)
    blocked_count = int(ledger.get("blocked_count") or 0)
    blockers: list[str] = []
    if read_only_detected:
        blockers.append("ibkr_api_read_only_mode_blocks_broker_whatif")
    elif whatif_attempts and best_whatif_response_count <= 0:
        blockers.append("broker_whatif_no_responses_after_retry")
    if option_quote_deferred and best_option_quoted_count <= 0:
        blockers.append("option_quote_deferred_until_regular_session")
    elif option_attempts and best_option_quoted_count <= 0:
        blockers.append("option_bid_ask_quotes_unavailable_after_retry")
    if pending_count <= 0 and blocked_count > 0:
        blockers.append("strict_defensive_ledger_has_no_pending_intents")

    next_actions: list[dict[str, Any]] = []
    if read_only_detected:
        next_actions.append(
            {
                "action_id": "disable_ibkr_api_read_only_mode_then_retry_whatif",
                "priority": 1,
                "description": "In TWS/Gateway API settings, disable API Read-Only mode if broker-side what-if previews are desired; reconnect the API session, then rerun the broker what-if retry. Keep whatIf=True. IBKR requires the API order transmit=True flag for what-if calculation, but no live order is routed.",
                "automation_safe": False,
            }
        )
    elif whatif_attempts and best_whatif_response_count <= 0:
        next_actions.append(
            {
                "action_id": "retry_ibkr_whatif_preview",
                "priority": 2,
                "description": "Retry explicit IBKR what-if preview with the defensive order-preview JSON. Keep whatIf=True; IBKR requires API transmit=True for the calculation, but no live order is routed.",
                "automation_safe": True,
            }
        )
    if option_quote_deferred and best_option_quoted_count <= 0:
        next_actions.append(
            {
                "action_id": "run_option_quote_retry_next_regular_session",
                "priority": 3,
                "description": "US option regular session is not active. Preserve current stock what-if evidence and rerun option quote snapshots during the next liquid regular-session window.",
                "automation_safe": True,
                "deferred_until_session": "regular",
            }
        )
    elif option_attempts and best_option_quoted_count <= 0:
        next_actions.append(
            {
                "action_id": "retry_option_quotes_during_regular_session_or_use_longer_quote_window",
                "priority": 3,
                "description": "Retry option quote snapshots during liquid regular-session windows, increase quote seconds, and try live/delayed market-data types; strict option gates require bid/ask quotes, not only last/close.",
                "automation_safe": True,
            }
        )

    option_status_counter: Counter[str] = Counter()
    for attempt in option_attempts:
        for key, value in (attempt.get("status_counts") or {}).items():
            option_status_counter[str(key)] += int(value or 0)

    return {
        "operating_defensive_gate_retry_version": DEFENSIVE_GATE_RETRY_VERSION,
        "generated_at": now or _iso_now(),
        "source_action_plan_version": plan.get("operating_defensive_action_plan_version"),
        "source_action_count": len(plan.get("top_actions") or []),
        "source_defensive_option_symbols": defensive_option_symbols_from_plan(plan),
        "order_preview_item_count": order_preview.get("preview_item_count"),
        "whatif_attempt_count": len(whatif_attempts),
        "option_quote_attempt_count": len(option_attempts),
        "whatif_attempts": whatif_attempts,
        "option_quote_attempts": option_attempts,
        "summary": {
            "read_only_mode_detected": bool(read_only_detected),
            "best_whatif_response_count": best_whatif_response_count,
            "best_option_quoted_count": best_option_quoted_count,
            "option_quote_deferred": option_quote_deferred,
            "option_quote_session": option_session_gate.get("session"),
            "aggregate_option_status_counts": dict(option_status_counter),
            "evidence_whatif_ok_count": evidence_summary.get("whatif_ok_count"),
            "evidence_option_quote_ok_count": evidence_summary.get("option_quote_ok_count"),
            "strict_ledger_pending_count": pending_count,
            "strict_ledger_blocked_count": blocked_count,
            "blockers": blockers,
            "next_action_count": len(next_actions),
        },
        "next_actions": next_actions,
        "retry_policy": retry_policy,
        "safety": {
            "mode": "defensive_gate_retry_diagnostics",
            "policy": "Aggregates explicit what-if and read-only option quote retry attempts. What-if attempts must keep whatIf=True. IBKR requires the API order transmit=True flag for what-if calculation, but no live order is routed. This report does not submit live orders or mutate real portfolio state.",
            "ticket_created": False,
            "ibkr_order_submitted": False,
            "transmit_enabled": False,
            "live_order_transmitted": False,
            "live_order_routing_enabled": False,
            "portfolio_mutated": False,
        },
    }


def operating_defensive_gate_retry_markdown(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    lines = [
        "# Operating Defensive Gate Retry Report",
        "",
        f"- Version: `{report.get('operating_defensive_gate_retry_version')}`",
        f"- Generated at: `{report.get('generated_at')}`",
        f"- Source action count: `{report.get('source_action_count')}`",
        f"- Defensive option symbols: `{', '.join(report.get('source_defensive_option_symbols') or []) or '-'}`",
        f"- Order preview items: `{report.get('order_preview_item_count')}`",
        f"- What-if attempts / best responses: `{report.get('whatif_attempt_count')}` / `{summary.get('best_whatif_response_count')}`",
        f"- Option quote attempts / best quoted contracts: `{report.get('option_quote_attempt_count')}` / `{summary.get('best_option_quoted_count')}`",
        f"- Option quote session: `{summary.get('option_quote_session') or '-'}` deferred `{summary.get('option_quote_deferred', False)}`",
        f"- Read-Only mode detected: `{summary.get('read_only_mode_detected')}`",
        f"- Strict ledger pending / blocked: `{summary.get('strict_ledger_pending_count')}` / `{summary.get('strict_ledger_blocked_count')}`",
        f"- Blockers: `{', '.join(summary.get('blockers') or []) or '-'}`",
        "- Safety: diagnostic/retry layer only; no live order transmission.",
        "",
        "## Next actions",
        "",
        "| Priority | Action | Automation safe | Description |",
        "| ---: | --- | --- | --- |",
    ]
    for item in report.get("next_actions") or []:
        lines.append(
            f"| {item.get('priority')} | `{item.get('action_id')}` | {item.get('automation_safe')} | {str(item.get('description') or '-').replace('|', '/')} |"
        )
    if not report.get("next_actions"):
        lines.append("| - | `none` | - | No retry action required. |")
    session_gate = (report.get("retry_policy") or {}).get("option_quote_session_gate") or {}
    if session_gate:
        lines.extend(
            [
                "",
                "## Option quote session gate",
                "",
                f"- Session: `{session_gate.get('session')}`",
                f"- Allowed: `{session_gate.get('allowed')}`",
                f"- Local time: `{session_gate.get('now_local')}`",
                f"- Reason: {session_gate.get('reason')}",
            ]
        )
    lines.extend(["", "## What-if attempts", "", "| Attempt | Requested | Responses | Errors | Read-Only |", "| ---: | ---: | ---: | ---: | --- |"])
    for item in report.get("whatif_attempts") or []:
        lines.append(
            f"| {item.get('attempt_index')} | {item.get('requested_count')} | {item.get('response_count')} | {item.get('error_count')} | {item.get('read_only_mode_detected')} |"
        )
    if not report.get("whatif_attempts"):
        lines.append("| - | - | - | - | - |")
    lines.extend(["", "## Option quote attempts", "", "| Attempt | Requested | Quoted | Errors | Status counts |", "| ---: | ---: | ---: | ---: | --- |"])
    for item in report.get("option_quote_attempts") or []:
        lines.append(
            f"| {item.get('attempt_index')} | {item.get('requested_quotes')} | {item.get('quoted_contracts')} | {item.get('error_count')} | `{item.get('status_counts') or {}}` |"
        )
    if not report.get("option_quote_attempts"):
        lines.append("| - | - | - | - | - |")
    return "\n".join(lines) + "\n"


def write_operating_defensive_gate_retry_json(output_path: Path, report: dict[str, Any]) -> Path:
    return write_json(output_path, report)


def write_operating_defensive_gate_retry_markdown(output_path: Path, report: dict[str, Any]) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(operating_defensive_gate_retry_markdown(report), encoding="utf-8")
    return output_path
