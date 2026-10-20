from __future__ import annotations

import sqlite3
import json
from pathlib import Path

from recorder.clean_backfill_cycle import (
    build_clean_backfill_cycle_audit,
    build_clean_backfill_cycle_report,
    effective_cycle_mode,
)


def _make_cycle_db(path: Path) -> None:
    con = sqlite3.connect(path)
    try:
        con.executescript(
            """
            CREATE TABLE news_articles (
                id INTEGER PRIMARY KEY,
                provider TEXT,
                article_id TEXT,
                headline TEXT,
                published_at TEXT,
                primary_symbol TEXT
            );
            CREATE TABLE news_symbols (
                news_id INTEGER,
                symbol TEXT,
                con_id INTEGER,
                first_seen_at TEXT
            );
            CREATE TABLE market_reactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                target_id INTEGER,
                news_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                horizon TEXT NOT NULL,
                due_at TEXT,
                captured_at TEXT,
                baseline_price REAL,
                reaction_price REAL,
                raw_return REAL,
                price_source TEXT,
                created_at TEXT,
                usage_status TEXT,
                usage_reason TEXT,
                governance_analysis_id INTEGER,
                calibration_eligible INTEGER,
                observation_eligible INTEGER
            );
            CREATE TABLE external_market_signals (
                id INTEGER PRIMARY KEY,
                source TEXT,
                external_id TEXT,
                title TEXT,
                published_at TEXT,
                region TEXT,
                theme TEXT,
                created_at TEXT,
                updated_at TEXT
            );
            """
        )
        con.execute(
            """
            INSERT INTO news_articles(id, provider, article_id, headline, published_at, primary_symbol)
            VALUES (1, 'BZ', 'a1', 'Unknown theme company surges on new demand', '2025-01-02T15:00:00+00:00', 'ZZZZ')
            """
        )
        con.execute(
            "INSERT INTO news_symbols(news_id, symbol, first_seen_at) VALUES (1, 'ZZZZ', '2025-01-02T15:00:00+00:00')"
        )
        con.executemany(
            """
            INSERT INTO external_market_signals(id, source, external_id, title, published_at, region, theme, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (1, "benzinga", "b1", "Benzinga source row", "2025-01-02T15:00:00+00:00", "US", "macro", "2026-07-01T00:00:00+00:00", "2026-07-01T00:00:00+00:00"),
                (2, "marketaux", "m1", "Marketaux source row", "2025-01-03T12:00:00+00:00", "US", "macro", "2026-07-01T00:00:00+00:00", "2026-07-01T00:00:00+00:00"),
            ],
        )
        con.commit()
    finally:
        con.close()


def test_effective_cycle_mode_forces_blind_after_first_cycle() -> None:
    assert effective_cycle_mode(1, "auto") == "codex-assisted"
    assert effective_cycle_mode(2, "auto") == "blind"
    assert effective_cycle_mode(2, "codex-assisted") == "blind"
    assert effective_cycle_mode(1, "blind") == "blind"
    assert effective_cycle_mode(4, "source-locked-blind-reclass") == "source-locked-blind-reclass"


def test_cycle_audit_allows_codex_only_on_first_cycle(tmp_path: Path) -> None:
    db = tmp_path / "market_events.db"
    ledger = tmp_path / "cycle_ledger.jsonl"
    _make_cycle_db(db)

    first = build_clean_backfill_cycle_audit(
        db,
        start="2025-01-01",
        end="2025-01-03",
        cycle_index=1,
        cycle_mode="auto",
        correction_ledger_path=ledger,
    )
    second = build_clean_backfill_cycle_audit(
        db,
        start="2025-01-01",
        end="2025-01-03",
        cycle_index=2,
        cycle_mode="codex-assisted",
        correction_ledger_path=ledger,
    )

    assert first["cycle_mode"] == "codex-assisted"
    assert first["codex_intervention_allowed"] is True
    assert any(item["suggested_action"] != "record_only" for item in first["correction_candidates"])
    assert second["cycle_mode"] == "blind"
    assert second["codex_intervention_allowed"] is False
    assert all(
        item["suggested_action"] in {"record_only", "record_for_post_cycle_review"}
        for item in second["correction_candidates"]
    )
    assert ledger.exists()
    assert len(ledger.read_text(encoding="utf-8").strip().splitlines()) == 2


def test_cycle_report_keeps_repeating_when_readiness_is_insufficient(tmp_path: Path) -> None:
    report = build_clean_backfill_cycle_report(
        start="2025-01-01",
        end="2025-01-03",
        cycle_index=4,
        cycle_mode="codex-assisted",
        artifact_dir=tmp_path,
    )

    decision = report["cycle_stop_decision"]
    assert report["cycle_mode"] == "blind"
    assert decision["status"] == "continue_blind_cycles"
    assert decision["should_stop_training_cycles"] is False
    assert decision["next_action"] == "run_next_blind_cycle_on_same_fixed_dataset"
    assert decision["targets"]["no_fixed_cycle_cap"] is True


def test_source_locked_blind_reclass_is_record_only_blind(tmp_path: Path) -> None:
    db = tmp_path / "market_events.db"
    _make_cycle_db(db)

    audit = build_clean_backfill_cycle_audit(
        db,
        start="2025-01-01",
        end="2025-01-03",
        cycle_index=4,
        cycle_mode="source-locked-blind-reclass",
    )
    report = build_clean_backfill_cycle_report(
        start="2025-01-01",
        end="2025-01-03",
        cycle_index=4,
        cycle_mode="source-locked-blind-reclass",
        artifact_dir=tmp_path,
    )

    assert audit["cycle_mode"] == "source-locked-blind-reclass"
    assert audit["blind_mode"] is True
    assert audit["codex_intervention_allowed"] is False
    assert audit["operating_rule"] == "record_only_no_mid_cycle_codex_intervention"
    assert report["cycle_mode"] == "source-locked-blind-reclass"
    assert report["next_cycle_recommended_mode"] == "source-locked-blind-reclass"


def test_cycle_audit_records_provider_coverage_depth(tmp_path: Path) -> None:
    db = tmp_path / "market_events.db"
    _make_cycle_db(db)

    audit = build_clean_backfill_cycle_audit(
        db,
        start="2025-01-01",
        end="2025-01-03",
        cycle_index=4,
        cycle_mode="source-locked-blind-reclass",
    )

    source_counts = audit["source_counts"]
    assert source_counts["external_market_signals_by_source"]["marketaux"] == 1
    assert source_counts["external_market_signals_by_source"]["benzinga"] == 1
    assert any(
        note["category"] == "source_coverage_thin_vs_benchmark"
        and note["subject"] == "dow_jones_news_articles"
        for note in source_counts["source_coverage_notes"]
    )
    assert any(item["category"] == "source_coverage_thin_vs_benchmark" for item in audit["anomalies"])


def test_cycle_report_can_stop_when_full_auto_candidate_gates_pass(tmp_path: Path) -> None:
    artifacts = {
        "historical_backfill_walk_forward_threshold_tune.json": {"selected": {"tradeable_accuracy": 0.62}},
        "historical_backfill_walk_forward_nuanced_train.json": {
            "cycle_results": [{"clean_directional": {"directional_accuracy": 0.66}}]
        },
        "historical_backfill_strategy_walk_forward_tune.json": {
            "result": {
                "folds": [
                    {
                        "test_summary": {
                            "total_realized_pnl": 125.0,
                            "profit_factor": 1.32,
                            "max_drawdown": 0.08,
                        }
                    }
                ]
            }
        },
        "historical_backfill_prediction_trend.json": {
            "comparisons": [{"direction_accuracy_delta": 0.0}]
        },
    }
    for filename, payload in artifacts.items():
        (tmp_path / filename).write_text(json.dumps(payload), encoding="utf-8")

    report = build_clean_backfill_cycle_report(
        start="2025-01-01",
        end="2025-01-03",
        cycle_index=5,
        cycle_mode="auto",
        artifact_dir=tmp_path,
    )

    decision = report["cycle_stop_decision"]
    assert report["cycle_mode"] == "blind"
    assert decision["status"] == "full_auto_candidate_ready"
    assert decision["should_stop_training_cycles"] is True
    assert decision["next_action"] == "paper_and_live_read_only_validation_before_limited_auto"
