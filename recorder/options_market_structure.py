from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any


OPTIONS_MARKET_STRUCTURE_VERSION = "options-market-structure-desk-v2-hardening"


def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _age_seconds(value: Any, *, now: datetime | None = None) -> float | None:
    parsed = _parse_datetime(value)
    if parsed is None:
        return None
    now = now or datetime.now(UTC)
    return round(max(0.0, (now - parsed).total_seconds()), 3)


def _parse_expiration(value: Any) -> date | None:
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%Y%m%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:10] if fmt == "%Y-%m-%d" else text[:8], fmt).date()
        except ValueError:
            continue
    return None


def _derive_dte(quote: dict[str, Any], *, as_of: datetime) -> int | None:
    dte = _num(quote.get("dte"))
    if dte is not None:
        return int(round(dte))
    expiration = _parse_expiration(quote.get("expiration") or quote.get("expiry"))
    if expiration is None:
        return None
    return max(0, (expiration - as_of.date()).days)


def _expiry_bucket(dte: int | None) -> str:
    if dte is None:
        return "unknown_dte"
    if dte <= 0:
        return "0dte"
    if dte == 1:
        return "1dte"
    if dte <= 7:
        return "weekly_2_7dte"
    if dte <= 35:
        return "front_month_8_35dte"
    return "deferred_36plus_dte"


def _distance_pct(level: Any, spot: Any) -> float | None:
    level_num = _num(level)
    spot_num = _num(spot)
    if level_num is None or spot_num in (None, 0):
        return None
    return round((level_num / spot_num - 1.0) * 100.0, 3)


def _confidence_level(score: float) -> str:
    if score >= 80:
        return "high"
    if score >= 55:
        return "medium"
    return "low"


def _read_json(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        payload = json.loads(path.expanduser().read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _market_prices(market_data: dict[str, Any] | None) -> dict[str, float]:
    prices: dict[str, float] = {}
    for row in (market_data or {}).get("rows") or []:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("key") or row.get("symbol") or "").strip().upper()
        price = _num(row.get("price") if row.get("price") is not None else row.get("last") if row.get("last") is not None else row.get("close"))
        if symbol and price is not None:
            prices[symbol] = price
    return prices


def _option_model(quote: dict[str, Any]) -> dict[str, Any]:
    computations = quote.get("option_computation") if isinstance(quote.get("option_computation"), dict) else {}
    for key in ("MODEL_OPTION", "BID_OPTION_COMPUTATION", "ASK_OPTION_COMPUTATION", "LAST_OPTION_COMPUTATION"):
        model = computations.get(key)
        if isinstance(model, dict) and model:
            return model
    return {}


def _quote_mid(quote: dict[str, Any]) -> float | None:
    mid = _num(quote.get("mid"))
    if mid is not None:
        return mid
    bid = _num(quote.get("bid"))
    ask = _num(quote.get("ask"))
    if bid is not None and ask is not None and bid > 0 and ask > 0 and ask >= bid:
        return (bid + ask) / 2.0
    return _num(quote.get("last") if quote.get("last") is not None else quote.get("close"))


def _quote_spot(quote: dict[str, Any], market_prices: dict[str, float]) -> float | None:
    symbol = str(quote.get("symbol") or "").strip().upper()
    if symbol in market_prices:
        return market_prices[symbol]
    model = _option_model(quote)
    return _num(model.get("underlying_price")) or _num(quote.get("reference_price"))


def _quote_gamma(quote: dict[str, Any]) -> float | None:
    gamma = _num(quote.get("gamma"))
    if gamma is not None:
        return gamma
    return _num(_option_model(quote).get("gamma"))


def _quote_delta(quote: dict[str, Any]) -> float | None:
    delta = _num(quote.get("delta"))
    if delta is not None:
        return delta
    return _num(_option_model(quote).get("delta"))


def _exposure_unit(quote: dict[str, Any], *, fallback_to_volume: bool) -> tuple[float, str]:
    oi = _num(quote.get("open_interest"))
    if oi is not None and oi > 0:
        return oi, "open_interest"
    if fallback_to_volume:
        volume = _num(quote.get("volume"))
        if volume is not None and volume > 0:
            return volume, "volume_proxy"
    return 0.0, "missing"


def _max_pain_by_symbol(quotes: list[dict[str, Any]], strikes: list[float], *, fallback_to_volume: bool) -> dict[str, Any]:
    pain_by_strike: list[dict[str, Any]] = []
    for candidate in strikes:
        total_pain = 0.0
        call_pain = 0.0
        put_pain = 0.0
        for quote in quotes:
            strike = _num(quote.get("strike"))
            if strike is None:
                continue
            units, _source = _exposure_unit(quote, fallback_to_volume=fallback_to_volume)
            multiplier = _num(quote.get("multiplier")) or 100.0
            right = str(quote.get("right") or "").upper()
            if right == "C":
                value = max(0.0, candidate - strike) * units * multiplier
                call_pain += value
            elif right == "P":
                value = max(0.0, strike - candidate) * units * multiplier
                put_pain += value
            else:
                value = 0.0
            total_pain += value
        pain_by_strike.append({"strike": candidate, "total_pain": total_pain, "call_pain": call_pain, "put_pain": put_pain})
    if not pain_by_strike:
        return {"max_pain": None, "pain_by_strike": []}
    best = min(pain_by_strike, key=lambda item: (float(item.get("total_pain") or 0.0), float(item.get("strike") or 0.0)))
    return {"max_pain": best.get("strike"), "pain_by_strike": pain_by_strike}


def _max_pain_from_rows(rows: list[dict[str, Any]], strikes: list[float]) -> dict[str, Any]:
    pain_by_strike: list[dict[str, Any]] = []
    for candidate in strikes:
        total_pain = 0.0
        call_pain = 0.0
        put_pain = 0.0
        for row in rows:
            strike = _num(row.get("strike"))
            if strike is None:
                continue
            units = float(row.get("exposure_units") or 0.0)
            multiplier = _num(row.get("multiplier")) or 100.0
            right = str(row.get("right") or "").upper()
            if right == "C":
                value = max(0.0, candidate - strike) * units * multiplier
                call_pain += value
            elif right == "P":
                value = max(0.0, strike - candidate) * units * multiplier
                put_pain += value
            else:
                value = 0.0
            total_pain += value
        pain_by_strike.append({"strike": candidate, "total_pain": total_pain, "call_pain": call_pain, "put_pain": put_pain})
    if not pain_by_strike:
        return {"max_pain": None, "pain_by_strike": []}
    best = min(pain_by_strike, key=lambda item: (float(item.get("total_pain") or 0.0), float(item.get("strike") or 0.0)))
    return {"max_pain": best.get("strike"), "pain_by_strike": pain_by_strike}


def _summarize_rows(
    rows: list[dict[str, Any]],
    *,
    spot: float | None,
    pin_near_pct: float,
    proximity_near_pct: float,
) -> dict[str, Any]:
    strikes = sorted({float(row["strike"]) for row in rows if row.get("strike") is not None})
    by_strike: dict[float, dict[str, Any]] = {}
    for strike in strikes:
        strike_rows = [row for row in rows if float(row.get("strike") or 0.0) == strike]
        call_units = sum(float(row.get("exposure_units") or 0.0) for row in strike_rows if row.get("right") == "C")
        put_units = sum(float(row.get("exposure_units") or 0.0) for row in strike_rows if row.get("right") == "P")
        call_gex = sum(float(row.get("gamma_exposure") or 0.0) for row in strike_rows if row.get("right") == "C")
        put_gex = sum(float(row.get("gamma_exposure") or 0.0) for row in strike_rows if row.get("right") == "P")
        by_strike[strike] = {
            "strike": strike,
            "call_units": call_units,
            "put_units": put_units,
            "net_units": call_units - put_units,
            "call_gamma_exposure": call_gex,
            "put_gamma_exposure": put_gex,
            "net_gamma_exposure": call_gex + put_gex,
        }
    levels = list(by_strike.values())
    call_wall = max(levels, key=lambda item: (float(item.get("call_units") or 0.0), float(item.get("call_gamma_exposure") or 0.0)), default={})
    put_wall = max(levels, key=lambda item: (float(item.get("put_units") or 0.0), abs(float(item.get("put_gamma_exposure") or 0.0))), default={})
    max_pain = _max_pain_from_rows(rows, strikes)
    total_gex = sum(float(item.get("net_gamma_exposure") or 0.0) for item in levels)
    gamma_regime = "positive_gamma_mean_reversion_bias" if total_gex > 0 else "negative_gamma_trend_amplification_bias" if total_gex < 0 else "neutral_or_missing_gamma"

    cumulative = 0.0
    gamma_flip = None
    previous_sign = 0
    for item in sorted(levels, key=lambda row: float(row.get("strike") or 0.0)):
        cumulative += float(item.get("net_gamma_exposure") or 0.0)
        sign = 1 if cumulative > 0 else -1 if cumulative < 0 else 0
        if previous_sign and sign and sign != previous_sign:
            gamma_flip = item.get("strike")
            break
        if sign:
            previous_sign = sign

    pin_level = max_pain.get("max_pain") or (call_wall.get("strike") if call_wall else None)
    distance_to_pin_pct = _distance_pct(pin_level, spot)
    proximity = {
        "call_wall_distance_pct": _distance_pct(call_wall.get("strike"), spot),
        "put_wall_distance_pct": _distance_pct(put_wall.get("strike"), spot),
        "max_pain_distance_pct": _distance_pct(max_pain.get("max_pain"), spot),
        "gamma_flip_distance_pct": _distance_pct(gamma_flip, spot),
    }
    proximity_alerts: list[str] = []
    for key, value in proximity.items():
        if value is not None and abs(float(value)) <= float(proximity_near_pct):
            proximity_alerts.append(f"near_{key.removesuffix('_distance_pct')}")
    pin_risk = bool(distance_to_pin_pct is not None and abs(distance_to_pin_pct) <= float(pin_near_pct))
    if pin_risk:
        proximity_alerts.append("near_pin_level")
    return {
        "strike_count": len(levels),
        "total_gamma_exposure": round(total_gex, 4),
        "gamma_regime": gamma_regime,
        "gamma_flip": gamma_flip,
        "call_wall": call_wall.get("strike"),
        "put_wall": put_wall.get("strike"),
        "max_pain": max_pain.get("max_pain"),
        "pin_level": pin_level,
        "distance_to_pin_pct": distance_to_pin_pct,
        "pin_risk": pin_risk,
        "proximity": proximity,
        "proximity_alerts": sorted(set(proximity_alerts)),
        "levels": sorted(levels, key=lambda row: float(row.get("strike") or 0.0))[:200],
        "top_gamma_levels": sorted(levels, key=lambda row: abs(float(row.get("net_gamma_exposure") or 0.0)), reverse=True)[:10],
        "pain_by_strike": max_pain.get("pain_by_strike", [])[:200],
    }


def _symbol_structure(
    symbol: str,
    quotes: list[dict[str, Any]],
    *,
    market_prices: dict[str, float],
    source_symbol_quality: dict[str, Any] | None,
    fallback_to_volume: bool,
    pin_near_pct: float,
    proximity_near_pct: float,
    as_of: datetime,
    source_quote_age_seconds: float | None,
    max_snapshot_age_minutes: float,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    warning_counts = Counter()
    exposure_source_counts = Counter()
    quote_quality_counts = Counter()
    expiry_bucket_counts = Counter()
    spot = None
    for quote in quotes:
        strike = _num(quote.get("strike"))
        right = str(quote.get("right") or "").upper()
        if strike is None or right not in {"C", "P"}:
            continue
        gamma = _quote_gamma(quote)
        delta = _quote_delta(quote)
        quote_spot = _quote_spot(quote, market_prices)
        if spot is None and quote_spot is not None:
            spot = quote_spot
        units, exposure_source = _exposure_unit(quote, fallback_to_volume=fallback_to_volume)
        exposure_source_counts[exposure_source] += 1
        dte = _derive_dte(quote, as_of=as_of)
        expiry_bucket = _expiry_bucket(dte)
        expiry_bucket_counts[expiry_bucket] += 1
        multiplier = _num(quote.get("multiplier")) or 100.0
        gamma_exposure = 0.0
        if gamma is not None and quote_spot is not None and units > 0:
            # Heuristic dealer-gamma convention: calls add positive gamma, puts subtract gamma.
            # Scaled to approximate dollar gamma per 1% underlying move.
            sign = 1.0 if right == "C" else -1.0
            gamma_exposure = sign * gamma * units * multiplier * quote_spot * quote_spot * 0.01
        else:
            warning_counts["missing_gamma_or_units"] += 1
        spread_pct = _num(quote.get("spread_pct"))
        has_bid_ask = bool(quote.get("has_bid_ask"))
        if has_bid_ask:
            quote_quality_counts["has_bid_ask"] += 1
        else:
            quote_quality_counts["missing_bid_ask"] += 1
        if spread_pct is not None and spread_pct > float(quote.get("max_spread_pct") or 0.3):
            quote_quality_counts["wide_spread"] += 1
        rows.append(
            {
                "symbol": symbol,
                "expiration": quote.get("expiration") or quote.get("expiry"),
                "dte": dte,
                "expiry_bucket": expiry_bucket,
                "strike": strike,
                "right": right,
                "spot": quote_spot,
                "gamma": gamma,
                "delta": delta,
                "open_interest": quote.get("open_interest"),
                "volume": quote.get("volume"),
                "exposure_units": units,
                "exposure_source": exposure_source,
                "gamma_exposure": gamma_exposure,
                "multiplier": multiplier,
                "mid": _quote_mid(quote),
                "has_bid_ask": quote.get("has_bid_ask"),
                "spread_pct": spread_pct,
                "quote_status": quote.get("quote_status"),
            }
        )
    aggregate = _summarize_rows(rows, spot=spot, pin_near_pct=pin_near_pct, proximity_near_pct=proximity_near_pct)
    expiry_buckets: list[dict[str, Any]] = []
    for bucket, bucket_rows in sorted(defaultdict(list, {bucket: [row for row in rows if row.get("expiry_bucket") == bucket] for bucket in expiry_bucket_counts}).items()):
        bucket_summary = _summarize_rows(bucket_rows, spot=spot, pin_near_pct=pin_near_pct, proximity_near_pct=proximity_near_pct)
        expiry_buckets.append(
            {
                "bucket": bucket,
                "quote_count": len(bucket_rows),
                "expiration_count": len({row.get("expiration") for row in bucket_rows if row.get("expiration")}),
                "min_dte": min((int(row.get("dte")) for row in bucket_rows if row.get("dte") is not None), default=None),
                "max_dte": max((int(row.get("dte")) for row in bucket_rows if row.get("dte") is not None), default=None),
                "total_gamma_exposure": bucket_summary.get("total_gamma_exposure"),
                "gamma_regime": bucket_summary.get("gamma_regime"),
                "call_wall": bucket_summary.get("call_wall"),
                "put_wall": bucket_summary.get("put_wall"),
                "max_pain": bucket_summary.get("max_pain"),
                "pin_risk": bucket_summary.get("pin_risk"),
                "proximity_alerts": bucket_summary.get("proximity_alerts") or [],
            }
        )

    confidence_score = 100.0
    warnings: list[str] = []
    quote_count = max(1, len(rows))
    if exposure_source_counts.get("open_interest", 0) == 0 and exposure_source_counts.get("volume_proxy", 0) > 0:
        confidence_score -= 20.0
        warnings.append("open_interest_missing_using_volume_proxy")
    if exposure_source_counts.get("missing", 0):
        confidence_score -= min(35.0, 35.0 * exposure_source_counts.get("missing", 0) / quote_count)
        warnings.append("some_contracts_missing_oi_and_volume")
    if warning_counts.get("missing_gamma_or_units"):
        confidence_score -= min(25.0, 25.0 * warning_counts.get("missing_gamma_or_units", 0) / quote_count)
        warnings.append("some_contracts_missing_gamma_or_exposure_units")
    if quote_quality_counts.get("missing_bid_ask", 0):
        confidence_score -= min(15.0, 15.0 * quote_quality_counts.get("missing_bid_ask", 0) / quote_count)
        warnings.append("some_contracts_missing_bid_ask")
    if quote_quality_counts.get("wide_spread", 0):
        confidence_score -= min(10.0, 10.0 * quote_quality_counts.get("wide_spread", 0) / quote_count)
        warnings.append("some_contracts_wide_spread")
    if spot is None:
        confidence_score -= 35.0
        warnings.append("underlying_spot_missing")
    if source_quote_age_seconds is None:
        confidence_score -= 20.0
        warnings.append("source_quote_timestamp_missing")
    elif float(max_snapshot_age_minutes) < 1440.0 and source_quote_age_seconds > float(max_snapshot_age_minutes) * 60.0:
        confidence_score -= 25.0
        warnings.append("source_quote_snapshot_stale")
    source_symbol_quality = source_symbol_quality or {}
    requested = _num(source_symbol_quality.get("requested_quotes") or source_symbol_quality.get("requested"))
    quoted = _num(source_symbol_quality.get("quoted_contracts") or source_symbol_quality.get("quoted"))
    quoted_rate = _num(source_symbol_quality.get("quoted_rate"))
    valid_contract_rate = _num(source_symbol_quality.get("valid_contract_rate"))
    if quoted_rate is None and requested and quoted is not None and requested > 0:
        quoted_rate = quoted / requested
    if quoted_rate is not None and quoted_rate < 0.35:
        confidence_score -= min(20.0, (0.35 - quoted_rate) / 0.35 * 20.0)
        warnings.append("low_symbol_quote_rate")
    if valid_contract_rate is not None and valid_contract_rate < 0.80:
        confidence_score -= min(18.0, (0.80 - valid_contract_rate) / 0.80 * 18.0)
        warnings.append("low_valid_contract_rate")
    if expiry_bucket_counts and len(expiry_bucket_counts) == 1 and expiry_bucket_counts.get("0dte"):
        confidence_score -= 8.0
        warnings.append("expiry_bucket_concentrated_0dte")
    confidence_score = round(max(0.0, min(100.0, confidence_score)), 1)
    confidence = _confidence_level(confidence_score)

    return {
        "symbol": symbol,
        "spot": spot,
        "quote_count": len(rows),
        "strike_count": aggregate.get("strike_count"),
        "total_gamma_exposure": aggregate.get("total_gamma_exposure"),
        "gamma_regime": aggregate.get("gamma_regime"),
        "gamma_flip": aggregate.get("gamma_flip"),
        "call_wall": aggregate.get("call_wall"),
        "put_wall": aggregate.get("put_wall"),
        "max_pain": aggregate.get("max_pain"),
        "pin_level": aggregate.get("pin_level"),
        "distance_to_pin_pct": aggregate.get("distance_to_pin_pct"),
        "pin_risk": aggregate.get("pin_risk"),
        "proximity": aggregate.get("proximity") or {},
        "proximity_alerts": aggregate.get("proximity_alerts") or [],
        "exposure_source_counts": dict(exposure_source_counts),
        "quote_quality_counts": dict(quote_quality_counts),
        "source_symbol_quality": source_symbol_quality,
        "expiry_bucket_counts": dict(expiry_bucket_counts),
        "expiry_buckets": expiry_buckets,
        "confidence_score": confidence_score,
        "confidence": confidence,
        "warnings": warnings,
        "levels": aggregate.get("levels") or [],
        "top_gamma_levels": aggregate.get("top_gamma_levels") or [],
        "pain_by_strike": aggregate.get("pain_by_strike") or [],
    }


def build_options_market_structure_report(
    *,
    option_quote_snapshot: dict[str, Any],
    market_data: dict[str, Any] | None = None,
    fallback_to_volume: bool = True,
    pin_near_pct: float = 0.5,
    proximity_near_pct: float = 1.0,
    max_snapshot_age_minutes: float = 30.0,
) -> dict[str, Any]:
    quotes = [row for row in (option_quote_snapshot.get("quotes") or []) if isinstance(row, dict)]
    market_prices = _market_prices(market_data)
    generated_at_dt = datetime.now(UTC)
    generated_at = generated_at_dt.isoformat()
    source_quote_generated_at = option_quote_snapshot.get("generated_at")
    source_quote_age_seconds = _age_seconds(source_quote_generated_at, now=generated_at_dt)
    source_quote_age_minutes = round(source_quote_age_seconds / 60.0, 3) if source_quote_age_seconds is not None else None
    market_data_generated_at = (market_data or {}).get("generated_at")
    market_data_age_seconds = _age_seconds(market_data_generated_at, now=generated_at_dt)
    max_age_seconds = float(max_snapshot_age_minutes) * 60.0
    if source_quote_age_seconds is None:
        freshness_status = "unknown_timestamp"
    elif source_quote_age_seconds <= max_age_seconds or float(max_snapshot_age_minutes) >= 1440.0:
        freshness_status = "fresh"
    else:
        freshness_status = "stale"
    by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for quote in quotes:
        symbol = str(quote.get("symbol") or "").strip().upper()
        if symbol:
            by_symbol[symbol].append(quote)
    source_quality_by_symbol = {
        str(row.get("symbol") or "").strip().upper(): row
        for row in option_quote_snapshot.get("by_symbol") or []
        if isinstance(row, dict) and str(row.get("symbol") or "").strip()
    }
    symbols = [
        _symbol_structure(
            symbol,
            symbol_quotes,
            market_prices=market_prices,
            source_symbol_quality=source_quality_by_symbol.get(symbol),
            fallback_to_volume=fallback_to_volume,
            pin_near_pct=pin_near_pct,
            proximity_near_pct=proximity_near_pct,
            as_of=_parse_datetime(source_quote_generated_at) or generated_at_dt,
            source_quote_age_seconds=source_quote_age_seconds,
            max_snapshot_age_minutes=max_snapshot_age_minutes,
        )
        for symbol, symbol_quotes in sorted(by_symbol.items())
    ]
    regime_counts = Counter(item.get("gamma_regime") for item in symbols)
    pin_count = sum(1 for item in symbols if item.get("pin_risk"))
    proximity_alert_symbols = [item.get("symbol") for item in symbols if item.get("proximity_alerts")]
    confidence_counts = Counter(item.get("confidence") for item in symbols)
    missing_oi_symbols = [item.get("symbol") for item in symbols if (item.get("exposure_source_counts") or {}).get("open_interest", 0) == 0]
    expiry_bucket_counts = Counter()
    for item in symbols:
        expiry_bucket_counts.update(item.get("expiry_bucket_counts") or {})
    status = "green"
    if not quotes:
        status = "red"
    elif confidence_counts.get("low", 0):
        status = "yellow"
    elif missing_oi_symbols:
        status = "yellow"
    elif freshness_status != "fresh":
        status = "yellow"
    session_guard = {
        "freshness_status": freshness_status,
        "source_quote_age_seconds": source_quote_age_seconds,
        "source_quote_age_minutes": source_quote_age_minutes,
        "max_snapshot_age_minutes": float(max_snapshot_age_minutes),
        "market_data_generated_at": market_data_generated_at,
        "market_data_age_seconds": market_data_age_seconds,
        "interpretation": "fresh_option_structure_context" if freshness_status == "fresh" else "refresh_option_snapshot_before_live_use",
        "live_validation_required_when_options_market_opens": freshness_status != "fresh",
    }
    return {
        "options_market_structure_version": OPTIONS_MARKET_STRUCTURE_VERSION,
        "generated_at": generated_at,
        "source_quote_snapshot_version": option_quote_snapshot.get("snapshot_version") or option_quote_snapshot.get("option_quote_snapshot_version"),
        "source_quote_generated_at": source_quote_generated_at,
        "quote_count": len(quotes),
        "symbol_count": len(symbols),
        "status": status,
        "fallback_to_volume": bool(fallback_to_volume),
        "pin_near_pct": float(pin_near_pct),
        "proximity_near_pct": float(proximity_near_pct),
        "session_guard": session_guard,
        "summary": {
            "gamma_regime_counts": dict(regime_counts),
            "pin_risk_symbol_count": pin_count,
            "proximity_alert_symbol_count": len(proximity_alert_symbols),
            "proximity_alert_symbols": proximity_alert_symbols,
            "confidence_counts": dict(confidence_counts),
            "missing_open_interest_symbols": missing_oi_symbols,
            "expiry_bucket_counts": dict(expiry_bucket_counts),
        },
        "symbols": symbols,
        "scorer_overlay": {
            "available": bool(symbols),
            "freshness_status": freshness_status,
            "should_downweight_when_stale": freshness_status != "fresh",
            "proximity_alert_symbols": proximity_alert_symbols,
            "use_when": [
                "newsless_index_move",
                "late_day_pin_risk",
                "expiration_or_0dte_session",
                "market_move_near_call_or_put_wall",
                "market_move_near_gamma_flip",
            ],
            "do_not_use_as_primary_cause": True,
            "notes": [
                "Gamma exposure is a heuristic derived from available quote snapshot greeks and OI/volume.",
                "If open interest is missing, volume proxy lowers confidence and should be treated as flow evidence, not confirmed dealer positioning.",
                "Stale option snapshots should be downweighted until a fresh regular-session quote snapshot is available.",
            ],
        },
        "safety": {
            "read_only": True,
            "uses_stored_option_snapshot": True,
            "ibkr_order_submitted": False,
            "transmit_enabled": False,
            "live_order_routing_enabled": False,
            "portfolio_mutated": False,
        },
    }


def options_market_structure_markdown(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    session_guard = report.get("session_guard") or {}
    lines = [
        "# Options Market Structure Desk",
        "",
        f"- Version: `{report.get('options_market_structure_version')}`",
        f"- Generated at: `{report.get('generated_at')}`",
        f"- Status: `{report.get('status')}`",
        f"- Source quote snapshot: `{report.get('source_quote_snapshot_version')}` at `{report.get('source_quote_generated_at')}`",
        f"- Snapshot freshness: `{session_guard.get('freshness_status') or '-'}` age minutes `{session_guard.get('source_quote_age_minutes') if session_guard.get('source_quote_age_minutes') is not None else '-'}` max `{session_guard.get('max_snapshot_age_minutes')}`",
        f"- Quotes / symbols: `{report.get('quote_count')}` / `{report.get('symbol_count')}`",
        f"- Gamma regimes: `{summary.get('gamma_regime_counts') or {}}`",
        f"- Pin-risk symbols: `{summary.get('pin_risk_symbol_count') or 0}` proximity-alert symbols `{summary.get('proximity_alert_symbol_count') or 0}`",
        f"- Confidence counts: `{summary.get('confidence_counts') or {}}`",
        f"- Expiry buckets: `{summary.get('expiry_bucket_counts') or {}}`",
        f"- Missing-OI symbols: `{summary.get('missing_open_interest_symbols') or []}`",
        "",
        "| Symbol | Spot | Gamma regime | Total GEX | Flip | Call wall | Put wall | Max pain | Pin dist % | Alerts | Confidence | Score | Warnings |",
        "| --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- | ---: | --- |",
    ]
    for item in report.get("symbols") or []:
        lines.append(
            f"| `{item.get('symbol')}` | {item.get('spot') if item.get('spot') is not None else '-'} | `{item.get('gamma_regime')}` | "
            f"{item.get('total_gamma_exposure')} | {item.get('gamma_flip') if item.get('gamma_flip') is not None else '-'} | "
            f"{item.get('call_wall') if item.get('call_wall') is not None else '-'} | {item.get('put_wall') if item.get('put_wall') is not None else '-'} | "
            f"{item.get('max_pain') if item.get('max_pain') is not None else '-'} | {item.get('distance_to_pin_pct') if item.get('distance_to_pin_pct') is not None else '-'} | "
            f"`{item.get('proximity_alerts') or []}` | `{item.get('confidence')}` | {item.get('confidence_score') if item.get('confidence_score') is not None else '-'} | `{item.get('warnings') or []}` |"
        )
    if not report.get("symbols"):
        lines.append("| - | - | - | - | - | - | - | - | - | - | - | - | - |")
    lines.extend(["", "## Expiry buckets", "", "| Symbol | Bucket | Quotes | DTE | Gamma regime | Total GEX | Call wall | Put wall | Max pain | Pin | Alerts |", "| --- | --- | ---: | --- | --- | ---: | ---: | ---: | ---: | --- | --- |"])
    bucket_rows = 0
    for item in report.get("symbols") or []:
        for bucket in item.get("expiry_buckets") or []:
            bucket_rows += 1
            dte_range = "-"
            if bucket.get("min_dte") is not None or bucket.get("max_dte") is not None:
                dte_range = f"{bucket.get('min_dte')}-{bucket.get('max_dte')}"
            lines.append(
                f"| `{item.get('symbol')}` | `{bucket.get('bucket')}` | {bucket.get('quote_count')} | {dte_range} | `{bucket.get('gamma_regime')}` | "
                f"{bucket.get('total_gamma_exposure')} | {bucket.get('call_wall') if bucket.get('call_wall') is not None else '-'} | "
                f"{bucket.get('put_wall') if bucket.get('put_wall') is not None else '-'} | {bucket.get('max_pain') if bucket.get('max_pain') is not None else '-'} | "
                f"{bucket.get('pin_risk')} | `{bucket.get('proximity_alerts') or []}` |"
            )
    if bucket_rows == 0:
        lines.append("| - | - | - | - | - | - | - | - | - | - | - |")
    lines.extend(
        [
            "",
            "## Safety",
            "",
            "- Read-only options market-structure analysis.",
            "- No option orders, broker order submission, transmit, live routing, scorer mutation, or portfolio mutation.",
            "- If OI is missing, volume proxy is lower-confidence flow evidence rather than confirmed dealer positioning.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_options_market_structure_json(output_path: Path, report: dict[str, Any]) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return output_path


def write_options_market_structure_markdown(output_path: Path, report: dict[str, Any]) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(options_market_structure_markdown(report), encoding="utf-8")
    return output_path


def build_options_market_structure_from_files(
    *,
    quote_json_path: Path,
    market_data_json_path: Path | None = None,
    fallback_to_volume: bool = True,
    pin_near_pct: float = 0.5,
    proximity_near_pct: float = 1.0,
    max_snapshot_age_minutes: float = 30.0,
) -> dict[str, Any]:
    return build_options_market_structure_report(
        option_quote_snapshot=_read_json(quote_json_path),
        market_data=_read_json(market_data_json_path),
        fallback_to_volume=fallback_to_volume,
        pin_near_pct=pin_near_pct,
        proximity_near_pct=proximity_near_pct,
        max_snapshot_age_minutes=max_snapshot_age_minutes,
    )
