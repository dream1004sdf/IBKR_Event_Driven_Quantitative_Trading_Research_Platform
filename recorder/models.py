from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass(frozen=True)
class QuoteSnapshot:
    symbol: str
    timestamp: datetime
    bid: Optional[float] = None
    ask: Optional[float] = None
    last: Optional[float] = None
    close: Optional[float] = None
    volume: Optional[float] = None
    currency: str = "unknown"
    con_id: Optional[int] = None
    contract_symbol: str = ""
    sec_type: str = ""
    exchange: str = ""
    primary_exchange: str = ""
    multiplier: str = ""
    market_data_type: Optional[int] = None
    bid_timestamp: Optional[datetime] = None
    ask_timestamp: Optional[datetime] = None
    last_timestamp: Optional[datetime] = None
    close_timestamp: Optional[datetime] = None

    @property
    def midpoint(self) -> Optional[float]:
        if (
            self.bid is not None
            and self.ask is not None
            and self.bid > 0
            and self.ask > 0
            and self.ask >= self.bid
        ):
            return (self.bid + self.ask) / 2.0
        return None

    def selected_price_with_time(
        self,
    ) -> tuple[Optional[float], Optional[str], Optional[datetime]]:
        candidates: list[tuple[datetime, float, str]] = []
        fallback_time = self.timestamp

        if self.last is not None and self.last > 0:
            candidates.append((self.last_timestamp or fallback_time, self.last, "last"))

        midpoint = self.midpoint
        if midpoint is not None:
            bid_time = self.bid_timestamp or fallback_time
            ask_time = self.ask_timestamp or fallback_time
            if abs((bid_time - ask_time).total_seconds()) <= 30:
                candidates.append((max(bid_time, ask_time), midpoint, "midpoint"))

        if not candidates and self.close is not None and self.close > 0:
            candidates.append((self.close_timestamp or fallback_time, self.close, "close"))

        if not candidates:
            return None, None, None
        selected_time, selected_price, selected_source = max(
            candidates, key=lambda item: item[0]
        )
        return selected_price, selected_source, selected_time

    def selected_price(self) -> tuple[Optional[float], Optional[str]]:
        price, source, _ = self.selected_price_with_time()
        return price, source


@dataclass(frozen=True)
class NewsInput:
    provider: str
    article_id: str
    headline: str
    published_at: datetime
    received_at: datetime
    primary_symbol: str
    con_id: Optional[int] = None
    extra_data: str = ""
    # Provenance timestamps are optional because some providers only deliver an
    # article at receipt time.  ``published_at`` remains the legacy-compatible
    # field; these fields state what is actually known about its availability.
    provider_published_at: Optional[datetime] = None
    source_accepted_at: Optional[datetime] = None
    first_seen_at: Optional[datetime] = None
    content_updated_at: Optional[datetime] = None


@dataclass(frozen=True)
class DedupDecision:
    canonical_news_id: int
    is_duplicate: bool
    reason: Optional[str] = None
    similarity: Optional[float] = None
    symbol_added: bool = False
