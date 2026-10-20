from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from recorder.config import Settings
from .cli_paper_cycle_helpers import (
    _parse_path_list,
    _write_component_json,
    _write_component_json_atomic,
    _load_operating_auto_gate_payload,
    _payload_result,
    _path_from_text,
    _write_markdown_text_atomic,
    _build_watch_evidence_preflight_auto_gate,
    _build_watch_market_data_risk_auto_gate,
    _run_watch_evidence_refresh_watchdog,
    _latency_budget_stage_policy,
    _latency_budget_artifact_skip,
    _latency_budget_read_json,
    _run_dashboard_source_ingest_health,
    _watched_file_fingerprint,
    _run_dashboard_market_data_session_overlay,
    _run_dashboard_market_data_risk_gate,
    _run_dashboard_session_transition_closed_loop,
    _run_dashboard_session_transition_release_governor,
    _run_dashboard_release_governed_recovery_closed_loop,
    _paper_watch_input_snapshot,
    _parse_hhmm,
    _session_minutes,
    _parse_session_gate_now,
    _load_session_gate_calendar,
    _session_calendar_date_map,
    _session_calendar_early_close_map,
    _evaluate_paper_session_gate,
    _parse_profile_symbol_list,
    _mark_refresh_symbols_from_snapshot,
    _freshened_mark_snapshot_payload,
    _parse_utc_datetime,
    _mark_snapshot_age_seconds,
    _mark_refresh_snapshot_summary,
    _mark_refresh_apply_partial_fallback,
    _evaluate_mark_refresh_policy,
    _mark_refresh_quality_execution_action,
    _apply_mark_refresh_quality_action_to_portfolio_risk,
    _write_json_to_targets,
    _run_paper_mark_refresh,
)

def command_replay_score_paper_cycle_runner(
    auto_gate_json_path: Path,
    portfolio_risk_state_json_path: Path,
    fill_marks_json_path: Path,
    loop_marks_jsons_value: str,
    existing_ledger_json_path: Path | None,
    existing_router_json_path: Path | None,
    exit_router_json_path: Path | None,
    session_transition_release_governor_json_path: Path | None,
    monitor_size_multiplier: float,
    max_intents: int,
    fill_price_source: str,
    fill_model: str,
    max_orders: int,
    slippage_bps: float,
    commission_per_order: float,
    take_profit_pct: float,
    stop_loss_pct: float,
    max_loss_dollars: float | None,
    max_gross_exposure: float,
    max_symbol_exposure: float,
    max_short_exposure: float,
    max_unrealized_loss: float,
    max_total_loss: float,
    max_open_symbols: int,
    warning_ratio: float,
    cycle_run_id: str | None,
    sample_limit: int,
    output: Path | None = None,
    json_output_path: Path | None = None,
    entry_ledger_json_output_path: Path | None = None,
    entry_router_json_output_path: Path | None = None,
    risk_loop_json_output_path: Path | None = None,
    next_risk_json_output_path: Path | None = None,
    json_output: bool = False,
    operating_auto_gate_json_path: Path | None = None,
    require_operating_preflight: bool = False,
    allow_non_operating_source_ledger: bool = False,
    require_session_transition_release_governor: bool = False,
) -> None:
    from recorder.options_analysis import (
        build_paper_cycle_runner_report,
        paper_cycle_runner_markdown,
        write_paper_cycle_runner_markdown,
    )

    auto_gate_json_path = auto_gate_json_path.expanduser().resolve()
    portfolio_risk_state_json_path = portfolio_risk_state_json_path.expanduser().resolve()
    fill_marks_json_path = fill_marks_json_path.expanduser().resolve()
    auto_gate_payload = json.loads(auto_gate_json_path.read_text(encoding="utf-8"))
    portfolio_risk_state_payload = json.loads(portfolio_risk_state_json_path.read_text(encoding="utf-8"))
    fill_marks_payload = json.loads(fill_marks_json_path.read_text(encoding="utf-8"))
    loop_mark_paths = [path.expanduser().resolve() for path in _parse_path_list(loop_marks_jsons_value)]
    if not loop_mark_paths:
        raise ValueError("--loop-marks-jsons must include at least one mark snapshot JSON path")
    loop_mark_payloads = [json.loads(path.read_text(encoding="utf-8")) for path in loop_mark_paths]
    existing_ledger_payload = None
    if existing_ledger_json_path is not None:
        existing_ledger_json_path = existing_ledger_json_path.expanduser().resolve()
        existing_ledger_payload = json.loads(existing_ledger_json_path.read_text(encoding="utf-8"))
    existing_router_payload = None
    if existing_router_json_path is not None:
        existing_router_json_path = existing_router_json_path.expanduser().resolve()
        existing_router_payload = json.loads(existing_router_json_path.read_text(encoding="utf-8"))
    exit_payload = None
    if exit_router_json_path is not None:
        exit_router_json_path = exit_router_json_path.expanduser().resolve()
        exit_payload = json.loads(exit_router_json_path.read_text(encoding="utf-8"))
    operating_auto_gate_payload, operating_auto_gate_json_path = _load_operating_auto_gate_payload(
        operating_auto_gate_json_path,
        require_operating_preflight=require_operating_preflight,
    )
    session_transition_release_governor_payload = None
    if session_transition_release_governor_json_path is not None:
        session_transition_release_governor_json_path = session_transition_release_governor_json_path.expanduser().resolve()
        if session_transition_release_governor_json_path.exists():
            session_transition_release_governor_payload = json.loads(session_transition_release_governor_json_path.read_text(encoding="utf-8"))
    filters = {
        "auto_gate_json": str(auto_gate_json_path),
        "portfolio_risk_state_json": str(portfolio_risk_state_json_path),
        "fill_marks_json": str(fill_marks_json_path),
        "loop_marks_jsons": ",".join(str(path) for path in loop_mark_paths),
        "existing_ledger_json": str(existing_ledger_json_path) if existing_ledger_json_path else None,
        "existing_router_json": str(existing_router_json_path) if existing_router_json_path else None,
        "exit_router_json": str(exit_router_json_path) if exit_router_json_path else None,
        "operating_auto_gate_json": str(operating_auto_gate_json_path) if operating_auto_gate_json_path else None,
        "session_transition_release_governor_json": str(session_transition_release_governor_json_path) if session_transition_release_governor_json_path else None,
        "require_operating_preflight": require_operating_preflight,
        "allow_non_operating_source_ledger": allow_non_operating_source_ledger,
        "require_session_transition_release_governor": require_session_transition_release_governor,
        "monitor_size_multiplier": monitor_size_multiplier,
        "max_intents": max_intents,
        "fill_price_source": fill_price_source,
        "fill_model": fill_model,
        "max_orders": max_orders,
        "slippage_bps": slippage_bps,
        "commission_per_order": commission_per_order,
        "take_profit_pct": take_profit_pct,
        "stop_loss_pct": stop_loss_pct,
        "max_loss_dollars": max_loss_dollars,
        "max_gross_exposure": max_gross_exposure,
        "max_symbol_exposure": max_symbol_exposure,
        "max_short_exposure": max_short_exposure,
        "max_unrealized_loss": max_unrealized_loss,
        "max_total_loss": max_total_loss,
        "max_open_symbols": max_open_symbols,
        "warning_ratio": warning_ratio,
        "cycle_run_id": cycle_run_id,
        "sample_limit": sample_limit,
    }
    result = build_paper_cycle_runner_report(
        auto_gate_payload,
        portfolio_risk_state_payload,
        fill_marks_payload,
        loop_mark_payloads,
        existing_ledger_payload=existing_ledger_payload,
        existing_router_payload=existing_router_payload,
        exit_router_payload=exit_payload,
        operating_auto_gate_payload=operating_auto_gate_payload,
        session_transition_release_governor_payload=session_transition_release_governor_payload,
        require_operating_preflight=require_operating_preflight,
        allow_non_operating_source_ledger=allow_non_operating_source_ledger,
        require_session_transition_release_governor=require_session_transition_release_governor,
        monitor_size_multiplier=monitor_size_multiplier,
        max_intents=max_intents,
        fill_price_source=fill_price_source,
        fill_model=fill_model,
        max_orders=max_orders,
        slippage_bps=slippage_bps,
        commission_per_order=commission_per_order,
        take_profit_pct=take_profit_pct,
        stop_loss_pct=stop_loss_pct,
        max_loss_dollars=max_loss_dollars,
        max_gross_exposure=max_gross_exposure,
        max_symbol_exposure=max_symbol_exposure,
        max_short_exposure=max_short_exposure,
        max_unrealized_loss=max_unrealized_loss,
        max_total_loss=max_total_loss,
        max_open_symbols=max_open_symbols,
        warning_ratio=warning_ratio,
        cycle_run_id=cycle_run_id,
        sample_limit=sample_limit,
    )
    written = write_paper_cycle_runner_markdown(output, result, filters=filters) if output else None
    component_outputs = {
        "entry_ledger_json_output": str(_write_component_json(entry_ledger_json_output_path, result["entry_ledger"], filters) or ""),
        "entry_router_json_output": str(_write_component_json(entry_router_json_output_path, result["entry_router"], filters) or ""),
        "risk_loop_json_output": str(_write_component_json(risk_loop_json_output_path, result["risk_loop"], filters) or ""),
        "next_risk_json_output": str(_write_component_json(next_risk_json_output_path, result["next_portfolio_risk_state"], filters) or ""),
    }
    if json_output_path is not None:
        json_output_path = json_output_path.expanduser().resolve()
        json_output_path.parent.mkdir(parents=True, exist_ok=True)
        json_output_path.write_text(
            json.dumps(
                {"result": result, "filters": filters, "output": str(written) if written else None, **component_outputs},
                indent=2,
                ensure_ascii=False,
            ),
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
                    **component_outputs,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return
    print(paper_cycle_runner_markdown(result, filters=filters))
    if written:
        print(f"\nMarkdown report written: {written}")
    if json_output_path:
        print(f"JSON report written: {json_output_path}")
    for label, path_text in component_outputs.items():
        if path_text:
            print(f"{label} written: {path_text}")


def command_replay_score_paper_multi_cycle_runner(
    auto_gate_json_path: Path,
    portfolio_risk_state_json_path: Path,
    fill_marks_json_path: Path,
    loop_marks_jsons_value: str,
    existing_ledger_json_path: Path | None,
    existing_router_json_path: Path | None,
    exit_router_json_path: Path | None,
    session_transition_release_governor_json_path: Path | None,
    cycle_count: int,
    dedupe_across_cycles: bool,
    monitor_size_multiplier: float,
    max_intents: int,
    fill_price_source: str,
    fill_model: str,
    max_orders: int,
    slippage_bps: float,
    commission_per_order: float,
    take_profit_pct: float,
    stop_loss_pct: float,
    max_loss_dollars: float | None,
    max_gross_exposure: float,
    max_symbol_exposure: float,
    max_short_exposure: float,
    max_unrealized_loss: float,
    max_total_loss: float,
    max_open_symbols: int,
    warning_ratio: float,
    multi_cycle_run_id: str | None,
    sample_limit: int,
    output: Path | None = None,
    json_output_path: Path | None = None,
    next_risk_json_output_path: Path | None = None,
    json_output: bool = False,
    operating_auto_gate_json_path: Path | None = None,
    require_operating_preflight: bool = False,
    allow_non_operating_source_ledger: bool = False,
    require_session_transition_release_governor: bool = False,
) -> None:
    from recorder.options_analysis import (
        build_paper_multi_cycle_runner_report,
        paper_multi_cycle_runner_markdown,
        write_paper_multi_cycle_runner_markdown,
    )

    auto_gate_json_path = auto_gate_json_path.expanduser().resolve()
    portfolio_risk_state_json_path = portfolio_risk_state_json_path.expanduser().resolve()
    fill_marks_json_path = fill_marks_json_path.expanduser().resolve()
    auto_gate_payload = json.loads(auto_gate_json_path.read_text(encoding="utf-8"))
    portfolio_risk_state_payload = json.loads(portfolio_risk_state_json_path.read_text(encoding="utf-8"))
    fill_marks_payload = json.loads(fill_marks_json_path.read_text(encoding="utf-8"))
    loop_mark_paths = [path.expanduser().resolve() for path in _parse_path_list(loop_marks_jsons_value)]
    if not loop_mark_paths:
        raise ValueError("--loop-marks-jsons must include at least one mark snapshot JSON path")
    loop_mark_payloads = [json.loads(path.read_text(encoding="utf-8")) for path in loop_mark_paths]
    existing_ledger_payload = None
    if existing_ledger_json_path is not None:
        existing_ledger_json_path = existing_ledger_json_path.expanduser().resolve()
        existing_ledger_payload = json.loads(existing_ledger_json_path.read_text(encoding="utf-8"))
    existing_router_payload = None
    if existing_router_json_path is not None:
        existing_router_json_path = existing_router_json_path.expanduser().resolve()
        existing_router_payload = json.loads(existing_router_json_path.read_text(encoding="utf-8"))
    exit_payload = None
    if exit_router_json_path is not None:
        exit_router_json_path = exit_router_json_path.expanduser().resolve()
        exit_payload = json.loads(exit_router_json_path.read_text(encoding="utf-8"))
    operating_auto_gate_payload, operating_auto_gate_json_path = _load_operating_auto_gate_payload(
        operating_auto_gate_json_path,
        require_operating_preflight=require_operating_preflight,
    )
    session_transition_release_governor_payload = None
    if session_transition_release_governor_json_path is not None:
        session_transition_release_governor_json_path = session_transition_release_governor_json_path.expanduser().resolve()
        if session_transition_release_governor_json_path.exists():
            session_transition_release_governor_payload = json.loads(session_transition_release_governor_json_path.read_text(encoding="utf-8"))
    filters = {
        "auto_gate_json": str(auto_gate_json_path),
        "portfolio_risk_state_json": str(portfolio_risk_state_json_path),
        "fill_marks_json": str(fill_marks_json_path),
        "loop_marks_jsons": ",".join(str(path) for path in loop_mark_paths),
        "existing_ledger_json": str(existing_ledger_json_path) if existing_ledger_json_path else None,
        "existing_router_json": str(existing_router_json_path) if existing_router_json_path else None,
        "exit_router_json": str(exit_router_json_path) if exit_router_json_path else None,
        "operating_auto_gate_json": str(operating_auto_gate_json_path) if operating_auto_gate_json_path else None,
        "session_transition_release_governor_json": str(session_transition_release_governor_json_path) if session_transition_release_governor_json_path else None,
        "require_operating_preflight": require_operating_preflight,
        "allow_non_operating_source_ledger": allow_non_operating_source_ledger,
        "require_session_transition_release_governor": require_session_transition_release_governor,
        "cycle_count": cycle_count,
        "dedupe_across_cycles": dedupe_across_cycles,
        "monitor_size_multiplier": monitor_size_multiplier,
        "max_intents": max_intents,
        "fill_price_source": fill_price_source,
        "fill_model": fill_model,
        "max_orders": max_orders,
        "slippage_bps": slippage_bps,
        "commission_per_order": commission_per_order,
        "take_profit_pct": take_profit_pct,
        "stop_loss_pct": stop_loss_pct,
        "max_loss_dollars": max_loss_dollars,
        "max_gross_exposure": max_gross_exposure,
        "max_symbol_exposure": max_symbol_exposure,
        "max_short_exposure": max_short_exposure,
        "max_unrealized_loss": max_unrealized_loss,
        "max_total_loss": max_total_loss,
        "max_open_symbols": max_open_symbols,
        "warning_ratio": warning_ratio,
        "multi_cycle_run_id": multi_cycle_run_id,
        "sample_limit": sample_limit,
    }
    result = build_paper_multi_cycle_runner_report(
        auto_gate_payload,
        portfolio_risk_state_payload,
        fill_marks_payload,
        loop_mark_payloads,
        existing_ledger_payload=existing_ledger_payload,
        existing_router_payload=existing_router_payload,
        exit_router_payload=exit_payload,
        operating_auto_gate_payload=operating_auto_gate_payload,
        session_transition_release_governor_payload=session_transition_release_governor_payload,
        require_operating_preflight=require_operating_preflight,
        allow_non_operating_source_ledger=allow_non_operating_source_ledger,
        require_session_transition_release_governor=require_session_transition_release_governor,
        cycle_count=cycle_count,
        dedupe_across_cycles=dedupe_across_cycles,
        monitor_size_multiplier=monitor_size_multiplier,
        max_intents=max_intents,
        fill_price_source=fill_price_source,
        fill_model=fill_model,
        max_orders=max_orders,
        slippage_bps=slippage_bps,
        commission_per_order=commission_per_order,
        take_profit_pct=take_profit_pct,
        stop_loss_pct=stop_loss_pct,
        max_loss_dollars=max_loss_dollars,
        max_gross_exposure=max_gross_exposure,
        max_symbol_exposure=max_symbol_exposure,
        max_short_exposure=max_short_exposure,
        max_unrealized_loss=max_unrealized_loss,
        max_total_loss=max_total_loss,
        max_open_symbols=max_open_symbols,
        warning_ratio=warning_ratio,
        multi_cycle_run_id=multi_cycle_run_id,
        sample_limit=sample_limit,
    )
    written = write_paper_multi_cycle_runner_markdown(output, result, filters=filters) if output else None
    component_outputs = {
        "next_risk_json_output": str(_write_component_json(next_risk_json_output_path, result["next_portfolio_risk_state"], filters) or ""),
    }
    if json_output_path is not None:
        json_output_path = json_output_path.expanduser().resolve()
        json_output_path.parent.mkdir(parents=True, exist_ok=True)
        json_output_path.write_text(
            json.dumps(
                {"result": result, "filters": filters, "output": str(written) if written else None, **component_outputs},
                indent=2,
                ensure_ascii=False,
            ),
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
                    **component_outputs,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return
    print(paper_multi_cycle_runner_markdown(result, filters=filters))
    if written:
        print(f"\nMarkdown report written: {written}")
    if json_output_path:
        print(f"JSON report written: {json_output_path}")
    for label, path_text in component_outputs.items():
        if path_text:
            print(f"{label} written: {path_text}")


def command_replay_score_paper_cycle_scheduler(
    auto_gate_json_path: Path,
    portfolio_risk_state_json_path: Path,
    fill_marks_json_path: Path,
    loop_marks_jsons_value: str,
    existing_ledger_json_path: Path | None,
    existing_router_json_path: Path | None,
    exit_router_json_path: Path | None,
    resume_checkpoint_json_path: Path | None,
    session_transition_release_governor_json_path: Path | None,
    max_runs: int,
    cycles_per_run: int,
    interval_seconds: float,
    sleep_between_runs: bool,
    dedupe_across_cycles: bool,
    no_carry_dedupe_state_across_runs: bool,
    monitor_size_multiplier: float,
    max_intents: int,
    fill_price_source: str,
    fill_model: str,
    max_orders: int,
    slippage_bps: float,
    commission_per_order: float,
    take_profit_pct: float,
    stop_loss_pct: float,
    max_loss_dollars: float | None,
    max_gross_exposure: float,
    max_symbol_exposure: float,
    max_short_exposure: float,
    max_unrealized_loss: float,
    max_total_loss: float,
    max_open_symbols: int,
    warning_ratio: float,
    scheduler_run_id: str | None,
    sample_limit: int,
    output: Path | None = None,
    json_output_path: Path | None = None,
    next_risk_json_output_path: Path | None = None,
    checkpoint_output_path: Path | None = None,
    json_output: bool = False,
    operating_auto_gate_json_path: Path | None = None,
    require_operating_preflight: bool = False,
    allow_non_operating_source_ledger: bool = False,
    require_session_transition_release_governor: bool = False,
) -> None:
    from recorder.options_analysis import (
        build_paper_cycle_scheduler_checkpoint,
        build_paper_cycle_scheduler_report,
        extract_paper_cycle_scheduler_resume_state,
        paper_cycle_scheduler_markdown,
        write_paper_cycle_scheduler_markdown,
    )

    auto_gate_json_path = auto_gate_json_path.expanduser().resolve()
    portfolio_risk_state_json_path = portfolio_risk_state_json_path.expanduser().resolve()
    fill_marks_json_path = fill_marks_json_path.expanduser().resolve()
    auto_gate_payload = json.loads(auto_gate_json_path.read_text(encoding="utf-8"))
    portfolio_risk_state_payload = json.loads(portfolio_risk_state_json_path.read_text(encoding="utf-8"))
    fill_marks_payload = json.loads(fill_marks_json_path.read_text(encoding="utf-8"))
    loop_mark_paths = [path.expanduser().resolve() for path in _parse_path_list(loop_marks_jsons_value)]
    if not loop_mark_paths:
        raise ValueError("--loop-marks-jsons must include at least one mark snapshot JSON path")
    loop_mark_payloads = [json.loads(path.read_text(encoding="utf-8")) for path in loop_mark_paths]
    resume_checkpoint_payload = None
    resume_state: dict[str, Any] = {}
    if resume_checkpoint_json_path is not None:
        resume_checkpoint_json_path = resume_checkpoint_json_path.expanduser().resolve()
        resume_checkpoint_payload = json.loads(resume_checkpoint_json_path.read_text(encoding="utf-8"))
        resume_state = extract_paper_cycle_scheduler_resume_state(resume_checkpoint_payload)
        if not resume_state:
            raise ValueError("--resume-checkpoint-json did not contain a valid M130 paper cycle scheduler resume state")
        portfolio_risk_state_payload = resume_state.get("next_portfolio_risk_state") or portfolio_risk_state_payload
    existing_ledger_payload = None
    if existing_ledger_json_path is not None:
        existing_ledger_json_path = existing_ledger_json_path.expanduser().resolve()
        existing_ledger_payload = json.loads(existing_ledger_json_path.read_text(encoding="utf-8"))
    elif resume_state:
        existing_ledger_payload = resume_state.get("existing_ledger")
    existing_router_payload = None
    if existing_router_json_path is not None:
        existing_router_json_path = existing_router_json_path.expanduser().resolve()
        existing_router_payload = json.loads(existing_router_json_path.read_text(encoding="utf-8"))
    elif resume_state:
        existing_router_payload = resume_state.get("existing_router")
    exit_payload = None
    if exit_router_json_path is not None:
        exit_router_json_path = exit_router_json_path.expanduser().resolve()
        exit_payload = json.loads(exit_router_json_path.read_text(encoding="utf-8"))
    operating_auto_gate_payload, operating_auto_gate_json_path = _load_operating_auto_gate_payload(
        operating_auto_gate_json_path,
        require_operating_preflight=require_operating_preflight,
    )
    session_transition_release_governor_payload = None
    if session_transition_release_governor_json_path is not None:
        session_transition_release_governor_json_path = session_transition_release_governor_json_path.expanduser().resolve()
        if session_transition_release_governor_json_path.exists():
            session_transition_release_governor_payload = json.loads(session_transition_release_governor_json_path.read_text(encoding="utf-8"))
    filters = {
        "auto_gate_json": str(auto_gate_json_path),
        "portfolio_risk_state_json": str(portfolio_risk_state_json_path),
        "fill_marks_json": str(fill_marks_json_path),
        "loop_marks_jsons": ",".join(str(path) for path in loop_mark_paths),
        "existing_ledger_json": str(existing_ledger_json_path) if existing_ledger_json_path else None,
        "existing_router_json": str(existing_router_json_path) if existing_router_json_path else None,
        "exit_router_json": str(exit_router_json_path) if exit_router_json_path else None,
        "resume_checkpoint_json": str(resume_checkpoint_json_path) if resume_checkpoint_json_path else None,
        "resume_prior_runs_completed": resume_state.get("runs_completed_total") if resume_state else 0,
        "operating_auto_gate_json": str(operating_auto_gate_json_path) if operating_auto_gate_json_path else None,
        "session_transition_release_governor_json": str(session_transition_release_governor_json_path) if session_transition_release_governor_json_path else None,
        "require_operating_preflight": require_operating_preflight,
        "allow_non_operating_source_ledger": allow_non_operating_source_ledger,
        "require_session_transition_release_governor": require_session_transition_release_governor,
        "max_runs": max_runs,
        "cycles_per_run": cycles_per_run,
        "interval_seconds": interval_seconds,
        "sleep_between_runs": sleep_between_runs,
        "dedupe_across_cycles": dedupe_across_cycles,
        "carry_dedupe_state_across_runs": not no_carry_dedupe_state_across_runs,
        "monitor_size_multiplier": monitor_size_multiplier,
        "max_intents": max_intents,
        "fill_price_source": fill_price_source,
        "fill_model": fill_model,
        "max_orders": max_orders,
        "slippage_bps": slippage_bps,
        "commission_per_order": commission_per_order,
        "take_profit_pct": take_profit_pct,
        "stop_loss_pct": stop_loss_pct,
        "max_loss_dollars": max_loss_dollars,
        "max_gross_exposure": max_gross_exposure,
        "max_symbol_exposure": max_symbol_exposure,
        "max_short_exposure": max_short_exposure,
        "max_unrealized_loss": max_unrealized_loss,
        "max_total_loss": max_total_loss,
        "max_open_symbols": max_open_symbols,
        "warning_ratio": warning_ratio,
        "scheduler_run_id": scheduler_run_id,
        "sample_limit": sample_limit,
    }
    if sleep_between_runs and max_runs > 1 and interval_seconds > 0:
        # Keep this intentionally finite: it waits between scheduler batches, then
        # runs the same deterministic paper scheduler over the current input files.
        for _ in range(max(0, int(max_runs) - 1)):
            time.sleep(float(interval_seconds))
    result = build_paper_cycle_scheduler_report(
        auto_gate_payload,
        portfolio_risk_state_payload,
        fill_marks_payload,
        loop_mark_payloads,
        existing_ledger_payload=existing_ledger_payload,
        existing_router_payload=existing_router_payload,
        exit_router_payload=exit_payload,
        operating_auto_gate_payload=operating_auto_gate_payload,
        session_transition_release_governor_payload=session_transition_release_governor_payload,
        require_operating_preflight=require_operating_preflight,
        allow_non_operating_source_ledger=allow_non_operating_source_ledger,
        require_session_transition_release_governor=require_session_transition_release_governor,
        max_runs=max_runs,
        cycles_per_run=cycles_per_run,
        interval_seconds=interval_seconds,
        dedupe_across_cycles=dedupe_across_cycles,
        carry_dedupe_state_across_runs=not no_carry_dedupe_state_across_runs,
        monitor_size_multiplier=monitor_size_multiplier,
        max_intents=max_intents,
        fill_price_source=fill_price_source,
        fill_model=fill_model,
        max_orders=max_orders,
        slippage_bps=slippage_bps,
        commission_per_order=commission_per_order,
        take_profit_pct=take_profit_pct,
        stop_loss_pct=stop_loss_pct,
        max_loss_dollars=max_loss_dollars,
        max_gross_exposure=max_gross_exposure,
        max_symbol_exposure=max_symbol_exposure,
        max_short_exposure=max_short_exposure,
        max_unrealized_loss=max_unrealized_loss,
        max_total_loss=max_total_loss,
        max_open_symbols=max_open_symbols,
        warning_ratio=warning_ratio,
        scheduler_run_id=scheduler_run_id,
        resume_prior_runs_completed=int(resume_state.get("runs_completed_total") or 0) if resume_state else 0,
        sample_limit=sample_limit,
    )
    checkpoint = build_paper_cycle_scheduler_checkpoint(result)
    written = write_paper_cycle_scheduler_markdown(output, result, filters=filters) if output else None
    component_outputs = {
        "next_risk_json_output": str(_write_component_json(next_risk_json_output_path, result["next_portfolio_risk_state"], filters) or ""),
        "checkpoint_output": str(_write_component_json(checkpoint_output_path, checkpoint, filters) or ""),
    }
    if json_output_path is not None:
        json_output_path = json_output_path.expanduser().resolve()
        json_output_path.parent.mkdir(parents=True, exist_ok=True)
        json_output_path.write_text(
            json.dumps(
                {"result": result, "filters": filters, "output": str(written) if written else None, **component_outputs},
                indent=2,
                ensure_ascii=False,
            ),
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
                    **component_outputs,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return
    print(paper_cycle_scheduler_markdown(result, filters=filters))
    if written:
        print(f"\nMarkdown report written: {written}")
    if json_output_path:
        print(f"JSON report written: {json_output_path}")
    for label, path_text in component_outputs.items():
        if path_text:
            print(f"{label} written: {path_text}")




def _execute_paper_cycle_watch_tick_scheduler(
    *,
    run_id: str,
    input_snapshot: dict[str, Any],
    auto_gate_json_path: Path,
    portfolio_risk_state_json_path: Path,
    fill_marks_json_path: Path,
    loop_mark_paths: list[Path],
    existing_ledger_json_path: Path | None,
    existing_router_json_path: Path | None,
    exit_router_json_path: Path | None,
    checkpoint_path: Path | None,
    filters: dict[str, Any],
    resume_state: dict[str, Any],
    fill_marks_payload_for_run: dict[str, Any] | None,
    loop_mark_payloads_for_run: list[dict[str, Any]] | None,
    mark_refresh_quality_action: dict[str, Any],
    operating_auto_gate_payload: dict[str, Any] | None,
    require_operating_preflight: bool,
    allow_non_operating_source_ledger: bool,
    cycles_per_run: int,
    dedupe_across_cycles: bool,
    no_carry_dedupe_state_across_runs: bool,
    effective_monitor_size_multiplier: float,
    max_intents: int | None,
    fill_price_source: str,
    fill_model: str,
    max_orders: int,
    slippage_bps: float,
    commission_per_order: float,
    take_profit_pct: float,
    stop_loss_pct: float,
    max_loss_dollars: float | None,
    max_gross_exposure: float,
    max_symbol_exposure: float,
    max_short_exposure: float,
    max_unrealized_loss: float,
    max_total_loss: float,
    max_open_symbols: int,
    warning_ratio: float,
    sample_limit: int,
    session_gate: dict[str, Any],
    preflight_gate: dict[str, Any] | None,
    evidence_refresh_watchdog: dict[str, Any] | None,
    evidence_preflight_gate: dict[str, Any] | None,
    market_data_risk_gate: dict[str, Any] | None,
    mark_refresh: dict[str, Any] | None,
) -> dict[str, Any]:
    from recorder.options_analysis import (
        build_paper_cycle_scheduler_checkpoint,
        build_paper_cycle_scheduler_report,
        build_paper_cycle_watch_tick_report,
    )

    auto_gate_payload = json.loads(auto_gate_json_path.read_text(encoding="utf-8"))
    portfolio_risk_state_payload = json.loads(portfolio_risk_state_json_path.read_text(encoding="utf-8"))
    portfolio_risk_state_payload = _apply_mark_refresh_quality_action_to_portfolio_risk(
        portfolio_risk_state_payload,
        mark_refresh_quality_action,
    )
    fill_marks_payload = fill_marks_payload_for_run or json.loads(fill_marks_json_path.read_text(encoding="utf-8"))
    loop_mark_payloads = loop_mark_payloads_for_run or [json.loads(path.read_text(encoding="utf-8")) for path in loop_mark_paths]
    if resume_state:
        portfolio_risk_state_payload = resume_state.get("next_portfolio_risk_state") or portfolio_risk_state_payload
        portfolio_risk_state_payload = _apply_mark_refresh_quality_action_to_portfolio_risk(
            portfolio_risk_state_payload,
            mark_refresh_quality_action,
        )
    existing_ledger_payload = None
    if existing_ledger_json_path is not None:
        existing_ledger_payload = json.loads(existing_ledger_json_path.read_text(encoding="utf-8"))
    elif resume_state:
        existing_ledger_payload = resume_state.get("existing_ledger")
    existing_router_payload = None
    if existing_router_json_path is not None:
        existing_router_payload = json.loads(existing_router_json_path.read_text(encoding="utf-8"))
    elif resume_state:
        existing_router_payload = resume_state.get("existing_router")
    exit_payload = json.loads(exit_router_json_path.read_text(encoding="utf-8")) if exit_router_json_path is not None else None
    scheduler_result = build_paper_cycle_scheduler_report(
        auto_gate_payload,
        portfolio_risk_state_payload,
        fill_marks_payload,
        loop_mark_payloads,
        existing_ledger_payload=existing_ledger_payload,
        existing_router_payload=existing_router_payload,
        exit_router_payload=exit_payload,
        operating_auto_gate_payload=operating_auto_gate_payload,
        require_operating_preflight=require_operating_preflight,
        allow_non_operating_source_ledger=allow_non_operating_source_ledger,
        max_runs=1,
        cycles_per_run=cycles_per_run,
        interval_seconds=0.0,
        dedupe_across_cycles=dedupe_across_cycles,
        carry_dedupe_state_across_runs=not no_carry_dedupe_state_across_runs,
        monitor_size_multiplier=effective_monitor_size_multiplier,
        max_intents=max_intents,
        fill_price_source=fill_price_source,
        fill_model=fill_model,
        max_orders=max_orders,
        slippage_bps=slippage_bps,
        commission_per_order=commission_per_order,
        take_profit_pct=take_profit_pct,
        stop_loss_pct=stop_loss_pct,
        max_loss_dollars=max_loss_dollars,
        max_gross_exposure=max_gross_exposure,
        max_symbol_exposure=max_symbol_exposure,
        max_short_exposure=max_short_exposure,
        max_unrealized_loss=max_unrealized_loss,
        max_total_loss=max_total_loss,
        max_open_symbols=max_open_symbols,
        warning_ratio=warning_ratio,
        scheduler_run_id=f"{run_id}_scheduler",
        resume_prior_runs_completed=int(resume_state.get("runs_completed_total") or 0) if resume_state else 0,
        sample_limit=sample_limit,
    )
    checkpoint = build_paper_cycle_scheduler_checkpoint(scheduler_result, checkpoint_run_id=f"{run_id}_checkpoint")
    _write_component_json_atomic(checkpoint_path, checkpoint, filters)
    return build_paper_cycle_watch_tick_report(
        watch_run_id=run_id,
        input_snapshot=input_snapshot,
        action="executed_scheduler_tick",
        scheduler_result=scheduler_result,
        checkpoint=checkpoint,
        session_gate=session_gate,
        preflight_gate=preflight_gate,
        evidence_refresh_watchdog=evidence_refresh_watchdog,
        evidence_preflight_gate=evidence_preflight_gate,
        market_data_risk_gate=market_data_risk_gate,
        mark_refresh=mark_refresh,
    )


def _emit_paper_cycle_watch_tick_result(
    *,
    result: dict[str, Any],
    filters: dict[str, Any],
    output: Path | None,
    json_output_path: Path | None,
    watch_state_json_path: Path | None,
    json_output: bool,
    emit_report: bool,
    checkpoint_path: Path | None,
    should_skip: bool,
) -> dict[str, Any]:
    from recorder.options_analysis import paper_cycle_watch_tick_markdown, write_paper_cycle_watch_tick_markdown

    written = write_paper_cycle_watch_tick_markdown(output, result, filters=filters) if output else None
    watch_state_output = _write_component_json_atomic(watch_state_json_path, result, filters) if watch_state_json_path else None
    if json_output_path is not None:
        json_output_path = json_output_path.expanduser().resolve()
        json_output_path.parent.mkdir(parents=True, exist_ok=True)
        json_output_path.write_text(
            json.dumps(
                {
                    "result": result,
                    "filters": filters,
                    "output": str(written) if written else None,
                    "watch_state_output": str(watch_state_output) if watch_state_output else "",
                },
                indent=2,
                ensure_ascii=False,
            ),
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
                    "watch_state_output": str(watch_state_output) if watch_state_output else "",
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return result
    if emit_report:
        print(paper_cycle_watch_tick_markdown(result, filters=filters))
        if written:
            print(f"\nMarkdown report written: {written}")
        if json_output_path:
            print(f"JSON report written: {json_output_path}")
        if watch_state_output:
            print(f"watch_state_output written: {watch_state_output}")
        if checkpoint_path and not should_skip:
            print(f"checkpoint_path written atomically: {checkpoint_path}")
    return result



def _build_paper_cycle_watch_tick_skip_result(
    *,
    run_id: str,
    input_snapshot: dict[str, Any],
    action: str,
    checkpoint_result: dict[str, Any],
    skipped_reason: str,
    session_gate: dict[str, Any],
    preflight_gate: dict[str, Any] | None,
    evidence_refresh_watchdog: dict[str, Any] | None,
    evidence_preflight_gate: dict[str, Any] | None,
    market_data_risk_gate: dict[str, Any] | None,
    mark_refresh: dict[str, Any] | None,
) -> dict[str, Any]:
    from recorder.options_analysis import build_paper_cycle_watch_tick_report

    return build_paper_cycle_watch_tick_report(
        watch_run_id=run_id,
        input_snapshot=input_snapshot,
        action=action,
        checkpoint=checkpoint_result,
        skipped_reason=skipped_reason,
        session_gate=session_gate,
        preflight_gate=preflight_gate,
        evidence_refresh_watchdog=evidence_refresh_watchdog,
        evidence_preflight_gate=evidence_preflight_gate,
        market_data_risk_gate=market_data_risk_gate,
        mark_refresh=mark_refresh,
    )

def command_replay_score_paper_cycle_watch_tick(
    profile_json_path: Path | None,
    auto_gate_json_path: Path | None,
    portfolio_risk_state_json_path: Path | None,
    fill_marks_json_path: Path | None,
    loop_marks_jsons_value: str | None,
    existing_ledger_json_path: Path | None,
    existing_router_json_path: Path | None,
    exit_router_json_path: Path | None,
    checkpoint_path: Path | None,
    watch_state_json_path: Path | None,
    skip_unchanged_inputs: bool | None,
    force_run: bool | None,
    cycles_per_run: int | None,
    dedupe_across_cycles: bool | None,
    no_carry_dedupe_state_across_runs: bool | None,
    monitor_size_multiplier: float | None,
    max_intents: int | None,
    fill_price_source: str | None,
    fill_model: str | None,
    max_orders: int | None,
    slippage_bps: float | None,
    commission_per_order: float | None,
    take_profit_pct: float | None,
    stop_loss_pct: float | None,
    max_loss_dollars: float | None,
    max_gross_exposure: float | None,
    max_symbol_exposure: float | None,
    max_short_exposure: float | None,
    max_unrealized_loss: float | None,
    max_total_loss: float | None,
    max_open_symbols: int | None,
    warning_ratio: float | None,
    watch_run_id: str | None,
    sample_limit: int | None,
    session_gate_now: str | None = None,
    ignore_session_gate: bool | None = None,
    preflight_now: str | None = None,
    ignore_preflight_gate: bool | None = None,
    preflight_max_snapshot_age_seconds: float | None = None,
    preflight_max_tick_age_seconds: float | None = None,
    allow_delayed_preflight: bool | None = None,
    mark_refresh_mode: str | None = None,
    ignore_mark_refresh: bool | None = None,
    operating_auto_gate_json_path: Path | None = None,
    ignore_operating_preflight: bool | None = None,
    allow_non_operating_source_ledger: bool | None = None,
    settings: Settings | None = None,
    output: Path | None = None,
    json_output_path: Path | None = None,
    json_output: bool = False,
    emit_report: bool = True,
) -> dict[str, Any]:
    from recorder.options_analysis import (
        build_paper_data_quality_preflight_report,
        extract_paper_cycle_scheduler_resume_state,
    )

    profile, profile_base = _load_profile_json(profile_json_path)
    watch_run_id = _coalesce_profile_value(watch_run_id, profile, "watch", "watch_run_id")
    run_id = watch_run_id or datetime.now(timezone.utc).strftime("paper_cycle_watch_tick_%Y%m%dT%H%M%SZ")
    auto_gate_json_path = _coalesce_profile_path(auto_gate_json_path, profile, "paths", "auto_gate_json", profile_base)
    operating_auto_gate_json_path = _coalesce_profile_path(
        operating_auto_gate_json_path,
        profile,
        "paths",
        "operating_auto_gate_json",
        profile_base,
    )
    if operating_auto_gate_json_path is None:
        default_operating_auto_gate_json = "../exports/m182_market_data_operating_auto_gate_latest.json" if profile_base is not None else "exports/m182_market_data_operating_auto_gate_latest.json"
        operating_auto_gate_json_path = _profile_path_value(
            _profile_lookup(profile, "operating_preflight", "operating_auto_gate_json", default_operating_auto_gate_json),
            profile_base,
        )
    portfolio_risk_state_json_path = _coalesce_profile_path(
        portfolio_risk_state_json_path,
        profile,
        "paths",
        "portfolio_risk_state_json",
        profile_base,
    )
    fill_marks_json_path = _coalesce_profile_path(fill_marks_json_path, profile, "paths", "fill_marks_json", profile_base)
    loop_marks_jsons_value = _coalesce_profile_path_list(
        loop_marks_jsons_value,
        profile,
        "paths",
        "loop_marks_jsons",
        profile_base,
    )
    existing_ledger_json_path = _coalesce_profile_path(
        existing_ledger_json_path,
        profile,
        "paths",
        "existing_ledger_json",
        profile_base,
    )
    existing_router_json_path = _coalesce_profile_path(
        existing_router_json_path,
        profile,
        "paths",
        "existing_router_json",
        profile_base,
    )
    exit_router_json_path = _coalesce_profile_path(exit_router_json_path, profile, "paths", "exit_router_json", profile_base)
    checkpoint_path = _coalesce_profile_path(checkpoint_path, profile, "paths", "checkpoint_path", profile_base)
    watch_state_json_path = _coalesce_profile_path(watch_state_json_path, profile, "paths", "watch_state_json", profile_base)
    output = _coalesce_profile_path(output, profile, "outputs", "output", profile_base)
    json_output_path = _coalesce_profile_path(json_output_path, profile, "outputs", "json_output_path", profile_base)
    skip_unchanged_inputs = _coalesce_profile_bool(skip_unchanged_inputs, profile, "watch", "skip_unchanged_inputs", False)
    force_run = _coalesce_profile_bool(force_run, profile, "watch", "force_run", False)
    ignore_session_gate = _coalesce_profile_bool(ignore_session_gate, profile, "session_gate", "ignore", False)
    ignore_preflight_gate = _coalesce_profile_bool(ignore_preflight_gate, profile, "preflight_gate", "ignore", False)
    ignore_mark_refresh = _coalesce_profile_bool(ignore_mark_refresh, profile, "mark_refresh", "ignore", False)
    ignore_operating_preflight = _coalesce_profile_bool(ignore_operating_preflight, profile, "operating_preflight", "ignore", False)
    allow_non_operating_source_ledger = _coalesce_profile_bool(
        allow_non_operating_source_ledger,
        profile,
        "operating_preflight",
        "allow_non_operating_source_ledger",
        False,
    )
    require_operating_preflight = not bool(ignore_operating_preflight)
    preflight_enabled = bool(_profile_lookup(profile, "preflight_gate", "enabled", False))
    preflight_now = _coalesce_profile_value(preflight_now, profile, "preflight_gate", "now")
    preflight_max_snapshot_age_seconds = float(
        _coalesce_profile_value(preflight_max_snapshot_age_seconds, profile, "preflight_gate", "max_snapshot_age_seconds", 28800.0)
    )
    preflight_max_tick_age_seconds_value = _coalesce_profile_value(
        preflight_max_tick_age_seconds,
        profile,
        "preflight_gate",
        "max_tick_age_seconds",
        preflight_max_snapshot_age_seconds,
    )
    preflight_max_tick_age_seconds = float(preflight_max_tick_age_seconds_value)
    allow_delayed_preflight = _coalesce_profile_bool(
        allow_delayed_preflight,
        profile,
        "preflight_gate",
        "allow_delayed",
        False,
    )
    preflight_require_all_marked = bool(_profile_lookup(profile, "preflight_gate", "require_all_marked", True))
    preflight_min_marked_ratio = float(_profile_lookup(profile, "preflight_gate", "min_marked_ratio", 1.0))
    dedupe_across_cycles = _coalesce_profile_bool(dedupe_across_cycles, profile, "watch", "dedupe_across_cycles", False)
    no_carry_dedupe_state_across_runs = _coalesce_profile_bool(
        no_carry_dedupe_state_across_runs,
        profile,
        "watch",
        "no_carry_dedupe_state_across_runs",
        not bool(_profile_lookup(profile, "watch", "carry_dedupe_state_across_runs", True)),
    )
    cycles_per_run = int(_coalesce_profile_value(cycles_per_run, profile, "watch", "cycles_per_run", 1))
    monitor_size_multiplier = float(_coalesce_profile_value(monitor_size_multiplier, profile, "risk_limits", "monitor_size_multiplier", 0.50))
    max_intents = int(_coalesce_profile_value(max_intents, profile, "watch", "max_intents", 10))
    fill_price_source = str(_coalesce_profile_value(fill_price_source, profile, "execution", "fill_price_source", "require_marks"))
    fill_model = str(_coalesce_profile_value(fill_model, profile, "execution", "fill_model", "ibkr_mark_reference_fill_v1"))
    max_orders = int(_coalesce_profile_value(max_orders, profile, "execution", "max_orders", 10))
    slippage_bps = float(_coalesce_profile_value(slippage_bps, profile, "execution", "slippage_bps", 0.0))
    commission_per_order = float(_coalesce_profile_value(commission_per_order, profile, "execution", "commission_per_order", 0.0))
    take_profit_pct = float(_coalesce_profile_value(take_profit_pct, profile, "risk_limits", "take_profit_pct", 0.03))
    stop_loss_pct = float(_coalesce_profile_value(stop_loss_pct, profile, "risk_limits", "stop_loss_pct", 0.02))
    max_loss_dollars = _coalesce_profile_value(max_loss_dollars, profile, "risk_limits", "max_loss_dollars")
    max_loss_dollars = float(max_loss_dollars) if max_loss_dollars is not None else None
    max_gross_exposure = float(_coalesce_profile_value(max_gross_exposure, profile, "risk_limits", "max_gross_exposure", 100000.0))
    max_symbol_exposure = float(_coalesce_profile_value(max_symbol_exposure, profile, "risk_limits", "max_symbol_exposure", 25000.0))
    max_short_exposure = float(_coalesce_profile_value(max_short_exposure, profile, "risk_limits", "max_short_exposure", 25000.0))
    max_unrealized_loss = float(_coalesce_profile_value(max_unrealized_loss, profile, "risk_limits", "max_unrealized_loss", 1000.0))
    max_total_loss = float(_coalesce_profile_value(max_total_loss, profile, "risk_limits", "max_total_loss", 1000.0))
    max_open_symbols = int(_coalesce_profile_value(max_open_symbols, profile, "risk_limits", "max_open_symbols", 25))
    warning_ratio = float(_coalesce_profile_value(warning_ratio, profile, "risk_limits", "warning_ratio", 0.80))
    sample_limit = int(_coalesce_profile_value(sample_limit, profile, "watch", "sample_limit", 20))
    if auto_gate_json_path is None:
        raise ValueError("--auto-gate-json or profile paths.auto_gate_json is required")
    if portfolio_risk_state_json_path is None:
        raise ValueError("--portfolio-risk-state-json or profile paths.portfolio_risk_state_json is required")
    if fill_marks_json_path is None:
        raise ValueError("--fill-marks-json or profile paths.fill_marks_json is required")
    if not loop_marks_jsons_value:
        raise ValueError("--loop-marks-jsons or profile paths.loop_marks_jsons is required")
    auto_gate_json_path = auto_gate_json_path.expanduser().resolve()
    portfolio_risk_state_json_path = portfolio_risk_state_json_path.expanduser().resolve()
    fill_marks_json_path = fill_marks_json_path.expanduser().resolve()
    loop_mark_paths = [path.expanduser().resolve() for path in _parse_path_list(loop_marks_jsons_value)]
    if not loop_mark_paths:
        raise ValueError("--loop-marks-jsons must include at least one mark snapshot JSON path")
    checkpoint_path = checkpoint_path.expanduser().resolve() if checkpoint_path is not None else None
    existing_ledger_json_path = existing_ledger_json_path.expanduser().resolve() if existing_ledger_json_path is not None else None
    existing_router_json_path = existing_router_json_path.expanduser().resolve() if existing_router_json_path is not None else None
    exit_router_json_path = exit_router_json_path.expanduser().resolve() if exit_router_json_path is not None else None
    operating_auto_gate_payload, operating_auto_gate_json_path = _load_operating_auto_gate_payload(
        operating_auto_gate_json_path,
        require_operating_preflight=require_operating_preflight,
    )
    evidence_refresh_watchdog, evidence_refresh_watchdog_summary = _run_watch_evidence_refresh_watchdog(
        profile=profile,
        profile_base=profile_base,
        run_id=run_id,
    )
    source_auto_gate_json_path = auto_gate_json_path
    auto_gate_json_path, evidence_preflight_gate, evidence_preflight_auto_gate = _build_watch_evidence_preflight_auto_gate(
        profile=profile,
        profile_base=profile_base,
        source_auto_gate_json_path=source_auto_gate_json_path,
        run_id=run_id,
    )
    auto_gate_json_path, market_data_risk_gate, market_data_risk_auto_gate = _build_watch_market_data_risk_auto_gate(
        profile=profile,
        profile_base=profile_base,
        source_auto_gate_json_path=auto_gate_json_path,
        run_id=run_id,
    )
    session_gate = _evaluate_paper_session_gate(
        profile,
        now_value=session_gate_now,
        ignore_gate=bool(ignore_session_gate),
        profile_base=profile_base,
    )
    mark_refresh = None
    if not (session_gate.get("enabled") and not session_gate.get("allowed") and not bool(force_run)):
        mark_refresh = _run_paper_mark_refresh(
            settings=settings,
            profile=profile,
            profile_base=profile_base,
            fill_marks_json_path=fill_marks_json_path,
            loop_mark_paths=loop_mark_paths,
            mode_override=mark_refresh_mode,
            ignore_refresh=ignore_mark_refresh,
        )
    mark_refresh_quality_action = _mark_refresh_quality_execution_action(
        mark_refresh,
        profile,
        base_monitor_size_multiplier=monitor_size_multiplier,
    )
    effective_monitor_size_multiplier = float(mark_refresh_quality_action.get("effective_monitor_size_multiplier") or monitor_size_multiplier)
    if mark_refresh is not None:
        mark_refresh["execution_quality_action"] = mark_refresh_quality_action
    input_snapshot = _paper_watch_input_snapshot(
        auto_gate_json_path=auto_gate_json_path,
        operating_auto_gate_json_path=operating_auto_gate_json_path,
        portfolio_risk_state_json_path=portfolio_risk_state_json_path,
        fill_marks_json_path=fill_marks_json_path,
        loop_mark_paths=loop_mark_paths,
        existing_ledger_json_path=existing_ledger_json_path,
        existing_router_json_path=existing_router_json_path,
        exit_router_json_path=exit_router_json_path,
        checkpoint_path=checkpoint_path,
    )
    previous_signature = None
    if watch_state_json_path is not None and watch_state_json_path.expanduser().resolve().exists():
        previous_payload = json.loads(watch_state_json_path.expanduser().resolve().read_text(encoding="utf-8"))
        previous_result = previous_payload.get("result") if isinstance(previous_payload.get("result"), dict) else previous_payload
        previous_signature = ((previous_result or {}).get("input_snapshot") or {}).get("signature")
    checkpoint_payload = None
    checkpoint_result: dict[str, Any] = {}
    resume_state: dict[str, Any] = {}
    if checkpoint_path is not None and checkpoint_path.exists():
        checkpoint_payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        checkpoint_result = checkpoint_payload.get("result") if isinstance(checkpoint_payload.get("result"), dict) else checkpoint_payload
        resume_state = extract_paper_cycle_scheduler_resume_state(checkpoint_payload)
    should_skip = (
        bool(skip_unchanged_inputs)
        and not bool(force_run)
        and checkpoint_payload is not None
        and previous_signature == input_snapshot.get("signature")
    )
    filters = {
        "profile_json": str(profile_json_path.expanduser().resolve()) if profile_json_path else None,
        "auto_gate_json": str(auto_gate_json_path),
        "source_auto_gate_json": str(source_auto_gate_json_path),
        "evidence_refresh_watchdog": evidence_refresh_watchdog_summary,
        "evidence_preflight_auto_gate": evidence_preflight_auto_gate,
        "market_data_risk_auto_gate": market_data_risk_auto_gate,
        "operating_auto_gate_json": str(operating_auto_gate_json_path) if operating_auto_gate_json_path else None,
        "require_operating_preflight": require_operating_preflight,
        "ignore_operating_preflight": ignore_operating_preflight,
        "allow_non_operating_source_ledger": allow_non_operating_source_ledger,
        "portfolio_risk_state_json": str(portfolio_risk_state_json_path),
        "fill_marks_json": str(fill_marks_json_path),
        "loop_marks_jsons": ",".join(str(path) for path in loop_mark_paths),
        "existing_ledger_json": str(existing_ledger_json_path) if existing_ledger_json_path else None,
        "existing_router_json": str(existing_router_json_path) if existing_router_json_path else None,
        "exit_router_json": str(exit_router_json_path) if exit_router_json_path else None,
        "checkpoint_path": str(checkpoint_path) if checkpoint_path else None,
        "watch_state_json": str(watch_state_json_path.expanduser().resolve()) if watch_state_json_path else None,
        "skip_unchanged_inputs": skip_unchanged_inputs,
        "force_run": force_run,
        "input_signature": input_snapshot.get("signature"),
        "previous_input_signature": previous_signature,
        "session_gate_now": session_gate_now,
        "ignore_session_gate": ignore_session_gate,
        "session_gate_allowed": session_gate.get("allowed"),
        "session_gate_session": session_gate.get("session"),
        "preflight_enabled": preflight_enabled,
        "ignore_preflight_gate": ignore_preflight_gate,
        "mark_refresh_mode": mark_refresh_mode or _profile_lookup(profile, "mark_refresh", "mode"),
        "ignore_mark_refresh": ignore_mark_refresh,
        "mark_refresh_status": (mark_refresh or {}).get("status"),
        "mark_refresh_quality_action": mark_refresh_quality_action.get("action"),
        "mark_refresh_quality_action_applied": mark_refresh_quality_action.get("applied"),
        "preflight_now": preflight_now,
        "preflight_max_snapshot_age_seconds": preflight_max_snapshot_age_seconds,
        "preflight_max_tick_age_seconds": preflight_max_tick_age_seconds,
        "allow_delayed_preflight": allow_delayed_preflight,
        "cycles_per_run": cycles_per_run,
        "dedupe_across_cycles": dedupe_across_cycles,
        "carry_dedupe_state_across_runs": not no_carry_dedupe_state_across_runs,
        "monitor_size_multiplier": monitor_size_multiplier,
        "effective_monitor_size_multiplier": effective_monitor_size_multiplier,
        "max_intents": max_intents,
        "fill_price_source": fill_price_source,
        "fill_model": fill_model,
        "max_orders": max_orders,
        "slippage_bps": slippage_bps,
        "commission_per_order": commission_per_order,
        "take_profit_pct": take_profit_pct,
        "stop_loss_pct": stop_loss_pct,
        "max_loss_dollars": max_loss_dollars,
        "max_gross_exposure": max_gross_exposure,
        "max_symbol_exposure": max_symbol_exposure,
        "max_short_exposure": max_short_exposure,
        "max_unrealized_loss": max_unrealized_loss,
        "max_total_loss": max_total_loss,
        "max_open_symbols": max_open_symbols,
        "warning_ratio": warning_ratio,
        "watch_run_id": run_id,
        "sample_limit": sample_limit,
    }
    preflight_gate = None
    fill_marks_payload_for_run = None
    loop_mark_payloads_for_run = None
    if mark_refresh and not mark_refresh.get("allowed") and not bool(force_run):
        result = _build_paper_cycle_watch_tick_skip_result(
            run_id=run_id,
            input_snapshot=input_snapshot,
            action="skipped_mark_refresh_gate",
            checkpoint_result=checkpoint_result,
            skipped_reason=mark_refresh.get("reason") or "Mark refresh gate blocked this paper watch tick.",
            session_gate=session_gate,
            preflight_gate=preflight_gate,
            evidence_refresh_watchdog=evidence_refresh_watchdog,
            evidence_preflight_gate=evidence_preflight_gate,
            market_data_risk_gate=market_data_risk_gate,
            mark_refresh=mark_refresh,
        )
    elif preflight_enabled and not (session_gate.get("enabled") and not session_gate.get("allowed") and not bool(force_run)):
        fill_marks_payload_for_run = json.loads(fill_marks_json_path.read_text(encoding="utf-8"))
        loop_mark_payloads_for_run = [json.loads(path.read_text(encoding="utf-8")) for path in loop_mark_paths]
        preflight_gate = build_paper_data_quality_preflight_report(
            fill_marks_payload=fill_marks_payload_for_run,
            loop_mark_payloads=loop_mark_payloads_for_run,
            now=preflight_now,
            max_snapshot_age_seconds=preflight_max_snapshot_age_seconds,
            max_tick_age_seconds=preflight_max_tick_age_seconds,
            allow_delayed=bool(allow_delayed_preflight),
            require_all_marked=bool(preflight_require_all_marked),
            min_marked_ratio=preflight_min_marked_ratio,
            enabled=not bool(ignore_preflight_gate),
        )
    if mark_refresh and not mark_refresh.get("allowed") and not bool(force_run):
        pass
    elif session_gate.get("enabled") and not session_gate.get("allowed") and not bool(force_run):
        result = _build_paper_cycle_watch_tick_skip_result(
            run_id=run_id,
            input_snapshot=input_snapshot,
            action="skipped_session_gate",
            checkpoint_result=checkpoint_result,
            skipped_reason=session_gate.get("reason") or "Session gate blocked this paper watch tick.",
            session_gate=session_gate,
            preflight_gate=preflight_gate,
            evidence_refresh_watchdog=evidence_refresh_watchdog,
            evidence_preflight_gate=evidence_preflight_gate,
            market_data_risk_gate=market_data_risk_gate,
            mark_refresh=mark_refresh,
        )
    elif preflight_gate and not preflight_gate.get("allowed") and not bool(force_run):
        result = _build_paper_cycle_watch_tick_skip_result(
            run_id=run_id,
            input_snapshot=input_snapshot,
            action="skipped_preflight_gate",
            checkpoint_result=checkpoint_result,
            skipped_reason=preflight_gate.get("reason") or "Preflight data-quality gate blocked this paper watch tick.",
            session_gate=session_gate,
            preflight_gate=preflight_gate,
            evidence_refresh_watchdog=evidence_refresh_watchdog,
            evidence_preflight_gate=evidence_preflight_gate,
            market_data_risk_gate=market_data_risk_gate,
            mark_refresh=mark_refresh,
        )
    elif should_skip:
        result = _build_paper_cycle_watch_tick_skip_result(
            run_id=run_id,
            input_snapshot=input_snapshot,
            action="skipped_unchanged_inputs",
            checkpoint_result=checkpoint_result,
            skipped_reason="Input file signature matched the prior watch-state signature and --force-run was not set.",
            session_gate=session_gate,
            preflight_gate=preflight_gate,
            evidence_refresh_watchdog=evidence_refresh_watchdog,
            evidence_preflight_gate=evidence_preflight_gate,
            market_data_risk_gate=market_data_risk_gate,
            mark_refresh=mark_refresh,
        )
    else:
        result = _execute_paper_cycle_watch_tick_scheduler(
            run_id=run_id,
            input_snapshot=input_snapshot,
            auto_gate_json_path=auto_gate_json_path,
            portfolio_risk_state_json_path=portfolio_risk_state_json_path,
            fill_marks_json_path=fill_marks_json_path,
            loop_mark_paths=loop_mark_paths,
            existing_ledger_json_path=existing_ledger_json_path,
            existing_router_json_path=existing_router_json_path,
            exit_router_json_path=exit_router_json_path,
            checkpoint_path=checkpoint_path,
            filters=filters,
            resume_state=resume_state,
            fill_marks_payload_for_run=fill_marks_payload_for_run,
            loop_mark_payloads_for_run=loop_mark_payloads_for_run,
            mark_refresh_quality_action=mark_refresh_quality_action,
            operating_auto_gate_payload=operating_auto_gate_payload,
            require_operating_preflight=require_operating_preflight,
            allow_non_operating_source_ledger=allow_non_operating_source_ledger,
            cycles_per_run=cycles_per_run,
            dedupe_across_cycles=dedupe_across_cycles,
            no_carry_dedupe_state_across_runs=no_carry_dedupe_state_across_runs,
            effective_monitor_size_multiplier=effective_monitor_size_multiplier,
            max_intents=max_intents,
            fill_price_source=fill_price_source,
            fill_model=fill_model,
            max_orders=max_orders,
            slippage_bps=slippage_bps,
            commission_per_order=commission_per_order,
            take_profit_pct=take_profit_pct,
            stop_loss_pct=stop_loss_pct,
            max_loss_dollars=max_loss_dollars,
            max_gross_exposure=max_gross_exposure,
            max_symbol_exposure=max_symbol_exposure,
            max_short_exposure=max_short_exposure,
            max_unrealized_loss=max_unrealized_loss,
            max_total_loss=max_total_loss,
            max_open_symbols=max_open_symbols,
            warning_ratio=warning_ratio,
            sample_limit=sample_limit,
            session_gate=session_gate,
            preflight_gate=preflight_gate,
            evidence_refresh_watchdog=evidence_refresh_watchdog,
            evidence_preflight_gate=evidence_preflight_gate,
            market_data_risk_gate=market_data_risk_gate,
            mark_refresh=mark_refresh,
        )
    return _emit_paper_cycle_watch_tick_result(
        result=result,
        filters=filters,
        output=output,
        json_output_path=json_output_path,
        watch_state_json_path=watch_state_json_path,
        json_output=json_output,
        emit_report=emit_report,
        checkpoint_path=checkpoint_path,
        should_skip=should_skip,
    )


def command_replay_score_paper_cycle_watch_runner(
    profile_json_path: Path,
    max_ticks: int,
    interval_seconds: float,
    sleep_between_ticks: bool,
    force_each_tick: bool,
    session_gate_now: str | None,
    ignore_session_gate: bool,
    preflight_now: str | None,
    ignore_preflight_gate: bool,
    preflight_max_snapshot_age_seconds: float | None,
    preflight_max_tick_age_seconds: float | None,
    allow_delayed_preflight: bool | None,
    mark_refresh_mode: str | None,
    ignore_mark_refresh: bool,
    operating_auto_gate_json_path: Path | None,
    ignore_operating_preflight: bool | None,
    allow_non_operating_source_ledger: bool | None,
    settings: Settings | None,
    runner_run_id: str | None,
    output_dir: Path | None,
    output: Path | None = None,
    json_output_path: Path | None = None,
    json_output: bool = False,
    emit_report: bool = True,
) -> dict[str, Any]:
    from recorder.options_analysis import (
        build_paper_cycle_watch_runner_report,
        paper_cycle_watch_runner_markdown,
        write_paper_cycle_watch_runner_markdown,
    )

    run_id = runner_run_id or datetime.now(timezone.utc).strftime("paper_cycle_watch_runner_%Y%m%dT%H%M%SZ")
    profile_json_path = profile_json_path.expanduser().resolve()
    output_dir = (output_dir or Path("exports") / run_id).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    tick_results: list[dict[str, Any]] = []
    for tick_index in range(1, max(0, int(max_ticks)) + 1):
        tick_run_id = f"{run_id}_tick_{tick_index:02d}"
        tick_output = output_dir / f"{tick_run_id}.md"
        tick_json_output = output_dir / f"{tick_run_id}.json"
        result = command_replay_score_paper_cycle_watch_tick(
            profile_json_path=profile_json_path,
            auto_gate_json_path=None,
            portfolio_risk_state_json_path=None,
            fill_marks_json_path=None,
            loop_marks_jsons_value=None,
            existing_ledger_json_path=None,
            existing_router_json_path=None,
            exit_router_json_path=None,
            checkpoint_path=None,
            watch_state_json_path=None,
            skip_unchanged_inputs=None,
            force_run=True if force_each_tick else None,
            cycles_per_run=None,
            dedupe_across_cycles=None,
            no_carry_dedupe_state_across_runs=None,
            monitor_size_multiplier=None,
            max_intents=None,
            fill_price_source=None,
            fill_model=None,
            max_orders=None,
            slippage_bps=None,
            commission_per_order=None,
            take_profit_pct=None,
            stop_loss_pct=None,
            max_loss_dollars=None,
            max_gross_exposure=None,
            max_symbol_exposure=None,
            max_short_exposure=None,
            max_unrealized_loss=None,
            max_total_loss=None,
            max_open_symbols=None,
            warning_ratio=None,
            watch_run_id=tick_run_id,
            sample_limit=None,
            session_gate_now=session_gate_now,
            ignore_session_gate=ignore_session_gate,
            preflight_now=preflight_now,
            ignore_preflight_gate=ignore_preflight_gate,
            preflight_max_snapshot_age_seconds=preflight_max_snapshot_age_seconds,
            preflight_max_tick_age_seconds=preflight_max_tick_age_seconds,
            allow_delayed_preflight=allow_delayed_preflight,
            mark_refresh_mode=mark_refresh_mode,
            ignore_mark_refresh=ignore_mark_refresh,
            operating_auto_gate_json_path=operating_auto_gate_json_path,
            ignore_operating_preflight=ignore_operating_preflight,
            allow_non_operating_source_ledger=allow_non_operating_source_ledger,
            settings=settings,
            output=tick_output,
            json_output_path=tick_json_output,
            json_output=False,
            emit_report=emit_report,
        )
        tick_results.append(result)
        if sleep_between_ticks and tick_index < max(0, int(max_ticks)) and interval_seconds > 0:
            time.sleep(float(interval_seconds))
    filters = {
        "profile_json": str(profile_json_path),
        "max_ticks": int(max_ticks),
        "interval_seconds": float(interval_seconds),
        "sleep_between_ticks": bool(sleep_between_ticks),
        "force_each_tick": bool(force_each_tick),
        "session_gate_now": session_gate_now,
        "ignore_session_gate": bool(ignore_session_gate),
        "preflight_now": preflight_now,
        "ignore_preflight_gate": bool(ignore_preflight_gate),
        "preflight_max_snapshot_age_seconds": preflight_max_snapshot_age_seconds,
        "preflight_max_tick_age_seconds": preflight_max_tick_age_seconds,
        "allow_delayed_preflight": allow_delayed_preflight,
        "mark_refresh_mode": mark_refresh_mode,
        "ignore_mark_refresh": bool(ignore_mark_refresh),
        "operating_auto_gate_json": str(operating_auto_gate_json_path) if operating_auto_gate_json_path else None,
        "ignore_operating_preflight": ignore_operating_preflight,
        "allow_non_operating_source_ledger": allow_non_operating_source_ledger,
        "runner_run_id": run_id,
        "output_dir": str(output_dir),
    }
    runner_result = build_paper_cycle_watch_runner_report(
        runner_run_id=run_id,
        tick_results=tick_results,
        max_ticks_requested=max_ticks,
        interval_seconds=interval_seconds,
        sleep_between_ticks=sleep_between_ticks,
        profile_json=str(profile_json_path),
    )
    written = write_paper_cycle_watch_runner_markdown(output, runner_result, filters=filters) if output else None
    if json_output_path is not None:
        json_output_path = json_output_path.expanduser().resolve()
        json_output_path.parent.mkdir(parents=True, exist_ok=True)
        json_output_path.write_text(
            json.dumps(
                {"result": runner_result, "filters": filters, "output": str(written) if written else None},
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    if json_output:
        print(
            json.dumps(
                {
                    "result": runner_result,
                    "filters": filters,
                    "output": str(written) if written else None,
                    "json_output": str(json_output_path) if json_output_path else None,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return runner_result
    if emit_report:
        print(paper_cycle_watch_runner_markdown(runner_result, filters=filters))
        if written:
            print(f"\nMarkdown report written: {written}")
        if json_output_path:
            print(f"JSON report written: {json_output_path}")
        print(f"Tick reports written under: {output_dir}")
    return runner_result


def _session_gate_now_for_batch(sequence_value: str | None, default_value: str | None, batch_index: int) -> str | None:
    if not sequence_value:
        return default_value
    values = [item.strip() for item in sequence_value.split(",") if item.strip()]
    if not values:
        return default_value
    return values[min(max(0, batch_index - 1), len(values) - 1)]


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def command_replay_score_paper_cycle_watch_daemon(
    profile_json_path: Path,
    max_batches: int | None,
    ticks_per_batch: int | None,
    batch_interval_seconds: float | None,
    sleep_between_batches: bool,
    runner_interval_seconds: float | None,
    sleep_between_ticks: bool,
    force_each_tick: bool,
    session_gate_now: str | None,
    session_gate_now_sequence: str | None,
    ignore_session_gate: bool,
    preflight_now: str | None,
    preflight_now_sequence: str | None,
    ignore_preflight_gate: bool,
    preflight_max_snapshot_age_seconds: float | None,
    preflight_max_tick_age_seconds: float | None,
    allow_delayed_preflight: bool | None,
    mark_refresh_mode: str | None,
    ignore_mark_refresh: bool,
    operating_auto_gate_json_path: Path | None,
    ignore_operating_preflight: bool | None,
    allow_non_operating_source_ledger: bool | None,
    settings: Settings | None,
    daemon_run_id: str | None,
    output_dir: Path | None,
    daily_log_jsonl: Path | None,
    output: Path | None = None,
    json_output_path: Path | None = None,
    json_output: bool = False,
) -> None:
    from recorder.options_analysis import (
        build_paper_cycle_watch_daemon_report,
        paper_cycle_watch_daemon_markdown,
        write_paper_cycle_watch_daemon_markdown,
    )

    run_id = daemon_run_id or datetime.now(timezone.utc).strftime("paper_cycle_watch_daemon_%Y%m%dT%H%M%SZ")
    supplied_profile_json_path = profile_json_path.expanduser().resolve()
    daemon_profile, daemon_profile_base = _load_profile_json(supplied_profile_json_path)
    if daemon_profile.get("watch_profile_json") or daemon_profile.get("profile_version") == "paper-cycle-watch-daemon-profile-v1":
        watch_profile_json_path = _profile_path_value(daemon_profile.get("watch_profile_json"), daemon_profile_base)
        if watch_profile_json_path is None:
            raise ValueError("M136 daemon profile requires watch_profile_json")
        profile_json_path = watch_profile_json_path.expanduser().resolve()
        max_batches = int(max_batches if max_batches is not None else _profile_lookup(daemon_profile, "daemon", "max_batches", 3))
        ticks_per_batch = int(ticks_per_batch if ticks_per_batch is not None else _profile_lookup(daemon_profile, "daemon", "ticks_per_batch", 1))
        batch_interval_seconds = float(
            batch_interval_seconds
            if batch_interval_seconds is not None
            else _profile_lookup(daemon_profile, "daemon", "batch_interval_seconds", 0.0)
        )
        runner_interval_seconds = float(
            runner_interval_seconds
            if runner_interval_seconds is not None
            else _profile_lookup(daemon_profile, "daemon", "runner_interval_seconds", 0.0)
        )
        if not sleep_between_batches:
            sleep_between_batches = bool(_profile_lookup(daemon_profile, "daemon", "sleep_between_batches", False))
        if not sleep_between_ticks:
            sleep_between_ticks = bool(_profile_lookup(daemon_profile, "daemon", "sleep_between_ticks", False))
        if not force_each_tick:
            force_each_tick = bool(_profile_lookup(daemon_profile, "daemon", "force_each_tick", False))
        output_dir = _coalesce_profile_path(output_dir, daemon_profile, "outputs", "output_dir", daemon_profile_base)
        daily_log_jsonl = _coalesce_profile_path(daily_log_jsonl, daemon_profile, "outputs", "daily_log_jsonl", daemon_profile_base)
        output = _coalesce_profile_path(output, daemon_profile, "outputs", "output", daemon_profile_base)
        json_output_path = _coalesce_profile_path(json_output_path, daemon_profile, "outputs", "json_output_path", daemon_profile_base)
    else:
        profile_json_path = supplied_profile_json_path
        max_batches = int(max_batches if max_batches is not None else 3)
        ticks_per_batch = int(ticks_per_batch if ticks_per_batch is not None else 1)
        batch_interval_seconds = float(batch_interval_seconds if batch_interval_seconds is not None else 0.0)
        runner_interval_seconds = float(runner_interval_seconds if runner_interval_seconds is not None else 0.0)

    output_dir = (output_dir or Path("exports") / run_id).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    if daily_log_jsonl is None:
        daily_log_jsonl = output_dir / f"{run_id}_daily_log.jsonl"
    daily_log_jsonl = daily_log_jsonl.expanduser().resolve()
    batch_results: list[dict[str, Any]] = []
    for batch_index in range(1, max(0, int(max_batches)) + 1):
        batch_session_gate_now = _session_gate_now_for_batch(session_gate_now_sequence, session_gate_now, batch_index)
        batch_preflight_now = _session_gate_now_for_batch(preflight_now_sequence, preflight_now, batch_index)
        batch_run_id = f"{run_id}_batch_{batch_index:02d}"
        batch_dir = output_dir / f"batch_{batch_index:02d}"
        batch_result = command_replay_score_paper_cycle_watch_runner(
            profile_json_path=profile_json_path,
            max_ticks=int(ticks_per_batch),
            interval_seconds=float(runner_interval_seconds),
            sleep_between_ticks=bool(sleep_between_ticks),
            force_each_tick=bool(force_each_tick),
            session_gate_now=batch_session_gate_now,
            ignore_session_gate=bool(ignore_session_gate),
            preflight_now=batch_preflight_now,
            ignore_preflight_gate=bool(ignore_preflight_gate),
            preflight_max_snapshot_age_seconds=preflight_max_snapshot_age_seconds,
            preflight_max_tick_age_seconds=preflight_max_tick_age_seconds,
            allow_delayed_preflight=allow_delayed_preflight,
            mark_refresh_mode=mark_refresh_mode,
            ignore_mark_refresh=bool(ignore_mark_refresh),
            operating_auto_gate_json_path=operating_auto_gate_json_path,
            ignore_operating_preflight=ignore_operating_preflight,
            allow_non_operating_source_ledger=allow_non_operating_source_ledger,
            settings=settings,
            runner_run_id=batch_run_id,
            output_dir=batch_dir,
            output=output_dir / f"{batch_run_id}.md",
            json_output_path=output_dir / f"{batch_run_id}.json",
            json_output=False,
            emit_report=False,
        )
        batch_results.append(batch_result)
        _append_jsonl(
            daily_log_jsonl,
            {
                "record_type": "paper_cycle_watch_daemon_batch",
                "daemon_run_id": run_id,
                "batch_index": batch_index,
                "session_gate_now": batch_session_gate_now,
                "preflight_now": batch_preflight_now,
                "result": batch_result,
            },
        )
        if sleep_between_batches and batch_index < max(0, int(max_batches)) and batch_interval_seconds > 0:
            time.sleep(float(batch_interval_seconds))
    filters = {
        "profile_json": str(profile_json_path),
        "max_batches": int(max_batches),
        "ticks_per_batch": int(ticks_per_batch),
        "batch_interval_seconds": float(batch_interval_seconds),
        "sleep_between_batches": bool(sleep_between_batches),
        "runner_interval_seconds": float(runner_interval_seconds),
        "sleep_between_ticks": bool(sleep_between_ticks),
        "force_each_tick": bool(force_each_tick),
        "session_gate_now": session_gate_now,
        "session_gate_now_sequence": session_gate_now_sequence,
        "ignore_session_gate": bool(ignore_session_gate),
        "preflight_now": preflight_now,
        "preflight_now_sequence": preflight_now_sequence,
        "ignore_preflight_gate": bool(ignore_preflight_gate),
        "preflight_max_snapshot_age_seconds": preflight_max_snapshot_age_seconds,
        "preflight_max_tick_age_seconds": preflight_max_tick_age_seconds,
        "allow_delayed_preflight": allow_delayed_preflight,
        "mark_refresh_mode": mark_refresh_mode,
        "ignore_mark_refresh": bool(ignore_mark_refresh),
        "operating_auto_gate_json": str(operating_auto_gate_json_path) if operating_auto_gate_json_path else None,
        "ignore_operating_preflight": ignore_operating_preflight,
        "allow_non_operating_source_ledger": allow_non_operating_source_ledger,
        "daemon_run_id": run_id,
        "output_dir": str(output_dir),
        "daily_log_jsonl": str(daily_log_jsonl),
    }
    daemon_result = build_paper_cycle_watch_daemon_report(
        daemon_run_id=run_id,
        batch_results=batch_results,
        max_batches_requested=max_batches,
        ticks_per_batch=ticks_per_batch,
        batch_interval_seconds=batch_interval_seconds,
        sleep_between_batches=sleep_between_batches,
        profile_json=str(profile_json_path),
        daily_log_jsonl=str(daily_log_jsonl),
    )
    _append_jsonl(
        daily_log_jsonl,
        {
            "record_type": "paper_cycle_watch_daemon_summary",
            "daemon_run_id": run_id,
            "result": daemon_result,
        },
    )
    written = write_paper_cycle_watch_daemon_markdown(output, daemon_result, filters=filters) if output else None
    if json_output_path is not None:
        json_output_path = json_output_path.expanduser().resolve()
        json_output_path.parent.mkdir(parents=True, exist_ok=True)
        json_output_path.write_text(
            json.dumps(
                {"result": daemon_result, "filters": filters, "output": str(written) if written else None},
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    if json_output:
        print(
            json.dumps(
                {
                    "result": daemon_result,
                    "filters": filters,
                    "output": str(written) if written else None,
                    "json_output": str(json_output_path) if json_output_path else None,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return
    print(paper_cycle_watch_daemon_markdown(daemon_result, filters=filters))
    if written:
        print(f"\nMarkdown report written: {written}")
    if json_output_path:
        print(f"JSON report written: {json_output_path}")
    print(f"Batch/tick reports written under: {output_dir}")
    print(f"Daily JSONL log written: {daily_log_jsonl}")
