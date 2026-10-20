from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone

from .models import QuoteSnapshot

UTC = timezone.utc


@dataclass(frozen=True)
class QuoteIntegrityConfig:
    enabled: bool = True
    max_age_seconds: int = 180
    max_spread_ratio: float = 0.05
    max_last_midpoint_deviation: float = 0.10
    max_close_reference_deviation: float = 0.75
    require_stock_contract: bool = True


@dataclass(frozen=True)
class QuoteIntegrityResult:
    status: str
    eligible: bool
    selected_price: float | None
    selected_source: str | None
    selected_at: datetime | None
    reasons: tuple[str, ...]
    spread_ratio: float | None = None
    last_midpoint_deviation: float | None = None
    close_reference_deviation: float | None = None


def _ratio(a: float | None, b: float | None) -> float | None:
    if a is None or b is None or a <= 0 or b <= 0:
        return None
    return abs(a - b) / max(abs(b), 1e-12)


def assess_quote_integrity(
    quote: QuoteSnapshot,
    *,
    reference_time: datetime | None = None,
    config: QuoteIntegrityConfig | None = None,
) -> QuoteIntegrityResult:
    config = config or QuoteIntegrityConfig()
    price, source, selected_at = quote.selected_price_with_time()
    if not config.enabled:
        return QuoteIntegrityResult(
            status="not_checked",
            eligible=price is not None,
            selected_price=price,
            selected_source=source,
            selected_at=selected_at,
            reasons=("quote integrity disabled",),
        )

    failures: list[str] = []
    warnings: list[str] = []

    symbol = str(quote.symbol or "").upper()
    contract_symbol = str(quote.contract_symbol or "").upper()
    sec_type = str(quote.sec_type or "").upper()
    currency = str(quote.currency or "unknown").upper()

    if contract_symbol and contract_symbol != symbol:
        failures.append(f"contract symbol mismatch: quote={symbol} contract={contract_symbol}")
    elif not contract_symbol:
        warnings.append("contract symbol unavailable")

    if config.require_stock_contract and sec_type and sec_type != "STK":
        failures.append(f"unexpected security type: {sec_type}")
    elif config.require_stock_contract and not sec_type:
        warnings.append("security type unavailable")

    if quote.con_id is not None and int(quote.con_id) <= 0:
        failures.append("invalid conId")
    elif quote.con_id is None:
        warnings.append("conId unavailable")

    if currency in {"", "UNKNOWN"}:
        warnings.append("contract currency unavailable")

    multiplier = str(quote.multiplier or "").strip()
    if multiplier:
        try:
            parsed_multiplier = float(multiplier)
        except ValueError:
            warnings.append(f"unparseable contract multiplier: {multiplier}")
        else:
            if not math.isclose(parsed_multiplier, 1.0, rel_tol=0.0, abs_tol=1e-9):
                failures.append(f"unexpected stock multiplier: {multiplier}")

    if quote.bid is not None and quote.ask is not None:
        if quote.bid <= 0 or quote.ask <= 0:
            failures.append("non-positive bid or ask")
        elif quote.bid > quote.ask:
            failures.append("bid exceeds ask")
    elif quote.bid is None and quote.ask is None:
        warnings.append("bid and ask unavailable")
    else:
        warnings.append("one-sided quote")

    midpoint = quote.midpoint
    spread_ratio: float | None = None
    if midpoint is not None and quote.bid is not None and quote.ask is not None:
        spread_ratio = (quote.ask - quote.bid) / midpoint
        if spread_ratio > config.max_spread_ratio:
            failures.append(
                f"bid-ask spread ratio {spread_ratio:.4f} exceeds {config.max_spread_ratio:.4f}"
            )

    last_midpoint_deviation = _ratio(quote.last, midpoint)
    if (
        last_midpoint_deviation is not None
        and last_midpoint_deviation > config.max_last_midpoint_deviation
    ):
        failures.append(
            "last/midpoint deviation "
            f"{last_midpoint_deviation:.4f} exceeds {config.max_last_midpoint_deviation:.4f}"
        )

    close_reference_deviation = _ratio(quote.close, price)
    if (
        close_reference_deviation is not None
        and close_reference_deviation > config.max_close_reference_deviation
    ):
        warnings.append(
            "close/reference deviation "
            f"{close_reference_deviation:.4f} exceeds {config.max_close_reference_deviation:.4f}"
        )

    if price is None or source is None or selected_at is None:
        failures.append("no usable event-time price")
    else:
        if not math.isfinite(float(price)) or float(price) <= 0:
            failures.append("selected price is not finite and positive")
        if reference_time is not None:
            ref = reference_time.astimezone(UTC)
            observed = selected_at.astimezone(UTC)
            age = abs((ref - observed).total_seconds())
            if age > config.max_age_seconds:
                failures.append(
                    f"quote age {age:.1f}s exceeds {config.max_age_seconds}s"
                )

    if failures:
        status = "failed"
        eligible = False
        reasons = tuple(failures + warnings)
        price = None
        source = None
    elif warnings:
        status = "warning"
        eligible = price is not None
        reasons = tuple(warnings)
    else:
        status = "valid"
        eligible = price is not None
        reasons = ("contract and quote checks passed",)

    return QuoteIntegrityResult(
        status=status,
        eligible=eligible,
        selected_price=price,
        selected_source=source,
        selected_at=selected_at,
        reasons=reasons,
        spread_ratio=spread_ratio,
        last_midpoint_deviation=last_midpoint_deviation,
        close_reference_deviation=close_reference_deviation,
    )
