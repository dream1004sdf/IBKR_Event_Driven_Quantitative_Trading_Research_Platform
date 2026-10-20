from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .economic_indicator_desk import build_economic_indicator_desk, compact_economic_indicator_desk
from .evidence_operations import evidence_gap_guard_from_packet
from .macro_universe import FRED_SCORER_CONTEXT_SERIES
from .market_overlay import overlay_asset_family, overlay_for_score
from .standing_regimes import active_standing_regimes_for_event


EVIDENCE_PACKET_VERSION = "tri-desk-scorer-evidence-packet-v5-adaptive-single-stock-options"

DEFAULT_MARKET_REGIME_SYMBOLS = (
    "SPY",
    "QQQ",
    "IWM",
    "TLT",
    "GLD",
    "VIX",
    "UUP",
    "USO",
    "XLE",
    "SMH",
    "SOXX",
)

DEFAULT_MACRO_SERIES = FRED_SCORER_CONTEXT_SERIES


def _parse_dt(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _finite_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed != parsed or parsed in {float("inf"), float("-inf")}:
        return None
    return parsed


def _pct(value: Any) -> str:
    if value is None:
        return "-"
    return f"{float(value):.2%}"


def _signed_pct(value: Any) -> str:
    if value is None:
        return "-"
    return f"{float(value):+.2%}"


def _latest_macro_observations(
    db: Any,
    *,
    cutoff_date: str,
    series_ids: tuple[str, ...] = DEFAULT_MACRO_SERIES,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with db.connect() as conn:
        for series_id in series_ids:
            row = conn.execute(
                """
                SELECT series_id,observation_date,value,realtime_start,realtime_end,ingestion_version
                FROM fred_observations
                WHERE series_id=? AND observation_date<=?
                ORDER BY observation_date DESC,id DESC
                LIMIT 1
                """,
                (series_id, cutoff_date),
            ).fetchone()
            if row is None:
                continue
            rows.append(
                {
                    "series_id": str(row["series_id"]),
                    "observation_date": str(row["observation_date"]),
                    "value": _finite_float(row["value"]),
                    "realtime_start": str(row["realtime_start"] or ""),
                    "realtime_end": str(row["realtime_end"] or ""),
                    "ingestion_version": str(row["ingestion_version"] or ""),
                }
            )
    return rows


def _latest_technical_context(
    db: Any,
    *,
    symbols: tuple[str, ...],
    cutoff_date: str,
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    cleaned = tuple(dict.fromkeys(symbol.strip().upper() for symbol in symbols if symbol.strip()))
    with db.connect() as conn:
        for symbol in cleaned:
            row = conn.execute(
                """
                SELECT symbol,as_of_date,price_source,close,return_1d,return_5d,return_20d,
                       sma_20,sma_50,macd,macd_signal,rsi_14,volatility_20,ingestion_version
                FROM technical_indicators
                WHERE symbol=? AND as_of_date<=?
                ORDER BY as_of_date DESC,
                         CASE price_source
                           WHEN 'ibkr' THEN 1
                           WHEN 'yahoo' THEN 2
                           WHEN 'stooq' THEN 3
                           ELSE 4
                         END,
                         id DESC
                LIMIT 1
                """,
                (symbol, cutoff_date),
            ).fetchone()
            if row is None:
                continue
            output.append(
                {
                    "symbol": str(row["symbol"]),
                    "as_of_date": str(row["as_of_date"]),
                    "price_source": str(row["price_source"]),
                    "close": _finite_float(row["close"]),
                    "return_1d": _finite_float(row["return_1d"]),
                    "return_5d": _finite_float(row["return_5d"]),
                    "return_20d": _finite_float(row["return_20d"]),
                    "sma_20": _finite_float(row["sma_20"]),
                    "sma_50": _finite_float(row["sma_50"]),
                    "macd": _finite_float(row["macd"]),
                    "macd_signal": _finite_float(row["macd_signal"]),
                    "rsi_14": _finite_float(row["rsi_14"]),
                    "volatility_20": _finite_float(row["volatility_20"]),
                    "ingestion_version": str(row["ingestion_version"] or ""),
                }
            )
    return output


def _by_symbol(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(row.get("symbol") or "").upper(): row for row in rows if row.get("symbol")}


def _by_series(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(row.get("series_id") or "").upper(): row for row in rows if row.get("series_id")}


def summarize_market_regime(
    technical_context: list[dict[str, Any]],
    macro_context: list[dict[str, Any]],
) -> dict[str, Any]:
    tech = _by_symbol(technical_context)
    macro = _by_series(macro_context)
    risk_off_flags: list[str] = []
    risk_on_flags: list[str] = []
    inflation_pressure_flags: list[str] = []
    dollar_pressure_flags: list[str] = []

    for symbol in ("SPY", "QQQ", "IWM", "SOXX", "SMH"):
        row = tech.get(symbol)
        if not row:
            continue
        r1 = _finite_float(row.get("return_1d"))
        r5 = _finite_float(row.get("return_5d"))
        if r1 is not None and r1 <= -0.012:
            risk_off_flags.append(f"{symbol} 1D weak {_signed_pct(r1)}")
        if r5 is not None and r5 <= -0.025:
            risk_off_flags.append(f"{symbol} 5D weak {_signed_pct(r5)}")
        if r1 is not None and r1 >= 0.012:
            risk_on_flags.append(f"{symbol} 1D strong {_signed_pct(r1)}")
        if r5 is not None and r5 >= 0.025:
            risk_on_flags.append(f"{symbol} 5D strong {_signed_pct(r5)}")

    vix = tech.get("VIX")
    if vix:
        vix_1d = _finite_float(vix.get("return_1d"))
        vix_5d = _finite_float(vix.get("return_5d"))
        if vix_1d is not None and vix_1d >= 0.06:
            risk_off_flags.append(f"VIX 1D spike {_signed_pct(vix_1d)}")
        if vix_5d is not None and vix_5d >= 0.12:
            risk_off_flags.append(f"VIX 5D spike {_signed_pct(vix_5d)}")
        if vix_1d is not None and vix_1d <= -0.06:
            risk_on_flags.append(f"VIX 1D easing {_signed_pct(vix_1d)}")

    for symbol in ("UUP", "GLD", "USO", "XLE"):
        row = tech.get(symbol)
        if not row:
            continue
        r5 = _finite_float(row.get("return_5d"))
        if r5 is None:
            continue
        if symbol == "UUP" and r5 >= 0.01:
            dollar_pressure_flags.append(f"UUP 5D stronger {_signed_pct(r5)}")
        if symbol in {"USO", "XLE"} and r5 >= 0.03:
            inflation_pressure_flags.append(f"{symbol} 5D energy up {_signed_pct(r5)}")
        if symbol == "GLD" and r5 >= 0.025:
            risk_off_flags.append(f"GLD 5D safe-haven bid {_signed_pct(r5)}")

    dgs10 = _finite_float((macro.get("DGS10") or {}).get("value"))
    dgs2 = _finite_float((macro.get("DGS2") or {}).get("value"))
    if dgs10 is not None and dgs10 >= 4.5:
        inflation_pressure_flags.append(f"DGS10 elevated {dgs10:.2f}")
    if dgs2 is not None and dgs10 is not None and dgs2 > dgs10:
        inflation_pressure_flags.append(f"2Y/10Y inverted {dgs2:.2f}/{dgs10:.2f}")

    if len(risk_off_flags) >= 2:
        regime = "risk_off"
    elif len(risk_on_flags) >= 2 and not risk_off_flags:
        regime = "risk_on"
    elif risk_off_flags or risk_on_flags or inflation_pressure_flags or dollar_pressure_flags:
        regime = "mixed"
    else:
        regime = "insufficient"

    return {
        "regime": regime,
        "risk_off_flags": risk_off_flags[:8],
        "risk_on_flags": risk_on_flags[:8],
        "inflation_pressure_flags": inflation_pressure_flags[:8],
        "dollar_pressure_flags": dollar_pressure_flags[:8],
        "macro_observation_count": len(macro_context),
        "technical_indicator_count": len(technical_context),
    }


def _packet_strength(
    *,
    overlay: dict[str, Any],
    macro_count: int,
    technical_count: int,
    standing_regime_overlay: dict[str, Any] | None = None,
    option_structure_context: dict[str, Any] | None = None,
) -> str:
    score = 0
    if overlay.get("overlay_state") != "normal":
        score += 2
    if int(overlay.get("matched_signal_count") or 0) >= 2:
        score += 1
    if standing_regime_overlay and int(standing_regime_overlay.get("matched_regime_count") or 0) > 0:
        score += 1
    if option_structure_context and option_structure_context.get("available"):
        score += 1
        if option_structure_context.get("scorer_action") in {
            "allow_larger_reaction_if_news_and_market_move_confirm",
            "haircut_overextended_directional_reaction",
        }:
            score += 1
    if macro_count >= 4:
        score += 1
    if technical_count >= 5:
        score += 1
    if score >= 4:
        return "rich"
    if score >= 2:
        return "partial"
    return "thin"


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _predicted_bucket(row: dict[str, Any]) -> str:
    predicted = str(row.get("predicted_direction") or "").strip().lower()
    if predicted in {"up", "bullish", "positive"}:
        return "up"
    if predicted in {"down", "bearish", "negative"}:
        return "down"
    if predicted in {"flat", "neutral", "unchanged", "sideways"}:
        return "neutral"
    return "unknown"


def _vote_relation(*, predicted: str, bias: str) -> str:
    if predicted == "up":
        if bias == "risk_on":
            return "confirm"
        if bias == "risk_off":
            return "conflict"
    elif predicted == "down":
        if bias == "risk_off":
            return "confirm"
        if bias == "risk_on":
            return "conflict"
    elif predicted == "neutral":
        if bias in {"neutral", "mixed"}:
            return "confirm"
        if bias in {"risk_on", "risk_off"}:
            return "conflict"
    return "neutral"


def _overlay_vote(overlay: dict[str, Any]) -> dict[str, Any]:
    action = str(overlay.get("overlay_action") or "none")
    state = str(overlay.get("overlay_state") or "normal")
    score = float(overlay.get("overlay_score") or 0.0)
    matched = int(overlay.get("matched_signal_count") or 0)
    top_signals = overlay.get("top_signals") or []
    evidence = []
    if state != "normal":
        evidence.append(f"state={state}")
    if action != "none":
        evidence.append(f"action={action}")
    if matched:
        evidence.append(f"matched_signals={matched}")
    for item in top_signals[:2]:
        title = str(item.get("title") or "").strip()
        if title:
            evidence.append(title[:140])

    if action == "risk_reduce" or state in {"risk_off", "asset_specific_shock"}:
        bias = "risk_off"
        directional_score = -2.0
    elif action == "caution" or state == "caution":
        bias = "risk_off"
        directional_score = -0.9
    elif state == "normal":
        bias = "neutral"
        directional_score = 0.0
    else:
        bias = "mixed"
        directional_score = 0.0

    confidence = 0.0
    if bias != "neutral":
        confidence = _clamp((score / 6.0) + (0.08 if matched >= 2 else 0.0), 0.25, 1.0)
    return {
        "desk": "geopolitical_overlay",
        "bias": bias,
        "score": round(directional_score, 3),
        "confidence": round(confidence, 3),
        "evidence": evidence[:5],
    }


def _economic_indicator_vote(economic_indicator_desk: dict[str, Any]) -> dict[str, Any]:
    risk_bias = str(economic_indicator_desk.get("risk_bias") or "unknown")
    covered = int(economic_indicator_desk.get("covered_series_count") or 0)
    total = int(economic_indicator_desk.get("series_count") or 0)
    coverage = (covered / total) if total else 0.0
    flags = [str(item.get("flag") or "") for item in (economic_indicator_desk.get("macro_flags") or []) if item.get("flag")]
    takeaways = [str(item) for item in (economic_indicator_desk.get("scorer_takeaways") or []) if item]
    evidence = [f"risk_bias={risk_bias}", f"coverage={covered}/{total}"]
    evidence.extend(flags[:3])
    evidence.extend(takeaways[:2])

    if risk_bias == "macro_risk_off":
        bias = "risk_off"
        directional_score = -1.7
    elif risk_bias == "macro_mixed":
        bias = "mixed"
        directional_score = -0.4
    elif risk_bias == "macro_neutral":
        bias = "neutral"
        directional_score = 0.0
    else:
        bias = "unknown"
        directional_score = 0.0

    confidence = 0.0 if bias in {"neutral", "unknown"} else _clamp(0.35 + coverage * 0.55 + min(len(flags), 4) * 0.04)
    return {
        "desk": "economic_indicator_desk",
        "bias": bias,
        "score": round(directional_score, 3),
        "confidence": round(confidence, 3),
        "evidence": evidence[:6],
    }


def _market_data_vote(market_regime: dict[str, Any]) -> dict[str, Any]:
    regime = str(market_regime.get("regime") or "insufficient")
    risk_off_flags = [str(item) for item in (market_regime.get("risk_off_flags") or [])]
    risk_on_flags = [str(item) for item in (market_regime.get("risk_on_flags") or [])]
    inflation_flags = [str(item) for item in (market_regime.get("inflation_pressure_flags") or [])]
    dollar_flags = [str(item) for item in (market_regime.get("dollar_pressure_flags") or [])]
    flag_count = len(risk_off_flags) + len(risk_on_flags) + len(inflation_flags) + len(dollar_flags)
    evidence = [f"regime={regime}"]
    evidence.extend(risk_off_flags[:3])
    evidence.extend(risk_on_flags[:3])
    evidence.extend(inflation_flags[:2])
    evidence.extend(dollar_flags[:2])

    if regime == "risk_off":
        bias = "risk_off"
        directional_score = -1.6
    elif regime == "risk_on":
        bias = "risk_on"
        directional_score = 1.6
    elif regime == "mixed":
        bias = "mixed"
        directional_score = 0.0
    else:
        bias = "unknown"
        directional_score = 0.0

    confidence = 0.0 if bias == "unknown" else _clamp(0.35 + min(flag_count, 6) * 0.08)
    return {
        "desk": "market_data_desk",
        "bias": bias,
        "score": round(directional_score, 3),
        "confidence": round(confidence, 3),
        "evidence": evidence[:6],
    }


def _standing_regime_vote(standing_regime_overlay: dict[str, Any] | None) -> dict[str, Any] | None:
    if not standing_regime_overlay:
        return None
    bias = str(standing_regime_overlay.get("bias") or "neutral")
    regimes = standing_regime_overlay.get("active_regimes") or []
    matched = int(standing_regime_overlay.get("matched_regime_count") or 0)
    risk_weight = float(standing_regime_overlay.get("risk_weight") or 0.0)
    favorable_weight = float(standing_regime_overlay.get("favorable_weight") or 0.0)
    evidence = [
        f"standing_bias={bias}",
        f"risk_weight={risk_weight:.2f}",
        f"favorable_weight={favorable_weight:.2f}",
        f"matched_regimes={matched}",
    ]
    for item in regimes[:3]:
        evidence.append(
            f"{item.get('family')}:{item.get('status')}:{item.get('directional_bias')} "
            f"effective={float(item.get('effective_weight') or 0.0):.2f} "
            f"freshness={item.get('freshness_status') or '-'} relation={item.get('relation_to_prediction') or '-'}"
        )

    if bias == "risk_off":
        directional_score = -1.3
    elif bias == "risk_on":
        directional_score = 1.3
    elif bias == "mixed":
        directional_score = 0.0
    else:
        directional_score = 0.0
    confidence = 0.0
    if bias in {"risk_off", "risk_on", "mixed"} and matched:
        confidence = _clamp(0.30 + min(0.35, matched * 0.06) + min(0.35, max(risk_weight, favorable_weight) / 12.0))
        if int(standing_regime_overlay.get("conflict_count") or 0) > 0:
            confidence = _clamp(confidence + 0.05)
        if int(standing_regime_overlay.get("aging_regime_count") or 0) > int(standing_regime_overlay.get("fresh_regime_count") or 0):
            confidence = _clamp(confidence * 0.85)
    return {
        "desk": "standing_regime_overlay",
        "bias": bias,
        "score": round(directional_score, 3),
        "confidence": round(confidence, 3),
        "evidence": evidence[:7],
    }


def _option_structure_context_from_row(row: dict[str, Any], *, symbol: str) -> dict[str, Any]:
    raw = (
        row.get("option_structure_context")
        or row.get("options_market_structure_context")
        or row.get("m220_option_structure_context")
        or row.get("m220_market_move_option_structure_overlay")
        or row.get("scorer_option_structure_context")
    )
    if not isinstance(raw, dict):
        return {
            "version": "scorer-option-structure-context-v1",
            "available": False,
            "symbol": symbol,
            "source": "not_provided",
            "freshness_status": "missing",
            "scorer_bias": "none",
            "scorer_action": "ignore_missing_option_structure",
            "confidence": 0.0,
            "rows": [],
            "guidance": ["No point-in-time option-structure context was attached to this scorer packet."],
            "anti_leakage": {
                "contains_actual_outcome": False,
                "contains_future_market_reaction": False,
            },
        }
    rows = raw.get("rows") if isinstance(raw.get("rows"), list) else []
    symbol_upper = str(symbol or "").upper()
    matched_rows = [
        item for item in rows
        if isinstance(item, dict)
        and (
            str(item.get("market_key") or "").upper() == symbol_upper
            or str(item.get("matched_option_symbol") or "").upper() == symbol_upper
            or symbol_upper in {str(asset).upper() for asset in item.get("related_symbols") or []}
        )
    ]
    if not matched_rows and rows:
        matched_rows = [item for item in rows if isinstance(item, dict)][:3]
    row0 = matched_rows[0] if matched_rows else {}
    gamma_regime = str(row0.get("gamma_regime") or raw.get("gamma_regime") or "")
    explanation_status = str(row0.get("explanation_status") or raw.get("overall_status") or "")
    freshness = str(raw.get("structure_freshness_status") or raw.get("freshness_status") or row0.get("freshness_status") or "unknown")
    coverage_status = str(row0.get("coverage_status") or raw.get("coverage_status") or "")
    selected_source_type = str(row0.get("selected_source_type") or raw.get("selected_source_type") or "")
    proximity_alerts = row0.get("proximity_alerts") or raw.get("proximity_alerts") or []
    confidence_text = str(row0.get("confidence") or raw.get("confidence") or "")
    proxy_or_probe = selected_source_type in {"proxy", "proxy_probe"} or coverage_status in {"proxy_only", "proxy_probe_only"}
    stale = freshness not in {"fresh", "green", "ok", "live", "unknown", ""}

    scorer_bias = "none"
    scorer_action = "use_as_context_only"
    confidence = 0.0
    guidance: list[str] = []
    if stale:
        scorer_bias = "stale_ignore_for_live_causality"
        scorer_action = "downweight_option_structure"
        confidence = 0.1
        guidance.append("Option-structure context is stale or not fresh; do not use it as live causality.")
    elif gamma_regime == "negative_gamma_trend_amplification_bias" or "trend_amplification" in explanation_status:
        scorer_bias = "trend_amplification"
        scorer_action = "allow_larger_reaction_if_news_and_market_move_confirm"
        confidence = 0.55
        guidance.append("Negative gamma/trend-amplification context can make news reactions overshoot; avoid fading a clean directional catalyst too early.")
    elif gamma_regime == "positive_gamma_mean_reversion_bias" or "mean_reversion" in explanation_status or "pin" in explanation_status:
        scorer_bias = "mean_reversion_or_pin"
        scorer_action = "haircut_overextended_directional_reaction"
        confidence = 0.50
        guidance.append("Positive gamma/wall/max-pain context can cap or mean-revert a headline move; haircut overextended directional calls near walls or pin levels.")
    elif gamma_regime or explanation_status:
        scorer_bias = "partial_context"
        scorer_action = "use_as_low_confidence_context"
        confidence = 0.25
        guidance.append("Option-structure context is present but not directional enough to override market/news evidence.")
    else:
        guidance.append("Option-structure context was attached but did not include a usable gamma/wall interpretation.")
    if proximity_alerts:
        guidance.append("Spot is near option wall/flip/max-pain proximity levels; check for pinning, rejection, or breakout behavior.")
        confidence = max(confidence, 0.45)
    if proxy_or_probe:
        guidance.append("Option-structure source is proxy/probe-backed; treat confidence as lower than direct index option evidence.")
        confidence = min(confidence, 0.40)
    if confidence_text == "low":
        confidence = min(confidence, 0.35)
    return {
        "version": "scorer-option-structure-context-v1",
        "available": bool(raw),
        "symbol": symbol,
        "source": str(raw.get("version") or raw.get("source") or "attached_context"),
        "overall_status": raw.get("overall_status"),
        "freshness_status": freshness,
        "structure_symbol_count": raw.get("structure_symbol_count"),
        "mapped_move_count": raw.get("mapped_move_count"),
        "status_counts": raw.get("status_counts") or {},
        "matched_row_count": len(matched_rows),
        "selected_source_type": selected_source_type,
        "coverage_status": coverage_status,
        "matched_option_symbol": row0.get("matched_option_symbol"),
        "gamma_regime": gamma_regime,
        "call_wall": row0.get("call_wall") or raw.get("call_wall"),
        "put_wall": row0.get("put_wall") or raw.get("put_wall"),
        "max_pain": row0.get("max_pain") or raw.get("max_pain"),
        "distance_to_pin_pct": row0.get("distance_to_pin_pct") or raw.get("distance_to_pin_pct"),
        "proximity_alerts": proximity_alerts[:8] if isinstance(proximity_alerts, list) else [],
        "explanation_status": explanation_status,
        "scorer_bias": scorer_bias,
        "scorer_action": scorer_action,
        "confidence": round(confidence, 3),
        "guidance": guidance[:6],
        "rows": [
            {
                "market_key": item.get("market_key"),
                "family_label": item.get("family_label"),
                "move_magnitude_pct": item.get("move_magnitude_pct"),
                "matched_option_symbol": item.get("matched_option_symbol"),
                "gamma_regime": item.get("gamma_regime"),
                "explanation_status": item.get("explanation_status"),
                "selected_source_type": item.get("selected_source_type"),
                "coverage_status": item.get("coverage_status"),
                "proximity_alerts": item.get("proximity_alerts") or [],
            }
            for item in matched_rows[:5]
            if isinstance(item, dict)
        ],
        "anti_leakage": {
            "policy": "Option-structure context must be generated at or before the scorer cutoff; do not attach realized outcome labels.",
            "contains_actual_outcome": False,
            "contains_future_market_reaction": False,
        },
    }


def _option_structure_vote(option_structure_context: dict[str, Any] | None) -> dict[str, Any] | None:
    if not option_structure_context or not option_structure_context.get("available"):
        return None
    action = str(option_structure_context.get("scorer_action") or "")
    bias_text = str(option_structure_context.get("scorer_bias") or "")
    guidance = [str(item) for item in option_structure_context.get("guidance") or []]
    confidence = float(option_structure_context.get("confidence") or 0.0)
    if bias_text == "trend_amplification":
        bias = "amplifies_prediction"
        score = 0.8
    elif bias_text == "mean_reversion_or_pin":
        bias = "dampens_prediction"
        score = -0.8
    elif bias_text == "stale_ignore_for_live_causality":
        bias = "unknown"
        score = 0.0
        confidence = 0.0
    else:
        bias = "mixed"
        score = 0.0
    return {
        "desk": "options_market_structure",
        "bias": bias,
        "score": round(score, 3),
        "confidence": round(confidence, 3),
        "scorer_action": action,
        "evidence": [
            f"gamma={option_structure_context.get('gamma_regime') or '-'}",
            f"status={option_structure_context.get('explanation_status') or '-'}",
            f"source={option_structure_context.get('selected_source_type') or '-'}",
            *guidance[:3],
        ][:7],
    }


def _single_stock_option_watchlist_context_from_row(row: dict[str, Any], *, symbol: str) -> dict[str, Any]:
    raw = (
        row.get("single_stock_option_watchlist_context")
        or row.get("adaptive_single_stock_option_watchlist")
        or row.get("m238_single_stock_option_watchlist")
    )
    if not isinstance(raw, dict):
        return {
            "version": "single-stock-option-watchlist-scorer-context-v1",
            "available": False,
            "symbol": symbol,
            "watchlist_membership": "not_attached",
            "scorer_action": "ignore_missing_single_stock_option_watchlist",
            "confidence": 0.0,
            "guidance": ["No adaptive single-stock option watchlist context was attached to this packet."],
            "anti_leakage": {
                "contains_actual_outcome": False,
                "contains_future_market_reaction": False,
            },
        }
    symbol_upper = str(symbol or "").upper()
    rows = raw.get("candidate_rows") if isinstance(raw.get("candidate_rows"), list) else []
    matched = [
        item for item in rows
        if isinstance(item, dict) and str(item.get("symbol") or "").upper() == symbol_upper
    ]
    selected_symbols = {str(item).upper() for item in raw.get("selected_symbols") or []}
    row0 = matched[0] if matched else {}
    membership = "selected" if symbol_upper in selected_symbols else "candidate" if matched else "not_selected"
    tier = str(row0.get("tier") or "")
    q = row0.get("quote_quality") if isinstance(row0.get("quote_quality"), dict) else {}
    structure = row0.get("structure_quality") if isinstance(row0.get("structure_quality"), dict) else {}
    state = row0.get("adaptive_state") if isinstance(row0.get("adaptive_state"), dict) else {}
    trade_permission = str(row0.get("trade_permission") or q.get("trade_permission") or "")
    promotion_status = str(row0.get("promotion_status") or state.get("promotion_status") or "")
    quote_score = _finite_float(q.get("quote_quality_score"))
    false_positive_rate = _finite_float(state.get("false_positive_rate"))

    guidance: list[str] = []
    confidence = 0.0
    scorer_action = "use_as_context_only"
    if membership == "selected":
        confidence = 0.35
        guidance.append("Symbol is selected by adaptive single-stock option watchlist; treat related news as operationally monitorable.")
    elif membership == "candidate":
        confidence = 0.20
        guidance.append("Symbol is a watchlist candidate but was not selected within current tier budgets; use as low-confidence context.")
    else:
        guidance.append("Symbol is not in the adaptive single-stock option watchlist; do not assume options structure confirms the news.")
    if trade_permission == "trade_candidate":
        confidence = max(confidence, 0.45)
        scorer_action = "preserve_if_news_and_option_context_confirm"
        guidance.append("Single-stock option quote quality is sufficient for trade-candidate context.")
    elif trade_permission == "analysis_only":
        confidence = min(max(confidence, 0.25), 0.35)
        scorer_action = "analysis_only_haircut_execution_confidence"
        guidance.append("Single-stock option context is analysis-only; keep scorer context but haircut execution confidence.")
    elif trade_permission == "quality_blocked":
        confidence = min(confidence, 0.15)
        scorer_action = "ignore_for_execution_use_news_only"
        guidance.append("Single-stock option quote quality is blocked; do not use it for execution confidence.")
    if false_positive_rate is not None and false_positive_rate >= 0.60:
        confidence = min(confidence, 0.20)
        scorer_action = "haircut_due_to_watchlist_false_positive_history"
        guidance.append("Historical watchlist false-positive rate is high; haircut this symbol's watchlist contribution.")
    if promotion_status == "promotion_candidate":
        confidence = max(confidence, 0.50)
        guidance.append("Watchlist learning marks this symbol as promotion candidate; prior inclusion/outcome evidence has been useful.")
    return {
        "version": "single-stock-option-watchlist-scorer-context-v1",
        "available": True,
        "symbol": symbol,
        "watchlist_version": raw.get("watchlist_version"),
        "generated_at": raw.get("generated_at"),
        "watchlist_membership": membership,
        "selected_symbols": list(selected_symbols)[:20],
        "tier": tier,
        "themes": row0.get("themes") or [],
        "score": row0.get("score"),
        "score_components": row0.get("score_components") or {},
        "ttl_minutes": row0.get("ttl_minutes"),
        "expires_at": row0.get("expires_at"),
        "quote_quality": q,
        "structure_quality": structure,
        "trade_permission": trade_permission,
        "promotion_status": promotion_status,
        "false_positive_rate": false_positive_rate,
        "quote_quality_score": quote_score,
        "scorer_action": scorer_action,
        "confidence": round(confidence, 3),
        "guidance": guidance[:6],
        "anti_leakage": {
            "policy": "Watchlist context should be generated at or before scorer cutoff. Outcome summaries must be historical aggregate state only, not this event future outcome.",
            "contains_actual_outcome": False,
            "contains_future_market_reaction": False,
        },
    }


def _single_stock_option_watchlist_vote(context: dict[str, Any] | None) -> dict[str, Any] | None:
    if not context or not context.get("available"):
        return None
    action = str(context.get("scorer_action") or "")
    membership = str(context.get("watchlist_membership") or "")
    permission = str(context.get("trade_permission") or "")
    confidence = float(context.get("confidence") or 0.0)
    if action in {"preserve_if_news_and_option_context_confirm"}:
        bias = "confirms_attention"
        score = 0.35
    elif action in {"analysis_only_haircut_execution_confidence", "haircut_due_to_watchlist_false_positive_history"}:
        bias = "dampens_prediction"
        score = -0.35
    elif action == "ignore_for_execution_use_news_only":
        bias = "unknown"
        score = 0.0
        confidence = 0.0
    else:
        bias = "mixed" if membership in {"selected", "candidate"} else "unknown"
        score = 0.0
    return {
        "desk": "adaptive_single_stock_options_watchlist",
        "bias": bias,
        "score": round(score, 3),
        "confidence": round(confidence, 3),
        "scorer_action": action,
        "evidence": [
            f"membership={membership}",
            f"tier={context.get('tier') or '-'}",
            f"permission={permission or '-'}",
            f"quote_quality={context.get('quote_quality_score')}",
            f"promotion={context.get('promotion_status') or '-'}",
            *[str(item) for item in (context.get("guidance") or [])[:2]],
        ][:7],
    }


def build_tri_desk_alignment(
    *,
    row: dict[str, Any],
    overlay: dict[str, Any],
    economic_indicator_desk: dict[str, Any],
    market_regime: dict[str, Any],
    standing_regime_overlay: dict[str, Any] | None = None,
    option_structure_context: dict[str, Any] | None = None,
    single_stock_option_watchlist_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    predicted = _predicted_bucket(row)
    votes = [
        _overlay_vote(overlay),
        _economic_indicator_vote(economic_indicator_desk),
        _market_data_vote(market_regime),
    ]
    standing_vote = _standing_regime_vote(standing_regime_overlay)
    if standing_vote is not None:
        votes.append(standing_vote)
    option_vote = _option_structure_vote(option_structure_context)
    if option_vote is not None:
        votes.append(option_vote)
    watchlist_vote = _single_stock_option_watchlist_vote(single_stock_option_watchlist_context)
    if watchlist_vote is not None:
        votes.append(watchlist_vote)
    for vote in votes:
        bias = str(vote.get("bias") or "unknown")
        if bias == "amplifies_prediction":
            relation = "confirm" if predicted in {"up", "down"} else "neutral"
        elif bias == "confirms_attention":
            relation = "confirm" if predicted in {"up", "down", "neutral"} else "neutral"
        elif bias == "dampens_prediction":
            relation = "conflict" if predicted in {"up", "down"} else "confirm" if predicted == "neutral" else "neutral"
        else:
            relation = _vote_relation(predicted=predicted, bias=bias)
        vote["relation_to_prediction"] = relation

    active_votes = [vote for vote in votes if str(vote.get("bias")) not in {"unknown"}]
    total_weight = sum(float(vote.get("confidence") or 0.0) for vote in active_votes)
    confirmation_weight = sum(
        float(vote.get("confidence") or 0.0) for vote in active_votes if vote.get("relation_to_prediction") == "confirm"
    )
    conflict_weight = sum(
        float(vote.get("confidence") or 0.0) for vote in active_votes if vote.get("relation_to_prediction") == "conflict"
    )
    neutral_weight = sum(
        float(vote.get("confidence") or 0.0) for vote in active_votes if vote.get("relation_to_prediction") == "neutral"
    )
    if total_weight > 0:
        confirmation_score = confirmation_weight / total_weight
        conflict_score = conflict_weight / total_weight
        neutral_score = neutral_weight / total_weight
        net_alignment = (confirmation_weight - conflict_weight) / total_weight
    else:
        confirmation_score = conflict_score = neutral_score = net_alignment = 0.0

    if predicted == "unknown" or not active_votes:
        alignment = "insufficient_context"
        action = "insufficient_context"
    elif conflict_score >= 0.67:
        alignment = "contradicted"
        action = "second_pass_required"
    elif conflict_score >= 0.40:
        alignment = "conflicted"
        action = "confidence_haircut"
    elif confirmation_score >= 0.67:
        alignment = "confirmed"
        action = "confirm_direction"
    elif confirmation_score >= 0.40 and conflict_score <= 0.25:
        alignment = "partially_confirmed"
        action = "moderate_confidence"
    else:
        alignment = "mixed"
        action = "mixed_context_review"

    strongest_conflict = sorted(
        [vote for vote in active_votes if vote.get("relation_to_prediction") == "conflict"],
        key=lambda vote: float(vote.get("confidence") or 0.0),
        reverse=True,
    )
    strongest_confirmation = sorted(
        [vote for vote in active_votes if vote.get("relation_to_prediction") == "confirm"],
        key=lambda vote: float(vote.get("confidence") or 0.0),
        reverse=True,
    )
    return {
        "alignment_version": (
            "tri-desk-alignment-v4-adaptive-single-stock-options"
            if watchlist_vote is not None
            else "tri-desk-alignment-v3-options-structure"
            if option_vote is not None
            else "tri-desk-alignment-v2-standing-regime"
            if standing_vote is not None
            else "tri-desk-alignment-v1"
        ),
        "target_prediction": predicted,
        "desk_votes": votes,
        "confirmation_score": round(confirmation_score, 3),
        "conflict_score": round(conflict_score, 3),
        "neutral_score": round(neutral_score, 3),
        "net_alignment_score": round(net_alignment, 3),
        "alignment": alignment,
        "scorer_action": action,
        "strongest_conflict_desk": "" if not strongest_conflict else str(strongest_conflict[0].get("desk") or ""),
        "strongest_confirmation_desk": "" if not strongest_confirmation else str(strongest_confirmation[0].get("desk") or ""),
        "vote_summary": "; ".join(
            f"{vote.get('desk')}={vote.get('bias')}:{vote.get('relation_to_prediction')}@{vote.get('confidence')}"
            for vote in votes
        ),
    }


def _scorer_guidance(
    *,
    row: dict[str, Any],
    overlay: dict[str, Any],
    market_regime: dict[str, Any],
    economic_indicator_desk: dict[str, Any] | None = None,
    tri_desk_alignment: dict[str, Any] | None = None,
    standing_regime_overlay: dict[str, Any] | None = None,
    option_structure_context: dict[str, Any] | None = None,
    single_stock_option_watchlist_context: dict[str, Any] | None = None,
) -> list[str]:
    guidance: list[str] = []
    overlay_action = str(overlay.get("overlay_action") or "none")
    overlay_state = str(overlay.get("overlay_state") or "normal")
    predicted = str(row.get("predicted_direction") or "").lower()
    regime = str(market_regime.get("regime") or "insufficient")

    if overlay_action == "risk_reduce":
        guidance.append(
            "High-severity external overlay is present; reduce confidence in isolated company-news direction unless company evidence is overwhelming."
        )
    elif overlay_action == "caution":
        guidance.append("External overlay is present but not decisive; treat it as a confidence haircut and require confirmation.")
    if overlay_state == "asset_specific_shock":
        guidance.append("Specific asset/sector overlap exists; compare the direct company signal against sector/geopolitical pressure before choosing direction.")
    if regime == "risk_off":
        guidance.append("Market regime is risk-off; up predictions need stronger idiosyncratic evidence and down/flat risk should be considered.")
    elif regime == "risk_on":
        guidance.append("Market regime is risk-on; negative company signals may be partially absorbed unless the event is severe.")
    elif regime == "mixed":
        guidance.append("Market regime is mixed; prefer moderate confidence and explicitly account for offsetting macro/market forces.")
    if predicted == "up" and (overlay_action == "risk_reduce" or regime == "risk_off"):
        guidance.append("Stored prediction is up while overlay/regime is defensive; this is a priority case for scorer re-check.")
    if predicted == "down" and regime == "risk_on" and overlay_action == "none":
        guidance.append("Stored prediction is down while broad regime is constructive; check whether the company-specific negative catalyst is strong enough.")
    standing_regime_overlay = standing_regime_overlay or {}
    standing_bias = str(standing_regime_overlay.get("bias") or "neutral")
    standing_count = int(standing_regime_overlay.get("matched_regime_count") or 0)
    if standing_count:
        conflict_count = int(standing_regime_overlay.get("conflict_count") or 0)
        confirmation_count = int(standing_regime_overlay.get("confirmation_count") or 0)
        aging_count = int(standing_regime_overlay.get("aging_regime_count") or 0)
        fresh_count = int(standing_regime_overlay.get("fresh_regime_count") or 0)
        if standing_bias == "risk_off":
            guidance.append(
                "Standing regime overlay is risk_off; treat recurring geopolitical/tariff/security pressure as background context until it eases or resolves."
            )
        elif standing_bias == "risk_on":
            guidance.append(
                "Standing regime overlay is risk_on; favorable long-running themes such as AI infrastructure demand can preserve affected up calls if event evidence also confirms."
            )
        elif standing_bias == "mixed":
            guidance.append(
                "Standing regime overlay is mixed; explicitly compare long-running favorable themes against unresolved risk regimes before final confidence."
            )
        if conflict_count:
            guidance.append(
                f"Standing regime conflicts with the stored direction in {conflict_count} matched regime(s); use this as a threshold modifier rather than a full direction override."
            )
        elif confirmation_count:
            guidance.append(
                f"Standing regime confirms the stored direction in {confirmation_count} matched regime(s); preserve confidence only if the current headline and market reaction also confirm."
            )
        if aging_count > fresh_count:
            guidance.append(
                "Matched standing regimes are aging more than freshly confirmed; downweight their causality unless new headlines refresh the regime."
            )
    option_structure_context = option_structure_context or {}
    if option_structure_context.get("available"):
        option_action = str(option_structure_context.get("scorer_action") or "")
        if option_action == "allow_larger_reaction_if_news_and_market_move_confirm":
            guidance.append(
                "Options Market Structure shows negative-gamma/trend-amplification context; if news direction and live market move agree, allow a larger reaction range."
            )
        elif option_action == "haircut_overextended_directional_reaction":
            guidance.append(
                "Options Market Structure shows positive-gamma/wall/max-pain context; haircut overextended directional calls and consider mean-reversion/pin risk."
            )
        elif option_action == "downweight_option_structure":
            guidance.append("Options Market Structure is stale or not fresh; do not use it as live causality.")
        for item in (option_structure_context.get("guidance") or [])[:2]:
            if item not in guidance:
                guidance.append(str(item))
    single_stock_option_watchlist_context = single_stock_option_watchlist_context or {}
    if single_stock_option_watchlist_context.get("available"):
        action = str(single_stock_option_watchlist_context.get("scorer_action") or "")
        if action == "preserve_if_news_and_option_context_confirm":
            guidance.append(
                "Adaptive single-stock options watchlist selected this symbol with usable quote quality; preserve attention if news and price action confirm."
            )
        elif action == "analysis_only_haircut_execution_confidence":
            guidance.append(
                "Adaptive single-stock options watchlist marks this as analysis-only; use for context but haircut execution confidence."
            )
        elif action == "haircut_due_to_watchlist_false_positive_history":
            guidance.append(
                "Adaptive single-stock options watchlist has high false-positive history for this symbol; haircut its contribution."
            )
        for item in (single_stock_option_watchlist_context.get("guidance") or [])[:2]:
            if item not in guidance:
                guidance.append(str(item))
    economic_indicator_desk = economic_indicator_desk or {}
    risk_bias = str(economic_indicator_desk.get("risk_bias") or "")
    if risk_bias == "macro_risk_off":
        guidance.append("Economic Indicator Desk is macro_risk_off; apply a rates/labor/inflation-aware confidence haircut to isolated up calls.")
    elif risk_bias == "macro_mixed":
        guidance.append("Economic Indicator Desk is macro_mixed; explicitly check whether macro evidence offsets the headline direction.")
    for takeaway in (economic_indicator_desk.get("scorer_takeaways") or [])[:2]:
        if takeaway not in guidance:
            guidance.append(str(takeaway))
    tri_desk_alignment = tri_desk_alignment or {}
    action = str(tri_desk_alignment.get("scorer_action") or "")
    conflict_score = float(tri_desk_alignment.get("conflict_score") or 0.0)
    confirmation_score = float(tri_desk_alignment.get("confirmation_score") or 0.0)
    if action == "second_pass_required":
        guidance.append(
            f"Tri-desk evidence contradicts the stored direction (conflict_score={conflict_score:.2f}); run a second-pass scorer before using this call."
        )
    elif action == "confidence_haircut":
        guidance.append(
            f"Tri-desk evidence is conflicted (conflict_score={conflict_score:.2f}); lower confidence unless the news catalyst is unusually strong."
        )
    elif action == "confirm_direction":
        guidance.append(
            f"Tri-desk evidence confirms the stored direction (confirmation_score={confirmation_score:.2f}); confidence can be preserved if the news catalyst is clean."
        )
    elif action == "mixed_context_review":
        guidance.append("Tri-desk evidence is mixed; explicitly weigh market data, macro indicators, and geopolitical overlay before final scoring.")
    if not guidance:
        guidance.append("No strong overlay or market-regime conflict detected from currently stored evidence.")
    return guidance


def _scorer_injection_text(packet: dict[str, Any]) -> str:
    overlay = packet["overlay"]
    regime = packet["market_regime"]
    tri_desk = packet.get("tri_desk_alignment") or {}
    standing = packet.get("standing_regime_overlay") or {}
    option_structure = packet.get("option_structure_context") or {}
    single_stock_watchlist = packet.get("single_stock_option_watchlist_context") or {}
    freshness = packet.get("evidence_gap_guard") or {}
    top_signal_titles = [
        str(item.get("title") or "")[:120]
        for item in overlay.get("top_signals", [])
        if item.get("title")
    ]
    top_regimes = [
        f"{item.get('family')}:{item.get('status')}:{item.get('directional_bias')} "
        f"effective={item.get('effective_weight')} freshness={item.get('freshness_status')} relation={item.get('relation_to_prediction')}"
        for item in (standing.get("active_regimes") or [])[:3]
    ]
    lines = [
        f"Evidence packet version: {packet['packet_version']}",
        f"Event cutoff: {packet['event']['event_time_utc']} / symbol {packet['event']['symbol']} / horizon {packet['event']['horizon']}",
        f"Overlay: state={overlay.get('overlay_state')} action={overlay.get('overlay_action')} score={overlay.get('overlay_score')} matched_signals={overlay.get('matched_signal_count')}",
        f"Standing regime overlay: bias={standing.get('bias')} matched_regimes={standing.get('matched_regime_count')} risk_weight={standing.get('risk_weight')} favorable_weight={standing.get('favorable_weight')} conflicts={standing.get('conflict_count')} confirmations={standing.get('confirmation_count')} regimes={'; '.join(top_regimes) or '-'}",
        f"Market regime: {regime.get('regime')} risk_off_flags={'; '.join(regime.get('risk_off_flags') or []) or '-'} risk_on_flags={'; '.join(regime.get('risk_on_flags') or []) or '-'}",
        f"Economic Indicator Desk: risk_bias={packet['economic_indicator_desk'].get('risk_bias')} flags={'; '.join(item.get('flag','') for item in packet['economic_indicator_desk'].get('macro_flags', [])[:5]) or '-'}",
        f"Options Market Structure: available={option_structure.get('available')} action={option_structure.get('scorer_action')} bias={option_structure.get('scorer_bias')} gamma={option_structure.get('gamma_regime') or '-'} source={option_structure.get('selected_source_type') or '-'} walls=call:{option_structure.get('call_wall') or '-'} put:{option_structure.get('put_wall') or '-'} max_pain:{option_structure.get('max_pain') or '-'} proximity={','.join(option_structure.get('proximity_alerts') or []) or '-'}",
        f"Adaptive Single-Stock Options Watchlist: available={single_stock_watchlist.get('available')} membership={single_stock_watchlist.get('watchlist_membership')} tier={single_stock_watchlist.get('tier') or '-'} score={single_stock_watchlist.get('score')} trade_permission={single_stock_watchlist.get('trade_permission') or '-'} quote_quality={single_stock_watchlist.get('quote_quality_score')} action={single_stock_watchlist.get('scorer_action')}",
        f"Tri-desk alignment: action={tri_desk.get('scorer_action')} alignment={tri_desk.get('alignment')} confirmation={tri_desk.get('confirmation_score')} conflict={tri_desk.get('conflict_score')} net={tri_desk.get('net_alignment_score')} votes={tri_desk.get('vote_summary')}",
        f"Evidence freshness guard: action={freshness.get('action')} gap_warning={freshness.get('evidence_gap_warning')} do_not_use_as_latest={','.join(freshness.get('do_not_use_as_latest') or []) or '-'}",
        f"Macro observations: {len(packet['macro_context'])}; technical indicators: {len(packet['technical_context'])}; packet_strength={packet['packet_strength']}",
        "Scorer guidance:",
    ]
    lines.extend(f"- {item}" for item in packet["scorer_guidance"])
    if top_signal_titles:
        lines.append("Top external signals:")
        lines.extend(f"- {title}" for title in top_signal_titles[:3])
    return "\n".join(lines)


def build_overlay_evidence_packet(
    row: dict[str, Any],
    signals: list[dict[str, Any]],
    db: Any,
    *,
    benchmark_symbols: tuple[str, ...] = DEFAULT_MARKET_REGIME_SYMBOLS,
) -> dict[str, Any]:
    event_time = _parse_dt(row.get("replay_timestamp") or row.get("prediction_cutoff_at"))
    if event_time is None:
        event_time = datetime.now(UTC)
    cutoff_date = event_time.date().isoformat()
    symbol = str(row.get("symbol") or row.get("actual_subject_symbol") or "").upper()
    related_assets = tuple(sorted(overlay_asset_family(symbol)))
    technical_symbols = tuple(dict.fromkeys((symbol, *related_assets, *benchmark_symbols)))
    overlay = overlay_for_score(row, signals)
    macro_context = _latest_macro_observations(db, cutoff_date=cutoff_date)
    economic_indicator_desk = compact_economic_indicator_desk(
        build_economic_indicator_desk(db, cutoff_date=cutoff_date),
        max_indicators=28,
    )
    technical_context = _latest_technical_context(db, symbols=technical_symbols, cutoff_date=cutoff_date)
    market_regime = summarize_market_regime(technical_context, macro_context)
    option_structure_context = _option_structure_context_from_row(row, symbol=symbol)
    single_stock_option_watchlist_context = _single_stock_option_watchlist_context_from_row(row, symbol=symbol)
    try:
        standing_regime_overlay = active_standing_regimes_for_event(
            db,
            symbol=symbol,
            cutoff_at=event_time,
            predicted_direction=str(row.get("predicted_direction") or ""),
        )
    except Exception as exc:  # pragma: no cover - defensive compatibility for old scratch DBs
        standing_regime_overlay = {
            "standing_regime_version": "unavailable",
            "symbol": symbol,
            "cutoff_at": event_time.isoformat(),
            "bias": "neutral",
            "risk_weight": 0.0,
            "favorable_weight": 0.0,
            "matched_regime_count": 0,
            "active_regimes": [],
            "error": str(exc)[:180],
        }
    tri_desk_alignment = build_tri_desk_alignment(
        row=row,
        overlay=overlay,
        economic_indicator_desk=economic_indicator_desk,
        market_regime=market_regime,
        standing_regime_overlay=standing_regime_overlay,
        option_structure_context=option_structure_context,
        single_stock_option_watchlist_context=single_stock_option_watchlist_context,
    )
    strength = _packet_strength(
        overlay=overlay,
        macro_count=len(macro_context),
        technical_count=len(technical_context),
        standing_regime_overlay=standing_regime_overlay,
        option_structure_context=option_structure_context,
    )
    packet = {
        "packet_version": EVIDENCE_PACKET_VERSION,
        "event": {
            "symbol": symbol,
            "event_type": str(row.get("event_type") or ""),
            "event_subtype": str(row.get("event_subtype") or ""),
            "horizon": str(row.get("horizon") or ""),
            "event_time_utc": event_time.isoformat(),
            "cutoff_date": cutoff_date,
            "session": str(row.get("session") or ""),
        },
        "anti_leakage": {
            "policy": "Only external signals with published_at <= event_time and structured rows with date <= cutoff_date are included.",
            "contains_actual_outcome": False,
            "contains_future_market_reaction": False,
        },
        "overlay": overlay,
        "macro_context": macro_context,
        "economic_indicator_desk": economic_indicator_desk,
        "technical_context": technical_context,
        "market_regime": market_regime,
        "standing_regime_overlay": standing_regime_overlay,
        "option_structure_context": option_structure_context,
        "single_stock_option_watchlist_context": single_stock_option_watchlist_context,
        "tri_desk_alignment": tri_desk_alignment,
        "packet_strength": strength,
        "scorer_guidance": [],
    }
    packet["evidence_gap_guard"] = evidence_gap_guard_from_packet(packet, now=event_time)
    packet["scorer_guidance"] = _scorer_guidance(
        row=row,
        overlay=overlay,
        market_regime=market_regime,
        economic_indicator_desk=economic_indicator_desk,
        tri_desk_alignment=tri_desk_alignment,
        standing_regime_overlay=standing_regime_overlay,
        option_structure_context=option_structure_context,
        single_stock_option_watchlist_context=single_stock_option_watchlist_context,
    )
    for warning in (packet.get("evidence_gap_guard") or {}).get("warnings") or []:
        packet["scorer_guidance"].append(str(warning))
    packet["scorer_injection_text"] = _scorer_injection_text(packet)
    return packet


def _signed_return(row: dict[str, Any], key: str) -> float | None:
    direction = str(row.get("predicted_direction") or "").lower()
    if direction not in {"up", "down"}:
        return None
    value = row.get(key)
    if value is None:
        return None
    realized = float(value)
    return realized if direction == "up" else -realized


def _group_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    evaluated = [row for row in rows if row.get("direction_hit") is not None]
    directional = [row for row in evaluated if str(row.get("predicted_direction") or "") in {"up", "down"}]
    directional_hits = sum(int(row["direction_hit"]) for row in directional)
    excess_returns = [value for value in (_signed_return(row, "actual_excess_return") for row in directional) if value is not None]
    return {
        "rows": len(rows),
        "directional_rows": len(directional),
        "directional_hits": directional_hits,
        "directional_accuracy": (directional_hits / len(directional)) if directional else None,
        "avg_signed_excess_return": (sum(excess_returns) / len(excess_returns)) if excess_returns else None,
    }


def _group_rows(rows: list[dict[str, Any]], key_fn: Any, *, min_rows: int = 1) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(str(key_fn(row) or "unknown"), []).append(row)
    output = []
    for bucket, members in groups.items():
        metrics = _group_metrics(members)
        if metrics["rows"] >= min_rows:
            output.append({"bucket": bucket, **metrics})
    output.sort(key=lambda item: (-int(item["rows"]), item["bucket"]))
    return output


def _group_rows_multi(rows: list[dict[str, Any]], key_fn: Any, *, min_rows: int = 1) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        keys = [str(item or "").strip() for item in (key_fn(row) or [])]
        keys = [item for item in dict.fromkeys(keys) if item]
        if not keys:
            keys = ["unknown"]
        for key in keys:
            groups.setdefault(key, []).append(row)
    output = []
    for bucket, members in groups.items():
        metrics = _group_metrics(members)
        if metrics["rows"] >= min_rows:
            output.append({"bucket": bucket, **metrics})
    output.sort(key=lambda item: (-int(item["rows"]), item["bucket"]))
    return output


def _standing_regime_families(row: dict[str, Any]) -> list[str]:
    overlay = row["_evidence_packet"].get("standing_regime_overlay") or {}
    regimes = overlay.get("active_regimes") or []
    return [str(item.get("family") or "") for item in regimes if item.get("family")]


def _tri_desk_conflict_bucket(row: dict[str, Any]) -> str:
    score = float(row["_evidence_packet"]["tri_desk_alignment"].get("conflict_score") or 0.0)
    if score >= 0.67:
        return "high_conflict"
    if score >= 0.40:
        return "medium_conflict"
    if score > 0.0:
        return "low_conflict"
    return "no_conflict"


def evidence_packet_summary(
    rows: list[dict[str, Any]],
    signals: list[dict[str, Any]],
    db: Any,
    *,
    min_group_rows: int = 1,
) -> dict[str, Any]:
    enriched: list[dict[str, Any]] = []
    guidance_counter: Counter[str] = Counter()
    for row in rows:
        packet = build_overlay_evidence_packet(row, signals, db)
        clone = dict(row)
        clone["_evidence_packet"] = packet
        enriched.append(clone)
        for item in packet["scorer_guidance"]:
            guidance_counter[item] += 1
    overlay_rows = [row for row in enriched if row["_evidence_packet"]["overlay"].get("overlay_state") != "normal"]
    risk_conflict_rows = [
        row
        for row in enriched
        if (
            str(row.get("predicted_direction") or "").lower() == "up"
            and (
                row["_evidence_packet"]["overlay"].get("overlay_action") == "risk_reduce"
                or row["_evidence_packet"]["market_regime"].get("regime") == "risk_off"
            )
        )
    ]
    tri_desk_conflict_rows = [
        row
        for row in enriched
        if float(row["_evidence_packet"]["tri_desk_alignment"].get("conflict_score") or 0.0) >= 0.40
    ]
    macro_covered = [row for row in enriched if row["_evidence_packet"]["macro_context"]]
    technical_covered = [row for row in enriched if row["_evidence_packet"]["technical_context"]]
    return {
        "rows": len(enriched),
        "signals": len(signals),
        "all_rows": _group_metrics(enriched),
        "overlay_rows": _group_metrics(overlay_rows),
        "risk_conflict_rows": _group_metrics(risk_conflict_rows),
        "tri_desk_conflict_rows": _group_metrics(tri_desk_conflict_rows),
        "macro_coverage": (len(macro_covered) / len(enriched)) if enriched else None,
        "technical_coverage": (len(technical_covered) / len(enriched)) if enriched else None,
        "by_packet_strength": _group_rows(enriched, lambda row: row["_evidence_packet"].get("packet_strength"), min_rows=min_group_rows),
        "by_market_regime": _group_rows(enriched, lambda row: row["_evidence_packet"]["market_regime"].get("regime"), min_rows=min_group_rows),
        "by_standing_regime_bias": _group_rows(
            enriched,
            lambda row: row["_evidence_packet"]["standing_regime_overlay"].get("bias"),
            min_rows=min_group_rows,
        ),
        "by_standing_regime_family": _group_rows_multi(enriched, _standing_regime_families, min_rows=min_group_rows),
        "by_economic_indicator_bias": _group_rows(
            enriched,
            lambda row: row["_evidence_packet"]["economic_indicator_desk"].get("risk_bias"),
            min_rows=min_group_rows,
        ),
        "by_overlay_action": _group_rows(enriched, lambda row: row["_evidence_packet"]["overlay"].get("overlay_action"), min_rows=min_group_rows),
        "by_tri_desk_action": _group_rows(
            enriched,
            lambda row: row["_evidence_packet"]["tri_desk_alignment"].get("scorer_action"),
            min_rows=min_group_rows,
        ),
        "by_tri_desk_alignment": _group_rows(
            enriched,
            lambda row: row["_evidence_packet"]["tri_desk_alignment"].get("alignment"),
            min_rows=min_group_rows,
        ),
        "by_tri_desk_conflict": _group_rows(enriched, _tri_desk_conflict_bucket, min_rows=min_group_rows),
        "top_guidance": guidance_counter.most_common(8),
        "top_packets": sorted(
            enriched,
            key=lambda row: (
                float(row["_evidence_packet"]["tri_desk_alignment"].get("conflict_score") or 0.0),
                float(row["_evidence_packet"]["overlay"].get("overlay_score") or 0.0),
            ),
            reverse=True,
        )[:10],
    }


def _append_group_table(lines: list[str], title: str, rows: list[dict[str, Any]]) -> None:
    lines.extend(
        [
            "",
            f"## {title}",
            "",
            "| Bucket | Rows | Directional rows | Directional hits | Directional acc | Avg signed excess |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    if not rows:
        lines.append("| - | 0 | 0 | 0 | - | - |")
        return
    for row in rows:
        lines.append(
            f"| {row['bucket']} | {row['rows']} | {row['directional_rows']} | {row['directional_hits']} "
            f"| {_pct(row['directional_accuracy'])} | {_signed_pct(row['avg_signed_excess_return'])} |"
        )


def evidence_packet_markdown(
    rows: list[dict[str, Any]],
    signals: list[dict[str, Any]],
    db: Any,
    *,
    filters: dict[str, Any] | None = None,
    min_group_rows: int = 1,
    title: str = "Replay Overlay + Macro Evidence Packet Report",
) -> str:
    summary = evidence_packet_summary(rows, signals, db, min_group_rows=min_group_rows)
    filters = filters or {}
    all_rows = summary["all_rows"]
    overlay_rows = summary["overlay_rows"]
    conflict_rows = summary["risk_conflict_rows"]
    tri_conflict_rows = summary["tri_desk_conflict_rows"]
    lines = [
        f"# {title}",
        "",
        f"- Generated at: `{datetime.now(UTC).isoformat()}`",
        f"- Packet version: `{EVIDENCE_PACKET_VERSION}`",
        f"- Rows: `{summary['rows']}`",
        f"- External signals available to packet builder: `{summary['signals']}`",
        f"- Macro coverage: `{_pct(summary['macro_coverage'])}`",
        f"- Technical coverage: `{_pct(summary['technical_coverage'])}`",
        f"- All directional accuracy: `{all_rows['directional_hits']}/{all_rows['directional_rows']} = {_pct(all_rows['directional_accuracy'])}`",
        f"- Overlay-row directional accuracy: `{overlay_rows['directional_hits']}/{overlay_rows['directional_rows']} = {_pct(overlay_rows['directional_accuracy'])}`",
        f"- Up-prediction risk-conflict accuracy: `{conflict_rows['directional_hits']}/{conflict_rows['directional_rows']} = {_pct(conflict_rows['directional_accuracy'])}`",
        f"- Tri-desk conflicted accuracy: `{tri_conflict_rows['directional_hits']}/{tri_conflict_rows['directional_rows']} = {_pct(tri_conflict_rows['directional_accuracy'])}`",
        "- Anti-leakage: packet content excludes actual outcomes and only uses external signals/structured rows available at or before the replay cutoff.",
        "- Safety: read-only evidence packet/report; no OpenAI call, IBKR account query, portfolio access, or order action.",
        "",
        "## Filters",
        "",
    ]
    if filters:
        for key, value in filters.items():
            lines.append(f"- `{key}`: `{value if value is not None else '-'}`")
    else:
        lines.append("- none")

    _append_group_table(lines, "By packet strength", summary["by_packet_strength"])
    _append_group_table(lines, "By market regime", summary["by_market_regime"])
    _append_group_table(lines, "By standing regime bias", summary["by_standing_regime_bias"])
    _append_group_table(lines, "By standing regime family", summary["by_standing_regime_family"])
    _append_group_table(lines, "By economic indicator bias", summary["by_economic_indicator_bias"])
    _append_group_table(lines, "By overlay action", summary["by_overlay_action"])
    _append_group_table(lines, "By tri-desk scorer action", summary["by_tri_desk_action"])
    _append_group_table(lines, "By tri-desk alignment", summary["by_tri_desk_alignment"])
    _append_group_table(lines, "By tri-desk conflict bucket", summary["by_tri_desk_conflict"])

    lines.extend(["", "## Top scorer guidance", "", "| Count | Guidance |", "| ---: | --- |"])
    if not summary["top_guidance"]:
        lines.append("| 0 | - |")
    for guidance, count in summary["top_guidance"]:
        lines.append(f"| {count} | {guidance.replace('|', '/')} |")

    lines.extend(
        [
            "",
            "## Top evidence packets",
            "",
            "| Symbol | Horizon | Replay timestamp | Pred | Actual | Hit | Strength | Regime | Overlay | Tri-desk action | Conflict | Net | Key guidance |",
            "| --- | --- | --- | --- | --- | ---: | --- | --- | --- | --- | ---: | ---: | --- |",
        ]
    )
    for row in summary["top_packets"]:
        packet = row["_evidence_packet"]
        overlay = packet["overlay"]
        tri_desk = packet["tri_desk_alignment"]
        first_guidance = str((packet["scorer_guidance"] or [""])[0]).replace("|", "/")[:180]
        lines.append(
            f"| {row.get('symbol') or ''} | {row.get('horizon') or ''} | {row.get('replay_timestamp') or ''} "
            f"| {row.get('predicted_direction') or ''} | {row.get('actual_direction') or ''} "
            f"| {int(row.get('direction_hit') or 0)} | {packet.get('packet_strength')} "
            f"| {packet['market_regime'].get('regime')} | {overlay.get('overlay_action')} "
            f"| {tri_desk.get('scorer_action')} | {float(tri_desk.get('conflict_score') or 0.0):.2f} "
            f"| {float(tri_desk.get('net_alignment_score') or 0.0):+.2f} | {first_guidance} |"
        )

    lines.extend(
        [
            "",
            "## Example scorer injection text",
            "",
        ]
    )
    if summary["top_packets"]:
        example_packet = summary["top_packets"][0]["_evidence_packet"]
        lines.extend(["```text", example_packet["scorer_injection_text"][:3000], "```"])
    else:
        lines.append("- No packets available.")
    lines.extend(
        [
            "",
            "## Interpretation note",
            "",
            "This report is intended to make scorer mistakes explainable before changing scorer behavior. The packet is safe to feed into a future challenger scorer because it excludes actual outcomes and future-dated evidence.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_evidence_packet_markdown(
    output_path: Path,
    rows: list[dict[str, Any]],
    signals: list[dict[str, Any]],
    db: Any,
    *,
    filters: dict[str, Any] | None = None,
    min_group_rows: int = 1,
) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        evidence_packet_markdown(rows, signals, db, filters=filters, min_group_rows=min_group_rows),
        encoding="utf-8",
    )
    return output_path
