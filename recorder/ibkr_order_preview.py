from __future__ import annotations

import json
import logging
import math
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


IBKR_ORDER_WHATIF_PREVIEW_VERSION = "ibkr-order-whatif-preview-v2"


@dataclass(frozen=True)
class OrderWhatIfPreviewConfig:
    host: str = "127.0.0.1"
    port: int = 4001
    client_id: int = 9021
    timeout_seconds: float = 20.0
    max_orders: int = 3
    include_human_review: bool = False
    allow_fractional: bool = False


def _float_or_none(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _is_fractional(value: Any) -> bool:
    numeric = _float_or_none(value)
    if numeric is None:
        return False
    return abs(numeric - round(numeric)) > 1e-9


def _ibkr_api_stock_quantity(quantity: float) -> tuple[float, bool, str]:
    if _is_fractional(quantity):
        return float(max(1, math.ceil(quantity))), True, "fractional_stock_quantity_rounded_up_to_whole_share_for_ibkr_api_whatif"
    return quantity, False, "as_is"


def _contract_from_preview(preview: dict[str, Any]) -> Contract | None:
    contract_preview = preview.get("contract_preview") or {}
    if str(contract_preview.get("sec_type") or "").upper() != "STK":
        return None
    contract = Contract()
    contract.symbol = str(contract_preview.get("symbol") or preview.get("symbol") or "").upper()
    contract.secType = "STK"
    contract.exchange = str(contract_preview.get("exchange") or "SMART")
    contract.currency = str(contract_preview.get("currency") or "USD")
    return contract


def _order_from_preview(preview: dict[str, Any]) -> Order | None:
    order_preview = preview.get("ibkr_what_if_order_preview") or {}
    quantity = _float_or_none(order_preview.get("total_quantity"))
    if quantity is None or quantity <= 0:
        return None
    api_quantity, normalized, quantity_policy = _ibkr_api_stock_quantity(quantity)
    order = Order()
    order.action = str(order_preview.get("action") or "").upper()
    order.orderType = str(order_preview.get("order_type") or "MKT").upper()
    order.totalQuantity = api_quantity
    order.sourceTotalQuantity = quantity
    order.quantityNormalizedForIbkrApi = bool(normalized)
    order.quantityNormalizationPolicy = quantity_policy
    order.tif = str(order_preview.get("tif") or "DAY")
    # IBKR rejects broker-side what-if requests when transmit=False
    # ("What-If order should have transmit flag set to TRUE").  This does not
    # route a live order because order.whatIf=True is set below; it only lets
    # TWS/Gateway calculate margin/commission preview state.
    order.transmit = True
    order.whatIf = True
    order.outsideRth = bool(order_preview.get("outside_rth") or False)
    # Current TWS/Gateway rejects legacy default Order flags from ibapi for
    # SMART stock orders.  Make them explicit and inert before requesting the
    # broker-side what-if calculation.
    order.eTradeOnly = False
    order.firmQuoteOnly = False
    if order_preview.get("account"):
        order.account = str(order_preview.get("account"))
    return order


def _order_state_payload(order_state: OrderState | None) -> dict[str, Any]:
    if order_state is None:
        return {}
    keys = [
        "status",
        "initMarginBefore",
        "maintMarginBefore",
        "equityWithLoanBefore",
        "initMarginChange",
        "maintMarginChange",
        "equityWithLoanChange",
        "initMarginAfter",
        "maintMarginAfter",
        "equityWithLoanAfter",
        "commission",
        "minCommission",
        "maxCommission",
        "commissionCurrency",
        "warningText",
    ]
    return {key: getattr(order_state, key, None) for key in keys}


def _contract_payload(contract: Contract) -> dict[str, Any]:
    return {
        "symbol": getattr(contract, "symbol", None),
        "sec_type": getattr(contract, "secType", None),
        "exchange": getattr(contract, "exchange", None),
        "currency": getattr(contract, "currency", None),
    }


def _order_payload(order: Order) -> dict[str, Any]:
    return {
        "action": getattr(order, "action", None),
        "order_type": getattr(order, "orderType", None),
        "total_quantity": getattr(order, "totalQuantity", None),
        "source_total_quantity": getattr(order, "sourceTotalQuantity", getattr(order, "totalQuantity", None)),
        "quantity_normalized_for_ibkr_api": bool(getattr(order, "quantityNormalizedForIbkrApi", False)),
        "quantity_normalization_policy": getattr(order, "quantityNormalizationPolicy", "as_is"),
        "tif": getattr(order, "tif", None),
        "account": getattr(order, "account", None),
        "what_if": bool(getattr(order, "whatIf", False)),
        "transmit": bool(getattr(order, "transmit", True)),
        "e_trade_only": bool(getattr(order, "eTradeOnly", False)),
        "firm_quote_only": bool(getattr(order, "firmQuoteOnly", False)),
    }


def _read_only_mode_errors(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for error in errors:
        message = str(error.get("message") or "").lower()
        if "read-only mode" in message or "read only mode" in message:
            matches.append(error)
    return matches


def selected_whatif_preview_items(
    preview_payload: dict[str, Any],
    *,
    include_human_review: bool,
    allow_fractional: bool,
    max_orders: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    result = preview_payload.get("result") or preview_payload
    selected: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for item in result.get("preview_items") or []:
        status = str(item.get("preview_status") or "")
        order_preview = item.get("ibkr_what_if_order_preview") or {}
        reason = None
        if status == "preview_needs_human_review" and not include_human_review:
            reason = "human_review_item_not_included"
        elif status not in {"preview_ready", "preview_needs_human_review"}:
            reason = f"unsupported_preview_status:{status}"
        elif _is_fractional(order_preview.get("total_quantity")) and not allow_fractional:
            reason = "fractional_quantity_not_allowed_for_live_whatif"
        elif _contract_from_preview(item) is None:
            reason = "unsupported_or_missing_contract_preview"
        elif _order_from_preview(item) is None:
            reason = "unsupported_or_missing_order_preview"
        if reason:
            skipped.append({"symbol": item.get("symbol"), "reason": reason, "preview_status": status})
            continue
        selected.append(item)
        if len(selected) >= int(max_orders):
            break
    return selected, skipped


class IBKROrderWhatIfPreviewApp(EWrapper, EClient):
    def __init__(self, config: OrderWhatIfPreviewConfig, preview_items: list[dict[str, Any]]):
        EClient.__init__(self, self)
        self.config = config
        self.preview_items = preview_items
        self.log = logging.getLogger(self.__class__.__name__)
        self._api_thread: threading.Thread | None = None
        self._connected = threading.Event()
        self._next_order_id: int | None = None
        self._requests: dict[int, dict[str, Any]] = {}
        self._responses: dict[int, dict[str, Any]] = {}
        self._errors: list[dict[str, Any]] = []
        self._statuses: list[dict[str, Any]] = []

    def nextValidId(self, orderId: int) -> None:  # noqa: N802
        self._next_order_id = int(orderId)
        self._connected.set()
        next_order_id = int(orderId)
        for preview in self.preview_items:
            contract = _contract_from_preview(preview)
            order = _order_from_preview(preview)
            if contract is None or order is None:
                continue
            order.orderId = next_order_id
            self._requests[next_order_id] = {
                "order_id": next_order_id,
                "symbol": preview.get("symbol"),
                "preview_status": preview.get("preview_status"),
                "contract": _contract_payload(contract),
                "order": _order_payload(order),
                "submitted_at": datetime.now(UTC).isoformat(),
            }
            self.placeOrder(next_order_id, contract, order)
            next_order_id += 1

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

    def openOrder(self, orderId: int, contract: Contract, order: Order, orderState: OrderState) -> None:  # noqa: N802
        self._responses[int(orderId)] = {
            "order_id": int(orderId),
            "symbol": getattr(contract, "symbol", None),
            "contract": _contract_payload(contract),
            "order": _order_payload(order),
            "order_state": _order_state_payload(orderState),
            "received_at": datetime.now(UTC).isoformat(),
        }

    def orderStatus(  # noqa: N802, PLR0913
        self,
        orderId: int,
        status: str,
        filled: float,
        remaining: float,
        avgFillPrice: float,
        permId: int,
        parentId: int,
        lastFillPrice: float,
        clientId: int,
        whyHeld: str,
        mktCapPrice: float,
    ) -> None:
        self._statuses.append(
            {
                "order_id": int(orderId),
                "status": status,
                "filled": filled,
                "remaining": remaining,
                "avg_fill_price": avgFillPrice,
                "perm_id": permId,
                "client_id": clientId,
                "why_held": whyHeld,
                "market_cap_price": mktCapPrice,
                "received_at": datetime.now(UTC).isoformat(),
            }
        )

    def start(self) -> None:
        self.connect(self.config.host, int(self.config.port), int(self.config.client_id))
        self._api_thread = threading.Thread(target=self.run, daemon=True)
        self._api_thread.start()

    def wait(self) -> None:
        deadline = time.monotonic() + float(self.config.timeout_seconds)
        self._connected.wait(timeout=min(5.0, float(self.config.timeout_seconds)))
        while time.monotonic() < deadline:
            if self._requests and len(self._responses) >= len(self._requests):
                break
            time.sleep(0.25)

    def stop(self) -> None:
        try:
            self.disconnect()
        finally:
            if self._api_thread and self._api_thread.is_alive():
                self._api_thread.join(timeout=2.0)

    def summary(self, skipped: list[dict[str, Any]]) -> dict[str, Any]:
        read_only_errors = _read_only_mode_errors(self._errors)
        return {
            "whatif_preview_version": IBKR_ORDER_WHATIF_PREVIEW_VERSION,
            "generated_at": datetime.now(UTC).isoformat(),
            "requested_count": len(self._requests),
            "response_count": len(self._responses),
            "skipped_count": len(skipped),
            "read_only_mode_detected": bool(read_only_errors),
            "read_only_mode_error_count": len(read_only_errors),
            "operator_guidance": {
                "whatif_blocker": "ibkr_api_read_only_mode" if read_only_errors else None,
                "next_step": "Disable API Read-Only mode in TWS/Gateway API settings, restart/reconnect the API session if needed, then rerun the explicit what-if preview. The IBKR API request must keep whatIf=True; TWS/Gateway requires transmit=True for what-if calculation but no live order is routed." if read_only_errors else None,
            },
            "requests": list(self._requests.values()),
            "responses": list(self._responses.values()),
            "order_statuses": self._statuses,
            "skipped_items": skipped,
            "errors": self._errors,
            "config": {
                "host": self.config.host,
                "port": self.config.port,
                "client_id": self.config.client_id,
                "timeout_seconds": self.config.timeout_seconds,
                "max_orders": self.config.max_orders,
                "include_human_review": self.config.include_human_review,
                "allow_fractional": self.config.allow_fractional,
            },
            "safety": {
                "ibkr_order_api_called": bool(self._requests),
                "what_if_only": True,
                "ibkr_whatif_api_transmit_flag": True,
                "live_order_transmitted": False,
                "ibkr_order_submitted": False,
            },
        }


def run_ibkr_order_whatif_preview(
    preview_payload: dict[str, Any],
    config: OrderWhatIfPreviewConfig,
) -> dict[str, Any]:
    preview_items, skipped = selected_whatif_preview_items(
        preview_payload,
        include_human_review=config.include_human_review,
        allow_fractional=config.allow_fractional,
        max_orders=config.max_orders,
    )
    app = IBKROrderWhatIfPreviewApp(config, preview_items)
    app.start()
    try:
        app.wait()
        return app.summary(skipped)
    finally:
        app.stop()


def order_whatif_preview_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# IBKR Order What-If Preview Report",
        "",
        f"- Generated at: `{summary.get('generated_at')}`",
        f"- Requested what-if count: `{summary.get('requested_count')}`",
        f"- Broker response count: `{summary.get('response_count')}`",
        f"- Skipped count: `{summary.get('skipped_count')}`",
        f"- IBKR API Read-Only mode detected: `{summary.get('read_only_mode_detected', False)}`",
        "- Safety: all IBKR requests use `whatIf=True`; IBKR requires the API order `transmit=True` flag for what-if calculation, but no live order is routed.",
    ]
    guidance = summary.get("operator_guidance") or {}
    if summary.get("read_only_mode_detected"):
        lines.extend(
            [
                "",
                "## Operator action required",
                "",
                f"- Blocker: `{guidance.get('whatif_blocker')}`",
                f"- Next step: {guidance.get('next_step')}",
            ]
        )
    lines.extend(
        [
            "",
            "## Broker responses",
            "",
            "| Order ID | Symbol | Status | Init margin change | Maint margin change | Warning |",
            "| ---: | --- | --- | ---: | ---: | --- |",
        ]
    )
    for row in summary.get("responses") or []:
        state = row.get("order_state") or {}
        lines.append(
            f"| {row.get('order_id')} | {row.get('symbol')} | {state.get('status')} "
            f"| {state.get('initMarginChange')} | {state.get('maintMarginChange')} | {state.get('warningText') or '-'} |"
        )
    if not summary.get("responses"):
        lines.append("| - | - | - | - | - | - |")
    lines.extend(["", "## Errors / warnings", "", "| Req ID | Code | Message |", "| ---: | ---: | --- |"])
    for err in summary.get("errors") or []:
        lines.append(f"| {err.get('request_id')} | {err.get('code')} | {err.get('message')} |")
    if not summary.get("errors"):
        lines.append("| - | - | - |")
    lines.extend(["", "## Skipped items", "", "| Symbol | Reason | Preview status |", "| --- | --- | --- |"])
    for item in summary.get("skipped_items") or []:
        lines.append(f"| {item.get('symbol')} | {item.get('reason')} | {item.get('preview_status')} |")
    if not summary.get("skipped_items"):
        lines.append("| - | - | - |")
    return "\n".join(lines) + "\n"


def write_order_whatif_preview_json(output_path: Path, summary: dict[str, Any]) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    return output_path


def write_order_whatif_preview_markdown(output_path: Path, summary: dict[str, Any]) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(order_whatif_preview_markdown(summary), encoding="utf-8")
    return output_path
