from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from recorder.signal_classification_audit import build_signal_classification_audit


def _make_signal_db(path: Path) -> None:
    con = sqlite3.connect(path)
    try:
        con.executescript(
            """
            CREATE TABLE external_market_signals (
                id INTEGER PRIMARY KEY,
                source TEXT,
                title TEXT,
                published_at TEXT,
                region TEXT,
                theme TEXT,
                sentiment_score REAL,
                sentiment_label TEXT,
                confidence REAL,
                assets_json TEXT,
                asset_map_reason TEXT
            );
            """
        )
        rows = [
            (
                1,
                "marketaux",
                "Is SpaceX Stock a Buy Before Its First Earnings Report as a Public Company?",
                "2026-07-04T20:25:00+00:00",
                "global",
                "semiconductor_supply_chain",
                -0.1,
                "neutral",
                0.4,
                json.dumps(["SOXX", "SMH", "QQQ"]),
                "test",
            ),
            (
                2,
                "dow_jones",
                "Press Release: Advisory: Chevron Corporation's 2Q 2026 Earnings Conference Call and Webcast",
                "2026-07-04T23:00:00+00:00",
                "global",
                "market_news",
                None,
                "unknown",
                0.5,
                json.dumps(["CVX", "XLE", "SPY"]),
                "test",
            ),
            (
                3,
                "benzinga",
                "June Jobs Report Shows Payroll Miss As Unemployment Rate Rises",
                "2026-07-03T13:30:00+00:00",
                "global",
                "market_news",
                None,
                "unknown",
                0.5,
                json.dumps(["SPY", "QQQ", "TLT"]),
                "test",
            ),
            (
                4,
                "emergency_breaking_news",
                "June Jobs Report Shows Payroll Miss As Unemployment Rate Rises",
                "2026-07-03T13:30:00+00:00",
                "global",
                "market_news",
                None,
                "unknown",
                0.5,
                json.dumps(["SPY", "QQQ", "TLT"]),
                "test",
            ),
        ]
        con.executemany(
            """
            INSERT INTO external_market_signals(
                id, source, title, published_at, region, theme, sentiment_score,
                sentiment_label, confidence, assets_json, asset_map_reason
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        con.commit()
    finally:
        con.close()


def test_signal_classification_audit_ignores_opinion_earnings_preview(tmp_path: Path) -> None:
    db = tmp_path / "market_events.db"
    _make_signal_db(db)

    report = build_signal_classification_audit(db, start="2026-07-01", end="2026-07-05", limit=100)

    titles = [note["title"] for note in report["notes"]]
    assert "Is SpaceX Stock a Buy Before Its First Earnings Report as a Public Company?" not in titles
    assert any(note["category"] == "earnings_theme_mismatch" for note in report["notes"])


def test_signal_classification_audit_dedupes_same_title_category(tmp_path: Path) -> None:
    db = tmp_path / "market_events.db"
    _make_signal_db(db)

    report = build_signal_classification_audit(db, start="2026-07-01", end="2026-07-05", limit=100)

    jobs_notes = [
        note
        for note in report["notes"]
        if note["category"] == "macro_theme_mismatch"
        and note["title"] == "June Jobs Report Shows Payroll Miss As Unemployment Rate Rises"
    ]
    assert len(jobs_notes) == 1
    assert jobs_notes[0]["duplicate_count"] == 2
    assert set(jobs_notes[0]["duplicate_sources"]) == {"benzinga", "emergency_breaking_news"}


def test_signal_classification_audit_reconciles_missing_ticker_notes_with_symbol_tiers(tmp_path: Path) -> None:
    db = tmp_path / "market_events.db"
    _make_signal_db(db)
    con = sqlite3.connect(db)
    try:
        con.execute(
            """
            INSERT INTO external_market_signals(
                id, source, title, published_at, region, theme, sentiment_score,
                sentiment_label, confidence, assets_json, asset_map_reason
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                5,
                "marketaux",
                "General Mills, Inc. (GIS) Q4 2026 Earnings Call Transcript",
                "2026-07-04T23:10:00+00:00",
                "global",
                "market_news",
                0.2,
                "positive",
                0.5,
                json.dumps(["SPY", "QQQ", "TLT", "GLD", "VIX"]),
                "test",
            ),
        )
        con.execute(
            """
            INSERT INTO external_market_signals(
                id, source, title, published_at, region, theme, sentiment_score,
                sentiment_label, confidence, assets_json, asset_map_reason
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                6,
                "benzinga",
                "Benzinga Market Summary: S&P Rises As Yields Drop After Decline In Payrolls, Pure Storage Sinks On Earnings Report",
                "2026-07-04T23:11:00+00:00",
                "global",
                "market_news",
                0.2,
                "positive",
                0.5,
                json.dumps(["SPY", "QQQ", "TLT", "GLD", "VIX"]),
                "test",
            ),
        )
        con.commit()
    finally:
        con.close()
    tiers = tmp_path / "tiers.json"
    tiers.write_text(json.dumps({"symbols": [{"symbol": "SPY"}, {"symbol": "QQQ"}]}), encoding="utf-8")

    report = build_signal_classification_audit(
        db,
        start="2026-07-01",
        end="2026-07-05",
        limit=100,
        symbol_tier_report_path=tiers,
    )

    reconciliation = report["tier_reconciliation"]
    assert reconciliation["kept_missing_ticker_count"] == 1
    assert reconciliation["kept_missing_tickers"][0]["missing_tickers"] == ["GIS"]
    assert reconciliation["suppressed_note_count"] == 1
    assert reconciliation["suppressed_notes"][0]["tier_reconciliation_status"] == "suppressed_broad_aggregate_summary"
