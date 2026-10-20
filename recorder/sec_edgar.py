from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from typing import Any, Callable, Iterable

from .database import parse_utc, utc_iso
from .official_sources import html_to_plain_text

UTC = timezone.utc
SEC_COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
SEC_SUBMISSIONS_ARCHIVE_URL = "https://data.sec.gov/submissions/{name}"
SEC_ARCHIVES_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{primary_document}"
SEC_INGESTION_VERSION = "sec-edgar-v1"
SUPPORTED_FORMS = ("8-K", "10-Q", "10-K", "6-K")
EXHIBIT_LINK_PATTERN = "href="


@dataclass(frozen=True)
class SecFiling:
    accession_number: str
    form_type: str
    cik: str
    company_name: str
    ticker: str
    filing_date: str
    accepted_at: str | None
    primary_document: str
    document_url: str
    items: tuple[str, ...] = ()
    report_date: str = ""

    @property
    def is_amended(self) -> bool:
        return self.form_type.upper().endswith("/A")

    def raw_metadata(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SecClassification:
    event_type: str
    materiality_classification: str
    reason: str


@dataclass(frozen=True)
class SecDocumentPayload:
    raw_snapshot: str
    content: str
    content_hash: str


def _normalise_form(value: str) -> str:
    return value.strip().upper()


def _normalise_accepted_at(value: str | None) -> str | None:
    if not value:
        return None
    raw = str(value).strip()
    if len(raw) == 14 and raw.isdigit():
        parsed = datetime.strptime(raw, "%Y%m%d%H%M%S").replace(tzinfo=UTC)
        return utc_iso(parsed)
    try:
        parsed = parse_utc(raw)
    except (TypeError, ValueError):
        return None
    return utc_iso(parsed)


def classify_sec_filing(filing: SecFiling, content: str) -> SecClassification:
    """Classify SEC primary evidence deterministically before any AI analysis.

    This deliberately makes no price prediction.  It only records an auditable
    event/materiality hint derived from the filing form, SEC item codes, and
    filing text.
    """

    form = _normalise_form(filing.form_type)
    lowered = " ".join((filing.primary_document, content)).lower()
    items = {item.strip() for item in filing.items if item.strip()}
    has_earnings = (
        "2.02" in items
        or "results of operations and financial condition" in lowered
        or "earnings release" in lowered
        or "quarterly results" in lowered
        or "financial results" in lowered
    )
    has_guidance = any(
        marker in lowered
        for marker in (
            "guidance",
            "outlook",
            "financial forecast",
            "we expect",
            "we anticipate",
            "we are raising",
            "we are lowering",
        )
    )

    if form.startswith("8-K"):
        if has_earnings and has_guidance:
            return SecClassification(
                "earnings_guidance",
                "material_earnings_guidance",
                "8-K item/text contains financial results and forward guidance",
            )
        if has_earnings:
            return SecClassification(
                "earnings",
                "material_earnings",
                "8-K item/text contains results of operations or financial results",
            )
        if has_guidance:
            return SecClassification(
                "guidance",
                "material_guidance",
                "8-K text contains explicit forward guidance or outlook",
            )
        return SecClassification(
            "corporate_filing",
            "unclassified",
            "8-K is stored as official evidence but has no deterministic earnings/guidance signal",
        )
    if form.startswith(("10-Q", "10-K")):
        return SecClassification(
            "financial_filing",
            "material_financial_filing",
            f"{form} is a periodic financial report; earnings/guidance extraction is deferred",
        )
    if form.startswith("6-K"):
        return SecClassification(
            "foreign_filing",
            "unclassified",
            "6-K is stored as foreign issuer evidence; materiality needs later review",
        )
    return SecClassification(
        "corporate_filing",
        "unclassified",
        f"{form or 'unknown'} is outside the initial SEC form classifier",
    )


class SecEdgarClient:
    """Small, rate-limited SEC EDGAR client with injectable I/O for tests."""

    def __init__(
        self,
        *,
        user_agent: str,
        timeout_seconds: float = 20.0,
        max_bytes: int = 4_000_000,
        min_interval_seconds: float = 0.2,
        opener: Callable[..., object] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if "@" not in user_agent:
            raise ValueError(
                "SEC_EDGAR_USER_AGENT must identify the requester and include a contact email address"
            )
        self.user_agent = user_agent
        self.timeout_seconds = timeout_seconds
        self.max_bytes = max_bytes
        self.min_interval_seconds = min_interval_seconds
        self.opener = opener or urllib.request.urlopen
        self.sleeper = sleeper
        self.clock = clock
        self._last_request_at: float | None = None

    def _request(self, url: str) -> tuple[bytes, str]:
        if self._last_request_at is not None:
            pause = self.min_interval_seconds - (self.clock() - self._last_request_at)
            if pause > 0:
                self.sleeper(pause)
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": self.user_agent,
                "Accept": "application/json,text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5",
            },
        )
        try:
            response = self.opener(request, timeout=self.timeout_seconds)
            self._last_request_at = self.clock()
            payload = response.read(self.max_bytes + 1)
            if len(payload) > self.max_bytes:
                raise ValueError(f"SEC response exceeds configured limit ({self.max_bytes} bytes)")
            headers = getattr(response, "headers", {})
            return payload, str(headers.get("Content-Type", ""))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RuntimeError(f"SEC EDGAR request failed: {exc}") from exc

    def _json(self, url: str) -> dict[str, Any]:
        payload, _ = self._request(url)
        try:
            decoded = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"SEC EDGAR returned invalid JSON for {url}") from exc
        if not isinstance(decoded, dict):
            raise RuntimeError(f"SEC EDGAR returned a non-object JSON response for {url}")
        return decoded

    def resolve_ticker(self, symbol: str) -> tuple[int, str]:
        wanted = symbol.strip().upper()
        if not wanted:
            raise ValueError("ticker symbol cannot be empty")
        listing = self._json(SEC_COMPANY_TICKERS_URL)
        for record in listing.values():
            if not isinstance(record, dict):
                continue
            if str(record.get("ticker") or "").upper() == wanted:
                try:
                    return int(record["cik_str"]), str(record.get("title") or wanted)
                except (KeyError, TypeError, ValueError) as exc:
                    raise RuntimeError(f"SEC ticker directory has invalid CIK data for {wanted}") from exc
        raise LookupError(f"SEC EDGAR has no ticker-directory entry for {wanted}")

    def discover_filings(
        self,
        symbol: str,
        *,
        forms: Iterable[str] = SUPPORTED_FORMS,
        limit: int = 10,
    ) -> list[SecFiling]:
        wanted_forms = {_normalise_form(value) for value in forms if value.strip()}
        if not wanted_forms:
            raise ValueError("at least one SEC form type is required")
        if limit < 1:
            return []
        cik, company_name = self.resolve_ticker(symbol)
        submission = self._json(SEC_SUBMISSIONS_URL.format(cik=cik))
        recent = submission.get("filings", {}).get("recent", {})
        if not isinstance(recent, dict):
            raise RuntimeError(f"SEC submissions response has no recent filings for {symbol.upper()}")

        return self._filings_from_columns(
            symbol=symbol,
            cik=cik,
            company_name=str(submission.get("name") or company_name),
            columns=recent,
            wanted_forms=wanted_forms,
            limit=limit,
        )

    def discover_filings_between(
        self,
        symbol: str,
        *,
        forms: Iterable[str] = ("8-K",),
        start: date,
        end: date,
        limit: int = 100,
    ) -> list[SecFiling]:
        wanted_forms = {_normalise_form(value) for value in forms if value.strip()}
        if not wanted_forms:
            raise ValueError("at least one SEC form type is required")
        if end < start:
            raise ValueError("historical import end date must be on or after start date")
        if limit < 1:
            return []

        cik, company_name = self.resolve_ticker(symbol)
        submission = self._json(SEC_SUBMISSIONS_URL.format(cik=cik))
        company = str(submission.get("name") or company_name)
        filings: list[SecFiling] = []
        filings.extend(
            self._filings_from_columns(
                symbol=symbol,
                cik=cik,
                company_name=company,
                columns=submission.get("filings", {}).get("recent", {}),
                wanted_forms=wanted_forms,
                limit=limit,
            )
        )
        for archive in submission.get("filings", {}).get("files", []) or []:
            if len(filings) >= limit:
                break
            if not isinstance(archive, dict) or not archive.get("name"):
                continue
            archive_payload = self._json(SEC_SUBMISSIONS_ARCHIVE_URL.format(name=str(archive["name"])))
            columns = archive_payload.get("filings", {}).get("recent", archive_payload)
            filings.extend(
                self._filings_from_columns(
                    symbol=symbol,
                    cik=cik,
                    company_name=company,
                    columns=columns,
                    wanted_forms=wanted_forms,
                    limit=limit - len(filings),
                )
            )

        by_accession: dict[str, SecFiling] = {}
        for filing in filings:
            try:
                filing_day = date.fromisoformat(filing.filing_date)
            except ValueError:
                continue
            if start <= filing_day <= end:
                by_accession[filing.accession_number] = filing
        return sorted(
            by_accession.values(),
            key=lambda filing: (filing.filing_date, filing.accepted_at or ""),
            reverse=True,
        )[:limit]

    @staticmethod
    def _filings_from_columns(
        *,
        symbol: str,
        cik: int,
        company_name: str,
        columns: Any,
        wanted_forms: set[str],
        limit: int,
    ) -> list[SecFiling]:
        if not isinstance(columns, dict):
            return []
        forms_column = columns.get("form") or []
        results: list[SecFiling] = []
        for index, raw_form in enumerate(forms_column):
            form = _normalise_form(str(raw_form or ""))
            if form not in wanted_forms:
                continue
            accession = str((columns.get("accessionNumber") or [""])[index] or "")
            primary_document = str((columns.get("primaryDocument") or [""])[index] or "")
            if not accession or not primary_document:
                continue
            compact_accession = accession.replace("-", "")
            document_url = SEC_ARCHIVES_URL.format(
                cik=str(cik),
                accession=compact_accession,
                primary_document=primary_document,
            )
            items_value = str((columns.get("items") or [""])[index] or "")
            results.append(
                SecFiling(
                    accession_number=accession,
                    form_type=form,
                    cik=str(cik),
                    company_name=company_name,
                    ticker=symbol.strip().upper(),
                    filing_date=str((columns.get("filingDate") or [""])[index] or ""),
                    accepted_at=_normalise_accepted_at(
                        str((columns.get("acceptanceDateTime") or [""])[index] or "")
                    ),
                    primary_document=primary_document,
                    document_url=document_url,
                    items=tuple(item.strip() for item in items_value.split(",") if item.strip()),
                    report_date=str((columns.get("reportDate") or [""])[index] or ""),
                )
            )
            if len(results) >= limit:
                break
        return results

    def fetch_primary_document(self, filing: SecFiling) -> SecDocumentPayload:
        payload, content_type = self._request(filing.document_url)
        charset = "utf-8"
        if "charset=" in content_type.lower():
            charset = content_type.lower().split("charset=", 1)[1].split(";", 1)[0].strip()
        raw_snapshot = payload.decode(charset, errors="replace")
        content = (
            html_to_plain_text(raw_snapshot)
            if "html" in content_type.lower() or "<html" in raw_snapshot[:500].lower()
            else " ".join(raw_snapshot.split())
        )
        if not content:
            raise RuntimeError(f"SEC primary document is empty: {filing.document_url}")
        exhibit_sections: list[tuple[str, str, str]] = []
        for exhibit_url in self._earnings_exhibit_urls(filing.document_url, raw_snapshot):
            try:
                exhibit_payload, exhibit_content_type = self._request(exhibit_url)
            except Exception:
                continue
            exhibit_charset = "utf-8"
            if "charset=" in exhibit_content_type.lower():
                exhibit_charset = exhibit_content_type.lower().split("charset=", 1)[1].split(";", 1)[0].strip()
            exhibit_raw = exhibit_payload.decode(exhibit_charset, errors="replace")
            exhibit_content = (
                html_to_plain_text(exhibit_raw)
                if "html" in exhibit_content_type.lower() or "<html" in exhibit_raw[:500].lower()
                else " ".join(exhibit_raw.split())
            )
            if exhibit_content:
                exhibit_sections.append((exhibit_url, exhibit_raw, exhibit_content))
        if exhibit_sections:
            raw_snapshot = "\n\n".join(
                [raw_snapshot]
                + [f"<!-- SEC exhibit: {url} -->\n{raw}" for url, raw, _content in exhibit_sections]
            )
            content = "\n\n".join(
                [content]
                + [f"[SEC exhibit: {url}]\n{exhibit_content}" for url, _raw, exhibit_content in exhibit_sections]
            )
        return SecDocumentPayload(
            raw_snapshot=raw_snapshot,
            content=content,
            content_hash=hashlib.sha256(raw_snapshot.encode("utf-8")).hexdigest(),
        )

    def _earnings_exhibit_urls(self, document_url: str, raw_snapshot: str) -> tuple[str, ...]:
        """Return likely earnings press-release exhibit links from a filing page."""

        if EXHIBIT_LINK_PATTERN not in raw_snapshot.lower():
            return ()
        links: list[str] = []
        for match in re.finditer(r"""href\s*=\s*["']([^"']+)["']""", raw_snapshot, flags=re.IGNORECASE):
            href = match.group(1).strip()
            lowered = href.lower()
            if not href or href.startswith("#"):
                continue
            if lowered.endswith((".xsd", ".xml", ".jpg", ".jpeg", ".png", ".gif", ".css", ".js")):
                continue
            if not lowered.endswith((".htm", ".html", ".txt")):
                continue
            is_exhibit_99 = (
                "exhibit99" in lowered
                or "ex-99" in lowered
                or "ex99" in lowered
                or "exhibit-99" in lowered
                or "991" in lowered
                or ("99" in lowered and "ex" in lowered)
            )
            is_press_release = (
                ("press" in lowered and ("release" in lowered or "earn" in lowered))
                or "earningsrelease" in lowered
                or "earnings-release" in lowered
                or "earnings.htm" in lowered
                or "results.htm" in lowered
                or "erq" in lowered
                or re.search(r"q[1-4](?:fy)?\d{2,4}pr\.htm", lowered) is not None
                or re.search(r"q[1-4]\d{4}99\d?\.htm", lowered) is not None
            )
            if not (is_exhibit_99 or is_press_release):
                continue
            url = urllib.parse.urljoin(document_url, href)
            if url not in links and url != document_url:
                links.append(url)
        return tuple(links[:3])


class SecEdgarCollector:
    """Persist SEC primary documents without creating synthetic news events."""

    def __init__(self, database: Any, client: SecEdgarClient) -> None:
        self.database = database
        self.client = client

    def sync_symbols(
        self,
        symbols: Iterable[str],
        *,
        forms: Iterable[str] = ("8-K",),
        limit_per_symbol: int = 5,
        force: bool = False,
    ) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "ingestion_version": SEC_INGESTION_VERSION,
            "symbols": [],
            "discovered": 0,
            "fetched": 0,
            "duplicates": 0,
            "errors": 0,
        }
        for raw_symbol in symbols:
            symbol = raw_symbol.strip().upper()
            if not symbol:
                continue
            symbol_result: dict[str, Any] = {"symbol": symbol, "filings": [], "error": None}
            summary["symbols"].append(symbol_result)
            try:
                filings = self.client.discover_filings(
                    symbol,
                    forms=forms,
                    limit=limit_per_symbol,
                )
            except Exception as exc:
                symbol_result["error"] = str(exc)
                summary["errors"] += 1
                continue

            summary["discovered"] += len(filings)
            for filing in filings:
                document_id, created = self.database.register_sec_filing(filing)
                if not created:
                    summary["duplicates"] += 1
                item: dict[str, Any] = {
                    "document_id": document_id,
                    "accession_number": filing.accession_number,
                    "form_type": filing.form_type,
                    "status": "candidate",
                    "created": created,
                }
                symbol_result["filings"].append(item)
                if not force and not self.database.sec_filing_needs_fetch(document_id):
                    item["status"] = "already_fetched"
                    continue
                try:
                    payload = self.client.fetch_primary_document(filing)
                    classification = classify_sec_filing(filing, payload.content)
                    self.database.store_sec_filing_content(
                        document_id,
                        filing=filing,
                        payload=payload,
                        classification=classification,
                    )
                    item["status"] = "fetched"
                    item["event_type"] = classification.event_type
                    item["materiality"] = classification.materiality_classification
                    summary["fetched"] += 1
                except Exception as exc:
                    self.database.mark_sec_filing_error(document_id, str(exc))
                    item["status"] = "error"
                    item["error"] = str(exc)
                    summary["errors"] += 1
        return summary
