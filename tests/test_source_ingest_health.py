from __future__ import annotations

import json
from pathlib import Path

from recorder.source_operations import (
    SourceIngestHealthConfig,
    build_source_ingest_health_report,
    run_source_ingest_health_loop,
    source_ingest_health_markdown,
)


def _write_json(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def test_source_ingest_health_classifies_online_required_gap_after_successful_refresh(tmp_path: Path) -> None:
    freshness = {
        "overall_status": "watch",
        "sources": [
            {
                "source_key": "market_news",
                "label": "Market / breaking news",
                "status": "lagging",
                "latest_event_at": "2026-06-30T08:00:00+00:00",
                "latest_ingested_at": "2026-06-30T08:05:00+00:00",
                "row_count": 100,
            },
            {
                "source_key": "market_data",
                "label": "Market Data Desk",
                "status": "fresh",
                "latest_event_at": "2026-06-30T09:59:00+00:00",
                "latest_ingested_at": "2026-06-30T09:59:00+00:00",
                "row_count": 20,
            },
            {
                "source_key": "geopolitical_signals",
                "label": "Geopolitical / external signals",
                "status": "fresh",
                "latest_event_at": "2026-06-30T09:00:00+00:00",
                "latest_ingested_at": "2026-06-30T09:01:00+00:00",
                "row_count": 50,
            },
            {
                "source_key": "standing_regimes",
                "label": "Standing market regimes",
                "status": "fresh",
                "latest_event_at": "2026-06-30T09:00:00+00:00",
                "latest_ingested_at": "2026-06-30T09:01:00+00:00",
                "row_count": 10,
            },
            {
                "source_key": "economic_indicators",
                "label": "Economic Indicator Desk",
                "status": "fresh",
                "latest_event_at": "2026-06-30T00:00:00+00:00",
                "latest_ingested_at": "2026-06-30T00:01:00+00:00",
                "row_count": 20,
            },
            {
                "source_key": "technical_indicators",
                "label": "Technical indicators",
                "status": "fresh",
                "latest_event_at": "2026-06-30",
                "latest_ingested_at": "2026-06-30T00:01:00+00:00",
                "row_count": 200,
            },
        ],
    }
    queue = {
        "status_counts": {"succeeded": 1},
        "jobs": [
            {
                "job_id": "ibkr_news_gap_backfill",
                "source_keys": ["market_news"],
                "status": "succeeded",
            }
        ],
    }
    watchdog = {
        "final_status": "refresh_completed_gap_remains",
        "final_lagging_source_keys": ["market_news"],
        "final_stale_source_keys": [],
    }
    config = SourceIngestHealthConfig(
        freshness_report_path=tmp_path / "freshness.json",
        refresh_queue_path=tmp_path / "queue.json",
        watchdog_path=tmp_path / "watchdog.json",
    )

    report = build_source_ingest_health_report(
        freshness_report=freshness,
        refresh_queue=queue,
        watchdog=watchdog,
        config=config,
        now="2026-06-30T10:00:00+00:00",
    )
    rows = {row["source_key"]: row for row in report["sources"]}
    markdown = source_ingest_health_markdown(report)

    assert report["overall_status"] == "watch"
    assert "market_news" in report["online_problem_source_keys"]
    assert rows["market_news"]["root_cause"] == "provider_returned_no_newer_rows_or_query_window_gap"
    assert rows["market_news"]["recommended_action"] == "expand_backfill_window_or_validate_provider_returns"
    assert rows["market_data"]["health_status"] == "healthy"
    assert "Data Source Doctor" not in markdown
    assert "Source Ingest Health" in markdown


def test_source_ingest_health_uses_live_validation_to_separate_provider_from_ingest_lag(tmp_path: Path) -> None:
    freshness = {
        "overall_status": "watch",
        "sources": [
            {
                "source_key": "market_news",
                "label": "Market / breaking news",
                "status": "lagging",
                "latest_event_at": "2026-06-30T08:00:00+00:00",
                "latest_ingested_at": "2026-06-30T08:05:00+00:00",
                "row_count": 100,
            },
            {
                "source_key": "geopolitical_signals",
                "label": "Geopolitical / external signals",
                "status": "lagging",
                "latest_event_at": "2026-06-30T07:00:00+00:00",
                "latest_ingested_at": "2026-06-30T07:01:00+00:00",
                "row_count": 50,
            },
        ],
    }
    live_validation = {
        "overall_status": "yellow",
        "source_status": {
            "market_news": "ok",
            "geopolitical_signals": "degraded",
        },
        "providers": [
            {
                "provider": "ibkr_historical_news",
                "source_key": "market_news",
                "status": "ok",
                "row_count": 50,
                "latest_event_at": "2026-06-30T09:55:00+00:00",
            },
            {
                "provider": "gdelt",
                "source_key": "geopolitical_signals",
                "status": "error",
                "row_count": 0,
            },
        ],
    }
    config = SourceIngestHealthConfig(
        freshness_report_path=tmp_path / "freshness.json",
        refresh_queue_path=tmp_path / "queue.json",
        watchdog_path=tmp_path / "watchdog.json",
        live_validation_path=tmp_path / "live.json",
    )

    report = build_source_ingest_health_report(
        freshness_report=freshness,
        refresh_queue={"status_counts": {}, "jobs": []},
        watchdog={},
        live_validation=live_validation,
        config=config,
        now="2026-06-30T10:00:00+00:00",
    )
    rows = {row["source_key"]: row for row in report["sources"]}

    assert report["live_validation_status"] == "yellow"
    assert rows["market_news"]["root_cause"] == "provider_ok_ingest_or_freshness_pipeline_lag"
    assert rows["market_news"]["recommended_action"] == "run_ingest_catchup_then_rebuild_freshness"
    assert rows["market_news"]["details"]["ok_provider_count"] == 1
    assert rows["geopolitical_signals"]["root_cause"] == "provider_live_validation_problem"
    assert rows["geopolitical_signals"]["recommended_action"] == "inspect_provider_specific_live_validation_report"


def test_source_ingest_health_treats_marketaux_crosschecked_market_news_as_healthy(tmp_path: Path) -> None:
    freshness = {
        "overall_status": "watch",
        "sources": [
            {
                "source_key": "market_news",
                "label": "Market / breaking news",
                "status": "lagging",
                "latest_event_at": "2026-06-30T08:30:00+00:00",
                "latest_ingested_at": "2026-06-30T08:31:00+00:00",
                "row_count": 100,
                "details": {
                    "marketaux_auxiliary_freshness": {
                        "applied": True,
                        "latest_event_at": "2026-06-30T09:35:00+00:00",
                        "age_minutes": 25.0,
                    }
                },
            },
            {
                "source_key": "market_data",
                "label": "Market Data Desk",
                "status": "fresh",
                "latest_event_at": "2026-06-30T09:59:00+00:00",
                "latest_ingested_at": "2026-06-30T09:59:00+00:00",
                "row_count": 20,
            },
            {
                "source_key": "geopolitical_signals",
                "label": "Geopolitical / external signals",
                "status": "fresh",
                "latest_event_at": "2026-06-30T09:35:00+00:00",
                "latest_ingested_at": "2026-06-30T09:36:00+00:00",
                "row_count": 50,
            },
        ],
    }
    config = SourceIngestHealthConfig(
        freshness_report_path=tmp_path / "freshness.json",
        refresh_queue_path=tmp_path / "queue.json",
        watchdog_path=tmp_path / "watchdog.json",
    )

    report = build_source_ingest_health_report(
        freshness_report=freshness,
        refresh_queue={"status_counts": {}, "jobs": []},
        watchdog={},
        config=config,
        now="2026-06-30T10:00:00+00:00",
    )
    rows = {row["source_key"]: row for row in report["sources"]}

    assert rows["market_news"]["health_status"] == "healthy"
    assert rows["market_news"]["root_cause"] == "market_news_lagging_but_marketaux_crosscheck_fresh"
    assert rows["market_news"]["recommended_action"] == "continue_broker_news_catchup_keep_marketaux_crosscheck"
    assert "market_news" not in report["online_problem_source_keys"]


def test_source_ingest_health_splits_quiet_geopolitical_gap_from_blocking_online_gap(tmp_path: Path) -> None:
    freshness = {
        "overall_status": "watch",
        "sources": [
            {
                "source_key": "market_data",
                "label": "Market Data Desk",
                "status": "fresh",
                "latest_event_at": "2026-06-30T09:59:00+00:00",
                "latest_ingested_at": "2026-06-30T09:59:00+00:00",
                "row_count": 20,
            },
            {
                "source_key": "market_news",
                "label": "Market / breaking news",
                "status": "fresh",
                "latest_event_at": "2026-06-30T09:58:00+00:00",
                "latest_ingested_at": "2026-06-30T09:58:00+00:00",
                "row_count": 100,
            },
            {
                "source_key": "geopolitical_signals",
                "label": "Geopolitical / external signals",
                "status": "lagging",
                "latest_event_at": "2026-06-30T07:00:00+00:00",
                "latest_ingested_at": "2026-06-30T09:55:00+00:00",
                "row_count": 50,
            },
        ],
    }
    queue = {
        "status_counts": {"succeeded": 1},
        "jobs": [
            {
                "job_id": "geo_quiet_gap",
                "source_keys": ["geopolitical_signals"],
                "status": "succeeded",
            }
        ],
    }

    report = build_source_ingest_health_report(
        freshness_report=freshness,
        refresh_queue=queue,
        watchdog={},
        config=SourceIngestHealthConfig(
            freshness_report_path=tmp_path / "freshness.json",
            refresh_queue_path=tmp_path / "queue.json",
            watchdog_path=tmp_path / "watchdog.json",
            batch_sources=(),
        ),
        now="2026-06-30T10:00:00+00:00",
    )

    assert report["overall_status"] == "watch"
    assert report["online_problem_source_keys"] == []
    assert report["monitoring_only_online_source_keys"] == ["geopolitical_signals"]
    assert report["online_gap_source_keys"] == ["geopolitical_signals"]


def test_source_ingest_health_treats_geopolitical_provider_ok_ingest_lag_as_monitoring_only(tmp_path: Path) -> None:
    report = build_source_ingest_health_report(
        freshness_report={
            "overall_status": "watch",
            "sources": [
                {
                    "source_key": "geopolitical_signals",
                    "status": "lagging",
                    "latest_event_at": "2026-06-30T07:00:00+00:00",
                    "latest_ingested_at": "2026-06-30T09:58:00+00:00",
                    "row_count": 50,
                }
            ],
        },
        refresh_queue={"status_counts": {}, "jobs": []},
        watchdog={},
        live_validation={
            "overall_status": "yellow",
            "source_status": {"geopolitical_signals": "ok"},
            "providers": [
                {
                    "provider": "marketaux",
                    "source_key": "geopolitical_signals",
                    "status": "ok",
                    "row_count": 10,
                    "latest_event_at": "2026-06-30T09:55:00+00:00",
                }
            ],
        },
        config=SourceIngestHealthConfig(
            freshness_report_path=tmp_path / "freshness.json",
            refresh_queue_path=tmp_path / "queue.json",
            watchdog_path=tmp_path / "watchdog.json",
            batch_sources=(),
            online_required_sources=("geopolitical_signals",),
        ),
        now="2026-06-30T10:00:00+00:00",
    )

    rows = {row["source_key"]: row for row in report["sources"]}
    assert rows["geopolitical_signals"]["root_cause"] == "provider_ok_ingest_or_freshness_pipeline_lag"
    assert report["online_problem_source_keys"] == []
    assert report["monitoring_only_online_source_keys"] == ["geopolitical_signals"]


def test_source_ingest_health_loop_writes_latest_report_and_tick_log(tmp_path: Path) -> None:
    freshness_path = _write_json(
        tmp_path / "freshness.json",
        {
            "overall_status": "watch",
            "sources": [
                {
                    "source_key": "geopolitical_signals",
                    "label": "Geopolitical / external signals",
                    "status": "lagging",
                    "latest_event_at": "2026-06-30T08:00:00+00:00",
                    "latest_ingested_at": "2026-06-30T08:02:00+00:00",
                    "row_count": 50,
                }
            ],
        },
    )
    queue_path = _write_json(tmp_path / "queue.json", {"status_counts": {}, "jobs": []})
    watchdog_path = _write_json(tmp_path / "watchdog.json", {"status": "skipped_cooldown", "last_lagging_source_keys": ["geopolitical_signals"]})
    output = tmp_path / "health.md"
    json_output = tmp_path / "health.json"
    tick_log = tmp_path / "ticks.jsonl"

    result = run_source_ingest_health_loop(
        config=SourceIngestHealthConfig(
            freshness_report_path=freshness_path,
            refresh_queue_path=queue_path,
            watchdog_path=watchdog_path,
            online_required_sources=("geopolitical_signals",),
            batch_sources=(),
            max_ticks=1,
        ),
        output_path=output,
        json_output_path=json_output,
        tick_log_jsonl_path=tick_log,
        run_id="test_source_health_loop",
    )

    written_report = json.loads(json_output.read_text(encoding="utf-8"))
    assert result["executed_tick_count"] == 1
    assert result["final_status"] == "watch"
    assert output.exists()
    assert json_output.exists()
    assert len(tick_log.read_text(encoding="utf-8").strip().splitlines()) == 1
    assert written_report["sources"][0]["root_cause"] == "watchdog_cooldown_gap_remains"
