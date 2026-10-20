from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ibapi.client import EClient
from ibapi.contract import Contract
from ibapi.wrapper import EWrapper


IBKR_SHORT_AVAILABILITY_VERSION = "ibkr-short-availability-v1"
SHORTABLE_GENERIC_TICKS = "236"
TICK_SHORTABLE = 46
TICK_SHORTABLE_SHARES = 89


@dataclass(frozen=True)
class ShortAvailabilityConfig:
    host: str = "127.0.0.1"
    port: int = 4001
    client_id: int = 9121
    symbols: tuple[str, ...] = ()
    timeout_seconds: float = 15.0
    market_data_type: int = 1
    exchange: str = "SMART"
    currency: str = "USD"


def _stock_contract(symbol: str, *, exchange: str = "SMART", currency: str = "USD") -> Contract:
    contract = Contract()
    contract.symbol = symbol.upper()
    contract.secType = "STK"
    contract.exchange = exchange
    contract.currency = currency
    return contract


def _shortable_description(score: float | None) -> str:
    if score is None:
        return "unknown"
    if score > 2.5:
        return "shortable_at_least_1000_shares_indicated"
    if score > 1.5:
        return "locate_required_or_conditionally_shortable"
    return "not_available_for_short_sale"


class IBKRShortAvailabilityApp(EWrapper, EClient):
    def __init__(self, config: ShortAvailabilityConfig):
        EClient.__init__(self, self)
        self.config = config
        self.log = logging.getLogger(self.__class__.__name__)
        self._api_thread: threading.Thread | None = None
        self._connected = threading.Event()
        self._errors: list[dict[str, Any]] = []
        self._rows_by_req: dict[int, dict[str, Any]] = {}
        self._req_by_symbol: dict[str, int] = {}

    def nextValidId(self, orderId: int) -> None:  # noqa: N802
        self._connected.set()
        self.reqMarketDataType(int(self.config.market_data_type))
        base_req_id = 912100
        for idx, symbol in enumerate(self.config.symbols):
            req_id = base_req_id + idx
            normalized = symbol.upper()
            self._req_by_symbol[normalized] = req_id
            self._rows_by_req[req_id] = {
                "request_id": req_id,
                "symbol": normalized,
                "shortable_score": None,
                "shortable_shares": None,
                "market_data_type": self.config.market_data_type,
                "received_ticks": [],
            }
            self.reqMktData(
                req_id,
                _stock_contract(normalized, exchange=self.config.exchange, currency=self.config.currency),
                SHORTABLE_GENERIC_TICKS,
                False,
                False,
                [],
            )

    def error(self, reqId: int, errorCode: int, errorString: str, advancedOrderRejectJson: str = "") -> None:  # noqa: N802
        self._errors.append(
            {
                "request_id": reqId,
                "code": errorCode,
                "message": errorString,
                "advanced_order_reject_json": advancedOrderRejectJson or None,
                "received_at": datetime.now(UTC).isoformat(),
            }
        )

    def tickGeneric(self, reqId: int, tickType: int, value: float) -> None:  # noqa: N802
        row = self._rows_by_req.get(int(reqId))
        if row is None:
            return
        row["received_ticks"].append({"tick_type": tickType, "value": value, "received_at": datetime.now(UTC).isoformat()})
        if int(tickType) == TICK_SHORTABLE:
            row["shortable_score"] = float(value)

    def tickSize(self, reqId: int, tickType: int, size: int) -> None:  # noqa: N802
        row = self._rows_by_req.get(int(reqId))
        if row is None:
            return
        row["received_ticks"].append({"tick_type": tickType, "value": size, "received_at": datetime.now(UTC).isoformat()})
        if int(tickType) == TICK_SHORTABLE_SHARES:
            row["shortable_shares"] = int(size)

    def marketDataType(self, reqId: int, marketDataType: int) -> None:  # noqa: N802
        row = self._rows_by_req.get(int(reqId))
        if row is not None:
            row["actual_market_data_type"] = int(marketDataType)

    def start(self) -> None:
        self.connect(self.config.host, int(self.config.port), int(self.config.client_id))
        self._api_thread = threading.Thread(target=self.run, daemon=True)
        self._api_thread.start()

    def wait(self) -> None:
        self._connected.wait(timeout=min(5.0, float(self.config.timeout_seconds)))
        time.sleep(max(1.0, float(self.config.timeout_seconds)))

    def stop(self) -> None:
        for req_id in list(self._rows_by_req):
            try:
                self.cancelMktData(req_id)
            except Exception:  # pragma: no cover - defensive cleanup only
                self.log.debug("cancelMktData failed for req_id=%s", req_id, exc_info=True)
        try:
            self.disconnect()
        finally:
            if self._api_thread and self._api_thread.is_alive():
                self._api_thread.join(timeout=2.0)

    def summary(self) -> dict[str, Any]:
        rows = []
        for req_id, row in sorted(self._rows_by_req.items()):
            score = row.get("shortable_score")
            shares = row.get("shortable_shares")
            rows.append(
                {
                    **row,
                    "shortable_description": _shortable_description(score),
                    "has_exact_shortable_shares": shares is not None,
                    "short_available": (shares is not None and shares > 0) or (score is not None and score > 2.5),
                }
            )
        return {
            "short_availability_version": IBKR_SHORT_AVAILABILITY_VERSION,
            "generated_at": datetime.now(UTC).isoformat(),
            "symbols_requested": list(self.config.symbols),
            "row_count": len(rows),
            "rows": rows,
            "errors": self._errors,
            "config": {
                "host": self.config.host,
                "port": self.config.port,
                "client_id": self.config.client_id,
                "timeout_seconds": self.config.timeout_seconds,
                "market_data_type": self.config.market_data_type,
                "generic_ticks": SHORTABLE_GENERIC_TICKS,
                "exchange": self.config.exchange,
                "currency": self.config.currency,
            },
            "safety": {
                "read_only_market_data": True,
                "ibkr_order_submitted": False,
            },
        }


def run_ibkr_short_availability(config: ShortAvailabilityConfig) -> dict[str, Any]:
    app = IBKRShortAvailabilityApp(config)
    app.start()
    try:
        app.wait()
        return app.summary()
    finally:
        app.stop()


def short_availability_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# IBKR Short Availability Snapshot",
        "",
        f"- Generated at: `{summary.get('generated_at')}`",
        f"- Symbols requested: `{len(summary.get('symbols_requested') or [])}`",
        f"- Rows: `{summary.get('row_count')}`",
        "- Safety: read-only market data only; no order submission.",
        "",
        "| Symbol | Short available | Score | Shares | Description |",
        "| --- | --- | ---: | ---: | --- |",
    ]
    for row in summary.get("rows") or []:
        shares = row.get("shortable_shares")
        lines.append(
            f"| {row.get('symbol')} | {row.get('short_available')} | {row.get('shortable_score')} | {shares if shares is not None else '-'} | {row.get('shortable_description')} |"
        )
    if not summary.get("rows"):
        lines.append("| - | - | - | - | - |")
    lines.extend(["", "## Errors / warnings", "", "| Req ID | Code | Message |", "| ---: | ---: | --- |"])
    for err in summary.get("errors") or []:
        lines.append(f"| {err.get('request_id')} | {err.get('code')} | {err.get('message')} |")
    if not summary.get("errors"):
        lines.append("| - | - | - |")
    return "\n".join(lines) + "\n"


def write_short_availability_json(output_path: Path, summary: dict[str, Any]) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    return output_path


def write_short_availability_markdown(output_path: Path, summary: dict[str, Any]) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(short_availability_markdown(summary), encoding="utf-8")
    return output_path
