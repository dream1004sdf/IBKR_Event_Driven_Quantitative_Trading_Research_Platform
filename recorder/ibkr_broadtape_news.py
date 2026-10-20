from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ibapi.client import EClient
from ibapi.contract import Contract
from ibapi.wrapper import EWrapper

from .config import Settings
from .database import Database
from .dedup import DedupConfig, NewsDeduplicator
from .ibkr_app import BROADTAPE_NEWS_CONTRACTS
from .models import NewsInput

UTC = timezone.utc


def _redact_error(value: object) -> str:
    text = str(value)
    for marker in ("token=", "api_key=", "apikey=", "password="):
        idx = text.lower().find(marker)
        if idx >= 0:
            end = text.find(" ", idx)
            if end < 0:
                end = len(text)
            text = text[: idx + len(marker)] + "***" + text[end:]
    return text


def _is_informational_ibkr_code(code: int | None) -> bool:
    return code in {
        2104,  # Market data farm connection is OK
        2106,  # HMDS data farm connection is OK
        2107,  # HMDS data farm is inactive but available on demand
        2108,  # Market data farm is inactive but available on demand
        2119,  # Market data farm is connecting
        2158,  # Sec-def data farm connection is OK
    }


def _broadtape_contract(provider_code: str) -> Contract:
    symbol, exchange = BROADTAPE_NEWS_CONTRACTS[provider_code]
    contract = Contract()
    contract.symbol = symbol
    contract.secType = "NEWS"
    contract.exchange = exchange
    return contract


@dataclass(frozen=True)
class BroadTapeProbeResult:
    requested_provider_codes: tuple[str, ...]
    returned_providers: dict[str, str]
    selected_provider_codes: tuple[str, ...]
    unsupported_provider_codes: tuple[str, ...]
    subscribed_provider_codes: tuple[str, ...]
    tick_count: int
    rows_created: int
    duplicates: int
    headlines: tuple[dict[str, Any], ...]
    errors: tuple[str, ...]
    timed_out: bool
    duration_seconds: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "requested_provider_codes": list(self.requested_provider_codes),
            "returned_providers": self.returned_providers,
            "selected_provider_codes": list(self.selected_provider_codes),
            "unsupported_provider_codes": list(self.unsupported_provider_codes),
            "subscribed_provider_codes": list(self.subscribed_provider_codes),
            "tick_count": self.tick_count,
            "rows_created": self.rows_created,
            "duplicates": self.duplicates,
            "headlines": list(self.headlines),
            "errors": list(self.errors),
            "timed_out": self.timed_out,
            "duration_seconds": self.duration_seconds,
            "interpretation": self.interpretation(),
            "safety": {
                "read_only_market_data_subscription": True,
                "orders_submitted": False,
                "transmit_enabled": False,
            },
        }

    def interpretation(self) -> str:
        if self.errors and not self.subscribed_provider_codes:
            return "subscription_failed_or_not_entitled"
        if self.subscribed_provider_codes and self.tick_count > 0:
            return "broadtape_live_ticks_received"
        if self.subscribed_provider_codes and not self.errors:
            return "subscription_attempted_no_headlines_observed"
        if self.unsupported_provider_codes and not self.selected_provider_codes:
            return "provider_codes_not_broadtape_mapped"
        return "no_supported_broadtape_provider_selected"


class IBKRBroadTapeNewsProbe(EWrapper, EClient):
    """Read-only IBKR BroadTape NEWS contract probe.

    This is intentionally separate from the always-on recorder so BroadTape
    entitlement and feed behavior can be checked without changing order routing
    or forcing the main process to restart.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        provider_codes: tuple[str, ...],
        seconds: float = 45.0,
        save_to_db: bool = False,
        client_id: int | None = None,
        headline_limit: int = 25,
    ) -> None:
        EClient.__init__(self, self)
        self.settings = settings
        self.provider_codes = tuple(dict.fromkeys(code.strip().upper() for code in provider_codes if code.strip()))
        self.seconds = max(1.0, float(seconds))
        self.save_to_db = bool(save_to_db)
        self.client_id = settings.client_id + 2600 if client_id is None else int(client_id)
        self.headline_limit = max(1, int(headline_limit))

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

        self._providers: dict[str, str] = {}
        self._providers_received = False
        self._next_valid_received = False
        self._subscription_started = False
        self._request_ids = iter(range(860000, 861000))
        self._req_to_provider: dict[int, str] = {}
        self._subscribed_provider_codes: list[str] = []
        self._errors: list[str] = []
        self._headlines: list[dict[str, Any]] = []
        self._tick_count = 0
        self._rows_created = 0
        self._duplicates = 0
        self._api_thread: threading.Thread | None = None

    def nextValidId(self, orderId: int) -> None:  # noqa: N802
        self.log.info("IBKR BroadTape probe connected; order id %s is unused", orderId)
        self._next_valid_received = True
        self.reqNewsProviders()
        self._try_start_subscriptions()

    def newsProviders(self, newsProviders):  # noqa: N802
        self._providers = {provider.code: provider.name for provider in newsProviders}
        self._providers_received = True
        for code, name in self._providers.items():
            self.db.upsert_provider(code, name)
        self.log.info("IBKR news providers returned for BroadTape probe: %s", self._providers or "none")
        self._try_start_subscriptions()

    def _selected_provider_codes(self) -> tuple[str, ...]:
        if self.provider_codes == ("AUTO",):
            return tuple(code for code in BROADTAPE_NEWS_CONTRACTS if not self._providers or code in self._providers)
        return tuple(
            code for code in self.provider_codes
            if code in BROADTAPE_NEWS_CONTRACTS and (not self._providers or code in self._providers)
        )

    def _unsupported_provider_codes(self) -> tuple[str, ...]:
        if self.provider_codes == ("AUTO",):
            return ()
        return tuple(code for code in self.provider_codes if code not in BROADTAPE_NEWS_CONTRACTS)

    def _try_start_subscriptions(self) -> None:
        if self._subscription_started:
            return
        if not self._next_valid_received or not self._providers_received:
            return
        self._subscription_started = True
        for provider_code in self._selected_provider_codes():
            req_id = next(self._request_ids)
            self._req_to_provider[req_id] = provider_code
            self._subscribed_provider_codes.append(provider_code)
            self.reqMktData(req_id, _broadtape_contract(provider_code), "mdoff,292", False, False, [])
            self.log.info("BroadTape probe subscribed to %s", provider_code)

    def tickNews(  # noqa: N802
        self,
        tickerId,
        timeStamp,
        providerCode,
        articleId,
        headline,
        extraData,
    ) -> None:
        provider = str(providerCode or self._req_to_provider.get(int(tickerId), "UNKNOWN"))
        received = datetime.now(UTC)
        try:
            published = datetime.fromtimestamp(int(timeStamp), tz=UTC)
            provider_published_at = published
        except (TypeError, ValueError, OSError):
            published = received
            provider_published_at = None

        original_article_id = str(articleId or "").strip()
        if original_article_id:
            stable_article_id = original_article_id
        else:
            headline_key = hashlib.sha256(str(headline).encode("utf-8")).hexdigest()[:16]
            stable_article_id = f"broadtape:{provider}:{int(published.timestamp())}:{headline_key}"

        headline_row = {
            "provider": provider,
            "article_id": stable_article_id,
            "headline": str(headline),
            "published_at": published.isoformat(),
            "received_at": received.isoformat(),
            "primary_symbol": f"BROADTAPE:{provider}",
            "extra_data": str(extraData or ""),
            "asset_mapping_deferred": True,
        }
        self._tick_count += 1
        if len(self._headlines) < self.headline_limit:
            self._headlines.append(headline_row)

        if not self.save_to_db:
            return

        item = NewsInput(
            provider=provider,
            article_id=stable_article_id,
            headline=str(headline),
            published_at=published,
            received_at=received,
            primary_symbol=f"BROADTAPE:{provider}",
            con_id=None,
            extra_data=json.dumps(
                {
                    "source_system": "ibkr",
                    "ingest_mode": "live",
                    "route_type": "broadtape",
                    "source_mode": "ibkr_broadtape",
                    "provider_code": provider,
                    "original_extra_data": str(extraData or ""),
                    "request_provider": self._req_to_provider.get(int(tickerId), ""),
                    "asset_mapping_deferred": True,
                },
                ensure_ascii=False,
            ),
            provider_published_at=provider_published_at,
            first_seen_at=received,
        )
        decision = self.dedup.ingest(item)
        if decision.is_duplicate:
            self._duplicates += 1
        else:
            self._rows_created += 1

    def error(self, reqId, *args) -> None:
        code = args[0] if args and isinstance(args[0], int) else None
        if _is_informational_ibkr_code(code):
            return
        self._errors.append(_redact_error(f"IBKR message reqId={reqId} details={args}"))

    def run_probe(self) -> BroadTapeProbeResult:
        started = time.monotonic()
        timed_out = False
        try:
            self.connect(self.settings.host, self.settings.port, clientId=self.client_id)
            self._api_thread = threading.Thread(target=self.run, daemon=True)
            self._api_thread.start()
            time.sleep(self.seconds)
        except Exception as exc:
            self._errors.append(_redact_error(exc))
        finally:
            if self.isConnected():
                self.disconnect()
            if self._api_thread is not None and self._api_thread.is_alive():
                time.sleep(0.1)
        duration = time.monotonic() - started
        if duration + 0.5 < self.seconds:
            timed_out = False
        return BroadTapeProbeResult(
            requested_provider_codes=self.provider_codes,
            returned_providers=dict(self._providers),
            selected_provider_codes=self._selected_provider_codes(),
            unsupported_provider_codes=self._unsupported_provider_codes(),
            subscribed_provider_codes=tuple(dict.fromkeys(self._subscribed_provider_codes)),
            tick_count=self._tick_count,
            rows_created=self._rows_created,
            duplicates=self._duplicates,
            headlines=tuple(self._headlines),
            errors=tuple(dict.fromkeys(self._errors)),
            timed_out=timed_out,
            duration_seconds=round(duration, 3),
        )
