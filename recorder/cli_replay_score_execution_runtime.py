from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from recorder.config import Settings
from recorder.cli_paper_cycle_runtime import _parse_path_list, _write_component_json
from .cli_replay_score_helpers import _parse_float_grid, _parse_float_tuple, _parse_inline_mark_prices

def command_replay_score_execution_readiness(
    allocation_json_path: Path,
    max_option_max_loss: float,
    max_option_net_debit: float,
    min_option_reward_to_risk: float,
    option_slippage_buffer_pct: float,
    min_option_dte: int,
    max_stock_notional: float,
    max_stock_stop_loss: float,
    min_group_rows: int,
    sample_limit: int,
    output: Path | None = None,
    json_output_path: Path | None = None,
    json_output: bool = False,
) -> None:
    from recorder.options_analysis import (
        build_execution_readiness_report,
        execution_readiness_markdown,
        write_execution_readiness_markdown,
    )

    allocation_json_path = allocation_json_path.expanduser().resolve()
    allocation_payload = json.loads(allocation_json_path.read_text(encoding="utf-8"))
    filters = {
        "allocation_json": str(allocation_json_path),
        "max_option_max_loss": max_option_max_loss,
        "max_option_net_debit": max_option_net_debit,
        "min_option_reward_to_risk": min_option_reward_to_risk,
        "option_slippage_buffer_pct": option_slippage_buffer_pct,
        "min_option_dte": min_option_dte,
        "max_stock_notional": max_stock_notional,
        "max_stock_stop_loss": max_stock_stop_loss,
        "min_group_rows": min_group_rows,
        "sample_limit": sample_limit,
    }
    result = build_execution_readiness_report(
        allocation_payload,
        max_option_max_loss=max_option_max_loss,
        max_option_net_debit=max_option_net_debit,
        min_option_reward_to_risk=min_option_reward_to_risk,
        option_slippage_buffer_pct=option_slippage_buffer_pct,
        min_option_dte=min_option_dte,
        max_stock_notional=max_stock_notional,
        max_stock_stop_loss=max_stock_stop_loss,
        min_group_rows=min_group_rows,
        sample_limit=sample_limit,
    )
    written = write_execution_readiness_markdown(output, result, filters=filters) if output else None
    if json_output_path is not None:
        json_output_path = json_output_path.expanduser().resolve()
        json_output_path.parent.mkdir(parents=True, exist_ok=True)
        json_output_path.write_text(
            json.dumps({"result": result, "filters": filters, "output": str(written) if written else None}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    if json_output:
        print(
            json.dumps(
                {
                    "result": result,
                    "filters": filters,
                    "output": str(written) if written else None,
                    "json_output": str(json_output_path) if json_output_path else None,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return
    print(execution_readiness_markdown(result, filters=filters))
    if written:
        print(f"\nMarkdown report written: {written}")
    if json_output_path:
        print(f"JSON report written: {json_output_path}")


def command_replay_score_mock_portfolio_readiness(
    readiness_json_path: Path,
    mock_account_equity: float,
    mock_cash: float,
    max_single_trade_risk_pct: float,
    max_total_open_risk_pct: float,
    max_gross_notional_pct: float,
    max_option_risk_pct: float,
    max_short_notional_pct: float,
    max_symbol_risk_pct: float,
    max_approved_tickets: int,
    sample_limit: int,
    output: Path | None = None,
    json_output_path: Path | None = None,
    json_output: bool = False,
) -> None:
    from recorder.options_analysis import (
        build_mock_portfolio_readiness_report,
        mock_portfolio_readiness_markdown,
        write_mock_portfolio_readiness_markdown,
    )

    readiness_json_path = readiness_json_path.expanduser().resolve()
    readiness_payload = json.loads(readiness_json_path.read_text(encoding="utf-8"))
    filters = {
        "readiness_json": str(readiness_json_path),
        "mock_account_equity": mock_account_equity,
        "mock_cash": mock_cash,
        "max_single_trade_risk_pct": max_single_trade_risk_pct,
        "max_total_open_risk_pct": max_total_open_risk_pct,
        "max_gross_notional_pct": max_gross_notional_pct,
        "max_option_risk_pct": max_option_risk_pct,
        "max_short_notional_pct": max_short_notional_pct,
        "max_symbol_risk_pct": max_symbol_risk_pct,
        "max_approved_tickets": max_approved_tickets,
        "sample_limit": sample_limit,
    }
    result = build_mock_portfolio_readiness_report(
        readiness_payload,
        mock_account_equity=mock_account_equity,
        mock_cash=mock_cash,
        max_single_trade_risk_pct=max_single_trade_risk_pct,
        max_total_open_risk_pct=max_total_open_risk_pct,
        max_gross_notional_pct=max_gross_notional_pct,
        max_option_risk_pct=max_option_risk_pct,
        max_short_notional_pct=max_short_notional_pct,
        max_symbol_risk_pct=max_symbol_risk_pct,
        max_approved_tickets=max_approved_tickets,
        sample_limit=sample_limit,
    )
    written = write_mock_portfolio_readiness_markdown(output, result, filters=filters) if output else None
    if json_output_path is not None:
        json_output_path = json_output_path.expanduser().resolve()
        json_output_path.parent.mkdir(parents=True, exist_ok=True)
        json_output_path.write_text(
            json.dumps({"result": result, "filters": filters, "output": str(written) if written else None}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    if json_output:
        print(
            json.dumps(
                {
                    "result": result,
                    "filters": filters,
                    "output": str(written) if written else None,
                    "json_output": str(json_output_path) if json_output_path else None,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return
    print(mock_portfolio_readiness_markdown(result, filters=filters))
    if written:
        print(f"\nMarkdown report written: {written}")
    if json_output_path:
        print(f"JSON report written: {json_output_path}")


def command_replay_score_account_backed_portfolio_readiness(
    readiness_json_path: Path,
    account_snapshot_json_path: Path,
    equity_haircut_pct: float,
    cash_haircut_pct: float,
    max_single_trade_risk_pct: float,
    max_total_open_risk_pct: float,
    max_gross_notional_pct: float,
    max_option_risk_pct: float,
    max_short_notional_pct: float,
    max_symbol_risk_pct: float,
    max_approved_tickets: int,
    sample_limit: int,
    output: Path | None = None,
    json_output_path: Path | None = None,
    json_output: bool = False,
) -> None:
    from recorder.options_analysis import (
        account_backed_portfolio_readiness_markdown,
        build_account_backed_portfolio_readiness_report,
        write_account_backed_portfolio_readiness_markdown,
    )

    readiness_json_path = readiness_json_path.expanduser().resolve()
    account_snapshot_json_path = account_snapshot_json_path.expanduser().resolve()
    readiness_payload = json.loads(readiness_json_path.read_text(encoding="utf-8"))
    account_snapshot = json.loads(account_snapshot_json_path.read_text(encoding="utf-8"))
    filters = {
        "readiness_json": str(readiness_json_path),
        "account_snapshot_json": str(account_snapshot_json_path),
        "equity_haircut_pct": equity_haircut_pct,
        "cash_haircut_pct": cash_haircut_pct,
        "max_single_trade_risk_pct": max_single_trade_risk_pct,
        "max_total_open_risk_pct": max_total_open_risk_pct,
        "max_gross_notional_pct": max_gross_notional_pct,
        "max_option_risk_pct": max_option_risk_pct,
        "max_short_notional_pct": max_short_notional_pct,
        "max_symbol_risk_pct": max_symbol_risk_pct,
        "max_approved_tickets": max_approved_tickets,
        "sample_limit": sample_limit,
    }
    result = build_account_backed_portfolio_readiness_report(
        readiness_payload,
        account_snapshot,
        equity_haircut_pct=equity_haircut_pct,
        cash_haircut_pct=cash_haircut_pct,
        max_single_trade_risk_pct=max_single_trade_risk_pct,
        max_total_open_risk_pct=max_total_open_risk_pct,
        max_gross_notional_pct=max_gross_notional_pct,
        max_option_risk_pct=max_option_risk_pct,
        max_short_notional_pct=max_short_notional_pct,
        max_symbol_risk_pct=max_symbol_risk_pct,
        max_approved_tickets=max_approved_tickets,
        sample_limit=sample_limit,
    )
    written = write_account_backed_portfolio_readiness_markdown(output, result, filters=filters) if output else None
    if json_output_path is not None:
        json_output_path = json_output_path.expanduser().resolve()
        json_output_path.parent.mkdir(parents=True, exist_ok=True)
        json_output_path.write_text(
            json.dumps({"result": result, "filters": filters, "output": str(written) if written else None}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    if json_output:
        print(
            json.dumps(
                {
                    "result": result,
                    "filters": filters,
                    "output": str(written) if written else None,
                    "json_output": str(json_output_path) if json_output_path else None,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return
    print(account_backed_portfolio_readiness_markdown(result, filters=filters))
    if written:
        print(f"\nMarkdown report written: {written}")
    if json_output_path:
        print(f"JSON report written: {json_output_path}")


def command_replay_score_account_scaled_position_sizing(
    readiness_json_path: Path,
    account_snapshot_json_path: Path,
    equity_haircut_pct: float,
    cash_haircut_pct: float,
    max_single_trade_risk_pct: float,
    max_total_open_risk_pct: float,
    max_gross_notional_pct: float,
    max_option_risk_pct: float,
    max_short_notional_pct: float,
    max_symbol_risk_pct: float,
    max_stock_notional_pct: float,
    max_cash_per_trade_pct: float,
    sizing_safety_buffer_pct: float,
    allow_fractional_shares: bool,
    min_stock_quantity: float,
    stock_quantity_increment: float,
    max_approved_tickets: int,
    sample_limit: int,
    output: Path | None = None,
    json_output_path: Path | None = None,
    json_output: bool = False,
) -> None:
    from recorder.options_analysis import (
        account_scaled_position_sizing_markdown,
        build_account_scaled_position_sizing_report,
        write_account_scaled_position_sizing_markdown,
    )

    readiness_json_path = readiness_json_path.expanduser().resolve()
    account_snapshot_json_path = account_snapshot_json_path.expanduser().resolve()
    readiness_payload = json.loads(readiness_json_path.read_text(encoding="utf-8"))
    account_snapshot = json.loads(account_snapshot_json_path.read_text(encoding="utf-8"))
    filters = {
        "readiness_json": str(readiness_json_path),
        "account_snapshot_json": str(account_snapshot_json_path),
        "equity_haircut_pct": equity_haircut_pct,
        "cash_haircut_pct": cash_haircut_pct,
        "max_single_trade_risk_pct": max_single_trade_risk_pct,
        "max_total_open_risk_pct": max_total_open_risk_pct,
        "max_gross_notional_pct": max_gross_notional_pct,
        "max_option_risk_pct": max_option_risk_pct,
        "max_short_notional_pct": max_short_notional_pct,
        "max_symbol_risk_pct": max_symbol_risk_pct,
        "max_stock_notional_pct": max_stock_notional_pct,
        "max_cash_per_trade_pct": max_cash_per_trade_pct,
        "sizing_safety_buffer_pct": sizing_safety_buffer_pct,
        "allow_fractional_shares": allow_fractional_shares,
        "min_stock_quantity": min_stock_quantity,
        "stock_quantity_increment": stock_quantity_increment,
        "max_approved_tickets": max_approved_tickets,
        "sample_limit": sample_limit,
    }
    result = build_account_scaled_position_sizing_report(
        readiness_payload,
        account_snapshot,
        equity_haircut_pct=equity_haircut_pct,
        cash_haircut_pct=cash_haircut_pct,
        max_single_trade_risk_pct=max_single_trade_risk_pct,
        max_total_open_risk_pct=max_total_open_risk_pct,
        max_gross_notional_pct=max_gross_notional_pct,
        max_option_risk_pct=max_option_risk_pct,
        max_short_notional_pct=max_short_notional_pct,
        max_symbol_risk_pct=max_symbol_risk_pct,
        max_stock_notional_pct=max_stock_notional_pct,
        max_cash_per_trade_pct=max_cash_per_trade_pct,
        sizing_safety_buffer_pct=sizing_safety_buffer_pct,
        allow_fractional_shares=allow_fractional_shares,
        min_stock_quantity=min_stock_quantity,
        stock_quantity_increment=stock_quantity_increment,
        max_approved_tickets=max_approved_tickets,
        sample_limit=sample_limit,
    )
    written = write_account_scaled_position_sizing_markdown(output, result, filters=filters) if output else None
    if json_output_path is not None:
        json_output_path = json_output_path.expanduser().resolve()
        json_output_path.parent.mkdir(parents=True, exist_ok=True)
        json_output_path.write_text(
            json.dumps({"result": result, "filters": filters, "output": str(written) if written else None}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    if json_output:
        print(
            json.dumps(
                {
                    "result": result,
                    "filters": filters,
                    "output": str(written) if written else None,
                    "json_output": str(json_output_path) if json_output_path else None,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return
    print(account_scaled_position_sizing_markdown(result, filters=filters))
    if written:
        print(f"\nMarkdown report written: {written}")
    if json_output_path:
        print(f"JSON report written: {json_output_path}")


def command_replay_score_order_preview(
    sizing_json_path: Path,
    max_preview_items: int,
    include_human_review: bool,
    sample_limit: int,
    output: Path | None = None,
    json_output_path: Path | None = None,
    json_output: bool = False,
) -> None:
    from recorder.options_analysis import (
        build_order_preview_report,
        order_preview_markdown,
        write_order_preview_markdown,
    )

    sizing_json_path = sizing_json_path.expanduser().resolve()
    sizing_payload = json.loads(sizing_json_path.read_text(encoding="utf-8"))
    filters = {
        "sizing_json": str(sizing_json_path),
        "max_preview_items": max_preview_items,
        "include_human_review": include_human_review,
        "sample_limit": sample_limit,
    }
    result = build_order_preview_report(
        sizing_payload,
        max_preview_items=max_preview_items,
        include_human_review=include_human_review,
        sample_limit=sample_limit,
    )
    written = write_order_preview_markdown(output, result, filters=filters) if output else None
    if json_output_path is not None:
        json_output_path = json_output_path.expanduser().resolve()
        json_output_path.parent.mkdir(parents=True, exist_ok=True)
        json_output_path.write_text(
            json.dumps({"result": result, "filters": filters, "output": str(written) if written else None}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    if json_output:
        print(
            json.dumps(
                {
                    "result": result,
                    "filters": filters,
                    "output": str(written) if written else None,
                    "json_output": str(json_output_path) if json_output_path else None,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return
    print(order_preview_markdown(result, filters=filters))
    if written:
        print(f"\nMarkdown report written: {written}")
    if json_output_path:
        print(f"JSON report written: {json_output_path}")


def command_replay_score_auto_execution_gate(
    preview_json_path: Path,
    broker_whatif_json_path: Path | None,
    short_availability_json_path: Path | None,
    evidence_preflight_json_path: Path | None,
    market_data_risk_gate_json_path: Path | None,
    allow_fractional_shares: bool,
    allow_short_stock: bool,
    require_short_availability_for_shorts: bool,
    require_broker_whatif: bool,
    max_auto_orders: int,
    min_cash_buffer: float,
    max_local_initial_margin_total: float | None,
    allowed_ticket_types: str,
    allowed_actions: str,
    sample_limit: int,
    output: Path | None = None,
    json_output_path: Path | None = None,
    json_output: bool = False,
) -> None:
    from recorder.options_analysis import (
        autonomous_execution_gate_markdown,
        build_autonomous_execution_gate_report,
        write_autonomous_execution_gate_markdown,
    )

    preview_json_path = preview_json_path.expanduser().resolve()
    preview_payload = json.loads(preview_json_path.read_text(encoding="utf-8"))
    broker_payload = None
    if broker_whatif_json_path is not None:
        broker_whatif_json_path = broker_whatif_json_path.expanduser().resolve()
        broker_payload = json.loads(broker_whatif_json_path.read_text(encoding="utf-8"))
    short_availability_payload = None
    if short_availability_json_path is not None:
        short_availability_json_path = short_availability_json_path.expanduser().resolve()
        short_availability_payload = json.loads(short_availability_json_path.read_text(encoding="utf-8"))
    evidence_preflight_payload = None
    if evidence_preflight_json_path is not None:
        evidence_preflight_json_path = evidence_preflight_json_path.expanduser().resolve()
        evidence_preflight_payload = json.loads(evidence_preflight_json_path.read_text(encoding="utf-8"))
    market_data_risk_gate_payload = None
    if market_data_risk_gate_json_path is not None:
        market_data_risk_gate_json_path = market_data_risk_gate_json_path.expanduser().resolve()
        market_data_risk_gate_payload = json.loads(market_data_risk_gate_json_path.read_text(encoding="utf-8"))
    ticket_types = tuple(item.strip() for item in allowed_ticket_types.split(",") if item.strip())
    actions = tuple(item.strip() for item in allowed_actions.split(",") if item.strip())
    filters = {
        "preview_json": str(preview_json_path),
        "broker_whatif_json": str(broker_whatif_json_path) if broker_whatif_json_path else None,
        "short_availability_json": str(short_availability_json_path) if short_availability_json_path else None,
        "evidence_preflight_json": str(evidence_preflight_json_path) if evidence_preflight_json_path else None,
        "market_data_risk_gate_json": str(market_data_risk_gate_json_path) if market_data_risk_gate_json_path else None,
        "allow_fractional_shares": allow_fractional_shares,
        "allow_short_stock": allow_short_stock,
        "require_short_availability_for_shorts": require_short_availability_for_shorts,
        "require_broker_whatif": require_broker_whatif,
        "max_auto_orders": max_auto_orders,
        "min_cash_buffer": min_cash_buffer,
        "max_local_initial_margin_total": max_local_initial_margin_total,
        "allowed_ticket_types": allowed_ticket_types,
        "allowed_actions": allowed_actions,
        "sample_limit": sample_limit,
    }
    result = build_autonomous_execution_gate_report(
        preview_payload,
        allow_fractional_shares=allow_fractional_shares,
        allow_short_stock=allow_short_stock,
        require_short_availability_for_shorts=require_short_availability_for_shorts,
        short_availability_payload=short_availability_payload,
        require_broker_whatif=require_broker_whatif,
        broker_whatif_payload=broker_payload,
        max_auto_orders=max_auto_orders,
        min_cash_buffer=min_cash_buffer,
        max_local_initial_margin_total=max_local_initial_margin_total,
        allowed_ticket_types=ticket_types,
        allowed_actions=actions,
        evidence_preflight_payload=evidence_preflight_payload,
        market_data_risk_gate_payload=market_data_risk_gate_payload,
        sample_limit=sample_limit,
    )
    written = write_autonomous_execution_gate_markdown(output, result, filters=filters) if output else None
    if json_output_path is not None:
        json_output_path = json_output_path.expanduser().resolve()
        json_output_path.parent.mkdir(parents=True, exist_ok=True)
        json_output_path.write_text(
            json.dumps({"result": result, "filters": filters, "output": str(written) if written else None}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    if json_output:
        print(
            json.dumps(
                {
                    "result": result,
                    "filters": filters,
                    "output": str(written) if written else None,
                    "json_output": str(json_output_path) if json_output_path else None,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return
    print(autonomous_execution_gate_markdown(result, filters=filters))
    if written:
        print(f"\nMarkdown report written: {written}")
    if json_output_path:
        print(f"JSON report written: {json_output_path}")


def _read_json_if_exists(path: Path | None, *, required: bool = False) -> dict[str, Any] | None:
    if path is None:
        if required:
            raise FileNotFoundError("Required JSON path was not provided")
        return None
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        if required:
            raise FileNotFoundError(str(resolved))
        return None
    return json.loads(resolved.read_text(encoding="utf-8"))


def _write_json_envelope(path: Path | None, payload: dict[str, Any]) -> Path | None:
    if path is None:
        return None
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return resolved


def paper_capital_aware_execution_markdown(result: dict[str, Any]) -> str:
    ledger = result.get("ledger") or {}
    router = result.get("router") or {}
    filters = result.get("filters") or {}
    lines = [
        "# M242 Paper Capital-Aware Execution",
        "",
        f"- Generated: `{result.get('generated_at')}`",
        f"- Mode: `{result.get('mode')}`",
        f"- Capital-aware source: required `{result.get('require_capital_aware_gate')}` / source_has `{ledger.get('source_has_capital_aware_gate')}`",
        f"- Ledger intents / pending: `{ledger.get('intent_count')}` / `{ledger.get('pending_paper_execution_count')}`",
        f"- Ledger capital source ready/resized/blocked: `{ledger.get('source_capital_ready_count')}` / `{ledger.get('source_capital_resized_count')}` / `{ledger.get('source_capital_blocked_count')}`",
        f"- Router paper orders / fills / positions: `{router.get('paper_order_count')}` / `{router.get('paper_fill_count')}` / `{router.get('paper_position_count')}`",
        f"- Router skipped counts: `{router.get('skipped_counts') or {}}`",
        f"- Gross fill notional: `{((router.get('totals') or {}).get('gross_fill_notional'))}`",
        "- Safety: paper-only simulated execution; no broker order submission, no transmit, no live routing.",
        "",
        "## Inputs",
        "",
    ]
    for key, value in filters.items():
        lines.append(f"- `{key}`: `{value if value is not None else '-'}`")
    lines.extend(["", "## Ledger state counts", "", "| State | Count |", "| --- | ---: |"])
    for key, value in sorted((ledger.get("intent_state_counts") or {}).items(), key=lambda item: (-int(item[1]), item[0])):
        lines.append(f"| `{key}` | {value} |")
    if not ledger.get("intent_state_counts"):
        lines.append("| - | 0 |")
    lines.extend(["", "## Router order/skipped counts", "", "| Bucket | Count |", "| --- | ---: |"])
    combined_counts = {
        **{f"order:{key}": value for key, value in (router.get("order_state_counts") or {}).items()},
        **{f"skip:{key}": value for key, value in (router.get("skipped_counts") or {}).items()},
    }
    for key, value in sorted(combined_counts.items(), key=lambda item: (-int(item[1]), item[0])):
        lines.append(f"| `{key}` | {value} |")
    if not combined_counts:
        lines.append("| - | 0 |")
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "M242 is the paper execution path that consumes M239 capital-aware candidates. Oversized or capital-blocked candidates do not become pending paper execution. The paper router additionally requires the capital-aware ledger before producing simulated fills.",
        ]
    )
    return "\n".join(lines) + "\n"


def command_replay_score_paper_capital_aware_execution(
    capital_aware_gate_json_path: Path,
    existing_ledger_json_path: Path | None,
    portfolio_risk_state_json_path: Path | None,
    session_transition_release_governor_json_path: Path | None,
    existing_router_json_path: Path | None,
    marks_json_path: Path | None,
    operating_auto_gate_json_path: Path | None,
    monitor_size_multiplier: float,
    max_intents: int,
    max_orders: int,
    fill_price_source: str,
    fill_model: str,
    slippage_bps: float,
    commission_per_order: float,
    ledger_run_id: str | None,
    router_run_id: str | None,
    sample_limit: int,
    output: Path | None = None,
    json_output_path: Path | None = None,
    ledger_output: Path | None = None,
    ledger_json_output_path: Path | None = None,
    router_output: Path | None = None,
    router_json_output_path: Path | None = None,
    json_output: bool = False,
) -> None:
    from datetime import UTC, datetime

    from recorder.options_analysis import (
        build_execution_intent_ledger_report,
        build_paper_order_router_report,
        write_execution_intent_ledger_markdown,
        write_paper_order_router_markdown,
    )

    capital_aware_gate_json_path = capital_aware_gate_json_path.expanduser().resolve()
    capital_aware_payload = _read_json_if_exists(capital_aware_gate_json_path, required=True) or {}
    existing_ledger_payload = _read_json_if_exists(existing_ledger_json_path)
    portfolio_risk_payload = _read_json_if_exists(portfolio_risk_state_json_path)
    session_governor_payload = _read_json_if_exists(session_transition_release_governor_json_path)
    existing_router_payload = _read_json_if_exists(existing_router_json_path)
    marks_payload = _read_json_if_exists(marks_json_path)
    operating_auto_gate_payload = _read_json_if_exists(operating_auto_gate_json_path)
    filters = {
        "capital_aware_gate_json": str(capital_aware_gate_json_path),
        "existing_ledger_json": str(existing_ledger_json_path.expanduser().resolve()) if existing_ledger_json_path else None,
        "portfolio_risk_state_json": str(portfolio_risk_state_json_path.expanduser().resolve()) if portfolio_risk_state_json_path else None,
        "session_transition_release_governor_json": str(session_transition_release_governor_json_path.expanduser().resolve()) if session_transition_release_governor_json_path else None,
        "existing_router_json": str(existing_router_json_path.expanduser().resolve()) if existing_router_json_path else None,
        "marks_json": str(marks_json_path.expanduser().resolve()) if marks_json_path else None,
        "operating_auto_gate_json": str(operating_auto_gate_json_path.expanduser().resolve()) if operating_auto_gate_json_path else None,
        "require_capital_aware_gate": True,
        "require_portfolio_risk_gate": True,
        "require_operating_preflight": True,
        "require_session_transition_release_governor": True,
        "max_intents": max_intents,
        "max_orders": max_orders,
        "fill_price_source": fill_price_source,
    }
    ledger = build_execution_intent_ledger_report(
        capital_aware_payload,
        existing_ledger_payload=existing_ledger_payload,
        portfolio_risk_state_payload=portfolio_risk_payload,
        session_transition_release_governor_payload=session_governor_payload,
        require_capital_aware_gate=True,
        monitor_size_multiplier=monitor_size_multiplier,
        max_intents=max_intents,
        ledger_run_id=ledger_run_id,
        sample_limit=sample_limit,
    )
    ledger_written = write_execution_intent_ledger_markdown(ledger_output, ledger, filters=filters) if ledger_output else None
    ledger_json_written = _write_json_envelope(
        ledger_json_output_path,
        {"result": ledger, "filters": filters, "output": str(ledger_written) if ledger_written else None},
    )
    router = build_paper_order_router_report(
        {"result": ledger},
        existing_router_payload=existing_router_payload,
        marks_payload=marks_payload,
        operating_auto_gate_payload=operating_auto_gate_payload,
        session_transition_release_governor_payload=session_governor_payload,
        require_operating_preflight=True,
        allow_non_operating_source_ledger=False,
        require_session_transition_release_governor=True,
        fill_price_source=fill_price_source,
        fill_model=fill_model,
        require_capital_aware_gate=True,
        require_portfolio_risk_gate=True,
        max_orders=max_orders,
        slippage_bps=slippage_bps,
        commission_per_order=commission_per_order,
        router_run_id=router_run_id,
        sample_limit=sample_limit,
    )
    router_written = write_paper_order_router_markdown(router_output, router, filters=filters) if router_output else None
    router_json_written = _write_json_envelope(
        router_json_output_path,
        {"result": router, "filters": filters, "output": str(router_written) if router_written else None},
    )
    result = {
        "paper_capital_aware_execution_version": "m242-paper-capital-aware-execution-v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "mode": "paper_capital_aware_execution",
        "require_capital_aware_gate": True,
        "filters": filters,
        "ledger": ledger,
        "router": router,
        "artifacts": {
            "ledger_markdown": str(ledger_written) if ledger_written else None,
            "ledger_json": str(ledger_json_written) if ledger_json_written else None,
            "router_markdown": str(router_written) if router_written else None,
            "router_json": str(router_json_written) if router_json_written else None,
        },
        "safety": {
            "paper_only": True,
            "ibkr_order_submitted": False,
            "transmit_enabled": False,
            "live_order_routing_enabled": False,
        },
    }
    written = None
    if output:
        output = output.expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(paper_capital_aware_execution_markdown(result), encoding="utf-8")
        written = output
    json_written = _write_json_envelope(json_output_path, {"result": result, "output": str(written) if written else None})
    if json_output:
        print(
            json.dumps(
                {
                    "result": result,
                    "output": str(written) if written else None,
                    "json_output": str(json_written) if json_written else None,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return
    print(paper_capital_aware_execution_markdown(result))
    if written:
        print(f"\nMarkdown report written: {written}")
    if json_written:
        print(f"JSON report written: {json_written}")


def command_replay_score_capital_aware_action_gate(
    auto_gate_json_path: Path,
    capital_state_json_path: Path,
    reduce_to_fit_capacity: bool,
    allow_fractional_shares: bool,
    min_stock_quantity: float,
    stock_quantity_increment: float,
    max_ready_actions: int,
    sample_limit: int,
    output: Path | None = None,
    json_output_path: Path | None = None,
    json_output: bool = False,
) -> None:
    from recorder.capital_aware_action_gate import (
        build_capital_aware_action_gate_report,
        capital_aware_action_gate_markdown,
        write_capital_aware_action_gate_json,
        write_capital_aware_action_gate_markdown,
    )

    def _read_optional_json(path: Path) -> dict[str, Any]:
        resolved = path.expanduser().resolve()
        if not resolved.exists():
            return {}
        return json.loads(resolved.read_text(encoding="utf-8"))

    auto_gate_json_path = auto_gate_json_path.expanduser().resolve()
    capital_state_json_path = capital_state_json_path.expanduser().resolve()
    auto_gate_payload = _read_optional_json(auto_gate_json_path)
    capital_state_payload = _read_optional_json(capital_state_json_path)
    filters = {
        "auto_gate_json": str(auto_gate_json_path),
        "capital_state_json": str(capital_state_json_path),
        "reduce_to_fit_capacity": reduce_to_fit_capacity,
        "allow_fractional_shares": allow_fractional_shares,
        "min_stock_quantity": min_stock_quantity,
        "stock_quantity_increment": stock_quantity_increment,
        "max_ready_actions": max_ready_actions,
        "sample_limit": sample_limit,
    }
    result = build_capital_aware_action_gate_report(
        auto_gate_payload,
        capital_state_payload,
        reduce_to_fit_capacity=reduce_to_fit_capacity,
        allow_fractional_shares=allow_fractional_shares,
        min_stock_quantity=min_stock_quantity,
        stock_quantity_increment=stock_quantity_increment,
        max_ready_actions=max_ready_actions,
        sample_limit=sample_limit,
    )
    written = write_capital_aware_action_gate_markdown(output, result) if output else None
    if json_output_path is not None:
        write_capital_aware_action_gate_json(json_output_path, result)
    if json_output:
        print(
            json.dumps(
                {
                    "result": result,
                    "filters": filters,
                    "output": str(written) if written else None,
                    "json_output": str(json_output_path) if json_output_path else None,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return
    print(capital_aware_action_gate_markdown(result))
    if written:
        print(f"\nMarkdown report written: {written}")
    if json_output_path:
        print(f"JSON report written: {json_output_path}")


def command_replay_score_execution_intent_ledger(
    auto_gate_json_path: Path,
    existing_ledger_json_path: Path | None,
    portfolio_risk_state_json_path: Path | None,
    session_transition_release_governor_json_path: Path | None,
    require_capital_aware_gate: bool,
    monitor_size_multiplier: float,
    max_intents: int,
    ledger_run_id: str | None,
    sample_limit: int,
    output: Path | None = None,
    json_output_path: Path | None = None,
    json_output: bool = False,
) -> None:
    from recorder.options_analysis import (
        build_execution_intent_ledger_report,
        execution_intent_ledger_markdown,
        write_execution_intent_ledger_markdown,
    )

    auto_gate_json_path = auto_gate_json_path.expanduser().resolve()
    auto_gate_payload = json.loads(auto_gate_json_path.read_text(encoding="utf-8"))
    existing_payload = None
    if existing_ledger_json_path is not None:
        existing_ledger_json_path = existing_ledger_json_path.expanduser().resolve()
        existing_payload = json.loads(existing_ledger_json_path.read_text(encoding="utf-8"))
    portfolio_risk_state_payload = None
    if portfolio_risk_state_json_path is not None:
        portfolio_risk_state_json_path = portfolio_risk_state_json_path.expanduser().resolve()
        portfolio_risk_state_payload = json.loads(portfolio_risk_state_json_path.read_text(encoding="utf-8"))
    session_transition_release_governor_payload = None
    if session_transition_release_governor_json_path is not None:
        session_transition_release_governor_json_path = session_transition_release_governor_json_path.expanduser().resolve()
        if session_transition_release_governor_json_path.exists():
            session_transition_release_governor_payload = json.loads(session_transition_release_governor_json_path.read_text(encoding="utf-8"))
    filters = {
        "auto_gate_json": str(auto_gate_json_path),
        "existing_ledger_json": str(existing_ledger_json_path) if existing_ledger_json_path else None,
        "portfolio_risk_state_json": str(portfolio_risk_state_json_path) if portfolio_risk_state_json_path else None,
        "session_transition_release_governor_json": str(session_transition_release_governor_json_path) if session_transition_release_governor_json_path else None,
        "require_capital_aware_gate": require_capital_aware_gate,
        "monitor_size_multiplier": monitor_size_multiplier,
        "max_intents": max_intents,
        "ledger_run_id": ledger_run_id,
        "sample_limit": sample_limit,
    }
    result = build_execution_intent_ledger_report(
        auto_gate_payload,
        existing_ledger_payload=existing_payload,
        portfolio_risk_state_payload=portfolio_risk_state_payload,
        session_transition_release_governor_payload=session_transition_release_governor_payload,
        require_capital_aware_gate=require_capital_aware_gate,
        monitor_size_multiplier=monitor_size_multiplier,
        max_intents=max_intents,
        ledger_run_id=ledger_run_id,
        sample_limit=sample_limit,
    )
    written = write_execution_intent_ledger_markdown(output, result, filters=filters) if output else None
    if json_output_path is not None:
        json_output_path = json_output_path.expanduser().resolve()
        json_output_path.parent.mkdir(parents=True, exist_ok=True)
        json_output_path.write_text(
            json.dumps({"result": result, "filters": filters, "output": str(written) if written else None}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    if json_output:
        print(
            json.dumps(
                {
                    "result": result,
                    "filters": filters,
                    "output": str(written) if written else None,
                    "json_output": str(json_output_path) if json_output_path else None,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return
    print(execution_intent_ledger_markdown(result, filters=filters))
    if written:
        print(f"\nMarkdown report written: {written}")
    if json_output_path:
        print(f"JSON report written: {json_output_path}")
