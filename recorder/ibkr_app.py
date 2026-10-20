from __future__ import annotations

import hashlib
import itertools
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ibapi.client import EClient
from ibapi.contract import Contract
from ibapi.ticktype import TickTypeEnum
from ibapi.wrapper import EWrapper

from .analysis_database import AnalysisDatabase
from .config import Settings
from .dedup import DedupConfig, NewsDeduplicator
from .external_market_sources import build_breaking_news_signal, classify_breaking_news_urgency
from .models import NewsInput
from .price_cache import PriceCache
from .reactions import ReactionTracker

UTC = timezone.utc


BROADTAPE_NEWS_CONTRACTS: dict[str, tuple[str, str]] = {
    # IBKR API News BroadTape contracts.  Benzinga is the primary target here:
    # symbol=BZ:BZ_ALL, secType=NEWS, exchange=BZ.
    "BZ": ("BZ:BZ_ALL", "BZ"),
    # Some accounts expose Briefing/other broad tapes under these codes.  Keep
    # them explicit instead of guessing Dow Jones variants.
    "BRF": ("BRF:BRF_ALL", "BRF"),
    "BT": ("BRF:BRF_ALL", "BRF"),
    "FLY": ("FLY:FLY_ALL", "FLY"),
}

def _tick_type_name(tick_type: int) -> str:
    for method_name in ("toStr", "to_str", "to_Str"):
        method = getattr(TickTypeEnum, method_name, None)
        if method is not None:
            try:
                return str(method(tick_type))
            except Exception:
                pass
    return str(tick_type)


class IBKRMarketEventRecorder(EWrapper, EClient):
    def __init__(self, settings: Settings):
        EClient.__init__(self, self)
        self.settings = settings
        self.log = logging.getLogger(self.__class__.__name__)
        self.db = AnalysisDatabase(settings.database_path)
        self.prices = PriceCache()
        self.dedup = NewsDeduplicator(
            self.db,
            DedupConfig(
                hash_window_hours=settings.hash_window_hours,
                fuzzy_window_minutes=settings.fuzzy_window_minutes,
                fuzzy_threshold=settings.fuzzy_threshold,
                fuzzy_require_same_symbol=settings.fuzzy_require_same_symbol,
            ),
        )
        self.reactions = ReactionTracker(
            self.db,
            self.prices,
            settings.reaction_horizons,
            settings.price_max_age_seconds,
            settings.reaction_max_lateness_seconds,
            settings.reaction_max_attempts,
            settings.reaction_retry_base_seconds,
            settings.reaction_retry_max_seconds,
            quote_integrity_enabled=settings.quote_integrity_enabled,
            max_spread_ratio=settings.quote_integrity_max_spread_ratio,
            max_last_midpoint_deviation=settings.quote_integrity_max_last_midpoint_deviation,
            max_close_reference_deviation=settings.quote_integrity_max_close_reference_deviation,
            require_stock_contract=settings.quote_integrity_require_stock_contract,
        )
        self._request_ids = itertools.count(1000)
        self._contracts: dict[str, Contract] = {}
        self._contract_req_to_symbol: dict[int, str] = {}
        self._contract_requests_completed: set[int] = set()
        self._market_req_to_symbol: dict[int, str] = {}
        self._news_req_to_symbol: dict[int, str] = {}
        self._broadtape_req_to_provider: dict[int, str] = {}
        self._article_req_to_news_id: dict[int, int] = {}
        self._providers: dict[str, str] = {}
        self._providers_received = False
        self._subscriptions_started = False
        self._api_thread: Optional[threading.Thread] = None
        self._reaction_thread: Optional[threading.Thread] = None
        self._reconnect_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._connect_lock = threading.RLock()
        self._next_valid_id_received = threading.Event()
        self._connection_status_lock = threading.RLock()
        self._connection_status: dict[str, object] = {
            "status": "not_started",
            "connected": False,
            "host": settings.host,
            "port": settings.port,
            "client_id": settings.client_id,
            "reconnect_enabled": settings.ibkr_reconnect_enabled,
            "reconnect_attempt_count": 0,
            "successful_reconnect_count": 0,
            "last_event": "not_started",
        }
        self._reconnect_attempt_count = 0
        self._successful_reconnect_count = 0
        self._current_reconnect_delay = float(settings.ibkr_reconnect_initial_delay_seconds)
        self._next_reconnect_at_monotonic = 0.0
        self._last_reconnect_reason = "startup"

    @staticmethod
    def _utc_now_iso() -> str:
        return datetime.now(UTC).isoformat()

    def _connection_status_path(self) -> Path:
        return Path(self.settings.ibkr_connection_status_json_path).expanduser()

    def _write_connection_status(self, *, status: str, event: str, error: str | None = None) -> None:
        now = self._utc_now_iso()
        connected = bool(self.isConnected())
        payload: dict[str, object]
        with self._connection_status_lock:
            self._connection_status.update(
                {
                    "status": status,
                    "connected": connected,
                    "host": self.settings.host,
                    "port": self.settings.port,
                    "client_id": self.settings.client_id,
                    "reconnect_enabled": self.settings.ibkr_reconnect_enabled,
                    "updated_at": now,
                    "last_event": event,
                    "last_error": error,
                    "reconnect_attempt_count": self._reconnect_attempt_count,
                    "successful_reconnect_count": self._successful_reconnect_count,
                    "next_reconnect_delay_seconds": round(float(self._current_reconnect_delay), 3),
                    "next_reconnect_after_seconds": round(max(0.0, self._next_reconnect_at_monotonic - time.monotonic()), 3),
                }
            )
            payload = dict(self._connection_status)
        try:
            path = self._connection_status_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
            tmp_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp_path.replace(path)
        except Exception:
            self.log.exception("Failed to write IBKR connection status artifact")

    def _reset_subscription_state_for_reconnect(self) -> None:
        self._contracts.clear()
        self._contract_req_to_symbol.clear()
        self._contract_requests_completed.clear()
        self._market_req_to_symbol.clear()
        self._news_req_to_symbol.clear()
        self._broadtape_req_to_provider.clear()
        self._article_req_to_news_id.clear()
        self._providers.clear()
        self._providers_received = False
        self._subscriptions_started = False
        self._next_valid_id_received.clear()

    def _schedule_reconnect(self, reason: str, *, immediate: bool = False) -> None:
        if not self.settings.ibkr_reconnect_enabled or self._stop.is_set():
            return
        delay = 0.0 if immediate else max(0.0, float(self._current_reconnect_delay))
        with self._connect_lock:
            candidate_due = time.monotonic() + delay
            if immediate or self._next_reconnect_at_monotonic <= 0:
                self._next_reconnect_at_monotonic = candidate_due
            else:
                self._next_reconnect_at_monotonic = min(self._next_reconnect_at_monotonic, candidate_due)
            self._last_reconnect_reason = reason
        self._write_connection_status(status="reconnect_scheduled", event=reason)

    def _connect_once(self, reason: str) -> None:
        with self._connect_lock:
            if self.isConnected():
                return
            self._reset_subscription_state_for_reconnect()
            self.log.info(
                "Connecting to IB Gateway %s:%s clientId=%s reason=%s",
                self.settings.host,
                self.settings.port,
                self.settings.client_id,
                reason,
            )
            self._write_connection_status(status="connecting", event=reason)
            self.connect(
                self.settings.host,
                self.settings.port,
                clientId=self.settings.client_id,
            )
            self._api_thread = threading.Thread(target=self.run, daemon=True, name="ibkr-api-client")
            self._api_thread.start()
        if not self._next_valid_id_received.wait(float(self.settings.ibkr_reconnect_connect_timeout_seconds)):
            try:
                if self.isConnected():
                    self.disconnect()
            finally:
                raise TimeoutError(
                    "Timed out waiting for IBKR nextValidId "
                    f"on {self.settings.host}:{self.settings.port}"
                )
        self._write_connection_status(status="connected", event=f"{reason}_connected")

    def _connection_watchdog_loop(self) -> None:
        while not self._stop.is_set():
            if self.isConnected():
                self._write_connection_status(status="connected", event="watchdog_connected")
                self._stop.wait(float(self.settings.ibkr_reconnect_check_seconds))
                continue
            if self._next_reconnect_at_monotonic <= 0:
                self._schedule_reconnect("watchdog_detected_disconnected", immediate=True)
            remaining = self._next_reconnect_at_monotonic - time.monotonic()
            if remaining > 0:
                self._write_connection_status(status="reconnect_wait", event=self._last_reconnect_reason)
                self._stop.wait(min(float(self.settings.ibkr_reconnect_check_seconds), max(0.5, remaining)))
                continue
            self._reconnect_attempt_count += 1
            try:
                self._write_connection_status(status="reconnecting", event=self._last_reconnect_reason)
                self._connect_once(self._last_reconnect_reason)
            except Exception as exc:
                error = str(exc)
                self.log.warning("IBKR reconnect attempt %s failed: %s", self._reconnect_attempt_count, error)
                self._current_reconnect_delay = min(
                    float(self.settings.ibkr_reconnect_max_delay_seconds),
                    max(float(self.settings.ibkr_reconnect_initial_delay_seconds), float(self._current_reconnect_delay))
                    * float(self.settings.ibkr_reconnect_backoff_multiplier),
                )
                self._next_reconnect_at_monotonic = time.monotonic() + self._current_reconnect_delay
                self._write_connection_status(status="reconnect_failed", event=self._last_reconnect_reason, error=error)
                self._stop.wait(float(self.settings.ibkr_reconnect_check_seconds))
                continue
            self._successful_reconnect_count += 1
            self._current_reconnect_delay = float(self.settings.ibkr_reconnect_initial_delay_seconds)
            self._next_reconnect_at_monotonic = 0.0
            self._write_connection_status(status="connected", event="reconnect_success")
            self._stop.wait(float(self.settings.ibkr_reconnect_check_seconds))

    def nextValidId(self, orderId: int) -> None:  # noqa: N802
        self.log.info("IBKR connected; next valid order id=%s (orders are not used)", orderId)
        self._next_valid_id_received.set()
        with self._connection_status_lock:
            self._connection_status["connected_at"] = self._utc_now_iso()
        self.reqMarketDataType(self.settings.market_data_type)
        self.reqNewsProviders()
        for symbol in self.settings.all_price_symbols:
            request_id = next(self._request_ids)
            self._contract_req_to_symbol[request_id] = symbol
            self.reqContractDetails(request_id, self._stock_contract(symbol))

    @staticmethod
    def _stock_contract(symbol: str) -> Contract:
        contract = Contract()
        contract.symbol = symbol
        contract.secType = "STK"
        contract.exchange = "SMART"
        contract.currency = "USD"
        return contract

    def contractDetails(self, reqId, contractDetails):  # noqa: N802
        symbol = self._contract_req_to_symbol.get(reqId)
        if symbol and symbol not in self._contracts:
            self._contracts[symbol] = contractDetails.contract
            contract = contractDetails.contract
            self.prices.set_contract_metadata(
                symbol,
                con_id=int(getattr(contract, "conId", 0) or 0),
                contract_symbol=str(getattr(contract, "symbol", "") or symbol),
                sec_type=str(getattr(contract, "secType", "") or ""),
                exchange=str(getattr(contract, "exchange", "") or ""),
                primary_exchange=str(getattr(contract, "primaryExchange", "") or ""),
                currency=str(getattr(contract, "currency", "") or "unknown"),
                multiplier=str(getattr(contract, "multiplier", "") or ""),
                market_data_type=self.settings.market_data_type,
            )

    def contractDetailsEnd(self, reqId: int) -> None:  # noqa: N802
        self._contract_requests_completed.add(reqId)
        symbol = self._contract_req_to_symbol.get(reqId)
        if symbol and symbol not in self._contracts:
            self.log.error("No unambiguous IBKR contract found for %s", symbol)
        self._try_start_subscriptions()

    def newsProviders(self, newsProviders):  # noqa: N802
        self._providers = {provider.code: provider.name for provider in newsProviders}
        self._providers_received = True
        for code, name in self._providers.items():
            self.db.upsert_provider(code, name)
        self.log.info("News providers returned by IBKR: %s", self._providers or "none")
        self._try_start_subscriptions()

    def _selected_provider_codes(self) -> tuple[str, ...]:
        configured = self.settings.news_provider_codes
        if configured == ("AUTO",):
            return tuple(self._providers)
        return tuple(code for code in configured if code in self._providers)

    def _selected_broadtape_provider_codes(self) -> tuple[str, ...]:
        configured = self.settings.ibkr_broadtape_provider_codes
        if configured == ("AUTO",):
            return tuple(code for code in BROADTAPE_NEWS_CONTRACTS if code in self._providers)
        return tuple(
            code for code in configured
            if code in BROADTAPE_NEWS_CONTRACTS and (not self._providers or code in self._providers)
        )

    @staticmethod
    def _broadtape_contract(provider_code: str) -> Contract:
        symbol, exchange = BROADTAPE_NEWS_CONTRACTS[provider_code]
        contract = Contract()
        contract.symbol = symbol
        contract.secType = "NEWS"
        contract.exchange = exchange
        return contract

    def _try_start_subscriptions(self) -> None:
        if self._subscriptions_started:
            return
        if len(self._contract_requests_completed) < len(self._contract_req_to_symbol):
            return
        # Wait for provider callback, including the valid case of an empty list.
        if not self._providers_received:
            return

        self._subscriptions_started = True
        for symbol in self.settings.all_price_symbols:
            contract = self._contracts.get(symbol)
            if contract is None:
                continue
            request_id = next(self._request_ids)
            self._market_req_to_symbol[request_id] = symbol
            self.reqMktData(
                request_id,
                contract,
                "",
                False,
                False,
                [],
            )

        provider_codes = self._selected_provider_codes()
        if not provider_codes:
            self.log.warning(
                "No configured API news provider is available. Price recording remains active."
            )
            return

        generic_ticks = "mdoff,292:" + "+".join(provider_codes)
        for symbol in self.settings.all_news_symbols:
            if symbol not in self._contracts:
                continue
            request_id = next(self._request_ids)
            self._news_req_to_symbol[request_id] = symbol
            self.reqMktData(
                request_id,
                self._contracts[symbol],
                generic_ticks,
                False,
                False,
                [],
            )
            self.log.info(
                "Subscribed to IBKR news for %s using %s", symbol, provider_codes
            )

        if self.settings.ibkr_broadtape_enabled:
            broadtape_provider_codes = self._selected_broadtape_provider_codes()
            unsupported = tuple(
                code for code in self.settings.ibkr_broadtape_provider_codes
                if code != "AUTO" and code not in BROADTAPE_NEWS_CONTRACTS
            )
            if unsupported:
                self.log.warning(
                    "IBKR BroadTape provider code(s) not mapped and skipped: %s",
                    unsupported,
                )
            if not broadtape_provider_codes:
                self.log.warning(
                    "IBKR BroadTape mode is enabled but no mapped/available BroadTape provider codes were selected."
                )
            for provider_code in broadtape_provider_codes:
                request_id = next(self._request_ids)
                self._broadtape_req_to_provider[request_id] = provider_code
                self.reqMktData(
                    request_id,
                    self._broadtape_contract(provider_code),
                    "mdoff,292",
                    False,
                    False,
                    [],
                )
                self.log.info("Subscribed to IBKR BroadTape news for %s", provider_code)


    def marketDataType(self, reqId: int, marketDataType: int) -> None:  # noqa: N802
        symbol = self._market_req_to_symbol.get(reqId)
        if symbol is not None:
            self.prices.set_market_data_type(symbol, int(marketDataType))

    def tickPrice(self, reqId, tickType, price, attrib):  # noqa: N802
        symbol = self._market_req_to_symbol.get(reqId)
        if symbol is None or price is None or price <= 0:
            return
        name = _tick_type_name(tickType)
        field_map = {
            "BID": "bid",
            "ASK": "ask",
            "LAST": "last",
            "CLOSE": "close",
            "DELAYED_BID": "bid",
            "DELAYED_ASK": "ask",
            "DELAYED_LAST": "last",
            "DELAYED_CLOSE": "close",
        }
        field = field_map.get(name)
        if field:
            self.prices.update_price(symbol, field, float(price))

    def tickSize(self, reqId, tickType, size):  # noqa: N802
        symbol = self._market_req_to_symbol.get(reqId)
        if symbol is None:
            return
        name = _tick_type_name(tickType)
        if name in {"VOLUME", "DELAYED_VOLUME"}:
            try:
                self.prices.update_volume(symbol, float(size))
            except (TypeError, ValueError):
                pass

    def tickNews(  # noqa: N802
        self,
        tickerId,
        timeStamp,
        providerCode,
        articleId,
        headline,
        extraData,
    ):
        broadtape_provider = self._broadtape_req_to_provider.get(tickerId)
        symbol = (
            f"BROADTAPE:{broadtape_provider}"
            if broadtape_provider
            else self._news_req_to_symbol.get(tickerId, "UNKNOWN")
        )
        received = datetime.now(UTC)
        provider_published_at = None
        try:
            published = datetime.fromtimestamp(int(timeStamp), tz=UTC)
            provider_published_at = published
        except (TypeError, ValueError, OSError):
            published = received
        con_id = None
        contract = self._contracts.get(symbol)
        if contract is not None:
            con_id = int(contract.conId)

        original_article_id = str(articleId or "").strip()
        if original_article_id:
            stable_article_id = original_article_id
        else:
            headline_key = hashlib.sha256(str(headline).encode("utf-8")).hexdigest()[:16]
            stable_article_id = f"synthetic:{int(published.timestamp())}:{headline_key}"

        if broadtape_provider:
            extra_data_value = json.dumps(
                {
                    "source_system": "ibkr",
                    "ingest_mode": "live",
                    "route_type": "broadtape",
                    "source_mode": "ibkr_broadtape",
                    "provider_code": str(providerCode),
                    "original_extra_data": str(extraData or ""),
                },
                ensure_ascii=False,
            )
        else:
            extra_data_value = str(extraData or "")

        item = NewsInput(
            provider=str(providerCode),
            article_id=stable_article_id,
            headline=str(headline),
            published_at=published,
            received_at=received,
            primary_symbol=symbol,
            con_id=con_id,
            extra_data=extra_data_value,
            provider_published_at=provider_published_at,
            first_seen_at=received,
        )
        decision = self.dedup.ingest(item)
        if decision.is_duplicate:
            self.log.info(
                "Duplicate suppressed [%s %.1f]: %s",
                decision.reason,
                decision.similarity or 0,
                headline,
            )
            if decision.symbol_added and symbol != "UNKNOWN":
                if not symbol.startswith("BROADTAPE:"):
                    self.reactions.register_event(
                        decision.canonical_news_id, received, (symbol,)
                    )
            return

        if broadtape_provider:
            tracked_symbols = tuple(dict.fromkeys(self.settings.benchmark_symbols))
        else:
            tracked_symbols = tuple(
                dict.fromkeys((symbol, *self.settings.benchmark_symbols))
            )
        self.reactions.register_event(
            decision.canonical_news_id,
            received,
            tracked_symbols,
        )
        self.log.info("Stored news #%s [%s] %s", decision.canonical_news_id, symbol, headline)
        self._maybe_store_emergency_breaking_signal(
            headline=str(headline),
            published_at=provider_published_at or published,
            symbol=symbol,
            provider_code=str(providerCode),
            article_id=stable_article_id,
            news_id=decision.canonical_news_id,
        )

        if self.settings.fetch_article_body and original_article_id:
            request_id = next(self._request_ids)
            self._article_req_to_news_id[request_id] = decision.canonical_news_id
            self.reqNewsArticle(request_id, providerCode, original_article_id, [])

    def _maybe_store_emergency_breaking_signal(
        self,
        *,
        headline: str,
        published_at: datetime,
        symbol: str,
        provider_code: str,
        article_id: str,
        news_id: int,
    ) -> None:
        try:
            urgency = classify_breaking_news_urgency(headline)
            if urgency.get("urgency") not in {"critical", "high"}:
                return
            signal = build_breaking_news_signal(
                self.db,
                headline=headline,
                published_at=published_at.isoformat(),
                symbols=(symbol,) if symbol and symbol != "UNKNOWN" else (),
                source_label=f"ibkr:{provider_code}",
                url="",
            )
            raw = dict(signal.raw)
            raw.update({"news_article_id": news_id, "article_id": article_id, "provider_code": provider_code})
            signal = signal.__class__(
                source=signal.source,
                external_id=signal.external_id,
                title=signal.title,
                url=signal.url,
                published_at=signal.published_at,
                region=signal.region,
                theme=signal.theme,
                sentiment_score=signal.sentiment_score,
                sentiment_label=signal.sentiment_label,
                confidence=signal.confidence,
                assets=signal.assets,
                asset_map_reason=signal.asset_map_reason,
                market_reaction_status=signal.market_reaction_status,
                market_reaction=signal.market_reaction,
                prediction_gate_status=signal.prediction_gate_status,
                prediction_gate_reason=signal.prediction_gate_reason,
                raw=raw,
                ingestion_version=signal.ingestion_version,
            )
            signal_id, created = self.db.upsert_external_market_signal(signal)
            action = "created" if created else "updated"
            self.log.warning(
                "Emergency breaking-news signal %s #%s [%s/%s] %s",
                action,
                signal_id,
                signal.region,
                signal.theme,
                headline,
            )
        except Exception as exc:
            self.log.warning("Failed to store emergency breaking-news signal: %s", exc)

    def newsArticle(self, requestId, articleType, articleText):  # noqa: N802
        news_id = self._article_req_to_news_id.pop(requestId, None)
        if news_id is not None:
            self.db.update_article_body(news_id, str(articleText))
            self.log.info("Stored article body for news #%s", news_id)

    def error(self, reqId, *args):
        # Compatible with older and newer ibapi error callback signatures.
        code = args[0] if args and isinstance(args[0], int) else None
        message = " ".join(str(item) for item in args)
        connection_loss_codes = {
            1100,  # Connectivity between IB and TWS/Gateway has been lost.
            1101,  # Connectivity restored, data lost; resubscribe.
            1300,  # Socket port has been reset.
            502, 504, 507,  # Socket/connectivity errors.
        }
        informational = {
            2104,  # Market data farm connection is OK
            2106,  # HMDS data farm connection is OK
            2107,  # HMDS data farm is inactive but available on demand
            2108,  # Market data farm is inactive but available on demand
            2158,  # Sec-def data farm connection is OK
        }
        if code in connection_loss_codes:
            self.log.warning("IBKR connection event reqId=%s details=%s", reqId, args)
            if self.isConnected():
                try:
                    self.disconnect()
                except Exception:
                    self.log.exception("Failed to disconnect stale IBKR session after connection event")
            self._schedule_reconnect(f"ibkr_error_{code}", immediate=True)
            self._write_connection_status(status="disconnected", event=f"ibkr_error_{code}", error=message)
        elif code in informational:
            self.log.info("IBKR status reqId=%s details=%s", reqId, args)
        else:
            self.log.warning("IBKR message reqId=%s details=%s", reqId, args)

    def connectionClosed(self) -> None:  # noqa: N802
        self.log.warning("IBKR connection closed; scheduling reconnect")
        self._schedule_reconnect("connection_closed", immediate=True)
        self._write_connection_status(status="disconnected", event="connection_closed")

    def start(self) -> None:
        self._reaction_thread = threading.Thread(
            target=self._reaction_loop, daemon=True
        )
        self._reaction_thread.start()
        if self.settings.ibkr_reconnect_enabled:
            self._write_connection_status(status="starting", event="startup")
            self._reconnect_thread = threading.Thread(
                target=self._connection_watchdog_loop,
                daemon=True,
                name="ibkr-reconnect-watchdog",
            )
            self._reconnect_thread.start()
            self._schedule_reconnect("startup", immediate=True)
            return
        self._connect_once("startup")

    def _reaction_loop(self) -> None:
        while not self._stop.wait(self.settings.reaction_poll_seconds):
            try:
                completed = self.reactions.process_due()
                if completed:
                    self.log.info("Completed %s reaction measurements", completed)
            except Exception:
                self.log.exception("Reaction loop failure")

    def stop(self) -> None:
        self._stop.set()
        if self.isConnected():
            self.disconnect()
        self._write_connection_status(status="stopped", event="stop")
        self.log.info("Recorder stopped")

    def wait_forever(self) -> None:
        try:
            while not self._stop.is_set():
                time.sleep(1)
        except KeyboardInterrupt:
            self.stop()
