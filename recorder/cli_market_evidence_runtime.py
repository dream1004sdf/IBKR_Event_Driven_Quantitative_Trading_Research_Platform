from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .cli_profile_utils import (
    coalesce_profile_path as _coalesce_profile_path,
    coalesce_profile_value as _coalesce_profile_value,
    load_profile_json as _load_profile_json,
)


def _console_safe_text(text: str) -> str:
    return text.encode("ascii", errors="replace").decode("ascii")


def _singleton_lock(command_name: str):
    from recorder.singleton_lock import SingleInstanceLock

    lock_name = command_name.strip().replace(" ", "_")
    repo_root = Path(__file__).resolve().parents[1]
    return SingleInstanceLock(lock_name, repo_root / "locks" / f"{lock_name}.lock")


def _singleton_skip_payload(command_name: str, exc: Exception) -> dict[str, Any]:
    payload = getattr(exc, "payload", None)
    if not isinstance(payload, dict):
        payload = {"message": str(exc)}
    return {
        "status": "skipped_already_running",
        "command": command_name,
        "reason": "singleton_lock_active",
        "singleton": payload,
        "safety": {
            "existing_process_left_running": True,
            "new_duplicate_process_started": False,
            "ibkr_order_submitted": False,
            "portfolio_mutated": False,
        },
    }


def _run_singleton(command_name: str, runner: Any) -> dict[str, Any]:
    from recorder.singleton_lock import AlreadyRunningError, append_singleton_event

    try:
        with _singleton_lock(command_name):
            return runner()
    except AlreadyRunningError as exc:
        payload = _singleton_skip_payload(command_name, exc)
        append_singleton_event(Path(__file__).resolve().parents[1] / "logs" / "singleton_events.jsonl", payload)
        return payload



def command_market_data_desk(
    settings: Settings,
    seconds: int,
    refresh_seconds: float,
    market_data_type: int,
    client_id: int | None,
    extra_stock_symbols: str | None,
    account_snapshot_json: Path | None,
    gap_threshold_pct: float,
    tick_move_threshold_pct: float,
    fallback_after_seconds: float,
    fallback_on_close_only: bool,
    output: Path | None,
    json_output_path: Path | None,
    json_output: bool,
) -> None:
    from recorder.market_data_desk import (
        MarketDataDeskApp,
        format_market_data_report,
        market_data_instruments_with_account_option_underlyings,
        market_data_snapshot_payload,
        write_market_data_snapshot_json,
    )

    def _runner() -> dict[str, Any]:
        instruments = market_data_instruments_with_account_option_underlyings(
            extra_stock_symbols=extra_stock_symbols,
            account_snapshot_path=account_snapshot_json,
        )
        app = MarketDataDeskApp(
            instruments=instruments,
            host=settings.host,
            port=settings.port,
            client_id=client_id if client_id is not None else settings.client_id + 4000,
            requested_market_data_type=market_data_type,
            timeout_seconds=max(5.0, float(refresh_seconds) * 2.0),
            gap_threshold_pct=gap_threshold_pct,
            tick_move_threshold_pct=tick_move_threshold_pct,
            fallback_after_seconds=fallback_after_seconds,
            fallback_on_close_only=fallback_on_close_only,
        )
        app.start()
        try:
            started = time.monotonic()
            final_payload: dict[str, Any] | None = None
            while True:
                time.sleep(max(0.5, float(refresh_seconds)))
                rows = app.snapshot_rows()
                final_payload = market_data_snapshot_payload(rows)
                report = format_market_data_report(rows)
                if not json_output:
                    print(report)
                    print()
                if output is not None:
                    output.parent.mkdir(parents=True, exist_ok=True)
                    output.write_text(report + "\n", encoding="utf-8")
                if json_output_path is not None:
                    write_market_data_snapshot_json(json_output_path, final_payload)
                if seconds > 0 and time.monotonic() - started >= seconds:
                    break
            if json_output:
                print(json.dumps(final_payload or {}, indent=2, ensure_ascii=False))
            return final_payload or {}
        finally:
            app.stop()

    result = _run_singleton("market-data-desk", _runner)
    if result.get("status") == "skipped_already_running":
        if json_output:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            print(f"{result.get('command')} skipped: {result.get('reason')} ({(result.get('singleton') or {}).get('message')})")


def command_source_online_supervisor(
    settings: Settings,
    profile_json_path: Path | None,
    max_ticks: int | None,
    interval_seconds: float | None,
    sleep_between_ticks: bool | None,
    execute_actions: bool,
    skip_live_ibkr: bool,
    job_timeout_seconds: float | None,
    run_id: str | None,
    output: Path | None,
    json_output_path: Path | None,
    json_output: bool,
) -> dict[str, Any]:
    from recorder.source_operations import (
        SourceOnlineSupervisorConfig,
        run_source_online_supervisor,
        source_online_supervisor_markdown,
        write_source_online_supervisor_json,
        write_source_online_supervisor_markdown,
    )

    profile: dict[str, Any] = {}
    profile_base: Path | None = None
    if profile_json_path is not None:
        profile, profile_base = _load_profile_json(profile_json_path)

    live_validation_profile_path = (
        _coalesce_profile_path(None, profile, "paths", "live_validation_profile_json", profile_base)
        or Path("config/source_live_validation_profile_v1.json")
    )
    live_validation_json_path = (
        _coalesce_profile_path(None, profile, "paths", "live_validation_json", profile_base)
        or Path("exports/m161_source_live_validation.json")
    )
    live_validation_markdown_path = (
        _coalesce_profile_path(None, profile, "paths", "live_validation_markdown", profile_base)
        or Path("exports/m161_source_live_validation.md")
    )
    freshness_profile_path = (
        _coalesce_profile_path(None, profile, "paths", "freshness_profile_json", profile_base)
        or Path("config/evidence_freshness_desk_profile_v1.json")
    )
    freshness_report_path = (
        _coalesce_profile_path(None, profile, "paths", "freshness_report_json", profile_base)
        or Path("exports/m152_evidence_freshness_desk.json")
    )
    source_ingest_health_profile_path = (
        _coalesce_profile_path(None, profile, "paths", "source_ingest_health_profile_json", profile_base)
        or Path("config/source_ingest_health_profile_v1.json")
    )
    source_ingest_health_json_path = (
        _coalesce_profile_path(None, profile, "paths", "source_ingest_health_json", profile_base)
        or Path("exports/m160_source_ingest_health.json")
    )
    source_ingest_health_markdown_path = (
        _coalesce_profile_path(None, profile, "paths", "source_ingest_health_markdown", profile_base)
        or Path("exports/m160_source_ingest_health.md")
    )
    output_dir = _coalesce_profile_path(None, profile, "paths", "output_dir", profile_base) or Path("exports")
    state_json_path = (
        _coalesce_profile_path(None, profile, "outputs", "state_json_path", profile_base)
        or Path("exports/m162_source_online_supervisor_state.json")
    )
    tick_log_jsonl_path = _coalesce_profile_path(None, profile, "outputs", "tick_log_jsonl_path", profile_base)
    output = _coalesce_profile_path(output, profile, "outputs", "output", profile_base)
    json_output_path = _coalesce_profile_path(json_output_path, profile, "outputs", "json_output_path", profile_base)
    dashboard_profile_path = _coalesce_profile_path(None, profile, "paths", "dashboard_profile_json", profile_base)

    max_ticks = int(_coalesce_profile_value(max_ticks, profile, "supervisor", "max_ticks", 1))
    interval_seconds = float(_coalesce_profile_value(interval_seconds, profile, "supervisor", "interval_seconds", 1.0))
    sleep_between_ticks = bool(_coalesce_profile_value(sleep_between_ticks, profile, "supervisor", "sleep_between_ticks", False))
    profile_execute = bool(_coalesce_profile_value(None, profile, "supervisor", "execute_actions", False))
    execute_actions = bool(execute_actions or profile_execute)
    include_live_ibkr = not bool(skip_live_ibkr or _coalesce_profile_value(None, profile, "supervisor", "skip_live_ibkr", False))
    job_timeout_seconds = float(_coalesce_profile_value(job_timeout_seconds, profile, "supervisor", "job_timeout_seconds", 180.0))
    action_cooldown_seconds = float(_coalesce_profile_value(None, profile, "supervisor", "action_cooldown_seconds", 300.0))
    market_data_action_cooldown_seconds = float(_coalesce_profile_value(None, profile, "supervisor", "market_data_action_cooldown_seconds", 1.0))
    market_news_action_cooldown_seconds = float(_coalesce_profile_value(None, profile, "supervisor", "market_news_action_cooldown_seconds", 1.0))
    unknown_theme_action_cooldown_seconds = float(_coalesce_profile_value(None, profile, "supervisor", "unknown_theme_action_cooldown_seconds", 300.0))
    geopolitical_action_cooldown_seconds = float(_coalesce_profile_value(None, profile, "supervisor", "geopolitical_action_cooldown_seconds", 300.0))
    marketaux_alpha_action_cooldown_seconds = float(_coalesce_profile_value(None, profile, "supervisor", "marketaux_alpha_action_cooldown_seconds", 60.0))
    news_subscription_mesh_action_cooldown_seconds = float(_coalesce_profile_value(None, profile, "supervisor", "news_subscription_mesh_action_cooldown_seconds", 60.0))
    cross_provider_event_roles_action_cooldown_seconds = float(_coalesce_profile_value(None, profile, "supervisor", "cross_provider_event_roles_action_cooldown_seconds", 60.0))
    m236_news_learning_action_cooldown_seconds = float(_coalesce_profile_value(None, profile, "supervisor", "m236_news_learning_action_cooldown_seconds", 60.0))
    m236_outcome_attachment_action_cooldown_seconds = float(_coalesce_profile_value(None, profile, "supervisor", "m236_outcome_attachment_action_cooldown_seconds", 300.0))
    macro_release_interpreter_action_cooldown_seconds = float(_coalesce_profile_value(None, profile, "supervisor", "macro_release_interpreter_action_cooldown_seconds", 60.0))
    macro_event_calendar_action_cooldown_seconds = float(_coalesce_profile_value(None, profile, "supervisor", "macro_event_calendar_action_cooldown_seconds", 3600.0))
    ibkr_validation_interval_seconds = float(_coalesce_profile_value(None, profile, "supervisor", "ibkr_validation_interval_seconds", 1.0))
    marketaux_alpha_validation_interval_seconds = float(_coalesce_profile_value(None, profile, "supervisor", "marketaux_alpha_validation_interval_seconds", 60.0))
    external_validation_interval_seconds = float(_coalesce_profile_value(None, profile, "supervisor", "external_validation_interval_seconds", marketaux_alpha_validation_interval_seconds))
    continuous_market_data = bool(_coalesce_profile_value(None, profile, "supervisor", "continuous_market_data", True))
    continuous_market_news = bool(_coalesce_profile_value(None, profile, "supervisor", "continuous_market_news", True))
    continuous_geopolitical = bool(_coalesce_profile_value(None, profile, "supervisor", "continuous_geopolitical", True))
    continuous_unknown_theme_discovery = bool(_coalesce_profile_value(None, profile, "supervisor", "continuous_unknown_theme_discovery", True))
    continuous_news_subscription_mesh = bool(_coalesce_profile_value(None, profile, "supervisor", "continuous_news_subscription_mesh", True))
    continuous_cross_provider_event_roles = bool(_coalesce_profile_value(None, profile, "supervisor", "continuous_cross_provider_event_roles", True))
    continuous_m236_news_learning = bool(_coalesce_profile_value(None, profile, "supervisor", "continuous_m236_news_learning", True))
    continuous_m236_outcome_attachment = bool(_coalesce_profile_value(None, profile, "supervisor", "continuous_m236_outcome_attachment", True))
    continuous_macro_release_interpreter = bool(_coalesce_profile_value(None, profile, "supervisor", "continuous_macro_release_interpreter", True))
    continuous_macro_event_calendar = bool(_coalesce_profile_value(None, profile, "supervisor", "continuous_macro_event_calendar", True))
    provider_rate_limit_cooldown_seconds = float(_coalesce_profile_value(None, profile, "supervisor", "provider_rate_limit_cooldown_seconds", 900.0))
    market_data_seconds = int(_coalesce_profile_value(None, profile, "supervisor", "market_data_seconds", 16))
    market_data_fallback_after_seconds = float(_coalesce_profile_value(None, profile, "supervisor", "market_data_fallback_after_seconds", 4.0))
    ibkr_news_limit_per_symbol = int(_coalesce_profile_value(None, profile, "supervisor", "ibkr_news_limit_per_symbol", 25))
    external_limit = int(_coalesce_profile_value(None, profile, "supervisor", "external_limit", 50))
    external_timespan = str(_coalesce_profile_value(None, profile, "supervisor", "external_timespan", "3d"))
    unknown_theme_lookback_minutes = int(_coalesce_profile_value(None, profile, "supervisor", "unknown_theme_lookback_minutes", 240))
    unknown_theme_limit = int(_coalesce_profile_value(None, profile, "supervisor", "unknown_theme_limit", 500))
    news_subscription_mesh_lookback_minutes = int(_coalesce_profile_value(None, profile, "supervisor", "news_subscription_mesh_lookback_minutes", 240))
    news_subscription_mesh_max_temp_symbols = int(_coalesce_profile_value(None, profile, "supervisor", "news_subscription_mesh_max_temp_symbols", 40))
    news_subscription_mesh_backfill_symbol_budget = int(_coalesce_profile_value(None, profile, "supervisor", "news_subscription_mesh_backfill_symbol_budget", 25))
    cross_provider_event_roles_lookback_minutes = int(_coalesce_profile_value(None, profile, "supervisor", "cross_provider_event_roles_lookback_minutes", 240))
    m236_outcome_attachment_max_records = int(_coalesce_profile_value(None, profile, "supervisor", "m236_outcome_attachment_max_records", 10))
    m236_outcome_attachment_max_symbols = int(_coalesce_profile_value(None, profile, "supervisor", "m236_outcome_attachment_max_symbols", 4))
    rebuild_dashboard = bool(_coalesce_profile_value(None, profile, "supervisor", "rebuild_dashboard", False))

    config = SourceOnlineSupervisorConfig(
        live_validation_profile_path=live_validation_profile_path,
        live_validation_json_path=live_validation_json_path,
        live_validation_markdown_path=live_validation_markdown_path,
        freshness_profile_path=freshness_profile_path,
        freshness_report_path=freshness_report_path,
        source_ingest_health_profile_path=source_ingest_health_profile_path,
        source_ingest_health_json_path=source_ingest_health_json_path,
        source_ingest_health_markdown_path=source_ingest_health_markdown_path,
        output_dir=output_dir,
        state_json_path=state_json_path,
        tick_log_jsonl_path=tick_log_jsonl_path,
        max_ticks=max_ticks,
        interval_seconds=interval_seconds,
        sleep_between_ticks=sleep_between_ticks,
        execute_actions=execute_actions,
        include_live_ibkr=include_live_ibkr,
        job_timeout_seconds=job_timeout_seconds,
        action_cooldown_seconds=action_cooldown_seconds,
        market_data_action_cooldown_seconds=market_data_action_cooldown_seconds,
        market_news_action_cooldown_seconds=market_news_action_cooldown_seconds,
        unknown_theme_action_cooldown_seconds=unknown_theme_action_cooldown_seconds,
        geopolitical_action_cooldown_seconds=geopolitical_action_cooldown_seconds,
        marketaux_alpha_action_cooldown_seconds=marketaux_alpha_action_cooldown_seconds,
        news_subscription_mesh_action_cooldown_seconds=news_subscription_mesh_action_cooldown_seconds,
        cross_provider_event_roles_action_cooldown_seconds=cross_provider_event_roles_action_cooldown_seconds,
        m236_news_learning_action_cooldown_seconds=m236_news_learning_action_cooldown_seconds,
        m236_outcome_attachment_action_cooldown_seconds=m236_outcome_attachment_action_cooldown_seconds,
        macro_release_interpreter_action_cooldown_seconds=macro_release_interpreter_action_cooldown_seconds,
        macro_event_calendar_action_cooldown_seconds=macro_event_calendar_action_cooldown_seconds,
        ibkr_validation_interval_seconds=ibkr_validation_interval_seconds,
        marketaux_alpha_validation_interval_seconds=marketaux_alpha_validation_interval_seconds,
        external_validation_interval_seconds=external_validation_interval_seconds,
        continuous_market_data=continuous_market_data,
        continuous_market_news=continuous_market_news,
        continuous_geopolitical=continuous_geopolitical,
        continuous_unknown_theme_discovery=continuous_unknown_theme_discovery,
        continuous_news_subscription_mesh=continuous_news_subscription_mesh,
        continuous_cross_provider_event_roles=continuous_cross_provider_event_roles,
        continuous_m236_news_learning=continuous_m236_news_learning,
        continuous_m236_outcome_attachment=continuous_m236_outcome_attachment,
        continuous_macro_release_interpreter=continuous_macro_release_interpreter,
        continuous_macro_event_calendar=continuous_macro_event_calendar,
        provider_rate_limit_cooldown_seconds=provider_rate_limit_cooldown_seconds,
        market_data_seconds=market_data_seconds,
        market_data_fallback_after_seconds=market_data_fallback_after_seconds,
        ibkr_news_limit_per_symbol=ibkr_news_limit_per_symbol,
        external_limit=external_limit,
        external_timespan=external_timespan,
        unknown_theme_lookback_minutes=unknown_theme_lookback_minutes,
        unknown_theme_limit=unknown_theme_limit,
        news_subscription_mesh_lookback_minutes=news_subscription_mesh_lookback_minutes,
        news_subscription_mesh_max_temp_symbols=news_subscription_mesh_max_temp_symbols,
        news_subscription_mesh_backfill_symbol_budget=news_subscription_mesh_backfill_symbol_budget,
        cross_provider_event_roles_lookback_minutes=cross_provider_event_roles_lookback_minutes,
        m236_outcome_attachment_max_records=m236_outcome_attachment_max_records,
        m236_outcome_attachment_max_symbols=m236_outcome_attachment_max_symbols,
        rebuild_dashboard=rebuild_dashboard,
        dashboard_profile_path=dashboard_profile_path,
        live_json_output_path=json_output_path,
    )
    result = _run_singleton(
        "source-online-supervisor",
        lambda: run_source_online_supervisor(config=config, settings=settings, cwd=Path.cwd(), run_id=run_id),
    )
    if result.get("status") == "skipped_already_running":
        if json_output:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            print(f"{result.get('command')} skipped: {result.get('reason')} ({(result.get('singleton') or {}).get('message')})")
        return result
    written = write_source_online_supervisor_markdown(output, result) if output else None
    json_written = write_source_online_supervisor_json(json_output_path, result) if json_output_path else None
    if json_output:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return result
    print(source_online_supervisor_markdown(result))
    if written:
        print(f"\nMarkdown report written: {written}")
    if json_written:
        print(f"JSON report written: {json_written}")
    return result

def command_evidence_freshness_desk(
    settings: Settings,
    profile_json_path: Path | None,
    database_path: Path | None,
    market_data_json: Path | None,
    economic_desk_json: Path | None,
    output: Path | None,
    json_output_path: Path | None,
    json_output: bool,
) -> dict[str, Any]:
    from recorder.evidence_operations import (
        build_evidence_freshness_report,
        evidence_freshness_markdown,
        write_evidence_freshness_json,
        write_evidence_freshness_markdown,
    )

    profile: dict[str, Any] = {}
    profile_base: Path | None = None
    if profile_json_path is not None:
        profile, profile_base = _load_profile_json(profile_json_path)
    database_path = _coalesce_profile_path(database_path, profile, "paths", "database_path", profile_base) or settings.database_path
    market_data_json = _coalesce_profile_path(market_data_json, profile, "paths", "market_data_json", profile_base)
    economic_desk_json = _coalesce_profile_path(economic_desk_json, profile, "paths", "economic_desk_json", profile_base)
    output = _coalesce_profile_path(output, profile, "outputs", "output", profile_base)
    json_output_path = _coalesce_profile_path(json_output_path, profile, "outputs", "json_output_path", profile_base)
    report = build_evidence_freshness_report(
        database_path=database_path,
        market_data_json_path=market_data_json,
        economic_desk_json_path=economic_desk_json,
    )
    written = write_evidence_freshness_markdown(output, report) if output else None
    json_written = write_evidence_freshness_json(json_output_path, report) if json_output_path else None
    if json_output:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return report
    print(evidence_freshness_markdown(report))
    if written:
        print(f"\nMarkdown report written: {written}")
    if json_written:
        print(f"JSON report written: {json_written}")
    return report


def command_evidence_refresh_queue(
    settings: Settings,
    profile_json_path: Path | None,
    freshness_report_json: Path | None,
    evidence_freshness_profile_json: Path | None,
    dashboard_profile_json: Path | None,
    output_dir: Path | None,
    market_data_seconds: int | None,
    external_limit: int | None,
    external_timespan: str | None,
    ibkr_news_limit_per_symbol: int | None,
    technical_start_days: int | None,
    execute: bool,
    max_jobs: int | None,
    job_timeout_seconds: float | None,
    skip_live_ibkr: bool,
    output: Path | None,
    json_output_path: Path | None,
    json_output: bool,
) -> dict[str, Any]:
    from recorder.evidence_operations import (
        EvidenceRefreshQueueConfig,
        build_evidence_refresh_queue,
        evidence_refresh_queue_markdown,
        write_evidence_refresh_queue_json,
        write_evidence_refresh_queue_markdown,
    )

    profile: dict[str, Any] = {}
    profile_base: Path | None = None
    if profile_json_path is not None:
        profile, profile_base = _load_profile_json(profile_json_path)
    freshness_report_json = (
        _coalesce_profile_path(freshness_report_json, profile, "paths", "freshness_report_json", profile_base)
        or Path("exports/m152_evidence_freshness_desk.json")
    )
    evidence_freshness_profile_json = (
        _coalesce_profile_path(evidence_freshness_profile_json, profile, "paths", "evidence_freshness_profile_json", profile_base)
        or Path("config/evidence_freshness_desk_profile_v1.json")
    )
    dashboard_profile_json = (
        _coalesce_profile_path(dashboard_profile_json, profile, "paths", "dashboard_profile_json", profile_base)
        or Path("config/trading_dashboard_snapshot_profile_v1.json")
    )
    output_dir = _coalesce_profile_path(output_dir, profile, "paths", "output_dir", profile_base) or Path("exports")
    output = _coalesce_profile_path(output, profile, "outputs", "output", profile_base)
    json_output_path = _coalesce_profile_path(json_output_path, profile, "outputs", "json_output_path", profile_base)
    market_data_seconds = int(_coalesce_profile_value(market_data_seconds, profile, "queue", "market_data_seconds", 8))
    external_limit = int(_coalesce_profile_value(external_limit, profile, "queue", "external_limit", 50))
    external_timespan = str(_coalesce_profile_value(external_timespan, profile, "queue", "external_timespan", "3d"))
    ibkr_news_limit_per_symbol = int(_coalesce_profile_value(ibkr_news_limit_per_symbol, profile, "queue", "ibkr_news_limit_per_symbol", 25))
    technical_start_days = int(_coalesce_profile_value(technical_start_days, profile, "queue", "technical_start_days", 14))
    max_jobs = _coalesce_profile_value(max_jobs, profile, "queue", "max_jobs", max_jobs)
    max_jobs = int(max_jobs) if max_jobs is not None else None
    job_timeout_seconds = float(_coalesce_profile_value(job_timeout_seconds, profile, "queue", "job_timeout_seconds", 120.0))
    profile_execute = bool(_coalesce_profile_value(None, profile, "queue", "execute", False))
    execute = bool(execute or profile_execute)
    include_live_ibkr = not bool(skip_live_ibkr)

    freshness_path = freshness_report_json.expanduser().resolve()
    if not freshness_path.exists():
        command_evidence_freshness_desk(
            settings,
            evidence_freshness_profile_json,
            None,
            None,
            None,
            None,
            freshness_path,
            False,
        )
    freshness_report = json.loads(freshness_path.read_text(encoding="utf-8"))
    config = EvidenceRefreshQueueConfig(
        freshness_report_path=freshness_path,
        evidence_freshness_profile_path=evidence_freshness_profile_json,
        dashboard_profile_path=dashboard_profile_json,
        output_dir=output_dir,
        market_data_seconds=market_data_seconds,
        external_limit=external_limit,
        external_timespan=external_timespan,
        ibkr_news_limit_per_symbol=ibkr_news_limit_per_symbol,
        technical_start_days=technical_start_days,
        execute=execute,
        max_jobs=max_jobs,
        job_timeout_seconds=job_timeout_seconds,
        include_live_ibkr=include_live_ibkr,
    )
    result = build_evidence_refresh_queue(
        freshness_report=freshness_report,
        settings=settings,
        config=config,
        cwd=Path.cwd(),
    )
    written = write_evidence_refresh_queue_markdown(output, result) if output else None
    json_written = write_evidence_refresh_queue_json(json_output_path, result) if json_output_path else None
    if json_output:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return result
    print(evidence_refresh_queue_markdown(result))
    if written:
        print(f"\nMarkdown report written: {written}")
    if json_written:
        print(f"JSON report written: {json_written}")
    return result


def command_evidence_refresh_watchdog(
    settings: Settings,
    profile_json_path: Path | None,
    freshness_profile_json: Path | None,
    refresh_queue_profile_json: Path | None,
    freshness_report_json: Path | None,
    refresh_queue_json: Path | None,
    refresh_queue_markdown: Path | None,
    state_json_path: Path | None,
    tick_log_jsonl_path: Path | None,
    max_ticks: int | None,
    interval_seconds: float | None,
    sleep_between_ticks: bool | None,
    cooldown_seconds: float | None,
    initial_backoff_seconds: float | None,
    max_backoff_seconds: float | None,
    queue_timeout_seconds: float | None,
    dry_run: bool,
    skip_freshness_recheck: bool,
    run_id: str | None,
    output: Path | None,
    json_output_path: Path | None,
    json_output: bool,
) -> dict[str, Any]:
    from recorder.evidence_operations import (
        EvidenceRefreshWatchdogConfig,
        evidence_refresh_watchdog_markdown,
        run_evidence_refresh_watchdog,
        write_evidence_refresh_watchdog_json,
        write_evidence_refresh_watchdog_markdown,
    )

    profile: dict[str, Any] = {}
    profile_base: Path | None = None
    if profile_json_path is not None:
        profile, profile_base = _load_profile_json(profile_json_path)
    freshness_profile_json = (
        _coalesce_profile_path(freshness_profile_json, profile, "paths", "freshness_profile_json", profile_base)
        or Path("config/evidence_freshness_desk_profile_v1.json")
    )
    refresh_queue_profile_json = (
        _coalesce_profile_path(refresh_queue_profile_json, profile, "paths", "refresh_queue_profile_json", profile_base)
        or Path("config/evidence_refresh_queue_profile_v1.json")
    )
    freshness_report_json = (
        _coalesce_profile_path(freshness_report_json, profile, "paths", "freshness_report_json", profile_base)
        or Path("exports/m152_evidence_freshness_desk.json")
    )
    refresh_queue_json = (
        _coalesce_profile_path(refresh_queue_json, profile, "paths", "refresh_queue_json", profile_base)
        or Path("exports/m153_evidence_refresh_queue_execute.json")
    )
    refresh_queue_markdown = (
        _coalesce_profile_path(refresh_queue_markdown, profile, "paths", "refresh_queue_markdown", profile_base)
        or Path("exports/m153_evidence_refresh_queue_execute.md")
    )
    state_json_path = (
        _coalesce_profile_path(state_json_path, profile, "outputs", "state_json_path", profile_base)
        or Path("exports/m154_evidence_refresh_watchdog_state.json")
    )
    tick_log_jsonl_path = _coalesce_profile_path(tick_log_jsonl_path, profile, "outputs", "tick_log_jsonl_path", profile_base)
    output = _coalesce_profile_path(output, profile, "outputs", "output", profile_base)
    json_output_path = _coalesce_profile_path(json_output_path, profile, "outputs", "json_output_path", profile_base)
    max_ticks = int(_coalesce_profile_value(max_ticks, profile, "watchdog", "max_ticks", 1))
    interval_seconds = float(_coalesce_profile_value(interval_seconds, profile, "watchdog", "interval_seconds", 1.0))
    sleep_between_ticks = bool(_coalesce_profile_value(sleep_between_ticks, profile, "watchdog", "sleep_between_ticks", False))
    cooldown_seconds = float(_coalesce_profile_value(cooldown_seconds, profile, "watchdog", "cooldown_seconds", 300.0))
    initial_backoff_seconds = float(_coalesce_profile_value(initial_backoff_seconds, profile, "watchdog", "initial_backoff_seconds", 120.0))
    max_backoff_seconds = float(_coalesce_profile_value(max_backoff_seconds, profile, "watchdog", "max_backoff_seconds", 1800.0))
    queue_timeout_seconds = float(_coalesce_profile_value(queue_timeout_seconds, profile, "watchdog", "queue_timeout_seconds", 240.0))
    profile_dry_run = bool(_coalesce_profile_value(None, profile, "watchdog", "dry_run", False))
    execute_refresh = not bool(dry_run or profile_dry_run)
    run_freshness_recheck = not bool(skip_freshness_recheck)
    config = EvidenceRefreshWatchdogConfig(
        freshness_profile_path=freshness_profile_json,
        refresh_queue_profile_path=refresh_queue_profile_json,
        freshness_report_path=freshness_report_json,
        refresh_queue_json_path=refresh_queue_json,
        refresh_queue_markdown_path=refresh_queue_markdown,
        state_json_path=state_json_path,
        tick_log_jsonl_path=tick_log_jsonl_path,
        max_ticks=max_ticks,
        interval_seconds=interval_seconds,
        sleep_between_ticks=sleep_between_ticks,
        cooldown_seconds=cooldown_seconds,
        initial_backoff_seconds=initial_backoff_seconds,
        max_backoff_seconds=max_backoff_seconds,
        queue_timeout_seconds=queue_timeout_seconds,
        execute_refresh=execute_refresh,
        run_freshness_recheck=run_freshness_recheck,
    )
    result = run_evidence_refresh_watchdog(config=config, cwd=Path.cwd(), run_id=run_id)
    written = write_evidence_refresh_watchdog_markdown(output, result) if output else None
    json_written = write_evidence_refresh_watchdog_json(json_output_path, result) if json_output_path else None
    if json_output:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return result
    print(evidence_refresh_watchdog_markdown(result))
    if written:
        print(f"\nMarkdown report written: {written}")
    if json_written:
        print(f"JSON report written: {json_written}")
    return result


def command_source_ingest_health(
    settings: Settings,
    profile_json_path: Path | None,
    freshness_report_json: Path | None,
    refresh_queue_json: Path | None,
    watchdog_json: Path | None,
    live_validation_json: Path | None,
    online_required_sources: str | None,
    batch_sources: str | None,
    refresh_freshness_before_check: bool,
    max_ticks: int | None,
    interval_seconds: float | None,
    sleep_between_ticks: bool | None,
    run_id: str | None,
    output: Path | None,
    json_output_path: Path | None,
    tick_log_jsonl_path: Path | None,
    json_output: bool,
) -> dict[str, Any]:
    from recorder.source_operations import (
        DEFAULT_BATCH_SOURCES,
        DEFAULT_ONLINE_REQUIRED_SOURCES,
        SourceIngestHealthConfig,
        run_source_ingest_health_loop,
        source_ingest_health_markdown,
        write_source_ingest_health_json,
        write_source_ingest_health_markdown,
    )

    profile: dict[str, Any] = {}
    profile_base: Path | None = None
    if profile_json_path is not None:
        profile, profile_base = _load_profile_json(profile_json_path)

    freshness_report_json = (
        _coalesce_profile_path(freshness_report_json, profile, "paths", "freshness_report_json", profile_base)
        or Path("exports/m152_evidence_freshness_desk.json")
    )
    refresh_queue_json = (
        _coalesce_profile_path(refresh_queue_json, profile, "paths", "refresh_queue_json", profile_base)
        or Path("exports/m153_evidence_refresh_queue_execute.json")
    )
    watchdog_json = (
        _coalesce_profile_path(watchdog_json, profile, "paths", "watchdog_json", profile_base)
        or _coalesce_profile_path(None, profile, "paths", "evidence_refresh_watchdog_json", profile_base)
        or Path("exports/m159_dashboard_evidence_refresh_watchdog_latest.json")
    )
    live_validation_json = (
        _coalesce_profile_path(live_validation_json, profile, "paths", "live_validation_json", profile_base)
        or Path("exports/m161_source_live_validation.json")
    )
    output = _coalesce_profile_path(output, profile, "outputs", "output", profile_base)
    json_output_path = _coalesce_profile_path(json_output_path, profile, "outputs", "json_output_path", profile_base)
    tick_log_jsonl_path = _coalesce_profile_path(tick_log_jsonl_path, profile, "outputs", "tick_log_jsonl_path", profile_base)
    max_ticks = int(_coalesce_profile_value(max_ticks, profile, "loop", "max_ticks", 1))
    interval_seconds = float(_coalesce_profile_value(interval_seconds, profile, "loop", "interval_seconds", 1.0))
    sleep_between_ticks = bool(_coalesce_profile_value(sleep_between_ticks, profile, "loop", "sleep_between_ticks", False))
    refresh_freshness_before_check = bool(
        refresh_freshness_before_check
        or _coalesce_profile_value(None, profile, "loop", "refresh_freshness_before_check", False)
    )
    run_id = str(_coalesce_profile_value(run_id, profile, "loop", "run_id", "") or "") or run_id

    def _csv_or_default(value: str | None, default: tuple[str, ...], profile_key: str) -> tuple[str, ...]:
        profile_value = _coalesce_profile_value(None, profile, "sources", profile_key, None)
        raw = value if value is not None else profile_value
        if isinstance(raw, list):
            return tuple(str(item).strip() for item in raw if str(item).strip())
        if raw:
            return tuple(item.strip() for item in str(raw).split(",") if item.strip())
        return default

    config = SourceIngestHealthConfig(
        freshness_report_path=freshness_report_json,
        refresh_queue_path=refresh_queue_json,
        watchdog_path=watchdog_json,
        live_validation_path=live_validation_json,
        online_required_sources=_csv_or_default(online_required_sources, DEFAULT_ONLINE_REQUIRED_SOURCES, "online_required_sources"),
        batch_sources=_csv_or_default(batch_sources, DEFAULT_BATCH_SOURCES, "batch_sources"),
        max_ticks=max_ticks,
        interval_seconds=interval_seconds,
        sleep_between_ticks=sleep_between_ticks,
    )

    def refresh_hook(_tick_index: int, _run_id: str) -> None:
        command_evidence_freshness_desk(
            settings,
            Path("config/evidence_freshness_desk_profile_v1.json"),
            None,
            None,
            None,
            None,
            freshness_report_json,
            False,
        )

    if refresh_freshness_before_check or not freshness_report_json.expanduser().resolve().exists():
        refresh_hook(0, run_id or "source_ingest_health")

    result = run_source_ingest_health_loop(
        config=config,
        output_path=output,
        json_output_path=json_output_path,
        tick_log_jsonl_path=tick_log_jsonl_path,
        run_id=run_id,
        refresh_hook=refresh_hook if refresh_freshness_before_check else None,
    )
    latest_report = result.get("latest_report") or {}
    if output and max_ticks == 1:
        write_source_ingest_health_markdown(output, latest_report)
    if json_output_path and max_ticks == 1:
        write_source_ingest_health_json(json_output_path, latest_report)
    if json_output:
        print(json.dumps(result if max_ticks != 1 else latest_report, indent=2, ensure_ascii=False))
        return result
    print(source_ingest_health_markdown(latest_report))
    if output:
        print(f"\nMarkdown report written: {output.expanduser().resolve()}")
    if json_output_path:
        print(f"JSON report written: {json_output_path.expanduser().resolve()}")
    return result


def command_source_live_validation(
    settings: Settings,
    profile_json_path: Path | None,
    symbols: str | None,
    providers: str | None,
    external_symbols: str | None,
    alpha_tickers: str | None,
    limit: int | None,
    ibkr_timeout_seconds: float | None,
    external_timeout_seconds: float | None,
    ibkr_client_id: int | None,
    skip_ibkr: bool,
    skip_marketaux: bool,
    skip_alpha_vantage: bool,
    output: Path | None,
    json_output_path: Path | None,
    json_output: bool,
) -> dict[str, Any]:
    from recorder.source_operations import (
        SourceLiveValidationConfig,
        build_source_live_validation_report,
        source_live_validation_markdown,
        write_source_live_validation_json,
        write_source_live_validation_markdown,
    )

    profile: dict[str, Any] = {}
    profile_base: Path | None = None
    if profile_json_path is not None:
        profile, profile_base = _load_profile_json(profile_json_path)

    def csv_or_default(value: str | None, section: str, key: str, default: tuple[str, ...]) -> tuple[str, ...]:
        raw = value if value is not None else _coalesce_profile_value(None, profile, section, key, None)
        if isinstance(raw, list):
            return tuple(str(item).strip().upper() for item in raw if str(item).strip())
        if raw:
            return tuple(item.strip().upper() for item in str(raw).split(",") if item.strip())
        return default

    output = _coalesce_profile_path(output, profile, "outputs", "output", profile_base)
    json_output_path = _coalesce_profile_path(json_output_path, profile, "outputs", "json_output_path", profile_base)
    config = SourceLiveValidationConfig(
        symbols=csv_or_default(symbols, "ibkr", "symbols", settings.all_news_symbols[:5]),
        providers=csv_or_default(providers, "ibkr", "providers", settings.news_provider_codes),
        external_symbols=csv_or_default(external_symbols, "external", "marketaux_symbols", settings.geopolitical_news_symbols[:8]),
        alpha_tickers=csv_or_default(alpha_tickers, "external", "alpha_tickers", settings.geopolitical_news_symbols[:8]),
        marketaux_search=_coalesce_profile_value(None, profile, "external", "marketaux_search", "geopolitical OR sanctions OR Taiwan OR oil OR war"),
        marketaux_filter_entities=bool(_coalesce_profile_value(None, profile, "external", "marketaux_filter_entities", True)),
        alpha_topics=_coalesce_profile_value(None, profile, "external", "alpha_topics", None),
        limit=int(_coalesce_profile_value(limit, profile, "validation", "limit", 10)),
        ibkr_timeout_seconds=float(_coalesce_profile_value(ibkr_timeout_seconds, profile, "validation", "ibkr_timeout_seconds", 20.0)),
        external_timeout_seconds=float(_coalesce_profile_value(external_timeout_seconds, profile, "validation", "external_timeout_seconds", 20.0)),
        external_retry_count=int(_coalesce_profile_value(None, profile, "validation", "external_retry_count", 1)),
        external_retry_sleep_seconds=float(_coalesce_profile_value(None, profile, "validation", "external_retry_sleep_seconds", 0.5)),
        ibkr_client_id=ibkr_client_id if ibkr_client_id is not None else _coalesce_profile_value(None, profile, "ibkr", "client_id", None),
        include_ibkr=not bool(skip_ibkr or _coalesce_profile_value(None, profile, "validation", "skip_ibkr", False)),
        include_marketaux=not bool(skip_marketaux or _coalesce_profile_value(None, profile, "validation", "skip_marketaux", False)),
        include_alpha_vantage=not bool(skip_alpha_vantage or _coalesce_profile_value(None, profile, "validation", "skip_alpha_vantage", True)),
    )
    report = build_source_live_validation_report(settings=settings, config=config)
    written = write_source_live_validation_markdown(output, report) if output else None
    json_written = write_source_live_validation_json(json_output_path, report) if json_output_path else None
    if json_output:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return report
    print(source_live_validation_markdown(report))
    if written:
        print(f"\nMarkdown report written: {written}")
    if json_written:
        print(f"JSON report written: {json_written}")
    return report


def command_evidence_preflight(
    profile_json_path: Path | None,
    freshness_report_json: Path | None,
    watchdog_state_json: Path | None,
    block_on_stale: bool | None,
    block_on_offline: bool | None,
    block_on_watchdog_failure: bool | None,
    allow_lagging_with_haircut: bool | None,
    lagging_size_multiplier: float | None,
    min_source_weight: float | None,
    output: Path | None,
    json_output_path: Path | None,
    json_output: bool,
) -> dict[str, Any]:
    from recorder.evidence_operations import (
        build_evidence_preflight_report,
        evidence_preflight_markdown,
        write_evidence_preflight_json,
        write_evidence_preflight_markdown,
    )

    profile: dict[str, Any] = {}
    profile_base: Path | None = None
    if profile_json_path is not None:
        profile, profile_base = _load_profile_json(profile_json_path)
    freshness_report_json = (
        _coalesce_profile_path(freshness_report_json, profile, "paths", "freshness_report_json", profile_base)
        or Path("exports/m152_evidence_freshness_desk.json")
    )
    watchdog_state_json = (
        _coalesce_profile_path(watchdog_state_json, profile, "paths", "watchdog_state_json", profile_base)
        or Path("exports/m154_evidence_refresh_watchdog_state.json")
    )
    output = _coalesce_profile_path(output, profile, "outputs", "output", profile_base)
    json_output_path = _coalesce_profile_path(json_output_path, profile, "outputs", "json_output_path", profile_base)
    block_on_stale = bool(_coalesce_profile_value(block_on_stale, profile, "preflight", "block_on_stale", True))
    block_on_offline = bool(_coalesce_profile_value(block_on_offline, profile, "preflight", "block_on_offline", True))
    block_on_watchdog_failure = bool(_coalesce_profile_value(block_on_watchdog_failure, profile, "preflight", "block_on_watchdog_failure", True))
    allow_lagging_with_haircut = bool(_coalesce_profile_value(allow_lagging_with_haircut, profile, "preflight", "allow_lagging_with_haircut", True))
    lagging_size_multiplier = float(_coalesce_profile_value(lagging_size_multiplier, profile, "preflight", "lagging_size_multiplier", 0.5))
    min_source_weight = float(_coalesce_profile_value(min_source_weight, profile, "preflight", "min_source_weight", 0.25))

    freshness_path = freshness_report_json.expanduser().resolve()
    if not freshness_path.exists():
        raise FileNotFoundError(f"Evidence freshness report not found: {freshness_path}")
    freshness_payload = json.loads(freshness_path.read_text(encoding="utf-8"))
    watchdog_payload = None
    watchdog_path = watchdog_state_json.expanduser().resolve()
    if watchdog_path.exists():
        watchdog_payload = json.loads(watchdog_path.read_text(encoding="utf-8"))
    result = build_evidence_preflight_report(
        freshness_payload,
        watchdog_payload=watchdog_payload,
        block_on_stale=block_on_stale,
        block_on_offline=block_on_offline,
        block_on_watchdog_failure=block_on_watchdog_failure,
        allow_lagging_with_haircut=allow_lagging_with_haircut,
        lagging_size_multiplier=lagging_size_multiplier,
        min_source_weight=min_source_weight,
    )
    written = write_evidence_preflight_markdown(output, result) if output else None
    json_written = write_evidence_preflight_json(json_output_path, result) if json_output_path else None
    if json_output:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return result
    print(evidence_preflight_markdown(result))
    if written:
        print(f"\nMarkdown report written: {written}")
    if json_written:
        print(f"JSON report written: {json_written}")
    return result


def command_market_data_risk_gate(
    profile_json_path: Path | None,
    market_data_json: Path | None,
    market_data_session_overlay_json: Path | None,
    block_on_delayed: bool | None,
    block_on_missing: bool | None,
    block_on_warming_up: bool | None,
    block_on_errors: bool | None,
    block_on_stale: bool | None,
    block_on_close_only: bool | None,
    block_on_severe_wide_spread: bool | None,
    watch_size_multiplier: float | None,
    hot_size_multiplier: float | None,
    defensive_size_multiplier: float | None,
    stress_score_threshold: float | None,
    output: Path | None,
    json_output_path: Path | None,
    json_output: bool,
) -> dict[str, Any]:
    from recorder.market_data_risk_gate import (
        build_market_data_risk_gate_report,
        market_data_risk_gate_markdown,
        write_market_data_risk_gate_json,
        write_market_data_risk_gate_markdown,
    )

    profile: dict[str, Any] = {}
    profile_base: Path | None = None
    if profile_json_path is not None:
        profile, profile_base = _load_profile_json(profile_json_path)
    market_data_json = (
        _coalesce_profile_path(market_data_json, profile, "paths", "market_data_json", profile_base)
        or Path("exports/m153_market_data_desk_refresh.json")
    )
    market_data_session_overlay_json = _coalesce_profile_path(
        market_data_session_overlay_json,
        profile,
        "paths",
        "market_data_session_overlay_json",
        profile_base,
    )
    macro_release_interpreter_json = _coalesce_profile_path(
        None,
        profile,
        "paths",
        "macro_release_interpreter_json",
        profile_base,
    )
    options_market_structure_json = _coalesce_profile_path(
        None,
        profile,
        "paths",
        "options_market_structure_json",
        profile_base,
    )
    options_market_structure_supervisor_json = _coalesce_profile_path(
        None,
        profile,
        "paths",
        "options_market_structure_supervisor_json",
        profile_base,
    )
    output = _coalesce_profile_path(output, profile, "outputs", "output", profile_base)
    json_output_path = _coalesce_profile_path(json_output_path, profile, "outputs", "json_output_path", profile_base)
    block_on_delayed = bool(_coalesce_profile_value(block_on_delayed, profile, "gate", "block_on_delayed", True))
    block_on_missing = bool(_coalesce_profile_value(block_on_missing, profile, "gate", "block_on_missing", True))
    block_on_warming_up = bool(_coalesce_profile_value(block_on_warming_up, profile, "gate", "block_on_warming_up", True))
    block_on_errors = bool(_coalesce_profile_value(block_on_errors, profile, "gate", "block_on_errors", True))
    block_on_stale = bool(_coalesce_profile_value(block_on_stale, profile, "gate", "block_on_stale", True))
    block_on_close_only = bool(_coalesce_profile_value(block_on_close_only, profile, "gate", "block_on_close_only", False))
    block_on_severe_wide_spread = bool(_coalesce_profile_value(block_on_severe_wide_spread, profile, "gate", "block_on_severe_wide_spread", False))
    watch_size_multiplier = float(_coalesce_profile_value(watch_size_multiplier, profile, "gate", "watch_size_multiplier", 0.70))
    hot_size_multiplier = float(_coalesce_profile_value(hot_size_multiplier, profile, "gate", "hot_size_multiplier", 0.50))
    defensive_size_multiplier = float(_coalesce_profile_value(defensive_size_multiplier, profile, "gate", "defensive_size_multiplier", 0.0))
    stress_score_threshold = float(_coalesce_profile_value(stress_score_threshold, profile, "gate", "stress_score_threshold", 30.0))
    stale_hard_block_instruments = _coalesce_profile_value(None, profile, "gate", "stale_hard_block_instruments", None)
    monitoring_only_instruments = _coalesce_profile_value(None, profile, "gate", "monitoring_only_instruments", None)
    regular_session_only_stale_hard_block_instruments = _coalesce_profile_value(None, profile, "gate", "regular_session_only_stale_hard_block_instruments", None)
    stale_hard_block_proxy_groups = _coalesce_profile_value(None, profile, "gate", "stale_hard_block_proxy_groups", None)
    macro_event_window_minutes = float(_coalesce_profile_value(None, profile, "gate", "macro_event_window_minutes", 180.0))
    closed_session_expected_stale_min_wait_hours = float(
        _coalesce_profile_value(None, profile, "gate", "closed_session_expected_stale_min_wait_hours", 24.0)
    )

    market_data_path = market_data_json.expanduser().resolve()
    if not market_data_path.exists():
        raise FileNotFoundError(f"Market Data Desk JSON not found: {market_data_path}")
    market_data_payload = json.loads(market_data_path.read_text(encoding="utf-8"))
    market_data_session_overlay_payload = None
    if market_data_session_overlay_json is not None:
        session_path = market_data_session_overlay_json.expanduser().resolve()
        if session_path.exists():
            market_data_session_overlay_payload = json.loads(session_path.read_text(encoding="utf-8"))
    macro_release_interpreter_payload = None
    if macro_release_interpreter_json is not None:
        macro_path = macro_release_interpreter_json.expanduser().resolve()
        if macro_path.exists():
            macro_release_interpreter_payload = json.loads(macro_path.read_text(encoding="utf-8"))
    options_market_structure_payload = None
    if options_market_structure_json is not None:
        option_path = options_market_structure_json.expanduser().resolve()
        if option_path.exists():
            options_market_structure_payload = json.loads(option_path.read_text(encoding="utf-8"))
    options_market_structure_supervisor_payload = None
    if options_market_structure_supervisor_json is not None:
        option_supervisor_path = options_market_structure_supervisor_json.expanduser().resolve()
        if option_supervisor_path.exists():
            options_market_structure_supervisor_payload = json.loads(option_supervisor_path.read_text(encoding="utf-8"))
    result = build_market_data_risk_gate_report(
        market_data_payload,
        market_data_session_overlay_payload=market_data_session_overlay_payload,
        macro_release_interpreter_payload=macro_release_interpreter_payload,
        options_market_structure_payload=options_market_structure_payload,
        options_market_structure_supervisor_payload=options_market_structure_supervisor_payload,
        block_on_delayed=block_on_delayed,
        block_on_missing=block_on_missing,
        block_on_warming_up=block_on_warming_up,
        block_on_errors=block_on_errors,
        block_on_stale=block_on_stale,
        block_on_close_only=block_on_close_only,
        block_on_severe_wide_spread=block_on_severe_wide_spread,
        watch_size_multiplier=watch_size_multiplier,
        hot_size_multiplier=hot_size_multiplier,
        defensive_size_multiplier=defensive_size_multiplier,
        stress_score_threshold=stress_score_threshold,
        stale_hard_block_instruments=stale_hard_block_instruments,
        monitoring_only_instruments=monitoring_only_instruments,
        regular_session_only_stale_hard_block_instruments=regular_session_only_stale_hard_block_instruments,
        stale_hard_block_proxy_groups=stale_hard_block_proxy_groups,
        macro_event_window_minutes=macro_event_window_minutes,
        closed_session_expected_stale_min_wait_hours=closed_session_expected_stale_min_wait_hours,
    )
    result["artifact_role"] = "manual_debug"
    result["canonical_latest_artifact"] = "exports/m175_dashboard_market_data_risk_gate_latest.json"
    result["canonical_latest_policy"] = "dashboard_m175_is_canonical_latest; m174 is a manual/debug report"
    written = write_market_data_risk_gate_markdown(output, result) if output else None
    json_written = write_market_data_risk_gate_json(json_output_path, result) if json_output_path else None
    if json_output:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return result
    print(market_data_risk_gate_markdown(result))
    if written:
        print(f"\nMarkdown report written: {written}")
    if json_written:
        print(f"JSON report written: {json_written}")
    return result


def command_market_data_session_overlay(
    market_data_json_path: Path,
    now: str | None,
    output: Path | None,
    json_output_path: Path | None,
    json_output: bool,
) -> dict[str, Any]:
    from recorder.market_data_session_overlay import (
        build_market_data_session_overlay,
        write_market_data_session_overlay_json,
        write_market_data_session_overlay_markdown,
    )

    market_data_json_path = market_data_json_path.expanduser().resolve()
    if not market_data_json_path.exists():
        raise FileNotFoundError(f"Market Data Desk JSON not found: {market_data_json_path}")
    payload = json.loads(market_data_json_path.read_text(encoding="utf-8"))
    result = build_market_data_session_overlay(payload, now=now)
    written = write_market_data_session_overlay_markdown(output, result) if output else None
    json_written = write_market_data_session_overlay_json(json_output_path, result) if json_output_path else None
    if json_output:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return result
    print(_console_safe_text(market_data_session_overlay_markdown(result)))
    if written:
        print(f"\nMarkdown report written: {written}")
    if json_written:
        print(f"JSON report written: {json_written}")
    return result


def command_market_data_session_transition_gate(
    market_data_session_overlay_json: Path,
    state_json_path: Path | None,
    market_data_risk_gate_json: Path | None,
    market_data_operating_auto_gate_json: Path | None,
    now: str | None,
    max_session_overlay_age_seconds: float,
    max_risk_gate_age_seconds: float,
    max_operating_gate_age_seconds: float,
    no_state_update: bool,
    output: Path | None,
    json_output_path: Path | None,
    json_output: bool,
) -> dict[str, Any]:
    from recorder.market_transition_operations import (
        build_market_data_session_transition_gate_from_paths,
        market_data_session_transition_gate_markdown,
        write_market_data_session_transition_gate_json,
        write_market_data_session_transition_gate_markdown,
        write_market_data_session_transition_state,
    )

    result = build_market_data_session_transition_gate_from_paths(
        current_session_overlay_path=market_data_session_overlay_json,
        state_path=state_json_path,
        market_data_risk_gate_path=market_data_risk_gate_json,
        market_data_operating_auto_gate_path=market_data_operating_auto_gate_json,
        now=now,
        max_session_overlay_age_seconds=max_session_overlay_age_seconds,
        max_risk_gate_age_seconds=max_risk_gate_age_seconds,
        max_operating_gate_age_seconds=max_operating_gate_age_seconds,
    )
    written = write_market_data_session_transition_gate_markdown(output, result) if output else None
    json_written = write_market_data_session_transition_gate_json(json_output_path, result) if json_output_path else None
    state_written = None
    if state_json_path is not None and not no_state_update:
        state_written = write_market_data_session_transition_state(state_json_path, result)
    if json_output:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return result
    print(_console_safe_text(market_data_session_transition_gate_markdown(result)))
    if written:
        print(f"\nMarkdown report written: {written}")
    if json_written:
        print(f"JSON report written: {json_written}")
    if state_written:
        print(f"State written: {state_written}")
    return result


def command_market_data_session_transition_closed_loop(
    market_data_json: Path,
    market_data_session_overlay_json: Path,
    state_json_path: Path | None,
    market_data_risk_gate_json: Path | None,
    market_data_operating_auto_gate_json: Path | None,
    preview_json: Path,
    short_availability_json: Path | None,
    evidence_preflight_json: Path | None,
    output_dir: Path,
    dry_run: bool,
    max_session_overlay_age_seconds: float,
    max_risk_gate_age_seconds: float,
    max_operating_gate_age_seconds: float,
    max_auto_orders: int,
    min_cash_buffer: float,
    allowed_ticket_types: str,
    allowed_actions: str,
    sample_limit: int,
    output: Path | None,
    json_output_path: Path | None,
    json_output: bool,
) -> dict[str, Any]:
    from recorder.market_transition_operations import (
        market_data_session_transition_closed_loop_markdown,
        run_market_data_session_transition_closed_loop,
        write_market_data_session_transition_closed_loop_json,
        write_market_data_session_transition_closed_loop_markdown,
    )

    ticket_types = tuple(item.strip() for item in str(allowed_ticket_types or "").split(",") if item.strip())
    actions = tuple(item.strip() for item in str(allowed_actions or "").split(",") if item.strip())
    result = run_market_data_session_transition_closed_loop(
        market_data_json_path=market_data_json,
        current_session_overlay_path=market_data_session_overlay_json,
        previous_state_path=state_json_path,
        market_data_risk_gate_path=market_data_risk_gate_json,
        market_data_operating_auto_gate_path=market_data_operating_auto_gate_json,
        preview_json_path=preview_json,
        short_availability_json_path=short_availability_json,
        evidence_preflight_json_path=evidence_preflight_json,
        output_dir=output_dir,
        execute=not bool(dry_run),
        max_session_overlay_age_seconds=max_session_overlay_age_seconds,
        max_risk_gate_age_seconds=max_risk_gate_age_seconds,
        max_operating_gate_age_seconds=max_operating_gate_age_seconds,
        max_auto_orders=max_auto_orders,
        min_cash_buffer=min_cash_buffer,
        allowed_ticket_types=ticket_types or ("stock",),
        allowed_actions=actions or ("BUY", "SELL"),
        sample_limit=sample_limit,
    )
    written = write_market_data_session_transition_closed_loop_markdown(output, result) if output else None
    json_written = write_market_data_session_transition_closed_loop_json(json_output_path, result) if json_output_path else None
    if json_output:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return result
    print(_console_safe_text(market_data_session_transition_closed_loop_markdown(result)))
    if written:
        print(f"\nMarkdown report written: {written}")
    if json_written:
        print(f"JSON report written: {json_written}")
    return result


def command_session_transition_release_governor(
    transition_gate_json: Path,
    market_data_risk_gate_json: Path | None,
    market_data_operating_auto_gate_json: Path | None,
    portfolio_risk_state_json: Path | None,
    evidence_preflight_json: Path | None,
    operating_final_preflight_json: Path | None,
    now: str | None,
    output: Path | None,
    json_output_path: Path | None,
    json_output: bool,
) -> dict[str, Any]:
    from recorder.market_transition_operations import (
        build_session_transition_release_governor_from_paths,
        session_transition_release_governor_markdown,
        write_session_transition_release_governor_json,
        write_session_transition_release_governor_markdown,
    )

    result = build_session_transition_release_governor_from_paths(
        transition_gate_path=transition_gate_json,
        market_data_risk_gate_path=market_data_risk_gate_json,
        market_data_operating_auto_gate_path=market_data_operating_auto_gate_json,
        portfolio_risk_state_path=portfolio_risk_state_json,
        evidence_preflight_path=evidence_preflight_json,
        operating_final_preflight_path=operating_final_preflight_json,
        now=now,
    )
    written = write_session_transition_release_governor_markdown(output, result) if output else None
    json_written = write_session_transition_release_governor_json(json_output_path, result) if json_output_path else None
    if json_output:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return result
    print(_console_safe_text(session_transition_release_governor_markdown(result)))
    if written:
        print(f"\nMarkdown report written: {written}")
    if json_written:
        print(f"JSON report written: {json_written}")
    return result


def command_release_governed_recovery_closed_loop(
    transition_gate_json: Path,
    market_data_risk_gate_json: Path | None,
    market_data_operating_auto_gate_json: Path,
    portfolio_risk_state_json: Path | None,
    evidence_preflight_json: Path | None,
    operating_final_preflight_json: Path | None,
    snapshot_json: Path | None,
    existing_ledger_json: Path | None,
    existing_router_json: Path | None,
    marks_json: Path | None,
    release_governor_output: Path | None,
    release_governor_json_output_path: Path | None,
    ledger_output: Path | None,
    ledger_json_output_path: Path | None,
    router_output: Path | None,
    router_json_output_path: Path | None,
    output: Path | None,
    json_output_path: Path | None,
    monitor_size_multiplier: float,
    max_intents: int,
    max_orders: int,
    sample_limit: int,
    require_operating_preflight: bool,
    allow_non_operating_source_ledger: bool,
    require_portfolio_risk_gate: bool,
    require_session_transition_release_governor: bool,
    fill_price_source: str,
    now: str | None,
    json_output: bool,
) -> dict[str, Any]:
    from recorder.market_transition_operations import (
        release_governed_recovery_closed_loop_markdown,
        run_release_governed_recovery_closed_loop,
        write_release_governed_recovery_closed_loop_json,
        write_release_governed_recovery_closed_loop_markdown,
    )

    result = run_release_governed_recovery_closed_loop(
        transition_gate_path=transition_gate_json,
        market_data_risk_gate_path=market_data_risk_gate_json,
        market_data_operating_auto_gate_path=market_data_operating_auto_gate_json,
        portfolio_risk_state_path=portfolio_risk_state_json,
        evidence_preflight_path=evidence_preflight_json,
        operating_final_preflight_path=operating_final_preflight_json,
        snapshot_path=snapshot_json,
        existing_ledger_path=existing_ledger_json,
        existing_router_path=existing_router_json,
        marks_path=marks_json,
        release_governor_markdown_output_path=release_governor_output,
        release_governor_json_output_path=release_governor_json_output_path,
        ledger_markdown_output_path=ledger_output,
        ledger_json_output_path=ledger_json_output_path,
        router_markdown_output_path=router_output,
        router_json_output_path=router_json_output_path,
        monitor_size_multiplier=monitor_size_multiplier,
        max_intents=max_intents,
        max_orders=max_orders,
        sample_limit=sample_limit,
        require_operating_preflight=require_operating_preflight,
        allow_non_operating_source_ledger=allow_non_operating_source_ledger,
        require_portfolio_risk_gate=require_portfolio_risk_gate,
        require_session_transition_release_governor=require_session_transition_release_governor,
        fill_price_source=fill_price_source,
        now=now,
    )
    written = write_release_governed_recovery_closed_loop_markdown(output, result) if output else None
    json_written = write_release_governed_recovery_closed_loop_json(json_output_path, result) if json_output_path else None
    if json_output:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return result
    print(_console_safe_text(release_governed_recovery_closed_loop_markdown(result)))
    if written:
        print(f"\nMarkdown report written: {written}")
    if json_written:
        print(f"JSON report written: {json_written}")
    return result


def command_market_data_defensive_mode(
    market_data_risk_gate_json: Path,
    state_json_path: Path | None,
    persistent_warning_seconds: float,
    persistent_critical_seconds: float,
    output: Path | None,
    json_output_path: Path | None,
    json_output: bool,
) -> dict[str, Any]:
    from recorder.market_data_defensive_mode import (
        build_market_data_defensive_mode_from_paths,
        market_data_defensive_mode_markdown,
        write_market_data_defensive_mode_json,
        write_market_data_defensive_mode_markdown,
    )

    report = build_market_data_defensive_mode_from_paths(
        market_data_risk_gate_path=market_data_risk_gate_json,
        state_path=state_json_path,
        persistent_warning_seconds=persistent_warning_seconds,
        persistent_critical_seconds=persistent_critical_seconds,
    )
    written = write_market_data_defensive_mode_markdown(output, report) if output else None
    json_written = write_market_data_defensive_mode_json(json_output_path, report) if json_output_path else None
    if json_output:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return report
    print(_console_safe_text(market_data_defensive_mode_markdown(report)))
    if written:
        print(f"\nMarket data defensive mode Markdown written: {written}")
    if json_written:
        print(f"Market data defensive mode JSON written: {json_written}")
    return report


def command_market_data_operating_auto_gate(
    auto_gate_json_path: Path,
    defensive_mode_json_path: Path,
    sample_limit: int,
    output: Path | None = None,
    json_output_path: Path | None = None,
    json_output: bool = False,
) -> None:
    from recorder.market_data_operating_auto_gate import (
        build_market_data_operating_auto_gate_from_paths,
        market_data_operating_auto_gate_markdown,
        write_market_data_operating_auto_gate_json,
        write_market_data_operating_auto_gate_markdown,
    )

    auto_gate_json_path = auto_gate_json_path.expanduser().resolve()
    defensive_mode_json_path = defensive_mode_json_path.expanduser().resolve()
    filters = {
        "auto_gate_json": str(auto_gate_json_path),
        "defensive_mode_json": str(defensive_mode_json_path),
        "sample_limit": sample_limit,
    }
    result = build_market_data_operating_auto_gate_from_paths(
        auto_gate_path=auto_gate_json_path,
        defensive_mode_path=defensive_mode_json_path,
        sample_limit=sample_limit,
    )
    written = write_market_data_operating_auto_gate_markdown(output, result, filters=filters) if output else None
    json_written = write_market_data_operating_auto_gate_json(json_output_path, result) if json_output_path else None
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
    print(_console_safe_text(market_data_operating_auto_gate_markdown(result, filters=filters)))
    if written:
        print(f"\nMarket data operating auto-gate Markdown written: {written}")
    if json_written:
        print(f"Market data operating auto-gate JSON written: {json_written}")
