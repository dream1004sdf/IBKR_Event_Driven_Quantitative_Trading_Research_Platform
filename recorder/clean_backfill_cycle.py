from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from recorder.news_symbol_tiers import build_news_symbol_tier_report


CYCLE_AUDIT_VERSION = "clean-backfill-cycle-audit-v1"
CYCLE_REPORT_VERSION = "clean-backfill-cycle-report-v1"
UTC = timezone.utc


def effective_cycle_mode(cycle_index: int, requested_mode: str = "auto") -> str:
    mode = str(requested_mode or "auto").strip().lower()
    if mode == "auto":
        return "codex-assisted" if int(cycle_index) <= 1 else "blind"
    if mode == "codex-assisted" and int(cycle_index) > 1:
        return "blind"
    if mode not in {"codex-assisted", "blind", "source-locked-blind-reclass"}:
        return "blind"
    return mode


def build_clean_backfill_cycle_audit(
    database_path: str | Path,
    *,
    start: str,
    end: str,
    cycle_index: int,
    cycle_mode: str = "auto",
    phase: str = "evidence_conversion",
    symbol_tier_config: str | Path = "config/news_market_outcome_symbol_tiers_v1.json",
    correction_ledger_path: str | Path | None = None,
) -> dict[str, Any]:
    db_path = Path(database_path)
    mode = effective_cycle_mode(cycle_index, cycle_mode)
    generated_at = datetime.now(UTC).isoformat()
    symbol_report = build_news_symbol_tier_report(
        db_path,
        start=start,
        end=end,
        config_path=symbol_tier_config,
        limit_samples_per_symbol=1,
    )
    anomalies: list[dict[str, Any]] = []
    corrections: list[dict[str, Any]] = []

    _audit_symbol_tiers(symbol_report, anomalies, corrections, mode=mode)
    source_counts = _source_counts(db_path, start=start, end=end)
    _audit_source_coverage(source_counts, anomalies, corrections, mode=mode)
    outcome_summary = _news_outcome_summary(db_path, start=start, end=end)
    _audit_outcomes(outcome_summary, anomalies, corrections, mode=mode)

    report: dict[str, Any] = {
        "version": CYCLE_AUDIT_VERSION,
        "generated_at": generated_at,
        "database_path": str(db_path),
        "start": start,
        "end": end,
        "cycle_index": int(cycle_index),
        "cycle_mode": mode,
        "phase": phase,
        "codex_intervention_allowed": mode == "codex-assisted" and int(cycle_index) <= 1,
        "blind_mode": mode in {"blind", "source-locked-blind-reclass"},
        "symbol_tier_config": str(symbol_tier_config),
        "symbol_tier_counts": symbol_report.get("tier_counts") or {},
        "symbol_collection_mode_counts": symbol_report.get("collection_mode_counts") or {},
        "recommended_symbols": symbol_report.get("recommended_symbols") or {},
        "source_counts": source_counts,
        "news_outcome_summary": outcome_summary,
        "anomaly_count": len(anomalies),
        "anomalies": anomalies,
        "correction_candidates": corrections,
        "operating_rule": (
            "cycle_1_codex_can_patch_code_or_config_when_anomaly_is_material"
            if mode == "codex-assisted" and int(cycle_index) <= 1
            else "record_only_no_mid_cycle_codex_intervention"
        ),
        "safety": {
            "broker_orders_submitted": False,
            "transmit_enabled": False,
            "live_order_routing_enabled": False,
            "live_threshold_promoted": False,
        },
    }
    if correction_ledger_path:
        append_cycle_ledger(correction_ledger_path, report, event_type="cycle_audit")
    return report


def build_clean_backfill_cycle_report(
    *,
    start: str,
    end: str,
    cycle_index: int,
    cycle_mode: str = "auto",
    artifact_dir: str | Path = "exports",
    correction_ledger_path: str | Path | None = None,
) -> dict[str, Any]:
    mode = effective_cycle_mode(cycle_index, cycle_mode)
    artifact_root = Path(artifact_dir)
    artifacts = {
        "summary": artifact_root / "historical_backfill_training_2025_summary.json",
        "news_symbol_tiers": artifact_root / "historical_backfill_news_symbol_tiers.json",
        "news_outcome_next_day": artifact_root / "historical_backfill_news_outcome_next_day.json",
        "news_outcome_intraday_core": artifact_root / "historical_backfill_news_outcome_intraday_core.json",
        "news_outcome_intraday_event": artifact_root / "historical_backfill_news_outcome_intraday_event.json",
        "signal_classification_audit": artifact_root / "clean_backfill_signal_classification_audit.json",
        "paper_action_sim": artifact_root / "historical_backfill_paper_action_sim.json",
        "paper_order_tune": artifact_root / "historical_backfill_paper_order_tune.json",
        "paper_order_walk_forward_tune": artifact_root / "historical_backfill_paper_order_walk_forward_tune.json",
        "threshold_walk_forward_tune": artifact_root / "historical_backfill_walk_forward_threshold_tune.json",
        "nuanced_train": artifact_root / "historical_backfill_walk_forward_nuanced_train.json",
        "strategy_walk_forward_tune": artifact_root / "historical_backfill_strategy_walk_forward_tune.json",
        "prediction_trend": artifact_root / "historical_backfill_prediction_trend.json",
        "m236_news_learning": artifact_root / "m236_news_learning_report.json",
        "cycle_audit_evidence": artifact_root / "historical_backfill_cycle_audit_evidence.json",
    }
    loaded = {name: _load_json(path) for name, path in artifacts.items()}
    metric_summary = _extract_metric_summary(loaded)
    technique_usage = _technique_usage(loaded)
    cycle_stop_decision = _cycle_stop_decision(metric_summary, technique_usage)
    report: dict[str, Any] = {
        "version": CYCLE_REPORT_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "start": start,
        "end": end,
        "cycle_index": int(cycle_index),
        "cycle_mode": mode,
        "codex_intervention_allowed": mode == "codex-assisted" and int(cycle_index) <= 1,
        "next_cycle_recommended_mode": "source-locked-blind-reclass" if mode == "source-locked-blind-reclass" else "blind",
        "optimization_goal": {
            "primary": "minimize_prediction_error",
            "secondary": "maximize_expected_pnl_under_safety_gates",
            "live_trading": "disabled",
        },
        "artifacts": {name: {"path": str(path), "exists": path.exists()} for name, path in artifacts.items()},
        "metric_summary": metric_summary,
        "technique_usage": technique_usage,
        "cycle_stop_decision": cycle_stop_decision,
        "cycle_interpretation": _cycle_interpretation(metric_summary, technique_usage, cycle_stop_decision),
        "safety": {
            "broker_orders_submitted": False,
            "transmit_enabled": False,
            "live_order_routing_enabled": False,
            "live_threshold_promoted": False,
        },
    }
    if correction_ledger_path:
        append_cycle_ledger(correction_ledger_path, report, event_type="cycle_report")
    return report


def append_cycle_ledger(path: str | Path, report: dict[str, Any], *, event_type: str) -> Path:
    ledger_path = Path(path)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "event_type": event_type,
        "generated_at": datetime.now(UTC).isoformat(),
        "cycle_index": report.get("cycle_index"),
        "cycle_mode": report.get("cycle_mode"),
        "phase": report.get("phase"),
        "codex_intervention_allowed": report.get("codex_intervention_allowed"),
        "anomaly_count": report.get("anomaly_count"),
        "operating_rule": report.get("operating_rule"),
        "cycle_decision_status": (report.get("cycle_stop_decision") or {}).get("status"),
        "should_stop_training_cycles": (report.get("cycle_stop_decision") or {}).get("should_stop_training_cycles"),
        "version": report.get("version"),
    }
    with ledger_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    return ledger_path


def _audit_symbol_tiers(
    symbol_report: dict[str, Any],
    anomalies: list[dict[str, Any]],
    corrections: list[dict[str, Any]],
    *,
    mode: str,
) -> None:
    for symbol in symbol_report.get("recommended_symbols", {}).get("unclassified_review", []) or []:
        _add_anomaly(
            anomalies,
            corrections,
            severity="yellow",
            category="symbol_tier_unclassified",
            subject=symbol,
            message="Symbol appeared during clean backfill but is not in the tier policy.",
            suggested_action="review_and_add_tier" if mode == "codex-assisted" else "record_for_post_cycle_review",
        )
    for item in symbol_report.get("symbols") or []:
        tier = item.get("tier")
        mode_name = item.get("collection_mode")
        news_count = int(item.get("news_count") or 0)
        primary_counts = item.get("primary_symbol_counts") or {}
        direct_count = int(primary_counts.get(item.get("symbol"), 0) or 0)
        direct_ratio = direct_count / max(1, news_count)
        if tier == "opportunistic_single_stock" and (news_count >= 100 or direct_ratio >= 0.75):
            _add_anomaly(
                anomalies,
                corrections,
                severity="yellow",
                category="opportunistic_symbol_may_need_promotion",
                subject=str(item.get("symbol")),
                message=f"On-demand symbol has {news_count} news rows and direct ratio {direct_ratio:.2f}.",
                suggested_action="consider_promoting_to_core_or_theme" if mode == "codex-assisted" else "record_only",
            )
        if mode_name == "event_window" and news_count > 500:
            _add_anomaly(
                anomalies,
                corrections,
                severity="yellow",
                category="event_proxy_too_broad",
                subject=str(item.get("symbol")),
                message=f"Event proxy has unusually broad coverage ({news_count} rows).",
                suggested_action="tighten_keywords_or_split_tier" if mode == "codex-assisted" else "record_only",
            )


def _audit_outcomes(
    outcome_summary: dict[str, Any],
    anomalies: list[dict[str, Any]],
    corrections: list[dict[str, Any]],
    *,
    mode: str,
) -> None:
    pending = outcome_summary.get("pending_by_horizon") or {}
    coverage = outcome_summary.get("stored_by_horizon") or {}
    if int(coverage.get("NEXT_DAY", {}).get("rows", 0) or 0) == 0:
        _add_anomaly(
            anomalies,
            corrections,
            severity="red",
            category="next_day_outcome_missing",
            subject="NEXT_DAY",
            message="No broad next-day news market outcomes are available.",
            suggested_action="run_next_day_outcome_backfill" if mode == "codex-assisted" else "record_only",
        )
    for horizon in ("5M", "15M", "1H", "CLOSE"):
        pending_rows = int(pending.get(horizon, 0) or 0)
        stored_rows = int(coverage.get(horizon, {}).get("rows", 0) or 0)
        if stored_rows == 0 and pending_rows > 0:
            _add_anomaly(
                anomalies,
                corrections,
                severity="yellow",
                category="intraday_outcome_not_filled",
                subject=horizon,
                message=f"{horizon} has pending candidates but no stored tiered outcomes.",
                suggested_action="run_tiered_intraday_backfill" if mode == "codex-assisted" else "record_only",
            )


def _audit_source_coverage(
    source_counts: dict[str, Any],
    anomalies: list[dict[str, Any]],
    corrections: list[dict[str, Any]],
    *,
    mode: str,
) -> None:
    for note in source_counts.get("source_coverage_notes") or []:
        _add_anomaly(
            anomalies,
            corrections,
            severity=str(note.get("severity") or "yellow"),
            category=str(note.get("category") or "source_coverage_note"),
            subject=str(note.get("subject") or "source_coverage"),
            message=str(note.get("message") or "Provider/source coverage needs review."),
            suggested_action=(
                "targeted_provider_backfill_or_weight_adjustment"
                if mode == "codex-assisted"
                else "record_for_post_cycle_review"
            ),
        )


def _add_anomaly(
    anomalies: list[dict[str, Any]],
    corrections: list[dict[str, Any]],
    *,
    severity: str,
    category: str,
    subject: str,
    message: str,
    suggested_action: str,
) -> None:
    item = {
        "severity": severity,
        "category": category,
        "subject": subject,
        "message": message,
        "suggested_action": suggested_action,
    }
    anomalies.append(item)
    corrections.append({**item, "correction_status": "candidate"})


def _source_counts(db_path: Path, *, start: str, end: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    start_bound, end_exclusive = _published_at_bounds(start, end)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        if _table_exists(conn, "news_articles"):
            provider_first_expr = (
                "min(provider_published_at)"
                if _table_has_column(conn, "news_articles", "provider_published_at")
                else "NULL"
            )
            provider_last_expr = (
                "max(provider_published_at)"
                if _table_has_column(conn, "news_articles", "provider_published_at")
                else "NULL"
            )
            primary_symbol_expr = (
                "count(DISTINCT primary_symbol)"
                if _table_has_column(conn, "news_articles", "primary_symbol")
                else "0"
            )
            provider_rows = list(
                conn.execute(
                    f"""
                    SELECT provider,
                           count(*) AS count,
                           min(published_at) AS first_published_at,
                           max(published_at) AS last_published_at,
                           {provider_first_expr} AS first_provider_published_at,
                           {provider_last_expr} AS last_provider_published_at,
                           {primary_symbol_expr} AS primary_symbol_count
                      FROM news_articles
                     WHERE published_at >= ? AND published_at < ?
                     GROUP BY provider
                     ORDER BY count DESC
                    """,
                    (start_bound, end_exclusive),
                )
            )
            result["news_articles_by_provider"] = {str(row["provider"]): int(row["count"]) for row in provider_rows}
            result["news_article_provider_coverage"] = [_row_to_plain_dict(row) for row in provider_rows]
        if _table_exists(conn, "external_market_signals"):
            source_col = "source" if _table_has_column(conn, "external_market_signals", "source") else None
            published_col = "published_at" if _table_has_column(conn, "external_market_signals", "published_at") else None
            if source_col and published_col:
                source_rows = list(
                    conn.execute(
                        """
                        SELECT source,
                               count(*) AS count,
                               min(published_at) AS first_published_at,
                               max(published_at) AS last_published_at,
                               count(DISTINCT region) AS region_count,
                               count(DISTINCT theme) AS theme_count
                          FROM external_market_signals
                         WHERE published_at >= ? AND published_at < ?
                         GROUP BY source
                         ORDER BY count DESC
                        """,
                        (start_bound, end_exclusive),
                    )
                )
                result["external_market_signals_by_source"] = {str(row["source"]): int(row["count"]) for row in source_rows}
                result["external_market_signal_source_coverage"] = [_row_to_plain_dict(row) for row in source_rows]
    result["source_coverage_notes"] = _source_coverage_notes(result, start=start)
    return result


def _published_at_bounds(start: str, end: str) -> tuple[str, str]:
    start_day = _parse_date_like(start)
    end_day = _parse_date_like(end)
    if end_day < start_day:
        end_day = start_day
    return f"{start_day:%Y-%m-%d}T00:00:00", f"{(end_day + timedelta(days=1)):%Y-%m-%d}T00:00:00"


def _parse_date_like(value: str) -> datetime:
    text = str(value or "").strip().replace("/", "-")
    if len(text) == 10:
        return datetime.fromisoformat(text)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


def _row_to_plain_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {str(key): row[key] for key in row.keys()}


def _source_coverage_notes(source_counts: dict[str, Any], *, start: str) -> list[dict[str, Any]]:
    notes: list[dict[str, Any]] = []
    news_counts = source_counts.get("news_articles_by_provider") or {}
    external_counts = source_counts.get("external_market_signals_by_source") or {}
    bz_articles = int(news_counts.get("BZ", 0) or 0)
    dj_articles = sum(int(count or 0) for provider, count in news_counts.items() if str(provider).upper().startswith("DJ"))
    benzinga_signals = int(external_counts.get("benzinga", 0) or 0)
    dow_jones_signals = int(external_counts.get("dow_jones", 0) or 0)
    marketaux_signals = int(external_counts.get("marketaux", 0) or 0)

    if bz_articles:
        _append_thin_coverage_note(
            notes,
            subject="dow_jones_news_articles",
            count=dj_articles,
            baseline_count=bz_articles,
            baseline_label="BZ news_articles",
        )
    if benzinga_signals:
        _append_thin_coverage_note(
            notes,
            subject="dow_jones_external_market_signals",
            count=dow_jones_signals,
            baseline_count=benzinga_signals,
            baseline_label="benzinga external_market_signals",
        )
        _append_thin_coverage_note(
            notes,
            subject="marketaux_external_market_signals",
            count=marketaux_signals,
            baseline_count=benzinga_signals,
            baseline_label="benzinga external_market_signals",
        )

    for item in source_counts.get("news_article_provider_coverage") or []:
        provider = str(item.get("provider") or "")
        if provider.upper().startswith("DJ"):
            _append_late_start_note(notes, subject=f"news_articles:{provider}", first_published_at=item.get("first_published_at"), start=start)
    for item in source_counts.get("external_market_signal_source_coverage") or []:
        source = str(item.get("source") or "")
        if source in {"dow_jones", "marketaux"}:
            _append_late_start_note(notes, subject=f"external_market_signals:{source}", first_published_at=item.get("first_published_at"), start=start)
    return notes


def _append_thin_coverage_note(
    notes: list[dict[str, Any]],
    *,
    subject: str,
    count: int,
    baseline_count: int,
    baseline_label: str,
    warn_ratio: float = 0.25,
) -> None:
    ratio = count / max(1, baseline_count)
    if ratio < warn_ratio:
        notes.append(
            {
                "severity": "yellow",
                "category": "source_coverage_thin_vs_benchmark",
                "subject": subject,
                "count": count,
                "baseline_count": baseline_count,
                "baseline_label": baseline_label,
                "ratio": round(ratio, 4),
                "message": (
                    f"{subject} has {count} rows versus {baseline_count} rows in {baseline_label} "
                    f"(ratio {ratio:.2%}); treat provider agreement and conflict scores as coverage-sensitive."
                ),
            }
        )


def _append_late_start_note(
    notes: list[dict[str, Any]],
    *,
    subject: str,
    first_published_at: Any,
    start: str,
    warn_days: int = 30,
) -> None:
    if not first_published_at:
        return
    try:
        first_day = _parse_date_like(str(first_published_at))
        start_day = _parse_date_like(start)
    except ValueError:
        return
    days_after = (first_day.date() - start_day.date()).days
    if days_after > warn_days:
        notes.append(
            {
                "severity": "yellow",
                "category": "source_history_starts_after_cycle_anchor",
                "subject": subject,
                "first_published_at": str(first_published_at),
                "cycle_start": str(start),
                "days_after_cycle_start": days_after,
                "message": (
                    f"{subject} first appears {days_after} days after the cycle start; "
                    "early-window provider agreement/conflict should not be interpreted as missing confirmation."
                ),
            }
        )


def _news_outcome_summary(db_path: Path, *, start: str, end: str) -> dict[str, Any]:
    horizons = ("NEXT_DAY", "5M", "15M", "1H", "CLOSE")
    result: dict[str, Any] = {"stored_by_horizon": {}, "pending_by_horizon": {}}
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        if not _table_exists(conn, "market_reactions"):
            return result
        for horizon in horizons:
            row = conn.execute(
                """
                SELECT count(*) AS rows, count(DISTINCT news_id) AS news_count, count(DISTINCT symbol) AS symbol_count
                  FROM market_reactions
                 WHERE upper(horizon)=?
                   AND usage_reason LIKE 'news-market-outcome-backfill-v1%'
                """,
                (horizon,),
            ).fetchone()
            result["stored_by_horizon"][horizon] = dict(row)
        if _table_exists(conn, "news_articles") and _table_exists(conn, "news_symbols"):
            for horizon in horizons:
                row = conn.execute(
                    """
                    SELECT count(DISTINCT n.id || ':' || upper(ns.symbol)) AS pending
                      FROM news_articles n
                      JOIN news_symbols ns ON ns.news_id=n.id
                     WHERE n.published_at >= ?
                       AND n.published_at <= ?
                       AND ns.symbol IS NOT NULL
                       AND ns.symbol <> ''
                       AND ns.symbol NOT LIKE '%:%'
                       AND NOT EXISTS (
                           SELECT 1 FROM market_reactions mr
                            WHERE mr.news_id=n.id
                              AND upper(mr.symbol)=upper(ns.symbol)
                              AND upper(mr.horizon)=?
                              AND mr.usage_reason LIKE 'news-market-outcome-backfill-v1%'
                       )
                    """,
                    (start, end, horizon),
                ).fetchone()
                result["pending_by_horizon"][horizon] = int(row["pending"] or 0)
    return result


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        if not path.exists():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {"value": payload}
    except Exception as exc:
        return {"load_error": str(exc)}


def _extract_metric_summary(loaded: dict[str, dict[str, Any] | None]) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    interesting = ("accuracy", "hit_rate", "win_rate", "pnl", "expected_pnl", "expected_return", "profit_factor", "brier", "ece", "sharpe", "drawdown")
    for name, payload in loaded.items():
        if not payload:
            continue
        found: dict[str, Any] = {}
        _walk_metrics(payload, found, interesting, prefix="")
        if found:
            metrics[name] = dict(list(found.items())[:40])
    return metrics


def _walk_metrics(value: Any, out: dict[str, Any], interesting: tuple[str, ...], *, prefix: str) -> None:
    if len(out) >= 120:
        return
    if isinstance(value, dict):
        for key, child in value.items():
            normalized_key = str(key).lower()
            next_prefix = f"{prefix}.{key}" if prefix else str(key)
            if any(token in normalized_key for token in interesting) and isinstance(child, (int, float, str, bool, type(None))):
                out[next_prefix] = child
            elif isinstance(child, (dict, list)):
                _walk_metrics(child, out, interesting, prefix=next_prefix)
    elif isinstance(value, list):
        for index, item in enumerate(value[:10]):
            _walk_metrics(item, out, interesting, prefix=f"{prefix}[{index}]")


def _technique_usage(loaded: dict[str, dict[str, Any] | None]) -> dict[str, Any]:
    return {
        "tiered_news_outcomes": bool(loaded.get("news_outcome_next_day") or loaded.get("news_outcome_intraday_core") or loaded.get("news_outcome_intraday_event")),
        "symbol_tier_filtering": bool(loaded.get("news_symbol_tiers")),
        "paper_action_simulation": bool(loaded.get("paper_action_sim")),
        "paper_order_tuning": bool(loaded.get("paper_order_tune") or loaded.get("paper_order_walk_forward_tune")),
        "threshold_walk_forward_tuning": bool(loaded.get("threshold_walk_forward_tune")),
        "nuanced_action_training": bool(loaded.get("nuanced_train")),
        "strategy_allocation_tuning": bool(loaded.get("strategy_walk_forward_tune")),
        "provider_news_learning": bool(loaded.get("m236_news_learning")),
        "prediction_trend_tracking": bool(loaded.get("prediction_trend")),
        "signal_classification_review": bool(loaded.get("signal_classification_audit")),
        "source_coverage_audit": bool(
            (loaded.get("cycle_audit_evidence") or {}).get("source_counts")
            if loaded.get("cycle_audit_evidence")
            else False
        ),
    }


def _cycle_stop_decision(metrics: dict[str, Any], techniques: dict[str, Any]) -> dict[str, Any]:
    targets = {
        "min_tradeable_accuracy": 0.60,
        "min_high_confidence_accuracy": 0.65,
        "min_profit_factor": 1.25,
        "max_drawdown_fraction": 0.15,
        "require_positive_expected_or_realized_pnl": True,
        "require_recent_trend_not_negative": True,
        "no_fixed_cycle_cap": True,
    }
    gates = [
        _metric_gate(
            name="tradeable_accuracy",
            value=_metric_value(metrics, "threshold_walk_forward_tune", ("tradeable_accuracy",)),
            op=">=",
            target=targets["min_tradeable_accuracy"],
            required=True,
        ),
        _metric_gate(
            name="high_confidence_or_clean_directional_accuracy",
            value=_metric_value(metrics, "nuanced_train", ("clean_directional.directional_accuracy", "tradeable_after_nuanced_action.directional_accuracy")),
            op=">=",
            target=targets["min_high_confidence_accuracy"],
            required=True,
        ),
        _metric_gate(
            name="profit_factor",
            value=_metric_value(metrics, "strategy_walk_forward_tune", ("profit_factor",)),
            op=">=",
            target=targets["min_profit_factor"],
            required=False,
        ),
        _metric_gate(
            name="drawdown",
            value=_metric_value(metrics, "strategy_walk_forward_tune", ("max_drawdown", "drawdown")),
            op="<=",
            target=targets["max_drawdown_fraction"],
            required=False,
            absolute_value=True,
        ),
        _metric_gate(
            name="positive_strategy_pnl",
            value=_metric_value(metrics, "strategy_walk_forward_tune", ("test_summary.total_realized_pnl", "total_realized_pnl", "expected_pnl")),
            op=">",
            target=0.0,
            required=True,
        ),
        _metric_gate(
            name="recent_trend_not_negative",
            value=_metric_value(metrics, "prediction_trend", ("direction_accuracy_delta",)),
            op=">=",
            target=-0.02,
            required=False,
        ),
    ]
    required_gates = [gate for gate in gates if gate["required"]]
    missing_required = [gate for gate in required_gates if gate["status"] == "missing"]
    failed_required = [gate for gate in required_gates if gate["status"] == "fail"]
    optional_failures = [gate for gate in gates if not gate["required"] and gate["status"] == "fail"]
    optional_missing = [gate for gate in gates if not gate["required"] and gate["status"] == "missing"]
    critical_techniques_present = all(
        techniques.get(key)
        for key in (
            "threshold_walk_forward_tuning",
            "nuanced_action_training",
            "strategy_allocation_tuning",
            "prediction_trend_tracking",
        )
    )

    if not metrics:
        status = "continue_blind_cycles"
        reason = "No metric artifacts are available yet."
        should_stop = False
    elif not critical_techniques_present:
        status = "continue_blind_cycles"
        reason = "One or more critical report/training artifacts are missing."
        should_stop = False
    elif missing_required or failed_required or optional_failures:
        status = "continue_blind_cycles"
        reason = "Full-auto readiness gates are not all satisfied yet."
        should_stop = False
    else:
        status = "full_auto_candidate_ready"
        reason = "Required readiness gates passed; stop repeated blind cycles and move to paper/live-read-only validation."
        should_stop = True

    return {
        "version": "clean-backfill-cycle-stop-decision-v1",
        "status": status,
        "should_stop_training_cycles": should_stop,
        "next_action": (
            "paper_and_live_read_only_validation_before_limited_auto"
            if should_stop
            else "run_next_blind_cycle_on_same_fixed_dataset"
        ),
        "reason": reason,
        "targets": targets,
        "gates": gates,
        "missing_required_gates": [gate["name"] for gate in missing_required],
        "failed_required_gates": [gate["name"] for gate in failed_required],
        "optional_missing_gates": [gate["name"] for gate in optional_missing],
        "optional_failed_gates": [gate["name"] for gate in optional_failures],
        "cycle_policy": "There is no fixed 2-3 cycle cap; keep blind cycles running until readiness gates pass or post-cycle review changes the policy.",
        "live_routing": "disabled",
    }


def _metric_gate(
    *,
    name: str,
    value: float | None,
    op: str,
    target: float,
    required: bool,
    absolute_value: bool = False,
) -> dict[str, Any]:
    compare_value = abs(value) if absolute_value and value is not None else value
    if compare_value is None:
        status = "missing"
    elif op == ">=":
        status = "pass" if compare_value >= target else "fail"
    elif op == ">":
        status = "pass" if compare_value > target else "fail"
    elif op == "<=":
        status = "pass" if compare_value <= target else "fail"
    else:
        status = "missing"
    return {
        "name": name,
        "value": value,
        "op": op,
        "target": target,
        "required": required,
        "status": status,
    }


def _metric_value(metrics: dict[str, Any], artifact: str, name_fragments: tuple[str, ...]) -> float | None:
    artifact_metrics = metrics.get(artifact) or {}
    if not isinstance(artifact_metrics, dict):
        return None
    for fragment in name_fragments:
        normalized_fragment = fragment.lower()
        for key, value in artifact_metrics.items():
            if normalized_fragment in str(key).lower():
                parsed = _as_float(value)
                if parsed is not None:
                    return parsed
    return None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip().replace("%", ""))
        except ValueError:
            return None
    return None


def _cycle_interpretation(metrics: dict[str, Any], techniques: dict[str, Any], decision: dict[str, Any]) -> list[str]:
    notes = []
    if techniques.get("tiered_news_outcomes"):
        notes.append("News outcome labels were tiered by symbol role; compare against older all-symbol attempts to verify speed/coverage tradeoff.")
    if techniques.get("threshold_walk_forward_tuning"):
        notes.append("Threshold walk-forward tuning participated in this cycle.")
    if techniques.get("strategy_allocation_tuning"):
        notes.append("Strategy allocation tuning participated in this cycle; inspect expected PnL and drawdown metrics before promotion.")
    if techniques.get("signal_classification_review"):
        notes.append("Signal classification audit notes participated in the final M236 source-quality review.")
    if techniques.get("source_coverage_audit"):
        notes.append("Source coverage audit participated in this cycle; interpret BZ/DJ/Marketaux agreement scores with provider-depth differences in mind.")
    if not metrics:
        notes.append("No metric JSON artifacts were available yet; run the final report after training stages complete.")
    if decision.get("status") == "continue_blind_cycles":
        notes.append("Readiness gates are not all satisfied; continue blind cycles on the same fixed dataset instead of re-running clean backfill.")
    elif decision.get("status") == "full_auto_candidate_ready":
        notes.append("Repeated blind training can stop here; the next step is paper/live-read-only validation, not immediate live routing.")
    return notes


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table_name,)).fetchone()
    return row is not None


def _table_has_column(conn: sqlite3.Connection, table_name: str, column_name: str) -> bool:
    return any(str(row[1]).lower() == column_name.lower() for row in conn.execute(f"PRAGMA table_info({table_name})"))


def clean_backfill_cycle_audit_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Clean Backfill Cycle Audit",
        "",
        f"- Cycle: `{report.get('cycle_index')}`",
        f"- Mode: `{report.get('cycle_mode')}`",
        f"- Phase: `{report.get('phase')}`",
        f"- Codex intervention allowed: `{report.get('codex_intervention_allowed')}`",
        f"- Operating rule: `{report.get('operating_rule')}`",
        f"- Anomalies: `{report.get('anomaly_count')}`",
        "",
        "## Symbol tiers",
        "",
        f"- Tier counts: `{report.get('symbol_tier_counts') or {}}`",
        f"- Collection mode counts: `{report.get('symbol_collection_mode_counts') or {}}`",
        "",
        "## Source coverage",
        "",
        f"- News articles by provider: `{(report.get('source_counts') or {}).get('news_articles_by_provider') or {}}`",
        f"- External market signals by source: `{(report.get('source_counts') or {}).get('external_market_signals_by_source') or {}}`",
        "",
        "### Source coverage notes",
        "",
    ]
    for note in (report.get("source_counts") or {}).get("source_coverage_notes") or []:
        lines.append(f"- `{note.get('severity')}` `{note.get('category')}` `{note.get('subject')}`: {note.get('message')}")
    if not (report.get("source_counts") or {}).get("source_coverage_notes"):
        lines.append("- None.")
    lines.extend(
        [
            "",
            "## Outcome summary",
            "",
            f"- Stored by horizon: `{(report.get('news_outcome_summary') or {}).get('stored_by_horizon') or {}}`",
            f"- Pending by horizon: `{(report.get('news_outcome_summary') or {}).get('pending_by_horizon') or {}}`",
            "",
            "## Anomalies",
            "",
        ]
    )
    for item in report.get("anomalies") or []:
        lines.append(f"- `{item.get('severity')}` `{item.get('category')}` `{item.get('subject')}`: {item.get('message')} -> `{item.get('suggested_action')}`")
    if not report.get("anomalies"):
        lines.append("- None.")
    lines.extend(["", "## Safety", "", "- No broker orders, no transmit, no live routing, no live threshold promotion."])
    return "\n".join(lines) + "\n"


def clean_backfill_cycle_report_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Clean Backfill Cycle Report",
        "",
        f"- Cycle: `{report.get('cycle_index')}`",
        f"- Mode: `{report.get('cycle_mode')}`",
        f"- Codex intervention allowed: `{report.get('codex_intervention_allowed')}`",
        f"- Next recommended mode: `{report.get('next_cycle_recommended_mode')}`",
        f"- Cycle decision: `{(report.get('cycle_stop_decision') or {}).get('status')}`",
        f"- Stop repeated cycles: `{(report.get('cycle_stop_decision') or {}).get('should_stop_training_cycles')}`",
        "",
        "## Technique usage",
        "",
    ]
    for key, value in (report.get("technique_usage") or {}).items():
        lines.append(f"- `{key}`: `{value}`")
    lines.extend(["", "## Metric summary", ""])
    if report.get("metric_summary"):
        for artifact, metrics in (report.get("metric_summary") or {}).items():
            lines.append(f"### `{artifact}`")
            for key, value in (metrics or {}).items():
                lines.append(f"- `{key}`: `{value}`")
            lines.append("")
    else:
        lines.append("- No metric JSON artifacts available yet.")
    decision = report.get("cycle_stop_decision") or {}
    lines.extend(["", "## Cycle stop / repeat decision", ""])
    lines.append(f"- Status: `{decision.get('status')}`")
    lines.append(f"- Next action: `{decision.get('next_action')}`")
    lines.append(f"- Reason: {decision.get('reason')}")
    lines.append(f"- Policy: {decision.get('cycle_policy')}")
    lines.append("")
    lines.append("| Gate | Value | Target | Required | Status |")
    lines.append("| --- | ---: | ---: | --- | --- |")
    for gate in decision.get("gates") or []:
        lines.append(
            f"| `{gate.get('name')}` | `{gate.get('value')}` | `{gate.get('op')} {gate.get('target')}` | `{gate.get('required')}` | `{gate.get('status')}` |"
        )
    lines.extend(["", "## Interpretation", ""])
    for note in report.get("cycle_interpretation") or []:
        lines.append(f"- {note}")
    lines.extend(["", "## Safety", "", "- No broker orders, no transmit, no live routing, no live threshold promotion."])
    return "\n".join(lines) + "\n"


def write_cycle_report(report: dict[str, Any], *, output: Path | None, json_output_path: Path | None, kind: str) -> None:
    if json_output_path:
        json_output_path.parent.mkdir(parents=True, exist_ok=True)
        json_output_path.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        if kind == "audit":
            output.write_text(clean_backfill_cycle_audit_markdown(report), encoding="utf-8")
        else:
            output.write_text(clean_backfill_cycle_report_markdown(report), encoding="utf-8")
