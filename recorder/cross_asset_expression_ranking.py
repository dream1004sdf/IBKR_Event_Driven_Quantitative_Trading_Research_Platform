from __future__ import annotations

import json
import math
import sqlite3
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


CROSS_ASSET_EXPRESSION_RANKING_VERSION = "m241_7-cross-asset-expression-ranking-v1"

_BULLISH_WORDS = {
    "beat", "beats", "raise", "raises", "raised", "upgrade", "upgraded",
    "surge", "surges", "rally", "rallies", "growth", "strong", "approval",
    "approved", "optimism", "cooling", "eases", "deal", "demand",
}
_BEARISH_WORDS = {
    "miss", "misses", "cut", "cuts", "downgrade", "downgraded", "warning",
    "weak", "drop", "drops", "falls", "plunge", "plunges", "war", "attack",
    "attacks", "missile", "sanction", "sanctions", "tariff", "tariffs",
    "inflation", "hot", "rates", "yields", "hawkish", "risk", "crisis",
}

_LIQUIDITY_PRIOR = {
    "SPY": 20.0,
    "QQQ": 20.0,
    "IWM": 18.0,
    "DIA": 16.0,
    "SMH": 15.0,
    "SOXX": 15.0,
    "NVDA": 18.0,
    "AVGO": 15.0,
    "MU": 14.0,
    "AAPL": 17.0,
    "MSFT": 17.0,
    "TSLA": 16.0,
    "GLD": 14.0,
    "TLT": 14.0,
    "UUP": 11.0,
    "VIX": 8.0,
    "VXX": 8.0,
    "USO": 13.0,
    "XLE": 14.0,
    "XOM": 14.0,
    "CVX": 14.0,
    "ITA": 10.0,
    "XAR": 9.0,
}

_OPTIONABLE = {
    "SPY", "QQQ", "IWM", "DIA", "SMH", "SOXX", "NVDA", "AVGO", "MU",
    "AAPL", "MSFT", "TSLA", "GLD", "TLT", "UUP", "VXX", "USO", "XLE",
    "XOM", "CVX", "ITA", "XAR",
}

_EXPRESSION_LIBRARY: tuple[dict[str, Any], ...] = (
    # Broad market down / risk-off expression set.
    {"scenario": "broad_market_down", "symbol": "SPY", "expression": "long_put", "action": "SELL_SHORT", "direction": "bearish", "asset_class": "index_etf", "base": 92.0, "requires_option": True, "reason": "most liquid broad-market put expression"},
    {"scenario": "broad_market_down", "symbol": "QQQ", "expression": "long_put", "action": "SELL_SHORT", "direction": "bearish", "asset_class": "growth_index_etf", "base": 90.0, "requires_option": True, "reason": "growth/tech beta usually expresses risk-off efficiently"},
    {"scenario": "broad_market_down", "symbol": "IWM", "expression": "long_put", "action": "SELL_SHORT", "direction": "bearish", "asset_class": "small_cap_index_etf", "base": 82.0, "requires_option": True, "reason": "small caps often carry higher domestic/rate sensitivity"},
    {"scenario": "broad_market_down", "symbol": "DIA", "expression": "put_spread_hedge_watch", "action": "SELL_SHORT", "direction": "bearish", "asset_class": "dow_index_etf", "base": 70.0, "requires_option": True, "reason": "lower-beta hedge expression"},
    {"scenario": "broad_market_down", "symbol": "GLD", "expression": "stock_long", "action": "BUY", "direction": "bullish", "asset_class": "safe_haven_proxy", "base": 65.0, "requires_option": False, "reason": "risk-off safe-haven proxy"},
    {"scenario": "broad_market_down", "symbol": "TLT", "expression": "stock_long", "action": "BUY", "direction": "bullish", "asset_class": "rates_proxy_etf", "base": 58.0, "requires_option": False, "reason": "risk-off/rates proxy ETF; not direct Treasury trading"},
    {"scenario": "broad_market_down", "symbol": "VIX", "expression": "volatility_watch", "action": "WATCH", "direction": "risk_reduction", "asset_class": "volatility_index_watch", "base": 62.0, "requires_option": False, "reason": "volatility confirmation/watch; not a direct stock order"},
    {"scenario": "broad_market_down", "symbol": "VXX", "expression": "volatility_proxy_watch", "action": "WATCH", "direction": "risk_reduction", "asset_class": "volatility_etf_high_risk", "base": 45.0, "requires_option": False, "reason": "high-risk volatility ETF proxy; review-only by default"},
    # Tariff / policy risk.
    {"scenario": "tariff_policy_risk", "symbol": "QQQ", "expression": "long_put", "action": "SELL_SHORT", "direction": "bearish", "asset_class": "growth_index_etf", "base": 92.0, "requires_option": True, "reason": "tariff/geopolitical tech beta expression"},
    {"scenario": "tariff_policy_risk", "symbol": "SMH", "expression": "long_put", "action": "SELL_SHORT", "direction": "bearish", "asset_class": "semiconductor_etf", "base": 84.0, "requires_option": True, "reason": "semiconductor supply-chain tariff sensitivity"},
    {"scenario": "tariff_policy_risk", "symbol": "SOXX", "expression": "long_put", "action": "SELL_SHORT", "direction": "bearish", "asset_class": "semiconductor_etf", "base": 80.0, "requires_option": True, "reason": "semiconductor ETF alternative"},
    {"scenario": "tariff_policy_risk", "symbol": "IWM", "expression": "long_put", "action": "SELL_SHORT", "direction": "bearish", "asset_class": "small_cap_index_etf", "base": 74.0, "requires_option": True, "reason": "domestic margin/rate sensitivity hedge"},
    {"scenario": "tariff_policy_risk", "symbol": "UUP", "expression": "stock_long", "action": "BUY", "direction": "bullish", "asset_class": "dollar_proxy_etf", "base": 55.0, "requires_option": False, "reason": "dollar proxy when trade-policy shock lifts dollar demand"},
    {"scenario": "tariff_policy_risk", "symbol": "GLD", "expression": "stock_long", "action": "BUY", "direction": "bullish", "asset_class": "safe_haven_proxy", "base": 58.0, "requires_option": False, "reason": "safe-haven proxy"},
    # AI / semiconductor upside.
    {"scenario": "ai_semiconductor_upside", "symbol": "NVDA", "expression": "long_call", "action": "BUY", "direction": "bullish", "asset_class": "single_stock_ai_leader", "base": 90.0, "requires_option": True, "reason": "direct AI leader expression"},
    {"scenario": "ai_semiconductor_upside", "symbol": "SMH", "expression": "stock_long", "action": "BUY", "direction": "bullish", "asset_class": "semiconductor_etf", "base": 84.0, "requires_option": False, "reason": "diversified semiconductor expression"},
    {"scenario": "ai_semiconductor_upside", "symbol": "SOXX", "expression": "stock_long", "action": "BUY", "direction": "bullish", "asset_class": "semiconductor_etf", "base": 80.0, "requires_option": False, "reason": "semiconductor ETF alternative"},
    {"scenario": "ai_semiconductor_upside", "symbol": "QQQ", "expression": "stock_long", "action": "BUY", "direction": "bullish", "asset_class": "growth_index_etf", "base": 72.0, "requires_option": False, "reason": "broad Nasdaq expression when single-name risk is high"},
    {"scenario": "ai_semiconductor_upside", "symbol": "AVGO", "expression": "long_call", "action": "BUY", "direction": "bullish", "asset_class": "single_stock_ai_infrastructure", "base": 76.0, "requires_option": True, "reason": "AI infrastructure single-name expression"},
    {"scenario": "ai_semiconductor_upside", "symbol": "MU", "expression": "long_call", "action": "BUY", "direction": "bullish", "asset_class": "single_stock_memory", "base": 68.0, "requires_option": True, "reason": "memory-cycle AI expression"},
    # Energy/geopolitical supply shock.
    {"scenario": "energy_supply_shock", "symbol": "USO", "expression": "stock_long", "action": "BUY", "direction": "bullish", "asset_class": "oil_proxy_etf", "base": 82.0, "requires_option": False, "reason": "oil price proxy for supply shock"},
    {"scenario": "energy_supply_shock", "symbol": "XLE", "expression": "stock_long", "action": "BUY", "direction": "bullish", "asset_class": "energy_sector_etf", "base": 76.0, "requires_option": False, "reason": "energy equity proxy"},
    {"scenario": "energy_supply_shock", "symbol": "XOM", "expression": "stock_long", "action": "BUY", "direction": "bullish", "asset_class": "energy_major", "base": 70.0, "requires_option": False, "reason": "large-cap energy major proxy"},
    {"scenario": "energy_supply_shock", "symbol": "CVX", "expression": "stock_long", "action": "BUY", "direction": "bullish", "asset_class": "energy_major", "base": 68.0, "requires_option": False, "reason": "large-cap energy major proxy"},
    {"scenario": "energy_supply_shock", "symbol": "GLD", "expression": "stock_long", "action": "BUY", "direction": "bullish", "asset_class": "safe_haven_proxy", "base": 62.0, "requires_option": False, "reason": "geopolitical safe-haven overlay"},
    # Broad market upside.
    {"scenario": "broad_market_up", "symbol": "SPY", "expression": "stock_long", "action": "BUY", "direction": "bullish", "asset_class": "index_etf", "base": 86.0, "requires_option": False, "reason": "broad-market long expression"},
    {"scenario": "broad_market_up", "symbol": "QQQ", "expression": "stock_long", "action": "BUY", "direction": "bullish", "asset_class": "growth_index_etf", "base": 84.0, "requires_option": False, "reason": "growth beta expression"},
    {"scenario": "broad_market_up", "symbol": "IWM", "expression": "stock_long", "action": "BUY", "direction": "bullish", "asset_class": "small_cap_index_etf", "base": 72.0, "requires_option": False, "reason": "risk-on small-cap expression"},
)


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _result(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    result = payload.get("result")
    return result if isinstance(result, dict) else payload


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, dict):
        for key in ("numeric_value", "value", "amount", "score"):
            parsed = _as_float(value.get(key))
            if parsed is not None:
                return parsed
        return None
    try:
        parsed = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _words(text: str) -> set[str]:
    clean = "".join(ch.lower() if ch.isalnum() else " " for ch in text)
    return {part for part in clean.split() if part}


def _market_rows(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    data = _result(payload)
    rows = data.get("rows") if isinstance(data.get("rows"), list) else data.get("instruments")
    return {str(row.get("key") or row.get("symbol") or "").upper(): row for row in (rows or []) if isinstance(row, dict)}


def _option_rows(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    data = _result(payload)
    rows = data.get("symbols") if isinstance(data.get("symbols"), list) else []
    return {str(row.get("symbol") or "").upper(): row for row in rows if isinstance(row, dict) and row.get("symbol")}


def _budget(capital_payload: dict[str, Any], key: str) -> float:
    capital = _result(capital_payload)
    budgets = capital.get("budgets") if isinstance(capital.get("budgets"), dict) else {}
    return max(0.0, _as_float(budgets.get(key)) or 0.0)


def _read_recent_scorer_predictions(database_path: Path | None, *, lookback_hours: float, limit: int, now_dt: datetime) -> list[dict[str, Any]]:
    if database_path is None:
        return []
    path = database_path.expanduser().resolve()
    if not path.exists():
        return []
    since = now_dt - timedelta(hours=max(0.25, float(lookback_hours)))
    rows: list[dict[str, Any]] = []
    try:
        con = sqlite3.connect(path)
        con.row_factory = sqlite3.Row
        for row in con.execute(
            """
            SELECT id, source_table, source_id, event_at, provider, symbol, headline,
                   event_family, predicted_direction, expected_return, confidence,
                   packet_quality_level, asset_mapping_confidence
              FROM m223_live_learning_predictions
             WHERE event_at >= ?
             ORDER BY event_at DESC
             LIMIT ?
            """,
            (since.isoformat(), max(1, int(limit))),
        ):
            rows.append(dict(row))
        con.close()
    except sqlite3.Error:
        return []
    return rows


def _ranking_lookup(ticker_candidate_ranking: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = ticker_candidate_ranking.get("ranking") if isinstance(ticker_candidate_ranking.get("ranking"), list) else []
    return {str(row.get("symbol") or "").upper(): row for row in rows if isinstance(row, dict) and row.get("symbol")}


def _score_adjustment_from_tuning(
    payload: dict[str, Any] | None,
    *,
    scenario: str,
    symbol: str,
    expression: str,
) -> tuple[float, list[str]]:
    data = _result(payload)
    adjustment_map = data.get("adjustment_map") if isinstance(data.get("adjustment_map"), dict) else {}
    reasons: list[str] = []

    def value_for(key: str) -> float:
        raw = adjustment_map.get(key)
        if isinstance(raw, dict):
            if raw.get("status") != "active":
                return 0.0
            return _as_float(raw.get("score_adjustment")) or 0.0
        return _as_float(raw) or 0.0

    exact_key = f"{scenario}|{symbol}|{expression}"
    scenario_key = f"scenario::{scenario}"
    exact = value_for(exact_key)
    scenario_adj = value_for(scenario_key) * 0.35
    total = max(-15.0, min(15.0, exact + scenario_adj))
    if exact:
        reasons.append(f"m241_8_exact_tuning:{exact_key}:{exact:+.2f}")
    if scenario_adj:
        reasons.append(f"m241_8_scenario_tuning:{scenario_key}:{scenario_adj:+.2f}")
    return round(total, 4), reasons


def _scenario_scores(ticker_candidate_ranking: dict[str, Any], scorer_predictions: list[dict[str, Any]]) -> tuple[dict[str, float], dict[str, list[str]]]:
    scores: Counter[str] = Counter()
    reasons: dict[str, list[str]] = {}

    def add(scenario: str, value: float, reason: str) -> None:
        if value <= 0:
            return
        scores[scenario] += value
        reasons.setdefault(scenario, [])
        if reason not in reasons[scenario] and len(reasons[scenario]) < 12:
            reasons[scenario].append(reason)

    for row in ticker_candidate_ranking.get("ranking") or []:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").upper()
        rank = int(_as_float(row.get("rank")) or 99)
        rank_weight = max(0.2, 1.0 - max(0, rank - 1) * 0.045)
        themes = row.get("themes") if isinstance(row.get("themes"), dict) else {}
        text = " ".join(
            [
                " ".join(str(key) for key in themes.keys()),
                " ".join(str(item.get("headline") or "") for item in (row.get("recent_events") or []) if isinstance(item, dict)),
                symbol,
            ]
        )
        tokens = _words(text)
        bullish_hits = tokens & _BULLISH_WORDS
        bearish_hits = tokens & _BEARISH_WORDS
        if bearish_hits:
            add("broad_market_down", 16.0 * rank_weight + 2.0 * len(bearish_hits), f"{symbol}:bearish_terms:{','.join(sorted(bearish_hits)[:4])}")
        if bullish_hits:
            add("broad_market_up", 12.0 * rank_weight + 1.5 * len(bullish_hits), f"{symbol}:bullish_terms:{','.join(sorted(bullish_hits)[:4])}")
        if {"tariff", "tariffs", "china", "taiwan", "sanction", "sanctions"} & tokens:
            add("tariff_policy_risk", 20.0 * rank_weight, f"{symbol}:trade_policy_or_china_taiwan_terms")
        if {"ai", "semiconductor", "semiconductors", "chip", "chips", "nvidia", "tokens"} & tokens or symbol in {"NVDA", "SMH", "SOXX", "AVGO", "MU", "TSM"}:
            add("ai_semiconductor_upside", 18.0 * rank_weight, f"{symbol}:ai_semiconductor_theme")
        if {"oil", "gas", "energy", "hormuz", "iran", "middle", "east"} & tokens or symbol in {"USO", "XLE", "XOM", "CVX"}:
            add("energy_supply_shock", 17.0 * rank_weight, f"{symbol}:energy_or_geopolitical_supply_theme")
        if {"rates", "yields", "inflation", "hawkish"} & tokens:
            add("broad_market_down", 10.0 * rank_weight, f"{symbol}:rates_macro_risk")

    for row in scorer_predictions:
        symbol = str(row.get("symbol") or "SPY").upper()
        direction = str(row.get("predicted_direction") or "neutral").lower()
        confidence = max(0.0, min(1.0, _as_float(row.get("confidence")) or 0.0))
        event_family = str(row.get("event_family") or "")
        text = f"{row.get('headline') or ''} {event_family} {symbol}"
        tokens = _words(text)
        weight = max(0.25, confidence)
        if direction == "down":
            add("broad_market_down", 14.0 * weight, f"scorer_down:{symbol}:{event_family or 'event'}")
        elif direction == "up":
            add("broad_market_up", 12.0 * weight, f"scorer_up:{symbol}:{event_family or 'event'}")
        if direction == "down" and ({"tariff", "tariffs", "china", "taiwan", "sanction", "sanctions"} & tokens):
            add("tariff_policy_risk", 16.0 * weight, f"scorer_trade_policy_down:{symbol}")
        if direction == "up" and ({"ai", "semiconductor", "chip", "chips", "nvidia", "tokens"} & tokens or symbol in {"NVDA", "SMH", "SOXX", "AVGO", "MU"}):
            add("ai_semiconductor_upside", 14.0 * weight, f"scorer_ai_up:{symbol}")
        if {"oil", "gas", "energy", "hormuz", "iran"} & tokens:
            add("energy_supply_shock", 12.0 * weight, f"scorer_energy_or_geopolitical:{symbol}")

    return {key: round(value, 4) for key, value in scores.items()}, reasons


def _option_context(symbol: str, option_rows: dict[str, dict[str, Any]], supervisor: dict[str, Any]) -> tuple[float, dict[str, Any], list[str]]:
    row = option_rows.get(symbol) or {}
    reasons: list[str] = []
    score = 0.0
    if symbol in _OPTIONABLE:
        score += 5.0
        reasons.append("optionable_prior")
    confidence = str(row.get("confidence") or "").lower()
    if confidence == "high":
        score += 8.0
        reasons.append("option_structure_high")
    elif confidence == "medium":
        score += 5.0
        reasons.append("option_structure_medium")
    elif row:
        score += 1.0
        reasons.append("option_structure_low")
    if supervisor.get("final_quote_usable") is True or supervisor.get("final_option_quote_allowed") is True:
        score += 8.0
        reasons.append("option_quotes_usable")
    elif supervisor:
        reasons.append("option_quotes_not_usable_or_deferred")
    if row.get("pin_risk"):
        score -= 3.0
        reasons.append("pin_risk_caution")
    return score, row, reasons


def _market_context(symbol: str, market_rows: dict[str, dict[str, Any]]) -> tuple[float, dict[str, Any], list[str]]:
    row = market_rows.get(symbol) or {}
    if not row:
        return 0.0, {}, ["no_direct_market_data_row"]
    reasons = ["market_data_row_available"]
    score = 4.0
    if row.get("delayed") or str(row.get("market_data_type") or "").lower() == "delayed":
        score -= 2.0
        reasons.append("market_data_delayed")
    if row.get("status") in {"live", "green"}:
        score += 2.0
        reasons.append("market_data_live_or_green")
    return score, row, reasons


def build_cross_asset_expression_ranking_report(
    *,
    ticker_candidate_ranking_payload: dict[str, Any],
    database_path: Path | None = None,
    market_data_payload: dict[str, Any] | None = None,
    options_market_structure_payload: dict[str, Any] | None = None,
    options_market_structure_supervisor_payload: dict[str, Any] | None = None,
    capital_state_payload: dict[str, Any] | None = None,
    cross_asset_tuning_payload: dict[str, Any] | None = None,
    lookback_hours: float = 24.0,
    max_scorer_predictions: int = 250,
    max_expressions: int = 30,
    min_expression_score: float = 35.0,
    now: str | None = None,
) -> dict[str, Any]:
    now_dt = datetime.fromisoformat(now.replace("Z", "+00:00")) if now else datetime.now(UTC)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=UTC)
    ranking_payload = _result(ticker_candidate_ranking_payload)
    market_rows = _market_rows(market_data_payload or {})
    option_rows = _option_rows(options_market_structure_payload or {})
    supervisor = _result(options_market_structure_supervisor_payload or {})
    capital = _result(capital_state_payload or {})
    options_budget = _budget(capital, "options_premium_budget")
    single_notional_budget = _budget(capital, "max_single_position_notional")
    ranking_by_symbol = _ranking_lookup(ranking_payload)
    scorer_predictions = _read_recent_scorer_predictions(
        database_path,
        lookback_hours=lookback_hours,
        limit=max_scorer_predictions,
        now_dt=now_dt,
    )
    scenario_scores, scenario_reasons = _scenario_scores(ranking_payload, scorer_predictions)

    rows: list[dict[str, Any]] = []
    for spec in _EXPRESSION_LIBRARY:
        scenario = str(spec["scenario"])
        strength = float(scenario_scores.get(scenario) or 0.0)
        if strength <= 0.0:
            continue
        symbol = str(spec["symbol"]).upper()
        ranking = ranking_by_symbol.get(symbol) or {}
        ranking_rank = int(_as_float(ranking.get("rank")) or 0)
        ranking_score = _as_float(ranking.get("total_score")) or 0.0
        ranking_bonus = min(24.0, ranking_score / 12.0) if ranking else 0.0
        if ranking_rank and ranking_rank <= 10:
            ranking_bonus += 5.0
        market_score, market_row, market_reasons = _market_context(symbol, market_rows)
        option_score, option_row, option_reasons = _option_context(symbol, option_rows, supervisor)
        liquidity_score = _LIQUIDITY_PRIOR.get(symbol, 5.0)
        tuning_adjustment, tuning_reasons = _score_adjustment_from_tuning(
            cross_asset_tuning_payload,
            scenario=scenario,
            symbol=symbol,
            expression=str(spec.get("expression") or ""),
        )
        capital_score = 0.0
        capital_reasons: list[str] = []
        if spec.get("requires_option"):
            if options_budget >= 100.0:
                capital_score += 7.0
                capital_reasons.append("option_budget_available")
            elif options_budget > 0:
                capital_score += 2.0
                capital_reasons.append("small_option_budget_available")
            else:
                capital_score -= 12.0
                capital_reasons.append("no_option_budget")
            if supervisor.get("final_quote_usable") is False:
                capital_reasons.append("option_expression_deferred_until_quotes_usable")
        else:
            if single_notional_budget >= 10.0 or _budget(capital, "deployable_cash_after_reserve") >= 10.0:
                capital_score += 5.0
                capital_reasons.append("stock_or_etf_proxy_budget_available")
        score = (
            float(spec.get("base") or 0.0) * min(1.5, strength / 30.0)
            + ranking_bonus
            + market_score
            + option_score
            + liquidity_score
            + capital_score
            + tuning_adjustment
        )
        cautions: list[str] = []
        if spec.get("requires_option") and supervisor.get("final_quote_usable") is False:
            cautions.append("option_quotes_deferred_or_not_usable")
        if not market_row:
            cautions.append("no_direct_market_data_row")
        if symbol == "VXX":
            cautions.append("high_decay_volatility_etf_proxy")
        if str(spec.get("action")) == "WATCH":
            cautions.append("watch_only_not_injected_by_default")
        if score < float(min_expression_score):
            continue
        rows.append(
            {
                "scenario": scenario,
                "scenario_strength": round(strength, 4),
                "symbol": symbol,
                "expression": spec.get("expression"),
                "action": spec.get("action"),
                "direction": spec.get("direction"),
                "asset_class": spec.get("asset_class"),
                "expression_score": round(score, 4),
                "score_components": {
                    "base_scaled": round(float(spec.get("base") or 0.0) * min(1.5, strength / 30.0), 4),
                    "ranking_bonus": round(ranking_bonus, 4),
                    "market_score": round(market_score, 4),
                    "option_score": round(option_score, 4),
                    "liquidity_score": round(liquidity_score, 4),
                    "capital_score": round(capital_score, 4),
                    "m241_8_tuning_adjustment": round(tuning_adjustment, 4),
                },
                "requires_option": bool(spec.get("requires_option")),
                "ranking": {
                    "available": bool(ranking),
                    "rank": ranking.get("rank"),
                    "total_score": ranking.get("total_score"),
                    "confidence": ranking.get("confidence"),
                },
                "option_context": {
                    "available": bool(option_row),
                    "quote_usable": supervisor.get("final_quote_usable"),
                    "quote_fresh": supervisor.get("final_quote_fresh"),
                    "structure_confidence": option_row.get("confidence"),
                    "gamma_regime": option_row.get("gamma_regime"),
                    "pin_risk": option_row.get("pin_risk"),
                },
                "market_context": {
                    "available": bool(market_row),
                    "status": market_row.get("status"),
                    "market_data_type": market_row.get("market_data_type"),
                    "price": market_row.get("price") or market_row.get("last") or market_row.get("close"),
                },
                "reasons": list(dict.fromkeys([
                    str(spec.get("reason")),
                    *(scenario_reasons.get(scenario) or [])[:5],
                    *market_reasons,
                    *option_reasons,
                    *capital_reasons,
                    *tuning_reasons,
                ]))[:12],
                "cautions": cautions,
            }
        )

    rows.sort(
        key=lambda item: (
            float(item.get("expression_score") or 0.0),
            not bool(item.get("requires_option")),
            _LIQUIDITY_PRIOR.get(str(item.get("symbol") or ""), 0.0),
        ),
        reverse=True,
    )
    rows = rows[: max(0, int(max_expressions))]
    for idx, row in enumerate(rows, start=1):
        row["rank"] = idx
    seeds = [
        {
            "symbol": row.get("symbol"),
            "action": row.get("action"),
            "direction": row.get("direction"),
            "expression_hint": row.get("expression"),
            "scenario": row.get("scenario"),
            "cross_asset_rank": row.get("rank"),
            "cross_asset_score": row.get("expression_score"),
            "requires_option": row.get("requires_option"),
            "reasons": row.get("reasons") or [],
            "cautions": row.get("cautions") or [],
        }
        for row in rows
    ]
    scorer_direction_counts = Counter(str(row.get("predicted_direction") or "neutral") for row in scorer_predictions)
    return {
        "cross_asset_expression_ranking_version": CROSS_ASSET_EXPRESSION_RANKING_VERSION,
        "milestone": "M241.7",
        "generated_at": _iso_now(),
        "input_window": {
            "lookback_hours": float(lookback_hours),
            "scorer_prediction_count": len(scorer_predictions),
            "scorer_direction_counts": dict(scorer_direction_counts),
            "source_ranked_candidate_count": len(ranking_payload.get("ranking") or []),
        },
        "summary": {
            "scenario_scores": scenario_scores,
            "top_scenarios": [key for key, _value in sorted(scenario_scores.items(), key=lambda pair: pair[1], reverse=True)[:5]],
            "expression_count": len(rows),
            "top_expressions": [f"{row.get('symbol')}:{row.get('expression')}" for row in rows[:10]],
            "option_deferred_count": sum(1 for row in rows if row.get("requires_option") and "option_quotes_deferred_or_not_usable" in set(row.get("cautions") or [])),
        },
        "expression_rankings": rows,
        "m241_6_candidate_seeds": seeds,
        "policy": {
            "role": "rank cross-asset expression choices after scorer/ticker direction but before M241.6 candidate synthesis",
            "examples": ["SPY put", "QQQ put", "IWM put", "GLD/TLT proxy", "sector ETF long/put", "VIX watch"],
            "does_not_submit_orders": True,
            "does_not_enable_transmit": True,
            "does_not_create_m242_order_intents": True,
            "m241_6_consumption": "M241.6 may use m241_6_candidate_seeds before falling back to raw M241.5 ticker direction.",
        },
        "safety": {
            "read_only": True,
            "paper_recommendation_only": True,
            "ibkr_order_submitted": False,
            "transmit_enabled": False,
            "live_routing_enabled": False,
            "real_portfolio_mutated": False,
        },
    }


def cross_asset_expression_ranking_markdown(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    lines = [
        "# M241.7 Cross-Asset Expression Ranking",
        "",
        f"- Version: `{report.get('cross_asset_expression_ranking_version')}`",
        f"- Generated: `{report.get('generated_at')}`",
        f"- Scenarios: `{summary.get('scenario_scores') or {}}`",
        f"- Top expressions: `{summary.get('top_expressions') or []}`",
        f"- Option deferred rows: `{summary.get('option_deferred_count')}`",
        "- Safety: read-only expression ranking; no broker order submission, no transmit, no live routing.",
        "",
        "| Rank | Scenario | Symbol | Expression | Action | Direction | Score | Reasons | Cautions |",
        "| ---: | --- | --- | --- | --- | --- | ---: | --- | --- |",
    ]
    for row in report.get("expression_rankings") or []:
        reasons = ", ".join(row.get("reasons") or [])[:160].replace("|", "/")
        cautions = ", ".join(row.get("cautions") or [])[:120].replace("|", "/")
        lines.append(
            f"| {row.get('rank')} | `{row.get('scenario')}` | `{row.get('symbol')}` | `{row.get('expression')}` | "
            f"`{row.get('action')}` | `{row.get('direction')}` | {row.get('expression_score')} | {reasons or '-'} | {cautions or '-'} |"
        )
    if not report.get("expression_rankings"):
        lines.append("| - | - | - | - | - | - | - | No cross-asset expression candidates. | - |")
    return "\n".join(lines) + "\n"


def write_cross_asset_expression_ranking_json(path: Path, report: dict[str, Any]) -> Path:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return resolved


def write_cross_asset_expression_ranking_markdown(path: Path, report: dict[str, Any]) -> Path:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(cross_asset_expression_ranking_markdown(report), encoding="utf-8")
    return resolved
