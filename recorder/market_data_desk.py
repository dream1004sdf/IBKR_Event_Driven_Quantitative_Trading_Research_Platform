from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from ibapi.client import EClient
from ibapi.contract import Contract
from ibapi.ticktype import TickTypeEnum
from ibapi.wrapper import EWrapper

from .models import QuoteSnapshot

UTC = timezone.utc
MARKET_DATA_DESK_VERSION = "market-data-specialist-desk-v1"
MARKET_DATA_TEMPERATURE_VERSION = "market-data-temperature-v1"


def _tick_type_name(tick_type: int) -> str:
    for method_name in ("toStr", "to_str", "to_Str"):
        method = getattr(TickTypeEnum, method_name, None)
        if method is not None:
            try:
                return str(method(tick_type))
            except Exception:
                pass
    return str(tick_type)


@dataclass(frozen=True)
class ContractCandidate:
    symbol: str = ""
    sec_type: str = ""
    exchange: str = ""
    currency: str = ""
    primary_exchange: str = ""
    multiplier: str = ""
    local_symbol: str = ""
    trading_class: str = ""
    label: str = ""
    invert_price: bool = False


@dataclass(frozen=True)
class MarketInstrument:
    key: str
    label: str
    desk_group: str
    symbol: str
    sec_type: str
    exchange: str
    currency: str
    primary_exchange: str = ""
    multiplier: str = ""
    local_symbol: str = ""
    trading_class: str = ""
    alternatives: tuple[ContractCandidate, ...] = ()


@dataclass
class MarketDataState:
    instrument: MarketInstrument
    status: str = "pending"
    error: str = ""
    market_data_type: int | None = None
    delayed: bool = False
    bid: float | None = None
    ask: float | None = None
    last: float | None = None
    close: float | None = None
    volume: float | None = None
    timestamp: datetime | None = None
    con_id: int | None = None
    contract_symbol: str = ""
    selected_price: float | None = None
    selected_source: str = ""
    first_price: float | None = None
    previous_price: float | None = None
    previous_selected_source: str = ""
    max_tick_move_pct: float = 0.0
    price_tick_count: int = 0
    non_close_price_tick_count: int = 0
    close_tick_count: int = 0
    live_selected_update_count: int = 0
    alerts: list[str] = field(default_factory=list)
    resolved_contract: Contract | None = None
    active_candidate_index: int = 0
    active_candidate_label: str = "base"
    active_candidate_invert_price: bool = False
    attempted_candidate_labels: list[str] = field(default_factory=list)
    candidate_retry_reasons: list[str] = field(default_factory=list)
    subscription_started_monotonic: float | None = None
    last_candidate_retry_monotonic: float | None = None


MARKET_DATA_TYPE_LABELS = {
    1: "real_time",
    2: "frozen",
    3: "delayed",
    4: "delayed_frozen",
}


CORE_US_TRADING_KEYS = {
    "DXY",
    "USDJPY",
    "US02Y",
    "US10Y",
    "VIX",
    "VXX",
    "ES",
    "NQ",
    "YM",
    "RTY",
    "SPY",
    "QQQ",
    "IWM",
    "DIA",
    "WTI",
    "GC",
}

US_TRADING_REFERENCE_KEYS = {
    "USDKRW",
    "USDCNH",
    "GBPUSD",
    "US03M",
    "US30Y",
    "VVIX",
    "KOSPI",
    "KOSPI_FUT",
    "KOSDAQ",
    "NIKKEI",
    "SI",
    "BRENT",
    "BTC",
    "ETH",
    "EWY",
}

REFERENCE_CLOSE_ONLY_KEYS = {"USDCNH", "KOSDAQ", "BRENT", "US03M", "US30Y"}
FACTUAL_ASIA_REFERENCE_MOVE_KEYS = {"KOSPI", "KOSPI_FUT", "KOSDAQ"}


def _market_data_importance(key: str, group: str) -> str:
    key = str(key or "").upper()
    if key in CORE_US_TRADING_KEYS:
        return "core_us_trading"
    if key in US_TRADING_REFERENCE_KEYS or group in {"asia_equity", "account_option_underlying"}:
        return "us_trading_reference"
    return "global_monitoring"


def _market_data_impact_weight(key: str, group: str) -> float:
    importance = _market_data_importance(key, group)
    if importance == "core_us_trading":
        return 1.0
    if importance == "us_trading_reference":
        return 0.30
    return 0.50


def _stale_threshold_seconds(key: str, group: str, *, selected_source_is_close: bool = False) -> float:
    key = str(key or "").upper()
    if selected_source_is_close:
        return 900.0
    if group == "asia_equity":
        return 1800.0
    if group == "rates":
        return 300.0
    if key in {"USDCNH", "USDKRW", "GBPUSD"}:
        return 300.0
    if key in {"BRENT", "BTC", "ETH"}:
        return 300.0
    if _market_data_importance(key, group) == "us_trading_reference":
        return 600.0
    return 90.0


DEFAULT_MARKET_DATA_INSTRUMENTS: tuple[MarketInstrument, ...] = (
    MarketInstrument("DXY", "US Dollar Index proxy / UUP ETF", "fx_macro", "UUP", "STK", "SMART", "USD", primary_exchange="ARCA"),
    MarketInstrument("USDKRW", "USD/KRW", "fx", "USD", "CASH", "IDEALPRO", "KRW", alternatives=(ContractCandidate(local_symbol="USD.KRW", label="idealpro_local_symbol"),)),
    MarketInstrument("USDJPY", "USD/JPY", "fx", "USD", "CASH", "IDEALPRO", "JPY", alternatives=(ContractCandidate(local_symbol="USD.JPY", label="idealpro_local_symbol"),)),
    MarketInstrument("USDCNH", "USD/CNH", "fx", "USD", "CASH", "IDEALPRO", "CNH", alternatives=(ContractCandidate(local_symbol="USD.CNH", label="idealpro_local_symbol"), ContractCandidate(symbol="RMB", sec_type="FUT", exchange="CME", currency="USD", label="rmb_future_proxy_inverted", invert_price=True))),
    MarketInstrument("EURUSD", "EUR/USD", "fx", "EUR", "CASH", "IDEALPRO", "USD", alternatives=(ContractCandidate(local_symbol="EUR.USD", label="idealpro_local_symbol"), ContractCandidate(symbol="M6E", sec_type="FUT", exchange="CME", currency="USD", label="micro_euro_fx_future_proxy"))),
    MarketInstrument("GBPUSD", "GBP/USD", "fx", "GBP", "CASH", "IDEALPRO", "USD", alternatives=(ContractCandidate(local_symbol="GBP.USD", label="idealpro_local_symbol"), ContractCandidate(symbol="M6B", sec_type="FUT", exchange="CME", currency="USD", label="micro_british_pound_fx_future_proxy"))),
    MarketInstrument("US03M", "US 3M yield proxy / IRX", "rates", "IRX", "IND", "CBOE", "USD"),
    MarketInstrument(
        "US02Y",
        "US 2Y Treasury live proxy / UTWO with SHY, 2YY, ZT fallback",
        "rates",
        "UTWO",
        "STK",
        "SMART",
        "USD",
        primary_exchange="ARCA",
        alternatives=(
            ContractCandidate(symbol="SHY", sec_type="STK", exchange="SMART", currency="USD", primary_exchange="NASDAQ", label="shy_1_3y_treasury_etf_proxy"),
            ContractCandidate(symbol="2YY", sec_type="FUT", exchange="CBOT", currency="USD", label="2yy_yield_reference"),
            ContractCandidate(symbol="ZT", sec_type="FUT", exchange="CBOT", currency="USD", label="zt_futures_reference"),
        ),
    ),
    MarketInstrument("US10Y", "US 10Y yield / TNX", "rates", "TNX", "IND", "CBOE", "USD"),
    MarketInstrument("US30Y", "US 30Y yield / TYX", "rates", "TYX", "IND", "CBOE", "USD"),
    MarketInstrument("VIX", "CBOE VIX", "volatility", "VIX", "IND", "CBOE", "USD"),
    MarketInstrument("VVIX", "CBOE VVIX", "volatility", "VVIX", "IND", "CBOE", "USD"),
    MarketInstrument("KOSPI", "KOSPI 200 Index / K200", "asia_equity", "K200", "IND", "KSE", "KRW"),
    MarketInstrument("KOSPI_FUT", "KOSPI 200 Futures front month", "asia_equity", "K200", "FUT", "KSE", "KRW"),
    MarketInstrument("KOSDAQ", "KOSDAQ 150 Index / KOSDQ150", "asia_equity", "KOSDQ150", "IND", "KSE", "KRW"),
    MarketInstrument("NIKKEI", "Nikkei 225 futures front month / NIY", "asia_equity", "NIY", "FUT", "CME", "JPY"),
    MarketInstrument("ES", "E-mini S&P 500 front month", "us_equity_futures", "ES", "FUT", "CME", "USD"),
    MarketInstrument("NQ", "E-mini Nasdaq 100 front month", "us_equity_futures", "NQ", "FUT", "CME", "USD"),
    MarketInstrument("YM", "E-mini Dow front month", "us_equity_futures", "YM", "FUT", "CBOT", "USD"),
    MarketInstrument("RTY", "E-mini Russell 2000 front month", "us_equity_futures", "RTY", "FUT", "CME", "USD"),
    MarketInstrument("SPY", "SPDR S&P 500 ETF Trust", "us_equity_index_etf", "SPY", "STK", "SMART", "USD", primary_exchange="ARCA"),
    MarketInstrument("QQQ", "Invesco QQQ Trust", "us_equity_index_etf", "QQQ", "STK", "SMART", "USD", primary_exchange="NASDAQ"),
    MarketInstrument("IWM", "iShares Russell 2000 ETF", "us_equity_index_etf", "IWM", "STK", "SMART", "USD", primary_exchange="ARCA"),
    MarketInstrument("DIA", "SPDR Dow Jones Industrial Average ETF Trust", "us_equity_index_etf", "DIA", "STK", "SMART", "USD", primary_exchange="ARCA"),
    MarketInstrument("VXX", "iPath Series B S&P 500 VIX Short-Term Futures ETN", "volatility_etf", "VXX", "STK", "SMART", "USD", primary_exchange="ARCA"),
    MarketInstrument("UVXY", "ProShares Ultra VIX Short-Term Futures ETF", "volatility_etf", "UVXY", "STK", "SMART", "USD", primary_exchange="ARCA"),
    MarketInstrument("GC", "Gold futures front month", "commodities", "GC", "FUT", "COMEX", "USD"),
    MarketInstrument("SI", "Silver futures front month", "commodities", "SI", "FUT", "COMEX", "USD"),
    MarketInstrument("WTI", "WTI crude futures front month", "commodities", "CL", "FUT", "NYMEX", "USD"),
    MarketInstrument("BRENT", "Brent crude futures front month", "commodities", "BZ", "FUT", "NYMEX", "USD"),
    MarketInstrument("BTC", "Bitcoin proxy / BITO ETF", "crypto", "BITO", "STK", "SMART", "USD", primary_exchange="ARCA"),
    MarketInstrument("ETH", "Ethereum proxy / ETHA ETF", "crypto", "ETHA", "STK", "SMART", "USD", primary_exchange="NASDAQ"),
)


def _normalise_symbol(value: Any) -> str:
    return str(value or "").strip().upper()


def parse_symbol_csv(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    symbols = []
    for item in str(value).replace(";", ",").split(","):
        symbol = _normalise_symbol(item)
        if symbol and symbol not in symbols:
            symbols.append(symbol)
    return tuple(symbols)


def account_option_underlying_symbols(account_snapshot_path: Path | None) -> tuple[str, ...]:
    if account_snapshot_path is None:
        return ()
    try:
        payload = json.loads(account_snapshot_path.expanduser().read_text(encoding="utf-8"))
    except Exception:
        return ()
    data = payload.get("result") if isinstance(payload.get("result"), dict) else payload
    symbols: list[str] = []
    for row in data.get("positions") or []:
        if not isinstance(row, dict):
            continue
        contract = row.get("contract") if isinstance(row.get("contract"), dict) else row
        if str(contract.get("sec_type") or contract.get("secType") or "").upper() != "OPT":
            continue
        symbol = _normalise_symbol(contract.get("symbol"))
        if symbol and symbol not in symbols:
            symbols.append(symbol)
    return tuple(symbols)


def account_option_underlying_instruments(symbols: tuple[str, ...] | list[str]) -> tuple[MarketInstrument, ...]:
    existing = {_normalise_symbol(item.key) for item in DEFAULT_MARKET_DATA_INSTRUMENTS}
    instruments: list[MarketInstrument] = []
    for symbol in symbols:
        key = _normalise_symbol(symbol)
        if not key or key in existing or any(item.key == key for item in instruments):
            continue
        instruments.append(
            MarketInstrument(
                key=key,
                label=f"{key} account option underlying",
                desk_group="account_option_underlying",
                symbol=key,
                sec_type="STK",
                exchange="SMART",
                currency="USD",
            )
        )
    return tuple(instruments)


def market_data_instruments_with_account_option_underlyings(
    *,
    extra_stock_symbols: str | tuple[str, ...] | list[str] | None = None,
    account_snapshot_path: Path | None = None,
) -> tuple[MarketInstrument, ...]:
    if isinstance(extra_stock_symbols, str) or extra_stock_symbols is None:
        extra_symbols = list(parse_symbol_csv(extra_stock_symbols))
    else:
        extra_symbols = [_normalise_symbol(item) for item in extra_stock_symbols if _normalise_symbol(item)]
    for symbol in account_option_underlying_symbols(account_snapshot_path):
        if symbol not in extra_symbols:
            extra_symbols.append(symbol)
    return DEFAULT_MARKET_DATA_INSTRUMENTS + account_option_underlying_instruments(tuple(extra_symbols))


def _candidate_for_instrument(instrument: MarketInstrument, candidate_index: int = 0) -> ContractCandidate:
    if candidate_index <= 0:
        return ContractCandidate(label="base")
    try:
        return instrument.alternatives[candidate_index - 1]
    except IndexError:
        raise ValueError(f"No contract candidate {candidate_index} for {instrument.key}") from None


def contract_candidate_count(instrument: MarketInstrument) -> int:
    return 1 + len(instrument.alternatives)


def contract_candidate_label(instrument: MarketInstrument, candidate_index: int = 0) -> str:
    candidate = _candidate_for_instrument(instrument, candidate_index)
    return candidate.label or ("base" if candidate_index == 0 else f"candidate_{candidate_index}")


def contract_candidate_inverts_price(instrument: MarketInstrument, candidate_index: int = 0) -> bool:
    return bool(_candidate_for_instrument(instrument, candidate_index).invert_price)


def contract_for_instrument(instrument: MarketInstrument, *, candidate_index: int = 0) -> Contract:
    candidate = _candidate_for_instrument(instrument, candidate_index)
    contract = Contract()
    contract.symbol = candidate.symbol or instrument.symbol
    contract.secType = candidate.sec_type or instrument.sec_type
    contract.exchange = candidate.exchange or instrument.exchange
    contract.currency = candidate.currency or instrument.currency
    primary_exchange = candidate.primary_exchange or instrument.primary_exchange
    multiplier = candidate.multiplier or instrument.multiplier
    local_symbol = candidate.local_symbol or instrument.local_symbol
    trading_class = candidate.trading_class or instrument.trading_class
    if primary_exchange:
        contract.primaryExchange = primary_exchange
    if multiplier:
        contract.multiplier = multiplier
    if local_symbol:
        contract.localSymbol = local_symbol
    if trading_class:
        contract.tradingClass = trading_class
    return contract


def _contract_expiry_key(contract: Contract) -> tuple[str, str]:
    expiry = str(getattr(contract, "lastTradeDateOrContractMonth", "") or "99999999")
    return (expiry, str(getattr(contract, "localSymbol", "") or getattr(contract, "symbol", "") or ""))


def quote_from_state(state: MarketDataState) -> QuoteSnapshot:
    now = state.timestamp or datetime.now(UTC)
    return QuoteSnapshot(
        symbol=state.instrument.key,
        timestamp=now,
        bid=state.bid,
        ask=state.ask,
        last=state.last,
        close=state.close,
        volume=state.volume,
        currency=state.instrument.currency,
        con_id=state.con_id,
        contract_symbol=state.contract_symbol or state.instrument.symbol,
        sec_type=state.instrument.sec_type,
        exchange=state.instrument.exchange,
        primary_exchange=state.instrument.primary_exchange,
        multiplier=state.instrument.multiplier,
        market_data_type=state.market_data_type,
        bid_timestamp=now if state.bid is not None else None,
        ask_timestamp=now if state.ask is not None else None,
        last_timestamp=now if state.last is not None else None,
        close_timestamp=now if state.close is not None else None,
    )


def refresh_selected_price(state: MarketDataState) -> None:
    price, source, _ = quote_from_state(state).selected_price_with_time()
    state.selected_price = price
    state.selected_source = source or ""


def _pct_change(price: Any, reference: Any) -> float | None:
    try:
        price_value = float(price)
        reference_value = float(reference)
    except (TypeError, ValueError):
        return None
    if reference_value <= 0:
        return None
    return (price_value / reference_value - 1.0) * 100.0


def _quote_spread_pct(bid: Any, ask: Any) -> float | None:
    try:
        bid_value = float(bid)
        ask_value = float(ask)
    except (TypeError, ValueError):
        return None
    if bid_value <= 0 or ask_value <= 0 or ask_value < bid_value:
        return None
    midpoint = (bid_value + ask_value) / 2.0
    if midpoint <= 0:
        return None
    return (ask_value - bid_value) / midpoint * 100.0


def _invert_price(value: float | None) -> float | None:
    if value is None or value <= 0:
        return None
    return 1.0 / value


def update_alerts(
    state: MarketDataState,
    *,
    gap_threshold_pct: float = 1.0,
    tick_move_threshold_pct: float = 0.5,
) -> None:
    alerts: list[str] = []
    if state.market_data_type in {3, 4} or state.delayed:
        alerts.append("delayed_market_data")
    spread_pct = _quote_spread_pct(state.bid, state.ask)
    if spread_pct is not None and spread_pct >= 0.5:
        alerts.append(f"wide_spread:{spread_pct:.2f}%")
    if state.selected_price is not None and state.close is not None and state.close > 0:
        gap_pct = (state.selected_price / state.close - 1.0) * 100.0
        if abs(gap_pct) >= gap_threshold_pct:
            direction = "gap_up" if gap_pct > 0 else "gap_down"
            alerts.append(f"{direction}:{gap_pct:+.2f}%")
    if state.max_tick_move_pct >= tick_move_threshold_pct:
        alerts.append(f"sudden_move:{state.max_tick_move_pct:.2f}%")
    state.alerts = alerts


class MarketDataDeskApp(EWrapper, EClient):
    def __init__(
        self,
        *,
        instruments: tuple[MarketInstrument, ...] = DEFAULT_MARKET_DATA_INSTRUMENTS,
        host: str = "127.0.0.1",
        port: int = 4001,
        client_id: int = 4021,
        requested_market_data_type: int = 1,
        timeout_seconds: float = 20.0,
        gap_threshold_pct: float = 1.0,
        tick_move_threshold_pct: float = 0.5,
        fallback_after_seconds: float = 8.0,
        fallback_on_close_only: bool = False,
    ) -> None:
        EClient.__init__(self, self)
        self.instruments = instruments
        self.host = host
        self.port = int(port)
        self.client_id = int(client_id)
        self.requested_market_data_type = int(requested_market_data_type)
        self.timeout_seconds = float(timeout_seconds)
        self.gap_threshold_pct = float(gap_threshold_pct)
        self.tick_move_threshold_pct = float(tick_move_threshold_pct)
        self.fallback_after_seconds = float(fallback_after_seconds)
        self.fallback_on_close_only = bool(fallback_on_close_only)
        self.states = {instrument.key: MarketDataState(instrument=instrument) for instrument in instruments}
        self._contract_req_to_key: dict[int, str] = {}
        self._contract_req_to_candidate: dict[int, int] = {}
        self._market_req_to_key: dict[int, str] = {}
        self._market_req_to_candidate: dict[int, int] = {}
        self._active_market_req_by_key: dict[str, int] = {}
        self._request_id = 9000
        self._api_thread: threading.Thread | None = None
        self._connected = threading.Event()
        self._lock = threading.RLock()

    def _next_req_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def _request_contract_details_for_candidate(self, key: str, candidate_index: int, *, reason: str = "initial") -> None:
        state = self.states[key]
        request_id = self._next_req_id()
        label = contract_candidate_label(state.instrument, candidate_index)
        state.status = "contract_pending"
        state.error = ""
        state.resolved_contract = None
        state.active_candidate_index = candidate_index
        state.active_candidate_label = label
        state.active_candidate_invert_price = contract_candidate_inverts_price(state.instrument, candidate_index)
        if label not in state.attempted_candidate_labels:
            state.attempted_candidate_labels.append(label)
        state.candidate_retry_reasons.append(f"{label}:{reason}")
        self._contract_req_to_key[request_id] = key
        self._contract_req_to_candidate[request_id] = candidate_index
        self.reqContractDetails(request_id, contract_for_instrument(state.instrument, candidate_index=candidate_index))

    def _request_next_candidate_if_available(self, key: str, *, reason: str) -> bool:
        state = self.states[key]
        next_candidate = int(state.active_candidate_index) + 1
        if next_candidate >= contract_candidate_count(state.instrument):
            return False
        old_req = self._active_market_req_by_key.pop(key, None)
        if old_req is not None:
            try:
                self.cancelMktData(old_req)
            except Exception:
                pass
            self._market_req_to_key.pop(old_req, None)
            self._market_req_to_candidate.pop(old_req, None)
        state.bid = None
        state.ask = None
        state.last = None
        state.close = None
        state.volume = None
        state.market_data_type = None
        state.delayed = False
        state.timestamp = None
        state.selected_price = None
        state.selected_source = ""
        state.first_price = None
        state.previous_price = None
        state.previous_selected_source = ""
        state.max_tick_move_pct = 0.0
        state.price_tick_count = 0
        state.non_close_price_tick_count = 0
        state.close_tick_count = 0
        state.live_selected_update_count = 0
        state.alerts = []
        state.last_candidate_retry_monotonic = time.monotonic()
        self._request_contract_details_for_candidate(key, next_candidate, reason=reason)
        return True

    def _retry_no_price_subscriptions(self, *, now: float | None = None) -> None:
        now = time.monotonic() if now is None else float(now)
        for key, state in self.states.items():
            if state.status not in {"subscribed", "live"}:
                continue
            if state.subscription_started_monotonic is None:
                continue
            if now - state.subscription_started_monotonic < self.fallback_after_seconds:
                continue
            if state.last_candidate_retry_monotonic is not None and now - state.last_candidate_retry_monotonic < self.fallback_after_seconds:
                continue
            no_price = state.selected_price is None
            close_only = bool(
                self.fallback_on_close_only
                and state.selected_price is not None
                and str(state.selected_source or "").lower() == "close"
                and int(state.non_close_price_tick_count or 0) <= 0
            )
            if not (no_price or close_only):
                continue
            reason = f"no_price_after_{self.fallback_after_seconds:g}s" if no_price else f"close_only_after_{self.fallback_after_seconds:g}s"
            self._request_next_candidate_if_available(key, reason=reason)

    def nextValidId(self, orderId: int) -> None:  # noqa: N802
        self.reqMarketDataType(self.requested_market_data_type)
        self._connected.set()
        for instrument in self.instruments:
            self._request_contract_details_for_candidate(instrument.key, 0, reason="initial")

    def contractDetails(self, reqId, contractDetails):  # noqa: N802
        key = self._contract_req_to_key.get(int(reqId))
        if not key:
            return
        candidate_index = self._contract_req_to_candidate.get(int(reqId), 0)
        contract = contractDetails.contract
        with self._lock:
            state = self.states[key]
            state.con_id = int(getattr(contract, "conId", 0) or 0) or None
            state.contract_symbol = str(getattr(contract, "symbol", "") or state.instrument.symbol)
            if state.resolved_contract is None or _contract_expiry_key(contract) < _contract_expiry_key(state.resolved_contract):
                state.resolved_contract = contract
        state.active_candidate_index = candidate_index
        state.active_candidate_label = contract_candidate_label(state.instrument, candidate_index)
        state.active_candidate_invert_price = contract_candidate_inverts_price(state.instrument, candidate_index)
        if state.status in {"pending", "contract_pending"}:
            state.status = "resolved"

    def contractDetailsEnd(self, reqId: int) -> None:  # noqa: N802
        key = self._contract_req_to_key.get(int(reqId))
        if not key:
            return
        candidate_index = self._contract_req_to_candidate.get(int(reqId), 0)
        with self._lock:
            state = self.states[key]
            if state.status in {"pending", "contract_pending"}:
                state.active_candidate_index = candidate_index
                state.active_candidate_label = contract_candidate_label(state.instrument, candidate_index)
                state.active_candidate_invert_price = contract_candidate_inverts_price(state.instrument, candidate_index)
                if self._request_next_candidate_if_available(key, reason="contract_unresolved"):
                    return
                state.status = "contract_unresolved"
                state.error = "No IBKR contract details returned for any candidate"
                return
            request_id = self._next_req_id()
            self._market_req_to_key[request_id] = key
            self._market_req_to_candidate[request_id] = candidate_index
            self._active_market_req_by_key[key] = request_id
            self.reqMktData(request_id, state.resolved_contract or contract_for_instrument(state.instrument), "", False, False, [])
            state.subscription_started_monotonic = time.monotonic()
            state.status = "subscribed"

    def marketDataType(self, reqId: int, marketDataType: int) -> None:  # noqa: N802
        key = self._market_req_to_key.get(int(reqId))
        if key:
            with self._lock:
                state = self.states[key]
                state.market_data_type = int(marketDataType)
                state.delayed = int(marketDataType) in {3, 4}
                refresh_selected_price(state)
                update_alerts(
                    state,
                    gap_threshold_pct=self.gap_threshold_pct,
                    tick_move_threshold_pct=self.tick_move_threshold_pct,
                )

    def tickPrice(self, reqId, tickType, price, attrib):  # noqa: N802
        key = self._market_req_to_key.get(int(reqId))
        if not key or price is None or price <= 0:
            return
        name = _tick_type_name(int(tickType))
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
        if not field:
            return
        with self._lock:
            state = self.states[key]
            if name.startswith("DELAYED_"):
                state.delayed = True
            state.timestamp = datetime.now(UTC)
            state.price_tick_count += 1
            if field == "close":
                state.close_tick_count += 1
            else:
                state.non_close_price_tick_count += 1
            price_value = float(price)
            if state.active_candidate_invert_price:
                price_value = _invert_price(price_value)
                if price_value is None:
                    return
                if field == "bid":
                    field = "ask"
                elif field == "ask":
                    field = "bid"
            setattr(state, field, price_value)
            previous_selected = state.selected_price
            previous_source = state.selected_source
            refresh_selected_price(state)
            if state.selected_price is not None:
                if state.first_price is None:
                    state.first_price = state.selected_price
                live_selected = state.selected_source != "close"
                previous_live_selected = previous_source not in {"", "close"}
                if live_selected and state.selected_price != previous_selected:
                    state.live_selected_update_count += 1
                if live_selected and previous_live_selected and previous_selected is not None and previous_selected > 0:
                    move_pct = abs(state.selected_price / previous_selected - 1.0) * 100.0
                    state.max_tick_move_pct = max(state.max_tick_move_pct, move_pct)
                state.previous_price = previous_selected
                state.previous_selected_source = previous_source
            state.status = "live" if state.selected_price is not None else state.status
            update_alerts(
                state,
                gap_threshold_pct=self.gap_threshold_pct,
                tick_move_threshold_pct=self.tick_move_threshold_pct,
            )

    def tickSize(self, reqId, tickType, size):  # noqa: N802
        key = self._market_req_to_key.get(int(reqId))
        if not key:
            return
        name = _tick_type_name(int(tickType))
        if name not in {"VOLUME", "DELAYED_VOLUME"}:
            return
        with self._lock:
            state = self.states[key]
            if name.startswith("DELAYED_"):
                state.delayed = True
            try:
                state.volume = float(size)
            except (TypeError, ValueError):
                pass

    def error(self, reqId, *args):  # noqa: N802
        code = args[0] if args and isinstance(args[0], int) else None
        informational = {2104, 2106, 2107, 2108, 2158, 10285}
        if code in informational:
            return
        message = " ".join(str(item) for item in args)
        key = self._contract_req_to_key.get(int(reqId)) or self._market_req_to_key.get(int(reqId))
        if key:
            with self._lock:
                state = self.states[key]
                if int(reqId) in self._contract_req_to_key and code in {200, 321, 322}:
                    if self._request_next_candidate_if_available(key, reason=f"ibkr_error_{code}"):
                        return
                state.error = message
                if state.status not in {"live", "subscribed"}:
                    state.status = "error"

    def start(self) -> None:
        self.connect(self.host, self.port, clientId=self.client_id)
        self._api_thread = threading.Thread(target=self.run, daemon=True)
        self._api_thread.start()
        if not self._connected.wait(self.timeout_seconds):
            self.stop()
            raise TimeoutError(f"Timed out waiting for IBKR nextValidId on {self.host}:{self.port}")

    def stop(self) -> None:
        for req_id in list(self._market_req_to_key):
            try:
                self.cancelMktData(req_id)
            except Exception:
                pass
        if self.isConnected():
            self.disconnect()

    def snapshot_rows(self) -> list[dict[str, Any]]:
        with self._lock:
            self._retry_no_price_subscriptions()
            rows: list[dict[str, Any]] = []
            for state in self.states.values():
                refresh_selected_price(state)
                update_alerts(
                    state,
                    gap_threshold_pct=self.gap_threshold_pct,
                    tick_move_threshold_pct=self.tick_move_threshold_pct,
                )
                rows.append(
                    {
                        "key": state.instrument.key,
                        "label": state.instrument.label,
                        "group": state.instrument.desk_group,
                        "status": state.status,
                        "price": state.selected_price,
                        "price_source": state.selected_source,
                        "bid": state.bid,
                        "ask": state.ask,
                        "last": state.last,
                        "close": state.close,
                        "gap_pct": _pct_change(state.selected_price, state.close),
                        "tick_move_pct": state.max_tick_move_pct,
                        "quote_spread_pct": _quote_spread_pct(state.bid, state.ask),
                        "price_tick_count": state.price_tick_count,
                        "non_close_price_tick_count": state.non_close_price_tick_count,
                        "close_tick_count": state.close_tick_count,
                        "live_selected_update_count": state.live_selected_update_count,
                        "selected_source_is_close": state.selected_source == "close",
                        "volume": state.volume,
                        "market_data_type": state.market_data_type,
                        "market_data_type_label": MARKET_DATA_TYPE_LABELS.get(state.market_data_type, "unknown"),
                        "delayed": state.market_data_type in {3, 4} or state.delayed,
                        "alerts": list(state.alerts),
                        "error": state.error,
                        "timestamp": state.timestamp.isoformat() if state.timestamp else "",
                        "quote_age_seconds": round((datetime.now(UTC) - state.timestamp).total_seconds(), 3) if state.timestamp else None,
                        "contract_candidate_index": state.active_candidate_index,
                        "contract_candidate_label": state.active_candidate_label,
                        "contract_candidate_invert_price": state.active_candidate_invert_price,
                        "attempted_contract_candidates": list(state.attempted_candidate_labels),
                        "candidate_retry_reasons": list(state.candidate_retry_reasons),
                    }
                )
            return rows


def _market_data_temperature_component_label(score: float) -> str:
    if score >= 30:
        return "stress"
    if score >= 18:
        return "hot"
    if score >= 8:
        return "watch"
    return "calm"


def _market_data_temperature_component_status(label: str) -> str:
    if label == "stress":
        return "red"
    if label in {"hot", "watch"}:
        return "yellow"
    return "green"


def _market_data_temperature_components(
    *,
    data_score: float,
    liquidity_score: float,
    market_move_score: float,
    missing_price_count: int,
    warming_up_count: int,
    delayed_count: int,
    error_count: int,
    stale_count: int,
    close_only_count: int,
    wide_spread_count: int,
    severe_wide_spread_count: int,
    market_move_alert_count: int,
    scored_market_move_alert_count: int,
    weighted_scored_market_move_alert_count: float,
    quality_excluded_market_move_alert_count: int,
    eligible_gap_score: float,
    eligible_tick_move_score: float,
) -> dict[str, dict[str, Any]]:
    data_label = _market_data_temperature_component_label(data_score)
    liquidity_label = _market_data_temperature_component_label(liquidity_score)
    market_move_label = _market_data_temperature_component_label(market_move_score)
    return {
        "data": {
            "label": data_label,
            "status": _market_data_temperature_component_status(data_label),
            "score": round(data_score, 3),
            "missing_count": missing_price_count,
            "warming_up_count": warming_up_count,
            "delayed_count": delayed_count,
            "error_count": error_count,
            "stale_count": stale_count,
            "close_only_count": close_only_count,
        },
        "liquidity": {
            "label": liquidity_label,
            "status": _market_data_temperature_component_status(liquidity_label),
            "score": round(liquidity_score, 3),
            "wide_spread_count": wide_spread_count,
            "severe_wide_spread_count": severe_wide_spread_count,
        },
        "market_move": {
            "label": market_move_label,
            "status": _market_data_temperature_component_status(market_move_label),
            "score": round(market_move_score, 3),
            "market_move_alert_count": market_move_alert_count,
            "scored_market_move_alert_count": scored_market_move_alert_count,
            "weighted_scored_market_move_alert_count": round(weighted_scored_market_move_alert_count, 3),
            "quality_excluded_market_move_alert_count": quality_excluded_market_move_alert_count,
            "eligible_gap_score": round(eligible_gap_score, 3),
            "eligible_tick_move_score": round(eligible_tick_move_score, 3),
        },
    }


def _market_data_risk_flags(
    *,
    delayed_count: int,
    missing_price_count: int,
    warming_up_count: int,
    error_count: int,
    stale_count: int,
    close_only_count: int,
    wide_spread_count: int,
    scored_market_move_alert_count: int,
    quality_excluded_market_move_alert_count: int,
    temperature_score: float,
    verified_reference_moves: list[dict[str, Any]],
    suspect_reference_moves: list[dict[str, Any]],
) -> list[str]:
    risk_flags = []
    if delayed_count:
        risk_flags.append("delayed_market_data_present")
    if missing_price_count:
        risk_flags.append("missing_market_prices")
    if warming_up_count:
        risk_flags.append("market_data_subscriptions_warming_up")
    if error_count:
        risk_flags.append("ibkr_contract_or_subscription_errors")
    if stale_count:
        risk_flags.append("stale_market_data_present")
    if close_only_count:
        risk_flags.append("close_only_quotes_present")
    if wide_spread_count:
        risk_flags.append("wide_spread_quotes_present")
    if scored_market_move_alert_count:
        risk_flags.append("gap_or_sudden_move_alerts")
    if quality_excluded_market_move_alert_count:
        risk_flags.append("quality_excluded_market_move_alerts")
    if temperature_score >= 18:
        risk_flags.append("market_temperature_elevated")
    if verified_reference_moves:
        risk_flags.append("verified_reference_market_moves")
    if suspect_reference_moves:
        risk_flags.append("suspect_reference_market_moves")
    if not risk_flags:
        risk_flags.append("market_data_temperature_normal")
    return risk_flags


def market_data_desk_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    row_count = len(rows)
    live_count = 0
    delayed_count = 0
    missing_price_count = 0
    warming_up_count = 0
    error_count = 0
    alert_count = 0
    priced_count = 0
    market_data_type_counts: dict[str, int] = {}
    group_summaries: dict[str, dict[str, Any]] = {}
    top_moves: list[dict[str, Any]] = []
    asia_reference_move_candidates: list[dict[str, Any]] = []
    delayed_instruments: list[str] = []
    missing_instruments: list[str] = []
    warming_up_instruments: list[str] = []
    error_instruments: list[str] = []
    alerted_instruments: list[str] = []
    fallback_instruments: list[str] = []
    close_only_instruments: list[str] = []
    stale_instruments: list[str] = []
    wide_spread_instruments: list[str] = []
    severe_wide_spread_instruments: list[str] = []
    close_only_count = 0
    stale_count = 0
    wide_spread_count = 0
    severe_wide_spread_count = 0
    market_move_alert_count = 0
    scored_market_move_alert_count = 0
    quality_excluded_market_move_alert_count = 0
    liquidity_alert_count = 0
    data_alert_count = 0
    weighted_scored_market_move_alert_count = 0.0
    importance_counts: dict[str, int] = {}
    impact_weight_counts: dict[str, int] = {}
    monitoring_only_instruments: list[str] = []
    core_us_trading_instruments: list[str] = []
    reference_close_only_instruments: list[str] = []
    stale_threshold_overrides: dict[str, float] = {}

    for row in rows:
        key = str(row.get("key") or "")
        group = str(row.get("group") or "unknown")
        importance = _market_data_importance(key, group)
        impact_weight = _market_data_impact_weight(key, group)
        importance_counts[importance] = importance_counts.get(importance, 0) + 1
        impact_bucket = f"{impact_weight:.2f}"
        impact_weight_counts[impact_bucket] = impact_weight_counts.get(impact_bucket, 0) + 1
        row["market_data_importance"] = importance
        row["market_data_impact_weight"] = impact_weight
        if importance == "core_us_trading" and key:
            core_us_trading_instruments.append(key)
        elif key:
            monitoring_only_instruments.append(key)
        group_summary = group_summaries.setdefault(
            group,
            {
                "instrument_count": 0,
                "priced_count": 0,
                "live_count": 0,
                "delayed_count": 0,
                "missing_price_count": 0,
                "warming_up_count": 0,
                "error_count": 0,
                "alert_count": 0,
                "wide_spread_count": 0,
                "stale_count": 0,
                "close_only_count": 0,
                "core_us_trading_count": 0,
                "monitoring_only_count": 0,
                "max_abs_gap_pct": 0.0,
                "max_tick_move_pct": 0.0,
                "status": "green",
            },
        )
        group_summary["instrument_count"] += 1
        if importance == "core_us_trading":
            group_summary["core_us_trading_count"] += 1
        else:
            group_summary["monitoring_only_count"] += 1
        label = str(row.get("market_data_type_label") or "unknown")
        market_data_type_counts[label] = market_data_type_counts.get(label, 0) + 1
        has_price = row.get("price") is not None
        is_delayed = bool(row.get("delayed"))
        is_error = bool(row.get("error")) or str(row.get("status") or "") in {"error", "contract_unresolved"}
        status_value = str(row.get("status") or "")
        is_warming_up = not has_price and not is_error and status_value in {
            "pending",
            "contract_pending",
            "resolved",
            "subscribed",
            "pending_contract_details",
        }
        alerts = list(row.get("alerts") or [])
        data_alert_count += sum(1 for item in alerts if str(item).startswith("delayed"))
        liquidity_alert_count += sum(1 for item in alerts if str(item).startswith("wide_spread"))
        row_market_move_alert_count = sum(1 for item in alerts if str(item).startswith(("gap_", "sudden_move")))
        market_move_alert_count += row_market_move_alert_count
        attempted_candidates = list(row.get("attempted_contract_candidates") or [])
        if len(attempted_candidates) > 1 and key:
            fallback_instruments.append(key)
        gap_pct = row.get("gap_pct")
        if gap_pct is None:
            gap_pct = _pct_change(row.get("price"), row.get("close"))
        tick_move_pct = row.get("tick_move_pct")
        try:
            abs_gap = abs(float(gap_pct)) if gap_pct is not None else 0.0
        except (TypeError, ValueError):
            abs_gap = 0.0
        try:
            tick_move = abs(float(tick_move_pct)) if tick_move_pct is not None else 0.0
        except (TypeError, ValueError):
            tick_move = 0.0
        try:
            quote_spread = abs(float(row.get("quote_spread_pct"))) if row.get("quote_spread_pct") is not None else 0.0
        except (TypeError, ValueError):
            quote_spread = 0.0
        selected_source_is_close = bool(row.get("selected_source_is_close"))
        stale_threshold = _stale_threshold_seconds(key, group, selected_source_is_close=selected_source_is_close)
        row["stale_threshold_seconds"] = stale_threshold
        if stale_threshold != 90.0 and key:
            stale_threshold_overrides[key] = stale_threshold
        if selected_source_is_close:
            close_only_count += 1
            group_summary["close_only_count"] += 1
            if key:
                close_only_instruments.append(key)
                if key.upper() in REFERENCE_CLOSE_ONLY_KEYS or importance != "core_us_trading":
                    reference_close_only_instruments.append(key)
        if quote_spread >= 0.5:
            wide_spread_count += 1
            group_summary["wide_spread_count"] += 1
            if key:
                wide_spread_instruments.append(key)
        if quote_spread >= 2.0:
            severe_wide_spread_count += 1
            if key:
                severe_wide_spread_instruments.append(key)
        try:
            quote_age_seconds = float(row.get("quote_age_seconds")) if row.get("quote_age_seconds") is not None else 0.0
        except (TypeError, ValueError):
            quote_age_seconds = 0.0
        if has_price and quote_age_seconds >= stale_threshold:
            stale_count += 1
            group_summary["stale_count"] += 1
            if key:
                stale_instruments.append(key)

        if has_price:
            priced_count += 1
            group_summary["priced_count"] += 1
        elif is_warming_up:
            warming_up_count += 1
            group_summary["warming_up_count"] += 1
            if key:
                warming_up_instruments.append(key)
        else:
            missing_price_count += 1
            group_summary["missing_price_count"] += 1
            if key:
                missing_instruments.append(key)
        if is_delayed:
            delayed_count += 1
            group_summary["delayed_count"] += 1
            if key:
                delayed_instruments.append(key)
        if is_error:
            error_count += 1
            group_summary["error_count"] += 1
            if key:
                error_instruments.append(key)
        if alerts:
            alert_count += 1
            group_summary["alert_count"] += 1
            if key:
                alerted_instruments.append(key)
        if has_price and not is_delayed and not is_error:
            live_count += 1
            group_summary["live_count"] += 1
        group_summary["max_abs_gap_pct"] = max(float(group_summary["max_abs_gap_pct"]), abs_gap)
        group_summary["max_tick_move_pct"] = max(float(group_summary["max_tick_move_pct"]), tick_move)
        gap_score_eligible = has_price and not selected_source_is_close and quote_spread < 2.0
        tick_move_score_eligible = has_price and int(row.get("live_selected_update_count") or 0) >= 2 and quote_spread < 2.0
        market_move_score_eligible = gap_score_eligible or tick_move_score_eligible
        if row_market_move_alert_count:
            if market_move_score_eligible:
                scored_market_move_alert_count += row_market_move_alert_count
                weighted_scored_market_move_alert_count += row_market_move_alert_count * impact_weight
            else:
                quality_excluded_market_move_alert_count += row_market_move_alert_count
        if has_price and (abs_gap > 0 or tick_move > 0):
            move_row = {
                "key": key,
                "group": group,
                "price": row.get("price"),
                "gap_pct": gap_pct,
                "tick_move_pct": tick_move_pct,
                "quote_spread_pct": row.get("quote_spread_pct"),
                "selected_source_is_close": selected_source_is_close,
                "gap_score_eligible": gap_score_eligible,
                "tick_move_score_eligible": tick_move_score_eligible,
                "market_move_score_eligible": market_move_score_eligible,
                "market_data_importance": importance,
                "market_data_impact_weight": impact_weight,
                "stale_threshold_seconds": stale_threshold,
                "alerts": alerts,
            }
            top_moves.append(move_row)
            if group == "asia_equity" and abs_gap >= 1.0:
                asia_reference_move_candidates.append(move_row)

    for group_summary in group_summaries.values():
        if group_summary["error_count"] or group_summary["missing_price_count"] >= 2:
            group_summary["status"] = "red"
        elif group_summary["delayed_count"] or group_summary["alert_count"] or group_summary["missing_price_count"] or group_summary["warming_up_count"] or group_summary["wide_spread_count"] or group_summary["stale_count"] or group_summary["close_only_count"]:
            group_summary["status"] = "yellow"

    top_moves.sort(
        key=lambda item: max(abs(float(item.get("gap_pct") or 0.0)), abs(float(item.get("tick_move_pct") or 0.0))),
        reverse=True,
    )
    asia_move_keys = {str(item.get("key") or "") for item in asia_reference_move_candidates}
    verified_reference_moves = []
    suspect_reference_moves = []
    for item in asia_reference_move_candidates:
        key = str(item.get("key") or "")
        factual_reference_key = key in FACTUAL_ASIA_REFERENCE_MOVE_KEYS
        asia_cross_market_count = len(asia_move_keys & FACTUAL_ASIA_REFERENCE_MOVE_KEYS)
        corroborated = bool(asia_cross_market_count >= 2 or {"KOSPI", "KOSPI_FUT"}.issubset(asia_move_keys))
        row_quality_issue = bool(item.get("selected_source_is_close") or float(item.get("quote_spread_pct") or 0.0) >= 2.0)
        quality = (
            "verified_reference_move"
            if factual_reference_key and (corroborated or not row_quality_issue)
            else "suspect_reference_move"
        )
        annotated = {
            "key": key,
            "group": item.get("group"),
            "gap_pct": item.get("gap_pct"),
            "tick_move_pct": item.get("tick_move_pct"),
            "alerts": item.get("alerts") or [],
            "quality": quality,
            "factual_asia_reference_key": factual_reference_key,
            "corroborated_by_asia_cross_market": corroborated,
            "trade_blocking": False,
            "note": "actual overseas reference move; displayed for context but downweighted for US trading gate"
            if quality == "verified_reference_move"
            else "reference move has quote-quality caveats; keep visible but do not hard-block US trading",
        }
        if quality == "verified_reference_move":
            verified_reference_moves.append(annotated)
        else:
            suspect_reference_moves.append(annotated)
    eligible_gap_score = sum(
        min(3.0, abs(float(item.get("gap_pct") or 0.0))) * float(item.get("market_data_impact_weight") or 1.0)
        for item in top_moves[:10]
        if item.get("gap_score_eligible")
    )
    eligible_tick_move_score = sum(
        min(3.0, abs(float(item.get("tick_move_pct") or 0.0))) * float(item.get("market_data_impact_weight") or 1.0)
        for item in top_moves[:10]
        if item.get("tick_move_score_eligible")
    )
    data_score = min(
        100.0,
        missing_price_count * 8.0
        + warming_up_count * 1.0
        + delayed_count * 4.0
        + error_count * 18.0
        + stale_count * 3.0
        + close_only_count * 1.0
        + data_alert_count * 2.0,
    )
    liquidity_score = min(100.0, wide_spread_count * 4.0 + severe_wide_spread_count * 8.0 + liquidity_alert_count * 2.0)
    market_move_score = min(100.0, weighted_scored_market_move_alert_count * 1.5 + eligible_gap_score + eligible_tick_move_score)
    temperature_score = max(data_score, liquidity_score, market_move_score)

    temperature_label = _market_data_temperature_component_label(temperature_score)
    status = _market_data_temperature_component_status(temperature_label)
    component_temperatures = _market_data_temperature_components(
        data_score=data_score,
        liquidity_score=liquidity_score,
        market_move_score=market_move_score,
        missing_price_count=missing_price_count,
        warming_up_count=warming_up_count,
        delayed_count=delayed_count,
        error_count=error_count,
        stale_count=stale_count,
        close_only_count=close_only_count,
        wide_spread_count=wide_spread_count,
        severe_wide_spread_count=severe_wide_spread_count,
        market_move_alert_count=market_move_alert_count,
        scored_market_move_alert_count=scored_market_move_alert_count,
        weighted_scored_market_move_alert_count=weighted_scored_market_move_alert_count,
        quality_excluded_market_move_alert_count=quality_excluded_market_move_alert_count,
        eligible_gap_score=eligible_gap_score,
        eligible_tick_move_score=eligible_tick_move_score,
    )
    risk_flags = _market_data_risk_flags(
        delayed_count=delayed_count,
        missing_price_count=missing_price_count,
        warming_up_count=warming_up_count,
        error_count=error_count,
        stale_count=stale_count,
        close_only_count=close_only_count,
        wide_spread_count=wide_spread_count,
        scored_market_move_alert_count=scored_market_move_alert_count,
        quality_excluded_market_move_alert_count=quality_excluded_market_move_alert_count,
        temperature_score=temperature_score,
        verified_reference_moves=verified_reference_moves,
        suspect_reference_moves=suspect_reference_moves,
    )
    return {
        "status": status,
        "temperature_label": temperature_label,
        "temperature_score": round(temperature_score, 3),
        "component_temperatures": component_temperatures,
        "row_count": row_count,
        "priced_count": priced_count,
        "live_count": live_count,
        "delayed_count": delayed_count,
        "missing_price_count": missing_price_count,
        "warming_up_count": warming_up_count,
        "error_count": error_count,
        "alert_count": alert_count,
        "market_move_alert_count": market_move_alert_count,
        "scored_market_move_alert_count": scored_market_move_alert_count,
        "weighted_scored_market_move_alert_count": round(weighted_scored_market_move_alert_count, 3),
        "quality_excluded_market_move_alert_count": quality_excluded_market_move_alert_count,
        "liquidity_alert_count": liquidity_alert_count,
        "data_alert_count": data_alert_count,
        "close_only_count": close_only_count,
        "stale_count": stale_count,
        "wide_spread_count": wide_spread_count,
        "severe_wide_spread_count": severe_wide_spread_count,
        "market_data_type_counts": market_data_type_counts,
        "group_summaries": group_summaries,
        "importance_counts": importance_counts,
        "impact_weight_counts": impact_weight_counts,
        "core_us_trading_instruments": core_us_trading_instruments,
        "monitoring_only_instruments": monitoring_only_instruments,
        "verified_reference_moves": verified_reference_moves,
        "suspect_reference_moves": suspect_reference_moves,
        "delayed_instruments": delayed_instruments,
        "missing_instruments": missing_instruments,
        "warming_up_instruments": warming_up_instruments,
        "error_instruments": error_instruments,
        "alerted_instruments": alerted_instruments,
        "fallback_instruments": fallback_instruments,
        "close_only_instruments": close_only_instruments,
        "reference_close_only_instruments": reference_close_only_instruments,
        "stale_instruments": stale_instruments,
        "stale_threshold_overrides": stale_threshold_overrides,
        "wide_spread_instruments": wide_spread_instruments,
        "severe_wide_spread_instruments": severe_wide_spread_instruments,
        "top_moves": top_moves[:10],
        "risk_flags": risk_flags,
    }


def market_data_snapshot_payload(
    rows: list[dict[str, Any]],
    *,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    generated = (generated_at or datetime.now(UTC)).isoformat()
    summary = market_data_desk_summary(rows)
    return {
        "market_data_desk_version": MARKET_DATA_DESK_VERSION,
        "market_data_temperature_version": MARKET_DATA_TEMPERATURE_VERSION,
        "generated_at": generated,
        "status": summary["status"],
        "temperature": {
            "label": summary["temperature_label"],
            "score": summary["temperature_score"],
            "components": summary.get("component_temperatures") or {},
            "risk_flags": summary["risk_flags"],
        },
        "summary": summary,
        "rows": rows,
        "safety": {
            "read_only_ibkr_market_data": True,
            "ibkr_order_submitted": False,
            "transmit_enabled": False,
            "account_or_portfolio_mutated": False,
        },
    }


def write_market_data_snapshot_json(output_path: Path, payload: dict[str, Any]) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return output_path


def format_market_data_report(rows: list[dict[str, Any]], *, generated_at: datetime | None = None) -> str:
    generated = (generated_at or datetime.now(UTC)).isoformat()
    summary = market_data_desk_summary(rows)
    lines = [
        (
            "Market Data Specialist Desk Report "
            f"generated_at={generated} status={summary['status']} "
            f"temperature={summary['temperature_label']} score={summary['temperature_score']}"
        ),
        "key | group | price | source | data | candidate | alerts | status",
        "--- | --- | ---: | --- | --- | --- | --- | ---",
    ]
    for row in rows:
        price = "-" if row["price"] is None else f"{float(row['price']):.6g}"
        data_label = row["market_data_type_label"]
        if row["delayed"] and "delayed" not in data_label:
            data_label += "/delayed"
        alerts = ",".join(row["alerts"]) if row["alerts"] else "-"
        status = str(row["status"])
        if row["error"]:
            status += f" ({row['error'][:80]})"
        lines.append(
            f"{row['key']} | {row['group']} | {price} | {row['price_source'] or '-'} | "
            f"{data_label} | {row.get('contract_candidate_label') or 'base'} | {alerts} | {status}"
        )
    delayed = [row["key"] for row in rows if row["delayed"]]
    alerted = [row["key"] for row in rows if row["alerts"]]
    errors = [row["key"] for row in rows if row["error"] or row["status"] in {"error", "contract_unresolved"}]
    warming = [
        row["key"]
        for row in rows
        if row["price"] is None and not row["error"] and row["status"] in {"subscribed", "pending_contract_details"}
    ]
    lines.extend(
        [
            "",
            f"delayed_instruments={','.join(delayed) if delayed else '-'}",
            f"warming_up_instruments={','.join(warming) if warming else '-'}",
            f"alerted_instruments={','.join(alerted) if alerted else '-'}",
            f"error_or_unresolved={','.join(errors) if errors else '-'}",
            "safety=read-only IBKR market data; no account, portfolio, or order action.",
        ]
    )
    return "\n".join(lines)
