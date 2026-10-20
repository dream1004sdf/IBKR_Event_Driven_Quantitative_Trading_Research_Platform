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


IBKR_STOCK_MARK_SNAPSHOT_VERSION = "ibkr-stock-mark-snapshot-v1"
MARKET_DATA_TYPE_LABELS = {
    1: "live",
    2: "frozen",
    3: "delayed",
    4: "delayed_frozen",
}
TICK_BID = 1
TICK_ASK = 2
TICK_LAST = 4
TICK_CLOSE = 9
TICK_DELAYED_BID = 66
TICK_DELAYED_ASK = 67
TICK_DELAYED_LAST = 68
TICK_DELAYED_CLOSE = 75


@dataclass(frozen=True)
class StockMarkSnapshotConfig:
    host: str = "127.0.0.1"
    port: int = 4001
    client_id: int = 9321
    symbols: tuple[str, ...] = ()
    quote_seconds: float = 8.0
    market_data_type: int = 1
    exchange: str = "SMART"
    currency: str = "USD"


def stock_mark_symbols_from_router_payload(payload: dict[str, Any]) -> tuple[str, ...]:
    router = payload.get("result") or payload
    symbols: list[str] = []
    seen: set[str] = set()
    for row in router.get("paper_positions") or []:
        symbol = str(row.get("symbol") or "").upper().strip()
        if symbol and symbol not in seen:
            seen.add(symbol)
            symbols.append(symbol)
    return tuple(symbols)


def _stock_contract(symbol: str, *, exchange: str = "SMART", currency: str = "USD") -> Contract:
    contract = Contract()
    contract.symbol = symbol.upper()
    contract.secType = "STK"
    contract.exchange = exchange
    contract.currency = currency
    return contract


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0.0:
        return None
    return number


def _quote_mark(row: dict[str, Any]) -> dict[str, Any]:
    bid = _float_or_none(row.get("bid"))
    ask = _float_or_none(row.get("ask"))
    last = _float_or_none(row.get("last"))
    close = _float_or_none(row.get("close"))
    delayed = bool(row.get("delayed")) or row.get("market_data_type") in {3, 4}
    if last is not None:
        return {
            "mark_price": last,
            "mark_source": "ibkr_last_delayed" if delayed else "ibkr_last",
            "mark_status": "marked",
        }
    if bid is not None and ask is not None:
        return {
            "mark_price": (bid + ask) / 2.0,
            "mark_source": "ibkr_midpoint_delayed" if delayed else "ibkr_midpoint",
            "mark_status": "marked",
        }
    if close is not None:
        return {
            "mark_price": close,
            "mark_source": "ibkr_close_delayed" if delayed else "ibkr_close",
            "mark_status": "marked",
        }
    return {
        "mark_price": None,
        "mark_source": None,
        "mark_status": "missing_mark",
    }


class IBKRStockMarkSnapshotApp(EWrapper, EClient):
    def __init__(self, config: StockMarkSnapshotConfig):
        EClient.__init__(self, self)
        self.config = config
        self.log = logging.getLogger(self.__class__.__name__)
        self._api_thread: threading.Thread | None = None
        self._connected = threading.Event()
        self._errors: list[dict[str, Any]] = []
        self._rows_by_req: dict[int, dict[str, Any]] = {}

    def nextValidId(self, orderId: int) -> None:  # noqa: N802
        self._connected.set()
        self.reqMarketDataType(int(self.config.market_data_type))
        base_req_id = 932100
        for idx, symbol in enumerate(self.config.symbols):
            req_id = base_req_id + idx
            normalized = symbol.upper()
            self._rows_by_req[req_id] = {
                "request_id": req_id,
                "symbol": normalized,
                "bid": None,
                "ask": None,
                "last": None,
                "close": None,
                "market_data_type": int(self.config.market_data_type),
                "market_data_type_label": MARKET_DATA_TYPE_LABELS.get(int(self.config.market_data_type), "unknown"),
                "delayed": int(self.config.market_data_type) in {3, 4},
                "received_ticks": [],
            }
            self.reqMktData(
                req_id,
                _stock_contract(normalized, exchange=self.config.exchange, currency=self.config.currency),
                "",
                False,
                False,
                [],
            )

    def error(self, reqId: int, errorCode: int, errorString: str, advancedOrderRejectJson: str = "") -> None:  # noqa: N802
        self._errors.append(
            {
                "request_id": reqId,
                "code": int(errorCode),
                "message": errorString,
                "advanced_order_reject_json": advancedOrderRejectJson or None,
                "received_at": datetime.now(UTC).isoformat(),
            }
        )

    def tickPrice(self, reqId: int, tickType: int, price: float, attrib: Any) -> None:  # noqa: N802
        row = self._rows_by_req.get(int(reqId))
        if row is None:
            return
        received_at = datetime.now(UTC).isoformat()
        row["received_ticks"].append({"tick_type": int(tickType), "price": price, "received_at": received_at})
        if int(tickType) in {TICK_BID, TICK_DELAYED_BID}:
            row["bid"] = float(price)
        elif int(tickType) in {TICK_ASK, TICK_DELAYED_ASK}:
            row["ask"] = float(price)
        elif int(tickType) in {TICK_LAST, TICK_DELAYED_LAST}:
            row["last"] = float(price)
        elif int(tickType) in {TICK_CLOSE, TICK_DELAYED_CLOSE}:
            row["close"] = float(price)
        if int(tickType) in {TICK_DELAYED_BID, TICK_DELAYED_ASK, TICK_DELAYED_LAST, TICK_DELAYED_CLOSE}:
            row["delayed"] = True

    def marketDataType(self, reqId: int, marketDataType: int) -> None:  # noqa: N802
        row = self._rows_by_req.get(int(reqId))
        if row is not None:
            row["market_data_type"] = int(marketDataType)
            row["market_data_type_label"] = MARKET_DATA_TYPE_LABELS.get(int(marketDataType), "unknown")
            row["delayed"] = int(marketDataType) in {3, 4} or bool(row.get("delayed"))

    def start(self) -> None:
        self.connect(self.config.host, int(self.config.port), int(self.config.client_id))
        self._api_thread = threading.Thread(target=self.run, name="ibkr-stock-mark-snapshot", daemon=True)
        self._api_thread.start()

    def wait(self) -> None:
        self._connected.wait(timeout=min(5.0, float(self.config.quote_seconds)))
        time.sleep(max(1.0, float(self.config.quote_seconds)))

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
        return stock_mark_snapshot_summary(
            rows=list(self._rows_by_req.values()),
            errors=self._errors,
            config=self.config,
        )


def stock_mark_snapshot_summary(
    *,
    rows: list[dict[str, Any]],
    errors: list[dict[str, Any]],
    config: StockMarkSnapshotConfig,
) -> dict[str, Any]:
    marks: list[dict[str, Any]] = []
    status_counts: dict[str, int] = {}
    for raw_row in sorted(rows, key=lambda item: str(item.get("symbol") or "")):
        mark = _quote_mark(raw_row)
        row = {
            **raw_row,
            **mark,
            "mark_timestamp": datetime.now(UTC).isoformat(),
        }
        marks.append(row)
        status = str(row.get("mark_status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
    marked_count = status_counts.get("marked", 0)
    return {
        "stock_mark_snapshot_version": IBKR_STOCK_MARK_SNAPSHOT_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "symbols_requested": list(config.symbols),
        "row_count": len(marks),
        "marked_count": marked_count,
        "missing_mark_count": len(marks) - marked_count,
        "status_counts": status_counts,
        "marks": marks,
        "errors": errors,
        "config": {
            "host": config.host,
            "port": config.port,
            "client_id": config.client_id,
            "quote_seconds": config.quote_seconds,
            "market_data_type": config.market_data_type,
            "market_data_type_label": MARKET_DATA_TYPE_LABELS.get(int(config.market_data_type), "unknown"),
            "exchange": config.exchange,
            "currency": config.currency,
        },
        "safety": {
            "mode": "read_only_ibkr_stock_mark_snapshot",
            "read_only_market_data": True,
            "ibkr_order_submitted": False,
            "transmit_enabled": False,
            "live_order_routing_enabled": False,
        },
    }


def run_ibkr_stock_mark_snapshot(config: StockMarkSnapshotConfig) -> dict[str, Any]:
    app = IBKRStockMarkSnapshotApp(config)
    app.start()
    try:
        app.wait()
        return app.summary()
    finally:
        app.stop()


def stock_mark_snapshot_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# IBKR Stock Mark Read-Only Snapshot",
        "",
        f"- Generated at: `{summary.get('generated_at')}`",
        f"- Symbols requested: `{len(summary.get('symbols_requested') or [])}`",
        f"- Rows: `{summary.get('row_count')}`",
        f"- Marked: `{summary.get('marked_count')}`",
        f"- Missing marks: `{summary.get('missing_mark_count')}`",
        "- Safety: read-only market data only; no order submission or transmit.",
        "",
        "| Symbol | Mark | Source | Status | Bid | Ask | Last | Close | Data type |",
        "| --- | ---: | --- | --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in summary.get("marks") or []:
        lines.append(
            f"| {row.get('symbol')} | {row.get('mark_price') if row.get('mark_price') is not None else '-'} | "
            f"{row.get('mark_source') or '-'} | {row.get('mark_status')} | "
            f"{row.get('bid') if row.get('bid') is not None else '-'} | "
            f"{row.get('ask') if row.get('ask') is not None else '-'} | "
            f"{row.get('last') if row.get('last') is not None else '-'} | "
            f"{row.get('close') if row.get('close') is not None else '-'} | "
            f"{row.get('market_data_type_label')} |"
        )
    if not summary.get("marks"):
        lines.append("| - | - | - | - | - | - | - | - | - |")
    lines.extend(["", "## Errors / warnings", "", "| Req ID | Code | Message |", "| ---: | ---: | --- |"])
    for err in summary.get("errors") or []:
        lines.append(f"| {err.get('request_id')} | {err.get('code')} | {err.get('message')} |")
    if not summary.get("errors"):
        lines.append("| - | - | - |")
    return "\n".join(lines) + "\n"


def write_stock_mark_snapshot_json(output_path: Path, summary: dict[str, Any]) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    return output_path


def write_stock_mark_snapshot_markdown(output_path: Path, summary: dict[str, Any]) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(stock_mark_snapshot_markdown(summary), encoding="utf-8")
    return output_path
