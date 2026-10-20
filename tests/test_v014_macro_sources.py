from __future__ import annotations

from pathlib import Path

from recorder.analysis_database import AnalysisDatabase
from recorder.macro_sources import (
    BEA_RSS_URL,
    BLS_CPI_RELEASE_URL,
    BLS_EMPLOYMENT_RELEASE_URL,
    FED_MONETARY_RSS_URL,
    MacroOfficialClient,
    MacroOfficialCollector,
    parse_rss_items,
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


def _client() -> MacroOfficialClient:
    fed_link = "https://www.federalreserve.gov/newsevents/pressreleases/monetary20260617a.htm"
    bea_link = "https://www.bea.gov/news/2026/personal-income-and-outlays-may-2026"
    opener = _FixtureOpener(
        {
            FED_MONETARY_RSS_URL: _Response(
                f"""<?xml version="1.0"?><rss><channel>
                <item><title>Federal Reserve issues FOMC statement</title>
                <link>{fed_link}</link><guid>fed-guid-1</guid>
                <pubDate>Wed, 17 Jun 2026 18:00:00 GMT</pubDate>
                <description>FOMC monetary policy statement.</description></item>
                </channel></rss>""",
                "application/rss+xml",
            ),
            fed_link: _Response(
                "<html><body><h1>FOMC statement</h1><p>The Committee decided to maintain the target range for the federal funds rate.</p></body></html>",
                "text/html; charset=utf-8",
            ),
            BEA_RSS_URL: _Response(
                f"""<?xml version="1.0"?><rss><channel>
                <item><title>Personal Income and Outlays, May 2026</title>
                <link>{bea_link}</link><guid>bea-guid-1</guid>
                <pubDate>Fri, 26 Jun 2026 12:30:00 GMT</pubDate>
                <description>Personal consumption expenditures price index release.</description></item>
                </channel></rss>""",
                "application/rss+xml",
            ),
            bea_link: _Response(
                "<html><body><h1>Personal Income and Outlays</h1><p>The PCE price index increased in May.</p></body></html>",
                "text/html; charset=utf-8",
            ),
            BLS_CPI_RELEASE_URL: _Response(
                "<html><body><h1>Consumer Price Index Summary</h1><p>The Consumer Price Index for All Urban Consumers increased.</p></body></html>",
                "text/html; charset=utf-8",
            ),
            BLS_EMPLOYMENT_RELEASE_URL: _Response(
                "<html><body><h1>The Employment Situation</h1><p>Total nonfarm payroll employment changed in May.</p></body></html>",
                "text/html; charset=utf-8",
            ),
        }
    )
    return MacroOfficialClient(
        user_agent="IBKRMarketEventRecorder/0.14 tests@example.com",
        opener=opener,
        sleeper=lambda _value: None,
        clock=lambda: 10.0,
    )


def test_parse_rss_items_keeps_published_time() -> None:
    raw = """<rss><channel><item><title>A</title><link>https://example.com/a</link>
    <guid>g1</guid><pubDate>Wed, 17 Jun 2026 18:00:00 GMT</pubDate></item></channel></rss>"""

    item = parse_rss_items(raw)[0]

    assert item.guid == "g1"
    assert item.published_at == "2026-06-17T18:00:00+00:00"


def test_macro_collector_stores_fed_bls_and_bea_documents(tmp_path: Path) -> None:
    db = AnalysisDatabase(tmp_path / "macro.db")
    collector = MacroOfficialCollector(db, _client())

    result = collector.sync("all", limit=2)
    again = collector.sync("all", limit=2)

    assert result["stored"] == 4
    assert result["errors"] == 0
    assert again["duplicates"] == 4
    assert again["stored"] == 0

    rows = db.recent_official_documents(limit=10)
    event_types = {row["event_type"] for row in rows}
    source_types = {row["source_type"] for row in rows}
    assert event_types == {"fomc", "cpi", "employment", "pce"}
    assert source_types == {"federal_reserve", "bls", "bea"}
    assert all(row["status"] == "fetched" for row in rows)
    assert any(row["available_at"] == "2026-06-17T18:00:00+00:00" for row in rows)


def test_macro_report_can_filter_by_source(tmp_path: Path) -> None:
    db = AnalysisDatabase(tmp_path / "macro-filter.db")
    collector = MacroOfficialCollector(db, _client())
    collector.sync("all", limit=2)

    bls_rows = db.recent_official_documents(limit=10, source_type="bls")

    assert len(bls_rows) == 2
    assert {row["event_type"] for row in bls_rows} == {"cpi", "employment"}
