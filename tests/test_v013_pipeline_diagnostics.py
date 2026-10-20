from __future__ import annotations

import sqlite3

from recorder.pipeline_diagnostics import (
    derive_gate_decisions,
    ensure_gate_audit_schema,
    write_gate_audit,
)


def _primary(decisions):
    return next(item for item in decisions if item.is_primary_failure)


def test_market_price_update_has_specific_primary_reason():
    decisions = derive_gate_decisions(
        {
            "event_type": "market_price_update",
            "causal_signal": 0,
            "prediction_eligible": 0,
            "attribution_status": "matched",
            "subject_target_available": 1,
            "target_quote_integrity_status": "valid",
            "text_integrity_status": "valid",
            "official_source_status": "not_available",
            "source_tier": "reputable_secondary",
        },
        raw={"event_stage": "retrospective"},
        prediction_count=0,
    )
    assert _primary(decisions).reason_code == "retrospective_market_price_update"


def test_routine_filing_materiality_is_explained():
    decisions = derive_gate_decisions(
        {
            "event_type": "ownership",
            "causal_signal": 0,
            "prediction_eligible": 0,
            "attribution_status": "matched",
            "subject_target_available": 1,
            "target_quote_integrity_status": "valid",
            "text_integrity_status": "valid",
            "official_source_status": "not_available",
            "source_tier": "reputable_secondary",
        },
        raw={"event_stage": "initial_release", "interpretation_flags": ["routine_filing_no_materiality"]},
        prediction_count=0,
    )
    assert _primary(decisions).reason_code == "routine_filing_no_materiality"


def test_official_requirement_is_a_diagnostic_gate():
    decisions = derive_gate_decisions(
        {
            "event_type": "earnings",
            "causal_signal": 1,
            "prediction_eligible": 0,
            "attribution_status": "matched",
            "subject_target_available": 1,
            "target_quote_integrity_status": "valid",
            "text_integrity_status": "valid",
            "official_source_status": "not_available",
            "source_tier": "reputable_secondary",
        },
        raw={"event_stage": "initial_release"},
        prediction_count=0,
        official_required_event_types=("earnings",),
    )
    assert _primary(decisions).reason_code == "official_evidence_missing"


def test_gate_audit_write_does_not_commit_callers_transaction():
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(
        """
        CREATE TABLE news_articles(id INTEGER PRIMARY KEY);
        CREATE TABLE news_analyses(id INTEGER PRIMARY KEY, news_id INTEGER NOT NULL);
        """
    )
    ensure_gate_audit_schema(conn)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("INSERT INTO news_articles(id) VALUES (1)")
    conn.execute("INSERT INTO news_analyses(id,news_id) VALUES (1,1)")
    decisions = derive_gate_decisions(
        {
            "event_type": "market_price_update",
            "causal_signal": 0,
            "prediction_eligible": 0,
            "attribution_status": "matched",
            "subject_target_available": 1,
            "target_quote_integrity_status": "valid",
            "text_integrity_status": "valid",
        },
        raw={"event_stage": "retrospective"},
        prediction_count=0,
    )
    write_gate_audit(conn, analysis_id=1, news_id=1, decisions=decisions)
    conn.execute("ROLLBACK")
    assert conn.execute("SELECT COUNT(*) FROM news_articles").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM prediction_gate_audit").fetchone()[0] == 0
