from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from recorder.config import Settings


def command_news_market_outcome_backfill(
    settings: Settings,
    start: str,
    end: str,
    symbols: str | None,
    horizons: str,
    price_source: str,
    limit: int,
    max_groups: int,
    only_missing: bool,
    include_pseudo_symbols: bool,
    flat_threshold: float,
    symbol_tier_config: Path | None,
    symbol_tier_filter: str | None,
    event_proxy_policy: str,
    unknown_symbol_policy: str,
    ibkr_client_id: int | None,
    ibkr_timeout: float,
    ibkr_what_to_show: str,
    ibkr_bar_size: str,
    ibkr_all_sessions: bool,
    close_retry_bypass_after_failures: int,
    progress_jsonl_path: Path | None,
    output: Path | None,
    json_output_path: Path | None,
    json_output: bool,
) -> dict[str, Any]:
    from recorder.market_outcomes import IBKRHistoricalIntradayPriceSource
    from recorder.news_market_outcome_backfill import (
        build_news_market_outcomes,
        news_market_outcome_backfill_markdown,
        write_news_market_outcome_backfill_report,
    )

    parsed_horizons = tuple(item.strip().upper() for item in str(horizons or "").split(",") if item.strip())
    intraday_required = any(h not in {"1D", "NEXT_DAY", "NEXT_1D", "NEXT_CLOSE"} for h in parsed_horizons)
    intraday_source = None
    try:
        if price_source.lower() == "ibkr" and intraday_required:
            intraday_source = IBKRHistoricalIntradayPriceSource(
                host=settings.host,
                port=settings.port,
                client_id=int(ibkr_client_id or (settings.client_id + 3100)),
                timeout_seconds=float(ibkr_timeout),
                what_to_show=ibkr_what_to_show,
                bar_size=ibkr_bar_size,
                use_rth=not bool(ibkr_all_sessions),
            )
        report = build_news_market_outcomes(
            settings.database_path,
            start=start,
            end=end,
            symbols=symbols,
            horizons=parsed_horizons or ("NEXT_DAY",),
            price_source=price_source.lower(),
            limit=limit,
            max_groups=max_groups,
            only_missing=only_missing,
            include_pseudo_symbols=include_pseudo_symbols,
            flat_threshold=flat_threshold,
            close_retry_bypass_after_failures=close_retry_bypass_after_failures,
            intraday_price_source=intraday_source,
            progress_jsonl_path=progress_jsonl_path,
            symbol_tier_config=symbol_tier_config,
            symbol_tier_filter=symbol_tier_filter,
            event_proxy_policy=event_proxy_policy,
            unknown_symbol_policy=unknown_symbol_policy,
        )
    finally:
        if intraday_source is not None:
            intraday_source.close()

    write_news_market_outcome_backfill_report(report, output=output, json_output_path=json_output_path)
    if json_output:
        print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    else:
        print(news_market_outcome_backfill_markdown(report))
        if output:
            print(f"News outcome backfill Markdown written: {output}")
        if json_output_path:
            print(f"News outcome backfill JSON written: {json_output_path}")
    return report


def command_news_symbol_tier_report(
    settings: Settings,
    start: str,
    end: str,
    symbol_tier_config: Path,
    limit_samples_per_symbol: int,
    output: Path | None,
    json_output_path: Path | None,
    json_output: bool,
) -> dict[str, Any]:
    from recorder.news_symbol_tiers import (
        build_news_symbol_tier_report,
        news_symbol_tier_report_markdown,
        write_news_symbol_tier_report,
    )

    report = build_news_symbol_tier_report(
        settings.database_path,
        start=start,
        end=end,
        config_path=symbol_tier_config,
        limit_samples_per_symbol=limit_samples_per_symbol,
    )
    write_news_symbol_tier_report(report, output=output, json_output_path=json_output_path)
    if json_output:
        print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    else:
        print(news_symbol_tier_report_markdown(report))
        if output:
            print(f"News symbol tier Markdown written: {output}")
        if json_output_path:
            print(f"News symbol tier JSON written: {json_output_path}")
    return report


def command_clean_backfill_cycle_audit(
    settings: Settings,
    start: str,
    end: str,
    cycle_index: int,
    cycle_mode: str,
    phase: str,
    symbol_tier_config: Path,
    correction_ledger_path: Path | None,
    output: Path | None,
    json_output_path: Path | None,
    json_output: bool,
) -> dict[str, Any]:
    from recorder.clean_backfill_cycle import (
        build_clean_backfill_cycle_audit,
        clean_backfill_cycle_audit_markdown,
        write_cycle_report,
    )

    report = build_clean_backfill_cycle_audit(
        settings.database_path,
        start=start,
        end=end,
        cycle_index=cycle_index,
        cycle_mode=cycle_mode,
        phase=phase,
        symbol_tier_config=symbol_tier_config,
        correction_ledger_path=correction_ledger_path,
    )
    write_cycle_report(report, output=output, json_output_path=json_output_path, kind="audit")
    if json_output:
        print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    else:
        print(clean_backfill_cycle_audit_markdown(report))
        if output:
            print(f"Clean backfill cycle audit Markdown written: {output}")
        if json_output_path:
            print(f"Clean backfill cycle audit JSON written: {json_output_path}")
    return report


def command_clean_backfill_cycle_report(
    start: str,
    end: str,
    cycle_index: int,
    cycle_mode: str,
    artifact_dir: Path,
    correction_ledger_path: Path | None,
    output: Path | None,
    json_output_path: Path | None,
    json_output: bool,
) -> dict[str, Any]:
    from recorder.clean_backfill_cycle import (
        build_clean_backfill_cycle_report,
        clean_backfill_cycle_report_markdown,
        write_cycle_report,
    )

    report = build_clean_backfill_cycle_report(
        start=start,
        end=end,
        cycle_index=cycle_index,
        cycle_mode=cycle_mode,
        artifact_dir=artifact_dir,
        correction_ledger_path=correction_ledger_path,
    )
    write_cycle_report(report, output=output, json_output_path=json_output_path, kind="report")
    if json_output:
        print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    else:
        print(clean_backfill_cycle_report_markdown(report))
        if output:
            print(f"Clean backfill cycle report Markdown written: {output}")
        if json_output_path:
            print(f"Clean backfill cycle report JSON written: {json_output_path}")
    return report


def command_clean_signal_classification_audit(
    settings: Settings,
    start: str,
    end: str,
    limit: int,
    symbol_tier_report_path: Path | None,
    output: Path | None,
    json_output_path: Path | None,
    notes_jsonl_path: Path | None,
    json_output: bool,
) -> dict[str, Any]:
    from recorder.signal_classification_audit import (
        build_signal_classification_audit,
        signal_classification_audit_markdown,
        write_signal_classification_audit,
    )

    report = build_signal_classification_audit(
        settings.database_path,
        start=start,
        end=end,
        limit=limit,
        symbol_tier_report_path=symbol_tier_report_path,
    )
    write_signal_classification_audit(
        report,
        output=output,
        json_output_path=json_output_path,
        notes_jsonl_path=notes_jsonl_path,
    )
    if json_output:
        print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    else:
        print(signal_classification_audit_markdown(report))
        if output:
            print(f"Signal classification audit Markdown written: {output}")
        if json_output_path:
            print(f"Signal classification audit JSON written: {json_output_path}")
        if notes_jsonl_path:
            print(f"Signal classification audit notes JSONL written: {notes_jsonl_path}")
    return report
