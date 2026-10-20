from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from recorder.config import Settings
from recorder.cli_paper_cycle_runtime import _parse_path_list, _write_component_json
from .cli_replay_score_helpers import _parse_float_grid, _parse_float_tuple, _parse_inline_mark_prices

def command_replay_score_paper_action_sim(
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
    min_confidence: float,
    normal_size: float,
    haircut_size_multiplier: float,
    min_group_rows: int,
    limit: int,
    output: Path | None = None,
    json_output_path: Path | None = None,
    json_output: bool = False,
) -> None:
    from recorder.analysis_database import AnalysisDatabase
    from recorder.replay_scoring import (
        paper_action_simulation_markdown,
        paper_action_simulation_summary,
        write_paper_action_simulation_markdown,
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
        "min_confidence": min_confidence,
        "normal_size": normal_size,
        "haircut_size_multiplier": haircut_size_multiplier,
        "min_group_rows": min_group_rows,
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
    summary = paper_action_simulation_summary(
        rows,
        min_confidence=min_confidence,
        normal_size=normal_size,
        haircut_size_multiplier=haircut_size_multiplier,
        min_group_rows=min_group_rows,
    )
    written = write_paper_action_simulation_markdown(output, summary, filters=filters) if output else None
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
    print(paper_action_simulation_markdown(summary, filters=filters))
    if written:
        print(f"\nMarkdown report written: {written}")
    if json_output_path:
        print(f"JSON report written: {json_output_path}")


def command_replay_score_paper_orders_build(
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
    min_confidence: float,
    normal_size: float,
    haircut_size_multiplier: float,
    base_quantity: float,
    stop_loss_pct: float | None,
    take_profit_pct: float | None,
    min_group_rows: int,
    sample_limit: int,
    limit: int,
    output: Path | None = None,
    json_output_path: Path | None = None,
    json_output: bool = False,
) -> None:
    from recorder.analysis_database import AnalysisDatabase
    from recorder.replay_scoring import (
        paper_order_objects_markdown,
        paper_order_objects_summary,
        write_paper_order_objects_markdown,
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
        "min_confidence": min_confidence,
        "normal_size": normal_size,
        "haircut_size_multiplier": haircut_size_multiplier,
        "base_quantity": base_quantity,
        "stop_loss_pct": stop_loss_pct,
        "take_profit_pct": take_profit_pct,
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
    summary = paper_order_objects_summary(
        rows,
        min_confidence=min_confidence,
        normal_size=normal_size,
        haircut_size_multiplier=haircut_size_multiplier,
        base_quantity=base_quantity,
        stop_loss_pct=stop_loss_pct,
        take_profit_pct=take_profit_pct,
        min_group_rows=min_group_rows,
        sample_limit=sample_limit,
    )
    written = write_paper_order_objects_markdown(output, summary, filters=filters) if output else None
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
    print(paper_order_objects_markdown(summary, filters=filters))
    if written:
        print(f"\nMarkdown report written: {written}")
    if json_output_path:
        print(f"JSON report written: {json_output_path}")


def command_replay_score_paper_order_tune(
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
    min_confidence_grid: str | None,
    haircut_size_grid: str | None,
    normal_size_grid: str | None,
    base_quantity: float,
    min_candidate_rate: float,
    coverage_weight: float,
    weighted_return_weight: float,
    quantity_penalty_weight: float,
    top_n: int,
    limit: int,
    output: Path | None = None,
    json_output_path: Path | None = None,
    json_output: bool = False,
) -> None:
    from recorder.analysis_database import AnalysisDatabase
    from recorder.replay_scoring import (
        paper_order_parameter_tuning_markdown,
        tune_paper_order_parameters,
        write_paper_order_parameter_tuning_markdown,
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
        "min_confidence_grid": min_confidence_grid,
        "haircut_size_grid": haircut_size_grid,
        "normal_size_grid": normal_size_grid,
        "base_quantity": base_quantity,
        "min_candidate_rate": min_candidate_rate,
        "coverage_weight": coverage_weight,
        "weighted_return_weight": weighted_return_weight,
        "quantity_penalty_weight": quantity_penalty_weight,
        "top_n": top_n,
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
    result = tune_paper_order_parameters(
        rows,
        min_confidence_grid=_parse_float_tuple(min_confidence_grid, (0.0, 0.30, 0.35, 0.40, 0.45)),
        haircut_size_grid=_parse_float_tuple(haircut_size_grid, (0.25, 0.50, 0.75)),
        normal_size_grid=_parse_float_tuple(normal_size_grid, (0.75, 1.0, 1.25)),
        base_quantity=base_quantity,
        min_candidate_rate=min_candidate_rate,
        coverage_weight=coverage_weight,
        weighted_return_weight=weighted_return_weight,
        quantity_penalty_weight=quantity_penalty_weight,
        top_n=top_n,
    )
    written = write_paper_order_parameter_tuning_markdown(output, result, filters=filters) if output else None
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
    print(paper_order_parameter_tuning_markdown(result, filters=filters))
    if written:
        print(f"\nMarkdown report written: {written}")
    if json_output_path:
        print(f"JSON report written: {json_output_path}")


def command_replay_score_paper_order_walk_forward_tune(
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
    min_confidence_grid: str | None,
    haircut_size_grid: str | None,
    normal_size_grid: str | None,
    base_quantity: float,
    min_candidate_rate: float,
    coverage_weight: float,
    weighted_return_weight: float,
    quantity_penalty_weight: float,
    top_n: int,
    min_group_rows: int,
    sample_limit: int,
    limit: int,
    output: Path | None = None,
    json_output_path: Path | None = None,
    json_output: bool = False,
) -> None:
    from recorder.analysis_database import AnalysisDatabase
    from recorder.replay_scoring import (
        walk_forward_paper_order_parameter_tuning,
        walk_forward_paper_order_parameter_tuning_markdown,
        write_walk_forward_paper_order_parameter_tuning_markdown,
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
        "min_train_rows": min_train_rows,
        "test_window_rows": test_window_rows,
        "step_rows": step_rows,
        "rolling_train_rows": rolling_train_rows,
        "min_confidence_grid": min_confidence_grid,
        "haircut_size_grid": haircut_size_grid,
        "normal_size_grid": normal_size_grid,
        "base_quantity": base_quantity,
        "min_candidate_rate": min_candidate_rate,
        "coverage_weight": coverage_weight,
        "weighted_return_weight": weighted_return_weight,
        "quantity_penalty_weight": quantity_penalty_weight,
        "top_n": top_n,
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
    result = walk_forward_paper_order_parameter_tuning(
        rows,
        min_train_rows=min_train_rows,
        test_window_rows=test_window_rows,
        step_rows=step_rows,
        rolling_train_rows=rolling_train_rows,
        min_confidence_grid=_parse_float_tuple(min_confidence_grid, (0.0, 0.30, 0.35, 0.40, 0.45)),
        haircut_size_grid=_parse_float_tuple(haircut_size_grid, (0.25, 0.50, 0.75)),
        normal_size_grid=_parse_float_tuple(normal_size_grid, (0.75, 1.0, 1.25)),
        base_quantity=base_quantity,
        min_candidate_rate=min_candidate_rate,
        coverage_weight=coverage_weight,
        weighted_return_weight=weighted_return_weight,
        quantity_penalty_weight=quantity_penalty_weight,
        top_n=top_n,
        min_group_rows=min_group_rows,
        sample_limit=sample_limit,
    )
    written = write_walk_forward_paper_order_parameter_tuning_markdown(output, result, filters=filters) if output else None
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
    print(walk_forward_paper_order_parameter_tuning_markdown(result, filters=filters))
    if written:
        print(f"\nMarkdown report written: {written}")
    if json_output_path:
        print(f"JSON report written: {json_output_path}")


def command_replay_score_paper_risk_layer(
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
    min_confidence: float,
    normal_size: float,
    haircut_size_multiplier: float,
    base_quantity: float,
    max_total_gross_quantity: float | None,
    max_symbol_gross_quantity: float | None,
    max_side_gross_quantity: float | None,
    max_symbol_orders: int | None,
    max_symbol_day_orders: int | None,
    min_abs_expected_return: float | None,
    reduce_to_fit_capacity: bool,
    min_reduced_quantity: float,
    min_group_rows: int,
    sample_limit: int,
    limit: int,
    output: Path | None = None,
    json_output_path: Path | None = None,
    json_output: bool = False,
) -> None:
    from recorder.analysis_database import AnalysisDatabase
    from recorder.replay_scoring import (
        paper_order_risk_layer_markdown,
        paper_order_risk_layer_summary,
        write_paper_order_risk_layer_markdown,
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
        "min_confidence": min_confidence,
        "normal_size": normal_size,
        "haircut_size_multiplier": haircut_size_multiplier,
        "base_quantity": base_quantity,
        "max_total_gross_quantity": max_total_gross_quantity,
        "max_symbol_gross_quantity": max_symbol_gross_quantity,
        "max_side_gross_quantity": max_side_gross_quantity,
        "max_symbol_orders": max_symbol_orders,
        "max_symbol_day_orders": max_symbol_day_orders,
        "min_abs_expected_return": min_abs_expected_return,
        "reduce_to_fit_capacity": reduce_to_fit_capacity,
        "min_reduced_quantity": min_reduced_quantity,
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
    summary = paper_order_risk_layer_summary(
        rows,
        min_confidence=min_confidence,
        normal_size=normal_size,
        haircut_size_multiplier=haircut_size_multiplier,
        base_quantity=base_quantity,
        max_total_gross_quantity=max_total_gross_quantity,
        max_symbol_gross_quantity=max_symbol_gross_quantity,
        max_side_gross_quantity=max_side_gross_quantity,
        max_symbol_orders=max_symbol_orders,
        max_symbol_day_orders=max_symbol_day_orders,
        min_abs_expected_return=min_abs_expected_return,
        reduce_to_fit_capacity=reduce_to_fit_capacity,
        min_reduced_quantity=min_reduced_quantity,
        min_group_rows=min_group_rows,
        sample_limit=sample_limit,
    )
    written = write_paper_order_risk_layer_markdown(output, summary, filters=filters) if output else None
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
    print(paper_order_risk_layer_markdown(summary, filters=filters))
    if written:
        print(f"\nMarkdown report written: {written}")
    if json_output_path:
        print(f"JSON report written: {json_output_path}")


def command_replay_score_paper_portfolio_risk_layer(
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
    min_confidence: float,
    normal_size: float,
    haircut_size_multiplier: float,
    base_quantity: float,
    max_total_gross_quantity: float | None,
    max_symbol_gross_quantity: float | None,
    max_side_gross_quantity: float | None,
    max_symbol_orders: int | None,
    max_symbol_day_orders: int | None,
    min_abs_expected_return: float | None,
    starting_equity: float,
    notional_per_quantity: float,
    max_daily_gross_exposure_pct: float | None,
    max_symbol_daily_exposure_pct: float | None,
    max_sector_daily_exposure_pct: float | None,
    max_daily_loss_pct: float | None,
    max_drawdown_pct: float | None,
    max_consecutive_losses: int | None,
    loss_throttle_multiplier: float,
    min_reduced_quantity: float,
    sector_map_path: Path | None,
    min_group_rows: int,
    sample_limit: int,
    limit: int,
    output: Path | None = None,
    json_output_path: Path | None = None,
    json_output: bool = False,
) -> None:
    from recorder.analysis_database import AnalysisDatabase
    from recorder.replay_scoring import (
        paper_portfolio_risk_layer_markdown,
        paper_portfolio_risk_layer_summary,
        write_paper_portfolio_risk_layer_markdown,
    )

    symbols = tuple(item.strip().upper() for item in (symbols_value or "").split(",") if item.strip())
    sector_map = None
    if sector_map_path is not None:
        sector_map = json.loads(sector_map_path.expanduser().read_text(encoding="utf-8"))
        if not isinstance(sector_map, dict):
            raise ValueError("--sector-map-path must contain a JSON object mapping symbols to sectors")
        sector_map = {str(key).upper(): str(value) for key, value in sector_map.items()}
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
        "min_confidence": min_confidence,
        "normal_size": normal_size,
        "haircut_size_multiplier": haircut_size_multiplier,
        "base_quantity": base_quantity,
        "max_total_gross_quantity": max_total_gross_quantity,
        "max_symbol_gross_quantity": max_symbol_gross_quantity,
        "max_side_gross_quantity": max_side_gross_quantity,
        "max_symbol_orders": max_symbol_orders,
        "max_symbol_day_orders": max_symbol_day_orders,
        "min_abs_expected_return": min_abs_expected_return,
        "starting_equity": starting_equity,
        "notional_per_quantity": notional_per_quantity,
        "max_daily_gross_exposure_pct": max_daily_gross_exposure_pct,
        "max_symbol_daily_exposure_pct": max_symbol_daily_exposure_pct,
        "max_sector_daily_exposure_pct": max_sector_daily_exposure_pct,
        "max_daily_loss_pct": max_daily_loss_pct,
        "max_drawdown_pct": max_drawdown_pct,
        "max_consecutive_losses": max_consecutive_losses,
        "loss_throttle_multiplier": loss_throttle_multiplier,
        "min_reduced_quantity": min_reduced_quantity,
        "sector_map_path": str(sector_map_path) if sector_map_path else None,
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
    summary = paper_portfolio_risk_layer_summary(
        rows,
        min_confidence=min_confidence,
        normal_size=normal_size,
        haircut_size_multiplier=haircut_size_multiplier,
        base_quantity=base_quantity,
        max_total_gross_quantity=max_total_gross_quantity,
        max_symbol_gross_quantity=max_symbol_gross_quantity,
        max_side_gross_quantity=max_side_gross_quantity,
        max_symbol_orders=max_symbol_orders,
        max_symbol_day_orders=max_symbol_day_orders,
        min_abs_expected_return=min_abs_expected_return,
        starting_equity=starting_equity,
        notional_per_quantity=notional_per_quantity,
        max_daily_gross_exposure_pct=max_daily_gross_exposure_pct,
        max_symbol_daily_exposure_pct=max_symbol_daily_exposure_pct,
        max_sector_daily_exposure_pct=max_sector_daily_exposure_pct,
        max_daily_loss_pct=max_daily_loss_pct,
        max_drawdown_pct=max_drawdown_pct,
        max_consecutive_losses=max_consecutive_losses,
        loss_throttle_multiplier=loss_throttle_multiplier,
        min_reduced_quantity=min_reduced_quantity,
        sector_map=sector_map,
        min_group_rows=min_group_rows,
        sample_limit=sample_limit,
    )
    written = write_paper_portfolio_risk_layer_markdown(output, summary, filters=filters) if output else None
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
    print(paper_portfolio_risk_layer_markdown(summary, filters=filters))
    if written:
        print(f"\nMarkdown report written: {written}")
    if json_output_path:
        print(f"JSON report written: {json_output_path}")


def command_replay_score_paper_order_router(
    intent_ledger_json_path: Path,
    existing_router_json_path: Path | None,
    marks_json_path: Path | None,
    operating_auto_gate_json_path: Path | None,
    session_transition_release_governor_json_path: Path | None,
    require_operating_preflight: bool,
    allow_non_operating_source_ledger: bool,
    require_session_transition_release_governor: bool,
    fill_price_source: str,
    fill_model: str,
    require_capital_aware_gate: bool,
    require_portfolio_risk_gate: bool,
    max_orders: int,
    slippage_bps: float,
    commission_per_order: float,
    router_run_id: str | None,
    sample_limit: int,
    output: Path | None = None,
    json_output_path: Path | None = None,
    json_output: bool = False,
) -> None:
    from recorder.options_analysis import (
        build_paper_order_router_report,
        paper_order_router_markdown,
        write_paper_order_router_markdown,
    )

    intent_ledger_json_path = intent_ledger_json_path.expanduser().resolve()
    intent_ledger_payload = json.loads(intent_ledger_json_path.read_text(encoding="utf-8"))
    existing_payload = None
    if existing_router_json_path is not None:
        existing_router_json_path = existing_router_json_path.expanduser().resolve()
        existing_payload = json.loads(existing_router_json_path.read_text(encoding="utf-8"))
    marks_payload = None
    if marks_json_path is not None:
        marks_json_path = marks_json_path.expanduser().resolve()
        marks_payload = json.loads(marks_json_path.read_text(encoding="utf-8"))
    operating_auto_gate_payload = None
    if operating_auto_gate_json_path is not None:
        operating_auto_gate_json_path = operating_auto_gate_json_path.expanduser().resolve()
        operating_auto_gate_payload = json.loads(operating_auto_gate_json_path.read_text(encoding="utf-8"))
    session_transition_release_governor_payload = None
    if session_transition_release_governor_json_path is not None:
        session_transition_release_governor_json_path = session_transition_release_governor_json_path.expanduser().resolve()
        if session_transition_release_governor_json_path.exists():
            session_transition_release_governor_payload = json.loads(session_transition_release_governor_json_path.read_text(encoding="utf-8"))
    filters = {
        "intent_ledger_json": str(intent_ledger_json_path),
        "existing_router_json": str(existing_router_json_path) if existing_router_json_path else None,
        "marks_json": str(marks_json_path) if marks_json_path else None,
        "operating_auto_gate_json": str(operating_auto_gate_json_path) if operating_auto_gate_json_path else None,
        "session_transition_release_governor_json": str(session_transition_release_governor_json_path) if session_transition_release_governor_json_path else None,
        "require_operating_preflight": require_operating_preflight,
        "allow_non_operating_source_ledger": allow_non_operating_source_ledger,
        "require_session_transition_release_governor": require_session_transition_release_governor,
        "fill_price_source": fill_price_source,
        "fill_model": fill_model,
        "require_capital_aware_gate": require_capital_aware_gate,
        "require_portfolio_risk_gate": require_portfolio_risk_gate,
        "max_orders": max_orders,
        "slippage_bps": slippage_bps,
        "commission_per_order": commission_per_order,
        "router_run_id": router_run_id,
        "sample_limit": sample_limit,
    }
    result = build_paper_order_router_report(
        intent_ledger_payload,
        existing_router_payload=existing_payload,
        marks_payload=marks_payload,
        operating_auto_gate_payload=operating_auto_gate_payload,
        session_transition_release_governor_payload=session_transition_release_governor_payload,
        require_operating_preflight=require_operating_preflight,
        allow_non_operating_source_ledger=allow_non_operating_source_ledger,
        require_session_transition_release_governor=require_session_transition_release_governor,
        fill_price_source=fill_price_source,
        fill_model=fill_model,
        require_capital_aware_gate=require_capital_aware_gate,
        require_portfolio_risk_gate=require_portfolio_risk_gate,
        max_orders=max_orders,
        slippage_bps=slippage_bps,
        commission_per_order=commission_per_order,
        router_run_id=router_run_id,
        sample_limit=sample_limit,
    )
    written = write_paper_order_router_markdown(output, result, filters=filters) if output else None
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
    print(paper_order_router_markdown(result, filters=filters))
    if written:
        print(f"\nMarkdown report written: {written}")
    if json_output_path:
        print(f"JSON report written: {json_output_path}")


def command_replay_score_paper_position_mark_timeseries(
    router_json_path: Path,
    marks_jsons_value: str,
    take_profit_pct: float,
    stop_loss_pct: float,
    max_loss_dollars: float | None,
    series_run_id: str | None,
    sample_limit: int,
    output: Path | None = None,
    json_output_path: Path | None = None,
    json_output: bool = False,
) -> None:
    from recorder.options_analysis import (
        build_paper_position_mark_timeseries_report,
        paper_position_mark_timeseries_markdown,
        write_paper_position_mark_timeseries_markdown,
    )

    router_json_path = router_json_path.expanduser().resolve()
    router_payload = json.loads(router_json_path.read_text(encoding="utf-8"))
    mark_paths = _parse_path_list(marks_jsons_value)
    if not mark_paths:
        raise ValueError("--marks-jsons must contain at least one JSON path")
    resolved_mark_paths: list[str] = []
    mark_payloads: list[dict[str, Any]] = []
    for path in mark_paths:
        resolved = path.expanduser().resolve()
        resolved_mark_paths.append(str(resolved))
        mark_payloads.append(json.loads(resolved.read_text(encoding="utf-8")))
    filters = {
        "router_json": str(router_json_path),
        "marks_jsons": ",".join(resolved_mark_paths),
        "take_profit_pct": take_profit_pct,
        "stop_loss_pct": stop_loss_pct,
        "max_loss_dollars": max_loss_dollars,
        "series_run_id": series_run_id,
        "sample_limit": sample_limit,
    }
    result = build_paper_position_mark_timeseries_report(
        router_payload,
        mark_payloads,
        take_profit_pct=take_profit_pct,
        stop_loss_pct=stop_loss_pct,
        max_loss_dollars=max_loss_dollars,
        series_run_id=series_run_id,
        sample_limit=sample_limit,
    )
    written = write_paper_position_mark_timeseries_markdown(output, result, filters=filters) if output else None
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
    print(paper_position_mark_timeseries_markdown(result, filters=filters))
    if written:
        print(f"\nMarkdown report written: {written}")
    if json_output_path:
        print(f"JSON report written: {json_output_path}")


def command_replay_score_paper_position_mark(
    router_json_path: Path,
    marks_json_path: Path | None,
    inline_marks_value: str | None,
    take_profit_pct: float,
    stop_loss_pct: float,
    max_loss_dollars: float | None,
    mark_run_id: str | None,
    sample_limit: int,
    output: Path | None = None,
    json_output_path: Path | None = None,
    json_output: bool = False,
) -> None:
    from recorder.options_analysis import (
        build_paper_position_mark_report,
        paper_position_mark_markdown,
        write_paper_position_mark_markdown,
    )

    router_json_path = router_json_path.expanduser().resolve()
    router_payload = json.loads(router_json_path.read_text(encoding="utf-8"))
    marks_payload = None
    if marks_json_path is not None:
        marks_json_path = marks_json_path.expanduser().resolve()
        marks_payload = json.loads(marks_json_path.read_text(encoding="utf-8"))
    inline_marks = _parse_inline_mark_prices(inline_marks_value)
    filters = {
        "router_json": str(router_json_path),
        "marks_json": str(marks_json_path) if marks_json_path else None,
        "marks": inline_marks_value,
        "take_profit_pct": take_profit_pct,
        "stop_loss_pct": stop_loss_pct,
        "max_loss_dollars": max_loss_dollars,
        "mark_run_id": mark_run_id,
        "sample_limit": sample_limit,
    }
    result = build_paper_position_mark_report(
        router_payload,
        marks_payload=marks_payload,
        inline_marks=inline_marks,
        take_profit_pct=take_profit_pct,
        stop_loss_pct=stop_loss_pct,
        max_loss_dollars=max_loss_dollars,
        mark_run_id=mark_run_id,
        sample_limit=sample_limit,
    )
    written = write_paper_position_mark_markdown(output, result, filters=filters) if output else None
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
    print(paper_position_mark_markdown(result, filters=filters))
    if written:
        print(f"\nMarkdown report written: {written}")
    if json_output_path:
        print(f"JSON report written: {json_output_path}")


def command_replay_score_paper_exit_intent_ledger(
    timeseries_json_path: Path,
    existing_ledger_json_path: Path | None,
    exit_signals_value: str,
    max_intents: int,
    ledger_run_id: str | None,
    sample_limit: int,
    output: Path | None = None,
    json_output_path: Path | None = None,
    json_output: bool = False,
) -> None:
    from recorder.options_analysis import (
        build_paper_exit_intent_ledger_report,
        paper_exit_intent_ledger_markdown,
        write_paper_exit_intent_ledger_markdown,
    )

    timeseries_json_path = timeseries_json_path.expanduser().resolve()
    timeseries_payload = json.loads(timeseries_json_path.read_text(encoding="utf-8"))
    existing_payload = None
    if existing_ledger_json_path is not None:
        existing_ledger_json_path = existing_ledger_json_path.expanduser().resolve()
        existing_payload = json.loads(existing_ledger_json_path.read_text(encoding="utf-8"))
    exit_signals = tuple(item.strip() for item in exit_signals_value.split(",") if item.strip())
    filters = {
        "timeseries_json": str(timeseries_json_path),
        "existing_ledger_json": str(existing_ledger_json_path) if existing_ledger_json_path else None,
        "exit_signals": ",".join(exit_signals),
        "max_intents": max_intents,
        "ledger_run_id": ledger_run_id,
        "sample_limit": sample_limit,
    }
    result = build_paper_exit_intent_ledger_report(
        timeseries_payload,
        existing_exit_ledger_payload=existing_payload,
        exit_signals=exit_signals,
        max_intents=max_intents,
        ledger_run_id=ledger_run_id,
        sample_limit=sample_limit,
    )
    written = write_paper_exit_intent_ledger_markdown(output, result, filters=filters) if output else None
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
    print(paper_exit_intent_ledger_markdown(result, filters=filters))
    if written:
        print(f"\nMarkdown report written: {written}")
    if json_output_path:
        print(f"JSON report written: {json_output_path}")


def command_replay_score_paper_exit_router(
    exit_ledger_json_path: Path,
    existing_router_json_path: Path | None,
    fill_model: str,
    max_exits: int,
    slippage_bps: float,
    commission_per_exit: float,
    router_run_id: str | None,
    sample_limit: int,
    output: Path | None = None,
    json_output_path: Path | None = None,
    json_output: bool = False,
) -> None:
    from recorder.options_analysis import (
        build_paper_exit_router_report,
        paper_exit_router_markdown,
        write_paper_exit_router_markdown,
    )

    exit_ledger_json_path = exit_ledger_json_path.expanduser().resolve()
    exit_ledger_payload = json.loads(exit_ledger_json_path.read_text(encoding="utf-8"))
    existing_payload = None
    if existing_router_json_path is not None:
        existing_router_json_path = existing_router_json_path.expanduser().resolve()
        existing_payload = json.loads(existing_router_json_path.read_text(encoding="utf-8"))
    filters = {
        "exit_ledger_json": str(exit_ledger_json_path),
        "existing_router_json": str(existing_router_json_path) if existing_router_json_path else None,
        "fill_model": fill_model,
        "max_exits": max_exits,
        "slippage_bps": slippage_bps,
        "commission_per_exit": commission_per_exit,
        "router_run_id": router_run_id,
        "sample_limit": sample_limit,
    }
    result = build_paper_exit_router_report(
        exit_ledger_payload,
        existing_exit_router_payload=existing_payload,
        fill_model=fill_model,
        max_exits=max_exits,
        slippage_bps=slippage_bps,
        commission_per_exit=commission_per_exit,
        router_run_id=router_run_id,
        sample_limit=sample_limit,
    )
    written = write_paper_exit_router_markdown(output, result, filters=filters) if output else None
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
    print(paper_exit_router_markdown(result, filters=filters))
    if written:
        print(f"\nMarkdown report written: {written}")
    if json_output_path:
        print(f"JSON report written: {json_output_path}")


def command_replay_score_paper_portfolio_state(
    entry_router_json_path: Path,
    exit_router_json_path: Path | None,
    timeseries_json_path: Path | None,
    state_run_id: str | None,
    sample_limit: int,
    output: Path | None = None,
    json_output_path: Path | None = None,
    json_output: bool = False,
) -> None:
    from recorder.options_analysis import (
        build_paper_portfolio_state_report,
        paper_portfolio_state_markdown,
        write_paper_portfolio_state_markdown,
    )

    entry_router_json_path = entry_router_json_path.expanduser().resolve()
    entry_payload = json.loads(entry_router_json_path.read_text(encoding="utf-8"))
    exit_payload = None
    if exit_router_json_path is not None:
        exit_router_json_path = exit_router_json_path.expanduser().resolve()
        exit_payload = json.loads(exit_router_json_path.read_text(encoding="utf-8"))
    timeseries_payload = None
    if timeseries_json_path is not None:
        timeseries_json_path = timeseries_json_path.expanduser().resolve()
        timeseries_payload = json.loads(timeseries_json_path.read_text(encoding="utf-8"))
    filters = {
        "entry_router_json": str(entry_router_json_path),
        "exit_router_json": str(exit_router_json_path) if exit_router_json_path else None,
        "timeseries_json": str(timeseries_json_path) if timeseries_json_path else None,
        "state_run_id": state_run_id,
        "sample_limit": sample_limit,
    }
    result = build_paper_portfolio_state_report(
        entry_payload,
        exit_router_payload=exit_payload,
        latest_timeseries_payload=timeseries_payload,
        state_run_id=state_run_id,
        sample_limit=sample_limit,
    )
    written = write_paper_portfolio_state_markdown(output, result, filters=filters) if output else None
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
    print(paper_portfolio_state_markdown(result, filters=filters))
    if written:
        print(f"\nMarkdown report written: {written}")
    if json_output_path:
        print(f"JSON report written: {json_output_path}")


def command_replay_score_paper_portfolio_risk_state(
    portfolio_state_json_path: Path,
    max_gross_exposure: float,
    max_symbol_exposure: float,
    max_short_exposure: float,
    max_unrealized_loss: float,
    max_total_loss: float,
    max_open_symbols: int,
    warning_ratio: float,
    risk_run_id: str | None,
    sample_limit: int,
    output: Path | None = None,
    json_output_path: Path | None = None,
    json_output: bool = False,
) -> None:
    from recorder.options_analysis import (
        build_paper_portfolio_risk_state_report,
        paper_portfolio_risk_state_markdown,
        write_paper_portfolio_risk_state_markdown,
    )

    portfolio_state_json_path = portfolio_state_json_path.expanduser().resolve()
    portfolio_state_payload = json.loads(portfolio_state_json_path.read_text(encoding="utf-8"))
    filters = {
        "portfolio_state_json": str(portfolio_state_json_path),
        "max_gross_exposure": max_gross_exposure,
        "max_symbol_exposure": max_symbol_exposure,
        "max_short_exposure": max_short_exposure,
        "max_unrealized_loss": max_unrealized_loss,
        "max_total_loss": max_total_loss,
        "max_open_symbols": max_open_symbols,
        "warning_ratio": warning_ratio,
        "risk_run_id": risk_run_id,
        "sample_limit": sample_limit,
    }
    result = build_paper_portfolio_risk_state_report(
        portfolio_state_payload,
        max_gross_exposure=max_gross_exposure,
        max_symbol_exposure=max_symbol_exposure,
        max_short_exposure=max_short_exposure,
        max_unrealized_loss=max_unrealized_loss,
        max_total_loss=max_total_loss,
        max_open_symbols=max_open_symbols,
        warning_ratio=warning_ratio,
        risk_run_id=risk_run_id,
        sample_limit=sample_limit,
    )
    written = write_paper_portfolio_risk_state_markdown(output, result, filters=filters) if output else None
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
    print(paper_portfolio_risk_state_markdown(result, filters=filters))
    if written:
        print(f"\nMarkdown report written: {written}")
    if json_output_path:
        print(f"JSON report written: {json_output_path}")


def command_replay_score_paper_portfolio_risk_loop(
    entry_router_json_path: Path,
    marks_jsons_value: str,
    exit_router_json_path: Path | None,
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
    monitor_size_multiplier: float,
    loop_run_id: str | None,
    sample_limit: int,
    output: Path | None = None,
    json_output_path: Path | None = None,
    timeseries_json_output_path: Path | None = None,
    portfolio_state_json_output_path: Path | None = None,
    portfolio_risk_json_output_path: Path | None = None,
    json_output: bool = False,
) -> None:
    from recorder.options_analysis import (
        build_paper_portfolio_risk_loop_report,
        paper_portfolio_risk_loop_markdown,
        write_paper_portfolio_risk_loop_markdown,
    )

    entry_router_json_path = entry_router_json_path.expanduser().resolve()
    entry_payload = json.loads(entry_router_json_path.read_text(encoding="utf-8"))
    mark_paths = [path.expanduser().resolve() for path in _parse_path_list(marks_jsons_value)]
    if not mark_paths:
        raise ValueError("--marks-jsons must include at least one mark snapshot JSON path")
    mark_payloads = [json.loads(path.read_text(encoding="utf-8")) for path in mark_paths]
    exit_payload = None
    if exit_router_json_path is not None:
        exit_router_json_path = exit_router_json_path.expanduser().resolve()
        exit_payload = json.loads(exit_router_json_path.read_text(encoding="utf-8"))
    filters = {
        "entry_router_json": str(entry_router_json_path),
        "marks_jsons": ",".join(str(path) for path in mark_paths),
        "exit_router_json": str(exit_router_json_path) if exit_router_json_path else None,
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
        "monitor_size_multiplier": monitor_size_multiplier,
        "loop_run_id": loop_run_id,
        "sample_limit": sample_limit,
    }
    result = build_paper_portfolio_risk_loop_report(
        entry_payload,
        mark_payloads,
        exit_router_payload=exit_payload,
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
        monitor_size_multiplier=monitor_size_multiplier,
        loop_run_id=loop_run_id,
        sample_limit=sample_limit,
    )
    written = write_paper_portfolio_risk_loop_markdown(output, result, filters=filters) if output else None
    component_outputs = {
        "timeseries_json_output": str(_write_component_json(timeseries_json_output_path, result["mark_timeseries"], filters) or ""),
        "portfolio_state_json_output": str(_write_component_json(portfolio_state_json_output_path, result["portfolio_state"], filters) or ""),
        "portfolio_risk_json_output": str(_write_component_json(portfolio_risk_json_output_path, result["portfolio_risk_state"], filters) or ""),
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
    print(paper_portfolio_risk_loop_markdown(result, filters=filters))
    if written:
        print(f"\nMarkdown report written: {written}")
    if json_output_path:
        print(f"JSON report written: {json_output_path}")
    for label, path_text in component_outputs.items():
        if path_text:
            print(f"{label} written: {path_text}")
