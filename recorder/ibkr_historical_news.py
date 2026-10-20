from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, time as dt_time, timezone
from typing import Any

from ibapi.client import EClient
from ibapi.contract import Contract
from ibapi.wrapper import EWrapper

from .config import Settings
from .database import Database
from .dedup import DedupConfig, NewsDeduplicator
from .models import NewsInput

UTC = timezone.utc


def parse_cli_datetime(value: str, *, end_of_day: bool = False) -> datetime:
    stripped = value.strip()
    if len(stripped) == 10:
        parsed_date = date.fromisoformat(stripped)
        clock = dt_time(23, 59, 59) if end_of_day else dt_time(0, 0, 0)
        return datetime.combine(parsed_date, clock, tzinfo=UTC)
    parsed = datetime.fromisoformat(stripped.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def format_ibkr_news_datetime(value: datetime) -> str:
    # IBKR reqHistoricalNews uses a different timestamp format from historical
    # bars: yyyy-MM-dd HH:mm:ss.0.
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.0")


def parse_ibkr_news_time(value: str, *, fallback: datetime | None = None) -> datetime:
    raw = str(value or "").strip()
    cleaned = raw.replace(" UTC", "").replace(" GMT", "").strip()
    if cleaned.endswith(".0"):
        cleaned = cleaned[:-2]

    formats = (
        "%Y%m%d %H:%M:%S",
        "%Y%m%d-%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
    )
    for fmt in formats:
        try:
            return datetime.strptime(cleaned, fmt).replace(tzinfo=UTC)
        except ValueError:
            pass

    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except ValueError:
        if fallback is not None:
            return fallback.astimezone(UTC)
        return datetime.now(UTC)


@dataclass(frozen=True)
class HistoricalNewsBackfillResult:
    requested_symbols: int
    completed_symbols: int
    provider_codes: tuple[str, ...]
    candidates: int
    skipped_out_of_range: int
    created: int
    duplicates: int
    pages_requested: int
    article_bodies_requested: int
    article_bodies_stored: int
    errors: tuple[str, ...]
    timed_out: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "requested_symbols": self.requested_symbols,
            "completed_symbols": self.completed_symbols,
            "provider_codes": list(self.provider_codes),
            "candidates": self.candidates,
            "skipped_out_of_range": self.skipped_out_of_range,
            "created": self.created,
            "duplicates": self.duplicates,
            "pages_requested": self.pages_requested,
            "article_bodies_requested": self.article_bodies_requested,
            "article_bodies_stored": self.article_bodies_stored,
            "errors": list(self.errors),
            "timed_out": self.timed_out,
        }


class IBKRHistoricalNewsBackfiller(EWrapper, EClient):
    """Read-only IBKR historical-news backfill into the canonical news store."""

    def __init__(
        self,
        settings: Settings,
        *,
        symbols: tuple[str, ...],
        start: datetime,
        end: datetime,
        provider_codes: tuple[str, ...] | None = None,
        limit_per_symbol: int = 100,
        fetch_article_body: bool | None = None,
        client_id: int | None = None,
        timeout_seconds: float = 30.0,
        max_pages_per_symbol: int = 1,
    ) -> None:
        EClient.__init__(self, self)
        self.settings = settings
        self.symbols = tuple(dict.fromkeys(symbol.upper() for symbol in symbols if symbol.strip()))
        self.start = start.astimezone(UTC)
        self.end = end.astimezone(UTC)
        self.provider_codes_config = provider_codes or settings.news_provider_codes
        self.limit_per_symbol = max(1, int(limit_per_symbol))
        self.fetch_article_body = settings.fetch_article_body if fetch_article_body is None else fetch_article_body
        self.client_id = client_id if client_id is not None else settings.client_id + 2000
        timeout_value = float(timeout_seconds)
        self.timeout_seconds: float | None = None if timeout_value <= 0 else timeout_value
        self.max_pages_per_symbol = max(1, int(max_pages_per_symbol))

        self.log = logging.getLogger(self.__class__.__name__)
        self.db = Database(settings.database_path)
        self.dedup = NewsDeduplicator(
            self.db,
            DedupConfig(
                hash_window_hours=settings.hash_window_hours,
                fuzzy_window_minutes=settings.fuzzy_window_minutes,
                fuzzy_threshold=settings.fuzzy_threshold,
                fuzzy_require_same_symbol=settings.fuzzy_require_same_symbol,
            ),
        )

        self._request_ids = iter(range(700000, 800000))
        self._contracts_by_symbol: dict[str, Contract] = {}
        self._contract_req_to_symbol: dict[int, str] = {}
        self._historical_req_to_symbol: dict[int, str] = {}
        self._historical_req_anchor: dict[int, datetime] = {}
        self._article_req_to_news_id: dict[int, int] = {}
        self._providers: dict[str, str] = {}
        self._providers_received = False
        self._contract_requests_completed: set[int] = set()
        self._historical_requests_completed: set[int] = set()
        self._symbol_pages_requested: dict[str, int] = {}
        self._symbol_oldest_seen: dict[str, datetime] = {}
        self._requests_started = False
        self._pending_article_requests = 0
        self._api_thread: threading.Thread | None = None
        self._done = threading.Event()
        self._lock = threading.Lock()

        self._candidates = 0
        self._skipped_out_of_range = 0
        self._created = 0
        self._duplicates = 0
        self._article_bodies_requested = 0
        self._article_bodies_stored = 0
        self._errors: list[str] = []

    @property
    def pages_requested(self) -> int:
        return sum(self._symbol_pages_requested.values())

    @staticmethod
    def _stock_contract(symbol: str) -> Contract:
        contract = Contract()
        contract.symbol = symbol
        contract.secType = "STK"
        contract.exchange = "SMART"
        contract.currency = "USD"
        return contract

    def selected_provider_codes(self) -> tuple[str, ...]:
        configured = self.provider_codes_config
        if configured == ("AUTO",):
            return tuple(self._providers)
        return tuple(code for code in configured if not self._providers or code in self._providers)

    def nextValidId(self, orderId: int) -> None:  # noqa: N802
        self.log.info("IBKR historical-news backfill connected; order id %s is unused", orderId)
        self.reqNewsProviders()
        for symbol in self.symbols:
            req_id = next(self._request_ids)
            self._contract_req_to_symbol[req_id] = symbol
            self.reqContractDetails(req_id, self._stock_contract(symbol))

    def newsProviders(self, newsProviders):  # noqa: N802
        self._providers = {provider.code: provider.name for provider in newsProviders}
        self._providers_received = True
        for code, name in self._providers.items():
            self.db.upsert_provider(code, name)
        self.log.info("IBKR historical-news providers: %s", self._providers or "none")
        self._try_start_historical_requests()

    def contractDetails(self, reqId, contractDetails):  # noqa: N802
        symbol = self._contract_req_to_symbol.get(reqId)
        if symbol and symbol not in self._contracts_by_symbol:
            self._contracts_by_symbol[symbol] = contractDetails.contract

    def contractDetailsEnd(self, reqId: int) -> None:  # noqa: N802
        self._contract_requests_completed.add(reqId)
        symbol = self._contract_req_to_symbol.get(reqId)
        if symbol and symbol not in self._contracts_by_symbol:
            self._record_error(f"No unambiguous IBKR contract found for {symbol}")
        self._try_start_historical_requests()

    def _try_start_historical_requests(self) -> None:
        if self._requests_started:
            return
        if not self._providers_received:
            return
        if len(self._contract_requests_completed) < len(self._contract_req_to_symbol):
            return

        self._requests_started = True
        provider_codes = self.selected_provider_codes()
        if not provider_codes:
            self._record_error("No IBKR news provider codes are available for historical backfill")
            self._done.set()
            return

        provider_code_string = "+".join(provider_codes)
        for symbol in self.symbols:
            if symbol not in self._contracts_by_symbol:
                continue
            self._request_historical_page(symbol, provider_code_string, self.end)

        if not self._historical_req_to_symbol:
            self._done.set()

    def _request_historical_page(self, symbol: str, provider_code_string: str, anchor: datetime) -> bool:
        contract = self._contracts_by_symbol.get(symbol)
        if contract is None:
            return False
        con_id = int(getattr(contract, "conId", 0) or 0)
        if con_id <= 0:
            self._record_error(f"IBKR contract for {symbol} has no conId")
            return False
        page_count = self._symbol_pages_requested.get(symbol, 0)
        if page_count >= self.max_pages_per_symbol:
            return False
        req_id = next(self._request_ids)
        anchor_utc = anchor.astimezone(UTC)
        self._historical_req_to_symbol[req_id] = symbol
        self._historical_req_anchor[req_id] = anchor_utc
        self._symbol_pages_requested[symbol] = page_count + 1
        self.reqHistoricalNews(
            req_id,
            con_id,
            provider_code_string,
            format_ibkr_news_datetime(anchor_utc),
            "",
            self.limit_per_symbol,
            [],
        )
        self.log.info(
            "Requested IBKR historical news page %s for %s providers=%s anchor=%s limit=%s",
            self._symbol_pages_requested[symbol],
            symbol,
            provider_code_string,
            anchor_utc.isoformat(),
            self.limit_per_symbol,
        )
        return True

    def historicalNews(  # noqa: N802
        self,
        requestId: int,
        time: str,
        providerCode: str,
        articleId: str,
        headline: str,
    ) -> None:
        symbol = self._historical_req_to_symbol.get(requestId, "UNKNOWN")
        contract = self._contracts_by_symbol.get(symbol)
        con_id = int(getattr(contract, "conId", 0) or 0) if contract is not None else None
        news_id = self._ingest_news(symbol, con_id, providerCode, articleId, headline, time)
        if news_id is not None and self.fetch_article_body and articleId:
            article_req_id = next(self._request_ids)
            with self._lock:
                self._pending_article_requests += 1
                self._article_bodies_requested += 1
                self._article_req_to_news_id[article_req_id] = news_id
            self.reqNewsArticle(article_req_id, providerCode, articleId, [])

    def historicalNewsEnd(self, requestId: int, hasMore: bool) -> None:  # noqa: N802
        self._historical_requests_completed.add(requestId)
        symbol = self._historical_req_to_symbol.get(requestId, "UNKNOWN")
        self.log.info("IBKR historical news completed for %s hasMore=%s", symbol, hasMore)
        if hasMore and symbol != "UNKNOWN":
            provider_code_string = "+".join(self.selected_provider_codes())
            oldest = self._symbol_oldest_seen.get(symbol)
            if oldest is not None and oldest > self.start:
                next_anchor = oldest - timedelta(seconds=1)
                if self._request_historical_page(symbol, provider_code_string, next_anchor):
                    return
        self._maybe_done()

    def newsArticle(self, requestId: int, articleType: int, articleText: str) -> None:  # noqa: N802
        news_id = self._article_req_to_news_id.pop(requestId, None)
        if news_id is not None:
            self.db.update_article_body(news_id, str(articleText))
            with self._lock:
                self._article_bodies_stored += 1
                self._pending_article_requests = max(0, self._pending_article_requests - 1)
        self._maybe_done()

    def error(self, reqId, *args) -> None:
        code = args[0] if args and isinstance(args[0], int) else None
        informational = {2104, 2106, 2107, 2108, 2158}
        if code in informational:
            self.log.info("IBKR status reqId=%s details=%s", reqId, args)
            return
        symbol_context = (
            self._contract_req_to_symbol.get(reqId)
            or self._historical_req_to_symbol.get(reqId)
            or "UNKNOWN"
        )
        self._record_error(f"IBKR message reqId={reqId} symbol={symbol_context} details={args}")
        if reqId in self._contract_req_to_symbol:
            self._contract_requests_completed.add(reqId)
            symbol = self._contract_req_to_symbol.get(reqId)
            if symbol and symbol not in self._contracts_by_symbol:
                self._record_error(f"No IBKR contract found for {symbol}; skipping historical news for this symbol")
            self._try_start_historical_requests()
            return
        if reqId in self._historical_req_to_symbol:
            self._historical_requests_completed.add(reqId)
            self._maybe_done()
            return
        if reqId in self._article_req_to_news_id:
            self._article_req_to_news_id.pop(reqId, None)
            with self._lock:
                self._pending_article_requests = max(0, self._pending_article_requests - 1)
            self._maybe_done()

    def _ingest_news(
        self,
        symbol: str,
        con_id: int | None,
        provider_code: str,
        article_id: str,
        headline: str,
        provider_time: str,
    ) -> int | None:
        now = datetime.now(UTC)
        published = parse_ibkr_news_time(provider_time, fallback=now)
        previous_oldest = self._symbol_oldest_seen.get(symbol)
        if previous_oldest is None or published < previous_oldest:
            self._symbol_oldest_seen[symbol] = published
        if published < self.start or published > self.end:
            with self._lock:
                self._candidates += 1
                self._skipped_out_of_range += 1
            return None
        extra = {
            "source": "ibkr_historical_news",
            "backfilled_at": now.isoformat(),
            "ibkr_provider_time": str(provider_time),
        }
        item = NewsInput(
            provider=str(provider_code),
            article_id=str(article_id or "").strip()
            or (
                "ibkr-historical:"
                f"{symbol}:{int(published.timestamp())}:"
                f"{hashlib.sha256(str(headline).encode('utf-8')).hexdigest()[:16]}"
            ),
            headline=str(headline),
            published_at=published,
            received_at=published,
            primary_symbol=symbol,
            con_id=con_id,
            extra_data=json.dumps(extra, sort_keys=True),
            provider_published_at=published,
            first_seen_at=now,
        )
        decision = self.dedup.ingest(item)
        with self._lock:
            self._candidates += 1
            if decision.is_duplicate:
                self._duplicates += 1
            else:
                self._created += 1
        return decision.canonical_news_id

    def _maybe_done(self) -> None:
        if not self._requests_started:
            return
        if len(self._historical_requests_completed) < len(self._historical_req_to_symbol):
            return
        with self._lock:
            pending_articles = self._pending_article_requests
        if pending_articles <= 0:
            self._done.set()

    def _record_error(self, message: str) -> None:
        self.log.warning(message)
        with self._lock:
            self._errors.append(message)

    def run_backfill(self) -> HistoricalNewsBackfillResult:
        self.log.info(
            "Connecting to IB Gateway %s:%s clientId=%s for historical news",
            self.settings.host,
            self.settings.port,
            self.client_id,
        )
        self.connect(self.settings.host, self.settings.port, clientId=self.client_id)
        self._api_thread = threading.Thread(target=self.run, daemon=True)
        self._api_thread.start()
        timed_out = not self._done.wait(self.timeout_seconds)
        self.stop()
        return HistoricalNewsBackfillResult(
            requested_symbols=len(self.symbols),
            completed_symbols=len(
                {
                    self._historical_req_to_symbol[req_id]
                    for req_id in self._historical_requests_completed
                    if req_id in self._historical_req_to_symbol
                }
            ),
            provider_codes=self.selected_provider_codes(),
            candidates=self._candidates,
            skipped_out_of_range=self._skipped_out_of_range,
            created=self._created,
            duplicates=self._duplicates,
            pages_requested=self.pages_requested,
            article_bodies_requested=self._article_bodies_requested,
            article_bodies_stored=self._article_bodies_stored,
            errors=tuple(self._errors),
            timed_out=timed_out,
        )

    def stop(self) -> None:
        if self.isConnected():
            self.disconnect()
        if self._api_thread is not None and self._api_thread.is_alive():
            time.sleep(0.1)
