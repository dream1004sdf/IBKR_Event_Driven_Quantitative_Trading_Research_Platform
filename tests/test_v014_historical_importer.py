from __future__ import annotations

import json
from pathlib import Path

from recorder.analysis_database import AnalysisDatabase
from recorder.historical_importer import HistoricalEventImporter, market_session_for, parse_date
from recorder.sec_edgar import (
    SEC_ARCHIVES_URL,
    SEC_COMPANY_TICKERS_URL,
    SEC_SUBMISSIONS_ARCHIVE_URL,
    SEC_SUBMISSIONS_URL,
    SecEdgarClient,
)


class _Response:
    def __init__(self, body: str, content_type: str) -> None:
        self._body = body.encode("utf-8")
        self.headers = {"Content-Type": content_type}

    def read(self, _size: int) -> bytes:
        return self._body


class _FixtureOpener:
    def __init__(self, values: dict[str, _Response]) -> None:
        self.values = values

    def __call__(self, request, *, timeout: float):
        return self.values[request.full_url]


def _client() -> SecEdgarClient:
    cik = 723125
    accession_recent = "0000723125-26-000013"
    accession_archive = "0000723125-25-000111"
    recent_document = "mu-20260624.htm"
    other_document = "mu-other.htm"
    archive_document = "mu-20250320.htm"
    archive_name = f"CIK{cik:010d}-submissions-001.json"
    recent_url = SEC_ARCHIVES_URL.format(
        cik=str(cik),
        accession=accession_recent.replace("-", ""),
        primary_document=recent_document,
    )
    archive_url = SEC_ARCHIVES_URL.format(
        cik=str(cik),
        accession=accession_archive.replace("-", ""),
        primary_document=archive_document,
    )
    other_url = SEC_ARCHIVES_URL.format(
        cik=str(cik),
        accession="000072312526000014",
        primary_document=other_document,
    )
    opener = _FixtureOpener(
        {
            SEC_COMPANY_TICKERS_URL: _Response(
                json.dumps({"0": {"cik_str": cik, "ticker": "MU", "title": "MICRON TECHNOLOGY INC"}}),
                "application/json",
            ),
            SEC_SUBMISSIONS_URL.format(cik=cik): _Response(
                json.dumps(
                    {
                        "name": "MICRON TECHNOLOGY INC",
                        "filings": {
                            "recent": {
                                "form": ["8-K", "8-K"],
                                "accessionNumber": [accession_recent, "0000723125-26-000014"],
                                "filingDate": ["2026-06-24", "2026-06-09"],
                                "acceptanceDateTime": ["20260624200201", "20260609201347"],
                                "primaryDocument": [recent_document, other_document],
                                "items": ["2.02,7.01", "8.01"],
                                "reportDate": ["2026-05-31", ""],
                            },
                            "files": [{"name": archive_name}],
                        },
                    }
                ),
                "application/json",
            ),
            SEC_SUBMISSIONS_ARCHIVE_URL.format(name=archive_name): _Response(
                json.dumps(
                    {
                        "form": ["8-K"],
                        "accessionNumber": [accession_archive],
                        "filingDate": ["2025-03-20"],
                        "acceptanceDateTime": ["20250320200500"],
                        "primaryDocument": [archive_document],
                        "items": ["2.02"],
                        "reportDate": ["2025-02-28"],
                    }
                ),
                "application/json",
            ),
            recent_url: _Response(
                "<html><body><h1>Financial Results</h1><p>Micron reports financial results and guidance.</p></body></html>",
                "text/html; charset=utf-8",
            ),
            archive_url: _Response(
                "<html><body><h1>Results of Operations and Financial Condition</h1><p>Quarterly results.</p></body></html>",
                "text/html; charset=utf-8",
            ),
            other_url: _Response(
                "<html><body><h1>Corporate update</h1><p>No financial guidance or results.</p></body></html>",
                "text/html; charset=utf-8",
            ),
        }
    )
    return SecEdgarClient(
        user_agent="Test Research test@example.com",
        opener=opener,
        sleeper=lambda _value: None,
        clock=lambda: 10.0,
    )


def test_historical_importer_creates_official_event_rows_idempotently(tmp_path: Path) -> None:
    db = AnalysisDatabase(tmp_path / "historical.db")
    importer = HistoricalEventImporter(db, _client())

    first = importer.import_sec_earnings_guidance(
        symbols=("MU",),
        start=parse_date("2026-01-01"),
        end=parse_date("2026-12-31"),
        event_type="earnings",
        limit_per_symbol=10,
    )
    second = importer.import_sec_earnings_guidance(
        symbols=("MU",),
        start=parse_date("2026-01-01"),
        end=parse_date("2026-12-31"),
        event_type="earnings",
        limit_per_symbol=10,
    )

    assert first["discovered_filings"] == 2
    assert first["historical_events_created"] == 1
    assert first["skipped_non_matching"] == 1
    assert second["historical_events_created"] == 0
    assert second["historical_events_updated"] == 1

    rows = db.recent_historical_events(limit=10)
    assert len(rows) == 1
    row = rows[0]
    assert row["event_type"] == "earnings"
    assert row["event_subtype"] == "quarterly_results_with_guidance"
    assert row["actual_subject_symbol"] == "MU"
    assert row["replay_eligibility"] == "eligible"
    assert row["source_hash"]
    assert row["prediction_cutoff"] == "2026-06-24T20:02:01+00:00"
    assert row["market_session"] == "after_hours"


def test_historical_importer_can_read_sec_archive_files(tmp_path: Path) -> None:
    db = AnalysisDatabase(tmp_path / "historical-archive.db")
    importer = HistoricalEventImporter(db, _client())

    result = importer.import_sec_earnings_guidance(
        symbols=("MU",),
        start=parse_date("2025-01-01"),
        end=parse_date("2025-12-31"),
        event_type="earnings",
        limit_per_symbol=10,
    )

    assert result["historical_events_created"] == 1
    rows = db.recent_historical_events(limit=10)
    assert rows[0]["available_at"] == "2025-03-20T20:05:00+00:00"


def test_market_session_for_us_equity_hours() -> None:
    assert market_session_for("2026-06-24T20:02:01+00:00") == "after_hours"
    assert market_session_for("2026-06-24T14:00:00+00:00") == "regular"
    assert market_session_for("2026-06-24T12:00:00+00:00") == "pre_market"
