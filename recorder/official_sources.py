from __future__ import annotations

import hashlib
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from typing import Callable, Iterable
from urllib.parse import urlparse

_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.I)

_OFFICIAL_DOMAINS: tuple[tuple[str, str], ...] = (
    ("sec.gov", "sec"),
    ("federalreserve.gov", "federal_reserve"),
    ("bea.gov", "bea"),
    ("bls.gov", "bls"),
    ("treasury.gov", "treasury"),
    ("census.gov", "census"),
    ("energy.gov", "government"),
    ("whitehouse.gov", "government"),
)

_REPUTABLE_PROVIDER_PREFIXES = (
    "DJ",
    "BRFG",
    "REUTERS",
    "BLOOMBERG",
)


@dataclass(frozen=True)
class SourceAssessment:
    source_tier: str
    source_kind: str
    official_urls: tuple[str, ...]
    reason: str


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.parts.append(data.strip())


def html_to_plain_text(value: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(value)
        return " ".join(unescape(" ".join(parser.parts)).split())
    except Exception:
        return " ".join(unescape(value).split())


def official_source_kind(url: str) -> str | None:
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return None
    for domain, kind in _OFFICIAL_DOMAINS:
        if host == domain or host.endswith("." + domain):
            return kind
    if host.startswith("investor.") or host.startswith("investors."):
        return "company_ir"
    path = (urlparse(url).path or "").lower()
    if any(token in path for token in ("/investor", "/investors", "/ir/", "/news-releases")):
        return "company_ir"
    return None


def extract_official_urls(raw_text: str | None) -> tuple[str, ...]:
    if not raw_text:
        return ()
    result: list[str] = []
    for match in _URL_RE.findall(raw_text):
        cleaned = match.rstrip(".,);]}")
        if official_source_kind(cleaned) and cleaned not in result:
            result.append(cleaned)
    return tuple(result)


def assess_source(provider: str, headline: str, body: str | None, official_documents: Iterable[dict] = ()) -> SourceAssessment:
    docs = list(official_documents)
    fetched_docs = [d for d in docs if str(d.get("status", "")) in {"fetched", "manual"} and d.get("content")]
    candidate_urls = list(extract_official_urls(body))
    for document in docs:
        url = str(document.get("source_url") or "")
        if url and url not in candidate_urls:
            candidate_urls.append(url)

    if fetched_docs:
        kinds = {str(d.get("source_type") or "official") for d in fetched_docs}
        return SourceAssessment(
            source_tier="official_primary",
            source_kind=sorted(kinds)[0],
            official_urls=tuple(candidate_urls),
            reason="official document content attached to the event snapshot",
        )

    provider_upper = provider.upper()
    if any(provider_upper.startswith(prefix) for prefix in _REPUTABLE_PROVIDER_PREFIXES):
        tier = "reputable_secondary"
        kind = "licensed_news"
    else:
        tier = "secondary"
        kind = "news"

    if candidate_urls:
        return SourceAssessment(
            source_tier=tier,
            source_kind=kind,
            official_urls=tuple(candidate_urls),
            reason="official source candidate discovered but content not attached",
        )
    return SourceAssessment(tier, kind, (), "no official source document attached")


class OfficialSourceFetcher:
    """Allowlist-only fetcher for public official documents.

    It never fetches licensed IBKR article URLs.  Only government, SEC, Federal Reserve,
    and obvious company investor-relations URLs pass the allowlist.
    """

    def __init__(
        self,
        *,
        timeout_seconds: float = 15.0,
        user_agent: str = "IBKRMarketEventRecorder/0.4 contact=local-user",
        opener: Callable[..., object] | None = None,
        max_bytes: int = 2_000_000,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.user_agent = user_agent
        self.opener = opener or urllib.request.urlopen
        self.max_bytes = max_bytes

    def fetch(self, url: str) -> tuple[str, str, str]:
        kind = official_source_kind(url)
        if kind is None:
            raise ValueError(f"URL is not on the official-source allowlist: {url}")
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": self.user_agent,
                "Accept": "text/html,application/xhtml+xml,text/plain,application/xml;q=0.9,*/*;q=0.5",
            },
        )
        try:
            response = self.opener(request, timeout=self.timeout_seconds)
            data = response.read(self.max_bytes + 1)
            if len(data) > self.max_bytes:
                raise ValueError("official document exceeds configured size limit")
            content_type = str(getattr(response, "headers", {}).get("Content-Type", ""))
            charset = "utf-8"
            if "charset=" in content_type:
                charset = content_type.split("charset=", 1)[1].split(";", 1)[0].strip()
            decoded = data.decode(charset, errors="replace")
            text = html_to_plain_text(decoded) if "html" in content_type.lower() or "<html" in decoded[:500].lower() else " ".join(decoded.split())
            if not text:
                raise ValueError("official document contained no readable text")
            return kind, text, hashlib.sha256(text.encode("utf-8")).hexdigest()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RuntimeError(f"official-source fetch failed: {exc}") from exc
