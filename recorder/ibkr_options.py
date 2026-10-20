from __future__ import annotations

import json
import logging
import math
import threading
import time
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from ibapi.client import EClient
from ibapi.contract import Contract
from ibapi.ticktype import TickTypeEnum
from ibapi.wrapper import EWrapper


IBKR_OPTION_CHAIN_SCAN_VERSION = "ibkr-option-chain-scan-v1"
IBKR_OPTION_QUOTE_SNAPSHOT_VERSION = "ibkr-option-quote-snapshot-v1"


@dataclass(frozen=True)
class OptionChainScanConfig:
    host: str = "127.0.0.1"
    port: int = 4001
    client_id: int = 6021
    symbols: tuple[str, ...] = ("NVDA",)
    reference_prices: dict[str, float] | None = None
    underlying_sec_types: dict[str, str] | None = None
    underlying_exchanges: dict[str, str] | None = None
    min_dte: int = 7
    max_dte: int = 60
    strikes_around: int = 2
    max_expirations: int = 4
    timeout_seconds: float = 20.0


@dataclass(frozen=True)
class OptionQuoteSnapshotConfig:
    host: str = "127.0.0.1"
    port: int = 4001
    client_id: int = 7021
    symbols: tuple[str, ...] = ("NVDA",)
    reference_prices: dict[str, float] | None = None
    underlying_sec_types: dict[str, str] | None = None
    underlying_exchanges: dict[str, str] | None = None
    min_dte: int = 7
    max_dte: int = 60
    strikes_around: int = 1
    max_expirations: int = 2
    max_quote_contracts: int = 20
    market_data_type: int = 1
    generic_ticks: str = "100,101,104,106"
    chain_timeout_seconds: float = 15.0
    quote_seconds: float = 8.0
    max_spread_pct: float = 0.25


def parse_reference_prices(text: str | None) -> dict[str, float]:
    if not text:
        return {}
    values: dict[str, float] = {}
    for item in text.split(","):
        stripped = item.strip()
        if not stripped:
            continue
        if "=" not in stripped:
            raise ValueError("Reference prices must be SYMBOL=PRICE pairs, e.g. NVDA=125,TSLA=250")
        symbol, price = stripped.split("=", 1)
        values[symbol.strip().upper()] = float(price.strip())
    return values


def parse_symbol_values(text: str | None) -> dict[str, str]:
    if not text:
        return {}
    values: dict[str, str] = {}
    for item in text.split(","):
        stripped = item.strip()
        if not stripped:
            continue
        if "=" not in stripped:
            raise ValueError("Symbol values must be SYMBOL=VALUE pairs, e.g. SPX=IND,NDX=IND")
        symbol, value = stripped.split("=", 1)
        values[symbol.strip().upper()] = value.strip().upper()
    return values


def _underlying_contract(symbol: str, *, sec_type: str = "STK", exchange: str = "SMART") -> Contract:
    contract = Contract()
    contract.symbol = symbol
    contract.secType = sec_type
    contract.exchange = exchange
    contract.currency = "USD"
    return contract


def _option_contract_template(
    *,
    symbol: str,
    expiration: str,
    strike: float,
    right: str,
    exchange: str,
    trading_class: str,
    multiplier: str,
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "secType": "OPT",
        "exchange": exchange or "SMART",
        "currency": "USD",
        "lastTradeDateOrContractMonth": expiration,
        "strike": float(strike),
        "right": right,
        "multiplier": str(multiplier or "100"),
        "tradingClass": trading_class or symbol,
    }


def _parse_expiration(expiration: str) -> date | None:
    value = str(expiration or "").strip()
    for fmt in ("%Y%m%d", "%Y%m"):
        try:
            parsed = datetime.strptime(value, fmt)
            return parsed.date()
        except ValueError:
            continue
    return None


def _dte(expiration: str, *, as_of: date | None = None) -> int | None:
    parsed = _parse_expiration(expiration)
    if parsed is None:
        return None
    base = as_of or datetime.now(UTC).date()
    return (parsed - base).days


def _nearest_strikes(strikes: list[float], reference_price: float, around: int) -> list[float]:
    cleaned = sorted({float(strike) for strike in strikes if strike and math.isfinite(float(strike))})
    if not cleaned:
        return []
    reference_price = float(reference_price)
    nearest_index = min(range(len(cleaned)), key=lambda index: abs(cleaned[index] - reference_price))
    start = max(0, nearest_index - max(0, int(around)))
    end = min(len(cleaned), nearest_index + max(0, int(around)) + 1)
    return cleaned[start:end]


def _chain_metadata_score(chain: dict[str, Any], symbol: str) -> int:
    exchange = str(chain.get("exchange") or "").upper()
    trading_class = str(chain.get("trading_class") or "").upper()
    score = 0
    if exchange in {"SMART", "CBOE", "BOX", "ISE", "PHLX", "AMEX", "NYSE", "NASDAQ"}:
        score += 2
    if trading_class == str(symbol).upper():
        score += 3
    elif trading_class:
        score += 1
    if str(chain.get("multiplier") or "") == "100":
        score += 1
    return score


def select_option_chain_candidates(
    chains: list[dict[str, Any]],
    *,
    reference_prices: dict[str, float] | None = None,
    min_dte: int = 7,
    max_dte: int = 60,
    strikes_around: int = 2,
    max_expirations: int = 4,
    as_of: date | None = None,
) -> list[dict[str, Any]]:
    reference_prices = {str(key).upper(): float(value) for key, value in (reference_prices or {}).items()}
    candidates: list[dict[str, Any]] = []
    seen: set[tuple[str, str, float, str, str, str, str]] = set()
    chain_rows = sorted(
        [chain for chain in chains if str(chain.get("symbol") or "").strip()],
        key=lambda chain: (
            str(chain.get("symbol") or "").upper(),
            -_chain_metadata_score(chain, str(chain.get("symbol") or "").upper()),
            str(chain.get("exchange") or ""),
            str(chain.get("trading_class") or ""),
        ),
    )
    expirations_seen_by_symbol: dict[str, set[str]] = {}
    for chain in chains:
        symbol = str(chain.get("symbol") or "").upper()
        if not symbol:
            continue
        expirations_seen_by_symbol.setdefault(symbol, set())
    for chain in chain_rows:
        symbol = str(chain.get("symbol") or "").upper()
        strikes = [float(value) for value in chain.get("strikes") or [] if value is not None]
        if not strikes:
            continue
        reference_price = reference_prices.get(symbol)
        if reference_price is None:
            reference_price = strikes[len(strikes) // 2]
        expirations = []
        for expiration in sorted(str(chain_exp) for chain_exp in (chain.get("expirations") or [])):
            days = _dte(expiration, as_of=as_of)
            if days is None or days < int(min_dte) or days > int(max_dte):
                continue
            if len(expirations_seen_by_symbol.setdefault(symbol, set())) >= max(0, int(max_expirations)) and expiration not in expirations_seen_by_symbol[symbol]:
                continue
            expirations.append((expiration, days))
        expirations = expirations[: max(0, int(max_expirations))]
        selected_strikes = _nearest_strikes(strikes, reference_price, strikes_around)
        for expiration, days in expirations:
            expirations_seen_by_symbol.setdefault(symbol, set()).add(expiration)
            for strike in selected_strikes:
                for right in ("C", "P"):
                    exchange = str(chain.get("exchange") or "SMART")
                    trading_class = str(chain.get("trading_class") or symbol)
                    multiplier = str(chain.get("multiplier") or "100")
                    dedupe_key = (symbol, expiration, float(strike), right, exchange, trading_class, multiplier)
                    if dedupe_key in seen:
                        continue
                    seen.add(dedupe_key)
                    moneyness = (float(strike) - float(reference_price)) / float(reference_price) if reference_price else None
                    candidates.append(
                        {
                            "symbol": symbol,
                            "expiration": expiration,
                            "dte": days,
                            "strike": float(strike),
                            "right": right,
                            "reference_price": float(reference_price),
                            "moneyness": moneyness,
                            "exchange": exchange,
                            "trading_class": trading_class,
                            "multiplier": multiplier,
                            "underlying_con_id": chain.get("underlying_con_id"),
                            "chain_metadata_score": _chain_metadata_score(chain, symbol),
                            "candidate_contract": _option_contract_template(
                                symbol=symbol,
                                expiration=expiration,
                                strike=float(strike),
                                right=right,
                                exchange=exchange,
                                trading_class=trading_class,
                                multiplier=multiplier,
                            ),
                            "safety": {
                                "paper_only": True,
                                "read_only_chain_scan": True,
                                "market_data_requested": False,
                                "ibkr_order_submitted": False,
                            },
                        }
                    )
    candidates.sort(
        key=lambda row: (
            row["symbol"],
            int(row["dte"]),
            abs(float(row["moneyness"] or 0.0)),
            float(row["strike"]),
            row["right"],
        )
    )
    return candidates


class IBKROptionChainScannerApp(EWrapper, EClient):
    def __init__(self, config: OptionChainScanConfig):
        EClient.__init__(self, self)
        self.config = config
        self.log = logging.getLogger(self.__class__.__name__)
        self._next_req_id = 700000
        self._api_thread: threading.Thread | None = None
        self._connected = threading.Event()
        self._done = threading.Event()
        self._contract_req_to_symbol: dict[int, str] = {}
        self._chain_req_to_symbol: dict[int, str] = {}
        self._underlying_contracts: dict[str, dict[str, Any]] = {}
        self._chain_rows: list[dict[str, Any]] = []
        self._completed_chain_reqs: set[int] = set()
        self._errors: list[dict[str, Any]] = []

    def _request_id(self) -> int:
        self._next_req_id += 1
        return self._next_req_id

    def nextValidId(self, orderId: int) -> None:  # noqa: N802
        self._connected.set()
        self.log.info("IBKR option-chain scanner connected; next valid order id=%s (unused)", orderId)
        for symbol in self.config.symbols:
            symbol = symbol.upper()
            request_id = self._request_id()
            self._contract_req_to_symbol[request_id] = symbol
            sec_type = str((self.config.underlying_sec_types or {}).get(symbol) or "STK").upper()
            exchange = str((self.config.underlying_exchanges or {}).get(symbol) or ("CBOE" if sec_type == "IND" else "SMART")).upper()
            self.reqContractDetails(request_id, _underlying_contract(symbol, sec_type=sec_type, exchange=exchange))

    def contractDetails(self, reqId: int, contractDetails) -> None:  # noqa: N802
        symbol = self._contract_req_to_symbol.get(reqId)
        if not symbol or symbol in self._underlying_contracts:
            return
        contract = contractDetails.contract
        sec_type = str((self.config.underlying_sec_types or {}).get(symbol) or getattr(contract, "secType", "") or "STK").upper()
        self._underlying_contracts[symbol] = {
            "symbol": symbol,
            "con_id": int(getattr(contract, "conId", 0) or 0),
            "sec_type": sec_type,
            "exchange": str(getattr(contract, "exchange", "") or ""),
            "primary_exchange": str(getattr(contract, "primaryExchange", "") or ""),
            "currency": str(getattr(contract, "currency", "") or "USD"),
            "trading_class": str(getattr(contract, "tradingClass", "") or symbol),
        }

    def contractDetailsEnd(self, reqId: int) -> None:  # noqa: N802
        symbol = self._contract_req_to_symbol.get(reqId)
        if not symbol:
            return
        underlying = self._underlying_contracts.get(symbol)
        if not underlying:
            self._errors.append({"symbol": symbol, "stage": "contract_details", "message": "No underlying contract resolved"})
            self._maybe_done()
            return
        chain_req_id = self._request_id()
        self._chain_req_to_symbol[chain_req_id] = symbol
        self.reqSecDefOptParams(
            chain_req_id,
            symbol,
            "",
            str(underlying.get("sec_type") or "STK"),
            int(underlying["con_id"]),
        )

    def securityDefinitionOptionParameter(  # noqa: N802
        self,
        reqId: int,
        exchange: str,
        underlyingConId: int,
        tradingClass: str,
        multiplier: str,
        expirations,
        strikes,
    ) -> None:
        symbol = self._chain_req_to_symbol.get(reqId, "")
        self._chain_rows.append(
            {
                "symbol": symbol,
                "exchange": exchange,
                "underlying_con_id": int(underlyingConId or 0),
                "trading_class": tradingClass,
                "multiplier": multiplier,
                "expirations": sorted(str(item) for item in expirations),
                "strikes": sorted(float(item) for item in strikes if item is not None),
            }
        )

    def securityDefinitionOptionParameterEnd(self, reqId: int) -> None:  # noqa: N802
        self._completed_chain_reqs.add(reqId)
        self._maybe_done()

    def error(self, reqId: int, errorCode: int, errorString: str, advancedOrderRejectJson: str = "") -> None:  # noqa: N802
        if errorCode in {2104, 2106, 2158}:
            return
        symbol = self._contract_req_to_symbol.get(reqId) or self._chain_req_to_symbol.get(reqId)
        self._errors.append(
            {
                "req_id": reqId,
                "symbol": symbol,
                "code": int(errorCode),
                "message": str(errorString),
                "advanced": advancedOrderRejectJson or "",
            }
        )
        if reqId in self._chain_req_to_symbol:
            self._completed_chain_reqs.add(reqId)
            self._maybe_done()

    def _maybe_done(self) -> None:
        expected = len(self.config.symbols)
        completed = len(self._completed_chain_reqs)
        failed_underlyings = len(
            {
                item.get("symbol")
                for item in self._errors
                if item.get("stage") == "contract_details"
            }
        )
        if completed + failed_underlyings >= expected:
            self._done.set()

    def start(self) -> None:
        self.connect(self.config.host, int(self.config.port), clientId=int(self.config.client_id))
        self._api_thread = threading.Thread(target=self.run, name="ibkr-option-chain-scanner", daemon=True)
        self._api_thread.start()
        if not self._connected.wait(min(10.0, float(self.config.timeout_seconds))):
            raise TimeoutError("Timed out waiting for IBKR option-chain scanner connection")

    def stop(self) -> None:
        try:
            if self.isConnected():
                self.disconnect()
        finally:
            if self._api_thread and self._api_thread.is_alive():
                self._api_thread.join(timeout=3.0)

    def wait(self) -> dict[str, Any]:
        self._done.wait(max(0.1, float(self.config.timeout_seconds)))
        candidates = select_option_chain_candidates(
            self._chain_rows,
            reference_prices=self.config.reference_prices,
            min_dte=self.config.min_dte,
            max_dte=self.config.max_dte,
            strikes_around=self.config.strikes_around,
            max_expirations=self.config.max_expirations,
        )
        return option_chain_scan_summary(
            symbols=self.config.symbols,
            underlying_contracts=list(self._underlying_contracts.values()),
            chains=self._chain_rows,
            candidates=candidates,
            errors=self._errors,
            config=self.config,
            completed=self._done.is_set(),
        )


def option_chain_scan_summary(
    *,
    symbols: tuple[str, ...],
    underlying_contracts: list[dict[str, Any]],
    chains: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    errors: list[dict[str, Any]] | None,
    config: OptionChainScanConfig,
    completed: bool,
) -> dict[str, Any]:
    by_symbol: list[dict[str, Any]] = []
    for symbol in symbols:
        symbol = symbol.upper()
        symbol_chains = [chain for chain in chains if str(chain.get("symbol") or "").upper() == symbol]
        symbol_candidates = [row for row in candidates if row.get("symbol") == symbol]
        expirations = sorted({exp for chain in symbol_chains for exp in (chain.get("expirations") or [])})
        strikes = sorted({float(strike) for chain in symbol_chains for strike in (chain.get("strikes") or [])})
        by_symbol.append(
            {
                "symbol": symbol,
                "chain_rows": len(symbol_chains),
                "expirations": len(expirations),
                "strikes": len(strikes),
                "candidate_contracts": len(symbol_candidates),
                "nearest_expirations": expirations[: int(config.max_expirations)],
                "min_strike": min(strikes) if strikes else None,
                "max_strike": max(strikes) if strikes else None,
            }
        )
    return {
        "scan_version": IBKR_OPTION_CHAIN_SCAN_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "completed": completed,
        "symbols": list(symbols),
        "underlying_contracts": underlying_contracts,
        "chain_rows": chains,
        "candidate_contracts": candidates,
        "by_symbol": by_symbol,
        "errors": errors or [],
        "config": {
            "host": config.host,
            "port": config.port,
            "client_id": config.client_id,
            "symbols": list(config.symbols),
            "reference_prices": config.reference_prices or {},
            "underlying_sec_types": config.underlying_sec_types or {},
            "underlying_exchanges": config.underlying_exchanges or {},
            "min_dte": config.min_dte,
            "max_dte": config.max_dte,
            "strikes_around": config.strikes_around,
            "max_expirations": config.max_expirations,
            "timeout_seconds": config.timeout_seconds,
        },
        "safety": {
            "mode": "read_only_ibkr_option_chain_scan",
            "market_data_requested": False,
            "ibkr_order_submitted": False,
            "account_or_portfolio_checked": False,
            "policy": "Uses reqContractDetails and reqSecDefOptParams only; no quotes, Greeks, margin, liquidity, account, portfolio, or order submission.",
        },
    }


def run_ibkr_option_chain_scan(config: OptionChainScanConfig) -> dict[str, Any]:
    app = IBKROptionChainScannerApp(config)
    app.start()
    try:
        return app.wait()
    finally:
        app.stop()


def option_chain_scan_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# IBKR Option Chain Read-Only Scan Report",
        "",
        f"- Generated at: `{summary.get('generated_at')}`",
        f"- Completed: `{summary.get('completed')}`",
        f"- Symbols: `{', '.join(summary.get('symbols') or [])}`",
        f"- Chain rows: `{len(summary.get('chain_rows') or [])}`",
        f"- Candidate contracts: `{len(summary.get('candidate_contracts') or [])}`",
        f"- Errors: `{len(summary.get('errors') or [])}`",
        "- Safety: read-only option-chain definition scan only; no quotes, account, margin, portfolio, or order submission.",
        "",
        "## Config",
        "",
    ]
    for key, value in (summary.get("config") or {}).items():
        if key in {"host", "port"}:
            continue
        lines.append(f"- `{key}`: `{value}`")
    lines.extend(
        [
            "",
            "## By symbol",
            "",
            "| Symbol | Chain rows | Expirations | Strikes | Candidate contracts | Nearest expirations | Strike range |",
            "| --- | ---: | ---: | ---: | ---: | --- | --- |",
        ]
    )
    for row in summary.get("by_symbol") or []:
        strike_range = (
            f"{float(row['min_strike']):.2f} - {float(row['max_strike']):.2f}"
            if row.get("min_strike") is not None and row.get("max_strike") is not None
            else "-"
        )
        lines.append(
            f"| {row.get('symbol')} | {row.get('chain_rows')} | {row.get('expirations')} "
            f"| {row.get('strikes')} | {row.get('candidate_contracts')} "
            f"| {', '.join(row.get('nearest_expirations') or []) or '-'} | {strike_range} |"
        )
    lines.extend(
        [
            "",
            "## Sample candidate contracts",
            "",
            "| Symbol | Expiration | DTE | Right | Strike | Moneyness | Exchange | Trading class | Multiplier |",
            "| --- | --- | ---: | --- | ---: | ---: | --- | --- | ---: |",
        ]
    )
    for row in (summary.get("candidate_contracts") or [])[:40]:
        lines.append(
            f"| {row.get('symbol')} | {row.get('expiration')} | {row.get('dte')} "
            f"| {row.get('right')} | {float(row.get('strike') or 0.0):.2f} "
            f"| {float(row.get('moneyness') or 0.0) * 100:+.2f}% "
            f"| {row.get('exchange')} | {row.get('trading_class')} | {row.get('multiplier')} |"
        )
    if summary.get("errors"):
        lines.extend(
            [
                "",
                "## Errors / warnings",
                "",
                "| Symbol | Req id | Code | Message |",
                "| --- | ---: | ---: | --- |",
            ]
        )
        for error in summary.get("errors") or []:
            lines.append(
                f"| {error.get('symbol') or '-'} | {error.get('req_id') or '-'} "
                f"| {error.get('code') or '-'} | {str(error.get('message') or '').replace('|', '/')} |"
            )
    lines.extend(
        [
            "",
            "## Interpretation note",
            "",
            "This scanner only proves that IBKR can resolve option-chain definitions and that the system can select candidate option contracts around a reference price. It does not request bid/ask/IV/open interest/volume yet and is not execution-ready.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_option_chain_scan_markdown(output_path: Path, summary: dict[str, Any]) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(option_chain_scan_markdown(summary), encoding="utf-8")
    return output_path


def write_option_chain_scan_json(output_path: Path, summary: dict[str, Any]) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    return output_path


def _tick_type_name(tick_type: int) -> str:
    for method_name in ("toStr", "to_str", "to_Str"):
        method = getattr(TickTypeEnum, method_name, None)
        if method is not None:
            try:
                return str(method(tick_type))
            except Exception:
                pass
    return str(tick_type)


def option_contract_from_template(template: dict[str, Any]) -> Contract:
    contract = Contract()
    contract.symbol = str(template.get("symbol") or "")
    contract.secType = str(template.get("secType") or "OPT")
    contract.exchange = str(template.get("exchange") or "SMART")
    contract.currency = str(template.get("currency") or "USD")
    contract.lastTradeDateOrContractMonth = str(template.get("lastTradeDateOrContractMonth") or "")
    contract.strike = float(template.get("strike") or 0.0)
    contract.right = str(template.get("right") or "")
    contract.multiplier = str(template.get("multiplier") or "100")
    contract.tradingClass = str(template.get("tradingClass") or template.get("symbol") or "")
    return contract


def _quote_quality(quote: dict[str, Any], *, max_spread_pct: float) -> dict[str, Any]:
    bid = quote.get("bid")
    ask = quote.get("ask")
    last = quote.get("last")
    close = quote.get("close")
    mid = None
    spread = None
    spread_pct = None
    if bid is not None and ask is not None and float(bid) > 0.0 and float(ask) > 0.0 and float(ask) >= float(bid):
        mid = (float(bid) + float(ask)) / 2.0
        spread = float(ask) - float(bid)
        spread_pct = spread / mid if mid else None
        status = "quoted" if spread_pct is not None and spread_pct <= float(max_spread_pct) else "wide_spread"
    elif last is not None and float(last) > 0.0:
        status = "last_only"
    elif close is not None and float(close) > 0.0:
        status = "close_only"
    else:
        status = "no_quote"
    return {
        "quote_status": status,
        "mid": mid,
        "spread": spread,
        "spread_pct": spread_pct,
        "has_bid_ask": mid is not None,
        "max_spread_pct": float(max_spread_pct),
    }


def _candidate_dte_bucket(candidate: dict[str, Any]) -> str:
    try:
        dte = int(candidate.get("dte"))
    except (TypeError, ValueError):
        days = _dte(str(candidate.get("expiration") or candidate.get("expiry") or ""))
        dte = int(days) if days is not None else 9999
    if dte <= 0:
        return "0dte"
    if dte <= 7:
        return "weekly_1_7dte"
    if dte <= 35:
        return "front_month_8_35dte"
    return "longer_36dte_plus"


def _dte_bucket_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        bucket = _candidate_dte_bucket(row)
        counts[bucket] = counts.get(bucket, 0) + 1
    return counts


def limit_option_candidates_by_symbol(candidates: list[dict[str, Any]], max_count: int) -> list[dict[str, Any]]:
    if max_count <= 0:
        return []
    grouped: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for candidate in candidates:
        symbol = str(candidate.get("symbol") or "").upper()
        bucket = _candidate_dte_bucket(candidate)
        grouped.setdefault(symbol, {}).setdefault(bucket, []).append(candidate)
    selected: list[dict[str, Any]] = []
    symbols = sorted(grouped)
    bucket_order = ["0dte", "weekly_1_7dte", "front_month_8_35dte", "longer_36dte_plus"]
    index = 0
    while len(selected) < max_count:
        appended = False
        for symbol in symbols:
            symbol_groups = grouped.get(symbol) or {}
            for bucket in bucket_order:
                rows = symbol_groups.get(bucket) or []
                if index < len(rows):
                    selected.append(rows[index])
                    appended = True
                    if len(selected) >= max_count:
                        break
            if len(selected) >= max_count:
                break
        if not appended:
            break
        index += 1
    return selected


class IBKROptionQuoteSnapshotApp(EWrapper, EClient):
    def __init__(self, config: OptionQuoteSnapshotConfig, candidates: list[dict[str, Any]]):
        EClient.__init__(self, self)
        self.config = config
        self.candidates = candidates[: max(0, int(config.max_quote_contracts))]
        self.log = logging.getLogger(self.__class__.__name__)
        self._next_req_id = 800000
        self._api_thread: threading.Thread | None = None
        self._connected = threading.Event()
        self._req_to_candidate: dict[int, dict[str, Any]] = {}
        self._quotes: dict[int, dict[str, Any]] = {}
        self._errors: list[dict[str, Any]] = []

    def _request_id(self) -> int:
        self._next_req_id += 1
        return self._next_req_id

    def nextValidId(self, orderId: int) -> None:  # noqa: N802
        self._connected.set()
        self.log.info("IBKR option quote snapshot connected; next valid order id=%s (unused)", orderId)
        self.reqMarketDataType(int(self.config.market_data_type))
        for candidate in self.candidates:
            request_id = self._request_id()
            self._req_to_candidate[request_id] = candidate
            self._quotes[request_id] = {
                **candidate,
                "request_id": request_id,
                "requested_at": datetime.now(UTC).isoformat(),
                "market_data_type": None,
                "bid": None,
                "ask": None,
                "last": None,
                "close": None,
                "bid_size": None,
                "ask_size": None,
                "last_size": None,
                "volume": None,
                "open_interest": None,
                "option_computation": {},
                "errors": [],
                "safety": {
                    "paper_only": True,
                    "read_only_quote_snapshot": True,
                    "ibkr_order_submitted": False,
                    "account_or_portfolio_checked": False,
                },
            }
            self.reqMktData(
                request_id,
                option_contract_from_template(candidate.get("candidate_contract") or {}),
                self.config.generic_ticks,
                False,
                False,
                [],
            )

    def marketDataType(self, reqId: int, marketDataType: int) -> None:  # noqa: N802
        if reqId in self._quotes:
            self._quotes[reqId]["market_data_type"] = int(marketDataType)
            self._quotes[reqId]["delayed"] = int(marketDataType) in {3, 4}

    def tickPrice(self, reqId: int, tickType: int, price: float, attrib) -> None:  # noqa: N802
        if reqId not in self._quotes or price is None or float(price) < 0.0:
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
            self._quotes[reqId][field] = float(price)

    def tickSize(self, reqId: int, tickType: int, size: int) -> None:  # noqa: N802
        if reqId not in self._quotes:
            return
        name = _tick_type_name(tickType)
        field_map = {
            "BID_SIZE": "bid_size",
            "ASK_SIZE": "ask_size",
            "LAST_SIZE": "last_size",
            "VOLUME": "volume",
            "DELAYED_BID_SIZE": "bid_size",
            "DELAYED_ASK_SIZE": "ask_size",
            "DELAYED_LAST_SIZE": "last_size",
            "DELAYED_VOLUME": "volume",
            "OPTION_CALL_OPEN_INTEREST": "open_interest",
            "OPTION_PUT_OPEN_INTEREST": "open_interest",
            "DELAYED_CALL_OPEN_INTEREST": "open_interest",
            "DELAYED_PUT_OPEN_INTEREST": "open_interest",
        }
        field = field_map.get(name)
        if field:
            self._quotes[reqId][field] = int(size)

    def tickOptionComputation(self, reqId: int, tickType: int, tickAttrib: int, impliedVol: float, delta: float, optPrice: float, pvDividend: float, gamma: float, vega: float, theta: float, undPrice: float) -> None:  # noqa: N802
        if reqId not in self._quotes:
            return
        name = _tick_type_name(tickType)
        self._quotes[reqId].setdefault("option_computation", {})[name] = {
            "implied_vol": None if impliedVol is None or impliedVol < 0 else float(impliedVol),
            "delta": None if delta is None or abs(float(delta)) > 2 else float(delta),
            "option_price": None if optPrice is None or optPrice < 0 else float(optPrice),
            "pv_dividend": None if pvDividend is None or pvDividend < 0 else float(pvDividend),
            "gamma": None if gamma is None or gamma < 0 else float(gamma),
            "vega": None if vega is None or vega < 0 else float(vega),
            "theta": None if theta is None or abs(float(theta)) > 1e6 else float(theta),
            "underlying_price": None if undPrice is None or undPrice < 0 else float(undPrice),
        }

    def error(self, reqId: int, errorCode: int, errorString: str, advancedOrderRejectJson: str = "") -> None:  # noqa: N802
        if errorCode in {2104, 2106, 2158}:
            return
        row = {
            "req_id": reqId,
            "symbol": (self._req_to_candidate.get(reqId) or {}).get("symbol"),
            "code": int(errorCode),
            "message": str(errorString),
            "advanced": advancedOrderRejectJson or "",
        }
        self._errors.append(row)
        if reqId in self._quotes:
            self._quotes[reqId].setdefault("errors", []).append(row)

    def start(self) -> None:
        self.connect(self.config.host, int(self.config.port), clientId=int(self.config.client_id))
        self._api_thread = threading.Thread(target=self.run, name="ibkr-option-quote-snapshot", daemon=True)
        self._api_thread.start()
        if not self._connected.wait(min(10.0, float(self.config.quote_seconds) + 5.0)):
            raise TimeoutError("Timed out waiting for IBKR option quote snapshot connection")

    def stop(self) -> None:
        for req_id in list(self._req_to_candidate):
            try:
                self.cancelMktData(req_id)
            except Exception:
                pass
        try:
            if self.isConnected():
                self.disconnect()
        finally:
            if self._api_thread and self._api_thread.is_alive():
                self._api_thread.join(timeout=3.0)

    def snapshot(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        time.sleep(max(0.1, float(self.config.quote_seconds)))
        quotes: list[dict[str, Any]] = []
        for quote in self._quotes.values():
            quality = _quote_quality(quote, max_spread_pct=self.config.max_spread_pct)
            quotes.append({**quote, **quality})
        quotes.sort(
            key=lambda row: (
                str(row.get("symbol") or ""),
                int(row.get("dte") or 0),
                abs(float(row.get("moneyness") or 0.0)),
                str(row.get("right") or ""),
                float(row.get("strike") or 0.0),
            )
        )
        return quotes, self._errors


def option_quote_snapshot_summary(
    *,
    chain_summary: dict[str, Any],
    quotes: list[dict[str, Any]],
    errors: list[dict[str, Any]],
    config: OptionQuoteSnapshotConfig,
) -> dict[str, Any]:
    status_counts: dict[str, int] = {}
    for quote in quotes:
        status = str(quote.get("quote_status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
    quoted = [quote for quote in quotes if quote.get("quote_status") in {"quoted", "wide_spread"}]
    spreads = [float(quote["spread_pct"]) for quote in quoted if quote.get("spread_pct") is not None]
    by_symbol: list[dict[str, Any]] = []
    for symbol in sorted({str(quote.get("symbol") or "") for quote in quotes}):
        members = [quote for quote in quotes if str(quote.get("symbol") or "") == symbol]
        symbol_quoted = [quote for quote in members if quote.get("quote_status") in {"quoted", "wide_spread"}]
        symbol_status_counts = dict(Counter(str(quote.get("quote_status") or "unknown") for quote in members))
        symbol_errors = [error for error in errors if str(error.get("symbol") or "").upper() == symbol.upper()]
        market_data_type_counts = dict(
            Counter(
                str(quote.get("market_data_type") if quote.get("market_data_type") is not None else "unknown")
                for quote in members
            )
        )
        delayed_count = len(
            [
                quote
                for quote in members
                if bool(quote.get("delayed")) or int(quote.get("market_data_type") or 0) in {3, 4}
            ]
        )
        no_security_error_count = len(
            [
                error
                for error in symbol_errors
                if "no security definition" in str(error.get("message") or "").lower()
            ]
        )
        valid_contract_rate = (
            (len(members) - no_security_error_count) / len(members)
            if members
            else None
        )
        spread_values = [float(item["spread_pct"]) for item in symbol_quoted if item.get("spread_pct") is not None]
        by_symbol.append(
            {
                "symbol": symbol,
                "requested": len(members),
                "requested_quotes": len(members),
                "quoted": len(symbol_quoted),
                "quoted_contracts": len(symbol_quoted),
                "quoted_rate": len(symbol_quoted) / len(members) if members else None,
                "avg_spread_pct": (
                    sum(spread_values) / len(spread_values)
                    if spread_values
                    else None
                ),
                "status_counts": symbol_status_counts,
                "no_quote": int(symbol_status_counts.get("no_quote") or 0),
                "close_only": int(symbol_status_counts.get("close_only") or 0),
                "last_only": int(symbol_status_counts.get("last_only") or 0),
                "wide_spread": int(symbol_status_counts.get("wide_spread") or 0),
                "delayed_count": delayed_count,
                "market_data_type_counts": market_data_type_counts,
                "error_count": len(symbol_errors),
                "no_security_definition_error_count": no_security_error_count,
                "valid_contract_rate": valid_contract_rate,
                "dte_bucket_counts": _dte_bucket_counts(members),
                "diagnostic_status": (
                    "green"
                    if symbol_quoted
                    and len(symbol_quoted) / len(members) >= 0.5
                    and no_security_error_count == 0
                    else "yellow"
                    if symbol_quoted
                    else "red"
                ) if members else "missing",
            }
        )
    return {
        "snapshot_version": IBKR_OPTION_QUOTE_SNAPSHOT_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "chain_summary": chain_summary,
        "quotes": quotes,
        "errors": errors,
        "status_counts": status_counts,
        "requested_quotes": len(quotes),
        "quoted_contracts": len(quoted),
        "quoted_rate": len(quoted) / len(quotes) if quotes else None,
        "dte_bucket_counts": _dte_bucket_counts(quotes),
        "avg_spread_pct": sum(spreads) / len(spreads) if spreads else None,
        "by_symbol": by_symbol,
        "config": {
            "host": config.host,
            "port": config.port,
            "client_id": config.client_id,
            "symbols": list(config.symbols),
            "reference_prices": config.reference_prices or {},
            "underlying_sec_types": config.underlying_sec_types or {},
            "underlying_exchanges": config.underlying_exchanges or {},
            "min_dte": config.min_dte,
            "max_dte": config.max_dte,
            "strikes_around": config.strikes_around,
            "max_expirations": config.max_expirations,
            "max_quote_contracts": config.max_quote_contracts,
            "market_data_type": config.market_data_type,
            "generic_ticks": config.generic_ticks,
            "chain_timeout_seconds": config.chain_timeout_seconds,
            "quote_seconds": config.quote_seconds,
            "max_spread_pct": config.max_spread_pct,
        },
        "safety": {
            "mode": "read_only_ibkr_option_quote_snapshot",
            "market_data_requested": True,
            "ibkr_order_submitted": False,
            "account_or_portfolio_checked": False,
            "policy": "Requests option market data snapshots only; no account, margin, portfolio, or order submission.",
        },
    }


def run_ibkr_option_quote_snapshot(config: OptionQuoteSnapshotConfig) -> dict[str, Any]:
    chain_config = OptionChainScanConfig(
        host=config.host,
        port=config.port,
        client_id=config.client_id,
        symbols=config.symbols,
        reference_prices=config.reference_prices,
        underlying_sec_types=config.underlying_sec_types,
        underlying_exchanges=config.underlying_exchanges,
        min_dte=config.min_dte,
        max_dte=config.max_dte,
        strikes_around=config.strikes_around,
        max_expirations=config.max_expirations,
        timeout_seconds=config.chain_timeout_seconds,
    )
    chain_summary = run_ibkr_option_chain_scan(chain_config)
    candidates = limit_option_candidates_by_symbol(
        list(chain_summary.get("candidate_contracts") or []),
        max(0, int(config.max_quote_contracts)),
    )
    app = IBKROptionQuoteSnapshotApp(config, candidates)
    app.start()
    try:
        quotes, quote_errors = app.snapshot()
    finally:
        app.stop()
    return option_quote_snapshot_summary(
        chain_summary=chain_summary,
        quotes=quotes,
        errors=list(chain_summary.get("errors") or []) + quote_errors,
        config=config,
    )


def option_quote_snapshot_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# IBKR Option Quote Read-Only Snapshot Report",
        "",
        f"- Generated at: `{summary.get('generated_at')}`",
        f"- Requested quotes: `{summary.get('requested_quotes')}`",
        f"- Quoted contracts: `{summary.get('quoted_contracts')}`",
        f"- Quoted rate: `{(float(summary.get('quoted_rate') or 0.0) * 100):.2f}%`",
        f"- Avg spread pct: `{(float(summary.get('avg_spread_pct') or 0.0) * 100):.2f}%`" if summary.get("avg_spread_pct") is not None else "- Avg spread pct: `-`",
        f"- Errors/warnings: `{len(summary.get('errors') or [])}`",
        "- Safety: read-only option quote snapshot only; no account, margin, portfolio, or order submission.",
        "",
        "## Config",
        "",
    ]
    for key, value in (summary.get("config") or {}).items():
        if key in {"host", "port"}:
            continue
        lines.append(f"- `{key}`: `{value}`")
    lines.extend(
        [
            "",
            "## Status counts",
            "",
        ]
    )
    for key, value in (summary.get("status_counts") or {}).items():
        lines.append(f"- `{key}`: `{value}`")
    lines.extend(
        [
            "",
            "## DTE bucket allocation",
            "",
        ]
    )
    for key, value in (summary.get("dte_bucket_counts") or {}).items():
        lines.append(f"- `{key}`: `{value}`")
    lines.extend(
        [
            "",
            "## By symbol",
            "",
            "| Symbol | Status | Requested | Quoted | Quoted rate | DTE buckets | No quote | Wide | Last-only | Close-only | Delayed | Errors | Valid contract rate | Avg spread pct |",
            "| --- | --- | ---: | ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for row in summary.get("by_symbol") or []:
        avg_spread = row.get("avg_spread_pct")
        valid_rate = row.get("valid_contract_rate")
        valid_text = f"{float(valid_rate) * 100:.2f}%" if valid_rate is not None else "-"
        spread_text = f"{float(avg_spread) * 100:.2f}%" if avg_spread is not None else "-"
        lines.append(
            f"| {row.get('symbol')} | `{row.get('diagnostic_status') or '-'}` | {row.get('requested')} | {row.get('quoted')} "
            f"| {float(row.get('quoted_rate') or 0.0) * 100:.2f}% "
            f"| `{row.get('dte_bucket_counts') or {}}` "
            f"| {row.get('no_quote') or 0} | {row.get('wide_spread') or 0} | {row.get('last_only') or 0} | {row.get('close_only') or 0} "
            f"| {row.get('delayed_count') or 0} | {row.get('error_count') or 0} | {valid_text} | {spread_text} |"
        )
    lines.extend(
        [
            "",
            "## Sample quotes",
            "",
            "| Symbol | Exp | DTE | Right | Strike | Status | Bid | Ask | Last | Close | Mid | Spread | Mkt data type | Delayed |",
            "| --- | --- | ---: | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    for quote in (summary.get("quotes") or [])[:40]:
        lines.append(
            f"| {quote.get('symbol')} | {quote.get('expiration')} | {quote.get('dte')} "
            f"| {quote.get('right')} | {float(quote.get('strike') or 0.0):.2f} "
            f"| {quote.get('quote_status')} "
            f"| {float(quote.get('bid') or 0.0):.2f} "
            f"| {float(quote.get('ask') or 0.0):.2f} "
            f"| {float(quote.get('last') or 0.0):.2f} "
            f"| {float(quote.get('close') or 0.0):.2f} "
            f"| {float(quote.get('mid') or 0.0):.2f} "
            f"| {(float(quote.get('spread_pct') or 0.0) * 100):.2f}% "
            f"| {quote.get('market_data_type') if quote.get('market_data_type') is not None else '-'} "
            f"| {quote.get('delayed') if quote.get('delayed') is not None else '-'} |"
        )
    if summary.get("errors"):
        lines.extend(
            [
                "",
                "## Errors / warnings",
                "",
                "| Symbol | Req id | Code | Message |",
                "| --- | ---: | ---: | --- |",
            ]
        )
        for error in summary.get("errors") or []:
            lines.append(
                f"| {error.get('symbol') or '-'} | {error.get('req_id') or '-'} "
                f"| {error.get('code') or '-'} | {str(error.get('message') or '').replace('|', '/')} |"
            )
    lines.extend(
        [
            "",
            "## Interpretation note",
            "",
            "This is the first quote layer. It proves whether candidate option contracts can return quote fields from IBKR. Liquidity scoring, IV surface selection, open interest, spread-aware strategy construction, account checks, and execution remain separate future steps.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_option_quote_snapshot_markdown(output_path: Path, summary: dict[str, Any]) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(option_quote_snapshot_markdown(summary), encoding="utf-8")
    return output_path


def write_option_quote_snapshot_json(output_path: Path, summary: dict[str, Any]) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    return output_path
