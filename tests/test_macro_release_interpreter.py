from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from freezegun import freeze_time

from recorder.macro_release_interpreter import (
    MacroEventCalendarConfig,
    MacroReleaseInterpreterConfig,
    build_macro_event_calendar_report,
    build_macro_release_interpreter_report,
)
from recorder.market_outcomes import IntradayBar


class _FakePriceSource:
    name = "fake_intraday"

    def __init__(self) -> None:
        self.requests: list[str] = []

    def get_intraday_bars(self, symbol: str, start, end) -> list[IntradayBar]:
        self.requests.append(symbol)
        return [
            IntradayBar(symbol=symbol, timestamp=datetime.fromisoformat("2026-07-02T12:29:00+00:00"), close=100.0),
            IntradayBar(symbol=symbol, timestamp=datetime.fromisoformat("2026-07-02T12:31:00+00:00"), close=100.2),
            IntradayBar(symbol=symbol, timestamp=datetime.fromisoformat("2026-07-02T12:35:00+00:00"), close=101.0),
            IntradayBar(symbol=symbol, timestamp=datetime.fromisoformat("2026-07-02T12:45:00+00:00"), close=100.5),
            IntradayBar(symbol=symbol, timestamp=datetime.fromisoformat("2026-07-02T13:00:00+00:00"), close=99.8),
        ]


def _create_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.execute(
        """
        CREATE TABLE news_articles (
            id INTEGER PRIMARY KEY,
            provider TEXT,
            primary_symbol TEXT,
            headline TEXT,
            published_at TEXT,
            received_at TEXT,
            provider_published_at TEXT,
            extra_data TEXT
        )
        """
    )
    con.execute(
        """
        CREATE TABLE external_market_signals (
            id INTEGER PRIMARY KEY,
            source TEXT,
            title TEXT,
            published_at TEXT,
            sentiment_score REAL,
            sentiment_label TEXT,
            assets_json TEXT
        )
        """
    )
    t = "2026-07-02T12:30:08+00:00"
    rows = [
        (1, "BZ", "BROADTAPE:BZ", "USA Nonfarm Payrolls For June 57K Vs 114K Est.", t, t, t, ""),
        (2, "BZ", "BROADTAPE:BZ", "USA Unemployment Rate For June 4.2% Vs 4.3% Est.", "2026-07-02T12:30:35+00:00", t, t, ""),
        (3, "BZ", "BROADTAPE:BZ", "USA Nonfarm Payrolls For June Revises Prior From 172K To 129K", "2026-07-02T12:31:13+00:00", t, t, ""),
    ]
    con.executemany("INSERT INTO news_articles VALUES (?,?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()


@freeze_time("2026-07-02 13:00:00")
def test_macro_release_interpreter_parses_jobs_surprise_and_market_reaction(tmp_path: Path) -> None:
    db = tmp_path / "events.db"
    _create_db(db)
    payload = build_macro_release_interpreter_report(
        MacroReleaseInterpreterConfig(
            database_path=db,
            lookback_minutes=60 * 24,
            reaction_horizons=("1M", "5M", "15M"),
            reaction_assets=("SPY", "TLT"),
        ),
        price_source=_FakePriceSource(),
    )

    assert payload["status"] == "ok"
    assert payload["summary"]["parsed_indicator_count"] == 3
    latest = payload["clusters"][0]
    assert "growth weak" in latest["macro_surprise_label"]
    assert latest["asset_bias"]["TLT"] == "up"
    assert latest["market_reaction"]["assets"]["SPY"]["horizons"]["5M"]["direction_label"] == "up"
    assert "Nonfarm Payrolls" in payload["latest_event_line"]


@freeze_time("2026-07-02 13:00:00")
def test_macro_event_calendar_contains_future_fomc_and_pce_without_network() -> None:
    payload = build_macro_event_calendar_report(
        MacroEventCalendarConfig(
            horizon_days=180,
            include_internet_refresh=False,
        )
    )

    names = [event["name"] for event in payload["upcoming_events"]]
    assert any("FOMC" in name for name in names)
    assert any("PCE" in name for name in names)
    assert payload["summary"]["critical_event_count"] >= 1
    assert payload["risk_windows"]
