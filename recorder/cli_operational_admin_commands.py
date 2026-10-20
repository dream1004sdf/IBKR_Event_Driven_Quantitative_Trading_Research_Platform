from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def command_artifact_cleanup(
    exports_dir: Path,
    work_dir: Path,
    keep_latest_per_logical_key: int,
    work_max_age_days: int,
    include_work: bool,
    execute: bool,
    output: Path | None,
    json_output_path: Path | None,
    json_output: bool,
) -> None:
    from recorder.artifact_cleanup import (
        artifact_cleanup_markdown,
        build_artifact_cleanup_report,
        write_artifact_cleanup_json,
        write_artifact_cleanup_markdown,
    )

    report = build_artifact_cleanup_report(
        exports_dir=exports_dir,
        work_dir=work_dir,
        keep_latest_per_logical_key=keep_latest_per_logical_key,
        include_work=include_work,
        work_max_age_days=work_max_age_days,
        execute=execute,
    )
    if json_output_path:
        write_artifact_cleanup_json(json_output_path, report)
    if output:
        write_artifact_cleanup_markdown(output, report)
    if json_output:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(artifact_cleanup_markdown(report))


def command_ai_cost_report(
    settings: Any,
    pricing_json: Path | None,
    output: Path | None,
    json_output_path: Path | None,
    json_output: bool,
) -> None:
    from recorder.ai_costs import (
        ai_cost_report_markdown,
        build_ai_cost_report,
        write_ai_cost_report_json,
        write_ai_cost_report_markdown,
    )

    report = build_ai_cost_report(database_path=settings.database_path, pricing_path=pricing_json)
    if json_output_path:
        write_ai_cost_report_json(json_output_path, report)
    if output:
        write_ai_cost_report_markdown(output, report)
    if json_output:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(ai_cost_report_markdown(report))


def command_prediction_trend_report(
    settings: Any,
    window_days: int,
    date_field: str,
    min_rows: int,
    output: Path | None,
    json_output_path: Path | None,
    json_output: bool,
) -> None:
    from recorder.prediction_trends import (
        build_prediction_trend_report,
        prediction_trend_markdown,
        write_prediction_trend_json,
        write_prediction_trend_markdown,
    )

    report = build_prediction_trend_report(
        database_path=settings.database_path,
        window_days=window_days,
        date_field=date_field,
        min_rows=min_rows,
    )
    if json_output_path:
        write_prediction_trend_json(json_output_path, report)
    if output:
        write_prediction_trend_markdown(output, report)
    if json_output:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(prediction_trend_markdown(report))


def command_future_earnings_calendar(
    settings: Any,
    symbols: str | None,
    horizon_days: int,
    default_cycle_days: int,
    output: Path | None,
    json_output_path: Path | None,
    json_output: bool,
) -> None:
    from recorder.earnings_calendar import (
        build_future_earnings_calendar,
        future_earnings_calendar_markdown,
        write_future_earnings_calendar_json,
        write_future_earnings_calendar_markdown,
    )

    parsed_symbols = tuple(item.strip().upper() for item in (symbols or "").split(",") if item.strip())
    report = build_future_earnings_calendar(
        database_path=settings.database_path,
        symbols=parsed_symbols,
        horizon_days=horizon_days,
        default_cycle_days=default_cycle_days,
    )
    if json_output_path:
        write_future_earnings_calendar_json(json_output_path, report)
    if output:
        write_future_earnings_calendar_markdown(output, report)
    if json_output:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(future_earnings_calendar_markdown(report))


def command_singleton_lock_status(
    repo_root: Path | None,
    output: Path | None,
    json_output_path: Path | None,
    json_output: bool,
) -> None:
    from recorder.operational_lock_status import (
        build_operational_lock_status,
        operational_lock_status_markdown,
        write_operational_lock_status_json,
        write_operational_lock_status_markdown,
    )

    report = build_operational_lock_status(repo_root=repo_root or Path.cwd())
    if json_output_path:
        write_operational_lock_status_json(json_output_path, report)
    if output:
        write_operational_lock_status_markdown(output, report)
    if json_output:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(operational_lock_status_markdown(report))


def command_singleton_lock_cleanup(
    repo_root: Path | None,
    min_age_seconds: float,
    execute: bool,
    output: Path | None,
    json_output_path: Path | None,
    json_output: bool,
) -> None:
    from recorder.operational_lock_status import (
        build_operational_lock_cleanup_report,
        operational_lock_cleanup_markdown,
        write_operational_lock_cleanup_json,
        write_operational_lock_cleanup_markdown,
    )

    report = build_operational_lock_cleanup_report(
        repo_root=repo_root or Path.cwd(),
        min_age_seconds=min_age_seconds,
        execute=execute,
    )
    if json_output_path:
        write_operational_lock_cleanup_json(json_output_path, report)
    if output:
        write_operational_lock_cleanup_markdown(output, report)
    if json_output:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(operational_lock_cleanup_markdown(report))


def command_code_health_report(
    repo_root: Path | None,
    top_n: int,
    large_file_threshold: int,
    long_function_threshold: int,
    output: Path | None,
    json_output_path: Path | None,
    json_output: bool,
) -> None:
    from recorder.code_health import (
        build_code_health_report,
        code_health_markdown,
        write_code_health_json,
        write_code_health_markdown,
    )

    report = build_code_health_report(
        repo_root=repo_root or Path.cwd(),
        top_n=top_n,
        large_file_threshold=large_file_threshold,
        long_function_threshold=long_function_threshold,
    )
    if json_output_path:
        write_code_health_json(json_output_path, report)
    if output:
        write_code_health_markdown(output, report)
    if json_output:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(code_health_markdown(report))


def add_operational_admin_commands(sub) -> None:
    artifact_cleanup = sub.add_parser("artifact-cleanup", help="Dry-run or execute cleanup for generated exports/work artifacts")
    artifact_cleanup.add_argument("--exports-dir", type=Path, default=Path("exports"))
    artifact_cleanup.add_argument("--work-dir", type=Path, default=Path("work"))
    artifact_cleanup.add_argument("--keep-latest-per-logical-key", type=int, default=5)
    artifact_cleanup.add_argument("--work-max-age-days", type=int, default=14)
    artifact_cleanup.add_argument("--no-work", action="store_true", help="Do not include work/ cleanup candidates")
    artifact_cleanup.add_argument("--execute", action="store_true", help="Actually delete candidates; default is dry-run")
    artifact_cleanup.add_argument("--output", type=Path, default=Path("exports/artifact_cleanup_latest.md"))
    artifact_cleanup.add_argument("--json-output-path", type=Path, default=Path("exports/artifact_cleanup_latest.json"))
    artifact_cleanup.add_argument("--json", action="store_true", help="Print machine-readable cleanup report JSON")

    ai_cost_report = sub.add_parser("ai-cost-report", help="Aggregate stored AI token usage and optional configured cost estimates")
    ai_cost_report.add_argument("--pricing-json", type=Path, default=Path("config/ai_cost_pricing_v1.json"))
    ai_cost_report.add_argument("--output", type=Path, default=Path("exports/ai_cost_report_latest.md"))
    ai_cost_report.add_argument("--json-output-path", type=Path, default=Path("exports/ai_cost_report_latest.json"))
    ai_cost_report.add_argument("--json", action="store_true", help="Print machine-readable AI cost report JSON")

    prediction_trend = sub.add_parser("prediction-trend-report", help="Compare recent vs previous replay-score hit-rate trends")
    prediction_trend.add_argument("--window-days", type=int, default=14)
    prediction_trend.add_argument(
        "--date-field",
        default="updated_at",
        choices=("updated_at", "created_at", "replay_timestamp", "prediction_cutoff_at"),
    )
    prediction_trend.add_argument("--min-rows", type=int, default=1)
    prediction_trend.add_argument("--output", type=Path, default=Path("exports/prediction_trend_report_latest.md"))
    prediction_trend.add_argument("--json-output-path", type=Path, default=Path("exports/prediction_trend_report_latest.json"))
    prediction_trend.add_argument("--json", action="store_true", help="Print machine-readable prediction trend JSON")

    future_earnings = sub.add_parser("future-earnings-calendar", help="Project upcoming earnings windows from stored official historical events")
    future_earnings.add_argument("--symbols", help="Comma-separated symbols; defaults to all symbols with stored historical earnings")
    future_earnings.add_argument("--horizon-days", type=int, default=120)
    future_earnings.add_argument("--default-cycle-days", type=int, default=91)
    future_earnings.add_argument("--output", type=Path, default=Path("exports/future_earnings_calendar_latest.md"))
    future_earnings.add_argument("--json-output-path", type=Path, default=Path("exports/future_earnings_calendar_latest.json"))
    future_earnings.add_argument("--json", action="store_true", help="Print machine-readable future earnings calendar JSON")

    singleton_status = sub.add_parser("singleton-lock-status", help="Report process/lock/artifact truth status for long-running loops")
    singleton_status.add_argument("--repo-root", type=Path, default=Path("."))
    singleton_status.add_argument("--output", type=Path, default=Path("exports/singleton_lock_status_latest.md"))
    singleton_status.add_argument("--json-output-path", type=Path, default=Path("exports/singleton_lock_status_latest.json"))
    singleton_status.add_argument("--json", action="store_true", help="Print machine-readable singleton lock status JSON")

    singleton_cleanup = sub.add_parser("singleton-lock-cleanup", help="Dry-run or remove stale singleton locks whose PID is no longer running")
    singleton_cleanup.add_argument("--repo-root", type=Path, default=Path("."))
    singleton_cleanup.add_argument("--min-age-seconds", type=float, default=30.0)
    singleton_cleanup.add_argument("--execute", action="store_true", help="Actually remove stale lock files; default is dry-run")
    singleton_cleanup.add_argument("--output", type=Path, default=Path("exports/singleton_lock_cleanup_latest.md"))
    singleton_cleanup.add_argument("--json-output-path", type=Path, default=Path("exports/singleton_lock_cleanup_latest.json"))
    singleton_cleanup.add_argument("--json", action="store_true", help="Print machine-readable singleton lock cleanup JSON")

    code_health = sub.add_parser("code-health-report", help="Report largest files/functions and middle-group refactor pressure")
    code_health.add_argument("--repo-root", type=Path, default=Path("."))
    code_health.add_argument("--top-n", type=int, default=25)
    code_health.add_argument("--large-file-threshold", type=int, default=1500)
    code_health.add_argument("--long-function-threshold", type=int, default=200)
    code_health.add_argument("--output", type=Path, default=Path("exports/code_health_report_latest.md"))
    code_health.add_argument("--json-output-path", type=Path, default=Path("exports/code_health_report_latest.json"))
    code_health.add_argument("--json", action="store_true", help="Print machine-readable code health JSON")
