from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from .calibration import CalibrationEngine
from .config import EvaluationConfig, parse_as_of
from .db import EvaluationDatabase, utc_iso
from .bundle import create_analysis_bundle
from recorder.pipeline_diagnostics import PipelineDiagnostics
from recorder.db_audit import audit_database
from .evaluation import PredictionEvaluationEngine
from .replay import HistoricalReplayEngine

UTC = timezone.utc


def _parse_boundary(value: str, *, end: bool = False) -> datetime:
    date_only = len(value.strip()) == 10
    parsed = parse_as_of(value)
    if end and date_only:
        parsed += timedelta(days=1)
    return parsed


def add_subcommands(sub: argparse._SubParsersAction) -> None:
    sub.add_parser(
        "evaluation-migrate",
        help="Create the evaluation, calibration, and historical replay tables",
    )

    evaluation_run = sub.add_parser(
        "evaluation-run",
        help="Evaluate eligible live and replay predictions against completed reactions",
    )
    evaluation_run.add_argument("--full", action="store_true", help="Reprocess every prediction")
    evaluation_run.add_argument("--as-of", help="Evaluation cutoff in ISO-8601 format")
    evaluation_run.add_argument(
        "--no-replay", action="store_true", help="Evaluate live recorder predictions only"
    )

    evaluation_report = sub.add_parser(
        "evaluation-report", help="Show prediction performance and exclusion reasons"
    )
    evaluation_report.add_argument("--event-type")
    evaluation_report.add_argument("--horizon")
    evaluation_report.add_argument("--model")
    evaluation_report.add_argument("--symbol")
    evaluation_report.add_argument("--source")
    evaluation_report.add_argument("--confidence")
    evaluation_report.add_argument("--show-exclusions", action="store_true")

    calibration_fit = sub.add_parser(
        "calibration-fit", help="Fit time-split probability calibration profiles"
    )
    calibration_fit.add_argument(
        "--method",
        default="auto",
        choices=("auto", "identity", "bin-shrinkage", "isotonic", "platt", "beta"),
    )
    calibration_fit.add_argument("--training-fraction", type=float, default=0.80)

    calibration_report = sub.add_parser(
        "calibration-report", help="Show raw and calibrated probability performance"
    )
    calibration_report.add_argument("--version")
    calibration_report.add_argument("--event-type")
    calibration_report.add_argument("--horizon")

    replay_run = sub.add_parser(
        "replay-run", help="Replay historical events from immutable point-in-time snapshots"
    )
    replay_run.add_argument("--start", required=True)
    replay_run.add_argument("--end", required=True)
    replay_run.add_argument(
        "--event-type",
        action="append",
        dest="event_types",
        help="Repeat for multiple event types; defaults to earnings and guidance",
    )
    replay_run.add_argument("--limit", type=int)
    strict_group = replay_run.add_mutually_exclusive_group()
    strict_group.add_argument("--strict", dest="strict", action="store_true", default=True)
    strict_group.add_argument(
        "--no-strict", dest="strict", action="store_false",
        help="Allow review-required replay jobs to run; hard leakage failures still stop the job",
    )
    replay_run.add_argument(
        "--plan-only",
        action="store_true",
        help="Create and audit replay jobs without calling the AI provider",
    )

    replay_audit = sub.add_parser(
        "replay-audit", help="Audit replay jobs for point-in-time data leakage"
    )
    replay_audit.add_argument("--strict", action="store_true")

    sub.add_parser("replay-report", help="Show historical replay queue and output status")

    pipeline_report = sub.add_parser(
        "pipeline-report",
        help="Explain each pipeline stage and why prediction generation is empty or blocked",
    )
    pipeline_report.add_argument("--limit", type=int, default=20)
    pipeline_report.add_argument("--no-backfill", action="store_true")

    sub.add_parser(
        "pipeline-audit-backfill",
        help="Backfill transparent prediction-gate audit rows for existing analyses",
    )

    sub.add_parser(
        "official-readiness-report",
        help="Show official-document coverage required for causal prediction and replay",
    )

    replay_eligibility = sub.add_parser(
        "replay-eligibility-report",
        help="Show which stored events can be safely seeded into historical replay",
    )
    replay_eligibility.add_argument("--start")
    replay_eligibility.add_argument("--end")
    replay_eligibility.add_argument("--event-type", action="append", dest="event_types")
    replay_eligibility.add_argument("--limit", type=int, default=50)

    replay_seed = sub.add_parser(
        "replay-seed",
        help="Create and leakage-audit replay jobs without calling the AI provider",
    )
    replay_seed.add_argument("--start", required=True)
    replay_seed.add_argument("--end", required=True)
    replay_seed.add_argument("--event-type", action="append", dest="event_types")
    replay_seed.add_argument("--limit", type=int)
    replay_seed.add_argument("--no-strict", dest="strict", action="store_false", default=True)

    sub.add_parser(
        "system-report",
        help="Produce a consolidated recorder, pipeline, evaluation, calibration, replay, and DB report",
    )

    analysis_bundle = sub.add_parser(
        "analysis-bundle",
        help="Create a safe ZIP containing a consistent DB snapshot and all diagnostic reports",
    )
    analysis_bundle.add_argument("--output")
    analysis_bundle.add_argument("--include-logs", action="store_true")


def handle_command(args: argparse.Namespace, settings: Any) -> bool:
    command = args.command
    supported = {
        "evaluation-migrate",
        "evaluation-run",
        "evaluation-report",
        "calibration-fit",
        "calibration-report",
        "replay-run",
        "replay-audit",
        "replay-report",
        "pipeline-report",
        "pipeline-audit-backfill",
        "official-readiness-report",
        "replay-eligibility-report",
        "replay-seed",
        "system-report",
        "analysis-bundle",
    }
    if command not in supported:
        return False

    config = EvaluationConfig.from_env(settings.database_path)
    diagnostics = PipelineDiagnostics(
        config.database_path,
        official_required_event_types=getattr(
            settings, "prediction_official_required_event_types", ()
        ),
    )
    if command == "evaluation-migrate":
        applied = EvaluationDatabase(config.database_path).migrate()
        print(json.dumps({"migration": "applied" if applied else "already_applied", "database": str(config.database_path)}, indent=2))
        return True

    if command == "evaluation-run":
        result = PredictionEvaluationEngine(config).run(
            full=bool(args.full),
            include_replay=not bool(args.no_replay),
            evaluation_as_of=parse_as_of(args.as_of),
        )
        print(json.dumps(result.as_dict(), indent=2, ensure_ascii=False))
        return True

    if command == "evaluation-report":
        result = PredictionEvaluationEngine(config).report(
            event_type=args.event_type,
            horizon=args.horizon,
            model=args.model,
            symbol=args.symbol,
            source=args.source,
            confidence=args.confidence,
            show_exclusions=bool(args.show_exclusions),
        )
        if int(result.get("outcome_count") or 0) == 0:
            result["zero_outcome_diagnosis"] = diagnostics.report(recent_limit=10)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return True

    if command == "calibration-fit":
        if not 0.5 <= args.training_fraction < 1.0:
            raise SystemExit("--training-fraction must be >= 0.5 and < 1.0")
        result = CalibrationEngine(config).fit(
            method=args.method,
            training_fraction=args.training_fraction,
        )
        print(json.dumps(result.as_dict(), indent=2, ensure_ascii=False))
        return True

    if command == "calibration-report":
        result = CalibrationEngine(config).report(
            version=args.version,
            event_type=args.event_type,
            horizon=args.horizon,
        )
        if int(result.get("profile_count") or 0) == 0:
            result["zero_profile_explanation"] = (
                "No calibration profile can be fitted until evaluated, probability-eligible outcomes exist. "
                "Use pipeline-report to inspect the upstream bottleneck."
            )
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return True

    replay = HistoricalReplayEngine(settings, config)
    if command == "replay-run":
        result = replay.run(
            start=_parse_boundary(args.start),
            end=_parse_boundary(args.end, end=True),
            event_types=tuple(args.event_types or ("earnings", "guidance")),
            limit=args.limit,
            strict=bool(args.strict),
            plan_only=bool(args.plan_only),
        )
        print(json.dumps(result.as_dict(), indent=2, ensure_ascii=False))
        return True

    if command == "replay-audit":
        result = replay.audit(strict=bool(args.strict))
        print(json.dumps(result, indent=2, ensure_ascii=False))
        if args.strict and (
            result["status_counts"].get("FAIL", 0)
            or result["status_counts"].get("REVIEW_REQUIRED", 0)
        ):
            raise SystemExit(2)
        return True

    if command == "replay-report":
        result = replay.report()
        if not result.get("jobs_by_status"):
            result["zero_job_explanation"] = diagnostics.replay_eligibility_report(
                event_types=("earnings", "guidance"), recent_limit=20
            )
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return True

    if command == "pipeline-report":
        print(json.dumps(
            diagnostics.report(
                recent_limit=args.limit,
                backfill=not bool(args.no_backfill),
            ),
            indent=2, ensure_ascii=False,
        ))
        return True

    if command == "pipeline-audit-backfill":
        print(json.dumps(diagnostics.backfill(), indent=2, ensure_ascii=False))
        return True

    if command == "official-readiness-report":
        print(json.dumps(diagnostics.official_readiness_report(), indent=2, ensure_ascii=False))
        return True

    if command == "replay-eligibility-report":
        start = utc_iso(_parse_boundary(args.start)) if args.start else None
        end = utc_iso(_parse_boundary(args.end, end=True)) if args.end else None
        print(json.dumps(
            diagnostics.replay_eligibility_report(
                start=start,
                end=end,
                event_types=tuple(args.event_types or ("earnings", "guidance")),
                recent_limit=args.limit,
            ),
            indent=2, ensure_ascii=False,
        ))
        return True

    if command == "replay-seed":
        result = replay.run(
            start=_parse_boundary(args.start),
            end=_parse_boundary(args.end, end=True),
            event_types=tuple(args.event_types or ("earnings", "guidance")),
            limit=args.limit,
            strict=bool(args.strict),
            plan_only=True,
        )
        payload = result.as_dict()
        payload["queue"] = replay.report()
        if payload["planned"] == 0:
            payload["zero_plan_explanation"] = diagnostics.replay_eligibility_report(
                start=utc_iso(_parse_boundary(args.start)),
                end=utc_iso(_parse_boundary(args.end, end=True)),
                event_types=tuple(args.event_types or ("earnings", "guidance")),
                recent_limit=50,
            )
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return True

    if command == "system-report":
        payload = {
            "pipeline": diagnostics.report(recent_limit=50),
            "official_readiness": diagnostics.official_readiness_report(),
            "replay_eligibility": diagnostics.replay_eligibility_report(recent_limit=100),
            "evaluation": PredictionEvaluationEngine(config).report(show_exclusions=True),
            "calibration": CalibrationEngine(config).report(),
            "replay": replay.report(),
            "replay_audit": replay.audit(strict=True),
            "database_audit": audit_database(config.database_path),
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return True

    if command == "analysis-bundle":
        path = create_analysis_bundle(
            settings=settings,
            config=config,
            output=args.output,
            include_logs=bool(args.include_logs),
        )
        print(json.dumps({"bundle": str(path)}, indent=2, ensure_ascii=False))
        return True

    return False
