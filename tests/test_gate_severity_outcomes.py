from __future__ import annotations

import sqlite3
from pathlib import Path

from recorder.gate_severity_outcomes import (
    build_gate_severity_outcome_report,
    gate_severity_outcome_markdown,
)


def _seed_market_prices(db_path: Path) -> None:
    con = sqlite3.connect(db_path)
    con.executescript(
        """
        CREATE TABLE market_baselines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            captured_at TEXT NOT NULL,
            selected_price REAL,
            selected_price_source TEXT
        );
        CREATE TABLE market_reactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            captured_at TEXT NOT NULL,
            reaction_price REAL,
            price_source TEXT
        );
        """
    )
    con.execute(
        "INSERT INTO market_baselines(symbol,captured_at,selected_price,selected_price_source) VALUES (?,?,?,?)",
        ("SPY", "2026-07-03T14:00:00+00:00", 500.0, "test_baseline"),
    )
    con.execute(
        "INSERT INTO market_reactions(symbol,captured_at,reaction_price,price_source) VALUES (?,?,?,?)",
        ("SPY", "2026-07-03T14:05:00+00:00", 505.0, "test_reaction"),
    )
    con.commit()
    con.close()


def test_gate_severity_outcomes_records_and_evaluates_m250_to_m242_intent(tmp_path: Path) -> None:
    db_path = tmp_path / "events.db"
    _seed_market_prices(db_path)

    report = build_gate_severity_outcome_report(
        database_path=db_path,
        limited_auto_readiness_payload={
            "limited_auto_trading_readiness_version": "m250-test",
            "generated_at": "2026-07-03T14:00:00+00:00",
            "summary": {
                "gate_execution_posture": "size_reduced",
                "gate_size_multiplier": 0.5,
                "gate_confidence_multiplier": 0.85,
            },
            "contexts": {
                "gate_severity_policy": {
                    "recommended_execution_posture": "size_reduced",
                    "size_multiplier": 0.5,
                    "confidence_multiplier": 0.85,
                    "signals": [{"code": "market_temperature_elevated", "category": "haircut"}],
                }
            },
        },
        paper_capital_execution_payload={
            "ledger": {
                "intents": [
                    {
                        "intent_id": "i1",
                        "symbol": "SPY",
                        "action": "BUY",
                        "state": "pending_paper_execution",
                        "quantity": 1,
                        "final_entry_size_resolution": {"final_size_multiplier": 0.5},
                    }
                ]
            }
        },
        horizons_minutes="5",
        now="2026-07-03T14:05:30+00:00",
    )

    assert report["status"] == "ok"
    assert report["summary"]["recorded_rows_this_run"] == 1
    assert report["summary"]["evaluated_rows_this_run"] == 1
    assert report["summary"]["status_counts"] == {"evaluated": 1}
    assert report["latest_rows"][0]["symbol"] == "SPY"
    assert report["latest_rows"][0]["gate_posture"] == "size_reduced"
    assert round(report["latest_rows"][0]["signed_return"], 4) == 0.01
    assert report["safety"]["broker_orders_submitted"] is False
    assert "M250.5 Gate Severity Outcome Log" in gate_severity_outcome_markdown(report)
