from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from recorder.config import Settings
from recorder.cli_paper_cycle_runtime import _parse_path_list, _write_component_json
from .cli_replay_score_helpers import _parse_float_grid, _parse_float_tuple, _parse_inline_mark_prices

def command_replay_score_options_analyze(
    settings: Settings,
    event_type: str | None,
    symbols_value: str | None,
    price_source: str | None,
    horizon: str | None,
    scorer_name: str | None,
    model_name: str | None,
    scorer_version: str | None,
    start: str | None,
    end: str | None,
    underlying_reference_price: float,
    min_dte: int,
    base_implied_vol: float,
    risk_free_rate: float,
    contract_multiplier: float,
    moneyness_offset_pct: float,
    spread_width_pct: float,
    min_option_score: float,
    min_group_rows: int,
    sample_limit: int,
    limit: int,
    output: Path | None = None,
    json_output_path: Path | None = None,
    json_output: bool = False,
) -> None:
    from recorder.analysis_database import AnalysisDatabase
    from recorder.options_analysis import (
        options_analysis_markdown,
        options_analysis_summary,
        write_options_analysis_markdown,
    )

    symbols = tuple(item.strip().upper() for item in (symbols_value or "").split(",") if item.strip())
    filters = {
        "event_type": event_type,
        "symbols": ",".join(symbols) if symbols else None,
        "price_source": price_source,
        "horizon": horizon.strip().upper() if horizon else None,
        "scorer_name": scorer_name,
        "model_name": model_name,
        "scorer_version": scorer_version,
        "start": start,
        "end": end,
        "underlying_reference_price": underlying_reference_price,
        "min_dte": min_dte,
        "base_implied_vol": base_implied_vol,
        "risk_free_rate": risk_free_rate,
        "contract_multiplier": contract_multiplier,
        "moneyness_offset_pct": moneyness_offset_pct,
        "spread_width_pct": spread_width_pct,
        "min_option_score": min_option_score,
        "min_group_rows": min_group_rows,
        "sample_limit": sample_limit,
        "limit": limit,
    }
    db = AnalysisDatabase(settings.database_path)
    rows = db.replay_model_score_analysis_rows(
        event_type=event_type,
        symbols=symbols or None,
        price_source=price_source,
        horizon=horizon,
        scorer_name=scorer_name,
        model_name=model_name,
        scorer_version=scorer_version,
        start=start,
        end=end,
        limit=limit,
    )
    summary = options_analysis_summary(
        rows,
        underlying_reference_price=underlying_reference_price,
        min_dte=min_dte,
        base_implied_vol=base_implied_vol,
        risk_free_rate=risk_free_rate,
        contract_multiplier=contract_multiplier,
        moneyness_offset_pct=moneyness_offset_pct,
        spread_width_pct=spread_width_pct,
        min_option_score=min_option_score,
        min_group_rows=min_group_rows,
        sample_limit=sample_limit,
    )
    written = write_options_analysis_markdown(output, summary, filters=filters) if output else None
    if json_output_path is not None:
        json_output_path = json_output_path.expanduser().resolve()
        json_output_path.parent.mkdir(parents=True, exist_ok=True)
        json_output_path.write_text(
            json.dumps({"summary": summary, "filters": filters, "output": str(written) if written else None}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    if json_output:
        print(
            json.dumps(
                {
                    "summary": summary,
                    "filters": filters,
                    "output": str(written) if written else None,
                    "json_output": str(json_output_path) if json_output_path else None,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return
    print(options_analysis_markdown(summary, filters=filters))
    if written:
        print(f"\nMarkdown report written: {written}")
    if json_output_path:
        print(f"JSON report written: {json_output_path}")


def command_replay_score_strategy_allocate(
    settings: Settings,
    event_type: str | None,
    symbols_value: str | None,
    price_source: str | None,
    horizon: str | None,
    scorer_name: str | None,
    model_name: str | None,
    scorer_version: str | None,
    start: str | None,
    end: str | None,
    underlying_reference_price: float,
    stock_quantity: float,
    stock_stop_loss_pct: float,
    min_confidence: float,
    min_stock_expected_return: float,
    min_dte: int,
    base_implied_vol: float,
    risk_free_rate: float,
    contract_multiplier: float,
    moneyness_offset_pct: float,
    spread_width_pct: float,
    min_option_score: float,
    min_option_return_on_risk: float,
    option_preference_edge: float,
    min_group_rows: int,
    sample_limit: int,
    limit: int,
    output: Path | None = None,
    json_output_path: Path | None = None,
    json_output: bool = False,
) -> None:
    from recorder.analysis_database import AnalysisDatabase
    from recorder.options_analysis import (
        strategy_allocation_markdown,
        strategy_allocation_summary,
        write_strategy_allocation_markdown,
    )

    symbols = tuple(item.strip().upper() for item in (symbols_value or "").split(",") if item.strip())
    filters = {
        "event_type": event_type,
        "symbols": ",".join(symbols) if symbols else None,
        "price_source": price_source,
        "horizon": horizon.strip().upper() if horizon else None,
        "scorer_name": scorer_name,
        "model_name": model_name,
        "scorer_version": scorer_version,
        "start": start,
        "end": end,
        "underlying_reference_price": underlying_reference_price,
        "stock_quantity": stock_quantity,
        "stock_stop_loss_pct": stock_stop_loss_pct,
        "min_confidence": min_confidence,
        "min_stock_expected_return": min_stock_expected_return,
        "min_dte": min_dte,
        "base_implied_vol": base_implied_vol,
        "risk_free_rate": risk_free_rate,
        "contract_multiplier": contract_multiplier,
        "moneyness_offset_pct": moneyness_offset_pct,
        "spread_width_pct": spread_width_pct,
        "min_option_score": min_option_score,
        "min_option_return_on_risk": min_option_return_on_risk,
        "option_preference_edge": option_preference_edge,
        "min_group_rows": min_group_rows,
        "sample_limit": sample_limit,
        "limit": limit,
    }
    db = AnalysisDatabase(settings.database_path)
    rows = db.replay_model_score_analysis_rows(
        event_type=event_type,
        symbols=symbols or None,
        price_source=price_source,
        horizon=horizon,
        scorer_name=scorer_name,
        model_name=model_name,
        scorer_version=scorer_version,
        start=start,
        end=end,
        limit=limit,
    )
    summary = strategy_allocation_summary(
        rows,
        underlying_reference_price=underlying_reference_price,
        stock_quantity=stock_quantity,
        stock_stop_loss_pct=stock_stop_loss_pct,
        min_confidence=min_confidence,
        min_stock_expected_return=min_stock_expected_return,
        min_dte=min_dte,
        base_implied_vol=base_implied_vol,
        risk_free_rate=risk_free_rate,
        contract_multiplier=contract_multiplier,
        moneyness_offset_pct=moneyness_offset_pct,
        spread_width_pct=spread_width_pct,
        min_option_score=min_option_score,
        min_option_return_on_risk=min_option_return_on_risk,
        option_preference_edge=option_preference_edge,
        min_group_rows=min_group_rows,
        sample_limit=sample_limit,
    )
    written = write_strategy_allocation_markdown(output, summary, filters=filters) if output else None
    if json_output_path is not None:
        json_output_path = json_output_path.expanduser().resolve()
        json_output_path.parent.mkdir(parents=True, exist_ok=True)
        json_output_path.write_text(
            json.dumps({"summary": summary, "filters": filters, "output": str(written) if written else None}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    if json_output:
        print(
            json.dumps(
                {
                    "summary": summary,
                    "filters": filters,
                    "output": str(written) if written else None,
                    "json_output": str(json_output_path) if json_output_path else None,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return
    print(strategy_allocation_markdown(summary, filters=filters))
    if written:
        print(f"\nMarkdown report written: {written}")
    if json_output_path:
        print(f"JSON report written: {json_output_path}")


def command_replay_score_strategy_walk_forward_tune(
    settings: Settings,
    event_type: str | None,
    symbols_value: str | None,
    price_source: str | None,
    horizon: str | None,
    scorer_name: str | None,
    model_name: str | None,
    scorer_version: str | None,
    start: str | None,
    end: str | None,
    min_train_rows: int,
    test_window_rows: int,
    step_rows: int | None,
    rolling_train_rows: int | None,
    underlying_reference_price: float,
    stock_quantity: float,
    stock_stop_loss_pct: float,
    min_confidence_grid: str | None,
    min_stock_expected_return_grid: str | None,
    min_option_score_grid: str | None,
    min_option_return_on_risk_grid: str | None,
    option_preference_edge_grid: str | None,
    min_dte: int,
    base_implied_vol: float,
    risk_free_rate: float,
    contract_multiplier: float,
    moneyness_offset_pct: float,
    spread_width_pct: float,
    min_selection_rate: float,
    pnl_weight: float,
    regret_penalty_weight: float,
    option_usage_penalty_weight: float,
    top_n: int,
    min_group_rows: int,
    sample_limit: int,
    limit: int,
    checkpoint_path: Path | None,
    output: Path | None = None,
    json_output_path: Path | None = None,
    json_output: bool = False,
) -> None:
    from recorder.analysis_database import AnalysisDatabase
    from recorder.options_analysis import (
        walk_forward_strategy_allocation_tuning,
        walk_forward_strategy_allocation_tuning_markdown,
        write_walk_forward_strategy_allocation_tuning_markdown,
    )

    symbols = tuple(item.strip().upper() for item in (symbols_value or "").split(",") if item.strip())
    filters = {
        "event_type": event_type,
        "symbols": ",".join(symbols) if symbols else None,
        "price_source": price_source,
        "horizon": horizon.strip().upper() if horizon else None,
        "scorer_name": scorer_name,
        "model_name": model_name,
        "scorer_version": scorer_version,
        "start": start,
        "end": end,
        "limit": limit,
    }
    db = AnalysisDatabase(settings.database_path)
    rows = db.replay_model_score_analysis_rows(
        event_type=event_type,
        symbols=symbols or None,
        price_source=price_source,
        horizon=horizon,
        scorer_name=scorer_name,
        model_name=model_name,
        scorer_version=scorer_version,
        start=start,
        end=end,
        limit=limit,
    )
    result = walk_forward_strategy_allocation_tuning(
        rows,
        min_train_rows=min_train_rows,
        test_window_rows=test_window_rows,
        step_rows=step_rows,
        rolling_train_rows=rolling_train_rows,
        underlying_reference_price=underlying_reference_price,
        stock_quantity=stock_quantity,
        stock_stop_loss_pct=stock_stop_loss_pct,
        min_confidence_grid=_parse_float_tuple(min_confidence_grid, (0.30, 0.35, 0.40)),
        min_stock_expected_return_grid=_parse_float_tuple(min_stock_expected_return_grid, (0.0025, 0.005, 0.01)),
        min_option_score_grid=_parse_float_tuple(min_option_score_grid, (0.30, 0.35, 0.40, 0.45)),
        min_option_return_on_risk_grid=_parse_float_tuple(min_option_return_on_risk_grid, (0.0, 0.10, 0.25)),
        option_preference_edge_grid=_parse_float_tuple(option_preference_edge_grid, (0.0, 0.05, 0.10, 0.20)),
        min_dte=min_dte,
        base_implied_vol=base_implied_vol,
        risk_free_rate=risk_free_rate,
        contract_multiplier=contract_multiplier,
        moneyness_offset_pct=moneyness_offset_pct,
        spread_width_pct=spread_width_pct,
        min_selection_rate=min_selection_rate,
        pnl_weight=pnl_weight,
        regret_penalty_weight=regret_penalty_weight,
        option_usage_penalty_weight=option_usage_penalty_weight,
        top_n=top_n,
        min_group_rows=min_group_rows,
        sample_limit=sample_limit,
        checkpoint_path=checkpoint_path,
    )
    written = write_walk_forward_strategy_allocation_tuning_markdown(output, result, filters=filters) if output else None
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
    print(walk_forward_strategy_allocation_tuning_markdown(result, filters=filters))
    if written:
        print(f"\nMarkdown report written: {written}")
    if json_output_path:
        print(f"JSON report written: {json_output_path}")


def command_replay_score_strategy_liquidity_gate(
    allocation_json_path: Path,
    quote_json_path: Path,
    max_spread_pct: float,
    min_quoted_contracts_per_symbol: int,
    min_volume: int,
    min_open_interest: int,
    require_iv: bool,
    no_fallback_to_stock: bool,
    min_group_rows: int,
    sample_limit: int,
    output: Path | None = None,
    json_output_path: Path | None = None,
    json_output: bool = False,
) -> None:
    from recorder.options_analysis import (
        apply_option_liquidity_gate_to_allocations,
        option_liquidity_gate_markdown,
        write_option_liquidity_gate_markdown,
    )

    allocation_json_path = allocation_json_path.expanduser().resolve()
    quote_json_path = quote_json_path.expanduser().resolve()
    allocation_payload = json.loads(allocation_json_path.read_text(encoding="utf-8"))
    quote_snapshot = json.loads(quote_json_path.read_text(encoding="utf-8"))
    filters = {
        "allocation_json": str(allocation_json_path),
        "quote_json": str(quote_json_path),
        "max_spread_pct": max_spread_pct,
        "min_quoted_contracts_per_symbol": min_quoted_contracts_per_symbol,
        "min_volume": min_volume,
        "min_open_interest": min_open_interest,
        "require_iv": require_iv,
        "fallback_to_stock": not no_fallback_to_stock,
        "min_group_rows": min_group_rows,
        "sample_limit": sample_limit,
    }
    result = apply_option_liquidity_gate_to_allocations(
        allocation_payload,
        quote_snapshot,
        max_spread_pct=max_spread_pct,
        min_quoted_contracts_per_symbol=min_quoted_contracts_per_symbol,
        min_volume=min_volume,
        min_open_interest=min_open_interest,
        require_iv=require_iv,
        fallback_to_stock=not no_fallback_to_stock,
        min_group_rows=min_group_rows,
        sample_limit=sample_limit,
    )
    written = write_option_liquidity_gate_markdown(output, result, filters=filters) if output else None
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
    print(option_liquidity_gate_markdown(result, filters=filters))
    if written:
        print(f"\nMarkdown report written: {written}")
    if json_output_path:
        print(f"JSON report written: {json_output_path}")


def command_replay_score_strategy_leg_quote_gate(
    allocation_json_path: Path,
    quote_json_path: Path,
    max_leg_spread_pct: float,
    no_fallback_to_stock: bool,
    min_group_rows: int,
    sample_limit: int,
    output: Path | None = None,
    json_output_path: Path | None = None,
    json_output: bool = False,
) -> None:
    from recorder.options_analysis import (
        apply_option_leg_quote_gate_to_allocations,
        option_leg_quote_gate_markdown,
        write_option_leg_quote_gate_markdown,
    )

    allocation_json_path = allocation_json_path.expanduser().resolve()
    quote_json_path = quote_json_path.expanduser().resolve()
    allocation_payload = json.loads(allocation_json_path.read_text(encoding="utf-8"))
    quote_snapshot = json.loads(quote_json_path.read_text(encoding="utf-8"))
    filters = {
        "allocation_json": str(allocation_json_path),
        "quote_json": str(quote_json_path),
        "max_leg_spread_pct": max_leg_spread_pct,
        "fallback_to_stock": not no_fallback_to_stock,
        "min_group_rows": min_group_rows,
        "sample_limit": sample_limit,
    }
    result = apply_option_leg_quote_gate_to_allocations(
        allocation_payload,
        quote_snapshot,
        max_leg_spread_pct=max_leg_spread_pct,
        fallback_to_stock=not no_fallback_to_stock,
        min_group_rows=min_group_rows,
        sample_limit=sample_limit,
    )
    written = write_option_leg_quote_gate_markdown(output, result, filters=filters) if output else None
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
    print(option_leg_quote_gate_markdown(result, filters=filters))
    if written:
        print(f"\nMarkdown report written: {written}")
    if json_output_path:
        print(f"JSON report written: {json_output_path}")


def command_replay_score_strategy_leg_aware_walk_forward_tune(
    settings: Settings,
    event_type: str | None,
    symbols_value: str | None,
    price_source: str | None,
    horizon: str | None,
    scorer_name: str | None,
    model_name: str | None,
    scorer_version: str | None,
    start: str | None,
    end: str | None,
    quote_json_path: Path,
    max_leg_spread_pct: float,
    no_fallback_to_stock: bool,
    min_train_rows: int,
    test_window_rows: int,
    step_rows: int | None,
    rolling_train_rows: int | None,
    underlying_reference_price: float,
    stock_quantity: float,
    stock_stop_loss_pct: float,
    min_confidence_grid: str | None,
    min_stock_expected_return_grid: str | None,
    min_option_score_grid: str | None,
    min_option_return_on_risk_grid: str | None,
    option_preference_edge_grid: str | None,
    min_dte: int,
    base_implied_vol: float,
    risk_free_rate: float,
    contract_multiplier: float,
    moneyness_offset_pct: float,
    spread_width_pct: float,
    min_selection_rate: float,
    pnl_weight: float,
    regret_penalty_weight: float,
    option_usage_penalty_weight: float,
    top_n: int,
    min_group_rows: int,
    sample_limit: int,
    limit: int,
    checkpoint_path: Path | None,
    output: Path | None = None,
    json_output_path: Path | None = None,
    json_output: bool = False,
) -> None:
    from recorder.analysis_database import AnalysisDatabase
    from recorder.options_analysis import (
        walk_forward_strategy_leg_aware_tuning,
        walk_forward_strategy_leg_aware_tuning_markdown,
        write_walk_forward_strategy_leg_aware_tuning_markdown,
    )

    symbols = tuple(item.strip().upper() for item in (symbols_value or "").split(",") if item.strip())
    quote_json_path = quote_json_path.expanduser().resolve()
    quote_snapshot = json.loads(quote_json_path.read_text(encoding="utf-8"))
    filters = {
        "event_type": event_type,
        "symbols": ",".join(symbols) if symbols else None,
        "price_source": price_source,
        "horizon": horizon.strip().upper() if horizon else None,
        "scorer_name": scorer_name,
        "model_name": model_name,
        "scorer_version": scorer_version,
        "start": start,
        "end": end,
        "limit": limit,
        "quote_json": str(quote_json_path),
        "max_leg_spread_pct": max_leg_spread_pct,
        "fallback_to_stock": not no_fallback_to_stock,
    }
    db = AnalysisDatabase(settings.database_path)
    rows = db.replay_model_score_analysis_rows(
        event_type=event_type,
        symbols=symbols or None,
        price_source=price_source,
        horizon=horizon,
        scorer_name=scorer_name,
        model_name=model_name,
        scorer_version=scorer_version,
        start=start,
        end=end,
        limit=limit,
    )
    result = walk_forward_strategy_leg_aware_tuning(
        rows,
        quote_snapshot,
        max_leg_spread_pct=max_leg_spread_pct,
        fallback_to_stock=not no_fallback_to_stock,
        min_train_rows=min_train_rows,
        test_window_rows=test_window_rows,
        step_rows=step_rows,
        rolling_train_rows=rolling_train_rows,
        underlying_reference_price=underlying_reference_price,
        stock_quantity=stock_quantity,
        stock_stop_loss_pct=stock_stop_loss_pct,
        min_confidence_grid=_parse_float_tuple(min_confidence_grid, (0.30, 0.35, 0.40)),
        min_stock_expected_return_grid=_parse_float_tuple(min_stock_expected_return_grid, (0.0025, 0.005, 0.01)),
        min_option_score_grid=_parse_float_tuple(min_option_score_grid, (0.30, 0.35, 0.40, 0.45)),
        min_option_return_on_risk_grid=_parse_float_tuple(min_option_return_on_risk_grid, (0.0, 0.10, 0.25)),
        option_preference_edge_grid=_parse_float_tuple(option_preference_edge_grid, (0.0, 0.05, 0.10, 0.20)),
        min_dte=min_dte,
        base_implied_vol=base_implied_vol,
        risk_free_rate=risk_free_rate,
        contract_multiplier=contract_multiplier,
        moneyness_offset_pct=moneyness_offset_pct,
        spread_width_pct=spread_width_pct,
        min_selection_rate=min_selection_rate,
        pnl_weight=pnl_weight,
        regret_penalty_weight=regret_penalty_weight,
        option_usage_penalty_weight=option_usage_penalty_weight,
        top_n=top_n,
        min_group_rows=min_group_rows,
        sample_limit=sample_limit,
        checkpoint_path=checkpoint_path,
    )
    written = write_walk_forward_strategy_leg_aware_tuning_markdown(output, result, filters=filters) if output else None
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
    print(walk_forward_strategy_leg_aware_tuning_markdown(result, filters=filters))
    if written:
        print(f"\nMarkdown report written: {written}")
    if json_output_path:
        print(f"JSON report written: {json_output_path}")
