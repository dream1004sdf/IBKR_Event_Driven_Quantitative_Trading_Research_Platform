from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .cli_profile_utils import (
    coalesce_profile_path as _coalesce_profile_path,
    coalesce_profile_value as _coalesce_profile_value,
    load_profile_json as _load_profile_json,
)


def _console_safe_text(text: str) -> str:
    return text.encode("ascii", errors="replace").decode("ascii")


def command_m236_news_learning(
    profile_json_path: Path | None,
    output: Path | None,
    json_output_path: Path | None,
    provider_profile_output_path: Path | None,
    json_output: bool,
) -> dict[str, Any]:
    from recorder.m236_news_learning import (
        M236NewsLearningConfig,
        build_m236_news_learning_report,
        render_m236_news_learning_markdown,
        write_m236_news_learning_json,
        write_m236_news_learning_markdown,
        write_m236_provider_profile,
    )

    profile: dict[str, Any] = {}
    profile_base: Path | None = None
    if profile_json_path is not None:
        profile, profile_base = _load_profile_json(profile_json_path)
    config = M236NewsLearningConfig(
        event_roles_json_path=(
            _coalesce_profile_path(None, profile, "paths", "event_roles_json", profile_base)
            or Path("exports/m236_cross_provider_event_roles.json")
        ),
        outcome_learning_jsonl_path=(
            _coalesce_profile_path(None, profile, "paths", "outcome_learning_jsonl", profile_base)
            or Path("exports/m236_cross_provider_outcome_learning.jsonl")
        ),
        evaluated_index_jsonl_path=(
            _coalesce_profile_path(None, profile, "paths", "evaluated_index_jsonl", profile_base)
            or Path("exports/m236_evaluated_outcome_index.jsonl")
        ),
        macro_release_interpreter_json_path=(
            _coalesce_profile_path(None, profile, "paths", "macro_release_interpreter_json", profile_base)
            or Path("exports/m237_macro_release_interpreter.json")
        ),
        signal_classification_notes_jsonl_path=(
            _coalesce_profile_path(None, profile, "paths", "signal_classification_notes_jsonl", profile_base)
            or Path("exports/clean_backfill_signal_classification_notes.jsonl")
        ),
        provider_profile_output_path=(
            _coalesce_profile_path(provider_profile_output_path, profile, "outputs", "provider_profile_json", profile_base)
            or Path("exports/m236_provider_weight_profile.json")
        ),
        report_json_output_path=(
            _coalesce_profile_path(json_output_path, profile, "outputs", "json_output_path", profile_base)
            or Path("exports/m236_news_learning_report.json")
        ),
        report_markdown_output_path=(
            _coalesce_profile_path(output, profile, "outputs", "output", profile_base)
            or Path("exports/m236_news_learning_report.md")
        ),
        max_records=int(_coalesce_profile_value(None, profile, "learning", "max_records", 5000)),
        evaluated_sample_records=int(_coalesce_profile_value(None, profile, "learning", "evaluated_sample_records", 2000)),
        max_evaluated_index_records=int(_coalesce_profile_value(None, profile, "learning", "max_evaluated_index_records", 20000)),
        flat_return_threshold=float(_coalesce_profile_value(None, profile, "learning", "flat_return_threshold", 0.001)),
    )
    payload = build_m236_news_learning_report(config)
    written = write_m236_news_learning_markdown(config.report_markdown_output_path, payload)
    json_written = write_m236_news_learning_json(config.report_json_output_path, payload)
    provider_written = write_m236_provider_profile(config.provider_profile_output_path, payload)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(_console_safe_text(render_m236_news_learning_markdown(payload)))
        print(f"M236 news learning Markdown written: {written}")
        print(f"M236 news learning JSON written: {json_written}")
        print(f"M236 provider profile JSON written: {provider_written}")
    return payload


def command_m236_outcome_attach(
    profile_json_path: Path | None,
    price_source_name: str | None,
    max_records: int | None,
    max_symbols: int | None,
    horizons: str | None,
    dry_run: bool,
    output: Path | None,
    json_output_path: Path | None,
    json_output: bool,
) -> dict[str, Any]:
    from recorder.m236_news_learning import (
        M236OutcomeAttachmentConfig,
        attach_m236_news_outcomes,
        render_m236_outcome_attachment_markdown,
        write_m236_outcome_attachment_json,
        write_m236_outcome_attachment_markdown,
    )

    profile: dict[str, Any] = {}
    profile_base: Path | None = None
    if profile_json_path is not None:
        profile, profile_base = _load_profile_json(profile_json_path)
    horizon_value = str(_coalesce_profile_value(horizons, profile, "attachment", "horizons", "5M,15M,30M,EOD"))
    parsed_horizons = tuple(item.strip().upper() for item in horizon_value.split(",") if item.strip())
    config = M236OutcomeAttachmentConfig(
        outcome_learning_jsonl_path=(
            _coalesce_profile_path(None, profile, "paths", "outcome_learning_jsonl", profile_base)
            or Path("exports/m236_cross_provider_outcome_learning.jsonl")
        ),
        report_json_output_path=(
            _coalesce_profile_path(json_output_path, profile, "outputs", "json_output_path", profile_base)
            or Path("exports/m236_outcome_attachment_report.json")
        ),
        report_markdown_output_path=(
            _coalesce_profile_path(output, profile, "outputs", "output", profile_base)
            or Path("exports/m236_outcome_attachment_report.md")
        ),
        max_records=int(_coalesce_profile_value(max_records, profile, "attachment", "max_records", 25)),
        max_symbols=int(_coalesce_profile_value(max_symbols, profile, "attachment", "max_symbols", 10)),
        horizons=parsed_horizons or ("5M", "15M", "30M"),
        flat_return_threshold=float(_coalesce_profile_value(None, profile, "attachment", "flat_return_threshold", 0.001)),
        lookback_records=int(_coalesce_profile_value(None, profile, "attachment", "lookback_records", 5000)),
        adaptive_batch_enabled=bool(_coalesce_profile_value(None, profile, "attachment", "adaptive_batch_enabled", True)),
        adaptive_backlog_mid=int(_coalesce_profile_value(None, profile, "attachment", "adaptive_backlog_mid", 500)),
        adaptive_backlog_high=int(_coalesce_profile_value(None, profile, "attachment", "adaptive_backlog_high", 2000)),
        adaptive_max_records_mid=int(_coalesce_profile_value(None, profile, "attachment", "adaptive_max_records_mid", 50)),
        adaptive_max_records_high=int(_coalesce_profile_value(None, profile, "attachment", "adaptive_max_records_high", 100)),
        adaptive_max_symbols_mid=int(_coalesce_profile_value(None, profile, "attachment", "adaptive_max_symbols_mid", 12)),
        adaptive_max_symbols_high=int(_coalesce_profile_value(None, profile, "attachment", "adaptive_max_symbols_high", 20)),
        dry_run=bool(dry_run or _coalesce_profile_value(None, profile, "attachment", "dry_run", False)),
    )
    source_choice = str(_coalesce_profile_value(price_source_name, profile, "attachment", "price_source", "ibkr")).lower()
    price_source = None
    if source_choice == "ibkr":
        from recorder.market_outcomes import IBKRHistoricalIntradayPriceSource

        price_source = IBKRHistoricalIntradayPriceSource(
            host=str(_coalesce_profile_value(None, profile, "ibkr", "host", "127.0.0.1")),
            port=int(_coalesce_profile_value(None, profile, "ibkr", "port", 4001)),
            client_id=int(_coalesce_profile_value(None, profile, "ibkr", "client_id", 1036)),
            timeout_seconds=float(_coalesce_profile_value(None, profile, "ibkr", "timeout_seconds", 12.0)),
            what_to_show=str(_coalesce_profile_value(None, profile, "ibkr", "what_to_show", "TRADES")),
            bar_size=str(_coalesce_profile_value(None, profile, "ibkr", "bar_size", "1 min")),
            use_rth=bool(_coalesce_profile_value(None, profile, "ibkr", "use_rth", False)),
        )
    elif source_choice != "none":
        raise SystemExit(f"Unsupported M236 outcome attachment price source: {source_choice}")
    try:
        payload = attach_m236_news_outcomes(config, price_source=price_source)
    finally:
        if price_source is not None and hasattr(price_source, "close"):
            price_source.close()
    written = write_m236_outcome_attachment_markdown(config.report_markdown_output_path, payload)
    json_written = write_m236_outcome_attachment_json(config.report_json_output_path, payload)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(_console_safe_text(render_m236_outcome_attachment_markdown(payload)))
        print(f"M236 outcome attachment Markdown written: {written}")
        print(f"M236 outcome attachment JSON written: {json_written}")
    return payload


def command_macro_event_calendar(
    output: Path | None,
    json_output_path: Path | None,
    horizon_days: int,
    no_internet_refresh: bool,
    json_output: bool,
) -> dict[str, Any]:
    from recorder.macro_release_interpreter import (
        MacroEventCalendarConfig,
        build_macro_event_calendar_report,
        render_macro_event_calendar_markdown,
        write_json,
        write_macro_calendar_markdown,
    )

    config = MacroEventCalendarConfig(
        output_json_path=json_output_path or Path("exports/m237_macro_event_calendar.json"),
        output_markdown_path=output or Path("exports/m237_macro_event_calendar.md"),
        horizon_days=horizon_days,
        include_internet_refresh=not bool(no_internet_refresh),
    )
    payload = build_macro_event_calendar_report(config)
    written = write_macro_calendar_markdown(config.output_markdown_path, payload)
    json_written = write_json(config.output_json_path, payload)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False))
    else:
        print(_console_safe_text(render_macro_event_calendar_markdown(payload)))
        print(f"M237 macro event calendar Markdown written: {written}")
        print(f"M237 macro event calendar JSON written: {json_written}")
    return payload


def command_macro_release_interpreter(
    database_path: Path | None,
    output: Path | None,
    json_output_path: Path | None,
    lookback_minutes: int,
    max_rows: int,
    no_market_reaction: bool,
    price_source_name: str,
    json_output: bool,
) -> dict[str, Any]:
    from recorder.macro_release_interpreter import (
        MacroReleaseInterpreterConfig,
        build_macro_release_interpreter_report,
        render_macro_release_interpreter_markdown,
        write_json,
        write_macro_release_markdown,
    )

    config = MacroReleaseInterpreterConfig(
        database_path=database_path or Path("market_events.db"),
        output_json_path=json_output_path or Path("exports/m237_macro_release_interpreter.json"),
        output_markdown_path=output or Path("exports/m237_macro_release_interpreter.md"),
        lookback_minutes=lookback_minutes,
        max_rows=max_rows,
        attach_market_reaction=not bool(no_market_reaction),
    )
    price_source = None
    if config.attach_market_reaction and price_source_name == "ibkr":
        from recorder.market_outcomes import IBKRHistoricalIntradayPriceSource

        price_source = IBKRHistoricalIntradayPriceSource(
            host="127.0.0.1",
            port=4001,
            client_id=1037,
            timeout_seconds=12.0,
            what_to_show="TRADES",
            bar_size="1 min",
            use_rth=False,
        )
    elif config.attach_market_reaction and price_source_name != "none":
        raise SystemExit(f"Unsupported macro release price source: {price_source_name}")
    try:
        payload = build_macro_release_interpreter_report(config, price_source=price_source)
    finally:
        if price_source is not None and hasattr(price_source, "close"):
            price_source.close()
    written = write_macro_release_markdown(config.output_markdown_path, payload)
    json_written = write_json(config.output_json_path, payload)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False))
    else:
        print(_console_safe_text(render_macro_release_interpreter_markdown(payload)))
        print(f"M237 macro release interpreter Markdown written: {written}")
        print(f"M237 macro release interpreter JSON written: {json_written}")
    return payload


def command_unknown_theme_discovery(
    settings: Settings,
    database_path: Path | None,
    lookback_minutes: int,
    min_group_events: int,
    limit: int,
    include_medium: bool,
    ai_review: bool,
    openai_model: str | None,
    reasoning_effort: str,
    max_ai_groups: int,
    output: Path | None,
    json_output_path: Path | None,
    json_output: bool,
) -> dict[str, Any]:
    from recorder.unknown_theme_discovery import (
        UnknownThemeDiscoveryConfig,
        discover_unknown_theme_candidates,
        unknown_theme_discovery_markdown,
    )

    config = UnknownThemeDiscoveryConfig(
        database_path=database_path or settings.database_path,
        lookback_minutes=lookback_minutes,
        min_group_events=min_group_events,
        limit=limit,
        include_medium=include_medium,
        ai_review=ai_review,
        openai_model=(openai_model or settings.ai_model),
        reasoning_effort=reasoning_effort,
        max_ai_groups=max_ai_groups,
        output_path=output,
        json_output_path=json_output_path,
    )
    result = discover_unknown_theme_candidates(config, openai_api_key=settings.openai_api_key)
    if json_output:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(_console_safe_text(unknown_theme_discovery_markdown(result)))
        if output:
            print(f"Unknown-theme discovery Markdown written: {output}")
        if json_output_path:
            print(f"Unknown-theme discovery JSON written: {json_output_path}")
    return result
