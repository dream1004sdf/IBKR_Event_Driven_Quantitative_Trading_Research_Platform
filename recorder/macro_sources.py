from __future__ import annotations

import hashlib
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Iterable

from .database import utc_iso
from .official_sources import html_to_plain_text

UTC = timezone.utc
MACRO_INGESTION_VERSION = "macro-official-v1"

FED_MONETARY_RSS_URL = "https://www.federalreserve.gov/feeds/press_monetary.xml"
BEA_RSS_URL = "https://apps.bea.gov/rss/rss.xml"
BLS_CPI_RELEASE_URL = "https://www.bls.gov/news.release/cpi.htm"
BLS_EMPLOYMENT_RELEASE_URL = "https://www.bls.gov/news.release/empsit.htm"


@dataclass(frozen=True)
class MacroDocument:
    source_type: str
    document_type: str
    event_type: str
    title: str
    source_url: str
    external_document_id: str
    content: str
    raw_snapshot: str
    content_hash: str
    published_at: str | None
    accepted_at: str | None
    first_seen_at: str
    available_at: str
    materiality_classification: str
    materiality_reason: str
    actual_subject_symbol: str = ""
    ingestion_version: str = MACRO_INGESTION_VERSION
    status: str = "fetched"


@dataclass(frozen=True)
class RssItem:
    title: str
    link: str
    guid: str
    published_at: str | None
    description: str


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalise_pubdate(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return utc_iso(parsed)


def parse_rss_items(raw_xml: str) -> list[RssItem]:
    root = ET.fromstring(raw_xml)
    items: list[RssItem] = []
    for node in root.findall(".//item"):
        def text(name: str) -> str:
            value = node.findtext(name)
            return " ".join(str(value or "").split())

        link = text("link")
        guid = text("guid") or link
        title = text("title")
        if not title or not link:
            continue
        items.append(
            RssItem(
                title=title,
                link=link,
                guid=guid,
                published_at=_normalise_pubdate(text("pubDate")),
                description=text("description"),
            )
        )
    return items


def classify_macro_document(source_type: str, title: str, content: str) -> tuple[str, str, str]:
    lowered = " ".join((title, content)).lower()
    if source_type == "federal_reserve":
        if "fomc" in lowered or "federal open market committee" in lowered:
            return (
                "fomc",
                "material_monetary_policy",
                "Federal Reserve release references FOMC monetary policy material",
            )
        return (
            "monetary_policy",
            "official_policy_release",
            "Federal Reserve monetary-policy press release",
        )
    if source_type == "bls":
        if "consumer price index" in lowered or "cpi" in lowered:
            return ("cpi", "material_inflation_release", "BLS CPI official release snapshot")
        if "employment situation" in lowered or "payroll employment" in lowered:
            return (
                "employment",
                "material_employment_release",
                "BLS Employment Situation official release snapshot",
            )
        return ("macro", "official_bls_release", "BLS official release snapshot")
    if source_type == "bea":
        if "personal income and outlays" in lowered or "personal consumption expenditures" in lowered:
            return ("pce", "material_pce_release", "BEA Personal Income and Outlays/PCE official release")
        return ("macro", "official_bea_release", "BEA official release")
    return ("macro", "official_release", "public official macro release")


class MacroOfficialClient:
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
        self.user_agent = user_agent
        self.timeout_seconds = timeout_seconds
        self.max_bytes = max_bytes
        self.min_interval_seconds = min_interval_seconds
        self.opener = opener or urllib.request.urlopen
        self.sleeper = sleeper
        self.clock = clock
        self._last_request_at: float | None = None

    def fetch(self, url: str) -> tuple[str, str, str]:
        if self._last_request_at is not None:
            pause = self.min_interval_seconds - (self.clock() - self._last_request_at)
            if pause > 0:
                self.sleeper(pause)
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": self.user_agent,
                "Accept": "application/rss+xml,application/xml,text/html,text/plain;q=0.9,*/*;q=0.5",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        try:
            response = self.opener(request, timeout=self.timeout_seconds)
            self._last_request_at = self.clock()
            data = response.read(self.max_bytes + 1)
            if len(data) > self.max_bytes:
                raise ValueError(f"official macro response exceeds configured limit ({self.max_bytes} bytes)")
            content_type = str(getattr(response, "headers", {}).get("Content-Type", ""))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RuntimeError(f"official macro request failed: {exc}") from exc
        charset = "utf-8"
        if "charset=" in content_type.lower():
            charset = content_type.lower().split("charset=", 1)[1].split(";", 1)[0].strip()
        raw = data.decode(charset, errors="replace")
        text = html_to_plain_text(raw) if "html" in content_type.lower() or "<html" in raw[:500].lower() else " ".join(raw.split())
        return raw, text, content_type


class MacroOfficialCollector:
    def __init__(self, database: Any, client: MacroOfficialClient) -> None:
        self.database = database
        self.client = client

    def sync(self, source: str = "all", *, limit: int = 5) -> dict[str, Any]:
        wanted = source.strip().lower()
        if wanted not in {"all", "fed", "federal_reserve", "bls", "bea"}:
            raise ValueError("source must be one of all, fed, bls, or bea")
        summary = {
            "ingestion_version": MACRO_INGESTION_VERSION,
            "source": wanted,
            "stored": 0,
            "duplicates": 0,
            "errors": 0,
            "documents": [],
        }
        if wanted in {"all", "fed", "federal_reserve"}:
            self._record_source(summary, "federal_reserve", lambda: self.sync_federal_reserve(limit=limit))
        if wanted in {"all", "bls"}:
            self._record_source(summary, "bls", self.sync_bls)
        if wanted in {"all", "bea"}:
            self._record_source(summary, "bea", lambda: self.sync_bea(limit=limit))
        return summary

    def _record_source(self, summary: dict[str, Any], source_type: str, producer: Callable[[], Iterable[MacroDocument]]) -> None:
        try:
            documents = producer()
        except Exception as exc:
            summary["errors"] += 1
            summary["documents"].append(
                {
                    "source_type": source_type,
                    "title": "",
                    "status": "error",
                    "error": str(exc),
                }
            )
            return
        self._record_many(summary, documents)

    def _record_many(self, summary: dict[str, Any], documents: Iterable[MacroDocument]) -> None:
        for document in documents:
            try:
                document_id, created = self.database.upsert_official_document(document)
                summary["documents"].append(
                    {
                        "document_id": document_id,
                        "source_type": document.source_type,
                        "event_type": document.event_type,
                        "title": document.title,
                        "status": "stored" if created else "duplicate",
                    }
                )
                if created:
                    summary["stored"] += 1
                else:
                    summary["duplicates"] += 1
            except Exception as exc:
                summary["errors"] += 1
                summary["documents"].append(
                    {
                        "source_type": getattr(document, "source_type", "unknown"),
                        "title": getattr(document, "title", ""),
                        "status": "error",
                        "error": str(exc),
                    }
                )

    def sync_federal_reserve(self, *, limit: int = 5) -> list[MacroDocument]:
        raw, _, _ = self.client.fetch(FED_MONETARY_RSS_URL)
        items = parse_rss_items(raw)
        selected = [
            item for item in items
            if any(marker in " ".join((item.title, item.description)).lower() for marker in ("fomc", "monetary policy", "federal funds"))
        ][: max(0, int(limit))]
        return [self._document_from_rss_item("federal_reserve", "monetary_policy_release", item) for item in selected]

    def sync_bea(self, *, limit: int = 5) -> list[MacroDocument]:
        raw, _, _ = self.client.fetch(BEA_RSS_URL)
        items = parse_rss_items(raw)
        selected = [
            item for item in items
            if any(marker in " ".join((item.title, item.description)).lower() for marker in ("personal income and outlays", "pce", "personal consumption expenditures"))
        ][: max(0, int(limit))]
        return [self._document_from_rss_item("bea", "bea_release", item) for item in selected]

    def sync_bls(self) -> list[MacroDocument]:
        definitions = (
            ("bls_cpi_release", "CPI official release", BLS_CPI_RELEASE_URL),
            ("bls_employment_release", "Employment Situation official release", BLS_EMPLOYMENT_RELEASE_URL),
        )
        documents: list[MacroDocument] = []
        for document_type, title, url in definitions:
            raw, content, _ = self.client.fetch(url)
            digest = _sha256(raw)
            if document_type == "bls_cpi_release":
                event_type, materiality, reason = (
                    "cpi",
                    "material_inflation_release",
                    "BLS CPI official release snapshot",
                )
            else:
                event_type, materiality, reason = (
                    "employment",
                    "material_employment_release",
                    "BLS Employment Situation official release snapshot",
                )
            now = utc_iso(datetime.now(UTC))
            documents.append(
                MacroDocument(
                    source_type="bls",
                    document_type=document_type,
                    event_type=event_type,
                    title=title,
                    source_url=url,
                    external_document_id=f"bls:{document_type}:{digest[:20]}",
                    content=content,
                    raw_snapshot=raw,
                    content_hash=digest,
                    published_at=None,
                    accepted_at=None,
                    first_seen_at=now,
                    available_at=now,
                    materiality_classification=materiality,
                    materiality_reason=reason,
                )
            )
        return documents

    def _document_from_rss_item(self, source_type: str, document_type: str, item: RssItem) -> MacroDocument:
        raw, content, _ = self.client.fetch(item.link)
        digest = _sha256(raw)
        event_type, materiality, reason = classify_macro_document(source_type, item.title, content)
        now = utc_iso(datetime.now(UTC))
        published_at = item.published_at
        return MacroDocument(
            source_type=source_type,
            document_type=document_type,
            event_type=event_type,
            title=item.title,
            source_url=item.link,
            external_document_id=f"{source_type}:{item.guid or item.link}",
            content=content,
            raw_snapshot=raw,
            content_hash=digest,
            published_at=published_at,
            accepted_at=published_at,
            first_seen_at=now,
            available_at=published_at or now,
            materiality_classification=materiality,
            materiality_reason=reason,
        )
