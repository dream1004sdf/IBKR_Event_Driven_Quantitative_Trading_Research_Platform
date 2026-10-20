from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _console_safe_text(text: str) -> str:
    return text.encode("ascii", errors="replace").decode("ascii")


def command_replay_market_overlay(
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
    signal_limit: int,
    min_group_rows: int,
    limit: int,
    output: Path | None = None,
    json_output: bool = False,
) -> None:
    from recorder.analysis_database import AnalysisDatabase
    from recorder.market_overlay import (
        market_overlay_markdown,
        market_overlay_summary,
        write_market_overlay_markdown,
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
        "signal_limit": signal_limit,
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
    signals = db.recent_external_market_signals(limit=signal_limit)
    if output is not None:
        written = write_market_overlay_markdown(
            output,
            rows,
            signals,
            filters=filters,
            min_group_rows=min_group_rows,
        )
    else:
        written = None
    summary = market_overlay_summary(rows, signals, min_group_rows=min_group_rows)
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
    print(
        "Replay market overlay analysis:\n"
        f"rows={summary['rows']} signals={summary['signals']} "
        f"overlay_coverage={summary['overlay_coverage']:.2%} "
        f"risk_overlay_coverage={summary['risk_overlay_coverage']:.2%} "
        f"all_directional={summary['all_rows']['directional_hits']}/{summary['all_rows']['directional_rows']} "
        f"non_risk={summary['non_risk_rows']['directional_hits']}/{summary['non_risk_rows']['directional_rows']}"
    )
    if output is not None:
        print(f"\nMarkdown report written: {written}")
    else:
        print(market_overlay_markdown(rows, signals, filters=filters, min_group_rows=min_group_rows))


def command_replay_emergency_overlay(
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
    signal_limit: int,
    min_group_rows: int,
    limit: int,
    output: Path | None = None,
    json_output: bool = False,
) -> None:
    from recorder.analysis_database import AnalysisDatabase
    from recorder.market_overlay import (
        emergency_overlay_markdown,
        emergency_overlay_summary,
        write_emergency_overlay_markdown,
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
        "signal_limit": signal_limit,
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
    signals = db.recent_external_market_signals(source="emergency_breaking_news", limit=signal_limit)
    if output is not None:
        written = write_emergency_overlay_markdown(
            output,
            rows,
            signals,
            filters=filters,
            min_group_rows=min_group_rows,
        )
    else:
        written = None
    summary = emergency_overlay_summary(rows, signals, min_group_rows=min_group_rows)
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
    print(
        "Replay emergency overlay tuning:\n"
        f"rows={summary['rows']} emergency_signals={summary['signals']} "
        f"coverage={summary['emergency_coverage']:.2%} "
        f"all_directional={summary['all_rows']['directional_hits']}/{summary['all_rows']['directional_rows']} "
        f"emergency_directional={summary['emergency_rows']['directional_hits']}/{summary['emergency_rows']['directional_rows']} "
        f"conflict={summary['conflict_rows']['directional_hits']}/{summary['conflict_rows']['directional_rows']} "
        f"confirmation={summary['confirmation_rows']['directional_hits']}/{summary['confirmation_rows']['directional_rows']}"
    )
    if output is not None:
        print(f"\nMarkdown report written: {written}")
    else:
        print(emergency_overlay_markdown(rows, signals, filters=filters, min_group_rows=min_group_rows))


def command_replay_evidence_packets(
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
    signal_limit: int,
    min_group_rows: int,
    limit: int,
    output: Path | None = None,
    json_output: bool = False,
) -> None:
    from recorder.analysis_database import AnalysisDatabase
    from recorder.evidence_packet import (
        evidence_packet_markdown,
        evidence_packet_summary,
        write_evidence_packet_markdown,
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
        "signal_limit": signal_limit,
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
    signals = db.recent_external_market_signals(limit=signal_limit)
    if output is not None:
        written = write_evidence_packet_markdown(
            output,
            rows,
            signals,
            db,
            filters=filters,
            min_group_rows=min_group_rows,
        )
    else:
        written = None
    summary = evidence_packet_summary(rows, signals, db, min_group_rows=min_group_rows)
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
    print(
        "Replay overlay + macro evidence packet analysis:\n"
        f"rows={summary['rows']} signals={summary['signals']} "
        f"macro_coverage={summary['macro_coverage']:.2%} "
        f"technical_coverage={summary['technical_coverage']:.2%} "
        f"all_directional={summary['all_rows']['directional_hits']}/{summary['all_rows']['directional_rows']} "
        f"overlay_directional={summary['overlay_rows']['directional_hits']}/{summary['overlay_rows']['directional_rows']} "
        f"risk_conflict={summary['risk_conflict_rows']['directional_hits']}/{summary['risk_conflict_rows']['directional_rows']}"
    )
    if output is not None:
        print(f"\nMarkdown report written: {written}")
    else:
        print(evidence_packet_markdown(rows, signals, db, filters=filters, min_group_rows=min_group_rows))


def command_replay_surprise_adjustments_build(
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
    limit: int,
    min_abs_error: float | None,
    include_hits: bool,
    analysis_provider: str,
    openai_model: str | None,
    reasoning_effort: str,
    max_input_chars: int,
) -> None:
    from recorder.ai_provider import PermanentAnalysisError, TransientAnalysisError
    from recorder.analysis_database import AnalysisDatabase
    from recorder.surprise_adjustments import OpenAISurpriseAdjustmentAnalyzer, ReplaySurpriseAdjustmentEngine

    symbols = tuple(item.strip().upper() for item in (symbols_value or "").split(",") if item.strip())
    db = AnalysisDatabase(settings.database_path)
    normalized_provider = analysis_provider.strip().lower()
    analyzer = None
    if normalized_provider == "openai":
        analyzer = OpenAISurpriseAdjustmentAnalyzer(
            api_key=settings.openai_api_key or "",
            model=openai_model or settings.ai_model,
            timeout_seconds=settings.ai_api_timeout_seconds,
            reasoning_effort=reasoning_effort,
            max_input_chars=max_input_chars,
        )
    elif normalized_provider != "deterministic":
        raise SystemExit(f"Unsupported surprise adjustment analysis provider: {analysis_provider}")
    try:
        result = ReplaySurpriseAdjustmentEngine(db, analyzer=analyzer).build(
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
            misses_only=not include_hits,
            min_abs_error=min_abs_error,
        )
    except PermanentAnalysisError as exc:
        raise SystemExit(f"Surprise adjustment configuration error: {exc}") from exc
    except TransientAnalysisError as exc:
        raise SystemExit(f"Surprise adjustment transient error: {exc}") from exc
    print(json.dumps(result, indent=2, ensure_ascii=False))


def command_replay_surprise_adjustments_report(
    settings: Settings,
    scorer_name: str | None,
    scorer_version: str | None,
    symbol: str | None,
    analysis_provider: str | None,
    adjustment_version: str | None,
    limit: int,
    output: Path | None,
    json_output: bool,
) -> None:
    from recorder.analysis_database import AnalysisDatabase
    from recorder.surprise_adjustments import (
        adjustment_markdown,
        format_adjustment_report,
        summarize_adjustment_notes,
        write_adjustment_markdown,
    )

    filters = {
        "scorer_name": scorer_name,
        "scorer_version": scorer_version,
        "symbol": symbol.strip().upper() if symbol else None,
        "analysis_provider": analysis_provider,
        "adjustment_version": adjustment_version,
        "limit": limit,
    }
    db = AnalysisDatabase(settings.database_path)
    rows = db.recent_replay_surprise_adjustment_notes(
        scorer_name=scorer_name,
        scorer_version=scorer_version,
        symbol=symbol,
        analysis_provider=analysis_provider,
        adjustment_version=adjustment_version,
        limit=limit,
    )
    written = write_adjustment_markdown(output, rows, filters=filters) if output is not None else None
    if json_output:
        print(
            json.dumps(
                {
                    "summary": summarize_adjustment_notes(rows),
                    "notes": rows,
                    "filters": filters,
                    "output": str(written) if written else None,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return
    print(format_adjustment_report(rows))
    if output is not None:
        print(f"\nMarkdown report written: {written}")
    else:
        print("\n" + adjustment_markdown(rows, filters=filters))


def command_ytd_calibration_run(
    settings: Settings,
    start: str,
    end: str,
    symbols_value: str | None,
    stages_value: str | None,
    sec_limit_per_symbol: int,
    financial_limit_per_symbol: int,
    macro_limit: int,
    fred_limit_per_series: int,
    external_limit: int,
    ibkr_news_limit_per_symbol: int,
    ibkr_news_providers: str | None,
    technical_limit_per_symbol: int,
    replay_event_type: str | None,
    replay_limit: int,
    outcome_limit: int,
    score_limit: int,
    openai_score_limit: int,
    adjustment_scorer: str,
    adjustment_limit: int,
    openai_adjustment_limit: int,
    output_dir: Path,
    openai_model: str | None,
    openai_scorer_version: str | None,
    reasoning_effort: str,
    max_input_chars: int,
    dry_run: bool,
) -> None:
    from recorder.ytd_calibration_pipeline import (
        DEFAULT_YTD_SYMBOLS,
        YtdCalibrationConfig,
        YtdCalibrationPipeline,
        parse_csv,
        parse_replay_event_type,
        parse_stages,
    )

    config = YtdCalibrationConfig(
        start=start,
        end=end,
        symbols=parse_csv(symbols_value, DEFAULT_YTD_SYMBOLS),
        stages=parse_stages(stages_value),
        sec_limit_per_symbol=sec_limit_per_symbol,
        financial_limit_per_symbol=financial_limit_per_symbol,
        macro_limit=macro_limit,
        fred_limit_per_series=fred_limit_per_series,
        external_limit=external_limit,
        ibkr_news_limit_per_symbol=ibkr_news_limit_per_symbol,
        ibkr_news_providers=tuple(item.strip().upper() for item in ibkr_news_providers.split(",") if item.strip()) if ibkr_news_providers else ("BZ",),
        technical_limit_per_symbol=technical_limit_per_symbol,
        replay_event_type=parse_replay_event_type(replay_event_type),
        replay_limit=replay_limit,
        outcome_limit=outcome_limit,
        score_limit=score_limit,
        openai_score_limit=openai_score_limit,
        adjustment_scorer=adjustment_scorer,
        adjustment_limit=adjustment_limit,
        openai_adjustment_limit=openai_adjustment_limit,
        output_dir=output_dir,
        openai_model=openai_model,
        openai_scorer_version=openai_scorer_version,
        reasoning_effort=reasoning_effort,
        max_input_chars=max_input_chars,
        dry_run=dry_run,
    )
    print(json.dumps(YtdCalibrationPipeline(settings, config).run(), indent=2, ensure_ascii=False, default=str))
