from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from recorder.config import Settings
from recorder.cli_paper_cycle_runtime import _parse_path_list, _write_component_json
from .cli_replay_score_helpers import _parse_float_grid, _parse_float_tuple, _parse_inline_mark_prices

def command_replay_score_build(
    settings: Settings,
    event_type: str | None,
    symbol: str | None,
    price_source: str | None,
    horizon: str | None,
    start: str | None,
    end: str | None,
    limit: int,
    scorer: str,
    openai_model: str | None,
    openai_scorer_version: str | None,
    reasoning_effort: str,
    max_input_chars: int,
    only_missing: bool,
) -> None:
    from recorder.ai_provider import PermanentAnalysisError, TransientAnalysisError
    from recorder.analysis_database import AnalysisDatabase
    from recorder.replay_scoring import HeuristicReplayScorer, OpenAIReplayScorer, ReplayScoringEngine

    normalized_scorer = scorer.strip().lower()
    db = AnalysisDatabase(settings.database_path)
    if normalized_scorer == "heuristic":
        scorer_instance = HeuristicReplayScorer()
    elif normalized_scorer == "openai":
        scorer_instance = OpenAIReplayScorer(
            api_key=settings.openai_api_key or "",
            model=openai_model or settings.ai_model,
            timeout_seconds=settings.ai_api_timeout_seconds,
            reasoning_effort=reasoning_effort,
            max_input_chars=max_input_chars,
            scorer_version=openai_scorer_version or None,
        )
    else:
        raise SystemExit(f"Unsupported replay scorer: {scorer}")
    try:
        result = ReplayScoringEngine(db, scorer_instance).build(
            event_type=event_type,
            symbol=symbol,
            price_source=price_source,
            horizon=horizon,
            start=start,
            end=end,
            limit=limit,
            only_missing=only_missing,
        )
    except PermanentAnalysisError as exc:
        raise SystemExit(f"Replay scoring configuration error: {exc}") from exc
    except TransientAnalysisError as exc:
        raise SystemExit(f"Replay scoring transient error: {exc}") from exc
    print(json.dumps(result, indent=2, ensure_ascii=False))


def command_replay_score_postscore_transform(
    settings: Settings,
    event_type: str | None,
    symbols: str | None,
    price_source: str | None,
    horizon: str | None,
    start: str | None,
    end: str | None,
    limit: int,
    base_scorer_name: str,
    base_model_name: str | None,
    base_scorer_version: str,
    target_scorer_version: str,
    signal_limit: int,
    nuanced_policy_path: Path | None,
    only_missing: bool,
) -> None:
    from recorder.analysis_database import AnalysisDatabase
    from recorder.replay_scoring import ReplayPostscoreTransformer

    db = AnalysisDatabase(settings.database_path)
    symbol_tuple = tuple(item.strip().upper() for item in (symbols or "").split(",") if item.strip()) or None
    result = ReplayPostscoreTransformer(
        db,
        base_scorer_name=base_scorer_name,
        base_model_name=base_model_name,
        base_scorer_version=base_scorer_version,
        target_scorer_version=target_scorer_version,
        signal_limit=signal_limit,
        nuanced_policy_path=nuanced_policy_path,
    ).build(
        event_type=event_type,
        symbols=symbol_tuple,
        price_source=price_source,
        horizon=horizon,
        start=start,
        end=end,
        limit=limit,
        only_missing=only_missing,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))


def command_replay_score_postscore_threshold_tune(
    settings: Settings,
    event_type: str | None,
    symbols: str | None,
    price_source: str | None,
    horizon: str | None,
    start: str | None,
    end: str | None,
    limit: int,
    base_scorer_name: str,
    base_model_name: str | None,
    base_scorer_version: str,
    signal_limit: int,
    overlay_score_grid: str | None,
    conflict_score_grid: str | None,
    confirmation_score_grid: str | None,
    age_hours_grid: str | None,
    confirmation_conflict_grid: str | None,
    min_directional_coverage: float,
    top_n: int,
    output: Path | None,
    json_output: bool,
) -> None:
    from recorder.analysis_database import AnalysisDatabase
    from recorder.replay_scoring import (
        postscore_threshold_tuning_markdown,
        postscore_threshold_tuning_summary,
        write_postscore_threshold_tuning_markdown,
    )

    db = AnalysisDatabase(settings.database_path)
    symbol_tuple = tuple(item.strip().upper() for item in (symbols or "").split(",") if item.strip()) or None
    rows = db.replay_model_score_analysis_rows(
        event_type=event_type,
        symbols=symbol_tuple,
        price_source=price_source,
        horizon=horizon,
        scorer_name=base_scorer_name,
        model_name=base_model_name,
        scorer_version=base_scorer_version,
        start=start,
        end=end,
        limit=limit,
    )
    signals = db.recent_external_market_signals(limit=signal_limit) if hasattr(db, "recent_external_market_signals") else []
    filters = {
        "event_type": event_type,
        "symbols": ",".join(symbol_tuple) if symbol_tuple else None,
        "price_source": price_source,
        "horizon": horizon.strip().upper() if horizon else None,
        "base_scorer_name": base_scorer_name,
        "base_model_name": base_model_name,
        "base_scorer_version": base_scorer_version,
        "signal_limit": signal_limit,
        "limit": limit,
        "min_directional_coverage": min_directional_coverage,
    }
    summary = postscore_threshold_tuning_summary(
        rows,
        signals,
        db,
        overlay_score_grid=_parse_float_grid(overlay_score_grid, (4.5, 5.5, 6.5, 7.5)),
        conflict_score_grid=_parse_float_grid(conflict_score_grid, (0.40, 0.50, 0.67)),
        confirmation_score_grid=_parse_float_grid(confirmation_score_grid, (0.40, 0.50, 0.67)),
        age_hours_grid=_parse_float_grid(age_hours_grid, (1.0, 4.0, 24.0)),
        confirmation_conflict_grid=_parse_float_grid(confirmation_conflict_grid, (0.25, 0.40)),
        min_directional_coverage=min_directional_coverage,
        top_n=top_n,
    )
    if json_output:
        print(json.dumps({"filters": filters, "summary": summary}, indent=2, ensure_ascii=False))
        return
    if output:
        written = write_postscore_threshold_tuning_markdown(output, summary, filters=filters)
        print(f"Markdown report written: {written}")
        return
    print(postscore_threshold_tuning_markdown(summary, filters=filters))


def command_replay_score_report(
    settings: Settings,
    event_type: str | None,
    symbol: str | None,
    scorer_name: str | None,
    limit: int,
    json_output: bool = False,
) -> None:
    from recorder.analysis_database import AnalysisDatabase
    from recorder.replay_scoring import format_score_report, summarize_scores

    db = AnalysisDatabase(settings.database_path)
    rows = db.recent_replay_model_scores(
        limit=limit,
        event_type=event_type,
        symbol=symbol,
        scorer_name=scorer_name,
    )
    if json_output:
        print(json.dumps({"summary": summarize_scores(rows), "scores": rows}, indent=2, ensure_ascii=False))
        return
    print(format_score_report(rows))


def command_replay_score_leaderboard(
    settings: Settings,
    event_type: str | None,
    symbol: str | None,
    price_source: str | None,
    horizon: str | None,
    scorer_name: str | None,
    min_rows: int,
    limit: int,
    output: Path | None = None,
    json_output: bool = False,
) -> None:
    from recorder.analysis_database import AnalysisDatabase
    from recorder.replay_scoring import (
        format_leaderboard,
        write_leaderboard_markdown,
    )

    filters = {
        "event_type": event_type,
        "symbol": symbol.strip().upper() if symbol else None,
        "price_source": price_source,
        "horizon": horizon.strip().upper() if horizon else None,
        "scorer_name": scorer_name,
        "min_rows": min_rows,
        "limit": limit,
    }
    db = AnalysisDatabase(settings.database_path)
    rows = db.replay_score_leaderboard_rows(
        event_type=event_type,
        symbol=symbol,
        price_source=price_source,
        horizon=horizon,
        scorer_name=scorer_name,
        min_rows=min_rows,
        limit=limit,
    )
    if output is not None:
        written = write_leaderboard_markdown(output, rows, filters=filters)
    else:
        written = None
    if json_output:
        print(
            json.dumps(
                {
                    "leaderboard": rows,
                    "filters": filters,
                    "output": str(written) if written else None,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return
    print(format_leaderboard(rows))
    if written:
        print(f"\nMarkdown report written: {written}")


def command_replay_score_calibration(
    settings: Settings,
    event_type: str | None,
    symbol: str | None,
    price_source: str | None,
    horizon: str | None,
    scorer_name: str | None,
    model_name: str | None,
    scorer_version: str | None,
    limit: int,
    output: Path | None = None,
    json_output: bool = False,
) -> None:
    from recorder.analysis_database import AnalysisDatabase
    from recorder.replay_scoring import (
        calibration_markdown,
        calibration_summary,
        write_calibration_markdown,
    )

    filters = {
        "event_type": event_type,
        "symbol": symbol.strip().upper() if symbol else None,
        "price_source": price_source,
        "horizon": horizon.strip().upper() if horizon else None,
        "scorer_name": scorer_name,
        "model_name": model_name,
        "scorer_version": scorer_version,
        "limit": limit,
    }
    db = AnalysisDatabase(settings.database_path)
    rows = db.replay_model_score_rows(
        event_type=event_type,
        symbol=symbol,
        price_source=price_source,
        horizon=horizon,
        scorer_name=scorer_name,
        model_name=model_name,
        scorer_version=scorer_version,
        limit=limit,
    )
    if output is not None:
        written = write_calibration_markdown(output, rows, filters=filters)
    else:
        written = None
    if json_output:
        print(
            json.dumps(
                {
                    "summary": calibration_summary(rows),
                    "filters": filters,
                    "output": str(written) if written else None,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return
    print(calibration_markdown(rows, filters=filters))
    if written:
        print(f"\nMarkdown report written: {written}")


def command_replay_score_oos(
    settings: Settings,
    event_type: str | None,
    symbol: str | None,
    price_source: str | None,
    horizon: str | None,
    scorer_name: str | None,
    model_name: str | None,
    scorer_version: str | None,
    cutoff: str,
    min_holdout_rows: int,
    limit: int,
    output: Path | None = None,
    json_output: bool = False,
) -> None:
    from recorder.analysis_database import AnalysisDatabase
    from recorder.replay_scoring import (
        format_oos_summary,
        oos_summary,
        write_oos_markdown,
    )

    filters = {
        "event_type": event_type,
        "symbol": symbol.strip().upper() if symbol else None,
        "price_source": price_source,
        "horizon": horizon.strip().upper() if horizon else None,
        "scorer_name": scorer_name,
        "model_name": model_name,
        "scorer_version": scorer_version,
        "cutoff": cutoff,
        "min_holdout_rows": min_holdout_rows,
        "limit": limit,
    }
    db = AnalysisDatabase(settings.database_path)
    rows = db.replay_model_score_rows(
        event_type=event_type,
        symbol=symbol,
        price_source=price_source,
        horizon=horizon,
        scorer_name=scorer_name,
        model_name=model_name,
        scorer_version=scorer_version,
        limit=limit,
    )
    if output is not None:
        written = write_oos_markdown(
            output,
            rows,
            cutoff=cutoff,
            min_holdout_rows=min_holdout_rows,
            filters=filters,
        )
    else:
        written = None
    if json_output:
        print(
            json.dumps(
                {
                    "summary": oos_summary(rows, cutoff=cutoff, min_holdout_rows=min_holdout_rows),
                    "filters": filters,
                    "output": str(written) if written else None,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return
    summary = oos_summary(rows, cutoff=cutoff, min_holdout_rows=min_holdout_rows)
    print(format_oos_summary(summary))
    if output is not None:
        print(f"\nMarkdown report written: {written}")


def command_replay_score_pair_compare(
    settings: Settings,
    event_type: str | None,
    symbols_value: str | None,
    price_source: str | None,
    horizon: str | None,
    base_scorer_name: str,
    base_model_name: str | None,
    base_scorer_version: str,
    candidate_scorer_name: str,
    candidate_model_name: str | None,
    candidate_scorer_version: str,
    start: str | None,
    end: str | None,
    min_group_rows: int,
    top_deltas: int,
    limit: int,
    output: Path | None = None,
    json_output: bool = False,
) -> None:
    from recorder.analysis_database import AnalysisDatabase
    from recorder.replay_scoring import (
        format_pair_comparison,
        pair_comparison_markdown,
        pair_comparison_summary,
        write_pair_comparison_markdown,
    )

    symbols = tuple(item.strip().upper() for item in (symbols_value or "").split(",") if item.strip())
    filters = {
        "event_type": event_type,
        "symbols": ",".join(symbols) if symbols else None,
        "price_source": price_source,
        "horizon": horizon.strip().upper() if horizon else None,
        "base_scorer_name": base_scorer_name,
        "base_model_name": base_model_name,
        "base_scorer_version": base_scorer_version,
        "candidate_scorer_name": candidate_scorer_name,
        "candidate_model_name": candidate_model_name,
        "candidate_scorer_version": candidate_scorer_version,
        "start": start,
        "end": end,
        "min_group_rows": min_group_rows,
        "top_deltas": top_deltas,
        "limit": limit,
    }
    db = AnalysisDatabase(settings.database_path)
    rows = db.replay_score_pair_comparison_rows(
        event_type=event_type,
        symbols=symbols or None,
        price_source=price_source,
        horizon=horizon,
        base_scorer_name=base_scorer_name,
        base_model_name=base_model_name,
        base_scorer_version=base_scorer_version,
        candidate_scorer_name=candidate_scorer_name,
        candidate_model_name=candidate_model_name,
        candidate_scorer_version=candidate_scorer_version,
        start=start,
        end=end,
        limit=limit,
    )
    if output is not None:
        written = write_pair_comparison_markdown(
            output,
            rows,
            filters=filters,
            min_group_rows=min_group_rows,
            top_deltas=top_deltas,
        )
    else:
        written = None
    if json_output:
        print(
            json.dumps(
                {
                    "summary": pair_comparison_summary(
                        rows,
                        min_group_rows=min_group_rows,
                        top_deltas=top_deltas,
                    ),
                    "filters": filters,
                    "output": str(written) if written else None,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return
    summary = pair_comparison_summary(rows, min_group_rows=min_group_rows, top_deltas=top_deltas)
    print(format_pair_comparison(summary))
    if output is not None:
        print(f"\nMarkdown report written: {written}")
    else:
        print(pair_comparison_markdown(rows, filters=filters, min_group_rows=min_group_rows, top_deltas=top_deltas))


def command_replay_score_variant_compare(
    settings: Settings,
    event_type: str | None,
    symbols_value: str | None,
    price_source: str | None,
    horizon: str | None,
    scorer_name: str,
    model_name: str | None,
    scorer_versions: str,
    base_scorer_version: str | None,
    start: str | None,
    end: str | None,
    min_group_rows: int,
    limit: int,
    output: Path | None = None,
    json_output: bool = False,
) -> None:
    from recorder.analysis_database import AnalysisDatabase
    from recorder.replay_scoring import (
        variant_comparison_markdown,
        variant_comparison_summary,
        write_variant_comparison_markdown,
    )

    symbols = tuple(item.strip().upper() for item in (symbols_value or "").split(",") if item.strip())
    versions = [item.strip() for item in str(scorer_versions or "").split(",") if item.strip()]
    if not versions:
        raise SystemExit("--scorer-versions must include at least one stored scorer version")
    base_version = base_scorer_version or versions[0]
    filters = {
        "event_type": event_type,
        "symbols": ",".join(symbols) if symbols else None,
        "price_source": price_source,
        "horizon": horizon.strip().upper() if horizon else None,
        "scorer_name": scorer_name,
        "model_name": model_name,
        "scorer_versions": ",".join(versions),
        "base_scorer_version": base_version,
        "start": start,
        "end": end,
        "min_group_rows": min_group_rows,
        "limit_per_variant": limit,
    }
    db = AnalysisDatabase(settings.database_path)
    rows_by_variant: dict[str, list[dict[str, Any]]] = {}
    for version in versions:
        rows_by_variant[version] = db.replay_model_score_analysis_rows(
            event_type=event_type,
            symbols=symbols or None,
            price_source=price_source,
            horizon=horizon,
            scorer_name=scorer_name,
            model_name=model_name,
            scorer_version=version,
            start=start,
            end=end,
            limit=limit,
        )
    summary = variant_comparison_summary(
        rows_by_variant,
        base_variant=base_version,
        min_group_rows=min_group_rows,
    )
    if output is not None:
        written = write_variant_comparison_markdown(output, summary, filters=filters)
    else:
        written = None
    if json_output:
        print(
            json.dumps(
                {
                    "summary": summary,
                    "filters": filters,
                    "output": str(written) if written else None,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return
    print(variant_comparison_markdown(summary, filters=filters))
    if written:
        print(f"\nMarkdown report written: {written}")


def command_replay_score_error_analysis(
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
    min_group_rows: int,
    top_misses: int,
    limit: int,
    output: Path | None = None,
    json_output: bool = False,
) -> None:
    from recorder.analysis_database import AnalysisDatabase
    from recorder.replay_scoring import (
        error_analysis_markdown,
        error_analysis_summary,
        format_error_analysis,
        write_error_analysis_markdown,
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
        "min_group_rows": min_group_rows,
        "top_misses": top_misses,
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
    if output is not None:
        written = write_error_analysis_markdown(
            output,
            rows,
            filters=filters,
            min_group_rows=min_group_rows,
            top_misses=top_misses,
        )
    else:
        written = None
    if json_output:
        print(
            json.dumps(
                {
                    "summary": error_analysis_summary(rows, min_group_rows=min_group_rows, top_misses=top_misses),
                    "filters": filters,
                    "output": str(written) if written else None,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return
    summary = error_analysis_summary(rows, min_group_rows=min_group_rows, top_misses=top_misses)
    print(format_error_analysis(summary))
    if output is not None:
        print(f"\nMarkdown report written: {written}")
    else:
        print(error_analysis_markdown(rows, filters=filters, min_group_rows=min_group_rows, top_misses=top_misses))


def command_replay_score_action_gate(
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
    min_group_rows: int,
    limit: int,
    output: Path | None = None,
    json_output: bool = False,
) -> None:
    from recorder.analysis_database import AnalysisDatabase
    from recorder.replay_scoring import (
        action_gate_markdown,
        action_gate_summary,
        format_action_gate,
        write_action_gate_markdown,
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
    if output is not None:
        written = write_action_gate_markdown(
            output,
            rows,
            filters=filters,
            min_group_rows=min_group_rows,
        )
    else:
        written = None
    if json_output:
        print(
            json.dumps(
                {
                    "summary": action_gate_summary(rows, min_group_rows=min_group_rows),
                    "filters": filters,
                    "output": str(written) if written else None,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return
    summary = action_gate_summary(rows, min_group_rows=min_group_rows)
    print(format_action_gate(summary))
    if output is not None:
        print(f"\nMarkdown report written: {written}")
    else:
        print(action_gate_markdown(rows, filters=filters, min_group_rows=min_group_rows))
