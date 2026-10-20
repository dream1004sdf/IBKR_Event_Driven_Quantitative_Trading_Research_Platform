from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


SINGLE_STOCK_OPTION_WATCHLIST_VERSION = "single-stock-option-watchlist-v3-institutional-adaptive"

DEFAULT_EXCLUDED_SYMBOLS = {
    "SPY",
    "QQQ",
    "IWM",
    "DIA",
    "SPX",
    "XSP",
    "NDX",
    "XND",
    "RUT",
    "VIX",
    "VXX",
    "VIXY",
    "UVXY",
    "ES",
    "NQ",
    "YM",
    "RTY",
    "TLT",
    "UUP",
    "GLD",
    "USO",
    "XLE",
    "ITA",
    "SMH",
    "SOXX",
    "EWY",
    "BTC",
    "ETH",
}

DEFAULT_BASE_WATCHLIST = [
    "NVDA",
    "AAPL",
    "MSFT",
    "TSLA",
    "AMD",
    "META",
    "GOOGL",
    "AMZN",
    "AVGO",
    "MU",
]

TICKER_RE = re.compile(r"\b[A-Z]{1,5}\b")

DEFAULT_THEME_BASKETS: dict[str, list[str]] = {
    "ai_compute": ["NVDA", "AMD", "AVGO", "MU", "TSM", "SMCI", "MSFT", "META", "PLTR", "ORCL"],
    "tariff_trade_war": ["AAPL", "TSLA", "NKE", "WMT", "TGT", "CAT", "DE", "TSM"],
    "middle_east_energy": ["XOM", "CVX", "OXY", "HAL", "SLB", "LMT", "RTX"],
    "rate_shock_financials": ["JPM", "BAC", "GS", "MS", "SCHW", "KRE"],
    "crypto_beta": ["COIN", "MSTR", "HOOD", "MARA", "RIOT"],
}

DEFAULT_THEME_KEYWORDS: dict[str, list[str]] = {
    "ai_compute": ["AI", "ARTIFICIAL INTELLIGENCE", "GPU", "CHIP", "SEMICONDUCTOR", "COMPUTE", "DATA CENTER"],
    "tariff_trade_war": ["TARIFF", "TRADE WAR", "IMPORT", "EXPORT CONTROL", "CUSTOMS"],
    "middle_east_energy": ["IRAN", "ISRAEL", "MIDDLE EAST", "OIL", "TANKER", "STRAIT OF HORMUZ"],
    "rate_shock_financials": ["YIELD", "TREASURY", "RATE", "FED", "FOMC", "JOBS", "CPI", "PCE"],
    "crypto_beta": ["BITCOIN", "CRYPTO", "ETHEREUM", "COINBASE"],
}

DEFAULT_TIER_BUDGETS: dict[str, int] = {
    "tier_1_core": 3,
    "tier_2_event": 4,
    "tier_3_position": 3,
    "tier_4_theme_regime": 3,
    "tier_5_discovery": 2,
}


@dataclass(frozen=True)
class SingleStockOptionWatchlistConfig:
    database_path: Path
    market_data_json_path: Path | None = None
    account_snapshot_json_path: Path | None = None
    paper_portfolio_state_json_path: Path | None = None
    quote_json_path: Path = Path("exports/m238_single_stock_option_quote_snapshot.json")
    quote_markdown_path: Path = Path("exports/m238_single_stock_option_quote_snapshot.md")
    structure_json_path: Path = Path("exports/m238_single_stock_options_market_structure.json")
    structure_markdown_path: Path = Path("exports/m238_single_stock_options_market_structure.md")
    outcome_json_path: Path = Path("exports/m238_single_stock_option_structure_outcome_log.json")
    outcome_markdown_path: Path = Path("exports/m238_single_stock_option_structure_outcome_log.md")
    adaptive_state_json_path: Path = Path("exports/m238_single_stock_option_watchlist_state.json")
    inclusion_log_jsonl_path: Path = Path("exports/m238_single_stock_option_watchlist_inclusions.jsonl")
    unknown_theme_discovery_json_path: Path | None = Path("exports/m234_unknown_theme_discovery.json")
    base_watchlist_symbols: list[str] = field(default_factory=lambda: list(DEFAULT_BASE_WATCHLIST))
    always_include_symbols: list[str] = field(default_factory=list)
    excluded_symbols: list[str] = field(default_factory=lambda: sorted(DEFAULT_EXCLUDED_SYMBOLS))
    theme_baskets: dict[str, list[str]] = field(default_factory=lambda: dict(DEFAULT_THEME_BASKETS))
    theme_keywords: dict[str, list[str]] = field(default_factory=lambda: dict(DEFAULT_THEME_KEYWORDS))
    tier_budgets: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_TIER_BUDGETS))
    adaptive_enabled: bool = True
    allow_unknown_news_symbols: bool = True
    news_lookback_minutes: float = 360.0
    news_limit: int = 300
    max_symbols: int = 8
    min_news_events_for_trigger: int = 1
    temporary_ttl_minutes: float = 240.0
    theme_ttl_minutes: float = 390.0
    position_ttl_minutes: float = 1440.0
    news_decay_half_life_minutes: float = 90.0
    core_score: float = 12.0
    news_score_per_event: float = 10.0
    position_score: float = 40.0
    paper_position_score: float = 28.0
    theme_score: float = 18.0
    discovery_score: float = 8.0
    liquidity_penalty_weight: float = 25.0
    prior_false_positive_penalty: float = 10.0
    min_useful_abs_return: float = 0.0015
    promotion_min_seen: int = 5
    promotion_min_completed: int = 4
    promotion_max_false_positive_rate: float = 0.35
    demotion_min_seen: int = 5
    demotion_min_false_positive_rate: float = 0.70
    max_quote_contracts: int = 120
    min_dte: int = 0
    max_dte: int = 45
    strikes_around: int = 3
    max_expirations: int = 3
    market_data_type: int = 1
    generic_ticks: str = "100,101,104,106"
    chain_timeout_seconds: float = 20.0
    quote_seconds: float = 10.0
    max_spread_pct: float = 0.30
    pin_near_pct: float = 0.5
    proximity_near_pct: float = 1.0
    max_snapshot_age_minutes: float = 30.0
    outcome_enabled: bool = True
    outcome_price_source: str = "ibkr"
    outcome_horizons: str = "5M,15M,30M,EOD"
    outcome_flat_threshold: float = 0.001
    job_timeout_seconds: float = 240.0
    client_id: int | None = None


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _parse_dt(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _read_json(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        return {}
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _read_adaptive_state(path: Path | None) -> dict[str, Any]:
    payload = _read_json(path)
    if not isinstance(payload.get("symbols"), dict):
        payload["symbols"] = {}
    return payload


def _write_json(path: Path, payload: dict[str, Any]) -> Path:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return resolved


def _append_jsonl(path: Path, payload: dict[str, Any]) -> Path:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    with resolved.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    return resolved


def _profile_path(value: Any, *, base: Path | None = None) -> Path | None:
    text = str(value or "").strip()
    if not text:
        return None
    path = Path(text).expanduser()
    if not path.is_absolute() and base is not None:
        path = base / path
    return path


def _upper_symbols(items: list[Any] | tuple[Any, ...] | None) -> list[str]:
    return sorted({str(item).strip().upper() for item in (items or []) if str(item).strip()})


def _upper_symbol_map(payload: Any, default: dict[str, list[str]]) -> dict[str, list[str]]:
    source = payload if isinstance(payload, dict) else default
    result: dict[str, list[str]] = {}
    for key, values in source.items():
        if isinstance(values, (list, tuple)):
            result[str(key)] = _upper_symbols(list(values))
    return result


def _int_map(payload: Any, default: dict[str, int]) -> dict[str, int]:
    source = payload if isinstance(payload, dict) else default
    result: dict[str, int] = {}
    for key, value in source.items():
        try:
            result[str(key)] = int(value)
        except (TypeError, ValueError):
            continue
    return result


def _is_single_stock_symbol(symbol: str, excluded: set[str]) -> bool:
    symbol = str(symbol or "").strip().upper()
    if not symbol or symbol in excluded:
        return False
    if symbol.startswith("BROADTAPE:"):
        return False
    if not re.fullmatch(r"[A-Z]{1,5}", symbol):
        return False
    return True


def _market_reference_prices(path: Path | None) -> dict[str, float]:
    payload = _read_json(path)
    prices: dict[str, float] = {}
    for row in payload.get("rows") or []:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("key") or row.get("symbol") or "").strip().upper()
        try:
            price = float(row.get("price"))
        except (TypeError, ValueError):
            continue
        if symbol and price > 0:
            prices[symbol] = price
    return prices


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _latest_quote_quality(path: Path | None) -> dict[str, dict[str, Any]]:
    payload = _read_json(path)
    rows = payload.get("quotes") or []
    quality: dict[str, dict[str, Any]] = {}
    if not isinstance(rows, list):
        return quality
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or row.get("underlying") or row.get("underlying_symbol") or "").strip().upper()
        if not symbol:
            continue
        bucket = quality.setdefault(
            symbol,
            {
                "total": 0,
                "quoted": 0,
                "last_only": 0,
                "wide_spread": 0,
                "no_quote": 0,
                "with_open_interest": 0,
                "with_volume": 0,
                "with_greeks": 0,
                "avg_spread_pct": None,
            },
        )
        bucket["total"] += 1
        status = str(row.get("quote_status") or "").strip().lower()
        if not status:
            status = "quoted" if row.get("bid") is not None or row.get("ask") is not None or row.get("last") is not None else "no_quote"
        if status in {"quoted", "last_only", "wide_spread", "no_quote"}:
            bucket[status] += 1
        if _safe_float(row.get("open_interest")) not in (None, 0.0):
            bucket["with_open_interest"] += 1
        if _safe_float(row.get("volume")) not in (None, 0.0):
            bucket["with_volume"] += 1
        option_computation = row.get("option_computation")
        if isinstance(option_computation, dict) and option_computation:
            bucket["with_greeks"] += 1
        spread = _safe_float(row.get("spread_pct"))
        if spread is not None:
            bucket["_spread_sum"] = float(bucket.get("_spread_sum") or 0.0) + spread
            bucket["_spread_count"] = int(bucket.get("_spread_count") or 0) + 1
    for bucket in quality.values():
        total = max(1, int(bucket.get("total") or 0))
        usable = int(bucket.get("quoted") or 0) + 0.5 * int(bucket.get("last_only") or 0)
        noisy = int(bucket.get("wide_spread") or 0) * 0.5 + int(bucket.get("no_quote") or 0)
        bucket["quote_quality_score"] = max(0.0, min(1.0, (usable - 0.25 * noisy) / total))
        oi_rate = int(bucket.get("with_open_interest") or 0) / total
        volume_rate = int(bucket.get("with_volume") or 0) / total
        greek_rate = int(bucket.get("with_greeks") or 0) / total
        bucket["open_interest_coverage"] = round(oi_rate, 4)
        bucket["volume_coverage"] = round(volume_rate, 4)
        bucket["greek_coverage"] = round(greek_rate, 4)
        bucket["oi_proxy_confidence"] = round(max(oi_rate, min(0.65, volume_rate * 0.65 + greek_rate * 0.35)), 4)
        bucket["trade_permission"] = (
            "trade_candidate"
            if bucket["quote_quality_score"] >= 0.65 and bucket["oi_proxy_confidence"] >= 0.35
            else "analysis_only"
            if bucket["quote_quality_score"] >= 0.25
            else "quality_blocked"
        )
        spread_count = int(bucket.pop("_spread_count", 0) or 0)
        spread_sum = float(bucket.pop("_spread_sum", 0.0) or 0.0)
        if spread_count:
            bucket["avg_spread_pct"] = spread_sum / spread_count
    return quality


def _latest_structure_quality(path: Path | None) -> dict[str, dict[str, Any]]:
    payload = _read_json(path)
    rows = payload.get("symbols") or []
    quality: dict[str, dict[str, Any]] = {}
    if not isinstance(rows, list):
        return quality
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").strip().upper()
        if symbol:
            quality[symbol] = {
                "gamma_regime": row.get("gamma_regime"),
                "confidence": row.get("confidence"),
                "confidence_score": _safe_float(row.get("confidence_score")),
                "missing_open_interest": any("open interest" in str(item).lower() for item in row.get("warnings") or []),
                "warnings": row.get("warnings") or [],
                "pin_risk": bool(row.get("pin_risk")),
                "proximity_alerts": row.get("proximity_alerts") or [],
            }
    return quality


def _latest_outcome_quality(path: Path | None) -> dict[str, dict[str, Any]]:
    payload = _read_json(path)
    rows = payload.get("rows") or []
    quality: dict[str, dict[str, Any]] = {}
    if not isinstance(rows, list):
        return quality
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        bucket = quality.setdefault(symbol, {"completed": 0, "pending": 0, "blocked": 0, "useful": 0, "avg_abs_return": None})
        status = str(row.get("price_status") or "").strip().lower()
        if status in {"complete", "completed"}:
            bucket["completed"] += 1
        elif status == "pending":
            bucket["pending"] += 1
        elif status == "blocked":
            bucket["blocked"] += 1
        raw_return = _safe_float(row.get("raw_return"))
        if raw_return is not None:
            bucket["_abs_sum"] = float(bucket.get("_abs_sum") or 0.0) + abs(raw_return)
            bucket["_abs_count"] = int(bucket.get("_abs_count") or 0) + 1
            if abs(raw_return) >= 0.0015:
                bucket["useful"] += 1
    for bucket in quality.values():
        count = int(bucket.pop("_abs_count", 0) or 0)
        total = float(bucket.pop("_abs_sum", 0.0) or 0.0)
        if count:
            bucket["avg_abs_return"] = total / count
    return quality


def _unknown_theme_rows(path: Path | None, excluded: set[str]) -> list[dict[str, Any]]:
    payload = _read_json(path)
    rows: list[dict[str, Any]] = []
    groups = payload.get("groups") or []
    if not isinstance(groups, list):
        return rows
    for group in groups[:20]:
        if not isinstance(group, dict):
            continue
        assets = group.get("provisional_asset_map") or group.get("assets") or []
        if not isinstance(assets, list):
            continue
        for symbol in assets:
            symbol_text = str(symbol or "").strip().upper()
            if _is_single_stock_symbol(symbol_text, excluded):
                rows.append(
                    {
                        "symbol": symbol_text,
                        "source": "unknown_theme_discovery",
                        "theme": str(group.get("candidate_key") or "unknown_theme"),
                        "headline": str(group.get("sample_headline") or group.get("headline") or "unknown theme discovery"),
                        "event_at": payload.get("generated_at"),
                        "event_count": group.get("event_count"),
                    }
                )
    return rows


def _adaptive_tier_budgets(base: dict[str, int], themes: set[str], recent_rows: list[dict[str, Any]]) -> dict[str, int]:
    budgets = dict(base)
    text = " ".join(str(row.get("headline") or "") for row in recent_rows).upper()
    if any(theme in themes for theme in {"rate_shock_financials", "middle_east_energy", "tariff_trade_war"}):
        budgets["tier_4_theme_regime"] = max(int(budgets.get("tier_4_theme_regime", 0)), 4)
        budgets["tier_2_event"] = max(int(budgets.get("tier_2_event", 0)), 4)
    if any(token in text for token in ["CPI", "PCE", "FOMC", "FED", "JOBS", "PAYROLL", "YIELD"]):
        budgets["tier_4_theme_regime"] = max(int(budgets.get("tier_4_theme_regime", 0)), 4)
        budgets["tier_1_core"] = min(int(budgets.get("tier_1_core", 0)), 2)
    if any(theme in themes for theme in {"ai_compute", "crypto_beta"}):
        budgets["tier_4_theme_regime"] = max(int(budgets.get("tier_4_theme_regime", 0)), 4)
    if not recent_rows:
        budgets["tier_1_core"] = max(int(budgets.get("tier_1_core", 0)), 4)
        budgets["tier_2_event"] = min(int(budgets.get("tier_2_event", 0)), 2)
    return budgets


def _age_decay(event_at: Any, *, now: datetime, half_life_minutes: float) -> float:
    parsed = _parse_dt(event_at)
    if parsed is None:
        return 1.0
    age_minutes = max(0.0, (now - parsed).total_seconds() / 60.0)
    return 0.5 ** (age_minutes / max(1.0, float(half_life_minutes)))


def _infer_themes_from_rows(rows: list[dict[str, Any]], config: SingleStockOptionWatchlistConfig) -> set[str]:
    text = " ".join(str(row.get("headline") or "") for row in rows).upper()
    themes: set[str] = {str(row.get("theme")) for row in rows if str(row.get("theme") or "").strip()}
    for theme, keywords in (config.theme_keywords or {}).items():
        if any(str(keyword).strip().upper() and str(keyword).strip().upper() in text for keyword in keywords or []):
            themes.add(str(theme))
    return themes


def _candidate_tier(symbol: str, rows: list[dict[str, Any]], themes: set[str], config: SingleStockOptionWatchlistConfig) -> str:
    sources = {str(row.get("source") or "") for row in rows}
    base = set(_upper_symbols(config.base_watchlist_symbols))
    if "account_position" in sources or "paper_position" in sources:
        return "tier_3_position"
    if symbol in base and "recent_news" not in sources and not themes:
        return "tier_1_core"
    if "recent_news" in sources:
        return "tier_2_event"
    if themes:
        return "tier_4_theme_regime"
    return "tier_5_discovery"


def _ttl_for_tier(tier: str, config: SingleStockOptionWatchlistConfig) -> float:
    if tier == "tier_3_position":
        return float(config.position_ttl_minutes)
    if tier == "tier_4_theme_regime":
        return float(config.theme_ttl_minutes)
    if tier == "tier_1_core":
        return float(config.position_ttl_minutes)
    return float(config.temporary_ttl_minutes)


def _tier_rank(tier: str) -> int:
    return {
        "tier_3_position": 0,
        "tier_2_event": 1,
        "tier_4_theme_regime": 2,
        "tier_1_core": 3,
        "tier_5_discovery": 4,
    }.get(tier, 9)


def _account_position_symbols(path: Path | None, excluded: set[str]) -> list[dict[str, Any]]:
    payload = _read_json(path)
    rows: list[dict[str, Any]] = []
    for row in payload.get("positions") or []:
        contract = row.get("contract") or {}
        symbol = str(contract.get("symbol") or row.get("symbol") or "").strip().upper()
        sec_type = str(contract.get("sec_type") or contract.get("secType") or "").upper()
        if sec_type == "OPT":
            trading_class = str(contract.get("trading_class") or contract.get("tradingClass") or "").strip().upper()
            if trading_class:
                symbol = trading_class
        if _is_single_stock_symbol(symbol, excluded):
            rows.append(
                {
                    "symbol": symbol,
                    "source": "account_position",
                    "sec_type": sec_type or "unknown",
                    "quantity": row.get("position"),
                }
            )
    return rows


def _paper_position_symbols(path: Path | None, excluded: set[str]) -> list[dict[str, Any]]:
    payload = _read_json(path)
    rows: list[dict[str, Any]] = []
    for row in payload.get("positions") or []:
        symbol = str(row.get("symbol") or "").strip().upper()
        if _is_single_stock_symbol(symbol, excluded):
            rows.append({"symbol": symbol, "source": "paper_position", "quantity": row.get("quantity")})
    return rows


def _recent_news_symbols(
    database_path: Path,
    *,
    base_watchlist: set[str],
    excluded: set[str],
    allow_unknown: bool,
    lookback_minutes: float,
    limit: int,
) -> list[dict[str, Any]]:
    resolved = database_path.expanduser().resolve()
    if not resolved.exists():
        return []
    cutoff = datetime.now(UTC) - timedelta(minutes=max(0.0, float(lookback_minutes)))
    rows: list[dict[str, Any]] = []
    try:
        con = sqlite3.connect(str(resolved), timeout=30)
        con.row_factory = sqlite3.Row
        try:
            news_rows = con.execute(
                """
                SELECT id, provider, primary_symbol, headline, published_at, received_at, provider_published_at, created_at
                FROM news_articles
                ORDER BY COALESCE(provider_published_at, published_at, received_at, created_at) DESC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        finally:
            con.close()
    except Exception:
        return []
    for row in news_rows:
        event_at = _parse_dt(row["provider_published_at"] or row["published_at"] or row["received_at"] or row["created_at"])
        if event_at is not None and event_at < cutoff:
            continue
        headline = str(row["headline"] or "")
        provider = str(row["provider"] or "")
        raw_primary = str(row["primary_symbol"] or "").strip().upper()
        candidates: set[str] = set()
        if raw_primary.startswith("BROADTAPE:"):
            candidates.update(symbol for symbol in base_watchlist if re.search(rf"\b{re.escape(symbol)}\b", headline.upper()))
        else:
            candidates.add(raw_primary)
        for token in TICKER_RE.findall(headline.upper()):
            if token in base_watchlist:
                candidates.add(token)
        for symbol in sorted(candidates):
            if not _is_single_stock_symbol(symbol, excluded):
                continue
            if symbol not in base_watchlist and not allow_unknown:
                continue
            rows.append(
                {
                    "symbol": symbol,
                    "source": "recent_news",
                    "provider": provider,
                    "headline": headline,
                    "news_id": int(row["id"]),
                    "event_at": event_at.isoformat() if event_at else None,
                    "primary_symbol": raw_primary,
                }
            )
    return rows


def _recent_news_context_rows(database_path: Path, *, lookback_minutes: float, limit: int) -> list[dict[str, Any]]:
    resolved = database_path.expanduser().resolve()
    if not resolved.exists():
        return []
    cutoff = datetime.now(UTC) - timedelta(minutes=max(0.0, float(lookback_minutes)))
    rows: list[dict[str, Any]] = []
    try:
        con = sqlite3.connect(str(resolved), timeout=30)
        con.row_factory = sqlite3.Row
        try:
            news_rows = con.execute(
                """
                SELECT id, provider, primary_symbol, headline, published_at, received_at, provider_published_at, created_at
                FROM news_articles
                ORDER BY COALESCE(provider_published_at, published_at, received_at, created_at) DESC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        finally:
            con.close()
    except Exception:
        return []
    for row in news_rows:
        event_at = _parse_dt(row["provider_published_at"] or row["published_at"] or row["received_at"] or row["created_at"])
        if event_at is not None and event_at < cutoff:
            continue
        rows.append(
            {
                "source": "recent_news_context",
                "provider": str(row["provider"] or ""),
                "headline": str(row["headline"] or ""),
                "news_id": int(row["id"]),
                "event_at": event_at.isoformat() if event_at else None,
                "primary_symbol": str(row["primary_symbol"] or "").strip().upper(),
            }
        )
    return rows


def _reference_price_csv(prices: dict[str, float], symbols: list[str]) -> str:
    wanted = set(symbols)
    return ",".join(f"{symbol}={prices[symbol]}" for symbol in sorted(wanted) if symbol in prices and prices[symbol] > 0)


def _symbol_csv(symbols: list[str]) -> str:
    return ",".join(sorted({str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()}))


def _run_command(command: list[str], *, cwd: Path, timeout_seconds: float) -> dict[str, Any]:
    started = _iso_now()
    try:
        completed = subprocess.run(
            [str(item) for item in command],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=max(1.0, float(timeout_seconds)),
            check=False,
        )
        status = "succeeded" if completed.returncode == 0 else "failed"
        return {
            "status": status,
            "started_at": started,
            "completed_at": _iso_now(),
            "returncode": completed.returncode,
            "stdout_tail": (completed.stdout or "")[-4000:],
            "stderr_tail": (completed.stderr or "")[-4000:],
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "status": "timeout",
            "started_at": started,
            "completed_at": _iso_now(),
            "timeout_seconds": float(timeout_seconds),
            "stdout_tail": (exc.stdout or "")[-4000:] if isinstance(exc.stdout, str) else "",
            "stderr_tail": (exc.stderr or "")[-4000:] if isinstance(exc.stderr, str) else "",
        }


def _update_adaptive_learning_state(config: SingleStockOptionWatchlistConfig, report: dict[str, Any]) -> dict[str, Any]:
    if not config.adaptive_enabled:
        return {}
    now = _iso_now()
    state = _read_adaptive_state(config.adaptive_state_json_path)
    symbols_state = state.setdefault("symbols", {})
    quote_quality = _latest_quote_quality(config.quote_json_path)
    structure_quality = _latest_structure_quality(config.structure_json_path)
    outcome_quality = _latest_outcome_quality(config.outcome_json_path)
    selected = set(str(symbol).upper() for symbol in report.get("selected_symbols") or [])
    for row in report.get("candidate_rows") or []:
        symbol = str(row.get("symbol") or "").upper()
        if not symbol:
            continue
        bucket = symbols_state.setdefault(symbol, {})
        bucket["seen_count"] = int(bucket.get("seen_count") or 0) + 1
        bucket["last_seen_at"] = now
        bucket["last_score"] = row.get("score")
        bucket["last_tier"] = row.get("tier")
        bucket["last_themes"] = row.get("themes") or []
        bucket["last_score_components"] = row.get("score_components") or {}
        if symbol in selected:
            bucket["selected_count"] = int(bucket.get("selected_count") or 0) + 1
            bucket["last_selected_at"] = now
            bucket["last_expires_at"] = row.get("expires_at")
        if symbol in quote_quality:
            bucket["last_quote_quality"] = quote_quality[symbol]
        if symbol in structure_quality:
            bucket["last_structure_quality"] = structure_quality[symbol]
        if symbol in outcome_quality:
            bucket["last_outcome_quality"] = outcome_quality[symbol]
            completed = int(outcome_quality[symbol].get("completed") or 0)
            blocked = int(outcome_quality[symbol].get("blocked") or 0)
            useful = int(outcome_quality[symbol].get("useful") or 0)
            total = completed + blocked
            if total:
                bucket["false_positive_rate"] = blocked / total
            if completed:
                bucket["useful_rate"] = useful / completed
                bucket["completed_outcome_count"] = completed
        seen = int(bucket.get("seen_count") or 0)
        completed_total = int(bucket.get("completed_outcome_count") or 0)
        false_positive_rate = float(bucket.get("false_positive_rate") or 0.0)
        useful_rate = float(bucket.get("useful_rate") or 0.0)
        q_score = _safe_float((bucket.get("last_quote_quality") or {}).get("quote_quality_score"))
        trade_permission = str((bucket.get("last_quote_quality") or {}).get("trade_permission") or "")
        if (
            seen >= int(config.promotion_min_seen)
            and completed_total >= int(config.promotion_min_completed)
            and false_positive_rate <= float(config.promotion_max_false_positive_rate)
            and useful_rate >= 0.35
            and (q_score is None or q_score >= 0.35)
        ):
            bucket["promotion_status"] = "promotion_candidate"
            bucket["promotion_reason"] = "sufficient_seen_completed_useful_low_false_positive"
        elif seen >= int(config.demotion_min_seen) and false_positive_rate >= float(config.demotion_min_false_positive_rate):
            bucket["promotion_status"] = "demoted_watchlist_penalty"
            bucket["promotion_reason"] = "high_false_positive_rate"
        elif trade_permission == "quality_blocked":
            bucket["promotion_status"] = "analysis_only_quality_blocked"
            bucket["promotion_reason"] = "option_quote_quality_blocked"
        else:
            bucket.setdefault("promotion_status", "candidate")
    state.update(
        {
            "state_version": "single-stock-option-watchlist-adaptive-state-v1",
            "updated_at": now,
            "last_selected_symbols": sorted(selected),
            "last_action_status_counts": report.get("action_status_counts") or {},
            "last_quote_quality_symbols": sorted(quote_quality.keys()),
            "last_structure_quality_symbols": sorted(structure_quality.keys()),
            "last_outcome_quality_symbols": sorted(outcome_quality.keys()),
        }
    )
    _write_json(config.adaptive_state_json_path, state)
    inclusion_payload = {
        "event_type": "single_stock_option_watchlist_inclusion",
        "generated_at": report.get("generated_at") or now,
        "selected_symbols": report.get("selected_symbols") or [],
        "candidate_rows": report.get("candidate_rows") or [],
        "action_status_counts": report.get("action_status_counts") or {},
        "quote_quality": {symbol: quote_quality.get(symbol) for symbol in selected if symbol in quote_quality},
        "structure_quality": {symbol: structure_quality.get(symbol) for symbol in selected if symbol in structure_quality},
        "outcome_quality": {symbol: outcome_quality.get(symbol) for symbol in selected if symbol in outcome_quality},
        "safety": report.get("safety") or {},
    }
    _append_jsonl(config.inclusion_log_jsonl_path, inclusion_payload)
    return state


def build_single_stock_option_watchlist(config: SingleStockOptionWatchlistConfig) -> dict[str, Any]:
    now = datetime.now(UTC)
    excluded = set(_upper_symbols(config.excluded_symbols))
    base_watchlist = set(_upper_symbols(config.base_watchlist_symbols))
    always_include = set(_upper_symbols(config.always_include_symbols))
    theme_baskets = {
        str(theme): set(_upper_symbols(symbols))
        for theme, symbols in (config.theme_baskets or {}).items()
    }
    quote_quality = _latest_quote_quality(config.quote_json_path) if config.adaptive_enabled else {}
    structure_quality = _latest_structure_quality(config.structure_json_path) if config.adaptive_enabled else {}
    outcome_quality = _latest_outcome_quality(config.outcome_json_path) if config.adaptive_enabled else {}
    adaptive_state = _read_adaptive_state(config.adaptive_state_json_path) if config.adaptive_enabled else {"symbols": {}}
    candidate_rows: list[dict[str, Any]] = []
    for symbol in sorted(always_include):
        if _is_single_stock_symbol(symbol, excluded):
            candidate_rows.append({"symbol": symbol, "source": "always_include"})
    for symbol in sorted(base_watchlist):
        if _is_single_stock_symbol(symbol, excluded):
            candidate_rows.append({"symbol": symbol, "source": "core_watchlist"})
    recent_rows = _recent_news_symbols(
        config.database_path,
        base_watchlist=base_watchlist,
        excluded=excluded,
        allow_unknown=bool(config.allow_unknown_news_symbols),
        lookback_minutes=config.news_lookback_minutes,
        limit=config.news_limit,
    )
    recent_context_rows = _recent_news_context_rows(
        config.database_path,
        lookback_minutes=config.news_lookback_minutes,
        limit=config.news_limit,
    )
    candidate_rows.extend(recent_rows)
    unknown_rows = _unknown_theme_rows(config.unknown_theme_discovery_json_path, excluded)
    candidate_rows.extend(unknown_rows)
    global_themes = _infer_themes_from_rows(recent_rows + recent_context_rows, config)
    global_themes.update(_infer_themes_from_rows(unknown_rows, config))
    effective_tier_budgets = _adaptive_tier_budgets(config.tier_budgets or {}, global_themes, recent_context_rows)
    for theme in sorted(global_themes):
        for symbol in sorted(theme_baskets.get(theme, set())):
            if _is_single_stock_symbol(symbol, excluded):
                candidate_rows.append({"symbol": symbol, "source": "theme_regime", "theme": theme})
    candidate_rows.extend(_account_position_symbols(config.account_snapshot_json_path, excluded))
    candidate_rows.extend(_paper_position_symbols(config.paper_portfolio_state_json_path, excluded))

    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in candidate_rows:
        symbol = str(row.get("symbol") or "").upper()
        if _is_single_stock_symbol(symbol, excluded):
            grouped.setdefault(symbol, []).append(row)
    scores: list[dict[str, Any]] = []
    for symbol, rows in grouped.items():
        source_counts = Counter(str(row.get("source") or "unknown") for row in rows)
        news_count = int(source_counts.get("recent_news") or 0)
        themes = _infer_themes_from_rows(rows, config)
        for theme, symbols in theme_baskets.items():
            if symbol in symbols and themes:
                themes.add(theme)
        tier = _candidate_tier(symbol, rows, themes, config)
        news_score = 0.0
        for row in rows:
            if row.get("source") == "recent_news":
                news_score += float(config.news_score_per_event) * _age_decay(
                    row.get("event_at"),
                    now=now,
                    half_life_minutes=config.news_decay_half_life_minutes,
                )
        position_score = float(config.position_score) if source_counts.get("account_position") else 0.0
        paper_position_score = float(config.paper_position_score) if source_counts.get("paper_position") else 0.0
        core_score = float(config.core_score) if symbol in base_watchlist else 0.0
        always_score = 100.0 if symbol in always_include else 0.0
        theme_score = float(config.theme_score) * len(themes)
        discovery_score = float(config.discovery_score) if symbol not in base_watchlist and news_count else 0.0
        symbol_state = (adaptive_state.get("symbols") or {}).get(symbol) or {}
        false_positive_rate = _safe_float(symbol_state.get("false_positive_rate")) or 0.0
        prior_penalty = float(config.prior_false_positive_penalty) * max(0.0, min(1.0, false_positive_rate))
        q = quote_quality.get(symbol) or {}
        q_score = _safe_float(q.get("quote_quality_score"))
        liquidity_penalty = 0.0
        if q_score is not None:
            liquidity_penalty = float(config.liquidity_penalty_weight) * max(0.0, 0.55 - q_score)
        trade_permission = str(q.get("trade_permission") or "unknown")
        if trade_permission == "quality_blocked":
            liquidity_penalty += float(config.liquidity_penalty_weight) * 0.40
        elif trade_permission == "analysis_only":
            liquidity_penalty += float(config.liquidity_penalty_weight) * 0.10
        triggered = (
            symbol in always_include
            or news_count >= int(config.min_news_events_for_trigger)
            or bool(source_counts.get("account_position") or source_counts.get("paper_position"))
            or bool(themes)
            or (symbol in base_watchlist and config.adaptive_enabled)
        )
        score_components = {
            "news_score": round(news_score, 4),
            "position_score": round(position_score, 4),
            "paper_position_score": round(paper_position_score, 4),
            "core_score": round(core_score, 4),
            "always_include_score": round(always_score, 4),
            "theme_score": round(theme_score, 4),
            "discovery_score": round(discovery_score, 4),
            "liquidity_penalty": round(liquidity_penalty, 4),
            "prior_false_positive_penalty": round(prior_penalty, 4),
        }
        score = sum(float(value) for key, value in score_components.items() if not key.endswith("penalty"))
        score -= liquidity_penalty + prior_penalty
        if triggered:
            ttl_minutes = _ttl_for_tier(tier, config)
            scores.append(
                {
                    "symbol": symbol,
                    "score": round(score, 4),
                    "tier": tier,
                    "ttl_minutes": ttl_minutes,
                    "expires_at": (now + timedelta(minutes=ttl_minutes)).isoformat(),
                    "themes": sorted(themes),
                    "score_components": score_components,
                    "quote_quality": q,
                    "structure_quality": structure_quality.get(symbol) or {},
                    "outcome_quality": outcome_quality.get(symbol) or {},
                    "trade_permission": trade_permission,
                    "promotion_status": str(symbol_state.get("promotion_status") or "candidate"),
                    "adaptive_state": symbol_state,
                    "source_counts": dict(source_counts),
                    "news_count": news_count,
                    "triggered": triggered,
                    "sample_reasons": rows[:5],
                }
            )
    sorted_scores = sorted(scores, key=lambda row: (_tier_rank(str(row.get("tier") or "")), -float(row.get("score") or 0), str(row.get("symbol") or "")))
    selected_rows: list[dict[str, Any]] = []
    per_tier_counts: Counter[str] = Counter()
    max_symbols = max(0, int(config.max_symbols))
    for row in sorted_scores:
        if len(selected_rows) >= max_symbols:
            break
        tier = str(row.get("tier") or "tier_5_discovery")
        tier_budget = int((effective_tier_budgets or {}).get(tier, max_symbols) or max_symbols)
        if per_tier_counts[tier] >= tier_budget:
            continue
        selected_rows.append(row)
        per_tier_counts[tier] += 1
    if len(selected_rows) < max_symbols:
        selected_symbols_so_far = {str(row.get("symbol") or "") for row in selected_rows}
        for row in sorted_scores:
            if len(selected_rows) >= max_symbols:
                break
            if str(row.get("symbol") or "") not in selected_symbols_so_far:
                selected_rows.append(row)
                selected_symbols_so_far.add(str(row.get("symbol") or ""))
    selected_symbols = [str(row.get("symbol")) for row in selected_rows]
    prices = _market_reference_prices(config.market_data_json_path)
    return {
        "watchlist_version": SINGLE_STOCK_OPTION_WATCHLIST_VERSION,
        "generated_at": _iso_now(),
        "status": "ready" if selected_symbols else "no_triggered_single_stock_symbols",
        "selected_symbols": selected_symbols,
        "selected_count": len(selected_symbols),
        "candidate_count": len(scores),
        "candidate_rows": selected_rows,
        "candidate_pool": sorted_scores[:50],
        "tier_counts": dict(Counter(str(row.get("tier") or "unknown") for row in selected_rows)),
        "adaptive_feedback": {
            "enabled": bool(config.adaptive_enabled),
            "quote_quality_symbols": sorted(quote_quality.keys()),
            "structure_quality_symbols": sorted(structure_quality.keys()),
            "outcome_quality_symbols": sorted(outcome_quality.keys()),
            "state_symbols": sorted((adaptive_state.get("symbols") or {}).keys()),
        },
        "raw_candidate_event_count": len(candidate_rows),
        "reference_prices": {symbol: prices[symbol] for symbol in selected_symbols if symbol in prices},
        "excluded_symbols": sorted(excluded),
        "policy": {
            "adaptive_enabled": bool(config.adaptive_enabled),
            "event_triggered": True,
            "tier_budgets": dict(config.tier_budgets or {}),
            "effective_tier_budgets": dict(effective_tier_budgets),
            "always_include_symbols": sorted(always_include),
            "base_watchlist_symbols": sorted(base_watchlist),
            "theme_baskets": {theme: sorted(symbols) for theme, symbols in theme_baskets.items()},
            "unknown_theme_discovery_json_path": str(config.unknown_theme_discovery_json_path) if config.unknown_theme_discovery_json_path else None,
            "allow_unknown_news_symbols": bool(config.allow_unknown_news_symbols),
            "min_news_events_for_trigger": int(config.min_news_events_for_trigger),
            "max_symbols": int(config.max_symbols),
            "news_lookback_minutes": float(config.news_lookback_minutes),
            "temporary_ttl_minutes": float(config.temporary_ttl_minutes),
            "theme_ttl_minutes": float(config.theme_ttl_minutes),
            "position_ttl_minutes": float(config.position_ttl_minutes),
        },
        "safety": {
            "read_only": True,
            "option_quote_snapshot_uses_reqMktData_only": True,
            "ibkr_order_submitted": False,
            "transmit_enabled": False,
            "live_order_routing_enabled": False,
        },
    }


def _quote_command(config: SingleStockOptionWatchlistConfig, selected_symbols: list[str], reference_prices: dict[str, float]) -> list[str]:
    command = [
        sys.executable,
        "main.py",
        "ibkr-options-quote-snapshot",
        "--symbols",
        _symbol_csv(selected_symbols),
        "--min-dte",
        str(config.min_dte),
        "--max-dte",
        str(config.max_dte),
        "--strikes-around",
        str(config.strikes_around),
        "--max-expirations",
        str(config.max_expirations),
        "--max-quote-contracts",
        str(config.max_quote_contracts),
        "--market-data-type",
        str(config.market_data_type),
        "--generic-ticks",
        str(config.generic_ticks),
        "--chain-timeout",
        str(config.chain_timeout_seconds),
        "--quote-seconds",
        str(config.quote_seconds),
        "--max-spread-pct",
        str(config.max_spread_pct),
        "--output",
        str(config.quote_markdown_path),
        "--json-output-path",
        str(config.quote_json_path),
    ]
    references = _reference_price_csv(reference_prices, selected_symbols)
    if references:
        command.extend(["--reference-prices", references])
    if config.client_id is not None:
        command.extend(["--client-id", str(config.client_id)])
    return command


def _structure_command(config: SingleStockOptionWatchlistConfig) -> list[str]:
    command = [
        sys.executable,
        "main.py",
        "options-market-structure-desk",
        "--quote-json",
        str(config.quote_json_path),
        "--pin-near-pct",
        str(config.pin_near_pct),
        "--proximity-near-pct",
        str(config.proximity_near_pct),
        "--max-snapshot-age-minutes",
        str(config.max_snapshot_age_minutes),
        "--output",
        str(config.structure_markdown_path),
        "--json-output-path",
        str(config.structure_json_path),
    ]
    if config.market_data_json_path is not None:
        command.extend(["--market-data-json", str(config.market_data_json_path)])
    return command


def _outcome_command(config: SingleStockOptionWatchlistConfig, selected_symbols: list[str]) -> list[str]:
    return [
        sys.executable,
        "main.py",
        "option-structure-outcome-build",
        "--options-market-structure-json",
        str(config.structure_json_path),
        "--horizons",
        str(config.outcome_horizons),
        "--flat-threshold",
        str(config.outcome_flat_threshold),
        "--price-source",
        str(config.outcome_price_source),
        "--symbols",
        _symbol_csv(selected_symbols),
        "--output",
        str(config.outcome_markdown_path),
        "--json-output-path",
        str(config.outcome_json_path),
    ]


def run_single_stock_option_watchlist(
    config: SingleStockOptionWatchlistConfig,
    *,
    cwd: Path,
    execute_actions: bool = False,
) -> dict[str, Any]:
    watchlist = build_single_stock_option_watchlist(config)
    selected = watchlist.get("selected_symbols") or []
    actions: list[dict[str, Any]] = []
    if selected:
        actions.append(
            {
                "action_id": "single_stock_option_quote_snapshot",
                "description": "Run read-only IBKR option quote snapshot for event-triggered single-stock symbols.",
                "command": _quote_command(config, selected, watchlist.get("reference_prices") or {}),
                "status": "planned",
                "live_ibkr": True,
                "details": {"symbols": selected, "output": str(config.quote_json_path)},
            }
        )
        actions.append(
            {
                "action_id": "single_stock_options_market_structure_rebuild",
                "description": "Build single-stock option market-structure report from the quote snapshot.",
                "command": _structure_command(config),
                "status": "planned",
                "live_ibkr": False,
                "details": {"symbols": selected, "output": str(config.structure_json_path)},
            }
        )
        if config.outcome_enabled:
            actions.append(
                {
                    "action_id": "single_stock_option_structure_outcome_log",
                    "description": "Log single-stock option structure against later market outcomes for learning.",
                    "command": _outcome_command(config, selected),
                    "status": "planned",
                    "live_ibkr": str(config.outcome_price_source).lower() == "ibkr",
                    "details": {"symbols": selected, "output": str(config.outcome_json_path)},
                }
            )
    if execute_actions:
        for action in actions:
            result = _run_command(action["command"], cwd=cwd, timeout_seconds=config.job_timeout_seconds)
            action.update(result)
            if action.get("status") != "succeeded":
                break
    status_counts = Counter(str(action.get("status") or "unknown") for action in actions)
    result = {
        **watchlist,
        "execute_actions": bool(execute_actions),
        "action_status_counts": dict(status_counts),
        "actions": actions,
        "outputs": {
            "quote_json_path": str(config.quote_json_path),
            "structure_json_path": str(config.structure_json_path),
            "outcome_json_path": str(config.outcome_json_path) if config.outcome_enabled else None,
            "adaptive_state_json_path": str(config.adaptive_state_json_path) if config.adaptive_enabled else None,
            "inclusion_log_jsonl_path": str(config.inclusion_log_jsonl_path) if config.adaptive_enabled else None,
        },
    }
    if config.adaptive_enabled:
        result["adaptive_state"] = _update_adaptive_learning_state(config, result)
    return result


def single_stock_option_watchlist_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Single-Stock Option Watchlist",
        "",
        f"- Version: `{report.get('watchlist_version')}`",
        f"- Generated at: `{report.get('generated_at')}`",
        f"- Status: `{report.get('status')}`",
        f"- Selected symbols: `{report.get('selected_symbols') or []}`",
        f"- Tier counts: `{report.get('tier_counts') or {}}`",
        f"- Adaptive feedback: `{report.get('adaptive_feedback') or {}}`",
        f"- Execute actions: `{report.get('execute_actions')}` action statuses `{report.get('action_status_counts') or {}}`",
        "",
        "## Selected candidates",
        "",
        "| Symbol | Tier | Score | TTL | News | Themes | Quality | Sources | Sample reason |",
        "| --- | --- | ---: | ---: | ---: | --- | --- | --- | --- |",
    ]
    for row in report.get("candidate_rows") or []:
        sample = ((row.get("sample_reasons") or [{}])[0] or {})
        quality = row.get("quote_quality") or {}
        q_score = quality.get("quote_quality_score")
        quality_text = "-" if q_score is None else f"{float(q_score):.2f}"
        lines.append(
            f"| `{row.get('symbol')}` | `{row.get('tier')}` | {row.get('score')} | {row.get('ttl_minutes')} | "
            f"{row.get('news_count')} | `{row.get('themes') or []}` | `{quality_text}` | "
            f"`{row.get('source_counts') or {}}` | {str(sample.get('headline') or sample.get('source') or '-').replace('|', '/')} |"
        )
    if not report.get("candidate_rows"):
        lines.append("| - | - | - | - | - | - | - | - | - |")
    lines.extend(
        [
            "",
            "## Actions",
            "",
            "| Action | Status | Live IBKR | Command |",
            "| --- | --- | --- | --- |",
        ]
    )
    for action in report.get("actions") or []:
        lines.append(
            f"| `{action.get('action_id')}` | `{action.get('status')}` | `{action.get('live_ibkr')}` | "
            f"`{' '.join(str(part) for part in action.get('command') or [])}` |"
        )
    if not report.get("actions"):
        lines.append("| - | - | - | - |")
    lines.extend(
        [
            "",
            "## Safety",
            "",
            "- Read-only single-stock option discovery and learning path.",
            "- No broker orders, transmit, live routing, or portfolio mutation.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_single_stock_option_watchlist_json(path: Path, report: dict[str, Any]) -> Path:
    return _write_json(path, report)


def write_single_stock_option_watchlist_markdown(path: Path, report: dict[str, Any]) -> Path:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(single_stock_option_watchlist_markdown(report), encoding="utf-8")
    return resolved


def load_single_stock_option_watchlist_config(profile_json: Path | None, *, database_path: Path | None = None) -> SingleStockOptionWatchlistConfig:
    profile: dict[str, Any] = {}
    base: Path | None = None
    if profile_json is not None:
        resolved = profile_json.expanduser().resolve()
        profile = json.loads(resolved.read_text(encoding="utf-8"))
        base = resolved.parent
    paths = profile.get("paths") if isinstance(profile.get("paths"), dict) else {}
    watchlist = profile.get("watchlist") if isinstance(profile.get("watchlist"), dict) else {}
    adaptive = profile.get("adaptive_watchlist") if isinstance(profile.get("adaptive_watchlist"), dict) else {}
    quote = profile.get("quote_snapshot") if isinstance(profile.get("quote_snapshot"), dict) else {}
    structure = profile.get("options_market_structure") if isinstance(profile.get("options_market_structure"), dict) else {}
    outcome = profile.get("outcome_learning") if isinstance(profile.get("outcome_learning"), dict) else {}
    db_path = database_path or _profile_path(paths.get("database_path"), base=base) or Path("market_events.db")
    return SingleStockOptionWatchlistConfig(
        database_path=db_path,
        market_data_json_path=_profile_path(paths.get("market_data_json_path"), base=base),
        account_snapshot_json_path=_profile_path(paths.get("account_snapshot_json_path"), base=base),
        paper_portfolio_state_json_path=_profile_path(paths.get("paper_portfolio_state_json_path"), base=base),
        quote_json_path=_profile_path(paths.get("quote_json_path"), base=base) or Path("exports/m238_single_stock_option_quote_snapshot.json"),
        quote_markdown_path=_profile_path(paths.get("quote_markdown_path"), base=base) or Path("exports/m238_single_stock_option_quote_snapshot.md"),
        structure_json_path=_profile_path(paths.get("structure_json_path"), base=base) or Path("exports/m238_single_stock_options_market_structure.json"),
        structure_markdown_path=_profile_path(paths.get("structure_markdown_path"), base=base) or Path("exports/m238_single_stock_options_market_structure.md"),
        outcome_json_path=_profile_path(paths.get("outcome_json_path"), base=base) or Path("exports/m238_single_stock_option_structure_outcome_log.json"),
        outcome_markdown_path=_profile_path(paths.get("outcome_markdown_path"), base=base) or Path("exports/m238_single_stock_option_structure_outcome_log.md"),
        adaptive_state_json_path=_profile_path(paths.get("adaptive_state_json_path"), base=base) or Path("exports/m238_single_stock_option_watchlist_state.json"),
        inclusion_log_jsonl_path=_profile_path(paths.get("inclusion_log_jsonl_path"), base=base) or Path("exports/m238_single_stock_option_watchlist_inclusions.jsonl"),
        unknown_theme_discovery_json_path=_profile_path(paths.get("unknown_theme_discovery_json_path"), base=base) or Path("exports/m234_unknown_theme_discovery.json"),
        base_watchlist_symbols=_upper_symbols(watchlist.get("base_watchlist_symbols") or DEFAULT_BASE_WATCHLIST),
        always_include_symbols=_upper_symbols(watchlist.get("always_include_symbols") or []),
        excluded_symbols=_upper_symbols(watchlist.get("excluded_symbols") or sorted(DEFAULT_EXCLUDED_SYMBOLS)),
        theme_baskets=_upper_symbol_map(adaptive.get("theme_baskets"), DEFAULT_THEME_BASKETS),
        theme_keywords={
            str(key): [str(item).upper() for item in values]
            for key, values in (adaptive.get("theme_keywords") if isinstance(adaptive.get("theme_keywords"), dict) else DEFAULT_THEME_KEYWORDS).items()
            if isinstance(values, list)
        },
        tier_budgets=_int_map(adaptive.get("tier_budgets"), DEFAULT_TIER_BUDGETS),
        adaptive_enabled=bool(adaptive.get("enabled", True)),
        allow_unknown_news_symbols=bool(watchlist.get("allow_unknown_news_symbols", True)),
        news_lookback_minutes=float(watchlist.get("news_lookback_minutes", 360.0)),
        news_limit=int(watchlist.get("news_limit", 300)),
        max_symbols=int(watchlist.get("max_symbols", 8)),
        min_news_events_for_trigger=int(watchlist.get("min_news_events_for_trigger", 1)),
        temporary_ttl_minutes=float(adaptive.get("temporary_ttl_minutes", 240.0)),
        theme_ttl_minutes=float(adaptive.get("theme_ttl_minutes", 390.0)),
        position_ttl_minutes=float(adaptive.get("position_ttl_minutes", 1440.0)),
        news_decay_half_life_minutes=float(adaptive.get("news_decay_half_life_minutes", 90.0)),
        core_score=float(adaptive.get("core_score", 12.0)),
        news_score_per_event=float(adaptive.get("news_score_per_event", 10.0)),
        position_score=float(adaptive.get("position_score", 40.0)),
        paper_position_score=float(adaptive.get("paper_position_score", 28.0)),
        theme_score=float(adaptive.get("theme_score", 18.0)),
        discovery_score=float(adaptive.get("discovery_score", 8.0)),
        liquidity_penalty_weight=float(adaptive.get("liquidity_penalty_weight", 25.0)),
        prior_false_positive_penalty=float(adaptive.get("prior_false_positive_penalty", 10.0)),
        min_useful_abs_return=float(adaptive.get("min_useful_abs_return", 0.0015)),
        promotion_min_seen=int(adaptive.get("promotion_min_seen", 5)),
        promotion_min_completed=int(adaptive.get("promotion_min_completed", 4)),
        promotion_max_false_positive_rate=float(adaptive.get("promotion_max_false_positive_rate", 0.35)),
        demotion_min_seen=int(adaptive.get("demotion_min_seen", 5)),
        demotion_min_false_positive_rate=float(adaptive.get("demotion_min_false_positive_rate", 0.70)),
        max_quote_contracts=int(quote.get("max_quote_contracts", 120)),
        min_dte=int(quote.get("min_dte", 0)),
        max_dte=int(quote.get("max_dte", 45)),
        strikes_around=int(quote.get("strikes_around", 3)),
        max_expirations=int(quote.get("max_expirations", 3)),
        market_data_type=int(quote.get("market_data_type", 1)),
        generic_ticks=str(quote.get("generic_ticks", "100,101,104,106")),
        chain_timeout_seconds=float(quote.get("chain_timeout_seconds", 20.0)),
        quote_seconds=float(quote.get("quote_seconds", 10.0)),
        max_spread_pct=float(quote.get("max_spread_pct", 0.30)),
        pin_near_pct=float(structure.get("pin_near_pct", 0.5)),
        proximity_near_pct=float(structure.get("proximity_near_pct", 1.0)),
        max_snapshot_age_minutes=float(structure.get("max_snapshot_age_minutes", 30.0)),
        outcome_enabled=bool(outcome.get("enabled", True)),
        outcome_price_source=str(outcome.get("price_source", "ibkr")),
        outcome_horizons=str(outcome.get("horizons", "5M,15M,30M,EOD")),
        outcome_flat_threshold=float(outcome.get("flat_threshold", 0.001)),
        job_timeout_seconds=float(profile.get("job_timeout_seconds", 240.0)),
        client_id=int(quote["client_id"]) if quote.get("client_id") is not None else None,
    )
