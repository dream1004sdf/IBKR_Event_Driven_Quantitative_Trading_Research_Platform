from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Optional

from .models import QuoteSnapshot

UTC = timezone.utc


class PriceCache:
    def __init__(self):
        self._lock = threading.RLock()
        self._data: dict[str, dict[str, float | int | str | datetime | None]] = {}

    def set_contract_metadata(
        self,
        symbol: str,
        *,
        con_id: int | None = None,
        contract_symbol: str | None = None,
        sec_type: str | None = None,
        exchange: str | None = None,
        primary_exchange: str | None = None,
        currency: str | None = None,
        multiplier: str | None = None,
        market_data_type: int | None = None,
    ) -> None:
        with self._lock:
            row = self._data.setdefault(symbol, {})
            if con_id is not None:
                row["con_id"] = int(con_id)
            if contract_symbol:
                row["contract_symbol"] = str(contract_symbol).upper()
            if sec_type:
                row["sec_type"] = str(sec_type).upper()
            if exchange:
                row["exchange"] = str(exchange).upper()
            if primary_exchange:
                row["primary_exchange"] = str(primary_exchange).upper()
            if currency:
                row["currency"] = str(currency).upper()
            if multiplier is not None:
                row["multiplier"] = str(multiplier)
            if market_data_type is not None:
                row["market_data_type"] = int(market_data_type)

    def set_market_data_type(self, symbol: str, market_data_type: int) -> None:
        with self._lock:
            row = self._data.setdefault(symbol, {})
            row["market_data_type"] = int(market_data_type)

    def update_price(self, symbol: str, field: str, value: float) -> None:
        if value is None or value <= 0:
            return
        with self._lock:
            row = self._data.setdefault(symbol, {})
            now = datetime.now(UTC)
            row[field] = float(value)
            row[f"{field}_timestamp"] = now
            row["timestamp"] = now

    def update_volume(self, symbol: str, value: float) -> None:
        if value is None or value < 0:
            return
        with self._lock:
            row = self._data.setdefault(symbol, {})
            row["volume"] = float(value)

    def put_snapshot(self, quote: QuoteSnapshot) -> None:
        with self._lock:
            self._data[quote.symbol] = {
                "bid": quote.bid,
                "ask": quote.ask,
                "last": quote.last,
                "close": quote.close,
                "volume": quote.volume,
                "currency": quote.currency,
                "con_id": quote.con_id,
                "contract_symbol": quote.contract_symbol,
                "sec_type": quote.sec_type,
                "exchange": quote.exchange,
                "primary_exchange": quote.primary_exchange,
                "multiplier": quote.multiplier,
                "market_data_type": quote.market_data_type,
                "timestamp": quote.timestamp,
                "bid_timestamp": quote.bid_timestamp or quote.timestamp,
                "ask_timestamp": quote.ask_timestamp or quote.timestamp,
                "last_timestamp": quote.last_timestamp or quote.timestamp,
                "close_timestamp": quote.close_timestamp or quote.timestamp,
            }

    def get(self, symbol: str) -> Optional[QuoteSnapshot]:
        with self._lock:
            row = self._data.get(symbol)
            if not row:
                return None
            return QuoteSnapshot(
                symbol=symbol,
                timestamp=row.get("timestamp") or datetime.now(UTC),
                bid=row.get("bid"),
                ask=row.get("ask"),
                last=row.get("last"),
                close=row.get("close"),
                volume=row.get("volume"),
                currency=str(row.get("currency") or "unknown"),
                con_id=(int(row["con_id"]) if row.get("con_id") is not None else None),
                contract_symbol=str(row.get("contract_symbol") or ""),
                sec_type=str(row.get("sec_type") or ""),
                exchange=str(row.get("exchange") or ""),
                primary_exchange=str(row.get("primary_exchange") or ""),
                multiplier=str(row.get("multiplier") or ""),
                market_data_type=(
                    int(row["market_data_type"])
                    if row.get("market_data_type") is not None
                    else None
                ),
                bid_timestamp=row.get("bid_timestamp"),
                ask_timestamp=row.get("ask_timestamp"),
                last_timestamp=row.get("last_timestamp"),
                close_timestamp=row.get("close_timestamp"),
            )
