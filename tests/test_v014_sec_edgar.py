from __future__ import annotations

import json
from pathlib import Path

from recorder.analysis_database import AnalysisDatabase
from recorder.sec_edgar import (
    SEC_ARCHIVES_URL,
    SEC_COMPANY_TICKERS_URL,
    SEC_SUBMISSIONS_URL,
    SecEdgarClient,
    SecEdgarCollector,
    classify_sec_filing,
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
        self.requests = []

    def __call__(self, request, *, timeout: float):
        self.requests.append((request.full_url, request.headers, timeout))
        return self.values[request.full_url]


def _client() -> tuple[SecEdgarClient, _FixtureOpener]:
    cik = 723125
    accession = "0000723125-26-000001"
    document = "mu-20260625x8k.htm"
    submissions_url = SEC_SUBMISSIONS_URL.format(cik=cik)
    archive_url = SEC_ARCHIVES_URL.format(
        cik=str(cik),
        accession=accession.replace("-", ""),
        primary_document=document,
    )
    exhibit_url = SEC_ARCHIVES_URL.format(
        cik=str(cik),
        accession=accession.replace("-", ""),
        primary_document="exhibit991.htm",
    )
    opener = _FixtureOpener(
        {
            SEC_COMPANY_TICKERS_URL: _Response(
                json.dumps(
                    {
                        "0": {
                            "cik_str": cik,
                            "ticker": "MU",
                            "title": "MICRON TECHNOLOGY INC",
                        }
                    }
                ),
                "application/json",
            ),
            submissions_url: _Response(
                json.dumps(
                    {
                        "name": "MICRON TECHNOLOGY INC",
                        "filings": {
                            "recent": {
                                "form": ["8-K", "10-Q"],
                                "accessionNumber": [accession, "0000723125-26-000002"],
                                "filingDate": ["2026-06-25", "2026-06-20"],
                                "acceptanceDateTime": ["20260625160500", "20260620160500"],
                                "primaryDocument": [document, "mu-10q.htm"],
                                "items": ["2.02,7.01", ""],
                                "reportDate": ["2026-05-31", "2026-05-31"],
                            }
                        },
                    }
                ),
                "application/json",
            ),
            archive_url: _Response(
                """<html><body><h1>Financial Results</h1>
                <a href="exhibit991.htm">Exhibit 99.1</a>
                <p>The company reported quarterly financial results and issued guidance
                for the next quarter.</p></body></html>""",
                "text/html; charset=utf-8",
            ),
            exhibit_url: _Response(
                """<html><body><h1>Exhibit 99.1</h1>
                <p>Micron beats expectations and raises outlook on stronger demand.</p>
                </body></html>""",
                "text/html; charset=utf-8",
            ),
        }
    )
    client = SecEdgarClient(
        user_agent="Test Research test@example.com",
        opener=opener,
        sleeper=lambda _value: None,
        clock=lambda: 10.0,
    )
    return client, opener


def test_sec_client_discovers_and_classifies_8k_from_fixtures() -> None:
    client, opener = _client()

    filings = client.discover_filings("MU", forms=("8-K",), limit=5)

    assert len(filings) == 1
    filing = filings[0]
    assert filing.accession_number == "0000723125-26-000001"
    assert filing.accepted_at == "2026-06-25T16:05:00+00:00"
    assert filing.document_url.endswith("/mu-20260625x8k.htm")

    payload = client.fetch_primary_document(filing)
    classification = classify_sec_filing(filing, payload.content)

    assert classification.event_type == "earnings_guidance"
    assert classification.materiality_classification == "material_earnings_guidance"
    assert "quarterly financial results" in payload.content.lower()
    assert "micron beats expectations" in payload.content.lower()
    assert "[sec exhibit:" in payload.content.lower()
    assert len(payload.content_hash) == 64
    assert all(
        any(key.lower() == "user-agent" for key in headers)
        for _, headers, _ in opener.requests
    )


def test_sec_collector_persists_primary_evidence_idempotently(tmp_path: Path) -> None:
    client, _ = _client()
    db = AnalysisDatabase(tmp_path / "sec.db")
    collector = SecEdgarCollector(db, client)

    first = collector.sync_symbols(("MU",), forms=("8-K",), limit_per_symbol=1)
    second = collector.sync_symbols(("MU",), forms=("8-K",), limit_per_symbol=1)

    assert first["discovered"] == 1
    assert first["fetched"] == 1
    assert first["errors"] == 0
    assert second["duplicates"] == 1
    assert second["fetched"] == 0

    rows = db.recent_sec_documents()
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "fetched"
    assert row["actual_subject_symbol"] == "MU"
    assert row["form_type"] == "8-K"
    assert row["event_type"] == "earnings_guidance"
    assert row["materiality_classification"] == "material_earnings_guidance"
    assert row["accepted_at"] == "2026-06-25T16:05:00+00:00"
    assert row["available_at"] == row["accepted_at"]


def test_sec_client_requires_a_contact_user_agent() -> None:
    try:
        SecEdgarClient(user_agent="anonymous-client")
    except ValueError as exc:
        assert "SEC_EDGAR_USER_AGENT" in str(exc)
    else:
        raise AssertionError("SEC client accepted an anonymous User-Agent")
