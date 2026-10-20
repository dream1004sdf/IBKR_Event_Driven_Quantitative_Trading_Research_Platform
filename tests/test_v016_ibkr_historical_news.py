from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from recorder.config import Settings
from recorder.ibkr_historical_news import (
    IBKRHistoricalNewsBackfiller,
    format_ibkr_news_datetime,
    parse_cli_datetime,
    parse_ibkr_news_time,
)

UTC = timezone.utc


def test_ibkr_historical_news_time_helpers_are_utc_safe() -> None:
    parsed = parse_cli_datetime("2026-06-26")
    assert parsed == datetime(2026, 6, 26, 0, 0, tzinfo=UTC)
    assert parse_cli_datetime("2026-06-26", end_of_day=True) == datetime(
        2026, 6, 26, 23, 59, 59, tzinfo=UTC
    )
    assert parse_ibkr_news_time("20260626 20:02:01") == datetime(
        2026, 6, 26, 20, 2, 1, tzinfo=UTC
    )
    assert format_ibkr_news_datetime(datetime(2026, 6, 26, 20, 2, 1, tzinfo=UTC)) == (
        "2026-06-26 20:02:01.0"
    )


def test_ibkr_historical_news_ingest_is_idempotent_and_provider_timestamped(tmp_path: Path) -> None:
    settings = Settings(
        database_path=tmp_path / "historical-news.db",
        watch_symbols=("MU",),
        fetch_article_body=False,
    )
    backfiller = IBKRHistoricalNewsBackfiller(
        settings,
        symbols=("MU",),
        start=datetime(2026, 1, 1, tzinfo=UTC),
        end=datetime(2026, 6, 26, tzinfo=UTC),
        provider_codes=("BRFG",),
    )

    first_id = backfiller._ingest_news(  # noqa: SLF001 - callback helper is tested without IBKR
        "MU",
        12345,
        "BRFG",
        "BRFG$123",
        "Micron reports quarterly results",
        "20260624 20:02:01",
    )
    second_id = backfiller._ingest_news(  # noqa: SLF001
        "MU",
        12345,
        "BRFG",
        "BRFG$123",
        "Micron reports quarterly results",
        "20260624 20:02:01",
    )

    assert first_id == second_id
    assert backfiller._created == 1  # noqa: SLF001
    assert backfiller._duplicates == 1  # noqa: SLF001

    with backfiller.db.connect() as conn:
        rows = conn.execute("SELECT * FROM news_articles").fetchall()
        duplicate_count = conn.execute("SELECT COUNT(*) FROM news_duplicates").fetchone()[0]
    assert len(rows) == 1
    assert duplicate_count == 1
    row = rows[0]
    assert row["provider"] == "BRFG"
    assert row["article_id"] == "BRFG$123"
    assert row["primary_symbol"] == "MU"
    assert row["con_id"] == 12345
    assert row["provider_published_at"] == "2026-06-24T20:02:01+00:00"
    assert row["timestamp_quality"] == "provider_published"
    assert row["timestamp_source"] == "provider_published_at"
    assert row["first_seen_at"] != row["published_at"]
    extra = json.loads(row["extra_data"])
    assert extra["source"] == "ibkr_historical_news"


def test_ibkr_historical_news_skips_provider_rows_outside_requested_window(tmp_path: Path) -> None:
    settings = Settings(
        database_path=tmp_path / "historical-news-window.db",
        watch_symbols=("MU",),
        fetch_article_body=False,
    )
    backfiller = IBKRHistoricalNewsBackfiller(
        settings,
        symbols=("MU",),
        start=datetime(2026, 1, 1, tzinfo=UTC),
        end=datetime(2026, 1, 31, 23, 59, 59, tzinfo=UTC),
        provider_codes=("BZ",),
    )

    skipped_id = backfiller._ingest_news(  # noqa: SLF001
        "MU",
        12345,
        "BZ",
        "BZ$outside",
        "Out-of-window news should not enter storage",
        "20260624 20:02:01",
    )
    kept_id = backfiller._ingest_news(  # noqa: SLF001
        "MU",
        12345,
        "BZ",
        "BZ$inside",
        "In-window news should enter storage",
        "20260124 20:02:01",
    )

    assert skipped_id is None
    assert kept_id is not None
    assert backfiller._candidates == 2  # noqa: SLF001
    assert backfiller._skipped_out_of_range == 1  # noqa: SLF001
    assert backfiller._created == 1  # noqa: SLF001

    with backfiller.db.connect() as conn:
        rows = conn.execute("SELECT article_id FROM news_articles").fetchall()
    assert [row["article_id"] for row in rows] == ["BZ$inside"]


def test_ibkr_historical_news_contract_error_skips_symbol_without_blocking(tmp_path: Path) -> None:
    settings = Settings(
        database_path=tmp_path / "historical-news-contract-error.db",
        watch_symbols=("SPY", "VIX"),
        fetch_article_body=False,
    )
    backfiller = IBKRHistoricalNewsBackfiller(
        settings,
        symbols=("SPY", "VIX"),
        start=datetime(2026, 1, 1, tzinfo=UTC),
        end=datetime(2026, 6, 26, tzinfo=UTC),
        provider_codes=("BZ",),
    )

    backfiller._providers = {"BZ": "Benzinga"}  # noqa: SLF001
    backfiller._providers_received = True  # noqa: SLF001
    backfiller._contract_req_to_symbol[700000] = "SPY"  # noqa: SLF001
    backfiller._contract_req_to_symbol[700001] = "VIX"  # noqa: SLF001
    backfiller._contracts_by_symbol["SPY"] = backfiller._stock_contract("SPY")  # noqa: SLF001
    backfiller._contracts_by_symbol["SPY"].conId = 756733  # noqa: SLF001

    requested: list[str] = []

    def fake_request(symbol: str, provider_code_string: str, anchor: datetime) -> bool:
        requested.append(symbol)
        return True

    backfiller._request_historical_page = fake_request  # type: ignore[method-assign]  # noqa: SLF001

    backfiller.contractDetailsEnd(700000)
    backfiller.error(700001, 200, "No security definition has been found for the request")

    assert requested == ["SPY"]
    assert backfiller._requests_started is True  # noqa: SLF001
    assert any("VIX" in message for message in backfiller._errors)  # noqa: SLF001
