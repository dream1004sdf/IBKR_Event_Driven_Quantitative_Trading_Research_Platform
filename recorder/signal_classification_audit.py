from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


SIGNAL_CLASSIFICATION_AUDIT_VERSION = "signal-classification-audit-v2"


MACRO_KEYWORDS = (
    "cpi",
    "inflation",
    "pce",
    "fomc",
    "fed",
    "payroll",
    "jobs report",
    "unemployment",
    "jobless",
    "ism",
    "pmi",
    "retail sales",
    "gdp",
    "treasury yield",
)
MACRO_HARD_EVENT_KEYWORDS = (
    "cpi",
    "consumer price index",
    "inflation report",
    "pce",
    "fomc",
    "fed decision",
    "rate decision",
    "nonfarm payroll",
    "payrolls",
    "jobs report",
    "unemployment rate",
    "jobless claims",
    "ism",
    "pmi",
    "retail sales",
    "gdp",
    "treasury yield",
    "auction",
)
GEOPOLITICAL_KEYWORDS = (
    "missile",
    "strike",
    "attack",
    "invasion",
    "ceasefire",
    "sanction",
    "tariff",
    "iran",
    "israel",
    "taiwan",
    "china",
    "russia",
    "ukraine",
    "north korea",
    "red sea",
    "middle east",
    "korea peninsula",
)
GEOPOLITICAL_REGION_KEYWORDS = (
    "iran",
    "israel",
    "taiwan",
    "china",
    "russia",
    "ukraine",
    "north korea",
    "south korea",
    "red sea",
    "middle east",
    "gaza",
    "yemen",
    "strait",
)
GEOPOLITICAL_HARD_EVENT_KEYWORDS = (
    "war",
    "missile",
    "strike",
    "attack",
    "invasion",
    "ceasefire",
    "sanction",
    "tariff",
    "export control",
    "restriction",
    "chip curbs",
    "tension",
    "military",
    "conflict",
    "retaliation",
    "blockade",
)
GEOPOLITICAL_METAPHOR_EXCLUSIONS = (
    "compute war",
    "price war",
    "bidding war",
    "talent war",
    "streaming war",
    "cloud war",
    "ai war",
    "cola war",
)
ANALYST_KEYWORDS = (
    "analyst",
    "upgrade",
    "downgrade",
    "price target",
    "initiates",
    "maintains",
    "reiterates",
)
EARNINGS_KEYWORDS = ("earnings", "eps", "revenue", "guidance", "results")
EARNINGS_HARD_EVENT_KEYWORDS = (
    "reports",
    "reported",
    "announces",
    "announced",
    "issues guidance",
    "raises guidance",
    "cuts guidance",
    "beats",
    "misses",
    "eps",
    "revenue",
    "q1",
    "q2",
    "q3",
    "q4",
    "quarter",
    "conference call",
    "webcast",
)
ANALYSIS_OR_OPINION_KEYWORDS = (
    "stock a buy",
    "is stock a buy",
    "buy before",
    "buy after",
    "can it rebound",
    "since last earnings",
    "next earnings report",
    "beat estimates again",
    "ahead of earnings",
    "ahead of q1 results",
    "ahead of q2 results",
    "ahead of q3 results",
    "ahead of q4 results",
    "ahead of q1 earnings",
    "ahead of q2 earnings",
    "ahead of q3 earnings",
    "ahead of q4 earnings",
    "ahead of the company's",
    "earnings are imminent",
    "analysts revise forecasts ahead",
    "prediction market bettors",
    "what does",
    "here's what",
    "previewing",
    "what to expect",
    "preview",
    "why shares",
    "major indexes",
    "us stocks likely",
    "could",
    "may",
)
NON_EARNINGS_RESULTS_KEYWORDS = (
    "clinical trial",
    "trial results",
    "study results",
    "election results",
    "test results",
    "poll results",
    "pension",
    "eps 2026",
)
MACRO_PREVIEW_KEYWORDS = (
    "ahead",
    "ahead of fed decision",
    "ahead of fomc decision",
    "ahead of cpi",
    "jobs report looms",
    "payrolls loom",
    "fomc looms",
    "before",
    "preview",
    "what to expect",
    "awaits",
    "brace for",
    "due",
    "set for",
)
MACRO_ACTUAL_RESULT_KEYWORDS = (
    "vs",
    "actual",
    "shows",
    "showed",
    "reports",
    "reported",
    "rose",
    "rises",
    "fell",
    "falls",
    "miss",
    "beat",
    "beats",
    "higher than",
    "lower than",
)
MACRO_ACTUAL_RESULT_STRONG_KEYWORDS = tuple(
    keyword for keyword in MACRO_ACTUAL_RESULT_KEYWORDS if keyword not in {"reports", "reported"}
)
MARKET_PROXY_ASSETS = {
    "SPY",
    "QQQ",
    "IWM",
    "DIA",
    "TLT",
    "GLD",
    "VIX",
    "XLE",
    "USO",
    "SMH",
    "SOXX",
}


def build_signal_classification_audit(
    database_path: str | Path,
    *,
    start: str,
    end: str,
    limit: int = 5000,
    symbol_tier_report_path: str | Path | None = None,
) -> dict[str, Any]:
    db_path = Path(database_path)
    notes: list[dict[str, Any]] = []
    summary: dict[str, Any] = {
        "version": SIGNAL_CLASSIFICATION_AUDIT_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "database_path": str(db_path),
        "start": start,
        "end": end,
        "limit": int(limit),
        "rows_scanned": 0,
        "note_count": 0,
        "notes": notes,
        "policy": {
            "mode": "strict_high_confidence",
            "purpose": "Keep cycle-1 learning baseline clean by recording only high-confidence source/theme/ticker classification issues.",
            "excluded": "Broad keyword-only hints and opinion/preview headlines are intentionally ignored.",
        },
    }
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        if not _table_exists(conn, "external_market_signals"):
            summary["status"] = "missing_external_market_signals"
            return summary
        raw_json_expr = "raw_json" if _table_has_column(conn, "external_market_signals", "raw_json") else "'' AS raw_json"
        rows = conn.execute(
            f"""
            SELECT id, source, title, published_at, region, theme, sentiment_score,
                   sentiment_label, confidence, assets_json, asset_map_reason, {raw_json_expr}
              FROM external_market_signals
             WHERE published_at >= ?
               AND published_at <= ?
             ORDER BY published_at DESC, id DESC
             LIMIT ?
            """,
            (start, end, max(1, int(limit))),
        ).fetchall()
    for row in rows:
        _audit_row(dict(row), notes)
    deduped_notes = _dedupe_notes(notes)
    notes[:] = deduped_notes
    tier_reconciliation = _reconcile_notes_with_symbol_tiers(notes, symbol_tier_report_path)
    notes[:] = tier_reconciliation["kept_notes"]
    summary["rows_scanned"] = len(rows)
    summary["note_count"] = len(notes)
    summary["by_category"] = dict(Counter(note["category"] for note in notes))
    summary["by_source"] = dict(Counter(note["source"] for note in notes))
    summary["by_severity"] = dict(Counter(note["severity"] for note in notes))
    summary["tier_reconciliation"] = {
        key: value for key, value in tier_reconciliation.items() if key != "kept_notes"
    }
    summary["training_use"] = {
        "stage": "m236_news_learning",
        "mode": "source_quality_and_ticker_mapping_review",
        "effect": "Notes do not directly change live thresholds; they flag source/theme/ticker quality for final learning reports and post-cycle review.",
    }
    return summary


def write_signal_classification_audit(
    report: dict[str, Any],
    *,
    output: Path | None = None,
    json_output_path: Path | None = None,
    notes_jsonl_path: Path | None = None,
) -> None:
    if json_output_path is not None:
        json_output_path.parent.mkdir(parents=True, exist_ok=True)
        json_output_path.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    if notes_jsonl_path is not None:
        notes_jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        with notes_jsonl_path.open("w", encoding="utf-8") as fh:
            for note in report.get("notes") or []:
                fh.write(json.dumps(note, ensure_ascii=False, default=str) + "\n")
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(signal_classification_audit_markdown(report), encoding="utf-8")


def signal_classification_audit_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Signal Classification Audit",
        "",
        f"- Version: `{report.get('version')}`",
        f"- Rows scanned: `{report.get('rows_scanned')}`",
        f"- Notes: `{report.get('note_count')}`",
        f"- By category: `{report.get('by_category') or {}}`",
        f"- By source: `{report.get('by_source') or {}}`",
        f"- Tier reconciliation: `{report.get('tier_reconciliation') or {}}`",
        "",
        "## Top notes",
        "",
    ]
    for note in (report.get("notes") or [])[:100]:
        lines.append(
            f"- `{note.get('severity')}` `{note.get('category')}` "
            f"`{note.get('source')}` id `{note.get('signal_id')}`: {note.get('reason')} "
            f"-> {note.get('training_use')}"
        )
    if not report.get("notes"):
        lines.append("- None.")
    tier_reconciliation = report.get("tier_reconciliation") or {}
    if tier_reconciliation:
        lines.extend(
            [
                "",
                "## Ticker-tier reconciliation",
                "",
                f"- Symbol-tier count: `{tier_reconciliation.get('symbol_tier_count')}`",
                f"- Kept notes after reconciliation: `{tier_reconciliation.get('kept_note_count')}`",
                f"- Suppressed notes: `{tier_reconciliation.get('suppressed_note_count')}`",
                f"- Kept missing ticker notes: `{tier_reconciliation.get('kept_missing_ticker_count')}`",
                f"- Kept unresolved ticker notes: `{tier_reconciliation.get('kept_unresolved_ticker_count')}`",
                f"- Status counts: `{tier_reconciliation.get('status_counts') or {}}`",
                f"- Suppressed status counts: `{tier_reconciliation.get('suppressed_status_counts') or {}}`",
                "",
                "### Missing from symbol-tier report",
                "",
            ]
        )
        for item in (tier_reconciliation.get("kept_missing_tickers") or [])[:25]:
            lines.append(
                f"- id `{item.get('signal_id')}` `{item.get('source')}` "
                f"tickers `{item.get('missing_tickers')}`: {item.get('reason')} "
                f"Title: {item.get('title')}"
            )
        if not tier_reconciliation.get("kept_missing_tickers"):
            lines.append("- None.")
        lines.extend(["", "### Ticker unresolved by local parser", ""])
        for item in (tier_reconciliation.get("kept_unresolved_tickers") or [])[:25]:
            lines.append(
                f"- id `{item.get('signal_id')}` `{item.get('source')}`: {item.get('reason')} "
                f"Title: {item.get('title')}"
            )
        if not tier_reconciliation.get("kept_unresolved_tickers"):
            lines.append("- None.")
        lines.extend(["", "### Suppressed after ticker-tier reconciliation", ""])
        for item in (tier_reconciliation.get("suppressed_notes") or [])[:25]:
            lines.append(
                f"- id `{item.get('signal_id')}` `{item.get('source')}` "
                f"`{item.get('tier_reconciliation_status')}`: {item.get('tier_reconciliation_reason')} "
                f"Title: {item.get('title')}"
            )
        if not tier_reconciliation.get("suppressed_notes"):
            lines.append("- None.")
    lines.extend(
        [
            "",
            "## Safety",
            "",
            "- Audit notes are source-quality context only.",
            "- No broker orders, no transmit, no live routing, no live threshold promotion.",
        ]
    )
    return "\n".join(lines) + "\n"


def _audit_row(row: dict[str, Any], notes: list[dict[str, Any]]) -> None:
    title = str(row.get("title") or "")
    theme = str(row.get("theme") or "").lower()
    sentiment_label = str(row.get("sentiment_label") or "").lower()
    source = str(row.get("source") or "")
    assets = _parse_assets(row.get("assets_json"))
    text = title.lower()

    macro_like = _is_high_confidence_macro_event(text)
    if macro_like and not _theme_contains(theme, ("macro", "economic", "rate", "policy", "treasury")):
        _add_note(
            row,
            notes,
            "yellow",
            "macro_theme_mismatch",
            "High-confidence macro/economic event headline is not classified as macro/economic policy.",
        )
    geo_like = _is_high_confidence_geopolitical_event(text)
    if geo_like and not _theme_contains(theme, ("geo", "war", "policy", "trade", "energy", "security", "military", "defense", "escalation")):
        _add_note(row, notes, "yellow", "geopolitical_theme_mismatch", "High-confidence geopolitical/policy headline may be under-classified.")
    if _is_analyst_news(text) and _theme_contains(theme, ("geo", "macro", "policy")):
        _add_note(row, notes, "yellow", "analyst_news_overclassified", "Analyst/rating headline may be over-classified as macro/geopolitical.")
    earnings_like = _is_high_confidence_earnings_event(text)
    if earnings_like and not _theme_contains(theme, ("earn", "single", "stock", "company", "guidance")):
        _add_note(row, notes, "yellow", "earnings_theme_mismatch", "High-confidence earnings/guidance headline is not classified as company/earnings.")
    if source in {"benzinga", "dow_jones"} and not assets:
        _add_note(row, notes, "yellow", "missing_asset_map", "Provider headline has no mapped assets/tickers.")
    elif source in {"benzinga", "dow_jones", "marketaux"} and earnings_like and _only_market_proxy_assets(assets):
        _add_note(
            row,
            notes,
            "yellow",
            "missing_primary_company_asset",
            "Company-specific earnings/guidance headline maps only to broad market proxies.",
        )

    try:
        score = float(row.get("sentiment_score"))
    except (TypeError, ValueError):
        score = None
    if score is not None:
        if score >= 0.45 and sentiment_label.startswith("neg"):
            _add_note(row, notes, "yellow", "sentiment_label_score_conflict", "Positive sentiment score conflicts with negative label.")
        elif score <= -0.45 and sentiment_label.startswith("pos"):
            _add_note(row, notes, "yellow", "sentiment_label_score_conflict", "Negative sentiment score conflicts with positive label.")


def _add_note(row: dict[str, Any], notes: list[dict[str, Any]], severity: str, category: str, reason: str) -> None:
    notes.append(
        {
            "version": SIGNAL_CLASSIFICATION_AUDIT_VERSION,
            "created_at": datetime.now(UTC).isoformat(),
            "severity": severity,
            "category": category,
            "signal_id": row.get("id"),
            "source": row.get("source"),
            "published_at": row.get("published_at"),
            "title": row.get("title"),
            "theme": row.get("theme"),
            "region": row.get("region"),
            "sentiment_score": row.get("sentiment_score"),
            "sentiment_label": row.get("sentiment_label"),
            "assets_json": row.get("assets_json"),
            "inferred_primary_tickers": _infer_primary_tickers(row),
            "reason": reason,
            "training_use": "Feed to M236 source-quality/ticker/theme review; do not directly promote live thresholds.",
            "correction_status": "candidate",
        }
    )


def _dedupe_notes(notes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: dict[tuple[str, str], dict[str, Any]] = {}
    for note in notes:
        title_key = _normalize_title_for_dedupe(str(note.get("title") or ""))
        key = (str(note.get("category") or ""), title_key)
        existing = deduped.get(key)
        if existing is None:
            note["duplicate_count"] = 1
            note["duplicate_sources"] = [note.get("source")]
            note["duplicate_signal_ids"] = [note.get("signal_id")]
            deduped[key] = note
            continue
        existing["duplicate_count"] = int(existing.get("duplicate_count") or 1) + 1
        sources = list(existing.get("duplicate_sources") or [])
        if note.get("source") not in sources:
            sources.append(note.get("source"))
        existing["duplicate_sources"] = sources
        signal_ids = list(existing.get("duplicate_signal_ids") or [])
        if note.get("signal_id") not in signal_ids:
            signal_ids.append(note.get("signal_id"))
        existing["duplicate_signal_ids"] = signal_ids[:20]
    return list(deduped.values())


def _reconcile_notes_with_symbol_tiers(
    notes: list[dict[str, Any]],
    symbol_tier_report_path: str | Path | None,
) -> dict[str, Any]:
    tier_map = _load_symbol_tier_map(symbol_tier_report_path)
    kept: list[dict[str, Any]] = []
    suppressed: list[dict[str, Any]] = []
    kept_missing_tickers: list[dict[str, Any]] = []
    kept_unresolved: list[dict[str, Any]] = []

    for note in notes:
        if note.get("category") != "missing_primary_company_asset":
            kept.append(note)
            continue
        title = str(note.get("title") or "")
        text = title.lower()
        inferred = [ticker for ticker in note.get("inferred_primary_tickers") or [] if ticker]
        present = [ticker for ticker in inferred if ticker in tier_map]
        missing = [ticker for ticker in inferred if ticker not in tier_map]

        if _is_broad_aggregate_market_summary(text):
            _suppress_note(
                note,
                suppressed,
                status="suppressed_broad_aggregate_summary",
                reason="Headline is a broad market/index/earnings-season summary; broad proxy mapping is expected and no single primary ticker is required.",
                present=present,
                missing=missing,
            )
            continue
        if present:
            _suppress_note(
                note,
                suppressed,
                status="suppressed_ticker_already_in_symbol_tiers",
                reason="Inferred primary ticker is already present in the symbol-tier report; do not duplicate this as a missing-tier audit note.",
                present=present,
                missing=missing,
            )
            continue
        if missing:
            note["tier_reconciliation_status"] = "missing_from_symbol_tier_report"
            note["tier_reconciliation_reason"] = (
                "The headline/raw provider entity implies a primary company ticker, but that ticker is not in the current symbol-tier report. "
                "Likely cause: the local source mapped the event to broad market proxies only, and the ticker is outside the current core/macro/event/on-demand tier universe."
            )
            note["missing_from_symbol_tier_report"] = missing
            kept_missing_tickers.append(
                {
                    "signal_id": note.get("signal_id"),
                    "source": note.get("source"),
                    "title": note.get("title"),
                    "missing_tickers": missing,
                    "reason": note["tier_reconciliation_reason"],
                }
            )
            kept.append(note)
            continue
        note["tier_reconciliation_status"] = "ticker_unresolved_by_local_parser"
        note["tier_reconciliation_reason"] = (
            "The note still appears company-specific, but the provider row lacks a clean ticker entity and the title has no unambiguous exchange/ticker marker. "
            "This should be reviewed by a future company-name/ticker resolver before being promoted to ticker-specific learning."
        )
        kept_unresolved.append(
            {
                "signal_id": note.get("signal_id"),
                "source": note.get("source"),
                "title": note.get("title"),
                "reason": note["tier_reconciliation_reason"],
            }
        )
        kept.append(note)

    return {
        "symbol_tier_report_path": str(symbol_tier_report_path) if symbol_tier_report_path else None,
        "symbol_tier_count": len(tier_map),
        "kept_notes": kept,
        "kept_note_count": len(kept),
        "suppressed_note_count": len(suppressed),
        "suppressed_notes": suppressed[:200],
        "kept_missing_ticker_count": len(kept_missing_tickers),
        "kept_missing_tickers": kept_missing_tickers[:200],
        "kept_unresolved_ticker_count": len(kept_unresolved),
        "kept_unresolved_tickers": kept_unresolved[:200],
        "status_counts": dict(Counter(item.get("tier_reconciliation_status") for item in kept if item.get("tier_reconciliation_status"))),
        "suppressed_status_counts": dict(Counter(item.get("tier_reconciliation_status") for item in suppressed)),
    }


def _suppress_note(
    note: dict[str, Any],
    suppressed: list[dict[str, Any]],
    *,
    status: str,
    reason: str,
    present: list[str],
    missing: list[str],
) -> None:
    item = dict(note)
    item["tier_reconciliation_status"] = status
    item["tier_reconciliation_reason"] = reason
    item["present_in_symbol_tier_report"] = present
    item["missing_from_symbol_tier_report"] = missing
    item["correction_status"] = "suppressed_after_tier_reconciliation"
    suppressed.append(item)


def _load_symbol_tier_map(symbol_tier_report_path: str | Path | None) -> dict[str, dict[str, Any]]:
    if symbol_tier_report_path is None:
        return {}
    path = Path(symbol_tier_report_path)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for item in payload.get("symbols") or []:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or "").upper().strip()
        if symbol:
            result[symbol] = item
    return result


def _is_broad_aggregate_market_summary(text: str) -> bool:
    return _has_keyword(
        text,
        (
            "benzinga market summary",
            "stock market today",
            "major indices",
            "major indexes",
            "s&p 500 companies",
            "s&p 500 firms",
            "earnings season",
            "earnings reports have been broadly",
            "this week's earnings reports",
            "dow jones stocks in spotlight",
            "futures fall",
            "futures rise",
            "indices are trading",
            "index on track",
            "stocks are performing",
            "the s&p is higher",
            "s&p is higher",
            "s&p roughly flat",
            "federal reserve vice chair",
            "fed will also",
            "fed will be",
            "investors assess earnings reports",
        ),
    )


def _is_high_confidence_macro_event(text: str) -> bool:
    if not (_has_keyword(text, MACRO_KEYWORDS) and _has_keyword(text, MACRO_HARD_EVENT_KEYWORDS)):
        return False
    if (
        _has_keyword(text, ("ahead of fed decision", "ahead of fomc decision"))
        and _has_keyword(text, ("market summary", "stock market today", "futures"))
    ):
        return False
    if _has_keyword(text, MACRO_PREVIEW_KEYWORDS) and not _has_keyword(text, MACRO_ACTUAL_RESULT_STRONG_KEYWORDS):
        return False
    return True


def _is_high_confidence_geopolitical_event(text: str) -> bool:
    if _has_keyword(text, GEOPOLITICAL_METAPHOR_EXCLUSIONS):
        return False
    has_hard_event = _has_keyword(text, GEOPOLITICAL_HARD_EVENT_KEYWORDS)
    if not has_hard_event:
        return False
    has_region = _has_keyword(text, GEOPOLITICAL_REGION_KEYWORDS)
    has_defense_context = _has_keyword(text, ("missile", "air defense", "drone", "military", "defense", "sanction", "tariff", "export control"))
    return has_region or has_defense_context


def _is_analyst_news(text: str) -> bool:
    if _has_keyword(text, ("price target", "initiates coverage", "resumes coverage")):
        return True
    rating_words = ("buy", "sell", "hold", "neutral", "outperform", "underperform", "overweight", "underweight", "market perform")
    rating_action = _has_keyword(text, ("upgrades", "downgrades", "upgrade", "downgrade", "maintains", "reiterates"))
    return rating_action and _has_keyword(text, rating_words)


def _is_high_confidence_earnings_event(text: str) -> bool:
    if _has_keyword(text, ANALYSIS_OR_OPINION_KEYWORDS) or _has_keyword(text, NON_EARNINGS_RESULTS_KEYWORDS):
        return False
    if _has_keyword(text, ("earnings conference call", "earnings call", "earnings report", "earnings release")):
        return True
    if _has_keyword(text, ("guidance",)) and _has_keyword(text, ("raises", "cuts", "lowers", "issues", "raises guidance", "cuts guidance")):
        return True
    if _has_keyword(text, ("eps",)) and _has_keyword(text, ("revenue", "gaap", "non-gaap", "quarter", "q1", "q2", "q3", "q4")):
        return True
    if _has_keyword(text, ("quarterly results", "financial results", "q1 results", "q2 results", "q3 results", "q4 results")):
        return True
    if _has_keyword(text, ("reports", "reported", "announces", "announced")) and _has_keyword(text, ("earnings", "eps", "quarterly revenue", "gaap eps")):
        return True
    return False


def _normalize_title_for_dedupe(title: str) -> str:
    text = title.lower()
    text = re.sub(r"\{[^}]+\}", " ", text)
    text = re.sub(r"\s+--\s+(market talk|update|dj|benzinga).*$", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())[:220]


def _infer_primary_tickers(row: dict[str, Any]) -> list[str]:
    title = str(row.get("title") or "")
    tickers: list[str] = []
    for pattern in (
        r"\((?:NYSE|NASDAQ|Nasdaq|OTCMKTS|OTC|AMEX|NYSEARCA|NSE|TSX|LON):\s*([A-Z0-9.\-]{1,10})\)",
        r"\(([A-Z]{1,6})\)",
    ):
        for match in re.finditer(pattern, title):
            _append_candidate_ticker(tickers, match.group(1))
    raw = row.get("raw_json")
    try:
        payload = json.loads(str(raw or "{}"))
    except json.JSONDecodeError:
        payload = {}
    entities = payload.get("entities") if isinstance(payload, dict) else None
    if isinstance(entities, list):
        sorted_entities = sorted(
            [entity for entity in entities if isinstance(entity, dict)],
            key=lambda item: float(item.get("match_score") or 0),
            reverse=True,
        )
        preferred = [entity for entity in sorted_entities if str(entity.get("country") or "").lower() == "us"]
        fallback = sorted_entities if not preferred else preferred
        for entity in fallback[:5]:
            if str(entity.get("type") or "").lower() not in {"equity", "stock", ""}:
                continue
            _append_candidate_ticker(tickers, str(entity.get("symbol") or ""))
    return tickers


def _append_candidate_ticker(tickers: list[str], value: str) -> None:
    symbol = str(value or "").upper().strip()
    if not symbol:
        return
    symbol = symbol.split(".", 1)[0]
    symbol = symbol.replace("-", ".") if len(symbol) <= 5 else symbol
    if not re.fullmatch(r"[A-Z][A-Z0-9.]{0,7}", symbol):
        return
    if symbol in MARKET_PROXY_ASSETS or symbol in {"CEO", "CFO", "EPS", "GAAP", "QTR", "IPO", "USA", "FOMC"}:
        return
    if symbol not in tickers:
        tickers.append(symbol)


def _parse_assets(value: Any) -> list[Any]:
    try:
        payload = json.loads(str(value or "[]"))
    except json.JSONDecodeError:
        return []
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        return list(payload.values())
    return []


def _has_keyword(text: str, keywords: tuple[str, ...]) -> bool:
    for keyword in keywords:
        normalized = keyword.lower().strip()
        if not normalized:
            continue
        if " " in normalized:
            if normalized in text:
                return True
        else:
            if re.search(rf"(?<![a-z0-9]){re.escape(normalized)}(?![a-z0-9])", text):
                return True
    return False


def _theme_contains(theme: str, tokens: tuple[str, ...]) -> bool:
    return any(token in theme for token in tokens)


def _only_market_proxy_assets(assets: list[Any]) -> bool:
    normalized = {str(asset).upper().strip() for asset in assets if str(asset or "").strip()}
    return bool(normalized) and normalized.issubset(MARKET_PROXY_ASSETS)


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table_name,)).fetchone() is not None


def _table_has_column(conn: sqlite3.Connection, table_name: str, column_name: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return any(str(row[1]) == column_name for row in rows)
