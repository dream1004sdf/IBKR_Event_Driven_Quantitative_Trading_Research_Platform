from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from recorder.analysis_database import AnalysisDatabase
from recorder import research_backfill_runner as runner
from recorder.news_lineage_v2 import upsert_raw_news_article


def _write_config(path: Path, output_dir: Path, database_path: Path | None = None) -> None:
    run = {
        "run_id": "test_canonical_backfill",
        "build_id": "test_canonical_backfill",
        "timezone": "America/New_York",
        "start_local": "2025-01-01T00:00:00",
        "end_local": "2026-07-06T00:00:00",
        "resume": True,
        "output_dir": str(output_dir),
    }
    if database_path is not None:
        run["database_path"] = str(database_path)
    path.write_text(
        json.dumps(
            {
                "run": run,
                "versions": {},
                "reaction_labels": {
                    "windows": ["5m"],
                    "decision_lags_seconds": [0],
                },
            }
        ),
        encoding="utf-8",
    )


def _make_test_context(config_path: Path, output_dir: Path, database_path: Path, config: dict | None = None) -> runner.BackfillContext:
    payload = config or {
        "run": {
            "run_id": "test_canonical_backfill",
            "build_id": "test_canonical_backfill",
        }
    }
    return runner.BackfillContext(
        run_id=str(payload.get("run", {}).get("run_id", "test_canonical_backfill")),
        build_id=str(payload.get("run", {}).get("build_id", "test_canonical_backfill")),
        config_path=config_path,
        config=payload,
        config_hash="test-config-hash",
        database_path=database_path,
        start_utc=runner._parse_local_datetime("2025-01-01T00:00:00", "America/New_York"),
        end_utc=runner._parse_local_datetime("2026-07-06T00:00:00", "America/New_York"),
        timezone_name="America/New_York",
        resume=True,
        force_stage=False,
        allow_blind=False,
        dry_run=False,
        output_dir=output_dir,
    )


def _insert_news_article(db: AnalysisDatabase, *, provider: str, article_id: str, symbol: str, published_at: str) -> int:
    headline = f"{symbol} test headline {article_id}"
    normalized = headline.lower()
    with db.connect() as conn:
        cursor = conn.execute(
            """INSERT INTO news_articles(
                provider,article_id,headline,normalized_headline,headline_hash,body,
                published_at,received_at,provider_published_at,first_seen_at,
                timestamp_quality,timestamp_source,primary_symbol,extra_data,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                provider,
                article_id,
                headline,
                normalized,
                hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
                "",
                published_at,
                published_at,
                published_at,
                published_at,
                "provider_published",
                "provider_published_at",
                symbol,
                "{}",
                published_at,
            ),
        )
        return int(cursor.lastrowid)


def test_external_signal_conversion_work_items_skip_completed_rows_on_resume(tmp_path: Path) -> None:
    db_path = tmp_path / "conversion-work.db"
    db = AnalysisDatabase(db_path)
    done_id = _insert_news_article(
        db,
        provider="BZ",
        article_id="bz-done",
        symbol="AAPL",
        published_at="2025-01-02T14:30:00+00:00",
    )
    _insert_news_article(
        db,
        provider="BZ",
        article_id="bz-pending",
        symbol="MSFT",
        published_at="2025-01-02T15:00:00+00:00",
    )
    with db.connect() as conn:
        conn.execute(
            """INSERT INTO external_market_signals(
                source,external_id,title,url,published_at,region,theme,confidence,
                assets_json,asset_map_reason,market_reaction_status,market_reaction_json,
                prediction_gate_status,prediction_gate_reason,raw_json,ingestion_version,
                created_at,updated_at,source_news_id
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "benzinga",
                f"bz:{done_id}",
                "already converted",
                "",
                "2025-01-02T14:30:00+00:00",
                "unknown",
                "unknown",
                0.5,
                "[]",
                "",
                "pending",
                "{}",
                "blocked",
                "",
                "{}",
                "test",
                "2025-01-02T14:30:00+00:00",
                "2025-01-02T14:30:00+00:00",
                123,
            ),
        )
    ctx = _make_test_context(tmp_path / "canonical.yaml", tmp_path / "artifacts", db_path)

    items = runner._build_conversion_work_items(
        ctx,
        source="benzinga",
        label="benzinga_external_signals_build",
        start_date="2025-01-01",
        end_date="2025-01-03",
        symbols="AAPL,MSFT",
        skip_completed=True,
        max_slice_days=45,
        target_rows_per_shard=750,
    )

    assert [(item.symbol, item.estimated_rows) for item in items] == [("MSFT", 1)]


def test_external_signal_conversion_args_keep_full_quality_flags() -> None:
    item = runner.ExternalSignalConversionWorkItem(
        command_name="benzinga-external-signals-build",
        label="benzinga_external_signals_build",
        source="benzinga",
        external_id_prefix="bz:",
        symbol="NVDA",
        start_date="2025-01-01",
        end_date="2025-02-14",
        estimated_rows=100,
    )

    args = runner._conversion_work_item_args(
        item,
        conversion_limit=100000,
        include_non_geopolitical=True,
    )

    assert args[:7] == [
        "benzinga-external-signals-build",
        "--start",
        "2025-01-01",
        "--end",
        "2025-02-14",
        "--symbols",
        "NVDA",
    ]
    assert "--limit" in args
    assert args[args.index("--limit") + 1] == "100000"
    assert "--include-non-geopolitical" in args


def test_command_timeout_zero_disables_runner_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _make_test_context(tmp_path / "canonical.yaml", tmp_path / "artifacts", tmp_path / "runner.db")
    ctx.config["runtime"] = {"timeouts_seconds": {"example_stage": 0}}
    captured: dict[str, object] = {}

    def fake_run(*args, **kwargs):
        captured["timeout"] = kwargs.get("timeout")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    assert runner._command_timeout(ctx, "example_stage", 7200) == 0
    payload = runner.run_main_command(
        ctx,
        "S03_m251a_news_raw_backfill",
        "example_stage",
        ["report", "--limit", "1"],
        timeout_seconds=0,
    )

    assert captured["timeout"] is None
    assert payload["timeout_enabled"] is False


def test_default_ibkr_news_providers_include_dow_jones_codes() -> None:
    providers = runner._configured_ibkr_news_providers({})

    assert providers == "BZ,DJ-N,DJ-RT,DJ-RTA,DJ-RTE,DJ-RTG,DJNL"


def test_ibkr_news_backfill_timeout_summary_is_fatal() -> None:
    payload = {
        "stdout_tail": json.dumps(
            {
                "requested_symbols": 46,
                "completed_symbols": 46,
                "provider_codes": ["BZ", "DJNL"],
                "timed_out": True,
            }
        )
    }

    with pytest.raises(RuntimeError, match="timed out"):
        runner._assert_ibkr_news_backfill_complete(payload)


def test_marketaux_work_items_retry_raw_only_and_skip_completed_daily_shards(tmp_path: Path) -> None:
    db_path = tmp_path / "marketaux-work.db"
    db = AnalysisDatabase(db_path)
    ctx = _make_test_context(tmp_path / "canonical.yaml", tmp_path / "artifacts", db_path)
    with db.connect() as conn:
        upsert_raw_news_article(
            conn,
            source_system="marketaux",
            provider="marketaux",
            publisher="fixture",
            provider_article_id="marketaux-2025-01-02",
            headline="Marketaux fixture",
            body=None,
            url="",
            published_at_utc="2025-01-02T12:00:00Z",
            updated_at_utc=None,
            received_at_utc="2025-01-02T12:01:00Z",
            available_at_utc="2025-01-02T12:00:00Z",
            availability_quality="provider_published",
            ingest_mode="manual_backfill",
            route_type="external_api",
            primary_route_symbol="",
            provider_primary_symbol="",
            raw_json={"uuid": "marketaux-2025-01-02"},
        )
        linked_raw_id = upsert_raw_news_article(
            conn,
            source_system="marketaux",
            provider="marketaux",
            publisher="fixture",
            provider_article_id="marketaux-2025-01-04",
            headline="Marketaux fixture with linked signal",
            body=None,
            url="",
            published_at_utc="2025-01-04T12:00:00Z",
            updated_at_utc=None,
            received_at_utc="2025-01-04T12:01:00Z",
            available_at_utc="2025-01-04T12:00:00Z",
            availability_quality="provider_published",
            ingest_mode="manual_backfill",
            route_type="external_api",
            primary_route_symbol="",
            provider_primary_symbol="",
            raw_json={"uuid": "marketaux-2025-01-04"},
        )
        conn.execute(
            """INSERT INTO external_market_signals(
                source,external_id,title,url,published_at,region,theme,
                sentiment_score,sentiment_label,confidence,assets_json,
                asset_map_reason,market_reaction_status,market_reaction_json,
                prediction_gate_status,prediction_gate_reason,raw_json,
                ingestion_version,created_at,updated_at,source_news_id
            ) VALUES (
                'marketaux','marketaux:2025-01-04','Marketaux linked signal','',
                '2025-01-04T12:00:00Z','global','market','0.1','neutral',
                0.5,'[]','','pending','{}','blocked','','{}',
                'test', '2025-01-04T12:00:01Z', '2025-01-04T12:00:01Z', ?
            )""",
            (linked_raw_id,),
        )
    runner._mark_stage_slice(
        ctx,
        runner._slice_tracking_stage_name("S03_m251a_news_raw_backfill", "marketaux_backfill"),
        runner.ExternalSignalConversionWorkItem(
            command_name="external-market-backfill",
            label="marketaux_backfill",
            source="marketaux",
            external_id_prefix="",
            symbol="MARKETAUX",
            start_date="2025-01-03",
            end_date="2025-01-03",
            estimated_rows=1,
        ),
        status="completed",
        output_count=0,
    )

    items = runner._build_marketaux_work_items(
        ctx,
        label="marketaux_backfill",
        start_date="2025-01-01",
        end_date="2025-01-04",
        skip_completed=True,
        marketaux_mode="broad",
    )

    route_count = len(runner._marketaux_route_work_specs("broad"))
    assert [item.start_date for item in items] == (["2025-01-01"] * route_count) + (["2025-01-02"] * route_count)
    assert all("#" in item.slice_key for item in items)
    assert {item.symbol for item in items if item.start_date == "2025-01-01"} == {
        symbol for symbol, _mode, _query_name in runner._marketaux_route_work_specs("broad")
    }


def test_marketaux_work_item_args_use_single_day_window(tmp_path: Path) -> None:
    ctx = _make_test_context(tmp_path / "canonical.yaml", tmp_path / "artifacts", tmp_path / "marketaux-args.db")
    item = runner.ExternalSignalConversionWorkItem(
        command_name="external-market-backfill",
        label="marketaux_backfill",
        source="marketaux",
        external_id_prefix="",
        symbol="MARKETAUX_QUERY_global_cyber",
        start_date="2025-01-01",
        end_date="2025-01-01",
        estimated_rows=1,
        slice_key="2025-01-01#MARKETAUX_QUERY_global_cyber",
    )

    args = runner._marketaux_work_item_args(
        item,
        symbols="SPY,QQQ",
        marketaux_cfg={"mode": "hybrid", "pause_seconds": 0.75},
        marketaux_params={
            "entity_types": "equity,index,etf",
            "countries": "us",
            "group_similar": True,
            "must_have_entities": True,
        },
        ctx=ctx,
    )

    assert args[:7] == [
        "external-market-backfill",
        "--source",
        "marketaux",
        "--start",
        "2025-01-01",
        "--end",
        "2025-01-01",
    ]
    assert args[args.index("--window-days") + 1] == "1"
    assert args[args.index("--marketaux-symbols") + 1] == "SPY,QQQ"
    assert args[args.index("--marketaux-mode") + 1] == "query"
    assert args[args.index("--marketaux-query-name") + 1] == "global_cyber"


def test_marketaux_work_items_can_use_broad_only_mode(tmp_path: Path) -> None:
    ctx = _make_test_context(tmp_path / "canonical.yaml", tmp_path / "artifacts", tmp_path / "marketaux-broad.db")

    items = runner._build_marketaux_work_items(
        ctx,
        label="marketaux_backfill",
        start_date="2025-01-01",
        end_date="2025-01-02",
        skip_completed=True,
        marketaux_mode="broad",
    )

    assert [(item.start_date, item.symbol) for item in items] == [
        ("2025-01-01", "MARKETAUX_BROAD"),
        ("2025-01-02", "MARKETAUX_BROAD"),
    ]


def test_research_backfill_preflight_creates_run_control(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "research-backfill.db"
    config_path = tmp_path / "canonical.yaml"
    output_dir = tmp_path / "artifacts"
    _write_config(config_path, output_dir)
    monkeypatch.setenv("DATABASE_PATH", str(db_path))

    exit_code = runner.main(["--config", str(config_path), "--stage", "preflight", "--resume"])

    assert exit_code == 0
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        run = conn.execute("SELECT * FROM research_backfill_runs WHERE run_id='test_canonical_backfill'").fetchone()
        stage = conn.execute(
            "SELECT * FROM research_backfill_stage_runs WHERE run_id='test_canonical_backfill' AND stage_name='S00_preflight'"
        ).fetchone()
        artifact = conn.execute(
            "SELECT * FROM research_build_artifacts WHERE run_id='test_canonical_backfill' AND artifact_type='preflight_report'"
        ).fetchone()

    assert run["status"] == "running"
    assert run["start_utc"] == "2025-01-01T05:00:00+00:00"
    assert run["end_utc"] == "2026-07-06T04:00:00+00:00"
    assert stage["status"] == "completed"
    assert artifact["path"].endswith("test_canonical_backfill_S00_preflight.json")


def test_blind_stage_is_locked_without_allow_blind(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "research-backfill.db"
    config_path = tmp_path / "canonical.yaml"
    output_dir = tmp_path / "artifacts"
    _write_config(config_path, output_dir)
    monkeypatch.setenv("DATABASE_PATH", str(db_path))

    exit_code = runner.main(["--config", str(config_path), "--stage", "m256_blind_cycle", "--resume"])

    assert exit_code == 0
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        stage = conn.execute(
            "SELECT status,warning_count,payload_json FROM research_backfill_stage_runs WHERE stage_name='S20_m256_blind_cycle'"
        ).fetchone()

    assert stage["status"] == "skipped"
    assert stage["warning_count"] == 1
    assert "skipped_locked_blind_stage" in stage["payload_json"]


def test_db_audit_error_counts_treats_critical_findings_as_failure() -> None:
    payload = {
        "integrity_check": "ok",
        "severity_counts": {
            "critical": 2,
            "error": 0,
            "warning": 0,
        },
    }

    counts = runner.db_audit_error_counts(json.dumps(payload), returncode=0)

    assert counts == {"critical": 2, "error": 0, "total": 2}


def test_config_database_path_overrides_environment_database(tmp_path: Path, monkeypatch) -> None:
    env_db = tmp_path / "live-market-events.db"
    build_db = tmp_path / "canonical" / "canonical-market-events.db"
    config_path = tmp_path / "canonical.yaml"
    _write_config(config_path, tmp_path / "artifacts", database_path=build_db)
    monkeypatch.setenv("DATABASE_PATH", str(env_db))

    exit_code = runner.main(["--config", str(config_path), "--stage", "preflight", "--resume"])

    assert exit_code == 0
    assert build_db.exists()
    assert not env_db.exists()


def test_news_stage_fails_on_empty_source_backfill(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "canonical.db"
    config_path = tmp_path / "canonical.yaml"
    output_dir = tmp_path / "artifacts"
    _write_config(config_path, output_dir, database_path=db_path)
    config, config_hash = runner._read_config(config_path)
    config["run"]["require_non_empty_sources"] = True
    config["sources"] = {
        "news": {
            "marketaux": {"enabled": False},
            "ibkr": {"enabled": False},
            "external_signal_conversion": {"enabled": False},
        }
    }
    ctx = runner.BackfillContext(
        run_id="test_empty_sources",
        build_id="test_empty_sources",
        config_path=config_path,
        config=config,
        config_hash=config_hash,
        database_path=db_path,
        start_utc=runner._parse_local_datetime("2025-01-01T00:00:00", "America/New_York"),
        end_utc=runner._parse_local_datetime("2026-07-06T00:00:00", "America/New_York"),
        timezone_name="America/New_York",
        resume=True,
        force_stage=False,
        allow_blind=False,
        dry_run=False,
        output_dir=output_dir / "test_empty_sources",
    )
    runner.register_or_resume_run(ctx)

    with pytest.raises(RuntimeError, match="zero news/source rows"):
        runner.stage_m251a_news_raw_backfill(ctx)
