from __future__ import annotations

def _parse_float_grid(text: str | None, default: tuple[float, ...]) -> tuple[float, ...]:
    if not text:
        return default
    values = []
    for item in text.split(","):
        stripped = item.strip()
        if stripped:
            values.append(float(stripped))
    return tuple(values) or default


def _parse_float_tuple(text: str | None, default: tuple[float, ...]) -> tuple[float, ...]:
    if not text:
        return default
    values: list[float] = []
    for item in text.split(","):
        stripped = item.strip()
        if stripped:
            values.append(float(stripped))
    return tuple(values) or default


def _parse_inline_mark_prices(value: str | None) -> dict[str, float]:
    marks: dict[str, float] = {}
    if not value:
        return marks
    for item in value.split(","):
        token = item.strip()
        if not token:
            continue
        if "=" not in token:
            raise ValueError(f"Invalid mark token {token!r}; expected SYMBOL=PRICE")
        symbol, raw_price = token.split("=", 1)
        symbol = symbol.strip().upper()
        if not symbol:
            raise ValueError(f"Invalid mark token {token!r}; symbol is empty")
        try:
            price = float(raw_price.strip())
        except ValueError as exc:
            raise ValueError(f"Invalid mark token {token!r}; price must be numeric") from exc
        if price <= 0:
            raise ValueError(f"Invalid mark token {token!r}; price must be positive")
        marks[symbol] = price
    return marks
