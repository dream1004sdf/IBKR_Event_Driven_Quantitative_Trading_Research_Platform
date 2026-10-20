from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from recorder.config import Settings


def _console_safe_text(text: str) -> str:
    return text.encode("ascii", errors="replace").decode("ascii")


def command_operating_defensive_action_plan(
    operating_auto_gate_json_path: Path,
    paper_portfolio_state_json_path: Path | None,
    account_positions_json_path: Path | None,
    max_candidates: int,
    max_actions_per_symbol: int,
    hedge_notional_fraction: float,
    output: Path | None = None,
    json_output_path: Path | None = None,
    json_output: bool = False,
) -> None:
    from recorder.operating_defensive import (
        build_operating_defensive_action_plan_from_paths,
        operating_defensive_action_plan_markdown,
        write_operating_defensive_action_plan_json,
        write_operating_defensive_action_plan_markdown,
    )

    operating_auto_gate_json_path = operating_auto_gate_json_path.expanduser().resolve()
    paper_portfolio_state_json_path = paper_portfolio_state_json_path.expanduser().resolve() if paper_portfolio_state_json_path else None
    account_positions_json_path = account_positions_json_path.expanduser().resolve() if account_positions_json_path else None
    filters = {
        "operating_auto_gate_json": str(operating_auto_gate_json_path),
        "paper_portfolio_state_json": str(paper_portfolio_state_json_path) if paper_portfolio_state_json_path else None,
        "account_positions_json": str(account_positions_json_path) if account_positions_json_path else None,
        "max_candidates": max_candidates,
        "max_actions_per_symbol": max_actions_per_symbol,
        "hedge_notional_fraction": hedge_notional_fraction,
    }
    result = build_operating_defensive_action_plan_from_paths(
        operating_auto_gate_path=operating_auto_gate_json_path,
        paper_portfolio_state_path=paper_portfolio_state_json_path,
        account_positions_path=account_positions_json_path,
        max_candidates=max_candidates,
        max_actions_per_symbol=max_actions_per_symbol,
        hedge_notional_fraction=hedge_notional_fraction,
    )
    written = write_operating_defensive_action_plan_markdown(output, result, filters=filters) if output else None
    json_written = write_operating_defensive_action_plan_json(json_output_path, result) if json_output_path else None
    if json_output:
        print(
            json.dumps(
                {
                    "result": result,
                    "filters": filters,
                    "output": str(written) if written else None,
                    "json_output": str(json_written) if json_written else None,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return
    print(_console_safe_text(operating_defensive_action_plan_markdown(result, filters=filters)))
    if written:
        print(f"\nOperating defensive action plan Markdown written: {written}")
    if json_written:
        print(f"Operating defensive action plan JSON written: {json_written}")


def command_operating_defensive_intent_ledger(
    defensive_action_plan_json_path: Path,
    existing_ledger_json_path: Path | None,
    option_quote_gate_json_path: Path | None,
    broker_whatif_json_path: Path | None,
    allow_missing_whatif: bool,
    allow_missing_option_quotes: bool,
    max_intents: int,
    ledger_run_id: str | None,
    sample_limit: int,
    output: Path | None = None,
    json_output_path: Path | None = None,
    json_output: bool = False,
) -> None:
    from recorder.operating_defensive import (
        build_operating_defensive_intent_ledger_from_paths,
        operating_defensive_intent_ledger_markdown,
        write_operating_defensive_intent_ledger_json,
        write_operating_defensive_intent_ledger_markdown,
    )

    defensive_action_plan_json_path = defensive_action_plan_json_path.expanduser().resolve()
    existing_ledger_json_path = existing_ledger_json_path.expanduser().resolve() if existing_ledger_json_path else None
    option_quote_gate_json_path = option_quote_gate_json_path.expanduser().resolve() if option_quote_gate_json_path else None
    broker_whatif_json_path = broker_whatif_json_path.expanduser().resolve() if broker_whatif_json_path else None
    filters = {
        "defensive_action_plan_json": str(defensive_action_plan_json_path),
        "existing_ledger_json": str(existing_ledger_json_path) if existing_ledger_json_path else None,
        "option_quote_gate_json": str(option_quote_gate_json_path) if option_quote_gate_json_path else None,
        "broker_whatif_json": str(broker_whatif_json_path) if broker_whatif_json_path else None,
        "allow_missing_whatif": allow_missing_whatif,
        "allow_missing_option_quotes": allow_missing_option_quotes,
        "max_intents": max_intents,
        "ledger_run_id": ledger_run_id,
        "sample_limit": sample_limit,
    }
    result = build_operating_defensive_intent_ledger_from_paths(
        defensive_action_plan_path=defensive_action_plan_json_path,
        existing_ledger_path=existing_ledger_json_path,
        option_quote_gate_path=option_quote_gate_json_path,
        broker_whatif_path=broker_whatif_json_path,
        allow_missing_whatif=allow_missing_whatif,
        allow_missing_option_quotes=allow_missing_option_quotes,
        max_intents=max_intents,
        ledger_run_id=ledger_run_id,
    )
    written = (
        write_operating_defensive_intent_ledger_markdown(output, result, filters=filters, sample_limit=sample_limit)
        if output
        else None
    )
    json_written = write_operating_defensive_intent_ledger_json(json_output_path, result) if json_output_path else None
    if json_output:
        print(
            json.dumps(
                {
                    "result": result,
                    "filters": filters,
                    "output": str(written) if written else None,
                    "json_output": str(json_written) if json_written else None,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return
    print(_console_safe_text(operating_defensive_intent_ledger_markdown(result, filters=filters, sample_limit=sample_limit)))
    if written:
        print(f"\nOperating defensive intent ledger Markdown written: {written}")
    if json_written:
        print(f"Operating defensive intent ledger JSON written: {json_written}")


def command_operating_defensive_gate_evidence(
    defensive_action_plan_json_path: Path,
    broker_whatif_json_path: Path | None,
    option_quote_json_path: Path | None,
    option_chain_json_path: Path | None,
    account: str | None,
    max_preview_items: int,
    sample_limit: int,
    order_preview_json_output_path: Path | None = None,
    output: Path | None = None,
    json_output_path: Path | None = None,
    json_output: bool = False,
) -> None:
    from recorder.operating_defensive import (
        build_operating_defensive_gate_evidence_from_paths,
        operating_defensive_gate_evidence_markdown,
        write_operating_defensive_gate_evidence_json,
        write_operating_defensive_gate_evidence_markdown,
        write_operating_defensive_order_preview_json,
    )

    defensive_action_plan_json_path = defensive_action_plan_json_path.expanduser().resolve()
    broker_whatif_json_path = broker_whatif_json_path.expanduser().resolve() if broker_whatif_json_path else None
    option_quote_json_path = option_quote_json_path.expanduser().resolve() if option_quote_json_path else None
    option_chain_json_path = option_chain_json_path.expanduser().resolve() if option_chain_json_path else None
    filters = {
        "defensive_action_plan_json": str(defensive_action_plan_json_path),
        "broker_whatif_json": str(broker_whatif_json_path) if broker_whatif_json_path else None,
        "option_quote_json": str(option_quote_json_path) if option_quote_json_path else None,
        "option_chain_json": str(option_chain_json_path) if option_chain_json_path else None,
        "account": account,
        "max_preview_items": max_preview_items,
        "sample_limit": sample_limit,
    }
    result = build_operating_defensive_gate_evidence_from_paths(
        defensive_action_plan_path=defensive_action_plan_json_path,
        broker_whatif_path=broker_whatif_json_path,
        option_quote_path=option_quote_json_path,
        option_chain_path=option_chain_json_path,
        account=account,
        max_preview_items=max_preview_items,
    )
    preview_written = write_operating_defensive_order_preview_json(order_preview_json_output_path, result) if order_preview_json_output_path else None
    written = write_operating_defensive_gate_evidence_markdown(output, result, filters=filters, sample_limit=sample_limit) if output else None
    json_written = write_operating_defensive_gate_evidence_json(json_output_path, result) if json_output_path else None
    if json_output:
        print(
            json.dumps(
                {
                    "result": result,
                    "filters": filters,
                    "order_preview_json": str(preview_written) if preview_written else None,
                    "output": str(written) if written else None,
                    "json_output": str(json_written) if json_written else None,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return
    print(_console_safe_text(operating_defensive_gate_evidence_markdown(result, filters=filters, sample_limit=sample_limit)))
    if preview_written:
        print(f"\nOperating defensive order-preview JSON written: {preview_written}")
    if written:
        print(f"Operating defensive gate evidence Markdown written: {written}")
    if json_written:
        print(f"Operating defensive gate evidence JSON written: {json_written}")


def command_operating_defensive_gate_retry(
    settings: Settings,
    defensive_action_plan_json_path: Path,
    account: str | None,
    max_preview_items: int,
    existing_whatif_json_path: Path | None,
    existing_option_quote_json_path: Path | None,
    run_ibkr_whatif: bool,
    whatif_attempts: int,
    include_human_review: bool,
    allow_fractional: bool,
    whatif_timeout_seconds: float,
    run_option_quotes: bool,
    regular_session_only_option_quotes: bool,
    option_session_gate_now: str | None,
    option_symbols_value: str | None,
    option_attempts: int,
    option_market_data_types_value: str,
    option_quote_seconds: float,
    option_chain_timeout_seconds: float,
    min_dte: int,
    max_dte: int,
    strikes_around: int,
    max_expirations: int,
    max_quote_contracts: int,
    max_spread_pct: float,
    sleep_seconds: float,
    output_dir: Path,
    output: Path | None,
    json_output_path: Path | None,
    json_output: bool,
) -> None:
    from recorder.operating_defensive import (
        build_operating_defensive_gate_evidence,
        build_operating_defensive_order_preview,
        operating_defensive_gate_evidence_markdown,
        write_operating_defensive_gate_evidence_json,
        write_operating_defensive_gate_evidence_markdown,
        write_operating_defensive_order_preview_json,
    )
    from recorder.operating_defensive import (
        build_operating_defensive_gate_retry_report,
        defensive_option_symbols_from_plan,
        detect_ibkr_read_only_mode,
        evaluate_option_quote_session_gate,
        read_json,
        write_operating_defensive_gate_retry_json,
        write_operating_defensive_gate_retry_markdown,
    )
    from recorder.operating_defensive import (
        build_operating_defensive_intent_ledger,
        write_operating_defensive_intent_ledger_json,
        write_operating_defensive_intent_ledger_markdown,
    )
    from recorder.ibkr_options import (
        OptionQuoteSnapshotConfig,
        run_ibkr_option_quote_snapshot,
        write_option_quote_snapshot_json,
        write_option_quote_snapshot_markdown,
    )
    from recorder.ibkr_order_preview import (
        OrderWhatIfPreviewConfig,
        run_ibkr_order_whatif_preview,
        write_order_whatif_preview_json,
        write_order_whatif_preview_markdown,
    )

    defensive_action_plan_json_path = defensive_action_plan_json_path.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    plan_payload = json.loads(defensive_action_plan_json_path.read_text(encoding="utf-8"))
    order_preview = build_operating_defensive_order_preview(
        plan_payload,
        account=account,
        max_preview_items=max_preview_items,
    )
    order_preview_path = write_operating_defensive_order_preview_json(output_dir / "m189_operating_defensive_order_preview.json", order_preview)

    whatif_payloads: list[dict[str, Any]] = []
    option_quote_payloads: list[dict[str, Any]] = []
    if existing_whatif_json_path is not None:
        existing_whatif_json_path = existing_whatif_json_path.expanduser().resolve()
        existing_payload = read_json(existing_whatif_json_path)
        if existing_payload:
            whatif_payloads.append(existing_payload)
    if existing_option_quote_json_path is not None:
        existing_option_quote_json_path = existing_option_quote_json_path.expanduser().resolve()
        existing_payload = read_json(existing_option_quote_json_path)
        if existing_payload:
            option_quote_payloads.append(existing_payload)

    if run_ibkr_whatif:
        for attempt_index in range(1, max(0, int(whatif_attempts)) + 1):
            config = OrderWhatIfPreviewConfig(
                host=settings.host,
                port=settings.port,
                client_id=int(settings.client_id + 9400 + attempt_index),
                timeout_seconds=whatif_timeout_seconds,
                max_orders=max_preview_items,
                include_human_review=include_human_review,
                allow_fractional=allow_fractional,
            )
            attempt = run_ibkr_order_whatif_preview(order_preview, config)
            whatif_payloads.append(attempt)
            attempt_json = write_order_whatif_preview_json(output_dir / f"m189_ibkr_defensive_order_whatif_attempt_{attempt_index:02d}.json", attempt)
            write_order_whatif_preview_markdown(output_dir / f"m189_ibkr_defensive_order_whatif_attempt_{attempt_index:02d}.md", attempt)
            if detect_ibkr_read_only_mode(attempt):
                print(f"M189 what-if retry stopped early after attempt {attempt_index}: IBKR API Read-Only mode detected in {attempt_json}")
                break
            if attempt_index < int(whatif_attempts) and sleep_seconds > 0:
                time.sleep(float(sleep_seconds))

    option_symbols = tuple(item.strip().upper() for item in (option_symbols_value or "").split(",") if item.strip())
    if not option_symbols:
        option_symbols = tuple(defensive_option_symbols_from_plan(plan_payload))
    market_data_types = [int(item.strip()) for item in str(option_market_data_types_value or "1").split(",") if item.strip()]
    if not market_data_types:
        market_data_types = [1]
    option_session_gate = evaluate_option_quote_session_gate(
        now=option_session_gate_now,
        regular_session_only=bool(regular_session_only_option_quotes),
    )
    if run_option_quotes and option_symbols and not bool(option_session_gate.get("allowed", True)):
        print(
            "M191 option quote retry deferred: "
            f"session={option_session_gate.get('session')} reason={option_session_gate.get('reason')}"
        )
    if run_option_quotes and option_symbols and bool(option_session_gate.get("allowed", True)):
        for attempt_index in range(1, max(0, int(option_attempts)) + 1):
            market_data_type = market_data_types[(attempt_index - 1) % len(market_data_types)]
            config = OptionQuoteSnapshotConfig(
                host=settings.host,
                port=settings.port,
                client_id=int(settings.client_id + 9500 + attempt_index),
                symbols=option_symbols,
                reference_prices={},
                min_dte=min_dte,
                max_dte=max_dte,
                strikes_around=strikes_around,
                max_expirations=max_expirations,
                max_quote_contracts=max_quote_contracts,
                market_data_type=market_data_type,
                quote_seconds=option_quote_seconds,
                chain_timeout_seconds=option_chain_timeout_seconds,
                max_spread_pct=max_spread_pct,
            )
            attempt = run_ibkr_option_quote_snapshot(config)
            option_quote_payloads.append(attempt)
            write_option_quote_snapshot_json(output_dir / f"m189_ibkr_defensive_option_quote_attempt_{attempt_index:02d}.json", attempt)
            write_option_quote_snapshot_markdown(output_dir / f"m189_ibkr_defensive_option_quote_attempt_{attempt_index:02d}.md", attempt)
            if int(attempt.get("quoted_contracts") or 0) > 0:
                break
            if attempt_index < int(option_attempts) and sleep_seconds > 0:
                time.sleep(float(sleep_seconds))

    best_whatif = max(whatif_payloads, key=lambda payload: int((payload.get("result") or payload).get("response_count") or 0), default={})
    best_option_quote = max(option_quote_payloads, key=lambda payload: int((payload.get("result") or payload).get("quoted_contracts") or 0), default={})
    evidence = build_operating_defensive_gate_evidence(
        plan_payload,
        broker_whatif_payload=best_whatif,
        option_quote_payload=best_option_quote,
        account=account,
        max_preview_items=max_preview_items,
    )
    evidence_md_path = write_operating_defensive_gate_evidence_markdown(output_dir / "m189_operating_defensive_gate_evidence.md", evidence)
    evidence_json_path = write_operating_defensive_gate_evidence_json(output_dir / "m189_operating_defensive_gate_evidence.json", evidence)
    ledger = build_operating_defensive_intent_ledger(
        plan_payload,
        broker_whatif_payload=evidence,
        option_quote_gate_payload=evidence,
        max_intents=max_preview_items,
        ledger_run_id="m189-strict-current",
    )
    ledger_md_path = write_operating_defensive_intent_ledger_markdown(output_dir / "m189_operating_defensive_intent_ledger_strict.md", ledger)
    ledger_json_path = write_operating_defensive_intent_ledger_json(output_dir / "m189_operating_defensive_intent_ledger_strict.json", ledger)

    retry_policy = {
        "run_ibkr_whatif": bool(run_ibkr_whatif),
        "whatif_attempts_requested": int(whatif_attempts),
        "run_option_quotes": bool(run_option_quotes),
        "option_attempts_requested": int(option_attempts),
        "option_market_data_types": market_data_types,
        "regular_session_only_option_quotes": bool(regular_session_only_option_quotes),
        "option_quote_session_gate": option_session_gate,
        "sleep_seconds": float(sleep_seconds),
        "output_dir": str(output_dir),
        "order_preview_json": str(order_preview_path),
        "gate_evidence_json": str(evidence_json_path),
        "gate_evidence_markdown": str(evidence_md_path),
        "strict_ledger_json": str(ledger_json_path),
        "strict_ledger_markdown": str(ledger_md_path),
    }
    report = build_operating_defensive_gate_retry_report(
        defensive_action_plan_payload=plan_payload,
        order_preview_payload=order_preview,
        whatif_attempt_payloads=whatif_payloads,
        option_quote_attempt_payloads=option_quote_payloads,
        gate_evidence_payload=evidence,
        intent_ledger_payload=ledger,
        retry_policy=retry_policy,
    )
    written = write_operating_defensive_gate_retry_markdown(output, report) if output else None
    json_written = write_operating_defensive_gate_retry_json(json_output_path, report) if json_output_path else None
    if json_output:
        print(json.dumps({"result": report, "retry_policy": retry_policy}, indent=2, ensure_ascii=False))
        return
    from recorder.operating_defensive import operating_defensive_gate_retry_markdown

    print(_console_safe_text(operating_defensive_gate_retry_markdown(report)))
    if written:
        print(f"\nOperating defensive gate retry Markdown written: {written}")
    if json_written:
        print(f"Operating defensive gate retry JSON written: {json_written}")
    print(f"M189 order preview JSON: {order_preview_path}")
    print(f"M189 gate evidence JSON: {evidence_json_path}")
    print(f"M189 strict ledger JSON: {ledger_json_path}")


def command_operating_option_quote_resume_plan(
    latest_retry_json_path: Path,
    defensive_action_plan_json_path: Path,
    existing_whatif_json_path: Path | None,
    existing_option_quote_json_path: Path | None,
    account: str | None,
    m192_output_dir: Path,
    m192_output: Path,
    m192_json_output_path: Path,
    option_attempts: int,
    option_market_data_types: str,
    option_quote_seconds: float,
    option_chain_timeout_seconds: float,
    max_quote_contracts: int,
    now: str | None,
    output: Path | None,
    json_output_path: Path | None,
    json_output: bool,
) -> None:
    from recorder.operating_defensive import (
        build_option_quote_resume_plan,
        option_quote_resume_plan_markdown,
        read_json,
        write_option_quote_resume_plan_json,
        write_option_quote_resume_plan_markdown,
    )

    latest_retry_json_path = latest_retry_json_path.expanduser().resolve()
    defensive_action_plan_json_path = defensive_action_plan_json_path.expanduser().resolve()
    latest_retry_payload = read_json(latest_retry_json_path)
    plan_payload = read_json(defensive_action_plan_json_path)
    existing_whatif = str(existing_whatif_json_path.expanduser().resolve()) if existing_whatif_json_path else None
    existing_option_quote = str(existing_option_quote_json_path.expanduser().resolve()) if existing_option_quote_json_path else None
    report = build_option_quote_resume_plan(
        latest_retry_payload=latest_retry_payload,
        defensive_action_plan_payload=plan_payload,
        defensive_action_plan_json=str(defensive_action_plan_json_path),
        existing_whatif_json=existing_whatif,
        existing_option_quote_json=existing_option_quote,
        account=account,
        output_dir=str(m192_output_dir),
        output=str(m192_output),
        json_output_path=str(m192_json_output_path),
        option_attempts=option_attempts,
        option_market_data_types=option_market_data_types,
        option_quote_seconds=option_quote_seconds,
        option_chain_timeout_seconds=option_chain_timeout_seconds,
        max_quote_contracts=max_quote_contracts,
        now=now,
    )
    written = write_option_quote_resume_plan_markdown(output, report) if output else None
    json_written = write_option_quote_resume_plan_json(json_output_path, report) if json_output_path else None
    if json_output:
        print(json.dumps({"result": report}, indent=2, ensure_ascii=False))
        return
    print(_console_safe_text(option_quote_resume_plan_markdown(report)))
    if written:
        print(f"\nOption quote resume plan Markdown written: {written}")
    if json_written:
        print(f"Option quote resume plan JSON written: {json_written}")


def command_execution_safety_cache_supervisor(
    profile_json: Path,
    max_ticks: int,
    interval_seconds: float,
    sleep_between_ticks: bool,
    execute_refresh: bool,
    timeout_seconds: float,
    tick_log_jsonl_path: Path | None,
    output: Path | None,
    json_output_path: Path | None,
    json_output: bool,
) -> dict[str, Any]:
    from recorder.execution_safety_cache_supervisor import (
        execution_safety_cache_supervisor_markdown,
        run_execution_safety_cache_supervisor,
        write_execution_safety_cache_supervisor_json,
        write_execution_safety_cache_supervisor_markdown,
    )

    result = run_execution_safety_cache_supervisor(
        profile_json_path=profile_json,
        max_ticks=max_ticks,
        interval_seconds=interval_seconds,
        sleep_between_ticks=sleep_between_ticks,
        execute_refresh=execute_refresh,
        tick_log_jsonl_path=tick_log_jsonl_path,
        cwd=Path.cwd(),
        timeout_seconds=timeout_seconds,
    )
    written = write_execution_safety_cache_supervisor_markdown(output, result) if output else None
    json_written = write_execution_safety_cache_supervisor_json(json_output_path, result) if json_output_path else None
    if json_output:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return result
    print(_console_safe_text(execution_safety_cache_supervisor_markdown(result)))
    if written:
        print(f"\nMarkdown report written: {written}")
    if json_written:
        print(f"JSON report written: {json_written}")
    return result
