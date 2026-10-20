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
from ibapi.order import Order
from ibapi.order_state import OrderState
from ibapi.wrapper import EWrapper


IBKR_ACCOUNT_SNAPSHOT_VERSION = "ibkr-account-read-only-snapshot-v1"


@dataclass(frozen=True)
class AccountSnapshotConfig:
    host: str = "127.0.0.1"
    port: int = 4001
    client_id: int = 8021
    account: str | None = None
    tags: str = "NetLiquidation,TotalCashValue,AvailableFunds,BuyingPower,ExcessLiquidity,MaintMarginReq,InitMarginReq"
    timeout_seconds: float = 12.0
    include_positions: bool = True
    include_open_orders: bool = True


def _contract_payload(contract: Contract) -> dict[str, Any]:
    return {
        "con_id": getattr(contract, "conId", None),
        "symbol": getattr(contract, "symbol", None),
        "sec_type": getattr(contract, "secType", None),
        "exchange": getattr(contract, "exchange", None),
        "currency": getattr(contract, "currency", None),
        "last_trade_date_or_contract_month": getattr(contract, "lastTradeDateOrContractMonth", None),
        "strike": getattr(contract, "strike", None),
        "right": getattr(contract, "right", None),
        "multiplier": getattr(contract, "multiplier", None),
        "trading_class": getattr(contract, "tradingClass", None),
    }


def _order_payload(order: Order, order_state: OrderState | None = None) -> dict[str, Any]:
    return {
        "order_id": getattr(order, "orderId", None),
        "perm_id": getattr(order, "permId", None),
        "client_id": getattr(order, "clientId", None),
        "action": getattr(order, "action", None),
        "order_type": getattr(order, "orderType", None),
        "total_quantity": getattr(order, "totalQuantity", None),
        "lmt_price": getattr(order, "lmtPrice", None),
        "aux_price": getattr(order, "auxPrice", None),
        "tif": getattr(order, "tif", None),
        "status": getattr(order_state, "status", None) if order_state is not None else None,
    }


class IBKRAccountSnapshotApp(EWrapper, EClient):
    def __init__(self, config: AccountSnapshotConfig):
        EClient.__init__(self, self)
        self.config = config
        self.log = logging.getLogger(self.__class__.__name__)
        self._api_thread: threading.Thread | None = None
        self._connected = threading.Event()
        self._account_summary_done = threading.Event()
        self._positions_done = threading.Event()
        self._errors: list[dict[str, Any]] = []
        self._account_summary: list[dict[str, Any]] = []
        self._positions: list[dict[str, Any]] = []
        self._open_orders: list[dict[str, Any]] = []
        self._next_order_id: int | None = None

    def nextValidId(self, orderId: int) -> None:  # noqa: N802
        self._next_order_id = int(orderId)
        self._connected.set()
        self.log.info("IBKR account snapshot connected; next valid order id=%s (unused)", orderId)
        self.reqAccountSummary(900001, "All", self.config.tags)
        if self.config.include_positions:
            self.reqPositions()
        else:
            self._positions_done.set()
        if self.config.include_open_orders:
            self.reqOpenOrders()

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

    def accountSummary(self, reqId: int, account: str, tag: str, value: str, currency: str) -> None:  # noqa: N802
        self._account_summary.append(
            {
                "request_id": reqId,
                "account": account,
                "tag": tag,
                "value": value,
                "currency": currency,
            }
        )

    def accountSummaryEnd(self, reqId: int) -> None:  # noqa: N802
        self._account_summary_done.set()

    def position(self, account: str, contract: Contract, position: float, avgCost: float) -> None:  # noqa: N802
        self._positions.append(
            {
                "account": account,
                "contract": _contract_payload(contract),
                "position": float(position),
                "avg_cost": float(avgCost),
            }
        )

    def positionEnd(self) -> None:  # noqa: N802
        self._positions_done.set()

    def openOrder(self, orderId: int, contract: Contract, order: Order, orderState: OrderState) -> None:  # noqa: N802
        self._open_orders.append(
            {
                "order_id": orderId,
                "contract": _contract_payload(contract),
                "order": _order_payload(order, orderState),
            }
        )

    def start(self) -> None:
        self.connect(self.config.host, self.config.port, self.config.client_id)
        self._api_thread = threading.Thread(target=self.run, name="ibkr-account-snapshot", daemon=True)
        self._api_thread.start()

    def stop(self) -> None:
        try:
            if self.isConnected():
                try:
                    self.cancelAccountSummary(900001)
                except Exception:
                    pass
                try:
                    self.cancelPositions()
                except Exception:
                    pass
                self.disconnect()
        finally:
            if self._api_thread and self._api_thread.is_alive():
                self._api_thread.join(timeout=3.0)

    def wait(self) -> dict[str, Any]:
        if not self._connected.wait(timeout=max(1.0, float(self.config.timeout_seconds) / 3.0)):
            self._errors.append({"request_id": None, "code": "timeout", "message": "IBKR account snapshot did not connect before timeout"})
        deadline = time.monotonic() + max(1.0, float(self.config.timeout_seconds))
        while time.monotonic() < deadline:
            if self._account_summary_done.is_set() and self._positions_done.is_set():
                break
            time.sleep(0.1)
        if not self._account_summary_done.is_set():
            self._errors.append({"request_id": 900001, "code": "timeout", "message": "accountSummaryEnd not received before timeout"})
        if self.config.include_positions and not self._positions_done.is_set():
            self._errors.append({"request_id": None, "code": "timeout", "message": "positionEnd not received before timeout"})
        return account_snapshot_summary(
            account_summary=self._account_summary,
            positions=self._positions,
            open_orders=self._open_orders,
            errors=self._errors,
            config=self.config,
            next_order_id_seen=self._next_order_id,
        )


def _numeric_value(value: Any) -> float | None:
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def account_snapshot_summary(
    *,
    account_summary: list[dict[str, Any]],
    positions: list[dict[str, Any]],
    open_orders: list[dict[str, Any]],
    errors: list[dict[str, Any]],
    config: AccountSnapshotConfig,
    next_order_id_seen: int | None,
) -> dict[str, Any]:
    by_account: dict[str, dict[str, Any]] = {}
    for row in account_summary:
        account = str(row.get("account") or "UNKNOWN")
        tag = str(row.get("tag") or "")
        account_row = by_account.setdefault(account, {"account": account, "tags": {}})
        account_row["tags"][tag] = {
            "value": row.get("value"),
            "numeric_value": _numeric_value(row.get("value")),
            "currency": row.get("currency"),
        }
    if config.account:
        selected_account = config.account
    elif by_account:
        selected_account = max(
            sorted(by_account),
            key=lambda account_id: (
                ((by_account.get(account_id, {}).get("tags") or {}).get("NetLiquidation") or {}).get("numeric_value")
                or float("-inf")
            ),
        )
    else:
        selected_account = None
    selected_tags = (by_account.get(selected_account or "", {}) or {}).get("tags") or {}
    equity = (selected_tags.get("NetLiquidation") or {}).get("numeric_value")
    cash = (
        (selected_tags.get("TotalCashValue") or {}).get("numeric_value")
        or (selected_tags.get("AvailableFunds") or {}).get("numeric_value")
    )
    buying_power = (selected_tags.get("BuyingPower") or {}).get("numeric_value")
    summary = {
        "snapshot_version": IBKR_ACCOUNT_SNAPSHOT_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "selected_account": selected_account,
        "account_count": len(by_account),
        "accounts": list(by_account.values()),
        "equity": equity,
        "cash": cash,
        "buying_power": buying_power,
        "positions": positions,
        "position_count": len(positions),
        "open_orders": open_orders,
        "open_order_count": len(open_orders),
        "errors": errors,
        "config": {
            "host": config.host,
            "port": config.port,
            "client_id": config.client_id,
            "account": config.account,
            "tags": config.tags,
            "timeout_seconds": config.timeout_seconds,
            "include_positions": config.include_positions,
            "include_open_orders": config.include_open_orders,
        },
        "safety": {
            "read_only_account_snapshot": True,
            "next_order_id_seen_but_unused": next_order_id_seen,
            "ibkr_order_submitted": False,
            "account_values_requested": True,
            "positions_requested": bool(config.include_positions),
            "open_orders_requested": bool(config.include_open_orders),
        },
    }
    return summary


def run_ibkr_account_snapshot(config: AccountSnapshotConfig) -> dict[str, Any]:
    app = IBKRAccountSnapshotApp(config)
    app.start()
    try:
        return app.wait()
    finally:
        app.stop()


def account_snapshot_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# IBKR Account Read-Only Snapshot Report",
        "",
        f"- Generated at: `{summary.get('generated_at')}`",
        f"- Selected account: `{summary.get('selected_account')}`",
        f"- Equity / NetLiquidation: `{summary.get('equity')}`",
        f"- Cash: `{summary.get('cash')}`",
        f"- Buying power: `{summary.get('buying_power')}`",
        f"- Positions: `{summary.get('position_count')}`",
        f"- Open orders: `{summary.get('open_order_count')}`",
        f"- Errors/warnings: `{len(summary.get('errors') or [])}`",
        "- Safety: read-only account/position/open-order snapshot only; no order submission.",
        "",
        "## Account tags",
        "",
        "| Account | Tag | Value | Currency |",
        "| --- | --- | ---: | --- |",
    ]
    for account in summary.get("accounts") or []:
        for tag, payload in sorted((account.get("tags") or {}).items()):
            lines.append(f"| {account.get('account')} | {tag} | {payload.get('value')} | {payload.get('currency') or '-'} |")
    lines.extend(["", "## Positions", "", "| Account | Symbol | SecType | Position | Avg cost |", "| --- | --- | --- | ---: | ---: |"])
    positions = summary.get("positions") or []
    if positions:
        for row in positions[:50]:
            contract = row.get("contract") or {}
            lines.append(
                f"| {row.get('account')} | {contract.get('symbol')} | {contract.get('sec_type')} | {row.get('position')} | {row.get('avg_cost')} |"
            )
    else:
        lines.append("| - | - | - | 0 | - |")
    lines.extend(["", "## Open orders", "", "| Order id | Symbol | SecType | Action | Type | Qty | Status |", "| ---: | --- | --- | --- | --- | ---: | --- |"])
    open_orders = summary.get("open_orders") or []
    if open_orders:
        for row in open_orders[:50]:
            contract = row.get("contract") or {}
            order = row.get("order") or {}
            lines.append(
                f"| {row.get('order_id')} | {contract.get('symbol')} | {contract.get('sec_type')} | {order.get('action')} | {order.get('order_type')} | {order.get('total_quantity')} | {order.get('status') or '-'} |"
            )
    else:
        lines.append("| - | - | - | - | - | - | - |")
    if summary.get("errors"):
        lines.extend(["", "## Errors / warnings", "", "| Req id | Code | Message |", "| ---: | --- | --- |"])
        for error in summary.get("errors") or []:
            lines.append(f"| {error.get('request_id') or '-'} | {error.get('code')} | {error.get('message')} |")
    lines.extend(
        [
            "",
            "## Interpretation note",
            "",
            "This snapshot is designed to feed later readiness layers with read-only equity, cash, position, and open-order context. Seeing a next valid order id is part of the IBKR connection handshake; this tool does not place, modify, cancel, or transmit orders.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_account_snapshot_markdown(output_path: Path, summary: dict[str, Any]) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(account_snapshot_markdown(summary), encoding="utf-8")
    return output_path


def write_account_snapshot_json(output_path: Path, summary: dict[str, Any]) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    return output_path
