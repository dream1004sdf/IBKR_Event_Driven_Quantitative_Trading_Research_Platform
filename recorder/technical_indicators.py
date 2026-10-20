from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from statistics import pstdev
from typing import Any

from .market_outcomes import DailyBar, DailyPriceSource

TECHNICAL_INDICATOR_VERSION = "technical-indicators-v1"


@dataclass(frozen=True)
class TechnicalIndicatorRow:
    symbol: str
    as_of_date: str
    price_source: str
    close: float
    return_1d: float | None
    return_5d: float | None
    return_20d: float | None
    sma_20: float | None
    sma_50: float | None
    ema_12: float | None
    ema_26: float | None
    macd: float | None
    macd_signal: float | None
    rsi_14: float | None
    volatility_20: float | None
    source_start_date: str
    source_end_date: str
    ingestion_version: str = TECHNICAL_INDICATOR_VERSION


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def _return(closes: list[float], index: int, lag: int) -> float | None:
    if index < lag:
        return None
    previous = closes[index - lag]
    if previous <= 0:
        return None
    return closes[index] / previous - 1.0


def _ema_series(values: list[float], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    if len(values) < period:
        return result
    seed = sum(values[:period]) / period
    result[period - 1] = seed
    alpha = 2.0 / (period + 1.0)
    previous = seed
    for index in range(period, len(values)):
        previous = values[index] * alpha + previous * (1.0 - alpha)
        result[index] = previous
    return result


def _rsi14(closes: list[float], index: int) -> float | None:
    period = 14
    if index < period:
        return None
    gains: list[float] = []
    losses: list[float] = []
    for cursor in range(index - period + 1, index + 1):
        delta = closes[cursor] - closes[cursor - 1]
        if delta >= 0:
            gains.append(delta)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(abs(delta))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _volatility20(closes: list[float], index: int) -> float | None:
    if index < 20:
        return None
    returns: list[float] = []
    for cursor in range(index - 19, index + 1):
        previous = closes[cursor - 1]
        if previous <= 0:
            return None
        returns.append(closes[cursor] / previous - 1.0)
    return pstdev(returns) * math.sqrt(252.0)


def build_indicator_rows(
    *,
    symbol: str,
    bars: list[DailyBar],
    price_source: str,
    min_date: date | None = None,
    max_rows: int = 1000,
) -> list[TechnicalIndicatorRow]:
    ordered = sorted((bar for bar in bars if bar.close > 0), key=lambda item: item.date)
    if not ordered:
        return []
    closes = [float(bar.close) for bar in ordered]
    ema12 = _ema_series(closes, 12)
    ema26 = _ema_series(closes, 26)
    macd_values: list[float | None] = []
    for index in range(len(closes)):
        macd_values.append(None if ema12[index] is None or ema26[index] is None else ema12[index] - ema26[index])
    macd_for_signal = [0.0 if value is None else value for value in macd_values]
    macd_signal = _ema_series(macd_for_signal, 9)

    rows: list[TechnicalIndicatorRow] = []
    for index, bar in enumerate(ordered):
        if min_date is not None and bar.date < min_date:
            continue
        sma20 = _mean(closes[index - 19:index + 1]) if index >= 19 else None
        sma50 = _mean(closes[index - 49:index + 1]) if index >= 49 else None
        rows.append(
            TechnicalIndicatorRow(
                symbol=symbol.strip().upper(),
                as_of_date=bar.date.isoformat(),
                price_source=price_source,
                close=closes[index],
                return_1d=_return(closes, index, 1),
                return_5d=_return(closes, index, 5),
                return_20d=_return(closes, index, 20),
                sma_20=sma20,
                sma_50=sma50,
                ema_12=ema12[index],
                ema_26=ema26[index],
                macd=macd_values[index],
                macd_signal=macd_signal[index] if macd_values[index] is not None else None,
                rsi_14=_rsi14(closes, index),
                volatility_20=_volatility20(closes, index),
                source_start_date=ordered[0].date.isoformat(),
                source_end_date=ordered[-1].date.isoformat(),
            )
        )
    rows.sort(key=lambda item: item.as_of_date, reverse=True)
    return rows[: max(0, int(max_rows))]


class TechnicalIndicatorBuilder:
    def __init__(self, database: Any, price_source: DailyPriceSource) -> None:
        self.database = database
        self.price_source = price_source

    def build(
        self,
        *,
        symbols: tuple[str, ...],
        start: date,
        end: date,
        lookback_days: int = 90,
        limit_per_symbol: int = 1000,
    ) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "ingestion_version": TECHNICAL_INDICATOR_VERSION,
            "price_source": self.price_source.name,
            "symbols": [symbol.strip().upper() for symbol in symbols],
            "stored": 0,
            "updated": 0,
            "errors": 0,
            "results": [],
        }
        source_start = date.fromordinal(max(1, start.toordinal() - max(0, int(lookback_days))))
        try:
            for symbol in symbols:
                normalized = symbol.strip().upper()
                item = {"symbol": normalized, "bars": 0, "indicators": 0, "stored": 0, "updated": 0, "error": None}
                try:
                    bars = self.price_source.get_daily_bars(normalized, source_start, end)
                    item["bars"] = len(bars)
                    rows = build_indicator_rows(
                        symbol=normalized,
                        bars=bars,
                        price_source=self.price_source.name,
                        min_date=start,
                        max_rows=limit_per_symbol,
                    )
                    item["indicators"] = len(rows)
                    for row in rows:
                        _, created = self.database.upsert_technical_indicator(row)
                        if created:
                            summary["stored"] += 1
                            item["stored"] += 1
                        else:
                            summary["updated"] += 1
                            item["updated"] += 1
                except Exception as exc:
                    summary["errors"] += 1
                    item["error"] = str(exc)
                summary["results"].append(item)
        finally:
            close = getattr(self.price_source, "close", None)
            if callable(close):
                close()
        return summary
