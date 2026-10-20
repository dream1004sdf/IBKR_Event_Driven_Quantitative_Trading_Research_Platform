from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from recorder.market_outcomes import contract_resolution_profile_for_symbol, primary_exchange_candidates_for_symbol


DEFAULT_SYMBOL_TIER_CONFIG = Path("config/news_market_outcome_symbol_tiers_v1.json")


@dataclass(frozen=True)
class SymbolTierRule:
    symbol: str
    tier: str
    collection_mode: str
    priority: int
    description: str
    keywords: tuple[str, ...]


@dataclass(frozen=True)
class SymbolTierPolicy:
    version: str
    config_path: Path
    default_unknown_tier: str
    rules_by_symbol: dict[str, SymbolTierRule]
    tier_metadata: dict[str, dict[str, Any]]

    def classify(self, symbol: str) -> SymbolTierRule:
        normalized = str(symbol or "").strip().upper()
        rule = self.rules_by_symbol.get(normalized)
        if rule is not None:
            return rule
        return SymbolTierRule(
            symbol=normalized,
            tier=self.default_unknown_tier,
            collection_mode="review_only",
            priority=999,
            description="New/unclassified symbol observed during backfill; review before deep intraday collection.",
            keywords=(),
        )

    def symbols_for_tiers(self, tiers: set[str]) -> set[str]:
        if not tiers:
            return set(self.rules_by_symbol)
        return {symbol for symbol, rule in self.rules_by_symbol.items() if rule.tier in tiers}


def load_symbol_tier_policy(path: str | Path | None = None) -> SymbolTierPolicy:
    config_path = Path(path or DEFAULT_SYMBOL_TIER_CONFIG)
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    rules_by_symbol: dict[str, SymbolTierRule] = {}
    tier_metadata: dict[str, dict[str, Any]] = {}
    for tier_name, tier_payload in (payload.get("tiers") or {}).items():
        tier = str(tier_name)
        collection_mode = str(tier_payload.get("collection_mode") or "review_only")
        priority = int(tier_payload.get("priority") or 999)
        description = str(tier_payload.get("description") or "")
        keywords = tuple(str(item).lower() for item in (tier_payload.get("keywords") or []) if str(item).strip())
        tier_metadata[tier] = {
            "collection_mode": collection_mode,
            "priority": priority,
            "description": description,
            "keywords": list(keywords),
            "symbols": [str(item).strip().upper() for item in (tier_payload.get("symbols") or []) if str(item).strip()],
        }
        for symbol in tier_metadata[tier]["symbols"]:
            rules_by_symbol[symbol] = SymbolTierRule(
                symbol=symbol,
                tier=tier,
                collection_mode=collection_mode,
                priority=priority,
                description=description,
                keywords=keywords,
            )
    return SymbolTierPolicy(
        version=str(payload.get("version") or "unknown"),
        config_path=config_path,
        default_unknown_tier=str(payload.get("default_unknown_tier") or "unclassified_review"),
        rules_by_symbol=rules_by_symbol,
        tier_metadata=tier_metadata,
    )


def parse_tier_filter(value: str | None) -> set[str]:
    return {item.strip() for item in str(value or "").split(",") if item.strip()}


def candidate_allowed_by_symbol_tier(
    *,
    symbol: str,
    headline: str,
    primary_symbol: str,
    policy: SymbolTierPolicy | None,
    tier_filter: set[str],
    event_proxy_policy: str = "matched-news-only",
    unknown_symbol_policy: str = "exclude",
) -> bool:
    if policy is None:
        return True
    rule = policy.classify(symbol)
    if rule.tier == policy.default_unknown_tier:
        return unknown_symbol_policy == "include"
    if tier_filter and rule.tier not in tier_filter:
        return False
    if rule.collection_mode != "event_window":
        return True
    if event_proxy_policy == "full-symbol":
        return True
    if str(primary_symbol or "").strip().upper() == str(symbol or "").strip().upper():
        return True
    text = str(headline or "").lower()
    return _headline_matches_keywords(text, rule.keywords)


def _headline_matches_keywords(text: str, keywords: tuple[str, ...]) -> bool:
    for keyword in keywords:
        normalized = str(keyword or "").strip().lower()
        if not normalized:
            continue
        if " " in normalized or not normalized.replace("-", "").isalnum():
            if normalized in text:
                return True
            continue
        pattern = rf"(?<![a-z0-9]){re.escape(normalized)}(?![a-z0-9])"
        if re.search(pattern, text):
            return True
    return False


def build_news_symbol_tier_report(
    database_path: str | Path,
    *,
    start: str,
    end: str,
    config_path: str | Path | None = None,
    limit_samples_per_symbol: int = 2,
) -> dict[str, Any]:
    policy = load_symbol_tier_policy(config_path)
    db_path = Path(database_path)
    report: dict[str, Any] = {
        "version": "news-symbol-tier-report-v1",
        "database_path": str(db_path),
        "start": start,
        "end": end,
        "symbol_tier_config": str(policy.config_path),
        "symbol_tier_config_version": policy.version,
        "symbols": [],
        "tier_counts": {},
        "collection_mode_counts": {},
        "contract_resolution_risk_counts": {},
        "recommended_symbols": {
            "core_full_intraday": [],
            "event_window_intraday": [],
            "opportunistic_on_demand": [],
            "unclassified_review": [],
        },
    }
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT upper(ns.symbol) AS symbol,
                   count(*) AS article_count,
                   count(DISTINCT n.id) AS news_count,
                   min(n.published_at) AS first_news_at,
                   max(n.published_at) AS last_news_at
              FROM news_articles n
              JOIN news_symbols ns ON ns.news_id=n.id
             WHERE n.published_at >= ?
               AND n.published_at <= ?
               AND ns.symbol IS NOT NULL
               AND ns.symbol <> ''
               AND ns.symbol NOT LIKE '%:%'
             GROUP BY upper(ns.symbol)
             ORDER BY article_count DESC, symbol ASC
            """,
            (start, end),
        ).fetchall()
        tier_counts: Counter[str] = Counter()
        mode_counts: Counter[str] = Counter()
        contract_risk_counts: Counter[str] = Counter()
        symbol_records: list[dict[str, Any]] = []
        for row in rows:
            symbol = str(row["symbol"] or "").upper()
            rule = policy.classify(symbol)
            provider_rows = conn.execute(
                """
                SELECT n.provider, count(*) AS count
                  FROM news_articles n
                  JOIN news_symbols ns ON ns.news_id=n.id
                 WHERE n.published_at >= ?
                   AND n.published_at <= ?
                   AND upper(ns.symbol)=?
                 GROUP BY n.provider
                 ORDER BY count DESC
                 LIMIT 5
                """,
                (start, end, symbol),
            ).fetchall()
            primary_rows = conn.execute(
                """
                SELECT n.primary_symbol, count(*) AS count
                  FROM news_articles n
                  JOIN news_symbols ns ON ns.news_id=n.id
                 WHERE n.published_at >= ?
                   AND n.published_at <= ?
                   AND upper(ns.symbol)=?
                 GROUP BY n.primary_symbol
                 ORDER BY count DESC
                 LIMIT 5
                """,
                (start, end, symbol),
            ).fetchall()
            sample_rows = conn.execute(
                """
                SELECT n.provider, n.primary_symbol, n.published_at, n.headline
                  FROM news_articles n
                  JOIN news_symbols ns ON ns.news_id=n.id
                 WHERE n.published_at >= ?
                   AND n.published_at <= ?
                   AND upper(ns.symbol)=?
                 ORDER BY n.published_at DESC
                 LIMIT ?
                """,
                (start, end, symbol, max(0, int(limit_samples_per_symbol))),
            ).fetchall()
            provider_counts = {str(item["provider"] or ""): int(item["count"] or 0) for item in provider_rows}
            primary_counts = {str(item["primary_symbol"] or ""): int(item["count"] or 0) for item in primary_rows}
            samples = [
                {
                    "provider": str(item["provider"] or ""),
                    "primary_symbol": str(item["primary_symbol"] or ""),
                    "published_at": str(item["published_at"] or ""),
                    "headline": str(item["headline"] or "")[:240],
                }
                for item in sample_rows
            ]
            contract_profile = contract_resolution_profile_for_symbol(symbol)
            for flag in contract_profile.get("risk_flags") or []:
                contract_risk_counts[str(flag)] += 1
            record = {
                "symbol": symbol,
                "tier": rule.tier,
                "collection_mode": rule.collection_mode,
                "priority": rule.priority,
                "primary_exchange_candidates": list(primary_exchange_candidates_for_symbol(symbol)),
                "contract_resolution_profile": contract_profile,
                "contract_resolution_risk_flags": list(contract_profile.get("risk_flags") or []),
                "requires_contract_probe": bool(contract_profile.get("requires_contract_probe")),
                "article_count": int(row["article_count"] or 0),
                "news_count": int(row["news_count"] or 0),
                "first_news_at": row["first_news_at"],
                "last_news_at": row["last_news_at"],
                "provider_counts": provider_counts,
                "primary_symbol_counts": primary_counts,
                "sample_headlines": samples,
                "recommended_action": _recommended_action(rule),
            }
            symbol_records.append(record)
            tier_counts[rule.tier] += 1
            mode_counts[rule.collection_mode] += 1
            if rule.collection_mode == "full_intraday" and rule.tier in {"core_always", "macro_proxy"}:
                report["recommended_symbols"]["core_full_intraday"].append(symbol)
            elif rule.collection_mode == "event_window":
                report["recommended_symbols"]["event_window_intraday"].append(symbol)
            elif rule.collection_mode == "on_demand":
                report["recommended_symbols"]["opportunistic_on_demand"].append(symbol)
            else:
                report["recommended_symbols"]["unclassified_review"].append(symbol)
        report["symbols"] = symbol_records
        report["tier_counts"] = dict(tier_counts)
        report["collection_mode_counts"] = dict(mode_counts)
        report["contract_resolution_risk_counts"] = dict(contract_risk_counts)
    for key, value in report["recommended_symbols"].items():
        report["recommended_symbols"][key] = sorted(set(value))
    return report


def _recommended_action(rule: SymbolTierRule) -> str:
    if rule.collection_mode == "full_intraday":
        return "deep_intraday_backfill"
    if rule.collection_mode == "event_window":
        return "matched_event_window_intraday_only"
    if rule.collection_mode == "on_demand":
        return "skip_until_promoted_or_direct_trade_candidate"
    return "review_before_collection"


def news_symbol_tier_report_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# News Symbol Tier Report",
        "",
        f"- Range: `{report.get('start')}` to `{report.get('end')}`",
        f"- Config: `{report.get('symbol_tier_config')}`",
        f"- Tier counts: `{report.get('tier_counts') or {}}`",
        f"- Collection mode counts: `{report.get('collection_mode_counts') or {}}`",
        f"- Contract risk counts: `{report.get('contract_resolution_risk_counts') or {}}`",
        "",
        "## Recommended collections",
        "",
    ]
    for key, symbols in (report.get("recommended_symbols") or {}).items():
        lines.append(f"- `{key}`: `{','.join(symbols or [])}`")
    lines.extend(["", "## Symbols", "", "| Symbol | Tier | Mode | Exchanges | Contract flags | Probe | News | Action |", "| --- | --- | --- | --- | --- | --- | ---: | --- |"])
    for item in report.get("symbols") or []:
        lines.append(
            "| `{symbol}` | `{tier}` | `{mode}` | `{exchanges}` | `{flags}` | `{probe}` | {news} | `{action}` |".format(
                symbol=item.get("symbol"),
                tier=item.get("tier"),
                mode=item.get("collection_mode"),
                exchanges=",".join(item.get("primary_exchange_candidates") or []),
                flags=",".join(item.get("contract_resolution_risk_flags") or []),
                probe=item.get("requires_contract_probe"),
                news=item.get("news_count"),
                action=item.get("recommended_action"),
            )
        )
    return "\n".join(lines) + "\n"


def write_news_symbol_tier_report(
    report: dict[str, Any],
    *,
    output: Path | None,
    json_output_path: Path | None,
) -> None:
    if json_output_path:
        json_output_path.parent.mkdir(parents=True, exist_ok=True)
        json_output_path.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(news_symbol_tier_report_markdown(report), encoding="utf-8")
