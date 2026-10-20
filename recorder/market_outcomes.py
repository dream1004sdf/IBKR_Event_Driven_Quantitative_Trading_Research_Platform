from __future__ import annotations

import csv
import copy
import io
import json
import math
import threading
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Protocol

from ibapi.client import EClient
from ibapi.contract import Contract
from ibapi.wrapper import EWrapper

from .database import parse_utc

REPLAY_OUTCOME_VERSION = "replay-market-outcome-v1"
XOM_REDOMICILIATION_EFFECTIVE_DATE = date(2026, 7, 1)
US_STOCK_PRIMARY_EXCHANGES: tuple[str, ...] = ("NASDAQ", "ISLAND", "ARCA", "NYSE", "BATS", "AMEX")
NASDAQ_EXCHANGE_ALIASES: tuple[str, ...] = ("NASDAQ", "ISLAND")
FOREIGN_OR_HOLDING_EXCHANGES: tuple[str, ...] = ("TSE", "VSE", "LSE", "FWB", "SWB", "GETTEX")
KNOWN_FOREIGN_LISTING_RISK: dict[str, tuple[str, ...]] = {
    "AVGO": ("TSE CDR",),
    "XOM": ("TSE CDR", "VSE"),
}
KNOWN_CORPORATE_ACTION_CONTRACT_RISK: dict[str, str] = {
    "XOM": "ExxonMobil redomiciled into ExxonMobil Holdings on 2026-07-01 while retaining ticker XOM; older historical requests may require date-aware handling.",
}

SMART_STOCK_PRIMARY_EXCHANGE_HINTS: dict[str, tuple[str, ...]] = {
    # Core index ETFs / macro proxies.
    "SPY": ("ARCA",),
    "IWM": ("ARCA",),
    "DIA": ("ARCA",),
    "GLD": ("ARCA",),
    "USO": ("ARCA",),
    "XLE": ("ARCA",),
    "UUP": ("ARCA",),
    "EWY": ("ARCA",),
    "HACK": ("ARCA",),
    "DBA": ("ARCA",),
    "WEAT": ("ARCA",),
    "XAR": ("ARCA",),
    "VXX": ("ARCA",),
    "UVXY": ("ARCA",),
    "BITO": ("ARCA",),
    # Nasdaq-listed ETFs and mega-cap/AI names.
    "QQQ": ("NASDAQ",),
    "SMH": ("NASDAQ",),
    "SOXX": ("NASDAQ",),
    "TLT": ("NASDAQ",),
    "CIBR": ("NASDAQ",),
    "ETHA": ("NASDAQ",),
    "AAPL": ("NASDAQ",),
    "MSFT": ("NASDAQ",),
    "NVDA": ("NASDAQ",),
    "AMZN": ("NASDAQ",),
    "META": ("NASDAQ",),
    "GOOGL": ("NASDAQ",),
    "GOOG": ("NASDAQ",),
    "TSLA": ("NASDAQ",),
    "AVGO": ("NASDAQ",),
    "AMD": ("NASDAQ",),
    "MU": ("NASDAQ",),
    # Event-window defense ETF listing metadata has moved across venues in some feeds.
    "ITA": ("BATS", "ARCA"),
    # NYSE-listed ADRs / large-cap single names used in core backfills.
    "TSM": ("NYSE",),
    "XOM": ("NYSE",),
    "CVX": ("NYSE",),
}
SMART_STOCK_FALLBACK_PRIMARY_EXCHANGE_CANDIDATES: tuple[str, ...] = (
    "NASDAQ",
    "ISLAND",
    "ARCA",
    "NYSE",
    "BATS",
    "AMEX",
)


@dataclass(frozen=True)
class IBKRHistoricalErrorClassification:
    category: str
    retryable: bool
    reason: str


class IBKRHistoricalDataError(RuntimeError):
    def __init__(
        self,
        symbol: str,
        message: str,
        *,
        classification: IBKRHistoricalErrorClassification,
    ) -> None:
        self.symbol = symbol.upper()
        self.classification = classification
        super().__init__(
            f"{message} [ibkr_category={classification.category}; retryable={classification.retryable}; "
            f"reason={classification.reason}]"
        )


@dataclass(frozen=True)
class DailyBar:
    symbol: str
    date: date
    close: float
    open: float | None = None
    high: float | None = None
    low: float | None = None
    volume: float | None = None
    wap: float | None = None
    trade_count: int | None = None


@dataclass(frozen=True)
class IntradayBar:
    symbol: str
    timestamp: datetime
    close: float
    open: float | None = None
    high: float | None = None
    low: float | None = None
    volume: float | None = None
    wap: float | None = None
    trade_count: int | None = None


@dataclass(frozen=True)
class ReplayMarketOutcome:
    replay_corpus_id: int
    historical_event_id: int
    symbol: str
    benchmark_symbol: str
    horizon: str
    baseline_at: str | None
    reaction_at: str | None
    baseline_price: float | None
    reaction_price: float | None
    raw_return: float | None
    benchmark_baseline_price: float | None
    benchmark_reaction_price: float | None
    benchmark_return: float | None
    excess_return: float | None
    direction_label: str
    price_source: str
    price_status: str
    blocker_reason: str
    ingestion_version: str = REPLAY_OUTCOME_VERSION


class DailyPriceSource(Protocol):
    name: str

    def get_daily_bars(self, symbol: str, start: date, end: date) -> list[DailyBar]:
        ...


class IntradayPriceSource(Protocol):
    name: str

    def get_intraday_bars(self, symbol: str, start: datetime, end: datetime) -> list[IntradayBar]:
        ...


class StooqDailyPriceSource:
    name = "stooq_daily_close"

    def __init__(self, timeout_seconds: float = 20.0) -> None:
        self.timeout_seconds = float(timeout_seconds)

    def get_daily_bars(self, symbol: str, start: date, end: date) -> list[DailyBar]:
        ticker = f"{symbol.strip().lower()}.us"
        params = urllib.parse.urlencode(
            {
                "s": ticker,
                "d1": start.strftime("%Y%m%d"),
                "d2": end.strftime("%Y%m%d"),
                "i": "d",
            }
        )
        url = f"https://stooq.com/q/d/l/?{params}"
        request = urllib.request.Request(url, headers={"User-Agent": "IBKRMarketEventRecorder/0.14"})
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            payload = response.read().decode("utf-8", errors="replace")
        rows: list[DailyBar] = []
        for row in csv.DictReader(io.StringIO(payload)):
            if not row or str(row.get("Date") or "").lower() == "no data":
                continue
            try:
                row_date = datetime.strptime(str(row["Date"]), "%Y-%m-%d").date()
                close = float(row["Close"])
            except (KeyError, TypeError, ValueError):
                continue
            if close > 0:
                rows.append(DailyBar(symbol=symbol.upper(), date=row_date, close=close))
        return sorted(rows, key=lambda item: item.date)


class YahooChartDailyPriceSource:
    name = "yahoo_chart_daily_adjclose"

    def __init__(self, timeout_seconds: float = 20.0) -> None:
        self.timeout_seconds = float(timeout_seconds)

    def get_daily_bars(self, symbol: str, start: date, end: date) -> list[DailyBar]:
        start_dt = datetime.combine(start, time.min, tzinfo=timezone.utc)
        end_dt = datetime.combine(end + timedelta(days=1), time.min, tzinfo=timezone.utc)
        params = urllib.parse.urlencode(
            {
                "period1": int(start_dt.timestamp()),
                "period2": int(end_dt.timestamp()),
                "interval": "1d",
                "events": "history",
                "includeAdjustedClose": "true",
            }
        )
        ticker = urllib.parse.quote(symbol.strip().upper())
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?{params}"
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
        result = ((payload.get("chart") or {}).get("result") or [None])[0]
        if not result:
            return []
        timestamps = result.get("timestamp") or []
        indicators = result.get("indicators") or {}
        quote = (indicators.get("quote") or [{}])[0]
        closes = quote.get("close") or []
        adjusted = ((indicators.get("adjclose") or [{}])[0]).get("adjclose") or []
        rows: list[DailyBar] = []
        for index, timestamp in enumerate(timestamps):
            close_value = adjusted[index] if index < len(adjusted) else None
            if close_value is None and index < len(closes):
                close_value = closes[index]
            if close_value is None:
                continue
            try:
                row_date = datetime.fromtimestamp(int(timestamp), tz=timezone.utc).date()
                close = float(close_value)
            except (TypeError, ValueError, OSError):
                continue
            if start <= row_date <= end and close > 0:
                rows.append(DailyBar(symbol=symbol.upper(), date=row_date, close=close))
        return sorted(rows, key=lambda item: item.date)


def _parse_ibkr_bar_date(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    candidates = [text]
    if len(text) >= 8:
        candidates.append(text[:8])
    for candidate, fmt in (
        (candidates[0], "%Y%m%d"),
        (candidates[0], "%Y-%m-%d"),
        (candidates[-1], "%Y%m%d"),
    ):
        try:
            return datetime.strptime(candidate, fmt).date()
        except ValueError:
            continue
    return None


def _parse_ibkr_bar_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.isdigit():
        try:
            timestamp = int(text)
            if timestamp > 10_000_000:
                return datetime.fromtimestamp(timestamp, tz=timezone.utc)
        except (ValueError, OSError):
            pass
    normalized = text.replace("  ", " ")
    for fmt in (
        "%Y%m%d %H:%M:%S",
        "%Y%m%d-%H:%M:%S",
        "%Y%m%d %H:%M:%S %Z",
        "%Y-%m-%d %H:%M:%S",
    ):
        try:
            parsed = datetime.strptime(normalized, fmt)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except ValueError:
            continue
    return None


def _primary_exchange_hint(symbol: str) -> str:
    values = _primary_exchange_hints(symbol)
    return values[0] if values else ""


def _primary_exchange_hints(symbol: str) -> tuple[str, ...]:
    return SMART_STOCK_PRIMARY_EXCHANGE_HINTS.get(str(symbol or "").strip().upper(), ())


def primary_exchange_candidates_for_symbol(symbol: str) -> tuple[str, ...]:
    """Return practical IBKR SMART stock primaryExchange candidates for a ticker.

    Known symbols start with curated hints. Unknown symbols still receive a
    bounded US stock/ETF fallback set so repair/backfill retries are not stuck
    on a generic SMART contract that IBKR historical data cannot resolve.
    """
    seen: set[str] = set()
    candidates: list[str] = []
    for value in (*_primary_exchange_hints(symbol), *SMART_STOCK_FALLBACK_PRIMARY_EXCHANGE_CANDIDATES):
        normalized = str(value or "").strip().upper()
        if normalized and normalized not in seen:
            seen.add(normalized)
            candidates.append(normalized)
    return tuple(candidates)


def contract_resolution_profile_for_symbol(symbol: str) -> dict[str, Any]:
    normalized = str(symbol or "").strip().upper()
    candidates = primary_exchange_candidates_for_symbol(normalized)
    hints = _primary_exchange_hints(normalized)
    known_foreign = list(KNOWN_FOREIGN_LISTING_RISK.get(normalized, ()))
    corporate_action_note = KNOWN_CORPORATE_ACTION_CONTRACT_RISK.get(normalized)
    gateway_aliases = list(NASDAQ_EXCHANGE_ALIASES) if any(value in NASDAQ_EXCHANGE_ALIASES for value in candidates) else []
    requires_probe = not hints or bool(known_foreign) or bool(corporate_action_note)
    if hints:
        resolution_mode = "curated_us_primary_exchange"
    else:
        resolution_mode = "bounded_us_exchange_fallback_review"
    risk_flags: list[str] = []
    if known_foreign:
        risk_flags.append("foreign_or_cdr_same_ticker")
    if corporate_action_note:
        risk_flags.append("corporate_action_or_new_conid")
    if gateway_aliases:
        risk_flags.append("nasdaq_island_gateway_alias")
    if not hints:
        risk_flags.append("unknown_symbol_requires_contract_probe")
    return {
        "symbol": normalized,
        "resolution_mode": resolution_mode,
        "primary_exchange_candidates": list(candidates),
        "gateway_exchange_aliases": gateway_aliases,
        "known_foreign_listing_risk": known_foreign,
        "corporate_action_contract_risk": corporate_action_note,
        "risk_flags": risk_flags,
        "requires_contract_probe": bool(requires_probe),
        "recommended_policy": (
            "probe_contract_details_before_deep_backfill"
            if requires_probe
            else "use_curated_primary_exchange_candidates"
        ),
    }


def _exchange_aliases(value: str | None) -> tuple[str, ...]:
    normalized = str(value or "").strip().upper()
    if normalized in NASDAQ_EXCHANGE_ALIASES:
        return NASDAQ_EXCHANGE_ALIASES
    return (normalized,) if normalized else ()


def _exchange_matches(requested: str | None, actual: str | None) -> bool:
    requested_aliases = set(_exchange_aliases(requested))
    actual_aliases = set(_exchange_aliases(actual))
    return bool(requested_aliases and actual_aliases and requested_aliases.intersection(actual_aliases))


def _contract_exchange_values(contract: Contract) -> tuple[str, ...]:
    values: list[str] = []
    for attr in ("primaryExchange", "exchange", "localSymbol", "tradingClass"):
        value = str(getattr(contract, attr, "") or "").strip().upper()
        if value:
            values.append(value)
    return tuple(values)


def _contract_looks_foreign_or_cdr(contract: Contract) -> bool:
    currency = str(getattr(contract, "currency", "") or "").strip().upper()
    if currency and currency != "USD":
        return True
    exchange_values = _contract_exchange_values(contract)
    if any(value in FOREIGN_OR_HOLDING_EXCHANGES for value in exchange_values):
        return True
    return any("CDR" in value for value in exchange_values)


def _clone_contract(contract: Contract) -> Contract:
    return copy.copy(contract)


def _contract_cache_key(contract: Contract) -> tuple[Any, ...]:
    return (
        int(getattr(contract, "conId", 0) or 0),
        str(getattr(contract, "symbol", "") or "").upper(),
        str(getattr(contract, "secType", "") or "").upper(),
        str(getattr(contract, "exchange", "") or "").upper(),
        str(getattr(contract, "primaryExchange", "") or "").upper(),
        str(getattr(contract, "currency", "") or "").upper(),
    )


def _historical_contract_variants(contract: Contract) -> tuple[Contract, ...]:
    variants: list[Contract] = []
    seen: set[tuple[Any, ...]] = set()

    def add(candidate: Contract) -> None:
        key = _contract_cache_key(candidate)
        if key not in seen:
            seen.add(key)
            variants.append(candidate)

    smart = _clone_contract(contract)
    smart.exchange = "SMART"
    add(smart)

    primary_exchange = str(getattr(contract, "primaryExchange", "") or "").strip().upper()
    if primary_exchange:
        direct = _clone_contract(contract)
        direct.exchange = primary_exchange
        direct.primaryExchange = ""
        add(direct)
    return tuple(variants)


def _select_best_stock_contract(
    symbol: str,
    contracts: list[Contract],
    *,
    requested_primary_exchange: str | None,
    require_primary_exchange: bool,
) -> Contract | None:
    normalized = symbol.upper()
    hints = _primary_exchange_hints(normalized)
    requested = str(requested_primary_exchange or "").strip().upper()

    scored: list[tuple[int, int, Contract]] = []
    for index, contract in enumerate(contracts):
        if str(getattr(contract, "secType", "") or "").strip().upper() not in {"", "STK"}:
            continue
        if str(getattr(contract, "symbol", "") or "").strip().upper() not in {"", normalized}:
            continue
        if _contract_looks_foreign_or_cdr(contract):
            continue
        primary_exchange = str(getattr(contract, "primaryExchange", "") or "").strip().upper()
        exchange = str(getattr(contract, "exchange", "") or "").strip().upper()
        score = 0
        if int(getattr(contract, "conId", 0) or 0) > 0:
            score += 100
        if requested and (_exchange_matches(requested, primary_exchange) or _exchange_matches(requested, exchange)):
            score += 70
        if primary_exchange in hints or any(_exchange_matches(hint, primary_exchange) for hint in hints):
            score += 40
        if primary_exchange in US_STOCK_PRIMARY_EXCHANGES or exchange in US_STOCK_PRIMARY_EXCHANGES:
            score += 20
        if exchange == "SMART":
            score += 5
        if require_primary_exchange and not primary_exchange and not requested:
            score -= 50
        scored.append((score, -index, contract))

    if not scored:
        return None
    return max(scored, key=lambda item: (item[0], item[1]))[2]


def classify_ibkr_historical_error(
    errors: list[str] | tuple[str, ...] | str,
    *,
    symbol: str = "",
    start: date | datetime | None = None,
) -> IBKRHistoricalErrorClassification:
    if isinstance(errors, str):
        values = [errors]
    else:
        values = [str(item) for item in errors]
    text = " ".join(values).lower()
    normalized_symbol = str(symbol or "").strip().upper()
    start_date: date | None = None
    if isinstance(start, datetime):
        start_date = start.date()
    elif isinstance(start, date):
        start_date = start

    if "pacing" in text or "rate" in text and "limit" in text:
        return IBKRHistoricalErrorClassification("pacing_limit", True, "ibkr_historical_pacing_or_rate_limit")
    if "timed out" in text or "timeout" in text:
        return IBKRHistoricalErrorClassification("timeout", True, "ibkr_historical_timeout")
    if "no market data permissions" in text or ("market data" in text and "permission" in text):
        return IBKRHistoricalErrorClassification("permission_missing", False, "market_data_permission_missing")
    if "ambiguous" in text:
        return IBKRHistoricalErrorClassification("ambiguous_contract", True, "contract_definition_ambiguous")
    if "unknown contract" in text or "unrecognised contract" in text or "unrecognized contract" in text:
        if normalized_symbol == "XOM" and start_date is not None and start_date < XOM_REDOMICILIATION_EFFECTIVE_DATE:
            return IBKRHistoricalErrorClassification(
                "corporate_action_or_new_conid",
                False,
                "xom_redomiciliation_current_contract_may_not_cover_prior_history",
            )
        return IBKRHistoricalErrorClassification("exchange_alias_retry_needed", True, "unknown_contract_try_exchange_aliases")
    if "no such data" in text or "no data" in text or "not available" in text:
        return IBKRHistoricalErrorClassification("historical_unavailable", False, "ibkr_hmds_data_unavailable")
    if "historical market data" in text and "162" in text:
        return IBKRHistoricalErrorClassification("historical_service_error", True, "generic_ibkr_162_historical_error")
    return IBKRHistoricalErrorClassification("historical_error", False, "unclassified_ibkr_historical_error")


def _ibkr_errors_indicate_unknown_contract(errors: list[str] | tuple[str, ...] | str) -> bool:
    if isinstance(errors, str):
        values = [errors]
    else:
        values = [str(item) for item in errors]
    text = " ".join(values).lower()
    return "unknown contract" in text or (" 162 " in f" {text} " and "historical market data" in text)


class _IBKRHistoricalClient(EWrapper, EClient):
    def __init__(self) -> None:
        EClient.__init__(self, self)
        self.next_valid_id = threading.Event()
        self._api_thread: threading.Thread | None = None
        self._request_id = 7000
        self._events: dict[int, threading.Event] = {}
        self._bars: dict[int, list[DailyBar]] = {}
        self._intraday_bars: dict[int, list[IntradayBar]] = {}
        self._intraday_requests: set[int] = set()
        self._symbols: dict[int, str] = {}
        self._errors: dict[int, list[str]] = {}
        self._contract_results: dict[int, list[Contract]] = {}
        self._contract_cache: dict[str, Contract] = {}

    def start(self, host: str, port: int, client_id: int, timeout_seconds: float) -> None:
        self.connect(host, int(port), clientId=int(client_id))
        self._api_thread = threading.Thread(target=self.run, daemon=True)
        self._api_thread.start()
        if not self.next_valid_id.wait(float(timeout_seconds)):
            self.close()
            raise TimeoutError(f"Timed out waiting for IBKR nextValidId on {host}:{port}")

    def close(self) -> None:
        if self.isConnected():
            self.disconnect()

    def nextValidId(self, orderId: int) -> None:  # noqa: N802
        self.next_valid_id.set()

    def error(self, reqId, *args):  # noqa: N802
        code = args[0] if args and isinstance(args[0], int) else None
        informational = {2104, 2106, 2107, 2108, 2158}
        if code in informational:
            return
        message = " ".join(str(item) for item in args)
        if int(reqId) in self._events:
            self._errors.setdefault(int(reqId), []).append(message)
            self._events[int(reqId)].set()

    def historicalData(self, reqId, bar):  # noqa: N802
        request_id = int(reqId)
        symbol = self._symbols.get(request_id, "")
        try:
            close = float(getattr(bar, "close"))
        except (TypeError, ValueError):
            close = 0.0
        if not symbol or not math.isfinite(close) or close <= 0:
            return
        def optional_float(name: str) -> float | None:
            try:
                value = float(getattr(bar, name))
            except (AttributeError, TypeError, ValueError):
                return None
            return value if math.isfinite(value) else None

        def optional_int(name: str) -> int | None:
            try:
                return int(getattr(bar, name))
            except (AttributeError, TypeError, ValueError):
                return None

        open_price = optional_float("open")
        high = optional_float("high")
        low = optional_float("low")
        volume = optional_float("volume")
        wap = optional_float("wap")
        trade_count = optional_int("barCount")
        if request_id in self._intraday_requests:
            timestamp = _parse_ibkr_bar_datetime(getattr(bar, "date", ""))
            if timestamp is not None:
                self._intraday_bars.setdefault(request_id, []).append(
                    IntradayBar(
                        symbol=symbol.upper(),
                        timestamp=timestamp,
                        close=close,
                        open=open_price,
                        high=high,
                        low=low,
                        volume=volume,
                        wap=wap,
                        trade_count=trade_count,
                    )
                )
            return
        bar_date = _parse_ibkr_bar_date(getattr(bar, "date", ""))
        if bar_date is not None:
            self._bars.setdefault(request_id, []).append(
                DailyBar(
                    symbol=symbol.upper(),
                    date=bar_date,
                    close=close,
                    open=open_price,
                    high=high,
                    low=low,
                    volume=volume,
                    wap=wap,
                    trade_count=trade_count,
                )
            )

    def historicalDataEnd(self, reqId, start, end):  # noqa: N802
        if int(reqId) in self._events:
            self._events[int(reqId)].set()

    def contractDetails(self, reqId, contractDetails):  # noqa: N802
        request_id = int(reqId)
        if request_id in self._events:
            self._contract_results.setdefault(request_id, []).append(contractDetails.contract)

    def contractDetailsEnd(self, reqId):  # noqa: N802
        if int(reqId) in self._events:
            self._events[int(reqId)].set()

    @staticmethod
    def stock_contract(
        symbol: str,
        *,
        use_primary_exchange_hint: bool = True,
        primary_exchange: str | None = None,
    ) -> Contract:
        contract = Contract()
        normalized = symbol.upper()
        contract.symbol = normalized
        contract.secType = "STK"
        contract.exchange = "SMART"
        contract.currency = "USD"
        selected_primary_exchange = primary_exchange or (
            _primary_exchange_hint(normalized) if use_primary_exchange_hint else ""
        )
        if selected_primary_exchange:
            contract.primaryExchange = selected_primary_exchange
        return contract

    @staticmethod
    def _normalize_resolved_stock_contract(
        contract: Contract,
        symbol: str,
        *,
        require_primary_exchange: bool,
        requested_primary_exchange: str | None = None,
    ) -> Contract:
        normalized = symbol.upper()
        contract = _clone_contract(contract)
        contract.exchange = "SMART"
        if not getattr(contract, "currency", ""):
            contract.currency = "USD"
        primary_exchange = str(requested_primary_exchange or "").strip().upper() or _primary_exchange_hint(normalized)
        if primary_exchange and (require_primary_exchange or not getattr(contract, "primaryExchange", "")):
            contract.primaryExchange = primary_exchange
        return contract

    def _resolve_stock_contract_once(
        self,
        symbol: str,
        timeout_seconds: float,
        *,
        use_primary_exchange_hint: bool,
        require_primary_exchange: bool,
        primary_exchange: str | None = None,
    ) -> tuple[Contract | None, list[str]]:
        normalized = symbol.upper()
        self._request_id += 1
        req_id = self._request_id
        done = threading.Event()
        self._events[req_id] = done
        self.reqContractDetails(
            req_id,
            self.stock_contract(
                normalized,
                use_primary_exchange_hint=use_primary_exchange_hint,
                primary_exchange=primary_exchange,
            ),
        )
        if not done.wait(float(timeout_seconds)):
            self._events.pop(req_id, None)
            raise TimeoutError(f"Timed out waiting for IBKR contract details for {normalized}")
        errors = self._errors.pop(req_id, [])
        contracts = self._contract_results.pop(req_id, [])
        self._events.pop(req_id, None)
        contract = _select_best_stock_contract(
            normalized,
            contracts,
            requested_primary_exchange=primary_exchange,
            require_primary_exchange=require_primary_exchange,
        )
        if contract is not None:
            return self._normalize_resolved_stock_contract(
                contract,
                normalized,
                require_primary_exchange=require_primary_exchange,
                requested_primary_exchange=primary_exchange,
            ), errors
        return None, errors

    def resolve_stock_contract_candidates(
        self,
        symbol: str,
        timeout_seconds: float,
        *,
        require_primary_exchange: bool = True,
    ) -> list[Contract]:
        normalized = symbol.upper()
        candidates: list[Contract] = []
        seen: set[tuple[Any, ...]] = set()
        primary_hints = primary_exchange_candidates_for_symbol(normalized) if require_primary_exchange else _primary_exchange_hints(normalized)
        for primary_exchange in primary_hints:
            contract, _errors = self._resolve_stock_contract_once(
                normalized,
                timeout_seconds,
                use_primary_exchange_hint=True,
                require_primary_exchange=True,
                primary_exchange=primary_exchange,
            )
            if contract is None:
                continue
            key = _contract_cache_key(contract)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(contract)
        return candidates

    def resolve_stock_contract(
        self,
        symbol: str,
        timeout_seconds: float,
        *,
        refresh: bool = False,
        require_primary_exchange: bool = False,
    ) -> Contract:
        normalized = symbol.upper()
        cached = self._contract_cache.get(normalized)
        if cached is not None and not refresh:
            if not require_primary_exchange or getattr(cached, "primaryExchange", ""):
                return cached
        if refresh:
            self._contract_cache.pop(normalized, None)

        primary_hints = primary_exchange_candidates_for_symbol(normalized) if require_primary_exchange else _primary_exchange_hints(normalized)
        if primary_hints:
            primary_errors: list[str] = []
            contract, errors = self._resolve_stock_contract_once(
                normalized,
                timeout_seconds,
                use_primary_exchange_hint=True,
                require_primary_exchange=True,
                primary_exchange=primary_hints[0],
            )
            if contract is not None:
                self._contract_cache[normalized] = contract
                return contract
            primary_errors.extend(errors)
            for primary_exchange in primary_hints[1:]:
                contract, errors = self._resolve_stock_contract_once(
                    normalized,
                    timeout_seconds,
                    use_primary_exchange_hint=True,
                    require_primary_exchange=True,
                    primary_exchange=primary_exchange,
                )
                if contract is not None:
                    self._contract_cache[normalized] = contract
                    return contract
                primary_errors.extend(errors)
            if require_primary_exchange:
                if primary_errors:
                    raise RuntimeError(f"IBKR contract lookup failed for {normalized}: {'; '.join(primary_errors)}")
                raise RuntimeError(f"IBKR contract lookup returned no contract for {normalized}")

        contract, errors = self._resolve_stock_contract_once(
            normalized,
            timeout_seconds,
            use_primary_exchange_hint=False,
            require_primary_exchange=False,
        )
        if contract is not None:
            self._contract_cache[normalized] = contract
            return contract

        if errors:
            raise RuntimeError(f"IBKR contract lookup failed for {normalized}: {'; '.join(errors)}")
        raise RuntimeError(f"IBKR contract lookup returned no contract for {normalized}")

    def _request_daily_bars_once(
        self,
        *,
        symbol: str,
        contract: Contract,
        start: date,
        end: date,
        timeout_seconds: float,
        what_to_show: str,
        use_rth: bool,
    ) -> list[DailyBar]:
        self._request_id += 1
        req_id = self._request_id
        done = threading.Event()
        self._events[req_id] = done
        self._symbols[req_id] = symbol.upper()
        self._bars[req_id] = []
        span_days = max(1, (end - start).days + 7)
        duration = f"{span_days} D" if span_days <= 365 else f"{max(1, math.ceil(span_days / 365))} Y"
        end_time = datetime.combine(end + timedelta(days=1), time.min, tzinfo=timezone.utc)
        self.reqHistoricalData(
            req_id,
            contract,
            end_time.strftime("%Y%m%d %H:%M:%S UTC"),
            duration,
            "1 day",
            what_to_show,
            1 if use_rth else 0,
            1,
            False,
            [],
        )
        if not done.wait(float(timeout_seconds)):
            self.cancelHistoricalData(req_id)
            raise TimeoutError(f"Timed out waiting for IBKR historical data for {symbol}")
        errors = self._errors.pop(req_id, [])
        rows = self._bars.pop(req_id, [])
        self._events.pop(req_id, None)
        self._symbols.pop(req_id, None)
        if errors and not rows:
            classification = classify_ibkr_historical_error(errors, symbol=symbol, start=start)
            raise IBKRHistoricalDataError(
                symbol,
                f"IBKR historical data failed for {symbol}: {'; '.join(errors)}",
                classification=classification,
            )
        return sorted((bar for bar in rows if start <= bar.date <= end), key=lambda item: item.date)

    def request_daily_bars(
        self,
        *,
        symbol: str,
        start: date,
        end: date,
        timeout_seconds: float,
        what_to_show: str,
        use_rth: bool,
    ) -> list[DailyBar]:
        contract = self.resolve_stock_contract(symbol, timeout_seconds)
        try:
            return self._request_daily_bars_once(
                symbol=symbol,
                contract=contract,
                start=start,
                end=end,
                timeout_seconds=timeout_seconds,
                what_to_show=what_to_show,
                use_rth=use_rth,
            )
        except IBKRHistoricalDataError as exc:
            if not exc.classification.retryable:
                raise
            last_error: IBKRHistoricalDataError = exc
            try:
                retry_contracts = self.resolve_stock_contract_candidates(
                    symbol,
                    timeout_seconds,
                    require_primary_exchange=True,
                )
            except Exception:
                retry_contracts = []
            if not retry_contracts:
                retry_contracts = [
                    self.resolve_stock_contract(
                        symbol,
                        timeout_seconds,
                        refresh=True,
                        require_primary_exchange=True,
                    )
                ]
            for retry_contract in retry_contracts:
                for candidate_contract in _historical_contract_variants(retry_contract):
                    try:
                        return self._request_daily_bars_once(
                            symbol=symbol,
                            contract=candidate_contract,
                            start=start,
                            end=end,
                            timeout_seconds=timeout_seconds,
                            what_to_show=what_to_show,
                            use_rth=use_rth,
                        )
                    except IBKRHistoricalDataError as retry_exc:
                        last_error = retry_exc
                        if not retry_exc.classification.retryable:
                            raise
            raise last_error

    def _request_intraday_bars_once(
        self,
        *,
        symbol: str,
        contract: Contract,
        start: datetime,
        end: datetime,
        timeout_seconds: float,
        bar_size: str,
        what_to_show: str,
        use_rth: bool,
    ) -> list[IntradayBar]:
        self._request_id += 1
        req_id = self._request_id
        done = threading.Event()
        self._events[req_id] = done
        self._symbols[req_id] = symbol.upper()
        self._intraday_bars[req_id] = []
        self._intraday_requests.add(req_id)
        span_seconds = max(60, int((end - start).total_seconds()) + 3600)
        if span_seconds <= 86_400:
            duration = f"{span_seconds} S"
        else:
            duration = f"{math.ceil(span_seconds / 86_400)} D"
        self.reqHistoricalData(
            req_id,
            contract,
            end.strftime("%Y%m%d %H:%M:%S UTC"),
            duration,
            bar_size,
            what_to_show,
            1 if use_rth else 0,
            2,
            False,
            [],
        )
        if not done.wait(float(timeout_seconds)):
            self.cancelHistoricalData(req_id)
            raise TimeoutError(f"Timed out waiting for IBKR intraday historical data for {symbol}")
        errors = self._errors.pop(req_id, [])
        rows = self._intraday_bars.pop(req_id, [])
        self._events.pop(req_id, None)
        self._symbols.pop(req_id, None)
        self._intraday_requests.discard(req_id)
        if errors and not rows:
            classification = classify_ibkr_historical_error(errors, symbol=symbol, start=start)
            raise IBKRHistoricalDataError(
                symbol,
                f"IBKR intraday historical data failed for {symbol}: {'; '.join(errors)}",
                classification=classification,
            )
        return sorted((bar for bar in rows if start <= bar.timestamp <= end), key=lambda item: item.timestamp)

    def request_intraday_bars(
        self,
        *,
        symbol: str,
        start: datetime,
        end: datetime,
        timeout_seconds: float,
        bar_size: str,
        what_to_show: str,
        use_rth: bool,
    ) -> list[IntradayBar]:
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        start = start.astimezone(timezone.utc)
        end = end.astimezone(timezone.utc)
        contract = self.resolve_stock_contract(symbol, timeout_seconds)
        try:
            return self._request_intraday_bars_once(
                symbol=symbol,
                contract=contract,
                start=start,
                end=end,
                timeout_seconds=timeout_seconds,
                bar_size=bar_size,
                what_to_show=what_to_show,
                use_rth=use_rth,
            )
        except IBKRHistoricalDataError as exc:
            if not exc.classification.retryable:
                raise
            last_error: IBKRHistoricalDataError = exc
            try:
                retry_contracts = self.resolve_stock_contract_candidates(
                    symbol,
                    timeout_seconds,
                    require_primary_exchange=True,
                )
            except Exception:
                retry_contracts = []
            if not retry_contracts:
                retry_contracts = [
                    self.resolve_stock_contract(
                        symbol,
                        timeout_seconds,
                        refresh=True,
                        require_primary_exchange=True,
                    )
                ]
            for retry_contract in retry_contracts:
                for candidate_contract in _historical_contract_variants(retry_contract):
                    try:
                        return self._request_intraday_bars_once(
                            symbol=symbol,
                            contract=candidate_contract,
                            start=start,
                            end=end,
                            timeout_seconds=timeout_seconds,
                            bar_size=bar_size,
                            what_to_show=what_to_show,
                            use_rth=use_rth,
                        )
                    except IBKRHistoricalDataError as retry_exc:
                        last_error = retry_exc
                        if not retry_exc.classification.retryable:
                            raise
            raise last_error


class IBKRHistoricalDailyPriceSource:
    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 4001,
        client_id: int = 1021,
        timeout_seconds: float = 20.0,
        what_to_show: str = "TRADES",
        use_rth: bool = True,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.client_id = int(client_id)
        self.timeout_seconds = float(timeout_seconds)
        self.what_to_show = what_to_show.strip().upper() or "TRADES"
        self.use_rth = bool(use_rth)
        self.name = f"ibkr_historical_{self.what_to_show.lower()}_daily"
        self._client: _IBKRHistoricalClient | None = None

    def _ensure_client(self) -> _IBKRHistoricalClient:
        if self._client is None or not self._client.isConnected():
            client = _IBKRHistoricalClient()
            client.start(self.host, self.port, self.client_id, self.timeout_seconds)
            self._client = client
        return self._client

    def get_daily_bars(self, symbol: str, start: date, end: date) -> list[DailyBar]:
        return self._ensure_client().request_daily_bars(
            symbol=symbol,
            start=start,
            end=end,
            timeout_seconds=self.timeout_seconds,
            what_to_show=self.what_to_show,
            use_rth=self.use_rth,
        )

    def resolve_stock_contract(self, symbol: str) -> Contract:
        return self._ensure_client().resolve_stock_contract(symbol, self.timeout_seconds)

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None


class IBKRHistoricalIntradayPriceSource:
    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 4001,
        client_id: int = 1021,
        timeout_seconds: float = 20.0,
        what_to_show: str = "TRADES",
        bar_size: str = "5 mins",
        use_rth: bool = False,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.client_id = int(client_id)
        self.timeout_seconds = float(timeout_seconds)
        self.what_to_show = what_to_show.strip().upper() or "TRADES"
        self.bar_size = bar_size.strip() or "5 mins"
        self.use_rth = bool(use_rth)
        session = "rth" if self.use_rth else "all_sessions"
        safe_bar_size = self.bar_size.lower().replace(" ", "_")
        self.name = f"ibkr_historical_{self.what_to_show.lower()}_{safe_bar_size}_{session}"
        self._client: _IBKRHistoricalClient | None = None

    def _ensure_client(self) -> _IBKRHistoricalClient:
        if self._client is None or not self._client.isConnected():
            client = _IBKRHistoricalClient()
            client.start(self.host, self.port, self.client_id, self.timeout_seconds)
            self._client = client
        return self._client

    def get_intraday_bars(self, symbol: str, start: datetime, end: datetime) -> list[IntradayBar]:
        return self._ensure_client().request_intraday_bars(
            symbol=symbol,
            start=start,
            end=end,
            timeout_seconds=self.timeout_seconds,
            bar_size=self.bar_size,
            what_to_show=self.what_to_show,
            use_rth=self.use_rth,
        )

    def resolve_stock_contract(self, symbol: str) -> Contract:
        return self._ensure_client().resolve_stock_contract(symbol, self.timeout_seconds)

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None


def parse_horizons(value: str | tuple[str, ...] | list[str]) -> tuple[str, ...]:
    if isinstance(value, str):
        parts = value.split(",")
    else:
        parts = list(value)
    horizons: list[str] = []
    for item in parts:
        normalized = str(item).strip().upper()
        if not normalized:
            continue
        if normalized.endswith(("M", "H", "D")):
            amount_text = normalized[:-1]
        else:
            raise ValueError(f"Unsupported horizon {item!r}; use values like 30M, 2H, 1D, or 5D")
        amount = int(amount_text)
        if amount <= 0:
            raise ValueError(f"Unsupported horizon {item!r}; horizon must be positive")
        horizons.append(f"{amount}{normalized[-1]}")
    if not horizons:
        raise ValueError("At least one horizon is required")
    return tuple(dict.fromkeys(horizons))


def _horizon_days(horizon: str) -> int:
    normalized = str(horizon).strip().upper()
    if not normalized.endswith("D"):
        raise ValueError(f"Unsupported horizon {horizon!r}")
    return int(normalized[:-1])


def _is_daily_horizon(horizon: str) -> bool:
    return str(horizon).strip().upper().endswith("D")


def _horizon_delta(horizon: str) -> timedelta:
    normalized = str(horizon).strip().upper()
    amount = int(normalized[:-1])
    if normalized.endswith("M"):
        return timedelta(minutes=amount)
    if normalized.endswith("H"):
        return timedelta(hours=amount)
    if normalized.endswith("D"):
        return timedelta(days=amount)
    raise ValueError(f"Unsupported horizon {horizon!r}")


def _direction_label(raw_return: float | None, threshold: float) -> str:
    if raw_return is None:
        return "unknown"
    if raw_return >= threshold:
        return "up"
    if raw_return <= -threshold:
        return "down"
    return "flat"


def _latest_index_leq(bars: list[DailyBar], target: date) -> int | None:
    answer: int | None = None
    for index, bar in enumerate(bars):
        if bar.date <= target:
            answer = index
        else:
            break
    return answer


def _latest_index_lt(bars: list[DailyBar], target: date) -> int | None:
    answer: int | None = None
    for index, bar in enumerate(bars):
        if bar.date < target:
            answer = index
        else:
            break
    return answer


def _bar_by_date(bars: list[DailyBar], target: date) -> DailyBar | None:
    for bar in bars:
        if bar.date == target:
            return bar
    return None


def _latest_intraday_leq(bars: list[IntradayBar], target: datetime) -> IntradayBar | None:
    answer: IntradayBar | None = None
    for bar in bars:
        timestamp = bar.timestamp.astimezone(timezone.utc)
        if timestamp <= target:
            answer = bar
        else:
            break
    return answer


def _first_intraday_geq(bars: list[IntradayBar], target: datetime) -> IntradayBar | None:
    for bar in bars:
        if bar.timestamp.astimezone(timezone.utc) >= target:
            return bar
    return None


class ReplayMarketOutcomeBuilder:
    def __init__(self, database: Any, price_source: DailyPriceSource | None = None) -> None:
        self.database = database
        self.price_source = price_source or YahooChartDailyPriceSource()

    def build(
        self,
        *,
        start: str | None = None,
        end: str | None = None,
        event_type: str | None = None,
        horizons: tuple[str, ...] = ("1D", "5D"),
        benchmark_symbol: str = "SPY",
        limit: int = 1000,
        flat_threshold: float = 0.005,
    ) -> dict[str, Any]:
        parsed_horizons = parse_horizons(horizons)
        daily_horizons = tuple(h for h in parsed_horizons if _is_daily_horizon(h))
        intraday_horizons = tuple(h for h in parsed_horizons if not _is_daily_horizon(h))
        if daily_horizons and intraday_horizons:
            raise ValueError("Do not mix daily and intraday horizons in one build; run separate builds.")
        rows = self.database.replay_market_outcome_candidates(
            start=start,
            end=end,
            event_type=event_type,
            limit=limit,
        )
        summary: dict[str, Any] = {
            "ingestion_version": REPLAY_OUTCOME_VERSION,
            "price_source": self.price_source.name,
            "start": start,
            "end": end,
            "event_type": event_type,
            "horizons": list(parsed_horizons),
            "candidate_count": len(rows),
            "created": 0,
            "updated": 0,
            "completed": 0,
            "blocked": 0,
            "price_source_errors": {},
            "rows": [],
        }
        if not rows:
            return summary
        if intraday_horizons:
            if not hasattr(self.price_source, "get_intraday_bars"):
                summary["blocked"] = len(rows) * len(intraday_horizons)
                summary["source_data_available"] = False
                summary["source_blocker_reason"] = "price_source_does_not_support_intraday_bars"
                return summary
            return self._build_intraday_rows(
                rows=rows,
                horizons=intraday_horizons,
                benchmark_symbol=benchmark_symbol,
                flat_threshold=flat_threshold,
                summary=summary,
            )

        symbols = sorted({str(row["actual_subject_symbol"]).upper() for row in rows})
        benchmark = benchmark_symbol.strip().upper()
        event_dates = [parse_utc(str(row["replay_timestamp"])).date() for row in rows]
        fetch_start = min(event_dates) - timedelta(days=10)
        fetch_end = max(event_dates) + timedelta(days=max(_horizon_days(h) for h in parsed_horizons) + 14)
        try:
            bar_cache: dict[str, list[DailyBar]] = {}
            for symbol in symbols:
                try:
                    bar_cache[symbol] = self.price_source.get_daily_bars(symbol, fetch_start, fetch_end)
                except Exception as exc:  # pragma: no cover - exercised by live connectors
                    bar_cache[symbol] = []
                    summary["price_source_errors"][symbol] = str(exc)
            if benchmark:
                try:
                    benchmark_bars = self.price_source.get_daily_bars(benchmark, fetch_start, fetch_end)
                except Exception as exc:  # pragma: no cover - exercised by live connectors
                    benchmark_bars = []
                    summary["price_source_errors"][benchmark] = str(exc)
            else:
                benchmark_bars = []

            if not any(bar_cache.values()) and not benchmark_bars:
                summary["blocked"] = len(rows) * len(parsed_horizons)
                summary["source_data_available"] = False
                summary["source_blocker_reason"] = "price_source_returned_no_daily_bars"
                return summary

            summary["source_data_available"] = True
            for row in rows:
                symbol = str(row["actual_subject_symbol"]).upper()
                bars = bar_cache.get(symbol, [])
                for horizon in parsed_horizons:
                    outcome = self._build_outcome(
                        row,
                        bars=bars,
                        benchmark_bars=benchmark_bars,
                        benchmark_symbol=benchmark,
                        horizon=horizon,
                        flat_threshold=flat_threshold,
                    )
                    outcome_id, created = self.database.upsert_replay_market_outcome(outcome)
                    if created:
                        summary["created"] += 1
                    else:
                        summary["updated"] += 1
                    if outcome.price_status == "complete":
                        summary["completed"] += 1
                    else:
                        summary["blocked"] += 1
                    summary["rows"].append(
                        {
                            "outcome_id": outcome_id,
                            "replay_corpus_id": outcome.replay_corpus_id,
                            "symbol": outcome.symbol,
                            "horizon": outcome.horizon,
                            "baseline_at": outcome.baseline_at,
                            "reaction_at": outcome.reaction_at,
                            "raw_return": outcome.raw_return,
                            "benchmark_return": outcome.benchmark_return,
                            "excess_return": outcome.excess_return,
                            "direction_label": outcome.direction_label,
                            "price_status": outcome.price_status,
                            "blocker_reason": outcome.blocker_reason,
                        }
                    )
            return summary
        finally:
            close = getattr(self.price_source, "close", None)
            if callable(close):
                close()

    def _build_intraday_rows(
        self,
        *,
        rows: list[dict[str, Any]],
        horizons: tuple[str, ...],
        benchmark_symbol: str,
        flat_threshold: float,
        summary: dict[str, Any],
    ) -> dict[str, Any]:
        benchmark = benchmark_symbol.strip().upper()
        max_delta = max(_horizon_delta(h) for h in horizons)
        any_data = False
        pending: list[ReplayMarketOutcome] = []
        try:
            for row in rows:
                symbol = str(row["actual_subject_symbol"]).upper()
                event_dt = parse_utc(str(row["replay_timestamp"]))
                fetch_start = event_dt - timedelta(hours=8)
                fetch_end = event_dt + max_delta + timedelta(hours=8)
                try:
                    bars = getattr(self.price_source, "get_intraday_bars")(symbol, fetch_start, fetch_end)
                except Exception as exc:  # pragma: no cover - exercised by live connectors
                    bars = []
                    summary["price_source_errors"][symbol] = str(exc)
                if benchmark:
                    try:
                        benchmark_bars = getattr(self.price_source, "get_intraday_bars")(benchmark, fetch_start, fetch_end)
                    except Exception as exc:  # pragma: no cover - exercised by live connectors
                        benchmark_bars = []
                        summary["price_source_errors"][benchmark] = str(exc)
                else:
                    benchmark_bars = []
                any_data = any_data or bool(bars) or bool(benchmark_bars)
                for horizon in horizons:
                    outcome = self._build_intraday_outcome(
                        row,
                        bars=bars,
                        benchmark_bars=benchmark_bars,
                        benchmark_symbol=benchmark,
                        horizon=horizon,
                        flat_threshold=flat_threshold,
                    )
                    pending.append(outcome)
            summary["source_data_available"] = any_data
            if not any_data:
                summary["blocked"] = len(rows) * len(horizons)
                summary["source_blocker_reason"] = "price_source_returned_no_intraday_bars"
                return summary
            for outcome in pending:
                outcome_id, created = self.database.upsert_replay_market_outcome(outcome)
                if created:
                    summary["created"] += 1
                else:
                    summary["updated"] += 1
                if outcome.price_status == "complete":
                    summary["completed"] += 1
                else:
                    summary["blocked"] += 1
                summary["rows"].append(
                    {
                        "outcome_id": outcome_id,
                        "replay_corpus_id": outcome.replay_corpus_id,
                        "symbol": outcome.symbol,
                        "horizon": outcome.horizon,
                        "baseline_at": outcome.baseline_at,
                        "reaction_at": outcome.reaction_at,
                        "raw_return": outcome.raw_return,
                        "benchmark_return": outcome.benchmark_return,
                        "excess_return": outcome.excess_return,
                        "direction_label": outcome.direction_label,
                        "price_status": outcome.price_status,
                        "blocker_reason": outcome.blocker_reason,
                    }
                )
            return summary
        finally:
            close = getattr(self.price_source, "close", None)
            if callable(close):
                close()

    def _build_intraday_outcome(
        self,
        row: dict[str, Any],
        *,
        bars: list[IntradayBar],
        benchmark_bars: list[IntradayBar],
        benchmark_symbol: str,
        horizon: str,
        flat_threshold: float,
    ) -> ReplayMarketOutcome:
        symbol = str(row["actual_subject_symbol"]).upper()
        event_dt = parse_utc(str(row["replay_timestamp"]))
        reaction_target = event_dt + _horizon_delta(horizon)
        blockers: list[str] = []
        baseline_bar = _latest_intraday_leq(bars, event_dt)
        reaction_bar = _first_intraday_geq(bars, reaction_target)
        if baseline_bar is None:
            blockers.append("baseline_price_unavailable")
        if reaction_bar is None:
            blockers.append("reaction_price_unavailable")

        raw_return: float | None = None
        if baseline_bar is not None and reaction_bar is not None and baseline_bar.close > 0:
            raw_return = reaction_bar.close / baseline_bar.close - 1.0

        benchmark_baseline = None
        benchmark_reaction = None
        benchmark_return = None
        excess_return = None
        if benchmark_symbol and baseline_bar is not None and reaction_bar is not None:
            benchmark_baseline = _latest_intraday_leq(benchmark_bars, baseline_bar.timestamp)
            benchmark_reaction = _first_intraday_geq(benchmark_bars, reaction_bar.timestamp)
            if benchmark_baseline is None or benchmark_reaction is None:
                blockers.append("benchmark_price_unavailable")
            elif benchmark_baseline.close > 0:
                benchmark_return = benchmark_reaction.close / benchmark_baseline.close - 1.0
                if raw_return is not None:
                    excess_return = raw_return - benchmark_return

        status = "complete" if raw_return is not None else "blocked"
        return ReplayMarketOutcome(
            replay_corpus_id=int(row["id"]),
            historical_event_id=int(row["historical_event_id"]),
            symbol=symbol,
            benchmark_symbol=benchmark_symbol,
            horizon=horizon,
            baseline_at=baseline_bar.timestamp.isoformat() if baseline_bar else None,
            reaction_at=reaction_bar.timestamp.isoformat() if reaction_bar else None,
            baseline_price=baseline_bar.close if baseline_bar else None,
            reaction_price=reaction_bar.close if reaction_bar else None,
            raw_return=raw_return,
            benchmark_baseline_price=benchmark_baseline.close if benchmark_baseline else None,
            benchmark_reaction_price=benchmark_reaction.close if benchmark_reaction else None,
            benchmark_return=benchmark_return,
            excess_return=excess_return,
            direction_label=_direction_label(raw_return, flat_threshold),
            price_source=self.price_source.name,
            price_status=status,
            blocker_reason=";".join(dict.fromkeys(blockers)),
        )

    def _build_outcome(
        self,
        row: dict[str, Any],
        *,
        bars: list[DailyBar],
        benchmark_bars: list[DailyBar],
        benchmark_symbol: str,
        horizon: str,
        flat_threshold: float,
    ) -> ReplayMarketOutcome:
        symbol = str(row["actual_subject_symbol"]).upper()
        event_dt = parse_utc(str(row["replay_timestamp"]))
        event_date = event_dt.date()
        session = str(row.get("session") or row.get("market_session") or "unknown").lower()
        baseline_index = (
            _latest_index_leq(bars, event_date)
            if session == "after_hours"
            else _latest_index_lt(bars, event_date)
        )
        blockers: list[str] = []
        baseline_bar: DailyBar | None = None
        reaction_bar: DailyBar | None = None
        if baseline_index is None:
            blockers.append("baseline_price_unavailable")
        else:
            baseline_bar = bars[baseline_index]
            reaction_index = baseline_index + _horizon_days(horizon)
            if reaction_index >= len(bars):
                blockers.append("reaction_price_unavailable")
            else:
                reaction_bar = bars[reaction_index]

        raw_return: float | None = None
        if baseline_bar is not None and reaction_bar is not None and baseline_bar.close > 0:
            raw_return = reaction_bar.close / baseline_bar.close - 1.0

        benchmark_baseline: DailyBar | None = None
        benchmark_reaction: DailyBar | None = None
        benchmark_return: float | None = None
        excess_return: float | None = None
        if benchmark_symbol and baseline_bar is not None and reaction_bar is not None:
            benchmark_baseline = _bar_by_date(benchmark_bars, baseline_bar.date)
            benchmark_reaction = _bar_by_date(benchmark_bars, reaction_bar.date)
            if benchmark_baseline is None or benchmark_reaction is None:
                blockers.append("benchmark_price_unavailable")
            elif benchmark_baseline.close > 0:
                benchmark_return = benchmark_reaction.close / benchmark_baseline.close - 1.0
                if raw_return is not None:
                    excess_return = raw_return - benchmark_return

        status = "complete" if raw_return is not None else "blocked"
        return ReplayMarketOutcome(
            replay_corpus_id=int(row["id"]),
            historical_event_id=int(row["historical_event_id"]),
            symbol=symbol,
            benchmark_symbol=benchmark_symbol,
            horizon=horizon,
            baseline_at=baseline_bar.date.isoformat() if baseline_bar else None,
            reaction_at=reaction_bar.date.isoformat() if reaction_bar else None,
            baseline_price=baseline_bar.close if baseline_bar else None,
            reaction_price=reaction_bar.close if reaction_bar else None,
            raw_return=raw_return,
            benchmark_baseline_price=benchmark_baseline.close if benchmark_baseline else None,
            benchmark_reaction_price=benchmark_reaction.close if benchmark_reaction else None,
            benchmark_return=benchmark_return,
            excess_return=excess_return,
            direction_label=_direction_label(raw_return, flat_threshold),
            price_source=self.price_source.name,
            price_status=status,
            blocker_reason=";".join(dict.fromkeys(blockers)),
        )
